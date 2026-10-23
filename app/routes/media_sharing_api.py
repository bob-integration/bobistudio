# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Partages de médias et de bins (app/media_sharing.py) et groupes d'utilisateurs
(app/user_groups.py) — routes `/api/media/shares*`, `/api/user-groups*`, page `/groups`.

Partager / modifier / retirer un partage : permission `media.manage` ET la cible dans le périmètre
PROPRIÉTAIRE de l'utilisateur (ses projets, ou accès global) — un partage reçu ne se repartage pas.
Le partageur peut aussi retirer le sien. Les partages d'une cible et leur journal se lisent dans ce
même périmètre. Groupes : gérés par qui gère les utilisateurs (`settings.edit`, comme
app/routes/users.py)."""
import json

from flask import jsonify, render_template, request

from . import bp
from ..auth import current_permissions, current_user, has_global_access, require_login, require_perm
from .. import media_registry as mr
from .. import media_sharing as ms
from .. import user_groups as ug


def _err(code, status, **kw):
    return jsonify(dict({"ok": False, "error": code}, **kw)), status


def _target(kind, tid, scope):
    """(kind, id normalisé, réponse d'erreur|None) : la cible doit être VISIBLE (sinon 404, comme
    toute la base) ; voir seulement par un partage → 403 `shared_read_only`."""
    if kind == "asset":
        tid = str(tid or "")
        if not mr.asset_visible(tid, scope):
            return kind, tid, _err("média inconnu", 404)
        if scope is not None and not ms.owns_asset(scope, tid):
            return kind, tid, _err("forbidden", 403, reason="shared_read_only")
        return kind, tid, None
    if kind == "bin":
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return kind, None, _err("bin inconnu", 404)
        if not mr.bin_visible(tid, scope):
            return kind, tid, _err("bin inconnu", 404)
        if scope is not None and not ms.owns_bin(scope, tid):
            return kind, tid, _err("forbidden", 403, reason="shared_read_only")
        return kind, tid, None
    return kind, None, _err("target_invalid", 400)


def _me():
    u = current_user() or {}
    return u.get("id"), u.get("username") or ""


@bp.route("/api/media/shares")
@require_login
def media_shares_list():
    """?kind=asset|bin&id=… → {shares, log, can_manage} — partages d'une cible, pour son propriétaire."""
    scope = ms.user_scope()
    kind, tid, err = _target(request.args.get("kind"), request.args.get("id"), scope)
    if err:
        return err
    return jsonify({"shares": ms.shares_of(kind, tid), "log": ms.log_of(kind, tid, limit=50),
                    "can_manage": ms.check_can_share(scope, current_permissions(), kind, tid) is None})


@bp.route("/api/media/shares", methods=["POST"])
@require_perm("media.manage")
def media_shares_create():
    """{targets:[{kind, id}], recipients:[{kind: user|group|project, id}], level: use|edit,
    expires_at?} → un partage par (cible, destinataire) ; un partage existant est mis à jour."""
    b = request.get_json(silent=True) or {}
    scope = ms.user_scope()
    level = b.get("level") or "use"
    if level not in ms.LEVELS:
        return _err("level_invalid", 400)
    targets = [t for t in (b.get("targets") or []) if isinstance(t, dict)][:500]
    recipients = [r for r in (b.get("recipients") or []) if isinstance(r, dict)][:200]
    if not targets or not recipients:
        return _err("targets_and_recipients_required", 400)
    checked = []
    for t in targets:
        kind, tid, err = _target(t.get("kind"), t.get("id"), scope)
        if err:
            return err
        why = ms.check_can_share(scope, current_permissions(), kind, tid, level)
        if why:
            return _err("forbidden", 403, reason=why)
        checked.append((kind, tid))
    uid, uname = _me()
    out = []
    try:
        for kind, tid in checked:
            for r in recipients:
                out.append(ms.share(kind, tid, r.get("kind"), r.get("id"), level, b.get("expires_at") or None,
                                    sharer_uid=uid, sharer_name=uname))
    except LookupError as e:
        return _err(str(e), 404)
    except ValueError as e:
        return _err(str(e), 400)
    return jsonify({"ok": True, "shares": out})


def _share_for_owner(sid):
    """(ligne, erreur) : retirer / modifier un partage — le propriétaire de la cible (media.manage)
    ou son auteur."""
    row = ms.get_share(sid)
    if not row:
        return None, _err("share_unknown", 404)
    uid, _n = _me()
    if row["sharer_uid"] is not None and uid is not None and int(row["sharer_uid"]) == int(uid):
        return row, None
    scope = ms.user_scope()
    if ms.check_can_share(scope, current_permissions(), row["target_kind"],
                          row["target_id"] if row["target_kind"] == "asset" else int(row["target_id"])):
        return None, _err("forbidden", 403, reason="not_owner")
    return row, None


