#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Real-time priority of the decklink_io data path (decklink_io 0.29.59, 2026-11-23).

The deadline is held by the DeckLink SDK callback threads inside decklink_rx / decklink_tx. The
supervisor puts each binary in SCHED_FIFO between fork and exec, so every thread the binary or
the SDK creates inherits it; the supervisor itself stays normal. Held here, offline:
  1. the manifest declares the role « output », the rendered CONFIG carries 8, the container gets
     SYS_NICE and an rtprio limit;
  2. every binary goes through _lancer, which asks FIFO in the child only; the probe and profile
     tools do not go through it;
  3. for real, on a child that starts a second thread: both threads are read in SCHED_FIFO at the
     given number; an older orchestrator, a disabled priority and no running binary say so;
  4. :8080 publishes it.

    ./venv/bin/python tests/verif_rt_decklink_io.py
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
D = os.path.join(ROOT, "plugins", "decklink_io")
ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


if not os.path.isfile(os.path.join(D, "script.py")):
    print("  · plugins/decklink_io absent, skipped")
    sys.exit(0)

from app import database                                                     # noqa: E402
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_rt_dl_"), "db.sqlite")   # before any access
database.init_db()
from app import docker_compute, plugins, realtime                            # noqa: E402

SRC = open(os.path.join(D, "script.py"), encoding="utf-8").read().format(
    config="{}", hostname="tst", plugin_version="test")
TREE = ast.parse(SRC)
MAN = json.load(open(os.path.join(D, "plugin.json"), encoding="utf-8"))
FN = {n.name: n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)}


def calls(node, attr):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None)) == attr]


print("\n1. manifest and controller")
res = MAN.get("resources") or {}
verifie(res.get("realtime") == "output", "manifest declares the role « output »", res.get("realtime"))
verifie(not ({"cap_add", "ulimits"} & set(res))
        and "rt_priority" not in {f.get("key") for f in MAN.get("config_schema", [])}, "no setting, no hand-written grant")
verifie(plugins.render_config("decklink_io", {"rt_priority": 0}).get("rt_priority") == realtime.ROLES["output"] == 8,
        "rendered CONFIG carries 8, over a persisted value")
caps, ul = docker_compute.granted_caps("decklink_io")
verifie("SYS_NICE" in caps and ul.get("rtprio", "").startswith("20"), "container gets SYS_NICE and rtprio", (caps, ul))

print("\n2. launch path")
popens = calls(TREE, "Popen")
verifie(len(popens) == 1 and popens[0] in calls(FN["_lancer"], "Popen"), "the only Popen is in _lancer")
verifie(any(k.arg == "preexec_fn" and "_rt_enfant" in ast.unparse(k.value) for k in popens[0].keywords),
        "it asks FIFO in the child (preexec_fn)")
users = sorted(f for f, n in FN.items() if f != "_lancer" and calls(n, "_lancer"))
verifie(users == ["_superviser", "_superviser_sortie"], "inputs and outputs both launch through it", users)
verifie(not calls(FN["_rt_enfant"], "Thread") and len(calls(FN["_rt_enfant"], "sched_setscheduler")) == 1,
        "the child hook is one syscall")
verifie(all(not calls(FN[f], "_lancer") for f in ("_inventaire", "_basculer_profil")),
        "probe and profile tools stay normal")

print("\n3. for real")
keep = [n for n in TREE.body if (isinstance(n, ast.FunctionDef) and n.name in
                                 ("_rt_enfant", "_rt_fils", "_rt_state", "_lancer"))
        or (isinstance(n, ast.Try) and "RT_PRIORITY" in ast.unparse(n))
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("_enfants", "_verrou_enfants") for t in n.targets))]


def load(cfg):
    g = {"os": os, "subprocess": subprocess, "threading": threading, "CONFIG": cfg}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "decklink_io", "exec"), g)
    return g


CHILD = [sys.executable, "-c", "import threading,time;t=threading.Thread(target=time.sleep,args=(3,));"
         "t.start();time.sleep(3)"]
g = load({"rt_priority": 8})
verifie(g["RT_PRIORITY"] == 8, "priority taken from CONFIG")
verifie(g["_rt_state"]() == {"rt_priority": 0, "rt_priority_error": "no connector running"},
        "no binary running: 0, and it says so", g["_rt_state"]())
me = os.sched_getscheduler(0)
p = g["_lancer"](CHILD)
time.sleep(0.5)
got = g["_rt_state"]()
n = len(g["_rt_fils"](p.pid))
p.kill(); p.wait()
verifie(os.sched_getscheduler(0) == me, "the supervisor's own policy is untouched")
verifie(n >= 2, "the child runs a second thread", n)
try:
    os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(1))
    os.sched_setscheduler(0, me, os.sched_param(0))
    permitted = True
except OSError as e:
    permitted, why = False, str(e)
if permitted:
    verifie(got == {"rt_priority": 8, "rt_priority_error": ""}, "every thread read in FIFO 8", got)
else:
    verifie(got["rt_priority"] == 0 and "not in SCHED_FIFO" in got["rt_priority_error"], "refused: 0, and it says so", got)
    print("  · SCHED_FIFO not permitted here (%s), FIFO read not checked" % why)
if permitted:
    # A thread that left FIFO, then one at a lower priority: the lowest thread is what is published.
    for arg, want in (("os.SCHED_OTHER,os.sched_param(0)", (0, "1 of 2 data path threads not in SCHED_FIFO")),
                      ("os.SCHED_FIFO,os.sched_param(3)", (3, ""))):
        p = g["_lancer"]([sys.executable, "-c", "import os,threading,time\n"
                          "def f():\n os.sched_setscheduler(0,%s); time.sleep(3)\n"
                          "threading.Thread(target=f).start(); time.sleep(3)" % arg])
        time.sleep(0.5)
        got = g["_rt_state"]()
        p.kill(); p.wait()
        verifie((got["rt_priority"], got["rt_priority_error"]) == want, "one thread at %s: %s" % (arg, want), got)
g0 = load({"rt_priority": 0})
p = g0["_lancer"](CHILD); time.sleep(0.3)
pol = os.sched_getscheduler(p.pid)
p.kill(); p.wait()
verifie(pol != os.SCHED_FIFO and g0["_rt_state"]() == {"rt_priority": 0, "rt_priority_error": ""},
        "rt_priority 0 asks nothing")
verifie("too old" in load({})["_rt_state"]()["rt_priority_error"], "older orchestrator: it says so")
verifie(load({"rt_priority": 150})["RT_PRIORITY"] == 99 and load({"rt_priority": "x"})["RT_PRIORITY"] == 0,
        "clamped to 0-99")

print("\n4. :8080")
verifie(any("**_rt_state()" in ast.unparse(n) for n in ast.walk(TREE)
            if isinstance(n, ast.FunctionDef) and n.name == "do_GET"), ":8080 publishes the obtained priority")

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
