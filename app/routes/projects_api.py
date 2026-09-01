# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Layouts multiview (mémoires de disposition, indépendantes des projets) + Projets : snapshot
d'un ensemble de containers (export/import .bsproj.json, restauration en streaming avec
prévisualisation de pré-vol, destruction en masse)."""

import json
import threading

from flask import jsonify, request, Response, stream_with_context

from . import bp
from ..auth import (require_login, require_perm, require_project_role,
                    scoped_project_ids, project_role_for, current_user,
                    require_global_access, PROJECT_ROLES, PROJECT_ROLE_LABELS)
from ..database import (db_get_layouts, db_save_layout, db_update_layout, db_delete_layout,
                      db_get_projects, db_save_project, db_delete_project,
                      db_import_project, db_get_project,
                      db_project_members, db_set_project_member,
                      db_remove_project_member, db_get_user_by_id,
                      db_project_views, db_get_view, db_create_view,
                      db_update_view, db_delete_view)
from ..projects import restaurer_projet, detruire_containers_projet, planifier_restore

# Priorité de déploiement au restore (les moteurs 2110_io/streamer d'abord, la
# composition — mixer/multiview — ensuite, une fois ses sources potentielles en place).
TYPE_PRIORITY = {"2110_io": 0, "streamer": 1, "mixer": 2, "multiview": 2}


def _absorb_fonts(config, data):
    """Import des POLICES embarquées dans un export de layout / modèle de PiP.
    `data.fonts` = [{name, family, ext, sha256, ttf_b64}] (cf. app/fonts.py:export_bundle).
    Dédup par HASH (jamais par nom) : police connue → référencée ; inconnue → ajoutée à la
    bibliothèque (nom suffixé en collision) ; référencée mais ni connue ni embarquée → repli
    DejaVu + avertissement remonté à l'appelant. Renvoie (config réécrite, warnings)."""
    from .. import fonts as _fonts
    refs = _fonts.collect_refs(config)
    if not refs:
        return config, []
    res = _fonts.import_bundle(data.get("fonts") or [], refs=refs,
                               created_by=(current_user() or {}).get("username") or "")
    return _fonts.rewrite_refs(config, res["mapping"]), res["warnings"]


@bp.route("/api/layouts", methods=["GET"])
@require_login
def liste_layouts():
    return jsonify(db_get_layouts())

@bp.route("/api/layouts", methods=["POST"])
@require_perm("multiview.edit")
def sauvegarder_layout():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    config = data.get("config") or {}
    if not name:
        return jsonify({"error": "name requis"}), 400
    config, warnings = _absorb_fonts(config, data)
    lid = db_save_layout(name, config)
    return jsonify({"status": "ok", "id": lid, "font_warnings": warnings})

@bp.route("/api/layouts/<int:lid>", methods=["PUT"])
@require_perm("multiview.edit")
def modifier_layout(lid):
    """Écrase un layout existant (nom + config) — l'édition en place d'un layout enregistré
    (bouton « Modifier »). L'id et created_at sont préservés ; les consommateurs qui référencent
    ce layout par id restent valides."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    config = data.get("config") or {}
    if not name:
        return jsonify({"error": "name requis"}), 400
    config, warnings = _absorb_fonts(config, data)
    if not db_update_layout(lid, name, config):
        return jsonify({"error": "introuvable"}), 404
    return jsonify({"status": "ok", "id": lid, "font_warnings": warnings})

@bp.route("/api/layouts/<int:lid>/export", methods=["GET"])
@require_login
def exporter_layout(lid):
    """Export d'un layout AVEC ses polices embarquées (autonome sur une autre instance)."""
    from .. import fonts as _fonts
    lay = next((l for l in db_get_layouts() if l.get("id") == lid), None)
    if not lay:
        return jsonify({"error": "introuvable"}), 404
    return jsonify({"name": lay.get("name"), "config": lay.get("config") or {},
                    "fonts": _fonts.export_bundle(lay.get("config") or {})})

@bp.route("/api/layouts/<int:lid>", methods=["DELETE"])
@require_perm("multiview.edit")
def supprimer_layout(lid):
    db_delete_layout(lid)
    return jsonify({"status": "supprimé"})

