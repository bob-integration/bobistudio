# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Lecture synchrone (cf. app/sync_groups.py) : liste, configuration, transport commun, playlist en lignes.

Droits : LIRE un groupe = voir tous ses players (viewer) ; le PILOTER ou le CONFIGURER = `plugins.operate` et
le rôle opérateur sur chacun de ses players — un groupe ne donne jamais plus de prise que ses membres. Les
refus d'opération rendent `{error, code}` en 409 : `code` est traduit par l'interface
(`section.syncplay.err.<code>`), `error` est la phrase de repli."""
from flask import jsonify, request

from . import bp
from ..auth import require_login, has_perm, check_vmid_access
from .. import sync_groups as sg

_TRANSPORT = ("play", "pause", "stop", "cue", "goto", "next", "prev", "seek", "step", "speed", "loop")
_LINES = ("add", "drop", "up", "down", "set_cell", "title", "duration", "clear", "save", "load", "unsave")


def _access(g_or_uuids, role):
    uuids = g_or_uuids["members"] if isinstance(g_or_uuids, dict) else g_or_uuids
    players = sg._players()
    for u in uuids:
        if u in players:
            err = check_vmid_access(players[u]["vmid"], role)
            if err:
                return err
    return None


def _operate(g_or_uuids):
    if not has_perm("plugins.operate"):
        return jsonify({"error": "forbidden", "missing_permission": "plugins.operate"}), 403
    return _access(g_or_uuids, "operator")


def _refused(e):
    return jsonify({"ok": False, "error": str(e), "code": e.code}), 409


def _group_or_404(gid):
    g = sg.get_group(gid)
    if not g:
        return None, (jsonify({"error": "groupe synchrone introuvable", "code": "unknown_group"}), 404)
    return g, None


@bp.route("/api/sync_groups", methods=["GET"])
@require_login
def sync_groups_list():
    groups = [sg.ENGINE.view(g) for g in sg.list_groups() if _access(g, "viewer") is None]
    players = []
    for u, c in sg._players().items():
        if check_vmid_access(c["vmid"], "viewer") is None:
            players.append({"uuid": u, "vmid": c["vmid"], "hostname": c.get("hostname"), "status": c.get("status"),
                            "project_id": c.get("project_id"), "node_id": c.get("node_id")})
    return jsonify({"groups": groups, "players": sorted(players, key=lambda p: (p["hostname"] or "").lower()),
                    "engine": sg.ENGINE.status(), "can_operate": has_perm("plugins.operate"),
                    "limits": {"lead_ms": [sg.LEAD_MS_MIN, sg.LEAD_MS_MAX], "lead_default": sg.LEAD_MS_DEFAULT,
                               "speed": [sg.SPEED_MIN, sg.SPEED_MAX]}})


@bp.route("/api/sync_groups", methods=["POST"])
@require_login
def sync_groups_create():
    b = request.get_json(silent=True) or {}
    err = _operate([str(u) for u in b.get("members") or []])
    if err:
        return err
    try:
        gid = sg.create_group(b)
    except sg.SyncError as e:
        return _refused(e)
    return jsonify({"ok": True, "id": gid, "group": sg.group_view(gid)})


@bp.route("/api/sync_groups/member/<int:vmid>", methods=["GET"])
@require_login
def sync_groups_of_member(vmid):
    """Le groupe synchrone dont ce player est un canal (bandeau de la console du player), ou null."""
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    from ..database import db_get_container
    c = db_get_container(vmid) or {}
    g = sg.group_of_member(c.get("instance_uuid")) if c.get("instance_uuid") else None
    if not g:
        return jsonify({"group": None})
    i = g["members"].index(c["instance_uuid"])
    return jsonify({"group": {"id": g["id"], "name": g["name"], "letter": sg.letter(i), "channels": len(g["members"]),
                              "state": sg.ENGINE.state_of(g["id"])}})


@bp.route("/api/sync_groups/<int:gid>", methods=["GET"])
@require_login
def sync_groups_get(gid):
    g, err = _group_or_404(gid)
    if err:
        return err
    err = _access(g, "viewer")
    if err:
        return err
    return jsonify(sg.ENGINE.view(g))


@bp.route("/api/sync_groups/<int:gid>", methods=["POST"])
@require_login
def sync_groups_update(gid):
    g, err = _group_or_404(gid)
    if err:
        return err
    b = request.get_json(silent=True) or {}
    err = _operate(g) or (_operate([str(u) for u in b["members"]]) if isinstance(b.get("members"), list) else None)
    if err:
        return err
    try:
        sg.update_group(gid, b)
        if "end_policy" in b:
            sg.ENGINE.replan(gid)
    except sg.SyncError as e:
        return _refused(e)
    return jsonify({"ok": True, "group": sg.group_view(gid)})


@bp.route("/api/sync_groups/<int:gid>", methods=["DELETE"])
@require_login
def sync_groups_delete(gid):
    g, err = _group_or_404(gid)
    if err:
        return err
    err = _operate(g)
    if err:
        return err
    sg.delete_group(gid)
    return jsonify({"ok": True})


@bp.route("/api/sync_groups/<int:gid>/<op>", methods=["POST"])
@require_login
def sync_groups_op(gid, op):
    """Transport : play · pause · stop · cue {line} · goto {line|index} · next · prev · seek {t} · step {frames}
    · speed {speed} · loop {mode}. Playlist : lines/<add|drop|up|down|set_cell|title|duration|clear|save|load|unsave>."""
    g, err = _group_or_404(gid)
    if err:
        return err
    err = _operate(g)
    if err:
        return err
    b = request.get_json(silent=True) or {}
    try:
        if op in _TRANSPORT:
            v = sg.op(gid, op, b, source="ui")
        else:
            return jsonify({"error": "opération inconnue", "code": "unknown_op"}), 404
    except sg.SyncError as e:
        return _refused(e)
    return jsonify({"ok": True, "group": v})


@bp.route("/api/sync_groups/<int:gid>/lines/<op>", methods=["POST"])
@require_login
def sync_groups_lines(gid, op):
    g, err = _group_or_404(gid)
    if err:
        return err
    err = _operate(g)
    if err:
        return err
    if op not in _LINES:
        return jsonify({"error": "opération inconnue", "code": "unknown_op"}), 404
    try:
        sg.lines_op(gid, op, request.get_json(silent=True) or {})
    except sg.SyncError as e:
        return _refused(e)
    return jsonify({"ok": True, "group": sg.group_view(gid)})
