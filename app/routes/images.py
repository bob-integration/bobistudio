# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Images runtime Docker (build SUR L'HÔTE via ssh + report dans les nœuds).

Même logique que les autres actions Local (ptp4l, prép MTL) : ça s'exécute sur l'HÔTE
(settings.proxmox_host), PAS sur la VM orchestrateur (qui n'a pas Docker). Le repo n'étant pas
sur l'hôte, on STREAME un contexte de build minimal (staging) en tar.gz via stdin ssh.

DEUX régimes de build, à ne pas confondre :
  · images PARTAGÉES (compute/media/webrtc) : build UNE fois sur l'hôte de build global (réglage
    `image_build_node`, cf. `_build_target`) puis DISTRIBUTION des bits aux nœuds (`_distribute_image`) ;
  · images `node_only` (mtl, compute-gpu) : jamais distribuées → construites SUR CHAQUE NŒUD qui en
    a besoin, déduit de la CAPACITÉ requise (`_image_target_nodes` / `_build_node_only_worker`), en
    ignorant le réglage global. Le champ de version `nodes.<field>` n'est écrit que sur un nœud où
    l'image est réellement présente (sinon on annonce « à jour » une image inexistante).
`stage` = (chemin repo, chemin dans le contexte) ; le Dockerfile est toujours stagé en "Dockerfile".

