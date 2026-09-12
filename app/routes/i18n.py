# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Éditeur de traductions (page + API i18n). Nommé `i18n.py` sans risque de collision
avec `app.i18n` (le module catalogue/`t()`) : toutes les références à ce dernier dans
ce paquet utilisent `from .. import i18n` (deux points, résolu vers `app`)."""

import json

from flask import jsonify, request, render_template, Response

from . import bp
from ..auth import require_perm


@bp.route("/i18n")
@require_perm("settings.edit")
def i18n_page():
    from .. import i18n
    return render_template("i18n.html",
                           languages=i18n.LANGUAGES,
                           namespaces=i18n.namespaces(),
                           default_lang=i18n.DEFAULT_LANG)

@bp.route("/api/i18n/rows", methods=["GET"])
@require_perm("settings.edit")
def api_i18n_rows():
    from .. import i18n
    lang = request.args.get("lang") or i18n.DEFAULT_LANG
    if lang not in i18n.LANG_CODES:
        return jsonify({"error": f"langue inconnue: {lang}"}), 400
    return jsonify({"lang": lang, "rows": i18n.editor_rows(lang),
                    "namespaces": i18n.namespaces()})

@bp.route("/api/i18n/override", methods=["POST"])
@require_perm("settings.edit")
def api_i18n_override():
    from .. import i18n
    data = request.json or {}
    lang = (data.get("lang") or "").strip()
    key  = (data.get("key") or "").strip()
    if not key:
        return jsonify({"error": "clé manquante"}), 400
    try:
        i18n.set_override(lang, key, data.get("value"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok"})

@bp.route("/api/i18n/language", methods=["POST"])
@require_perm("settings.edit")
def api_i18n_add_language():
    from .. import i18n
    data = request.json or {}
    try:
        lang = i18n.add_language(data.get("code"), data.get("label"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok", "language": lang})

@bp.route("/api/i18n/language/<code>", methods=["DELETE"])
@require_perm("settings.edit")
def api_i18n_remove_language(code):
    from .. import i18n
    try:
        i18n.remove_language(code)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok"})

@bp.route("/api/i18n/export/<lang>", methods=["GET"])
@require_perm("settings.edit")
def api_i18n_export(lang):
    from .. import i18n
    if lang not in i18n.LANG_CODES:
        return jsonify({"error": f"langue inconnue: {lang}"}), 400
    body = json.dumps(i18n.export_catalog(lang), ensure_ascii=False,
                      indent=2, sort_keys=True) + "\n"
    return Response(body, mimetype="application/json", headers={
        "Content-Disposition": f'attachment; filename="{lang}.json"'})

@bp.route("/api/i18n/import", methods=["POST"])
@require_perm("settings.edit")
def api_i18n_import():
    from .. import i18n
    data = request.json or {}
    lang = (data.get("lang") or "").strip()
    try:
        n = i18n.import_overrides(lang, data.get("strings"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok", "imported": n})

@bp.route("/api/i18n/write-file", methods=["POST"])
@require_perm("settings.edit")
def api_i18n_write_file():
    from .. import i18n
    data = request.json or {}
    lang = (data.get("lang") or "").strip()
    if lang not in i18n.LANG_CODES:
        return jsonify({"error": f"langue inconnue: {lang}"}), 400
    try:
        path = i18n.write_catalog_file(lang)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"status": "ok", "path": path})
