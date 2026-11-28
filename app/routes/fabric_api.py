# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Fabric proposals (rework step 2, 2026-11-25): a wall on air is never re-cut by itself; what
the fabric would do with every wall planned together is proposed here, and applied on request.
For the Murs tool — Monitoring stays a view."""

import json
import threading
from datetime import datetime

from flask import jsonify, request

from . import bp
from ..auth import require_login, require_perm


def _cells_of(params):
    from .. import compositor_fabric as _cf
    return [{"x": e["x"], "y": e["y"], "w": e["w"], "h": e["h"], "src": e["src"]}
            for e in _cf._visible_cells(params)]


def _owner(cell, tiles):
    """The container computing this cell: the tile holding its centre, else None (the wall)."""
    cx, cy = cell["x"] + cell["w"] / 2, cell["y"] + cell["h"] / 2
    for t in tiles:
        if t["x"] <= cx < t["x"] + t["w"] and t["y"] <= cy < t["y"] + t["h"]:
            return t["ref"]
    return None


@bp.route("/api/fabric/walls", methods=["GET"])
@require_login
def api_fabric_walls():
    """Every wall of the cluster (logical multiviews, never a fabric node), what computes each of
    its cells, its level, cadence, deadlines and « if it saturates » setting, plus the proposals.
    Read only: registry, metric caches, last witness blocks — no call to a container."""
    from .. import compositor_fabric as _cf, deadlines, metrics
    from ..cpu_weight import for_container
    from ..database import db_fabric_all, db_get_containers, db_get_nodes, db_role_for_instance, get_db
    from ..deploy import manual_walls, _DELAI_JUGEMENT_S
    conts = {c["vmid"]: c for c in db_get_containers()}
    nodes = {n["id"]: n.get("name") or str(n["id"]) for n in db_get_nodes()}
    with get_db() as db:
        projects = {r[0]: r[1] for r in db.execute("SELECT id, name FROM projects").fetchall()}
    rows = db_fabric_all()
    by_shm = {r.get("shm"): r for r in rows if r.get("kind") in ("shard", "shared")}
    internal = {int(r["ref"]) for r in by_shm.values() if str(r.get("ref") or "").isdigit()}
    manual = manual_walls()
    now = datetime.now()

    def _witness(vmid):
        b = deadlines.state(vmid)
        return None if b is None else {"missed": b.get("missed"), "window_s": b.get("window_s")}

    walls = []
    for vmid, c in sorted(conts.items()):
        try:
            dc = json.loads(c.get("deploy_config") or "{}") or {}
        except ValueError:
            continue
        if dc.get("type") != "multiview" or vmid in internal or \
                str(c.get("hostname") or "").startswith("bobi-fab-"):
            continue
        p = dc.get("params") or {}
        cells = _cells_of(p)
        tiles, shards = [], []
        asm = _cf.db_fabric_get(f"asm:{vmid}")
        if asm:
            try:
                saved = (json.loads(asm.get("params") or "{}") or {}).get("asm") or {}
            except ValueError:
                saved = {}
            for w in saved.get("flux_config") or []:
                r = by_shm.get(_cf._norm_path(w.get("path")))
                if not r:
                    continue
                ref = int(r["ref"]) if str(r.get("ref") or "").isdigit() else None
                tiles.append({"x": int(w.get("x") or 0), "y": int(w.get("y") or 0),
                              "w": int(w.get("w") or 0), "h": int(w.get("h") or 0), "ref": ref})
                sc = conts.get(ref) or {}
                try:
                    parents = sorted(int(x) for x in json.loads(r.get("parents") or "[]"))
                except (TypeError, ValueError):
                    parents = []
                shards.append({"vmid": ref, "kind": r.get("kind"), "parents": parents,
                               "level": (for_container(sc, "multiview") or {}).get("niveau") if sc else None,
                               "status": sc.get("status"), "deadline": _witness(ref)})
        for cell in cells:
            cell["by"] = _owner(cell, tiles) if tiles else vmid
        role = db_role_for_instance(c.get("instance_uuid")) if c.get("instance_uuid") else None
        age = None
        try:
            age = (now - datetime.fromisoformat(c["deployed_at"])).total_seconds() if c.get("deployed_at") else None
        except (TypeError, ValueError):
            pass
        walls.append({
            "vmid": vmid, "hostname": c.get("hostname"), "label": (role or {}).get("label"),
            "project": projects.get(c.get("project_id")), "node_id": c.get("node_id"),
            "node": nodes.get(c.get("node_id")), "status": c.get("status"),
            "level": (for_container(c, "multiview") or {}).get("niveau"),
            # the polled rate (`containers.fps`): the multiview publishes no frame_index, so
            # `fps_precis_cache` is always empty for it (seen live 2026-11-25)
            "fps": metrics.fps_precis_cache.get(vmid) or c.get("fps"), "fps_target": p.get("fps") or None,
            "deadline": _witness(vmid), "on_air": age is None or age >= _DELAI_JUGEMENT_S,
            "saturation": "manual" if str(c.get("instance_uuid") or "") in manual else "auto",
            "out_w": int(p.get("out_width") or 0), "out_h": int(p.get("out_height") or 0),
            "cells": cells, "shards": shards})
    props = {str(k): dict(v, node=nodes.get(k)) for k, v in _cf.proposals().items()}
    return jsonify({"walls": walls, "proposals": props})


@bp.route("/api/fabric/walls/<int:vmid>/saturation", methods=["POST"])
@require_perm("containers.deploy")
def api_fabric_wall_saturation(vmid):
    """« If the wall saturates »: auto (cut by the fabric, the default) or manual (proposed)."""
    from .. import settings as _st
    from ..database import db_get_container
    from ..deploy import manual_walls
    mode = (request.get_json(silent=True) or {}).get("mode")
    c = db_get_container(vmid)
    if mode not in ("auto", "manual") or not c or not c.get("instance_uuid"):
        return jsonify({"error": "mode: auto|manual"}), 400
    m = manual_walls()
    (m.add if mode == "manual" else m.discard)(str(c["instance_uuid"]))
    _st.set("fabric_manual_walls", sorted(m))
    return jsonify({"ok": True, "mode": mode})


@bp.route("/api/fabric/proposals", methods=["GET"])
@require_login
def api_fabric_proposals():
    from .. import compositor_fabric as _cf
    return jsonify({str(k): v for k, v in _cf.proposals().items()})


@bp.route("/api/fabric/proposals/<int:node_id>/apply", methods=["POST"])
@require_perm("containers.deploy")
def api_fabric_proposal_apply(node_id):
    from .. import compositor_fabric as _cf
    from ..deploy import apply_fabric_proposal
    if node_id not in _cf.proposals():
        return jsonify({"error": "no proposal"}), 404
    threading.Thread(target=apply_fabric_proposal, args=(node_id,), daemon=True).start()
    return jsonify({"ok": True}), 202
