#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Bootstrap CLI : crée le premier utilisateur admin de l'orchestrateur.

Usage interactif :
    ./venv/bin/python tools/create_admin.py

Usage non-interactif :
    ./venv/bin/python tools/create_admin.py --username alice --password 'changeme'

Réinitialiser le mot de passe d'un compte existant (ex. migration depuis un
ancien hash scrypt illisible) :
    ./venv/bin/python tools/create_admin.py --username alice --password 'nouveau' --reset
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import getpass

from app.database import (init_db, db_get_user, db_create_user, db_update_user,
                          db_list_users)
from app.auth import hash_password, ROLES, valider_motdepasse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", "-u")
    parser.add_argument("--password", "-p")
    parser.add_argument("--role", "-r", default="admin",
                        choices=list(ROLES.keys()))
    parser.add_argument("--reset", action="store_true",
                        help="réinitialise le mot de passe si l'utilisateur existe déjà")
    parser.add_argument("--force", action="store_true",
                        help="accepte un mot de passe faible (à n'employer que pour un compte "
                             "de dépannage détruit dans la foulée)")
    args = parser.parse_args()

    init_db()

    username = args.username or input("Username: ").strip()
    if not username:
        print("Username vide, abandon.", file=sys.stderr)
        sys.exit(1)

    existing = db_get_user(username)
    if existing and not args.reset:
        print(f"L'utilisateur '{username}' existe déjà "
              f"(utiliser --reset pour réinitialiser son mot de passe).", file=sys.stderr)
        sys.exit(1)

    password = args.password or getpass.getpass("Mot de passe: ")
    if not password:
        print("Mot de passe vide, abandon.", file=sys.stderr)
        sys.exit(1)

    if not args.password:
        confirm = getpass.getpass("Confirme: ")
        if confirm != password:
            print("Confirmation différente, abandon.", file=sys.stderr)
            sys.exit(1)

    # Même règle que l'interface. Cet outil crée des ADMINISTRATEURS depuis un shell : c'est le
    # chemin le plus discret du produit, donc le dernier où tolérer « admin/admin ».
    fautes = valider_motdepasse(password, username)
    if fautes:
        from app.i18n import t as _t
        print("Mot de passe trop faible :", file=sys.stderr)
        for f in fautes:
            print("  - " + _t("compte.pwd_regle_" + f, lang="fr"), file=sys.stderr)
        if not args.force:
            print("(--force pour passer outre)", file=sys.stderr)
            sys.exit(1)
        print("⚠ --force : accepté malgré tout.", file=sys.stderr)

    if existing:
        db_update_user(existing["id"], password_hash=hash_password(password))
        print(f"✓ Mot de passe de '{username}' réinitialisé (id={existing['id']}).")
    else:
        uid = db_create_user(username, hash_password(password), args.role)
        print(f"✓ Utilisateur '{username}' créé (id={uid}, role={args.role}).")
    print(f"  Total utilisateurs : {len(db_list_users())}")


if __name__ == "__main__":
    main()
