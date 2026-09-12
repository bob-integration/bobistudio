# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Télémétrie/diagnostic infra pour la page Monitoring : tissu de composition (fabric overview),
onglets contribués par les plugins, pyramide (reconcile/overview), bande passante mémoire,
santé nœuds (CPU/RAM/disque/GPU), GPU, et graphe shm (activité/producteur/consommateurs).
Presque tout est lecture de cache rafraîchi par la surveillance périodique — aucun appel réseau
synchrone ici, sauf les endpoints `*/reconcile`, `*/measure`, `*/refresh` explicitement forcés."""

import json
import logging

from flask import jsonify, request

from . import bp
from .shared import _load_dc
from ..auth import require_login, require_perm
from ..database import db_get_containers

log = logging.getLogger(__name__)


@bp.route("/api/fabric/overview", methods=["GET"])
@require_login
def api_fabric_overview():
    """Vue diagnostic du tissu de composition : pour chaque multiview ASSEMBLEUR, ses shards
    (avec latence propre) + la latence cumulée du chemin critique (data-driven : max(shards) +
    assembleur, les shards tournant en parallèle), + les proxies pyramide. Lecture seule (registre
    + cache de latences, aucun appel réseau)."""
    from .. import metrics as _m
    from ..database import db_fabric_all, db_get_nodes
    conts = {c["vmid"]: c for c in db_get_containers()}
    _nodes = {n["id"]: (n.get("name") or ("serveur " + str(n["id"]))) for n in db_get_nodes()}
    # Nom de shm SOURCE → serveur producteur (depuis shm_out dénormalisé) : pour repérer les flux
    # qui TRAVERSENT un serveur (échange inter-nœuds) côté UI.
    _shm_node = {}
    for _c in conts.values():
        for _tok in (_c.get("shm_out") or "").split("·"):
            _tok = _tok.strip()
            if _tok and " " not in _tok and ":" not in _tok:
                _shm_node[_tok] = _c.get("node_id")

    def _params(vmid):
        c = conts.get(vmid) or {}
        try:
            return (json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
        except Exception:
            return {}

    def _fmt(p):
        return {"chroma": p.get("chroma") or "422", "bit_depth": p.get("bit_depth") or 8}

    rows = db_fabric_all()
    asm = {}
    shard_rows = []
    for r in rows:
        if r.get("kind") == "assembler":
            asm[r.get("vmid")] = {"vmid": r.get("vmid"), "shm": r.get("shm"),
                                  "node_id": r.get("node_id"), "shards": []}
        elif r.get("kind") in ("shard", "shared"):
            shard_rows.append(r)
    for r in shard_rows:
        try: parents = json.loads(r["parents"]) if r.get("parents") else []
        except Exception: parents = []
        ref = r.get("ref")
        rvmid = int(ref) if ref and str(ref).isdigit() else None
        sp = _params(rvmid) if rvmid is not None else {}
        # Sources amont du shard (dédupliquées) = ce qu'il lit (cellules de son deploy_config).
        srcs, seen = [], set()
        for w in (sp.get("flux_config") or []):
            s = (w.get("path") or "").replace("/dev/shm/", "")
            if s and s not in seen:
                seen.add(s); srcs.append(s)
        info = {"signature": r["signature"], "shm": r.get("shm"), "kind": r.get("kind"),
                "vmid": rvmid, "out_w": r.get("out_w"), "out_h": r.get("out_h"),
                "own_latency_ms": _m.own_latency_cache.get(rvmid),
                "sources": srcs, "cells": len(sp.get("flux_config") or []), "format": _fmt(sp),
                "node_id": r.get("node_id"), "node_name": _nodes.get(r.get("node_id"))}
        for pv in parents:
            try: pvi = int(pv)
            except (TypeError, ValueError): continue
            if pvi in asm:
                asm[pvi]["shards"].append(info)
    out = []
    for vmid, a in asm.items():
        c = conts.get(vmid) or {}
        a["hostname"] = c.get("hostname")
        a["node_name"] = _nodes.get(a.get("node_id"))
        a["format"] = _fmt(_params(vmid))
        try:
            a["fps"] = float(_params(vmid).get("fps") or 0) or None   # pour le verdict « > 1 image » côté UI
        except (TypeError, ValueError):
            a["fps"] = None
        a["own_latency_ms"] = _m.own_latency_cache.get(vmid)
        sl = [s["own_latency_ms"] for s in a["shards"] if s["own_latency_ms"]]
        a["cumulative_latency_ms"] = round((max(sl) if sl else 0) + (a["own_latency_ms"] or 0), 1)
        out.append(a)
    proxies = []
    for c in conts.values():
        try:
            dc = json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            dc = {}
        if dc.get("type") == "pyramide":
            proxies.append({"vmid": c["vmid"], "hostname": c.get("hostname"),
                            "own_latency_ms": _m.own_latency_cache.get(c["vmid"])})
    # Serveur de chaque source référencée (pour le rendu multi-serveur côté UI).
    src_nodes = {}
    for a in out:
        for s in a["shards"]:
            for src in (s.get("sources") or []):
                nid = _shm_node.get(src)
                if nid is not None:
                    src_nodes[src] = _nodes.get(nid)
    return jsonify({"assemblers": out, "proxies": proxies, "source_nodes": src_nodes})


@bp.route("/api/monitoring/panels", methods=["GET"])
@require_login
def api_monitoring_panels():
    """Onglets Monitoring contribués par les plugins (manifest.monitoring). Chaque plugin
    présent (≥1 container de son type, ou when=="always") apparaît comme un onglet ; la page
    Monitoring charge son fragment UI via /api/plugins/<type>/ui/monitoring_html + monitoring_js.
    Aucun panneau n'est codé en dur côté page : tout vient des plugins."""
    from .. import plugins
    # Compte d'instances par type (parse unique des deploy_config).
    counts = {}
    for c in db_get_containers():
        t = (_load_dc(c) or {}).get("type")
        if t:
            counts[t] = counts.get(t, 0) + 1
    out = []
    for p in plugins.monitoring_panels():
        present = True if p["when"] == "always" else (counts.get(p["type"], 0) > 0)
        if not present:
            continue
        out.append({"type": p["type"], "label": p["label"],
                    "order": p["order"], "instances": counts.get(p["type"], 0)})
    return jsonify({"panels": out})


