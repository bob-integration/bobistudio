# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""API du REGISTRE DES MÉDIAS (app/media_registry.py) — cf. docs/chantiers/MEDIAS_REGISTRE.md.

Lecture : connecté. Écriture (métadonnées, vignette, import, suppression, champs, sauvegarde) :
permission `media.manage`. Les opérations longues (analyse des volumes, reprise de l'existant)
partent en tâche de fond ; leur état se lit sur la même route en GET.

PORTÉE (2026-10-12) : un utilisateur à portée restreinte (`auth.scoped_project_ids()` non None) ne
voit et n'atteint que les médias rattachés à l'un de SES projets. Média ou bin hors portée : 404
(on ne confirme pas son existence), en lecture comme en action ; listes, recherches, bins et
compteurs filtrés. Ce qui est GLOBAL par nature (intégrité, sauvegarde / export / import de la base,
champs personnalisés, réglages, analyse de tous les volumes, reprise de l'existant, rattrapage des
planches) reste réservé à l'accès global, même avec `media.manage`.

PARTAGES (2026-10-12, app/media_sharing.py) : la portée est ÉTENDUE aux médias et bins partagés
avec l'utilisateur (`_scope()` = media_sharing.user_scope()). Chaque geste dit ce qu'il exige
(`need`) : « use » (lire, charger, rapatrier, copier), « edit » (fiche, vignette, proxy, planche,
ranger dans un bin partagé en « Modifier »), « owner » (supprimer, oublier une copie, renommer un
fichier, rattacher à un projet, supprimer un proxy, créer / renommer / supprimer un bin — périmètre
propriétaire seul). Partagé mais niveau insuffisant : 403 `shared_read_only` (l'existence est
déjà connue de l'utilisateur)."""

import io
import os
import threading
import time

from flask import Response, jsonify, request, send_file, stream_with_context

from . import bp
from ..auth import require_login, require_perm, has_global_access
from .. import media_registry as mr
from .. import media_sharing as ms

_job = {"kind": "", "running": False, "started": 0.0, "ended": 0.0, "result": None, "error": "", "progress": None, "uid": None}
_job_lock = threading.Lock()


def _start(kind, fn):
    with _job_lock:
        if _job["running"]:
            return False
        from ..auth import current_user
        try:
            uid = (current_user() or {}).get("id")
        except RuntimeError:                     # hors requête
            uid = None
        _job.update(kind=kind, running=True, started=time.time(), ended=0.0, result=None, error="", progress=None, uid=uid)

    def run():
        try:
            res = fn()
            with _job_lock:
                _job["result"] = res
        except Exception as e:                                             # noqa: BLE001
            with _job_lock:
                _job["error"] = str(e)
        finally:
            with _job_lock:
                _job["running"] = False
                _job["ended"] = time.time()
    threading.Thread(target=run, daemon=True, name="media-" + kind).start()
    return True


def _progress(done, total, name):
    """Avancement d'une tâche longue (copie d'un bin), lu par GET /api/media/scan."""
    with _job_lock:
        _job["progress"] = {"done": done, "total": total, "name": name}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _scope():
    return ms.user_scope()


def _shared_denied():
    return jsonify({"ok": False, "error": "forbidden", "reason": "shared_read_only"}), 403


def _hidden(aid, scope=None, need="use"):
    """Réponse 404 si le média est hors de la portée de l'utilisateur (ou inconnu), 403 s'il ne le
    voit que par un partage dont le niveau ne suffit pas à `need`, sinon None. Accès global : rien
    n'est vérifié ici (comportement d'avant inchangé)."""
    scope = _scope() if scope is None else scope
    if scope is not None and not mr.asset_visible(aid, scope):
        return jsonify({"ok": False, "error": "média inconnu"}), 404
    if scope is not None and need != "use" and not ms.asset_allows(scope, aid, need):
        return _shared_denied()
    return None


def _bin_hidden(bid, scope=None, need="use"):
    scope = _scope() if scope is None else scope
    if scope is not None and not mr.bin_visible(bid, scope):
        return jsonify({"ok": False, "error": "bin inconnu"}), 404
    if scope is not None and need != "use" and not ms.bin_allows(scope, bid, need):
        return _shared_denied()
    return None


def _project_denied(pid, scope=None):
    scope = _scope() if scope is None else scope
    if pid is not None and not mr.project_in_scope(pid, scope):
        return jsonify({"ok": False, "error": "forbidden", "reason": "not_project_member"}), 403
    return None


def _global_only():
    """Opérations globales de la base (intégrité, sauvegarde…) : réservées à l'accès global."""
    if not has_global_access():
        return jsonify({"error": "forbidden", "reason": "global_access_required"}), 403
    return None


@bp.route("/api/media/assets")
@require_login
def registry_assets():
    """Médias de la base (d'un projet, ou tous). `node` : marque pour chacun s'il a une copie
    présente sur ce nœud (`local`) — c'est la colonne « copie locale » du player."""
    # `vmid` : la vue d'un CONTENEUR (player…) — son nœud, et le sous-dossier de son projet. Chaque
    # média reçoit alors `local_path` (chemin à lui passer) quand une copie lui est accessible.
    vmid = _int(request.args.get("vmid"))
    if vmid is not None:
        from ..auth import check_vmid_access
        err = check_vmid_access(vmid, "viewer")
        if err:
            return err
    scope = _scope()
    out = mr.list_assets(project_id=_int(request.args.get("project")), q=(request.args.get("q") or "").strip() or None,
                         scope=scope)
    node = _annotate(out, vmid, _int(request.args.get("node")), scope)
    return jsonify({"assets": out, "fields": mr.list_fields(), "node_id": node})


def _annotate(out, vmid, node=None, scope=None):
    """Ajoute à chaque média, pour la vue d'un conteneur : `local_path` (copie qu'il voit),
    `available_nodes`, `on_share`, `growing` ; et, pour tous, `projects` / `homonyms` /
    `name_hint` (homonymes dans la portée, cf. mr.annotate_names). Renvoie le nœud retenu."""
    mr.annotate_names(out, scope)
    ms.annotate_assets(out, scope)
    prefix = ""
    if vmid is not None:
        node, prefix = mr.container_view(vmid)
    from ..database import db_get_nodes
    names = {n["id"]: n.get("name") for n in db_get_nodes()}
    for a in out:
        # La LIGNE d'un média vaut sa version par défaut : copie locale, nœuds, copies, écriture en cours.
        dv = a.get("default_version") or "original"
        copies = [c for c in a.pop("copies", []) if c.get("version", "original") == dv]
        nodes = [c for c in copies if c["state"] == "present" and c["node_id"]]
        a["local_path"] = mr.local_path(nodes, node, prefix) if node is not None else None
        a["available_nodes"] = sorted({names.get(c["node_id"], "#%s" % c["node_id"]) for c in nodes})
        a["on_share"] = any(c.get("share_id") for c in copies)
        a["growing"] = any(c["growing"] for c in nodes)
        # Colonne « Copies » : le NOMBRE (le détail est dans Infos → Emplacements).
        a["copies_n"] = len(nodes) + sum(1 for c in copies if c.get("share_id"))
    return node


@bp.route("/api/media/search_fields")
@require_login
def registry_search_fields():
    from ..database import db_get_nodes, db_get_projects
    scope = _scope()
    return jsonify({"fields": mr.search_fields(),
                    "nodes": [{"id": n["id"], "name": n.get("name")} for n in db_get_nodes()],
                    "projects": [{"id": p["id"], "name": p.get("name")} for p in (db_get_projects() or [])
                                 if mr.project_in_scope(p["id"], scope)]})


@bp.route("/api/media/search_values")
@require_login
def registry_search_values():
    """?field= → valeurs distinctes présentes dans la base (aide de la recherche avancée)."""
    try:
        return jsonify({"field": request.args.get("field") or "",
                        "values": mr.search_values(request.args.get("field") or "", _int(request.args.get("limit")),
                                                   scope=_scope())})
    except ValueError as e:
        return jsonify({"error": str(e), "values": []}), 400


@bp.route("/api/media/context")
@require_login
def registry_context():
    """?vmid= → {node_id, project_id, project_name} : la bibliothèque d'un conteneur de projet
    s'ouvre filtrée sur ce projet."""
    from ..auth import check_vmid_access
    vmid = _int(request.args.get("vmid"))
    if vmid is None:
        return jsonify({"error": "vmid requis"}), 400
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    return jsonify(mr.container_context(vmid))


@bp.route("/api/media/search", methods=["POST"])
@require_login
def registry_search():
    """{query, project?, vmid?} → médias. `query` : {q, kinds?, match: all|any, items: [{field, op, value} | groupe]}
    (groupes imbriqués, cf. media_registry.query_tree) ; l'ancien {q, all:[…], any:[…]} reste lu."""
    b = request.get_json(silent=True) or {}
    vmid = _int(b.get("vmid"))
    if vmid is not None:
        from ..auth import check_vmid_access
        err = check_vmid_access(vmid, "viewer")
        if err:
            return err
    scope = _scope()
    try:
        out = mr.search(b.get("query") or {}, project_id=_int(b.get("project")), scope=scope)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    _annotate(out, vmid, None, scope)
    return jsonify({"assets": out})


@bp.route("/api/media/bins", methods=["GET", "POST"])
@require_login
def registry_bins():
    if request.method == "POST":
        from ..auth import has_perm
        if not has_perm("media.manage"):
            return jsonify({"error": "forbidden", "missing_permission": "media.manage"}), 403
        b = request.get_json(silent=True) or {}
        scope = _scope()
        # Portée : on ne crée un bin que dans SES projets (ou sans projet), sous un bin visible, et
        # on ne modifie qu'un bin visible.
        for chk in (_project_denied(_int(b.get("project_id")), scope),
                    _bin_hidden(_int(b.get("parent_id")), scope, "owner") if _int(b.get("parent_id")) is not None else None,
                    _bin_hidden(_int(b.get("id")), scope, "owner") if _int(b.get("id")) is not None else None):
            if chk:
                return chk
        try:
            bid = mr.save_bin(b.get("name"), parent_id=_int(b.get("parent_id")), project_id=_int(b.get("project_id")),
                              query=b.get("query") or None, bin_id=_int(b.get("id")))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "id": bid, "bins": ms.annotate_bins(mr.list_bins(_int(b.get("project_id")), scope=scope), scope)})
    # Reprise unique des bins de l'ancien Gestionnaire (no-op une fois faite).
    try:
        mr.migrate_legacy_bins()
    except Exception:                                                      # noqa: BLE001
        pass
    scope = _scope()
    return jsonify({"bins": ms.annotate_bins(mr.list_bins(_int(request.args.get("project")), scope=scope), scope)})


