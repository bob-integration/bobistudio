#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Le journal d'un composant doit porter le même numéro que son manifeste.

★ POURQUOI. Un composant (plugin ou service) porte sa version à DEUX endroits : son
manifeste (`plugin.json` / `manifest.json`), que le Catalogue lit pour afficher « 0.15.2 »,
et son journal (`meta.json`), qui dit ce qui a changé. Rien ne les tenait ensemble.

Mesuré le 2026-11-01, en préparant trois notes de release : `color_corrector` annonçait
0.15.2 avec un journal arrêté à 0.14.0 — quatre versions sans une ligne. Le Catalogue
proposait donc une mise à jour dont il ne pouvait rien dire.

★ CE QUE CETTE GARDE NE FAIT PAS, et pourquoi elle est aussi grossière. Elle ne juge ni le
CONTENU du journal, ni le TITRE de ses sections, ni son SCHÉMA. Trois critères plus fins ont
été essayés et rendaient des faux positifs, chaque fois parce que la convention réelle est
plus large que celle qu'on croyait :
  · `scope` titre ses sections par une PLAGE (« 0.76.4 → 0.76.7 ») ;
  · un composant en version initiale a des sections NON numérotées (« Nouveautés ») ;
  · DEUX schémas coexistent — 34 composants ont un dictionnaire `changes`, 3 une liste
    `changelog` (mesuré le 2026-11-01). Exiger `changes` comptait les trois autres en faute.
Elle ne tient donc qu'à l'ÉGALITÉ DES DEUX NUMÉROS — mécanique, sans jugement, et vraie quelle
que soit la forme du journal. Un critère qu'on ne peut pas se tromper en lisant.

★ LA LISTE D'EXCLUSIONS NE PEUT QUE RÉTRÉCIR. Les 12 cas existants ne se corrigent pas en une
soirée, et une garde rouge qui ne désigne rien d'actionnable finit désactivée. On les inscrit
donc dans `tools/journaux_versions_exclus.txt` — et une exclusion qui NE MORD PLUS est une
ERREUR : dès qu'un composant est remis d'accord avec lui-même, la garde exige qu'on le retire
de la liste. C'est ce qui la vide, au lieu de la laisser s'installer.

⚠ La liste vit dans `tools/` et sort de la publication (cf. `INTERNE` dans `tools/publier.sh`) :
elle nomme des plugins privés, que `CLAUDE.md` s'interdit d'énumérer. Son absence vaut donc
« aucune exclusion », ce qui est le comportement voulu sur l'arbre dérivé — où seuls les
composants PUBLIÉS existent, et où ils doivent être d'accord avec eux-mêmes.

Sortie non-zéro = au moins un composant à remettre d'accord, ou une exclusion à retirer.
"""
import json
import pathlib
import subprocess
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent
EXCLUSIONS = RACINE / "tools" / "journaux_versions_exclus.txt"
MANIFESTES = ("plugin.json", "manifest.json")


def _composants():
    """Chemins des sous-modules déclarés QUI EXISTENT sur ce disque et portent un manifeste.

    ⚠ On part de `.gitmodules`, pas d'un parcours de `plugins/` : les dossiers `_*` sont des
    runtimes partagés, pas des composants. Et on saute ce qui n'est pas là — sur l'arbre
    dérivé, les sous-modules privés sont retirés et leur dossier est vide.
    """
    try:
        out = subprocess.run(["git", "config", "-f", str(RACINE / ".gitmodules"),
                              "--get-regexp", r"\.path$"],
                             cwd=RACINE, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    chemins = []
    for ligne in out.stdout.splitlines():
        parts = ligne.split()
        if len(parts) != 2:
            continue
        rel = parts[1]
        for nom in MANIFESTES:
            if (RACINE / rel / nom).is_file():
                chemins.append((rel, nom))
                break
    return sorted(chemins)


def _version(chemin):
    try:
        return json.loads((RACINE / chemin).read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def _exclusions():
    if not EXCLUSIONS.is_file():
        return set()
    gardees = set()
    for ligne in EXCLUSIONS.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if ligne and not ligne.startswith("#"):
            gardees.add(ligne)
    return gardees


def _etat(rel, nom_manifeste):
    """(ok, motif). ok=True si le journal est d'accord avec le manifeste."""
    mv = _version(f"{rel}/{nom_manifeste}")
    if mv is None:
        return False, f"{nom_manifeste} illisible ou sans version"
    meta = RACINE / rel / "meta.json"
    if not meta.is_file():
        return False, f"manifeste {mv}, aucun meta.json"
    try:
        d = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, f"manifeste {mv}, meta.json illisible"
    if not (d.get("changes") or d.get("changelog")):
        return False, f"manifeste {mv}, meta.json sans aucun historique"
    jv = d.get("version")
    if jv != mv:
        return False, f"manifeste {mv}, journal {jv}"
    return True, ""


def main():
    exclus = _exclusions()
    composants = _composants()
    rouges, perimees, vus = [], [], set()
    for rel, nom in composants:
        vus.add(rel)
        ok, motif = _etat(rel, nom)
        if rel in exclus:
            if ok:
                perimees.append(rel)
        elif not ok:
            rouges.append((rel, motif))

    # Une exclusion qui nomme un composant ABSENT n'est pas périmée : sur l'arbre dérivé,
    # les composants privés ne sont pas là. On ne la signale pas.
    if not rouges and not perimees:
        print(f"  ✓ {len(composants)} composant(s) : journal et manifeste au même numéro"
              + (f" ({len(exclus & vus)} exclusion(s) encore valide(s))" if exclus & vus else ""))
        return 0

    if rouges:
        print(f"\n  ✗ {len(rouges)} composant(s) dont le journal ne suit pas le manifeste :\n")
        for rel, motif in rouges:
            print(f"    {rel} — {motif}")
        print("\n  Le Catalogue lit la VERSION dans le manifeste et l'HISTORIQUE dans meta.json :")
        print("  un composant proposé en 0.15.2 avec un journal à 0.14.0 ne dit rien de ce qui")
        print("  a changé, et sa note de release part vide. Avancez meta.json (version + une")
        print("  section pour cette version) en même temps que le manifeste.")
    if perimees:
        print(f"\n  ✗ {len(perimees)} exclusion(s) PÉRIMÉE(S) dans {EXCLUSIONS.name} :\n")
        for rel in perimees:
            print(f"    {rel} — ce composant est d'accord avec lui-même, retirez sa ligne")
        print("\n  Cette liste ne peut que rétrécir : c'est ce qui l'empêche de s'installer.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
