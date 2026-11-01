#!/usr/bin/env python3
"""Vérifie le DISTRIBUTEUR DE FLUX (app/stream_hub.py) : un flux d'état ouvert ne coûte plus
AUCUN fil du serveur web.

Un VRAI Waitress (4 fils seulement) et un VRAI cheroot en TLS (2 fils), montés comme main.py
(registre des requêtes, en-têtes de confiance, Detacher le plus extérieur), devant le VRAI relais
d'état (app/state_relay.py) et le VRAI proxy de flux (app/plugin_streams.handle), contre un FAUX
conteneur SSE. Contrat :
 (1) 20 flux sur 4 fils : tous servis, AUCUN fil occupé au repos, une requête ordinaire répond ;
 (2) octets identiques au chemin historique (fil gardé, `stream_hub_enabled` coupé) : mêmes blocs,
     premier bloc = dernier état, keepalives ; mêmes en-têtes, after_request compris ; fin propre
     (terminateur chunked) quand l'amont tombe ;
 (3) droits vérifiés AVANT la remise : un refus reste un 403 servi par le fil, rien n'est remis ;
 (4) client lent : coupé et COMPTÉ, les autres continuent sans retard ;
 (5) départ d'un client détecté à la lecture (FIN), bien avant le keepalive ;
 (6) TLS (cheroot) : mêmes octets, aucun fil gardé, départ détecté ;
 (7) contrôleur de secours : pas d'abonnement, proxy brut (le fil est gardé, comme avant) ;
 (8) visibilité : Monitoring → Connexions liste les flux du distributeur (utilisateur, adresse,
     cible, octets), hors des fils occupés ;
 (9) « le dernier gagne » : un client en retard reçoit le dernier état, l'arriéré reste borné ;
 (10) aucun réveil perdu : remise et publication servies sans attendre un délai, même quand le
     dernier client vient de partir.

Aucun réseau externe ; base jetable. Mutations : `STREAM_HUB_SRC=<copie mutée>` charge le module
depuis ce fichier ; `PLUGIN_STREAMS_SRC` de même pour le proxy.

    ./venv/bin/python tests/verif_stream_hub.py
"""
import datetime
import http.client
import importlib.util
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)

from app import database as _db                                 # noqa: E402
_TMP = tempfile.mkdtemp(prefix="verif_stream_hub_")
_db.DB_PATH = os.path.join(_TMP, "db.sqlite")
_db.init_db()
sys.stdout.reconfigure(line_buffering=True)
import app                                                     # noqa: E402


def _load(name, env):
    if not os.environ.get(env):
        return importlib.import_module("app." + name)
    spec = importlib.util.spec_from_file_location("app." + name, os.environ[env])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["app." + name] = mod
    spec.loader.exec_module(mod)
    setattr(app, name, mod)
    return mod


import importlib                                               # noqa: E402
sh = _load("stream_hub", "STREAM_HUB_SRC")
ps = _load("plugin_streams", "PLUGIN_STREAMS_SRC")
from app import state_relay as sr, http_connections as hc, web_mode, auth   # noqa: E402
from flask import Flask, jsonify, request                      # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % (detail,))
        ko.append(titre)


def wait_for(pred, timeout=5.0, step=0.005):
    t = time.time() + timeout
    while time.time() < t:
        if pred():
            return True
        time.sleep(step)
    return pred()


sr.STALE_S = 1.5
sr.READ_TIMEOUT_S = 1.5
sr.CONNECT_TIMEOUT_S = 1.0
sr.BACKOFF_S = (0.05, 0.1, 0.2)
sr.FIRST_EVENT_WAIT_S = 2.0
sr.CLIENT_KEEPALIVE_S = 0.3
sh.STALL_S = 1.0
_HUB_ON = [True]
sh.enabled = lambda: _HUB_ON[0]


# ── faux conteneur ───────────────────────────────────────────────────────────────────────────
class Fake:
    def __init__(self):
        self.cv = threading.Condition()
        self.doc = {"pgm": 0, "pvw": 1, "pad": ""}
        self.rev = 0
        self.sent = []
        self.kill = 0
        self.conns = 0

    def push(self, **changes):
        with self.cv:
            self.doc = dict(self.doc, **changes)
            self.rev += 1
            self.cv.notify_all()


F = Fake()


