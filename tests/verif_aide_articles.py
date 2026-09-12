#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Page Aide : tout article listé doit avoir un gabarit, et réciproquement.

★ POURQUOI. Les articles vivent à DEUX endroits dans `templates/aide.html` : une entrée dans le
tableau `ARTICLES` (ce que le sommaire affiche) et un `<template id="wiki-art-<id>">` (le
contenu). Rien ne relie les deux à l'écriture. Un article listé sans gabarit s'ouvre donc **vide**
— pas d'erreur, pas de trace, juste une page blanche que personne ne signale ; un gabarit sans
entrée est du contenu écrit que personne ne peut atteindre.

C'est un échec silencieux de plus, et il coûte cher pour de la documentation : elle n'est lue que
par quelqu'un qui cherche déjà, et qui conclura que le sujet n'est pas traité.

On vérifie aussi que la CATÉGORIE de chaque article existe : un article rangé dans une rubrique
inconnue n'apparaît sous aucune, donc n'est atteignable que par la recherche.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, database                                   # noqa: E402

ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def main():
    chemin = os.path.join(tempfile.mkdtemp(), "t.db")
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    database._tls.__dict__.pop("conn", None)
    database.init_db()

    import main as _main
    from app.auth import hash_password

    uid = database.db_create_user("essai", hash_password("Tulipe-Vent-9312"), "admin",
                                  None, None, None)
    cli = _main.app.test_client()
    with cli.session_transaction() as sess:
        sess["user_id"] = uid
    page = cli.get("/aide").get_data(as_text=True)

    ids = re.findall(r"\{ id: '([^']+)',\s*title:", page)
    gabarits = set(re.findall(r'<template id="wiki-art-([^"]+)"', page))
    categories = set(re.findall(r"\{ id: '([^']+)',\s*label:", page))
    par_cat = dict(re.findall(r"\{ id: '([^']+)',\s*title:[^,]*,\s*cat: '([^']+)' \}", page))

    verifier(len(ids) > 30, f"le sommaire est bien rendu ({len(ids)} articles)")

    sans_gabarit = [i for i in ids if i not in gabarits]
    verifier(not sans_gabarit,
             "aucun article listé sans gabarit (il s'ouvrirait VIDE) — " + (
                 ", ".join(sans_gabarit) if sans_gabarit else "ok"))

    orphelins = sorted(gabarits - set(ids))
    verifier(not orphelins,
             "aucun gabarit inatteignable (absent du sommaire) — " + (
                 ", ".join(orphelins) if orphelins else "ok"))

    inconnues = sorted({c for c in par_cat.values() if c not in categories})
    verifier(not inconnues,
             "toutes les catégories d'article existent — " + (
                 ", ".join(inconnues) if inconnues else "ok"))

    # L'onglet Réglages → Mises à jour a son article : c'est la page par laquelle on installe
    # un plugin et on met le produit à jour, elle n'en avait aucun jusqu'au 2026-09-03.
    verifier("mises-a-jour-page" in ids and "mises-a-jour-page" in gabarits,
             "la page « Mises à jour » a bien un article d'aide")

    print()
    if ECHECS:
        print(f"✗ {len(ECHECS)} échec(s)")
        return 1
    print("✓ tout est vert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
