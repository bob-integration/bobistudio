# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""A container's CPU resources as ONE place reads and changes them (2026-11-07).

Three windows show a container's cores: the container settings panel (plugin page and Containers
page), the node's « Gérer les cœurs » window, and the project Resources view. Until then each
derived its own reading — the Containers palette still offered a « CPU mode » the server had
stopped honouring, and the cores window showed « place » and « level » as two settings when the
level alone decides the place. This module is what the first two share; the project view keeps
its own plan (moves between nodes) but calls the same `set_dedicated_cores` for a resize.

  · `resources_view(vmid)`: the level, what it gives (dedicated cores or a CPU cap on the shared
    pool), the memory, the cores actually applied and whether they match the reservation, the room
    left on the node, the projects — everything the panel shows, in one read.
  · `set_dedicated_cores(vmid, n)`: resize a DEDICATED container's reservation, hot when it runs
    (`etalonnage.garantir`, `docker update --cpuset-cpus`), checked BEFORE anything is released —
    `garantir` frees the old reservation first, so a refusal found after it would leave the
    container with nothing.
"""
import logging

log = logging.getLogger(__name__)


def resources_view(vmid):
    """→ dict for the panel, or None when the container does not exist."""
    from . import core_pool, cpu_weight, docker_compute
    from .database import db_get_container, db_get_node
    c = db_get_container(vmid)
    if not c:
        return None
    t = docker_compute._type_of(c)
    node = db_get_node(c.get("node_id")) if c.get("node_id") else None
    out = {"vmid": vmid, "hostname": c.get("hostname") or "", "status": c.get("status") or "",
           "type": t, "node": ({"id": node["id"], "name": node.get("name") or ""} if node else None),
           "engine": docker_compute.is_mtl_type(t),
           "levels": [{"id": l, "value": cpu_weight.WEIGHTS[l]} for l in cpu_weight.LEVELS]}
    out["realtime"] = realtime_view(vmid, t)
    out["deadline"] = deadline_view(c)
    if out["engine"]:
        # The 2110 engine's cpuset comes from its DPDK lcores: no level, no cores to set here.
        return out
    res = docker_compute.effective_resources(t, c) or {}
    w = cpu_weight.for_container(c, t)
    dedicated = bool(res.get("pin"))
    out.update({
        "level": w["niveau"], "level_source": w["source"],
        "node_io": bool(res.get("node_io")),
        "dedicated": dedicated,
        # Dedicated: how many cores are reserved for it. Shared: `cores` is a CPU cap (`--cpus`),
        # set or not — the same column, two meanings the panel must name apart.
        "cores": (int(res.get("cores") or 0) or None) if dedicated else None,
        "cap": (int(res["cores"]) if (not dedicated and res.get("cores")) else None),
        "memory": int(res.get("memory") or 0) or None,
        # What is SET for this reservation, sibling included when the type asks for it: the panel
        # compares it to what is applied, so both sides must be the same notion (`cpuset_pose`).
        "reserved": (core_pool.cpuset_pose(
            node["id"], vmid, core_pool.allocations_by_vmid(node["id"]).get(vmid) or [], res=res)
            if node else ""),
        "assigned": c.get("pinned_cores") or "",
        # Dedicated without room at deployment: refused (False), or run on the shared pool by the
        # user's choice (True) — cf. docker_compute.deploy_compute.
        "dedicated_fallback": bool(c.get("dedicated_fallback_ok")),
        # Peak of memory.current over 24 h (app/memory_watch.py): what the limit is judged on.
        "memory_peak": memory_peak_view(c),
    })
    if node:
        out["free_dedicated"] = core_pool.free_dedicated_capacity(
            node["id"], vmid=vmid, physical=out["node_io"])
        line = _cores_line(node["id"], vmid)
        if line:
            out["applied"] = line.get("cpuset_applique") or ""
            # None = not measured (node unreachable): never shown as « applied ».
            out["coherent"] = line.get("coherent")
            out["protection"] = line.get("protection")
    return out


def realtime_view(vmid, type_):
    """Real-time priority of the container (app/realtime.py), or None when it neither has a role nor
    publishes one: {role, asked, applied, error}. `applied` is what the SCRIPT published on :8080
    (None = not published yet, or a version that does not publish it); the ask is never shown as
    applied."""
    from . import metrics, plugins, realtime
    man = plugins.get(type_) or {}
    role = realtime.role_of(man)
    st = metrics.rt_state_cache.get(vmid)
    if not role and not (st and st.get("priority")):
        return None
    return {"role": role or None, "asked": realtime.priority_of(man) or None,
            "applied": st.get("priority") if st else None, "error": (st or {}).get("error") or ""}


def memory_peak_view(c):
    """{peak_mb, limit_mb, pct, hours} over 24 h, or None (never measured)."""
    from . import memory_watch
    p = memory_watch.peak_24h(c.get("instance_uuid") or ("vmid:%s" % c.get("vmid")))
    if not p:
        return None
    return {"peak_mb": round(p["peak"] / 2**20), "limit_mb": round(p["limit"] / 2**20) if p["limit"] else None,
            "pct": p["pct"], "hours": p["hours"]}


def deadline_view(c):
    """Deadline witness of the container (app/deadlines.py), or None when its script publishes
    none: {missed_24h, hours, missed, window_s, late_p999_ms, late_max_ms}."""
    from . import deadlines
    blk = deadlines.state(c.get("vmid"))
    if blk is None:
        return None
    m24, hours = deadlines.missed_24h(c.get("instance_uuid") or ("vmid:%s" % c.get("vmid")))
    return {"missed_24h": m24, "hours": hours, "missed": blk.get("missed"),
            "window_s": blk.get("window_s"), "late_p999_ms": blk.get("late_p999_ms"),
            "late_max_ms": blk.get("late_max_ms")}


def _cores_line(node_id, vmid):
    from . import core_pool
    try:
        v = core_pool.vue_coeurs(node_id)
    except Exception as e:                                                   # noqa: BLE001
        log.warning("container settings: cores view of node %s unreadable (%s)", node_id, e)
        return None
    for l in (v.get("dedies") or []) + (v.get("partages") or []):
        if l.get("vmid") == vmid:
            return l
    return None


def set_dedicated_cores(vmid, n):
    """Resize a dedicated container's reservation → (ok, payload). Refusals are dicts the windows
    translate: `not_dedicated`, `node_io` (sized by its active connectors), `engine`, `cores`,
    `no_room` {name, need, available}. A stopped container gets the setting only: its next start
    builds the reservation."""
    from . import core_pool, docker_compute, etalonnage
    from .database import db_get_container, db_get_node, db_set_resources_override
    c = db_get_container(vmid)
    if not c:
        return False, {"error": "not_found"}
    t = docker_compute._type_of(c)
    if docker_compute.is_mtl_type(t):
        return False, {"error": "engine"}
    res = docker_compute.effective_resources(t, c) or {}
    if res.get("node_io"):
        return False, {"error": "node_io"}
    if not res.get("pin"):
        return False, {"error": "not_dedicated"}
    try:
        n = int(n)
    except (TypeError, ValueError):
        return False, {"error": "cores"}
    if n < 1 or n > 256:
        return False, {"error": "cores"}
    node = db_get_node(c.get("node_id")) or {}
    if c.get("status") != "running":
        db_set_resources_override(vmid, cores=n)
        return True, {"cores": n, "hot": False}
    room = core_pool.free_dedicated_capacity(node.get("id"), vmid=vmid)
    if n > room:
        return False, {"error": "no_room", "name": node.get("name") or "", "need": n, "available": room}
    ok, r = etalonnage.garantir(vmid, coeurs=n)
    if not ok:
        return False, {"error": "failed", "detail": r}
    return True, {"cores": n, "hot": True, "cpuset": r.get("cpuset")}
