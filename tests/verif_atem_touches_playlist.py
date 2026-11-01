#!/usr/bin/env python3
"""Pupitre ATEM : une rangée de touches MACRO suit la playlist CHARGÉE d'un player.

★ CE QUE CETTE GARDE TIENT (2026-10-29). Demande de l'utilisateur : « une rangée de touches (par
exemple 10) affiche les 10 clips de la playlist chargée dans un player, et chaque touche charge son
clip, quelle que soit la playlist chargée ». Le mécanisme de base existait (« aller au clip » avec
un index par touche) ; manquaient :
  - l'éditeur : sur une playlist VIDE, le choix de la position n'offrait que « … » (rapporté le
    29/10, player « Atelier-Synthe ») — et il disait « Intro » là où on mémorise une POSITION ;
  - le remplissage en lot avec « lecture immédiate », sans écraser en silence ;
  - des actions explicites « charger l'élément N » / « charger et lire l'élément N » ;
  - un repère de l'élément chargé dans le nom (le voyant d'une touche MACRO ne se pilote pas).

Le tout est DÉCLARATIF (`option_semantics: "position"`, `option_current_state`) : rien de propre
au player dans le pont ni dans les éditeurs. Actions et état : le VRAI manifeste du player.

On vérifie :
  1. le manifeste : actions explicites, compatibilité de `plgoto`, déclarations, état `pl_index` ;
  2. ce que l'action ENVOIE au player (vrai `exec_action`, requête interceptée, base jetable) ;
  3. les noms : changement de playlist, playlist plus courte, vide, injoignable, renommage,
     repère de l'élément chargé, deux players sur deux rangées ;
  4. la liste des positions (vrai `static/js/option_choices.js`, sous node) ;
  5. le remplissage : qui serait écrasé (vraie fonction de settings.html, sous node) ;
  6. les deux éditeurs passent par le module partagé ; i18n.

Lancer : ./venv/bin/python tests/verif_atem_touches_playlist.py   (ATEM_PL_MUTATION=<nom>)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Isolation de la base : base jetable AVANT tout accès (cf. verif_atem_touches_macro.py).
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = os.path.join(
    _tempfile_iso.mkdtemp(prefix="verif-iso-"), "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)
_dbm_iso.init_db()

SOURCE = os.path.join(ROOT, "services/panel_atem/__init__.py")
PLAYER = os.path.join(ROOT, "plugins/player/plugin.json")
CHOICES = os.path.join(ROOT, "static/js/option_choices.js")
SETTINGS = os.path.join(ROOT, "templates/settings.html")
WORKSPACE = os.path.join(ROOT, "templates/workspace.html")
LAYOUT = os.path.join(ROOT, "templates/layout.html")

# (fichier, avant, après, nombre d'occurrences attendu)
MUTATIONS = {
    # le repère de l'élément chargé jamais posé
    "repere_ignore": (SOURCE, "                return _with_mark(nom)\n", "                return nom\n", 1),
    # le repère sans troncature : le nom déborde des 20 octets
    "repere_deborde": (SOURCE, 'return CURRENT_MARK + name.encode("utf-8")[:room]',
                       'return CURRENT_MARK + name.encode("utf-8")[:99]', 1),
    # comparaison stricte de la valeur choisie : "1" (formulaire) ≠ 1 (état du player)
    "egalite_stricte": (SOURCE, "    if a == b or str(a) == str(b):\n        return True\n    try:",
                        "    if a == b:\n        return True\n    try:\n        raise ValueError", 1),
    # le manifeste ne déclare plus l'état de l'élément chargé
    "etat_courant_absent": (PLAYER, ',\n          "option_current_state": "pl_index"', "", 3),
    # « charger et lire » ne lit pas
    "lecture_inversee": (PLAYER, '"play": true', '"play": false', 1),
    # la sémantique de position ignorée : la liste vivante brute (« … » seul, playlist vide)
    "position_ignoree": (CHOICES, "if (!isPosition(p)) return items;", "return items;", 1),
    # pas de minimum : playlist vide → aucune position à choisir
    "minimum_absent": (CHOICES, "Math.max(POSITION_MIN, items.length,", "Math.max(0, items.length,", 1),
    # le remplissage écrase sans rien dire
    "ecrasement_muet": (SETTINGS, "return keys.filter(k => avant[String(k.index)] && !pareil(",
                        "return [].filter(k => avant[String(k.index)] && !pareil(", 1),
    # un nom saisi perdu sans prévenir
    "nom_saisi_ecrase": (SETTINGS, "&& x.action_id === y.action_id && !x.label", "&& x.action_id === y.action_id", 1),
    # l'éditeur de macros garde sa propre liste
    "workspace_liste_privee": (WORKSPACE, "MXLOptionChoices.build(p, items, cur)", "items", 1),
}

FAILS = []


def verifier(nom, obtenu, attendu):
    ok = obtenu == attendu
    print(("  ok   " if ok else "  ÉCHEC") + f" {nom} : obtenu {obtenu!r}, attendu {attendu!r}")
    if not ok:
        FAILS.append(nom)


def lire(path):
    src = open(path, encoding="utf-8").read()
    mut = os.environ.get("ATEM_PL_MUTATION")
    if mut:
        if mut not in MUTATIONS:
            print("mutation inconnue", mut); sys.exit(2)
        f, a, b, n = MUTATIONS[mut]
        if f == path:
            if src.count(a) != n:
                print("ancre de mutation introuvable :", mut, src.count(a)); sys.exit(2)
            src = src.replace(a, b)
    return src


def charger_pont():
    src = lire(SOURCE)
    mod = types.ModuleType("services.panel_atem")
    mod.__file__ = SOURCE
    exec(compile(src, SOURCE, "exec"), mod.__dict__)
    return mod, src


def node(js):
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(r.stderr[-800:])
        return None
    return json.loads(r.stdout)


def main():
    print(__doc__.splitlines()[0])
    if not os.path.exists(SOURCE):
        print("  sous-module services/panel_atem absent — garde inapplicable")
        return 0
    atem, src = charger_pont()
    player = json.loads(lire(PLAYER))
    acts = {a["id"]: a for a in player["actions"]}

    # ── 1. Manifeste.
    verifier("actions explicites présentes", [i in acts for i in ("pl_load", "pl_load_play", "plgoto")],
             [True, True, True])
    verifier("plgoto inchangée pour les touches existantes (index + play)",
             [(p["key"], p["type"]) for p in acts["plgoto"]["params"]], [("index", "number"), ("play", "bool")])
    for aid in ("plgoto", "pl_load", "pl_load_play"):
        p = atem._live_choice(acts[aid])
        verifier(f"{aid} : choix vivant /pl_entries déclaré position, élément chargé = pl_index",
                 (p["options_endpoint"], p.get("option_semantics"), p.get("option_current_state")),
                 ("/pl_entries", "position", "pl_index"))
    verifier("état pl_index publié (lu dans /state)",
             [(s["endpoint"], s["path"]) for s in player["state"] if s["id"] == "pl_index"], [("/state", "pl_index")])
    verifier("/state et /pl_entries lisibles avec le login seul",
             {"/state", "/pl_entries"} <= set(player["control"]["read_endpoints"]), True)

    # ── 2. Ce que l'action envoie (vrai exec_action, POST intercepté).
    from app import database as db, macros
    db.db_upsert_container(301, "Atelier-Synthe", instance_uuid="u-a")
    db.db_update_deploy_config(301, "player", {"hostname": "Atelier-Synthe"})
    envoyes = []

    class _Stop(Exception):
        pass

    def faux_post(url, json=None, timeout=None):
        envoyes.append((url.split(":", 2)[-1].split("/", 1)[-1], json))
        raise _Stop()
    vrai_req, vrai_ip, vrai_get = macros.requests, macros.get_container_ip, macros.plugins.get
    macros.requests = types.SimpleNamespace(post=faux_post, get=vrai_req.get)
    macros.get_container_ip = lambda vmid: "10.255.0.1"
    # le manifeste LU ici (éventuellement muté), pas celui du registre
    macros.plugins.get = lambda t: player if t == "player" else vrai_get(t)
    try:
        for aid, params in (("pl_load", {"index": "3"}), ("pl_load_play", {"index": 3}),
                            ("plgoto", {"index": "2"}), ("plgoto", {"index": "2", "play": True})):
            try:
                macros.exec_action(301, aid, params, actor="atem:test")
            except _Stop:
                pass
    finally:
        macros.requests, macros.get_container_ip, macros.plugins.get = vrai_req, vrai_ip, vrai_get
    verifier("charger l'élément 4 : en pause", envoyes[0], ("plgoto", {"play": False, "index": 3}))
    verifier("charger et lire l'élément 4 : lecture", envoyes[1], ("plgoto", {"play": True, "index": 3}))
    verifier("aller au clip (touche existante) : inchangé", envoyes[2], ("plgoto", {"index": 2, "play": False}))
    verifier("aller au clip, lecture immédiate cochée au remplissage", envoyes[3],
             ("plgoto", {"index": 2, "play": True}))
    C = atem._clean_binding
    verifier("touche « élément 1 » acceptée (index 0 n'est pas « vide »)",
             C({"kind": "action", "instance_uuid": "u-a", "action_id": "pl_load_play", "params": {"index": 0}}),
             ({"kind": "action", "instance_uuid": "u-a", "action_id": "pl_load_play", "params": {"index": 0}}, None))

    # ── 3. Noms des touches : deux players, deux rangées.
    lists = {301: ["Intro", "Titre", "Météo"], 302: ["Pub A", "Pub B"]}
    etat = {301: -1, 302: -1}
    joignable = {301: True, 302: True}
    get_target = lambda b: {"u-a": (301, player), "u-b": (302, player)}.get(b.get("instance_uuid"), (None, None))

    def get_options(vmid, act, p):
        if not joignable[vmid]:
            return None
        return {"items": [{"value": i, "label": t} for i, t in enumerate(lists[vmid])]}

    def get_state(vmid, sid):
        return etat[vmid] if sid == "pl_index" and joignable[vmid] else None

    rangee = lambda uuid, aid="pl_load_play": {i: {"kind": "action", "instance_uuid": uuid, "action_id": aid,
                                                    "params": {"index": str(i)}} for i in range(10)}

    def noms(uuid, aid="pl_load_play"):
        return [atem._key_name(b, None, get_target, get_options, get_state) for b in rangee(uuid, aid).values()]
    vide7 = [None] * 7
    verifier("rangée 1 (player A, 3 clips) : titres puis touches éteintes", noms("u-a"),
             ["Intro", "Titre", "Météo"] + vide7)
    verifier("rangée 2 (player B) : SA playlist", noms("u-b"), ["Pub A", "Pub B", None] + vide7)
    etat[301] = 1
    verifier("élément 2 chargé sur A : repère « > » sur la touche 2 seule", noms("u-a"),
             ["Intro", "> Titre", "Météo"] + vide7)
    verifier("… sans toucher la rangée du player B", noms("u-b"), ["Pub A", "Pub B", None] + vide7)
    verifier("repère aussi sur « aller au clip »", noms("u-a", "plgoto")[1], "> Titre")
    lists[301] = ["Ouverture", "Plateau", "Reportage", "Interview", "Météo", "Sport",
                  "Bourse", "Culture", "Agenda", "Générique", "Bonus"]
    etat[301] = -1                                         # charger une playlist remet pl_index à -1
    verifier("autre playlist chargée : les 10 touches suivent", noms("u-a"), lists[301][:10])
    lists[301] = ["Ouverture", "Plateau"]
    verifier("playlist plus courte : touches au-delà éteintes", noms("u-a"), ["Ouverture", "Plateau"] + [None] * 8)
    lists[301] = []
    verifier("playlist vide : toutes éteintes, sans erreur", noms("u-a"), [None] * 10)
    joignable[301] = False
    verifier("player injoignable : toutes éteintes, sans erreur", noms("u-a"), [None] * 10)
    joignable[301] = True
    lists[301] = ["Ouverture", "Plateau (v2)"]
    verifier("clip renommé : nouveau titre", noms("u-a")[1], "Plateau (v2)")
    lists[301] = ["Un titre de clip vraiment beaucoup trop long"]
    etat[301] = 0
    n0 = noms("u-a")[0]
    verifier("titre long + repère : tient dans les 20 octets, repère en tête",
             (n0.startswith("> "), len(n0.encode("utf-8")) <= atem.MACRO_NAME_MAX), (True, True))
    verifier("nom saisi + élément chargé : repère sur le nom saisi",
             atem._key_name(dict(rangee("u-a")[0], label="Ouv"), None, get_target, get_options, get_state), "> Ouv")
    verifier("noms relus au plus chaque seconde (changement visible en < 2 s)",
             "time.sleep(1.0)" in src[src.index("def _key_names_loop"):src.index("def _post_loop")], True)

    # ── 4. Liste des positions (le vrai module partagé, sous node).
    if not shutil.which("node"):
        print("  node absent — parties JS non vérifiées"); FAILS.append("node absent")
    else:
        cat = json.load(open(os.path.join(ROOT, "i18n/fr.json"), encoding="utf-8"))
        boot = ("global.window = {I18N: %s, t: k => (window.I18N[k] || k)};\n%s\n"
                % (json.dumps({k: v for k, v in cat.items() if k.startswith("js.option_position")}), lire(CHOICES)))
        pos = json.dumps(atem._live_choice(acts["pl_load"]))
        r = node(boot + """
