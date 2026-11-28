# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""MEMORY OF THE CONTAINERS, measured (P3 of the resource guarantee, 2026-11-23).

A memory limit no one measured is a guess that constrains: on 2026-10-30 decklink_io at 1011 of
1024 MiB had its capture threads blocked by direct reclaim (21 fps instead of 50), with no OOM, no
restart and no counter moving. None of the twelve manifests that set a limit carries a measurement.
Decided with Cyril on 2026-11-23: measure continuously, keep every limit, and say it.

This module:
  - keeps the PEAK of each container instance per hour (`memory_hours`, keyed on instance_uuid: a
    vmid is recycled), from the agent's `/stats` read by the metrics poll (`memory.current`, kernel
    memory and the mapped MXL bus included) — written only when the peak grows, or a new hour;
  - answers the 24 h peak of a container (`peak_24h`, the container panel) and the fleet peak of
    each plugin against its manifest limit (`by_type`, the cores settings): that is what corrects a
    manifest with figures;
  - admits a memory limit on a node (`admission`): the sum of the limits must fit in the node's RAM
    (share `NODE_SHARE`, the rest for the host and the MXL bus). An unknown RAM abstains.

The pressure alert itself (70 % / 95 % of the limit) stays in `metrics._verifier_pression_memoire`.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from .database import get_db

log = logging.getLogger(__name__)

WINDOW_H = 24
KEEP_DAYS = 8
NODE_SHARE = 0.9          # of the node's RAM, for the containers' limits together
GROW_MIN = 1.01           # a peak is rewritten when it grew by more than 1 %

_lock = threading.Lock()
_peaks = {}               # (instance_uuid, hour) → peak bytes already written
_purged = [0.0]


def ensure_table(db=None):
    db = db or get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS memory_hours (
        instance_uuid TEXT NOT NULL,
        hour          TEXT NOT NULL,      -- local time, YYYY-MM-DDTHH
        vmid          INTEGER,
        type          TEXT,
        peak          INTEGER NOT NULL,   -- bytes, memory.current
        mem_limit     INTEGER,            -- bytes, memory.max at that peak (NULL = none)
        PRIMARY KEY (instance_uuid, hour))""")
    db.commit()


def _hour(ts=None):
    return datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%dT%H")


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def note(vmid, cont, s, type_=None):
    """Called by the metrics poll with the agent's `/stats` payload."""
    used, lim = _int((s or {}).get("mem_used")), _int((s or {}).get("mem_limit"))
    if used <= 0:
        return
    uuid = (cont or {}).get("instance_uuid") or ("vmid:%s" % vmid)
    key = (uuid, _hour())
    with _lock:
        prev = _peaks.get(key)
        if prev is not None and used <= prev * GROW_MIN:
            return
        if prev is None and len(_peaks) > 4096:
            _peaks.clear()
        _peaks[key] = used
    db = get_db()
    try:
        ensure_table(db)
        db.execute("INSERT INTO memory_hours (instance_uuid, hour, vmid, type, peak, mem_limit) "
                   "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(instance_uuid, hour) DO UPDATE SET "
                   "vmid = excluded.vmid, type = coalesce(excluded.type, type), "
                   "mem_limit = CASE WHEN excluded.peak >= peak THEN excluded.mem_limit ELSE mem_limit END, "
                   "peak = max(peak, excluded.peak)",
                   (uuid, key[1], vmid, type_, used, lim or None))
        db.commit()
    except Exception as e:                                     # noqa: BLE001 (never break the poll)
        log.warning("memory_watch: cannot record vmid=%s: %s", vmid, e)
    if time.time() - _purged[0] > 3600:
        _purged[0] = time.time()
        purge()


