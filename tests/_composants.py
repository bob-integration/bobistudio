# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Déclarer les composants qu'un test exige, et se SAUTER proprement s'ils manquent.

★ POURQUOI. Les plugins et services sont des dépôts indépendants : tous ne sont pas
présents dans toutes les copies. Le dépôt PUBLIC n'embarque que l'orchestrateur,
`hello_world` et `nmos` — les autres s'installent depuis la page Catalogue.

Un test qui exerce le tally du mixer ou la composition en tranches de `split` n'a alors
rien à mesurer. Il ne doit PAS échouer pour autant : un échec dit « quelque chose est
cassé », et ce n'est pas le cas. Constaté le 2026-09-01 — 23 tests en rouge sur le dépôt
public à sa première heure d'existence, aucun ne signalant un vrai défaut.

Un test rouge qui ne désigne aucun défaut est pire qu'un test absent : on apprend à
ignorer la couleur, et le jour où elle dit vrai, plus personne ne regarde.

Usage, en tête du test :

    from _composants import exiger
    exiger("plugins/split")                      # un seul
    exiger("services/tsl", "plugins/mixer")      # ou plusieurs
"""
import os
import pathlib
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent


def present(chemin):
    """Vrai si le composant est là ET peuplé (un sous-module non cloné laisse un dossier VIDE)."""
    p = RACINE / chemin
    if not p.is_dir():
        return False
    try:
        return any(os.scandir(p))
    except OSError:
        return False


def exiger(*chemins):
    """Sort en SUCCÈS avec un message si l'un des composants manque."""
    manquants = [c for c in chemins if not present(c)]
    if manquants:
        print(f"  ⊘ ignoré — composant(s) absent(s) de cette copie : {', '.join(manquants)}")
        print("     (installables depuis Réglages → Catalogue)")
        sys.exit(0)
