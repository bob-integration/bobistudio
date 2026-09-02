#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc des ADRESSES DE LIBELLÉS et de leurs redirections.
#
# CE QU'IL PROTÈGE. Les routes de libellés portaient le préfixe `/api/tsl/…` alors qu'elles
# n'ont rien de protocolaire : elles servent les libellés d'une source et la configuration de
# leurs colonnes, que le tally vienne de TSL, d'IS-07 ou d'un mélangeur. Renommées sous
# `/api/labels/*` le 2026-09-01, avec redirection depuis les anciennes.
#
# Une compatibilité qu'on n'éprouve pas n'est qu'une intention. Et le piège précis est le CODE
# de redirection : un 301 autorise le client à retomber en GET, et un POST de libellés y perdrait
# son corps — donc l'écriture — sans la moindre erreur visible. C'est 308, et ce banc le vérifie.
#
# ⚠ CE BANC CITE VOLONTAIREMENT LES ANCIENNES ADRESSES. Un remplacement global de
# « /api/tsl/label_names » → « /api/labels/names » à travers le dépôt le VIDE de son sens : il
# vérifierait alors que la nouvelle adresse redirige vers elle-même. C'est arrivé pendant le
# renommage du 2026-09-01. Les chaînes ci-dessous ne doivent pas être « harmonisées ».
#
#   $ ./venv/bin/python tests/verif_labels_routes.py
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
os.chdir(RACINE)
import main                                                              # noqa: E402

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print(f"  {'OK  ' if condition else 'ÉCHEC'} {intitule}")
    if not condition and explication:
        print(f"        → {explication}")


CARTE = {str(r): r for r in main.app.url_map.iter_rules()}

# ─── 1. Les nouvelles adresses existent ──────────────────────────────────────
NOUVELLES = ["/api/labels", "/api/labels/batch", "/api/labels/by_shm", "/api/labels/names",
             "/api/labels/cols", "/api/labels/orphelins", "/api/labels/suffix_map",
             "/api/labels/<path:shm>", "/api/tally/state"]
for u in NOUVELLES:
    controle("route servie : %s" % u, u in CARTE)

# ─── 2. Plus AUCUNE adresse de libellé sous le préfixe du protocole ──────────
restes = [u for u in CARTE if u.startswith("/api/tsl/")
          and any(k in u for k in ("label", "sources", "state"))]
# celles-là ne doivent subsister QUE comme redirections (vérifié au point 3)
controle("les anciennes adresses subsistent, mais seulement pour rediriger",
         set(restes) == {"/api/tsl/label_names", "/api/tsl/label_cols",
                         "/api/tsl/sources/by_shm", "/api/tsl/state"},
         "obtenu %s" % sorted(restes))

# ─── 3. Chaque ancienne adresse redirige en 308 vers la bonne ────────────────
ANCIENNES = {
    "/api/tsl/label_names":     "/api/labels/names",
    "/api/tsl/label_cols":      "/api/labels/cols",
    "/api/tsl/sources/by_shm":  "/api/labels/by_shm",
    "/api/tsl/state":           "/api/tally/state",
    "/api/source_labels":       "/api/labels",
    "/api/source_labels/batch": "/api/labels/batch",
    "/api/source_labels/orphelins":  "/api/labels/orphelins",
    "/api/source_labels/suffix_map": "/api/labels/suffix_map",
}
cli = main.app.test_client()
for vieux, neuf in ANCIENNES.items():
    r = cli.get(vieux, follow_redirects=False)
    cible = (r.headers.get("Location") or "").split("?")[0]
    controle("GET %s → 308 %s" % (vieux, neuf),
             r.status_code == 308 and cible.endswith(neuf),
             "obtenu %s vers %r" % (r.status_code, cible))

# ─── 4. ★ LE POINT QUI COMPTE : un POST conserve sa méthode ──────────────────
# Un 301/302 ferait retomber le client en GET. L'écriture serait perdue en silence.
for vieux in ("/api/source_labels/batch", "/api/tsl/label_names"):
    r = cli.post(vieux, json={}, follow_redirects=False)
    controle("POST %s redirige en 308 (méthode conservée)" % vieux,
             r.status_code == 308,
             "obtenu %s — un 301 transformerait ce POST en GET et perdrait le corps"
             % r.status_code)

print()
if echecs:
    print(f"{len(echecs)} échec(s) : {echecs}")
    sys.exit(1)
print(f"Adresses de libellés : {len(reussites)} contrôles OK.")