def peak_24h(uuid, now=None):
    """{peak, limit, pct, hours} over the last 24 h for an instance, or None (never measured)."""
    since = _hour((now or time.time()) - WINDOW_H * 3600)
    db = get_db()
    try:
        ensure_table(db)
        r = db.execute("SELECT peak, mem_limit, (SELECT count(*) FROM memory_hours WHERE "
                       "instance_uuid = ? AND hour > ?) FROM memory_hours WHERE instance_uuid = ? "
                       "AND hour > ? ORDER BY peak DESC LIMIT 1", (uuid, since, uuid, since)).fetchone()
    except Exception as e:                                     # noqa: BLE001
        log.warning("memory_watch: cannot read %s: %s", uuid, e)
        return None
    if not r:
        return None
    peak, lim = int(r[0]), (int(r[1]) if r[1] else None)
    return {"peak": peak, "limit": lim, "pct": round(100.0 * peak / lim, 1) if lim else None,
            "hours": int(r[2])}


def by_type(days=7, now=None):
    """Fleet peak of each plugin over `days` against its manifest limit → list of
    {type, manifest_mb, peak_mb, pct, instances}, worst first. `pct` is None without a limit."""
    from . import plugins
    since = _hour((now or time.time()) - days * 86400)
    db = get_db()
    try:
        ensure_table(db)
        rows = db.execute("SELECT type, max(peak), count(DISTINCT instance_uuid) FROM memory_hours "
                          "WHERE hour > ? AND type IS NOT NULL GROUP BY type", (since,)).fetchall()
    except Exception as e:                                     # noqa: BLE001
        log.warning("memory_watch: cannot read the fleet: %s", e)
        return []
    out = []
    for t, peak, n in rows:
        man = _int(((plugins.get(t) or {}).get("resources") or {}).get("memory")) or None
        peak_mb = round(int(peak) / 2**20)
        out.append({"type": t, "manifest_mb": man, "peak_mb": peak_mb, "instances": int(n),
                    "pct": round(100.0 * peak_mb / man, 1) if man else None})
    out.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0), x["type"]))
    return out


def node_ram_mb(node_id):
    """The node's RAM as last sampled by node_health, or None (not sampled yet: abstain)."""
    try:
        from . import node_health
        snap = (node_health.latest().get("nodes") or {}).get(str(node_id)) or {}
        return _int((snap.get("resources") or {}).get("mem_total_mb")) or None
    except Exception:                                          # noqa: BLE001
        return None


def limits_on_node(node_id, exclude_vmid=None):
    """(sum of the memory limits in MiB, [{vmid, hostname, memory}]) of the node's containers."""
    from . import docker_compute
    from .database import db_get_containers
    total, holders = 0, []
    for c in db_get_containers() or []:
        if c.get("node_id") != node_id or c.get("vmid") == exclude_vmid:
            continue
        try:
            mem = _int((docker_compute.effective_resources(docker_compute._type_of(c), c) or {})
                       .get("memory"))
        except Exception:                                      # noqa: BLE001
            mem = 0
        if mem > 0:
            total += mem
            holders.append({"vmid": c.get("vmid"), "hostname": c.get("hostname") or "", "memory": mem})
    holders.sort(key=lambda h: -h["memory"])
    return total, holders


def admission(node_id, demande_mb, exclude_vmid=None):
    """Would a limit of `demande_mb` more fit in this node's RAM? → {suffisant, demande, somme,
    budget, ram, conteneurs, noeud}. No demand, or the RAM not known yet: admitted."""
    from .database import db_get_node
    out = {"suffisant": True, "demande": _int(demande_mb), "somme": None, "budget": None,
           "ram": None, "conteneurs": [], "noeud": str(node_id)}
    if out["demande"] <= 0:
        return out
    ram = node_ram_mb(node_id)
    if not ram:
        return out
    node = db_get_node(node_id) or {}
    out["noeud"] = node.get("name") or str(node_id)
    somme, holders = limits_on_node(node_id, exclude_vmid=exclude_vmid)
    out.update({"ram": ram, "somme": somme, "budget": int(ram * NODE_SHARE), "conteneurs": holders[:8]})
    out["suffisant"] = somme + out["demande"] <= out["budget"]
    return out


def purge(days=KEEP_DAYS):
    db = get_db()
    try:
        ensure_table(db)
        db.execute("DELETE FROM memory_hours WHERE hour < ?",
                   ((datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H"),))
        db.commit()
    except Exception as e:                                     # noqa: BLE001
        log.warning("memory_watch: purge failed: %s", e)