@bp.route("/api/pyramide/reconcile", methods=["POST"])
@require_login
def api_pyramide_reconcile():
    """Force le reconcile des tailles sur-mesure de la pyramide (hot-apply) : agrège les besoins
    (proxy_needs) des consommateurs et met à jour les pyramides. `node_id` optionnel (sinon tous
    les nœuds portant ≥1 pyramide)."""
    import json as _json
    from ..deploy import reconcile_pyramide_sizes
    from ..database import db_get_containers as _dbc
    nid = (request.args.get("node_id") or (request.get_json(silent=True) or {}).get("node_id"))
    if nid not in (None, ""):
        try: nodes = [int(nid)]
        except (TypeError, ValueError): return jsonify({"error": "node_id invalide"}), 400
    else:
        nodes = sorted({c.get("node_id") for c in _dbc()
                        if c.get("node_id") is not None
                        and (_json.loads(c.get("deploy_config") or "{}") or {}).get("type") == "pyramide"})
    changed = []
    for n in nodes:
        try: changed += (reconcile_pyramide_sizes(n) or {}).get("changed", [])
        except Exception as e: log.warning("reconcile pyramide node %s: %s", n, e)
    return jsonify({"ok": True, "nodes": nodes, "changed": changed})


@bp.route("/api/pyramide/overview", methods=["GET"])
@require_login
def api_pyramide_overview():
    """Console Pyramide (P3) : par pyramide → proxies (taille, #conso, orphelin), besoins non
    couverts, réglages ; + KPIs. `node_id` optionnel. Lecture cache (pas d'appel réseau)."""
    from ..metrics import pyramide_overview
    nid = request.args.get("node_id")
    try:
        nid = int(nid) if nid not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "node_id invalide"}), 400
    return jsonify(pyramide_overview(nid))


@bp.route("/api/membw", methods=["GET"])
@require_login
def api_membw():
    """Bande passante mémoire par nœud (canary memcpy) : dernier débit Go/s, référence apprise,
    ratio et niveau d'alerte. Lecture du cache rafraîchi par la surveillance (pas d'appel ici)."""
    from .. import membw
    return jsonify({"nodes": membw.latest()})


@bp.route("/api/membw/measure", methods=["POST"])
@require_perm("containers.deploy")
def api_membw_measure():
    """Force une mesure immédiate de tous les nœuds (sinon throttlé ~60 s)."""
    from .. import membw
    membw.sample_all(force=True)
    return jsonify({"nodes": membw.latest()})


