# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""A project's CPU resources: is it guaranteed, where are its cores, and the plan to change that.

Decided with Cyril on 2026-11-07:
  · WHAT must be guaranteed is the CPU weight level « Dédié » (`dedicated`): a dedicated container must
    not miss its CPU; the others run best effort on the shared pool and do not count in the verdict
    (14 containers needing 50 cores for 0.3 consumed each would have made « guaranteed »
    unreachable, hence meaningless);
  · « Guaranteed » is strict: dedicated cores AND a calibration made on this node for this
    configuration, covering the measured peak with the margin `etalonnage.garantir` uses. A
    reservation nobody measured is not a guarantee, it is a guess that happens to be pinned;
  · the window draws every node whole, cell by cell, at its real size, and the proposals (a
    guarantee, a move) differently on the same schema — never behind a « now / after » toggle;
  · a container can be moved to another node. A move RECREATES it (a cut of a few seconds), so the
    interface asks for an explicit confirmation naming the containers concerned;
  · move suggestions only between containers of the SAME project that are wired together. When the
    target lacks room because OTHER containers hold its cores, the suggestion is still shown as
    « relevant, no room right now »; a node simply too small for it gets no suggestion at all.

Node I/O (decided on 2026-11-07, `resources.node_io` of the manifest — a DeckLink card): the card is
the node's input/output, like the 2110 engine, not a processing of the project. Its cores are sized
by its active connectors and held whole (HT siblings included, `core_pool.allocate_cores`
`physical`), so it is guaranteed by construction: it is shown, never counted in the verdict, never
calibrated, never resized nor moved from this window.

