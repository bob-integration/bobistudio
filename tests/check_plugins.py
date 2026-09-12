***REMOVED***!/usr/bin/env python3
***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** check_plugins.py — garde-fou CI du registre de plugins.
***REMOVED***
***REMOVED*** Importe app.plugins (ce qui déclenche le _scan() au chargement du module) puis
***REMOVED*** vérifie que AUCUN plugin n'a été écarté (SCAN_ERRORS vide). Un plugin est écarté
***REMOVED*** quand : manifeste JSON invalide, clés obligatoires manquantes, script_template
***REMOVED*** introuvable, ou accolade littérale non doublée dans le template str.format
***REMOVED*** (le piège classique documenté dans CLAUDE.md).
***REMOVED***
***REMOVED*** Exit 0 si tout est chargé, exit 1 si au moins un plugin est en erreur.
***REMOVED***
***REMOVED*** Usage : ./venv/bin/python tools/check_plugins.py

import os
import sys

***REMOVED*** Racine du projet = parent de tools/ ; on l'ajoute au path pour trouver le package app.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import plugins  ***REMOVED*** noqa: E402  (le _scan() tourne à l'import)

***REMOVED*** Rescan explicite pour être robuste quel que soit l'ordre d'import.
plugins.reload()

registry = plugins.REGISTRY
errors = plugins.scan_errors()

print(f"plugins chargés : {len(registry)}")
for type_, m in sorted(registry.items()):
    print(f"  OK    {type_:<20} v{m.get('version', '?')}")

if errors:
    print(f"\nplugins ÉCARTÉS : {len(errors)}", file=sys.stderr)
    for name, reason in sorted(errors.items()):
        print(f"  ERREUR {name:<20} {reason}", file=sys.stderr)
    print("\nÉchec : au moins un plugin n'a pas été chargé "
          "(voir raisons ci-dessus — souvent une accolade non doublée).",
          file=sys.stderr)
    sys.exit(1)

if not registry:
    print("\nÉchec : aucun plugin chargé — dossier plugins/ vide ou sous-modules "
          "non initialisés ?", file=sys.stderr)
    sys.exit(1)

print("\nOK : tous les plugins présents ont été chargés sans erreur.")
sys.exit(0)
