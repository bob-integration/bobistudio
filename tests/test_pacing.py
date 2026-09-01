#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Test OFFLINE de _derive_pacing (profil d'émission 2110-21 → MTL_PACING device-level).
# DÉFAUT = narrow ; émis SEULEMENT sur un nœud à port dpdk (af_xdp = iso, rien émis).
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import docker_driver as dd

_IFACES = {}   # node_id → liste de node_interfaces
dd_db = None

def _fake_ifaces(node_id):
    return _IFACES.get(node_id, [])

# Monkeypatch db_get_node_interfaces (importé dynamiquement dans _derive_pacing)
import app.database as _adb
_adb.db_get_node_interfaces = _fake_ifaces

n = 0
def check(label, cond, got=None):
    global n; n += 1
    print(("  [OK] " if cond else "  [KO] ") + label + ("" if cond else f"  → {got!r}"))
    assert cond, label

def case(name, ifaces):
    _IFACES.clear(); _IFACES[1] = ifaces
    return dd._derive_pacing({"id": 1})

DP = lambda role="media2110", pmd=None, prof=None: {"role": role, "pmd": pmd, "output_profile": prof}

print("_derive_pacing — défaut narrow, dpdk-gated")
p,_ = case("dpdk sans profil → défaut narrow → rl", [DP(pmd="dpdk")]);            check("dpdk non configuré → rl (défaut narrow)", p == "rl", p)
p,_ = case("dpdk narrow explicite", [DP(pmd="dpdk", prof="narrow")]);            check("dpdk narrow → rl", p == "rl", p)
p,_ = case("dpdk narrow_linear", [DP(pmd="dpdk", prof="narrow_linear")]);        check("dpdk NL → rl", p == "rl", p)
p,_ = case("dpdk wide (toutes wide)", [DP(pmd="dpdk", prof="wide")]);            check("dpdk tout wide → tsc", p == "tsc", p)
p,_ = case("dpdk mixte narrow+wide", [DP(pmd="dpdk", prof="wide"), DP(pmd="dpdk", prof="narrow")]); check("mixte → rl (mécanisme narrow-wins)", p == "rl", p)
p,_ = case("dpdk wide + non configuré (=narrow)", [DP(pmd="dpdk", prof="wide"), DP(pmd="dpdk")]);   check("wide + défaut narrow → rl", p == "rl", p)
p,_ = case("af_xdp pur (pas de dpdk) → None (iso)", [DP(pmd=None, prof="narrow")]); check("af_xdp → None (rien émis)", p is None, p)
p,_ = case("af_xdp pur sans pmd", [DP(pmd="af_xdp", prof="wide")]);              check("af_xdp explicite → None", p is None, p)
p,_ = case("aucune media2110", [DP(role="management", pmd="dpdk")]);             check("pas de media2110 → None", p is None, p)

print(f"\nTous les tests pacing passent ({n} assertions).")
