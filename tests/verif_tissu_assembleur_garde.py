#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""A sharded wall restarts as an assembler, not as a monolith (fabric rework step 1, 2026-11-25).

The fabric turned a wall into an assembler at hot only: any restart started it as a monolith and
the next pass converted it again — 14 missed deadlines on wall 1248 the 24/11. Held here, offline
(throwaway database, the REAL reconcile_fabric with fake callbacks):
  1. a conversion saves the assembler config on the `asm:` row and rewrites the script file; a
     pass that finds it in place rewrites nothing; a wall sharded before (nothing saved) is saved
     and rewritten once;
  2. the render (`deploy._render_send`) uses it: shard paths in the config sent, logical params
     kept in the base; a new plugin version keeps it; a moved cell, a stopped shard, a shard
     missing from the registry → rendered logical;
  3. a restore drops it and rewrites the file logical;
  4. rewrite_wall_script: renders at the RUNNING version, posts the file, never starts; unknown
     version → nothing written.

MUTATIONS, ASM_MUTATION=<name> — each must give rc=1:
    sans_rendu      the render ignores the saved config
    sans_fichier    a conversion does not rewrite the file
    sans_garde      the logical signature is not checked

    ./venv/bin/python tests/verif_tissu_assembleur_garde.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_asm_"), "t.db")
config.DB_PATH = database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import compositor_fabric as cf, deploy                               # noqa: E402

