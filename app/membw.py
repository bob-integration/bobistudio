# SPDX-License-Identifier: GPL-3.0-or-later
"""Mesure + alerte de la BANDE PASSANTE MÉMOIRE par nœud (canary memcpy).

Le compositing multiview est *memory-bandwidth bound* (cf. test de charge 2026-06 : à
saturation les cœurs restent ~80% idle, c'est le bus RAM qui plafonne). Ni /proc ni les
compteurs CPU ne donnent ce signal ; les compteurs uncore (Intel PCM / perf) exigent
MSR + install spécifiques au CPU. On utilise donc un **canary portable** : un memcpy
mono-thread d'un gros buffer, dont le débit atteint CHUTE quand le bus est saturé par la
flotte. C'est un indicateur de *headroom* (combien de bande passante reste), pas la bande
passante totale du bus — exactement ce qu'il faut pour alerter « la RAM sature → les
multiviews vont décrocher ».

Architecture : depuis l'agent-nœud 0.14.0, le canary tourne DANS l'agent (thread de fond,
`_membw_loop`) qui publie le débit brut dans `/v1/health` → `node_health` le pousse ici via
`ingest()` à chaque snapshot (aucun exec par mesure). Ce module reste le cerveau : référence
par nœud = meilleur débit observé (apprise au repos), alerte par TRANSITION (warning/error)
quand le débit tombe sous un ratio de la référence (réglages `membw_*`). REPLI pour les nœuds
qui n'auto-reportent pas (agent < 0.14.0, nœud legacy SSH) : `sample_all` lance encore le
canary one-shot via `host_ops.ssh_run` (agent /v1/host/exec ou SSH), throttlé défaut 60 s.
"""
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from .host_ops import ssh_run
from .database import db_get_nodes, db_add_alert
from . import settings as S

log = logging.getLogger("membw")

# Canary mono-ligne (python3 -c) : memcpy de N octets r fois → débit copie en Go/s.
# Aucune double-quote à l'intérieur (on enrobe la commande en double-quotes côté shell).
_CANARY_TMPL = (
    "python3 -c \""
    "import ctypes,time;"
    "N={mb}*1024*1024;"
    "a=ctypes.create_string_buffer(N);b=ctypes.create_string_buffer(N);"
    "mm=ctypes.memmove;mm(b,a,N);"
    "r=8;t=time.perf_counter();"
    "[mm(b,a,N) for _ in range(r)];"
    "print(round(r*N/(time.perf_counter()-t)/1e9,3))\""
)

# État en mémoire (process orchestrateur).
_baseline = {}     # node_id -> meilleur Go/s observé (référence « au repos »)
_last = {}         # node_id -> {gbps, ratio, ts}
_alert_state = {}  # node_id -> None|"warning"|"error" (cache RAM du chemin chaud)
# Le MÊME état, SURVIVANT au redémarrage de l'orchestrateur (cf. app/episodes.py) : une contention
# mémoire ne cesse pas parce que le service redémarre, et la ré-annoncer à chaque boot est la
# moitié du spam constaté le 2026-07-26.
from .episodes import EtatEpisodes as _Episodes
_episodes = _Episodes("membw")
_last_sample_m = 0.0  # monotone du dernier passage (throttle global)
_sampling = threading.Lock()  # garde anti-chevauchement du cycle de fond
_ingest_ts = {}    # node_id -> ts agent du dernier échantillon ingéré (dédoublonnage)
_ingest_m = {}     # node_id -> monotone de la dernière ingestion (repli exec si trop vieux)


