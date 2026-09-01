# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Chantier 6 : catalogue d'actions/états, macros (CRUD + run + journal), variables de
projet, exécution d'action unitaire (shotbox) et lecture d'états en lot (feedback).
Droits : editor écrit les macros, operator exécute (décision 2026-07-06)."""

import json

from flask import jsonify, request

from . import bp
from .shared import _load_dc
from ..auth import (require_project_role, require_global_access, current_user,
                    check_vmid_access, project_role_for, project_role_at_least,
                    require_login, has_perm)
from ..database import (db_get_containers, db_project_macros, db_get_macro,
                        db_create_macro, db_update_macro, db_delete_macro,
                        db_project_vars, db_set_project_var, db_system_macros,
                        db_macros_published_to, db_get_project,
                        db_project_triggers, db_get_trigger, db_create_trigger,
                        db_update_trigger, db_delete_trigger)
from ..auth import vmid_project_ids
from .. import macros as engine
from .. import plugins


# ─── Catalogue actions/états (partagé projet / système) ───────

def _with_recall_options(vmid, type_, actions):
    """Options des params `options_from: "recall"` (action « rappeler une mémoire ») :
    résolues côté ORCHESTRATEUR depuis control.recall du manifeste (plugin_store de
    l'instance ou layouts globaux) — le front les reçoit comme des options statiques."""
    from . import recall_presets   # défini dans routes/__init__ (partagé avec Ember+)
    try:
        _rc, presets = recall_presets(vmid, type_)
    except Exception:
        presets = []
    opts = [{"value": pr.get("id"), "label": pr.get("name") or f"#{pr.get('id')}"}
            for pr in presets if pr.get("id") is not None]
    out = []
    for a in actions:
        if any(p.get("options_from") == "recall" for p in (a.get("params") or [])):
            a = json.loads(json.dumps(a))   # copie : ne pas muter le manifeste du registre
            for p in a["params"]:
                if p.get("options_from") == "recall":
                    p["options"] = opts
        out.append(a)
    return out


# ─── Contrôlables GÉNÉRIQUES (8e passe ch.6) : rien n'est figé, on DÉCOUVRE ──
#
# En plus des actions/états curatés, le catalogue expose :
#  - les champs de config_schema (→ « Réglages », écrits via le chemin plugin_config) ;
#  - les control.endpoints non couverts par une action curatée (→ « action avancée »
#    générique, advanced:true, POST via le proxy/moteur — toujours whitelistés) ;
#  - un flag `discoverable` (control.read_endpoints présents) → le front peut appeler
#    /api/containers/<vmid>/discover_states pour les états découverts sur l'instance.

def _config_fields(m, params, with_values):
    """Champs du config_schema → contrôlables « réglage ». `with_values` (permission
    plugins.operate) inclut la valeur courante du deploy_config (lecture)."""
    out = []
    for f in (m.get("config_schema") or []):
        k = f.get("key")
        if not k:
            continue
        e = {"key": k, "label": f.get("label") or k, "type": f.get("type") or "text",
             "scope": f.get("scope") or "system", "source": "config"}
        for extra in ("options", "min", "max", "step", "default", "placeholder"):
            if f.get(extra) is not None:
                e[extra] = f[extra]
        if with_values and k in (params or {}):
            e["value"] = params[k]
        out.append(e)
    return out


def _advanced_endpoints(m):
    """control.endpoints whitelistés (normalisés — liste ou dict 2110_io) non couverts
    par une action curatée (et hors read_endpoints, qui sont des lectures)
    → « actions avancées » génériques."""
    covered = {a.get("endpoint") for a in (m.get("actions") or []) if a.get("endpoint")}
    read = set((m.get("control") or {}).get("read_endpoints") or [])
    return [{"endpoint": e["path"], "label": e["desc"] or e["path"], "port": e["port"],
             "advanced": True, "source": "endpoint"}
            for e in plugins.control_post_endpoints(m)
            if e["path"] not in covered and e["path"] not in read]


def _catalog_containers(pids=None):
    """Containers × catalogues `actions`/`state` (manifestes plugins) + ENTRÉES
    câblées (options des params `options_from: "inputs"` — l'opérateur choisit une
    source nommée, pas un index) + contrôlables GÉNÉRIQUES (config/advanced/discoverable).
    `pids=None` = TOUS les containers plugins
    (catalogue global des macros système) ; sinon filtre au(x) projet(s)."""
    containers = db_get_containers()
    # shm → « hostname · label » : pour nommer les sources câblées sur les entrées.
    shm_label = {}
    for c in containers:
        dc = _load_dc(c) or {}
        t = dc.get("type")
        if not plugins.is_plugin(t):
            continue
        p = dc.get("params") or {}
        hn = p.get("hostname") or c.get("hostname") or ""
        try:
            for pr in plugins.derive_wiring(t, hn, p)["produces"]:
                if pr.get("shm"):
                    lbl = (pr.get("label") or "").strip()
                    shm_label[pr["shm"]] = hn + (" · " + lbl if lbl else "")
        except Exception:
            continue
    out = []
    with_values = has_perm("plugins.operate")   # valeurs courantes des réglages
    for c in containers:
        if pids is not None and not (set(pids) & vmid_project_ids(c["vmid"])):
            continue
        dc = _load_dc(c) or {}
        t = dc.get("type")
        m = plugins.get(t) or {}
        actions, state = m.get("actions") or [], m.get("state") or []
        config = _config_fields(m, dc.get("params") or {}, with_values)
        advanced = _advanced_endpoints(m)
        has_param_tree = bool(m.get("param_tree"))
        if not (actions or state or config or advanced or has_param_tree):
            continue
        if any(p.get("options_from") == "recall"
               for a in actions for p in (a.get("params") or [])):
            actions = _with_recall_options(c["vmid"], t, actions)
        # `source` sur chaque entrée (curated|config|endpoint|discovered) — copies
        # superficielles pour ne pas muter les manifestes du registre.
        actions = [dict(a, source="curated") for a in actions]
        state = [dict(s, source="curated") for s in state]
        # Entrées de l'instance : slot + libellé de la source câblée dessus.
        inputs = []
        p = dc.get("params") or {}
        hn = p.get("hostname") or c.get("hostname") or ""
        try:
            for cons in plugins.derive_wiring(t, hn, p)["consumes"]:
                if (cons.get("essence") or "video") != "video":
                    continue
                shm = cons.get("shm") or p.get(cons.get("state_field") or "") or ""
                inputs.append({"value": cons.get("slot", len(inputs)),
                               "label": (cons.get("label") or f"Entrée {len(inputs)+1}")
                                        + " — " + (shm_label.get(shm) or shm or "non câblée")})
        except Exception:
            inputs = []
        out.append({"vmid": c["vmid"], "instance_uuid": c.get("instance_uuid"),
                    "hostname": c.get("hostname"), "type": t,
                    "actions": actions, "state": state, "inputs": inputs,
                    "config": config, "advanced": advanced,
                    "has_param_tree": has_param_tree,
                    "discoverable": bool((m.get("control") or {}).get("read_endpoints"))})
    return out


# ─── États découverts sur l'instance live (8e passe ch.6) ─────
# Interroge les control.read_endpoints (même chemin réseau que fetch_state), aplatit le
# JSON en chemins pointés, cache 15 s côté moteur. Lecture pure → login + viewer.

@bp.route("/api/containers/<int:vmid>/discover_states", methods=["GET"])
@require_login
def container_discover_states(vmid):
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    return jsonify(engine.discover_states(vmid))


@bp.route("/api/containers/<int:vmid>/param_tree", methods=["GET"])
@require_login
def container_param_tree(vmid):
    """Arbre de paramètres résolu (structure manifeste × caps live) : élément (box/global)
    → groupe → paramètre borné. Lecture pure → login + viewer."""
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    return jsonify(engine.param_tree(vmid))


@bp.route("/api/projects/<int:pid>/action_catalog", methods=["GET"])
@require_project_role("viewer")
def project_action_catalog(pid):
    """Matière première shotbox/macros du projet. La liste `macros` inclut les
    macros SYSTÈME publiées vers ce projet (marquées system:true, opaques)."""
    macros = [{"id": m["id"], "name": m["name"]} for m in db_project_macros(pid)]
    macros += [{"id": m["id"], "name": m["name"], "system": True}
               for m in db_macros_published_to(pid)]
    from .. import core_plugins
    return jsonify({"containers": _catalog_containers(pids={pid}), "macros": macros,
                    "services": core_plugins.service_actions(availability="project", pid=pid)})


# ─── Exécution d'action unitaire (boutons de shotbox) ─────────

@bp.route("/api/projects/<int:pid>/actions/run", methods=["POST"])
@require_project_role("operator")
def project_action_run(pid):
    data = request.json or {}
    # Action de SERVICE (tsl.set_label…) : availability project + bornage aux
    # ressources du projet gérés par core_plugins + le run_action du service.
    if data.get("service"):
        try:
            engine.exec_service_action(data["service"], data.get("action_id"),
                                       data.get("params") or {}, pid=pid,
                                       user=(current_user() or {}).get("username"),
                                       variables=db_project_vars(pid))
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"status": "ok"})
    vmid = engine._resolve_vmid(data)
    if not vmid or pid not in vmid_project_ids(vmid):
        return jsonify({"error": "cible hors projet"}), 403
    kind = data.get("kind") or "action"
    try:
        if kind == "config":
            # Réglage (config_schema) : même chemin d'écriture que plugin_config —
            # scope system gaté par containers.deploy (droits inchangés).
            engine.exec_config(vmid, data.get("params") or {}, db_project_vars(pid),
                               allow_system=has_perm("containers.deploy"))
        elif kind == "post":
            # Action avancée : POST libre sur un endpoint whitelisté (control.endpoints).
            engine.exec_post(vmid, data.get("endpoint"), data.get("params") or {},
                             db_project_vars(pid))
        else:
            engine.exec_action(vmid, data.get("action_id"), data.get("params") or {},
                               db_project_vars(pid))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok"})


