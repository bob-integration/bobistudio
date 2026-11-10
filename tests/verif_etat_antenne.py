#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""État d'antenne des conteneurs d'un projet (`control.onair`, 2026-11-08).
Avant : un projet ne gardait que la configuration ; un mélangeur redémarré revenait avec
l'entrée 1 au PGM. Décisions : case « Reprendre » décochée par défaut, le player reprend aussi la
lecture, reprise automatique après une relance sur plantage, capture régulière COUPÉE par défaut
avec un réglage par projet.

Vérifié hors ligne (base jetable, un faux conteneur HTTP sur 127.0.0.1, vraies routes) :
  1. la déclaration : valide, refusée si une route manque aux listes du manifeste ;
  2. capture : rangée sous le nom de l'entrée démarrée, réécrite seulement si elle change, un
     conteneur muet garde la précédente, un type sans déclaration est ignoré ;
  3. reprise au démarrage : envoyée au NOUVEAU vmid, réessayée tant que le script ne répond
     pas, jamais réessayée après un refus (4xx) ;
  4. reprise après relance : seulement d'une capture prise depuis le démarrage du projet ;
  5. capture régulière : rien par défaut, au pas choisi sinon, jamais sur un projet arrêté ;
  6. « Arrêter » capture avant de supprimer, « Démarrer » ne reprend que si la case est cochée ;
  7. routes : lecture, réglage du pas (valeurs refusées), « Enregistrer maintenant », droits ;
  8. Hello-World déclare et sert /onair (exemple de l'aide) ; la fenêtre et l'aide en parlent.

⚠ Garde MUTÉE à l'écriture.
"""
import json
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
os.chdir(RACINE)

from app import config, database                  # noqa: E402
_TMP = tempfile.mkdtemp(prefix="etat_antenne_db_")
_DBF = os.path.join(_TMP, "t.db")
config.DB_PATH = _DBF
database.DB_PATH = _DBF
database._tls.__dict__.pop("conn", None)
database.init_db()
assert database.DB_PATH == _DBF

ECHECS = []


def ok(cond, libelle, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond else "  [%s]" % (detail,)))
    if not cond:
        ECHECS.append(libelle)


from app import plugins, project_onair as po      # noqa: E402
plugins._scan() if not plugins.REGISTRY else None

# ── Faux conteneurs : /<vmid>/onair ────────────────────────────────────────────────
STATE = {}          # vmid -> document servi
CALLS = []          # (vmid, method)
BEHAVIOUR = {}      # vmid -> list of status codes to answer first (then 200)


class Fake(BaseHTTPRequestHandler):
    def _vmid(self):
        return int(self.path.split("/")[1])

    def _answer(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        v = self._vmid()
        CALLS.append((v, "GET"))
        if v not in STATE:
            return self._answer(500, {"error": "down"})
        self._answer(200, STATE[v])

    def do_POST(self):
        v = self._vmid()
        CALLS.append((v, "POST"))
        doc = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        seq = BEHAVIOUR.get(v) or []
        if seq:
            code = seq.pop(0)
            return self._answer(code, {"error": "refused" if code < 500 else "not ready"})
        STATE[v] = doc
        self._answer(200, {"ok": True})

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
po._url = lambda vmid, path: "http://127.0.0.1:%d/%d%s" % (PORT, vmid, path)
FAST = (0.01, 0.01, 0.01, 0.01, 0.01)

print("1. Déclaration")
hw = plugins.get("hello_world") or {}
ok(po.spec(hw) == {"read": "/onair", "restore": "/onair"}, "Hello-World déclare control.onair", po.spec(hw))
bad = {"control": {"endpoints": ["/x"], "read_endpoints": [], "onair": {"read": "/onair", "restore": "/onair"}}}
errs = po.validate_spec(bad)
ok(len(errs) == 2 and po.spec(bad) is None, "routes absentes des listes : deux erreurs, pas de spec", errs)
ok(po.validate_spec({"control": {"onair": "/onair"}}) == ["control.onair must be an object"], "pas un objet : refusé")
ok(po.validate_spec({"control": {}}) == [], "absent : rien à redire")
src = open("app/plugins.py").read()
ok("_onair_errs(manifest)" in src and "control.onair invalide" in src, "le scan refuse une déclaration fausse")

print("2. Capture")
db = database.get_db()


def add_container(vmid, host, type_, pid=None):
    db.execute("INSERT INTO containers (vmid, hostname, deploy_config, project_id) VALUES (?,?,?,?)",
               (vmid, host, json.dumps({"type": type_, "params": {}}), pid))
    db.commit()


add_container(501, "mix", "hello_world")
add_container(502, "cam", "nope_type")           # pas de déclaration
add_container(503, "gfx", "hello_world")         # muet
PID = database.db_save_project("Atelier", [501, 502, 503])
db.execute("UPDATE containers SET project_id=? WHERE vmid IN (501,502,503)", (PID,))
db.execute("UPDATE projects SET state='active' WHERE id=?", (PID,))
db.commit()
KEY = po._key("mix", "Atelier")
ok(KEY == "Atelier-mix", "clé = nom de l'entrée démarrée", KEY)
STATE[501] = {"version": 1, "fond": "nuit", "texte": "A"}
r = po.capture(PID, alert=False)
st = po.stored(PID)
ok(r["read"] == 1 and r["changed"] == 1 and st.get(KEY, {}).get("doc") == STATE[501], "capture rangée", (r, st))
ok(not any(v == 502 for v, _ in CALLS), "un type sans déclaration n'est pas interrogé")
ok([f["entry"] for f in r["failed"]] == ["Atelier-gfx"], "le conteneur muet est signalé", r["failed"])
first_at = st[KEY]["captured_at"]
db.execute("UPDATE project_onair SET captured_at='2000-01-01T00:00:00' WHERE entry=?", (KEY,))
db.commit()
r = po.capture(PID, alert=False)
ok(r["changed"] == 0 and po.stored(PID)[KEY]["captured_at"] == "2000-01-01T00:00:00",
   "même état : rien de réécrit", r)
STATE[503] = {"version": 1, "fond": "foret"}
po.capture(PID, alert=False)
del STATE[503]
po.capture(PID, alert=False)
ok(po.stored(PID).get("Atelier-gfx", {}).get("doc") == {"version": 1, "fond": "foret"},
   "un conteneur devenu muet garde sa capture précédente")
STATE[501] = {"version": 1, "fond": "bordeaux", "texte": "B"}
r = po.capture(PID, alert=False)
ok(r["changed"] == 1 and po.stored(PID)[KEY]["doc"]["fond"] == "bordeaux", "état changé : réécrit", r)
sm = po.summary(PID)
ok(sm["supported"] == 2 and sm["saved"] == 2 and sm["interval_s"] == 0, "résumé : 2 déclarés, 2 enregistrés, pas régulier", sm)

print("3. Reprise au démarrage")
entries = database.db_get_project(PID)["snapshot"]
add_container(601, "Atelier-mix", "hello_world", PID)
add_container(603, "Atelier-gfx", "hello_world", PID)
BEHAVIOUR[601] = [503, 503]                      # le script n'est pas encore prêt
BEHAVIOUR[603] = [400]                           # refusé : ne pas réessayer
CALLS.clear()
lines = []
out = po.restore_after_start(PID, entries, {501: 601, 502: 602, 503: 603}, progress=lines.append, delays=FAST)
ok(out.get(KEY) == "ok" and STATE.get(601, {}).get("fond") == "bordeaux", "envoyé au nouveau vmid après deux refus « pas prêt »", out)
ok(CALLS.count((601, "POST")) == 3, "réessayé tant que pas prêt", CALLS)
ok(out.get("Atelier-gfx", "").startswith("refused") and CALLS.count((603, "POST")) == 1, "refus 4xx : un seul essai", (out, CALLS))
ok(not any(v in (501, 503) for v, m in CALLS if m == "POST"), "rien d'envoyé aux anciens vmid")
ok(any("état d'antenne repris" in x for x in lines), "le journal du démarrage le dit", lines)
alerts = [a for a in database.db_get_alerts() if "onair" in (a.get("msg_key") or "")]
ok(alerts and alerts[0]["msg_key"] == "alert.projet.onair_restored_partial", "alerte : reprise partielle", alerts[:1])

print("4. Reprise après relance automatique")
db.execute("UPDATE containers SET project_id=? WHERE vmid IN (601,603)", (PID,))
snap = database.db_get_project(PID)["snapshot"]
for e in snap:
    e["vmid"] = {501: 601, 502: 602, 503: 603}[e["vmid"]]
database.db_update_project_snapshot(PID, snap)
later = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
db.execute("UPDATE projects SET started_at=? WHERE id=?", (later, PID))
db.commit()
STATE[601] = {"version": 1, "fond": "nuit"}
ok(po.after_auto_restart(601, delays=FAST) is None and STATE[601]["fond"] == "nuit",
   "capture d'avant le démarrage : rien de repris")
db.execute("UPDATE projects SET started_at=? WHERE id=?", ("2000-01-01T00:00:00", PID))
db.commit()
ok(po.after_auto_restart(601, delays=FAST) == "ok" and STATE[601]["fond"] == "bordeaux",
   "capture du démarrage en cours : reprise")
add_container(700, "seul", "hello_world")
ok(po.after_auto_restart(700, delays=FAST) is None, "conteneur hors projet : rien")
msrc = open("app/metrics.py").read()
blk = msrc[msrc.index('if prev_run is False:\n                    _script_running_since'):]
blk = blk[:blk.index("_prev_script_running[vmid] = running")]
ok(re.search(r"if vmid in _script_restart_count:\s+import threading as _th\s+from \. import project_onair as _onair\s+"
             r"_th\.Thread\(target=_onair\.after_auto_restart", blk) is not None,
   "la surveillance reprend l'état au retour d'un script qu'ELLE a relancé")

print("5. Capture régulière")
seen = []
real_capture = po.capture
po.capture = lambda pid, alert=True: seen.append(pid) or {"read": 0, "changed": 0, "failed": []}
po._last.clear()
po._tick(now=1000.0)
ok(seen == [], "défaut : aucune capture régulière", seen)
try:
    po.set_interval(PID, 7)
    ok(False, "un pas hors liste est refusé")
except ValueError:
    ok(True, "un pas hors liste est refusé")
po.set_interval(PID, 30)
po._tick(now=1000.0)
po._tick(now=1010.0)
po._tick(now=1031.0)
ok(seen == [PID, PID], "au pas choisi (30 s) et pas avant", seen)
db.execute("UPDATE projects SET state='saved' WHERE id=?", (PID,))
db.commit()
po._tick(now=2000.0)
ok(seen == [PID, PID], "projet arrêté : rien", seen)
po.capture = real_capture

print("6. Arrêter / Démarrer")
from app import projects                          # noqa: E402
calls = []
po.capture = lambda pid, alert=True: calls.append(("capture", pid, database.db_get_project(pid)["state"]))
projects.detruire_container = lambda vmid: calls.append(("destroy", vmid))
db.execute("UPDATE projects SET state='active' WHERE id=?", (PID,))
db.commit()
projects.detruire_containers_projet(PID)
ok(calls and calls[0][0] == "capture" and all(c[0] == "destroy" for c in calls[1:]) and len(calls) > 1,
   "Arrêter : capture AVANT de supprimer", calls)
po.capture = real_capture
psrc = open("app/projects.py").read()
ok(re.search(r"if onair and vmid_map:\s+try:\s+from \. import project_onair\s+project_onair\.restore_after_start\(", psrc),
   "Démarrer : reprise seulement si la case est cochée")
ok("placement, onair)" in psrc and "onair=onair)" in open("app/routes/projects_api.py").read(), "le choix descend jusqu'à la restauration")

print("7. Routes")
from app.auth import hash_password                # noqa: E402
uid = database.db_create_user("admin_t", hash_password("x"), "admin")
vid_ = database.db_create_user("lecteur_t", hash_password("x"), "viewer")
import main as app_main                           # noqa: E402
app = app_main.app
app.config["TESTING"] = True
cli_v = app.test_client()
with cli_v.session_transaction() as s:
    s["user_id"] = vid_
    s["username"] = "lecteur_t"
r = cli_v.patch("/api/projects/%d/onair" % PID, json={"capture_s": 60})
ok(r.status_code == 403, "un lecteur ne règle pas le pas", r.status_code)
cli = app.test_client()
with cli.session_transaction() as s:
    s["user_id"] = uid
    s["username"] = "admin_t"
r = cli.get("/api/projects/%d/onair" % PID)
ok(r.status_code == 200 and r.get_json()["choices"] == list(po.CAPTURE_CHOICES_S), "lecture", r.get_json())
r = cli.patch("/api/projects/%d/onair" % PID, json={"capture_s": 45})
ok(r.status_code == 400, "pas invalide : 400", r.status_code)
r = cli.patch("/api/projects/%d/onair" % PID, json={"capture_s": 600})
ok(r.status_code == 200 and r.get_json()["interval_s"] == 600, "pas réglé", r.get_json())
mod_before = database.db_get_project(PID).get("modified_at")
db.execute("UPDATE projects SET modified_at=NULL WHERE id=?", (PID,))
db.commit()
STATE[601] = {"version": 1, "fond": "graphite"}
r = cli.post("/api/projects/%d/onair/capture" % PID)
j = r.get_json()
ok(r.status_code == 200 and j["read"] >= 1 and j["summary"]["saved"] >= 1, "Enregistrer maintenant", j)
ok(database.db_get_project(PID).get("modified_at") is None, "capturer ne compte pas comme une modification du projet")
r = cli.get("/api/projects/%d/restore_preview" % PID)
ok(r.status_code == 200 and (r.get_json().get("onair") or {}).get("saved", 0) >= 1, "la fenêtre Démarrer reçoit le résumé", r.get_json().get("onair"))
asrc = open("app/routes/projects_api.py").read()
ok('onair      = bool(data.get("onair"))' in asrc, "la route de démarrage lit la case")

print("8. Hello-World, fenêtre, aide")
from app.plugins import render_script             # noqa: E402
txt = render_script("hello_world", {"texte": "x"}, None) or ""
compile(txt, "hello_world", "exec")
ok('if chemin == "/onair":' in txt and txt.count('if chemin == "/onair":') == 2, "script : GET et POST /onair")
ok('data.get("version") != ONAIR_VERSION' in txt, "script : refuse une version inconnue")
# At least the version that brought /onair: an exact match broke on every later bump.
from app import version as _ver                   # noqa: E402
ok(_ver.au_moins(hw.get("version") or "", "0.10.5") and _ver.comparable(hw.get("version") or "", "0.10.5"),
   "Hello-World porte /onair (>= 0.10.5)", hw.get("version"))
html = open("templates/projects.html").read()
m = re.search(r'<input type="checkbox" data-onair[^>]*>', html)
ok(m and "checked" not in m.group(0), "case « Reprendre » décochée par défaut", m and m.group(0))
ok("_restoreRun(pid, name, null, move, placement, onair)" in html, "la case part avec le démarrage")
js = open("static/scripts.js").read()
ok("onair: !!onair" in js and "_restoreCtx.placement, _restoreCtx.onair)" in js, "le corps POST et la reprise des échecs portent la case")
for lang in ("fr", "en"):
    d = json.load(open("i18n/%s.json" % lang))
    keys = set(re.findall(r"t\('(projects\.(?:onair|start_onair)[a-z_]*)'", html))
    keys |= {"alert.projet.onair_" + k for k in ("capture_failed", "restored", "restored_partial",
                                                 "after_restart", "after_restart_failed")}
    miss = sorted(k for k in keys if k not in d)
    ok(len(keys) > 15 and not miss, "textes %s présents" % lang, miss)
aide = open("templates/aide.html").read()
art = aide[aide.index('<template id="wiki-art-dev-plugin">'):]
art = art[:art.index("</template>")]
ok('class="wiki-doc-content" data-doc="authoring"' in art,
   "aide#dev-plugin affiche AUTHORING (une seule source, plus de copie à la main)")
for lang, fich, mot in (("fr", "plugins/AUTHORING.fr.md", "État d'antenne"), ("en", "plugins/AUTHORING.md", "On-air state")):
    db.execute("UPDATE users SET lang=? WHERE id=?", (lang, uid))
    db.commit()
    r = cli.get("/api/doc/authoring")
    j = r.get_json() or {}
    ok(r.status_code == 200 and j.get("fichier") == fich and mot in j.get("html", ""),
       "/api/doc/authoring en %s : %s" % (lang, fich), (r.status_code, j.get("fichier")))
for f in ("plugins/AUTHORING.md", "plugins/AUTHORING.fr.md"):
    ok('"onair": { "read": "/onair", "restore": "/onair" }' in open(f).read(), "aide : %s" % f)

print()
if ECHECS:
    print("ÉCHECS (%d) : %s" % (len(ECHECS), "; ".join(ECHECS)))
    sys.exit(1)
print("OK")
