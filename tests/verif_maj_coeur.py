#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""La version de Bobi.Studio est VISIBLE, et la page Mises à jour la confronte à l'amont.

★ CE QU'ON RÉPARE. `app/version.py` fait autorité depuis le 2026-09-02, et deux routes savent
comparer cette instance à la dernière release publiée (`GET /api/update/core`) puis l'appliquer
(`POST /api/update/core/apply`). Aucune page n'appelait ni l'une ni l'autre : mesuré, la chaîne
« 0.9.5 » n'apparaissait dans AUCUN rendu. Un exploitant ne pouvait pas dire ce qu'il faisait
tourner, et un utilisateur extérieur n'avait aucun chemin vers la version suivante — alors que
ses plugins, eux, se mettaient à jour depuis cette même page.

Deux choses vérifiées, et la seconde est celle qui casse en silence :

  1. la version APPARAÎT dans les pages qui comptent, y compris celles qui n'ont pas de session
     (login) — c'est justement à quelqu'un qui n'est pas entré que le support la demande ;

  2. `catCoreCharger()` est EXÉCUTÉ sur les cinq formes que la route peut rendre. Un rendu par
     concaténation de chaînes passe `node --check` quoi qu'il arrive : seule l'exécution dit si
     l'état « pas d'artefact » affiche bien son explication au lieu d'un bouton qui échouerait.

⚠ La mise à jour n'est PAS appliquée ici : elle tire du code depuis GitHub et redémarre le
service. C'est du banc, jamais de la CI.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, database                                   # noqa: E402

ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


# Les cinq formes que `GET /api/update/core` peut rendre, et ce qu'on doit lire à l'écran.
# ⚠ AUCUN NUMÉRO DE VERSION EN DUR. `ICI` est la version réelle du code — l'y recopier obligerait
# à éditer ce fichier à chaque release, et un `sed` global y a déjà rendu la version installée
# IDENTIQUE à la publiée, ce qui vidait de son sens le cas « mise à jour disponible ». `AMONT` est
# volontairement un numéro distinct et plus élevé.
from app.version import VERSION as ICI                              # noqa: E402
AMONT = "99.0.0"

CAS = [
    ("à jour",
     {"version_installee": ICI, "disponible": False,
      "derniere": {"version": ICI, "tag": "v" + ICI, "artefacts": ["x"], "applicable": True}},
     [ICI], ["cat-core-btn"]),
    ("mise à jour applicable → un bouton",
     {"version_installee": ICI, "disponible": True,
      "derniere": {"version": AMONT, "tag": "v" + AMONT, "applicable": True, "url": "u"}},
     ["cat-core-btn", AMONT], []),
    ("mise à jour SANS artefact → pas de bouton, une explication",
     {"version_installee": ICI, "disponible": True,
      "derniere": {"version": AMONT, "tag": "v" + AMONT, "applicable": False,
                   "url": "https://example.invalid/r"}},
     ["cat-core-note"], ["cat-core-btn"]),
    ("pré-version signalée",
     {"version_installee": ICI, "disponible": True,
      "derniere": {"version": AMONT, "tag": "v" + AMONT, "applicable": True, "prerelease": True}},
     ["cat-chip new"], []),
    ("amont injoignable → on montre quand même la version locale",
     {"version_installee": ICI, "disponible": False, "derniere": None},
     [ICI, "cat-chip old"], ["cat-core-btn"]),
]

# Le chrome de la page (témoin de connexion, minuteries, stockage local) s'exécute au chargement :
# on lui donne de quoi ne pas planter, sans rien simuler de plus que nécessaire.
STUB = """
global.window = global;
global.window.addEventListener = function(){};
global.setInterval = function(){}; global.setTimeout = function(){};
global.location = { href: '', hash: '', reload: function(){} };
global.localStorage = { getItem: function(){ return null; }, setItem: function(){},
                        removeItem: function(){} };
global.navigator = { onLine: true, language: 'fr' };
var _sortie = '';
var _el = { set innerHTML(v){ _sortie = v; }, get innerHTML(){ return _sortie; },
            set textContent(v){ _sortie = v; }, get textContent(){ return _sortie; } };
global.document = { getElementById: function(id){ return id === 'cat-core' ? _el : null; },
                    querySelectorAll: function(){ return []; },
                    querySelector: function(){ return null; },
                    addEventListener: function(){}, readyState: 'complete',
                    body: { classList: { add: function(){}, remove: function(){} } } };
global.REPONSE = null;
global.fetch = async function(){ return { json: async () => global.REPONSE, ok: true, status: 200 }; };
"""


def main():
    chemin = os.path.join(tempfile.mkdtemp(), "t.db")
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    database._tls.__dict__.pop("conn", None)
    database.init_db()

    import main as _main
    from app.auth import hash_password
    from app.version import VERSION

    database.db_create_user("essai", hash_password("Tulipe-Vent-9312"), "admin", None, None, None)
    cli = _main.app.test_client()

    # ── 1. La version est VISIBLE. `/login` d'abord : sans session, c'est le cas qui prouve
    #      que l'injection ne dépend pas d'une route métier.
    print("\n── la version apparaît dans les pages")
    verifier(VERSION in cli.get("/login").get_data(as_text=True),
             f"/login affiche {VERSION} (sans session)")
    # `/setup` ne s'affiche que tant qu'AUCUN compte n'existe : on l'interroge donc avant de
    # créer l'administrateur, avec un client neuf.
    import main as _m
    _base2 = os.path.join(tempfile.mkdtemp(), "t2.db")
    config.DB_PATH = _base2
    database.DB_PATH = _base2
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    verifier(VERSION in _m.app.test_client().get("/setup").get_data(as_text=True),
             f"/setup affiche {VERSION} (premier démarrage, aucun compte)")
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    database._tls.__dict__.pop("conn", None)
    with cli.session_transaction() as sess:
        sess["user_id"] = database.db_get_user("essai")["id"]
    for url in ("/", "/aide", "/settings", "/setup/wizard?force=1"):
        verifier(VERSION in cli.get(url).get_data(as_text=True), f"{url} affiche {VERSION}")

    # ── 2. Le bloc du cœur, EXÉCUTÉ sur chaque forme de réponse.
    print("\n── catCoreCharger() sur les cinq réponses possibles")
    page = cli.get("/settings").get_data(as_text=True)
    blocs = "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.S))
    tmp = tempfile.mkdtemp()
    for libelle, reponse, attendus, interdits in CAS:
        drive = ("\nglobal.REPONSE = %s;\n"
                 "catCoreCharger().then(function(){ console.log(_sortie); });\n"
                 % json.dumps(reponse))
        f = os.path.join(tmp, "c.js")
        open(f, "w", encoding="utf-8").write(STUB + blocs + drive)
        r = subprocess.run(["node", f], capture_output=True, text=True)
        if r.returncode != 0:
            verifier(False, f"{libelle} → le script a planté : {r.stderr.strip()[:160]}")
            continue
        html = r.stdout
        ok = all(a in html for a in attendus) and not any(i in html for i in interdits)
        verifier(ok, libelle)
        if not ok:
            print("      rendu :", html.strip()[:200])

    print()
    if ECHECS:
        print(f"✗ {len(ECHECS)} échec(s)")
        return 1
    print("✓ tout est vert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
