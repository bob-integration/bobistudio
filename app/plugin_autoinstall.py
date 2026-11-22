# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Plugins a node CAPABILITY cannot work without, installed from the catalogue on their own.

A node enrolled with `io2110` needs the `2110_io` plugin: its folder holds the build context of
the `bobi-mtl` image, which the ST 2110 engine AND the 2110 probe run on. The plugin is not in
the base tree — the catalogue installs it (decision of 2026-11-01). Nothing said so: a tester
ticked io2110 at install, found no image for the probe, and had no lead (2026-11-22). The
installation now fetches it when a node needs it.

Called at the end of an enrolment, by the all-in-one installer once its local node exists, and
once at boot (a catch-up when the catalogue was out of reach before). Never raises: the outcome
is an alert, and `install_gestures` still lists the plugin while it is missing.
"""
import json
import logging
import shutil
import tempfile
import threading

log = logging.getLogger(__name__)

# capability -> plugin type it requires
REQUIRED_BY_CAPABILITY = {"io2110": "2110_io"}

_lock = threading.Lock()


def _caps(node):
    try:
        return json.loads(node.get("capabilities") or "[]") or []
    except Exception:
        return []


def missing_for(nodes):
    """{plugin type: [node names]} for the required plugins that are not installed."""
    from . import plugins
    manque = {}
    for node in nodes or []:
        for cap in _caps(node):
            type_ = REQUIRED_BY_CAPABILITY.get(cap)
            if type_ and not plugins.is_plugin(type_):
                manque.setdefault(type_, []).append(node.get("name") or str(node.get("id")))
    return manque


def install(type_):
    """Installs the plugin `type_` from the catalogue → (ok, detail). Never raises."""
    from . import catalogue
    from .routes.catalogue_api import install_plugin_archive
    try:
        e = next((x for x in catalogue.lister().get("entrees") or []
                  if x.get("genre") == "plugin" and x.get("type") == type_), None)
        if not e:
            return False, "not in the catalogue"
        if not e.get("installable"):
            return False, e.get("indisponible") or "not installable"
        brut = catalogue.telecharger(e["depot"], e.get("tag"))
        tmp = tempfile.mkdtemp(prefix="autoinstall-")
        try:
            corps, code = install_plugin_archive(brut, tmp, activer=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if code in (200, 409):
            return True, corps.get("version") or ""
        return False, corps.get("error") or "HTTP %s" % code
    except Exception as exc:                 # noqa: BLE001 — an outcome, never a crash
        return False, str(exc)


def ensure(trigger="", nodes=None):
    """Installs every required plugin some node lacks → {type: (ok, detail)}."""
    from .database import db_add_alert, db_get_nodes
    from . import ha
    if not ha.is_active():                   # the standby installs nothing: the active one does
        return {}
    if not _lock.acquire(blocking=False):
        return {}
    try:
        manque = missing_for(nodes if nodes is not None else (db_get_nodes() or []))
        res = {}
        for type_, noms in manque.items():
            ok, detail = install(type_)
            res[type_] = (ok, detail)
            log.info("plugin auto-install (%s): %s for %s -> %s %s", trigger, type_,
                     ", ".join(noms), "ok" if ok else "failed", detail)
            params = {"t": type_, "nodes": ", ".join(noms), "detail": detail}
            if ok:
                db_add_alert("alert.plugin_autoinstall.ok", "info", kind="deploy", params=params)
            else:
                db_add_alert("alert.plugin_autoinstall.failed", "warning", kind="deploy", params=params)
        return res
    finally:
        _lock.release()
