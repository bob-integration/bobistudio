#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : une SÉLECTION de médias se range dans un projet, ou en sort (demandé le 2026-11-07 ;
jusque-là, seulement fiche par fiche).

  1. serveur : une requête pour toute la sélection ; ajouter deux fois ne double rien ; retirer ;
     mêmes droits qu'un média seul — gestion des médias requise, projet de l'utilisateur, médias
     hors de sa portée ignorés et comptés, identifiant inconnu ignoré ; projet inconnu 404 ;
  2. bibliothèque (vraies fonctions, exécutées sous node) : le menu propose tous les projets à
     l'ajout, au retrait seulement ceux où se trouve un média de la sélection ; un média partagé
     (pas à soi) n'est pas envoyé ; l'appel part vers la bonne route ; bouton dans la barre de
     sélection et entrées dans le menu de sélection, pour qui gère les médias.

Base jetable (jamais la base de prod).

    ./venv/bin/python tests/verif_media_selection_projet.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
TMP = tempfile.mkdtemp(prefix="verif_sel_projet_")
import app.database as database                                          # noqa: E402
database.DB_PATH = os.path.join(TMP, "principale.db")                     # jamais la base de prod
database.init_db()
from app import media_registry as mr                                      # noqa: E402
mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
mr.BACKUP_DIR = os.path.join(TMP, "backups")
os.makedirs(mr.THUMB_DIR, exist_ok=True)
LIB = os.environ.get("SEL_LIB") or os.path.join(RACINE, "static", "js", "media_library.js")

ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


c = mr.db()
NOW = "2026-11-07T10:00:00"
A, B, C, D = ("%08d-0000-4000-8000-000000000000" % n for n in (1, 2, 3, 4))
for aid, name in ((A, "a.mxf"), (B, "b.mxf"), (C, "c.mxf"), (D, "d.mxf")):
    c.execute("INSERT INTO media_assets(id, name, created_at, updated_at, origin, tech_json, meta_json) "
              "VALUES (?,?,?,?,?,?,?)", (aid, name, NOW, NOW, "import", "{}", "{}"))
c.commit()
P1 = database.db_import_project("Atelier", [])
P2 = database.db_import_project("Autre", [])
mr.link_project(A, P1)            # A et B dans Atelier : visibles de l'opérateur restreint
mr.link_project(B, P1)
mr.link_project(C, P2)            # C seulement dans Autre : hors de sa portée


def projets(aid):
    return [r["project_id"] for r in c.execute("SELECT project_id FROM project_media WHERE asset_id=? "
                                               "ORDER BY project_id", (aid,))]


print("1. Serveur")
from app.auth import hash_password                                       # noqa: E402
pw = hash_password("Banc-Motdepasse-2026!")
from app import auth                                                     # noqa: E402
for u, role in (("adm", "admin"), ("ope", "gestmedia"), ("expl", "exploitant")):
    database.db_create_user(u, pw, role)
uid = {u: database.get_db().execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()["id"]
       for u in ("adm", "ope", "expl")}
database.db_set_project_member(P1, uid["ope"], "operator")
database.db_set_project_member(P1, uid["expl"], "operator")
import main as app_main                                                  # noqa: E402
app_main.app.config["TESTING"] = True
# After the app import: it reloads the roles from the database.
auth.ROLES["gestmedia"] = {"media.manage"}       # restreint à ses projets, mais gère les médias
cli = app_main.app.test_client()


def as_(who):
    with cli.session_transaction() as ss:
        ss.clear()
        ss["user_id"], ss["username"] = uid[who], who
        r = database.get_db().execute("SELECT session_epoch FROM users WHERE id=?", (uid[who],)).fetchone()
        ss["session_epoch"] = (r["session_epoch"] if r else 0) or 0


post = lambda pid, body: cli.post("/api/media/projects/%s/assets" % pid, json=body)
as_("adm")
r = post(P2, {"asset_ids": [A, B, D]})
verifier(r.status_code == 200 and r.get_json()["changed"] == 3 and all(P2 in projets(x) for x in (A, B, D)),
         "toute la sélection rangée dans le projet, en une requête")
r = post(P2, {"asset_ids": [A, B]})
verifier(r.get_json()["changed"] == 0 and projets(A) == [P1, P2], "rangée deux fois : rien ne double")
r = post(P2, {"asset_ids": [A, D], "on": False})
verifier(r.get_json()["changed"] == 2 and projets(A) == [P1] and projets(D) == [] and P2 in projets(B),
         "retirée du projet : seulement les médias sélectionnés")
