# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""RAPATRIEMENT d'un média pour un conteneur, en tâche de fond SUIVIE (player, bibliothèque).

Une tâche = (média, nœud du conteneur, champ de vision du conteneur) : deux demandes pour la même
copie se rattachent à la MÊME tâche au lieu d'en lancer une seconde (ou de se voir refuser, comme
quand le rapatriement occupait l'emplacement unique des opérations de la base, 409 « une opération
est déjà en cours »). États : queued → copying → verifying → done | failed. L'avancement vient du
rappel de `media_registry.copy_asset` (fraction lue chez l'agent cible toutes les 2 s) ; le débit
est mesuré sur une fenêtre glissante, pas déduit du plafond réglé. Une tâche finie est oubliée
`JOB_TTL_S` après sa fin. Aucun état persistant : un redémarrage de l'orchestrateur perd le suivi
(l'agent cible, lui, reprend sa copie au prochain essai)."""

import threading
import time
import uuid

from . import media_registry as mr

MAX_PARALLEL = 2          # copies simultanées (le plafond de débit est PAR copie) ; au-delà : queued
JOB_TTL_S = 600           # une tâche finie reste lisible 10 min, puis est oubliée
RATE_WINDOW_S = 12.0      # fenêtre du débit mesuré (l'agent est relu toutes les 2 s)
ACTIVE = ("queued", "copying", "verifying")

_lock = threading.Lock()
_sem = threading.Semaphore(MAX_PARALLEL)
_jobs = {}                # id → tâche
_by_key = {}              # (asset_id, node_id, préfixe, version) → id de la tâche ACTIVE


def _purge(now=None):
    now = now or time.time()
    for jid in [j for j, t in _jobs.items() if t["ended"] and now - t["ended"] > JOB_TTL_S]:
        _jobs.pop(jid, None)


def _public(t):
    """Vue renvoyée par l'API (sans les échantillons internes)."""
    rate = None
    s = t["_samples"]
    if t["state"] == "copying" and len(s) >= 2 and s[-1][0] > s[0][0]:
        rate = max(0.0, (s[-1][1] - s[0][1]) / (s[-1][0] - s[0][0]))
    eta = None
    if rate and t["total_bytes"]:
        eta = max(0.0, (t["total_bytes"] - t["done_bytes"]) / rate)
    return {"job": t["id"], "state": t["state"], "asset_id": t["asset_id"], "version": t["version"], "vmid": t["vmid"],
            "node_id": t["node_id"], "done_bytes": int(t["done_bytes"]), "total_bytes": t["total_bytes"],
            "rate": round(rate) if rate is not None else None, "eta_s": round(eta, 1) if eta is not None else None,
            "error": t["error"], "path": t["path"], "started": t["started"], "ended": t["ended"]}


def _source_size(aid, version):
    r = mr.db().execute("""SELECT size FROM media_files WHERE asset_id=? AND state='present' AND share_id=0
                           AND growing=0 AND kind='main' AND version=? ORDER BY node_id LIMIT 1""", (aid, version)).fetchone()
    return int(r["size"]) if r and r["size"] else None


def _progress_cb(t):
    def cb(frac):
        now = time.time()
        with _lock:
            frac = max(0.0, min(1.0, float(frac or 0)))
            if t["total_bytes"]:
                t["done_bytes"] = frac * t["total_bytes"]
            s = t["_samples"]
            s.append((now, t["done_bytes"]))
            while len(s) > 2 and now - s[0][0] > RATE_WINDOW_S:
                s.pop(0)
            # Fichier entier reçu : reste le sidecar, l'analyse du nœud et la comparaison d'empreinte.
            if frac >= 1.0:
                t["state"] = "verifying"
    return cb


def _run(t, fetch):
    try:
        with _sem:
            with _lock:
                t["state"] = "copying"
            path = fetch(t["asset_id"], t["vmid"], _progress_cb(t), t["version"])
        with _lock:
            t.update(state="done", path=path, done_bytes=t["total_bytes"] or t["done_bytes"])
    except Exception as e:                                                 # noqa: BLE001
        with _lock:
            t.update(state="failed", error=str(e) or e.__class__.__name__)
    finally:
        with _lock:
            t["ended"] = time.time()
            if _by_key.get(t["_key"]) == t["id"]:
                _by_key.pop(t["_key"], None)


def _default_fetch(aid, vmid, progress, version):
    return mr.fetch_for_container(aid, vmid, progress=progress, version=version)


def start(aid, vmid, fetch=None, version=None):
    """Lance (ou rejoint) le rapatriement de `aid` pour le conteneur `vmid` — de sa version par
    défaut, ou de `version`. Copie déjà là : tâche `done` tout de suite, `path` renseigné (l'appelant
    qui attend un chemin l'a). `fetch(aid, vmid, progress, version)` : injecté par les gardes ; par
    défaut la copie vérifiée de la base."""
    nid, prefix = mr.container_view(vmid)
    if nid is None:
        raise ValueError("conteneur sans nœud")
    version = mr.resolve_version(aid, version)
    key = (aid, int(nid), prefix, version)
    now = time.time()
    with _lock:
        _purge(now)
        jid = _by_key.get(key)
        if jid in _jobs and _jobs[jid]["state"] in ACTIVE:
            return _public(_jobs[jid])
    t = {"id": uuid.uuid4().hex[:16], "_key": key, "_samples": [], "asset_id": aid, "version": version, "vmid": int(vmid),
         "node_id": int(nid), "state": "queued", "done_bytes": 0.0, "total_bytes": None, "error": "",
         "path": None, "started": now, "ended": 0.0}
    lp = mr.local_path(mr._files_of(aid), nid, prefix, version=version)
    if lp is not None:
        mr.link_container_project(aid, vmid)
        t.update(state="done", path=lp, ended=now)
        with _lock:
            _jobs[t["id"]] = t
        return _public(t)
    t["total_bytes"] = _source_size(aid, version)
    with _lock:
        jid = _by_key.get(key)                    # course entre deux demandes simultanées
        if jid in _jobs and _jobs[jid]["state"] in ACTIVE:
            return _public(_jobs[jid])
        _jobs[t["id"]] = t
        _by_key[key] = t["id"]
    threading.Thread(target=_run, args=(t, fetch or _default_fetch), daemon=True,
                     name="media-fetch-%s" % t["id"]).start()
    with _lock:
        return _public(t)


def get(jid):
    with _lock:
        _purge()
        t = _jobs.get(jid)
        return _public(t) if t else None
