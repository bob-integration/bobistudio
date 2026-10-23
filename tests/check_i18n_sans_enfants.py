#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Un élément `[data-i18n]` ne doit contenir AUCUN élément enfant.

★ POURQUOI. Au montage, l'interface d'un plugin traduit en réécrivant le `textContent` de chaque
`[data-i18n]` — ce qui DÉTRUIT tout ce que l'élément contient. Vécu (transcoder, du commit de
traduction 0384320 jusqu'au 2026-10-11) : le bouton d'onglet « File d'attente » portait
`data-i18n` ET le badge de compte ; la traduction effaçait le badge, `renderJobs` plantait sur un
badge nul, un `catch(e) {}` avalait l'erreur — et la file d'attente n'a jamais rien affiché dans
un navigateur, jobs en cours compris. Invisible hors navigateur : sans `window.t`, rien n'est
réécrit, et tout marche.

Règle : le texte traduit va dans son propre `<span data-i18n>`, à côté des éléments vivants.

    ./venv/bin/python tests/check_i18n_sans_enfants.py [fichier.html …]
"""
import glob
import os
import sys
from html.parser import HTMLParser

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDES = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
         "track", "wbr"}


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pile = []          # [(tag, clé i18n ou None, [enfants])]
        self.fautes = []

    def handle_starttag(self, tag, attrs):
        if self.pile:
            self.pile[-1][2].append(tag)
        if tag in VIDES:
            return
        cle = dict(attrs).get("data-i18n")
        self.pile.append((tag, cle, [], self.getpos()[0]))

    def handle_endtag(self, tag):
        if tag in VIDES:
            return
        while self.pile:
            t, cle, enfants, ligne = self.pile.pop()
            if cle and enfants:
                self.fautes.append((ligne, cle, enfants))
            if t == tag:
                break


def main(fichiers):
    if not fichiers:
        fichiers = sorted(glob.glob(os.path.join(_R, "plugins", "*", "*.html"))
                          + glob.glob(os.path.join(_R, "services", "*", "*.html")))
    total = 0
    for f in fichiers:
        s = _Scan()
        s.feed(open(f, encoding="utf-8").read())
        for ligne, cle, enfants in s.fautes:
            total += 1
            print("  ✗ %s:%d  data-i18n=\"%s\" contient <%s> — la traduction l'effacera"
                  % (os.path.relpath(f, _R), ligne, cle, ">, <".join(enfants)))
    if total:
        print("%d élément(s) traduit(s) avec des enfants : mettre le texte dans son propre <span data-i18n>." % total)
        sys.exit(1)
    print("  ✓ aucun [data-i18n] avec élément enfant (%d fichiers HTML de plugins/services)" % len(fichiers))


if __name__ == "__main__":
    main(sys.argv[1:])
