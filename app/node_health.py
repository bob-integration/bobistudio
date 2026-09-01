# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Santé matérielle des nœuds (remplacement de la console Proxmox).

En déployant des nœuds SANS Proxmox (cf. NODE_AGENT.md), on perd la seule vue de santé
serveur (CPU/RAM/disque/versions). Ce module la reconstruit côté contrôleur :

- **Source des données** : l'agent-nœud `/v1/health` (via `node_driver.health`) pour les nœuds
  enrôlés ; un collecteur python one-shot par `ssh_run` pour les nœuds legacy sans agent ; une
  lecture LOCALE de `/proc` + `shutil.disk_usage` pour l'hôte du CONTRÔLEUR lui-même (pseudo-nœud
  `"controller"`, utile en mode collapse 1-box).
- **Modèle** : calqué sur `app/membw.py` (sampler throttlé depuis `surveillance`) + `app/ptp.py`
  (ring 10 min en RAM pour les sparklines + agrégats 24 h flushés en JSON, rechargés au boot).
- **Alertes par transition** (réuse `db_add_alert`) : remplissage disque au-delà d'un seuil.

La bande passante mémoire (`membw`) est fusionnée dans chaque snapshot pour l'UI.
"""
import json
import logging
import os
import platform
import shutil
import threading
import time
from collections import deque

from .config import DB_PATH
from .database import db_get_nodes, db_add_alert
from .episodes import EtatEpisodes as _Episodes
from . import settings as S

log = logging.getLogger(__name__)

# ─── Cadence & rétention (mêmes ordres de grandeur que ptp.py) ───────────────
SAMPLE_INTERVAL_S = 5
HISTORY_SECONDS   = 600                                   # 10 min
HISTORY_MAX       = HISTORY_SECONDS // SAMPLE_INTERVAL_S   # 120 points (sparklines)
STATS_SECONDS     = 86400                                  # 24 h
STATS_MAX         = STATS_SECONDS // SAMPLE_INTERVAL_S     # 17 280 points
STATS_PERSIST_PATH = os.path.join(os.path.dirname(DB_PATH) or ".", "node_health_stats.json")
STATS_FLUSH_S      = 300                                   # flush 24 h toutes les 5 min

# ─── État en mémoire (process contrôleur) ────────────────────────────────────
_last = {}            # node_key → dernier snapshot complet (pour l'API/UI)
_hist = {}            # node_key → deque(maxlen=HISTORY_MAX) (sparklines 10 min)
_stats = {}           # node_key → deque(maxlen=STATS_MAX)  (agrégats 24 h)
_lock = threading.Lock()
_alert_state = {}     # node_key → None|"warning"|"error" (disque ; cache RAM du chemin chaud)
_episodes = _Episodes("node_disk")        # idem : l'épisode disque survit au redémarrage
_last_sample_m = 0.0  # monotone du dernier passage (throttle global)
_last_flush = 0.0

# État pour les deltas LOCAUX du contrôleur (CPU %, débit réseau).
_ctl_prev_cpu = {}
_ctl_prev_net = {}

CONTROLLER_KEY = "controller"


def _cfg(key, default):
    try:
        v = S.get(key)
        return type(default)(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ─── Lecture LOCALE (hôte contrôleur) ────────────────────────────────────────
def _local_cpu_real_pct():
    try:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
    except Exception:
        return None
    prev_total = _ctl_prev_cpu.get("total")
    pct = None
    if prev_total is not None and total - prev_total > 0:
        pct = round((1.0 - (idle - _ctl_prev_cpu.get("idle", 0)) / (total - prev_total)) * 100, 1)
    _ctl_prev_cpu["total"], _ctl_prev_cpu["idle"] = total, idle
    return pct


def _local_resources():
    res = {"cpu_pct": None, "cpu_pct_real": _local_cpu_real_pct(), "loadavg": None,
           "cpu_count": os.cpu_count(), "cpu_model": None,
           "mem_used_mb": None, "mem_total_mb": None,
           "swap_used_mb": None, "swap_total_mb": None}
    try:
        with open("/proc/meminfo") as f:
            mi = {}
            for line in f:
                k, _, v = line.partition(":")
                mi[k] = int(v.split()[0]) if v.split() else 0
        res["mem_total_mb"] = mi.get("MemTotal", 0) // 1024
        res["mem_used_mb"] = res["mem_total_mb"] - mi.get("MemAvailable", 0) // 1024
        res["swap_total_mb"] = mi.get("SwapTotal", 0) // 1024
        res["swap_used_mb"] = res["swap_total_mb"] - mi.get("SwapFree", 0) // 1024
    except Exception:
        pass
    try:
        la = os.getloadavg()
        res["loadavg"] = [round(x, 2) for x in la]
        res["cpu_pct"] = round(la[0] / (os.cpu_count() or 1) * 100, 1)
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    res["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass
    return res


def _local_disks():
    out = {}
    for key, path in {"shm": "/dev/shm", "root": "/"}.items():
        try:
            u = shutil.disk_usage(path)
            out[key] = {"path": path, "used": u.used, "total": u.total,
                        "pct": round(u.used / u.total * 100, 1) if u.total else None}
        except Exception:
            continue
    return out


def _local_net():
    now = time.time()
    out = {}
    try:
        names = os.listdir("/sys/class/net")
    except OSError:
        return out
    for name in sorted(names):
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr")):
            continue
        try:
            with open(f"/sys/class/net/{name}/statistics/rx_bytes") as f:
                rx = int(f.read().strip())
            with open(f"/sys/class/net/{name}/statistics/tx_bytes") as f:
                tx = int(f.read().strip())
        except Exception:
            continue
        prev = _ctl_prev_net.get(name)
        if prev and now - prev["ts"] > 0:
            dt = now - prev["ts"]
            out[name] = {"rx_bps": round((rx - prev["rx"]) * 8 / dt),
                         "tx_bps": round((tx - prev["tx"]) * 8 / dt)}
        _ctl_prev_net[name] = {"rx": rx, "tx": tx, "ts": now}
    return out


def _local_versions():
    v = {"kernel": platform.release(), "os": None}
    try:
        with open("/etc/os-release") as f:
            kv = dict(line.partition("=")[::2] for line in f if "=" in line)
        v["os"] = (kv.get("PRETTY_NAME") or kv.get("NAME") or "").strip().strip('"') or None
    except Exception:
        pass
    return v


def _local_host_uptime_s():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return None


def _controller_snapshot():
    """Snapshot de l'hôte du CONTRÔLEUR lui-même (lecture /proc locale, aucun réseau)."""
    return {
        "ts": time.time(), "ok": True, "is_controller": True,
        "name": platform.node(), "host": "127.0.0.1",
        "host_uptime_s": _local_host_uptime_s(),
        "resources": _local_resources(),
        "disks": _local_disks(),
        "net": _local_net(),
        "versions": _local_versions(),
    }


