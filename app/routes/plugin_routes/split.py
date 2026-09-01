# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Split / SuperSource — persistance + mémoires (état/contrôle live via le proxy
plugin générique /api/containers/<vmid>/plugin/{state,input,box,recall})."""

from flask import jsonify, request

from .. import bp
from ..shared import _load_dc, _mixer_proxy
from ...auth import require_perm, require_login


@bp.route("/api/containers/<int:vmid>/split/persist", methods=["POST"])
@require_perm("containers.deploy")
def split_persist(vmid):
    """Persiste la disposition + câblage live dans deploy_config, SANS redeploy
    (pas de /stop+/start). Lit l'état :8082/state et met à jour box_config + bg_input."""
    from ...addressing import get_container_ip
    from ...database import db_get_container, db_update_deploy_config
    import requests as _req
    ip = get_container_ip(vmid)
    if not ip:
        return jsonify({"error": "IP container introuvable"}), 404
    try:
        r = _req.get(f"http://{ip}:8082/state", timeout=2)
        st = r.json()
    except Exception as e:
        return jsonify({"error": f"état Split injoignable : {e}"}), 502
    from ... import plugins as _split_pl
    c = db_get_container(vmid)
    dc = _load_dc(c) if c else None
    if not dc:
        return jsonify({"error": "container introuvable ou sans script"}), 404
    t = dc.get("type"); params = dict(dc.get("params") or {})
    sync_h = _split_pl.get_hook(t, "sync_state")
    if not sync_h:
        return jsonify({"error": f"sync_state non supporté pour le type {t}"}), 400
    new_params = sync_h(st, params, {"vmid": vmid, "type": t})
    db_update_deploy_config(vmid, t, new_params)
    return jsonify({"ok": True})


@bp.route("/api/containers/<int:vmid>/split/metrics", methods=["GET"])
@require_login
def split_metrics(vmid):
    """Métriques :8080 du moteur (bloc `adv` 0.9.0 : budget_pct / over_budget / rotated_boxes /
    stamp_hit_rate). Lecture seule → l'UI affiche le coût de composition EN PERMANENCE : un
    dépassement de budget (rotation) ne doit jamais se manifester par un simple décrochage fps
    inexpliqué."""
    from ...addressing import get_container_ip
    import requests as _req
    ip = get_container_ip(vmid)
    if not ip:
        return jsonify({"error": "IP container introuvable"}), 404
    try:
        r = _req.get(f"http://{ip}:8080/", timeout=2)
        return (r.text, r.status_code, {"Content-Type": "application/json"})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/api/containers/<int:vmid>/split/memories", methods=["GET"])
@require_login
def split_memories_list_route(vmid):
    # Stockage générique (type='split', scope=vmid) ; remap value→config pour le front Split.
    from ...database import plugin_store_list
    return jsonify([{"id": m["id"], "name": m["name"], "config": m["value"],
                     "created_at": m["created_at"]}
                    for m in plugin_store_list("split", str(vmid))])


@bp.route("/api/containers/<int:vmid>/split/memories", methods=["POST"])
@require_perm("containers.deploy")
def split_memory_create_route(vmid):
    """Crée une mémoire. Body: {name, config?}. Si `config` (liste de boxes) est
    fourni → import / envoi depuis un autre Split ; sinon snapshot de l'état live."""
    from ...addressing import get_container_ip
    from ...database import plugin_store_create
    import requests as _req
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "nom requis"}), 400
    # ★ FORMAT D'UNE MÉMOIRE (0.12.0) : une mémoire ne peut plus être la seule liste des box —
    # elle porte AUSSI le canal GLOBAL (le calque de transformation du groupe) et le FOND (mode +
    # couleur). Sans eux, rappeler « groupe hors champ » ou « fond bleu » ne restituerait rien.
    #   ancien format : [box, box, box, box]                      (toujours accepté, en lecture)
    #   nouveau        : {"boxes": [...], "global": {...}, "bg": {...}}
    config = data.get("config")
    if isinstance(config, list):
        value = config[:4]                       # import d'une mémoire ANCIENNE (rétro-compat)
    elif isinstance(config, dict) and isinstance(config.get("boxes"), list):
        value = {"boxes": config["boxes"][:4],
                 "global": config.get("global") or {},
                 "bg": config.get("bg") or {}}   # import / copie vers un autre Split
    else:
        ip = get_container_ip(vmid)
        if not ip:
            return jsonify({"error": "IP container introuvable"}), 404
        try:
            st = _req.get(f"http://{ip}:8082/state", timeout=2).json()
        except Exception as e:
            return jsonify({"error": f"état Split injoignable : {e}"}), 502
        _bg = st.get("bg") or {}
        # le fond est publié avec sa conversion YUV : on ne persiste QUE le réglage (mode/couleur/
        # plage + DÉGRADÉ en hex) — jamais des valeurs YUV figées à une profondeur. Le dégradé fait
        # partie de la mémoire (sans lui, rappeler « wipe bleu→rouge » ne restituerait rien).
        _bgv = {k: _bg.get(k) for k in ("mode", "color", "legal") if _bg.get(k) is not None}
        if isinstance(_bg.get("gradient"), dict):
            _bgv["gradient"] = _bg["gradient"]
        value = {"boxes": st.get("boxes") or [],
                 "global": st.get("global") or {},
                 "bg": _bgv}
    mem_id = plugin_store_create("split", str(vmid), name, value)
    return jsonify({"ok": True, "id": mem_id})


@bp.route("/api/containers/<int:vmid>/split/memories/<int:mem_id>", methods=["DELETE"])
@require_perm("containers.deploy")
def split_memory_delete_route(vmid, mem_id):
    from ...database import plugin_store_delete
    return jsonify({"ok": plugin_store_delete(mem_id)})


@bp.route("/api/containers/<int:vmid>/split/memories/<int:mem_id>/recall", methods=["POST"])
@require_perm("containers.deploy")
def split_memory_recall_route(vmid, mem_id):
    """Rappelle une mémoire avec transition. Body: {duration_ms, curve?, stagger_ms?}.

    `curve` / `stagger_ms` sont OPTIONNELS : omis, le moteur applique ses défauts (POST
    /transition). Fournis, ils surchargent le rappel (courbe d'accélération 0.9.0)."""
    from ...database import plugin_store_get
    mem = plugin_store_get(mem_id)
    if not mem or mem.get("scope") != str(vmid):
        return jsonify({"error": "mémoire introuvable"}), 404
    data = request.json or {}
    try:
        duration = max(0, int(data.get("duration_ms") or 0))
    except Exception:
        duration = 0
    # Mémoire ANCIENNE (liste de box) ou NOUVELLE (dict boxes/global/bg) — les deux se rappellent.
    val = mem["value"]
    if isinstance(val, dict):
        body = {"boxes": val.get("boxes") or [], "duration_ms": duration}
        if isinstance(val.get("global"), dict) and val["global"]:
            body["global"] = val["global"]
        if isinstance(val.get("bg"), dict) and val["bg"]:
            body["bg"] = val["bg"]
    else:
        body = {"boxes": val, "duration_ms": duration}
    if data.get("curve"):
        body["curve"] = str(data["curve"])
    if data.get("stagger_ms") is not None:
        try:
            body["stagger_ms"] = float(data["stagger_ms"])
        except Exception:
            pass
    return _mixer_proxy(vmid, "/recall", body=body)
