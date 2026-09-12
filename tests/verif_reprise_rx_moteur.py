#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Un moteur 2110 revenu SANS session RX doit être RÉPARÉ, pas seulement signalé.
#
# Incident du 2026-09-12, qui a produit ce banc. Moteur redémarré à 09:42:21 pour adopter une
# nouvelle image. `resync_moteur` a re-poussé les abonnements à +7 s puis à +39 s — deux fois
# dans le vide, le moteur n'ayant pas fini son `mtl_init` —, a posé une alerte `error`, et n'a
# PLUS RIEN TENTÉ. Le détecteur permanent de `metrics`, lui, a fait ce pour quoi il était écrit :
# alerter. Personne n'a réparé. **L'antenne est restée muette 13 minutes**, jusqu'à un
# `repush_subscriptions` lancé à la main.
#
# Deux défauts, un seul symptôme :
#   1. la reprise comptait des ESSAIS (exactement deux) au lieu de tenir une ÉCHÉANCE, alors que
#      la docstring de la fonction elle-même annonce 30-60 s de `mtl_init` ;
#   2. le détecteur permanent alertait sans jamais tenter la reprise.
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


print("Reprise automatique des sessions RX d'un moteur 2110\n")

dd = open(os.path.join(RACINE, "app", "docker_driver.py"), encoding="utf-8").read()
me = open(os.path.join(RACINE, "app", "metrics.py"), encoding="utf-8").read()

print("── resync_moteur : une échéance, pas un compte d'essais ────────────────")
controle("★★★ la reprise n'est plus bornée à deux essais",
         "for tentative in (1, 2):" not in dd,
         "deux essais de 30 s bornent la reprise à ~60 s, alors que le moteur met 30-60 s de "
         "PLUS que `:8081/status` à être prêt — le budget expirait avant qu'il n'écoute")
controle("★★★ une échéance franche est déclarée et utilisée",
         "_RESYNC_RX_DEADLINE_S" in dd
         and "fin = time.monotonic() + _RESYNC_RX_DEADLINE_S" in dd
         and "if time.monotonic() >= fin:" in dd,
         "sans échéance lue, la constante ne serait qu'un commentaire")
_m = re.search(r"_RESYNC_RX_DEADLINE_S\s*=\s*(\d+)", dd)
controle("★★ l'échéance couvre largement un `mtl_init`",
         bool(_m) and int(_m.group(1)) >= 180,
         "30-60 s de mtl_init sur E810, entraînement du lien 100G compris, plus la marge d'une "
         "recréation qui s'enchaînerait. Obtenu %s s" % (_m.group(1) if _m else "?"))
controle("★★★ on re-pousse à CHAQUE tour, pas une seule fois",
         dd.count("_nmos.repush_subscriptions(vmid)") >= 2
         and "n_push += 1" in dd,
         "le cas qui nous intéresse est celui où le moteur n'était pas prêt au tour précédent : "
         "ne pas re-pousser ensuite, c'est ne rien faire")
controle("★★ l'échec au bout de l'échéance reste une ALERTE",
         'alert.docker.rx_revenu_vide' in dd,
         "réessayer sans jamais renoncer masquerait une vraie panne")

print("\n── le détecteur permanent RÉPARE ───────────────────────────────────────")
controle("★★★ une fonction de reprise existe", "_tenter_reprise_rx" in me,
         "le détecteur ne faisait qu'alerter — c'est ce qui a laissé l'antenne muette")
controle("★★★ elle est APPELÉE par le détecteur",
         "_tenter_reprise_rx(vmid, hn)" in me,
         "une fonction jamais appelée est du code mort qui rassure")
controle("★★★ elle ne bloque PAS la boucle de surveillance",
         "threading.Thread(target=_worker, daemon=True).start()" in me,
         "`repush_subscriptions` attend la disponibilité du contrôleur : en ligne, elle gèlerait "
         "`surveillance()` pour TOUS les autres conteneurs — défaut déjà payé avec la "
         "réconciliation RDMA")
controle("★★ jamais deux reprises en vol pour le même moteur",
         "_rx_repair_en_vol" in me and "if vmid in _rx_repair_en_vol:" in me
         and "_rx_repair_en_vol.discard(vmid)" in me,
         "sans garde, un tick toutes les 5 s empilerait les threads ; sans `discard` dans un "
         "`finally`, un échec bloquerait toute reprise ultérieure")
