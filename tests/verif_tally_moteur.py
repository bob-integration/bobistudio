#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du MOTEUR de propagation du tally (services/tsl/__init__.py:propager).
#
# tally(entrée) = tally(sortie) ET (l'entrée contribue).
#
# LES DEUX ERREURS SE VALENT ICI, et le banc les vise toutes les deux : ne pas propager laisse une
# caméra froide alors qu'elle est à l'antenne ; propager trop allume un rouge sur une source qui
# ne l'est pas et fige un plateau. On vérifie donc autant ce que le moteur DÉDUIT que ce qu'il
# REFUSE de déduire.
#
#   $ ./venv/bin/python tools/verif_tally_moteur.py
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

print("Tally — moteur de propagation\n")

NIV = 3          # un niveau quelconque
IDX = {"cam1": 1, "cam2": 2, "delay_1": 3, "mixer_pgm": 4, "mur": 5}


def _ct(vmid, hostname, type_, params):
    return ({"vmid": vmid, "hostname": hostname}, {"type": type_, "params": params})


# Graphe : cam1 → delay → (rien) ; cam1+cam2 → mixer → mixer_pgm
PAR_SHM = {
    "delay_1":   _ct(10, "delay", "delay", {"video_channels": 1, "input_v_1": "cam1"}),
    "mixer_pgm": _ct(20, "mixer", "mixer", {"input_1": "cam1", "input_2": "cam2"}),
}


def _idx(table):
    """`idx_de(shm, niveau)` — une CALLABLE, parce que l'index dépend du porteur du niveau."""
    return lambda shm, niveau: table.get(shm)


def _prop(etat, ctrl=None):
    return tsl.propager(etat, PAR_SHM, _idx(IDX), ctrl or {})


# ── Élément TRAVERSANT ───────────────────────────────────────────────────────
r = _prop({(IDX["delay_1"], NIV): "red"})
controle("★★★ un traversant propage à son entrée",
         r.get((IDX["cam1"], NIV)) == "red",
         "la sortie d'un delay EST son entrée, transformée : si elle est à l'antenne, la source "
         "l'est. Obtenu %s" % r)

r = _prop({})
controle("★★★ rien à l'antenne → rien de propagé", r == {},
         "propager sans source allumerait tout le parc")

# ── MÉLANGEUR : seulement le PGM ─────────────────────────────────────────────
# ⚠ `cle_input` prend un indice 0-BASED et rend une clé 1-based : `pgm=0` désigne `input_1`.
# Ma première version supposait l'inverse et accusait le moteur d'allumer la mauvaise source.
r = _prop({(IDX["mixer_pgm"], NIV): "red"},
          {20: {"pgm": 0, "pvw": 1, "input_1": "cam1", "input_2": "cam2"}})
controle("★★★ un mélangeur propage à sa source PGM", r.get((IDX["cam1"], NIV)) == "red")
controle("★★★ et PAS à ses autres entrées", (IDX["cam2"], NIV) not in r,
         "cam2 est au PVW, elle n'est pas diffusée — l'allumer figerait un plateau. Obtenu %s" % r)

r = _prop({(IDX["mixer_pgm"], NIV): "red"},
          {20: {"pgm": 1, "pvw": 0, "input_1": "cam1", "input_2": "cam2"}})
controle("★★ le PGM change → la propagation suit",
         r.get((IDX["cam2"], NIV)) == "red" and (IDX["cam1"], NIV) not in r)

r = _prop({(IDX["mixer_pgm"], NIV): "red"}, {})     # pas d'état du mélangeur
controle("★★★ mélangeur dont on n'a pas l'état → on ne propage RIEN", r == {},
         "sans son /state on ne SAIT pas quelle source est au PGM. Deviner, c'est allumer un "
         "rouge au hasard parmi ses entrées — obtenu %s" % r)

# ── Ce qu'on ne connaît pas ne propage rien ──────────────────────────────────
PAR_SHM_DVE = {"mur": _ct(30, "mv", "multiview", {"input_1": "cam1", "input_2": "cam2"})}
r = tsl.propager({(IDX["mur"], NIV): "red"}, PAR_SHM_DVE, _idx(IDX), {})
controle("★★★ un DVE ne propage rien (il ne sait pas encore dire ce qui est VISIBLE)", r == {},
         "une source hors cadre n'a pas de tally ; propager à toutes les entrées d'un multiview "
         "allumerait tout le mur — c'est le défaut qu'on corrige, à l'envers. Obtenu %s" % r)

# ⚠ LE CONTRE-EXEMPLE DOIT DISTINGUER. Une première version utilisait un type inventé et un DVE
# sans `flux_config` : dans les deux cas `derive_wiring` ne rend AUCUNE entrée, donc le test
# passait quelle que soit la règle — muté « un type inconnu propage tout », le banc restait vert.
# `split` déclare de vrais `state_field` ET n'est pas dans `_CONTRIBUTION` : c'est le seul cas qui
# oppose réellement « je ne sais pas » à « tout ».
PAR_SHM_SPLIT = {"mur": _ct(40, "sp", "split", {"input_1": "cam1", "input_2": "cam2"})}
r = tsl.propager({(IDX["mur"], NIV): "red"}, PAR_SHM_SPLIT, _idx(IDX), {})
controle("★★★ un type CONNU mais absent de la liste ne propage rien", r == {},
         "`_CONTRIBUTION` est une liste FERMÉE : un type absent vaut « je ne sais pas », jamais "
         "« tout ». `split` a de vraies entrées câblées — les allumer serait inventer un rouge. "
         "Obtenu %s" % r)
