#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""« No Blackmagic device » only when the listing says so (2026-11-23).

On dell-1, a card whose driver stayed stuck made the 20 s listing of /dev/blackmagic time out, and
the deployment said « no Blackmagic device » while io0..io3 were there — the wrong lead — and the
exception reached no screen. Held here on the REAL `deploy_compute` (host doubled):
  1. an empty listing: refused, alert `decklink_aucun_peripherique`;
  2. a listing that could not be read: refused, alert `decklink_liste_illisible` with the cause,
     never « no device »;
  3. devices listed: they are passed to the container.

MUTATION, DL_MUTATION=confond — must give rc=1 (both causes told the same).

    ./venv/bin/python tests/verif_decklink_liste_illisible.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_dl_liste_"), "t.db")
config.DB_PATH = database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import docker_compute                                                # noqa: E402

if os.environ.get("DL_MUTATION") == "confond":
    _src = open(docker_compute.__file__, encoding="utf-8").read()
    _old = "            if _listed:\n"
    if _src.count(_old) != 1:
        print("mutation: pattern stale")
        sys.exit(1)
    exec(compile(_src.replace(_old, "            if True:\n"), docker_compute.__file__, "exec"),
         docker_compute.__dict__)
    print("MUTATION active: confond")

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


from app import core_pool, node_driver, placement, plugins, allocations       # noqa: E402
from app import deploy as _deploy                                            # noqa: E402

LS = {"rc": 0, "out": ""}
specs = {}


def host_exec(node, cmd, timeout=None, **kw):
    if "/dev/blackmagic" in cmd:
        return (LS["rc"], LS["out"], "timed out" if LS["rc"] else "")
    return (0, "", "")


class _Rep:
    status_code = 200


class _Session:
    def get(self, *a, **k):
        return _Rep()


node_driver.host_exec = host_exec
node_driver.has_agent = lambda n: True
node_driver.run_container = lambda node, spec: (specs.__setitem__(spec.get("name"), spec) or (True, {"ip": "10.9.9.9"}))
docker_compute._cert_mtls_conteneur = lambda vmid: None
_deploy.token_a_injecter = lambda vmid: None
_deploy.agent_session = lambda: _Session()
allocations.centralized_ipam = lambda *a, **k: False
core_pool.core_map_cached = lambda nid: {}
core_pool.numa_map_cached = lambda nid: {}
core_pool.numa_of_gpu = lambda *a, **k: None
core_pool.numa_of_media_nic = lambda *a, **k: None
core_pool.numa_of_pci_vendor = lambda *a, **k: None
core_pool.ordonnancables = lambda nid, cpuset: core_pool.parse_cpuset(cpuset)
core_pool.reapply_shared_after_reservation = lambda *a, **k: None
placement.releve_cache = lambda node_id, ttl=None, force=False: None
plugins.needs_decklink = lambda t: True

NA = database.db_add_node("dl-x", "127.0.0.1")
database.db_update_node(NA, agent_url="http://127.0.0.1:9100", compute_image="bobi-compute:99.0",
                        media_image="bobi-media:99.0", docker_network="banc-net")
T = next(m["type"] for m in plugins.all() if plugins.image_variant(m["type"]) in ("compute", "media")
         and not plugins.needs_dpdk(m["type"]) and not (m.get("resources") or {}).get("gpu")
         and not (m.get("requires") or {}).get("image_min") and not (m.get("resources") or {}).get("pin"))
said = []
docker_compute.db_add_alert = lambda key, *a, **k: said.append((key, k.get("params")))


def deploie(vmid):
    database.db_upsert_container_docker(vmid, "c-%d" % vmid, NA, "bobi-%d" % vmid)
    database.db_update_deploy_config(vmid, T, {})
    said.clear()
    try:
        return docker_compute.deploy_compute(vmid, params={}, deploy_type=T, force=True), None
    except RuntimeError as e:
        return None, str(e)


print("\n1. Empty listing")
LS.update(rc=0, out="")
ok, err = deploie(801)
verifie(err and "no Blackmagic device" in err and said and said[0][0] == "alert.deploy.decklink_aucun_peripherique",
        "refused, « no device », alerted", (err, said))
print("\n2. Listing not readable")
LS.update(rc=124, out="")
ok, err = deploie(802)
verifie(err and "could not be listed" in err and "no Blackmagic device" not in err,
        "★ refused, NOT « no device »", err)
verifie(said and said[0][0] == "alert.deploy.decklink_liste_illisible" and "timed out" in said[0][1]["e"],
        "★ alerted with its cause", said)
print("\n3. Devices listed")
LS.update(rc=0, out="io0\nio1\n")
ok, err = deploie(803)
verifie(err is None and specs.get("bobi-803", {}).get("devices") == ["/dev/blackmagic/io0", "/dev/blackmagic/io1"],
        "passed to the container", (err, specs.get("bobi-803", {}).get("devices")))
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    verifie(cat.get("alert.deploy.decklink_aucun_peripherique") and "{e}" in (cat.get("alert.deploy.decklink_liste_illisible") or ""),
            "%s: labels" % lang)

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
