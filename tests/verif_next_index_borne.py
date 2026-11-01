#!/usr/bin/env python3
"""Vérifie que `bobimxl.Writer.next_index` BORNE l'avance sur la grille TAI et RETIENT la trame
excédentaire au lieu d'écrire dans le futur.

Pourquoi ce banc existe : le `max(grille, précédent + 1)` qui garantit la stricte croissance est
asymétrique — en retard il saute à la grille, en avance il ajoute +1 à chaque appel QUELLE QUE
SOIT l'heure. Un producteur qui appelle un peu plus souvent que la grille n'a de créneaux prend
donc une avance acquise pour toujours. Mesuré le 2026-10-25 sur la régie légère : rl-mix écrivait
68 s dans le futur, cadence 50,0, aucun compteur ému.

Ce banc tourne SANS libmxl : `_lib` est remplacée par un double (grille réglable, journal des
grains publiés) et les Writer sont construits sans `__init__`.

    ./venv/bin/python tests/verif_next_index_borne.py
"""
import ctypes
import os
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_R, "script_templates"))
import bobimxl                                    # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


class FakeLib:
    """Grille réglable (`g`) ; chaque writer a son tampon et son journal de grains PUBLIÉS."""

    def __init__(self):
        self.g = 1_000_000
        self.bufs = {}
        self.published = {}

    def mxlGetCurrentIndex(self, rate_ref):
        return self.g

    def mxlGetTime(self):
        """Heure TAI cohérente avec la grille : 5 ms après le début du créneau `g` (50 i/s)."""
        return self.g * 20_000_000 + 5_000_000

    def mxlFlowWriterOpenGrain(self, h, idx, gi_ref, payload_ref):
        key = h.value
        buf = self.bufs.setdefault(key, (ctypes.c_uint8 * 64)())
        gi = gi_ref._obj
        gi.index = idx
        gi.grainSize = 64
        gi.totalSlices = 4
        payload_ref._obj.contents = ctypes.cast(buf, bobimxl._PU8).contents
        return bobimxl.MXL_STATUS_OK

    def mxlFlowWriterCommitGrain(self, h, gi_ref):
        self.published.setdefault(h.value, []).append(int(gi_ref._obj.index))
        return bobimxl.MXL_STATUS_OK


lib = FakeLib()
bobimxl._lib = lib


def writer(name, hid, mode="tai", interlace="progressive"):
    w = object.__new__(bobimxl.Writer)
    w.name = name
    w.flow_def = '{"interlace_mode": "%s"}' % interlace
    w.rate = bobimxl.mxlRational(50, 1)
    w.index_mode = mode
    w.ref = None
    w.frame_size = 64
    w._counter = 0
    w._last_idx = -1
    w._scratch = None
    w._h = bobimxl._VOID(hid)
    bobimxl._live_writers.add(w)
    return w


def frame(*ws):
    """Une trame de sortie : le premier writer calcule l'index, les autres l'imposent (sœurs)."""
    idx, gi, view = ws[0].open_grain()
    view[:] = 1
    ws[0].commit(gi)
    for w in ws[1:]:
        _i, g2, v2 = w.open_grain(index=idx)
        v2[:] = 1
        w.commit(g2)
    return idx


# 1. Une trame par créneau : sur la grille, rien de retenu.
a = writer("a", 1)
for _ in range(20):
    lib.g += 1
    frame(a)
pub = lib.published[1]
ok(pub == list(range(lib.g - 19, lib.g + 1)), "1 trame/créneau : index = grille", pub[-3:])
ok(a.index_ahead == 0 and a.index_held == 0 and a.grains_dropped == 0,
   "1 trame/créneau : ni avance ni retenue", (a.index_ahead, a.index_held, a.grains_dropped))

# 2. Deux trames dans le même créneau : la seconde prend le suivant (bande), pas de retenue.
lib.g += 1
frame(a)
frame(a)
ok(lib.published[1][-1] == lib.g + 1 and a.index_held == 0,
   "créneau double : +1 dans la bande, rien de retenu", (lib.published[1][-2:], a.index_held))

# 3. Excès SOUTENU (deux trames par créneau) : l'avance ne dépasse JAMAIS la bande.
b, c = writer("b", 2), writer("c", 3)
pire = 0
for _ in range(50):
    lib.g += 1
    for _k in range(2):
        frame(b, c)
        pire = max(pire, lib.published[2][-1] - lib.g)
ok(pire <= bobimxl.Writer.INDEX_BAND, "excès soutenu : avance bornée à INDEX_BAND",
   "avance max %d grains" % pire)
pb = lib.published[2]
ok(all(y > x for x, y in zip(pb, pb[1:])), "excès soutenu : publiés strictement croissants")
ok(b.index_held > 0 and b.grains_dropped == b.index_held,
   "excès soutenu : trames retenues comptées et non publiées", (b.index_held, b.grains_dropped))
