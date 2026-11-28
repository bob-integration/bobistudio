#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Re-applying the shared cpuset never undoes a reservation made meanwhile (2026-11-23).

Every dedicated reservation starts, in the background, a run that gives the shared containers the
shared pool again. Measured on dell-1: three shards moved to dedicated cores in a row, two of them
ended back on the shared pool — a run working on a view taken before the next moves wrote the shared
cpuset over containers that had just been given their cores. Held here, offline:
  1. a container listed shared in the view but holding a reservation NOW is not written;
  2. runs on the same node never overlap (each computes its view when it starts);
     runs on different nodes are not serialised together.

MUTATIONS, REAPPLY_MUTATION=<name> — each must give rc=1:
    relire      no re-read before writing
    verrou      no lock

    ./venv/bin/python tests/verif_reapplication_partages_course.py
"""
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_reapply_"), "t.db")
config.DB_PATH = database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import core_pool                                                     # noqa: E402

MUT = os.environ.get("REAPPLY_MUTATION") or ""
MUTATIONS = {
    "relire": ('        if allocated_for(node_id, ligne["vmid"]):\n', '        if False:\n'),
    "verrou": ("    with _reapply_lock(node_id):\n        return _reappliquer_partages(node_id, force=force)\n",
               "    return _reappliquer_partages(node_id, force=force)\n"),
}
if MUT:
    if MUT not in MUTATIONS:
        print("unknown mutation:", MUT)
        sys.exit(2)
    _old, _new = MUTATIONS[MUT]
    _src = open(core_pool.__file__, encoding="utf-8").read()
    if _src.count(_old) != 1:
        print("mutation %s: pattern found %d times — the guard is stale" % (MUT, _src.count(_old)))
        sys.exit(1)
    exec(compile(_src.replace(_old, _new), core_pool.__file__, "exec"), core_pool.__dict__)
    print("MUTATION active:", MUT)

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


NODE = database.db_add_node("re-x", "127.0.0.1")
for v in (301, 302, 303):
    database.db_upsert_container_docker(v, "c-%d" % v, NODE, "bobi-%d" % v)
from app import node_driver                                                   # noqa: E402
writes = []
node_driver.host_exec = lambda node, cmd, timeout=None, **k: (writes.append(cmd) or (0, "", ""))
core_pool.db_add_alert = lambda *a, **k: None

print("\n1. Re-read before writing")
# The view was taken when 302 was still shared; 302 holds cores now.
core_pool.vue_coeurs = lambda nid, force=False: {
    "releve": True, "partage": {"cpuset": "10-20"},
    "partages": [{"vmid": v, "cpuset_applique": "0-20"} for v in (301, 302, 303)]}
HOLD = {302: 3}
core_pool.allocated_for = lambda nid, v: HOLD.get(v, 0)
out = core_pool.reappliquer_partages(NODE)
verifie(not any("bobi-302" in w for w in writes) and 302 in out["inchanges"],
        "★ a container that took a reservation meanwhile is not written", writes)
verifie(any("bobi-301" in w for w in writes) and any("bobi-303" in w for w in writes), "the shared ones are")

print("\n2. One run at a time per node")
active, overlap = [0], [0]
_real = core_pool._reappliquer_partages


def slow(node_id, force=False):
    active[0] += 1
    overlap[0] = max(overlap[0], active[0])
    time.sleep(0.2)
    active[0] -= 1
    return {"faits": [], "echecs": [], "inchanges": []}


core_pool._reappliquer_partages = slow
ts = [threading.Thread(target=core_pool.reappliquer_partages, args=(NODE,)) for _ in range(3)]
[t.start() for t in ts]
[t.join() for t in ts]
verifie(overlap[0] == 1, "★ three runs on one node never overlap", overlap[0])
overlap[0] = 0
ts = [threading.Thread(target=core_pool.reappliquer_partages, args=(n,)) for n in (NODE, NODE + 50)]
[t.start() for t in ts]
[t.join() for t in ts]
verifie(overlap[0] == 2, "two nodes are not serialised together", overlap[0])
core_pool._reappliquer_partages = _real

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
