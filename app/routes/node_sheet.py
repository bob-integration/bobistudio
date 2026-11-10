# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Fiche d'un nœud (Réglages → Réseau → Vue d'ensemble) et vérification avant de le couper.

Demande de Cyril, validée sur maquette le 2026-10-30 : arrêter ou redémarrer un nœud devait
être FACILE À TROUVER, et la décision prise en voyant ce qu'on coupe. Deux routes :

  · `GET /api/nodes/<id>/sheet` — tout ce que la fiche affiche, en UNE lecture. Rien n'y est
    sondé : relevé de santé déjà en mémoire (`node_health.latest`), base, alertes. Ouvrir la
    fiche ne coûte donc aucun aller-retour vers le nœud.
  · `GET /api/nodes/<id>/power-check` — ce que les gardes de `node_power` diraient MAINTENANT
    (processus en D, installation en cours), plus la liste de ce qui sera coupé, à l'antenne
    d'abord. Lecture seule, mais elle interroge l'hôte : réservée à qui peut couper.

Les gestes eux-mêmes restent sur leurs routes existantes (`/api/mtl/reboot`,
`/api/nodes/<id>/host-poweroff`), qui rejouent les gardes : cette vérification INFORME la boîte
de confirmation, elle n'autorise rien.
"""

import json
import logging
import time
from datetime import datetime

from flask import jsonify

from . import bp
from ..auth import require_login, require_perm
from ..database import db_get_node, db_get_containers, db_get_alerts

log = logging.getLogger(__name__)

# Bilans de reprise écrits par `node_recovery` (une ligne par démarrage), du plus au moins utile.
RECOVERY_KEYS = ("alert.node.recovery_releve", "alert.node.recovery_releve_echecs",
                 "alert.node.recovery_rien_a_relever", "alert.node.recovery_erreur",
                 "alert.node.reboot_recovery_off", "alert.node.reboot_recovery_lance")
# Bilans de la réparation des certificats mTLS (`node_recovery._certs_conteneurs_node`). Elle tourne
# MÊME reprise automatique désactivée — et c'est alors la seule reprise du démarrage : sur la Z8 le
# 30/10, 12 conteneurs sur 12 relevés ainsi, quand l'alerte « désactivée » disait seulement que rien
# d'autre ne le serait (relevé par la session Z8 sur la fiche déployée).
CERT_KEYS = ("alert.agent.certs_reprovisionnes", "alert.agent.certs_reprovisionnes_echecs",
             "alert.agent.certs_http_clair", "alert.agent.certs_verif_impossible")


def _node_or_404(node_id):
    node = db_get_node(node_id)
    if not node:
        return None, (jsonify({"ok": False, "error": "node_not_found"}), 404)
    return node, None


def _snapshot(node_id):
    """(dernier relevé de santé, en ligne ?, âge en s) — même règle que `/api/nodes`."""
    from .. import node_health, settings as _st
    snap = ((node_health.latest() or {}).get("nodes") or {}).get(str(node_id))
    fresh = max(20.0, 4 * float(_st.get("node_health_interval_s") or 5))
    age = (time.time() - snap.get("ts", 0)) if snap else None
    return snap or {}, bool(snap and snap.get("ok") and age is not None and age < fresh), age


def _alert_ts(a):
    try:
        return datetime.fromisoformat(a.get("timestamp") or "").timestamp()
    except ValueError:
        return None


def last_recovery(node_id, boot_ts):
    """Bilan de la reprise du démarrage COURANT, lu dans les alertes que `node_recovery` écrit
    déjà (aucune persistance de plus). None si aucun bilan depuis ce démarrage — premier relevé du
    nœud, ou orchestrateur absent au moment du redémarrage.

    `up` = conteneurs attendus et debout à la fin : les relevés PLUS ceux que Docker avait déjà
    remontés, c'est-à-dire tous sauf les échecs. `duration_s` = du démarrage du noyau au bilan :
    le temps pendant lequel le service a manqué, pas la durée de la seule passe de reprise."""
    if not boot_ts:
        return None
    certs = None
    for a in db_get_alerts(node_id=node_id, kind="node,agent", limit=300):
        key = a.get("msg_key")
        if key not in RECOVERY_KEYS and key not in CERT_KEYS:
            continue
        ts = _alert_ts(a)
        if ts is None or ts < float(boot_ts) - 60:
            return None                     # le bilan le plus récent date d'un démarrage antérieur
        try:
            p = json.loads(a.get("msg_params") or "{}")
        except ValueError:
            p = {}
        if key in CERT_KEYS:
            # Le plus RÉCENT seulement (les alertes arrivent de la plus récente à la plus ancienne).
            if certs is None:
                certs = {"key": key, "duration_s": max(0, round(ts - float(boot_ts))),
                         "broken": p.get("n_casses"), "repaired": p.get("n_repares") or 0,
                         "failed": int(p.get("n_echecs") or 0),
                         "error": key in ("alert.agent.certs_http_clair", "alert.agent.certs_verif_impossible")}
            continue
        total = p.get("total")
        failed = int(p.get("n_failed") or 0)
        out = {"key": key, "ts": ts, "duration_s": max(0, round(ts - float(boot_ts)))}
        if key == "alert.node.reboot_recovery_off":
            # Reprise automatique désactivée : la réparation des certificats est alors LA reprise
            # de ce démarrage. Reprise activée, son résultat est déjà fondu dans le bilan.
            out.update(state="disabled", certs=certs)
        elif key == "alert.node.reboot_recovery_lance":
            out["state"] = "running"
        elif key == "alert.node.recovery_erreur":
            out["state"] = "error"
        else:
            out.update(state="done", total=total, failed=failed,
                       up=(int(total) - failed) if total is not None else None)
        return out
    return None


@bp.route("/api/nodes/<int:node_id>/sheet", methods=["GET"])
@require_login
def api_node_sheet(node_id):
    from .. import core_pool, container_net
    from .images import node_images_state
    node, err = _node_or_404(node_id)
    if err:
        return err
    snap, online, age = _snapshot(node_id)
    res = snap.get("resources") or {}
    machine = snap.get("machine") or {}
    # Charge : les cœurs ORDONNANÇABLES d'abord (cf. `/api/nodes`, même raison : sur un nœud très
    # isolé, la moyenne machine reste au vert pendant la saturation). `scope` dit lequel est servi.
    ordo = (snap.get("cpu_partage") or {}).get("ordonnancables") or {}
    if ordo.get("pct") is not None:
        cpu_pct, cpu_scope = ordo["pct"], "schedulable"
    else:
        cpu_pct = res.get("cpu_pct_real") if res.get("cpu_pct_real") is not None else res.get("cpu_pct")
        cpu_scope = "machine" if cpu_pct is not None else None
    mem_pct = (round((res.get("mem_used_mb") or 0) / res["mem_total_mb"] * 100, 1)
               if res.get("mem_total_mb") else None)
    disk_pct = ((snap.get("disks") or {}).get("root") or {}).get("pct")
    try:
        caps = json.loads(node.get("capabilities") or "[]") or []
    except ValueError:
        caps = []
    try:
        pool = core_pool.cores_status(node_id)
    except Exception as e:                                                  # noqa: BLE001
        log.warning("node sheet %s: cores_status: %s", node_id, e)
        pool = None
    try:
        images = node_images_state(node)
    except Exception as e:                                                  # noqa: BLE001
        log.warning("node sheet %s: images: %s", node_id, e)
        images = {}
    boot_ts = node.get("last_boot_ts")
    return jsonify({
        "ok": True,
        "id": node_id, "name": node.get("name"), "host": node.get("host"),
        "online": online, "last_seen_s": round(age) if age is not None else None,
        "agent_version": snap.get("agent_version") or node.get("agent_version"),
        "capabilities": caps,
        "all_in_one": container_net.est_tout_en_un(node),
        "machine": {
            # Mesuré par l'agent (relevé de santé), jamais la colonne `nodes.cpu_model` : celle-ci
            # est la clé de qualification CPU (cf. cpu_qualify), vide tant qu'on n'a pas qualifié.
            "vendor": machine.get("vendor"), "product": machine.get("product"),
            "cpu_model": res.get("cpu_model") or machine.get("cpu_model"),
            "cores": machine.get("cores"), "threads": res.get("cpu_count") or machine.get("threads"),
            "sockets": machine.get("sockets"),
            "ram_mb": res.get("mem_total_mb"),
            "gpus": machine.get("gpus") if "gpus" in machine else None,
            # agent ≥ 0.32.4: what the CPU offers to protect a dedicated core's memory (absent = older agent)
            "memory_qos": machine.get("memory_qos"),
            "gpu_count": node.get("gpu_count") if node.get("gpu_capable") else 0,
            "reported": bool(machine),
        },
        "bmc": {"configured": bool((node.get("ilo_host") or "").strip())},
        "boot": {"last_boot_ts": boot_ts, "uptime_s": snap.get("host_uptime_s")},
        "recovery": last_recovery(node_id, boot_ts),
        "load": {"cpu_pct": cpu_pct, "cpu_scope": cpu_scope, "mem_pct": mem_pct, "disk_pct": disk_pct},
        "cores_pool": pool,
        "containers": sum(1 for c in db_get_containers() if c.get("node_id") == node_id),
        "images": images,
    })


def _params(c):
    try:
        return (json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
    except ValueError:
        return {}


def air_map(containers):
    """{vmid: raison} des conteneurs dont la coupure se voit HORS du cluster — sur TOUS les nœuds.

    ★ PAS UN RÉGLAGE. La première version lisait le poids CPU « Antenne » (cpu_weight) : sur la Z8,
    où personne ne l'a posé, les 13 conteneurs sortaient « hors antenne », decklink-io compris, qui
    porte la sortie SDI PGM (relevé par la session Z8 le 30/10). Une confirmation qui ne signale
    aucune coupure d'antenne parce qu'un réglage facultatif est vide ment par omission.

    Le critère est donc structurel :
      · SORTIE PHYSIQUE EN SERVICE (`{"kind": "output"}`) — un moteur d'entrée/sortie déclaré
        `split_io` par son manifeste (2110_io, decklink_io : ce qu'il CONSOMME part sur le câble)
        qui tourne et consomme au moins un flux ;
      · EN AMONT (`{"kind": "feeds", "output": <hostname>}`) — tout conteneur en marche qui
        alimente une telle sortie, directement ou en cascade, d'après `plugins.ports_topologie`
        (source unique du câblage). Une sortie d'un AUTRE nœud compte : couper le mélangeur coupe
        l'émission 2110 du voisin.
    Un moteur `split_io` rencontré en amont est une SOURCE : on ne remonte pas ses propres entrées
    TX, qui n'alimentent pas ses sorties RX."""
    from .. import plugins
    from ..docker_compute import _type_of
    ports, producers = {}, {}
    for c in containers:
        t = _type_of(c)
        if not t:
            continue
        try:
            pt = plugins.ports_topologie(t, c.get("hostname") or "", _params(c)) or {}
        except Exception as e:                                              # noqa: BLE001
            log.debug("air_map: ports of #%s unreadable (%s)", c.get("vmid"), e)
            continue
        split = bool((plugins.get(t) or {}).get("split_io"))
        cons = [x["shm"] for x in pt.get("consumes") or [] if isinstance(x, dict) and x.get("shm")]
        ports[c.get("vmid")] = (c, split, cons)
        for x in pt.get("produces") or []:
            if isinstance(x, dict) and x.get("shm"):
                producers.setdefault(x["shm"], []).append(c.get("vmid"))
    air = {}
    for vmid, (c, split, cons) in ports.items():
        if not (split and cons and c.get("status") == "running"):
            continue
        air[vmid] = {"kind": "output"}
        pile, vus = list(cons), set()
        while pile:
            shm = pile.pop()
            if shm in vus:
                continue
            vus.add(shm)
            for pv in producers.get(shm, ()):
                pc, psplit, pcons = ports[pv]
                if pv == vmid or pc.get("status") != "running":
                    continue
                air.setdefault(pv, {"kind": "feeds", "output": c.get("hostname")})
                if not psplit:
                    pile.extend(pcons)
    return air


def _ordered_containers(node_id):
    """Ce que la coupure emporte, à l'antenne d'abord (cf. `air_map`), puis ce qui tourne, puis le
    reste — l'ordre dans lequel l'exploitant doit le lire."""
    from ..docker_compute import _type_of
    tous = db_get_containers()
    air = air_map(tous)
    out = []
    for c in tous:
        if c.get("node_id") != node_id:
            continue
        a = air.get(c.get("vmid"))
        out.append({"vmid": c.get("vmid"), "hostname": c.get("hostname"), "type": _type_of(c) or None,
                    "status": c.get("status"), "onair": bool(a), "air": a})
    out.sort(key=lambda c: (not c["onair"], (c["air"] or {}).get("kind") != "output",
                            c["status"] != "running", (c["hostname"] or "").lower()))
    return out


@bp.route("/api/nodes/<int:node_id>/power-check", methods=["GET"])
@require_perm("settings.edit")
def api_node_power_check(node_id):
    from .. import node_power, container_net
    node, err = _node_or_404(node_id)
    if err:
        return err
    body = node_power.check(node)
    body.update(ok=True, checked_at=time.time(), name=node.get("name"),
                all_in_one=container_net.est_tout_en_un(node),
                bmc_configured=bool((node.get("ilo_host") or "").strip()),
                containers=_ordered_containers(node_id))
    return jsonify(body)
