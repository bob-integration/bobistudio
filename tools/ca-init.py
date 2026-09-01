#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France

"""Initialise la CA interne du plan de contrôle (mTLS).

Génère la CA racine + le cert du contrôleur dans config.TLS_DIR (droits 600 pour les clés).
Idempotent : refuse de réécrire une CA existante sans --force (réémettre la CA invaliderait
TOUS les certs de nœuds déjà émis).

    ./venv/bin/python tools/ca-init.py --controller-ip x.x.x.x [--controller-ip <VIP HA>]
    ./venv/bin/python tools/ca-init.py --controller-host bobi-ctrl.lan

HA : après génération, répliquer TLS_DIR (au moins ca.key + ca.crt) sur le contrôleur
standby, hors snapshot SQLite, pour qu'il puisse re-signer après un failover.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ca, config  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Initialise la CA interne mTLS de Bobi.Studio")
    ap.add_argument("--controller-ip", action="append", default=[], metavar="IP",
                    help="IP par laquelle les nœuds joignent le contrôleur (répétable ; inclure la VIP HA)")
    ap.add_argument("--controller-host", action="append", default=[], metavar="HOST",
                    help="Nom d'hôte du contrôleur (répétable)")
    ap.add_argument("--force", action="store_true",
                    help="Réémet la CA MÊME si elle existe (invalide tous les certs déjà émis)")
    args = ap.parse_args()

    sans = list(args.controller_ip) + list(args.controller_host)
    if ca.ca_available() and not args.force:
        print(f"CA déjà présente dans {config.TLS_DIR} — rien à faire (--force pour réémettre).")
        for k, v in ca.paths().items():
            print(f"  {k:10s} {v}")
        return 0
    if not sans:
        print("⚠ Aucun --controller-ip/--controller-host : le cert contrôleur n'aura pas de SAN, "
              "les nœuds ne pourront pas vérifier le contrôleur. Ajoutez au moins l'IP de contrôle.",
              file=sys.stderr)

    written = ca.create_ca_material(controller_sans=sans, overwrite=args.force)
    print(f"CA initialisée dans {config.TLS_DIR} :")
    for f in written:
        print(f"  {f}  ({oct(os.stat(f).st_mode)[-3:]})")
    print("\nProchaine étape : répliquer ce dossier sur le contrôleur standby (HA), "
          "puis déployer ca.crt sur les nœuds à l'enrôlement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
