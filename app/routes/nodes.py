# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Nœuds (cluster multi-hôte) — CRUD, GPU, images vers les nœuds, agent-nœud sans SSH."""

import logging
import re
import threading

from flask import jsonify, request

from . import bp
from .images import (_IMAGES, _img_lock, _node_img_build, _node_img_push, _image_tag, _stage_tar,
                     _image_present, _image_present_node, _repo_root, _ssh_bin,
                     _provision_shared_images, _node_image_inventory, _used_tag,
                     node_images_state)
from ..auth import require_perm, require_login
from ..database import (db_get_nodes, db_get_node, db_get_containers, db_add_node,
                        db_update_node, db_add_alert, db_delete_node)

log = logging.getLogger(__name__)


@bp.route("/api/nodes", methods=["GET"])
@require_login
def api_list_nodes():
    nodes = db_get_nodes()
    # Enrichit : présence de l'image + nombre de conteneurs rattachés + pool de cœurs (pinning).
    from .. import docker_driver, core_pool, node_health, settings as _st, node_driver as _nd
    import time as _t
    # Auto-réparation : nœud enrôlé mais register raté (capacités DB vides) → ré-enregistrer + relire.
    def _reenregistrer(n):
        try:
            if _nd.ensure_registered(n):
                fresh_n = db_get_node(n["id"])
                if fresh_n:
                    n.update(fresh_n)
        except Exception:
            pass
    if nodes:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=min(len(nodes), 8)) as _ex0:
            list(_ex0.map(_reenregistrer, nodes))
    conts = db_get_containers()
    # Voyant online : dernier snapshot node_health (sampler /v1/health toutes les ~5 s) — pas de ping
    # supplémentaire ici. online = snapshot frais (< 4× l'intervalle) avec ok=True.
    snaps = (node_health.latest() or {}).get("nodes", {}) or {}
    interval = float(_st.get("node_health_interval_s") or 5)
    fresh = max(20.0, 4 * interval)
    _now = _t.time()

    # Enrichissement PAR NŒUD, exécuté EN PARALLÈLE (cf. plus bas). Le corps ne touche que `n` :
    # aucun état partagé en écriture, donc rien à sérialiser. Séquentiellement, chaque nœud
    # enchaîne plusieurs allers-retours vers son agent, et un nœud injoignable fait attendre son
    # timeout à TOUS les suivants — 16 host_exec en série, 10 s pour cinq nœuds (mesuré
    # 2026-08-19, r620-1 down). En parallèle, le coût est celui du nœud le plus lent.
    def _enrichir(n):
        snap = snaps.get(str(n["id"]))
        age = (_now - snap.get("ts", 0)) if snap else None
        n["online"] = bool(snap and snap.get("ok") and age is not None and age < fresh)
        n["last_seen_s"] = round(age) if age is not None else None
        n["containers"] = sum(1 for c in conts if c.get("node_id") == n["id"])
        try:
            n["cores_pool"] = core_pool.cores_status(n["id"])
        except Exception:
            n["cores_pool"] = None
        # ── Charge MESURÉE, à côté de la comptabilité du pool ──────────────────────────────────
        # `cores_pool` compte des RÉSERVATIONS (une intention), pas de la charge : un pool à moitié
        # libre sur un nœud déjà saturé se lit « il reste de la place ». On expose donc aussi le
        # dernier relevé — pris dans `snaps`, DÉJÀ en main : aucune sonde supplémentaire.
        # On préfère la charge des cœurs ORDONNANÇABLES seuls (`cpu_partage`) : sur un nœud très
        # isolé, le pourcentage machine moyenne la bande busy-poll avec le reste et reste au vert
        # pendant la saturation (cf. node_health._merge_cpu_partage). `scope` dit lequel des deux
        # est servi — jamais un chiffre dont on ignore la portée.
        _res = (snap or {}).get("resources") or {}
        _ord = ((snap or {}).get("cpu_partage") or {}).get("ordonnancables") or {}
        if _ord.get("pct") is not None:
            n["cpu_pct"], n["cpu_pct_scope"] = _ord["pct"], "ordonnancables"
        else:
            _c = _res.get("cpu_pct_real")
            if _c is None:
                _c = _res.get("cpu_pct")
            n["cpu_pct"], n["cpu_pct_scope"] = _c, ("machine" if _c is not None else None)
        n["mem_pct"] = (round((_res.get("mem_used_mb") or 0) / _res["mem_total_mb"] * 100, 1)
                        if _res.get("mem_total_mb") else None)
        # GPU : lecture DB seule (table node_gpu_alloc), pas de sonde nœud.
        try:
            from .. import gpu_pool
            n["gpu_pool"] = gpu_pool.gpu_status(n["id"])
        except Exception:
            n["gpu_pool"] = None
        # Présence PAR IMAGE (compute/media/webrtc/mtl…) → la palette ne grise un nœud que si
        # l'image du TYPE choisi y manque (cf. plugins.image_kind), au lieu d'exiger bobi-mtl partout.
        try:
            n["images"] = node_images_state(n)
        except Exception as e:
            n["images"] = {}
            log.warning("node_images_state(%s): %s", n.get("id"), e)
        # Manquantes = ATTENDUES sur ce nœud (capacité déclarée / drapeau matériel) mais absentes.
        # C'est CE champ que le badge de la page Déploiement doit lire : `image_ok` ci-dessous ne
        # parle que de bobi-mtl et criait donc « image absente » sur les nœuds compute/média.
        n["images_missing"] = [{"which": w, "tag": st.get("tag") or ""}
                               for w, st in (n["images"] or {}).items()
                               if st.get("expected") and not st.get("present")]
        mtl = (n["images"] or {}).get("mtl") or {}
        if not mtl.get("expected"):
            # Nœud sans capacité io2110 : bobi-mtl n'a rien à y faire → pas de sonde ssh inutile.
            n["image_ok"] = True
            n["image_msg"] = ""
        else:
            try:
                ok, msg = docker_driver.verify_image(n)
                n["image_ok"] = ok        # rétro-compat : présence de bobi-mtl UNIQUEMENT (moteur 2110)
                n["image_msg"] = msg
            except Exception as e:
                n["image_ok"] = False
                n["image_msg"] = str(e)
        # Flux d'enrôlement (page Déploiement) : un nœud « en attente » = jeton créé, pas encore
        # consommé (status=pending + enroll_token non vide). enroll_token reste exposé (l'UI l'affiche
        # pour l'install manuelle/USB), mais on NE FUITE JAMAIS le mot de passe iLO au navigateur.
        n["pending_enroll"] = (n.get("status") == "pending" and bool(n.get("enroll_token")))
        n.pop("ilo_password", None)

    if nodes:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(nodes), 8)) as _ex:
            for _f in [_ex.submit(_enrichir, _n) for _n in nodes]:
                try:
                    _f.result()
                except Exception as e:                                     # noqa: BLE001
                    log.warning("enrichissement d'un nœud: %s", e)
    return jsonify({"nodes": nodes, "agent_bundled": _bundled_agent_version()})

@bp.route("/api/nodes", methods=["POST"])
@require_perm("settings.edit")
def api_add_node():
    d = request.json or {}
    if not d.get("name") or not d.get("host"):
        return jsonify({"error": "name et host requis"}), 400
    node_id = db_add_node(
        name=d["name"], host=d["host"], kind=d.get("kind", "docker"),
        mtl_iface=d.get("mtl_iface"), mtl_capable=d.get("mtl_capable", 0),
        lcores=d.get("lcores"), image=d.get("image"),
        mxl_mount=d.get("mxl_mount") or "/dev/shm", ram_mb=d.get("ram_mb"),
        docker_network=d.get("docker_network"), compute_image=d.get("compute_image"),
        compute_cpuset=d.get("compute_cpuset"),
        media_image=d.get("media_image"), media_mount=d.get("media_mount"))
    db_add_alert("alert.node.ajoute", "info", kind="node",
                 params={"n": d["name"], "host": d["host"]})
    return jsonify({"status": "ok", "id": node_id})

