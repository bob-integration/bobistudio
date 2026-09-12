#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Confronte les clés d'alerte UTILISÉES dans le code aux catalogues i18n.

Pourquoi cet outil : `i18n.t()` replie sur la clé brute quand elle manque, donc une clé oubliée
s'affiche comme `alert.deploy.detruit` — du texte plausible, dans une interface qui a l'air de
marcher. `db_add_alert` journalise déjà une erreur à l'écriture, mais seulement si le chemin est
emprunté ; ce contrôle-ci voit les clés qui n'ont encore jamais été déclenchées.

    ./venv/bin/python tools/verif_cles_alertes.py

Sortie : les clés manquantes par langue, et les clés du catalogue que plus personne n'émet.
Code de retour 1 s'il manque quelque chose — utilisable en contrôle avant commit.
"""
import io
import json
import os
import re
import string
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# On repère d'abord les APPELS, puis on ramasse les clés DANS la fenêtre de l'appel. Deux motifs
# naïfs ont été essayés et écartés :
#   · ancré sur le premier argument : un producteur sur deux choisit sa clé par une conditionnelle
#     (`"alert.x" if cause else "alert.y"`) et la seconde branche passait pour « sans producteur » ;
#   · tout littéral `alert.…`/`plugin.…` du fichier : ramasse `plugin.json` (un nom de fichier),
#     les exemples de docstrings, et les copies du dépôt dans les worktrees d'autres agents.
APPEL = re.compile(r"(?:db_add_alert|ajouter_alerte)\s*\(")
CLE = re.compile(r"""["'](alert\.[A-Za-z0-9_.]+)["']""")
FENETRE = 400          # de quoi couvrir un appel sur plusieurs lignes, pas le reste du fichier

# Les deux contrôles n'ont PAS besoin de la même précision, et les confondre les rend faux tous
# les deux :
#   · « clé manquante au catalogue » doit être STRICT — on ne veut pas alarmer sur un
#     `alert.foo` d'exemple dans une docstring. D'où la fenêtre autour d'un appel (MOTIF ci-dessus).
#   · « clé du catalogue sans producteur » doit être PERMISSIF — une clé peut parfaitement être
#     produite sans apparaître dans un appel : passée en `clef=` à un helper, rangée dans un tuple
#     `msg_on_i18n=("alert.rx.image_noire", {…})`, ou choisie par une variable calculée plus haut.
#     Exiger la fenêtre y produisait 31 fausses orphelines — un outil qui crie pour rien finit par
#     ne plus être lu.
# Fragments `alert.…` trouvés N'IMPORTE OÙ dans le source, quote fermante NON exigée : une clé
# sur six est COMPOSÉE (`f"alert.prep.controles_casses_{suffixe}"`, ou un suffixe `_sans_cible`
# ajouté par un helper), et seul son PRÉFIXE apparaît littéralement. Une clé de catalogue est donc
# tenue pour produite dès qu'un fragment du code en est un PRÉFIXE — sinon l'outil rapportait 50
# fausses orphelines, et un outil qui crie pour rien finit par ne plus être lu.
LITTERAL = re.compile(r"(alert\.[A-Za-z0-9_.]*)")
# `.claude` contient les worktrees d'autres agents : autant de COPIES du dépôt, qui feraient
# compter chaque clé plusieurs fois et signaleraient des fichiers qui ne sont pas les nôtres.
IGNORE = {".git", ".claude", "venv", "old", "node_modules", "__pycache__"}


def cles_du_code():
    """→ (clés vues dans un APPEL d'alerte, tous les littéraux `alert.*` du code)."""
    trouvees, litteraux = {}, set()
    for base, dirs, fichiers in os.walk(RACINE):
        dirs[:] = [d for d in dirs if d not in IGNORE]
        for f in fichiers:
            if not f.endswith(".py"):
                continue
            chemin = os.path.join(base, f)
            try:
                texte = io.open(chemin, encoding="utf-8").read()
            except Exception:
                continue
            for appel in APPEL.finditer(texte):
                for cle in CLE.findall(texte[appel.end():appel.end() + FENETRE]):
                    trouvees.setdefault(cle, []).append(os.path.relpath(chemin, RACINE))
            for cle in LITTERAL.findall(texte):
                litteraux.add(cle)
    return trouvees, litteraux


def catalogue(code):
    """Catalogue EFFECTIF : cœur + contributeurs, comme `i18n._file_catalog_for`.

    ⚠ Ne lire que `i18n/<code>.json` ne suffit plus. Depuis que plugins et services portent
    leurs propres libellés (migration du 2026-09-01), une alerte émise par un service a sa
    clé dans `services/<nom>/i18n/` — la chercher au seul catalogue du cœur la déclarerait
    MANQUANTE alors qu'elle est traduite. Un test qui crie au loup finit ignoré, et c'est
    alors les vraies clés oubliées qu'on ne voit plus.
    """
    fusion = {}
    for base in ("plugins", "services"):
        rep = os.path.join(RACINE, base)
        for nom in sorted(os.listdir(rep)) if os.path.isdir(rep) else []:
            f = os.path.join(rep, nom, "i18n", "%s.json" % code)
            if os.path.isfile(f):
                try:
                    fusion.update(json.load(io.open(f, encoding="utf-8")))
                except Exception as e:
                    print("catalogue %s illisible : %s" % (f, e))
    chemin = os.path.join(RACINE, "i18n", "%s.json" % code)
    try:
        fusion.update(json.load(io.open(chemin, encoding="utf-8")))   # le cœur prime
    except Exception as e:
        print("catalogue %s illisible : %s" % (chemin, e))
    return fusion


def champs_gabarit(texte):
    """Noms de paramètres exigés par un gabarit `str.format` ({fps:.1f} → fps)."""
    return {f for _, f, _, _ in string.Formatter().parse(texte or "") if f}


def asymetries(catalogues):
    """Clés dont les gabarits FR et EN n'exigent PAS les mêmes paramètres.

    Défaut muet s'il en est : `i18n.t()` rattrape l'échec de `.format` et rend le gabarit BRUT.
    Une clé dont la version anglaise réclame un paramètre que l'appelant ne passe pas afficherait
    donc « {h} : cadence non tenue » à l'écran, en anglais seulement, sans la moindre erreur —
    et personne ne s'en apercevrait tant que personne ne lit dans cette langue."""
    out = []
    fr, en = catalogues["fr"], catalogues["en"]
    for cle in sorted(fr):
        if not cle.startswith(("alert.", "plugin.")):
            continue
        a, b = champs_gabarit(fr[cle]), champs_gabarit(en.get(cle, ""))
        if a != b:
            out.append((cle, sorted(a - b), sorted(b - a)))
    return out


def main():
    utilisees, litteraux = cles_du_code()
    catalogues = {code: catalogue(code) for code in ("fr", "en")}
    defaut = False

    for code, cat in catalogues.items():
        manquantes = sorted(c for c in utilisees if c not in cat)
        if manquantes:
            defaut = True
            print("\n%s : %d clé(s) MANQUANTE(S)" % (code.upper(), len(manquantes)))
            for c in manquantes:
                print("  %-50s ← %s" % (c, ", ".join(sorted(set(utilisees[c])))))

    # Une clé de catalogue que plus aucun producteur n'émet n'est pas une erreur (elle a pu être
    # retirée d'un chemin de code), mais elle mérite d'être vue : c'est du catalogue mort.
    orphelines = sorted(c for c in catalogues["fr"]
                        if c.startswith("alert.") and c != "alert.repete"
                        and not any(c.startswith(f) for f in litteraux))
    # `plugin.*` n'est volontairement PAS contrôlé ici : ces clés sont émises par du code qui
    # tourne DANS les conteneurs (avis de plugins), pas par ce dépôt — leur absence de producteur
    # visible est normale, pas un signalement.
    if orphelines:
        print("\nCatalogue sans producteur (%d) :" % len(orphelines))
        for c in orphelines:
            print("  %s" % c)

    ecarts = asymetries(catalogues)
    if ecarts:
        defaut = True
        print("\n%d clé(s) dont les gabarits FR et EN n'exigent pas les mêmes paramètres :"
              % len(ecarts))
        for cle, fr_seul, en_seul in ecarts:
            print("  %-46s fr seul=%s  en seul=%s" % (cle, fr_seul, en_seul))

    print("\n%d clé(s) d'alerte utilisée(s) dans le code." % len(utilisees))
    return 1 if defaut else 0


if __name__ == "__main__":
    sys.exit(main())
