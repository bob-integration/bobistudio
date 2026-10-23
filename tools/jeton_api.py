#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Jetons d'API — créer, lister, révoquer, depuis le contrôleur.

    ./venv/bin/python tools/jeton_api.py lister
    ./venv/bin/python tools/jeton_api.py creer <compte> [libellé]
    ./venv/bin/python tools/jeton_api.py revoquer <id>

Un jeton donne un accès programmatique à l'API sous l'identité d'un compte, AVEC L'HABILITATION
DE CE COMPTE. Pour un accès en lecture seule, créez d'abord un compte en lecture seule : le
jeton ne porte pas de droits propres, c'est délibéré (une seconde échelle de droits recréerait
l'homonymie qui nous a déjà coûté cher).

Le secret n'est affiché QU'À LA CRÉATION, et n'est stocké nulle part — seule son empreinte
l'est. Perdu, il ne se récupère pas : on en crée un autre et on révoque celui-ci.

Agit DIRECTEMENT sur la base du contrôleur, donc à lancer sur le contrôleur. Les mêmes
opérations existent en HTTP (`/api/api-tokens`, permission `settings.edit`) pour un appel
distant ou depuis l'interface.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import (db_api_token_creer, db_api_token_revoquer,      # noqa: E402
                          db_api_tokens_liste, db_get_user, db_list_users)


def lister():
    lignes = db_api_tokens_liste()
    if not lignes:
        print("Aucun jeton. `creer <compte>` pour en ouvrir un.")
        return 0
    print("%-4s %-16s %-24s %-11s %-19s %s"
          % ("id", "compte", "libellé", "préfixe", "dernier usage", "état"))
    for t in lignes:
        print("%-4s %-16s %-24s %-11s %-19s %s" % (
            t["id"], (t["username"] or "?")[:16], (t["label"] or "—")[:24],
            (t["prefix"] or "") + "…", t["last_used_at"] or "jamais",
            ("RÉVOQUÉ le " + t["revoked_at"]) if t["revoked_at"] else "actif"))
    return 0


def creer(nom, label=None):
    u = db_get_user(nom)
    if not u:
        # On NOMME les comptes existants : l'erreur la plus fréquente est une faute de frappe,
        # et répondre « compte inconnu » sans dire lesquels existent fait perdre un aller-retour.
        noms = ", ".join(x["username"] for x in db_list_users()) or "(aucun)"
        print("Compte inconnu : %r.\nComptes existants : %s" % (nom, noms), file=sys.stderr)
        return 2
    tid, jeton = db_api_token_creer(u["id"], label=label)
    print("Jeton %s créé pour %s (habilitation « %s »).\n" % (tid, nom, u.get("role")))
    print("    %s\n" % jeton)
    print("⚠ Il ne sera PLUS JAMAIS affiché : copiez-le maintenant. Il n'est pas stocké en")
    print("  clair, donc personne — administrateur compris — ne peut le relire.")
    print("⚠ Il vaut les droits de ce compte et n'expire pas, et il se renifle comme un mot de")
    print("  passe : le port web de l'orchestrateur est en HTTP clair. Réseau maîtrisé seulement.")
    print("\nRévocation : tools/jeton_api.py revoquer %s" % tid)
    return 0


def revoquer(tid):
    try:
        tid = int(tid)
    except ValueError:
        print("id attendu, reçu %r" % tid, file=sys.stderr)
        return 2
    connus = {t["id"]: t for t in db_api_tokens_liste()}
    if tid not in connus:
        print("Jeton %s inconnu. `lister` pour les voir." % tid, file=sys.stderr)
        return 2
    if db_api_token_revoquer(tid):
        t = connus[tid]
        print("Jeton %s (%s / %s) révoqué. Tout accès le présentant est refusé dès maintenant."
              % (tid, t["username"] or "?", t["label"] or "sans libellé"))
    else:
        print("Jeton %s était DÉJÀ révoqué le %s — rien à faire."
              % (tid, connus[tid]["revoked_at"]))
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "aide"):
        print(__doc__)
        return 0
    cmd, args = argv[1], argv[2:]
    if cmd == "lister":
        return lister()
    if cmd == "creer":
        if not args:
            print("usage : creer <compte> [libellé]", file=sys.stderr)
            return 2
        return creer(args[0], " ".join(args[1:]) or None)
    if cmd == "revoquer":
        if not args:
            print("usage : revoquer <id>", file=sys.stderr)
            return 2
        return revoquer(args[0])
    print("Commande inconnue : %r (lister | creer | revoquer)" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