@bp.route("/api/media/bins/<int:bid>", methods=["DELETE"])
@require_perm("media.manage")
def registry_bin_delete(bid):
    scope = _scope()
    err = _bin_hidden(bid, scope, "owner")
    if err:
        return err
    if scope is not None:
        # Supprimer un bin emporte ses sous-bins : refusé si l'un d'eux est hors portée.
        vis = {x for x in mr.bin_subtree(bid) if mr.bin_visible(x, ms.base_scope(scope))}
        if vis != set(mr.bin_subtree(bid)):
            return jsonify({"ok": False, "error": "ce bin contient des bins d'autres projets"}), 403
    mr.delete_bin(bid)
    return jsonify({"ok": True})


@bp.route("/api/media/bins/<int:bid>/items", methods=["POST"])
@require_perm("media.manage")
def registry_bin_items(bid):
    b = request.get_json(silent=True) or {}
    scope = _scope()
    err = _bin_hidden(bid, scope, "edit")        # bin partagé : ranger / retirer en « Modifier » seulement
    if err:
        return err
    ids = [x for x in (b.get("asset_ids") or []) if isinstance(x, str)]
    if scope is not None and len(mr.visible_ids(ids, scope)) != len(ids):
        return jsonify({"ok": False, "error": "média inconnu"}), 404
    try:
        mr.bin_add(bid, ids, on=b.get("on", True) is not False)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@bp.route("/api/media/bins/<int:bid>/assets", methods=["POST"])
