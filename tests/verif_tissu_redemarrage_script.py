#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""A sharded wall that lost its assembler config gets it back at the next metrics poll.

Measured 2026-11-23, wall 1248 on dell-1: the assembler config is pushed hot, so a restarted
script came back as a monolith reading its sources at full size, and the fabric only noticed on its
next 30 s pass — 30 to 63 s with every frame about one period late. A restart signal is not enough
(the agent of that container publishes no `started_at`, and a 3 s restart is never seen stopped by
a 5 s poll), so the poll reads what the wall publishes: the source of each tile. Held here, offline:
  1. deploy.is_sharded_wall: true only with the fabric's `asm:<vmid>` row;
  2. metrics._wall_lost_assembler: pushes back (via _fabric_refresh_wall) only for a sharded wall
     whose tiles read no `fab_*` shard; never for a wall reading a shard, a wall that is not
     sharded, a payload without tiles; at most once per ASSEMBLER_PUSH_MIN_S;
  3. the poll calls it after reading :8080, and an error there does not break the poll.

    ./venv/bin/python tests/verif_tissu_redemarrage_script.py
"""
import ast
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


from app import database                                                     # noqa: E402
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_tissu_redem_"), "db.sqlite")
database.init_db()
from app import deploy, metrics                                              # noqa: E402

print("\n1. deploy.is_sharded_wall")
verifie(deploy.is_sharded_wall(901) is False, "no asm row: not sharded")
database.db_fabric_upsert("asm:901", 1, 901, "mv_out", "assembler", 0, 0)
verifie(deploy.is_sharded_wall(901) is True, "asm row: sharded")
verifie(deploy.is_sharded_wall(902) is False, "another wall is not concerned")

print("\n2. metrics._wall_lost_assembler")
pushed = []
deploy._fabric_refresh_wall = lambda v: pushed.append(v)
clock = [1000.0]
metrics.time.monotonic = lambda: clock[0]
MONO = {"tuiles": {"0": {"etat": "ok", "src": "decklink-io_3"}, "1": {"etat": "ok", "src": ""}}}
ASM = {"tuiles": {"0": {"etat": "ok", "src": "fab_efab"}, "1": {"etat": "ok", "src": ""}}}
verifie(metrics._wall_lost_assembler(901, ASM) is False and not pushed,
        "a wall reading a shard: nothing")
verifie(metrics._wall_lost_assembler(902, MONO) is False and not pushed,
        "a monolith that is not sharded: nothing")
verifie(metrics._wall_lost_assembler(901, {"fps": 50}) is False and not pushed,
        "no tiles published: nothing")
verifie(metrics._wall_lost_assembler(901, {"tuiles": {}}) is False and not pushed,
        "empty tiles: nothing")
verifie(metrics._wall_lost_assembler(901, MONO) is True and pushed == [901],
        "a sharded wall reading no shard: pushed back", pushed)
clock[0] += metrics.ASSEMBLER_PUSH_MIN_S - 1
verifie(metrics._wall_lost_assembler(901, MONO) is False and pushed == [901],
        "not again before ASSEMBLER_PUSH_MIN_S", pushed)
clock[0] += 2
verifie(metrics._wall_lost_assembler(901, MONO) is True and pushed == [901, 901],
        "again after ASSEMBLER_PUSH_MIN_S", pushed)
verifie(metrics.ASSEMBLER_PUSH_MIN_S < 30, "quicker than the fabric's 30 s pass",
        metrics.ASSEMBLER_PUSH_MIN_S)

print("\n3. the poll calls it")
src = open(os.path.join(ROOT, "app", "metrics.py"), encoding="utf-8").read()
fn = next(n for n in ast.parse(src).body
          if isinstance(n, ast.FunctionDef) and n.name == "rafraichir_metrics")
seq = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
       and getattr(n.func, "id", getattr(n.func, "attr", "")) in ("get_metrics", "_wall_lost_assembler")]
names = [getattr(c.func, "id", getattr(c.func, "attr", "")) for c in sorted(seq, key=lambda c: c.lineno)]
verifie("_wall_lost_assembler" in names, "rafraichir_metrics calls _wall_lost_assembler")
verifie(names and names.index("_wall_lost_assembler") > names.index("get_metrics"),
        "after reading :8080", names)
guarded = any(isinstance(t, ast.Try) and any(
    isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_wall_lost_assembler"
    for s in t.body for c in ast.walk(s)) for t in ast.walk(fn))
verifie(guarded, "inside a try (never breaks the poll)")

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
