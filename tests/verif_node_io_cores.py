#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Guard: node I/O (a DeckLink card) holds WHOLE physical cores, sized by its active connectors.

Decided on 2026-11-07: the SDI card is the node's input/output, like the 2110 engine. Nothing may
run beside its threads — the Z8 flashed black on 30/10 because a card's cores were the HT siblings
of two busy pyramids. Its size follows its active connectors (activating one recreates it); it is
guaranteed by construction, hence out of the project verdict.

Held offline (throwaway database; HT/NUMA maps, the node agent and `docker run` are doubled):
  1. ★ the size: ceil((0.1 + 0.49 × connectors) × 1.22), at least 1 — 1→1, 2→2, 4→3, 8→5; a fill &
     key output counts two connectors; a malformed list counts as empty;
  2. ★ deploying the card: as many cores as its connectors need, ONE logical CPU per physical core,
     on the socket of its card, marked `physical`;
  3. ★ the shared pool loses those cores AND their HT siblings; a dedicated neighbour never gets
     them either;
  4. a change of connectors resizes the reservation at the next deployment, both ways;
  5. a whole-core reservation that holds two siblings of one core is re-picked;
  6. the estimate (« 3 → 5 cores », fits, holders) counts whole cores;
  7. ★ the Resources window: the card's cells AND their siblings are `io`; on air or not, it does not
     count in the verdict; a plan cannot resize or move it; no move suggestion;
  8. ★ the watch: another container measured on a card's sibling → `io_cores_shared`.

MUTATIONS, NODE_IO_MUTATION=<name> — each must give rc=1:
    shared_keeps_siblings  the shared pool keeps the siblings of the card's cores
    two_siblings           a whole-core reservation may take both siblings of one core
    items_ignored          the deployment ignores the connector count (manifest cores)
    no_physical_mark       the reservation is not marked whole
    no_card_socket         the card's socket is not preferred
    io_required            node I/O counts in the project verdict
    watch_blind            the watch ignores a container on a card's sibling

    ./venv/bin/python tests/verif_node_io_cores.py
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="node_io_cores_"), "t.db")
config.DB_PATH = _DBF
database.DB_PATH = _DBF
database._tls.__dict__.pop("conn", None)
database.init_db()
assert database.DB_PATH == _DBF

from app import core_pool, docker_compute, node_driver, placement, plugins     # noqa: E402
from app import project_resources, guarantee_watch, etalonnage                 # noqa: E402

MUT = os.environ.get("NODE_IO_MUTATION") or ""
MUTATIONS = {
    "shared_keeps_siblings": (core_pool, "    if entiers:\n        libres = [c for c in libres if core_of.get(c) not in entiers]\n", ""),
    "two_siblings": (core_pool, "    return sorted(chosen.values())\n", "    return sorted(cands)\n"),
    "items_ignored": (docker_compute, "        _by_items = None if _cores_force else items_cores(res, _p)",
                      "        _by_items = None"),
    "no_physical_mark": (core_pool, "                           (1 if physical else 0, node_id, vmid))",
                         "                           (0, node_id, vmid))"),
    "no_card_socket": (docker_compute, '    if prefer_numa is None and res.get("io_pci_vendor"):',
                       '    if False:'),
    "io_required": (project_resources, '        r["required"] = (r.get("level") == "dedicated") and not r["node_io"]',
                    '        r["required"] = (r.get("level") == "dedicated")'),
    "watch_blind": (guarantee_watch, "        if others:\n            c = db_get_container(io_vmid)",
                    "        if False:\n            c = db_get_container(io_vmid)"),
}
if MUT:
    if MUT not in MUTATIONS:
        print("mutation inconnue :", MUT)
        sys.exit(2)
    _mod, _old, _new = MUTATIONS[MUT]
    _src = open(_mod.__file__, encoding="utf-8").read()
    if _src.count(_old) != 1:
        print("mutation %s : motif introuvable ou ambigu (%d) — la garde est périmée" % (MUT, _src.count(_old)))
        sys.exit(1)
    exec(compile(_src.replace(_old, _new), _mod.__file__, "exec"), _mod.__dict__)
    print("MUTATION active :", MUT)

ECHECS = []


