"""Project environment routes (see `app/project_environment.py`).

GET /api/plugins/<type>/project_notice   reminder before a container of that type joins a project
GET /api/containers/<vmid>/project_notice   same, for an existing container
GET /api/projects/<pid>/environment      services the project needs that this cluster lacks
"""
import json

from flask import jsonify

from . import bp
from ..auth import require_login, require_project_role
from .. import project_environment as penv
from ..database import db_get_project, db_get_container


@bp.route("/api/plugins/<type_>/project_notice", methods=["GET"])
@require_login
def project_notice(type_):
    return jsonify({"notice": penv.attach_notice(type_)})


@bp.route("/api/containers/<int:vmid>/project_notice", methods=["GET"])
@require_login
def container_project_notice(vmid):
    c = db_get_container(vmid) or {}
    dc = c.get("deploy_config")
    try:
        dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    except Exception:
        dc = {}
    return jsonify({"notice": penv.attach_notice(dc.get("type"))})


@bp.route("/api/projects/<int:pid>/environment", methods=["GET"])
@require_project_role("viewer")
def project_environment(pid):
    p = db_get_project(pid)
    if not p:
        return jsonify({"error": "projet introuvable"}), 404
    return jsonify({"missing": penv.missing(pid, p.get("snapshot") or []),
                    "recorded": penv.recorded(pid)})
