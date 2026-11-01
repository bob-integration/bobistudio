#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Garde : les vues enregistrées de la page Câbles appartiennent à leur auteur (24/10).

Vérifie, sur une base jetable (processus à part : la connexion par thread garderait sinon la base
de prod) :
  1. une disposition d'avant la propriété devient une vue COMMUNE, rappelée en Libre avec des
     filtres neutres ;
  2. une vue privée n'est visible que de son auteur ; partagée, elle l'est de tous ;
  3. seul l'auteur peut la modifier ou la supprimer ; une vue commune, tout le monde ;
  4. les réglages enregistrés sont filtrés : clé inconnue, type faux ou valeur hors liste écartés ;
  6. « Mettre à jour » remplace le contenu d'une vue (nettoyé), sans toucher à son nom ;
  5. partage ciblé : à un utilisateur, à un groupe (suit ses membres), aux membres d'un projet, à tout
     le monde — et le retrait vaut tout de suite.
"""
import json
import os
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = r"""
import json, sqlite3, sys
sys.path.insert(0, sys.argv[1])
from app import database
database.DB_PATH = sys.argv[2]
# Base d'AVANT : table sans propriétaire, une disposition enregistrée.
db = sqlite3.connect(sys.argv[2])
db.execute("CREATE TABLE cable_layouts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TEXT, payload TEXT)")
db.execute("INSERT INTO cable_layouts (name, payload) VALUES ('Ancienne', ?)", (json.dumps({"positions": {"7": {"x": 1, "y": 2}}, "collapsed": ["7"]}),))
db.commit(); db.close()
database.init_db()
database.init_db()   # idempotent
from app.routes import cabling as c
out = {}
legacy = database.db_cable_layouts_list(None)[0]
out["legacy"] = {"shared": legacy["shared"], "owner": legacy["owner_id"], "settings": legacy["payload"]["settings"],
                 "positions": legacy["payload"]["positions"]}
priv = database.db_cable_layout_save("Privée", {"positions": {}}, owner_id=1, shared=False)
pub = database.db_cable_layout_save("Publique", {"positions": {}}, owner_id=1, shared=True)
out["seen_by_1"] = sorted(l["name"] for l in database.db_cable_layouts_list(1))
out["seen_by_2"] = sorted(l["name"] for l in database.db_cable_layouts_list(2))
g = database.db_cable_layout_get
out["visible_priv_2"] = c._view_visible(g(priv), 2)
out["visible_priv_1"] = c._view_visible(g(priv), 1)
out["editable_pub_2"] = c._view_editable(g(pub), 2)
out["editable_pub_1"] = c._view_editable(g(pub), 1)
out["editable_legacy_2"] = c._view_editable(legacy, 2)
database.db_cable_layout_update(priv, shared=True)
out["seen_by_2_after_share"] = sorted(l["name"] for l in database.db_cable_layouts_list(2))
# 5. Partage ciblé. Utilisateurs 1 (auteur), 2, 3 ; groupe G = {3} ; projet P = {2}.
db = database.get_db()
for uid in (1, 2, 3, 4):
    db.execute("INSERT INTO users (id, username, password_hash) VALUES (?, ?, 'x')", (uid, "u%d" % uid))
db.execute("INSERT INTO projects (id, name) VALUES (50, 'P')")
db.execute("INSERT INTO project_members (project_id, user_id) VALUES (50, 2)")
db.commit()
from app import user_groups
gid = user_groups.create_group("G")
user_groups.set_members(gid, [3])
cible = database.db_cable_layout_save("Ciblée", {"positions": {}}, owner_id=1)
def who():
    return [u for u in (2, 3, 4) if c._view_visible(g(cible), u)]
out["t_none"] = who()
database.db_cable_layout_set_shares(cible, False, [{"kind": "user", "id": 2}])
out["t_user"] = who()
database.db_cable_layout_set_shares(cible, False, [{"kind": "group", "id": gid}])
out["t_group"] = who()
user_groups.set_members(gid, [3, 4])
out["t_group_joined"] = who()
database.db_cable_layout_set_shares(cible, False, [{"kind": "project", "id": 50}])
out["t_project"] = who()
database.db_cable_layout_set_shares(cible, True, [])
out["t_all"] = who()
database.db_cable_layout_set_shares(cible, False, [])
out["t_removed"] = who()
database.db_cable_layout_set_shares(cible, False, [{"kind": "user", "id": 2}, {"kind": "bogus", "id": 3}])
out["t_shares"] = database.db_cable_layout_shares(cible)
database.db_cable_layout_delete(cible)
out["t_orphans"] = database.get_db().execute("SELECT COUNT(*) FROM cable_layout_shares WHERE layout_id=?", (cible,)).fetchone()[0]
# 6. Mise à jour du contenu d'une vue (« Mettre à jour ») : remplacé, nettoyé, nom inchangé.
upd = database.db_cable_layout_save("Àjour", {"positions": {}}, owner_id=1)
database.db_cable_layout_update(upd, payload=c._clean_view_payload(
    {"positions": {"9": {"x": 5, "y": 6}}, "collapsed": [9], "settings": {"view_mode": "cols", "evil": 1}}))
