#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : un redéploiement GARANTIT que le processus qui tourne exécute le nouveau script (29/10).

Mesuré sur la Z8 : #227 mixer redéployé de 0.23.41 en 0.23.42, fichier réécrit, processus d'avant
toujours là, base à 0.23.42 — et le /start suivant répondait `already_running`, compté comme un
succès (« déployé et redémarré »). Rien ne le signalait.

CAUSE (partie 1, reproduite avec le VRAI agent `script_templates/agent.py` sous TLS) : l'agent sert
une requête à la fois et fait la poignée de main TLS DANS accept(). Un /stop envoyé pendant qu'il
est occupé expire côté contrôleur AVANT la poignée de main : ses octets ne partent jamais, l'agent
ne le voit pas. L'ancien code avalait l'exception, puis /start → `already_running` (200) → succès.
En HTTP clair, le même /stop n'est que retardé : c'est pourquoi aucun banc ne l'avait montré.

Vérifie, sans réseau extérieur et sur une base jetable :
  1. vrai agent (TLS auto-signé, loopback) : la séquence de l'ANCIEN code laisse tourner l'ancien
     processus (reproduction) ; `restart_script_verified` dans la même situation lance le nouveau,
     et l'agent publie le sha256 du fichier lancé ; arrêt qui traîne (SIGTERM ignoré) idem ;
     un pair bloqué ne gèle plus l'agent au-delà de SOCKET_TIMEOUT_S ;
  2. `_deployer_script_locked` de bout en bout contre un FAUX agent : /stop perdu une fois, arrêt
     lent, processus qui ne meurt pas (alerte d'erreur, AUCUN succès, retour False), version
     publiée sur :8080 qui n'est pas la nouvelle (idem), script qui ne publie pas sa version (pas
     de fausse alarme), sha256 du nouvel agent ≠ fichier poussé, script désactivé (arrêt constaté,
     pas de démarrage), et agent d'ANCIENNE image (ni pid ni sha) ;
  3. `metrics.note_running_version` : écart signalé une fois, rien si la version n'est pas publiée,
     rien pendant une opération de cycle de vie, retour à la normale signalé.

    ./venv/bin/python tests/verif_deploy_restart_verified.py
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app.database as _database                                       # noqa: E402
_TMP = tempfile.mkdtemp(prefix="restart_verified_")
_database.DB_PATH = os.path.join(_TMP, "principale.db")                 # jamais la base de prod
_database.init_db()

import requests                                                         # noqa: E402
import urllib3                                                          # noqa: E402
from app import deploy, metrics, plugins                                # noqa: E402

urllib3.disable_warnings()
fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


# Délais de test (le comportement est le même, les attentes plus courtes).
deploy.RESTART_STOP_HTTP_TIMEOUT_S = 1.0
deploy.RESTART_STOP_CONFIRM_S = 5.0
deploy.RESTART_POLL_S = 0.05
deploy.VERSION_CONFIRM_S = 1.0


# ── 1. Vrai agent ────────────────────────────────────────────────────────────────────────────
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


RUNNER = r'''
import sys, importlib.util
spec = importlib.util.spec_from_file_location("agent", sys.argv[1])
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
a.PORT = int(sys.argv[2]); a.ALLOWED_SCRIPT_DIR = sys.argv[3]; a.TLS_DIR = sys.argv[4]
a.BIND = "127.0.0.1"; a.AGENT_TOKEN = ""
a.TLS_ALLOW_LOOPBACK = False          # le test parle en « contrôleur » depuis le loopback
a.TLS_VERIFY_CLIENT_CN = False        # pas de cert client dans ce banc
a.SOCKET_TIMEOUT_S = float(sys.argv[5])
a.serve()
'''


def _pids_of(path):
    out = []
    for e in os.listdir("/proc"):
        if e.isdigit():
            try:
                with open("/proc/%s/cmdline" % e, "rb") as f:
                    if path.encode() in f.read().split(b"\0"):
                        out.append(int(e))
            except OSError:
                pass
    return out


