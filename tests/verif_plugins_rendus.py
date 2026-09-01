#!/usr/bin/env python3
"""Rend CHAQUE plugin et vérifie que le résultat est du Python valide.

Le garde-fou existant (`plugins._scan`) fait un dry-run `.format` : il attrape les accolades non
doublées et SKIPPE le plugin fautif. Il ne compile rien. Un script rendu syntaxiquement invalide —
une indentation cassée par une édition, par exemple — franchit donc le scan, s'inscrit au registre
avec sa version, et n'explose QUE dans le conteneur au déploiement, loin de sa cause.

Vécu le 2026-08-19 : un remplacement littéral appliqué à trois points de publication d'indentations
différentes (20, 16 et 24 espaces) a produit un `IndentationError` dans `udc`. Le registre l'a
accepté sans broncher.

Ce banc ne déploie rien et n'interroge aucun conteneur.

    ./venv/bin/python tools/verif_plugins_rendus.py
"""
import ast
import os
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)
os.chdir(_R)

import main                       # noqa: E402,F401  (charge la config + le registre)
from app import plugins           # noqa: E402

# Params neutres : on vérifie la SYNTAXE du rendu, pas un déploiement. Les plugins qui exigent
# d'autres clés les prendront par défaut — `render_script` ne fait qu'un `.format`.
PARAMS = {"shm_name": "in", "shm_out": "out", "audio_shm": "in_audio"}


def run():
    types = sorted(plugins.REGISTRY)
    print("%d type(s) au registre" % len(types))
    # ⚠ UN PLUGIN SKIPPÉ DISPARAÎT DU REGISTRE — et donc de ce banc, qui annonçait alors
    # « tous compilent » en n'en ayant pas vu un seul de cassé. C'est le même angle mort que
    # celui qu'on corrige : l'absence doit être un ÉCHEC, pas un silence. On confronte donc le
    # registre au disque.
    sur_disque = {d for d in os.listdir(os.path.join(_R, "plugins"))
                  if os.path.isfile(os.path.join(_R, "plugins", d, "plugin.json"))}
    absents = sorted(sur_disque - set(types))
    for t in absents:
        print(" ÉCHEC %-18s présent sur disque mais ABSENT du registre (skippé au scan :"
              " accolade non doublée ?)" % t)
    ko, skips = [], []
    for t in types:
        try:
            src = plugins.render_script(t, dict(PARAMS), "%s-verif" % t)
        except Exception as e:
            skips.append((t, "rendu impossible : %s: %s" % (type(e).__name__, e)))
            continue
        if not src or not src.strip():
            skips.append((t, "rendu vide"))
            continue
        try:
            ast.parse(src)
        except SyntaxError as e:
            ko.append((t, "ligne %s : %s" % (e.lineno, e.msg)))
            continue
        print("  ok   %-18s %5d lignes" % (t, len(src.splitlines())))

    for t, why in skips:
        print("  n/a  %-18s %s" % (t, why))
    for t, why in ko:
        print(" ÉCHEC %-18s %s" % (t, why))

    total = len(ko) + len(absents)
    print("\n%s" % ("Tous les plugins rendus compilent."
                    if not total else "%d plugin(s) invalide(s) ou absent(s) du registre." % total))
    return 1 if total else 0


if __name__ == "__main__":
    c = run()
    sys.stdout.flush()
    os._exit(c)
