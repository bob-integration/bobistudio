# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Registre de pairs (flotte d'instances Bobi.Studio) — CRUD + pull/push/preview."""

import json as _json
import urllib.request

from flask import jsonify, request

from . import bp
from .updates import _my_identity
from ..auth import require_perm


@bp.route("/api/peers", methods=["GET"])
@require_perm("settings.edit")
def peers_list():
    from .. import peers
    return jsonify({"peers": peers.list_peers(), "self": _my_identity()})

@bp.route("/api/peers", methods=["POST"])
@require_perm("settings.edit")
def peers_add():
    from .. import peers
    data = request.json or {}
    try:
        p = peers.add_peer(data.get("name"), data.get("url"), data.get("token"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "peer": p})

@bp.route("/api/peers/<int:pid>", methods=["PATCH"])
@require_perm("settings.edit")
def peers_update(pid):
    from .. import peers
    data = request.json or {}
    return jsonify({"ok": True, "peer": peers.update_peer(pid, name=data.get("name"), token=data.get("token"))})

@bp.route("/api/peers/<int:pid>", methods=["DELETE"])
@require_perm("settings.edit")
def peers_delete(pid):
    from .. import peers
    peers.delete_peer(pid)
    return jsonify({"ok": True})

@bp.route("/api/peers/refresh", methods=["POST"])
@require_perm("settings.edit")
def peers_refresh():
    from .. import peers
    return jsonify({"peers": peers.refresh_all()})

@bp.route("/api/peers/discover", methods=["POST"])
@require_perm("settings.edit")
def peers_discover():
    from .. import peers, settings as st
    data = request.json or {}
    cidr = (data.get("cidr") or "").strip()
    if not cidr:
        # ⚠ PAS DE REPLI CODÉ EN DUR. Il portait une adresse de SITE — donc une valeur
        # juste sur une installation et fausse sur toutes les autres, sans que rien
        # ne le dise. Sans réglage, on rend une chaîne vide : l'absence se traite,
        # une mauvaise adresse se diagnostique.
        gw = st.get("gateway") or ""
        bits = int(st.get("netmask_bits") or 24)
        cidr = f"{gw}/{bits}"
    try:
        found = peers.discover(cidr)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "cidr": cidr, "found": found})

@bp.route("/api/peers/<int:pid>/pull", methods=["POST"])
@require_perm("settings.edit")
def peers_pull(pid):
    """Met à jour CETTE instance depuis le pair pid."""
    from .. import peers, updater
    p = peers.get_peer(pid)
    if not p:
        return jsonify({"ok": False, "error": "pair inconnu"}), 404
    _inew = (request.json or {}).get("install_new") if request.is_json else None
    ok, msg = updater.apply_update(p["url"], p.get("token") or "", install_new=_inew)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 500)

@bp.route("/api/peers/<int:pid>/push", methods=["POST"])
@require_perm("settings.edit")
def peers_push(pid):
    """Met à jour le pair pid depuis CETTE instance (déclenche son pull vers nous)."""
    from .. import peers, settings as st
    p = peers.get_peer(pid)
    if not p:
        return jsonify({"ok": False, "error": "pair inconnu"}), 404
    if not st.get("update_server_enabled") or not st.get("update_token"):
        return jsonify({"ok": False, "error": "active d'abord le mode serveur (token) sur cette instance"}), 400
    my_url = request.host_url.rstrip("/")
    # Opt-in de NOUVEAUX composants pour la cible (composition par instance) : relayé tel
    # quel au /api/update/apply du pair, qui l'applique lors de son pull vers nous.
    _inew = (request.json or {}).get("install_new") if request.is_json else None
    body = _json.dumps({"source_url": my_url, "token": st.get("update_token"),
                        "install_new": _inew}).encode()
    req = urllib.request.Request(p["url"].rstrip("/") + "/api/update/apply", data=body,
                                 headers={"Content-Type": "application/json",
                                          "X-MXL-Update-Token": p.get("token") or ""})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:   # noqa: S310
            res = _json.loads(r.read().decode())
    except Exception as e:
        return jsonify({"ok": False, "error": f"pair injoignable : {e}"}), 502
    return jsonify(res)

@bp.route("/api/peers/<int:pid>/preview", methods=["GET"])
@require_perm("settings.edit")
def peers_preview(pid):
    """Aperçu (lecture seule) de ce que ferait un Pull/Push : versions source/cible
    + diff des plugins/services. N'applique rien."""
    from .. import peers, updater, settings as st
    p = peers.get_peer(pid)
    if not p:
        return jsonify({"ok": False, "error": "pair inconnu"}), 404
    direction = request.args.get("direction", "pull")
    if direction not in ("pull", "push"):
        return jsonify({"ok": False, "error": "direction invalide"}), 400

    warning = None
    can_apply = True
    reason = None

    # Manifeste local (toujours dispo) et celui du pair (peut échouer si injoignable).
    local = updater.current_manifest()
    try:
        remote = updater.fetch_manifest(p["url"], p.get("token") or "")
    except Exception as e:
        remote = {}
        warning = f"pair injoignable : {e}"

    if direction == "pull":
        # On tire le pair → cette instance redémarre. Ancien = local, nouveau = pair.
        old, new = local, remote
        target = {"label": (local.get("label") or "?"), "url": request.host_url.rstrip("/"),
                  "is_local": True}
        source = {"label": remote.get("label") or "?", "git_hash": remote.get("git_hash"),
                  "built_at": remote.get("built_at")}
        if warning:
            can_apply, reason = False, warning   # rien à tirer si le pair ne répond pas
    else:  # push
        # On pousse vers le pair → le pair redémarre. Ancien = pair, nouveau = local.
        old, new = remote, local
        target = {"label": remote.get("label") or "?", "url": p["url"], "is_local": False}
        source = {"label": local.get("label") or "?", "git_hash": local.get("git_hash"),
                  "built_at": local.get("built_at")}
        if not st.get("update_server_enabled") or not st.get("update_token"):
            can_apply, reason = False, "active d'abord le mode serveur (token) sur cette instance"

    # Si le pair est injoignable, son manifeste est vide → un diff serait trompeur
    # (tout « ajouté » ou « retiré »). On ne calcule le diff que si les deux côtés sont là.
    if warning:
        diff = {"components": [], "counts": {"added": 0, "updated": 0,
                                             "removed": 0, "unchanged": 0}}
    else:
        diff = updater.diff_manifests(old, new)
    same_build = bool(old.get("build_id") and new.get("build_id")
                      and old.get("build_id") == new.get("build_id"))
    return jsonify({
        "ok": True,
        "direction": direction,
        "target": target,
        "source": source,
        "same_build": same_build,
        "components": diff["components"],
        "counts": diff["counts"],
        "package": {"sha256": new.get("sha256"), "size": new.get("size")},
        "warning": warning,
        "can_apply": can_apply,
        "reason": reason,
    })