def ok(cond, libelle, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond else "  [%s]" % (detail,)))
    if not cond:
        ECHECS.append(libelle)


# ── Doubles: I/O only ───────────────────────────────────────────────────────────────────────────
# Two sockets, 8 physical cores, logical c ↔ c+8. Physical 0-3 on socket 0, 4-7 on socket 1; the
# card sits on socket 0 — the LESS attractive one (CPU 0 is outside the pool, so socket 1 has more
# room): without the card preference the allocator would pick socket 1.
HT = {c: c % 8 for c in range(16)}
NUMA = {c: (0 if c % 8 < 4 else 1) for c in range(16)}
core_pool.core_map_cached = lambda nid: dict(HT)
core_pool.numa_map_cached = lambda nid: dict(NUMA)
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)
core_pool.reapply_shared_after_reservation = lambda *a, **k: None
core_pool.after_cores_released = lambda *a, **k: None
placement.releve_cache = lambda node_id, ttl=None, force=False: None


def _host_exec(node, cmd, timeout=None, **kw):
    return (0, "0\n", "") if "0xbdbd" in cmd else (0, "", "")


node_driver.host_exec = _host_exec
docker_compute.ssh_run = lambda host, cmd, timeout=None, **kw: (0, "", "")

SDI = {"cores": 5, "memory": 2048, "pin": True, "physical_cores": True, "node_io": True,
       "io_pci_vendor": "0xbdbd",
       "cores_per_item": {"lists": ["inputs", "outputs"], "pair_key": "key_persistent_id",
                          "base": 0.1, "per": 0.49, "peak": 1.22}}
PROFILES = {"decklink_io": SDI, "mixer": {"cores": 2, "pin": True}, "stills": {}}
plugins.get = lambda t: ({"resources": dict(PROFILES[t])} if t in PROFILES else None)
plugins.image_variant = lambda t: "compute"
plugins.wants_media_volume = lambda t: False
plugins.wants_lv2_volume = lambda t: False
plugins.wants_state_volume = lambda t: False
plugins.needs_decklink = lambda t: False
plugins.gpu_capable = lambda t: False

SPEC = {}


def _fake_run(node, spec):
    SPEC[spec.get("name") or len(SPEC)] = spec
    SPEC["last"] = spec
    return True, {"ip": "10.0.0.9"}


node_driver.run_container = _fake_run
node_driver.has_agent = lambda n: True
docker_compute.status_compute = lambda v: "running"
docker_compute._cert_mtls_conteneur = lambda v: None
docker_compute.db_update_docker_ip = lambda v, ip: None
import app.deploy as _deploy                                                  # noqa: E402
_deploy.token_a_injecter = lambda v: None
_deploy.agent_session = lambda: type("S", (), {"get": lambda *a, **k: type("R", (), {"status_code": 200})()})()
_deploy.agent_url = lambda ip, p: "http://x" + p
_deploy.agent_headers = lambda v: {}

NID = database.db_add_node("dell", "127.0.0.1")
database.db_update_node(NID, compute_cpuset="1-7,9-15", agent_url="http://127.0.0.1:9100",
                        compute_image="bobi-compute:1.0", docker_network="bobi-net")


def _add(vmid, name, typ, params=None):
    database.db_upsert_container_docker(vmid, name, NID, "bobi-%d" % vmid)
    database.db_update_deploy_config(vmid, typ, params or {})
    with database.get_db() as db:
        db.execute("UPDATE containers SET status='running' WHERE vmid=?", (vmid,))
        db.commit()


def _ports(n_in, n_out, key=0):
    return {"inputs": [{"persistent_id": "i%d" % i} for i in range(n_in)],
            "outputs": [dict({"persistent_id": "o%d" % i}, **({"key_persistent_id": "k%d" % i} if i < key else {}))
                        for i in range(n_out)]}


def _deploy_card(params):
    database.db_update_deploy_config(701, "decklink_io", params)
    docker_compute.deploy_compute(701, params=params, deploy_type="decklink_io", force=True)
    return core_pool.parse_cpuset((SPEC["last"].get("resources") or {}).get("cpuset") or "")


