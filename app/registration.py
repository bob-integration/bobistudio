# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Registering an installation: a PREPARED MAIL the administrators send themselves (2026-11-08).

★ WHY. We do not know who uses the product: GitHub shows stars, forks and download counts, never
a name. Decided with Cyril: offer the administrators to register, by a mail to
`register@bobi.studio` that THEY send from their own mail client. No server of ours receives
anything, and nothing leaves this controller: the browser opens a `mailto:` link, and the text is
shown before, word for word, with a « Copy » button for a control-room PC without a mail client.

The offer comes ONCE, as a window, to administrators only, one day after the first visit (not in
the middle of an installation), and can be refused for good. A section of Settings → Updates
keeps it available at any time.

⚠ THE ADDRESS IS NOT A SETTING, for the same reason as `config.CATALOGUE_ORG`: editable from the
web, `settings.edit` would let anyone redirect every registration to an address of their choice.
It lives in the code, `config.REGISTER_EMAIL` can override it (server access required).
"""
import time
import urllib.parse

DEFAULT_EMAIL = "register@bobi.studio"
STATE_KEY = "registration_state"        # {status, first_seen, later_until, at, info}
STATUSES = ("", "registered", "declined", "later")
USAGES = ("control_room", "ob_van", "training", "evaluation", "development", "other")
LATER_S = 30 * 86400                    # « Later »: the window comes back in 30 days
FIRST_DELAY_S = 86400                   # first offer one day after the first visit
_FIELDS = ("org", "name", "email", "country", "usage", "comment")
_MAX = {"org": 200, "name": 200, "email": 200, "country": 100, "usage": 40, "comment": 2000}


def email():
    from . import config
    return str(getattr(config, "REGISTER_EMAIL", "") or DEFAULT_EMAIL).strip()


def state():
    from .database import db_get_setting
    s = db_get_setting(STATE_KEY, {}) or {}
    return s if isinstance(s, dict) else {}


def _save(s):
    from .database import db_set_setting
    db_set_setting(STATE_KEY, s)


def offer_due(now=None):
    """Should the window be shown now? Records the first visit the first time it is asked."""
    now = time.time() if now is None else now
    s = state()
    if s.get("status") in ("registered", "declined"):
        return False
    if not s.get("first_seen"):
        s["first_seen"] = now
        _save(s)
        return False
    if now - float(s["first_seen"]) < FIRST_DELAY_S:
        return False
    return now >= float(s.get("later_until") or 0)


def clean(info):
    """The form, trimmed and bounded. Unknown fields are dropped, `usage` is a closed vocabulary."""
    info = info if isinstance(info, dict) else {}
    out = {k: str(info.get(k) or "").strip()[:_MAX[k]] for k in _FIELDS}
    if out["usage"] not in USAGES:
        out["usage"] = ""
    out["facts"] = bool(info.get("facts", True))
    return out


def facts():
    """What « attach the version and the size of the installation » adds. Shown before sending."""
    from .version import VERSION
    from .database import db_get_nodes
    from . import plugins, core_plugins
    try:
        nodes = len(db_get_nodes() or [])
    except Exception:
        nodes = None
    try:
        pl = sorted(m.get("type") for m in (plugins.all() or []) if m.get("type"))
    except Exception:
        pl = []
    try:
        sv = sorted(m.get("id") for m in (core_plugins.manifest_list() or []) if m.get("id"))
    except Exception:
        sv = []
    return {"version": VERSION, "nodes": nodes, "plugins": pl, "services": sv}


def compose(info):
    """→ {to, subject, body, mailto}: the exact mail, in the language of the person preparing it."""
    from .i18n import t
    info = clean(info)

    def line(k, v):                     # « Organisation : … » / « Organisation: … »
        return t("settings.reg.mail_line").replace("{k}", str(k)).replace("{v}", str(v))
    lines = [t("settings.reg.mail_intro"), ""]
    for k in _FIELDS:
        v = info[k]
        if k == "usage" and v:
            v = t("settings.reg.usage_" + v)
        lines.append(line(t("settings.reg.f_" + k), v or "—"))
    if info["facts"]:
        f = facts()
        lines += ["", t("settings.reg.mail_facts"),
                  "Bobi.Studio %s" % f["version"],
                  line(t("settings.reg.mail_nodes"), "?" if f["nodes"] is None else f["nodes"]),
                  line(t("settings.reg.mail_plugins"), ", ".join(f["plugins"]) or "—"),
                  line(t("settings.reg.mail_services"), ", ".join(f["services"]) or "—")]
    subject = t("settings.reg.mail_subject").replace("{org}", info["org"] or "?")
    body = "\n".join(lines)
    to = email()
    mailto = "mailto:%s?%s" % (to, urllib.parse.urlencode(
        {"subject": subject, "body": body}, quote_via=urllib.parse.quote))
    return {"to": to, "subject": subject, "body": body, "mailto": mailto}


def act(action, info=None, now=None):
    """prepare | later | decline | reset → (state, composed mail or None). Raises ValueError."""
    now = time.time() if now is None else now
    s = state()
    mail = None
    if action == "prepare":
        info = clean(info)
        if not info["org"]:
            raise ValueError("org")
        mail = compose(info)
        # « Registered » as soon as the mail is prepared: we cannot know whether it left the mail
        # client, and asking again would be worse. The section offers to send it again.
        s.update({"status": "registered", "at": now, "info": info})
    elif action == "later":
        s.update({"status": "later", "later_until": now + LATER_S})
    elif action == "decline":
        s.update({"status": "declined", "at": now})
    elif action == "reset":
        s = {"first_seen": s.get("first_seen") or now}
    else:
        raise ValueError("action")
    _save(s)
    return s, mail
