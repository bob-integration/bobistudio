# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import json
from .numerotation import (cle_input, cle_input_v, cle_input_a,
                           flux_video, flux_audio, flux_anc)
import os
import threading
import time
import requests
import logging
from .config import DB_PATH
from .addressing import get_container_ip
from .database import (db_update_ip, db_update_fps, db_update_status,
                      db_add_alert, db_update_usage, db_get_container)
from .episodes import EtatEpisodes as _Episodes

log = logging.getLogger(__name__)

# Mémoires d'état pour ne loguer que sur transition (évite le spam toutes les 5s)
_prev_script_running = {}   # vmid → bool | None
_prev_agent_ok       = {}   # vmid → bool | None

# Compteurs de redémarrage automatique de script ; reset sur reprise confirmée
_script_restart_count = {}  # vmid → int
SCRIPT_RESTART_ALERT_THRESHOLD = 3
# Anti-crash-loop (audit B3) : backoff exponentiel entre tentatives + quarantaine au-delà d'un
# plafond d'échecs consécutifs (le POST /start inconditionnel toutes les 5 s masquait les
# crash-loops et spammait l'alerte seuil à chaque tick).
_restart_next_try = {}      # vmid → time.monotonic() de la prochaine tentative autorisée
_crash_quarantine = set()   # vmids en quarantaine : plus d'auto-restart (sortie : reprise/restart manuel)
_script_running_since = {}  # vmid → time.monotonic() du dernier passage à running:true (cf. SCRIPT_STABLE_S)
SCRIPT_STABLE_S                = 60.0   # marche continue requise pour considérer la reprise CONFIRMÉE
                                        # (et seulement alors remettre compteur/backoff à zéro)
SCRIPT_RESTART_BACKOFF_S       = 5.0    # délai initial, doublé à chaque échec consécutif
SCRIPT_RESTART_BACKOFF_MAX_S   = 300    # plafond du backoff (setting script_restart_backoff_max_s)
SCRIPT_RESTART_QUARANTINE      = 10     # échecs consécutifs → quarantaine (setting script_restart_quarantine_count)


class _EnVol:
    """Ensemble de vmid « réparation en vol », avec un add atomique (test-and-set sous verrou) :
    `add_if_absent` renvoie False si le vmid y était déjà → l'appelant renonce. Un simple
    `if vmid not in s: s.add(vmid)` laisserait passer deux threads de surveillance concurrents."""
    def __init__(self):
        self._s = set()
        self._lock = threading.Lock()

    def add_if_absent(self, vmid):
        with self._lock:
            if vmid in self._s:
                return False
            self._s.add(vmid)
            return True

    def discard(self, vmid):
        with self._lock:
            self._s.discard(vmid)


_redeploy_inflight = _EnVol()


def reset_crash_loop(vmid):
    """Sort `vmid` de la quarantaine crash-loop et remet compteur/backoff à zéro. Appelé sur
    reprise confirmée du script, et par le restart manuel (containers.redemarrer_container)."""
    _script_restart_count.pop(vmid, None)
    _restart_next_try.pop(vmid, None)
    _script_running_since.pop(vmid, None)
    _crash_quarantine.discard(vmid)
    _reset_cadence(vmid)


def _redeployer_script_perdu(vmid):
    """Script PERDU (agent :8081 → running:false, path:null) : le rootfs éphémère du conteneur a
    été recréé (docker restart) et /opt/script est vide — l'auto-restart /start ne peut rien
    relancer. Redéploie le script complet depuis le deploy_config persisté (sérialisé par le
    verrou vmid de deployer_script). Le compteur/backoff de l'appelant reste appliqué : un
    redéploiement qui échoue en boucle finit en quarantaine comme un crash-loop classique.

    GARDE D'UNICITÉ : un seul redéploiement-réparation en vol par vmid. `est_verrouille` (côté
    appelant) ne couvre pas la fenêtre entre le démarrage du thread et la prise du verrou vmid ;
    sans ce garde, deux ticks de surveillance rapprochés empilaient deux réparations sur le même
    conteneur (elles se sérialisent ensuite, mais la seconde est un pur doublon).

    ★ GARDE DE FRAÎCHEUR (2026-08-10) : cette réparation restaure une SAUVEGARDE (le
    `deploy_config` persisté). Si un VRAI déploiement, porteur de paramètres NEUFS, est en train de
    passer, la restaurer revient à ANNULER le changement de l'exploitant — et `deployer_script`
    renvoyant `True` des deux côtés, personne ne l'apprend jamais. Vécu ce jour sur le mur 906 :
    un déploiement activant `gpu_slice` a recréé le conteneur (signature d'env changée) ; la
    surveillance a vu le rootfs vide pendant les 1,6 s qui séparent la recréation du push, lancé sa
    réparation, et le script est reparti SANS le paramètre — les deux « déployé et redémarré » se
    lisent dans les alertes à 680 ms d'écart.

    Le garde `est_verrouille` de l'appelant aurait dû l'empêcher, mais `vmlocks` est un registre de
    `threading.RLock` : un verrou EN MÉMOIRE DE PROCESSUS. Un déploiement lancé hors du processus
    Flask — script d'administration, ou le CONTRÔLEUR DE SECOURS en HA — lui est strictement
    invisible. On ne peut donc pas se fier au verrou seul.

    Parade : reprendre le verrou vmid (RLock ré-entrant : `deployer_script` le reprendra sans
    interblocage), puis RE-VÉRIFIER SOUS VERROU que le script est toujours absent. Si quelqu'un
    l'a déployé entre-temps, il n'y a plus rien à réparer — et surtout, plus rien à écraser. La
    vérification porte sur l'ÉTAT OBSERVÉ (l'agent), jamais sur ce que la base prétend."""
    if not _redeploy_inflight.add_if_absent(vmid):
        log.debug("redéploiement script perdu %s : déjà en vol — ignoré", vmid)
        return
    try:
        from .vmlocks import verrou_vmid
        with verrou_vmid(vmid, op="reparation-script"):
            # Relecture SOUS VERROU : entre le constat de la boucle et ici, un déploiement a pu
            # persister de nouveaux params. Un snapshot pris trop tôt serait exactement la mise à
            # jour perdue qu'on cherche à empêcher.
            c = db_get_container(vmid)
            if not c:
                return
            if not _script_toujours_absent(vmid, c):
                log.info("Réparation du script %s ABANDONNÉE : le script est de retour — un "
                         "déploiement concurrent l'a posé. Restaurer la sauvegarde par-dessus "
                         "annulerait ses paramètres.", vmid)
                return
            dc = c.get("deploy_config")
            dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
            t = dc.get("type")
            if not t:
                db_add_alert("alert.agent.type_inconnu", "error",
                             vmid=vmid, node_id=c.get("node_id"), kind="agent",
                             params={"vmid": vmid})
                return
            db_add_alert("alert.agent.redeploiement_auto", "warning",
                         vmid=vmid, node_id=c.get("node_id"), kind="agent",
                         params={"vmid": vmid, "t": t})
            from .deploy import deployer_script
            deployer_script(vmid, t, dc.get("params") or {})
    except Exception as e:
        log.warning(f"Redéploiement script perdu {vmid} échoué : {e}")
    finally:
        _redeploy_inflight.discard(vmid)


def _script_toujours_absent(vmid, c=None):
    """Le script est-il ENCORE absent du disque du conteneur ? (agent :8081 → `path` nul)

    Deuxième regard, pris sous verrou, juste avant d'écraser avec la sauvegarde. Rend False dès
    que le doute existe — agent muet, IP inconnue, exception : dans ce cas on NE RÉPARE PAS.
    L'asymétrie est délibérée. Ne pas réparer un conteneur réellement cassé coûte un tour de
    boucle de 5 s, et la panne sera reconstatée ; écraser un déploiement légitime coûte un
    changement de l'exploitant perdu EN SILENCE, que rien ne rattrape.
    """
    try:
        ip = (c or db_get_container(vmid) or {}).get("ip")
        if not ip:
            return False
        # Imports LOCAUX comme partout ailleurs dans ce module : `agent_session`/`agent_url` ne
        # sont pas liés au niveau module (seulement dans `rafraichir_metrics`). S'y référer sans
        # les importer lèverait un NameError que le `except` ci-dessous avalerait — le garde
        # rendrait alors False EN TOUTES CIRCONSTANCES, et la réparation du script perdu, elle,
        # ne se déclencherait PLUS JAMAIS. Un garde-fou muet est pire que pas de garde-fou.
        from .deploy import (agent_headers as _ah, agent_session as _agent_session,
                             agent_url as _agent_url)
        r = _agent_session().get(_agent_url(ip, "/status"), timeout=3, headers=_ah(vmid))
        if r.status_code != 200:
            return False
        return not (r.json() or {}).get("path")
    except Exception as e:
        log.debug("vérification script absent %s : %s — réparation abandonnée par prudence", vmid, e)
        return False


def _crash_loop_logs(vmid, tail=15):
    """Dernières lignes de log du conteneur via l'agent-nœud (diagnostic crash-loop, audit B3).
    Best-effort : '' si nœud/nom inconnus ou agent injoignable (motif services/rdma)."""
    try:
        c = db_get_container(vmid)
        if not c or not c.get("node_id") or not c.get("docker_name"):
            return ""
        from .database import db_get_node
        from . import node_driver
        node = db_get_node(c["node_id"])
        if not node:
            return ""
        lines = node_driver.container_logs(node, c["docker_name"], tail=tail) or []
        return " | ".join(str(l).strip() for l in lines[-4:] if str(l).strip())
    except Exception as e:
        log.debug(f"crash_loop logs {vmid}: {e}")
        return ""

# Cache latence par container : {vmid: {shm_name: ms_float_or_None}}.
# Pas persisté en DB (valeur instantanée haute-fréquence comme fps).
latency_cache = {}

# Cache latence PROPRE (traitement) par nœud : {vmid: ms}. own_latency_ms = ts_out − ts_cycle_start,
# rapporté par les plugins processeurs (:8080). Distinct de latency_cache (= transit par arête).
# Consommé par la topologie : badge ⧖ du nœud + cumul _delay_out.
own_latency_cache = {}
# DÉLAI D'ÉTAGE en TRAMES : `index de sortie − index d'entrée`, publié par le plugin.
# ⚠ À NE PAS CONFONDRE avec own_latency_cache, qui est un TEMPS DE CALCUL (ts_out − ts_cycle_start).
# Ce sont deux grandeurs différentes, pas deux unités de la même : un étage peut calculer en 4 ms
# et retarder le signal de 2 trames, parce que ce qui domine est la QUANTIFICATION par la cadence
# (lire un contenu daté N, publier en N+2). Le cumul de la page Câbles additionnait des temps de
# calcul en croyant additionner des délais — 4,3 ms affichés pour 4,0 trames réelles (facteur ~19).
# C'est la SEULE mesure directe d'un étage ; tout le reste est un modèle.
# vmid → {"trames": float, "trames_max": float}. Absent = l'étage ne mesure pas (≠ zéro).
# Cf. docs/reference/LATENCE_CHAINE.md.
delai_etage_cache = {}
# Cache GPU par conteneur : {vmid: {gpu: bool, name: str}}. Renseigné depuis `gpu`/`gpu_name` de :8080
# (plugins compositing GPU, ex. multiview cupy). Consommé par le badge GPU (home + topologie Câbles).
gpu_cache = {}
# Cache mode tranche RUNTIME par conteneur : bool(m["slice_mode"]) — le multiview ne pose la clé
# que s'il tranche RÉELLEMENT (un slice_mode=true de CONFIG peut être replié en trame entière :
# GPU sans gpu_slice, portrait, hauteur non divisible…). Consommé par le badge ▤ de la topologie
# (le badge lisait la CONFIG → « Tranche » affiché sur un mur GPU qui ne tranche pas).
slice_cache = {}

# Détection « abonné mais ne reçoit pas » par slot RX 2110_io : un receiver en mode="mtl" dont le
# frame_index n'avance pas (création st20p_rx_create ratée — budget lcores —, OU aucun trafic réseau).
# {vmid: {idx: True/False}} consommé par la topologie + la page I/O Sources (badge + alerte).
rx_stalled_cache = {}
_rx_fi_prev   = {}   # (vmid, idx) → dernier frame_index vu
_rx_stall_cnt = {}   # (vmid, idx) → nb de polls consécutifs sans avance (≥ seuil ⇒ stalled)
RX_STALL_POLLS = 3   # tolérance avant de déclarer stalled (évite le faux positif au démarrage)

# Symétrique côté TX : un sender vidéo « activé + câblé » (inputs_latency_ms renseigné par le moteur ⇒
# enabled & shm_in & entrée fraîche) censé émettre (fps_nominal>0) mais à fps≈0 sur ≥ seuil polls ⇒
# n'émet aucun flux (entrée morte, budget files/lcores, ou files désalignées). {vmid: {idx: bool}}.
tx_stalled_cache = {}
_tx_stall_cnt = {}   # (vmid, tx_idx) → nb de polls consécutifs à fps≈0 alors qu'il devrait émettre
_ip_warn_prev = {}   # vmid → dernier nic.ip_warnings vu (alerte UNE fois par changement d'état)

# Fil vs source (2110_io TX, moteur ≥ chantier instrumentation) : fps = cadence RÉELLE sur le fil
# (trames remises à libmtl/s), fps_source = trames UNIQUES/s (nouveau grain source), repeats =
# compteur cumulé de trames rejouées. Simple relais {vmid: {idx: {fps_source, repeats}}} — pas
# encore d'alerte dessus (lot ultérieur) ; champs absents (vieille image moteur) → None.
tx_source_cache = {}

# ─── État CANONIQUE par flux 2110 (RX et TX), toutes essences ────────────────────────────────
# {vmid: {"<rx|tx>:<essence>:<idx>": {mode, fps, fps_nominal, frame_index, latency_ms, late,
#                                     repeats, fps_source, inputs_latency_ms, signal, stalled}}}
#
# Pourquoi une table de plus alors qu'il en existe déjà cinq : les caches ci-dessus sont taillés
# pour la topologie (clé = nom de shm, vidéo seule pour certains, tri-état documenté pour
# rx_served_cache qui OMET délibérément les slots inactifs). Aucun d'eux ne répond à la question
# « quel est l'état complet du slot RX/TX n° i, servi ou non ? » — question que pose tout consommateur
# de supervision, à commencer par les monitors BCP-008 exposés en IS-12 (services/nmos/monitors.py).
# Sans elle, ce consommateur devait re-poller :8080 pour lui-même, alors que la boucle de
# surveillance vient de le faire. C'est donc un RELAIS BRUT de ce que le moteur a publié (pas une
# dérivation), écrit une seule fois par tick, plus le drapeau `stalled` calculé juste en dessous
# (il demande un historique, le moteur ne le porte pas). Non persisté (valeur instantanée).
flux_etat_cache = {}

# État des liens physiques des ports média d'un moteur 2110_io : {vmid: {"up": [iface…],
# "down": [iface…]}}, depuis nic.ports[].link_up de :8080. Personne ne le conservait — la page
# Câbles lit les débits, pas l'état de lien. Consommé par le linkStatus des monitors BCP-008.
nic_link_cache = {}


def cle_flux(sens, essence, idx, sub_idx=None):
    """Clé canonique d'un flux dans `flux_etat_cache`. `sub_idx` distingue les N flux audio d'un
    MÊME slot TX (un slot 2110_io porte une vidéo, plusieurs 2110-30 et un 2110-40)."""
    ess = "anc" if essence == "data" else essence
    base = "{}:{}:{}".format(sens, ess, idx)
    return base if sub_idx is None else "{}.{}".format(base, sub_idx)


def etat_flux(vmid, sens, essence, idx, sub_idx=None):
    """État courant d'un flux du moteur 2110_io, ou None si inconnu (moteur muet, slot absent).

    `sens` = "rx" | "tx". Lecture seule sur `flux_etat_cache` — aucun appel réseau : la boucle de
    surveillance a déjà relevé :8080 au dernier tick."""
    d = flux_etat_cache.get(vmid)
    if not d:
        return None
    r = d.get(cle_flux(sens, essence, idx, sub_idx))
    return dict(r) if r else None


def etat_liens(vmid):
    """{"up": [...], "down": [...]} des ports média du moteur, ou None si jamais relevé."""
    d = nic_link_cache.get(vmid)
    return {"up": list(d.get("up") or []), "down": list(d.get("down") or [])} if d else None


# ─── Épisodes de panne de flux 2110 : PERSISTÉS + honnêtes sur l'ancienneté ──────────────────
# Les sentinelles ci-dessus (_rx_stall_cnt = -1 « déjà alerté », _tx_stall_cnt) sont des dicts EN
# MÉMOIRE du process orchestrateur : tout redémarrage repartait d'un état vide → la panne EN COURS
# était re-signalée comme neuve, et la branche `else` (mode ni "error" ni "mtl") faisait un .pop()
# immédiat → un simple blip du mode réarmait la sentinelle. Mesuré en prod (24-25/07) : le slot 3
# de mtlrx603 a réémis EXACTEMENT la même alerte 118 fois, saturant le journal (rétention 1000
# lignes ≈ 2 jours) et affichant « il y a 2 minutes » pour une panne de 33 h — l'ancienneté
# maquillée désamorce l'urgence.
#
# On tient donc un ÉPISODE par flux, clé stable (vmid, "rx"|"tx", idx), persisté en JSON à côté de
# la DB (motif node_health.STATS_PERSIST_PATH : chemin dérivé de DB_PATH, écriture atomique
# tmp+rename, flush périodique, rechargement au boot) :
#   - une seule alerte à l'OUVERTURE de l'épisode, portant l'instant de franchissement ;
#   - aucune ré-émission tant que l'épisode reste ouvert, redémarrage de l'orchestrateur compris ;
#   - une alerte de RÉSOLUTION avec la durée réelle quand la panne se lève (modèle
#     node_recovery.evaluer_prep : état partagé, transition dans les deux sens, message qui nomme
#     ce qui s'est levé) ;
#   - hystérésis : il faut FLUX_RESOLVE_POLLS observations saines CONSÉCUTIVES pour clore, et
#     FLUX_IDLE_POLLS observations hors état surveillé avant de lâcher les compteurs de détection.
# Les caches consommés par l'UI (rx_stalled_cache/rx_fps_cache/rx_latency_cache/tx_stalled_cache)
# et les seuils de détection ne changent NI de schéma NI de comportement.
PANNES_PERSIST_PATH = os.path.join(os.path.dirname(DB_PATH) or ".", "flux_pannes.json")
PANNES_FLUSH_S      = 300.0   # flush périodique (la boucle surveillance tourne toutes les 5 s :
                              # aucune I/O disque dans le chemin chaud). Les transitions
                              # (ouverture/clôture d'épisode), elles, sont rares → flush immédiat.
