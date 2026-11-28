#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Deadline witness (P1 of the resource guarantee, 2026-11-23).

Decided with Cyril: one block, `deadline`, published on :8080 by every plugin with a cadence; a
deadline is missed when a cycle's output is ready after it or a slot is skipped; the controller
keeps 24 h per instance IN THE DATABASE; a Dedicated container is guaranteed only with its cores,
a witness, and nothing missed in 24 h — no witness = not guaranteed. Held here, offline:
  1. bobimxl.DeadlineWitness: lateness, missed in the window (late + skipped), sliding window,
     total, p99.9;
  2. app/deadlines.note: first poll = baseline, then deltas, a restarted script counts again, a new
     instance is a new baseline; one row per instance and hour; alerts once on transition;
  3. the 24 h: older rows ignored, survives a fresh process (read back from the base);
  1b/2b. STARTUP (2026-11-24): nothing counts before the first cycle on time (capped); a
     redeploy/restart marks the container, and while it starts neither its misses nor those of
     the wall that reads it count or alert; the mark ends on a witness born after it, or after
     the grace;
  4. the verdict: no witness / missed in 24 h / clean; project_guarantee applies it to Dedicated
     members whose cores are guaranteed, and only to them; the metrics poll feeds it; the page
     and both languages know the two states.

MUTATIONS, TE_MUTATION=compte_demarrage | mur_compte — each must give rc=1.

    ./venv/bin/python tests/verif_temoin_echeance.py
