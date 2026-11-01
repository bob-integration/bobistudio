#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""« Faire entrer cette source par un port du projet » (TODO de Cyril, point E du chantier
« ports jamais contournés », 31/10).

Au lieu du seul refus 403 « source hors projet », la page Câbles propose de faire entrer le flux
par un port source du projet. Vérifié hors ligne (base jetable, câblage simulé) :
  1. le refus de la frontière dit QUEL projet (la page en a besoin pour proposer) ;
  2. un administrateur fait entrer le flux : port source créé, nommé d'après la source, affecté au
     flux, l'entrée câblée dessus et liée ;
  3. une seconde entrée sur le même flux réutilise le même port (pas de doublon) ;
  4. affecter une source reste réservé aux accès globaux (403 pour un éditeur du projet) ;
  5. un conteneur hors du projet est refusé ;
  6. la page ouvre la proposition sur ce refus, et appelle la route.

⚠ Garde MUTÉE à l'écriture.
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
os.chdir(RACINE)

from app import config, database                  # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="ports_admettre_db_"), "t.db")
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


from app.auth import hash_password                # noqa: E402
admin = database.db_create_user("admin_t", hash_password("x"), "admin")
ed = database.db_create_user("ed_t", hash_password("x"), "viewer")
for v, hn in ((101, "mx"), (102, "autre")):
    database.db_upsert_container(v, hn)
    database.db_update_deploy_config(v, "mixer", {"hostname": hn, "n_inputs": 4})
    database.db_set_instance_uuid(v, "u-%d" % v)
PID = database.db_save_project("P", [101])
database.db_set_project(101, PID)
database.db_set_project_member(PID, ed, "editor")
from app import auth, project_ports               # noqa: E402
auth._vmid_project_cache.clear()
auth._snap_map_cache["ts"] = 0.0
database.db_upsert_source_label("studio_cam", {"label_2": "Caméra studio"})

APPLY = []
project_ports._apply = lambda vmid, kind, slot, shm, *a, **k: (APPLY.append((vmid, kind, slot, shm)) or (True, 200, {}))

# 1.
from app.routes import cabling                    # noqa: E402
okw, st, pl = cabling._apply_wire(None, 101, "studio_cam", "video", 0)
ok(not okw and st == 403 and pl.get("reason") == "shm_not_in_project" and pl.get("project_id") == PID,
   "1. le refus de la frontière dit quel projet", pl)

import main as app_main                           # noqa: E402
app = app_main.app
app.config["TESTING"] = True


def client(uid, name):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
        s["username"] = name
    return c


ADM, ED = client(admin, "admin_t"), client(ed, "ed_t")
url = "/api/projects/%d/ports/admit" % PID
# 2.
r = ADM.post(url, json={"shm": "studio_cam", "kind": "video", "to_vmid": 101, "to_slot": 1})
j = r.get_json() or {}
port = database.db_get_port(j.get("port_id") or 0) or {}
links = database.db_port_links(port_id=port.get("id") or 0)
ok(r.status_code == 200 and j.get("created") and port.get("kind") == "source" and port.get("name") == "Caméra studio"
   and port.get("binding") == {"shm": "studio_cam"} and APPLY[-1:] == [(101, "video", 1, "studio_cam")]
   and [(l["consumer_uuid"], l["to_slot"]) for l in links] == [("u-101", 1)],
   "2. port source créé (nommé d'après la source), affecté, entrée câblée et liée", (r.status_code, j, port, links))
# 3.
r = ADM.post(url, json={"shm": "studio_cam", "kind": "video", "to_vmid": 101, "to_slot": 2})
j3 = r.get_json() or {}
ok(r.status_code == 200 and not j3.get("created") and j3.get("port_id") == port.get("id")
   and len([p for p in database.db_project_ports(PID) if p["kind"] == "source"]) == 1,
   "3. seconde entrée sur le même flux : même port, pas de doublon", j3)
# 4.
r = ED.post(url, json={"shm": "autre_flux", "kind": "video", "to_vmid": 101, "to_slot": 3})
ok(r.status_code == 403 and (r.get_json() or {}).get("reason") == "binding_requires_admin",
   "4. éditeur du projet : affecter une source reste réservé aux accès globaux", (r.status_code, r.get_json()))
# 5.
r = ADM.post(url, json={"shm": "studio_cam", "kind": "video", "to_vmid": 102, "to_slot": 0})
ok(r.status_code == 400, "5. conteneur hors du projet : refusé", r.status_code)

# 6.
src = open(os.path.join(RACINE, "templates", "cables.html"), encoding="utf-8").read()
ok("j.project_id != null && payload.shm) { openAdmitModal(j.project_id, payload, target, targetVmid)" in src
   and "/api/projects/${pid}/ports/admit" in src,
   "6. la page propose sur le refus, et appelle la route")

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — une source hors projet peut entrer par un port du projet")
