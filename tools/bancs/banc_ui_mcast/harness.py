#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Offline harness for the multicast plan editor (Settings -> Multicast).

Renders the REAL `templates/settings.html` through the Flask test client against a throwaway
database (created before any import that opens the DB), then serves it, plus `/static/`, from a
tiny HTTP server on 127.0.0.1. The plan API is answered by the REAL `app.mcast_plans` functions
against that same throwaway database. Other `/api/` GETs (other tabs of the page) are answered
by the real app on the throwaway DB so they get the right shape; anything it cannot answer, and
every other write, gets a harmless empty 200.

Nothing touches `db_bobistudio.db` or the orchestrator on :5000. Invented addresses only.

Standalone use (opens a server you can point any browser at):
    ./venv/bin/python tools/bancs/banc_ui_mcast/harness.py
"""
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

# ⚠ BEFORE any access: one SQLite connection is kept PER THREAD (cf. CLAUDE.md).
from app import config, database  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="banc_ui_mcast_")
config.DB_PATH = database.DB_PATH = os.path.join(_TMP, "bench.db")
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import mcast_plans as mp  # noqa: E402
from app.database import (db_add_media_network, db_add_node, db_upsert_node_interface,  # noqa: E402
                          db_update_media_network, db_upsert_container_docker,
                          db_update_deploy_config, db_delete_mcast_plan, db_get_mcast_plans)

RULE_OCT2 = {"mode": "octet", "octet": 2, "delta": 100}
SEED = {}


def seed():
    """Three media networks, a paired node, an unpaired node, one 2110_io engine, one saved plan."""
    with database.get_db() as db:
        db.execute("INSERT INTO users (username, password_hash, role) VALUES ('bench', 'x', 'admin')")
    n1 = db_add_media_network("Media net 1", 127)                 # no saved rule, red first
    n2 = db_add_media_network("Media net 2", 126)                 # saved rule, blue first
    db_update_media_network(n2, first_fabric="blue", default_b_rule=RULE_OCT2)
    n3 = db_add_media_network("Media net 3", 125)                 # holds the pre-existing plan
    db_update_media_network(n3, default_b_rule={"mode": "same"})
    pair = db_add_node("node-pair", "127.0.0.1", mtl_iface="ensR")
    db_upsert_node_interface(pair, "ensR", role="media2110", media_network_id=n1, pair_group=1,
                             pair_role="red")
    db_upsert_node_interface(pair, "ensB", role="media2110", media_network_id=n1, pair_group=1,
                             pair_role="blue")
    single = db_add_node("node-single", "127.0.0.1", mtl_iface="ens3")
    db_upsert_node_interface(single, "ens3", role="media2110", media_network_id=n2)
    # One engine, 8 TX slots PROVISIONED, 3 ACTIVE (`active_tx_count`): video + 2 audios + ANC each.
    # Slot 0 already has addresses (ok/move), slot 1 is typed by hand (manual), slot 2 has nothing
    # yet (new). Slots 3-7 are provisioned only; 3 and 4 already carry addresses, as on a real
    # engine where every provisioned slot is addressed.
    slots = [
        {"scan": "i", "width": 1920, "height": 1080, "fps": 25,
         "multicast_ip": "239.20.1.1", "dest_port": 5000, "multicast_ip_leg1": "239.120.1.1",
         "anc_multicast_ip": "239.20.9.1", "anc_dest_port": 5008,
         "audios": [{"multicast_ip": "239.20.5.1", "dest_port": 5004},
                    {"multicast_ip": "239.20.5.2", "dest_port": 5006}]},
        {"scan": "i", "width": 1920, "height": 1080, "fps": 25,
         "multicast_ip": "239.20.7.7", "multicast_ip_manual": True, "dest_port": 5000,
         "audios": [{}, {}]},
        {"scan": "p", "width": 1920, "height": 1080, "fps": 50, "audios": [{}, {}]},
    ]
    for k in range(3, 8):
        s = {"scan": "i", "width": 1920, "height": 1080, "fps": 25, "audios": [{}, {}]}
        if k < 5:
            s.update(multicast_ip="239.20.1.%d" % (k + 1), dest_port=5000,
                     multicast_ip_leg1="239.120.1.%d" % (k + 1))
        slots.append(s)
    params = {"tx_count": 8, "active_tx_count": 3, "video_count": 0, "audio_count": 16,
              "width": 1920, "height": 1080, "fps": 25, "tx_slots": slots}
    db_upsert_container_docker(301, "io-bench", pair, "io-bench")
    db_update_deploy_config(301, "2110_io", params)
    pid, err = mp.save_plan({"scope": "network", "media_network_id": n3, "label": "Existing plan",
                             "b_rule": {"mode": "same"}, "rank_mode": "counter", "port_mode": "fixed",
                             "reserve_last": 1,
                             "lines": [{"flow_class": None, "cidr_a": "239.30.0.0/24", "port_video": 2120,
                                        "port_audio": 2130, "port_anc": 2140}]})
    if err:
        raise SystemExit("seed plan refused: %s" % err)
    SEED.update(n1=n1, n2=n2, n3=n3, pair=pair, single=single, plan=pid)


def reset():
    """Back to the seeded state between two runs (other viewport)."""
    for p in db_get_mcast_plans():
        if p["id"] != SEED["plan"]:
            db_delete_mcast_plan(p["id"])
    db_update_media_network(SEED["n1"], default_b_rule=None)
    db_update_media_network(SEED["n2"], first_fabric="blue", default_b_rule=RULE_OCT2)


_FLASK = {}
_FLASK_LOCK = threading.Lock()


def _client():
    """Flask test client with a forged admin session (as tests/verif_rendu_settings.py does)."""
    import main
    app = main.app
    app.config["TESTING"] = True
    cli = app.test_client()
    with database.get_db() as db:
        u = db.execute("SELECT id, username FROM users LIMIT 1").fetchone()
    with cli.session_transaction() as s:
        s["user_id"] = u["id"]
        s["username"] = u["username"]
    return cli


def flask_get(path):
    """Read-only GET answered by the real app on the throwaway DB: (status, body, content type)."""
    with _FLASK_LOCK:
        if "cli" not in _FLASK:
            _FLASK["cli"] = _client()
        r = _FLASK["cli"].get(path)
        return r.status_code, r.data, r.headers.get("Content-Type", "application/json")


def flask_post(path, body):
    """POST answered by the real app on the throwaway DB (the settings save path)."""
    with _FLASK_LOCK:
        if "cli" not in _FLASK:
            _FLASK["cli"] = _client()
        r = _FLASK["cli"].post(path, json=body)
        return r.status_code, r.data, r.headers.get("Content-Type", "application/json")


def render_settings():
    """HTML really served by /settings."""
    status, data, _ct = flask_get("/settings")
    if status != 200:
        raise SystemExit("/settings answered %s" % status)
    return data.decode("utf-8")


STATIC = os.path.join(ROOT, "static")
_CT = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
       ".png": "image/png", ".woff2": "font/woff2", ".woff": "font/woff", ".ico": "image/x-icon",
       ".json": "application/json", ".html": "text/html", ".jpg": "image/jpeg"}


class Handler(BaseHTTPRequestHandler):
    page = ""
    log = []                       # (method, path, status) of every request
    delay_refs = 0.0               # seconds: slows /api/mcast_targets to observe the loading state

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        Handler.log.append((self.command, self.path, code))

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"null")
        except ValueError:
            return None

    def _api(self, method, path):
        if path == "/api/mcast_targets" and method == "GET":
            if Handler.delay_refs:
                import time
                time.sleep(Handler.delay_refs)
            return self._json(200, dict(ok=True, **mp.targets()))
        if path == "/api/flow_classes" and method == "GET":
            return self._json(200, {"ok": True, "classes": mp.flow_classes()})
        if path == "/api/mcast_plans" and method == "GET":
            return self._json(200, {"ok": True, "plans": mp.plans_api()})
        if path == "/api/mcast_plans" and method == "POST":
            pid, err = mp.save_plan(self._body() or {})
            return self._json(400, {"ok": False, "error": err}) if err else self._json(200, {"ok": True, "id": pid})
        if path == "/api/mcast_plans/preview" and method == "POST":
            out, err = mp.preview(self._body() or {})
            return self._json(400, {"ok": False, "error": err}) if err else self._json(200, dict(ok=True, **out))
        m = re.match(r"^/api/mcast_plans/(\d+)$", path)
        if m and method == "PUT":
            pid, err = mp.save_plan(self._body() or {}, int(m.group(1)))
            if err:
                return self._json(404 if err == "plan introuvable" else 400, {"ok": False, "error": err})
            return self._json(200, {"ok": True, "id": pid})
        if m and method == "DELETE":
            db_delete_mcast_plan(int(m.group(1)))
            return self._json(200, {"ok": True})
        if path == "/api/mcast_ranges" and method == "GET":
            return self._json(200, {"ok": True, "ranges": []})
        if path in ("/api/media_networks",) and method == "GET":
            return self._json(200, {"ok": True, "networks": database.db_get_media_networks()})
        if path == "/api/settings" and method == "POST":
            # The real save path, on the throwaway DB: a reload then shows what was really stored.
            status, data, ctype = flask_post(path, self._body() or {})
            return self._send(status, data, ctype)
        if method != "GET":
            self._body()
            return self._json(200, {})
        # Other tabs' GETs: the real app on the throwaway DB gives them the right SHAPE (a canned {}
        # makes unrelated tabs throw, which would hide the editor's own errors). Anything the app
        # cannot answer here (no node, no service) degrades to a harmless empty 200.
        try:
            status, data, ctype = flask_get(self.path)
        except Exception:
            status = 500
        if status != 200:
            return self._json(200, {})
        return self._send(200, data, ctype)

    def _handle(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            try:
                return self._api(self.command, path)
            except Exception as e:                       # surfaced as a failed request
                return self._json(500, {"ok": False, "error": "harness: %s" % e})
        if path in ("/", "/settings"):
            return self._send(200, Handler.page, "text/html; charset=utf-8")
        if path.startswith("/i18n/") and path.endswith(".js"):
            status, data, ctype = flask_get(self.path)
            return self._send(status, data, ctype)
        if path.startswith("/static/"):
            f = os.path.normpath(os.path.join(STATIC, path[len("/static/"):]))
            if f.startswith(STATIC + os.sep) and os.path.isfile(f):
                with open(f, "rb") as fh:
                    return self._send(200, fh.read(),
                                      _CT.get(os.path.splitext(f)[1], "application/octet-stream"))
        return self._send(404, "not found", "text/plain")

    do_GET = do_POST = do_PUT = do_DELETE = _handle


def start():
    """Seed, render and serve. Returns (base_url, server)."""
    seed()
    Handler.page = render_settings()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv


if __name__ == "__main__":
    Handler.delay_refs = float(os.environ.get("BANC_DELAY_REFS") or 0)
    url, srv = start()
    print("serving %s/settings (Ctrl-C to stop); throwaway DB in %s" % (url, _TMP), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
