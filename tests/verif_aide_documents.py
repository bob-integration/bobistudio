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
