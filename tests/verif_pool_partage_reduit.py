#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Shared pool reduced to a thread: said, and said without opening a page (2026-11-09).

Z8, 2026-11-08: with `pin_reserve_physical=1` every dedicated core also takes its HT sibling, and
the shared pool was down to ONE thread. Every new shared container got that cpuset, silently. An
alert already existed (« shared pool too low »), with three holes, closed here:
  1. it only counted REAL-TIME containers → an absolute floor: fewer than 2 logical CPUs for any
     shared container is said too;
  2. it was only evaluated when someone opened the cores window → every 5 min from the node
     health sampler (key presence, not a default date);
  3. nothing warned when a RESERVATION emptied the pool → checked after every reservation.
     Warned, not refused: Cyril keeps the setting on the Z8, where the pool is already one thread.
Offline: throwaway database, HT map and placement doubled.

    ./venv/bin/python tests/verif_pool_partage_reduit.py
"""
import json
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_pool_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import core_pool, node_driver, docker_compute, placement, settings, node_health  # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


node_driver.host_exec = lambda node, cmd, timeout=None, **kw: (0, "", "")
docker_compute.ssh_run = lambda host, cmd, timeout=None, **kw: (0, "", "")
# Five logical CPUs: 0↔3 and 1↔4 are HT siblings, 2 has none.
core_pool.core_map_cached = lambda nid: {0: 0, 1: 1, 2: 2, 3: 0, 4: 1}
core_pool.numa_map_cached = lambda nid: {c: 0 for c in range(5)}
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)
placement.releve_cache = lambda node_id, ttl=None, force=False: None

NID = database.db_add_node("petit", "127.0.0.1")
database.db_update_node(NID, compute_cpuset="0-4", agent_url="http://127.0.0.1:9100")
for vmid, nom in ((300, "dedie"), (301, "partage")):
    database.db_upsert_container_docker(vmid, nom, NID, "bobi-%d" % vmid)
    database.db_update_deploy_config(vmid, "delay", {})
with database.get_db() as db:
    db.executemany("INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?,?,?)",
                   [(NID, 0, 300), (NID, 1, 300)])
    db.commit()

alertes = []
core_pool.db_add_alert = lambda key, niveau="info", **kw: alertes.append(key)

print("\n1. an absolute floor, real-time or not")
settings.set("pin_reserve_physical", 1)
p = core_pool.vue_coeurs(NID)["partage"]
verifie(p["cores"] == 1 and p["reduit"], "siblings closed → one thread left (2) → reduced", p)
core_pool._alerte_last.clear()
del alertes[:]
core_pool.alerter_partage_bas(NID)
verifie(alertes == ["alert.resource.partage_reduit"], "… and alerted, though no container is real-time", alertes)
settings.set("pin_reserve_physical", 0)
p = core_pool.vue_coeurs(NID)["partage"]
verifie(p["cores"] == 3 and not p["reduit"], "siblings open → 3 threads → not reduced", p)
settings.set("pin_reserve_physical", 1)
with database.get_db() as db:
    db.execute("DELETE FROM containers WHERE vmid=301")
    db.commit()
verifie(not core_pool.vue_coeurs(NID)["partage"]["reduit"], "no shared container at all → nothing to say")
database.db_upsert_container_docker(301, "partage", NID, "bobi-301")
database.db_update_deploy_config(301, "delay", {})

del alertes[:]
core_pool._alerte_last.clear()
vue = {"partage": {"bas": True, "reduit": True, "cpuset": "2", "cores": 1, "temps_reel": 2, "conteneurs": 2},
       "partages": [{"vmid": 301, "temps_reel": True}]}
core_pool.alerter_partage_bas(NID, vue=vue)
verifie(alertes == ["alert.resource.partage_trop_bas"], "the real-time verdict, when it holds, replaces it", alertes)

print("\n3. checked after every reservation")
appels = []
_vrais = (core_pool._reapply_shared, core_pool.sync_dedicated_cpufreq, core_pool.alerter_partage_bas)
core_pool._reapply_shared = lambda n, v, c: None
core_pool.sync_dedicated_cpufreq = lambda n, v=None: None
core_pool.alerter_partage_bas = lambda n, vue=None: appels.append(n)
try:
    core_pool._reapply_shared_after_reservation(NID, 300, "0-1")
finally:
    core_pool._reapply_shared, core_pool.sync_dedicated_cpufreq, core_pool.alerter_partage_bas = _vrais
verifie(appels == [NID], "a reservation that succeeded → the shared pool is checked", appels)

print("\n2. evaluated by the node sampler, not only by the cores page")
import time as _time                                                          # noqa: E402
vu = []
_vrais = (core_pool.vue_coeurs, core_pool.alerter_partage_bas, core_pool.alerter_dedies_non_exclusifs, _time.monotonic)
core_pool.vue_coeurs = lambda nid, force=False: {"n": nid}
core_pool.alerter_partage_bas = lambda nid, vue=None: vu.append(("bas", nid))
core_pool.alerter_dedies_non_exclusifs = lambda nid, vue=None: vu.append(("dedies", nid))
horloge = [12.0]                                       # a controller that booted 12 s ago
_time.monotonic = lambda: horloge[0]
try:
    node_health._coeurs_vus.clear()
    node_health._verifier_coeurs(NID)
    verifie(vu == [("bas", NID), ("dedies", NID)], "first pass, 12 s after boot → checked (key presence)", vu)
    horloge[0] += 60
    node_health._verifier_coeurs(NID)
    verifie(len(vu) == 2, "one minute later → not again", vu)
    horloge[0] += 300
    node_health._verifier_coeurs(NID)
    verifie(len(vu) == 4, "five minutes later → again", vu)
finally:
    core_pool.vue_coeurs, core_pool.alerter_partage_bas, core_pool.alerter_dedies_non_exclusifs, _time.monotonic = _vrais
src = open(os.path.join(RACINE, "app", "node_health.py"), encoding="utf-8").read()
verifie("        _verifier_coeurs(nid)\n" in src, "the sampler calls it for each node")

print("\n4. shown in the cores window, and worded")
js = open(os.path.join(RACINE, "static", "js", "cores.js"), encoding="utf-8").read()
verifie("settings.cores.partage_reduit" in js, "the cores window says it")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", "%s.json" % lang), encoding="utf-8"))
    for k, champs in (("alert.resource.partage_reduit", {"n", "cpuset", "cores", "count", "noms"}),
                      ("settings.cores.partage_reduit", {"cpuset", "cores", "count"})):
        vus = set(re.findall(r"\{(\w+)\}", cat.get(k, "")))
        verifie(vus == champs, "%s %s: placeholders" % (lang, k), vus)

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