# ─── Modèles de PiP (bibliothèque composable, éditeur Réglages → PiP) ──
# Comme les layouts : bibliothèque GLOBALE (indépendante des projets — les modèles affectés
# sont embarqués dans les deploy_config, donc snapshotés avec les projets). Les modèles
# d'usine (id "builtin:…") sont servis avec la liste mais ni modifiables ni supprimables.

@bp.route("/api/pip_templates", methods=["GET"])
@require_login
def liste_pip_templates():
    from ..pip_library import all_pip_templates
    return jsonify(all_pip_templates())

@bp.route("/api/pip_templates", methods=["POST"])
@require_perm("multiview.edit")
def sauvegarder_pip_template():
    from ..database import db_save_pip_template
    data = request.json or {}
    name = (data.get("name") or "").strip()
    config = data.get("config") or {}
    if not name:
        return jsonify({"error": "name requis"}), 400
    if not isinstance(config.get("components"), list):
        return jsonify({"error": "config.components requis"}), 400
    tid = data.get("id")
    if isinstance(tid, str) and tid.startswith("builtin:"):
        return jsonify({"error": "modèle d'usine non modifiable"}), 400
    try:
        tid = int(tid) if tid is not None else None
    except (TypeError, ValueError):
        tid = None
    tags = data.get("tags")
    if not isinstance(tags, list):
        tags = None
    else:
        # Chaînes libres, dédupliquées, nettoyées — jamais de tag vide.
        seen = []
        for tg in tags:
            tg = str(tg).strip()
            if tg and tg not in seen:
                seen.append(tg)
        tags = seen
    # Polices embarquées (import d'un modèle exporté depuis une autre instance) : dédup par hash.
    config, font_warnings = _absorb_fonts(config, data)
    tid = db_save_pip_template(name, config, tid, tags=tags)
    return jsonify({"status": "ok", "id": tid, "font_warnings": font_warnings})

@bp.route("/api/pip_templates/<int:tid>/export", methods=["GET"])
@require_login
def exporter_pip_template(tid):
    """Export d'un modèle de PiP AVEC ses polices embarquées ({name, family, sha256, ttf_b64})."""
    from .. import fonts as _fonts
    from ..database import db_get_pip_templates
    tpl = next((t for t in db_get_pip_templates() if t.get("id") == tid), None)
    if not tpl:
        return jsonify({"error": "introuvable"}), 404
    cfg = tpl.get("config") or {}
    return jsonify({"name": tpl.get("name"), "config": cfg, "tags": tpl.get("tags") or [],
                    "fonts": _fonts.export_bundle(cfg)})

@bp.route("/api/pip_templates/<int:tid>", methods=["DELETE"])
@require_perm("multiview.edit")
def supprimer_pip_template(tid):
    from ..database import db_delete_pip_template
    db_delete_pip_template(tid)
    return jsonify({"status": "supprimé"})

@bp.route("/api/projects", methods=["GET"])
@require_login
def liste_projets():
    projs = db_get_projects()
    member_pids = scoped_project_ids()   # None = accès global
    if member_pids is not None:
        # Utilisateur scopé : seulement SES projets, sans le snapshot (config interne).
        projs = [{k: v for k, v in p.items() if k != "snapshot"}
                 for p in projs if p["id"] in member_pids]
        for p in projs:
            p["my_role"] = project_role_for(p["id"])
    return jsonify(projs)

# ─── Membres + résumé par projet (chantier 1, cf. docs/reference/PROJETS.md §12) ──

@bp.route("/api/projects/<int:pid>/members", methods=["GET"])
@require_project_role("owner")
def project_members_list(pid):
    return jsonify({"members": db_project_members(pid),
                    "roles": PROJECT_ROLES, "role_labels": PROJECT_ROLE_LABELS})