u = g(upd)
out["upd"] = {"name": u["name"], "positions": u["payload"]["positions"], "collapsed": u["payload"]["collapsed"],
              "settings": u["payload"]["settings"]}
out["upd_bad"] = c._clean_view_payload(["pas", "un", "objet"])
out["clean"] = c._clean_view_settings({"view_mode": "free", "signal": ["video", "x"], "project": 3,
                                       "projects_view": "yes", "lat_mode": "nope", "rdma_mode": "label",
                                       "zoom": 1.5, "pan_x": True, "evil": "<script>"})
print(json.dumps(out))
"""

fails = []


def check(cond, what, extra=None):
    print(("OK   " if cond else "FAIL ") + what + ("" if cond or extra is None else "  → %.300r" % (extra,)))
    if not cond:
        fails.append(what)


tmp = tempfile.mkdtemp()
r = subprocess.run([sys.executable, "-c", SCRIPT, RACINE, os.path.join(tmp, "v.db")],
                   capture_output=True, text=True, timeout=120)
try:
    o = json.loads(r.stdout.strip().splitlines()[-1])
except Exception:
    print(r.stdout[-800:], r.stderr[-1500:])
    sys.exit(1)

lg = o["legacy"]
check(lg["shared"] is True and lg["owner"] is None, "1. ancienne disposition : commune, sans propriétaire", lg)
check(lg["settings"].get("view_mode") == "free" and lg["settings"].get("project") == ""
      and lg["settings"].get("signal") == ["video", "audio", "data"], "1b. rappelée en Libre, filtres neutres", lg["settings"])
check(lg["positions"] == {"7": {"x": 1, "y": 2}}, "1c. positions conservées", lg["positions"])
check(o["seen_by_1"] == ["Ancienne", "Privée", "Publique"], "2. l'auteur voit ses vues et les communes", o["seen_by_1"])
check(o["seen_by_2"] == ["Ancienne", "Publique"], "2b. un autre ne voit pas la vue privée", o["seen_by_2"])
check(not o["visible_priv_2"] and o["visible_priv_1"], "2c. lecture directe d'une vue privée refusée aux autres")
check(o["seen_by_2_after_share"] == ["Ancienne", "Privée", "Publique"], "2d. partagée, elle devient visible")
check(not o["editable_pub_2"] and o["editable_pub_1"], "3. seule l'auteure modifie ou supprime sa vue partagée")
check(o["editable_legacy_2"], "3b. vue commune : modifiable par tous")
check(o["clean"] == {"view_mode": "free", "signal": ["video"], "project": "3", "rdma_mode": "label", "zoom": 1.5},
      "4. réglages filtrés (clé, type, valeur)", o["clean"])
check(o["upd"] == {"name": "Àjour", "positions": {"9": {"x": 5, "y": 6}}, "collapsed": ["9"],
                  "settings": {"view_mode": "cols"}}, "6. mise à jour : contenu remplacé et nettoyé, nom gardé", o["upd"])
check(o["upd_bad"] is None, "6b. contenu qui n'est pas un objet : refusé", o["upd_bad"])
check(o["t_none"] == [], "5. vue non partagée : personne d'autre", o["t_none"])
check(o["t_user"] == [2], "5b. partagée à un utilisateur : lui seul", o["t_user"])
check(o["t_group"] == [3], "5c. partagée à un groupe : ses membres", o["t_group"])
check(o["t_group_joined"] == [3, 4], "5d. un nouveau membre du groupe la voit aussitôt", o["t_group_joined"])
check(o["t_project"] == [2], "5e. partagée à un projet : ses membres", o["t_project"])
check(o["t_all"] == [2, 3, 4], "5f. partagée à tout le monde", o["t_all"])
check(o["t_removed"] == [], "5g. partage retiré : plus visible", o["t_removed"])
check(o["t_shares"] == [{"kind": "user", "id": 2}], "5h. type de destinataire inconnu écarté", o["t_shares"])
check(o["t_orphans"] == 0, "5i. supprimer la vue supprime ses partages", o["t_orphans"])

if fails:
    print("\nÉCHEC — %d vérification(s)" % len(fails))
    sys.exit(1)
print("\nOK — vues de la page Câbles : propriété et partage")
