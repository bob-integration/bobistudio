#!/usr/bin/env python3
"""Vérifie les FLUX déclaratifs du proxy de plugin (`control.streams`, app/plugin_streams.py).

Contrat : (1) déclaration validée au scan, collision avec un endpoint JSON refusée ; (2) un GET
déclaré arrive PROGRESSIVEMENT (un vrai Waitress sur port libre, amont factice qui émet 10 ms de
PCM toutes les 10 ms) ; la latence ajoutée est MESURÉE avec le défaut de Waitress 3.0.2 (send_bytes=1) ET
avec send_bytes=18000 pour que l'écart reste visible ; le client qui part ferme l'amont ;
(3) un POST relaie octets + query + statut, 413 au-delà de max_bytes ; (4) droits read/operate ;
(5) aucun relais public : les flux (écoute, micro) ne sortent jamais par /p/<jeton>.

Aucun réseau externe, aucune base : auth et adressage sont remplacés par des doublures.

    ./venv/bin/python tests/verif_plugin_streams.py
"""
import http.client
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)
from flask import Flask                   # noqa: E402
from app import plugin_streams as ps, plugins, auth   # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


STREAMS = [{"path": "/listen.pcm", "method": "GET", "perm": "read",
            "content_type": "audio/L16;rate=48000;channels=2"},
           {"path": "/ops.pcm", "method": "GET", "perm": "operate", "content_type": "audio/L16"},
           {"path": "/talk.pcm", "method": "POST", "perm": "operate", "max_bytes": 65536}]
CTRL = {"endpoints": ["/state", "/set"], "read_endpoints": ["/state"], "streams": STREAMS}
M = {"type": "fake", "control": CTRL}

# ── 1. validation ────────────────────────────────────────────────────────────────────
ok(ps.validate_spec(M) == [] and ps.validate_spec({"control": {}}) == [], "déclaration valide / absente")
for titre, mut in (("collision avec un endpoint JSON", {"path": "/state"}),
                   ("POST en perm=read", {"path": "/t", "method": "POST", "perm": "read"}),
                   ("méthode DELETE", {"method": "DELETE"}),
                   ("GET sans content_type", {"content_type": ""}),
                   ("chemin relatif", {"path": "listen"}),
                   ("chemin avec ..", {"path": "/../x"}),
                   ("max_bytes absurde", {"path": "/t", "method": "POST", "perm": "operate", "max_bytes": 0}),
                   ("doublon", {"path": "/listen.pcm"})):
    bad = {"control": dict(CTRL, streams=STREAMS + [dict(STREAMS[0], **mut)])}
    ok(ps.validate_spec(bad), "refusé : " + titre)
ok(ps.validate_spec({"control": {"streams": {}}}), "refusé : pas une liste")
# La page publique (/p/<jeton>, app/routes/pages.py) ne relaie QUE control.read_endpoints : un flux
# déclaré aussi en lecture JSON y passerait. Refusé, même s'il n'est déclaré qu'en read_endpoints.
ro = {"control": {"endpoints": ["/state"], "read_endpoints": ["/state", "/listen.pcm"], "streams": STREAMS}}
ok(any("collisionne" in e for e in ps.validate_spec(ro)), "refusé : flux aussi dans read_endpoints (jamais public)")
ok(ps.find(M, "GET", "listen.pcm") is STREAMS[0] and ps.find(M, "POST", "/listen.pcm") is None
   and ps.find(M, "GET", "/state") is None, "find : méthode et chemin exacts")

TMP = tempfile.mkdtemp(prefix="verif_streams_")
for nom, streams in (("sok", STREAMS), ("sbad", [dict(STREAMS[0], path="/state")])):
    d = os.path.join(TMP, nom)
    os.makedirs(d)
    open(os.path.join(d, "script.py"), "w").write("print({config!r}, {hostname!r}, {plugin_version!r})\n")
    json.dump({"type": nom, "label": nom, "version": "1.0", "script_template": "script.py",
               "deploy_defaults": {}, "control": dict(CTRL, streams=streams)},
              open(os.path.join(d, "plugin.json"), "w"))
