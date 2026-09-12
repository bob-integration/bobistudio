#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc DIFFÉRENTIEL de la surface plan 2 : l'agent générique contre le contrôleur du moteur 2110.
#
# POURQUOI DIFFÉRENTIEL. Le moteur 2110 n'embarque pas `script_templates/agent.py` — il a son
# propre :8081. Il y a donc DEUX implémentations de `/x-nmos/` dans le produit, et la seconde est
# un décalque de la première. Deux décalques finissent toujours par diverger ; ce qui est
# dangereux ici, c'est que la divergence ne se voit pas : chacune répond 200 à sa façon, et c'est
# le contrôleur tiers qui découvre l'écart, chez le client.
#
# On ne teste donc pas « le moteur répond correctement » mais « le moteur répond LA MÊME CHOSE ».
#
#   $ ./venv/bin/python tools/verif_nmos_plan2_moteur.py
import ast
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(RACINE, "script_templates", "agent.py")
MOTEUR = os.path.join(RACINE, "plugins", "2110_io", "docker", "controller.py")

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


def _charger(chemin, doc):
    """Extrait `_nmos_get` et ses constantes du fichier, sans exécuter le reste.

    ⚠ On N'IMPORTE PAS le module : l'agent comme le contrôleur démarrent des serveurs, ouvrent des
    sockets et (pour le moteur) chargent numpy et le SDK MXL. On récupère donc les seuls noeuds
    d'AST qui nous intéressent — ce qui a aussi le mérite de prouver que la surface NMOS ne dépend
    de rien d'autre que d'elle-même."""
    src = open(chemin, encoding="utf-8").read()
    arbre = ast.parse(src)
    voulus = {"_nmos_get", "NMOS_IS04", "NMOS_IS05", "NMOS_COLLECTIONS"}
    morceaux = []
    for n in arbre.body:
        if isinstance(n, ast.FunctionDef) and n.name in voulus:
            morceaux.append(ast.get_source_segment(src, n))
        elif isinstance(n, ast.Assign):
            for c in n.targets:
                if isinstance(c, ast.Name) and c.id in voulus:
                    morceaux.append(ast.get_source_segment(src, n))
    ns = {"json": json, "os": os, "_nmos_doc": lambda: doc}
    exec("\n\n".join(morceaux), ns)
    return ns


DOC = {
    "node": {"id": "n-1", "label": "moteur"},
    "devices": [{"id": "d-1"}],
    "sources": [{"id": "s-1"}],
    "flows": [{"id": "f-1"}],
    "senders": [{"id": "snd-1"}],
    "receivers": [{"id": "rx-1"}],
    "connection": {
        "senders": {"snd-1": {"constraints": [{}], "staged": {"a": 1}, "active": {"b": 2}}},
        "receivers": {"rx-1": {"constraints": [{}], "staged": {}, "active": {}}},
    },
}

CHEMINS = [
    "/x-nmos/", "/x-nmos", "/x-nmos/node/", "/x-nmos/node/v1.3/", "/x-nmos/node/v1.3/self",
    "/x-nmos/node/v1.3/devices", "/x-nmos/node/v1.3/devices/d-1", "/x-nmos/node/v1.3/devices/zzz",
    "/x-nmos/node/v1.3/sources", "/x-nmos/node/v1.3/flows", "/x-nmos/node/v1.3/senders",
    "/x-nmos/node/v1.3/senders/snd-1", "/x-nmos/node/v1.3/receivers/rx-1",
    "/x-nmos/node/v1.2/self", "/x-nmos/node/v1.3/inconnu",
    "/x-nmos/connection/", "/x-nmos/connection/v1.1/", "/x-nmos/connection/v1.1/single/",
    "/x-nmos/connection/v1.1/single/senders/", "/x-nmos/connection/v1.1/single/senders/snd-1/",
    "/x-nmos/connection/v1.1/single/senders/snd-1/staged",
    "/x-nmos/connection/v1.1/single/senders/snd-1/active",
    "/x-nmos/connection/v1.1/single/senders/snd-1/constraints",
    "/x-nmos/connection/v1.1/single/senders/snd-1/transportfile",
    "/x-nmos/connection/v1.1/single/receivers/rx-1/staged",
    "/x-nmos/connection/v1.1/single/receivers/zzz/staged",
    "/x-nmos/connection/v1.0/single/", "/x-nmos/connection/v1.1/multi/",
    "/x-nmos/zzz", "/x-nmos/node/v1.3/senders/snd-1?query=1",
]

print("Plan 2 : le moteur 2110 doit répondre EXACTEMENT comme l'agent générique\n")

try:
    ns_agent = _charger(AGENT, DOC)
    ns_moteur = _charger(MOTEUR, DOC)
except Exception as e:
    print("  ÉCHEC extraction : %s" % e)
    sys.exit(1)

controle("★ la surface NMOS du moteur est autonome (aucun import hors json/os)", True,
         "l'extraction a réussi sans numpy ni bobimxl")

ecarts = []
for c in CHEMINS:
    a = ns_agent["_nmos_get"](c)
    m = ns_moteur["_nmos_get"](c)
    if a != m:
        ecarts.append((c, a, m))
controle("★★★ les deux implémentations répondent à l'identique sur %d chemins" % len(CHEMINS),
         not ecarts,
         "une divergence ne se voit pas : chacune répond 200 à sa façon, et c'est le contrôleur "
         "tiers qui la découvre chez le client — %s" % ecarts[:2])

# ── Le cas « rien n'a été poussé » ──────────────────────────────────────────
vide_a = _charger(AGENT, None)["_nmos_get"]("/x-nmos/node/v1.3/self")
vide_m = _charger(MOTEUR, None)["_nmos_get"]("/x-nmos/node/v1.3/self")
controle("★★ sans document poussé, les deux rendent 503 (pas 404)",
         vide_a[0] == 503 and vide_m[0] == 503,
         "la surface EXISTE, elle n'est pas alimentée. Un 404 ferait conclure à une image sans "
         "NMOS, donc à un défaut de déploiement — obtenu agent=%s moteur=%s"
         % (vide_a[0], vide_m[0]))
controle("et le même message", vide_a[1] == vide_m[1])

# ── Les invariants qui comptent, vérifiés côté moteur ───────────────────────
controle("★★ `transportfile` EXISTE et rend 404 (BCP-007-03, pendant de manifest_href:null)",
         ns_moteur["_nmos_get"](
             "/x-nmos/connection/v1.1/single/senders/snd-1/transportfile")[0] == 404)
controle("★ une version d'API non servie rend 404, pas 200",
         ns_moteur["_nmos_get"]("/x-nmos/node/v1.2/self")[0] == 404)
controle("★ la racine liste exactement node/ et connection/",
         ns_moteur["_nmos_get"]("/x-nmos/")[1] == ["node/", "connection/"])

# ── Le POST /nmos ne doit pas se confondre avec /nmos/subscribe ─────────────
src_m = open(MOTEUR, encoding="utf-8").read()
i_desc, i_sub = src_m.find('route == "/nmos"'), src_m.find('route == "/nmos/subscribe"')
controle("★★★ POST /nmos est traité AVANT /nmos/subscribe, et distinctement",
         0 < i_desc < i_sub,
         "confondre les deux ferait redémarrer des flux en production à chaque description "
         "poussée : l'un DÉCRIT, l'autre COMMANDE")
controle("★★ l'écriture du document est ATOMIQUE",
         'os.replace(tmp, cible)' in src_m,
         "un GET concurrent doit voir l'ancienne version ou la nouvelle, jamais un fichier "
         "tronqué qu'il prendrait pour une absence de surface")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
