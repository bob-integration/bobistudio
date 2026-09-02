#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de l'ÉNUMÉRATION des Sources IS-07 : qui décide quels signaux sont publiés.
#
# ★ LE DÉFAUT CORRIGÉ. `_sources()` ne lisait QUE la table de correspondance TSL. Un site qui ne
# fait pas de TSL — tally reçu en IS-07, ou produit par ses propres mélangeurs — ne publiait
# AUCUNE Source IS-07 : sa publication NMOS dépendait d'un protocole absent de son chemin. C'est
# la même faute que la lecture d'état avant le passage à l'adressage par source, un cran plus haut.
#
# La règle du module, elle, est CONSERVÉE et vérifiée ici : un flux n'a que les niveaux que
# quelqu'un déclare lui adresser. Publier (tous les flux × tous les niveaux) inventerait des
# Sources qui ne changent jamais, et un contrôleur ne pourrait plus distinguer un signal qu'on ne
# tallye pas d'un signal éteint.
#
#   $ ./venv/bin/python tests/verif_is07_sources_declarants.py
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


from services.nmos import is07                                       # noqa: E402
from app.numerotation import cle_input                               # noqa: E402
import app.database as db                                            # noqa: E402
import app.tally as tally                                            # noqa: E402

print("IS-07 — qui déclare les Sources publiées\n")

NA, NB = "niveau-a", "niveau-b"

_ETAT = {"tsl_map": [], "tsl_conn": [], "i7_map": [], "i7_conn": [], "cts": []}

db.db_get_tsl_mappings_all  = lambda: list(_ETAT["tsl_map"])
db.db_get_tsl_connections   = lambda: list(_ETAT["tsl_conn"])
db.db_get_is07_mappings_all = lambda: list(_ETAT["i7_map"])
db.db_get_is07_connections  = lambda: list(_ETAT["i7_conn"])
db.db_get_containers        = lambda: list(_ETAT["cts"])
db.db_get_tally_levels      = lambda: [{"uuid": NA, "nom": "Antenne"}, {"uuid": NB, "nom": "Plateau"}]
db.db_get_tally_levels_of   = lambda t, i: []


def _sources(**kw):
    for k in _ETAT:
        _ETAT[k] = list(kw.get(k) or [])
    is07._src_cache["ts"] = 0.0            # l'instantané ne doit pas masquer le changement
    return sorted((s, n) for s, n, _ in is07._sources())


def _mixer(entrees, niveaux=(NA,), emit=True, vmid=1):
    p = {"tally_emit": emit, "tally_level_base": list(niveaux)}
    for k, v in enumerate(entrees):          # `cle_input` prend un indice 0-BASED
        p[cle_input(k)] = v
        # ⚠ DEUX FORMES, ET DEUX GARDES DISTINCTS. Un `_fmt` en dictionnaire est écarté par le
        # test de TYPE ; un `_fmt` en chaîne — les configurations héritées en portent — ne l'est
        # que par le test de SUFFIXE. Mon premier fixture n'avait que la forme dictionnaire :
        # muté, le banc restait vert et ne prouvait qu'un garde sur deux.
        p[cle_input(k, fmt=True)] = {"width": 1920} if k % 2 else "1920x1080p50"
    # ★ CLÉ RÉELLE, relevée dans la configuration de production : elle commence par `input_`
    # et ne finit PAS par `_fmt`. Seul le test de TYPE l'écarte — sans lui, un dictionnaire de
    # format serait publié comme une Source IS-07, avec un nom illisible et une valeur
    # éternellement « off ».
    p["input_format"] = {"width": 1920, "height": 1080}
    return {"vmid": vmid, "hostname": "mix", "project_id": None,
            "deploy_config": json.dumps({"type": "mixer", "params": p})}


# ═══ 1. CHAQUE DÉCLARANT, SEUL ══════════════════════════
controle("★★ TSL seul déclare ses correspondances",
         _sources(tsl_map=[{"connection_id": 1, "source_shm": "cam1", "tsl_index": 3}],
                  tsl_conn=[{"id": 1, "level_uuid": NA}]) == [("cam1", NA)])

controle("★★★ IS-07 ENTRANT seul publie — SANS aucune table TSL",
         _sources(i7_map=[{"connection_id": 7, "source_shm": "cam2"}],
                  i7_conn=[{"id": 7, "level_uuid": NB}]) == [("cam2", NB)],
         "★ C'EST LE DÉFAUT. Une passerelle qui reçoit son tally en IS-07 et le republie en "
         "IS-07 est un cas normal ; elle ne publiait RIEN faute d'une table TSL qu'elle n'a "
         "aucune raison de remplir.")

controle("★★★ un MÉLANGEUR ÉMETTEUR seul publie ses entrées, SANS TSL",
         _sources(cts=[_mixer(["cam1", "cam2"])]) == [("cam1", NA), ("cam2", NA)],
         "un site dont le tally naît de ses propres mélangeurs ne publiait rien non plus")

