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
from ..auth import require_perm, require_login
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
    res = _cat.lister(force=force)
    # L'état de l'interrupteur « activer après récupération » voyage avec la liste, comme
    # `actif` : la page ne doit pas avoir à faire un second appel pour savoir dans quel mode
    # elle est, sinon les deux se désynchronisent le temps d'un chargement.
    res["activer_apres"] = _activer_apres_defaut()
    # L'écran doit savoir s'il peut PROPOSER un jeton, sans jamais le renvoyer.
    res["jeton_pose"] = bool(_cat._jeton())
    return jsonify(res)


@bp.route("/api/catalogue/token", methods=["POST"])
@require_login
def catalogue_token():
    """Pose ou efface le jeton GitHub de l'utilisateur COURANT.

    ★ SUR SOI, JAMAIS SUR AUTRUI, et pas de réglage de site : un jeton est une identité. Celui
    qui bute sur le plafond anonyme (60 requêtes/heure, soit six relectures du catalogue) fournit
    le sien et n'élargit que pour lui — personne ne consomme sans le savoir le quota d'un autre.

    ⚠ ON VÉRIFIE LE JETON AVANT DE L'ENREGISTRER, contre `/rate_limit` — le seul point d'API que
    GitHub ne décompte pas. Un jeton faux ou révoqué stocké tel quel donnerait exactement le
    symptôme qu'on cherche à faire disparaître, en pire : l'utilisateur croirait le problème
    réglé. On rend aussi le plafond obtenu, pour que l'écran le montre plutôt que l'affirmer.
    """
    import json as _json
    import urllib.error
    import urllib.request
    from ..auth import current_user
    from ..database import db_set_user_gh_token

    uid = (current_user() or {}).get("id")
    if not uid:
        return jsonify({"error": "non authentifié"}), 401
    jeton = ((request.get_json(silent=True) or {}).get("token") or "").strip()

    if not jeton:                                   # effacement explicite
        db_set_user_gh_token(uid, "")
        return jsonify({"status": "efface", "jeton_pose": False})

    req = urllib.request.Request("https://api.github.com/rate_limit", headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "bobistudio-catalogue",
        "Authorization": "Bearer %s" % jeton,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            corps = _json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return jsonify({"error": "jeton refusé par GitHub (expiré ou invalide)"}), 400
        return jsonify({"error": "GitHub a répondu HTTP %s" % e.code}), 502
    except Exception as e:
        return jsonify({"error": "GitHub injoignable : %s" % (e,)}), 502

    plafond = (((corps or {}).get("resources") or {}).get("core") or {}).get("limit") or 0
    if plafond <= 60:
        # Le jeton est valide mais ne change rien : le dire, plutôt que de l'enregistrer et
        # laisser l'utilisateur se cogner au même plafond en croyant l'avoir levé.
        return jsonify({"error": "ce jeton n'élargit rien (plafond %s/heure)" % plafond}), 400
    db_set_user_gh_token(uid, jeton)
    db_add_alert("alert.catalogue.jeton_pose", "info", params={"n": plafond})
    return jsonify({"status": "pose", "jeton_pose": True, "plafond": plafond})


def _en_derive(type_, ver):
    """Conteneurs de ce type qui ne tournent PAS déjà la version qu'on vient d'activer.
    Même lecture que la page Plugins (`plugin_registry`), pour que les deux comptent pareil."""
    from ..database import db_get_containers
    from .plugin_registry import _load_dc
    n = 0
    for c in db_get_containers() or []:
        dc = _load_dc(c) or {}
        if dc.get("type") != type_:
            continue
        if (dc.get("params") or {}).get("plugin_version") != ver:
            n += 1
    return n


def _activer_apres_defaut():
    from .. import settings as st
    return str(st.get("catalogue_activer") or "1") not in ("0", "false", "no")


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
    # ★ ACTIVER OU NON EST UN CHOIX DE L'EXPLOITANT, plus une règle déduite du fait que le
    # type existait déjà. L'ancienne règle — neuf → activé, mise à jour → rangée — était
    # sensée mais INVISIBLE : deux clics identiques donnaient deux résultats différents, et
    # rien ne le disait avant de cliquer. Le corps peut trancher au coup par coup ; sans lui,
    # c'est l'interrupteur de la page (réglage `catalogue_activer`, coché par défaut).
    activer = data.get("activer")
    activer = _activer_apres_defaut() if activer is None else bool(activer)
    e = _cat.entree(depot)
    if not e:
        return jsonify({"error": "dépôt absent du catalogue"}), 404
    if not e["manifeste_lu"]:
        # On refuse d'installer ce qu'on n'a pas su lire : sans manifeste, on ne
        # connaît ni le type ni la version, donc on ne sait pas ce qu'on remplace.
        return jsonify({"error": "manifeste illisible sur ce dépôt"}), 400

    try:
        brut = _cat.telecharger(depot, e.get("tag"))
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
            plugins.install_package(root, activate=activer)
            statut = "installe" if activer else "range"
            db_add_alert("alert.deploy.plugin_importe", "info", kind="deploy",
                         params={"t": type_, "v": ver, "statut": statut})
            # ★ CE QUE L'ACTIVATION LAISSE DERRIÈRE, COMPTÉ. Promouvoir une version ne
            # touche AUCUN conteneur en marche : ils continuent sur le script qu'ils ont
            # reçu, et se retrouvent en DÉRIVE. C'est ce que la page Plugins savait dire et
            # que le catalogue taisait — on rend donc le nombre, pour que le message le
            # dise au lieu d'un « installé et activé » qui laisse croire le parc à jour.
            derive = _en_derive(type_, ver) if (activer and existait) else 0
            return jsonify({"status": statut, "genre": "plugin", "type": type_,
                            "version": ver, "version_precedente": courante,
                            "activee": activer, "derive": derive})

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
        core_plugins.install_package(root, activate=activer)
        db_add_alert("alert.deploy.service_importe", "info", kind="deploy",
                     params={"id": svc_id, "v": ver,
                             "statut": "installe" if activer else "range"})
        return jsonify({"status": "installe" if activer else "range",
                        "genre": "service", "type": svc_id, "version": ver,
                        "version_precedente": courante,
                        "activee": activer,
                        # ★ LE SEUL MESSAGE QUI COMPTE POUR UN SERVICE. `main.py`
                        # importe les services par leur nom au démarrage : tant
                        # que le contrôleur n'a pas redémarré, le paquet est sur
                        # disque et sans effet. Le taire donnerait un service
                        # « installé » qui ne fait rien, sans rien dire.
                        "redemarrage_requis": True})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
