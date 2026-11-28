#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""What follows a media port when its PMD changes (app/pmd_switch.py) — the two defects seen on
dl-360-1 on 2026-11-23:

  1. `pmd=dpdk` stored on an interface without a PCI address: the engine received the interface
     name and stopped on `mtl_init fail`. The address is now read on the node, or the mode refused;
  2. the node's single-interface ptp4l left running on a port bound to vfio-pci (« No such
     device » in a loop). It is stopped at the bind and started again at the unbind — and only the
     one WE stopped.

No database, no node: settings, alerts, probe and ptp4l are doubles.

    ./venv/bin/python tests/verif_pmd_switch.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, database                                                # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_pmd_"), "t.db")
database._tls.__dict__.pop("conn", None)
from app import pmd_switch as ps, mtl                                           # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{str(detail)[:300]}]"))
    if not cond:
        ko += 1


NODE = {"id": 7, "name": "node-a", "host": "192.0.2.10"}
IFN = "ens1f0np0"

print("1. PCI address of a DPDK port")
calls = []


def probe_ok():
    calls.append(1)
    return True, None, [{"name": "eno1", "pci": "0000:02:00.0"}, {"name": IFN, "pci": "0000:10:00.0"}]


verifie(ps.resolve_dpdk_pci(IFN, " 0000:aa:00.1 ", probe_ok) == ("0000:aa:00.1", None) and not calls,
        "a declared address is kept, the node is not probed")
verifie(ps.resolve_dpdk_pci(IFN, None, probe_ok) == ("0000:10:00.0", None),
        "★ nothing declared: the address is read on the node")
verifie(ps.resolve_dpdk_pci("ens9", "", probe_ok) == (None, "not_found"),
        "★ interface unknown on the node: refused (not_found)")
verifie(ps.resolve_dpdk_pci(IFN, "", lambda: (True, None, [{"name": IFN, "pci": None}])) == (None, "not_found"),
        "a netdev without a PCI device: refused")
verifie(ps.resolve_dpdk_pci(IFN, "", lambda: (False, "timeout", [])) == (None, "unreachable"),
        "node not probed: refused (unreachable)")


def probe_raises():
    raise OSError("boom")


verifie(ps.resolve_dpdk_pci(IFN, "", probe_raises) == (None, "unreachable"),
        "a probe that raises is a refusal, not an exception")


class World:
    """Node settings, alerts and ptp4l of one node."""

    def __init__(self, settings, groups=()):
        self.s = dict(settings)
        self.groups = list(groups)
        self.stopped, self.deployed, self.started, self.alerts = [], [], [], []
        self.deploy_ok = True

    def kw(self):
        return dict(setting_for=lambda k, nid=None: self.s.get(k),
                    set_setting=lambda nid, k, v: self.s.__setitem__(k, v),
                    del_setting=lambda nid, k: self.s.pop(k, None),
                    groups=lambda nid: self.groups,
                    alert=lambda key, niveau="info", **kw: self.alerts.append((key, niveau, kw)))

    def park(self, ifn=IFN):
        return ps.park_ptp4l(NODE, ifn, NODE["host"], stop=lambda h: self.stopped.append(h), **self.kw())

    def restore(self, ifn=IFN):
        def deploy(host, ifname, domain, hw_ts, **opts):
            self.deployed.append((host, ifname, domain, hw_ts, opts))
            return (True, "ok") if self.deploy_ok else (False, "no such device")
        return ps.restore_ptp4l(NODE, ifn, NODE["host"], deploy=deploy,
                                start=lambda h: (self.started.append(h), (True, "ok"))[1], **self.kw())


MONO = {"ptp_enabled": True, "ptp_ifname": IFN, "ptp_domain": 127, "ptp_hw_ts": True,
        "ptp_priority1": 200, "ptp_client_only": True}

print("\n2. bind to vfio-pci: the single-interface ptp4l is stopped")
w = World(MONO)
verifie(w.park() is True and w.stopped == [NODE["host"]], "★ ptp4l on the bound interface is stopped")
verifie(w.s["ptp_enabled"] is False and w.s.get(ps.PARK_KEY) == IFN,
        "★ the setting says stopped, and the interface is remembered", w.s)
verifie([a[0] for a in w.alerts] == ["alert.ptp.ptp4l_arrete_dpdk"] and w.alerts[0][2].get("node_id") == 7,
        "the stop is written to the journal", w.alerts)
verifie(w.park() is False and len(w.stopped) == 1, "a second bind (already bound) stops nothing more")

w = World(dict(MONO, ptp_ifname="eno4"))
verifie(w.park() is False and not w.stopped and ps.PARK_KEY not in w.s,
        "★ ptp4l on ANOTHER interface is left alone")
