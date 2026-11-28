#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The node's real-time aptitude sheet (P4, decided with Cyril on 2026-11-23).

A reservation proves nothing about the machine under it: the sheet MEASURES the kernel, the
frequency obtained under work, the governor, HT siblings of dedicated cores, the real-time
throttling, the interrupts received by dedicated cores, the memory bandwidth — one line each, with
what to do. A red line makes the node unfit, which the project verdict says. Held here, offline:
  1. the PROBE runs for real in bash on this machine and parses (kernel, rt, governor, window);
  2. evaluate: frequency against the NOMINAL (base, else the model's « @ x GHz », never the turbo
     maximum when a nominal is known); HT siblings held by another or left to the shared pool;
     interrupts counted only on dedicated cores; DeckLink keeps the normal kernel without a warning;
  3. measure: the busy loop goes to a SHARED core, the sheet is stored, an alert is raised once when
     a line turns red and once when it is back — never repeated;
  4. the project verdict knows `node_unfit`; routes, node_health hook, sheet and labels.

MUTATIONS, APT_MUTATION=<name> — each must give rc=1:
    turbo       the frequency is judged against the turbo maximum
    irq_tous    interrupts are counted on every core
    repete      the red-line alert is raised at every measure
    dedie       the busy loop may go to a dedicated core
    rafales     the burst measure is ignored
    frere       the busy loop may go to the HT sibling of a dedicated core
    ht_naif     a sibling nobody reserves is the pool's, even one the pool closes (2026-11-25)
    ht_mesure   measure does not give evaluate the shared pool
    irq_geres   a kernel-managed IRQ is taken for a movable one (no boot parameter named)
    moteur      IRQs on the 2110 engine's main / service CPUs count as on a dedicated core

    ./venv/bin/python tests/verif_aptitude_noeud.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_aptitude_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import node_aptitude as na                                           # noqa: E402

MUT = os.environ.get("APT_MUTATION") or ""
MUTATIONS = {
    "turbo": ('nominal = facts.get("fbase_mhz") or facts.get("fmodel_mhz") or facts.get("fmax_mhz") or 0',
              'nominal = facts.get("fmax_mhz") or 0'),
    "irq_tous": ("            v = alloc.get(q[\"cpu\"])\n            if v is None:\n                continue\n",
                 "            v = alloc.get(q[\"cpu\"], 0)\n"),
    "repete": ('if l["level"] == "bad" and not _alerted.get(k):', 'if l["level"] == "bad":'),
    "rafales": ("        low = min(fwork, fburst) if fburst else fwork\n", "        low = fwork\n"),
    "frere": ("        return (quiet or libres)[-1]\n", "        return libres[-1]\n"),
    "ht_naif": ("                elif shared is None or sib in shared:\n", "                elif True:\n"),
    "ht_mesure": ("                         shared=shared, pool=pool,", "                         shared=None, pool=pool,"),
    "irq_geres": ("    for prefix, (mod, managed_value, token) in MANAGED_IRQ_DRIVERS.items():",
                  "    for prefix, (mod, managed_value, token) in ():"),
    "moteur": ("            if v in engines and isolated and q[\"cpu\"] not in isolated:\n",
               "            if False:\n"),
    "dedie": ("        libres, _ = core_pool.partage_disponible(node_id)\n",
              "        libres = core_pool.parse_cpuset((db_get_node(node_id) or {}).get(\"compute_cpuset\") or \"\")\n"),
}
if MUT:
    if MUT not in MUTATIONS:
        print("unknown mutation:", MUT)
        sys.exit(2)
    _old, _new = MUTATIONS[MUT]
    _src = open(na.__file__, encoding="utf-8").read()
    if _src.count(_old) != 1:
        print("mutation %s: pattern found %d times — the guard is stale" % (MUT, _src.count(_old)))
        sys.exit(1)
    exec(compile(_src.replace(_old, _new), na.__file__, "exec"), na.__dict__)
    print("MUTATION active:", MUT)

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


