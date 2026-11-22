#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""A node capability that needs a plugin gets it from the catalogue (2026-11-22).

A tester ticked io2110 at install and found no image for the 2110 probe: `bobi-mtl` is built from
the `2110_io` plugin, which only the catalogue installs, and nothing said so. Held here, offline
(catalogue, base and HA doubled):
  1. an io2110 node without 2110_io → the plugin is installed through the SAME path as the
     catalogue's « Install » button, once, and an alert says so;
  2. nothing to do → nothing done: plugin present, no io2110 node, standby controller;
  3. a catalogue without the plugin, or a release missing → a warning alert, never an exception;
  4. the three triggers are wired: end of enrolment, all-in-one installer, boot catch-up.

    ./venv/bin/python tests/verif_plugin_autoinstall.py
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import database as d                                    # noqa: E402
d.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")             # never the production base
from app import ha, plugins, catalogue                           # noqa: E402
from app.routes import catalogue_api                             # noqa: E402
from app import plugin_autoinstall as pa                         # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


alertes, installs, telecharges = [], [], []
etat = {"installe": set(), "actif": True,
        "entrees": [{"genre": "plugin", "type": "2110_io", "depot": "bobistudio-plugin-2110_io",
                     "tag": "v0.108.5", "installable": True}]}
d.db_add_alert = lambda msg, niveau="info", **k: alertes.append((msg, niveau, k.get("params")))
ha.is_active = lambda: etat["actif"]
plugins.is_plugin = lambda t: t in etat["installe"]
catalogue.lister = lambda force=False: {"entrees": etat["entrees"]}
catalogue.telecharger = lambda depot, tag=None: telecharges.append((depot, tag)) or b"zip"


def _archive(brut, tmp, activer):
    installs.append(activer)
    etat["installe"].add("2110_io")
    return {"status": "installe", "version": "0.108.5"}, 200


catalogue_api.install_plugin_archive = _archive
N2110 = {"id": 1, "name": "noeud-2110", "capabilities": '["compute","io2110"]'}
NCALC = {"id": 2, "name": "noeud-calc", "capabilities": '["compute"]'}


def reset(**k):
    alertes.clear(); installs.clear(); telecharges.clear()
    etat["installe"] = set(k.get("installe", ())); etat["actif"] = k.get("actif", True)


print("\n1. io2110 without 2110_io: installed through the catalogue's own path")
reset()
r = pa.ensure("test", nodes=[N2110, NCALC])
verifie(r == {"2110_io": (True, "0.108.5")}, "the plugin is installed", r)
verifie(installs == [True] and telecharges == [("bobistudio-plugin-2110_io", "v0.108.5")],
        "via install_plugin_archive (the « Install » button's path), at the release tag, activated",
        (installs, telecharges))
verifie([a[:2] for a in alertes] == [("alert.plugin_autoinstall.ok", "info")]
        and "noeud-2110" in alertes[0][2]["nodes"], "an alert names the plugin and the node", alertes)
reset()
pa.ensure("test", nodes=[N2110, dict(N2110, id=3, name="noeud-2110-b")])
verifie(len(installs) == 1 and "noeud-2110-b" in alertes[0][2]["nodes"], "two io2110 nodes: one install", installs)

print("\n2. nothing to do, nothing done")
for quoi, k, nodes in (("plugin already there", {"installe": {"2110_io"}}, [N2110]),
                       ("no io2110 node", {}, [NCALC]),
                       ("standby controller", {"actif": False}, [N2110])):
    reset(**k)
    r = pa.ensure("test", nodes=nodes)
    verifie(r == {} and not installs and not alertes, quoi, (r, installs, alertes))

print("\n3. failures are alerts, not exceptions")
reset(); sauve = etat["entrees"]
etat["entrees"] = []
r = pa.ensure("test", nodes=[N2110])
verifie(r["2110_io"][0] is False and alertes[0][1] == "warning", "not in the catalogue: warning", (r, alertes))
etat["entrees"] = [dict(sauve[0], installable=False, indisponible="aucune release publiée")]
reset(); r = pa.ensure("test", nodes=[N2110])
verifie(r["2110_io"] == (False, "aucune release publiée") and not telecharges, "no release: warning, nothing downloaded", r)
etat["entrees"] = sauve
catalogue.telecharger = lambda depot, tag=None: (_ for _ in ()).throw(OSError("réseau coupé"))
reset(); r = pa.ensure("test", nodes=[N2110])
verifie(r["2110_io"][0] is False and "réseau coupé" in r["2110_io"][1], "download fails: reported, no raise", r)

catalogue_api.install_plugin_archive = lambda brut, tmp, activer: ({"error": "manifeste invalide"}, 400)
catalogue.telecharger = lambda depot, tag=None: b"zip"
reset(); r = pa.ensure("test", nodes=[N2110])
verifie(r["2110_io"] == (False, "manifeste invalide") and alertes[0][1] == "warning",
        "archive refused: reported as a failure, not as installed", r)

print("\n4. the three triggers")
src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()             # noqa: E731
enr = src("app/routes/enrollment.py")
fin = enr[enr.find("def _enroll_finalize"):]
verifie(fin.find('plugin_autoinstall.ensure("enroll")') > fin.find("node_driver.register("),
        "end of enrolment, after the node is registered")
verifie('_pa.ensure("boot")' in src("main.py"), "boot catch-up")
inst = src("install/install.py")
aio = inst[inst.find("def path_all_in_one"):]
verifie(aio.find("installer_plugins_requis()") > aio.find("register_local_agent(token)") > 0,
        "all-in-one installer, once its local node exists")
api = src("app/routes/catalogue_api.py")
route = api[api.find("def catalogue_installer"):]
route = route[:route.find("\n@bp.route")]
verifie("install_plugin_archive(brut, tmp, activer)" in route and not re.search(r"(?<!core_)plugins\.install_package\(", route),
        "the « Install » button goes through the same function")

print("\n✓ all green" if not ko else "\n✗ %d failure(s)" % ko)
sys.exit(1 if ko else 0)
