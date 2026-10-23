#!/usr/bin/env python3
"""Pupitre ATEM : les sources des M/E doivent être les MÊMES, et l'écart doit se voir.

★ POURQUOI ÇA COMPTE (2026-10-23). Un `InPr` est indexé par SOURCE, pas par rangée M/E — mesuré
sur la capture Constellation, où `InPr` est le seul porteur de noms de sources et où les 20
sources physiques sont déclarées disponibles sur les 4 M/E. Le pupitre n'a donc qu'UNE liste de
noms, celle du M/E 1. Si un autre M/E pilote un mélangeur aux sources différentes, **ses touches
portent les noms de celles du M/E 1** : elles mentent, sans que rien ne le signale.

Deux faces, et chacune a son piège :

  · LE CALCUL. Il porte sur le câblage DÉCLARÉ (`deploy_config`), pas sur le `/state` des
    conteneurs : pas de réseau au rafraîchissement de la page, et ça marche conteneur éteint —
    c'est-à-dire précisément quand l'exploitant a besoin de l'information.
  · L'AFFICHAGE. Un écart porte toujours un LIBELLÉ TEXTE, jamais la seule couleur (règle
    « Statut > Couleur » de docs/design/DESIGN.md). Et le badge doit se voir PLI FERMÉ, sinon
    l'information dort derrière un pli que personne n'ouvre.

Lancer : ./venv/bin/python tests/verif_atem_sources_rangees.py
"""
import ast
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

SOURCE = os.path.join(RACINE, "services/atem/__init__.py")
JS = os.path.join(RACINE, "templates/settings.html")
ECHECS = []

CLES = ["settings.js.sources_btn", "settings.js.sources_differ_badge",
        "settings.js.sources_me1_badge", "settings.js.sources_me1_off_badge",
        "settings.js.sources_identical", "settings.js.sources_me_differs",
        "settings.js.sources_me_unknown", "settings.js.sources_me1_unreachable",
        "settings.js.sources_me1_unassigned", "settings.js.sources_me1_unknown",
        "settings.js.sources_col_key", "settings.js.sources_col_shown",
        "settings.js.sources_reference", "settings.js.sources_same",
        "settings.js.sources_differs", "settings.js.sources_empty",
        "settings.js.sources_truncated", "settings.js.sources_no_me"]


def verifier(nom, obtenu, attendu):
    ok = obtenu == attendu
    print(("  ok   " if ok else "  ÉCHEC") + f" {nom} : obtenu {obtenu!r}, attendu {attendu!r}")
    if not ok:
        ECHECS.append(nom)


