#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Macros : une étape / un nœud DÉSACTIVÉ est sauté par les DEUX moteurs (blocks/v1 et nodes/v2),
# une structure ne se désactive pas, et le commentaire survit à l'aller-retour Blocs ⇄ Scénario.
#
# ★ LE CAS (2026-10-29) : l'éditeur permet de désactiver un bloc « sans le supprimer, pour
# tester ». Un drapeau que l'éditeur grise mais que le moteur ignore exécuterait quand même
# l'étape — le pire des échecs silencieux : l'écran dit « ne fait rien », la régie le fait.
#
#   $ ./venv/bin/python tests/verif_macros_desactive_commentaire.py
import os
import sys
import tempfile
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

# ⚠ AVANT tout accès : une connexion SQLite est tenue PAR THREAD et réutilisée — surcharger
# DB_PATH après un premier accès n'isole plus rien (cf. CLAUDE.md, « Modèle de threading »).
from app import config, database                                     # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_macros_off_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database.init_db()

from app import macros                                               # noqa: E402

echecs = []


def controle(intitule, condition, explication=""):
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition:
        echecs.append(intitule)
        if explication:
            print("        → %s" % explication)


def journal(run):
    return [j["msg"] for j in run.journal]


# ═══ 1. BLOCS ═══════════════════════════════════════
run = macros._Run(0, None)
t0 = time.monotonic()
macros._run_steps([{"type": "sleep", "ms": 3000, "disabled": True},
                   {"type": "set_var", "name": "a", "value": "1"}], None, run, 0)
dt = time.monotonic() - t0
controle("★★★ blocs : une pause désactivée n'attend pas (%.2f s)" % dt, dt < 1.0)
controle("★★ blocs : l'étape suivante s'exécute", any(m.startswith("var a") for m in journal(run)), journal(run))
controle("★ blocs : le journal dit que l'étape a été ignorée",
         any("désactivé" in m for m in journal(run)), journal(run))

run = macros._Run(0, None)
macros._run_steps([{"type": "parallel", "disabled": True,
                    "branches": [[{"type": "set_var", "name": "b", "value": "2"}], []]}], None, run, 0)
controle("★★ blocs : une STRUCTURE (parallèle) ne se désactive pas — elle s'exécute",
         any(m.startswith("var b") for m in journal(run)), journal(run))

# ═══ 2. SCÉNARIO ════════════════════════════════════
g = {"format": "nodes/v2",
     "nodes": [{"id": "e", "type": "entry", "params": {"mode": "manual"}},
               {"id": "s", "type": "sleep", "params": {"ms": 3000, "disabled": True}},
               {"id": "v", "type": "set_var", "params": {"name": "c", "value": "3"}}],
     "edges": [{"from": "e", "port": 0, "to": "s"}, {"from": "s", "port": 0, "to": "v"}]}
run = macros._Run(0, None)
t0 = time.monotonic()
macros._run_graph(g, None, run, 0)
dt = time.monotonic() - t0
controle("★★★ scénario : un nœud désactivé n'attend pas (%.2f s)" % dt, dt < 1.0)
controle("★★ scénario : le jeton continue au nœud suivant", any(m.startswith("var c") for m in journal(run)),
         journal(run))
controle("★ scénario : le journal dit que le nœud a été ignoré",
         any("désactivé" in m for m in journal(run)), journal(run))

g2 = {"format": "nodes/v2",
      "nodes": [{"id": "e", "type": "entry", "params": {"mode": "manual", "disabled": True}},
                {"id": "v", "type": "set_var", "params": {"name": "d", "value": "4"}}],
      "edges": [{"from": "e", "port": 0, "to": "v"}]}
run = macros._Run(0, None)
macros._run_graph(g2, None, run, 0)
controle("★ scénario : une entrée ne se désactive pas", any(m.startswith("var d") for m in journal(run)))

# ═══ 3. ALLER-RETOUR BLOCS ⇄ SCÉNARIO ═══════════════
blocs = [
    {"type": "sleep", "ms": 200, "disabled": True, "comment": "à réactiver au direct"},
    {"type": "if", "comment": "seulement en répétition",
     "cond": {"left": {"kind": "var", "name": "x"}, "op": "==", "right": {"kind": "const", "value": "1"}},
     "then": [{"type": "set_var", "name": "y", "value": "2", "comment": "branche alors"}], "else": []},
    {"type": "parallel", "comment": "les deux players ensemble",
     "branches": [[{"type": "sleep", "ms": 1}], [{"type": "sleep", "ms": 2}]]},
]
graphe = macros.blocks_to_graph(blocs)
retour = macros.graph_to_blocks(graphe)
controle("★★★ aller-retour sans perte (commentaires des structures et drapeau désactivé compris)",
         retour == blocs, "obtenu %s" % retour)
cond = [n for n in graphe["nodes"] if n["type"] == "cond"]
controle("★ le commentaire du si est porté par le nœud cond",
         cond and cond[0]["params"].get("comment") == "seulement en répétition")

if echecs:
    print("\n%d échec(s)" % len(echecs))
    sys.exit(1)
print("\nTout est vert.")
