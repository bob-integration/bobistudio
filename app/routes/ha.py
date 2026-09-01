# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Haute disponibilité (paire de contrôleurs) — rôle, réplication, promote/demote.

Le garde-fou standby (`_ha_readonly_guard`, whitelist /api/ha/ + /api/update/) et
l'injecteur de rôle pour les templates (`inject_role`) restent dans app/routes/
__init__.py : ce sont des hooks globaux attachés directement au blueprint `bp`
(before_request / context_processor), pas des routes — ils doivent être visibles
dès l'exécution du module racine, indépendamment de l'ordre d'import des domaines."""

from flask import jsonify, request

from . import bp
from ..auth import require_perm, require_login


@bp.route("/api/ha/role", methods=["GET"])
@require_login
def ha_role_get():
    from .. import ha
    return jsonify({"role": ha.role(), "roles": list(ha.ROLES)})

@bp.route("/api/ha/role", methods=["POST"])
@require_perm("settings.edit")
def ha_role_set():
    """Fixe le rôle de CE contrôleur (active|standby). Prend effet au prochain redémarrage du
    service (la bascule à chaud = promote, B3-2c). Whitelisté côté garde-fou standby."""
    from .. import ha
    from ..database import db_set_setting, db_add_alert
    r = str((request.json or {}).get("role") or "").strip().lower()
    if r not in ha.ROLES:
        return jsonify({"ok": False, "error": f"rôle inconnu : {r}"}), 400
    db_set_setting("control_role", r)
    db_add_alert("alert.node.ha_role_change", "warning", kind="node", params={"r": r})
    return jsonify({"ok": True, "role": r})


def _ha_token_ok():
    """Auth machine-à-machine de la réplication : token partagé `update_token` non vide qui
    correspond (indépendant d'`update_server_enabled` — HA est sa propre feature)."""
    import hmac
    from .. import settings as st
    expected = st.get("update_token") or ""
    given = request.headers.get("X-MXL-Update-Token", "")
    return bool(expected) and hmac.compare_digest(str(expected), str(given))


@bp.route("/api/ha/peer", methods=["GET"])
def ha_peer():
    """Témoin de vie pour le chien de garde de l'AUTRE contrôleur. Auth par le secret partagé (pas
    de session : c'est un appel machine-à-machine). Volontairement minimal — si cette route ne
    répond pas, c'est que l'orchestrateur est à terre, ce qui est précisément l'information."""
    if not _ha_token_ok():
        return jsonify({"ok": False, "error": "non autorisé"}), 401
    from .. import ha
    return jsonify({"ok": True, "role": ha.role()})


@bp.route("/api/ha/replicate", methods=["POST"])
def ha_replicate():
    """Réception (côté STANDBY) d'un snapshot SQLite poussé par l'actif. Corps binaire ; stagé
    (pas appliqué à chaud). Auth par token partagé. Whitelisté par le garde standby (/api/ha/)."""
    if not _ha_token_ok():
        return jsonify({"ok": False, "error": "non autorisé"}), 401
    from .. import ha
    raw = request.get_data(cache=False)
    if not raw:
        return jsonify({"ok": False, "error": "corps vide"}), 400
    try:
        meta = ha.store_replica(raw, source=request.remote_addr or "?")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "bytes": meta.get("bytes"), "at": meta.get("at")})


@bp.route("/api/ha/replicate-now", methods=["POST"])
@require_perm("settings.edit")
def ha_replicate_now():
    """Déclenche un push immédiat (côté actif)."""
    from .. import ha
    ok, msg = ha.push_replica()
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 400)


@bp.route("/api/ha/status", methods=["GET"])
@require_login
def ha_status():
    from .. import ha
    return jsonify(ha.replication_status())


# ─── VIP de management (keepalived) ───────────────────────────────────────────
# Sous /api/ha/ → whitelistées par le garde standby : un contrôleur en veille DOIT pouvoir
# configurer sa propre VIP (sinon la moitié de la paire reste non configurable).

@bp.route("/api/ha/vip", methods=["GET"])
@require_perm("settings.edit")
def ha_vip_get():
    """État + aperçu de la conf qui SERAIT écrite (jamais d'écriture sur un GET)."""
    from .. import vip
    st = vip.status()
    st["preview"] = vip.render_config() if st["address"] else ""
    st["auth_pass"] = vip.config_values()["auth_pass"]   # à recopier sur l'autre contrôleur
    return jsonify(st)


@bp.route("/api/ha/vip", methods=["POST"])
@require_perm("settings.edit")
def ha_vip_set():
    """Enregistre les réglages VIP puis applique (écrit la conf + (re)démarre keepalived).
    `install: true` installe le paquet au passage (action explicite de l'utilisateur)."""
    from .. import vip
    from ..database import db_set_setting
    data = request.json or {}
    for key, cast in (("vip_enabled", bool), ("vip_address", str), ("vip_interface", str),
                      ("vip_vrid", int), ("vip_auth_pass", str),
                      ("vip_priority_active", int), ("vip_priority_standby", int)):
        if key in data:
            try:
                db_set_setting(key, cast(data[key]) if cast is not str else str(data[key]).strip())
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"valeur invalide pour {key}"}), 400
    if not data.get("apply", True):
        return jsonify({"ok": True, "msg": "réglages enregistrés (non appliqués)"})
    ok, msg = vip.apply(install_pkg=bool(data.get("install")))
    if ok is None:      # VIP désactivée : enregistrer sans appliquer n'est pas un échec
        return jsonify({"ok": True, "msg": msg})
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 400)


@bp.route("/api/ha/vip/remove", methods=["POST"])
@require_perm("settings.edit")
def ha_vip_remove():
    from .. import vip
    from ..database import db_set_setting
    db_set_setting("vip_enabled", False)
    ok, msg = vip.remove()
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 400)


@bp.route("/api/ha/promote", methods=["POST"])
@require_perm("settings.edit")
def ha_promote():
    """Bascule STANDBY → ACTIF : applique le replica stagé + redémarre. Whitelistée par le garde
    standby (/api/ha/) → doit fonctionner SUR un standby."""
    from .. import ha
    ok, msg = ha.promote()
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 400)


@bp.route("/api/ha/demote", methods=["POST"])
@require_perm("settings.edit")
def ha_demote():
    """Bascule ACTIF → VEILLE : flip du rôle + redémarre (DB intouchée)."""
    from .. import ha
    ok, msg = ha.demote()
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 400)
