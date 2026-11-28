# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Rapport de problème — produire, relire, télécharger (cf. `app/support_report.py`).

Deux routes, et la séparation est le point : on PRODUIT (POST, parce que ça interroge les nœuds et
que ce n'est pas gratuit), on relit à l'écran, puis on télécharge CE rapport-là par son
identifiant. Le téléchargement ne recollecte rien — sinon le fichier différerait de ce que la
personne vient de relire, et la transparence ne vaut que si les deux sont les mêmes octets.

Permission : `settings.edit`. Le rapport porte les réglages, les journaux et l'inventaire ;
c'est un geste d'administration, pas une lecture.
"""

import re

from flask import Response, jsonify, request

from . import bp
from ..auth import require_perm
from .. import support_report

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


@bp.route("/api/support-report", methods=["POST"])
@require_perm("settings.edit")
def api_support_report_build():
    data = request.get_json(silent=True) or {}
    rapport = support_report.build(include_nodes=bool(data.get("with_nodes", True)),
                                   description=data.get("description") or "")
    rid = support_report.remember(rapport)
    # `size` est la taille du FICHIER, pas celle de la réponse : c'est le nombre que la page
    # annonce, et il doit désigner ce qui sera téléchargé.
    taille = len(support_report.as_text(rapport).encode("utf-8", "replace"))
    return jsonify({"ok": True, "id": rid, "filename": support_report.filename(rapport),
                    "size": taille, "report": rapport})


@bp.route("/api/support-report/<rid>/file", methods=["GET"])
@require_perm("settings.edit")
def api_support_report_file(rid):
    if not _ID_RE.match(rid or ""):
        return jsonify({"ok": False, "error": "bad_id"}), 400
    rapport = support_report.recall(rid)
    if not rapport:
        # Le rapport n'est gardé qu'un court moment (quatre au plus). On le DIT, au lieu de
        # servir un fichier recollecté qui ne serait pas celui qui a été relu.
        return jsonify({"ok": False, "error": "report_expired"}), 404
    texte = support_report.as_text(rapport)
    return Response(texte, mimetype="text/plain; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="%s"' % support_report.filename(rapport),
        "Cache-Control": "no-store"})