FLUX_RESOLVE_POLLS  = 3       # observations saines consécutives avant de clore un épisode
FLUX_IDLE_POLLS     = 3       # observations « hors état surveillé » avant de lâcher les compteurs

# `sens` d'un épisode → `kind` d'alerte (vocabulaire FERMÉ database.ALERT_KINDS). Le sens "agent"
# réutilise CE mécanisme (persistance, ancienneté honnête, alerte unique + résolution chiffrée)
# pour l'état « conteneur injoignable » : il n'a pas de slot, donc idx=None.
_SENS_KIND = {"rx": "rx_stall", "tx": "tx_stall", "agent": "agent"}

_pannes            = {}   # (vmid, sens, idx) → {kind, since, ident, niveau}
_pannes_lock       = threading.Lock()
_pannes_last_flush = 0.0
_pannes_dirty      = False
_flux_ok_cnt       = {}   # (vmid, sens, idx) → nb d'observations saines consécutives
_rx_off_cnt        = {}   # (vmid, idx) → nb de polls consécutifs avec un mode RX non surveillé
_tx_off_cnt        = {}   # (vmid, idx) → idem côté TX (slot ni activé ni câblé)


def _duree_fr(secondes):
    """Durée lisible en français, à la granularité utile (« 47 s », « 12 min », « 33 h », « 2 j 9 h »)."""
    s = max(0, int(secondes or 0))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h" + (f" {m:02d}" if m else "")
    j, h = divmod(s // 3600, 24)
    return f"{j} j" + (f" {h} h" if h else "")


def _horodate_fr(ts):
    try:
        return time.strftime("%d/%m %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "?"


def _panne_serialiser_cle(k):
    vmid, sens, idx = k
    return f"{vmid}|{sens}|{idx}"


def _panne_deserialiser_cle(s):
    """Inverse de _panne_serialiser_cle. None si la ligne est inexploitable (fichier bricolé)."""
    parts = str(s).split("|")
    if len(parts) != 3 or parts[1] not in _SENS_KIND:
        return None
    vmid, idx = parts[0], parts[2]
    try:
        vmid = int(vmid)
    except ValueError:
        pass
    if idx == "None":            # slot sans index (moteur ancien) : la clé mémoire porte None,
        idx = None               # la relire en chaîne ferait un épisode ORPHELIN → ré-alerte
    else:
        try:
            idx = int(idx)
        except ValueError:
            pass
    return (vmid, parts[1], idx)


def _flush_pannes(force=False):
    """Écrit les épisodes ouverts sur disque (tmp + rename). Purge au passage les épisodes dont le
    vmid n'existe plus en base — sinon le fichier grossit indéfiniment. JAMAIS silencieux : un échec
    d'écriture est logué en WARNING (on ne peut pas faire croire que l'état est sauvegardé)."""
    global _pannes_last_flush, _pannes_dirty
    now = time.time()
    if not force:
        if now - _pannes_last_flush < PANNES_FLUSH_S:
            return                      # chemin chaud (tick 5 s) : aucune I/O disque
        if not _pannes_dirty and not _pannes:
            return                      # rien à écrire ni à purger
    vivants = None
    try:
        from .database import db_get_containers
        vivants = {c.get("vmid") for c in (db_get_containers() or [])}
    except Exception as e:
        log.debug("purge épisodes de panne : liste des conteneurs indisponible (%s)", e)
    with _pannes_lock:
        if vivants is not None:      # `set()` (plus aucun conteneur) DOIT purger — pas `if vivants`
            for k in [k for k in _pannes if k[0] not in vivants]:
                ep = _pannes.pop(k)
                log.info("épisode de panne %s oublié : le conteneur %s n'existe plus (ouvert le %s)",
                         ep.get("ident") or k, k[0], _horodate_fr(ep.get("since")))
        snapshot = {_panne_serialiser_cle(k): v for k, v in _pannes.items()}
    tmp = PANNES_PERSIST_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, PANNES_PERSIST_PATH)
        _pannes_last_flush = now
        _pannes_dirty = False
    except (OSError, TypeError, ValueError) as e:
        # Surtout PAS d'échec silencieux : sans ce log, un disque plein ferait re-spammer toutes
        # les alertes au prochain redémarrage sans que personne ne sache pourquoi.
        log.warning("Persistance des épisodes de panne de flux 2110 IMPOSSIBLE (%s) : %s — "
                    "l'état ne survivra pas à un redémarrage de l'orchestrateur (les alertes de "
                    "flux en panne seront ré-émises).", PANNES_PERSIST_PATH, e)


def load_persisted():
    """Recharge les épisodes de panne au boot. Appelé à l'import du module (et ré-appelable depuis
    main.py). Fichier absent/corrompu ⇒ on repart d'un état VIDE, en le DISANT."""
    global _pannes_last_flush
    _pannes_last_flush = time.time()
    try:
        with open(PANNES_PERSIST_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        log.info("Épisodes de panne de flux 2110 : aucun état persisté (%s) — départ à vide.",
                 PANNES_PERSIST_PATH)
        return
    except (ValueError, OSError) as e:
        log.warning("Épisodes de panne de flux 2110 : état persisté illisible (%s : %s) — départ à "
                    "vide, les pannes EN COURS seront re-signalées une fois.",
                    PANNES_PERSIST_PATH, e)
        return
    if not isinstance(data, dict):
        log.warning("Épisodes de panne de flux 2110 : %s n'est pas un objet JSON — départ à vide.",
                    PANNES_PERSIST_PATH)
        return
    charges = 0
    with _pannes_lock:
        _pannes.clear()
        for skey, ep in data.items():
            k = _panne_deserialiser_cle(skey)
            if k is None or not isinstance(ep, dict) or not ep.get("kind"):
                log.warning("Épisode de panne ignoré (entrée illisible) : %r", skey)
                continue
            try:
                since = float(ep.get("since") or 0)
            except (TypeError, ValueError):
                since = 0.0
            if since <= 0:
                since = time.time()
            _pannes[k] = {"kind": ep["kind"], "since": since,
                          "ident": ep.get("ident") or str(skey),
                          "niveau": ep.get("niveau") or "warning"}
            charges += 1
    if charges:
        log.info("Épisodes de panne de flux 2110 rechargés : %d en cours (aucune alerte ne sera "
                 "ré-émise pour eux) — %s", charges, PANNES_PERSIST_PATH)


def _node_de(vmid):
    """node_id du conteneur, pour le CONTEXTE d'une alerte (colonne `alerts.node_id`). Appelé
    uniquement sur les TRANSITIONS (rares) : le coût d'une lecture DB y est sans conséquence.
    Inconnu → None ; on ne devine pas."""
    try:
        return (db_get_container(vmid) or {}).get("node_id")
    except Exception:
        return None


def _flux_panne(vmid, sens, idx, kind, ident, detail, niveau="warning", cle=None, params=None):
    """Franchissement : ouvre l'épisode et alerte UNE SEULE FOIS. Appelable à chaque poll (idempotent).
    Un épisode déjà ouvert (mémoire OU fichier rechargé après redémarrage) ne ré-alerte pas.
    Un changement de NATURE de la panne ré-alerte, mais CONSERVE l'instant de franchissement
    d'origine (le flux est mort depuis ce moment-là — c'est l'ancienneté honnête).

    `ident`/`detail` restent le texte FRANÇAIS passe-plat historique (repli si `cle` est omis).
    Avec `cle` (clé i18n complète propre à l'appelant), le message est rendu depuis `params`
    (fusionné avec `ident`/les horodatages calculés ici) — `_flux_panne` choisit SEULE le suffixe
    `_requalifie` (nature du défaut changée), les autres appelants n'ont pas à connaître ce cas."""
    global _pannes_dirty
    k = (vmid, sens, idx)
    _flux_ok_cnt.pop(k, None)
    with _pannes_lock:
        ep = _pannes.get(k)
        if ep and ep.get("kind") == kind:
            return False                       # déjà signalé — jamais deux fois la même alerte
        since = float(ep["since"]) if ep else time.time()
        requalif = bool(ep)
        _pannes[k] = {"kind": kind, "since": since, "ident": ident, "niveau": niveau}
        _pannes_dirty = True
    age = time.time() - since
    if cle:
        _p = dict(params or {})
        _p.setdefault("ident", ident)
        _p["date"] = _horodate_fr(since)
        _p["duree"] = _duree_fr(age)
        db_add_alert(cle + ("_requalifie" if requalif else ""), niveau, vmid=vmid,
                     node_id=_node_de(vmid), kind=_SENS_KIND.get(sens, "agent"), params=_p)
    else:
        if requalif:
            depuis = (f"flux en panne depuis le {_horodate_fr(since)}, soit {_duree_fr(age)} "
                      f"(la nature du défaut a changé)")
        else:
            depuis = f"flux en panne depuis le {_horodate_fr(since)}"
        # `kind` interne (rx_error/rx_stall/tx_stall/agent_unreachable) ≠ vocabulaire d'alerte : le
        # SENS suffit à classer l'incident (réception, émission, ou joignabilité du conteneur), le
        # détail de la nature reste dans le message.
        db_add_alert(f"{ident} : {detail} — {depuis}.", niveau, vmid=vmid, node_id=_node_de(vmid),
                     kind=_SENS_KIND.get(sens, "agent"))
    _flush_pannes(force=True)                  # transition rare : on la grave tout de suite
    return True


def _flux_ok(vmid, sens, idx, cle=None, params=None):
    """Observation SAINE d'un flux. Après FLUX_RESOLVE_POLLS observations saines consécutives, clôt
    l'épisode et émet le message de RÉTABLISSEMENT avec la durée réelle. L'hystérésis empêche un
    blip de refermer (puis réarmer) l'épisode.

    `cle` (optionnel) : clé i18n complète de l'appelant — sans elle, repli sur le texte FR historique
    (`quoi` recalculé ici selon `sens`)."""
    global _pannes_dirty
    k = (vmid, sens, idx)
    with _pannes_lock:
        ouvert = k in _pannes
    if not ouvert:
        _flux_ok_cnt.pop(k, None)
        return False
    n = _flux_ok_cnt.get(k, 0) + 1
    _flux_ok_cnt[k] = n
    if n < FLUX_RESOLVE_POLLS:
        return False
    with _pannes_lock:
        ep = _pannes.pop(k, None)
        _pannes_dirty = True
    _flux_ok_cnt.pop(k, None)
    if ep:
        if cle:
            _p = dict(params or {})
            _p.setdefault("ident", ep.get("ident"))
            _p["duree"] = _duree_fr(time.time() - ep["since"])
            _p["date"] = _horodate_fr(ep["since"])
            db_add_alert(cle, "info", vmid=vmid, node_id=_node_de(vmid),
                         kind=_SENS_KIND.get(sens, "agent"), params=_p)
        else:
            quoi = "à nouveau joignable" if sens == "agent" else "flux rétabli"
            db_add_alert(f"{ep.get('ident')} : {quoi} après {_duree_fr(time.time() - ep['since'])} "
                         f"de panne (détectée le {_horodate_fr(ep['since'])}).", "info",
                         vmid=vmid, node_id=_node_de(vmid),
                         kind=_SENS_KIND.get(sens, "agent"))
        _flush_pannes(force=True)
    return True


def panne_en_cours(vmid, sens, idx):
    """Épisode ouvert pour ce flux (ou None) — lecture seule, pour l'UI/diagnostic."""
    with _pannes_lock:
        ep = _pannes.get((vmid, sens, idx))
        return dict(ep) if ep else None


# Rechargement au boot : l'import de `app.metrics` a lieu au démarrage de l'orchestrateur, donc
# l'état est en place AVANT le premier tick de surveillance (pas d'alerte fantôme au réveil).
try:
    load_persisted()
except Exception as e:                                       # jamais bloquant, jamais muet
    log.warning("Rechargement des épisodes de panne de flux 2110 échoué : %s — départ à vide.", e)

# Alarmes présence signal (audit A5) : noir/gel/silence (≥ 0.37.0) + hors-gamut/loudness (≥ 0.39.9)
# remontés par le moteur 2110_io
# (champ `signal` des entrées receivers[]/senders[] de :8080). Alerte à TRANSITION (pattern membw) :
# warning à l'apparition, info au retour. Clé (vmid, sens, idx, type) ; absence de champ = inconnu
# (moteur ancien, slot idle) → n'alerte ni ne résout.
_signal_prev = {}   # (vmid, "rx"|"tx", idx, "black"|"frozen"|"silence") → bool
_signal_cnt  = {}   # même clé → nb d'observations CONSÉCUTIVES contredisant l'état retenu
# ⚠ `loud` N'EST PLUS UNE ALARME (2026-07-28). Le moteur publie un loudness MOMENTANÉ (~400 ms,
# L/R seulement) comparé à une cible ±2 LU — or la conformité R128 se juge sur l'INTÉGRÉ d'un
# PROGRAMME, qui a un début et une fin. N'importe quelle source vivante sort de ±2 LU en permanence.
# Le moteur continue de publier `lufs` (mesure brute) ; c'est l'alarme qui disparaît, en attendant
# un plugin de mesure dédié qu'on arme au début d'un programme et qu'on coupe à la fin.
_SIGNAL_LABELS = {"black": "image noire", "frozen": "image figée", "silence": "silence audio",
                  "clip": "saturation audio", "gamut": "image hors gamut",
                  "tx_late": "trames TX en retard"}
# Débounce des DEUX bords (≈ 15 s au tick de 5 s). Une présence signal oscille par nature — source
# à la limite du seuil, fondu, mire quasi statique : sans confirmation, chaque bascule partait en
# notification. Vécu le 2026-07-26 : « signal rétabli » puis « image figée détectée » à 6 s d'écart,
# donc deux mails pour un flux qui n'avait pas changé d'état. Le pendant de FLUX_RESOLVE_POLLS, qui
# protège déjà la clôture des épisodes de panne — mais ici les DEUX sens comptent.
SIGNAL_CONFIRM_POLLS = 3
# ★ LIMITEUR PAR ÉPISODE (2026-07-27). Le débounce ci-dessus ne suffit PAS, et le durcir ne ferait
# que déplacer le seuil : mesuré sur Horace, un slot (Rx #5) a produit 376 alertes en 19 h — 188
# « image figée détectée » + 188 « signal rétabli » — avec un intervalle MÉDIAN de 17 s, soit juste
# au-dessus des 15 s de confirmation. Aucun réglage de débounce ne rattrape ça, parce que le
# problème n'est pas la détection (le gel est peut-être bien réel) mais la RÉPONSE : une source qui
# bascule sans cesse est UN incident — « cette source est instable » — pas 376.
# Règle : au-delà de SIGNAL_FLAP_MAX transitions confirmées dans SIGNAL_FLAP_WINDOW_S, on émet UNE
# alerte « instable » et on SE TAIT sur ce slot ; le silence se lève après SIGNAL_STABLE_S d'état
# réellement stable, avec une alerte de sortie. L'exploitant garde l'information (la source
# clignote) et récupère un fil lisible. Cf. [alarm-must-compare-to-intent] règle 2.
SIGNAL_FLAP_MAX      = 4      # transitions confirmées… (réglage signal_flap_max)
SIGNAL_FLAP_WINDOW_S = 600.0  # …dans cette fenêtre glissante ⇒ slot déclaré INSTABLE
SIGNAL_STABLE_S      = 900.0  # durée d'état stable qui lève le silence (réglage signal_stable_s)
_signal_flaps = {}   # clé → [instants (monotonic) des transitions confirmées, fenêtre glissante]
_signal_mute  = {}   # clé → instant de la dernière transition tant que le slot est déclaré instable
# Épisode PERSISTÉ : un slot instable le reste au travers d'un redémarrage du service — sans ça, le
# silence se lèverait à chaque boot et la rafale repartirait (cf. app/episodes.py).
_episodes_signal = _Episodes("signal_instable")


# ─── Trames TX en retard (audit A5, 2026-07-28) ────────────────────────────────────────────────
# Le moteur publie déjà sur chaque sender vidéo un compteur CUMULATIF `late` (plugins/2110_io/
# mtl_rx.c : « TX vidéo : trames en retard (get_frame > 1,5 période = epoch raté) », lu par
# `_read_tx_stats`/controller.py) — mais jusqu'ici personne ne le consommait. Le compteur ne fait
# que croître : une alarme sur sa VALEUR ABSOLUE resterait vraie pour toujours dès la première
# trame en retard. Ce qui compte, c'est le DELTA entre deux relevés (fenêtre = un tick de
# `surveillance`, ~CHECK_INTERVAL). Un delta NÉGATIF ne peut venir que d'un redémarrage du moteur
# (compteur remis à zéro) : ce n'est pas « moins de retard », c'est un nouveau départ — on réarme
# la référence SANS alerter, plutôt que de rapporter un delta négatif absurde.
TX_LATE_ALARM_THRESHOLD = int(os.environ.get("TX_LATE_ALARM_THRESHOLD", "1"))
# MAINTIEN du drapeau après le dernier raté. Décision d'exploitation (2026-07-28) : « dès que
# quelque chose est raté, c'est un incident » — donc seuil à 1. Mais un ÉVÉNEMENT rare ne peut pas
# être signalé par un drapeau vrai seulement pendant la fenêtre où il tombe : `_check_signal` exige
# 3 relevés concordants avant d'alerter, et le limiteur d'instabilité déclarerait la sortie
# « INSTABLE » puis se tairait. Mesuré sur Horace : un mur qui rate 6 epochs/minute fait basculer
# un drapeau par fenêtre environ une fois sur deux — exactement le profil qui bat.
# On tient donc le drapeau HOLD secondes après le dernier raté : une sortie qui rate régulièrement
# reste franchement en défaut, une sortie guérie se referme d'elle-même. Même raisonnement que le
# drapeau `clip` côté audio.
TX_LATE_HOLD_S = float(os.environ.get("TX_LATE_HOLD_S") or 60.0)
_tx_late_seen = {}   # (vmid, idx) → instant monotone du dernier raté constaté
# ↑ nb de trames en retard sur une fenêtre de poll qui arme le drapeau `tx_late`. Défaut à 1 :
# une seule epoch ratée sur un slot TX veut déjà dire que le producteur câblé n'a pas fourni son
# image à temps — pas la peine d'en accumuler plusieurs pour que ce soit un défaut. Le bruit d'un
# décrochage occasionnel est déjà filtré en aval par SIGNAL_CONFIRM_POLLS (débounce) et
# SIGNAL_FLAP_MAX (limiteur d'instabilité) ; inutile d'empiler un second seuil ici.
_tx_late_prev = {}   # (vmid, idx) → dernière valeur CUMULATIVE de `late` relevée sur ce slot TX


def _tx_late_delta(vmid, idx, late_cur):
    """Delta du compteur cumulatif `late` depuis le relevé précédent.

    None si la valeur est absente (moteur trop ancien pour la publier) — distinct de 0, pour que
    l'appelant n'ajoute PAS le drapeau `tx_late` au dict `signal` plutôt que d'y mettre un `False`
    qui prétendrait « mesuré, rien à signaler »."""
    if late_cur is None:
        return None
    try:
        late_cur = int(late_cur)
    except (TypeError, ValueError):
        return None
    key = (vmid, idx)
    prev = _tx_late_prev.get(key)
    _tx_late_prev[key] = late_cur
    if prev is None or late_cur < prev:
        return 0     # 1ʳᵉ observation, ou compteur remis à zéro (redémarrage moteur) : pas d'alerte
    return late_cur - prev


def _signal_alerts_enabled():
    from .database import db_get_setting
    v = db_get_setting("signal_alerts_enabled", "1")
    return str(v).strip().lower() not in ("0", "false", "off", "non", "")


# Niveau d'attention PAR SOURCE (cf. io2110_flows.ALARMES_DEFAUT) : quels drapeaux de présence
# signal ce slot a le droit de faire remonter. Caché 30 s par conteneur — `_check_signal` tourne
# pour ~25 slots toutes les 5 s, relire et parser le deploy_config à chaque fois serait absurde.
# Contrepartie assumée : un changement de réglage prend effet au plus tard 30 s après.
_ALARMES_TTL_S = 30.0
_alarmes_cache = {}   # (vmid, sens) → (instant, {(essence, idx): {drapeau: bool}})


def alarmes_slot(vmid, sens, idx, essence="video"):
    """Alias public de `_alarmes_slot` : quels drapeaux de présence signal ce slot a le droit de
    faire remonter. Consommé hors du module (monitors BCP-008), où il vaut mieux dépendre d'un nom
    stable que d'un symbole privé."""
    return _alarmes_slot(vmid, sens, idx, essence)


def _alarmes_slot(vmid, sens, idx, essence="video"):
    """Drapeaux autorisés pour ce slot. Défauts de `io2110_flows` si le conteneur n'est pas un
    moteur 2110 ou si sa configuration est illisible — jamais de silence par accident."""
    from .io2110_flows import ALARMES_DEFAUT, NIVEAU_DEFAUT
    cle = (vmid, sens)
    now = time.monotonic()
    hit = _alarmes_cache.get(cle)
    if hit is None or now - hit[0] > _ALARMES_TTL_S:
        table = {}
        try:
            import json as _json
            from . import io2110_flows as _iof
            c = db_get_container(vmid) or {}
            dc = c.get("deploy_config")
            dc = _json.loads(dc) if isinstance(dc, str) else (dc or {})
            if (dc.get("type") or "") == "2110_io":
                table = _iof.alarmes_par_slot(dc.get("params") or {}, sens)
        except Exception as e:
            log.debug("alarmes par source %s/%s : %s", vmid, sens, e)
            table = {}
        _alarmes_cache[cle] = (now, table)
        hit = _alarmes_cache[cle]
    return hit[1].get((essence, idx)) or {"drapeaux": dict(ALARMES_DEFAUT),
                                          "niveau": NIVEAU_DEFAUT}


def _signal_cfg(cle, defaut):
    from .database import db_get_setting
    try:
        v = db_get_setting(cle, defaut)
        return type(defaut)(v) if v not in (None, "") else defaut
    except (TypeError, ValueError):
        return defaut


def _signal_bascule_muette(key, hn, lab, idx, txt, vmid):
    """Enregistre une transition confirmée et dit s'il faut TAIRE cette bascule.

    True = le slot est déclaré instable (alerte unique déjà émise) → l'appelant ne notifie pas la
    bascule. False = régime normal, l'appelant alerte comme avant. Cf. le bloc SIGNAL_FLAP_MAX."""
    maxi = _signal_cfg("signal_flap_max", SIGNAL_FLAP_MAX)
    fen  = _signal_cfg("signal_flap_window_s", SIGNAL_FLAP_WINDOW_S)
    now = time.monotonic()
    # Reprise après redémarrage : un slot déjà déclaré instable le reste (l'état vit en RAM, or
    # l'instabilité de la source ne cesse pas parce que l'orchestrateur redémarre).
    if key not in _signal_mute and _episodes_signal.get(key):
        _signal_mute[key] = now
    if key in _signal_mute:
        _signal_mute[key] = now             # l'instabilité continue : on repousse la sortie
        return True
    ts = [t for t in _signal_flaps.get(key, ()) if now - t < fen]
    ts.append(now)
    _signal_flaps[key] = ts
    if len(ts) < maxi:
        return False
    _signal_mute[key] = now
    _signal_flaps.pop(key, None)
    _episodes_signal.poser(key, True)
    if _signal_alerts_enabled():
        db_add_alert("alert.signal.instable", "warning",
                     vmid=vmid, node_id=_node_de(vmid), kind="signal",
                     params={"h": hn, "lab": lab, "idx": int(idx) + 1, "count": len(ts),
                             "txt": txt, "fen_min": int(fen / 60),
                             "stable_min": int(_signal_cfg("signal_stable_s", SIGNAL_STABLE_S) / 60)})
    return True


def _signal_purger_mutes():
    """Lève le silence des slots redevenus STABLES (aucune transition confirmée depuis
    `signal_stable_s`) et émet l'alerte de sortie. Appelée à chaque tour de `surveillance` :
    sans ça, un slot stabilisé resterait muet pour toujours — un silence qui ne se lève jamais est
    une alarme perdue, pas une alarme calme."""
    stable = _signal_cfg("signal_stable_s", SIGNAL_STABLE_S)
    now = time.monotonic()
    for key, dernier in list(_signal_mute.items()):
        if now - dernier < stable:
            continue
        _signal_mute.pop(key, None)
        _episodes_signal.retirer(key)
        try:
            vmid, sens, idx, typ = key
        except (TypeError, ValueError):
            continue
        if _signal_alerts_enabled():
            lab = "Rx" if sens == "rx" else "TX"
            hn = (db_get_container(vmid) or {}).get("hostname") or vmid
            db_add_alert("alert.signal.stabilisee", "info",
                         vmid=vmid, node_id=_node_de(vmid), kind="signal",
                         params={"h": hn, "lab": lab, "idx": int(idx) + 1,
                                 "txt": _SIGNAL_LABELS.get(typ, typ), "stable_min": int(stable / 60)})


# PRESSION MÉMOIRE D'UN CONTENEUR — seuils sur `memory.current / memory.max` du cgroup.
# 85 % = il reste de la marge mais la tendance mérite un œil ; 95 % = le noyau a commencé à
# récupérer les pages du conteneur pour tenir sous le plafond.
MEM_PRESSION_WARN_PCT   = 85
MEM_PRESSION_ERREUR_PCT = 95


def _verifier_pression_memoire(vmid, hn, s):
    """Alerte quand un conteneur approche SON PLAFOND mémoire (`memory.max`), pas celui du nœud.

    POURQUOI (incident Horace, 2026-08-19) : trois shards du tissu ont accumulé ~2 Gio de mémoire
    NOYAU (des descripteurs de fichiers fuités, comptés dans le `slab` du cgroup) jusqu'à toucher
    leur `memory.max`. Le noyau ne tue pas dans ce cas : il RÉCUPÈRE les pages utiles du conteneur
    et les envoie en swap. La boucle de composition est alors passée de 13 ms à 7,2 s par trame et
    le mur est tombé à 0,6 fps. Pendant les 40 heures de montée, RIEN n'a alerté : le nœud avait
    160 Gio libres et 71 % de CPU au repos, donc toutes nos sondes de nœud étaient au vert. La
    seule grandeur qui montrait quoi que ce soit était ce rapport-là, et personne ne le lisait —
    l'agent publiait pourtant `mem_limit` à côté de `mem_used` depuis toujours.

    ⚠ On compare à l'INTENTION (le plafond alloué à CE conteneur), jamais à la RAM du nœud : c'est
    la même erreur que de juger une cadence sans connaître sa cible. Un nœud à moitié vide peut
    héberger un conteneur en train d'étouffer.
    ⚠ `memory.current` inclut la mémoire NOYAU (slab, tables de pages). C'est voulu : la fuite de
    l'incident était entièrement là, et un contrôle qui ne regarderait que l'`anon` l'aurait ratée.
    """
    used, lim = s.get("mem_used"), s.get("mem_limit")
    try:
        used, lim = int(used or 0), int(lim or 0)
    except (TypeError, ValueError):
        return
    if used <= 0 or lim <= 0:      # `mem_limit` 0 = aucun plafond posé → rien à comparer
        return
    pct = 100.0 * used / lim
    if pct < MEM_PRESSION_WARN_PCT:
        return
    grave = pct >= MEM_PRESSION_ERREUR_PCT
    db_add_alert(
        "alert.resource.pression_grave" if grave else "alert.resource.pression_attention",
        "error" if grave else "warning",
        vmid=vmid, node_id=_node_de(vmid), kind="resource",
        params={"h": hn or vmid, "used_gio": used / 2**30, "lim_gio": lim / 2**30, "pct": pct})


def _verifier_gpu_effectif(vmid, hn, m, node_id):
    """Un conteneur à QUI UNE CARTE EST RÉSERVÉE s'en sert-il vraiment ?

    POURQUOI (incident Horace, 2026-08-28) : le conteneur de monitoring d'un utilisateur avait un
    GPU alloué, ses `/dev/nvidia*` en place, et pourtant NVML cassé à l'intérieur — un
    `systemctl daemon-reload` sur le nœud avait révoqué l'accès aux conteneurs DÉJÀ lancés. ffmpeg
    mourait sur « Invalid argument (-22) » et était relancé sans fin : `pushed_fps` 0,7 pour
    `in_fps_seen` 50,8, **pendant des jours**, sans une alerte — le conteneur était « running » et
    sa cadence non tenue passait pour un moniteur en veille.

    ⚠ ON COMPARE À L'INTENTION, et l'intention est la RÉSERVATION en base (`node_gpu_alloc`), pas
    le manifeste. Un plugin en `encoder: auto` sur un nœud sans carte DOIT tourner en logiciel :
    c'est le comportement correct, il n'y a rien à signaler. Ce qui est anormal, c'est de tenir une
    carte réservée et de ne pas s'en servir — quelqu'un paie ce GPU en le croyant utilisé, et le
    calcul de capacité compte des cœurs économisés qui ne le sont pas.

    Chaque plugin le dit à sa façon ; on lit ce qu'ils publient, sans rien deviner.
    """
    if not isinstance(m, dict) or node_id is None:
        return
    try:
        from .gpu_pool import gpu_par_vmid
        if vmid not in (gpu_par_vmid(node_id) or {}):
            return                      # aucune carte réservée → un repli logiciel est NORMAL
    except Exception:
        return
    if m.get("encoder_replie"):
        motif = ("l'encodeur matériel a été déclassé en logiciel EN COURS DE ROUTE — la carte a "
                 "très probablement été révoquée sous le conteneur")
    elif m.get("encoder") == "cpu" and m.get("encoder_demande") in ("auto", "nvenc"):
        motif = "encodage LOGICIEL alors que le matériel est demandé (%s)" % m.get("encoder_demande")
    elif m.get("gpu") is False:
        motif = "composition sur CPU"
    else:
        return
    db_add_alert(
        "%s : une carte GPU lui est RÉSERVÉE mais il ne s'en sert pas — %s. Recréer le conteneur "
        "réinjecte les périphériques (`docker start` ne suffit pas : ils sont posés à la CRÉATION)."
        % (hn or vmid, motif),
        "warning", vmid=vmid, node_id=node_id, kind="resource")


def _check_signal(vmid, hn, sens, idx, sig):
    """Compare l'état signal d'un slot (dict du moteur) au précédent et alerte aux transitions."""
    if not isinstance(sig, dict) or idx is None:
        return
    lab = "Rx" if sens == "rx" else "TX"
    # Niveau d'attention DE CETTE SOURCE : un gel n'est un incident que si elle est censée bouger.
    # On filtre AVANT la machine à états — un drapeau non surveillé ne doit ni alerter, ni consommer
    # de débounce, ni compter pour le limiteur d'instabilité : sinon une mire fixe déclarerait
    # « source INSTABLE » sans qu'aucune de ses bascules n'ait jamais été notifiée.
    _cfg_src = _alarmes_slot(vmid, sens, idx)
    autorisees, _niv = _cfg_src["drapeaux"], _cfg_src["niveau"]
    for typ, txt in _SIGNAL_LABELS.items():
        if typ not in sig or not autorisees.get(typ, True):
            continue
        cur = bool(sig[typ])
        key = (vmid, sens, idx, typ)
        prev = _signal_prev.get(key)
        if cur == prev:
            _signal_cnt.pop(key, None)      # état stable : le débounce repart à zéro
            continue
        n = _signal_cnt.get(key, 0) + 1
        _signal_cnt[key] = n
        if n < SIGNAL_CONFIRM_POLLS:
            continue                        # bascule pas encore CONFIRMÉE — on ne notifie rien
        _signal_cnt.pop(key, None)
        # Transition CONFIRMÉE : elle compte pour le limiteur d'instabilité, qu'on la notifie ou non.
        if _signal_bascule_muette(key, hn, lab, idx, txt, vmid):
            _signal_prev[key] = cur
            continue                        # slot déclaré instable → on ne notifie plus ses bascules
        if _signal_alerts_enabled() and (cur or prev is True):
            # `prev is None` et retour à la normale = 1ʳᵉ observation : rien à « rétablir ».
            if _niv == "log":
                # « Journal seul » : n'entre PAS dans le fil d'alertes — donc ni notification, ni
                # ligne consommée sur la rétention. La trace reste, au niveau technique.
                _msg = ((f"{hn} {lab} #{int(idx) + 1} : {txt} détectée" if typ != "silence"
                         else f"{hn} {lab} #{int(idx) + 1} : silence audio détecté") if cur
                        else f"{hn} {lab} #{int(idx) + 1} : signal rétabli ({txt})")
                log.info("signal: %s", _msg)
            else:
                # Le niveau de la SOURCE porte la détection ; un retour à la normale reste `info`
                # (une résolution n'est jamais une erreur, quel que soit le réglage de la source).
                _p = {"h": hn, "lab": lab, "idx": int(idx) + 1, "txt": txt}
                if not cur:
                    _cle = "alert.signal.retabli"
                elif typ == "silence":
                    _cle = "alert.signal.silence_detecte"
                else:
                    _cle = "alert.signal.detecte"
                db_add_alert(_cle, _niv if cur else "info",
                             vmid=vmid, node_id=_node_de(vmid), kind="signal", params=_p)
        _signal_prev[key] = cur


# Cache latence de RÉCEPTION par container 2110_io : {vmid: {shm_name: ms}}.
# Segment A = capture média (PTP/TAI) → écriture shm (réseau + framebuffers MTL + de-jitter).
# Distinct de latency_cache (segment B = écriture shm → lecteur). Renseigné depuis
# receivers[].rx_latency_ms de :8080 ; consommé par la topologie (page Câbles).
rx_latency_cache = {}

# Cache fps PAR FLUX des moteurs multi-flux (2110_io) : {vmid: {shm_name: fps}}.
# Un fps unique agrégé n'a pas de sens sur un moteur RX multi-session → la topologie
# (page Câbles) masque le badge fps de carte (aggregate_fps:false) et affiche à la place
# le fps réel de chaque port de sortie. Renseigné depuis receivers[].fps de :8080.
# Cadence de CONTENU NEUF par vmid (None = le plugin ne la publie pas). À afficher À CÔTÉ de
# `fps`, jamais à la place : l'écart entre les deux EST le diagnostic (cf. rafraichir_metrics).
fps_content_cache = {}

# PLANCHER GLISSANT de cadence, par vmid : le minimum observé sur la dernière fenêtre. Sert aux
# avertissements de l'interface, qui ne doivent pas CLIGNOTER. La cadence d'un shard oscille
# naturellement de quelques images (mesuré : 42 à 48 sur un mur à 50) ; comparer la valeur
# INSTANTANÉE à un seuil fait donc apparaître et disparaître l'avertissement d'un rafraîchissement
# à l'autre — l'opérateur croit à un défaut d'affichage et cesse d'y prêter attention. Le plancher,
# lui, répond à « ce maillon a-t-il décroché récemment ? », qui est la vraie question.
_fps_fenetre = {}          # (canal, vmid) → [(t, valeur), …] sur _FPS_FENETRE_S
_FPS_FENETRE_S = 30.0

def fps_note(vmid, valeur, canal="fps"):
    """Enregistre une mesure dans la fenêtre glissante (ignorée si non numérique)."""
    try:
        f = float(valeur)
    except (TypeError, ValueError):
        return
    import time as _t
    now = _t.monotonic()
    d = _fps_fenetre.setdefault((canal, vmid), [])
    d.append((now, f))
    coupe = now - _FPS_FENETRE_S
    while d and d[0][0] < coupe:
        d.pop(0)

def fps_plancher(vmid, canal="fps"):
    """Valeur la plus BASSE observée sur la fenêtre, ou None si rien de récent."""
    import time as _t
    d = _fps_fenetre.get((canal, vmid)) or []
    coupe = _t.monotonic() - _FPS_FENETRE_S
    vals = [f for (t, f) in d if t >= coupe]
    return min(vals) if vals else None

def fps_pic(vmid, canal="fps"):
    """Valeur la plus HAUTE observée sur la fenêtre. Sert aux compteurs de PERTE : une trame
    perdue il y a vingt secondes reste une trame perdue, et doit rester visible."""
    import time as _t
    d = _fps_fenetre.get((canal, vmid)) or []
    coupe = _t.monotonic() - _FPS_FENETRE_S
    vals = [f for (t, f) in d if t >= coupe]
    return max(vals) if vals else None

# ─── Cadence PRÉCISE : mesurée ICI, pas dans le plugin ───────────────────────────────────────
# Le champ `fps` d'un plugin est un débit glissant sur ~1 s (compteur de trames / durée de la
# fenêtre, arrondi au dixième). La troncature vaut ±1 trame par fenêtre, donc **±1 fps** : on
# publiait « 49,8 » là où la mesure ne sait dire que « 50 ± 1 ». Une décimale affichée PRÉTEND
# être significative — l'exploitant lit un défaut là où il n'y en a pas, et un chiffre qui bouge
# tout le temps finit par n'être plus regardé du tout (même raison que le plancher glissant).
#
# Élargir la fenêtre DANS les plugins aurait été le réflexe, et c'est un piège : une fenêtre plus
# longue retarde d'autant la chute à 0 quand une source gèle — précisément ce que la règle du
# débit glissant a été écrite pour empêcher. Or l'orchestrateur dispose déjà des deux termes
# EXACTS : le compteur cumulé `frame_index` et l'horloge de son propre poll (~5 s). Δindex / Δt
# divise le bruit par cinq sans toucher à un seul plugin et sans rien retarder.
_fps_precis_prev = {}    # vmid → (t_monotonic, frame_index) du poll précédent
fps_precis_cache = {}    # vmid → cadence lissée (float), ou None si non calculable
_FPS_PRECIS_DT_MIN = 2.0    # sous 2 s, Δindex est trop court pour gagner quoi que ce soit
_FPS_PRECIS_DT_MAX = 30.0   # au-delà, l'échantillon précédent est périmé (polls manqués)


def frame_index_de(m):
    """`frame_index` vidéo d'un bloc de métriques :8080, quelle que soit sa forme (top-level,
    receivers[], senders[]). Extrait ici parce que DEUX chemins en ont besoin — la cadence
    précise (tôt dans le poll) et le cache de write-pointer (plus tard)."""
    if isinstance(m.get("frame_index"), int):
        return m["frame_index"]
    for cle in ("receivers", "senders"):
        lst = m.get(cle)
        if not isinstance(lst, list):
            continue
        for rec in lst:
            if not isinstance(rec, dict):
                continue
            if cle == "receivers" and rec.get("essence") not in (None, "video"):
                continue
            if rec.get("frame_index") is not None:
                try:
                    return int(rec["frame_index"])
                except (TypeError, ValueError):
                    return None
    return None


def fps_precis_note(vmid, fi):
    """Recalcule la cadence par Δframe_index / Δt entre deux polls. Rend la valeur (ou None).

    Trois cas rendent None plutôt qu'un chiffre douteux — un capteur qui se tait vaut mieux qu'un
    capteur qui invente : premier échantillon, compteur REPARTI EN ARRIÈRE (script redéployé,
    format changé), et échantillon précédent périmé."""
    import time as _t
    if fi is None:
        _fps_precis_prev.pop(vmid, None)
        fps_precis_cache.pop(vmid, None)
        return None
    now = _t.monotonic()
    prev = _fps_precis_prev.get(vmid)
    _fps_precis_prev[vmid] = (now, fi)
    if prev is None:
        fps_precis_cache.pop(vmid, None)
        return None
    t0, fi0 = prev
    dt = now - t0
    if fi < fi0 or dt < _FPS_PRECIS_DT_MIN or dt > _FPS_PRECIS_DT_MAX:
        fps_precis_cache.pop(vmid, None)
        return None
    v = round((fi - fi0) / dt, 1)
    fps_precis_cache[vmid] = v
    return v


rx_fps_cache = {}

# Cache d'ALIMENTATION par flux, toutes essences : {vmid: {shm_name: {essence, fps, mode}}}.
#
# Pourquoi il ne suffisait pas de réutiliser rx_fps_cache : celui-ci écarte délibérément les
# essences non-vidéo, donc les ports audio et ANC d'un moteur multi-flux n'avaient AUCUN état
# live. La page Câbles les offrait au câblage exactement comme les ports vidéo, alors qu'ils ne
# transportaient rien — et rien ne le disait. On peut vouloir PRÉ-CÂBLER un port pas encore
# alimenté (décision produit), donc on ne les masque pas : on dit lesquels sont servis.
#
# Un shm ABSENT de la table d'un vmid qui en publie une = ce port n'est pas servi par son moteur.
# L'absence de table (plugin qui ne publie pas receivers[]) ne prouve RIEN et ne doit jamais se
# lire comme « non alimenté » — d'où le tri-état côté topologie.
rx_served_cache = {}

# Essence publiée par :8080 → suffixe du nom de shm, tel que déclaré dans wiring.produces du
# moteur (`{hostname}_{i}`, `{hostname}_audio_{i}`, `{hostname}_anc_{i}`). Le moteur nomme l'ANC
# « anc » là où le manifeste parle d'essence « data » : les deux mènent au même suffixe.
_RX_SHM_SUFFIXE = {"video": "_", "audio": "_audio_", "anc": "_anc_", "data": "_anc_"}

# Modes de slot qui ne correspondent à AUCUNE session : le slot est déclaré, rien ne tourne
# derrière. Tout autre mode (mtl, error…) signifie qu'une session existe.
_RX_MODES_INACTIFS = {"idle", "", "off", "none"}

# Cache calage A/V par container (streamer) : {vmid: {applied, live, drift}} en ms.
# Renseigné depuis les clés av_*_ms de :8080 ; consommé par la topologie (page Câbles).
av_sync_cache = {}

# Cache d'alignement multi-entrées (mélangeur/DVE) : {vmid: {skew:{shm:ms}, late:[shm], sync_ref, budget}}.
# Renseigné depuis inputs_skew_ms/inputs_late/sync_ref/align_budget de :8080 ; consommé par la topologie.
align_cache = {}

# Cache retard par entrée (multiview input-locked) : {vmid: {shm: nb d'images de décalage}} (0 = synchrone).
# Renseigné depuis inputs_lag_frames de :8080 ; consommé par la topologie (badge « +N img » page Câbles).
inputs_lag_cache = {}

# Cache frame_index par container : {vmid: int} — write pointer cumulatif du pipeline shm.
# Utilisé par /api/mxl/pipeline pour calculer le débit grains/s côté front.
frame_index_cache = {}

# Valeur frame_index au poll précédent : permet de détecter si le shm avance ou est figé.
_frame_index_prev = {}  # vmid → int

# Activité shm par container : True si le frame_index a progressé depuis le dernier poll.
# Consommé par la topologie (page Câbles) et la page Monitoring.
shm_active_cache = {}  # vmid → bool

# Nombre de CPUs alloués signalé par l'agent /stats (compute Docker uniquement).
# Consommé par /api/containers pour l'affichage de la barre CPU.
cpu_count_cache = {}  # vmid → int

# Monitoring pyramide (P2/P3/P4) : renseignés depuis :8080 des consommateurs (proxy_usage/read/needs)
# et des pyramides (sources). Consommés par pyramide_overview() (console + KPI + alertes).
proxy_usage_cache = {}   # vmid(multiview) → {idx: {src,read,cost,kind}}
proxy_read_cache  = {}   # vmid(multiview) → [noms de shm proxy réellement lus]
proxy_needs_cache = {}   # vmid(multiview) → {src: [[w,h,count]…]}
pyr_sources_cache = {}   # vmid(pyramide) → {slot: {shm,fps,frame_index,latency_ms,proxies:[…]}}

# ─── Cadence tenue vs cadence CIBLE (le détecteur le plus honnête qui soit) ──────────────────
# On COLLECTE déjà le fps de chaque conteneur (colonne containers.fps) et sa cible est en base
# (deploy_config.params.fps) — mais personne ne comparait les deux : un mur pouvait rester des
# heures sous sa cadence sans qu'aucune alerte ne se déclenche (dl360-1, 2026-07-14 : famine CPU,
# 0,5 fps pour 50 attendus, tableau de bord au vert). Alerte par TRANSITION, avec l'observé vs la
# cible. Pièges évités : (1) le fps est une fenêtre glissante → délai de grâce après (re)démarrage,
# sinon la montée en régime alerte à tort ; (2) un script arrêté ne publie plus de fps du tout
# (m["fps"] is None) → on n'alerte pas ici, les alertes « script arrêté / crash-loop » couvrent ce
# cas et on ne veut pas doubler ; (3) une seule alerte par épisode (état par vmid).
#
# ★ HYSTÉRÉSIS (2026-07-27) : la 1ʳᵉ version armait sous 90 % de la cible mais désarmait dès UN
# échantillon repassé au-dessus. Un conteneur qui oscille autour du seuil — le régime NORMAL d'un
# décrochage — produit alors un BATTEMENT : Horace, mur `multiview-vision` bloqué à 25-37 fps pour
# 50, a émis 29 alertes en une heure, « cadence NON TENUE » puis « cadence rétablie (45,6/50) »
# toutes les 3 minutes, sans que rien ne soit rétabli. Une alarme qui bat s'apprend par cœur et
# s'ignore : c'est ce maillon-là qui a échoué le 2026-07-27, pas la détection. On exige donc un
# retour FRANC (≥ 98 % de la cible) et SOUTENU pour clore l'épisode — exactement la règle déjà
# appliquée par `cpu_pressure._transition` (« une accalmie partielle ne résout rien »), qui avait
# reçu ce traitement au 1er essai alors que la cadence, elle, ne l'a jamais eu.
FPS_RATIO_MIN     = 0.90  # < 90 % de la cible = décroché (réglage fps_target_ratio)
FPS_LOW_SAMPLES   = 12    # ~60 s de sous-cadence CONTINUE à 5 s/tick (réglage fps_low_samples)
FPS_CLEAR_RATIO   = 0.98  # ≥ 98 % de la cible = vraiment rétabli (réglage fps_clear_ratio)
FPS_CLEAR_SAMPLES = 12    # ~60 s de cadence TENUE avant de clore l'épisode (fps_clear_samples)
FPS_GRACE_S       = 60.0  # pas d'armement dans la minute qui suit l'apparition/le redémarrage
_fps_seen_at  = {}  # vmid → time.monotonic() du 1er échantillon (ou du dernier redémarrage script)
_fps_low_cnt  = {}  # vmid → nb d'échantillons consécutifs sous la cible
_fps_ok_cnt   = {}  # vmid → nb d'échantillons consécutifs FRANCHEMENT au-dessus (sortie d'épisode)
_fps_alert    = {}  # vmid → True si l'alerte « sous-cadence » est posée (cache RAM du chemin chaud)
# Le MÊME état, SURVIVANT au redémarrage : une sous-cadence ne cesse pas parce que l'orchestrateur
# redémarre, et la ré-annoncer à chaque boot est l'autre moitié du spam (cf. app/episodes.py).
_episodes_fps = _Episodes("cadence")


# ─── Péremption de la cadence : une mesure NON PRISE n'est pas « la dernière connue » ─────────
# `get_metrics` avale l'exception et rend {"fps": None} quand :8080 ne répond pas — et l'appelant
# ne persistait alors RIEN (`if m.get("fps") is not None: db_update_fps(...)`). La colonne
# containers.fps gardait donc INDÉFINIMENT la dernière cadence vue : un conteneur injoignable depuis
# des heures s'affichait « running · 49,8 fps » (incident prod : deux multiviews morts, tableau de
# bord au vert, personne n'a rien vu). C'est un capteur qui ment — le même défaut de famille que le
# fps calculé avec un mauvais dénominateur.
#
# Règle : après FPS_STALE_POLLS polls CONSÉCUTIFS sans mesure exploitable, la colonne passe à NULL
# = INCONNU (l'UI affiche « ? fps », jamais un chiffre périmé). Une seule écriture par épisode.
# SEUIL RETENU : 3 polls ≈ 15 s (la boucle `surveillance` tourne à CHECK_INTERVAL = 5 s). Un seul
# poll raté est banal (timeout HTTP de 2 s, GC du script, tick décalé) ; trois d'affilée ne le sont
# pas. 15 s est aussi le budget en dessous duquel on ne peut de toute façon rien affirmer avec un
# capteur échantillonné à 5 s, et très en dessous de la durée d'un incident exploitant.
FPS_STALE_POLLS = 3
_fps_miss_cnt = {}   # vmid → nb de polls consécutifs sans mesure exploitable
_fps_perime   = {}   # vmid → True quand la colonne a DÉJÀ été mise à NULL (une écriture, pas 720/h)


def _fps_mesure(vmid, fps):
    """Mesure RÉUSSIE : on persiste et on réarme la péremption."""
    _fps_miss_cnt.pop(vmid, None)
    _fps_perime.pop(vmid, None)
    db_update_fps(vmid, fps)


def _fps_sans_mesure(vmid, hn=None):
    """Mesure IMPOSSIBLE (:8080 muet, ou réponse sans cadence). Au-delà du seuil, la cadence
    devient INCONNUE (NULL) au lieu de rester figée sur la dernière valeur connue."""
    n = _fps_miss_cnt.get(vmid, 0) + 1
    _fps_miss_cnt[vmid] = n
    if n < FPS_STALE_POLLS or _fps_perime.get(vmid):
        return False
    _fps_perime[vmid] = True
    db_update_fps(vmid, None)
    # La sous-cadence ne se juge pas sur une mesure absente : on désarme son compteur (sinon la
    # reprise déclencherait une fausse alerte « cadence non tenue » sur des échantillons fantômes).
    _fps_low_cnt[vmid] = 0
    log.info("Cadence de %s (%s) PÉRIMÉE après %d polls sans mesure : affichée INCONNUE "
             "(la dernière valeur connue n'est plus présentée comme courante).",
             hn or vmid, vmid, n)
    return True


# ─── « Injoignable » : un état de PREMIER ORDRE, distinct de running ──────────────────────────
# La détection `script_stopped` exige que l'agent RÉPONDE (`:8081/status` → running:false). Si
# l'agent est mort, on partait dans l'`except` et le statut restait `running` : le conteneur était
# affiché EN MARCHE alors que plus personne ne pouvait ni le mesurer ni le piloter. Trois situations
# doivent se distinguer, et c'est la deuxième qui manquait :
#   1. `running`      — le conteneur produit ET on le mesure (fps chiffré) ;
#   2. `unreachable`  — le conteneur tourne (docker inspect) mais on ne peut PAS le mesurer
#                       (agent :8081 muet pour un conteneur applicatif, contrôleur :8080 muet pour
#                       un moteur 2110) : cadence INCONNUE, pilotage impossible ;
#   3. `stopped`      — le conteneur est mort (posé par la boucle `surveillance` depuis docker).
# Deux seuils gradués, pour ne pas fabriquer de faux positif sur un conteneur qui vient d'être
# déployé (fenêtre normale où l'agent ne répond pas encore ; le moteur 2110, lui, peut passer 60-90 s
# en entraînement de lien E810 avant de servir :8080) :
#   - AGENT_KO_POLLS      → bascule du STATUT (honnête et sans bruit : on ne prétend rien savoir) ;
#   - AGENT_KO_ALERT_POLLS→ ouverture d'un ÉPISODE persisté + alerte `error` (le fait marquant).
# Garde supplémentaire : aucune bascule si une opération de cycle de vie est EN VOL sur le vmid
# (`vmlocks.est_verrouille`) — même précédent que l'auto-restart plus bas : le déploiement en cours
# EST l'explication.
AGENT_KO_POLLS       = 3     # ~15 s à 5 s/tick → statut `unreachable`
AGENT_KO_ALERT_POLLS = 24    # ~2 min → épisode + alerte (couvre déploiement et lien E810)
_agent_ko_cnt = {}           # vmid → nb de polls consécutifs sans réponse de l'agent/contrôleur


def _ident(vmid, c=None):
    hn = (c or {}).get("hostname") if isinstance(c, dict) else None
    return f"{hn} ({vmid})" if hn else f"Container {vmid}"


def _injoignable(vmid, c, quoi, detail, cle=None):
    """Un poll de plus sans réponse. Bascule le statut au-delà d'AGENT_KO_POLLS, ouvre l'épisode
    (persisté, alerte UNE fois) au-delà d'AGENT_KO_ALERT_POLLS. Renvoie True si le statut a basculé.

    `cle` (optionnel) : clé i18n propre à l'appelant (contrôleur :8080 / agent :8081 — le diagnostic
    diffère selon le point sourd, donc une clé par appelant plutôt qu'un `{detail}` générique)."""
    n = _agent_ko_cnt.get(vmid, 0) + 1
    _agent_ko_cnt[vmid] = n
    if n < AGENT_KO_POLLS:
        return False
    from .vmlocks import est_verrouille
    if est_verrouille(vmid):
        # Déploiement / redémarrage / destruction en vol : l'absence de réponse est ATTENDUE.
        log.debug("injoignable %s : opération lifecycle en vol — pas de bascule de statut", vmid)
        return False
    if vmid in _crash_quarantine:
        db_update_status(vmid, "crash_loop")   # diagnostic plus précis déjà posé : ne pas le masquer
    else:
        db_update_status(vmid, "unreachable")
    if n >= AGENT_KO_ALERT_POLLS:
        _flux_panne(vmid, "agent", None, "agent_unreachable", _ident(vmid, c),
                    f"{quoi} injoignable depuis {int(n * 5)} s — le conteneur est peut-être vivant, "
                    f"mais il n'est ni pilotable ni mesurable : sa cadence est INCONNUE (elle n'est "
                    f"plus affichée). {detail}", "error",
                    cle=cle, params={"secs": int(n * 5)})
    return True


def _joignable(vmid, cle=None):
    """Réponse obtenue : réarme le compteur et clôt l'épisode « injoignable » (avec hystérésis).
    `_flux_ok` doit être appelé à CHAQUE poll sain (il compte les observations consécutives avant de
    clore) — le sortir dès le premier laisserait l'épisode ouvert pour toujours."""
    _agent_ko_cnt.pop(vmid, None)
    _flux_ok(vmid, "agent", None, cle=cle)


# ─── Moteur 2110 « revenu VIDE » : sessions RX attendues vs actives ─────────────────────────
# Le détecteur de cadence (_check_cadence) NE COUVRE PAS les moteurs : la branche Docker MTL de
# rafraichir_metrics sort AVANT (return, cf. plus bas) — et de toute façon un moteur vide publie
# des slots "idle" à fps 0 sans qu'aucun `receivers[]` ne soit en défaut. Autrement dit, un moteur
# revenu à 0 session après un redéploiement était INDÉTECTABLE : tableau de bord au vert, murs
# gelés (banc 2026-07-14). On compare donc ce que l'orchestrateur SAIT devoir être abonné
# (abonnements IS-05 actifs, nmos.nb_sessions_rx_attendues) à ce que le moteur SERT réellement
# (receivers[] en mode "mtl"). Alerte par TRANSITION, avec délai de grâce (un moteur qui vient de
# démarrer monte ses sessions en quelques secondes) — complémentaire de la vérification
# post-resync de docker_driver.resync_moteur : ce détecteur-ci attrape AUSSI la perte de sessions
# survenue APRÈS le déploiement (contrôleur relancé sous nous, SDP perdus, repush partiel).
RX_MISSING_POLLS = 4      # ~20 s à 5 s/tick de manque CONTINU avant d'alerter
_rx_missing_cnt   = {}    # vmid → nb de polls consécutifs avec moins de sessions que d'abonnements
_rx_missing_alert = {}    # vmid → True si l'alerte « moteur vide » est posée (transition)


def _check_sessions_moteur(vmid, hn, receivers):
    """Sessions RX ACTIVES du moteur (mode "mtl") vs abonnements IS-05 actifs côté orchestrateur.
    Manque soutenu ⇒ alerte `error` (une par épisode) ; retour à l'équilibre ⇒ `info`."""
    try:
        from services import nmos as _nmos
        attendus = _nmos.nb_sessions_rx_attendues(vmid)
    except Exception:
        return
    if attendus <= 0:
        _rx_missing_cnt.pop(vmid, None)
        return
    actives = sum(1 for r in receivers
                  if r.get("essence") in (None, "video") and r.get("mode") == "mtl")
    if actives < attendus:
        _rx_missing_cnt[vmid] = _rx_missing_cnt.get(vmid, 0) + 1
        if _rx_missing_cnt[vmid] >= RX_MISSING_POLLS and not _rx_missing_alert.get(vmid):
            db_add_alert(
                "alert.rx.sessions_manquantes", "error",
                vmid=vmid, node_id=_node_de(vmid), kind="rx_stall",
                params={"h": hn, "vmid": vmid, "actives": actives, "attendus": attendus,
                        "manquants": attendus - actives})
            _rx_missing_alert[vmid] = True
    else:
        _rx_missing_cnt[vmid] = 0
        if _rx_missing_alert.get(vmid):
            db_add_alert("alert.rx.sessions_retablies", "info",
                         vmid=vmid, node_id=_node_de(vmid), kind="rx_stall",
                         params={"h": hn, "vmid": vmid, "actives": actives, "attendus": attendus})
            _rx_missing_alert[vmid] = False


def _cadence_cible(dc):
    """Cadence NOMINALE d'un conteneur, depuis son deploy_config (params.fps, ou video.fps pour le
    streamer), sinon le format vidéo par défaut du site. 0 si indéterminable."""
    p = (dc or {}).get("params") or {}
    for v in (p.get("fps"), (p.get("video") or {}).get("fps")):
        try:
            f = float(v or 0)
        except (TypeError, ValueError):
            f = 0.0
        if f > 0:
            return f
    try:
        from .docker_driver import _default_video_format
        return float(_default_video_format().get("fps") or 0)
    except Exception:
        return 0.0


cadence_cible = _cadence_cible   # alias PUBLIC : l'affichage doit lire la MÊME cible que l'alarme

# Le seuil de « cadence tenue » est un RÉGLAGE en base, et `cadence_etat` est appelée une fois par
# conteneur à chaque rafraîchissement de /api/containers (5 s, par client) : le lire à chaque appel
# ouvrirait une connexion SQLite par conteneur et par tour. Cache court — un réglage changé se voit
# en une minute, ce qui est très en deçà du temps qu'il faut pour aller le changer.
_ratio_tenue = [0.0, 0.0]        # [t_monotonic du relevé, valeur]
_RATIO_TTL_S = 60.0


def _ratio_cadence_tenue():
    """Seuil de clôture de l'alarme (`fps_clear_ratio`), au-dessus duquel la cadence est TENUE."""
    from .database import db_get_setting
    now = time.monotonic()
    if now - _ratio_tenue[0] < _RATIO_TTL_S and _ratio_tenue[1] > 0:
        return _ratio_tenue[1]
    try:
        v = float(db_get_setting("fps_clear_ratio", FPS_CLEAR_RATIO) or FPS_CLEAR_RATIO)
    except (TypeError, ValueError):
        v = FPS_CLEAR_RATIO
    _ratio_tenue[0], _ratio_tenue[1] = now, v
    return v


def cadence_etat(vmid, c, dc):
    """État de cadence PRÊT À AFFICHER : {cible, tenue, mesure}.

    Le badge et l'alarme doivent lire la même chose. C'est pourquoi le « tenue / pas tenue » se
    décide ICI, avec la cible de `_cadence_cible` et le seuil de clôture de l'alarme
    (`fps_clear_ratio`), et non côté navigateur avec un ratio recopié qui dériverait au premier
    réglage changé. Le front reçoit un verdict, pas de quoi en fabriquer un autre.

    `tenue` s'apprécie sur le PLANCHER glissant 30 s, pas sur l'instantané : c'est la question
    utile (« ce maillon a-t-il décroché récemment ? ») et c'est ce qui empêche le badge de
    clignoter. `mesure` est la cadence précise (Δframe_index) quand elle existe, sinon la valeur
    brute du plugin.
    """
    try:
        fps = float(c.get("fps"))
    except (TypeError, ValueError):
        return {"cible": None, "tenue": None, "mesure": None}
    cible = _cadence_cible(dc)
    mesure = fps_precis_cache.get(vmid)
    if mesure is None:
        mesure = fps
    if cible <= 0:
        return {"cible": None, "tenue": None, "mesure": mesure}
    r_ok = _ratio_cadence_tenue()
    plancher = fps_plancher(vmid)
    if plancher is None:
        plancher = mesure
    return {"cible": cible, "tenue": plancher >= cible * r_ok, "mesure": mesure}


def _causes_cadence(vmid, c, m):
    """Causes RÉELLEMENT CONSTATABLES d'une sous-cadence, sous forme de LISTE DE PAIRES
    `[clé i18n "alert.cause.…", params]` (cf. `i18n._developper_sous_cles`) — jamais de sous-phrase
    française toute faite : c'est ce qui permettait à un lecteur anglophone de voir une alerte
    anglaise truffée de français au milieu, alors que les causes sont précisément la partie utile
    du message. Liste vide si on ne constate rien — mieux vaut une alerte qui dit « cause non
    identifiée » qu'une liste de suspects plausibles récitée de mémoire : l'ancienne rédaction
    (« famine CPU, entrée absente, ou traitement trop lourd ») n'a jamais permis à personne de
    remonter au vrai coupable.
    Appelée UNIQUEMENT à l'armement de l'alerte (certaines sondes font des host_exec au 1er appel)."""
    causes = []
    # 1) Placement CPU/NUMA (cf. core_pool.diagnostic_placement) — la cause du 2026-07-27.
    try:
        from . import core_pool
        d = core_pool.diagnostic_placement(c.get("node_id"), vmid, c.get("pinned_cores"))
        if d:
            causes.append([d["msg_key"], d["msg_params"]])
    except Exception as e:
        log.debug("causes cadence %s (placement): %s", vmid, e)
    # 2) Aucun cœur dédié : le conteneur partage le pool avec les autres.
    if not (c.get("pinned_cores") or "").strip():
        causes.append(["alert.cause.pinning", {}])
    # 3) Famine CPU mesurée (PSI du cgroup, déjà collectée par cpu_pressure).
    try:
        from . import cpu_pressure
        psi = cpu_pressure.for_container(vmid) or {}
        some = psi.get("some")
        if some is not None and float(some) >= 10.0:
            causes.append(["alert.cause.famine_cpu", {"pct": float(some)}])
    except Exception as e:
        log.debug("causes cadence %s (psi): %s", vmid, e)
    # 4) Segment dominant du compositing + entrées en retard, publiés par le script sur :8080.
    br = (m or {}).get("compose_breakdown_ms") or {}
    if isinstance(br, dict) and br:
        try:
            seg, val = max(((k, float(v)) for k, v in br.items()
                            if k in ("inputs", "overlays", "output") and v is not None),
                           key=lambda kv: kv[1])
            causes.append(["alert.cause.temps_trame", {"seg": seg, "val": val}])
        except (ValueError, TypeError):
            pass
    lag = (m or {}).get("inputs_lag_frames") or {}
    if isinstance(lag, dict):
        tard = sorted((k for k, v in lag.items() if isinstance(v, (int, float)) and v >= 2))
        if tard:
            causes.append(["alert.cause.entrees_retard",
                          {"liste": ', '.join(tard[:4]),
                           "extra": (f" (+{len(tard) - 4})" if len(tard) > 4 else "")}])
    return causes


def _check_cadence(vmid, c, dc, fps, m=None):
    """Compare le fps OBSERVÉ à la cadence CIBLE et alerte (transition) sur sous-cadence soutenue.
    Armement sous `fps_target_ratio`, clôture seulement sur retour FRANC et SOUTENU (hystérésis,
    cf. le bloc de constantes) — et l'alerte NOMME les causes constatées."""
    from .database import db_get_setting
    if fps is None or c.get("status") != "running":
        return
    cible = _cadence_cible(dc)
    if cible <= 0:
        return
    now = time.monotonic()
    _fps_seen_at.setdefault(vmid, now)
    try:
        ratio = float(db_get_setting("fps_target_ratio", FPS_RATIO_MIN) or FPS_RATIO_MIN)
        need = int(db_get_setting("fps_low_samples", FPS_LOW_SAMPLES) or FPS_LOW_SAMPLES)
        r_ok = float(db_get_setting("fps_clear_ratio", FPS_CLEAR_RATIO) or FPS_CLEAR_RATIO)
        n_ok = int(db_get_setting("fps_clear_samples", FPS_CLEAR_SAMPLES) or FPS_CLEAR_SAMPLES)
    except (TypeError, ValueError):
        ratio, need, r_ok, n_ok = FPS_RATIO_MIN, FPS_LOW_SAMPLES, FPS_CLEAR_RATIO, FPS_CLEAR_SAMPLES
    # Reprise après (re)démarrage : l'état vit en RAM, mais un mur sous-cadencé le reste au travers
    # d'un restart du service. Sans cette relecture, chaque boot ré-annonce le MÊME incident.
    if vmid not in _fps_alert:
        _fps_alert[vmid] = bool(_episodes_fps.get(vmid))
    fps = float(fps)
    if fps < cible * ratio:
        _fps_ok_cnt[vmid] = 0
        if now - _fps_seen_at[vmid] < FPS_GRACE_S:
            return                                   # montée en régime (fenêtre glissante) → artefact
        _fps_low_cnt[vmid] = _fps_low_cnt.get(vmid, 0) + 1
        if _fps_low_cnt[vmid] >= need and not _fps_alert.get(vmid):
            hn = c.get("hostname") or vmid
            causes = _causes_cadence(vmid, c, m)
            _p = {"h": hn, "vmid": vmid, "fps": fps, "cible": cible,
                  "depuis_s": int(_fps_low_cnt[vmid] * 5)}
            if causes:
                _cle_cad = "alert.fps.non_tenue"
                _p["causes"] = causes          # liste de paires [clé, params] — cf. i18n._developper_sous_cles
            else:
                _cle_cad = "alert.fps.non_tenue_sans_cause"
            db_add_alert(_cle_cad, "error" if fps < cible * 0.5 else "warning",
                         vmid=vmid, node_id=c.get("node_id"), kind="fps", params=_p)
            _fps_alert[vmid] = True
            _episodes_fps.poser(vmid, True)
    elif fps >= cible * r_ok:
        # Retour FRANC : seul un tel échantillon fait avancer la sortie d'épisode. Entre les deux
        # seuils (zone grise), on ne fait RIEN — ni alerte, ni clôture : c'est précisément là que
        # l'ancienne version battait.
        _fps_low_cnt[vmid] = 0
        _fps_ok_cnt[vmid] = _fps_ok_cnt.get(vmid, 0) + 1
        if _fps_alert.get(vmid) and _fps_ok_cnt[vmid] >= n_ok:
            hn = c.get("hostname") or vmid
            db_add_alert("alert.fps.retablie", "info", vmid=vmid, node_id=c.get("node_id"),
                         kind="fps",
                         params={"h": hn, "vmid": vmid, "fps": fps, "cible": cible,
                                 "depuis_s": int(_fps_ok_cnt[vmid] * 5)})
            _fps_alert[vmid] = False
            _episodes_fps.retirer(vmid)


_PURGE_EPISODES_S = 300.0
_last_purge_episodes = [0.0]


def purger_episodes_cadence(force=False):
    """Retire les épisodes de cadence des conteneurs DISPARUS — sans ça un vmid recyclé hériterait
    de l'épisode d'un autre (donc un SILENCE sur sa propre sous-cadence, l'état étant déjà
    « alerté »). Même raison et même contrat que `cpu_pressure._purger_episodes`.
    AUTO-THROTTLÉ (5 min) : appelable à chaque tour de `surveillance` sans précaution — l'appelant
    n'a pas à porter la cadence d'une purge qui ne le concerne pas."""
    now = time.time()
    if not force and now - _last_purge_episodes[0] < _PURGE_EPISODES_S:
        return
    _last_purge_episodes[0] = now
    try:
        from .database import db_get_containers
        vivants = {str(c.get("vmid")) for c in (db_get_containers() or [])}
    except Exception as e:
        log.debug("purge des épisodes de cadence impossible (%s) — reportée.", e)
        return
    n = _episodes_fps.purger(lambda cle: cle in vivants)
    if n:
        log.info("Épisodes de cadence : %d entrée(s) purgée(s) (conteneur disparu).", n)


def _reset_cadence(vmid):
    """Redémarrage de script/conteneur : la fenêtre glissante repart de zéro → on ré-arme le délai
    de grâce (sinon la montée en régime déclencherait une fausse alerte de sous-cadence)."""
    _fps_seen_at[vmid] = time.monotonic()
    _fps_low_cnt[vmid] = 0
    _fps_ok_cnt[vmid] = 0           # la sortie d'épisode doit être RE-prouvée après un redémarrage
    _fps_miss_cnt.pop(vmid, None)   # idem pour la péremption : le compteur repart du redémarrage


def get_metrics(ip, port=8080, avec_statut=False):
    """Renvoie un dict normalisé : {fps, receivers?, senders?}.
    - receiver/multiview classiques : {fps, frame_index} → fps top-level conservé tel quel
    - receiver_nmos (multi-pipeline) : {receivers: [{idx, essence, fps, ...}, ...]} →
      on aggrège fps = max sur les pipelines VIDÉO (audio = chunks/sec, hors-norme)
    - 2110_io (TX) unifié : {senders: [...], sdp: {...}} → idem, fps = max vidéo
    `port` = base des ports du contrôleur (8080 par défaut ; offsetté pour une sonde probe_2110
    coexistant avec un moteur sur le même nœud, cf. deploy.controller_port_base).

    `avec_statut=True` → renvoie `(data, joignable)`. Sans lui, un dict SANS fps est indiscernable
    d'un endpoint MUET (les deux valaient {"fps": None}) : c'est exactement l'ambiguïté qui
    empêchait de périmer la cadence sans risquer un faux positif. Les autres appelants
    (probe_monitor, routes/probe) gardent la signature historique.
    """
    try:
        r = requests.get(f"http://{ip}:{port}", timeout=2)
        if r.status_code == 200:
            data = r.json()
            for key in ("receivers", "senders", "channels"):
                if isinstance(data.get(key), list):
                    # On ne prend que les entrées vidéo pour l'agrégat principal (les fps audio
                    # = nombre de chunks/sec, ~1000, qui polluerait l'affichage)
                    video_fps = [float(it.get("fps") or 0)
                                 for it in data[key]
                                 if (it.get("essence") in (None, "video"))]
                    if video_fps:
                        data["fps"] = max(video_fps)
                        break
            return (data, True) if avec_statut else data
    except Exception:
        pass
    return ({"fps": None}, False) if avec_statut else {"fps": None}

_advisory_seen = {}   # (vmid, texte) → ts du dernier relais (throttle avis plugin 1 h)

# Signaux POSIX les plus parlants pour un script mort. La liste est courte à dessein : nommer
# `SIGILL` évite précisément le diagnostic de trois jours qu'a coûté le SIGILL des R620.
_SIGNAUX = {4: "SIGILL (instruction illégale — binaire compilé pour un CPU plus récent)",
            6: "SIGABRT (abort)", 7: "SIGBUS (bus error — bus MXL recréé ?)",
            9: "SIGKILL (tué — OOM ?)", 11: "SIGSEGV (segmentation)", 15: "SIGTERM (arrêt demandé)"}


def _cause_arret(vmid, cont, etat_agent):
    """Pourquoi le script s'est-il arrêté ? Code de sortie, signal, dernières lignes du journal.

    Deux sources, dans cet ordre : ce que l'agent par-conteneur publie (`last_exit`/`last_signal`,
    agents récents), puis le journal du conteneur — le script écrit sur la sortie standard, donc sa
    dernière trace est là et NULLE PART ailleurs. Sans ça, l'interface annonce « script arrêté » et
    laisse l'exploitant ouvrir un terminal : c'est ce qui a coûté le plus de temps de tout le
    chantier CPU, où « script_stopped » cachait un SIGILL en boucle.

    Best-effort : jamais d'exception remontée, jamais de blocage de la boucle de surveillance.
    """
    bouts = []
    try:
        sig = (etat_agent or {}).get("last_signal")
        code = (etat_agent or {}).get("last_exit")
        if sig:
            bouts.append(_SIGNAUX.get(int(sig), "signal %s" % sig))
        elif code:
            bouts.append("code de sortie %s" % code)
    except (TypeError, ValueError):
        pass
    try:
        from .database import db_get_node
        from . import node_driver
        node = db_get_node((cont or {}).get("node_id")) if cont else None
        nom = (cont or {}).get("docker_name")
        if node and nom:
            rc, out, err = node_driver.host_exec(
                node, "docker logs --tail 25 %s 2>&1 | tail -25" % nom, timeout=20)
            lignes = [l.strip() for l in (out or "").splitlines() if l.strip()]
            # On garde les DERNIÈRES lignes non vides : une pile Python se termine par son message.
            if lignes:
                bouts.append(lignes[-1][:200])
    except Exception as e:
        log.debug("cause d'arrêt vmid=%s: %s", vmid, e)
    return " · ".join(bouts) or None


# ─── DÉCROCHAGE SILENCIEUX D'UN CONSOMMATEUR ────────────────────────────────────────────────
# Un consommateur du bus MXL peut rester accroché à une génération MORTE d'un flux : le
# producteur a recréé le sien (redéploiement, redémarrage, changement de format) et le lecteur
# ne voit plus jamais rien — SANS SIGBUS ni exception, donc sans que rien ne se plaigne.
# Vécu le 2026-08-06 : le streamer de monitoring a poussé 25 fps et 2 Mbit/s de contenu PÉRIMÉ
# pendant des heures, destination `up`, tout au vert côté supervision. Un seul chiffre le
# disait : `in_fps_seen: 0.0`.
#
# Le témoin retenu est une CONTRADICTION INTERNE au consommateur : il PRODUIT alors qu'il ne
# reçoit RIEN. Interne exprès — corréler avec le producteur via le câblage déclaré lèverait de
# faux positifs (un assembleur de tissu garde dans ses métriques des entrées logiques qu'il ne
# lit plus). On exige donc que TOUTES les entrées soient muettes, jamais une seule.
_DECROCHE_SEUIL = 6          # polls consécutifs (~30 s à 5 s de période) avant d'alerter
_decroche_ctr = {}           # vmid → nb de polls consécutifs en contradiction


def _detecter_decrochage(vmid, m, lat):
    """Alerte si le conteneur ÉMET sans RIEN recevoir. Renvoie True quand l'alerte est levée."""
    sortie = m.get("pushed_fps")
    if sortie is None:
        sortie = m.get("fps")
    try:
        sortie = float(sortie or 0)
    except (TypeError, ValueError):
        return False
    if sortie <= 0:
        _decroche_ctr.pop(vmid, None)
        return False        # il n'émet pas : c'est un autre problème, pas un décrochage

    # `in_fps_seen` (streamer) est le témoin le plus direct quand il existe.
    vu = m.get("in_fps_seen")
    if vu is not None:
        try: muet = float(vu) <= 0
        except (TypeError, ValueError): return False
    else:
        # Sinon : au moins une entrée connue, et TOUTES sans latence (aucune trame reçue).
        entrees = {k: v for k, v in (lat or {}).items() if k != "*"}
        if not entrees:
            _decroche_ctr.pop(vmid, None)
            return False    # aucune entrée déclarée → rien à conclure (générateur, mire…)
        muet = all(v is None for v in entrees.values())

    if not muet:
        _decroche_ctr.pop(vmid, None)
        return False
    n = _decroche_ctr.get(vmid, 0) + 1
    _decroche_ctr[vmid] = n
    if n != _DECROCHE_SEUIL:
        return False        # alerte UNE fois au franchissement, pas à chaque poll
    c = db_get_container(vmid) or {}
    db_add_alert(
        "alert.signal.decrochage", "warning", vmid=vmid, kind="signal",
        params={"h": c.get("hostname") or ("container " + str(vmid)), "vmid": vmid, "fps": sortie})
    log.warning("décrochage silencieux : vmid %s émet %.1f fps sans entrée vivante", vmid, sortie)
    return True


def rafraichir_metrics(vmid, status_data=None):
    """status_data : dict optionnel renvoyé par proxmox.get_full_status (évite de
    refetcher si l'appelant l'a déjà). Contient cpu (0..1), mem (octets), etc."""
    if status_data:
        cpu_frac = status_data.get("cpu")
        mem = status_data.get("mem")
        if cpu_frac is not None:
            db_update_usage(vmid, round(cpu_frac * 100, 1), mem)

    ip = get_container_ip(vmid)
    if not ip:
        return
    db_update_ip(vmid, ip)

    # Auth agent :8081 (rétro-compatible : {} si pas de secret/token → en-tête absent).
    # + Session/URL TLS du plan de contrôle : agent_session()/agent_url() passent en HTTPS (vérif CA,
    # hostname off, cert client mTLS) quand la CA interne est dispo, sinon http (comportement historique).
    from .deploy import (agent_headers as _agent_headers,
                         agent_session as _agent_session, agent_url as _agent_url)
    _hdrs = _agent_headers(vmid)

    # Backend Docker MTL : pas d'agent :8081 (le contrôleur sert :8080 + :8081/nmos/subscribe,
    # pas /status). La liveness vient de `docker inspect` (boucle surveillance). On lit juste
    # les métriques :8080 et on sort, sans le bloc agent/auto-restart (qui 404rait).
    # Compute : embarque l'agent → on NE court-circuite PAS (bloc agent standard plus bas).
    _c = db_get_container(vmid)
    from .docker_compute import is_compute_container
    if _c and not is_compute_container(_c):
        from .deploy import controller_port_base as _cpb
        m, _joint = get_metrics(ip, port=_cpb(vmid), avec_statut=True)
        # Le contrôleur :8080 EST l'unique sonde de ce backend (pas d'agent :8081) : son silence
        # est donc l'équivalent d'un agent injoignable — statut `unreachable`, et surtout PAS
        # `running` avec la dernière cadence connue.
        if _joint:
            _joignable(vmid, cle="alert.flux.controleur_retabli")
        else:
            _injoignable(vmid, _c, "Contrôleur :8080",
                         "Vérifier le conteneur sur le nœud (docker logs) : moteur en cours de "
                         "démarrage (entraînement du lien E810), crash du contrôleur, ou réseau "
                         "de contrôle coupé.",
                         cle="alert.flux.controleur_injoignable")
        if m.get("fps") is not None:
            _fps_mesure(vmid, m["fps"])
        else:
            _fps_sans_mesure(vmid, (_c or {}).get("hostname"))
        fi = frame_index_de(m)
        # Même traitement que les plugins : cadence précise par Δindex, et c'est ELLE qui alimente
        # le plancher glissant (le fps brut du moteur porte la même troncature de fenêtre).
        _precis = fps_precis_note(vmid, fi)
        fps_note(vmid, _precis if _precis is not None else m.get("fps"))
        if fi is not None:
            prev = _frame_index_prev.get(vmid)
            shm_active_cache[vmid] = (prev is not None and fi > prev)
            _frame_index_prev[vmid] = fi
            frame_index_cache[vmid] = fi
        # Latence de RÉCEPTION (segment A) par slot vidéo : receivers[].rx_latency_ms keyé sur le
        # nom de shm producteur ({hostname}_{idx}, cf. wiring produces du plugin 2110_io). Permet à
        # la topologie (page Câbles) de l'afficher sur le port source, distinct du badge segment B.
        # État canonique par flux (cf. flux_etat_cache) : construit ici, publié après la boucle
        # senders pour que RX et TX atterrissent dans la MÊME table (un remplacement en deux temps
        # laisserait un lecteur concurrent voir un moteur sans ses sorties).
        _flux_now = {}
        if isinstance(m.get("nic"), dict):
            _ports = m["nic"].get("ports") or []
            # `link_up` absent (vieille image moteur) ⇒ port ignoré des deux listes : on ne sait
            # pas, et un « up » supposé vaudrait mieux que rien seulement si l'on aimait mentir.
            nic_link_cache[vmid] = {
                "up":   [p.get("iface") for p in _ports if isinstance(p, dict) and p.get("link_up") is True],
                "down": [p.get("iface") for p in _ports if isinstance(p, dict) and p.get("link_up") is False],
            }
        if isinstance(m.get("receivers"), list):
            for rec in m["receivers"]:
                if rec.get("idx") is None:
                    continue
                _flux_now[cle_flux("rx", rec.get("essence") or "video", rec["idx"])] = {
                    "mode": rec.get("mode"), "fps": rec.get("fps"),
                    "frame_index": rec.get("frame_index"),
                    "latency_ms": rec.get("rx_latency_ms"),
                    "signal": rec.get("signal") or {},
                    "error": rec.get("rx_error"),
                    "stalled": None,       # renseigné par la détection de stall ci-dessous
                }
        if isinstance(m.get("senders"), list):
            for snd in m["senders"]:
                _i = snd.get("tx_idx", snd.get("idx"))
                if _i is None:
                    continue
                _ess = snd.get("essence") or "video"
                # Un slot TX porte N flux audio (audio_idx) : sans lui, le 2ᵉ écraserait le 1ᵉʳ.
                _flux_now[cle_flux("tx", _ess, _i, snd.get("audio_idx") if _ess == "audio" else None)] = {
                    "mode": None, "fps": snd.get("fps"),
                    "fps_nominal": snd.get("fps_nominal"),
                    "fps_source": snd.get("fps_source"), "repeats": snd.get("repeats"),
                    "late": snd.get("late"),
                    "inputs_latency_ms": snd.get("inputs_latency_ms"),
                    "signal": snd.get("signal") or {},
                    "stalled": None,
                }
        if isinstance(m.get("receivers"), list):
            # Même précédence de hostname que la topologie (routes.api_home_summary) pour que la clé
            # corresponde au nom de shm producteur : params.hostname puis hostname du container.
            try:
                _dc = json.loads(_c.get("deploy_config")) if isinstance(_c.get("deploy_config"), str) else (_c.get("deploy_config") or {})
            except Exception:
                _dc = {}
            hn = ((_dc.get("params") or {}).get("hostname")) or _c.get("hostname") or ""
            # Garde-fou IP des ports média (moteur ≥ 0.35.2) : sip absent / non primaire / dupliqué
            # sur l'hôte ⇒ joins IGMP déroutés → slots RX muets (post-mortem Horace 2026-07).
            # Alerte UNE fois par changement d'état (apparition, changement, résolution).
            _warns = (m.get("nic") or {}).get("ip_warnings") or []
            if _warns != _ip_warn_prev.get(vmid, []):
                if _warns:
                    # `_warns` : texte déjà rendu par le CONTENEUR (plugins/2110_io/docker/controller.py),
                    # hors catalogue i18n (même statut qu'un `str(exception)`) — passé tel quel en
                    # paramètre, comme `e` ailleurs dans ce fichier.
                    db_add_alert("alert.net.ip_anomalie", "error",
                                 vmid=vmid, node_id=(_c or {}).get("node_id"), kind="net",
                                 params={"h": hn, "warnings": " ; ".join(_warns)})
                elif _ip_warn_prev.get(vmid):
                    db_add_alert("alert.net.ip_corrigee", "info",
                                 vmid=vmid, node_id=(_c or {}).get("node_id"), kind="net",
                                 params={"h": hn})
                _ip_warn_prev[vmid] = _warns
            rxlat = {}; stalled = {}; rxfps = {}
            # ALIMENTATION par flux, TOUTES essences (cf. rx_served_cache). Boucle séparée et
            # volontairement SANS effet de bord : le traitement vidéo ci-dessous pilote des alarmes,
            # y greffer les essences non-vidéo ferait battre des sentinelles calibrées sur la vidéo.
            served = {}
            for rec in m["receivers"]:
                suffixe = _RX_SHM_SUFFIXE.get(rec.get("essence") or "video")
                if suffixe is None or rec.get("idx") is None:
                    continue
                # PRÉSENCE DANS LA LISTE ≠ SESSION. Le moteur publie un récepteur par slot DÉCLARÉ
                # (25 ici), la plupart en mode "idle" : les compter comme alimentés remplacerait un
                # silence par un mensonge. Seul un mode de session réelle compte — "error" en fait
                # partie (le slot est abonné, il produit un flux, il ne reçoit simplement rien) :
                # ce cas-là a déjà son propre badge « ne reçoit pas », qu'on ne veut pas doubler.
                if (rec.get("mode") or "idle") in _RX_MODES_INACTIFS:
                    continue
                # ⚠ NOM DE FLUX = `numerotation`, jamais une concaténation locale (2026-08-19).
                # Ces clés sont relues AVEC LE VRAI NOM du flux (`home_dashboard` :
                # `_rxf.get(pp["shm"])`). Construites ici à la main sur l'indice BRUT, elles sont
                # restées 0-based après la migration du 2026-08-13 : plus une seule ne
                # correspondait, donc le fps par flux et le badge « abonné mais ne reçoit pas »
                # étaient MORTS depuis — sans une erreur, sans une trace. Un `.get()` qui ne
                # trouve jamais rien ne se plaint pas.
                _ess = rec.get("essence") or "video"
                _nom_flux = (flux_video(hn, rec["idx"]) if _ess == "video" else
                             flux_audio(hn, rec["idx"]) if _ess == "audio" else
                             flux_anc(hn, rec["idx"]))
                served[_nom_flux] = {
                    "essence": rec.get("essence") or "video",
                    "fps": rec.get("fps"),
                    "mode": rec.get("mode"),
                    "signal": rec.get("signal") or {},
                }
            rx_served_cache[vmid] = served

            for rec in m["receivers"]:
                if rec.get("essence") not in (None, "video"):
                    continue
                idx = rec.get("idx")
                if rec.get("rx_latency_ms") is not None:
                    rxlat[flux_video(hn, idx)] = rec["rx_latency_ms"]
                if rec.get("fps") is not None:
                    rxfps[flux_video(hn, idx)] = rec["fps"]
                # « Abonné mais ne reçoit pas », deux cas :
                #  - mode "error" : mtl_rx a remonté un échec de création (budget lcores) → stall IMMÉDIAT
                #    + cause précise (rx_error) dans l'alerte ;
                #  - mode "mtl" mais frame_index FIGÉ sur ≥ seuil polls → pas de trafic (source/réseau).
                k = (vmid, idx); skey = flux_video(hn, idx)
                _mode = rec.get("mode")
                if _mode == "error":
                    stalled[skey] = True
                    _r = _flux_now.get(cle_flux("rx", "video", idx))
                    if _r is not None:
                        _r["stalled"] = True
                    # Alerte UNE fois par ÉPISODE (persisté) — la sentinelle -1 reste posée pour le
                    # calcul de stall, mais ce n'est plus elle qui décide de l'émission.
                    _flux_panne(vmid, "rx", idx, "rx_error", f"RX {hn} slot {idx}",
                                f"création de session échouée "
                                f"({rec.get('rx_error') or 'transport'}) — budget lcores du nœud "
                                f"dépassé ? augmentez les lcores (auto)", "error",
                                cle="alert.flux.rx_erreur_creation",
                                params={"cause": rec.get("rx_error") or "transport"})
                    _rx_stall_cnt[k] = -1; _rx_fi_prev.pop(k, None); _rx_off_cnt.pop(k, None)
                elif _mode == "mtl":
                    _rx_off_cnt.pop(k, None)
                    fi_now = rec.get("frame_index")
                    prev = _rx_fi_prev.get(k)
                    if isinstance(fi_now, int) and prev is not None and fi_now <= prev:
                        _rx_stall_cnt[k] = _rx_stall_cnt.get(k, 0) + 1
                    else:
                        _rx_stall_cnt[k] = 0
                    if isinstance(fi_now, int):
                        _rx_fi_prev[k] = fi_now
                    is_stalled = _rx_stall_cnt.get(k, 0) >= RX_STALL_POLLS
                    stalled[skey] = is_stalled
                    _r = _flux_now.get(cle_flux("rx", "video", idx))
                    if _r is not None:
                        _r["stalled"] = is_stalled
                    if is_stalled:      # alerte UNE fois par épisode (état persisté), pas par tick
                        _flux_panne(vmid, "rx", idx, "rx_stall", f"RX {hn} slot {idx}",
                                    "abonnée mais ne reçoit aucun flux — source absente / "
                                    "réseau-switch, OU files désalignées : un redéploiement du "
                                    "moteur réaligne les files", "warning",
                                    cle="alert.flux.rx_stall")
                    elif isinstance(fi_now, int) and prev is not None and fi_now > prev:
                        # Clôture sur PREUVE POSITIVE de santé (le frame_index a réellement avancé),
                        # jamais sur simple absence de preuve : au redémarrage de l'orchestrateur
                        # _rx_fi_prev est vide, donc les premiers polls d'un flux TOUJOURS MORT ne
                        # comptent pas encore de stall — les prendre pour de la santé refermait
                        # l'épisode (« rétabli ») puis le rouvrait aussitôt. Deux alertes de plus
                        # à chaque restart au lieu de zéro.
                        _flux_ok(vmid, "rx", idx, cle="alert.flux.rx_retabli")      # hystérésis : N polls sains avant clôture
                else:
                    # Mode ni "error" ni "mtl" (slot idle, moteur en cours de bascule…) : NE PAS
                    # lâcher l'état au premier tick — un blip du mode remis à zéro réarmait la
                    # sentinelle, et la même alerte repartait au tick suivant (118 fois en prod).
                    _rx_off_cnt[k] = _rx_off_cnt.get(k, 0) + 1
                    if _rx_off_cnt[k] >= FLUX_IDLE_POLLS:
                        _rx_stall_cnt.pop(k, None); _rx_fi_prev.pop(k, None)
                        _flux_ok(vmid, "rx", idx, cle="alert.flux.rx_retabli")      # sorti de l'état surveillé ⇒ clôt l'épisode
                # Présence signal (audit A5) : noir/gel/silence du slot (moteur ≥ 0.37.0).
                _check_signal(vmid, hn, "rx", idx, rec.get("signal"))
            rx_latency_cache[vmid] = rxlat
            rx_stalled_cache[vmid] = stalled
            rx_fps_cache[vmid] = rxfps
            # Moteur revenu VIDE (0 session alors que des abonnements IS-05 sont actifs) : aucun
            # slot n'est « en défaut » dans ce cas (ils sont simplement "idle") → seul l'écart
            # attendu/servi le révèle. Jamais silencieux.
            try:
                _check_sessions_moteur(vmid, hn, m["receivers"])
            except Exception as e:
                log.debug(f"check sessions moteur {vmid}: {e}")
        # Famine TX (symétrique du RX) : un sender vidéo « activé + câblé » (inputs_latency_ms non vide)
        # censé émettre (fps_nominal>0) mais à fps≈0 sur ≥ seuil polls ⇒ n'émet rien. Alerte UNE fois.
        if isinstance(m.get("senders"), list):
            try:
                _dc = json.loads(_c.get("deploy_config")) if isinstance(_c.get("deploy_config"), str) else (_c.get("deploy_config") or {})
            except Exception:
                _dc = {}
            hn = ((_dc.get("params") or {}).get("hostname")) or _c.get("hostname") or ""
            txstalled = {}
            txsource = {}
            for snd in m["senders"]:
                if snd.get("essence") != "video":
                    continue
                idx = snd.get("tx_idx", snd.get("idx"))
                k = (vmid, idx)
                # Fil vs source : simple relais, aucune alerte ici (cf. commentaire tx_source_cache).
                txsource[idx] = {"fps_source": snd.get("fps_source"), "repeats": snd.get("repeats")}
                _intended = bool(snd.get("inputs_latency_ms")) and float(snd.get("fps_nominal") or 0) > 0
                if _intended and float(snd.get("fps") or 0) < 1.0:
                    _tx_stall_cnt[k] = _tx_stall_cnt.get(k, 0) + 1
                else:
                    _tx_stall_cnt[k] = 0
                is_stalled = _tx_stall_cnt.get(k, 0) >= RX_STALL_POLLS
                txstalled[idx] = is_stalled
                _r = _flux_now.get(cle_flux("tx", "video", idx))
                if _r is not None:
                    _r["stalled"] = is_stalled
                if is_stalled:          # alerte UNE fois par épisode (état persisté), pas par tick
                    _tx_off_cnt.pop(k, None)
                    _flux_panne(vmid, "tx", idx, "tx_stall", f"TX {hn} slot {idx}",
                                "activé mais n'émet aucun flux — entrée absente, budget "
                                "files/lcores, OU files désalignées : un redéploiement du moteur "
                                "réaligne les files", "warning",
                                cle="alert.flux.tx_stall")
                elif _intended and float(snd.get("fps") or 0) >= 1.0:
                    # Idem RX : clôture sur PREUVE POSITIVE (le slot émet vraiment), jamais sur
                    # « pas encore assez de polls pour déclarer le stall » (état du redémarrage).
                    _tx_off_cnt.pop(k, None)
                    _flux_ok(vmid, "tx", idx, cle="alert.flux.tx_retabli")     # hystérésis : N polls sains avant clôture
                else:
                    # Slot plus « activé + câblé » : même prudence que côté RX — un blip de
                    # inputs_latency_ms/fps_nominal ne doit pas clore puis réarmer l'épisode.
                    _tx_off_cnt[k] = _tx_off_cnt.get(k, 0) + 1
                    if _tx_off_cnt[k] >= FLUX_IDLE_POLLS:
                        _flux_ok(vmid, "tx", idx, cle="alert.flux.tx_retabli")
                # Présence signal (audit A5) : contenu du shm d'entrée du slot TX (moteur ≥ 0.37.0)
                # + trames TX en retard (delta du compteur cumulatif `late`, cf. _tx_late_delta).
                # Copie défensive : `snd.get("signal")` peut être None (pas de câble) alors que
                # `late` reste pertinent (le slot peut être en retard indépendamment du câblage).
                _sig_tx = dict(snd.get("signal") or {})
                _late_delta = _tx_late_delta(vmid, idx, snd.get("late"))
                if _late_delta is not None:
                    _k_late = (vmid, idx)
                    if _late_delta >= TX_LATE_ALARM_THRESHOLD:
                        _tx_late_seen[_k_late] = time.monotonic()
                    _t_late = _tx_late_seen.get(_k_late)
                    _sig_tx["tx_late"] = bool(
                        _t_late is not None and time.monotonic() - _t_late < TX_LATE_HOLD_S)
                _check_signal(vmid, hn, "tx", idx, _sig_tx)
                # `tx_late` est CALCULÉ ici (delta du compteur cumulatif, cf. _tx_late_delta) et
                # n'existe pas dans le signal brut du moteur : sans cette recopie, un consommateur
                # de flux_etat_cache ne pourrait pas le revoir sans re-consommer le delta.
                if _r is not None:
                    _r["signal"] = _sig_tx
            tx_stalled_cache[vmid] = txstalled
            tx_source_cache[vmid] = txsource
        # Publication ATOMIQUE de l'état par flux (RX + TX ensemble, cf. flux_etat_cache). Un
        # moteur qui n'a publié ni receivers[] ni senders[] ne remplace rien : garder le dernier
        # état connu vaut mieux que le vider, et l'absence des deux listes n'est pas une preuve
        # que les flux ont disparu (payload tronqué, image ancienne).
        if _flux_now:
            flux_etat_cache[vmid] = _flux_now
        # Métriques ressources MTL (contrôleur /stats, cgroup v2 — même endpoint que compute)
        try:
            rs = _agent_session().get(_agent_url(ip, "/stats"), timeout=2, headers=_hdrs)
            if rs.status_code == 200:
                s = rs.json()
                if s.get("cpu_pct") is not None:
                    db_update_usage(vmid, round(s["cpu_pct"], 1), s.get("mem_used"),
                                    cpu_count=s.get("cpu_count"))
                if s.get("cpu_count") is not None:
                    cpu_count_cache[vmid] = int(s["cpu_count"])
                _verifier_pression_memoire(vmid, hn, s)
        except Exception:
            pass
        # Flush périodique des épisodes de panne (auto-throttlé : no-op tant que PANNES_FLUSH_S
        # n'est pas écoulé → aucune écriture disque dans la boucle de surveillance 5 s).
        _flush_pannes()
        return

    # Vérifier si le script tourne via l'agent (HTTPS mTLS si CA dispo, sinon http)
    agent_ok = False
    try:
        r = _agent_session().get(_agent_url(ip, "/status"), timeout=2, headers=_hdrs)
        agent_ok = (r.status_code == 200)
        if agent_ok:
            _st = r.json() or {}
            running = _st.get("running", False)
            prev_run = _prev_script_running.get(vmid)
            if not running:
                cause = None
                # ★ ARRÊTÉ VOLONTAIREMENT ? Alors ce n'est ni une panne, ni quelque chose à
                # réparer. `containers.script_enabled` porte l'intention d'exploitation, posée par
                # le `NcWorker.enabled` du modèle MS-05-02 : un contrôleur NMOS tiers a le droit
                # d'arrêter un traitement, et il faut que ça TIENNE.
                #
                # Sans ce garde, l'auto-relance ci-dessous redémarrait le script dans les cinq
                # secondes : la consigne du contrôleur était acceptée, appliquée, puis défaite —
                # avec, en prime, une alerte « script arrêté » et un statut `script_stopped` qui
                # décrivaient comme une panne ce que l'exploitation avait demandé. MESURÉ le
                # 2026-08-31 : c'est exactement ce qui se passait.
                from .database import db_script_enabled as _voulu_actif
                if not _voulu_actif(vmid):
                    _prev_script_running[vmid] = False
                    db_update_status(vmid, "script_disabled", cause=None)
                    return                       # ni alerte, ni relance : c'est un état voulu
                if prev_run is None or prev_run is True:
                    # TRANSITION seulement : la cause se collecte une fois, pas à chaque tour de
                    # boucle (elle coûte un `docker logs` sur le nœud).
                    cause = _cause_arret(vmid, _c, _st)
                    db_add_alert("alert.agent.script_arrete_cause" if cause else "alert.agent.script_arrete",
                                 "warning", vmid=vmid, node_id=(_c or {}).get("node_id"), kind="agent",
                                 params={"vmid": vmid, "cause": cause})
                # Auto-restart durci (audit B3) : backoff exponentiel entre tentatives,
                # alerte seuil UNE fois (edge-trigger), quarantaine + logs au plafond.
                if vmid in _crash_quarantine:
                    db_update_status(vmid, "crash_loop", cause=cause)
                else:
                    db_update_status(vmid, "script_stopped", cause=cause)
                    now = time.monotonic()
                    # NE PAS réparer un conteneur dont le cycle de vie est DÉJÀ en cours d'opération
                    # (deploy/restart/destroy en vol). Un déploiement en cours passe forcément par un
                    # état « script absent » (rootfs recréé, script pas encore poussé) : le prendre
                    # pour une panne et lancer un redéploiement CONCURRENT, c'est empiler des
                    # réparations qui se détruisent l'une l'autre (chaque recréation revide le rootfs
                    # que la précédente allait garnir) — la boucle de recréation de conteneurs
                    # observée venait de là. Le déploiement en vol EST la réparation : on passe.
                    from .vmlocks import est_verrouille
                    if est_verrouille(vmid):
                        log.debug("auto-restart %s : opération lifecycle en vol — tour passé", vmid)
                    elif now >= _restart_next_try.get(vmid, 0.0):
                        try:
                            if not _st.get("path"):
                                # Rootfs éphémère recréé (docker restart) : le script a DISPARU du
                                # disque — /start est sans objet. Redéploiement complet depuis le
                                # deploy_config persisté (thread : la boucle de surveillance ne
                                # doit pas bloquer sur un rendu + push de script).
                                import threading as _th
                                _th.Thread(target=_redeployer_script_perdu, args=(vmid,),
                                           daemon=True).start()
                            else:
                                _agent_session().post(_agent_url(ip, "/start"), timeout=3, headers=_hdrs)
                            count = _script_restart_count.get(vmid, 0) + 1
                            _script_restart_count[vmid] = count
                            from .database import db_get_setting
                            backoff_max = float(db_get_setting("script_restart_backoff_max_s",
                                                               SCRIPT_RESTART_BACKOFF_MAX_S) or SCRIPT_RESTART_BACKOFF_MAX_S)
                            delay = min(SCRIPT_RESTART_BACKOFF_S * (2 ** (count - 1)), backoff_max)
                            _restart_next_try[vmid] = now + delay
                            log.info(f"Auto-restart script container {vmid} (tentative #{count}, "
                                     f"prochaine dans {delay:.0f}s)")
                            threshold = int(db_get_setting("script_restart_alert_threshold",
                                                           SCRIPT_RESTART_ALERT_THRESHOLD) or SCRIPT_RESTART_ALERT_THRESHOLD)
                            quarantine = int(db_get_setting("script_restart_quarantine_count",
                                                            SCRIPT_RESTART_QUARANTINE) or SCRIPT_RESTART_QUARANTINE)
                            if count == threshold:
                                db_add_alert(
                                    "alert.crash_loop.seuil",
                                    "error", vmid=vmid, node_id=(_c or {}).get("node_id"),
                                    kind="crash_loop", params={"vmid": vmid, "count": count}
                                )
                            if count >= quarantine:
                                _crash_quarantine.add(vmid)
                                db_update_status(vmid, "crash_loop")
                                logs = _crash_loop_logs(vmid)
                                db_add_alert(
                                    "alert.crash_loop.actif_logs" if logs else "alert.crash_loop.actif",
                                    "error", vmid=vmid, node_id=(_c or {}).get("node_id"),
                                    kind="crash_loop", params={"vmid": vmid, "count": count, "logs": logs}
                                )
                        except Exception as e:
                            log.warning(f"Auto-restart script {vmid} échoué : {e}")
            elif running:
                # REPRISE CONFIRMÉE = marche CONTINUE pendant SCRIPT_STABLE_S, pas un simple
                # `running:true` fugace. Un script qui démarre puis remeurt entre deux polls
                # remettait compteur ET backoff à zéro à CHAQUE tour : le backoff exponentiel
                # restait bloqué à « tentative #1, prochaine dans 5s » et la quarantaine (10
                # échecs consécutifs) n'était JAMAIS atteinte → auto-restart infini. Exactement
                # le mode de panne qu'anti-crash-loop est censé arrêter (observé sur le mur 145).
                if prev_run is False:
                    _script_running_since[vmid] = time.monotonic()
                    _reset_cadence(vmid)   # fenêtre fps glissante repartie de zéro → délai de grâce
                    # La cause est EFFACÉE au retour à la normale : la garder afficherait une panne
                    # résolue comme si elle durait — un vieux message est pire qu'aucun.
                    db_update_status(vmid, "running", cause="")
                    db_add_alert("alert.agent.script_repris", "info",
                                 vmid=vmid, node_id=(_c or {}).get("node_id"), kind="agent",
                                 params={"vmid": vmid})
                if (vmid in _script_restart_count
                        and time.monotonic() - _script_running_since.get(vmid, 0.0) >= SCRIPT_STABLE_S):
                    reset_crash_loop(vmid)
            _prev_script_running[vmid] = running
    except Exception:
        agent_ok = False

    # « Agent injoignable » : état de PREMIER ORDRE (statut `unreachable`) + ÉPISODE PERSISTÉ.
    # Avant : une alerte à front montant dont l'état vivait dans un dict en mémoire — elle partait
    # une fois, se perdait au redémarrage de l'orchestrateur, et le statut restait `running`.
    # Maintenant : le statut le dit en continu, et l'épisode porte l'ancienneté RÉELLE (cf.
    # _flux_panne / flux_pannes.json), avec un message de rétablissement chiffré.
    prev_agent = _prev_agent_ok.get(vmid)
    if agent_ok:
        _joignable(vmid, cle="alert.flux.agent_retabli")
    else:
        _injoignable(vmid, _c, "Agent :8081",
                     "Vérifier le conteneur sur le nœud (docker logs / docker inspect) : agent "
                     "mort, conteneur figé, ou réseau de contrôle coupé.",
                     cle="alert.flux.agent_injoignable")
    if prev_agent != agent_ok:
        log.info("Container %s : agent %s", vmid, "joignable" if agent_ok else "INJOIGNABLE")
    _prev_agent_ok[vmid] = agent_ok

    # Métriques ressources Docker compute (agent /stats, cgroup v2)
    if agent_ok:
        try:
            rs = _agent_session().get(_agent_url(ip, "/stats"), timeout=2, headers=_hdrs)
            if rs.status_code == 200:
                s = rs.json()
                if s.get("cpu_pct") is not None:
                    db_update_usage(vmid, round(s["cpu_pct"], 1), s.get("mem_used"),
                                    cpu_count=s.get("cpu_count"))
                if s.get("cpu_count") is not None:
                    cpu_count_cache[vmid] = int(s["cpu_count"])
                # `hn` n'existe QUE dans la branche moteur (qui sort par `return`) : ici c'est
                # `_c` qui porte l'identité. Le confondre lèverait UnboundLocalError — avalé par
                # le `except` ci-dessous, donc une alarme muette de plus.
                _verifier_pression_memoire(vmid, (_c or {}).get("hostname"), s)
        except Exception:
            pass

    # CADENCE DE CONTENU NEUF, à côté de la cadence de composition. Un nœud peut composer 50 fois
    # par seconde à partir d'entrées inchangées : il publie alors 50 fps en toute honnêteté, et
    # l'interface conclut que la chaîne va bien alors que l'aval, qui n'émet que sur changement,
    # tombe à 38. Mesuré le 2026-08-07 : mur à 50,1 fps et 0 trame manquée, shards à 24-26 fps
    # ratant la moitié de leurs créneaux, TX à 38 — et la page Câbles affichait 50.
    # Publié par le script (multiview 0.69.0) ; absent ailleurs → None, l'UI n'affiche rien.
    m = get_metrics(ip)
    _fc = m.get("fps_content")
    fps_content_cache[vmid] = float(_fc) if isinstance(_fc, (int, float)) else None
    # Cadence PRÉCISE (Δframe_index / Δpoll) — calculée AVANT les fenêtres glissantes, qu'elle
    # alimente de préférence : un plancher construit sur le fps brut du plugin porte son ±1 fps
    # de troncature, et frôlerait le seuil de « cadence tenue » (49/50 = 0,98) alors que rien
    # ne décroche. Repli sur la valeur brute quand Δindex n'est pas exploitable.
    _precis = fps_precis_note(vmid, frame_index_de(m))
    # Alimente les fenêtres glissantes (avertissements de l'UI : pas de clignotement).
    fps_note(vmid, _precis if _precis is not None else m.get("fps"))
    # TRAMES PERDUES par seconde, telles que le producteur les COMPTE lui-même. C'est le signal
    # exact — il ne demande aucun seuil, aucune tolérance : zéro ou pas zéro. Un taux, lui, oblige
    # toujours à trancher « à partir de combien d'écart est-ce grave », question à laquelle il n'y
    # a pas de bonne réponse en broadcast, où une image perdue est un incident.
    fps_note(vmid, m.get("frames_missed_per_s"), canal="perdues")
    fps_note(vmid, m.get("fps_content"), canal="contenu")
    if m.get("fps") is not None:
        _fps_mesure(vmid, m["fps"])
        # Cadence tenue vs cadence CIBLE (deploy_config) → alerte de sous-cadence soutenue.
        try:
            _dcc = json.loads(_c.get("deploy_config")) if isinstance(_c.get("deploy_config"), str) \
                   else (_c.get("deploy_config") or {})
            _check_cadence(vmid, _c, _dcc, m["fps"], m)
        except Exception as e:
            log.debug(f"check cadence {vmid}: {e}")
    else:
        # PAS de mesure : ni valeur figée, ni silence. Au-delà du seuil, la cadence devient INCONNUE
        # (colonne NULL) — l'UI affiche « ? fps ». Le cas le plus courant est un :8080 muet ; un
        # script sans métriques (rien de déployé) est lui aussi, honnêtement, sans cadence connue.
        _fps_sans_mesure(vmid, (_c or {}).get("hostname"))

    # AVIS PLUGIN (générique) : un script expose `advisory` dans ses métriques :8080 pour signaler
    # une condition que l'EXPLOITANT doit arbitrer (ex. mixer slice : « source en retard chronique
    # — insérer un délai d'1 image via le plugin delay »). Relayé en alerte warning, THROTTLÉ 1 h —
    # on signale, on ne dégrade jamais automatiquement.
    #
    # DEUX FORMES acceptées (2026-08-21, migration i18n) :
    #   - dict {"key": "plugin.xxx.advisory.yyy", "params": {...}} → forme STRUCTURÉE, seule que
    #     les plugins REDÉPLOYÉS émettent depuis cette date. `key` est une clé i18n (rendue à la
    #     lecture, cf. db_add_alert) — le conteneur ne fige plus de phrase française dans le journal.
    #   - str → chemin HISTORIQUE (parc pas encore redéployé), comportement INCHANGÉ : la phrase du
    #     plugin est relayée telle quelle, non traduisible (elle vient d'un processus distant, hors
    #     catalogue i18n). À retirer quand plus aucun plugin ne publie cette forme.
    # Le throttle (clé, fenêtre 1 h) doit continuer de fonctionner dans les deux cas : la forme
    # dict throttle sur (vmid, key, params sérialisés) plutôt que sur le texte, puisqu'il n'y a
    # plus de texte avant rendu.
    # Toute AUTRE forme (liste, nombre, dict sans `key`) est ignorée avec un log.warning — jamais
    # d'exception : cette valeur vient d'un processus distant, elle n'est pas de confiance.
    _adv = m.get("advisory")
    if isinstance(_adv, dict):
        _adv_key = _adv.get("key")
        # L'avis vient d'un processus DISTANT : on n'accepte une clé que dans les espaces de noms
        # réservés (cf. `database._ALERT_CLE`). Hors de là, la chaîne serait écrite telle quelle
        # comme message — un conteneur pourrait poser n'importe quel texte au journal.
        if (isinstance(_adv_key, str)
                and _adv_key.strip().startswith(("alert.", "plugin."))):
            _adv_params = _adv.get("params") or {}
            if not isinstance(_adv_params, dict):
                _adv_params = {}
            import time as _t
            _throttle_key = (vmid, _adv_key, json.dumps(_adv_params, sort_keys=True, default=str))
            _last = _advisory_seen.get(_throttle_key, 0)
            if _t.time() - _last > 3600:
                _advisory_seen[_throttle_key] = _t.time()
                # NIVEAU déclarable par le plugin, `warning` par défaut (comportement
                # historique). Sans ça, un avis qui annonce une BONNE nouvelle — « programme
                # conforme » — partait en avertissement : l'exploitant apprend alors que les
                # avertissements ne veulent rien dire, ce que la règle des alarmes proscrit.
                # Liste blanche : la valeur vient d'un processus distant, elle n'est pas de
                # confiance, et `db_add_alert` refuserait un niveau hors vocabulaire.
                _niv = _adv.get("niveau")
                if _niv not in ("info", "warning", "error"):
                    _niv = "warning"
                db_add_alert(_adv_key, _niv, vmid=vmid, node_id=(_c or {}).get("node_id"),
                             kind="advisory", params=_adv_params)
        else:
            log.warning("advisory du conteneur %s ignoré : dict sans clé i18n exploitable (%r)",
                        vmid, _adv)
    elif isinstance(_adv, str):
        if _adv.strip():
            import time as _t
            _key = (vmid, _adv.strip())
            _last = _advisory_seen.get(_key, 0)
            if _t.time() - _last > 3600:
                _advisory_seen[_key] = _t.time()
                db_add_alert(f"Avis du conteneur {vmid} : {_adv.strip()}", "warning",
                             vmid=vmid, node_id=(_c or {}).get("node_id"), kind="advisory")
    elif _adv is not None:
        log.warning("advisory du conteneur %s ignoré : forme inattendue (%s)", vmid, type(_adv).__name__)

    # frame_index : write pointer cumulatif (video, premier pipeline trouvé)
    fi = frame_index_de(m)
    if fi is not None:
        prev = _frame_index_prev.get(vmid)
        shm_active_cache[vmid] = (prev is not None and fi > prev)
        _frame_index_prev[vmid] = fi
        frame_index_cache[vmid] = fi

    # Cache latence par shm consommé (lookup côté topology par edge.shm).
    # Trois shapes possibles dans le JSON :
    #   - inputs_latency_ms: {shm_name: ms}  (multiview, mixer, color_corrector, worker_udp)
    #   - senders: [{inputs_latency_ms: {shm_name: ms}, ...}]  (2110_io TX, multi-essence)
    lat = {}
    if isinstance(m.get("inputs_latency_ms"), dict):
        for shm_name, val in m["inputs_latency_ms"].items():
            if shm_name:
                lat[shm_name] = val
    if isinstance(m.get("senders"), list):
        for s in m["senders"]:
            shm_name = s.get("shm_name")
            if shm_name:
                lat[shm_name] = s.get("latency_ms")
            for k, v in (s.get("inputs_latency_ms") or {}).items():
                if k and v is not None:
                    lat[k] = v
    # Shape delay/avsync : channels[{delay_ms}] — pas de shm_name → fallback "*" par vmid.
    if isinstance(m.get("channels"), list):
        for ch in m["channels"]:
            d = ch.get("delay_ms")
            if d is not None and d > 0:
                lat["*"] = max(lat.get("*") or 0, d)
    latency_cache[vmid] = lat

    _detecter_decrochage(vmid, m, lat)

    # Latence PROPRE du nœud (traitement) : top-level own_latency_ms (multiview, mixer, color_corrector,
    # udc, split, delay). Sert au badge ⧖ du nœud et au cumul. Absente (plugin non migré) → on n'écrase
    # pas (repli côté routes sur max(latency_cache)).
    _own = m.get("own_latency_ms")
    if isinstance(_own, (int, float)):
        own_latency_cache[vmid] = _own

    # DÉLAI D'ÉTAGE, en TRAMES (cf. delai_etage_cache) — la seule mesure DIRECTE de ce que
    # l'étage ajoute à la chaîne. On retient `vieux_*` : le plugin distingue `recent` (plancher
    # de l'étage) de `vieux` (délai réellement SUBI par la trame produite), et c'est le second
    # qui décrit le signal tel qu'il sort. Absent → on n'invente rien, la clé reste absente et
    # l'UI affichera « non mesuré » plutôt qu'un zéro qui se lirait comme « aucun délai ».
    _de = m.get("delai_etage_trames")
    if isinstance(_de, dict):
        _moy = _de.get("vieux_moy")
        _mx = _de.get("vieux_max")
        if isinstance(_moy, (int, float)):
            delai_etage_cache[vmid] = {"trames": round(float(_moy), 2),
                                       "trames_max": (round(float(_mx), 2)
                                                      if isinstance(_mx, (int, float)) else None),
                                       # Zéro STRUCTUREL (grain ouvert avec src_index=, la
                                       # coordonnée source est propagée) vs zéro MESURÉ d'un étage
                                       # qui re-cadence : sans ce drapeau les deux s'affichent
                                       # « 0,00 img » et ne veulent pas dire la même chose.
                                       "propage": bool(_de.get("propage"))}

    # Accélération GPU : un plugin compositing (multiview) expose `gpu` (bool) + `gpu_name` sur :8080
    # quand il tourne sur cupy. Sert au badge GPU des cartes/topologie. Absent → pas de badge.
    if "gpu" in m:
        gpu_cache[vmid] = {"gpu": bool(m.get("gpu")), "name": m.get("gpu_name")}
    _verifier_gpu_effectif(vmid, (_c or {}).get("hostname"), m, (_c or {}).get("node_id"))
    # Tranche RUNTIME : clé posée par le script quand il publie réellement bande par bande
    # (multiview : seulement si SLICE_ON effectif ; mixer : SLICE_ON booléen systématique).
    slice_cache[vmid] = bool(m.get("slice_mode"))

    # Cache d'alignement multi-entrées (mélangeur/DVE) : skew par entrée (vs instant de référence),
    # entrées présentées en retard, index de l'entrée de référence (sync_ref) et budget d'alignement.
    # Consommé par la topologie (page Câbles) pour montrer « qui est immédiat / qui attend ».
    if isinstance(m.get("inputs_skew_ms"), dict) or m.get("sync_ref") is not None:
        align_cache[vmid] = {
            "skew":     m.get("inputs_skew_ms") if isinstance(m.get("inputs_skew_ms"), dict) else {},
            "late":     m.get("inputs_late") if isinstance(m.get("inputs_late"), list) else [],
            "sync_ref": m.get("sync_ref"),
            "budget":   m.get("align_budget"),
        }

    # Cache retard par entrée (multiview input-locked) : {shm: nb d'images de décalage} (0 = synchrone).
    # Consommé par la topologie (page Câbles) pour montrer « +N img » sur le port d'entrée décalé.
    if isinstance(m.get("inputs_lag_frames"), dict):
        inputs_lag_cache[vmid] = {k: v for k, v in m["inputs_lag_frames"].items() if k}

    # Cache calage A/V (streamer) : délai MANUEL appliqué (ms, signé) — affiché sur la page Câbles.
    if m.get("av_offset_ms") is not None:
        av_sync_cache[vmid] = {"applied": m.get("av_offset_ms")}

    # Monitoring pyramide : choix de proxy par tuile + besoins (multiview) ; proxies produits (pyramide).
    if isinstance(m.get("proxy_usage"), dict):
        proxy_usage_cache[vmid] = m["proxy_usage"]
    if isinstance(m.get("proxy_read"), list):
        proxy_read_cache[vmid] = m["proxy_read"]
    if isinstance(m.get("proxy_needs"), dict):
        proxy_needs_cache[vmid] = m["proxy_needs"]
    if isinstance(m.get("sources"), dict):
        pyr_sources_cache[vmid] = m["sources"]

    # Notifier Ember+ (best-effort, n'échoue pas si désactivé)
    try:
        from services import emberplus
        emberplus.notify_change()
    except Exception:
        pass


def pyramide_overview(node_id=None):
    """Croise ce que les consommateurs LISENT/DEMANDENT (caches proxy_*) avec ce que les pyramides
    PRODUISENT (derive_wiring) → par proxy : nb consommateurs + orphelin ; par source : besoins non
    couverts ; + KPIs flotte (répartition des tuiles par classe de coût). node_id=None = flotte.
    Lecture 100% CACHE (aucun appel réseau). Consommé par la console P3, le dashboard et les alertes."""
    import json as _json
    from .database import db_get_containers
    from . import plugins as _pl
    try:
        from .deploy import _bucketize_needs
    except Exception:
        def _bucketize_needs(raw, tol=0.02):
            return [(w, h, c) for (w, h, c) in raw]

    conts = [c for c in db_get_containers()
             if node_id is None or c.get("node_id") == node_id]

    # 1. Agrégat consommateurs : lectures (par shm), besoins (par source), classes de coût.
    read_count = {}                                  # shm proxy → nb tuiles le lisant
    cost_tally = {"copy": 0, "strided": 0, "gather": 0, "full": 0}
    needs_by_src = {}                                # src → [(w,h,count)]
    for c in conts:
        v = c.get("vmid")
        for _idx, info in (proxy_usage_cache.get(v) or {}).items():
            k = info.get("kind")
            # cost peut manquer (cache partiel) : replier sur "full" — une clé None ferait
            # un bucket "null" dans le JSON et casserait tout tri de clés côté consommateur.
            key = "full" if k == "full" else (info.get("cost") or "full")
            cost_tally[key] = cost_tally.get(key, 0) + 1
        for sh in (proxy_read_cache.get(v) or []):
            read_count[sh] = read_count.get(sh, 0) + 1
        for src, items in (proxy_needs_cache.get(v) or {}).items():
            for it in (items or []):
                try:
                    w = int(it[0]); h = int(it[1]); cnt = int(it[2]) if len(it) > 2 else 1
                except (TypeError, ValueError, IndexError):
                    continue
                needs_by_src.setdefault(src, []).append((w, h, cnt))

    # 2. Par pyramide : proxies produits (+#conso/orphelin), réglages, besoins non couverts.
    pyrs = []
    for c in conts:
        try:
            dc = _json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        if dc.get("type") != "pyramide":
            continue
        p = dc.get("params") or {}
        try:
            thr = max(1, int(p.get("custom_size_threshold") or 2))
        except (TypeError, ValueError):
            thr = 2
        w = _pl.derive_wiring("pyramide", c.get("hostname") or "", p)
        srcs = {}
        produced = {}                                # src → set((w,h)) exacts produits
        for prod in w.get("produces") or []:
            shm = prod.get("shm") or ""
            src = shm.split("__")[0]
            f = prod.get("format") or {}
            cons = read_count.get(shm, 0)
            srcs.setdefault(src, []).append({
                "shm": shm, "kind": ("custom" if "__s" in shm else "oct"),
                "w": f.get("width"), "h": f.get("height"),
                "consumers": cons, "orphan": cons == 0,
            })
            if f.get("width"):
                produced.setdefault(src, set()).add((int(f["width"]), int(f["height"])))
        # Besoins non couverts : taille demandée sans proxy EXACT produit (→ tuile en gather).
        unmet = []
        try:
            n = int(p.get("n_inputs") or 8)
        except (TypeError, ValueError):
            n = 8
        try:
            from .deploy import _MAX_CUSTOM_PER_SRC
        except Exception:
            _MAX_CUSTOM_PER_SRC = 6    # même garde que _bucketize_needs plus haut
        for i in range(n):
            src = p.get(cle_input(i))
            if not src:
                continue
            # Seuil SOUPLE (cf. reconcile_pyramide_sizes) : une taille « qualifie » dès lors qu'elle
            # tient dans le cap. Le seuil ne départage que le surplus quand il y a > cap tailles.
            buckets = sorted(_bucketize_needs(needs_by_src.get(src) or []), key=lambda b: -b[2])
            qual = buckets
            if len(buckets) > _MAX_CUSTOM_PER_SRC:
                qual = [b for b in buckets if b[2] >= thr]
            qual_set = {(b[0], b[1]) for b in qual[:_MAX_CUSTOM_PER_SRC]}
            for (bw, bh, cnt) in buckets:
                if (bw, bh) not in produced.get(src, set()):
                    unmet.append({"src": src, "w": bw, "h": bh, "count": cnt,
                                  "would_qualify": (bw, bh) in qual_set})
        # Orphelin ACTIONNABLE = proxy SUR-MESURE produit mais lu par personne (vrai gaspillage :
        # reconcile a créé une taille inutile). Un octave inutilisé est NORMAL (filet générique,
        # état attendu quand un sur-mesure couvre la tuile) → flag per-proxy informatif, pas compté.
        orphan_n = sum(1 for lst in srcs.values() for pr in lst
                       if pr["orphan"] and pr["kind"] == "custom")
        pyrs.append({
            "vmid": c.get("vmid"), "hostname": c.get("hostname"), "node_id": c.get("node_id"),
            "threshold": thr, "base_octaves": p.get("base_octaves") or "full",
            "extra_sizes": p.get("extra_sizes") or {},
            "sources": srcs, "orphans": orphan_n, "unmet": unmet,
            "live": pyr_sources_cache.get(c.get("vmid")) or {},
        })

    total_tiles = sum(cost_tally.values()) or 1
    kpi = {
        "cost_tally": cost_tally,
        "pct": {k: round(100.0 * v / total_tiles) for k, v in cost_tally.items()},
        "orphans": sum(x["orphans"] for x in pyrs),
        "unmet": sum(len(x["unmet"]) for x in pyrs),
        "tiles": sum(cost_tally.values()),
    }
    return {"pyramides": pyrs, "kpi": kpi}
