#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Empêche les clés i18n d'un plugin de retomber dans le catalogue du cœur.

★ POURQUOI. Un plugin est un dépôt indépendant, distribuable en `.mxlplugin` et
installable depuis la page Catalogue. S'il laisse ses libellés dans `i18n/<lang>.json`
du cœur, il VOYAGE SANS SES LANGUES : sur l'instance qui l'installe, la clé n'existe
pas, et `plugins._traduit` retombe sur le libellé du MANIFESTE — qui est en français.

C'est une panne silencieuse de plus : rien ne casse, rien n'alerte, l'interface
anglaise affiche simplement « Échelle verticale ». Le défaut a existé pour 19 plugins
sur 20 avant la migration du 2026-09-01 ; seul `hello_world`, écrit APRÈS le contrat,
faisait les choses correctement.

Le contrat est dans `plugins/AUTHORING.md` : les libellés d'un contributeur vivent dans
`plugins/<type>/i18n/{fr,en}.json` — `plugin.<type>.*` pour la console,
`type.<type>.*` pour la palette.

⚠ Le cœur PRIME sur le plugin (`i18n._file_catalog_for` fait `merged.update(core)`).
Une clé restée au cœur MASQUE donc celle du plugin : l'auteur édite son fichier,
recharge, et ne voit rien changer.

Les SERVICES sont soumis à la même règle (`service.<name>.*`), et pour la même raison :
ce sont aussi des sous-modules indépendants. Seul `service.group.*` reste au cœur — ce
n'est le libellé d'aucun service, mais le nom des GROUPES de la navigation.

Sortie non-zéro = au moins un plugin a des clés au mauvais endroit.
"""
import json
import pathlib
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent

# `service.group.*` n'appartient à aucun service : c'est le nom des GROUPES de la
# navigation, rendu par le cœur. Il reste au cœur, et ce n'est pas une dérive.
EXEMPT_SERVICE = {"group"}


def _lire(p):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:                                    # JSON cassé = échec net
        print(f"  ✗ {p} illisible : {e}")
        sys.exit(2)


def main():
    plugins = {p.name for p in (RACINE / "plugins").iterdir()
               if (p / "plugin.json").exists()}
    services = {p.name for p in (RACINE / "services").iterdir() if p.is_dir()}

    fautes = {}
    for lang in ("fr", "en"):
        for cle in _lire(RACINE / "i18n" / f"{lang}.json"):
            tete = cle.split(".")
            if len(tete) < 2:
                continue
            fam, nom = tete[0], tete[1]
            if fam in ("type", "plugin") and nom in plugins:
                fautes.setdefault(nom, set()).add(cle)
            elif fam == "service" and nom in services and nom not in EXEMPT_SERVICE:
                fautes.setdefault(nom, set()).add(cle)

    if not fautes:
        print(f"  ✓ aucune clé de plugin ni de service dans le catalogue du cœur "
              f"({len(plugins)} plugins, {len(services)} services vérifiés)")
        return 0

    print(f"\n  ✗ {sum(len(v) for v in fautes.values())} clé(s) de contributeur "
          f"dans i18n/<lang>.json du cœur :\n")
    for t in sorted(fautes):
        cles = sorted(fautes[t])
        print(f"    {t} ({len(cles)}) :")
        for c in cles[:4]:
            print(f"        {c}")
        if len(cles) > 4:
            print(f"        … et {len(cles) - 4} autre(s)")
    print("\n  Déplacez-les dans <plugins|services>/<nom>/i18n/{fr,en}.json.")
    print("  Le cœur PRIME sur le plugin : tant qu'elles sont là, celles du plugin")
    print("  sont masquées et l'éditer ne change rien à l'écran.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
