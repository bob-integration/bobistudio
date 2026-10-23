#!/usr/bin/env python3
"""Vérifie la persistance GÉNÉRIQUE déclarée par un plugin (`control.persist`, app/plugin_persist.py).

Contrat : (1) une déclaration fausse est refusée au scan ; (2) un réglage accepté est relu PAR LOT
— une rafale donne UNE lecture et UNE écriture, un geste continu écrit au plus tard toutes les
MAX_WAIT_S ; un refus du conteneur ou un endpoint non déclaré n'écrit rien ; un instantané en
échec lève une alerte et n'écrit rien ; (3) une structure est validée à blanc, PERSISTÉE AVANT
d'être appliquée, et l'ancien document est remis si l'application est refusée ; (4) les macros
passent par la même persistance que le proxy.

Aucun réseau, aucune base : requêtes et base remplacées par des doublures.

    ./venv/bin/python tests/verif_plugin_persist.py
"""
import json
import os
import sys
import tempfile
import time

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)
from app import plugin_persist as pp, plugins, database, addressing, macros   # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


PERSIST = {"param": "console", "snapshot": "/doc", "settings": ["/set", "/mute"],
           "structure": {"apply": "/structure/apply", "validate": "/structure/validate"}}
CTRL = {"endpoints": ["/state", "/doc", "/set", "/mute", "/structure/validate", "/structure/apply"],
        "read_endpoints": ["/state", "/doc"], "persist": PERSIST}
M = {"type": "fake", "control": CTRL}

# ── 1. validation de la déclaration ──────────────────────────────────────────────────
ok(pp.validate_spec(M) == [] and pp.spec(M)["param"] == "console", "déclaration valide acceptée")
ok(pp.validate_spec({"control": {}}) == [] and pp.spec({"control": {}}) is None, "sans déclaration : rien")
for titre, mut in (("snapshot hors read_endpoints", {"snapshot": "/set"}),
                   ("réglage non déclaré en endpoint", {"settings": ["/nope"]}),
                   ("structure sans validate", {"structure": {"apply": "/structure/apply"}}),
                   ("param vide", {"param": ""})):
    bad = {"control": dict(CTRL, persist=dict(PERSIST, **mut))}
    ok(pp.validate_spec(bad) and pp.spec(bad) is None, "refusé : " + titre, str(pp.validate_spec(bad)))

# scan : un plugin à déclaration fausse est SAUTÉ
TMP = tempfile.mkdtemp(prefix="verif_persist_")
for nom, persist in (("pok", PERSIST), ("pbad", dict(PERSIST, snapshot="/set"))):
    d = os.path.join(TMP, nom)
    os.makedirs(d)
    open(os.path.join(d, "script.py"), "w").write("print({config!r}, {hostname!r}, {plugin_version!r})\n")
    json.dump({"type": nom, "label": nom, "version": "1.0", "script_template": "script.py",
               "deploy_defaults": {}, "control": dict(CTRL, persist=persist)},
              open(os.path.join(d, "plugin.json"), "w"))
_old_dir = plugins.PLUGINS_DIR
plugins.PLUGINS_DIR = TMP
plugins.reload()
ok("pok" in plugins.REGISTRY, "scan : déclaration valide chargée", str(plugins.SCAN_ERRORS))
ok("pbad" not in plugins.REGISTRY and "persist" in plugins.SCAN_ERRORS.get("pbad", ""), "scan : déclaration fausse sautée",
   str(plugins.SCAN_ERRORS.get("pbad")))
_, err = plugins.validate_package(os.path.join(TMP, "pbad"))
ok(err and "persist" in err, "validate_package : même règle", str(err))
plugins.PLUGINS_DIR = _old_dir
plugins.reload()

# ── doublures : base, réseau, verrou ────────────────────────────────────────────────
DB = {7: {"type": "fake", "params": {"console": {"version": 1, "g": 0}, "autre": "x"}}}
writes, alerts, calls = [], [], []


class Rep:
    def __init__(self, code, obj):
        self.status_code, self._o = code, obj
        self.text = json.dumps(obj)

    def json(self):
        return self._o


LIVE = {"doc": {"version": 1, "g": 0}, "snap_code": 200, "validate": (200, None), "apply": (200, {"ok": True})}


def fake_get(url, timeout=None, **k):
    calls.append(("GET", url.rsplit(":8082", 1)[1]))
    return Rep(LIVE["snap_code"], LIVE["doc"])


def fake_post(url, json=None, timeout=None, **k):
    path = url.rsplit(":8082", 1)[1]
    calls.append(("POST", path, json))
    if path == "/structure/validate":
        code, doc = LIVE["validate"]
        return Rep(code, {"ok": code == 200, "doc": doc} if code == 200 else {"ok": False, "errors": [{"code": "x"}]})
    if path == "/structure/apply":
        code, obj = LIVE["apply"]
        calls.append(("PERSISTED_AT_APPLY", DB[7]["params"].get("console")))
        return Rep(code, obj)
    return Rep(200, {"ok": True})