@require_login
def registry_bin_assets(bid):
    """Contenu d'un bin, réévalué maintenant ; {vmid, query} : vue d'un conteneur + filtre."""
    b = request.get_json(silent=True) or {}
    scope = _scope()
    err = _bin_hidden(bid, scope)
    if err:
        return err
    vmid = _int(b.get("vmid"))
    if vmid is not None:
        from ..auth import check_vmid_access
        err = check_vmid_access(vmid, "viewer")
        if err:
            return err
    try:
        out = mr.bin_assets(bid, extra=b.get("query") or None, children=bool(b.get("children")), scope=scope)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    _annotate(out, vmid, None, scope)
    return jsonify({"assets": out})


@bp.route("/api/media/bins/<int:bid>/copy", methods=["POST"])
@require_login
def registry_bin_copy(bid):
    """Tout le contenu d'un bin (et de ses sous-bins), copié et vérifié : {node_id} vers ce nœud
    (gestion des médias), ou {vmid} sur le nœud de ce conteneur, dans son champ de vision
    (pilotage du conteneur). Tâche de fond ; avancement et résultat sur GET /api/media/scan."""
    from ..auth import check_vmid_access, has_perm
    b = request.get_json(silent=True) or {}
    vmid, nid = _int(b.get("vmid")), _int(b.get("node_id"))
    if (vmid is None) == (nid is None):
        return jsonify({"ok": False, "error": "node_id ou vmid requis"}), 400
    if vmid is not None:
        err = check_vmid_access(vmid, "operator")
        if err:
            return err
        if not has_perm("plugins.operate"):
            return jsonify({"error": "forbidden", "missing_permission": "plugins.operate"}), 403
    elif not has_perm("media.manage"):
        return jsonify({"error": "forbidden", "missing_permission": "media.manage"}), 403
    children = b.get("children", True) is not False
    scope = _scope()
    err = _bin_hidden(bid, scope)
    if err:
        return err
    try:
        content = mr.bin_assets(bid, children=children, scope=scope)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    n = len(content)
    ms.guard_container_load(scope, [a["id"] for a in content], vmid)
    if not _start("bin_copy", lambda: mr.copy_bin(bid, node_id=nid, vmid=vmid, children=children, progress=_progress,
                                                  scope=scope)):
        return jsonify({"ok": False, "error": "une opération est déjà en cours"}), 409
    return jsonify({"ok": True, "background": True, "total": n})


@bp.route("/api/media/assets/<aid>/rename_copy", methods=["POST"])
@require_perm("media.manage")
def registry_asset_rename_copy(aid):
    """{node_id, path, new_name} : renomme le fichier d'une copie (et son sidecar) sur son nœud.
    L'identité et le nom du média ne changent pas. Refusé si la cible existe."""
    err = _hidden(aid, need="owner")
    if err:
        return err
    b = request.get_json(silent=True) or {}
    try:
        p = mr.rename_copy(aid, _int(b.get("node_id")) or 0, b.get("path") or "", b.get("new_name") or "")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "path": p})


@bp.route("/api/media/assets/<aid>/sidecar")
@require_login
def registry_asset_sidecar(aid):
    """?node_id=&path=[&share_id=] → {xml} : le sidecar brut d'une copie, en lecture seule."""
    err = _hidden(aid)
    if err:
        return err
    try:
        x = mr.read_sidecar(aid, _int(request.args.get("node_id")) or 0, request.args.get("path") or "",
                            share_id=_int(request.args.get("share_id")) or 0)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "xml": x})


@bp.route("/api/media/assets/<aid>")
@require_login
def registry_asset(aid):
    scope = _scope()
    a = mr.get_asset(aid, scope=scope)
    if not a:
        return jsonify({"error": "média inconnu"}), 404
    a["fields"] = mr.list_fields()
    # Ce que l'utilisateur peut faire de CE média (l'interface grise le reste) ; partagé : par qui.
    a["access"] = ("owner" if scope is None or ms.owns_asset(scope, aid)
                   else "edit" if ms.asset_allows(scope, aid, "edit") else "use")
    if a["access"] != "owner":
        a["shared"] = {"by": scope.asset_by.get(aid, ""), "level": a["access"]}
    return jsonify(a)


@bp.route("/api/media/assets/<aid>", methods=["PATCH"])
@require_perm("media.manage")
def registry_asset_update(aid):
    err = _hidden(aid, need="edit")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    a = mr.update_meta(aid, body.get("meta") or {}, name=body.get("name"))
    return (jsonify(a), 200) if a else (jsonify({"error": "média inconnu"}), 404)


@bp.route("/api/media/assets/<aid>/thumb")
@require_login
def registry_asset_thumb(aid):
    err = _hidden(aid)
    if err:
        return err
    p = mr.ensure_thumb(aid)
    if not p:
        return jsonify({"error": "vignette indisponible"}), 404
    r = send_file(p, mimetype="image/jpeg", max_age=0)
    # La version (`v`) est dans l'URL : une vignette changée change d'URL ; celle-ci peut se garder.
    if request.args.get("v"):
        r.headers["Cache-Control"] = "private, max-age=86400"
    return r


@bp.route("/api/media/assets/<aid>/frame")
@require_login
def registry_asset_frame(aid):
    """Image à l'instant ?t= (secondes), largeur ?w= — pour choisir la vignette ; non stockée."""
    err = _hidden(aid)
    if err:
        return err
    try:
        t = max(0.0, float(request.args.get("t") or 0))
        w = max(64, min(960, int(request.args.get("w") or 480)))
    except ValueError:
        return jsonify({"error": "t / w invalides"}), 400
    data = mr.frame(aid, t, w)
    if not data:
        return jsonify({"error": "image indisponible"}), 404
    return send_file(io.BytesIO(data), mimetype="image/jpeg", max_age=0)


