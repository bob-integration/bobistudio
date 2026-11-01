#!/usr/bin/env python3
"""Vérifie le REGISTRE DES REQUÊTES EN COURS et le compteur de fils (app/http_connections.py).

Contrat vérifié : (1) comptage — N flux SSE ouverts en parallèle sur un VRAI Waitress à 4 fils :
le registre en voit N, le dispatcher en voit N, un flux de plus attend en FILE et le snapshot le
dit ; tout revient à zéro à la fermeture (close() = fil rendu) ; (2) contenu d'une ligne — fil,
méthode, chemin, IP, type (sse / flux / requête), conteneur visé, octets envoyés, utilisateur
résolu par l'application (nom, jamais l'id seul) ; (3) AUCUNE FUITE — ni cookie, ni jeton, ni
en-tête Authorization, ni chaîne de requête, ni segment de chemin porteur d'un jeton (/p/<jeton>) ;
(4) un fichier servi par `wsgi.file_wrapper` ne tient aucun fil et n'est pas enregistré ; (5) droits —
anonyme 401, opérateur 403, administrateur 200 sur la vraie route de main.py ; (6) alerte —
front montant après la tenue, RIEN pendant, front descendant après la tenue, rien après, écrite en
base avec kind=resource et sa clé i18n, dans les deux langues.

Base jetable, aucun réseau hors 127.0.0.1.
Mutations : `HTTP_CONNECTIONS_SRC=<copie mutée>` charge le module depuis ce fichier.

    ./venv/bin/python tests/verif_http_connections.py
"""
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)
os.chdir(_R)

from app import config, database                                # noqa: E402
_chemin = os.path.join(tempfile.mkdtemp(prefix="verif_http_connections_"), "db.sqlite")
config.DB_PATH = _chemin
database.DB_PATH = _chemin
database._tls.__dict__.pop("conn", None)
database.init_db()

import app                                                      # noqa: E402
if os.environ.get("HTTP_CONNECTIONS_SRC"):
    _spec = importlib.util.spec_from_file_location("app.http_connections", os.environ["HTTP_CONNECTIONS_SRC"])
    hc = importlib.util.module_from_spec(_spec)
    sys.modules["app.http_connections"] = hc
    _spec.loader.exec_module(hc)
    app.http_connections = hc
else:
    from app import http_connections as hc                     # noqa: E402

from flask import Flask, Response, g                            # noqa: E402

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


SECRET_TOKEN = "Zq9xSECRETtoken0123456789abcdef"
SECRET_COOKIE = "cookieSECRET_4242_value"
SECRET_QUERY = "querySECRET77"

# ── 1. un vrai Waitress à 4 fils devant une petite application ──────────────────────────────────
mini = Flask("mini")
mini.secret_key = "x"
gates = {}                                                      # vmid -> Event : libère le flux


@mini.before_request
def _fake_auth():
    # L'application résout l'utilisateur (dans le produit : current_user() → g._user).
    from flask import request
    who = request.headers.get("X-Test-User")
    if who:
        g._user = {"id": 7, "username": who, "password_hash": "HASH-SHOULD-NEVER-LEAK"}


@mini.route("/api/containers/<int:vmid>/plugin/events")
def _events(vmid):
    ev = gates.setdefault(vmid, threading.Event())

    def gen():
        yield b"data: {\"hello\": 1}\n\n"
        while not ev.wait(0.02):
            pass
        yield b"data: bye\n\n"
    return Response(gen(), content_type="text/event-stream", direct_passthrough=True)


@mini.route("/fast")
def _fast():
    return "ok"


@mini.route("/p/<token>/plugin/state")
def _public(token):
    return "public"


@mini.route("/file")
def _file():
    from flask import send_file
    return send_file(os.path.abspath(__file__), mimetype="text/plain")


hc.install(mini)

from waitress import create_server                              # noqa: E402
srv = create_server(mini, host="127.0.0.1", port=0, threads=4)
pool = hc.attach_waitress(srv, "http")
PORT = srv.effective_port
threading.Thread(target=srv.run, daemon=True).start()
time.sleep(0.2)