@bp.route("/api/membw/reset", methods=["POST"])
@require_perm("containers.deploy")
def api_membw_reset():
    """Réinitialise la référence (à faire après ajout de RAM / changement matériel)."""
    from .. import membw
    nid = (request.json or {}).get("node_id")
    membw.reset_baseline(int(nid) if nid not in (None, "") else None)
    return jsonify({"status": "ok"})


@bp.route("/api/nodes/health", methods=["GET"])
@require_login
def api_nodes_health():
    """Santé matérielle (CPU/RAM/disque/réseau/versions) du contrôleur + de chaque nœud.
    Lecture du cache rafraîchi par la surveillance (~5 s) — pas d'appel agent/SSH ici."""
    from .. import node_health
    return jsonify(node_health.latest())


@bp.route("/api/nodes/health/<node_key>/history", methods=["GET"])
@require_login
def api_nodes_health_history(node_key):
    """Ring 10 min (sparklines) pour un nœud ('controller' pour l'hôte du contrôleur)."""
    from .. import node_health
    return jsonify({"history": node_health.history(node_key)})


@bp.route("/api/nodes/health/<node_key>/stats_24h", methods=["GET"])
@require_login
def api_nodes_health_stats_24h(node_key):
    """Agrégats min/moy/max sur 24 h (cpu/mem/shm/membw) pour un nœud."""
    from .. import node_health
    return jsonify(node_health.stats_24h(node_key))


@bp.route("/api/nodes/health/refresh", methods=["POST"])
@require_login
def api_nodes_health_refresh():
    """Force un échantillon immédiat (sinon throttlé ~5 s)."""
    from .. import node_health, gpu
    gpu.sample_all(force=True)        # GPU avant santé → le merge lit le cache frais
    node_health.sample_all(force=True)
    return jsonify(node_health.latest())


@bp.route("/api/nodes/<int:node_id>/prep/refresh", methods=["POST"])
@require_login
def api_node_prep_refresh(node_id):
    """Re-sonde la prép hôte MTL d'un nœud MAINTENANT (aller-retour agent, ~1 s).

    Route DÉDIÉE et non un paramètre de `/api/nodes/health/refresh` : ce dernier est appelé
    automatiquement par l'UI et doit rester une simple relecture de caches, alors qu'ici on
    déclenche une vraie sonde sur l'hôte. Le verdict périodique a jusqu'à 30 min ; sans ce bouton
    l'opérateur n'aurait aucun moyen de savoir si ce qu'il vient de corriger est pris en compte."""
    from .. import node_health
    r = node_health.forcer_prep(node_id)
    return jsonify(r), (200 if r.get("ok") else 400)


