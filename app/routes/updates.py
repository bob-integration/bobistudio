# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Build de distribution + mise à jour entre instances (pull/push, identité publique)."""

import os

from flask import jsonify, request, Response, send_file, abort

from . import bp
from ..auth import require_perm, require_login

# Le nœud Proxmox télécharge le bootstrap + le zip sans session : routes PUBLIQUES,
# gardées par le réglage install_hosting_enabled. Le zip est nettoyé de tout secret
# par app.builder (garde-fou). One-liner : bash <(curl -fsSL http://<host>/install.sh)

_INSTALL_FILES = {
    "install.py":         "text/x-python",   # installeur unifié (menu : nœud / orchestrateur / …)
    "install_proxmox.py": "text/x-python",   # flux legacy Proxmox (délégué par install.py)
    "bobistudio.zip":     "application/zip",
}

def _hosting_on():
    from .. import settings as st
    return bool(st.get("install_hosting_enabled"))

@bp.route("/install.sh", methods=["GET"])
def install_sh():
    if not _hosting_on():
        abort(404)
    base = request.host_url.rstrip("/")
    script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'BASE="${{BOBI_BASE:-{base}}}"\n'
        'TMP="$(mktemp -d)"; cd "$TMP"\n'
        'echo "Bobi.Studio — téléchargement depuis $BASE"\n'
        'curl -fsSL "$BASE/install/install.py"          -o install.py\n'
        'curl -fsSL "$BASE/install/install_proxmox.py"  -o install_proxmox.py\n'
        'curl -fsSL "$BASE/install/bobistudio.zip"      -o bobistudio.zip\n'
        'echo "Lancement de l\'installeur…"\n'
        "python3 install.py\n"
    )
    return Response(script, mimetype="text/x-shellscript",
                    headers={"Cache-Control": "no-cache"})

@bp.route("/install/<path:fname>", methods=["GET"])
def install_file(fname):
    if not _hosting_on() or fname not in _INSTALL_FILES:
        abort(404)
    from ..builder import DIST_DIR
    path = os.path.join(DIST_DIR, fname)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype=_INSTALL_FILES[fname], as_attachment=False,
                     download_name=fname)

@bp.route("/api/build/options", methods=["GET"])
@require_perm("settings.edit")
def api_build_options():
    from .. import builder
    avail = builder.available()
    avail["selection"] = builder.last_selection()
    return jsonify(avail)

@bp.route("/api/build", methods=["POST"])
@require_perm("settings.edit")
def api_build():
    from .. import builder
    data = request.json or {}
    plugins = data.get("plugins")
    services = data.get("services")
    offline = bool(data.get("offline"))
    images = bool(data.get("images"))
    try:
        res = builder.build(plugins=plugins, services=services, offline=offline, images=images)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(res)

# ─── Mise à jour entre instances (pull / push) ────────────────────────────────

def _update_token_ok():
    """Vrai si le mode serveur est actif et le token de la requête correspond."""
    import hmac
    from .. import settings as st
    if not st.get("update_server_enabled"):
        return False
    expected = st.get("update_token") or ""
    given = request.headers.get("X-MXL-Update-Token", "")
    return bool(expected) and hmac.compare_digest(str(expected), str(given))

def _my_identity():
    from .. import settings as st, builder, updater
    info = builder.current_build_info()
    return {"name": st.get("system_name") or st.get("company_name") or "Bobi.Studio",
            "label": info.get("label"), "build_id": info.get("build_id"),
            "deployed_at": updater.deploy_info().get("deployed_at")}

@bp.route("/api/update/core", methods=["GET"])
@require_login
def update_core():
    """La dernière version de Bobi.Studio publiée, comparée à celle de cette instance.

    ★ CE CHEMIN N'EXISTAIT PAS. `updater.py` met à jour d'instance à INSTANCE sur le réseau
    local ; le catalogue ne liste que les composants. Un utilisateur extérieur installait une
    version et n'avait donc AUCUN moyen d'apprendre qu'une suivante existait, ni de l'obtenir —
    alors que ses plugins, eux, se mettent à jour depuis le catalogue.

    ⚠ CETTE ROUTE INFORME, ELLE N'APPLIQUE PAS, et `applicable` dit pourquoi : tant qu'une
    release ne porte pas l'artefact du builder et son empreinte, il n'y a rien d'installable à
    tirer — l'archive de source que sert GitHub n'embarque ni l'installeur ni son SHA256SUMS, que
    `get.sh` vérifie avant d'exécuter quoi que ce soit en root. Annoncer une mise à jour
    applicable qui échouerait serait pire que de ne rien annoncer.
    """
    from .. import catalogue
    from ..version import VERSION
    dispo, info = catalogue.maj_core_disponible()
    return jsonify({"version_installee": VERSION, "disponible": dispo, "derniere": info})


