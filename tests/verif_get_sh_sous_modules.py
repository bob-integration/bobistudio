#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de `get.sh` : la ref d'un SOUS-MODULE n'est pas celle du produit.
#
# ★ PANNE RÉELLE, 2026-09-02. `_recuperer` employait la MÊME ref pour le dépôt principal et pour
# ses sous-modules. Tant qu'on installait « main », présente partout, ça passait. Le jour où l'on
# a installé par ÉTIQUETTE, l'installation a échoué au premier sous-module : il n'existe aucun
# « v0.9.3 » dans `bobistudio-service-nmos`, qui vit sur ses propres numéros.
#
# Le message d'erreur, lui, était très bien écrit — il disait que le composant n'est pas
# optionnel et suggérait de vérifier l'accès au dépôt. Il désignait donc la mauvaise cause, avec
# assurance. Un bon message sur un mauvais diagnostic coûte plus cher que pas de message :
# l'installateur a cherché du côté des droits, là où il n'y avait rien.
#
# ⚠ AUCUN ACCÈS RÉSEAU. On vérifie la STRUCTURE du script et la logique de résolution, pas la
# disponibilité de GitHub.
#
#   $ ./venv/bin/python tests/verif_get_sh_sous_modules.py
import io
import json
import os
import re
import subprocess
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GET = os.path.join(RACINE, "get.sh")

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


print("get.sh — ref des sous-modules\n")

s = io.open(GET, encoding="utf-8").read()

controle("★ get.sh est un script bash valide",
         subprocess.run(["bash", "-n", GET], capture_output=True).returncode == 0)

controle("★★★ le SHA épinglé du sous-module est résolu",
         "_sha_sous_module" in s and "contents/$1?ref=$REF" in s,
         "sans ça on retombe sur la ref du produit, qui n'existe pas chez le composant")

# ★ LE CŒUR : l'APPEL à `_recuperer` dans la boucle des sous-modules doit porter une 4ᵉ ref.
#
# ⚠ Ce contrôle a d'abord cherché la chaîne n'importe où dans le bloc de la boucle — et il était
# MUET : `[ -n "$ref_sm" ]`, deux lignes plus haut, la contient aussi. Muté en retirant l'argument
# de l'appel, le banc restait vert. On isole donc la LIGNE D'APPEL.
_apres = s.split('for entree in "${SOUS_MODULES_REQUIS[@]}"; do', 1)
_appel = None
if len(_apres) == 2:
    for l in _apres[1].splitlines():
        if "_recuperer" in l:
            _appel = l.strip()
            break
controle("★★★ l'APPEL de la boucle passe une ref DISTINCTE",
         bool(_appel) and '"$ref_sm"' in _appel,
         "c'est LA correction : `_recuperer …` sans quatrième argument reprend $REF, celle du "
         "produit, qui n'existe pas chez le composant. Ligne trouvée : %r" % _appel)

controle("★★ un repli existe quand le SHA n'est pas résoluble",
         'ref_sm="main"' in s,
         "API injoignable ou quota épuisé ne doit pas rendre le produit ININSTALLABLE : « main » "
         "du composant reste préférable à un échec sec")

controle("★★★ l'archive est tentée aussi sur un SHA BRUT",
         re.search(r'tar\.gz/\$ref"', s) is not None,
         "un sous-module est épinglé sur un COMMIT, qui ne porte ni branche ni étiquette : sans "
         "ce troisième essai, le SHA résolu ne servirait à rien")

controle("★★ `_recuperer` accepte une ref en 4ᵉ argument, avec $REF par défaut",
         'ref="${4:-$REF}"' in s,
         "le défaut préserve les appels existants — le dépôt principal, lui, s'installe bien à "
         "la ref demandée")

controle("★★ ...et n'emploie plus $REF ailleurs dans son corps",
         len(re.findall(r'tar\.gz/refs/(?:heads|tags)/\$REF"', s)) == 0,
         "un seul reste et le sous-module repart sur la ref du produit")

# ═══ La logique de résolution, EXTRAITE DE get.sh et jouée hors ligne ═══════
#
# ⚠ Une première version recopiait ce fragment dans le banc. Il était donc MUET : muter get.sh ne
# changeait rien à ce qu'on exécutait. On teste ce que le script contient VRAIMENT.
_src_get = io.open(GET, encoding="utf-8").read()
_bloc = _src_get.split("_sha_sous_module()", 1)[-1]
_m = re.search(r"python3 -c '(.*?)'\s*2>/dev/null", _bloc, re.S)
controle("★★★ le fragment python de get.sh est extractible", bool(_m),
         "sans extraction, ce banc jouerait une COPIE et ne dirait rien du script")
