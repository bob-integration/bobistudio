# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Update watch: tell the administrators that a new version is published (2026-11-08).

★ WHY. `GET /api/update/core` and the Catalogue have always known how to compare this instance
with what is published on GitHub — but only when someone opened Settings → Updates. Nobody was
ever TOLD. An instance installed in a broadcast room stays on its version for months, its admins
having no reason to open that page.

What this module does, and what it deliberately does not:
  • it CHECKS, on the active controller, at the chosen HOUR (`update_check_hour`, local time to
    the controller) and at the chosen frequency (`update_check_interval_h`, 24 h by default —
    plus `update_check_weekday` when it is weekly);
  • it NOTIFIES the way the admin chose: an alert (journal + the Alerting channels), a banner,
    a window at login, an E-MAIL to the admin who asked for one — any combination;
  • it NEVER INSTALLS. On an on-air installation the moment of an update is chosen (rule already
    written in `routes/updates.py`).

Decided with Cyril: ON by default (once a day, core only, alert + banner), and the installation
SAYS so and offers to turn it off (installer summary + first-start wizard step).

Two switches, both honoured: `update_check_enabled` (this watch) and `catalogue_actif` (no
request at all may leave for the Internet — an isolated site). The second wins.

★ THE MAIL IS PER ADMINISTRATOR (2026-11-12). Not a NOC distribution list: each administrator
subscribes for themselves, at their own account's address (`users.email`), in their own interface
language — the same grain as the « ignore this version » already stored per account. Why: the
alert channels of the Alerting service deliver to ONE global list under a level THRESHOLD, and an
available update is `info`, below the `warning` default. Lowering that threshold to let this one
mail through would have mailed every informative alert of the product.

The transport is `app/mail.py` (one SMTP server for the whole product, Settings → System → Mail).
Failure is NOT silent and NOT lost: `last_mail` in the state carries it, and `mailed` tracks what
each account really received, so the next pass retries that mail without re-announcing the
version in the journal.

The GitHub quota is not a concern: one request a day for the core, and the catalogue cache sends
conditional requests whose `304` GitHub does not count. ⚠ With `scope = all` a pass costs about
one request per published repository (~18): several instances behind ONE public IP, all checking
at the same hour, share the 60 anonymous requests per hour of that IP. Measured on 2026-11-10.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)

STATE_KEY = "update_watch_state"        # {checked_at, error, available, notified, mailed, last_mail}
DISMISSED_KEY = "update_dismissed"      # {str(uid): {item_key: version}}
SUBSCRIBERS_KEY = "update_mail_to"      # {str(uid): "1"} — who asked to be mailed, per account
INTERVALS_H = (6, 12, 24, 168)          # closed vocabulary offered by the Settings page
SCOPES = ("core", "all")
HOURS = tuple(range(24))                # local hour of the check
WEEKDAYS = tuple(range(7))              # 0 = Monday … 6 = Sunday (matches `time.localtime().tm_wday`)
_TICK_S = 300                           # how often the loop looks at the clock (settings follow)
_FIRST_DELAY_S = 120                    # let the network and the node agents come up after a boot
# A pass sends at most this many messages, however many accounts subscribed. `check()` is also
# reachable from the « Check now » button, and each send BLOCKS up to `mail_timeout_s` (15 s) on
# an unreachable server: without this cap, one button press could hold a request for minutes.
_MAIL_MAX_PER_PASS = 10
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
            "hour": _int_in(st.get("update_check_hour", 4), HOURS, 4),
            "weekday": _int_in(st.get("update_check_weekday", 0), WEEKDAYS, 0),
            "alert": _flag(st.get("update_notify_alert", "1")),
            "banner": _flag(st.get("update_notify_banner", "1")),
            "popup": _flag(st.get("update_notify_popup", "0"))}


def _flag(v):
    return str(v).strip().lower() not in ("0", "false", "off", "")


