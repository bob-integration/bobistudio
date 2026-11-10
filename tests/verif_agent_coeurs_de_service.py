#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent-nœud 0.31.0 : rien de ce que lance l'agent ne tombe sur un cœur dédié (2026-11-08).

Le constat (Z8, ftrace sur le CPU 9 de la console audio à 48/72, 45 min) : les 52 blocages > 1,6 ms du
fil temps réel ont TOUS un intrus — 36 × `runc` (le `docker exec` de la sonde du chien de garde, qui
rejoint le cgroup donc le cpuset du conteneur) et 16 × `nvidia-smi` (relevé GPU, lancé par l'agent sur
n'importe quel processeur). D'où :

  1. la sonde entre dans l'ESPACE RÉSEAU du conteneur seul (`nsenter -n`), jeton et TLS lus par
     /proc/<pid>/ ; `docker exec` reste le repli, et il est DIT ;
  2. les cœurs de service : l'agent pose une frontière sur TOUS ses fils (les enfants en héritent),
     la garde sur disque, la remonte dans /v1/health ;
  3. le contrôleur pousse `core_pool.cpuset_auxiliaire` quand elle diffère, jamais à un vieil agent ;
  4. branchements (tour du chien de garde, démarrage, santé, route).

MUTATIONS (le test DOIT échouer) — HK_MUT=<nom> :
    exec        la sonde passe toujours par docker exec
    onethread   la frontière n'est posée que sur le fil appelant
    nopush      le contrôleur ne pousse jamais
    samepush    le contrôleur pousse même quand l'agent a déjà la bonne frontière
    aux         la frontière de l'agent = celle d'un conteneur auxiliaire (le seul pool partagé)
    nocache     la version de Docker est redemandée à chaque relevé
    nosvc       docker.service / containerd.service ne sont pas bornés

    ./venv/bin/python tests/verif_agent_coeurs_de_service.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
MUT = os.environ.get("HK_MUT", "")
if MUT not in ("", "exec", "onethread", "nopush", "samepush", "aux", "nocache", "nosvc"):
    print(" ÉCHEC mutation inconnue %r" % MUT)
    sys.exit(2)
echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


def charger_agent():
    chemin = os.path.join(RACINE, "node_agent", "agent.py")
    spec = importlib.util.spec_from_file_location("bobi_node_agent_sous_test", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = charger_agent()
SRC = open(os.path.join(RACINE, "node_agent", "agent.py"), encoding="utf-8").read()
if MUT == "exec":
    A.shutil.which = lambda name: None
if MUT == "nocache":
    A.DOCKER_INFO_TTL_S = 0
if MUT == "nosvc":
    A._hk_services = lambda _run=None: {}
if MUT == "onethread":
    _apply = A._hk_apply
    A._hk_apply = lambda spec, _tasks=None, _set=None, _online=None: _apply(
        spec, _tasks=lambda: [0], _set=_set, _online=_online)

# Un processus « conteneur » factice : son environnement d'init porte le jeton, comme un docker run -e.
fils = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                        env=dict(os.environ, MXL_AGENT_TOKEN="jeton-de-test"))
try:
    # ================================================================ 1. la sonde
    print("── 1. sonde du chien de garde ──────────────────────────────────────────────────")
    argv, env = A._wd_argv("bobi-x", "/status", "GET", fils.pid)
    verifie(argv[:4] == ["nsenter", "-t", str(fils.pid), "-n"] and "docker" not in argv,
            "1a. ★ avec un PID : nsenter dans l'espace RÉSEAU seul, pas de docker exec (pas de runc)", argv)
    verifie(env and env.get("WD_ROOT") == "/proc/%d/root" % fils.pid and env.get("WD_PATH") == "/status",
            "1b. le TLS est lu dans la vue du conteneur (/proc/<pid>/root)", env and env.get("WD_ROOT"))
    verifie(env and env.get("WD_TOKEN") == "jeton-de-test",
            "1c. le jeton vient de l'environnement d'init du conteneur", env and env.get("WD_TOKEN"))

    notes = []
    vrai_note, vrai_which = A._wd_note, A.shutil.which
    A._wd_note = notes.append
    A.shutil.which = lambda name: None
    A._WD_EXEC_FALLBACK_NOTED.clear()
    argv2, _ = A._wd_argv("bobi-y", "/status", "GET", fils.pid)
    A._wd_argv("bobi-y", "/status", "GET", fils.pid)
    A.shutil.which = vrai_which
    if MUT == "exec":
        A.shutil.which = lambda name: None
    A._wd_note = vrai_note
    verifie(argv2[:2] == ["docker", "exec"] and len(notes) == 1 and "docker exec" in notes[0],
            "1d. sans nsenter : repli docker exec, DIT une fois (pas à chaque tour)", (argv2[:2], notes))

    vrai_docker = A.docker
    A.docker = lambda *a, **k: (1, "/bobi-a\t1234\n/bobi-b\t0\nbobi-c\t77\n", "Error: No such object: bobi-d")
    pids = A._wd_pids(["bobi-a", "bobi-b", "bobi-c", "bobi-d"])
    A.docker = vrai_docker
    verifie(pids == {"bobi-a": 1234, "bobi-c": 77},
            "1e. PID relus en UN docker inspect ; arrêté (0) ou absent → pas de PID, les autres gardés", pids)

    # Sonde réelle : un :8081 factice sur la boucle locale, la sonde entre dans l'espace réseau du « conteneur »
    # (ici le nôtre) et doit y présenter le jeton du conteneur.
    vu = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            vu["path"], vu["jeton"] = self.path, self.headers.get("X-MXL-Agent-Token")
            corps = json.dumps({"running": True, "path": "/opt/s.py"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    vraie_sonde = A._WD_SONDE
    A._WD_SONDE = vraie_sonde.replace("127.0.0.1:8081", "127.0.0.1:%d" % srv.server_port)
    if os.geteuid() == 0 and A.shutil.which("nsenter"):
        code, data = A._wd_sonder("bobi-x", "/status", pid=fils.pid)
        verifie(code == 200 and data.get("running") is True and vu.get("jeton") == "jeton-de-test",
                "1f. ★ vraie sonde par nsenter : 200, état lu, jeton du conteneur présenté", (code, data, vu))
    else:
        print("  --  1f. pas root ou pas de nsenter ici : vraie sonde non essayée")
    A._WD_SONDE = vraie_sonde
    srv.shutdown()

    # Le tour du chien de garde passe le PID à la sonde, et ne relit les PID qu'UNE fois.
    appels, inspects = [], []
    sauve = (A._list_containers, A._wd_reseau_cached, A._wd_pids, A._wd_sonder, A._wd_controleur_silencieux)
    A._list_containers = lambda: [{"name": "bobi-a", "status": "Up 1h"}, {"name": "bobi-b", "status": "Up 1h"},
                                  {"name": "bobi-eng", "status": "Up 1h"}]
    A._wd_reseau_cached = lambda n: "host" if n == "bobi-eng" else "bobi-mv"
    A._wd_pids = lambda names: inspects.append(list(names)) or {"bobi-a": 11, "bobi-b": 22}
    A._wd_sonder = lambda name, path, method="GET", timeout=15, pid=None: appels.append((name, pid)) or (
        200, {"running": True})
    A._wd_controleur_silencieux = lambda: False
    A._WD_STATE.clear()
    A._wd_tick()
    (A._list_containers, A._wd_reseau_cached, A._wd_pids, A._wd_sonder, A._wd_controleur_silencieux) = sauve
    verifie(appels == [("bobi-a", 11), ("bobi-b", 22)] and inspects == [["bobi-a", "bobi-b"]],
            "1g. tour : un seul relevé de PID, chaque sonde reçoit le sien, le moteur 2110 reste hors périmètre",
            (appels, inspects))

    # ================================================================ 2. cœurs de service
    print("── 2. cœurs de service de l'agent ──────────────────────────────────────────────")
    poses = []
    r = A._hk_apply("2-5,9", _tasks=lambda: [101, 102, 103], _set=lambda t, c: poses.append((t, sorted(c))),
                    _online=lambda: set(range(8)))
    verifie(r == "2-5" and sorted(t for t, _ in poses) == [101, 102, 103] and all(c == [2, 3, 4, 5] for _, c in poses),
            "2a. ★ la frontière est posée sur TOUS les fils de l'agent, ∩ processeurs en ligne", (r, poses))
    poses.clear()
    r = A._hk_apply("", _tasks=lambda: [7], _set=lambda t, c: poses.append((t, sorted(c))), _online=lambda: {0, 1, 2})
    verifie(r == "0-2" and poses == [(7, [0, 1, 2])], "2b. \"\" = plus de frontière (tous les processeurs en ligne)",
            (r, poses))
    for spec, motif in (("3-x", "malformed"), ("40-41", "no online CPU")):
        poses.clear()
        avant = dict(A._HK)
        r = A._hk_apply(spec, _tasks=lambda: [7], _set=lambda t, c: poses.append(t), _online=lambda: {0, 1})
        verifie(r is None and not poses and motif in (A._HK["error"] or "") and A._HK["cpus"] == avant["cpus"],
                "2c. refus (%s) : rien n'est touché, la raison est dite" % motif, (r, poses, A._HK))

    # The real-process checks below call the real _hk_set / _hk_load: they must NEVER run systemctl on
    # this machine (it would confine the Docker daemons of whatever host runs the suite).
    vrais_services, systemctl_reel = A._hk_services, []
    A._hk_services = lambda _run=None: systemctl_reel.append(1) or {}
    if hasattr(os, "sched_getaffinity") and len(os.sched_getaffinity(0)) >= 2:
        mien = sorted(os.sched_getaffinity(0))
        d = tempfile.mkdtemp()
        A.HK_PATH = os.path.join(d, "hk.json")
        pret, fini, vu_fil = threading.Event(), threading.Event(), {}

        def autre_fil():
            pret.wait(5)
            vu_fil["aff"] = sorted(os.sched_getaffinity(0))
            vu_fil["enfant"] = subprocess.run(["sh", "-c", "grep Cpus_allowed_list /proc/self/status"],
                                              capture_output=True, text=True).stdout.split()[-1]
            fini.set()
        th = threading.Thread(target=autre_fil, daemon=True)
        th.start()
        ok = A._hk_set(str(mien[0]))
        pret.set()
        fini.wait(10)
        verifie(ok and vu_fil.get("aff") == [mien[0]] and vu_fil.get("enfant") == str(mien[0]),
                "2d. ★ vrai processus : un AUTRE fil de l'agent et la commande qu'il lance restent sur la frontière",
                (ok, vu_fil, A._HK))
        verifie(json.load(open(A.HK_PATH)) == {"cpus": str(mien[0])}, "2e. la frontière est gardée sur disque")
        A._HK.update(cpus="", applied="", error=None)
        A._hk_apply("")
        A._hk_load()
        verifie(A._HK["cpus"] == str(mien[0]) and sorted(os.sched_getaffinity(0)) == [mien[0]],
                "2f. au démarrage, la frontière gardée est reposée", A._HK)
        A._hk_apply("")
        os.sched_setaffinity(0, set(mien))
    else:
        print("  --  2d-2f. une seule CPU ici : vrai processus non essayé")

    A._hk_services = vrais_services
    verifie(MUT == "nosvc" or len(systemctl_reel) >= 2,
            "2f bis. _hk_set et _hk_load bornent les services (doublure : jamais de vrai systemctl ici)", systemctl_reel)

    # Les démons Docker : dockerd et les shims lancent runc depuis LEUR affinité.
    vus = []

    def faux_run(argv, timeout=60, input_bytes=None, env=None):
        vus.append(argv)
        if argv[:2] == ["systemctl", "show"]:
            return (0, "loaded\n" if argv[-1] == "docker.service" else "not-found\n", "")
        return (0, "", "")
    A._HK.update(applied="4-5,7")
    r = A._hk_services(_run=faux_run) if MUT != "nosvc" else {}
    poses_svc = [a for a in vus if a[:2] == ["systemctl", "set-property"]]
    verifie(poses_svc == [["systemctl", "set-property", "--runtime", "docker.service", "AllowedCPUs=4-5,7"]]
            and r.get("containerd.service") == "absent",
            "2g. ★ dockerd (et containerd s'il existe) bornés à la frontière ; un service absent est sauté", (vus, r))
    vus.clear()
    A._HK.update(applied="0-11")
    A._hk_services(_run=faux_run) if MUT != "nosvc" else None
    verifie(any(a[-1] == "AllowedCPUs=0-11" for a in vus),
            "2h. plus de frontière : la liste COMPLÈTE est écrite (un AllowedCPUs vide ne rétablit rien)", vus)
    verifie('_hk_services()' in SRC[SRC.index("def _hk_set"):SRC.index("def _hk_load")]
            and '_hk_services()' in SRC[SRC.index("def _hk_load"):SRC.index("def _hk_load") + 900],
            "2i. les services sont bornés à chaque poussée ET au démarrage")

    # La version de Docker : chaque `docker version` fait lancer `runc --version` à dockerd.
    appels_v = []
    vrai_d = A.docker
    A.docker = lambda *a, **k: appels_v.append(a) or (0, "27.5.1\n", "")
    A._DOCKER_INFO.update(ts=0.0, val=None)
    v1, v2 = A._docker_info(), A._docker_info()
    A.docker = lambda *a, **k: (1, "", "down")
    A._DOCKER_INFO.update(ts=0.0, val=None)
    v3 = A._docker_info()
    A.docker = vrai_d
    verifie(len(appels_v) == 1 and v1 == v2 == {"ok": True, "version": "27.5.1"} and v3["ok"] is False
            and A._DOCKER_INFO["val"] is None,
            "2j. ★ version de Docker relue au plus toutes les 10 min ; un échec n'est pas gardé", (appels_v, v1, v2, v3))
finally:
    fils.kill()

# ================================================================ 3. contrôleur
print("── 3. le contrôleur pousse la frontière ────────────────────────────────────────")
from app import core_pool, node_driver, node_health  # noqa: E402

# La frontière : tous les processeurs − réservés − fermés par le pool − isolés (cas dl360-1 du 08/11 en petit).
#   12 CPU ; isolés 1-3 ; pool 6-11 ; réservés : moteur 0-3, console 6 ; fermés : 6 et son jumeau 9.
core_pool.core_map_cached = lambda nid: {c: c % 6 for c in range(12)}
core_pool.isolated_cached = lambda nid: {1, 2, 3}
import app.database as _db  # noqa: E402
_db.db_get_node = lambda nid: {"id": nid, "compute_cpuset": "6-11"}
core_pool.partage_disponible = lambda nid, pool=None, core_of=None, **k: ([7, 8, 10, 11], {0})
core_pool.allocations_by_vmid = lambda nid: {619: [0, 1, 2, 3], 220: [6]}
core_pool.cpuset_auxiliaire = lambda nid: "7-8,10-11"
if MUT == "aux":
    node_health.housekeeping_cpus = lambda nid: core_pool.cpuset_auxiliaire(nid)
hk = node_health.housekeeping_cpus(34)
verifie(hk == "4-5,7-8,10-11",
        "3e. ★ frontière de l'agent : les cœurs de l'HÔTE en plus du partagé (4-5), sans réservés, jumeau ni isolés", hk)
core_pool.isolated_cached = lambda nid: None
hk = node_health.housekeeping_cpus(34) if MUT != "aux" else "x"
verifie(hk == "7-8,10-11", "3f. bande isolée illisible : repli sur la frontière d'auxiliaire, jamais « aucune »", hk)

poussees = []
node_health.housekeeping_cpus = lambda nid: {1: "4-5,10", 2: ""}.get(nid, "")
node_driver.set_housekeeping = lambda node, cpus: poussees.append((node["id"], cpus)) or (
    True, {"ok": True, "cpus": cpus, "applied": cpus, "error": None})
if MUT == "nopush":
    node_health._push_housekeeping = lambda node, snap: None
elif MUT == "samepush":
    _vrai = node_health._push_housekeeping

    def _toujours(node, snap):
        if isinstance(snap.get("housekeeping"), dict):
            snap["housekeeping"]["cpus"] = "?"
        _vrai(node, snap)
    node_health._push_housekeeping = _toujours

n1 = {"id": 1, "agent_url": "https://n1:9100"}
snap = {"housekeeping": {"cpus": "", "applied": "0-51", "error": None}}
node_health._push_housekeeping(n1, snap)
verifie(poussees == [(1, "4-5,10")] and snap["housekeeping"]["cpus"] == "4-5,10",
        "3a. ★ frontière différente : poussée, et le relevé dit la nouvelle", (poussees, snap))
poussees.clear()
node_health._push_housekeeping(n1, {"housekeeping": {"cpus": "4-5,10", "applied": "4-5,10", "error": None}})
verifie(poussees == [], "3b. déjà la bonne : rien n'est envoyé (un nœud stable ne coûte rien)", poussees)
node_health._push_housekeeping(n1, {"agent_version": "0.30.2"})
verifie(poussees == [], "3c. vieil agent (pas de `housekeeping` dans sa santé) : laissé tranquille", poussees)
node_health._push_housekeeping({"id": 2, "agent_url": "https://n2:9100"}, {"housekeeping": {"cpus": ""}})
verifie(poussees == [], "3d. nœud sans pool déclaré : rien à poser", poussees)

# ================================================================ 4. branchements
print("── 4. branchements ─────────────────────────────────────────────────────────────")
NH = open(os.path.join(RACINE, "app", "node_health.py"), encoding="utf-8").read()
corps = NH[NH.index("def _sample_one"):NH.index("def sample_all")]
verifie("_push_housekeeping(node, snap)" in corps, "4a. le relevé de santé de chaque nœud pousse la frontière")
m = SRC[SRC.index("def main():"):]
verifie(m.index("_hk_load()") < m.index("target=_membw_loop") < m.index("target=_wd_loop"),
        "4b. au démarrage, la frontière est reposée AVANT le premier fil")
verifie('"housekeeping": dict(_HK)' in SRC, "4c. /v1/health remonte la frontière")
verifie('if path == "/v1/host/housekeeping":' in SRC and "_hk_set(spec.strip())" in SRC,
        "4d. route POST /v1/host/housekeeping")
verifie(tuple(map(int, A.VERSION.split("."))) >= (0, 31, 1), "4e. version de l'agent (≥ 0.31.1)", A.VERSION)

print()
if echecs:
    print("ÉCHEC : %d vérification(s)" % len(echecs))
    for e in echecs:
        print("  - " + e)
    sys.exit(1)
print("OK : la sonde n'entre plus dans les conteneurs, et rien de ce que lance l'agent ne va sur un cœur dédié")