@bp.route("/api/update/core/apply", methods=["POST"])
@require_perm("settings.edit")
def update_core_apply():
    """Tire la dernière release publiée sur GitHub, vérifie son empreinte, applique, relance.

    ⚠ CE GESTE REDÉMARRE LE SERVICE. Il n'est donc pas automatique et ne le sera pas : sur une
    installation d'antenne, le moment se choisit. La détection (`GET /api/update/core`) prévient ;
    l'application reste une décision.

    Refuse si la release ne porte pas `bobistudio.zip` + `SHA256SUMS` : l'archive de source que
    GitHub sert d'office n'embarque ni l'installeur ni d'empreinte, et on n'applique pas du code
    non vérifié sur une instance qui tourne en root.
    """
    from .. import updater
    data = request.get_json(silent=True) or {}
    ok, msg = updater.apply_update_github(tag=data.get("tag") or None,
                                          install_new=data.get("install_new"))
    return jsonify({"ok": bool(ok), "message": msg}), (200 if ok else 400)


@bp.route("/api/update/ping", methods=["GET"])
def update_ping():
    """Identité légère pour la découverte réseau (pas de code exposé → pas de token)."""
    return jsonify(_my_identity())

@bp.route("/api/identity", methods=["GET"])
def api_identity():
    """Identité publique du contrôleur (branding, sans secret) — lue par la page d'état des
    agents-nœuds (:80) pour afficher le nom/entreprise/localisation + un lien retour. Pas de token."""
    from .. import settings as st, builder
    info = builder.current_build_info()
    name = st.get("brand_system_name") or st.get("brand_org_name") or "Bobi.Studio"
    return jsonify({
        "name":     name,
        "org":      st.get("brand_org_name") or "",
        "location": st.get("brand_location") or "",
        "logo_url": st.get("brand_logo_url") or "",
        "version":  info.get("label") or "",
    })

@bp.route("/api/update/manifest", methods=["GET"])
def update_manifest():
    if not _update_token_ok():
        return jsonify({"error": "non autorisé"}), 401
    from .. import updater
    return jsonify(updater.current_manifest())

@bp.route("/api/update/download", methods=["GET"])
def update_download():
    if not _update_token_ok():
        abort(401)
    from .. import updater
    updater.ensure_build()
    return send_file(updater.ZIP_PATH, mimetype="application/zip",
                     as_attachment=True, download_name="bobistudio.zip")

@bp.route("/api/update/apply", methods=["POST"])
def update_apply():
    """Tire le code depuis une source + token et s'auto-met à jour. Appelé soit par l'UI
    locale (pull), soit par un pair (push). Auth : token de CETTE instance OU session admin."""
    from .. import updater
    from ..auth import has_perm
    if not (_update_token_ok() or has_perm("settings.edit")):
        return jsonify({"ok": False, "error": "non autorisé"}), 401
    data = request.json or {}
    source = (data.get("source_url") or "").strip()
    token = data.get("token") or ""
    if not source:
        return jsonify({"ok": False, "error": "source_url requise"}), 400
    ok, msg = updater.apply_update(source, token, install_new=data.get("install_new"))
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 500)

@bp.route("/api/update/rollback", methods=["POST"])
@require_perm("settings.edit")
def update_rollback():
    from .. import updater
    ok, msg = updater.rollback()
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)

@bp.route("/api/update/self", methods=["GET"])
@require_login
def update_self():
    """État local : ma version + token serveur + s'il y a un backup (pour le rollback)."""
    from .. import settings as st, updater, builder
    return jsonify({
        "identity":       _my_identity(),
        "server_enabled": bool(st.get("update_server_enabled")),
        "token":          st.get("update_token") or "",
        "has_backup":     bool(updater.latest_backup()),
        "pending":        updater.pending_build_id(),
    })

@bp.route("/api/update/server", methods=["POST"])
@require_perm("settings.edit")
def update_server_config():
    """Active/désactive le mode serveur + (re)génère OU pose explicitement le token.

    `token` (valeur explicite) est indispensable à la RÉPLICATION HA : le secret est PARTAGÉ,
    l'actif présente le sien et le standby le compare au sien — deux tokens générés séparément
    ne correspondront jamais. Il faut donc pouvoir recopier celui de l'autre contrôleur.
    Route whitelistée par le garde standby (`/api/update/`) → posable des DEUX côtés."""
    import secrets
    from ..database import db_set_setting
    from .. import settings as st
    data = request.json or {}
    if "enabled" in data:
        db_set_setting("update_server_enabled", bool(data.get("enabled")))
    if data.get("regen_token"):
        db_set_setting("update_token", secrets.token_urlsafe(24))
    elif "token" in data:
        tok = str(data.get("token") or "").strip()
        if tok and len(tok) < 16:
            return jsonify({"ok": False, "error": "token trop court (16 caractères minimum)"}), 400
        db_set_setting("update_token", tok)
    elif data.get("enabled") and not st.get("update_token"):
        db_set_setting("update_token", secrets.token_urlsafe(24))
    return jsonify({"ok": True, "token": st.get("update_token") or "",
                    "server_enabled": bool(st.get("update_server_enabled"))})
