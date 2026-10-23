# SPDX-License-Identifier: GPL-3.0-or-later
"""SHOWS of a « document » plugin (audio console) — cf. app/plugin_shows.py for what a show is and is not.

Rights: listing and exporting = login; saving, loading, importing, deleting = the permission DECLARED by the plugin
(`control.shows.permission`, `console.build` for the console) — a show replaces the whole console, so never an
operator limited to his zones. Loading is a human gesture only: no macro, no trigger (cf. plugin_shows docstring).

    GET    /api/plugin-shows/<type>?kind=show|template   library of one kind (summaries, no documents) + can_save, kinds
    GET    /api/plugin-shows/<type>/<id>/export          exchange file (JSON)
    POST   /api/plugin-shows/<type>/import               {document: file, mode: new|overwrite, name?, kind?, grants_from?}
                                                         or the bare file (grants_from = vmid whose zone grants are kept)
    DELETE /api/plugin-shows/<type>/<id>
    POST   /api/containers/<vmid>/shows                  {name, overwrite?}           save this console as a show
    POST   /api/containers/<vmid>/shows/<id>/check       {unwire_missing?, rebind_own?}   dry run: the report
    POST   /api/containers/<vmid>/shows/<id>/load        {base_version, accept_latency_jump?, unwire_missing?, rebind_own?}

Errors: {"error": <code>, …values} — the interface translates the code."""
from flask import jsonify, request

from . import bp
from ..auth import require_login, current_user, has_perm, check_vmid_access
from .. import plugin_shows as ps


def _err(e):
    return jsonify(e.body()), e.status


def _need(perm):
    if not has_perm(perm):
        return jsonify({"error": "forbidden", "missing_permission": perm}), 403
    return None


def _body():
    b = request.get_json(force=True, silent=True)
    return b if isinstance(b, dict) else {}


@bp.route("/api/plugin-shows/<type_>", methods=["GET"])
@require_login
def plugin_shows_list(type_):
    try:
        sp = ps.type_spec(type_)
    except ps.ShowError as e:
        return _err(e)
    try:
        kind = ps.check_kind(request.args.get("kind"))
    except ps.ShowError as e:
        return _err(e)
    return jsonify({"shows": ps.list_shows(type_, kind=kind), "kind": kind, "kinds": list(ps.KINDS),
                    "can_save": has_perm(sp["permission"]), "permission": sp["permission"]})


@bp.route("/api/plugin-shows/<type_>/<int:sid>/export", methods=["GET"])
@require_login
def plugin_shows_export(type_, sid):
    try:
        ps.type_spec(type_)
        doc = ps.export_document(type_, sid, with_grants=has_perm("settings.edit"))
    except ps.ShowError as e:
        return _err(e)
    r = jsonify(doc)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in doc["name"])[:60] or "show"
    r.headers["Content-Disposition"] = 'attachment; filename="show-%s-%s.json"' % (type_, safe)
    return r


@bp.route("/api/plugin-shows/<type_>/import", methods=["POST"])
@require_login
def plugin_shows_import(type_):
    try:
        sp = ps.type_spec(type_)
    except ps.ShowError as e:
        return _err(e)
    deny = _need(sp["permission"])
    if deny:
        return deny
    b = _body()
    gf = b.get("grants_from")
    if gf is not None:                          # « save as template » from a console: ITS grants go with the template
        if isinstance(gf, bool) or not isinstance(gf, int):
            return jsonify({"error": "bad_grants_from"}), 400
        err = check_vmid_access(gf, "operator")
        if err:
            return err
    try:
        return jsonify({"ok": True, "show": ps.import_document(type_, b, current_user(), grants_from=gf)})
    except ps.ShowError as e:
        return _err(e)


@bp.route("/api/plugin-shows/<type_>/<int:sid>", methods=["DELETE"])
@require_login
def plugin_shows_delete(type_, sid):
    try:
        sp = ps.type_spec(type_)
    except ps.ShowError as e:
        return _err(e)
    deny = _need(sp["permission"])
    if deny:
        return deny
    if not ps.delete_show(type_, sid):
        return jsonify({"error": "not_found", "id": sid}), 404
    return jsonify({"ok": True})


def _console_gate(vmid):
    """Project access + the declared permission. Returns (spec, None) or (None, response)."""
    err = check_vmid_access(vmid, "operator")
    if err:
        return None, err
    try:
        _c, _dc, _m, sp = ps._container(vmid)
    except ps.ShowError as e:
        return None, _err(e)
    deny = _need(sp["permission"])
    if deny:
        return None, deny
    return sp, None


@bp.route("/api/containers/<int:vmid>/shows", methods=["POST"])
@require_login
def plugin_shows_save(vmid):
    _sp, deny = _console_gate(vmid)
    if deny:
        return deny
    b = _body()
    try:
        return jsonify({"ok": True, "show": ps.save(vmid, b.get("name"), current_user(),
                                                    overwrite=b.get("overwrite") is True)})
    except ps.ShowError as e:
        return _err(e)


@bp.route("/api/containers/<int:vmid>/shows/<int:sid>/check", methods=["POST"])
@require_login
def plugin_shows_check(vmid, sid):
    _sp, deny = _console_gate(vmid)
    if deny:
        return deny
    b = _body()
    try:
        return jsonify(ps.check(vmid, sid, unwire_missing=b.get("unwire_missing") is True,
                                rebind_own=b.get("rebind_own") is not False))
    except ps.ShowError as e:
        return _err(e)


@bp.route("/api/containers/<int:vmid>/shows/<int:sid>/load", methods=["POST"])
@require_login
def plugin_shows_load(vmid, sid):
    _sp, deny = _console_gate(vmid)
    if deny:
        return deny
    b = _body()
    try:
        status, body = ps.load(vmid, sid, accept_latency_jump=b.get("accept_latency_jump") is True,
                               base_version=b.get("base_version"),
                               unwire_missing=b.get("unwire_missing") is True,
                               rebind_own=b.get("rebind_own") is not False)
    except ps.ShowError as e:
        return _err(e)
    return jsonify(body), status
