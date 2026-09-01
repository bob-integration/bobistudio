# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Télémétrie GPU par nœud (NVIDIA) — utilisation, mémoire, et surtout l'ÉCHANGE RAM↔GPU.

Le compositing multiview accéléré (cf. banc Phase 0, Tesla T4) est limité non par le calcul
mais par le **transfert PCIe RAM↔GPU** : chaque mur copie les sources de la RAM hôte vers la
VRAM, compose, puis recopie le résultat. À l'échelle de la flotte, c'est le bus PCIe — pas les
SM ni la VRAM — qui plafonne d'abord. Ce module remonte donc, en plus des compteurs classiques
(util SM, VRAM, temp, conso), le **débit PCIe rx/txpci** (`nvidia-smi dmon -s t`) et le compare
à la capacité théorique du lien (gen × largeur) → un indicateur de *headroom* sur l'échange.

Architecture : calquée sur `app/membw.py`. Sampler throttlé appelé depuis la boucle `surveillance`,
qui SSHe `nvidia-smi` sur l'hôte de chaque nœud via le choke-point `host_ops.ssh_run` (agent
`/v1/host/exec` ou SSH legacy). Le dernier relevé par nœud est caché en RAM ; `node_health`
fusionne ce cache dans chaque snapshot (`_merge_gpu`, non bloquant) pour l'UI Monitoring. Les nœuds
sans GPU sont détectés une fois (nvidia-smi absent) puis re-sondés rarement, pour ne pas SSHer en
boucle dans le vide.
"""
import logging
import time

from .host_ops import ssh_run
from .database import db_get_nodes
from . import settings as S

log = logging.getLogger("gpu")

# Capacité PCIe USABLE par voie et par sens (MB/s ≈ MiB/s, suffisant pour un % de headroom).
# PCIe 3.0 = 8 GT/s ≈ 985 MB/s/voie ; ×16 ≈ 15,8 GB/s/sens.
_PCIE_LANE_MBPS = {1: 250, 2: 500, 3: 985, 4: 1969, 5: 3938}

# Champs interrogés en une passe (ordre = parsing positionnel). nounits → valeurs nues.
_QUERY_FIELDS = (
    "index,name,driver_version,utilization.gpu,utilization.memory,"
    "memory.used,memory.total,temperature.gpu,power.draw,power.limit,"
    "clocks.sm,clocks.mem,pcie.link.gen.current,pcie.link.gen.max,"
    "pcie.link.width.current,pcie.link.width.max,utilization.encoder,utilization.decoder"
)
# Une seule connexion SSH : query-gpu (CSV), un marqueur, puis dmon -s t (rx/txpci par GPU).
_PROBE_CMD = (
    "nvidia-smi --query-gpu=" + _QUERY_FIELDS + " --format=csv,noheader,nounits"
    " && echo '@@DMON@@' && nvidia-smi dmon -s t -c 1"
)

# État en mémoire (process orchestrateur).
_last = {}            # node_id -> {"gpus":[...], "ts":...}
_absent = {}          # node_id -> monotone du dernier constat « pas de GPU » (re-sonde espacée)
_last_sample_m = 0.0  # throttle global


def _cfg(key, default):
    try:
        v = S.get(key)
        return type(default)(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _num(s, cast=float):
    """Parse tolérant : '[N/A]', '', 'Insufficient Permissions' → None."""
    s = (s or "").strip()
    if not s or s.lower().startswith(("[n/a", "n/a", "insufficient", "unknown")):
        return None
    try:
        return cast(s)
    except (TypeError, ValueError):
        return None


def _parse_dmon(text):
    """`nvidia-smi dmon -s t -c 1` → {gpu_index: (rx_mbps, tx_mbps)}. Les 2 dernières colonnes
    numériques d'une ligne de données sont rxpci/txpci ; la 1ʳᵉ est l'index GPU."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split()
        if len(cols) < 3:
            continue
        try:
            idx = int(cols[0])
            rx = _num(cols[-2]); tx = _num(cols[-1])
        except (ValueError, IndexError):
            continue
        out[idx] = (rx, tx)
    return out


