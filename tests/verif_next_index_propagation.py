#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
"""Vérifie `bobimxl.Writer.next_index` en PROPAGATION (src_index) : jamais de cran d'avance permanent.

Pourquoi ce banc existe : en propagation, `next_index` rendait max(source, compteur + 1). Une seule
trame où l'index source n'avance pas (correcteur en image entière dont l'entrée manque, donc
publication sur la grille ; player dont la source repart plus bas ; split 0.24.2) installait un
cran d'avance que rien ne résorbait : le contenu restait pour toujours une trame en retard sur son
étiquette. Décidé le 2026-10-25 (session Mélangeur) :
  1. source qui avance : l'index publié EST l'index source ;
  2. source qui n'avance pas : trame RETENUE (non publiée), comptée `prop_held`, sans alarme ;
  3. après une retenue, reprise exacte sur l'index source — aucun cran d'avance ;
  4. trame publiée sur la grille puis source plus ancienne : retenue qui converge ;
  5. grand recul (> PROP_JUMP_S) : NOUVELLE GÉNÉRATION du flux (répertoire purgé, writer recréé),
     puis publication à l'index source ;
  6. index_report publie prop_held et regenerations, hors alarme.

Sans libmxl : `_lib` est remplacée par un double (comme verif_next_index_borne.py).

    ./venv/bin/python tests/verif_next_index_propagation.py
"""
import ctypes
import os
import sys
import tempfile

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
    def __init__(self):
        self.g = 1_000_000
        self.bufs = {}
        self.published = {}
        self.created = []
        self.released = []

    def mxlGetCurrentIndex(self, rate_ref):
        return self.g

    def mxlFlowWriterOpenGrain(self, h, idx, gi_ref, payload_ref):
        buf = self.bufs.setdefault(h.value, (ctypes.c_uint8 * 64)())
        gi = gi_ref._obj
        gi.index = idx
        gi.grainSize = 64
        gi.totalSlices = 4
        payload_ref._obj.contents = ctypes.cast(buf, bobimxl._PU8).contents
        return bobimxl.MXL_STATUS_OK

    def mxlFlowWriterCommitGrain(self, h, gi_ref):
        self.published.setdefault(h.value, []).append(int(gi_ref._obj.index))
        return bobimxl.MXL_STATUS_OK

    def mxlReleaseFlowWriter(self, inst_h, h):
        self.released.append(h.value)
        return bobimxl.MXL_STATUS_OK

    def mxlCreateFlowWriter(self, inst_h, flow_def, opts, h_ref, _x, created_ref):
        h_ref._obj.value = 100 + len(self.created)       # nouveau handle
        created_ref._obj.value = True
        self.created.append(flow_def)
        return bobimxl.MXL_STATUS_OK


lib = FakeLib()
bobimxl._lib = lib
DOMAIN = tempfile.mkdtemp(prefix="mxl-dom-")


class Inst:
    domain = DOMAIN
    _h = bobimxl._VOID(1)


def writer(name, hid):
    w = object.__new__(bobimxl.Writer)
    w.name = name
    w.inst = Inst()
    w.flow_def = '{"id": "x"}'
    w.rate = bobimxl.mxlRational(50, 1)
    w.index_mode = "tai"
    w.ref = None
    w.frame_size = 64
    w._counter = 0
    w._last_idx = -1
    w._scratch = None
    w._h = bobimxl._VOID(hid)
    bobimxl._live_writers.add(w)
    return w


def pub(w, src=None):
    """Une trame propagée (src) ou générée (None) ; rend l'index renvoyé par open_grain."""
    idx, gi, view = w.open_grain(src_index=src) if src is not None else w.open_grain()
    view[:] = 1
    w.commit(gi)
    return idx


S = 900_000
# 1-3. Source qui avance, puis qui n'avance pas, puis repart.
a = writer("a", 1)
for k in range(5):
    pub(a, S + k)
ok(lib.published[1] == [S + k for k in range(5)], "source qui avance : index publié = index source",
   lib.published[1])
pub(a, S + 4)                       # la source n'a pas avancé
ok(lib.published[1][-1] == S + 4 and len(lib.published[1]) == 5 and a.prop_held == 1,
   "source qui n'avance pas : trame retenue, comptée prop_held", (lib.published[1][-2:], a.prop_held))
for k in range(5, 9):
    pub(a, S + k)
ok(lib.published[1][-4:] == [S + 5, S + 6, S + 7, S + 8],
   "après la retenue : reprise exacte, aucun cran d'avance", lib.published[1][-4:])
ok(a.index_alarm is False, "retenue de propagation : jamais d'alarme")

# 4. Publication sur la grille (entrée absente), puis source plus ancienne qui avance : converge.
b = writer("b", 2)
lib.g = S + 50
pub(b)                              # grille → S+50
srcs = [S + 48 + k for k in range(5)]
for x in srcs:
    pub(b, x)
ok(lib.published[2] == [S + 50, S + 51, S + 52],
   "grille puis source 2 crans plus bas : retenue jusqu'à la dépasser, puis reprise à l'index source",
   lib.published[2])
ok(b.prop_held == 3, "grille puis source plus basse : 3 retenues (S+48, S+49, S+50 ≤ S+50)", b.prop_held)

# 5. Grand recul (> PROP_JUMP_S) : nouvelle génération.
c = writer("c", 3)
fdir = os.path.join(DOMAIN, bobimxl.flow_id("c") + ".mxl-flow")
os.makedirs(fdir)
open(os.path.join(fdir, "data"), "w").write("ancien anneau")
pub(c, S + 10_000)
idx = pub(c, S)                     # source repartie 10 000 grains plus bas (200 s)
ok(c.regenerations == 1 and not os.path.exists(fdir) and lib.created,
   "grand recul : flux purgé et recréé (nouvelle génération)",
   (c.regenerations, os.path.exists(fdir), len(lib.created)))
ok(idx == S and lib.published.get(100) == [S] and 3 in lib.released,
   "grand recul : publié à l'index source sur le nouveau writer", (idx, lib.published.get(100)))
ok(c.prop_held == 0, "grand recul : pas compté comme une retenue", c.prop_held)

# Recul juste sous le seuil : retenue, pas de nouvelle génération.
d = writer("d", 4)
seuil = int(bobimxl.Writer.PROP_JUMP_S * 50)
pub(d, S + seuil)
pub(d, S)
ok(d.regenerations == 0 and d.prop_held == 1, "recul sous le seuil : retenue, pas de régénération",
   (d.regenerations, d.prop_held))

# 6. index_report.
r = bobimxl.index_report()
fa, fc = r["flows"].get("a", {}), r["flows"].get("c", {})
ok(fa.get("prop_held") == 1 and fc.get("regenerations") == 1 and r["alarm"] is False,
   "index_report : prop_held et regenerations publiés, sans alarme", r["flows"])

if ko:
    print("\n%d échec(s)" % len(ko))
    sys.exit(1)
print("\nTOUT OK : propagation sans cran d'avance, grand recul en nouvelle génération")
