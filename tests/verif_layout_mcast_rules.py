#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline guard: a TX layout allocates multicast through the SAME rule resolution as the deploy hook.

Defect: `io2110_layouts.apply_layout` called `allocate_multicast_for(node_id, ifname)` with no media
network, format or port, so a NETWORK-scope `mcast_ranges` rule was never found and the flow came
from the global pool (an audio at 239.100.0.40 while the network rule said 239.10.1.0/24).

Drives the allocation helpers apply_layout uses (`_layout_alloc_ctx`, `flat_audio_idx`,
`_alloc_layout_flow`) rather than the whole apply function (which needs a container and a live node).
Throwaway database, created BEFORE any import that opens the DB.

    ./venv/bin/python tests/verif_layout_mcast_rules.py
"""
import ipaddress
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app.database as database                                           # noqa: E402
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="layout_mcast_db_"), "principale.db")
database.init_db()
from app import allocations as alloc, io2110_layouts as lay              # noqa: E402

fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def run_slots(node_id, ukey, vmid, params, decl, n_slots):
    """Same sequence as apply_layout, per slot; returns [(essence, ip, port)]."""
    out = []
    for i in range(n_slots):
        slot = {}
        ctx = lay._layout_alloc_ctx(node_id, ukey, i, decl, slot, params, n_slots)
        out.append(("video", i) + lay._alloc_layout_flow(
            ctx, "video", lay.layout_owner_ref(vmid, i, "video"), i))
        for ai in range(decl["audio_count"]):
            aidx = lay.flat_audio_idx(params, n_slots, i, ai)
            out.append(("audio", i) + lay._alloc_layout_flow(
                ctx, "audio", lay.layout_owner_ref(vmid, i, "audio", aidx), i, sub_index=ai,
                aidx=aidx))
        out.append(("anc", i) + lay._alloc_layout_flow(
            ctx, "anc", lay.layout_owner_ref(vmid, i, "anc"), i))
    return out


decl = {"video": {"w": 1280, "h": 720, "fps": 50, "scan": "p"}, "audio_count": 2, "anc": True}
params = {"tx_count": 4, "active_tx_count": 2, "audio_count": 4}

# ── Case 1: network-scope rule, media interface attached to that network ───────────────────────
net = database.db_add_media_network("Net A", 127)
node = database.db_add_node("n1", "127.0.0.1")
database.db_upsert_node_interface(node, "ens1", role="media2110", media_network_id=net)
database.db_add_mcast_range(scope="network", media_network_id=net, base_ip="239.10.1.0", size=256,
                            port_default_video=2110, port_default_audio=2130, port_default_anc=2150)
res = run_slots(node, "ens1", 101, params, decl, 2)
inside = ipaddress.ip_network("239.10.1.0/24")
check(all(ip and ipaddress.ip_address(ip) in inside for _e, _i, ip, _p in res),
      "every layout flow is inside the network rule's /24: %s" % [(e, ip) for e, _i, ip, _p in res])
ports = {(e, i): p for e, i, _ip, p in res}
check(ports[("video", 0)] == 2110 and ports[("video", 1)] == 2110, "video ports follow the rule base")
check(ports[("audio", 0)] >= 2130, "audio base port follows the rule (%s)" % ports[("audio", 0)])
check(ports[("anc", 1)] == 2150 + 2, "ANC port = rule base + slot*2 (as the hook)")
check(len({(ip, p) for _e, _i, ip, p in res}) == len(res), "no (ip, port) given twice")
owners = {r["owner_ref"] for r in database.get_db().execute("SELECT owner_ref FROM mcast_allocations")}
check(not any(o.endswith(":layout") for o in owners) and "tx:101:1:audio:2:leg0" in owners,
      "layout rows use the hook's owner refs (':leg0', flat audio index): %s" % sorted(owners))
hook_ifn, hook_net = alloc._egress_iface(node, {"tx_pins": {"0": "ens1"}}, 0, leg=0)
check((hook_ifn, hook_net) == ("ens1", net), "resolution agrees with the hook's _egress_iface")

# ── Case 2: no rule anywhere → global pool, historical behaviour ───────────────────────────────
net2 = database.db_add_media_network("Net B", 128)
node2 = database.db_add_node("n2", "127.0.0.2")
database.db_upsert_node_interface(node2, "ens2", role="media2110", media_network_id=net2)
res2 = run_slots(node2, "ens2", 202, params, decl, 1)
check(all(ip and not ipaddress.ip_address(ip) in inside for _e, _i, ip, _p in res2),
      "no rule: flows come from the global pool (%s)" % [ip for _e, _i, ip, _p in res2])

# ── Declared format drives rule matching (fmt from decl, not the not-yet-applied slot) ─────────
c = lay._layout_alloc_ctx(node, "ens1", 0, decl, {"width": 1920, "height": 1080, "fps": 25}, params, 2)
check(c["fmt_v"] == {"scan": "p", "width": 1280, "height": 720, "fps": 50},
      "fmt comes from the declaration first")

print()
print("%d failure(s)" % len(fails) if fails else "all green")
sys.exit(1 if fails else 0)
