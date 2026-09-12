#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# check_motdepasse.py — confronte le validateur Python et son miroir JavaScript.
#
# POURQUOI CET OUTIL EXISTE
#
# La règle de robustesse existe DEUX FOIS : `app/auth.py:valider_motdepasse` (qui fait foi) et
# `static/js/motdepasse.js` (qui montre à l'utilisateur ce qui manque pendant qu'il tape). Deux
# implémentations d'une même règle divergent toujours, et la divergence est SILENCIEUSE dans le
# sens qui compte : le navigateur affiche cinq coches vertes, l'utilisateur clique, le serveur
# refuse. L'interface a alors l'air cassée alors que c'est elle qui a menti.
#
# CE QU'IL FAIT
#
# Fait tourner les deux sur le même corpus et compare la LISTE DE RÈGLES rendue, pas un booléen :
# deux verdicts « refusé » pour des raisons différentes sont aussi une divergence.
#
#   ./venv/bin/python tools/check_motdepasse.py
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.auth import (valider_motdepasse, PWD_INTERDITS, PWD_PROFILS,  # noqa: E402
                      pwd_exigences)

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(RACINE, "static", "js", "motdepasse.js")

# ⚠ IDENTITÉ FICTIVE, ET ELLE DOIT LE RESTER. Ce corpus contenait une identité RÉELLE —
# prénom, nom, adresse e-mail, et jusqu'à une année de naissance dans un mot de passe
# d'essai. Le dépôt est public : une donnée personnelle y reste indéfiniment, y compris
# dans l'historique. Le test ne perd rien au change, il vérifie que le validateur refuse
# un mot de passe DÉRIVÉ de l'identité du compte, quelle qu'elle soit.
USERNAME = "jdupont"
EXTRAS = ["Jean", "Dupont", "jdupont@exemple.fr"]

# Corpus : les cas de bord, pas des exemples décoratifs. Un cas par mécanisme, plus les
# frontières exactes des seuils (11/12 signes, 15/16, 19/20) où deux implémentations se séparent.
CORPUS = [
    "", "a", "ab", "aaa", "        ", "é", "ééééééééééééé",
    "azerty", "azertyuiop", "qwerty123456", "wxcvbn12",
    "MotDePass1!", "MotDePasse1!", "MotDePasseLong", "MotDePasseLongue",
    "phrase de passe tres longue", "le chat gris dort ici",
    "Xk9!pQ2#mL4v", "Xk9!pQ2#mL4", "abcdWXYZ0000", "ZZZZzzzz1111",
    "jdupont", "jdupont2026!", "Jean.Dupont.1978", "JDUPONT-long-1",
    "bobi", "bobistudio", "controleur-1026", "BobiStudio2026!",
    "0123456789ab", "9876543210ab", "lkjhgfdsa123", "poiuytrewq12",
    "  espaces  au  bord  ", "Tr0ubad0ur!Vent", "aA1!aA1!aA1!",
    "été-2026-très-long-mdp", "ÀÉÎÕÜàéîõü!9xyz",
] + sorted(PWD_INTERDITS) + [m + "2026" for m in sorted(PWD_INTERDITS)]

# Corpus passé par l'ENVIRONNEMENT et pas en argument : `node -e` décale argv, et un corpus
# qui grandit finirait par buter sur la longueur maximale d'une ligne de commande.
# ⚠ TOUS LES PROFILS, pas seulement le défaut : les seuils sont justement ce qu'un profil
# déplace, donc ne confronter que « standard » laisserait « souple » et « stricte » sans filet.
script = """
const valider = require(%s);
const cas = JSON.parse(process.env.CAS);
const out = cas.map(([p, ex]) => valider(p, %s, %s, ex));
process.stdout.write(JSON.stringify(out));
""" % (json.dumps(JS), json.dumps(USERNAME), json.dumps(EXTRAS))

cas = [(pwd, pwd_exigences(prof)) for prof in sorted(PWD_PROFILS) for pwd in CORPUS]
env = dict(os.environ, CAS=json.dumps(cas))
r = subprocess.run(["node", "-e", script], capture_output=True, text=True, env=env)
if r.returncode:
    print("node a échoué :\n" + r.stderr, file=sys.stderr)
    sys.exit(2)
cote_js = json.loads(r.stdout)

ecarts = 0
for (pwd, ex), js in zip(cas, cote_js):
    py = valider_motdepasse(pwd, USERNAME, EXTRAS, exigences=ex)
    if sorted(py) != sorted(js):
        ecarts += 1
        print("✗ [%s] %-30r\n    python : %s\n    js     : %s"
              % (ex["profil"], pwd, sorted(py), sorted(js)))

print("%d confrontations (%d mots de passe × %d profils) — %d divergence(s)"
      % (len(cas), len(CORPUS), len(PWD_PROFILS), ecarts))
sys.exit(1 if ecarts else 0)