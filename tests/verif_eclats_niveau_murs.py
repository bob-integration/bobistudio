#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Shards of the compositor fabric inherit the most demanding level of their walls (2026-11-23).

A shard is a plain multiview and took the manifest's level, Shared — wall 1248, Dedicated, had its
shards at weight 1024 on the shared pool. A Dedicated wall is only as guaranteed as the cells it
reads. Held here, offline (throwaway database):
  1. shard_level_for: the most demanding level among the walls, unknown walls ignored;
  2. sync_shard_levels: every shard of the node is given its walls' level through cpu_weight.regler
     (hot, refused without room); nothing when it already has it; a refusal said ONCE;
  3. a new shard gets the level before its first `docker run`; reconcile passes the walls; the sync
     runs at every pass, fabric_auto on or off;
  4. the project verdict: a Dedicated wall with a shard not Dedicated, without cores, or with a
     deadline verdict, is `shards_not_guaranteed`; labels.

MUTATIONS, ECLAT_MUTATION=<name> — each must give rc=1:
    moins       the least demanding level is taken
    eteint      no sync when fabric_auto is off
    faible      a shard on the shared pool counts as guaranteed

    ./venv/bin/python tests/verif_eclats_niveau_murs.py
"""
import ast
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_eclats_"), "t.db")
config.DB_PATH = database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import deploy, project_resources                                     # noqa: E402

MUT = os.environ.get("ECLAT_MUTATION") or ""
MUTATIONS = {
    "moins": (deploy, "if lvl in _LEVEL_RANK and (best is None or _LEVEL_RANK[lvl] > _LEVEL_RANK[best]):",
              "if lvl in _LEVEL_RANK and (best is None or _LEVEL_RANK[lvl] < _LEVEL_RANK[best]):"),
    "eteint": (deploy, "        # Shards made while it was on (or by a wall edit) still follow their walls' level.\n"
                       "        if _lk.acquire(blocking=False):\n",
               "        if False:\n"),
    "faible": (project_resources, '        if lvl != "dedicated" or not holds or deadlines.verdict_of(sc):',
               '        if not holds or deadlines.verdict_of(sc):'),
}
if MUT:
    if MUT not in MUTATIONS:
        print("unknown mutation:", MUT)
        sys.exit(2)
    _mod, _old, _new = MUTATIONS[MUT]
    _src = open(_mod.__file__, encoding="utf-8").read()
    if _src.count(_old) != 1:
        print("mutation %s: pattern found %d times — the guard is stale" % (MUT, _src.count(_old)))
        sys.exit(1)
    exec(compile(_src.replace(_old, _new), _mod.__file__, "exec"), _mod.__dict__)
    print("MUTATION active:", MUT)

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


NODE = database.db_add_node("fab-x", "127.0.0.1")
for v, lvl in ((101, "dedicated"), (102, "shared"), (103, "background"), (201, None), (202, None), (203, None)):
    database.db_upsert_container_docker(v, "c-%d" % v, NODE, "bobi-%d" % v)
    database.db_update_deploy_config(v, "multiview", {})
    if lvl:
        database.db_set_resources_override(v, cpu_weight=lvl)

print("\n1. shard_level_for")
verifie(deploy.shard_level_for(["101", "102"]) == "dedicated", "★ a Dedicated wall among two: Dedicated")
verifie(deploy.shard_level_for([102, 103]) == "shared", "Shared and Background: Shared")
verifie(deploy.shard_level_for(["103", "999", "x"]) == "background", "unknown walls ignored")
verifie(deploy.shard_level_for([]) is None, "no wall: nothing")

print("\n2. sync_shard_levels")
ROWS = [{"kind": "shard", "ref": "201", "parents": json.dumps(["101", "102"])},
        {"kind": "shared", "ref": "202", "parents": json.dumps(["102"])},
        {"kind": "assembler", "ref": None, "parents": None},
        {"kind": "shard", "ref": "203", "parents": json.dumps(["103"])}]
database.db_fabric_all = lambda node_id=None: [dict(r) for r in ROWS]
from app import cpu_weight                                                    # noqa: E402
calls, answer = [], {"ok": True}
cpu_weight.regler = lambda vmid, niveau, reset=False, confirme_rt=False: (calls.append((vmid, niveau, confirme_rt)) or dict(answer))
said = []
deploy.db_add_alert = lambda key, *a, **k: said.append((key, k.get("vmid")))
lvl_of = cpu_weight.for_container(database.db_get_container(202), "multiview")["niveau"]
out = deploy.sync_shard_levels(NODE)
verifie((201, "dedicated", True) in calls and (203, "background", True) in calls,
        "★ shards given their walls' level, hot, through regler", calls)
verifie(all(c[0] != 202 for c in calls) or lvl_of != "shared", "a shard already at its level: untouched", (calls, lvl_of))
answer["ok"] = False
calls.clear()
deploy.sync_shard_levels(NODE)
deploy.sync_shard_levels(NODE)
verifie(said.count(("alert.fabric.shard_niveau_refuse", 201)) == 1, "★ refused (no room): said once", said)

print("\n3. Creation and passes")
src = open(deploy.__file__, encoding="utf-8").read()
fn = src[src.find("    def _deploy(name, params, hostname, walls=None):"):][:900]
verifie(fn and fn.find("db_set_resources_override(vmid, cpu_weight=_lvl)") < fn.find("deployer_script(vmid"),
        "★ a new shard gets the level BEFORE its first deployment")
cf = open(os.path.join(ROOT, "app", "compositor_fabric.py"), encoding="utf-8").read()
verifie('deploy_fn(_name, _params, _name, walls=node.get("shared_by"))' in cf, "reconcile passes the walls")
tree = ast.parse(src)
wrap = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "reconcile_fabric_node")
n_sync = sum(1 for n in ast.walk(wrap) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "sync_shard_levels")
verifie(n_sync == 2, "the sync runs at every pass, fabric_auto on and off", n_sync)
from app import settings, compositor_fabric                                   # noqa: E402
ran = []
deploy.sync_shard_levels = lambda nid: ran.append(nid)
settings.get = lambda k, d=None: "off" if k == "fabric_auto" else d
deploy.reconcile_fabric_node(NODE)
verifie(ran == [NODE], "fabric_auto off: the shards in place still follow", ran)

print("\n4. Project verdict")
from app import core_pool, deadlines                                          # noqa: E402
HOLD = {201: 2, 202: 1}      # 202 holds a core: only its LEVEL makes it weak
core_pool.allocated_for = lambda nid, v: HOLD.get(v, 0)
deadlines.verdict_of = lambda c: None
database.db_set_resources_override(201, cpu_weight="dedicated")
database.db_set_resources_override(202, cpu_weight="shared")
verifie(project_resources.weak_shards(101, {101: [201]}) == [], "a Dedicated shard holding cores: guaranteed")
verifie(project_resources.weak_shards(101, {101: [201, 202]}) == [202], "★ a shard not Dedicated: not guaranteed, even holding a core")
HOLD[201] = 0
verifie(project_resources.weak_shards(101, {101: [201]}) == [201], "Dedicated without cores: not guaranteed")
HOLD[201] = 2
deadlines.verdict_of = lambda c: "deadline_missed" if c.get("vmid") == 201 else None
verifie(project_resources.weak_shards(101, {101: [201]}) == [201], "a shard missing deadlines: not guaranteed")
verifie(project_resources.weak_shards(102, {}) == [], "a wall that is not sharded: nothing")
gs = project_resources.GUARANTEE_STATES
verifie(gs.index("shards_not_guaranteed") < gs.index("deadline_missed"), "state before the wall's own deadlines")
ph = open(os.path.join(ROOT, "templates", "projects.html"), encoding="utf-8").read()
verifie("shards_not_guaranteed: 'stopped'" in ph and "'node_unfit', 'shards_not_guaranteed'" in ph, "Projects shows it")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    missing = [k for k in ("projects.res_state.shards_not_guaranteed", "projects.res_count.shards_not_guaranteed",
                           "alert.fabric.shard_niveau_refuse") if not cat.get(k)]
    verifie(not missing, "%s: labels" % lang, missing)

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
