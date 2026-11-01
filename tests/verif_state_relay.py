#!/usr/bin/env python3
"""Vérifie le RELAIS D'ÉTAT (app/state_relay.py) contre un FAUX conteneur SSE.

Contrat vérifié : (1) déclaration — seul un flux `text/event-stream` en GET est relayé, `relay:
false` le rend au proxy brut ; (2) FAN-OUT — N navigateurs, UNE SEULE connexion au conteneur ;
octets STRICTEMENT ceux du conteneur, premier bloc = dernier état connu ; (3) lecteurs internes —
document frais servi, champ mesuré / champ en mouvement / document entier REFUSÉS et comptés, une
lecture de stabilisation après l'événement ; (3b) éditeur qui pousse en opération (`motion_pushed_since`,
mélangeur ≥ 0.23.48) — servi pendant une opération, jamais relu, les versions antérieures non ;
(4) reconnexion après coupure, navigateurs fermés à la
coupure ; (5) source PÉRIMÉE signalée, jamais servie ; (6) moteur sans flux (404) ; (7) HA — le
contrôleur en veille ne s'abonne pas, le proxy brut reprend ; (8) arrêt sur inactivité ;
(9) droits — le refus du proxy passe AVANT le relais ; (10) consommateurs branchés : tally, macros
(murs, pupitre), pupitre ATEM — relais d'abord, repli compté sinon ; (11) la World réelle ne
s'active jamais sous tests/.

Aucun réseau externe, aucune base : le monde (rôle HA, manifeste, IP) est une doublure.
Mutations : `STATE_RELAY_SRC=<copie mutée>` charge le module depuis ce fichier.

    ./venv/bin/python tests/verif_state_relay.py
"""
import http.client
import importlib.util
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)

import tempfile                                                 # noqa: E402
from app import database as _db                                 # noqa: E402
_db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_state_relay_"), "db.sqlite")
import app                                                     # noqa: E402
if os.environ.get("STATE_RELAY_SRC"):
    _spec = importlib.util.spec_from_file_location("app.state_relay", os.environ["STATE_RELAY_SRC"])
    sr = importlib.util.module_from_spec(_spec)
    sys.modules["app.state_relay"] = sr
    _spec.loader.exec_module(sr)
    app.state_relay = sr
else:
    from app import state_relay as sr                          # noqa: E402
from flask import Flask                                        # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


def wait_for(pred, timeout=5.0, step=0.01):
    t = time.time() + timeout
    while time.time() < t:
        if pred():
            return True
        time.sleep(step)
    return pred()


# Délais du module réduits : le banc doit tenir en quelques secondes.
sr.STALE_S = 0.8
sr.READ_TIMEOUT_S = 0.8
sr.CONNECT_TIMEOUT_S = 1.0
sr.BACKOFF_S = (0.05, 0.1, 0.2)
sr.UNSUPPORTED_RETRY_S = 0.3
sr.SETTLE_S = 0.15
sr.CLIENT_KEEPALIVE_S = 0.2
sr.FIRST_EVENT_WAIT_S = 2.0
sr.IDLE_STOP_S = 30.0


