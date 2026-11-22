#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""An alert state that was posed is on disk, whatever its callers do next (2026-11-21).

`EtatEpisodes.flush` was debounced at 30 s and only caught up at the NEXT change: a state posed
within 30 s of the start (or of the previous write) with no change behind it was never written,
so a restart lost it and its alert was said again. Seven modules posed without ever flushing.
Held here, offline (throwaway directory, debounce shortened):
  1. the first change after a start is written at once;
  2. a change inside the debounce window is written at its end with no further call — one timer
     for a burst of changes;
  3. a clean exit writes what is still pending;
  4. concurrent writers leave a readable file that matches the state.

    ./venv/bin/python tests/verif_episodes_ecriture_garantie.py
"""
import json
import os
import sys
import tempfile
import threading
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import episodes                                                      # noqa: E402
_TMP = tempfile.mkdtemp(prefix="verif_episodes_")
from app import database                                                      # noqa: E402
database.DB_PATH = os.path.join(_TMP, "t.db")   # episodes follow the CURRENT database (§5)
episodes.FLUSH_S = 0.4

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


def disque(e):
    try:
        return json.load(open(e.chemin))
    except (OSError, ValueError):
        return None


e = episodes.EtatEpisodes("essai")
if not e.chemin.startswith(_TMP):
    sys.exit("episode file outside the throwaway directory: " + e.chemin)

print("\n1. first change after a start")
e.poser(1187, "uuid-a")
verifie(disque(e) == {"1187": "uuid-a"}, "written at once", disque(e))

print("\n2. inside the debounce window")
e.poser(1188, "x")
e.poser(1189, "y")
verifie("1188" not in (disque(e) or {}), "not yet written (debounced)", disque(e))
verifie(threading.active_count() >= 2 and e._timer is not None, "one deferred write armed")
t1 = e._timer
e.poser(1190, "z")
verifie(e._timer is t1, "a burst arms ONE timer")
time.sleep(episodes.FLUSH_S + 0.3)
verifie(disque(e) == {"1187": "uuid-a", "1188": "x", "1189": "y", "1190": "z"},
        "written at the end of the window, with no further call", disque(e))
verifie(e._timer is None, "timer released")

print("\n3. clean exit")
time.sleep(episodes.FLUSH_S + 0.1)
e.poser(1, "a")                               # written at once (window elapsed)
e.retirer(1188)                               # pending
episodes._flush_all()
verifie("1188" not in (disque(e) or {}) and disque(e).get("1") == "a",
        "the exit hook writes what is pending", disque(e))

print("\n4. concurrent writers")
import logging                                                                # noqa: E402
echecs = []
class _Capte(logging.Handler):
    def emit(self, rec):
        if rec.levelno >= logging.WARNING:
            echecs.append(rec.getMessage())
episodes.log.addHandler(_Capte())
_vrai_dump = episodes.json.dump
def _dump_lent(obj, fh):            # widens the race window: open → write → (others) → replace
    _vrai_dump(obj, fh)
    time.sleep(0.003)
episodes.json.dump = _dump_lent
f = episodes.EtatEpisodes("concurrence")
def ecrire(i):
    for j in range(50):
        f.poser((i, j), j)
        if j % 7 == 0:
            f.flush(force=True)
fils = [threading.Thread(target=ecrire, args=(i,)) for i in range(8)]
for x in fils: x.start()
for x in fils: x.join()
f.flush(force=True)
d = disque(f)
verifie(isinstance(d, dict) and len(d) == 400 and d == f._d, "file readable and complete",
        None if d is None else len(d))
verifie(not [n for n in os.listdir(_TMP) if n.endswith(".tmp")], "no stray .tmp left")
verifie(not echecs, "no failed write", echecs[:2])
episodes.json.dump = _vrai_dump

print("\n5. the file follows the database in use, not the one at import")
g = episodes.EtatEpisodes("suit_la_base")
autre = tempfile.mkdtemp(prefix="verif_episodes_autre_")
database.DB_PATH = os.path.join(autre, "t.db")
g.poser("k", 1)
verifie(os.path.exists(os.path.join(autre, "episodes_suit_la_base.json")),
        "a state built before the database moved writes next to the new one")
database.DB_PATH = os.path.join(_TMP, "t.db")

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