@bp.route("/api/nodes/register", methods=["POST"])
@require_perm("settings.edit")
def api_register_node():
    """Enregistre un nœud via son agent (bobi-node-agent) : sonde /v1/capabilities et upsert la
    ligne `nodes` (capacités mappées). Remplace la saisie manuelle des images/réseau. Cf.
    node_driver.register / NODE_AGENT.md."""
    from .. import node_driver
    d = request.json or {}
    host = (d.get("host") or "").strip()
    if not host or not d.get("token"):
        return jsonify({"ok": False, "error": "host et token requis"}), 400
    try:
        port = int(d.get("port") or 9100)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "port invalide"}), 400
    ok, res = node_driver.register(host, port, d["token"], name=(d.get("name") or "").strip() or None)
    return (jsonify({"ok": True, "node": res}) if ok
            else (jsonify({"ok": False, "error": res}), 502))

@bp.route("/api/nodes/<int:node_id>/push_image", methods=["POST"])
@require_perm("settings.edit")
def api_node_push_image(node_id):
    """Pousse une image (buildée côté contrôleur) vers le nœud via son agent (docker save→load).
    Asynchrone (2 Go ≈ minutes) → thread + alerte. Cf. node_driver.push_image."""
    from .. import node_driver
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    if not node_driver.has_agent(node):
        return jsonify({"ok": False, "error": "nœud sans agent (pas d'agent_url)"}), 400
    tag = ((request.json or {}).get("image") or "").strip()
    if not tag:
        return jsonify({"ok": False, "error": "image requise"}), 400
    def _run():
        ok, msg = node_driver.push_image(node, tag)
        db_add_alert("alert.prep.push_image", "info" if ok else "warning",
                     node_id=node.get("id"), kind="prep",
                     params={"tag": tag, "n": node["name"], "r": "OK" if ok else msg})
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "status": "push en cours"})

@bp.route("/api/nodes/<int:node_id>/build-image", methods=["POST"])
@require_perm("settings.edit")
def api_node_build_image(node_id):
    """Build une image node-only (ex. bobi-mtl) DIRECTEMENT sur le nœud via l'agent (POST contexte tar
    → /v1/host/images/build). Réservé aux images `node_only` (bobi-mtl : clone MTL + E810 sur la cible).
    compute/media sont des images CLUSTER (onglet Build → poussées). Asynchrone (minutes) → thread +
    état pollable + alerte. Réutilise `_image_tag`/`_stage_tar`/`node_driver.build_image`."""
    from .. import node_driver
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    if not node_driver.has_agent(node):
        return jsonify({"ok": False, "error": "nœud sans agent (pas d'agent_url)"}), 400
    which = ((request.json or {}).get("which") or "mtl").strip()
    spec = _IMAGES.get(which)
    if not spec or not spec.get("node_only"):
        return jsonify({"ok": False, "error": "image non buildable par-nœud (réservé aux images node-only)"}), 400
    key = "%d:%s" % (node_id, which)
    import time as _t
    _start = _t.time()
    with _img_lock:
        if _node_img_build.get(key, {}).get("status") == "building":
            return jsonify({"ok": True, "status": "build déjà en cours"})
        _node_img_build[key] = {"status": "building", "msg": "préparation du contexte…", "start": _start}
    tag = _image_tag(which)
    def _run():
        ok, tail = False, ""
        try:
            ctx = _stage_tar(which)
            with _img_lock:
                _node_img_build[key] = {"status": "building", "msg": "build sur le nœud (agent)…", "start": _start}
            rc, tail = node_driver.build_image(node, tag, ctx, timeout=2400)
            ok = (rc == 0)
            # Anti-faux-ok : un rc=0 ne suffit pas — l'image DOIT être réellement présente sur le
            # nœud (l'inventaire agent pilote le badge « absent », pas la DB). Sans cette vérif, un
            # « ok » fantôme re-rend le panneau en « Builder/absent » (symptôme « ça recharge / rien »).
            # Présence via l'INVENTAIRE AGENT (pas ssh) : un nœud enrôlé sans root-SSH répondait
            # toujours « absente » → build réussi rapporté en faux-échec. Repli ssh sans agent.
            _vu = (_image_present_node(node, tag, force=True) if node.get("agent_url")
                   else _image_present(node.get("host"), tag))
            if ok and not _vu:
                ok = False
                tail = "build rc=0 mais image %s introuvable sur le nœud (faux-ok). %s" % (tag, str(tail)[-400:])
        except Exception as e:
            tail = str(e)
        dur = int(_t.time() - _start)
        if ok:
            # Le tag node-only est enregistré sur le NŒUD buildé uniquement (pas d'autofill flotte) :
            # via le `field` du spec (mtl→`image`, compute-gpu→`compute_gpu_image`). Une image
            # node_only est locale au nœud (GPU/E810) → ne pas la propager aux nœuds sans la capacité.
            db_update_node(node_id, **{spec["field"]: tag})
            msg = "%s buildée sur %s en %dm%02ds" % (tag, node.get("name"), dur // 60, dur % 60)
        else:
            msg = "échec build %s : %s" % (tag, str(tail)[-600:])
            log.warning("build %s sur %s (id=%s) : %s", tag, node.get("name"), node_id, msg)
        with _img_lock:
            _node_img_build[key] = {"status": "ok" if ok else "error", "msg": msg}
        db_add_alert("alert.prep.build_ok" if ok else "alert.prep.build_echec",
                     "info" if ok else "warning", node_id=node.get("id"), kind="prep",
                     params=({"tag": tag, "n": node.get("name")} if ok
                             else {"tag": tag, "n": node.get("name"), "msg": msg}))
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "status": "build en cours", "tag": tag})

def _node_build_status(node_id, which="mtl"):
    """État courant d'un build node-only (status idle|building|ok|error, msg, + elapsed si en cours)."""
    import time as _t
    with _img_lock:
        st = dict(_node_img_build.get("%d:%s" % (node_id, which)) or {"status": "idle", "msg": ""})
    if st.get("status") == "building" and st.get("start"):
        st["elapsed"] = int(_t.time() - st["start"])
    st.pop("start", None)
    return st

@bp.route("/api/nodes/<int:node_id>/build-image/status", methods=["GET"])
@require_login
def api_node_build_image_status(node_id):
    which = (request.args.get("which") or "mtl").strip()
    return jsonify(_node_build_status(node_id, which))

def _probe_gpu(host):
    """Probe GPU NVIDIA d'un nœud (orchestrateur, via ssh) : (gpu_capable, gpu_count, runtime_ok, names).
    GPU-capable ⇔ nvidia-smi liste ≥1 GPU ET le runtime `nvidia` est présent dans Docker (sinon
    --gpus échouerait au run). Détection sans dépendre d'une nouvelle version d'agent."""
    # _ssh_bin renvoie des BYTES (text=False) → décoder avant tout traitement str (sinon
    # names=[b'…'] et "nvidia" in bytes lève → runtime_ok=False, détection ratée à tort).
    names = []
    try:
        rc, out = _ssh_bin(host, "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null", timeout=20)
        if rc == 0:
            names = [l.strip() for l in (out or b"").decode("utf-8", "replace").splitlines() if l.strip()]
    except Exception:
        pass
    runtime_ok = False
    try:
        rc, out = _ssh_bin(host, "docker info --format '{{.Runtimes}}' 2>/dev/null", timeout=20)
        runtime_ok = (rc == 0 and "nvidia" in (out or b"").decode("utf-8", "replace").lower())
    except Exception:
        pass
    # ★ CUDA RÉELLEMENT UTILISABLE : nvidia-smi peut lister le GPU (via libnvidia-ml) alors que la
    # lib driver CUDA userspace (libcuda1 / libcuda.so.1) MANQUE → CUDA totalement cassé, tout cupy
    # échoue (cudaErrorInsufficientDriver), un mur GPU retombe en numpy CPU EN SILENCE. Symptôme
    # précis et fiable : nvidia-smi affiche « CUDA Version: N/A / Not Found » au lieu d'un numéro.
    # Sans cette vérif, gpu_capable=1 pouvait MENTIR (bug vécu sur dell-1). On l'exige pour le vert.
    cuda_ok = False
    try:
        rc, out = _ssh_bin(host, "nvidia-smi -q 2>/dev/null | grep -i 'cuda version'", timeout=20)
        cuda_ok = bool(re.search(r"cuda version\s*:\s*\d", (out or b"").decode("utf-8", "replace"), re.I))
    except Exception:
        pass
    cap = bool(names) and runtime_ok and cuda_ok
    return cap, len(names), runtime_ok, names, cuda_ok