EXTRAIT = _m.group(1) if _m else "import json,sys"


def _resoudre(charge):
    p = subprocess.run([sys.executable, "-c", EXTRAIT],
                       input=charge, capture_output=True, text=True)
    return p.stdout.strip(), p.returncode


sha, rc = _resoudre(json.dumps({"type": "submodule", "sha": "abc123", "path": "services/nmos"}))
controle("★★ un sous-module rend son SHA", sha == "abc123" and rc == 0)

sha, rc = _resoudre(json.dumps({"type": "dir", "sha": "abc123"}))
controle("★★★ un DOSSIER ordinaire ne rend rien",
         sha == "" and rc == 0,
         "le chemin peut avoir cessé d'être un sous-module : prendre son sha ferait chercher une "
         "archive de composant à un commit du produit")

for mauvais in ("", "pas du json", "[]", json.dumps({"message": "Not Found"})):
    sha, rc = _resoudre(mauvais)
    controle("★★ réponse inexploitable (%r) → silence, pas d'erreur" % mauvais[:18],
             sha == "" and rc == 0,
             "le script continue vers son repli ; une trace sur stderr ferait passer une "
             "installation saine pour une panne")

# ═══ L'en-tête : version affichée, et cadre centré PAR CALCUL ═════════
#
# C'est la première chose que voit quelqu'un qui installe le produit. Un cadre de travers y donne
# le ton — et il l'a donné : le sous-titre était décalé de deux caractères, remplissage posé à
# l'œil, signalé le 2026-09-02.
_src = io.open(GET, encoding="utf-8").read()

controle("★★★ l'installateur affiche SA version",
         "INSTALLEUR_VERSION=" in _src and "installateur $INSTALLEUR_VERSION" in _src,
         "quand une installation échoue, on demande le numéro affiché. Le script est servi par le "
         "site, donc mis en cache : sans lui, rien ne dit lequel la personne exécute.")

# Lu dans le FICHIER, pas importé : ce banc ne doit dépendre d'aucun module du produit — il
# vérifie un script autonome, téléchargé et exécuté avant que quoi que ce soit soit installé.
_vprod = re.search(r'VERSION = "([^"]+)"',
                   io.open(os.path.join(RACINE, "app", "version.py"), encoding="utf-8").read())
_ver = re.search(r'INSTALLEUR_VERSION="([^"]+)"', _src)
controle("★★ ...et c'est la SIENNE, pas celle du produit",
         bool(_ver) and bool(_vprod) and _ver.group(1) != _vprod.group(1),
         "get.sh est téléchargé AVANT qu'une version de produit soit choisie : y afficher celle "
         "du produit ne dirait rien de l'outil qui échoue. Trouvé %r"
         % (_ver.group(1) if _ver else None))

controle("★★★ le remplissage du cadre est CALCULÉ, pas compté à la main",
         "_cadre_ligne" in _src and "(l - ${#t}) / 2" in _src,
         "un remplissage écrit en dur se décale dès qu'on touche au texte — c'est exactement ce "
         "qui est arrivé au sous-titre, et un numéro de version change de longueur à chaque bump")

# Le rendu, joué pour de vrai : c'est la seule preuve que le calcul centre.
_fn = re.search(r"(_cadre_ligne\(\) \{.*?\n\})", _src, re.S)
controle("★ la fonction de cadre est extractible", bool(_fn))
if _fn:
    for _t in ("B O B I . S T U D I O", "Installation depuis GitHub",
               "installateur 2026.09.02", "x", "un texte nettement plus long que les autres"):
        _out = subprocess.run(["bash", "-c", _fn.group(1) + '\n_cadre_ligne "%s"' % _t],
                              capture_output=True, text=True).stdout.rstrip("\n")
        _i, _j = _out.find("║"), _out.rfind("║")
        _in = _out[_i + 1:_j]
        _g, _d = len(_in) - len(_in.lstrip()), len(_in) - len(_in.rstrip())
        controle("★★ « %s » centré" % (_t[:34]),
                 len(_in) == 54 and abs(_g - _d) <= 1,
                 "largeur=%d gauche=%d droite=%d — le cadre doit garder sa largeur ET rester "
                 "centré quelle que soit la longueur du texte" % (len(_in), _g, _d))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
