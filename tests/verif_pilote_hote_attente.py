#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Un conteneur qui exige un PILOTE HÔTE absent ATTEND son pilote au lieu de mourir, démarre de
# lui-même au retour du nœud, et l'alerte nomme la cause.
#
# ★ LE CAS (Z8 neuve, 2026-10-30). 15:04:10 : #228 decklink_io créé sans pilote Desktop Video —
# l'agent-nœud rendait `install_needs_file` / `driver_missing_upload_needed`. 15:04:14 : le
# conteneur meurt, alerte « DISPARU du nœud ». Pilote installé à 15:08, redémarrage du nœud à
# 15:13 demandé par l'interface ; au retour, rien ne relance #228 (auto-recovery à 0, défaut).
# Décision de Cyril : enregistrer sans lancer, démarrer au retour du nœud MÊME auto-recovery
# désactivé, nommer la cause. Cf. app/host_driver_gate.py.
#
#   $ ./venv/bin/python tests/verif_pilote_hote_attente.py
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
# ⚠ AVANT tout accès : connexion SQLite tenue PAR THREAD (cf. CLAUDE.md, « Modèle de threading »).
from app import config, database                                     # noqa: E402
_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_pilote_hote_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database.init_db()
from app import deploy, fleet_status, host_driver_gate as g, node_driver, node_recovery as nr  # noqa: E402
from app import plugins, iface_identity                              # noqa: E402

echecs = []


def controle(intitule, condition, explication=""):
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition:
        echecs.append(intitule)
        if explication:
            print("        → %s" % (explication,))


class _FilSynchrone:
    def __init__(self, target=None, args=(), kwargs=None, **_):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


MANQUE = ["install_needs_file", "missing_prereqs", "install", "no_tool"]
MANIFESTES = {"carte": {"type": "carte", "host_driver": {"kind": "blackmagic", "missing_status": MANQUE}},
              "simple": {"type": "simple"}}
ETAT = {"doc": None}             # ce que l'agent-nœud répond à /v1/host/drivers/<kind>/state
APPELS = {"locked": [], "state": 0}

_get_orig = plugins.get
plugins.get = lambda t: MANIFESTES.get(t) or _get_orig(t)
node_driver.has_agent = lambda node: True


def _etat(node, method, path, body=None, timeout=100):
    APPELS["state"] += 1
    return (ETAT["doc"] is not None), (ETAT["doc"] or "agent muet")
node_driver.host_driver_call = _etat
import app.routes.host_drivers as _hd                                # noqa: E402
_hd.push_scripts = lambda node, only=None: (True, "")
deploy._deployer_script_locked = lambda vmid, t, p, **k: APPELS["locked"].append(vmid) or True
g.time.sleep = lambda s: None
# Dans CES modules seulement : remplacer threading.Thread partout bloquerait le fil d'alerting.
nr.threading = fleet_status.threading = type("_Fils", (), {"Thread": _FilSynchrone})
iface_identity.schedule_now = lambda nid: None
nr._verifier_prep_post_boot = lambda nid, name: None
nr._certs_conteneurs_node = lambda *a, **k: ([], [], [])

NID = database.db_add_node("z8", "10.0.0.8")
ABSENT = {"status": "install_needs_file", "plan": {"code": "driver_missing_upload_needed"}}
PRESENT = {"status": "ok", "plan": {"code": "all_up_to_date"}}


def conteneur(vmid, type_):
    database.db_upsert_container_docker(vmid, "c%d" % vmid, NID, "bobi-cmp-%d" % vmid)
    database.db_update_deploy_config(vmid, type_, {})
    return vmid


def alertes(vmid=None):
    return [a.get("msg_key") or a.get("message") for a in
            database.db_get_alerts(limit=200, vmid=vmid, node_id=None if vmid else NID)]


def ligne(vmid):
    return database.db_get_container(vmid) or {}


print("Pilote hôte requis : attendre, démarrer au retour du nœud, nommer la cause\n")

print("── 1. création sans pilote : enregistré, pas lancé ──────────────────────")
ETAT["doc"] = ABSENT
v = conteneur(228, "carte")
ok = deploy.deployer_script(v, "carte", {"profile": "8in"})
controle("★★★ aucun docker run (le chemin de déploiement n'est pas emprunté)",
         APPELS["locked"] == [], APPELS["locked"])
controle("★★★ le conteneur est enregistré « en attente du pilote »",
         ligne(v).get("desired_state") == "awaiting_driver" and ligne(v).get("status") == "awaiting_driver",
         {k: ligne(v).get(k) for k in ("desired_state", "status")})
