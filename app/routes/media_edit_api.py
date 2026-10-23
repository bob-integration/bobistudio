# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Éditeur d'image de la base des médias (recadrage, fond, calques, rendu pleine résolution) —
routes `/api/media/edit/*`. Logique dans app/media_edit.py.

Lecture : connecté. Enregistrer (rendu + import dans la base) : permission `media.manage`.
L'aperçu en direct n'a PAS de route ici : le navigateur réutilise `/api/media/assets/<id>/frame`."""
from flask import jsonify, request, send_file

from . import bp
from ..auth import require_login, require_perm
from .. import media_sharing as ms
from .. import media_edit as me
from .. import media_registry as mr


def _visible_original(aid):
    """Portée (cf. media_registry_api) : le média ET son original doivent être dans la portée de
    l'utilisateur (ses projets OU un partage, niveau « utiliser » suffisant pour lire et éditer vers une
    NOUVELLE fiche) — sinon 404, comme partout dans la base. Renvoie (scope, réponse d'erreur|None).
    Portée : media_sharing.user_scope(), la seule."""
    scope = ms.user_scope()
    if scope is None:
        return scope, None
    if not mr.asset_visible(aid, scope):
        return scope, (jsonify({"error": "média inconnu"}), 404)
    try:
        orig, _e = me.resolve_original(aid)
    except ValueError:
        return scope, None
    if not mr.asset_visible(orig["id"], scope):
        return scope, (jsonify({"error": "média inconnu"}), 404)
    return scope, None


@bp.route("/api/media/edit/fonts")
@require_login
def media_edit_fonts():
    return jsonify({"fonts": me.font_catalog()})


@bp.route("/api/media/edit/font/<path:key>")
@require_login
def media_edit_font(key):
    """Fichier de la police (le navigateur la charge en FontFace : même police que le rendu serveur)."""
    p = me.font_file(key)
    if not p:
        return jsonify({"error": "police introuvable"}), 404
    return send_file(p, max_age=3600)


@bp.route("/api/media/edit/<aid>/info")
@require_login
def media_edit_info(aid):
    """Ce qu'il faut pour ouvrir l'éditeur : l'original (une édition renvoie son original), la
    recette de l'édition demandée, les variantes existantes, les formats prédéfinis."""
    scope, err = _visible_original(aid)
    if err:
        return err
    try:
        d = me.info(aid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if scope is not None:
        ok = set(mr.visible_ids([v["id"] for v in d["variants"]], scope))
        ok |= {v["id"] for v in d["variants"] if v["id"].startswith(me.VERSION_PREFIX)}   # versions : suivent l'original
        d["variants"] = [v for v in d["variants"] if v["id"] in ok]
        if d.get("editing") and d["editing"]["id"] not in ok and d["editing"]["id"] != aid:
            d["editing"] = None
    return jsonify(d)


@bp.route("/api/media/edit/<aid>/save", methods=["POST"])
@require_perm("media.manage")
def media_edit_save(aid):
    """Corps : `{recipe, overwrite?: <id d'édition>, confirm_overwrite?: true}`. Sans `overwrite`,
    une NOUVELLE variante. Écraser exige la confirmation explicite. Renvoie `{job}` (état en GET)."""
    scope, err = _visible_original(aid)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    ow = body.get("overwrite") or None
    is_ver = bool(ow) and str(ow).startswith(me.VERSION_PREFIX)
    if ow and not is_ver and scope is not None and not mr.asset_visible(ow, scope):
        return jsonify({"error": "média inconnu"}), 404
    if ow and body.get("confirm_overwrite") is not True:
        return jsonify({"error": "écraser une édition exige confirm_overwrite=true"}), 409
    try:
        me.normalize_recipe(body.get("recipe"))
        orig, _e = me.resolve_original(aid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # Ce qui touche la fiche d'un média seulement PARTAGÉ exige « Modifier » : écraser une retouche-version,
    # ou ranger la sortie comme version (sinon : fiche à part, comme le réglage par défaut).
    owned = scope is None or ms.owns_asset(scope, orig["id"])
    can_edit = owned or ms.asset_allows(scope, orig["id"], "edit")
    if is_ver and not can_edit:
        return jsonify({"ok": False, "error": "forbidden", "reason": "shared_read_only"}), 403
    if ow and not is_ver and not owned and not ms.asset_allows(scope, ow, "edit"):
        return jsonify({"ok": False, "error": "forbidden", "reason": "shared_read_only"}), 403
    as_version = is_ver or (me.output_mode() == me.OUTPUT_VERSION and can_edit)
    link = None if owned else sorted(ms.base_scope(scope) or ())      # partagée : la nouvelle fiche va chez l'éditeur
    if link is not None and not link:
        link = None                                                    # aucun projet : repli sur ceux de l'originale
    return jsonify({"job": me.start_save(orig["id"], body.get("recipe"), ow, as_version=as_version, link_projects=link)})


@bp.route("/api/media/edit/jobs/<jid>")
@require_login
def media_edit_job(jid):
    j = me.job(jid)
    if not j:
        return jsonify({"error": "tâche inconnue"}), 404
    return jsonify(j)


from . import media_upload_api  # noqa: E402,F401 — envoi depuis le poste (routes /api/media/upload/*)
