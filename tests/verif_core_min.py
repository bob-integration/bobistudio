#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de l'EXIGENCE DE VERSION DU CŒUR (`requires.core_min`) et du comparateur d'`app/version.py`.
#
# ★ CE QUE ÇA PROTÈGE. Un composant peut avoir besoin d'un orchestrateur récent, et jusqu'au
# 2026-09-02 rien ne le déclarait. Deux cas réels ce jour-là, entre le dépôt public et le privé :
#
#   · `services/tsl` importe `app.tally`, créé après la release publiée → ImportError au
#     chargement, le service ne démarre pas. Panne BRUYANTE.
#   · `plugins/multiview` 0.115.2 attend que le cœur lui pousse ses colonnes de libellé. Sur un
#     cœur d'avant, personne ne les pousse : le mur affiche des libellés périmés et RIEN ne le
#     signale. Panne SILENCIEUSE — c'est elle qui a motivé le garde.
#
# ★ ET OÙ IL EST POSÉ, ce qui compte autant : dans `validate_package`, par où passent les TROIS
# voies d'installation (catalogue + les deux imports manuels). Le mettre sur les sites d'appel
# aurait laissé le prochain en oubli silencieux — le défaut exact relevé sur l'épinglage de
# version, où un seul appel sur 23 transmettait sa consigne.
#
#   $ ./venv/bin/python tests/verif_core_min.py
import io
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from app import version as V                                         # noqa: E402
from app import plugins, core_plugins                                # noqa: E402

print("Exigence de version du cœur\n")

# ═══ 1. LE COMPARATEUR ═══════════════════════════════
controle("★★ une version égale satisfait l'exigence", V.au_moins("0.9.2", "0.9.2"))
controle("★★★ une version INFÉRIEURE ne la satisfait pas", not V.au_moins("0.9.1", "0.9.2"),
         "sans ça le garde laisse tout passer et n'a jamais mordu")
controle("★★ une version supérieure la satisfait", V.au_moins("0.10.0", "0.9.9"),
         "★ comparaison NUMÉRIQUE par segment : lexicalement, « 0.10.0 » précède « 0.9.9 » et un "
         "cœur plus récent serait refusé")
controle("★★ les longueurs inégales se comblent de zéros", V.au_moins("0.9", "0.9.0"),
         "sinon « 0.9 » serait jugé antérieur à « 0.9.0 », sans que personne puisse le comprendre")
controle("★ une exigence VIDE ne bloque rien", V.au_moins("0.9.2", "") and V.au_moins("0.9.2", None))

# ★ NE PAS SAVOIR COMPARER N'AUTORISE PAS À BLOQUER. Un refus fondé sur une comparaison bancale
# serait pire que l'absence de contrôle — c'est la règle déjà tenue par le contrôle d'image des
# nœuds (`docker_compute`), et les deux doivent se comporter pareil.
controle("★★★ une version NON analysable laisse passer, et se signale",
         V.au_moins("main", "0.9.2") and V.au_moins("0.9.2", "0.24-fix")
         and not V.comparable("main", "0.9.2") and not V.comparable("0.9.2", "0.24-fix"),
         "obtenu au_moins=%s/%s comparable=%s/%s"
         % (V.au_moins("main", "0.9.2"), V.au_moins("0.9.2", "0.24-fix"),
            V.comparable("main", "0.9.2"), V.comparable("0.9.2", "0.24-fix")))
controle("★ le préfixe « v » d'un tag est toléré", V.analyser("v0.9.2") == (0, 9, 2))

# ═══ 2. LE GARDE, SUR LES DEUX FAMILLES ═════════════════════
def _plugin(tmp, core_min=None):
    d = os.path.join(tmp, "p"); os.makedirs(d, exist_ok=True)
    man = {"type": "banc_core_min", "label": "Banc", "version": "1.0.0",
           "script_template": "script.py"}
    if core_min is not None:
        man["requires"] = {"core_min": core_min}
    json.dump(man, io.open(os.path.join(d, "plugin.json"), "w", encoding="utf-8"))
    io.open(os.path.join(d, "script.py"), "w", encoding="utf-8").write(
        "# {config} {hostname} {plugin_version}\n")
    return d


def _service(tmp, core_min=None):
    d = os.path.join(tmp, "s"); os.makedirs(d, exist_ok=True)
    # Les six clés exigées d'un manifeste de service : sans elles, `validate_package` refuse
    # sur la STRUCTURE et le banc ne dirait plus rien de l'exigence de version.
    man = {"id": "banc_core_min", "label": "Banc", "version": "1.0.0",
           "nav_tab": "protocoles", "tab_template": "banc/settings_tab.html",
           "settings_keys": {}}
    if core_min is not None:
        man["requires"] = {"core_min": core_min}
    json.dump(man, io.open(os.path.join(d, "manifest.json"), "w", encoding="utf-8"))
    io.open(os.path.join(d, "__init__.py"), "w", encoding="utf-8").write("")
    return d


with tempfile.TemporaryDirectory() as tmp:
    _, err = plugins.validate_package(_plugin(tmp, "9.9.9"))
    controle("★★★ un PLUGIN exigeant une version future est REFUSÉ",
             bool(err) and "9.9.9" in err and V.VERSION in err,
             "le motif doit nommer les DEUX versions, sinon l'exploitant ne sait pas quoi faire. "
             "Obtenu %r" % err)

    _, err = plugins.validate_package(_plugin(tmp, V.VERSION))
    controle("★★ ...et l'exigence exactement satisfaite passe", not err, "obtenu %r" % err)

    _, err = plugins.validate_package(_plugin(tmp, None))
    controle("★★ un plugin SANS exigence passe", not err,
             "la très grande majorité n'en déclare pas : les bloquer serait une régression totale")

    _, err = core_plugins.validate_package(_service(tmp, "9.9.9"))
    controle("★★★ un SERVICE exigeant une version future est REFUSÉ",
             bool(err) and "9.9.9" in err, "obtenu %r" % err)

    _, err = core_plugins.validate_package(_service(tmp, None))
    controle("★★ un service sans exigence passe", not err, "obtenu %r" % err)

# ═══ 3. LE GARDE EST AU POINT DE PASSAGE UNIQUE ═══════════════
import inspect                                                       # noqa: E402
for nom, fn in (("plugins", plugins.validate_package),
                ("core_plugins", core_plugins.validate_package)):
    controle("★★★ %s.validate_package porte le garde lui-même" % nom,
             "core_min_de" in inspect.getsource(fn),
             "les trois voies d'installation passent par cette fonction ; le garde posé sur les "
             "sites d'appel laisserait le prochain en oubli silencieux")

# ═══ 4. LES DEUX COMPOSANTS CONCERNÉS LE DÉCLARENT ══════════════
for chemin, fichier in (("plugins/multiview", "plugin.json"),
                        ("services/tsl", "manifest.json")):
    p = os.path.join(RACINE, chemin, fichier)
    if not os.path.isfile(p):
        continue
    man = json.load(io.open(p, encoding="utf-8"))
    controle("★★ %s déclare son exigence de cœur" % chemin,
             bool(V.core_min_de(man)),
             "les deux cassent sur un cœur antérieur — l'un bruyamment, l'autre en silence. "
             "Obtenu %r" % (man.get("requires")))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