# ─── Lecture d'états en lot (feedback shotbox, ~1 requête/s par vue) ──

@bp.route("/api/projects/<int:pid>/states", methods=["POST"])
@require_project_role("viewer")
def project_states(pid):
    queries = (request.json or {}).get("queries") or []
    out = []
    for q in queries[:32]:
        vmid = engine._resolve_vmid(q)
        val = None
        if vmid and pid in vmid_project_ids(vmid):
            # state_id (curaté) OU endpoint+path (état découvert, borné aux read_endpoints).
            val = engine.fetch_state(vmid, q.get("state_id"),
                                     endpoint=q.get("endpoint"), path=q.get("path"))
        out.append({"instance_uuid": q.get("instance_uuid"), "vmid": q.get("vmid"),
                    "state_id": q.get("state_id"), "endpoint": q.get("endpoint"),
                    "path": q.get("path"), "value": val})
    return jsonify({"states": out})


# ─── Variables de projet ──────────────────────────────────────

@bp.route("/api/projects/<int:pid>/vars", methods=["GET"])
@require_project_role("viewer")
def project_vars_get(pid):
    return jsonify({"vars": db_project_vars(pid)})

@bp.route("/api/projects/<int:pid>/vars", methods=["POST"])
@require_project_role("operator")
def project_vars_set(pid):
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name requis"}), 400
    db_set_project_var(pid, name, data.get("value"))
    return jsonify({"status": "ok", "vars": db_project_vars(pid)})