A cell is one logical CPU of the node's compute pool that the scheduler really uses
(`core_pool.ordonnancables`) — the unit of a reservation (`node_core_alloc` rows), so the schema
and the « cores » column count the same thing.
"""
import logging
import threading
import time

from .database import db_get_container, db_get_containers, db_get_node, db_get_nodes

log = logging.getLogger(__name__)

def is_node_io(c):
    """Is this container node I/O (`resources.node_io` of its effective profile)?"""
    from .docker_compute import effective_resources, _type_of
    try:
        return bool((effective_resources(_type_of(c), c) or {}).get("node_io"))
    except Exception as e:                                                 # noqa: BLE001
        log.debug("is_node_io(%s): %s", (c or {}).get("vmid"), e)
        return False


def move_block(r):
    """Why this member of `project_guarantee` cannot be moved to another node, or None.

    `node_io`: a DeckLink card is physically in its node; `type`: refused by the migration itself
    (`migration.INTERDITS`, the 2110 engine, a pyramid); `stopped`: nothing runs to recreate
    elsewhere. The window does not offer the gesture at all, and `check_plan` refuses it."""
    from .migration import INTERDITS
    if r.get("node_io"):
        return "node_io"
    if r.get("type") in INTERDITS:
        return "type"
    if r.get("guarantee") == "stopped":
        return "stopped"
    return None


# Per container, in order of severity.
GUARANTEE_STATES = ("stopped", "undersized", "reserved_unmeasured", "shared", "guaranteed")


# ── Is the project guaranteed? ──────────────────────────────────────────────────────────────────
def guarantee_state(row):
    """Guarantee state of one `etalonnage.ressources()` row (see GUARANTEE_STATES)."""
    if row.get("reserve_pct") is None:
        return "shared"
    if row.get("etat") != "etalonne" or row.get("besoin_pct") is None:
        return "reserved_unmeasured"
    if row["besoin_pct"] > row["reserve_pct"]:
        return "undersized"
    return "guaranteed"


def project_guarantee(pid):
    """Every container of the project with its guarantee state, and the project verdict.

    Membership = attached (project_id) OR referenced by the snapshot: the rule of the workspace
    summary and of `auth.vmid_project_ids`. Stopped containers are listed too — hiding them would
    make the verdict look better than reality.

    `verdict`: `guaranteed` when every dedicated member is; `not_guaranteed` otherwise, with `counts`
    per state of the dedicated members; `no_dedicated` when nothing is set dedicated (nothing is required —
    the screen says so rather than claiming a guarantee); `empty` without members.
    `nodes_without_pool` names the nodes of dedicated members where nothing can be reserved at all —
    the Z8 stayed so three days after its reinstallation, unnoticed."""
    from . import etalonnage
    from .docker_compute import _type_of
    from .database import db_get_project
    from .projects import PROJECT_EXCLUDED_TYPES
    snap = {sc.get("vmid") for sc in ((db_get_project(int(pid)) or {}).get("snapshot") or [])}
    members = [c for c in (db_get_containers() or [])
               if (c.get("project_id") == int(pid) or c.get("vmid") in snap)
               and _type_of(c) not in PROJECT_EXCLUDED_TYPES]
    rows = {r["vmid"]: r for r in etalonnage.ressources(vmids={c.get("vmid") for c in members})}
    out = []
    for c in members:
        r = rows.get(c.get("vmid"))
        if r is None:
            r = {"vmid": c.get("vmid"), "hostname": c.get("hostname"), "type": _type_of(c),
                 "node_id": c.get("node_id"), "etat": None, "reserve_pct": None,
                 "consomme_pct": None, "mesure": None, "noeud": None, "status": c.get("status"),
                 "project_id": c.get("project_id"),
                 "level": etalonnage._weight_level(_type_of(c), c), "guarantee": "stopped"}
        else:
            r = dict(r, guarantee=guarantee_state(r))
        r["node_io"] = is_node_io(c)
        r["required"] = (r.get("level") == "dedicated") and not r["node_io"]
        out.append(r)
    required = [r for r in out if r["required"]]
    counts = {s: 0 for s in GUARANTEE_STATES}
    for r in required:
        counts[r["guarantee"]] += 1
    without_pool = []
    for nid in sorted({r.get("node_id") for r in required if r.get("node_id")}):
        n = db_get_node(nid) or {}
        if not (n.get("compute_cpuset") or "").strip():
            without_pool.append({"id": nid, "name": n.get("name")})
    verdict = ("empty" if not out else "no_dedicated" if not required else
               "guaranteed" if counts["guaranteed"] == len(required) else "not_guaranteed")
    # On-air members first, then the rest, each group in name order.
    out.sort(key=lambda r: (not r["required"], r.get("hostname") or "", r.get("vmid") or 0))
    return {"verdict": verdict, "counts": counts, "best_effort": len(out) - len(required),
            "containers": out, "nodes_without_pool": without_pool}


# ── The schema ───────────────────────────────────────────────────────────────────────────────────
def node_map(node_id, members):
    """One node, cell by cell. `members` = vmids of the project.

    Cell kinds: `project` (dedicated to a member, with its vmid), `other` (dedicated to a container
    of another project, named in `owner`), `engine` (2110 engine lcores), `io` (held whole by node
    I/O — its cores AND their HT siblings, which nothing may use; `vmid` set when it is a member, for
    the cross highlight), `ht_blocked` (free, but the
    HT sibling of a dedicated core: usable as shared, refused to a new reservation by the strict HT
    rule), `free` (shared pool, can be dedicated). `dedicable` is what a new reservation can obtain
    today (`core_pool.free_dedicated_capacity`), so the client never promises more than that."""
    from . import core_pool
    from .docker_compute import _type_of
    node = db_get_node(node_id) or {}
    declared = (node.get("compute_cpuset") or "").strip()
    out = {"id": node_id, "name": node.get("name") or ("#%s" % node_id), "pool": bool(declared),
           "cells": [], "dedicable": 0}
    if not declared:
        return out
    pool = sorted(core_pool.ordonnancables(node_id, declared))
    owner = {c: v for v, cs in (core_pool.allocations_by_vmid(node_id) or {}).items() for c in cs}
    core_of = core_pool.core_map_cached(node_id) or {}
    taken_physical = {core_of[c] for c in owner if c in core_of}
    engines, io, names = set(), set(), {}
    for v in set(owner.values()):
        c = db_get_container(v) or {}
        names[v] = c.get("hostname")
        if _type_of(c) == "2110_io":
            engines.add(v)
        elif is_node_io(c):
            io.add(v)
    io_phys = {core_of[c]: v for c, v in owner.items() if v in io and c in core_of}
    for cpu in pool:
        v = owner.get(cpu)
        if v is None and core_of.get(cpu) in io_phys:
            v = io_phys[core_of[cpu]]                  # idle HT sibling of a node I/O core
        if v in io:
            out["cells"].append({"cpu": cpu, "kind": "io", "vmid": v if v in members else None,
                                 "owner": names.get(v) or ("#%s" % v)})
        elif v is not None:
            kind = "project" if v in members else ("engine" if v in engines else "other")
            cell = {"cpu": cpu, "kind": kind, "vmid": v if kind == "project" else None}
            if kind != "project":
                # Named in the tooltip: « whose is this core? » is the question asked of a grey cell.
                cell["owner"] = names.get(v) or ("#%s" % v)
            out["cells"].append(cell)
        elif core_of and core_of.get(cpu) in taken_physical:
            out["cells"].append({"cpu": cpu, "kind": "ht_blocked"})
        else:
            out["cells"].append({"cpu": cpu, "kind": "free"})
    out["dedicable"] = core_pool.free_dedicated_capacity(node_id)
    return out


def _wired_pairs(rows):
    """Wired pairs among the project's containers: (producer vmid, consumer vmid)."""
    from .migration import _shms, _dc
    produced, consumed = {}, {}
    for r in rows:
        c = db_get_container(r["vmid"]) or {}
        dc = _dc(c)
        p, k = _shms(dc.get("type"), c.get("hostname") or "", dc.get("params") or {})
        produced[r["vmid"]], consumed[r["vmid"]] = set(p), set(k)
    out = set()
    for a, ps in produced.items():
        for b, ks in consumed.items():
            if a != b and ps & ks:
                out.add((a, b))
    return sorted(out)


