# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""MISSING-FRAME MODE: what a processing stage emits when an input frame is missing.

Decision of 2026-10-30 (Cyril): REPEAT the last valid frame by default in operation, BLACK
available for commissioning (to see exactly when a source skips a frame).

Why it exists. A DeckLink capture that receives a frame more than one slot late jumps its index
and leaves a HOLE (`decklink_rx.cpp`, deliberately: the hole is the information — filling it at the
capture would deprive every consumer of the choice). A consumer that reads the hole gets nothing.
Before this setting the mixer then composed BLACK: the flashes seen in operation.

The contract (shared with the DeckLink output, which implements its own side):
  · global setting `missing_frame_mode` (Settings → MXL → “Image manquante”): `repeat` (default) | `black`;
  · per-container override: config_schema key `missing_frame_mode` = "" | `repeat` | `black`.
    "" means "follows the global" and STAYS PERSISTED as "": it is NOT resolved through
    `plugins.effective_deploy_defaults` (that would freeze the global at deployment time);
  · resolution AT RENDER TIME (`plugins.render_config`, the one place every render and every hot
    reconfiguration goes through): the script receives `missing_frame_mode_effective` and reads
    nothing else — it never looks at a setting;
  · changing the global PUSHES a live reconfiguration (`control.hot_config`) to every live
    container that follows it; an unreachable container is logged AND alerted, never skipped
    silently.
"""

import json
import logging
import threading

log = logging.getLogger(__name__)

KEY = "missing_frame_mode"
EFFECTIVE_KEY = "missing_frame_mode_effective"
MODES = ("repeat", "black")
DEFAULT = "repeat"


def _norm(v):
    """`repeat` | `black`, or "" when the value does not name a mode (follows the global)."""
    s = str(v or "").strip().lower()
    return s if s in MODES else ""


def global_mode():
    """The global setting, normalised (an unreadable stored value falls back to the default)."""
    try:
        from . import settings
        return _norm(settings.get(KEY)) or DEFAULT
    except Exception:                                                     # noqa: BLE001
        log.exception("missing_frame: global setting unreadable — using %s", DEFAULT)
        return DEFAULT


def effective(override, global_value=None):
    """Mode the script must apply: the container override when it names a mode, else the global."""
    o = _norm(override)
    if o:
        return o
    return (_norm(global_value) if global_value is not None else global_mode()) or DEFAULT


def declares(type_):
    """True when the plugin's config_schema carries `missing_frame_mode` (generic: no type list)."""
    from . import plugins
    schema = (plugins.get(type_) or {}).get("config_schema") or []
    return any((f or {}).get("key") == KEY for f in schema)


def _params_of(c):
    dc = c.get("deploy_config")
    try:
        dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    except Exception:                                                     # noqa: BLE001
        dc = {}
    return dc.get("type"), (dc.get("params") or {})


def followers():
    """Containers that FOLLOW the global: plugin declares the key, persisted value is "" (or
    absent — a container deployed before the key existed), running, script enabled."""
    from .database import db_get_containers
    out = []
    for c in db_get_containers():
        t, p = _params_of(c)
        if not t or not declares(t):
            continue
        if _norm(p.get(KEY)):
            continue                      # explicit value: the global does not touch it
        if c.get("status") != "running" or not c.get("script_enabled", 1):
            continue
        out.append((c["vmid"], t, p))
    return out


def apply_live(vmid, type_, params):
    """Re-render and push the configuration of ONE live container WITHOUT restarting it.

    Goes through the normal deployment path (same rendering, the script file on disk is rewritten
    so a later restart keeps the mode), in `hot_only_keys` mode: the hot reconfiguration is the
    only way the new mode may reach the running script — never a stop/start. Returns True when the
    container confirmed the mode (HTTP 200, or its answer reports the expected effective mode)."""
    from . import plugins
    from .deploy import deployer_script
    pv = params.get("plugin_version")
    version = pv if (pv and pv in plugins.versions(type_)) else None
    return bool(deployer_script(vmid=vmid, type_script=type_, params=dict(params), version=version,
                                hot_only_keys=(EFFECTIVE_KEY,)))


def push_global_change(old, new, wait=False):
    """The global changed from `old` to `new`: push it to every container that follows it.

    Background thread (the settings route answers at once). Each container: `apply_live`. A
    failure (unreachable, script too old to reconfigure live, version drift) is logged and ALERTED
    with the container named — the setting would otherwise do nothing until the next deployment
    without anyone knowing."""
    old, new = _norm(old) or DEFAULT, _norm(new) or DEFAULT
    if old == new:
        return None

    def _run():
        from .database import db_add_alert
        targets = followers()
        ok, failed = [], []
        for vmid, t, p in targets:
            try:
                good = apply_live(vmid, t, p)
            except Exception as e:                                        # noqa: BLE001
                # deployer_script alerte lui-même ses refus (version, conteneur injoignable, script
                # qui ne prend pas la clé) ; une EXCEPTION, elle, n'a laissé aucune trace.
                log.warning("missing_frame: live push to #%s failed (%s)", vmid, e)
                db_add_alert("alert.missing_frame.push_failed", "warning", vmid=vmid, kind="deploy",
                             params={"vmid": vmid, "mode": new, "e": str(e)[:120]})
                good = False
            (ok if good else failed).append(vmid)
        log.info("missing_frame: global %s -> %s pushed live to %d container(s), %d failed%s",
                 old, new, len(ok), len(failed),
                 (" (%s)" % ", ".join("#%s" % v for v in failed)) if failed else "")
        db_add_alert("alert.missing_frame.global_changed", "warning" if failed else "info",
                     kind="deploy", params={"old": old, "new": new, "ok": len(ok),
                                            "failed": len(failed)})
        return ok, failed

    if wait:
        return _run()
    threading.Thread(target=_run, name="missing-frame-push", daemon=True).start()
    return None