def main():
    print(__doc__.splitlines()[0])
    if not os.path.exists(SOURCE):
        print("  sous-module services/atem absent — garde inapplicable")
        return 0
    from services import atem

    # ── 1. LE CRITÈRE. Aucun réseau, aucune base : on injecte les configurations déclarées.
    vrai = atem._inputs_declares
    vrai_host = atem._hostname_de
    try:
        atem._hostname_de = lambda v: f"mix-{v}"
        decl = {}
        atem._inputs_declares = lambda v: decl.get(v)

        decl[1] = {"n_inputs": 3, "inputs": ["a", "b", "c"]}
        decl[2] = {"n_inputs": 3, "inputs": ["a", "b", "c"]}
        r = atem._sources_des_rangees([1, 2])
        verifier("mêmes sources partout : identiques", r["identiques"], True)
        verifier("mêmes sources : aucun slot signalé", r["rangees"][1]["diff_slots"], [])
        verifier("la référence est le M/E 1", r["reference_me"], 1)

        decl[2] = {"n_inputs": 3, "inputs": ["z", "b", "y"]}
        r = atem._sources_des_rangees([1, 2])
        verifier("deux slots différents : les deux sont donnés",
                 r["rangees"][1]["diff_slots"], [0, 2])
        verifier("un écart suffit à ne plus être identiques", r["identiques"], False)

        # Un slot câblé d'un seul côté EST une différence (sinon l'écart le plus trompeur passe).
        decl[2] = {"n_inputs": 3, "inputs": ["a", "", "c"]}
        verifier("slot vide d'un seul côté : signalé",
                 atem._sources_des_rangees([1, 2])["rangees"][1]["diff_slots"], [1])
        decl[1] = {"n_inputs": 3, "inputs": ["a", "", "c"]}
        decl[2] = {"n_inputs": 3, "inputs": ["a", "b", "c"]}
        verifier("slot vide de l'autre côté : signalé aussi",
                 atem._sources_des_rangees([1, 2])["rangees"][1]["diff_slots"], [1])

        # Un NOMBRE D'ENTRÉES différent produit autant d'écarts qu'il manque de touches, et la
        # largeur retenue est la PLUS GRANDE — sinon les touches en trop ne seraient pas vues.
        decl[1] = {"n_inputs": 2, "inputs": ["a", "b"]}
        decl[2] = {"n_inputs": 4, "inputs": ["a", "b", "c", "d"]}
        r = atem._sources_des_rangees([1, 2])
        verifier("nombre d'entrées différent : les touches en trop sont signalées",
                 r["rangees"][1]["diff_slots"], [2, 3])
        verifier("la largeur du tableau est la plus grande des rangées", r["n_inputs_max"], 4)

        # Configuration illisible : on ne se fait PAS passer pour conforme.
        decl[2] = None
        r = atem._sources_des_rangees([1, 2])
        verifier("rangée illisible : marquée inconnue", r["rangees"][1]["connu"], False)
        verifier("rangée illisible : jamais « identiques »", r["identiques"], False)

        # ⚠ Et le cas qui FAIT TRAVAILLER `connu` : des rangées sans aucune entrée déclarée, donc
        # sans écart possible. Sans le drapeau, une configuration illisible passerait pour
        # conforme — c'est le seul cas où `diff_slots` ne sauve pas l'assertion précédente.
        decl[1] = {"n_inputs": 0, "inputs": []}
        decl[2] = None
        r = atem._sources_des_rangees([1, 2])
        verifier("aucun écart POSSIBLE mais rangée illisible : pas « identiques »",
                 (r["rangees"][1]["diff_slots"], r["identiques"]), ([], False))

        # Les rangées non affectées n'ont pas de colonne.
        decl[1] = {"n_inputs": 1, "inputs": ["a"]}
        r = atem._sources_des_rangees([1, None, None])
        verifier("une rangée non affectée ne produit pas de colonne", len(r["rangees"]), 1)
        r = atem._sources_des_rangees([None, 1])
        verifier("M/E 1 non affecté : pas de référence", r["reference_me"], None)
        verifier("M/E 1 non affecté : me1_affecte faux", r["me1_affecte"], False)
        verifier("sans référence, on n'affirme pas l'égalité", r["identiques"], False)
        verifier("aucun M/E : tableau vide", atem._sources_des_rangees([])["rangees"], [])
    finally:
        atem._inputs_declares = vrai
        atem._hostname_de = vrai_host

    # ── 1 bis. LA LECTURE DÉCLARATIVE elle-même (`_inputs_declares`) : c'est elle qui rend la
    # comparaison possible sans réseau et conteneur éteint, donc elle doit encaisser ce que la
    # base contient réellement. Doublure de `db_get_container` : aucune base, aucun réseau.
    from app import database as _db
    vrai_get = _db.db_get_container
    try:
        cont = {}
        _db.db_get_container = lambda v: cont.get(int(v))
        cont[10] = {"deploy_config": json.dumps(
            {"type": "mixer", "params": {"n_inputs": 2, "input_1": "a", "input_2": "b"}})}
        verifier("déclaré : n_inputs et les entrées",
                 atem._inputs_declares(10), {"n_inputs": 2, "inputs": ["a", "b"]})
        # Sans `n_inputs` : on déduit du plus grand NUMÉRO posé — et les clés de format
        # (`input_2_fmt`, posées à côté par le câblage) ne doivent pas être comptées.
        cont[11] = {"deploy_config": json.dumps({"type": "mixer", "params": {
            "input_1": "a", "input_3": "c", "input_3_fmt": {"width": 1920},
            "input_format": {}, "autre": 1}})}
        verifier("déclaré : sans n_inputs, déduit du plus grand numéro (slots vides compris)",
                 atem._inputs_declares(11), {"n_inputs": 3, "inputs": ["a", "", "c"]})
        cont[12] = {"deploy_config": json.dumps({"type": "mixer", "params": {}})}
        verifier("déclaré : aucune entrée posée",
                 atem._inputs_declares(12), {"n_inputs": 0, "inputs": []})
        cont[13] = {"deploy_config": "{ pas du json"}
        verifier("déclaré : deploy_config illisible → None (et pas d'exception)",
                 atem._inputs_declares(13), None)
        cont[14] = {}
        verifier("déclaré : conteneur sans deploy_config → None", atem._inputs_declares(14), None)
        verifier("déclaré : conteneur inconnu → None", atem._inputs_declares(99), None)
    finally:
        _db.db_get_container = vrai_get

    # ── 2. L'APERÇU passe par le code qui ÉMET, et expose la troncature.
    vrai_etat = atem._etat_noms_de
    vrai_lv = atem._label_levels_for_ip
    vrai_lab = atem._label_de_niveau
    try:
        atem._etat_noms_de = lambda v: ({"n_inputs": 2, "inputs": ["flux-a", ""]}, "live")
        atem._label_levels_for_ip = lambda ip: {"long": 2, "short": 2}
        atem._label_de_niveau = lambda shm, niv: "Mélangeur PGM" if shm == "flux-a" else ""
        ap, org = atem._apercu_touches("10.0.0.1", [1])
        verifier("aperçu : l'origine des noms est rendue", org, "live")
        verifier("aperçu : le nom long est tronqué à 20 octets", ap[0]["long"], "Mélangeur PGM")
        # 4 octets, frontière de caractère : « Mélangeur PGM » → « Mél » (le « é » fait 2 octets).
        verifier("aperçu : le nom court montre la troncature RÉELLE", ap[0]["short"], "Mél")
        verifier("aperçu : la valeur complète est fournie pour l'infobulle",
                 ap[0]["short_complet"], "Mélangeur PGM")
        verifier("aperçu : un slot non câblé est explicitement vide", ap[1]["long"], None)
        atem._etat_noms_de = lambda v: (None, "absent")
        verifier("aperçu : M/E 1 non affecté → rien à montrer",
                 atem._apercu_touches("10.0.0.1", [None]), ([], "absent"))
    finally:
        atem._etat_noms_de = vrai_etat
        atem._label_levels_for_ip = vrai_lv
        atem._label_de_niveau = vrai_lab

    # ── 3. LA ROUTE expose les trois champs, et ne calcule l'aperçu QU'UNE FOIS (il interroge le
    # M/E 1 : deux appels, c'était deux fois le réseau à chaque rafraîchissement de la page).
    src = ast.unparse(ast.parse(open(SOURCE, encoding="utf-8").read()))
    verifier("route : expose la comparaison", "'sources': _sources_des_rangees(me_vmids)" in src, True)
    verifier("route : expose l'aperçu", "'apercu': _apercu[0]" in src, True)
    verifier("route : expose l'origine des noms", "'noms_origine': _apercu[1]" in src, True)
    # ⚠ Compter les APPELS par AST, pas les occurrences de texte : la définition de la fonction
    # porte la même signature et faisait rougir cette garde sur un code correct.
    arbre = ast.parse(open(SOURCE, encoding="utf-8").read())
    appels = sum(1 for n in ast.walk(arbre)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_apercu_touches")
    verifier("route : l'aperçu n'est calculé qu'une fois par pupitre", appels, 1)

    # ── 4. L'AFFICHAGE.
    js = open(JS, encoding="utf-8").read()
    verifier("js : le bouton ouvre le pli", "basculerSources(" in js, True)
    verifier("js : la ligne dépliable existe", 'class="atem-sources-row hidden"' in js, True)
    verifier("js : elle est pleine largeur", 'colspan="9"' in js, True)
    # ⚠ « _atemSourcesBadge(p) in js » restait VRAI après retrait de l'appel : la DÉFINITION
    # porte la même chaîne. On vérifie le site d'appel, dans la cellule de la ligne du pupitre.
    cellule = js.split("const lvSel =", 1)
    verifier("js : la cellule de la ligne du pupitre existe", len(cellule) == 2, True)
    if len(cellule) == 2:
        c = cellule[1].split(";", 1)[0]
        verifier("js : le badge est rendu DANS la ligne du pupitre (donc visible pli fermé)",
                 "_atemSourcesBadge(p)" in c, True)
        verifier("js : le bouton d'ouverture est dans la même cellule",
                 "basculerSources(" in c, True)
    # ⚠ « Statut > Couleur » : l'écart porte un mot, pas seulement une couleur de fond.
    bloc = js.split("function _atemSourcesTable", 1)
    verifier("js : la fonction du tableau existe", len(bloc) == 2, True)
    if len(bloc) == 2:
        c = bloc[1].split("\nfunction ", 1)[0]
        verifier("js : un écart porte le LIBELLÉ texte « diffère », pas que la couleur",
                 "settings.js.sources_differs" in c, True)
        verifier("js : la couleur d'écart vient des tokens du thème",
                 "var(--status-warning-bg)" in c and "var(--status-warning-fg)" in c, True)
        verifier("js : défilement horizontal du TABLEAU seul (jamais de la page)",
                 "overflow-x:auto" in c, True)
        # ⚠ « _atemEchappe( in c » survivait au retrait d'UN échappement : il en restait
        # d'autres. On cherche les interpolations NUES des valeurs venues de la base.
        import re as _re
        nues = _re.findall(r"\$\{(?:nom|r\.hostname|a\.long|a\.short|r\.vmid)[^}]*\}", c)
        nues = [x for x in nues if "_atemEchappe" not in x]
        verifier("js : aucune valeur de la base interpolée sans échappement", nues, [])
        verifier("js : la colonne de référence est signalée",
                 "settings.js.sources_reference" in c, True)
    # Le pli ne doit pas se refermer sous les doigts au rafraîchissement (défaut corrigé le
    # 2026-08-30 sur le panneau « Régler »), et la garde ne dépend pas de QUI appelle.
    # Même piège : on exige que l'appel CONDITIONNE un retour anticipé avant la réécriture du
    # tbody (« if (_atemPliOuvert())  …  return; » puis « tb.innerHTML = »).
    avant_rendu = js.split("tb.innerHTML = data.panels.map(", 1)[0]
    verifier("js : un pli ouvert interdit le re-rendu du tableau",
             "if (_atemPliOuvert())" in avant_rendu and "return;" in avant_rendu.split(
                 "if (_atemPliOuvert())", 1)[1], True)
    verifier("js : un pli ouvert est mis à jour EN PLACE (pas de clignotement)",
             "_atemMajSourcesOuvertes(" in js, True)
    maj = js.split("function _atemMajSourcesOuvertes", 1)
    if len(maj) == 2:
        c = maj[1].split("\nfunction ", 1)[0]
        verifier("js : on ne réécrit que ce qui a changé",
                 "if (td.innerHTML !== neuf)" in c, True)

    # ── 5. L'i18n, dans les deux langues.
    for lang in ("fr", "en"):
        cat = json.load(open(os.path.join(RACINE, f"i18n/{lang}.json"), encoding="utf-8"))
        verifier(f"i18n {lang} : toutes les clés du tableau sont présentes",
                 [k for k in CLES if not cat.get(k)], [])
    fr = json.load(open(os.path.join(RACINE, "i18n/fr.json"), encoding="utf-8"))
    for cle, jeton in (("settings.js.sources_me_differs", "{me}"),
                       ("settings.js.sources_me_differs", "{n}"),
                       ("settings.js.sources_truncated", "{v}")):
        verifier(f"i18n : {cle} porte son paramètre {jeton}", jeton in fr[cle], True)
    verifier("i18n : le badge d'écart porte un MOT, pas qu'un symbole",
             any(c.isalpha() for c in fr["settings.js.sources_differ_badge"]), True)

    print()
    if ECHECS:
        print("ÉCHEC :", ", ".join(ECHECS))
        return 1
    print("OK — l'écart est calculé sans réseau, et il se voit, pli fermé comme ouvert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