@bp.route("/api/projects/<int:pid>/members", methods=["POST"])
@require_project_role("owner")
def project_members_set(pid):
    data = request.json or {}
    uid = data.get("user_id")
    if uid in (None, "") and data.get("username"):
        # Repli par username : un `operator` (projects.manage sans settings.edit) n'a
        # pas accès à la liste /api/users pour choisir un id.
        from ..database import db_get_user
        u = db_get_user((data.get("username") or "").strip())
        if not u:
            return jsonify({"error": "utilisateur introuvable"}), 404
        uid = u["id"]
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return jsonify({"error": "user_id ou username requis"}), 400
    role = (data.get("role") or "").strip()
    if role not in PROJECT_ROLES:
        return jsonify({"error": f"rôle invalide (attendu : {PROJECT_ROLES})"}), 400
    if not db_get_project(pid):
        return jsonify({"error": "projet introuvable"}), 404
    if not db_get_user_by_id(uid):
        return jsonify({"error": "utilisateur introuvable"}), 404
    db_set_project_member(pid, uid, role)
    return jsonify({"status": "ok", "members": db_project_members(pid)})

@bp.route("/api/projects/<int:pid>/members/<int:uid>", methods=["DELETE"])
@require_project_role("owner")
def project_members_remove(pid, uid):
    db_remove_project_member(pid, uid)
    return jsonify({"status": "ok", "members": db_project_members(pid)})

@bp.route("/api/projects/<int:pid>/summary", methods=["GET"])
@require_project_role("viewer")
def project_summary(pid):
    """Résumé scopé d'un projet pour le workspace (/workspace/<pid>) : containers
    rattachés (project_id) avec statut/fps/sorties. Même esprit que /api/home/summary
    mais limité au projet — les utilisateurs scopés n'ont pas accès au summary global."""
    from .. import plugins as _plugins
    from ..database import db_get_containers
    from .shared import _load_dc
    proj = db_get_project(pid)
    if not proj:
        return jsonify({"error": "projet introuvable"}), 404
    # « Dans le projet » = rattachement direct (project_id) OU référencé par le snapshot
    # (db_save_project ne pose pas project_id — le rattachement systématique arrive avec
    # le cycle de vie du chantier 3). Même sémantique que auth.vmid_project_ids.
    snap_vmids = {sc.get("vmid") for sc in (proj.get("snapshot") or [])}
    containers = []
    for c in db_get_containers():
        if c.get("project_id") != pid and c["vmid"] not in snap_vmids:
            continue
        dc = _load_dc(c) or {}
        kind = dc.get("type")
        p = dc.get("params") or {}
        hn = p.get("hostname") or c.get("hostname") or f"mxl{c['vmid']}"
        produces = []
        if _plugins.is_plugin(kind):
            try:
                produces = [{"shm": pr.get("shm"), "kind": pr.get("essence") or "video",
                             "label": pr.get("label") or ""}
                            for pr in _plugins.derive_wiring(kind, hn, p)["produces"]
                            if pr.get("shm")]
            except Exception:
                produces = []
        containers.append({
            "vmid": c["vmid"], "hostname": c.get("hostname"), "type": kind,
            "status": c.get("status"), "fps": c.get("fps"),
            "instance_uuid": c.get("instance_uuid"),
            "plugin_version": p.get("plugin_version"), "produces": produces,
        })
    containers.sort(key=lambda x: (x.get("hostname") or "", x["vmid"]))
    return jsonify({
        "project": {"id": proj["id"], "name": proj["name"],
                    "created_at": proj.get("created_at"),
                    "state": proj.get("state") or "saved"},
        "my_role": project_role_for(pid),
        "containers": containers,
    })

# ─── Ports virtuels (chantier 4, cf. docs/reference/PROJETS.md §5) ───────────
#
# Frontière du projet : les EDITORS déclarent les ports (nom, sens, essence, labels
# canaux, sortie interne publiée pour une destination) ; le BINDING physique d'une
# source (quel shm alimente CAM1) est réservé aux accès globaux (admin/operator).
# Re-binder un port d'un projet chargé re-câble À CHAUD les consommateurs concernés
# (mécanique _apply_wire existante : hot-input si possible, redeploy async sinon).

def _find_producer_vmid(shm):
    """vmid du container qui produit ce shm (pour _apply_wire/format), ou None."""
    from .. import plugins as _plugins
    from ..database import db_get_containers
    from .shared import _load_dc
    for c in db_get_containers():
        dc = _load_dc(c) or {}
        t = dc.get("type")
        if not _plugins.is_plugin(t):
            continue
        p = dc.get("params") or {}
        hn = p.get("hostname") or c.get("hostname") or ""
        try:
            if any(pr.get("shm") == shm for pr in
                   _plugins.derive_wiring(t, hn, p)["produces"]):
                return c["vmid"]
        except Exception:
            continue
    return None