@bp.route("/api/nodes/<int:node_id>/detect-gpu", methods=["POST"])
@require_perm("settings.edit")
def api_node_detect_gpu(node_id):
    """Détecte le GPU NVIDIA du nœud (nvidia-smi + runtime nvidia Docker) et met à jour
    nodes.gpu_capable/gpu_count. La présence de l'image compute_gpu_image (build node-only) reste
    requise en plus pour qu'un plugin GPU s'y déploie réellement (cf. docker_compute)."""
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    if not node.get("host"):
        return jsonify({"ok": False, "error": "nœud sans hôte (host)"}), 400
    cap, n, runtime_ok, names, cuda_ok = _probe_gpu(node["host"])
    db_update_node(node_id, gpu_capable=1 if cap else 0, gpu_count=(n or None))
    if cap:
        msg = f"{n} GPU détecté(s) ({', '.join(names)}) + runtime nvidia + CUDA OK"
    elif names and not runtime_ok:
        msg = "GPU(s) vus mais runtime nvidia Docker absent"
    elif names and not cuda_ok:
        # Le piège silencieux : GPU + runtime OK mais CUDA userspace cassé (libcuda1 manquant / driver
        # trop vieux). On REFUSE gpu_capable pour ne pas router de murs GPU qui tomberaient en CPU muet.
        msg = ("GPU(s) vus + runtime nvidia OK mais CUDA INUTILISABLE (nvidia-smi « CUDA Version: N/A ») "
               "— libcuda1 manquant ou driver trop vieux. Nœud NON marqué GPU-ready.")
    else:
        msg = "aucun GPU NVIDIA détecté"
    db_add_alert("alert.node.detection_gpu", "info" if cap else "warning", node_id=node_id, kind="node",
                 params={"n": node.get("name"), "msg": msg})
    return jsonify({"ok": True, "gpu_capable": cap, "gpu_count": n,
                    "runtime_ok": runtime_ok, "cuda_ok": cuda_ok, "gpus": names, "msg": msg})

# ─── Rattrapage de capacité (capacité oubliée à l'enrôlement) ──────────────────────────────────
# Une capacité oubliée au moment du profil d'enrôlement n'est PAS rattrapable depuis l'UI aujourd'hui :
# le profil est consommé une fois, et `nodes.capabilities` n'est réécrite que par `node_driver.register`.
# Or une liste périmée a des conséquences MUETTES : node_health ne sonde pas le PTP d'un nœud sans
# "io2110" déclaré, images._node_needs_image ne le compte pas comme cible de build, node_recovery en
# dépend. D'où ce rattrapage : provisionner l'hôte + fusionner la capacité côté agent + resynchroniser
# la base. Les CINQ capacités sont rattrapables (cf. install-node.sh --add-caps).
_CAP_ADDABLE = ("gpu", "io2110", "compute", "media", "webrtc")
# Capacités dont le provisioning n'est effectif qu'après redémarrage du nœud : `gpu` (module DKMS à
# charger), `io2110` (noyau MTL, cmdline hugepages 1G, DDP relu au probe du driver ice).
_CAP_NEEDS_REBOOT = ("gpu", "io2110")
# Blob DDP Intel E810 : vendoré dans le dépôt, mais il ne voyage PAS avec le script quand celui-ci est
# pipé sur stdin. Sans lui, `ice` démarre en Safe Mode (pas d'horloge PTP matérielle, pas de steering)
# → capacité io2110 déclarée mais inopérante. On le dépose donc séparément avant de lancer le script.
_DDP_REL = ("node_agent", "firmware", "ice", "ice_comms-1.3.63.0.pkg")
_DDP_DEST = "/tmp/bobi-ice-ddp.pkg"
_cap_lock = threading.Lock()
_cap_jobs = {}          # node_id -> {status: running|done|error, msg, log, reboot_required}


def _add_cap_args(node, caps):
    """Arguments `install-node.sh` amenés par les capacités demandées, reconstruits depuis la base.

    Une capacité ajoutée après coup a besoin de SES réglages : sans `--mtl-iface`, io2110 pose bien le
    noyau/les hugepages mais laisse le PTP non configuré ; sans `--kernel-pkg`, le nœud garde un noyau
    dont rien ne garantit qu'il fasse tourner MTL. On ne passe que ce qu'on sait : une valeur absente
    de la base est OMISE (le script la traite alors en mode différé), jamais devinée."""
    import json as _json
    from .. import settings as _st
    args, notes = [], []
    try:
        prof = _json.loads(node.get("enroll_profile") or "{}")
    except (TypeError, ValueError):
        prof = {}
    if "io2110" in caps:
        if node.get("mtl_iface"):
            args += ["--mtl-iface", str(node["mtl_iface"])]
        else:
            notes.append("carte E810 inconnue en base → PTP/binding NIC différés : à choisir dans "
                         "Configuration du nœud après le rattrapage")
        if node.get("lcores"):
            args += ["--lcores", str(node["lcores"])]
        if prof.get("hugepages"):
            args += ["--hugepages", str(prof["hugepages"])]
        if prof.get("ptp_domain") is not None:
            args += ["--ptp-domain", str(prof["ptp_domain"])]
        # Noyau MTL : réglage CLUSTER (jamais figé dans le script) — même source que l'enrôlement.
        kpkg, kapt = _st.get("io2110_kernel_pkg") or "", _st.get("io2110_kernel_apt") or ""
        if kpkg:
            args += ["--kernel-pkg", kpkg]
            if kapt:
                args += ["--kernel-apt", kapt]
        else:
            notes.append("aucun noyau MTL épinglé (réglage cluster vide) → le nœud gardera son noyau "
                         "courant, dont la compatibilité MTL n'est pas garantie")
    if "media" in caps and node.get("media_mount"):
        args += ["--media-mount", str(node["media_mount"])]
    return args, notes