pp.requests.get, pp.requests.post = fake_get, fake_post
pp._dc = lambda vmid: json.loads(json.dumps(DB.get(vmid))) if vmid in DB else None
database.db_update_deploy_config = lambda vmid, t, params: (writes.append(json.loads(json.dumps(params))),
                                                            DB.__setitem__(vmid, {"type": t, "params": params}))
database.db_add_alert = lambda msg, *a, **k: alerts.append(msg)
addressing.get_container_ip = lambda vmid: "10.0.0.7"
pp.DEBOUNCE_S, pp.MAX_WAIT_S = 0.15, 0.6
# Script réécrit sur disque à chaque écriture (sinon un stop/start revient au dernier déploiement)
from app import scripts as _scripts, deploy as _deploy      # noqa: E402
pushed = []
_scripts.generer_script = lambda t, params, v=None: "CONFIG=%r" % (params,)


class _Sess:
    def post(self, url, json=None, headers=None, timeout=None):
        pushed.append((url, json))
        return Rep(200, {"status": "ok"})


_deploy.agent_session = lambda: _Sess()
_deploy.agent_headers = lambda vmid: {}
database.db_get_container = lambda vmid: {"vmid": vmid, "script": "/opt/script/main.py"}


def wait_idle(t=2.0):
    end = time.time() + t
    while pp.pending(7) and time.time() < end:
        time.sleep(0.02)
    time.sleep(0.05)


# ── 2. réglages par lot ──────────────────────────────────────────────────────────────
for i in range(30):
    LIVE["doc"] = {"version": 1, "g": i}
    pp.after_post(7, M, "/set", 200)
wait_idle()
gets = [c for c in calls if c[0] == "GET"]
ok(len(gets) == 1 and gets[0][1] == "/doc", "rafale de 30 réglages : UNE lecture d'instantané", str(gets))
ok(len(writes) == 1 and writes[0]["console"]["g"] == 29, "… et UNE écriture, la dernière valeur", str(writes))
ok(writes[0].get("autre") == "x", "les autres params ne sont pas touchés")
ok(len(pushed) == 1 and pushed[0][0].endswith("/deploy") and pushed[0][1]["path"] == "/opt/script/main.py"
   and "'g': 29" in pushed[0][1]["content"], "script réécrit dans le conteneur avec l'état persisté (sans relance)",
   str(pushed)[:200])

del calls[:], writes[:]
t0 = time.time()
while time.time() - t0 < 1.5:            # geste continu, jamais 0,15 s d'immobilité
    LIVE["doc"] = {"version": 1, "g": round(time.time() - t0, 3)}
    pp.after_post(7, M, "/set", 200)
    time.sleep(0.05)
n_during = len(writes)
wait_idle()
ok(n_during >= 2, "geste continu : écrit au plus tard toutes les MAX_WAIT_S (%d écritures pendant 1,5 s)" % n_during)

del calls[:], writes[:]
pp.after_post(7, M, "/set", 409)
pp.after_post(7, M, "/state", 200)
pp.after_post(7, {"type": "x", "control": {"endpoints": ["/set"]}}, "/set", 200)
wait_idle(0.5)
ok(not calls and not writes, "refus du conteneur / endpoint non déclaré / plugin sans déclaration : rien")

del calls[:], writes[:]
LIVE["doc"] = json.loads(json.dumps(DB[7]["params"]["console"]))
pp.after_post(7, M, "/mute", 200)
wait_idle()
ok(not writes, "instantané identique à la base : aucune écriture (pas de config_rev gratuit)")

del writes[:]
LIVE["snap_code"] = 500
LIVE["doc"] = {"version": 1, "g": 99}
pp.after_post(7, M, "/set", 200)
wait_idle()
ok(not writes and alerts and "non enregistrés" in alerts[-1], "instantané en échec : alerte, aucune écriture", str(alerts[-1:]))
LIVE["snap_code"] = 200

# ── 3. structure : persister avant d'appliquer ──────────────────────────────────────
del calls[:], writes[:]
DB[7]["params"]["console"] = {"version": 1, "g": 29}
canon = {"version": 2, "g": 29, "new": True}
LIVE["validate"], LIVE["apply"] = (200, canon), (200, {"ok": True, "version": 2})
r = pp.structure_apply(7, M, "/structure/apply", {"doc": {"g": 29, "new": True}, "base_version": 1})
order = [c[0] if c[0] != "POST" else c[1] for c in calls]
ok(r[1] == 200 and order[:2] == ["/structure/validate", "/structure/apply"], "validation à blanc puis application", str(order))
at_apply = next(c[1] for c in calls if c[0] == "PERSISTED_AT_APPLY")
ok(at_apply == canon, "le document CANONIQUE est persisté AVANT l'application", str(at_apply))
ok(DB[7]["params"]["console"] == canon, "après succès : document canonique en base")