const O = window.MXLOptionChoices, p = %s;
const trois = O.normalize({items: [{value: 0, label: 'Intro'}, {value: 1, label: 'Titre'}, {value: 2, label: 'Météo'}]}, p);
console.log(JSON.stringify({
  vide: O.build(p, O.normalize({items: []}, p), 0),
  trois: O.build(p, trois, '1'),
  loin: O.build(p, [], '24').length,
  fichiers: O.build({options_endpoint: '/files'}, trois, ''),
}));""" % pos)
        if r is None:
            FAILS.append("node : échec")
        else:
            verifier("playlist vide : 20 positions choisissables", len(r["vide"]), 20)
            verifier("… nommées « Élément N (vide pour l'instant) »", r["vide"][0],
                     {"v": 0, "l": "Élément 1 (vide pour l'instant)"})
            verifier("3 clips : « Élément 2 (actuellement : Titre) », valeur = position",
                     r["trois"][1], {"v": 1, "l": "Élément 2 (actuellement : Titre)"})
            verifier("position mémorisée au-delà de 20 : toujours dans la liste", r["loin"], 25)
            verifier("choix non positionnel (fichiers) : liste vivante telle quelle",
                     [o["l"] for o in r["fichiers"]], ["Intro", "Titre", "Météo"])

        # ── 5. Remplissage : qui serait écrasé (fonction de settings.html).
        html = lire(SETTINGS)
        m = re.search(r"function _atemTouchesEcrasees\(avant, keys\) \{.*?\n\}\n", html, re.S)
        verifier("settings : _atemTouchesEcrasees présente", bool(m), True)
        if m:
            r = node(m.group(0) + """