def _parse(out):
    """Parse la sortie combinée du _PROBE_CMD → liste de dict GPU (un par carte)."""
    csv_part, _, dmon_part = (out or "").partition("@@DMON@@")
    pcie = _parse_dmon(dmon_part)
    gpus = []
    for line in csv_part.splitlines():
        line = line.strip()
        if not line:
            continue
        f = [c.strip() for c in line.split(",")]
        if len(f) < 18:
            continue
        idx = int(_num(f[0], int) or 0)
        mem_used = _num(f[5]); mem_total = _num(f[6])
        gen = _num(f[12], int); width = _num(f[14], int)
        rx, tx = pcie.get(idx, (None, None))
        link_mbps = None
        if gen and width:
            link_mbps = _PCIE_LANE_MBPS.get(int(gen), 985) * int(width)
        pcie_pct = None
        if link_mbps and (rx is not None or tx is not None):
            pcie_pct = round(max(rx or 0, tx or 0) / link_mbps * 100, 1)
        power = _num(f[8]); plimit = _num(f[9])
        gpus.append({
            "index": idx,
            "name": f[1] or None,
            "driver": f[2] or None,
            "util_pct": _num(f[3]),
            "mem_util_pct": _num(f[4]),         # % du temps où le bus VRAM est sollicité
            "mem_used_mb": mem_used,
            "mem_total_mb": mem_total,
            "mem_pct": round(mem_used / mem_total * 100, 1) if (mem_used and mem_total) else None,
            "temp_c": _num(f[7]),
            "power_w": power,
            "power_limit_w": plimit,
            "power_pct": round(power / plimit * 100, 1) if (power and plimit) else None,
            "clock_sm_mhz": _num(f[10], int),
            "clock_mem_mhz": _num(f[11], int),
            "enc_pct": _num(f[16]),
            "dec_pct": _num(f[17]),
            "pcie_gen": gen,
            "pcie_gen_max": _num(f[13], int),
            "pcie_width": width,
            "pcie_width_max": _num(f[15], int),
            "pcie_rx_mbps": rx,                 # RAM → GPU (host-to-device)
            "pcie_tx_mbps": tx,                 # GPU → RAM (device-to-host)
            "pcie_link_mbps": link_mbps,        # capacité théorique du lien (gen × largeur)
            "pcie_pct": pcie_pct,               # % du lien utilisé (headroom de l'échange)
        })
    return gpus


def measure_host(host):
    """SSHe nvidia-smi sur `host`. Retourne (liste GPU) ou None si pas de GPU / erreur."""
    try:
        rc, out, err = ssh_run(host, _PROBE_CMD, timeout=20)
    except Exception as e:
        log.debug("gpu ssh %s: %s", host, e)
        return None
    if rc != 0:
        # nvidia-smi absent (command not found) ou pas de carte → nœud sans GPU.
        return None
    gpus = _parse(out)
    return gpus or None


def sample_all(force=False):
    """Échantillonne le GPU de chaque nœud (throttlé `gpu_sample_interval_s`, défaut 5 s).
    Appelée depuis `surveillance`. No-op tant que l'intervalle n'est pas écoulé. Les nœuds sans
    GPU sont mis en quarantaine (re-sonde toutes les `gpu_absent_recheck_s`, défaut 300 s)."""
    global _last_sample_m
    if not _cfg("gpu_enabled", 1):
        return
    interval = _cfg("gpu_sample_interval_s", 5)
    now_m = time.monotonic()
    if not force and (now_m - _last_sample_m) < interval:
        return
    _last_sample_m = now_m

    recheck = _cfg("gpu_absent_recheck_s", 300)
    nodes = [n for n in (db_get_nodes() or []) if n.get("id") is not None and n.get("host")]
    for n in nodes:
        nid = n["id"]; host = n["host"]
        if not force and nid in _absent and (now_m - _absent[nid]) < recheck:
            continue
        gpus = measure_host(host)
        if gpus:
            _absent.pop(nid, None)
            _last[nid] = {"gpus": gpus, "ts": time.time()}
        else:
            _absent[nid] = now_m
            _last.pop(nid, None)


def latest():
    """{node_id -> {"gpus":[...], "ts":...}} pour les nœuds porteurs d'un GPU."""
    return dict(_last)