ok(lib.published[3] == pb and c.grains_dropped == b.grains_dropped,
   "writer sœur : abandonne la même trame, publie les mêmes index",
   (len(lib.published[3]), len(pb), c.grains_dropped))
ok(len(pb) == 100 - b.index_held, "excès soutenu : publiés + retenus = trames rendues",
   (len(pb), b.index_held))

# 4. Le régime d'avant le correctif : dernier index publié 68 s devant la grille → retenue + ALARME, rien
#    publié tant que la grille n'a pas rattrapé, puis reprise et levée de l'alarme.
d = writer("d", 4)
lib.g += 1
frame(d)
lib.g -= 3430                       # l'horloge recule de 68,6 s sous un writer déjà publié
avant = len(lib.published[4])
for _ in range(10):
    lib.g += 1
    frame(d)
ok(d.index_alarm, "avance de 68 s : alarme levée", d.index_ahead)
ok(len(lib.published[4]) == avant, "avance de 68 s : rien publié dans le futur",
   lib.published[4][avant:][:3])
lib.g = d._counter + 1
frame(d)
ok(not d.index_alarm and lib.published[4][-1] == lib.g,
   "grille rattrapée : reprise sur la grille, alarme levée", (d.index_alarm, lib.published[4][-1]))

# 5. Propagation src_index : une source qui RECULE ne fait pas reculer la sortie, et ce chemin
#    n'est PAS borné contre la grille (sa référence est l'index source). Depuis le 2026-10-25 la
#    trame dont l'index source n'avance pas est RETENUE (`prop_held`) au lieu de partir à
#    « compteur + 1 » — ce cran d'avance ne se résorbait jamais (cf. verif_next_index_propagation).
e = writer("e", 5)
for s in (lib.g - 10, lib.g - 9, lib.g - 30, lib.g - 29):
    i, gi, v = e.open_grain(src_index=s)
    e.commit(gi)
pe = lib.published[5]
ok(pe == [lib.g - 10, lib.g - 9] and e.prop_held == 2 and e.index_held == 0,
   "src_index : source repartie en arrière → retenue (prop_held), ni recul ni cran d'avance", pe)

# 6. Allouer puis dormir (`lookahead=1`) : l'avance demandée n'est pas comptée comme excès.
f = writer("f", 6)
for _ in range(10):
    lib.g += 1
    i, gi, v = f.open_grain(index=f.next_index(lookahead=1))
    f.commit(gi)
ok(lib.published[6][-1] == lib.g + 1 and f.index_ahead == 0 and f.index_held == 0,
   "lookahead=1 : grille + 1, avance comptée 0", (lib.published[6][-1] - lib.g, f.index_ahead))

# 7. Rapport :8080.
z = writer("z", 7)                       # n'a jamais rien écrit
r = bobimxl.index_report()
ok(r["flows"].get("b", {}).get("held") == b.index_held and r["dropped"] >= b.grains_dropped
   and "a" in r["flows"] and r["flows"].get("e", {}).get("prop_held") == 2
   and "z" not in r["flows"],
   "index_report : agrège retenues/abandons (dont prop_held), omet les writers sans rien", r)

# 8. grid_ms : la MÊME règle pour tous les writers, quelle que soit l'origine de leur index.
#    Heure du double = début du créneau g + 5 ms.
h = writer("h", 8)
lib.g += 1
frame(h)
ok(h.grid_ms() == -5.0, "grid_ms : producteur sur la grille → −5 ms (dans son créneau)", h.grid_ms())
_att = 20.0 * (lib.published[6][-1] - lib.g) - 5.0     # f : dernier index écrit vs heure du double
ok(f.grid_ms() == _att, "grid_ms : lookahead=1, mesuré sur le dernier index écrit", (f.grid_ms(), _att))
fl = writer("fl", 9, interlace="interlaced_tff")
for fi in (2 * lib.g, 2 * lib.g + 1):    # champs de la trame g, index imposé (base×2+parité)
    _i, gi, v = fl.open_grain(index=fi)
    fl.commit(gi)
ok(fl.grid_ms() == 5.0, "grid_ms : champ entrelacé compté à la cadence CHAMP (+5 ms, pas +20 s)",
   fl.grid_ms())
k = writer("k", 10)
_i, gi, v = k.open_grain(index=lib.g + 100)   # index IMPOSÉ, hors next_index (genlock, maison)
k.commit(gi)
ok(k.grid_ms() == 1995.0, "grid_ms : index imposé dans le futur → +1995 ms, sans next_index",
   k.grid_ms())
r = bobimxl.index_report()
ok(r["flows"].get("k", {}).get("grid_ms") == 1995.0 and (r["grid_ms_max"] or 0) >= 1995.0
   and r["flows"].get("h", {}).get("grid_ms") == -5.0,
   "index_report : grid_ms par flux et grid_ms_max", {n: e.get("grid_ms") for n, e in r["flows"].items()})

print()
if ko:
    print("%d ÉCHEC(S)" % len(ko))
    sys.exit(1)
print("tout est vert")