"""
import ast
import json
import os
import sys
import tempfile
import time
import types
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


from app import database                                                     # noqa: E402
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_temoin_"), "db.sqlite")   # before any access
database.init_db()

print("\n1. bobimxl.DeadlineWitness")
import numpy as np                                                           # noqa: E402
MUT = os.environ.get("TE_MUTATION")
_bsrc = open(os.path.join(ROOT, "script_templates", "bobimxl.py"), encoding="utf-8").read()
if MUT == "compte_demarrage":
    assert _bsrc.count("        if self._warming(late <= 0):\n") == 1, "mutation: pattern stale"
    _bsrc = _bsrc.replace("        if self._warming(late <= 0):\n", "        if False:\n")
    print("MUTATION active: compte_demarrage")
tree = ast.parse(_bsrc)
cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DeadlineWitness"]
verifie(len(cls) == 1, "the class exists")
clock = types.SimpleNamespace(t=1000.0)
fake_time = types.SimpleNamespace(monotonic=lambda: clock.t)
g = {"np": np, "deque": deque, "time": fake_time, "now_tai": lambda: 0}
cap = [n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "STARTUP_MAX_S" for t in n.targets)]
exec(compile(ast.Module(body=cap + cls, type_ignores=[]), "bobimxl", "exec"), g)
W = g["DeadlineWitness"]
w = W(0.02, window_s=1)                                       # 50 cycles in the window
MS = 1_000_000
for k in range(50):
    w.done(k * 20 * MS, now_ns=k * 20 * MS - 5 * MS)          # 5 ms early each time
r = w.report()
verifie(r["cycles"] == 50 and r["missed"] == 0 and r["missed_total"] == 0 and r["late_p999_ms"] == -5.0
        and r["period_ms"] == 20.0 and r["window_s"] == 1, "on time: nothing missed, 5 ms of margin", r)
clock.t += 2                                                 # past the 1 s report cache
w.done(0, now_ns=3 * MS)                                      # 3 ms late
w.skipped(2)
r = w.report()
verifie(r["missed"] == 3 and r["missed_total"] == 3 and r["late_max_ms"] == 3.0,
        "a late output and two skipped slots: 3 missed", r)
verifie(r["late_p999_ms"] > 0, "p99.9 shows the late cycle", r["late_p999_ms"])
clock.t += 0.5
r2 = w.report()
verifie(r2 is r, "report cached within the second")
clock.t += 1.0
for k in range(50):
    w.done(0, now_ns=-1 * MS)
r = w.report()
verifie(r["missed"] == 0 and r["missed_total"] == 3, "window slid past both: 0 in the window, 3 in total", r)
verifie(W(0.02).report()["late_p999_ms"] is None, "no cycle yet: no percentile")

print("\n1b. startup is not a missed deadline")
w = W(0.02, window_s=1)
w.done(0, now_ns=1413 * MS)                                   # first frame 1.41 s late (shard 1345)
w.skipped(71)
r = w.report()
verifie(r["starting"] and r["missed_total"] == 0 and r["missed"] == 0 and r["cycles"] == 0
        and r["startup_missed"] == 72, "★ the first frame and its 71 slots are a startup, not misses", r)
clock.t += 2
w.done(0, now_ns=-2 * MS)                                     # first cycle on time: started
w.done(0, now_ns=1 * MS)                                      # late afterwards: counts
r = w.report()
verifie(not r["starting"] and r["missed_total"] == 1 and r["cycles"] == 2 and r["startup_ms"] == 2000.0,
        "after the first cycle on time, a late one counts", r)
w2 = W(0.02, window_s=1, startup_max_s=15)
clock.t += 16
w2.done(0, now_ns=5 * MS)
w2.skipped(1)
r = w2.report()
verifie(not r["starting"] and r["missed_total"] == 2, "never on time: counted after the cap", r)

print("\n2. controller: note()")
from app import deadlines                                                    # noqa: E402
alerts = []
deadlines.db_add_alert = lambda key, level="info", **k: alerts.append((key, level, k.get("params")))
C = {"vmid": 701, "hostname": "mix", "instance_uuid": "u-701", "node_id": 1}


def poll(total, window=0, cont=C, late=1.5):
    deadlines.note(cont["vmid"], cont, {"fps": 50, "deadline": {
        "period_ms": 20.0, "window_s": 60, "missed": window, "missed_total": total, "late_max_ms": late}})


def rows(uuid="u-701"):
    return database.get_db().execute("SELECT hour, missed FROM deadline_hours WHERE instance_uuid = ?",
                                     (uuid,)).fetchall()


poll(40, window=40)
verifie(deadlines.missed_24h("u-701")[0] == 0 and len(rows()) == 1 and not alerts,
        "first poll = baseline: hour marked watched, history not counted, no alert", (rows(), alerts))
poll(40)
verifie(deadlines.missed_24h("u-701") == (0, 1), "nothing new, no write", deadlines.missed_24h("u-701"))
poll(43, window=3)
verifie(deadlines.missed_24h("u-701")[0] == 3, "3 new misses recorded")
verifie([a[0] for a in alerts] == ["alert.deadline.missed"] and alerts[0][2]["n"] == 3,
        "one alert when they start", alerts)
poll(45, window=5)
verifie(len(alerts) == 1 and deadlines.missed_24h("u-701")[0] == 5, "no second alert while it lasts")
poll(2, window=2)
verifie(deadlines.missed_24h("u-701")[0] == 7, "script restarted (total went down): its 2 count", deadlines.missed_24h("u-701"))
poll(2, window=0)
verifie([a[0] for a in alerts] == ["alert.deadline.missed", "alert.deadline.recovered"],
        "one alert when a window is clean again", alerts)
C2 = dict(C, instance_uuid="u-702")
poll(99, window=9, cont=C2)
verifie(deadlines.missed_24h("u-702")[0] == 0 and deadlines.missed_24h("u-701")[0] == 7,
        "same vmid, new instance: a new baseline, the old one keeps its own")
deadlines.note(701, C2, {"fps": 50})
verifie(deadlines.state(701) is None, "a script that stops publishing is no longer watched")
deadlines.note(701, C2, {"fps": None})
verifie(deadlines.state(701) is None, "silent :8080 changes nothing")
deadlines.note(702, dict(C, vmid=702, instance_uuid="u-x"), {"fps": 50, "deadline": {"missed_total": "zz"}})
verifie(deadlines.state(702) is None, "unreadable block ignored")

print("\n2b. a redeploy: neither the shard nor its wall count its startup")
from app import compositor_fabric                                           # noqa: E402
if MUT == "mur_compte":
    compositor_fabric.shards_par_parent = lambda: {}
    print("MUTATION active: mur_compte")
else:
    compositor_fabric.shards_par_parent = lambda: {900: [901]}
said = []
deadlines.db_add_alert = lambda key, level="info", **k: said.append(key)
CW = {"vmid": 900, "hostname": "wall", "instance_uuid": "u-900"}
CS = {"vmid": 901, "hostname": "shard", "instance_uuid": "u-901"}


def poll2(cont, total, window=0, **extra):
    blk = {"period_ms": 20.0, "window_s": 60, "missed": window, "missed_total": total, "late_max_ms": 9.0}
    blk.update(extra)
    deadlines.note(cont["vmid"], cont, {"fps": 50, "deadline": blk})


poll2(CW, 10)
poll2(CS, 10, starting=False, age_s=500.0)                      # baselines
deadlines.mark_starting(901)
poll2(CW, 12, window=2)                                       # the wall misses 2 while 901 restarts
verifie(deadlines.missed_24h("u-900")[0] == 0 and not said,
        "★ the wall's misses during its shard's startup: not counted, no alert", (deadlines.missed_24h("u-900"), said))
poll2(CS, 11, window=1, starting=False, age_s=500.0)          # the OLD script still answers
verifie(deadlines.is_starting(901) and deadlines.missed_24h("u-901")[0] == 0,
        "the old script answering does not end the startup, its own miss not counted")
with deadlines._lock:
    deadlines._starting[901] -= 10                            # the redeploy began 10 s ago
poll2(CS, 0, starting=False, age_s=12.0)                        # witness older than the mark: no
verifie(deadlines.is_starting(901), "a witness older than the mark does not end it")
poll2(CS, 0, starting=True, age_s=3.0)                          # the new witness, still starting
verifie(deadlines.is_starting(901), "a new witness still starting does not end it")
poll2(CS, 0, starting=False, age_s=3.0)                         # the new witness, done starting
verifie(not deadlines.is_starting(901), "a witness born after the mark and started ends it")
poll2(CW, 14, window=2)
verifie(deadlines.missed_24h("u-900")[0] == 2 and said == ["alert.deadline.missed"],
        "after the startup, the wall's misses count again", (deadlines.missed_24h("u-900"), said))
deadlines.mark_starting(901)
with deadlines._lock:
    deadlines._starting[901] -= deadlines.STARTUP_GRACE_S + 1
verifie(not deadlines.is_starting(901), "past the grace, no excuse")
for f, needle in (("deploy.py", "deadlines.mark_starting(vmid)"), ("containers.py", "deadlines.mark_starting(vmid)")):
    verifie(needle in open(os.path.join(ROOT, "app", f), encoding="utf-8").read(), "%s marks the start" % f)
dsrc = open(os.path.join(ROOT, "app", "deploy.py"), encoding="utf-8").read()
_i = dsrc.index("def deployer_script(")
_body = dsrc[_i:dsrc.index("\ndef ", _i + 10)]
verifie(_body.index("if not hot_only_keys:") < _body.index("deadlines.mark_starting(vmid)"),
        "a hot-only push (no restart) does not mark a start")

print("\n3. 24 h, kept in the base")
db = database.get_db()
old = time.strftime("%Y-%m-%dT%H", time.localtime(time.time() - 30 * 3600))
db.execute("INSERT INTO deadline_hours (instance_uuid, hour, vmid, missed) VALUES ('u-old', ?, 9, 50)", (old,))
db.commit()
verifie(deadlines.missed_24h("u-old") == (0, 0), "a miss 30 h ago is out of the 24 h")
deadlines.purge(days=1)
verifie(not db.execute("SELECT 1 FROM deadline_hours WHERE instance_uuid='u-old'").fetchone(), "purged after a day")
import importlib                                                             # noqa: E402
importlib.reload(deadlines)
verifie(deadlines.missed_24h("u-701")[0] == 7, "a fresh process reads the 24 h back from the base")

print("\n4. verdict")
deadlines.db_add_alert = lambda *a, **k: None
cont = {"vmid": 801, "hostname": "pgm", "instance_uuid": "u-801"}
verifie(deadlines.verdict_of(cont) == "deadline_unwatched", "no witness: not guaranteed")
deadlines.note(801, cont, {"fps": 50, "deadline": {"missed": 0, "missed_total": 0}})
verifie(deadlines.verdict_of(cont) is None, "a clean witness says nothing against")
deadlines.note(801, cont, {"fps": 50, "deadline": {"missed": 1, "missed_total": 1}})
verifie(deadlines.verdict_of(cont) == "deadline_missed", "one miss in 24 h: not guaranteed")
from app import etalonnage, project_resources                              # noqa: E402
pid = db.execute("INSERT INTO projects (name, snapshot) VALUES ('Atelier', '[]')").lastrowid
db.commit()
for v, lvl in ((801, "dedicated"), (802, "dedicated"), (803, "normal")):
    db.execute("INSERT INTO containers (vmid, hostname, status, instance_uuid, deploy_config) VALUES "
               "(?, ?, 'running', ?, ?)", (v, "c%d" % v, "u-%d" % v, json.dumps({"type": "mixer", "params": {}})))
    database.db_set_project(v, pid)
db.commit()
G = {"reserve_pct": 200, "etat": "etalonne", "besoin_pct": 100}
lines = {801: dict(G, vmid=801, level="dedicated"), 802: dict(G, vmid=802, level="dedicated"),
         803: dict(G, vmid=803, level="normal")}
etalonnage.ressources = lambda vmids=None, **k: [r for v, r in lines.items() if vmids is None or v in vmids]
rp = project_resources.project_guarantee(pid)
st = {c["vmid"]: c["guarantee"] for c in rp["containers"]}
verifie(st == {801: "deadline_missed", 802: "deadline_unwatched", 803: "guaranteed"}
        and rp["verdict"] == "not_guaranteed" and rp["counts"]["deadline_missed"] == 1
        and rp["counts"]["deadline_unwatched"] == 1,
        "Dedicated members judged on their deadlines, a best-effort one is not", (st, rp["verdict"]))
lines[802] = dict(G, vmid=802, level="dedicated", reserve_pct=None)
st = {c["vmid"]: c["guarantee"] for c in project_resources.project_guarantee(pid)["containers"]}
verifie(st[802] == "shared", "cores missing comes first: shared, not « unwatched »", st)
verifie(list(project_resources.GUARANTEE_STATES).index("deadline_unwatched")
        < list(project_resources.GUARANTEE_STATES).index("guaranteed"), "states ordered by severity")

from app import container_settings                                          # noqa: E402
v = container_settings.deadline_view({"vmid": 801, "instance_uuid": "u-801"})
verifie(v and v["missed_24h"] == 1 and v["missed"] == 1, "panel: 24 h and window read for the container", v)
verifie(container_settings.deadline_view({"vmid": 802, "instance_uuid": "u-802"}) is None, "panel: no witness, no line")

print("\n5. wiring")
src = open(os.path.join(ROOT, "app", "metrics.py"), encoding="utf-8").read()
fn = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "rafraichir_metrics"]
verifie(fn and "deadlines.note(vmid, _c, m)" in ast.unparse(fn[0]), "the metrics poll feeds it")
page = open(os.path.join(ROOT, "templates", "projects.html"), encoding="utf-8").read()
for s in ("deadline_missed", "deadline_unwatched"):
    verifie(page.count(s) >= 2, "projects page: badge and detail for %s" % s)
    for lang in ("fr", "en"):
        cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
        verifie(cat.get("projects.res_state." + s) and cat.get("projects.res_count." + s),
                "%s: state and count of %s" % (lang, s))
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    for k, p in (("alert.deadline.missed", {"h": "x", "vmid": 1, "n": 2, "late": 1.0, "period": 20.0}),
                 ("alert.deadline.recovered", {"h": "x", "vmid": 1, "window": 60})):
        try:
            cat[k].format(**p)
            good = True
        except Exception as e:                                               # noqa: BLE001
            good = e
        verifie(good is True, "%s: %s renders" % (lang, k), good)
verifie(" :" not in json.load(open(os.path.join(ROOT, "i18n", "fr.json"), encoding="utf-8"))["alert.deadline.missed"],
        "fr: no-break space before the colon")

print("\n6. plugins: a role (a deadline thread) means a witness")
# Cadence plugins without a real-time role that can be set Dedicated: no witness = never guaranteed.
ALSO = {"split", "color_corrector", "delay"}
# Plugins that do not publish it yet, each with why and until when.
PENDING = {
    "decklink_io": "2026-11-23: the A/B on the driver Oops (FIFO vs RT kernel) comes first",
    "audio_console": "2026-11-23: submodule carries another session's uncommitted work",
}
for mp in sorted(__import__("glob").glob(os.path.join(ROOT, "plugins", "*", "plugin.json"))):
    name = os.path.basename(os.path.dirname(mp))
    man = json.load(open(mp, encoding="utf-8"))
    if not ((man.get("resources") or {}).get("realtime") or name in ALSO):
        continue
    if name in PENDING:
        print("  · %s: pending (%s)" % (name, PENDING[name]))
        continue
    sp = os.path.join(os.path.dirname(mp), man.get("script_template") or "script.py")
    code = open(sp, encoding="utf-8").read().format(config="{}", hostname="h", plugin_version="t")
    t = ast.parse(code)
    mk = [n for n in ast.walk(t) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "DeadlineWitness"]
    guarded = "hasattr(bobimxl, \"DeadlineWitness\")" in code or "hasattr(bobimxl, 'DeadlineWitness')" in code
    feeds = [n for n in ast.walk(t) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("done", "skipped")
             and "dw" in ast.unparse(n.func.value).lower()]
    verifie(len(mk) == 1 and guarded and feeds and "report()" in code and "\"deadline\"" in code,
            "%s: one witness, guarded, fed, published as « deadline »" % name, (len(mk), guarded, len(feeds)))
for name in PENDING:
    if os.path.isdir(os.path.join(ROOT, "plugins", name)):
        code = open(os.path.join(ROOT, "plugins", name, "script.py"), encoding="utf-8").read()
        verifie("DeadlineWitness" not in code, "%s: still pending (remove it from PENDING once done)" % name)

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
