# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Pression CPU (PSI) par nœud ET par conteneur — le détecteur de FAMINE.

**Pourquoi PSI et pas un pourcentage d'occupation.** Le tableau de bord affichait « au vert » un
nœud où un mur multiview mettait 1,2 à 2,2 s par trame pour un budget de 20 ms (dl360-1, 2026-07-14).
Deux raisons, toutes deux structurelles :

1. `node_health.cpu_pct` = loadavg / nb de cœurs, agrégé sur les **48** CPU du nœud. La famine était
   confinée aux **5 cœurs physiques** du pool de calcul (les 19 autres étant dédiés au moteur 2110,
   busy-poll à 100 % — donc « occupés » et parfaitement sains). Elle était **noyée dans la moyenne**.
2. Un taux d'occupation ne distingue PAS « chargé et sain » de « en train d'étouffer ». Un cœur à
   100 % qui exécute UNE tâche est nominal ; le même cœur à 100 % avec 4 tâches qui se battent
   dessus est une panne. Le taux est identique.

PSI (`/proc/pressure/cpu`, cgroup v2 `cpu.pressure`) mesure exactement ce qui manque : le temps
pendant lequel des tâches sont **bloquées à attendre le CPU**.
  - `some` = au moins une tâche du groupe est en attente ;
  - `full` = **TOUTES** les tâches du groupe sont à l'arrêt — le conteneur ne progresse plus du tout
    pendant ce temps-là. Sur un conteneur temps réel, `full` EST la famine, en clair.

Le `throttling` CFS (`cpu.stat: nr_throttled`) ne dirait RIEN ici : les conteneurs compute n'ont pas
de quota, ils se battent pour un **cpuset partagé**. Le compteur est à 0 pendant que le mur rate 98 %
de ses trames. C'est bien PSI qu'il faut.

Modèle : sampler throttlé appelé depuis `surveillance` (comme `membw`/`node_health`), lecture par
`node_driver.host_exec` (agent, read-only), rings 10 min / 24 h persistés, alertes **par transition**
sur pression **soutenue** (N échantillons consécutifs), nommant le nœud et les conteneurs.
"""
import json
import logging
import os
import threading
import time
from collections import deque

from .config import DB_PATH
from .database import db_get_nodes, db_add_alert
from .episodes import EtatEpisodes as _Episodes
from . import settings as S

log = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 10          # réglage `cpu_psi_interval_s` (le PSI bouge lentement : avg10/60/300)
HISTORY_MAX = 120               # ~20 min de sparkline
STATS_MAX = 8640                # 24 h à 10 s
STATS_PERSIST_PATH = os.path.join(os.path.dirname(DB_PATH) or ".", "cpu_pressure_stats.json")
STATS_FLUSH_S = 300

# ─── Seuils (mesurés, pas au jugé — relevé dl360-1 du 2026-07-14) ────────────
# Relevé : hôte `some avg300` = 45,1 ; mur 145, qui tient 49,7 fps « à la corde », `full avg300`
# = 16,2 ; mur 570, 0,5 fps, `full` > 60. Autres nœuds sains de la flotte : `full` ≈ 0.
#
# `full` d'un CONTENEUR = fraction du temps mural pendant laquelle il ne progresse PAS.
#   - 10 % : à 50 fps (budget 20 ms), c'est 2 ms par trame perdues à attendre un CPU. Un conteneur
#     temps réel qui perd 10 % de son budget avant même de calculer est en danger — et c'est déjà
#     au-dessus de tout ce qu'on mesure sur un nœud sain (≈ 0). → warning.
#   - 25 % : un quart du budget est mangé par l'attente : la cadence n'est plus tenable. → error.
# `some` de l'HÔTE = au moins une tâche attend. Sur un nœud correctement dimensionné il reste bas ;
#   30 % = le nœud sur-souscrit ses cœurs, 60 % = il étouffe. Sert de tuile « santé du nœud ».
SEUIL_CONT_WARN = 10.0
SEUIL_CONT_ERR  = 25.0
SEUIL_HOTE_WARN = 30.0
SEUIL_HOTE_ERR  = 60.0
# « Soutenu » : N échantillons consécutifs au-dessus du seuil avant d'alerter (une rafale de 10 s au
# démarrage d'un conteneur n'est pas une famine). N=6 à 10 s ⇒ ~1 min de pression continue.
ECHANTILLONS_SOUTENUS = 6
# Hystérésis de retour à la normale : il faut redescendre sous 60 % du seuil, N fois, pour clore.
HYSTERESIS = 0.6

_lock = threading.Lock()
_last = {}        # node_id(str) → {"ts", "host": {...}, "containers": {vmid: {...}}}
_hist = {}        # node_id(str) → deque (sparkline : some/full hôte)
_stats = {}       # node_id(str) → deque 24 h
_cnt = {}         # (clé) → nb d'échantillons consécutifs au-dessus du seuil
_etat = {}        # (clé) → None|"warning"|"error" (alerte par transition ; cache RAM du chemin chaud)
_episodes = _Episodes("cpu_pressure")   # le MÊME état, SURVIVANT au redémarrage (cf. app/episodes.py)
_last_sample_m = 0.0
_last_flush = 0.0


def _cfg(key, default):
    try:
        v = S.get(key)
        return type(default)(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ─── Collecte (agent-nœud, lecture seule) ────────────────────────────────────
# Un seul host_exec par nœud : PSI de l'hôte + PSI du cgroup de CHAQUE conteneur docker. Le chemin
# cgroup v2 dépend du driver (systemd → system.slice/docker-<id>.scope ; cgroupfs → docker/<id>) :
# on essaie les deux. Sortie ligne à ligne, parsée ci-dessous (pas de dépendance sur le nœud).
_COLLECTEUR = r"""
echo "H $(cat /proc/pressure/cpu 2>/dev/null | tr '\n' ';')"
docker ps --no-trunc --format '{{.ID}} {{.Names}}' 2>/dev/null | while read -r id name; do
  for f in /sys/fs/cgroup/system.slice/docker-$id.scope/cpu.pressure \
           /sys/fs/cgroup/docker/$id/cpu.pressure; do
    if [ -r "$f" ]; then
      echo "C $name $(tr '\n' ';' < "$f")"
      break
    fi
  done
