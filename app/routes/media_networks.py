# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""« Réseaux 2110 » — CRUD global (cluster) : identités de réseau logique (nom, domaine PTP,
surcharges SMPTE 2059-2), agrégat d'état PTP live par réseau, topologie du cluster (Vue
d'ensemble)."""

import json

from flask import jsonify, request

from . import bp
from ..auth import require_login, require_perm


@bp.route("/api/media-networks", methods=["GET"])
@require_login
def media_networks_list():
    from ..database import db_get_media_networks, db_media_network_in_use
    nets = db_get_media_networks()
    for n in nets:
        n["in_use"] = db_media_network_in_use(n["id"])
    return jsonify({"ok": True, "networks": nets})


def _coerce_net_ptp_params(raw):
    """ptp_params d'un réseau (colonne TEXT JSON) → dict. Tolère None / déjà-dict / chaîne JSON."""
    if isinstance(raw, dict):
        return raw
    if raw:
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


@bp.route("/api/media-networks/overview", methods=["GET"])
@require_login
def media_networks_overview():
    """Agrégat CLUSTER par réseau 2110 : pour chaque réseau, la liste de ses NIC membres groupées
    par nœud, avec l'état PTP live (port SLAVE/STANDBY, offset, grandmaster, phc2sys). Lit le CACHE
    sampler (`ptp.cached_status`, pas de SSH) → sûr en polling. Membres = node_interfaces dont
    media_network_id == réseau (indépendant de l'état PTP)."""
    from ..database import db_get_media_networks, db_media_network_in_use, db_get_nodes, db_get_node_interfaces
    from .. import settings as st
    from .. import ptp
    nets = db_get_media_networks()
    nodes = db_get_nodes() or []
    # Cache des status par nœud (un seul appel cached_status par nœud) + interfaces par nœud.
    node_status = {n["id"]: (ptp.cached_status(n["id"]) or {}) for n in nodes}
    node_ifaces = {n["id"]: db_get_node_interfaces(n["id"]) for n in nodes}
    out = []
    for net in nets:
        nid = net["id"]
        net_nodes = []
        grandmaster = None
        for node in nodes:
            members = [i for i in node_ifaces.get(node["id"], [])
                       if i.get("media_network_id") == nid]
            if not members:
                continue
            cs = node_status.get(node["id"], {})
            dom = next((d for d in (cs.get("domains") or []) if d.get("network_id") == nid), {})
            ist = dom.get("ifaces_state") or {}
            if dom.get("grandmaster_id") and not grandmaster:
                grandmaster = dom["grandmaster_id"]
            slave_if = next((nm for nm in ist if ist[nm] == "SLAVE"), None)
            # Nœud full-PF DPDK : pas de ptp_enabled kernel, mais PTP porté par le moteur libmtl
            # (dom.engine_ptp). On le compte comme participant (has_ptp) pour qu'il apparaisse dans
            # les sélecteurs de graphe et affiche son état verrou/offset.
            engine_ptp = bool(dom.get("engine_ptp"))
            has_ptp = any(m.get("ptp_enabled") for m in members) or engine_ptp
            net_nodes.append({
                "node_id": node["id"], "node_name": node.get("name") or node.get("host"),
                "primary": str(st.setting_for("ptp_primary_network", node["id"]) or "") == str(nid),
                "ptp4l_running": bool(dom.get("ptp4l_running")),
                "engine_ptp": engine_ptp,
                # ⚠ `locked` seul ne suffit PAS à décider si l'horloge va bien : c'est le verrou
                # servo STRICT de libmtl, qui pouvait rester faux (cf. ptp.clock_ok, dont la
                # docstring raconte l'alarme « holdover » criée sur un nœud parfaitement
                # synchronisé). La carte n'avait que `locked` et affichait donc « en attente d'un
                # grandmaster » à côté de l'identifiant DUDIT grandmaster et de l'offset mesuré
                # contre lui. On envoie le verdict CANONIQUE : le front n'a pas à re-dériver une
                # doctrine qui vit déjà ici.
                "locked": bool(dom.get("locked")),
                "synced": bool(dom.get("synced")),
                "sync_ok": ptp.clock_ok(dom),
                "phc2sys_state": dom.get("phc2sys_state"),
                "phc2sys_offset_ns": dom.get("phc2sys_sys_offset_ns"),
                "offset_ns": dom.get("offset_ns"),
                "slave_iface": slave_if,
                # has_ptp = au moins une NIC ptp_enabled → ce nœud participe au PTP de ce réseau
                # (sinon « Appliquer » n'a rien à démarrer). needs_apply (front) = has_ptp ∧ ¬ptp4l_running.
                "has_ptp": has_ptp,
                "members": [{"ifname": m["ifname"], "pair_role": m.get("pair_role"),
                             "ptp_enabled": bool(m.get("ptp_enabled")),
                             "port_state": ist.get(m["ifname"])} for m in members],
            })
        out.append({
            "id": nid, "name": net["name"], "domain": net["domain"],
            "ptp_params": _coerce_net_ptp_params(net.get("ptp_params")),
            "in_use": db_media_network_in_use(nid), "grandmaster": grandmaster,
            "nodes": net_nodes,
        })
    return jsonify({"ok": True, "networks": out, "defaults": dict(ptp.SMPTE_DEFAULTS)})