def line(lines, key):
    return next((l for l in lines if l["key"] == key), {})


print("\n1. The probe, for real")
r = subprocess.run(["bash", "-c", na.PROBE % {"core": ""}], capture_output=True, text=True, timeout=30)
f = na.parse(r.stdout)
verifie(r.returncode == 0 and "kernel" in f and "rt_runtime" in f and "gov_total" in f
        and "window_ms" in f and f["window_ms"] >= 900, "bash runs it; kernel, rt, governor, window parsed",
        (r.returncode, r.stderr[-300:], {k: f.get(k) for k in ("kernel", "rt_runtime", "gov_total", "window_ms")}))

import shutil                                                                 # noqa: E402
if shutil.which("taskset") and "cpu MHz" in open("/proc/cpuinfo").read():
    _c = os.cpu_count() - 1
    r = subprocess.run(["bash", "-c", na.PROBE % {"core": _c}], capture_output=True, text=True, timeout=30)
    f = na.parse(r.stdout)
    verifie(f.get("fwork_mhz") and f.get("fburst_mhz") and f.get("fwork_core") == _c,
            "with a core: full and burst frequencies measured, on that core",
            {k: f.get(k) for k in ("fwork_core", "fwork_mhz", "fburst_mhz")})
else:
    print("  SKIP(taskset or cpu MHz missing): the busy part of the probe is not run here")

print("\n2. Evaluation")
CANNED = """kernel 6.12.96+deb13-amd64 0 0
rt 950000 1000000
governor 8 8
fmax 3600000 0
model 2.20
decklink 0
fwork 7 2800.4
window_ms 1000
irqd 141 2 40 megasas0-msix3
irqd 142 5 7 eth0-rx
irqd 0 9 1 timer
"""
f = na.parse(CANNED)
ALLOC = {2: 901, 3: 901, 4: 902}                     # cpu → vmid
CORE_OF = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 2, 7: 3, 8: 4, 9: 9}  # 6/7/8 = HT siblings of 2/3/4
L = na.evaluate(1, f, alloc=ALLOC, core_of=CORE_OF, membw={"gbps": 40, "ratio": 1.0, "level": None})
fl = line(L, "freq")
verifie(fl.get("nominal") == 2200 and fl.get("level") == "ok" and fl.get("pct") == 127,
        "★ 2800 MHz under work for 2200 nominal (turbo 3600): ok", fl)
f2 = dict(f, fwork_mhz=1900)
verifie(line(na.evaluate(1, f2), "freq").get("level") == "bad", "1900 for 2200 (86 %): red")
verifie(line(na.evaluate(1, dict(f, fwork_mhz=2100)), "freq").get("level") == "warn", "2100 (95 %): warning")
verifie(line(na.evaluate(1, dict(f, fbase_mhz=2400)), "freq").get("nominal") == 2400, "base_frequency wins")
fb = line(na.evaluate(1, dict(f, fburst_mhz=1200)), "freq")
verifie(fb.get("level") == "bad" and fb.get("value") == 1200 and fb.get("full") == 2800,
        "★ in bursts at 1200 while 2800 fully loaded (r620-2): red, judged on the lower", fb)
verifie(line(L, "kernel").get("level") == "warn" and line(L, "kernel").get("action") == "rt_kernel",
        "normal kernel: warning, with its action")
verifie(line(na.evaluate(1, f, decklink_node=True), "kernel").get("level") == "ok",
        "normal kernel with a DeckLink card: not a warning")
verifie(line(na.evaluate(1, dict(f, preempt_rt=True)), "kernel").get("rt") is True, "PREEMPT_RT seen")
ht = line(L, "ht")
verifie(ht.get("level") == "warn" and ht.get("pool") == [901, 902] and ht.get("other") == [],
        "★ siblings left to the shared pool are named", ht)