def _add_cap_run(node_id, caps):
    """Exécute le rattrapage sur le nœud : pipe `install-node.sh --add-caps <caps>` sur stdin de
    /v1/host/exec (donc AUCUNE dépendance à un payload présent sur le nœud), puis resynchronise la
    base depuis `/v1/capabilities`. Le code de retour du script PORTE le résultat du provisioning
    hôte (best-effort mais drapeauté côté script) — on ne l'écrase jamais par un « ✓ »."""
    import base64
    import os
    import shlex
    from .. import node_driver
    node = db_get_node(node_id)
    csv = ",".join(caps)

    def _set(**kw):
        with _cap_lock:
            _cap_jobs.setdefault(node_id, {}).update(kw)

    try:
        with open(os.path.join(_repo_root(), "node_agent", "install-node.sh")) as f:
            script = f.read()
    except OSError as e:
        _set(status="error", msg=f"install-node.sh illisible côté contrôleur : {e}")
        return

    args, notes = _add_cap_args(node, caps)
    env = ""
    # DDP E810 : déposé AVANT le script, sinon io2110 serait provisionné avec un `ice` en Safe Mode.
    # Échec du dépôt → on renonce plutôt que de poser une capacité 2110 muette (le script la
    # drapeauterait, mais autant ne pas modifier l'hôte du tout).
    if "io2110" in caps:
        try:
            with open(os.path.join(_repo_root(), *_DDP_REL), "rb") as f:
                blob = base64.b64encode(f.read()).decode()
        except OSError as e:
            _set(status="error", msg=f"blob DDP E810 illisible côté contrôleur ({e}) — rattrapage "
                                     "io2110 annulé (sans DDP, le driver ice démarre en Safe Mode).")
            return
        rc_d, _o, e_d = node_driver.host_exec(
            node, f"base64 -d > {shlex.quote(_DDP_DEST)}", input_data=blob, timeout=180)
        if rc_d != 0:
            _set(status="error", msg=f"dépôt du blob DDP E810 sur le nœud échoué (rc={rc_d}, {e_d}) "
                                     "— rattrapage io2110 annulé, l'hôte n'a pas été modifié.")
            return
        env = f"DDP_SRC={shlex.quote(_DDP_DEST)} "

    # `bash -s -- <args>` : le script vient de stdin, $0 vaut "bash" (SCRIPT_DIR inutilisé en
    # --add-caps). Timeout large : NVIDIA compile un module DKMS, io2110 installe un noyau (minutes).
    cmd = env + "bash -s -- --add-caps " + shlex.quote(csv)
    if args:
        cmd += " " + " ".join(shlex.quote(a) for a in args)
    rc, out, err = node_driver.host_exec(node, cmd, input_data=script, timeout=1800)
    journal = ((out or "") + ("\n" + err if err else "")).strip()
    if "io2110" in caps:
        node_driver.host_exec(node, f"rm -f {shlex.quote(_DDP_DEST)}", timeout=30)

    # Resync de la base depuis l'agent, MÊME en cas d'échec du provisioning : le script fusionne la
    # capacité dans config.json avant de sortir en erreur, donc la déclaration a pu changer et la base
    # doit refléter ce que le nœud annonce réellement (pas ce qu'on espérait).
    resync = ""
    try:
        port = int((node.get("agent_url") or "").rsplit(":", 1)[-1] or 9100)
    except (TypeError, ValueError):
        port = 9100
    if not node.get("host"):
        resync = " — ⚠ nœud sans `host` : capacités non resynchronisées en base."
        fresh = node
    else:
        ok_reg, res = node_driver.register(node["host"], port, node.get("agent_token") or "", node=node)
        # L'ajout d'une capacité RELANCE l'agent (il doit relire config.json) : il est donc
        # normalement indisponible une poignée de secondes, juste au moment où l'on vient lire ses
        # capacités. Conclure « resync échouée » sur ce premier refus laisse la base en retard sur
        # le nœud — la capacité est active là-bas, invisible ici, donc jamais utilisée. On retente
        # au-delà de la fenêtre du disjoncteur de transport (5 s) avant de renoncer.
        if not ok_reg:
            import time as _tps
            for _essai in range(3):
                _tps.sleep(6)
                ok_reg, res = node_driver.register(node["host"], port,
                                                   node.get("agent_token") or "", node=node)
                if ok_reg:
                    log.info("resync capacités node=%s : obtenue au %de essai (agent en redémarrage)",
                             node_id, _essai + 2)
                    break
        # La NATURE du nœud vient peut-être de changer (ajout d'`io2110`) : le pool doit suivre,
        # sinon des conteneurs de calcul se poseraient sur les lcores busy-poll du moteur. Ne touche
        # rien si l'opérateur a réglé le pool à la main.
        if ok_reg:
            try:
                from .. import core_pool
                frais_n = db_get_node(node_id) or node
                carte = core_pool.read_cpu_core_map(frais_n) or {}
                if carte:
                    core_pool.rederiver_si_auto(node_id, len(carte), core_of=carte)
            except Exception as e:
                log.warning("re-dérivation du pool node=%s: %s", node_id, e)
        if not ok_reg:
            resync = f" — ⚠ resync des capacités échouée ({res}) : la base peut être en retard sur le nœud."
        fresh = db_get_node(node_id) or node

    # Suites : les images runtime des capacités macvlan sont PARTAGÉES (buildées une fois, poussées).
    # Elles se poussent depuis les capacités RELUES (`_provision_shared_images` lit la colonne, d'où
    # l'appel APRÈS le resync) — sans elles, la capacité est déclarée mais aucun conteneur ne démarre.
    suites = list(notes)
    if rc == 0 and set(caps) & {"compute", "media", "webrtc"}:
        try:
            n_ok, n_fail = _provision_shared_images(fresh)
            if n_fail:
                suites.append(f"{n_ok} image(s) poussée(s), {n_fail} en échec — voir l'onglet Build")
            elif n_ok:
                suites.append(f"{n_ok} image(s) runtime poussée(s) sur le nœud")
            else:
                suites.append("aucune image poussée (pas encore buildée côté contrôleur, ou ce nœud "
                              "EST l'hôte de build) — vérifier l'onglet Build")
        except Exception as e:                                 # noqa: BLE001 (best-effort, jamais bloquant)
            log.warning("provision images après rattrapage %s: %s", node_id, e)
            suites.append(f"push des images non effectué ({e}) — le faire depuis l'onglet Build")
    if set(caps) & {"compute", "media", "webrtc"} and not fresh.get("docker_network"):
        suites.append("réseau containers non configuré sur ce nœud → Configuration → Réseau "
                      "(la carte parente n'est pas déductible, elle doit être choisie)")
    reboot = bool(set(caps) & set(_CAP_NEEDS_REBOOT))
    if reboot:
        suites.append("REBOOT du nœud requis pour que la/les capacité(s) soient effectives"
                      + (", puis « Détecter GPU »" if "gpu" in caps else ""))
    queue = (" Suites : " + " · ".join(suites) + ".") if suites else ""

    if rc == 0:
        # `msg` alimente AUSSI `_set()` (état pollable relu par l'UI de rattrapage) : il reste EN
        # FRANÇAIS, inchangé. L'alerte, elle, est composée à part depuis une clé i18n — `resync`/
        # `queue` sont eux-mêmes des phrases FRANÇAISES composées de fragments (pas des données),
        # donc pas rejouées ici ; seul le fait « reboot requis » (déjà un booléen) est repris.
        msg = f"Capacité(s) « {csv} » ajoutée(s) sur {node.get('name')}." + resync + queue
        _set(status="done", msg=msg, log=journal, reboot_required=reboot)
        cle = "alert.node.capacites_ajoutees_reboot" if reboot else "alert.node.capacites_ajoutees"
        db_add_alert(cle, "info", node_id=node_id, kind="node",
                     params={"csv": csv, "n": node.get("name")})
    else:
        # rc 255 = agent injoignable (contrat host_exec) ; sinon échec de provisioning drapeauté par
        # le script. Dans les deux cas la capacité n'est PAS utilisable — le dire, ne pas l'habiller.
        why = "agent injoignable" if rc == 255 else "provisioning hôte en échec"
        msg = (f"Rattrapage « {csv} » sur {node.get('name')} : ÉCHEC ({why}, rc={rc}) — "
               "capacité non utilisable, voir le journal.") + resync + queue
        _set(status="error", msg=msg, log=journal, reboot_required=False)
        cle = ("alert.node.capacites_echec_agent" if rc == 255
               else "alert.node.capacites_echec_provisioning")
        db_add_alert(cle, "warning", node_id=node_id, kind="node",
                     params={"csv": csv, "n": node.get("name"), "rc": rc})