@bp.route("/api/nodes/<int:node_id>/cpu-map", methods=["GET"])
@require_login
def api_node_cpu_map(node_id):
    """« Qui consomme quoi » sur un nœud : containers avec CPU% courant (colonne rafraîchie ~5 s
    par la surveillance) + pinning (pinned_cores, complété par node_core_alloc pour les lcores
    DPDK des moteurs posés via reserve_exact — même logique que cpu_status). DB seule."""
    from .. import core_pool
    from .. import cpu_pressure as _psi
    from .. import gpu_pool
    from ..database import db_get_node
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud inconnu"}), 404
    engine_cores = core_pool.allocations_by_vmid(node_id)  # {vmid: [cores]} pins + moteurs
    # Conteneurs qui TIENNENT un GPU : leurs cœurs se distinguent des cœurs de calcul ordinaires
    # dans la carte CPU. C'est ce qui rend visible un mur GPU épinglé du mauvais côté du bus.
    gpu_de = gpu_pool.gpu_par_vmid(node_id)
    out, by_cpu = [], {}
    for c in db_get_containers():
        if c.get("node_id") != node_id:
            continue
        pin = c.get("pinned_cores")
        if pin:
            cores, source = sorted(core_pool.parse_cpuset(pin)), "pin"
        elif engine_cores.get(c["vmid"]):
            cores, source = sorted(engine_cores[c["vmid"]]), "engine"
            pin = core_pool.fmt_cpuset(cores)
        else:
            cores, source, pin = None, None, None
        out.append({
            "vmid": c["vmid"], "hostname": c.get("hostname"),
            "type": (_load_dc(c) or {}).get("type"),
            "cpu_percent": c.get("cpu_percent"), "mem_used": c.get("mem_used"),
            "pinned_cores": pin, "cores": cores, "source": source,
            "gpu_index": gpu_de.get(c["vmid"]),
            # Pression CPU du conteneur (PSI cgroup v2) : `full` = fraction du temps où TOUTES ses
            # tâches sont bloquées en attente de cœur. Le CPU% ne dit PAS ça (un conteneur affamé
            # consomme peu de CPU — c'est justement le problème). Cf. app/cpu_pressure.py.
            "psi": _psi.for_container(c["vmid"]) or {},
        })
        for cpu in (cores or []):
            by_cpu.setdefault(str(cpu), []).append(
                {"vmid": c["vmid"], "hostname": c.get("hostname"), "source": source,
                 "gpu_index": gpu_de.get(c["vmid"])})
    out.sort(key=lambda x: -(x["cpu_percent"] or -1))
    psi_node = _psi.latest(node_id) or {}
    # VENTILATION PAR SOCKET : l'agrégat `capacite` ne suffit pas — « il reste 2 cœurs » se lit
    # « il reste de la place » alors que ces cœurs peuvent être tous du mauvais côté du bus (cf.
    # core_pool.capacite_par_socket). `numa` sert à découper la bande de cœurs par socket dans l'UI.
    # Liste vide / dict vide = topologie non lisible → l'UI retombe sur l'affichage agrégé.
    return jsonify({"ok": True, "node_id": node_id,
                    "compute_cpuset": node.get("compute_cpuset") or "",
                    "capacite": core_pool.cores_status(node_id),
                    "sockets": core_pool.capacite_par_socket(node_id),
                    "numa": {str(k): v for k, v in (core_pool.numa_map_cached(node_id) or {}).items()},
                    # {cpu logique: cœur PHYSIQUE} — deux threads HyperThreading d'un même cœur
                    # partagent les unités d'exécution. C'est pour ça que la capacité se compte en
                    # cœurs physiques ; encore faut-il que l'UI puisse MONTRER l'appariement (les
                    # jumeaux sont `i` et `i+48`, donc invisibles dans une bande ordonnée par index).
                    "cores_phys": {str(k): v for k, v in (core_pool.core_map_cached(node_id) or {}).items()},
                    "psi_host": psi_node.get("host") or {},
                    "containers": out, "by_cpu": by_cpu})


@bp.route("/api/nodes/<int:node_id>/core-snapshot", methods=["GET"])
@require_login
def api_node_core_snapshot(node_id):
    """Instantané des cœurs : mesure on-demand ~0,5 s VIA L'AGENT du nœud (% réel par cœur +
    threads/conteneurs dessus). Exception assumée au « aucun appel réseau synchrone ici » du
    docstring module — même famille que les `*/measure` explicitement forcés. Enrichit les noms
    docker en vmid/hostname (l'agent ne connaît pas les vmids)."""
    from .. import node_driver
    from ..database import db_get_node
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud inconnu"}), 404
    if not node.get("agent_url"):
        return jsonify({"ok": False, "error": "agent_required"})
    ok, data = node_driver.core_snapshot(node)
    if not ok or not isinstance(data, dict) or "cores" not in data:
        # 404 « route inconnue » = agent < 0.16.0 → l'UI propose la MAJ agent.
        return jsonify({"ok": False, "error": "agent_too_old",
                        "agent_version": node.get("agent_version")})
    by_name = {c.get("docker_name"): c for c in db_get_containers()
               if c.get("node_id") == node_id and c.get("docker_name")}
    for core in data.get("cores") or []:
        for t in core.get("top") or []:
            c = by_name.get(t.get("container"))
            if c:
                t["vmid"], t["hostname"] = c["vmid"], c.get("hostname")
    return jsonify(data)


