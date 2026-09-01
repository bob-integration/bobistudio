#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du RÉORDONNANCEMENT des niveaux de tally (`db_set_tally_levels_order`) — modèle uuid/num.
#
# CE QU'IL PROTÈGE. Un niveau a DEUX barreaux, comme un conteneur (cf. CLAUDE.md) : `uuid` est
# l'identité — c'est elle que citent les configurations, les conteneurs et les Sources IS-07, et
# elle ne bouge jamais — tandis que `num` n'est qu'un rang d'affichage, que réordonner réécrit
# librement. C'est cette séparation qui rend le réordonnancement gratuit : la ligne bouge, son
# numéro suit, et rien d'autre ne s'en aperçoit. Trois façons de la casser, toutes silencieuses :
#   · faire bouger un uuid en réordonnant — les configurations qui le citent pointent alors
#     ailleurs, et un rouge apparaît sur la mauvaise source ;
#   · perdre un niveau absent de la liste envoyée (une page filtrée le ferait disparaître) ;
#   · laisser deux niveaux au même numéro, ce qui rend l'ordre dépendant du rowid.
#
# ⚠ CE BANC ÉCRIT DANS LA BASE DE PRODUCTION, sur la seule colonne `num`, et REMET l'ordre initial
# dans un `finally`. C'est le seul moyen de prouver quoi que ce soit : `get_db()` épingle le
# chemin de la base au premier accès, donc une copie ne serait pas celle que la fonction ouvre.
#
#   $ ./venv/bin/python tools/verif_tally_ordre.py
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


from app.database import (db_get_tally_levels, db_set_tally_levels_order,   # noqa: E402
                          db_get_tally_levels_of)

print("Tally — réordonner : la ligne ET son numéro, jamais l'identité\n")

depart = [n["uuid"] for n in db_get_tally_levels()]
noms_depart = {n["uuid"]: n["nom"] for n in db_get_tally_levels()}
controle("★ il y a au moins trois niveaux, sinon le banc ne prouve rien", len(depart) >= 3,
         "obtenu %d" % len(depart))

try:
    # ── Déplacer le dernier en tête ──────────────────────────────────────
    voulu = [depart[-1]] + depart[:-1]
    db_set_tally_levels_order(voulu)
    apres = [n["uuid"] for n in db_get_tally_levels()]
    controle("★★★ l'ordre demandé est celui rendu", apres == voulu,
             "attendu %s, obtenu %s" % (voulu[:2], apres[:2]))

    # ── LE NUMÉRO SUIT LA LIGNE — c'est ce que « réordonner » veut dire ──
    nums = [n["num"] for n in db_get_tally_levels()]
    controle("★★★ le numéro suit la ligne : 1..N dans le nouvel ordre",
             nums == list(range(1, len(nums) + 1)),
             "un numéro qui reste en place quand la ligne bouge, personne n'appelle ça "
             "réordonner. Obtenu %s" % nums[:6])

    # ── ...SANS que l'IDENTITÉ ne bouge : c'est tout l'enjeu ─────────────
    controle("★★★ aucun UUID n'a changé", sorted(apres) == sorted(depart),
             "l'uuid est ce que citent les configurations : le déplacer les ferait pointer "
             "ailleurs — un rouge sur la mauvaise source")
    controle("★★★ chaque niveau a gardé SON nom",
             {n["uuid"]: n["nom"] for n in db_get_tally_levels()} == noms_depart,
             "un nom qui suit le rang au lieu de l'identité, c'est le même bug vu d'ailleurs")

    # ── Une liste PARTIELLE ne fait disparaître personne ─────────────────
    partiel = [depart[1], depart[0]]
    db_set_tally_levels_order(partiel)
    apres = [n["uuid"] for n in db_get_tally_levels()]
    controle("★★★ une liste partielle ne perd aucun niveau",
             sorted(apres) == sorted(depart),
             "un appelant qui n'a qu'une vue filtrée ne doit pas pouvoir effacer du classement "
             "ce qu'il ne voyait pas. Obtenu %d au lieu de %d" % (len(apres), len(depart)))
    controle("★★ les niveaux nommés passent devant, les autres gardent leur ordre relatif",
             apres[:2] == partiel,
             "obtenu %s" % apres[:4])

    # ── Rangs UNIQUES : sinon `id` départage et l'ordre saute ────────────
    rangs = [n["num"] for n in db_get_tally_levels()]
    controle("★★★ les numéros sont uniques et contigus à partir de 1",
             sorted(rangs) == list(range(1, len(rangs) + 1)),
             "deux niveaux au même numéro, et c'est le rowid qui tranche : l'ordre affiché ne "
             "serait plus celui qu'on a demandé. Obtenu %s" % rangs[:6])

    # ── Entrées absurdes : on ignore, on ne casse pas ────────────────────
    avant = [n["uuid"] for n in db_get_tally_levels()]
    db_set_tally_levels_order([depart[0], depart[0], "pas-un-uuid", "", None, 0])
    apres = [n["uuid"] for n in db_get_tally_levels()]
    controle("★★ doublons, inconnus et valeurs illisibles sont ignorés sans casse",
             sorted(apres) == sorted(avant) and apres[0] == depart[0],
             "obtenu %s" % apres[:4])

    # ── L'ordre d'un PORTEUR suit, car c'est la même colonne ─────────────
    # Il compte : une connexion rattachée à une production sans niveau propre hérite du PREMIER
    # de cette production (`db_upsert_tsl_connection`).
    proj = [n for n in db_get_tally_levels() if n["owner_kind"] == "project"]
    if len(proj) >= 1:
        pid = proj[0]["owner_id"]
        controle("★★ `db_get_tally_levels_of` rend le même ordre que la liste globale",
                 db_get_tally_levels_of("project", pid)
                 == [n["uuid"] for n in db_get_tally_levels()
                     if n["owner_kind"] == "project" and n["owner_id"] == pid],
                 "deux ordres différents pour la même donnée, et l'héritage d'une connexion "
                 "cesserait de correspondre à ce que la page montre")
finally:
    db_set_tally_levels_order(depart)
    revenu = [n["uuid"] for n in db_get_tally_levels()]
    print("\n  ordre initial %s : %s"
          % ("restauré" if revenu == depart else "NON RESTAURÉ ⚠",
             [n["num"] for n in db_get_tally_levels()][:6]))
    if revenu != depart:
        echecs.append("restauration")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
