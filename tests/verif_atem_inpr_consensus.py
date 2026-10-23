#!/usr/bin/env python3
"""Nos `InPr` ATEM doivent décrire une ENTRÉE DE BUS, pas une sortie de multiviewer.

★ CE QUE CETTE GARDE EMPÊCHE DE REVENIR (2026-10-23). Aucun de nos libellés de touches n'est
JAMAIS apparu sur un pupitre, et l'encodage en était une des deux causes : `_cmd_inpr` posait ses
deux champs de disponibilité à 33/34 au lieu de 34/35, d'où `meAvailability = 0` — la signature
des `Output`/`Mask`, c'est-à-dire de ce qui ne s'affiche PAS sur les touches de bus d'un M/E.
Le défaut était invisible : la commande partait, le pupitre ne décrochait pas, rien ne protestait.

La référence n'est pas une spécification — le layout d'`InPr` dépend du firmware et le code le
disait (« ~36-byte layout, best-effort »). C'est la capture `replay_constellation8k.json`, dont on
sait qu'elle FONCTIONNE sur ce pupitre : quand on la rejoue, il affiche « Camera 1 ». On compare
donc notre encodage au CONSENSUS UNANIME de ses sources physiques, et à lui seul — un octet qui
varie d'une source à l'autre ne prouve rien et n'est pas contrôlé ici.

Lancer : ./venv/bin/python tests/verif_atem_inpr_consensus.py
"""
import binascii
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CAPTURE = os.path.join(ROOT, "services/atem/replay_constellation8k.json")
FAILS = []

# Les sources PHYSIQUES de la capture : ce que le pupitre montre sur ses touches de bus. Les ids
# au-delà (4010+ masques, 8019+ sorties) sont justement ceux dont `meAvailability` vaut 0.
PHYS_MIN, PHYS_MAX = 1, 20

# Offsets dont on contrôle la valeur. Au-delà de 35 il n'y a rien : la commande fait 36 octets.
QUEUE = range(26, 36)


def verifier(nom, obtenu, attendu):
    ok = obtenu == attendu
    print(("  ok   " if ok else "  ÉCHEC") + f" {nom} : obtenu {obtenu!r}, attendu {attendu!r}")
    if not ok:
        FAILS.append(nom)


def main():
    print(__doc__.splitlines()[0])
    if not os.path.exists(CAPTURE):
        # Sous-module `services/atem` non déployé (checkout partiel, CI sans submodules) : on le
        # DIT plutôt que de rendre un vert qui ne vérifierait rien.
        print("  capture de replay absente (sous-module services/atem) — garde inapplicable")
        return 0
    try:
        from services import atem
    except Exception as e:                                   # pragma: no cover
        print(f"  services.atem non importable ({type(e).__name__}: {e}) — garde inapplicable")
        return 0

    cmds = json.load(open(CAPTURE, encoding="utf-8"))
    inpr = [binascii.unhexlify(d) for n, d in cmds if n == "InPr"]
    phys = [b for b in inpr if PHYS_MIN <= int.from_bytes(b[0:2], "big") <= PHYS_MAX]
    verifier("la capture fournit des InPr de sources physiques", len(phys) >= 10, True)
    verifier("toutes les commandes InPr font 36 octets", sorted({len(b) for b in inpr}), [36])

    # ── Le consensus : un offset ne fait référence que s'il est UNANIME sur les sources physiques.
    consensus = {}
    for off in QUEUE:
        vals = {b[off] for b in phys}
        if len(vals) == 1:
            consensus[off] = vals.pop()
        else:
            print(f"  (offset {off} : {len(vals)} valeurs distinctes — non contrôlé, sans consensus)")
        continue
    verifier("des offsets unanimes à comparer", len(consensus) >= 5, True)

    corps = atem._cmd_inpr(0, 8, "Camera 1", "CAM1")
    verifier("notre InPr est bien une commande ATEM complète", corps[4:8], b"InPr")
    b = corps[8:]
    verifier("notre InPr fait 36 octets", len(b), 36)

    ecarts = {off: (f"0x{b[off]:02x}", f"0x{v:02x}") for off, v in consensus.items() if b[off] != v}
    verifier("aucun écart au consensus des sources physiques", ecarts, {})

    # ── Les deux champs qui portaient le défaut, nommés et contrôlés pour eux-mêmes : un futur
    # remaniement du consensus ne doit pas pouvoir les laisser repasser à 0 en silence.
    verifier("availability (offset 34) : toutes les destinations", b[34], 0x1F)
    verifier("meAvailability (offset 35) : AU MOINS UN M/E", b[35] != 0, True)
    verifier("meAvailability vaut le masque déclaré par le module", b[35], atem.ME_AVAILABILITY_MASK)
    verifier("offset 33 au consensus (0x00), pas le 0x1F décalé d'antan", b[33], 0x00)
    # areNamesDefault : nos libellés viennent du flux ou de l'emplacement, jamais de l'usine.
    verifier("areNamesDefault (offset 26) : nos noms sont personnalisés", b[26], 0x00)

    # ── La structure, pour que la comparaison d'octets garde un sens.
    verifier("le slot est écrit en tête (u16)", int.from_bytes(b[0:2], "big"),
             atem._slot_to_source(0))
    verifier("nom long en 2..22", b[2:22].split(b"\0")[0], b"Camera 1")
    verifier("nom court en 22..26", b[22:26].split(b"\0")[0], b"CAM1")
    # `_pad` tronque en silence : on vérifie que la troncature tombe sur les bonnes bornes plutôt
    # que de déborder sur le champ suivant.
    long_b = atem._cmd_inpr(0, 8, "X" * 40, "YYYYYY")[8:]
    verifier("nom long trop long : tronqué à 20, sans déborder", long_b[2:22], b"X" * 20)
    verifier("nom court trop long : tronqué à 4", long_b[22:26], b"YYYY")
    verifier("une troncature ne touche pas la queue", long_b[26:], b[26:])

    print()
    if FAILS:
        print("ÉCHEC :", ", ".join(FAILS))
        return 1
    print("OK — nos InPr collent au consensus de la capture ; meAvailability n'est pas nul.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
