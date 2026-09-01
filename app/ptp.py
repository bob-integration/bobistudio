# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""PTP (IEEE 1588 / SMPTE 2059-2) — gestion ptp4l + phc2sys côté host Proxmox via SSH.

Pour ST 2110 : on synchronise l'horloge système (CLOCK_REALTIME) sur le PHC d'une
NIC PTP-capable, ce qui aligne les RTP timestamps de ffmpeg sur le domaine PTP.

Architecture cible : ptp4l sur l'host (BMCA décide master/slave automatiquement)
+ phc2sys qui propage PHC → système. Les containers LXC héritent automatiquement
de CLOCK_REALTIME de l'host (LXC sans time namespace).
"""
import json
import logging
import os
import re
import shlex
import threading
import time
from collections import deque
from datetime import datetime

from .config import DB_PATH
from .host_ops import ssh_run

log = logging.getLogger(__name__)

PTP4L_CONF_PATH    = "/etc/linuxptp/ptp4l.conf"
PTP4L_UNIT_PATH    = "/etc/systemd/system/mxl-ptp4l.service"
PHC2SYS_UNIT_PATH  = "/etc/systemd/system/mxl-phc2sys.service"

# Démons de synchro d'horloge CONCURRENTS de phc2sys. Un seul process doit discipliner
# CLOCK_REALTIME : si NTP (timesyncd/chrony/ntp) tourne en parallèle, les deux servos se
# battent → phc2sys reste collé à sa butée de fréquence et l'horloge n'est jamais alignée
# sur le PHC (TAI). On les coupe à l'activation PTP et on signale leur présence dans status().
COMPETING_TIMESYNC = ("systemd-timesyncd", "chrony", "chronyd", "ntp", "ntpsec", "ntpd")

# ─── Historique des métriques (échantillonnage serveur) ──────────────────────
# Ring buffer en mémoire alimenté par un thread de fond, pour que la page PTP
# affiche les 10 dernières minutes dès le chargement (le buffer client repartait
# de zéro à chaque reload).
SAMPLE_INTERVAL_S = 5
HISTORY_SECONDS   = 600                                  # 10 min
HISTORY_MAX       = HISTORY_SECONDS // SAMPLE_INTERVAL_S  # 120 points
# B1b-2 : état/historique PTP keyés par node_id (multi-nœud). node_id int.
_history = {}            # node_id → deque(maxlen=HISTORY_MAX)
_history_lock = threading.Lock()
_sampler_thread = None

# ─── Statistiques 24 h (moyenne / max de dérive) ─────────────────────────────
# Buffer basse résolution distinct du graphe : on ne renvoie pas 17 280 points au
# client, on en dérive juste moyenne / max d'|offset| et de mean path delay sur 24 h.
# Ça permet de quantifier la dérive que le graphe (fenêtre 10 min max) ne montre pas.
STATS_SECONDS = 86400                                    # 24 h
STATS_MAX     = STATS_SECONDS // SAMPLE_INTERVAL_S       # 17 280 points
_stats_history = {}      # node_id → deque(maxlen=STATS_MAX)
# Historique PAR RÉSEAU (multi-NIC) : (node_id, network_id) → deque. En mémoire seulement
# (le 24 h persisté reste node-niveau). Permet de grapher chaque réseau 2110 séparément.
_net_history = {}        # (node_id, network_id) → deque(maxlen=HISTORY_MAX)  — 10 min
_net_stats   = {}        # (node_id, network_id) → deque(maxlen=STATS_MAX)    — 24 h (RAM)

# Persistance « option C » : on ne fait PAS un write disque par échantillon (5 s →
# 17 280 fsync/jour). Le ring 24 h vit en mémoire et est flushé en bloc dans un
# seul JSON toutes les 5 min (~288 écritures/jour, qq dizaines de Ko). Au pire on
# perd 5 min d'historique sur un crash — acceptable pour de la stat moyenne/max.
STATS_PERSIST_PATH = os.path.join(os.path.dirname(DB_PATH) or ".", "ptp_stats.json")
STATS_FLUSH_S      = 300                                 # flush toutes les 5 min
_last_flush        = 0.0

# Dernier status() COMPLET par nœud (l'échantillon réduit de _history ne garde pas grandmaster_id).
# {node_id → status dict}. Permet aux routes pollées de lire l'état PTP sans refaire les commandes SSH.
_last_status = {}

# État précédent par (node_id, network_id) pour la DÉTECTION D'ÉVÉNEMENTS PTP (journal persisté).
# {ifaces_state, grandmaster_id, locked, clock_ok, ptp4l_running}. Cf. _detect_ptp_events.
_ptp_event_state = {}

# Alarmes antenne (audit A6) : suivi d'horloge ABSENTE de façon prolongée, par (node_id, network_id).
# Une perte brève (resync, bascule GM) reste un warning du journal ; au-delà de `ptp_unlock_err_s`
# (défaut 30 s) l'antenne n'est plus alignée → alerte ERROR une fois, info au retour à la normale.
# Ces deux structures sont en MÉMOIRE : elles sont re-seedées au boot depuis le journal persisté
# (_seed_escalade_depuis_journal), sinon chaque redémarrage ré-émet l'alerte d'un incident en cours.
_unlocked_since  = {}     # (node_id, network_id) → time.time() du passage clock_ok→False
_unlock_alerted  = set()  # clés ayant déjà émis l'alerte error (edge-trigger)
PTP_UNLOCK_ERR_S = 30.0   # setting ptp_unlock_err_s

# ── QUALITÉ du servo, distincte de la DISPONIBILITÉ de l'horloge ────────────────────────────────
# `clock_ok` (donc l'alarme ci-dessus) répond « y a-t-il une référence de temps ». Le verrou servo
# STRICT de libmtl, lui, répond « le servo tient-il sous 100 ns en continu » — une question de
# QUALITÉ. Elle n'était pas posable jusqu'au 2026-08-30 : l'asservissement en fréquence du PHC
# n'était pas compilé, le verrou ne s'armait JAMAIS, et une alarme dessus aurait hurlé en
# permanence sur tout le parc. Depuis le correctif il tient — il devient donc le premier
# indicateur de DÉGRADATION d'horloge du produit, là où on ne savait signaler que l'absence.
_servo_loose_since = {}   # (node_id, network_id) → time.time() du passage locked→False
_servo_alerted     = set()
# 600 s : très au-dessus de la convergence au démarrage (1 à 2 min mesurées), pour qu'un
# déploiement ne déclenche jamais l'alarme. C'est une dégradation DURABLE qu'on veut voir, pas une
# excursion passagère — sinon on rejoue l'alarme qui bat et qu'on apprend à ignorer.
PTP_SERVO_WARN_S = 600.0  # setting ptp_servo_warn_s
# Anomalies DIFFUSÉES dans le fil d'alertes, par (node_id, network_id, ifname, type) : c'est ce qui
# permet de refermer chaque ligne d'alerte quand l'événement inverse arrive (cf. ev()). Re-seedé au
# boot depuis le journal persisté (_seed_alertes_depuis_journal) — une anomalie ouverte avant un
# redémarrage doit pouvoir se refermer après.
_alerte_diffusee = set()


def _ptp_alertes_actives():
    """Gate `ptp_alerts_enabled` (défaut ON) : pont des événements PTP warning/error vers la
    table alerts (fil d'alertes général) — le journal ptp_events reste toujours alimenté."""
    from .database import db_get_setting
    v = db_get_setting("ptp_alerts_enabled", "1")
    return str(v).strip().lower() not in ("0", "false", "off", "non", "")


def _hist(node_id):
    """deque d'historique 10 min du nœud (créé à la demande). Appeler sous _history_lock."""
    return _history.setdefault(int(node_id), deque(maxlen=HISTORY_MAX))

def _stats(node_id):
    """deque de stats 24 h du nœud (créé à la demande). Appeler sous _history_lock."""
    return _stats_history.setdefault(int(node_id), deque(maxlen=STATS_MAX))


def _load_stats():
    """Recharge les rings 24 h depuis le dump JSON au démarrage (keyé par node_id, filtré à 24 h).
    Tolère l'ancien format (liste plate) en l'ignorant (repart à zéro — stat moyenne/max seulement)."""
    try:
        with open(STATS_PERSIST_PATH, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return
    if not isinstance(data, dict):
        return   # ancien format mono-hôte (liste) → ignoré
    cutoff = time.time() - STATS_SECONDS
    n = 0
    with _history_lock:
        _stats_history.clear()
        for nid, rows in data.items():
            try:
                nid = int(nid)
            except (TypeError, ValueError):
                continue
            dq = _stats(nid)
            for r in rows or []:
                if isinstance(r, dict) and r.get("t", 0) >= cutoff:
                    dq.append(r); n += 1
    log.info("PTP : %d échantillons 24 h rechargés (%d nœuds) depuis %s",
             n, len(_stats_history), STATS_PERSIST_PATH)


def _flush_stats():
    """Écrit les rings 24 h (par nœud) dans le JSON (write atomique)."""
    with _history_lock:
        snapshot = {str(nid): list(dq) for nid, dq in _stats_history.items()}
    tmp = STATS_PERSIST_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, STATS_PERSIST_PATH)
    except OSError as e:
        log.debug("ptp flush stats: %s", e)


def record_sample(node_id, s):
    """Ajoute un échantillon (dict status) à l'historique. Node-niveau (agrégat = réseau primaire)
    ET un échantillon par RÉSEAU présent dans `s["domains"]` (multi-NIC)."""
    with _history_lock:
        t = time.time()
        sample = {
            "t":             t,
            "offset_ns":     s.get("offset_ns"),
            "mpd_ns":        s.get("mean_path_delay_ns"),
            "locked":        bool(s.get("locked")),
            "port_state":    s.get("port_state"),
            "ptp4l_running": bool(s.get("ptp4l_running")),
        }
        _hist(node_id).append(sample)
        _stats(node_id).append({"t": t, "offset_ns": sample["offset_ns"], "mpd_ns": sample["mpd_ns"]})
        # Par réseau (un échantillon par domaine de l'agrégat multi-NIC).
        for d in (s.get("domains") or []):
            nid2 = d.get("network_id")
            if nid2 is None:
                continue
            k = (int(node_id), int(nid2))
            _net_history.setdefault(k, deque(maxlen=HISTORY_MAX)).append({
                "t": t, "offset_ns": d.get("offset_ns"), "mpd_ns": d.get("mean_path_delay_ns"),
                "locked": bool(d.get("locked")), "port_state": d.get("port_state"),
                "ptp4l_running": bool(d.get("ptp4l_running")),
            })
            _net_stats.setdefault(k, deque(maxlen=STATS_MAX)).append({
                "t": t, "offset_ns": d.get("offset_ns"), "mpd_ns": d.get("mean_path_delay_ns")})


def get_history(node_id, network_id=None):
    if node_id is None:
        return []
    with _history_lock:
        if network_id is not None:
            return list(_net_history.get((int(node_id), int(network_id)), ()))
        return list(_history.get(int(node_id), ()))


def cached_status(node_id):
    """Dernier status() complet du nœud relevé par le sampler (+ "t"), ou None si pas encore relevé."""
    if node_id is None:
        return None
    with _history_lock:
        s = _last_status.get(int(node_id))
        return dict(s) if s else None


def get_stats_24h(node_id, network_id=None):
    """Moyenne et max (sur |valeur|) de l'offset master et du mean path delay sur 24 h, pour un
    nœud (ou un RÉSEAU précis si network_id fourni)."""
    cutoff = time.time() - STATS_SECONDS
    if node_id is None:
        rows = []
    else:
        with _history_lock:
            src = (_net_stats.get((int(node_id), int(network_id)), ()) if network_id is not None
                   else _stats_history.get(int(node_id), ()))
            rows = [r for r in src if r["t"] >= cutoff]

    def _agg(key):
        vals = [r[key] for r in rows if r[key] is not None]
        if not vals:
            return {"mean_ns": None, "mean_abs_ns": None, "max_abs_ns": None}
        return {
            "mean_ns":     sum(vals) / len(vals),
            # Moyenne de la valeur absolue : pour un offset qui oscille autour de 0, la moyenne
            # signée tend vers 0 et masque la dérive réelle → on expose aussi la moyenne des |v|.
            "mean_abs_ns": sum(abs(v) for v in vals) / len(vals),
            "max_abs_ns":  max(abs(v) for v in vals),
        }

    span = (rows[-1]["t"] - rows[0]["t"]) if rows else 0
    return {
        "count":  len(rows),
        "span_s": span,
        "offset": _agg("offset_ns"),
        "mpd":    _agg("mpd_ns"),
    }


_PTP_BAD_PORT_STATES = ("FAULTY", "DISABLED")


def clock_ok(d):
    """L'horloge de ce domaine est-elle réellement utilisable ?

    CRITÈRE UNIQUE de tout le produit (alarmes, badges, sonde) : `locked` seul est le critère de
    l'ÈRE AF_XDP (ptp4l noyau) et ment sur un nœud full-PF DPDK, où il désigne le lock servo STRICT
    de libmtl. Tout code qui affiche ou alarme sur la synchro PTP passe par ICI.

    Nœud à PTP MOTEUR (socle full-PF DPDK) : c'est `synced` qui fait foi, pas `locked`. Se fier à
    `locked` ICI faisait crier « holdover » sur un nœud correctement synchronisé.

    ⚠ LE MOTIF A CHANGÉ LE 2026-08-30, la règle non. On écrivait ici que le lock strict « ne s'arme
    pas sur E810 » — c'était un CONSTAT pris pour une fatalité du matériel. La cause réelle était
    que l'asservissement en FRÉQUENCE du PHC n'était jamais compilé dans libmtl (`#ifdef` dont le
    macro n'existait nulle part). Une fois activé, le lock strict s'arme en permanence.

    `synced` reste le critère, et pour une raison qui vaut MAINTENANT, pas par compatibilité : les
    deux flags ne répondent pas à la même question. `synced` = « il Y A une référence de temps »
    (DISPONIBILITÉ) ; `locked` = « le servo est CONVERGÉ » (QUALITÉ). Cette fonction décide d'une
    ALARME d'horloge absente/holdover — donc de disponibilité. Au démarrage d'un moteur, le servo
    met une à deux minutes à converger : alarmer sur `locked` crierait à chaque déploiement, et une
    excursion passagère au-dessus de 100 ns ferait battre l'alarme. Une alarme de QUALITÉ sur
    `locked` est un autre objet, à créer séparément si on la veut.
    Cf. [[ptp-servo-frequence-jamais-compile]].
    Nœud ptp4l : `locked` reste le critère (calculé par status()/status_multi depuis pmc)."""
    if d.get("engine_ptp"):
        return bool(d.get("synced") or d.get("locked"))
    return bool(d.get("locked"))


_clock_ok = clock_ok      # alias interne historique


# Types d'événement d'ESCALADE (horloge perdue) dans le journal ptp_events ; le retour à la normale
# est journalisé en `clock_ok`. Ce vocabulaire est relu au boot par _seed_escalade_depuis_journal.
_ESCALADE_TYPES = ("holdover", "clock_absent", "no_gm")


def _clock_fault(d):
    """(type d'événement, phrase, clé i18n, params i18n) décrivant POURQUOI l'horloge manque,
    d'après l'observation COURANTE du domaine. `phrase` alimente le journal persisté ptp_events
    (non i18n, inchangée) ; `(clé, params)` sert UNIQUEMENT au fil d'alertes (cf. ev()).

    On ne dit « holdover » — qui signifie « on AVAIT une référence, on l'a perdue, la dérive est
    désormais libre » — que s'il y a bel et bien un client PTP doté d'un grandmaster. Un moteur sans
    session n'a JAMAIS eu d'horloge : l'annoncer en holdover envoie l'exploitant chercher une panne
    réseau ou un GM défaillant alors que le problème est ailleurs (constaté le 2026-07-26)."""
    if d.get("engine_ptp") and not d.get("engine_ptp_client"):
        if d.get("engine_sessions") == 0:
            return ("clock_absent",
                    "aucune horloge — le moteur 2110_io ne tourne AUCUNE session, donc pas de "
                    "mtl_init ni de client PTP libmtl, et un port DPDK n'a pas de ptp4l noyau : ce "
                    "nœud n'a AUCUNE référence de temps (ce n'est PAS un holdover)",
                    "alert.ptp.horloge_absente_sans_session", {})
        return ("clock_absent",
                "aucune horloge — client PTP du moteur absent : {}".format(
                    d.get("error") or "moteur muet sur :8080"),
                "alert.ptp.horloge_absente_client_absent",
                {"e": d.get("error") or "moteur muet sur :8080"})
    if not d.get("grandmaster_id"):
        return ("no_gm", "aucun grandmaster annoncé — horloge en roue libre",
                "alert.ptp.aucun_grandmaster", {})
    return ("holdover", "déverrouillé — holdover, dérive libre", "alert.ptp.holdover", {})


def _seed_escalade_depuis_journal(key, nid, net_id):
    """Restaure l'état d'ESCALADE depuis le journal persisté (ptp_events) au 1er échantillon d'un
    (nœud, réseau) — c'est-à-dire à chaque (re)démarrage de l'orchestrateur.

    `_unlock_alerted` vit en mémoire et l'alerte est edge-triggered : sans cette reprise, un
    orchestrateur redémarré re-seedait à vide puis ré-émettait la MÊME erreur 30 s plus tard. Un
    incident UNIQUE et permanent ressemblait donc à un incident par redémarrage (10 redémarrages
    dans l'après-midi du 2026-07-26 sur dl360-1 = 10 « erreurs PTP » pour une seule panne). On
    repart aussi de l'horodatage de l'escalade, pour que la durée annoncée reste celle de
    l'incident et non celle du process."""
    from .database import db_last_ptp_event
    try:
        row = db_last_ptp_event(nid, net_id, _ESCALADE_TYPES + ("clock_ok",))
    except Exception as e:
        log.debug("seed escalade PTP (%s/%s): %s", nid, net_id, e)
        return
    if not row or row.get("type") not in _ESCALADE_TYPES:
        return
    _unlock_alerted.add(key)
    try:
        _unlocked_since[key] = datetime.fromisoformat(row["ts"]).timestamp()
    except (TypeError, ValueError):
        _unlocked_since[key] = time.time()


def _seed_alertes_depuis_journal(nid, net_id):
    """Restaure `_alerte_diffusee` depuis le journal persisté au 1er échantillon d'un (nœud, réseau).

    Même raison d'être que _seed_escalade_depuis_journal, côté fermeture : sans cette reprise, une
    anomalie diffusée AVANT un redémarrage de l'orchestrateur ne serait jamais refermée dans le fil
    (l'état vit en mémoire) — l'exploitant garderait une ligne de panne éternelle, exactement le
    défaut qu'on corrige. On ne retient que le DERNIER événement par (interface, type) : s'il est en
    warning/error, l'anomalie est encore ouverte."""
    from .database import db_get_ptp_events
    try:
        rows = db_get_ptp_events(nid, net_id, limit=200)      # ordre DESC (le plus récent d'abord)
    except Exception as e:
        log.debug("seed alertes PTP (%s/%s): %s", nid, net_id, e)
        return
    vus = set()
    for r in rows:
        k = (r.get("ifname") or "", r.get("type"))
        if k in vus:
            continue                                          # déjà vu = plus récent, il fait foi
        vus.add(k)
        if r.get("level") in ("warning", "error"):
            _alerte_diffusee.add((int(nid), int(net_id), k[0], k[1]))


def _detect_ptp_events(node, s):
    """Compare l'échantillon `s` à l'état mémorisé par (nœud, réseau) et persiste les changements
    (service ptp4l, état de port par interface, grandmaster, lock) dans la table ptp_events.
    1er passage pour un (nœud, réseau) = seed MUET (mémorise sans émettre, pas de rafale au boot)."""
    from .database import db_add_ptp_event
    nid = node.get("id")
    nname = node.get("name") or node.get("host")
    if nid is None:
        return
    for d in (s.get("domains") or []):
        net_id = d.get("network_id")
        if net_id is None:
            continue
        key = (int(nid), int(net_id))
        net_name = d.get("name")
        cur = {
            "ifaces_state":   dict(d.get("ifaces_state") or {}),
            "grandmaster_id": d.get("grandmaster_id"),
            "locked":         bool(d.get("locked")),
            # Critère d'ALARME (≠ `locked` brut sur un nœud à PTP moteur) — cf. _clock_ok.
            "clock_ok":       _clock_ok(d),
            "ptp4l_running":  bool(d.get("ptp4l_running")),
        }
        prev = _ptp_event_state.get(key)
        if prev is None:
            _ptp_event_state[key] = cur          # seed muet
            _seed_escalade_depuis_journal(key, nid, net_id)
            _seed_alertes_depuis_journal(nid, net_id)
            continue

        def ev(ifn, typ, detail, level, alert_key=None, alert_params=None):
            """`detail` = phrase FR déjà rendue, persistée TELLE QUELLE dans le journal ptp_events
            (table non i18n — ne JAMAIS la faire dépendre d'une clé). `alert_key`/`alert_params`
            (optionnels) : quand fournis, c'est CE couple qui part vers le fil d'alertes (rendu
            dans la langue du lecteur, `nname`/`net` injectés automatiquement) ; `detail` reste
            réservé à ptp_events. Sans clé : comportement historique inchangé (f-string brute)."""
            db_add_ptp_event(nid, nname, net_id, net_name, ifn, typ, detail, level)
            # Pont vers le fil d'alertes (audit A6). Les warning/error partent tels quels ; les info
            # NE partent QUE si elles referment une anomalie DÉJÀ DIFFUSÉE sur le même
            # (nœud, réseau, interface, type). Sans cette symétrie, l'alarme est à sens unique :
            # mesuré sur Horace le 2026-07-27 après un redéploiement du moteur — le fil montrait
            # « ens1f0np0 : SLAVE → FAULTY » et « horloge déverrouillée », et JAMAIS le retour à la
            # normale, pourtant bien journalisé 12 s plus tard (le nœud était re-verrouillé, rms
            # ~70 ns). L'exploitant restait devant une panne close depuis des heures. Le filtrage
            # « pas d'info dans le fil » évitait le bruit des changements bénins : on garde ce
            # principe, on ne diffuse un retour à la normale que là où on a diffusé la panne.
            from .database import db_add_alert

            def _emit(lvl):
                if not _ptp_alertes_actives():
                    return
                if alert_key:
                    p = dict(alert_params or {})
                    p.setdefault("nname", nname)
                    p.setdefault("net", net_name or net_id)
                    db_add_alert(alert_key, lvl, node_id=nid, kind="ptp", params=p)
                else:
                    db_add_alert(f"PTP {nname}/{net_name or net_id} : {detail}", lvl,
                                 node_id=nid, kind="ptp")

            ek = (int(nid), int(net_id), ifn or "", typ)
            if level in ("warning", "error"):
                _alerte_diffusee.add(ek)
                _emit(level)
            elif ek in _alerte_diffusee:
                _alerte_diffusee.discard(ek)
                _emit("info")

        if cur["ptp4l_running"] != prev["ptp4l_running"]:
            if cur["ptp4l_running"]:
                ev(None, "service", "ptp4l démarré", "info",
                   alert_key="alert.ptp.service_demarre")
            else:
                ev(None, "service", "ptp4l arrêté", "error",
                   alert_key="alert.ptp.service_arrete")
        # État de port par interface (la bascule red/blue se voit ici : SLAVE → FAULTY, etc.)
        for ifn, stt in cur["ifaces_state"].items():
            old = prev["ifaces_state"].get(ifn)
            if old != stt:
                level = "error" if stt in _PTP_BAD_PORT_STATES else "info"
                ev(ifn, "port_state", f"{ifn} : {old or '—'} → {stt}", level,
                   alert_key="alert.ptp.port_etat_change",
                   alert_params={"ifn": ifn, "old": old or "—", "new": stt})
        for ifn, old in prev["ifaces_state"].items():       # interface qui ne reporte plus d'état
            if ifn not in cur["ifaces_state"] and old:
                ev(ifn, "port_state", f"{ifn} : {old} → —", "warning",
                   alert_key="alert.ptp.port_etat_change",
                   alert_params={"ifn": ifn, "old": old, "new": "—"})
        if cur["grandmaster_id"] != prev["grandmaster_id"]:
            ev(None, "grandmaster",
               f"grandmaster : {prev['grandmaster_id'] or '—'} → {cur['grandmaster_id'] or '—'}", "warning",
               alert_key="alert.ptp.grandmaster_change",
               alert_params={"old": prev["grandmaster_id"] or "—", "new": cur["grandmaster_id"] or "—"})
        if cur["clock_ok"] != prev["clock_ok"]:
            if cur["clock_ok"]:
                ev(None, "lock", "horloge verrouillée", "info",
                   alert_key="alert.ptp.horloge_verrouillee")
            else:
                ev(None, "lock", "horloge déverrouillée", "warning",
                   alert_key="alert.ptp.horloge_deverrouillee")
        _ptp_event_state[key] = cur

        # Horloge absente PROLONGÉE : au-delà du seuil, plus rien n'aligne l'antenne 2110 → escalade
        # en ERROR (une fois), retour à la normale en info. La CAUSE est diagnostiquée par
        # _clock_fault : holdover véritable, absence de grandmaster, ou absence pure et simple de
        # client PTP (moteur sans session) — trois pannes distinctes qui appelaient le même message.
        if not cur["clock_ok"]:
            t0 = _unlocked_since.setdefault(key, time.time())
            if key not in _unlock_alerted:
                from .database import db_get_setting
                try:
                    seuil = float(db_get_setting("ptp_unlock_err_s", PTP_UNLOCK_ERR_S) or PTP_UNLOCK_ERR_S)
                except (TypeError, ValueError):
                    seuil = PTP_UNLOCK_ERR_S
                dur = time.time() - t0
                if dur >= seuil:
                    typ, phrase, alert_key, alert_params = _clock_fault(d)
                    # Via ev() : l'escalade part dans le journal ptp_events *et* dans le fil
                    # d'alertes. Elle n'était journalisée NULLE PART — d'où un journal PTP vide
                    # (aucune ligne `lock` pour le nœud) pendant que le fil répétait l'erreur.
                    ev(None, typ, f"{phrase} (depuis {int(dur)} s)", "error",
                       alert_key=alert_key, alert_params=dict(alert_params, dur=int(dur)))
                    # Edge-trigger posé même si la passerelle d'alertes est coupée : `ptp_alerts_enabled`
                    # décide de la DIFFUSION, pas de la mémoire de l'incident.
                    _unlock_alerted.add(key)
            # Le servo n'est pas jugé pendant que l'horloge est absente : sans référence, « non
            # convergé » est une conséquence, pas une cause. On repart donc de zéro au retour.
            _servo_loose_since.pop(key, None)
            _servo_alerted.discard(key)
        else:
            # ── QUALITÉ : référence PRÉSENTE mais servo non convergé, durablement ────────────
            # Uniquement sur un nœud à PTP MOTEUR : sur un nœud ptp4l, `locked` EST le critère de
            # disponibilité (déjà traité au-dessus), et le juger deux fois ferait doublon.
            if d.get("engine_ptp") and not cur["locked"]:
                t0s = _servo_loose_since.setdefault(key, time.time())
                if key not in _servo_alerted:
                    from .database import db_get_setting
                    try:
                        seuil_s = float(db_get_setting("ptp_servo_warn_s", PTP_SERVO_WARN_S)
                                        or PTP_SERVO_WARN_S)
                    except (TypeError, ValueError):
                        seuil_s = PTP_SERVO_WARN_S
                    durs = time.time() - t0s
                    if durs >= seuil_s:
                        ev(None, "servo_loose",
                           "servo PTP non convergé depuis %d s (verrou strict < 100 ns non tenu) — "
                           "l'horloge reste utilisable, sa PRÉCISION se dégrade" % int(durs),
                           "warning",
                           alert_key="alert.ptp.servo_non_converge",
                           alert_params={"nname": nname, "net": net_name or net_id,
                                         "dur": int(durs)})
                        _servo_alerted.add(key)
            else:
                if key in _servo_alerted:
                    ev(None, "servo_loose", "servo PTP reconvergé (verrou strict tenu)", "info",
                       alert_key="alert.ptp.servo_reconverge",
                       alert_params={"nname": nname, "net": net_name or net_id})
                _servo_loose_since.pop(key, None)
                _servo_alerted.discard(key)

            if key in _unlock_alerted:
                ev(None, "clock_ok", "horloge rétablie", "info")
                if _ptp_alertes_actives():
                    from .database import db_add_alert
                    db_add_alert("alert.ptp.horloge_retablie", "info", node_id=nid, kind="ptp",
                                 params={"nname": nname, "net": net_name or net_id})
            _unlocked_since.pop(key, None)
            _unlock_alerted.discard(key)
            for _t in _ESCALADE_TYPES:           # l'escalade se referme par son propre message
                _alerte_diffusee.discard((int(nid), int(net_id), "", _t))


def _sampler_loop():
    """Thread de fond : échantillonne status() de CHAQUE nœud dont le PTP est activé (par-nœud)."""
    global _last_flush
    from . import settings as st
    from .database import db_get_nodes
    while True:
        try:
            for node in db_get_nodes() or []:
                nid  = node.get("id")
                host = node.get("host")
                if nid is None or not host:
                    continue
                # Nœud full-PF DPDK : ptp_enabled est OFF sur le port vfio (pas de ptp4l), mais le
                # moteur porte un PTP interne → l'échantillonner quand même (sinon panneau vide).
                _dpdk = False
                try:
                    from . import docker_driver
                    _dpdk = docker_driver._has_dpdk_pf(node)
                except Exception:
                    _dpdk = False
                if not _dpdk and not st.setting_for("ptp_enabled", nid):
                    continue
                # status_for_node = chemin multi-NIC si le nœud a des groupes PTP
                # (node_interfaces), sinon repli mono. Dict plat dans les deux cas.
                s = status_for_node(nid, host, int(st.setting_for("ptp_domain", nid) or 0))
                with _history_lock:
                    _last_status[int(nid)] = {**s, "t": time.time()}
                record_sample(nid, s)
                try:
                    _detect_ptp_events(node, s)
                except Exception as e:
                    log.debug("ptp events detect: %s", e)
            # Flush périodique du ring 24 h (pas à chaque échantillon)
            if time.time() - _last_flush >= STATS_FLUSH_S:
                _flush_stats()
                _last_flush = time.time()
        except Exception as e:
            log.debug("ptp sampler: %s", e)
        time.sleep(SAMPLE_INTERVAL_S)


def start_sampler():
    """Lance le thread d'échantillonnage (idempotent). Appelé depuis main.py."""
    global _sampler_thread, _last_flush
    if _sampler_thread and _sampler_thread.is_alive():
        return
    _load_stats()
    _last_flush = time.time()  # évite un flush immédiat juste après le reload
    _sampler_thread = threading.Thread(target=_sampler_loop, daemon=True)
    _sampler_thread.start()
    log.info("PTP : échantillonnage historique démarré (%ds, %d points max)",
             SAMPLE_INTERVAL_S, HISTORY_MAX)


def _ptp4l_conf(domain, priority1=128, priority2=128,
                log_announce=0, log_sync=-3, log_delay_req=-3,
                announce_timeout=3, delay_thresh=800, utc_offset=37,
                client_only=True):
    """Configuration ptp4l générique, profile SMPTE 2059-2. BMCA actif.

    `client_only` (clientOnly 1) : le nœud ne se proclame jamais grandmaster — en perte
    d'Announce il attend en LISTENING au lieu de basculer MASTER (cf. réglage ptp_client_only).
    gmCapable devient sans effet quand clientOnly=1."""
    return f"""# Géré par orchestrateur MXL
[global]
domainNumber              {int(domain)}
priority1                 {int(priority1)}
priority2                 {int(priority2)}
logAnnounceInterval       {int(log_announce)}
logSyncInterval           {int(log_sync)}
logMinDelayReqInterval    {int(log_delay_req)}
announceReceiptTimeout    {int(announce_timeout)}
syncReceiptTimeout        0
neighborPropDelayThresh   {int(delay_thresh)}
utc_offset                {int(utc_offset)}
# SMPTE 2059-2 (ST 2110-10) profile
clientOnly                {1 if client_only else 0}
gmCapable                 1
free_running              0
clock_servo               pi
"""


def _systemd_ptp4l(ifname, hw_ts):
    ts_flag = "" if hw_ts else "-S"  # -S = software timestamping
    return f"""[Unit]
Description=MXL ptp4l (IEEE 1588)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# L'interface PTP peut être hors d'un bridge / absente de /etc/network/interfaces et donc
# rester administrativement DOWN au boot → ptp4l la verrait « link down ». On la monte
# nous-mêmes avant de lancer ptp4l ('-' = ne pas échouer si la commande renvoie une erreur).
ExecStartPre=-/sbin/ip link set {ifname} up
ExecStart=/usr/sbin/ptp4l -f {PTP4L_CONF_PATH} -i {ifname} {ts_flag}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _systemd_phc2sys(ifname, hw_ts, domain=0):
    # phc2sys garde CLOCK_REALTIME aligné sur le PHC de la NIC (hardware) ou sur
    # ptp4l directement via Unix socket (software-only fallback).
    # `-n <domain>` : phc2sys interroge ptp4l (mode -w) sur le bon domaine — sans ça,
    # il parle au domaine 0 par défaut et reste bloqué « Waiting for ptp4l... » si
    # ptp4l tourne sur 127 (profil SMPTE). Cf. PROD-007.
    #
    # ⚠ `-O 0` EST VOLONTAIRE — NE PAS « CORRIGER » EN -O -37.
    # Il aligne CLOCK_REALTIME sur le PHC SANS retrancher le décalage TAI↔UTC : l'horloge système du
    # nœud tourne donc en TAI, soit 37 s en avance sur UTC. Ce n'est pas une dérive, c'est la
    # condition pour que le moteur 2110 soit juste : sans PTP interne (AF-XDP, pas de port DPDK), il
    # lit CLOCK_REALTIME et la TRAITE COMME L'HORLOGE PTP (`ptp_from_real_time` de libmtl, cf.
    # plugins/2110_io/mtl_rx.c « libmtl lit CLOCK_REALTIME … discipliné par ptp4l/phc2sys kernel »).
    # Le temps PTP étant du TAI, mettre CLOCK_REALTIME sur UTC décalerait de 37 s TOUS les
    # horodatages RTP émis par ce nœud — flux refusés par les récepteurs, synchro A/V rompue.
    # Conséquence assumée : les horodatages de journaux et de fichiers du nœud sont 37 s devant ceux
    # de l'orchestrateur. En corréler deux exige d'en tenir compte. (Le chemin DPDK aboutit au même
    # résultat par ENGINE_PHC2SYS : le nœud 2110 porte l'heure PTP, dans les deux modes.)
    d = int(domain)
    if hw_ts:
        exec_line = f"/usr/sbin/phc2sys -s {ifname} -O 0 -w -n {d}"
    else:
        # Pas de PHC → on suit ptp4l via SHM/socket
        exec_line = f"/usr/sbin/phc2sys -a -r -r -n {d}"
    return f"""[Unit]
Description=MXL phc2sys (PHC → system)
After=mxl-ptp4l.service
Requires=mxl-ptp4l.service

[Service]
Type=simple
ExecStart={exec_line}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


# ─── PTP multi-NIC : une horloge JBOD par domaine ────────────────────────────
# Modèle conforme IEEE/SMPTE pour un hôte à plusieurs NIC 2110 : UN ptp4l multi-port par
# domaine PTP (boundary_clock_jbod), donc UN seul clockIdentity et UN seul BMCA qui élit le
# meilleur master (port élu = SLAVE, les autres = PASSIVE = redondance 2022-7 chaude). Lancer
# un ptp4l par NIC donnerait N clockIdentity / N BMCA non coordonnés → red/blue peuvent
# diverger et un validateur PTP management voit plusieurs horloges → échec.
#
# Chaque domaine a ses propres unités/conf/uds (multi-domaine) :
#   - mxl-ptp4l-d<dom>.service      → ptp4l -f ptp4l-d<dom>.conf (ports déclarés dans la conf)
#   - mxl-phc2sys-d<dom>.service    → phc2sys -a (mode auto, lit l'uds de SON ptp4l)
# Un SEUL phc2sys discipline CLOCK_REALTIME : celui du domaine PRIMAIRE (-r). Les domaines
# secondaires tournent SANS -r → ils n'alignent que les PHC non-élus de leur groupe, jamais
# l'horloge système (sinon deux servos se battent — cf. PROD-009).
#
# Ce bloc est ADDITIF : les fonctions mono (_ptp4l_conf/_systemd_ptp4l/deploy_config/start/
# stop/status ci-dessus) restent la façade de compat utilisée par les routes tant que la
# Phase B ne les a pas recâblées sur node_interfaces.

# Unités/conf/uds keyées par ID DE RÉSEAU (pas par domaine) : deux réseaux peuvent partager un
# même numéro de domaine → des noms keyés-domaine entreraient en collision. Le domainNumber du
# réseau est posé DANS la conf.
def _net_uds(net_id):
    return f"/var/run/ptp4l-net{int(net_id)}"

def _net_conf_path(net_id):
    return f"/etc/linuxptp/ptp4l-net{int(net_id)}.conf"

def _net_ptp4l_unit(net_id):
    return f"mxl-ptp4l-net{int(net_id)}.service"

def _net_phc2sys_unit(net_id):
    return f"mxl-phc2sys-net{int(net_id)}.service"


def _ptp4l_conf_jbod(net_id, domain, ifaces, priority1=128, priority2=128,
                     log_announce=0, log_sync=-3, log_delay_req=-3,
                     announce_timeout=3, delay_thresh=800, utc_offset=37,
                     client_only=True):
    """Conf ptp4l multi-port (profil SMPTE 2059-2) d'un RÉSEAU : [global] + une section par
    interface. `boundary_clock_jbod 1` dès qu'il y a plusieurs NIC (le BMCA tourne sur tous les
    ports, ptp4l ne discipline que le PHC du port élu, phc2sys -a aligne le reste). `uds_address`
    dédié au réseau (pmc/phc2sys ciblent le bon process via -s/-z)."""
    ifaces = [i for i in (ifaces or []) if (i or "").strip()]
    jbod = 1 if len(ifaces) > 1 else 0
    head = f"""# Géré par orchestrateur MXL — Réseau 2110 net{int(net_id)} (PTP JBOD), domaine {int(domain)}
[global]
domainNumber              {int(domain)}
priority1                 {int(priority1)}
priority2                 {int(priority2)}
logAnnounceInterval       {int(log_announce)}
logSyncInterval           {int(log_sync)}
logMinDelayReqInterval    {int(log_delay_req)}
announceReceiptTimeout    {int(announce_timeout)}
syncReceiptTimeout        0
neighborPropDelayThresh   {int(delay_thresh)}
utc_offset                {int(utc_offset)}
# SMPTE 2059-2 (ST 2110-10) profile
clientOnly                {1 if client_only else 0}
gmCapable                 1
free_running              0
clock_servo               pi
boundary_clock_jbod       {jbod}
uds_address               {_net_uds(net_id)}
"""
    ports = "".join(f"\n[{i}]\n" for i in ifaces)
    return head + ports


def _systemd_ptp4l_net(net_id, name, ifaces, hw_ts):
    """Unité ptp4l d'un réseau. Interfaces déclarées dans la CONF (pas de -i). ExecStartPre monte
    chaque NIC (peut être DOWN au boot) — '-' = ne pas échouer."""
    ts_flag = "" if hw_ts else "-S"
    ifaces = [i for i in (ifaces or []) if (i or "").strip()]
    pre = "".join(f"ExecStartPre=-/sbin/ip link set {i} up\n" for i in ifaces)
    return f"""[Unit]
Description=MXL ptp4l (IEEE 1588) — réseau 2110 {name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{pre}ExecStart=/usr/sbin/ptp4l -f {_net_conf_path(net_id)} {ts_flag}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _systemd_phc2sys_net(net_id, domain, name, hw_ts, primary):
    """Unité phc2sys d'un réseau, en mode automatique (-a) bornée à l'uds du ptp4l du réseau.

    `primary` = ce réseau discipline aussi CLOCK_REALTIME (-r). Les réseaux secondaires tournent
    SANS -r : phc2sys n'aligne alors que les PHC non-élus du réseau, sans toucher l'horloge
    système (un seul maître système, cf. PROD-009)."""
    d = int(domain)
    r_flag = "-r " if primary else ""
    exec_line = f"/usr/sbin/phc2sys -a {r_flag}-z {_net_uds(net_id)} -n {d}"
    return f"""[Unit]
Description=MXL phc2sys (PHC → {'système + ' if primary else ''}PHC) — réseau 2110 {name}
After={_net_ptp4l_unit(net_id)}
Requires={_net_ptp4l_unit(net_id)}

[Service]
Type=simple
ExecStart={exec_line}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


# ─── Opérations SSH ─────────────────────────────────────────────

def is_installed(host):
    rc, _, _ = ssh_run(host, "which ptp4l && which phc2sys", timeout=10)
    return rc == 0


def install(host):
    """apt install linuxptp. Idempotent."""
    if is_installed(host):
        return True, "déjà installé"
    cmd = ("apt-get update >/dev/null && "
           "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends linuxptp")
    rc, out, err = ssh_run(host, cmd, timeout=120)
    if rc != 0:
        return False, f"apt rc={rc} stderr={err.strip()[:300]}"
    return True, "installé"


def deploy_config(host, ifname, domain, hw_ts, priority1=128, priority2=128,
                  log_announce=0, log_sync=-3, log_delay_req=-3,
                  announce_timeout=3, delay_thresh=800, utc_offset=37,
                  client_only=True):
    """Écrit /etc/linuxptp/ptp4l.conf + unités systemd. Reload daemon."""
    if not ifname:
        return False, "ifname requis"
    conf  = _ptp4l_conf(domain, priority1=priority1, priority2=priority2,
                        log_announce=log_announce, log_sync=log_sync,
                        log_delay_req=log_delay_req, announce_timeout=announce_timeout,
                        delay_thresh=delay_thresh, utc_offset=utc_offset,
                        client_only=client_only)
    ptp_u = _systemd_ptp4l(ifname, hw_ts)
    sys_u = _systemd_phc2sys(ifname, hw_ts, domain)
    # tee plutôt que >> pour idempotence
    script = f"""set -e
mkdir -p /etc/linuxptp
cat > {PTP4L_CONF_PATH} << 'EOF'
{conf}
EOF
cat > {PTP4L_UNIT_PATH} << 'EOF'
{ptp_u}
EOF
cat > {PHC2SYS_UNIT_PATH} << 'EOF'
{sys_u}
EOF
systemctl daemon-reload
"""
    rc, out, err = ssh_run(host, script, timeout=15)
    if rc != 0:
        return False, f"rc={rc} stderr={err.strip()}"
    return True, "ok"


def disable_competing_timesync(host):
    """Coupe + désactive tout démon NTP concurrent de phc2sys (PROD-009). Sans ça, NTP
    ramène CLOCK_REALTIME à l'UTC pendant que phc2sys vise le PHC (TAI) → tir à la corde,
    servo en butée, jamais convergé. `timedatectl set-ntp false` neutralise systemd-timesyncd
    proprement ; on `disable --now` aussi chrony/ntp s'ils sont installés. Idempotent."""
    cmd = ("timedatectl set-ntp false 2>/dev/null || true; "
           f"systemctl disable --now {' '.join(COMPETING_TIMESYNC)} 2>/dev/null || true")
    ssh_run(host, cmd, timeout=20)


def start(host):
    # Un seul maître d'horloge : on coupe NTP AVANT de lancer phc2sys (cf. PROD-009).
    disable_competing_timesync(host)
    # `enable` (persistance reboot) + `restart` (PAS `--now`) : un service déjà
    # actif n'est pas relancé par `enable --now`, donc les changements de conf/unité
    # (HW timestamping, intervalles…) seraient ignorés. `restart` force la reprise
    # de la nouvelle conf. Cf. PROD-006.
    cmd = ("systemctl enable mxl-ptp4l.service mxl-phc2sys.service 2>&1; "
           "systemctl restart mxl-ptp4l.service mxl-phc2sys.service 2>&1")
    rc, out, err = ssh_run(host, cmd, timeout=20)
    if rc != 0:
        return False, f"rc={rc} {out.strip()} {err.strip()}"
    # `systemctl --now` renvoie rc=0 dès que le service est lancé, mais ptp4l peut
    # mourir juste après (conf invalide → exit 254, puis crash-loop). On revérifie
    # l'état ~2 s plus tard et on remonte les dernières lignes de log en cas d'échec.
    check = ("sleep 2; systemctl is-active mxl-ptp4l 2>&1; "
             "echo '---'; journalctl -u mxl-ptp4l -n 5 --no-pager 2>&1")
    rc2, out2, _ = ssh_run(host, check, timeout=15)
    state, _, jlog = out2.partition("---")
    if state.strip() != "active":
        detail = " | ".join(l.strip() for l in jlog.strip().splitlines()[-3:])
        return False, f"ptp4l n'est pas resté actif : {detail or 'voir journalctl -u mxl-ptp4l'}"
    return True, "ok"


def stop(host):
    cmd = "systemctl disable --now mxl-ptp4l.service mxl-phc2sys.service 2>&1 || true"
    ssh_run(host, cmd, timeout=20)
    return True, "ok"


def status(host, domain=0):
    """Renvoie un dict { running, port_state, offset_ns, mean_path_delay_ns,
    grandmaster_id, locked, error }.
    `domain` doit correspondre au domainNumber de ptp4l : pmc interroge le
    domaine 0 par défaut, donc sur un profil SMPTE (domaine 127) il faut le
    passer explicitement sinon aucune réponse → champs vides."""
    out_state = {
        "ptp4l_running":   False,
        "phc2sys_running": False,
        "port_state":      None,
        "offset_ns":       None,
        "mean_path_delay_ns": None,
        "grandmaster_id":  None,
        "locked":          False,
        "error":           None,
        # État servo réel de phc2sys (PROD-008) : 'waiting'|'converging'|'locked'|None
        "phc2sys_state":         None,
        "phc2sys_sys_offset_ns": None,
        "phc2sys_freq_ppb":      None,
        # Démons NTP concurrents encore actifs (PROD-009) : liste de noms d'unités. Non vide =
        # conflit d'horloge → l'UI invite à ré-appliquer le PTP (qui les coupera).
        "competing_timesync":    [],
    }
    # Service status
    rc, out, _ = ssh_run(host, "systemctl is-active mxl-ptp4l mxl-phc2sys 2>&1 || true", timeout=8)
    lines = out.strip().splitlines()
    if len(lines) >= 1: out_state["ptp4l_running"]   = lines[0].strip() == "active"
    if len(lines) >= 2: out_state["phc2sys_running"] = lines[1].strip() == "active"

    # Démons d'horloge concurrents (tir à la corde sur CLOCK_REALTIME). `is-active` imprime une
    # ligne par unité (active/inactive/failed/unknown) dans l'ordre des arguments.
    rc, out, _ = ssh_run(host, "systemctl is-active " + " ".join(COMPETING_TIMESYNC) + " 2>&1 || true", timeout=8)
    states = out.strip().splitlines()
    out_state["competing_timesync"] = [u for u, s in zip(COMPETING_TIMESYNC, states)
                                       if s.strip() == "active"]

    # État servo phc2sys : un service "active" peut être bloqué « Waiting for ptp4l... »
    # (mauvais domaine, ptp4l down…) — on lit sa dernière ligne de log pour le révéler.
    if out_state["phc2sys_running"]:
        rc, jl, _ = ssh_run(host, "journalctl -u mxl-phc2sys -n 1 -o cat 2>&1", timeout=8)
        if rc == 0:
            out_state.update(_parse_phc2sys_log(jl))

    if not out_state["ptp4l_running"]:
        out_state["error"] = "ptp4l inactif"
        return out_state

    # pmc : interrogations sur le domaine configuré (-d) — sinon domaine 0 par défaut
    d = int(domain)
    rc, out, _ = ssh_run(host, f"pmc -u -b 0 -d {d} 'GET CURRENT_DATA_SET' 2>&1", timeout=8)
    if rc == 0:
        out_state.update(_parse_current_dataset(out))
    rc, out, _ = ssh_run(host, f"pmc -u -b 0 -d {d} 'GET PARENT_DATA_SET' 2>&1", timeout=8)
    if rc == 0:
        out_state.update(_parse_parent_dataset(out))
    rc, out, _ = ssh_run(host, f"pmc -u -b 0 -d {d} 'GET PORT_DATA_SET' 2>&1", timeout=8)
    if rc == 0:
        out_state.update(_parse_port_dataset(out))

    # « Locked » = on a un master ET l'offset est < seuil raisonnable
    if out_state["port_state"] in ("SLAVE", "MASTER", "GRAND_MASTER", "PASSIVE"):
        off = out_state.get("offset_ns")
        if off is not None and abs(off) < 1_000_000:  # < 1 ms
            out_state["locked"] = True
        elif out_state["port_state"] in ("MASTER", "GRAND_MASTER"):
            out_state["locked"] = True
    return out_state


# ─── Pilote PTP multi-NIC par domaine (Phase A — non encore câblé aux routes) ─────────────

# Paramètres PTP de profil PROPRES AU RÉSEAU (un réseau = une horloge logique → il définit son
# profil ; il n'hérite de rien). hw_ts n'en fait PAS partie (capacité de la carte → node-global).
_NET_PTP_PARAM_KEYS = ("priority1", "priority2", "log_announce", "log_sync", "log_delay_req",
                       "announce_timeout", "delay_thresh", "utc_offset", "client_only")
# Valeurs par défaut SMPTE 2059-2 d'un réseau (= défauts des kwargs de _ptp4l_conf_jbod). Servent
# de valeurs initiales du formulaire et de repli au déploiement pour toute clé non définie.
SMPTE_DEFAULTS = {"priority1": 128, "priority2": 128, "log_announce": 0, "log_sync": -3,
                  "log_delay_req": -3, "announce_timeout": 3, "delay_thresh": 800,
                  "utc_offset": 37, "client_only": True}


def _norm_domain(d, default=127):
    try:
        return int(d)
    except (TypeError, ValueError):
        return int(default)


def groups_from_node_interfaces(node_id):
    """Construit le spec de groupes PTP d'un nœud depuis node_interfaces.

    Renvoie [{domain, ifaces:[...], primary:bool}] : un groupe par domaine PTP distinct
    agrégeant les NIC role='media2110' ∧ ptp_enabled=1 par **media_network_id** (Réseaux 2110).
    Renvoie [{network_id, name, domain, ifaces, primary}]. Le réseau PRIMAIRE (discipline
    CLOCK_REALTIME du nœud) = réglage `ptp_primary_network`, sinon le réseau dont le domaine ==
    réglage nœud `ptp_domain`, sinon le plus petit id. Vide → l'appelant retombe sur la façade mono."""
    from . import settings as st
    from .database import db_get_node_interfaces, db_get_media_networks
    nets = {n["id"]: n for n in db_get_media_networks()}
    by_net = {}
    for r in db_get_node_interfaces(node_id):
        if (r.get("role") != "media2110") or not r.get("ptp_enabled"):
            continue
        ifn = (r.get("ifname") or "").strip()
        nid = r.get("media_network_id")
        if not ifn or nid is None or int(nid) not in nets:
            continue
        by_net.setdefault(int(nid), []).append(ifn)
    if not by_net:
        return []
    net_ids = sorted(by_net)
    # Réseau primaire (discipline CLOCK_REALTIME) : réglage ptp_primary_network, sinon le réseau
    # dont le domaine == ptp_domain du nœud, sinon le plus petit id.
    node_dom = _norm_domain(st.setting_for("ptp_domain", node_id) or 127)
    default_prim = next((i for i in net_ids if int(nets[i]["domain"]) == node_dom), net_ids[0])
    prim_raw = st.setting_for("ptp_primary_network", node_id)
    try:
        prim = int(prim_raw) if prim_raw not in (None, "") else default_prim
    except (TypeError, ValueError):
        prim = default_prim
    if prim not in by_net:
        prim = default_prim
    def _parse_params(raw):
        if not raw:
            return {}
        try:
            d = json.loads(raw)
            return {k: v for k, v in d.items() if k in _NET_PTP_PARAM_KEYS and v is not None}
        except Exception:
            return {}
    return [{"network_id": i, "name": nets[i]["name"], "domain": int(nets[i]["domain"]),
             "ifaces": sorted(set(by_net[i])), "primary": (i == prim),
             "ptp_params": _parse_params(nets[i].get("ptp_params"))}
            for i in net_ids]


def _reconcile_units(host, keep_units):
    """Désactive+supprime les unités PTP gérées par l'orchestrateur ABSENTES de `keep_units` :
    les unités mono legacy (mxl-ptp4l.service / mxl-phc2sys.service) ET les unités par-domaine
    d'une config précédente (domaines retirés). Évite que deux servos se battent pour un PHC.
    Best-effort."""
    keep = " ".join(sorted(keep_units))
    script = f"""KEEP=" {keep} "
for f in $(ls /etc/systemd/system/mxl-ptp4l*.service /etc/systemd/system/mxl-phc2sys*.service 2>/dev/null); do
  u=$(basename "$f")
  case "$KEEP" in *" $u "*) continue;; esac
  systemctl disable --now "$u" 2>/dev/null || true
  rm -f "$f"
done
systemctl daemon-reload 2>/dev/null || true"""
    try:
        ssh_run(host, script, timeout=20)
    except Exception as e:
        log.debug("_reconcile_units(%s): %s", host, e)


def deploy_config_multi(host, groups, hw_ts=True):
    """Écrit conf + unités systemd pour chaque RÉSEAU de `groups` puis reconcile (purge des unités
    legacy/obsolètes). `groups` = [{network_id, name, domain, ifaces, primary, ptp_params}]. Le
    profil PTP vient du RÉSEAU (g['ptp_params']) ; toute clé absente retombe sur le défaut SMPTE
    (kwargs de _ptp4l_conf_jbod). hw_ts est le seul paramètre node-global. N'active rien (start_multi)."""
    groups = [g for g in (groups or []) if g.get("ifaces")]
    if not groups:
        return False, "aucune interface PTP (media2110 + ptp_enabled)"
    keep_units = set()
    blocks = ["set -e", "mkdir -p /etc/linuxptp"]
    for g in groups:
        nid = int(g["network_id"]); dom = int(g["domain"]); ifaces = g["ifaces"]
        primary = bool(g.get("primary")); name = g.get("name") or f"net{nid}"
        # Profil PROPRE au réseau (les clés absentes → défaut SMPTE via les kwargs de la conf).
        opts = {k: v for k, v in (g.get("ptp_params") or {}).items() if k in _NET_PTP_PARAM_KEYS}
        conf  = _ptp4l_conf_jbod(nid, dom, ifaces, **opts)
        ptp_u = _systemd_ptp4l_net(nid, name, ifaces, hw_ts)
        sys_u = _systemd_phc2sys_net(nid, dom, name, hw_ts, primary)
        keep_units.add(_net_ptp4l_unit(nid)); keep_units.add(_net_phc2sys_unit(nid))
        blocks.append(f"cat > {_net_conf_path(nid)} << 'EOF'\n{conf}\nEOF")
        blocks.append(f"cat > /etc/systemd/system/{_net_ptp4l_unit(nid)} << 'EOF'\n{ptp_u}\nEOF")
        blocks.append(f"cat > /etc/systemd/system/{_net_phc2sys_unit(nid)} << 'EOF'\n{sys_u}\nEOF")
    blocks.append("systemctl daemon-reload")
    rc, out, err = ssh_run(host, "\n".join(blocks) + "\n", timeout=20)
    if rc != 0:
        return False, f"rc={rc} stderr={err.strip()}"
    _reconcile_units(host, keep_units)
    return True, "ok"


def start_multi(host, groups):
    """Active + (re)démarre ptp4l/phc2sys de chaque RÉSEAU. Coupe d'abord les démons NTP
    concurrents (un seul maître CLOCK_REALTIME, cf. PROD-009). `restart` (pas `enable --now`)
    pour reprendre une conf modifiée (cf. PROD-006)."""
    nets = sorted({int(g["network_id"]) for g in (groups or []) if g.get("ifaces")})
    if not nets:
        return False, "aucun réseau PTP"
    disable_competing_timesync(host)
    units = []
    for i in nets:
        units += [_net_ptp4l_unit(i), _net_phc2sys_unit(i)]
    u = " ".join(units)
    rc, out, err = ssh_run(host, f"systemctl enable {u} 2>&1; systemctl restart {u} 2>&1", timeout=25)
    if rc != 0:
        return False, f"rc={rc} {out.strip()} {err.strip()}"
    # Revérif ~2 s : un ptp4l peut mourir juste après le start (conf invalide → crash-loop).
    pu = " ".join(_net_ptp4l_unit(i) for i in nets)
    jflags = " ".join("-u " + _net_ptp4l_unit(i) for i in nets)
    rc2, out2, _ = ssh_run(host, f"sleep 2; systemctl is-active {pu} 2>&1; echo '---'; "
                                 f"journalctl {jflags} -n 5 --no-pager 2>&1", timeout=15)
    state, _, jlog = out2.partition("---")
    if any(s.strip() != "active" for s in state.strip().splitlines() if s.strip()):
        detail = " | ".join(l.strip() for l in jlog.strip().splitlines()[-3:])
        return False, f"un ptp4l n'est pas resté actif : {detail or 'voir journalctl'}"
    return True, "ok"


def stop_multi(host, groups=None):
    """Coupe le PTP. `groups` fourni → ne coupe que ces réseaux ; sinon coupe TOUTES les unités
    PTP gérées (reconcile vers l'ensemble vide, purge legacy comprise)."""
    if groups:
        units = []
        for g in groups:
            i = int(g["network_id"]); units += [_net_ptp4l_unit(i), _net_phc2sys_unit(i)]
        ssh_run(host, "systemctl disable --now " + " ".join(units) + " 2>&1 || true", timeout=20)
    else:
        _reconcile_units(host, set())
    return True, "ok"


def _pmc(host, dom, query, uds=None):
    """`pmc -u GET <query>` pour le domaine `dom`. `uds` = socket de management dédié (réseau
    multi-NIC, /var/run/ptp4l-net<id>) → ciblé via `-s` ; None = socket par défaut (mono / ptp4l
    plain). Renvoie (rc, stdout). Centralise le flag `-s`."""
    sock = f"-s {uds} " if uds else ""
    rc, out, _ = ssh_run(host, f"pmc -u {sock}-b 0 -d {int(dom)} '{query}' 2>&1", timeout=8)
    return rc, out


# ─── Résolution DÉRIVE réseau↔unité (audit A6 bis) ────────────────────────────
# Un `media_networks.id` peut se renuméroter (suppression/recréation d'un réseau 2110) sans
# que le nœud soit ré-appliqué (deploy_config_multi/start_multi jamais rejoués) : les unités
# systemd posées lors du dernier `apply` restent nommées `mxl-ptp4l-net<ANCIEN_id>` alors que
# la DB pointe maintenant vers un nouveau `network_id`. Constaté sur dl360Horace (nœud 31) :
# le réseau « 2110 Horace » a été recréé id 1 → 7, le nœud tourne toujours sur
# `mxl-ptp4l-net1.service` (SLAVE, verrouillé, offset ~qq ns) pendant que le sampler
# interroge `mxl-ptp4l-net7` (jamais posée) → « ptp4l inactif » → fausse alerte holdover
# après PTP_UNLOCK_ERR_S alors que l'horloge réelle est verrouillée.
#
# Fix : chemin rapide = l'unité `mxl-ptp4l-net<network_id>` directe si elle est active (cas
# nominal, aucun coût supplémentaire). Sinon on scanne les unités `mxl-ptp4l-net*.service`
# actives et on retient celle dont la conf déclare EXACTEMENT le même domaine ET le même
# ensemble d'interfaces que le groupe attendu (égalité stricte des deux — on ne veut pas
# confondre deux réseaux 2110 distincts qui partageraient un domaine). Un scan coûte quelques
# ssh_run ; on le cache par (host, network_id) avec un TTL court (UNIT_ALIAS_TTL_S) pour ne
# pas payer ce coût à chaque échantillon (5 s) tout en se ré-alignant vite après un vrai
# ré-apply qui recrée l'unité sous le bon nom. IMPORTANT : si rien ne matche (scan à vide),
# on retombe sur la référence DIRECTE — un ptp4l réellement arrêté doit continuer à déclencher
# l'alerte holdover, cette résolution ne doit jamais « aveugler » la détection d'un vrai down.
_UNIT_ALIAS_CACHE = {}     # (host, network_id) → (alias_network_id | None, ts)
_UNIT_ALIAS_LOCK  = threading.Lock()
_UNIT_ALIAS_LOGGED = set() # (host, network_id, alias_id) déjà loggés — 1 warning par dérive, pas par sample
UNIT_ALIAS_TTL_S  = 60.0


def _resolve_ptp4l_ref(host, net_id, domain, ifaces):
    """Renvoie (unit_ptp4l, unit_phc2sys, uds) de l'unité RÉELLE servant ce réseau logique
    (`net_id`/`domain`/`ifaces` = ce que la DB attend). Direct si actif, sinon alias résolu par
    scan (caché). Ne modifie jamais l'identité `network_id` restituée à l'appelant (l'historique
    et les événements PTP restent keyés sur le network_id logique — seule la commande sous-jacente
    change de cible)."""
    net_id = int(net_id)
    direct_unit = _net_ptp4l_unit(net_id)
    rc, out, _ = ssh_run(host, f"systemctl is-active {direct_unit} 2>&1 || true", timeout=8)
    if out.strip() == "active":
        return direct_unit, _net_phc2sys_unit(net_id), _net_uds(net_id)

    key = (host, net_id)
    now = time.time()
    with _UNIT_ALIAS_LOCK:
        cached = _UNIT_ALIAS_CACHE.get(key)
    if cached and now - cached[1] < UNIT_ALIAS_TTL_S:
        alias_id = cached[0]
        if alias_id is None:
            return direct_unit, _net_phc2sys_unit(net_id), _net_uds(net_id)
        rc2, out2, _ = ssh_run(host, f"systemctl is-active {_net_ptp4l_unit(alias_id)} 2>&1 || true", timeout=8)
        if out2.strip() == "active":
            return _net_ptp4l_unit(alias_id), _net_phc2sys_unit(alias_id), _net_uds(alias_id)
        # L'alias caché n'est plus actif non plus (double arrêt / rescan mérité) → on retombe
        # au direct pour laisser la fenêtre de cache expirer naturellement au prochain scan.
        return direct_unit, _net_phc2sys_unit(net_id), _net_uds(net_id)

    # Scan : unités mxl-ptp4l-net*.service actives, sauf la directe (déjà écartée ci-dessus).
    alias_id = None
    rc, out, _ = ssh_run(host, "systemctl list-units 'mxl-ptp4l-net*.service' --all --plain --no-legend 2>&1", timeout=10)
    ifaces_set = set(i for i in (ifaces or []) if i)
    if ifaces_set:
        cand_ids = []
        for line in out.strip().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            m = re.match(r"mxl-ptp4l-net(\d+)\.service$", parts[0])
            if m and parts[2] == "active":
                cand_ids.append(int(m.group(1)))
        for cid in cand_ids:
            if cid == net_id:
                continue
            rc2, conf, _ = ssh_run(host, f"cat {_net_conf_path(cid)} 2>/dev/null", timeout=8)
            if rc2 != 0 or not conf:
                continue
            m_dom = re.search(r"domainNumber\s+(\d+)", conf)
            cand_dom = int(m_dom.group(1)) if m_dom else None
            cand_ifaces = set(m.group(1) for m in re.finditer(r"(?m)^\[(\S+)\]", conf)) - {"global"}
            if cand_dom == int(domain) and cand_ifaces and cand_ifaces == ifaces_set:
                alias_id = cid
                # 1 warning par dérive (host, réseau, alias) — pas à chaque expiration du cache
                # (sampler 5 s → le même message re-partait toutes les ~60 s, log inondé).
                _lk = (host, net_id, cid)
                if _lk not in _UNIT_ALIAS_LOGGED:
                    _UNIT_ALIAS_LOGGED.add(_lk)
                    log.warning("PTP : réseau net%d (domaine %s, %s) résolu vers l'unité alias "
                                "mxl-ptp4l-net%d.service (dérive network_id↔unité, cf. re-apply "
                                "manquant sur %s)", net_id, domain, sorted(ifaces_set), cid, host)
                break
    with _UNIT_ALIAS_LOCK:
        _UNIT_ALIAS_CACHE[key] = (alias_id, now)
    if alias_id is not None:
        return _net_ptp4l_unit(alias_id), _net_phc2sys_unit(alias_id), _net_uds(alias_id)
    return direct_unit, _net_phc2sys_unit(net_id), _net_uds(net_id)


def _host_network_uds_for_domain(host, dom):
    """uds du ptp4l du nœud `host` servant le domaine `dom` en multi-NIC (réseau 2110), ou None.
    Ambigu si plusieurs réseaux partagent le domaine sur l'hôte → 1er (best-effort ; idéalement
    résoudre par l'iface du sender, TODO)."""
    try:
        from .database import db_get_node_by_host
        node = db_get_node_by_host(host)
        if not node:
            return None
        for g in groups_from_node_interfaces(node["id"]):
            if int(g["domain"]) == int(dom):
                # Résolution dérive network_id↔unité (cf. _resolve_ptp4l_ref) : l'uds « attendu »
                # peut ne plus exister si le réseau a été renuméroné sans re-apply sur le nœud.
                _u, _p, uds = _resolve_ptp4l_ref(host, int(g["network_id"]), int(g["domain"]),
                                                 g.get("ifaces"))
                return uds
    except Exception:
        pass
    return None


def status_multi(host, groups):
    """Statut PTP par domaine : {domains:[{domain, primary, ifaces, ptp4l_running,
    phc2sys_running, port_state, offset_ns, grandmaster_id, locked, …}], competing_timesync}.
    pmc ciblé sur l'uds du domaine via -s (chaque ptp4l a un uds dédié)."""
    out = {"domains": [], "competing_timesync": []}
    groups = [g for g in (groups or []) if g.get("ifaces")]
    if not groups:
        return out
    rc, o, _ = ssh_run(host, "systemctl is-active " + " ".join(COMPETING_TIMESYNC) + " 2>&1 || true", timeout=8)
    states = o.strip().splitlines()
    out["competing_timesync"] = [u for u, s in zip(COMPETING_TIMESYNC, states) if s.strip() == "active"]
    for g in groups:
        nid = int(g["network_id"]); d = int(g["domain"])
        # Unité/uds RÉELS : direct si `mxl-ptp4l-net<nid>` est actif, sinon alias résolu par
        # domaine+interfaces (dérive network_id↔unité, cf. _resolve_ptp4l_ref) — un ptp4l
        # vraiment arrêté continue de sortir "inactif" via ce même chemin.
        unit_ptp4l, unit_phc2sys, uds = _resolve_ptp4l_ref(host, nid, d, g.get("ifaces"))
        ds = {"network_id": nid, "name": g.get("name"), "domain": d,
              "primary": bool(g.get("primary")), "ifaces": g.get("ifaces", []),
              "ptp4l_running": False, "phc2sys_running": False, "port_state": None,
              "offset_ns": None, "mean_path_delay_ns": None, "grandmaster_id": None,
              "locked": False, "error": None, "phc2sys_state": None,
              "phc2sys_sys_offset_ns": None, "phc2sys_freq_ppb": None,
              "ifaces_state": {}}     # {ifname: SLAVE|PASSIVE|LISTENING|MASTER|…} par port
        rc, o, _ = ssh_run(host, f"systemctl is-active {unit_ptp4l} {unit_phc2sys} 2>&1 || true", timeout=8)
        ls = o.strip().splitlines()
        if len(ls) >= 1: ds["ptp4l_running"]   = ls[0].strip() == "active"
        if len(ls) >= 2: ds["phc2sys_running"] = ls[1].strip() == "active"
        if ds["phc2sys_running"]:
            rc, jl, _ = ssh_run(host, f"journalctl -u {unit_phc2sys} -n 1 -o cat 2>&1", timeout=8)
            if rc == 0:
                ds.update(_parse_phc2sys_log(jl))
        if not ds["ptp4l_running"]:
            ds["error"] = "ptp4l inactif"; out["domains"].append(ds); continue
        for q, parser in (("GET CURRENT_DATA_SET", _parse_current_dataset),
                          ("GET PARENT_DATA_SET",  _parse_parent_dataset),
                          # La QUALITÉ de la référence, pas seulement la qualité du verrou :
                          # sans ce jeu de données, un nœud verrouillé sur un grandmaster en
                          # roue libre est indistinguable d'un nœud verrouillé sur du GPS.
                          ("GET TIME_PROPERTIES_DATA_SET", _parse_time_properties)):
            rc, o = _pmc(host, d, q, uds=uds)
            if rc == 0:
                ds.update(parser(o))
        # PORT_DATA_SET : un ptp4l JBOD a N ports → pmc renvoie l'état de CHACUN (portIdentity
        # …-<n>). On mappe par index sur les ifaces (ordre des sections [iface] de la conf = ordre
        # de g["ifaces"]). port_state d'en-tête = SLAVE si présent (le port élu), sinon le 1er.
        rc, o = _pmc(host, d, "GET PORT_DATA_SET", uds=uds)
        if rc == 0:
            pstates = _parse_port_states(o)                      # {portnum -> state}
            ifs = g.get("ifaces", [])
            ds["ifaces_state"] = {ifs[pn - 1]: stt for pn, stt in pstates.items()
                                  if 0 <= pn - 1 < len(ifs)}
            vals = list(pstates.values())
            ds["port_state"] = ("SLAVE" if "SLAVE" in vals else (vals[0] if vals else None))
        if ds["port_state"] in ("SLAVE", "MASTER", "GRAND_MASTER", "PASSIVE"):
            off = ds.get("offset_ns")
            if off is not None and abs(off) < 1_000_000:
                ds["locked"] = True
            elif ds["port_state"] in ("MASTER", "GRAND_MASTER"):
                ds["locked"] = True
        out["domains"].append(ds)
    return out


def _aggregate_multi(multi):
    """Réduit un status_multi (par domaine) en un dict PLAT rétro-compatible avec status()
    mono. En-tête = domaine PRIMAIRE (celui qui discipline CLOCK_REALTIME) → c'est l'état que
    lisent les consommateurs historiques (badge home, page PTP). Le détail par domaine reste
    sous la clé `domains` (consommée par l'UI réseau multi-NIC, Phase C)."""
    doms = multi.get("domains", [])
    prim = next((d for d in doms if d.get("primary")), (doms[0] if doms else {}))
    return {
        "ptp4l_running":      all(d.get("ptp4l_running") for d in doms) if doms else False,
        "phc2sys_running":    bool(prim.get("phc2sys_running")),
        "port_state":         prim.get("port_state"),
        "offset_ns":          prim.get("offset_ns"),
        "mean_path_delay_ns": prim.get("mean_path_delay_ns"),
        "grandmaster_id":     prim.get("grandmaster_id"),
        "locked":             bool(prim.get("locked")),
        # Qualité de la RÉFÉRENCE (≠ qualité du verrou, cf. _parse_parent_dataset). Remontée au
        # niveau plat parce que c'est ce niveau que lisent le badge d'accueil et la page Horloges :
        # laisser ça uniquement dans `domains` reviendrait à ne le montrer nulle part.
        "gm_clock_class":     prim.get("gm_clock_class"),
        "gm_clock_accuracy":  prim.get("gm_clock_accuracy"),
        "utc_offset_valid":   prim.get("utc_offset_valid"),
        "gm_saine":           gm_reference_saine(prim)[0],
        "gm_raison":          gm_reference_saine(prim)[1],
        "error":              prim.get("error"),
        "phc2sys_state":         prim.get("phc2sys_state"),
        "phc2sys_sys_offset_ns": prim.get("phc2sys_sys_offset_ns"),
        "phc2sys_freq_ppb":      prim.get("phc2sys_freq_ppb"),
        "competing_timesync":    multi.get("competing_timesync", []),
        # État par-NIC agrégé sur tous les domaines : {ifname -> SLAVE|PASSIVE|LISTENING|…}.
        "port_states":           {k: v for dd in doms for k, v in (dd.get("ifaces_state") or {}).items()},
        "domains":               doms,
    }


def status_for_node(node_id, host, domain=None):
    """Statut PTP d'un nœud. Chemin MULTI-NIC si des groupes existent (node_interfaces :
    role=media2110 ∧ ptp_enabled), agrégé en dict plat rétro-compatible (+ clé `domains`) ;
    sinon repli sur le `status()` mono. Point d'entrée unique pour le sampler et les routes.

    Socle full-PF DPDK (port média en pmd=dpdk/vfio-pci) : plus de netdev kernel → plus de ptp4l →
    la source PTP est le client INTERNE libmtl du moteur 2110_io (:8080). On bascule AVANT le
    chemin ptp4l (les groupes seraient de toute façon vides : ptp_enabled OFF sur un port vfio)."""
    if node_id is not None:
        try:
            from . import docker_driver
            if docker_driver._has_dpdk_pf({"id": node_id}):
                return status_from_engine(node_id)
        except Exception as e:
            log.debug("status_for_node dpdk check (%s): %s", node_id, e)
    groups = groups_from_node_interfaces(node_id) if node_id is not None else []
    if not groups:
        from . import settings as st
        dom = domain if domain is not None else int(st.setting_for("ptp_domain", node_id) or 0)
        return status(host, dom)
    return _aggregate_multi(status_multi(host, groups))


# ─── Parsers pmc ─────────────────────────────────────────────

def _parse_phc2sys_log(line):
    """Dernière ligne de log phc2sys → état servo (PROD-008).
    Formats : 'CLOCK_REALTIME phc offset <ns> s<0|1|2> freq <ppb> delay <ns>'
    ou 'Waiting for ptp4l...'. s2 = verrouillé, s0/s1 = convergence."""
    out = {}
    if "Waiting for ptp4l" in line:
        out["phc2sys_state"] = "waiting"
        return out
    m = re.search(r"offset\s+(-?\d+)\s+s(\d)\s+freq\s+([+-]?\d+)", line)
    if m:
        out["phc2sys_sys_offset_ns"] = int(m.group(1))
        servo = int(m.group(2))
        out["phc2sys_freq_ppb"] = int(m.group(3))
        out["phc2sys_state"] = "locked" if servo == 2 else "converging"
    return out


def _parse_current_dataset(text):
    """offsetFromMaster en ns (pmc l'affiche en ns)."""
    out = {}
    m = re.search(r"offsetFromMaster\s+(-?\d+)", text)
    if m: out["offset_ns"] = int(m.group(1))
    m = re.search(r"meanPathDelay\s+(-?\d+)", text)
    if m: out["mean_path_delay_ns"] = int(m.group(1))
    return out


def _parse_parent_dataset(text):
    """grandmasterIdentity au format '001122.fffe.334455' → 'AA:BB:CC:FF:FE:DD:EE:FF', plus la
    QUALITÉ annoncée par ce grandmaster.

    `gm.ClockClass` est le champ le plus important de tout ce module et personne ne le lisait.
    Il dit si la référence est traçable à un temps réel : 6 = verrouillé sur une source primaire
    (GPS), 7 = holdover après l'avoir été, **≥ 128 = libre, traçable à rien**. Un esclave se
    verrouille à la nanoseconde sur un grandmaster à 248 exactement comme sur un à 6 — la qualité
    du VERROU ne dit rien sur la JUSTESSE de la référence. Mesuré sur Horace le 2026-07-28 :
    ptp4l à 75 ns rms, offsetFromMaster 121 ns, et 16,2 minutes d'écart avec l'UTC réel parce que
    le grandmaster élu était en roue libre (clockClass 248, `currentUtcOffsetValid 0`)."""
    out = {}
    m = re.search(r"grandmasterIdentity\s+([0-9a-fA-F.]+)", text)
    if m:
        raw = m.group(1).replace(".", "")
        if len(raw) == 16:
            out["grandmaster_id"] = ":".join(raw[i:i+2].upper() for i in range(0, 16, 2))
    m = re.search(r"gm\.ClockClass\s+(\d+)", text)
    if m:
        out["gm_clock_class"] = int(m.group(1))
    m = re.search(r"gm\.ClockAccuracy\s+(\S+)", text)
    if m:
        out["gm_clock_accuracy"] = m.group(1)
    return out


def _parse_time_properties(text):
    """TIME_PROPERTIES_DATA_SET : ce que le grandmaster dit de son propre rapport à l'UTC.

    `currentUtcOffsetValid 0` = « mon décalage TAI↔UTC n'est pas fiable » — le GM l'admet
    lui-même. Combiné à un clockClass ≥ 128, c'est la signature d'une horloge qui a gagné le
    BMCA faute de mieux et qui sert son propre temps."""
    out = {}
    m = re.search(r"currentUtcOffset\s+(-?\d+)", text)
    if m: out["utc_offset_s"] = int(m.group(1))
    for champ, cle in (("currentUtcOffsetValid", "utc_offset_valid"),
                       ("timeTraceable", "time_traceable"),
                       ("frequencyTraceable", "freq_traceable"),
                       ("ptpTimescale", "ptp_timescale")):
        m = re.search(r"%s\s+(\d+)" % champ, text)
        if m: out[cle] = bool(int(m.group(1)))
    return out


# Seuil normatif IEEE 1588 : au-delà, le grandmaster n'est traçable à AUCUNE référence de temps.
# Ce n'est pas un réglage — c'est la table de la norme (6/7 = traçable, 13/14 = application
# spécifique, 52/58/187/193 = dégradés, 248 = défaut libre, 255 = esclave seulement).
GM_CLOCK_CLASS_LIBRE = 128


def gm_reference_saine(ds):
    """Le grandmaster de ce domaine est-il une référence de temps DÉFENDABLE ?

    Renvoie (bool, raison). None/inconnu → True : on ne crie pas sur une absence de mesure, on ne
    conclut que sur ce qu'on a lu (même règle que partout ailleurs dans ce module)."""
    cc = ds.get("gm_clock_class")
    if cc is not None and cc >= GM_CLOCK_CLASS_LIBRE:
        return False, ("grandmaster en roue libre (clockClass %d) : l'horloge est stable mais sa "
                       "référence n'est traçable à aucun temps réel" % cc)
    if ds.get("utc_offset_valid") is False:
        return False, ("le grandmaster déclare son propre décalage UTC NON valide "
                       "(currentUtcOffsetValid 0)")
    return True, ""


def _parse_port_dataset(text):
    out = {}
    m = re.search(r"portState\s+(\w+)", text)
    if m: out["port_state"] = m.group(1)
    return out


def _parse_port_states(text):
    """`pmc GET PORT_DATA_SET` sur un ptp4l multi-port renvoie un bloc par port (portIdentity
    …-<n> + portState). Renvoie {numéro_de_port:int -> état}. Aligne portIdentity et portState
    par paires (un bloc = un portIdentity suivi d'un portState)."""
    nums   = re.findall(r"portIdentity\s+\S+-(\d+)", text)
    states = re.findall(r"portState\s+(\w+)", text)
    out = {}
    for n, s in zip(nums, states):
        try:
            out[int(n)] = s
        except ValueError:
            pass
    return out


def sdp_refclk_lines(host):
    """Renvoie les lignes a=ts-refclk et a=mediaclk à injecter dans un SDP 2110
    si PTP est sync, sinon chaîne vide. Format SMPTE 2110-10."""
    s = status(host)
    if not s.get("locked") or not s.get("grandmaster_id"):
        return ""
    # Récupère le domain configuré (depuis settings) pour l'annoncer dans le SDP
    from . import settings as st
    domain = int(st.get("ptp_domain") or 127)
    # RFC 7273 §4.8 : le GM s'écrit en TIRETS dans le SDP (AA-BB-…-HH), le dernier `:`
    # étant le séparateur du domaine. Le format interne `:` (UI, _detect_ptp_events)
    # reste inchangé — conversion au moment de fabriquer la ligne seulement.
    gm_sdp = s["grandmaster_id"].replace(":", "-")
    return (f"a=ts-refclk:ptp=IEEE1588-2008:{gm_sdp}:{domain}\r\n"
            f"a=mediaclk:direct=0\r\n")


# ─── Refclk PAR HÔTE (caché) — pour le SDP TX d'un sender (nœud du sender) ────
# Le SDP d'un sender 2110 doit annoncer le grandmaster du ptp4l qui discipline SON nœud
# (pas le proxmox_host global). On interroge ce nœud par SSH pmc et on cache la ligne par
# (host, domaine) avec un TTL court : pas de SSH à chaque GET de transportfile (et latence bornée).
_refclk_cache = {}                # (host, domain) -> (line, ts)
_refclk_lock = threading.Lock()
REFCLK_TTL_S = 10.0

def refclk_for_host(host, domain=None):
    """Ligne a=ts-refclk:ptp (+ a=mediaclk) du ptp4l de `host`, ou '' si non verrouillé /
    injoignable. Cachée par (host, domaine), TTL ~10 s. `domain` défaut = réglage ptp_domain.

    Interroge pmc DIRECTEMENT (pas status()) : le ptp4l d'un nœud MTL n'est PAS forcément l'unité
    systemd mxl-ptp4l gérée par l'orchestrateur (souvent un ptp4l plain préexistant) — on ne peut
    donc pas gater sur `systemctl is-active mxl-ptp4l`. pmc -u répond quel que soit le lanceur de
    ptp4l, tant qu'il sert le socket de management sur le domaine donné.

    PTP multi-NIC (Réseaux 2110) : un nœud basculé en multi sert le socket pmc sur
    `/var/run/ptp4l-net<id>` (uds dédié au réseau) — on résout l'uds du réseau servant ce domaine
    via `_host_network_uds_for_domain` (sinon socket par défaut pour le chemin mono / un ptp4l
    plain). Garantit que le SDP TX d'un sender multi-NIC garde sa ligne ts-refclk."""
    if not host:
        return ""
    from . import settings as st
    dom = int(domain if domain is not None else (st.get("ptp_domain") or 127))
    key = (host, dom)
    now = time.time()
    with _refclk_lock:
        ent = _refclk_cache.get(key)
        if ent and now - ent[1] < REFCLK_TTL_S:
            return ent[0]
    line = ""
    uds = _host_network_uds_for_domain(host, dom)
    try:
        rc, parent = _pmc(host, dom, "GET PARENT_DATA_SET", uds=uds)
        gm = _parse_parent_dataset(parent).get("grandmaster_id") if rc == 0 else None
        rc, port = _pmc(host, dom, "GET PORT_DATA_SET", uds=uds)
        state = _parse_port_dataset(port).get("port_state") if rc == 0 else None
        if gm and state in ("SLAVE", "MASTER", "GRAND_MASTER", "PASSIVE"):
            # RFC 7273 §4.8 : GM en tirets dans le SDP (le `:` final sépare le domaine).
            gm_sdp = gm.replace(":", "-")
            line = (f"a=ts-refclk:ptp=IEEE1588-2008:{gm_sdp}:{dom}\r\n"
                    f"a=mediaclk:direct=0\r\n")
    except Exception as e:
        log.debug("refclk_for_host(%s): %s", host, e)
    with _refclk_lock:
        _refclk_cache[key] = (line, now)
    return line


def refclk_from_engine(ip, domain=None):
    """Ligne a=ts-refclk:ptp (+ a=mediaclk) LUE DU MOTEUR (:8080 champ `ptp`) — repli quand le ptp4l
    kernel est absent (socle full-PF DPDK : le PTP est DANS libmtl, `pmc` ne voit rien → refclk_for_host
    renvoie ''). Le contrôleur relaie le grandmaster VU par le PTP interne (gm_identity déjà au format
    SDP AA-BB-…-HH, domain). '' si le GM est inconnu (aucun Announce) ou le moteur injoignable.
    Cachée par ip (TTL commun).

    ★ NE PAS GATER SUR `locked` : c'est le lock SERVO STRICT de libmtl (delta BRUT < 100 ns en
    continu). Il ne se déclenchait JAMAIS tant que l'asservissement en fréquence du PHC n'était pas
    compilé (corrigé le 2026-08-30, il s'arme désormais) — mais le SDP annonce une RÉFÉRENCE
    d'horloge, pas un certificat de convergence : le gater sur un seuil de qualité priverait de
    `a=ts-refclk` un moteur en cours de convergence, pourtant parfaitement légitime à émettre. Gater
    dessus rendait la ligne TOUJOURS vide sur tout le parc DPDK : les SDP TX partaient SANS
    `a=ts-refclk`, donc NON CONFORMES ST 2110-10, et un récepteur strict (EVS Neuron) refusait de
    s'abonner alors que le flux arrivait bien. Le SDP annonce la RÉFÉRENCE d'horloge, pas la
    précision du servo — même règle que `pmc GET PARENT_DATA_SET`, et même piège que celui déjà
    corrigé côté patch libmtl (gate `master_initialized`, pas `locked`). Le critère est donc :
    grandmaster CONNU (Announce reçu)."""
    if not ip:
        return ""
    from . import settings as st
    key = ("engine", ip)
    now = time.time()
    with _refclk_lock:
        ent = _refclk_cache.get(key)
        if ent and now - ent[1] < REFCLK_TTL_S:
            return ent[0]
    line = ""
    # ★★ DÉSACTIVÉ (2026-07-28) — `gm_identity` publié par le moteur N'EST PAS le grandmaster.
    # Le patch libmtl (plugins/2110_io/docker/patch_ptp_gm_export.py:53) exporte
    # `ptp->master_port_id.clock_identity`, c'est-à-dire la `sourcePortIdentity` de l'en-tête
    # Announce = le MAÎTRE IMMÉDIAT. Dès qu'une horloge de frontière est dans le chemin — le cas
    # normal en broadcast — c'est le BC qu'on annonce, pas le GM. Mesuré en prod : le Nexus est BC
    # (identité 4c:77:6d:ff:fe:fb:c6:03) et relaie le vrai GM 00:09:0d:ff:fe:01:14:d9 ; nos SDP
    # annonçaient le premier. Un récepteur strict (EVS Neuron) compare cette identité à SA
    # référence, ne la reconnaît pas, et REFUSE le flux — en le recevant parfaitement par ailleurs
    # (aucune perte, pacing indifférent, narrow comme wide).
    # Annoncer une référence FAUSSE est pire que n'en annoncer aucune : sans la ligne, le récepteur
    # accepte (comportement d'avant ce correctif, vérifié) ; avec une identité étrangère, il rejette.
    # Réactiver UNIQUEMENT quand le patch exportera le `grandmasterIdentity` du CORPS de l'Announce.
    return line
    try:
        import requests
        r = requests.get(f"http://{ip}:8080", timeout=2)
        ptp = (r.json() or {}).get("ptp") if r.status_code == 200 else None
        if ptp and ptp.get("gm_identity"):
            dom = int(domain if domain is not None
                      else (ptp.get("domain") if ptp.get("domain") is not None
                            else (st.get("ptp_domain") or 127)))
            line = (f"a=ts-refclk:ptp=IEEE1588-2008:{ptp['gm_identity']}:{dom}\r\n"
                    f"a=mediaclk:direct=0\r\n")
    except Exception as e:
        log.debug("refclk_from_engine(%s): %s", ip, e)
    with _refclk_lock:
        _refclk_cache[key] = (line, now)
    return line


# ─── PTP MOTEUR (libmtl) — nœud full-PF DPDK, plus de ptp4l kernel ───────────
# Socle narrow full-PF DPDK (cf. mémoire narrow-full-pf-dpdk-socle) : le port média E810 est
# bindé vfio-pci → plus de netdev kernel → plus de ptp4l/phc2sys → `pmc`/`status()` ne voient
# RIEN. La SEULE horloge PTP est le client PTP INTERNE de libmtl, exposé par le moteur 2110_io
# sur son :8080 (bloc `ptp` = {locked, offset_ns, gm_id, domain} ; absent/None si pas de PTP
# moteur). Ce chemin remplace status()/status_multi pour ces nœuds — dict plat COMPATIBLE (mêmes
# clés) + `engine_ptp=True` (l'UI affiche « PTP moteur (libmtl) » au lieu de « ptp4l inactif »).
_engine_status_cache = {}          # node_id → (status_dict, ts)
ENGINE_STATUS_TTL_S  = 3.0         # < SAMPLE_INTERVAL_S : le sampler relit une valeur fraîche


def _find_node_engine(node_id):
    """Le moteur 2110_io UNIQUE du nœud (docker MTL, hors sonde), ou None. Même prédicat que
    docker_driver.ensure_node_engine._engine()."""
    try:
        from .database import db_get_containers
        from .docker_compute import is_mtl_type, _type_of
        from .docker_driver import _is_probe_type
        return next((c for c in db_get_containers()
                     if c.get("node_id") == node_id
                     and is_mtl_type(_type_of(c)) and not _is_probe_type(_type_of(c))), None)
    except Exception:
        return None


def _norm_gm_id(raw):
    """Normalise un clock id de grandmaster vers le format ':' interne (comme _parse_parent_dataset).
    Tolère l'entrée SDP 'AA-BB-CC-FF-FE-DD-EE-FF', déjà-':' ou 'aabbcc.fffe.ddeeff'."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if "-" in s:
        s = s.replace("-", ":")
    elif "." in s:
        hexs = s.replace(".", "")
        if len(hexs) == 16:
            s = ":".join(hexs[i:i + 2] for i in range(0, 16, 2))
    return s.upper() or None


def status_from_engine(node_id):
    """Statut PTP d'un nœud DPDK LU DU MOTEUR 2110_io (:8080 bloc `ptp`). Renvoie un dict PLAT
    compatible status()/status_multi (mêmes clés + `domains`), avec `engine_ptp=True` et
    `ptp4l_running=False`. offset_ns (CORRIGÉ)/locked/synced/mean_path_delay_ns/raw_delta_ns/
    grandmaster_id proviennent du bloc `ptp` du PTP interne libmtl (:8080). Une entrée `domains` par
    réseau média DPDK (network_id) alimente l'historique/graphe par réseau SANS modifier
    record_sample. Nœud sans GM (ou moteur injoignable) → locked=False, port_state=None, `error`
    EXPLICITE (« en attente / non verrouillé ») — jamais un panneau vide ni un échec muet. Caché
    (TTL court)."""
    key = int(node_id)
    now = time.time()
    with _refclk_lock:
        ent = _engine_status_cache.get(key)
        if ent and now - ent[1] < ENGINE_STATUS_TTL_S:
            return dict(ent[0])

    from .database import db_get_node_interfaces
    # Réseaux média portés par un port DPDK (une entrée `domains` par réseau).
    net_ids = []
    for r in db_get_node_interfaces(node_id) or []:
        if r.get("role") != "media2110":
            continue
        if (r.get("pmd") or "").strip().lower() != "dpdk":
            continue
        nid = r.get("media_network_id")
        if nid is not None and int(nid) not in net_ids:
            net_ids.append(int(nid))
    try:
        from .database import db_get_media_networks
        _mn = {n["id"]: n for n in db_get_media_networks()}
    except Exception:
        _mn = {}

    # Lecture du bloc `ptp` du moteur (:8080), best-effort.
    ptp = None
    engine_sessions = None      # sessions RX+TX réellement actives dans le moteur (None = inconnu)
    engine = _find_node_engine(node_id)
    if engine:
        try:
            from .addressing import get_container_ip
            ip = get_container_ip(engine.get("vmid"))
            if ip:
                import requests
                r = requests.get(f"http://{ip}:8080", timeout=2)
                if r.status_code == 200:
                    _payload = r.json() or {}
                    ptp = _payload.get("ptp")
                    # Le client PTP de libmtl n'existe QUE si le daemon mtl_rx tourne, et le moteur ne
                    # le lance que s'il a au moins UNE session (controller.py, `if _mtl_proc is None
                    # and sessions`). Un moteur à 0 session n'a donc AUCUNE horloge — état à ne pas
                    # confondre avec un holdover, d'où la remontée du compte (cf. _clock_fault).
                    _rl = _payload.get("rl") or {}
                    engine_sessions = (int(_rl.get("rx_sessions") or 0)
                                       + int(_rl.get("tx_sessions") or 0))
        except Exception as e:
            log.debug("status_from_engine(%s): %s", node_id, e)

    locked    = bool(ptp.get("locked")) if ptp else False
    # `synced` = synchro RÉELLE au GM (GM connu + offset corrigé dispo) ; sur E810 DPDK le lock servo
    # STRICT de libmtl (`locked`, delta brut <100ns) reste souvent False alors que la synchro est bonne
    # (delta brut ~1,3µs) → c'est `synced` qui pilote l'état/badge, `locked` reste un détail technique.
    synced    = bool(ptp.get("synced")) if ptp else False
    offset_ns = ptp.get("offset_ns") if ptp else None          # offset CORRIGÉ (≈ offset from master ptp4l)
    mpd_ns    = ptp.get("path_delay_ns") if ptp else None      # mean path delay
    raw_delta = ptp.get("raw_delta_ns") if ptp else None       # delta brut (diagnostic du lock strict)
    gm        = _norm_gm_id(ptp.get("gm_id") or ptp.get("gm_identity")) if ptp else None
    eng_dom   = ptp.get("domain") if ptp else None
    # synchronisé au GM ⇒ le port suit le GM (esclave), même si le lock servo strict n'est pas armé.
    port_state = "SLAVE" if (synced or locked) else None

    if engine is None:
        error = "moteur 2110_io introuvable"
    elif ptp is None:
        error = "PTP moteur indisponible (:8080)"
    elif not gm:
        error = "PTP moteur : en attente d'un grandmaster (non verrouillé)"
    else:
        error = None

    def _dom_for(net_id):
        n = _mn.get(net_id)
        if n and n.get("domain") is not None:
            return int(n["domain"])
        return int(eng_dom) if eng_dom is not None else None

    domains = [{
        "network_id":     i,
        "name":           (_mn.get(i) or {}).get("name"),
        "domain":         _dom_for(i),
        "primary":        True,
        "ifaces":         [],
        "ptp4l_running":  False,
        "phc2sys_running": False,
        "engine_ptp":     True,
        "port_state":     port_state,
        "offset_ns":      offset_ns,
        "mean_path_delay_ns": mpd_ns,
        "grandmaster_id": gm,
        "locked":         locked,
        "synced":         synced,
        # Y a-t-il un client PTP DU TOUT (bloc `ptp` publié par le moteur) et combien de sessions le
        # moteur sert-il ? Sans ça, « pas de client PTP » et « client PTP désynchronisé » sont
        # indiscernables en aval — c'est ce qui faisait annoncer un holdover pour un moteur vide.
        "engine_ptp_client": bool(ptp),
        "engine_sessions":   engine_sessions,
        "raw_delta_ns":   raw_delta,
        "error":          error,
        "ifaces_state":   {},
        "phc2sys_state":  None,
        "phc2sys_sys_offset_ns": None,
        "phc2sys_freq_ppb":      None,
    } for i in net_ids]

    status_dict = {
        "ptp4l_running":   False,
        "phc2sys_running": False,
        "engine_ptp":      True,
        "engine_domain":   (int(eng_dom) if eng_dom is not None else None),
        "port_state":      port_state,
        "offset_ns":       offset_ns,
        "mean_path_delay_ns": mpd_ns,
        "grandmaster_id":  gm,
        "locked":          locked,
        "synced":          synced,
        "engine_ptp_client": bool(ptp),
        "engine_sessions":   engine_sessions,
        "raw_delta_ns":    raw_delta,
        "error":           error,
        "phc2sys_state":   None,
        "phc2sys_sys_offset_ns": None,
        "phc2sys_freq_ppb":      None,
        "competing_timesync":    [],
        "port_states":           {},
        "domains":               domains,
    }
    with _refclk_lock:
        _engine_status_cache[key] = (status_dict, now)
    return dict(status_dict)


# ─── Préflight Phase 1 (chantier DPDK, Lot A2) : bascule de la NIC PTP ────────

def render_switch_plan(node_id, new_iface):
    """RENDU PUR (aucune pose d'unité, aucun accès hôte) du plan de bascule PTP d'un nœud :
    contenu exact des unités ptp4l/phc2sys qui seraient posées si le flag `ptp_enabled`
    passait de la (des) NIC média actuelle(s) à `new_iface`, + checklist texte (PHC partagé
    E810 ? ts-refclk SDP inchangé ? pmd vfio ?).

    Réutilise les rendus mono existants (`_ptp4l_conf`/`_systemd_ptp4l`/`_systemd_phc2sys`)
    avec le profil du nœud (ptp_domain/ptp_hw_ts/ptp_client_only). Renvoie un dict :
    {new_iface, current_ifaces, domain, hw_ts, ptp4l_conf, ptp4l_unit, phc2sys_unit,
     checklist:[str]}."""
    from . import settings as st
    from .database import db_get_node_interfaces

    new_iface = (new_iface or "").strip()
    if not new_iface:
        raise ValueError("new_iface requis")

    rows = db_get_node_interfaces(node_id) or []
    by_ifname = {r.get("ifname"): r for r in rows if r.get("ifname")}
    current = sorted(r["ifname"] for r in rows if r.get("ptp_enabled") and r.get("ifname"))

    dom = _norm_domain(st.setting_for("ptp_domain", node_id) or 127)
    hw_ts_raw = st.setting_for("ptp_hw_ts", node_id)
    hw_ts = True if hw_ts_raw is None else bool(hw_ts_raw)
    co_raw = st.setting_for("ptp_client_only", node_id)
    client_only = True if co_raw is None else bool(co_raw)

    checklist = ["préflight seulement — AUCUNE unité posée, aucun accès hôte"]

    new_row = by_ifname.get(new_iface)
    if new_row is None:
        checklist.append(f"warning: {new_iface} absente de node_interfaces — PHC partagé et "
                         "rôle non vérifiables")
    else:
        if (new_row.get("pmd") or "af_xdp") == "dpdk":
            checklist.append(f"ERREUR: {new_iface} est en pmd=dpdk (vfio-pci) — plus de PHC "
                             "kernel, ptp4l impossible sur cette interface")
        if new_row.get("ptp_enabled"):
            checklist.append(f"note: {new_iface} porte déjà ptp_enabled=1 — bascule no-op")

    # PHC partagé ? Sur E810 bi-port les deux fonctions PCI (…:00.0/…:00.1) partagent UN PHC
    # (vérifié dl360-1, ethtool -T : clock 4 pour les deux ports). Comparaison sur le préfixe
    # domaine:bus:device du BDF.
    def _slot(row):
        pci = ((row or {}).get("pci") or "").strip().lower()
        return pci.rsplit(".", 1)[0] if "." in pci else None
    new_slot = _slot(new_row)
    shared = [c for c in current
              if c != new_iface and new_slot and _slot(by_ifname.get(c)) == new_slot]
    if shared:
        checklist.append(f"PHC partagé E810 : {new_iface} et {', '.join(shared)} sont deux "
                         "fonctions de la même carte → même horloge matérielle, phc2sys "
                         "continue de discipliner le même PHC (bascule sans re-convergence)")
    elif current and new_row is not None:
        checklist.append(f"PHC différent : {new_iface} n'est pas sur la même carte que "
                         f"{', '.join(current)} → nouveau servo phc2sys, re-convergence à "
                         "surveiller (offset < 1 µs avant de valider)")

    checklist.append(f"domaine PTP inchangé ({dom}) → la ligne SDP a=ts-refclk:ptp "
                     "(gmIdentity:domaine) reste identique tant que le grandmaster ne change "
                     "pas — à revérifier via refclk_for_host après bascule")
    if current:
        checklist.append(f"NIC PTP actuelle(s) : {', '.join(current)} — le flag ptp_enabled "
                         f"doit être retiré de ces lignes et posé sur {new_iface} "
                         "(node_interfaces), puis redéploiement des unités")
    else:
        checklist.append("aucune NIC ptp_enabled actuellement — première activation, "
                         "pas de bascule à proprement parler")
    checklist.append("après pose : vérifier lock stable (pmc PORT_DATA_SET = SLAVE, "
                     "|offset| < 1 µs) et cohabitation avec l'AF_XDP encore en place")

    return {
        "new_iface":      new_iface,
        "current_ifaces": current,
        "domain":         dom,
        "hw_ts":          hw_ts,
        "ptp4l_conf":     _ptp4l_conf(dom, client_only=client_only),
        "ptp4l_unit":     _systemd_ptp4l(new_iface, hw_ts),
        "phc2sys_unit":   _systemd_phc2sys(new_iface, hw_ts, dom),
        "checklist":      checklist,
    }


# ─── Heure CIVILE depuis une horloge de nœud TAI (affichage seulement) ────────
# Par conception (docs/reference/PTP_CLOCK.md), CLOCK_REALTIME des nœuds est disciplinée sur l'échelle PTP
# (TAI) — c'est la grille média, on n'y touche JAMAIS. L'heure civile affichée (timecode de la
# mire, horloge « PTP » du multiview) = horloge nœud − currentUtcOffset (37 s en 2026, bouge
# aux leap seconds). Plutôt que de figer 37 en dur, on MESURE l'écart nœud↔contrôleur (le
# contrôleur est en NTP/heure civile) arrondi à la seconde ENTIÈRE — auto-recalibré au prochain
# leap second, et nul si un nœud est resté en heure civile. Cache par nœud (TTL 1 h).

_utc_off_cache = {}   # node_id → (offset_s, monotonic)

def node_clock_utc_offset_s(node, default=37, ttl_s=3600.0):
    """Offset (s, entier ≥ 0) entre l'horloge du nœud et l'heure civile du contrôleur.
    Mesure encadrée (aller-retour < 1 s exigé) ; hors-borne/injoignable → `default`."""
    node_id = node.get("id")
    c = _utc_off_cache.get(node_id)
    now = time.monotonic()
    if c and now - c[1] < ttl_s:
        return c[0]
    off = default
    try:
        from . import node_driver
        t0 = time.time()
        rc, out, _err = node_driver.host_exec(node, "date +%s.%N", timeout=5)
        t1 = time.time()
        if rc == 0 and (t1 - t0) < 1.0:
            d = float(str(out).strip().split()[0]) - (t0 + t1) / 2.0
            r = int(round(d))
            if abs(d - r) < 0.3 and 0 <= r <= 120:
                off = r
    except Exception:
        pass
    _utc_off_cache[node_id] = (off, now)
    return off


def _controller_tz_name():
    """Nom du fuseau du SYSTÈME (ex. Europe/Paris). Le réglage `timezone` FAIT FOI quand il est
    posé — c'est le levier unique voulu (journaux, UI, conteneurs) ; on ne retombe sur le fuseau de
    l'OS du contrôleur (/etc/timezone, sinon /etc/localtime) que s'il est vide."""
    try:
        from . import settings as _settings
        _tz = (_settings.get("timezone") or "").strip()
        if _tz:
            return _tz
    except Exception:
        pass
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            tz = f.read().strip()
            if tz:
                return tz
    except Exception:
        pass
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except Exception:
        pass
    return ""


def civil_clock_params(vmid):
    """{tz, tai_utc_offset_s} pour un container : fuseau du contrôleur (les images runtime
    sont en UTC) + offset TAI mesuré de l'horloge du nœud du container. Consommé par les hooks
    before_deploy des générateurs/afficheurs d'heure civile (avsync, multiview)."""
    out = {}
    tz = _controller_tz_name()
    if tz:
        out["tz"] = tz
    try:
        from .database import get_db
        with get_db() as db:
            row = db.execute("SELECT node_id FROM containers WHERE vmid=?", (vmid,)).fetchone()
            node = dict(db.execute("SELECT * FROM nodes WHERE id=?",
                                   (row["node_id"],)).fetchone()) if row and row["node_id"] else None
        if node:
            out["tai_utc_offset_s"] = node_clock_utc_offset_s(node)
    except Exception:
        pass
    return out