L2 = na.evaluate(1, f, alloc={2: 901, 6: 902}, core_of=CORE_OF)
verifie(line(L2, "ht").get("other") == [901, 902], "siblings held by another reservation", line(L2, "ht"))
verifie(line(na.evaluate(1, f, alloc={2: 901, 6: 901}, core_of=CORE_OF), "ht").get("level") == "ok",
        "a container holding both siblings: fine")
irq = line(L, "irq")
verifie(irq.get("value") == 40 and irq.get("vmids") == [901] and "megasas0-msix3 (40/s)" in irq.get("names", []),
        "★ only interrupts received by dedicated cores count (40/s on 901)", irq)
verifie(line(na.evaluate(1, f, alloc={}), "irq").get("level") == "ok", "no dedicated core: no interrupt counted")
# 2026-11-25: what the old reading got wrong on all three nodes. CPU map: 0/10, 1/11, 2/12, 3/13.
CO2 = {0: 0, 10: 0, 1: 1, 11: 1, 2: 2, 12: 2, 3: 3, 13: 3}
AL2 = {10: 905, 1: 906, 2: 907, 3: 908}       # 905: twin 0 is the system's; 908: a 2110 engine
L3 = na.evaluate(1, f, alloc=AL2, core_of=CO2, shared={11}, pool={1, 2, 3, 10, 11, 12, 13}, engines={908})
h3 = line(L3, "ht")
verifie(h3.get("pool") == [906] and h3.get("system") == [905] and h3.get("other") == [],
        "★ twin in the shared pool → pool; twin of CPU 0 → system; twin the pool CLOSES and the "
        "engine's own twin → not shared", h3)
verifie(h3.get("action") == "ht", "pool sibling → action ht")
h4 = line(na.evaluate(1, dict(f, isolated=[0]), alloc={10: 905, 2: 907}, core_of=CO2, shared=set(),
                      pool={1, 2, 3, 10, 11, 12, 13}), "ht")
verifie(h4.get("level") == "ok" and h4.get("value") == 0, "★ closed twin + isolated system twin: ok", h4)
h5 = line(na.evaluate(1, f, alloc={10: 905}, core_of=CO2, shared=set(), pool={10, 11}), "ht")
verifie(h5.get("level") == "warn" and h5.get("action") == "ht_system", "system twin only: action ht_system", h5)
IRQS = """kernel 6.12.116+deb13-rt-amd64 1 1
window_ms 1000
isolated 5
irqmod megaraid_sas 1
irqmod smartpqi 0
irqd 56 2 19 megasas0-msix13
irqd 77 3 5 eno3-tx
irqd 90 6 7 smartpqi
irqd 91 5 3 smartpqi
irqd 120 2 2 nvme0q3
"""
fi = na.parse(IRQS)
verifie(fi.get("isolated") == [5] and fi.get("irqmod", {}).get("megaraid_sas") == "1", "probe: isolated band, module parameters", fi)
q = line(na.evaluate(1, fi, alloc={2: 901, 3: 902, 5: 908, 6: 908}, engines={908}, unmovable={120}), "irq")
verifie(q.get("action") == "irq" and q.get("movable") == ["eno3-tx (5/s)"], "★ a movable IRQ on a dedicated core: action irq", q)
verifie("megasas0-msix13 (19/s)" in q.get("managed", []) and "megaraid_sas.smp_affinity_enable=0" in q.get("cmdline", []),
        "★ megasas managed (smp_affinity_enable=1): its boot parameter is named", q)
verifie("isolcpus=managed_irq,2" in q.get("cmdline", []), "managed without a driver parameter (agent: unmovable): isolcpus=managed_irq", q)
verifie("smartpqi (3/s)" in q.get("names", []) and "smartpqi (7/s)" not in q.get("names", [])
        and "smartpqi.disable_managed_interrupts=1" in q.get("cmdline", []),
        "★ engine: its isolated lcore counts, its service CPU does not; smartpqi parameter named", q)