def _rewire_shm_consumers(old_shm, new_shm, restrict_pids=None, exclude_pids=None):
    """Re-câble à chaud tous les consommateurs de old_shm vers new_shm.
    restrict_pids : ne toucher QUE les containers de ces projets (rebind d'un port
    source → consommateurs internes) ; exclude_pids : ne toucher que les containers
    HORS de ces projets (rebind d'une destination → consommateurs externes).
    Renvoie [{vmid, ok, error?}]."""
    from .cabling import _apply_wire, _collect_current_edges
    from ..auth import vmid_project_ids
    out = []
    if not old_shm or not new_shm or old_shm == new_shm:
        return out
    prod = _find_producer_vmid(new_shm)
    for e in _collect_current_edges():
        if e.get("shm") != old_shm:
            continue
        to_vmid = e.get("to_vmid")
        pids = vmid_project_ids(to_vmid)
        if restrict_pids is not None and not (pids & set(restrict_pids)):
            continue
        if exclude_pids is not None and (pids & set(exclude_pids)):
            continue
        try:
            ok, _st, payload = _apply_wire(prod, to_vmid, new_shm,
                                           e.get("kind") or "video",
                                           to_slot=e.get("to_slot"))
            out.append({"vmid": to_vmid, "ok": bool(ok),
                        **({} if ok else {"error": (payload or {}).get("error")})})
        except Exception as ex:
            out.append({"vmid": to_vmid, "ok": False, "error": str(ex)})
    return out

@bp.route("/api/ports", methods=["GET"])
@require_global_access
def all_ports():
    """Tous les ports de tous les projets (vue d'ensemble Câbles / éditeur de bindings)."""
    from ..database import db_project_ports
    projs = {p["id"]: p["name"] for p in db_get_projects()}
    ports = db_project_ports(None)
    for pt in ports:
        pt["project_name"] = projs.get(pt["project_id"])
    return jsonify({"ports": ports})

@bp.route("/api/projects/<int:pid>/ports", methods=["GET"])
@require_project_role("viewer")
def project_ports_list(pid):
    from ..database import db_project_ports
    return jsonify({"ports": db_project_ports(pid)})

@bp.route("/api/projects/<int:pid>/ports", methods=["POST"])
@require_project_role("editor")
def project_ports_create(pid):
    from ..database import db_create_port, db_get_port
    data = request.json or {}
    name = (data.get("name") or "").strip()
    kind = data.get("kind") if data.get("kind") in ("source", "dest") else "source"
    media = data.get("media") if data.get("media") in ("video", "audio", "anc") else "video"
    if not name:
        return jsonify({"error": "name requis"}), 400
    port_id = db_create_port(pid, kind, media, name,
                             ord_=int(data.get("ord") or 0),
                             channel_labels=data.get("channel_labels"))
    return jsonify({"status": "ok", "port": db_get_port(port_id)})

@bp.route("/api/projects/<int:pid>/ports/<int:port_id>", methods=["PATCH"])
@require_project_role("editor")
def project_ports_update(pid, port_id):
    from ..database import db_get_port, db_update_port
    from ..auth import has_global_access
    port = db_get_port(port_id)
    if not port or port.get("project_id") != pid:
        return jsonify({"error": "port introuvable"}), 404
    data = request.json or {}
    rewired = []
    if "binding" in data:
        binding = data.get("binding") or {}
        if not isinstance(binding, dict):
            return jsonify({"error": "binding invalide"}), 400
        old = port.get("binding") or {}
        if port["kind"] == "source":
            # Binding physique d'une source = réservé aux accès globaux (admin binde).
            if not has_global_access():
                return jsonify({"error": "forbidden",
                                "reason": "binding_requires_admin"}), 403
            if old.get("shm") and binding.get("shm"):
                rewired = _rewire_shm_consumers(old.get("shm"), binding.get("shm"),
                                                restrict_pids={pid})
            if old.get("audio_shm") and binding.get("audio_shm"):
                rewired += _rewire_shm_consumers(old.get("audio_shm"),
                                                 binding.get("audio_shm"),
                                                 restrict_pids={pid})
        else:
            # Destination : la sortie interne publiée est du CONTENU (editor OK) ;
            # les consommateurs EXTERNES suivent à chaud.
            if old.get("internal_shm") and binding.get("internal_shm"):
                rewired = _rewire_shm_consumers(old.get("internal_shm"),
                                                binding.get("internal_shm"),
                                                exclude_pids={pid})
        db_update_port(port_id, binding=binding)
    db_update_port(port_id,
                   name=(data.get("name") or "").strip() or None,
                   ord_=(int(data["ord"]) if "ord" in data else None),
                   channel_labels=data.get("channel_labels"))
    return jsonify({"status": "ok", "port": db_get_port(port_id), "rewired": rewired})

