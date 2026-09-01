# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Bibliothèque de polices (Réglages → Polices) : liste, téléversement, suppression, usage.

La LISTE est ouverte à tout utilisateur loggé (elle alimente les sélecteurs de police des
éditeurs — modèles de PiP, composer multiview) ; l'écriture exige `settings.edit`.
Logique métier + gardes de sécurité : app/fonts.py."""

from flask import jsonify, request

from . import bp
from ..auth import require_login, require_perm, current_user
from ..database import db_add_alert
from .. import fonts as _fonts


@bp.route("/api/fonts", methods=["GET"])
@require_login
def api_fonts_list():
    """Catalogue complet (polices d'image + bibliothèque) pour un sélecteur.
    `?library=1` → uniquement les polices téléversées (page Réglages)."""
    if request.args.get("library"):
        return jsonify({"fonts": _fonts.list_fonts(),
                        "bytes": _fonts.library_bytes(),
                        "max_bytes": _fonts.MAX_LIBRARY_BYTES,
                        "max_font_bytes": _fonts.MAX_FONT_BYTES})
    return jsonify({"fonts": _fonts.catalog(), "default": _fonts.DEFAULT_FONT_KEY})


@bp.route("/api/fonts", methods=["POST"])
@require_perm("settings.edit")
def api_fonts_upload():
    """Téléverse une police (multipart, champ `font`). Le fichier est validé (signature sfnt
    + chargement Pillow effectif), borné en taille, et écrit sous un nom dérivé du sha256."""
    f = request.files.get("font")
    if not f or not f.filename:
        return jsonify({"error": "missing_file"}), 400
    data = f.read(_fonts.MAX_FONT_BYTES + 1)
    try:
        pub, created = _fonts.add_font(
            data, filename=f.filename, name=(request.form.get("name") or None),
            created_by=(current_user() or {}).get("username") or "")
    except _fonts.FontError as e:
        return jsonify({"error": e.code, "detail": e.detail}), 400
    if created:
        db_add_alert("alert.advisory.police_ajoutee", "info", kind="advisory", params={"name": pub["name"]})
    return jsonify({"status": "ok", "font": pub, "created": created})


@bp.route("/api/fonts/<key>/usage", methods=["GET"])
@require_login
def api_fonts_usage(key):
    return jsonify({"usage": _fonts.usage(_norm(key))})


@bp.route("/api/fonts/<key>", methods=["DELETE"])
@require_perm("settings.edit")
def api_fonts_delete(key):
    """Supprime une police. Refuse (409 + liste d'usage) si elle est utilisée, sauf `?force=1`
    (l'appelant a alors affiché l'avertissement : les usages retomberont sur DejaVu)."""
    key = _norm(key)
    force = bool(request.args.get("force"))
    ok, used = _fonts.delete_font(key, force=force)
    if not ok and used:
        return jsonify({"error": "in_use", "usage": used}), 409
    if not ok:
        return jsonify({"error": "not_found"}), 404
    if used:
        db_add_alert("alert.advisory.police_supprimee_utilisee", "warning", kind="advisory",
                     params={"n": len(used)})
    return jsonify({"status": "ok", "usage": used})


def _norm(key):
    """Tolère `lib:<sha16>` comme `<sha16>` dans l'URL (le « : » est pénible à router)."""
    key = str(key or "")
    return key if key.startswith("lib:") else "lib:" + key
