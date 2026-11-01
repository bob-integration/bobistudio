#!/usr/bin/env python3
"""Pupitre ATEM : les touches MACRO lancent une macro Bobi ou une action de conteneur, et portent son nom.

★ CE QUE CETTE GARDE TIENT (2026-10-28). Demande de l'utilisateur : affecter, dans Réglages →
Pupitres → ATEM, ce que lance chaque touche MACRO du pupitre — une macro Bobi, ou « autre chose
que des macros, par exemple la gestion de playlist d'un Player : les noms des 7 premiers clips,
puis Play, Pause, Next ». Le pont journalisait `MAct` « NON TRAITÉE » et ne faisait rien.

Les octets viennent du VRAI pupitre de la Z8 (relevé du testeur, 16:06-16:08 : MAct 00000000 pour la
touche 1 … 00130000 pour la touche 20) et de la capture rejouée (100 MPrp vides de 8 octets),
jamais de mémoire. Les actions sont celles du VRAI manifeste du player.

On vérifie :
  1. `MAct` → la touche lancée (numéro sur 2 octets ; seule l'action 0 « lancer » agit) ;
  2. l'exécution : macro par le moteur de macros, action par `exec_action`, au nom du pupitre ;
  3. le nom d'une touche : macro, action, nom saisi, et choix VIVANT (clip n de la playlist) ;
  4. MPrp : même disposition que la capture, nom compris, envoyé sur CHANGEMENT seulement ;
  5. la validation d'écriture (base jetable) ; la boucle de poll, le réglage et l'interface.

Lancer : ./venv/bin/python tests/verif_atem_touches_macro.py   (ATEM_MACRO_MUTATION=<nom>)
"""
import ast
import json
import os
import sys
import threading
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Isolation de la base (cf. verif_atem_touches_libres.py) : base jetable AVANT tout accès.
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = os.path.join(
    _tempfile_iso.mkdtemp(prefix="verif-iso-"), "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)
_dbm_iso.init_db()

SOURCE = os.path.join(ROOT, "services/panel_atem/__init__.py")
SETTINGS = os.path.join(ROOT, "templates/settings.html")
PLAYER = os.path.join(ROOT, "plugins/player/plugin.json")

MUTATIONS = {
    # numéro lu sur UN octet (l'octet 0 vaut 0 jusqu'à la touche 256)
    "numero_un_octet": ('self._run_key(sess, struct.unpack(">H", body[:2])[0])',
                        'self._run_key(sess, body[0])'),
    # toute action lance (y compris « arrêter »)
    "action_ignoree": ('elif name == "MAct" and len(body) >= 3 and body[2] == 0:',
                       'elif name == "MAct" and len(body) >= 3:'),
    # le choix vivant ignoré : le libellé de l'action au lieu du titre du clip
    "choix_ignore": ("    p = _live_choice(act)\n    if p is None:\n        nom = custom",
                     "    p = None\n    if p is None:\n        nom = custom"),
    # le nom saisi ignoré
    "nom_saisi_ignore": ('    if binding.get("label"):\n        custom = str(binding["label"])',
                         '    if False:\n        custom = str(binding["label"])'),
    # état ignoré
    "etat_ignore": ('            return _with_state(nom, None if v is None else v == fb["value"])',
                    '            return nom'),
    # MPrp sans complément à 4 octets
    "mprp_sans_bourrage": ('    body += b"\\x00" * (-len(body) % 4)\n', ''),
    # noms renvoyés à chaque tour
    "noms_sans_diff": ("        if n != (prev or {}).get(i):\n            out.append(_cmd_mprp(i, n))",
                       "        if True:\n            out.append(_cmd_mprp(i, n))"),
    # pas d'envoi dans la boucle de poll
    "pas_d_envoi": ("                        if cmds:\n                            self._send_commands(sess, cmds)\n                        sess.fb_names",
                    "                        if False:\n                            self._send_commands(sess, cmds)\n                        sess.fb_names"),
    # noms non oubliés après le dump rejoué (qui redéclare les 100 emplacements vides)
    "fb_names_pas_remis_replay": ("                sess.fb_keys = {}\n                sess.fb_names = {}\n",
                                  "                sess.fb_keys = {}\n"),
    # la macro lancée avec les droits de réglage système
    "droits_systeme": ("run, err = macros.run_macro(b[\"macro_id\"], user=acteur)",
                       "run, err = macros.run_macro(b[\"macro_id\"], user=acteur, allow_system_config=True)"),
}

FAILS = []


def verifier(nom, obtenu, attendu):
    ok = obtenu == attendu
    print(("  ok   " if ok else "  ÉCHEC") + f" {nom} : obtenu {obtenu!r}, attendu {attendu!r}")
    if not ok:
        FAILS.append(nom)


def charger_pont():
    src = open(SOURCE, encoding="utf-8").read()
    mut = os.environ.get("ATEM_MACRO_MUTATION")
    if mut:
        if mut not in MUTATIONS:
            print("mutation inconnue", mut); sys.exit(2)
        a, b = MUTATIONS[mut]
        if src.count(a) != 1:
            print("ancre de mutation introuvable :", mut); sys.exit(2)
        src = src.replace(a, b)
    mod = types.ModuleType("services.panel_atem")
    mod.__file__ = SOURCE
    exec(compile(src, SOURCE, "exec"), mod.__dict__)
    return mod, src


class _Sess:
    def __init__(self):
        self.ip = "192.0.2.202"
        self.addr = (self.ip, 62426)
        self.me_vmids = []
        self.rx_window_ts = 0.0
        self.rx_count = 0
        self.shift_held = False     # Shift (atem 0.4.20)
        self.n_inputs_me = {}


def main():
    print(__doc__.splitlines()[0])
    if not os.path.exists(SOURCE):
        print("  sous-module services/panel_atem absent — garde inapplicable")
        return 0
    atem, src = charger_pont()
    h = bytes.fromhex
    player = json.load(open(PLAYER, encoding="utf-8"))

    # ── 1. MAct relevés à la Z8 → touche lancée, sans M/E affectée.
    srv = atem._Server.__new__(atem._Server)
    lancees = []
    srv._run_key = lambda sess, i: lancees.append(i)
    payload = b"".join(atem._cmd("MAct", h(b)) for b in ("00000000", "00090000", "000a0000", "00130000"))
    atem._Server._handle_payload(srv, _Sess(), payload)
    verifier("MAct du testeur : touches 1, 10, 11, 20", lancees, [0, 9, 10, 19])
    verifier("« lancer » n'est plus « NON TRAITÉE »", atem._UNKNOWN_SEEN.get("MAct/0000"), None)
    atem._Server._handle_payload(srv, _Sess(), atem._cmd("MAct", h("00000100")))
    verifier("autre action (00000100) : rien lancé, mais journalisée pour la mesurer",
             (lancees, atem._UNKNOWN_SEEN.get("MAct/0000")), ([0, 9, 10, 19], 1))
    verifier("numéro sur 2 octets (touche 257)", (lancees.clear(), atem._Server._handle_payload(
        srv, _Sess(), atem._cmd("MAct", h("01000000"))), lancees)[2], [256])

    # ── 2. Exécution au nom du pupitre.
    from app import macros
    vrai_run, vrai_exec, vrai_bind = macros.run_macro, macros.exec_action, atem._key_bindings_for_ip
    appels = []
    macros.run_macro = lambda mid, **kw: (appels.append(("macro", mid, kw)), (object(), None))[1]
    macros.exec_action = lambda vmid, aid, params, **kw: appels.append(("action", vmid, aid, params, kw))
    try:
        atem._execute_binding("192.0.2.202", 0, {"kind": "macro", "macro_id": 8})
        atem._execute_binding("192.0.2.202", 7, {"kind": "action", "vmid": 222,
                                                   "action_id": "plgoto", "params": {"index": "3"}})
        verifier("macro lancée comme un déclencheur (sans droit système)", appels[0],
                 ("macro", 8, {"user": "atem:192.0.2.202"}))
        verifier("action exécutée sur le conteneur, au nom du pupitre", appels[1],
                 ("action", 222, "plgoto", {"index": "3"}, {"actor": "atem:192.0.2.202"}))
        # _run_key : affectée → exécutée hors du fil RX ; non affectée → rien.
        fait = threading.Event()
        vrai_exe = atem._execute_binding
        atem._execute_binding = lambda ip, i, b: (appels.append(("run_key", i, b)), fait.set())
        atem._key_bindings_for_ip = lambda ip: {4: {"kind": "macro", "macro_id": 9}}
        n0 = len(appels)
        atem._Server._run_key(srv, _Sess(), 3)
        atem._Server._run_key(srv, _Sess(), 4)
        fait.wait(2)
        verifier("_run_key : seule la touche affectée part", appels[n0:],
                 [("run_key", 4, {"kind": "macro", "macro_id": 9})])
        atem._execute_binding = vrai_exe
    finally:
        macros.run_macro, macros.exec_action, atem._key_bindings_for_ip = vrai_run, vrai_exec, vrai_bind

    # ── 3. Nom d'une touche — actions du VRAI manifeste du player.
    pl_entries = {"items": [{"value": i, "label": t} for i, t in enumerate(["Intro", "Titre", "Météo"])]}
    get_macro = {8: {"name": "Ouverture"}}.get
    get_target = lambda b: (222, player) if b.get("instance_uuid") == "u-synthe" else (None, None)
    lus = []

    def get_options(vmid, act, p):
        lus.append((vmid, p["options_endpoint"]))
        return pl_entries
    N = lambda b: atem._key_name(b, get_macro, get_target, get_options)
    clip = lambda i, **kw: dict(kind="action", instance_uuid="u-synthe", action_id="plgoto",
                                params={"index": str(i)}, **kw)
    verifier("macro : son nom", N({"kind": "macro", "macro_id": 8}), "Ouverture")
    verifier("macro supprimée : touche éteinte", N({"kind": "macro", "macro_id": 99}), None)
    play = next(a for a in player["actions"] if a["id"] == "play")
    verifier("Play : libellé de l'action du manifeste",
             N({"kind": "action", "instance_uuid": "u-synthe", "action_id": "play"}), play["label"])
    verifier("Play renommée « Play »",
             N({"kind": "action", "instance_uuid": "u-synthe", "action_id": "play", "label": "Play"}), "Play")
    verifier("clip 2 de la playlist : son titre, lu dans /pl_entries", N(clip(1)), "Titre")
    verifier("… lu à l'endpoint déclaré par le manifeste", lus[-1], (222, "/pl_entries"))
    verifier("clip 7 d'une playlist de 3 : touche éteinte", N(clip(6)), None)
    verifier("clip renommé", N(clip(0, label="Ouv.")), "Ouv.")
    verifier("conteneur disparu : touche éteinte",
             N(dict(clip(0), instance_uuid="u-parti")), None)
    # État dans le nom (le voyant d'une touche MACRO ne se pilote pas) : retour d'état du manifeste.
    etat = {"loop": False, "paused": False}
    NS = lambda b: atem._key_name(b, get_macro, get_target, get_options, lambda v, sid: etat.get(sid))
    boucle = {"kind": "action", "instance_uuid": "u-synthe", "action_id": "loop_toggle"}
    verifier("Boucle éteinte : « Boucle (bascule) OFF »", NS(boucle), "Boucle (bascule) OFF")
    etat["loop"] = True
    verifier("Boucle allumée : « Boucle (bascule) ON »", NS(boucle), "Boucle (bascule) ON")
    verifier("Lecture en cours : « Play ON » (nom saisi + état)",
             NS({"kind": "action", "instance_uuid": "u-synthe", "action_id": "play", "label": "Play"}), "Play ON")
    verifier("Pause hors pause : « Pause OFF »",
             NS({"kind": "action", "instance_uuid": "u-synthe", "action_id": "pause"}), "Pause OFF")
    verifier("état illisible : le nom seul", atem._key_name(boucle, get_macro, get_target, get_options,
                                                            lambda v, sid: None), "Boucle (bascule)")
    verifier("clip : pas d'état ajouté", NS(clip(1)), "Titre")
    long_ = atem._with_state("Générique de début très long", True)
    verifier("nom long : raccourci, l'état tient dans les 20 octets",
             (long_.endswith(" ON"), len(long_.encode()) <= atem.MACRO_NAME_MAX), (True, True))
    fichier = next(a for a in player["actions"] if a["id"] == "file")
    verifier("options /files du player (options_path, option_value, option_label)",
             atem._option_items({"files": [{"path": "/m/a.mov", "label": "a"}]}, fichier["params"][0]),
             [("/m/a.mov", "a")])

    # ── 4. MPrp : la disposition des 100 trames de la capture.
    cap = [b for n, b in json.load(open(os.path.join(ROOT, "services/panel_atem/replay_constellation8k.json")))
           if n == "MPrp"]
    verifier("MPrp vide = celui de la capture (touche 3)", atem._cmd_mprp(2, None)[8:].hex(), cap[2])
    m = atem._cmd_mprp(4, "Météo")[8:]
    verifier("MPrp nommé : numéro, occupé, longueur UTF-8, nom", (m[:8].hex(), m[8:15].decode()),
             ("0004010000070000", "Météo"))
    verifier("MPrp complété à 4 octets", len(m) % 4, 0)
    verifier("nom tronqué à 20 octets sans couper un caractère",
             len(atem._cmd_mprp(0, "é" * 15)[8:]) - 8 - (-(8 + 20) % 4), 20)
    diff = atem._key_name_commands({0: "Intro", 1: "Titre"}, {0: "Intro", 2: "Vieux"})
    verifier("sur CHANGEMENT : touche 2 nommée, touche 3 libérée (déclarée vide)",
             [c[8:10].hex() + c[10:11].hex() for c in diff], ["000101", "000200"])
    verifier("rien de changé : rien envoyé", atem._key_name_commands({0: "A"}, {0: "A"}), [])

    # ── 5. Validation d'écriture et lecture tolérante (base jetable).
    from app import database as db
    mid = db.db_create_macro(None, "Ouverture", None)
    db.db_upsert_container(222, "Synthe", instance_uuid="u-synthe")
    db.db_update_deploy_config(222, "player", {"hostname": "Synthe"})
    C = atem._clean_binding
    verifier("macro existante acceptée", C({"kind": "macro", "macro_id": str(mid)}),
             ({"kind": "macro", "macro_id": mid}, None))
    verifier("macro inexistante refusée", C({"kind": "macro", "macro_id": 999})[1], "macro introuvable")
    verifier("clip 1 du player accepté", C(clip(0)),
             ({"kind": "action", "instance_uuid": "u-synthe", "action_id": "plgoto",
               "params": {"index": "0"}}, None))
    verifier("aller au clip SANS choix refusé", bool(C(dict(clip(0), params={}))[1]), True)
    verifier("action inconnue refusée", C(dict(clip(0), action_id="explose"))[1],
             "action inconnue pour ce conteneur")
    verifier("conteneur inconnu refusé", C(dict(clip(0), instance_uuid="u-parti"))[1], "conteneur introuvable")
    verifier("nom affiché trop long refusé", bool(C(dict(clip(0), label="x" * 41))[1]), True)
    verifier("lecture tolérante : entrées illisibles ignorées",
             sorted(atem._key_bindings_from_entry({"keys": {
                 "0": {"kind": "macro", "macro_id": 1}, "x": {"kind": "macro", "macro_id": 2},
                 "150": {"kind": "macro", "macro_id": 3}, "3": {"kind": "action"}, "4": "zut"}})), [0])

    # ── 6. Boucle de poll, oubli au dump, route, interface, i18n — dans la source (mutée).
    arbre = ast.parse(src)
    poll = next(n for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef) and n.name == "_poll_loop")
    psrc = ast.get_source_segment(src, poll)
    verifier("la boucle de poll envoie les noms qui changent",
             "if cmds:\n                            self._send_commands(sess, cmds)" in psrc, True)
    verifier("noms oubliés à chaque dump (replay, placeholder, état réel)", src.count("sess.fb_names = {}"), 3)
    verifier("fil des noms démarré avec le serveur", "self._names_thread.start()" in src, True)
    verifier("route : champ keys validé par _clean_binding", "b, err = _clean_binding(b)" in src, True)
    html = open(SETTINGS, encoding="utf-8").read()
    for f in ("function basculerTouches(", "function remplirTouches(", "function _atemToucheModifiee(",
              "function _atemMajTouchesOuvertes("):
        verifier(f"interface : {f}…", f in html, True)
    verifier("un pli « touches » ouvert bloque le re-rendu",
             "tr.atem-keys-row:not(.hidden)" in html, True)
    for lang in ("fr", "en"):
        cat = json.load(open(os.path.join(ROOT, f"i18n/{lang}.json"), encoding="utf-8"))
        manq = sorted({k for k in __import__("re").findall(r"T\('(settings\.js\.keys_[a-z_]+)'\)", html)
                       if k not in cat} | {k for k in ("settings.js.keys_col_key", "settings.js.keys_col_shown")
                                           if k not in cat})
        verifier(f"i18n {lang} : clés des touches présentes", manq, [])

    print()
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)}")
        return 1
    print("OK — les touches MACRO lancent leur cible et portent son nom.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
