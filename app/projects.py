# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import json
import os
import re
import threading
import time
import logging
import requests
from .containers import detruire_container, MEDIA_HOST_DIR
from .addressing import get_container_ip
from .deploy import deployer_script
from .database import (db_get_project, db_add_alert, db_get_containers,
                       plugin_store_create, db_set_instance_uuid,
                       db_set_project_state, db_set_project_media_path)
from .allocations import _used_vmids, next_free_vmid
from . import settings

log = logging.getLogger(__name__)


# Types exclus des projets (infra persistante/partagée, jamais clonée par projet).
# storage = filebrowser scopé au projet, recréé manuellement, fichiers sur le host.
# 2110_io (décision 2026-07-05) : le moteur ST 2110 est LIÉ AU NŒUD (un par nœud),
# jamais à un projet — ni créable dans un projet, ni snapshoté dedans. Les projets
# le consomment via les PORTS ; sa config se sauvegarde par les config-snapshots
# par-container (POST /api/containers/<vmid>/config_snapshots).
PROJECT_EXCLUDED_TYPES = {"webrtc_gateway", "storage", "2110_io"}

# ─── Cycle de vie (chantier 3) : verrou par projet ────────────
# Le modèle threading n'a pas de lock par-VMID ; on verrouille au niveau PROJET :
# un seul chargement/déchargement à la fois par projet (anti double-restore).
_project_locks = {}
_locks_guard = threading.Lock()

def _project_lock(pid):
    with _locks_guard:
        if pid not in _project_locks:
            _project_locks[pid] = threading.Lock()
        return _project_locks[pid]


# ─── « Projet vivant » : re-snapshot auto débouncé (chantier 3) ─────
# Toute modif de deploy_config d'un container appartenant à un projet re-snapshote le
# projet après REsnap_DEBOUNCE_S sans nouvelle modif : décharger ne perd jamais rien.
# L'ancien snapshot est poussé dans project_versions (version auto, rétention bornée).

RESNAP_DEBOUNCE_S = 10
_resnap_timers = {}          # pid → threading.Timer
_resnap_guard = threading.Lock()

def _project_ids_for_vmid(vmid):
    """Projets portant ce container : rattachement direct + snapshots (cache court)."""
    try:
        from .auth import vmid_project_ids
        return vmid_project_ids(vmid)
    except Exception:
        return set()

def notify_container_changed(vmid):
    """Appelé par db_update_deploy_config : programme un re-snapshot débouncé des
    projets qui portent ce container. Ne bloque jamais l'appelant."""
    for pid in _project_ids_for_vmid(vmid):
        with _resnap_guard:
            t = _resnap_timers.get(pid)
            if t:
                t.cancel()
            t = threading.Timer(RESNAP_DEBOUNCE_S, _resnapshot_safe, args=(pid,))
            t.daemon = True
            _resnap_timers[pid] = t
            t.start()

def _resnapshot_safe(pid):
    with _resnap_guard:
        _resnap_timers.pop(pid, None)
    try:
        resnapshot_projet(pid)
    except Exception as e:
        log.warning("re-snapshot auto projet #%s: %s", pid, e)

def resnapshot_projet(pid, label=None, force=False):
    """Re-snapshote le projet depuis ses containers LIVE (project_id + vmids du snapshot
    encore vivants). L'ancien snapshot part dans project_versions. No-op si identique
    (sauf force) ou si un chargement/déchargement est en cours."""
    from .database import (db_update_project_snapshot, db_add_project_version,
                           db_snapshot_for_vmids)
    p = db_get_project(pid)
    if not p:
        return None
    if (p.get("state") or "saved") in ("loading", "unloading"):
        return None   # la prochaine modif re-déclenchera
    live = db_get_containers()
    def _typ(c):
        dc = c.get("deploy_config")
        try:
            dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
        except Exception:
            dc = {}
        return dc.get("type")
    live_vmids = {c["vmid"] for c in live if _typ(c) not in PROJECT_EXCLUDED_TYPES}
    vmids = {c["vmid"] for c in live if c.get("project_id") == pid
             and c["vmid"] in live_vmids}
    vmids |= {c.get("vmid") for c in (p.get("snapshot") or [])
              if c.get("vmid") in live_vmids}
    if not vmids:
        return None   # projet déchargé : on garde le dernier snapshot tel quel
    snapshot = db_snapshot_for_vmids(sorted(vmids))
    old = p.get("snapshot") or []
    if not force and json.dumps(snapshot, sort_keys=True) == json.dumps(old, sort_keys=True):
        if label:   # rien n'a changé mais on veut un point nommé
            db_add_project_version(pid, old, label=label)
        return None
    db_add_project_version(pid, old, label=label)
    db_update_project_snapshot(pid, snapshot)
    log.info("projet #%s re-snapshoté (%d containers%s)", pid, len(snapshot),
             f", version « {label} »" if label else "")
    return snapshot


