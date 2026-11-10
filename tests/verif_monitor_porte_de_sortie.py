#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Le diagnostic « pas de passerelle WebRTC » doit nommer une porte QUI EXISTE.

★ POURQUOI (2026-11-08). Le message était unique : « la passerelle WebRTC n'est pas
déployée/activée (Réglages → WebRTC) ». Cet onglet est servi par le manifeste de
`services/webrtc_gateway` — service NON INSTALLÉ, pas d'onglet. Or c'est l'état par DÉFAUT
d'une installation issue du dépôt public, qui n'embarque que deux sous-modules : le produit
renvoyait donc l'exploitant vers une page absente, sans jamais nommer la seule porte réelle
(Réglages → Catalogue, ou l'import d'un `.mxlservice` sur un site hors ligne).

C'est la forme la plus coûteuse d'un message d'erreur : il a l'air de dire quoi faire.

Ce que cette garde vérifie : que la porte citée DÉPEND de l'état d'installation, et qu'aucune
des deux branches ne cite la porte de l'autre. Elle ne juge pas la formulation.

    $ ./venv/bin/python tests/verif_monitor_porte_de_sortie.py
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import database as _database                                  # noqa: E402
TMP = tempfile.mkdtemp(prefix="verif_mon_porte_")
_database.DB_PATH = os.path.join(TMP, "principale.db")                  # jamais la base de prod
_database.init_db()

from app import monitor as mon                                         # noqa: E402
from app import core_plugins                                           # noqa: E402

ECHECS = []


def ok(cond, msg):
    print(("  ok    " if cond else "  ÉCHEC ") + msg)
    if not cond:
        ECHECS.append(msg)


def avec_service(present):
    """Force la réponse d'`is_service`, le seul fait dont dépend le message."""
    vrai = core_plugins.is_service
    core_plugins.is_service = lambda sid: present if sid == "webrtc_gateway" else vrai(sid)
    try:
        return mon.raison_sans_passerelle()
    finally:
        core_plugins.is_service = vrai


print(__doc__.strip().splitlines()[0])
print()

absent = avec_service(False)
present = avec_service(True)

print("  service ABSENT   : " + absent)
print("  service INSTALLÉ : " + present)
print()

ok(absent != present,
   "le message DIFFÈRE selon que le service est installé")

ok("Catalogue" in absent,
   "service absent → la porte citée est le Catalogue")
ok("Réglages → WebRTC" not in absent,
   "service absent → l'onglet WebRTC n'est PAS cité (il n'existe pas)")
ok(".mxlservice" in absent,
   "service absent → l'import hors ligne est cité (site sans Internet)")

ok("Réglages → WebRTC" in present,
   "service installé → son propre onglet est cité")
ok("Catalogue" not in present,
   "service installé → on n'envoie pas l'exploitant réinstaller ce qu'il a")

# Les deux appelants streamés doivent passer par la fonction, pas réécrire le texte : un
# message recopié à la main échappe à tout ce qui précède.
src = open(os.path.join(RACINE, "app", "monitor.py"), encoding="utf-8").read()
corps = src.split("def raison_sans_passerelle", 1)[1].split("\ndef ", 1)[1]
ok("Réglages → WebRTC" not in corps,
   "aucun appelant ne recopie le texte (un seul endroit le produit)")
ok(src.count("raison_sans_passerelle()") >= 2,
   "les deux chemins streamés (create_iter, aperçu) l'appellent")

print()
if ECHECS:
    print("  ✗ %d contrôle(s) en échec" % len(ECHECS))
    sys.exit(1)
print("  ✓ le diagnostic nomme une porte qui existe, dans les deux états")