def move_suggestions(rows, nodes, pairs):
    """Containers that would be better on the node of the members they are wired to.

    For each running member, count its wired peers per node; the node holding the most of them,
    if it is not its own and holds MORE than its own, is a candidate move — its gain is how many
    links become local. Candidates are taken by decreasing gain; once a container is moved to a
    node, it and its peers already there are settled (no suggestion to move them back the other
    way). The room check uses what the container would need there (its proposal, else its current
    reservation, else 1 core):
      · the node's whole pool is smaller than that → no suggestion (it never fits);
      · cores held by others prevent it → suggestion with `fits: false` (« relevant, no room »);
      · otherwise `fits: true`, and the interface offers the move."""
    from .migration import INTERDITS
    by_vmid = {r["vmid"]: r for r in rows}
    peers = {}
    for a, b in pairs:
        peers.setdefault(a, set()).add(b)
        peers.setdefault(b, set()).add(a)
    size = {n["id"]: len(n["cells"]) for n in nodes}
    dedicable = {n["id"]: n["dedicable"] for n in nodes}
    names = {n["id"]: n["name"] for n in nodes}
    candidates = []
    for v, vs in peers.items():
        r = by_vmid.get(v)
        if not r or r.get("guarantee") == "stopped" or r.get("type") in INTERDITS or r.get("node_io"):
            continue
        per_node = {}
        for p in vs:
            pn = (by_vmid.get(p) or {}).get("node_id")
            if pn:
                per_node[pn] = per_node.get(pn, 0) + 1
        here = per_node.get(r.get("node_id"), 0)
        target = max((n for n in per_node if n != r.get("node_id")), key=lambda n: (per_node[n], -n),
                     default=None)
        if target is None or per_node[target] <= here:
            continue
        need = r.get("proposed") or r.get("alloc") or 1
        if size.get(target, 0) < need:
            continue                                   # node too small: never proposed
        candidates.append((per_node[target] - here, v, target, need))
    out, settled = [], set()
    for _gain, v, target, need in sorted(candidates, key=lambda x: (-x[0], x[1])):
        if v in settled:
            continue
        with_ = sorted(p for p in peers[v] if (by_vmid.get(p) or {}).get("node_id") == target)
        settled |= {v, *with_}
        out.append({"vmid": v, "hostname": by_vmid[v].get("hostname"), "from": by_vmid[v].get("node_id"),
                    "to": target, "to_name": names.get(target),
                    "with": [by_vmid[p]["hostname"] for p in with_],
                    "need": need, "fits": dedicable.get(target, 0) >= need})
    return out