class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/events":
            self.send_response(404)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        self.close_connection = True
        kill0 = F.kill
        with F.cv:
            F.conns += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            rev, last = None, 0.0
            while F.kill == kill0:
                with F.cv:
                    if F.rev == rev:
                        F.cv.wait(0.02)
                    cur, doc = F.rev, F.doc
                if cur != rev:
                    rev = cur
                    blk = b"data: " + json.dumps(doc).encode() + b"\n\n"
                    F.sent.append(blk)
                    self.wfile.write(blk)
                    self.wfile.flush()
                    last = time.time()
                elif time.time() - last > 0.3:
                    self.wfile.write(b": ka\n\n")
                    self.wfile.flush()
                    last = time.time()
        except OSError:
            pass
        finally:
            with F.cv:
                F.conns -= 1


up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
up.daemon_threads = True
threading.Thread(target=up.serve_forever, daemon=True).start()
UP_PORT = up.server_address[1]

ENTRY = {"path": "/events", "method": "GET", "perm": "read", "content_type": "text/event-stream"}
M = {"type": "fakemix", "control": {"port": UP_PORT, "endpoints": ["/state"], "read_endpoints": ["/state"],
                                    "streams": [ENTRY]}}


class FakeWorld(sr.World):
    def __init__(self):
        super().__init__()
        self.active = True

    def is_active(self):
        return self.active

    def enabled(self):
        return True

    def context(self, vmid):
        return M, "host%d" % vmid

    def ip(self, vmid):
        return "127.0.0.1"


W = FakeWorld()
R = sr.Relay(W)
sr.RELAY = R

# Droits : la doublure lit un en-tête (la VRAIE fonction handle décide de l'ordre deny → relais).
auth.check_vmid_access = lambda vmid, lvl: None if request.headers.get("X-Who") == "ok" else \
    (jsonify({"error": "forbidden"}), 403)
auth.has_perm = lambda p: True
ps._url = lambda vmid, p: "http://127.0.0.1:%d/%s" % (UP_PORT, p.lstrip("/"))

# ── l'application, montée comme main.py ──────────────────────────────────────────────────────
web = Flask(__name__)


@web.route("/api/containers/<int:vmid>/plugin/<path:p>")
def proxy(vmid, p):
    r = ps.handle(vmid, M, p)
    return r if r is not None else ("not a stream", 404)


@web.route("/ping")
def ping():
    return jsonify({"pong": True})


@web.after_request
def _after(resp):
    resp.headers["X-After"] = "1"             # un en-tête posé APRÈS la vue : doit survivre
    return resp


hc.install(web)
web.wsgi_app = web_mode.EnTetesTransmis(web.wsgi_app)
web.wsgi_app = sh.Detacher(web.wsgi_app)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


from waitress import create_server                             # noqa: E402
WPORT = free_port()
wsrv = create_server(web_mode.EcouteMachines(web), host="127.0.0.1", port=WPORT, threads=4)
sh.install_waitress(wsrv)
hc.attach_waitress(wsrv, "http")
threading.Thread(target=wsrv.run, daemon=True).start()
DISP = wsrv.task_dispatcher


class Client:
    """Navigateur : corps décodé (chunked) bloc par bloc, heure d'arrivée de chaque bloc."""

    def __init__(self, vmid, who="ok", port=None, tls=False, slow=False, read=True):
        self.blocks, self.times, self.status, self.headers = [], [], None, {}
        self.clean_end = None
        self.done = threading.Event()
        port = port or WPORT
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.c = http.client.HTTPSConnection("127.0.0.1", port, timeout=10, context=ctx)
        else:
            self.c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.c.connect()
        if slow:
            self.c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        self.c.request("GET", "/api/containers/%d/plugin/events" % vmid, headers={"X-Who": who})
        self.sock = self.c.sock
        if slow:
            return
        self.r = self.c.getresponse()
        self.status = self.r.status
        self.headers = {k.lower(): v for k, v in self.r.getheaders()}
        if self.status == 200 and read:
            threading.Thread(target=self._run, daemon=True).start()
        elif read:
            self.body = self.r.read()
            self.done.set()

    def _run(self):
        buf = b""
        try:
            while True:
                b = self.r.read1(65536)
                if not b:
                    self.clean_end = True
                    return
                buf += b
                while b"\n\n" in buf:
                    blk, buf = buf.split(b"\n\n", 1)
                    self.blocks.append(blk + b"\n\n")
                    self.times.append(time.perf_counter())
        except http.client.IncompleteRead:
            self.clean_end = False
        except (OSError, ValueError, AttributeError):
            self.clean_end = False if self.clean_end is None else self.clean_end
        finally:
            self.done.set()

    def data(self):
        return [b for b in self.blocks if b.startswith(b"data:")]

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def get(path, port=None):
    c = http.client.HTTPConnection("127.0.0.1", port or WPORT, timeout=5)
    t = time.perf_counter()
    c.request("GET", path)
    r = c.getresponse()
    r.read()
    return r.status, (time.perf_counter() - t) * 1000


