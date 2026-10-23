#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du champ `multiselect` + de la source d'options `tally_levels`.
#
# CE QU'IL PROTÈGE. Un niveau de tally est une ENTITÉ NOMMÉE du site, et le tally se CUMULE : une
# source peut être suivie sur plusieurs chaînes de destination. Deux façons de se tromper, et
# toutes deux sont silencieuses côté exploitant :
#   · figer la liste des niveaux dans un manifeste (elle dérive dès qu'une production est créée,
#     et le menu propose des niveaux qui n'existent plus) ;
#   · lire un select multiple comme un select simple — `el.value` ne rend que la PREMIÈRE option
#     cochée, donc les suivantes disparaissent à l'enregistrement sans aucune erreur.
#
#   $ ./venv/bin/python tools/verif_tally_multiselect.py
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from app import plugins                                            # noqa: E402


def sans_commentaires(txt):
    """Le code seul, commentaires JS retirés.

    ⚠ NÉCESSAIRE POUR LES CONTRÔLES D'ABSENCE. Ceux d'ici vérifient qu'un motif n'est PLUS là —
    or les commentaires du produit CITENT ce qu'ils ont remplacé (« pas un `<select multiple>` :
    essayé, retiré »), et c'est justement ce qu'on veut qu'ils fassent. Chercher dans le fichier
    brut faisait échouer le banc sur sa propre documentation."""
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    return re.sub(r"^\s*//.*$", "", txt, flags=re.M)
plugins._scan()

print("Tally — champ multiselect et niveaux vivants\n")

# ── La source d'options est VIVANTE, et nommée ───────────────────────────────
opts = plugins.options_dynamiques("tally_levels")
controle("★★ la source `tally_levels` rend des options", bool(opts),
         "sans elle le menu est vide et le réglage inutilisable")
controle("★★★ chaque option porte le NUMÉRO et le NOM écrits par l'exploitant",
         all(isinstance(o.get("label"), str) and "—" in o["label"] for o in opts),
         "« niveau 7 » oblige à aller voir ailleurs ce que ça désigne ; « 7 — Antenne » se "
         "choisit sur place. Obtenu %s" % opts[:2])
controle("★★★ la VALEUR est l'UUID, jamais le numéro",
         all(isinstance(o.get("value"), str) and "-" in o["value"] for o in opts),
         "le numéro n'est qu'un rang d'affichage : réordonner le réécrit, et une configuration "
         "qui l'aurait mémorisé pointerait ensuite un AUTRE niveau, sans rien afficher "
         "d'anormal. Obtenu %s" % [o.get("value") for o in opts[:2]])
from app.database import db_get_tally_levels                       # noqa: E402
controle("★★ la liste suit la base, elle n'est pas figée dans un manifeste",
         len(opts) == len(db_get_tally_levels()),
         "une liste figée dérive dès la première production créée")
controle("★ une source inconnue rend une liste vide, sans lever",
         plugins.options_dynamiques("pas_une_source") == [])

# ── hello_world est l'EXEMPLE : c'est lui qui doit montrer le motif ──────────
m = plugins.REGISTRY.get("hello_world") or {}
champ = next((f for f in (m.get("config_schema") or []) if f.get("key") == "tally_level"), None)
controle("★★★ l'exemple hello_world déclare un multiselect sur la source vivante",
         bool(champ) and champ.get("type") == "multiselect"
         and champ.get("options_from") == "tally_levels",
         "un plugin d'exemple qui fige ses niveaux enseigne le mauvais motif à tous ceux qui "
         "le copient. Obtenu %s" % champ)
controle("★★ son défaut est une LISTE VIDE, pas 0",
         (champ or {}).get("default") == [],
         "`0` était le repli « celui du projet » d'un champ scalaire ; en liste, c'est la liste "
         "vide qui porte ce sens, et 0 y serait un niveau inexistant")

# ── Le va-et-vient : ce qu'on accepte, ce qu'on refuse, ce qu'on normalise ───
_u = [n["uuid"] for n in db_get_tally_levels()]
controle("★★★ un scalaire hérité devient la liste à un élément",
         plugins.coerce_config("hello_world", {"tally_level": _u[0]})["tally_level"] == [_u[0]],
         "un conteneur configuré avant le multiselect doit continuer de tourner sans resave")
controle("★★★ les UUID SURVIVENT à la normalisation",
         plugins.coerce_config("hello_world", {"tally_level": _u[:2]})["tally_level"] == _u[:2],
         "une conversion en entier les jetterait tous, et le champ se viderait à chaque "
         "enregistrement sans le moindre message")
controle("★★ la liste est dédoublonnée, l'ordre gardé, les vides retirés",
         plugins.coerce_config("hello_world",
                               {"tally_level": [_u[1], _u[1], _u[0], 0, ""]})["tally_level"]
         == [_u[1], _u[0]],
         "0 n'est pas un niveau : c'était le code « celui du projet » du champ scalaire")
controle("★★ un choix valide passe",
         plugins.validate_config("hello_world", {"tally_level": _u[:2]}) == [])
