#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Le paquet de distribution ne doit JAMAIS emporter l'état de l'instance qui le construit.

★ CE QUI A FAILLI PARTIR. `static/` est embarqué en entier par `CORE_DIRS`, et `static/uploads/`
s'y trouve — or ce dossier est GITIGNORÉ : c'est ce que les utilisateurs de CETTE installation y
ont déposé. Sur le contrôleur de l'éditeur, 76 fichiers, 14 Mo, des captures de diagnostic
nommées d'après des machines de production. Un paquet construit depuis le dépôt de travail les
emportait dans une release PUBLIQUE. Constaté le 2026-09-03 à un clic de la publication.

Les releases publiques y échappaient PAR ACCIDENT DE MÉTHODE : elles sont construites depuis
l'arbre de publication, qui ne contient que du versionné. Un garde-fou qui dépend du répertoire
d'où l'on lance la commande n'en est pas un.

Deux mécanismes INDÉPENDANTS sont vérifiés ici, et c'est le point du test :
  1. `EXCLUDE_PATHS` écarte le chemin pendant la construction ;
  2. `SECRET_PATTERNS` fait REFUSER le build si le premier a été défait.

Un seul des deux suffit à protéger — mais un seul des deux peut être supprimé par une refonte
sans que personne ne s'en aperçoive. On teste donc que chacun mord SEUL.

⚠ Ne construit aucun zip : le build réel prend des minutes et exige les sous-modules. On exerce
les deux fonctions de décision, qui sont toute la logique.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import builder                                            # noqa: E402

ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def main():
    print("\n── 1. l'exclusion écarte l'état d'instance pendant la construction")
    verifier(builder._excluded("static/uploads/capture.png"),
             "static/uploads/<fichier> est exclu")
    verifier(builder._excluded("static/uploads/fonts/abcdef.ttf"),
             "et récursivement (sous-dossiers compris)")

    print("\n── 2. ...sans emporter le reste de static/, qui est du code")
    for garde in ("static/scripts.js", "static/css/base.css", "static/js/motdepasse.js"):
        verifier(not builder._excluded(garde), f"{garde} reste embarqué")

    print("\n── 3. l'exclusion vise un CHEMIN, pas un nom de dossier")
    # Un plugin a le droit de porter un dossier « uploads » : l'exclure aussi serait un dégât
    # collatéral silencieux, et le plugin s'installerait amputé.
    verifier(not builder._excluded("plugins/exemple/uploads/ressource.png"),
             "un dossier « uploads » d'un plugin n'est PAS écarté")

    print("\n── 4. le second mécanisme refuse le build, si le premier tombe")
    verifier(builder._is_secret("static/uploads/capture.png"),
             "★ static/uploads est aussi traité comme une fuite (build REFUSÉ)")
    verifier(builder._is_secret("config_local.py") and builder._is_secret("x/db_bobistudio.db"),
             "les fuites historiques restent couvertes (config_local, .db)")
    verifier(not builder._is_secret("static/css/base.css"),
             "et le code ordinaire ne déclenche rien")

    print()
    if ECHECS:
        print(f"✗ {len(ECHECS)} échec(s)")
        return 1
    print("✓ tout est vert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
