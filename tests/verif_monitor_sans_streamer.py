#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The monitor says what this instance lacks BEFORE creating anything (2026-11-15).

On an instance without the `streamer` plugin, opening the monitor created a container, failed
with « Type de script inconnu : streamer » in the alerts only, restarted it three times, and
the panel said « Déploiement de l'encodeur échoué ». Held here, offline (throwaway database):
  1. streamer absent → `unavailable()` says so; the user monitor AND the dedicated preview
     refuse with that message and never create a container;
  2. streamer present, gateway missing → the code tells « service absent » from « not deployed »;
  3. everything there → None, and creation is attempted;
  4. the status the panel reads carries the code, and the panel and the workspace use it.

    ./venv/bin/python tests/verif_monitor_sans_streamer.py
"""
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import database                                                      # noqa: E402
_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_mon_streamer_"), "t.db")
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import monitor as mon, core_plugins                                  # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


vrais = (mon._plugins.is_plugin, mon.gateway_ready, core_plugins.is_service,
         mon._create_monitor_container)
crees = []


def etat(streamer, passerelle, service):
    mon._plugins.is_plugin = lambda t: streamer if t == "streamer" else vrais[0](t)
    mon.gateway_ready = lambda: passerelle
    core_plugins.is_service = lambda sid: service if sid == "webrtc_gateway" else vrais[2](sid)


def stop_creation(*a, **k):
    crees.append(a)
    raise RuntimeError("stop: creation reached")


mon._create_monitor_container = stop_creation
try:
    print("\n1. streamer absent")
    etat(False, True, True)
    why = mon.unavailable()
    verifie(why and why[0] == "no_streamer" and "streamer" in why[1], "unavailable() names the plugin", why)
    lignes = list(mon.create_iter(1))
    verifie(lignes[-1].startswith("❌") and "streamer" in lignes[-1], "user monitor refuses, naming streamer",
            lignes[-1])
    lignes = list(mon.create_dedicated_iter(999, shm="x"))
    verifie(lignes and lignes[-1].startswith("❌") and "streamer" in lignes[-1],
            "dedicated preview refuses, naming streamer", lignes[-1:])
    verifie(not crees, "no container created", crees)
    verifie(mon.status(1).get("unavailable") == "no_streamer", "status carries the code")

    print("\n2. gateway missing")
    etat(True, False, False)
    verifie((mon.unavailable() or ("",))[0] == "no_gateway_service", "service absent → no_gateway_service")
    etat(True, False, True)
    verifie((mon.unavailable() or ("",))[0] == "no_gateway", "service present, not deployed → no_gateway")

    print("\n3. everything there")
    etat(True, True, True)
    verifie(mon.unavailable() is None, "unavailable() is None")
    list(mon.create_iter(1))
    verifie(len(crees) == 1, "creation is attempted", len(crees))
finally:
    (mon._plugins.is_plugin, mon.gateway_ready, core_plugins.is_service,
     mon._create_monitor_container) = vrais

print("\n4. the UI uses the code")
lay = open(os.path.join(RACINE, "templates", "layout.html"), encoding="utf-8").read()
wsp = open(os.path.join(RACINE, "templates", "workspace.html"), encoding="utf-8").read()
verifie("'js.monitor.unavailable_' + s.unavailable" in lay, "monitor panel shows the reason")
verifie("s.unavailable === 'no_streamer' ? 'projects.fn_video_nostreamer'" in wsp
        and "(s.unavailable || s.gateway_ready === false ? '' :" in wsp,
        "workspace preview says it and hides its create button")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(RACINE, "i18n", lang + ".json"), encoding="utf-8"))
    manque = [k for k in ("js.monitor.unavailable_no_streamer", "js.monitor.unavailable_no_gateway_service",
                          "projects.fn_video_nostreamer") if not cat.get(k)]
    verifie(not manque, f"{lang}: keys present", manque)

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