controle("★★★ les reprises sont ESPACÉES et BORNÉES",
         "RX_REPAIR_TOUS_LES_N_POLLS" in me and "RX_REPAIR_MAX" in me
         and "_rx_repair_n.get(vmid, 0) < RX_REPAIR_MAX" in me,
         "une cause qui n'est PAS une perte d'abonnement — source amont éteinte, lien coupé — "
         "ne doit pas faire marteler le contrôleur indéfiniment")
controle("★★ le compteur de reprises repart à l'épisode suivant",
         "_rx_repair_n.pop(vmid, None)" in me,
         "sans remise à zéro, un moteur ayant épuisé ses 5 reprises un jour ne serait plus "
         "jamais réparé — la panne suivante resterait muette")

print("\n── on n'a pas cassé ce qui marchait ────────────────────────────────────")
controle("★★ l'alerte de manque soutenu subsiste",
         "alert.rx.sessions_manquantes" in me and "alert.rx.sessions_retablies" in me,
         "la reprise complète l'alerte, elle ne la remplace pas")
controle("★★ le seuil d'alerte n'a pas bougé",
         "RX_MISSING_POLLS = 4" in me,
         "alerter plus tard pour laisser la reprise agir serait un recul : l'exploitant doit "
         "savoir qu'il s'est passé quelque chose, même si c'est réparé")

# ── ET ON L'EXÉCUTE ─────────────────────────────────────────────────────────
# Les contrôles ci-dessus lisent la source. Tous verts du premier coup, donc suspects : ils ne
# prouvent pas que la reprise se DÉCLENCHE. On fait tourner le détecteur pour de vrai, avec un
# moteur qui doit 6 sessions et n'en sert aucune.
print("\n── Le détecteur, exécuté ───────────────────────────────────────────────")
import app.metrics as M                                              # noqa: E402

_appels = []
M._tenter_reprise_rx = lambda vmid, hn: _appels.append((vmid, hn))
M._rx_missing_cnt.clear(); M._rx_missing_alert.clear(); M._rx_repair_n.clear()
M.db_add_alert = lambda *a, **k: None          # on ne pollue pas la base du site
M._node_de = lambda v: None


class _FauxNmos:
    @staticmethod
    def nb_sessions_rx_attendues(vmid):
        return 6


sys.modules["services.nmos"] = _FauxNmos
import services                                                      # noqa: E402
services.nmos = _FauxNmos

MUETS = [{"essence": "video", "mode": "idle"} for _ in range(6)]
SAINS = [{"essence": "video", "mode": "mtl"} for _ in range(6)]

for i in range(1, 4):                       # sous le seuil : on ne doit RIEN tenter
    M._check_sessions_moteur(42, "moteur-test", MUETS)
controle("★★★ aucune reprise avant le seuil de %d polls" % M.RX_MISSING_POLLS, not _appels,
         "réparer au premier tick confondrait un démarrage normal avec une panne — un moteur "
         "qui vient de partir monte ses sessions en quelques secondes. Obtenu %r" % (_appels,))

for i in range(4, M.RX_REPAIR_TOUS_LES_N_POLLS + 1):
    M._check_sessions_moteur(42, "moteur-test", MUETS)
controle("★★★ la reprise est DÉCLENCHÉE quand le manque dure",
         len(_appels) == 1 and _appels[0][0] == 42,
         "c'est tout l'objet du correctif : sans cet appel, l'antenne reste muette jusqu'à ce "
         "qu'un humain regarde. Obtenu %r après %d polls" % (_appels, M.RX_REPAIR_TOUS_LES_N_POLLS))

for _ in range(M.RX_REPAIR_TOUS_LES_N_POLLS * (M.RX_REPAIR_MAX + 3)):
    M._check_sessions_moteur(42, "moteur-test", MUETS)
controle("★★★ et elle est BORNÉE", len(_appels) == M.RX_REPAIR_MAX,
         "une source amont éteinte ne doit pas faire marteler le contrôleur indéfiniment. "
         "Obtenu %d tentatives pour un maximum de %d" % (len(_appels), M.RX_REPAIR_MAX))

M._check_sessions_moteur(42, "moteur-test", SAINS)        # épisode clos
avant = len(_appels)
for _ in range(M.RX_REPAIR_TOUS_LES_N_POLLS + M.RX_MISSING_POLLS + 1):
    M._check_sessions_moteur(42, "moteur-test", MUETS)
controle("★★★ un NOUVEL épisode redonne droit à la reprise", len(_appels) > avant,
         "sans remise à zéro, un moteur ayant épuisé ses reprises un jour ne serait plus jamais "
         "réparé — la panne suivante resterait muette pour toujours")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