@bp.route("/api/nodes/<int:node_id>/capabilities", methods=["POST"])
@require_perm("settings.edit")
def api_node_add_capability(node_id):
    """Ajoute une ou plusieurs capacités à un nœud DÉJÀ enrôlé (rattrapage d'un oubli d'enrôlement).
    Asynchrone (NVIDIA compile un module DKMS, io2110 installe un noyau) → thread + état pollable
    par le GET.

    N'accepte QUE l'ajout : retirer une capacité n'est pas symétrique (on ne désinstalle pas un
    pilote, on ne rend pas des hugepages, on ne détruit pas un macvlan) — un « décocher » qui ne
    ferait qu'éditer la déclaration laisserait la base MENTIR sur l'état de l'hôte."""
    from .. import node_driver
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    if not node_driver.has_agent(node):
        return jsonify({"ok": False, "error": "nœud sans agent (pas d'agent_url)"}), 400
    asked = (request.json or {}).get("add") or []
    if isinstance(asked, str):
        asked = [asked]
    caps = [str(c).strip() for c in asked if str(c).strip()]
    if not caps:
        return jsonify({"ok": False, "error": "aucune capacité à ajouter"}), 400
    refus = [c for c in caps if c not in _CAP_ADDABLE]
    if refus:
        return jsonify({"ok": False, "error": "capacité inconnue : %s (attendu : %s)"
                        % (", ".join(refus), ", ".join(_CAP_ADDABLE))}), 400
    already = node_driver.node_capabilities(node)
    caps = [c for c in caps if c not in already]
    if not caps:
        return jsonify({"ok": True, "status": "déjà déclarée", "noop": True})
    with _cap_lock:
        if (_cap_jobs.get(node_id) or {}).get("status") == "running":
            return jsonify({"ok": True, "status": "rattrapage déjà en cours"})
        _cap_jobs[node_id] = {"status": "running", "caps": caps, "log": "", "reboot_required": False,
                              "msg": "provisioning de « %s » en cours…" % ", ".join(caps)}
    threading.Thread(target=_add_cap_run, args=(node_id, caps), daemon=True).start()
    return jsonify({"ok": True, "status": "rattrapage lancé", "caps": caps})


@bp.route("/api/nodes/<int:node_id>/capabilities", methods=["GET"])
@require_perm("settings.edit")
def api_node_capability_state(node_id):
    """État du dernier rattrapage de capacité (pollé par l'UI) + liste courante déclarée."""
    from .. import node_driver
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    with _cap_lock:
        job = dict(_cap_jobs.get(node_id) or {})
    return jsonify({"ok": True, "capabilities": node_driver.node_capabilities(node),
                    "addable": list(_CAP_ADDABLE), "job": job})


# ─── Mise à jour des images vers les NŒUDS (≠ Flotte = entre orchestrateurs) ────────────────────
_SHARED_IMAGES = ("compute", "media", "webrtc")   # images PARTAGÉES (build-once + push) ; mtl = par-nœud

_IMG_ORDER = ("compute", "compute-gpu", "media", "webrtc", "mtl")

def _node_wants(which, node, caps):
    """Ce nœud exécute-t-il cette image ? (capacités déclarées + flag GPU pour la variante GPU)."""
    if which == "mtl":
        return "io2110" in caps
    if which == "compute-gpu":
        return bool(node.get("gpu_capable"))
    return which in caps

def _img_state(repo_tag, used, present_tags):
    """Croise les TROIS notions (dépôt / présentes sur le nœud / UTILISÉE par le nœud).
      ok           : le nœud utilise le tag du dépôt, et il est bien présent.
      stale_used   : le tag du dépôt EST présent sur le nœud, mais le nœud en utilise un AUTRE
                     → « j'ai buildé et ça ne sert à rien » (le piège : build « ok », zéro effet).
      used_missing : le tag utilisé n'existe PAS sur le nœud → le conteneur ne démarrera pas.
      unset        : aucun tag utilisé renseigné.
      outdated     : le nœud utilise ce qu'il a, mais le dépôt propose plus récent (pas encore buildé/poussé).
      absent       : rien de cette famille sur le nœud."""
    if used and used not in present_tags:
        return "used_missing"
    if not used:
        return "absent" if not present_tags else "unset"
    if used == repo_tag:
        return "ok"
    if repo_tag in present_tags:
        return "stale_used"
    return "outdated"

@bp.route("/api/nodes/images-status", methods=["GET"])
@require_login
def api_nodes_images_status():
    """État des images runtime par nœud, sur les TROIS notions distinctes :
      · `repo`    — la version que le DÉPÔT propose au build (meta.json) ;
      · `present` — les versions RÉELLEMENT installées sur le nœud (`docker images` via l'agent) ;
      · `used`    — la version que le nœud UTILISE (`nodes.<field>`, ou setting pour webrtc).
    `?refresh=1` force la ré-énumération (sinon cache 20 s). Lecture seule."""
    from .. import node_driver as _nd
    force = request.args.get("refresh") in ("1", "true")
    expected = {w: _image_tag(w) for w in _IMG_ORDER}
    rows = []
    for n in db_get_nodes():
        caps = _nd.node_capabilities(n)
        has_agent = bool((n.get("agent_url") or "").strip())
        inv, inv_err = _node_image_inventory(n, force=force) if has_agent else ([], "nœud sans agent")
        imgs = {}
        for w in _IMG_ORDER:
            if not _node_wants(w, n, caps):
                continue
            prefix = _IMAGES[w]["prefix"] + ":"
            present = [i for i in inv if i["tag"].startswith(prefix)]
            ptags = [i["tag"] for i in present]
            repo_tag = expected[w]
            used = _used_tag(w, n)
            # Repli : l'agent n'a pas répondu → on ne SAIT pas ce qui est présent. On le dit
            # (`unknown`) au lieu de prétendre « absent » (anti-patron : l'échec silencieux).
            state = "unknown" if inv_err else _img_state(repo_tag, used, ptags)
            imgs[w] = {"label": _IMAGES[w]["label"], "repo": repo_tag, "used": used,
                       "present": present, "newest": ptags[0] if ptags else "",
                       "repo_present": repo_tag in ptags, "state": state,
                       "shared": w in _SHARED_IMAGES,
                       "global": bool(_IMAGES[w].get("setting"))}
        rows.append({"id": n["id"], "name": n.get("name"), "host": n.get("host"),
                     "has_agent": has_agent, "inventory_error": inv_err,
                     "capabilities": caps, "gpu_capable": bool(n.get("gpu_capable")),
                     "images": imgs})
    return jsonify({"nodes": rows, "expected": expected, "order": list(_IMG_ORDER)})

@bp.route("/api/nodes/<int:node_id>/images/use", methods=["POST"])
@require_perm("settings.edit")
def api_node_image_use(node_id):
    """Bascule le nœud sur une version PRÉSENTE de l'image (écrit `nodes.<field>`, ou le setting
    commun pour webrtc). C'est l'action qui manquait : builder une image ne suffisait pas, il
    fallait éditer la base à la main. Refuse un tag absent du nœud (image fantôme)."""
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    body = request.json or {}
    which, tag = (body.get("which") or ""), (body.get("tag") or "").strip()
    if which not in _IMAGES:
        return jsonify({"ok": False, "error": "image inconnue"}), 400
    if not tag:
        return jsonify({"ok": False, "error": "tag manquant"}), 400
    inv, err = _node_image_inventory(node, force=True)
    if err:
        return jsonify({"ok": False, "error": "inventaire du nœud indisponible : %s" % err}), 502
    if tag not in [i["tag"] for i in inv]:
        return jsonify({"ok": False, "error": "tag absent du nœud : %s" % tag}), 400
    spec = _IMAGES[which]
    if spec.get("setting"):
        from .. import settings as _st
        _st.set(spec["setting"], tag)
    else:
        db_update_node(node_id, **{spec["field"]: tag})
    db_add_alert("alert.prep.image_defaut", "info", node_id=node_id, kind="prep",
                 params={"which": which, "n": node.get("name"), "tag": tag})
    return jsonify({"ok": True, "used": tag})