@bp.route("/api/cluster/topology", methods=["GET"])
@require_login
def cluster_topology():
    """Topologie du cluster pour la carte graphique (Vue d'ensemble) : l'orchestrateur, les
    réseaux 2110, et chaque nœud avec ses interfaces (rôle, IP, appartenance réseau). Lecture
    seule (DB + IP locale best-effort)."""
    import socket
    from ..database import db_get_nodes, db_get_node_interfaces, db_get_media_networks
    try:
        orch_ip = socket.gethostbyname(socket.getfqdn())
    except Exception:
        orch_ip = None
    # Débit live par interface (cache node_health, pas de SSH). RoCE bypasse la pile kernel → le
    # débit RDMA réel vient du sampler IB (cf. bloc rdma plus bas), pas de net[] ; ici = ports kernel.
    try:
        from .. import node_health
        _nh = (node_health.latest() or {}).get("nodes") or {}
    except Exception:
        _nh = {}
    nodes = []
    for n in db_get_nodes() or []:
        ifs = db_get_node_interfaces(n["id"]) or []
        net = ((_nh.get(str(n["id"])) or _nh.get(n["id"]) or {}).get("net")) or {}
        nodes.append({
            "id": n["id"], "name": n.get("name") or n.get("host"), "host": n.get("host"),
            "kind": n.get("kind"), "capabilities": n.get("capabilities"),
            "interfaces": [{
                "ifname": i.get("ifname"), "role": i.get("role"),
                "pair_role": i.get("pair_role"), "ip_cidr": i.get("ip_cidr"),
                "media_network_id": i.get("media_network_id"),
                "rx_bps": (net.get(i.get("ifname")) or {}).get("rx_bps"),
                "tx_bps": (net.get(i.get("ifname")) or {}).get("tx_bps"),
            } for i in ifs],
        })
    networks = [{"id": m["id"], "name": m["name"], "domain": m["domain"]}
                for m in db_get_media_networks()]
    # Plan RDMA : état des NIC rôle rdma + classification du raccordement (direct vs switch via LLDP).
    try:
        from services import rdma as _rdma
        rdma_topo = _rdma.rdma_topology()
    except Exception:
        rdma_topo = {"nics": {}, "edges": [], "lldp_seen": False}
    return jsonify({"ok": True,
                    "orchestrator": {"name": socket.gethostname(), "ip": orch_ip},
                    "networks": networks, "nodes": nodes, "rdma": rdma_topo})