@bp.route("/api/media/assets/<aid>/thumb", methods=["POST"])
@require_perm("media.manage")
def registry_asset_thumb_set(aid):
    err = _hidden(aid, need="edit")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        a = mr.set_thumb(aid, t=body.get("t"), reset=bool(body.get("reset")))
    except (ValueError, RuntimeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "asset": a})


@bp.route("/api/media/assets/<aid>", methods=["DELETE"])
@require_perm("media.manage")
def registry_asset_delete(aid):
    """Sans corps : le média ENTIER (toutes ses copies sur les nœuds). Avec {node_id, path} :
    cette copie seulement. Les copies sur partage réseau ne sont jamais supprimées d'ici."""
    err = _hidden(aid, need="owner")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        if body.get("path"):
            mr.delete_copy(aid, body.get("node_id"), body["path"])
        else:
            mr.delete_asset(aid, with_files=True)
    except (ValueError, RuntimeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@bp.route("/api/media/assets/<aid>/default_version", methods=["POST"])
@require_perm("media.manage")
def registry_asset_default_version(aid):
    """{version} : la version que charge « Charger ce média » (l'original : retour au cas général)."""
    err = _hidden(aid, need="edit")
    if err:
        return err
    b = request.get_json(silent=True) or {}
    try:
        key = mr.set_default_version(aid, b.get("version") or "original")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "default_version": key})


@bp.route("/api/media/assets/<aid>/versions/<key>", methods=["DELETE"])
@require_perm("media.manage")
def registry_asset_version_delete(aid, key):
    """Supprime UNE version (ses copies sur les nœuds, puis la version) ; jamais l'original."""
    err = _hidden(aid, need="owner")
    if err:
        return err
    try:
        mr.delete_version(aid, key)
    except (ValueError, RuntimeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@bp.route("/api/media/assets/<aid>/forget", methods=["POST"])
@require_perm("media.manage")
def registry_asset_forget(aid):
    err = _hidden(aid, need="owner")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        mr.forget_copy(aid, body.get("node_id"), body.get("path") or "")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@bp.route("/api/media/assets/<aid>/projects", methods=["POST"])
@require_perm("media.manage")
def registry_asset_projects(aid):
    err = _hidden(aid, need="owner")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    pid = _int(body.get("project_id"))
    if pid is None:
        return jsonify({"ok": False, "error": "project_id requis"}), 400
    err = _project_denied(pid)
    if err:
        return err
    mr.link_project(aid, pid, on=body.get("on", True) is not False)
    return jsonify({"ok": True})


@bp.route("/api/media/scan", methods=["GET", "POST"])
@require_login
def registry_scan():
    if request.method == "POST":
        from ..auth import has_perm, check_vmid_access
        body = request.get_json(silent=True) or {}
        vmid = _int(body.get("vmid"))
        if vmid is not None:
            # Analyse du nœud d'UN conteneur (un recorder qui démarre ou s'arrête) : ouverte à qui
            # voit ce conteneur — elle n'importe que ce que les producteurs ont déjà marqué.
            err = check_vmid_access(vmid, "viewer")
            if err:
                return err
            nid, _p = mr.container_view(vmid)
            if not _start("scan", lambda: [mr.scan_node(nid, probe_max=20)]):
                return jsonify({"ok": False, "error": "une opération est déjà en cours"}), 409
            with _job_lock:
                return jsonify({k: v for k, v in _job.items() if k != "uid"})
        if not has_perm("media.manage"):
            return jsonify({"error": "forbidden", "missing_permission": "media.manage"}), 403
        err = _global_only()                     # analyse des volumes : opération d'infrastructure
        if err:
            return err
        nid = _int(body.get("node_id"))
        fn = (lambda: [mr.scan_node(nid)]) if nid else mr.scan_all
        if not _start("scan", fn):
            return jsonify({"ok": False, "error": "une opération est déjà en cours", "job": _job}), 409
    with _job_lock:
        j = dict(_job)
    if _scope() is not None:
        from ..auth import current_user
        if j.get("uid") != (current_user() or {}).get("id"):
            # Tâche lancée par un AUTRE (ids, noms de médias) : son résultat n'est pas pour un restreint.
            j["result"], j["progress"], j["error"] = None, None, ""
    j.pop("uid", None)
    return jsonify(j)


@bp.route("/api/media/import", methods=["POST"])
@require_perm("media.manage")
def registry_import():
    """{node_id, paths:[…], project_id?} importe ces fichiers ; {all:true, node_id?} reprend tout
    l'existant non référencé (en tâche de fond)."""
    body = request.get_json(silent=True) or {}
    pid = _int(body.get("project_id"))
    scope = _scope()
    if scope is not None:
        # Un restreint importe DANS un de ses projets — sans projet, le média lui serait aussitôt
        # invisible. Reprise de tout l'existant : globale par nature.
        if body.get("all"):
            return _global_only()
        if pid is None:
            pid = next(iter(scope)) if len(scope) == 1 else None
        if pid is None:
            return jsonify({"ok": False, "error": "project_id requis (le média doit entrer dans un de vos projets)"}), 400
        err = _project_denied(pid, scope)
        if err:
            return err
    if body.get("all"):
        nid = _int(body.get("node_id"))
        if not _start("import", lambda: {"imported": mr.import_all_unreferenced(nid, project_id=pid)}):
            return jsonify({"ok": False, "error": "une opération est déjà en cours"}), 409
        return jsonify({"ok": True, "background": True})
    nid = _int(body.get("node_id"))
    paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]
    if nid is None or not paths:
        return jsonify({"ok": False, "error": "node_id et paths requis"}), 400
    if scope is not None:
        # Un fichier DÉJÀ média de la base, hors portée : l'importer le rattacherait à son projet
        # (donc le révélerait). Refusé comme inconnu.
        known = [r["asset_id"] for r in (mr.db().execute(
            "SELECT asset_id FROM media_files WHERE node_id=? AND share_id=0 AND kind='main' AND path=? AND asset_id IS NOT NULL",
            (nid, p)).fetchone() for p in paths) if r]
        if len(mr.visible_ids(known, ms.base_scope(scope))) != len(known):   # partagé : ne s'importe pas
            return jsonify({"ok": False, "error": "média inconnu"}), 404
    try:
        ids = mr.import_files(nid, paths, project_id=pid)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "ids": ids})


