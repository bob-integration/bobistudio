# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Ferme de transcodage : file centrale, membres, parcours des sources (cf. app/transcode_farm.py).

Lire et soumettre : `media.manage` (comme le transcodage depuis le gestionnaire de médias), dans
les projets de l'utilisateur. Inscrire un transcoder : `containers.deploy` — c'est décider où
tourne du travail, pas seulement en demander.
"""
import json
import os
import shlex

from flask import jsonify, request

from . import bp
from .. import transcode_farm as farm
from ..auth import require_perm, current_user, scoped_project_ids, has_perm
from ..database import db_get_projects, db_get_node, db_get_nodes


def _scope():
    return scoped_project_ids()


def _check_project(pid):
    s = _scope()
    if s is not None and pid not in s:
        return jsonify({"error": "projet hors de vos accès"}), 403
    return None


def _shared_scope():
    """Portée étendue aux médias partagés : media_sharing.user_scope(), la seule fonction de portée."""
    from .. import media_sharing as ms
    return ms.user_scope()


def _job_or_403(job_id):
    job = farm.get_job(job_id)
    if not job:
        return None, (jsonify({"error": "job introuvable"}), 404)
    s = _scope()
    if s is not None and job["project_id"] not in s:
        # Job demandé SUR UN MÉDIA que l'appelant ne voit que par un partage : il reste le sien.
        from .. import media_registry as mr
        if not (job.get("src_asset") and mr.asset_visible(job["src_asset"], _shared_scope())):
            return None, (jsonify({"error": "job introuvable"}), 404)
    return job, None


def _int_or_none(v):
    if v in (None, "", "null"):
        return None
    return int(v)


@bp.route("/api/transcode-farm/state", methods=["GET"])
@require_perm("media.manage")
def farm_state():
    farm.ensure_tables()
    s = _scope()
    projects = [{"id": p["id"], "name": p["name"]} for p in db_get_projects() or []
                if s is None or p["id"] in s]
    members = [m for m in farm.members_view() if s is None or m["project_id"] in s]
    return jsonify({"members": members, "projects": projects, "global": s is None,
                    "can_manage_members": has_perm("containers.deploy"),
                    "priorities": farm.PRIORITIES})


@bp.route("/api/transcode-farm/jobs", methods=["GET"])
@require_perm("media.manage")
def farm_jobs():
    farm.ensure_tables()
    a = request.args
    try:
        limit = max(1, min(500, int(a.get("limit") or 200)))
        offset = max(0, int(a.get("offset") or 0))
    except ValueError:
        return jsonify({"error": "limit/offset invalides"}), 400
    s = _scope()
    extra = []
    if s is not None:
        from .. import media_registry as mr
        extra = mr.visible_ids(farm.asset_job_assets(), _shared_scope())
    return jsonify(farm.list_jobs(sorted(s) if s is not None else None, a.get("status") or None,
                                  (a.get("q") or "").strip() or None, limit, offset, extra_assets=extra))


@bp.route("/api/transcode-farm/jobs/<int:job_id>", methods=["GET"])
@require_perm("media.manage")
def farm_job(job_id):
    _, err = _job_or_403(job_id)
    if err:
        return err
    return jsonify(farm.job_detail(job_id))


@bp.route("/api/transcode-farm/jobs", methods=["POST"])
@require_perm("media.manage")
def farm_submit():
    """Un job (`source`), ou un job PAR source (`sources` : sélection multiple, contenu d'un bin), mêmes
    réglages pour tous. En lot, une source refusée n'empêche pas les autres : `ids` + `errors`
    (index dans `sources`, message) — la fenêtre dit lesquelles n'ont pas été lancées."""
    farm.ensure_tables()
    d = request.json or {}
    refused = _bin_refusal(d.get("bin_id"))
    if refused:
        return jsonify({"error": refused[0]}), refused[1]
    if isinstance(d.get("sources"), list):
        if not d["sources"]:
            return jsonify({"error": "aucune source"}), 400
        if len(d["sources"]) > MAX_BATCH:
            return jsonify({"error": "%d sources au plus par envoi" % MAX_BATCH}), 400
        ids, errors = [], []
        for i, src in enumerate(d["sources"]):
            jid, err = _submit_one(d, src or {})
            if err:
                errors.append({"index": i, "error": err[0], "reason": err[1]})
            else:
                ids.append(jid)
        return jsonify({"ok": bool(ids), "ids": ids, "errors": errors}), (200 if ids else 400)
    jid, err = _submit_one(d, d.get("source") or {})
    if err:
        body = {"error": err[0]}
        if err[1]:
            body.update(ok=False, reason=err[1])
        return jsonify(body), err[2]
    return jsonify({"ok": True, "id": jid})


MAX_BATCH = 500


def _bin_refusal(bin_id):
    """Bin de destination (fenêtre « Transcoder… ») : visible de l'appelant, modifiable par lui (un bin
    partagé en « Modifier »), et MANUEL. → None, ou (message, code HTTP)."""
    if bin_id in (None, "", 0):
        return None
    from .. import media_registry as mr, media_sharing as ms
    try:
        bid = int(bin_id)
    except (TypeError, ValueError):
        return "bin invalide", 400
    scope = ms.user_scope()
    if scope is not None and (not mr.bin_visible(bid, scope) or not ms.bin_allows(scope, bid, "edit")):
        return "bin inconnu ou en lecture seule", 403
    try:
        farm.check_bin(bid)
    except ValueError as e:
        return str(e), 400
    return None


def _submit_one(d, src):
    """→ (id, None) ou (None, (message, raison, code HTTP))."""
    if src.get("kind") == "asset":
        return _submit_asset(d, src)
    try:
        pid = _int_or_none(d.get("project_id"))
    except (TypeError, ValueError):
        return None, ("projet invalide", None, 400)
    if _check_project(pid):
        return None, ("projet hors de vos accès", None, 403)
    try:
        return farm.submit(pid, src, (d.get("type") or "").strip(), d.get("params") or {},
                           d.get("priority"), d.get("output_dir") or "", current_user(), bin_id=d.get("bin_id") or None), None
    except ValueError as e:
        return None, (str(e), None, 400)


def _asset_refusal(aid, need="edit"):
    """Droit d'agir sur un média de la base → None, ou (message, raison, code HTTP)."""
    from .. import media_sharing as ms
    scope = ms.user_scope()
    if scope is not None and not ms.asset_allows(scope, aid, "use"):
        return "média inconnu", None, 404
    if need == "edit" and scope is not None and not ms.asset_allows(scope, aid, "edit"):
        return "forbidden", "shared_read_only", 403
    return None


def _submit_asset(d, src):
    """Job SUR UN MÉDIA de la base. Portée : media_sharing.user_scope() (projets ET partages). Le résultat est
    rangé par le scan comme une VERSION du média (cf. docs/reference/MEDIAS_VERSIONS.md) : toucher la fiche,
    donc « Modifier » pour un média seulement partagé ; « Consulter et utiliser » suffit pour lire."""
    aid = str(src.get("asset_id") or "")
    refused = _asset_refusal(aid)
    if refused:
        return None, refused
    try:
        return farm.submit_asset(aid, (d.get("type") or "").strip(), d.get("params") or {}, d.get("priority"),
                                 d.get("output_dir") or "", current_user(), version=src.get("version"),
                                 bin_id=d.get("bin_id") or None), None
    except ValueError as e:
        return None, (str(e), None, 400)


@bp.route("/api/transcode-farm/preview", methods=["POST"])
@require_perm("media.manage")
def farm_preview():
    """Nom qu'aura chaque sortie, AVANT de lancer (fenêtre « Transcoder… ») : `sources` comme pour le lot,
    `version` et `ext` du préréglage choisi (publiés par /presets), `output_dir`. Une entrée par source, dans
    l'ordre ; une source illisible porte `error` au lieu du nom."""
    d = request.json or {}
    srcs = d.get("sources") or []
    if not isinstance(srcs, list) or len(srcs) > MAX_BATCH:
        return jsonify({"error": "sources invalides"}), 400
    try:
        pid = _int_or_none(d.get("project_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "projet invalide"}), 400
    nodes = {n["id"]: n.get("name") for n in db_get_nodes() or []}
    out = []
    for src in srcs:
        src = src or {}
        try:
            if src.get("kind") == "asset":
                refused = _asset_refusal(str(src.get("asset_id") or ""), need="use")
                if refused:
                    raise ValueError(refused[0])
            elif _check_project(pid):
                raise ValueError("projet hors de vos accès")
            r = farm.preview_output(src, str(d.get("version") or ""), d.get("ext") or "", d.get("output_dir") or "",
                                    mode="asset" if d.get("as") == "asset" else "version")
            r["node"] = nodes.get(r.get("node_id"))
            out.append(r)
        except ValueError as e:
            out.append({"error": str(e)})
    return jsonify({"items": out, "code_placeholder": farm.CODE_PLACEHOLDER})


def _action(job_id, fn, *args):
    _, err = _job_or_403(job_id)
    if err:
        return err
    try:
        r = fn(job_id, *args)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"ok": True, "result": r})


@bp.route("/api/transcode-farm/jobs/<int:job_id>/cancel", methods=["POST"])
@require_perm("media.manage")
def farm_cancel(job_id):
    return _action(job_id, farm.cancel)


@bp.route("/api/transcode-farm/jobs/<int:job_id>/retry", methods=["POST"])
@require_perm("media.manage")
def farm_retry(job_id):
    return _action(job_id, farm.retry)


@bp.route("/api/transcode-farm/jobs/<int:job_id>/priority", methods=["POST"])
@require_perm("media.manage")
def farm_priority(job_id):
    return _action(job_id, farm.set_priority, (request.json or {}).get("priority"))


@bp.route("/api/transcode-farm/purge", methods=["POST"])
@require_perm("media.manage")
def farm_purge():
    s = _scope()
    return jsonify({"ok": True, "removed": farm.purge(sorted(s) if s is not None else None)})


@bp.route("/api/transcode-farm/members/<int:vmid>", methods=["POST"])
@require_perm("containers.deploy")
def farm_member(vmid):
    farm.ensure_tables()
    try:
        farm.set_member(vmid, bool((request.json or {}).get("enabled")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@bp.route("/api/transcode-farm/presets", methods=["GET"])
@require_perm("media.manage")
def farm_presets():
    """Les presets viennent d'un transcoder (source unique : son script), pas d'une copie ici."""
    ts = sorted(farm.transcoders(), key=lambda t: (not t["member"], t["status"] != "running"))
    for t in ts:
        if t["status"] != "running":
            continue
        try:
            code, r = farm._call(t, "GET", "/presets", timeout=4)
            if code == 200:
                return jsonify(r)
        except Exception:
            continue
    return jsonify({"error": "aucun transcoder joignable pour lire les presets"}), 503


@bp.route("/api/transcode-farm/sources", methods=["GET"])
@require_perm("media.manage")
def farm_sources():
    """Où l'on peut choisir une source pour `project_id` : dossiers réseau accordés, nœuds média."""
    try:
        pid = _int_or_none(request.args.get("project_id"))
    except ValueError:
        return jsonify({"error": "projet invalide"}), 400
    err = _check_project(pid)
    if err:
        return err
    shares = []
    try:
        from services import storage
        for g in storage.visible_folders(current_user(), pid):
            if storage.container_entry(g["id"], pid):
                shares.append({"id": g["id"], "name": g["name"], "mounted": g["mounted"]})
        nodes = storage._media_nodes()
    except ImportError:
        nodes = [n for n in db_get_nodes() or [] if n.get("media_mount")]
    return jsonify({"shares": shares,
                    "nodes": [{"id": n["id"], "name": n.get("name")} for n in nodes]})


_LIST_PY = r'''
import json, os, sys
base, rel = sys.argv[1], sys.argv[2]
rb = os.path.realpath(base)
t = os.path.realpath(os.path.join(base, rel))
if t != rb and not t.startswith(rb + os.sep):
    print(json.dumps({"error": "hors racine"})); sys.exit(0)
if not os.path.isdir(t):
    print(json.dumps({"error": "dossier absent"})); sys.exit(0)
items = []
for n in sorted(os.listdir(t)):
    if n.startswith("."):
        continue
    p = os.path.join(t, n)
    try:
        st = os.stat(p)
    except OSError:
        continue
    items.append({"name": n, "type": "dir" if os.path.isdir(p) else "file",
                  "size": st.st_size, "mtime": int(st.st_mtime)})
print(json.dumps({"items": items}))
'''


def _list_local_dir(full):
    items = []
    for n in sorted(os.listdir(full)):
        if n.startswith("."):
            continue
        p = os.path.join(full, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        items.append({"name": n, "type": "dir" if os.path.isdir(p) else "file",
                      "size": st.st_size, "mtime": int(st.st_mtime)})
    return items


@bp.route("/api/transcode-farm/browse", methods=["GET"])
@require_perm("media.manage")
def farm_browse():
    """Contenu d'un dossier (source à choisir, ou destination : `dirs=1` ne rend que les dossiers).
    `asset_id` : le volume du nœud qui porte ce média de la base (destination d'un job sur un média,
    hors projet) — ouvert à qui peut le modifier, comme la soumission elle-même."""
    a = request.args
    try:
        pid = _int_or_none(a.get("project_id"))
    except ValueError:
        return jsonify({"error": "projet invalide"}), 400
    node_override = None
    if a.get("asset_id"):
        aid = str(a.get("asset_id"))
        refused = _asset_refusal(aid)
        if refused:
            return jsonify({"error": refused[0]}), refused[2]
        try:
            node_override = farm.asset_source(aid)[0]
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        pid = None
    else:
        err = _check_project(pid)
        if err:
            return err
    rel = (a.get("path") or "").strip().strip("/")
    if any(p.startswith(".") for p in rel.split("/") if p):
        return jsonify({"error": "chemin invalide"}), 400
    if a.get("kind") == "share" and node_override is None:
        from services import storage
        try:
            _, full = storage.folder_path(current_user(), int(a.get("grant_id") or 0), rel, pid)
        except PermissionError:
            return jsonify({"error": "dossier inconnu ou accès refusé"}), 403
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if not os.path.isdir(full):
            return jsonify({"error": "pas un dossier (partage monté ?)"}), 400
        items = _list_local_dir(full)
    else:
        from .. import node_driver
        from ..docker_compute import project_media_dir
        try:
            node = db_get_node(int(node_override if node_override is not None else a.get("node_id") or 0))
        except ValueError:
            node = None
        if not node:
            return jsonify({"error": "nœud inconnu"}), 404
        base = project_media_dir(node, pid)
        try:
            rc, out, e = node_driver.host_exec(
                node, "python3 -c %s %s %s" % (shlex.quote(_LIST_PY), shlex.quote(base), shlex.quote(rel)),
                timeout=20)
            r = json.loads((out or "").strip().splitlines()[-1]) if rc == 0 else {"error": (e or out or "")[-200:]}
        except Exception as ex:
            return jsonify({"error": "nœud injoignable : %s" % ex}), 502
        if r.get("error"):
            return jsonify({"error": r["error"]}), 400
        items = r["items"]
    if a.get("dirs") in ("1", "true"):
        items = [x for x in items if x["type"] == "dir"]
    items.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))
    return jsonify({"path": rel, "items": items})
