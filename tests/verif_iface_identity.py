#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Garde de `app/iface_identity.reconcile` — identité stable des interfaces déclarées.

Aucune base, aucun nœud : la sonde, l'exécution distante et la base sont remplacées par des
doublures. Chaque cas reproduit une situation réelle ou plausible :

  1. la carte RDMA de dell-1 renommée par l'ajout d'une Quad 2 (2026-10-20) → elle reprend son nom,
     sa configuration est réappliquée, le nom est épinglé ;
  2. déclaration d'avant le module (MAC vide), même BDF → MAC complétée et nom épinglé ;
  3. MAC vide mais BDF différent → on NE grave PAS la MAC d'une autre carte, alerte ;
  4. le nom est porté par une autre carte → rien n'est touché, alerte ;
  5. carte introuvable → alerte, interface marquée absente (le RDMA s'en sert pour ne pas boucler) ;
  6. port DPDK (vfio-pci) → ignoré.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import iface_identity as ii  # noqa: E402

MAC_A = "aa:bb:cc:00:00:01"
MAC_B = "aa:bb:cc:00:00:02"


class World:
    def __init__(self, rows, nics, links=None):
        self.rows = rows
        self.nics = nics
        self.links = dict(links or {})
        self.cmds = []
        self.alerts = []
        self.upserts = []
        self.ips = []

    def host_exec(self, node, cmd, timeout=0, input_data=None):
        self.cmds.append(cmd)
        if cmd.startswith("ip link set dev") and " name " in cmd:
            parts = cmd.split()
            i = parts.index("name")
            cur, new = parts[i - 1], parts[i + 1]
            self.nics[new] = self.nics.pop(cur)
        if "PermanentMACAddress=" in cmd and "printf" in cmd:
            name = cmd.split("Name=")[1].split("\\n")[0].split("'")[0].strip()
            mac = cmd.split("PermanentMACAddress=")[1].split("\n")[0].split("\\n")[0].strip()
            self.links["10-bobi-%s.link" % name] = mac
        return (0, "WROTE", "")


def run(world):
    ii._missing.clear()
    ii._alerted.clear()
    ii.probe = lambda node: ({k: dict(v) for k, v in world.nics.items()}, dict(world.links))
    ii.db_get_node_interfaces = lambda nid: [dict(r) for r in world.rows]
    ii.db_upsert_node_interface = lambda nid, ifname, **f: world.upserts.append((ifname, f))
    ii.db_add_alert = lambda key, level, **kw: world.alerts.append((key, level, kw.get("params")))
    from app import node_driver, docker_driver
    node_driver.host_exec = world.host_exec
    docker_driver.ensure_iface_ip = lambda node, ifn, cidr, persist=False: (
        world.ips.append((ifn, cidr, persist)) or (True, cidr))
    ii.reconcile({"id": 35, "name": "dell-1", "host": "h", "agent_url": "x"})


fails = []


def check(cond, what):
    print(("OK   " if cond else "FAIL ") + what)
    if not cond:
        fails.append(what)


# 1. Carte renommée par un ajout matériel.
w = World(rows=[{"ifname": "enp132s0", "mac": MAC_A, "pci": "0000:84:00.0", "role": "rdma",
                 "ip_cidr": "192.168.1.2/24"}],
          nics={"enp136s0": {"mac": MAC_A, "pci": "0000:88:00.0"}})
run(w)
check("enp132s0" in w.nics and "enp136s0" not in w.nics, "1. la carte reprend son nom déclaré")
check(w.ips == [("enp132s0", "192.168.1.2/24", True)], "1. IP RDMA réappliquée et gravée")
check(w.links.get("10-bobi-enp132s0.link") == MAC_A, "1. nom épinglé sur la MAC")
check(all("flock /run/bobi-initramfs.lock update-initramfs" in c for c in w.cmds if "update-initramfs" in c)
      and any("update-initramfs" in c for c in w.cmds),
      "1. régénération de l'initramfs sérialisée (trois en parallèle ont cassé celui de dl360-1)")
check([a[0] for a in w.alerts] == ["alert.net.iface_renommee"], "1. une alerte qui dit ce qui a été fait")
check(not ii.is_missing(35, "enp132s0"), "1. interface plus marquée absente")

# 2. Backfill : MAC vide, même BDF.
w = World(rows=[{"ifname": "eno5np0", "mac": None, "pci": "0000:b2:00.0", "role": "rdma"}],
          nics={"eno5np0": {"mac": MAC_A, "pci": "0000:b2:00.0"}})
run(w)
check(("eno5np0", {"mac": MAC_A}) in w.upserts, "2. MAC complétée en base")
check(w.links.get("10-bobi-eno5np0.link") == MAC_A, "2. nom épinglé")
check(not w.alerts, "2. aucune alerte")

# 3. MAC vide, BDF différent : ne pas graver la MAC d'une autre carte.
w = World(rows=[{"ifname": "enp132s0", "mac": None, "pci": "0000:84:00.0", "role": "rdma"}],
          nics={"enp132s0": {"mac": MAC_B, "pci": "0000:86:00.0"}})
run(w)
check(not w.upserts and not w.links, "3. rien n'est gravé")
check([a[0] for a in w.alerts] == ["alert.net.iface_pci_change"], "3. alerte changement de BDF")
check(ii.is_missing(35, "enp132s0") == "pci", "3. interface marquée douteuse")

# 4. Le nom est porté par une autre carte.
w = World(rows=[{"ifname": "enp132s0", "mac": MAC_A, "pci": "0000:84:00.0", "role": "rdma"}],
          nics={"enp132s0": {"mac": MAC_B, "pci": "0000:84:00.0"}})
run(w)
check(not any(" name " in c for c in w.cmds), "4. aucun renommage")
check([a[0] for a in w.alerts] == ["alert.net.iface_autre_carte"], "4. alerte autre carte")

# 5. Carte introuvable ; l'alerte n'est émise qu'une fois (transition).
w = World(rows=[{"ifname": "enp132s0", "mac": MAC_A, "pci": "0000:84:00.0", "role": "rdma"}],
          nics={"eno3": {"mac": MAC_B, "pci": "0000:01:00.0"}})
run(w)
check(ii.is_missing(35, "enp132s0") == "absent", "5. interface marquée absente")
check([a[0] for a in w.alerts] == ["alert.net.iface_absente"], "5. alerte absente")
ii.reconcile({"id": 35, "name": "dell-1", "host": "h", "agent_url": "x"})
check(len(w.alerts) == 1, "5. pas de seconde alerte au passage suivant")
w.nics["enp132s0"] = {"mac": MAC_A, "pci": "0000:84:00.0"}
ii.reconcile({"id": 35, "name": "dell-1", "host": "h", "agent_url": "x"})
check(w.alerts[-1][0] == "alert.net.iface_retrouvee" and not ii.is_missing(35, "enp132s0"),
      "5. retour signalé, plus marquée absente")

# 6. Port DPDK : ignoré.
w = World(rows=[{"ifname": "ens1f0np0", "mac": None, "pci": "0000:11:00.0", "role": "media2110",
                 "pmd": "dpdk"}], nics={})
run(w)
check(not w.alerts and not w.upserts and not ii.is_missing(35, "ens1f0np0"), "6. port DPDK ignoré")

print("\n%d échec(s)" % len(fails))
sys.exit(1 if fails else 0)
