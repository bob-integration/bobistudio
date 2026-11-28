#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Real-time priority of the player, multiview and recorder deadline threads (2026-11-23).

The number comes from the orchestrator (app/realtime.py); each plugin must give it to the RIGHT
thread and to it alone (a thread inherits its creator's policy). Held here, offline:
  1. multiview (role monitoring): the compose loop is promoted at module level, after every
     module-level thread start and before the loop; no thread is started from inside a function
     except by the TSL server (a normal thread); :8080 publishes what was obtained;
  2. recorder (role program): the capture thread promotes itself first; it starts no thread and no
     process (ffmpeg is spawned by the writer); the result is published;
  3. player (role program): the grid thread of the Rust element is created inside set_state, so the
     calling thread is promoted around set_state ONLY and dropped back in a finally, with no thread
     started in between; GStreamer streaming threads are dropped back on ENTER; :8080 publishes the
     priority read on the grid thread itself — checked for real on a renamed thread when permitted.

    ./venv/bin/python tests/verif_rt_player_multiview_recorder.py
"""
import ast
import ctypes
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


def plugin(name):
    d = os.path.join(ROOT, "plugins", name)
    if not os.path.isfile(os.path.join(d, "script.py")):
        return None, None, None
    src = open(os.path.join(d, "script.py"), encoding="utf-8").read()
    src = src.format(config="{}", hostname="tst", plugin_version="test")
    return src, ast.parse(src), json.load(open(os.path.join(d, "plugin.json"), encoding="utf-8"))


def starts_thread(node):
    return any(isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "start"
               and isinstance(n.func.value, ast.Call) and getattr(n.func.value.func, "attr", None) == "Thread"
               for n in ast.walk(node))


def calls(node, attr):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == attr]


def threads_in_functions(tree):
    out = []
    for f in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        for c in calls(f, "Thread"):
            out.append(f.name)
    return out


def manifest_ok(name, man, role):
    res = man.get("resources") or {}
    verifie(res.get("realtime") == role, "%s: manifest declares the role « %s »" % (name, role), res.get("realtime"))
    verifie("rt_priority" not in {f.get("key") for f in man.get("config_schema", [])}
            and not ({"cap_add", "ulimits"} & set(res)), "%s: no setting, no hand-written grant" % name)


import tempfile                                                              # noqa: E402
from app import database                                                     # noqa: E402
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_rt_p_"), "db.sqlite")   # before any access
database.init_db()
from app import plugins, realtime                                             # noqa: E402

print("\n1. multiview")
src, tree, man = plugin("multiview")
if src is None:
    print("  · plugins/multiview absent, skipped")
else:
    body = tree.body
    rt_at = [i for i, n in enumerate(body) if isinstance(n, ast.If) and calls(n, "rt_promote")]
    loops = [i for i, n in enumerate(body) if isinstance(n, ast.While)]
    starts = [i for i, n in enumerate(body) if not isinstance(n, (ast.FunctionDef, ast.ClassDef)) and starts_thread(n)]
    verifie(len(rt_at) == 1 and len(calls(tree, "rt_promote")) == 1, "compose loop promoted once, at module level", rt_at)
    verifie(rt_at and loops and rt_at[0] < loops[-1]
            and not any(rt_at[0] < i < loops[-1] for i in starts), "before the loop, no thread started in between")
    verifie(starts and rt_at and max(starts) < rt_at[0], "after every module-level thread start", (starts, rt_at))
    verifie("rt_priority(CONFIG)" in ast.unparse(body[rt_at[0]]) if rt_at else False, "at the number CONFIG carries")
    verifie(sorted(threads_in_functions(tree)) == ["_splash_phase", "_tsl_server"],
            "no thread started from a function but the TSL server's clients and the startup splash",
            threads_in_functions(tree))
    # The startup splash (0.116.36) starts its writer thread: only BEFORE the promotion, never inherit it.
    sp_at = [i for i, n in enumerate(body) if isinstance(n, ast.Expr) and ast.unparse(n) == "_splash_phase()"]
    verifie(len(sp_at) == 1 and rt_at and sp_at[0] < rt_at[0], "the startup splash runs before the promotion",
            (sp_at, rt_at))
    verifie("metrics.update(bobimxl.rt_state()" in src, ":8080 publishes the obtained priority")
    manifest_ok("multiview", man, "monitoring")
    verifie(plugins.render_config("multiview", {}).get("rt_priority") == realtime.ROLES["monitoring"] == 3,
            "rendered CONFIG carries 3")

print("\n2. recorder")
src, tree, man = plugin("recorder")
if src is None:
    print("  · plugins/recorder absent, skipped")
else:
    fn = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    cap = fn.get("_capture")
    first = [s for s in (cap.body if cap else []) if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))][:1]
    verifie(bool(first) and isinstance(first[0], ast.If) and calls(first[0], "rt_promote"),
            "capture promotes itself first")
    verifie(len(calls(tree, "rt_promote")) == 1, "and nothing else is promoted")
    verifie(cap is not None and not calls(cap, "Thread") and not calls(cap, "Popen"),
            "capture starts no thread and no process")
    verifie(cap is not None and any("metrics.update(rt)" in ast.unparse(s) for s in cap.body),
            "the result is published")
    manifest_ok("recorder", man, "program")

print("\n3. player")
src, tree, man = plugin("player")
if src is None:
    print("  · plugins/player absent, skipped")
else:
    fn = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    ns_ = fn["_native_start"]
    stmts = ns_.body
    pi = [i for i, s in enumerate(stmts) if isinstance(s, ast.If) and calls(s, "rt_promote")]
    ti = [i for i, s in enumerate(stmts) if isinstance(s, ast.Try) and calls(s, "set_state")]
    verifie(len(pi) == 1 and len(ti) == 1 and ti[0] == pi[0] + 1,
            "promoted right before the try that holds set_state", (pi, ti))
    t = None
    if ti:
        t = stmts[ti[0]]
        verifie(any(calls(s, "rt_release") for s in t.finalbody), "dropped back in its finally")
        verifie(not starts_thread(t) and not any(starts_thread(stmts[i]) for i in range(pi[0], ti[0] + 1)),
                "no thread started while promoted")
    verifie(len(calls(tree, "set_state")) >= 1 and all(
        c in calls(ns_, "set_state") or "NULL" in ast.unparse(c) for c in calls(tree, "set_state")),
        "the pipeline is set PLAYING only there (decks go to NULL only)")
    st = fn["_native_stream_status"]
    verifie(calls(st, "rt_release") and ast.unparse(st).index("rt_release") < ast.unparse(st).index("setpriority"),
            "streaming threads dropped back on ENTER, before their nice")
    verifie("payload.update(_grid_rt_state())" in src, ":8080 publishes the grid thread's priority")
    manifest_ok("player", man, "program")
    # _grid_rt_state for real: a thread renamed like the element's grid thread.
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == "_grid_rt_state")
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_GRID_COMM" for t in n.targets))]
    state = {"rt_priority": 0, "rt_priority_error": ""}
    fake_bx = type("B", (), {"rt_promote": staticmethod(lambda p: p), "rt_state": staticmethod(lambda: dict(state))})
    g = {"os": os, "bobimxl": fake_bx}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "player", "exec"), g)
    verifie(g["_grid_rt_state"]() == {"rt_priority": 0, "rt_priority_error": "grid thread not found"},
            "no grid thread: 0, and it says so", g["_grid_rt_state"]())
    box, go, done = {}, threading.Event(), threading.Event()
    named, normal_read = threading.Event(), threading.Event()

    def grid():
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(15, g["_GRID_COMM"].encode(), 0, 0, 0)          # PR_SET_NAME
        named.set(); normal_read.wait(5)
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(4))
            box["fifo"] = True
        except OSError as e:
            box["skip"] = str(e)
        go.set(); done.wait(5)
        if box.get("fifo"):
            os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    th = threading.Thread(target=grid); th.start(); named.wait(5)
    got0 = g["_grid_rt_state"]()
    verifie(got0 == {"rt_priority": 0, "rt_priority_error": ""}, "grid thread left normal: read as 0", got0)
    normal_read.set(); go.wait(5)
    got = g["_grid_rt_state"]()
    done.set(); th.join(5)
    if "skip" in box:
        verifie(got["rt_priority"] == 0 and got["rt_priority_error"] == "", "normal grid thread: 0", got)
        print("  · SCHED_FIFO not permitted here (%s), FIFO read not checked" % box["skip"])
    else:
        verifie(got == {"rt_priority": 4, "rt_priority_error": ""}, "FIFO 4 grid thread: read as 4", got)
    # _rt_keep_only_grid for real: a FIFO helper is dropped back, the grid keeps FIFO.
    if t is not None:
        verifie(any("_rt_keep_only_grid()" in ast.unparse(s) for s in t.finalbody), "the finally also drops every helper")
    keep2 = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_rt_keep_only_grid"]
    g2 = dict(g, log=lambda *a: None)
    exec(compile(ast.Module(body=keep2, type_ignores=[]), "player", "exec"), g2)
    res, ready, stop = {}, threading.Barrier(3, timeout=5), threading.Event()

    def worker(name):
        ctypes.CDLL(None).prctl(15, name.encode(), 0, 0, 0)
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(2))
        except OSError as e:
            res["skip"] = str(e)
        ready.wait(); stop.wait(5)
        res[name] = os.sched_getscheduler(0)
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0)) if res[name] != os.SCHED_OTHER else None
    ws = [threading.Thread(target=worker, args=(n,)) for n in (g["_GRID_COMM"], "mxl-helper")]
    [w.start() for w in ws]; ready.wait()
    g2["_rt_keep_only_grid"]()
    stop.set(); [w.join(5) for w in ws]
    if "skip" in res:
        print("  · SCHED_FIFO not permitted here, _rt_keep_only_grid not checked for real")
    else:
        verifie(res.get(g["_GRID_COMM"]) == os.SCHED_FIFO and res.get("mxl-helper") == os.SCHED_OTHER,
                "a FIFO helper is dropped back, the grid keeps FIFO", res)
    g["bobimxl"] = type("Old", (), {})
    verifie("too old" in g["_grid_rt_state"]()["rt_priority_error"], "older orchestrator: it says so")

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
