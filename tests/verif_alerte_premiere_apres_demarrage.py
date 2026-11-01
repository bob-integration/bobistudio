#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""La PREMIÈRE alerte d'un motif sort, quelle que soit l'heure depuis le démarrage.

★ LE DÉFAUT (trouvé le 2026-11-01). L'anti-rebond de `core_pool` s'écrivait
`now - _alerte_last.get(k, 0.0) < _ALERTE_THROTTLE_S`. Clé absente → défaut 0.0 → la condition
devenait `time.monotonic() < 300`. Or `monotonic()` compte DEPUIS LE DÉMARRAGE DE LA MACHINE :
sur un contrôleur fraîchement redémarré, la PREMIÈRE alerte de chaque motif était journalisée
« throttlée » et jetée pendant cinq minutes.

★ POURQUOI C'EST GRAVE, ET POURQUOI PERSONNE NE L'A VU. Ces cinq minutes sont exactement la
fenêtre où les nœuds remontent et où les conteneurs se replacent — donc celle où les alertes de
placement servent le plus. Et le défaut est INVISIBLE sur une machine en service : `monotonic()`
y vaut des centaines de milliers de secondes. Il n'est sorti que sur un exécuteur de CI neuf,
où `verif_dedie_frere_dedie` échouait seul, avec une table d'alertes vide et un throttle vide —
deux faits incompatibles avec une alerte « throttlée », ce qui a mis sur la voie.

★ « JAMAIS ÉMIS » N'EST PAS « ÉMIS À L'INSTANT 0 ». C'est la leçon, et elle vaut pour tout
anti-rebond : une date par défaut fait d'une absence une date, et 0.0 est une date valide sur une
horloge relative au démarrage.

    ./venv/bin/python tests/verif_alerte_premiere_apres_demarrage.py
"""
import os
import sys
import tempfile
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_alerte_demarrage_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import core_pool                                                     # noqa: E402

ko = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        ko.append(titre)


def _n_alertes():
    with database.get_db() as db:
        return db.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]


_vrai_monotonic = time.monotonic


def _horloge(valeur):
    """Fige `time.monotonic` — `_alerter_throttle` fait `import time` à chaque appel."""
    time.monotonic = lambda: valeur


print("\n1. ★★ Machine FRAÎCHEMENT démarrée : la première alerte sort")
# 12 secondes depuis le boot : très en dessous de la fenêtre de 300 s. C'est le cas qui échouait.
_horloge(12.0)
core_pool._alerte_last.clear()
_avant = _n_alertes()
core_pool._alerter_throttle(1, "motif_essai", "message d'essai", "warning")
verifie(_n_alertes() == _avant + 1,
        "1a. ★★ monotonic=12 s, clé absente → l'alerte est ÉCRITE", (_avant, _n_alertes()))
verifie(("1", "motif_essai") in core_pool._alerte_last,
        "1b. et la clé est armée pour la suite", dict(core_pool._alerte_last))

print("\n2. L'anti-rebond fonctionne toujours")
_horloge(12.0 + 10.0)
_avant = _n_alertes()
core_pool._alerter_throttle(1, "motif_essai", "message d'essai", "warning")
verifie(_n_alertes() == _avant, "2a. 10 s plus tard, dans la fenêtre → rien n'est écrit", _n_alertes())

print("\n3. Et il relâche après la fenêtre")
# ⚠ TEXTE DIFFÉRENT, À DESSEIN. `db_add_alert` a SON PROPRE anti-rebond, en base
# (`alert_episodes`, par signature de message) : réémettre le même texte serait légitimement
# regroupé, et on mesurerait ce second mécanisme en croyant mesurer le throttle. Mon premier
# jet faisait exactement ça et sortait rouge — le produit avait raison, pas le test.
_horloge(12.0 + core_pool._ALERTE_THROTTLE_S + 1.0)
_avant = _n_alertes()
core_pool._alerter_throttle(1, "motif_essai", "message d'essai NUMÉRO DEUX", "warning")
verifie(_n_alertes() == _avant + 1, "3a. au-delà de la fenêtre → l'alerte repart", _n_alertes())

print("\n4. Un AUTRE motif n'est pas muselé par le premier")
_horloge(12.0 + core_pool._ALERTE_THROTTLE_S + 2.0)
_avant = _n_alertes()
core_pool._alerter_throttle(1, "autre_motif", "autre message", "warning")
verifie(_n_alertes() == _avant + 1, "4a. motif distinct, clé distincte → écrit", _n_alertes())

print("\n5. Machine en service depuis des jours : comportement inchangé")
_horloge(1_200_000.0)
core_pool._alerte_last.clear()
_avant = _n_alertes()
core_pool._alerter_throttle(2, "motif_essai", "message d'essai", "warning")
verifie(_n_alertes() == _avant + 1, "5a. témoin : le cas qui marchait marche toujours", _n_alertes())

time.monotonic = _vrai_monotonic
print()
if ko:
    print("ÉCHEC : %d contrôle(s)" % len(ko))
    for t in ko:
        print("   - %s" % t)
    sys.exit(1)
print("OK — la première alerte sort quelle que soit l'heure depuis le démarrage")
