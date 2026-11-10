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

# ── 6-9. Les quatre autres sites du même motif (relevés le 2026-11-01, corrigés le 2026-11-08) ──
# Même machine fraîche (12 s), même question : le premier passage a-t-il lieu ?
from app import node_driver, docker_driver                                    # noqa: E402
from app.routes import images as _images                                      # noqa: E402

print("\n6. ★★ node_driver : l'alerte « TLS en clair » d'un nœud sort au premier refus")
_horloge(12.0)
node_driver._derniere_alerte_tls.clear()
_vrai_request, _vrai_base, _vrai_tls = node_driver._request, node_driver._agent_base, node_driver._tls_on
node_driver._request = lambda *a, **k: (False, node_driver._MARQUE_TLS_CLAIR + "refus")
node_driver._agent_base = lambda node: "https://192.0.2.1:9100"
node_driver._tls_on = lambda node: True
try:
    _avant = _n_alertes()
    node_driver._call({"id": 7, "name": "noeud-essai"}, "GET", "/v1/health")
    verifie(_n_alertes() == _avant + 1, "6a. ★★ monotonic=12 s → l'alerte TLS est ÉCRITE", _n_alertes())
    _horloge(12.0 + 30.0)
    _avant = _n_alertes()
    node_driver._call({"id": 7, "name": "noeud-essai"}, "GET", "/v1/health")
    verifie(_n_alertes() == _avant, "6b. 30 s plus tard → l'anti-rebond tient", _n_alertes())
finally:
    node_driver._request, node_driver._agent_base, node_driver._tls_on = _vrai_request, _vrai_base, _vrai_tls

print("\n7-8. docker_driver : réconciliation du pinning et du dimensionnement au premier passage")
for nom, fn, memo in (("pinning", docker_driver.reconcile_engine_pinning, docker_driver._reconcile_last),
                      ("dimensionnement", docker_driver.reconcile_engine_sizing, docker_driver._sizing_last)):
    _horloge(12.0)
    memo.clear()
    fn(424242)               # nœud inconnu : la fonction sort juste APRÈS l'anti-rebond
    verifie(424242 in memo, "%s : monotonic=12 s → le premier passage a LIEU" % nom, dict(memo))

print("\n9. images : le contrôle de dérive a lieu au premier passage")
_horloge(12.0)
_images._derive_vue.clear()
_vrai_attend = _images._node_expects_image
_images._node_expects_image = lambda which, node: False
# Since 2026-11-09 the inventory is read FIRST (an unreadable one ends the check silently):
# give it an answer, or this section would wait on a network timeout and measure nothing.
_vrai_inv = _images._node_image_inventory
_images._node_image_inventory = lambda node, force=False: ([], "")
try:
    r = _images.verifier_derive_images({"id": 9, "name": "n", "agent_url": "https://192.0.2.1:9100"})
    verifie(r == [], "9a. monotonic=12 s → contrôle FAIT (None = sauté par l'anti-rebond)", r)
    _horloge(12.0 + 60.0)
    r = _images.verifier_derive_images({"id": 9, "name": "n", "agent_url": "https://192.0.2.1:9100"})
    verifie(r is None, "9b. une minute plus tard → l'anti-rebond tient", r)
finally:
    _images._node_expects_image = _vrai_attend
    _images._node_image_inventory = _vrai_inv

time.monotonic = _vrai_monotonic
print()
if ko:
    print("ÉCHEC : %d contrôle(s)" % len(ko))
    for t in ko:
        print("   - %s" % t)
    sys.exit(1)
print("OK — la première alerte sort quelle que soit l'heure depuis le démarrage")
