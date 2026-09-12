# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Emplacements (rôles) : CRUD de l'identité FONCTIONNELLE des conteneurs.

Un emplacement (« MULTIVIEW RÉGIE 1 ») est ce qu'un système de contrôle externe adresse ;
il se réaffecte d'un conteneur à l'autre sans que la config du pupitre en face ne bouge.
Le vmid est un handle jetable, `instance_uuid` ne survit pas au REMPLACEMENT — cf. le
commentaire de la table `production_roles` dans `database.init_db`.

Consommateur actuel : le provider Ember+ (arbre `emplacements.<num>`). Les routes vivent
ici (cœur) et non dans le service : TSL, le pont ATEM et les macros publiées viseront la
même identité.
"""

from flask import jsonify, request

from . import bp
from ..auth import require_login, require_perm
from ..database import (db_roles_list, db_role_get, db_role_create, db_role_set,
                        db_role_bind, db_role_delete, db_get_containers,
                        db_container_by_instance)


def _role_public(r):
    """Emplacement + état de sa liaison. `online` = un conteneur EXISTE derrière ; un
    emplacement lié à un conteneur détruit reste lié (un restore en mode déplacement
    conserve l'instance_uuid → la liaison revient d'elle-même) mais il est hors ligne."""
    c = db_container_by_instance(r.get("instance_uuid"))
    out = dict(r)
    out["online"] = bool(c)
    out["vmid"] = c.get("vmid") if c else None
    out["hostname"] = c.get("hostname") if c else None
    out["status"] = c.get("status") if c else None
    out["type"] = None
    if c:
        from .shared import _load_dc
        out["type"] = (_load_dc(c) or {}).get("type")
    # Divergence de type : l'emplacement attend un multiview, on y a lié un mixer → les
    # paramètres exposés ne correspondront pas à ce que le pupitre croit piloter.
    out["type_mismatch"] = bool(out["type"] and r.get("expect_type")
                                and out["type"] != r["expect_type"])
    return out


@bp.route("/api/roles", methods=["GET"])
@require_login
def roles_list():
    """Emplacements + conteneurs candidats à une liaison (pour le sélecteur de l'UI)."""
    from .shared import _load_dc
    roles = [_role_public(r) for r in db_roles_list()]
    pris = {r["instance_uuid"] for r in roles if r.get("instance_uuid")}
    candidats = []
    from .. import hostnames as _hn
    for c in sorted(db_get_containers(), key=lambda x: x["vmid"]):
        if c.get("monitor_user_id") or not c.get("instance_uuid"):
            continue
        # L'infra portant un préfixe réservé (shards du tissu `bobi-fab-*`, conteneurs Docker
        # générés, encodeurs de monitoring) n'est pas une fonction de production : elle n'a rien
        # à faire dans un sélecteur d'emplacement. C'est elle qui avait rempli la table quand le
        # semage était automatique — l'écarter ici évite de la proposer à la main.
        if any(str(c.get("hostname") or "").startswith(p) for p in _hn.PREFIXES_RESERVES):
            continue
        candidats.append({
            "instance_uuid": c["instance_uuid"],
            "vmid": c["vmid"],
            "hostname": c.get("hostname"),
            "type": (_load_dc(c) or {}).get("type"),
            "libre": c["instance_uuid"] not in pris,
        })
    return jsonify({"roles": roles, "candidats": candidats})


@bp.route("/api/roles", methods=["POST"])
@require_perm("settings.edit")
def roles_create():
    d = request.json or {}
    try:
        r = db_role_create(d.get("label"), expect_type=d.get("expect_type"),
                           instance_uuid=d.get("instance_uuid") or None,
                           key=d.get("key"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _notify_ember()
    return jsonify(_role_public(r))


@bp.route("/api/roles/<int:num>", methods=["POST"])
@require_perm("settings.edit")
def roles_update(num):
    """Renomme / re-type / (dé)lie. La clé et le numéro sont immuables — c'est le contrat
    d'adressage externe ; on ignore silencieusement une clé envoyée par erreur."""
    if not db_role_get(num):
        return jsonify({"error": "emplacement inconnu"}), 404
    d = request.json or {}
    try:
        if "label" in d or "expect_type" in d:
            db_role_set(num, label=d.get("label"), expect_type=d.get("expect_type"))
        if "instance_uuid" in d:
            db_role_bind(num, d.get("instance_uuid") or None)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _notify_ember()
    return jsonify(_role_public(db_role_get(num)))


@bp.route("/api/roles/<int:num>", methods=["DELETE"])
@require_perm("settings.edit")
def roles_delete(num):
    ok = db_role_delete(num)
    if ok:
        _notify_ember()
    return jsonify({"ok": ok})


@bp.route("/api/roles/purge_offline", methods=["POST"])
@require_perm("settings.edit")
def roles_purge_offline():
    """Supprime en bloc les emplacements HORS LIGNE (aucun conteneur ne les sert).

    Même définition d'« hors ligne » que `_role_public` : `db_container_by_instance` ne rend
    rien. On ne touche JAMAIS à un emplacement servi.

    ⚠ Les numéros ne sont pas réattribués (c'est le contrat d'adressage : un num recyclé
    re-pointerait un pupitre sur autre chose). Supprimer est donc définitif du point de vue
    d'un système de contrôle externe : un pupitre configuré sur l'un de ces emplacements ne le
    retrouvera pas, et le recréer lui donnera un autre numéro. D'où la confirmation côté UI.
    """
    cibles = [r for r in db_roles_list()
              if not db_container_by_instance(r.get("instance_uuid"))]
    nums = []
    for r in cibles:
        if db_role_delete(r["num"]):
            nums.append(r["num"])
    if nums:
        _notify_ember()
    return jsonify({"ok": True, "supprimes": len(nums), "nums": nums})


def _notify_ember():
    """Pousse l'arbre aux clients Ember+ abonnés (best-effort, service optionnel)."""
    try:
        from services import emberplus
        emberplus.notify_change()
    except Exception:
        pass