def _slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "projet"

def _ensure_media_dir(p):
    """Garantit le dossier média du projet (créé à la sauvegarde, mais absent après un
    import/clone — les containers retomberaient sinon sur la racine partagée)."""
    if p.get("media_path"):
        return p["media_path"]
    path = os.path.join(MEDIA_HOST_DIR, _slugify(p.get("name")))
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        log.warning("media dir projet #%s (%s): %s", p.get("id"), path, e)
    db_set_project_media_path(p["id"], path)
    p["media_path"] = path
    return path


# Champs multicast des tx_slots (2110_io) : JAMAIS rejoués d'un snapshot — purgés au
# chargement pour forcer la réallocation via mcast_allocations dans le hook
# before_deploy (sinon deux chargements du même export émettent sur les mêmes groupes,
# conflit silencieux). Les sessions RX (adresses de sources EXTERNES) ne sont pas touchées.
_TX_MCAST_FIELDS = ("multicast_ip", "dest_port", "anc_multicast_ip", "anc_dest_port",
                    "multicast_ip_leg1", "dest_port_leg1",
                    "anc_multicast_ip_leg1", "anc_dest_port_leg1")
_TX_MCAST_AUDIO_FIELDS = ("multicast_ip", "dest_port", "multicast_ip_leg1", "dest_port_leg1")

def _purge_tx_multicast(params):
    """Retire les adresses/ports multicast TX des params (le hook before_deploy
    réallouera au déploiement). Renvoie le nombre d'adresses purgées (= allocations
    qui seront re-demandées, utilisé par le pré-vol de capacité)."""
    n = 0
    for t in (params.get("tx_slots") or []):
        if not isinstance(t, dict):
            continue
        for f in _TX_MCAST_FIELDS:
            v = t.pop(f, None)
            if v is not None and f.startswith(("multicast", "anc_multicast")):
                n += 1
        for a in (t.get("audios") or []):
            if not isinstance(a, dict):
                continue
            for f in _TX_MCAST_AUDIO_FIELDS:
                v = a.pop(f, None)
                if v is not None and f.startswith("multicast"):
                    n += 1
    return n


def _shm_produced(dc):
    """SHM names qu'un container *produit* (collisions = corruption /dev/shm).
    Le streamer est purement consommateur."""
    if not dc:
        return set()
    t = dc.get("type")
    p = dc.get("params") or {}
    hn = p.get("hostname") or ""
    out = set()
    from . import plugins as _pl
    if _pl.is_plugin(t):
        for prod in _pl.derive_wiring(t, hn, p)["produces"]:
            if prod.get("shm"):
                out.add(prod["shm"])
    return {s for s in out if s}


def _sanitize_host_frag(s):
    """Fragment de hostname valide : ASCII, [A-Za-z0-9-], sans espace. Délègue à la source
    unique (`app/hostnames.py`) — c'était une copie littérale de `monitor._sanitize_host`."""
    from .hostnames import normaliser
    return normaliser(s)