controle("★ ...et split a bien des entrées à propager, sinon le contrôle ne prouve rien",
         len(tsl._entrees_contributives(
             {"vmid": 40, "hostname": "sp"},
             {"type": "delay", "params": {"video_channels": 1, "input_v_1": "cam1"}}, None)) == 1,
         "le témoin positif : la même mécanique DÉDUIT bien une entrée quand la règle existe")

# ── Le niveau et la couleur sont conservés ───────────────────────────────────
r = _prop({(IDX["delay_1"], 7): "green"})
controle("★★ le NIVEAU est conservé le long de la chaîne",
         r.get((IDX["cam1"], 7)) == "green" and (IDX["cam1"], NIV) not in r,
         "propager sur un autre niveau tallyerait une source pour une production qui ne la "
         "diffuse pas")

# ── On n'écrase jamais ce qu'un émetteur a dit ───────────────────────────────
r = _prop({(IDX["delay_1"], NIV): "red", (IDX["cam1"], NIV): "green"})
controle("★★ un tally déjà posé par un émetteur n'est pas écrasé",
         (IDX["cam1"], NIV) not in r,
         "VSM fait autorité sur ce qu'il dit ; notre déduction ne vient qu'en complément")

# ── Cycles et profondeur ─────────────────────────────────────────────────────
BOUCLE = {"a": _ct(50, "a", "delay", {"video_channels": 1, "input_v_1": "b"}),
          "b": _ct(51, "b", "delay", {"video_channels": 1, "input_v_1": "a"})}
# ⚠ SOUS CHIEN DE GARDE, et pas dans un simple `try`. Muter le plafond de profondeur ne lève
# rien : le moteur boucle, et le banc BLOQUE — un banc qui ne rend pas la main ne dit pas ce qui
# ne va pas, il fait juste perdre du temps à chercher où. On borne donc le temps, et on ÉCHOUE.
import threading                                                    # noqa: E402
_fini = []


def _essai_boucle():
    tsl.propager({(8, NIV): "red"}, BOUCLE, _idx({"a": 8, "b": 9}), {})
    _fini.append(True)


_th = threading.Thread(target=_essai_boucle, daemon=True)
_th.start()
_th.join(10)
boucle_ok = bool(_fini)
controle("★★★ un graphe qui BOUCLE ne fait pas tourner le moteur indéfiniment", boucle_ok,
         "le moteur tourne dans la boucle du distributeur : ne pas rendre la main, c'est arrêter "
         "toute distribution de tally")
controle("★ le plafond de profondeur est déclaré", tsl._PROFONDEUR_MAX >= 4)

# ── Le moteur ne modifie RIEN ────────────────────────────────────────────────
etat = {(IDX["delay_1"], NIV): "red"}
copie = dict(etat)
_prop(etat)
controle("★★★ le moteur ne modifie pas l'état qu'on lui donne", etat == copie,
         "il rend ses déductions à part : sans cette séparation, un tally propagé deviendrait "
         "indiscernable d'un tally reçu au tour suivant et se propagerait à son tour")

# ── Une source sans index n'est adressable par personne ──────────────────────
r = tsl.propager({(IDX["delay_1"], NIV): "red"},
                 {"delay_1": _ct(10, "delay", "delay",
                                 {"video_channels": 1, "input_v_1": "inconnue"})}, _idx(IDX), {})
controle("★ une entrée sans index TSL est ignorée, sans lever", r == {})

# ── L'index dépend du PORTEUR du niveau ──────────────────────────────────────
# ★ C'est la raison d'être de la callable. Deux porteurs peuvent employer le MÊME index pour des
# sources différentes : une table à plat ferait propager sur le mauvais signal, silencieusement.
def _idx_par_porteur(shm, niveau):
    # niveau 3 → porteur A (cam1 = 1) ; niveau 7 → porteur B (cam1 = 42)
    return {"cam1": 1, "delay_1": 3}.get(shm) if niveau == 3 \
        else {"cam1": 42, "delay_1": 43}.get(shm)


r3 = tsl.propager({(3, 3): "red"}, PAR_SHM, _idx_par_porteur, {})
r7 = tsl.propager({(43, 7): "red"}, PAR_SHM, _idx_par_porteur, {})
controle("★★★ l'index est résolu SELON LE PORTEUR du niveau",
         r3 == {(1, 3): "red"} and r7 == {(42, 7): "red"},
         "deux porteurs peuvent employer le même index pour des sources différentes : une table "
         "à plat propagerait sur le mauvais signal, sans rien signaler. Obtenu %s / %s" % (r3, r7))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
