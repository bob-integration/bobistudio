# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Internal image registry (app/registry.py): status, settings, start, and the digests each node
holds compared to what was published (the intent)."""

import threading

from flask import jsonify, request

from . import bp
from ..auth import require_login, require_perm

_KEYS = ("registry_enabled", "registry_port", "registry_storage_kind", "registry_storage_path",
         "registry_host", "registry_s3_endpoint", "registry_s3_bucket", "registry_s3_region",
         "registry_s3_access_key")


@bp.route("/api/registry/status", methods=["GET"])
@require_login
def api_registry_status():
    from .. import registry, settings
    st = registry.status()
    st["address"] = registry.address()
    st["config"] = {k: settings.get(k) for k in _KEYS}
    st["config"]["registry_s3_secret_set"] = bool(settings.get("registry_s3_secret"))
    st["published"] = registry.published()
    st["standby"] = {"checked": registry._standby_state["checked"],
                     "lag": registry._standby_state["lag"]}
    return jsonify(st)


@bp.route("/api/registry/config", methods=["POST"])
@require_perm("settings.edit")
def api_registry_config():
    """Save the settings, then (re)start the registry. The storage is CHECKED before anything is
    written: a refused path leaves the settings as they were."""
    from .. import registry, settings
    d = request.json or {}
    kind = str(d.get("registry_storage_kind") or settings.get("registry_storage_kind") or "dedicated").lower()
    path = str(d.get("registry_storage_path", settings.get("registry_storage_path")) or "").strip()
    enabled = bool(d.get("registry_enabled", settings.get("registry_enabled")))
    if kind not in registry.STORAGE_KINDS:
        return jsonify({"ok": False, "code": "unknown_kind"}), 400
    if enabled:
        st = registry.storage_check(kind, path)
        if not st["ok"]:
            return jsonify({"ok": False, "code": st["code"], "storage": st}), 400
    try:
        port = int(d.get("registry_port") or settings.get("registry_port") or registry.DEFAULT_PORT)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "code": "bad_port"}), 400
    if not 1024 <= port <= 65535:
        return jsonify({"ok": False, "code": "bad_port"}), 400
    for k in _KEYS:
        if k in d:
            settings.set(k, d[k])
    settings.set("registry_enabled", enabled)
    settings.set("registry_storage_kind", kind)
    settings.set("registry_storage_path", path)
    settings.set("registry_port", port)
    if d.get("registry_s3_secret"):                 # empty = keep the stored one
        settings.set("registry_s3_secret", d["registry_s3_secret"])
    if not enabled:
        registry.stop()
        return jsonify({"ok": True, "msg": "stopped"})
    ok, msg = registry.apply()
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 500)


@bp.route("/api/registry/digests", methods=["GET"])
@require_login
def api_registry_digests():
    """For every published tag: the expected digest and what each node holds (read on the node,
    never inferred). `match` is False when a node runs the tag with ANOTHER content."""
    from .. import registry
    from ..database import db_get_nodes
    pub = registry.published()
    nodes = [n for n in db_get_nodes() if (n.get("agent_url") or "").strip()]
    out = {}
    lock = threading.Lock()

    archs = {}

    def one(n):
        row = {}
        for tag, rec in pub.items():
            got = registry.node_digest(n, tag)
            row[tag] = {"digest": got, "match": registry.holds_published(tag, got) if got else None,
                        "missing_arch": registry.missing_platform(n, tag)}
        with lock:
            out[n.get("name") or str(n.get("id"))] = row
            archs[n.get("name") or str(n.get("id"))] = registry.node_arch(n)

    ts = [threading.Thread(target=one, args=(n,), daemon=True) for n in nodes]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=40)
    return jsonify({"published": pub, "nodes": out, "archs": archs})


@bp.route("/api/registry/versions", methods=["GET"])
@require_login
def api_registry_versions():
    """Versions kept in the registry for each image of the Images page, and the one in use."""
    from .. import registry
    from .images import _IMAGES, _image_tag, _rollback
    if not registry.usable():
        return jsonify({"ok": False, "images": {}})
    out = {}
    for which, spec in _IMAGES.items():
        try:
            vs = registry.versions(spec["prefix"])
        except Exception:
            vs = []
        if vs:
            out[which] = {"label": spec["label"], "current": _image_tag(which), "versions": vs,
                          "rollback": _rollback.get(which)}
    return jsonify({"ok": True, "images": out})


@bp.route("/api/registry/rollback", methods=["POST"])
@require_perm("settings.edit")
def api_registry_rollback():
    from .images import _IMAGES, _rollback, rollback_image
    d = request.json or {}
    which, tag = d.get("which"), (d.get("tag") or "").strip()
    if which not in _IMAGES or not tag:
        return jsonify({"ok": False, "error": "which and tag required"}), 400
    if (_rollback.get(which) or {}).get("status") == "running":
        return jsonify({"ok": False, "error": "already running"}), 409
    _rollback[which] = {"status": "running", "msg": tag, "nodes": []}   # before the thread: no double start
    threading.Thread(target=rollback_image, args=(which, tag), daemon=True).start()
    return jsonify({"ok": True, "started": True})
