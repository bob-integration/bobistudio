# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Envoi d'un fichier depuis le poste de l'utilisateur (par morceaux, reprenable) — routes
`/api/media/upload/*`. Logique dans app/media_upload.py. Tout est sous `media.manage` : c'est un
geste d'écriture de la base (le fichier y est importé), comme les autres."""
from flask import jsonify, request

from . import bp
from .. import media_sharing as ms
from ..auth import require_perm, current_user, has_global_access, scoped_project_ids
from .. import media_upload as mu


def _err(e):
    return jsonify(dict({"ok": False, "error": str(e)}, **e.extra)), e.code


def _int(v):
    try:
        return int(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _scoped_target(b, vmid):
    """Portée (cf. media_registry_api) : le média envoyé par un utilisateur RESTREINT doit entrer
    dans un de SES projets — sinon il lui serait invisible aussitôt arrivé. Projet demandé, sinon
    celui du bin, du conteneur, ou son projet unique ; à défaut, refus explicite (400) plutôt qu'un
    média orphelin. Renvoie (project_id, réponse d'erreur|None). Accès global : inchangé."""
    from .. import media_registry as mr
    scope = scoped_project_ids()
    pid, bid = _int(b.get("project_id")), _int(b.get("bin_id"))
    if scope is None:
        return pid, None
    if bid is not None:
        if not mr.bin_visible(bid, scope):
            return pid, (jsonify({"ok": False, "error": "bin inconnu"}), 404)
        r = mr.db().execute("SELECT project_id FROM media_bins WHERE id=?", (bid,)).fetchone()
        pid = pid or (r["project_id"] if r else None)
    if pid is None and vmid is not None:
        pid = mr.container_context(vmid).get("project_id")
    if pid is None and len(scope) == 1:
        pid = next(iter(scope))
    if pid is None:
        return pid, (jsonify({"ok": False, "error": "choisissez le projet dans lequel ranger ce média",
                              "project_required": True}), 400)
    if not mr.project_in_scope(pid, scope):
        return pid, (jsonify({"ok": False, "error": "forbidden", "reason": "not_project_member"}), 403)
    return pid, None


def _session(sid):
    u = current_user() or {}
    return mu.session_for(sid, u.get("id"), has_global_access(u))


@bp.route("/api/media/upload/config")
@require_perm("media.manage")
def media_upload_config():
    """Ce que la page doit savoir avant d'envoyer : taille des morceaux, maximum, extensions, et
    les nœuds qui savent recevoir (agent ≥ 0.26.0)."""
    from .. import node_driver
    from ..database import db_get_nodes
    return jsonify({"chunk_size": mu.CHUNK, "max_bytes": mu.max_bytes(), "max_gb": mu.max_bytes() / (1 << 30),
                    "exts": list(mu.EXTS), "kinds": {k: list(v) for k, v in mu.KIND_EXTS.items()},
                    "nodes": [{"id": n["id"], "name": n.get("name"), "agent_version": n.get("agent_version"),
                               "capable": node_driver.upload_resumable(n)}
                              for n in db_get_nodes() if (n.get("media_image") or "").strip()]})


@bp.route("/api/media/upload/settings", methods=["POST"])
@require_perm("media.manage")
def media_upload_settings():
    """{max_gb} : taille maximale d'un fichier envoyé (Go)."""
    from .. import settings as _st
    b = request.get_json(silent=True) or {}
    try:
        v = float(b.get("max_gb"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "taille en Go attendue"}), 400
    if not 0.01 <= v <= 100000:
        return jsonify({"ok": False, "error": "taille hors bornes (0,01 à 100 000 Go)"}), 400
    _st.set("media_upload_max_gb", v)
    return jsonify({"ok": True, "max_gb": v})


@bp.route("/api/media/upload/check", methods=["POST"])
@require_perm("media.manage")
def media_upload_check():
    """{items:[{name, size, mtime, new_name?, fp?}], node_id?, folder?, vmid?, reserved?:[noms]} → pour chaque
    fichier : nom libre, reprise d'un envoi interrompu, ou nom déjà pris + proposition (cf. mu.check_names)."""
    b = request.get_json(silent=True) or {}
    vmid = _int(b.get("vmid"))
    if vmid is not None:
        from ..auth import check_vmid_access
        err = check_vmid_access(vmid, "operator")
        if err:
            return err
    items = b.get("items")
    if not isinstance(items, list) or not items or len(items) > 500:
        return jsonify({"ok": False, "error": "items : liste de 1 à 500 fichiers"}), 400
    reserved = b.get("reserved") if isinstance(b.get("reserved"), list) else []
    try:
        return jsonify(dict(mu.check_names((current_user() or {}).get("id"), items, node_id=_int(b.get("node_id")),
                                           folder=b.get("folder") or "", vmid=vmid, reserved=reserved[:1000],
                                           scope=ms.user_scope()), ok=True))
    except mu.UploadError as e:
        return _err(e)


@bp.route("/api/media/upload/attach", methods=["POST"])
@require_perm("media.manage")
def media_upload_attach():
    """{asset_id, vmid?, project_id?, bin_id?} — « Ne pas l'envoyer » : le média déjà présent (même
    contenu) est rangé dans le projet / le bin du contexte, comme l'aurait été l'envoi."""
    b = request.get_json(silent=True) or {}
    vmid = _int(b.get("vmid"))
    if vmid is not None:
        from ..auth import check_vmid_access
        err = check_vmid_access(vmid, "operator")
        if err:
            return err
    try:
        return jsonify(dict(mu.attach_existing(str(b.get("asset_id") or ""), vmid=vmid, project_id=_int(b.get("project_id")),
                                               bin_id=_int(b.get("bin_id")), scope=ms.user_scope()), ok=True))
    except mu.UploadError as e:
        return _err(e)


@bp.route("/api/media/upload/start", methods=["POST"])
@require_perm("media.manage")
def media_upload_start():
    """{name, size, mtime, node_id?, folder?, vmid?, project_id?, bin_id?, source_name?, fp?} → session (nouvelle ou
    reprise : `resumed`, `received` = octets déjà sur le nœud). `fp` : empreinte du fichier du poste — l'identité
    réservée est celle d'un contenu identique déjà connu (le nom de fichier porte son code)."""
    b = request.get_json(silent=True) or {}
    vmid = _int(b.get("vmid"))
    if vmid is not None:
        from ..auth import check_vmid_access
        err = check_vmid_access(vmid, "operator")
        if err:
            return err
    pid, err = _scoped_target(b, vmid)
    if err:
        return err
    try:
        return jsonify(mu.start((current_user() or {}).get("id"), b.get("name"), b.get("size"), b.get("mtime"),
                                node_id=_int(b.get("node_id")), folder=b.get("folder") or "", vmid=vmid,
                                project_id=pid, bin_id=_int(b.get("bin_id")),
                                source_name=b.get("source_name"), fp=b.get("fp")))
    except mu.UploadError as e:
        return _err(e)


@bp.route("/api/media/upload/sessions")
@require_perm("media.manage")
def media_upload_sessions():
    u = current_user() or {}
    return jsonify({"sessions": mu.sessions(u.get("id"), has_global_access(u) and request.args.get("all") == "1")})


@bp.route("/api/media/upload/<sid>", methods=["GET"])
@require_perm("media.manage")
def media_upload_get(sid):
    try:
        return jsonify(mu.public(_session(sid)))
    except mu.UploadError as e:
        return _err(e)


@bp.route("/api/media/upload/<sid>", methods=["PUT"])
@require_perm("media.manage")
def media_upload_chunk(sid):
    """Corps = octets bruts du morceau ; `?offset=`. Relayé EN FLUX au nœud (jamais lu d'un bloc)."""
    try:
        s = _session(sid)
        received = mu.put_chunk(s, request.args.get("offset"), request.stream, request.content_length)
        return jsonify({"ok": True, "received": received})
    except mu.UploadError as e:
        return _err(e)


@bp.route("/api/media/upload/<sid>/finish", methods=["POST"])
@require_perm("media.manage")
def media_upload_finish(sid):
    try:
        return jsonify(mu.finish(_session(sid)))
    except mu.UploadError as e:
        return _err(e)


@bp.route("/api/media/upload/<sid>/cancel", methods=["POST"])
@require_perm("media.manage")
def media_upload_cancel(sid):
    try:
        return jsonify(mu.cancel(_session(sid)))
    except mu.UploadError as e:
        return _err(e)