del calls[:], writes[:]
canon3 = {"version": 3, "g": 29, "delay": 4}
LIVE["validate"], LIVE["apply"] = (200, canon3), (409, {"ok": False, "error": "latency_jump"})
r = pp.structure_apply(7, M, "/structure/apply", {"doc": canon3, "base_version": 2})
ok(r[1] == 409 and DB[7]["params"]["console"] == canon, "application refusée : réponse relayée, ancien document remis",
   str(DB[7]["params"]["console"]))

del calls[:], writes[:]
LIVE["validate"] = (422, None)
r = pp.structure_apply(7, M, "/structure/apply", {"doc": {"bad": 1}, "base_version": 2})
ok(r[1] == 422 and not writes and not any(c[0] == "POST" and c[1] == "/structure/apply" for c in calls),
   "document invalide : rien persisté, rien appliqué")
ok(pp.structure_apply(7, M, "/set", {}) is None, "autre endpoint : pas concerné (None)")

# ── 4. les macros persistent comme le proxy ─────────────────────────────────────────
del calls[:], writes[:]
seen = []
_orig = pp.after_post
pp.after_post = lambda vmid, m, ep, st: seen.append((vmid, m.get("type"), ep, st))
macros.db_get_container = lambda vmid: {"vmid": vmid, "deploy_config": json.dumps({"type": "pok2", "params": {}})}
macros.get_container_ip = lambda vmid: "10.0.0.7"
macros.requests.post = fake_post
plugins.REGISTRY["pok2"] = dict(M, type="pok2", actions=[{"id": "mute", "endpoint": "/mute", "params": []}])
macros.exec_post(7, "/set", {"path": "a", "value": 1})
macros.exec_action(7, "mute", {})
ok(seen == [(7, "pok2", "/set", 200), (7, "pok2", "/mute", 200)], "macros (POST avancé, action) : même persistance", str(seen))
pp.after_post = _orig

# ── 5. après un déploiement : la base reprend la forme CANONIQUE du conteneur ─────────────────
plugins.REGISTRY["fake"] = M
DB[7] = {"type": "fake", "params": {"console": {"version": 5, "strips": [{"id": "s_a", "direct_out": {"after": "input"}}]},
                                    "autre": "x"}}
canon5 = {"version": 5, "strips": [{"id": "s_a", "direct_outs": [{"id": "d_1", "after": "input"}]}], "outputs": []}
LIVE["doc"], LIVE["snap_code"] = canon5, 200
del writes[:]
th = pp.after_deploy(7, "fake", delays=(0.0,))
th.join(2)
ok(DB[7]["params"]["console"] == canon5 and DB[7]["params"]["autre"] == "x" and len(writes) == 1,
   "5a. après déploiement : document canonique du conteneur persisté (le reste de la config intact)", str(DB[7]))
del writes[:]
pp.after_deploy(7, "fake", delays=(0.0,)).join(2)
ok(not writes, "5b. déjà canonique : aucune écriture")
DB[7]["params"]["console"] = {"version": 5, "strips": []}
for titre, snap in (("objet d'erreur d'un conteneur qui a refusé son document",
                     {"error": "document de console refusé", "running": False}),
                    ("autre version (pas le même document)", dict(canon5, version=1)),
                    ("clé perdue", {"version": 5})):
    LIVE["doc"] = snap
    del writes[:]
    pp.after_deploy(7, "fake", delays=(0.0,)).join(2)
    ok(not writes and DB[7]["params"]["console"] == {"version": 5, "strips": []}, "5c. non retenu : " + titre, str(DB[7]))
DB[7]["params"]["console"] = {"version": 5, "strips": [], "listen_mode": "afl"}
LIVE["doc"] = {"version": 5, "strips": [], "buses": [{"id": "b_l", "mode": "afl"}]}
del writes[:]
pp.after_deploy(7, "fake", delays=(0.0,)).join(2)
ok(len(writes) == 1 and DB[7]["params"]["console"] == LIVE["doc"],
   "5f. migration qui abandonne une clé SIMPLE (listen_mode → mode du monitoring) : retenue", str(DB[7]))
DB[7]["params"]["console"] = {"version": 5, "strips": []}
LIVE["snap_code"] = 503
del writes[:]
pp.after_deploy(7, "fake", delays=(0.0, 0.0)).join(2)
ok(not writes and "illisible" in (pp.stats()["last_error"] or ""), "5d. conteneur muet : rien écrit, raison retenue",
   str(pp.stats()["last_error"]))
LIVE["snap_code"] = 200
ok(pp.after_deploy(7, "sans_persist") is False, "5e. plugin sans control.persist : rien")

print()
print("ÉCHEC : %d" % len(ko) if ko else "TOUT VERT")
sys.exit(1 if ko else 0)
