#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The two READMEs must name things that EXIST, and name the same things.

★ WHY. The README is the first page a stranger reads, and the only one they cannot check
against anything. On 2026-11-08 it had been announcing a `control_surface` service for weeks —
twice in the English version. There is no such service: the MCU/HUI surfaces live in
`panel_audio`. Someone renamed the service and the README kept the old name, so the one claim
a reader would have chased led nowhere. The same page also listed five of the thirteen
documents of `docs/reference/`, a hand-kept list that had simply fallen behind.

★ TWO CHECKS, and the first is the one that bites.
  1. Every repository path the READMEs quote in backticks EXISTS. This is language-independent:
     it would have caught `control_surface` in either file, on its own, the day of the rename.
  2. Both READMEs quote the SAME set of paths and link the SAME documents. Nothing else keeps
     them aligned — there was no guard at all — and that is how the stale name survived in
     English after being discussed in French.

What this does NOT do: compare prose. Translation is a judgement, and a guard that demanded
sentence-for-sentence equality would be wrong as often as it was right. Paths and links are
facts, so those are what it holds.

Non-zero exit = a path that does not exist, or a divergence between the two files.
"""
import pathlib
import re
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent
FICHIERS = ("README.md", "README.en.md")

# Les préfixes qui désignent un chemin DU DÉPÔT. Un `backtick` qui ne commence pas par l'un
# d'eux est du code, une commande ou un nom de réglage — pas une promesse vérifiable ici.
PREFIXES = ("app/", "services/", "plugins/", "tools/", "tests/", "docs/", "node_agent/",
            "script_templates/", "static/", "templates/", "i18n/", "install/")
# Fichiers de la racine cités au fil du texte.
RACINE_FICHIERS = re.compile(r"^[A-Z][A-Z0-9_.-]*\.md$|^(main\.py|install\.sh|get\.sh|LICENSE)$")

echecs = []


def _jetons(texte):
    """Les chemins cités entre accents graves, et les cibles des liens Markdown."""
    vus = set()
    for brut in re.findall(r"`([^`\n]+)`", texte):
        v = brut.strip().rstrip(".,;:)")
        # Un gabarit (`plugins/<type>/plugin.json`) ne désigne aucun fichier réel : c'est une
        # FORME, et la vérifier produirait un faux échec permanent.
        if "<" in v or " " in v or "*" in v:
            continue
        if v.startswith(PREFIXES) or RACINE_FICHIERS.match(v):
            vus.add(v)
    for cible in re.findall(r"\]\(([^)#\s]+)", texte):
        if not cible.startswith(("http", "mailto")):
            vus.add(cible.strip())
    return vus


def main():
    jeux = {}
    for nom in FICHIERS:
        chemin = RACINE / nom
        if not chemin.exists():
            echecs.append("%s est absent" % nom)
            continue
        jeux[nom] = _jetons(chemin.read_text(encoding="utf-8"))

    # ── 1. tout ce qui est cité existe
    for nom, jeu in jeux.items():
        for v in sorted(jeu):
            if not (RACINE / v).exists():
                echecs.append("%s cite `%s` — ce chemin n'existe pas" % (nom, v))

    # ── 2. les deux disent la même chose
    if len(jeux) == 2:
        a, b = FICHIERS
        # Chacun lie vers l'autre : c'est une asymétrie VOULUE, pas une dérive. On l'exclut de
        # la comparaison, jamais de la vérification d'existence juste au-dessus.
        croise = set(FICHIERS)
        for seul, autre in ((a, b), (b, a)):
            for v in sorted((jeux[seul] - jeux[autre]) - croise):
                echecs.append("`%s` n'est cité que dans %s, pas dans %s" % (v, seul, autre))

    if echecs:
        print("\n  ✗ %d écart(s) :\n" % len(echecs))
        for e in echecs:
            print("    " + e)
        print("\n  Le README est la seule page qu'un lecteur ne peut recouper avec rien.")
        return 1
    total = len(jeux.get(FICHIERS[0], ()))
    print("  ✓ %d chemin(s) cité(s), tous existants, et les deux READMEs concordent" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
