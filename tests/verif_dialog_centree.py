#!/usr/bin/env python3
"""Garde : une <dialog> modale reste centrée malgré le reset `* { margin: 0 }` de base.css.

Le navigateur centre une <dialog> ouverte par showModal() grâce à `margin: auto`. Le reset global
l'écrase, et la modale se colle en haut à gauche. Constaté plusieurs fois, dont la fenêtre des cœurs
le 2026-10-31. La correction vit UNE fois dans base.css, APRÈS le reset ; ce test vérifie qu'elle y
est encore et qu'aucune feuille ne la défait pour toutes les modales.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "css")


def strip_comments(txt):
    return re.sub(r"/\*.*?\*/", "", txt, flags=re.S)


def main():
    errors = []
    base = strip_comments(open(os.path.join(CSS, "base.css"), encoding="utf-8").read())
    reset = re.search(r"\*\s*\{[^}]*margin:\s*0", base)
    fix = re.search(r"dialog:modal\s*\{[^}]*margin:\s*auto", base)
    if not reset:
        print("note : plus de reset `* { margin: 0 }` dans base.css — la garde n'a plus d'objet")
    elif not fix:
        errors.append("base.css : `dialog:modal { margin: auto }` absent — les modales se décentrent")
    elif fix.start() < reset.start():
        errors.append("base.css : `dialog:modal { margin: auto }` placé AVANT le reset, qui l'écrase")

    # Une règle qui vise TOUTES les modales et remet leur marge à zéro défait la correction.
    for name in sorted(os.listdir(CSS)):
        if not name.endswith(".css"):
            continue
        txt = strip_comments(open(os.path.join(CSS, name), encoding="utf-8").read())
        for m in re.finditer(r"(^|[},])\s*dialog(:modal)?\s*\{([^}]*)\}", txt):
            if re.search(r"margin:\s*0(\s|;|$)", m.group(3)):
                errors.append("%s : une règle `dialog { margin: 0 }` défait le centrage" % name)

    if errors:
        for e in errors:
            print("ÉCHEC :", e)
        sys.exit(1)
    print("OK : les <dialog> modales restent centrées")


if __name__ == "__main__":
    main()
