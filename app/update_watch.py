# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Update watch: tell the administrators that a new version is published (2026-11-08).

★ WHY. `GET /api/update/core` and the Catalogue have always known how to compare this instance
with what is published on GitHub — but only when someone opened Settings → Updates. Nobody was
ever TOLD. An instance installed in a broadcast room stays on its version for months, its admins
having no reason to open that page.

What this module does, and what it deliberately does not:
  • it CHECKS, on the active controller, every `update_check_interval_h` hours (24 by default);
  • it NOTIFIES the way the admin chose: an alert (journal + the Alerting channels), a banner,
    a window at login — any combination;
  • it NEVER INSTALLS. On an on-air installation the moment of an update is chosen (rule already
    written in `routes/updates.py`).

Decided with Cyril: ON by default (once a day, core only, alert + banner), and the installation
SAYS so and offers to turn it off (installer summary + first-start wizard step).

Two switches, both honoured: `update_check_enabled` (this watch) and `catalogue_actif` (no
request at all may leave for the Internet — an isolated site). The second wins.

The GitHub quota is not a concern: one request a day for the core, and the catalogue cache sends
conditional requests whose `304` GitHub does not count.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)

STATE_KEY = "update_watch_state"        # {checked_at, error, available: [...], notified: {key: v}}
DISMISSED_KEY = "update_dismissed"      # {str(uid): {item_key: version}}
INTERVALS_H = (6, 12, 24, 168)          # closed vocabulary offered by the Settings page
SCOPES = ("core", "all")
_TICK_S = 300                           # how often the loop looks at the clock (settings follow)
_FIRST_DELAY_S = 120                    # let the network and the node agents come up after a boot
_lock = threading.Lock()


def _settings():
    from . import settings as st
    on = str(st.get("update_check_enabled", "1")).strip().lower() not in ("0", "false", "off", "")
    try:
        h = int(st.get("update_check_interval_h", 24) or 24)
    except (TypeError, ValueError):
        h = 24
    scope = st.get("update_check_scope") or "core"
    return {"enabled": on, "interval_h": h if h in INTERVALS_H else 24,
            "scope": scope if scope in SCOPES else "core",
            "alert": _flag(st.get("update_notify_alert", "1")),
            "banner": _flag(st.get("update_notify_banner", "1")),
            "popup": _flag(st.get("update_notify_popup", "0"))}


def _flag(v):
    return str(v).strip().lower() not in ("0", "false", "off", "")


def state():
    from .database import db_get_setting
    return db_get_setting(STATE_KEY, {}) or {}


def _collect(scope):
    """→ (available items, error or None). An item: {key, kind, name, label, version, installed, url}."""
    from . import catalogue
    items, error = [], None
    dispo, info = catalogue.maj_core_disponible(force=True)
    if info is None:
        error = "core"
    elif dispo:
        items.append({"key": "core", "kind": "core", "name": "bobistudio", "label": "Bobi.Studio",
                      "version": info["version"], "installed": info.get("version_installee"),
                      "url": info.get("url") or ""})
    if scope == "all":
        cat = catalogue.lister(force=True)
        if cat.get("erreur"):
            error = error or "catalogue"
        for e in cat.get("entrees") or []:
            if e.get("etat") != "maj":
                continue
            items.append({"key": "%s:%s" % (e["genre"], e["type"]), "kind": e["genre"],
                          "name": e["type"], "label": e.get("label") or e["type"],
                          "version": e.get("version_dispo"), "installed": e.get("version_installee"),
                          "url": e.get("url") or ""})
    return items, error


def _alert(item):
    from .database import db_add_alert
    # Literal keys, within reach of the call: tests/verif_cles_alertes.py only sees those.
    # `advisory` = « the operator arbitrates » — exactly what an available update is. A dedicated
    # kind would also mean a new value in the published SNMP MIB.
    params = {"label": item["label"], "v": item["version"], "inst": item.get("installed") or "?"}
    if item["kind"] == "core":
        db_add_alert("alert.update.core_available", "info", kind="advisory", params=params,
                     antirebond=False)
    else:
        db_add_alert("alert.update.component_available", "info", kind="advisory", params=params,
                     antirebond=False)


def check(now=None):
    """One pass. Raises one alert per NEW published version (never twice for the same one).
    → the state written. Callable at any time (button « Check now »), whatever the switch."""
    from . import catalogue
    from .database import db_set_setting
    cfg = _settings()
    now = time.time() if now is None else now
    with _lock:
        prev = state()
        notified = dict(prev.get("notified") or {})
        if not catalogue._reglages()[2]:
            # Internet forbidden on this site: say so rather than show an old « available ».
            new = {"checked_at": now, "error": "offline", "available": [], "notified": notified}
            db_set_setting(STATE_KEY, new)
            return new
        try:
            items, error = _collect(cfg["scope"])
        except Exception as e:
            log.warning("update watch: check failed (%s)", e)
            items, error = list(prev.get("available") or []), "exception"
        for it in items:
            if notified.get(it["key"]) == it["version"]:
                continue
            if cfg["alert"]:
                try:
                    _alert(it)
                except Exception as e:
                    log.error("update watch: alert not recorded for %s (%s)", it["key"], e)
                    continue
            notified[it["key"]] = it["version"]
        new = {"checked_at": now, "error": error, "available": items, "notified": notified}
        db_set_setting(STATE_KEY, new)
        return new


def notice_for(uid):
    """What the banner / window shows to THIS administrator: available items minus the ones
    they dismissed at that version. A newer version comes back, by design."""
    from .database import db_get_setting
    cfg = _settings()
    st = state()
    gone = ((db_get_setting(DISMISSED_KEY, {}) or {}).get(str(uid)) or {})
    items = [i for i in (st.get("available") or []) if gone.get(i["key"]) != i["version"]]
    return {"enabled": cfg["enabled"], "banner": cfg["banner"], "popup": cfg["popup"],
            "items": items, "checked_at": st.get("checked_at")}


def dismiss(uid):
    """« Ignore this version »: for this admin, every item currently shown, at its version."""
    from .database import db_get_setting, db_set_setting
    with _lock:
        allx = db_get_setting(DISMISSED_KEY, {}) or {}
        mine = dict(allx.get(str(uid)) or {})
        for i in state().get("available") or []:
            mine[i["key"]] = i["version"]
        allx[str(uid)] = mine
        db_set_setting(DISMISSED_KEY, allx)
    return mine


def _due(now):
    cfg = _settings()
    if not cfg["enabled"]:
        return False
    last = float(state().get("checked_at") or 0)
    return now - last >= cfg["interval_h"] * 3600


def _loop():
    time.sleep(_FIRST_DELAY_S)
    while True:
        try:
            if _due(time.time()):
                check()
        except Exception as e:
            log.error("update watch: loop error (%s)", e)
        time.sleep(_TICK_S)


def start():
    """Started by main.py on the ACTIVE controller (it is the one that alerts)."""
    threading.Thread(target=_loop, daemon=True, name="update-watch").start()
