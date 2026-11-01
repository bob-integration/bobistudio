#!/usr/bin/env python3
"""Pupitre ATEM : KEY ON AIR, Next Transition, MIX/WIPE et DSK pilotent le mélangeur, et l'état revient.

★ CE QUE CETTE GARDE TIENT (2026-10-28). Demande du testeur (recette custom-ff5bd032bb), confiée à la
session Mélangeur : premier lot de touches du pupitre — KEY 1 ON AIR, Next Transition KEY 1 / BKGD,
DSK 1 TIE / CUT / AUTO, styles MIX et WIPE. Le pont ne traitait que PGM/PVW, CUT, AUTO et la
T-barre ; tout le reste était journalisé « NON TRAITÉE » et perdu.

Les octets viennent du VRAI pupitre de la Z8 (2 M/E Advanced Panel, 28/10 14:59-15:06), jamais de
mémoire. Une touche DSK AFFECTÉE à un keyer pilote ce keyer (décision de l'utilisateur, 28/10, du
temps où le mélangeur n'avait pas de DSK) ; NON AFFECTÉE, elle pilote le VRAI DSK de même numéro du
mélangeur (0.23.50, 29/10 : /dsk/cut, /dsk/tie, /dsk/auto) et l'écho vient de `dsks` du /state.

On vérifie :
  1. le décodage de chaque trame relevée (dont le MASQUE de CTTp, dont l'octet 0 n'est pas la M/E) ;
  2. le passage dans `_handle_payload` jusqu'à la file de POST, M/E comprise ;
  3. ces corps, POSTés au VRAI ControlHandler du mélangeur (gabarit rendu), changent bien son état ;
  4. l'écho vers le pupitre (KeOn, TrSS, DskS, DskP) depuis le /state réel du mélangeur, sur
     CHANGEMENT seulement, en partant des trames de la capture rejouée ;
  5. la boucle de poll l'envoie et l'oublie à chaque nouveau dump ; réglage et interface.

Lancer : ./venv/bin/python tests/verif_atem_touches_keyers.py   (ATEM_KEYS_MUTATION=<nom>)
"""
import ast
import json
import os
import sys
import threading
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

