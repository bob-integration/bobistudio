# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Auto-recovery de la flotte au reboot d'un nœud.

Problème : le moteur MTL (2110_io) tourne en `docker run --rm` → un reboot du nœud le fait
DISPARAÎTRE ; les conteneurs compute (`--restart unless-stopped`) sont relevés par Docker
mais rien ne vérifie ni ne relève ce qui manque. La boucle `surveillance` alerte seulement
(anti restart-storm) — ce module comble le trou, UNIQUEMENT dans le cas précis « le nœud
vient de rebooter », one-shot par boot.

Détection (étape 2, toujours active) : l'agent expose déjà `host_uptime_s` dans /v1/health
(le collecteur SSH legacy aussi) → `boot_ts ≈ ts_snapshot − uptime`. Une régression d'uptime
= boot_ts qui AVANCE au-delà de la tolérance. Persisté dans `nodes.last_boot_ts` /
`nodes.recovered_boot_ts` → la détection et le one-shot survivent à un restart du contrôleur.

Recovery (étapes 3-4, gated par le réglage `auto_recovery_enabled`, défaut OFF, overridable
par nœud via node_settings) : après une grace period (laisser Docker relever les
`unless-stopped` et systemd relancer PTP), on relève dans l'ordre : PTP (vérif) → moteur MTL
(producteur racine, seul détruit par --rm) → compute/media, en ne touchant QUE les conteneurs
`desired_state='running'`. Max N tentatives + backoff par conteneur, verrou par nœud,
`recovered_boot_ts` écrit AVANT d'agir (ferme la fenêtre de double déclenchement).
"""
import json
import logging
import threading
import time

from .database import (db_get_containers, db_get_node, db_add_alert, db_set_node_boot)
from . import settings as st

log = logging.getLogger(__name__)

# Tolérance sur la dérive de boot_ts recalculé (arrondis /proc/uptime, latence de collecte,
# NTP) : un « nouveau boot » n'est retenu que s'il est postérieur de plus de BOOT_TOLERANCE_S
# au dernier boot connu.
BOOT_TOLERANCE_S = 120

_recovery_locks = {}            # node_id -> Lock (anti double recovery pendant le même boot)
_registry_lock = threading.Lock()


def _lock_for(node_id):
    with _registry_lock:
        lk = _recovery_locks.get(node_id)
        if lk is None:
            lk = _recovery_locks[node_id] = threading.Lock()
        return lk


def _cfg(key, default):
    """Réglage avec override par-nœud si dispo (setting_for), sinon global, sinon défaut."""
    try:
        v = st.get(key)
        return type(default)(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _cfg_node(key, node_id, default):
    """setting_for (override par-nœud > global) avec repli sur défaut typé."""
    try:
        v = st.setting_for(key, node_id)
        return type(default)(v) if v not in (None, "") else default
    except (TypeError, ValueError, AttributeError):
        return _cfg(key, default)


def on_health_snapshot(node, snap):
    """Appelé par node_health._sample_one à chaque snapshot santé (best-effort, ne lève jamais).
    Détecte un reboot (régression d'uptime persistée) et déclenche le recovery si activé."""
    try:
        nid = node.get("id")
        uptime = snap.get("host_uptime_s")
        if nid is None or uptime is None:
            return
        boot_ts = float(snap.get("ts") or time.time()) - float(uptime)
        last = node.get("last_boot_ts")
        if last is None:
            # Première observation de ce nœud : on initialise SANS alerter ni relever — le boot
            # courant est l'état de référence (sinon chaque enrôlement déclencherait un recovery).
            db_set_node_boot(nid, last_boot_ts=boot_ts, recovered_boot_ts=boot_ts)
            return
        if boot_ts <= float(last) + BOOT_TOLERANCE_S:
            return                          # même boot (dérive dans la tolérance)
        # ── Reboot détecté ────────────────────────────────────────────────────────────
        db_set_node_boot(nid, last_boot_ts=boot_ts)
        name = node.get("name") or node.get("host") or f"nœud {nid}"
        # Vérification de la prép hôte : HORS du gate auto_recovery_enabled (lecture seule) et
        # dans son propre thread (elle attend la grace puis fait un aller-retour agent — elle ne
        # doit jamais retenir le sampler santé, ni la reprise des conteneurs ci-dessous).
        threading.Thread(target=_verifier_prep_post_boot, args=(nid, name),
                         daemon=True, name=f"node-prep-check-{nid}").start()
        if not _cfg_node("auto_recovery_enabled", nid, 0):
            db_add_alert("alert.node.reboot_recovery_off", "warning", node_id=nid, kind="node",
                         params={"n": name, "uptime": int(uptime)})
            # Certs mTLS : la CONSTATATION est hors du gate (lecture seule), comme la prép hôte —
            # sinon un parc en auto-recovery OFF (le défaut !) subit la panne de certs sans le
            # moindre message. La RÉPARATION, elle, reste dans _recover_node (gated).
            threading.Thread(target=_certs_conteneurs_node, args=(nid, name, False),
                             daemon=True, name=f"node-tls-check-{nid}").start()
            # recovered_boot_ts suit quand même : si l'opérateur ACTIVE le réglage plus tard,
            # on ne « rattrape » pas un vieux boot déjà géré à la main.
            db_set_node_boot(nid, recovered_boot_ts=boot_ts)
            return
        db_add_alert("alert.node.reboot_recovery_lance", "warning", node_id=nid, kind="node",
                     params={"n": name, "uptime": int(uptime),
                             "grace": _cfg_node('auto_recovery_grace_s', nid, 45)})
        threading.Thread(target=_recover_node, args=(nid, boot_ts),
                         daemon=True, name=f"node-recovery-{nid}").start()
    except Exception as e:
        log.warning("node_recovery snapshot nœud %s : %s", node.get("id"), e)


# ─── Moteur de recovery (étapes 3-4) ──────────────────────────────────────────────────

def _desired_running(node_id):
    """Conteneurs de ce nœud censés tourner : backend docker + deploy_config non vide +
    desired_state='running'. Renvoie (mtl, compute) — le moteur MTL d'abord (producteur racine)."""
    from .docker_compute import is_compute_container
    mtl, compute = [], []
    for c in db_get_containers():
        if c.get("node_id") != node_id:
            continue
        if c.get("desired_state") != "running":
            # STRICT : arrêt volontaire ('stopped') OU intention jamais exprimée (NULL = créé
            # mais jamais déployé/démarré) → on ne touche pas.
            continue
        try:
            dc = json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            dc = {}
        if not dc.get("type"):
            continue                        # jamais déployé → rien à relever
        (compute if is_compute_container(c) else mtl).append(c)
    return mtl, compute


def _try_start(c, starter, attempts, backoff_s):
    """Tente `starter(vmid)` jusqu'à `attempts` fois (backoff entre les essais).
    True si le conteneur est up à la fin."""
    vmid = c["vmid"]
    for i in range(max(1, attempts)):
        try:
            ok = starter(vmid)
            if isinstance(ok, tuple):       # stop/start renvoient parfois (ok, msg)
                ok = ok[0]
            if ok:
                return True
        except Exception as e:
            log.warning("recovery start vmid=%s (essai %d) : %s", vmid, i + 1, e)
        if i + 1 < attempts:
            time.sleep(backoff_s)
    return False


def _check_ptp(node):
    """Vérifie (et relance au besoin) PTP sur le nœud — systemd doit l'avoir relevé au boot,
    on ne fait que constater/rattraper. Best-effort : jamais bloquant pour la suite."""
    nid = node.get("id")
    try:
        if not _cfg_node("ptp_enabled", nid, 0):
            return
        from . import node_driver
        stp = node_driver.ptp_status(node) or {}
        if stp.get("running"):
            return
        log.info("recovery nœud %s : PTP non relevé par systemd → start", nid)
        ok, data = node_driver.ptp_start(node)
        if not ok:
            db_add_alert("alert.ptp.recovery_echec", "error", node_id=nid, kind="ptp",
                         params={"n": node.get('name') or nid, "data": str(data)})
    except Exception as e:
        log.warning("recovery PTP nœud %s : %s", nid, e)


# ─── Vérification de la prép hôte après un reboot ────────────────────────────────────
# POURQUOI : jusqu'ici, un reboot de nœud ne vérifiait RIEN de la préparation hôte. Le seul
# contrôle de prép existant (`docker_driver._bind_ports_vfio`) ne se déclenche qu'au moment de
# BINDER un port lors d'un DÉPLOIEMENT — or après un reboot le moteur est simplement REDÉMARRÉ,
# donc cette porte n'est jamais franchie. `mtl.verifier()` n'était appelé qu'à la demande, quand
# un humain ouvre le panneau du nœud. Conséquence : une prép qui se dégrade au boot (unité systemd
# en échec, cmdline édité, fréquence retombée au plancher) restait INVISIBLE jusqu'à ce que les
# flux tombent — typiquement plusieurs heures plus tard, sans lien apparent avec le reboot.
#
# Cette passe est délibérément INDÉPENDANTE de `auto_recovery_enabled` : constater et alerter est
# en lecture seule, et c'est utile même quand on ne veut PAS que l'orchestrateur relève les
# conteneurs tout seul. Elle ne tourne que sur les nœuds qui font de la 2110 (ailleurs il n'y a
# pas de prép MTL à vérifier, et alerter serait du bruit).

def _prep_checks():
    """Table des contrôles de prép hôte post-boot : (clé, prédicat sur le dict `verifier`,
    gravité, CLÉ i18n du message). Le prédicat renvoie True quand C'EST CASSÉ.

    Le message n'est plus écrit ici mais dans `i18n/{fr,en}.json` sous `alert.prep.controle.<clé>` :
    une SEULE source pour les deux usages, le verdict affiché (onglet Nœuds, rendu en français par
    `_msg_controle`) et l'alerte (rendue dans la langue du LECTEUR). Dupliquer le texte entre la
    table et le catalogue les aurait laissés diverger en silence.

    Table plutôt que des `if` en série pour qu'ajouter un contrôle (isolation des cœurs, etc.)
    soit UNE ligne, et pour que la liste soit lisible d'un coup d'œil."""
    return [
        ("iommu",     lambda p: not p.get("iommu_active"), "error",
         "alert.prep.controle.iommu"),
        ("vfio",      lambda p: not p.get("vfio_present"), "error",
         "alert.prep.controle.vfio"),
        ("hugepages", lambda p: not p.get("hugepages_size_ok") or not p.get("hugepages_total"),
         "error", "alert.prep.controle.hugepages"),
        # Fréquence des cœurs isolés : LE piège du socle DPDK. `nohz_full` prive intel_pstate du
        # retour d'utilisation sur les cœurs tickless → ils restent au plancher à 100 % de
        # busy-poll et le moteur s'étouffe QUELQUES HEURES plus tard. Symptôme différé, cause au
        # boot : exactement ce qu'il faut attraper ici.
        ("cpufreq",   lambda p: bool((p.get("cpufreq") or {}).get("risk")), "error",
         "alert.prep.controle.cpufreq"),
        ("reboot",    lambda p: bool(p.get("reboot_needed")), "warning",
         "alert.prep.controle.reboot"),
        # Isolation des cœurs : `warning` et NON `error`, délibérément. Un nœud sans bande isolée
        # FONCTIONNE — il perd de la marge (IRQ/softirq/RCU préemptent les busy-poll) mais ne casse
        # pas. Mettre `error` ferait hurler tout le parc à chaque reboot tant que la migration n'est
        # pas faite, et une alerte qu'on apprend à ignorer ne vaut plus rien. Le `hint` de la sonde
        # porte le détail (bande attendue vs active).
        ("isolation", lambda p: bool((p.get("isolation") or {}).get("risk")), "warning",
         "alert.prep.controle.isolation"),
    ]


# État d'alerte de la prép, PARTAGÉ entre la passe post-boot et la re-vérification périodique
# (`node_health._check_prep_drift`). Deux détecteurs, UN état : sans ça, un défaut signalé au boot
# puis réparé ne produisait AUCUN message de résolution (le détecteur périodique ne savait pas
# qu'une alerte était en cours) — l'alerte restait orpheline dans le dashboard.
# La clé d'état est (niveau, ENSEMBLE des contrôles cassés) et pas le seul niveau : sinon un
# défaut qui en remplace un autre au même niveau passait inaperçu.
_prep_alert_state = {}       # node_id → [niveau, [clés]] | None (cache RAM du chemin chaud)
# Le MÊME état, SURVIVANT au redémarrage (cf. app/episodes.py) : une prép hôte cassée le reste
# quand l'orchestrateur redémarre.
from .episodes import EtatEpisodes as _Episodes
_episodes = _Episodes("node_prep")

# Dernier VERDICT complet de la prép, par nœud — publié tel quel dans le snapshot node_health
# (onglet Nœuds du Monitoring). Distinct de `_prep_alert_state` (qui ne sert qu'à ne pas répéter
# une alerte) : ici on garde de quoi AFFICHER, dont l'horodatage de la sonde. Sans cet horodatage
# l'UI mettrait un verdict vieux de 30 min à côté de chiffres CPU vieux de 5 s, sans le dire —
# c'est-à-dire un mensonge sur la fraîcheur.
_prep_verdict = {}           # node_id (str) → dict verdict | absent si jamais sondé


def verdict_prep(node_id):
    """Dernier verdict de prép hôte connu pour ce nœud, ou None s'il n'a JAMAIS été sondé.
    None ≠ « conforme » : l'appelant doit rendre « pas encore vérifié »."""
    return _prep_verdict.get(str(node_id))


# Suffixe de clé i18n pour chaque valeur de `contexte` (fragment FRANÇAIS, cf. docstring
# d'`evaluer_prep`) : le texte de l'alerte doit être traduisible, mais `contexte` lui-même reste
# recopié TEL QUEL dans le verdict affichable (`_verdict_prep["context"]`, lu par l'UI) — on ne
# le modifie donc jamais, on se contente de choisir la bonne clé complète à partir de sa valeur.
# `.get(contexte, "autre")` couvre un futur appelant qui passerait un fragment non prévu ici.
_CONTEXTE_SUFFIXES = {
    "après reboot": "reboot",
    "après réparation": "reparation",
    "dérive détectée": "derive",
    "revérification manuelle": "manuel",
}


def _contexte_suffixe(contexte):
    return _CONTEXTE_SUFFIXES.get(contexte, "autre")


def evaluer_prep(node_id, name, prep, contexte):
    """Évalue la table de contrôles sur `prep`, alerte À TRANSITION (dégradation, changement de
    nature du défaut, ET retour à la normale), met à jour l'état partagé. Renvoie la liste des
    contrôles cassés. `contexte` = fragment de message (« après reboot », « dérive détectée »)."""
    casses = [(cle, niv, _msg_controle(cle, cle_i18n, prep), cle_i18n)
              for cle, casse, niv, cle_i18n in _prep_checks() if casse(prep)]
    niveau = ("error" if any(n == "error" for _, n, _, _ in casses)
              else ("warning" if casses else None))
    # Forme LISTE (et non tuple/frozenset) : cet état est persisté en JSON (cf. plus bas), et un
    # tuple relu revient en liste — comparer les deux formes rendrait TOUJOURS « changé », donc une
    # fausse alerte à chaque redémarrage. On normalise donc des deux côtés.
    etat = [niveau, sorted(c for c, _, _, _ in casses)] if niveau else None
    if node_id not in _prep_alert_state:
        _prep_alert_state[node_id] = _episodes.get(node_id)   # reprise après (re)démarrage
    prev = _prep_alert_state.get(node_id)
    if etat and etat != prev:
        # `casses` a une longueur VARIABLE (0 à N contrôles simultanément cassés). On envoie la
        # LISTE DE SOUS-CLÉS, pas les clés techniques : réduire l'alerte à « iommu · vfio » ferait
        # perdre à l'exploitant l'explication de CHAQUE contrôle — « IOMMU inactif — le moteur
        # crash-loopera au bind vfio », etc. — c'est-à-dire tout ce qui permet d'agir sans ouvrir
        # l'interface. Le rendu se fait à la lecture, dans la langue du lecteur
        # (`i18n._developper_sous_cles`).
        db_add_alert(f"alert.prep.controles_casses_{_contexte_suffixe(contexte)}", niveau,
                     node_id=node_id, kind="prep",
                     params={"n": name, "_sep": " · ",
                             "controles": [[ci, _params_controle(cle, prep)]
                                           for cle, _, _, ci in casses]})
    elif etat is None and prev is not None:
        db_add_alert(f"alert.prep.controles_retablis_{_contexte_suffixe(contexte)}", "info",
                     node_id=node_id, kind="prep",
                     params={"n": name, "controles": ", ".join(sorted(prev[1]))})
    _prep_alert_state[node_id] = etat
    if etat:
        _episodes.poser(node_id, etat)
    else:
        _episodes.retirer(node_id)
    _prep_verdict[str(node_id)] = _verdict_prep(prep, casses, niveau, contexte)
    return casses


def _verdict_prep(prep, casses, niveau, contexte):
    """Construit le verdict AFFICHABLE (snapshot node_health → onglet Nœuds du Monitoring) à
    partir de la sonde brute et des contrôles cassés. Rien n'est recalculé ici : on ne fait que
    reprendre ce que `mtl.verifier` a mesuré (bande isolée attendue/active, fréquences réelles
    du CPU présent) — aucune valeur de site ni seuil en dur ne doit apparaître dans l'UI."""
    iso = prep.get("isolation") or {}
    cf = prep.get("cpufreq") or {}
    return {
        "level": niveau or "ok",
        "checks": [{"key": c, "level": n, "message": m} for c, n, m, _ in casses],
        "summary": (" · ".join(m for _, _, m, _ in casses) if casses
                    else "prép hôte MTL conforme (IOMMU, vfio, hugepages, isolation, fréquence)."),
        "isolation": {
            "expected":   iso.get("expected") or "",
            "active":     iso.get("active") or "",
            "match":      bool(iso.get("match")),
            "ht_aware":   bool(iso.get("ht_aware")),
            "irqbalance": bool(iso.get("irqbalance")),
            "unit":       bool(iso.get("unit")),
            "unit_state": iso.get("unit_state"),
            "hint":       iso.get("hint"),
        },
        "cpufreq": {
            "isolated":    cf.get("isolated") or 0,
            "pinned":      bool(cf.get("pinned")),
            "governor":    cf.get("governor"),
            "min_mhz":     cf.get("min_mhz"),
            "cur_min_mhz": cf.get("cur_min_mhz"),
            "max_mhz":     cf.get("max_mhz"),
        },
        "reboot_needed": bool(prep.get("reboot_needed")),
        "ts": time.time(),           # HORODATAGE DE LA SONDE (≠ ts du snapshot, 5 s) — l'UI en a
        "context": contexte,         # besoin pour dire « vérifié il y a N min ».
    }


def _params_controle(cle, prep):
    """Paramètres du message d'un contrôle. Seule `isolation` en a un : le `hint` de la sonde
    porte la bande attendue vs active, propre au nœud — il n'a donc rien à faire dans un gabarit
    figé. C'est une DONNÉE, pas de la prose : légitime en paramètre."""
    if cle == "isolation":
        return {"hint": (prep.get("isolation") or {}).get("hint") or "voir Réglages → nœud."}
    return {}


def _msg_controle(cle, cle_i18n, prep):
    """Message d'un contrôle rendu en FRANÇAIS, pour le verdict AFFICHABLE (onglet Nœuds).

    Le français est ici la langue du verdict publié, comme avant ce chantier — c'est l'alerte,
    elle, qui suit la langue du lecteur. Les deux sortent de la même clé, donc ils ne peuvent pas
    diverger."""
    from .i18n import t as _t, DEFAULT_LANG
    return _t(cle_i18n, DEFAULT_LANG, **_params_controle(cle, prep))


def _reparer_cpufreq(node_id, name, node, prep):
    """Re-pose l'épinglage de fréquence des cœurs isolés et RE-SONDE. Renvoie le verdict de prép
    à jour (celui d'entrée si la réparation n'a pas pu être tentée).

    La fonction appelée installe le script + l'unité systemd PUIS applique à chaud : le nœud est
    réparé tout de suite, et le prochain reboot rejoue l'unité tout seul. Best-effort, ne lève
    jamais — une réparation ratée doit laisser l'alerte d'origine, pas masquer l'état."""
    from . import mtl
    host = (node or {}).get("host")
    if not host:
        return prep
    try:
        ok, msg, _ = mtl.ensure_cpufreq_performance(host)
    except Exception as e:
        log.warning("réparation cpufreq nœud %s : %s", node_id, e)
        return prep
    try:
        neuf = mtl.verifier_node(node) or {}
    except Exception:
        neuf = {}
    if neuf.get("error") or not neuf:
        return prep                      # re-sonde impossible : on garde le verdict d'origine
    # VÉRIFICATION EXPLICITE : un `ok` qui laisserait les cœurs au plancher serait exactement
    # l'échec silencieux qu'on corrige — on croit le message, pas la mesure.
    if not bool((neuf.get("cpufreq") or {}).get("risk")):
        cf = neuf.get("cpufreq") or {}
        db_add_alert("alert.prep.cpufreq_repare", "info", node_id=node_id, kind="prep",
                     params={"n": name, "isolated": cf.get('isolated'),
                             "min_mhz": cf.get('min_mhz')})
    else:
        db_add_alert("alert.prep.cpufreq_repare_echec", "error", node_id=node_id, kind="prep",
                     params={"n": name, "msg": msg})
    return neuf


def _verifier_prep_post_boot(node_id, name):
    """Sonde la prép hôte MTL après un reboot et alerte sur ce qui est cassé. Lecture seule,
    best-effort, jamais bloquant. Une alerte par contrôle en échec (messages actionnables) ;
    une ligne de log en cas de prép saine, pour que le passage laisse une trace vérifiable."""
    try:
        node = db_get_node(node_id) or {}
        from . import node_driver, mtl
        try:
            caps = node_driver.node_capabilities(node) or []
        except Exception:
            caps = []
        if "io2110" not in caps:
            return                          # pas de moteur 2110 ici → pas de prép à vérifier
        # Laisser systemd finir (unités de prép : fréquence, IRQ, rdma) avant de constater.
        time.sleep(max(0, _cfg_node("auto_recovery_grace_s", node_id, 45)))
        prep = mtl.verifier_node(node) or {}
        if prep.get("error"):
            # NE PAS avaler : une sonde muette qui laisse croire que tout va bien est pire que
            # pas de sonde du tout (cf. le canari qui meurt et fait passer le test au vert).
            db_add_alert("alert.prep.non_sondable_reboot", "error", node_id=node_id, kind="prep",
                         params={"n": name, "e": prep['error']})
            return
        casses = evaluer_prep(node_id, name, prep, "après reboot")
        # ── RÉPARATION AUTOMATIQUE de l'épinglage de fréquence ────────────────────────────────
        # `cpufreq` est le SEUL contrôle de la table qui soit réparable À CHAUD : IOMMU, vfio,
        # hugepages et isolation passent tous par le cmdline noyau, donc par un reboot — qui n'est
        # jamais automatique ici. L'épinglage, lui, est un `systemctl` idempotent qui pose en plus
        # l'unité de persistance : le réparer une fois suffit à ce que le problème ne revienne pas.
        # Sans ça, la sonde répétait à chaque boot un message parfaitement juste que personne
        # n'allait exécuter à la main sur chaque nœud (constaté sur Horace : les schedulers DPDK
        # tournaient à 1000 MHz au lieu de 3700, soit 3,7× de marge perdue, l'unité n'ayant jamais
        # été posée sur ce nœud). Même doctrine que la re-provision des certs : gated par
        # `auto_recovery_enabled`, UNE tentative, verdict re-sondé derrière.
        if any(k == "cpufreq" for k, _, _, _ in casses) and _cfg_node("auto_recovery_enabled", node_id, 0):
            prep = _reparer_cpufreq(node_id, name, node, prep)
            casses = evaluer_prep(node_id, name, prep, "après réparation")
        if not casses:
            cf = prep.get("cpufreq") or {}
            detail = (f", {cf['isolated']} cœur(s) isolé(s) épinglé(s) à {cf.get('min_mhz')} MHz"
                      if cf.get("isolated") and cf.get("pinned") else "")
            log.info("post-boot nœud %s : prép hôte MTL saine (IOMMU, vfio, hugepages%s)",
                     name, detail)
    except Exception as e:
        log.warning("vérif prép post-boot nœud %s : %s", node_id, e)


# ─── Certificats mTLS des conteneurs : re-provision après un reboot ───────────────────
# LA PANNE (prod, 2026-07) : l'agent-nœud matérialise les PEM d'un conteneur dans
# /run/bobi-tls/<nom>/ — et /run est un TMPFS. Au reboot : (1) /run est vidé ; (2) Docker relève
# les conteneurs `--restart unless-stopped` AVANT toute reprovision ; (3) la source du bind-mount
# n'existant plus, Docker la RECRÉE VIDE ; (4) l'agent du conteneur, qui lit /etc/bobi-tls au
# démarrage, ne trouve rien et sert en HTTP CLAIR ; (5) le contrôleur, lui, a tranché HTTPS
# globalement → le conteneur est injoignable DÉFINITIVEMENT tout en continuant de tourner.
# Empreinte : les dossiers d'AVANT le reboot contiennent 0 fichier, ceux d'APRÈS en contiennent 3.
# Le moteur 2110 y échappe (il tourne en --rm : détruit puis RECRÉÉ, donc re-provisionné) ; les
# conteneurs compute, simplement relevés, gardent leur montage vide — d'où « seuls les multiviews
# sont tombés ».
#
# DÉCISION : on ne persiste PAS les clés sur disque (elles restent en RAM — mesure d'hygiène :
# la clé d'un conteneur, cf. volet identité côté agent, est un secret réutilisable). On
# RE-PROVISIONNE donc, ce qui passe par le SEUL chemin qui matérialise le trio PEM côté nœud :
# la spec de `docker run` (node_driver.run_container). D'où : effacer la signature de spec →
# redéployer → l'agent-nœud réécrit /run/bobi-tls/<nom>/ et rebinde → l'agent conteneur relit ses
# certs au démarrage. Le redéploiement re-pousse aussi le script (rootfs éphémère recréé).
#
# GARDE-FOUS (ce projet a déjà connu une boucle de recréation, cf. en-tête docker_compute) :
#   · appelé UNIQUEMENT depuis la passe one-shot par boot (verrou par nœud, recovered_boot_ts
#     écrit AVANT d'agir) ou depuis la passe de CONSTAT (reparer=False, aucune écriture) ;
#   · on ne touche QU'aux conteneurs dont le verdict est « clair » — c'est-à-dire ceux dont on a
#     la PREUVE que l'agent est vivant mais dans le mauvais schéma ; un conteneur sain ou
#     réellement mort n'est jamais recréé ici ;
#   · UNE tentative de réparation par conteneur et par boot, puis re-diagnostic et alerte si ça
#     n'a pas suffi (jamais de nouvelle boucle).

def _certs_conteneurs_node(node_id, name, reparer):
    """Diagnostique (et répare si `reparer`) le désaccord de schéma mTLS des conteneurs compute
    d'un nœud qui vient de rebooter. Renvoie (casses, repares, echecs) — listes de vmid.
    Best-effort : ne lève jamais, ne bascule jamais le contrôleur en HTTP clair."""
    casses, repares, echecs = [], [], []
    try:
        from . import deploy, node_driver
        from .addressing import get_container_ip
        if not deploy.agent_tls_on():
            return casses, repares, echecs      # pas de CA → toute la flotte en http, rien à faire
        node = db_get_node(node_id) or {}
        if not node_driver.has_agent(node):
            # Chemin legacy (ssh_run) : il n'injecte AUCUN cert conteneur → hors sujet.
            return casses, repares, echecs
        if not reparer:
            # Passe de constat : laisser Docker relever les `unless-stopped` avant de conclure.
            time.sleep(max(0, _cfg_node("auto_recovery_grace_s", node_id, 45)))
        _, compute = _desired_running(node_id)
        for c in compute:
            vmid = c["vmid"]
            ip = c.get("docker_ip") or get_container_ip(vmid)
            if not ip:
                continue
            if deploy.diagnostiquer_schema_agent(ip, vmid=vmid) != "clair":
                continue                        # sain, ou muet des deux côtés (→ voie de relance)
            casses.append(vmid)
            if not reparer:
                continue
            (repares if _reprovisionner_certs(c) else echecs).append(vmid)
            time.sleep(1)                       # ne pas saturer l'agent-nœud / le démon Docker
        if casses and reparer:
            # vmid = identifiant technique (pas une phrase) → paramètre unique, comme les hostnames
            # ou vmid ailleurs dans ce fichier.
            if echecs:
                db_add_alert("alert.agent.certs_reprovisionnes_echecs", "error",
                             node_id=node_id, kind="agent",
                             params={"n": name, "n_casses": len(casses), "n_repares": len(repares),
                                     "n_echecs": len(echecs),
                                     "vmids": ", ".join(map(str, echecs))})
            else:
                db_add_alert("alert.agent.certs_reprovisionnes", "info", node_id=node_id,
                             kind="agent",
                             params={"n": name, "n_casses": len(casses), "n_repares": len(repares)})
        elif casses:
            db_add_alert("alert.agent.certs_http_clair", "error", node_id=node_id, kind="agent",
                         params={"n": name, "n_casses": len(casses),
                                 "vmids": ", ".join(map(str, casses))})
    except Exception as e:
        log.error("certs conteneurs nœud %s : %s", node_id, e)
        db_add_alert("alert.agent.certs_verif_impossible", "error", node_id=node_id, kind="agent",
                     params={"n": name, "e": str(e)})
    return casses, repares, echecs


def _reprovisionner_certs(c):
    """Re-provisionne le cert d'UN conteneur : on efface sa signature de spec (sans quoi
    `_conteneur_deja_conforme` le juge conforme et ne le recrée pas — le montage TLS resterait
    vide) puis on redéploie. Le redéploiement recrée le conteneur avec un trio PEM frais ET
    re-pousse le script. UNE tentative ; vérdict re-sondé derrière."""
    vmid = c["vmid"]
    try:
        from . import deploy
        from .database import db_update_spec_sig
        from .addressing import get_container_ip
        dc = json.loads(c.get("deploy_config") or "{}") or {}
        type_ = dc.get("type")
        if not type_:
            return False
        db_update_spec_sig(vmid, None)          # force la recréation (donc la matérialisation TLS)
        ok = bool(deploy.deployer_script(vmid, type_, dc.get("params") or {}))
        if not ok:
            return False
        ip = get_container_ip(vmid) or c.get("docker_ip")
        # Vérification EXPLICITE : un redéploiement « réussi » qui laisserait l'agent en clair
        # serait exactement l'échec silencieux qu'on corrige.
        return deploy.diagnostiquer_schema_agent(ip, vmid=vmid, alerter=False) == "ok"
    except Exception as e:
        log.warning("re-provision certs vmid=%s : %s", vmid, e)
        return False


def _recover_node(node_id, boot_ts):
    """Passe de recovery pour UN boot d'UN nœud. One-shot : recovered_boot_ts est posé SOUS
    verrou AVANT d'agir — un second déclenchement (sampler 5 s) ne fait rien."""
    lk = _lock_for(node_id)
    if not lk.acquire(blocking=False):
        return                              # recovery déjà en cours sur ce nœud
    try:
        node = db_get_node(node_id) or {}
        if float(node.get("recovered_boot_ts") or 0) >= float(boot_ts) - 1:
            return                          # déjà traité (contrôleur redémarré entre-temps, etc.)
        db_set_node_boot(node_id, recovered_boot_ts=boot_ts)   # AVANT d'agir (one-shot)
        name = node.get("name") or node.get("host") or f"nœud {node_id}"
        grace = _cfg_node("auto_recovery_grace_s", node_id, 45)
        attempts = _cfg_node("auto_recovery_max_attempts", node_id, 2)
        backoff = _cfg_node("auto_recovery_backoff_s", node_id, 20)
        time.sleep(max(0, grace))           # laisser Docker (unless-stopped) et systemd (PTP) agir

        node = db_get_node(node_id) or node   # re-lire (le nœud a pu être édité pendant la grace)
        _check_ptp(node)

        from . import docker_driver, docker_compute
        mtl, compute = _desired_running(node_id)
        relanced, failed = [], []

        # 1) Moteur MTL d'abord (producteur racine des shm ; --rm → absent après reboot).
        for c in mtl:
            stc = docker_driver.status_docker(c["vmid"])
            if stc == "running":
                continue
            ok = _try_start(c, docker_driver.start_docker, attempts, backoff)
            (relanced if ok else failed).append(c["vmid"])
            if ok:
                # Attendre que le moteur soit réellement up avant les consommateurs (poll borné).
                for _ in range(15):
                    if docker_driver.status_docker(c["vmid"]) == "running":
                        break
                    time.sleep(2)

        # 2) Compute/media/webrtc ensuite. Docker (unless-stopped) a normalement déjà relevé —
        #    on ne rattrape que exited/absent. start_compute gère les deux (start ou redéploiement).
        for c in compute:
            stc = docker_compute.status_compute(c["vmid"])
            if stc == "running":
                continue
            ok = _try_start(c, docker_compute.start_compute, attempts, backoff)
            (relanced if ok else failed).append(c["vmid"])
            time.sleep(2)                   # ne pas saturer l'agent/le démon Docker

        # 2bis) Certificats mTLS : les conteneurs relevés par Docker au boot ont un /etc/bobi-tls
        #       VIDE (/run est un tmpfs) → leur agent sert en clair et le contrôleur, qui parle
        #       mTLS, ne les joindra plus JAMAIS. On re-provisionne ceux dont la sonde prouve le
        #       désaccord de schéma. Placé APRÈS la relance (il faut que les conteneurs tournent
        #       pour les sonder) et AVANT le reconcile du tissu (qui parle à leurs agents).
        _tls_casses, _tls_repares, _tls_echecs = _certs_conteneurs_node(node_id, name, True)

        # 3) Post-recovery : reconcile du tissu de composition (no-op si fabric_auto off).
        try:
            from .deploy import reconcile_fabric_node
            reconcile_fabric_node(node_id)
        except Exception as e:
            log.warning("recovery fabric nœud %s : %s", node_id, e)

        total = len(mtl) + len(compute)
        # Les certs re-provisionnés comptent comme une reprise : sans ça le bilan pouvait dire
        # « rien à relever, tout est up » à propos de conteneurs qu'on venait de recréer.
        relanced = list(relanced) + [v for v in _tls_repares if v not in relanced]
        failed = list(failed) + [v for v in _tls_echecs if v not in failed]
        if relanced or failed:
            if failed:
                db_add_alert("alert.node.recovery_releve_echecs", "error", node_id=node_id,
                             kind="node",
                             params={"n": name, "n_relances": len(relanced), "n_failed": len(failed),
                                     "vmids": ", ".join(map(str, failed)), "total": total})
            else:
                db_add_alert("alert.node.recovery_releve", "info", node_id=node_id, kind="node",
                             params={"n": name, "n_relances": len(relanced), "total": total})
        else:
            db_add_alert("alert.node.recovery_rien_a_relever", "info", node_id=node_id,
                         kind="node", params={"n": name, "total": total})
    except Exception as e:
        log.error("recovery nœud %s : %s", node_id, e)
        db_add_alert("alert.node.recovery_erreur", "error", node_id=node_id, kind="node",
                     params={"node_id": node_id, "e": str(e)})
    finally:
        lk.release()
