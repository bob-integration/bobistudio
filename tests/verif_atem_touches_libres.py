#!/usr/bin/env python3
"""Toutes les touches du pupitre ATEM reçoivent un nom — jamais le reste de la capture rejouée.

★ CE QUE CETTE GARDE EMPÊCHE DE REVENIR (2026-10-27). Demande de l'utilisateur : « pour les
labels envoyés au panel, il faudrait mettre à jour tous les labels, pas seulement ceux qu'on
utilise. Sinon le panel garde d'anciens labels. » Trois chemins laissaient un nom périmé :
  · un slot du mélangeur sans source était SAUTÉ : la touche gardait le nom d'avant ;
  · la capture rejouée déclare 40 entrées, le mélangeur 8 : les 32 autres gardaient les noms du
    site capturé (« Paluche », « Scoring », « Synth GAM ») ;
  · sorties et M/E de la capture portaient eux aussi les noms du site (« Patch B4 - Cécilia »,
    « PGM GAM ») — remplacés par les noms d'usine ATEM.
Et un changement de câblage n'atteignait le pupitre qu'au dump suivant : la boucle de poll
renvoie maintenant les noms qui ont changé.

Le nom d'une touche libre se choisit PAR PUPITRE (Réglages → Pupitres → ATEM) :
« Entrée N » | « - » | « Camera N » (usine).

Lancer : ./venv/bin/python tests/verif_atem_touches_libres.py
"""
import ast
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Isolation de la base (cf. verif_atem_labels_apres_replay.py) : base jetable, posée AVANT le
# premier accès ; on ne touche pas PRODUCTION_DB_PATH.
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = os.path.join(
    _tempfile_iso.mkdtemp(prefix="verif-iso-"), "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)
_dbm_iso.init_db()

SOURCE = os.path.join(ROOT, "services/panel_atem/__init__.py")
CAPTURE = os.path.join(ROOT, "services/panel_atem/replay_constellation8k.json")
SETTINGS = os.path.join(ROOT, "templates/settings.html")
FAILS = []


def verifier(nom, obtenu, attendu):
    ok = obtenu == attendu
    print(("  ok   " if ok else "  ÉCHEC") + f" {nom} : obtenu {obtenu!r}, attendu {attendu!r}")
    if not ok:
        FAILS.append(nom)


def _noms(cmds):
    out = []
    for c in cmds:
        b = c[8:]
        out.append((b[2:22].split(b"\0")[0].decode("utf-8", "replace"),
                    b[22:26].split(b"\0")[0].decode("utf-8", "replace")))
    return out


class _Sess:
    def __init__(self, ip="10.0.0.1", me_vmids=(1250,)):
        self.ip = ip
        self.me_vmids = list(me_vmids)
        self.anim_idx = 0
        self.addr = (ip, 1234)
        self.pending_dump = []
        self.labels_after_replay = False
        self.labels_sent = {}
        self.shift_held = False     # Shift (atem 0.4.20)
        self.n_inputs_me = {}


def main():
    print(__doc__.splitlines()[0])
    if not os.path.exists(SOURCE):
        print("  sous-module services/panel_atem absent — garde inapplicable")
        return 0
    from services import panel_atem as atem

    # ── 1. Les trois noms de touche libre, et ils tiennent dans les champs d'InPr.
    verifier("mode input", atem._nom_libre(4, "input"), ("Entrée 5", "5"))
    verifier("mode free : juste « - »", atem._nom_libre(4, "free"), ("-", "-"))
    verifier("mode factory", atem._nom_libre(4, "factory"), ("Camera 5", "CAM5"))
    verifier("mode factory ≥ 10 : nom court de la capture", atem._nom_libre(17, "factory"), ("Camera 18", "CM18"))
    trop = [(m, s) for m in atem.UNUSED_NAME_MODES for s in range(40)
            if len(atem._nom_libre(s, m)[0].encode()) > 20 or len(atem._nom_libre(s, m)[1].encode()) > 4]
    verifier("aucun nom de touche libre n'est tronqué (20 / 4 octets)", trop, [])

    # ── 2. Mode PAR PUPITRE, tolérant à la lecture.
    vrai_panels = atem._panels
    try:
        atem._panels = lambda: {"10.0.0.1": {"unused_names": "free"}, "10.0.0.2": {"unused_names": "?"}}
        verifier("le mode est lu par pupitre", atem._unused_mode_for_ip("10.0.0.1"), "free")
        verifier("valeur inconnue → défaut", atem._unused_mode_for_ip("10.0.0.2"), "input")
        verifier("pupitre sans réglage → défaut", atem._unused_mode_for_ip("10.0.0.9"), "input")
    finally:
        atem._panels = vrai_panels

    # ── 3. La capture rejouée ne porte plus AUCUN nom du site capturé.
    brut = json.load(open(CAPTURE))
    atem._REPLAY_CACHE = None
    rejoue = atem._replay_commands()
    verifier("même nombre de commandes que la capture", len(rejoue), len(brut))
    perso, autres_octets, usine_change = [], [], []
    for (nom, corps), c in zip(brut, rejoue):
        if nom != "InPr":
            if c[8:] != bytes.fromhex(corps):
                autres_octets.append(nom)
            continue
        avant, apres = bytes.fromhex(corps), c[8:]
        if apres[26] != 1:
            perso.append(struct.unpack(">H", apres[:2])[0])
        # Seuls les noms (2..26) et le drapeau 26 bougent : le reste est ce qui FONCTIONNE.
        if avant[:2] + avant[27:] != apres[:2] + apres[27:]:
            autres_octets.append(struct.unpack(">H", avant[:2])[0])
        if avant[26] == 1 and avant != apres:
            usine_change.append(struct.unpack(">H", avant[:2])[0])
    verifier("toutes les sources de la capture portent un nom d'usine", perso, [])
    verifier("rien d'autre que les noms n'a changé", autres_octets, [])
    verifier("une source déjà au nom d'usine est rejouée telle quelle", usine_change, [])
    tous = " ".join(n for n, _ in _noms([c for c in rejoue if c[4:8] == b"InPr"]))
    verifier("plus de nom du site (Paluche, Cécilia, PGM GAM, Scoring)",
             [m for m in ("Paluche", "Cécilia", "PGM GAM", "Scoring", "MurLED") if m in tous], [])
    par_id = {struct.unpack(">H", c[8:10])[0]: _noms([c])[0] for c in rejoue if c[4:8] == b"InPr"}
    verifier("source 7 → Camera 7", par_id.get(7), ("Camera 7", "CAM7"))
    verifier("M/E 1 PGM → ME 1", par_id.get(10010), ("ME 1", "M/E1"))
    verifier("M/E 2 PVW → ME 2 PVW", par_id.get(10021), ("ME 2 PVW", "PVW2"))
    verifier("sortie 4 → Output 4", par_id.get(8004), ("Output 4", "OUT4"))
    verifier("sortie 12 → Output 12", par_id.get(8012), ("Output 12", "OT12"))
    verifier("la capture déclare 40 entrées physiques", atem._replay_inputs(), 40)

    # ── 4. Après le replay : les 40 touches, les libres au nom du mode du pupitre.
    pont = atem._Server.__new__(atem._Server)
    etat = {"n_inputs": 3, "inputs": ["CAM A", "", "Mire"]}
    pont._primary_state = lambda sess: (0, 1250, etat)
    vrai_me_state, vrai_replay = atem._Server._me_state, atem._replay_enabled
    try:
        atem._Server._me_state = staticmethod(lambda vmid: etat)
        atem._replay_enabled = lambda: True
        sess = _Sess()
        cmds = atem._Server._label_commands(pont, sess)
        noms = _noms(cmds)
        verifier("replay : les 40 entrées du pupitre sont nommées", len(noms), 40)
        verifier("les sources du mélangeur gardent leur nom", [noms[0][0], noms[2][0]], ["CAM A", "Mire"])
        verifier("slot non câblé → libre", noms[1], ("Entrée 2", "2"))
        verifier("au-delà du mélangeur → libre", noms[39], ("Entrée 40", "40"))
        verifier("les noms envoyés sont mémorisés (base du rafraîchissement)",
                 len(sess.labels_sent), 40)
        atem._panels = lambda: {"10.0.0.1": {"unused_names": "factory"}}
        verifier("mode usine : les libres en Camera N",
                 _noms(atem._Server._label_commands(pont, _Sess()))[10], ("Camera 11", "CM11"))
        atem._replay_enabled = lambda: False
        verifier("sans replay : seulement les entrées du mélangeur",
                 len(atem._Server._label_commands(pont, _Sess())), 3)
    finally:
        atem._panels = vrai_panels
        atem._Server._me_state = vrai_me_state
        atem._replay_enabled = vrai_replay

    # ── 5. Aperçu des Réglages : le même calcul, libres signalés.
    vrai_noms_de = atem._etat_noms_de
    try:
        atem._etat_noms_de = lambda me: (etat, "live")
        ap, _o = atem._apercu_touches("10.0.0.1", [1250])
        verifier("aperçu : la touche libre affiche son nom",
                 [(a["long"], a["libre"]) for a in ap],
                 [("CAM A", False), ("Entrée 2", True), ("Mire", False)])
    finally:
        atem._etat_noms_de = vrai_noms_de

    # ── 6. Par AST : le dump synthétique nomme toutes ses touches et mémorise, et la boucle de
    # poll renvoie ce qui a CHANGÉ (sans quoi un câblage modifié attend une reconnexion).
    arbre = ast.parse(open(SOURCE, encoding="utf-8").read())
    fn = {n.name: n for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef)}
    dump = ast.unparse(fn["_dump_state"])
    verifier("dump synthétique : _noms_touches", "_noms_touches(" in dump, True)
    verifier("dump synthétique : noms mémorisés", "sess.labels_sent =" in dump, True)
    boucle = None
    for n in ast.walk(arbre):
        if isinstance(n, ast.If) and "LABEL_REFRESH_S" in ast.unparse(n.test):
            boucle = ast.unparse(n)
    verifier("la boucle de poll relit les noms périodiquement", boucle is not None, True)
    if boucle:
        verifier("…via _label_names", "self._label_names(" in boucle, True)
        verifier("…ne renvoie que les changés", "sess.labels_sent.get(" in boucle and "!=" in boucle, True)
        verifier("…les envoie", "self._send_commands(" in boucle and "_cmd_inpr(" in boucle, True)
        verifier("…et mémorise l'envoi", "sess.labels_sent[" in boucle, True)
    route = ast.unparse(fn["atem_assign"]) if "atem_assign" in fn else ""
    verifier("route : unused_names validé contre UNUSED_NAME_MODES",
             "not in UNUSED_NAME_MODES" in route and "400" in route, True)

    # ── 7. L'interface : le menu existe et part sur la route.
    html = open(SETTINGS, encoding="utf-8").read()
    corps = html.split("async function assignerTouchesLibres", 1)
    verifier("js : assignerTouchesLibres défini", len(corps) == 2, True)
    if len(corps) == 2:
        f = corps[1].split("\n}\n", 1)[0]
        verifier("js : envoie unused_names à /api/atem/assign",
                 "/api/atem/assign" in f and "unused_names: mode" in f, True)
    verifier("js : le menu est rendu dans la ligne du pupitre",
             "assignerTouchesLibres('${p.ip}'" in html and "p.unused_names" in html, True)
    for lang in ("fr", "en"):
        d = json.load(open(os.path.join(ROOT, f"i18n/{lang}.json"), encoding="utf-8"))
        manq = [k for k in ("unused_names", "unused_names_tip", "unused_names_input",
                            "unused_names_free", "unused_names_factory") if f"settings.js.{k}" not in d]
        verifier(f"i18n {lang} : clés du menu", manq, [])

    print()
    if FAILS:
        print("ÉCHEC :", ", ".join(FAILS))
        return 1
    print("OK — toutes les touches sont nommées, la capture ne laisse plus de nom du site.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
