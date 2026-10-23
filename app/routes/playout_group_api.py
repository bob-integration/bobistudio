# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Groupes de lecture A/B/C… (cf. app/playout_groups.py) : liste, configuration, Play / Cue par sortie,
Suivant, opérations de playlist.

Droits : LIRE un groupe = voir tous ses players (viewer) ; le PILOTER ou le CONFIGURER = `plugins.operate`
et le rôle opérateur sur chacun de ses players — un groupe ne donne jamais plus de prise que ses membres.
Les refus d'opération rendent `{error, code}` en 409 : `code` est traduit par l'interface
(`section.playout.err.<code>`), `error` est la phrase de repli."""
from flask import jsonify, request

from . import bp
from ..auth import require_login, has_perm, check_vmid_access
from .. import playout_groups as pg
from ..numerotation import cle_input


def _members_vmids(uuids):
    players = pg._players()
    return [players[u]["vmid"] for u in uuids if u in players]


def _access(g_or_uuids, role):
    uuids = g_or_uuids["members"] if isinstance(g_or_uuids, dict) else g_or_uuids
    for vm in _members_vmids(uuids):
        err = check_vmid_access(vm, role)
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
    g = pg.get_group(gid)
    if not g:
        return None, (jsonify({"error": "groupe de lecture introuvable", "code": "unknown_group"}), 404)
    return g, None


@bp.route("/api/playout_groups", methods=["GET"])
@require_login
def playout_groups_list():
    groups = [pg.ENGINE.view(g) for g in pg.list_groups() if _access(g, "viewer") is None]
    players = []
    for u, c in pg._players().items():
        if check_vmid_access(c["vmid"], "viewer") is None:
            players.append({"uuid": u, "vmid": c["vmid"], "hostname": c.get("hostname"), "status": c.get("status"),
                            "project_id": c.get("project_id"), "node_id": c.get("node_id")})
    return jsonify({"groups": groups, "players": sorted(players, key=lambda p: (p["hostname"] or "").lower()),
                    "mixers": _mixers(), "consoles": _consoles(), "engine": pg.ENGINE.status(),
                    "can_operate": has_perm("plugins.operate")})


def _label(shm):
    try:
        from ..tally import libelle_de
        return libelle_de(shm, 2) or ""
    except Exception:
        return ""


def _mixers():
    """Mélangeurs déployés (désignation du groupe) : entrées avec leur flux câblé — le formulaire en tire une
    SUGGESTION (l'entrée qui reçoit le flux vidéo du player), que l'humain valide."""
    out = []
    for u, c in pg._of_type(pg.MIXER_TYPE).items():
        if check_vmid_access(c["vmid"], "viewer"):
            continue
        params = pg._dc(c).get("params") or {}
        n = pg.mixer_n_inputs(c)
        ins = []
        for i in range(n):
            shm = str(params.get(cle_input(i)) or "")
            ins.append({"n": i + 1, "shm": shm, "resolved": _resolve(shm), "label": _label(shm) if shm else ""})
        out.append({"uuid": u, "label": c.get("hostname") or str(c["vmid"]), "status": c.get("status"),
                    "n_inputs": n, "inputs": ins})
    return sorted(out, key=lambda m: m["label"].lower())


def _consoles():
    """Consoles déployées : tranches du document persisté (n° = rang), avec le flux de leur entrée A — de quoi
    SUGGÉRER la tranche qui reçoit l'audio du player."""
    out = []
    for u, c in pg._of_type(pg.CONSOLE_TYPE).items():
        if check_vmid_access(c["vmid"], "viewer"):
            continue
        doc = (pg._dc(c).get("params") or {}).get("console") or {}
        strips = []
        for i, x in enumerate([x for x in doc.get("strips") or [] if isinstance(x, dict) and x.get("id")]):
            inp = x.get("input") if isinstance(x.get("input"), dict) else {}
            flow = str(inp.get("flow") or "") if inp.get("kind") == "flow" else ""
            strips.append({"n": i + 1, "id": x["id"], "label": x.get("label") or x.get("short") or x["id"],
                           "flow": flow, "resolved": _resolve(flow)})
        out.append({"uuid": u, "label": c.get("hostname") or str(c["vmid"]), "status": c.get("status"), "strips": strips})
    return sorted(out, key=lambda m: m["label"].lower())


def _resolve(ref):
    if not ref:
        return ""
    try:
        from ..tally import resolve_ref
        return resolve_ref(ref) or ref
    except Exception:
        return ref


@bp.route("/api/playout_groups", methods=["POST"])
@require_login
def playout_groups_create():
    b = request.get_json(silent=True) or {}
    err = _operate([str(u) for u in b.get("members") or []])
    if err:
        return err
    try:
        gid = pg.create_group(b)
    except pg.PlayoutError as e:
        return _refused(e)
    return jsonify({"ok": True, "id": gid, "group": pg.group_view(gid)})


@bp.route("/api/playout_groups/member/<int:vmid>", methods=["GET"])
@require_login
def playout_groups_of_member(vmid):
    """Le groupe dont ce player est une sortie (bandeau de la console du player), ou null."""
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    from ..database import db_get_container
    c = db_get_container(vmid) or {}
    g = pg.group_of_member(c.get("instance_uuid")) if c.get("instance_uuid") else None
    if not g:
        return jsonify({"group": None})
    i = g["members"].index(c["instance_uuid"])
    return jsonify({"group": {"id": g["id"], "name": g["name"], "letter": pg.letter(i), "outputs": len(g["members"])}})


@bp.route("/api/playout_groups/<int:gid>", methods=["GET"])
@require_login
def playout_groups_get(gid):
    g, err = _group_or_404(gid)
    if err:
        return err
    err = _access(g, "viewer")
    if err:
        return err
    return jsonify(pg.ENGINE.view(g))


@bp.route("/api/playout_groups/<int:gid>", methods=["POST"])
@require_login
def playout_groups_update(gid):
    g, err = _group_or_404(gid)
    if err:
        return err
    b = request.get_json(silent=True) or {}
    err = _operate(g) or (_operate([str(u) for u in b["members"]]) if isinstance(b.get("members"), list) else None)
    if err:
        return err
    try:
        pg.update_group(gid, b)
    except pg.PlayoutError as e:
        return _refused(e)
    return jsonify({"ok": True, "group": pg.group_view(gid)})


@bp.route("/api/playout_groups/<int:gid>", methods=["DELETE"])
@require_login
def playout_groups_delete(gid):
    g, err = _group_or_404(gid)
    if err:
        return err
    err = _operate(g)
    if err:
        return err
    pg.delete_group(gid)
    return jsonify({"ok": True})


@bp.route("/api/playout_groups/<int:gid>/lists", methods=["GET"])
@require_login
def playout_groups_lists(gid):
    g, err = _group_or_404(gid)
    if err:
        return err
    err = _access(g, "viewer")
    if err:
        return err
    return jsonify({"lists": pg.saved_lists(gid)})


@bp.route("/api/playout_groups/<int:gid>/<op>", methods=["POST"])
@require_login
def playout_groups_op(gid, op):
    """next · play {output} · cue {output} · item {item_id, op, value} · add {items, mode} · load_list {name, mode}
    · reset."""
    g, err = _group_or_404(gid)
    if err:
        return err
    err = _operate(g)
    if err:
        return err
    b = request.get_json(silent=True) or {}
    try:
        if op == "next":
            r = pg.op_next(gid, "ui")
        elif op == "play":
            r = pg.op_play(gid, b.get("output"), "ui")
        elif op == "cue":
            r = pg.op_cue(gid, b.get("output"), "ui")
        elif op == "item":
            pg.op_item(gid, str(b.get("op") or ""), str(b.get("item_id") or ""), b.get("value"))
            r = {}
        elif op == "add":
            r = {"added": len(pg.op_add(gid, b.get("items") or [], str(b.get("mode") or "append")))}
        elif op == "load_list":
            r = {"added": len(pg.op_load_list(gid, str(b.get("name") or ""), str(b.get("mode") or "replace")))}
        elif op == "reset":
            pg.op_reset(gid)
            r = {}
        else:
            return jsonify({"error": "opération inconnue", "code": "unknown_op"}), 404
    except pg.PlayoutError as e:
        return _refused(e)
    return jsonify(dict(r if isinstance(r, dict) else {}, ok=True, group=pg.group_view(gid)))