def open_stream(vmid, user=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    h = {"Cookie": "session=" + SECRET_COOKIE, "Authorization": "Bearer " + SECRET_TOKEN}
    if user:
        h["X-Test-User"] = user
    c.request("GET", "/api/containers/%d/plugin/events?token=%s" % (vmid, SECRET_QUERY), headers=h)
    return c


def mine():
    return [r for r in hc.snapshot()["connections"] if r["port"] == str(PORT)]


def pool_row():
    return [p for p in hc.snapshot()["pools"] if p["port"] == str(PORT)][0]


ok(pool.threads == 4, "Waitress attaché : 4 fils déclarés", pool.threads)

conns = [open_stream(1200 + i, user="alice") for i in range(3)]
resps = []
for c in conns:
    r = c.getresponse()
    r.read1(64) if hasattr(r, "read1") else None
    resps.append(r)
ok(wait_for(lambda: len(mine()) == 3), "3 flux ouverts → 3 lignes au registre", mine())
ok(wait_for(lambda: pool_row()["busy"] >= 3), "le dispatcher Waitress compte 3 fils occupés", pool_row())
rows = mine()
r0 = rows[0] if rows else {}
ok(all(r["kind"] == "sse" for r in rows), "type = sse", [r["kind"] for r in rows])
ok(sorted(r["vmid"] for r in rows) == [1200, 1201, 1202], "conteneur visé extrait du chemin",
   [r["vmid"] for r in rows])
ok(all(r["user"] == "alice" for r in rows), "utilisateur = NOM résolu par l'application", [r["user"] for r in rows])
ok(all(r["ip"] == "127.0.0.1" for r in rows), "adresse cliente", [r["ip"] for r in rows])
ok(all(r["bytes"] > 0 for r in rows), "octets envoyés comptés", [r["bytes"] for r in rows])
ok(all((r["thread"] or "").startswith("waitress-") for r in rows), "fil Waitress nommé", [r["thread"] for r in rows])
ok(all(r["duration_s"] >= 0 and r["start"] <= time.time() for r in rows), "début et durée")
ok(pool_row()["streams"] == 3, "3 flux longs sur l'écoute", pool_row())

# 4e flux : le dernier fil ; 5e : en FILE (aucun fil libre)
c4 = open_stream(1203, user="bob")
r4 = c4.getresponse()
ok(wait_for(lambda: pool_row()["busy"] == 4), "4 fils sur 4 occupés", pool_row())
c5 = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
c5.request("GET", "/fast")
ok(wait_for(lambda: pool_row()["queue"] == 1), "la 5e requête attend en FILE et le snapshot le dit", pool_row())
ok(pool_row()["max_busy"] == 4, "maximum atteint = 4", pool_row())

# ── 3. aucune fuite ────────────────────────────────────────────────────────────────────────────
dump = json.dumps(hc.snapshot())
for s in (SECRET_TOKEN, SECRET_COOKIE, SECRET_QUERY, "HASH-SHOULD-NEVER-LEAK", "Bearer", "session="):
    ok(s not in dump, "rien de « %s » dans le snapshot" % s[:12])
ok(all(set(r) <= set(hc._PUBLIC) for r in mine()), "seuls les champs de la liste blanche sortent")

# libération : tout revient à zéro
for ev in gates.values():
    ev.set()
for r in resps + [r4]:
    r.read()
r5 = c5.getresponse()
ok(r5.status == 200 and r5.read() == b"ok", "la requête en file est servie dès qu'un fil se libère")
ok(wait_for(lambda: len(mine()) == 0), "registre vide après fermeture (close() = fil rendu)", mine())
ok(wait_for(lambda: pool_row()["busy"] == 0), "dispatcher à 0", pool_row())

# chemin porteur de jeton + fichier
c6 = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
seen = []
_orig = hc.Tracker.__call__


def _spy(self, environ, start_response):
    res = _orig(self, environ, start_response)
    seen.append(json.dumps(hc.snapshot(), ensure_ascii=False))
    return res


hc.Tracker.__call__ = _spy
c6.request("GET", "/p/%s/plugin/state" % SECRET_TOKEN)
c6.getresponse().read()
c6.request("GET", "/file")
body = c6.getresponse().read()
hc.Tracker.__call__ = _orig
ok(seen and SECRET_TOKEN not in seen[0] and "/p/…/plugin/state" in seen[0],
   "segment /p/<jeton> masqué dans le chemin", seen[:1])
ok(b"verif_http_connections" in body, "fichier servi")
ok(len(seen) == 2 and '"/file"' not in seen[1], "un fichier passé à wsgi.file_wrapper n'est PAS enregistré "
   "(Waitress l'envoie depuis sa boucle, aucun fil tenu)", seen[1:] and seen[1][:300])
ok(wait_for(lambda: len(mine()) == 0), "registre vide à la fin")
ok(hc.mask_path("/api/media/assets/12/versions/3") == "/api/media/assets/12/versions/3",
   "chemin ordinaire inchangé (pas de faux masquage)")
ok(hc.mask_path("/iso/%s.iso" % SECRET_TOKEN) == "/iso/….iso", "jeton d'ISO masqué")

# ── 6. alerte : deux fronts, rien entre ─────────────────────────────────────────────────────────
w = hc.SaturationWatch(16, ratio=0.875, hold_s=10)
ok(w.high == 14, "seuil 14/16", w.high)
ev = [w.step(t, b) for t, b in ((0, 14), (5, 16), (9.9, 15))]
ok(ev == [None, None, None], "rien avant la tenue de 10 s", ev)
ok(w.step(10, 14) == "raise", "front MONTANT à 10 s au-dessus du seuil")
ev = [w.step(t, b) for t, b in ((11, 16), (12, 3), (15, 15), (16, 2), (25.9, 2))]
ok(ev == [None, None, None, None, None], "rien pendant l'épisode ni sous la tenue de retour", ev)
ok(w.step(26, 2) == "clear", "front DESCENDANT après 10 s sous le seuil")
ok([w.step(t, 0) for t in (27, 40, 80)] == [None, None, None], "rien après le retour")
w2 = hc.SaturationWatch(16, hold_s=10)
ok([w2.step(t, b) for t, b in ((0, 15), (4, 13), (6, 15), (15.9, 15))] == [None] * 4,
   "un creux remet la tenue à zéro (pas d'alerte sur des pics espacés)")

# bout en bout par sample_once → base (kind, clé i18n)
fake = hc.register_pool("65001", "test", 16, busy_fn=lambda: fake_busy[0])
fake.watch.hold_s = 1.0
fake_busy = [15]
for t in (100.0, 100.5, 101.1, 101.5, 102.0):
    hc.sample_once(now=t)
fake_busy = [1]
for t in (103.0, 103.5, 104.1, 105.0):
    hc.sample_once(now=t)
fake_busy = [16]
hc.sample_once(now=106.0)
hc.sample_once(now=106.5)
db = database.get_db()
alerts = [dict(r) for r in db.execute("SELECT * FROM alerts WHERE msg_key LIKE 'alert.resource.http_threads%' ORDER BY id")]
ok([a["msg_key"] for a in alerts] == ["alert.resource.http_threads_saturation",
                                      "alert.resource.http_threads_saturation_retour"],
   "exactement deux lignes : montée puis retour", [a["msg_key"] for a in alerts])
ok(all(a["kind"] == "resource" for a in alerts), "kind = resource (vocabulaire fermé)", alerts)
ok(alerts and alerts[0]["niveau"] == "warning" and alerts[-1]["niveau"] == "info", "niveaux warning / info",
   [a["niveau"] for a in alerts])
ok("resource" in database.ALERT_KINDS, "resource ∈ ALERT_KINDS")
ok(alerts and "15" in (alerts[0]["message"] or "") and "16" in (alerts[0]["message"] or ""),
   "message rendu avec occupation et total", alerts[:1])
ok(fake.all_busy_events == 1 and fake.max_busy == 16, "fois tous pris = 1, maximum = 16",
   (fake.all_busy_events, fake.max_busy))
for lang in ("fr", "en"):
    d = json.load(open(os.path.join(_R, "i18n", lang + ".json"), encoding="utf-8"))
    for k in ("alert.resource.http_threads_saturation", "alert.resource.http_threads_saturation_retour",
              "monitoring.tab_connections", "monitoring.conn_threads"):
        ok(k in d, "%s : clé %s" % (lang, k))
del hc._pools["65001"]

# ── 5. droits sur la vraie route ──────────────────────────────────────────────────────────────
import main as _main                                            # noqa: E402
from app.auth import hash_password                             # noqa: E402
adm = database.db_create_user("adm_conn", hash_password("Tulipe-Vent-9312"), "admin", None, None, None)
opr = database.db_create_user("op_conn", hash_password("Tulipe-Vent-9313"), "operator", None, None, None)
cli = _main.app.test_client()
r = cli.get("/api/server/connections")
ok(r.status_code == 401, "anonyme → 401", r.status_code)
with cli.session_transaction() as s:
    s["user_id"] = opr
r = cli.get("/api/server/connections")
ok(r.status_code == 403, "opérateur → 403", r.status_code)
cli2 = _main.app.test_client()
with cli2.session_transaction() as s:
    s["user_id"] = adm
r = cli2.get("/api/server/connections?token=" + SECRET_QUERY,
             headers={"Authorization": "Bearer " + SECRET_TOKEN})
ok(r.status_code == 200, "administrateur → 200", r.status_code)
j = r.get_json() or {}
txt = r.get_data(as_text=True)
cookie = cli2.get_cookie("session")
ok(SECRET_QUERY not in txt and SECRET_TOKEN not in txt and (cookie is None or cookie.value not in txt),
   "réponse sans chaîne de requête, jeton ni cookie")
# NB : le client de test de Werkzeug n'appelle pas close() sur les réponses qu'on ne ferme pas — les
# requêtes précédentes restent donc au registre ici (Waitress et cheroot, eux, ferment toujours).
moi = [c for c in j.get("connections", []) if c["path"] == "/api/server/connections" and c["status"] is None]
ok(len(moi) == 1 and moi[0]["user"] == "adm_conn" and moi[0]["kind"] == "request",
   "la requête en cours s'y voit elle-même, avec le compte de session (pas encore résolu → indice uid)", moi)

# Jeton d'API : le compte est nommé PENDANT la requête (current_user l'inscrit dès la résolution —
# un flux long n'atteint after_request qu'à sa fin), sans que le jeton apparaisse nulle part.
_tid, _jeton = database.db_api_token_creer(adm, "verif")
cli3 = _main.app.test_client()
r = cli3.get("/api/server/connections", headers={"Authorization": "Bearer " + _jeton})
txt = r.get_data(as_text=True)
moi = [c for c in (r.get_json() or {}).get("connections", []) if c["status"] is None]
ok(r.status_code == 200 and len(moi) == 1 and moi[0]["user"] == "adm_conn" and moi[0]["auth"] == "token",
   "jeton d'API : compte nommé pendant la requête, marqué « token »", moi)
ok(_jeton not in txt and _jeton[:12] not in txt, "ni le jeton ni son préfixe dans la réponse")

# Adresse cliente : celle que le MODE WEB décide de croire. Le registre est monté DANS le
# middleware des en-têtes transmis : en http, un X-Forwarded-For choisi par le client est ignoré ;
# derrière un proxy (mode proxy), il est cru.
from app import web_mode                                        # noqa: E402


def ip_vue(mode):
    av = web_mode._actif
    web_mode._actif = mode
    try:
        c = _main.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = adm
        r = c.get("/api/server/connections", headers={"X-Forwarded-For": "203.0.113.9"},
                  environ_base={"REMOTE_ADDR": "10.9.8.7"})
        rows = [x for x in (r.get_json() or {}).get("connections", []) if x["status"] is None]
        return rows[0]["ip"] if rows else None
    finally:
        web_mode._actif = av


ok(ip_vue("http") == "10.9.8.7", "mode http : X-Forwarded-For du client IGNORÉ", ip_vue("http"))
ok(ip_vue("proxy") == "203.0.113.9", "mode proxy : adresse transmise par le proxy", ip_vue("proxy"))

print()
if ko:
    print("ÉCHEC : %d point(s) — %s" % (len(ko), "; ".join(ko)))
    sys.exit(1)
print("OK — registre des connexions et fils de service")
