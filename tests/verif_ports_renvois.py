#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Renvois : une destination marquée « réutilisable » est aussi une source dans son projet
(décisions de Cyril, 29/10 — maquette B).

Vérifié hors ligne (base jetable ; les producteurs de flux sont simulés) :
  1. le drapeau se pose sur une destination de tête seulement (ni source, ni rattaché) ;
  2. dans la vue Câbles du projet, les destinations réutilisables — et elles seules — s'ajoutent
     à la carte des sources, APRÈS les sources externes, avec leurs rattachés, ce qui les
     alimente et le nombre d'entrées qui les suivent ;
  3. boucle refusée dans les deux sens : un conteneur ne suit pas une destination qu'il alimente,
     une destination ne publie pas la sortie d'un conteneur qui la suit ;
  4. la page coupe la carte en « Externes » puis « Renvois du projet » (rendu exécuté sous Node).

⚠ Garde MUTÉE à l'écriture.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
os.chdir(RACINE)

from app import config, database                  # noqa: E402
_DBF = os.path.join(tempfile.mkdtemp(prefix="ports_renvois_db_"), "t.db")
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
uid = database.db_create_user("admin_t", hash_password("x"), "admin")
for v, hn, t in ((101, "mixer", "mixer"), (102, "mv", "multiview"), (103, "enc", "streamer")):
    database.db_upsert_container(v, hn)
    database.db_update_deploy_config(v, t, {"hostname": hn})
    database.db_set_instance_uuid(v, "u-%d" % v)
PID = database.db_save_project("P", [101, 102, 103])

import main as app_main                           # noqa: E402
from app import project_ports                     # noqa: E402
from app.routes import home_dashboard             # noqa: E402
app = app_main.app
app.config["TESTING"] = True
cli = app.test_client()
with cli.session_transaction() as s:
    s["user_id"] = uid
    s["username"] = "admin_t"

r = cli.post("/api/projects/%d/ports/batch" % PID,
             json={"kind": "dest", "names": ["PGM"], "video": True, "audios": 1, "anc": False})
r2 = cli.post("/api/projects/%d/ports/batch" % PID,
              json={"kind": "dest", "names": ["Clean", "Aux"], "video": True, "audios": 0, "anc": False})
r3 = cli.post("/api/projects/%d/ports/batch" % PID,
              json={"kind": "source", "names": ["Cam 1"], "video": True, "audios": 0, "anc": False})
P = {p["name"]: p for p in database.db_project_ports(PID)}
ok(all(x.status_code == 200 for x in (r, r2, r3)) and {"PGM", "PGM A1", "Clean", "Aux", "Cam 1"} <= set(P),
   "ports créés", sorted(P))
PGM, PGMA1, CLEAN, AUX, CAM = (P[n]["id"] for n in ("PGM", "PGM A1", "Clean", "Aux", "Cam 1"))

# 1.
patch = lambda pid_, body: cli.patch("/api/projects/%d/ports/%d" % (PID, pid_), json=body)
r = patch(PGM, {"reusable": True})
ok(r.status_code == 200 and database.db_get_port(PGM).get("reusable") == 1, "1. destination de tête : marquée", r.status_code)
patch(CLEAN, {"reusable": True})
ok(patch(CAM, {"reusable": True}).status_code == 400, "1b. une source ne se marque pas")
ok(patch(PGMA1, {"reusable": True}).status_code == 400, "1c. un rattaché ne se marque pas (il suit sa vidéo)")
ok(project_ports.is_reusable(database.db_get_port(PGMA1)), "1d. … mais il est réutilisable avec elle")

# Le mixer publie PGM / PGM audio / Clean ; le multiview suit PGM et PGM A1.
SHM_PROD = {"mx_pgm": 101, "mx_pgm_a": 101, "mx_clean": 101, "mv_out": 102}
project_ports.producer_vmid = lambda shm: SHM_PROD.get(shm)
database.db_update_port(PGM, binding={"internal_shm": "mx_pgm"})
database.db_update_port(PGMA1, binding={"internal_shm": "mx_pgm_a"})
database.db_update_port(CLEAN, binding={"internal_shm": "mx_clean"})
database.db_port_link_set(PGM, "u-102", "video", 0, "mx_pgm")
database.db_port_link_set(PGM, "u-103", "video", 0, "mx_pgm")
database.db_port_link_set(PGMA1, "u-102", "audio", 0, "mx_pgm_a")

# 2.
def node(vmid, hn, produces=(), consumes=()):
    return {"vmid": vmid, "hostname": hn, "kind": "x", "col": "composition",
            "produces": [dict(p) for p in produces], "consumes": [dict(c) for c in consumes]}