# Surcharges PTP de profil par réseau : bornes de validation (mêmes que la page PTP nœud).
_NET_PTP_PARAM_RANGES = {
    "priority1": (0, 255), "priority2": (0, 255), "log_announce": (-3, 4),
    "log_sync": (-7, 0), "log_delay_req": (-7, 0), "announce_timeout": (2, 10),
    "delay_thresh": (100, 100000), "utc_offset": (0, 255),
}
def _ptp_params_from_payload(raw):
    """Dict {clé→valeur} des surcharges PTP valides/bornées (vide/None ignoré → hérite). client_only
    en bool. Renvoie {} si aucune surcharge."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, (lo, hi) in _NET_PTP_PARAM_RANGES.items():
        v = raw.get(k)
        if v is None or v == "":
            continue
        try:
            out[k] = max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            continue
    if raw.get("client_only") not in (None, ""):
        out["client_only"] = bool(raw.get("client_only"))
    return out

@bp.route("/api/media-networks", methods=["POST"])
@require_perm("settings.edit")
def media_networks_add():
    from ..database import db_add_media_network
    d = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "nom requis"}), 400
    try:
        domain = int(d.get("domain"))
        assert 0 <= domain <= 127
    except (TypeError, ValueError, AssertionError):
        return jsonify({"error": "domaine invalide (0-127)"}), 400
    nid = db_add_media_network(name, domain, _ptp_params_from_payload(d.get("ptp_params")) or None)
    return jsonify({"ok": True, "id": nid})

@bp.route("/api/media-networks/<int:net_id>", methods=["PATCH"])
@require_perm("settings.edit")
def media_networks_update(net_id):
    from ..database import db_update_media_network
    d = request.json or {}
    name = (d.get("name") or "").strip() or None
    domain = None
    if d.get("domain") is not None and str(d.get("domain")) != "":
        try:
            domain = int(d.get("domain"))
            assert 0 <= domain <= 127
        except (TypeError, ValueError, AssertionError):
            return jsonify({"error": "domaine invalide (0-127)"}), 400
    kw = dict(name=name, domain=domain)
    if "ptp_params" in d:                       # présent → applique (vide = hérite tout)
        kw["ptp_params"] = _ptp_params_from_payload(d.get("ptp_params")) or None
    db_update_media_network(net_id, **kw)
    return jsonify({"ok": True})

@bp.route("/api/media-networks/<int:net_id>", methods=["DELETE"])
@require_perm("settings.edit")
def media_networks_delete(net_id):
    from ..database import db_delete_media_network, db_media_network_in_use
    if db_media_network_in_use(net_id) > 0:
        return jsonify({"ok": False, "error": "réseau utilisé par des NIC — détachez-les d'abord"}), 409
    db_delete_media_network(net_id)
    return jsonify({"ok": True})


@bp.route("/api/cluster/clocks", methods=["GET"])
@require_login
def cluster_clocks():
    """État d'horloge de tous les nœuds + cohérence du cluster (Réglages → Réseau → Horloges).

    Lecture seule et CACHÉE (~20 s) : la page peut se rafraîchir sans harceler les nœuds. Le
    verdict `ok` alimente aussi le badge du Monitoring, qui n'affiche QUE « tout va bien ou non » —
    le détail et les réglages vivent ici."""
    from .. import clocks
    return jsonify({"ok": True, **clocks.etat(force=(request.args.get("force") == "1"))})


@bp.route("/api/cluster/clocks/<int:node_id>/ntp-tai", methods=["POST"])
@require_perm("settings.edit")
def cluster_clock_apply(node_id):
    """Met un nœud sur la grille : chrony + `leapseclist` (pose et MAINTIENT le `tai_offset`).
    Refuse un nœud dont REALTIME porte du TAI — y installer chrony ferait battre deux disciplines
    sur la même horloge."""
    from .. import clocks
    ok, msg, clef, params = clocks.appliquer_ntp_tai(node_id)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    from ..database import db_add_alert
    db_add_alert(clef, "info", node_id=node_id, kind="net", params=params)
    return jsonify({"ok": True, "message": msg})


@bp.route("/api/cluster/clocks/ntp-test", methods=["POST"])
@require_perm("settings.edit")
def cluster_clock_ntp_test():
    """Interroge les serveurs en SNTP AVANT de les poser : depuis le contrôleur ET depuis chaque
    nœud concerné, parce qu'un pare-feu ou une route peuvent différer d'une machine à l'autre.
    Ne modifie rien."""
    from .. import clocks
    from ..database import db_get_nodes
    srv = (request.json or {}).get("servers") or ""
    res = {"controleur": clocks.tester_ntp(srv)}
    for n in db_get_nodes():
        if clocks._a_un_moteur_2110(n):
            # Ce nœud ne PRENDRA pas ces serveurs (son heure vient du grandmaster), mais il est le
            # meilleur JUGE dont on dispose : il est verrouillé sur le GM au nanoseconde près. Y
            # interroger un serveur NTP, c'est le confronter à la référence de temps du site.
            # C'est ce contrôle qui manquait : un GM dont le service NTP est en retard de 100 ms
            # sur son PROPRE PTP passait pour une bonne source parce qu'il annonce « strate 1 ».
            res["%s (juge PTP)" % n.get("name")] = [
                {**r, "vs_ptp_ms": round((r["offset_s"] + clocks.TAI_UTC_OFFSET_S) * 1000, 1)}
                if r.get("ok") else r
                for r in clocks.tester_ntp(srv, n)]
            continue
        res[n.get("name")] = clocks.tester_ntp(srv, n)
    return jsonify({"ok": True, "resultats": res, "fps_reference": clocks._fps_reference()})


@bp.route("/api/cluster/clocks/ntp-apply", methods=["POST"])
@require_perm("settings.edit")
def cluster_clock_ntp_apply():
    """Pose la source NTP commune sur les nœuds tenus par chrony, puis VÉRIFIE que chacun a bien
    retenu une source — au lieu de supposer que l'écriture a suffi."""
    from .. import clocks
    from ..database import db_add_alert
    srv = (request.json or {}).get("servers") or ""
    res = clocks.appliquer_ntp(srv)
    if srv:
        db_add_alert("alert.net.ntp_source_appliquee", "info", kind="net", params={"srv": srv})
    else:
        db_add_alert("alert.net.ntp_source_appliquee_aucune", "info", kind="net")
    return jsonify({"ok": True, "resultats": res})
