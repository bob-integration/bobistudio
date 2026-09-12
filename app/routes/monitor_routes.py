# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Monitoring WebRTC par utilisateur (app/monitor.py) : un container `streamer` dédié par
utilisateur (hostname `monitor-u<uid>`), créé à la demande, re-pointable sur n'importe quel shm
depuis les pages productrices (panneau global du topnav). + monitor dédié par player (page
Médias : lecture continue sans passer par le monitor personnel)."""

from flask import jsonify, request, Response, stream_with_context

from . import bp
from ..auth import (require_login, require_perm, current_user, has_global_access,
                    check_vmid_access)


def _shm_access_err(*shms):
    """Garde scoping du monitoring : pour un utilisateur non-global, chaque shm demandé
    doit être produit par un container accessible (cf. check_vmid_access). Le shm est
    redérivé côté serveur depuis les manifestes (derive_wiring) — on ne fait jamais
    confiance au client. Renvoie None si OK, sinon la réponse 403 à retourner."""
    if has_global_access():
        return None
    from .. import plugins as _plugins
    from ..database import db_get_containers
    from .shared import _load_dc
    want = {s for s in shms if s}
    if not want:
        return None
    for c in db_get_containers():
        dc = _load_dc(c) or {}
        kind = dc.get("type")
        if not _plugins.is_plugin(kind):
            continue
        p = dc.get("params") or {}
        hn = p.get("hostname") or c.get("hostname") or f"mxl{c['vmid']}"
        try:
            produced = {pr.get("shm") for pr in
                        _plugins.derive_wiring(kind, hn, p)["produces"]}
        except Exception:
            continue
        for s in list(want & produced):
            if check_vmid_access(c["vmid"], "viewer") is None:
                want.discard(s)
        if not want:
            return None
    return (jsonify({"error": "forbidden", "reason": "shm_not_accessible",
                     "shm": sorted(want)}), 403)


@bp.route("/monitor/popout", methods=["GET"])
@require_login
def monitor_popout():
    """Monitoring DÉTACHÉ dans une fenêtre à part. Page servie par NOUS (et pas l'iframe de la
    passerelle directement) parce qu'elle doit porter SON PROPRE heartbeat : le lecteur est d'une
    autre origine, on ne peut pas y injecter de code, et l'encodeur est tué après IDLE_TIMEOUT sans
    heartbeat (monitor.py, reaper) — la fenêtre détachée survivrait donc mal à la fermeture ou à la
    navigation de la page d'origine."""
    from flask import render_template
    return render_template("monitor_popout.html")


@bp.route("/api/monitor/status", methods=["GET"])
@require_login
def monitor_status():
    from .. import monitor
    uid = (current_user() or {}).get("id")
    monitor.touch(uid)
    return jsonify(monitor.status(uid))

