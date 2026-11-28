#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The STANDBY controller's disk must be judged by the active one.

The standby runs no sampler and an alert written into its own database is seen by nobody, so it
reports its disk in its answer to each replica push and the active controller runs the node
thresholds on it. Checked on both ends:
  - standby side: `/api/ha/replicate` returns the disk report on success AND on refusal, and a
    full disk is named as such (507), not as a generic error;
  - active side: `ha.push_replica` turns the report into warning / error / cleared alerts, worded
    for the standby, with a NULL node_id; an answer without report (older standby) is ignored."""
import sys; sys.path.insert(0, '/opt/bobistudio')
# ── Isolation de la base : un test n'OUVRE JAMAIS la production, même en lecture ──────
# Base jetable, posée AVANT le premier accès (cf. tests/verif_alerte_domaine_mxl.py).
import os as _os_iso                                                        # noqa: E402
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_TMP = _tempfile_iso.mkdtemp(prefix="verif-iso-")
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = _os_iso.path.join(_TMP, "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)
_dbm_iso.init_db()

import errno                                                                # noqa: E402
import json                                                                 # noqa: E402
from flask import Flask                                                     # noqa: E402
from app import ha, ha_tls, node_health as nh, i18n                         # noqa: E402
from app.routes import ha as routes_ha                                      # noqa: E402

fails = []

# ── Standby side: the receiver answers with its disk ────────────────────────────────────
routes_ha._pair_mtls = lambda: True
flask_app = Flask(__name__)
nh.platform.node = lambda: "standby-test-host"


def call_replicate(store):
    ha.store_replica = store
    with flask_app.test_request_context("/api/ha/replicate", method="POST", data=b"SQLite"):
        rv = routes_ha.ha_replicate()
    resp, code = rv if isinstance(rv, tuple) else (rv, rv.status_code)
    return code, resp.get_json()


code, j = call_replicate(lambda raw, source="?": {"bytes": len(raw), "at": "now"})
d = (j or {}).get("disk") or {}
if code != 200 or not j.get("ok") or d.get("name") != "standby-test-host" \
        or not isinstance((d.get("disks") or {}).get("root", {}).get("pct"), (int, float)):
    fails.append("success answer without a usable disk report: %r %r" % (code, j))


def full(raw, source="?"):
    raise OSError(errno.ENOSPC, "No space left on device")


code, j = call_replicate(full)
if code != 507 or (j or {}).get("code") != "no_space" or not (j or {}).get("disk"):
    fails.append("ENOSPC refusal without code no_space, or without disk report: %r %r" % (code, j))

# One entry per filesystem: a database on the root partition is NOT a second disk (VM test,
# 2026-11-28: the alert named "/opt/bobistudio" while / was full).
nh.DB_PATH = "/etc/principale.db"            # same filesystem as /
rep = nh.local_disk_report()["disks"]
if set(rep) != {"root"}:
    fails.append("database on the root filesystem listed twice: %r" % sorted(rep))
# A directory on a filesystem of its own: the temp dir is one here, not on a CI runner, where
# /tmp sits on the root partition (public CI, 2026-11-28) — /dev/shm is a tmpfs on both.
_own_fs = next((d for d in (_os_iso.path.dirname(_cfg_iso.DB_PATH), "/dev/shm")
                if _os_iso.path.isdir(d) and _os_iso.stat(d).st_dev != _os_iso.stat("/").st_dev), None)
if _own_fs is None:
    print("  skip: no filesystem other than / on this machine (database-on-its-own-fs check)")
else:
    nh.DB_PATH = _os_iso.path.join(_own_fs, "principale.db")
    rep = nh.local_disk_report()["disks"]
    if set(rep) != {"root", "data"}:
        fails.append("database on its own filesystem not listed: %r" % sorted(rep))
    nh.DB_PATH = _cfg_iso.DB_PATH

# ── Active side: push_replica judges the report ─────────────────────────────────────────
alerts = []
nh.db_add_alert = lambda key, level="info", **k: alerts.append((key, level, k))
ha.settings.get = lambda k, d=None: "https://standby.invalid" if k == "ha_standby_url" else d
ha.push_lv2 = lambda: ""
answer = {}
ha_tls.requete = lambda *a, **k: (answer["code"], json.dumps(answer["body"]).encode())


def push(code, pct, ok=True, with_disk=True):
    body = {"ok": ok}
    if with_disk:
        body["disk"] = {"name": "standby-test-host",
                        "disks": {"root": {"path": "/", "used": 1, "total": 2, "pct": pct}}}
    answer.update(code=code, body=body)
    alerts.clear()
    ha.push_replica()
    return list(alerts)


def check(name, got, want_key, want_level):
    if want_key is None:
        if got:
            fails.append("%s: expected no alert, got %r" % (name, got))
        return
    if len(got) != 1 or got[0][0] != want_key or got[0][1] != want_level:
        fails.append("%s: expected one %s/%s, got %r" % (name, want_key, want_level, got))
        return
    key, level, k = got[0]
    if k.get("kind") != "disk" or _dbm_iso._alert_int(k.get("node_id")) is not None:
        fails.append("%s: kind=%r node_id=%r" % (name, k.get("kind"), k.get("node_id")))
    params = k.get("params") or {}
    for lang in ("fr", "en"):
        txt = i18n.t(key, lang, **params)
        if txt == key or "standby-test-host" not in txt:
            fails.append("%s [%s]: rendered text %r" % (name, lang, txt))


check("70 %", push(200, 70.0), None, None)
check("92 %", push(200, 92.0), "alert.disk.saturation_standby", "warning")
check("older standby, no report", push(200, 0, with_disk=False), None, None)
check("refused for lack of space, 99 %", push(507, 99.0, ok=False),
      "alert.disk.saturation_standby", "error")
check("70 %, cleared", push(200, 70.0), "alert.disk.retabli_standby", "info")

# ── Replication loop: a coded refusal reaches the alert as a TRANSLATED reason ─────────
class _OneTick(Exception):
    pass


loop_alerts = []
_dbm_iso.db_add_alert = lambda key, level="info", **k: loop_alerts.append((key, k))
ha.time.sleep = lambda s: (_ for _ in ()).throw(_OneTick())
ha._last_fail_sig = None
answer.update(code=507, body={"ok": False, "code": "no_space",
                              "error": "no space left on the standby controller's disk"})
try:
    ha._replication_loop()
except _OneTick:
    pass
rep = [a for a in loop_alerts if a[0] == "alert.node.ha_replication_echec"]
if len(rep) != 1:
    fails.append("loop: expected one replication failure alert, got %r" % loop_alerts)
else:
    params = rep[0][1].get("params") or {}
    row = {"msg_key": rep[0][0], "msg_params": json.dumps(params)}
    fr = i18n.rendre_alerte(row, "fr")["message"]
    en = i18n.rendre_alerte(row, "en")["message"]
    if "plus de place sur le disque du contrôleur de secours" not in fr or "no space" in fr:
        fails.append("loop [fr]: %r" % fr)
    if "no space left on the standby controller's disk" not in en:
        fails.append("loop [en]: %r" % en)
if ha.replication_status()["last_push"].get("msg") != \
        "plus de place sur le disque du contrôleur de secours":
    fails.append("UI status keeps the raw English reason: %r" % ha.replication_status()["last_push"])

if fails:
    print("FAIL verif_alerte_disque_secours")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("OK verif_alerte_disque_secours : receiver (success, ENOSPC), one entry per filesystem, "
      "5 active-side checks, translated refusal reason")