MUT = os.environ.get("ASM_MUTATION") or ""
MUTATIONS = {
    "sans_rendu": (deploy, "            if _asm is not None:\n                base = {**params, **_asm}",
                   "            if False:\n                base = {**params, **_asm}"),
    "sans_fichier": (cf, "        if _save_assembler(vmid, p, asm) and script_fn is not None:\n            script_fn(vmid)\n"
                         "        res[\"sharded\"].append(vmid)",
                     "        _save_assembler(vmid, p, asm)\n        res[\"sharded\"].append(vmid)"),
    "sans_garde": (cf, '    if saved.get("wall") != wall_signature(logical):',
                   '    if False:'),
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
WALL = 100


def wall(dx=0):
    cells = [{"path": "/dev/shm/src%d" % i, "name": "S%d" % i, "x": (i % 2) * 960 + (dx if i == 0 else 0),
              "y": (i // 2) * 540, "w": 960, "h": 540, "in_w": 1920, "in_h": 1080} for i in range(4)]
    return {"out_width": 1920, "out_height": 1080, "shm_out": "wall100", "flux_config": cells,
            "overlays": [], "fps": 50, "plugin_version": "0.116.37"}


database.db_upsert_container_docker(WALL, "multiview-Atelier", NODE, "bobi-cmp-100", status="running")
database.db_update_deploy_config(WALL, "multiview", wall())

_next = [300]
written, pushed, restored = [], [], []


def deploy_fn(name, params, hostname, walls=None):
    _next[0] += 1
    database.db_upsert_container_docker(_next[0], hostname, NODE, "bobi-cmp-%d" % _next[0], status="running")
    return _next[0]


def run(p, lat=40.0, **kw):
    return cf.reconcile_fabric(NODE, {WALL: p}, {WALL: lat}, deploy_fn, lambda n: None,
                               lambda v, a: pushed.append(a), lambda v, lp: restored.append(lp),
                               max_cells_per_shard=2, script_fn=lambda v: written.append(v), **kw)


print("1. conversion saves the assembler config and rewrites the file")
res = run(wall())
row = database.db_fabric_get("asm:%d" % WALL)
saved = json.loads((row or {}).get("params") or "{}")
verifie(res["sharded"] == [WALL] and len(res["nodes_created"]) >= 2, "the wall is sharded", res)
verifie(saved.get("wall") == cf.wall_signature(wall()) and
        all(w["path"].startswith("/dev/shm/fab_") for w in saved["asm"]["flux_config"]),
        "★ saved: the shard paths, with the signature of the logical wall", saved.get("asm", {}).get("flux_config"))
verifie(written == [WALL], "★ the script file is rewritten once", written)
written.clear()
cf._asm_pousse.clear()
pushed.clear()
run(wall())
verifie(written == [], "a pass that pushes the same config again: file not rewritten", written)
database.db_fabric_set_params("asm:%d" % WALL, None)
cf._asm_pousse[WALL] = (cf._asm_empreinte(pushed[-1]), __import__("time").monotonic())
run(wall())
verifie(written == [WALL] and database.db_fabric_get("asm:%d" % WALL)["params"],
        "a wall sharded before the config was saved: saved and rewritten once", written)

print("\n2. the render uses it")
send = deploy._render_send(WALL, "multiview", wall())
paths = [w["path"] for w in send.get("flux_config") or []]
verifie(paths and all(p.startswith("/dev/shm/fab_") for p in paths) and send.get("default_template") is None,
        "★ rendered as the assembler of its shards", paths)
verifie(json.loads(database.db_get_container(WALL)["deploy_config"])["params"]["flux_config"][0]["path"] == "/dev/shm/src0",
        "deploy_config keeps the logical wall")
verifie(all(p.startswith("/dev/shm/fab_") for p in
            [w["path"] for w in deploy._render_send(WALL, "multiview", dict(wall(), plugin_version="0.117.0"))["flux_config"]]),
        "a new plugin version keeps it")
verifie([w["path"] for w in deploy._render_send(WALL, "multiview", wall(dx=8))["flux_config"]][0] == "/dev/shm/src0",
        "★ a cell moved since: rendered logical")
shard = next(r for r in database.db_fabric_all(NODE) if r["kind"] == "shard")
database.db_update_status(int(shard["ref"]), "stopped")
verifie(deploy._render_send(WALL, "multiview", wall())["flux_config"][0]["path"] == "/dev/shm/src0",
        "a shard not running: rendered logical")
database.db_update_status(int(shard["ref"]), "running")
database.db_fabric_delete(shard["signature"])
verifie(deploy._render_send(WALL, "multiview", wall())["flux_config"][0]["path"] == "/dev/shm/src0",
        "a shard missing from the registry: rendered logical")
database.db_fabric_upsert(shard["signature"], NODE, None, shard["shm"], "shard", shard["out_w"], shard["out_h"],
                          ref=shard["ref"], parents=json.loads(shard["parents"] or "null"),
                          tile_x=shard["tile_x"], tile_y=shard["tile_y"], fmt=shard["fmt"])
verifie(deploy._render_send(WALL, "mixer", wall())["flux_config"][0]["path"] == "/dev/shm/src0",
        "another type: never")

print("\n3. rewrite_wall_script")
posts = []


class _Sess:
    def post(self, url, json=None, headers=None, timeout=None):
        posts.append((url, json))
        return type("R", (), {"status_code": 200})()


deploy.agent_session = lambda: _Sess()
deploy.agent_headers = lambda v: {}
deploy.get_container_ip = lambda v: "10.0.0.1"
rendered = []
deploy.generer_script = lambda t, p, v: rendered.append((t, p, v)) or "#script %s" % v
deploy.version_en_cours = lambda ip, v: "0.116.37"
ok = deploy.rewrite_wall_script(WALL)
verifie(ok and len(posts) == 1 and posts[0][0].endswith("/deploy") and posts[0][1]["path"] == "/opt/script/main.py",
        "★ the file is posted to the agent's /deploy, nothing else (no /start)", posts)
verifie(rendered and rendered[-1][2] == "0.116.37" and rendered[-1][1]["flux_config"][0]["path"].startswith("/dev/shm/fab_"),
        "rendered at the running version, as the assembler", rendered[-1][2] if rendered else None)
posts.clear()
deploy.version_en_cours = lambda ip, v: None
verifie(deploy.rewrite_wall_script(WALL) is False and not posts, "running version unknown: nothing written")
deploy.version_en_cours = lambda ip, v: "0.116.37"

print("\n4. restore drops it")
written.clear()
cf._asm_pousse.clear()
res = run(wall(), lat=5.0, restaurables={WALL})
verifie(res["restored"] == [WALL] and database.db_fabric_get("asm:%d" % WALL) is None, "the wall is restored", res)
verifie(written == [WALL], "★ the file is rewritten logical", written)
verifie(deploy._render_send(WALL, "multiview", wall())["flux_config"][0]["path"] == "/dev/shm/src0",
        "and rendered logical")

print("\n5. wiring")
src = open(deploy.__file__, encoding="utf-8").read()
verifie("generer_script(type_script, _render_send(vmid, type_script, params), version)" in src,
        "deployer_script renders through _render_send")
verifie("rewrite_wall_script(v)" in src, "the node reconcile passes rewrite_wall_script")

print("\n✓ all green" if not ko else "\n✗ %d failure(s)" % ko)
sys.exit(1 if ko else 0)
