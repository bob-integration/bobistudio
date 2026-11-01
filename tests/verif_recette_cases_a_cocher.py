#!/usr/bin/env python3
"""Garde : gestes à cocher d'un élément de la page Recette (/tests), avec un commentaire à côté.

Demandé par Cyril le 2026-10-30 pour la recette des éditeurs : un élément porte une liste de gestes
(`steps`) ; chaque geste a sa case et son commentaire (`state[id].checks[n]`). On vérifie : création
avec gestes, case + commentaire enregistrés, geste inconnu refusé, le STATUT de l'élément n'efface
pas les cases (même état), remplacer les gestes garde les cases qui restent, l'export de campagne
les emporte, et la page sait les afficher (ctl-switch du catalogue + champ commentaire).

Store dans un répertoire jetable ; aucune base, aucun réseau."""
import json, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask
from app import auth, testplan as tp

TMP = tempfile.mkdtemp(prefix="recette_cases_")
tp.STORE_PATH = os.path.join(TMP, "db_testplan.json")
tp.LOCK_PATH = tp.STORE_PATH + ".lock"
tp.IMAGES_DIR = os.path.join(TMP, "testplan_images")
tp.SEED_ITEMS = [("pt-1", "Zone", "Point 1", "", "")]
auth.current_user = lambda: {"id": 1, "username": "t"}
auth.has_perm = lambda p: True
tp._actif = lambda: True

app = Flask(__name__)
app.register_blueprint(tp.testplan_bp)
c = app.test_client()
echecs = []


def controle(intitule, cond, expl=""):
    print("  %-5s %s" % ("OK" if cond else "ÉCHEC", intitule))
    if not cond:
        echecs.append(intitule)
        if expl:
            print("        → %s" % expl)


def etat(iid):
    return (c.get("/api/tests").get_json().get("state") or {}).get(iid) or {}


r = c.post("/api/tests/item", json={"area": "Éditeurs", "title": "Composer",
                                    "steps": "Aligner\n\nMême taille\n  Distribuer  "})
it = (r.get_json() or {}).get("item") or {}
iid = it.get("id")
controle("★★ un élément se crée avec ses gestes (une ligne = un geste, lignes vides ignorées)",
         it.get("steps") == ["Aligner", "Même taille", "Distribuer"], it)

r = c.post("/api/tests/check", json={"id": iid, "step": 1, "done": True, "comment": "OK au pixel", "tester": "le testeur"})
ch = etat(iid).get("checks") or {}
controle("★★★ case cochée ET commentaire à côté, enregistrés pour CE geste",
         r.status_code == 200 and ch.get("1", {}).get("done") is True and ch["1"]["comment"] == "OK au pixel"
         and ch["1"]["tester"] == "le testeur" and "0" not in ch, ch)
r = c.post("/api/tests/check", json={"id": iid, "step": 0, "done": False, "comment": "décalé de 2 px"})
controle("★★ un commentaire sans case cochée (défaut constaté) est gardé",
         (etat(iid).get("checks") or {}).get("0", {}).get("comment") == "décalé de 2 px")
controle("★ geste hors liste refusé", c.post("/api/tests/check", json={"id": iid, "step": 3, "done": True}).status_code == 400)
controle("★ élément inconnu refusé", c.post("/api/tests/check", json={"id": "nope", "step": 0}).status_code == 404)

c.post("/api/tests/state", json={"id": iid, "status": "defect", "severite": "mineur", "remark": "voir geste 1"})
st = etat(iid)
controle("★★★ changer le STATUT de l'élément n'efface pas ses cases",
         st.get("status") == "defect" and (st.get("checks") or {}).get("1", {}).get("done") is True, st)

r = c.post("/api/tests/item/steps", json={"id": iid, "steps": ["Aligner", "Même taille"]})
ch = etat(iid).get("checks") or {}
controle("★★ remplacer les gestes : les cases qui restent gardent leur état, celles retirées partent",
         r.status_code == 200 and ch.get("1", {}).get("done") is True and "2" not in ch, ch)
controle("★ un élément intégré à la campagne n'est pas réécrit",
         c.post("/api/tests/item/steps", json={"id": "pt-1", "steps": ["x"]}).status_code == 400)

exp = tp._recette_export(True)
controle("★★ l'export de campagne emporte gestes et cases",
         any(i.get("steps") for i in exp["items"]) and (exp["state"].get(iid) or {}).get("checks"))

page = open(os.path.join(ROOT, "templates", "tests.html"), encoding="utf-8").read()
controle("★★ la page dessine une case du CATALOGUE (ctl-switch) et un commentaire par geste",
         'cb.className = "ctl-switch"' in page and 'cm.placeholder = "Commentaire…"' in page
         and '"/api/tests/check"' in page and "renderSteps(it, st)" in page)
controle("★ le formulaire d'ajout accepte des gestes", 'id="tp-add-steps"' in page and "steps: document.getElementById" in page)
controle("★ saisir un commentaire suspend le rafraîchissement (pas de saisie écrasée)",
         "editing.add(cm.id)" in page)

print("\n%s" % ("ÉCHEC : %d contrôle(s)" % len(echecs) if echecs else "TOUT VERT"))
sys.exit(1 if echecs else 0)
