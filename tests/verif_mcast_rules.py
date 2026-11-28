#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
"""Offline guard: multicast range rules and group exclusivity (app/allocations.py).

Two defects measured on 2026-11-11:
  1. Format criteria compared as TEXT: a rule saved as `{"fps": 50}` never matched a TX slot
     carrying `fps: 50.0` ("50" != "50.0"), so the rule silently never applied.
  2. The scan "first free (ip, port)" piled several flows on one multicast GROUP: the 2022-7 leg-1
     audios of TX1 and TX2 both landed on the leg-0 video group of TX1. IGMP subscribes to a group,
     not a port, so a receiver could not separate them.

  A. Numeric criteria: 50 == 50.0 == "50", 59.94 == 60000/1001; mismatch, missing key, None rejected.
  B. Rule specificity ordering unchanged.
  C. Scan never returns an address held by another flow on another port (ledger and registry).
  D. Leg 1 on a range shared by both legs: distinct groups from leg 0 and from each other.
  E. An owner re-allocating gets the same address back (plan path and scan path).
  F. Planned address held by another flow on another port → distinct group from the scan.
  G. Concurrent reservations of one group on two ports: the later row backs off, the earlier keeps.
  H. Global pool: same group exclusivity.
  I. Exhaustion still returns (None, None); the panel sees the range as exhausted.

Throwaway database, set up BEFORE any import that opens it; no network.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ⚠ BEFORE any access: one SQLite connection is kept PER THREAD and reused — overriding DB_PATH
# after a first access isolates nothing (cf. CLAUDE.md, "Modèle de threading").
from app import config, database  # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_mcast_rules_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database.init_db()

from app import allocations as al  # noqa: E402
from app.database import db_add_mcast_range, db_reserve_mcast, get_db  # noqa: E402

failures = []


def check(label, cond, got=None):
    print(("  [OK] " if cond else "  [KO] ") + label + ("" if cond else f"  → {got!r}"))
    if not cond:
        failures.append(label)


def ledger():
    with get_db() as db:
        return [(r["ip"], r["port"], r["owner_ref"])
                for r in db.execute("SELECT ip, port, owner_ref FROM mcast_allocations ORDER BY rowid")]


def holders(ip):
    return {o for i, _p, o in ledger() if i == ip}


def rule(rid, match=None, essence=None, leg=None):
    return {"id": rid, "essence": essence, "leg": leg, "match_json": match}


# Registry stand-in: tests choose what the NMOS registry shows.
REGISTRY = []
al._registry_transports = lambda: list(REGISTRY)

# ── A. Numeric criteria ───────────────────────────────────────────────────────────────────────────
print("A. numeric format criteria")
R = rule(1, '{"fps":50,"scan":"p"}', essence="video")
check("fps 50 (rule) matches 50.0 (slot)", al._regle_correspond(R, "video", 0, {"scan": "p", "fps": 50.0}) is not None)
check("fps 50 matches \"50\"", al._regle_correspond(R, "video", 0, {"scan": "p", "fps": "50"}) is not None)
check("fps \"50\" (rule) matches 50.0",
      al._regle_correspond(rule(1, '{"fps":"50"}'), "video", 0, {"fps": 50.0}) is not None)
check("59.94 matches 60000/1001",
      al._regle_correspond(rule(1, '{"fps":59.94}'), "video", 0, {"fps": 60000 / 1001}) is not None)
check("1080 matches 1080.0 (height)",
      al._regle_correspond(rule(1, '{"height":1080}'), "video", 0, {"height": 1080.0}) is not None)
check("fps 50 rejects 25", al._regle_correspond(R, "video", 0, {"scan": "p", "fps": 25}) is None)
check("59.94 rejects 60", al._regle_correspond(rule(1, '{"fps":59.94}'), "video", 0, {"fps": 60}) is None)
check("scan p rejects i", al._regle_correspond(R, "video", 0, {"scan": "i", "fps": 50.0}) is None)
check("missing key rejected", al._regle_correspond(R, "video", 0, {"scan": "p"}) is None)
check("None value rejected", al._regle_correspond(R, "video", 0, {"scan": "p", "fps": None}) is None)
check("no fmt rejected", al._regle_correspond(R, "video", 0, None) is None)
check("null criterion does not match a missing key",
      al._regle_correspond(rule(1, '{"fps":null}'), "video", 0, {}) is None)
check("non-numeric text still compared as text",
      al._regle_correspond(rule(1, '{"scan":"p"}'), "video", 0, {"scan": "p"}) is not None)

# ── B. Specificity ordering ─────────────────────────────────────────────────────────────────────
print("B. specificity ordering")
rows = [rule(10), rule(11, essence="video"), rule(12, '{"fps":50}', essence="video"),
        rule(13, essence="video", leg=0), rule(14, essence="audio")]
best = al._meilleure_regle(rows, "video", 0, {"fps": 50.0})
check("leg-specific beats criteria (essence, leg, nb_crit, id)", best and best["id"] == 13, best)
best = al._meilleure_regle(rows[:3], "video", 0, {"fps": 50.0})
check("fps 50 rule beats essence-only for a 50.0 slot", best and best["id"] == 12, best)
best = al._meilleure_regle(rows[:3], "video", 0, {"fps": 25})
check("essence-only chosen for a 25 slot", best and best["id"] == 11, best)
best = al._meilleure_regle(rows[:3], "anc", 0, {"fps": 50.0})
check("generic rule for another essence", best and best["id"] == 10, best)

# ── Ranges (interface scope, no essence/leg: shared by every essence and both legs) ──────────────
db_add_mcast_range(scope="interface", node_id=1, ifname="ensC", base_ip="239.20.1.0", size=16,
                   label="scan")
db_add_mcast_range(scope="interface", node_id=1, ifname="ensA", base_ip="239.10.1.0", size=256,
                   label="shared")
db_add_mcast_range(scope="interface", node_id=1, ifname="ensB", base_ip="239.10.1.0", size=256,
                   label="shared-b")
db_add_mcast_range(scope="interface", node_id=1, ifname="ensX", base_ip="239.30.1.0", size=4,
                   label="tiny")

# ── C. Scan never piles on a held group ─────────────────────────────────────────────────────────
print("C. scan group exclusivity")
db_reserve_mcast("239.20.1.1", 5000, "other:video")
REGISTRY.append(("res-manual", {"multicast_ip": "239.20.1.2", "port": 6000}))
ip, port = al.allocate_multicast_for(1, "ensC", essence="audio", leg=0, port=5004, owner_ref="c:audio")
check("skips a group held in the ledger on another port", ip != "239.20.1.1", ip)
check("skips a group held in the registry only", ip != "239.20.1.2", ip)
check("takes the next free group", (ip, port) == ("239.20.1.3", 5004), (ip, port))
REGISTRY.clear()
# A registry transport that IS the owner's ledger row does not block the owner's group.
REGISTRY.append(("res-c", {"multicast_ip": "239.20.1.3", "port": 5004}))
taken = al._groups_held_by_others("c:audio")
check("own registry entry (ledger-backed) is not a foreign holder", "239.20.1.3" not in taken, taken)
REGISTRY.clear()

# ── D. 2022-7: legs on a shared range ───────────────────────────────────────────────────────────
print("D. leg 1 on a shared range")
legs0, legs1 = {}, {}
for s in (0, 1):
    legs0[f"v{s}"] = al.allocate_multicast_for(1, "ensA", essence="video", leg=0, port=5000,
                                               owner_ref=f"tx:9:{s}:video:leg0", slot=s)
    legs0[f"a{s}"] = al.allocate_multicast_for(1, "ensA", essence="audio", leg=0, port=5004 + 2 * s,
                                               owner_ref=f"tx:9:{s}:audio:{s}:leg0", slot=s)
check("leg-0 video planned at .1/.2",
      (legs0["v0"][0], legs0["v1"][0]) == ("239.10.1.1", "239.10.1.2"), legs0)
check("leg-0 audio planned at .101/.102",
      (legs0["a0"][0], legs0["a1"][0]) == ("239.10.1.101", "239.10.1.102"), legs0)
for s in (0, 1):
    legs1[f"v{s}"] = al.allocate_multicast_for(1, "ensB", essence="video", leg=1, port=5000,
                                               owner_ref=f"tx:9:{s}:video:leg1", slot=s)
    legs1[f"a{s}"] = al.allocate_multicast_for(1, "ensB", essence="audio", leg=1, port=5004 + 2 * s,
                                               owner_ref=f"tx:9:{s}:audio:{s}:leg1", slot=s)
g0 = {v[0] for v in legs0.values()}
g1 = [v[0] for v in legs1.values()]
check("every leg-1 flow got an address", all(g1), legs1)
check("leg-1 groups distinct from leg-0 groups", not (set(g1) & g0), (g1, g0))
check("leg-1 groups distinct from each other", len(set(g1)) == len(g1), g1)
check("leg-1 audio of TX0 not on the TX0 video group", legs1["a0"][0] != legs0["v0"][0], legs1)
multi = {}
for ip_, _p, o in ledger():
    multi.setdefault(ip_, set()).add(o)
check("no group shared by two owners in the ledger",
      all(len(v) == 1 for v in multi.values()), {k: v for k, v in multi.items() if len(v) > 1})

# ── E. Owner re-allocation ──────────────────────────────────────────────────────────────────────
print("E. owner re-allocation")
again = al.allocate_multicast_for(1, "ensA", essence="video", leg=0, port=5000,
                                  owner_ref="tx:9:0:video:leg0", slot=0)
check("plan path: same address back", again == legs0["v0"], again)
again = al.allocate_multicast_for(1, "ensB", essence="audio", leg=1, port=5004,
                                  owner_ref="tx:9:0:audio:0:leg1", slot=0)
check("scan path: same address back", again == legs1["a0"], again)
again = al.allocate_multicast_for(1, "ensC", essence="audio", leg=0, port=5004, owner_ref="c:audio")
check("scan path without slot: same address back", again == ("239.20.1.3", 5004), again)
check("no duplicate row for a re-allocating owner",
      sum(1 for _i, _p, o in ledger() if o == "tx:9:0:audio:0:leg1") == 1, ledger())

# ── F. Planned address held on another port → scan ──────────────────────────────────────────────
print("F. plan path group exclusivity")
check("precondition: slot-40 planned group .41 is free", holders("239.10.1.41") == set(), holders("239.10.1.41"))
db_reserve_mcast("239.10.1.41", 5004, "legacy:audio")        # piled audio on the slot-40 video group
ip, port = al.allocate_multicast_for(1, "ensA", essence="video", leg=0, port=5000,
                                     owner_ref="tx:9:40:video:leg0", slot=40)
check("planned group held by another flow on another port is not shared", ip and ip != "239.10.1.41", ip)
check("fallback group held by nobody else", holders(ip) == {"tx:9:40:video:leg0"}, holders(ip))
ip, port = al.allocate_multicast_for(1, "ensA", essence="video", leg=0, port=5000,
                                     owner_ref="tx:9:41:video:leg0", slot=41)
check("free planned group still taken by the plan", ip == "239.10.1.42", ip)

# ── G. Concurrent reservation of one group on two ports ─────────────────────────────────────────
print("G. deterministic loser")
db_reserve_mcast("239.10.1.200", 5000, "early")
check("later row on another port backs off", al._claim_group("239.10.1.200", 5004, "late") is False)
check("loser's row released, winner kept", holders("239.10.1.200") == {"early"}, holders("239.10.1.200"))
db_reserve_mcast("239.10.1.201", 5000, "first")
db_reserve_mcast("239.10.1.201", 5004, "second")              # concurrent insert accepted by SQLite
check("earlier row keeps the group", al._claim_group("239.10.1.201", 5000, "first") is True)
check("later row yields", al._claim_group("239.10.1.201", 5004, "second") is False)
check("only the earlier owner remains", holders("239.10.1.201") == {"first"}, holders("239.10.1.201"))

# ── H. Global pool ──────────────────────────────────────────────────────────────────────────────
print("H. global pool")
p1 = al.allocate_multicast(5000, owner_ref="pool:video")
p2 = al.allocate_multicast(5004, owner_ref="pool:audio")
check("pool: first flow gets .1", p1 == ("239.100.0.1", 5000), p1)
check("pool: second flow on another port gets another group", p2[0] and p2[0] != p1[0], p2)
check("pool: owner re-allocation", al.allocate_multicast(5004, owner_ref="pool:audio") == p2)
nr = al.allocate_multicast_for(5, "nope", essence="audio", port=5006, owner_ref="pool:norule")
check("no rule → pool, still a distinct group", nr[0] not in (p1[0], p2[0]), nr)

# ── I. Exhaustion ───────────────────────────────────────────────────────────────────────────────
print("I. exhaustion")
for i in (1, 2, 3):
    db_reserve_mcast(f"239.30.1.{i}", 7000 + i, f"fill:{i}")
ex = al.allocate_multicast_for(1, "ensX", essence="audio", port=5004, owner_ref="x:audio")
check("full range (every group held on other ports) → (None, None)", ex == (None, None), ex)
check("no row left for the refused owner", not any(o == "x:audio" for _i, _p, o in ledger()))
labels = [r.get("label") for r in al.plages_epuisees(transports=[])]
check("panel reports the full range", "tiny" in labels, labels)
check("panel does not report a range with free groups", "shared" not in labels, labels)

print()
if failures:
    print(f"FAIL: {len(failures)} check(s): " + "; ".join(failures))
    sys.exit(1)
print("OK: all checks passed")
