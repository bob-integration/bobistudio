# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Last output of a container script that DIED ON ITS OWN, kept in the controller database.

Why (2026-10-30): on 29/10 at 18:16:57 the Z8 mixer #227 script stopped by itself right after a
redeploy (« script terminé (code 1) »). The container was recreated at 21:30; `docker logs` starts
from zero at each recreation, and nothing else had kept the output — the traceback was LOST, and
the cause could not be found. The durable host journal (`app/journal.py`, journald driver) is the
general answer, but it depends on the node (persistent journal, recent node agent, setting left on
`journald`) and it is size-capped: a crash can rotate out before anyone reads it.

Chosen design — CONTROLLER side, no contract change:
  - the per-container agent ALREADY tells a spontaneous death from a `/stop`: `last_exit_ts` (in
    `/status`) is set only when the script process ended without being asked to (`_is_running`),
    never by `_stop_proc`. A NEW `last_exit_ts` value is therefore exactly « died on its own »;
  - at that moment the container still exists, so the controller reads the last lines of its
    output from the node agent (`GET /v1/containers/<name>/logs`, i.e. `docker logs --tail`, which
    works with both log drivers), MASKS the obvious secrets, BOUNDS the size and stores the result
    in `script_crash_tails`. The row outlives the container, the node journal and the agent.
  - one row per death: deduplicated on (vmid, last_exit_ts), so the surveillance loop and the
    deploy path (« died right after start ») share the same capture.
The alternative — a ring buffer inside the agent — was rejected: the agent is baked into the
runtime IMAGES (`COPY script_templates/agent.py`), so it would only reach a site after rebuilding
every image and recreating every container, and the script output would have to go through a relay
thread of the agent (a stalled relay blocks the script on a full pipe). This one takes effect with
an orchestrator update alone, on every existing container.

No silent failure: when the output could not be read, a row is still written with
`status='unavailable'` and the reason, and the caller raises an alert that says so.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime

from .database import get_db

log = logging.getLogger(__name__)

MAX_LINES = 200                 # lines asked to the node and kept
MAX_BYTES = 64 * 1024           # stored text never exceeds this (UTF-8 bytes), the END is kept
KEEP_PER_VMID = 20              # retention: newest rows per vmid
KEEP_TOTAL = 500                # retention: newest rows overall
FETCH_TIMEOUT_S = 15
MAX_AGE_S = 600                 # a death older than this is not captured as new (see is_new_death)

_lock = threading.Lock()        # check-then-insert dedup between surveillance and deploy threads

# ─── Masking ────────────────────────────────────────────────────────────────────────────────
# Minimal and conservative: a script output can carry tokens, credentials in URLs, auth headers.
# Each rule keeps the NAME (useful to diagnose) and replaces the VALUE.
MASK = "***"
_RULES = [
    # Authorization: Bearer xxx / Basic xxx ; X-MXL-Agent-Token: xxx ; any *-Token / *-Key header
    (re.compile(r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)(?:(bearer|basic|token)\s+)?[^\s,;'\"]+"),
     lambda m: m.group(1) + m.group(2) + ((m.group(3) + " ") if m.group(3) else "") + MASK),
    (re.compile(r"(?i)\b([\w-]*(?:token|api[-_]?key|secret)[\w-]*)(\s*[:=]\s*)(['\"]?)[^\s,;&'\"}]+"),
     lambda m: m.group(1) + m.group(2) + m.group(3) + MASK),
    (re.compile(r"(?i)\b([\w-]*(?:password|passwd|pwd|passphrase)[\w-]*)(\s*[:=]\s*)(['\"]?)[^\s,;&'\"}]+"),
     lambda m: m.group(1) + m.group(2) + m.group(3) + MASK),
    # JSON / repr style: "token": "xxx" , 'password': 'xxx'
    (re.compile(r"(?i)(['\"][\w-]*(?:token|password|passwd|secret|api[-_]?key)[\w-]*['\"]\s*:\s*['\"])[^'\"]*(['\"])"),
     lambda m: m.group(1) + MASK + m.group(2)),
    # scheme://user:pass@host → scheme://user:***@host
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:)[^\s/@]+@"),
     lambda m: m.group(1) + MASK + "@"),
]


def redact(text):
    """Mask the obvious secrets of a text. Never raises."""
    if not text:
        return text or ""
    out = str(text)
    for rx, rep in _RULES:
        try:
            out = rx.sub(rep, out)
        except Exception:
            pass
    return out


