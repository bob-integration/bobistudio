# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
"""PLANCHE D'IMAGES d'un média vidéo — n images réparties sur la durée, en UNE image JPEG.

Sert le mode « Pellicule » de la bibliothèque (8 images par média, prises dans la planche) et,
plus tard, le survol instantané : une seule requête par média, déjà en cache, au lieu de n
extractions à la volée sur un nœud.

★ OÙ C'EST FAIT. Par le TRANSCODER, comme le proxy (« on sait où c'est fait ») : à la fin d'un job
de proxy de la ferme, sur le proxy tout juste écrit (images clés seules, une par seconde), ou en
job `storyboard` seul, priorité basse, pour un proxy fait avant (rattrapage). La planche reste
dans la copie de travail du transcoder (`.farm/storyboards/`) jusqu'à ce que l'orchestrateur la
RAPATRIE — par le contrôle :8082 du transcoder, qu'il joint déjà pour suivre le job, et non par le
port de transfert du nœud : le worker n'est pas toujours sur le nœud de la source (lecture à
distance, copie), et c'est lui qui a le fichier — puis l'efface. Rien ne reste sur le nœud.

★ OÙ C'EST GARDÉ. Centralement, comme les vignettes : `<données>/media_storyboards/<id>.jpg` +
`<id>.json` (description). Le .json est écrit APRÈS l'image : sa présence fait foi
(`has_storyboard`). Contrat servi à la bibliothèque (routes `storyboard.json` / `storyboard.jpg`) :
{n, cols, rows, w, h, times: [instant de chaque case, s], url} — cases cols × rows de w × h, ordre
ligne par ligne. Suppression du média → planche supprimée ; proxy recréé → planche refaite.
"""
import json
import logging
import os
import secrets
import threading
import time

from . import media_registry as mr

log = logging.getLogger(__name__)

STORYBOARD_DIR = os.path.join(os.path.dirname(mr.THUMB_DIR), "media_storyboards")
FETCH_MAX = 16 * 1024 * 1024          # une planche de 120 cases fait ~1 Mo : au-delà, ce n'en est pas une
AUTO_EVERY_S = 600                    # rattrapage automatique (réglage `media_proxy_auto`) : au plus toutes les 10 min
CONTRACT_KEYS = ("n", "cols", "rows", "w", "h", "times")

_ids_cache = {"mtime": None, "ids": frozenset()}
_ids_lock = threading.Lock()
_auto_last = [0.0]


def paths(aid):
    return os.path.join(STORYBOARD_DIR, "%s.jpg" % aid), os.path.join(STORYBOARD_DIR, "%s.json" % aid)


def _url(aid):
    return "/api/media/assets/%s/storyboard.jpg" % aid


def ids():
    """Médias qui ONT une planche — UNE lecture du dossier (relue seulement s'il a changé), pour
    marquer `has_storyboard` sur toute une liste sans une requête par ligne."""
    try:
        mt = os.stat(STORYBOARD_DIR).st_mtime_ns
    except OSError:
        return frozenset()
    with _ids_lock:
        if _ids_cache["mtime"] != mt:
            _ids_cache["ids"] = frozenset(f[:-5] for f in os.listdir(STORYBOARD_DIR) if f.endswith(".json"))
            _ids_cache["mtime"] = mt
        return _ids_cache["ids"]


