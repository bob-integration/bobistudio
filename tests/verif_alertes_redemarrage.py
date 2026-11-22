#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""A restart of the orchestrator does not replay what was already said (2026-11-21).

On 2026-10-20 a dozen restarts read as a string of breakdowns: each start announced again the
same stuck containers (« émet N fps mais ne reçoit plus rien ») and a « boucle de surveillance
lente » that was only the first pass on cold caches. Held here, offline (throwaway database,
episode file next to it):
  1. a stuck container alerted before a restart stays silent after it while still stuck;
  2. once it recovers, a new stall is said again — and a recycled vmid (other instance) too;
  3. the first surveillance pass is logged, not alerted; a slow second pass still alerts.

    ./venv/bin/python tests/verif_alertes_redemarrage.py
"""
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402
_TMP = tempfile.mkdtemp(prefix="verif_alertes_redem_")
config.DB_PATH = database.DB_PATH = os.path.join(_TMP, "t.db")
database._tls.__dict__.pop("conn", None)
database.init_db()
from app import episodes                                                      # noqa: E402
episodes.DB_PATH = config.DB_PATH
from app import metrics                                                       # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


def redemarrer():
    """What a restart does to this state: memory gone, episode file reread."""
    metrics._decroche_ctr.clear()
    metrics._decroche_dit = episodes.EtatEpisodes("decrochage")
    if not metrics._decroche_dit.chemin.startswith(_TMP):
        sys.exit("episode file outside the throwaway directory: " + metrics._decroche_dit.chemin)


def alertes(vmid):
    return [a for a in database.db_get_alerts(vmid=vmid) if "decrochage" in (a.get("msg_key") or "")]


def set_uuid(vmid, uuid):
    db = database.get_db()
    db.execute("UPDATE containers SET instance_uuid=? WHERE vmid=?", (uuid, vmid))
    db.commit()


MUET = {"pushed_fps": 25.0, "in_fps_seen": 0.0}
VIVANT = {"pushed_fps": 25.0, "in_fps_seen": 25.0}


def polls(vmid, m, n=metrics._DECROCHE_SEUIL):
    return [metrics._detecter_decrochage(vmid, m, {}) for _ in range(n)]


redemarrer()
n = database.db_add_node("n1", "192.0.2.9", kind="docker")
database.db_upsert_container_docker(1187, "mv", n, "bobi-cmp-1187")
set_uuid(1187, "uuid-a")

print("\n1. stuck across a restart")
verifie(any(polls(1187, MUET)) and len(alertes(1187)) == 1, "stuck: said once", len(alertes(1187)))
redemarrer()
verifie(not any(polls(1187, MUET, 3 * metrics._DECROCHE_SEUIL)) and len(alertes(1187)) == 1,
        "after a restart, still stuck: not said again", len(alertes(1187)))

print("\n2. a new stall is said")
polls(1187, VIVANT, 1)
redemarrer()
verifie(any(polls(1187, MUET)), "recovered then stuck again: said")
set_uuid(1187, "uuid-b")
redemarrer()
verifie(any(polls(1187, MUET)), "recycled vmid (other instance): said")
metrics._decroche_ctr.clear()
verifie(not any(polls(1187, {"pushed_fps": 0.0, "in_fps_seen": 0.0}, 1)) and
        metrics._decroche_dit.get(1187) is None, "stopped emitting: the episode is cleared")

print("\n3. the first surveillance pass")
src = open(os.path.join(RACINE, "main.py"), encoding="utf-8").read()
m = re.search(r"\n_TICK_SLOW_S = .*?(?=\n_PYR_RECONCILE_S)", src, re.S)
verifie(bool(m), "watchdog block found in main.py")
dites = []


class _Log:
    def __init__(self): self.infos = []
    def info(self, msg, *a): self.infos.append(msg)
    def warning(self, msg, *a): pass


ns = {"db_add_alert": lambda msg, lvl="info", **k: dites.append((msg, lvl)), "log": _Log()}
exec(m.group(0) if m else "", ns)
ns["_watchdog_tick"](38.0)
verifie(not dites and ns["log"].infos, "first pass at 38 s: logged, no alert", dites)
ns["_watchdog_tick"](5.0)
ns["_watchdog_tick"](38.0)
verifie(len(dites) == 1 and dites[0][1] == "warning", "a later slow pass alerts", dites)

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