r = post(P2, {"asset_ids": ["%08d-0000-4000-8000-000000000000" % 99]})
verifier(r.status_code == 200 and r.get_json()["changed"] == 0, "identifiant inconnu : ignoré")
verifier(post(9999, {"asset_ids": [A]}).status_code == 404, "projet inconnu : 404")
verifier(post(P2, {"asset_ids": []}).status_code == 400 and post(P2, {"asset_ids": A}).status_code == 400
         and post(P2, {"asset_ids": [1]}).status_code == 400, "sélection vide ou mal formée : 400")
as_("ope")
verifier(post(P2, {"asset_ids": [A]}).status_code == 403 and projets(A) == [P1],
         "restreint : un projet qui n'est pas le sien est refusé (403)")
r = post(P1, {"asset_ids": [A, C, D]})
verifier(r.status_code == 200 and r.get_json()["skipped"] == 2 and projets(C) == [P2] and projets(D) == [],
         "restreint : médias hors de sa portée ignorés et comptés (%s)" % r.get_json())
as_("expl")
verifier(post(P1, {"asset_ids": [B]}).status_code == 403 and projets(B) == [P1, P2], "sans gestion des médias : 403")

print("2. Bibliothèque")
src = open(LIB, encoding="utf-8").read()
fns = {}
for name in ("toProjItems", "linkProj"):
    m = re.search(r"    (?:async )?function %s\([\s\S]*?\n    \}\n" % name, src)
    verifier(m is not None, name + " présente")
    fns[name] = m.group(0) if m else "function %s(){}" % name
prog = fns["toProjItems"] + fns["linkProj"] + r"""
const L = (k, d) => d, calls = [], toasts = [];
let reloaded = 0;
const regCall = (m, u, b) => { calls.push({m, u, b}); return Promise.resolve({ok: true, changed: 2, skipped: 1}); };
const toast = (t) => toasts.push(t);
const loadAssets = async () => { reloaded++; }, render = () => {};
const st = {sf: {projects: [{id: 1, name: 'Atelier'}, {id: 2, name: 'Autre'}, {id: 3, name: 'Tiers'}]}};
const rs = [{asset: {id: 'a', projects: [2]}}, {asset: {id: 'b', projects: []}},
            {asset: {id: 's', shared: {level: 'use'}, projects: [3]}}, {ext: {}}];
(async () => {
  const it = toProjItems(rs);
  const out = {labels: it.map(x => x.header !== undefined ? '#' + x.header : x.label)};
  await it[1].run();                                     // ajouter à Atelier
  const rmAutre = it.find((x, i) => i > 4 && x.label === 'Autre');
  if (rmAutre) await rmAutre.run();
  out.calls = calls; out.toasts = toasts; out.reloaded = reloaded;
  out.none = toProjItems([{asset: {id: 's', shared: {level: 'use'}}}]).map(x => x.header);
  st.sf.projects = [];
  out.noproj = toProjItems(rs).map(x => x.header);
  process.stdout.write(JSON.stringify(out));
})();
"""
p = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
try:
    o = json.loads(p.stdout)
except Exception:
    o = None
    verifier(False, "exécution JS : " + p.stderr[-400:])
if o:
    verifier(o["labels"] == ["#Ajouter au projet", "Atelier", "Autre", "Tiers", "#Retirer du projet", "Autre"],
             "menu : tous les projets à l'ajout, au retrait seulement ceux d'un média À SOI (%s)" % o["labels"])
    verifier(o["calls"] == [{"m": "POST", "u": "/api/media/projects/1/assets", "b": {"asset_ids": ["a", "b"], "on": True}},
                            {"m": "POST", "u": "/api/media/projects/2/assets", "b": {"asset_ids": ["a", "b"], "on": False}}],
             "envoi : la sélection À SOI (pas le partagé), vers la bonne route, ajout puis retrait")
    verifier(o["reloaded"] == 2 and "2 média(s) ajouté(s) à « Atelier » · 1 ignoré(s)" in o["toasts"],
             "bilan affiché (ignorés compris), liste relue (%s)" % o["toasts"])
    verifier(o["none"] == ["Aucun de ces médias ne vous appartient."], "rien à soi : le menu le dit")
    verifier(o["noproj"] == ["Aucun projet."], "aucun projet : le menu le dit")
verifier(re.search(r'canShare\(\) \? `<button type="button" class="btn" data-bulk="proj"', src) is not None,
         "barre de sélection : bouton « Projet ▾ » pour qui gère les médias")
verifier("if(b.dataset.bulk === 'proj'){ await loadSF(); return openMenu(b, toProjItems(rs)); }" in src,
         "le bouton ouvre le menu, liste des projets chargée d'abord")
verifier("if(own.length && canShare() && st.sf) out.push(...toProjItems(rs));" in src,
         "menu de sélection (clic droit) : mêmes entrées")

print()
if ECHECS:
    print("%d échec(s)" % len(ECHECS))
    sys.exit(1)
print("Sélection rangée dans un projet : tous les cas tiennent.")