@bp.route("/api/nodes/<int:node_id>/placement", methods=["GET"])
@require_login
def api_node_placement(node_id):
    """CONSTAT du placement CPU réel des conteneurs du nœud (cf. app/placement.py) : bande isolée
    ACTIVE, cpuset posé par conteneur, répartition RÉELLE des threads par cœur, et les violations
    des deux invariants (I1 ordonnançabilité, I2 exclusivité du busy-poll).

    Pendant de `core-snapshot` : celui-ci dit QUI CONSOMME, celui-là dit QUI A LE DROIT DE TOURNER
    OÙ — la question à laquelle rien ne répondait quand un moteur pinné sur 16 cœurs en avait 15
    d'isolés. `?force=1` force un relevé neuf (sinon cache 120 s, partagé avec la surveillance)."""
    from .. import placement
    from ..database import db_get_node
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud inconnu"}), 404
    force = request.args.get("force") in ("1", "true", "on")
    res = placement.constater(node_id, force=force)
    res["releve"] = placement.releve_cache(node_id)          # déjà en cache : pas de second exec
    return jsonify(res)


@bp.route("/api/cpu/profiles", methods=["GET"])
@require_login
def api_cpu_profiles():
    """Coût CPU MESURÉ par type de conteneur, confronté au `resources.cores` de son manifeste
    (cf. app/cpu_profiles.py). Lecture du cache accumulé par la surveillance — aucun appel réseau.

    C'est la réponse à « combien de X tient ce nœud », qui n'existait que sous forme déclarative :
    `verdict` dit si le profil déclaré est conforme, sur-dimensionné (on réserve du vide) ou
    sous-dimensionné (le quota étrangle, et le pré-vol projet ment). `?node_id=` pour un nœud."""
    from .. import cpu_profiles
    nid = request.args.get("node_id")
    return jsonify({"profiles": cpu_profiles.profils(node_id=int(nid) if nid else None),
                    "unite": "% d'un CPU (100 % = un cœur saturé)",
                    "fenetre_min": int(cpu_profiles.MAX_POINTS * cpu_profiles.INTERVALLE_S / 60),
                    "min_points": cpu_profiles.MIN_POINTS})


@bp.route("/api/gpu", methods=["GET"])
@require_login
def api_gpu():
    """Télémétrie GPU par nœud (NVIDIA) : util/VRAM + échange PCIe RAM↔GPU. Lecture du cache
    rafraîchi par la surveillance (~5 s). Aussi fusionnée dans /api/nodes/health (champ `gpu`)."""
    from .. import gpu
    return jsonify({"nodes": gpu.latest()})


@bp.route("/api/shm/status", methods=["GET"])
@require_login
def api_shm_status():
    """Renvoie l'état des SHM connus du pipeline : activité (frame_index progresse ?),
    fps, frame_rate calculé, producteur et consommateurs (depuis la topologie en mémoire)."""
    from ..metrics import frame_index_cache, shm_active_cache, _frame_index_prev, latency_cache
    from ..database import db_get_containers as db_list_containers
    containers = db_list_containers()
    vmid_info = {c["vmid"]: c for c in containers}

    # Reconstituer le graphe shm → producteur / consommateurs depuis deploy_config
    from .. import plugins as _plugins
    import json as _json
    shm_map = {}   # shm_name → {producer, consumers, fps, shm_active, frame_index}

    for c in containers:
        vmid = c["vmid"]
        cfg = _json.loads(c.get("deploy_config") or "{}")
        params = cfg.get("params", {})
        kind = params.get("plugin_type") or cfg.get("plugin_type") or ""
        hostname = c.get("hostname") or f"vmid{vmid}"
        fps = c.get("fps")

        try:
            topo_fn = _plugins.get_hook(kind, "topology_ports")
            ctx = {"vmid": vmid, "settings": {}}
            ports = topo_fn(hostname, params, ctx) if topo_fn else {}
        except Exception:
            ports = {}

        for p in (ports.get("produces") or []):
            shm = p.get("shm") or ""
            if not shm:
                continue
            shm_map.setdefault(shm, {"shm": shm, "producer": None, "consumers": [], "fps": None, "shm_active": None, "frame_index": None})
            shm_map[shm]["producer"] = {"vmid": vmid, "hostname": hostname, "kind": kind}
            shm_map[shm]["fps"] = fps
            shm_map[shm]["shm_active"] = shm_active_cache.get(vmid)
            shm_map[shm]["frame_index"] = frame_index_cache.get(vmid)

        for p in (ports.get("consumes") or []):
            shm = p.get("shm") or ""
            if not shm or p.get("disconnected"):
                continue
            shm_map.setdefault(shm, {"shm": shm, "producer": None, "consumers": [], "fps": None, "shm_active": None, "frame_index": None})
            shm_map[shm]["consumers"].append({"vmid": vmid, "hostname": hostname, "kind": kind})

    result = sorted(shm_map.values(), key=lambda x: (x["producer"] is None, x["shm"]))
    return jsonify(result)


