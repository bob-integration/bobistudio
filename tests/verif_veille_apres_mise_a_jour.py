# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The update banner stops offering what was installed since the last check (2026-11-25).

Seen by the tester: updated to 1.0.4, controller restarted, and the banner kept saying « 1.0.4
available (installed: 1.0.3) », reload after reload — the « ? » menu said 1.0.4. The banner reads
the state of the last CHECK, which runs once a day. `update_watch.reconcile()` re-judges, without
asking GitHub, every item whose installed version changed since that check:
  1. core updated to the offered version → gone;
  2. core updated, a NEWER one is offered → kept, « installed » follows;
  3. nothing changed → kept untouched;
  4. plugin / service updated, still older, uninstalled → gone / kept / gone;
  5. the banner (`notice_for`) shows the reconciled list;
  6. called at boot (`start`) and after a catalogue install (both branches).
    ./venv/bin/python tests/verif_veille_apres_mise_a_jour.py
"""
import inspect
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import config, database                      # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="veille_maj_db_"), "t.db")
database.init_db()
from app import update_watch as uw, version, plugins, catalogue  # noqa: E402

ko = 0


def ok(cond, label, detail=""):
    global ko
    print(("  ok  " if cond else " ÉCHEC ") + label)
    if not cond:
        print("         " + str(detail))
        ko += 1


def core(v, inst):
    return {"key": "core", "kind": "core", "name": "bobistudio", "label": "Bobi.Studio",
            "version": v, "installed": inst, "url": ""}


def comp(kind, name, v, inst):
    return {"key": "%s:%s" % (kind, name), "kind": kind, "name": name, "label": name,
            "version": v, "installed": inst, "url": ""}


def put(items):
    database.db_set_setting(uw.STATE_KEY, {"checked_at": 1.0, "error": None, "available": items,
                                           "notified": {}})


INSTALLED = {"mixer": "0.5.0", "split": "0.3.1"}
plugins.is_plugin = lambda t: t in INSTALLED
plugins.get = lambda t: {"version": INSTALLED[t]} if t in INSTALLED else None
SERVICES = {"tts": "1.2.0"}
catalogue._version_service_installee = lambda n: SERVICES.get(n)

version.VERSION = "1.0.4"
put([core("1.0.4", "1.0.3")])
ok(uw.reconcile() == [] and uw.state()["available"] == [],
   "1. core updated to the offered 1.0.4: the offer is gone, and stored", uw.state())

put([core("1.0.5", "1.0.3")])
k = uw.reconcile()
ok([(i["version"], i["installed"]) for i in k] == [("1.0.5", "1.0.4")],
   "2. core updated, 1.0.5 offered: kept, installed now 1.0.4", k)

put([core("1.0.4", "1.0.4"), comp("plugin", "mixer", "0.6.0", "0.5.0")])
before = uw.state()
ok(uw.reconcile() == before["available"] and uw.state() == before,
   "3. nothing installed since the check: kept untouched", uw.state())

put([comp("plugin", "mixer", "0.5.0", "0.4.9"),          # updated to the offer
     comp("plugin", "split", "0.4.0", "0.3.0"),          # updated, still older
     comp("plugin", "gone", "1.0.0", "0.9.0"),           # uninstalled
     comp("service", "tts", "1.2.0", "1.1.0")])          # service updated
k = uw.reconcile()
ok([(i["key"], i["installed"]) for i in k] == [("plugin:split", "0.3.1")],
   "4. components: updated → gone, still older → kept (installed follows), uninstalled → gone", k)

put([core("1.0.4", "1.0.3")])
uw.reconcile()
n = uw.notice_for(1)
ok(n["items"] == [], "5. the banner shows nothing once 1.0.4 runs", n)

calls = []
real = uw.reconcile
uw.reconcile = lambda: calls.append(1) or []
uw._loop = lambda: None
uw.start()
uw.reconcile = real
ok(calls == [1], "6a. start() (boot of the active controller) reconciles", calls)
from app.routes import catalogue_api                    # noqa: E402
src = inspect.getsource(catalogue_api.catalogue_installer)
ok(src.count("_reconcile_update_notice()") == 2,
   "6b. a catalogue install reconciles, plugin AND service branch", src.count("_reconcile_update_notice()"))

sys.exit(1 if ko else 0)
