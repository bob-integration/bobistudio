#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""« Éprouver » a project (P5, decided with Cyril on 2026-11-23).

For a few minutes one load container per node saturates that node's SHARED pool at the
« Arrière-plan » weight, and the deadline witness of every Dedicated container of the project is
read before and after: the guarantee measured, not inferred. Held here, offline (host doubled):
  1. the load: `docker run --rm`, its cpuset = the shared pool only (never a dedicated core), weight
     128, `timeout` inside so it stops by itself, no network, one busy loop per core;
  2. judge: held / not held (with how many) / restarted / without witness never counted as held /
     inconclusive when nobody is watched;
  3. end to end: started, removed at the end, result stored and alerted; one trial at a time per
     project and per node; abort removes the load; a node without shared pool or image refuses;
  4. route (POST and DELETE need containers.deploy), journal, Projects → Resources, labels.

MUTATIONS, TRIAL_MUTATION=<name> — each must give rc=1:
    poids       the load runs at the shared weight (1024)
    dedie       the load's cpuset is the whole compute pool
    non_suivi   a container without witness counts as held
    ménage      the load is not removed at the end
    statut      the members' status is read from the verdict rows (which have none when running)

    ./venv/bin/python tests/verif_eprouver_projet.py
"""
import json
import os
import re
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_eprouver_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import project_trial as pt                                           # noqa: E402

MUT = os.environ.get("TRIAL_MUTATION") or ""
MUTATIONS = {
    "poids": ("LOAD_WEIGHT = 128 ", "LOAD_WEIGHT = 1024 "),
    "dedie": ("            cpus = sorted(core_pool.partage_disponible(nid)[0])\n",
              "            cpus = sorted(core_pool.parse_cpuset(node.get(\"compute_cpuset\") or \"\"))\n"),
    "non_suivi": ('            unwatched.append(m["vmid"])\n            rows.append(dict(m, result="unwatched"))\n',
                  '            rows.append(dict(m, result="held", missed=0))\n'),
    "statut": ('               and (db_get_container(c["vmid"]) or {}).get("status") == "running"]',
               '               and c.get("status") == "running"]'),
    "ménage": ('        _remove(pid, db_get_node(n["id"]) or {"id": n["id"]})\n', "        pass\n"),
}
if MUT:
    if MUT not in MUTATIONS:
        print("unknown mutation:", MUT)
        sys.exit(2)
    _old, _new = MUTATIONS[MUT]
    _src = open(pt.__file__, encoding="utf-8").read()
    if _src.count(_old) != 1:
        print("mutation %s: pattern found %d times — the guard is stale" % (MUT, _src.count(_old)))
        sys.exit(1)
    exec(compile(_src.replace(_old, _new), pt.__file__, "exec"), pt.__dict__)
    print("MUTATION active:", MUT)

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


from app import core_pool, node_driver, project_resources, deadlines          # noqa: E402

NA = database.db_add_node("trial-a", "127.0.0.1")
database.db_update_node(NA, compute_cpuset="0-11", compute_image="bobi-compute:9.9")
NB = database.db_add_node("trial-b", "127.0.0.1")
database.db_update_node(NB, compute_cpuset="0-3", compute_image="")
SHARED = {NA: [6, 7, 8, 9, 10, 11], NB: [0, 1]}            # 0-5 of trial-a are dedicated
core_pool.partage_disponible = lambda nid, **k: (list(SHARED.get(nid, [])), set())

print("\n1. The load")
cmd = pt.load_command(7, database.db_get_node(NA), SHARED[NA], 300)
verifie("docker run -d --rm " in cmd and "--cpuset-cpus=6-11 " in cmd and "--cpu-shares=128 " in cmd
        and "--entrypoint timeout bobi-compute:9.9 300 " in cmd and "--network none" in cmd
        and "$(seq 6)" in cmd and "--name bobi-trial-p7-n%d" % NA in cmd,
        "★ shared pool only, weight 128, stops by itself, no network, one loop per core", cmd)

print("\n2. Judge")
M = [{"vmid": 1, "hostname": "a"}, {"vmid": 2, "hostname": "b"}, {"vmid": 3, "hostname": "c"}]
r = pt.judge(M, {1: 10, 2: 0, 3: None}, {1: 10, 2: 0, 3: None})
verifie(r["verdict"] == "held" and r["unwatched"] == [3], "★ held, the unwatched one named, not held", r)
r = pt.judge(M, {1: 10, 2: 0, 3: 5}, {1: 14, 2: 0, 3: 5})
verifie(r["verdict"] == "not_held" and r["missed"] == [1] and r["members"][0]["missed"] == 4,
        "not held: who, and how many", r)
r = pt.judge(M[:1], {1: 10}, {1: 3})
verifie(r["verdict"] == "not_held" and r["members"][0]["result"] == "restarted", "a restart under load fails", r)
verifie(pt.judge(M[:1], {1: None}, {1: None})["verdict"] == "inconclusive", "nobody watched: inconclusive")

print("\n3. End to end")
PID = 5
# Like the real verdict rows: NO status on a running member (the base says it).
MEMBERS = [{"vmid": 11, "hostname": "mix", "node_id": NA, "required": True},
           {"vmid": 12, "hostname": "aud", "node_id": NA, "required": True},
           {"vmid": 13, "hostname": "pre", "node_id": NA, "required": False},
           {"vmid": 14, "hostname": "off", "node_id": NA, "required": True}]
for v, st in ((11, "running"), (12, "running"), (13, "running"), (14, "crash_loop"), (21, "running")):
    database.db_upsert_container_docker(v, "c-%d" % v, NA, "bobi-%d" % v)
    database.db_update_status(v, st)
project_resources.project_guarantee = lambda pid: {"containers": MEMBERS if pid == PID else
                                                   [dict(MEMBERS[0], vmid=21, node_id=NB if pid == 6 else NA)]}
TOT = {11: 100, 12: 4}
deadlines.state = lambda v: ({"missed_total": TOT[v]} if v in TOT else None)
cmds = []
node_driver.host_exec = lambda node, c, timeout=None, **k: (cmds.append((node.get("id"), c)) or (0, "", ""))
pt.DURATION_MIN_S, pt.SETTLE_S = 1, 0
pl = pt.plan(PID)
verifie([m["vmid"] for m in pl["members"]] == [11, 12] and pl["nodes"][0]["cores"] == SHARED[NA],
        "★ the plan: the RUNNING Dedicated members (status from the base), the shared cores", pl)
ok, st = pt.start(PID, 2)
verifie(ok and st["status"] == "running" and any("docker run" in c for _, c in cmds), "started", st)
ok2, why = pt.start(PID, 2)
verifie(not ok2 and why.get("error") == "running", "★ not twice on the same project")
ok3, why3 = pt.start(9, 2)
verifie(not ok3 and why3.get("error") == "refused" and why3["refused"][0]["reason"] == "busy",
        "★ not twice on the same node", why3)
TOT[11] = 103                       # 3 missed under load
for _ in range(60):
    if pt.state(PID).get("status") != "running":
        break
    time.sleep(0.2)
res = pt.state(PID)
verifie(res.get("status") == "done" and res.get("verdict") == "not_held" and res.get("missed") == [11],
        "★ result: not held, the mixer missed 3", res)
verifie(any(c.startswith("docker rm -f bobi-trial-p%d-n%d" % (PID, NA)) for _, c in cmds),
        "★ the load is removed at the end", [c for _, c in cmds][-2:])
with database.get_db() as db:
    n = db.execute("SELECT COUNT(*) FROM alerts WHERE msg_key='alert.project.trial_not_held'").fetchone()[0]
verifie(n == 1, "alerted once")
verifie(pt.start(9, 2)[0] is True, "the node is free again")
cmds.clear()
verifie(pt.abort(9) is True, "abort")
for _ in range(30):
    if (pt.state(9) or {}).get("status") != "running":
        break
    time.sleep(0.2)
verifie(pt.state(9).get("status") == "aborted" and any("docker rm -f" in c for _, c in cmds), "abort removes the load")
ok, why = pt.start(6, 2)
verifie(not ok and why["refused"][0]["reason"] == "no_image", "a node without compute image refuses", why)
SHARED[NB] = []
verifie(pt.plan(6)["refused"][0]["reason"] == "no_shared_pool", "a node without shared pool refuses")
verifie(pt.start(PID, 5000)[1].get("error") == "duration", "duration bounded")

print("\n4. Route, interface, labels")
rs = open(os.path.join(ROOT, "app", "routes", "projects_api.py"), encoding="utf-8").read()
blk = rs[rs.find('@bp.route("/api/projects/<int:pid>/trial"'):][:1800]
verifie('methods=["GET", "POST", "DELETE"]' in blk and 'if not has_perm("containers.deploy"):' in blk
        and blk.find('has_perm("containers.deploy")') < blk.find("project_trial.abort(pid)")
        and "alert.audit.eprouver_projet" in blk, "route: changing needs containers.deploy, journalled")
ph = open(os.path.join(ROOT, "templates", "projects.html"), encoding="utf-8").read()
verifie("function resTrialStart" in ph and "window.confirm(t('projects.trial.confirm')" in ph
        and "_resTrialLoad(pid)" in ph and "class=\"ctl-push\" onclick=\"resTrialStart" in ph,
        "Projects → Resources: shared push control, confirmation naming the nodes")
keys = set("projects.trial." + k for k in re.findall(r"t\('projects\.trial\.(\w+)'\)", ph))
keys |= {"projects.trial." + p + v for p, vs in (("refused_", ("no_shared_pool", "no_image", "busy")),
         ("verdict_", ("held", "not_held", "inconclusive")),
         ("err_", ("running", "no_dedicated", "refused", "load_failed", "duration", "forbidden", "other")))
         for v in vs} | {"projects.trial.aborted", "projects.trial.failed",
                          "alert.project.trial_held", "alert.project.trial_not_held",
                          "alert.project.trial_inconclusive", "alert.audit.eprouver_projet"}
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    missing = sorted(k for k in keys if not cat.get(k))
    verifie(not missing, "%s: labels (%d)" % (lang, len(keys)), missing)

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
