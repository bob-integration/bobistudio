#!/usr/bin/env python3
"""Réglages → Pupitres → ATEM : le niveau de libellé des touches, sur ses quatre faces.

CE QU'ELLE ATTRAPE. Un réglage d'interface ne casse jamais bruyamment : le sélecteur s'affiche,
on choisit un niveau, et les touches ne changent pas. Quatre faces doivent rester d'accord —

  le GABARIT (l'en-tête de colonne) ── le JS (ce qu'il poste) ── la ROUTE (ce qu'elle accepte
  et persiste) ── l'i18N (dans les deux langues, et du BON côté : cœur ou service)

— et deux pièges de comportement propres à ce réglage :

  · n'envoyer QU'UN des deux niveaux remettrait l'autre au défaut sans le dire (la route reçoit
    l'objet entier) ;
  · une source dont le niveau choisi est vide doit garder le DÉFAUT : une touche vide se lit
    comme une panne du pupitre, alors que c'est un libellé qu'on n'a pas écrit.

Lancer : ./venv/bin/python tests/verif_atem_niveau_libelle.py
"""
import ast
import json
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

GABARIT = os.path.join(RACINE, "services/atem/settings_tab.html")
SOURCE = os.path.join(RACINE, "services/atem/__init__.py")
JS = os.path.join(RACINE, "templates/settings.html")
ECHECS = []

