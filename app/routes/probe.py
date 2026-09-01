# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Sonde ST 2110 (probe_2110) — analyseur ponctuel piloté par NMOS (Phase A).

« Un receiver dont le but est la mesure » : la sonde est un type MATÉRIEL MTL (needs_dpdk) qui
RÉUTILISE l'image bobi-mtl en mode RX-only + parser de conformité 2110-21 (TIMING_PARSER=1). Elle
tourne sur SA PROPRE PF vfio DÉDIÉE (jamais celle du moteur). Ce module porte :

  * GET  /probe                       — page dédiée (sélecteur de flux IS-04 + rapport live)
  * GET  /api/probe/engines           — sondes déployées + rapport + PF candidates par nœud
  * GET  /api/probe/senders           — flux NMOS abonnables (registre IS-04)
  * POST /api/probe/deploy            — déployer une sonde (nœud + PF dédiée)
  * POST /api/probe/<vmid>/subscribe  — braquer la sonde sur un flux (SDP → IS-05 manual_subscribe)
  * POST /api/probe/<vmid>/unsubscribe— libérer un slot
  * GET  /api/probe/<vmid>/report     — rapport live (:8080) : conformité + transport

AUCUN code de plugin n'est exécuté ici : on réutilise la chaîne IS-05 existante
(services.nmos.manual_subscribe, identique au moteur 2110_io) et le contrôleur baké dans bobi-mtl.
"""

import json
import logging

import requests
from flask import jsonify, render_template, request

from . import bp
from ..auth import require_login, require_perm
from ..database import (db_add_alert, db_get_container, db_get_containers,
                        db_get_node, db_get_node_interfaces)

log = logging.getLogger(__name__)

PROBE_TYPE = "probe_2110"


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _load_dc(c):
    try:
        return json.loads(c.get("deploy_config") or "{}")
    except Exception:
        return {}


def _is_probe(c):
    return (_load_dc(c).get("type") == PROBE_TYPE)


def _node_has_engine(node_id):
    """Vrai si un moteur 2110_io est présent sur ce nœud (il réclame TOUTES les PF média du
    nœud → aucune PF n'est réellement « libre » pour une sonde tant qu'il est là, cf. --network
    host + budget de files). Sert au garde-fou « PF dédiée, jamais celle du moteur »."""
    for c in db_get_containers():
        if c.get("node_id") == node_id and _load_dc(c).get("type") == "2110_io":
            return True
    return False


def _candidate_ifaces(node_id):
    """PF média (node_interfaces role=media2110) candidates pour une sonde sur ce nœud, chacune
    marquée `busy` si un moteur 2110_io occupe le nœud. La conformité (Cinst/VRX) exige le HW
    timestamp → PF en pmd=dpdk/vfio (af_xdp = transport/contenu seulement) : on le signale."""
    busy = _node_has_engine(node_id)
    out = []
    for r in db_get_node_interfaces(node_id):
        if (r.get("role") or "") != "media2110":
            continue
        pmd = (r.get("pmd") or "af_xdp").strip() or "af_xdp"
        out.append({
            "ifname": r.get("ifname"),
            "pci": r.get("pci") or "",
            "pmd": pmd,
            "ip": r.get("ip") or "",
            "conformance_ready": pmd == "dpdk",   # HW timestamp → verdict Cinst/VRX fiable
            "busy": busy,                          # PF réclamée par le moteur du nœud
        })
    return out


def _read_report(vmid):
    """Lit le rapport :8080 du contrôleur bobi-mtl de la sonde (verdict conformité + transport).
    Réutilise metrics.get_metrics (même normalisation que le reste de la flotte)."""
    from ..addressing import get_container_ip
    from ..metrics import get_metrics
    from ..deploy import controller_port_base
    ip = get_container_ip(vmid)
    if not ip:
        return {"ok": False, "error": "IP de la sonde introuvable", "receivers": []}
    data = get_metrics(ip, port=controller_port_base(vmid)) or {}
    data["ok"] = True
    return data


def _fetch_local_sdp(sender_id):
    """Récupère le SDP (transportfile IS-05) d'un sender LOCAL depuis l'API x-nmos servie en
    propre par le service NMOS (même origine). None si indisponible."""
    try:
        from services import nmos as _nmos
        href = "/x-nmos/connection/{}/single/senders/{}/transportfile".format(
            _nmos.IS05_VERSION, sender_id)
    except Exception:
        return None
    for base in ("http://127.0.0.1:5000", "http://127.0.0.1:8080"):
        try:
            r = requests.get(base + href, timeout=3)
            if r.status_code == 200 and r.text.strip():
                return r.text
        except Exception:
            continue
    return None


def _list_senders():
    """Flux NMOS abonnables (vidéo) du registre IS-04. Deux origines :
      * LOCAUX — moteurs 2110_io TX (via _compute_senders_detail, code en dépôt) : SDP résolu par
        le transportfile de notre service NMOS.
      * REGISTRE — tout sender enregistré (senders distants découverts inclus), best-effort depuis
        `services.nmos._senders`. On expose leur manifest_href pour l'abonnement.
    """
    senders = []
    seen = set()
    # 1) Senders locaux (2110_io) — chemin fiable, entièrement en dépôt.
    try:
        from .nmos_detail import _compute_senders_detail
        for blk in _compute_senders_detail():
            vmid = blk.get("vmid")
            host = blk.get("hostname")
            for s in blk.get("senders", []):
                if s.get("essence") not in (None, "video"):
                    continue
                sid = s.get("id")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                senders.append({
                    "id": sid,
                    "label": s.get("label") or host,
                    "hostname": host,
                    "vmid": vmid,
                    "origin": "local",
                    "multicast_ip": s.get("multicast_ip"),
                    "port": s.get("destination_port"),
                    "width": s.get("width"), "height": s.get("height"),
                    "scan": s.get("scan"),
                })
    except Exception as e:
        log.warning("probe: liste senders locaux échouée: %s", e)
    # 2) Registre complet (best-effort) : senders sans notre tag vmid = découverts/distants.
    try:
        from services import nmos as _nmos
        with _nmos._lock:
            snap = list(_nmos._senders.values())
        for s in snap:
            sid = s.get("id")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            senders.append({
                "id": sid,
                "label": s.get("label") or sid[:8],
                "hostname": (s.get("tags") or {}).get("urn:x-nmos:tag:grouphint", [""])[0] or "",
                "vmid": None,
                "origin": "registry",
                "manifest_href": s.get("manifest_href") or "",
                "multicast_ip": None, "port": None,
            })
    except Exception as e:
        log.debug("probe: registre NMOS complet indisponible (%s)", e)
    return senders


# ─── Page ────────────────────────────────────────────────────────────────────
@bp.route("/probe")
@require_login
def probe_page():
    from .. import plugins
    return render_template("probe.html", page="probe",
                           probe_available=bool(plugins.get(PROBE_TYPE)))


# ─── API ─────────────────────────────────────────────────────────────────────
@bp.route("/api/probe/engines", methods=["GET"])
@require_login
def api_probe_engines():
    """Sondes déployées (+ rapport live) et, par nœud, les PF candidates pour en déployer une."""
    probes = []
    for c in db_get_containers():
        if not _is_probe(c):
            continue
        vmid = c["vmid"]
        dc = _load_dc(c)
        params = dc.get("params") or {}
        node = db_get_node(c.get("node_id")) or {}
        probes.append({
            "vmid": vmid,
            "hostname": c.get("hostname") or f"#{vmid}",
            "node": node.get("name") or node.get("host") or "",
            "node_id": c.get("node_id"),
            "status": c.get("status"),
            "probe_iface": params.get("probe_iface") or "",
            "measure_audio": bool(params.get("measure_audio")),
            "report": _read_report(vmid),
        })
    # PF candidates par nœud (pour le déploiement d'une nouvelle sonde).
    nodes = []
    from ..database import db_get_nodes
    for n in db_get_nodes():
        ifaces = _candidate_ifaces(n["id"])
        if not ifaces:
            continue
        nodes.append({"node_id": n["id"],
                      "name": n.get("name") or n.get("host") or f"#{n['id']}",
                      "ifaces": ifaces})
    return jsonify({"probes": probes, "nodes": nodes})


@bp.route("/api/probe/senders", methods=["GET"])
@require_login
def api_probe_senders():
    """Flux NMOS abonnables (registre IS-04)."""
    return jsonify({"senders": _list_senders()})


@bp.route("/api/probe/deploy", methods=["POST"])
@require_perm("containers.deploy")
def api_probe_deploy():
    """Déploie une sonde sur un nœud, liée à une PF média DÉDIÉE (garde-fou : PF déclarée,
    libre — jamais celle d'un moteur 2110_io présent). Réutilise le chemin MTL (docker_driver)."""
    from .. import docker_driver, plugins
    data = request.get_json(force=True, silent=True) or {}
    node_id = data.get("node_id")
    iface = (data.get("probe_iface") or "").strip()
    if not node_id or not iface:
        return jsonify({"ok": False, "error": "node_id et probe_iface requis"}), 400
    if not plugins.get(PROBE_TYPE):
        return jsonify({"ok": False, "error": "plugin probe_2110 absent du registre"}), 400
    cand = {i["ifname"]: i for i in _candidate_ifaces(int(node_id))}
    sel = cand.get(iface)
    if not sel:
        return jsonify({"ok": False, "error": f"« {iface} » n'est pas une PF média (media2110) de ce nœud"}), 400
    if sel["busy"]:
        return jsonify({"ok": False, "error": "PF occupée par un moteur 2110_io — la sonde exige une PF "
                        "DÉDIÉE distincte du moteur (déployer sur un nœud/port libre)."}), 409
    params = dict((plugins.get(PROBE_TYPE) or {}).get("deploy_defaults") or {})
    params["probe_iface"] = iface
    params["measure_audio"] = bool(data.get("measure_audio"))
    params = plugins.coerce_config(PROBE_TYPE, params)

    import threading

    def _run():
        vmid = docker_driver.creer_container_docker(int(node_id), hostname=data.get("hostname"),
                                                    deploy_type=PROBE_TYPE)
        if vmid:
            docker_driver.deploy_docker(vmid, params, type_script=PROBE_TYPE)

    threading.Thread(target=_run, daemon=True).start()
    db_add_alert("alert.deploy.sonde_en_cours", "info", kind="deploy", params={"iface": iface})
    return jsonify({"ok": True, "status": "deploy_en_cours"})


@bp.route("/api/probe/<int:vmid>/subscribe", methods=["POST"])
@require_perm("containers.deploy")
def api_probe_subscribe(vmid):
    """Braque la sonde sur un flux : { sdp } (canonique) OU { sender_id } (SDP résolu depuis le
    transportfile local). Réutilise la chaîne IS-05 du moteur (services.nmos.manual_subscribe)."""
    c = db_get_container(vmid)
    if not c or not _is_probe(c):
        return jsonify({"ok": False, "error": "sonde introuvable"}), 404
    data = request.get_json(force=True, silent=True) or {}
    slot = int(data.get("slot") or 0)
    essence = data.get("essence") or "video"
    sdp = (data.get("sdp") or "").strip()
    if not sdp and data.get("sender_id"):
        sdp = (_fetch_local_sdp(data["sender_id"]) or "").strip()
    if not sdp:
        return jsonify({"ok": False, "error": "SDP indisponible (fournir { sdp } ou un sender_id local)"}), 400
    try:
        from services import nmos as _nmos
        code, res = _nmos.manual_subscribe(vmid, slot, essence, sdp, enable=True)
    except Exception as e:
        return jsonify({"ok": False, "error": f"abonnement IS-05 échoué : {e}"}), 500
    if code != 200:
        return jsonify({"ok": False, "error": (res or {}).get("error", "échec")}), code
    db_add_alert("alert.deploy.sonde_abonnee", "info", vmid=vmid, kind="deploy",
                 params={"vmid": vmid, "slot": slot})
    return jsonify({"ok": True})


@bp.route("/api/probe/<int:vmid>/unsubscribe", methods=["POST"])
@require_perm("containers.deploy")
def api_probe_unsubscribe(vmid):
    c = db_get_container(vmid)
    if not c or not _is_probe(c):
        return jsonify({"ok": False, "error": "sonde introuvable"}), 404
    data = request.get_json(force=True, silent=True) or {}
    slot = int(data.get("slot") or 0)
    essence = data.get("essence") or "video"
    try:
        from services import nmos as _nmos
        code, res = _nmos.manual_subscribe(vmid, slot, essence, None, enable=False)
    except Exception as e:
        return jsonify({"ok": False, "error": f"désabonnement échoué : {e}"}), 500
    if code != 200:
        return jsonify({"ok": False, "error": (res or {}).get("error", "échec")}), code
    return jsonify({"ok": True})


@bp.route("/api/probe/<int:vmid>/report", methods=["GET"])
@require_login
def api_probe_report(vmid):
    """Rapport live de la sonde (:8080) : verdict conformité (compliant/failed_cause/cinst/vrx/
    vrx_span/fpt/latency) + transport. Le verdict ABSOLU (compliant) exige PTP ; sans grandmaster,
    lire Cinst + vrx_span (invariants à la dérive)."""
    c = db_get_container(vmid)
    if not c or not _is_probe(c):
        return jsonify({"ok": False, "error": "sonde introuvable"}), 404
    return jsonify(_read_report(vmid))


# ─── Phase B : monitoring longue durée + journal d'événements ────────────────
@bp.route("/api/probe/events", methods=["GET"])
@require_login
def api_probe_events():
    """Timeline d'incidents (probe_events), récents d'abord. Filtres query : vmid, flow, kind,
    severity (info|warning|error), active (1 = incidents non refermés), limit."""
    from .. import probe_monitor
    args = request.args
    def _int(k):
        v = args.get(k)
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    events = probe_monitor.db_get_probe_events(
        vmid=_int("vmid"),
        flow=(args.get("flow") or None),
        kind=(args.get("kind") or None),
        severity=(args.get("severity") or None),
        active_only=(args.get("active") in ("1", "true", "yes")),
        limit=_int("limit") or 500)
    return jsonify({"events": events})


@bp.route("/api/probe/monitor", methods=["GET"])
@require_login
def api_probe_monitor():
    """Instantané du tableau de bord monitoring : incidents actifs + timeline récente +
    signaux de production surveillés en continu."""
    from .. import probe_monitor
    return jsonify(probe_monitor.monitor_summary(limit=int(request.args.get("limit") or 200)))


@bp.route("/api/probe/watch", methods=["POST"])
@require_perm("containers.deploy")
def api_probe_watch():
    """Marque (ou démarque) un signal de production (receiver 2110_io) comme surveillé en continu
    par le moteur d'événements : { vmid, idx?, essence?, label?, on? }. Les sondes probe_2110 sont
    surveillées d'office (pas besoin de les inscrire ici)."""
    from .. import probe_monitor
    data = request.get_json(force=True, silent=True) or {}
    vmid = data.get("vmid")
    if vmid is None:
        return jsonify({"ok": False, "error": "vmid requis"}), 400
    watched = probe_monitor.watch(
        vmid, idx=data.get("idx") or 0, essence=data.get("essence") or "video",
        label=data.get("label") or "", on=bool(data.get("on", True)))
    return jsonify({"ok": True, "watched": watched})