def _int_in(v, allowed, default):
    """Closed vocabulary, read side: anything else falls back to `default` rather than raising.
    The REFUSAL happens at entry (`routes/settings_api.py`), where the operator can read it."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n if n in allowed else default


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
            # `mailed`/`last_mail` sont RECOPIÉS : les perdre ici ferait renvoyer, au retour du
            # réseau, un mail pour une version déjà annoncée.
            new = {"checked_at": now, "error": "offline", "available": [], "notified": notified,
                   "mailed": dict(prev.get("mailed") or {}),
                   "last_mail": prev.get("last_mail")}
            db_set_setting(STATE_KEY, new)
            return new
        try:
            items, error = _collect(cfg["scope"])
        except Exception as e:
            log.warning("update watch: check failed (%s)", e)
            items, error = list(prev.get("available") or []), "exception"
        fresh = []
        for it in items:
            if notified.get(it["key"]) != it["version"]:
                if cfg["alert"]:
                    try:
                        _alert(it)
                    except Exception as e:
                        log.error("update watch: alert not recorded for %s (%s)", it["key"], e)
                        continue
                notified[it["key"]] = it["version"]
            fresh.append(it)
        # ⚠ `fresh` ne vaut PAS « ce qui vient d'être alerté ». Un envoi de mail peut avoir échoué
        # alors que l'alerte est passée : l'offre reste donc à poster tant que `mailed` ne la
        # porte pas pour ce compte. C'est ce découplage qui évite les deux fautes symétriques —
        # reperdre le mail, ou réécrire la même ligne dans le journal à chaque passe.
        mailed = dict(prev.get("mailed") or {})
        last_mail = _post(fresh, mailed) or prev.get("last_mail")
        new = {"checked_at": now, "error": error, "available": items, "notified": notified,
               "mailed": mailed, "last_mail": last_mail}
        db_set_setting(STATE_KEY, new)
        return new


def subscribers():
    """→ {uid(str): account} for the accounts that asked to be mailed AND can be.

    An account without an address is kept out: subscribing is harmless, but we must never report
    « mailed » for an address that does not exist. The UI says so where the box is ticked.
    """
    from .database import db_get_setting, db_get_user_by_id
    out = {}
    for uid, on in (db_get_setting(SUBSCRIBERS_KEY, {}) or {}).items():
        if not _flag(on):
            continue
        try:
            u = db_get_user_by_id(int(uid))
        except (TypeError, ValueError):
            continue
        if u and (u.get("email") or "").strip():
            out[str(uid)] = u
    return out


def subscribe(uid, on):
    """« Mail me » for THIS account. → the new state for it."""
    from .database import db_get_setting, db_set_setting
    with _lock:
        allx = dict(db_get_setting(SUBSCRIBERS_KEY, {}) or {})
        if on:
            allx[str(uid)] = "1"
        else:
            allx.pop(str(uid), None)
        db_set_setting(SUBSCRIBERS_KEY, allx)
    return bool(on)


def compose(items, lang="fr"):
    """→ (subject, body) of the mail, in `lang`. Pure: no settings, no I/O, no send.

    Separated so that a guard — and the « send a test » button — can read what is actually
    written, rather than trusting that something was written."""
    from . import i18n
    from . import settings as st
    try:
        systeme = str(st.get("brand_system_name", "") or "")
    except Exception:
        systeme = ""
    T = lambda k, **kw: i18n.t("mail.updwatch." + k, lang=lang, **kw)
    subject = T("subject_one", label=items[0]["label"], v=items[0]["version"]) \
        if len(items) == 1 else T("subject_many", n=len(items))
    if systeme:
        subject = "[%s] %s" % (systeme, subject)
    lignes = [T("intro"), ""]
    for it in items:
        lignes.append("  • " + T("line", label=it["label"], v=it["version"],
                                 inst=it.get("installed") or "?"))
        if it.get("url"):
            lignes.append("    " + it["url"])
    lignes += ["", T("never_installs"), "", T("footer")]
    return subject, "\n".join(lignes)


def _post(fresh, mailed):
    """Sends what each subscribed account has not yet received. MUTATES `mailed`.
    → a report {at, sent, failed, gaps, error} or None when there was nothing to do.

    ⚠ BLOCKING (`app.mail.send`, bounded by `mail_timeout_s`). Called from the watch thread, and
    from the « Check now » button — hence `_MAIL_MAX_PER_PASS`.
    """
    from . import mail
    if not fresh:
        return None
    abonnes = subscribers()
    if not abonnes:
        return None
    gaps = mail.missing()
    if gaps:
        # Not an exception: nobody configured the server, which is a state to SHOW, not an error
        # to log every six hours. The box is offered greyed out with this very reason.
        return {"at": time.time(), "sent": 0, "failed": 0, "gaps": gaps}
    cfg = mail.server_config()
    sent, failed, err, budget = 0, 0, None, _MAIL_MAX_PER_PASS
    for uid, u in abonnes.items():
        due = [it for it in fresh if (mailed.get(uid) or {}).get(it["key"]) != it["version"]]
        if not due:
            continue
        if budget <= 0:
            err = err or "budget"      # the rest goes out at the next pass, never dropped
            break
        budget -= 1
        subject, body = compose(due, (u.get("lang") or "fr"))
        try:
            mail.send(subject, body, [u["email"]], cfg=cfg)
        except Exception as e:
            failed += 1
            err = err or ("%s: %s" % (type(e).__name__, e))[:200]
            log.warning("update watch: mail to account %s failed (%s)", uid, e)
            continue        # `mailed` untouched → retried at the next pass
        sent += 1
        mine = dict(mailed.get(uid) or {})
        for it in due:
            mine[it["key"]] = it["version"]
        mailed[uid] = mine
    if not sent and not failed and err is None:
        return None
    return {"at": time.time(), "sent": sent, "failed": failed, "error": err}


def schedule(now=None):
    """→ what the Settings page shows about the CLOCK: the chosen slot, and the next one.

    `next_at` is None when the watch is off — showing a next check that will never happen is
    exactly the kind of promise the interface must not make."""
    cfg = _settings()
    now = time.time() if now is None else now
    return {"enabled": cfg["enabled"], "interval_h": cfg["interval_h"], "hour": cfg["hour"],
            "weekday": cfg["weekday"],
            "next_at": (next_slot(now, cfg["interval_h"], cfg["hour"], cfg["weekday"])
                        if cfg["enabled"] else None),
            "last_at": float(state().get("checked_at") or 0) or None}


def _installed_now(item):
    """The version of `item` installed NOW (None = not installed any more)."""
    if item.get("kind") == "core":
        from .version import VERSION
        return VERSION
    if item.get("kind") == "plugin":
        from . import plugins
        return (plugins.get(item["name"]) or {}).get("version") if plugins.is_plugin(item["name"]) else None
    from . import catalogue
    return catalogue._version_service_installee(item["name"])


def reconcile():
    """Drops from the stored state what is installed by now → the items still available.

    The state is the result of the last CHECK, and a check runs once a day. An update applied in
    between left the banner offering the version just installed — « 1.0.4 available (installed:
    1.0.3) » on an instance running 1.0.4, reload after reload (2026-11-25). Called at boot (a core
    update restarts the controller) and after a catalogue install. Never asks GitHub: only an item
    whose installed version CHANGED since the check is re-judged."""
    from . import catalogue
    from .database import db_set_setting
    from .version import au_moins, comparable
    with _lock:
        st = state()
        items = list(st.get("available") or [])
        kept = []
        for it in items:
            cur = _installed_now(it)
            if cur == it.get("installed"):
                kept.append(it)
            elif it.get("kind") == "core":
                if comparable(cur, it["version"]) and not au_moins(cur, it["version"]):
                    kept.append(dict(it, installed=cur))
            elif catalogue._comparer(it.get("version") or "", cur) == "maj":   # uninstalled: « absent »
                kept.append(dict(it, installed=cur))
        if kept != items:
            db_set_setting(STATE_KEY, dict(st, available=kept))
        return kept


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


def _slot_at(ts, hour):
    """Timestamp of `hour`:00 LOCAL on the calendar day of `ts`.

    ⚠ Goes through `mktime` with `isdst = -1` rather than arithmetic on 86400 s: on the two
    daylight-saving days a « day » is 23 or 25 hours long, and « every day at 4 h » must stay
    4 h on those days too.
    """
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))


def _slots_of_day(ts, interval_h, hour):
    """The slots of the calendar day of `ts`: `hour`, then every `interval_h` hours.

    6 h chosen with hour = 4 → 4 h, 10 h, 16 h, 22 h. The chosen hour is therefore an ANCHOR,
    not just the first check of the day: a frequency shorter than a day has several moments, and
    they are all derived from the one the operator picked.
    """
    return [_slot_at(ts, (hour + k * interval_h) % 24)
            for k in range(max(1, 24 // interval_h))]


def last_slot(now, interval_h, hour, weekday):
    """The most recent scheduled instant at or before `now`, or None if there is none."""
    if interval_h >= 168:
        # Weekly: the chosen weekday. `back` goes up to 7 so that « today, but before the hour »
        # falls back on the same weekday LAST week instead of returning nothing.
        for back in range(0, 8):
            ts = now - back * 86400
            if time.localtime(ts).tm_wday != weekday:
                continue
            slot = _slot_at(ts, hour)
            if slot <= now:
                return slot
        return None
    best = None
    for back in (1, 0):
        for slot in _slots_of_day(now - back * 86400, interval_h, hour):
            if slot <= now and (best is None or slot > best):
                best = slot
    return best


def next_slot(now, interval_h, hour, weekday):
    """The next scheduled instant strictly after `now` — what the Settings page announces."""
    if interval_h >= 168:
        for fwd in range(0, 8):
            ts = now + fwd * 86400
            if time.localtime(ts).tm_wday != weekday:
                continue
            slot = _slot_at(ts, hour)
            if slot > now:
                return slot
        return None
    best = None
    for fwd in (0, 1):
        for slot in _slots_of_day(now + fwd * 86400, interval_h, hour):
            if slot > now and (best is None or slot < best):
                best = slot
    return best


def _due(now):
    """Has a scheduled slot gone by since the last check?

    ⚠ Compared against the SLOT, not against « now − last check ≥ interval ». The old form made
    the hour of the check the hour of the last BOOT, and it drifted a little further at every
    pass. Comparing to the slot also catches up a missed one: a controller powered off at 4 h and
    booted at 10 h checks at boot, rather than waiting for tomorrow.
    """
    cfg = _settings()
    if not cfg["enabled"]:
        return False
    slot = last_slot(now, cfg["interval_h"], cfg["hour"], cfg["weekday"])
    if slot is None:
        return False
    return float(state().get("checked_at") or 0) < slot


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
    try:
        reconcile()
    except Exception as e:                       # never costs the boot
        log.warning("update watch: reconcile failed (%s)", e)
    threading.Thread(target=_loop, daemon=True, name="update-watch").start()