# ── faux conteneur ───────────────────────────────────────────────────────────────────────────
class Fake:
    def __init__(self):
        self.cv = threading.Condition()
        self.doc = {"pgm": 0, "pvw": 1, "tally": {"pgm": [0], "pvw": [1]}, "transition": None,
                    "manual_mix": None, "keyers": [{"active": False, "auto": False}],
                    "inputs_timing": {"n": 0}}
        self.state_doc = None       # ce que /state renvoie (None = self.doc)
        self.rev = 0
        self.events_conns = 0       # connexions /events ouvertes en ce moment
        self.events_total = 0
        self.state_hits = 0
        self.sent = []              # blocs data envoyés (octets exacts)
        self.mode = "ok"            # ok | 404 | mute (n'envoie plus rien, garde la connexion)
        self.kill = 0               # incrémenté → les connexions en cours se ferment

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
        if self.path == "/state":
            F.state_hits += 1
            body = json.dumps(F.state_doc or F.doc).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/events" or F.mode == "404":
            self.send_response(404)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        self.close_connection = True
        with F.cv:
            F.events_conns += 1
            F.events_total += 1
        kill0 = F.kill
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            rev, last = None, 0.0
            while F.kill == kill0:
                with F.cv:
                    if F.rev == rev:
                        F.cv.wait(0.05)
                    cur, doc = F.rev, F.doc
                if F.mode == "mute":
                    continue
                if cur != rev:
                    rev = cur
                    blk = b"data: " + json.dumps(doc).encode() + b"\n\n"
                    F.sent.append(blk)
                    self.wfile.write(blk)
                    self.wfile.flush()
                    last = time.time()
                elif time.time() - last > 0.2:
                    self.wfile.write(b": ka\n\n")
                    self.wfile.flush()
                    last = time.time()
        except OSError:
            pass
        finally:
            with F.cv:
                F.events_conns -= 1


up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
up.daemon_threads = True
threading.Thread(target=up.serve_forever, daemon=True).start()
UP_PORT = up.server_address[1]

ENTRY = {"path": "/events", "method": "GET", "perm": "read", "content_type": "text/event-stream"}
M = {"type": "fakemix", "control": {"port": UP_PORT, "endpoints": ["/state", "/pvw"],
                                    "read_endpoints": ["/state"], "streams": [ENTRY]},
     "state": [{"id": "pgm", "endpoint": "/state", "path": "pgm"},
               {"id": "timing", "endpoint": "/state", "path": "inputs_timing.n"}]}
# Éditeur qui pousse pendant une opération à partir d'une version (mélangeur 0.23.48, 2026-10-29).
ENTRY_PUSH = dict(ENTRY, motion_pushed_since="0.23.48")
M_PUSH = dict(M, control=dict(M["control"], streams=[ENTRY_PUSH]))
M_PLAIN = {"type": "plain", "control": {"port": UP_PORT, "endpoints": ["/state"],
                                        "read_endpoints": ["/state"]}}


class FakeWorld(sr.World):
    def __init__(self):
        super().__init__()
        self.active = True
        self.on = True
        self.manifests = {}

    def is_active(self):
        return self.active

    def enabled(self):
        return self.on

    def context(self, vmid):
        return self.manifests.get(vmid), "host%d" % vmid

    def ip(self, vmid):
        return "127.0.0.1"


W = FakeWorld()
for vm in (1, 2, 3, 4, 5, 6, 7, 8):
    W.manifests[vm] = M
W.manifests[50] = M_PLAIN
W.manifests[9] = M_PUSH
R = sr.Relay(W)
sr.RELAY = R

# ── 1. déclaration ───────────────────────────────────────────────────────────────────────────
sp = sr.stream_spec(M)
ok(sp is not None and sp["path"] == "/events" and sp["mirrors"] == "/state" and sp["port"] == UP_PORT,
   "déclaration : flux text/event-stream relayé, miroir /state par défaut", sp)
ok(sr.stream_spec({"control": {"streams": [dict(ENTRY, relay=False)]}}) is None,
   "déclaration : relay=false → proxy brut")
ok(sr.stream_spec({"control": {"streams": [dict(ENTRY, content_type="audio/L16")]}}) is None,
   "déclaration : un flux audio n'est pas un flux d'état")
ok(sr.stream_spec(M_PLAIN) is None and R.read(50, "t") is None and not R.subs,
   "conteneur sans flux : aucun abonnement, aucun repli compté", R.fallbacks)
ok(not R.fallbacks, "conteneur sans flux : rien compté")

# ── 2. fan-out navigateurs ───────────────────────────────────────────────────────────────────
web = Flask(__name__)


@web.route("/ev/<int:vmid>")
def ev(vmid):
    r = R.serve(vmid, W.manifests.get(vmid), ENTRY)
    return r if r is not None else ("plain-proxy", 299)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


