#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du `NcWorker.enabled` des scripts (services/nmos/plugins_ncp.py:Script).
#
# CE QUI COMPTE. Exposer la propriété est facile ; ce qui est difficile, c'est qu'elle TIENNE.
# Deux façons pour ce pilotage d'être un leurre, et le banc les vise toutes les deux :
#   1. la consigne est acceptée mais le script continue de tourner ;
#   2. la consigne est appliquée puis DÉFAITE au premier redéploiement — l'orchestrateur fait
#      /stop puis /start, et le contrôleur n'en saura rien. C'est ce cas-là qui est vicieux :
#      tout paraît marcher le jour du test.
#
# ⚠ MUTANT : ce banc ARRÊTE puis REDÉMARRE un vrai script. Il choisit son cobaye et restaure son
#    état dans un `finally`, intention comprise.
#
#   $ ./venv/bin/python tools/verif_nmos_script_enabled.py [vmid]
import json
import os
import sys
import time
import urllib.request

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

BASE = "http://127.0.0.1:5000/x-nmos/configuration/v1.0/rolePaths"
ENABLED = "2p1"                      # NcWorker.enabled — inscriptible (NcBlock.enabled ne l'est pas)
echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


def _http(url, methode="GET", corps=None):
    d = json.dumps(corps).encode() if corps is not None else None
    r = urllib.request.Request(url, data=d, method=methode,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as rep:
            return rep.status, json.loads(rep.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None


def lire(chemin):
    c, j = _http("%s/%s/properties/%s/value" % (BASE, chemin, ENABLED))
    return (j or {}).get("value") if c == 200 else None


def ecrire(chemin, v):
    """(statut MS-05-02, message). ⚠ LE STATUT EST DANS LE CORPS, pas dans le code HTTP : IS-14
    répond 200 même pour un refus, et c'est le `status` du NcMethodResult qui fait foi. Première
    version de ce banc : elle lisait le code HTTP et voyait donc 200 partout — elle déclarait un
    défaut du produit là où le produit refusait correctement."""
    _c, j = _http("%s/%s/properties/%s/value" % (BASE, chemin, ENABLED), "PUT", {"value": v})
    j = j or {}
    return j.get("status"), j.get("errorMessage")


from app.database import db_get_container, db_script_enabled, db_set_script_enabled  # noqa: E402
from app import deploy                                                              # noqa: E402

print("NcWorker.enabled — pilotage du script depuis un contrôleur NMOS\n")

code, chemins = _http(BASE + "/")
if code != 200:
    print("  SAUTÉ  IS-14 fermé ou orchestrateur injoignable (HTTP %s)" % code)
    sys.exit(0)
scripts = [c.rstrip("/") for c in (chemins or []) if c.rstrip("/").endswith(".script")]
controle("★ les scripts sont publiés en NcWorker", bool(scripts),
         "sans eux, aucun contrôleur ne peut démarrer ni arrêter un traitement")
if not scripts:
    sys.exit(1)

VMID = int(sys.argv[1]) if len(sys.argv) > 1 else int(scripts[0].split(".")[-2].split("_")[-1])
CHEMIN = next(c for c in scripts if ("plugin_%d." % VMID) in c)
print("  cobaye : vmid %s (%s)\n" % (VMID, CHEMIN))

_intention_avant = db_script_enabled(VMID)
_tournait = deploy._agent_script_running((db_get_container(VMID) or {}).get("ip"), VMID)
print("  état de départ : script %s, intention %s\n"
      % ("en marche" if _tournait else "arrêté", _intention_avant))

try:
    controle("la lecture rend le CONSTAT, pas une valeur en dur", lire(CHEMIN) == _tournait,
             "obtenu %r alors que l'agent dit %r" % (lire(CHEMIN), _tournait))

    # ── Arrêt ────────────────────────────────────────────────────────────────
    c, msg = ecrire(CHEMIN, False)
    controle("l'écriture de `enabled=false` est acceptée", c == 200, "statut %s (%s)" % (c, msg))
    time.sleep(6)
    ip = (db_get_container(VMID) or {}).get("ip")
    controle("★★ le script s'est RÉELLEMENT arrêté",
             not deploy._agent_script_running(ip, VMID),
             "consigne acceptée sans effet = pilotage fantôme")
    controle("et la propriété le reflète", lire(CHEMIN) is False)
    controle("★ l'intention est PERSISTÉE en base", db_script_enabled(VMID) is False,
             "sans persistance, le prochain déploiement la défait en silence")

    # ── Le piège : un redéploiement ne doit PAS rallumer ──────────────────────
    from app.deploy import deployer_script
    c_ = db_get_container(VMID)
    dc = json.loads(c_["deploy_config"] or "{}")
    deployer_script(VMID, dc["type"], dc.get("params") or {})
    time.sleep(6)
    controle("★★★ un REDÉPLOIEMENT ne rallume pas un script désactivé",
             not deploy._agent_script_running(ip, VMID),
             "l'orchestrateur fait /stop puis /start : sans la garde sur l'intention, la consigne "
             "du contrôleur serait défaite ici, et personne ne le verrait")

    # ── Redémarrage ──────────────────────────────────────────────────────────
    c, msg = ecrire(CHEMIN, True)
    controle("l'écriture de `enabled=true` est acceptée", c == 200, "statut %s (%s)" % (c, msg))
    time.sleep(6)
    controle("★★ le script est REPARTI", deploy._agent_script_running(ip, VMID))
    controle("l'intention est revenue à vrai", db_script_enabled(VMID) is True)

    # ── NcBlock.enabled doit rester en LECTURE SEULE ─────────────────────────
    bloc = CHEMIN.rsplit(".", 1)[0]
    c, msg = ecrire(bloc, False)
    controle("★ écrire `enabled` sur le BLOC est refusé (405)", c == 405,
             "MS-05-02 : NcBlock.enabled est en lecture seule, seul NcWorker.enabled est "
             "inscriptible — c'est la raison d'être de l'objet Script. Obtenu %s" % c)
finally:
    db_set_script_enabled(VMID, _intention_avant)
    if _tournait and not deploy._agent_script_running(
            (db_get_container(VMID) or {}).get("ip"), VMID):
        ecrire(CHEMIN, True)
        time.sleep(3)
    print("\n  restauré : intention %s, script %s"
          % (db_script_enabled(VMID),
             "en marche" if deploy._agent_script_running(
                 (db_get_container(VMID) or {}).get("ip"), VMID) else "arrêté"))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