print("\n1. ★ The size follows the active connectors")
for n, want in ((0, 1), (1, 1), (2, 2), (4, 3), (8, 5)):
    got = docker_compute.items_cores(SDI, _ports(n, 0))
    ok(got == want, "1. %d connector(s) → %d core(s)" % (n, want), got)
ok(docker_compute.active_items(SDI["cores_per_item"], _ports(1, 2, key=1)) == 4,
   "1b. a fill & key output counts two connectors")
ok(docker_compute.items_cores(SDI, {"inputs": "x", "outputs": None}) == 1, "1c. malformed lists count as empty")
ok(docker_compute.items_cores({"cores": 3}, {}) is None, "1d. a type without the rule: None")

print("\n2. ★ Deploying the card")
_add(701, "decklink-io", "decklink_io")
cores = _deploy_card(_ports(2, 2))
ok(len(cores) == 3, "2a. four connectors → three cores", sorted(cores))
ok(len({HT[c] for c in cores}) == len(cores), "2b. ★ one logical CPU per physical core", sorted(cores))
ok(cores and all(NUMA[c] == 0 for c in cores), "2c. ★ on the socket of its card", sorted(cores))
ok(core_pool.physical_allocations(NID).get(701) == sorted(cores), "2d. ★ the reservation is marked whole",
   core_pool.physical_allocations(NID))

print("\n3. ★ Nothing else runs on those physical cores")
siblings = {c for c in range(16) if HT[c] in {HT[x] for x in cores}}
shared, _ = core_pool.partage_disponible(NID)
ok(not (set(shared) & siblings), "3a. ★ the shared pool has neither the cores nor their siblings",
   sorted(set(shared) & siblings))
_add(702, "stills", "stills")
docker_compute.deploy_compute(702, params={}, deploy_type="stills", force=True)
st = core_pool.parse_cpuset((SPEC["last"].get("resources") or {}).get("cpuset") or "")
ok(st and not (st & siblings), "3b. a shared container deployed now stays off them", sorted(st))
_add(703, "mixer", "mixer")
docker_compute.deploy_compute(703, params={}, deploy_type="mixer", force=True)
mx = core_pool.parse_cpuset((SPEC["last"].get("resources") or {}).get("cpuset") or "")
ok(len(mx) == 2 and not (mx & siblings), "3c. a dedicated neighbour never gets them either", sorted(mx))

print("\n4. A change of connectors resizes at the next deployment")
cores8 = _deploy_card(_ports(4, 4))
ok(len(cores8) == 5 and len({HT[c] for c in cores8}) == 5, "4a. eight connectors → five whole cores", sorted(cores8))
cores1 = _deploy_card(_ports(1, 0))
ok(len(cores1) == 1 and core_pool.allocated_for(NID, 701) == 1, "4b. one connector → one core, the rest released",
   sorted(cores1))
siblings1 = {c for c in range(16) if HT[c] in {HT[x] for x in cores1}}
shared, _ = core_pool.partage_disponible(NID)
ok((set(range(16)) - siblings1 - mx - {0, 8}) <= set(shared), "4c. the released cores are shared again",
   sorted(set(range(16)) - siblings1 - mx - {0, 8} - set(shared)))

print("\n5. A whole reservation holding two siblings of one core is re-picked")
with database.get_db() as db:
    db.execute("DELETE FROM node_core_alloc WHERE vmid=701")
    taken = {HT[c] for c in core_pool.allocations_by_vmid(NID).get(703, [])}
    p = next(p for p in range(1, 4) if p not in taken)            # a free physical core, socket 0
    db.executemany("INSERT INTO node_core_alloc (node_id, core, vmid, physical) VALUES (?,?,?,1)",
                   [(NID, p, 701), (NID, p + 8, 701)])
    db.commit()
got = core_pool.parse_cpuset(core_pool.allocate_cores(NID, 701, 2, prefer_numa=0, physical=True) or "")
ok(len(got) == 2 and len({HT[c] for c in got}) == 2, "5. ★ two distinct physical cores", sorted(got))

