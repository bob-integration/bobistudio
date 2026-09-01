#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du PREMIER ÉTAGE de la propagation du tally : un mélangeur ne tallye ses entrées que si sa
# propre sortie PGM est à l'antenne (services/tsl/__init__.py:_sortie_a_l_antenne).
#
# POURQUOI CE BANC EXISTE. Un tally faux est pire que pas de tally : un rouge manquant fait parler
# sur une source qu'on croit froide, un rouge en trop fige un plateau. Les deux erreurs se valent
# ici, donc les deux sens sont éprouvés — on ne se contente pas de vérifier que la garde bloque,
# on vérifie aussi qu'elle laisse passer.
#
# ⚠ Ce banc n'écrit RIEN dans la production : il manipule `_tally_state` dans SON processus, pas
#   dans celui de l'orchestrateur.
#
#   $ ./venv/bin/python tools/verif_tally_propagation.py
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from services import tsl                                            # noqa: E402

print("Tally — propagation, premier étage (un mélangeur suit sa propre sortie)\n")

# Depuis le dénouement, un mélangeur reçoit la LISTE des niveaux de sa production — plus une base
# dont on dériverait +0/+1/+2. Le « 3 » de TSL ne structure plus le modèle.
BASE = 18                       # conservé pour fabriquer des clés d'état lisibles
NIVEAUX = [18, 19, 20]          # les niveaux de « notre » production
AUTRE = [21, 22, 23]            # ceux d'une AUTRE production
IDX_PGM = 7                     # index TSL de la sortie PGM du mélangeur
CT = {"vmid": 1085, "hostname": "mixer",
      "deploy_config": json.dumps({"type": "mixer", "params": {}})}


def idx_for(shm):
    """Résolution shm → index, telle que la fournit l'appelant réel."""
    return IDX_PGM if shm == "mixer_pgm" else None


def _etat(d=None):
    """Pose l'état de tally de CE processus. Les clés sont des tuples (index, niveau) : on ne
    peut donc pas passer par des mots-clés."""
    with tsl._lock:
        tsl._tally_state.clear()
        tsl._tally_state.update(d or {})


# ── La sortie est-elle correctement identifiée ? ─────────────────────────────
from app import plugins as _plg                                     # noqa: E402
_w = _plg.derive_wiring("mixer", "mixer") or {}
_prod = [(p.get("label"), p.get("shm")) for p in (_w.get("produces") or [])]
controle("★ le mélangeur déclare bien un PGM, un CLEAN et un PVW",
         {l for l, _ in _prod} >= {"PGM", "CLEAN", "PVW"}, "obtenu %s" % _prod)
controle("★★ c'est le PGM qui est retenu, pas la première sortie venue",
         next((s for l, s in _prod if l == "PGM"), None) == "mixer_pgm",
         "CLEAN et PVW ne disent rien de la diffusion — les confondre allumerait un rouge sur "
         "un mélangeur dont seul le PVW est regardé")

# ── Les deux sens de la garde ────────────────────────────────────────────────
_etat()
controle("★★★ sortie NON tallyée → le mélangeur n'émet pas",
         not tsl._sortie_a_l_antenne(CT, NIVEAUX, idx_for),
         "c'est le défaut corrigé : un mélangeur de préparation allumait un rouge sur une caméra "
         "diffusée nulle part")

_etat({(IDX_PGM, BASE + 0): "red"})
controle("★★★ sortie tallyée sur le PREMIER niveau → il émet",
         tsl._sortie_a_l_antenne(CT, NIVEAUX, idx_for),
         "une garde qui ne laisse jamais passer éteint le tally partout — c'est l'erreur "
         "symétrique, et elle est aussi grave")

_etat({(IDX_PGM, BASE + 2): "red"})
controle("★ ...sur n'importe lequel de SES trois niveaux aussi",
         tsl._sortie_a_l_antenne(CT, NIVEAUX, idx_for))