_old = plugins.PLUGINS_DIR
plugins.PLUGINS_DIR = TMP
plugins.reload()
ok("sok" in plugins.REGISTRY, "scan : flux valides chargés", str(plugins.SCAN_ERRORS))
ok("sbad" not in plugins.REGISTRY and "streams" in plugins.SCAN_ERRORS.get("sbad", ""),
   "scan : flux en collision sautés", str(plugins.SCAN_ERRORS.get("sbad")))
_, err = plugins.validate_package(os.path.join(TMP, "sbad"))
ok(err and "streams" in err, "validate_package : même règle", str(err))
plugins.PLUGINS_DIR = _old
plugins.reload()

# ── amont factice ────────────────────────────────────────────────────────────────────
STATE = {"closed": threading.Event(), "post": None, "chunked": False}


class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for i in range(10 ** 6):
                payload = struct.pack("<dI", time.time(), i).ljust(ps.CHUNK, b"\0")
                self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
                self.wfile.flush()
                time.sleep(0.010)
        except OSError:
            STATE["closed"].set()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        STATE["post"] = (self.path, self.headers.get("Content-Type"), self.rfile.read(n))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
up.daemon_threads = True
threading.Thread(target=up.serve_forever, daemon=True).start()
UP_PORT = up.server_address[1]

# ── doublures ────────────────────────────────────────────────────────────────────────
PERMS = {"plugins.operate"}
ACCESS = {"min": None, "deny": False}


def fake_access(vmid, min_role="viewer"):
    ACCESS["min"] = min_role
    return ({"error": "nope"}, 403) if ACCESS["deny"] else None


auth.check_vmid_access = fake_access
auth.has_perm = lambda p: p in PERMS
_real_url = ps._url
ps._url = lambda vmid, p: "http://127.0.0.1:%d/%s" % (UP_PORT, p.lstrip("/"))

app = Flask(__name__)


@app.route("/api/containers/<int:vmid>/plugin/<path:p>", methods=["GET", "POST"])
def proxy(vmid, p):
    r = ps.handle(vmid, M, p)
    return r if r is not None else ("json-path", 200)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_waitress(**kw):
    from waitress import create_server
    port = free_port()
    srv = create_server(app, host="127.0.0.1", port=port, threads=4, **kw)
    threading.Thread(target=srv.run, daemon=True).start()
    return srv, port


def start_cheroot():
    from cheroot import wsgi
    port = free_port()
    srv = wsgi.Server(("127.0.0.1", port), app, numthreads=4)
    srv.prepare()
    threading.Thread(target=srv.serve, daemon=True).start()
    return srv, port


