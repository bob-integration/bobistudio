#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Le dimensionnement des cœurs dédiés : calibré sur une MESURE, et une formule qu'on peut exécuter.

★ CE QUI A ÉTÉ MESURÉ (Z8, 2026-10-28, 30 s de `cgroup cpu.stat` + fils /proc).
Deux pyramides réservaient 9 et 10 cœurs DÉDIÉS et consommaient 1,32 et 1,41 cœur — environ sept
fois trop. Dans chaque conteneur : 8 fils de travail à 14-16 %, own 1,5-1,6 ms pour un budget de
20 ms. Le profil (`plancher 6` + `0,5 cœur par 1080p-équivalent`) donnait 9 à 10 ; le plancher
seul valait déjà quatre fois le besoin. Conséquence sur le nœud : les réservations touchaient les
26 cœurs physiques, `physical_free` 0 — et toute politique d'exclusivité HT (frère réservé, nosmt)
aurait alors laissé ZÉRO cœur au mélangeur et au decklink-io. Un sur-dimensionnement ne se voit
pas sur le conteneur qui en profite : il se voit sur les voisins qui n'ont plus de place.

Ce que cette garde tient :
1. ★ la formule est EXÉCUTABLE (`coeurs_dedies_voulus`) : elle vivait dans le corps de
   `creer_container_compute`, donc aucune garde ne pouvait la faire tourner — et une formule
   qu'on ne peut pas exécuter dérive sans que personne ne le voie ;
2. ★ sur les DEUX configurations réellement mesurées, la réservation reste dans une fourchette
   raisonnable de la consommation mesurée (marge voulue : une réservation paie les pointes et la
   gigue, pas la moyenne) — et plus jamais 7× ;
3. le plancher EST surchargeable par conteneur (`cores_override`), contrairement à ce qu'on a cru :
   le mécanisme existe déjà et passe par `effective_resources` ;
4. les bornes : le plancher n'est jamais rogné par le manque de place, et la demande ne dépasse
   jamais ce que le pool peut rendre.

