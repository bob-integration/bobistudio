# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""DEADLINE WITNESS read by the controller (P1 of the resource guarantee, 2026-11-23).

Every plugin with a cadence publishes the same block on :8080, `deadline`, built by
`bobimxl.DeadlineWitness`: period, deadlines missed over a fixed window, missed since start, late
p99.9. A deadline is missed when the output of a cycle is ready after it, or when a slot is skipped.

This module:
  - keeps the last block per container (`state`), for the panel;
  - adds what was missed since the last poll to an hourly row per container INSTANCE
    (`deadline_hours`, keyed on instance_uuid: a vmid is recycled), so the 24 h survive an
    orchestrator restart;
  - alerts once when deadlines start being missed, once when a full window is clean again;
  - answers the guarantee verdict (`verdict_of`): decided with Cyril on 2026-11-23, a Dedicated
    container is only guaranteed if it has its cores AND publishes a witness AND missed nothing
    in 24 h. No witness = no proof = not guaranteed.

STARTUP (2026-11-24, decided with Cyril: « c'est normal qu'il faille un temps de démarrage »): a
redeploy or a restart marks the container as starting (`mark_starting`). While it starts, its own
misses AND those of the walls that read it (fabric shards) are not counted nor alerted — a shard
restart froze its zone ~6 s and cost the wall 2 deadlines. The mark ends when the container
publishes a witness born after it and out of its own startup (bobimxl), or after STARTUP_GRACE_S.
A crash has its own alert; the restart that follows (surveillance) is a startup like any other.
A script relaunched by its agent alone is not marked: its own witness still skips its startup.

What it does not see: misses while the orchestrator was down are counted only through the
script's `missed_total` (the delta from the last value read). The first poll after a restart takes
the script's total as a baseline, not as new misses — re-counting the whole run would alert on
history.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from .database import db_add_alert, get_db
from .episodes import EtatEpisodes

log = logging.getLogger(__name__)

WINDOW_H = 24
STARTUP_GRACE_S = 60.0

_lock = threading.Lock()
_last = {}                 # vmid → {"block": dict, "uuid": str, "total": int, "t": float}
_alerted = EtatEpisodes("deadline")     # instance_uuid → 1 while deadlines are being missed
_purged = [0.0]
_starting = {}             # vmid → time.time() of the redeploy/restart (see STARTUP above)


def mark_starting(vmid):
    """A redeploy or a restart begins: its startup is not a missed deadline."""
    try:
        with _lock:
            _starting[int(vmid)] = time.time()
    except (TypeError, ValueError):
        pass


def is_starting(vmid, now=None):
    now = now or time.time()
    with _lock:
        t = _starting.get(vmid)
        if t is not None and now - t > STARTUP_GRACE_S:
            _starting.pop(vmid, None)
            t = None
    return t is not None


def _end_startup(vmid, blk, now):
    """The container answers with a witness born after the mark and done starting."""
    with _lock:
        t = _starting.get(vmid)
    if t is None or blk.get("starting") is not False:
        return
    age = blk.get("age_s")
    if isinstance(age, (int, float)) and age < now - t:
        with _lock:
            _starting.pop(vmid, None)


def _excused(vmid):
    """Misses of `vmid` right now are part of a startup: its own, or one of its shards'."""
    with _lock:
        if not _starting:
            return False
    if is_starting(vmid):
        return True
    try:
        from .compositor_fabric import shards_par_parent
        return any(is_starting(s) for s in shards_par_parent().get(vmid) or ())
    except Exception as e:                                     # noqa: BLE001
        log.warning("deadlines: cannot read the shards of %s: %s", vmid, e)
        return False