@bp.route("/api/nodes/<int:node_id>/provision-images", methods=["POST"])
@require_perm("settings.edit")
def api_node_provision_images(node_id):
    """Pousse (à la demande) les images PARTAGÉES requises sur UN nœud, depuis l'hôte de build.
    Asynchrone (push 2 Go ≈ minutes) → thread + état POLLABLE + alerte.

    L'état pollable n'est pas un ornement : rendre la main en 20 ms sur une opération de plusieurs
    minutes, sans rien à interroger ensuite, se lit comme un bouton mort — l'utilisateur reclique, et
    lance un second transfert de 2 Go. D'où aussi le verrou : un push déjà en cours n'est pas doublé."""
    import time as _t
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    if not (node.get("agent_url") or "").strip():
        return jsonify({"ok": False, "error": "nœud sans agent"}), 400
    with _img_lock:
        if _node_img_push.get(node_id, {}).get("status") == "pushing":
            return jsonify({"ok": True, "status": "push déjà en cours", "already": True})
        _node_img_push[node_id] = {"status": "pushing", "msg": "préparation…", "start": _t.time()}
    def _avance(txt):
        with _img_lock:
            st = _node_img_push.get(node_id)
            if st and st.get("status") == "pushing":
                st["msg"] = txt
    def _run():
        try:
            n_ok, n_fail = _provision_shared_images(node, progress=_avance)
            msg = "%d image(s) chargée(s), %d échec" % (n_ok, n_fail) if n_fail else (
                  "%d image(s) chargée(s)" % n_ok if n_ok else
                  "aucune image à pousser (déjà à jour, pas encore buildée, ou nœud de build)")
            etat = "error" if n_fail else "ok"
        except Exception as e:                        # un thread qui meurt laisserait « pushing » à vie
            n_ok, n_fail, msg, etat = 0, 1, str(e), "error"
        with _img_lock:
            _node_img_push[node_id] = {"status": etat, "msg": msg}
        db_add_alert("alert.prep.images_push", "info" if not n_fail else "warning",
                     node_id=node.get("id"), kind="prep",
                     params={"n": node.get("name"), "ok": n_ok, "fail": n_fail})
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "status": "push en cours"})

@bp.route("/api/nodes/<int:node_id>/provision-images/status", methods=["GET"])
@require_login
def api_node_provision_images_status(node_id):
    """État du push d'images partagées (idle|pushing|ok|error, msg, + elapsed si en cours)."""
    import time as _t
    with _img_lock:
        st = dict(_node_img_push.get(node_id) or {"status": "idle", "msg": ""})
    if st.get("status") == "pushing" and st.get("start"):
        st["elapsed"] = int(_t.time() - st["start"])
    st.pop("start", None)
    return jsonify(st)

@bp.route("/api/nodes/sync-images", methods=["POST"])
@require_perm("settings.edit")
def api_nodes_sync_images():
    """Synchronise les images PARTAGÉES sur TOUS les nœuds (depuis l'hôte de build). Async + alerte récap."""
    nodes = [n for n in db_get_nodes() if (n.get("agent_url") or "").strip()]
    def _run():
        tot_ok = tot_fail = 0
        for n in nodes:
            o, f = _provision_shared_images(n)
            tot_ok += o
            tot_fail += f
        db_add_alert("alert.prep.images_sync", "info" if not tot_fail else "warning", kind="prep",
                     params={"n": len(nodes), "ok": tot_ok, "fail": tot_fail})
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "status": "synchronisation en cours", "nodes": len(nodes)})

# ─── Mise à jour de l'agent-nœud (bobi-node-agent) sans SSH, via le canal agent ─────────────────
def _bundled_agent_version():
    """Version de l'agent EMBARQUÉE dans ce repo (node_agent/agent.py : `VERSION = "x.y.z"`)."""
    import os, re as _re
    try:
        with open(os.path.join(_repo_root(), "node_agent", "agent.py"), encoding="utf-8") as f:
            for line in f:
                m = _re.match(r'\s*VERSION\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ""

def _update_agent_on_node(node):
    """Pousse `node_agent/agent.py` (repo) sur le nœud via `/v1/host/exec` (stdin) + restart différé.
    Garde-fou : `py_compile` AVANT de remplacer le fichier (un agent cassé ne se déploie pas → l'ancien
    reste actif). Restart via `systemd-run` (hors cgroup de l'agent). (ok, msg)."""
    import os
    from .. import node_driver
    try:
        with open(os.path.join(_repo_root(), "node_agent", "agent.py"), encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return False, "agent.py introuvable côté contrôleur : %s" % e
    cmd = ("set -e; T=/opt/bobi-node-agent/agent.py.new; cat > \"$T\"; "
           "python3 -m py_compile \"$T\"; mv \"$T\" /opt/bobi-node-agent/agent.py; "
           "systemd-run --on-active=2 --quiet systemctl restart bobi-node-agent; echo OK")
    rc, out, err = node_driver.host_exec(node, cmd, input_data=content, timeout=60)
    if rc == 0 and "OK" in (out or ""):
        return True, "agent mis à jour (restart en cours)"
    return False, (((err or out) or "").strip()[:300] or ("rc=%s" % rc))

@bp.route("/api/agent/version", methods=["GET"])
@require_login
def api_agent_version():
    return jsonify({"bundled": _bundled_agent_version()})

@bp.route("/api/nodes/<int:node_id>/update-agent", methods=["POST"])
@require_perm("settings.edit")
def api_node_update_agent(node_id):
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    if not (node.get("agent_url") or "").strip():
        return jsonify({"ok": False, "error": "nœud sans agent"}), 400
    ok, msg = _update_agent_on_node(node)
    db_add_alert("alert.node.maj_agent", "info" if ok else "warning", node_id=node.get("id"), kind="node",
                 params={"n": node.get("name"), "r": "OK" if ok else msg})
    body = {"ok": ok, "version": _bundled_agent_version()}
    body["msg" if ok else "error"] = msg
    return jsonify(body)

@bp.route("/api/nodes/update-agent-all", methods=["POST"])
@require_perm("settings.edit")
def api_nodes_update_agent_all():
    nodes = [n for n in db_get_nodes() if (n.get("agent_url") or "").strip()]
    def _run():
        n_ok = n_fail = 0
        for n in nodes:
            ok, _m = _update_agent_on_node(n)
            n_ok += int(ok)
            n_fail += int(not ok)
        db_add_alert("alert.node.maj_agent_flotte", "info" if not n_fail else "warning", kind="node",
                     params={"ok": n_ok, "fail": n_fail})
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "status": "MAJ en cours", "nodes": len(nodes), "version": _bundled_agent_version()})

@bp.route("/api/nodes/<int:node_id>", methods=["PUT"])
@require_perm("settings.edit")
def api_update_node(node_id):
    if not db_get_node(node_id):
        return jsonify({"error": "nœud introuvable"}), 404
    db_update_node(node_id, **(request.json or {}))
    return jsonify({"status": "ok"})

@bp.route("/api/nodes/<int:node_id>", methods=["DELETE"])
@require_perm("settings.edit")
def api_delete_node(node_id):
    attached = [c for c in db_get_containers() if c.get("node_id") == node_id]
    force = request.args.get("force") in ("1", "true", "yes")
    if attached and not force:
        return jsonify({"error": "des conteneurs sont rattachés à ce nœud",
                        "containers": len(attached)}), 409
    if attached and force:
        # Nœud DÉCOMMISSIONNÉ/injoignable : on PURGE les lignes orphelines en base SANS contacter le
        # nœud (l'agent est mort → detruire_container partirait en timeout et laisserait les lignes).
        # Les vrais conteneurs Docker sont supposés partis avec le nœud. On libère quand même les
        # allocations purement-DB (cœurs/GPU) pour éviter des réservations fantômes.
        from ..database import db_delete_container
        from .. import core_pool, gpu_pool
        for c in attached:
            vmid = c.get("vmid")
            for _mod, _fn in ((core_pool, "release_cores"), (gpu_pool, "release_gpu")):
                try:
                    getattr(_mod, _fn)(vmid)
                except Exception:
                    pass
            try:
                db_delete_container(vmid)
            except Exception:
                pass
        try:
            from services import nmos as _nmos
            _nmos.purge_orphan_resources(dry_run=False)
        except Exception:
            pass
        db_add_alert("alert.node.supprime_force", "warning", node_id=node_id, kind="node",
                     params={"node_id": node_id, "n": len(attached)})
    db_delete_node(node_id)
    return jsonify({"status": "ok", "purged": len(attached) if force else 0})


