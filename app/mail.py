# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Outgoing e-mail (SMTP) for the ORCHESTRATOR: one transport, one configuration (2026-11-12).

★ ORIGIN OF THE SENDING CODE. `split_addrs`, `build_message` and `_smtp_send` come from the
**Bobi.Tools Mail service** (same company, same author, same GPL-3.0-or-later licence), by way of
`services/alerting/channels/mail.py`, which imported them in 2026-08. They are MOVED here, not
copied again: the alerting channel calls this module now.

★ WHY IT MOVED. The update watch had to send a mail, and the only SMTP code in the product lived
inside an OPTIONAL git submodule. A plain installation — no alerting service — could not send
anything at all, and duplicating the sender would have meant a second SMTP server to configure
and maintain. Decided with Cyril on 2026-11-12.

★ WHAT THIS MODULE IS, AND IS NOT. A transport: build a message, open a connection, send, raise
on failure. No queue, no retry, no state. The caller decides when, to whom, and what a failure
means — the alerting service has its own queue and anti-storm grouping (`decision.py`), the
update watch simply retries at its next pass.

⚠ `send()` BLOCKS (bounded by `timeout_s`, 15 s by default). Never call it from `surveillance()`
nor from a request handler: an unreachable SMTP server would hold the loop or the browser for the
whole timeout. Call it from a background thread that owns its own pace.

★ WHERE THE SERVER COMES FROM. The core settings `mail_*` (Settings → System → Mail), and
nothing else. An instance that had configured the ALERTING channel before gets its server COPIED
here once, at boot (`migrate_from_alerting`): only when the core server is empty, and never again
afterwards (`MIGRATED_KEY`), so a core value set later is never overwritten. Until 2026-11-28 the
old keys were READ as a fallback instead: the Mail tab then showed the alerting server in fields
said to be empty, and the Alerts tab kept its own editable server — two places to set one thing.

Security: `mail_password` ends with `_password`, so it is covered by `settings._SECRET_SUFFIXES`
and therefore EXPURGATED from `settings.public()`, the HTTP output path of the settings.
"""
import logging
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr

log = logging.getLogger(__name__)

SECURITIES = ("none", "starttls", "ssl")
_LEGACY_PREFIX = "alerting_mail_"       # the alerting service's former server keys
MIGRATED_KEY = "mail_migrated_from_alerting"
_MIGRATED = ("host", "port", "security", "username", "password", "from", "timeout_s")


def _int(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def server_config():
    """→ the SMTP server to use. `source` is `"core"`, or `None` when no server is configured."""
    from . import settings as st
    source = "core"

    def g(key, default=None):
        return st.get("mail_" + key, default)

    security = str(g("security", "starttls") or "starttls").lower()
    if security not in SECURITIES:
        security = "starttls"
    host = str(g("host", "") or "").strip()
    return {"host": host,
            "port": max(1, min(65535, _int(g("port", 587), 587))),
            "security": security,
            "username": str(g("username", "") or ""),
            "password": str(g("password", "") or ""),
            "from": str(g("from", "") or "").strip(),
            "timeout_s": max(1, min(120, _int(g("timeout_s", 15), 15))),
            "source": source if host else None}


def migrate_from_alerting():
    """Copy the alerting service's server into the core settings, ONCE (called at boot).
    Only when the core server is empty: a value set in Settings → System → Mail always wins.
    → the list of copied keys (empty when nothing was copied)."""
    from . import settings as st
    if st.get(MIGRATED_KEY):
        return []
    copied = []
    if (not str(st.get("mail_host", "") or "").strip()
            and str(st.get(_LEGACY_PREFIX + "host", "") or "").strip()):
        for k in _MIGRATED:
            v = st.get(_LEGACY_PREFIX + k)
            if v not in (None, ""):
                st.set("mail_" + k, v)
                copied.append(k)
        log.info("mail: SMTP server copied from the alerting service (%s)", ", ".join(copied))
    st.set(MIGRATED_KEY, True)
    return copied


def missing(cfg=None):
    """→ the list of settings that still block an envelope, `[]` when we can send.

    Returned rather than a bare boolean on purpose: « mail not configured » sends an operator
    hunting through a whole tab, « sender address missing » does not.
    """
    cfg = cfg or server_config()
    gaps = []
    if not cfg["host"]:
        gaps.append("mail_host")
    if not cfg["from"] or "@" not in parseaddr(cfg["from"])[1]:
        gaps.append("mail_from")
    return gaps


def configured(cfg=None):
    return not missing(cfg)


def split_addrs(raw):
    """Splits an address list (CSV, semicolons, or a list) into non-empty addresses."""
    if not raw:
        return []
    parts = raw if isinstance(raw, (list, tuple)) else str(raw).replace(";", ",").split(",")
    return [a.strip() for a in parts if a and a.strip()]


def build_message(subject, body, recipients, sender):
    """Builds the EmailMessage. Raises ValueError when the sender or the recipients are
    missing or invalid."""
    if not sender or "@" not in parseaddr(sender)[1]:
        raise ValueError("sender address (mail_from) missing or invalid")
    if not recipients:
        raise ValueError("no recipient")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject or ""
    msg.set_content(body or "")
    return msg


def _smtp_send(msg, cfg):
    """Connects per the settings and sends (synchronous, blocking). Raises on failure."""
    host = cfg["host"]
    if not host:
        raise ValueError("SMTP server (mail_host) not configured")
    port, security, timeout = cfg["port"], cfg["security"], cfg["timeout_s"]

    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        smtp = smtplib.SMTP(host, port, timeout=timeout)
    try:
        smtp.ehlo()
        if security == "starttls":
            smtp.starttls()
            smtp.ehlo()
        if cfg["username"]:
            smtp.login(cfg["username"], cfg["password"])
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def send(subject, body, recipients, cfg=None):
    """Sends one message. Synchronous and BLOCKING. Raises on failure. → addresses served."""
    cfg = cfg or server_config()
    dest = split_addrs(recipients)
    _smtp_send(build_message(subject, body, dest, cfg["from"]), cfg)
    return dest