@bp.route("/api/media/shares/<int:sid>", methods=["PATCH", "DELETE"])
@require_login
def media_share_edit(sid):
    row, err = _share_for_owner(sid)
    if err:
        return err
    _uid, uname = _me()
    if request.method == "DELETE":
        ms.revoke(sid, actor=uname)
        return jsonify({"ok": True})
    b = request.get_json(silent=True) or {}
    level = b.get("level") or row["level"]
    exp = b["expires_at"] if "expires_at" in b else row["expires_at"]
    try:
        r = ms.share(row["target_kind"], row["target_id"], row["recipient_kind"], row["recipient_id"], level, exp or None,
                     sharer_uid=row["sharer_uid"], sharer_name=row["sharer_name"],
                     sharer_projects=None if row["sharer_projects"] in (None, "null") else json.loads(row["sharer_projects"]))
    except LookupError as e:
        return _err(str(e), 404)
    except ValueError as e:
        return _err(str(e), 400)
    return jsonify({"ok": True, "share": r})


@bp.route("/api/media/shares/mine")
@require_login
def media_shares_mine():
    """« Partagés avec moi » : {bins:[{bin_id, name, level, by, expires_at}], assets:[…]}."""
    uid, _n = _me()
    return jsonify(ms.shared_with_me(uid, ms.user_scope()))


@bp.route("/api/media/shares/recipients")
@require_perm("media.manage")
def media_shares_recipients():
    """Destinataires possibles (fenêtre « Partager… ») : utilisateurs, groupes, projets ; ?q= filtre."""
    from ..database import db_list_users, db_get_projects
    q = (request.args.get("q") or "").strip().lower()
    uid, _n = _me()

    def hit(*xs):
        return not q or any(q in (x or "").lower() for x in xs)
    users = [{"kind": "user", "id": u["id"], "label": ug.display_name(u)} for u in db_list_users()
             if u["id"] != uid and hit(u.get("username"), u.get("prenom"), u.get("nom"))]
    groups = [{"kind": "group", "id": g["id"], "label": g["name"], "members": len(g["members"])}
              for g in ug.list_groups() if hit(g["name"])]
    projects = [{"kind": "project", "id": p["id"], "label": p.get("name") or "#%s" % p["id"]}
                for p in (db_get_projects() or []) if hit(p.get("name"))]
    return jsonify({"users": users[:200], "groups": groups[:200], "projects": projects[:200]})


@bp.route("/api/media/shares/log")
@require_login
def media_shares_log():
    """Journal complet des partages : accès global seulement (celui d'une cible : /api/media/shares)."""
    if not has_global_access():
        return _err("forbidden", 403, reason="global_access_required")
    return jsonify({"log": ms.log_of(limit=min(2000, int(request.args.get("limit") or 500)))})


# ─── Groupes d'utilisateurs ─────────────────────────────────────────────────────────────────
@bp.route("/groups")
@require_perm("settings.edit")
def user_groups_page():
    return render_template("user_groups.html")


@bp.route("/api/user-groups", methods=["GET", "POST"])
@require_perm("settings.edit")
def user_groups_api():
    if request.method == "GET":
        from ..database import db_list_users
        return jsonify({"groups": ug.list_groups(),
                        "users": [{"id": u["id"], "username": u["username"], "label": ug.display_name(u)}
                                  for u in db_list_users()]})
    b = request.get_json(silent=True) or {}
    try:
        gid = ug.create_group(b.get("name"), by=_me()[1])
        if isinstance(b.get("members"), list):
            ug.set_members(gid, b["members"])
    except ValueError as e:
        return _err(str(e), 400)
    return jsonify({"ok": True, "id": gid, "group": ug.get_group(gid)})


@bp.route("/api/user-groups/<int:gid>", methods=["PATCH", "DELETE"])
@require_perm("settings.edit")
def user_group_edit(gid):
    if request.method == "DELETE":
        return jsonify({"ok": True}) if ug.delete_group(gid) else _err("group_unknown", 404)
    b = request.get_json(silent=True) or {}
    try:
        if b.get("name") is not None:
            ug.rename_group(gid, b.get("name"))
        if isinstance(b.get("members"), list):
            ug.set_members(gid, b["members"])
    except LookupError as e:
        return _err(str(e), 404)
    except ValueError as e:
        return _err(str(e), 400)
    g = ug.get_group(gid)
    return jsonify({"ok": True, "group": g}) if g else _err("group_unknown", 404)