# ─── Macros : CRUD (editor) + run/journal (operator) ──────────
#
# Deux formats de graph cohabitent (9e passe ch.6) : blocks/v1 (éditeur à blocs) et
# nodes/v2 (éditeur Scénario nodal). L'API expose `format` + `structured` (un graphe
# nodal NON structuré ne s'ouvre qu'en vue nodale — badge « avancé ») et compile à la
# volée blocks→graph via `?as=graph` sur le GET d'une macro.

def _graph_error(graph):
    """Valide un graph soumis (les deux formats). Renvoie un message ou None."""
    if graph is None:
        return None
    if not isinstance(graph, dict):
        return "graph invalide"
    if graph.get("format") == engine.GRAPH_FORMAT:
        errs = engine.validate_graph(graph)
        return ("graphe invalide : " + " ; ".join(errs)) if errs else None
    if not isinstance(graph.get("steps", []), list):
        return "steps invalide"
    return None


def _macro_meta(m):
    """Champs communs listes/GET : format + structured + nb d'étapes (ou de nœuds)."""
    g = m.get("graph") or {}
    if engine.macro_format(m) == engine.GRAPH_FORMAT:
        count = len([n for n in (g.get("nodes") or []) if n.get("type") != "entry"])
    else:
        count = len(g.get("steps") or [])
    return {"format": engine.macro_format(m), "structured": engine.macro_structured(m),
            "steps": count}


