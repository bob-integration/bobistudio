#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Câble depuis une virtuelle SANS flux encore : il existe dans le projet (Cyril, 30/10).

Câbler une entrée sur une source non affectée pose le lien et VIDE l'entrée ; la page Câbles
ne dessinait que les entrées qui lisent un flux — le câble n'apparaissait nulle part.
Vérifié hors ligne (base jetable) :
  1. câblage par la vraie route de port (`wire_port`) : lien posé, entrée vide ;
  2. la vue détaillée émet un consommateur « en attente » qui part du port, sur LE bon slot ;
  3. l'entrée porte le nom de la virtuelle, marquée en attente ; le slot voisin non ;
  4. entrée recâblée ailleurs entre-temps : plus de câble en attente ;
  5. l'arête qui en sort est « non alimentée », et la page nomme l'entrée « (sans source) ».

⚠ Garde MUTÉE à l'écriture.
"""
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
os.chdir(RACINE)

from app import config, database                  # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="ports_sans_source_db_"), "t.db")
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


database.db_upsert_container(101, "mixer")
database.db_update_deploy_config(101, "mixer", {"hostname": "mixer"})
database.db_set_instance_uuid(101, "u-101")
PID = database.db_save_project("P", [101])
database.db_set_project(101, PID)
CAM = database.db_create_port(PID, "source", "video", "Cam 1")

from app import project_ports                     # noqa: E402
from app.routes import home_dashboard             # noqa: E402
APPLY = []
project_ports._apply = lambda vmid, kind, slot, shm, *a, **k: (APPLY.append((vmid, kind, slot, shm)) or (True, 200, {}))

# 1.
ok_, st, pl = project_ports.wire_port(CAM, 101, "video", 1)
links = database.db_port_links(port_id=CAM)
ok(ok_ and pl.get("port_unassigned") and APPLY == [(101, "video", 1, "")] and len(links) == 1
   and links[0]["shm"] == "" and links[0]["to_slot"] == 1,
   "1. câblé sur une source non affectée : lien posé, entrée 1 vidée", (ok_, pl, APPLY, links))


def vue(slot1_shm=""):
    nodes = [{"vmid": 101, "hostname": "mixer", "kind": "mixer", "col": "composition", "produces": [],
              "consumes": [{"shm": "", "kind": "video", "slot": 0},
                           {"shm": slot1_shm, "kind": "video", "slot": 1}]}]
    cons = [{"vmid": 101, "shm": slot1_shm, "kind": "video", "slot": 1}] if slot1_shm else []
    return home_dashboard._project_port_nodes(nodes, cons, database.db_get_containers(), {})


# 2.
nodes, cons = vue()
pend = [c for c in cons if c.get("pending")]
ok(len(pend) == 1 and pend[0]["slot"] == 1 and pend[0]["shm"] == ""
   and pend[0]["via_port"] == {"vmid": -PID, "port_id": CAM, "ref": "port:%d" % CAM},
   "2. un câble en attente part du port vers l'entrée 1", cons)
# 3.
mx = next(n for n in nodes if n["vmid"] == 101)
e0, e1 = mx["consumes"]
ok((e1.get("via_port") or {}).get("name") == "Cam 1" and e1["via_port"].get("pending") and not e0.get("via_port"),
   "3. l'entrée 1 porte « Cam 1 » en attente, l'entrée 0 rien", mx["consumes"])
# 4.
_, cons4 = vue("autre_flux")
ok(not [c for c in cons4 if c.get("pending")] and not [c for c in cons4 if c.get("via_port")],
   "4. entrée recâblée ailleurs : plus de câble en attente", cons4)

# 5.
src = open(os.path.join(RACINE, "app", "routes", "home_dashboard.py"), encoding="utf-8").read()
ok(re.search(r'\{"pending": True, "fed": False\} if cn\.get\("pending"\)', src),
   "5. l'arête d'un câble en attente est « non alimentée »")
tpl = open(os.path.join(RACINE, "templates", "cables.html"), encoding="utf-8").read()
ok(re.search(r"vp\.pending\) return _renderPort_orig\(side, \{ \.\.\.port, disconnected: false,\s*"
             r"shm_label: `\$\{txt\} \(\$\{T\('cables\.proj\.no_source'\)\}\)`", tpl),
   "5b. la page nomme l'entrée « Cam 1 (sans source) »")

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — un câble depuis une virtuelle sans source existe dans le projet")
