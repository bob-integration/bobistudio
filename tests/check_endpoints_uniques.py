#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Le blueprint COMPLET (cœur + routes des services) s'enregistre sur une application Flask.

★ CE QUE ÇA PROTÈGE. Les services ajoutent leurs routes au blueprint du cœur
(`core_plugins.register_all_routes`). Deux fonctions de vue de même NOM — une dans le cœur, une
dans un service — ne se voient qu'au `app.register_blueprint(bp)` de main.py :
`AssertionError: View function mapping is overwriting an existing endpoint function`. L'import
passe, pyflakes ne dit rien, le smoke test non plus — et l'orchestrateur boucle au démarrage.
Constaté le 2026-10-11 : `media_bin_delete`, ajoutée au cœur (base des médias) alors que le
Gestionnaire de Médias l'avait déjà ; 6 minutes d'orchestrateur indisponible.

★ CE QUI EST VÉRIFIÉ (bloquant) : l'enregistrement réussit.
★ VÉRIFIÉ AUSSI (bloquant depuis 2026-10-11) : aucune paire de règles sur le même chemin et la
même méthode — la seconde ne serait jamais servie. Les six doublons d'origine (compatibilité
/api/tsl/label_* déclarée par le service TSL ET par les labels) ont été réduits à zéro.

   $ ./venv/bin/python tests/check_endpoints_uniques.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import database                                                # noqa: E402
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="endpoints_"), "t.db")
database.init_db()

from flask import Flask                                                 # noqa: E402
from app.routes import bp                                               # noqa: E402

app = Flask("check_endpoints")
try:
    app.register_blueprint(bp)
except AssertionError as e:
    print("✗ enregistrement du blueprint :", e)
    sys.exit(1)

seen, dup = {}, []
for r in app.url_map.iter_rules():
    for m in (r.methods or set()) - {"HEAD", "OPTIONS"}:
        k = (r.rule, m)
        if k in seen and seen[k] != r.endpoint:
            dup.append("%s %s : %s et %s" % (m, r.rule, seen[k], r.endpoint))
        seen.setdefault(k, r.endpoint)
for d in dup:
    print("  ⚠ même chemin, même méthode (la seconde n'est jamais servie) :", d)
if dup:
    print("✗ %d doublon(s) : une route déclarée deux fois est une route morte." % len(dup))
    sys.exit(1)
print("✓ blueprint enregistré : %d règles, aucun doublon." % len(list(app.url_map.iter_rules())))
