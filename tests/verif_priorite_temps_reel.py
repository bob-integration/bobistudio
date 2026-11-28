#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Real-time priority given by the PRODUCT (app/realtime.py, decided with Cyril 2026-11-23).

The plugin declares the role of its deadline thread, the core turns it into a grant and a number,
bobimxl applies it and the panel shows what was OBTAINED. Held here, offline:
  1. the grid, and an unknown role refused out loud (never a silent SCHED_OTHER);
  2. the grant: a role brings SYS_NICE + an rtprio limit covering the grid, an explicit limit never
     lowers it, a capability outside the allow-list is never granted;
  3. the number reaches the script: render_config injects it over a persisted value, a type with
     no role is left alone;
  4. bobimxl's helpers, for real when this machine permits: the calling thread only, the highest
     priority obtained and the first refusal published, rt_release drops a helper back;
  5. the controller keeps what :8080 published and the panel never shows the ask as applied;
  6. EVERY plugin: a declared role is a known one, no Python plugin script sets SCHED_FIFO itself
     (bobimxl does), and a hand-written SYS_NICE without a role is a listed, dated exception;
  7. the panel's line and its labels (FR with a no-break space before the colon).

    ./venv/bin/python tests/verif_priorite_temps_reel.py
"""
import ast
import glob
import json
import logging
import os
import re
import sys
import threading
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ko = 0

# Plugins that still ask SYS_NICE by hand, without a role. Each one says why and until when.
HAND_GRANTS = {
    # Its C audio thread applies CONFIG["rt_priority"] itself; the submodule carries another
    # session's uncommitted work (2026-11-23): its role is added with that session.
    "audio_console": "2026-11-23, pending: role « audio » to declare",
}

# Plugins whose script sets SCHED_FIFO itself, because the deadline thread is not one of its own:
# rt_promote acts on the calling thread, these put a CHILD process in FIFO before its exec.
CHILD_FIFO = {
    # The SDK callback threads live in the decklink_rx / decklink_tx binaries (2026-11-23).
    "decklink_io": "preexec_fn of _lancer, at CONFIG[\"rt_priority\"]",
}


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


import tempfile                                                              # noqa: E402
from app import database                                                     # noqa: E402
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_rt_"), "db.sqlite")   # before any access
database.init_db()
from app import container_settings, docker_compute, metrics, plugins, realtime   # noqa: E402

print("\n1. the grid")
verifie(realtime.ROLES == {"audio": 10, "output": 8, "program": 5, "monitoring": 3},
        "audio 10, output 8, program 5, monitoring 3", realtime.ROLES)
verifie(realtime.RTPRIO_LIMIT >= max(realtime.ROLES.values()), "the rtprio limit covers every role")
verifie(realtime.priority_of({"resources": {}}) == 0 and realtime.container_grants({}) == ([], {}),
        "no role: no priority, no grant")


class _Rec(logging.Handler):
    def __init__(self):
        super().__init__(); self.msgs = []

    def emit(self, r):
        self.msgs.append(r.getMessage())


rec = _Rec(); logging.getLogger("app.realtime").addHandler(rec)
verifie(realtime.priority_of({"type": "x", "resources": {"realtime": "programme"}}) == 0
        and any("unknown role" in m for m in rec.msgs), "an unknown role gives nothing, and is logged", rec.msgs)

print("\n2. the grant")
saved = dict(plugins.REGISTRY)
try:
    plugins.REGISTRY["t_role"] = {"type": "t_role", "resources": {"realtime": "audio"}}
    plugins.REGISTRY["t_low"] = {"type": "t_low", "resources": {"realtime": "audio", "cap_add": ["SYS_NICE"],
                                                              "ulimits": {"rtprio": 2}}}
    plugins.REGISTRY["t_bad"] = {"type": "t_bad", "resources": {"cap_add": ["SYS_ADMIN", "SYS_NICE"]}}
    plugins.REGISTRY["t_none"] = {"type": "t_none", "resources": {"cores": 1}}
    verifie(docker_compute.granted_caps("t_role") == (["SYS_NICE"], {"rtprio": "20:20"}),
            "a role brings SYS_NICE and rtprio 20", docker_compute.granted_caps("t_role"))
    verifie(docker_compute.granted_caps("t_low") == (["SYS_NICE"], {"rtprio": "20:20"}),
            "an explicit lower limit never goes below the role's", docker_compute.granted_caps("t_low"))
    verifie(docker_compute.granted_caps("t_bad") == (["SYS_NICE"], {}),
            "a capability outside the allow-list is not granted", docker_compute.granted_caps("t_bad"))
    verifie(docker_compute.granted_caps("t_none") == ([], {}) and docker_compute.granted_caps(None) == ([], {}),
            "nothing asked, nothing granted")
    src = open(os.path.join(ROOT, "app", "docker_compute.py"), encoding="utf-8").read()
    verifie(src.count("_caps, _ul = granted_caps(deploy_type)") == 1 and 'spec["cap_add"] = _caps' in src,
            "ensure_container takes its grant from granted_caps")

    print("\n3. the number reaches the script")
    verifie(plugins.render_config("t_role", {"rt_priority": 0}).get("rt_priority") == 10,
            "render_config injects the grid's number over a persisted value")
    verifie("rt_priority" not in plugins.render_config("t_none", {}) and
            plugins.render_config("t_none", {"rt_priority": 7}).get("rt_priority") == 7,
            "a type with no role is left alone")
finally:
    plugins.REGISTRY.clear(); plugins.REGISTRY.update(saved)

print("\n4. bobimxl's helpers")
tree = ast.parse(open(os.path.join(ROOT, "script_templates", "bobimxl.py"), encoding="utf-8").read())
keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name.startswith("rt_"))
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_RT_STATE" for t in n.targets))]
verifie({n.name for n in keep if isinstance(n, ast.FunctionDef)} >= {"rt_priority", "rt_promote", "rt_release", "rt_state"},
        "rt_priority, rt_promote, rt_release, rt_state defined at module level")


def fresh(fake_os=None):
    err = []
    g = {"os": fake_os or os,
         "sys": types.SimpleNamespace(stderr=types.SimpleNamespace(write=err.append))}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "bobimxl", "exec"), g)
    g["_err"] = err
    return g


g = fresh()
verifie(g["rt_priority"]({"rt_priority": 5}) == 5 and g["rt_priority"]({}) == 0
        and g["rt_priority"]({"rt_priority": "x"}) == 0 and g["rt_priority"]({"rt_priority": 500}) == 99
        and g["rt_priority"](None) == 0, "rt_priority reads CONFIG, 0 by default, clamped to 99")
verifie(g["rt_promote"](0) == 0 and g["rt_state"]() == {"rt_priority": 0, "rt_priority_error": ""},
        "0 asks nothing")


class RefusingOs:
    SCHED_FIFO, SCHED_OTHER = os.SCHED_FIFO, os.SCHED_OTHER
    sched_param = os.sched_param

    def sched_setscheduler(self, pid, pol, prm):
        raise PermissionError(1, "Operation not permitted")


g = fresh(RefusingOs())
verifie(g["rt_promote"](5) == 0 and g["rt_state"]()["rt_priority"] == 0
        and "not permitted" in g["rt_state"]()["rt_priority_error"] and g["_err"],
        "a refusal: 0, reason kept, written to stderr", (g["rt_state"](), g["_err"]))
box = {}


def real():
    g2 = fresh()
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(1))
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    except OSError as e:
        box["skip"] = str(e); return
    box["main_before"] = os.sched_getscheduler(0)
    box["got"] = g2["rt_promote"](3)
    box["self"] = (os.sched_getscheduler(0), os.sched_getparam(0).sched_priority)

    def child():
        box["child"] = os.sched_getscheduler(0)
        g2["rt_release"]()
        box["child_after"] = os.sched_getscheduler(0)
    t = threading.Thread(target=child); t.start(); t.join(5)
    g2["rt_promote"](2)
    box["state"] = g2["rt_state"]()
    g2["rt_release"]()
    box["after"] = os.sched_getscheduler(0)


t = threading.Thread(target=real); t.start(); t.join(10)
if "skip" in box:
    print("  · SCHED_FIFO not permitted here (%s), real check skipped" % box["skip"])
else:
    verifie(box["got"] == 3 and box["self"] == (os.SCHED_FIFO, 3), "the calling thread is FIFO 3", box)
    verifie(os.sched_getscheduler(0) == os.SCHED_OTHER, "the main thread was not touched")
    verifie(box["child"] == os.SCHED_FIFO and box["child_after"] == os.SCHED_OTHER,
            "a helper inherits FIFO, rt_release drops it back", box)
    verifie(box["state"] == {"rt_priority": 3, "rt_priority_error": ""}, "rt_state keeps the highest obtained",
            box["state"])
    verifie(box["after"] == os.SCHED_OTHER, "rt_release on a promoted thread too")

print("\n5. the controller and the panel")
saved_cache = dict(metrics.rt_state_cache)
try:
    metrics.rt_state_cache.clear()
    metrics.note_rt_state(9001, {"fps": 50, "rt_priority": 5, "rt_priority_error": ""})
    verifie(metrics.rt_state_cache.get(9001) == {"priority": 5, "error": ""}, "published value kept")
    metrics.note_rt_state(9001, {"fps": None})
    verifie(9001 in metrics.rt_state_cache, ":8080 silent: state kept")
    metrics.note_rt_state(9001, {"fps": 50})
    verifie(9001 not in metrics.rt_state_cache, "a script that publishes nothing: forgotten")
    saved = dict(plugins.REGISTRY)
    try:
        plugins.REGISTRY["t_role"] = {"type": "t_role", "resources": {"realtime": "program"}}
        plugins.REGISTRY["t_none"] = {"type": "t_none", "resources": {}}
        v = container_settings.realtime_view(9001, "t_role")
        verifie(v == {"role": "program", "asked": 5, "applied": None, "error": ""},
                "role, nothing published yet: applied is None, not the ask", v)
        metrics.note_rt_state(9001, {"fps": 50, "rt_priority": 0, "rt_priority_error": "[Errno 1] nope"})
        v = container_settings.realtime_view(9001, "t_role")
        verifie(v["applied"] == 0 and v["error"] == "[Errno 1] nope", "a refusal is shown as such", v)
        verifie(container_settings.realtime_view(9002, "t_none") is None, "no role, nothing published: no line")
        metrics.note_rt_state(9002, {"fps": 50, "rt_priority": 50})
        v = container_settings.realtime_view(9002, "t_none")
        verifie(v and v["applied"] == 50 and v["role"] is None, "applied without a role is still shown", v)
    finally:
        plugins.REGISTRY.clear(); plugins.REGISTRY.update(saved)
finally:
    metrics.rt_state_cache.clear(); metrics.rt_state_cache.update(saved_cache)
msrc = open(os.path.join(ROOT, "app", "metrics.py"), encoding="utf-8").read()
verifie(re.search(r"m = get_metrics\(ip\)\n    note_running_version\(vmid, _c, m\)\n    note_rt_state\(vmid, m\)", msrc),
        "the poll records it next to the running version")
csrc = open(os.path.join(ROOT, "app", "container_settings.py"), encoding="utf-8").read()
verifie(csrc.index('out["realtime"] = realtime_view(vmid, t)') < csrc.index('if out["engine"]:'),
        "resources_view carries it, engine included")

print("\n6. every plugin")
for mp in sorted(glob.glob(os.path.join(ROOT, "plugins", "*", "plugin.json"))):
    name = os.path.basename(os.path.dirname(mp))
    try:
        man = json.load(open(mp, encoding="utf-8"))
    except ValueError:
        continue
    res = man.get("resources") or {}
    role = res.get("realtime")
    if role is not None:
        verifie(role in realtime.ROLES, "%s: role « %s » is in the grid" % (name, role))
    if "SYS_NICE" in (res.get("cap_add") or []) and role is None:
        verifie(name in HAND_GRANTS, "%s: SYS_NICE by hand without a role is a listed exception" % name)
    sp = os.path.join(os.path.dirname(mp), man.get("script_template") or "script.py")
    if os.path.isfile(sp) and sp.endswith(".py"):
        code = open(sp, encoding="utf-8").read()
        verifie(name in CHILD_FIFO or not re.search(r"os\.sched_setscheduler\([^)]*SCHED_FIFO", code),
                "%s: script does not set SCHED_FIFO itself (bobimxl.rt_promote does)" % name)
for name in HAND_GRANTS:
    mp = os.path.join(ROOT, "plugins", name, "plugin.json")
    if not os.path.isfile(mp):
        continue                                     # absent from this tree (public repository)
    man = json.load(open(mp, encoding="utf-8"))
    verifie((man.get("resources") or {}).get("realtime") is None,
            "%s: exception still needed (remove it from HAND_GRANTS once the role is declared)" % name)

print("\n7. panel and labels")
js = open(os.path.join(ROOT, "static", "js", "container_panel.js"), encoding="utf-8").read()
verifie("if (rv.realtime) kids.push(realtimeLine(rv.realtime));" in js and "function realtimeLine(rt)" in js,
        "the resources block shows the line")
body = js[js.index("function realtimeLine(rt)"):js.index("function dedicatedCores(")]
verifie(body.index("rt.applied)") < body.index("rt.error"), "applied first, then a refusal, then the rest")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    for k in ("res_rt_applied", "res_rt_refused", "res_rt_not_applied", "res_rt_unknown"):
        verifie(("containers.panel." + k) in cat and "{prio}" in cat["containers.panel." + k],
                "%s: containers.panel.%s" % (lang, k))
    if lang == "fr":
        verifie(all(" :" not in cat["containers.panel." + k] for k in ("res_rt_applied", "res_rt_refused")),
                "fr: no plain space before a colon")

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