def bound(lines, max_lines=MAX_LINES, max_bytes=MAX_BYTES):
    """Join the LAST `max_lines` lines and keep at most `max_bytes` UTF-8 bytes from the END
    (a Python traceback ends with its message). Returns (text, truncated)."""
    lines = [str(l).rstrip("\r\n") for l in (lines or [])]
    truncated = len(lines) > max_lines
    lines = lines[-max_lines:] if max_lines else []
    text = "\n".join(lines)
    raw = text.encode("utf-8", "replace")
    if len(raw) > max_bytes:
        truncated = True
        raw = raw[-max_bytes:]
        text = raw.decode("utf-8", "ignore")
        nl = text.find("\n")             # drop the partial first line
        if 0 <= nl < len(text) - 1:
            text = text[nl + 1:]
    return text, truncated


# ─── Storage ────────────────────────────────────────────────────────────────────────────────
def ensure_table(db=None):
    db = db or get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS script_crash_tails (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        vmid          INTEGER NOT NULL,
        instance_uuid TEXT,
        hostname      TEXT,
        node_id       INTEGER,
        docker_name   TEXT,
        exit_code     INTEGER,
        exit_signal   INTEGER,
        exit_ts       REAL,
        captured_at   TEXT NOT NULL,
        source        TEXT,
        status        TEXT NOT NULL,      -- ok | unavailable
        error         TEXT,
        truncated     INTEGER NOT NULL DEFAULT 0,
        tail          TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_crash_tails_vmid ON script_crash_tails(vmid, id)")


def _row(r):
    return dict(r) if r is not None else None


def _prune(db, vmid):
    db.execute("""DELETE FROM script_crash_tails WHERE vmid=? AND id NOT IN (
                    SELECT id FROM script_crash_tails WHERE vmid=? ORDER BY id DESC LIMIT ?)""",
               (vmid, vmid, KEEP_PER_VMID))
    db.execute("""DELETE FROM script_crash_tails WHERE id NOT IN (
                    SELECT id FROM script_crash_tails ORDER BY id DESC LIMIT ?)""", (KEEP_TOTAL,))


def get(tail_id):
    with get_db() as db:
        ensure_table(db)
        return _row(db.execute("SELECT * FROM script_crash_tails WHERE id=?",
                               (int(tail_id),)).fetchone())


def list_for(vmid, limit=KEEP_PER_VMID):
    """Newest first, WITHOUT the text (listing)."""
    with get_db() as db:
        ensure_table(db)
        rows = db.execute("""SELECT id, vmid, instance_uuid, hostname, node_id, exit_code, exit_signal,
                                    exit_ts, captured_at, source, status, error, truncated,
                                    length(tail) AS size
                             FROM script_crash_tails WHERE vmid=? ORDER BY id DESC LIMIT ?""",
                          (int(vmid), int(limit))).fetchall()
    return [dict(r) for r in rows]


# ─── Reading the output on the node ─────────────────────────────────────────────────────────
_DOCKER_ERR = re.compile(r"^(Error response from daemon|Error: No such container|Error:)", re.I)


def fetch_lines(cont, tail=MAX_LINES):
    """Last output lines of the container through its node agent. Returns (lines, None) or
    (None, reason). Unlike `node_driver.container_logs`, a failure is NOT an empty list."""
    try:
        from .database import db_get_node
        from . import node_driver
        if not cont:
            return None, "container unknown to the database"
        name = cont.get("docker_name")
        if not cont.get("node_id") or not name:
            return None, "container has no node or docker name"
        node = db_get_node(cont["node_id"])
        if not node:
            return None, "node %s unknown" % cont.get("node_id")
        ok, data = node_driver._call(node, "GET", "/v1/containers/%s/logs?tail=%d" % (name, int(tail)),
                                     timeout=FETCH_TIMEOUT_S)
        if not ok:
            return None, "node agent: %s" % (str(data)[:200] or "no answer")
        lines = (data or {}).get("lines") if isinstance(data, dict) else None
        if lines is None:
            return None, "node agent answered without lines"
        # The node agent returns `docker logs` stderr in place of stdout when stdout is empty:
        # a daemon error must not be stored as if it were the script output.
        if len(lines) <= 2 and lines and _DOCKER_ERR.match(str(lines[0]).strip()):
            return None, "docker: %s" % str(lines[0]).strip()[:200]
        return list(lines), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


# ─── Capture ────────────────────────────────────────────────────────────────────────────────
def is_new_death(status, last_seen_ts, now=None):
    """True if the agent `/status` reports a spontaneous death not seen yet. `last_exit_ts` is set
    by the agent ONLY when the script ended without a `/stop` (cf. module docstring).

    A death older than MAX_AGE_S is NOT new: after an orchestrator restart the in-memory
    « last seen » is empty, and the agent may still report a death from hours ago followed by a
    /stop — reading `docker logs` now would store output that has nothing to do with it."""
    ts = (status or {}).get("last_exit_ts")
    if ts is None or ts == last_seen_ts:
        return False
    try:
        return (float(now if now is not None else time.time()) - float(ts)) <= MAX_AGE_S
    except (TypeError, ValueError):
        return False


def capture(vmid, cont=None, status=None, source="surveillance", fetch=None):
    """Store the last output of a script that died on its own. Returns the row (dict, WITHOUT
    `tail` when it already existed) — `status` is `ok` or `unavailable` (+`error`). Deduplicated
    on (vmid, last_exit_ts): a second call for the same death returns the first row with
    `existing=True`. Never raises (returns None on an internal error, logged)."""
    try:
        from .database import db_get_container
        status = status or {}
        if cont is None:
            cont = db_get_container(vmid)
        exit_ts = status.get("last_exit_ts")
        with _lock:
            with get_db() as db:
                ensure_table(db)
                if exit_ts is not None:
                    r = db.execute("""SELECT id, status, error, tail FROM script_crash_tails
                                      WHERE vmid=? AND exit_ts=? ORDER BY id DESC LIMIT 1""",
                                   (int(vmid), float(exit_ts))).fetchone()
                    if r is not None:
                        d = dict(r)
                        d["existing"] = True
                        return d
            lines, err = (fetch or fetch_lines)(cont)
            text, truncated = (bound([redact(l) for l in lines]) if lines is not None
                               else ("", False))
            row = {
                "vmid": int(vmid),
                "instance_uuid": (cont or {}).get("instance_uuid"),
                "hostname": (cont or {}).get("hostname"),
                "node_id": (cont or {}).get("node_id"),
                "docker_name": (cont or {}).get("docker_name"),
                "exit_code": status.get("last_exit"),
                "exit_signal": status.get("last_signal"),
                "exit_ts": float(exit_ts) if exit_ts is not None else None,
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "source": source,
                "status": "ok" if lines is not None else "unavailable",
                "error": err,
                "truncated": 1 if truncated else 0,
                "tail": text if lines is not None else None,
            }
            with get_db() as db:
                cur = db.execute(
                    """INSERT INTO script_crash_tails (vmid, instance_uuid, hostname, node_id,
                         docker_name, exit_code, exit_signal, exit_ts, captured_at, source, status,
                         error, truncated, tail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(row[k] for k in ("vmid", "instance_uuid", "hostname", "node_id",
                                           "docker_name", "exit_code", "exit_signal", "exit_ts",
                                           "captured_at", "source", "status", "error",
                                           "truncated", "tail")))
                row["id"] = cur.lastrowid
                _prune(db, int(vmid))
            if lines is None:
                log.warning("crash tail %s: script output NOT captured (%s)", vmid, err)
            else:
                log.info("crash tail %s: %d bytes captured (id %s, exit %s, signal %s)", vmid,
                         len(text.encode("utf-8")), row["id"], row["exit_code"], row["exit_signal"])
            row["existing"] = False
            return row
    except Exception as e:
        log.warning("crash tail %s: capture failed: %s", vmid, e)
        return None


def last_line(row):
    """Last non-empty line of a captured tail (cause summary), or None."""
    for l in reversed(((row or {}).get("tail") or "").splitlines()):
        if l.strip():
            return l.strip()[:200]
    return None


def alert_unavailable(row, vmid, node_id=None):
    """Raise the alert that says the output could not be kept (no silent failure)."""
    try:
        from .database import db_add_alert
        db_add_alert("alert.agent.crash_tail_unavailable", "warning", vmid=vmid, node_id=node_id,
                     kind="agent", params={"vmid": vmid,
                                           "e": (row or {}).get("error") or "?"})
    except Exception as e:
        log.warning("crash tail %s: alert failed: %s", vmid, e)


def public(row):
    """Row as served to the interface (JSON-safe)."""
    if not row:
        return None
    d = dict(row)
    d.pop("existing", None)
    return json.loads(json.dumps(d, default=str))
