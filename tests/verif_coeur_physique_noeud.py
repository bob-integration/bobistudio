#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""A dedicated core shares its physical core with nobody (2026-11-25: the aptitude sheet's « ht »
reserves on dell-1, r620-2 and dl-360-1).

Held here, offline (throwaway base, no node):
  1. `edge_phys`: the physical cores straddling the compute pool's edge (CPU 0's twin on a compute
     node); none on a 2110 node, whose pool is the complement of the engine's HT-aware footprint;
  2. a reservation takes such a core LAST — only when nothing else fits, so no capacity is lost;
     an EXISTING one keeps it (never released for that);
  3. « dedicated = physical core » per node: the cores view says whether it is on and, before it is,
     the counted capacity after (exclusive physical cores — unchanged by construction);
  4. turning it on or off through the node settings re-applies the shared cpusets at once;
  5. the cores window offers the switch (shared push control), labels in both languages.

MUTATIONS, CORE_MUTATION=<name> — each must give rc=1:
    sans_bord       a reservation takes the twin of a system CPU while another core fits
    sans_place      that twin is refused even when it is the last core that fits
    sans_reapply    changing the node setting does not re-apply the shared cpusets
    sans_etat       the cores view does not say the setting is on

    ./venv/bin/python tests/verif_coeur_physique_noeud.py
"""
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_coeur_phys_"), "t.db")
config.DB_PATH = database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import core_pool                                                     # noqa: E402
from app.routes import settings_api                                           # noqa: E402

MUT = os.environ.get("CORE_MUTATION") or ""
MUTATIONS = {
    "sans_bord": (core_pool, "            edge = edge_phys(pool, core_of)\n", "            edge = set()\n"),
    "sans_place": (core_pool, "                    pick = sorted(keep + spare[:len(on_edge)] + on_edge[len(spare):])\n",
                   "                    pick = sorted(keep + spare[:len(on_edge)])\n"),
    "sans_reapply": (settings_api, '    if "pin_reserve_physical" in clear or "pin_reserve_physical" in (data.get("set") or {}):',
                     "    if False:"),
    "sans_etat": (core_pool, '    etat_partage["strict"] = bool(core_of) and reserve_physique(node_id)\n', ""),
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


print("\n1. edge_phys")
DELL = {c: c % 44 for c in range(88)}                   # dell-1: twin of n is n+44
verifie(core_pool.edge_phys(range(1, 88), DELL) == {0}, "★ dell-1, pool 1-87: CPU 0's physical core (twin 44)")
DL = {c: c % 40 for c in range(80)}                      # dl-360-1: engine 0-17 + twins, pool the rest
verifie(core_pool.edge_phys(list(range(18, 40)) + list(range(58, 80)), DL) == set(),
        "2110 node, pool = complement of the HT-aware footprint: no edge")
verifie(core_pool.edge_phys(range(1, 6), {}) == set(), "no HT map: nothing invented")

print("\n2. Allocation")
CO = {0: 0, 1: 0, 2: 2, 3: 2, 4: 4, 5: 4}               # CPU 1's twin is 0, outside the pool
NODE = database.db_add_node("phys-x", "127.0.0.1")
database.db_update_node(NODE, compute_cpuset="1-5")
core_pool.core_map_cached = lambda nid: CO
core_pool.numa_map_cached = lambda nid: {}
core_pool.isolated_cached = lambda nid: set()
core_pool.reapply_shared_after_reservation = lambda *a, **k: None
core_pool.purger_orphelines = lambda nid: None             # no container rows here: nothing is orphan
got = core_pool.allocate_cores(NODE, 60, 1)
verifie(got == "2", "★ CPU 1 (twin of system CPU 0) is not taken while another core fits", got)
verifie(core_pool.allocate_cores(NODE, 61, 1) == "4", "next one: 4")
verifie(core_pool.free_dedicated_capacity(NODE, vmid=62) == 1, "capacity unchanged: CPU 1 still counts",
        core_pool.free_dedicated_capacity(NODE, vmid=62))
got = core_pool.allocate_cores(NODE, 62, 1)
verifie(got == "1", "★ the last core that fits is CPU 1: taken, never a shared fallback", got)
with database.get_db() as db:
    db.execute("DELETE FROM node_core_alloc WHERE vmid IN (60, 61, 62)")
    db.commit()
with database.get_db() as db:
    db.execute("INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?, 1, 70)", (NODE,))
    db.commit()
_got = core_pool.allocate_cores(NODE, 70, 1)
verifie(_got == "1", "an EXISTING reservation on CPU 1 is kept, never released", _got)

print("\n3. The cores view")
from app import placement                                                     # noqa: E402
placement.releve_cache = lambda nid, force=False: None
core_pool.reserve_physique = lambda node_id=None: False
v = core_pool.vue_coeurs(NODE)
p = v["partage"]
verifie(p.get("strict") is False and p.get("si_strict", {}).get("exclusifs") == p.get("capacite_comptee"),
        "setting off: what it would cost, counted capacity after = before", {k: p.get(k) for k in ("strict", "si_strict", "capacite_comptee")})
core_pool.reserve_physique = lambda node_id=None: True
p = core_pool.vue_coeurs(NODE)["partage"]
verifie(p.get("strict") is True and "si_strict" not in p, "★ setting on: the view says so", p.get("strict"))

print("\n4. Node setting → shared cpusets re-applied at once")
calls = []
core_pool.reappliquer_partages = lambda node_id, force=False: calls.append((node_id, force))
from flask import Flask                                                       # noqa: E402
_app = Flask(__name__)
for body in ({"set": {"pin_reserve_physical": 1}}, {"set": {"pin_reserve_physical": 0}}):
    with _app.test_request_context(json=body):
        settings_api.api_set_node_settings.__wrapped__(NODE)
import time                                                                   # noqa: E402
for _ in range(50):
    if len(calls) >= 2:
        break
    time.sleep(0.02)
verifie(calls == [(NODE, True), (NODE, True)], "★ on and off: re-applied, forced measure", calls)
calls.clear()
with _app.test_request_context(json={"set": {"ip_start": ""}}):
    settings_api.api_set_node_settings.__wrapped__(NODE)
time.sleep(0.1)
verifie(calls == [], "another setting: nothing re-applied", calls)

print("\n5. Cores window, labels")
js = open(os.path.join(ROOT, "static", "js", "cores.js"), encoding="utf-8").read()
verifie('class="ctl-push bc-strict" data-on="1"' in js and 'class="ctl-push bc-strict" data-on="0"' in js
        and "pin_reserve_physical: on ? 1 : 0" in js and "p.strict" in js,
        "switch on / off in the capacity box, shared push control, node setting")
keys = set(re.findall(r"'(settings\.cores\.(?:strict_\w+|si_strict_capacite))'", js))
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    missing = sorted(k for k in keys if not cat.get(k))
    verifie(len(keys) >= 7 and not missing, "%s: labels (%d)" % (lang, len(keys)), missing)
fr = json.load(open(os.path.join(ROOT, "i18n", "fr.json"), encoding="utf-8"))
verifie("Réglages → CPU" not in fr.get("settings.node_sheet.apt_action_ht", ""),
        "the aptitude sheet points to where the switch really is", fr.get("settings.node_sheet.apt_action_ht"))

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
