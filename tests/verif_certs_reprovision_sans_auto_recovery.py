#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Après le redémarrage d'un nœud, les certificats mTLS perdus des conteneurs compute sont
# RE-PROVISIONNÉS même avec `auto_recovery_enabled` à son défaut d'usine (0).
#
# ★ LE CAS (Z8 neuve, 2026-10-30) : redémarrage de 15:13 demandé par l'interface (pilote
# Blackmagic). /run/bobi-tls est un tmpfs : Docker relève les `unless-stopped` avec un montage
# vide, 9 conteneurs sur 9 (#219-#227) servent en HTTP clair. Alerte `certs_http_clair` à
# 15:14:39, puis rien : la réparation n'était appelée que depuis `_recover_node`, derrière le
# réglage. Recréés à la main à 15:24-15:28. Décision de Cyril : sortir la RÉPARATION des certs du
# réglage (elle a ses garde-fous : une passe par boot, seulement les « clair », une tentative).
#
#   $ ./venv/bin/python tests/verif_certs_reprovision_sans_auto_recovery.py
import os
import sys
import tempfile
import threading
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
# ⚠ AVANT tout accès : connexion SQLite tenue PAR THREAD (cf. CLAUDE.md, « Modèle de threading »).
from app import config, database                                     # noqa: E402
_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_certs_reprov_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database.init_db()
from app import deploy, node_driver, iface_identity, node_recovery as nr   # noqa: E402

echecs = []


def controle(intitule, condition, explication=""):
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition:
        echecs.append(intitule)
        if explication:
            print("        → %s" % explication)


class _FilSynchrone:
    """threading.Thread exécuté sur place : la passe se joue dans le test, pas en arrière-plan."""
    def __init__(self, target=None, args=(), kwargs=None, **_):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


class _Temps:
    """Horloge du module : `time()` figé (détection du boot), `monotonic()` et `sleep()` RÉELS
    mais comptés — une attente fixe de 45 s se verrait dans `dormi`."""
    dormi = 0.0

    @staticmethod
    def time():
        return 1_000_000.0

    @staticmethod
    def monotonic():
        return time.monotonic()

    @classmethod
    def sleep(cls, s):
        cls.dormi += s
        time.sleep(min(s, 0.01))


CONTENEURS = [{"vmid": 219, "docker_ip": "10.0.0.219", "deploy_config": "{}"},   # clair
              {"vmid": 220, "docker_ip": "10.0.0.220", "deploy_config": "{}"},   # sain
              {"vmid": 221, "docker_ip": "10.0.0.221", "deploy_config": "{}"}]   # clair
SCHEMA = {"10.0.0.219": "clair", "10.0.0.220": "ok", "10.0.0.221": "clair"}
TARDIFS = {}        # ip → nombre de sondes muettes avant que Docker ait relevé le conteneur


def scenario(auto_recovery, tls=True, pret_apres=0, reprov_s=0.0):
    _Temps.dormi = 0.0
    sondes = {"n": 0}
    vol = {"en_cours": 0, "max": 0}
    verrou = threading.Lock()
    etat = {"reprov": [], "recover": 0, "alertes": [], "vol": vol, "sondes": sondes,
            "node": {"id": 7, "name": "z8", "last_boot_ts": 500_000.0, "recovered_boot_ts": 500_000.0}}

    def set_boot(nid, last_boot_ts=None, recovered_boot_ts=None):
        if last_boot_ts is not None:
            etat["node"]["last_boot_ts"] = last_boot_ts
        if recovered_boot_ts is not None:
            etat["node"]["recovered_boot_ts"] = recovered_boot_ts

    nr.threading = type("_Fils", (), {"Thread": _FilSynchrone})   # ce module seulement
    nr.time = _Temps
    nr.db_get_node = lambda nid: dict(etat["node"])
    nr.db_set_node_boot = set_boot
    nr.db_add_alert = lambda msg, *a, **k: etat["alertes"].append(msg)
    nr._cfg_node = lambda key, nid, d: auto_recovery if key == "auto_recovery_enabled" else d
    nr._verifier_prep_post_boot = lambda nid, name: None
    nr._desired_running = lambda nid: ([], [dict(c) for c in CONTENEURS])
    def reprov(c):
        with verrou:
            vol["en_cours"] += 1
            vol["max"] = max(vol["max"], vol["en_cours"])
        time.sleep(reprov_s)
        with verrou:
            vol["en_cours"] -= 1
            etat["reprov"].append(c["vmid"])
        return True
    nr._reprovisionner_certs = reprov

    def sante(node):
        sondes["n"] += 1
        return {"ok": True} if sondes["n"] > pret_apres else None
    node_driver.health = sante
    node_driver.list_containers = lambda node: []

    def recover(nid, boot_ts):
        etat["recover"] += 1
    nr._recover_node = recover
    deploy.agent_tls_on = lambda: tls
    vus = {}

    def schema(ip, vmid=None, alerter=True):
        vus[ip] = vus.get(ip, 0) + 1
        return "injoignable" if vus[ip] <= TARDIFS.get(ip, 0) else SCHEMA[ip]
    deploy.diagnostiquer_schema_agent = schema
    node_driver.has_agent = lambda node: True
    iface_identity.schedule_now = lambda nid: None

    # Boot neuf : uptime 120 s à t=1 000 000 → boot_ts 999 880, bien après 500 000.
    nr.on_health_snapshot(dict(etat["node"]), {"ts": 1_000_000.0, "host_uptime_s": 120})
    premier = list(etat["reprov"])
    # Snapshot suivant du MÊME boot (sampler 5 s) : rien ne doit repartir.
    nr.on_health_snapshot(dict(etat["node"]), {"ts": 1_000_005.0, "host_uptime_s": 125})
    return etat, premier