done
"""


def _parse_psi(blob):
    """« some avg10=1.00 avg60=2.00 avg300=3.00 total=…;full avg10=… » → {some:{avg10,…}, full:{…}}."""
    out = {}
    for part in (blob or "").split(";"):
        toks = part.split()
        if len(toks) < 2 or toks[0] not in ("some", "full"):
            continue
        vals = {}
        for t in toks[1:]:
            k, _, v = t.partition("=")
            try:
                vals[k] = float(v)
            except ValueError:
                continue
        out[toks[0]] = vals
    return out


def _vmid_de(nom):
    """« bobi-cmp-570 » / « bobi-mtl-140 » → 570 / 140. None pour un conteneur hors flotte."""
    for pref in ("bobi-cmp-", "bobi-mtl-", "bobi-fab-"):
        if nom.startswith(pref):
            reste = nom[len(pref):]
            if reste.isdigit():
                return int(reste)
    return None


def _collecter(node):
    from . import node_driver
    rc, out, _ = node_driver.host_exec(node, _COLLECTEUR, timeout=20)
    if rc != 0:
        return None
    hote, conts = {}, {}
    for ln in (out or "").splitlines():
        ln = ln.strip()
        if ln.startswith("H "):
            hote = _parse_psi(ln[2:])
        elif ln.startswith("C "):
            reste = ln[2:]
            nom, _, blob = reste.partition(" ")
            v = _vmid_de(nom)
            if v is not None:
                conts[v] = {"name": nom, "psi": _parse_psi(blob)}
    if not hote:
        return None
    return {"host": hote, "containers": conts}


# ─── Alertes par transition ──────────────────────────────────────────────────
def _niveau(val, warn, err):
    if val is None:
        return None
    return "error" if val >= err else ("warning" if val >= warn else None)


_RANG = {None: 0, "warning": 1, "error": 2}


def _ctx(cle):
    """Contexte STRUCTURÉ de l'alerte (colonnes `vmid`/`node_id`/`kind`) déduit de la clé d'épisode.
    Le producteur SAIT de quoi il parle — il ne laisse pas le consommateur le redeviner depuis le
    texte du message (cf. `database.ALERT_KINDS`)."""
    ctx = {"kind": "resource"}
    if isinstance(cle, tuple) and len(cle) == 2:
        quoi, ident = cle
        if quoi == "host":
            ctx["node_id"] = ident
        elif quoi == "cont":
            ctx["vmid"] = ident
            try:
                from .database import db_get_container
                ctx["node_id"] = (db_get_container(ident) or {}).get("node_id")
            except Exception:
                pass
    return ctx


def _transition(cle, val, warn, err, msg_actif, msg_retour):
    """Alerte quand la pression est SOUTENUE (N échantillons consécutifs), retour à la normale avec
    hystérésis. ⚠ N'alerte QU'À L'ESCALADE (rien → warning → error) : une valeur qui oscille autour
    d'un seuil (24 % ↔ 26 % : c'est le régime NORMAL d'une famine) re-déclencherait sinon une alerte
    à chaque franchissement — spam observé au 1er essai. Une accalmie partielle ne « résout » rien :
    seul un retour franc sous 60 % du seuil (hystérésis), N fois, clôt l'épisode.

    `msg_actif`/`msg_retour` retournent (clé i18n, params) — jamais une phrase déjà rédigée :
    le rendu est différé à la lecture, dans la langue du lecteur (cf. `database.db_add_alert`)."""
    n = max(2, int(_cfg("cpu_psi_samples", ECHANTILLONS_SOUTENUS)))
    niv = _niveau(val, warn, err)
    if niv:
        _cnt[cle] = _cnt.get(cle, 0) + 1
    elif val is not None and val < warn * HYSTERESIS:
        _cnt[cle] = 0
    prev = _etat.get(cle)
    if prev is None and cle not in _etat:
        # Reprise après (re)démarrage : `_etat` vit en RAM, or une famine CPU ne cesse pas parce que
        # l'orchestrateur redémarre. Sans cette relecture, chaque redémarrage ré-annonçait la MÊME
        # famine ~70 s plus tard (17 fois le 2026-07-26 pour un seul conteneur). Cf. app/episodes.py.
        prev = _episodes.get(cle)
        _etat[cle] = prev
    if niv and _cnt.get(cle, 0) >= n and _RANG[niv] > _RANG.get(prev, 0):
        clef, params = msg_actif(niv, val)
        db_add_alert(clef, niv, params=params, **_ctx(cle))
        _etat[cle] = niv
        _episodes.poser(cle, niv)
    elif prev and _cnt.get(cle, 0) == 0:
        clef, params = msg_retour(val)
        db_add_alert(clef, "info", params=params, **_ctx(cle))
        _etat[cle] = None
        _episodes.retirer(cle)


def _nom_conteneur(vmid, defaut):
    try:
        from .database import db_get_container
        c = db_get_container(vmid) or {}
        return c.get("hostname") or defaut
    except Exception:
        return defaut


def _verifier(node, data):
    nid = node.get("id")
    nom = node.get("name") or node.get("host")
    warn_h = _cfg("cpu_psi_host_warn", SEUIL_HOTE_WARN)
    err_h = _cfg("cpu_psi_host_err", SEUIL_HOTE_ERR)
    warn_c = _cfg("cpu_psi_cont_warn", SEUIL_CONT_WARN)
    err_c = _cfg("cpu_psi_cont_err", SEUIL_CONT_ERR)

    some = ((data["host"].get("some") or {}).get("avg60"))
    _transition(
        ("host", nid), some, warn_h, err_h,
        lambda niv, v: ("alert.resource.cpu_pression_hote", {"n": nom, "v": (v or 0)}),
        lambda v: ("alert.resource.cpu_pression_hote_retour", {"n": nom, "v": (v or 0)}))

    for vmid, ent in (data.get("containers") or {}).items():
        full = ((ent.get("psi", {}).get("full") or {}).get("avg60"))
        hn = _nom_conteneur(vmid, ent.get("name"))
        _transition(
            ("cont", vmid), full, warn_c, err_c,
            lambda niv, v, hn=hn, vmid=vmid: (
                "alert.resource.cpu_famine_conteneur",
                {"h": hn, "vmid": vmid, "n": nom, "v": (v or 0)}),
            lambda v, hn=hn, vmid=vmid: (
                "alert.resource.cpu_famine_conteneur_retour",
                {"h": hn, "vmid": vmid, "n": nom, "v": (v or 0)}))


# ─── Sampler ─────────────────────────────────────────────────────────────────
def _sample_one(node):
    try:
        data = _collecter(node)
        if not data:
            return
        nid = str(node.get("id"))
        data["ts"] = time.time()
        point = {
            "t": data["ts"],
            "some": (data["host"].get("some") or {}).get("avg60"),
            "full": (data["host"].get("full") or {}).get("avg60"),
        }
        with _lock:
            _last[nid] = data
            _hist.setdefault(nid, deque(maxlen=HISTORY_MAX)).append(point)
            _stats.setdefault(nid, deque(maxlen=STATS_MAX)).append(point)
        _verifier(node, data)
    except Exception as e:
        log.debug("cpu_pressure nœud %s: %s", node.get("host"), e)


def sample_all(force=False):
    """Échantillonne la pression CPU de tous les nœuds enrôlés (throttlé `cpu_psi_interval_s`).
    Appelé depuis `surveillance`. No-op si `cpu_psi_enabled` = 0."""
    global _last_sample_m, _last_flush
    if not _cfg("cpu_psi_enabled", 1):
        return
    interval = _cfg("cpu_psi_interval_s", SAMPLE_INTERVAL_S)
    now_m = time.monotonic()
    if not force and (now_m - _last_sample_m) < interval:
        return
    _last_sample_m = now_m
    nodes = [n for n in (db_get_nodes() or [])
             if n.get("id") is not None and n.get("host") and n.get("agent_url")]
    if nodes:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(nodes), 8),
                                thread_name_prefix="cpu-psi") as ex:
            list(ex.map(_sample_one, nodes))
    if time.time() - _last_flush >= STATS_FLUSH_S:
        _flush()
        _purger_episodes()
        _last_flush = time.time()


def _purger_episodes():
    """Retire les épisodes des conteneurs/nœuds DISPARUS. Sans ça le fichier grossit indéfiniment,
    et surtout un vmid recyclé hériterait de l'épisode d'un autre conteneur (donc un silence sur sa
    propre famine, l'état étant déjà « alerté »). Cadencé avec le flush des stats (5 min)."""
    try:
        from .database import db_get_containers
        vmids = {str(c.get("vmid")) for c in (db_get_containers() or [])}
        nids = {str(n.get("id")) for n in (db_get_nodes() or [])}
    except Exception as e:
        log.debug("purge des épisodes de pression CPU impossible (%s) — reportée.", e)
        return

    def garder(cle_txt):
        quoi, _, ident = cle_txt.partition("\x1f")
        if quoi == "cont":
            return ident in vmids
        if quoi == "host":
            return ident in nids
        return True

    n = _episodes.purger(garder)
    if n:
        log.info("Épisodes de pression CPU : %d entrée(s) purgée(s) (objet disparu).", n)


def _flush():
    with _lock:
        snap = {k: list(dq) for k, dq in _stats.items()}
    try:
        tmp = STATS_PERSIST_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, STATS_PERSIST_PATH)
    except OSError as e:
        log.debug("cpu_pressure flush: %s", e)


def load_persisted():
    global _last_flush
    try:
        with open(STATS_PERSIST_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    cutoff = time.time() - 86400
    with _lock:
        for k, rows in (data or {}).items():
            dq = _stats.setdefault(k, deque(maxlen=STATS_MAX))
            for r in rows or []:
                if isinstance(r, dict) and r.get("t", 0) >= cutoff:
                    dq.append(r)
    _last_flush = time.time()


# ─── Accès API / fusion node_health ──────────────────────────────────────────
def latest(node_id=None):
    with _lock:
        if node_id is None:
            return {k: dict(v) for k, v in _last.items()}
        return dict(_last.get(str(node_id)) or {})


def history(node_id):
    with _lock:
        return list(_hist.get(str(node_id), ()))


def for_container(vmid):
    """{some, full} avg60 du conteneur (ou {} si inconnu) — pour la table « qui consomme quoi »."""
    with _lock:
        for snap in _last.values():
            ent = (snap.get("containers") or {}).get(vmid)
            if ent:
                psi = ent.get("psi") or {}
                return {"some": (psi.get("some") or {}).get("avg60"),
                        "full": (psi.get("full") or {}).get("avg60")}
    return {}