Hors ligne : aucun réseau, aucune base — tout est pur.
"""
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import docker_compute as dc                                          # noqa: E402

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


MAN = json.load(open(os.path.join(RACINE, "plugins", "pyramide", "plugin.json"), encoding="utf-8"))
RES = MAN["resources"]
BASE = int(RES["cores"])
PER = float(RES["cores_per_1080p_input"])

# Les deux conteneurs mesurés sur la Z8 : (nom, charge 1080p-équivalente, cœurs consommés).
MESURES = [("#230 Atelier-pyramide-2 — 8 entrées (2×1080p, 6×720p)", 4.67, 1.41),
           ("#232 pyramide — 7 sources actives", 4.00, 1.32)]
# Fourchette acceptée entre la réservation et la consommation MESURÉE. Le plancher haut n'est pas
# de la pudeur : au-delà, les voisins n'ont plus de place, et c'est ce qui est arrivé.
MARGE_MIN, MARGE_MAX = 1.5, 4.0

print("\n1. ★ La formule est exécutable, et calibrée sur la mesure")
verifie(hasattr(dc, "coeurs_dedies_voulus"),
        "1a. ★ `coeurs_dedies_voulus` existe : la formule a été sortie du corps de "
        "`creer_container_compute`, où aucune garde ne pouvait l'exécuter")
for nom, load, conso in MESURES:
    n = dc.coeurs_dedies_voulus(BASE, PER, load, libres=40)
    marge = n / conso
    verifie(MARGE_MIN <= marge <= MARGE_MAX,
            "1b. %s : %d cœurs pour %.2f consommé (×%.1f)" % (nom, n, conso, marge),
            "hors de la fourchette [%.1f, %.1f]" % (MARGE_MIN, MARGE_MAX))
    # Le profil d'AVANT, sur la même charge : la garde dit ce qu'on a quitté, pour qu'un retour
    # en arrière échoue ici et pas sur l'antenne.
    ancien = dc.coeurs_dedies_voulus(6, 0.5, load, libres=40)
    verifie(n < ancien / 2,
            "1c. %s : au moins deux fois moins que l'ancien profil (%d → %d)" % (nom, ancien, n))

print("\n2. Le plancher tient le cas vide, et la charge ne l'emballe pas")
verifie(dc.coeurs_dedies_voulus(BASE, PER, 0, libres=40) == BASE,
        "2a. aucune entrée câblée → le plancher, pas zéro (un conteneur sans source doit pouvoir "
        "démarrer avant d'être câblé)")
verifie(dc.coeurs_dedies_voulus(BASE, PER, 0.1, libres=40) == BASE + 1,
        "2b. une fraction d'entrée compte pour un cœur entier (arrondi au-dessus : un cœur ne se "
        "réserve pas en morceaux)",
        str(dc.coeurs_dedies_voulus(BASE, PER, 0.1, libres=40)))
verifie(dc.coeurs_dedies_voulus(BASE, PER, 16, libres=40) == BASE + 4,
        "2c. quatre entrées 4K (charge 16) → %d cœurs : la pondération par PIXELS est conservée, "
        "un worker 4K pèse bien ~4 fois un 1080p" % (BASE + 4),
        str(dc.coeurs_dedies_voulus(BASE, PER, 16, libres=40)))
verifie(dc.coeurs_dedies_voulus(BASE, PER, 8, libres=1) == BASE,
        "2d. ★ pool presque plein : on retombe au PLANCHER, jamais en dessous — mieux vaut un "
        "conteneur sur-souscrit et alerté qu'un conteneur rogné en douce sous son besoin",
        str(dc.coeurs_dedies_voulus(BASE, PER, 8, libres=1)))
verifie(dc.coeurs_dedies_voulus(BASE, PER, 100, libres=3) == 3,
        "2e. une charge délirante (100 équivalents) ne demande que les 3 cœurs que le pool peut "
        "rendre : la demande est bornée par le libre, pas par l'appétit",
        str(dc.coeurs_dedies_voulus(BASE, PER, 100, libres=3)))
verifie(dc.coeurs_dedies_voulus(BASE, PER, 100, libres=0, miens=5) == 5,
        "2f. et les cœurs DÉJÀ tenus par ce conteneur comptent comme disponibles (un resize les "
        "rend d'abord)", str(dc.coeurs_dedies_voulus(BASE, PER, 100, libres=0, miens=5)))

print("\n3. Le plancher EST surchargeable par conteneur")
# On l'a cru impossible (`base = res["cores"]`, lu dans le manifeste). Le mécanisme existe :
# `effective_resources` applique `containers.cores_override` AVANT que le plancher soit lu.
eff = dc.effective_resources("pyramide", {"cores_override": 5})
verifie(eff.get("cores") == 5,
        "3a. ★ `cores_override` remplace le plancher du manifeste pour CE conteneur seulement "
        "(PATCH /api/containers/<vmid>/resources)", str(eff))
verifie(dc.coeurs_dedies_voulus(eff["cores"], PER, 4.67, libres=40) == 5 + 2,
        "3b. et le ratio dynamique s'applique par-dessus ce plancher surchargé",
        str(dc.coeurs_dedies_voulus(eff["cores"], PER, 4.67, libres=40)))
verifie(dc.effective_resources("pyramide", {}).get("cores") == BASE,
        "3c. sans override, le manifeste fait foi")

print("\n4. La mesure qui justifie le chiffre est écrite À CÔTÉ de lui, et datée")
note = MAN.get("_resources_mesure") or ""
verifie("2026-10-28" in note and "1,41" in note,
        "4a. ★ le manifeste porte la mesure ET sa date : un chiffre de dimensionnement sans la "
        "mesure qui le justifie se périme en silence, et le suivant n'ose plus y toucher",
        note[:120])
verifie("REMESURER" in note,
        "4b. et il dit explicitement de remesurer avant de réduire encore")

print("\n5. Les autres plugins `pin` : pas de ratio dynamique, donc pas cette dérive")
import glob                                                                    # noqa: E402

ratios = {}
for f in sorted(glob.glob(os.path.join(RACINE, "plugins", "*", "plugin.json"))):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    r = d.get("resources") or {}
    if r.get("cores_per_1080p_input"):
        ratios[d.get("type")] = (r.get("cores"), r["cores_per_1080p_input"])
verifie(set(ratios) == {"pyramide"},
        "5a. ★ `pyramide` est le SEUL plugin à dimensionnement dynamique : c'est ce cumul "
        "(plancher + ratio) qui a produit 10 cœurs, pas les planchers en général. Un nouveau "
        "plugin qui adopte le ratio devra être mesuré avant — cette garde le lui rappellera.",
        str(ratios))

print("\n" + ("─" * 70))
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ cœurs dédiés : formule exécutable, calibrée sur la mesure, plancher surchargeable")
