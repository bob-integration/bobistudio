#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du RETRAIT de la contribution d'un mélangeur.
#
# ★ SE TAIRE, C'EST DIRE « RIEN », PAS SE TAIRE. Un mélangeur qui cesse d'émettre faisait
# `continue` : sa contribution précédente restait dans le modèle, et sa caméra gardait son rouge
# indéfiniment. L'exploitant qui décoche « émettre le tally » n'a pas demandé à figer un plateau.
#
# C'est la même faute que la tuile sautée (PiP4), l'IS-07 entrant qui rejetait les sources sans
# index, la propagation qui abandonnait ses amonts, et le libellé cuit qui survivait à sa source.
# Elle a une forme unique : NE PAS SAVOIR, OU NE PLUS AVOIR À DIRE, N'AUTORISE PAS À LAISSER
# CROIRE. Un état qu'on cesse de rafraîchir n'est pas neutre — il ment.
#
# ⚠ UNE EXCEPTION, ET UNE SEULE : le mélangeur injoignable. Là on GARDE, parce qu'un timeout de
# 800 ms est presque toujours un hoquet, et qu'éteindre le tally d'une source à l'antenne pour ça
# serait pire. Le cas définitif — le mélangeur détruit — est couvert par le balayage, pas par le
# timeout. Ce banc vérifie les deux, sinon « on garde toujours » le passerait.
#
#   $ ./venv/bin/python tests/verif_mixer_tally_retrait.py
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


from app import tally                                                # noqa: E402
import app.metrics as _m                                             # noqa: E402

_m.get_container_ip = lambda v: "127.0.0.1"

print("Mélangeur — retrait de la contribution\n")

NIV = "niv-a"
ALLUME = {"cam1_%s" % NIV: "red", "cam2_%s" % NIV: "green"}


def _ct(emit=True, niveaux=(NIV,), type_="mixer", vmid=900):
    return {"vmid": vmid, "hostname": "mix", "project_id": None,
            "deploy_config": json.dumps({"type": type_, "params": {
                "tally_emit": emit, "tally_level_base": list(niveaux),
                "tally_force": True}})}


class _OK:
    class _Rep:
        def json(self):
            return {"pgm": 0, "pvw": 1, "input_1": "cam1", "input_2": "cam2"}

    def get(self, *a, **k):
        return self._Rep()


class _KO:
    def get(self, *a, **k):
        raise OSError("timeout")


def _tick(req, cts):
    tally._mixer_publisher_tick(req, lambda: cts)
    return tally.get_tally_state()


def _vider():
    with tally._lock:
        tally._tally_state.clear()
        tally._tally_par_source.clear()
    tally._mixers_publies.clear()


# ── Le témoin positif : sans lui, tous les contrôles « éteint » passeraient à vide ──
_vider()
controle("★★★ un mélangeur émetteur allume bien ses sources",
         _tick(_OK(), [_ct()]) == ALLUME,
         "sans ce témoin, un modèle qui n'allumerait JAMAIS rien rendrait ce banc entièrement "
         "vert. Obtenu %s" % tally.get_tally_state())

# ── 1. « Émettre le tally » décoché ────────────────────────
controle("★★★ décocher « émettre le tally » ÉTEINT sa contribution",
         _tick(_OK(), [_ct(emit=False)]) == {},
         "★ C'est le défaut : `continue` laissait le rouge en place indéfiniment. Reproduit "
         "avant correction. Obtenu %s" % tally.get_tally_state())

controle("★★ ...et le recocher la rallume",
         _tick(_OK(), [_ct()]) == ALLUME,
         "un retrait qui ne se défait pas serait aussi grave : le mélangeur ne pourrait plus "
         "jamais tallyer. Obtenu %s" % tally.get_tally_state())

# ── 2. Plus de niveau ────────────────────────────
# ⚠ CE CONTRÔLE A ÉTÉ REFAIT. Il affirmait « sans niveau, la contribution s'éteint » — vrai,
# mais le garde n'y est pour rien : sans niveau, `want` est vide et le chemin normal retire déjà.
# Muté, le banc restait vert. Ce que le garde apporte réellement, c'est de ne pas aller
# INTERROGER le conteneur pour un mélangeur qui n'a personne à adresser : une requête par
# mélangeur et par tour, dix fois par seconde. C'est cela qu'on vérifie.
class _Compteur(_OK):
    def __init__(self):
        self.n = 0

    def get(self, *a, **k):
        self.n += 1
        return super().get(*a, **k)


_c = _Compteur()
_tick(_c, [_ct(niveaux=())])
controle("★★★ un mélangeur sans niveau n'est pas interrogé, et n'allume rien",
         _c.n == 0 and tally.get_tally_state() == {},
         "il n'adresse plus personne : l'interroger dix fois par seconde pour n'en rien faire "
         "est une requête pure perte. Obtenu %d requête(s), état %s"
         % (_c.n, tally.get_tally_state()))

# ── 3. Le mélangeur disparaît ───────────────────────────
_tick(_OK(), [_ct()])
controle("★★★ un mélangeur DÉTRUIT ne laisse pas sa dernière contribution",
         _tick(_OK(), []) == {},
         "il ne repassera jamais par la boucle : sans balayage de fin, son rouge resterait dans "
         "le modèle pour toujours. Obtenu %s" % tally.get_tally_state())

_tick(_OK(), [_ct()])
controle("★★ ...ni un conteneur qui CHANGE de type",
         _tick(_OK(), [_ct(type_="multiview")]) == {},
         "un vmid réattribué à autre chose emporterait sinon le tally de son prédécesseur. "
         "Obtenu %s" % tally.get_tally_state())

# ── 4. L'EXCEPTION : injoignable ────────────────────────
_tick(_OK(), [_ct()])
controle("★★★ un mélangeur INJOIGNABLE garde sa contribution",
         _tick(_KO(), [_ct()]) == ALLUME,
         "800 ms de timeout est presque toujours un hoquet ; éteindre le tally d'une source à "
         "l'antenne pour ça serait pire que de le garder. C'est ce contrôle qui empêche de "
         "« corriger » la famille en éteignant partout. Obtenu %s" % tally.get_tally_state())

controle("★★ ...mais un mélangeur injoignable ET disparu est bien retiré",
         _tick(_KO(), []) == {},
         "l'indulgence vaut pour le silence, pas pour l'absence. Obtenu %s"
         % tally.get_tally_state())

# ── 5. Un mélangeur n'éteint jamais ce qu'un AUTRE écrivain affirme ────────
_vider()
tally.poser_tally("vsm", {("cam1", NIV): "red"})
_tick(_OK(), [_ct()])
controle("★★ le retrait ne touche pas aux autres écrivains",
         _tick(_OK(), [_ct(emit=False)]) == {"cam1_%s" % NIV: "red"},
         "VSM affirme toujours cam1 : retirer la contribution du mélangeur ne doit pas emporter "
         "la sienne. Obtenu %s" % tally.get_tally_state())
tally.poser_tally("vsm", {})
_vider()

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
