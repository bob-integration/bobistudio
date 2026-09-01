# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""API d'étalonnage CPU : lancer une campagne, la suivre, enregistrer les profils mesurés.

Le geste utilisateur est en trois temps, et ils sont SÉPARÉS à dessein :
  1. `start`  — l'utilisateur exerce son dispositif pendant que la sonde tourne ;
  2. `stop`   — la campagne se ferme et rend une distribution, sans rien écrire ;
  3. `save`   — l'utilisateur retient les conteneurs dont la mesure lui paraît valable.

Une campagne ratée (pic jamais joué, nœud injoignable la moitié du temps) ne doit pas devenir un
profil par simple écoulement du temps. Cf. `app/etalonnage.py` pour le fond.
"""
from flask import jsonify, request

from . import bp
from ..auth import require_perm, require_login
from .. import etalonnage


@bp.route("/api/etalonnage/start", methods=["POST"])
@require_perm("containers.deploy")
def api_etalonnage_start():
    d = request.json or {}
    ok, res = etalonnage.demarrer(node_ids=d.get("node_ids"), projet=d.get("projet"),
                                  libelle=(d.get("libelle") or "").strip() or None)
    if not ok:
        return jsonify({"ok": False, "error": res}), 409
    return jsonify({"ok": True, "etat": etalonnage.etat()})


@bp.route("/api/etalonnage/stop", methods=["POST"])
@require_perm("containers.deploy")
def api_etalonnage_stop():
    ok, res = etalonnage.arreter()
    if not ok:
        return jsonify({"ok": False, "error": res}), 409
    return jsonify({"ok": True, "resultat": res})


@bp.route("/api/etalonnage/state", methods=["GET"])
@require_login
def api_etalonnage_state():
    return jsonify(etalonnage.etat() or {"etat": "aucune"})


@bp.route("/api/etalonnage/save", methods=["POST"])
@require_perm("containers.deploy")
def api_etalonnage_save():
    d = request.json or {}
    ok, res = etalonnage.sauver(vmids=d.get("vmids"), note=(d.get("note") or "").strip() or None)
    if not ok:
        return jsonify({"ok": False, "error": res}), 409
    return jsonify({"ok": True, **res})


@bp.route("/api/ressources", methods=["GET"])
@require_login
def api_ressources():
    """Vue « réservé / consommé / mesuré » — alimente le moniteur de ressources.

    Filtres : `?type=`, `?node_id=`, `?vmid=`. La version compacte en coin de page plugin passe
    `type=`, l'onglet Ressources n'en passe aucun.
    """
    return jsonify({"conteneurs": etalonnage.ressources(
        type_=(request.args.get("type") or "").strip() or None,
        node_id=request.args.get("node_id"), vmid=request.args.get("vmid"))})


@bp.route("/api/etalonnage/garantir", methods=["POST"])
@require_perm("containers.deploy")
def api_etalonnage_garantir():
    """Pose la garantie À CHAUD (docker update --cpuset-cpus, sans recréer le conteneur).

    `coeurs` force le dimensionnement ; sans lui, il vient du pic mesuré × marge. Un refus (pas
    assez de cœurs ordonnançables) est un 409 explicite : mieux vaut refuser que garantir à moitié.
    """
    d = request.json or {}
    if not d.get("vmid"):
        return jsonify({"ok": False, "error": "vmid requis"}), 400
    ok, res = etalonnage.garantir(d["vmid"], coeurs=d.get("coeurs"),
                                 marge=float(d.get("marge") or etalonnage.MARGE))
    if not ok:
        return jsonify({"ok": False, "error": res}), 409
    return jsonify({"ok": True, **res})
