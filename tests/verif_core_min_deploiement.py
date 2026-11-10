#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""`requires.core_min` beyond installation: deploy, activation, service load (2026-11-09).

Installation already refused a component needing a newer core. A core ROLLBACK restores
`plugins/` and `services/` from its backup but deletes nothing added since, so a component (or an
archived version a container is pinned to) can need a newer core than the one running. Decided
with Cyril: a version NAMED by a human is refused; with no version named (auto-restart,
rewiring, project recall) the newest compatible one is used and the alert says so; none
compatible → refused. « Unknown » (script-only archive) never refuses and is never chosen.

    ./venv/bin/python tests/verif_core_min_deploiement.py
"""
import json
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_core_min_deploy_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import deploy, plugins, core_plugins                                 # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


alertes = []
deploy.db_add_alert = lambda key, niveau="info", **kw: alertes.append((key, (kw.get("params") or {}).get("to")))

# A plugin « p »: current 3.0, archives 2.0 and 1.0 (newest first, as plugins.versions() gives).
VERDICT = {}
_vrais = (plugins.is_plugin, plugins.get, plugins.versions, plugins.version_compatible)
plugins.is_plugin = lambda t: t == "p"
plugins.get = lambda t: {"version": "3.0"}
plugins.versions = lambda t: ["3.0", "2.0", "1.0"]
plugins.version_compatible = lambda t, v: ("9.9", VERDICT[v])


def choix(version, explicit, **verdicts):
    VERDICT.clear(); VERDICT.update({k.replace("v", "").replace("_", "."): x for k, x in verdicts.items()})
    del alertes[:]
    return deploy.version_for_this_core(7, "p", version, explicit, "h")


print("\n1. version_for_this_core")
r = choix(None, False, v3_0=True, v2_0=True, v1_0=True)
verifie(r is None and not alertes, "compatible current → unchanged, silent", (r, alertes))
r = choix("2.0", False, v3_0=True, v2_0=None, v1_0=True)
verifie(r == "2.0" and not alertes, "unknown verdict → unchanged (never refuses on a guess)", (r, alertes))
r = choix("2.0", True, v3_0=True, v2_0=False, v1_0=True)
verifie(r is False and alertes == [("alert.deploy.core_too_old", None)],
        "version NAMED by a human, too new → refused, never swapped", (r, alertes))
r = choix("2.0", False, v3_0=True, v2_0=False, v1_0=True)
verifie(r is None and alertes == [("alert.deploy.core_fallback", "3.0")],
        "pinned 2.0 too new, current fits → current, and the alert names it", (r, alertes))
r = choix(None, False, v3_0=False, v2_0=None, v1_0=True)
verifie(r == "1.0" and alertes == [("alert.deploy.core_fallback", "1.0")],
        "current too new → the newest COMPATIBLE archive, skipping an unknown one", (r, alertes))
r = choix(None, False, v3_0=False, v2_0=None, v1_0=False)
verifie(r is False and alertes == [("alert.deploy.core_too_old", None)],
        "nothing compatible (unknown is not compatible) → refused", (r, alertes))
plugins.is_plugin = lambda t: False
r = deploy.version_for_this_core(7, "svc", "x", True)
verifie(r == "x", "not a plugin → untouched", r)
plugins.is_plugin, plugins.get, plugins.versions, plugins.version_compatible = _vrais

print("\n2. the deploy path honours it, BEFORE any container work")
appels = []
_vrai_vfc, _vrai_get = deploy.version_for_this_core, deploy.db_get_container
deploy.version_for_this_core = lambda vmid, t, v, explicit, h=None: appels.append((v, explicit)) or False
deploy.db_get_container = lambda vmid: None
deploy.get_container_ip = lambda vmid: (_ for _ in ()).throw(AssertionError("went on after a refusal"))
try:
    r1 = deploy._deployer_script_locked(7, "p", {}, version="2.0")
    r2 = deploy._deployer_script_locked(7, "p", {"plugin_version": ""})
finally:
    deploy.version_for_this_core, deploy.db_get_container = _vrai_vfc, _vrai_get
verifie(r1 is False and r2 is False, "refused → the deploy returns False and stops", (r1, r2))
verifie([e for _v, e in appels] == [True, False], "a version passed by the caller is EXPLICIT, none is not", appels)

print("\n3. activating an archived version that needs a newer core is refused")
tmp = tempfile.mkdtemp(prefix="verif_core_min_activ_")
os.makedirs(os.path.join(tmp, "versions", "1.0"))
with open(os.path.join(tmp, "versions", "1.0", "plugin.json"), "w") as f:
    json.dump({"type": "zz", "version": "1.0", "requires": {"core_min": "99.0"}}, f)
with open(os.path.join(tmp, "versions", "1.0", "script.py"), "w") as f:
    f.write("# 1.0\n")
with open(os.path.join(tmp, "script.py"), "w") as f:
    f.write("# 2.0\n")
plugins.REGISTRY["zz"] = {"type": "zz", "version": "2.0", "_dir": tmp}
try:
    plugins.activate_version("zz", "1.0")
    verifie(False, "plugin: refused")
except ValueError as e:
    verifie("99.0" in str(e), "plugin: refused, naming the core it needs", str(e))
finally:
    plugins.REGISTRY.pop("zz", None)
verifie(open(os.path.join(tmp, "script.py")).read() == "# 2.0\n", "… and nothing moved on disk")

os.rename(os.path.join(tmp, "versions", "1.0", "plugin.json"), os.path.join(tmp, "versions", "1.0", "manifest.json"))
_vrai_entry = core_plugins._entry
core_plugins._entry = lambda sid: {"dir": tmp, "manifest": {"id": "zz", "version": "2.0"}}
try:
    core_plugins.activate_version("zz", "1.0")
    verifie(False, "service: refused")
except ValueError as e:
    verifie("99.0" in str(e), "service: refused, naming the core it needs", str(e))
finally:
    core_plugins._entry = _vrai_entry

print("\n4. a service needing a newer core is SAID at load, once")
salertes = []
_vrai_add = database.db_add_alert
database.db_add_alert = lambda key, niveau="info", **kw: salertes.append(key)
try:
    core_plugins._core_too_old_signalled.discard("zz")
    core_plugins._signal_if_core_too_old("zz", {"id": "zz", "version": "1.0", "requires": {"core_min": "99.0"}})
    core_plugins._signal_if_core_too_old("zz", {"id": "zz", "version": "1.0", "requires": {"core_min": "99.0"}})
    verifie(salertes == ["alert.service.core_too_old"], "one alert, not one per rescan", salertes)
    del salertes[:]
    for man in ({"id": "ok", "requires": {"core_min": "0.1"}}, {"id": "nc", "requires": {"core_min": "main"}},
                {"id": "nd"}):
        core_plugins._signal_if_core_too_old(man["id"], man)
    verifie(not salertes, "compatible, unreadable or undeclared → silent", salertes)
finally:
    database.db_add_alert = _vrai_add
scan_src = open(os.path.join(RACINE, "app", "core_plugins.py"), encoding="utf-8").read()
verifie(scan_src.index("_signal_if_core_too_old(name, manifest)") < scan_src.index('importlib.import_module(f"services.{name}")'),
        "scan() checks before importing the service")

print("\n5. messages")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", "%s.json" % lang), encoding="utf-8"))
    for k, champs in (("alert.deploy.core_too_old", {"h", "vmid", "t", "ver", "cmin", "core"}),
                      ("alert.deploy.core_fallback", {"h", "vmid", "t", "ver", "cmin", "core", "to"}),
                      ("alert.service.core_too_old", {"id", "ver", "cmin", "core"})):
        vus = set(re.findall(r"\{(\w+)\}", cat.get(k, "")))
        verifie(vus == champs, "%s %s: placeholders match the parameters sent" % (lang, k), vus)

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
