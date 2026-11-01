#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Garde de RÉENTRANCE du registre des services du cœur (`app/core_plugins.scan`).
#
# POURQUOI (2026-10-25). `scan()` importe chaque service, et un service peut appeler `scan()`
# depuis son propre import. Jusqu'ici la fonction publiait `_registry = {}` AVANT de le remplir :
# l'appelant recevait donc un dict qui GROSSISSAIT sous ses pieds. Au premier run vert de la CI
# (run 40187687253, processus NEUF), le simple `import main` mourait sur
# `RuntimeError: dictionary changed size during iteration` dans `register_all_routes` — donc
# tout le produit mort au boot, et pas seulement un test. Invisible en local, où le cache est
# déjà peuplé par un import antérieur quand ce chemin est atteint : c'est exactement le genre de
# panne qu'aucune passe locale ne peut voir.
#
# Ce que la garde verrouille :
#   (1) une réentrance pendant le scan ne lève RIEN et ne récursionne PAS sans fond — elle reçoit
#       le registre partiel, jamais `None` (rendre `_registry` à la fin SANS registre partiel
#       visible donnerait une récursion sans fond : c'est l'erreur symétrique) ;
#   (2) le registre n'est publié qu'une fois COMPLET : ce que voit un appelant extérieur ne
#       change plus de taille ;
#   (3) ajouter une entrée pendant que l'appelant itère ne lève pas (les boucles travaillent sur
#       un instantané) ;
#   (4) un scan qui part en exception ne laisse pas l'état « scan en cours » posé à vie — sinon
#       tous les appels suivants rendraient un registre partiel, en silence ;
#   (5) `reload()` remet les deux à zéro ;
#   (6) `register_all_routes` traverse le vrai registre sans lever.
#
# Aucun réseau, aucune base : base jetable posée avant le premier accès, doublures sur le module.
#
#     ./venv/bin/python tests/verif_core_plugins_scan_reentrant.py
import os
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)

# Base jetable AVANT tout accès : la connexion est mémorisée par thread, surcharger DB_PATH
# après coup n'isole plus rien.
import tempfile                                          # noqa: E402
from app import config as _cfg, database as _dbm         # noqa: E402
_cfg.DB_PATH = _dbm.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif-scan-"), "principale.db")
# ⚠ On NE touche PAS `PRODUCTION_DB_PATH` : il doit continuer de désigner la VRAIE base de
# production. Le poser sur la base jetable la ferait prendre POUR la production par la
# garde, qui — le fichier n'existant pas encore — la remplacerait par une autre base
# jetable à elle : le test écrirait alors ailleurs que là où il croit. Mesuré le 25/10.
_dbm._tls.__dict__.pop("conn", None)
_dbm.init_db()

import app.core_plugins as cp                            # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


def _invalider():
    """Vide le cache SANS re-scanner. `reload()` ne convient pas ici : il invalide puis
    re-scanne aussitôt (c'est sa raison d'être), si bien que le cache est déjà repeuplé quand on
    l'interroge — mon premier jet de cette garde s'y est trompé et croyait mesurer un scan neuf
    alors qu'il lisait le cache."""
    cp._registry = None
    cp._scan_en_cours = None


# ── (1) et (2) réentrance pendant le scan ───────────────────────────────────────────
_invalider()
_vrai_import = cp.importlib.import_module
vu = {"appels": 0, "none": 0, "tailles": [], "leve": None, "publie_tot": 0}


def _import_qui_reentre(nom, *a, **k):
    """Simule un service qui appelle `scan()` depuis son propre import, et ITÈRE dessus."""
    vu["appels"] += 1
    # ★ LE critère qui distingue le correctif de la régression : TANT QUE le scan tourne,
    # `_registry` doit rester None. S'il est déjà posé ici, c'est qu'un registre PARTIEL est
    # publié — et c'est exactement le bug de 2026-10-25, qu'un appelant extérieur verrait
    # grossir sous ses pieds. Sans ce contrôle la garde ne mordait pas : vérifié en remettant
    # la publication précoce, elle restait verte.
    if cp._registry is not None:
        vu["publie_tot"] += 1
    try:
        reg = cp.scan()
        if reg is None:
            vu["none"] += 1
        else:
            vu["tailles"].append(len(reg))
            for _ in list(reg.values()):      # un appelant réel itère
                pass
    except Exception as e:                    # noqa: BLE001 — on veut le TYPE exact au rapport
        vu["leve"] = "%s: %s" % (type(e).__name__, e)
    return _vrai_import(nom, *a, **k)


cp.importlib.import_module = _import_qui_reentre
try:
    registre = cp.scan()
except RecursionError as e:
    registre = None
    vu["leve"] = "RecursionError: %s" % e
finally:
    cp.importlib.import_module = _vrai_import

ok(vu["appels"] > 0, "le harnais a bien intercepté des imports de service",
   "aucun import observé : le dossier services/ est-il vide ?")
