# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Page et API du JOURNAL D'EXPLOITATION des plugins (app/ops_journal.py).

    GET /ops-journal                    page (famille Monitoring)
    GET /api/ops-journal                lignes filtrées : instance, vmid, actor, category, since, until, q
    GET /api/ops-journal/facets         valeurs des filtres (instances, opérateurs, types présents)
    GET /api/ops-journal/export         CSV, mêmes filtres (séparateur `;`, BOM — cf. journaux_export)

Portée : un utilisateur à accès global voit tout ; un utilisateur scopé projet ne voit que les lignes
des instances de SES projets (colonne `project_id` posée à l'écriture). L'export suit la même règle
que l'écran : il ne révèle rien que l'écran ne montre déjà.
"""
from datetime import datetime

from flask import Response, jsonify, render_template, request

from . import bp
from ..auth import require_login, scoped_project_ids
from .. import ops_journal

CSV_COLUMNS = [("ts", "horodatage"), ("instance_label", "instance"), ("plugin_type", "plugin"),
               ("actor_kind", "auteur_type"), ("actor_name", "operateur"), ("actor_detail", "auteur_detail"),
               ("category", "type_action"), ("summary_text", "action"), ("target", "cible_ids"),
               ("instance_uuid", "instance_uuid"), ("vmid", "vmid"), ("endpoint", "endpoint")]


def _filters():
    a = request.args
    f = {}
    for k in ("instance_uuid", "actor", "actor_kind", "category", "since", "until", "q"):
        v = (a.get(k) or "").strip()
        if v:
            f[k] = v
    for k in ("vmid", "before_id"):
        try:
            if (a.get(k) or "").strip():
                f[k] = int(a.get(k))
        except ValueError:
            pass
    pids = scoped_project_ids()
    if pids is not None:
        f["project_ids"] = sorted(pids)
    return f


def _limit(default, cap):
    try:
        return max(1, min(int(request.args.get("limit", default)), cap))
    except ValueError:
        return default


@bp.route("/ops-journal")
@require_login
def ops_journal_page():
    return render_template("ops_journal.html", page="monitoring")


@bp.route("/api/ops-journal", methods=["GET"])
@require_login
def ops_journal_list():
    rows = ops_journal.query(_filters(), limit=_limit(300, ops_journal.QUERY_MAX))
    return jsonify({"rows": rows, "retention_days": ops_journal._retention_days()})


@bp.route("/api/ops-journal/facets", methods=["GET"])
@require_login
def ops_journal_facets():
    from ..i18n import t
    f = ops_journal.facets(scoped_project_ids())
    f["category_labels"] = {c: t("opsj.cat." + c) for c in f["all_categories"]}
    return jsonify(f)


@bp.route("/api/ops-journal/export", methods=["GET"])
@require_login
def ops_journal_export():
    from .journaux_export import _csv
    rows = ops_journal.query(_filters(), limit=_limit(ops_journal.QUERY_MAX, ops_journal.QUERY_MAX),
                             lang=(request.args.get("lang") or "").strip() or None)
    sep = "," if request.args.get("sep") == "," else ";"
    body = "﻿" + _csv(rows, CSV_COLUMNS, sep)
    name = "bobistudio-ops-journal-%s.csv" % datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(body, mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="%s"' % name,
        "X-Lignes": str(len(rows)), "Cache-Control": "no-store"})