controle("★★★ un niveau INEXISTANT est refusé, pas écrêté en silence",
         bool(plugins.validate_config("hello_world", {"tally_level": ["pas-un-niveau"]})),
         "règle du projet : on refuse avec un message, on ne rabote pas sans le dire — sinon "
         "l'exploitant croit avoir réglé quelque chose qui n'a pas pris")

# ── Les DEUX renderers doivent connaître le type ─────────────────────────────
# Un champ rendu par la palette et pas par le panneau ⚙ (ou l'inverse) donne un réglage qui
# disparaît selon la page d'où on l'ouvre — et rien ne le signale.
for chemin, quoi in (("static/scripts.js", "palette"),
                     ("templates/plugin_section.html", "panneau ⚙ des pages de plugin")):
    txt = open(os.path.join(RACINE, chemin), encoding="utf-8").read()
    controle("★★★ %s : le type `multiselect` est rendu" % quoi,
             "'multiselect'" in txt or '"multiselect"' in txt,
             "%s ne connaît pas le type : le champ tombe sur la branche texte, et le réglage "
             "est perdu à l'enregistrement" % chemin)
    # ⚠ ON EXIGE LE CONTRÔLE DE CATALOGUE, pas un `<select multiple>`. Celui-ci tient à quatre
    # entrées ; un site a autant de niveaux qu'il a de chaînes de destination, et à vingt il
    # devient inutilisable. Les deux rendus doivent monter LE MÊME contrôle, sinon un réglage se
    # comporte différemment selon la page d'où on l'ouvre.
    code = sans_commentaires(txt)
    controle("★★★ %s : c'est le contrôle de catalogue qui est monté" % quoi,
             "chooseList" in code and not re.search(r"<select[^>]*\bmultiple\b", code),
             "un `<select multiple>` demande de faire défiler une boîte de quatre lignes et un "
             "Ctrl+clic pour désélectionner, qui ne s'invente pas")
    controle("★★ %s : la LECTURE passe par le contrôle" % quoi,
             "_ctl" in txt and "value()" in txt,
             "lire la sélection dans le DOM obligerait à connaître le rendu du contrôle")

# ── Le conteneur affiche les NOMS, pas des numéros ───────────────────────────
import types                                                       # noqa: E402
sys.modules.setdefault("bobimxl", types.ModuleType("bobimxl"))
cas = [({"tally_level": ["a-1", "b-2"],
         "tally_level_noms": {"a-1": "1 — Antenne", "b-2": "3 — Plateau"}},
        "1 — Antenne, 3 — Plateau"),
       ({"tally_level": "c-3", "tally_level_noms": {"c-3": "7 — Régie"}}, "7 — Régie"),
       ({}, "ceux du projet")]
for cfg, attendu in cas:
    src = plugins.render_script("hello_world", cfg, "hw-banc")
    g = {"__name__": "x"}
    try:
        exec(compile(src.split("SHM_VIDEO = ")[0], "hw", "exec"), g)
        got = g.get("_NOM_NIV")
    except Exception as e:
        got = "exception : %s" % e
    controle("★★ le conteneur écrit « %s »" % attendu, attendu in str(got),
             "le nom vient de `before_deploy` : le conteneur n'a pas accès aux tables de "
             "l'orchestrateur, et un numéro nu oblige à aller voir ailleurs. Obtenu %r" % got)

# ── ET C'EST `before_deploy` QUI LES INJECTE ─────────────────────────────────
# ⚠ Ce contrôle manquait, et une mutation l'a montré : vider `tally_level_noms` dans le hook ne
# faisait broncher personne, parce que les contrôles ci-dessus passaient les noms DÉJÀ FAITS à
# `render_script`. On vérifiait donc le gabarit, pas la chaîne qui l'alimente — et c'est
# justement le maillon que le conteneur ne peut pas suppléer.
_niv = db_get_tally_levels()
if _niv:
    _hook = plugins.get_hook("hello_world", "before_deploy")
    _out = (_hook or (lambda p, c: p))({"tally_level": [_niv[0]["uuid"]]}, {}) or {}
    _noms = _out.get("tally_level_noms") or {}
    controle("★★★ `before_deploy` fait descendre le libellé LISIBLE dans les params",
             _noms.get(_niv[0]["uuid"])
             == "%d — %s" % (_niv[0].get("num") or 0, _niv[0].get("nom") or "?"),
             "sans ce hook le conteneur n'a qu'un UUID, qui ne dit rien devant un mur : il ne "
             "peut pas joindre les tables de "
             "l'orchestrateur. Obtenu %r" % _noms)
    controle("★★ il n'embarque QUE les niveaux suivis",
             len(_noms) == 1,
             "expédier toute la liste du site la périme au premier renommage et la fait grossir "
             "à chaque production créée. Obtenu %d entrées" % len(_noms))