ok(vu["leve"] is None, "(1) une réentrance pendant le scan ne lève rien", str(vu["leve"]))
ok(vu["none"] == 0, "(1) une réentrance ne reçoit JAMAIS None (pas de récursion sans fond)",
   "%d appels ont reçu None" % vu["none"])
ok(vu["publie_tot"] == 0,
   "(2) ★ `_registry` reste None PENDANT le scan : aucun registre partiel n'est publié",
   "%d appels ont vu un `_registry` déjà posé — un appelant extérieur le verrait grossir "
   "sous ses pieds (RuntimeError: dictionary changed size during iteration)" % vu["publie_tot"])
ok(isinstance(registre, dict) and registre, "(2) le scan rend un registre non vide",
   str(type(registre)))
ok(cp._scan_en_cours is None, "(2) l'état « scan en cours » est retombé après le scan",
   str(cp._scan_en_cours))
# Ce que voit l'appelant EXTÉRIEUR ne doit plus bouger : il reçoit le registre complet.
ok(cp.scan() is registre, "(2) le registre publié est stable d'un appel à l'autre")

# ── (3) ajouter une entrée pendant que l'appelant itère ─────────────────────────────
# Sans instantané (`list(...)`), muter le dict pendant la boucle lève. Les appelants du produit
# itèrent sur un instantané : on vérifie que le motif qu'ils emploient supporte la mutation.
leve = None
try:
    for _ in list(cp.scan().values()):
        cp.scan()["_sonde_%d" % len(cp.scan())] = {"manifest": {"id": "x"}, "module": None,
                                                   "dir": ""}
except RuntimeError as e:
    leve = str(e)
finally:
    for cle in [k for k in cp.scan() if str(k).startswith("_sonde_")]:
        cp.scan().pop(cle, None)
ok(leve is None, "(3) muter le registre pendant une boucle instantanée ne lève pas", str(leve))

# ── (4) un scan qui part en exception ne laisse pas l'état posé ─────────────────────
_invalider()
_vrai_listdir = os.listdir


def _listdir_qui_leve(chemin, *a, **k):
    if os.path.realpath(chemin) == os.path.realpath(cp.SERVICES_DIR):
        raise OSError("panne simulée de listdir")
    return _vrai_listdir(chemin, *a, **k)


os.listdir = _listdir_qui_leve
try:
    cp.scan()
    monte = False
except OSError:
    monte = True
finally:
    os.listdir = _vrai_listdir
ok(monte, "(4) l'exception du scan n'est pas avalée (elle remonte)")
ok(cp._scan_en_cours is None,
   "(4) …et l'état « scan en cours » est quand même retombé (pas de registre partiel à vie)",
   str(cp._scan_en_cours))

# ── (5) reload() : il invalide PUIS re-scanne, et ne laisse aucun état intermédiaire ─
# `reload()` ne rend donc pas le cache à None — il le remplace par un registre neuf et complet.
# Ce qui compte pour nous, c'est qu'il ne laisse pas « scan en cours » posé derrière lui.
cp.reload()
ok(isinstance(cp._registry, dict) and cp._registry,
   "(5) reload() re-scanne et republie un registre complet", repr(type(cp._registry)))
ok(cp._scan_en_cours is None,
   "(5) …sans laisser l'état « scan en cours » posé", repr(cp._scan_en_cours))
_invalider()
ok(cp._registry is None, "(5) le cache est bien invalidable (scan neuf possible)")

# ── (6) le vrai chemin de boot ne lève pas ─────────────────────────────────────────
# `register_all_routes` est précisément la fonction qui mourait au boot en CI.
try:
    from flask import Blueprint
    cp.register_all_routes(Blueprint("sonde_scan_reentrant", __name__))
    ok(True, "(6) register_all_routes traverse le registre sans lever")
except Exception as e:                        # noqa: BLE001
    ok(False, "(6) register_all_routes traverse le registre sans lever",
       "%s: %s" % (type(e).__name__, e))

# ── le code source dit-il encore ce qu'il faut ? ───────────────────────────────────
# Garde de forme, visant le SITE d'écriture : publier le registre avant de l'avoir rempli est
# précisément la régression de 2026-10-25, et elle ne se voit à l'exécution que sur un
# processus neuf dont l'ordre d'import réveille la réentrance.
src = open(os.path.join(_R, "app", "core_plugins.py"), encoding="utf-8").read()
corps = src.split("def scan(")[1].split("\ndef ")[0]
ok("_registry = {}" not in corps,
   "le scan ne publie PAS un registre vide dans `_registry` avant de le remplir",
   "`_registry = {}` est réapparu dans scan()")
ok("_scan_en_cours = {}" in corps,
   "le scan construit dans un registre séparé")

print()
print("ÉCHEC : %d" % len(ko) if ko else "TOUT VERT")
sys.exit(1 if ko else 0)