def _prefix_snapshot(snapshot, prefix):
    """Préfixe le hostname de chaque container par `prefix` et réécrit les références
    shm INTERNES au projet (les sources externes restent inchangées), pour que le
    câblage du projet reste fonctionnel après restauration."""
    prefix = _sanitize_host_frag(prefix)
    if not prefix:
        return snapshot
    produced = set()
    for c in snapshot:
        produced |= _shm_produced(c.get("deploy_config") or {})

    def pref(name):                       # nom de shm « nu »
        return f"{prefix}-{name}" if name and name in produced else name

    def pref_path(path):                  # « /dev/shm/X » ou « X »
        if not path:
            return path
        full = path.startswith("/dev/shm/")
        bare = path[len("/dev/shm/"):] if full else path
        if bare in produced:
            return f"/dev/shm/{prefix}-{bare}" if full else f"{prefix}-{bare}"
        return path

    out = []
    for c in snapshot:
        c = dict(c)
        c["hostname"] = f"{prefix}-{c.get('hostname') or ''}"[:63]
        dc = c.get("deploy_config")
        if dc:
            dc = dict(dc); params = dict(dc.get("params") or {})
            params["hostname"] = c["hostname"]
            t = dc.get("type")
            # Sorties explicites (les sorties dérivées du hostname suivent le nouveau nom)
            for k in ("shm_out", "shm_out_pgm", "shm_out_clean", "shm_out_pvw"):
                if params.get(k):
                    params[k] = pref(params[k])
            # Entrées (références internes → préfixées ; externes → inchangées)
            if params.get("input_shm"):
                params["input_shm"] = pref_path(params["input_shm"])
            if params.get("bg_input"):
                params["bg_input"] = pref_path(params["bg_input"])
            if params.get("shm_name"):
                params["shm_name"] = pref_path(params["shm_name"])
            if params.get("audio_shm"):
                params["audio_shm"] = pref_path(params["audio_shm"])
            if isinstance(params.get("flux_config"), list):
                fc = []
                for f in params["flux_config"]:
                    f = dict(f)
                    if f.get("path"):     f["path"]     = pref_path(f["path"])
                    if f.get("shm_name"): f["shm_name"] = pref_path(f["shm_name"])
                    fc.append(f)
                params["flux_config"] = fc
            if isinstance(params.get("video"), dict) and params["video"].get("shm_name"):
                v = dict(params["video"]); v["shm_name"] = pref_path(v["shm_name"]); params["video"] = v
            if isinstance(params.get("audios"), list):
                params["audios"] = [
                    {**a, "shm_name": pref_path(a.get("shm_name"))} if isinstance(a, dict) else a
                    for a in params["audios"]]
            dc["params"] = params
            c["deploy_config"] = dc
        out.append(c)
    return out


def _existing_hostnames_and_shms():
    """Hostnames et SHM produits par les containers actuellement en DB."""
    hosts = set()
    shms  = set()
    for c in db_get_containers():
        if c.get("hostname"):
            hosts.add(c["hostname"])
        try:
            dc = json.loads(c["deploy_config"]) if c.get("deploy_config") else None
        except Exception:
            dc = None
        shms |= _shm_produced(dc)
    return hosts, shms


def verifier_capacite(snapshot):
    """Pré-vol de CAPACITÉ (chantier 3, refus bloquant) : vérifie AVANT toute création
    que le cluster peut accueillir le snapshot à côté des projets actifs. Renvoie une
    liste d'issues [{kind, detail, need?, free?}] — liste vide = ça rentre.
    Conçu selon `demande + occupé ≤ capacité` pour accepter plus tard des réservations."""
    issues = []
    from .database import db_get_nodes
    try:
        nodes = db_get_nodes()
    except Exception:
        nodes = []

    # 1) Cœurs CPU (agrégé cluster : le nœud cible est choisi au vol par pick_compute_node).
    #    Neutralisé si AUCUN nœud n'a de pool de pinning (compute_cpuset) : dans ce mode,
    #    allocate_cores retombe sur le quota --cpus sans pinning, il n'y a rien à épuiser.
    try:
        from .core_pool import cores_status
        stats = [cores_status(n["id"]) or {} for n in nodes]
        pool_total = sum(s.get("total", 0) for s in stats)
        if pool_total > 0:
            # ★ La demande vient du coût MESURÉ du type quand on en a un (p95 sur 4 h glissantes,
            # cf. app/cpu_profiles.py), et seulement à défaut du `cores` déclaré. Comparer du
            # déclaré à du déclaré ne valide rien : les deux côtés de l'inégalité étaient des
            # chiffres que personne n'avait jamais vérifiés. `mesures` dit, par type, d'où vient
            # le nombre retenu — un refus de déploiement doit pouvoir s'expliquer.
            from . import cpu_profiles
            need = 0.0
            mesures = {}
            for c in snapshot:
                t = (c.get("deploy_config") or {}).get("type")
                if t in ("2110_io",):
                    continue
                # Ordre de préséance, et il compte : la MESURE d'abord (seul chiffre vérifié) ;
                # à défaut le `cores` du snapshot, qui est l'intention de CETTE instance (un
                # override explicite doit être respecté) ; le manifeste seulement en dernier.
                # Ainsi, tant qu'aucune mesure n'existe, le pré-vol se comporte EXACTEMENT comme
                # avant — on ne déplace pas un seuil de refus par effet de bord.
                pct, source = cpu_profiles.cout_estime(t)
                if source != "mesure":
                    if c.get("cores"):
                        pct, source = float(c["cores"]) * 100.0, "projet"
                    elif pct is None:
                        continue
                need += pct / 100.0
                mesures.setdefault(t, {"source": source, "cores": round(pct / 100.0, 2)})
            need = int(need + 0.999) if need else 0        # arrondi SUPÉRIEUR : un pré-vol se trompe
            free = sum(s.get("free", 0) for s in stats)    # du côté prudent, jamais de l'autre
            if need > free:
                d = ", ".join(f"{t} {v['cores']} cœur(s) ({v['source']})"
                              for t, v in sorted(mesures.items()))
                issues.append({"kind": "cores", "need": need, "free": free, "mesures": mesures,
                               "detail": f"cœurs CPU : {need} demandés, {free} libres sur le "
                                         f"cluster — {d}"})
    except Exception as e:
        log.warning("pré-vol cœurs: %s", e)

    # 2) (retiré 2026-07-05) — le 2110_io est exclu des projets (PROJECT_EXCLUDED_TYPES,
    #    lié au nœud) : le restore le SAUTE, plus de conflit « un moteur par nœud » ici.

    # 3) Multicast : les adresses TX du snapshot seront RÉALLOUÉES au chargement —
    #    il faut assez d'adresses libres dans les plages.
    try:
        import ipaddress
        from .allocations import _used_multicasts
        from .database import db_get_mcast_ranges
        need = 0
        for c in snapshot:
            params = dict(((c.get("deploy_config") or {}).get("params")) or {})
            # copie profonde légère : on compte sans muter le snapshot
            params = json.loads(json.dumps(params))
            need += _purge_tx_multicast(params)
        if need:
            used = {u.split(":")[0] for u in _used_multicasts()}
            free = 0
            for r in db_get_mcast_ranges():
                size = int(r.get("size") or 0)
                if size <= 0:
                    continue
                try:
                    a = int(ipaddress.IPv4Address(r["base_ip"]))
                except Exception:
                    continue
                free += sum(1 for i in range(size)
                            if str(ipaddress.IPv4Address(a + i)) not in used)
            if need > free:
                issues.append({"kind": "mcast", "need": need, "free": free,
                               "detail": f"adresses multicast : {need} demandées, "
                                         f"{free} libres dans les plages"})
    except Exception as e:
        log.warning("pré-vol multicast: %s", e)

    return issues


