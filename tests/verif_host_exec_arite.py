#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Tout site d'appel à `node_driver.host_exec` dépaquette TROIS valeurs — (rc, stdout, stderr).

★ POURQUOI CE BANC EXISTE (2026-10-27, trouvé en production par la session Z8).
`appliquer_shim` faisait `ok, res = node_driver.host_exec(...)`. La fonction rend un TRIPLET —
même contrat que `host_ops.ssh_run` — donc l'appel levait `ValueError: too many values to unpack`
AU PREMIER APPEL RÉEL : réponse 500 en HTML, et l'opérateur voyait « SyntaxError: Unexpected
token '<' ». Un site d'appel ANTÉRIEUR au chantier (création de VLAN par-nœud) portait le même
défaut depuis des mois : tout nœud configuré avec un VLAN taggé plantait là.

★ ET POURQUOI AUCUN BANC NE L'AVAIT VU. Tous doublaient `node_driver` — chacun avec une doublure
rendant ce que SON AUTEUR croyait du contrat. Une doublure ne peut pas contredire son auteur :
elle propage son erreur au lieu de la révéler. Muter le code n'y change rien, la mutation ne
touche pas les doublures. Le seul instrument qui attrape ça est STATIQUE : comparer les sites
d'appel à la signature RÉELLE, sans exécuter. Même famille que
[[garde-avec-donnees-inventees-ne-mesure-rien]] : la garde partageait la cécité du code.

Portée : TOUT le dépôt (app/, services/), pas une liste de fichiers — un site d'appel écrit
demain doit tomber dedans sans que personne n'y pense.
"""
import ast
import glob
import inspect
import io
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ─── 1. Combien de valeurs la fonction rend-elle VRAIMENT ? ─────────────────────────────────
from app import node_driver                                                   # noqa: E402

print("\n1. Le contrat, lu dans la fonction elle-même")
# `textwrap.dedent` et non `cleandoc` : il faut retirer l'indentation de TOUTES les lignes de
# façon homogène, pas seulement nettoyer un docstring — sinon le corps reste décalé et ne compile
# pas. (Ici la fonction est au niveau module, mais le banc doit tenir si elle devient méthode.)
import textwrap                                                               # noqa: E402

src_fn = textwrap.dedent(inspect.getsource(node_driver.host_exec))
arbre_fn = ast.parse(src_fn)
retours = [n for n in ast.walk(arbre_fn) if isinstance(n, ast.Return) and n.value is not None]
arites = {len(n.value.elts) for n in retours if isinstance(n.value, ast.Tuple)}
verifie(arites == {3},
        "1a. `host_exec` rend des triplets, et rien d'autre (%s)" % sorted(arites),
        str(sorted(arites)))
verifie(len(retours) >= 2,
        "1b. ses deux chemins (agent injoignable / réponse) rendent la même forme — c'est ce qui "
        "rend l'arité vérifiable", str(len(retours)))

# `ssh_run`, dont host_exec reprend le contrat : les deux doivent rester d'accord, sinon un
# appelant qui bascule de l'un à l'autre (host_ops.ssh_run → node_driver.host_exec) casse.
from app import host_ops                                                      # noqa: E402

src_ssh = textwrap.dedent(inspect.getsource(host_ops.ssh_run))
ar_ssh = {len(n.value.elts) for n in ast.walk(ast.parse(src_ssh))
          if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)}
verifie(ar_ssh == {3},
        "1c. `host_ops.ssh_run` rend la même forme (host_exec dit reprendre son contrat)",
        str(sorted(ar_ssh)))


# ─── 2. Tous les sites d'appel du dépôt ─────────────────────────────────────────────────────
print("\n2. Les sites d'appel, dans TOUT le dépôt")
FICHIERS = sorted(set(glob.glob(os.path.join(RACINE, "app", "**", "*.py"), recursive=True))
                  | set(glob.glob(os.path.join(RACINE, "services", "*", "*.py")))
                  | set(glob.glob(os.path.join(RACINE, "tools", "*.py"))))
verifie(len(FICHIERS) > 50, "2a. les fichiers du dépôt sont bien balayés", str(len(FICHIERS)))

sites, fautes = 0, []
for f in FICHIERS:
    try:
        arbre = ast.parse(io.open(f, encoding="utf-8").read())
    except Exception as e:
        fautes.append("%s illisible : %s" % (os.path.relpath(f, RACINE), e))
        continue
    for n in ast.walk(arbre):
        if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Call):
            continue
        nom = getattr(n.value.func, "attr", None) or getattr(n.value.func, "id", None)
        if nom != "host_exec":
            continue
        sites += 1
        cible = n.targets[0]
        # Un appel dont le résultat n'est PAS dépaqueté (affecté tel quel, ou ignoré) est hors
        # sujet : c'est le dépaquetage qui lève, pas l'appel.
        if isinstance(cible, ast.Tuple) and len(cible.elts) != 3:
            fautes.append("%s:%d dépaquette %d valeur(s) au lieu de 3"
                          % (os.path.relpath(f, RACINE), n.lineno, len(cible.elts)))

verifie(sites >= 20,
        "2b. le balayage TROUVE les sites d'appel (sinon ce banc est vert en ne voyant rien)",
        "%d site(s)" % sites)
verifie(not fautes, "2c. ★ aucun site ne dépaquette autre chose que 3 valeurs",
        " | ".join(fautes[:4]))


# ─── 3. La même exigence pour ssh_run, dont le contrat est identique ────────────────────────
print("\n3. Et pour `ssh_run`, même contrat donc même piège")
sites_s, fautes_s = 0, []
for f in FICHIERS:
    try:
        arbre = ast.parse(io.open(f, encoding="utf-8").read())
    except Exception:
        continue
    for n in ast.walk(arbre):
        if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Call):
            continue
        nom = getattr(n.value.func, "attr", None) or getattr(n.value.func, "id", None)
        if nom != "ssh_run":
            continue
        sites_s += 1
        cible = n.targets[0]
        if isinstance(cible, ast.Tuple) and len(cible.elts) != 3:
            fautes_s.append("%s:%d dépaquette %d valeur(s)"
                            % (os.path.relpath(f, RACINE), n.lineno, len(cible.elts)))
verifie(sites_s >= 5, "3a. les sites de `ssh_run` sont trouvés", "%d site(s)" % sites_s)
verifie(not fautes_s, "3b. aucun n'en dépaquette un autre nombre", " | ".join(fautes_s[:4]))


print("\n" + ("─" * 70))
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ arité de host_exec / ssh_run : %d + %d site(s) d'appel conformes" % (sites, sites_s))
sys.exit(0)