NODES = [node(101, "Mixer", produces=[{"shm": "mx_pgm", "kind": "video", "label": "PGM"},
                                      {"shm": "mx_pgm_a", "kind": "audio", "label": "PGM audio"},
                                      {"shm": "mx_clean", "kind": "video", "label": "Clean"}]),
         node(102, "MV", consumes=[{"shm": "mx_pgm", "kind": "video", "slot": 0},
                                   {"shm": "mx_pgm_a", "kind": "audio", "slot": 0}]),
         node(103, "Enc", consumes=[{"shm": "mx_pgm", "kind": "video", "slot": 0}])]
PRODUCERS = {"mx_pgm": [{"vmid": 101, "kind": "video"}], "mx_pgm_a": [{"vmid": 101, "kind": "audio"}],
             "mx_clean": [{"vmid": 101, "kind": "video"}]}
CONS = [{"vmid": n["vmid"], "shm": c["shm"], "kind": c["kind"], "slot": c["slot"]}
        for n in NODES for c in n["consumes"]]
nodes, cons = home_dashboard._project_port_nodes([dict(n) for n in NODES], [dict(c) for c in CONS],
                                                 database.db_get_containers(), PRODUCERS)
src_card = next((n for n in nodes if n["vmid"] == -PID and n.get("col") == "sources"), {})
outs = src_card.get("produces") or []
ids = [o["port_id"] for o in outs]
rv = {o["port_id"]: o for o in outs if o.get("renvoi")}
ok(set(rv) == {PGM, PGMA1, CLEAN}, "2. renvois = les destinations marquées et leurs rattachés, seulement",
   sorted(rv))
ok(ids and ids[0] == CAM and all(ids.index(CAM) < ids.index(i) for i in rv),
   "2b. après les sources externes", ids)
ok(rv.get(PGM, {}).get("shm") == "port:%d" % PGM and rv[PGM].get("origin") == {"host": "Mixer", "out": "PGM"}
   and rv[PGM].get("readers") == 2 and rv[PGMA1].get("parent_port_id") == PGM,
   "2c. adressé port:<id>, alimenté par « Mixer · PGM », 2 lecteurs, rattaché sous sa vidéo", rv.get(PGM))
via = {(c["vmid"], c["kind"]): c.get("via_port") for c in cons if c["vmid"] > 0}
ok((via.get((102, "video")) or {}).get("vmid") == -PID and (via.get((102, "audio")) or {}).get("port_id") == PGMA1,
   "2d. les câbles des lecteurs partent du renvoi", via)

# 3.
_apply0 = project_ports._apply
project_ports._apply = lambda *a, **k: (True, 200, {})   # sans la garde, le câblage passerait
ok_, st, pl = project_ports.wire_port(PGM, 101, "video", 0)
project_ports._apply = _apply0
ok(not ok_ and st == 409 and (pl or {}).get("reason") == "port_loop",
   "3. le mixer ne suit pas PGM, qu'il alimente lui-même", (ok_, st, pl))
r = patch(PGM, {"binding": {"internal_shm": "mv_out"}})
ok(r.status_code == 409 and (r.get_json() or {}).get("reason") == "port_loop"
   and database.db_get_port(PGM)["binding"] == {"internal_shm": "mx_pgm"},
   "3b. PGM ne publie pas la sortie du multiview, qui le suit — rien n'a changé", (r.status_code, r.get_json()))

# 4.
src = open(os.path.join(RACINE, "templates", "cables.html"), encoding="utf-8").read()
fns = "".join((re.search(r"\nfunction %s\(.*?\n\}" % f, src, re.S) or [""])[0]
              for f in ("_projPortRows", "_projPortRowsFlat", "_projKidName", "_projPortRow"))
js = fns + """
const escapeHtml = s => String(s == null ? '' : s), T = k => k, _projPortVia = () => '';
const window = {t: k => k};
const P = %s;
const html = _projPortRows('out', P, (p, i) => i);
const order = [...html.matchAll(/topo-proj-section( topo-proj-section-renvoi)?"|data-port-id="(\\d+)"/g)]
    .map(m => m[2] ? +m[2] : (m[1] ? 'RV' : 'EXT'));
console.log(JSON.stringify({order, readers: (html.match(/topo-proj-readers">([^<]*)/) || [])[1] || '',
                            from: html.includes('← Mixer · PGM'), tinted: (html.match(/topo-port-renvoi/g) || []).length}));
""" % json.dumps(outs)
try:
    o = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30).stdout.strip() or "{}")
except (OSError, ValueError) as e:
    o = {"_error": str(e)}
ok(o.get("order") == ["EXT", CAM, "RV", PGM, PGMA1, CLEAN],
   "4. la carte : « Externes », ses sources, puis « Renvois du projet » et ses destinations", o)
ok(o.get("readers") == "cables.proj.readers_n" and o.get("from") and o.get("tinted") == 3,
   "4b. un renvoi dit d'où il vient et combien le lisent", o)

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — renvois : destinations réutilisables comme sources du projet")