# ── LA PAGE DU PLUGIN, qui a son PROPRE sélecteur ────────────────────────────
# ⚠ CE BLOC MANQUAIT, et c'est exactement là que le défaut a survécu : le manifeste était bien
# passé en multi-sélection, mais `control.js` gardait son menu à un choix, alimenté par une liste
# figée `[0,1,2,3,4]` — les numéros de bande de la trame TSL, écrits dans la page. On regardait le
# manifeste et on concluait que c'était fait.
import json as _json                                                   # noqa: E402
import re as _re                                                       # noqa: E402
import subprocess as _sp                                               # noqa: E402
import tempfile as _tf                                                 # noqa: E402

_js = open(os.path.join(RACINE, "plugins", "hello_world", "control.js"), encoding="utf-8").read()
_code = sans_commentaires(_js)
# ⚠ ON CHERCHE L'ATTRIBUT, PAS LE MOT. « multiple » apparaît aussi dans un texte d'aide sans
# rapport (la sélection à la souris) : chercher le mot nu faisait échouer le banc sur une phrase
# d'interface.
controle("★★★ la page du plugin monte le contrôle de catalogue",
         "MXLControls.chooseList" in _code and not re.search(r"<select[^>]*\bmultiple\b", _code),
         "le manifeste ne suffit pas : la page du plugin a son propre contrôle, et c'est "
         "celui-là que l'exploitant utilise")
controle("★★★ plus aucune liste de niveaux FIGÉE dans la page",
         "[1, 2, 3, 4].map" not in _code and 'T("nivn"' not in _code,
         "elle listait `[0,1,2,3,4]` — les numéros de bande de la trame TSL, recopiés dans la "
         "page, avec un plafond de quatre et des numéros qui ne veulent plus rien dire")
controle("★★★ elle envoie une LISTE, à chaque ajout ou retrait",
         "onChange: (v) => envoyerConfig(\"tally_level\", v)" in _js,
         "le contrôle rend une liste ; envoyer autre chose perdrait tout sauf un élément")
controle("★★ et la liste des niveaux vient de /state, pas d'une route",
         "tally_levels_dispo" in _js,
         "une route de l'orchestrateur échouerait derrière un jeton public, où la page doit "
         "rester lisible en lecture seule")

# Le conteneur doit publier les deux : ce qui est CHOISI, et ce qui est CHOISISSABLE.
_sc = plugins.render_script("hello_world",
                            {"tally_level": ["u-1"],
                             "tally_levels_dispo": [{"uuid": "u-1", "label": "1 — Antenne"}]},
                            "hw-banc")
controle("★★★ `/state` publie le choix en LISTE, sans `int()` dessus",
         'st["tally_level"] = list(_tl)' in _sc and 'int(CONFIG.get("tally_level")' not in _sc,
         "`int()` sur une liste LÈVE, et emportait toute la réponse /state dès qu'un niveau "
         "était affecté — la page entière devenait muette")
controle("★★ ...et la liste des niveaux disponibles",
         'st["tally_levels_dispo"]' in _sc)

_hk = open(os.path.join(RACINE, "plugins", "hello_world", "hooks.py"), encoding="utf-8").read()
controle("★★ `before_deploy` fait descendre la liste choisissable",
         "tally_levels_dispo" in _hk,
         "le conteneur n'a pas accès aux tables de l'orchestrateur : sans cette injection, le "
         "menu de la page serait vide")

# Les clés d'options du catalogue ne doivent plus traîner : elles décrivaient l'ancien modèle.
for _f in ("fr", "en"):
    _cat = _json.load(open(os.path.join(RACINE, "plugins", "hello_world", "i18n",
                                        "%s.json" % _f), encoding="utf-8"))
    controle("★ catalogue %s : plus de clés d'option héritées" % _f,
             not [k for k in _cat if k.startswith("type.hello_world.cfg.tally_level.opt.")],
             "elles nommaient « Niveau 1 » … « Niveau 4 » : un vocabulaire que le modèle a "
             "abandonné, et qu'un traducteur aurait entretenu pour rien")
    # On teste le FAIT, pas une heuristique de langue : l'ancien libellé ne doit plus être là,
    # et l'aide doit annoncer le choix multiple. Une première version cherchait « s » dans la
    # chaîne — ça ne prouve rien et ça se trompe de langue.
    _lbl = _cat.get("type.hello_world.cfg.tally_level.label", "")
    _aide = _cat.get("type.hello_world.cfg.tally_level.help", "")
    controle("★★ catalogue %s : l'ancien libellé au singulier a disparu" % _f,
             _lbl not in ("Niveau de tally suivi", "Tally level followed") and bool(_lbl),
             "c'est ce que lit l'exploitant, et il décrivait l'ancien comportement. Obtenu %r"
             % _lbl)
    controle("★★★ catalogue %s : l'aide annonce le choix MULTIPLE" % _f,
             "PLUSIEURS" in _aide or "SEVERAL" in _aide,
             "un menu multiple ne se devine pas : sans un mot, on choisit une ligne et on s'en "
             "va. Obtenu %r" % _aide[:90])
    controle("★★ catalogue %s : l'aide ne parle plus de « 0 = hérité »" % _f,
             "0 = " not in _aide,
             "c'était le code d'un champ scalaire ; en liste, 0 n'est pas un niveau — et cette "
             "phrase enseignait un modèle qui n'existe plus")

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