def planifier_restore(snapshot):
    """Pré-vol d'un restore : calcule les remaps VMID et liste les conflits non
    résolubles automatiquement (hostname / SHM) + les manques de CAPACITÉ (chantier 3)."""
    used_vmids = set(_used_vmids())
    hosts_exist, shms_exist = _existing_hostnames_and_shms()

    floor = int(settings.get("vmid_start"))

    remaps = []        # [{from, to, hostname}]
    host_conflicts = []
    shm_conflicts  = []
    # On remappe à la volée pour éviter qu'un VMID alloué pendant le plan
    # ne soit re-choisi pour un autre container du même snapshot.
    reserved = set(used_vmids)

    for c in snapshot:
        old = c.get("vmid")
        new = old
        if old in reserved:
            # Allocation MONOTONE (handle local) : prochain au-dessus de tous les réservés. Jamais
            # d'échec (plus de plafond). Le vmid source est de toute façon sans valeur sur ce cluster.
            above = [v for v in reserved if isinstance(v, int) and v >= floor]
            new = (max(above) if above else floor - 1) + 1
            remaps.append({"from": old, "to": new, "hostname": c.get("hostname")})
        reserved.add(new)

        if c.get("hostname") and c["hostname"] in hosts_exist:
            host_conflicts.append(c["hostname"])

        for s in _shm_produced(c.get("deploy_config")):
            if s in shms_exist:
                shm_conflicts.append(s)

    capacity = verifier_capacite(snapshot)
    return {
        "remaps": remaps,
        "hostname_conflicts": sorted(set(host_conflicts)),
        "shm_conflicts": sorted(set(shm_conflicts)),
        "capacity_issues": capacity,
        "can_restore": not host_conflicts and not shm_conflicts and not capacity,
    }

def _attendre_ip(vmid, timeout=60):
    debut = time.time()
    while time.time() - debut < timeout:
        ip = get_container_ip(vmid)
        if ip:
            return ip
        time.sleep(2)
    return None

