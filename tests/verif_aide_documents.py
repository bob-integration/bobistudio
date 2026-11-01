#!/usr/bin/env python3
"""Garde : tout document que le serveur sait rendre dans l'Aide y est réellement AFFICHÉ.

DÉFAUT D'ORIGINE (relevé le 2026-10-11) : `HA.md`, `NODE_AGENT.md` et `THIRD-PARTY-NOTICES.md`
étaient dans la liste blanche `_DOCS_RACINE` — `/api/doc/<nom>` les servait — mais AUCUN article
de `templates/aide.html` ne les chargeait. Les mettre à jour ne changeait donc rien pour un
lecteur de l'Aide, et CLAUDE.md affirmait le contraire. La liste blanche dit « peut être rendu »,
seul un conteneur `data-doc` dit « est rendu ».

Vérifie aussi l'inverse — un conteneur dont le nom n'est pas servi resterait sur « Chargement… » —
et que chaque article déclaré dans CORE_ARTICLES a son gabarit (et réciproquement).
"""
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

# ── Isolation de la base : un test n'OUVRE JAMAIS la production, même en lecture ──────
# `connect_guarded` ne donne la base de prod à un test qu'en lecture seule et l'avertit sur
# stderr. Lire la prod reste une dépendance cachée : le verdict dépendrait des réglages du site,
# et sur un exécuteur neuf (CI) la base est VIDE — le test passe alors en mesurant du vide.
# Base jetable, posée AVANT le premier accès : la connexion est mémorisée par thread, donc
# surcharger DB_PATH après coup n'isole plus rien.
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = os.path.join(
    _tempfile_iso.mkdtemp(prefix="verif-iso-"), "principale.db")
# ⚠ On NE touche PAS `PRODUCTION_DB_PATH` : il doit continuer de désigner la VRAIE base de
# production. Le poser sur la base jetable la ferait prendre POUR la production par la
# garde, qui — le fichier n'existant pas encore — la remplacerait par une autre base
# jetable à elle : le test écrirait alors ailleurs que là où il croit. Mesuré le 25/10.
_dbm_iso._tls.__dict__.pop("conn", None)
_dbm_iso.init_db()

from app.routes.pages import _DOCS_RACINE                                 # noqa: E402

aide = open(os.path.join(RACINE, "templates", "aide.html"), encoding="utf-8").read()
conteneurs = set(re.findall(r'class="wiki-doc-content" data-doc="([^"$]+)"', aide))
gabarits = set(re.findall(r'<template id="wiki-art-([^"]+)"', aide))
debut = aide.index("const CORE_ARTICLES")
declares = set(re.findall(r"\{ id: '([^']+)'", aide[debut:aide.index("];", debut)]))

echecs = []
for nom in sorted(set(_DOCS_RACINE) - conteneurs):
    echecs.append("%s (%s) est servi par /api/doc mais AUCUN article ne l'affiche"
                  % (nom, _DOCS_RACINE[nom]))
for nom in sorted(conteneurs - set(_DOCS_RACINE)):
    echecs.append("un article charge %r, que /api/doc ne sert pas (resterait « Chargement… »)" % nom)
for i in sorted(declares - gabarits):
    echecs.append("article %r déclaré dans CORE_ARTICLES sans gabarit wiki-art-%s" % (i, i))
for i in sorted(gabarits - declares):
    echecs.append("gabarit wiki-art-%s jamais listé dans CORE_ARTICLES (inatteignable)" % i)

print("%d documents servis, %d affichés ; %d articles déclarés, %d gabarits"
      % (len(_DOCS_RACINE), len(conteneurs), len(declares), len(gabarits)))
if echecs:
    print("ÉCHEC :")
    for e in echecs:
        print("  - " + e)
    sys.exit(1)
print("OK — chaque document servi est affiché, chaque article est atteignable")