def _macro_payload(m):
    """Macro complète pour le GET unitaire ; `?as=graph` ajoute `nodal` = la
    compilation à la volée blocks→nodes/v2 (vue nodale d'une macro blocs) ;
    `?as=blocks` (symétrique, 10e passe) ajoute `blocks` = la forme blocs d'une
    macro nodes/v2 STRUCTURÉE (ouverture en vue Blocs sans réimplémenter
    graph_to_blocks côté client) — absent si le graphe n'est pas structuré."""
    out = dict(m)
    out.update(_macro_meta(m))
    if request.args.get("as") == "graph" and engine.macro_format(m) != engine.GRAPH_FORMAT:
        out["nodal"] = engine.blocks_to_graph((m.get("graph") or {}).get("steps"))
    if request.args.get("as") == "blocks" and engine.macro_format(m) == engine.GRAPH_FORMAT:
        try:
            out["blocks"] = engine.graph_to_blocks(m.get("graph") or {})
        except engine.UnstructuredGraph:
            pass   # badge « avancé » : la vue Blocs reste indisponible
    return out


def _published_macro(pid, mid):
    """La macro système `mid` si elle est publiée vers le projet `pid`, sinon None."""
    m = db_get_macro(mid)
    if m and m.get("project_id") is None and pid in (m.get("published_to") or []):
        return m
    return None

@bp.route("/api/projects/<int:pid>/macros", methods=["GET"])
@require_project_role("viewer")
def project_macros_list(pid):
    out = []
    for m in db_project_macros(pid):
        out.append({"id": m["id"], "name": m["name"], "updated_at": m.get("updated_at"),
                    "graph": m.get("graph"), **_macro_meta(m)})
    # Macros système publiées : bouton OPAQUE (ni graph, ni détail — modèle sudo).
    for m in db_macros_published_to(pid):
        out.append({"id": m["id"], "name": m["name"], "system": True})
    return jsonify({"macros": out})

@bp.route("/api/projects/<int:pid>/macros", methods=["POST"])
@require_project_role("editor")
def project_macros_create(pid):
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name requis"}), 400
    err = _graph_error(data.get("graph"))
    if err:
        return jsonify({"error": err}), 400
    mid = db_create_macro(pid, name, (current_user() or {}).get("id"),
                          graph=data.get("graph"))
    return jsonify({"status": "ok", "macro": _macro_payload(db_get_macro(mid))})

@bp.route("/api/projects/<int:pid>/macros/<int:mid>", methods=["GET"])
@require_project_role("viewer")
def project_macros_get(pid, mid):
    m = db_get_macro(mid)
    if not m or m.get("project_id") != pid:
        return jsonify({"error": "macro introuvable"}), 404
    return jsonify({"macro": _macro_payload(m)})

@bp.route("/api/projects/<int:pid>/macros/<int:mid>", methods=["PATCH"])
@require_project_role("editor")
def project_macros_update(pid, mid):
    m = db_get_macro(mid)
    if not m or m.get("project_id") != pid:
        return jsonify({"error": "macro introuvable"}), 404
    data = request.json or {}
    graph = data.get("graph")
    if graph is not None and not isinstance(graph, dict):
        return jsonify({"error": "graph invalide"}), 400
    err = _graph_error(graph)
    if err:
        return jsonify({"error": err}), 400
    db_update_macro(mid, name=(data.get("name") or "").strip() or None, graph=graph)
    return jsonify({"status": "ok", "macro": _macro_payload(db_get_macro(mid))})

@bp.route("/api/projects/<int:pid>/macros/<int:mid>", methods=["DELETE"])
@require_project_role("editor")
def project_macros_delete(pid, mid):
    m = db_get_macro(mid)
    if not m or m.get("project_id") != pid:
        return jsonify({"error": "macro introuvable"}), 404
    db_delete_macro(mid)
    return jsonify({"status": "ok"})

@bp.route("/api/projects/<int:pid>/macros/<int:mid>/run", methods=["POST"])
@require_project_role("operator")
def project_macros_run(pid, mid):
    # Macro du projet, OU macro système publiée vers ce projet (elle s'exécute
    # avec sa propre autorité — le moteur tourne côté orchestrateur ; le journal
    # enregistre l'invocateur réel via user=).
    m = db_get_macro(mid)
    if not m or (m.get("project_id") != pid and not _published_macro(pid, mid)):
        return jsonify({"error": "macro introuvable"}), 404
    # nodes/v2 : `entry_id` (optionnel) = démarrer sur UNE entrée précise du graphe.
    run, err = engine.run_macro(mid, user=(current_user() or {}).get("username"),
                                allow_system_config=has_perm("containers.deploy"),
                                entry_id=(request.get_json(silent=True) or {}).get("entry_id"))
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"status": "started", "run": run.snapshot()})