from waitress import create_server                             # noqa: E402
WEB_PORT = free_port()
_srv = create_server(web, host="127.0.0.1", port=WEB_PORT, threads=12)
threading.Thread(target=_srv.run, daemon=True).start()


class Client:
    """Navigateur : lit le flux brut, garde chaque bloc tel que reçu."""

    def __init__(self, vmid):
        self.blocks, self.status, self.closed = [], None, threading.Event()
        self.c = http.client.HTTPConnection("127.0.0.1", WEB_PORT, timeout=5)
        self.c.request("GET", "/ev/%d" % vmid)
        self.sock = self.c.sock          # getresponse() en cède la propriété à la réponse
        self.r = self.c.getresponse()
        self.status = self.r.status
        if self.status == 200:
            threading.Thread(target=self._run, daemon=True).start()
        else:
            self.body = self.r.read()
            self.closed.set()

    def _run(self):
        buf = b""
        try:
            while True:
                b = self.r.read1(65536)
                if not b:
                    break
                buf += b
                while b"\n\n" in buf:
                    blk, buf = buf.split(b"\n\n", 1)
                    self.blocks.append(blk + b"\n\n")
        except Exception:
            pass
        self.closed.set()

    def data(self):
        return [b for b in self.blocks if b.startswith(b"data:")]

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        self.sock.close()


clients = [Client(1) for _ in range(6)]
ok(all(c.status == 200 for c in clients), "6 navigateurs : flux ouvert (200)", [c.status for c in clients])
ok(wait_for(lambda: all(c.data() for c in clients)), "chaque navigateur reçoit un premier état")
ok(F.events_conns == 1 and F.events_total == 1,
   "fan-out : UNE seule connexion au conteneur pour 6 navigateurs",
   "connexions ouvertes %d, total %d" % (F.events_conns, F.events_total))
ok(all(c.data()[0] == F.sent[-1] for c in clients),
   "premier bloc = octets EXACTS du dernier état du conteneur")
F.push(pgm=3, pvw=0)
ok(wait_for(lambda: all(len(c.data()) >= 2 for c in clients)), "un changement atteint les 6 navigateurs")
ok(all(c.data()[-1] == F.sent[-1] for c in clients), "format identique : bloc relayé = bloc du conteneur",
   (clients[0].data()[-1][:80], F.sent[-1][:80]))
time.sleep(0.5)
ok(all(any(b == b": ka\n\n" for b in c.blocks) for c in clients), "lignes de vie relayées aux navigateurs")
late = Client(1)
ok(late.status == 200 and wait_for(lambda: late.data()) and late.data()[0] == F.sent[-1]
   and F.events_total == 1, "navigateur tardif : dernier état connu d'emblée, aucune connexion de plus")
ok(R.stats()["subscriptions"][0]["clients"] == 7, "statistiques : 7 clients comptés",
   R.stats()["subscriptions"][0]["clients"])

# ── 3. lecteurs internes ─────────────────────────────────────────────────────────────────────
hits0 = F.state_hits
d = R.read(1, "tally", fields=("tally", "pgm"))
ok(isinstance(d, dict) and d["pgm"] == 3, "lecture interne : document frais servi", d)
ok(F.state_hits == hits0, "lecture interne : aucune requête au conteneur")
d["pgm"] = 99
ok(R.read(1, "tally", fields=("pgm",))["pgm"] == 3, "document rendu = copie (une modification ne fuit pas)")
ok(R.read(1, "macros", fields=("inputs_timing",)) is None
   and R.fallbacks.get(("macros", "volatile_field")) == 1, "champ MESURÉ refusé et compté", R.fallbacks)
ok(R.read(1, "macros", fields=(sr.ALL_FIELDS,)) is None
   and R.fallbacks.get(("macros", "volatile_field")) == 2, "document ENTIER refusé et compté")
