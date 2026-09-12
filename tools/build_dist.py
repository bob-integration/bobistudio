#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Wrapper CLI autour de app.builder.build — fabrique dist/bobistudio.zip.

Exemples :
    python3 tools/build_dist.py                       # sélection par défaut
    python3 tools/build_dist.py --all                 # tous les plugins + services
    python3 tools/build_dist.py --plugins receiver_2110,streamer --services nmos
"""
import argparse
import os
import sys

# Permettre l'import de `app` quel que soit le cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import builder


def main():
    ap = argparse.ArgumentParser(description="Build du paquet de distribution Bobi.Studio")
    ap.add_argument("--all", action="store_true", help="inclure tous les plugins et services installés")
    ap.add_argument("--plugins", help="liste d'ids plugins séparés par des virgules")
    ap.add_argument("--services", help="liste d'ids services séparés par des virgules")
    ap.add_argument("--offline", action="store_true",
                    help="bundle hors-ligne : embarque roues pip + .deb système (~300 Mo, "
                         "déploiement sans réseau ; nécessite root pour apt)")
    ap.add_argument("--images", action="store_true",
                    help="embarque aussi les images Docker runtime (docker save ; +plusieurs Go, "
                         "nœud opérationnel sans registre)")
    args = ap.parse_args()

    if args.all:
        avail = builder.available()
        plugins = [p["type"] for p in avail["plugins"]]
        services = [s["id"] for s in avail["services"]]
    else:
        plugins = args.plugins.split(",") if args.plugins else None
        services = args.services.split(",") if args.services else None

    verbose = args.offline or args.images
    try:
        res = builder.build(plugins=plugins, services=services,
                            offline=args.offline, images=args.images,
                            log=(print if verbose else None))
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    mb = res["size"] / (1024 * 1024)
    print(f"✅ {res['file']} — {res['count']} fichiers, {mb:.1f} Mo")
    print(f"   plugins : {', '.join(res['plugins']) or '(aucun)'}")
    print(f"   services: {', '.join(res['services']) or '(aucun)'}")
    if res.get("offline"):
        print(f"   hors-ligne : {res.get('offline_wheels')} roues + {res.get('offline_debs')} .deb embarqués")
    if res.get("images"):
        print(f"   images Docker : {res.get('offline_images')} embarquée(s)")


if __name__ == "__main__":
    main()