@bp.route("/api/projects/<int:pid>/macros/<int:mid>/status", methods=["GET"])
@require_project_role("viewer")
def project_macros_status(pid, mid):
    m = db_get_macro(mid)
    if not m or (m.get("project_id") != pid and not _published_macro(pid, mid)):
        return jsonify({"error": "macro introuvable"}), 404
    return jsonify({"run": engine.run_status(mid)})

@bp.route("/api/projects/<int:pid>/macros/<int:mid>/cancel", methods=["POST"])
@require_project_role("operator")
def project_macros_cancel(pid, mid):
    return jsonify({"cancelled": engine.cancel_run(mid)})


# ─── Macros SYSTÈME (admin, inter-projets) + publication ──────
# project_id IS NULL. Création/édition réservée aux accès globaux ; l'admin les
# PUBLIE vers des projets (published_to) où elles apparaissent comme des boutons
# opaques exécutables (routes projet ci-dessus). Hors exports projet.

@bp.route("/api/macros", methods=["GET"])
@require_global_access
def system_macros_list():
    return jsonify({"macros": [
        {"id": m["id"], "name": m["name"], "updated_at": m.get("updated_at"),
         "graph": m.get("graph"), "published_to": m.get("published_to") or [],
         **_macro_meta(m)}
        for m in db_system_macros()]})

@bp.route("/api/macros", methods=["POST"])
@require_global_access
def system_macros_create():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name requis"}), 400
    err = _graph_error(data.get("graph"))
    if err:
        return jsonify({"error": err}), 400
    mid = db_create_macro(None, name, (current_user() or {}).get("id"),
                          graph=data.get("graph"))
    return jsonify({"status": "ok", "macro": _macro_payload(db_get_macro(mid))})

@bp.route("/api/macros/<int:mid>", methods=["GET"])
@require_global_access
def system_macros_get(mid):
    m = db_get_macro(mid)
    if not m or m.get("project_id") is not None:
        return jsonify({"error": "macro introuvable"}), 404
    return jsonify({"macro": _macro_payload(m)})

@bp.route("/api/macros/<int:mid>", methods=["PATCH"])
@require_global_access
def system_macros_update(mid):
    m = db_get_macro(mid)
    if not m or m.get("project_id") is not None:
        return jsonify({"error": "macro introuvable"}), 404
    data = request.json or {}
    graph = data.get("graph")
    if graph is not None and not isinstance(graph, dict):
        return jsonify({"error": "graph invalide"}), 400
    err = _graph_error(graph)
    if err:
        return jsonify({"error": err}), 400
    published_to = data.get("published_to")
    if published_to is not None:
        if not isinstance(published_to, list):
            return jsonify({"error": "published_to invalide"}), 400
        try:
            published_to = sorted({int(p) for p in published_to})
        except (TypeError, ValueError):
            return jsonify({"error": "published_to invalide"}), 400
        missing = [p for p in published_to if not db_get_project(p)]
        if missing:
            return jsonify({"error": f"projets inconnus : {missing}"}), 400
    db_update_macro(mid, name=(data.get("name") or "").strip() or None,
                    graph=graph, published_to=published_to)
    return jsonify({"status": "ok", "macro": _macro_payload(db_get_macro(mid))})

@bp.route("/api/macros/<int:mid>", methods=["DELETE"])
@require_global_access
def system_macros_delete(mid):
    m = db_get_macro(mid)
    if not m or m.get("project_id") is not None:
        return jsonify({"error": "macro introuvable"}), 404
    db_delete_macro(mid)
    return jsonify({"status": "ok"})

@bp.route("/api/macros/<int:mid>/run", methods=["POST"])
@require_global_access
def system_macros_run(mid):
    m = db_get_macro(mid)
    if not m or m.get("project_id") is not None:
        return jsonify({"error": "macro introuvable"}), 404
    run, err = engine.run_macro(mid, user=(current_user() or {}).get("username"),
                                allow_system_config=has_perm("containers.deploy"),
                                entry_id=(request.get_json(silent=True) or {}).get("entry_id"))
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"status": "started", "run": run.snapshot()})

