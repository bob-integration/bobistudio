#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The shared pool's real-time budget (decided with Cyril, 2026-11-23).

A plugin with a real-time role runs its deadline thread in SCHED_FIFO, ahead of every normal thread
of the pool. On the SHARED pool that load is bounded: the FIFO load (measured, else the cap, else
one core per container) must stay within half of the pool's counted capacity. Above it, a new
container — or one leaving « Dédié » — is refused unless the user chooses explicitly; a load that
grew past it afterwards is alerted. Held here, offline:
  1. core_pool.rt_load_of / charge_rt / admission_rt: the counting and the boundary;
  2. docker_compute.preflight_rt_shared: only a role counts, only off dedicated cores;
  3. the creation route gates on the server, before any row, with its own confirmation flag;
  4. cpu_weight.regler: leaving Dédié is refused past the budget and the level is put back,
     unless confirme_rt; both level routes pass the flag;
  5. core_pool.alerter_rt_partage: alerts past the budget, not below; node_health calls it;
  6. the four UI callers handle `rt_shared_full` with an explicit choice; labels in both languages.

    ./venv/bin/python tests/verif_admission_rt_partage.py
"""
import ast
import json
import os
import re
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
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_admission_rt_"), "db.sqlite")
database.init_db()
from app import core_pool, cpu_weight, docker_compute, plugins               # noqa: E402

print("\n1. Counting and boundary")
verifie(core_pool.rt_load_of(0.4, False, 3) == (0.4, False), "a measured load counts as measured")
verifie(core_pool.rt_load_of(None, False, 3) == (3.0, True), "not measured: its cap, estimated")
verifie(core_pool.rt_load_of(None, False, None) == (1.0, True), "no cap: one core")
verifie(core_pool.rt_load_of(2.0, True, 4) == (4.0, True), "a floor (« at least ») takes the cap")
verifie(core_pool.rt_load_of(5.0, True, 4) == (5.0, True), "a floor above the cap stays")
lines = [{"vmid": 1, "hostname": "stills", "rt_role": "program", "charge": 0.5, "charge_minorant": False},
         {"vmid": 2, "hostname": "shard", "rt_role": "monitoring", "charge": None, "plafond": 2},
         {"vmid": 3, "hostname": "monitor", "rt_role": "", "charge": 3.0}]
rt = core_pool.charge_rt(lines, 10)
verifie(rt["charge"] == 2.5 and [r["vmid"] for r in rt["conteneurs"]] == [1, 2],
        "only FIFO containers count (2.5 cores, the monitor is not one)", rt)
verifie(rt["budget"] == 5.0 and core_pool.RT_SHARED_SHARE == 0.5, "budget = half the capacity", rt)

NODE = {"id": 7, "name": "dell-x", "compute_cpuset": "0-19"}
core_pool.db_get_node = lambda nid: dict(NODE) if nid == 7 else {}
vue = {"partage": {"rt": rt}}
core_pool.vue_coeurs = lambda nid, force=False: vue
verifie(core_pool.admission_rt(7, 2.5)["suffisant"] is True, "2.5 + 2.5 = 5.0: admitted (boundary)")
a = core_pool.admission_rt(7, 2.6)
verifie(a["suffisant"] is False and a["noeud"] == "dell-x" and len(a["conteneurs"]) == 2,
        "2.5 + 2.6 > 5.0: refused, naming who holds the load", a)
verifie(core_pool.admission_rt(7, 0)["suffisant"] is True, "no demand: nothing to refuse")
NODE["compute_cpuset"] = ""
verifie(core_pool.admission_rt(7, 9)["suffisant"] is True, "no compute pool: abstain")
NODE["compute_cpuset"] = "0-19"

print("\n2. preflight_rt_shared")
MAN = {"rtplug": {"type": "rtplug", "resources": {"realtime": "program"}},
       "plain": {"type": "plain", "resources": {}}}
plugins.get = lambda t: MAN.get(t)
RES = {"rtplug": {"cores": 3}, "plain": {"cores": 2}}
docker_compute.effective_resources = lambda t, c: dict(RES.get(t) or {})
p = docker_compute.preflight_rt_shared(7, "rtplug")
verifie(p["role"] == "program" and p["demande"] == 3.0 and p["suffisant"] is False,
        "a shared type with a role asks its cap (3) — refused here", p)
verifie(docker_compute.preflight_rt_shared(7, "plain")["demande"] == 0, "no role: never counted")
RES["rtplug"] = {"cores": 3, "pin": True}
verifie(docker_compute.preflight_rt_shared(7, "rtplug")["demande"] == 0,
        "a dedicated type: not on the pool")
verifie(docker_compute.preflight_rt_shared(7, "rtplug", dedicated_ok=False)["demande"] == 3.0,
        "a dedicated type without room, created anyway: on the pool, counted")
RES["rtplug"] = {"cores": 3}

print("\n3. Creation route")
src = open(os.path.join(ROOT, "app", "routes", "__init__.py"), encoding="utf-8").read()
i_gate = src.find('if not data.get("confirme_rt")')
i_row = src.find("def _creer_docker():")
verifie(0 < i_gate < i_row, "gated before any container is created")
blk = src[i_gate:i_gate + 1500]
verifie("preflight_rt_shared" in blk and '"error_code": "rt_shared_full"' in blk and "409" in blk,
        "409 rt_shared_full from preflight_rt_shared")
verifie("dedicated_ok=_dedie_ok" in blk and 'data.get("confirme_partage")' in blk
        and re.search(r"_dedie_ok = docker_compute\.preflight_dedication\(.*\)\[\"suffisant\"\]", blk),
        "a Dedicated created on the shared pool by choice is counted too")

print("\n4. Leaving « Dédié »")
calls = []
docker_compute.apply_place_hot = lambda v: (calls.append(v) or (True, "moved"))
cpu_weight.appliquer = lambda nid, v, niveau=None: (True, "ok")
with database.get_db() as db:
    db.execute("INSERT INTO containers (vmid, hostname, node_id, deploy_config, status) "
               "VALUES (501, 'mixer-x', 7, ?, 'running')", (json.dumps({"type": "rtplug"}),))
    # The measured load lives in `container_metrics` (cf. database._METRIC_COLS).
    db.execute("INSERT OR REPLACE INTO container_metrics (vmid, cpu_percent, cpu_count) VALUES (501, 50, 4)")
    db.commit()
database.db_set_resources_override(501, cpu_weight="dedicated")


def eff(t, c):
    lvl = cpu_weight.normalize((c or {}).get("cpu_weight_override")) or "shared"
    return {"cores": 3, "pin": lvl == "dedicated", "cpu_weight": lvl}


docker_compute.effective_resources = eff
docker_compute._type_of = lambda c: "rtplug"
vue["partage"]["rt"] = core_pool.charge_rt(lines, 8)            # 2.5 held, budget 4.0
r = cpu_weight.regler(501, "shared")
c = database.db_get_container(501)
verifie(r.get("ok") is False and (r.get("err") or {}).get("error") == "rt_shared_full"
        and (r["err"].get("demande") == 2.0),
        "refused past the budget, with its measured load (50 % × 4 = 2 cores)", r)
verifie(c.get("cpu_weight_override") == "dedicated" and not calls,
        "the level is put back and nothing is moved", (c.get("cpu_weight_override"), calls))
verifie(isinstance((r.get("err") or {}).get("message"), str) and "dell-x" in r["err"]["message"],
        "the refusal says it in words", r.get("err"))
r = cpu_weight.regler(501, "shared", confirme_rt=True)
verifie(r.get("ok") is True and calls == [501]
        and database.db_get_container(501).get("cpu_weight_override") == "shared",
        "the user's explicit choice goes through", r)
database.db_set_resources_override(501, cpu_weight="dedicated")
vue["partage"]["rt"] = core_pool.charge_rt(lines[:1], 10)      # 0.5 of 5.0
calls.clear()
verifie(cpu_weight.regler(501, "shared").get("ok") is True and calls == [501],
        "within the budget: no question asked")
for f in ("app/routes/__init__.py", "app/routes/nodes.py"):
    s = open(os.path.join(ROOT, f), encoding="utf-8").read()
    verifie(any("regler(" in l and re.search(r"confirme_rt=bool\(\w+\.get\(\"confirme_rt\"\)\)", l)
                for l in s.splitlines()),
            "%s passes the user's choice to regler" % f)

print("\n5. State alert")
al = []
core_pool._alerter_throttle = lambda *a, **k: al.append((a, k))
core_pool._noms_vmids = lambda vs: ",".join(map(str, vs))
vue["partage"]["rt"] = core_pool.charge_rt(lines, 4)            # 2.5 for 2.0
verifie(core_pool.alerter_rt_partage(7, vue) is True and al
        and al[0][1].get("cle") == "alert.resource.rt_partage_depasse",
        "past the budget: alerted", al)
al.clear()
vue["partage"]["rt"] = core_pool.charge_rt(lines, 10)
verifie(core_pool.alerter_rt_partage(7, vue) is False and not al, "within the budget: silent")
nh = open(os.path.join(ROOT, "app", "node_health.py"), encoding="utf-8").read()
verifie("core_pool.alerter_rt_partage(nid, vue=vue)" in nh, "node_health checks it every pass")
cp = open(os.path.join(ROOT, "app", "core_pool.py"), encoding="utf-8").read()
verifie('etat_partage["rt"] = charge_rt(partages, _capacite)' in cp
        and 'ligne["rt_role"] = _rt_role(c)' in cp, "vue_coeurs publishes the load and each role")

print("\n6. Interface and labels")
for f in ("static/scripts.js", "static/js/container_panel.js", "static/js/cores.js",
          "templates/projects.html"):
    s = open(os.path.join(ROOT, f), encoding="utf-8").read()
    verifie("rt_shared_full" in s and "confirme_rt" in s, "%s answers rt_shared_full" % f)
sj = open(os.path.join(ROOT, "static", "scripts.js"), encoding="utf-8").read()
verifie("rendre('liberer'), { once: true }" in sj[sj.find("function _createRtDialog"):],
        "closing the creation window never creates")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    keys = ("api.containers.rt_partage_plein", "js.create.rt_titre", "js.create.rt_aide",
            "js.create.rt_tenus", "js.create.rt_charge", "js.create.rt_non",
            "containers.panel.rt_confirm", "alert.resource.rt_partage_depasse")
    missing = [k for k in keys if not cat.get(k)]
    verifie(not missing, "%s: every label" % lang, missing)
    verifie(all(x in cat.get("alert.resource.rt_partage_depasse", "")
                for x in ("{n}", "{charge}", "{budget}", "{count}", "{noms}")),
            "%s: the alert names its figures" % lang)

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