# ── Le multi-production, qui est la vraie subtilité ──────────────────────────
_etat({(IDX_PGM, AUTRE[0]): "red"})       # production SUIVANTE, même index de source
controle("★★★ un rouge sur une AUTRE production ne déclenche pas",
         not tsl._sortie_a_l_antenne(CT, NIVEAUX, idx_for),
         "le système fait tourner plusieurs productions : regarder tous les niveaux ferait "
         "s'allumer un mélangeur de la production 2 parce qu'un homonyme est à l'antenne sur la 5")

_etat({(IDX_PGM + 1, BASE + 0): "red"})   # un AUTRE signal, même production
controle("★★ un rouge sur un autre signal ne déclenche pas",
         not tsl._sortie_a_l_antenne(CT, NIVEAUX, idx_for))

_etat({(IDX_PGM, BASE + 0): "green"})
controle("★ un VERT sur notre sortie ne vaut pas antenne",
         not tsl._sortie_a_l_antenne(CT, NIVEAUX, idx_for),
         "le vert désigne une autre chaîne de destination, pas la diffusion")

# ── Ce qu'on fait quand on ne SAIT pas ───────────────────────────────────────
_etat({(IDX_PGM, BASE + 0): "red"})
controle("★★ sortie non résoluble en index → on n'émet pas",
         not tsl._sortie_a_l_antenne(CT, NIVEAUX, lambda shm: None),
         "ne pas savoir n'est pas une raison d'allumer un rouge")
controle("★★ conteneur illisible → on n'émet pas, et ça ne lève pas",
         not tsl._sortie_a_l_antenne({"vmid": 0, "hostname": "x", "deploy_config": "{pas du json"},
                                     NIVEAUX, idx_for))

# ── Le cumul : rouge + vert sur UN niveau ───────────────────────────────────
# ⚠ Ce contrôle a été REFAIT. Il vérifiait que le PREMIER niveau du mélangeur porte le rouge et le
# SECOND le vert — c'était le pas de 3 de TSL transposé : deux niveaux là où il n'y a qu'une
# chaîne de destination. Le rouge et le vert sont deux ÉTATS d'un même niveau, et leur coexistence
# a un nom, `amber`. C'est ce cumul qu'il faut protéger : sans lui, une source au programme ET en
# préparation perd l'une des deux informations, silencieusement.
controle("★★★ rouge et vert sur le même niveau se cumulent en ambre",
         tsl.cumuler("red", "green") == "amber" and tsl.cumuler("green", "red") == "amber",
         "c'est l'orange que voit l'exploitant ; l'écraser en rouge lui cache que la source est "
         "déjà armée ailleurs")
controle("★★ le cumul est neutre sur `off`",
         tsl.cumuler("off", "red") == "red" and tsl.cumuler("green", None) == "green"
         and tsl.cumuler(None, None) == "off",
         "une contribution éteinte ne doit rien allumer ni rien effacer")
controle("★ deux contributions identiques ne changent rien",
         tsl.cumuler("red", "red") == "red")
controle("★★★ un ambre reçu reste ambre après cumul",
         tsl.cumuler("amber", "red") == "amber" and tsl.cumuler("amber", "off") == "amber",
         "l'émetteur TSL peut envoyer l'ambre directement (code 3) : le dégrader en rouge "
         "perdrait ce qu'il a explicitement dit")

# ── Le contournement, et son défaut ──────────────────────────────────────────
mani = json.load(open(os.path.join(RACINE, "plugins", "mixer", "plugin.json")))
champ = next((c for c in mani.get("config_schema", []) if c.get("key") == "tally_force"), None)
controle("★★★ le contournement `tally_force` existe et vaut VRAI par défaut",
         bool(champ) and champ.get("default") is True,
         "on livre la correction pour tous, mais un site dont la sortie de mélangeur n'est mappée "
         "nulle part perdrait sinon son tally du jour au lendemain, sur une fonction d'antenne")
controle("★ et il s'explique dans son aide", bool((champ or {}).get("help")),
         "un réglage qui change un comportement d'antenne sans dire lequel est un piège")

_etat()
print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
