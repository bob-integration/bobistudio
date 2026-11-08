# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Automatic project versions: when they are taken, and which ones are kept (2026-11-07).

A project re-saves itself a few seconds after one of its containers changed (projects.py,
notify_container_changed) and the previous content goes to `project_versions`. NAMED versions
(a label) are never pruned here. Automatic ones follow one policy, shared by all projects:

    {"delay_s": 10,          seconds between the last change and the automatic version
     "max": 30,              at most this many automatic versions per project (0 = no cap)
     "tiers": [...]}         age brackets, youngest first; none = keep everything (up to `max`)

A tier is {"upto_h": hours or None, "keep": mode, "n": count}: it covers the versions younger than
`upto_h` (None = everything older than the previous tier). Modes: "all", "count" (the n most
recent of the bracket), "hourly" / "daily" / "weekly" / "monthly" (the most recent of each hour,
day, ISO week, month — local time), "none". With tiers, a version older than the last bounded
tier is dropped: the editor says so, and an open-ended last tier keeps them.

The default reproduces what existed before (no tier, 30 kept): editing the policy is the only way
versions start disappearing, and the editor shows how many before saving."""
import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

SETTING_KEY = "project_versions_policy"
MODES = ("all", "count", "hourly", "daily", "weekly", "monthly", "none")
DEFAULT_POLICY = {"delay_s": 10, "max": 30, "tiers": []}
DELAY_MIN_S, DELAY_MAX_S = 2, 3600
MAX_CAP = 10000


def validate(policy):
    """Normalized copy of `policy`, or raise ValueError with a short reason."""
    if not isinstance(policy, dict):
        raise ValueError("policy: object expected")
    try:
        delay = int(policy.get("delay_s", DEFAULT_POLICY["delay_s"]))
        cap = int(policy.get("max", DEFAULT_POLICY["max"]))
    except (TypeError, ValueError):
        raise ValueError("delay_s / max: integers expected")
    if not DELAY_MIN_S <= delay <= DELAY_MAX_S:
        raise ValueError("delay_s: %d to %d seconds" % (DELAY_MIN_S, DELAY_MAX_S))
    if not 0 <= cap <= MAX_CAP:
        raise ValueError("max: 0 to %d" % MAX_CAP)
    tiers, last = [], 0.0
    raw = policy.get("tiers") or []
    if not isinstance(raw, list):
        raise ValueError("tiers: list expected")
    for i, t in enumerate(raw):
        if not isinstance(t, dict) or t.get("keep") not in MODES:
            raise ValueError("tier %d: keep must be one of %s" % (i + 1, ", ".join(MODES)))
        upto = t.get("upto_h")
        if upto is None:
            if i != len(raw) - 1:
                raise ValueError("tier %d: only the last tier can be open-ended" % (i + 1))
        else:
            try:
                upto = float(upto)
            except (TypeError, ValueError):
                raise ValueError("tier %d: upto_h must be a number of hours" % (i + 1))
            if upto <= last:
                raise ValueError("tier %d: brackets must grow (%g h after %g h)" % (i + 1, upto, last))
            last = upto
        out = {"upto_h": upto, "keep": t["keep"]}
        if t["keep"] == "count":
            try:
                n = int(t.get("n"))
            except (TypeError, ValueError):
                raise ValueError("tier %d: n must be an integer" % (i + 1))
            if not 1 <= n <= MAX_CAP:
                raise ValueError("tier %d: n from 1 to %d" % (i + 1, MAX_CAP))
            out["n"] = n
        tiers.append(out)
    return {"delay_s": delay, "max": cap, "tiers": tiers}


def current():
    """The stored policy (validated), the default when none or unreadable."""
    from . import settings
    raw = settings.get(SETTING_KEY)
    if not raw:
        return dict(DEFAULT_POLICY)
    try:
        return validate(json.loads(raw) if isinstance(raw, str) else raw)
    except (ValueError, TypeError) as e:
        log.warning("project versions policy unreadable (%s): default used", e)
        return dict(DEFAULT_POLICY)


def save(policy):
    from . import settings
    policy = validate(policy)
    settings.set(SETTING_KEY, json.dumps(policy))
    return policy


def delay_s():
    return current()["delay_s"]


def _bucket(ts, mode):
    if mode == "hourly":
        return ts.strftime("%Y-%m-%d %H")
    if mode == "daily":
        return ts.strftime("%Y-%m-%d")
    if mode == "weekly":
        y, w, _ = ts.isocalendar()
        return "%d-W%02d" % (y, w)
    return ts.strftime("%Y-%m")


def kept_ids(versions, policy, now=None):
    """Ids of the AUTOMATIC versions the policy keeps. `versions`: [{id, created_at}] (naive local
    ISO, as written). Unreadable dates are kept: a policy never deletes what it cannot place."""
    now = now or datetime.now()
    dated, keep = [], set()
    for v in versions:
        try:
            dated.append((datetime.fromisoformat(v["created_at"]), v["id"]))
        except (TypeError, ValueError, KeyError):
            keep.add(v.get("id"))
    dated.sort(key=lambda x: (x[0], x[1]), reverse=True)          # newest first
    tiers = policy.get("tiers") or []
    if not tiers:
        chosen = [vid for _, vid in dated]
    else:
        chosen, counts, buckets = [], {}, set()
        for ts, vid in dated:
            age_h = (now - ts).total_seconds() / 3600.0
            idx = next((i for i, t in enumerate(tiers) if t["upto_h"] is None or age_h <= t["upto_h"]), None)
            if idx is None:
                continue                                          # older than every bracket
            mode = tiers[idx]["keep"]
            if mode == "all":
                chosen.append(vid)
            elif mode == "count":
                if counts.get(idx, 0) < tiers[idx]["n"]:
                    counts[idx] = counts.get(idx, 0) + 1
                    chosen.append(vid)
            elif mode != "none":
                key = (idx, _bucket(ts, mode))
                if key not in buckets:
                    buckets.add(key)
                    chosen.append(vid)
    cap = policy.get("max") or 0
    if cap:
        chosen = chosen[:cap]                                     # still newest first
    return keep | set(chosen)


def _auto_versions(db, pid):
    return [dict(r) for r in db.execute(
        "SELECT id, created_at FROM project_versions WHERE project_id=? AND label IS NULL", (pid,))]


def prune(db, pid, policy=None):
    """Delete the automatic versions of `pid` the policy does not keep (caller commits). Returns n."""
    policy = policy or current()
    auto = _auto_versions(db, pid)
    drop = [v["id"] for v in auto if v["id"] not in kept_ids(auto, policy)]
    for vid in drop:
        db.execute("DELETE FROM project_versions WHERE id=?", (vid,))
    return len(drop)


def preview(policy):
    """What `policy` would delete, per project, without deleting anything."""
    from .database import get_db
    policy = validate(policy)
    out = []
    with get_db() as db:
        for p in db.execute("SELECT id, name FROM projects ORDER BY name").fetchall():
            auto = _auto_versions(db, p["id"])
            kept = kept_ids(auto, policy)
            n = sum(1 for v in auto if v["id"] not in kept)
            out.append({"pid": p["id"], "name": p["name"], "auto": len(auto), "delete": n})
    return {"delete": sum(x["delete"] for x in out), "projects": out}


def apply_all(policy=None):
    """Prune every project with `policy` (the stored one by default). Returns the number deleted."""
    from .database import get_db
    policy = validate(policy) if policy else current()
    n = 0
    with get_db() as db:
        for (pid,) in db.execute("SELECT id FROM projects").fetchall():
            n += prune(db, pid, policy)
        db.commit()
    if n:
        log.info("project versions: %d automatic version(s) pruned by the retention policy", n)
    return n
