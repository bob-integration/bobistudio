# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Routes du catalogue : lire les paquets publiés, en installer un.
#
# ⚠ INSTALLER, C'EST EXÉCUTER. Le corps d'un plugin tourne dans un conteneur,
# jamais ici — mais `hooks.py` est l'exception documentée : il est importé DANS
# l'orchestrateur. Ces routes exigent donc `settings.edit`, ne travaillent que sur
# la LISTE BLANCHE que le catalogue a construite depuis l'organisation configurée,
# et ne prennent jamais une URL fournie par l'appelant.
import tempfile
import shutil

from flask import jsonify, request

from . import bp
from ..auth import require_perm
from ..database import db_add_alert
from .. import catalogue as _cat


@bp.route("/api/catalogue", methods=["GET"])
@require_perm("settings.edit")
def catalogue_lister():
    """Paquets publiés, comparés à l'installé. `?force=1` force la relecture.

    L'erreur voyage DANS la réponse (200 avec `erreur` renseignée), pas en code
    HTTP : un contrôleur sans accès Internet est un cas normal, et la page doit
    pouvoir afficher la dernière liste connue en disant qu'elle est périmée."""
    force = (request.args.get("force") or "") in ("1", "true", "yes")
    return jsonify(_cat.lister(force=force))


@bp.route("/api/catalogue/install", methods=["POST"])
@require_perm("settings.edit")
def catalogue_installer():
    """Installe ou met à jour UN paquet du catalogue.

    Le plugin passe par exactement le même chemin que l'import manuel d'un
    `.mxlplugin` : extraction anti *zip-slip*, validation du manifeste, dry-run
    `str.format`. Une archive GitHub a un dossier racine englobant, que
    `_extract_validated_package` tolère déjà — il n'y a donc pas de second chemin
    d'installation à maintenir, et c'est délibéré : deux chemins divergent."""
    from .plugin_registry import _extract_validated_package
    from .. import plugins

    data = request.get_json(silent=True) or {}
    depot = (data.get("depot") or "").strip()
    e = _cat.entree(depot)
    if not e:
        return jsonify({"error": "dépôt absent du catalogue"}), 404
    if not e["manifeste_lu"]:
        # On refuse d'installer ce qu'on n'a pas su lire : sans manifeste, on ne
        # connaît ni le type ni la version, donc on ne sait pas ce qu'on remplace.
        return jsonify({"error": "manifeste illisible sur ce dépôt"}), 400

    try:
        brut = _cat.telecharger(depot, e["branche"])
    except Exception as exc:
        return jsonify({"error": "téléchargement impossible : %s" % (exc,)}), 502

    tmp = tempfile.mkdtemp(prefix="catalogue-")
    try:
        if e["genre"] == "plugin":
            root, man, err = _extract_validated_package(brut, tmp)
            if err:
                return jsonify({"error": err}), 400
            type_, ver = man["type"], man["version"]
            existait = plugins.is_plugin(type_)
            courante = (plugins.get(type_) or {}).get("version") if existait else None
            # ⚠ RÉINSTALLER LA MÊME VERSION N'EST PAS ANODIN : `install_package`
            # archiverait un doublon sous versions/<ver>/ sans rien changer. La
            # route d'import manuel rend 409 dans ce cas ; on fait pareil, au lieu
            # de laisser croire à une mise à jour qui n'a rien mis à jour.
            if existait and ver in plugins.versions(type_):
                return jsonify({"status": "deja_present", "genre": "plugin",
                                "type": type_, "version": ver,
                                "version_courante": courante}), 409
            plugins.stamp_imported_at(root)
            # ★ ACTIVER UNE NOUVELLE VERSION N'EST PAS AU CATALOGUE DE LE DÉCIDER.
            # Une installation neuve s'active (sinon le plugin n'apparaît nulle
            # part et l'exploitant croit que rien ne s'est passé) ; une mise à jour
            # est RANGÉE, et c'est la page Plugins qui la promeut — c'est elle qui
            # sait combien de conteneurs tournent dessus.
            plugins.install_package(root, activate=not existait)
            statut = "installe" if not existait else "range"
            db_add_alert("alert.deploy.plugin_importe", "info", kind="deploy",
                         params={"t": type_, "v": ver, "statut": statut})
            return jsonify({"status": statut, "genre": "plugin", "type": type_,
                            "version": ver, "version_precedente": courante,
                            "activee": not existait})

        # ── Service : MÊME chemin que l'import manuel d'un .mxlservice ───────
        # ⚠ Un service a son propre registre versionné (`core_plugins`), au même
        # titre qu'un plugin. La première version de cette route déposait le
        # dossier à la main dans services/<id>/ : ça marchait, et ça contournait
        # la validation, l'archivage de la version précédente et l'activation.
        # Un second chemin d'installation, c'est un chemin qui dérivera.
        from .core_services import _extract_service_package
        from .. import core_plugins

        root, man, err = _extract_service_package(brut, tmp)
        if err:
            return jsonify({"error": err}), 400
        svc_id, ver = man["id"], man["version"]
        if svc_id != e["type"]:
            return jsonify({"error": "le paquet dit « %s », le catalogue « %s »"
                                     % (svc_id, e["type"])}), 400
        existait = core_plugins.is_service(svc_id)
        courante = _cat._version_service_installee(svc_id)
        if existait and ver in core_plugins.versions(svc_id):
            return jsonify({"status": "deja_present", "genre": "service",
                            "type": svc_id, "version": ver,
                            "version_courante": courante}), 409
        core_plugins.stamp_imported_at(root)
        core_plugins.install_package(root, activate=not existait)
        db_add_alert("alert.deploy.service_importe", "info", kind="deploy",
                     params={"id": svc_id, "v": ver,
                             "statut": "installe" if not existait else "range"})
        return jsonify({"status": "installe" if not existait else "range",
                        "genre": "service", "type": svc_id, "version": ver,
                        "version_precedente": courante,
                        "activee": not existait,
                        # ★ LE SEUL MESSAGE QUI COMPTE POUR UN SERVICE. `main.py`
                        # importe les services par leur nom au démarrage : tant
                        # que le contrôleur n'a pas redémarré, le paquet est sur
                        # disque et sans effet. Le taire donnerait un service
                        # « installé » qui ne fait rien, sans rien dire.
                        "redemarrage_requis": True})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