@bp.route("/api/projects/<int:pid>/ports/<int:port_id>", methods=["DELETE"])
@require_project_role("editor")
def project_ports_delete(pid, port_id):
    from ..database import db_get_port, db_delete_port
    port = db_get_port(port_id)
    if not port or port.get("project_id") != pid:
        return jsonify({"error": "port introuvable"}), 404
    db_delete_port(port_id)
    return jsonify({"status": "ok"})

# ─── Versions de projet (« projet vivant », chantier 3) ───────

@bp.route("/api/projects/<int:pid>/versions", methods=["GET"])
@require_project_role("viewer")
def project_versions_list(pid):
    from ..database import db_project_versions as _pv
    return jsonify({"versions": _pv(pid)})

@bp.route("/api/projects/<int:pid>/versions", methods=["POST"])
@require_project_role("owner")
def project_versions_label(pid):
    """« Nommer cette version » : synchronise le snapshot sur le live (re-snapshot),
    puis enregistre une version NOMMÉE de l'état courant (conservée sans limite)."""
    from ..projects import resnapshot_projet
    from ..database import db_add_project_version
    label = ((request.json or {}).get("label") or "").strip()
    if not label:
        return jsonify({"error": "label requis"}), 400
    resnapshot_projet(pid)
    p = db_get_project(pid)
    if not p:
        return jsonify({"error": "projet introuvable"}), 404
    vid = db_add_project_version(pid, p.get("snapshot") or [], label=label)
    return jsonify({"status": "ok", "id": vid})

@bp.route("/api/projects/<int:pid>/versions/<int:vid>/rollback", methods=["POST"])
@require_project_role("owner")
def project_versions_rollback(pid, vid):
    """Retour arrière : le snapshot du projet redevient celui de la version (le live
    n'est PAS touché — recharger le projet applique la version). L'état courant est
    d'abord préservé en version automatique."""
    from ..database import (db_get_project_version, db_add_project_version,
                            db_update_project_snapshot)
    v = db_get_project_version(vid)
    if not v or v.get("project_id") != pid:
        return jsonify({"error": "version introuvable"}), 404
    p = db_get_project(pid)
    if (p.get("state") or "saved") in ("loading", "unloading"):
        return jsonify({"error": "chargement en cours"}), 409
    db_add_project_version(pid, p.get("snapshot") or [], label=None)
    db_update_project_snapshot(pid, v.get("snapshot") or [])
    return jsonify({"status": "ok", "containers": len(v.get("snapshot") or [])})

@bp.route("/api/projects/<int:pid>/versions/<int:vid>", methods=["DELETE"])
@require_project_role("owner")
def project_versions_delete(pid, vid):
    from ..database import db_get_project_version, db_delete_project_version
    v = db_get_project_version(vid)
    if not v or v.get("project_id") != pid:
        return jsonify({"error": "version introuvable"}), 404
    db_delete_project_version(vid)
    return jsonify({"status": "ok"})

# ─── Vues composées (chantier 2, cf. docs/reference/PROJETS.md §7) ───────────
#
# Règles : chaque membre ≥ operator compose SES vues privées ; le partage au projet
# (visibility=project) et l'édition d'une vue partagée (edit_shared) exigent ≥ editor.
# Suppression / changement de partage : propriétaire de la vue, owner du projet ou admin.

def _view_rights(view, pid):
    u = current_user() or {}
    role = project_role_for(pid) or ""
    from ..auth import project_role_at_least as _al, has_global_access
    is_owner = view.get("owner_id") == u.get("id")
    manage = has_global_access() or is_owner or _al(role, "owner")
    edit = manage or (view.get("visibility") == "project" and view.get("edit_shared")
                      and _al(role, "editor"))
    return edit, manage

