#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Démarrer un projet : placement nœud par nœud (2026-11-07).

Avant : chaque conteneur retournait sur son ANCIEN nœud dès que ce nœud était éligible, sans
regarder s'il y restait de la place, et le pré-vol ne comptait que le TOTAL du cluster (« ça
tient » alors qu'aucun nœud ne prend le conteneur). Le type n'était pas passé au choix du nœud.

Vérifié hors ligne (base jetable ; nœuds, cœurs libres et GPU simulés) :
  1. ancien nœud avec de la place : il est gardé ;
  2. ancien nœud plein : le conteneur va là où il tient ;
  3. deux conteneurs ne comptent pas deux fois sur les mêmes cœurs libres ;
  4. choix à la main honoré, même s'il déborde (le nœud est alors signalé) ;
  5. un nœud déjà en dépassement qui ne reçoit rien ne fait pas échouer le plan ;
  6. affinité GPU dans les deux sens ;
  7. la restauration passe le nœud du plan (et le type) à `pick_compute_node` ;
  8. « Arrêter » enregistre la configuration AVANT de passer en déchargement.

⚠ Garde MUTÉE à l'écriture.
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
os.chdir(RACINE)

from app import config, database                  # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="projet_placement_db_"), "t.db")
config.DB_PATH = _DBF
database.DB_PATH = _DBF
database._tls.__dict__.pop("conn", None)
database.init_db()
assert database.DB_PATH == _DBF

ECHECS = []


def ok(cond, libelle, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond else "  [%s]" % (detail,)))
    if not cond:
        ECHECS.append(libelle)


from app import projects, core_pool, gpu_pool, plugins, cpu_profiles, docker_compute   # noqa: E402

NODES = {1: {"id": 1, "name": "n1"}, 2: {"id": 2, "name": "n2"}, 3: {"id": 3, "name": "n3"}}
FREE = {}
GPUS = {}
docker_compute.eligible_compute_nodes = lambda: list(NODES.values())
core_pool.capacite_placement = lambda nid: {"libres": FREE[nid]}
gpu_pool.gpu_status = lambda nid: {"count": GPUS.get(nid, 0), "used": 0}
plugins.runtime = lambda t: "docker"
plugins.gpu_capable = lambda t: t == "multiview"
cpu_profiles.cout_estime = lambda t: (None, None)


def snap(vmid, t, node, cores):
    return {"vmid": vmid, "hostname": "h%d" % vmid, "node_id": node, "cores": cores,
            "deploy_config": {"type": t, "params": {}}}


def where(plan):
    return {r["key"]: (r["node_id"], r["how"]) for r in plan["containers"]}


print("1. Ancien nœud avec de la place")
FREE.update({1: 4, 2: 8, 3: 0})
pl = projects.plan_placement([snap(10, "mixer", 1, 2)])
ok(where(pl)["10"] == (1, "previous") and pl["fits"], "gardé sur n1", where(pl))

print("2. Ancien nœud plein")
FREE.update({1: 1, 2: 8, 3: 0})
pl = projects.plan_placement([snap(10, "mixer", 1, 2)])
ok(where(pl)["10"] == (2, "auto") and pl["fits"], "envoyé sur n2 (n1 n'a qu'un cœur)", where(pl))

print("3. Pas de double compte")
FREE.update({1: 3, 2: 2, 3: 0})
pl = projects.plan_placement([snap(10, "mixer", 1, 2), snap(11, "mixer", 1, 2)])
ok(where(pl)["10"] == (1, "previous") and where(pl)["11"] == (2, "auto") and pl["fits"],
   "le 2e ne compte plus sur les cœurs pris par le 1er", where(pl))

print("4. Choix à la main")
FREE.update({1: 3, 2: 8, 3: 0})
pl = projects.plan_placement([snap(10, "mixer", 1, 2), snap(11, "mixer", 1, 2)],
                             {"10": 3, "11": "2"})
n3 = next(n for n in pl["nodes"] if n["id"] == 3)
ok(where(pl) == {"10": (3, "manual"), "11": (2, "manual")} and not n3["ok"] and not pl["fits"],
   "honoré ; n3 déborde et le plan le dit", (where(pl), pl["nodes"]))

print("5. Nœud déjà en dépassement, rien à recevoir")
FREE.update({1: -11, 2: 8, 3: 0})
pl = projects.plan_placement([snap(10, "mixer", 2, 2)])
ok(pl["fits"] and where(pl)["10"] == (2, "previous"), "le plan tient (n1 ne reçoit rien)", pl["nodes"])

print("6. Affinité GPU")
FREE.update({1: 8, 2: 8, 3: 8})
GPUS.update({3: 1})
pl = projects.plan_placement([snap(10, "multiview", None, 1), snap(11, "mixer", None, 1)])
ok(where(pl)["10"][0] == 3 and where(pl)["11"][0] != 3, "multiview → n3 (GPU) ; mixer l'évite", where(pl))
GPUS.clear()

print("7. La restauration suit le plan")
for v, hn, t in ((101, "a", "mixer"), (102, "b", "stills")):
    database.db_upsert_container(v, hn)
    database.db_update_deploy_config(v, t, {"hostname": hn})
PID = database.db_save_project("Atelier", [101, 102])
p = database.db_get_project(PID)
database.db_update_project_snapshot(PID, [dict(c, node_id=1, cores=2) for c in p["snapshot"]])
with database.get_db() as db:
    db.execute("DELETE FROM containers")
    db.commit()
FREE.update({1: 3, 2: 8, 3: 8})
PICKS = []
docker_compute.pick_compute_node = lambda prefer_id=None, deploy_type=None: (PICKS.append((prefer_id, deploy_type)) or None)
projects.restaurer_projet(PID, placement={"102": 3})
ok(sorted(PICKS) == [(1, "mixer"), (3, "stills")],
   "a → ancien nœud n1 (place) ; b → n3 choisi à la main ; type transmis", PICKS)

print("8. Arrêter : enregistrer avant de décharger")
SEEN = []
projects.resnapshot_projet = lambda pid, **k: SEEN.append(database.db_get_project(pid).get("state"))
projects.detruire_containers_projet(PID)
ok(SEEN and SEEN[0] not in ("unloading",), "re-snapshot appelé AVANT l'état « unloading »", SEEN)

print()
if ECHECS:
    print("ÉCHEC : %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("OK")
