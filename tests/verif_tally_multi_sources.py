#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du CUMUL PAR SOURCE (`services/tsl:poser_tally`).
#
# CE QU'IL PROTÈGE. Plusieurs sources peuvent servir le MÊME niveau, et c'est voulu : deux
# contrôleurs broadcast sur une même chaîne, un émetteur TSL doublé par un Receiver IS-07, un
# mélangeur qui complète ce qu'un pupitre externe annonce. Avec une seule couche d'état, trois
# façons de se tromper — toutes silencieuses, toutes sur une fonction d'ANTENNE :
#   · le dernier écrivain écrase les autres ;
#   · une source qui repasse au vert écrit « off » sur le ROUGE d'une autre ;
#   · un écrivain qui ne pose que ses cases allumées ne peut jamais en éteindre une, donc une
#     source sortie du programme garde son rouge indéfiniment.
#
#   $ ./venv/bin/python tools/verif_tally_multi_sources.py
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

print("Tally — plusieurs sources sur un même niveau\n")

N = "niveau-a"          # un uuid de niveau, opaque : le code ne doit rien en supposer
M = "niveau-b"


def raz():
    with tsl._lock:
        tsl._tally_par_source.clear()
        tsl._tally_state.clear()


def etat():
    with tsl._lock:
        return dict(tsl._tally_state)


# ── Le cumul lui-même ────────────────────────────────────────────────────────
raz()
tsl.poser_tally("tsl:1", {(5, N): "red"})
tsl.poser_tally("tsl:2", {(5, N): "green"})
controle("★★★ deux sources sur la même case se CUMULENT en ambre",
         etat().get((5, N)) == "amber",
         "un rouge et un vert affirmés par deux contrôleurs sont deux faits VRAIS en même "
         "temps ; en garder un seul, c'est perdre l'information. Obtenu %r" % etat())

# ── Une source n'efface pas l'autre ──────────────────────────────────────────
tsl.poser_tally("tsl:2", {})
controle("★★★ une source qui se tait laisse le rouge de l'autre",
         etat().get((5, N)) == "red",
         "c'est LE piège de la couche unique : la seconde source repasse à vide et éteint la "
         "lampe de la première. Obtenu %r" % etat())
tsl.poser_tally("tsl:1", {})
controle("★★ quand toutes se taisent, la case s'éteint et disparaît",
         (5, N) not in etat() and not tsl.sources_du_tally(),
         "une case résiduelle à « off » fait grossir l'état indéfiniment. Obtenu %r" % etat())

# ── Le remplacement est INTÉGRAL par source ──────────────────────────────────
raz()
tsl.poser_tally("mixer:1", {(1, N): "red", (2, N): "green"})
tsl.poser_tally("mixer:1", {(3, N): "red"})
controle("★★★ une source remplace sa contribution ENTIÈRE",
         etat() == {(3, N): "red"},
         "sans ça, changer le PGM d'un mélangeur laisserait un rouge sur l'ancienne source — "
         "elle n'est plus à l'antenne et sa lampe reste allumée. Obtenu %r" % etat())

# ── ...sans toucher aux AUTRES sources ───────────────────────────────────────
raz()
tsl.poser_tally("tsl:1", {(7, N): "red"})
tsl.poser_tally("mixer:1", {(7, N): "green", (8, N): "red"})
tsl.poser_tally("mixer:1", {(8, N): "red"})
controle("★★★ le remplacement intégral ne retire que SES cases",
         etat().get((7, N)) == "red" and etat().get((8, N)) == "red",
         "un écrivain qui purge les cases d'un autre éteint un tally que personne n'a demandé "
         "d'éteindre. Obtenu %r" % etat())

# ── Les niveaux restent étanches ─────────────────────────────────────────────
raz()
tsl.poser_tally("tsl:1", {(1, N): "red"})
tsl.poser_tally("tsl:2", {(1, M): "green"})
controle("★★ deux niveaux différents ne se mélangent pas",
         etat().get((1, N)) == "red" and etat().get((1, M)) == "green",
         "un niveau est une chaîne de destination : les confondre allume un rouge sur la "
         "mauvaise production. Obtenu %r" % etat())

# ── Ce que le diagnostic doit pouvoir dire ───────────────────────────────────
raz()
tsl.poser_tally("tsl:1", {(4, N): "red"})
tsl.poser_tally("is07:r-9", {(4, N): "green"})
src = tsl.sources_du_tally().get("4_%s" % N) or {}
controle("★★★ on sait QUI affirme quoi",
         src == {"tsl:1": "red", "is07:r-9": "green"},
         "un niveau servi par deux écrivains ne dit pas lequel allume la lampe : sans cette "
         "vue, un tally en trop se cherche des deux côtés à la fois. Obtenu %r" % src)

# ── Idempotence : reposer la même chose ne réveille personne ─────────────────
raz()
tsl.poser_tally("tsl:1", {(2, N): "red"})
controle("★★ reposer une contribution identique ne signale aucun changement",
         tsl.poser_tally("tsl:1", {(2, N): "red"}) is False,
         "le distributeur se réveille sur ce signal : le lever pour rien, c'est un push vers "
         "tous les murs à chaque trame TSL reçue")
controle("★ un vrai changement, lui, est bien signalé",
         tsl.poser_tally("tsl:1", {(2, N): "green"}) is True)

# ── « off » posé explicitement = retrait, pas une couleur ────────────────────
raz()
tsl.poser_tally("tsl:1", {(6, N): "red"})
tsl.poser_tally("tsl:2", {(6, N): "off"})
controle("★★ « off » d'une source ne compte pas dans le cumul",
         etat().get((6, N)) == "red",
         "sinon la source muette ferait basculer la case à « off » par cumul. Obtenu %r" % etat())

raz()
print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
