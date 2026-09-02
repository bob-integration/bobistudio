#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc des LIBELLÉS CUITS du multiview (plugins/multiview/hooks.py).
#
# Le conteneur ne peut pas lire `source_labels` : le hook embarque les libellés par fenêtre au
# déploiement, et `%src_labelN%` les lit dans la config. C'est une PHOTO, avec ce que ça implique.
#
# ★ CE QUE CE BANC PROTÈGE. Le hook ne posait les libellés QUE si la nouvelle source en avait
# (`if _r:`). Une fenêtre basculée sur un flux sans ligne de libellé gardait donc ceux de la
# source PRÉCÉDENTE — et comme le résultat est réécrit dans `params`, la valeur périmée se
# repersistait à chaque déploiement, si bien que redéployer ne la chassait pas.
#
# C'est la troisième occurrence de la même faute dans cette chaîne : ne rien savoir de neuf n'est
# pas une raison de laisser en place l'ancien. Constaté en production le 2026-09-01 sur PiP3, qui
# affichait « Mire Externe » alors que sa source était le Clean du mélangeur.
#
#   $ ./venv/bin/python tests/verif_multiview_libelles_cuits.py
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


print("Multiview — libellés cuits au déploiement\n")

import importlib.util                                                # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "_mv_hooks", os.path.join(RACINE, "plugins", "multiview", "hooks.py"))
hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hooks)

import app.database as db                                            # noqa: E402

def _bake(flux, table):
    """Passe `flux_config` par le hook et rend la config telle qu'elle serait déployée.

    ⚠ `before_deploy(params, context)` — params EN PREMIER, et il RETOURNE une copie. Ma première
    version passait le conteneur en premier : l'arité était bonne, donc aucune erreur levée, et
    le banc a rendu quatre échecs qui n'accusaient que lui. Un banc qui se trompe d'appel n'est
    pas un banc rouge, c'est un banc muet dans l'autre sens."""
    db.db_get_source_labels = lambda: list(table)
    out = hooks.before_deploy({"flux_config": [dict(f) for f in flux]},
                              {"hostname": "mv", "settings": {}})
    return out["flux_config"]


import inspect                                                       # noqa: E402
controle("★ le hook de déploiement a bien la signature attendue",
         list(inspect.signature(hooks.before_deploy).parameters) == ["params", "context"],
         "obtenu %s" % list(inspect.signature(hooks.before_deploy).parameters))


T = [{"shm": "mixer_clean", "label_2": "Mélangeur Clean", "projet": ""}]

# ── 1. Le cas nominal ────────────────────────────
out = _bake([{"path": "/dev/shm/mixer_clean", "name": "PiP 3"}], T)
controle("★ la source qui a un libellé le reçoit",
         (out[0].get("labels") or {}).get("2") == "Mélangeur Clean",
         "obtenu %r" % (out[0].get("labels")))

# ── 2. LE DÉFAUT ─────────────────────────────
out = _bake([{"path": "/dev/shm/flux_sans_ligne", "name": "PiP 3",
              "labels": {"2": "Mire Externe"}}], T)
controle("★★★ une source SANS ligne de libellé n'hérite pas de celui d'avant",
         (out[0].get("labels") or {}).get("2") == "",
         "★ C'EST LA PANNE PiP3 du 2026-09-01. Le `if _r:` ne remplaçait qu'en cas de trouvaille, "
         "et le résultat étant réécrit dans `params`, la valeur périmée se repersistait à chaque "
         "déploiement — redéployer ne la chassait donc pas. Obtenu %r" % (out[0].get("labels")))

controle("★★ ...et le repli est le NOM DE LA FENÊTRE, pas du vide à l'écran",
         (out[0].get("name") or "") == "PiP 3",
         "`_src_label` retombe sur `cfg['name']` quand le niveau est vide : le hook ne doit pas "
         "toucher au nom choisi par l'utilisateur")

# ── 3. La table VIDE ne doit pas figer les libellés non plus ─────────
out = _bake([{"path": "/dev/shm/mixer_clean", "name": "PiP 3",
              "labels": {"2": "Mire Externe"}}], [])
controle("★★★ une table de libellés VIDE efface aussi",
         (out[0].get("labels") or {}).get("2") == "",
         "le hook sautait tout le bloc quand la table était vide (`and _lbl`) : sur une "
         "installation dont on vient d'effacer les libellés, tous les murs auraient gardé les "
         "leurs indéfiniment. Obtenu %r" % (out[0].get("labels")))

# ── 4. Ce que le hook ne doit PAS toucher ───────────────────
out = _bake([{"path": "/dev/shm/mixer_clean", "name": "Mon titre à moi",
              "tally_red": True}], T)
controle("★★ le hook ne touche ni au nom ni aux réglages de la fenêtre",
         out[0].get("name") == "Mon titre à moi" and out[0].get("tally_red") is True,
         "obtenu %r" % out[0])

# ── 5. Toutes les colonnes sont posées, pas seulement celles remplies ────
out = _bake([{"path": "/dev/shm/mixer_clean", "name": "PiP 3"}], T)
lb = out[0].get("labels") or {}
controle("★ les huit colonnes (2→9) sont posées, vides comprises",
         sorted(lb) == [str(n) for n in range(2, 10)],
         "une colonne absente et une colonne vide ne se distinguent pas côté conteneur ; les "
         "poser toutes rend l'effacement explicite. Obtenu %s" % sorted(lb))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