q2 = line(na.evaluate(1, fi, alloc={2: 901}), "irq")
verifie(q2.get("action") == "irq_managed" and q2.get("movable") == [], "only managed ones: action irq_managed", q2)
q3 = line(na.evaluate(1, dict(fi, irqmod={"megaraid_sas": "0"}), alloc={2: 901}), "irq")
verifie(q3.get("action") == "irq" and "megasas0-msix13 (19/s)" in q3.get("movable", []),
        "megasas with smp_affinity_enable=0: an ordinary, movable IRQ", q3)
verifie(line(L, "governor").get("level") == "ok", "8 of 8 in performance")
verifie(line(na.evaluate(1, dict(f, gov_perf=6)), "governor").get("level") == "bad", "2 of 8 off: red")
verifie(line(L, "rt").get("level") == "ok" and line(na.evaluate(1, dict(f, rt_runtime=500000)), "rt").get("level") == "warn",
        "rt throttling: 95 % fine, 50 % warned")
verifie(line(L, "membw").get("level") == "ok"
        and line(na.evaluate(1, f, membw={"gbps": 9, "level": "warning"}), "membw").get("level") == "warn"
        and line(na.evaluate(1, f), "membw").get("level") == "unknown", "membw: ok / warn / not measured")
verifie(na.verdict(L) == "fit_with_warnings" and na.verdict(na.evaluate(1, f2)) == "unfit"
        and na.verdict([]) == "unknown", "verdict")

print("\n3. Measure")
NODE = database.db_add_node("apt-x", "127.0.0.1")
database.db_update_node(NODE, compute_cpuset="0-9")
with database.get_db() as db:
    # cpu 9, the last of the pool, is dedicated too: a busy loop sent to « the last core » would hit it.
    for cpu, v in list(ALLOC.items()) + [(9, 903)]:
        db.execute("INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?, ?, ?)", (NODE, cpu, v))
    db.commit()
from app import node_driver, core_pool                                        # noqa: E402
sent, out = [], [CANNED]
node_driver.host_exec = lambda node, cmd, timeout=None, **k: (sent.append(cmd) or (0, out[0], ""))
node_driver.node_capabilities = lambda n: []
core_pool.core_map_cached = lambda nid: CORE_OF
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)
core_pool.reserve_physique = lambda node_id=None: False
sh = na.measure(NODE)
m = re.search(r"^c=(\d*)$", sent[0], re.M) if sent else None
verifie(m and m.group(1) and int(m.group(1)) not in list(ALLOC) + [9], "★ the busy loop goes to a shared core",
        m.group(1) if m else sent[:1])
# Shared cores of the pool: 6, 7, 8 — siblings of the dedicated 2, 3, 4. Shared with a shared
# sibling: none here but 5 (alone on its physical core) → it must be chosen over 8.
verifie(m and m.group(1) == "5", "★ never the HT sibling of a dedicated core when another is free", m.group(1) if m else None)
verifie(sh and na.get(NODE) and na.get(NODE)["verdict"] == "fit_with_warnings", "stored and read back")
verifie(line(sh["lines"], "ht").get("pool") == [901, 902], "siblings 6-8 in the shared pool: named", line(sh["lines"], "ht"))
core_pool.reserve_physique = lambda node_id=None: True       # « dedicated = physical core » on this node
sh2 = na.measure(NODE)
verifie(line(sh2["lines"], "ht").get("level") == "ok",
        "★ whole-core reservation: the twins the pool closes are not reported as shared", line(sh2["lines"], "ht"))
core_pool.reserve_physique = lambda node_id=None: False


# Count the CALLS: db_add_alert's own anti-bounce would hide a repeated alert from the table.
_calls = []
na.db_add_alert = lambda key, *a, **k: _calls.append(key)


def alerts(key):
    return _calls.count(key)


