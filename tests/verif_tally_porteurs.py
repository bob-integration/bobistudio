#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du REGISTRE DE PORTEURS (`app/tally.py`).
#
# CE QU'IL PROTÈGE. Le distributeur allait chercher ses porteurs dans
# `db_get_tsl_connections()` : le modèle de tally lisait la table d'un PROTOCOLE. Il aurait
# fallu lui apprendre `is07_connections`, puis celle du protocole suivant — et surtout,
# supprimer le service TSL aurait emporté le distributeur, donc les murs multiview, alors que
# la moitié des sources de tally ne vient pas de TSL.
#
# Depuis l'inversion, chaque protocole se DÉCLARE. Ce banc vérifie que la déclaration tient
# ses promesses — et il existe parce que les seize autres bancs de tally passaient déjà tous
# AVANT que le registre ne fonctionne : aucun n'exerce ce chemin.
#
#   $ ./venv/bin/python tests/verif_tally_porteurs.py
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
from app import tally                                                    # noqa: E402

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print(f"  {'OK  ' if condition else 'ÉCHEC'} {intitule}")
    if not condition and explication:
        print(f"        → {explication}")


def _vider():
    for c in list(tally.liste_porteurs()):
        tally.retirer_porteur(c)


NA, NB, NC = "niv-a", "niv-b", "niv-c"

# ─── 1. Déclarer, retrouver, retirer ─────────────────────────────────────────
_vider()
tally.enregistrer_porteur("proto:1", [NA], lambda shm, n=None: 7 if shm == "cam1" else None,
                          nom="Protocole 1")
niveau, pt = tally.porteur_pour([NA])
controle("un porteur déclaré est retrouvé par son niveau",
         niveau == NA and pt and pt.get("cle") == "proto:1", f"{niveau} / {pt}")
controle("l'index se résout CHEZ le porteur",
         tally.index_chez(pt, "cam1") == 7, str(tally.index_chez(pt, "cam1")))
controle("une source inconnue du porteur ne rend pas d'index",
         tally.index_chez(pt, "cam9") is None)
controle("un niveau que personne ne porte rend (None, None)",
         tally.porteur_pour([NC]) == (None, None))

tally.retirer_porteur("proto:1")
controle("retiré, le porteur n'est plus trouvé", tally.porteur_pour([NA]) == (None, None))

# ─── 2. DEUX porteurs peuvent employer le MÊME index ─────────────────────────
# C'est la raison d'être de « chez le porteur » : une table à plat (index → source) mélangerait
# les deux et allumerait un rouge sur le mauvais signal.
_vider()
tally.enregistrer_porteur("proto:A", [NA], lambda shm, n=None: 1 if shm == "cam1" else None)
tally.enregistrer_porteur("proto:B", [NB], lambda shm, n=None: 1 if shm == "cam2" else None)
_, pa = tally.porteur_pour([NA])
_, pb = tally.porteur_pour([NB])
controle("l'index 1 désigne cam1 chez A et cam2 chez B",
         tally.index_chez(pa, "cam1") == 1 and tally.index_chez(pb, "cam2") == 1
         and tally.index_chez(pa, "cam2") is None and tally.index_chez(pb, "cam1") is None,
         "une table à plat confondrait les deux")

# ─── 3. Le PREMIER déclarant d'un niveau le garde ────────────────────────────
_vider()
tally.enregistrer_porteur("proto:1", [NA], lambda shm, n=None: 10)
tally.enregistrer_porteur("proto:2", [NA], lambda shm, n=None: 20)
_, pt = tally.porteur_pour([NA])
controle("deux porteurs sur un même niveau : le premier gagne, sans osciller",
         tally.index_chez(pt, "x") == 10,
         "sinon le tally changerait de porteur d'un tour à l'autre")

# ─── 4. Ordre des niveaux DEMANDÉS ───────────────────────────────────────────
_vider()
tally.enregistrer_porteur("proto:1", [NB], lambda shm, n=None: 5)
niveau, _ = tally.porteur_pour([NC, NB, NA])
controle("le premier niveau demandé QUI A un porteur est retenu", niveau == NB, str(niveau))

# ─── 5. Un porteur qui LÈVE ne fait pas tomber les autres ────────────────────
_vider()
def _casse(shm, n=None):
    raise RuntimeError("porteur défaillant")
tally.enregistrer_porteur("proto:casse", [NA], _casse)
_, pt = tally.porteur_pour([NA])
# ⚠ On appelle SOUS try : si la garde d'`index_chez` disparaît, l'exception remonte ici et
# ferait MOURIR ce banc — il dirait « traceback » au lieu de nommer la propriété perdue.
# Un banc qui plante ne diagnostique rien. Vérifié en retirant la garde : c'est bien ce
# contrôle-ci qui doit rougir, et lui seul.
try:
    _res = tally.index_chez(pt, "cam1")
    _leve = False
except Exception:
    _res, _leve = "propagée", True
controle("un porteur dont la résolution lève rend None, sans propager",
         _res is None and not _leve,
         "le distributeur sert TOUS les murs : une exception les arrêterait tous")

# ─── 6. La référence de libellé est facultative ──────────────────────────────
_vider()
tally.enregistrer_porteur("proto:1", [NA], lambda shm, n=None: 1)
_, pt = tally.porteur_pour([NA])
controle("sans ref_de, ref_chez rend None sans lever", tally.ref_chez(pt, "cam1") is None)
tally.enregistrer_porteur("proto:2", [NB], lambda shm, n=None: 1,
                          ref_de=lambda shm: "port:42" if shm == "cam1" else None)
_, pt2 = tally.porteur_pour([NB])
controle("avec ref_de, la référence d'origine du libellé est rendue",
         tally.ref_chez(pt2, "cam1") == "port:42", str(tally.ref_chez(pt2, "cam1")))

# ─── 7. Retirer un porteur n'éteint PAS le tally d'un autre écrivain ─────────
_vider()
# ⚠ La clé est une RÉFÉRENCE DE SOURCE, plus un index. Un porteur ne sert donc plus à LIRE
# l'état — seulement à traduire vers un protocole sortant : le retirer ne peut, par construction,
# rien éteindre. Le contrôle garde tout son sens, et en gagne : il vérifie que la lecture ne
# passe plus par lui du tout.
tally.poser_tally("srcX", {("cam1", NA): "red"})
tally.enregistrer_porteur("proto:1", [NA], lambda shm, n=None: 3)
tally.retirer_porteur("proto:1")
controle("un porteur retiré ne touche pas aux contributions",
         tally.get_tally_level("cam1", NA) == "red",
         "retirer le porteur d'un niveau ne veut pas dire éteindre ce que d'autres affirment")
tally.poser_tally("srcX", {})
_vider()

print()
if echecs:
    print(f"{len(echecs)} échec(s) : {echecs}")
    sys.exit(1)
print(f"Registre de porteurs : {len(reussites)} contrôles OK.")
