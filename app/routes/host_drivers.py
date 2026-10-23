# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Pilotes hôte d'un nœud — relais GÉNÉRIQUE vers l'agent (`/v1/host/drivers/*`, agent >= 0.28.0).

Un « kind » (aujourd'hui `nvidia`) est un script `node_agent/host_drivers/<kind>.py` que le
contrôleur dépose sur le nœud puis pilote par l'agent : état + plan, lancement d'un job long, journal
lisible, action en attente (redémarrage / arrêt complet). RIEN ici ne nomme un kind : en ajouter un,
c'est déposer son script dans `node_agent/host_drivers/` et ses libellés `settings.hostdrv.<kind>.*`.
Cf. NODE_AGENT.md §4.6.

Le redémarrage réutilise la route existante de la préparation MTL (`/api/mtl/reboot?node_id=`,
`confirm:true`) ; seul l'ARRÊT COMPLET est nouveau (`host-poweroff`), car il pose le marqueur qui
permet à l'agent de lever une action « arrêt complet » en attente.
"""

import hashlib
import logging
import os
import re
import shlex

from flask import jsonify, request

from . import bp
from ..auth import require_perm
from ..database import db_get_node, db_add_alert

log = logging.getLogger(__name__)

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_AGENT_DIR = "/opt/bobi-node-agent/host_drivers"
_UPLOAD_MAX = 2 << 30


def _drivers_dir():
    from .images import _repo_root
    return os.path.join(_repo_root(), "node_agent", "host_drivers")


def available_kinds():
    """Kinds livrés avec ce contrôleur (un script = un kind)."""
    try:
        return sorted(f[:-3] for f in os.listdir(_drivers_dir())
                      if f.endswith(".py") and _KIND_RE.match(f[:-3]))
    except OSError:
        return []


def _script_path(kind):
    return os.path.join(_drivers_dir(), kind + ".py") if _KIND_RE.match(kind or "") else None


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def push_scripts(node, only=None):
    """Dépose sur le nœud les scripts de kind absents ou périmés (sha256), via /v1/host/exec.
    `py_compile` AVANT de remplacer : un script cassé ne remplace jamais le bon. (ok, msg)."""
    from .. import node_driver
    kinds = [only] if only else available_kinds()
    for kind in kinds:
        path = _script_path(kind)
        if not path or not os.path.isfile(path):
            return False, "unknown kind %s" % kind
        with open(path, encoding="utf-8") as f:
            content = f.read()
        want = hashlib.sha256(content.encode()).hexdigest()
        dest = "%s/%s.py" % (_AGENT_DIR, kind)
        cmd = ("set -e; mkdir -p %s; if [ -f %s ] && [ \"$(sha256sum %s | cut -d' ' -f1)\" = %s ]; then "
               "echo SAME; exit 0; fi; T=%s.new; cat > \"$T\"; python3 -m py_compile \"$T\"; "
               "chmod 0755 \"$T\"; mv \"$T\" %s; echo OK"
               % (_AGENT_DIR, shlex.quote(dest), shlex.quote(dest), want, shlex.quote(dest),
                  shlex.quote(dest)))
        rc, out, err = node_driver.host_exec(node, cmd, input_data=content, timeout=60)
        if rc != 0 or not any(w in (out or "") for w in ("OK", "SAME")):
            return False, ((err or out or "").strip()[:300] or "rc=%s" % rc)
    return True, "ok"


def _node_or_404(node_id):
    from .. import node_driver
    node = db_get_node(node_id)
    if not node:
        return None, (jsonify({"ok": False, "error": "node_not_found"}), 404)
    if not node_driver.has_agent(node):
        return None, (jsonify({"ok": False, "error": "no_agent"}), 400)
    return node, None


def _kind_or_400(kind):
    if kind not in available_kinds():
        return jsonify({"ok": False, "error": "unknown_kind", "kind": kind}), 400
    return None


def _agent_error(data):
    """Un agent ANTÉRIEUR à 0.22.0 répond 404 « route inconnue » : on le dit en code, l'UI propose la
    mise à jour de l'agent (bouton existant), plutôt qu'un « erreur 404 » sans issue."""
    txt = str(data)
    if "404" in txt or "route inconnue" in txt:
        return {"ok": False, "error": "agent_too_old"}
    return {"ok": False, "error": "agent_error", "detail": txt[:300]}


@bp.route("/api/nodes/<int:node_id>/host-drivers", methods=["GET"])
@require_perm("settings.edit")
def api_host_drivers_list(node_id):
    node, err = _node_or_404(node_id)
    if err:
        return err
    return jsonify({"ok": True, "kinds": available_kinds()})


@bp.route("/api/nodes/<int:node_id>/host-drivers/<kind>/state", methods=["GET"])
@require_perm("settings.edit")
def api_host_driver_state(node_id, kind):
    from .. import node_driver
    node, err = _node_or_404(node_id)
    if err:
        return err
    bad = _kind_or_400(kind)
    if bad:
        return bad
    ok, msg = push_scripts(node, only=kind)
    if not ok:
        return jsonify({"ok": False, "error": "push_failed", "detail": msg}), 502
    ok, data = node_driver.host_driver_call(node, "GET", "/v1/host/drivers/%s/state" % kind, timeout=120)
    if not ok:
        return jsonify(_agent_error(data)), 502
    return jsonify(data)


@bp.route("/api/nodes/<int:node_id>/host-drivers/<kind>/run", methods=["POST"])
@require_perm("settings.edit")
def api_host_driver_run(node_id, kind):
    from .. import node_driver
    node, err = _node_or_404(node_id)
    if err:
        return err
    bad = _kind_or_400(kind)
    if bad:
        return bad
    ok, msg = push_scripts(node, only=kind)
    if not ok:
        return jsonify({"ok": False, "error": "push_failed", "detail": msg}), 502
    ok, data = node_driver.host_driver_call(node, "POST", "/v1/host/drivers/%s/run" % kind, body={},
                                            timeout=30)
    if not ok:
        return jsonify(_agent_error(data)), (409 if "409" in str(data) else 502)
    db_add_alert("alert.node.host_driver_start", "info", node_id=node_id, kind="node",
                 params={"n": node.get("name"), "k": kind})
    return jsonify(data)


@bp.route("/api/nodes/<int:node_id>/host-drivers/<kind>/job", methods=["GET"])
@require_perm("settings.edit")
def api_host_driver_job(node_id, kind):
    from .. import node_driver
    node, err = _node_or_404(node_id)
    if err:
        return err
    bad = _kind_or_400(kind)
    if bad:
        return bad
    try:
        off = int(request.args.get("offset") or 0)
    except ValueError:
        off = 0
    ok, data = node_driver.host_driver_call(node, "GET", "/v1/host/drivers/%s/job?offset=%d" % (kind, off),
                                            timeout=20)
    if not ok:
        return jsonify(_agent_error(data)), 502
    return jsonify(data)


@bp.route("/api/nodes/<int:node_id>/host-drivers/<kind>/upload", methods=["POST"])
@require_perm("settings.edit")
def api_host_driver_upload(node_id, kind):
    """Téléverse un installeur (nœud sans Internet) : le corps binaire est relayé à l'agent par
    morceaux, sans jamais être tenu en mémoire."""
    from .. import node_driver
    node, err = _node_or_404(node_id)
    if err:
        return err
    bad = _kind_or_400(kind)
    if bad:
        return bad
    name = request.args.get("name") or ""
    length = request.content_length or 0
    if not _NAME_RE.match(name):
        return jsonify({"ok": False, "error": "bad_name"}), 400
    if length <= 0 or length > _UPLOAD_MAX:
        return jsonify({"ok": False, "error": "bad_size"}), 400
    ok, data = node_driver.host_driver_upload(node, kind, name, request.stream, length)
    if not ok:
        return jsonify(data if isinstance(data, dict) else _agent_error(data)), 502
    return jsonify(data)


@bp.route("/api/nodes/<int:node_id>/host-poweroff", methods=["POST"])
@require_perm("settings.edit")
def api_host_poweroff(node_id):
    """ARRÊT COMPLET du nœud (jamais automatique : `confirm:true` exigé). Le redémarrage, lui, passe par
    `/api/mtl/reboot?node_id=` (route existante de la préparation MTL)."""
    from .. import node_driver
    node, err = _node_or_404(node_id)
    if err:
        return err
    if not (request.get_json(silent=True) or {}).get("confirm"):
        return jsonify({"ok": False, "error": "confirm_required"}), 400
    # Un processus bloqué dans le noyau empêcherait l'arrêt de finir : machine perdue, ni éteinte
    # ni joignable (cf. app/node_power.py). Refus explicite, avant de toucher à l'agent.
    from .. import node_power
    refus = node_power.poweroff_guard(node)
    if refus:
        return jsonify(refus[0]), refus[1]
    ok, data = node_driver.host_driver_call(node, "POST", "/v1/host/poweroff", body={"confirm": True},
                                            timeout=20)
    if not ok:
        return jsonify(_agent_error(data)), 502
    db_add_alert("alert.node.host_poweroff", "warning", node_id=node_id, kind="node",
                 params={"n": node.get("name")})
    return jsonify(data)
