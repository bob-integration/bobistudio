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
_apres = s.split('for entree in "${SOUS_MODULES_UTILES[@]}"; do', 1)
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

# ═══ AUCUN COMPOSANT N'EST BLOQUANT ════════════════════
#
# ★ L'installation ABANDONNAIT si `services/nmos` était injoignable, en affirmant que
# « l'orchestrateur ne démarrerait pas ». C'était vrai quand main.py l'importait sans garde ; ça
# ne l'est plus. Et le remède suggéré — installer depuis le catalogue — exige que le produit
# TOURNE : un cercle dont l'utilisateur ne peut pas sortir.
_g = io.open(GET, encoding="utf-8").read()
controle("★★★ aucun composant n'est déclaré REQUIS",
         "SOUS_MODULES_REQUIS" not in _g and "SOUS_MODULES_UTILES" in _g,
         "le nom porte la règle : ce qui est « utile » se rattrape, ce qui est « requis » bloque")

_boucle = _g.split("SOUS_MODULES_UTILES[@]", 1)[-1].split("\ndone", 1)[0]
controle("★★★ un composant manquant n'interrompt PAS l'installation",
         "die " not in _boucle,
         "un `die` dans cette boucle rend le produit ININSTALLABLE dès qu'un composant est "
         "injoignable. Trouvé : %r"
         % [l.strip() for l in _boucle.splitlines() if "die " in l])

controle("★★ ...et l'absence est DITE, pas avalée",
         "manques+=" in _g and "warn " in _boucle,
         "continuer en silence laisserait découvrir le manque à l'usage")

# Le contrôle d'intégrité final ne doit plus exiger un composant facultatif : il annulerait
# l'installation quelques lignes après qu'on ait décidé de continuer sans lui.
_verif = _g.split("manque = [c for c in", 1)[-1].split("]", 1)[0]
controle("★★★ le contrôle d'intégrité final n'exige aucun composant",
         "services/" not in _verif,
         "il annulerait l'installation juste après. Contenu : %s" % _verif.strip()[:110])

# ═══ L'en-tête : version affichée, et cadre centré PAR CALCUL ═════════
#
# C'est la première chose que voit quelqu'un qui installe le produit. Un cadre de travers y donne
# le ton — et il l'a donné : le sous-titre était décalé de deux caractères, remplissage posé à
# l'œil, signalé le 2026-09-02.
_src = io.open(GET, encoding="utf-8").read()

controle("★★★ l'installateur affiche SA version",
         "INSTALLEUR_VERSION=" in _src and '_t installateur "$INSTALLEUR_VERSION"' in _src,
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
               "installateur %s" % (_ver.group(1) if _ver else "?"), "x", "un texte nettement plus long que les autres"):
        _out = subprocess.run(["bash", "-c", _fn.group(1) + '\n_cadre_ligne "%s"' % _t],
                              capture_output=True, text=True).stdout.rstrip("\n")
        _i, _j = _out.find("║"), _out.rfind("║")
        _in = _out[_i + 1:_j]
        _g, _d = len(_in) - len(_in.lstrip()), len(_in) - len(_in.rstrip())
        controle("★★ « %s » centré" % (_t[:34]),
                 len(_in) == 54 and abs(_g - _d) <= 1,
                 "largeur=%d gauche=%d droite=%d — le cadre doit garder sa largeur ET rester "
                 "centré quelle que soit la longueur du texte" % (len(_in), _g, _d))

# ═══ BILINGUE ══════════════════════════════════════
_g2 = io.open(GET, encoding="utf-8").read()

controle("★★ la langue par défaut vient de l'ENVIRONNEMENT",
         "LC_ALL:-${LC_MESSAGES" in _g2 and 'fr*|FR*)' in _g2,
         "une machine en anglais doit parler anglais sans qu'on le demande ; la question ne sert "
         "qu'à contredire ce défaut")

controle("★★ BOBI_LANG court-circuite la question",
         'UI="${BOBI_LANG:-}"' in _g2 and '[ -z "${BOBI_LANG:-}" ] && [ -t 0 ]' in _g2,
         "en non-interactif aucune question ne peut être posée, et les journaux doivent quand "
         "même être lisibles par leur destinataire")

# ★ La langue choisie DOIT suivre : l'installeur la reposait trois secondes plus tard.
controle("★★★ la langue est transmise à l'installeur",
         'export BOBI_LANG="$UI"' in _g2,
         "sans ça, deux fois la même question à trois secondes d'intervalle — et l'on doute que "
         "la première ait servi")

_inst = io.open(os.path.join(RACINE, "install", "install.py"), encoding="utf-8").read()
controle("★★★ l'installeur HONORE la langue héritée",
         'os.environ.get("BOBI_LANG")' in _inst and "_premier" in _inst,
         "transmettre sans lire ne sert à rien")
controle("★★★ ...mais le choix reste ATTEIGNABLE en reculant",
         "_premier = False" in _inst and "while True:" in _inst,
         "reculer depuis le menu principal ramène au choix de langue : c'est le seul moyen d'en "
         "changer. Le sauter définitivement enfermerait dans une réponse donnée à l'étape d'avant")

# Toutes les clés employées doivent exister dans la table, et réciproquement.
_cles_table = set(re.findall(r"^\s{4}([a-z_0-9]+)\)\s+fr=", _g2, re.M))
_cles_usage = set(re.findall(r'_t ([a-z_0-9]+)', _g2))
controle("★★★ aucune clé employée n'est absente de la table",
         not (_cles_usage - _cles_table),
         "une clé absente s'affiche… vide, et le message disparaît sans bruit. Manquantes : %s"
         % sorted(_cles_usage - _cles_table))
controle("★ aucune clé de la table n'est inutilisée",
         not (_cles_table - _cles_usage),
         "orphelines : %s" % sorted(_cles_table - _cles_usage))

# ⚠ BORNÉ À L'ENTRÉE. Une première version cherchait `en=` n'importe où APRÈS la clé : elle
# trouvait donc celle de la clé SUIVANTE, et restait verte quand une traduction disparaissait.
# Chaque entrée d'un `case` bash finit par `;;` — c'est la borne.
_entrees = dict(re.findall(r"^\s{4}([a-z_0-9]+)\)((?:.|\n)*?);;", _g2, re.M))
_sans_en = sorted(k for k in _cles_table if 'en="' not in _entrees.get(k, ""))
controle("★★ chaque clé a sa traduction anglaise",
         not _sans_en, "sans traduction : %s" % _sans_en)

# ═══ LA PAUSE AVANT L'INSTALLEUR ═══════════════════════
controle("★★ une pause laisse LIRE ce que get.sh a affiché",
         "_t pause" in _g2 and "sleep 1" in _g2,
         "l'installeur efface l'écran : sans pause, les avertissements non bloquants — un "
         "composant non récupéré — disparaissent avant d'avoir été lus")
controle("★★★ ...et elle est sautée en non-interactif",
         re.search(r'if \[ -t 0 \]; then\n  for _s in 3 2 1', _g2) is not None,
         "attendre devant une CI ne montre rien à personne")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