@bp.route("/api/projects/<int:pid>/views", methods=["GET"])
@require_project_role("viewer")
def project_views_list(pid):
    uid = (current_user() or {}).get("id")
    from ..auth import has_global_access
    out = []
    for v in db_project_views(pid):
        if not (has_global_access() or v.get("owner_id") == uid
                or v.get("visibility") == "project"):
            continue
        edit, manage = _view_rights(v, pid)
        v["can_edit"], v["can_manage"] = edit, manage
        out.append(v)
    return jsonify({"views": out, "my_role": project_role_for(pid)})

@bp.route("/api/projects/<int:pid>/views", methods=["POST"])
@require_project_role("operator")
def project_views_create(pid):
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name requis"}), 400
    visibility = data.get("visibility") if data.get("visibility") in ("private", "project") else "private"
    from ..auth import project_role_at_least as _al, has_global_access
    if visibility == "project" and not (has_global_access()
            or _al(project_role_for(pid) or "", "editor")):
        return jsonify({"error": "forbidden", "reason": "share_requires_editor"}), 403
    vid = db_create_view(pid, name, (current_user() or {}).get("id"),
                         layout=data.get("layout") or [],
                         visibility=visibility,
                         edit_shared=bool(data.get("edit_shared")))
    return jsonify({"status": "ok", "view": db_get_view(vid)})

@bp.route("/api/projects/<int:pid>/views/<int:vid>", methods=["PATCH"])
@require_project_role("operator")
def project_views_update(pid, vid):
    v = db_get_view(vid)
    if not v or v.get("project_id") != pid:
        return jsonify({"error": "vue introuvable"}), 404
    edit, manage = _view_rights(v, pid)
    data = request.json or {}
    touches_sharing = ("visibility" in data or "edit_shared" in data)
    if touches_sharing and not manage:
        return jsonify({"error": "forbidden", "reason": "not_view_owner"}), 403
    if not edit:
        return jsonify({"error": "forbidden", "reason": "view_not_editable"}), 403
    layout = data.get("layout")
    if layout is not None and not isinstance(layout, list):
        return jsonify({"error": "layout doit être une liste"}), 400
    db_update_view(vid,
                   name=(data.get("name") or "").strip() or None,
                   layout=layout,
                   visibility=data.get("visibility"),
                   edit_shared=(bool(data["edit_shared"]) if "edit_shared" in data else None))
    return jsonify({"status": "ok", "view": db_get_view(vid)})

@bp.route("/api/projects/<int:pid>/views/<int:vid>", methods=["DELETE"])
@require_project_role("operator")
def project_views_delete(pid, vid):
    v = db_get_view(vid)
    if not v or v.get("project_id") != pid:
        return jsonify({"error": "vue introuvable"}), 404
    _edit, manage = _view_rights(v, pid)
    if not manage:
        return jsonify({"error": "forbidden", "reason": "not_view_owner"}), 403
    db_delete_view(vid)
    return jsonify({"status": "ok"})

@bp.route("/api/projects/<int:pid>/views/<int:vid>/duplicate", methods=["POST"])
@require_project_role("operator")
def project_views_duplicate(pid, vid):
    v = db_get_view(vid)
    uid = (current_user() or {}).get("id")
    from ..auth import has_global_access
    if not v or v.get("project_id") != pid or not (
            has_global_access() or v.get("owner_id") == uid
            or v.get("visibility") == "project"):
        return jsonify({"error": "vue introuvable"}), 404
    name = ((request.json or {}).get("name") or "").strip() or (v["name"] + " (copie)")
    nvid = db_create_view(pid, name, uid, layout=v.get("layout") or [])
    return jsonify({"status": "ok", "view": db_get_view(nvid)})