# ═══ 2. CE QUI NE DOIT PAS ÊTRE PUBLIÉ ═══════════════════
controle("★★ une connexion TSL SANS niveau n'écrit rien, donc ne publie rien",
         _sources(tsl_map=[{"connection_id": 1, "source_shm": "cam1", "tsl_index": 3}],
                  tsl_conn=[{"id": 1, "level_uuid": None}]) == [])

controle("★★ un mélangeur qui N'ÉMET PAS ne publie pas ses entrées",
         _sources(cts=[_mixer(["cam1"], emit=False)]) == [],
         "il ne tallyera jamais rien : la Source resterait éteinte à vie")

controle("★★ un mélangeur SANS niveau ne publie rien",
         _sources(cts=[_mixer(["cam1"], niveaux=())]) == [])

controle("★★★ les champs voisins d'une entrée ne sont pas pris pour des flux",
         _sources(cts=[_mixer(["cam1", "cam2"])]) == [("cam1", NA), ("cam2", NA)],
         "`input_1_fmt` commence par `input_` : le confondre avec un flux publierait une Source "
         "pour une valeur de format — dictionnaire OU chaîne. Obtenu %s"
         % _sources(cts=[_mixer(["cam1", "cam2"])]))

# ═══ 3. L'UNION, ET LA DÉDUPLICATION ═════════════════════
u = _sources(tsl_map=[{"connection_id": 1, "source_shm": "cam1", "tsl_index": 3}],
             tsl_conn=[{"id": 1, "level_uuid": NA}],
             cts=[_mixer(["cam1", "cam3"])])
controle("★★★ deux déclarants s'ADDITIONNENT sans se dupliquer",
         u == [("cam1", NA), ("cam3", NA)],
         "cam1 est déclarée deux fois sur le MÊME niveau : la publier deux fois ferait voir à un "
         "contrôleur des doublons qui changent ensemble sans savoir lequel fait foi. Obtenu %s" % u)

u = _sources(tsl_map=[{"connection_id": 1, "source_shm": "cam1", "tsl_index": 3}],
             tsl_conn=[{"id": 1, "level_uuid": NA}],
             cts=[_mixer(["cam1"], niveaux=(NB,))])
controle("★★★ le MÊME flux sur DEUX niveaux fait DEUX Sources",
         u == [("cam1", NA), ("cam1", NB)],
         "ce sont deux signaux distincts — deux productions peuvent tallyer la même caméra "
         "indépendamment. Obtenu %s" % u)

# ═══ 4. LA RÉFÉRENCE EST RÉSOLUE ═══════════════════════
tally._ports_cache["by_id"] = {42: {"kind": "source", "binding": {"shm": "cam9"}}}
tally._ports_cache["ts"] = 1e18
u = _sources(tsl_map=[{"connection_id": 1, "source_shm": "port:42", "tsl_index": 3}],
             tsl_conn=[{"id": 1, "level_uuid": NA}])
controle("★★★ un `port:<id>` est publié sous le flux auquel il est lié",
         u == [("cam9", NA)],
         "publié tel quel, la Source aurait une valeur toujours « off » : la clé d'état "
         "n'existerait jamais, et personne n'aurait de quoi le comprendre. Obtenu %s" % u)
tally._ports_cache["ts"] = 0

# ═══ 5. UN DÉCLARANT QUI TOMBE N'EMPORTE PAS LES AUTRES ══════════
_boom = is07._paires_melangeur


def _leve():
    raise RuntimeError("table illisible")


is07._paires_melangeur = _leve
try:
    u = _sources(tsl_map=[{"connection_id": 1, "source_shm": "cam1", "tsl_index": 3}],
                 tsl_conn=[{"id": 1, "level_uuid": NA}])
finally:
    is07._paires_melangeur = _boom
controle("★★ un déclarant en erreur ne fait pas disparaître les Sources des autres",
         u == [("cam1", NA)],
         "toutes les Sources s'évanouiraient du registre IS-04 d'un coup, sans rien pour "
         "l'expliquer côté contrôleur. Obtenu %s" % u)

# ═══ 6. STABILITÉ ═════════════════════════════════
_ETAT["tsl_map"] = [{"connection_id": 1, "source_shm": s, "tsl_index": i}
                    for i, s in enumerate(("camZ", "camA", "camM"), 1)]
_ETAT["tsl_conn"] = [{"id": 1, "level_uuid": NA}]
is07._src_cache["ts"] = 0.0
a = is07._sources()
is07._src_cache["ts"] = 0.0
b = is07._sources()
controle("★★★ l'énumération est ORDONNÉE et reproductible",
         a == b == sorted(a),
         "une Source IS-04 doit être stable : un registre qui se réordonne d'un tour à l'autre "
         "ferait croire à des changements. Obtenu %s" % [x[0] for x in a])

controle("★★ ...et l'identifiant d'une Source ne dépend QUE de (flux, niveau)",
         is07._sid("cam1", NA) == is07._sid("cam1", NA) != is07._sid("cam1", NB),
         "il ne doit dépendre ni de l'index TSL — une adresse de pupitre, réattribuable — ni du "
         "déclarant : la même Source déclarée par TSL puis par un mélangeur reste la même")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