@bp.route("/api/monitor/create", methods=["POST"])
@require_login
def monitor_create():
    """Crée le container monitor de l'utilisateur + déploie l'encodeur (streamé)."""
    from .. import monitor
    uid = (current_user() or {}).get("id")
    def gen():
        try:
            for line in monitor.create_iter(uid):
                yield line + "\n"
        except Exception as e:
            yield f"❌ Erreur inattendue : {e}\n"
    return Response(stream_with_context(gen()),
                    mimetype="text/plain; charset=utf-8",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

@bp.route("/api/monitor/source", methods=["POST"])
@require_login
def monitor_source():
    from .. import monitor
    uid = (current_user() or {}).get("id")
    body = request.get_json(force=True, silent=True) or {}
    shm = (body.get("shm") or "").strip()
    if not shm:
        return jsonify({"error": "shm manquant"}), 400
    audio_shm = (body.get("audio_shm") or "").strip() or None
    err = _shm_access_err(shm, audio_shm)
    if err:
        return err
    return jsonify(monitor.set_source(uid, shm, body.get("label"), audio_shm))

@bp.route("/api/monitor/activate", methods=["POST"])
@require_login
def monitor_activate():
    from .. import monitor
    uid = (current_user() or {}).get("id")
    return jsonify(monitor.activate(uid))

@bp.route("/api/monitor/heartbeat", methods=["POST"])
@require_login
def monitor_heartbeat():
    from .. import monitor
    monitor.touch((current_user() or {}).get("id"))
    return jsonify({"ok": True})

@bp.route("/api/player/<int:vmid>/dedicated_monitor", methods=["GET"])
@require_login
def player_dedicated_monitor_status(vmid):
    """Statut du monitor dédié au player vmid."""
    from .. import monitor
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    return jsonify(monitor.dedicated_status(vmid))

@bp.route("/api/player/<int:vmid>/dedicated_monitor", methods=["POST"])
@require_perm("containers.create")
def player_dedicated_monitor_create(vmid):
    """Crée le monitor dédié au player vmid (réponse streamée ligne par ligne)."""
    from .. import monitor
    def gen():
        try:
            for line in monitor.create_dedicated_iter(vmid):
                yield line + "\n"
        except Exception as e:
            yield f"❌ Erreur inattendue : {e}\n"
    return Response(stream_with_context(gen()),
                    mimetype="text/plain; charset=utf-8",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

# ─── Aperçu dédié d'un conteneur quelconque ────────────────────────────────────────────────
# Généralisation des deux routes player ci-dessus. Le player est un cas particulier où la source
# se DÉDUIT du hostname ; ailleurs il faut la dire. Un scope, par exemple, ne produit rien : ce
# qu'on veut voir est ce qu'il MESURE, une source câblée ailleurs. L'appelant qui connaît cette
# source la passe ; le backend ne devine pas.

@bp.route("/api/containers/<int:vmid>/preview", methods=["GET"])
@require_login
def container_preview_status(vmid):
    """Statut de l'aperçu WebRTC dédié au conteneur vmid."""
    from .. import monitor
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    st = monitor.dedicated_status(vmid)
    # `gateway_ready` est DANS la réponse : sans passerelle, l'UI doit dire pourquoi le bouton
    # ne servira à rien, pas laisser l'exploitant cliquer et attendre.
    st["gateway_ready"] = monitor.gateway_ready()
    return jsonify(st)

@bp.route("/api/containers/<int:vmid>/preview", methods=["POST"])
@require_perm("containers.create")
def container_preview_create(vmid):
    """Crée (ou re-pointe) l'aperçu WebRTC dédié au conteneur vmid.
    Corps : {shm, audio_shm?} — la source à encoder. Réponse streamée ligne par ligne."""
    from .. import monitor
    err = check_vmid_access(vmid, "operator")
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    shm = (body.get("shm") or "").strip()
    audio_shm = (body.get("audio_shm") or "").strip() or None
    def gen():
        if not shm:
            yield "❌ Aucune source à encoder : le conteneur n'a rien de câblé.\n"
            return
        try:
            for line in monitor.create_dedicated_iter(vmid, shm=shm, audio_shm=audio_shm):
                yield line + "\n"
        except Exception as e:
            yield f"❌ Erreur inattendue : {e}\n"
    return Response(stream_with_context(gen()),
                    mimetype="text/plain; charset=utf-8",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

@bp.route("/api/monitor/me/destroy", methods=["POST"])
@require_login
def monitor_me_destroy():
    """Libère SON PROPRE encodeur, depuis la page « Mon compte ».

    ⚠ Distincte de `/api/monitor/destroy` À DESSEIN, et pas un assouplissement de sa permission :
    celle-ci prend un `uid` en corps et exige `settings.edit` parce qu'elle détruit le moniteur
    d'AUTRUI. Ici l'uid n'est pas un paramètre — il vient de la session — donc il n'y a rien à
    autoriser au-delà d'être connecté. Un utilisateur sans `settings.edit` voyait sa consommation
    sur sa fiche sans pouvoir y mettre fin : il devait déranger un administrateur pour rendre un
    conteneur qui n'appartient qu'à lui.

    La destruction est ASYNCHRONE (thread dans `monitor.destroy`) : la réponse dit qu'elle est
    lancée, pas qu'elle est finie. Le client recharge la fiche pour lire l'état réel."""
    from .. import monitor
    uid = (current_user() or {}).get("id")
    return jsonify(monitor.destroy(int(uid)))


@bp.route("/api/monitor/destroy", methods=["POST"])
@require_perm("settings.edit")
def monitor_destroy():
    """Détruit le container monitor d'un utilisateur (body {uid}) depuis Réglages → Utilisateurs.
    Sans uid → le moniteur de l'utilisateur courant."""
    from .. import monitor
    data = request.json or {}
    uid = data.get("uid")
    if uid in (None, ""):
        uid = (current_user() or {}).get("id")
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return jsonify({"error": "uid invalide"}), 400
    return jsonify(monitor.destroy(uid))