@bp.route("/api/projects", methods=["POST"])
@require_perm("projects.manage")
def sauvegarder_projet():
    import os, re
    from .. import settings as st
    data = request.json or {}
    name = (data.get("name") or "").strip()
    vmids = data.get("vmids") or []
    if not name or not vmids:
        return jsonify({"error": "name et vmids requis"}), 400
    # La passerelle WebRTC (webrtc_gateway) est une infra PERSISTANTE et partagée :
    # on l'exclut des snapshots (sinon le rappel en clonerait une copie « <projet>-… »).
    gateway_vmid = int(st.get("webrtc_gateway_vmid") or 0)
    if gateway_vmid:
        vmids = [v for v in vmids if int(v) != gateway_vmid]
    # Types exclus des projets (2110_io lié au nœud, storage…) : filtrés PAR TYPE.
    from ..projects import PROJECT_EXCLUDED_TYPES
    from .shared import _load_dc
    from ..database import db_get_container as _dgc
    def _excl(v):
        dc = _load_dc(_dgc(int(v)) or {}) or {}
        return dc.get("type") in PROJECT_EXCLUDED_TYPES
    vmids = [v for v in vmids if not _excl(v)]
    if not vmids:
        return jsonify({"error": "aucun container à sauvegarder (infra partagée/liée au "
                                 "nœud exclue : passerelle, 2110_io…)"}), 400
    # Dossier média dédié au projet sur le host : /srv/mxl-media/<slug>/
    from ..containers import MEDIA_HOST_DIR
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-") or "projet"
    slug = re.sub(r"-+", "-", slug)
    media_path = os.path.join(MEDIA_HOST_DIR, slug)
    try:
        os.makedirs(media_path, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"impossible de créer le dossier média : {e}"}), 500
    pid = db_save_project(name, vmids, media_path=media_path)
    return jsonify({"status": "ok", "id": pid})

