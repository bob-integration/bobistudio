#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de l'EXPOSITION IS-07 : ce qu'on annonce, et ce qu'on n'annonce pas.
#
# ⚠ HORS CHEMIN DEPUIS LE 2026-09-01. Ce banc couvre `is07.candidats`/`exposes`, écrits pour
# arbitrer un Receiver PAR SORTIE. Le modèle est passé à un Receiver par CONNEXION (une connexion
# = un niveau, comme en TSL), ce qui rend l'arbitrage sans objet : il n'y a plus que les
# connexions qu'un exploitant a créées. Le code et ce banc sont GARDÉS DE CÔTÉ (décision de
# l'utilisateur) : le même arbitrage se posera peut-être pour les Sources SORTANTES, aujourd'hui
# filtrées implicitement par la correspondance TSL. Il ne teste donc plus le chemin de
# production — ne pas s'en servir comme preuve que l'entrant marche (c'est `verif_is07_entrant`).
#
# CE QU'IL PROTÈGE. On publiait un Receiver de tally par groupe de sortie — 99 sur ce banc, dont
# 6 seulement pouvaient recevoir quoi que ce soit. Un contrôleur se voyait offrir 93 abonnements
# qui auraient été refusés : annoncer une capacité qu'on ne sait pas honorer fait perdre son temps
# à celui d'en face, et noie les six qui marchent. Quatre façons de se tromper :
#   · exposer par défaut ce qu'on ne sait pas servir ;
#   · confondre EXPOSER (ce qu'un contrôleur voit) et AFFECTER (ce que ça alimente) ;
#   · rendre l'exposition irréversible en ne listant que les exposés — la sortie retirée
#     disparaîtrait du seul écran d'où on peut la remettre ;
#   · perdre la RAISON du refus : « pas de flux MXL » et « hors correspondance TSL » n'appellent
#     pas la même action, et « indisponible » envoie chercher au mauvais endroit.
#
# ⚠ CE BANC OUVRE `nmos_is07`/`nmos_is07_entrant` et TOUCHE `is07_expose_mode`, puis restaure.
#
#   $ ./venv/bin/python tools/verif_is07_exposition.py
import importlib
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


from app.database import db_set_setting, db_get_setting                # noqa: E402

print("IS-07 — choisir ce qu'on expose\n")

CLES = ("nmos_is07", "nmos_is07_entrant", "is07_expose_mode", "is07_expose")
AVANT = {k: db_get_setting(k) for k in CLES}
try:
    db_set_setting("nmos_is07", "1")
    db_set_setting("nmos_is07_entrant", "1")
    db_set_setting("is07_expose_mode", "adressables")
    from services import nmos                                          # noqa: E402
    from services.nmos import is07                                     # noqa: E402
    importlib.reload(is07)
    nmos.rebuild_model()

    cands = is07.candidats(nmos._senders)
    controle("★ il y a des candidats, sinon le banc ne prouve rien", len(cands) > 1,
             "obtenu %d" % len(cands))
    adr = [c for c in cands if c["adressable"]]
    controle("★★ tous ne sont PAS adressables, sinon le tri ne se mesure pas",
             0 < len(adr) < len(cands),
             "%d adressables sur %d" % (len(adr), len(cands)))

    raisons = {c["raison"] for c in cands if not c["adressable"]}
    controle("★★★ chaque refus porte SA raison, distincte",
             raisons == {"pas de flux MXL", "hors correspondance TSL"},
             "« indisponible » enverrait chercher au mauvais endroit : l'un se règle sur le "
             "moteur, l'autre dans la correspondance TSL. Obtenu %r" % raisons)
    controle("★★ un candidat adressable n'a pas de raison de refus",
             all(not c["raison"] for c in adr))

    # ── Le DÉFAUT n'annonce que ce qu'on sait honorer ────────────────────
    controle("★★★ mode par défaut : on n'expose que les adressables",
             len(is07.exposes(nmos._senders)) == len(adr),
             "exposer ce qu'on ne sait pas servir fait perdre son temps au contrôleur d'en "
             "face. Obtenu %d au lieu de %d" % (len(is07.exposes(nmos._senders)), len(adr)))
    # ⚠ LE CONTRÔLE « et le modèle IS-04 ne publie que ceux-là » A ÉTÉ RETIRÉ le 2026-09-01 :
    # `receivers_depuis` ne passe plus par `exposes` (un Receiver par CONNEXION, plus par sortie).
    # Le laisser en le « corrigeant » aurait fait passer ce banc pour une preuve du chemin de
    # production, qu'il n'est plus.

    # ── Les autres modes font ce qu'ils disent ───────────────────────────
    db_set_setting("is07_expose_mode", "toutes")
    controle("★★ mode `toutes` : tout est exposé, y compris le non servable",
             len(is07.exposes(nmos._senders)) == len(cands),
             "c'est un choix légitime, mais il doit être EXPLICITE — d'où le mode. Obtenu %d"
             % len(is07.exposes(nmos._senders)))

    db_set_setting("is07_expose_mode", "choisies")
    db_set_setting("is07_expose", [])
    controle("★★ mode `choisies` sans choix : on n'expose rien",
             is07.exposes(nmos._senders) == [],
             "un mode manuel qui exposerait tout par défaut serait un piège")
    deux = [c["groupe"] for c in cands[:2]]
    db_set_setting("is07_expose", deux)
    exp = [c["groupe"] for c in is07.exposes(nmos._senders)]
    controle("★★★ mode `choisies` : exactement les groupes désignés", sorted(exp) == sorted(deux),
             "obtenu %r" % exp)
    controle("★★ ...y compris un groupe NON adressable si on l'a choisi",
             all(g in exp for g in deux),
             "le mode manuel doit obéir : c'est l'exploitant qui décide, on l'a prévenu")

    # ── L'écran d'arbitrage : RETIRÉ ─────────────────────────────────────
    # `/api/tally/is07/receivers` n'existe plus — l'écran d'arbitrage par sortie non plus. Ce qui
    # reste ici ne teste que les FONCTIONS mises de côté, pas une route.

    # ── EXPOSER n'est pas AFFECTER ───────────────────────────────────────
    # La distinction reste vraie et vaut d'être gardée : l'exposition décide de ce qu'un
    # contrôleur VOIT, l'affectation de ce que ça ALIMENTE. Dans le modèle actuel c'est la
    # connexion qui porte le niveau, et rien dans ces fonctions ne le touche.
    controle("★★★ ces fonctions ne portent AUCUN niveau",
             all("level" not in c and "niveau" not in c for c in cands),
             "un candidat qui trimballerait un niveau referait la confusion qu'on vient de "
             "défaire : ce sont deux réglages, pas un")
finally:
    for k, v in AVANT.items():
        if k in ("nmos_is07", "nmos_is07_entrant"):
            db_set_setting(k, "0" if v in (None, "", 0, "0", False) else v)
        elif k == "is07_expose_mode":
            db_set_setting(k, v or "adressables")
        else:
            db_set_setting(k, v if isinstance(v, list) else [])
    try:
        from services import nmos as _n
        _n.rebuild_model()
    except Exception:
        pass
    print("\n  réglages restaurés : %r" % ({k: db_get_setting(k) for k in CLES},))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
