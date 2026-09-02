#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de COHÉRENCE entre le manifeste d'un composant et son journal de versions.
#
# ★ CE QUE ÇA PROTÈGE. Un composant porte sa version à DEUX endroits : le manifeste
# (`plugin.json` / `manifest.json`) et le journal (`meta.json`). Rien n'a jamais vérifié qu'ils
# s'accordent — et le 2026-09-02, ONZE composants divergeaient.
#
# LE MANIFESTE FAIT FOI, sans ambiguïté : c'est lui que lit le registre, lui qui est estampillé
# dans `params.plugin_version` au déploiement, lui que compare la garde d'application à chaud, et
# lui que le catalogue annonce. `meta.json` n'est que de la documentation.
#
# ⚠ LES DEUX SENS N'ONT PAS LA MÊME GRAVITÉ, et c'est pourquoi ce banc les distingue :
#
#   · JOURNAL EN RETARD (le cas courant) : on a bumpé le manifeste sans écrire l'entrée. La
#     documentation manque, le produit se comporte correctement.
#   · MANIFESTE EN RETARD (2110_io : 0.91.0 contre 0.106.0 au journal) : BEAUCOUP plus grave.
#     Tout déploiement s'estampille d'un numéro périmé, la garde de hot-apply compare un nombre
#     qui n'a plus bougé, et un tag posé d'après le journal rend une version INVISIBLE au
#     catalogue — qui affiche « à jour » sans que rien ne le signale.
#
#   $ ./venv/bin/python tests/verif_versions_manifeste_journal.py
import io
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app.version import analyser                                     # noqa: E402

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


def _lire(p):
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return None


def composants():
    """[(nom, chemin_manifeste, chemin_journal)] — ceux qui portent les DEUX fichiers."""
    out = []
    for famille, nom_man in (("plugins", "plugin.json"), ("services", "manifest.json")):
        base = os.path.join(RACINE, famille)
        if not os.path.isdir(base):
            continue
        for d in sorted(os.listdir(base)):
            if d.startswith(("_", ".")):
                continue          # runtimes partagés, pas des composants
            rep = os.path.join(base, d)
            man, jour = os.path.join(rep, nom_man), os.path.join(rep, "meta.json")
            if os.path.isfile(man) and os.path.isfile(jour):
                out.append(("%s/%s" % (famille, d), man, jour))
    return out


print("Cohérence manifeste ↔ journal de versions\n")

liste = composants()
# ★ TÉMOIN. Sans lui, un banc qui ne trouverait AUCUN composant — chemin faux, dossier renommé —
# serait vert en n'ayant rien vérifié. C'est le mode de panne d'un contrôle par balayage.
controle("★★★ des composants sont bien trouvés", len(liste) >= 5,
         "un balayage qui ne trouve rien passe tous ses contrôles sans en faire aucun. "
         "Trouvé %d" % len(liste))

graves, legers = [], []
for nom, pman, pjour in liste:
    man, jour = _lire(pman), _lire(pjour)
    if not man or not jour:
        continue
    vm, vj = man.get("version"), jour.get("version")
    if not vm or not vj or vm == vj:
        continue
    am, aj = analyser(vm), analyser(vj)
    if am is None or aj is None:
        legers.append((nom, vm, vj, "non comparable"))
    elif am < aj:
        graves.append((nom, vm, vj))
    else:
        legers.append((nom, vm, vj, "journal en retard"))

controle("★★★ aucun MANIFESTE en retard sur son journal", not graves,
         "le manifeste fait foi : un numéro périmé s'estampille à chaque déploiement, la garde "
         "de hot-apply compare un nombre figé, et un tag posé d'après le journal rend la version "
         "INVISIBLE au catalogue. " + "; ".join("%s manifeste=%s journal=%s" % g for g in graves))

# ★ LE JOURNAL EN RETARD N'EST PAS UN ÉCHEC, et c'est un choix. `meta.json:version` est TOUJOURS
# le numéro de sa dernière ENTRÉE — ce n'est pas « la version du composant », c'est « la dernière
# version documentée ». Un journal en retard veut donc dire « bumpé sans écrire l'entrée » : une
# dette de documentation, pas un défaut de produit. Faire échouer la CI dessus la rendrait rouge
# dès sa naissance sur dix composants, et on apprendrait à l'ignorer — ce qui coûterait le seul
# contrôle qui compte, juste au-dessus. On le SIGNALE, on ne le bloque pas.
if legers:
    print("\n  ⚠ %d composant(s) dont le journal est en retard sur le manifeste — dette de"
          % len(legers))
    print("    documentation, pas un défaut. Écrire l'entrée manquante, pas changer le numéro :")
    for nom, vm, vj, motif in legers:
        print("      · %-26s manifeste=%-9s journal=%-9s (%s)" % (nom, vm, vj, motif))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
