#!/usr/bin/env python3
"""Nos libellés doivent être posés APRÈS le drainage du dump REPLAY, et une seule fois.

★ CE QUE CETTE GARDE EMPÊCHE DE REVENIR (2026-10-23). Avec `atem_replay` actif, `_dump_state`
retournait dès la mise en file de la capture : ni le placeholder ni l'état réel n'étaient envoyés,
donc **aucun** de nos libellés n'atteignait jamais le pupitre — vérifié sur deux fichiers de
journal, « dump PLACEHOLDER » n'y apparaît pas une seule fois. Le replay écrit « Camera 1 »...
sur toutes les touches ; nos noms doivent passer derrière.

Deux pièges que cette garde tient :
  · empiler nos `InPr` AVANT la fin du drainage les ferait écraser par la suite de la capture ;
  · les empiler à chaque tour de boucle inonderait le pupitre (un dump toutes les 100 ms).

Lancer : ./venv/bin/python tests/verif_atem_labels_apres_replay.py
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SOURCE = os.path.join(ROOT, "services/atem/__init__.py")
FAILS = []


def verifier(nom, obtenu, attendu):
    ok = obtenu == attendu
    print(("  ok   " if ok else "  ÉCHEC") + f" {nom} : obtenu {obtenu!r}, attendu {attendu!r}")
    if not ok:
        FAILS.append(nom)


class _Sess:
    """Doublure de `_Session`. ⚠ Un stub qui DIVERGE du vrai objet cache la panne qu'il devrait
    montrer : celui-ci n'avait pas d'`ip`, et il a planté le jour où `_label_commands` a lu
    `sess.ip` pour le niveau de libellé — alors que le code était juste. L'assertion
    « le stub porte les mêmes attributs » plus bas tient cette dérive."""
    def __init__(self, me_vmids=(None,), anim_idx=0):
        self.ip = "10.0.0.1"
        self.me_vmids = list(me_vmids)
        self.anim_idx = anim_idx
        self.addr = (self.ip, 1234)
        self.pending_dump = []
        self.labels_after_replay = False


def _noms_de(cmds):
    """(nom long, nom court) de chaque commande InPr d'une liste."""
    out = []
    for c in cmds:
        if c[4:8] != b"InPr":
            continue        # consigné par l'assertion « rien que des InPr » ; ne pas planter ici
        b = c[8:]
        # UTF-8, pas latin1 : « DÉCLARÉ » relu en latin1 donnait « DÃCLARÃ » et faisait rougir
        # cette garde sur un code juste. `errors="replace"` ne lève jamais, et un caractère de
        # remplacement dans le résultat signale une troncature à l'octet (cf. _pad).
        out.append((b[2:22].split(b"\0")[0].decode("utf-8", "replace"),
                    b[22:26].split(b"\0")[0].decode("utf-8", "replace")))
    return out


