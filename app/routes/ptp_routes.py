# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""PTP (IEEE 1588 / SMPTE 2059-2) par nœud : historique/stats échantillonnés, journal
d'événements (bascules de port, grandmaster, lock, service), tail des logs ptp4l/phc2sys,
état live (services + offset + master), installation (apt) et application des réglages.

_eff_node_id/_req_node_id/_req_host restent dans __init__.py (génériques, utilisés bien
au-delà du PTP) — importés localement. _ptp_apply_core vit dans app/routes/shared.py
(partagé avec app/routes/node_network.py)."""

from flask import jsonify, request

from . import bp
from .shared import _ptp_apply_core
from ..auth import require_login, require_perm
from ..addressing import node_host as _node_host, primary_host as _primary_host


@bp.route("/api/ptp/history", methods=["GET"])
@require_login
def ptp_history():
    """Historique échantillonné des métriques PTP (offset/mpd/locked) sur ~10 min,
    pour afficher le graphique dès le chargement de la page."""
    from . import _eff_node_id
    from .. import ptp
    nid = _eff_node_id()
    # ?network=<id> → historique/stat d'un RÉSEAU précis (multi-NIC) ; sinon node-niveau (primaire).
    _net = request.args.get("network")
    net_id = int(_net) if (_net not in (None, "")) else None
    return jsonify({
        "interval_s": ptp.SAMPLE_INTERVAL_S,
        "span_s":     ptp.HISTORY_SECONDS,
        "samples":    ptp.get_history(nid, net_id),
        "stats_24h":  ptp.get_stats_24h(nid, net_id),
    })

@bp.route("/api/ptp/events", methods=["GET"])
@require_login
def ptp_events():
    """Journal d'événements PTP persisté (bascules de port, grandmaster, lock, service).
    Filtrable par nœud (node_id) / réseau (network) / recherche (q)."""
    from . import _req_node_id
    from ..database import db_get_ptp_events
    node_id = _req_node_id()
    _net = request.args.get("network")
    net_id = int(_net) if (_net not in (None, "")) else None
    q = (request.args.get("q") or "").strip() or None
    try:
        limit = max(1, min(int(request.args.get("limit", 1000)), 5000))
    except ValueError:
        limit = 1000
    return jsonify(db_get_ptp_events(node_id=node_id, network_id=net_id, q=q, limit=limit))

@bp.route("/api/ptp/log-sources", methods=["GET"])
@require_login
def ptp_log_sources():
    """Sources de logs « journal » disponibles : pour chaque nœud avec des groupes PTP, une entrée
    ptp4l + une phc2sys PAR RÉSEAU. Le front préfixe les sources statiques (alertes, événements)."""
    from .. import ptp
    from ..database import db_get_nodes
    out = []
    for n in db_get_nodes() or []:
        if not n.get("host"):
            continue
        nname = n.get("name") or n.get("host")
        for g in ptp.groups_from_node_interfaces(n["id"]) or []:
            net_id, net_name, dom = g.get("network_id"), g.get("name"), g.get("domain")
            for unit in ("ptp4l", "phc2sys"):
                out.append({
                    "value": f"j:{n['id']}:{unit}:{net_id}",
                    "label": f"{unit} — {nname} / {net_name} (d{dom})",
                    "kind": "journal", "node_id": n["id"], "unit": unit, "network_id": net_id,
                })
    return jsonify({"ok": True, "sources": out})

@bp.route("/api/ptp/logs", methods=["GET"])
@require_login
def ptp_logs():
    """Tail à la demande du journal d'un démon PTP d'un nœud (ptp4l/phc2sys d'un réseau), via
    l'agent du nœud (ssh_run → /v1/host/exec). Lecture seule. Unité construite à partir d'IDs int
    (pas d'injection)."""
    from . import _req_node_id
    from .. import ptp
    from ..host_ops import ssh_run
    node_id = _req_node_id()
    host = _node_host(node_id)
    if not host:
        return jsonify({"ok": False, "error": "nœud introuvable / sans hôte"}), 404
    unit_kind = request.args.get("unit")
    try:
        net_id = int(request.args.get("network"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "réseau invalide"}), 400
    if unit_kind == "ptp4l":
        unit = ptp._net_ptp4l_unit(net_id)
    elif unit_kind == "phc2sys":
        unit = ptp._net_phc2sys_unit(net_id)
    else:
        return jsonify({"ok": False, "error": "unit invalide (ptp4l|phc2sys)"}), 400
    try:
        lines = max(10, min(int(request.args.get("lines", 300)), 2000))
    except ValueError:
        lines = 300
    rc, out, err = ssh_run(host, f"journalctl -u {unit} -n {lines} --no-pager -o short-iso 2>&1", timeout=20)
    if rc != 0:
        return jsonify({"ok": False, "error": (out or err or "").strip()[:300] or f"rc={rc}"}), 502
    return jsonify({"ok": True, "unit": unit, "lines": (out or "").splitlines()})

@bp.route("/api/ptp/status", methods=["GET"])
@require_login
def ptp_status():
    """État live du démon PTP du nœud sélectionné (services + offset + master)."""
    from . import _eff_node_id
    from .. import settings as st
    from .. import ptp
    nid  = _eff_node_id()
    host = _node_host(nid) or _primary_host()
    sf   = lambda k: st.setting_for(k, nid)   # réglage PTP par-nœud (override > global > défaut)
    s = ptp.status_for_node(nid, host, int(sf("ptp_domain") or 0)) if host else {"ptp4l_running": False}
    s["settings"] = {
        "enabled":      bool(sf("ptp_enabled")),
        "ifname":       sf("ptp_ifname") or "",
        "domain":       int(sf("ptp_domain") or 127),
        "hw_ts":        bool(sf("ptp_hw_ts")),
        "client_only":  bool(sf("ptp_client_only")),
        "priority1":    int(sf("ptp_priority1") or 128),
        "priority2":    int(sf("ptp_priority2") or 128),
        "log_announce": int(sf("ptp_log_announce") if sf("ptp_log_announce") is not None else 0),
        "log_sync":     int(sf("ptp_log_sync") or -3),
        "log_delay_req":int(sf("ptp_log_delay_req") or -3),
        "announce_to":  int(sf("ptp_announce_to") or 3),
        "delay_thresh": int(sf("ptp_delay_thresh") or 800),
        "utc_offset":   int(sf("ptp_utc_offset") or 37),
    }
    s["installed"] = ptp.is_installed(host) if host else False
    s["host"] = host
    s["node_id"] = nid
    s["stats_24h"] = ptp.get_stats_24h(nid)
    # PTP multi-NIC : si le nœud a des NIC media2110 PTP (node_interfaces), l'interface unique de
    # cette page est sans objet (les NIC + domaines sont réglés dans Vue d'ensemble). On expose les
    # groupes pour que l'UI masque le champ interface et affiche la liste à la place.
    _grp = ptp.groups_from_node_interfaces(nid) if nid is not None else []
    s["multi"] = bool(_grp)
    s["ptp_networks"] = [{"network_id": g["network_id"], "name": g["name"], "domain": g["domain"],
                          "ifaces": g["ifaces"], "ptp_params": g.get("ptp_params") or {}} for g in _grp]
    # Valeurs par défaut SMPTE 2059-2 (valeurs initiales du formulaire réseau ; un réseau ne hérite
    # pas du nœud).
    s["ptp_defaults"] = dict(ptp.SMPTE_DEFAULTS)
    return jsonify(s)

@bp.route("/api/ptp/install", methods=["POST"])
@require_perm("settings.edit")
def ptp_install():
    """apt install linuxptp sur l'host (idempotent)."""
    from . import _req_host
    from .. import ptp
    ok, msg = ptp.install(_req_host())
    return jsonify({"ok": ok, "msg": msg})

# _ptp_apply_core extrait dans app/routes/shared.py (partagé par app/routes/node_network.py).

@bp.route("/api/ptp/apply", methods=["POST"])
@require_perm("settings.edit")
def ptp_apply():
    """Persiste les settings + déploie config + start/stop selon enabled."""
    from . import _eff_node_id
    ok, msg, code = _ptp_apply_core(_eff_node_id(), request.json or {})
    return jsonify({"ok": True, "msg": msg} if ok else {"ok": False, "error": msg}), code