print("\n6. The estimate shown before applying")
_deploy_card(_ports(2, 2))
est = docker_compute.item_cores_estimate(database.db_get_container(701), _ports(4, 4))
ok(est["current"] == 3 and est["wanted"] == 5 and est["items"] == 8, "6a. « 3 → 5 cores » for eight connectors", est)
# 8 physical cores, 0 is outside the pool, the mixer holds 2 → 5 physical cores, the card's 3 included.
ok(est["available"] == 5 and est["fits"], "6b. counted in whole physical cores, its own included", est)
ok([h["hostname"] for h in est["holders"]] == ["mixer"], "6c. the holders are named", est["holders"])
ok(not docker_compute.item_cores_estimate(database.db_get_container(701), _ports(8, 8))["fits"],
   "6d. sixteen connectors (10 cores) do not fit")
ok(docker_compute.item_cores_estimate(database.db_get_container(703), {}) is None, "6e. not for a mixer")

print("\n7. ★ The Resources window")
pid = database.db_save_project("Atelier", [701, 702, 703])
database.db_set_resources_override(701, cpu_weight="dedicated")
database.db_set_resources_override(703, cpu_weight="dedicated")
etalonnage.ressources = lambda **k: [
    {"vmid": v, "hostname": h, "type": t, "node_id": NID, "level": lv, "reserve_pct": rp, "etat": "non_etalonne",
     "consomme_pct": None, "mesure": None, "besoin_pct": None, "project_id": None}
    for v, h, t, lv, rp in ((701, "decklink-io", "decklink_io", "dedicated", 300), (702, "stills", "stills", "normal", None),
                            (703, "mixer", "mixer", "dedicated", 200))]
pg = project_resources.project_guarantee(pid)
card = next(r for r in pg["containers"] if r["vmid"] == 701)
ok(card["node_io"] and not card["required"], "7a. ★ on air, the card still does not count in the verdict", card)
ok(next(r for r in pg["containers"] if r["vmid"] == 703)["required"], "7b. the on-air mixer does")
nm = project_resources.node_map(NID, {701, 702, 703})
io_cells = {x["cpu"] for x in nm["cells"] if x["kind"] == "io"}
held = set(core_pool.allocations_by_vmid(NID)[701])
want_cells = {c for c in core_pool.parse_cpuset("1-7,9-15") if HT[c] in {HT[x] for x in held}}
ok(io_cells == want_cells, "7c. ★ its cells AND their siblings are drawn as node I/O", (sorted(io_cells), sorted(want_cells)))
ok(all(x.get("vmid") == 701 for x in nm["cells"] if x["kind"] == "io"), "7d. linked to its row (member)")
okp, res = project_resources.check_plan(pid, [{"vmid": 701, "cores": 2}])
ok(not okp and res["errors"][0]["error"] == "node_io", "7e. a plan cannot resize it", res)
rows = [dict(card, guarantee="reserved_unmeasured", node_id=NID), {"vmid": 702, "node_id": 99, "guarantee": "shared", "hostname": "x"}]
sug = project_resources.move_suggestions(rows, [{"id": NID, "cells": [1] * 14, "dedicable": 9, "name": "dell"},
                                                {"id": 99, "cells": [1] * 14, "dedicable": 9, "name": "autre"}],
                                         [(702, 701)])
ok(not any(s["vmid"] == 701 for s in sug), "7f. never suggested for a move", sug)

print("\n8. ★ The watch")
VIEW = {"releve": True, "dedies": [], "partages": []}
core_pool.vue_coeurs = lambda node_id, force=False: VIEW
sib = sorted(want_cells - held)
VIEW["partages"] = [{"vmid": 702, "hostname": "stills", "cpuset_applique": "%d" % sib[0]}]
d = guarantee_watch.defects(NID)
ok((701, "io_cores_shared") in d and "stills" in d[(701, "io_cores_shared")]["voisins"],
   "8a. ★ a shared container on a card's sibling is a defect, naming it", d)
VIEW["partages"] = [{"vmid": 702, "hostname": "stills", "cpuset_applique": "1-3"}]
VIEW["partages"][0]["cpuset_applique"] = core_pool.fmt_cpuset(set(range(16)) - want_cells - {0, 8})
ok((701, "io_cores_shared") not in (guarantee_watch.defects(NID) or {}), "8b. none when it stays off them")

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — la carte SDI tient ses cœurs physiques entiers, à la taille de ses prises")
