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
    from ..allocations import ranges_with_usage
    return jsonify({"ok": True, "ranges": ranges_with_usage(db_get_mcast_ranges(
        media_network_id=media_network_id, node_id=node_id, ifname=ifname))})

MCAST_RANGE_API_FIELDS = ("scope", "media_network_id", "node_id", "ifname", "base_ip", "size",
                          "prefix_len", "port_default", "port_default_video", "port_default_audio",
                          "port_default_anc", "ip_offset_video", "ip_offset_audio",
                          "ip_offset_anc", "ip_step_audio", "essence", "leg", "label",
                          "base_ip_b", "rank_mode", "port_mode", "reserve_last")

MCAST_RANK_MODES = ("slot", "counter")
MCAST_PORT_MODES = ("increment", "fixed")


def _validate_plan_axes(fields, current=None):
    """Normalise and check the per-range plan axes in `fields` (in place). `current` = the stored
    rule on an update (the B block is checked against the A block as it will be after the update).
    Returns an error message (str) or None."""
    import ipaddress as _ip
    mcast_net = _ip.ip_network("224.0.0.0/4")
    for key, allowed in (("rank_mode", MCAST_RANK_MODES), ("port_mode", MCAST_PORT_MODES)):
        if key in fields:
            v = fields[key]
            if isinstance(v, str):
                v = v.strip().lower() or None
            if v is not None and v not in allowed:
                return f"{key} invalide (attendu {' | '.join(allowed)})"
            fields[key] = v
    if "reserve_last" in fields:
        v = fields["reserve_last"]
        fields["reserve_last"] = None if v in (None, "") else (1 if v in (True, 1, "1", "true", "on") else 0)
    if "base_ip_b" in fields:
        fields["base_ip_b"] = (fields["base_ip_b"] or "").strip() or None
    merged = dict(current or {})
    merged.update({k: v for k, v in fields.items() if k in ("base_ip", "size", "base_ip_b")})
    b = merged.get("base_ip_b")
    if not b:
        return None
    try:
        ib = _ip.IPv4Address(b)
    except Exception:
        return "base_ip_b invalide (adresse IPv4 attendue)"
    if ib not in mcast_net:
        return "base_ip_b n'est pas une adresse multicast (224.0.0.0/4)"
    try:
        a = int(_ip.IPv4Address(merged.get("base_ip")))
        size = int(merged.get("size") or 0)
    except Exception:
        return None                       # A block itself invalid: reported by the caller
    if int(ib) + size - 1 > int(_ip.IPv4Address("239.255.255.255")):
        return "bloc B hors de 224.0.0.0/4"
    if int(ib) != a and int(ib) < a + size and a < int(ib) + size:
        return "le bloc B chevauche partiellement le bloc A (identique ou disjoint attendu)"
    return None

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
    _err = _validate_plan_axes(fields)
    if _err:
        return jsonify({"ok": False, "error": _err}), 400
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
    from ..database import db_update_mcast_range, db_get_mcast_ranges
    data = request.json or {}
    fields = {k: data.get(k) for k in MCAST_RANGE_API_FIELDS if k in data}
    _err = _mcast_cidr_to_fields(data, fields)
    if _err:
        return jsonify({"ok": False, "error": _err}), 400
    current = next((r for r in db_get_mcast_ranges() if r.get("id") == range_id), None)
    _err = _validate_plan_axes(fields, current)
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


# ─── Multicast PLANS (app/mcast_plans.py): one target + shared settings + lines, by fabric ──────

@bp.route("/api/mcast_plans", methods=["GET"])
@require_perm("settings.edit")
def api_mcast_plans_list():
    from ..mcast_plans import plans_api
    return jsonify({"ok": True, "plans": plans_api()})

@bp.route("/api/mcast_plans", methods=["POST"])
@require_perm("settings.edit")
def api_mcast_plans_add():
    from ..mcast_plans import save_plan
    pid, err = save_plan(request.get_json(silent=True) or {})
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "id": pid})

@bp.route("/api/mcast_plans/preview", methods=["POST"])
@require_perm("settings.edit")
def api_mcast_plans_preview():
    """Simulate a plan (saved or not) against the TX flows it addresses: nothing written or reserved."""
    from ..mcast_plans import preview
    out, err = preview(request.get_json(silent=True) or {})
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, **out})

@bp.route("/api/mcast_plans/<int:plan_id>", methods=["PUT"])
@require_perm("settings.edit")
def api_mcast_plans_update(plan_id):
    """Whole-plan replace: a line with `id` is updated, one without is created, missing ones are
    deleted. Existing allocations are left as they are (re-planning is a separate, explicit act)."""
    from ..mcast_plans import save_plan
    pid, err = save_plan(request.get_json(silent=True) or {}, plan_id)
    if err:
        return jsonify({"ok": False, "error": err}), (404 if err == "plan introuvable" else 400)
    return jsonify({"ok": True, "id": pid})

@bp.route("/api/mcast_plans/<int:plan_id>", methods=["DELETE"])
@require_perm("settings.edit")
def api_mcast_plans_delete(plan_id):
    from ..database import db_delete_mcast_plan
    db_delete_mcast_plan(plan_id)
    return jsonify({"ok": True})

@bp.route("/api/mcast_targets", methods=["GET"])
@require_perm("settings.edit")
def api_mcast_targets():
    from ..mcast_plans import targets
    return jsonify({"ok": True, **targets()})

@bp.route("/api/flow_classes", methods=["GET"])
@require_perm("settings.edit")
def api_flow_classes():
    from ..mcast_plans import flow_classes
    return jsonify({"ok": True, "classes": flow_classes()})