print("Certs mTLS re-provisionnés après reboot, auto-recovery désactivé\n")

etat, premier = scenario(auto_recovery=0)
controle("★★★ réglage d'usine (0) : les conteneurs « clair » sont re-provisionnés",
         sorted(premier) == [219, 221], "re-provisionnés : %s" % premier)
controle("★★★ un conteneur sain n'est jamais recréé", 220 not in etat["reprov"], etat["reprov"])
controle("★★ une seule passe par boot (le snapshot suivant ne relance rien)",
         sorted(etat["reprov"]) == [219, 221], etat["reprov"])
controle("★★ la reprise générale reste gardée par le réglage", etat["recover"] == 0, etat["recover"])
controle("★★ le bilan dit « re-provisionnés », pas « HTTP clair »",
         "alert.agent.certs_reprovisionnes" in etat["alertes"]
         and "alert.agent.certs_http_clair" not in etat["alertes"], etat["alertes"])
controle("★ l'arrêt du réglage reste signalé",
         "alert.node.reboot_recovery_off" in etat["alertes"], etat["alertes"])

controle("★★★ nœud prêt tout de suite : aucune attente fixe (la grace n'est qu'un plafond)",
         _Temps.dormi < 5, "dormi %.1f s" % _Temps.dormi)

etat, premier = scenario(auto_recovery=0, pret_apres=3)
controle("★★ nœud prêt après 3 sondes : on attend la sonde, puis on répare",
         sorted(premier) == [219, 221] and etat["sondes"]["n"] >= 4 and _Temps.dormi < 10,
         (premier, etat["sondes"], _Temps.dormi))

TARDIFS["10.0.0.221"] = 3
etat, premier = scenario(auto_recovery=0)
TARDIFS.clear()
controle("★★★ conteneur pas encore relevé par Docker : on attend SON agent, puis on le répare",
         sorted(premier) == [219, 221], premier)

CONTENEURS[:] = [{"vmid": v, "docker_ip": "10.0.1.%d" % v, "deploy_config": "{}"} for v in range(1, 13)]
SCHEMA.update({"10.0.1.%d" % v: "clair" for v in range(1, 13)})
t0 = time.monotonic()
etat, premier = scenario(auto_recovery=0, reprov_s=0.2)
duree = time.monotonic() - t0
controle("★★★ 12 réparations EN PARALLÈLE, bornées à %d à la fois" % nr._REPARATIONS_EN_VOL,
         etat["vol"]["max"] == nr._REPARATIONS_EN_VOL and len(premier) == 12,
         "max en vol %d, %d réparés" % (etat["vol"]["max"], len(premier)))
controle("★★ et sans pause entre deux (12 × 0,2 s en 4 files ≈ 0,6 s, pas 2,4 + 12)",
         duree < 2.0 and _Temps.dormi < 5, "%.2f s, dormi %.1f s" % (duree, _Temps.dormi))

etat, premier = scenario(auto_recovery=0, tls=False)
controle("★★ sans CA (flotte en HTTP) : rien n'est recréé", etat["reprov"] == [], etat["reprov"])

etat, premier = scenario(auto_recovery=1)
controle("★★ réglage activé : la passe passe par _recover_node, pas en double",
         etat["recover"] == 1 and etat["reprov"] == [], etat)

print("\n%s" % ("ÉCHEC : %d contrôle(s)" % len(echecs) if echecs else "TOUT VERT"))
sys.exit(1 if echecs else 0)
