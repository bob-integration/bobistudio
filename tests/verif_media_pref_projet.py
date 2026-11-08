#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : le filtre « projet » de la bibliothèque d'un player est un choix RETENU par utilisateur
(remonté le 2026-11-07 : un utilisateur qui voit toute la base retrouvait le filtre à chaque retour
sur le player).

  1. serveur : défaut = filtre posé ; le choix d'un utilisateur ne touche pas les autres ; il suit
     l'utilisateur (base, pas navigateur) ; clé inconnue ou mauvais type refusés ;
  2. bibliothèque (fonction réelle, exécutée) : retirer le filtre du projet du conteneur l'enregistre,
     le remettre aussi ; rien n'est envoyé quand rien ne change, ni au Gestionnaire (vue globale),
     ni pour un filtre sur un AUTRE projet ; à l'ouverture, le filtre n'est posé que si l'utilisateur
     ne l'a pas retiré.

Base jetable (jamais la base de prod).

    ./venv/bin/python tests/verif_media_pref_projet.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
TMP = tempfile.mkdtemp(prefix="verif_pref_projet_")
import app.database as database                                          # noqa: E402
database.DB_PATH = os.path.join(TMP, "principale.db")                     # jamais la base de prod
database.init_db()
from app import media_registry as mr                                      # noqa: E402
mr.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
mr.THUMB_DIR = os.path.join(TMP, "thumbs")
mr.BACKUP_DIR = os.path.join(TMP, "backups")
LIB = os.environ.get("PREF_LIB") or os.path.join(RACINE, "static", "js", "media_library.js")

ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


print("1. Serveur")
from app import auth                                                     # noqa: E402
from app.auth import hash_password                                       # noqa: E402
pw = hash_password("Banc-Motdepasse-2026!")
for u in ("tout", "autre"):
    database.db_create_user(u, pw, "admin")
uid = {u: database.get_db().execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()["id"] for u in ("tout", "autre")}
auth.check_vmid_access = lambda vmid, min_role="viewer": None
mr.container_context = lambda vmid: {"vmid": vmid, "node_id": 1, "project_id": 7, "project_name": "Régie"}
import main as app_main                                                  # noqa: E402
app_main.app.config["TESTING"] = True
cli = app_main.app.test_client()


def as_(who):
    with cli.session_transaction() as ss:
        ss.clear()
        ss["user_id"], ss["username"] = uid[who], who
        r = database.get_db().execute("SELECT session_epoch FROM users WHERE id=?", (uid[who],)).fetchone()
        ss["session_epoch"] = (r["session_epoch"] if r else 0) or 0


ctx = lambda: cli.get("/api/media/context?vmid=900").get_json()
as_("tout")
verifier(ctx()["project_filter"] is True and ctx()["project_id"] == 7, "par défaut : filtre posé")
r = cli.put("/api/media/prefs", json={"project_filter": False})
verifier(r.status_code == 200 and ctx()["project_filter"] is False, "retiré par l'utilisateur : retenu")
as_("autre")
verifier(ctx()["project_filter"] is True, "un autre utilisateur n'en est pas touché")
as_("tout")
verifier(mr.user_pref(uid["tout"], "project_filter") is False, "retenu dans la BASE (suit l'utilisateur d'un poste à l'autre)")
verifier(cli.put("/api/media/prefs", json={"inconnue": 1}).status_code == 400, "clé inconnue : refusée")
verifier(cli.put("/api/media/prefs", json={"project_filter": "non"}).status_code == 400 and ctx()["project_filter"] is False,
         "mauvais type : refusé, rien ne change")

print("2. Bibliothèque")
src = open(LIB, encoding="utf-8").read()
m = re.search(r"    function syncProjPref\(\)\{[\s\S]*?\n    \}\n", src)
verifier(m is not None, "syncProjPref présente")
prog = (m.group(0) if m else "function syncProjPref(){}") + r"""
const sent = [];
const fetch = (u, o) => { sent.push(JSON.parse(o.body)); return Promise.resolve(); };
const cases = JSON.parse(process.argv[1]), out = [];
for (const c of cases) {
  sent.length = 0;
  global.__ = null;
  const st = {ctx: c.ctx, tree: {items: c.items}}, o = {projectFilter: c.pf}, glob = c.global;
  const f = new Function('st', 'o', 'global', 'fetch', 'locked', 'return ' + syncProjPref.toString() + ';')(st, o, glob, fetch, false);
  f();
  out.push({sent: sent.slice(), after: st.ctx && st.ctx.project_filter});
}
process.stdout.write(JSON.stringify(out));
"""
P7 = {"field": "project", "op": "eq", "value": 7}
cases = [
    {"ctx": {"project_id": 7, "project_filter": True}, "items": [], "global": False},                   # retiré
    {"ctx": {"project_id": 7, "project_filter": False}, "items": [P7], "global": False},                # remis
    {"ctx": {"project_id": 7, "project_filter": True}, "items": [P7], "global": False},                 # inchangé
    {"ctx": {"project_id": 7, "project_filter": True}, "items": [], "global": True},                    # Gestionnaire
    {"ctx": {"project_id": 7, "project_filter": False}, "items": [dict(P7, value=8)], "global": False},  # autre projet
    {"ctx": {"project_id": 7, "project_filter": True}, "items": [], "global": False, "pf": False},      # hôte sans filtre
]
res = json.loads(subprocess.run(["node", "-e", prog, json.dumps(cases)], capture_output=True, text=True).stdout or "[]")
ok = len(res) == len(cases)
verifier(ok and res[0]["sent"] == [{"project_filter": False}] and res[0]["after"] is False, "filtre du projet retiré : enregistré")
verifier(ok and res[1]["sent"] == [{"project_filter": True}], "remis : enregistré")
verifier(ok and res[2]["sent"] == [], "rien ne change : rien n'est envoyé")
verifier(ok and res[3]["sent"] == [], "Gestionnaire (vue globale) : aucune préférence touchée")
verifier(ok and res[4]["sent"] == [], "filtre sur un AUTRE projet : ce n'est pas le filtre du conteneur")
verifier(ok and res[5]["sent"] == [], "hôte qui ne pose pas de filtre projet : rien")
verifier("syncProjPref(); _fT = setTimeout" in src, "appelée à chaque changement de filtre fait par l'utilisateur")
verifier(re.search(r"o\.projectFilter !== false && st\.ctx\.project_filter !== false\s*&& !st\.tree\.items\.some", src) is not None,
         "à l'ouverture : filtre posé seulement si l'utilisateur ne l'a pas retiré")