# ─── Collecteur legacy (nœud sans agent, via ssh_run) ────────────────────────
# python one-shot émettant le même schéma en JSON (pas de delta CPU/net : one-shot).
_COLLECTOR_PY = (
    "import json,os,platform,shutil\n"
    "r={'cpu_count':os.cpu_count(),'cpu_model':None,'cpu_pct':None,'loadavg':None,"
    "'mem_used_mb':None,'mem_total_mb':None,'swap_used_mb':None,'swap_total_mb':None}\n"
    "try:\n"
    " mi={}\n"
    " [mi.__setitem__(l.split(':')[0],int(l.split()[1])) for l in open('/proc/meminfo') if len(l.split())>1]\n"
    " r['mem_total_mb']=mi.get('MemTotal',0)//1024; r['mem_used_mb']=r['mem_total_mb']-mi.get('MemAvailable',0)//1024\n"
    " r['swap_total_mb']=mi.get('SwapTotal',0)//1024; r['swap_used_mb']=r['swap_total_mb']-mi.get('SwapFree',0)//1024\n"
    "except Exception: pass\n"
    "try:\n"
    " la=os.getloadavg(); r['loadavg']=[round(x,2) for x in la]; r['cpu_pct']=round(la[0]/(os.cpu_count() or 1)*100,1)\n"
    "except Exception: pass\n"
    "try:\n"
    " r['cpu_model']=[l.split(':',1)[1].strip() for l in open('/proc/cpuinfo') if l.startswith('model name')][0]\n"
    "except Exception: pass\n"
    "d={}\n"
    "for k,p in (('shm','/dev/shm'),('root','/')):\n"
    " try:\n"
    "  u=shutil.disk_usage(p); d[k]={'path':p,'used':u.used,'total':u.total,'pct':round(u.used/u.total*100,1) if u.total else None}\n"
    " except Exception: pass\n"
    "v={'kernel':platform.release(),'os':None}\n"
    "try:\n"
    " kv=dict(l.rstrip().split('=',1) for l in open('/etc/os-release') if '=' in l)\n"
    " v['os']=(kv.get('PRETTY_NAME') or kv.get('NAME') or '').strip('\"') or None\n"
    "except Exception: pass\n"
    "up=None\n"
    "try: up=int(float(open('/proc/uptime').read().split()[0]))\n"
    "except Exception: pass\n"
    "print(json.dumps({'resources':r,'disks':d,'versions':v,'host_uptime_s':up}))\n"
)