controle("★★ ses params sont gardés pour le démarrage différé",
         json.loads(ligne(v).get("deploy_config") or "{}").get("params") == {"profile": "8in"},
         ligne(v).get("deploy_config"))
controle("★★ la création réussit (la page du plugin, où se dépose le pilote, existe)", ok is True, ok)
controle("★★ l'alerte nomme le pilote et la cause",
         "alert.deploy.awaiting_host_driver" in alertes(v), alertes(v))
mtl, comp = nr._desired_running(NID)
controle("★★★ hors de la liste « censé tourner » → jamais « disparu », jamais relancé à l'aveugle",
         v not in [c["vmid"] for c in comp], [c["vmid"] for c in comp])

print("\n── 2. ce qu'on ne retient PAS ───────────────────────────────────────────")
for nom, doc, type_ in (("pilote présent", PRESENT, "carte"), ("agent muet (état inconnu)", None, "carte"),
                        ("plugin sans pilote déclaré", ABSENT, "simple"),
                        ("statut hors liste (aucune carte)", {"status": "no_card"}, "carte")):
    ETAT["doc"] = doc
    APPELS["locked"] = []
    w = conteneur(300 + len(nom), type_)
    deploy.deployer_script(w, type_, {})
    controle("★★ %s : déployé normalement" % nom,
             APPELS["locked"] == [w] and ligne(w).get("desired_state") == "running",
             (APPELS["locked"], ligne(w).get("desired_state")))

print("\n── 3. retour du nœud, auto-recovery désactivé ──────────────────────────")
nr._cfg_node = lambda key, nid, d: 0 if key == "auto_recovery_enabled" else d
nr._recover_node = lambda *a: None


def reboot(boot):
    n = database.db_get_node(NID)
    database.db_set_node_boot(NID, last_boot_ts=boot - 1000, recovered_boot_ts=boot - 1000)
    nr.on_health_snapshot(database.db_get_node(NID) or n, {"ts": boot + 100, "host_uptime_s": 100})


ETAT["doc"] = ABSENT
APPELS["locked"] = []
reboot(2_000_000.0)
controle("★★ pilote toujours absent : on n'essaie pas", APPELS["locked"] == [], APPELS["locked"])
ETAT["doc"] = None
reboot(2_500_000.0)
controle("★★★ agent-nœud muet au retour : sans PREUVE que le pilote est là, on ne démarre rien",
         APPELS["locked"] == [] and ligne(v).get("desired_state") == "awaiting_driver",
         (APPELS["locked"], ligne(v).get("desired_state")))
ETAT["doc"] = PRESENT
reboot(3_000_000.0)
controle("★★★ pilote présent : le conteneur qui l'attendait démarre de lui-même",
         APPELS["locked"] == [v] and ligne(v).get("desired_state") == "running",
         (APPELS["locked"], ligne(v).get("desired_state")))
controle("★★ avec les params qu'il avait à la création",
         json.loads(ligne(v).get("deploy_config") or "{}").get("params") == {"profile": "8in"})
APPELS["locked"] = []
reboot(4_000_000.0)
controle("★★ ciblé : un conteneur qui n'attend plus rien n'est pas relancé au boot suivant",
         APPELS["locked"] == [], APPELS["locked"])

print("\n── 4. disparu : la cause est nommée ────────────────────────────────────")
fleet_status._gone_prev.clear()
x = conteneur(400, "carte")
database.db_set_desired_state(x, "running")
y = conteneur(401, "simple")
database.db_set_desired_state(y, "running")
fleet_status._gone_prev.update({x: False, y: False})
fleet_status._episodes_gone.retirer(x)
fleet_status._episodes_gone.retirer(y)
ETAT["doc"] = ABSENT
fleet_status.reconcile({NID: {}}, set())
ax, ay = alertes(x), alertes(y)
controle("★★★ pilote absent : l'alerte le dit au lieu de « disparu »",
         "alert.deploy.container_disparu_pilote" in ax and "alert.deploy.container_disparu" not in ax, ax)
controle("★★ plugin sans pilote : « disparu » inchangé",
         "alert.deploy.container_disparu" in ay, ay)

print("\n── 5. la surveillance ne réécrit pas « stopped » sur un conteneur en attente ──")
src = open(os.path.join(RACINE, "main.py"), encoding="utf-8").read()
i = src.find('c.get("desired_state") == "awaiting_driver"')
j = src.find('db_update_status(vmid, "running" if st == "running" else "stopped")')
controle("★★ main.py saute l'écriture du statut observé pour un conteneur en attente",
         0 < i < j and src[i:j].count("continue") == 1, (i, j))

print("\n%s" % ("ÉCHEC : %d contrôle(s)" % len(echecs) if echecs else "TOUT VERT"))
sys.exit(1 if echecs else 0)