print("3. Verrou « Garder ici » (demandé le 2026-11-07)")
fn = {}
for name in ("saveLocked", "syncProjPref"):
    m = re.search(r"    function %s\(\)\{[\s\S]*?\n    \}\n" % name, src)
    verifier(m is not None, name + " présente")
    fn[name] = m.group(0) if m else "function %s(){}" % name
mk = re.search(r"    const lockKey = .*\n", src)
verifier(mk is not None, "clé du verrou présente")
prog3 = r"""
const store = {}, sent = [];
const pref = {get: (k, d) => k in store ? store[k] : d, set: (k, v) => { store[k] = v; }};
const fetch = (u, o) => { sent.push(JSON.parse(o.body)); return Promise.resolve(); };
const toQuery = t => t;
const out = {};
function keyOf(vmid, viewKey){ const o = {viewKey}, global = vmid == null; """ + (mk.group(0) if mk else "const lockKey = null;") + r""" return lockKey; }
out.keys = [keyOf(5), keyOf(6), keyOf(5, 'playout'), keyOf(null)];
const P7 = {field: 'project', op: 'eq', value: 7};
function run(locked, lockReady){
  const lockKey = 'lock.c5', global = false, o = {};
  const st = {tab: 'base', bin: 3, q: 'clip', tree: {match: 'all', items: [P7]}, ctx: {project_id: 7, project_filter: false}};
  const f = new Function('pref', 'fetch', 'toQuery', 'st', 'o', 'global', 'lockKey', 'locked', 'lockReady',
    """ + json.dumps(fn["saveLocked"] + fn["syncProjPref"]) + r""" + '; saveLocked(); syncProjPref();');
  f(pref, fetch, toQuery, st, o, global, lockKey, locked, lockReady);
}
sent.length = 0; run(true, true);
out.saved = store['lock.c5']; out.sentLocked = sent.slice();
delete store['lock.c5']; sent.length = 0; run(true, false);
out.notReady = 'lock.c5' in store;
sent.length = 0; run(false, true);
out.unlockedSaved = 'lock.c5' in store; out.sentUnlocked = sent.slice();
process.stdout.write(JSON.stringify(out));
"""
p3 = subprocess.run(["node", "-e", prog3], capture_output=True, text=True)
try:
    r3 = json.loads(p3.stdout)
except Exception:
    r3 = None
    verifier(False, "exécution JS : " + p3.stderr[-300:])
if r3:
    verifier(r3["keys"] == ["lock.c5", "lock.c6", "lock.playout.c5", None],
             "clé : par conteneur, distincte pour un groupe (viewKey), aucune au Gestionnaire (%s)" % r3["keys"])
    verifier(r3["saved"] == {"tab": "base", "bin": 3, "q": "clip", "tree": {"match": "all", "items": [P7]}},
             "verrouillé : onglet, bin, recherche et filtres gardés sur ce poste")
    verifier(r3["sentLocked"] == [], "verrouillé : la préférence du compte n'est pas touchée")
    verifier(r3["notReady"] is False, "rien n'est écrit avant que la recherche gardée soit rétablie")
    verifier(r3["unlockedSaved"] is False and r3["sentUnlocked"] == [{"project_filter": True}],
             "déverrouillé : rien de local, la préférence du compte comme avant")
boot = re.search(r"const kept = locked \? pref\.get\(lockKey, null\) : null;\s*"
                 r"if\(kept && typeof kept === 'object'\)\{ lockReady = true; await setState\(kept\); if\(!_dead\) loadBins\(\); return; \}\s*"
                 r"lockReady = true;\s*// … sauf si l'UTILISATEUR", src)
verifier(boot is not None, "ouverture : la recherche gardée passe AVANT le filtre de projet, qui n'est alors pas posé")
verifier("async function loadAssets(){\n      saveLocked();" in src and "pvStop(true); saveLocked();" in src,
         "gardée à chaque recherche et à chaque changement d'onglet")
verifier("locked = !locked; lockReady = true;\n      if(locked) saveLocked(); else pref.set(lockKey, null);" in src,
         "le bouton : verrouiller garde l'état présent, déverrouiller l'oublie")

print()
if ECHECS:
    print("%d échec(s)" % len(ECHECS))
    sys.exit(1)
print("Filtre projet retenu par utilisateur : tous les cas tiennent.")