# ─── Prép host DPDK/vfio déclarative (chantier DPDK, Lot A) ─────────────────────
@bp.route("/api/nodes/<int:node_id>/dpdk-prep", methods=["POST"])
@require_perm("settings.edit")
def api_node_dpdk_prep(node_id):
    """Prép DPDK d'un nœud : IOMMU + hugepages 1G + vfio.conf (mtl.appliquer_node) et,
    si `bdf` est fourni, binding vfio-pci du port média (plan idempotent + persistance boot).

    Body JSON : {dry:bool, bdf:"0000:12:00.0", unbind:bool, hugepages_1g:int}
    (`?dry=1` en query accepté). Mode dry : renvoie {script, checks, etat, reboot_needed}
    SANS RIEN EXÉCUTER de mutant (etat = mtl.verifier_node, lecture seule). Sans dry :
    applique en thread (pattern host-ops) + alerte au résultat. `unbind` = rollback Lot G."""
    from .. import mtl
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    d = request.json or {}
    dry = bool(d.get("dry")) or request.args.get("dry") in ("1", "true", "yes")
    bdf = (d.get("bdf") or "").strip()
    unbind = bool(d.get("unbind"))
    try:
        huge = int(d.get("hugepages_1g"))
    except (TypeError, ValueError):
        huge = 16
    if not 0 <= huge <= 256:
        huge = 16

    script, checks = "", []
    if bdf:
        try:
            plan = mtl.vfio_unbind_plan if unbind else mtl.vfio_bind_plan
            script, checks = plan(node, bdf)
        except mtl.GardeFouVfio as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    if dry:
        etat = mtl.verifier_node(node)
        # Alerte SR-IOV/MMIO (prérequis VF DPDK/narrow) : le noyau a DÉJÀ échoué faute de MMIO →
        # BIOS à régler. Signal SÛR (dmesg), sans dépendance iLO. Levé à la vérif on-demand.
        _sr = (etat or {}).get("sriov") or {}
        if _sr.get("mmio_error"):
            db_add_alert("alert.prep.sriov_mmio", "warning", node_id=node_id, kind="prep",
                         params={"n": node.get("name") or node_id,
                                 "hint": _sr.get("hint") or
                                 "régler le BIOS (PciResourcePadding=High / Above-4G) + reboot"})
        return jsonify({"ok": True, "dry": True, "script": script, "checks": checks,
                        "etat": etat, "reboot_needed": bool(etat.get("reboot_needed"))})

    def _run():
        ok, msg, _reboot = mtl.appliquer_node(node, huge)
        notes = [msg]
        if ok and bdf:
            do = mtl.vfio_unbind_apply if unbind else mtl.vfio_bind_apply
            ok_b, msg_b, _ = do(node, bdf)
            ok = ok and ok_b
            notes.append(msg_b)
        # Verdict OK/ÉCHEC → deux clés complètes (jamais un paramètre, cf. piège n°3) ; `notes` est
        # un diagnostic dynamique renvoyé par `mtl.appliquer_node`/`vfio_*_apply` (pas une phrase
        # figée d'ici) — comme `{e}` ailleurs dans ce fichier, il voyage tel quel en paramètre.
        cle = "alert.prep.dpdk_ok" if ok else "alert.prep.dpdk_echec"
        db_add_alert(cle, "info" if ok else "error", node_id=node.get("id"), kind="prep",
                     params={"n": node["name"], "notes": " ; ".join(notes)})
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "status": "prép DPDK en cours (voir alertes)",
                    "checks": checks, "reboot_needed": True})


@bp.route("/api/nodes/<int:node_id>/sriov-vf", methods=["POST"])
@require_perm("settings.edit")
def api_node_sriov_vf(node_id):
    """Provisionne la VF SR-IOV d'une PF média (chantier narrow, cf. docs/chantiers/SRIOV_IMPL.md) : crée 1 VF +
    mac/trust + bind vfio-pci + persistance boot — la **PF reste kernel** (ptp4l intact). Body :
    `{pf_bdf}`. Persiste le VF BDF découvert dans `node_interfaces.vf_bdf` (ligne de la PF).
    Prérequis : BIOS MMIO OK (sinon échec -ENOMEM, cf. verifier().sriov)."""
    from .. import mtl
    from ..database import db_get_node_interfaces, db_upsert_node_interface
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    pf_bdf = ((request.json or {}).get("pf_bdf") or "").strip()
    if not pf_bdf:
        return jsonify({"ok": False, "error": "pf_bdf requis"}), 400
    try:
        ok, msg, vf_bdf, checks = mtl.sriov_vf_apply(node, pf_bdf)
    except mtl.GardeFouVfio as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if ok and vf_bdf:                                    # persiste le VF BDF sur la ligne de la PF
        ifn = next((r.get("ifname") for r in db_get_node_interfaces(node_id)
                    if (r.get("pci") or "").strip().lower() == pf_bdf.lower()), None)
        if ifn:
            db_upsert_node_interface(node_id, ifn, vf_bdf=vf_bdf)
    cle = "alert.prep.sriov_vf_ok" if ok else "alert.prep.sriov_vf_echec"
    db_add_alert(cle, "info" if ok else "error", node_id=node_id, kind="prep",
                 params={"n": node.get("name") or node_id, "pf_bdf": pf_bdf,
                         "vf_bdf": vf_bdf or "", "msg": msg})
    return jsonify({"ok": ok, "msg": msg, "vf_bdf": vf_bdf, "checks": checks})


@bp.route("/api/nodes/<int:node_id>/install-ice", methods=["POST"])
@require_perm("settings.edit")
def api_node_install_ice(node_id):
    """Build + install (NON-disruptif) le driver ice Kahawai 2.6.6 — prérequis du RL narrow sur VF
    (cf. docs/chantiers/SRIOV_IMPL.md §3). Threadé + alerte (build ~2 min). Actif au PROCHAIN REBOOT (pas de rmmod
    à chaud). Idempotent (skip si déjà Kahawai)."""
    from .. import mtl
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404

    def _run():
        ok, msg, reboot = mtl.install_patched_ice(node)
        if not ok:
            cle = "alert.prep.ice_echec"
        elif reboot:
            cle = "alert.prep.ice_ok_reboot"
        else:
            cle = "alert.prep.ice_ok"
        db_add_alert(cle, "info" if ok else "error", node_id=node_id, kind="prep",
                     params={"n": node.get("name") or node_id, "msg": msg})
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "status": "build ice patché en cours (~2 min, voir alertes)"})


# ─── Sécurité / mTLS du plan de contrôle ────────────────────────────────────────
@bp.route("/api/security/mtls/status", methods=["GET"])
@require_perm("settings.edit")
def api_security_mtls_status():
    """État de la CA interne + inventaire des nœuds (badge HTTP/HTTPS). AUCUN secret
    (ca.ca_info est déjà sûr : sujet/empreinte/validité/SAN, pas de clé)."""
    from .. import ca
    nodes = [{"id": n["id"], "name": n.get("name"), "tls_ready": int(n.get("tls_ready") or 0),
              "agent_url": n.get("agent_url") or "", "status": n.get("status") or ""}
             for n in db_get_nodes()]
    return jsonify({"ca": ca.ca_info(), "nodes": nodes,
                    "detected_control_ip": ca._detect_control_ip()})

@bp.route("/api/security/mtls/ca/init", methods=["POST"])
@require_perm("settings.edit")
def api_security_mtls_ca_init():
    """Initialise (ou réémet, si force) la CA interne. force=True INVALIDE tous les certs
    déjà émis → les nœuds sont à re-migrer en HTTPS. Renvoie ca_info() + un avertissement."""
    from .. import ca
    d = request.json or {}
    ips = [str(x).strip() for x in (d.get("controller_ips") or []) if str(x).strip()]
    force = bool(d.get("force"))
    existed = ca.ca_available()
    if existed and not force:
        return jsonify({"ok": False, "error": "CA déjà initialisée — utiliser « Réémettre » (force) pour la remplacer"}), 409
    if not ips:
        ip = ca._detect_control_ip()
        if ip:
            ips = [ip]
    try:
        written = ca.create_ca_material(controller_sans=ips, overwrite=force)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    reissued = existed and force
    if reissued:
        db_add_alert("alert.node.ca_reemise", "warning", kind="node")
        # Les certs installés sur les nœuds ne sont plus signés par la nouvelle CA.
        for n in db_get_nodes():
            if int(n.get("tls_ready") or 0):
                try:
                    db_update_node(n["id"], tls_ready=0)
                except Exception:
                    pass
    else:
        db_add_alert("alert.node.ca_initialisee", "info", kind="node")
    return jsonify({"ok": True, "ca": ca.ca_info(), "reissued": reissued,
                    "written": len(written),
                    "warning": ("Réémission : tous les certificats émis sont invalidés ; "
                                "re-migrer chaque nœud en HTTPS.") if reissued else ""})