Module FONDATION du domaine nœuds/cluster : `nodes.py`, `enrollment.py` en dépendent
(_IMAGES, _image_tag, _repo_root, _stage_tar, _img_lock, _node_img_build, _image_present…)."""

import os
import re
import subprocess
import threading

from flask import jsonify, request

from . import bp
from ..auth import require_perm
from ..addressing import primary_host as _primary_host
from ..database import db_get_node, db_get_nodes, db_update_node, db_add_alert

# Conformité de redistribution : Apache-2.0 §4(a) et BSD-3-Clause exigent qu'une COPIE du texte
# de licence accompagne le BINAIRE redistribué. Les images embarquent libmxl et libmtl, donc
# elles doivent embarquer les licences. Cf. licenses/README.md et THIRD-PARTY-NOTICES.md.
_LICENCES = [("licenses/README.md", "licenses/README.md"),
             ("licenses/Apache-2.0.txt", "licenses/Apache-2.0.txt"),
             ("licenses/BSD-3-Clause-MTL.txt", "licenses/BSD-3-Clause-MTL.txt"),
             ("THIRD-PARTY-NOTICES.md", "THIRD-PARTY-NOTICES.md")]

_AGENT = ("script_templates/agent.py", "script_templates/agent.py")
# bobimxl : binding ctypes libmxl, COPY-é dans l'image compute (chantier MXL Phase 0).
_BOBIMXL = ("script_templates/bobimxl.py", "script_templates/bobimxl.py")
# Patch libmxl (type vidéo planar) appliqué dans le builder de l'image compute (Phase 1).
_PLANAR_PATCH = ("plugins/_compute_runtime/patches/mxl-planar-type.patch",
                 "plugins/_compute_runtime/patches/mxl-planar-type.patch")
# Patch libmxl (grain planar en N tranches — slice_height du flowDef → commit progressif, latence
# sous-trame). Appliqué APRÈS mxl-planar-type dans les Dockerfiles compute/media/mtl (chantier slice).
_PLANAR_SLICES_PATCH = ("plugins/_compute_runtime/patches/mxl-planar-slices.patch",
                        "plugins/_compute_runtime/patches/mxl-planar-slices.patch")
# Patch mxl-fabrics : le curseur de tranche n'était pas réinitialisé au changement de grain dans
# l'initiateur → les premières tranches du grain suivant n'étaient jamais transférées, et la cible
# committait quand même le grain comme complet. Bandes venant d'une autre trame sur toute réplique
# de flux TRANCHÉ. Appliqué dans le builder MXL de l'image mtl (cf. MXL_FABRICS_SLICE_CORRUPTION).
_FABRICS_SLICE_PATCH = ("plugins/_compute_runtime/patches/mxl-fabrics-slice-reset.patch",
                        "plugins/_compute_runtime/patches/mxl-fabrics-slice-reset.patch")
# Patch mxl-fabrics : FI_FENCE sur l'écriture qui porte la donnée immédiate d'un lot MULTI-PLANS.
# Rien n'ordonne les écritures RMA (aucun FI_ORDER_WAW demandé) → la notification pouvait précéder
# la charge utile qu'elle annonce, et la cible publiait des tranches encore en vol. SECONDE cause.
_FABRICS_FENCE_PATCH = ("plugins/_compute_runtime/patches/mxl-fabrics-fence-notification.patch",
                        "plugins/_compute_runtime/patches/mxl-fabrics-fence-notification.patch")
# Convertisseur v210↔planar (C auto-vectorisé) compilé dans l'étage v210-builder de l'image
# compute → libbobi_v210.so (+ variante AVX2), chargée par bobimxl (chantier interop MXL).
_V210_CONV = ("script_templates/v210convert.c", "script_templates/v210convert.c")
# Kernel compose multiview fusionné (blend/blend_pre/place en 1 passe, chantier fusion numpy→C)
# compilé dans le même étage v210-builder → libbobi_mvk.so (+ variante AVX2), chargé par bobimxl.
_MVK = ("script_templates/mvcompose.c", "script_templates/mvcompose.c")
# Copie non temporelle (chantier « 80 % du coût d'un producteur = déplacement de données ») :
# écrit le grain sans lire d'abord la ligne de cache qu'on écrase (pas de read-for-ownership).
# Compilé en x86-64-v2 dans les DEUX images (compute ET média) → doit figurer dans les deux
# contextes de build, sinon le COPY échoue et l'image entière ne se construit plus.
_NTCOPY = ("script_templates/ntcopy.c", "script_templates/ntcopy.c")
# Kernel CUDA du split (chantier GPU) : source .cu COPY-ée telle quelle dans l'image GPU, compilée
# à chaud côté conteneur. Le plugin `split` la lit à côté de bobimxl (cf. plugins/split/script.py).
# ⚠ Elle DOIT figurer dans le staging de `compute-gpu` : ce contexte ne contenait que le Dockerfile
# (« le reste vient de l'image de base »), et le COPY ajouté au Dockerfile a rendu l'image
# inconstruisible — le contexte n'a pas de dossier script_templates, donc `lstat` échoue au
# calcul de la clé de cache, sans que rien ne désigne la cause. Même piège que _NTCOPY plus haut.
# Noyaux de mesure du plugin `scope` (waveform, vecteur-scope, extrêmes), compilés dans le même
# étage v210-builder → libbobi_scope.so (+ variante AVX2), chargés par bobimxl.charger_noyau().
_SCOPEK = ("script_templates/scopekernel.c", "script_templates/scopekernel.c")
_SPLIT_GPU = ("script_templates/split_gpu.cu", "script_templates/split_gpu.cu")
_IMAGES = {
    "compute": {"label": "bobi-compute (calcul)", "meta": "plugins/_compute_runtime/meta.json",
                "prefix": "bobi-compute", "field": "compute_image",
                "stage": [("plugins/_compute_runtime/Dockerfile", "Dockerfile"), _AGENT, _BOBIMXL,
                          _PLANAR_PATCH, _PLANAR_SLICES_PATCH, _FABRICS_SLICE_PATCH, _FABRICS_FENCE_PATCH, _V210_CONV, _MVK, _NTCOPY, _SCOPEK]
                         + _LICENCES},
    # Variante GPU/NVIDIA de l'image compute : fine couche FROM bobi-compute:<ver> + cupy[ctk]
    # (cf. plugins/_compute_gpu_runtime/Dockerfile). node_only = buildée SUR le nœud GPU (image
    # CUDA lourde, locale, pas de push flotte — comme bobi-mtl). Le FROM résout l'image compute
    # présente sur le nœud → stage = Dockerfile seul (agent/bobimxl/libmxl viennent du base).
    "compute-gpu": {"label": "bobi-compute-gpu (calcul GPU/NVIDIA)",
                    "meta": "plugins/_compute_gpu_runtime/meta.json",
                    "prefix": "bobi-compute-gpu", "field": "compute_gpu_image", "node_only": True,
                    # Capacité REQUISE pour ce node_only : la liste `capabilities` OU le drapeau
                    # matériel `gpu_capable` (les deux dérivent, cf. _image_target_nodes).
                    "cap": "gpu", "cap_flag": "gpu_capable",
                    # base_from : le FROM de cette couche fine est RÉÉCRIT au staging vers l'image_tag
                    # COURANT de l'image `compute` (cf. _stage_tar) → plus de bump manuel du BASE_IMAGE à
                    # tenir synchro (la désync silencieuse 0.13→0.16 tentait un pull docker.io = échec).
                    "base_from": "compute",
                    # Dockerfile + les sources qu'il COPY : toute ligne COPY ajoutée au Dockerfile
                    # doit avoir son entrée ICI, sinon l'image ne se construit plus du tout.
                    "stage": [("plugins/_compute_gpu_runtime/Dockerfile", "Dockerfile"),
                              _SPLIT_GPU] + _LICENCES},
    "media":   {"label": "bobi-media (média)", "meta": "plugins/_media_runtime/meta.json",
                "prefix": "bobi-media", "field": "media_image",
                "stage": [("plugins/_media_runtime/Dockerfile", "Dockerfile"), _AGENT, _BOBIMXL,
                          _PLANAR_PATCH, _PLANAR_SLICES_PATCH, _FABRICS_SLICE_PATCH, _FABRICS_FENCE_PATCH, _NTCOPY] + _LICENCES},
    # Passerelle WebRTC (MediaMTX pré-baké). Pas de colonne nœud : le tag est stocké dans le
    # setting `webrtc_image` (commun à tous les nœuds) — cf. _autofill_nodes_image (cas spécial).
    "webrtc":  {"label": "bobi-webrtc (passerelle)", "meta": "plugins/_webrtc_runtime/meta.json",
                "prefix": "bobi-webrtc", "field": "webrtc_image", "setting": "webrtc_image",
                "stage": [("plugins/_webrtc_runtime/Dockerfile", "Dockerfile"), _AGENT]},
    # MTL : Dockerfile dans docker/ + mtl_rx.c (racine plugin) + controller/entrypoint.
    # NB : normalement buildé sur le nœud cible (clone MTL Internet + E810).
    "mtl":     {"label": "bobi-mtl (ST 2110 / E810)", "meta": "plugins/2110_io/meta.json",
                "prefix": "bobi-mtl", "field": "image", "node_only": True,
                # Capacité REQUISE : capabilities contient "io2110" OU drapeau `mtl_capable`.
                "cap": "io2110", "cap_flag": "mtl_capable",
                "stage": [("plugins/2110_io/docker/Dockerfile", "Dockerfile"),
                          ("plugins/2110_io/mtl_rx.c", "mtl_rx.c"),
                          ("plugins/2110_io/docker/controller.py", "controller.py"),
                          ("plugins/2110_io/docker/entrypoint.sh", "entrypoint.sh"),
                          # Patch libmtl st40/AF-XDP (RX ANC : mbuf port=UINT16_MAX → fallback mono-port).
                          ("plugins/2110_io/docker/patch_st40_afxdp_port.py", "patch_st40_afxdp_port.py"),
                          # Patch libmtl 2022-7 hitless TX (port au lien mort ⇒ drop, cf. 0.38.0).
                          ("plugins/2110_io/docker/patch_afxdp_tx_link_drop.py", "patch_afxdp_tx_link_drop.py"),
                          # Patch libmtl RX+TX resetting guard (RX+TX pacing RL même port, cf. 0.39.5).
                          ("plugins/2110_io/docker/patch_rx_resetting_guard.py", "patch_rx_resetting_guard.py"),
                          # Patch libmtl TX burst rendezvous (fix racine morts silencieuses TX au
                          # commit RL — compteur atomique per-port autour des bursts RX/TX + spin-wait
                          # borné avant le commit, cf. 0.45.0). DOIT suivre patch_rx_resetting_guard.
                          ("plugins/2110_io/docker/patch_tx_burst_rendezvous.py", "patch_tx_burst_rendezvous.py"),
                          # Patch libmtl garde TX hang au commit RL (stall d'ajout ≠ wedge, cf. 0.39.x).
                          ("plugins/2110_io/docker/patch_tx_hang_resetting_guard.py", "patch_tx_hang_resetting_guard.py"),
                          # Patch libmtl builder famine recovery (vidéo+audio) : session TX déjà vivante
                          # qui perd son mempool hdr au commit RL d'une AUTRE session → alloc-fail muet
                          # (hang detector natif jamais atteint) → déclenche la récupération existante
                          # (queue_fatal_error) après 2 s de famine confirmée (cf. 0.44.0).
                          ("plugins/2110_io/docker/patch_tx_builder_famine_recovery.py", "patch_tx_builder_famine_recovery.py"),
                          # Patch libmtl TX inflight frame reclaim (0.49.0) : 2ᵉ mode de mort du commit RL,
                          # DISTINCT de la famine mempool. Les mbufs perdus sans free au stop de port tiennent
                          # encore une ref extbuf sur la TRAME en vol ⇒ jamais rendue à l'app ⇒ get_next_frame
                          # rend -EBUSY à vie (`build ret -203`) sur une session qui n'échoue jamais à allouer
                          # (donc invisible du filet famine). Rappelle les trames orphelines SANS récupération
                          # de queue (zéro commit TM). DOIT suivre patch_tx_builder_famine_recovery.
                          ("plugins/2110_io/docker/patch_tx_frame_inflight_reclaim.py", "patch_tx_frame_inflight_reclaim.py"),
                          # Patch libmtl TX reset no-drop (0.50.0) : LA FUITE de mbufs du commit TM, À LA
                          # SOURCE. Le PMD ice libère bien les mbufs postés au stop de queue (DPDK 26.03,
                          # ice_rxtx.c:1196 → common/tx.h:360) : la fuite venait de NOTRE hang-guard, qui
                          # retournait nb_pkts (« émis ») pendant la fenêtre de commit — les transmetteurs
                          # vidéo/audio lâchaient alors les mbufs SANS free (mempool vidé = -207 ; ref extbuf
                          # jamais rendue sur la trame = -203 permanent). Fix : retourner 0 (« queue pleine »),
                          # les paquets restent en inflight et sont ré-émis au redémarrage du port.
                          # DOIT suivre patch_tx_hang_resetting_guard / famine / inflight_reclaim.
                          ("plugins/2110_io/docker/patch_tx_reset_no_drop.py", "patch_tx_reset_no_drop.py"),
                          # Patch libmtl hiérarchie TM ramifiée (>8 senders RL/port, cf. 0.39.6).
                          ("plugins/2110_io/docker/patch_tm_hierarchy.py", "patch_tm_hierarchy.py"),
                          # Patch libmtl option IP Router Alert aux reports IGMP (PTP carte-directe, cf. 0.39.12).
                          ("plugins/2110_io/docker/patch_igmp_router_alert.py", "patch_igmp_router_alert.py"),
                          # Patch libmtl répondeur ICMP echo : un port en vfio-pci n'a plus de netdev
                          # kernel — personne ne répond au ping, et libmtl ne connaît que l'ARP.
                          ("plugins/2110_io/docker/patch_icmp_echo.py", "patch_icmp_echo.py"),
                          # Patch libmtl règle rte_flow admettant le mcast PTP sur la queue CNI (E810/ice, cf. 0.39.13).
                          ("plugins/2110_io/docker/patch_ptp_mcast_flow.py", "patch_ptp_mcast_flow.py"),
                          # Patch libmtl getter d'état PTP stable pour le backstop TX de mtl_rx (cf. 0.39.15).
                          ("plugins/2110_io/docker/patch_ptp_stable_getter.py", "patch_ptp_stable_getter.py"),
                          # Patch libmtl export du grandmaster PTP pour a=ts-refclk:ptp du SDP TX (cf. 0.39.19).
                          ("plugins/2110_io/docker/patch_ptp_gm_export.py", "patch_ptp_gm_export.py"),
                          # Patch libmtl export de l'OFFSET PTP (mt_bobi_ptp_offset) → métriques :8080
                          # pour l'onglet PTP en socle DPDK (ptp4l absent). Cf. 0.54.0.
                          ("plugins/2110_io/docker/patch_ptp_offset_getter.py", "patch_ptp_offset_getter.py"),
                          # Patch libmtl : ACTIVE l'asservissement en fréquence du PHC (la branche
                          # existe mais son macro n'est défini nulle part en amont — sans lui le servo
                          # ne fait que sauter la phase à chaque Sync).
                          ("plugins/2110_io/docker/patch_ptp_adjust_freq.py", "patch_ptp_adjust_freq.py"),
                          # Patch libmtl DEV LINK WAIT : budget dev_detect_link 90s->120s
                          # (MT_DEV_LINK_POLL_COUNT 300->400) pour l'entraînement autoneg+FEC E810 100G DPDK.
                          ("plugins/2110_io/docker/patch_dev_link_wait.py", "patch_dev_link_wait.py"),
                          # Patch libmtl EPOCH-SHIFT TX (émission décalée après l'epoch, stamp nominal, cf. 0.41.0).
                          ("plugins/2110_io/docker/patch_epoch_shift.py", "patch_epoch_shift.py"),
                          # Patch DPDK ice_tm move retry (fix RACINE sessions TX mortes en cold-batch,
                          # cf. 0.46.0) : cible ice_tm.c (driver DPDK), pas libmtl — dépose un .patch
                          # dans patches/dpdk/<ver>/ du clone MTL, appliqué par build_dpdk.sh lui-même.
                          # Retry borné (5×20ms) du move admin-queue firmware + dégradation (queue
                          # laissée en place) au lieu de tuer le port entier si le move échoue quand même.
                          ("plugins/2110_io/docker/patch_ice_tm_move_retry.py", "patch_ice_tm_move_retry.py"),
                          # Chantier MXL : mtl_rx.c lie libmxl (étage builder identique à compute,
                          # patché planar) et controller.py importe bobimxl pour la simu/txgen.
                          _BOBIMXL, _PLANAR_PATCH, _PLANAR_SLICES_PATCH, _FABRICS_SLICE_PATCH, _FABRICS_FENCE_PATCH]},
}
_img_build = {w: {"status": "idle", "msg": ""} for w in _IMAGES}
_img_lock = threading.Lock()
_node_img_build = {}   # "{node_id}:{which}" → {status:idle|building|ok|error, msg} (build PAR-NŒUD via agent)
_node_img_push = {}    # node_id → {status:idle|pushing|ok|error, msg, start} (push des images PARTAGÉES)

def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _build_target():
    """Hôte de build EXPLICITE des images runtime PARTAGÉES (compute/media/webrtc). Renvoie
    ('local', None) [docker de l'orchestrateur] ou ('node', node_dict) [build via l'agent du nœud].
    Réglage `image_build_node` : 'local' | '<node_id>' | '' (auto). Auto/repli : setting
    `image_build_local`, sinon le nœud unique (`_primary_host`), sinon local."""
    from .. import settings as _st
    from ..database import db_get_node_by_host
    sel = str(_st.get("image_build_node") or "").strip()
    if sel == "local":
        return ("local", None)
    if sel.isdigit():
        n = db_get_node(int(sel))
        if n and (n.get("agent_url") or "").strip():
            return ("node", n)
        return ("local", None)                       # nœud choisi introuvable/sans agent → local
    # auto (rétro-compat) :
    if _st.get("image_build_local"):
        return ("local", None)
    h = (_primary_host() or "").strip()
    if h:
        n = db_get_node_by_host(h)
        if n:
            return ("node", n)
    return ("local", None)

def _build_host():
    kind, node = _build_target()
    return "" if kind == "local" else (node.get("host") or "")

def _ssh_bin(host, cmd, input_bytes=None, timeout=300):
    """ssh root@host avec stdin BINAIRE (ssh_run est text=True → inutilisable pour un tar)."""
    full = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=40", f"root@{host}", cmd]
    p = subprocess.run(full, input=input_bytes, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout or b""

def _ssh_stream(host, cmd, input_bytes, on_line, timeout=2400):
    """Comme _ssh_bin mais EN STREAMING : pousse le tar sur stdin (thread) et lit stdout ligne à
    ligne en appelant on_line(str) à chaque ligne (pour rapporter l'avancement live d'un build).
    Retourne (returncode, tail) où tail = les ~80 dernières lignes (pour le diagnostic d'erreur)."""
    import threading as _th, time as _t, collections as _col
    full = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=40", f"root@{host}", cmd]
    p = subprocess.Popen(full, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    def _feed():
        try:
            if input_bytes:
                p.stdin.write(input_bytes)
        except Exception:
            pass
        finally:
            try: p.stdin.close()
            except Exception: pass
    _th.Thread(target=_feed, daemon=True).start()
    tail = _col.deque(maxlen=80)
    start = _t.time()
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip("\n")
        tail.append(line)
        try: on_line(line)
        except Exception: pass
        if _t.time() - start > timeout:
            p.kill(); break
    try: p.wait(timeout=10)
    except Exception: p.kill()
    return (p.returncode if p.returncode is not None else 1), "\n".join(tail)

def _image_tag(which):
    """Tag de l'image : `image_tag` du meta.json, sinon `<prefix>:<version>` (repli)."""
    import json as _json
    spec = _IMAGES[which]
    try:
        with open(os.path.join(_repo_root(), spec["meta"])) as f:
            meta = _json.load(f)
        tag = (meta.get("image_tag") or "").strip()
        if tag:
            return tag
        ver = (meta.get("version") or "").strip()
        return "%s:%s" % (spec["prefix"], ver) if ver else spec["prefix"] + ":latest"
    except Exception:
        return spec["prefix"] + ":latest"

def _present_tags(imgs):
    """Tags réellement présents depuis l'inventaire agent. Tolère les 2 formats : liste de
    {tag, present} (agent actuel) ou liste de chaînes (legacy). Renvoie un set de tags présents."""
    out = set()
    for t in (imgs or []):
        if isinstance(t, dict):
            if t.get("present"):
                out.add(str(t.get("tag") or ""))
        elif t:
            out.add(str(t))
    return out

_inv_cache = {}          # node_id → (ts, [{tag, id, created, size}], err)
_inv_lock = threading.Lock()
_INV_TTL = 20            # s — l'inventaire `docker images` d'un nœud est stable, on évite le martèlement

def _ver_key(tag):
    """Clé de tri d'un tag `prefix:version` — tri DÉCROISSANT attendu (0.15 > 0.9 > 0.2.3).
    Les tags nommés (`:banc-after`, `:latest`) doivent finir DERRIÈRE les versions numériques :
    un segment numérique pèse donc plus qu'un segment texte (rang 1 vs 0)."""
    ver = (tag.split(":", 1)[1] if ":" in tag else "")
    parts = []
    for seg in re.split(r"[._-]", ver):
        parts.append((1, int(seg), "") if seg.isdigit() else (0, 0, seg))
    return tuple(parts)

def _node_image_inventory(node, force=False):
    """Énumère les tags `bobi-*` RÉELLEMENT présents sur un nœud (`docker images` via l'agent).
    LECTURE SEULE. Renvoie (liste [{tag,id,created,size}] triée version décroissante, err_str).
    Cache TTL `_INV_TTL` (bypass avec force=True)."""
    import time as _t
    from .. import node_driver as _nd
    nid = (node or {}).get("id")
    with _inv_lock:
        hit = _inv_cache.get(nid)
    if hit and not force and (_t.time() - hit[0]) < _INV_TTL:
        return hit[1], hit[2]
    imgs, err = [], ""
    if not (node or {}).get("agent_url"):
        err = "nœud sans agent"
    else:
        fmt = "{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}\t{{.Size}}"
        rc, out, serr = _nd.host_exec(
            node, "docker images --filter reference='bobi-*' --format '%s'" % fmt, timeout=25)
        if rc != 0:
            err = (serr or out or "docker images a échoué").strip()[:200]
        else:
            for line in (out or "").splitlines():
                f = line.split("\t")
                if len(f) < 4 or not f[0] or f[0].endswith(":<none>"):
                    continue
                imgs.append({"tag": f[0].strip(), "id": f[1].strip(),
                             "created": f[2].strip(), "size": f[3].strip()})
            imgs.sort(key=lambda i: _ver_key(i["tag"]), reverse=True)
    with _inv_lock:
        _inv_cache[nid] = (_t.time(), imgs, err)
    return imgs, err

def _used_tag(which, node):
    """Tag que le nœud UTILISE réellement pour cette image : colonne `nodes.<field>`, ou setting
    commun (webrtc). C'est CE tag qui décide de ce qui tourne."""
    spec = _IMAGES.get(which) or {}
    skey = spec.get("setting")
    if skey:
        from .. import settings as _st
        return (_st.get(skey) or "").strip()
    return ((node or {}).get(spec.get("field") or "") or "").strip()

def _image_present(host, tag):
    """L'image existe-t-elle sur l'HÔTE (via ssh) ?"""
    if not host or not tag:
        return False
    try:
        import shlex as _sh
        rc, _ = _ssh_bin(host, "docker image inspect %s >/dev/null 2>&1" % _sh.quote(tag), timeout=20)
        return rc == 0
    except Exception:
        return False

def _node_needs_image(which, node):
    """Ce nœud a-t-il BESOIN de cette image node_only ? Prédicat en UNION des deux sources, qui
    dérivent l'une de l'autre en production :
      · la liste `capabilities` (JSON, posée à l'enrôlement / éditée à la main) ;
      · le DRAPEAU matériel de la colonne nœud (`gpu_capable`, `mtl_capable`, sondé à chaud).
    Cas vécus (banc) : dl360-1 a "io2110" dans `capabilities` mais `mtl_capable=0` ; dell-1 a
    `gpu_capable=1` mais PAS "gpu" dans `capabilities`. Prendre une seule des deux exclurait à tort
    un nœud → image annoncée en service là où elle n'existe pas. On prend donc l'UNION (faux positif
    = un build de trop, faux négatif = un déploiement qui échoue au run)."""
    spec = _IMAGES.get(which) or {}
    if not spec.get("node_only"):
        return False
    from .. import node_driver as _nd
    cap, flag = spec.get("cap"), spec.get("cap_flag")
    if cap and cap in _nd.node_capabilities(node):
        return True
    return bool(flag and (node or {}).get(flag))


def _image_target_nodes(which):
    """Nœuds sur lesquels une image `node_only` doit être CONSTRUITE (capacité requise + agent)."""
    return [n for n in db_get_nodes()
            if (n.get("agent_url") or "").strip() and _node_needs_image(which, n)]


def _image_present_node(node, tag, force=False):
    """L'image `tag` est-elle réellement présente SUR CE NŒUD ? (inventaire agent, cache TTL)."""
    if not tag or not node:
        return False
    imgs, err = _node_image_inventory(node, force=force)
    if err:
        return False
    return any((i.get("tag") or "") == tag for i in imgs)


def _node_expects_image(which, node):
    """Cette image est-elle ATTENDUE sur ce nœud ? Même critère que ce qui la PROVISIONNE :
      · `node_only` (mtl, compute-gpu) → `_node_needs_image` (union capacités / drapeau matériel) ;
      · partagées (compute, media, webrtc) → capacité déclarée, comme `_provision_shared_images`
        (`if which not in caps: continue`) et comme la checklist nœud (`_imgMap` côté UI).
    Nœud sans `capabilities` (ligne héritée, jamais réenregistrée) : on retombe sur « un tag est
    configuré » plutôt que de tout déclarer non-attendu — sinon un vrai manque passerait en vert."""
    spec = _IMAGES.get(which) or {}
    if spec.get("node_only"):
        return _node_needs_image(which, node)
    from .. import node_driver as _nd
    caps = _nd.node_capabilities(node)
    return (which in caps) if caps else bool(_used_tag(which, node))


def node_images_state(node):
    """which→{tag, present, expected} des images Docker d'un nœud (clés de `_IMAGES` : compute /
    compute-gpu / media / webrtc / mtl), pour la palette de création : ne griser un nœud QUE si
    l'image du type choisi (`plugins.image_kind` → cette clé) y manque. LECTURE SEULE — réutilise
    l'inventaire agent caché (`_node_image_inventory`, TTL 20 s) ; repli ssh direct `docker image
    inspect` si le nœud n'a pas d'agent (JAMAIS pour une image non attendue : inutile et lent).
    Le tag attendu = `_used_tag` (colonne `nodes.<field>`, ou setting pour webrtc).
    `expected` = l'image a-t-elle une raison d'être là (cf. `_node_expects_image`) — un badge qui
    l'ignore criait « image absente » sur tout nœud non-io2110, faute de bobi-mtl."""
    imgs, err = _node_image_inventory(node)
    present = {(i.get("tag") or "") for i in (imgs or [])}
    out = {}
    for which in _IMAGES:
        tag = _used_tag(which, node)
        try:
            exp = _node_expects_image(which, node)
        except Exception:
            exp = bool(tag)
        inconnu = False
        if not tag:
            ok = False                                   # image non configurée → un run échouerait
        elif err:
            # Inventaire agent KO. Le repli ssh n'est autoritatif que sur un nœud SANS agent : sur un
            # nœud enrôlé B3-1 il n'y a plus de root-SSH et `docker image inspect` répond « absente »
            # quoi qu'il arrive (cf. `_present_on_build_target`). L'appeler quand l'agent existe mais
            # ne répond pas, c'est donc payer un timeout ssh (3,1 s par image, 9,4 s des 12 s de
            # /api/nodes — mesuré 2026-08-19) pour une réponse sans valeur, à chaque poll.
            # On ne sonde donc que les nœuds legacy ; ailleurs on rend la MÊME valeur, tout de suite,
            # en marquant `unknown` pour que « pas pu vérifier » ne se confonde pas avec « vérifié
            # absent » côté diagnostic.
            if node.get("agent_url"):
                ok, inconnu = False, True
            else:
                ok = _image_present(node.get("host"), tag) if exp else False
        else:
            ok = tag in present
        out[which] = {"tag": tag, "present": bool(ok), "expected": bool(exp)}
        if inconnu:
            out[which]["unknown"] = True
    return out


def _autofill_nodes_image(which, tag):
    """Met à jour le tag dans tous les nœuds concernés après un build réussi."""
    # Image sans colonne nœud (ex. webrtc) : stockée dans un setting commun à tous les nœuds.
    setting_key = _IMAGES[which].get("setting")
    if setting_key:
        from .. import settings as _st
        if (_st.get(setting_key) or "").strip() != tag:
            _st.set(setting_key, tag)
        return 1
    field = _IMAGES[which]["field"]
    filled = 0
    non_verifies = []
    for n in db_get_nodes():
        # ★ NE PAS INSCRIRE UN TAG QU'ON N'A PAS VU SUR LE NŒUD (2026-08-21).
        # Cette fonction écrivait le tag sur TOUS les nœuds dès qu'un build réussissait, sans
        # vérifier que les bits y étaient arrivés. Un nœud ÉTEINT pendant la distribution gardait
        # donc ses vieilles images tandis que la base affirmait qu'il avait la neuve — et la
        # distribution ratée ne se rejoue pas au rallumage. Le mensonge ne se voyait qu'au
        # premier build DÉRIVÉ sur ce nœud (`FROM bobi-compute:0.29` absent), où Docker part
        # chercher l'image sur Docker Hub et rend « pull access denied » : un message
        # d'AUTORISATION pour une cause de DISPONIBILITÉ. Le testeur a cherché du côté des
        # identifiants de registre — exactement la mauvaise piste (recette Valentin, 2026-08-21).
        if not _node_expects_image(which, n):
            continue
        if not _image_present_node(n, tag, force=True):
            non_verifies.append(n)
            continue
        if (n.get(field) or "").strip() != tag:
            db_update_node(n["id"], **{field: tag})
            filled += 1
    for n in non_verifies:
        # Visible, et NOMMÉ : « 1 échec(s) » se lit comme un détail, « échec vers r620-1 » se lit
        # comme une panne. Le tag précédent du nœud est LAISSÉ EN PLACE : il décrit ce qu'il a.
        db_add_alert("alert.image.tag_non_confirme", "warning", node_id=n.get("id"), kind="prep",
                     params={"tag": tag, "n": n.get("name") or n.get("id")})
    return filled

_derive_vue = {}          # node_id -> monotone du dernier contrôle de dérive
_DERIVE_PERIODE_S = 900.0  # 15 min : la dérive d'images n'est pas un phénomène rapide


def verifier_derive_images(node):
    """Le nœud a-t-il VRAIMENT les images que la base lui prête ? Constate et NOMME l'écart.

    ★ POURQUOI CE CONTRÔLE (2026-08-22). Une distribution ratée ne se rejoue pas au rallumage :
    un nœud éteint au mauvais moment garde ses vieilles images indéfiniment, sans que rien ne le
    dise. L'écart ne se manifeste qu'au premier build DÉRIVÉ, sous un message d'autorisation
    trompeur — le testeur part alors vérifier ses identifiants de registre.

    ⚠ CE CONTRÔLE NE RAPATRIE RIEN, DÉLIBÉRÉMENT. Relancer seul un transfert de plusieurs Go
    depuis un échantillonneur de fond, sans que personne ne l'ait demandé, saturerait le lien au
    pire moment (une distribution pèse plusieurs gigaoctets). On rend l'écart VISIBLE ; le geste
    reste à l'exploitant, qui sait si le moment s'y prête. Automatiser le rattrapage est une
    décision d'exploitation, pas un détail d'implémentation.
    """
    import time as _t
    nid = (node or {}).get("id")
    if not nid or not (node.get("agent_url") or "").strip():
        return None
    if (_t.monotonic() - _derive_vue.get(nid, 0.0)) < _DERIVE_PERIODE_S:
        return None
    _derive_vue[nid] = _t.monotonic()
    manquantes = []
    for which in _IMAGES:
        if _IMAGES[which].get("node_only"):
            continue                     # buildée SUR le nœud : pas distribuée, hors sujet ici
        if not _node_expects_image(which, node):
            continue
        tag = _image_tag(which)
        if not tag:
            continue
        if not _image_present_node(node, tag, force=True):
            manquantes.append(tag)
    if manquantes:
        db_add_alert("alert.image.derive_noeud", "warning", node_id=nid, kind="prep",
                     params={"n": node.get("name") or nid, "tags": ", ".join(manquantes)})
    return manquantes


def _stage_tar(which):
    """Construit en mémoire le tar.gz du contexte de build minimal (staging)."""
    import io, tarfile
    root = _repo_root()
    missing = [src for src, _ in _IMAGES[which]["stage"]
               if not os.path.exists(os.path.join(root, src))]
    if missing:
        # Cas typique : instance DÉPLOYÉE (build) où les contextes d'image n'ont pas été embarqués
        # (corrigé dans builder.py:RUNTIME_IMAGE_DIRS) → il faut re-builder/mettre à jour l'instance.
        raise FileNotFoundError(
            "contexte de build incomplet sur cette instance (%s). "
            "Mettre à jour l'instance (re-build incluant plugins/_compute_runtime & _media_runtime)."
            % ", ".join(missing))
    # GARDE-FOU : tout ce que le Dockerfile COPY doit exister DANS le contexte. Sans ce contrôle,
    # une ligne COPY ajoutée sans entrée de staging correspondante rend l'image inconstruisible et
    # buildkit ne rend qu'un `lstat …/buildkit-mount…/<dir>: no such file or directory` — un chemin
    # temporaire qui ne désigne ni le fichier manquant ni la liste à corriger. On échoue ici, avec
    # le nom du fichier et l'endroit à modifier. (Piège récurrent : cf. _NTCOPY et _SPLIT_GPU.)
    _dockerfile = next((s for s, d in _IMAGES[which]["stage"] if d == "Dockerfile"), None)
    if _dockerfile:
        _fournis = {dst for _s, dst in _IMAGES[which]["stage"]}
        with open(os.path.join(root, _dockerfile), "r", errors="replace") as _f:
            _lignes = _f.read().splitlines()
        _absents = []
        for _l in _lignes:
            _m = re.match(r'^\s*COPY\s+(?!--from)(?:--\S+\s+)*(\S+)\s+\S+\s*$', _l)
            if not _m:
                continue
            _src = _m.group(1).strip('"')
            if _src not in _fournis and not any(d.startswith(_src.rstrip("/") + "/")
                                                for d in _fournis):
                _absents.append(_src)
        if _absents:
            raise FileNotFoundError(
                "Dockerfile de l'image « %s » : %s COPY-é(s) mais absent(s) du contexte de build. "
                "Ajouter l'entrée correspondante dans _IMAGES[\"%s\"][\"stage\"] "
                "(app/routes/images.py) — un COPY sans staging rend l'image inconstruisible."
                % (which, ", ".join(_absents), which))

    # Couche fine (base_from) : on réécrit le `ARG BASE_IMAGE=` du Dockerfile vers l'image_tag
    # COURANT de l'image parente (ex. compute) au lieu de dépendre de la valeur figée dans le
    # fichier — sinon un bump de l'image parente désync silencieusement (FROM inexistant en local
    # → pull docker.io → échec d'auth). Repli : si rien à réécrire, le Dockerfile passe tel quel.
    base_from = _IMAGES[which].get("base_from")
    base_tag = _image_tag(base_from) if base_from else None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for src, dst in _IMAGES[which]["stage"]:
            path = os.path.join(root, src)
            if base_tag and dst == "Dockerfile":
                with open(path, "rb") as f:
                    data = re.sub(rb'(?m)^(ARG\s+BASE_IMAGE=).*$',
                                  b'\\1' + base_tag.encode(), f.read())
                info = tarfile.TarInfo(name=dst)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.add(path, arcname=dst)
    return buf.getvalue()

def _build_is_local():
    """Vrai si on build sur l'hôte LOCAL du contrôleur (docker en subprocess) au lieu d'un nœud.
    Dérive de `_build_target()` (réglage `image_build_node` explicite, repli `image_build_local` /
    nœud unique). Permet de builder + push/load vers les nœuds sans registry."""
    return _build_target()[0] == "local"

def _docker_local_present(tag):
    try:
        return subprocess.run(["docker", "image", "inspect", tag], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except Exception:
        return False

def _present_on_build_target(tag, force=False):
    """L'image `tag` est-elle présente LÀ OÙ L'ON BUILDE ?
    ★ Un nœud enrôlé façon B3-1 n'a PLUS de root-SSH depuis le contrôleur (le build passe par
    l'agent) : `_image_present` (ssh `docker image inspect`) y répond TOUJOURS « absente », et un
    build parfaitement réussi était rapporté « échec build : … » avec, en queue de message, la
    trace d'un build qui se termine par « naming to … done » (bug vécu sur r620-3, 2026-08-19).
    On interroge donc l'INVENTAIRE AGENT dès que le nœud en a un ; le repli ssh ne sert plus qu'aux
    nœuds legacy sans agent. `force` = bypass du cache TTL (à réserver au verdict de fin de build ;
    le polling de statut doit garder le cache)."""
    kind, node = _build_target()
    if kind == "local":
        return _docker_local_present(tag)
    if node and node.get("agent_url"):
        return _image_present_node(node, tag, force=force)
    return _image_present((node or {}).get("host"), tag)

def _build_local_stream(ctx_bytes, tag, on_line, timeout=2400):
    """Build LOCAL : extrait le contexte (tar.gz de _stage_tar) dans un tmpdir et lance
    `docker build` en subprocess, en streamant la sortie ligne à ligne. Retourne (rc, tail)."""
    import io, tarfile, tempfile, shutil, collections
    d = tempfile.mkdtemp(prefix="bobi-img-")
    tail = collections.deque(maxlen=80)
    try:
        with tarfile.open(fileobj=io.BytesIO(ctx_bytes), mode="r:gz") as t:
            t.extractall(d)   # noqa: S202 (contexte produit par nous)
        env = dict(os.environ, DOCKER_BUILDKIT="1")
        p = subprocess.Popen(["docker", "build", "--progress=plain", "-t", tag,
                              "-f", os.path.join(d, "Dockerfile"), d],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        for raw in p.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            tail.append(line)
            try: on_line(line)
            except Exception: pass
        p.wait(timeout=timeout)
        return p.returncode, "\n".join(tail)
    finally:
        shutil.rmtree(d, ignore_errors=True)

# Parsers de la sortie `docker build --progress=plain` (buildkit) :
#  · étape Dockerfile     : `#12 [ 9/15] RUN …`  → numéro d'étape + commande
#  · sous-progression ninja: `[1234/2451] Compiling …` (DPDK/MTL = meson+ninja) → barre fine
_STEP_RE  = re.compile(r'^#\d+\s+\[\s*(\d+)/(\d+)\]\s+(.*)$')
_NINJA_RE = re.compile(r'\[(\d+)/(\d+)\]\s+(Compiling|Linking|Generating|Installing)')

def _materialize_image_tar(which, tag, dest, build_node=None):
    """Écrit le tar de l'image `tag` dans `dest` depuis l'HÔTE DE BUILD : `docker save` local si build
    local, sinon export depuis le nœud de build (relais, sans Docker côté orchestrateur). `build_node`
    explicite (worker) sinon résolu via `_build_target()` (enrôlement/provision). (ok, msg)."""
    import subprocess as _sp
    from .. import node_driver as _nd
    bnode = build_node
    if bnode is None:
        kind, bnode = _build_target()
        if kind != "node":
            bnode = None
    if bnode:
        return _nd.export_image(bnode, tag, dest)
    ok = _sp.run(["docker", "save", "-o", dest, tag]).returncode == 0
    return (ok, "docker save local" if ok else "docker save local échoué (image présente côté contrôleur ?)")


def _distribute_image(which, tag, build_node):
    """Distribue les BITS de l'image `tag` à tous les nœuds qui l'exécutent (capacité == `which`),
    SAUF le nœud de build (il l'a déjà). Matérialise le tar UNE fois (`_materialize_image_tar`) puis
    `load_image_file` sur chaque cible. `mtl` (node_only) exclu (build par-nœud).

    Renvoie `(n_ok, n_fail, msg, echecs)` — `echecs` = [(nom du nœud, raison), …].
    Le 4ᵉ terme existe parce qu'un COMPTE n'est pas actionnable : « 1 échec(s) » se lit comme un
    détail, et le nœud resté en retard ne se manifeste qu'au premier build DÉRIVÉ, sous la forme
    d'un `pull access denied` qui accuse le registre (recette 2026-08-21, r620-1 éteint pendant la
    distribution). L'appelant doit pouvoir NOMMER les nœuds dans sa synthèse."""
    import os, tempfile
    from .. import node_driver as _nd
    spec = _IMAGES.get(which) or {}
    if spec.get("node_only"):
        return (0, 0, "node_only — pas de distribution", [])
    targets = [n for n in db_get_nodes()
               if (n.get("agent_url") or "").strip()
               and not (build_node and n.get("id") == build_node.get("id"))
               and which in _nd.node_capabilities(n)]
    if not targets:
        return (0, 0, "aucun nœud cible", [])
    tmp = tempfile.NamedTemporaryFile(prefix="bobi-dist-", suffix=".tar", delete=False)
    tmp.close()
    try:
        ok, msg = _materialize_image_tar(which, tag, tmp.name, build_node)
        if not ok:
            # Export raté = TOUTES les cibles restent en retard. On comptait `len(targets)` échecs
            # sans nommer personne NI poser la moindre alerte par nœud : le seul chemin où le parc
            # divergeait en silence. Chaque cible est désormais signalée comme les autres.
            echecs = [(n.get("name"), "export image échoué : %s" % msg) for n in targets]
            for n in targets:
                db_add_alert("alert.image.distribution_echouee", "warning", node_id=n.get("id"),
                             kind="prep", params={"tag": tag, "n": n.get("name"),
                                                  "m": "export image échoué : %s" % msg})
            return (0, len(targets), "export image échoué : %s" % msg, echecs)
        n_ok = 0
        echecs = []
        for n in targets:
            o, m = _nd.load_image_file(n, tag, tmp.name)
            if o:
                n_ok += 1
            else:
                echecs.append((n.get("name"), m))
                db_add_alert("alert.image.distribution_echouee", "warning", node_id=n.get("id"),
                             kind="prep", params={"tag": tag, "n": n.get("name"), "m": m})
        detail = " (%s)" % ", ".join(nom or "?" for nom, _m in echecs) if echecs else ""
        return (n_ok, len(echecs), "%d ok / %d échec%s" % (n_ok, len(echecs), detail), echecs)
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def _provision_shared_images(node, progress=None):
    """Charge sur `node` les images PARTAGÉES (compute/media/webrtc) requises par ses capacités, depuis
    l'hôte de build. Skippe si le nœud EST l'hôte de build, ou si l'image n'est pas encore buildée (pas
    une erreur). Utilisé à l'enrôlement + via le bouton « Pousser les images partagées ». (n_ok, n_fail).

    `progress(texte)` : appelé à chaque étape. Un push fait 2 Go et dure des MINUTES — sans ce fil,
    l'appelant HTTP rend la main immédiatement et l'utilisateur ne voit rigoureusement rien se passer
    (signalé 2026-08-02). Ce qui est long est ici : `_materialize_image_tar` (export côté contrôleur)
    puis `load_image_file` (transfert + `docker load` côté nœud)."""
    import os, tempfile
    from .. import node_driver as _nd
    kind, bnode = _build_target()
    caps = _nd.node_capabilities(node)
    n_ok = n_fail = 0
    _dire = progress or (lambda _t: None)
    for which in ("compute", "media", "webrtc"):
        if which not in caps:
            continue
        if bnode and node.get("id") == bnode.get("id"):
            continue                                  # build host = ce nœud → image déjà présente
        tag = _image_tag(which)
        _dire("%s : préparation de l'archive…" % tag)
        tmp = tempfile.NamedTemporaryFile(prefix="bobi-prov-", suffix=".tar", delete=False)
        tmp.close()
        try:
            ok, _msg = _materialize_image_tar(which, tag, tmp.name)
            if not ok:
                _dire("%s : pas encore buildée côté contrôleur — ignorée" % tag)
                continue                              # image pas encore buildée → on n'échoue pas
            try:
                _taille = " (%.1f Go)" % (os.path.getsize(tmp.name) / 1e9)
            except OSError:
                _taille = ""
            _dire("%s : transfert vers le nœud%s…" % (tag, _taille))
            o, m = _nd.load_image_file(node, tag, tmp.name)
            # ★ CONSTATER, PAS CROIRE (2026-08-22). On comptait un succès sur le seul retour de
            # `load_image_file`. Un transfert « ok » qui n'aboutit pas laisse le nœud sans image
            # pendant qu'on annonce l'inverse — et le mensonge ne se voit qu'au premier build
            # dérivé, avec un message d'autorisation trompeur. On relit donc l'inventaire du nœud.
            if o and not _image_present_node(node, tag, force=True):
                o, m = False, "chargement rapporté OK mais l'image reste ABSENTE de l'inventaire du nœud"
            if o:
                n_ok += 1
                _dire("%s : chargée sur le nœud ✓" % tag)
            else:
                n_fail += 1
                _dire("%s : ÉCHEC — %s" % (tag, m))
                db_add_alert("alert.image.provision_echouee", "warning", node_id=node.get("id"),
                             kind="prep", params={"tag": tag, "n": node.get("name"), "m": m})
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass
    return (n_ok, n_fail)


# Combien de temps on surveille l'apparition de l'image APRÈS un suivi HTTP expiré. Le build
# continue sur le nœud : à Horace le 2026-08-19, `bobi-mtl:0.96.0` est sortie 13 min après le
# timeout de 40 min. 30 min laissent de la marge sans immobiliser le statut indéfiniment.
_BUILD_SUIVI_APRES_TIMEOUT_S = 1800


def _build_node_only_worker(which):
    """Build d'une image `node_only` (bobi-mtl, bobi-compute-gpu) : PAS sur le nœud de build global
    (réglage `image_build_node`, pertinent seulement pour les images partagées build-once+push) mais
    sur CHAQUE nœud qui en a besoin, déduit de la CAPACITÉ requise (`_image_target_nodes`).

    Trois invariants (bug de prod 2026-07 : bobi-compute-gpu:0.6 buildée sur dl360-1, ABSENTE de
    dell-1 — le seul nœud GPU — et pourtant annoncée « à jour » sur les DEUX nœuds) :
      1. le champ de version `nodes.<field>` n'est renseigné QUE sur un nœud où l'image a été
         effectivement construite ET vérifiée présente (jamais d'autofill flotte) ;
      2. statut PAR NŒUD (partagé avec le build par-nœud de `nodes.py`, même clé `_node_img_build`),
         et l'agrégat passe en `error` dès qu'UN nœud échoue — pas d'« ok » global masquant un trou ;
      3. aucun nœud cible → erreur explicite, on ne build PAS au hasard sur le nœud de build."""
    import time as _t
    from .. import node_driver as _nd
    tag = _image_tag(which)
    spec = _IMAGES[which]
    start = _t.time()
    targets = _image_target_nodes(which)
    if not targets:
        cap = spec.get("cap") or which
        with _img_lock:
            _img_build[which] = {
                "status": "error",
                "msg": "aucun nœud cible pour %s : aucun nœud enrôlé (avec agent) n'a la capacité "
                       "« %s » (ni le drapeau %s). Image NON construite." % (tag, cap, spec.get("cap_flag"))}
        db_add_alert("alert.image.build_aucun_noeud", "warning", kind="prep",
                     params={"tag": tag, "cap": cap})
        return
    names = ", ".join(str(n.get("name") or n.get("id")) for n in targets)
    with _img_lock:
        _img_build[which] = {"status": "building",
                             "msg": "build par-nœud sur %d nœud(s) : %s" % (len(targets), names),
                             "nodes": []}
    try:
        ctx = _stage_tar(which)
    except Exception as e:
        with _img_lock:
            _img_build[which] = {"status": "error", "msg": str(e)}
        return
    results = []
    for node in targets:
        nid = node["id"]
        nname = str(node.get("name") or nid)
        key = "%d:%s" % (nid, which)
        n_start = _t.time()
        with _img_lock:
            _node_img_build[key] = {"status": "building", "msg": "build sur le nœud (agent)…",
                                    "start": n_start}
            if _img_build.get(which, {}).get("status") == "building":
                _img_build[which]["msg"] = "build sur %s (%d/%d)…" % (
                    nname, len(results) + 1, len(targets))
        ok, tail = False, ""
        try:
            rc, tail = _nd.build_image(node, tag, ctx, timeout=2400)
            # ANTI-FAUX-ÉCHEC (2026-08-19, incident Horace) : le timeout ne coupe que l'attente
            # HTTP, jamais le `docker build` du nœud. Déclarer « échec » à cet instant est un
            # MENSONGE qui coûte cher : il invite à relancer, donc à lancer une seconde
            # compilation en parallèle sur un nœud qui porte l'antenne. On surveille l'apparition
            # de l'image au lieu de conclure — c'est exactement ce qu'un opérateur ferait à la
            # main, et ce qui a effectivement récupéré bobi-mtl:0.96.0 ce jour-là.
            if rc == _nd.BUILD_RC_TIMEOUT:
                _fin = _t.time() + _BUILD_SUIVI_APRES_TIMEOUT_S
                with _img_lock:
                    _node_img_build[key] = {
                        "status": "building",
                        "msg": "suivi interrompu à %d min — le build CONTINUE sur %s, on surveille "
                               "l'apparition de %s" % (2400 // 60, nname, tag),
                        "start": n_start}
                while _t.time() < _fin and not _image_present_node(node, tag, force=True):
                    _t.sleep(30)
                ok = _image_present_node(node, tag, force=True)
                tail = ("image apparue après un suivi interrompu" if ok else
                        "suivi interrompu à %d min et %s toujours absente %d min plus tard — le "
                        "build peut encore tourner sur %s : VÉRIFIER (`ps -eo args | grep \"docker "
                        "build\"`) AVANT de relancer, deux compilations en parallèle sur un nœud "
                        "d'antenne se voient à l'image."
                        % (2400 // 60, tag, _BUILD_SUIVI_APRES_TIMEOUT_S // 60, nname))
            else:
                ok = (rc == 0)
            # Anti-faux-ok : rc=0 ne suffit pas, l'image doit être RÉELLEMENT présente sur ce nœud.
            if ok and not _image_present_node(node, tag, force=True):
                ok, tail = False, ("build rc=0 mais %s introuvable sur %s (faux-ok). %s"
                                   % (tag, nname, str(tail)[-400:]))
        except Exception as e:
            tail = str(e)
        dur = int(_t.time() - n_start)
        _min, _sec = dur // 60, dur % 60
        if ok:
            # Le tag n'est écrit QUE sur ce nœud, et seulement parce que l'image y est présente.
            db_update_node(nid, **{spec["field"]: tag})
            nmsg = "%s buildée sur %s en %dm%02ds" % (tag, nname, _min, _sec)
            db_add_alert("alert.image.build_noeud_ok", "info", node_id=nid, kind="prep",
                         params={"tag": tag, "n": nname, "min": _min, "sec": _sec})
        else:
            nmsg = "échec build %s sur %s : %s" % (tag, nname, str(tail)[-600:])
            db_add_alert("alert.image.build_noeud_echec", "warning", node_id=nid, kind="prep",
                         params={"tag": tag, "n": nname, "e": str(tail)[-600:]})
        with _img_lock:
            _node_img_build[key] = {"status": "ok" if ok else "error", "msg": nmsg}
        results.append({"id": nid, "name": nname, "ok": ok, "msg": nmsg})
    # Nettoyage du MENSONGE inverse : l'ancien autofill flotte a laissé le tag d'une image node_only
    # sur des nœuds qui ne l'exécutent pas et ne l'ont jamais eue (ex. `nodes.image = bobi-mtl:x` sur
    # un nœud sans io2110). On efface le champ sur les nœuds hors cible dont la valeur porte le
    # préfixe de CETTE image — jamais une valeur d'une autre image.
    tgt_ids = {n["id"] for n in targets}
    pfx = spec["prefix"] + ":"
    for n in db_get_nodes():
        if n["id"] in tgt_ids:
            continue
        cur = (n.get(spec["field"]) or "").strip()
        if cur.startswith(pfx) and not _image_present_node(n, cur):
            db_update_node(n["id"], **{spec["field"]: ""})
            db_add_alert("alert.image.tag_efface", "warning", node_id=n.get("id"), kind="prep",
                         params={"tag": cur, "n": n.get("name")})
    n_ok = sum(1 for r in results if r["ok"])
    n_ko = len(results) - n_ok
    dur = int(_t.time() - start)
    import datetime as _dt
    detail = " · ".join(("✓ " if r["ok"] else "✕ ") + r["name"] for r in results)
    msg = "%s : %d/%d nœud(s) OK en %dm%02ds — %s" % (tag, n_ok, len(results), dur // 60,
                                                      dur % 60, detail)
    with _img_lock:
        _img_build[which] = {"status": "ok" if n_ko == 0 else "error", "msg": msg,
                             "built_at": _dt.datetime.now().strftime("%H:%M:%S"),
                             "nodes": results}
    if n_ko:
        db_add_alert("alert.image.build_incomplet", "error", kind="prep",
                     params={"tag": tag, "n_ko": n_ko, "n_tot": len(results), "detail": detail})


def _build_image_worker(which):
    import shlex as _sh, time as _t
    if _IMAGES[which].get("node_only"):
        # node_only = build PAR NŒUD porteur de la capacité (jamais sur le nœud de build global).
        return _build_node_only_worker(which)
    tag = _image_tag(which)
    host = _build_host()
    local = _build_is_local()
    build_node = None       # nœud de build (si build sur un nœud) → source d'export pour la distribution
    if not local and not host:
        with _img_lock:
            _img_build[which] = {"status": "error", "msg": "aucun hôte de build (Build → « Construire sur »)"}
        return
    start = _t.time()
    with _img_lock:
        _img_build[which] = {"status": "building", "msg": "préparation du contexte…",
                             "step": None, "step_total": None, "phase": "", "sub_done": None,
                             "sub_total": None, "pct": None, "elapsed": 0}
    st = {"step": None, "total": None, "phase": "", "sd": None, "stt": None}
    def _on_line(line):
        m = _STEP_RE.match(line)
        if m:
            st["step"], st["total"] = int(m.group(1)), int(m.group(2))
            st["phase"] = m.group(3).strip()[:90]
            st["sd"] = st["stt"] = None        # nouvelle étape → on remet la sous-barre à zéro
        else:
            mn = _NINJA_RE.search(line)
            if mn:
                st["sd"], st["stt"] = int(mn.group(1)), int(mn.group(2))
        el = int(_t.time() - start)
        parts = []
        if st["step"]:  parts.append("étape %d/%d" % (st["step"], st["total"]))
        if st["phase"]: parts.append(st["phase"])
        if st["stt"]:   parts.append("%d/%d fichiers" % (st["sd"], st["stt"]))
        with _img_lock:
            b = _img_build.get(which)
            if not b or b.get("status") != "building":
                return
            b["msg"] = " · ".join(parts) or "build en cours…"
            b["step"], b["step_total"], b["phase"] = st["step"], st["total"], st["phase"]
            b["sub_done"], b["sub_total"] = st["sd"], st["stt"]
            b["pct"] = round(st["sd"] / st["stt"] * 100) if st["stt"] else None
            b["elapsed"] = el
    try:
        # ★ CONTRÔLE AVANT VOL : l'image de BASE d'une couche fine doit être PRÉSENTE sur la
        # cible. Sans ce contrôle, Docker ne trouve pas le `FROM` en local, part le chercher sur
        # Docker Hub, et rend « pull access denied, repository does not exist or may require
        # authorization ». Le message parle d'AUTORISATION quand la cause est une image
        # MANQUANTE : le testeur est allé vérifier ses identifiants de registre, à l'opposé de la
        # cause (recette Valentin, r620-3/r620-1, 2026-08-21). On échoue ici, en nommant l'image,
        # le nœud, et le geste qui répare.
        _bf = _IMAGES[which].get("base_from")
        if _bf:
            _bt = _image_tag(_bf)
            _cible = None
            if not local:
                from ..database import db_get_node_by_host as _dgnbh
                _cible = _dgnbh(host)
            _presente = (_docker_local_present(_bt) if local
                         else (_image_present_node(_cible, _bt, force=True) if _cible else True))
            if not _presente:
                raise RuntimeError(
                    "image de base « %s » ABSENTE %s : ce build en dérive (FROM). Docker irait la "
                    "chercher sur Docker Hub et échouerait sur « pull access denied » — un message "
                    "d'autorisation pour une cause de disponibilité. Distribuer d'abord « %s » "
                    "(Réglages → Images → construire/distribuer %s), puis relancer ce build."
                    % (_bt, ("localement" if local else "sur le nœud %s"
                             % ((_cible or {}).get("name") or host)), _bt, _bf))
        ctx = _stage_tar(which)
        if local:
            # Build LOCAL (subprocess docker) — contrôleur sur box Docker autonome / bare Debian.
            rc, tail = _build_local_stream(ctx, tag, _on_line, timeout=2400)
            present = _docker_local_present(tag)
            where = "localement"
        else:
            # B3-1 : si l'hôte est un NŒUD-AGENT → build via l'agent (POST contexte tar, plus de
            # root-SSH ; pas de stream live → message « build en cours… » jusqu'au résultat). Sinon
            # SSH brut (legacy) : extrait le contexte dans un tmpdir, build progress=plain streamé.
            from ..database import db_get_node_by_host
            from .. import node_driver as _nd
            _node = db_get_node_by_host(host)
            if _node and _node.get("agent_url"):
                build_node = _node
                with _img_lock:
                    if _img_build.get(which, {}).get("status") == "building":
                        _img_build[which]["msg"] = "build sur le nœud (agent)…"
                rc, tail = _nd.build_image(_node, tag, ctx, timeout=2400)
            else:
                remote = ('D=$(mktemp -d) && tar -xzf - -C "$D" && '
                          'DOCKER_BUILDKIT=1 docker build --progress=plain -t %s -f "$D/Dockerfile" "$D"; '
                          'rc=$?; rm -rf "$D"; exit $rc'
                          % _sh.quote(tag))
                rc, tail = _ssh_stream(host, remote, ctx, _on_line, timeout=2400)
            present = _present_on_build_target(tag, force=True)
            where = host
        if rc == 0 and present:
            filled = _autofill_nodes_image(which, tag)
            # Auto-distribution des BITS aux nœuds concernés (sauf le nœud de build).
            with _img_lock:
                if _img_build.get(which, {}).get("status") == "building":
                    _img_build[which]["msg"] = "distribution aux nœuds…"
            d_ok, d_fail, _dmsg, d_echecs = _distribute_image(which, tag, build_node)
            # Les NOMS, pas le compte : c'est ce qui dit à l'exploitant quel nœud rallumer.
            d_noms = ", ".join(nom or "?" for nom, _m in d_echecs)
            import datetime as _dt
            _ts = _dt.datetime.now().strftime("%H:%M:%S")
            _dur = int(_t.time() - start)
            dist_note = (" · distribuée à %d nœud(s)" % d_ok if d_ok else "") + \
                        (" · %d échec(s) distribution : %s" % (d_fail, d_noms) if d_fail else "")
            with _img_lock:
                _img_build[which] = {"status": "ok",
                                     "msg": "%s buildée %s en %dm%02ds%s%s" % (tag, where,
                                            _dur // 60, _dur % 60,
                                            (" · renseignée sur %d nœud(s)" % filled) if filled else "",
                                            dist_note),
                                     "built_at": _ts}
            # « localement » n'est pas une donnée (adverbe FR) : deux clés selon l'hôte de build,
            # plutôt qu'un fragment français glissé dans un paramètre. Les compteurs (renseignée/
            # distribuée/échecs) restent toujours affichés (donnée pure), au lieu d'être omis à zéro.
            _params = {"tag": tag, "filled": filled or 0, "d_ok": d_ok or 0, "d_fail": d_fail or 0}
            # Deux clés COMPLÈTES plutôt qu'un fragment optionnel en paramètre : la variante « avec
            # échecs » nomme les nœuds ET monte en `warning` — un parc qui diverge n'est pas un
            # `info`. Le paramètre `noms` est une donnée pure (des noms de nœuds), jamais une phrase.
            if d_fail:
                _params["noms"] = d_noms
            _cle = ("alert.image.buildee_locale" if where == "localement"
                    else "alert.image.buildee_noeud") + ("_echecs" if d_fail else "")
            if where != "localement":
                _params["n"] = where
            db_add_alert(_cle, "warning" if d_fail else "info", kind="prep", params=_params)
        else:
            with _img_lock:
                _img_build[which] = {"status": "error", "msg": "échec build : " + tail[-700:]}
    except Exception as e:
        with _img_lock:
            _img_build[which] = {"status": "error", "msg": str(e)}

@bp.route("/api/images/status", methods=["GET"])
@require_perm("settings.edit")
def api_images_status():
    host = _build_host()
    local = _build_is_local()
    out = {"_host": "local" if local else host}
    for which, spec in _IMAGES.items():
        tag = _image_tag(which)
        with _img_lock:
            bs = dict(_img_build[which])
        building = bs["status"] == "building"
        node_only = bool(spec.get("node_only"))
        # node_only : la présence se juge SUR LES NŒUDS QUI EN ONT BESOIN (inventaire agent, caché),
        # pas sur l'hôte de build global — c'est exactement le mensonge « à jour » du bug de prod.
        nodes_state = None
        if node_only:
            nodes_state = [{"id": n["id"], "name": n.get("name"),
                            "present": (True if building else _image_present_node(n, tag))}
                           for n in _image_target_nodes(which)]
            present = bool(nodes_state) and all(n["present"] for n in nodes_state)
        elif building:
            present = True                     # on évite l'inspect quand un build tourne (poll rapide)
        else:
            present = _present_on_build_target(tag)
        out[which] = {"tag": tag, "label": spec["label"],
                      "present": present,
                      "nodes": nodes_state,
                      "targets": (len(nodes_state) if nodes_state is not None else None),
                      "node_only": node_only,
                      "status": bs["status"], "msg": bs["msg"],
                      "built_at": bs.get("built_at"),
                      # Avancement live (build streamé) — null hors build.
                      "step": bs.get("step"), "step_total": bs.get("step_total"),
                      "phase": bs.get("phase"), "pct": bs.get("pct"),
                      "sub_done": bs.get("sub_done"), "sub_total": bs.get("sub_total"),
                      "elapsed": bs.get("elapsed")}
    return jsonify(out)

@bp.route("/api/images/build", methods=["POST"])
@require_perm("settings.edit")
def api_images_build():
    which = (request.json or {}).get("which") or "all"
    # 'all' ne build PAS les images node_only (MTL, compute-gpu) : elles se construisent sur CHAQUE
    # nœud porteur de la capacité (long : clone MTL/CUDA) → build explicite seulement.
    targets = ([w for w in _IMAGES if not _IMAGES[w].get("node_only")] if which == "all"
               else [which])
    started = []
    for w in targets:
        if w not in _IMAGES:
            continue
        with _img_lock:
            if _img_build[w]["status"] == "building":
                continue
            _img_build[w] = {"status": "building", "msg": "en file…"}
        threading.Thread(target=_build_image_worker, args=(w,), daemon=True).start()
        started.append(w)
    return jsonify({"started": started})