def _ssh_snapshot(host):
    from .host_ops import ssh_run
    try:
        rc, out, err = ssh_run(host, "python3 -", input_data=_COLLECTOR_PY, timeout=20)
    except Exception as e:
        log.debug("node_health ssh %s: %s", host, e)
        return None
    if rc != 0:
        log.debug("node_health ssh %s: rc=%s %r", host, rc, (err or out)[:200])
        return None
    try:
        data = json.loads((out or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    data.update({"ts": time.time(), "ok": True})
    return data


# ─── Échantillonnage ─────────────────────────────────────────────────────────
def _hist_dq(key):
    return _hist.setdefault(str(key), deque(maxlen=HISTORY_MAX))


def _stats_dq(key):
    return _stats.setdefault(str(key), deque(maxlen=STATS_MAX))


def _record(key, snap):
    """Range le snapshot dans _last + pousse un point réduit dans les rings 10 min / 24 h."""
    res = snap.get("resources") or {}
    disks = snap.get("disks") or {}
    mb = snap.get("membw") or {}
    cpu = res.get("cpu_pct_real")
    if cpu is None:
        cpu = res.get("cpu_pct")
    mem_pct = None
    if res.get("mem_total_mb"):
        mem_pct = round((res.get("mem_used_mb") or 0) / res["mem_total_mb"] * 100, 1)
    point = {
        "t": snap.get("ts", time.time()),
        "cpu": cpu,
        "mem": mem_pct,
        "shm": (disks.get("shm") or {}).get("pct"),
        "membw": mb.get("gbps"),
        "ptp_off": (snap.get("ptp") or {}).get("offset_ns"),   # offset PTP (ns) → mini-graphe UI
        # Charge des cœurs ORDONNANÇABLES seuls (cf. _merge_cpu_partage) : sur un nœud très isolé
        # c'est la seule courbe qui dise quelque chose — `cpu` moyenne les 42 cœurs réservés avec
        # les 6 qui portent réellement le système, et reste au vert pendant la saturation.
        "cpu_ord": ((snap.get("cpu_partage") or {}).get("ordonnancables") or {}).get("pct"),
    }
    with _lock:
        _last[str(key)] = snap
        _hist_dq(key).append(point)
        _stats_dq(key).append(point)


def _merge_cpu_partage(nid, snap):
    """Partage de la machine entre cœurs ISOLÉS (busy-poll DPDK) et cœurs ORDONNANÇABLES (tout le
    reste), avec la charge réelle de chaque groupe.

    ★ POURQUOI CE CHIFFRE. Un `cpu_pct` global est une moyenne sur toute la machine : sur un nœud
    où 42 CPU sur 48 sont isolés, il noie DEUX régimes opposés dans un seul nombre rassurant.
    Mesuré sur dl360-1 le 2026-08-01 :

        42 CPU isolés          21 % chacun   ← 10 boucles busy-poll, le reste réservé pour rien
         6 CPU ordonnançables  95 % chacun   ← noyau, dockerd, conteneurs, threads de service

    …pour un `cpu_pct` global de ~30 %, parfaitement au vert. C'est LE chiffre qui a expliqué une
    journée entière de symptômes, et il n'était calculé nulle part. Cf. [[silent-failure-antipattern]].

    Gratuit : `cpu_per_core` vient déjà de l'agent (0.16.0+), la bande isolée est cachée par
    `core_pool`. Aucun appel réseau supplémentaire. Absent (agent ancien, nœud sans isolation) →
    champ non posé, jamais de valeur inventée."""
    from . import core_pool
    per_core = ((snap.get("resources") or {}).get("cpu_per_core")) or []
    if not per_core:
        return
    iso = core_pool.isolated_cached(nid)
    if not iso:
        return                      # None = illisible, set() = nœud sans isolation : rien à partager
    n = len(per_core)
    grp = {"isoles": [], "ordonnancables": []}
    for cpu, pct in enumerate(per_core):
        if pct is None:
            continue
        grp["isoles" if cpu in iso else "ordonnancables"].append(float(pct))
    if not grp["ordonnancables"]:
        return
    out = {}
    for k, vals in grp.items():
        cpus = [c for c in range(n) if (c in iso) == (k == "isoles")]
        out[k] = {"n": len(cpus), "cpuset": core_pool.fmt_cpuset(cpus),
                  "pct": round(sum(vals) / len(vals), 1) if vals else None,
                  "cumul_pct": round(sum(vals), 0) if vals else None}
    out["part_isolee_pct"] = round(100.0 * out["isoles"]["n"] / n, 1) if n else None
    snap["cpu_partage"] = out


def _check_disk_alert(key, name, disks):
    """Alerte par transition sur le remplissage disque (max sur shm/root/media)."""
    warn = _cfg("node_health_disk_warn_pct", 85.0)
    err = _cfg("node_health_disk_err_pct", 95.0)
    worst = None
    worst_mount = None
    for mk, d in (disks or {}).items():
        pct = d.get("pct")
        if pct is not None and (worst is None or pct > worst):
            worst, worst_mount = pct, d.get("path") or mk
    level = None
    if worst is not None:
        level = "error" if worst >= err else ("warning" if worst >= warn else None)
    # Épisode PERSISTÉ + hystérésis (balayage des alarmes du 2026-07-27) : un disque plein le reste
    # au travers d'un redémarrage du service, et un remplissage qui oscille autour du seuil (85 %
    # est exactement le régime d'un disque qui se remplit lentement) notifiait chaque franchissement.
    # Le retour à la normale exige une MARGE (`node_health_disk_clear_margin`, 5 points par défaut).
    k = str(key)
    prev = _alert_state.get(k)
    if prev is None and k not in _alert_state:
        prev = _episodes.get(k)
        _alert_state[k] = prev
    marge = _cfg("node_health_disk_clear_margin", 5.0)
    if level and level != prev:
        db_add_alert("alert.disk.saturation", level, node_id=key, kind="disk",
                     params={"mount": worst_mount, "n": name, "pct": worst})
        _alert_state[k] = level
        _episodes.poser(k, level)
    elif not level and prev and (worst is None or worst <= warn - marge):
        db_add_alert("alert.disk.retabli", "info", node_id=key, kind="disk",
                     params={"n": name, "pct": worst})
        _alert_state[k] = None
        _episodes.retirer(k)


# ─── Dérive de la prép hôte MTL (hors reboot) ────────────────────────────────────────
# La vérification post-boot (`node_recovery`) attrape ce qui casse AU redémarrage. Mais la prép
# peut se dégrader SANS reboot : unité systemd qui passe en `failed`, `irqbalance` réinstallé qui
# ré-étale les IRQ sur la bande isolée, cmdline édité à la main, ou simplement `mtl_lcore_max`
# modifié dans les réglages — auquel cas la bande ATTENDUE change et celle qui est active devient
# obsolète. Sans ce contrôle, ces cas ne se voyaient que si un humain ouvrait le panneau du nœud.
#
# Cadence LENTE (défaut 30 min) : la sonde est un aller-retour agent complet, hors de question de
# la jouer au rythme du sampler (5 s). Alerte À TRANSITION (pattern `_check_disk_alert`) : on ne
# répète pas une anomalie qui persiste, et le retour à la normale est signalé.
_prep_drift_at = {}      # node_id → time.monotonic() du prochain contrôle
# L'état de transition vit dans node_recovery (`_prep_alert_state`), PARTAGÉ avec la passe
# post-boot — ici on ne garde que la cadence.
PREP_DRIFT_INTERVAL_S = 1800


def _check_prep_drift(node_id, name, node):
    """Re-sonde périodiquement la prép hôte MTL d'un nœud 2110 et alerte à la transition.
    Best-effort, jamais bloquant : au moindre pépin on log en debug et on repart au tour suivant."""
    try:
        if not _cfg("node_health_prep_drift_enabled", 1):
            return
        now_m = time.monotonic()
        due = _prep_drift_at.get(node_id)
        if due is not None and now_m < due:
            return
        _prep_drift_at[node_id] = now_m + _cfg("node_health_prep_drift_interval_s",
                                               PREP_DRIFT_INTERVAL_S)
        if due is None:
            return               # 1er passage : on ARME seulement (pas de sonde au démarrage du
                                 # contrôleur — le post-boot s'en charge, inutile de doubler).
        _sonder_prep(node_id, name, node, "dérive détectée")
    except Exception as e:
        log.debug("node_health dérive prép nœud %s: %s", node_id, e)


# ─── Dérive du gouverneur de fréquence (TOUS les nœuds) ──────────────────────────────
# ★ POURQUOI CE CONTRÔLE EXISTE, ET POURQUOI IL N'EST PAS DANS LA PRÉP MTL.
# La sonde de prép ci-dessus est conditionnée à la capacité `io2110` : un nœud qui ne fait que
# du compute (murs, traitements) n'est JAMAIS vérifié. Or le couplage fréquence×temps-réel n'a
# rien de spécifique au 2110 — mesuré le 2026-08-08 sur dell-1 (aucune capacité io2110) : le
# gouverneur `schedutil` garait les cœurs du mur 906 à 1,2 GHz pour 3,6 max, parce qu'un fil de
# compo sérialisé par le GIL n'occupe que 57 % d'UN cœur sur trois alloués et que les cœurs
# paraissent donc oisifs. Résultat : des effondrements à 25 fps, une à deux fois par minute,
# avec TOUS les postes de compo qui doublent ensemble. A/B alterné : 112 → 68 trames perdues/min
# et zéro effondrement sur 300 s. Le réglage se pose à chaud mais un REBOOT le perd — et sans
# ce contrôle, la régression serait totalement muette. Cf. [[silent-failure-antipattern]].
#
# Cadence lente et alerte à transition, comme la dérive de prép : c'est un aller-retour agent.
_freq_drift_at = {}          # node_id → time.monotonic() du prochain contrôle
_freq_alert_state = {}       # node_id → dernier niveau alerté (None = sain)
FREQ_DRIFT_INTERVAL_S = 1800

_FREQ_PROBE = (
    "n=0; p=0; "
    "for d in /sys/devices/system/cpu/cpu*/cpufreq; do [ -d \"$d\" ] || continue; "
    "n=$((n+1)); "
    "[ \"$(cat $d/scaling_governor 2>/dev/null)\" = performance ] && p=$((p+1)); done; "
    "echo \"$n $p $(systemctl is-enabled bobi-cpufreq-perf.service 2>/dev/null)\""
)


def _check_freq_drift(node_id, name, node):
    """Vérifie périodiquement que TOUS les cœurs du nœud sont en gouverneur `performance`, et
    alerte à la transition. Best-effort, jamais bloquant.

    Un nœud sans interface cpufreq (VM, pilote absent) rend `n=0` : ce n'est PAS un défaut, le
    couplage n'existe pas là — on sort sans rien dire, comme le script lui-même."""
    try:
        if not _cfg("node_health_freq_drift_enabled", 1):
            return
        now_m = time.monotonic()
        due = _freq_drift_at.get(node_id)
        if due is not None and now_m < due:
            return
        _freq_drift_at[node_id] = now_m + _cfg("node_health_freq_drift_interval_s",
                                               FREQ_DRIFT_INTERVAL_S)
        if due is None:
            return               # 1er passage : on ARME seulement (cf. _check_prep_drift).
        from . import node_driver
        rc, out, _err = node_driver.host_exec(node, _FREQ_PROBE, timeout=30)
        parts = (out or "").split()
        if rc != 0 or len(parts) < 2:
            return               # sonde illisible : on ne fabrique pas un verdict
        total, perf = int(parts[0]), int(parts[1])
        unit_ok = len(parts) > 2 and parts[2].strip() == "enabled"
        if total == 0:
            return               # pas d'interface cpufreq → rien à garder
        casse = perf < total
        etat = _freq_alert_state.get(node_id)
        if casse and etat is None:
            cle = ("alert.prep.freq_drift" if unit_ok
                   else "alert.prep.freq_drift_unit_absente")
            db_add_alert(cle, "error", node_id=node_id, kind="prep",
                         params={"n": name, "n_defaut": total - perf, "n_total": total})
            _freq_alert_state[node_id] = "error"
        elif not casse and etat is not None:
            db_add_alert("alert.prep.frequence_retablie", "info", node_id=node_id, kind="prep",
                         params={"n": name, "total": total})
            _freq_alert_state[node_id] = None
    except Exception as e:
        log.debug("node_health dérive fréquence nœud %s: %s", node_id, e)


# ─── Dérive de la profondeur des ring buffers MXL (TOUS les nœuds) ───────────────────────────
# Même schéma que la dérive de fréquence ci-dessus : `/dev/shm/mxl/options.json` vit dans un
# tmpfs (perdu au reboot, cf. `mtl.ensure_mxl_history`), et RIEN ne dit qu'il a survécu au
# dernier redémarrage tant qu'on ne le vérifie pas. RÈGLE DU PROJET (cf. [[alarm-must-compare-
# to-intent]]) : on ne compare PAS à une constante en dur, on compare à l'INTENTION — le réglage
# `mxl_history_ms` — pour que l'alarme reste correcte si l'exploitant change ce réglage.
_mxl_history_drift_at = {}       # node_id → time.monotonic() du prochain contrôle
_mxl_history_alert_state = {}    # node_id → dernier niveau alerté (None = sain)
MXL_HISTORY_DRIFT_INTERVAL_S = 1800

# ★ On sonde AUSSI l'existence du domaine. Un nœud enrôlé qui n'a jamais fait tourner de
# conteneur MXL n'a pas de `/dev/shm/mxl` du tout : il n'y a alors AUCUNE intention à laquelle
# comparer, et alerter reviendrait à reprocher à un nœud de ne pas servir un bus qu'on ne lui a
# jamais demandé de servir. Le domaine est créé par `mxlCreateInstance` (bobimxl fait un
# `makedirs`) ou par la prép hôte — sa présence est donc le signal « ce nœud porte du MXL ».
_MXL_HISTORY_PROBE = (
    "d=/dev/shm/mxl; [ -d \"$d\" ] || { echo 'sansdomaine -'; exit 0; }; "
    "f=/dev/shm/mxl/options.json; "
    "if [ -f \"$f\" ]; then "
    "  cur=$(grep -o '\"urn:x-mxl:option:history_duration/v1.0\"[[:space:]]*:[[:space:]]*[0-9]*' \"$f\" "
    "        | grep -o '[0-9]*$'); "
    "fi; "
    # Persistance au boot : config tmpfiles.d (et non plus une unité systemd — poser une unité
    # exige un daemon-reload, qui révoque le GPU des conteneurs en marche, cf. mtl.MXL_TMPFILES_PATH).
    # 3e champ — SECONDS DOMAINES. Le SDK règle la profondeur par DOMAINE ; tout notre outillage
    # (pose du fichier, tmpfiles.d, cette sonde) suppose qu'un nœud n'en a qu'un, le défaut. Un
    # conteneur qui poserait `MXL_DOMAIN` ailleurs créerait un domaine sans `options.json`, donc
    # à 200 ms, INVISIBLE partout — on compte donc les valeurs non-défaut plutôt que de croire
    # l'hypothèse sur parole. Un domaine n'existe que si un conteneur l'utilise : inspecter les
    # conteneurs suffit, inutile de balayer le disque.
    "n=$(docker ps -q 2>/dev/null | xargs -r docker inspect "
    "     -f '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null "
    "   | grep '^MXL_DOMAIN=' | grep -v '^MXL_DOMAIN=/dev/shm/mxl$' | sort -u | wc -l); "
    "echo \"${cur:-absent} $([ -f /etc/tmpfiles.d/bobi-mxl.conf ] && echo persistant || echo volatil) ${n:-0}\""
)


def _check_mxl_history_drift(node_id, name, node):
    """Vérifie périodiquement que `/dev/shm/mxl/options.json` (profondeur des ring buffers MXL,
    cf. `mtl.ensure_mxl_history`) porte bien la durée voulue par le réglage `mxl_history_ms`, et
    alerte à la transition. Best-effort, jamais bloquant.

    Compare à l'INTENTION (le réglage), pas à une constante : un nœud qui applique fidèlement
    une valeur non-défaut ne doit JAMAIS alerter."""
    try:
        if not _cfg("node_health_mxl_history_drift_enabled", 1):
            return
        now_m = time.monotonic()
        due = _mxl_history_drift_at.get(node_id)
        if due is not None and now_m < due:
            return
        _mxl_history_drift_at[node_id] = now_m + _cfg(
            "node_health_mxl_history_drift_interval_s", MXL_HISTORY_DRIFT_INTERVAL_S)
        if due is None:
            return               # 1er passage : on ARME seulement (cf. _check_prep_drift).
        from . import node_driver, mtl
        rc, out, _err = node_driver.host_exec(node, _MXL_HISTORY_PROBE, timeout=30)
        parts = (out or "").split()
        if rc != 0 or not parts:
            return               # sonde illisible : on ne fabrique pas un verdict
        cur_raw = parts[0]
        if cur_raw == "sansdomaine":
            # Pas de domaine MXL sur ce nœud → rien à vérifier. On DÉSARME une alerte
            # éventuellement en cours (un nœud vidé de ses conteneurs MXL doit se taire).
            if _mxl_history_alert_state.get(node_id) is not None:
                _mxl_history_alert_state[node_id] = None
            return
        unit_ok = len(parts) > 1 and parts[1].strip() == "persistant"
        want_ms = _cfg("mxl_history_ms", mtl.MXL_HISTORY_MS_DEFAULT)
        want_ns = int(want_ms) * 1_000_000
        cur_ns = int(cur_raw) if cur_raw.isdigit() else None
        # Second domaine détecté → notre hypothèse « un nœud = un domaine » est tombée, et le
        # réglage ne couvre PAS ce domaine-là. On le dit plutôt que de continuer à supposer.
        autres = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else 0
        casse = (cur_ns != want_ns) or autres > 0
        etat = _mxl_history_alert_state.get(node_id)
        if casse and etat is None:
            # « profondeur inconnue » (fichier introuvable) est une VALEUR DE REPLI française : elle
            # ne peut pas voyager en paramètre (piège n°3), donc clé complète par branche — de même
            # pour les deux suffixes conditionnels (unité tmpfiles.d, domaines secondaires), qui sont
            # des demi-phrases de diagnostic et non des données. 2×2×2 = 8 clés, une par combinaison.
            cle = "alert.prep.mxl_profondeur_derive_{}_{}_{}".format(
                "connue" if cur_ns is not None else "inconnue",
                "unitok" if unit_ok else "unitko",
                "autres" if autres else "seul")
            params = {"n": name, "want_ms": want_ms}
            if cur_ns is not None:
                params["cur_ms"] = cur_ns // 1_000_000
            if autres:
                params["autres"] = autres
            db_add_alert(cle, "error", node_id=node_id, kind="prep", params=params)
            _mxl_history_alert_state[node_id] = "error"
        elif not casse and etat is not None:
            db_add_alert("alert.prep.mxl_profondeur_retablie", "info", node_id=node_id,
                         kind="prep", params={"n": name, "want_ms": want_ms})
            _mxl_history_alert_state[node_id] = None
    except Exception as e:
        log.debug("node_health dérive profondeur MXL nœud %s: %s", node_id, e)


# ─── Identité du domaine MXL : `domain_def.json` (BCP-007-03) ────────────────────────────────
# Pendant de `_check_mxl_history_drift`, avec une différence de NATURE : ici on RÉPARE au lieu
# d'alerter. Une alerte se justifie quand la remise en état demande un arbitrage (la profondeur
# des ring buffers ne prend effet qu'en recréant les flux : c'est à l'exploitant de choisir son
# moment). `domain_def.json` est purement DESCRIPTIF — aucun conteneur en marche n'en dépend, le
# réécrire ne perturbe rien — donc lever une alerte « allez cliquer sur Préparation hôte » serait
# du bruit pour une chose qu'on sait faire soi-même. Le tmpfs le perd à chaque reboot ; c'est ce
# passage-là qui le repose sur les nœuds qui ne repassent jamais par la prép hôte MTL.
_mxl_domain_def_at = {}          # node_id → time.monotonic() du prochain contrôle
MXL_DOMAIN_DEF_INTERVAL_S = 1800

# ★ Même garde que la sonde de profondeur : PAS de `/dev/shm/mxl` = ce nœud ne porte pas de MXL,
# et on ne va pas lui fabriquer un domaine vide juste pour le nommer. Ce serait doublement
# nuisible : la présence du répertoire est précisément le signal sur lequel `_check_mxl_history_
# drift` décide s'il a quelque chose à vérifier — créer le domaine partout le ferait alerter sur
# des nœuds qui n'ont jamais servi une trame.
_MXL_DOMAIN_DEF_PROBE = (
    "d=/dev/shm/mxl; [ -d \"$d\" ] || { echo sansdomaine; exit 0; }; "
    "f=$d/domain_def.json; [ -f \"$f\" ] || { echo absent; exit 0; }; "
    "grep -o '\"id\"[[:space:]]*:[[:space:]]*\"[^\"]*\"' \"$f\" | head -1 "
    "  | sed 's/.*\"\\([^\"]*\\)\"$/\\1/' | grep . || echo sansid"
)


def _check_mxl_domain_def(node_id, name, node):
    """Vérifie périodiquement que le domaine MXL du nœud porte son `domain_def.json` (BCP-007-03)
    et le REPOSE s'il manque ou porte une autre identité. Best-effort, jamais bloquant.

    Ne touche RIEN sur un nœud sans domaine MXL, et n'écrit que si le fichier vivant s'écarte de
    l'identité en base — la sonde est une lecture, l'écriture est l'exception."""
    try:
        if not _cfg("node_health_mxl_domain_def_enabled", 1):
            return
        now_m = time.monotonic()
        due = _mxl_domain_def_at.get(node_id)
        if due is not None and now_m < due:
            return
        _mxl_domain_def_at[node_id] = now_m + _cfg(
            "node_health_mxl_domain_def_interval_s", MXL_DOMAIN_DEF_INTERVAL_S)
        if due is None:
            return               # 1er passage : on ARME seulement (cf. _check_prep_drift).
        from . import node_driver, mtl
        from .database import db_node_mxl_domain_id
        rc, out, _err = node_driver.host_exec(node, _MXL_DOMAIN_DEF_PROBE, timeout=30)
        cur = (out or "").strip()
        if rc != 0 or not cur or cur == "sansdomaine":
            return               # injoignable, illisible, ou pas de MXL ici : rien à faire.
        # On ne CRÉE l'identité en base que si le nœud porte bien un domaine — inutile de semer
        # un UUID pour un nœud qui n'en aura jamais l'usage.
        voulu = db_node_mxl_domain_id(node_id)
        if voulu and cur == voulu:
            return
        ok, msg = mtl.ensure_mxl_domain_def(node)
        if ok:
            # Pas de log de succès ici : `ensure_mxl_domain_def` journalise elle-même son écriture,
            # pour TOUS ses appelants (cf. son commentaire). Doubler la ligne n'ajouterait rien.
            pass
        else:
            # WARNING, pas DEBUG (corrigé le 2026-08-15) : à ce stade on SAIT que le nœud porte un
            # domaine et que son fichier est absent ou faux — un échec de pose est donc un fait
            # constaté, pas du bruit de sonde. En DEBUG il était invisible en production, et la
            # seule trace d'une réparation était celle des réparations RÉUSSIES : on n'aurait vu
            # que les succès, jamais les échecs, ce qui est précisément la façon dont un défaut
            # se déguise en « ça marche » (cf. l'enquête qui a suivi la pose initiale).
            log.warning("node_health : domain_def.json NON posé sur %s : %s", name, msg)
    except Exception as e:
        log.debug("node_health domain_def MXL nœud %s: %s", node_id, e)


def _sonder_prep(node_id, name, node, contexte):
    """Sonde la prép hôte MTL d'un nœud 2110 et fait évaluer le résultat. Renvoie le verdict
    affichable, ou None si le nœud n'a pas de prép à vérifier / est injoignable.

    Extrait de `_check_prep_drift` pour être RÉUTILISABLE par la re-sonde forcée depuis l'UI
    (`forcer_prep`) : le verdict a jusqu'à 30 min, l'opérateur doit pouvoir le rafraîchir à la
    demande sans attendre le prochain tour."""
    from . import node_driver, node_recovery, mtl
    if "io2110" not in (node_driver.node_capabilities(node) or []):
        return None              # pas de 2110 sur ce nœud → aucune prép MTL à vérifier
    prep = mtl.verifier_node(node) or {}
    if prep.get("error"):
        return None              # nœud injoignable : c'est déjà signalé ailleurs, pas de doublon
    # Décision + alerte à transition déléguées à node_recovery : état PARTAGÉ avec la passe
    # post-boot, sinon un défaut signalé au boot puis réparé n'aurait jamais son message de
    # résolution (et inversement, on ré-alerterait un défaut déjà signalé au reboot).
    node_recovery.evaluer_prep(node_id, name, prep, contexte)
    return node_recovery.verdict_prep(node_id)


def _merge_prep(node_id, snap, node):
    """Publie le verdict de prép hôte MTL dans le snapshot (onglet Nœuds du Monitoring).

    Trois cas DISTINCTS, à ne surtout pas confondre :
      - nœud sans capacité `io2110` → AUCUNE clé `prep` (rien à afficher, pas un état vide) ;
      - nœud 2110 jamais sondé (le contrôle de dérive n'arme qu'au 1er passage, et le post-boot
        n'a pas encore tourné) → `{"level": "unknown", "ts": None}` = « pas encore vérifié »,
        surtout PAS « OK » ;
      - nœud sondé → le verdict complet, AVEC son horodatage de sonde (`ts`), qui n'a rien à voir
        avec la fraîcheur du reste du snapshot (5 s) : l'UI doit afficher les deux séparément."""
    try:
        from . import node_driver, node_recovery
        if "io2110" not in (node_driver.node_capabilities(node) or []):
            return
        snap["prep"] = node_recovery.verdict_prep(node_id) or {"level": "unknown", "ts": None}
    except Exception as e:
        log.debug("node_health merge prép nœud %s: %s", node_id, e)


def forcer_prep(node_id):
    """Re-sonde MAINTENANT la prép hôte d'un nœud (bouton « revérifier » du Monitoring) et
    remet à zéro la cadence de dérive. Renvoie {ok, prep} — `prep` None si le nœud n'a pas de
    prép à vérifier ou n'a pas répondu."""
    from .database import db_get_node
    node = db_get_node(node_id)
    if not node:
        return {"ok": False, "error": "nœud inconnu"}
    name = node.get("name") or node.get("host") or str(node_id)
    try:
        verdict = _sonder_prep(node_id, name, node, "revérification manuelle")
    except Exception as e:
        log.debug("node_health re-sonde prép nœud %s: %s", node_id, e)
        return {"ok": False, "error": str(e)}
    # Cadence repoussée d'un intervalle plein : on vient de sonder, inutile de recommencer tout de
    # suite. `_prep_drift_at` est aussi le drapeau d'armement — le poser ici évite qu'une re-sonde
    # manuelle sur un nœud jamais armé soit suivie d'une seconde sonde au tour suivant.
    _prep_drift_at[node_id] = time.monotonic() + _cfg("node_health_prep_drift_interval_s",
                                                      PREP_DRIFT_INTERVAL_S)
    # Le snapshot déjà en cache porte l'ANCIEN verdict : le remettre à jour tout de suite, sinon
    # l'UI rafraîchie dans la seconde réafficherait l'horodatage périmé (échec silencieux).
    if verdict is not None:
        with _lock:
            last = _last.get(str(node_id))
            if last is not None:
                last["prep"] = verdict
    return {"ok": True, "prep": verdict}


def _merge_membw(node_id, snap):
    """Pousse d'abord l'échantillon auto-reporté par l'agent (clé `membw` du /v1/health,
    agent ≥ 0.14.0) vers `membw.ingest` (qui garde référence/ratio/alertes), puis REMPLACE
    la valeur brute du snapshot par la version enrichie {gbps, baseline, ratio, level, ts}
    — même schéma pour l'UI quel que soit le chemin de mesure (agent ou repli exec)."""
    try:
        from . import membw
        # snap porte déjà name/host — les seules clés lues pour nommer l'alerte.
        membw.ingest(node_id, snap, snap.get("membw"))
        mb = membw.latest().get(node_id)
        if mb:
            snap["membw"] = mb
    except Exception:
        pass


def _merge_gpu(node_id, snap):
    """Fusionne la télémétrie GPU (cache `app/gpu.py`, échantillonné indépendamment dans
    surveillance) dans le snapshot du nœud, pour l'onglet Serveurs du Monitoring."""
    try:
        from . import gpu
        g = gpu.latest().get(node_id)
        if g:
            snap["gpu"] = g
    except Exception:
        pass


def _merge_rdma(node_id, snap):
    """Fusionne la santé RDMA (cache du service `services/rdma`, échantillonné dans surveillance)
    dans le snapshot du nœud, pour la tuile + le détail de l'onglet Serveurs du Monitoring."""
    try:
        from services import rdma
        r = rdma.stats_for_node(node_id)
        if r and r.get("devices"):
            snap["rdma"] = r
    except Exception:
        pass


def _merge_ptp(node_id, snap):
    """Écrase le `ptp` auto-reporté par l'agent par le relevé AUTORITAIRE de l'orchestrateur.
    L'agent tourne DANS le conteneur : pmc -u (socket de management) est bloqué par le mount-ns
    → il voit ptp4l « running » mais offset/grandmaster/locked restent vides (cf. ptp.refclk_for_host).
    L'orchestrateur, lui, lit pmc par SSH sur l'hôte (ptp.status) et cache ça dans cached_status(nid).
    On ne fusionne que si PTP est activé pour le nœud et qu'un relevé existe — sinon on laisse le
    champ agent (ou son absence) tel quel."""
    try:
        from . import ptp, settings as st
        # ── Ce que « PTP est activé sur ce nœud » veut dire ────────────────────────────────────
        # Le réglage `ptp_enabled` vaut False PAR DÉFAUT et n'est jamais semé à l'installation.
        # Or le déploiement PTP réel, lui, est piloté par `node_interfaces.ptp_enabled` — c'est
        # lui qui pose les unités `mxl-ptp4l-net<id>`. Les deux pouvaient donc se contredire, et
        # ça s'est vu en prod (Horace, 2026-07-28) : quatre interfaces à ptp_enabled=1, ptp4l
        # verrouillé depuis quatre jours, et le réglage de nœud jamais posé → cette ligne rendait
        # None → tout le relevé pmc AUTORITAIRE de l'orchestrateur (le seul qui cible le bon
        # domaine et le bon socket) ne tournait pas une seule fois. La page Horloges retombait
        # alors sur le bloc de l'agent, muet, et concluait « aucune source de temps ».
        # La présence de groupes PTP est un FAIT ; le réglage n'est qu'une intention. On lit les
        # deux, et un fait suffit.
        actif = bool(st.setting_for("ptp_enabled", node_id))
        if not actif:
            try:
                actif = bool(ptp.groups_from_node_interfaces(node_id))
            except Exception:
                actif = False
        s = ptp.cached_status(node_id) if actif else None
        if not s:
            # Nœud full-PF DPDK : ses ports média sont sur vfio, donc PAS de netdev noyau, donc ni
            # ptp4l ni pmc — `cached_status` reste vide À JAMAIS et le bloc PTP restait nul. Son
            # horloge existe pourtant : elle est disciplinée par le client PTP INTERNE de libmtl,
            # qui publie son offset sur le :8080 du moteur. On va le chercher là.
            # Sans ça, un nœud 2110 s'affichait sans aucune mesure de synchro — sur la page
            # Monitoring comme sur Réglages → Réseau → Horloges — alors qu'il est le mieux
            # discipliné du parc.
            eng = (_io_engine_snapshot(node_id) or {}).get("ptp") or {}
            if not eng:
                return
            base = dict(snap.get("ptp") or {})
            base.update({
                "running":   bool(eng.get("engine")),
                "locked":    bool(eng.get("locked")),
                "synced":    bool(eng.get("synced")),
                "engine_ptp": True,
                "sync_ok":   ptp.clock_ok({"engine_ptp": True, "synced": eng.get("synced"),
                                           "locked": eng.get("locked")}),
                "raw_delta_ns": eng.get("raw_delta_ns"),
                "offset_ns": eng.get("offset_ns"),
                "mpd_ns":    eng.get("path_delay_ns"),
                "gm_id":     eng.get("gm_identity") or eng.get("gm_id"),
                "source":    "moteur",
            })
            snap["ptp"] = base
            return
        base = dict(snap.get("ptp") or {})   # garde l'iface remontée par l'agent
        base.update({
            # `running` : sur un nœud full-PF DPDK il n'y a PAS de ptp4l — le client PTP est celui
            # de libmtl, dans le moteur. Se contenter de ptp4l_running affichait « PTP arrêté ».
            "running":   bool(s.get("ptp4l_running") or s.get("engine_ptp_client")),
            "locked":    bool(s.get("locked")),
            # Synchro RÉELLE au GM, et critère d'affichage/alarme (cf. ptp.clock_ok). `locked` seul
            # est le critère de l'ère AF_XDP : sur PTP moteur c'est le lock servo STRICT, qui restait
            # False tant que l'asservissement en fréquence n'était pas compilé (corrigé 2026-08-30).
            # Il s'arme désormais — mais `synced` reste le critère parce qu'il dit la DISPONIBILITÉ
            # d'une référence, quand `locked` dit la CONVERGENCE du servo (cf. ptp.clock_ok).
            "synced":    bool(s.get("synced")),
            "engine_ptp": bool(s.get("engine_ptp")),
            "sync_ok":   ptp.clock_ok(s),
            "raw_delta_ns": s.get("raw_delta_ns"),
            "error":     s.get("error"),
            "offset_ns": s.get("offset_ns"),
            "mpd_ns":    s.get("mean_path_delay_ns"),
            "gm_id":     s.get("grandmaster_id"),
            "port_state": s.get("port_state"),
            "phc2sys_state": s.get("phc2sys_state"),
            # Qualité de la RÉFÉRENCE : un verrou nanométrique sur une horloge en roue libre
            # doit se voir, sinon `locked: true` raconte que tout va bien (cf. ptp.gm_reference_saine).
            "gm_clock_class":  s.get("gm_clock_class"),
            "utc_offset_valid": s.get("utc_offset_valid"),
            "gm_saine":        s.get("gm_saine"),
            "gm_raison":       s.get("gm_raison"),
            # phc2sys discipline-t-il CLOCK_REALTIME sur ce nœud ? Consommé par clocks.py pour
            # REFUSER de proposer chrony là où un servo tient déjà l'horloge.
            "phc2sys_running": bool(s.get("phc2sys_running")),
        })
        # Interfaces porteuses de PTP (node_interfaces.ptp_enabled) → liste affichée dans le détail.
        try:
            from .database import db_get_node_interfaces
            base["interfaces"] = [{"ifname": i.get("ifname"), "role": i.get("role")}
                                  for i in (db_get_node_interfaces(node_id) or []) if i.get("ptp_enabled")]
        except Exception:
            pass
        snap["ptp"] = base
    except Exception:
        pass


# ─── Ports média en DPDK/vfio (chantier DPDK/narrow, cf. docs/chantiers/DPDK_NARROW.md) ─────
# Quand `node_interfaces.pmd == "dpdk"`, le port média est lié à vfio-pci : il DISPARAÎT de
# /sys/class/net (plus d'ethtool -S ni de compteurs kernel) → l'agent ne peut plus mesurer son
# débit. Les compteurs viennent alors du moteur 2110_io (snapshot :8080, champ
# `nic.ports[i].mtl_stats`, contrat /tmp/mtl_ports.json — cf. docs/chantiers/DPDK_NARROW.md « Contrats de la
# nuit »). Deux formes d'agent tolérées : ancien agent (< 0.15.0 : l'interface disparaît de
# net/nics) et agent ≥ 0.15.0 (entrée `{"state": "vfio"}`) — dans les deux cas l'absence du
# netdev est NORMALE, ce n'est PAS une « interface disparue ». Un nœud sans interface
# pmd=dpdk (flotte af_xdp actuelle) n'est PAS touché : aucun fetch, aucun changement.
_mtl_prev = {}          # (node_id, ifname) → {"rx","tx","pkts","ts"} (deltas débit + gel)
_vfio_frozen_cnt = {}   # (node_id, ifname) → nb d'échantillons consécutifs à compteurs figés
_vfio_alert_state = {}  # (node_id, ifname) → True si l'alerte « port vfio muet » est posée (cache RAM)
_episodes_vfio = _Episodes("node_vfio")   # le MÊME état, SURVIVANT au redémarrage (cf. episodes.py)
VFIO_FROZEN_SAMPLES = 6  # ≈ 30 s à 5 s/échantillon (setting node_health_vfio_frozen_samples)


def _io_engine_snapshot(node_id):
    """Snapshot :8080 du moteur 2110_io du nœud. None si pas de moteur / IP inconnue /
    injoignable (best-effort : le débit restera vide, pas d'alerte ici)."""
    try:
        from .database import db_get_containers
        for c in db_get_containers() or []:
            if str(c.get("node_id")) != str(node_id) or not c.get("ip"):
                continue
            try:
                dc = json.loads(c["deploy_config"]) if isinstance(c.get("deploy_config"), str) \
                     else (c.get("deploy_config") or {})
            except (ValueError, TypeError):
                continue
            if not isinstance(dc, dict) or dc.get("type") != "2110_io":
                continue
            import requests
            r = requests.get(f"http://{c['ip']}:8080", timeout=2)
            if r.status_code == 200:
                return r.json()
            return None
    except Exception as e:
        log.debug("node_health moteur 2110 nœud %s: %s", node_id, e)
    return None


def _merge_dpdk_net(node_id, snap):
    """Débits par NIC des ports média en `pmd=dpdk` (vfio) : sourcés depuis
    `nic.ports[i].mtl_stats` du moteur (deltas rx_bytes/tx_bytes) et injectés dans
    `snap["net"]` au schéma agent ({rx_bps, tx_bps, speed_mbps} + pmd/source) — l'onglet
    Serveurs du Monitoring les affiche sans changement. Pas d'alerte « interface disparue » :
    l'absence du netdev est le fonctionnement nominal d'un port vfio. En revanche, alerte
    `warning` À TRANSITION (pattern _check_disk_alert) « port vfio muet » si les compteurs
    mtl_stats sont figés sur N échantillons consécutifs alors que des sessions MTL sont
    actives sur le port ; `info` au retour du trafic."""
    try:
        from .database import db_get_node_interfaces
        dpdk_ifaces = [i for i in (db_get_node_interfaces(node_id) or [])
                       if (i.get("pmd") or "").strip().lower() == "dpdk"]
    except Exception as e:
        log.debug("node_health node_interfaces nœud %s: %s", node_id, e)
        return
    if not dpdk_ifaces:
        return                                   # nœud 100 % af_xdp/kernel → strictement inchangé
    eng = _io_engine_snapshot(node_id) or {}
    ports = (eng.get("nic") or {}).get("ports") or []
    now = time.time()
    name = snap.get("name") or snap.get("host") or f"nœud {node_id}"
    net = snap.setdefault("net", {})
    frozen_n = max(2, int(_cfg("node_health_vfio_frozen_samples", VFIO_FROZEN_SAMPLES)))
    interval = _cfg("node_health_interval_s", SAMPLE_INTERVAL_S)
    for itf in dpdk_ifaces:
        ifname = itf.get("ifname")
        if not ifname:
            continue
        # Le moteur peut désigner le port par son ifname (héritage af_xdp) ou par son BDF PCI
        # (port vfio) → on matche sur les deux (colonne `pci` de node_interfaces).
        idents = {ifname, (itf.get("pci") or "").strip()} - {""}
        port = next((p for p in ports if isinstance(p, dict)
                     and (p.get("iface") in idents
                          or (p.get("mtl_stats") or {}).get("port") in idents)), None)
        stats = (port or {}).get("mtl_stats")
        # L'entrée réseau existe TOUJOURS pour un port dpdk (l'agent, lui, ne la voit plus) :
        # l'UI continue d'afficher l'interface au lieu de la faire disparaître.
        entry = dict(net.get(ifname) or {})
        entry.setdefault("rx_bps", None)
        entry.setdefault("tx_bps", None)
        entry["pmd"] = "dpdk"
        entry["source"] = "mtl"                  # provenance des compteurs (≠ agent)
        cap = (port or {}).get("port_capacity_gbps")
        if not entry.get("speed_mbps") and cap:
            entry["speed_mbps"] = int(float(cap) * 1000)
        key = (str(node_id), ifname)
        if isinstance(stats, dict):
            rx = int(stats.get("rx_bytes") or 0)
            tx = int(stats.get("tx_bytes") or 0)
            pkts = (int(stats.get("rx_packets") or 0), int(stats.get("tx_packets") or 0))
            prev = _mtl_prev.get(key)
            if prev and now - prev["ts"] > 0 and rx >= prev["rx"] and tx >= prev["tx"]:
                # compteurs cumulés : delta négatif = moteur redémarré → on saute un cycle
                dt = now - prev["ts"]
                entry["rx_bps"] = round((rx - prev["rx"]) * 8 / dt)
                entry["tx_bps"] = round((tx - prev["tx"]) * 8 / dt)
            _mtl_prev[key] = {"rx": rx, "tx": tx, "pkts": pkts, "ts": now}
            # « Port vfio muet » : paquets figés alors que le moteur a des sessions actives sur
            # le port (`nic.ports[i].active` = sessions live). Sans session, un port silencieux
            # est normal (rien d'abonné) → compteur remis à zéro.
            sessions = (port or {}).get("active")
            try:
                sessions = int(sessions) if sessions is not None else 0
            except (TypeError, ValueError):
                sessions = 0
            if sessions > 0 and prev is not None and pkts == prev.get("pkts"):
                _vfio_frozen_cnt[key] = _vfio_frozen_cnt.get(key, 0) + 1
            else:
                _vfio_frozen_cnt[key] = 0
            muet = _vfio_frozen_cnt.get(key, 0) >= frozen_n
            entry["frozen"] = muet
            prev_alert = _vfio_alert_state.get(key)
            if prev_alert is None and key not in _vfio_alert_state:
                prev_alert = bool(_episodes_vfio.get(key))   # reprise après (re)démarrage
                _vfio_alert_state[key] = prev_alert
            if muet and not prev_alert:
                db_add_alert(
                    "alert.net.port_muet", "warning", node_id=node_id, kind="net",
                    params={"ifname": ifname, "n": name, "s": int(frozen_n * interval),
                            "sessions": sessions})
                _vfio_alert_state[key] = True
                _episodes_vfio.poser(key, True)
            elif prev_alert and not muet:
                db_add_alert("alert.net.port_retabli", "info", node_id=node_id, kind="net",
                             params={"ifname": ifname, "n": name})
                _vfio_alert_state[key] = False
                _episodes_vfio.retirer(key)
        else:
            # Pas de mtl_stats (moteur arrêté, ancien controller.py, port inconnu) : on n'invente
            # ni débit ni alerte gel — l'entrée reste affichée avec des débits inconnus.
            _vfio_frozen_cnt[key] = 0
        net[ifname] = entry


def _sample_one(node):
    """Échantillonne UN nœud (exécuté EN PARALLÈLE par sample_all). Best-effort, ne lève jamais.
    Isolé pour qu'un nœud injoignable (timeout ~8 s) ne retarde pas l'échantillonnage des autres."""
    nid = node.get("id")
    host = node.get("host")
    if nid is None or not host:
        return
    try:
        if node.get("agent_url"):
            from . import node_driver
            # Auto-réparation : nœud enrôlé mais register raté (capacités vides) → ré-enregistrer.
            if node_driver.ensure_registered(node):
                from .database import db_get_node
                node = db_get_node(nid) or node
            snap = node_driver.health(node)        # /v1/health (riche)
        else:
            snap = _ssh_snapshot(host)             # legacy ssh (réduit)
        if not snap:
            with _lock:
                last = _last.get(str(nid))
                if last:
                    last["ok"] = False              # marque stale sans écraser les données
            return
        snap.setdefault("ts", time.time())
        snap["ok"] = True
        snap["name"] = node.get("name") or host
        snap["host"] = host
        snap["capabilities"] = node.get("capabilities")
        # Heartbeat nœud : persiste agent_version/last_seen/status dans la table `nodes` (self-heal du
        # « ? » de version dans l'UI). refresh() n'était câblé nulle part → agent_version n'était posé
        # qu'au 1er enroll (perdu si timing/maj agent). Nœuds à agent uniquement.
        try:
            av = snap.get("agent_version")
            if av and av != node.get("agent_version"):
                from .database import db_update_node
                from datetime import datetime as _dt
                db_update_node(nid, agent_version=av,
                               last_seen=_dt.now().isoformat(timespec="seconds"), status="up")
        except Exception:
            pass
        # Dérive d'images : ce nœud a-t-il vraiment ce que la base lui prête ? Contrôle throttlé
        # (15 min) et NON bloquant — il ne rapatrie rien, il rend l'écart visible. Cf.
        # `verifier_derive_images` pour la raison de ne pas rattraper automatiquement.
        try:
            from .routes.images import verifier_derive_images
            verifier_derive_images(node)
        except Exception as e:
            log.debug("node_health dérive images nœud %s: %s", nid, e)
        _merge_membw(nid, snap)
        _merge_gpu(nid, snap)
        _merge_rdma(nid, snap)
        _merge_ptp(nid, snap)
        try:
            _merge_dpdk_net(nid, snap)           # ports média vfio/DPDK (best-effort)
        except Exception as e:
            log.debug("node_health dpdk net nœud %s: %s", nid, e)
        try:
            _merge_cpu_partage(nid, snap)        # isolés vs ordonnançables (best-effort)
        except Exception as e:
            log.debug("node_health partage CPU nœud %s: %s", nid, e)
        # Prép hôte AVANT `_record` : la dérive (sonde lente, ≤1×/30 min) peut rafraîchir le
        # verdict que `_merge_prep` publie dans CE snapshot — sinon le verdict tout juste calculé
        # n'apparaîtrait qu'au tour suivant.
        _check_prep_drift(nid, snap["name"], node)
        # Fréquence : contrôle SÉPARÉ de la prép MTL, parce qu'il vaut pour TOUS les nœuds et pas
        # seulement ceux qui portent du 2110 (cf. le commentaire de _check_freq_drift).
        _check_freq_drift(nid, snap["name"], node)
        # Profondeur MXL : même raisonnement que la fréquence — tous les nœuds qui portent le
        # domaine MXL (donc tous les nœuds Docker, pas seulement l'io2110), cf. _check_mxl_history_drift.
        _check_mxl_history_drift(nid, snap["name"], node)
        # Identité du domaine MXL : même cadence, mais RÉPARE au lieu d'alerter (cf.
        # _check_mxl_domain_def) — le tmpfs perd le fichier à chaque reboot du nœud.
        _check_mxl_domain_def(nid, snap["name"], node)
        _merge_prep(nid, snap, node)
        _record(nid, snap)
        _check_disk_alert(nid, snap["name"], snap.get("disks"))
        # Détection de reboot du nœud (+ auto-recovery si activé) — best-effort, ne lève jamais.
        try:
            from . import node_recovery
            node_recovery.on_health_snapshot(node, snap)
        except Exception as e:
            log.debug("node_recovery hook nœud %s: %s", nid, e)
    except Exception as e:
        log.debug("node_health nœud %s: %s", host, e)


def sample_all(force=False):
    """Échantillonne le contrôleur + tous les nœuds (throttlé `node_health_interval_s`, défaut 5 s).
    Appelée depuis `surveillance` à chaque tick → no-op tant que l'intervalle n'est pas écoulé."""
    global _last_sample_m, _last_flush
    if not _cfg("node_health_enabled", 1):
        return
    interval = _cfg("node_health_interval_s", SAMPLE_INTERVAL_S)
    now_m = time.monotonic()
    if not force and (now_m - _last_sample_m) < interval:
        return
    _last_sample_m = now_m

    # 1) Contrôleur (toujours, lecture locale).
    try:
        _record(CONTROLLER_KEY, _controller_snapshot())
    except Exception as e:
        log.debug("node_health contrôleur: %s", e)

    # 2) Nœuds enrôlés — EN PARALLÈLE (un thread par nœud, borné). Sinon la boucle série calait sur un
    # nœud injoignable (timeout ~8 s/health, 20 s/ssh) → le snapshot des nœuds VIVANTS vieillissait
    # au-delà du seuil de fraîcheur et ils passaient « hors-ligne » à tort. Le cycle est désormais
    # borné par max(par-nœud), plus par la somme.
    nodes = [n for n in (db_get_nodes() or []) if n.get("id") is not None and n.get("host")]
    if nodes:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(nodes), 16),
                                thread_name_prefix="node-health") as ex:
            list(ex.map(_sample_one, nodes))

    # 3) Flush périodique du ring 24 h.
    if time.time() - _last_flush >= STATS_FLUSH_S:
        _flush_stats()
        _last_flush = time.time()