@bp.route("/api/media/import_share", methods=["POST"])
@require_perm("media.manage")
def registry_import_share():
    """{vmid, path} : `path` tel que le conteneur le voit sous /mnt/ext — vérifie, copie sur le
    nœud du conteneur, enregistre. {node_id, path} : vers ce nœud, portée globale (Gestionnaire).
    Long (copie) : tâche de fond, état sur GET /api/media/scan."""
    body = request.get_json(silent=True) or {}
    vmid, nid, path = _int(body.get("vmid")), _int(body.get("node_id")), (body.get("path") or "").strip()
    if (vmid is None and nid is None) or not path:
        return jsonify({"ok": False, "error": "vmid ou node_id, et path requis"}), 400
    if vmid is not None:
        from ..auth import check_vmid_access
        err = check_vmid_access(vmid, "operator")
        if err:
            return err
        if _scope() is not None and not mr.container_context(vmid).get("project_id"):
            # Le média entre dans le projet du CONTENEUR : sans projet, il serait invisible.
            return jsonify({"ok": False, "error": "ce conteneur n'est rattaché à aucun projet"}), 403
    else:
        err = _global_only()                     # vers un nœud, portée réseau globale : Gestionnaire
        if err:
            return err
    if not _start("import_share", lambda: mr.import_from_share(vmid, path, node_id=nid)):
        return jsonify({"ok": False, "error": "une opération est déjà en cours"}), 409
    return jsonify({"ok": True, "background": True})


@bp.route("/api/media/browse_share")
@require_login
def registry_browse_share():
    """Un niveau d'un serveur réseau, vu par le conteneur `vmid` : ?vmid=&path=<entrée>/<dossier>."""
    vmid = _int(request.args.get("vmid"))
    if vmid is not None:
        from ..auth import check_vmid_access
        err = check_vmid_access(vmid, "viewer")
        if err:
            return err
    elif _scope() is not None:
        return jsonify({"error": "forbidden", "reason": "global_access_required", "dirs": [], "files": []}), 403
    try:
        return jsonify(mr.browse_share(vmid, request.args.get("path") or ""))
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e), "dirs": [], "files": []}), 404


@bp.route("/api/media/copy", methods=["POST"])
@require_perm("media.manage")
def registry_copy():
    """{asset_ids, node_id, version?} : copies VÉRIFIÉES vers ce nœud (tâche de fond, état sur
    /api/media/scan) — de la version par défaut de chaque média, ou de `version`."""
    b = request.get_json(silent=True) or {}
    ids = [x for x in (b.get("asset_ids") or []) if isinstance(x, str)]
    nid = _int(b.get("node_id"))
    if not ids or nid is None:
        return jsonify({"ok": False, "error": "asset_ids et node_id requis"}), 400
    scope = _scope()
    if scope is not None and len(mr.visible_ids(ids, scope)) != len(ids):
        return jsonify({"ok": False, "error": "média inconnu"}), 404

    def run():
        done, errors = [], {}
        for aid in ids:
            try:
                done.append(mr.copy_asset(aid, nid, version=b.get("version") or None))
            except (ValueError, RuntimeError) as e:
                errors[aid] = str(e)
        if errors and not done:
            raise RuntimeError("; ".join(sorted(set(errors.values())))[:400])
        return {"copied": done, "errors": errors}
    if not _start("copy", run):
        return jsonify({"ok": False, "error": "une opération est déjà en cours"}), 409
    return jsonify({"ok": True, "background": True})