def server_threads():
    """Fils du côté serveur (hors lecteurs de ce banc et hors faux conteneur)."""
    return sum(1 for t in threading.enumerate()
               if not t.name.endswith(("(_run)", "(process_request_thread)")))


# ── (1) 20 flux sur 4 fils ───────────────────────────────────────────────────────────────────
F.push(pgm=1)
base_threads = server_threads()
cs = [Client(1) for _ in range(20)]
ok(all(c.status == 200 for c in cs), "20 flux : tous 200 sur un Waitress de 4 fils",
   [c.status for c in cs])
ok(wait_for(lambda: all(c.data() for c in cs), 3), "20 flux : chacun reçoit le premier bloc")
ok(wait_for(lambda: DISP.active_count == 0, 2), "20 flux ouverts : AUCUN fil Waitress occupé au repos",
   DISP.active_count)
st, ms = get("/ping")
ok(st == 200 and ms < 500, "requête ordinaire servie pendant les 20 flux (%.1f ms)" % ms, (st, ms))
ok(server_threads() - base_threads <= 3, "fils côté serveur : pas un par flux (+%d pour 20 flux)"
   % (server_threads() - base_threads), [t.name for t in threading.enumerate()])
ok(sh.HUB.stats()["clients"] == 20, "distributeur : 20 clients", sh.HUB.stats()["clients"])
ok(R.subs[1].clients == 20 and F.conns == 1, "relais : 20 navigateurs, UNE connexion au conteneur",
   (R.subs[1].clients, F.conns))
ok(not any(e.get("vmid") == 1 for e in hc._entries.values()),
   "registre des requêtes : le flux remis ne compte plus comme fil occupé")

lat = []
for k in range(8):
    n0 = [len(c.data()) for c in cs]
    t0 = time.perf_counter()
    F.push(pgm=10 + k)
    wait_for(lambda: all(len(c.data()) > n0[i] for i, c in enumerate(cs)), 2)
    for i, c in enumerate(cs):
        ts = [t for j, t in enumerate(t_ for b, t_ in zip(c.blocks, c.times) if b.startswith(b"data:"))
              if j >= n0[i]]
        if ts:
            lat.append((ts[0] - t0) * 1000)
    time.sleep(0.05)