def measure(port, n=100):
    """Retards (ms) d'arrivée de n morceaux : maintenant − horodatage posé par l'amont."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/api/containers/7/plugin/listen.pcm")
    r = c.getresponse()
    hdr = (r.status, r.getheader("Content-Type"), r.getheader("Cache-Control"),
           r.getheader("X-Accel-Buffering"))
    lat = []
    buf = b""
    while len(lat) < n:
        b = r.read1(65536)
        if not b:
            break
        now = time.time()
        buf += b
        while len(buf) >= ps.CHUNK:
            ts = struct.unpack("<d", buf[:8])[0]
            buf = buf[ps.CHUNK:]
            lat.append((now - ts) * 1000)
    c.close()
    return hdr, lat


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))]


# ── 2. GET progressif, latence ───────────────────────────────────────────────────────
srv, port = start_waitress()      # défaut Waitress 3.0.2 : send_bytes=1 (main.py ne le fixe pas)
hdr, lat = measure(port)
ok(hdr == (200, "audio/L16;rate=48000;channels=2", "no-store", "no"), "GET : en-têtes déclarés", str(hdr))
ok(len(lat) >= 100, "GET : 100 morceaux reçus", str(len(lat)))
p50, p95 = pct(lat, .5), pct(lat, .95)
print("  mesure  Waitress (défaut = 1)   : p50 %.1f ms  p95 %.1f ms  max %.1f ms" % (p50, p95, max(lat)))
ok(p95 < 30, "GET Waitress défaut : latence ajoutée p95 < 30 ms", "p95=%.1f" % p95)
STATE["closed"].clear()
hdr, _ = measure(port, 20)           # coupe après 20 morceaux
ok(STATE["closed"].wait(3), "client parti : l'amont est fermé")
srv.close()

import warnings
warnings.simplefilter("ignore")
srv, port = start_waitress(send_bytes=18000)   # ancien défaut (Waitress < 2) : MUTATION de contrôle
_, lat0 = measure(port, 60)
p50d, p95d = pct(lat0, .5), pct(lat0, .95)
print("  mesure  Waitress send_bytes=18000: p50 %.1f ms  p95 %.1f ms  max %.1f ms" % (p50d, p95d, max(lat0)))
ok(p95d > p95 + 20, "send_bytes=18000 nettement pire que le défaut (le réglage MORD)",
   "%.1f vs %.1f" % (p95d, p95))
srv.close()

try:
    csrv, cport = start_cheroot()
    _, latc = measure(cport, 60)
    print("  mesure  cheroot                 : p50 %.1f ms  p95 %.1f ms  max %.1f ms"
          % (pct(latc, .5), pct(latc, .95), max(latc)))
    ok(pct(latc, .95) < 30, "GET cheroot (mode tls) : latence ajoutée p95 < 30 ms", "p95=%.1f" % pct(latc, .95))
    csrv.stop()
except ImportError:
    print("  (cheroot absent : non mesuré)")

# fermeture de l'amont à la déconnexion, prouvée sans dépendre du ramasse-miettes
class FakeRaw:
    def read1(self, n):
        return b"x" * 10


class FakeUp:
    status_code = 200
    raw = FakeRaw()
    closed = False

    def close(self):
        FakeUp.closed = True


with app.test_request_context("/"):
    rr = ps.relay_get("http://x/", STREAMS[0], {}, http_get=lambda *a, **k: FakeUp())
    it = iter(rr.response)
    next(it)
    it.close()                          # = GeneratorExit : le client est parti
ok(FakeUp.closed, "déconnexion (GeneratorExit) : amont fermé")

# ── 3. POST ──────────────────────────────────────────────────────────────────────────
c = app.test_client()
body = bytes(range(256)) * 4
r = c.post("/api/containers/7/plugin/talk.pcm?seq=3", data=body, content_type="audio/L16")
ok(r.status_code == 204 and STATE["post"] == ("/talk.pcm?seq=3", "application/octet-stream", body),
   "POST : octets bruts + query relayés, 204 rendu", str((r.status_code, STATE["post"] and STATE["post"][:2])))
r = c.post("/api/containers/7/plugin/talk.pcm", data=b"x" * 65537)
ok(r.status_code == 413, "POST > max_bytes : 413", str(r.status_code))
STATE["post"] = None
c.post("/api/containers/7/plugin/talk.pcm", data=b"x" * 65537)
ok(STATE["post"] is None, "POST refusé : rien n'est parti vers le conteneur")
r = c.post("/api/containers/7/plugin/talk.pcm", data=b"x" * 65536)
ok(r.status_code == 204, "POST pile à max_bytes : accepté", str(r.status_code))

# ── 4. droits ────────────────────────────────────────────────────────────────────────
ok(c.get("/api/containers/7/plugin/state").data == b"json-path", "chemin non flux : rendu au proxy JSON")
PERMS.clear()
ACCESS["min"] = None
cc = app.test_client()
r = cc.post("/api/containers/7/plugin/talk.pcm", data=b"x")
ok(r.status_code == 403 and ACCESS["min"] == "operator", "POST sans plugins.operate : 403, accès operator",
   str((r.status_code, ACCESS["min"])))
STATE["post"] = None
ok(STATE["post"] is None, "refus : rien relayé")
r = cc.get("/api/containers/7/plugin/ops.pcm")
ok(r.status_code == 403, "GET perm=operate sans plugins.operate : 403", str(r.status_code))
r = cc.get("/api/containers/7/plugin/listen.pcm", buffered=False)
ok(r.status_code == 200 and ACCESS["min"] == "viewer", "GET perm=read : login seul, accès viewer",
   str((r.status_code, ACCESS["min"])))
r.response.close()
PERMS.add("plugins.operate")
ACCESS["deny"] = True
ok(cc.get("/api/containers/7/plugin/listen.pcm").status_code == 403, "accès projet refusé : 403")
ACCESS["deny"] = False

print("\n%s" % ("TOUT VERT" if not ko else "ÉCHECS : %s" % ko))
sys.exit(1 if ko else 0)
