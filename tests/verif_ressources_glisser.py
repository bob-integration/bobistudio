#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Server side of the drag-and-drop on the node map of the project Resources window (2026-11-07):
a drop builds the same plan as a suggestion, checked by `check_plan` through the route of « Apply »
with `check: true` — nothing applied from a drop.

Held offline (throwaway database; ressources/garantir/migration doubled):
  1. ★ `view` says per member why it cannot move (`move_block`): node I/O (the DeckLink card is
     physically in its node), a type the migration refuses (`migration.INTERDITS`), stopped;
  2. ★ `check_plan` refuses moving such a type before touching anything (`not_movable`), still
     accepts a resize in place for it, and keeps refusing node I/O and a node without room;
  3. ★ POST /api/projects/<pid>/apply with `check: true` answers the same refusals (409) or the
     plan (200) and applies NOTHING (no job, no migration); without it, the plan still runs.

MUTATIONS, GLISSER_PY_MUTATION=<name> — each must give rc=1:
    check_applique   the route ignores `check` (a drop would apply the move)
    type_passe       check_plan lets a refused type move
    bloc_muet        view no longer says why a member cannot move
    au_mieux_dedie   moving a shared container makes it dedicated (the behaviour before 07/11)

    ./venv/bin/python tests/verif_ressources_glisser.py