def _attendre_agent(ip, timeout=30, vmid=None):
    # `vmid` sert UNIQUEMENT à joindre l'en-tête d'auth de l'agent (X-MXL-Agent-Token) : depuis que
    # MXL_AGENT_TOKEN est réellement injecté au `docker run`, un agent sous token répond 401 sans
    # l'en-tête → la restauration voyait « agent injoignable (timeout) » et SAUTAIT le déploiement
    # de chaque conteneur restauré.
    from . import deploy   # helper mTLS :8081 (repli http si CA absente)
    debut = time.time()
    while time.time() - debut < timeout:
        try:
            r = deploy.agent_session().get(deploy.agent_url(ip, "/status"), timeout=2,
                                           headers=deploy.agent_headers(vmid))
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False

def restaurer_projet(project_id, progress=None, should_abort=None, only_vmids=None, preserve_uuid=False):
    """Recrée et redéploie tous les containers d'un projet.

    `progress(msg)`   : callback optionnel pour streamer chaque (sous-)étape.
    `should_abort()`  : callback optionnel ; si True (vérifié entre containers),
                        on stoppe et le reste est compté en échec « interrompu ».
    `only_vmids`      : si fourni (iterable de VMID *du snapshot*), ne traite que
                        ces containers — utilisé pour réessayer les échecs.

    Retourne un bilan {ok, total, failed:[{vmid, hostname, reason}], error?}.
    `failed[].vmid` est le VMID **d'origine** (snapshot), réutilisable pour un retry.
    """
    _p     = progress or (lambda m: None)
    _abort = should_abort or (lambda: False)

    p = db_get_project(project_id)
    if not p:
        _p("✕ projet introuvable")
        return {"ok": 0, "total": 0, "failed": [], "error": "projet introuvable"}

    # Verrou de cycle de vie (chantier 3) : un seul chargement à la fois par projet.
    lock = _project_lock(project_id)
    if not lock.acquire(blocking=False):
        _p("✕ un chargement/déchargement de ce projet est déjà en cours")
        return {"ok": 0, "total": 0, "failed": [], "error": "chargement déjà en cours"}
    db_set_project_state(project_id, "loading")
    try:
        return _restaurer_projet_verrouille(project_id, p, _p, _abort,
                                            only_vmids, preserve_uuid)
    except Exception:
        db_set_project_state(project_id, "error")
        raise
    finally:
        lock.release()