def ensure_table(db=None):
    db = db or get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS deadline_hours (
        instance_uuid TEXT NOT NULL,
        hour          TEXT NOT NULL,      -- local time, YYYY-MM-DDTHH
        vmid          INTEGER,
        missed        INTEGER NOT NULL DEFAULT 0,
        late_max_ms   REAL,
        PRIMARY KEY (instance_uuid, hour))""")
    db.commit()


def _hour(ts=None):
    return datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%dT%H")


def _record(uuid, vmid, missed, late_max, ts=None):
    """One poll: the hour is marked as WATCHED (row exists), misses are added."""
    db = get_db()
    try:
        ensure_table(db)
        db.execute("INSERT INTO deadline_hours (instance_uuid, hour, vmid, missed, late_max_ms) "
                   "VALUES (?, ?, ?, ?, ?) ON CONFLICT(instance_uuid, hour) DO UPDATE SET "
                   "missed = missed + excluded.missed, vmid = excluded.vmid, "
                   "late_max_ms = max(coalesce(late_max_ms, excluded.late_max_ms), "
                   "coalesce(excluded.late_max_ms, late_max_ms))",
                   (uuid, _hour(ts), vmid, int(missed), late_max))
        db.commit()
    except Exception as e:                                     # noqa: BLE001 (never break the poll)
        log.warning("deadlines: cannot record vmid=%s: %s", vmid, e)


def note(vmid, cont, m):
    """Called by the metrics poll with the :8080 payload. Silent :8080 → state kept; a script that
    publishes no `deadline` block → forgotten (and not watched)."""
    if not isinstance(m, dict) or (m.get("fps") is None and len(m) == 1):
        return
    blk = m.get("deadline")
    if not isinstance(blk, dict):
        with _lock:
            _last.pop(vmid, None)
        return
    uuid = (cont or {}).get("instance_uuid") or ("vmid:%s" % vmid)
    try:
        total = int(blk.get("missed_total") or 0)
        window = int(blk.get("missed") or 0)
    except (TypeError, ValueError):
        return
    late_max = blk.get("late_max_ms")
    late_max = float(late_max) if isinstance(late_max, (int, float)) else None
    with _lock:
        prev = _last.get(vmid)
        if prev is None or prev["uuid"] != uuid:
            new = 0                       # baseline (see the module docstring)
        elif total >= prev["total"]:
            new = total - prev["total"]
        else:
            new = total                   # the script restarted: its counter started again
        first_this_hour = prev is None or _hour(prev["t"]) != _hour()
        _last[vmid] = {"block": dict(blk), "uuid": uuid, "total": total, "t": time.time()}
    _end_startup(vmid, blk, time.time())
    if new and _excused(vmid):
        log.info("deadlines: vmid=%s %d missed during a startup, not counted", vmid, new)
        new, late_max = 0, None
    if new or first_this_hour:
        _record(uuid, vmid, new, late_max)
    if time.time() - _purged[0] > 3600:
        _purged[0] = time.time()
        purge()
    _transition(vmid, cont, uuid, new, window, blk)


def _num(v):
    return float(v) if isinstance(v, (int, float)) else 0.0


def _transition(vmid, cont, uuid, new, window, blk):
    host = (cont or {}).get("hostname") or str(vmid)
    if new and not _alerted.get(uuid):
        _alerted.poser(uuid, 1)
        db_add_alert("alert.deadline.missed", "warning", vmid=vmid,
                     node_id=(cont or {}).get("node_id"), kind="fps",
                     params={"h": host, "vmid": vmid, "n": new, "late": _num(blk.get("late_max_ms")),
                             "period": _num(blk.get("period_ms"))})
    elif not window and _alerted.get(uuid):
        _alerted.retirer(uuid)
        db_add_alert("alert.deadline.recovered", "info", vmid=vmid,
                     node_id=(cont or {}).get("node_id"), kind="fps",
                     params={"h": host, "vmid": vmid, "window": int(_num(blk.get("window_s")))})
    _alerted.flush()


def state(vmid):
    """Last block published by this container, or None (no witness, or never polled)."""
    with _lock:
        p = _last.get(vmid)
        return dict(p["block"]) if p else None


def missed_24h(uuid, now=None):
    """(missed, hours watched) over the last 24 h for an instance."""
    since = _hour((now or time.time()) - WINDOW_H * 3600)
    db = get_db()
    try:
        ensure_table(db)
        r = db.execute("SELECT coalesce(sum(missed), 0), count(*) FROM deadline_hours "
                       "WHERE instance_uuid = ? AND hour > ?", (uuid, since)).fetchone()
        return int(r[0]), int(r[1])
    except Exception as e:                                     # noqa: BLE001
        log.warning("deadlines: cannot read %s: %s", uuid, e)
        return 0, 0


def verdict_of(cont):
    """`deadline_unwatched` (no witness published now), `deadline_missed` (missed in 24 h), or
    None when the deadlines say nothing against the guarantee."""
    vmid = (cont or {}).get("vmid")
    if state(vmid) is None:
        return "deadline_unwatched"
    uuid = cont.get("instance_uuid") or ("vmid:%s" % vmid)
    missed, _hours = missed_24h(uuid)
    return "deadline_missed" if missed else None


def purge(days=8):
    """Drop rows older than `days` (the verdict reads 24 h; a week is kept for the panel)."""
    db = get_db()
    try:
        ensure_table(db)
        db.execute("DELETE FROM deadline_hours WHERE hour < ?",
                   ((datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H"),))
        db.commit()
    except Exception as e:                                     # noqa: BLE001
        log.warning("deadlines: purge failed: %s", e)
