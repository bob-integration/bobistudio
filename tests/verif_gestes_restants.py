#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Les GESTES qui restent après une installation : mesurés, nommés, et annoncés aux deux endroits.

★ LE CONSTAT (installation d'essai en VM, 2026-11-08). Un tout-en-un s'installe en deux minutes
et annonce « Tout-en-un prêt ». Rien n'est déployable : aucune image d'exécution n'est présente,
parce que le chemin tout-en-un n'appelle jamais `install-node.sh --registry`. L'absence d'image ne
produit qu'un `warn`, une quarantaine de lignes avant le verdict, et ce `warn` ne nomme aucun
endroit du produit. Et l'assistant de premier démarrage, lui, annonçait « les deux gestes qui
suivent » ÉCRITS EN DUR : enrôler un nœud — que l'installeur venait de faire — et désigner une
carte réseau — inutile en mode interne. Le seul geste qui débloque réellement la machine, la
construction des images, n'était annoncé nulle part.

★ POURQUOI UNE LISTE EN DUR NE PEUT PAS ÊTRE JUSTE. Le même écran sert un contrôleur seul, un
nœud seul et un tout-en-un. Une liste figée est donc fausse dans deux cas sur trois : elle réclame
du déjà-fait (ce qui apprend à ne plus la lire) et taît ce qui manque. D'où la règle que cette
garde tient : la liste est MESURÉE à un seul endroit (`app/install_gestures.py`), et les DEUX
lecteurs — l'assistant et l'installeur — la lisent au lieu de la recopier.

Ce qu'elle tient :
1. ★★ sans nœud, UN geste (`node`) et rien d'autre : réseau et images sont des propriétés d'un
   nœud, les annoncer sans nœud serait deviner ;
2. ★★ images attendues et absentes → geste `images`, avec le TAG nommé (c'est le geste qui
   manquait) ; images présentes → aucun geste bloquant ;
3. ★★ le tri-état du réseau est CONSERVÉ : `present=None` (agent muet) n'est PAS un geste, il est
   publié comme non mesuré. Confondre les deux fait passer « on ne sait pas » pour « rien à
   faire », sur la machine où les sondes échouent le plus ;
4. ★ un inventaire d'images incertain (`unknown`) est non mesuré, pas un geste ;
5. ★★ la passerelle WebRTC est un geste NON BLOQUANT, et son lien va sur Services — l'onglet
   `#webrtc` n'existe PAS tant que le service est éteint, donc justement sur la machine neuve ;
6. ★★ tout code de geste a ses deux clés i18n dans LES DEUX catalogues, et tout `{param}` de ces
   libellés est réellement fourni (un libellé qui garde un `{tags}` brut à l'écran est pire que
   pas de libellé) ;
7. ★★ l'assistant n'écrit AUCUN geste en dur : il appelle /api/setup/gestures ;
8. ★★ l'installeur annonce la liste dans les DEUX chemins qui installent un contrôleur, et ne
   transforme pas un échec de lecture en « rien à faire » ;
9. ★ deux situations d'image, deux alertes : jamais construite ≠ distribution perdue.

Hors ligne : base jetable, aucun nœud joignable, aucune image, aucun réseau Docker.

MUTATIONS (GESTES_MUTATION=<nom>) — chacune doit donner rc=1 :
    geste_deja_fait     les images présentes produisent quand même le geste
    geste_manquant_tu   les images absentes ne produisent plus de geste
    tri_etat_ecrase     `present=None` est traité comme « réseau absent »
    flou_avale          les non-mesurés sont jetés au lieu d'être publiés
    lien_webrtc_mort    le geste WebRTC pointe sur l'onglet qui n'existe pas
    wizard_en_dur       l'assistant réécrit la liste en dur
    installeur_muet     l'installeur n'annonce plus les gestes
    alerte_unique       une seule alerte pour les deux situations d'image
    mtls_sur_la_ca      le geste mTLS se mesure sur la CA, pas sur `tls_ready`

    ./venv/bin/python tests/verif_gestes_restants.py
"""
import importlib.util
import json
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                        # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_gestes_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

MUT = os.environ.get("GESTES_MUTATION", "")
echecs, total = [], [0]


def verifie(nom, cond, detail=""):
    total[0] += 1
    if cond:
        print(f"  \033[32m✓\033[0m {nom}")
    else:
        print(f"  \033[31m✗\033[0m {nom}" + (f"  → {detail}" if detail else ""))
        echecs.append(nom)


# ─── Le module sous test, chargé depuis sa SOURCE pour pouvoir la muter ──────
# Charger la source plutôt qu'importer permet de RE-CRÉER le défaut dans le code lui-même, pas
# dans une doublure : une mutation qui ne touche que le harnais ne prouverait rien.
_SRC_GESTES = os.path.join(RACINE, "app", "install_gestures.py")

_MUTATIONS_SOURCE = {
    "geste_deja_fait": ('        if v.get("present"):\n            continue\n',
                        '        if False:\n            continue\n'),
    "geste_manquant_tu": ('    if a_construire:\n', '    if False and a_construire:\n'),
    "tri_etat_ecrase": ('            if present is None:\n'
                        '                unmeasured.append({"code": "network", "node": nom})\n'
                        '            elif not present:\n'
                        '                sans_reseau.append(nom)\n',
                        '            if not present:\n'
                        '                sans_reseau.append(nom)\n'),
    "flou_avale": ('    return {"gestures": gestures, "unmeasured": unmeasured}\n',
                   '    return {"gestures": gestures, "unmeasured": []}\n'),
    "lien_webrtc_mort": ('"link": "/settings#services", "params": {}}',
                         '"link": "/settings#webrtc", "params": {}}'),
    # Le défaut que j'avais écrit moi-même : mesurer la CA au lieu de l'état du nœud.
    "mtls_sur_la_ca": ('and not node.get("tls_ready")]',
                       'and not node.get("tls_ready") and False]'),
}


def charger_gestes():
    src = open(_SRC_GESTES, encoding="utf-8").read()
    if MUT in _MUTATIONS_SOURCE:
        avant, apres = _MUTATIONS_SOURCE[MUT]
        assert avant in src, f"mutation {MUT} : motif introuvable (la source a bougé)"
        src = src.replace(avant, apres, 1)
    spec = importlib.util.spec_from_file_location("app._gestes_sous_test", _SRC_GESTES)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "app"            # les imports relatifs du module doivent résoudre
    exec(compile(src, _SRC_GESTES, "exec"), mod.__dict__)
    return mod


G = charger_gestes()

from app import database as db                                          # noqa: E402
from app.routes import images as mod_images                             # noqa: E402
from app import node_driver, monitor                                    # noqa: E402


# ─── Doublures de BORD, fidèles à l'auteur ───────────────────────────────────
# ⚠ Une doublure qui ne peut pas contredire son auteur ne mesure rien : on vérifie d'abord que
# les deux fonctions remplacées existent et rendent bien la forme qu'on imite (section 0).
_RES = {}        # node_id -> valeur de `present` (True / False / None)
_IMGS = {}       # node_id -> which -> {tag, present, expected, unknown?}


def _faux_reseau(node, force=False):
    return {"declare": "", "reel": "", "porteuse": None,
            "present": _RES.get((node or {}).get("id")),
            "derive": False, "sans_lien": False}


def _faux_images(node):
    return dict(_IMGS.get((node or {}).get("id")) or {})


node_driver.etat_reseau_conteneurs = _faux_reseau
mod_images.node_images_state = _faux_images
monitor.gateway_ready = lambda: False

print("\n── 0. les doublures imitent des fonctions qui EXISTENT ─────────────")
import inspect                                                          # noqa: E402

_reel_res = inspect.signature(
    inspect.unwrap(getattr(node_driver, "etat_reseau_conteneurs")))
verifie("0a. `install_gestures` lit bien les deux sources documentées",
        "etat_reseau_conteneurs" in open(_SRC_GESTES, encoding="utf-8").read()
        and "node_images_state" in open(_SRC_GESTES, encoding="utf-8").read())
_src_images = open(os.path.join(RACINE, "app", "routes", "images.py"), encoding="utf-8").read()
verifie("0b. ★ la vraie `node_images_state` rend bien {tag, present, expected}",
        '"tag": tag, "present": bool(ok), "expected": bool(exp)' in _src_images)
verifie("0c. ★ la vraie `etat_reseau_conteneurs` publie un tri-état `present`",
        '"present": None' in open(os.path.join(RACINE, "app", "node_driver.py"),
                                  encoding="utf-8").read())

print("\n── 1. aucun nœud : UN geste, et rien d'inventé ─────────────────────")
r = G.remaining_gestures()
codes = [g["code"] for g in r["gestures"]]
verifie("1a. ★★ sans nœud → le seul geste est « enrôler un nœud »", codes == ["node"], codes)
verifie("1b. ★ et il est bloquant", all(g["blocking"] for g in r["gestures"]))
verifie("1c. ★★ ni réseau ni images annoncés (ils n'ont pas de nœud où être mesurés)",
        "network" not in codes and "images" not in codes, codes)

print("\n── 2. images : le geste qui manquait ───────────────────────────────")
nid = db.db_add_node("essai", "192.0.2.10", docker_network="bobi-interne")
db.db_update_node(nid, agent_url="http://192.0.2.10:9100", capabilities=json.dumps(["compute"]))
_RES[nid] = True

_IMGS[nid] = {"compute": {"tag": "", "present": False, "expected": True},
              "media": {"tag": "", "present": False, "expected": False}}
r = G.remaining_gestures()
g_img = next((g for g in r["gestures"] if g["code"] == "images"), None)
verifie("2a. ★★ image attendue et absente → geste `images`", g_img is not None)
verifie("2b. ★★ le TAG est nommé (sinon on envoie construire quoi ?)",
        bool(g_img) and "bobi-compute" in (g_img["params"].get("tags") or ""),
        bool(g_img) and g_img["params"])
verifie("2c. ★ le nœud est nommé", bool(g_img) and g_img["params"].get("nodes") == "essai")
verifie("2d. ★ et c'est bloquant : rien ne démarre sans image",
        bool(g_img) and g_img["blocking"] is True)
verifie("2e. ★ le lien mène à l'onglet Build existant",
        bool(g_img) and g_img["link"] == "/settings#deploiement/build",
        bool(g_img) and g_img["link"])

_IMGS[nid] = {"compute": {"tag": "bobi-compute:0.81", "present": True, "expected": True}}
r = G.remaining_gestures()
verifie("2f. ★★ images présentes → AUCUN geste bloquant",
        not [g for g in r["gestures"] if g["blocking"]],
        [g["code"] for g in r["gestures"] if g["blocking"]])
verifie("2g. ★ une image non attendue ne produit jamais de geste",
        True if "images" not in [g["code"] for g in r["gestures"]] else False)

print("\n── 3. réseau : le tri-état est conservé ────────────────────────────")
_RES[nid] = False
r = G.remaining_gestures()
g_net = next((g for g in r["gestures"] if g["code"] == "network"), None)
verifie("3a. ★ réseau mesuré ABSENT → geste `network` bloquant",
        bool(g_net) and g_net["blocking"] is True)
verifie("3b. ★ il nomme le nœud et mène au panneau du nœud",
        bool(g_net) and g_net["params"].get("nodes") == "essai"
        and g_net["link"] == "/settings#deploiement/nodes")

_RES[nid] = None
r = G.remaining_gestures()
codes = [g["code"] for g in r["gestures"]]
flous = [f["code"] for f in r["unmeasured"]]
verifie("3c. ★★ `present=None` n'est PAS un geste (« on ne sait pas » ≠ « à faire »)",
        "network" not in codes, codes)
verifie("3d. ★★ et il est PUBLIÉ comme non mesuré (une abstention muette = une abstention fausse)",
        "network" in flous, flous)

print("\n── 4. inventaire d'images incertain ────────────────────────────────")
_RES[nid] = True
_IMGS[nid] = {"compute": {"tag": "bobi-compute:0.81", "present": False,
                          "expected": True, "unknown": True}}
r = G.remaining_gestures()
verifie("4a. ★ `unknown` → aucun geste affirmé",
        "images" not in [g["code"] for g in r["gestures"]])
verifie("4b. ★★ `unknown` → publié comme non mesuré",
        "images" in [f["code"] for f in r["unmeasured"]], r["unmeasured"])

print("\n── 5. passerelle WebRTC : utile, pas bloquante, et le lien VIT ─────")
_IMGS[nid] = {"compute": {"tag": "bobi-compute:0.81", "present": True, "expected": True}}
r = G.remaining_gestures()
g_w = next((g for g in r["gestures"] if g["code"] == "webrtc_gateway"), None)
verifie("5a. ★ passerelle absente → geste annoncé", g_w is not None)
verifie("5b. ★★ NON bloquant (le monitoring est noir, rien n'est cassé)",
        bool(g_w) and g_w["blocking"] is False)
# ⚠ `#webrtc` n'existe pas tant que `webrtc_enabled` est faux : `routes/pages.py` RETIRE des
# onglets tout service dont la clé `*_enabled` est fausse. Donc le lien doit viser Services.
verifie("5c. ★★ le lien vise Services, pas l'onglet absent d'une installation neuve",
        bool(g_w) and g_w["link"] == "/settings#services", bool(g_w) and g_w["link"])
_src_pages = open(os.path.join(RACINE, "app", "routes", "pages.py"), encoding="utf-8").read()
verifie("5d. ★ et la raison est toujours vraie dans le code (service éteint = onglet retiré)",
        'endswith("_enabled")' in _src_pages and "disabled.add" in _src_pages)
monitor.gateway_ready = lambda: True
r = G.remaining_gestures()
verifie("5e. ★ passerelle prête → le geste disparaît",
        "webrtc_gateway" not in [g["code"] for g in r["gestures"]])
monitor.gateway_ready = lambda: False

print("\n── 5 bis. mTLS : mesuré sur `tls_ready`, pas sur la présence de la CA ──")
# ⚠ Premier jet mesuré sur `ca.ca_available()` : sur l'installation d'essai la CA EXISTAIT et le
# nœud était à `tls_ready=0` — le geste se taisait précisément quand il y avait à faire.
db.db_update_node(nid, tls_ready=0)
r = G.remaining_gestures()
g_tls = next((g for g in r["gestures"] if g["code"] == "control_plane_tls"), None)
verifie("5f. ★★ nœud en HTTP clair → geste annoncé même si la CA existe", g_tls is not None)
verifie("5g. ★ non bloquant, nœud nommé, lien vers Sécurité",
        bool(g_tls) and g_tls["blocking"] is False
        and g_tls["params"].get("nodes") == "essai" and g_tls["link"] == "/settings#securite",
        bool(g_tls) and g_tls)
db.db_update_node(nid, tls_ready=1)
verifie("5h. ★★ nœud migré → le geste disparaît",
        "control_plane_tls" not in [g["code"] for g in G.remaining_gestures()["gestures"]])
# Hors commentaires : la mention de l'erreur passée doit rester lisible dans le code, c'est
# l'APPEL qui ne doit plus exister.
_code_nu = "\n".join(l for l in open(_SRC_GESTES, encoding="utf-8").read().splitlines()
                     if not l.lstrip().startswith("#"))
verifie("5i. ★★ la mesure n'est PAS `ca_available` (elle était muette quand il fallait agir)",
        "ca_available()" not in _code_nu and "tls_ready" in _code_nu)

print("\n── 6. contrat i18n de CHAQUE code de geste ────────────────────────")
fr = json.load(open(os.path.join(RACINE, "i18n", "fr.json"), encoding="utf-8"))
en = json.load(open(os.path.join(RACINE, "i18n", "en.json"), encoding="utf-8"))
# Tous les codes que le module sait produire, lus dans sa source : une liste recopiée ici
# oublierait le prochain code ajouté — exactement la panne qu'on corrige.
# ⚠ `[a-z0-9_]` et pas `[a-z_]` : le premier jet ratait `io2110_reboot` — un code avec un
# CHIFFRE devenait invisible, donc dispensé du contrat i18n. Attrapé par 6a, qui compare la liste
# lue à celle qu'on croit avoir.
codes_connus = sorted(set(re.findall(r'"code":\s*"([a-z0-9_]+)"',
                                     open(_SRC_GESTES, encoding="utf-8").read())))
verifie("6a. ★ les six codes sont bien ceux qu'on croit",
        codes_connus == ["control_plane_tls", "images", "io2110_reboot", "network",
                         "node", "webrtc_gateway"], codes_connus)
for code in codes_connus:
    for suffixe in ("", "_link"):
        cle = f"wiz.done.geste_{code}{suffixe}"
        verifie(f"6b. ★★ `{cle}` est dans LES DEUX catalogues",
                cle in fr and cle in en,
                f"fr={cle in fr} en={cle in en}")
# Un libellé qui garde un « {tags} » brut à l'écran est pire que pas de libellé : on vérifie que
# tout paramètre attendu par le texte est réellement fourni par le module, dans les deux langues.
_PARAMS_FOURNIS = {"node": set(), "network": {"nodes"}, "images": {"nodes", "tags"},
                   "io2110_reboot": {"nodes"}, "control_plane_tls": {"nodes"},
                   "webrtc_gateway": set()}
for code in codes_connus:
    attendus = set()
    for cat in (fr, en):
        attendus |= set(re.findall(r"\{(\w+)\}", cat.get(f"wiz.done.geste_{code}", "")))
    verifie(f"6c. ★★ `{code}` : tout {{param}} du libellé est fourni",
            attendus <= _PARAMS_FOURNIS[code], f"manquants : {sorted(attendus - _PARAMS_FOURNIS[code])}")
for cle in ("wiz.done.next_none", "wiz.done.next_unmeasured", "wiz.done.next_title"):
    verifie(f"6d. ★ `{cle}` existe des deux côtés", cle in fr and cle in en)
verifie("6e. ★★ aucune trace des clés en dur retirées (geste déjà fait réclamé)",
        not any(k.startswith("wiz.done.next_node") or k.startswith("wiz.done.next_iface")
                for k in fr) and
        not any(k.startswith("wiz.done.next_node") or k.startswith("wiz.done.next_iface")
                for k in en))
verifie("6f. ★ le titre ne compte plus les gestes (« les deux gestes » était faux)",
        "deux" not in fr["wiz.done.next_title"].lower()
        and "two" not in en["wiz.done.next_title"].lower(),
        fr["wiz.done.next_title"])

print("\n── 7. l'assistant LIT la liste, il ne la réécrit pas ───────────────")
_wiz = open(os.path.join(RACINE, "templates", "setup_wizard.html"), encoding="utf-8").read()
if MUT == "wizard_en_dur":
    _wiz = _wiz.replace("/api/setup/gestures", "/api/setup/complete").replace(
        "wiz.done.geste_", "wiz.done.next_node")
verifie("7a. ★★ la dernière étape appelle /api/setup/gestures",
        "/api/setup/gestures" in _wiz)
verifie("7b. ★★ elle rend les codes mesurés (`wiz.done.geste_` + code), pas une liste écrite",
        "wiz.done.geste_" in _wiz)
verifie("7c. ★★ aucun geste n'est encore écrit en dur dans le gabarit",
        "wiz.done.next_node" not in _wiz and "wiz.done.next_iface" not in _wiz)
verifie("7d. ★★ un appel en échec ne devient pas « rien à faire »",
        "unmeasured: [{ code: 'all' }]" in _wiz)
verifie("7e. ★ la route existe et exige d'être connecté",
        re.search(r'@bp\.route\("/api/setup/gestures".*?\n@require_login\n\s*def ',
                  open(os.path.join(RACINE, "app", "routes", "auth_pages.py"),
                       encoding="utf-8").read(), re.S) is not None)

print("\n── 8. l'installeur annonce la MÊME liste ──────────────────────────")
_inst = open(os.path.join(RACINE, "install", "install.py"), encoding="utf-8").read()
if MUT == "installeur_muet":
    _inst = _inst.replace("    dire_gestes_restants()\n", "")
verifie("8a. ★★ le tout-en-un l'annonce",
        re.search(r"def path_all_in_one.*?dire_gestes_restants\(\)", _inst, re.S) is not None)
verifie("8b. ★★ l'orchestrateur seul aussi",
        re.search(r"def path_controller.*?dire_gestes_restants\(\)", _inst, re.S) is not None)
verifie("8c. ★★ la liste vient du produit, pas d'une copie dans l'installeur",
        "install_gestures" in _inst and "remaining_gestures" in _inst)
verifie("8d. ★★ un calcul illisible est DIT, pas transformé en « rien à faire »",
        'warn(t("gestes_bad"))' in _inst)
import ast                                                              # noqa: E402

_STR = {}
for _n in ast.walk(ast.parse(_inst)):
    if isinstance(_n, ast.Assign) and any(getattr(c, "id", "") == "STR" for c in _n.targets):
        _STR = ast.literal_eval(_n.value)
_cles = ("gestes_title", "gestes_none", "gestes_flous", "gestes_bad")
verifie("8e. ★ les quatre libellés de l'installeur sont dans ses deux langues",
        all(k in _STR.get("fr", {}) and k in _STR.get("en", {}) for k in _cles),
        {k: (k in _STR.get("fr", {}), k in _STR.get("en", {})) for k in _cles})
verifie("8f. ★ et l'installeur, qui n'a pas le catalogue, lit les libellés DANS le produit",
        '"wiz.done.geste_"' not in _inst and "'wiz.done.geste_'" in _inst)

print("\n── 9. deux situations d'image, deux alertes ───────────────────────")
_src_img = _src_images
if MUT == "alerte_unique":
    _src_img = _src_img.replace("alert.image.jamais_construite", "alert.image.derive_noeud")
verifie("9a. ★★ « jamais construite » a son propre code d'alerte",
        "alert.image.jamais_construite" in _src_img)
verifie("9b. ★★ les deux codes coexistent (le remède n'est pas le même)",
        "alert.image.derive_noeud" in _src_img and "alert.image.jamais_construite" in _src_img)
verifie("9c. ★★ le départage est la COLONNE du nœud, pas une devinette",
        re.search(r"used = _used_tag\(which, node\)\n", _src_img) is not None
        and re.search(r"\(manquantes if used else jamais\)", _src_img) is not None)
verifie("9d. ★ le libellé neuf est traduit des deux côtés",
        "alert.image.jamais_construite" in fr and "alert.image.jamais_construite" in en)
verifie("9e. ★★ et il ne parle PAS d'un échec (rien n'a échoué sur une installation neuve)",
        "échou" not in fr["alert.image.jamais_construite"].split("—")[0],
        fr.get("alert.image.jamais_construite", "")[:80])

print()
if echecs:
    print(f"\033[31m{len(echecs)} échec(s)\033[0m sur {total[0]} vérification(s) :")
    for e in echecs:
        print(f"   · {e}")
    sys.exit(1)
print(f"\033[32m{total[0]} vérification(s), 0 échec(s)\033[0m"
      + (f"  (mutation {MUT} NON DÉTECTÉE)" if MUT else ""))
sys.exit(0)