@bp.route("/api/media/fetch", methods=["POST"])
@require_login
def registry_fetch():
    """{asset_id, vmid, version?} : rapatrie le média pour ce conteneur (copie vérifiée dans son
    champ de vision ; sa version par défaut, ou `version`), en tâche de fond SUIVIE (app/media_fetch.py) : réponse immédiate `{ok, job, state,
    background}` ; copie déjà là → `state: done` et `path` (= `local_path`) tout de suite. Une
    seconde demande pour la même copie rejoint la tâche en cours. Avancement :
    GET /api/media/fetch/<job>."""
    from ..auth import check_vmid_access, has_perm
    b = request.get_json(silent=True) or {}
    vmid, aid = _int(b.get("vmid")), b.get("asset_id") or ""
    if vmid is None or not aid:
        return jsonify({"ok": False, "error": "vmid et asset_id requis"}), 400
    err = check_vmid_access(vmid, "operator")
    if err:
        return err
    if not has_perm("plugins.operate"):
        return jsonify({"error": "forbidden", "missing_permission": "plugins.operate"}), 403
    scope = _scope()
    err = _hidden(aid, scope)                    # le rapatrier le ferait entrer dans le projet du conteneur
    if err:
        return err
    ms.guard_container_load(scope, [aid], vmid)  # … sauf s'il n'est vu que par un partage
    from .. import media_fetch
    try:
        j = media_fetch.start(aid, vmid, version=b.get("version") or None)
    except (ValueError, RuntimeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify(_fetch_view(j))


def _fetch_view(j):
    out = dict(j, ok=j["state"] != "failed", background=j["state"] != "done")
    if j["state"] == "done":
        out["local_path"] = j["path"]
    return out


@bp.route("/api/media/fetch/<job>")
@require_login
def registry_fetch_status(job):
    """État d'un rapatriement : {state: queued|copying|verifying|done|failed, done_bytes,
    total_bytes, rate (o/s mesuré), eta_s, error, path}. Lisible par qui voit le conteneur."""
    from .. import media_fetch
    from ..auth import check_vmid_access
    j = media_fetch.get(job)
    if not j:
        return jsonify({"ok": False, "error": "tâche inconnue ou expirée", "state": "unknown"}), 404
    err = check_vmid_access(j["vmid"], "viewer")
    if err:
        return err
    return jsonify(_fetch_view(j))


@bp.route("/api/media/share_status", methods=["POST"])
@require_login
def registry_share_status():
    body = request.get_json(silent=True) or {}
    vmid = _int(body.get("vmid"))
    paths = [p for p in (body.get("paths") or []) if isinstance(p, str)][:2000]
    if vmid is None:
        return jsonify({"imported": {}})
    from ..auth import check_vmid_access
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    out = mr.share_status_for(vmid, paths)
    scope = _scope()
    if scope is not None:
        ok = set(mr.visible_ids(list(out.values()), scope))
        out = {k: v for k, v in out.items() if v in ok}
    return jsonify({"imported": out})


@bp.route("/api/media/send_targets")
@require_login
def registry_send_targets():
    """Player / Images fixes démarrés que l'utilisateur peut piloter — « Envoyer vers »."""
    from ..auth import check_vmid_access, has_perm
    if not has_perm("plugins.operate"):
        return jsonify({"targets": []})
    return jsonify({"targets": mr.send_targets(lambda v: check_vmid_access(v, "operator") is None)})


@bp.route("/api/media/send", methods=["POST"])
@require_login
def registry_send():
    """{vmid, action, asset_ids?, items?:[{node_id, path}], version?} → le conteneur charge / ajoute /
    affiche (la version par défaut de chaque média, ou `version`). Refuse ce qui n'a pas de copie
    que ce conteneur voit, en le nommant."""
    from ..auth import check_vmid_access, has_perm
    b = request.get_json(silent=True) or {}
    vmid = _int(b.get("vmid"))
    if vmid is None:
        return jsonify({"ok": False, "error": "vmid requis"}), 400
    err = check_vmid_access(vmid, "operator")
    if err:
        return err
    if not has_perm("plugins.operate"):
        return jsonify({"error": "forbidden", "missing_permission": "plugins.operate"}), 403
    scope = _scope()
    paths, missing, origins = mr.resolve_for_container(vmid, [x for x in (b.get("asset_ids") or []) if isinstance(x, str)],
                                                       [x for x in (b.get("items") or []) if isinstance(x, dict)],
                                                       scope=scope, version=b.get("version") or None)
    ms.guard_container_load(scope, set(origins.values()), vmid)   # partagé : chargé, pas rattaché
    if not paths:
        return jsonify({"ok": False, "error": "aucune copie accessible à ce conteneur : %s" % ", ".join(missing[:5]),
                        "missing": missing}), 409
    try:
        from ..auth import current_user
        u = current_user() or {}
        n = mr.send_to(vmid, b.get("action") or "", paths, params=b.get("params") or None,
                       origins=origins, user=u.get("username") or u.get("login") or "")
    except (ValueError, RuntimeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "sent": n, "missing": missing})


@bp.route("/api/media/settings", methods=["GET", "POST"])
@require_login
def registry_settings():
    """Copies entre nœuds : plafond de débit (Mo/s, 0 = sans plafond) et, par nœud, le transport
    utilisé — direct entre agents (≥ 0.24.0 + TLS) ou repli root-SSH par l'orchestrateur."""
    from .. import settings as _st, node_driver
    from ..database import db_get_nodes
    if request.method == "POST":
        from ..auth import has_perm
        if not has_perm("media.manage"):
            return jsonify({"error": "forbidden", "missing_permission": "media.manage"}), 403
        err = _global_only()                     # réglage de toute l'installation
        if err:
            return err
        b = request.get_json(silent=True) or {}
        if "copy_rate_mbps" in b:
            try:
                v = float(b.get("copy_rate_mbps"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "débit en Mo/s attendu"}), 400
            if not 0 <= v <= 10000:
                return jsonify({"ok": False, "error": "débit hors bornes (0 à 10 000 Mo/s)"}), 400
            _st.set("media_copy_rate_mbps", v)
        if "proxy_auto" in b:
            # Proxys automatiques (import d'une vidéo, fin d'enregistrement, sortie du transcodeur).
            _st.set("media_proxy_auto", "1" if b.get("proxy_auto") in (True, 1, "1", "true", "on") else "0")
        if "edit_output" in b:
            # Une image retouchée devient : une fiche à part (défaut) / une version de son original.
            from .. import media_edit as _me
            if b.get("edit_output") not in (_me.OUTPUT_FICHE, _me.OUTPUT_VERSION):
                return jsonify({"ok": False, "error": "edit_output : asset ou version"}), 400
            _st.set("media_edit_output", b.get("edit_output"))
    from .. import media_edit as _me
    rate = _st.get("media_copy_rate_mbps")
    return jsonify({"copy_rate_mbps": float(rate) if rate not in (None, "") else mr.COPY_RATE_DEFAULT_MBPS,
                    "proxy_auto": mr.proxy_auto_enabled(), "edit_output": _me.output_mode(),
                    "nodes": [{"id": n["id"], "name": n.get("name"), "agent_version": n.get("agent_version"),
                               "direct": node_driver.transfer_capable(n)} for n in db_get_nodes()]})


@bp.route("/api/media/integrity")
@require_perm("media.manage")
def registry_integrity():
    """Rapport de TOUTE la base (noms, chemins, projets) : gestion des médias ET accès global."""
    err = _global_only()
    if err:
        return err
    return jsonify(mr.integrity())


@bp.route("/api/media/fields", methods=["GET", "POST"])
@require_login
def registry_fields():
    if request.method == "POST":
        from ..auth import has_perm
        if not has_perm("media.manage"):
            return jsonify({"error": "forbidden", "missing_permission": "media.manage"}), 403
        err = _global_only()                     # champs personnalisés : schéma commun à tous
        if err:
            return err
        b = request.get_json(silent=True) or {}
        try:
            mr.save_field(b.get("key"), b.get("label"), b.get("type"), b.get("options"), b.get("position") or 0)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"fields": mr.list_fields(), "types": list(mr.FIELD_TYPES)})