@bp.route("/api/nodes/<int:node_id>/nic_queues", methods=["GET"])
@require_login
def api_node_nic_queues_get(node_id):
    """Lit le nombre de combined queues E810 sur le nœud via ethtool -l."""
    from ..host_ops import ssh_run
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    iface = node.get("mtl_iface") or "ens1f0np0"
    rc, out, err = ssh_run(node["host"], f"ethtool -l {iface} 2>&1", timeout=8)
    import re as _re
    m_max = _re.search(r"Pre-set maximums.*?Combined:\s*(\d+)", out, _re.S)
    m_cur = _re.search(r"Current hardware settings.*?Combined:\s*(\d+)", out, _re.S)
    if not m_max or not m_cur:
        return jsonify({"error": f"ethtool -l {iface} inaccessible ou interface inconnue",
                        "raw": out[:400]}), 502
    hw_max = int(m_max.group(1))
    hw_cur = int(m_cur.group(1))
    # AF-XDP natif sur la PF : le moteur MTL se binde aux files ACTIVES de la carte
    # (1 file par session 2110, queue_id < combined). Il faut donc TOUTES les activer
    # (`ethtool -L combined max`), sinon « no free queue found » → 0 flux. La cible
    # optimale est donc le maximum matériel, et le nœud est prêt quand current == max.
    return jsonify({"max_combined": hw_max, "current_combined": hw_cur,
                    "optimal_combined": hw_max, "ready": hw_cur >= hw_max,
                    "xdp_available": hw_max - hw_cur, "iface": iface})

@bp.route("/api/nodes/<int:node_id>/nic_queues", methods=["POST"])
@require_perm("settings.edit")
def api_node_nic_queues_set(node_id):
    """Applique `ethtool -L combined N` sur la carte 2110 du nœud (queues réservées au noyau ;
    le reste est libéré pour l'AF-XDP du pipeline MTL), et le rend persistant au boot.

    E810/`ice` : `ethtool -L combined` échoue (« Cannot change channels when RDMA is active »,
    netlink EBUSY) tant que la RDMA est active sur la PF → on la désactive (devlink, runtime,
    best-effort, scopé à la carte sélectionnée) AVANT de changer les channels. La sortie d'`ethtool`
    est capturée EXPLICITEMENT → plus de faux « OK ». Persistance : un oneshot systemd refait
    désactivation RDMA + ethtool au boot (la RDMA et les channels reviennent après un reboot, p.ex.
    celui de la prép MTL) ; déposé via base64 pour éviter tout enfer de quoting."""
    import base64
    from ..host_ops import ssh_run
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    iface  = node.get("mtl_iface") or "ens1f0np0"
    target = max(1, int((request.json or {}).get("combined") or 4))
    # Script de boot (recompute la PCI, désactive RDMA, applique les channels) — POSIX sh.
    boot_sh = (
        "#!/bin/sh\n"
        "IFACE=%s\nTARGET=%s\n"
        "P=$(basename \"$(readlink -f /sys/class/net/$IFACE/device 2>/dev/null)\")\n"
        "if [ -n \"$P\" ]; then\n"
        "  devlink dev param set pci/$P name enable_roce value false cmode runtime 2>/dev/null\n"
        "  devlink dev param set pci/$P name enable_iwarp value false cmode runtime 2>/dev/null\n"
        "fi\n"
        "ethtool -L \"$IFACE\" combined \"$TARGET\"\n"
    ) % (iface, target)
    unit = (
        "[Unit]\nDescription=Bobi.Studio NIC queues + disable RDMA (%s)\n"
        "After=network-pre.target\nWants=network-pre.target\n"
        "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        "ExecStart=/usr/local/sbin/bobi-nicqueues.sh\n"
        "[Install]\nWantedBy=multi-user.target\n"
    ) % iface
    b64s = base64.b64encode(boot_sh.encode()).decode()
    b64u = base64.b64encode(unit.encode()).decode()
    # Application RUNTIME (gate dur sur le résultat d'ethtool) puis persistance best-effort.
    cmd = (
        "IFACE=" + iface + "; TARGET=" + str(target) + "; "
        "PCI=$(basename \"$(readlink -f /sys/class/net/$IFACE/device 2>/dev/null)\"); "
        "if [ -n \"$PCI\" ]; then "
        "devlink dev param set pci/$PCI name enable_roce value false cmode runtime 2>/dev/null; "
        "devlink dev param set pci/$PCI name enable_iwarp value false cmode runtime 2>/dev/null; "
        "fi; "
        "OUT=$(ethtool -L \"$IFACE\" combined \"$TARGET\" 2>&1); RC=$?; "
        "if [ $RC -ne 0 ]; then echo \"FAIL:$OUT\"; exit 1; fi; "
        "echo " + b64s + " | base64 -d > /usr/local/sbin/bobi-nicqueues.sh 2>/dev/null "
        "&& chmod +x /usr/local/sbin/bobi-nicqueues.sh; "
        "echo " + b64u + " | base64 -d > /etc/systemd/system/bobi-nicqueues.service 2>/dev/null "
        "&& systemctl daemon-reload 2>/dev/null && systemctl enable bobi-nicqueues.service 2>/dev/null; "
        "echo OK"
    )
    rc, out, err = ssh_run(node["host"], cmd, timeout=20)
    out = out or ""
    if "FAIL:" in out:
        detail = out.split("FAIL:", 1)[1].strip()[:300]
        if "RDMA" in detail or "busy" in detail.lower():
            detail += " — la RDMA n'a pas pu être désactivée (devlink) ; un redémarrage de l'hôte peut être nécessaire."
        return jsonify({"ok": False, "error": detail}), 502
    if "OK" not in out:
        return jsonify({"ok": False, "error": (out + (err or ""))[:400] or "échec inconnu"}), 502
    db_add_alert("alert.net.queues_nic", "info", node_id=node.get("id"), kind="net",
                 params={"n": node["name"], "iface": iface, "target": target})
    return jsonify({"ok": True, "combined": target, "iface": iface})


@bp.route("/api/fleet/orphans/destroy", methods=["POST"])
@require_perm("containers.delete")
def api_destroy_orphan():
    """Détruit un conteneur ORPHELIN constaté par la réconciliation (panneau Monitoring).
    Garde-fou : refuse tout nom qui n'est pas ACTUELLEMENT flaggé orphelin par fleet_status
    (on ne détruit jamais un nom arbitraire via cette route)."""
    from .. import fleet_status, node_driver
    data = request.json or {}
    try:
        nid = int(data.get("node_id") or 0)
    except (TypeError, ValueError):
        nid = 0
    name = str(data.get("name") or "").strip()
    if not fleet_status.est_orphelin(nid, name):
        return jsonify({"ok": False, "error": "pas (ou plus) orphelin — rafraîchir la page"}), 400
    node = db_get_node(nid)
    if not node:
        return jsonify({"ok": False, "error": "nœud inconnu"}), 404
    ok, err = node_driver.container_action(node, name, "destroy")
    if ok:
        fleet_status.oublier_orphelin(nid, name)
        db_add_alert("alert.node.orphelin_detruit", "info", node_id=node.get("id"), kind="deploy",
                     params={"name": name, "n": node.get("name") or node.get("host")})
    return jsonify({"ok": ok, "error": None if ok else str(err)[:200]}), (200 if ok else 502)