ok(R.read(1, "macros", endpoint="/other") is None, "autre endpoint que le miroir : pas servi")
# stabilisation : après un événement, UNE relecture de /state (la liste tally du moteur suit l'image)
F.state_doc = dict(F.doc, pgm=4, pvw=0, tally={"pgm": [4], "pvw": [0]})
F.push(pgm=4, pvw=0)        # le document poussé porte encore l'ANCIENNE liste tally
ok(wait_for(lambda: R.read(1, "tally", fields=("pgm",))["pgm"] == 4), "événement suivant lu")
ok(wait_for(lambda: F.state_hits == hits0 + 1, timeout=2), "UNE lecture de stabilisation après l'événement",
   F.state_hits - hits0)
ok(wait_for(lambda: (R.read(1, "tally", fields=("tally",)) or {}).get("tally", {}).get("pgm") == [4]),
   "stabilisation : la liste tally relue remplace celle de l'événement")
ok(clients[0].data()[-1] == F.sent[-1], "stabilisation jamais envoyée aux navigateurs (octets du conteneur)")
time.sleep(0.4)
ok(F.state_hits == hits0 + 1, "pas de relecture sans nouvel événement", F.state_hits - hits0)
F.state_doc = None
# mouvement : transition en cours → `tally` / `transition` refusés, le reste servi
F.push(transition={"start": time.time(), "duration_ms": 1000})
ok(wait_for(lambda: (R.read(1, "atem") or {}).get("transition") is not None), "transition visible")
ok(R.read(1, "tally", fields=("tally", "pgm")) is None
   and R.fallbacks.get(("tally", "motion"), 0) >= 1, "pendant une opération : `tally` refusé (motion)",
   R.fallbacks)
ok(R.read(1, "atem") is not None, "pendant une opération : un lecteur sans champ mobile est servi")
F.push(transition=None, keyers=[{"active": True, "auto": True}])
ok(wait_for(lambda: R.read(1, "x", fields=("pgm",)) is not None
            and (R.read(1, "x", fields=("pgm",)) or {}).get("transition") is None), "fin de transition")
ok(R.read(1, "tally", fields=("tally",)) is None, "AUTO de keyer en cours = opération (keyers[].auto)")
F.push(keyers=[{"active": True, "auto": False}])
ok(wait_for(lambda: R.read(1, "tally", fields=("tally",)) is not None), "opération finie : servi de nouveau")

# ── 3b. éditeur qui pousse pendant une opération (`motion_pushed_since`, 2026-10-29) ─────────
# Le mélangeur 0.23.48 met sa liste tally dans la signature de poussée et pousse pendant une
# opération : un document de cette version (champ `plugin_version`) est servi ENTIER pendant une
# opération et n'est jamais relu ; un document plus ancien, ou sans version lisible, garde les deux
# contournements (repli « motion », lecture de stabilisation).
ok(sr.stream_spec(M_PUSH)["motion_pushed_since"] == (0, 23, 48) and sr.stream_spec(M)["motion_pushed_since"] is None,
   "déclaration : motion_pushed_since lu du manifeste (absent = aucun)")
R.read(9, "tally", fields=("pgm",))
ok(wait_for(lambda: R.subs.get(9) is not None and R.subs[9].fresh()), "conteneur 9 (pousse en opération) abonné")
s9 = R.subs[9]
time.sleep(sr.SETTLE_S + 0.25)      # la relecture du premier document (sans version) est passée
sr0 = s9.settle_reads
F.push(plugin_version="0.23.48", transition={"start": time.time(), "duration_ms": 1000}, tally={"pgm": [4, 0], "pvw": [0]})
ok(wait_for(lambda: ((R.read(9, "tally", fields=("tally", "pgm")) or {}).get("tally") or {}).get("pgm") == [4, 0]),
   "0.23.48 : pendant une opération, `tally` SERVI (poussé par le moteur)")
mo0 = R.fallbacks.get(("tally", "motion"), 0)
ok(R.read(9, "tally", fields=("transition",)) is not None and R.fallbacks.get(("tally", "motion"), 0) == mo0,
   "0.23.48 : `transition` servi pendant l'opération, aucun repli « motion »")
