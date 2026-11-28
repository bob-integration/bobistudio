#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Image registry on a HA pair, the standby side — defects seen on VMs on 2026-11-28, offline:
throwaway databases, no Zot, no network, no systemd (commands are recorded, never run).
  1. a standby reads the registry settings of the ACTIVE controller (its staged replica), not its
     own frozen database (its write API answers 409);
  2. reconcile: standby + shared storage → Zot STOPPED (the demoted one held the NFS boltdb lock),
     disabled → stopped, unchanged → nothing; both boot branches and each replica call it;
  3. apply says « started » only once Zot answers, and fails when it never does;
  4. every local address and the VIP go in the controller certificate (the standby's sync reaches
     the active one by the pairing address, and Zot checks it);
  5. a standby's health is checked on ITS registry, not through the VIP (the active's);
  6. promotion keeps the machine-local settings (peer URL, VIP interface) and they reach the
     promoted database file — the thread's connection pointed at the replaced one;
  7. demotion stops the registry before the restart (NFS lock) and sets aside the replica staged
     before this controller's own active period (stale: settings and data of another day).

    ./venv/bin/python tests/verif_registre_ha_secours.py
"""
import inspect
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="verif_registre_ha_")
from app import config, database                                              # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(TMP, "live.db")
database._tls.__dict__.pop("conn", None)
database.init_db()
from app import ha, registry, settings, ca, vip, backup, updater               # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{str(detail)[:300]}]"))
    if not cond:
        ko += 1


def db_with(path, values):
    """A database file holding the settings table with `values`."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    for k, v in values.items():
        con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, json.dumps(v)))
    con.commit()
    con.close()


def read_file_setting(path, key):
    con = sqlite3.connect(path)
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        con.close()


ha.STAGING_DIR = os.path.join(TMP, "ha_staging")
ha.STAGING_DB = os.path.join(ha.STAGING_DIR, "db_replica.db")
ha.STAGING_META = os.path.join(ha.STAGING_DIR, "ha_replica.json")
os.makedirs(ha.STAGING_DIR)
ha.snapshot_db(ha.STAGING_DB)          # a full schema, like a real replica (WAL: never copyfile)
registry.UNIT_PATH = os.path.join(TMP, "bobi-registry.service")
registry.HOME = os.path.join(TMP, "registry-home")
cmds = []
registry._run = lambda cmd, timeout=60: (cmds.append(cmd) or (0, ""))

# Own (frozen) settings of this machine: registry off. The active's replica: on, on NFS.
settings.set("registry_enabled", False)
settings.set("ha_standby_url", "http://198.51.100.1:5000")
db_with(ha.STAGING_DB, {"registry_enabled": True, "registry_storage_kind": "nfs",
                        "registry_storage_path": "/mnt/reg", "registry_port": 5600,
                        "control_role": "active", "ha_standby_url": "http://198.51.100.2:5000",
                        "vip_interface": "eth9"})

print("1. a standby follows the ACTIVE controller's registry settings")
settings.set("control_role", "standby")
v = registry.settings_values()
verifie(v["enabled"] and v["kind"] == "nfs" and v["path"] == "/mnt/reg" and v["port"] == 5600,
        "standby: settings read from the staged replica", v)
settings.set("control_role", "active")
verifie(not registry.settings_values()["enabled"], "active: its own settings", registry.settings_values())
settings.set("control_role", "standby")
os.replace(ha.STAGING_DB, ha.STAGING_DB + ".away")
verifie(not registry.settings_values()["enabled"], "standby without a replica: its own settings")
os.replace(ha.STAGING_DB + ".away", ha.STAGING_DB)

print("\n2. reconcile: what runs on each side")
cmds.clear()
ok, msg = registry.reconcile(force=True)
verifie(ok and ["systemctl", "disable", "--now", registry.UNIT_NAME] in cmds
        and not any("restart" in c for c in cmds),
        "standby on shared storage (NFS): Zot stopped, never started", (msg, cmds))
cmds.clear()
verifie(registry.reconcile() == (None, "unchanged") and not cmds, "nothing changed: nothing done", cmds)
settings.set("control_role", "active")
open(registry.UNIT_PATH, "w").close()
cmds.clear()
ok, msg = registry.reconcile()
verifie(ok and ["systemctl", "disable", "--now", registry.UNIT_NAME] in cmds,
        "registry disabled while a unit exists: stopped (role changed → reconciled again)", (msg, cmds))
src_boot = open(os.path.join(ROOT, "main.py")).read()
std = src_boot[src_boot.index('log.warning("Contrôleur en STANDBY'):src_boot.index("# VIP de management (keepalived)")]
verifie("_reg.reconcile(" in std and "threading.Thread(target=_boot_registry_standby" in std,
        "boot, STANDBY branch: the registry is reconciled (it was only in the active branch)")
act = src_boot[src_boot.index("def _boot_registry():"):src_boot.index('name="registry-boot").start()')]
verifie("_reg.reconcile(force=True)" in act and "_reg.apply()" not in act,
        "boot, ACTIVE branch: reconcile (stops a disabled registry too)")
from app.routes import ha as ha_routes                                          # noqa: E402
body = inspect.getsource(ha_routes.ha_replicate)
verifie("registry.reconcile" in body[body.index("store_replica"):],
        "each replica received by a standby reconciles its registry")