def view(pid):
    """Everything the Resources window draws: the verdict and members (`project_guarantee`), each
    member's current dedicated cores (`alloc`) and proposal (`proposed`, from its calibration —
    None when unmeasured or not dedicated), every node's cells, the wired pairs and the suggestions."""
    from . import core_pool
    from .etalonnage import besoin
    pg = project_guarantee(pid)
    members = {r["vmid"] for r in pg["containers"]}
    for r in pg["containers"]:
        r["move_block"] = move_block(r)
        nid = r.get("node_id")
        r["alloc"] = core_pool.allocated_for(nid, r["vmid"]) if nid else 0
        r["proposed"] = (besoin(r.get("mesure")) if (r.get("required") and r.get("etat") == "etalonne")
                         else None)
    ids = {n["id"] for n in (db_get_nodes() or []) if (n.get("compute_cpuset") or "").strip()}
    ids |= {r.get("node_id") for r in pg["containers"] if r.get("node_id")}
    nodes = []
    for nid in sorted(ids):
        try:
            nodes.append(node_map(nid, members))
        except Exception as e:                                             # noqa: BLE001
            log.warning("project_resources.node_map(%s): %s", nid, e)
    try:
        pairs = _wired_pairs(pg["containers"])
    except Exception as e:                                                 # noqa: BLE001
        log.warning("project_resources._wired_pairs(%s): %s", pid, e)
        pairs = []
    pg.update(nodes=nodes, links=[list(x) for x in pairs],
              suggestions=move_suggestions(pg["containers"], nodes, pairs))
    return pg


# ── Applying a plan ──────────────────────────────────────────────────────────────────────────────
_JOBS = {}              # pid → {state, started, ended, steps[]}
_lock = threading.Lock()


def check_plan(pid, changes):
    """Refuse a plan before touching anything: not a member, no room on a node.

    `changes` = [{vmid, cores, node_id}] — `node_id` absent or equal to the current node: resize in
    place (hot); different: move (recreation). Room per node = what can be dedicated today + what
    the plan itself releases there (cores of members resized in place or leaving)."""
    from . import core_pool
    v_ = view(pid)
    by_vmid = {r["vmid"]: r for r in v_["containers"]}
    nodes = {n["id"]: n for n in v_["nodes"]}
    need, released, errors, plan = {}, {}, [], []
    for ch in changes or []:
        v = int(ch.get("vmid"))
        r = by_vmid.get(v)
        if not r:
            errors.append({"vmid": v, "error": "not_in_project"})
            continue
        if r.get("node_io"):
            # Sized by its active connectors, held on its card's node: nothing to plan here.
            errors.append({"vmid": v, "error": "node_io"})
            continue
        src = r.get("node_id")
        dst = int(ch.get("node_id") or src)
        # A move keeps the container's level: only a dedicated one takes cores on its target. A
        # shared one (« au mieux ») goes to the target's shared pool — moving it must not make it
        # dedicated. A resize in place is « guarantee it », which does mean dedicated.
        dedicated = (r.get("level") == "dedicated") or dst == src
        n = int(ch.get("cores") or 0) if dedicated else 0
        if dedicated and n < 1:
            errors.append({"vmid": v, "error": "cores"})
            continue
        if dst not in nodes or (dedicated and not nodes[dst]["pool"]):
            errors.append({"vmid": v, "error": "no_pool", "node_id": dst})
            continue
        if dst != src and move_block(r) in ("type", "stopped"):
            # Same refusal as the migration would give, but before touching anything.
            errors.append({"vmid": v, "error": "not_movable", "reason": move_block(r)})
            continue
        current = core_pool.allocated_for(src, v) if src else 0
        released[src] = released.get(src, 0) + current
        if n:
            need[dst] = need.get(dst, 0) + n
        plan.append({"vmid": v, "hostname": r.get("hostname"), "cores": n, "from": src, "to": dst,
                     "move": dst != src, "dedicated": dedicated})
    for nid, b in need.items():
        available = nodes[nid]["dedicable"] + released.get(nid, 0)
        if b > available:
            errors.append({"node_id": nid, "name": nodes[nid]["name"], "error": "capacity",
                           "need": b, "available": available})
    return (not errors), {"plan": plan, "errors": errors}