@bp.route("/api/media/fields/<key>", methods=["DELETE"])
@require_perm("media.manage")
def registry_field_delete(key):
    err = _global_only()
    if err:
        return err
    mr.delete_field(key)
    return jsonify({"ok": True})


@bp.route("/api/media/export")
@require_perm("media.manage")
def registry_export():
    err = _global_only()
    if err:
        return err
    data = mr.export_bytes()
    return send_file(io.BytesIO(data), mimetype="application/gzip", as_attachment=True,
                     download_name="db_medias-%s.tar.gz" % time.strftime("%Y%m%d-%H%M%S"))


@bp.route("/api/media/import_db", methods=["POST"])
@require_perm("media.manage")
def registry_import_db():
    """Archive d'export (fichier `file`), `mode` = merge (défaut) | replace. `replace` fait une
    sauvegarde de l'état courant avant."""
    err = _global_only()
    if err:
        return err
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "fichier requis"}), 400
    mode = request.form.get("mode") or "merge"
    if mode not in ("merge", "replace"):
        return jsonify({"ok": False, "error": "mode inconnu"}), 400
    try:
        res = mr.import_bytes(f.read(), mode=mode)
    except Exception as e:                                                 # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify(dict(res, ok=True))


@bp.route("/api/media/backups", methods=["GET", "POST"])
@require_perm("media.manage")
def registry_backups():
    err = _global_only()
    if err:
        return err
    if request.method == "POST":
        name = mr.backup()
        return jsonify({"ok": True, "name": name, "backups": mr.list_backups()})
    return jsonify({"backups": mr.list_backups()})


# ─── Proxys de lecture ──────────────────────────────────────────────────────────────────────
# Plafond d'une plage OUVERTE servie au navigateur (≤ mr.NODE_RANGE_CHUNK : une lecture chez l'agent).
# Chaque recherche du survol en lit autant (Waitress aspire la réponse avant que le navigateur ne
# coupe) : 512 Kio ≈ 4 s du proxy de 1 Mb/s, au moins un GOP. Mesuré 2026-10-12 sur le contrôleur
# (VM QEMU, AES sans PCLMUL) : ~10 ms de CPU par Mio lu en TLS — c'est le coût qui reste par recherche.
PROXY_RANGE_MAX = 512 * 1024
@bp.route("/api/media/assets/<aid>/proxy")
@require_login
def registry_proxy_stream(aid):
    """Le proxy du média, servi pour un lecteur HTML5 : `Range` respecté (206 + Content-Range
    exact, 416 hors fichier), lu sur son nœud par le port de transfert. ?node_id= : celui de ce
    nœud. Lecture seule : connecté suffit."""
    err = _hidden(aid)
    if err:
        return err
    pf = mr.proxy_file(aid, _int(request.args.get("node_id")))
    if not pf:
        return jsonify({"error": "aucun proxy présent"}), 404
    node = mr._node(pf["node_id"])
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    size = pf["size"]
    rng = mr.parse_range(request.headers.get("Range"), size)
    if rng is False:
        r = Response(status=416)
        r.headers["Content-Range"] = "bytes */%d" % size
        return r
    start, end = rng if rng else (0, size - 1)
    # Plage OUVERTE (`bytes=a-`, ce qu'envoie un <video> à chaque recherche) : servie BORNÉE à
    # PROXY_RANGE_MAX (206 + Content-Range exact, le lecteur redemande la suite). Sinon Waitress
    # aspire jusqu'à 16 Mo de la réponse avant que le navigateur, déjà reparti ailleurs, ne coupe ;
    # et la lecture chez l'agent, plage exacte lue jusqu'au bout, garde sa connexion (agent ≥ 0.27.0
    # seulement : un agent plus ancien garde le comportement d'avant).
    from .. import node_driver
    if rng and (request.headers.get("Range") or "").rstrip().endswith("-") and node_driver.transfer_range_capable(node):
        end = min(end, start + PROXY_RANGE_MAX - 1)
    length = end - start + 1
    gen = mr.read_node_range(node, pf["path"], size, start, length)
    try:
        first = next(gen, b"")                   # erreur d'ouverture : un vrai code d'erreur, pas un flux vide
    except Exception as e:                       # noqa: BLE001 — refus de l'agent, nœud injoignable
        return jsonify({"error": str(e)}), 502

    def body():
        yield first
        yield from gen
    r = Response(stream_with_context(body()), status=206 if rng else 200, mimetype=pf["mime"], direct_passthrough=True)
    r.headers["Content-Length"] = str(length)
    r.headers["Accept-Ranges"] = "bytes"
    r.headers["Cache-Control"] = "private, max-age=300"
    if rng:
        r.headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    return r