lat.sort()
p95 = lat[int(len(lat) * 0.95) - 1] if lat else None
ok(len(lat) == 160 and p95 < 100, "commande → 20 clients : %d mises à jour, p50 %.1f ms, p95 %.1f ms"
   % (len(lat), lat[len(lat) // 2] if lat else -1, p95 or -1), len(lat))

# ── (2) octets identiques au chemin historique ───────────────────────────────────────────────
time.sleep(0.8)
ka = [b for b in cs[0].blocks if b == b": ka\n\n"]
ok(len(ka) >= 1, "keepalive `: ka` écrit par le distributeur sans bloc nouveau", len(ka))
want = [b for b in F.sent]
got = cs[0].data()
ok(got == want[-len(got):] and got[0] == want[len(want) - len(got)],
   "octets : les blocs `data:` reçus sont STRICTEMENT ceux du conteneur", (got[:2], want[-2:]))
h = cs[0].headers
ok(h.get("content-type", "").startswith("text/event-stream") and h.get("cache-control") == "no-store"
   and h.get("x-accel-buffering") == "no" and h.get("x-after") == "1"
   and h.get("transfer-encoding") == "chunked" and "content-length" not in h,
   "en-têtes : ceux de Flask (after_request compris), chunked comme Waitress", h)
F.push(pgm=99)
time.sleep(0.15)
last = F.sent[-1]
late = Client(1)
ok(wait_for(lambda: late.data(), 2) and late.data()[0] == last,
   "premier bloc d'un client arrivé tard = DERNIER état", (late.data()[:1], last))
cs.append(late)

# chemin historique (fil gardé) : mêmes octets, mêmes en-têtes métier
_HUB_ON[0] = False
old = Client(1)
ok(wait_for(lambda: DISP.active_count >= 1, 1), "interrupteur coupé : le flux garde un fil (chemin historique)",
   DISP.active_count)
F.push(pgm=123)
ok(wait_for(lambda: old.data() and old.data()[-1] == F.sent[-1] and cs[0].data()[-1] == F.sent[-1], 2),
   "même bloc servi par le fil et par le distributeur", (old.data()[-1:], cs[0].data()[-1:]))
ho = old.headers
ok(all(ho.get(k) == h.get(k) for k in ("content-type", "cache-control", "x-accel-buffering", "x-after",
                                       "transfer-encoding")),
   "mêmes en-têtes métier que le chemin historique", (ho, h))
_HUB_ON[0] = True

# ── (3) droits AVANT la remise ───────────────────────────────────────────────────────────────
tot0 = sh.HUB.stats()["total_clients"]
den = Client(1, who="intrus")
ok(den.status == 403 and b"forbidden" in den.body, "droits : refus 403 servi normalement", den.status)
ok(sh.HUB.stats()["total_clients"] == tot0, "droits : rien remis au distributeur pour un refus")

# ── (4) client lent ──────────────────────────────────────────────────────────────────────────
slow = Client(1, slow=True)
time.sleep(0.3)
cut0 = sh.HUB.stats()["slow_cut"]
pad = "x" * 300000
lat2 = []
others = cs[1:6]
for k in range(30):
    n0 = [len(c.data()) for c in others]
    t0 = time.perf_counter()
    F.push(pgm=200 + k, pad=pad[k:])
    wait_for(lambda: all(len(c.data()) > n0[i] for i, c in enumerate(others)), 2)
    for i, c in enumerate(others):
        ts = [t for j, t in enumerate(t_ for b, t_ in zip(c.blocks, c.times) if b.startswith(b"data:"))
              if j >= n0[i]]
        if ts:
            lat2.append((ts[0] - t0) * 1000)
    time.sleep(0.05)
ok(wait_for(lambda: sh.HUB.stats()["slow_cut"] > cut0, 4), "client lent : coupé et compté",
   sh.HUB.stats()["closes"])
lat2.sort()
ok(len(lat2) == 150 and lat2[int(len(lat2) * 0.95) - 1] < 150,
   "client lent : les autres continuent (p95 %.1f ms sur %d mises à jour)"
   % (lat2[int(len(lat2) * 0.95) - 1] if lat2 else -1, len(lat2)))
F.push(pad="")
slow.close()

# ── (5) départ détecté à la lecture ──────────────────────────────────────────────────────────
sr.CLIENT_KEEPALIVE_S = 5.0                  # nouveau conteneur → nouveau feed : keepalive à 5 s
d = [Client(2) for _ in range(3)]
ok(wait_for(lambda: all(c.data() for c in d), 3), "départ : 3 clients servis sur un autre conteneur")
n_before = R.subs[2].clients
t0 = time.perf_counter()
d[0].close()
ok(wait_for(lambda: R.subs[2].clients == n_before - 1, 1.0),
   "départ détecté en %.0f ms (keepalive à 5 s : c'est la lecture qui l'a vu)" % ((time.perf_counter() - t0) * 1000),
   R.subs[2].clients)
dt = (time.perf_counter() - t0) * 1000
ok(dt < 300, "départ : moins de 300 ms (%.0f ms)" % dt)

# ── amont perdu → fin propre ─────────────────────────────────────────────────────────────────
F.kill += 1
ok(wait_for(lambda: all(c.done.is_set() for c in d[1:]), 3) and all(c.clean_end for c in d[1:]),
   "amont perdu : les flux du distributeur finissent PROPREMENT (terminateur chunked)",
   [c.clean_end for c in d[1:]])
ok(wait_for(lambda: all(c.done.is_set() for c in cs), 3) and all(c.clean_end for c in cs),
   "amont perdu : 21 flux du conteneur 1 fermés proprement", [c.clean_end for c in cs][:5])
ok(wait_for(lambda: (R.subs.get(1) is None or R.subs[1].clients == 0), 3),
   "compteur de navigateurs du relais revenu à 0 (avec le flux historique)",
   R.subs.get(1) and R.subs[1].clients)
old.close()

# ── (6) TLS : cheroot ────────────────────────────────────────────────────────────────────────
from cryptography import x509                                   # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec        # noqa: E402
from cryptography.x509.oid import NameOID                       # noqa: E402
key = ec.generate_private_key(ec.SECP256R1())
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
now = datetime.datetime.now(datetime.timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
crt_p, key_p = os.path.join(_TMP, "t.crt"), os.path.join(_TMP, "t.key")
open(crt_p, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
open(key_p, "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
from cheroot import wsgi as cwsgi                               # noqa: E402
from cheroot.ssl.builtin import BuiltinSSLAdapter               # noqa: E402
TPORT = free_port()
csrv = cwsgi.Server(("127.0.0.1", TPORT), web, numthreads=2, request_queue_size=32)
ad = BuiltinSSLAdapter(crt_p, key_p)
ad.context = web_mode.contexte_tls(crt_p, key_p)
csrv.ssl_adapter = ad
sh.install_cheroot(csrv)
csrv.prepare()
threading.Thread(target=csrv.serve, daemon=True).start()
tpool = hc.attach_cheroot(csrv, TPORT, "https")
sr.CLIENT_KEEPALIVE_S = 0.3
F.push(pgm=500)
ts_ = [Client(3, port=TPORT, tls=True) for _ in range(8)]
ok(all(c.status == 200 for c in ts_) and wait_for(lambda: all(c.data() for c in ts_), 3),
   "TLS : 8 flux sur un cheroot de 2 fils, tous servis", [c.status for c in ts_])
ok(wait_for(lambda: tpool.busy() == 0, 2), "TLS : aucun fil cheroot occupé au repos", tpool.busy())
ok(all(c.headers.get("x-after") == "1" and c.headers.get("transfer-encoding") == "chunked" for c in ts_),
   "TLS : en-têtes de Flask, chunked", ts_[0].headers)
F.push(pgm=501)
ok(wait_for(lambda: all(c.data()[-1] == F.sent[-1] for c in ts_), 2), "TLS : mêmes octets que le conteneur")
time.sleep(0.5)
ok(all(b": ka\n\n" in c.blocks for c in ts_), "TLS : keepalives écrits (écriture non bloquante chiffrée)")
n3 = R.subs[3].clients
t0 = time.perf_counter()
ts_[0].close()
ok(wait_for(lambda: R.subs[3].clients == n3 - 1, 1.0),
   "TLS : départ détecté en %.0f ms" % ((time.perf_counter() - t0) * 1000), R.subs[3].clients)
st_t = None
try:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    cc = http.client.HTTPSConnection("127.0.0.1", TPORT, timeout=5, context=ctx)
    cc.request("GET", "/ping")
    st_t = cc.getresponse().status
except Exception as e:                                          # noqa: BLE001
    st_t = str(e)
ok(st_t == 200, "TLS : requête ordinaire servie pendant les flux", st_t)

# ── (8) visibilité ───────────────────────────────────────────────────────────────────────────
snap = hc.snapshot()
rows = [r for r in snap["connections"] if r.get("detached")]
ok(len(rows) == 7 and all(r["vmid"] == 3 and r["ip"] == "127.0.0.1" and r["kind"] == "sse" and r["relay"]
                          and r["bytes"] > 0 and r["thread"] is None for r in rows),
   "Connexions : les 7 flux TLS listés (adresse, cible, octets), sans fil", rows[:1])
ok(snap.get("hub") and snap["hub"]["clients"] == 7 and snap["hub"]["slow_cut"] >= 1
   and snap["hub"]["departures"] >= 2 and "rows" not in snap["hub"],
   "Connexions : section distributeur (clients, lents coupés, départs)", snap.get("hub"))
https_pool = [p for p in snap["pools"] if p["name"] == "https"][0]
ok(https_pool["streams"] == 0, "Connexions : les flux remis ne comptent pas dans les fils de l'écoute",
   https_pool)

# ── (7) contrôleur de secours ────────────────────────────────────────────────────────────────
for s in list(R.subs.values()):
    s.stop()
wait_for(lambda: not R.subs, 3)
W.active = False
tot0 = sh.HUB.stats()["total_clients"]
F.push(pgm=700)
sb = Client(4)
ok(sb.status == 200 and wait_for(lambda: sb.data(), 2), "secours : le flux est servi (proxy brut)", sb.status)
ok(not R.subs and sh.HUB.stats()["total_clients"] == tot0,
   "secours : aucun abonnement, rien remis au distributeur", (list(R.subs), sh.HUB.stats()["total_clients"]))
ok(DISP.active_count >= 1, "secours : le proxy brut garde son fil, comme avant", DISP.active_count)
sb.close()

# ── (9) « le dernier gagne » : un client en retard reçoit le dernier état, pas l'arriéré ──────
hb = sh.Hub(name="stream-hub-test")
fd = hb.feed(label="t", keepalive_s=30)
a_, b_ = socket.socketpair()
a_.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
b_.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
hb.attach(a_, False, b"HEAD\n", None, 0, fd, False)
wait_for(lambda: hb.stats()["clients"] == 1, 2)
blocks = [b"data: %d %s\n\n" % (i, b"y" * 65536) for i in range(100)]
for i, blk in enumerate(blocks):
    fd.publish(blk, i + 1)
time.sleep(0.3)
st9 = hb.stats()
ok(st9["clients"] == 1 and st9["buffered"] <= 3 * 65600,
   "dernier gagne : arriéré borné à ~2 blocs pour un client qui ne lit pas (%d octets en attente)"
   % st9["buffered"], st9["closes"])
got9 = b""
b_.settimeout(0.5)
try:
    while True:
        x = b_.recv(1 << 20)
        if not x:
            break
        got9 += x
        if got9.endswith(blocks[-1]):
            break
except socket.timeout:
    pass
seen = [int(x.split(b" ", 2)[1]) for x in got9.split(b"\n\n") if x.startswith(b"data: ")]
ok(seen and seen[-1] == 99 and len(seen) < 100,
   "dernier gagne : le client reçoit le DERNIER bloc, %d sur 100 (intermédiaires sautés)" % len(seen),
   seen[-3:])
hb.stop()

# ── (10) aucun réveil perdu : publication et remise servies sans attendre un délai ────────────
# (bug vu au banc : un drapeau « déjà réveillé » sautait l'envoi du réveil → une remise restait
# bloquée indéfiniment une fois le dernier client parti, une publication attendait 0,5 s)
import random                                                   # noqa: E402
hw = sh.Hub(name="stream-hub-wake")
fw = hw.feed(label="w", keepalive_s=30)
lat10, stuck = [], 0
for cycle in range(40):                    # le dernier client part, un nouveau arrive : 40 fois
    x_, y_ = socket.socketpair()
    t0 = time.perf_counter()
    hw.attach(x_, False, b"H", b"data: first\n\n", 0, fw, False)
    y_.settimeout(1.0)
    try:
        got = y_.recv(100)
        lat10.append((time.perf_counter() - t0) * 1000)
    except socket.timeout:
        stuck += 1
        got = b""
    for k in range(10):                     # publications à intervalles irréguliers
        time.sleep(random.random() * 0.003)
        t0 = time.perf_counter()
        fw.publish(b"data: %d\n\n" % k, cycle * 100 + k + 1)
        try:
            y_.recv(100)
            lat10.append((time.perf_counter() - t0) * 1000)
        except socket.timeout:
            stuck += 1
    y_.close()
    wait_for(lambda: hw.stats()["clients"] == 0, 1)
# deux publications rapprochées (écart aléatoire de 0 à 50 µs) : la seconde tombe parfois pendant
# que la boucle vide son réveil — c'est là qu'un réveil se perdait
x_, y_ = socket.socketpair()
hw.attach(x_, False, b"H", None, 0, fw, False)
y_.settimeout(1.0)
y_.recv(10)
t_end = time.time() + 20
for i in range(3000):
    if time.time() > t_end:
        break
    fw.publish(b"a", 100000 + 2 * i)
    t1 = time.perf_counter() + random.random() * 0.00005
    while time.perf_counter() < t1:
        pass
    t0 = time.perf_counter()
    tok = b"b%d;" % i
    fw.publish(tok, 100001 + 2 * i)
    buf = b""
    try:
        while tok not in buf:
            buf += y_.recv(4096)
        lat10.append((time.perf_counter() - t0) * 1000)
    except socket.timeout:
        stuck += 1
        break
lat10.sort()
ok(stuck == 0 and lat10 and lat10[-1] < 100,
   "aucun réveil perdu : %d remises/publications servies, max %.1f ms, %d bloquées"
   % (len(lat10), lat10[-1] if lat10 else -1, stuck))
hw.stop()

print()
if ko:
    print("%d ÉCHEC(S) : %s" % (len(ko), ", ".join(ko)))
    os._exit(1)
print("OK — distributeur de flux")
os._exit(0)