# ── Isolation de la base (cf. verif_atem_touches_libres.py) : base jetable AVANT tout accès.
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = os.path.join(
    _tempfile_iso.mkdtemp(prefix="verif-iso-"), "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)
_dbm_iso.init_db()

SOURCE = os.path.join(ROOT, "services/panel_atem/__init__.py")
SETTINGS = os.path.join(ROOT, "templates/settings.html")

MUTATIONS = {
    # la M/E de CTTp lue à l'octet 0 (le masque) comme pour les autres commandes
    "cttp_me_octet0": ("masque, me, style, sel = body[0], body[1], body[2], body[3]",
                       "masque, me, style, sel = body[0], body[0], body[2], body[3]"),
    # le style appliqué sans regarder le masque
    "style_sans_masque": ("        if masque & 0x01:\n            mode = ATEM_STYLE_TO_MODE",
                          "        if True:\n            mode = ATEM_STYLE_TO_MODE"),
    # KEY n décalé d'un bit
    "next_bit_decale": ('"keyers": [bool(sel >> (i + 1) & 1)', '"keyers": [bool(sel >> i & 1)'),
    # pas d'écho dans la boucle de poll
    "pas_d_echo": ("                            if echo:\n                                self._send_commands(sess, echo)",
                   "                            if False:\n                                self._send_commands(sess, echo)"),
    # l'écho n'est pas oublié au nouveau dump
    "fb_keys_jamais_remis": ("        sess.fb_me = {}\n        sess.fb_keys = {}",
                             "        sess.fb_me = {}\n        pass"),
    # écho à chaque tour au lieu du changement
    "echo_sans_diff": ("        if prev.get(\"on\") is None or prev[\"on\"][i] != on:",
                       "        if True:"),
    # DskP reconstruit à zéro au lieu de la trame de la capture
    "dskp_a_zero": ("    b = bytearray(_replay_body(name, index) or bytes([index]) + bytes(size - 1))",
                    "    b = bytearray(bytes([index]) + bytes(size - 1))"),
    # DSK affecté ignoré : toujours le keyer 1
    "dsk_ignore_reglage": ("        k = dsk_keyers[d] if d < len(dsk_keyers) else None",
                           "        k = 1"),
    # (0.23.50) DSK non affecté : toujours le DSK 1 du mélangeur, quel que soit le numéro pressé
    "dsk_reel_numero": ('                out.append((0, "DDsA-dsk", {"dsk": d + 1}))',
                        '                out.append((0, "DDsA-dsk", {"dsk": 1}))'),
    # (0.23.50) TIE du vrai DSK inversé
    "dsk_reel_tie_inverse": ('{"dsk": d + 1, "tie": bool(body[1])}', '{"dsk": d + 1, "tie": not bool(body[1])}'),
    # (0.23.50) pas d'écho du vrai DSK vers le pupitre
    "dsk_reel_sans_echo": ("bool(k(n - 1).get(\"auto\"))) if n else real(d)", "bool(k(n - 1).get(\"auto\"))) if n else None"),
}

FAILS = []


def verifier(nom, obtenu, attendu):
    ok = obtenu == attendu
    print(("  ok   " if ok else "  ÉCHEC") + f" {nom} : obtenu {obtenu!r}, attendu {attendu!r}")
    if not ok:
        FAILS.append(nom)


def charger_pont():
    """Le module du pont depuis sa source (mutée si demandé), `__file__` réel : la capture
    rejouée est lue à côté."""
    src = open(SOURCE, encoding="utf-8").read()
    mut = os.environ.get("ATEM_KEYS_MUTATION")
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
    def __init__(self, me_vmids):
        self.ip = "192.0.2.202"
        self.addr = (self.ip, 62426)
        self.me_vmids = list(me_vmids)
        self.rx_window_ts = 0.0
        self.rx_count = 0


def main():
    print(__doc__.splitlines()[0])
    if not os.path.exists(SOURCE):
        print("  sous-module services/panel_atem absent — garde inapplicable")
        return 0
    atem, src = charger_pont()
    h = bytes.fromhex

    # ── 1. Décodage des trames RELEVÉES à la Z8.
    D = atem._decode_key_command
    verifier("KEY 1 ON AIR (CKOn 00000100)", D("CKOn", h("00000100"), []),
             [(0, "CKOn", {"keyer": 1, "active": True})])
    verifier("KEY 3 ON AIR (CKOn 00020100)", D("CKOn", h("00020100"), [])[0][2]["keyer"], 3)
    verifier("Next KEY 1 (CTTp 02000102) : sélection seule, style 1 du pupitre IGNORÉ",
             D("CTTp", h("02000102"), []),
             [(0, "CTTp", {"bgd": False, "keyers": [True, False, False, False]})])
    verifier("Next BKGD (CTTp 02000101)", D("CTTp", h("02000101"), []),
             [(0, "CTTp", {"bgd": True, "keyers": [False, False, False, False]})])
    verifier("MIX (CTTp 01000004) : style seul, sélection 04 IGNORÉE",
             D("CTTp", h("01000004"), []), [(0, "CTTp-style", {"mode": "dissolve"})])
    verifier("WIPE (CTTp 01000204)", D("CTTp", h("01000204"), []),
             [(0, "CTTp-style", {"mode": "wipe"})])
    verifier("CTTp sur la M/E 2 : M/E lue à l'octet 1", D("CTTp", h("02010101"), [])[0][0], 1)
    verifier("DIP (style 1) : rien envoyé", D("CTTp", h("01000104"), []), [])
    # Contre-exemple du MASQUE : sélection seule alors que le pupitre CROIT être en MIX (style 0,
    # qui a un équivalent). Trame construite : la Z8 n'a relevé que des sélections en style 1.
    verifier("sélection seule (masque 2) en style MIX : le style n'est PAS appliqué",
             [c for _m, c, _b in D("CTTp", h("02000001"), [])], ["CTTp"])
    verifier("DSK 1 non affecté → DSK 1 du mélangeur hors antenne", D("CDsL", h("00000000"), [None, None]),
             [(0, "CDsL-dsk", {"dsk": 1, "active": False})])
    verifier("DSK 1 TIE non affecté → lien du DSK 1", D("CDsT", h("00010000"), [None, None]),
             [(0, "CDsT-dsk", {"dsk": 1, "tie": True})])
    dsk = [2, None]
    verifier("DSK 1 TIE (CDsT 00010000) → cible du keyer 2", D("CDsT", h("00010000"), dsk),
             [(0, "CDsT", {"keyer_n": 2, "on": True})])
    verifier("DSK 1 CUT (CDsL 00000000) → keyer 2 hors antenne", D("CDsL", h("00000000"), dsk),
             [(0, "CDsL", {"keyer": 2, "active": False})])
    verifier("DSK 1 AUTO (DDsA 00000600) → AUTO du keyer 2", D("DDsA", h("00000600"), dsk),
             [(0, "DDsA", {"keyer": 2})])
    verifier("DSK 2 non affecté ici → AUTO du DSK 2 du mélangeur", D("DDsA", h("01000600"), dsk),
             [(0, "DDsA-dsk", {"dsk": 2})])

    # Réglage par pupitre, tolérant à la lecture.
    verifier("réglage : lecture tolérante", atem._dsk_keyers_from_entry({"dsk_keyers": [3, 9]}), [3, None])
    verifier("réglage absent : non affecté", atem._dsk_keyers_from_entry({}), [None, None])

    # ── 2. Par _handle_payload jusqu'à la file de POST.
    srv = atem._Server.__new__(atem._Server)
    posts = []
    srv._enqueue_post = lambda vmid, cle, corps: posts.append((vmid, cle, corps))
    vrai_panels = atem._panels
    atem._panels = lambda: {"192.0.2.202": {"dsk_keyers": [2, None]}}
    try:
        payload = b"".join(atem._cmd(n, h(b)) for n, b in (
            ("CKOn", "00000100"), ("CTTp", "02000102"), ("CTTp", "01000204"),
            ("CDsT", "00010000"), ("CDsL", "00000000"), ("DDsA", "00000600"), ("DDsA", "01000600")))
        atem._Server._handle_payload(srv, _Sess([1250, 1251]), payload)
    finally:
        atem._panels = vrai_panels
    verifier("les six commandes partent vers le mélangeur de la M/E 1", posts, [
        (1250, "CKOn", {"keyer": 1, "active": True}),
        (1250, "CTTp", {"bgd": False, "keyers": [True, False, False, False]}),
        (1250, "CTTp-style", {"mode": "wipe"}),
        (1250, "CDsT", {"keyer_n": 2, "on": True}),
        (1250, "CDsL", {"keyer": 2, "active": False}),
        (1250, "DDsA", {"keyer": 2}),
        (1250, "DDsA-dsk", {"dsk": 2})])

    # ── 3. Ces corps, sur les routes du manifeste, contre le VRAI ControlHandler du mélangeur.
    import verif_mixer_delais as H                                           # noqa: E402
    import requests                                                         # noqa: E402
    ns, _r, _p = H.make_ns({0: H.G, 1: H.G, 2: H.G, 3: H.G})
    if len(ns["state"]["keyers"]) < 2:
        # Le gabarit de test déploie 1 keyer par défaut : la garde en veut 2 (DSK → keyer 2).
        ns["state"]["keyers"].append(dict(ns["state"]["keyers"][0], active=False))
        ns["N_KEYERS"] = 2
        ns["state"]["next_targets"]["keyers"] = [False, False]
    http = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["ControlHandler"])
    http.daemon_threads = True
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % http.server_address[1]
    man = json.load(open(os.path.join(ROOT, "plugins/mixer/plugin.json")))
    amap = (man.get("control") or {}).get("atem") or {}
    codes = []
    for _vmid, cle, corps in posts:
        r = requests.post(base + atem._atem_path(amap, cle), json=corps, timeout=2)
        codes.append(r.status_code)
    # Le gabarit de test n'a qu'UN DSK (schéma d'avant 0.23.50 : l'habillage) : l'AUTO du DSK 2 est
    # refusé par le mélangeur, et ce refus est journalisé par _mixer_post — jamais muet.
    verifier("le mélangeur accepte chaque POST, et refuse le DSK 2 qu'il n'a pas", codes,
             [200] * (len(posts) - 1) + [400])
    for corps in ({"dsk": 1, "tie": True}, {"dsk": 1, "active": True}):
        cle = "CDsT-dsk" if "tie" in corps else "CDsL-dsk"
        requests.post(base + atem._atem_path(amap, cle), json=corps, timeout=2)
    st = requests.get(base + "/state", timeout=2).json()
    verifier("mélangeur : keyer 1 à l'antenne", st["keyers"][0]["active"], True)
    verifier("mélangeur : style WIPE", st["preset"]["mode"], "wipe")
    verifier("mélangeur : prochaine transition = KEY 1 + keyer 2 (TIE du DSK 1), sans fond",
             (st["next_targets"]["bgd"], st["next_targets"]["keyers"][:2]), (False, [True, True]))
    verifier("mélangeur : keyer 2 en AUTO (DSK 1 AUTO)", st["keyers"][1]["auto"], True)

    # ── 4. Écho vers le pupitre, depuis ce /state réel.
    snap = atem._key_feedback(st, [2, None])
    verifier("écho : keyers à l'antenne", snap["on"][:2], (True, False))
    verifier("écho : style WIPE = 2", snap["style"], 2)
    verifier("écho : sélection = KEY 1 + KEY 2 (bits 1 et 2)", snap["next"], 0b110)
    verifier("écho : DSK 1 = (antenne, tie, auto) du keyer 2", snap["dsk"][0], (False, True, True))
    cmds = atem._key_feedback_commands(0, snap, None)
    par = {}
    for c in cmds:
        par.setdefault(c[4:8].decode(), []).append(c[8:])
    verifier("premier écho : 4 KeOn + TrSS + DskS + DskP",
             sorted((k, len(v)) for k, v in par.items()),
             [("DskP", 1), ("DskS", 1), ("KeOn", 4), ("TrSS", 1)])
    verifier("KeOn du keyer 1", par["KeOn"][0], bytes([0, 0, 1, 0]))
    capt_trss = atem._replay_body("TrSS", 0)
    trss = par["TrSS"][0]
    verifier("TrSS : style et sélection réécrits", (trss[1], trss[2], trss[3], trss[4]), (2, 6, 2, 6))
    verifier("TrSS : le reste vient de la capture", trss[5:], capt_trss[5:])
    capt_dskp = atem._replay_body("DskP", 0)
    dskp = par["DskP"][0]
    verifier("DskP : tie à 1, le reste (rate, clip, gain…) vient de la capture",
             (dskp[1], dskp[2:]), (1, capt_dskp[2:]))
    verifier("DskS : antenne 0, AUTO en cours", tuple(par["DskS"][0][1:4]), (0, 1, 1))
    verifier("état inchangé : aucun écho", atem._key_feedback_commands(0, snap, snap), [])
    snap2 = dict(snap, on=(False,) + snap["on"][1:])
    verifier("keyer 1 retiré : un seul KeOn", atem._key_feedback_commands(0, snap2, snap),
             [atem._cmd_keon(0, 0, False)])
    verifier("M/E 2 : pas de DSK", atem._key_feedback(st, None)["dsk"], ())
    snap_d = atem._key_feedback(st, [None, None])
    verifier("écho : DSK 1 non affecté = le DSK 1 du mélangeur (antenne, tie, auto)", snap_d["dsk"][0],
             (True, True, False))
    verifier("écho : DSK 2 que le mélangeur n'a pas = rien", snap_d["dsk"][1], None)

    # ── 5. Boucle de poll, dump, route, interface (par AST / texte).
    arbre = ast.parse(src)          # la source CHARGÉE (mutée le cas échéant), pas le disque
    fn = {n.name: n for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef)}
    boucle = ast.unparse(fn["_poll_loop"])
    verifier("poll : écho calculé depuis l'état du mélangeur",
             "_key_feedback(state" in boucle and "_key_feedback_commands(me_idx" in boucle, True)
    verifier("poll : écho mémorisé par M/E", "sess.fb_keys[me_idx] = snap" in boucle, True)
    envoi = [n for n in ast.walk(fn["_poll_loop"]) if isinstance(n, ast.If)
             and isinstance(n.test, ast.Name) and n.test.id == "echo"
             and "self._send_commands(sess, echo)" in ast.unparse(n.body[0])]
    verifier("poll : l'écho est ENVOYÉ quand il n'est pas vide", len(envoi), 1)
    dump = ast.unparse(fn["_dump_state"])
    verifier("dump : l'écho est oublié (re-émis après le dump)",
             dump.count("sess.fb_keys = {}"), dump.count("sess.fb_me = {}"))
    route = ast.unparse(fn["atem_assign"])
    verifier("route : dsk_keyer validé (index et keyer bornés)",
             "dsk_keyer" in route and "DSK_COUNT" in route and "DSK_KEYER_MAX" in route, True)
    sessions = ast.unparse(fn["atem_sessions"])
    verifier("route : le réglage est publié par pupitre", "'dsk_keyers'" in sessions, True)
    html = open(SETTINGS, encoding="utf-8").read()
    verifier("js : menus DSK dans la ligne du pupitre",
             "assignerDsk('${p.ip}', ${d}" in html and "p.dsk_keyers" in html, True)
    corps = html.split("async function assignerDsk", 1)
    verifier("js : envoie dsk_keyer à /api/atem/assign",
             len(corps) == 2 and "/api/atem/assign" in corps[1].split("\n}\n", 1)[0]
             and "dsk_keyer:" in corps[1].split("\n}\n", 1)[0], True)
    for lang in ("fr", "en"):
        d = json.load(open(os.path.join(ROOT, f"i18n/{lang}.json"), encoding="utf-8"))
        verifier(f"i18n {lang}", [k for k in ("dsk_keyer", "dsk_keyer_tip", "dsk_none")
                                  if f"settings.js.{k}" not in d], [])

    print()
    if FAILS:
        print("ÉCHEC :", ", ".join(FAILS))
        return 1
    print("OK — les touches keyer, transition et DSK du pupitre pilotent le mélangeur, et leur état revient.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