out[0] = CANNED.replace("fwork 7 2800.4", "fwork 7 1500")
na.measure(NODE)
na.measure(NODE)
verifie(alerts("alert.node.aptitude_bad") == 1, "★ a red line alerts ONCE", alerts("alert.node.aptitude_bad"))
verifie(na.unfit_lines(NODE) == ["freq"], "unfit_lines names it")
out[0] = CANNED
na.measure(NODE)
na.measure(NODE)
verifie(alerts("alert.node.aptitude_recovered") == 1 and na.unfit_lines(NODE) == [],
        "back to fine: one alert")
na.refresh_due  # exists
verifie(na.refresh_due(NODE) is False, "a fresh sheet is not measured again")

print("\n4. Verdict, routes, sheet, labels")
from app import project_resources                                             # noqa: E402
gs = project_resources.GUARANTEE_STATES
verifie("node_unfit" in gs and gs.index("node_unfit") < gs.index("deadline_missed"), "node_unfit state, before deadlines")
pr = open(os.path.join(ROOT, "app", "project_resources.py"), encoding="utf-8").read()
verifie('r["node_unfit"] = node_aptitude.unfit_lines(c["node_id"])' in pr
        and pr.find("node_aptitude.unfit_lines") < pr.find("deadlines.verdict_of(c)"), "project verdict reads the sheet")
rs = open(os.path.join(ROOT, "app", "routes", "node_sheet.py"), encoding="utf-8").read()
verifie('"/api/nodes/<int:node_id>/aptitude"' in rs and '"/api/nodes/<int:node_id>/aptitude/measure"' in rs
        and '@require_perm("settings.edit")\ndef api_node_aptitude_measure' in rs, "routes (measure needs settings.edit)")
nh = open(os.path.join(ROOT, "app", "node_health.py"), encoding="utf-8").read()
verifie("node_aptitude.refresh_due(nid)" in nh, "node_health refreshes it")
js = open(os.path.join(ROOT, "static", "node_sheet.js"), encoding="utf-8").read()
verifie("section('aptitude'" in js and "loadAptitude(id, false)" in js and "class=\"ctl-push\" data-ns=\"apt-measure\"" in js,
        "sheet section with the shared push control")
ph = open(os.path.join(ROOT, "templates", "projects.html"), encoding="utf-8").read()
verifie("node_unfit: 'stopped'" in ph and re.search(r"const detail = \[[^\]]*'node_unfit'", ph), "Projects → Resources shows it")
keys = (["settings.node_sheet.sec_aptitude", "projects.res_state.node_unfit", "projects.res_count.node_unfit",
         "alert.node.aptitude_bad", "alert.node.aptitude_recovered"]
        + ["settings.node_sheet.apt_" + k for k in ("kernel", "freq", "governor", "ht", "rt", "irq", "membw")]
        + ["settings.node_sheet.apt_action_" + a for a in ("rt_kernel", "bios", "governor", "ht", "rt_runtime", "irq", "membw",
                                                            "ht_system", "irq_managed")]
        + ["settings.node_sheet.apt_verdict_" + v for v in ("fit", "fit_with_warnings", "unfit", "unknown")]
        + ["settings.node_sheet." + k for k in re.findall(r"T\('(apt_\w+)'", js) if not k.endswith("_")])
src = open(na.__file__, encoding="utf-8").read()
actions = set(re.findall(r'action="(\w+)"', src)) | set(re.findall(r'act = "(\w+)"', src)) \
    | set(re.findall(r'"(irq_managed)"', src))
verifie(actions <= {"rt_kernel", "bios", "governor", "ht", "rt_runtime", "irq", "membw", "ht_system", "irq_managed"}
        and {"ht_system", "irq_managed"} <= actions, "every action has a label", actions)
verifie("Tf('apt_action_' + l.action, {cmdline:" in js and "apt_irq_managed" in js,
        "the sheet shows the boot parameter and the managed IRQs")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    missing = sorted({k for k in keys if not cat.get(k)})
    verifie(not missing, "%s: labels (%d)" % (lang, len(set(keys))), missing)

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
