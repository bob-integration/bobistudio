# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Règles de plage multicast STRICTES (réseau logique / interface physique) — allocation
centralisée des adresses multicast ST 2110 (cf. app/allocations.py pour la réservation)."""

import json

from flask import jsonify, request

from . import bp
from ..auth import require_perm


@bp.route("/api/media_networks", methods=["GET"])
@require_perm("settings.edit")
def api_media_networks_list():
    from ..database import db_get_media_networks
    return jsonify({"ok": True, "networks": db_get_media_networks()})

@bp.route("/api/mcast_ranges", methods=["GET"])
@require_perm("settings.edit")
def api_mcast_ranges_list():
    from ..database import db_get_mcast_ranges
    media_network_id = request.args.get("media_network_id", type=int)
    node_id = request.args.get("node_id", type=int)
    ifname = request.args.get("ifname") or None
    return jsonify({"ok": True, "ranges": db_get_mcast_ranges(
        media_network_id=media_network_id, node_id=node_id, ifname=ifname)})

MCAST_RANGE_API_FIELDS = ("scope", "media_network_id", "node_id", "ifname", "base_ip", "size",
                          "prefix_len", "port_default", "port_default_video", "port_default_audio",
                          "port_default_anc", "ip_offset_video", "ip_offset_audio",
                          "ip_offset_anc", "ip_step_audio", "essence", "leg", "label")

def _mcast_cidr_to_fields(data, fields):
    """Si `data['cidr']` est fourni (ex. '239.10.30.0/24'), dérive base_ip/size/prefix_len et les
    pose dans `fields` (prime sur des base_ip/size fournis séparément). Retourne un message d'erreur
    (str) si le CIDR est invalide, sinon None."""
    cidr = (data.get("cidr") or "").strip()
    if not cidr:
        return None
    import ipaddress as _ip
    try:
        net = _ip.ip_network(cidr, strict=False)
    except Exception:
        return "CIDR invalide (attendu adresse/préfixe, ex. 239.10.30.0/24)"
    fields["base_ip"] = str(net.network_address)
    fields["size"] = net.num_addresses
    fields["prefix_len"] = net.prefixlen
    return None

@bp.route("/api/mcast_ranges", methods=["POST"])
@require_perm("settings.edit")
def api_mcast_ranges_add():
    import ipaddress as _ip
    from ..database import db_add_mcast_range
    data = request.json or {}
    fields = {k: data.get(k) for k in MCAST_RANGE_API_FIELDS}
    _err = _mcast_cidr_to_fields(data, fields)
    if _err:
        return jsonify({"ok": False, "error": _err}), 400
    try:
        _ip.IPv4Address(fields.get("base_ip"))
        size = int(fields.get("size") or 0)
        if size < 1:
            raise ValueError("size")
    except Exception:
        return jsonify({"ok": False, "error": "base_ip/size invalides"}), 400
    if data.get("match_json") is not None:
        fields["match_json"] = json.dumps(data["match_json"]) if not isinstance(data["match_json"], str) \
            else data["match_json"]
    try:
        range_id = db_add_mcast_range(**fields)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "id": range_id})

@bp.route("/api/mcast_ranges/<int:range_id>", methods=["PUT"])
@require_perm("settings.edit")
def api_mcast_ranges_update(range_id):
    from ..database import db_update_mcast_range
    data = request.json or {}
    fields = {k: data.get(k) for k in MCAST_RANGE_API_FIELDS if k in data}
    _err = _mcast_cidr_to_fields(data, fields)
    if _err:
        return jsonify({"ok": False, "error": _err}), 400
    if "match_json" in data:
        fields["match_json"] = json.dumps(data["match_json"]) if not isinstance(data["match_json"], str) \
            else data["match_json"]
    db_update_mcast_range(range_id, **fields)
    return jsonify({"ok": True})

@bp.route("/api/mcast_ranges/<int:range_id>", methods=["DELETE"])
@require_perm("settings.edit")
def api_mcast_ranges_delete(range_id):
    from ..database import db_delete_mcast_range
    db_delete_mcast_range(range_id)
    return jsonify({"ok": True})
