#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Pilotes hôte de l'agent-nœud : le MÉCANISME générique (job long, journal, action en attente).

★ Ce que ce test protège. Le mécanisme ne connaît aucun « kind » : il lance `<kind>.py run`, garde le
journal, et mémorise ce que le script réclame à sa sortie — 10 = redémarrage, 11 = ARRÊT COMPLET.
Les deux obligations qui ne se voient pas :
  · une action « arrêt complet » en attente NE se lève PAS sur un simple redémarrage (le micrologiciel
    d'une carte ne se recharge qu'à la remise sous tension — un redémarrage qui la levait ferait croire
    la mise à jour faite) ;
  · l'état d'un job survit à un redémarrage de l'agent (code retour lu depuis le fichier .rc).
On exerce de vrais sous-processus avec de faux « kinds » dans un dossier jetable.

    ./venv/bin/python tests/verif_host_driver_jobs.py
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import time

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("BOBI_AGENT_SRC") or os.path.join(R, "node_agent", "agent.py")
tmp = tempfile.mkdtemp(prefix="hd-test-")
os.environ["BOBI_HOST_DRIVERS_DIR"] = os.path.join(tmp, "scripts")
os.environ["BOBI_HOST_DRIVERS_STATE"] = os.path.join(tmp, "state")
os.environ["BOBI_HOST_DRIVERS_FILES"] = os.path.join(tmp, "files")
os.makedirs(os.environ["BOBI_HOST_DRIVERS_DIR"])
spec = importlib.util.spec_from_file_location("agent_under_test", SRC)
ag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ag)

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


def kind(name, rc, body="print('line one')\nprint('line two')"):
    with open(os.path.join(os.environ["BOBI_HOST_DRIVERS_DIR"], name + ".py"), "w") as f:
        f.write("import sys, json\nif sys.argv[1] == 'state':\n    print(json.dumps({'kind': '%s', 'status': 'ok', "
                "'plan': {}, 'rows': [], 'can_run': True}))\n    sys.exit(0)\n%s\nsys.exit(%d)\n" % (name, body, rc))


def wait(k, secs=15):
    t0 = time.time()
    while time.time() - t0 < secs:
        j = ag._hd_job_public(k)
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    return ag._hd_job_public(k)


class Req:
    """Faux gestionnaire HTTP (ce que _hd_route attend de `h`)."""
    def __init__(self, body=b"", headers=None):
        self.rfile = io.BytesIO(body)
        self.headers = headers or {}
        self.sent = None

    def _send(self, code, obj):
        self.sent = (code, obj)

    def _body(self):
        try:
            return json.loads(self.rfile.read() or b"{}")
        except ValueError:
            return {}


