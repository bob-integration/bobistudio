#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Projet rechargé : ses conteneurs ont de NOUVEAUX vmid, et tout doit le savoir (28/10).

Recette custom-f610d405bc (Z8) : page Containers, filtre Projet = Atelier → « 0 / 5 », alors que
les 5 conteneurs portaient project_id. `restaurer_projet` les recrée sous de nouveaux vmid, mais
le snapshot citait les anciens, et `_attach_projects` ne lisait que le snapshot.

Vérifié hors ligne (base jetable ; création et déploiement simulés) :
  1. après rechargement, le snapshot cite les vmid créés, avec leur instance_uuid ;
  2. une entrée en échec garde son vmid d'origine (celui que « Réessayer » cite) ;
  3. `_attach_projects` met le projet dans `projects` d'un conteneur rattaché par project_id seul ;
  4. … sans doublon quand le snapshot le cite aussi ;
  5. la carte Composition compare des ids (`projects` est une liste d'objets).

⚠ Garde MUTÉE à l'écriture.
"""
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                  # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="projet_recharge_db_"), "t.db")
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


from app import projects, docker_compute       # noqa: E402
from app.routes import _attach_projects        # noqa: E402

# Snapshot d'origine : deux correcteurs, 205 et 206. Le second échouera à la création.
snap = [{"vmid": 205, "hostname": "cc-a", "instance_uuid": "old-a",
         "deploy_config": {"type": "color_corrector", "params": {}}},
        {"vmid": 206, "hostname": "cc-b", "instance_uuid": "old-b",
         "deploy_config": {"type": "color_corrector", "params": {}}}]
pid = database.get_db().execute("INSERT INTO projects (name, snapshot) VALUES ('Atelier', '[]')").lastrowid
database.get_db().commit()
database.db_update_project_snapshot(pid, snap)
database.get_db().execute("UPDATE projects SET media_path=? WHERE id=?", (tempfile.mkdtemp(), pid))
database.get_db().commit()

_next = [213]


def _fake_create(node_id, deploy_type, hostname=None):
    if hostname and hostname.endswith("cc-b"):
        return None
    v = _next[0]
    _next[0] += 1
    database.db_upsert_container(v, hostname, 1, 512, "running")
    return v


docker_compute.pick_compute_node = lambda *a, **k: 1
docker_compute.creer_container_compute = _fake_create
projects.deployer_script = lambda *a, **k: True
projects.planifier_restore = lambda snapshot: {"can_restore": True, "remaps": [],
                                               "hostname_conflicts": [], "shm_conflicts": []}

res = projects.restaurer_projet(pid)
p = database.db_get_project(pid)
by_host = {sc["hostname"].split("-", 1)[-1] if "-" in sc["hostname"] else sc["hostname"]: sc
           for sc in p["snapshot"]}
vmids = sorted(sc["vmid"] for sc in p["snapshot"])
new = database.db_get_container(213) or {}

ok(res.get("ok") == 1 and len(res.get("failed") or []) == 1, "0. rechargement : 1 créé, 1 en échec", res)
ok(213 in vmids, "1. le snapshot cite le vmid créé", vmids)
sc213 = next((sc for sc in p["snapshot"] if sc["vmid"] == 213), {})
ok(sc213.get("instance_uuid") == new.get("instance_uuid") and new.get("instance_uuid"),
   "1b. … avec l'instance_uuid du conteneur créé", (sc213.get("instance_uuid"), new.get("instance_uuid")))
ok(206 in vmids and 205 not in vmids, "2. l'entrée en échec garde son vmid d'origine ; l'ancien a disparu", vmids)

# 3. Rattaché par project_id seul (le snapshot ne le cite pas).
database.db_upsert_container(300, "seul", 1, 512, "running")
database.db_set_project(300, pid)
rows = {c["vmid"]: c for c in _attach_projects(database.db_get_containers())}
ok([x["id"] for x in rows[300]["projects"]] == [pid], "3. project_id seul → projet dans `projects`",
   rows[300]["projects"])
ok([x["id"] for x in rows[213]["projects"]] == [pid], "4. cité par le snapshot ET rattaché : une fois",
   rows[213]["projects"])

# 5. Carte Composition : `projects` est une liste d'objets {id, name}.
html = open(os.path.join(RACINE, "templates", "projects.html"), encoding="utf-8").read()
m = re.search(r"function _compoMembersOf\(pid\) \{(.*?)\n\}", html, re.S)
body = m.group(1) if m else ""
ok(".includes(pid)" not in body and re.search(r"\.some\(\s*\w+\s*=>\s*\w+\.id\s*===\s*pid\s*\)", body),
   "5. Composition compare l'id des projets", body.strip()[:160])

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — projet rechargé : vmid à jour partout")