class RealAgent:
    def __init__(self, sock_timeout=10.0):
        self.dir = tempfile.mkdtemp(prefix="agent_", dir=_TMP)
        self.sd = os.path.join(self.dir, "script")
        os.makedirs(self.sd)
        self.tls = os.path.join(self.dir, "tls")
        os.makedirs(self.tls)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", os.path.join(self.tls, "key.pem"),
                        "-out", os.path.join(self.tls, "cert.pem"), "-days", "1",
                        "-subj", "/CN=agent"], check=True, capture_output=True)
        runner = os.path.join(self.dir, "runner.py")
        with open(runner, "w") as f:
            f.write(RUNNER)
        self.port = _free_port()
        self.main = os.path.join(self.sd, "main.py")
        self.proc = subprocess.Popen([sys.executable, runner,
                                      os.path.join(ROOT, "script_templates", "agent.py"),
                                      str(self.port), self.sd, self.tls, str(sock_timeout)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = "https://127.0.0.1:%d" % self.port
        self.sess = requests.Session()
        self.sess.verify = False
        for _ in range(100):
            try:
                self.sess.get(self.url + "/status", timeout=1)
                break
            except Exception:
                time.sleep(0.05)

    def post(self, path, timeout=5, **kw):
        return self.sess.post(self.url + path, timeout=timeout, **kw)

    def deploy(self, content):
        return self.post("/deploy", json={"path": self.main, "content": content})

    def stall(self, seconds):
        """Un pair qui ouvre une connexion et ne dit rien : l'agent reste dans accept()."""
        s = socket.create_connection(("127.0.0.1", self.port))
        threading.Timer(seconds, s.close).start()
        time.sleep(0.1)

    def bind_deploy(self):
        """Branche les helpers de deploy.py sur CET agent (même code que la production)."""
        deploy.agent_session = lambda: self.sess
        deploy.agent_url = lambda ip, path="", port=8081: self.url + path
        deploy.agent_headers = lambda vmid: {}

    def close(self):
        try:
            self.post("/stop", timeout=10)
        except Exception:
            pass
        self.proc.kill()
        self.proc.wait()
        for p in _pids_of(self.main):
            try:
                os.kill(p, 9)
            except OSError:
                pass


V1 = "import time\nPLUGIN_VERSION = '1'\nwhile True: time.sleep(1)\n"
V2 = "import time\nPLUGIN_VERSION = '2'\nwhile True: time.sleep(1)\n"
TERM_IGNORED = ("import signal, time\nsignal.signal(signal.SIGTERM, lambda *a: None)\n"
                "PLUGIN_VERSION = '%s'\nwhile True: time.sleep(1)\n")

_orig = (deploy.agent_session, deploy.agent_url, deploy.agent_headers)
if shutil.which("openssl") is None:
    print("SKIP 1. openssl absent : partie « vrai agent » sautée")
else:
    print("1. Vrai agent sous TLS")
    ag = RealAgent()
    try:
        # 1a. REPRODUCTION : la séquence de l'ancien code (stop sans vérification, start).
        ag.deploy(V1)
        ag.post("/start")
        time.sleep(0.3)
        old = _pids_of(ag.main)
        ag.deploy(V2)
        ag.stall(3)
        try:
            ag.post("/stop", timeout=1)
            stop_outcome = "answered"
        except requests.RequestException:
            stop_outcome = "timeout"
        st = ag.post("/start").json()
        time.sleep(0.2)
        check(stop_outcome == "timeout" and st.get("status") == "already_running"
              and old and set(old) == set(_pids_of(ag.main)),
              "1a. reproduit : /stop expiré pendant que l'agent est occupé, /start → already_running, "
              "ANCIEN processus toujours là (%s → %s)" % (old, _pids_of(ag.main)))

        # 1b. Même situation, chemin corrigé.
        ag.bind_deploy()
        ag.stall(3)
        t0 = time.monotonic()
        res = deploy.restart_script_verified("127.0.0.1", 9001, content=V2)
        now = _pids_of(ag.main)
        check(res["ok"] and now and not set(now) & set(old),
              "1b. restart_script_verified : ancien processus arrêté, nouveau lancé (%s, %.1f s)"
              % (res, time.monotonic() - t0))
        status = ag.sess.get(ag.url + "/status", timeout=3).json()
        check(status.get("script_sha256") == hashlib.sha256(V2.encode()).hexdigest()
              and status.get("pid") in now,
              "1c. l'agent publie pid + sha256 du fichier LANCÉ (= fichier poussé)")

        # 1d. Arrêt qui traîne : SIGTERM ignoré → SIGKILL au bout de 3 s ; le /stop expire côté
        #     contrôleur (1 s) mais l'arrêt est CONSTATÉ sur /status avant de redémarrer.
        ag.deploy(TERM_IGNORED % "3")
        deploy.restart_script_verified("127.0.0.1", 9001, content=TERM_IGNORED % "3")
        slow_old = _pids_of(ag.main)
        ag.deploy(TERM_IGNORED % "4")
        res = deploy.restart_script_verified("127.0.0.1", 9001, content=TERM_IGNORED % "4")
        now = _pids_of(ag.main)
        check(res["ok"] and now and not set(now) & set(slow_old),
              "1d. arrêt qui traîne (SIGTERM ignoré) : relancé quand même, sur le nouveau fichier")
    finally:
        ag.close()
        deploy.agent_session, deploy.agent_url, deploy.agent_headers = _orig

    # 1e. Un pair bloqué ne gèle plus l'agent au-delà de SOCKET_TIMEOUT_S.
    ag = RealAgent(sock_timeout=1.0)
    try:
        ag.stall(6)
        t0 = time.monotonic()
        try:
            ok = ag.sess.get(ag.url + "/status", timeout=4).status_code == 200
        except requests.RequestException:
            ok = False
        check(ok, "1e. pair bloqué 6 s : l'agent répond quand même en %.1f s (délai de poignée "
                  "de main borné)" % (time.monotonic() - t0))
    finally:
        ag.close()


# ── 2. _deployer_script_locked contre un FAUX agent ──────────────────────────────────────────
print("2. Déploiement de bout en bout, faux agent")
TYPE = "hello_world"
check(plugins.is_plugin(TYPE), "2.0 plugin %s présent" % TYPE)
VMID = 4242
db = _database.get_db()
db.execute("INSERT INTO containers (vmid, hostname, status, deploy_config) VALUES (?,?,?,?)",
           (VMID, "hw-test", "running",
            json.dumps({"type": TYPE, "params": {"plugin_version": "0.0.1"}})))
db.commit()


class Rep:
    def __init__(self, code, obj):
        self.status_code, self._obj, self.text = code, obj, json.dumps(obj)

    def json(self):
        return self._obj


class FakeAgent:
    """Fichier sur disque ≠ processus en mémoire : c'est tout le sujet."""

    def __init__(self, stop_mode="ok", new_agent=False, publish_version=True, wrong_sha=False,
                 version_override=None, start_dies=False, silent_8080=False):
        self.files, self.proc, self.next_pid = {}, None, 100
        self.stop_mode, self.new_agent = stop_mode, new_agent
        self.publish_version, self.wrong_sha = publish_version, wrong_sha
        self.version_override = version_override
        self.stops, self.dies_at = 0, None
        self.calls = []
        self.start_dies, self.silent_8080 = start_dies, silent_8080   # 2i / 2j (Z8, 29/10)
        self.last_exit = None

    # processus
    def alive(self):
        if self.proc and self.dies_at and time.monotonic() >= self.dies_at:
            self.proc, self.dies_at = None, None
            if self.start_dies:
                self.last_exit = 1          # sortie spontanée, comme la Z8 le 29/10
        return self.proc is not None

    def spawn(self):
        self.next_pid += 1
        c = self.files.get("/opt/script/main.py", "")
        self.proc = {"pid": self.next_pid, "content": c,
                     "sha": hashlib.sha256(("x" + c if self.wrong_sha else c).encode()).hexdigest()}

    # HTTP
    def post(self, url, json=None, timeout=None, headers=None, data=None):
        path = url.split(":8081", 1)[-1] if ":8081" in url else url
        self.calls.append(path)
        if path == "/deploy":
            self.files[json["path"]] = json["content"]
            return Rep(200, {"status": "ok"})
        if path == "/nmos":
            return Rep(200, {"status": "ok"})
        if path == "/stop":
            self.stops += 1
            if self.stop_mode == "lost_once" and self.stops == 1:
                raise requests.ReadTimeout("stop lost (TLS handshake behind a busy agent)")
            if self.stop_mode == "slow":
                if self.dies_at is None and self.proc:
                    self.dies_at = time.monotonic() + 1.2
                raise requests.ReadTimeout("stop still in progress")
            if self.stop_mode == "never":
                return Rep(200, {"status": "not_running"})     # l'agent ne le voit pas
            self.proc = None
            return Rep(200, {"status": "stopped"})
        if path == "/start":
            if self.alive():
                return Rep(200, {"status": "already_running"})
            self.spawn()
            if self.start_dies and self.stops:      # le script RELANCÉ meurt 0,3 s après son départ
                self.dies_at = time.monotonic() + 0.3
            return Rep(200, {"status": "started", "pid": self.proc["pid"]})
        return Rep(404, {"error": "not found"})

    def get(self, url, timeout=None, headers=None):
        if url.endswith(":8081/status") or url.endswith("/status"):
            run = self.alive()
            st = {"running": run, "path": "/opt/script/main.py", "last_exit": self.last_exit}
            if self.new_agent:
                st.update(pid=self.proc["pid"] if run else None,
                          script_sha256=self.proc["sha"] if run else None)
            return Rep(200, st)
        if ":8080" in url:
            if not self.alive() or self.silent_8080 or (self.start_dies and self.dies_at):
                raise requests.ConnectionError("8080 closed")
            m = re.search(r'PLUGIN_VERSION\s*=\s*"([^"]*)"', self.proc["content"])
            body = {"fps": 25.0}
            if self.publish_version:
                body["plugin_version"] = self.version_override or (m.group(1) if m else None)
            return Rep(200, body)
        return Rep(404, {})


class FakeRequestsModule:
    """`deploy.requests` (lecture :8080) redirigé vers le faux conteneur."""

    def __init__(self, fake):
        self.fake = fake
        self.RequestException = requests.RequestException

    def get(self, url, timeout=None, **kw):
        return self.fake.get(url, timeout=timeout)

    def post(self, url, **kw):
        return self.fake.post(url, **kw)


alerts = []
_patched = {}


def patch(mod, name, value):
    _patched.setdefault((mod, name), getattr(mod, name))
    setattr(mod, name, value)


from app import docker_compute, plugin_persist                         # noqa: E402
patch(docker_compute, "deploy_compute", lambda *a, **k: True)
patch(deploy, "get_container_ip", lambda vmid: "10.9.9.9")
patch(deploy, "_attendre_agent_pret", lambda *a, **k: True)
patch(deploy, "_schedule_consumer_refresh", lambda *a, **k: None)
patch(deploy, "db_add_alert", lambda key, level="info", **kw: alerts.append((key, level, kw)))
patch(plugin_persist, "after_deploy", lambda *a, **k: None)
patch(deploy, "agent_headers", lambda vmid: {})
patch(deploy, "agent_url", lambda ip, path="", port=8081: "https://%s:%d%s" % (ip, port, path))


def run(fake, enabled=True, prime=True):
    """Déploie une PREMIÈRE fois (processus ancien en place), puis redéploie : rend
    (retour, alertes du redéploiement, pid avant, processus après)."""
    patch(deploy, "agent_session", lambda: fake)
    patch(deploy, "requests", FakeRequestsModule(fake))
    _database.db_set_script_enabled(VMID, True)
    mode = fake.stop_mode
    if prime:
        fake.stop_mode = "ok"
        fake.files["/opt/script/main.py"] = 'PLUGIN_VERSION = "0.0.1"\n'
        fake.spawn()
    fake.stop_mode = mode
    before = fake.proc["pid"] if fake.proc else None
    _database.db_set_script_enabled(VMID, enabled)
    del alerts[:]
    ret = deploy._deployer_script_locked(VMID, TYPE, {"hostname": "hw-test"})
    return ret, list(alerts), before, fake.proc


WANT = plugins.resolved_version(TYPE, None)
keys = lambda al: [a[0] for a in al]                                    # noqa: E731
SUCCESS = "alert.deploy.script_deploie_redemarre"

# 2a. /stop perdu une fois → réessayé, arrêt constaté, nouveau processus.
f = FakeAgent(stop_mode="lost_once")
ret, al, before, proc = run(f)
check(ret is True and proc and proc["pid"] != before and WANT in proc["content"]
      and SUCCESS in keys(al) and f.stops >= 2,
      "2a. /stop perdu : réessayé, ancien processus remplacé par le nouveau code (%s)" % keys(al))

# 2b. Arrêt lent (le /stop expire, le processus meurt 1,2 s après).
f = FakeAgent(stop_mode="slow")
ret, al, before, proc = run(f)
check(ret is True and proc and proc["pid"] != before and SUCCESS in keys(al),
      "2b. arrêt qui traîne : attendu puis relancé")

# 2c. Processus qui ne meurt pas → erreur explicite, AUCUN succès, False.
f = FakeAgent(stop_mode="never")
ret, al, before, proc = run(f)
err = [a for a in al if a[0] == "alert.deploy.script_non_relance"]
check(ret is False and proc and proc["pid"] == before and SUCCESS not in keys(al)
      and err and err[0][1] == "error" and err[0][2].get("vmid") == VMID
      and err[0][2].get("kind") in _database.ALERT_KINDS,
      "2c. ancien processus indestructible : alerte d'erreur (vmid, kind fermé), aucun succès "
      "affiché, retour False (%s)" % keys(al))
check("/start" not in f.calls, "2c'. on ne démarre pas par-dessus un arrêt non constaté")

# 2d. Le nouveau processus publie une AUTRE version → erreur, pas de succès.
f = FakeAgent(version_override="0.0.0-old")
ret, al, before, proc = run(f)
vna = [a for a in al if a[0] == "alert.deploy.version_non_appliquee"]
check(ret is False and SUCCESS not in keys(al) and vna and vna[0][1] == "error"
      and vna[0][2]["params"].get("ver_vue") == "0.0.0-old"
      and vna[0][2]["params"].get("ver_attendue") == WANT,
      "2d. version publiée ≠ version déployée : alerte d'erreur, aucun succès")

# 2e. Script qui ne publie pas sa version → pas de verdict, pas de fausse alarme.
f = FakeAgent(publish_version=False)
ret, al, before, proc = run(f)
check(ret is True and SUCCESS in keys(al)
      and not [k for k in keys(al) if "version" in k],
      "2e. version non publiée : succès, aucune alerte de version")

# 2f. Nouvel agent : sha256 du fichier lancé ≠ fichier poussé → erreur.
f = FakeAgent(new_agent=True, wrong_sha=True)
ret, al, before, proc = run(f)
check(ret is False and "alert.deploy.script_non_relance" in keys(al) and SUCCESS not in keys(al),
      "2f. nouvel agent, sha256 différent du fichier poussé : erreur, aucun succès")
f = FakeAgent(new_agent=True)
ret, al, before, proc = run(f)
check(ret is True and SUCCESS in keys(al), "2f'. nouvel agent, sha256 conforme : succès")

# 2g. Script désactivé par l'exploitation : arrêt constaté, PAS de démarrage.
f = FakeAgent(stop_mode="lost_once")
ret, al, before, proc = run(f, enabled=False)
check(ret is True and proc is None and "/start" not in f.calls
      and "alert.deploy.script_deploie_non_demarre" in keys(al) and SUCCESS not in keys(al),
      "2g. script désactivé : ancien processus arrêté (même /stop perdu), rien de démarré")

# 2h. Agent d'ancienne image (ni pid ni sha) : chemin nominal inchangé.
f = FakeAgent()
ret, al, before, proc = run(f)
check(ret is True and proc["pid"] != before and SUCCESS in keys(al) and f.stops == 1,
      "2h. ancien agent, chemin nominal : un seul /stop, relancé, succès")

# 2i. Le script RELANCÉ s'arrête seul juste après son départ (code 1) : :8080 reste muet. Mesuré
#     sur la Z8 le 29/10 (#227, 0.23.47 → 0.23.48) : « déployé et redémarré » partait quand même,
#     à la milliseconde du constat « :8080 silent ». Erreur nommée, code de sortie, retour False.
patch(deploy, "VERSION_CONFIRM_S", 1.5)
f = FakeAgent(start_dies=True)
ret, al, before, proc = run(f)
dead = [a for a in al if a[0] == "alert.deploy.script_died_after_start"]
check(ret is False and SUCCESS not in keys(al) and dead and dead[0][1] == "error"
      and dead[0][2]["params"].get("code") == 1 and dead[0][2].get("kind") in _database.ALERT_KINDS,
      "2i. script mort juste après son départ : erreur (code 1), AUCUN succès, retour False (%s)" % keys(al))

# 2j. Processus vivant mais :8080 muet tout le délai : pas de succès affiché, avertissement.
f = FakeAgent(silent_8080=True)
ret, al, before, proc = run(f)
unc = [a for a in al if a[0] == "alert.deploy.version_unconfirmed"]
check(ret is True and SUCCESS not in keys(al) and unc and unc[0][1] == "warning",
      "2j. :8080 muet, processus vivant : avertissement « version non confirmée », pas de succès (%s)"
      % keys(al))

for (mod, name), v in _patched.items():
    setattr(mod, name, v)

# ── 3. Écart version exécutée / version en base (boucle de surveillance) ─────────────────────
print("3. metrics.note_running_version")
malerts = []
_orig_ma = metrics.db_add_alert
metrics.db_add_alert = lambda key, level="info", **kw: malerts.append((key, level, kw))
cont = {"vmid": 77, "hostname": "mix", "node_id": 3,
        "deploy_config": json.dumps({"type": "mixer", "params": {"plugin_version": "0.23.42"}})}
try:
    metrics.note_running_version(77, cont, {"fps": 50, "plugin_version": "0.23.41"})
    metrics.note_running_version(77, cont, {"fps": 50, "plugin_version": "0.23.41"})
    gap = [a for a in malerts if a[0] == "alert.deploy.version_ecart"]
    check(len(gap) == 1 and gap[0][2]["params"] == {"vmid": 77, "h": "mix", "ver_vue": "0.23.41",
                                                     "ver_base": "0.23.42"}
          and gap[0][2].get("kind") in _database.ALERT_KINDS,
          "3a. écart signalé UNE fois (front), vmid + kind fermé")
    check(metrics.version_running_cache.get(77) == "0.23.41", "3b. version exécutée exposée (cache → API)")
    metrics.note_running_version(77, cont, {"fps": None})       # :8080 muet : rien ne change
    check(len(malerts) == 1 and metrics.version_running_cache.get(77) == "0.23.41",
          "3c. :8080 muet : ni verdict ni oubli")
    metrics.note_running_version(77, cont, {"fps": 50, "plugin_version": "0.23.42"})
    check([a[0] for a in malerts][-1] == "alert.deploy.version_rattrapee", "3d. retour à la normale signalé")
    del malerts[:]
    metrics.note_running_version(78, dict(cont, vmid=78), {"fps": 50})   # ne publie pas
    check(not malerts and 78 not in metrics.version_running_cache,
          "3e. version non publiée : aucune alerte (pas de faux positif)")
    from app.vmlocks import verrou_vmid
    # Le déploiement tient le verrou dans SON fil ; la surveillance sonde depuis le sien (RLock).
    held, release = threading.Event(), threading.Event()

    def _deploy_in_flight():
        with verrou_vmid(79, op="deploy"):
            held.set()
            release.wait(10)
    th = threading.Thread(target=_deploy_in_flight)
    th.start()
    held.wait(5)
    metrics.note_running_version(79, dict(cont, vmid=79), {"fps": 50, "plugin_version": "0.23.41"})
    release.set()
    th.join()
    check(not malerts, "3f. opération de cycle de vie en vol : pas de verdict (la base précède le redémarrage)")
finally:
    metrics.db_add_alert = _orig_ma

print("\n%d échec(s)" % len(fails))
sys.exit(1 if fails else 0)
