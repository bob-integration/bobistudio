# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Jetons d'API — création, liste, révocation (admin uniquement, via `settings.edit`).

Un jeton donne un accès PROGRAMMATIQUE à l'API sous l'identité d'un compte, avec l'habilitation
de ce compte (cf. `auth.current_user`). Pour un accès restreint, on crée un compte restreint :
le jeton ne porte pas de droits propres, délibérément.

Le secret n'est rendu QU'À LA CRÉATION et n'est stocké nulle part — seule son empreinte l'est.
Il n'y a donc pas de route « relire un jeton », et il n'y en aura pas : ce serait défaire la
seule propriété qui rend le vol de la base inoffensif.
"""
from flask import jsonify, request

from . import bp
from ..auth import require_perm, current_user
from ..database import (db_add_alert, db_api_token_creer, db_api_token_revoquer,
                        db_api_tokens_liste, db_get_user_by_id)


@bp.route("/api/api-tokens", methods=["GET"])
@require_perm("settings.edit")
def api_tokens_liste():
    """Les jetons : porteur, libellé, préfixe visible, dates. Jamais le secret ni l'empreinte."""
    return jsonify({"tokens": db_api_tokens_liste()})


@bp.route("/api/api-tokens", methods=["POST"])
@require_perm("settings.edit")
def api_tokens_creer():
    """Crée un jeton pour un compte existant. Renvoie le secret UNE SEULE FOIS.

    Le corps attend `user_id` et, facultativement, `label`. On exige un compte EXISTANT plutôt
    que d'en créer un au vol : décider qui a le droit de quoi est une décision humaine, et un
    compte créé par effet de bord d'un appel d'API n'aurait été voulu par personne."""
    d = request.get_json(silent=True) or {}
    try:
        uid = int(d.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "user_id requis"}), 400
    porteur = db_get_user_by_id(uid)
    if not porteur:
        return jsonify({"error": "compte inconnu", "user_id": uid}), 404
    label = (d.get("label") or "").strip()
    moi = current_user() or {}
    tid, jeton = db_api_token_creer(uid, label=label, created_by=moi.get("id"))
    # Événement de SÉCURITÉ : un nouvel accès durable vient d'être ouvert. Les paramètres sont
    # des CHAMPS NOMMÉS, jamais un objet interpolé — une alerte est du texte diffusé, et c'est
    # par ce chemin qu'un jeton d'agent a déjà fuité une fois.
    db_add_alert("alert.securite.api_token_cree", "info", kind="account",
                 params={"label": label or "(sans libellé)",
                         "identifiant": porteur.get("username") or str(uid),
                         "par": moi.get("username") or "?"})
    # `jeton` sort ICI et nulle part ailleurs : ni journal, ni alerte, ni base.
    return jsonify({"id": tid, "token": jeton, "user_id": uid,
                    "username": porteur.get("username"), "label": label or None})


@bp.route("/api/api-tokens/<int:tid>", methods=["DELETE"])
@require_perm("settings.edit")
def api_tokens_revoquer(tid):
    """Révoque. Idempotent : un jeton déjà révoqué rend 200 avec `deja: true` plutôt qu'une
    erreur — l'appelant voulait que ce jeton ne serve plus, et c'est le cas."""
    lignes = {t["id"]: t for t in db_api_tokens_liste()}
    t = lignes.get(tid)
    if not t:
        return jsonify({"error": "jeton inconnu"}), 404
    fait = db_api_token_revoquer(tid)
    if fait:
        moi = current_user() or {}
        db_add_alert("alert.securite.api_token_revoque", "warning", kind="account",
                     params={"label": t.get("label") or "(sans libellé)",
                             "identifiant": t.get("username") or str(t.get("user_id")),
                             "par": moi.get("username") or "?"})
    return jsonify({"ok": True, "deja": not fait})
