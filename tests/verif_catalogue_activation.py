#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Catalogue : « activer après récupération » est un CHOIX, plus une règle cachée.

★ CE QU'ON A CHANGÉ, ET POURQUOI. Le sort d'un paquet récupéré était déduit : type inconnu →
activé, type déjà présent → simplement rangé. La règle était défendable — la page Plugins sait
combien de conteneurs tournent sur une version, le catalogue non — mais elle était **invisible** :
deux clics identiques donnaient deux résultats différents, et rien ne l'annonçait avant de
cliquer. C'est désormais un interrupteur, coché par défaut (`catalogue_activer`).

Ce qui est vérifié ici, c'est le CÂBLAGE — la partie qui casse en silence :
  1. la liste publie l'état de l'interrupteur (sans ça, la case de la page reste sur son défaut
     d'affichage et ment sur le mode réel) ;
  2. le défaut est « activer » ;
  3. le réglage est suivi dans les deux sens ;
  4. la page porte bien la case, et l'appel d'installation transporte le choix.

⚠ L'installation elle-même n'est PAS jouée : elle exige un paquet publié et un accès réseau.
C'est le rôle du banc, pas de la CI (cf. CLAUDE.md, `tools/bancs/`).
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
    from app import settings as st
    from app.auth import hash_password

    database.db_create_user("essai", hash_password("Tulipe-Vent-9312"), "admin", None, None, None)
    cli = _main.app.test_client()
    with cli.session_transaction() as sess:
        sess["user_id"] = database.db_get_user("essai")["id"]

    # 1 & 2 — la liste publie l'état, et il vaut « activer » par défaut.
    j = cli.get("/api/catalogue").get_json()
    verifier("activer_apres" in (j or {}), "la liste publie « activer_apres »")
    verifier(j.get("activer_apres") is True, "le défaut est : activer après récupération")

    # 3 — le réglage est suivi dans les deux sens (et pas seulement à la baisse).
    st.set("catalogue_activer", "0")
    verifier(cli.get("/api/catalogue").get_json().get("activer_apres") is False,
             "décocher est bien répercuté")
    st.set("catalogue_activer", "1")
    verifier(cli.get("/api/catalogue").get_json().get("activer_apres") is True,
             "recocher est bien répercuté")

    # 4 — la page porte la case, et l'appel d'installation transporte un choix EXPLICITE.
    #
    # ⚠ CE CONTRÔLE A CHANGÉ D'ÉNONCÉ LE 2026-10-31, et c'est une décision, pas une régression.
    # Il exigeait littéralement `activer: catActiverCoche()` : le geste lisait une case située
    # plus haut dans le panneau, donc le même bouton rangeait ou mettait en service selon un
    # état qu'il fallait penser à aller vérifier. Cyril a tranché : activer reste le défaut, mais
    # « télécharger seulement » est OFFERT AU MOMENT DU GESTE (point N7.1). Le choix vient donc
    # du bouton cliqué. Ce qui reste vrai, et qui est ce que la garde doit tenir : l'appel ne
    # consulte JAMAIS le réglage en base — dont l'enregistrement peut avoir échoué — mais
    # l'intention exprimée à l'écran, explicite d'abord, la case en repli.
    page = cli.get("/settings").get_data(as_text=True)
    verifier('id="s_catalogue_activer"' in page, "la page Réglages porte l'interrupteur")
    verifier("catSauverActiver" in page, "l'interrupteur est câblé à l'enregistrement")
    verifier(re.search(r"activer:\s*\(activer === undefined\)\s*\?\s*catActiverCoche\(\)",
                       page) is not None,
             "l'appel d'installation transporte le choix EXPLICITE, la case en repli")
    verifier(page.count("catInstaller(this, ' + depot + ', true)") == 1
             and page.count("catInstaller(this, ' + depot + ', false)") == 1,
             "et les deux issues sont deux boutons, lisibles avant le clic")
    verifier("done_active_drift" in page,
             "le message de dérive existe (activer ne redéploie rien)")

    print()
    if ECHECS:
        print(f"✗ {len(ECHECS)} échec(s)")
        return 1
    print("✓ tout est vert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
