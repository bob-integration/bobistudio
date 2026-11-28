# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""« ÉPROUVER » A PROJECT: does it hold under load? (P5 of the resource guarantee, 2026-11-23).

The guarantee verdict is INFERRED: cores reserved, a calibration, a deadline witness, a fit node.
This trial MEASURES it, the way it was done by hand in the night of 22/11: for DURATION seconds,
one load container per node saturates that node's SHARED pool at the « Arrière-plan » weight (128,
like a background container: the CPU the shared pool would give anyone else), and the deadline
witness (P1, app/deadlines.py) of every Dedicated container of the project is read before and after.

  · the load is `docker run --rm` of the node's compute image, `timeout DURATION` inside: it stops by
    itself even if the orchestrator dies, and is removed (`docker rm -f`) at the end or on abort;
  · it never touches a dedicated core: its cpuset is the shared pool as `core_pool.partage_disponible`
    gives it (dedicated cores, HT siblings of physical reservations and engine lcores excluded);
  · verdict: `held` (every watched Dedicated container missed nothing), `not_held` (the list of
    those that missed, with how many), `inconclusive` (no Dedicated container publishes a witness).
    A container without a witness is listed as such — never counted as held.

The shared pool's best-effort containers DO slow down during the trial: that is the point, and the
interface says so before starting. One trial at a time per node.
"""

import json
import logging
import threading
import time

from .database import db_add_alert, db_get_node, get_db

log = logging.getLogger(__name__)

DURATION_S = 300
DURATION_MIN_S, DURATION_MAX_S = 60, 1800
SETTLE_S = 7               # one metrics poll (5 s) after the load stops, so the last misses are read
LOAD_WEIGHT = 128          # cpu_weight.WEIGHTS["background"]

_lock = threading.Lock()
_active = {}               # pid → state dict (running trials)
_busy_nodes = {}           # node_id → pid


def ensure_table(db=None):
    db = db or get_db()
    db.execute("CREATE TABLE IF NOT EXISTS project_trials (project_id INTEGER PRIMARY KEY, data TEXT)")
    db.commit()


def _container_name(pid, nid):
    return "bobi-trial-p%s-n%s" % (pid, nid)


def load_command(pid, node, cpus, duration):
    """The `docker run` of the load container (pure: the guard reads it)."""
    from .core_pool import fmt_cpuset
    import shlex
    image = (node.get("compute_image") or "").strip()
    return ("docker run -d --rm --name %s --label bobi.trial=%s --network none --pull never "
            "--cpuset-cpus=%s --cpu-shares=%d --entrypoint timeout %s %d "
            "sh -c 'for i in $(seq %d); do (while :; do :; done) & done; wait'"
            % (_container_name(pid, node["id"]), int(pid), shlex.quote(fmt_cpuset(cpus)), LOAD_WEIGHT,
               shlex.quote(image), int(duration), len(cpus)))


def _witness_total(vmid):
    from . import deadlines
    blk = deadlines.state(vmid)
    if blk is None:
        return None
    try:
        return int(blk.get("missed_total") or 0)
    except (TypeError, ValueError):
        return None


def plan(pid):
    """What a trial would do → {members: [{vmid, hostname, node_id, watched}], nodes: [{id, name,
    cores, image}], refused: [{node_id, name, reason}]}. Measured, changes nothing."""
    from . import core_pool, project_resources
    g = project_resources.project_guarantee(pid)
    # The verdict rows carry no status for a running container (etalonnage rows): read the base.
    from .database import db_get_container
    members = [c for c in g["containers"] if c.get("required") and c.get("node_id")
               and (db_get_container(c["vmid"]) or {}).get("status") == "running"]
    out = {"members": [], "nodes": [], "refused": []}
    for c in members:
        out["members"].append({"vmid": c["vmid"], "hostname": c.get("hostname") or "",
                               "node_id": c["node_id"], "watched": _witness_total(c["vmid"]) is not None})
    for nid in sorted({c["node_id"] for c in members}):
        node = db_get_node(nid) or {}
        name = node.get("name") or str(nid)
        try:
            cpus = sorted(core_pool.partage_disponible(nid)[0])
        except Exception as e:                                 # noqa: BLE001
            log.warning("project_trial: shared pool of %s unreadable: %s", name, e)
            cpus = []
        if not cpus:
            out["refused"].append({"node_id": nid, "name": name, "reason": "no_shared_pool"})
        elif not (node.get("compute_image") or "").strip():
            out["refused"].append({"node_id": nid, "name": name, "reason": "no_image"})
        elif _busy_nodes.get(nid) not in (None, pid):
            out["refused"].append({"node_id": nid, "name": name, "reason": "busy"})
        else:
            out["nodes"].append({"id": nid, "name": name, "cores": cpus})
    return out


def start(pid, duration=DURATION_S, actor=None):
    """Start the trial → (ok, state | refusal dict)."""
    from . import node_driver
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        return False, {"error": "duration"}
    if not DURATION_MIN_S <= duration <= DURATION_MAX_S:
        return False, {"error": "duration", "min": DURATION_MIN_S, "max": DURATION_MAX_S}
    with _lock:
        if pid in _active:
            return False, {"error": "running"}
    p = plan(pid)
    if not p["members"]:
        return False, {"error": "no_dedicated"}
    if p["refused"]:
        return False, {"error": "refused", "refused": p["refused"]}
    with _lock:
        if pid in _active or any(_busy_nodes.get(n["id"]) not in (None, pid) for n in p["nodes"]):
            return False, {"error": "running"}
        for n in p["nodes"]:
            _busy_nodes[n["id"]] = pid
        st = {"project_id": pid, "status": "running", "started": time.time(), "duration_s": duration,
              "ends": time.time() + duration + SETTLE_S, "nodes": p["nodes"], "abort": False,
              "base": {m["vmid"]: _witness_total(m["vmid"]) for m in p["members"]},
              "members": p["members"], "actor": actor}
        _active[pid] = st
    started = []
    for n in p["nodes"]:
        node = db_get_node(n["id"]) or {}
        rc, out, err = node_driver.host_exec(node, load_command(pid, node, n["cores"], duration), timeout=60)
        if rc != 0:
            for s in started:
                _remove(pid, s)
            _finish(pid, {"status": "failed", "error": ((err or out) or "rc=%s" % rc).strip()[-300:],
                          "node": n["name"]})
            return False, {"error": "load_failed", "node": n["name"], "detail": (err or out or "")[-300:]}
        started.append(node)
    threading.Thread(target=_run, args=(pid,), daemon=True, name="trial-%s" % pid).start()
    return True, public(st)


def _remove(pid, node):
    from . import node_driver
    try:
        node_driver.host_exec(node, "docker rm -f %s >/dev/null 2>&1 || true" % _container_name(pid, node["id"]),
                              timeout=60)
    except Exception as e:                                     # noqa: BLE001
        log.warning("project_trial: cannot remove the load on %s: %s", node.get("name"), e)


def _run(pid):
    st = _active.get(pid)
    while st and time.time() < st["ends"] and not st["abort"]:
        time.sleep(1)
    if not st:
        return
    for n in st["nodes"]:
        _remove(pid, db_get_node(n["id"]) or {"id": n["id"]})
    if st["abort"]:
        _finish(pid, {"status": "aborted"})
        return
    _finish(pid, judge(st["members"], st["base"], {m["vmid"]: _witness_total(m["vmid"]) for m in st["members"]}))


def judge(members, base, after):
    """Before/after witness totals → result (pure: the guard calls it)."""
    rows, missed, unwatched = [], [], []
    for m in members:
        b, a = base.get(m["vmid"]), after.get(m["vmid"])
        if b is None or a is None:
            unwatched.append(m["vmid"])
            rows.append(dict(m, result="unwatched"))
        elif a < b:
            # The script restarted during the trial: its counter started again — what it missed
            # before the restart is lost, and a restart under load is itself a failure.
            missed.append(m["vmid"])
            rows.append(dict(m, result="restarted", missed=a))
        else:
            if a > b:
                missed.append(m["vmid"])
            rows.append(dict(m, result="missed" if a > b else "held", missed=a - b))
    watched = len(members) - len(unwatched)
    verdict = "inconclusive" if not watched else "not_held" if missed else "held"
    return {"status": "done", "verdict": verdict, "members": rows, "missed": missed, "unwatched": unwatched}


def _finish(pid, result):
    with _lock:
        st = dict(_active.get(pid) or {"project_id": pid})
    data = {k: v for k, v in st.items() if k not in ("abort", "base")}
    data.update(result, finished=time.time())
    if data.get("status") == "done":
        key = {"held": "alert.project.trial_held", "not_held": "alert.project.trial_not_held",
               "inconclusive": "alert.project.trial_inconclusive"}[data["verdict"]]
        db_add_alert(key, "info" if data["verdict"] == "held" else "warning",
                     kind="resource", params={"p": _project_name(pid), "n": len(data.get("missed") or []),
                                              "s": data.get("duration_s")})
    # Stored LAST: a result read as done has its alert written.
    db = get_db()
    try:
        ensure_table(db)
        db.execute("INSERT OR REPLACE INTO project_trials (project_id, data) VALUES (?, ?)",
                   (pid, json.dumps(data)))
        db.commit()
    except Exception as e:                                     # noqa: BLE001
        log.warning("project_trial: cannot store the result of %s: %s", pid, e)
    # Released last: never a moment where the trial is neither running nor stored.
    with _lock:
        _active.pop(pid, None)
        for nid, p in list(_busy_nodes.items()):
            if p == pid:
                _busy_nodes.pop(nid, None)


def _project_name(pid):
    from .database import db_get_project
    try:
        return (db_get_project(pid) or {}).get("name") or "#%s" % pid
    except Exception:                                          # noqa: BLE001
        return "#%s" % pid


def abort(pid):
    with _lock:
        st = _active.get(pid)
        if not st:
            return False
        st["abort"] = True
        st["ends"] = 0
    return True


def public(st):
    return {k: v for k, v in (st or {}).items() if k not in ("abort", "base")}


def state(pid):
    """The running trial, else the last stored result, else None."""
    with _lock:
        st = _active.get(pid)
        if st:
            return dict(public(st), remaining_s=max(0, round(st["ends"] - time.time())))
    db = get_db()
    try:
        ensure_table(db)
        r = db.execute("SELECT data FROM project_trials WHERE project_id=?", (pid,)).fetchone()
        return json.loads(r[0]) if r else None
    except Exception as e:                                     # noqa: BLE001
        log.warning("project_trial: cannot read %s: %s", pid, e)
        return None