print("\n3. « started » only once Zot answers")
settings.set("registry_enabled", True)
settings.set("registry_storage_kind", "system")
settings.set("registry_storage_path", TMP)
ca.ca_available = lambda: True
ca.controller_client_files = lambda: ("c.crt", "c.key", "ca.crt")
ca._detect_control_ip = lambda: "192.0.2.11"
registry.install_binary = lambda source=None, timeout=600: (True, "present")
registry._ensure_certificate_addresses = lambda: None
registry._write = lambda path, data, mode=0o644: None
answers = []
seen_urls = []


def fake_health(timeout=5, url=None):
    seen_urls.append(url)
    return (answers.pop(0) if answers else False), "HTTP 000"


registry.health = fake_health
registry.time.sleep = lambda s: None
answers[:] = [False, False, True]
ok, msg = registry.apply()
verifie(ok and msg == "started" and len(seen_urls) == 3, "Zot answers on the 3rd try: started", (msg, seen_urls))
verifie(bool(seen_urls) and seen_urls[0] == "https://192.0.2.11:%d" % registry.settings_values()["port"],
        "the wait checks THIS controller's registry", seen_urls[:1])
registry.READY_TIMEOUT_S = 0
answers[:] = []
ok, msg = registry.apply()
verifie(ok is False and "not answering" in msg, "Zot never answers: apply fails, says so", msg)

print("\n4. the controller certificate carries every local address and the VIP")
import importlib                                                                # noqa: E402
registry_fresh = importlib.reload(registry)
got = {}
parse_local_ipv4 = registry_fresh._local_ipv4
registry_fresh._local_ipv4 = lambda: ["192.0.2.31", "10.0.2.15"]
vip.ip_certificat = lambda: "192.0.2.30"
ca.assurer_certificat_controleur = lambda ips=(), ident=None, renouveler=False: got.setdefault("ips", list(ips)) and True
reloaded = []
vip.recharger_ecoutes = lambda: reloaded.append(1)
registry_fresh._ensure_certificate_addresses()
verifie(set(got.get("ips") or []) == {"192.0.2.31", "10.0.2.15", "192.0.2.30"},
        "pairing address, default-route address and VIP requested", got)
verifie(reloaded == [1], "re-issued → listeners reload the certificate", reloaded)
registry_fresh._run = lambda cmd, timeout=60: (0, "2: ens3    inet 10.0.2.15/24 brd 10.0.2.255 scope global ens3\n"
                                                  "3: ens4    inet 192.0.2.31/24 brd 192.0.2.255 scope global ens4\n")
verifie(parse_local_ipv4() == ["10.0.2.15", "192.0.2.31"], "`ip -o addr` parsed", parse_local_ipv4())

print("\n5. a standby's health is its own registry, never the VIP")
registry = registry_fresh
registry.UNIT_PATH = os.path.join(TMP, "bobi-registry.service")
open(registry.UNIT_PATH, "w").close()
registry._run = lambda cmd, timeout=60: (0, "active")
seen_urls.clear()
registry.health = fake_health
settings.set("vip_address", "192.0.2.30/24")
settings.set("control_role", "standby")
registry.status()
verifie(seen_urls and seen_urls[-1] == registry.own_url() and "192.0.2.30" not in seen_urls[-1],
        "standby: status() checks own_url()", seen_urls)
settings.set("control_role", "active")
seen_urls.clear()
registry.status()
verifie(seen_urls == [None], "active: status() checks the address the nodes use", seen_urls)

print("\n6. promotion keeps this machine's settings, in the promoted file")
settings.set("control_role", "standby")
settings.set("ha_standby_url", "http://198.51.100.1:5000")     # this standby's peer = the active
settings.set("vip_interface", "ens4")
backup.BACKUP_DIR = os.path.join(TMP, "backups")
updater.restart_service = lambda *a, **k: None
vip.refresh_for_role = lambda: None
ok, msg = ha.promote()
verifie(ok, "promote() succeeds", msg)
path = database.DB_PATH
verifie(read_file_setting(path, "ha_standby_url") == "http://198.51.100.1:5000",
        "peer URL = the former active, not the replica's (this very controller)",
        read_file_setting(path, "ha_standby_url"))
verifie(read_file_setting(path, "vip_interface") == "ens4", "VIP interface of THIS machine kept",
        read_file_setting(path, "vip_interface"))
verifie(read_file_setting(path, "control_role") == "active" and read_file_setting(path, "registry_storage_kind") == "nfs",
        "role active and the replica's cluster settings, in the file", read_file_setting(path, "control_role"))

print("\n7. demotion: the registry stops first, the stale replica is set aside")
stopped = []
registry.stop = lambda: (stopped.append(1), (True, "stopped"))[1]
verifie(os.path.exists(ha.STAGING_DB), "a replica is staged before the demotion")
ok, msg = ha.demote()
verifie(ok and stopped == [1], "demote() stops the registry before the restart", (msg, stopped))
verifie(not os.path.exists(ha.STAGING_DB) and os.path.exists(ha.STAGING_DB + ".before-demote"),
        "the replica staged before this controller's active period is set aside, not deleted")
verifie(ha.promote()[0] is False, "and cannot be promoted back by mistake", ha.promote())
verifie("TimeoutStopSec=15" in registry.render_unit(), "the unit gives Zot 15 s to stop, not 90")

print("\n✓ all green" if not ko else "\n✗ %d failure(s)" % ko)
sys.exit(1 if ko else 0)