def get(aid):
    """Description de la planche (contrat de la bibliothèque) + `url`, ou None."""
    jp, js = paths(aid)
    try:
        with open(js, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not os.path.isfile(jp):
        return None
    out = {k: d.get(k) for k in CONTRACT_KEYS}
    out["url"] = _url(aid)
    out["v"] = d.get("v")
    return out


def validate(desc):
    """Contrôle du contrat (ce que le transcoder a déclaré) : entiers positifs, n ≤ cols × rows,
    un instant par case. Lève ValueError."""
    try:
        n, cols, rows, w, h = (int(desc[k]) for k in ("n", "cols", "rows", "w", "h"))
        times = [round(float(t), 3) for t in desc["times"]]
    except (KeyError, TypeError, ValueError):
        raise ValueError("description de planche invalide")
    if min(n, cols, rows, w, h) <= 0 or n > cols * rows or len(times) != n or n > 1000:
        raise ValueError("description de planche incohérente")
    return {"n": n, "cols": cols, "rows": rows, "w": w, "h": h, "times": times}


def store(aid, jpg, desc, made=None):
    """Range la planche de `aid` : l'image puis la description (qui fait foi). Remplace l'ancienne."""
    if not jpg.startswith(b"\xff\xd8"):
        raise ValueError("planche : ce n'est pas un JPEG")
    d = validate(desc)
    os.makedirs(STORYBOARD_DIR, exist_ok=True)
    jp, js = paths(aid)
    with open(jp + ".tmp", "wb") as f:
        f.write(jpg)
    os.replace(jp + ".tmp", jp)
    d.update(size=len(jpg), v="%x" % int(time.time() * 1000), made=made or {})
    with open(js + ".tmp", "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(js + ".tmp", js)
    return get(aid)


def delete(aid):
    n = 0
    for p in paths(aid)[::-1]:            # la description d'abord : plus de planche annoncée
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    return n


# ─── Fabrication (ferme de transcodage) ─────────────────────────────────────────────────────
def _present_proxy(aid):
    """Proxy VIDÉO présent de `aid` : (node_id, path) — celui dont on tire une planche."""
    r = mr.db().execute("""SELECT node_id, path FROM media_files WHERE asset_id=? AND kind='proxy' AND state='present'
                           AND share_id=0 AND path LIKE '%.mp4' ORDER BY node_id LIMIT 1""", (aid,)).fetchone()
    return (r["node_id"], r["path"]) if r else (None, None)


def jobs(asset_id=None, limit=200):
    """Jobs de la ferme qui FONT une planche (planche seule, ou proxy qui la fait à la suite), les
    plus récents d'abord, avec l'état vu par la base des médias (cf. media_registry.proxy_jobs)."""
    farm = mr._farm()
    where, args = ["storyboard_asset IS NOT NULL"], []
    if asset_id is not None:
        where.append("storyboard_asset=?")
        args.append(asset_id)
    rows = farm._rows("SELECT id, storyboard_asset, job_type, status, phase, progress, error, created_at, completed_at "
                      "FROM farm_jobs WHERE %s ORDER BY id DESC LIMIT ?" % " AND ".join(where), args + [int(limit)])
    out = []
    for r in rows:
        st = mr._FARM_STATE.get(r["status"], r["status"])
        out.append({"id": r["id"], "asset_id": r["storyboard_asset"], "with_proxy": r["job_type"] != "storyboard",
                    "state": st, "phase": r["phase"], "waiting": st == "queued" and r["phase"] == "no_transcoder",
                    "progress": float(r["progress"] or 0), "error": r["error"] or "",
                    "created": r["created_at"], "ended": r["completed_at"]})
    return out


def _active_job(aid):
    return next((j for j in jobs(asset_id=aid, limit=20) if j["state"] in ("queued", "running")), None)


def enqueue(aid, user="", priority="low"):
    """Confie à la ferme la planche de `aid`, tirée de son proxy vidéo présent. Une planche déjà en
    route (job seul, ou proxy qui la fera) : ce job-là. Pas de proxy : refus explicite — la planche
    suit le proxy (« Créer le proxy »). → job (dict de `jobs`)."""
    r = mr.db().execute("SELECT tech_json FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not r:
        raise ValueError("média inconnu")
    if mr._media_kind(mr._j(r["tech_json"], None)) != "video":
        raise ValueError("planche : vidéo seulement")
    j = _active_job(aid)
    if j:
        return j
    nid, path = _present_proxy(aid)
    if nid is None:
        raise RuntimeError("pas de proxy présent : créer le proxy, la planche suit")
    jid = mr._farm().submit_storyboard(aid, nid, path, priority=priority, user={"username": user})
    return next(x for x in jobs(asset_id=aid, limit=5) if x["id"] == jid)


def missing(limit=100000):
    """Médias vidéo qui ont un proxy présent mais PAS de planche, ni de planche en route."""
    have = ids()
    rows = mr.db().execute("""SELECT DISTINCT asset_id FROM media_files WHERE kind='proxy' AND state='present'
                              AND share_id=0 AND path LIKE '%.mp4' AND asset_id IS NOT NULL""").fetchall()
    cand = [r["asset_id"] for r in rows if r["asset_id"] not in have]
    if not cand:
        return []
    busy = {j["asset_id"] for j in jobs(limit=5000) if j["state"] in ("queued", "running")}
    return [a for a in cand if a not in busy][:int(limit)]


def catch_up(user="", limit=100000):
    """« Préparer les planches manquantes » : un job basse priorité par média concerné.
    → {queued: n, refused: {id: motif}}."""
    queued, refused = 0, {}
    for aid in missing(limit):
        try:
            enqueue(aid, user=user)
            queued += 1
        except (ValueError, RuntimeError) as e:
            refused[aid] = str(e)
    return {"queued": queued, "refused": refused}


def status():
    """Avancement du rattrapage (Réglages → Base des médias → Proxys) : combien de médias ont un
    proxy vidéo, combien ont leur planche, combien en file / en cours / en ATTENTE d'un transcoder
    (aucun inscrit, ou aucun ≥ 0.11.0), combien en échec parmi les récents."""
    c = mr.db()
    proxied = {r["asset_id"] for r in c.execute("""SELECT DISTINCT asset_id FROM media_files WHERE kind='proxy'
                                                   AND state='present' AND share_id=0 AND path LIKE '%.mp4'""")}
    have = ids()
    js = jobs(limit=5000)
    last = {}
    for j in js:                                          # le plus récent par média
        last.setdefault(j["asset_id"], j)
    act = [j for j in last.values() if j["state"] in ("queued", "running")]
    return {"proxied": len(proxied), "with_storyboard": len(proxied & have), "total_storyboards": len(have),
            "missing": len([a for a in proxied if a not in have and not (last.get(a) or {}).get("state") in ("queued", "running")]),
            "queued": sum(1 for j in act if j["state"] == "queued"),
            "running": sum(1 for j in act if j["state"] == "running"),
            "waiting": sum(1 for j in act if j["waiting"]),
            "failed": sum(1 for a, j in last.items() if j["state"] == "failed" and a not in have)}


def auto_tick():
    """Rattrapage AUTOMATIQUE, s'il est voulu (même réglage que les proxys : `media_proxy_auto`) :
    appelé par l'analyse périodique, au plus toutes les AUTO_EVERY_S."""
    if not mr.proxy_auto_enabled() or time.time() - _auto_last[0] < AUTO_EVERY_S:
        return None
    _auto_last[0] = time.time()
    res = catch_up(user="auto")
    if res["queued"]:
        log.info("planches : %d manquante(s) mise(s) en file (automatique)", res["queued"])
    return res


def farm_done(job_id):
    """Fin d'un job de la ferme qui a fait (ou devait faire) une planche : la RAPATRIE depuis le
    transcoder qui l'a fabriquée (`/farm/share` puis `/farm/file/<jeton>`, comme un fichier servi
    entre transcoders), la range, puis l'efface chez lui. Un job de proxy dont le transcoder n'a
    pas fait la planche (antérieur à 0.11.0, ou échec de la planche seule) : une planche SEULE est
    mise en file — elle attendra un transcoder qui sait la faire, et la fiche le dira."""
    import requests
    farm = mr._farm()
    job = farm.get_job(int(job_id))
    if not job or job["status"] != "done" or not job.get("storyboard_asset"):
        return None
    aid = job["storyboard_asset"]
    info = mr._j(job.get("info"), {}) or {}
    sb = info.get("storyboard") if isinstance(info.get("storyboard"), dict) else None
    exists = mr.db().execute("SELECT 1 FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not sb:
        if exists and job["job_type"] != "storyboard":
            log.info("planche non faite par le job de proxy #%s (%s) : planche seule en file", job_id,
                     info.get("storyboard_error") or "transcoder antérieur à 0.11.0")
            try:
                enqueue(aid, user=job.get("created_by") or "")
            except (ValueError, RuntimeError) as e:
                log.warning("planche de %s : %s", aid, e)
        return None
    worker = next((t for t in farm.transcoders() if t["vmid"] == job.get("worker_vmid")), None)
    if not worker:
        raise RuntimeError("transcoder #%s introuvable : planche non rapatriée" % job.get("worker_vmid"))
    rel = sb.get("path") or ""
    if not rel.startswith(".farm/storyboards/"):
        raise RuntimeError("planche hors de la copie de travail : %s" % rel)
    tok = secrets.token_hex(16)
    try:
        if exists:
            code, r = farm._call(worker, "POST", "/farm/share", {"token": tok, "path": rel, "ttl_s": 600})
            if code != 200:
                raise RuntimeError("planche non servie par %s : %s" % (worker["hostname"], r.get("error") or code))
            resp = requests.get("%s/farm/file/%s" % (farm._base_url(worker), tok), timeout=60, stream=True)
            try:
                if resp.status_code != 200:
                    raise RuntimeError("planche : HTTP %d" % resp.status_code)
                buf = bytearray()
                for b in resp.iter_content(256 * 1024):
                    buf += b
                    if len(buf) > FETCH_MAX:
                        break
                data = bytes(buf)
            finally:
                resp.close()
            if len(data) > FETCH_MAX or (sb.get("size") and len(data) != int(sb["size"])):
                raise RuntimeError("planche tronquée ou démesurée (%d octets, %s annoncés)" % (len(data), sb.get("size")))
            store(aid, data, sb, made={"farm_job": job["id"], "worker": worker["hostname"],
                                       "worker_vmid": worker["vmid"], "make_s": sb.get("make_s"),
                                       "keyframes": sb.get("keyframes"), "at": mr._now()})
            log.info("planche de %s rapatriée (job #%s, %d cases, %d octets, %.1f s)", aid, job["id"], sb["n"],
                     len(data), float(sb.get("make_s") or 0))
    finally:
        farm._call_quiet(worker, "/farm/unshare", {"token": tok})
        farm._call_quiet(worker, "/farm/remove", {"path": rel})
    return get(aid)
