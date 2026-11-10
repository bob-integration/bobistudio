#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""SDI output alarm: frames played LATE, dropped or repeated (app/metrics.py, 2026-11-09).

Z8, 2026-10-30: 77 % of the frames of an on-air DeckLink output were played late for 33 minutes,
with no alarm — a constant offset, invisible on screen, every other counter green. Form validated
by Cyril the same day, held here offline (synthetic :8080 readings, no container):
  1. per output, on a sliding 60 s window — never on a cumulative counter;
  2. more than 1 % late → warning, once, not once per reading;
  3. one dropped or repeated frame → error at once; on an output playing a MULTIVIEW, warning;
  4. clean for a whole window → one « cleared » alert;
  5. a binary restart (counters going back) is not a loss; an older plugin without card
     counters is ignored; a removed output leaves no state behind;
  6. the alarm is wired on every :8080 reading, and its messages carry their parameters.

    ./venv/bin/python tests/verif_alerte_sorties_sdi.py
"""
import json
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_sdi_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import metrics as M                                                  # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


alertes = []
M.db_add_alert = lambda key, niveau="info", **kw: alertes.append((key, niveau))
M.db_get_container = lambda vmid: {"hostname": "sdi-%s" % vmid, "deploy_config": "{}"}
M._node_de = lambda vmid: 1
PRODUCTEUR = {"mv_out": "multiview", "pgm": "mixer"}
M._producteur_de = lambda flow: flow if flow in PRODUCTEUR else None
_vrai_dbget = M.db_get_container
M.db_get_container = lambda vmid: ({"hostname": "src", "deploy_config": json.dumps({"type": PRODUCTEUR[vmid]})}
                                   if vmid in PRODUCTEUR else {"hostname": "sdi-%s" % vmid})


class Sortie:
    """One output read every 5 s at 50 fps; counters only move when told to."""
    def __init__(self, vmid, flow="pgm", pid="p1"):
        self.vmid, self.flow, self.pid = vmid, flow, pid
        self.t, self.idx, self.late, self.drop, self.rep = 1000.0, 10_000_000, 0, 0, 0

    def releve(self, late_par_releve=0, drop=0, rep=0, n=1, voie=None):
        for _ in range(n):
            self.t += 5.0
            self.idx += 250
            self.late += late_par_releve
            self.drop += drop
            self.rep += rep
            v = voie or {"persistent_id": self.pid, "sortie": 1, "flow": self.flow, "fps": 50.0,
                         "frame_index": self.idx, "late": self.late, "dropped": self.drop,
                         "repeated": self.rep}
            M._noter_sorties_sdi(self.vmid, {"voies": [v]}, now=self.t)


def cles():
    return [a for a in alertes]


print("\n1-2. late ratio on a sliding window")
s = Sortie(1)
s.releve(n=30)
verifie(cles() == [], "healthy output, 2.5 min → silent", cles())
del alertes[:]
s.releve(late_par_releve=5, n=30)                       # 5/250 = 2 %
verifie(cles() == [("alert.tx.sdi_retard", "warning")], "2 % late, sustained → ONE warning", cles())
del alertes[:]
s.releve(n=10)                                          # 50 s clean: window still sees late
verifie(cles() == [], "50 s clean: the window still holds late frames → no clearing yet", cles())
s.releve(n=4)
verifie(cles() == [("alert.tx.sdi_retablie", "info")], "a whole clean window → cleared, once", cles())

del alertes[:]
s2 = Sortie(2)
s2.releve(late_par_releve=1, n=40)                      # 1/250 = 0.4 %
verifie(cles() == [], "0.4 % late → below the 1 % threshold", cles())

del alertes[:]
s3 = Sortie(3)
s3.late = 5_000_000                                     # huge CUMULATIVE count from before
s3.releve(n=30)
verifie(cles() == [], "a large cumulative count that does not move → silent", cles())

print("\n3. a lost frame is an error at once; a multiview output stays at warning")
del alertes[:]
s4 = Sortie(4)
s4.releve(n=2)
s4.releve(drop=1)
verifie(cles() == [("alert.tx.sdi_pertes", "error")], "ONE dropped frame, 15 s after start → error now", cles())
del alertes[:]
s4.releve(n=5)
verifie(cles() == [], "still in the window → no repeat", cles())
del alertes[:]
s5 = Sortie(5)
s5.releve(n=20)
s5.releve(rep=3)
verifie(cles() == [("alert.tx.sdi_pertes", "error")], "repeated frames on a PGM output → error", cles())
del alertes[:]
s6 = Sortie(6, flow="mv_out")
s6.releve(n=20)
s6.releve(rep=3)
verifie(cles() == [("alert.tx.sdi_pertes", "warning")], "same on an output playing a multiview → warning", cles())

print("\n5. restarts, older plugins, removed outputs")
del alertes[:]
s7 = Sortie(7)
s7.drop, s7.late = 40, 900
s7.releve(n=20)
s7.drop, s7.late, s7.rep = 0, 0, 0                      # binary restarted: counters back to 0
s7.releve(n=20)
verifie(cles() == [], "counters going back (restart) → not a loss", cles())
del alertes[:]
s8 = Sortie(8)
s8.releve(n=20, voie={"persistent_id": "p1", "sortie": 1, "flow": "pgm", "frame_index": 1, "repeated": 9, "missed": 9})
verifie(cles() == [] and not any(k[0] == 8 for k in M._sdi_etat),
        "older plugin without card counters → ignored", cles())
M._noter_sorties_sdi(7, {"voies": []}, now=s7.t + 5)
verifie(not any(k[0] == 7 for k in M._sdi_etat), "output removed → its state is dropped")
M._noter_sorties_sdi(9, {"fps": 50.0}, now=0.0)
verifie(not any(k[0] == 9 for k in M._sdi_etat), "not a DeckLink container → untouched")

print("\n6. wiring and messages")
src = open(os.path.join(RACINE, "app", "metrics.py"), encoding="utf-8").read()
corps = src[src.index("def rafraichir_metrics("):]
verifie("_noter_sorties_sdi(vmid, m)" in corps, "called on every :8080 reading (rafraichir_metrics)")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", "%s.json" % lang), encoding="utf-8"))
    for k, champs in (("alert.tx.sdi_retard", {"sortie", "h", "flow", "pct", "s"}),
                      ("alert.tx.sdi_pertes", {"sortie", "h", "flow", "drop", "rep", "s"}),
                      ("alert.tx.sdi_retablie", {"sortie", "h", "flow", "s"})):
        vus = set(re.findall(r"\{(\w+)\}", cat.get(k, "")))
        verifie(vus == champs, "%s %s: placeholders" % (lang, k), vus)

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