def _restaurer_projet_verrouille(project_id, p, _p, _abort, only_vmids, preserve_uuid):
    db_add_alert("alert.projet.restauration_en_cours", "info", kind="deploy",
                 params={"p": p["name"]})
    _p(f"Restauration du projet « {p['name']} »…")

    # Dossier média garanti (un projet importé/cloné n'en a pas → les containers
    # retomberaient sur la racine partagée de tous les projets).
    _ensure_media_dir(p)

    # Receivers d'abord (les multiviews ont besoin de leur SHM), puis workers
    from . import plugins as _plg
    snapshot = sorted(p["snapshot"], key=lambda c: _plg.REGISTRY.get(
        (c.get("deploy_config") or {}).get("type"), {}).get("deploy_order", 99))

    # Préfixe le hostname par le nom du projet + réécrit les références shm internes.
    snapshot = _prefix_snapshot(snapshot, p["name"])

    # Reprise ciblée des échecs : on ne garde que les VMID d'origine demandés.
    if only_vmids is not None:
        wanted = {int(v) for v in only_vmids}
        snapshot = [c for c in snapshot if c.get("vmid") in wanted]
        _p(f"Reprise des échecs : {len(snapshot)} container(s) à retraiter.")

    plan = planifier_restore(snapshot)
    if not plan["can_restore"]:
        details = []
        if plan["hostname_conflicts"]:
            details.append("hostnames : " + ", ".join(plan["hostname_conflicts"]))
        if plan["shm_conflicts"]:
            details.append("SHM : " + ", ".join(plan["shm_conflicts"]))
        for iss in plan.get("capacity_issues") or []:
            details.append(iss.get("detail") or iss.get("kind"))
        if plan.get("error"):
            details.append(plan["error"])
        msg = (f"Projet '{p['name']}' — restauration annulée (refus bloquant du "
               f"pré-vol) : " + " ; ".join(details))
        # Un seul message composé n'est pas traduisible terme à terme (chaque raison est une
        # sous-phrase française) : une alerte PAR raison bloquante, chacune une clé complète.
        if plan["hostname_conflicts"]:
            db_add_alert("alert.projet.restore_conflit_hostnames", "error", kind="deploy",
                         params={"p": p["name"], "liste": ", ".join(plan["hostname_conflicts"])})
        if plan["shm_conflicts"]:
            db_add_alert("alert.projet.restore_conflit_shm", "error", kind="deploy",
                         params={"p": p["name"], "liste": ", ".join(plan["shm_conflicts"])})
        for iss in plan.get("capacity_issues") or []:
            if iss.get("kind") == "cores":
                db_add_alert("alert.projet.restore_capacite_coeurs", "error", kind="deploy",
                             params={"p": p["name"], "need": iss.get("need"), "free": iss.get("free")})
            elif iss.get("kind") == "mcast":
                db_add_alert("alert.projet.restore_capacite_mcast", "error", kind="deploy",
                             params={"p": p["name"], "need": iss.get("need"), "free": iss.get("free")})
            else:
                db_add_alert("alert.projet.restore_capacite_autre", "error", kind="deploy",
                             params={"p": p["name"], "kind": iss.get("kind") or "?"})
        if plan.get("error"):
            # Site mort en pratique (`planifier_restore` ne pose jamais "error") : conservé par
            # défense, clé générique sans contenu français composé.
            db_add_alert("alert.projet.restore_refus_autre", "error", kind="deploy",
                         params={"p": p["name"], "e": plan["error"]})
        _p("✕ " + msg)
        db_set_project_state(project_id, "saved")
        return {"ok": 0, "total": len(snapshot), "failed":
                [{"vmid": c["vmid"], "hostname": c.get("hostname"), "reason": "conflit"}
                 for c in snapshot],
                "error": "conflit"}

    remap = {r["from"]: r["to"] for r in plan["remaps"]}
    for r in plan["remaps"]:
        db_add_alert(
            "alert.projet.vmid_remappe", "info", kind="deploy",
            params={"p": p["name"], "from": r["from"], "to": r["to"], "h": r["hostname"]})
        _p(f"VMID {r['from']} déjà pris → remappé sur {r['to']} ({r['hostname']}).")

    total     = len(snapshot)
    ok_count  = 0
    failed    = []   # [{vmid (origine), hostname, reason}]
    vmid_map  = {}   # vmid d'origine (snapshot) → vmid créé (remap des vues à la fin)

    for i, c in enumerate(snapshot, 1):
        if _abort():
            _p(f"⛔ Interruption demandée — arrêt ({i-1}/{total} traités).")
            for c2 in snapshot[i-1:]:
                failed.append({"vmid": c2["vmid"], "hostname": c2.get("hostname"),
                               "reason": "interrompu"})
            break

        vmid     = remap.get(c["vmid"], c["vmid"])
        hostname = c.get("hostname") or f"#{vmid}"
        _p(f"[{i}/{total}] {hostname} (vmid {vmid})…")

        def _echec(reason, niveau="error", clef=None, **extra):
            # `reason` reste le texte LIBRE consommé tel quel par l'UI (failed[].reason,
            # static/scripts.js) — inchangé. `clef` (optionnel) est la clé i18n migrée pour
            # l'alerte elle-même : jurisprudence oblige (jamais un verdict/texte libre en
            # paramètre), CHAQUE site d'appel a sa propre clé complète plutôt qu'un {reason}
            # générique.
            if clef:
                params = {"p": p["name"], "h": hostname, "vmid": vmid}
                params.update(extra)
                db_add_alert(clef, niveau, vmid=vmid, kind="deploy", params=params)
            else:
                db_add_alert(f"Projet '{p['name']}' — {hostname} (vmid {vmid}) : {reason}",
                             niveau, vmid=vmid, kind="deploy")
            failed.append({"vmid": c["vmid"], "hostname": c.get("hostname"), "reason": reason})

        _dc_type = (c.get("deploy_config") or {}).get("type")
        # Types liés au nœud / infra partagée (2110_io, passerelle…) : jamais restaurés
        # par projet — présents dans les VIEUX snapshots seulement, sautés sans erreur.
        if _dc_type in PROJECT_EXCLUDED_TYPES:
            _p(f"  · {hostname} : type « {_dc_type} » lié au nœud/infra — non restauré par projet (ignoré)")
            total -= 1
            continue

        # 1) Création : chemin Docker (compute/MTL) uniquement ; un snapshot non-docker est rejeté.
        _p("      · clonage + configuration + démarrage…")
        from . import plugins as _pl
        _is_docker = bool(_dc_type) and _pl.runtime(_dc_type) == "docker"
        if _is_docker:
            # Type docker-only (chemin compute) : pas de vaisseau LXC. On crée le conteneur
            # compute sur son nœud (épinglé dans le snapshot, sinon auto-pick) ; deployer_script
            # fera le docker run + attente agent + push du script (cf. _insert_udc).
            from . import docker_compute as _dctr
            _node_id = _dctr.pick_compute_node(c.get("node_id"))
            if not _node_id:
                _p("  ✕ aucun nœud compute configuré")
                _echec("aucun nœud compute pour un type docker-only", clef="alert.projet.echec_aucun_noeud_compute")
                continue
            try:
                _vmid_new = _dctr.creer_container_compute(_node_id, _dc_type, hostname=c["hostname"])
            except Exception as e:
                _p(f"  ✕ exception à la création compute : {e}")
                _echec(f"exception création compute : {e}", clef="alert.projet.echec_creation_compute_exception", e=str(e))
                continue
            if not _vmid_new:
                _p("  ✕ création compute échouée")
                _echec("création compute échouée", clef="alert.projet.echec_creation_compute")
                continue
            vmid = _vmid_new   # vmid synthétique alloué par le chemin compute
        else:
            # Backend LXC/Proxmox retiré (full-Docker) : un snapshot d'un type non-docker
            # (ancien conteneur LXC) n'a plus de cible de restauration. Rejet explicite.
            _p("  ✕ snapshot LXC legacy non restaurable (full-Docker)")
            _echec(f"type '{_dc_type or '?'}' non-docker : snapshot LXC legacy non restaurable", clef="alert.projet.echec_type_non_docker", dc_type=(_dc_type or "?"))
            continue

        # Identité d'instance : COPIE = nouvel uuid (généré à la création, défaut, sûr) ;
        # DÉPLACEMENT (preserve_uuid) = on réapplique l'instance_uuid du snapshot → identité/NMOS
        # conservées (cf. doublon si la source tourne encore — assumé par le choix opérateur).
        if preserve_uuid and c.get("instance_uuid"):
            db_set_instance_uuid(vmid, c["instance_uuid"])
            _p("      · identité conservée (déplacement)")

        # Mémoires par-container (DVE, multiview, presets…) : recréées sous le
        # nouveau vmid (scope remappé). Embarquées dans le snapshot à la sauvegarde.
        mems = c.get("memories") or []
        if mems:
            ok_mem = 0
            for m in mems:
                try:
                    plugin_store_create(m["type"], str(vmid), m["name"],
                                        m.get("value"), unique_name=True)
                    ok_mem += 1
                except Exception as e:
                    _p(f"      · mémoire « {m.get('name')} » non restaurée : {e}")
            if ok_mem:
                _p(f"      · {ok_mem} mémoire(s) restaurée(s).")

        dc = c.get("deploy_config")
        if not dc:
            vmid_map[c["vmid"]] = vmid
            _p("  ✓ créé (aucun script à déployer)")
            ok_count += 1
            continue

        # Multicast TX : JAMAIS rejoué du snapshot — purge → le hook before_deploy
        # réalloue via mcast_allocations (owner_ref = nouveau vmid).
        params = dc.get("params", {}) or {}
        n_mc = _purge_tx_multicast(params)
        if n_mc:
            _p(f"      · {n_mc} adresse(s) multicast TX à réallouer au déploiement")

        # Chemin docker-only : pas d'attente IP/agent LXC ; deployer_script fait le docker run +
        # attente agent + push script. On rattache le projet AVANT (le bind média lit project_id).
        if _is_docker:
            from .database import db_set_project
            db_set_project(vmid, project_id)
            _p(f"      · déploiement compute du script « {dc['type']} »…")
            try:
                if not deployer_script(vmid, dc["type"], params):
                    _p("  ✕ déploiement compute échoué")
                    _echec("déploiement compute échoué", clef="alert.projet.echec_deploiement_compute")
                    continue
            except Exception as e:
                _p(f"  ✕ déploiement compute échoué : {e}")
                _echec(f"déploiement compute échoué : {e}", clef="alert.projet.echec_deploiement_compute_exception", e=str(e))
                continue
            vmid_map[c["vmid"]] = vmid
            _p("  ✓ créé + déployé (compute)")
            ok_count += 1
            continue

        # 2) IP
        _p("      · attente de l'IP…")
        ip = _attendre_ip(vmid)
        if not ip:
            _p("  ✕ IP non obtenue (timeout)")
            _echec("IP non obtenue", "warning", clef="alert.projet.echec_ip_non_obtenue")
            continue

        # 3) Agent
        _p(f"      · IP {ip} obtenue, attente de l'agent…")
        if not _attendre_agent(ip, vmid=vmid):
            _p("  ✕ agent injoignable (timeout)")
            _echec("agent injoignable", "warning", clef="alert.projet.echec_agent_injoignable")
            continue

        # 4) Déploiement (protégé : un échec ne doit plus tuer toute la boucle)
        _p(f"      · déploiement du script « {dc['type']} »…")
        try:
            deployer_script(vmid, dc["type"], params)
        except Exception as e:
            _p(f"  ✕ déploiement échoué : {e}")
            _echec(f"déploiement échoué : {e}", clef="alert.projet.echec_deploiement_exception", e=str(e))
            continue

        vmid_map[c["vmid"]] = vmid
        _p("  ✓ créé + déployé")
        ok_count += 1

    # Remap des VUES (chantier 3) : les widgets référencent instance_uuid/vmid — après un
    # rechargement (les originaux n'existent plus), on re-pointe les widgets orphelins sur
    # les clones. Une COPIE à côté d'originaux vivants ne vole pas les vues (on ne remappe
    # que si l'ancienne cible a disparu).
    if vmid_map:
        try:
            _remap_project_views(project_id, vmid_map, _p)
        except Exception as e:
            log.warning("remap vues projet #%s: %s", project_id, e)

    if failed:
        db_add_alert(
            "alert.projet.restaure_avec_erreurs", "warning", kind="deploy",
            params={"p": p["name"], "ok": ok_count, "total": total, "nfail": len(failed)})
    else:
        db_add_alert("alert.projet.restaure_ok", "info", kind="deploy",
                     params={"p": p["name"], "ok": ok_count, "total": total})

    db_set_project_state(project_id,
                         "error" if failed else ("active" if ok_count else "saved"))
    if ok_count:
        # La production TOURNE : c'est ici qu'elle reçoit son niveau de tally, pas à
        # l'enregistrement. Un serveur qui stocke cinquante projets et n'en joue qu'un n'a pas à
        # traîner cinquante niveaux que rien ne sert. Idempotent : un projet rejoué retrouve le
        # sien, avec son UUID — ce que ses conteneurs citent.
        try:
            from .database import db_assurer_niveau_projet
            db_assurer_niveau_projet(project_id, p.get("name"))
        except Exception as e:
            log.warning("projet %s : niveau de tally non semé (%s)", project_id, e)
    return {"ok": ok_count, "total": total, "failed": failed}


