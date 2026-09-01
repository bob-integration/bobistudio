# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Moteur d'événements + sampler de la sonde ST 2110 (probe_2110) — Phase B.

Surveillance longue durée des signaux mesurés : on interroge périodiquement le rapport `:8080`
(offsetté par conteneur, cf. `deploy.controller_port_base`) de CHAQUE sonde `probe_2110` déployée
— et, optionnellement, des receivers `2110_io` de PRODUCTION marqués « surveillés » (le journal
d'événements est une capacité PARTAGÉE, cf. docs/reference/PROBE_2110.md §Architecture). On applique des SEUILS
aux métriques de conformité / transport / contenu et on émet des **événements à transition**
(entrée → sortie, horodatés) :

  * conformité : narrow→wide = warning, →failed = error (+ cause) ;
  * fps 0 / gel (freeze) = error, noir (black) = warning, silence audio = warning ;
  * erreur de session RX (rx_error) = error ;
  * pertes transport (rx_hw_dropped / rx_nombuf) au-delà d'un seuil = error ;
  * perte PTP (unlock / offset hors seuil) sur le nœud de la sonde = warning ;
  * sonde injoignable = warning.

Chaque événement est écrit dans la table `probe_events` (ts_start/ts_end, vmid, flow, kind,
severity, message, value) ET signalé sur les fronts via `ajouter_alerte`. Anti-rebond
(hystérésis) : N échantillons consécutifs avant d'OUVRIR, N avant de FERMER — pattern
`app/node_health.py` (_vfio_frozen_cnt) / `app/ptp.py`. Modèle du sampler = node_health.sample_all.

Périmètre : ce module ne touche PAS `database.py` (la table `probe_events` est déjà posée) — les
helpers db_add/close/get vivent ici avec `get_db()`. Aucun code de plugin n'est exécuté in-process
(on ne lit que le rapport HTTP du contrôleur baké dans l'image).
"""

import json
import logging
import threading
import time
from datetime import datetime

from .database import get_db, db_get_container, db_get_containers, db_get_node
from .containers import ajouter_alerte
from . import settings as S

log = logging.getLogger(__name__)

PROBE_TYPE = "probe_2110"
ENGINE_TYPE = "2110_io"

# ─── Rétention du journal (modèle db_add_ptp_event) ──────────────────────────
PROBE_EVENTS_RETENTION = 5000
PROBE_EVENTS_PURGE_MARGIN = 500

# ─── Cadence / seuils par défaut (surchargeables en settings) ────────────────
SAMPLE_INTERVAL_S = 5      # probemon_interval_s
ENTER_SAMPLES     = 2      # probemon_enter_samples — anti-rebond à l'OUVERTURE
EXIT_SAMPLES      = 2      # probemon_exit_samples  — anti-rebond à la FERMETURE
LOSS_PPS          = 5.0    # probemon_loss_pps — pertes transport (paquets/s) tolérées
PTP_OFFSET_NS     = 1000   # probemon_ptp_offset_ns — |offset| PTP toléré (ns)
UNREACH_SAMPLES   = 3      # échantillons injoignables avant l'événement « sonde muette »

SETTING_WATCHED = "probemon_watched"   # JSON : signaux de prod surveillés en continu

# ─── État en mémoire (process contrôleur) ────────────────────────────────────
_lock = threading.Lock()
_last_sample_m = 0.0
# FSM binaire par (vmid, flow, kind) : {"cand": int, "active": bool, "id": event_id|None}
_fsm = {}
# FSM conformité par (vmid, flow) : verdict stable + candidat débouncé + event ouvert
_conf = {}
# Compteurs cumulés pour les deltas transport : (vmid, port) → {"drop": int, "ts": float}
_loss_prev = {}
# Compteurs cumulés « late » par flux : (vmid, flow) → int
_late_prev = {}


# ─── Réglages ────────────────────────────────────────────────────────────────
def _cfg(key, default):
    try:
        v = S.get(key)
        return type(default)(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _now():
    return datetime.now().isoformat(timespec="milliseconds")


# ─── Persistance : helpers probe_events (get_db, PAS d'édition de database.py) ─
def _val(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def db_add_probe_event(vmid, flow, kind, severity, message, value=None):
    """Ouvre un incident dans le journal (ts_end NULL = toujours actif). Renvoie son id."""
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO probe_events (vmid, flow, kind, severity, message, value, ts_start, ts_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (vmid, flow, kind, severity, message, _val(value), _now()))
        n = db.execute("SELECT COUNT(*) FROM probe_events").fetchone()[0]
        if n > PROBE_EVENTS_RETENTION + PROBE_EVENTS_PURGE_MARGIN:
            db.execute("DELETE FROM probe_events WHERE id NOT IN "
                       "(SELECT id FROM probe_events ORDER BY id DESC LIMIT ?)",
                       (PROBE_EVENTS_RETENTION,))
        db.commit()
        return cur.lastrowid


def db_close_probe_event(event_id):
    """Ferme un incident (pose ts_end). No-op s'il est déjà fermé / inconnu."""
    if not event_id:
        return
    with get_db() as db:
        db.execute("UPDATE probe_events SET ts_end=? WHERE id=? AND ts_end IS NULL",
                   (_now(), event_id))
        db.commit()


def db_get_probe_events(vmid=None, flow=None, kind=None, severity=None,
                        active_only=False, since_ts=None, limit=500):
    """Timeline filtrable (récents d'abord). `active_only` = incidents non refermés (ts_end NULL)."""
    sql = "SELECT * FROM probe_events"
    where, params = [], []
    if vmid is not None:
        where.append("vmid = ?"); params.append(vmid)
    if flow:
        where.append("flow = ?"); params.append(flow)
    if kind:
        where.append("kind = ?"); params.append(kind)
    if severity:
        where.append("severity = ?"); params.append(severity)
    if active_only:
        where.append("ts_end IS NULL")
    if since_ts:
        where.append("ts_start >= ?"); params.append(since_ts)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with get_db() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


# ─── Registre des signaux de PROD surveillés (persisté en settings) ───────────
def get_watched():
    """Liste des signaux de production explicitement mis sous surveillance longue durée
    (receivers 2110_io hors sonde). [{vmid, idx, essence, label}]."""
    raw = S.get(SETTING_WATCHED)
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, list) else []
    except Exception:
        return []


def set_watched(entries):
    S.set(SETTING_WATCHED, json.dumps(entries, ensure_ascii=False))


def watch(vmid, idx=0, essence="video", label="", on=True):
    """Active/désactive la surveillance continue d'un signal de prod. Renvoie la liste à jour."""
    vmid = int(vmid); idx = int(idx); essence = essence or "video"
    cur = [w for w in get_watched()
           if not (int(w.get("vmid")) == vmid and int(w.get("idx", 0)) == idx
                   and (w.get("essence") or "video") == essence)]
    if on:
        cur.append({"vmid": vmid, "idx": idx, "essence": essence, "label": label or ""})
    set_watched(cur)
    return cur


# ─── Moteur d'événements : FSM binaire à hystérésis ──────────────────────────
def _binary(vmid, flow, kind, cond, severity, msg_on, msg_off, value=None, alert=True,
            msg_on_i18n=None, msg_off_i18n=None):
    """Événement à transition sur une CONDITION booléenne (noir, gel, silence, fps 0, pertes…).
    Ouvre après ENTER_SAMPLES vrais consécutifs, ferme après EXIT_SAMPLES faux consécutifs.

    `msg_on`/`msg_off` : phrase FR déjà rendue, persistée telle quelle dans `probe_events` (table
    non i18n). `msg_on_i18n`/`msg_off_i18n` : tuple `(clé, params)` optionnel pour le fil d'alertes
    — quand fourni, c'est LUI qui part vers `ajouter_alerte` (rendu dans la langue du lecteur),
    `msg_on`/`msg_off` restant réservés à `probe_events`."""
    enter_n = max(1, int(_cfg("probemon_enter_samples", ENTER_SAMPLES)))
    exit_n = max(1, int(_cfg("probemon_exit_samples", EXIT_SAMPLES)))
    key = (vmid, flow, kind)
    st = _fsm.setdefault(key, {"cand": 0, "active": False, "id": None})
    if cond:
        if st["active"]:
            st["cand"] = 0
        else:
            st["cand"] += 1
            if st["cand"] >= enter_n:
                st["active"] = True
                st["cand"] = 0
                st["id"] = db_add_probe_event(vmid, flow, kind, severity, msg_on, value)
                if alert:
                    if msg_on_i18n:
                        ajouter_alerte(msg_on_i18n[0], severity, params=msg_on_i18n[1])
                    else:
                        ajouter_alerte(msg_on, severity)
    else:
        if not st["active"]:
            st["cand"] = 0
        else:
            st["cand"] += 1
            if st["cand"] >= exit_n:
                db_close_probe_event(st["id"])
                st["active"] = False
                st["cand"] = 0
                st["id"] = None
                if alert and msg_off:
                    if msg_off_i18n:
                        ajouter_alerte(msg_off_i18n[0], "info", params=msg_off_i18n[1])
                    else:
                        ajouter_alerte(msg_off, "info")


def _conformance(vmid, flow, verdict, cause):
    """Événement à transition sur le VERDICT 2110-21 (narrow/wide/failed), débouncé.
    narrow = régime stable (aucun événement) ; wide = warning ; failed = error (+ cause).
    Une bascule wide→failed ferme l'incident wide et en ouvre un neuf (escalade de sévérité)."""
    enter_n = max(1, int(_cfg("probemon_enter_samples", ENTER_SAMPLES)))
    verdict = (verdict or "").lower()
    if verdict not in ("narrow", "wide", "failed"):
        return
    key = (vmid, flow)
    st = _conf.setdefault(key, {"stable": "narrow", "cand": None, "cand_n": 0, "id": None})
    # Débounce : il faut enter_n échantillons consécutifs du même verdict pour le rendre « stable ».
    if verdict == st["cand"]:
        st["cand_n"] += 1
    else:
        st["cand"] = verdict
        st["cand_n"] = 1
    if st["cand_n"] < enter_n or verdict == st["stable"]:
        return
    # Transition confirmée : fermer l'incident courant s'il existe, ouvrir le nouveau si anormal.
    if st["id"]:
        db_close_probe_event(st["id"]); st["id"] = None
    prev = st["stable"]
    st["stable"] = verdict
    if verdict == "narrow":
        if prev != "narrow":
            ajouter_alerte("alert.rx.retour_narrow", "info", params={"flow": flow})
        return
    if verdict == "wide":
        msg = f"Sonde {flow} : sortie du gabarit narrow → WIDE (2110-21)."
        sev = "warning"
        val = {"verdict": "wide"}
        alert_key, alert_params = "alert.rx.sortie_narrow_wide", {"flow": flow}
    else:  # failed
        c = (cause or "").strip()
        msg = f"Sonde {flow} : flux NON CONFORME (2110-21 failed{': ' + c if c else ''})."
        sev = "error"
        val = {"verdict": "failed", "cause": c}
        if c:
            alert_key, alert_params = "alert.rx.non_conforme_avec_cause", {"flow": flow, "cause": c}
        else:
            alert_key, alert_params = "alert.rx.non_conforme_sans_cause", {"flow": flow}
    st["id"] = db_add_probe_event(vmid, flow, "conformance", sev, msg, val)
    ajouter_alerte(alert_key, sev, params=alert_params)


# ─── Lecture du rapport :8080 d'un conteneur (sonde ou moteur) ───────────────
def _read_report(vmid):
    try:
        from .addressing import get_container_ip
        from .metrics import get_metrics
        from .deploy import controller_port_base
        ip = get_container_ip(vmid)
        if not ip:
            return None
        return get_metrics(ip, port=controller_port_base(vmid)) or None
    except Exception as e:
        log.debug("probe_monitor: rapport %s indisponible: %s", vmid, e)
        return None


def _flow_id(vmid, rec):
    idx = rec.get("idx")
    ess = rec.get("essence") or "video"
    return f"#{vmid}/{idx}/{ess}"


def _eval_receiver(vmid, rec):
    """Applique les seuils à UN flux (une entrée receivers[] du rapport) et pilote les FSM."""
    ess = rec.get("essence") or "video"
    mode = rec.get("mode")
    if mode == "idle":              # non abonné / générateur off → rien à journaliser
        return
    flow = _flow_id(vmid, rec)
    sig = rec.get("signal") or {}
    try:
        fps = float(rec.get("fps") or 0.0)
    except (TypeError, ValueError):
        fps = 0.0

    # Erreur de session RX (budget lcores/files, RTP alignment…) — le plus grave.
    _rx_detail = rec.get("rx_error") or "session RX en échec"
    _binary(vmid, flow, "rx_error", mode == "error", "error",
            f"Sonde {flow} : erreur de réception — {_rx_detail}.",
            f"Sonde {flow} : réception rétablie.",
            value=rec.get("rx_error"),
            msg_on_i18n=("alert.rx.erreur_reception", {"flow": flow, "detail": _rx_detail}),
            msg_off_i18n=("alert.rx.reception_retablie", {"flow": flow}))

    if ess in (None, "video", "anc"):
        # Perte de signal : session MTL vivante mais 0 fps (aucune trame reconstruite).
        _binary(vmid, flow, "no_signal", mode == "mtl" and fps <= 0.0, "error",
                f"Sonde {flow} : plus de trames vidéo (fps 0) alors que la session est active.",
                f"Sonde {flow} : trames vidéo de nouveau reçues.", value={"fps": fps},
                msg_on_i18n=("alert.rx.plus_de_trames", {"flow": flow}),
                msg_off_i18n=("alert.rx.trames_retablies", {"flow": flow}))
        # Contenu (exposé par le contrôleur via _signal_loop, persistance SIGNAL_HOLD_S côté moteur).
        _binary(vmid, flow, "freeze", bool(sig.get("frozen")), "error",
                f"Sonde {flow} : image GELÉE (freeze) détectée.",
                f"Sonde {flow} : image de nouveau animée.",
                msg_on_i18n=("alert.rx.image_gelee", {"flow": flow}),
                msg_off_i18n=("alert.rx.image_animee", {"flow": flow}))
        _binary(vmid, flow, "black", bool(sig.get("black")), "warning",
                f"Sonde {flow} : image NOIRE (black) détectée.",
                f"Sonde {flow} : image de nouveau présente.",
                msg_on_i18n=("alert.rx.image_noire", {"flow": flow}),
                msg_off_i18n=("alert.rx.image_presente", {"flow": flow}))
        # Conformité 2110-21 (présente seulement si TIMING_PARSER=1 → DPDK/vfio).
        if rec.get("compliant") is not None:
            _conformance(vmid, flow, rec.get("compliant"), rec.get("failed_cause"))

    if ess == "audio":
        _binary(vmid, flow, "silence", bool(sig.get("silence")), "warning",
                f"Sonde {flow} : SILENCE audio détecté.",
                f"Sonde {flow} : audio de nouveau présent.",
                msg_on_i18n=("alert.rx.silence_audio", {"flow": flow}),
                msg_off_i18n=("alert.rx.audio_present", {"flow": flow}))


def _eval_transport(vmid, report):
    """Pertes transport agrégées PAR PORT (rx_hw_dropped + rx_nombuf, compteurs cumulés libmtl).
    Événement si le débit de pertes dépasse le seuil probemon_loss_pps (paquets/s)."""
    ports = ((report.get("nic") or {}).get("ports")) or []
    thr = float(_cfg("probemon_loss_pps", LOSS_PPS))
    now = time.time()
    for p in ports:
        st = p.get("mtl_stats") or {}
        pname = p.get("iface") or st.get("port")
        if not pname:
            continue
        try:
            drop = int(st.get("rx_hw_dropped") or 0) + int(st.get("rx_nombuf") or 0)
        except (TypeError, ValueError):
            continue
        key = (vmid, pname)
        prev = _loss_prev.get(key)
        _loss_prev[key] = {"drop": drop, "ts": now}
        rate = 0.0
        over = False
        if prev and now > prev["ts"] and drop >= prev["drop"]:
            rate = (drop - prev["drop"]) / (now - prev["ts"])
            over = rate > thr
        flow = f"#{vmid}/port/{pname}"
        _binary(vmid, flow, "loss", over, "error",
                f"Sonde {flow} : pertes transport {rate:.0f} pq/s (> {thr:.0f}) — "
                f"réseau amont / switch à vérifier.",
                f"Sonde {flow} : pertes transport rentrées sous le seuil.",
                value={"pps": round(rate, 1)},
                msg_on_i18n=("alert.rx.pertes_transport", {"flow": flow, "rate": rate, "thr": thr}),
                msg_off_i18n=("alert.rx.pertes_transport_ok", {"flow": flow}))


def _eval_ptp(vmid, node_id):
    """Perte PTP sur le nœud de la sonde (le verdict conformité ABSOLU en dépend, cf. docs/reference/PROBE_2110.md).
    Ne s'exécute que si PTP est activé pour le nœud ET qu'un relevé autoritaire existe."""
    try:
        if not S.setting_for("ptp_enabled", node_id):
            return
        from . import ptp
        s = ptp.cached_status(node_id)
        if not s:
            return
        off = s.get("offset_ns")
        thr = float(_cfg("probemon_ptp_offset_ns", PTP_OFFSET_NS))
        # `ptp.clock_ok` et non `locked` : sur un nœud full-PF DPDK `locked` est le verrou servo
        # STRICT de libmtl, jamais armé sur E810 — la sonde déclarait « PTP perdu » en permanence.
        synced = ptp.clock_ok(s)
        lost = (not synced) or (off is not None and abs(off) > thr)
        flow = f"#{vmid}/ptp"
        _binary(vmid, flow, "ptp", lost, "warning",
                f"Sonde #{vmid} : PTP perdu/désaligné (synchro={synced}, "
                f"offset={off} ns) — verdict de conformité absolu non fiable.",
                f"Sonde #{vmid} : PTP de nouveau verrouillé.",
                value={"locked": synced, "offset_ns": off},
                msg_on_i18n=("alert.ptp.sonde_perdu",
                             {"vmid": vmid, "locked": synced, "offset_ns": off}),
                msg_off_i18n=("alert.ptp.sonde_retablie", {"vmid": vmid}))
    except Exception as e:
        log.debug("probe_monitor ptp %s: %s", vmid, e)


def _targets():
    """Signaux à surveiller : (a) TOUTES les sondes probe_2110 déployées ; (b) les receivers
    2110_io de prod explicitement marqués « surveillés » (registre settings)."""
    probes, engines = [], {}
    for c in db_get_containers() or []:
        try:
            dc = json.loads(c["deploy_config"]) if isinstance(c.get("deploy_config"), str) \
                else (c.get("deploy_config") or {})
        except Exception:
            dc = {}
        t = dc.get("type")
        if t == PROBE_TYPE:
            probes.append(c)
        elif t == ENGINE_TYPE:
            engines[int(c["vmid"])] = c
    watched = {}
    for w in get_watched():
        try:
            vmid = int(w.get("vmid"))
        except (TypeError, ValueError):
            continue
        if vmid in engines:
            watched.setdefault(vmid, set()).add((int(w.get("idx", 0)), w.get("essence") or "video"))
    return probes, engines, watched


def _sample_probe(c):
    vmid = int(c["vmid"])
    node_id = c.get("node_id")
    report = _read_report(vmid)
    # Sonde injoignable : événement à transition (warning) — hystérésis dédiée UNREACH_SAMPLES
    # (plus tolérante que l'hystérésis générique : un rapport :8080 peut manquer un tick).
    unreach_n = max(1, int(_cfg("probemon_unreach_samples", UNREACH_SAMPLES)))
    key = (vmid, f"#{vmid}", "unreachable")
    st = _fsm.setdefault(key, {"cand": 0, "active": False, "id": None})
    ok = bool(report and isinstance(report.get("receivers"), list))
    if not ok:
        if not st["active"]:
            st["cand"] += 1
            if st["cand"] >= unreach_n:
                st["active"] = True; st["cand"] = 0
                st["id"] = db_add_probe_event(vmid, f"#{vmid}", "unreachable", "warning",
                                              f"Sonde #{vmid} : rapport :8080 injoignable.", None)
                ajouter_alerte("alert.rx.sonde_injoignable", "warning", params={"vmid": vmid})
        return
    if st["active"]:
        db_close_probe_event(st["id"])
        st["active"] = False; st["cand"] = 0; st["id"] = None
        ajouter_alerte("alert.rx.sonde_accessible", "info", params={"vmid": vmid})
    else:
        st["cand"] = 0

    for rec in report.get("receivers") or []:
        try:
            _eval_receiver(vmid, rec)
        except Exception as e:
            log.debug("probe_monitor eval receiver %s: %s", vmid, e)
    try:
        _eval_transport(vmid, report)
    except Exception as e:
        log.debug("probe_monitor eval transport %s: %s", vmid, e)
    try:
        _eval_ptp(vmid, node_id)
    except Exception as e:
        log.debug("probe_monitor eval ptp %s: %s", vmid, e)


def _sample_watched_engine(c, slots):
    """Journalise UNIQUEMENT les flux marqués surveillés d'un moteur 2110_io de prod (pas de
    conformité en général : moteur af_xdp sans HW timestamp → seuls transport+contenu comptent)."""
    vmid = int(c["vmid"])
    report = _read_report(vmid)
    if not report:
        return
    for rec in report.get("receivers") or []:
        pair = (int(rec.get("idx", 0)), rec.get("essence") or "video")
        if pair in slots:
            try:
                _eval_receiver(vmid, rec)
            except Exception as e:
                log.debug("probe_monitor watched %s: %s", vmid, e)


# ─── Sampler (appelé depuis surveillance, throttlé) ──────────────────────────
def sample_all(force=False):
    global _last_sample_m
    if not _cfg("probemon_enabled", 1):
        return
    interval = _cfg("probemon_interval_s", SAMPLE_INTERVAL_S)
    now_m = time.monotonic()
    if not force and (now_m - _last_sample_m) < interval:
        return
    _last_sample_m = now_m
    try:
        probes, engines, watched = _targets()
    except Exception as e:
        log.debug("probe_monitor targets: %s", e)
        return
    for c in probes:
        try:
            _sample_probe(c)
        except Exception as e:
            log.debug("probe_monitor sonde %s: %s", c.get("vmid"), e)
    for vmid, slots in watched.items():
        c = engines.get(vmid)
        if c:
            try:
                _sample_watched_engine(c, slots)
            except Exception as e:
                log.debug("probe_monitor moteur surveillé %s: %s", vmid, e)


# ─── Accès API (résumé pour le tableau de bord) ──────────────────────────────
def monitor_summary(limit=200):
    """Instantané pour l'onglet Monitoring : incidents actifs, timeline récente, signaux surveillés."""
    return {
        "active": db_get_probe_events(active_only=True, limit=limit),
        "recent": db_get_probe_events(limit=limit),
        "watched": get_watched(),
    }