"""
import os
import sys
import tempfile
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                  # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="ressources_glisser_db_"), "t.db")
config.DB_PATH = _DBF
database.DB_PATH = _DBF
database._tls.__dict__.pop("conn", None)
database.init_db()
assert database.DB_PATH == _DBF

import flask                                      # noqa: E402
from app import project_resources as PR, core_pool, etalonnage, migration   # noqa: E402
from app.routes import projects_api as RP         # noqa: E402

MUT = os.environ.get("GLISSER_PY_MUTATION") or ""
MUTATIONS = {
    "check_applique": (RP, '    if body.get("check"):', '    if False:'),
    "type_passe": (PR, '        if dst != src and move_block(r) in ("type", "stopped"):', '        if False:'),
    "au_mieux_dedie": (PR, '        dedicated = (r.get("level") == "dedicated") or dst == src', '        dedicated = True'),
    "bloc_muet": (PR, '        r["move_block"] = move_block(r)', '        r["move_block"] = None'),
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


core_pool.core_map_cached = lambda nid: {}
core_pool.numa_map_cached = lambda nid: {}
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)
NA = database.db_add_node("A", "127.0.0.1")
NB = database.db_add_node("B", "127.0.0.2")
database.db_update_node(NA, compute_cpuset="0-7")
database.db_update_node(NB, compute_cpuset="0-3")


def _cont(vmid, host, typ, node):
    database.db_upsert_container(vmid, host, 1, 512, "running")
    database.get_db().execute("UPDATE containers SET node_id=? WHERE vmid=?", (node, vmid))
    database.get_db().commit()
    database.db_update_deploy_config(vmid, typ, {})


def _alloc(node, vmid, cores):
    with database.get_db() as db:
        for c in cores:
            db.execute("INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?,?,?)", (node, c, vmid))
        db.commit()


pid = database.get_db().execute("INSERT INTO projects (name, snapshot) VALUES ('Atelier', '[]')").lastrowid
database.get_db().commit()
MEMBRES = ((901, "mix", "mixer"), (902, "sdi", "decklink"), (903, "pyr", "pyramide"), (904, "off", "mixer"),
           (905, "mv", "multiview"))
for v, h, t in MEMBRES:
    _cont(v, h, t, NA)
    database.db_set_project(v, pid)
_alloc(NA, 901, [0])
_alloc(NA, 902, [1, 2])
_alloc(NA, 903, [3])
PR.is_node_io = lambda c: (c or {}).get("vmid") == 902
# 904 is absent from the measures: it is stopped.
etalonnage.ressources = lambda vmids=None, **k: [
    {"vmid": v, "hostname": h, "node_id": NA, "type": t, "reserve_pct": None, "etat": "non_etalonne",
     "level": "dedicated" if v == 901 else "normal"}
    for v, h, t in MEMBRES if v != 904 and (vmids is None or v in vmids)]

print("\n1. ★ Ce qui ne se déplace pas, et pourquoi")
blocs = {r["vmid"]: r.get("move_block") for r in PR.view(pid)["containers"]}
ok(blocs == {901: None, 902: "node_io", 903: "type", 904: "stopped", 905: None},
   "1. ★ mixer : déplaçable ; carte SDI : node_io ; pyramide : type ; arrêté : stopped", blocs)
ok("2110_io" in migration.INTERDITS, "1b. le moteur 2110 est un type que la migration refuse")

print("\n2. ★ check_plan")
okp, res = PR.check_plan(pid, [{"vmid": 901, "cores": 1, "node_id": NB}])
ok(okp and res["plan"][0]["move"], "2a. le mixer vers B : accepté, c'est un déplacement", res)
okp, res = PR.check_plan(pid, [{"vmid": 903, "cores": 1, "node_id": NB}])
ok(not okp and res["errors"] == [{"vmid": 903, "error": "not_movable", "reason": "type"}],
   "2b. ★ la pyramide vers B : refusée AVANT la migration, avec sa raison", res)
okp, res = PR.check_plan(pid, [{"vmid": 903, "cores": 2, "node_id": NA}])
ok(okp, "2c. … mais l'agrandir sur place reste possible", res)
okp, res = PR.check_plan(pid, [{"vmid": 902, "cores": 2, "node_id": NB}])
ok(not okp and res["errors"][0]["error"] == "node_io", "2d. la carte SDI : refusée (node_io)", res)
okp, res = PR.check_plan(pid, [{"vmid": 904, "cores": 1, "node_id": NB}])
ok(not okp and res["errors"][0].get("reason") == "stopped", "2e. un arrêté : refusé", res)
okp, res = PR.check_plan(pid, [{"vmid": 901, "cores": 5, "node_id": NB}])
ok(not okp and res["errors"][0]["error"] == "capacity" and res["errors"][0]["available"] == 4,
   "2f. 5 cœurs sur B qui en a 4 : refus « pas la place »", res)

print("\n2bis. ★ Déplacer un conteneur « au mieux » ne le rend pas Dédié")
okp, res = PR.check_plan(pid, [{"vmid": 905, "cores": 3, "node_id": NB}, {"vmid": 901, "cores": 4, "node_id": NB}])
p905 = [p for p in res["plan"] if p["vmid"] == 905]
ok(okp and p905 and p905[0]["move"] and not p905[0]["dedicated"] and p905[0]["cores"] == 0,
   "2g. ★ le multiview (Normal) vers B : déplacé sans cœurs, et sans prendre la place du mixer (4 sur 4)", res)
_av = []
migration.migrer = lambda vmid, node: (_av.append(vmid) or {"ok": True})
PR._run_plan(pid, {"steps": [], "state": "running"}, [p for p in res["plan"] if p["vmid"] == 905])
c905 = database.db_get_container(905)
ok(_av == [905] and c905.get("cpu_weight_override") is None and c905.get("cores_override") is None,
   "2h. ★ déplacé, il garde son niveau : aucun niveau Dédié ni cœurs posés", (_av, c905.get("cpu_weight_override")))
PR._run_plan(pid, {"steps": [], "state": "running"}, [p for p in res["plan"] if p["vmid"] == 901])
ok(database.db_get_container(901).get("cpu_weight_override") == "dedicated",
   "2i. un conteneur Dédié déplacé garde ses cœurs réservés sur la cible")

print("\n3. ★ La route de « Appliquer » en vérification seule")
appels = []
migration.migrer = lambda vmid, node: (appels.append(("migrer", vmid, node)) or {"ok": True})
etalonnage.garantir = lambda vmid, coeurs=None, marge=None: (appels.append(("garantir", vmid)) or True, {})
app_test = flask.Flask(__name__)


def _post(charge):
    with app_test.test_request_context(json=charge):
        r = RP.project_apply_plan.__wrapped__(pid)
    rep, code = r if isinstance(r, tuple) else (r, 200)
    return rep.get_json(), code


rep, code = _post({"check": True, "changes": [{"vmid": 901, "cores": 1, "node_id": NB}]})
time.sleep(0.2)
ok(code == 200 and rep["ok"] and rep["plan"][0]["to"] == NB, "3a. plan qui tient : 200 avec le plan", (code, rep))
ok(not appels and PR.plan_status(pid) == {"state": "none"}, "3b. ★ … et RIEN n'est appliqué (ni migration ni tâche)",
   (appels, PR.plan_status(pid)))
rep, code = _post({"check": True, "changes": [{"vmid": 901, "cores": 5, "node_id": NB}]})
ok(code == 409 and not rep["ok"] and rep["errors"][0]["error"] == "capacity", "3c. ★ plan refusé : 409 avec la raison", (code, rep))
ok(not appels, "3d. … rien n'est appliqué", appels)
rep, code = _post({"changes": [{"vmid": 901, "cores": 1, "node_id": NB}]})
for _ in range(100):
    if PR.plan_status(pid)["state"] != "running":
        break
    time.sleep(0.05)
ok(code == 202 and ("migrer", 901, NB) in appels, "3e. sans `check`, « Appliquer » applique toujours (202, migration)", (code, appels))

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — raisons de non-déplacement, refus avant migration, vérification seule par la route d'application")