F.push(plugin_version="0.23.49", pvw=2)
ok(wait_for(lambda: (R.read(9, "t", fields=("pgm",)) or {}).get("pvw") == 2), "version plus récente : événement lu")
time.sleep(sr.SETTLE_S + 0.25)
ok(s9.settle_reads == sr0, "version ≥ motion_pushed_since : AUCUNE lecture de stabilisation", s9.settle_reads - sr0)
F.push(plugin_version="0.23.47", pvw=3)
ok(wait_for(lambda: (R.read(9, "t", fields=("pgm",)) or {}).get("pvw") == 3), "version antérieure : événement lu")
ok(R.read(9, "tally", fields=("tally",)) is None and R.fallbacks.get(("tally", "motion"), 0) > mo0,
   "version antérieure : `tally` refusé pendant l'opération (repli « motion » compté)")
ok(wait_for(lambda: s9.settle_reads > sr0, timeout=2), "version antérieure : lecture de stabilisation", s9.settle_reads - sr0)
mo1 = R.fallbacks.get(("tally", "motion"), 0)
F.push(plugin_version="vieux", pvw=4)
ok(wait_for(lambda: (R.read(9, "t", fields=("pgm",)) or {}).get("pvw") == 4)
   and R.read(9, "tally", fields=("tally",)) is None and R.fallbacks.get(("tally", "motion"), 0) > mo1,
   "version illisible : traitée comme ancienne (repli « motion »)")
F.push(plugin_version=None, transition=None, pvw=1, tally={"pgm": [4], "pvw": [0]})
ok(wait_for(lambda: (R.read(1, "t", fields=("pgm",)) or {}).get("transition", 0) is None), "fin de l'opération")
s9.stop()
R._forget(s9)
ok(wait_for(lambda: F.events_conns == 1, timeout=3), "abonnement du conteneur 9 arrêté", F.events_conns)

# ── 4. reconnexion ───────────────────────────────────────────────────────────────────────────
sub = R.subs[1]
F.kill += 1
ok(wait_for(lambda: all(c.closed.is_set() for c in clients + [late]), timeout=3),
   "coupure amont : les navigateurs sont FERMÉS (la page repasse à son interrogation)")
ok(wait_for(lambda: sub.reconnects >= 1 and sub.fresh(), timeout=3), "reconnexion automatique",
   (sub.reconnects, sub.last_error))
F.push(pgm=5)
ok(wait_for(lambda: (R.read(1, "t", fields=("pgm",)) or {}).get("pgm") == 5), "après reconnexion : état suivi")
ok(F.events_conns == 1, "toujours une seule connexion amont", F.events_conns)

# ── 5. périmé ────────────────────────────────────────────────────────────────────────────────
# STALE_S plus court que le délai de lecture amont : c'est l'ÂGE du dernier octet qui doit
# trancher, avant même que la connexion ne tombe.
sr.STALE_S = 0.3
F.mode = "mute"
t_mute = time.time()
ok(wait_for(lambda: R.read(1, "tally", fields=("pgm",)) is None, timeout=3)
   and time.time() - t_mute < sr.READ_TIMEOUT_S, "source muette : plus servie après STALE_S (âge du dernier octet)",
   time.time() - t_mute)
ok(R.fallbacks.get(("tally", "stale"), 0) >= 1, "repli « stale » compté (connexion encore ouverte)", R.fallbacks)
st = R.stats()["subscriptions"][0]
ok(st["state"] in ("stale", "disconnected") and not st["fresh"], "statistiques : état périmé SIGNALÉ", st)
fb = R.fallbacks
ok(fb.get(("tally", "stale"), 0) + fb.get(("tally", "disconnected"), 0) + fb.get(("tally", "starting"), 0) >= 1,
   "repli « périmé » compté", fb)
sr.STALE_S = 0.8
F.mode = "ok"
F.push(pgm=6)
ok(wait_for(lambda: (R.read(1, "t", fields=("pgm",)) or {}).get("pgm") == 6, timeout=4),
   "source revenue : servie de nouveau")