def _remap_project_views(project_id, vmid_map, _p):
    """Re-pointe les widgets des vues du projet sur les containers fraîchement créés,
    UNIQUEMENT si leur cible actuelle a disparu (vmid absent de la DB)."""
    from .database import (db_project_views, db_update_view, db_get_container)
    live = {c["vmid"] for c in db_get_containers()}
    for v in db_project_views(project_id):
        changed = False
        lay = v.get("layout") or []
        for w in lay:
            old = w.get("vmid")
            if old in vmid_map and old not in live:
                new = vmid_map[old]
                nc = db_get_container(new) or {}
                w["vmid"] = new
                w["instance_uuid"] = nc.get("instance_uuid") or w.get("instance_uuid")
                changed = True
        if changed:
            db_update_view(v["id"], layout=lay)
            _p(f"      · vue « {v['name']} » re-pointée sur les nouveaux containers")


def detruire_containers_projet(project_id):
    p = db_get_project(project_id)
    if not p:
        return
    lock = _project_lock(project_id)
    if not lock.acquire(blocking=False):
        db_add_alert("alert.projet.operation_en_cours", "warning", kind="deploy",
                     params={"p": p["name"]})
        return
    db_set_project_state(project_id, "unloading")
    try:
        db_add_alert("alert.projet.destruction_en_cours", "warning", kind="deploy",
                     params={"p": p["name"]})
        # Cibles = containers RATTACHÉS au projet (project_id — couvre les vmids remappés
        # au chargement) + les vmids du snapshot encore vivants (rattachement legacy).
        vmids = {c["vmid"] for c in db_get_containers()
                 if c.get("project_id") == project_id}
        live = {c["vmid"] for c in db_get_containers()}
        vmids |= {c["vmid"] for c in p["snapshot"] if c.get("vmid") in live}
        for vmid in sorted(vmids):
            try:
                detruire_container(vmid)
            except Exception as e:
                db_add_alert("alert.projet.destruction_container_echouee", "error", vmid=vmid,
                             kind="deploy", params={"p": p["name"], "vmid": vmid, "e": str(e)})
        db_add_alert("alert.projet.containers_detruits", "warning", kind="deploy",
                     params={"p": p["name"]})
    finally:
        db_set_project_state(project_id, "saved")
        lock.release()
