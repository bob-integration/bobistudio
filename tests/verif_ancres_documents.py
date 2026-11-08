#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""A link to a section of another document must land somewhere.

★ WHY. The README of 2026-11-08 pointed twice at `INSTALL.md#9-avant-la-première-antenne-
étalonner-sur-votre-matériel`. That anchor does not exist: where the em dash sits, GitHub emits
TWO hyphens, not one. The two links were dead on the front page of the public repository, and
they pointed at the one piece of advice that matters most before going on air. Two people had
reviewed the page, and both carried the same wrong model of the rendering.

A dead anchor is silent. The browser opens the document at the top, the reader believes the
section does not exist, and nothing anywhere says otherwise.

★ WHY THE ALGORITHM CAN BE TRUSTED. It was not written from memory. `INSTALL.md` was rendered
by GitHub itself (`POST /markdown`), its 38 heading ids extracted, and this function verified to
reproduce all 38 — accents, `œ`, digits, double hyphens and all. Should a future document show a
divergence, re-render it the same way rather than patching the function by guesswork.

Offline: no network here, the anchors are recomputed from the headings.

Non-zero exit = at least one link points at a section that does not exist.
"""
import pathlib
import re
import sys
import unicodedata

RACINE = pathlib.Path(__file__).resolve().parent.parent
# Les documents PUBLICS de la racine, ceux qu'un visiteur lit et parcourt.
DOCS = ("README.md", "README.en.md", "INSTALL.md", "INFRASTRUCTURE.md", "HA.md",
        "NODE_AGENT.md", "THIRD-PARTY-NOTICES.md", "CONTRIBUTING.md", "CHANGELOG.md")
LIEN = re.compile(r"\]\(([A-Za-z0-9_.-]+\.md)#([^)\s]+)\)")
TITRE = re.compile(r"^#{1,6} +(.+?)\s*$", re.M)


def ancre(titre):
    """L'identifiant que GitHub pose sur un titre.

    Minuscules, on jette tout ce qui n'est ni alphanumérique, ni espace, ni tiret, ni souligné
    — en gardant les diacritiques combinants — puis les espaces deviennent des tirets. Un tiret
    cadratin entouré d'espaces disparaît donc en laissant DEUX tirets, et c'est exactement le
    cas qui a produit les liens morts.
    """
    t = titre.strip().lower()
    t = "".join(c for c in t
                if c.isalnum() or c in " -_" or unicodedata.category(c) == "Mn")
    return t.replace(" ", "-")


def main():
    echecs, verifies = [], 0
    cache = {}
    for nom in DOCS:
        src = RACINE / nom
        if not src.exists():
            continue
        texte = src.read_text(encoding="utf-8")
        for cible, frag in LIEN.findall(texte):
            dest = RACINE / cible
            if not dest.exists():
                echecs.append("%s renvoie à `%s`, qui n'existe pas" % (nom, cible))
                continue
            if cible not in cache:
                cache[cible] = {ancre(t) for t in TITRE.findall(
                    dest.read_text(encoding="utf-8"))}
            verifies += 1
            if frag not in cache[cible]:
                echecs.append("%s → %s#%s : cette section n'existe pas" % (nom, cible, frag))

    if echecs:
        print("\n  ✗ %d lien(s) mort(s) :\n" % len(echecs))
        for e in echecs:
            print("    " + e)
        print("\n  Un lecteur arrive en haut du document et croit la section absente.")
        return 1
    print("  ✓ %d lien(s) de section vérifié(s), tous atteignent un titre" % verifies)
    return 0


if __name__ == "__main__":
    sys.exit(main())