def _cfg(key, default):
    try:
        v = S.get(key)
        return type(default)(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def measure_host(host):
    """Lance le canary sur l'hôte `host`, renvoie le débit copie en Go/s (float) ou None."""
    mb = max(16, _cfg("membw_sample_mb", 128))
    try:
        rc, out, err = ssh_run(host, _CANARY_TMPL.format(mb=mb), timeout=30)
    except Exception as e:
        log.warning("canary membw %s : %s", host, e)
        return None
    if rc != 0:
        log.warning("canary membw %s : rc=%s %r", host, rc, (err or out))
        return None
    try:
        return float((out or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        log.warning("canary membw %s : sortie inattendue %r", host, out)
        return None


def _check_alert(node_id, node, gbps, base):
    """Alerte par transition selon le ratio débit/référence (réglages membw_warn/err_ratio).

    Deux garde-fous, ajoutés au balayage des alarmes du 2026-07-27 :
      - ÉPISODE PERSISTÉ : l'état vivait en RAM, donc une contention EN COURS était ré-annoncée à
        chaque redémarrage du service (mode d'échec des 26 doublons du 26/07).
      - HYSTÉRÉSIS à la clôture : on armait sous `warn` et on clôturait dès qu'on repassait
        au-dessus de `warn` — un débit qui oscille autour du seuil (le régime NORMAL d'une
        contention) notifiait chaque franchissement. Le retour à la normale exige désormais une
        marge (`membw_clear_margin`, +15 % par défaut), comme `cpu_pressure` le fait déjà."""
    warn = _cfg("membw_warn_ratio", 0.5)
    err = _cfg("membw_err_ratio", 0.3)
    marge = _cfg("membw_clear_margin", 1.15)
    ratio = gbps / base if base else 1.0
    level = "error" if ratio < err else ("warning" if ratio < warn else None)
    prev = _alert_state.get(node_id)
    if prev is None and node_id not in _alert_state:
        prev = _episodes.get(node_id)             # reprise après (re)démarrage
        _alert_state[node_id] = prev
    if level and level != prev:
        name = node.get("name") or node.get("host") or f"nœud {node_id}"
        db_add_alert(
            "alert.resource.membw_contention",
            level, node_id=node_id, kind="resource",
            params={"name": name, "gbps": gbps, "ratio": ratio * 100, "base": base})
        _alert_state[node_id] = level
        _episodes.poser(node_id, level)
    elif not level and prev and ratio >= warn * marge:
        name = node.get("name") or node.get("host") or f"nœud {node_id}"
        db_add_alert("alert.resource.membw_normale", "info", node_id=node_id, kind="resource",
                     params={"name": name, "ratio": ratio * 100})
        _alert_state[node_id] = None
        _episodes.retirer(node_id)
    # Zone grise (entre `warn` et `warn × marge`) : ni alerte, ni clôture — l'épisode court encore.
    return ratio, level


def _apply_sample(nid, node, gbps):
    """Tronc commun ingest/exec : apprentissage de la référence + alerte + cache pour l'UI."""
    base = _baseline.get(nid, 0.0)
    if gbps > base:
        base = gbps
        _baseline[nid] = base   # apprentissage de la référence (pic observé = repos)
    ratio, level = _check_alert(nid, node, gbps, base)
    _last[nid] = {"gbps": round(gbps, 1), "baseline": round(base, 1),
                  "ratio": round(ratio, 2), "level": level, "ts": time.time()}


def ingest(node_id, node, mb):
    """Échantillon auto-reporté par l'agent-nœud (`/v1/health` → clé `membw`, agent ≥ 0.14.0),
    poussé par `node_health` à chaque snapshot. L'agent ne fournit que le débit brut ; référence,
    ratio et alertes restent ici. Dédoublonné sur le ts agent (le poll health à 5 s revoit ~12×
    le même échantillon du canary à 60 s). Un nœud qui auto-reporte n'est plus canary-é par exec
    (cf. sample_all)."""
    if not isinstance(mb, dict) or mb.get("gbps") is None:
        return
    _ingest_m[node_id] = time.monotonic()
    ts = mb.get("ts")
    if ts is not None and _ingest_ts.get(node_id) == ts:
        return
    _ingest_ts[node_id] = ts
    if not _cfg("membw_enabled", 1):
        return
    try:
        _apply_sample(node_id, node, float(mb["gbps"]))
    except (TypeError, ValueError):
        pass


def _sample_one(node):
    nid = node.get("id")
    gbps = measure_host(node.get("host"))
    if gbps is None:
        return
    _apply_sample(nid, node, gbps)


def _sample_cycle(nodes):
    """Cycle complet, exécuté HORS de la boucle surveillance (thread de fond) : les canaries
    tournent en parallèle (borné par max(par-nœud), pas la somme — un nœud injoignable
    coûtait jusqu'à 30 s × N en série et calait la surveillance)."""
    if not _sampling.acquire(blocking=False):
        return                      # cycle précédent encore en cours
    try:
        with ThreadPoolExecutor(max_workers=min(len(nodes), 16),
                                thread_name_prefix="membw") as ex:
            list(ex.map(_sample_one, nodes))
    except Exception as e:
        log.warning("cycle membw : %s", e)
    finally:
        _sampling.release()


def sample_all(force=False):
    """REPLI exec : canary one-shot via ssh_run pour les nœuds qui n'auto-reportent PAS
    (agent < 0.14.0, nœud legacy SSH) — les nœuds vus par `ingest()` dans les 3 derniers
    intervalles sont sautés. Throttlé à `membw_interval_s` (défaut 60 s), appelée depuis
    `surveillance` à chaque tick → no-op tant que l'intervalle n'est pas écoulé.
    NON-BLOQUANT : la mesure part dans un thread de fond (la surveillance ne cale jamais)."""
    global _last_sample_m
    if not _cfg("membw_enabled", 1):
        return
    interval = _cfg("membw_interval_s", 60)
    now_m = time.monotonic()
    if not force and (now_m - _last_sample_m) < interval:
        return
    _last_sample_m = now_m
    fresh_s = interval * 3
    nodes = [n for n in (db_get_nodes() or [])
             if n.get("host")
             and (force                                   # mesure forcée (API) = tout le monde
                  or _ingest_m.get(n.get("id")) is None
                  or now_m - _ingest_m[n.get("id")] > fresh_s)]
    if not nodes:
        return
    threading.Thread(target=_sample_cycle, args=(nodes,),
                     daemon=True, name="membw-sampler").start()


def latest():
    """Dernières lectures par node_id (pour l'API / l'UI)."""
    return dict(_last)


def reset_baseline(node_id=None):
    """Réinitialise la référence (ex. après ajout de RAM / changement matériel)."""
    if node_id is None:
        _baseline.clear()
    else:
        _baseline.pop(node_id, None)