@bp.route("/api/projects/<int:pid>/export", methods=["GET"])
@require_login
def exporter_projet(pid):
    p = db_get_project(pid)
    if not p:
        return jsonify({"error": "projet introuvable"}), 404
    # v2 (chantier 3) : embarque aussi les VUES composées (les widgets référencent
    # instance_uuid/vmid — remappés au chargement par _remap_project_views).
    payload = {
        "schema": "bobi.studio.project.v2",
        "name": p["name"],
        "created_at": p.get("created_at"),
        "snapshot": p.get("snapshot") or [],
        "views": [{"name": v["name"], "visibility": v.get("visibility") or "private",
                   "edit_shared": bool(v.get("edit_shared")), "layout": v.get("layout") or []}
                  for v in db_project_views(pid)],
    }
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in p["name"]) or f"projet-{pid}"
    return Response(
        json.dumps(payload, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{safe}.bsproj.json"'},
    )

@bp.route("/api/projects/import", methods=["POST"])
@require_perm("projects.manage")
def importer_projet():
    # Accepte soit un upload multipart (champ "file"), soit un JSON brut.
    raw = None
    if "file" in request.files:
        try:
            raw = json.loads(request.files["file"].read().decode("utf-8"))
        except Exception as e:
            return jsonify({"error": f"fichier illisible : {e}"}), 400
    else:
        raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        return jsonify({"error": "format invalide"}), 400

    snapshot = raw.get("snapshot")
    if not isinstance(snapshot, list) or not snapshot:
        return jsonify({"error": "snapshot manquant ou vide"}), 400

    # Nom : priorité au paramètre POST (renommage à l'import), sinon celui du fichier.
    override = (request.form.get("name") or "").strip() if request.form else ""
    name = override or (raw.get("name") or "").strip()
    if not name:
        return jsonify({"error": "nom manquant"}), 400

    pid = db_import_project(name, snapshot)
    # Dossier média garanti dès l'import (sinon les containers du projet retomberaient
    # sur la racine partagée) + vues embarquées (schema v2, importeur = propriétaire).
    from ..projects import _ensure_media_dir
    from ..database import db_get_project as _gp
    try:
        _ensure_media_dir(_gp(pid))
    except Exception:
        pass
    for v in (raw.get("views") or []):
        if isinstance(v, dict) and v.get("name"):
            db_create_view(pid, str(v["name"]), (current_user() or {}).get("id"),
                           layout=v.get("layout") or [],
                           visibility=v.get("visibility") if v.get("visibility") in
                                      ("private", "project") else "private",
                           edit_shared=bool(v.get("edit_shared")))
    return jsonify({"status": "ok", "id": pid, "name": name})

@bp.route("/api/projects/<int:pid>/restore_preview", methods=["GET"])
@require_perm("projects.manage")
def preview_restore(pid):
    from ..projects import _prefix_snapshot
    p = db_get_project(pid)
    if not p:
        return jsonify({"error": "projet introuvable"}), 404
    snapshot = sorted(p["snapshot"], key=lambda c: TYPE_PRIORITY.get(
        (c.get("deploy_config") or {}).get("type"), 99))
    # Mêmes hostnames/SHM que le rappel réel (préfixés par le nom du projet),
    # sinon le pré-vol détecte de faux conflits avec les originaux.
    snapshot = _prefix_snapshot(snapshot, p["name"])
    return jsonify(planifier_restore(snapshot))

# Flag d'interruption du restore (op globale : un seul restore verbeux à la fois).
_restore_abort = threading.Event()

@bp.route("/api/projects/<int:pid>/restore/abort", methods=["POST"])
@require_perm("projects.manage")
def restaurer_abort(pid):
    """Demande l'interruption du restore en cours (pris en compte entre containers)."""
    _restore_abort.set()
    return jsonify({"ok": True})

@bp.route("/api/projects/<int:pid>/restore", methods=["POST"])
@require_perm("projects.manage")
def restaurer(pid):
    """Restaure un projet en streaming (log verbeux + bilan + liste des échecs)."""
    import queue as _queue
    data       = request.json or {}
    only_vmids = data.get("only_vmids")   # None = tout ; liste = reprise des échecs
    preserve_uuid = bool(data.get("preserve_uuid"))   # True = déplacement (conserver l'identité)

    def restore_iter():
        _restore_abort.clear()
        q   = _queue.Queue()
        box = {}
        def worker():
            try:
                box["summary"] = restaurer_projet(
                    pid,
                    progress=lambda m: q.put(m),
                    should_abort=lambda: _restore_abort.is_set(),
                    only_vmids=only_vmids,
                    preserve_uuid=preserve_uuid)
            except Exception as e:
                box["err"] = str(e)
            finally:
                q.put(None)
        threading.Thread(target=worker, daemon=True).start()

        while True:
            m = q.get()
            if m is None:
                break
            yield m + "\n"

        summary = box.get("summary") or {"ok": 0, "total": 0, "failed": []}
        if box.get("err"):
            yield f"✕ Erreur : {box['err']}\n"
        ok, total, failed = summary.get("ok", 0), summary.get("total", 0), summary.get("failed", [])
        if failed:
            yield (f"⚠ Restauration terminée : {ok}/{total} OK, {len(failed)} échec(s).\n")
        else:
            yield f"✅ Restauration terminée : {ok}/{total} OK.\n"
        # Marqueur machine pour l'UI (bouton « réessayer »).
        yield "__SUMMARY__" + json.dumps(summary) + "\n"

    return Response(stream_with_context(restore_iter()),
                    mimetype="text/plain; charset=utf-8",
                    headers={"X-Accel-Buffering": "no",
                             "Cache-Control": "no-cache"})

@bp.route("/api/projects/<int:pid>/destroy_containers", methods=["POST"])
@require_perm("projects.manage")
def detruire_projet_containers(pid):
    # Journal d'exploitation : destruction EN MASSE — c'est exactement le geste qu'on veut pouvoir
    # attribuer à la relecture. Ligne posée avant le dispatch (cf. app/audit.py).
    from ..audit import journal as _journal
    from ..database import db_get_project
    # Repli sans le mot « projet » : la cible est une DONNÉE, et un mot français dedans
    # ressortirait tel quel au milieu d'une phrase traduite (la phrase, elle, dit déjà « projet »).
    _journal("alert.audit.detruire_conteneurs_projet",
             cible=(db_get_project(pid) or {}).get("name") or f"#{pid}", kind="deploy")
    threading.Thread(target=detruire_containers_projet, args=(pid,)).start()
    return jsonify({"status": "destruction_en_cours"})

@bp.route("/api/projects/<int:pid>", methods=["DELETE"])
@require_perm("projects.manage")
def supprimer_projet(pid):
    from ..audit import journal as _journal
    from ..database import db_get_project
    _journal("alert.audit.supprimer_projet",
             cible=(db_get_project(pid) or {}).get("name") or f"#{pid}", kind="deploy")
    db_delete_project(pid)
    return jsonify({"status": "supprimé"})
