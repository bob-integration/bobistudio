#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Sortir un conteneur VIVANT d'un projet (cas réel : DeckLink #231, projet Atelier, Z8, 30/10).

Avant : PATCH /api/containers/<vmid>/project mettait project_id à NULL, mais resnapshot_projet
garde tout vmid VIVANT de l'ancien snapshot — le conteneur restait dans le projet (et les droits
le lisaient là). Vérifié hors ligne (base jetable, redéploiement simulé) :
  1. le défaut existe toujours dans resnapshot seul (témoin du mécanisme) ;
  2. le changement de projet vers « aucun » le retire du snapshot, en version automatique,
     et les droits (vmid_project_ids) ne le voient plus dans le projet ;
  3. un re-snapshot ultérieur ne le ramène pas ;
  4. les références restantes sont signalées (vue, entrée qui suit un port, port qui publie
     sa sortie), sans être effacées ;
  5. vers un AUTRE projet : retiré de l'ancien, gardé dans le nouveau ;
  6. redéploiement en échec : rien ne bouge.

⚠ Garde MUTÉE à l'écriture.
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
os.chdir(RACINE)

from app import config, database                  # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="projet_sortie_db_"), "t.db")
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


from app import auth, containers, deploy, projects   # noqa: E402

for v, hn, t in ((231, "Atelier-decklink-io", "decklink_io"), (232, "Atelier-mixer", "mixer"),
                 (233, "Atelier-mv", "multiview")):
    database.db_upsert_container(v, hn)
    database.db_update_deploy_config(v, t, {"hostname": hn})
    database.db_set_instance_uuid(v, "u-%d" % v)
ATELIER = database.db_save_project("Atelier", [231, 232, 233])
AUTRE = database.db_save_project("Autre", [232, 233])   # 232 déjà dans les deux snapshots
for v in (231, 232):
    database.db_set_project(v, ATELIER)
database.db_set_project(233, AUTRE)

snap = lambda pid: sorted(c.get("vmid") for c in database.db_get_project(pid).get("snapshot") or [])
nver = lambda pid: len(database.db_project_versions(pid))
def droits(v):
    auth._snap_map_cache["ts"] = 0.0
    return auth.vmid_project_ids(v)

# Références dans Atelier : une vue, une entrée du mixer qui suit un port, un port lié à la sortie de #231.
database.db_create_view(ATELIER, "Régie", None, layout=[{"id": "w1", "instance_uuid": "u-231", "vmid": 231}])
CAM = database.db_create_port(ATELIER, "source", "video", "Cam 1")
database.db_update_port(CAM, binding={"shm": "deck_in1"})
database.db_port_link_set(CAM, "u-232", "video", 0, "deck_in1")
# ⚠ un port lu par le conteneur qui SORT : signalé aussi
database.db_port_link_set(CAM, "u-231", "video", 0, "deck_in1")
auth.shm_producer_vmids = lambda shms: {s: {231} for s in shms if s == "deck_in1"}
deploy.deployer_script = lambda *a, **k: True        # « déjà conforme »
alertes = []
containers.ajouter_alerte = lambda m, n="info", **k: alertes.append((m, k.get("params")))

# 1.
database.db_set_project(231, None)
projects.resnapshot_projet(ATELIER)
ok(231 in snap(ATELIER), "1. témoin : resnapshot seul garde un vmid vivant de l'ancien snapshot", snap(ATELIER))
database.db_set_project(231, ATELIER)

# 2.
v0 = nver(ATELIER)
ok(containers.changer_media_projet(231, None, None), "2. changement de projet vers « aucun » réussi")
ok(snap(ATELIER) == [232, 233] and database.db_get_container(231).get("project_id") is None,
   "2b. #231 sorti du snapshot d'Atelier", snap(ATELIER))
import json                                       # noqa: E402
with database.get_db() as _db:
    _last = _db.execute("SELECT snapshot FROM project_versions WHERE project_id=? ORDER BY id DESC LIMIT 1",
                        (ATELIER,)).fetchone()
ok(nver(ATELIER) == v0 + 1 and 231 in [c.get("vmid") for c in json.loads(_last["snapshot"])],
   "2c. l'ancien snapshot est gardé en version automatique", (v0, nver(ATELIER)))
ok(ATELIER not in droits(231), "2d. les droits ne le voient plus dans Atelier", droits(231))

# 3.
projects.resnapshot_projet(ATELIER, force=True)
ok(231 not in snap(ATELIER), "3. un re-snapshot ne le ramène pas", snap(ATELIER))

# 4.
refs = [p for m, p in alertes if m == "alert.deploy.projet_retire_refs"]
ok(len(refs) == 1 and refs[0]["project"] == "Atelier" and refs[0]["views"] == "Régie"
   and refs[0]["links"] == 1 and refs[0]["bindings"] == "Cam 1",
   "4. références signalées : vue, entrée qui suit un port du projet, port qui publie sa sortie", alertes)
ok(len(database.db_port_links(port_id=CAM)) == 2 and database.db_project_views(ATELIER)[0]["layout"],
   "4b. … sans être effacées")

# 5.
alertes.clear()
ok(containers.changer_media_projet(232, AUTRE, None) and 232 not in snap(ATELIER) and 232 in snap(AUTRE),
   "5. vers un autre projet : retiré de l'ancien, gardé dans le nouveau", (snap(ATELIER), snap(AUTRE)))
ok(not [p for m, p in alertes if m.startswith("alert.deploy.projet_retir") and p["project"] == "Autre"],
   "5b. … pas du nouveau", alertes)

# 6.
deploy.deployer_script = lambda *a, **k: False
ok(not containers.changer_media_projet(233, None, None) and 233 in snap(AUTRE)
   and database.db_get_container(233).get("project_id") == AUTRE,
   "6. redéploiement en échec : rien ne bouge", (snap(AUTRE), database.db_get_container(233).get("project_id")))

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — un conteneur vivant sort de son projet")