# Les clés et LEUR CÔTÉ : `service.atem.*` appartient au sous-module (le cœur PRIME, donc une clé
# de service laissée dans le cœur masque celle du service et l'éditer ne change rien à l'écran —
# c'est `tests/check_i18n_scope.py` qui l'a appris à l'auteur de ce fichier).
CLES_SERVICE = ["service.atem.niveau_de_libelle_des_touches", "service.atem.niveau_de_libelle_aide"]
CLES_COEUR = ["settings.js.label_level_default", "settings.js.label_level_long",
              "settings.js.label_level_short"]


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

    # ── 1. LE GABARIT : la colonne existe, par sa clé i18n, et le tableau reste cohérent.
    html = open(GABARIT, encoding="utf-8").read()
    verifier("gabarit : l'en-tête de colonne passe par l'i18n",
             "service.atem.niveau_de_libelle_des_touches" in html, True)
    verifier("gabarit : l'aide du réglage est rendue", "service.atem.niveau_de_libelle_aide" in html, True)
    # Le `colspan` du cas « aucun pupitre » doit suivre le nombre de colonnes, sinon la ligne
    # vide se décale et la table paraît cassée.
    tete = re.search(r"service\.atem\.pupitres_amp_affectation.*?</thead>", html, re.S)
    verifier("gabarit : la section des pupitres est toujours là", tete is not None, True)
    if tete:
        # ⚠ `<th` compte aussi `<thead>` : ça a fait rougir cette garde sur un gabarit correct.
        n_th = len(re.findall(r"<th[ >]", tete.group(0)))
        colspans = [int(c) for c in re.findall(r'colspan="(\d+)"', html)]
        verifier("gabarit : colspan du tableau = nombre de colonnes",
                 n_th in colspans, True)

    # ── 2. LE JS : les options, et surtout l'envoi de l'objet COMPLET.
    js = open(JS, encoding="utf-8").read()
    verifier("js : la cellule des niveaux est rendue", "data-labels-ip=" in js, True)
    verifier("js : le sélecteur est construit", "_atemNiveauOptions(" in js, True)
    verifier("js : l'envoi existe", "assignerLibelles(" in js, True)
    bloc = js.split("async function assignerLibelles", 1)
    verifier("js : assignerLibelles est bien défini", len(bloc) == 2, True)
    if len(bloc) == 2:
        corps = bloc[1].split("\n}", 1)[0]
        # ★ Le piège : poster {long} seul remettrait short au défaut. On exige les DEUX clés.
        verifier("js : l'envoi porte les DEUX niveaux (pas celui qu'on vient de changer seul)",
                 "long:" in corps and "short:" in corps, True)
        verifier("js : il part sur /api/atem/assign", "/api/atem/assign" in corps, True)
        verifier("js : « Défaut » se poste en null, pas en 0 ni en chaîne vide",
                 "=== '' ? null" in corps, True)
        verifier("js : l'erreur de la route est montrée, pas avalée",
                 "d.error" in corps, True)
    verifier("js : le niveau courant est mémorisé par pupitre", "_atemPanelLabels" in js, True)
    # Les niveaux offerts : 2..9, et le défaut. Rien d'autre — 0 et 1 sont le hostname et le shm,
    # qui ne sont pas des niveaux de la page Libellés.
    opts = js.split("function _atemNiveauOptions", 1)
    if len(opts) == 2:
        c = opts[1].split("\n}", 1)[0]
        verifier("js : niveaux offerts 2..9", ("n = 2" in c and "n <= 9" in c), True)

    # ── 3. LA ROUTE : accepte `labels`, refuse hors bornes, expose l'état.
    arbre = ast.parse(open(SOURCE, encoding="utf-8").read())
    src = ast.unparse(arbre)
    verifier("route : /api/atem/assign lit la clé labels", "'labels' in data" in src, True)
    verifier("route : un niveau hors 2..9 est REFUSÉ (et pas ignoré en silence)",
             "hors 2..9" in open(SOURCE, encoding="utf-8").read(), True)
    verifier("route : /api/atem/sessions expose les niveaux",
             "'labels': _label_levels_for_ip(ip)" in src, True)

    # ── 4. L'i18n, dans les deux langues ET du bon côté.
    for lang in ("fr", "en"):
        coeur = json.load(open(os.path.join(RACINE, f"i18n/{lang}.json"), encoding="utf-8"))
        serv = json.load(open(os.path.join(RACINE, f"services/atem/i18n/{lang}.json"),
                              encoding="utf-8"))
        verifier(f"i18n {lang} : les clés du service sont dans le SERVICE",
                 [k for k in CLES_SERVICE if not serv.get(k)], [])
        verifier(f"i18n {lang} : aucune clé de service laissée dans le cœur (elle le masquerait)",
                 [k for k in CLES_SERVICE if k in coeur], [])
        verifier(f"i18n {lang} : les clés du JS sont dans le cœur",
                 [k for k in CLES_COEUR if not coeur.get(k)], [])
    fr = json.load(open(os.path.join(RACINE, "services/atem/i18n/fr.json"), encoding="utf-8"))
    verifier("i18n : l'aide ANNONCE la limite de 4 octets du nom court",
             "4" in fr["service.atem.niveau_de_libelle_aide"], True)

    # ── 5. LE COMPORTEMENT : lecture tolérante, et le repli qui interdit la touche vide.
    vrai_panels = atem._panels
    try:
        atem._panels = lambda: {"10.0.0.1": {"labels": {"long": 3, "short": 4}},
                                "10.0.0.2": {"labels": {"long": 99, "short": "x"}},
                                "10.0.0.3": {},
                                "10.0.0.4": {"labels": "n'importe quoi"}}
        verifier("lecture : les niveaux valides sont rendus",
                 atem._label_levels_for_ip("10.0.0.1"), {"long": 3, "short": 4})
        # Tolérance en LECTURE : un réglage aberrant ne doit pas rendre un pupitre muet.
        verifier("lecture : un niveau hors bornes est ignoré, pas une exception",
                 atem._label_levels_for_ip("10.0.0.2"), {"long": None, "short": None})
        verifier("lecture : pupitre sans réglage = défaut",
                 atem._label_levels_for_ip("10.0.0.3"), {"long": None, "short": None})
        verifier("lecture : réglage d'un type inattendu = défaut",
                 atem._label_levels_for_ip("10.0.0.4"), {"long": None, "short": None})
        verifier("lecture : pupitre inconnu = défaut",
                 atem._label_levels_for_ip("10.9.9.9"), {"long": None, "short": None})
    finally:
        atem._panels = vrai_panels

    etat = {"n_inputs": 3, "inputs": ["flux-a", "", "flux-c"]}
    vrai_label = atem._label_de_niveau
    try:
        # Niveau renseigné pour UNE source seulement : l'autre doit garder le défaut.
        atem._label_de_niveau = lambda shm, niv: "CAM UN" if (shm == "flux-a" and niv == 2) else ""
        r = [atem._noms_source(etat, i, {"long": 2}) for i in range(3)]
        verifier("repli : la source nommée prend son libellé", r[0], ("CAM UN", "1"))
        verifier("repli : la source SANS libellé garde le nom de flux (jamais vide)",
                 r[2], ("flux-c", "3"))
        verifier("un slot non câblé reste (None, None) : il sera SAUTÉ, pas écrasé", r[1], (None, None))
        # Niveau en nom court : on lit le libellé, et il est tronqué à 4 octets SANS couper un
        # caractère UTF-8 en deux (« Caméra 1 » donnait b'Cam\xc3', illisible sur la touche).
        atem._label_de_niveau = lambda shm, niv: "Caméra 1" if shm == "flux-a" else ""
        ln, sn = atem._noms_source(etat, 0, {"short": 2})
        verifier("nom court : le libellé est lu", sn, "Caméra 1")
        verifier("nom court : tronqué sur une frontière de caractère, pas d'octet",
                 atem._pad(sn, 4), b"Cam\x00")
        verifier("nom court : aucun niveau = le NUMÉRO d'entrée",
                 atem._noms_source(etat, 0, None)[1], "1")
    finally:
        atem._label_de_niveau = vrai_label

    print()
    if ECHECS:
        print("ÉCHEC :", ", ".join(ECHECS))
        return 1
    print("OK — gabarit, JS, route et i18n d'accord ; une touche n'est jamais vide.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
