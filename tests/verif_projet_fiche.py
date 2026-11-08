#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Fiche d'un projet (2026-11-07) : description, étiquettes, auteur, dernier démarrage / arrêt /
modification. Avant : `projects.owner_id` existait mais personne ne l'écrivait (13 projets sur 13
sans auteur), et rien ne disait qui avait démarré ou arrêté un projet.

Vérifié hors ligne (base jetable, vraies routes) :
  1. créer un projet enregistre son AUTEUR et sa description ;
  2. la fiche se modifie (étiquettes nettoyées, doublons retirés) et cela tamponne « modifié » ;
  3. l'export emporte description et étiquettes, l'import les reprend (auteur = l'importeur) ;
  4. démarrer tamponne « démarré » seulement si au moins un conteneur a été restauré ;
  5. arrêter tamponne « arrêté », sans toucher « modifié » ;
  6. nommer une version n'est pas une modification, ajouter un port en est une ;
  7. changer la composition tamponne le projet quitté ET le projet rejoint ;
  8. la page rend la fiche (avec les lignes de l'onglet Conteneurs) et son JS se lit ;
  9. un projet ARRÊTÉ qui reçoit un conteneur le GARDE en plus de son contenu (avant : le
     re-snapshot ne voyait que le nouveau et remplaçait tout — 3 conteneurs → 1) ;
 10. retirer un conteneur arrêté ôte son entrée (version gardée) ; un conteneur en marche se
     détache, la route le refuse (409) ; un inconnu est un 404 ;
 11. suivi en direct de la fenêtre (`_projApplyLive`, exécuté) : un conteneur recréé sous un NOUVEAU
     vmid est reconnu par son nom, un nouveau venu s'ajoute, l'arrêt se termine quand plus rien
     ne tourne ; Démarrer et Arrêter mènent à l'onglet Conteneurs.

⚠ Garde MUTÉE à l'écriture.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
os.chdir(RACINE)

from app import config, database                  # noqa: E402
_TMP = tempfile.mkdtemp(prefix="projet_fiche_db_")
_DBF = os.path.join(_TMP, "t.db")
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
for v, hn, t in ((301, "Atelier-mixer", "mixer"), (302, "Atelier-stills", "stills"), (303, "Studio-mv", "multiview")):
    database.db_upsert_container(v, hn)
    database.db_update_deploy_config(v, t, {"hostname": hn})

import main as app_main                           # noqa: E402
from app import containers as _containers         # noqa: E402
from app.routes import projects_api               # noqa: E402
_containers.MEDIA_HOST_DIR = os.path.join(_TMP, "media")   # jamais /srv/mxl-media du banc
app = app_main.app
app.config["TESTING"] = True
cli = app.test_client()
with cli.session_transaction() as s:
    s["user_id"] = uid
    s["username"] = "admin_t"


def proj(pid):
    return database.db_get_project(pid)


print("1. Création : auteur et description")
r = cli.post("/api/projects", json={"name": "Atelier", "vmids": [301, 302], "description": "Plateau du matin"})
PID = (r.get_json() or {}).get("id")
p = proj(PID) if PID else {}
facts = database.db_projects_facts().get(PID, {})
ok(r.status_code == 200 and p.get("owner_id") == uid and facts.get("author") == "admin_t"
   and p.get("description") == "Plateau du matin", "auteur = le compte connecté, description gardée",
   (r.status_code, p.get("owner_id"), facts, p.get("description")))

print("2. Modifier la fiche")
database.db_project_event(PID, "modified", None)
with database.get_db() as db:
    db.execute("UPDATE projects SET modified_at=NULL, modified_by=NULL WHERE id=?", (PID,))
    db.commit()
r = cli.patch("/api/projects/%d" % PID, json={"description": "  Plateau\ndu soir ", "tags": ["Régie", " régie ", "", "essai"]})
p = proj(PID)
ok(r.status_code == 200 and p["description"] == "Plateau\ndu soir" and p["tags"] == ["Régie", "essai"],
   "texte rogné, étiquettes sans doublon ni vide", (r.status_code, p["description"], p["tags"]))
ok(p.get("modified_by") == "admin_t" and p.get("modified_at"), "« modifié » tamponné", (p.get("modified_at"), p.get("modified_by")))
r = cli.patch("/api/projects/%d" % PID, json={"tags": "pas une liste"})
ok(r.status_code == 400, "étiquettes hors liste refusées", r.status_code)

print("3. Export / import")
exp = json.loads(cli.get("/api/projects/%d/export" % PID).data)
ok(exp.get("description") == "Plateau\ndu soir" and exp.get("tags") == ["Régie", "essai"], "l'export les porte", exp.get("tags"))
r = cli.post("/api/projects/import", json=dict(exp, name="Atelier bis"))
PID2 = (r.get_json() or {}).get("id")
p2 = proj(PID2) if PID2 else {}
ok(p2.get("description") == "Plateau\ndu soir" and p2.get("tags") == ["Régie", "essai"] and p2.get("owner_id") == uid,
   "l'import les reprend, l'importeur est l'auteur", (r.status_code, p2.get("tags"), p2.get("owner_id")))

print("4. Démarrer")
RES = {"ok": 0, "total": 2, "failed": [1, 2]}
projects_api.restaurer_projet = lambda pid, **k: dict(RES)
cli.post("/api/projects/%d/restore" % PID, json={}).data
ok(not proj(PID).get("started_at"), "échec complet : pas de « démarré »", proj(PID).get("started_at"))
RES = {"ok": 2, "total": 2, "failed": []}
cli.post("/api/projects/%d/restore" % PID, json={}).data
p = proj(PID)
ok(p.get("started_at") and p.get("started_by") == "admin_t", "réussi : « démarré » par admin_t", (p.get("started_at"), p.get("started_by")))

print("5. Arrêter")
with database.get_db() as db:
    db.execute("UPDATE projects SET modified_at='2000-01-01T00:00:00' WHERE id=?", (PID,))
    db.commit()
projects_api.detruire_containers_projet = lambda pid: None
cli.post("/api/projects/%d/destroy_containers" % PID)
for _ in range(50):
    if proj(PID).get("stopped_at"):
        break
    time.sleep(0.05)
p = proj(PID)
ok(p.get("stopped_at") and p.get("stopped_by") == "admin_t", "« arrêté » par admin_t", (p.get("stopped_at"), p.get("stopped_by")))
ok(p.get("modified_at") == "2000-01-01T00:00:00", "arrêter n'est pas modifier", p.get("modified_at"))

print("6. Ce qui modifie, ce qui ne modifie pas")
cli.post("/api/projects/%d/versions" % PID, json={"label": "v1"})
ok(proj(PID).get("modified_at") == "2000-01-01T00:00:00", "nommer une version : non", proj(PID).get("modified_at"))
r = cli.post("/api/projects/%d/ports/batch" % PID, json={"kind": "dest", "names": ["PGM"], "video": True, "audios": 0, "anc": False})
ok(r.status_code < 400 and proj(PID).get("modified_at") != "2000-01-01T00:00:00", "ajouter un port : oui",
   (r.status_code, proj(PID).get("modified_at")))

print("7. Composition")
_containers.changer_media_projet = lambda vmid, pid, media: database.db_set_project(vmid, pid)
database.db_set_project(303, PID2)
for q in (PID, PID2):
    with database.get_db() as db:
        db.execute("UPDATE projects SET modified_at=NULL WHERE id=?", (q,))
        db.commit()
r = cli.patch("/api/containers/303/project", json={"project_id": PID})
for _ in range(40):      # the move runs in a background thread
    if (database.db_get_container(303) or {}).get("project_id") == PID:
        break
    time.sleep(0.05)
ok((database.db_get_container(303) or {}).get("project_id") == PID, "le conteneur a rejoint le projet",
   (database.db_get_container(303) or {}).get("project_id"))
ok(r.status_code < 400 and proj(PID).get("modified_at") and proj(PID2).get("modified_at"),
   "projet quitté et projet rejoint tamponnés", (r.status_code, proj(PID).get("modified_at"), proj(PID2).get("modified_at")))

print("8. La page")
r = cli.get("/projects")
html = r.get_data(as_text=True)
m = re.search(r'<script type="application/json" id="proj-sheets">(.*?)</script>', html, re.S)
sheets = json.loads(m.group(1)) if m else {}
sh = sheets.get(str(PID), {})
ok(r.status_code == 200 and sh.get("author") == "admin_t" and sh.get("started_by") == "admin_t"
   and sh.get("tags") == ["Régie", "essai"], "la fiche est dans la page", (r.status_code, sh))
ok('data-tags="|régie|essai|"' in html and "openProject(%d, 'info')" % PID in html
   and "openProject(%d, 'compo')" % PID not in html,
   "tuile : étiquettes filtrables, un seul bouton vers la fenêtre")
rows = {r["vmid"]: r for r in sh.get("rows") or []}
ok({301, 302, 303} <= set(rows) and rows[303]["live"] and not rows[303]["saved"] and rows[301]["saved"],
   "onglet Conteneurs : enregistrés + vivants rattachés depuis", rows)
js = "\n".join(re.findall(r"<script(?![^>]*src)(?![^>]*application/json)[^>]*>(.*?)</script>", html, re.S))
f = os.path.join(_TMP, "page.js")
open(f, "w", encoding="utf-8").write(js)
chk = subprocess.run(["node", "--check", f], capture_output=True, text=True)
ok(chk.returncode == 0, "le JS de la page se lit", chk.stderr[:300])

_live = re.search(r"\nfunction _projApplyLive\(.*?\n\}", html, re.S)
_live = _live.group(0) if _live else ""

print("9. Projet arrêté qui reçoit un conteneur")
from app import projects as _projects            # noqa: E402
for v, hn, t in ((304, "Banc-a", "mixer"), (305, "Banc-b", "stills"), (306, "Banc-new", "multiview")):
    database.db_upsert_container(v, hn)
    database.db_update_deploy_config(v, t, {"hostname": hn})
PID3 = database.db_save_project("Banc", [304, 305])
with database.get_db() as db:
    db.execute("DELETE FROM containers WHERE vmid IN (304, 305)")   # projet arrêté
    db.commit()
database.db_set_project(306, PID3)
_projects.resnapshot_projet(PID3)
ok(sorted(c["vmid"] for c in proj(PID3)["snapshot"]) == [304, 305, 306], "contenu gardé, nouveau venu ajouté",
   [c["vmid"] for c in proj(PID3)["snapshot"]])

print("10. Retirer un conteneur")
nv = len(database.db_project_versions(PID3))
r = cli.delete("/api/projects/%d/containers/304" % PID3)
ok(r.status_code == 200 and sorted(c["vmid"] for c in proj(PID3)["snapshot"]) == [305, 306]
   and len(database.db_project_versions(PID3)) == nv + 1, "arrêté : entrée retirée, version gardée",
   (r.status_code, [c["vmid"] for c in proj(PID3)["snapshot"]]))
r = cli.delete("/api/projects/%d/containers/306" % PID3)
ok(r.status_code == 409 and 306 in [c["vmid"] for c in proj(PID3)["snapshot"]], "en marche : 409, rien ne bouge", r.status_code)
r = cli.delete("/api/projects/%d/containers/999" % PID3)
ok(r.status_code == 404, "inconnu : 404", r.status_code)

print("11. Suivi en direct")
harness = """
const PROJ_SHEETS = {7: {rows: [{vmid: 1, hostname: 'a', live: false, status: null, saved: true},
                                {vmid: 2, hostname: 'b', live: false, status: null, saved: true},
                                {vmid: 3, hostname: 'd', run_host: 'Atelier-d', live: false, status: null, saved: true}]}};
const PROJ_NODE_NAMES = {5: 'dell-1'};
const _projStopping = new Set(); let _projDirty = false; let heads = 0;
const _projPoll = {filter: null};
function _projHead() { heads++; }
function chargerComposition() {}
const document = {getElementById: () => null};
""" + _live + """
const out = {};
_projApplyLive(7, [{vmid: 41, hostname: 'a', project_id: 7, status: 'running', node_id: 5},
                   {vmid: 42, hostname: 'c', project_id: 7, status: 'starting', node_id: 5, deploy_config: '{"type":"mixer"}'},
                   {vmid: 43, hostname: 'b', project_id: 8, status: 'running'},
                   {vmid: 44, hostname: 'Atelier-d', project_id: 7, status: 'running', node_id: 5}]);
const R = PROJ_SHEETS[7].rows;
out.start = R.map(r => [r.hostname, r.vmid, r.live, r.status, r.node || null, r.type || null]);
_projStopping.add(7);
_projApplyLive(7, [{vmid: 41, hostname: 'a', project_id: 7, status: 'running'}]);
out.still = _projStopping.has(7);
_projApplyLive(7, []);
out.stopped = !_projStopping.has(7) && _projDirty && R.every(r => !r.live);
out.heads = heads;
console.log(JSON.stringify(out));
"""
f = os.path.join(_TMP, "live.js")
open(f, "w", encoding="utf-8").write(harness)
run = subprocess.run(["node", f], capture_output=True, text=True)
try:
    res = json.loads(run.stdout.strip().splitlines()[-1])
except Exception:
    res = {"error": run.stderr[:300]}
ok(res.get("start") == [["a", 41, True, "running", "dell-1", None], ["b", 2, False, None, None, None],
                        ["d", 44, True, "running", "dell-1", None], ["c", 42, True, "starting", "dell-1", "mixer"]],
   "recréé sous un nouveau vmid : reconnu par son nom, préfixé ou non ; nouveau venu ajouté ; autre projet ignoré", res)
ok(rows.get(301, {}).get("run_host") == "Atelier-mixer"    # déjà préfixé : pas « Atelier-Atelier-… »
   and rows.get(303, {}).get("run_host") == "Atelier-Studio-mv",
   "la page donne le nom de démarrage de chaque ligne", rows.get(301))
ok(res.get("still") is True and res.get("stopped") is True and res.get("heads") == 3,
   "l'arrêt se termine quand plus rien ne tourne (et la page se relira)", res)
ok("_projShow(pid, 'containers');   // on regarde les conteneurs démarrer" in html
   and "_projStopping.add(pid);" in html, "Démarrer et Arrêter mènent à l'onglet Conteneurs")

print()
if ECHECS:
    print("ÉCHEC : %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("OK")