def apply_plan(pid, changes):
    """Check the plan, then run it in the background (a move recreates a container: tens of
    seconds). Resizes in place first — hot, no cut — then the moves, one by one."""
    ok, res = check_plan(pid, changes)
    if not ok:
        return False, res
    with _lock:
        cur = _JOBS.get(pid)
        if cur and cur["state"] == "running":
            return False, {"errors": [{"error": "running"}]}
        job = {"state": "running", "started": time.time(), "ended": None, "steps": []}
        _JOBS[pid] = job
    plan = sorted(res["plan"], key=lambda p: p["move"])
    threading.Thread(target=_run_plan, args=(pid, job, plan), daemon=True,
                     name="project-resources-%s" % pid).start()
    return True, {"plan": plan}


def _run_plan(pid, job, plan):
    from . import etalonnage, migration
    from .database import db_set_resources_override, db_add_alert, get_db
    for p in plan:
        step = {"vmid": p["vmid"], "hostname": p["hostname"], "cores": p["cores"],
                "move": p["move"], "ok": False, "detail": ""}
        job["steps"].append(step)
        try:
            if not p["move"]:
                ok, r = etalonnage.garantir(p["vmid"], coeurs=p["cores"])
                step["ok"], step["detail"] = ok, ("" if ok else r)
                continue
            # Move: the reservation is written on the container FIRST, so that the redeploy on the
            # target reserves it there; restored as it was if the move is refused.
            c = db_get_container(p["vmid"]) or {}
            before = (c.get("cores_override"), c.get("cpu_weight_override"))
            if p.get("dedicated", True):
                db_set_resources_override(p["vmid"], cores=p["cores"], cpu_weight="dedicated")
            r = migration.migrer(p["vmid"], p["to"])
            step["ok"] = bool(r.get("ok"))
            if not step["ok"]:
                step["detail"] = r.get("erreur") or "migration"
                with get_db() as db:
                    db.execute("UPDATE containers SET cores_override=?, cpu_weight_override=? WHERE vmid=?",
                               (before[0], before[1], p["vmid"]))
                    db.commit()
        except Exception as e:                                             # noqa: BLE001
            log.exception("project_resources: %s", p)
            step["detail"] = str(e)
    job["state"] = "done"
    job["ended"] = time.time()
    n_ok = sum(1 for s in job["steps"] if s["ok"])
    db_add_alert("alert.prep.resource_plan_applied", "info" if n_ok == len(plan) else "warning",
                 kind="prep", params={"ok": n_ok, "n": len(plan)})


def plan_status(pid):
    with _lock:
        j = _JOBS.get(pid)
        return dict(j, steps=list(j["steps"])) if j else {"state": "none"}