@bp.route("/api/containers/<int:vmid>/fabric", methods=["GET"])
@require_login
def api_fabric_mur(vmid):
    """État du tissu POUR UN MUR, à l'usage du composer. Lecture seule (registre + état mémoire),
    aucun appel réseau : l'éditeur l'interroge en boucle tant qu'il est ouvert.

    Deux informations, pour deux besoins d'interface distincts :

    • `regions` — les rectangles réellement matérialisés (un shard = un conteneur qui compose sa
      part du mur). Une fenêtre déplacée À L'INTÉRIEUR de sa région ne coûte qu'une mutation à
      chaud ; une fenêtre qui FRANCHIT une frontière oblige à recomposer la découpe, donc à
      construire un conteneur. Sans les voir, l'utilisateur subit deux comportements très
      différents pour le même geste, sans rien qui les distingue.

    • `etat` — « reorganisation » tant qu'un conteneur neuf n'a pas pris le relais. C'est le seul
      cas lent (~5-10 s) ; il faut l'ANNONCER, sinon l'attente passe pour un raté.

    `regions` est vide si le mur n'est pas shardé (cas courant) : rien à dessiner, tout est
    instantané."""
    from ..database import db_fabric_get, db_fabric_all
    from .. import compositor_fabric as _cf
    if not db_fabric_get(f"asm:{vmid}"):
        return jsonify({"sharded": False, "regions": [], "etat": None, "depuis_s": 0.0})
    regions = []
    for r in db_fabric_all():
        if r.get("kind") != "shard" or r.get("tile_x") is None:
            continue
        try:
            parents = json.loads(r.get("parents")) if r.get("parents") else []
        except Exception:                                                  # noqa: BLE001
            parents = []
        if str(vmid) not in [str(p) for p in parents]:
            continue
        regions.append({"x": int(r.get("tile_x") or 0), "y": int(r.get("tile_y") or 0),
                        "w": int(r.get("out_w") or 0), "h": int(r.get("out_h") or 0)})
    etat, depuis = _cf.etat_mur(vmid)
    return jsonify({"sharded": True, "regions": regions, "etat": etat, "depuis_s": depuis})


@bp.route("/api/containers/<int:vmid>/migration/simuler", methods=["GET"])
@require_login
def api_migration_simuler(vmid):
    """SIMULATION d'une migration de conteneur — ne touche à RIEN.

    `?node_id=N` : verdict pour ce nœud. Sans `node_id` : verdict pour TOUS les autres nœuds, de
    quoi répondre « où pourrais-je le mettre ? » d'un seul appel.

    La bascule elle-même n'est pas exposée : elle viendra s'appuyer sur ce plan. La vérification
    vaut d'être livrée seule — c'est l'essentiel du travail, elle répond déjà à « ce nœud
    pourrait-il accueillir ce conteneur ? », et c'est elle qui rendra le bouton sûr."""
    from .. import migration as _mig
    nid = request.args.get("node_id")
    if nid not in (None, ""):
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            return jsonify({"error": "node_id invalide"}), 400
        return jsonify(_mig.plan_migration(vmid, nid))
    return jsonify({"vmid": vmid, "candidats": _mig.noeuds_candidats(vmid)})


@bp.route("/api/containers/<int:vmid>/migration", methods=["POST"])
@require_perm("containers.deploy")
def api_migration_executer(vmid):
    """BASCULE : déplace le conteneur vers `node_id`, en conservant son vmid.

    Corps : {node_id, forcer?}. `forcer` outrepasse les vérifications de CAPACITÉ, jamais les refus
    de TYPE. Opération DESTRUCTIVE avec coupure (le conteneur est retiré du nœud source puis
    redéployé sur la cible) — la simulation, elle, est en GET et ne touche à rien."""
    from .. import migration as _mig
    body = request.get_json(silent=True) or {}
    nid = body.get("node_id", request.args.get("node_id"))
    try:
        nid = int(nid)
    except (TypeError, ValueError):
        return jsonify({"error": "node_id requis"}), 400
    forcer = str(body.get("forcer", request.args.get("forcer", ""))).strip().lower() \
        in ("1", "true", "yes", "on")
    res = _mig.migrer(vmid, nid, forcer=forcer)
    return jsonify(res), (200 if res.get("ok") else 409)
