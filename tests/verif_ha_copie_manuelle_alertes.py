#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""A manual replica push ("replicate now") must alert like the background loop does.

Two defects found by a VM test of the replication alerts:
  - the alerts raised while serving /api/ha/replicate-now (standby disk, replication failure)
    were stored with the logged-in admin as actor, although they are MACHINE alerts;
  - a failed manual push raised nothing: the alert only came with the next loop tick.
Now both paths go through `ha.push_and_alert` (one anti-spam signature) and every alert of a
push is written under `database.machine_actor()` (user NULL, anti-bounce kept)."""
import sys; sys.path.insert(0, '/opt/bobistudio')
# ── Isolation de la base : un test n'OUVRE JAMAIS la production, même en lecture ──────
import os as _os_iso                                                        # noqa: E402
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_TMP = _tempfile_iso.mkdtemp(prefix="verif-iso-")
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = _os_iso.path.join(_TMP, "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)
_dbm_iso.init_db()

import json                                                                 # noqa: E402
from flask import Flask                                                     # noqa: E402
from app import ha, ha_tls, auth, node_health as nh, i18n                   # noqa: E402
from app.routes import ha as routes_ha                                      # noqa: E402

fails = []
flask_app = Flask(__name__)
ha.settings.get = lambda k, d=None: "https://standby.invalid" if k == "ha_standby_url" else d
ha.push_lv2 = lambda: ""
nh.platform.node = lambda: "standby-test-host"
answer = {}
ha_tls.requete = lambda *a, **k: (answer["code"], json.dumps(answer["body"]).encode())
# The route reads the actor through auth.current_user (cf. database._acteur_courant).
auth.current_user = lambda: {"id": 1, "username": "admin"}
view = getattr(routes_ha.ha_replicate_now, "__wrapped__", routes_ha.ha_replicate_now)


def set_answer(code, ok, pct, no_space=False):
    body = {"ok": ok, "disk": {"name": "standby-test-host",
                               "disks": {"root": {"path": "/", "used": 1, "total": 2, "pct": pct}}}}
    if no_space:
        body.update(code="no_space", error="no space left on the standby controller's disk")
    answer.update(code=code, body=body)


def rows():
    with _dbm_iso.get_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM alerts ORDER BY id")]


def clear():
    with _dbm_iso.get_db() as db:
        db.execute("DELETE FROM alerts")
        db.execute("DELETE FROM alert_episodes")


def manual():
    with flask_app.test_request_context("/api/ha/replicate-now", method="POST"):
        rv = view()
    return rv[1] if isinstance(rv, tuple) else rv.status_code


class _OneTick(Exception):
    pass


def loop_tick():
    ha.time.sleep = lambda s: (_ for _ in ()).throw(_OneTick())
    try:
        ha._replication_loop()
    except _OneTick:
        pass


def echecs():
    return [r for r in rows() if r["msg_key"] == "alert.node.ha_replication_echec"]


# ── 1. Manual push refused for lack of space ────────────────────────────────────────────
ha._last_fail_sig = None
clear()
set_answer(507, False, 99.0, no_space=True)
if manual() != 400:
    fails.append("manual failed push should answer 400")
ech = echecs()
if len(ech) != 1:
    fails.append("manual failure: expected exactly one ha_replication_echec, got %d" % len(ech))
else:
    if ech[0]["user"] is not None:
        fails.append("replication failure alert carries user=%r" % ech[0]["user"])
    row = {"msg_key": ech[0]["msg_key"], "msg_params": ech[0]["msg_params"]}
    fr = i18n.rendre_alerte(row, "fr")["message"]
    en = i18n.rendre_alerte(row, "en")["message"]
    if "plus de place sur le disque du contrôleur de secours" not in fr or "no space" in fr:
        fails.append("manual failure [fr]: %r" % fr)
    if "no space left on the standby controller's disk" not in en:
        fails.append("manual failure [en]: %r" % en)

# ── 2. The standby disk alert raised during that manual push ─────────────────────────────
disk = [r for r in rows() if (r["msg_key"] or "").startswith("alert.disk.")]
if len(disk) != 1:
    fails.append("expected one standby disk alert, got %d" % len(disk))
elif disk[0]["user"] is not None:
    fails.append("standby disk alert carries user=%r" % disk[0]["user"])

# ── 3. Anti-spam shared by both paths; success clears; new failure alerts again ─────────
_dbm_iso.db_set_setting("alerts_antirebond_s", 0)       # isolate OUR signature from the bounce
before = len(echecs())
manual()
loop_tick()
if len(echecs()) != before:
    fails.append("identical failure re-alerted by manual/loop after a manual alert")
set_answer(200, True, 50.0)
if manual() != 200:
    fails.append("manual success should answer 200")
set_answer(507, False, 50.0, no_space=True)
loop_tick()
if len(echecs()) != before + 1:
    fails.append("failure after a success must alert again (loop)")
loop_tick()
manual()
if len(echecs()) != before + 1:
    fails.append("loop alert then identical manual failure re-alerted")
set_answer(200, True, 50.0)
loop_tick()
set_answer(507, False, 50.0, no_space=True)
manual()
if len(echecs()) != before + 2:
    fails.append("failure after a loop success must alert again (manual)")

# ── 4. The loop alerts as before, machine actor, from a request-less thread ─────────────
ha._last_fail_sig = None
clear()
set_answer(507, False, 50.0, no_space=True)
loop_tick()
ech = echecs()
if len(ech) != 1 or ech[0]["user"] is not None:
    fails.append("loop: expected one NULL-user failure alert, got %r" % ech)

# ── 5. Anti-bounce kept for machine alerts raised inside a request ──────────────────────
_dbm_iso.db_set_setting("alerts_antirebond_s", 300)
clear()
with flask_app.test_request_context("/x", method="POST"):
    with _dbm_iso.machine_actor():
        _dbm_iso.db_add_alert("alert.node.ha_replication_echec", "warning", kind="node",
                              params={"url": "u", "msg": "m"})
        _dbm_iso.db_add_alert("alert.node.ha_replication_echec", "warning", kind="node",
                              params={"url": "u", "msg": "m"})
    _dbm_iso.db_add_alert("alert.node.ha_replication_echec", "warning", kind="node",
                          params={"url": "u", "msg": "other"}, user=None)
rs = echecs()
if len(rs) != 2 or rs[0]["user"] is not None or rs[1]["user"] != "admin":
    fails.append("machine_actor must keep the anti-bounce and leave other alerts derived: %r"
                 % [(r["user"], r["msg_params"]) for r in rs])

if fails:
    print("FAIL verif_ha_copie_manuelle_alertes")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("OK verif_ha_copie_manuelle_alertes : manual push alerts once (machine actor, fr/en), "
      "disk alert machine, shared anti-spam, loop unchanged, anti-bounce kept")
