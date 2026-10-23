#!/usr/bin/env python3
"""Garde : le rappel GÉNÉRIQUE d'une mémoire de split accepte les deux formats.

DÉFAUT D'ORIGINE (relevé le 2026-10-20 en montant le poste « Technique ») : une mémoire au format
0.12 (`{boxes, global, bg}`, celui que crée l'instantané de l'UI du split) postée telle quelle en
`{"boxes": <le dict entier>}` par `recall_preset` → « rappel en échec » (le conteneur répond
« boxes requis »). La route `/split/memories/<id>/recall` savait lire les deux formats ; le rappel
générique — macros, workspace, Ember+ — non.

On exécute le VRAI `recall_preset` avec le conteneur doublé. Aucun réseau.
"""
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
ECHECS = []


def verifier(cond, libelle):
    print(("  ok   " if cond else "  ÉCHEC ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def main():
    import app.routes as R
    import app.addressing as A
    import requests
    postes = []

    class Rep:
        status_code = 200

    def post(url, json=None, timeout=None):
        postes.append(json)
        return Rep()
    requests.post = post
    A.get_container_ip = lambda v: "10.0.0.9"
    neuf = {"name": "Quatre", "value": {"boxes": [{"x": 0.25}], "global": {"light": 1}, "bg": {"c": "noir"}}}
    ancien = {"name": "Côte", "value": [{"x": 0.3}, {"x": 0.7}]}
    R.recall_presets = lambda vmid, t, kind=None: ({"endpoint": "/recall"}, [neuf, ancien])

    print("\n1. Mémoire au format 0.12 {boxes, global, bg}")
    ok, nom = R.recall_preset(1239, "split", 0, duration_ms=200)
    b = postes[-1]
    verifier(ok and nom == "Quatre", "rappel accepté (%s, %s)" % (ok, nom))
    verifier(b.get("boxes") == [{"x": 0.25}] and b.get("global") == {"light": 1} and b.get("bg") == {"c": "noir"},
             "★ boxes, global et bg postés séparément, comme la route du split (%s)" % b)
    verifier(b.get("duration_ms") == 200, "durée de transition transmise")

    print("\n2. Mémoire ancienne (liste de boîtes)")
    R.recall_preset(1239, "split", 1)
    verifier(postes[-1] == {"boxes": [{"x": 0.3}, {"x": 0.7}], "duration_ms": 0}, "★ inchangé pour l'ancien format")

    print()
    if ECHECS:
        print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  - " + e)
        sys.exit(1)
    print("TOUT PASSE")


if __name__ == "__main__":
    main()
