#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""A container the database forgot must neither be created nor collide (2026-11-21).

The tester's « Address already in use »: `destroy_compute` ignored a failed `docker rm` and deleted
the row anyway; `bobi-cmp-103` kept running with 10.17.4.103, and the allocation — which only read
the database — gave that IP to the next container. Held here, offline (throwaway database, host
doubled):
  1. A — the row goes only when the node confirms the container is gone; still there → row,
     cores kept, an alert naming it; node unreachable → deleted as before, with a warning;
  2. B — an IP Docker already gives out on the node is skipped, and an occupant unknown to the
     database is named in an alert; the container's own IP stays sticky on redeploy.

    ./venv/bin/python tests/verif_destruction_orphelin_ip.py
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402
_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_orphelin_ip_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import allocations, core_pool, docker_compute, node_driver           # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


def alertes(vmid, cle):
    return [a for a in database.db_get_alerts(vmid=vmid) if cle in (a.get("msg_key") or "")]


hote = {"inspect": "absent", "rc": 0}


def faux_ssh_run(host, cmd, timeout=None, **kw):
    if cmd.startswith("docker inspect") and "echo present" in cmd:
        return (hote["rc"], "" if hote["rc"] else hote["inspect"] + "\n", "")
    return (0, "", "")


docker_compute.ssh_run = faux_ssh_run
node_driver.host_exec = lambda *a, **k: (0, "", "")
core_pool.core_map_cached = lambda nid: {}
core_pool.numa_map_cached = lambda nid: {}
released = []
core_pool.release_cores = lambda vmid: released.append(vmid)

n = database.db_add_node("n1", "192.0.2.9", kind="docker")
database.db_update_node(n, container_net_mode="interne")

print("\n1. A — the row goes only when the node confirms")
database.db_upsert_container_docker(103, "c103", n, "bobi-cmp-103")
hote.update(inspect="present", rc=0)
verifie(docker_compute.destroy_compute(103) is False, "still on the node → destroy returns False")
verifie(database.db_get_container(103) is not None, "the row is kept")
verifie(103 not in released, "its cores are kept (it still runs on them)")
verifie(bool(alertes(103, "destruction_refusee")), "an alert names it")

hote.update(inspect="absent", rc=0)
verifie(docker_compute.destroy_compute(103) is True and database.db_get_container(103) is None,
        "gone on the node → row deleted")
verifie(not alertes(103, "destruction_non_verifiee"), "no warning when the node confirmed")

database.db_upsert_container_docker(104, "c104", n, "bobi-cmp-104")
hote.update(rc=255)
verifie(docker_compute.destroy_compute(104) is True and database.db_get_container(104) is None,
        "node unreachable → deleted as before (a dismantled node must stay cleanable)")
verifie(bool(alertes(104, "destruction_non_verifiee")), "… with a warning that it was not checked")

print("\n2. B — the node's own view of its IPs")
database.db_upsert_container_docker(112, "c112", n, "bobi-cmp-112")
lo = allocations.node_ip_bounds(n)[0]
orphelin = {lo: "bobi-cmp-103"}
ip = allocations.allocate_container_ip(112, node_id=n, held_on_node=orphelin)
verifie(ip is not None and ip != lo, "an IP held on the node is skipped", (ip, lo))
al = alertes(112, "ip_tenue_hors_base")
verifie(bool(al) and "bobi-cmp-103" in (al[0].get("msg_params") or ""),
        "the unknown occupant is named", al[:1])
database.db_update_docker_ip(112, ip)

verifie(allocations.allocate_container_ip(112, node_id=n, held_on_node=orphelin) == ip,
        "sticky IP kept on redeploy")
database.db_upsert_container_docker(113, "c113", n, "bobi-cmp-113")
avant = len(alertes(113, "ip_tenue_hors_base"))
ip3 = allocations.allocate_container_ip(113, node_id=n, held_on_node={ip: "bobi-cmp-112"})
verifie(ip3 not in (ip, None), "an IP held by a known container is skipped too", ip3)
verifie(len(alertes(113, "ip_tenue_hors_base")) == avant, "… without an orphan alert")

print("\n3. the deploy passes the node's view, minus the container itself")
src = open(os.path.join(RACINE, "app", "docker_compute.py"), encoding="utf-8").read()
verifie("held_on_node={ip: nm for ip, nm in ips_held_on_node(node, network).items() if nm != name}"
        in src, "creer_container_compute feeds allocate_container_ip from the node")
from app import container_net                                                  # noqa: E402
container_net.inspect_reseau = lambda node, name: {"Containers": {
    "a": {"Name": "bobi-cmp-103", "IPv4Address": "10.17.4.103/24"},
    "b": {"Name": "bobi-cmp-112", "IPv4Address": "10.17.4.112/24"}}}
verifie(docker_compute.ips_held_on_node({"name": "n1"}, "net") ==
        {"10.17.4.103": "bobi-cmp-103", "10.17.4.112": "bobi-cmp-112"},
        "ips_held_on_node reads Docker's IPv4Address (mask stripped)")

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