# ─── Persistance 24 h (modèle ptp._flush_stats / _load_stats) ─────────────────
def _flush_stats():
    with _lock:
        snapshot = {k: list(dq) for k, dq in _stats.items()}
    tmp = STATS_PERSIST_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, STATS_PERSIST_PATH)
    except OSError as e:
        log.debug("node_health flush stats: %s", e)


def _load_stats():
    try:
        with open(STATS_PERSIST_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return
    if not isinstance(data, dict):
        return
    cutoff = time.time() - STATS_SECONDS
    with _lock:
        _stats.clear()
        for key, rows in data.items():
            dq = _stats_dq(key)
            for r in rows or []:
                if isinstance(r, dict) and r.get("t", 0) >= cutoff:
                    dq.append(r)
    log.info("node_health : agrégats 24 h rechargés (%d entrées) depuis %s",
             len(_stats), STATS_PERSIST_PATH)


def load_persisted():
    """Recharge les agrégats 24 h au boot (appelé depuis main.py avant le 1er sample)."""
    global _last_flush
    _load_stats()
    _last_flush = time.time()


# ─── Accès API ────────────────────────────────────────────────────────────────
def latest():
    """{nodes:{node_id→snapshot}, controller:snapshot}. node_id en str pour le JSON."""
    with _lock:
        nodes = {k: v for k, v in _last.items() if k != CONTROLLER_KEY}
        controller = _last.get(CONTROLLER_KEY)
    return {"nodes": nodes, "controller": controller}


def history(node_key):
    """Ring 10 min (sparklines) pour un nœud (ou 'controller')."""
    with _lock:
        return list(_hist.get(str(node_key), ()))


def stats_24h(node_key):
    """Agrégats min/moy/max sur 24 h pour cpu/mem/shm/membw d'un nœud."""
    cutoff = time.time() - STATS_SECONDS
    with _lock:
        rows = [r for r in _stats.get(str(node_key), ()) if r.get("t", 0) >= cutoff]

    def _agg(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if not vals:
            return {"mean": None, "max": None, "min": None}
        return {"mean": round(sum(vals) / len(vals), 1), "max": max(vals), "min": min(vals)}

    span = (rows[-1]["t"] - rows[0]["t"]) if rows else 0
    return {"count": len(rows), "span_s": span,
            "cpu": _agg("cpu"), "mem": _agg("mem"),
            "shm": _agg("shm"), "membw": _agg("membw")}
