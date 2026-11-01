#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Les ports d'un projet se configurent sur /projects, et seulement là (décision de Cyril, 28/10).

Vérifié hors ligne (base jetable) :
  1. /projects se rend, chaque carte porte le bouton « Ports du projet », qui ouvre une FENÊTRE
     recouvrant la page (pas un panneau dans la carte, illisible au milieu des autres projets) ;
  2. les motifs de noms ({1-6}, {01-12}, {a,b}, combinés) et les noms des ports qu'un ajout crée
     (vidéo + rattachés A1…, Anc) — fonctions du gabarit exécutées sous Node, comparées au serveur ;
     chaque clé i18n du panneau existe en fr et en ;
  3. la Vue d'exploitation n'a plus d'éditeur de ports (ni liste, ni ajout, ni libellés).

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
_DBF = os.path.join(tempfile.mkdtemp(prefix="ports_page_db_"), "t.db")
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
database.db_upsert_container(101, "cam")
database.db_update_deploy_config(101, "color_corrector", {"hostname": "cam"})
pid = database.db_save_project("P", [101])

import main as app_main                           # noqa: E402
app = app_main.app
app.config["TESTING"] = True
cli = app.test_client()
with cli.session_transaction() as s:
    s["user_id"] = uid
    s["username"] = "admin_t"
r = cli.get("/projects")
html = r.get_data(as_text=True)
ok(r.status_code == 200, "1. /projects se rend", r.status_code)
ok('onclick="togglePorts(%d)"' % pid in html, "1b. la carte porte le bouton des ports")
_tp = re.search(r"\nfunction togglePorts\(.*?\n\}", open(os.path.join(RACINE, "templates", "projects.html"),
                                                          encoding="utf-8").read(), re.S)
_tp = _tp.group(0) if _tp else ""
# Viser le code de togglePorts, pas la page : « modal-overlay » figure ailleurs dans la page rendue
# (la première version de cette garde restait verte quand togglePorts n'ouvrait plus de fenêtre).
ok('id="proj-ports-%d"' % pid not in html and "ov.className = 'modal-overlay'" in _tp
   and 'aria-modal="true"' in _tp and "document.body.appendChild(ov)" in _tp,
   "1c. les ports s'ouvrent dans une fenêtre qui recouvre la page, pas dans la carte")

src = open(os.path.join(RACINE, "templates", "projects.html"), encoding="utf-8").read()


def js_fn(name):
    """Source d'une fonction de premier niveau du gabarit (jusqu'à la ligne `}` qui la ferme)."""
    m = re.search(r"\nfunction %s\(.*?\n\}" % re.escape(name), src, re.S)
    return m.group(0) if m else ""


# 2. Motifs de noms et noms des ports créés : exécutés tels quels sous Node.
SCRIPT = js_fn("portExpandNames") + js_fn("_portAllNames") + """
const out = {
  range: portExpandNames('Cam {1-3}'),
  zeros: portExpandNames('Cam {08-10}'),
  product: portExpandNames('Cam {Rouge,Bleue} {Haut,Bas}'),
  glued: portExpandNames('Cam{1-2}'),
  plain: portExpandNames('PGM'),
  cap: portExpandNames('X{1-999}').length,
  withKids: _portAllNames(['Cam 1'], {v: true, na: 2, d: true}),
  noVideo: _portAllNames(['Mic'], {v: false, na: 2, d: false}),
  oneAudio: _portAllNames(['Mic'], {v: false, na: 1, d: false}),
};
console.log(JSON.stringify(out));
"""
try:
    r = subprocess.run(["node", "-e", SCRIPT], capture_output=True, text=True, timeout=30)
    o = json.loads(r.stdout.strip() or "{}")
except (OSError, ValueError) as e:
    o = {"_error": str(e)}
ok(o.get("range") == ["Cam 1", "Cam 2", "Cam 3"], "2. {1-3} numérote", o)
ok(o.get("zeros") == ["Cam 08", "Cam 09", "Cam 10"], "2b. {08-10} garde les zéros", o.get("zeros"))
ok(o.get("product") == ["Cam Rouge Haut", "Cam Rouge Bas", "Cam Bleue Haut", "Cam Bleue Bas"],
   "2c. les accolades se combinent (produit)", o.get("product"))
ok(o.get("glued") == ["Cam1", "Cam2"] and o.get("plain") == ["PGM"], "2d. collé au nom ; sans accolades : tel quel")
ok(o.get("cap") == 256, "2e. développement borné à 256 noms", o.get("cap"))
ok(o.get("withKids") == ["Cam 1", "Cam 1 A1", "Cam 1 A2", "Cam 1 Anc"]
   and o.get("noVideo") == ["Mic A1", "Mic A2"] and o.get("oneAudio") == ["Mic"],
   "2f. noms des ports créés = ceux du serveur (virtual_port_names)", o)
sys.path.insert(0, RACINE)
from app import project_ports                     # noqa: E402
ok(project_ports.virtual_port_names(["Cam 1"], True, 2, True) == o.get("withKids")
   and project_ports.virtual_port_names(["Mic"], False, 2, False) == o.get("noVideo"),
   "2g. … et le serveur dit pareil")

# 2h. Chaque clé i18n du panneau existe dans les deux langues.
bloc = src[src.index("/* ─── Ports du projet"):src.index("/* ─── Macros système")]
cles = sorted(set(re.findall(r"""t\('(projects\.[a-z_0-9]+)'\)""", bloc)))
manque = {}
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", lang + ".json"), encoding="utf-8"))
    manque[lang] = [k for k in cles if not cat.get(k)]
ok(len(cles) > 20 and not manque["fr"] and not manque["en"], "2h. clés i18n du panneau présentes (fr, en)",
   (len(cles), manque))

ws = open(os.path.join(RACINE, "templates", "workspace.html"), encoding="utf-8").read()
restes = [k for k in ("wsp-ports-list", "wspAddPort", "wspPortLabels", "wspDelPort", "loadPorts(")
          if k in ws]
ok(not restes, "3. la Vue d'exploitation n'a plus d'éditeur de ports", restes)

if ECHECS:
    print("\nÉCHEC — %d vérification(s)" % len(ECHECS))
    sys.exit(1)
print("\nOK — ports configurés sur /projects")
