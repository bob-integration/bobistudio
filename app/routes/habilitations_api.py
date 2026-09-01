# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Habilitations : CRUD des RÔLES D'AUTORISATION et de ce qu'ils permettent.

⚠ À NE PAS CONFONDRE avec `roles_api.py`, qui sert les EMPLACEMENTS (`production_roles`,
troisième barreau d'identité d'un conteneur). Deux notions homonymes, deux tables : ici on
décide qui a le droit de faire quoi, là on décide quel conteneur tient quelle fonction.

Les habilitations étaient des constantes Python (`auth.ROLES`) : ajouter « le monteur pilote
les plugins mais ne déploie pas » demandait de modifier le code et de redéployer.

DEUX GARDE-FOUS, ET ILS NE SONT PAS NÉGOCIABLES
1. Le rôle `admin` est INTOUCHABLE (cf. `auth.ROLE_INTOUCHABLE`). Une interface qui laisse lui
   retirer `settings.edit` laisse verrouiller l'installation pour de bon : plus personne ne peut
   rouvrir l'onglet qui rendrait le droit.
2. On ne supprime pas une habilitation que des comptes portent — ils se retrouveraient avec un
   rôle inconnu, donc `current_permissions()` vide, donc un parc d'utilisateurs muets d'un coup.
"""

import re

from flask import jsonify, request

from . import bp
from .. import auth
from ..auth import require_perm, PERMISSIONS, current_user
from ..database import (db_habilitations_lister, db_habilitation_get, db_habilitation_upsert,
                        db_habilitation_supprimer, db_habilitation_compte_utilisateurs)

# Identifiant technique : il finit en base et dans des comparaisons `role == "…"`. Pas d'espace,
# pas d'accent, pas de majuscule — le libellé, lui, est libre.
_ID_VALIDE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def _vue(h):
    """Une habilitation telle que l'interface la consomme."""
    h = dict(h)
    h["users"] = db_habilitation_compte_utilisateurs(h["id"])
    h["intouchable"] = (h["id"] == auth.ROLE_INTOUCHABLE)
    return h


@bp.route("/api/habilitations", methods=["GET"])
@require_perm("settings.edit")
def api_habilitations():
    """Les habilitations, le catalogue des autorisations, et qui les porte."""
    return jsonify({
        "habilitations": [_vue(h) for h in db_habilitations_lister(set(PERMISSIONS))],
        "permissions": PERMISSIONS,
        "intouchable": auth.ROLE_INTOUCHABLE,
        # Le rôle de l'appelant : l'interface s'en sert pour prévenir avant de se retirer un
        # droit à soi-même (un administrateur ne peut pas, mais un rôle personnalisé le peut).
        "mon_role": (current_user() or {}).get("role"),
    })


def _permissions_propres(brut):
    """Filtre une liste reçue au catalogue. Une autorisation inconnue est JETÉE, pas refusée :
    un client d'une autre version ne doit pas pouvoir inscrire un droit qui n'existe pas."""
    if not isinstance(brut, list):
        return None
    return sorted({p for p in brut if p in PERMISSIONS})


@bp.route("/api/habilitations", methods=["POST"])
@require_perm("settings.edit")
def api_habilitation_creer():
    d = request.json or {}
    rid = (d.get("id") or "").strip().lower()
    if not _ID_VALIDE.match(rid):
        return jsonify({"error": "identifiant invalide : minuscules, chiffres et « _ », "
                                 "2 à 32 signes, commençant par une lettre"}), 400
    if db_habilitation_get(rid):
        return jsonify({"error": "cette habilitation existe déjà"}), 400
    perms = _permissions_propres(d.get("permissions") or [])
    if perms is None:
        return jsonify({"error": "permissions doit être une liste"}), 400
    db_habilitation_upsert(rid, label=(d.get("label") or rid).strip(), permissions=perms,
                           global_access=bool(d.get("global_access")), builtin=False)
    auth.recharger_habilitations()
    return jsonify({"status": "ok", "habilitation": _vue(db_habilitation_get(rid))})


@bp.route("/api/habilitations/<rid>", methods=["PATCH"])
@require_perm("settings.edit")
def api_habilitation_modifier(rid):
    h = db_habilitation_get(rid)
    if not h:
        return jsonify({"error": "habilitation inconnue"}), 404
    if rid == auth.ROLE_INTOUCHABLE:
        # Refus EXPLICITE plutôt qu'une modification silencieusement ignorée : `auth` remettrait
        # de toute façon toutes les autorisations au rechargement, et l'interface afficherait
        # une opération « réussie » sans effet.
        return jsonify({"error": "le rôle administrateur n'est pas modifiable : il doit rester "
                                 "capable de rouvrir les droits qu'on vient de retirer"}), 400
    d = request.json or {}
    perms = None
    if "permissions" in d:
        perms = _permissions_propres(d.get("permissions"))
        if perms is None:
            return jsonify({"error": "permissions doit être une liste"}), 400
    db_habilitation_upsert(
        rid,
        label=(d.get("label") or "").strip() or None if "label" in d else None,
        permissions=perms,
        global_access=bool(d["global_access"]) if "global_access" in d else None)
    auth.recharger_habilitations()
    return jsonify({"status": "ok", "habilitation": _vue(db_habilitation_get(rid))})


@bp.route("/api/habilitations/<rid>", methods=["DELETE"])
@require_perm("settings.edit")
def api_habilitation_supprimer(rid):
    h = db_habilitation_get(rid)
    if not h:
        return jsonify({"error": "habilitation inconnue"}), 404
    if rid == auth.ROLE_INTOUCHABLE:
        return jsonify({"error": "le rôle administrateur ne se supprime pas"}), 400
    n = db_habilitation_compte_utilisateurs(rid)
    if n:
        # On ne propose PAS de réaffecter d'office : choisir à la place de l'administrateur vers
        # quel rôle basculer des comptes, c'est décider de leurs droits sans le lui demander.
        return jsonify({"error": "%d compte(s) portent cette habilitation : "
                                 "changez-les de rôle avant de la supprimer" % n}), 400
    db_habilitation_supprimer(rid)
    auth.recharger_habilitations()
    return jsonify({"status": "ok"})


@bp.route("/api/habilitations/<rid>/reinitialiser", methods=["POST"])
@require_perm("settings.edit")
def api_habilitation_reinitialiser(rid):
    """Remet une habilitation INTÉGRÉE à ses autorisations d'origine."""
    if rid not in auth.ROLES_DEFAUT:
        return jsonify({"error": "cette habilitation n'a pas de valeur d'origine"}), 400
    db_habilitation_upsert(rid,
                           label=auth.ROLE_LABELS_DEFAUT.get(rid, rid),
                           permissions=sorted(auth.ROLES_DEFAUT[rid]),
                           global_access=rid in auth.GLOBAL_ACCESS_DEFAUT)
    auth.recharger_habilitations()
    return jsonify({"status": "ok", "habilitation": _vue(db_habilitation_get(rid))})