def main():
    print(__doc__.splitlines()[0])
    if not os.path.exists(SOURCE):
        print("  sous-module services/atem absent — garde inapplicable")
        return 0
    try:
        from services import atem
    except Exception as e:                                   # pragma: no cover
        print(f"  services.atem non importable ({type(e).__name__}: {e}) — garde inapplicable")
        return 0

    pont = atem._Server.__new__(atem._Server)
    vrai_me_state = atem._Server._me_state

    # ── 0. Le stub doit porter ce que le vrai `_Session` porte, sinon il valide une fiction.
    vraie = atem._Session.__new__(atem._Session)
    try:
        atem._Session.__init__(vraie, ("10.0.0.1", 1234), 0x8001)
    except Exception:
        vraie = None
    if vraie is not None:
        manquants = [a for a in ("ip", "me_vmids", "anim_idx", "pending_dump",
                                 "labels_after_replay") if not hasattr(vraie, a)]
        verifier("le vrai _Session porte les attributs que la doublure imite", manquants, [])

    # ── 1. Non affecté : le placeholder complet, et RIEN QUE des InPr.
    pont._primary_state = lambda sess: (None, None, None)
    cmds = atem._Server._label_commands(pont, _Sess())
    verifier("non affecté : un InPr par touche du placeholder",
             len(cmds), atem.PLACEHOLDER_INPUTS)
    verifier("rien que des InPr (pas de _ver/_top/InCm qui relanceraient l'init)",
             sorted({c[4:8] for c in cmds}), [b"InPr"])
    noms = _noms_de(cmds)
    verifier("la dernière touche porte le témoin animé",
             noms[-1][1] in tuple(atem.SPINNER_CHARS), True)

    # ── 2. Affecté : les noms du mélangeur, et un slot sans nom est SAUTÉ, pas écrasé.
    # C'est le point de conception : `_cmd_inpr` mettrait « Input k » sur un slot sans libellé,
    # effaçant le « Camera 3 » que la capture a posé par un nom génériquement faux.
    # ⚠ AUCUN RÉSEAU : `_etat_des_noms` appelle `_me_state`, qui joint le conteneur. Sans la
    # doublure ci-dessous, cette garde interrogeait le VRAI mixer de production — un test de
    # `tests/` ne fait pas d'E/S réseau (cf. CLAUDE.md) et son verdict dépendrait du parc.
    etat = {"n_inputs": 4, "inputs": ["CAM A", "", "Mire", None]}
    pont._primary_state = lambda sess: (0, 1250, etat)
    atem._Server._me_state = staticmethod(lambda vmid: etat)
    cmds = atem._Server._label_commands(pont, _Sess(me_vmids=(1250,)))
    verifier("affecté : seuls les slots NOMMÉS sont envoyés", len(cmds), 2)
    verifier("ce sont bien les noms du mélangeur",
             [n for n, _ in _noms_de(cmds)], ["CAM A", "Mire"])
    verifier("aucun « Input k » générique n'est émis",
             [n for n, _ in _noms_de(cmds) if n.startswith("Input ")], [])
    etat_vide = {"n_inputs": 4, "inputs": ["", "", "", ""]}
    atem._Server._me_state = staticmethod(lambda vmid: etat_vide)
    verifier("aucun nom connu : on n'envoie rien du tout",
             atem._Server._label_commands(pont, _Sess(me_vmids=(1250,))), [])

    # ── 2 bis. LES NOMS VIENNENT DU M/E 1, JAMAIS D'UNE AUTRE RANGÉE (décision 2026-10-23).
    # `_primary_state` prend « la 1re rangée JOIGNABLE » : quand le M/E 1 tombait, les noms
    # passaient en silence à ceux du M/E 2 — soit renommer toutes les touches sans le dire,
    # puisqu'un InPr est indexé par SOURCE et que le pupitre n'a qu'UNE liste de noms.
    me1 = {"n_inputs": 2, "inputs": ["DU M/E 1", "AUSSI M/E 1"]}
    me2 = {"n_inputs": 2, "inputs": ["DU M/E 2", "AUSSI M/E 2"]}
    pont._primary_state = lambda sess: (1, 1251, me2)        # la rangée primaire est le M/E 2
    atem._Server._me_state = staticmethod(lambda vmid: me1 if vmid == 1250 else me2)
    cmds = atem._Server._label_commands(pont, _Sess(me_vmids=(1250, 1251)))
    verifier("deux M/E : les noms sont ceux du M/E 1",
             [n for n, _ in _noms_de(cmds)], ["DU M/E 1", "AUSSI M/E 1"])

    vrai_decl = atem._inputs_declares
    try:
        # M/E 1 INJOIGNABLE : on lit son câblage DÉCLARÉ, on ne bascule pas sur le M/E 2.
        atem._Server._me_state = staticmethod(lambda vmid: None if vmid == 1250 else me2)
        atem._inputs_declares = lambda vmid: ({"n_inputs": 2, "inputs": ["DÉCLARÉ 1", "DÉCLARÉ 2"]}
                                              if vmid == 1250 else None)
        cmds = atem._Server._label_commands(pont, _Sess(me_vmids=(1250, 1251)))
        verifier("M/E 1 injoignable : on lit sa configuration enregistrée",
                 [n for n, _ in _noms_de(cmds)], ["DÉCLARÉ 1", "DÉCLARÉ 2"])
        verifier("M/E 1 injoignable : AUCUN nom du M/E 2 ne passe",
                 [n for n, _ in _noms_de(cmds) if "M/E 2" in n], [])
        atem._inputs_declares = lambda vmid: None
        verifier("M/E 1 inconnu des deux côtés : on n'envoie rien",
                 atem._Server._label_commands(pont, _Sess(me_vmids=(1250, 1251))), [])
        atem._Server._me_state = staticmethod(lambda vmid: me2)
        verifier("M/E 1 non affecté : aucun nom, malgré un M/E 2 joignable",
                 atem._Server._label_commands(pont, _Sess(me_vmids=(None, 1251))), [])
    finally:
        atem._inputs_declares = vrai_decl
        atem._Server._me_state = vrai_me_state

    # ── 3. L'ORDRE, par AST (pas par comparaison de texte : un commentaire reformaté ne doit pas
    # faire rougir cette garde, et un `if` déplacé doit la faire rougir).
    arbre = ast.parse(open(SOURCE, encoding="utf-8").read())
    bloc = None
    for noeud in ast.walk(arbre):
        # Le bloc du drainage : `if not sess.pending_dump:` imbriqué dans `if sess.pending_dump:`
        if (isinstance(noeud, ast.If) and "not sess.pending_dump" in ast.unparse(noeud.test)):
            bloc = noeud
            break
    verifier("le bloc de fin de drainage existe toujours", bloc is not None, True)
    if bloc is not None:
        corps = ast.unparse(bloc)
        verifier("nos libellés sont empilés DANS la fin du drainage",
                 "labels_after_replay" in corps and "_label_commands" in corps, True)
        # ⚠ La présence du texte ne suffit pas : neutraliser la CONDITION (`if False:`) laisse
        # tout le reste en place. On exige un `if` dont le TEST porte sur le drapeau.
        gardes = [n for n in ast.walk(bloc)
                  if isinstance(n, ast.If) and "labels_after_replay" in ast.unparse(n.test)]
        verifier("l'empilage est bien CONDITIONNÉ au drapeau (pas un if neutralisé)",
                 len(gardes) >= 1, True)
        if gardes:
            verifier("et l'appel des libellés est sous cette condition",
                     "_label_commands" in ast.unparse(gardes[0]), True)
        # L'empilage doit précéder `initialized = True` : sinon la boucle sort du drainage et
        # passe en régime de poll, où le dump ne repasse plus.
        pos_garde = corps.index("labels_after_replay")
        pos_init = corps.index("sess.initialized = True")
        verifier("l'empilage précède sess.initialized = True", pos_garde < pos_init, True)
        verifier("l'empilage passe par pending_dump (même fenêtre, même renvoi sur perte)",
                 "pending_dump = self._chunk_commands" in corps, True)
        verifier("une seule fois : le drapeau est posé avant l'envoi",
                 "sess.labels_after_replay = True" in corps, True)

    # ── 4. Le drapeau doit être REMIS À ZÉRO quand un nouveau dump replay est mis en file,
    # sinon une réaffectation laisse le pupitre sur les libellés de la capture.
    dump = next((n for n in ast.walk(arbre)
                 if isinstance(n, ast.FunctionDef) and n.name == "_dump_state"), None)
    verifier("_dump_state existe toujours", dump is not None, True)
    if dump is not None:
        src_dump = ast.unparse(dump)
        verifier("_dump_state remet labels_after_replay à False",
                 "sess.labels_after_replay = False" in src_dump, True)

    # ── 5. L'attribut existe sur une session neuve (sinon AttributeError au premier drainage).
    sess_src = next((n for n in ast.walk(arbre)
                     if isinstance(n, ast.ClassDef) and n.name in ("_Session",)), None)
    if sess_src is not None:
        verifier("une session neuve porte le drapeau",
                 "self.labels_after_replay = False" in ast.unparse(sess_src), True)

    print()
    if FAILS:
        print("ÉCHEC :", ", ".join(FAILS))
        return 1
    print("OK — nos libellés passent après le drainage, une fois, et jamais à la place d'un nom connu.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