@bp.route("/api/macros/<int:mid>/status", methods=["GET"])
@require_global_access
def system_macros_status(mid):
    m = db_get_macro(mid)
    if not m or m.get("project_id") is not None:
        return jsonify({"error": "macro introuvable"}), 404
    return jsonify({"run": engine.run_status(mid)})

@bp.route("/api/macros/<int:mid>/cancel", methods=["POST"])
@require_global_access
def system_macros_cancel(mid):
    return jsonify({"cancelled": engine.cancel_run(mid)})

@bp.route("/api/macros/catalog", methods=["GET"])
@require_global_access
def system_macros_catalog():
    """Catalogue GLOBAL (tous les containers plugins, sans filtre projet + TOUTES
    les actions de services) pour l'édition des macros système."""
    from .. import core_plugins
    return jsonify({"containers": _catalog_containers(pids=None),
                    "macros": [{"id": m["id"], "name": m["name"], "system": True}
                               for m in db_system_macros()],
                    "services": core_plugins.service_actions()})
# ─── Déclencheurs permanents (editor écrit, operator active/désactive) ──────

def _trigger_field_error(pid, data):
    """Valide les champs modifiables d'un trigger. Renvoie un message ou None."""
    if "condition" in data and not isinstance(data.get("condition"), dict):
        return "condition invalide"
    if "macro_id" in data and data.get("macro_id") is not None:
        try:
            mid = int(data["macro_id"])
        except (TypeError, ValueError):
            return "macro_id invalide"
        if not any(m["id"] == mid for m in db_project_macros(pid)):
            return "macro hors projet"
    if "cooldown_ms" in data:
        try:
            if int(data.get("cooldown_ms") or 0) < 0:
                return "cooldown_ms invalide"
        except (TypeError, ValueError):
            return "cooldown_ms invalide"
    return None

@bp.route("/api/projects/<int:pid>/triggers", methods=["GET"])
@require_project_role("viewer")
def project_triggers_list(pid):
    return jsonify({"triggers": db_project_triggers(pid)})

@bp.route("/api/projects/<int:pid>/triggers", methods=["POST"])
@require_project_role("editor")
def project_triggers_create(pid):
    data = request.json or {}
    err = _trigger_field_error(pid, data)
    if err:
        return jsonify({"error": err}), 400
    tid = db_create_trigger(pid, name=(data.get("name") or "").strip() or None,
                            condition=data.get("condition"),
                            macro_id=data.get("macro_id"),
                            cooldown_ms=data.get("cooldown_ms", 2000),
                            enabled=bool(data.get("enabled")))
    return jsonify({"status": "ok", "trigger": db_get_trigger(tid)})

@bp.route("/api/projects/<int:pid>/triggers/<int:tid>", methods=["PATCH"])
@require_project_role("operator")
def project_triggers_update(pid, tid):
    tr = db_get_trigger(tid)
    if not tr or tr.get("project_id") != pid:
        return jsonify({"error": "déclencheur introuvable"}), 404
    data = request.json or {}
    # operator peut UNIQUEMENT (dés)activer ; le reste (nom/condition/macro/
    # cooldown) exige editor — vérifié ici car le décorateur ne porte que le min.
    if set(data.keys()) - {"enabled"}:
        if not project_role_at_least(project_role_for(pid, current_user()), "editor"):
            return jsonify({"error": "forbidden", "reason": "editor_required"}), 403
    err = _trigger_field_error(pid, data)
    if err:
        return jsonify({"error": err}), 400
    db_update_trigger(tid,
                      name=(data.get("name") or "").strip() if "name" in data else None,
                      enabled=bool(data["enabled"]) if "enabled" in data else None,
                      condition=data.get("condition"),
                      macro_id=(data.get("macro_id") if "macro_id" in data else ...),
                      cooldown_ms=data.get("cooldown_ms"))
    return jsonify({"status": "ok", "trigger": db_get_trigger(tid)})

@bp.route("/api/projects/<int:pid>/triggers/<int:tid>", methods=["DELETE"])
@require_project_role("editor")
def project_triggers_delete(pid, tid):
    tr = db_get_trigger(tid)
    if not tr or tr.get("project_id") != pid:
        return jsonify({"error": "déclencheur introuvable"}), 404
    db_delete_trigger(tid)
    return jsonify({"status": "ok"})