def run():
    # ── Découverte : un kind = un script ────────────────────────────────────────────────────────
    kind("fakeok", 0)
    kind("fakereboot", 10)
    kind("fakeshut", 11)
    kind("fakefail", 3, "print('boom')")
    open(os.path.join(os.environ["BOBI_HOST_DRIVERS_DIR"], "_private.py"), "w").write("x=1")
    ok(set(ag._hd_kinds()) == {"fakeok", "fakereboot", "fakeshut", "fakefail"},
       "les kinds sont découverts dans le dossier (un script = un kind, `_x` ignoré)", "kinds=%s" % ag._hd_kinds())
    ok(ag._hd_script("../etc/passwd") is None and ag._hd_script("Bad Kind") is None,
       "un nom de kind malformé n'atteint jamais le système de fichiers", "")

    # ── Codes de sortie → action en attente ─────────────────────────────────────────────────────
    code, _ = ag._hd_run("fakeok")
    j = wait("fakeok")
    ok(code == 200 and j["status"] == "done" and j["pending"] == "", "sortie 0 → terminé, rien en attente", "job=%s" % j)
    ag._hd_run("fakereboot")
    j = wait("fakereboot")
    ok(j["status"] == "done" and j["pending"] == "reboot", "sortie 10 → terminé, REDÉMARRAGE en attente", "job=%s" % j)
    ag._hd_run("fakeshut")
    j = wait("fakeshut")
    ok(j["status"] == "done" and j["pending"] == "shutdown", "sortie 11 → terminé, ARRÊT COMPLET en attente", "job=%s" % j)
    ag._hd_run("fakefail")
    j = wait("fakefail")
    ok(j["status"] == "error" and j["rc"] == 3 and j["pending"] == "", "sortie 3 → échec, rien en attente", "job=%s" % j)

    # ── Journal : lisible par morceaux, avec décalage ───────────────────────────────────────────
    r = ag._hd_log("fakeok", 0)
    ok("line one" in r["log"] and "line two" in r["log"], "le journal du job est servi", "log=%r" % r["log"])
    r2 = ag._hd_log("fakeok", r["next_offset"])
    ok(r2["log"] == "" and r2["next_offset"] == r["next_offset"], "relire depuis le décalage ne renvoie rien de neuf", "r2=%s" % r2)
    r3 = ag._hd_log("fakeok", 5)
    ok(r3["log"] == r["log"][5:], "un décalage au milieu renvoie la suite exacte", "")

    # ── Un job à la fois par kind ───────────────────────────────────────────────────────────────
    kind("fakeslow", 0, "import time; time.sleep(3); print('late')")
    c1, _ = ag._hd_run("fakeslow")
    c2, d2 = ag._hd_run("fakeslow")
    ok(c1 == 200 and c2 == 409 and d2["error"] == "job_running", "deuxième lancement du même kind pendant un job → 409", "c2=%s %s" % (c2, d2))
    wait("fakeslow")

    # ── Redémarrage de l'agent en plein job : état retrouvé par le fichier .rc ────────────────────
    kind("fakeslow2", 10, "import time; time.sleep(2)")
    ag._hd_run("fakeslow2")
    jp = ag._hd_paths("fakeslow2")[0]
    time.sleep(0.3)
    ok(json.load(open(jp))["status"] == "running", "pendant le job : état « running » sur disque", "")
    j = wait("fakeslow2")
    ok(j["status"] == "done" and j["pending"] == "reboot", "fin de job retrouvée depuis le disque (code 10 → redémarrage)", "job=%s" % j)
    # simule la perte du thread : état écrit « running » avec un pid mort, .rc présent
    d = json.load(open(jp))
    d.update(status="running", pid=2 ** 22 + 12345, pending="", ended=None)
    json.dump(d, open(jp, "w"))
    open(ag._hd_paths("fakeslow2")[2], "w").write("11\n")
    j = ag._hd_job_public("fakeslow2")
    ok(j["status"] == "done" and j["pending"] == "shutdown", "agent redémarré après la fin du job : finalisé paresseusement depuis le .rc (11)", "job=%s" % j)
    json.dump(dict(d, status="running", pid=2 ** 22 + 12345), open(jp, "w"))
    os.unlink(ag._hd_paths("fakeslow2")[2])
    j = ag._hd_job_public("fakeslow2")
    ok(j["status"] == "error" and j["rc"] == -1, "job disparu SANS code retour → échec explicite, jamais un succès supposé", "job=%s" % j)

    # ── Levée de l'action en attente ────────────────────────────────────────────────────────────
    real_boot = ag._boot_id
    try:
        ag._boot_id = lambda: "boot-A"
        ag._hd_run("fakereboot")
        wait("fakereboot")
        ok(ag._hd_job_public("fakereboot")["pending"] == "reboot", "même boot → redémarrage toujours en attente", "")
        ag._boot_id = lambda: "boot-B"
        ok(ag._hd_job_public("fakereboot")["pending"] == "", "le noyau a redémarré (boot_id change) → redémarrage LEVÉ", "")

        ag._boot_id = lambda: "boot-A"
        ag._hd_run("fakeshut")
        wait("fakeshut")
        ag._boot_id = lambda: "boot-B"
        ok(ag._hd_job_public("fakeshut")["pending"] == "shutdown",
           "arrêt complet en attente + simple REDÉMARRAGE (boot_id change) → TOUJOURS en attente", "job=%s" % ag._hd_job_public("fakeshut"))
        # arrêt demandé par l'agent lui-même (marqueur posé pour le boot du job), puis nouveau boot
        ag._boot_id = lambda: "boot-A"
        os.makedirs(ag.HD_STATE_DIR, exist_ok=True)
        open(ag._hd_poweroff_marker(), "w").write("boot-A")
        ag._boot_id = lambda: "boot-C"
        ok(ag._hd_job_public("fakeshut")["pending"] == "", "arrêt demandé via l'agent puis remise sous tension → levé", "")
        open(ag._hd_poweroff_marker(), "w").write("boot-OTHER")
        ok(ag._hd_job_public("fakeshut")["pending"] == "shutdown", "marqueur d'un AUTRE boot → n'autorise pas la levée", "")
    finally:
        ag._boot_id = real_boot

    # ── State relayé : le script ne sait rien du job, l'agent l'ajoute ───────────────────────────
    code, doc = ag._hd_state("fakeok")
    ok(code == 200 and doc["ok"] and doc["job"]["status"] == "done" and len(doc["script_sha256"]) == 64,
       "state : document du script + job + empreinte du script", "doc=%s" % doc)
    code, doc = ag._hd_state("nonexistent")
    ok(code == 404 and doc["error"] == "host_driver_missing", "kind absent → 404 host_driver_missing", "")
    kind("fakebad", 0, "pass")
    open(os.path.join(os.environ["BOBI_HOST_DRIVERS_DIR"], "fakebad.py"), "w").write("print('not json')\n")
    code, doc = ag._hd_state("fakebad")
    ok(code == 500 and doc["error"] == "state_failed", "script dont le state n'est pas du JSON → 500 explicite", "code=%s" % code)

    # ── Téléversement : nom validé, corps tronqué refusé, écriture atomique ──────────────────────
    from urllib.parse import urlparse
    h = Req(b"x" * 10, {"Content-Length": "10"})
    ag._hd_upload(h, "fakeok", urlparse("/v1/host/drivers/fakeok/upload?name=../../evil.run"))
    ok(h.sent[0] == 400 and h.sent[1]["error"] == "bad_name", "nom avec « .. » refusé", "sent=%s" % (h.sent,))
    h = Req(b"x" * 10, {"Content-Length": "10"})
    ag._hd_upload(h, "fakeok", urlparse("/v1/host/drivers/fakeok/upload?name=script.sh"))
    ok(h.sent[0] == 400, "extension non listée refusée", "sent=%s" % (h.sent,))
    h = Req(b"x" * 4, {"Content-Length": "10"})
    ag._hd_upload(h, "fakeok", urlparse("/v1/host/drivers/fakeok/upload?name=a.run"))
    dest = os.path.join(ag.HD_FILES_DIR, "fakeok", "a.run")
    ok(h.sent[0] == 400 and h.sent[1]["error"] == "truncated" and not os.path.exists(dest)
       and not os.path.exists(dest + ".part"), "corps tronqué → refusé, aucun fichier ni .part laissé", "sent=%s" % (h.sent,))
    h = Req(b"y" * 10, {"Content-Length": "10"})
    ag._hd_upload(h, "fakeok", urlparse("/v1/host/drivers/fakeok/upload?name=a.run"))
    ok(h.sent[0] == 200 and open(dest, "rb").read() == b"y" * 10 and len(h.sent[1]["sha256"]) == 64,
       "fichier complet → écrit sous <files>/<kind>/, empreinte renvoyée", "sent=%s" % (h.sent,))

    # ── Aiguillage : /v1/host/drivers/* est pris par la couche générique ─────────────────────────
    h = Req()
    handled = ag._hd_route(h, "GET", "/v1/host/drivers", urlparse("/v1/host/drivers"))
    ok(handled and h.sent[0] == 200 and {k["kind"] for k in h.sent[1]["kinds"]} >= {"fakeok", "fakeshut"},
       "GET /v1/host/drivers liste les kinds", "sent=%s" % (h.sent,))
    h = Req()
    ok(ag._hd_route(h, "GET", "/v1/host/clock", urlparse("/v1/host/clock")) is False, "les autres routes ne sont pas avalées", "")
    h = Req(json.dumps({}).encode())
    ag._hd_route(h, "POST", "/v1/host/poweroff", urlparse("/v1/host/poweroff"))
    ok(h.sent[0] == 400 and h.sent[1]["error"] == "confirm_required",
       "arrêt complet SANS confirm:true → refusé, rien d'exécuté", "sent=%s" % (h.sent,))
    src = open(SRC, encoding="utf-8").read()
    ok('if _hd_route(self, "GET"' in src and 'if _hd_route(self, "POST"' in src,
       "l'agent branche la couche générique en GET et en POST (avant la lecture du corps JSON)", "")
    ok(len(__import__("re").findall(r'"nvidia"', src[src.index("# ─── Pilotes hôte"):src.index("# ─── Serveur HTTP")])) == 0,
       "la couche générique ne nomme aucun kind", "")

    # ── Côté contrôleur (lecture statique : l'import complet de l'application touche la base) ────
    import re
    rs = open(os.path.join(R, "app", "routes", "host_drivers.py"), encoding="utf-8").read()
    routes = re.findall(r"@bp\.route\(([^\n]*)\)\n(@require_\w+\([^\n]*\))?", rs)
    ok(len(routes) >= 6 and all(g[1] == '@require_perm("settings.edit")' for g in routes),
       "toutes les routes de pilote hôte exigent la permission d'administration (settings.edit)", "routes=%s" % routes)
    ok(not re.search(r"[\"']nvidia[\"']", rs),
       "le relais contrôleur ne nomme aucun kind (il découvre node_agent/host_drivers/*.py)", "")
    ok("confirm_required" in rs and 'get("confirm")' in rs, "l'arrêt complet exige confirm:true côté contrôleur", "")
    ui = open(os.path.join(R, "templates", "settings.html"), encoding="utf-8").read()
    a = ui.index("// ─── Pilotes hôte d'un nœud : panneau GÉNÉRIQUE")
    b = ui.index("// GPU NVIDIA : détection")
    ok("nvidia" not in ui[a:b].lower().replace("de nvidia", ""),
       "le panneau d'interface ne nomme aucun kind (libellés settings.hostdrv.<kind>.*)", "")
    # ★ LE REDÉMARRAGE PASSE PAR LE PARCOURS COMMUN, il ne refait pas sa propre route.
    #   Cette garde visait « /api/mtl/reboot » DANS le panneau ; depuis 845f759 le panneau
    #   appelle `_nodeRebootFlow()`, lequel refuse un redémarrage normal quand des processus
    #   sont bloqués dans le noyau — un parcours strictement meilleur, et partagé. Ce qu'il faut
    #   garder, c'est l'invariant (« pas de route de redémarrage propre à un kind »), pas l'URL
    #   d'hier : la garde était rouge pour une amélioration.
    ok("_nodeRebootFlow(" in ui[ui.index("async function hostDrvReboot"):][:400],
       "le redémarrage du panneau passe par le parcours commun de redémarrage de nœud", "")
    ok(not re.search(r"host-drivers[^;\n]*reboot", ui[a:b]),
       "aucune route de redémarrage propre à un pilote hôte", "")


if __name__ == "__main__":
    try:
        run()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if ko:
        print("%d ÉCHEC(S)" % len(ko))
        sys.exit(1)
    print("tout est vert")
