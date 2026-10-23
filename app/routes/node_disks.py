# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Disques locaux d'un nœud : routes du contrôleur (Réglages → Médias → Stockage).

Le contrôleur RELAIE, il ne décide pas : disque système, partition montée, série et taille
saisies sont revérifiés par l'AGENT-NŒUD à l'exécution (cf. NODE_AGENT.md §4.7). Ici : droits
(admin, comme les autres host-ops), liste blanche des champs transmis, et JOURNAL — chaque geste
destructif, réussi, refusé ou raté, laisse une alerte structurée (node_id, kind="disk").

Le redémarrage n'est jamais automatique : `reboot_required` remonte à l'interface, et c'est
l'opérateur qui clique (`/reboot`, `confirm:true`).
"""

from flask import jsonify, request

from . import bp
from .. import node_driver
from ..auth import require_perm
from ..database import db_add_alert, db_get_node

# Champs que l'on laisse passer jusqu'à l'agent. Tout le reste est jeté : le contrôleur ne
# fabrique ni n'élargit jamais une demande.
_FIELDS = ("action", "device", "partition", "role", "mountpoint", "label", "minutes",
           "allow_non_empty", "ack_media_containers", "confirm", "confirm_serial", "confirm_size")
_ACTIONS = ("format", "mount", "unmount", "spindown")


def _node_or_error(node_id):
    node = db_get_node(node_id)
    if not node:
        return None, (jsonify({"ok": False, "error": "node_not_found"}), 404)
    if not node_driver.has_agent(node):
        return None, (jsonify({"ok": False, "error": "no_agent"}), 409)
    return node, None


def _body():
    raw = request.get_json(silent=True) or {}
    return {k: raw[k] for k in _FIELDS if k in raw}


def _agent_answer(ok, data):
    """Réponse d'agent -> (corps JSON, code). Un agent trop ancien n'a pas la route : on le dit."""
    if not ok:
        msg = str(data)
        code = "agent_too_old" if "route inconnue" in msg or "404" in msg else "agent_unreachable"
        return {"ok": False, "error": code, "detail": msg[:200]}, 502
    return data, 200


def _journal(node, req, res):
    """Alerte structurée d'un geste destructif. Jamais de secret : disque, série, cible."""
    action = req.get("action")
    if action not in _ACTIONS:
        return
    params = {"n": node.get("name") or node["id"], "dev": req.get("device") or "?",
              "serial": (res.get("disk") or {}).get("serial") or req.get("confirm_serial") or "?",
              "target": res.get("target") or req.get("mountpoint") or ""}
    kw = dict(node_id=node["id"], kind="disk", params=params)
    if res.get("ok"):
        if res.get("changed"):
            db_add_alert("alert.storage.%s_ok" % action, "info", **kw)
        if res.get("reboot_required"):
            db_add_alert("alert.storage.reboot_required", "warning", **kw)
    elif res.get("error") == "refused":
        params["why"] = ",".join(res.get("refusals") or [])
        db_add_alert("alert.storage.refused", "warning", **kw)
    else:
        params["why"] = "%s %s" % (res.get("error") or "?", res.get("detail") or "")
        db_add_alert("alert.storage.failed", "error", **kw)
        if res.get("reboot_required"):
            db_add_alert("alert.storage.reboot_required", "warning", **kw)


@bp.route("/api/nodes/<int:node_id>/disks", methods=["GET"])
@require_perm("settings.edit")
def node_disks_list(node_id):
    node, err = _node_or_error(node_id)
    if err:
        return err
    body, code = _agent_answer(*node_driver.disks_list(node))
    return jsonify(body), code


@bp.route("/api/nodes/<int:node_id>/disks/plan", methods=["POST"])
@require_perm("settings.edit")
def node_disks_plan(node_id):
    node, err = _node_or_error(node_id)
    if err:
        return err
    body, code = _agent_answer(*node_driver.disks_plan(node, _body()))
    return jsonify(body), code


@bp.route("/api/nodes/<int:node_id>/disks/execute", methods=["POST"])
@require_perm("settings.edit")
def node_disks_execute(node_id):
    node, err = _node_or_error(node_id)
    if err:
        return err
    req = _body()
    if req.get("action") not in _ACTIONS:
        return jsonify({"ok": False, "error": "bad_request"}), 400
    body, code = _agent_answer(*node_driver.disks_execute(node, req))
    if code == 200 and isinstance(body, dict):
        _journal(node, req, body)
    else:
        _journal(node, req, {"ok": False, "error": body.get("error"), "detail": body.get("detail")})
    return jsonify(body), code


@bp.route("/api/nodes/<int:node_id>/reboot", methods=["POST"])
@require_perm("settings.edit")
def node_reboot(node_id):
    """Redémarre le nœud — UNIQUEMENT sur geste explicite (`confirm:true`). Refusé si des processus
    sont bloqués dans le noyau, sauf `force:true` (redémarrage sysrq) — cf. app/node_power.py."""
    from .. import node_power
    node, err = _node_or_error(node_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"ok": False, "error": "confirm_required"}), 400
    body, code = node_power.reboot(node, force=bool(data.get("force")))
    if body.get("ok"):
        db_add_alert("alert.storage.reboot_requested", "warning", node_id=node["id"], kind="node",
                     params={"n": node.get("name") or node["id"]})
    return jsonify(body), code
