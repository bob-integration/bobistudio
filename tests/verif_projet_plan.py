#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""A project's resources as a plan: the schema of every node, the move suggestions, the plan check
and its application (`app/project_resources.py`, decided with Cyril on 2026-11-07).

Held offline (throwaway database; host, garantir and migration doubled):
  1. a node's cells: dedicated to the project, to another project, to a 2110 engine, free, or
     free-but-HT-sibling of a dedicated core (shared, not dedicable);
  2. ★ suggestions only between containers of the SAME project that are wired together; « no room
     right now » when other containers hold the target's cores; nothing when the node is too small;
  3. ★ the plan is refused as a whole when a node lacks room — counting what the plan itself
     releases there (a move out, a shrink);
  4. ★ applying: a resize goes through `garantir` (hot); a move writes the reservation on the
     container, then migrates it; a refused move restores the container's previous reservation.

MUTATIONS, PROJET_PLAN_MUTATION=<name> — each must give rc=1:
    rendu_ignore     the room check forgets what the plan releases
    trop_petit       a node too small still gets a suggestion
    restaure_muet    a refused move leaves the new reservation on the container

    ./venv/bin/python tests/verif_projet_plan.py
"""
import os
import sys
import tempfile
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                  # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="projet_plan_db_"), "t.db")
config.DB_PATH = _DBF
database.DB_PATH = _DBF
database._tls.__dict__.pop("conn", None)
database.init_db()
assert database.DB_PATH == _DBF

from app import project_resources as PR, core_pool, etalonnage, migration   # noqa: E402

MUT = os.environ.get("PROJET_PLAN_MUTATION") or ""
MUTATIONS = {
    "rendu_ignore": (PR, '        available = nodes[nid]["dedicable"] + released.get(nid, 0)',
                     '        available = nodes[nid]["dedicable"]'),
    "trop_petit": (PR, "        if size.get(target, 0) < need:\n            continue",
                   "        if False:\n            continue"),
    "restaure_muet": (PR, '                    db.execute("UPDATE containers SET cores_override=?, cpu_weight_override=? WHERE vmid=?",\n'
                          '                               (before[0], before[1], p["vmid"]))',
                      '                    pass'),
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


# Node A: cpus 0-7, HT pairs (0,4) (1,5) (2,6) (3,7). Node B: cpus 0-3, no HT map. Node C: 0-1.
_HT = {1: {c: c % 4 for c in range(8)}}
core_pool.core_map_cached = lambda nid: _HT.get(nid, {})
core_pool.numa_map_cached = lambda nid: {}
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)
NA = database.db_add_node("A", "127.0.0.1")
NB = database.db_add_node("B", "127.0.0.2")
NC = database.db_add_node("C", "127.0.0.3")
database.db_update_node(NA, compute_cpuset="0-7")
database.db_update_node(NB, compute_cpuset="0-3")
database.db_update_node(NC, compute_cpuset="0-1")


def _cont(vmid, host, typ, node, params=None):
    database.db_upsert_container(vmid, host, 1, 512, "running")
    database.get_db().execute("UPDATE containers SET node_id=? WHERE vmid=?", (node, vmid))
    database.get_db().commit()
    database.db_update_deploy_config(vmid, typ, params or {})


def _alloc(node, vmid, cores):
    with database.get_db() as db:
        for c in cores:
            db.execute("INSERT INTO node_core_alloc (node_id, core, vmid) VALUES (?,?,?)", (node, c, vmid))
        db.commit()


print("\n1. Les cellules d'un nœud")
_cont(701, "p-mix", "mixer", NA)
_cont(702, "autre-mv", "multiview", NA)
_cont(703, "io", "2110_io", NA)
_alloc(NA, 701, [0])
_alloc(NA, 702, [1])
_alloc(NA, 703, [2])
carte = PR.node_map(NA, {701})
kinds = [c["kind"] for c in carte["cells"]]
ok(kinds == ["project", "other", "engine", "free", "ht_blocked", "ht_blocked", "ht_blocked", "free"],
   "1a. ★ projet / autre projet / moteur / libre / frère HT d'un dédié (non dédiable)", kinds)
ok(carte["cells"][0]["vmid"] == 701 and carte["cells"][1]["vmid"] is None,
   "1b. seul un cœur du projet porte un vmid", carte["cells"][:2])
ok(carte["cells"][1].get("owner") == "autre-mv" and "owner" not in carte["cells"][0],
   "1b'. un cœur d'un autre projet nomme son conteneur (infobulle)", carte["cells"][:2])
ok(carte["dedicable"] == 2, "1c. dédiable = libres hors frères HT (3 et 7)", carte["dedicable"])

print("\n2. ★ Suggestions de rapprochement")
noeuds = [PR.node_map(n, {701}) for n in (NA, NB, NC)]
rows = [{"vmid": 801, "hostname": "src", "node_id": NA, "guarantee": "shared", "type": "mixer", "alloc": 0, "proposed": None},
        {"vmid": 802, "hostname": "dst", "node_id": NB, "guarantee": "shared", "type": "streamer", "alloc": 0, "proposed": None},
        {"vmid": 803, "hostname": "dst2", "node_id": NB, "guarantee": "shared", "type": "streamer", "alloc": 0, "proposed": None}]
s = PR.move_suggestions(rows, noeuds, [(801, 802), (801, 803)])
ok(len(s) == 1 and s[0]["vmid"] == 801 and s[0]["to"] == NB and s[0]["fits"] is True,
   "2a. ★ câblé à 2 conteneurs sur B → on propose de rapprocher le conteneur isolé sur B, avec la place", s)
rows[0]["proposed"] = 4
_alloc(NB, 999, [0, 1])
noeuds = [PR.node_map(n, {701}) for n in (NA, NB, NC)]
s = PR.move_suggestions(rows, noeuds, [(801, 802), (801, 803)])
ok(len(s) == 1 and s[0]["fits"] is False, "2b. ★ B a 4 cœurs mais 2 sont tenus par d'autres → « pertinent, pas la place »", s)
rows2 = [dict(rows[0], proposed=3), dict(rows[1], node_id=NC), dict(rows[2], node_id=NC)]
s = PR.move_suggestions(rows2, noeuds, [(801, 802), (801, 803)])
ok(not any(x["to"] == NC for x in s), "2c. ★ C n'a que 2 cœurs pour 3 demandés → rien n'est proposé vers C", s)
ok(PR.move_suggestions(rows, noeuds, []) == [], "2d. sans câble entre eux, aucune suggestion")

print("\n3. ★ Le plan est refusé en bloc s'il ne tient pas — ce qu'il libère compte")
pid = database.get_db().execute("INSERT INTO projects (name, snapshot) VALUES ('Atelier', '[]')").lastrowid
database.get_db().commit()
for v in (701, 704):
    if v == 704:
        _cont(704, "p-cam", "avsync", NB)
    database.db_set_project(v, pid)
etalonnage.ressources = lambda vmids=None, **k: [
    {"vmid": v, "hostname": h, "node_id": n, "type": t, "reserve_pct": None, "etat": "non_etalonne", "level": "dedicated"}
    for v, h, n, t in ((701, "p-mix", NA, "mixer"), (704, "p-cam", NB, "avsync")) if vmids is None or v in vmids]
okp, res = PR.check_plan(pid, [{"vmid": 701, "cores": 3, "node_id": NA}])
ok(okp, "3a. agrandir le mixer de 1 → 3 sur A : 2 dédiables + son propre cœur rendu = 3", res)
okp, res = PR.check_plan(pid, [{"vmid": 701, "cores": 4, "node_id": NA}])
ok(not okp and res["errors"][0]["error"] == "capacity" and res["errors"][0]["available"] == 3,
   "3b. 4 cœurs sur A → refus qui dit 3 disponibles", res)
okp, res = PR.check_plan(pid, [{"vmid": 701, "cores": 1, "node_id": NB}, {"vmid": 704, "cores": 3, "node_id": NA}])
ok(okp, "3c. ★ croisé : le mixer quitte A (rend 1 cœur), la caméra y prend 3 — ça tient", res)
okp, res = PR.check_plan(pid, [{"vmid": 999, "cores": 1}])
ok(not okp and res["errors"][0]["error"] == "not_in_project", "3d. un conteneur hors du projet est refusé", res)

print("\n4. ★ Application")
appels = []
etalonnage.garantir = lambda vmid, coeurs=None, marge=None: (appels.append(("garantir", vmid, coeurs)) or True, {"coeurs": coeurs})
_migr = {"ok": False}


def _faux_migrer(vmid, node):
    c = database.db_get_container(vmid) or {}
    appels.append(("migrer", vmid, node, c.get("cores_override"), c.get("cpu_weight_override")))
    return {"ok": _migr["ok"], "erreur": None if _migr["ok"] else "refusée"}


migration.migrer = _faux_migrer


def _attendre():
    for _ in range(100):
        if PR.plan_status(pid)["state"] != "running":
            return
        time.sleep(0.05)


okp, res = PR.apply_plan(pid, [{"vmid": 704, "cores": 1, "node_id": NA}, {"vmid": 701, "cores": 2, "node_id": NA}])
_attendre()
ok(okp, "4. décor : le plan tient (3 cœurs sur A pour 3 disponibles)", res)
ok(appels[:1] == [("garantir", 701, 2)], "4a. ★ l'agrandissement sur place passe EN PREMIER, à chaud (garantir)", appels)
# 2026-11-07 : la place suit le niveau — la réservation s'écrit « Antenne » sur le conteneur.
ok(appels[1:2] == [("migrer", 704, NA, 1, "dedicated")],
   "4b. ★ le déplacement : réservation écrite sur le conteneur AVANT la migration", appels)
c = database.db_get_container(704) or {}
ok(c.get("cores_override") is None and c.get("cpu_weight_override") is None,
   "4c. ★ migration refusée → la réservation d'avant est remise", (c.get("cores_override"), c.get("cpu_weight_override")))
et = PR.plan_status(pid)
ok(et["state"] == "done" and [e["ok"] for e in et["steps"]] == [True, False],
   "4d. le suivi dit ce qui a réussi et ce qui a échoué, étape par étape", et)

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — schéma, suggestions, plan vérifié en bloc, application")