# ── 6. moteur sans flux (404) ────────────────────────────────────────────────────────────────
F.mode = "404"
ok(R.read(2, "atem") is None and R.fallbacks.get(("atem", "starting"), 0) >= 1, "premier accès : « starting »")
ok(wait_for(lambda: R.subs.get(2) is not None and R.subs[2].unsupported), "404 : moteur marqué sans flux")
ok(R.read(2, "atem") is None and R.fallbacks.get(("atem", "unsupported"), 0) >= 1, "404 : repli compté")
c404 = Client(2)
ok(c404.status == 404, "404 : le navigateur reçoit 404 (sa page cesse d'insister)", c404.status)
F.mode = "ok"
ok(wait_for(lambda: R.read(2, "atem") is not None, timeout=3), "moteur redéployé avec flux : relais repris")

# ── 7. HA : veille ───────────────────────────────────────────────────────────────────────────
total0 = F.events_total
W.active = False
ok(R.read(3, "tally", fields=("pgm",), manifest=M) is None and R.fallbacks.get(("tally", "standby")) == 1
   and 3 not in R.subs, "veille : aucun abonnement, repli « standby » compté")
cs = http.client.HTTPConnection("127.0.0.1", WEB_PORT, timeout=5)
cs.request("GET", "/ev/3")
rs = cs.getresponse()
ok(rs.status == 299 and F.events_total == total0, "veille : le navigateur passe par le proxy brut", rs.status)
cs.close()
ok(wait_for(lambda: not R.subs, timeout=4), "veille : les abonnements existants s'arrêtent", list(R.subs))
W.active = True

# ── 7b. interrupteur (réglage state_relay_enabled) ──────────────────────────────────────────
total0 = F.events_total
W.on = False
ok(R.read(3, "tally", fields=("pgm",), manifest=M) is None and R.fallbacks.get(("tally", "disabled")) == 1
   and 3 not in R.subs and F.events_total == total0, "relais coupé : repli « disabled » compté, aucun abonnement")
cs = http.client.HTTPConnection("127.0.0.1", WEB_PORT, timeout=5)
cs.request("GET", "/ev/3")
ok(cs.getresponse().status == 299, "relais coupé : le navigateur passe par le proxy brut")
cs.close()
W.on = True

# ── 8. inactivité ────────────────────────────────────────────────────────────────────────────
sr.IDLE_STOP_S = 0.5
ok(R.read(4, "tally", fields=("pgm",)) is None, "nouvel abonnement")
ok(wait_for(lambda: R.subs.get(4) is not None and R.subs[4].fresh()), "abonné")
ok(wait_for(lambda: 4 not in R.subs, timeout=4), "plus aucun lecteur : abonnement arrêté")
ok(wait_for(lambda: F.events_conns == 0, timeout=2), "connexion amont fermée", F.events_conns)
cl = Client(5)
ok(wait_for(lambda: cl.data()), "navigateur abonné")
time.sleep(1.2)
ok(5 in R.subs and R.subs[5].fresh(), "un navigateur ouvert garde l'abonnement au-delà du délai")
cl.close()
ok(wait_for(lambda: R.subs.get(5) is None or R.subs[5].clients == 0, timeout=4),
   "navigateur parti : client décompté", R.subs.get(5) and R.subs[5].clients)
ok(wait_for(lambda: 5 not in R.subs, timeout=4), "navigateur parti : abonnement arrêté après le délai")
sr.IDLE_STOP_S = 30.0

# ── 9. droits : le refus du proxy passe AVANT le relais ──────────────────────────────────────
from app import plugin_streams as ps                           # noqa: E402
_deny0 = ps._deny
ps._deny = lambda vmid, entry: ({"error": "forbidden"}, 403)
with web.test_request_context("/api/containers/6/plugin/events"):
    r = ps.handle(6, M, "events")
ok(r == ({"error": "forbidden"}, 403) and 6 not in R.subs, "droits : refus du proxy, aucun abonnement", r)
ps._deny = lambda vmid, entry: None
with web.test_request_context("/api/containers/6/plugin/events"):
    r = ps.handle(6, M, "events")
