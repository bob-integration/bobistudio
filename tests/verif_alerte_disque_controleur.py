#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The controller's own disk must raise the same alerts as a node's disk.

`sample_all` measured the controller's disk and displayed it, but only the node loop compared it
to the thresholds: on 2026-11-28 the controller sat at 92 % with no alert at all. This drives
`sample_all` itself (not `_check_disk_alert` directly, which was never the broken part) with a
fake controller snapshot and checks: warning at 92 %, error at 97 %, cleared at 70 %, alert of
kind `disk`, NULL node_id, naming the controller host, rendered in both languages."""
import sys; sys.path.insert(0, '/opt/bobistudio')
# ── Isolation de la base : un test n'OUVRE JAMAIS la production, même en lecture ──────
# Base jetable, posée AVANT le premier accès : la connexion est mémorisée par thread, donc
# surcharger DB_PATH après coup n'isole plus rien. `PRODUCTION_DB_PATH` n'est PAS touché
# (cf. tests/verif_alerte_domaine_mxl.py).
import os as _os_iso                                                        # noqa: E402
import tempfile as _tempfile_iso                                            # noqa: E402
from app import config as _cfg_iso, database as _dbm_iso                    # noqa: E402
_TMP = _tempfile_iso.mkdtemp(prefix="verif-iso-")
_cfg_iso.DB_PATH = _dbm_iso.DB_PATH = _os_iso.path.join(_TMP, "principale.db")
_dbm_iso._tls.__dict__.pop("conn", None)
_dbm_iso.init_db()

import time                                                                 # noqa: E402
from app import node_health as nh                                           # noqa: E402
from app import i18n                                                        # noqa: E402

nh.STATS_PERSIST_PATH = _os_iso.path.join(_TMP, "node_health_stats.json")
nh._last_flush = time.time()          # no 24 h flush during the test
nh.db_get_nodes = lambda: []          # controller only: no node to sample

HOST = "ctl-test-host"
pct_now = [0.0]


def fake_snapshot():
    p = pct_now[0]
    return {"ts": time.time(), "ok": True, "is_controller": True, "name": HOST,
            "host": "127.0.0.1", "disks": {"root": {"path": "/", "used": int(p * 10),
                                                    "total": 1000, "pct": p}}}


nh._controller_snapshot = fake_snapshot

alerts = []
nh.db_add_alert = lambda key, level="info", **k: alerts.append((key, level, k))


def tick(pct):
    pct_now[0] = pct
    alerts.clear()
    nh.sample_all(force=True)
    return list(alerts)


fails = []


def check(name, got, want_key, want_level):
    if want_key is None:
        if got:
            fails.append("%s: expected no alert, got %r" % (name, got))
        return
    if len(got) != 1 or got[0][0] != want_key or got[0][1] != want_level:
        fails.append("%s: expected one %s/%s, got %r" % (name, want_key, want_level, got))
        return
    key, level, k = got[0]
    if k.get("kind") != "disk":
        fails.append("%s: kind=%r, expected 'disk'" % (name, k.get("kind")))
    # node_id is passed as the controller key and must END UP NULL in the alert row.
    if _dbm_iso._alert_int(k.get("node_id")) is not None:
        fails.append("%s: node_id=%r would be stored as a node" % (name, k.get("node_id")))
    params = k.get("params") or {}
    if params.get("n") != HOST:
        fails.append("%s: alert names %r, expected the controller host" % (name, params.get("n")))
    for lang in ("fr", "en"):
        txt = i18n.t(key, lang, **params)
        if txt == key or HOST not in txt:
            fails.append("%s [%s]: rendered text %r" % (name, lang, txt))


check("70 % (healthy)", tick(70.0), None, None)
check("92 % (warning)", tick(92.0), "alert.disk.saturation_controller", "warning")
check("93 % (same episode)", tick(93.0), None, None)
check("97 % (error)", tick(97.0), "alert.disk.saturation_controller", "error")
check("82 % (inside the clear margin)", tick(82.0), None, None)
check("70 % (cleared)", tick(70.0), "alert.disk.retabli_controller", "info")

if fails:
    print("FAIL verif_alerte_disque_controleur")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("OK verif_alerte_disque_controleur : 6 checks (warning, episode, error, margin, clear)")
