#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Libellés COMMUNS des éditeurs (clés `editor.*` : barre « Édition », Espacer, Remplir, aide des
# raccourcis) : ils doivent ARRIVER dans le catalogue JS (`js_catalog`), sinon window.t() rend la
# clé et chaque éditeur retombe sur son texte français — même en anglais. Constaté le 30/10 : le
# préfixe `editor.` manquait à la liste blanche du catalogue.
#
#   $ ./venv/bin/python tests/verif_i18n_editeur_catalogue.py
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
import tempfile  # noqa: E402
# ⚠ AVANT tout accès : connexion SQLite tenue PAR THREAD (cf. CLAUDE.md, « Modèle de threading »).
from app import config, database  # noqa: E402
_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_i18n_editeur_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
from app import i18n  # noqa: E402

echecs = []
for lg in ("fr", "en"):
    src = json.load(open(os.path.join(RACINE, "i18n", "%s.json" % lg), encoding="utf-8"))
    cles = sorted(k for k in src if k.startswith("editor."))
    cat = i18n.js_catalog(lg)
    manque = [k for k in cles if k not in cat]
    ok = cles and not manque
    print("  %-5s %s : %d clés editor.* dans le catalogue JS" % ("OK" if ok else "ÉCHEC", lg, len(cles) - len(manque)))
    if not ok:
        echecs.append(lg)
        print("        → manquent : %s" % ", ".join(manque[:6]))
fr = json.load(open(os.path.join(RACINE, "i18n", "fr.json"), encoding="utf-8"))
en = json.load(open(os.path.join(RACINE, "i18n", "en.json"), encoding="utf-8"))
diff = sorted(set(k for k in fr if k.startswith("editor.")) ^ set(k for k in en if k.startswith("editor.")))
print("  %-5s mêmes clés editor.* en FR et en EN" % ("OK" if not diff else "ÉCHEC"))
if diff:
    echecs.append("fr/en")
    print("        → %s" % ", ".join(diff[:6]))
print("\n%s" % ("ÉCHEC" if echecs else "TOUT VERT"))
sys.exit(1 if echecs else 0)
