#!/usr/bin/env python3
"""Pupitre ATEM : Shift fait valoir aux boutons 1-8 les entrées 9-16, et les voyants suivent.

★ CE QUE CETTE GARDE TIENT (2026-10-29, décisions de Cyril, 20h). Sur un mélangeur de plus de 8
entrées, tant que Shift est tenu, les boutons de bus (PGM, PVW) valent les entrées 9-16 ; relâché,
1-8. Les voyants suivent (PrgI/PrvI, tally TlIn, noms InPr) et la lampe de la touche Shift
(MRPr « macro en cours ») s'allume tant qu'il est tenu. 8 entrées ou moins : aucun effet. Au-delà
de 16 : pas de second Shift.

La touche SHIFT physique n'est pas transmise par le protocole (constat écrit dans le pont, au-dessus
de SHIFT_BANK) : le repli est une touche MACRO désignée par pupitre, qui VERROUILLE (le protocole
n'envoie que l'appui).

On vérifie, sur les VRAIES fonctions du pont (source chargée, mutée si demandé) :
  1. l'échange des bancs (involution), le refus sur 8 entrées, le trou au-delà des entrées réelles ;
  2. `_handle_payload` : CPgI/CPvI passent par le banc, MAct de la touche désignée bascule Shift
     sans lancer d'affectation, un autre MAct lance la sienne ;
  3. la VRAIE boucle de poll (deux tours) : PrgI/PrvI, TlIn, InPr et MRPr avec et sans Shift ;
  4. un dump relâche Shift ; noms des touches MACRO ; route, interface, i18n, manifeste, macros.

Lancer : ./venv/bin/python tests/verif_atem_shift.py            (ATEM_SHIFT_MUTATION=<nom>)
         ./venv/bin/python tests/verif_atem_shift.py --mutations   (chaque mutation doit mordre)
"""
import ast
import json
import os
import re
import struct
import subprocess
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Isolation de la base (cf. verif_atem_labels_apres_replay.py) : base jetable AVANT tout accès.
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = os.path.join(
    _tempfile_iso.mkdtemp(prefix="verif-iso-"), "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)
_dbm_iso.init_db()

ATEM_DIR = os.path.join(ROOT, "services/panel_atem")
SOURCE = os.path.join(ATEM_DIR, "__init__.py")
IP = "192.0.2.202"

MUTATIONS = {
    # les boutons ignorent Shift
    "entree_sans_shift": ("_shift_offset(n_in, sess.shift_held), n_in)", "0, n_in)"),
    # Shift actif sur un mélangeur de 8 entrées
    "huit_entrees_actif": ("return SHIFT_BANK if held and n > SHIFT_BANK else 0",
                           "return SHIFT_BANK if held else 0"),
    # entrée inexistante envoyée au mélangeur (12 entrées, bouton 6 décalé = 14)
    "trou_envoye": ("    if offset and s >= int(n_inputs or 0):\n        return None",
                    "    if False:\n        return None"),
    # la touche désignée lance son affectation au lieu de basculer Shift
    "mact_jamais_shift": ("                if index == _shift_key_for_ip(sess.ip):",
                          "                if False:"),
    # voyants PGM/PVW sans le banc
    "voyants_sans_banc": ("_cmd_prgi(_slot_to_source(_bank_swap(pgm - 1, off)), me=me_idx)",
                          "_cmd_prgi(pgm, me=me_idx)"),
    # tally sans le banc
    "tally_sans_banc": ('ppgm = _slot_to_source(_bank_swap(int(primary.get("pgm", 0)), poff))',
                        'ppgm = _slot_to_source(int(primary.get("pgm", 0)))'),
    # noms des touches sans le banc
    "noms_sans_banc": ("return [(_bank_swap(sl, off), ln, sn) for sl, ln, sn in noms]",
                       "return [(sl, ln, sn) for sl, ln, sn in noms]"),
    # la lampe Shift n'est jamais envoyée
    "lampe_jamais": ("                    if lampe != sess.fb_shift:", "                    if False:"),
    # basculer Shift ne force pas le renvoi des voyants
    "bascule_sans_noms": ("        sess.labels_ts = 0.0\n        log.info(\"atem: Shift %s on %s\"",
                          "        log.info(\"atem: Shift %s on %s\""),
    # un dump garde Shift tenu
    "dump_garde_shift": ("        sess.shift_held = False\n        sess.fb_shift = None\n        sess.n_inputs_me = {}\n",
                         "        sess.fb_shift = None\n"),
    # la touche Shift n'est pas nommée sur le pupitre
    "touche_sans_nom": ("        names[k] = SHIFT_KEY_NAME", "        pass"),
    # l'action de macro « relâché » tient Shift
    "macro_off_inverse": ('want = (not sess.shift_held) if mode == "toggle" else (mode == "on")',
                          'want = (not sess.shift_held) if mode == "toggle" else True'),
}

FAILS = []


def verifier(nom, obtenu, attendu):
    ok = obtenu == attendu
    print(("  ok   " if ok else "  ÉCHEC") + f" {nom} : obtenu {obtenu!r}, attendu {attendu!r}")
    if not ok:
        FAILS.append(nom)


def charger_pont():
    src = open(SOURCE, encoding="utf-8").read()
    mut = os.environ.get("ATEM_SHIFT_MUTATION")
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


def decoder(cmds):
    """[(nom, corps)] des commandes envoyées."""
    out = []
    for c in cmds:
        out.append((c[4:8].decode(), c[8:]))
    return out


def lancer_mutations():
    rc_global = 0
    for nom in MUTATIONS:
        env = dict(os.environ, ATEM_SHIFT_MUTATION=nom)
        r = subprocess.run([sys.executable, os.path.abspath(__file__)], env=env,
                           capture_output=True, text=True)
        mord = r.returncode == 1
        print(("  mord " if mord else "  RATÉE") + f" {nom} (code {r.returncode})")
        if not mord:
            rc_global = 1
    return rc_global


def main():
    print(__doc__.splitlines()[0])
    if not os.path.exists(SOURCE):
        print("  sous-module services/panel_atem absent — garde inapplicable")
        return 0
    atem, src = charger_pont()

    # ── 1. Fonctions pures.
    verifier("8 entrées : Shift sans effet", atem._shift_offset(8, True), 0)
    verifier("12 entrées : décalage 8", atem._shift_offset(12, True), 8)
    verifier("relâché : aucun décalage", atem._shift_offset(16, False), 0)
    verifier("échange des bancs = involution sur 0..19",
             all(atem._bank_swap(atem._bank_swap(s, 8), 8) == s for s in range(20)), True)
    verifier("bouton 1 décalé = entrée 9", atem._panel_slot_to_mixer(0, 8, 16), 8)
    verifier("bouton 8 décalé = entrée 16", atem._panel_slot_to_mixer(7, 8, 16), 15)
    verifier("au-delà de 16 : pas de second Shift (entrée 17 reste 17)", atem._panel_slot_to_mixer(16, 8, 20), 16)
    verifier("12 entrées : bouton 6 décalé = entrée 14 absente → rien", atem._panel_slot_to_mixer(5, 8, 12), None)
    verifier("12 entrées : bouton 3 décalé = entrée 11", atem._panel_slot_to_mixer(2, 8, 12), 10)
    verifier("sans Shift : identité", atem._panel_slot_to_mixer(5, 0, 12), 5)
    verifier("touche désignée : lecture tolérante", [atem._shift_key_from_entry(e) for e in
             ({"shift_key": 4}, {"shift_key": "7"}, {"shift_key": 100}, {"shift_key": "x"}, {})],
             [4, 7, None, None, None])
    verifier("MRPr allumé = macro en cours sur la touche", atem._cmd_mrpr(4)[8:], bytes([1, 0, 0, 4]))
    verifier("MRPr éteint = disposition de la capture (0000ffff)",
             atem._cmd_mrpr(None)[8:], atem._replay_body("MRPr", 0xFFFF) or bytes.fromhex("0000ffff"))

    # ── 2. `_handle_payload` : VRAI _Server et VRAIE _Session.
    entry = {"me": [1250], "shift_key": 4}
    atem._panels = lambda: {IP: entry}
    srv = atem._Server(0)
    posts, cles = [], []
    srv._enqueue_post = lambda vmid, cle, corps: posts.append((vmid, cle, corps))
    srv._run_key = lambda sess, index: cles.append(index)
    sess = atem._Session((IP, 62426), 0x8001)
    sess.me_vmids = [1250]

    def presser(*cmds):
        del posts[:]
        atem._Server._handle_payload(srv, sess, b"".join(atem._cmd(n, b) for n, b in cmds))
        return list(posts)

    def cpgi(src_id):
        return ("CPgI", bytes([0, 0]) + struct.pack(">H", src_id))

    def cpvi(src_id):
        return ("CPvI", bytes([0, 0]) + struct.pack(">H", src_id))

    def mact(index, action=0):
        return ("MAct", struct.pack(">H", index) + bytes([action, 0]))

    sess.n_inputs_me = {0: 16}
    verifier("relâché : bouton 3 → entrée 3", presser(cpgi(3)), [(1250, "CPgI", {"input": 2})])
    presser(mact(4))
    verifier("touche MACRO 5 désignée : Shift tenu", sess.shift_held, True)
    verifier("…sans lancer d'affectation", cles, [])
    verifier("tenu : PGM bouton 3 → entrée 11, PVW bouton 1 → entrée 9",
             presser(cpgi(3), cpvi(1)), [(1250, "CPgI", {"input": 10}), (1250, "CPvI", {"input": 8})])
    presser(mact(4))
    verifier("second appui : relâché", sess.shift_held, False)
    verifier("relâché : retour à 1-8", presser(cpvi(1)), [(1250, "CPvI", {"input": 0})])
    presser(mact(2))
    verifier("autre touche MACRO : son affectation", (cles, sess.shift_held), ([2], False))
    presser(mact(4))
    presser(mact(0xFFFF, action=1))
    verifier("« arrêt de macro » du pupitre : relâche Shift", sess.shift_held, False)
    sess.n_inputs_me = {0: 8}
    presser(mact(4))
    verifier("8 entrées : Shift refusé, aucun effet", sess.shift_held, False)
    verifier("8 entrées : bouton 3 → entrée 3", presser(cpgi(3)), [(1250, "CPgI", {"input": 2})])
    sess.n_inputs_me = {0: 12}
    presser(mact(4))
    verifier("12 entrées : bouton 6 décalé (entrée 14 absente) n'envoie rien", presser(cpgi(6)), [])
    srv._set_shift(sess, False)

    # ── 3. La VRAIE boucle de poll, deux tours à chaque fois.
    noms = [f"src-{i + 1}" for i in range(16)]
    etat = {"pgm": 10, "pvw": 2, "n_inputs": 16, "inputs": noms}
    atem._relay_state = lambda vmid: dict(etat)
    atem._etat_noms_de = lambda me_vmids: (dict(etat), "live")
    envoyes = []
    srv._send_commands = lambda s, cmds: envoyes.extend(cmds)
    srv._send_reliable = lambda s, payload: None
    srv._key_names = {}
    sess.initialized = True
    sess.placeholder = False
    sess.pending_dump = []
    sess.labels_ts = 0.0
    srv.sessions = {sess.addr: sess}

    vrai_time = atem.time

    def tour():
        del envoyes[:]
        appels = [0]

        def dormir(_s):
            appels[0] += 1
            if appels[0] >= 2:
                srv.running = False
        faux = {k: getattr(vrai_time, k) for k in dir(vrai_time) if not k.startswith("_")}
        faux["sleep"] = dormir
        atem.time = types.SimpleNamespace(**faux)
        sess.last_activity = vrai_time.time()
        srv.running = True
        try:
            srv._poll_loop()
        finally:
            atem.time = vrai_time
        return decoder(envoyes)

    def dernier(cmds, nom):
        v = [b for n, b in cmds if n == nom]
        return v[-1] if v else None

    def source_de(cmds, nom):
        b = dernier(cmds, nom)
        return struct.unpack(">H", b[2:4])[0] if b and len(b) >= 4 else None

    def nom_court(cmds, src_id):
        for n, b in cmds:
            if n == "InPr" and struct.unpack(">H", b[:2])[0] == src_id:
                return b[2:22].rstrip(b"\0").decode()
        return None

    c = tour()
    verifier("relâché : PrgI = entrée 11", source_de(c, "PrgI"), 11)
    verifier("relâché : PrvI = entrée 3", source_de(c, "PrvI"), 3)
    verifier("relâché : aucune lampe Shift envoyée", dernier(c, "MRPr"), None)
    verifier("relâché : bouton 1 nommé d'après l'entrée 1", nom_court(c, 1), "src-1")
    verifier("n_inputs mémorisé pour le fil de réception", sess.n_inputs_me.get(0), 16)

    presser(mact(4))
    c = tour()
    verifier("tenu : PrgI = bouton 3 (entrée 11)", source_de(c, "PrgI"), 3)
    verifier("tenu : PrvI = source 11 (entrée 3 hors de la page)",
             source_de(c, "PrvI"), 11)
    tl = dernier(c, "TlIn")
    verifier("tenu : tally — bouton 3 au PGM", tl[2 + 2] & 1 if tl else None, 1)
    verifier("tenu : tally — bouton 11 au PVW", tl[2 + 10] & 2 if tl else None, 2)
    verifier("tenu : lampe de la touche 5 allumée (MRPr)", dernier(c, "MRPr"), bytes([1, 0, 0, 4]))
    verifier("tenu : bouton 1 nommé d'après l'entrée 9", nom_court(c, 1), "src-9")
    verifier("tenu : bouton 9 nommé d'après l'entrée 1", nom_court(c, 9), "src-1")

    presser(mact(4))
    c = tour()
    verifier("relâché : PrgI revient à 11", source_de(c, "PrgI"), 11)
    verifier("relâché : lampe éteinte", dernier(c, "MRPr"), bytes([0, 0, 0xFF, 0xFF]))
    verifier("relâché : bouton 1 renommé entrée 1", nom_court(c, 1), "src-1")

    # ── 4. Dump, noms, route, interface, i18n, manifeste, macros.
    srv._set_shift(sess, True)
    atem._replay_enabled = lambda: True
    atem._replay_commands = lambda: [atem._cmd("Time", bytes(8))]
    srv._dump_state(sess)
    verifier("un dump relâche Shift", sess.shift_held, False)

    atem._key_names_live = lambda bindings: {0: "Macro A", 4: "Ancienne"}
    verifier("touche désignée nommée SHIFT 9-16 (prime sur une affectation)",
             atem._key_names_with_shift({"shift_key": 4}), {0: "Macro A", 4: "SHIFT 9-16"})

    # Macros : état et action, sur le serveur du module.
    atem._server = srv
    sess.n_inputs_me = {0: 16}
    verifier("macro : état relâché", atem.read_state("shift_held", {}), False)
    atem.run_action("set_shift", {"panel": IP, "mode": "on"}, {})
    verifier("macro : on → tenu", (sess.shift_held, atem.read_state("shift_held", {})), (True, True))
    atem.run_action("set_shift", {"panel": IP, "mode": "off"}, {})
    verifier("macro : off → relâché", sess.shift_held, False)
    atem.run_action("set_shift", {"panel": IP, "mode": "toggle"}, {})
    verifier("macro : toggle → tenu", sess.shift_held, True)
    sess.n_inputs_me = {0: 8}
    srv._set_shift(sess, False)
    try:
        atem.run_action("set_shift", {"panel": IP, "mode": "on"}, {})
        refus = None
    except RuntimeError as e:
        refus = "8" in str(e)
    verifier("macro : 8 entrées → l'action ÉCHOUE (jamais un faux succès)", refus, True)
    try:
        atem.run_action("set_shift", {"panel": "10.9.9.9", "mode": "on"}, {})
        refus = None
    except RuntimeError:
        refus = True
    verifier("macro : pupitre absent → échec", refus, True)
    atem._server = None

    man = json.load(open(os.path.join(ATEM_DIR, "manifest.json"), encoding="utf-8"))
    verifier("manifeste : action set_shift + état shift_held",
             ([a["id"] for a in man.get("actions") or []], [s["id"] for s in man.get("states") or []]),
             (["set_shift"], ["shift_held"]))

    arbre = ast.parse(src)
    fn = {n.name: n for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef)}
    route = ast.unparse(fn["atem_assign"])
    verifier("route : shift_key validé (borné à MACRO_KEY_COUNT)",
             "'shift_key'" in route and "MACRO_KEY_COUNT" in route, True)
    verifier("route : réglage et état publiés par pupitre",
             all(k in ast.unparse(fn["atem_sessions"]) for k in ("'shift_key'", "'shift_held'")), True)

    html = open(os.path.join(ATEM_DIR, "settings_tab.html"), encoding="utf-8").read()
    verifier("interface : menu par pupitre qui écrit shift_key",
             "data-atem-shift-ip" in html and "shift_key:" in html and "/api/atem/assign" in html, True)
    verifier("interface : lampe du catalogue partagé (ctl-led)", "ctl-led" in html, True)
    cles_html = set(re.findall(r"_\('(service\.atem\.shift_[a-z_]+)'\)", html))
    for lang in ("fr", "en"):
        d = json.load(open(os.path.join(ATEM_DIR, f"i18n/{lang}.json"), encoding="utf-8"))
        verifier(f"i18n {lang} : clés Shift présentes", sorted(k for k in cles_html if k not in d), [])
    verifier("i18n : au moins 8 clés Shift utilisées", len(cles_html) >= 8, True)

    print()
    if FAILS:
        print("ÉCHEC :", ", ".join(FAILS))
        return 1
    print("OK — Shift fait valoir les entrées 9-16 aux boutons 1-8, et les voyants suivent.")
    return 0


if __name__ == "__main__":
    if "--mutations" in sys.argv:
        sys.exit(lancer_mutations())
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        # A crash is not a verdict: code 3, so that a mutation "bites" only through a failed check.
        import traceback
        traceback.print_exc()
        sys.exit(3)
