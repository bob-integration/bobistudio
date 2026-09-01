#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Diagnostic login : où vit la DB que lit l'orchestrateur, quels utilisateurs
existent, et quel type de hash. À lancer DANS la VM :

    cd /opt/bobistudio
    ./venv/bin/python tools/diag_login.py

Option : tester un mot de passe contre un compte existant
    ./venv/bin/python tools/diag_login.py --username admin --password 'monmdp'
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
from app.config import DB_PATH
from app.database import db_list_users, db_get_user
from app.auth import verify_password


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", "-u")
    parser.add_argument("--password", "-p")
    args = parser.parse_args()

    print(f"DB_PATH lu par l'app : {DB_PATH}")
    exists = os.path.exists(DB_PATH)
    size = os.path.getsize(DB_PATH) if exists else 0
    print(f"Fichier existe       : {exists}  | taille {size} octets")
    print(f"Répertoire courant   : {os.getcwd()}")

    # Autres db_bobistudio.db visibles (piège du mauvais cwd)
    here = os.path.join(os.getcwd(), "db_bobistudio.db")
    if os.path.abspath(here) != os.path.abspath(DB_PATH) and os.path.exists(here):
        print(f"⚠  Autre DB dans le cwd : {here} ({os.path.getsize(here)} octets) — IGNORÉE par l'app")

    if not exists:
        print("\n❌ La DB que lit l'app n'existe pas → 'Identifiants invalides' garanti.")
        print("   Crée l'admin avec : ./venv/bin/python tools/create_admin.py -u admin -p 'motdepasse'")
        return

    users = db_list_users()
    print(f"\nUtilisateurs ({len(users)}) :")
    if not users:
        print("  (aucun) → créer un admin avec tools/create_admin.py")
    for u in users:
        full = db_get_user(u["username"])
        h = full["password_hash"] or ""
        method = h.split("$")[0] if "$" in h else (h[:12] or "VIDE")
        scrypt = method.startswith("scrypt")
        flag = "  ⚠ scrypt (illisible sur ce LXC → --reset requis)" if scrypt else ""
        print(f"  {u['username']:18} role={u.get('role','?'):10} hash={method}{flag}")

    if args.username and args.password:
        full = db_get_user(args.username)
        print(f"\nTest mot de passe pour '{args.username}' :")
        if not full:
            print("  ❌ utilisateur introuvable dans cette DB")
        else:
            ok = verify_password(args.password, full["password_hash"])
            print(f"  {'✅ correct' if ok else '❌ rejeté'}")
            if not ok and (full['password_hash'] or '').startswith('scrypt'):
                print("  → hash scrypt : réinitialise avec")
                print(f"     ./venv/bin/python tools/create_admin.py -u {args.username} -p '{args.password}' --reset")


if __name__ == "__main__":
    main()