const b = (i, extra) => Object.assign({kind: 'action', instance_uuid: 'u-a', action_id: 'pl_load_play', params: {index: i}}, extra || {});
const avant = {
  '0': Object.assign(b('0'), {name: 'Intro'}),            // déjà à l'identique (index texte, nom affiché)
  '1': {kind: 'macro', macro_id: 4},                       // autre chose
  '2': b('2', {label: 'Ouv'}),                             // même cible, nom saisi : perdu si on remplace
  '3': Object.assign(b('3'), {action_id: 'plgoto'}),       // autre action
};
const keys = [0, 1, 2, 3, 4].map(i => ({index: i, binding: b(i)}));
console.log(JSON.stringify(_atemTouchesEcrasees(avant, keys)));""")
            verifier("remplissage : touches 2, 3, 4 signalées (pas la 1, déjà identique ; pas la 5, libre)",
                     r, [1, 2, 3])

    # ── 6. Les éditeurs passent par le module partagé ; i18n.
    html = lire(SETTINGS)
    ws = lire(WORKSPACE)
    verifier("layout charge option_choices.js", "/static/js/option_choices.js" in open(LAYOUT, encoding="utf-8").read(), True)
    verifier("ATEM : choix d'une touche par MXLOptionChoices.build",
             "MXLOptionChoices.build(p, await _atemOptionsVivantes(c, p), cur)" in html, True)
    verifier("ATEM : remplissage positionnel déclaré", "MXLOptionChoices.isPosition(p)" in html, True)
    verifier("ATEM : remplacement confirmé", "if (prises.length && !(await mxlConfirm(" in html, True)
    verifier("macros (workspace) : même liste", "MXLOptionChoices.build(p, items, cur)" in ws, True)
    verifier("macros : la position 0 reste choisie (plus de `if (cur)`)", "if (cur) el.value = cur;" in ws, False)
    for lang in ("fr", "en"):
        cat = json.load(open(os.path.join(ROOT, f"i18n/{lang}.json"), encoding="utf-8"))
        cles = set(re.findall(r"T\('(settings\.js\.keys_[a-z_]+)'\)", html)) | {
            "js.option_position_now", "js.option_position_empty"}
        verifier(f"i18n {lang} : clés présentes", sorted(k for k in cles if k not in cat), [])
        verifier(f"i18n {lang} : gabarits {{n}}/{{title}}",
                 ("{n}" in cat.get("js.option_position_now", "") and "{title}" in cat.get("js.option_position_now", ""),
                  "{n}" in cat.get("js.option_position_empty", "")), (True, True))

    print()
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)}")
        return 1
    print("OK — une rangée de touches suit la playlist chargée, positions lisibles, remplissage sans écrasement muet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
