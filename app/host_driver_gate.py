# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Conteneurs qui exigent un PILOTE HÔTE (Blackmagic Desktop Video…) : attendre plutôt que mourir.

★ LE CAS (Z8 neuve, 2026-10-30). 15:04:10 : un `decklink_io` est créé sur un nœud sans pilote —
l'agent-nœud le savait (`install_needs_file`, `driver_missing_upload_needed`). 15:04:14 : le
conteneur meurt, l'alerte dit seulement « DISPARU du nœud ». Pilote installé à 15:08, redémarrage
du nœud à 15:13 demandé par l'interface elle-même — et au retour, rien ne relance le conteneur.

DÉCISION DE CYRIL. Ne PAS refuser la création : le dépôt du pilote se fait dans la page du plugin,
qui n'existe que si le conteneur existe. Donc :
  1. création sans pilote → le conteneur est ENREGISTRÉ (type + params) mais pas lancé :
     `desired_state='awaiting_driver'`. Pas de `docker run` qui meurt en 4 s, pas de « disparu »
     (fleet_status et la reprise ne regardent que `desired_state='running'`) ;
  2. retour du nœud, pilote présent → démarrage de lui-même, MÊME auto-recovery désactivé. Comme la
     re-provision des certificats : prouvé (l'agent-nœud dit le pilote prêt), ciblé (seulement les
     conteneurs qui l'attendaient), une fois par boot ;
  3. un conteneur qui disparaît alors que l'agent-nœud connaît la cause → l'alerte la nomme.

Le plugin DÉCLARE son pilote dans son manifeste — le cœur ne connaît aucun kind :
    "host_driver": {"kind": "blackmagic",
                    "missing_status": ["install_needs_file", "missing_prereqs", "install", "no_tool"]}
`missing_status` = les statuts du script d'état (`node_agent/host_drivers/<kind>.py state`) qui
veulent dire « pilote absent ou inutilisable ». Un statut hors liste (ok, update, no_card…) n'est
PAS une absence de pilote : on laisse le conteneur partir, son propre diagnostic dira le reste.

État INCONNU (agent ancien, nœud muet, script en échec) → on ne bloque RIEN : on ne retient un
conteneur que sur une PREUVE d'absence, sinon une panne de l'agent-nœud empêcherait toute création.
"""
import json
import logging
import time

from . import plugins
from .database import db_add_alert, db_get_container, db_get_containers, db_get_node, get_db

log = logging.getLogger(__name__)

AWAITING = "awaiting_driver"
_STATE_TIMEOUT_S = 30


def required_driver(type_):
    """(kind, missing_status) déclarés par le manifeste du plugin, ou None."""
    try:
        man = plugins.get(type_) or {}
    except Exception:
        return None
    hd = man.get("host_driver")
    if not isinstance(hd, dict) or not hd.get("kind"):
        return None
    return str(hd["kind"]), set(hd.get("missing_status") or ())


def driver_state(node, kind):
    """Document d'état du pilote sur le nœud, ou None si on ne SAIT pas (jamais d'exception)."""
    from . import node_driver
    try:
        if not node_driver.has_agent(node):
            return None
        try:
            from .routes.host_drivers import push_scripts
            push_scripts(node, only=kind)       # un nœud neuf n'a pas encore le script d'état
        except Exception as e:
            log.debug("host driver %s: push to node %s: %s", kind, node.get("id"), e)
        ok, data = node_driver.host_driver_call(node, "GET", "/v1/host/drivers/%s/state" % kind,
                                                timeout=_STATE_TIMEOUT_S)
        return data if ok and isinstance(data, dict) else None
    except Exception as e:
        log.warning("host driver %s state on node %s: %s", kind, node.get("id"), e)
        return None


def missing_cause(type_, node):
    """Code de cause si le pilote requis est PROUVÉ absent sur ce nœud, sinon None."""
    req = required_driver(type_)
    if not req or not node:
        return None
    kind, missing = req
    doc = driver_state(node, kind)
    if not doc or doc.get("status") not in missing:
        return None
    return kind, ((doc.get("plan") or {}).get("code") or doc.get("status"))


def _set_desired(vmid, state):
    # `db_set_desired_state` n'accepte que running/stopped : l'attente du pilote est un troisième
    # état VOULU, écrit ici seulement (et effacé par tout déploiement, qui repose « running »).
    with get_db() as db:
        db.execute("UPDATE containers SET desired_state=? WHERE vmid=?", (state, vmid))
        db.commit()


def hold_if_missing(vmid, type_, params):
    """Appelé par deploy.deployer_script AVANT tout `docker run`. True = conteneur mis en attente
    du pilote (rien n'a été lancé) ; False = on déploie normalement."""
    try:
        c = db_get_container(vmid) or {}
        node = db_get_node(c.get("node_id")) if c.get("node_id") is not None else None
        cause = missing_cause(type_, node)
        if not cause:
            return False
        kind, code = cause
        from .database import db_update_deploy_config, db_update_status
        db_update_deploy_config(vmid, type_, params or {})   # le démarrage différé repart d'ici
        _set_desired(vmid, AWAITING)
        db_update_status(vmid, AWAITING, cause=code)
        db_add_alert("alert.deploy.awaiting_host_driver", "warning", vmid=vmid,
                     node_id=node.get("id"), kind="deploy",
                     params={"h": c.get("hostname") or vmid, "k": kind, "code": code,
                             "n": node.get("name") or node.get("id")})
        return True
    except Exception as e:
        log.warning("host driver gate vmid=%s: %s", vmid, e)
        return False


def resume_after_boot(node_id, name, grace_s=45):
    """Une passe par boot (appelée une fois à la détection du reboot) : démarre les conteneurs
    qui attendaient leur pilote, si l'agent-nœud le dit maintenant présent. UNE tentative."""
    try:
        waiting = [c for c in db_get_containers()
                   if c.get("node_id") == node_id and c.get("desired_state") == AWAITING]
        if not waiting:
            return []
        time.sleep(max(0, grace_s))         # laisser l'agent-nœud et le module noyau se lever
        node = db_get_node(node_id) or {}
        from .deploy import deployer_script
        started, still, failed = [], [], []
        for c in waiting:
            vmid = c["vmid"]
            try:
                dc = json.loads(c.get("deploy_config") or "{}") or {}
            except (TypeError, ValueError):
                dc = {}
            type_ = dc.get("type")
            req = required_driver(type_) if type_ else None
            if not req:
                continue
            doc = driver_state(node, req[0])
            if not doc or doc.get("status") in req[1]:
                still.append(vmid)          # toujours absent, ou on ne sait pas : on n'essaie pas
                continue
            ok = bool(deployer_script(vmid, type_, dc.get("params") or {}))
            (started if ok else failed).append(vmid)
            time.sleep(1)
        if started or failed:
            db_add_alert("alert.deploy.host_driver_started" if not failed
                         else "alert.deploy.host_driver_started_failures",
                         "info" if not failed else "error", node_id=node_id, kind="deploy",
                         params={"n": name, "n_started": len(started), "n_failed": len(failed),
                                 "vmids": ", ".join(map(str, failed or started))})
        return started
    except Exception as e:
        log.error("host driver resume on node %s: %s", node_id, e)
        return []