w = World(dict(MONO, ptp_enabled=False))
verifie(w.park() is False and not w.stopped and ps.PARK_KEY not in w.s,
        "ptp4l switched off by the operator: nothing stopped, nothing remembered")
w = World(MONO, groups=[{"network_id": 1}])
verifie(w.park() is False and not w.stopped, "per-network ptp4l in charge: not ours")

print("\n3. back on the kernel driver: the ptp4l we stopped is started again")
w = World(MONO)
w.park()
verifie(w.restore() is True and w.started == [NODE["host"]], "★ ptp4l is started again")
d = w.deployed[0] if w.deployed else (None,) * 5
verifie(d[1] == IFN and d[2] == 127 and d[3] is True and d[4].get("priority1") == 200
        and d[4].get("client_only") is True, "with the node's stored profile", d)
verifie(w.s["ptp_enabled"] is True and ps.PARK_KEY not in w.s, "★ setting back on, mark dropped", w.s)
verifie(w.alerts[-1][0] == "alert.ptp.ptp4l_relance", "the restart is written to the journal")

w = World(dict(MONO, ptp_enabled=False))
verifie(w.restore() is False and not w.started and w.s["ptp_enabled"] is False,
        "★ ptp4l switched off by the operator (no mark) is NOT started")
w = World(MONO)
w.park()
verifie(w.restore("ens1f1np1") is False and not w.started and w.s.get(ps.PARK_KEY) == IFN,
        "another port coming back does not restart it, the mark stays")
w = World(MONO)
w.park()
w.s["ptp_ifname"] = "eno4"
w.s["ptp_enabled"] = True
verifie(w.restore() is False and not w.deployed and ps.PARK_KEY not in w.s,
        "moved to another interface by hand meanwhile: mark dropped, nothing redeployed")
w = World(MONO)
w.park()
w.deploy_ok = False
verifie(w.restore() is False and w.s["ptp_enabled"] is False and not w.started
        and w.alerts[-1][:2] == ("alert.ptp.ptp4l_non_relance", "warning"),
        "★ a restart that fails is said (warning) and the setting stays off", w.alerts[-1:])

print("\n4. wiring: the bind and the unbind call it")
seen = []
orig = (mtl._vfio_executer, mtl._iface_pour_bdf, ps.park_ptp4l, ps.restore_ptp4l)
try:
    mtl._vfio_executer = lambda plan, node, bdf, run=None, timeout=60: (True, "ok", [])
    mtl._iface_pour_bdf = lambda node, bdf: ({"ifname": IFN, "pci": bdf}, [])
    ps.park_ptp4l = lambda node, ifn, host, **kw: seen.append(("park", ifn, host))
    ps.restore_ptp4l = lambda node, ifn, host, **kw: seen.append(("restore", ifn, host))
    mtl.vfio_bind_apply(NODE, "0000:10:00.0")
    mtl.vfio_unbind_apply(NODE, "0000:10:00.0")
    verifie(seen == [("park", IFN, NODE["host"]), ("restore", IFN, NODE["host"])],
            "★ bind → stop, unbind → restart", seen)
    seen.clear()
    mtl.vfio_bind_apply(NODE, "0000:10:00.0", run=lambda *a, **k: (0, "@@OK", ""))
    verifie(not seen, "an injected runner (dry run, test) touches no ptp4l", seen)
    seen.clear()
    mtl._vfio_executer = lambda plan, node, bdf, run=None, timeout=60: (False, "échec vfio", [])
    mtl.vfio_bind_apply(NODE, "0000:10:00.0")
    verifie(not seen, "★ a bind that FAILED leaves ptp4l running", seen)
finally:
    mtl._vfio_executer, mtl._iface_pour_bdf, ps.park_ptp4l, ps.restore_ptp4l = orig

src = open(os.path.join(ROOT, "app", "routes", "node_network.py"), encoding="utf-8").read()
i_guard, i_upsert = src.find("resolve_dpdk_pci("), src.find("db_upsert_node_interface(\n        node_id, ifname, clear=clear")
verifie(0 < i_guard < i_upsert, "★ the interface route resolves the address BEFORE storing the row",
        (i_guard, i_upsert))
i_store = src.find('d["pci"] = _pci', max(i_guard, 0))
verifie(i_guard < i_store < i_upsert, "★ and the address read on the node is the one stored",
        (i_guard, i_store, i_upsert))
verifie('"error_code": "dpdk_without_pci"' in src, "and refuses with a code when it cannot")

print("\n✓ all green" if not ko else "\n✗ %d failure(s)" % ko)
sys.exit(1 if ko else 0)