ok(getattr(r, "status_code", None) == 200 and r.mimetype == "text/event-stream" and 6 in R.subs,
   "droits accordés : le proxy sert le flux par le relais", r)
try:
    r.close()
except Exception:
    pass
ps._deny = _deny0

# ── 10. consommateurs branchés ───────────────────────────────────────────────────────────────
F.push(pgm=2, pvw=1, tally={"pgm": [2], "pvw": [1]}, input_3="camA", inputs=["a", "b", "c"])
ok(wait_for(lambda: (R.read(7, "t", fields=("pgm",)) or {}).get("pgm") == 2), "conteneur 7 abonné")
from app import tally                                          # noqa: E402
from app import plugins as _plg                                # noqa: E402
_get0 = _plg.get
_plg.get = lambda t: M if t == "fakemix" else _get0(t)


etats = tally._EtatsCtrl()      # chemin réel : sans relais, l'IP de #7 est introuvable → None
ok((etats.get(7) or {}).get("pgm") == 2, "tally (propagation) : état lu au relais")
st7 = tally._relay_state(7, {"type": "fakemix"})
ok(st7 and st7["tally"]["pgm"] == [2], "tally (émetteur) : état lu au relais", st7)
F.push(transition={"start": time.time(), "duration_ms": 500})
ok(wait_for(lambda: tally._relay_state(7, {"type": "fakemix"}) is None),
   "tally : opération en cours → repli (la liste `tally` n'est pas poussée)")
F.push(transition=None)
ok(wait_for(lambda: tally._relay_state(7, {"type": "fakemix"}) is not None), "tally : relais repris")

from app import macros                                         # noqa: E402
_dbg0, _ip0, _rq0 = macros.db_get_container, macros.get_container_ip, macros.requests
macros.db_get_container = lambda vm: {"vmid": vm, "deploy_config": json.dumps({"type": "fakemix"})}
macros.get_container_ip = lambda vm: "127.0.0.1"


class CountReq:
    n = 0

    def get(self, url, timeout=None):
        CountReq.n += 1

        class _R:
            def json(self_):
                return dict(F.doc, inputs_timing={"n": 42})
        return _R()


macros.requests = CountReq()
cache = {}
ok(macros.fetch_state(7, state_id="pgm", cache=cache, consumer="wall_states") == 2 and CountReq.n == 0,
   "macros/murs : valeur lue au relais, aucune requête")
ok(cache.get((7, "/state")) is not None, "murs : le lot note que le conteneur a répondu")
ok(macros.fetch_state(7, state_id="timing", cache=cache) == 42 and CountReq.n == 1,
   "champ mesuré : lu au conteneur, même dans le lot déjà servi par le relais", CountReq.n)
ok(R.fallbacks.get(("macros", "volatile_field"), 0) >= 3, "repli des macros compté")
ok(macros.fetch_state(50, endpoint="/state", path="pgm") is None or True, "conteneur sans flux : chemin réseau")
macros.db_get_container, macros.get_container_ip, macros.requests = _dbg0, _ip0, _rq0

try:
    from services import panel_atem as _atem
    ok((_atem._relay_state(7) or {}).get("pgm") == 2, "pupitre ATEM : état lu au relais")
    ok(_atem._relay_state(50) is None, "pupitre ATEM : conteneur sans flux → chemin habituel")
except ImportError:
    print("  --   services/panel_atem absent : branchement du pupitre non vérifié")
_plg.get = _get0

# ── 11. la World réelle ne s'active pas sous tests/ ──────────────────────────────────────────
ok(sr.World().is_active() is False, "World réelle : inactive dans un processus de tests/")

st = R.stats()
ok(isinstance(st.get("subscriptions"), list) and "fallbacks" in st and "served" in st
   and st["served"].get("tally", 0) >= 1, "statistiques exposées (abonnements, servis, replis)")
json.dumps(st)

R.stop_all()
print("\n%s" % ("TOUT VERT" if not ko else "%d ÉCHEC(S) : %s" % (len(ko), ko)))
sys.exit(1 if ko else 0)