@bp.route("/api/media/proxies", methods=["GET", "POST"])
@require_login
def registry_proxies():
    """GET : générations en file / en cours / récentes (?asset=, ?node_id=). POST (gestion des
    médias) : {asset_ids:[…] | bin_id (+children), node_id?} → mises en file, une à la fois par nœud.
    Renvoie les tâches créées et, par nom, les refus (image fixe, en cours d'écriture…)."""
    scope = _scope()
    if request.method == "GET":
        aid = request.args.get("asset") or None
        if aid:
            err = _hidden(aid, scope)
            if err:
                return err
        js = mr.proxy_jobs(asset_id=aid, node_id=_int(request.args.get("node_id")))
        if scope is not None:
            ok = set(mr.visible_ids([j["asset_id"] for j in js], scope))
            js = [j for j in js if j["asset_id"] in ok]
        return jsonify({"jobs": js})
    from ..auth import current_user, has_perm
    if not has_perm("media.manage"):
        return jsonify({"error": "forbidden", "missing_permission": "media.manage"}), 403
    b = request.get_json(silent=True) or {}
    ids = [x for x in (b.get("asset_ids") or []) if isinstance(x, str)]
    if scope is not None and len(mr.visible_ids(ids, scope)) != len(ids):
        return jsonify({"ok": False, "error": "média inconnu"}), 404
    if scope is not None and not all(ms.asset_allows(scope, x, "edit") for x in ids):
        return _shared_denied()
    if _int(b.get("bin_id")) is not None:
        err = _bin_hidden(_int(b.get("bin_id")), scope)
        if err:
            return err
        try:
            ids += [a["id"] for a in mr.bin_assets(_int(b.get("bin_id")), children=b.get("children", True) is not False,
                                                   scope=scope)]
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    if not ids:
        return jsonify({"ok": False, "error": "asset_ids ou bin_id requis"}), 400
    u = current_user() or {}
    jobs, refused = [], {}
    for aid in dict.fromkeys(ids):
        if scope is not None and not ms.asset_allows(scope, aid, "edit"):   # contenu d'un bin partagé
            a = mr.db().execute("SELECT name FROM media_assets WHERE id=?", (aid,)).fetchone()
            refused[a["name"] if a else aid] = "partagé en lecture seule"
            continue
        try:
            jobs.append(mr.enqueue_proxy(aid, node_id=_int(b.get("node_id")), user=u.get("username") or ""))
        except (ValueError, RuntimeError) as e:
            a = mr.db().execute("SELECT name FROM media_assets WHERE id=?", (aid,)).fetchone()
            refused[a["name"] if a else aid] = str(e)
    for j in jobs:
        j.pop("tech", None)
    return jsonify({"ok": bool(jobs), "jobs": jobs, "refused": refused}), (200 if jobs or not refused else 409)


@bp.route("/api/media/proxies/<jid>/cancel", methods=["POST"])
@require_perm("media.manage")
def registry_proxy_cancel(jid):
    scope = _scope()
    if scope is not None:
        j = next((x for x in mr.proxy_jobs(limit=5000) if str(x["id"]) == str(jid)), None)
        if not j or not mr.asset_visible(j["asset_id"], scope):
            return jsonify({"ok": False, "error": "tâche inconnue"}), 404
        if not ms.asset_allows(scope, j["asset_id"], "owner"):
            return _shared_denied()
    try:
        j = mr.proxy_cancel(jid)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    j.pop("tech", None)
    j.pop("cancel", None)
    return jsonify({"ok": True, "job": j})


@bp.route("/api/media/assets/<aid>/proxy", methods=["DELETE"])
@require_perm("media.manage")
def registry_proxy_delete(aid):
    """{node_id} : supprime le proxy de ce nœud (fichier + ligne) ; génération en cours annulée."""
    err = _hidden(aid, need="owner")
    if err:
        return err
    b = request.get_json(silent=True) or {}
    nid = _int(b.get("node_id"))
    if nid is None:
        return jsonify({"ok": False, "error": "node_id requis"}), 400
    try:
        n = mr.delete_proxy(aid, nid)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "deleted": n})


# ─── Planches d'images (app/media_storyboard.py) ────────────────────────────────────────────
@bp.route("/api/media/assets/<aid>/storyboard.json")
@require_login
def registry_storyboard_json(aid):
    """Contrat de la bibliothèque : {n, cols, rows, w, h, times, url} ; 404 si pas de planche."""
    err = _hidden(aid)
    if err:
        return err
    from .. import media_storyboard as sbm
    d = sbm.get(aid)
    if not d:
        return jsonify({"error": "pas de planche"}), 404
    r = jsonify(d)
    r.headers["Cache-Control"] = "private, no-cache"
    return r


@bp.route("/api/media/assets/<aid>/storyboard.jpg")
@require_login
def registry_storyboard_jpg(aid):
    """La planche (JPEG, cols × rows cases). ETag (304 si inchangée) ; l'URL ne change pas quand la
    planche est refaite (proxy recréé) : gardée 5 min, puis revalidée."""
    err = _hidden(aid)
    if err:
        return err
    from .. import media_storyboard as sbm
    jp, js = sbm.paths(aid)
    if not (os.path.isfile(jp) and os.path.isfile(js)):
        return jsonify({"error": "pas de planche"}), 404
    r = send_file(jp, mimetype="image/jpeg", conditional=True, etag=True, max_age=300)
    r.headers["Cache-Control"] = "private, max-age=300"
    return r


@bp.route("/api/media/assets/<aid>/storyboard", methods=["POST"])
@require_perm("media.manage")
def registry_storyboard_make(aid):
    """Prépare la planche du média (job basse priorité de la ferme, tiré de son proxy)."""
    err = _hidden(aid, need="edit")
    if err:
        return err
    from .. import media_storyboard as sbm
    from ..auth import current_user
    try:
        j = sbm.enqueue(aid, user=(current_user() or {}).get("username") or "")
    except (ValueError, RuntimeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True, "job": j})


@bp.route("/api/media/storyboards", methods=["GET", "POST"])
@require_login
def registry_storyboards():
    """GET : avancement (médias à proxy vidéo, avec planche, manquantes, en file / en cours / en
    attente d'un transcoder, en échec). POST (gestion des médias) : « Préparer les planches
    manquantes » — un job basse priorité par média concerné."""
    from .. import media_storyboard as sbm
    err = _global_only()                         # compteurs et rattrapage de TOUTE la base
    if err:
        return err
    if request.method == "POST":
        from ..auth import current_user, has_perm
        if not has_perm("media.manage"):
            return jsonify({"error": "forbidden", "missing_permission": "media.manage"}), 403
        res = sbm.catch_up(user=(current_user() or {}).get("username") or "")
        return jsonify(dict(res, ok=True, status=sbm.status()))
    return jsonify(sbm.status())


from . import media_edit_api  # noqa: E402,F401 — éditeur d'image (routes /api/media/edit/*)
from . import media_sharing_api  # noqa: E402,F401 — partages et groupes d'utilisateurs (/api/media/shares/*, /api/user-groups)
