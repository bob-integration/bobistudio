#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde HORS LIGNE du banc de lecture synchrone (tools/bancs/banc_sync_play.py) : son verdict est à
TOLÉRANCE ZÉRO (décision 2026-10-17, paires fill / key). Captures synthétiques : deux flux identiques passent ;
un décalage d'UNE image, une seule image répétée sur un flux pendant que l'autre avance, une rupture à un
index différent, un échantillon audio d'écart — chacun fait échouer le banc.

MUTATIONS, BANC_MUT=<nom> :
    tolerate_one   un écart d'une image passe
    no_repeats     la désynchro passagère n'est pas comptée

    ./venv/bin/python tests/verif_banc_sync_play.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "bancs"))
import banc_sync_play as B  # noqa: E402

MUT = os.environ.get("BANC_MUT", "")
if MUT == "tolerate_one":
    _v = B.verdict
    B.verdict = lambda an, same_content=True: [d for d in _v(an, same_content) if "[1]" not in d and "[-1]" not in d]
elif MUT == "no_repeats":
    _a = B.analyse

    def _an(c):
        r = _a(c)
        r["repeats"] = {}
        return r
    B.analyse = _an

ko = []


def ok(cond, msg, detail=""):
    print(("  ok   " if cond else "  ÉCHEC ") + msg)
    if not cond:
        print("         %s" % (detail,))
        ko.append(msg)


def cap(name, seq, k0=1000, onsets=()):
    return {"rate": [25, 1], "frames": {name: {str(k0 + i): v for i, v in enumerate(seq)}},
            "onsets": {name: list(onsets)}}


base = list(range(100, 150))
A = cap("a", base, onsets=[48000 * 41])
an = B.analyse([A, cap("b", base, onsets=[48000 * 41])])
ok(B.verdict(an) == [], "deux flux identiques : synchrones", B.verdict(an))
an = B.analyse([A, cap("b", [v - 1 for v in base], onsets=[48000 * 41])])
ok(any("décalage" in d for d in B.verdict(an)), "UNE image de décalage permanent : le banc échoue", B.verdict(an))
rep = list(base)
rep[20] = rep[19]                        # b répète une image, puis rattrape
an = B.analyse([A, cap("b", rep, onsets=[48000 * 41])])
ok(an["repeats"].get("b") == 1 or any("passagère, 1 " in d for d in B.verdict(an)),
   "une image répétée sur b pendant que a avance : comptée", an.get("repeats"))
ok(any("passagère" in d for d in B.verdict(an)), "désynchro passagère d'UNE image : le banc échoue", B.verdict(an))
paused = [120] * 50
an = B.analyse([cap("a", paused), cap("b", paused)])
ok(B.verdict(an) == [], "les deux en pause (répètent ensemble) : synchrones", B.verdict(an))
cut_a = base[:25] + list(range(500, 525))
cut_b = base[:26] + list(range(501, 525))
an = B.analyse([cap("a", cut_a), cap("b", cut_b)])
ok(any("ruptures" in d or "décalage" in d for d in B.verdict(an)), "changement de ligne une image plus tard sur b : échec",
   B.verdict(an))
an = B.analyse([A, cap("b", base, onsets=[48000 * 41 + 1])])
ok(any("audio" in d for d in B.verdict(an)), "un échantillon audio d'écart : échec", B.verdict(an))
held = base[:30] + [base[29]] * 20       # contenu différent, clip court qui TIENT sa fin
an = B.analyse([cap("a", [v + 400 for v in base]), cap("b", held)])
ok(not any("passagère" in d for d in B.verdict(an, same_content=False)),
   "contenus différents : la fin tenue d'un clip court n'est pas une désynchro", B.verdict(an, same_content=False))

print()
if ko:
    print("ÉCHECS (%d)" % len(ko))
    sys.exit(1)
print("verif_banc_sync_play : tout est vert%s" % (" (mutation %s NON détectée !)" % MUT if MUT else ""))
