#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du contrôle de CATALOGUE « en-tête dépliant » (`ctl-disclose`).
#
# POURQUOI IL EXISTE. Ce contrôle est né en privé : les groupes de la liste d'instances (tri par
# projet / par capacité, demande du testeur du 2026-11-27) portaient leurs propres `.tp-group-head`,
# `.tp-group-caret`, `.tp-group-label`, `.tp-group-count` dans `templates/plugin_section.html` —
# exactement ce que le dépôt interdit (CLAUDE.md, « Interface : TOUJOURS les contrôles partagés » ;
# cinq rotatifs dans cinq fichiers privés avant 2026-07-26). La famille a été montée au catalogue
# le 2026-11-28 et la page s'en sert. Rien ne le gardait.
#
# CE QU'IL PROTÈGE, et qui ne se voit pas en lisant le code :
#   · la REDÉFINITION qui revient. Une page qui réintroduit un en-tête dépliant chez elle ne casse
#     rien de visible : elle fait juste diverger deux dessins que personne ne compare plus.
#   · le chevron piloté à la MAIN. Il tourne sur `[aria-expanded="true"]`, donc l'état accessible
#     et l'état dessiné ne peuvent pas se contredire. Une classe posée à la main les sépare : le
#     chevron dirait ouvert et le lecteur d'écran fermé, sans que rien ne soit rouge.
#   · le COMPTEUR replié. C'est la seule chose qui dise ce qu'on cache ; replier un groupe dont on
#     ne sait pas s'il contient trois murs ou aucun, c'est perdre l'information qu'on vient de
#     ranger.
#   · les valeurs EN DUR. Une couleur ou une police hors token tient dans la lumière par défaut et
#     se voit dans les deux autres — là où personne ne regarde en développant.
#
#   $ ./venv/bin/python tests/verif_controle_depliant.py
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(RACINE, "static", "css", "controls.css")
PAGE = os.path.join(RACINE, "templates", "plugin_section.html")
# Les trois lumières : un token doit être défini dans les trois, sinon il tombe dans l'une d'elles.
LUMIERES = [os.path.join(RACINE, "static", "css", f)
            for f in ("base.css", "theme-light.css", "theme-studio.css")]

_rouges = []


def controle(libelle, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + libelle + ("" if ok else "  [%s]" % (detail,)))
    if not ok:
        _rouges.append(libelle)


def bloc_famille(css):
    """Les règles de `controls.css` qui portent sur la famille, texte brut.

    Découpé sur les accolades plutôt que par une expression sur tout le fichier : une famille
    voisine dont le sélecteur contient le mot ne doit pas entrer dans le bloc mesuré.
    """
    out = []
    for regle in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        sel, corps = regle[0].strip(), regle[1]
        if ".ctl-disclose" in sel:
            out.append((sel, corps))
    return out


def main():
    css = open(CSS, encoding="utf-8").read()
    page = open(PAGE, encoding="utf-8").read()
    famille = bloc_famille(css)
    selecteurs = " ".join(s for s, _ in famille)
    corps = " ".join(c for _, c in famille)

    print("1. la famille est au catalogue")
    controle("le catalogue porte la famille", bool(famille), len(famille))
    for part in ("ctl-disclose-caret", "ctl-disclose-lab", "ctl-disclose-count",
                 "ctl-disclose--active"):
        controle("le catalogue porte .%s" % part, ("." + part) in selecteurs, selecteurs[:200])
    controle("le contrat est écrit au-dessus de la famille",
             "aria-expanded" in css.split(".ctl-disclose {")[0][-1200:],
             "aucun contrat documenté")

    print("\n2. l'état dessiné ne peut pas contredire l'état accessible")
    rotation = [s for s, c in famille if "rotate" in c]
    controle("le chevron tourne, et sur une seule règle", len(rotation) == 1, rotation)
    controle("c'est `aria-expanded` qui le tourne, pas une classe posée à la main",
             bool(rotation) and 'aria-expanded="true"' in rotation[0], rotation)
    # Une classe d'ouverture au catalogue rouvrirait la porte qu'on vient de fermer.
    controle("le catalogue n'offre aucune classe d'ouverture concurrente",
             not re.search(r"\.ctl-disclose--(open|expanded|ouvert)", selecteurs), selecteurs[:200])

    print("\n3. les invariants du catalogue")
    dur_couleur = re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", corps)
    controle("aucune couleur en dur dans la famille", not dur_couleur, dur_couleur)
    # La géométrie en px est permise (largeurs, marges) ; la POLICE et le RAYON passent par les
    # tokens, c'est là que les trois lumières divergent.
    def valeurs(propriete):
        return [v.strip() for v in re.findall(propriete + r"\s*:([^;}]+)", corps)]

    dur_police = [v for v in valeurs("font-family") if not v.startswith("var(")]
    controle("aucune police en dur dans la famille", not dur_police, dur_police)
    dur_rayon = [v for v in valeurs("border-radius")
                 if not v.startswith("var(") and v != "9px"]
    controle("le rayon passe par un token (sauf la pastille, ronde par construction)",
             not dur_rayon, dur_rayon)
    tokens = sorted(set(re.findall(r"var\(\s*(--[a-z0-9-]+)", corps)))
    controle("la famille s'appuie sur des tokens", len(tokens) >= 5, tokens)
    manquants = []
    for tok in tokens:
        definis = sum(1 for f in LUMIERES
                      if re.search(re.escape(tok) + r"\s*:", open(f, encoding="utf-8").read()))
        if definis != len(LUMIERES):
            manquants.append((tok, definis))
    controle("chaque token est défini dans les TROIS lumières", not manquants, manquants)

    print("\n4. la page s'en sert au lieu de le redéfinir")
    controle("la liste d'instances utilise la famille", "ctl-disclose" in page, "")
    prives = re.findall(r"\.tp-group-(?:head|caret|label|count)\b|\.tp-group\s*\{[^}]*cursor",
                        page)
    controle("la page ne redéfinit plus d'en-tête dépliant chez elle", not prives, prives)
    # Le dessin ne doit pas être recopié : seul le catalogue porte le fond, le chevron et la
    # pastille. Ce qui reste légitime dans la page, c'est l'IMBRICATION des instances.
    reste = re.findall(r"\.tp-group[a-z-]*\s*\{([^}]*)\}", page)
    controle("ce qui reste dans la page est de l'imbrication, pas du dessin",
             not [c for c in reste if re.search(r"background|cursor|font-weight", c)], reste)

    print("\n5. le contrat de l'en-tête construit par la page")
    bloc = page[page.find("tpGroups(items).forEach"):]
    bloc = bloc[:bloc.find("// Deep-link")] if "// Deep-link" in bloc else bloc[:4000]
    controle("l'en-tête est un bouton, pas une div cliquable",
             "createElement('button')" in bloc and "type = 'button'" in bloc, bloc[:200])
    controle("il porte aria-expanded dès sa création", "aria-expanded" in bloc, "")
    controle("l'ouverture et la fermeture mettent aria-expanded à jour",
             bloc.count("aria-expanded") >= 2, bloc.count("aria-expanded"))
    controle("le compteur est rendu (visible replié)",
             "ctl-disclose-count" in bloc and "g.items.length" in bloc, "")
    controle("le libellé est échappé", "psEsc(g.label)" in bloc, "")

    print()
    if _rouges:
        print("✗ %d contrôle(s) rouge(s) : %s" % (len(_rouges), " · ".join(_rouges)))
        return 1
    print("✓ all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
