#!/usr/bin/env python3
"""Les tables `window.PLUGIN_*` de deploy_palette.html doivent être posées HORS du <template>.

Pourquoi : un <script> dans un <template> ne s'exécute qu'au clonage, donc à la première ouverture
de la palette. L'onglet Création (forms.html) lit les mêmes tables ; tant que la palette n'avait pas
été ouverte, il les voyait indéfinies et retombait sur les défauts — format vidéo demandé à TOUS les
types, console audio comprise (constaté le 2026-10-18). Aucune erreur nulle part : échec silencieux.
Lecture du gabarit seule, aucun effet de bord.
"""
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GABARIT = os.path.join(RACINE, "templates", "deploy_palette.html")
TABLES = ("PLUGIN_VIDEO_FORMAT", "PLUGIN_VERSIONS", "PLUGIN_CONFIG_SCHEMAS")


def verifier(texte):
    fails = []
    # Commentaires HTML et Jinja retirés : ils peuvent citer « <template> » en toutes lettres.
    texte = re.sub(r"<!--.*?-->|\{#.*?#\}", "", texte, flags=re.S)
    dedans = "".join(re.findall(r"<template\b.*?</template>", texte, re.S))
    dehors = re.sub(r"<template\b.*?</template>", "", texte, flags=re.S)
    for t in TABLES:
        affect = re.compile(r"window\.%s\s*=" % t)
        if affect.search(dedans):
            fails.append("%s posée DANS un <template> : indéfinie hors palette" % t)
        if not affect.search(dehors):
            fails.append("%s n'est posée nulle part hors <template>" % t)
    return fails


def main():
    with open(GABARIT, encoding="utf-8") as f:
        fails = verifier(f.read())
    for t in TABLES:
        print(("  ✗ " if any(t in x for x in fails) else "  ✓ ") + t)
    print()
    if fails:
        for x in fails:
            print("✗ " + x)
        return 1
    print("✓ tables des plugins visibles par l'onglet Création sans ouvrir la palette")
    return 0


if __name__ == "__main__":
    sys.exit(main())
