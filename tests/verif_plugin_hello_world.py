#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Vérifie que le plugin d'EXEMPLE `hello_world` honore encore le contrat qu'il
# est censé enseigner.
#
# POURQUOI CE FICHIER EXISTE. Une documentation que rien n'exécute dérive, et
# personne ne s'en aperçoit : `plugins/AUTHORING.md` a passé trois mois à décrire
# un contrat qui avait changé sous lui. Un plugin d'exemple a exactement le même
# défaut par défaut — sauf s'il est VÉRIFIÉ. Si le contrat évolue sans que
# l'exemple suive, ce banc échoue, et la CI avec lui.
#
# Ce qu'il vérifie est délibérément le CONTRAT, pas le style : chaque contrôle
# correspond à une règle dont l'oubli produit une panne SILENCIEUSE.
#
#   $ ./venv/bin/python tools/verif_plugin_hello_world.py
import json
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOSSIER = os.path.join(RACINE, "plugins", "hello_world")

echecs = []
reussites = []


def controle(intitule, condition, explication=""):
    if condition:
        reussites.append(intitule)
    else:
        echecs.append((intitule, explication))


def lire(nom):
    with open(os.path.join(DOSSIER, nom), encoding="utf-8") as f:
        return f.read()


# ── 0. Le plugin existe et se charge ─────────────────────────────────────────
if not os.path.isdir(DOSSIER) or not os.path.isfile(os.path.join(DOSSIER, "plugin.json")):
    # ⚠ DEUX PANNES TRÈS DIFFÉRENTES, ET LE MÊME DOSSIER VIDE. `hello_world` est un
    # SOUS-MODULE : un clone sans `--recursive` laisse le dossier là, vide. Dire
    # « l'exemple a disparu » enverrait chercher un fichier supprimé alors qu'il
    # suffit d'initialiser le sous-module. On distingue donc les deux cas.
    if os.path.isdir(os.path.join(RACINE, ".git")) and os.path.isfile(
            os.path.join(RACINE, ".gitmodules")):
        print("ÉCHEC : plugins/hello_world est vide — sous-module non initialisé.\n"
              "        git submodule update --init plugins/hello_world", file=sys.stderr)
    else:
        print("ÉCHEC : plugins/hello_world est absent — l'exemple de référence a disparu.",
              file=sys.stderr)
    sys.exit(1)

manifeste = json.loads(lire("plugin.json"))
script = lire("script.py")
controle_js = lire("control.js")

# ── 1. Gabarit str.format : il doit se rendre ET compiler ────────────────────
# `.format` seul ne suffit pas : un script qui se rend peut très bien ne pas
# compiler, et le conteneur boucle alors en silence.
params = dict(manifeste.get("deploy_defaults") or {})
try:
    rendu = script.format(config=repr(params), hostname="hello-1",
                          plugin_version=manifeste.get("version", "0"))
    controle("le gabarit str.format se rend (accolades doublées)", True)
except Exception as e:
    rendu = ""
    controle("le gabarit str.format se rend (accolades doublées)", False,
             "une accolade littérale n'est pas doublée : %r" % (e,))

if rendu:
    try:
        compile(rendu, "<hello_world>", "exec")
        controle("le script rendu compile", True)
    except SyntaxError as e:
        controle("le script rendu compile", False,
                 "SyntaxError ligne %s : %s" % (e.lineno, e.msg))

# ── 2. Mode tranche — obligatoire pour tout nouveau plugin ───────────────────
controle("le mode tranche est actif par défaut",
         manifeste.get("deploy_defaults", {}).get("slice_mode") is True,
         "deploy_defaults.slice_mode doit valoir true : l'exemple doit montrer la règle, pas l'exception")
controle("slice_mode est masqué dans le schéma de config",
         any(c.get("key") == "slice_mode" and c.get("hidden") is True
             for c in manifeste.get("config_schema") or []),
         "le réglage qui compte est le commutateur global Réglages → Vidéo")
controle("le script lit son entrée par tranches",
         "get_slice(" in script,
         "sans get_slice, l'étage attend la trame entière")
controle("le script publie en commit progressif",
         "valid_slices=" in script,
         "sans valid_slices, l'aval attend la trame entière — le gain du mode tranche est perdu")
controle("le repli image entière est explicite",
         "slice_height_pour" in script and "return 0" in script,
         "une hauteur sans diviseur doit retomber en image entière, pas produire des bandes bancales")

# ── 3. Exposition aux macros — sinon la capacité est morte ───────────────────
pt = (manifeste.get("param_tree") or {}).get("global_groups") or []
champs = {c for g in pt for c in (g.get("fields") or {})}
controle("les paramètres continus sont exposés en param_tree",
         len(champs) >= 2,
         "un paramètre réglable à chaud non exposé est une capacité que nulle macro ne peut atteindre")
actions = manifeste.get("actions") or []
controle("des actions discrètes sont déclarées",
         len(actions) >= 2,
         "une action non déclarée n'est pilotable que par un humain devant l'écran")
cibles = {a.get("endpoint") for a in actions} | {g.get("endpoint") for g in pt}
declares = set(manifeste.get("control", {}).get("endpoints") or [])
manquants = sorted(t for t in cibles if t and t not in declares)
controle("chaque cible de macro est déclarée dans control.endpoints",
         not manquants,
         "le proxy REFUSE un chemin non déclaré : %s" % ", ".join(manquants))

