# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Core plugins (services infrastructure) — manifeste, cycle de vie (enable/export/import/
versions/activate) des services (NMOS, Ember+, TSL, ATEM, Skaarhoj, RDMA…). Nommé
`core_services` (pas `core_plugins`) pour ne pas entrer en collision avec `app.core_plugins`,
le registre lui-même, massivement importé ici via `from .. import core_plugins`."""

import logging
import os
import shutil

from flask import jsonify, request, send_file

from . import bp
from ..auth import require_login, require_perm
from ..database import db_add_alert

log = logging.getLogger(__name__)


@bp.route("/api/settings/manifest", methods=["GET"])
@require_login
def api_settings_manifest():
    from .. import core_plugins
    return jsonify(core_plugins.manifest_list())

@bp.route("/api/services", methods=["GET"])
@require_login
def api_services():
    from .. import core_plugins, settings as _st
    result = []
    for entry in core_plugins.scan().values():
        m   = entry["manifest"]
        mod = entry["module"]
        status = {}
        for fn_name in ("status_dict", "status"):
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                try:
                    status = fn() or {}
                except Exception:
                    pass
                break
        # ★ CE QUE LE SERVICE DIT PRIME SUR CE QU'UN RÉGLAGE LAISSE SUPPOSER.
        # Chercher une clé `*_enabled` dans le manifeste ne marche que pour les
        # services à interrupteur unique. TSL, lui, s'active PAR CONNEXION (table
        # `tsl_connections`, un port chacune) : aucune clé à trouver, donc « — »
        # affiché sur un service qui écoutait sur deux ports TCP. Un service qui
        # publie son `enabled` fait donc foi ; le réglage n'est que le repli.
        if "enabled" in status:
            enabled = None if status["enabled"] is None else bool(status["enabled"])
        else:
            enabled_key = next((k for k in m.get("settings_keys", {})
                                if k.endswith("_enabled")), None)
            enabled = bool(_st.get(enabled_key)) if enabled_key else None

        # ★ UNE ANOMALIE SE COMPARE À UNE INTENTION. Il n'y a plus de colonne
        # « État », et c'est une correction, pas un retrait : le badge vert/rouge
        # mélangeait trois notions sans rapport —
        #
        #   la SANTÉ      : le thread tourne, le port est pris ;
        #   l'ACTIVITÉ    : un pupitre est connecté, un lien est monté ;
        #   l'INTENTION   : le réglage `*_enabled`, qui a déjà sa colonne.
        #
        # D'où un rouge alarmant sur « aucun pupitre connecté », qui n'est pas une
        # panne mais un service qui attend. Un feu rouge qu'on apprend à ignorer ne
        # protège plus de rien.
        #
        # On ne signale donc QUE ce que le service DÉCLARE lui-même en défaut, et
        # seulement s'il a été activé : on n'alarme pas sur ce qu'on n'a pas
        # demandé. Rien n'est DÉDUIT d'un compteur d'activité.
        #
        # ⚠ LIMITE ASSUMÉE : un thread qui meurt sans poser son champ d'erreur
        # passe inaperçu. La déduire d'un `running` faux était précisément le
        # défaut qu'on corrige — mieux vaut un manque connu qu'une fausse alarme
        # quotidienne. Les services qui publient une erreur : atem, emberplus,
        # nmos, sap, snmp, tsl.
        anomalie = None
        if enabled is not False:
            for cle in ("last_error", "error", "erreur", "erreurs"):
                v = status.get(cle)
                if isinstance(v, (list, tuple)):
                    v = "; ".join(str(x) for x in v if x)
                if v:
                    anomalie = str(v)[:300]
                    break
        result.append({
            "id":           m["id"],
            "label":        m["label"],
            "description":  m.get("description", ""),
            "version":      m.get("version", ""),
            "versions":     core_plugins.versions(m["id"]),
            "versions_meta": core_plugins.versions_meta(m["id"]),
            # `anomalie` : null quand tout va bien — donc RIEN à l'écran dans le
            # cas normal. L'onglet du service reste l'endroit où lire son détail.
            "anomalie":     anomalie,
            "has_runtime":  bool(m.get("runtime", True)),
            "enabled":      enabled,
            "nav_tab":      m["nav_tab"],
            "tab_group":    m.get("tab_group"),
            "order":        m.get("order", 99),
        })
    result.sort(key=lambda s: (s["order"], s["label"]))
    return jsonify(result)


def _extract_service_package(raw, tmp):
    """Extrait et valide un paquet .mxlservice (zip) dans tmp.
    Retourne (root, manifest, None) ou (None, None, erreur)."""
    import io, zipfile
    from .. import core_plugins
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return None, None, "fichier invalide (pas un zip)"
    base = os.path.realpath(tmp)
    for info in zf.infolist():
        if info.filename.endswith("/"):
            continue
        dest = os.path.realpath(os.path.join(tmp, info.filename))
        if not (dest == base or dest.startswith(base + os.sep)):
            return None, None, f"archive rejetée (chemin suspect : {info.filename})"
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    root = tmp
    if not os.path.isfile(os.path.join(root, "manifest.json")):
        subs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
        if len(subs) == 1 and os.path.isfile(os.path.join(root, subs[0], "manifest.json")):
            root = os.path.join(root, subs[0])
    man, err = core_plugins.validate_package(root)
    if err:
        return None, None, err
    return root, man, None


@bp.route("/api/services/<svc_id>/enable", methods=["POST"])
@require_perm("settings.edit")
def services_enable(svc_id):
    """Active ou désactive un service. Body: {"enabled": bool}.
    Met à jour la clé _enabled en DB et appelle start()/stop() du module."""
    from .. import core_plugins, settings as _st
    from ..database import db_set_setting
    entry = core_plugins._entry(svc_id)
    if not entry:
        return jsonify({"error": "service inconnu"}), 404
    enabled = bool((request.json or {}).get("enabled"))
    m = entry["manifest"]
    enabled_key = next((k for k in m.get("settings_keys", {}) if k.endswith("_enabled")), None)
    if not enabled_key:
        return jsonify({"error": "ce service n'a pas de clé _enabled"}), 400
    db_set_setting(enabled_key, enabled)
    mod = entry["module"]
    try:
        if enabled:
            # Récupère le port si nécessaire (clé _port dans les settings)
            port_key = next((k for k in m.get("settings_keys", {}) if k.endswith("_port")), None)
            port = int(_st.get(port_key) or 0) if port_key else None
            fn = getattr(mod, "start", None)
            if callable(fn):
                fn(port) if port else fn()
        else:
            fn = getattr(mod, "stop", None)
            if callable(fn):
                fn()
    except Exception as e:
        log.warning(f"services_enable {svc_id}: {e}")
    status = {}
    for fn_name in ("status_dict", "status"):
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            try: status = fn() or {}
            except Exception: pass
            break
    return jsonify({"ok": True, "enabled": enabled,
                    "running": bool(status.get("running") or status.get("registered"))})


@bp.route("/api/services/<svc_id>/export", methods=["GET"])
@require_login
def services_export(svc_id):
    """Télécharge le service complet (flat + versions/) en .mxlservice."""
    import io, zipfile as _zf
    from .. import core_plugins
    d = core_plugins.export_dir(svc_id)
    if not d:
        return jsonify({"error": "service inconnu"}), 404
    ver = core_plugins._entry(svc_id)["manifest"].get("version", "0")
    buf = io.BytesIO()
    with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for fn in files:
                if fn.endswith(".pyc"):
                    continue
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, d))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{svc_id}-{ver}.mxlservice")


@bp.route("/api/services/<svc_id>/versions/<version>/export", methods=["GET"])
@require_login
def services_export_version(svc_id, version):
    """Télécharge une version seule en .mxlservice."""
    import io, zipfile as _zf
    from .. import core_plugins
    d, ver = core_plugins.export_version_dir(svc_id, version)
    if not d:
        return jsonify({"error": "version inconnue"}), 404
    flat = (d == core_plugins._entry(svc_id)["dir"])
    buf = io.BytesIO()
    with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x != "__pycache__"
                       and not (flat and x == "versions")]
            for fn in files:
                if fn.endswith(".pyc"):
                    continue
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, d))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{svc_id}-{ver}.mxlservice")


@bp.route("/api/services/import", methods=["POST"])
@require_perm("settings.edit")
def services_import():
    """Importe un .mxlservice (zip complet). Même logique que /api/plugins/import."""
    import tempfile
    from .. import core_plugins
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "aucun fichier"}), 400
    raw = f.read()
    if len(raw) > 20 * 1024 * 1024:
        return jsonify({"error": "fichier trop volumineux (max 20 Mo)"}), 400
    force = request.form.get("force") == "replace"
    tmp = tempfile.mkdtemp(prefix="mxlservice-")
    try:
        root, man, err = _extract_service_package(raw, tmp)
        if err:
            return jsonify({"error": err}), 400
        core_plugins.stamp_imported_at(root)
        svc_id, ver = man["id"], man["version"]
        exists  = core_plugins.is_service(svc_id)
        avail   = core_plugins.versions(svc_id) if exists else []
        cur     = core_plugins._entry(svc_id)["manifest"].get("version") if exists else None
        offer_activate = False
        if not exists:
            core_plugins.install_package(root, activate=True)
            status = "installed"
        elif ver in avail:
            if not force:
                return jsonify({"status": "conflict", "id": svc_id,
                                "version": ver, "current": cur}), 409
            core_plugins.install_package(root, activate=(ver == cur))
            status = "replaced"
        elif core_plugins._ver_key(ver) > core_plugins._ver_key(cur):
            core_plugins.install_package(root, activate=False)
            status = "imported"; offer_activate = True
        else:
            core_plugins.install_package(root, activate=False)
            status = "imported"
        db_add_alert("alert.deploy.service_importe", "info", kind="deploy",
                     params={"id": svc_id, "v": ver, "statut": status})
        return jsonify({"status": status, "id": svc_id, "version": ver,
                        "offer_activate": offer_activate})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@bp.route("/api/services/<svc_id>/versions/import", methods=["POST"])
@require_perm("settings.edit")
def services_import_version(svc_id):
    """Importe une version seule dans un service existant (toujours archivée)."""
    import tempfile
    from .. import core_plugins
    if not core_plugins.is_service(svc_id):
        return jsonify({"error": f"service inconnu : {svc_id}"}), 404
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "aucun fichier"}), 400
    raw = f.read()
    if len(raw) > 20 * 1024 * 1024:
        return jsonify({"error": "fichier trop volumineux"}), 400
    force = request.form.get("force") == "replace"
    tmp = tempfile.mkdtemp(prefix="mxlservice-")
    try:
        root, man, err = _extract_service_package(raw, tmp)
        if err:
            return jsonify({"error": err}), 400
        if man["id"] != svc_id:
            return jsonify({"error": f"paquet est de type « {man['id']} »"}), 400
        ver = man["version"]
        cur = core_plugins._entry(svc_id)["manifest"].get("version")
        if ver in core_plugins.versions(svc_id) and not force:
            return jsonify({"status": "conflict", "id": svc_id,
                            "version": ver, "current": cur}), 409
        core_plugins.stamp_imported_at(root)
        core_plugins.install_package(root, activate=False)
        db_add_alert("alert.deploy.service_version_importee", "info", kind="deploy",
                     params={"id": svc_id, "v": ver})
        return jsonify({"status": "imported", "id": svc_id, "version": ver})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@bp.route("/api/services/<svc_id>/activate", methods=["POST"])
@require_perm("settings.edit")
def services_activate(svc_id):
    """Promeut une version archivée en version courante (redémarrage requis)."""
    from .. import core_plugins
    version = (request.json or {}).get("version")
    if not version:
        return jsonify({"error": "version manquante"}), 400
    try:
        core_plugins.activate_version(svc_id, version)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db_add_alert("alert.deploy.service_version_activee", "info", kind="deploy",
                 params={"id": svc_id, "v": version})
    return jsonify({"status": "activated", "id": svc_id, "version": version})


def _register_core_plugins():
    from .. import core_plugins
    core_plugins.register_all_routes(bp)

_register_core_plugins()