# ── 4. Observabilité — dire si l'étage fait ce qu'on a demandé ───────────────
# ⚠ CE BLOC A ÉTÉ DURCI APRÈS COUP. Sa première version cherchait le nom de la
# métrique N'IMPORTE OÙ dans le fichier : retirer la clé du dictionnaire publié
# la laissait passer, parce que le nom subsistait dans la boucle. Un banc qui ne
# peut pas échouer ne vérifie rien — y compris quand c'est le banc d'un exemple
# censé enseigner cette règle. On lit donc l'AST : la clé doit être DÉCLARÉE dans
# le dictionnaire `metrics` ET mise à jour ailleurs.
def _cles_du_dict_metrics(source):
    import ast
    try:
        arbre = ast.parse(source)
    except SyntaxError:
        return None, set()
    declarees, affectees = set(), set()
    for n in ast.walk(arbre):
        if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)
                and any(isinstance(c, ast.Name) and c.id == "metrics" for c in n.targets)):
            declarees = {k.value for k in n.value.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                and n.value.id == "metrics" and isinstance(n.slice, ast.Constant)):
            affectees.add(n.slice.value)
    return declarees, affectees

_declarees, _affectees = _cles_du_dict_metrics(rendu) if rendu else (set(), set())
for champ, raison in (("slice_mode", "sinon on ne sait pas si la sortie est RÉELLEMENT tranchée"),
                      ("own_latency_ms", "sinon on ne connaît pas la marge de l'étage"),
                      ("source", "sinon on ne sait pas d'où vient l'image")):
    controle("la métrique « %s » est déclarée ET mise à jour" % champ,
             champ in (_declarees or set()) and champ in _affectees,
             raison + (" (déclarée: %s, mise à jour: %s)"
                       % (champ in (_declarees or set()), champ in _affectees)))
controle("l'état est lisible en condition de macro",
         "/state" in (manifeste.get("control", {}).get("read_endpoints") or []),
         "un état non publié est un état sur lequel aucun automatisme ne peut décider")

# ── 4 bis. Les trois essences, en entrée comme en sortie ─────────────────────
# Un plugin mono-essence n'apprend pas à en câbler trois : c'est justement là que
# les erreurs coûtent (audio muet, ANC perdu, entrée confondue avec une autre).
w = manifeste.get("wiring") or {}
ess_in = {c.get("essence") for c in (w.get("consumes") or [])}
ess_out = {p.get("essence") for p in (w.get("produces") or [])}
for e in ("video", "audio", "data"):
    controle("l'essence « %s » est consommée" % e, e in ess_in,
             "l'exemple doit montrer comment on câble cette essence")
    controle("l'essence « %s » est produite" % e, e in ess_out,
             "l'exemple doit montrer comment on la republie")
controle("chaque entrée déclare son state_field",
         all(c.get("state_field") for c in (w.get("consumes") or [])),
         "sans state_field, l'orchestrateur ne sait pas quel champ pousser au câblage à chaud")
controle("l'ANC est écrit au format normatif RFC 8331",
         "anc_pack_rfc8331" in script,
         "un format maison fait conclure « 0 paquet ANC » à un SDK stock, SANS erreur : "
         "perte silencieuse du timecode, du tally et des sous-titres")
controle("l'audio absent est remplacé par du SILENCE",
         "np.zeros((n_ech, CANAUX)" in script,
         "un aval qui attend de l'audio doit en recevoir, sinon il déduit une panne inexistante")
controle("une entrée câblée mais muette est distinguée d'une entrée absente",
         '"no signal"' in script and '"not wired"' in script,
         "les confondre envoie l'exploitant vérifier un câblage qui est bon")
controle("la latence est publiée PAR entrée",
         "inputs_latency_ms" in script,
         "avec trois essences, un chiffre global ne dit pas laquelle décroche")

# ── 5. Robustesse — les pannes de ce produit sont silencieuses ───────────────
controle("SIGBUS est intercepté",
         "SIGBUS" in script,
         "un producteur qui recrée son flux tue sinon le processus, et Docker le relance en boucle")
controle("la boucle survit à une exception",
         "except Exception" in script,
         "une exception non rattrapée fait redémarrer le conteneur sans jamais dire pourquoi")

# ── 6. Page publique — le contrat que l'orchestrateur exige ──────────────────
controle("le manifeste déclare ui.public_page",
         (manifeste.get("ui") or {}).get("public_page") is True,
         "sans cette déclaration l'orchestrateur REFUSE de créer un lien public")
controle("la console honore ctx.base par une fonction unique",
         "ctx.base" in controle_js and "const api" in controle_js,
         "un seul `if (public)` oublié appelle l'API privée et échoue en 401 sans explication")
controle("la console arrête son sondage au démontage",
         "clearInterval" in controle_js,
         "sinon le sondage survit à la page et se cumule à chaque montage")

# ── Verdict ──────────────────────────────────────────────────────────────────
print("hello_world — vérification du contrat de plugin\n")
for r in reussites:
    print("  OK    %s" % r)
if echecs:
    print("", file=sys.stderr)
    for intitule, explication in echecs:
        print("  ÉCHEC %s" % intitule, file=sys.stderr)
        if explication:
            print("        → %s" % explication, file=sys.stderr)
    print("\n%d contrôle(s) en échec : l'exemple de référence ne montre plus le contrat qu'il "
          "prétend enseigner.\nCorrigez plugins/hello_world, ou ce banc si c'est le CONTRAT qui "
          "a changé." % len(echecs), file=sys.stderr)
    sys.exit(1)

print("\nOK : %d contrôles passés — l'exemple honore le contrat." % len(reussites))
sys.exit(0)
