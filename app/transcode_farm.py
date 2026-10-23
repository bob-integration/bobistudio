# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Ferme de transcodage : UNE file centrale, des transcoders sur plusieurs nœuds qui la vident.

★ CE QUI EST CENTRAL, CE QUI NE L'EST PAS. La FILE (priorités, ordre, historique) vit ici, dans la
base de l'orchestrateur. Le TRAVAIL reste dans les conteneurs `transcoder` : l'orchestrateur leur
POUSSE un job quand ils ont une place libre (jobs simultanés − en cours − en attente locale), puis
suit ce job par leur `/job`. Un transcoder garde sa propre file pour ses jobs à lui (interface,
watchfolders) ; il ne prend des jobs de la ferme que s'il y est INSCRIT (`farm_members`).
Pousser plutôt que laisser les conteneurs venir chercher : un conteneur n'a ni identité ni accès
vers l'orchestrateur, alors que l'orchestrateur joint déjà tous les conteneurs.

★ LES FICHIERS. Les volumes /mnt/media sont LOCAUX à chaque nœud ; les partages réseau (/mnt/ext)
sont visibles de tous les nœuds qui les montent, en lecture seule. D'où trois manières de lire la
source (`access`), décidées au moment de confier le job :
  - `direct` : source sur un partage, ou sur le volume du nœud où tourne le transcoder. La sortie
    reste sur ce nœud — on ne copie rien ;
  - `stream` : source sur le volume d'un AUTRE nœud où tourne un transcoder du même projet. Ce
    transcoder sert le fichier en HTTP (jeton), ffmpeg le lit à distance : aucune copie de la
    source. La sortie est ensuite RAPATRIÉE sur le nœud de la source (le transcoder la tire) ;
  - `copy` : même cas, mais aucun transcoder ne peut servir le fichier là-bas. L'hôte du nœud
    source l'envoie (`curl -T`) au transcoder qui travaille, puis récupère la sortie (`curl -o`).
Une sortie rapatriée l'est sous le même dossier de projet que la source ; les copies de travail
(`.farm/` côté worker) sont effacées ensuite.

Le répartiteur ne tourne que sur le contrôleur ACTIF (démarré par main.py). Tout son état
durable est en base ; ce qui est en mémoire (transferts en cours) est rejoué au redémarrage.
"""
import datetime
import json
import logging
import posixpath
import secrets
import shlex
import threading
import time

import requests

from .database import get_db, db_get_container, db_get_containers, db_get_node, db_add_alert

log = logging.getLogger(__name__)

TICK_S = 3
STATE_TIMEOUT_S = 3
MEMBER_LOST_S = 90           # worker injoignable au-delà : son job repart en file
MAX_ATTEMPTS = 3             # remises en file (worker perdu, transfert échoué) avant l'échec
SHARE_TTL_S = 3 * 24 * 3600  # un film peut attendre longtemps dans la file locale d'un worker
TRANSFER_TIMEOUT_S = 12 * 3600
CONTROL_PORT = 8082
WORK_DIR = ".farm"

PRIORITIES = {"urgent": 20, "high": 10, "normal": 0, "low": -10}
PRIORITY_NAMES = {v: k for k, v in PRIORITIES.items()}
JOB_TYPES = ("video", "audio", "extract", "slate")
ACTIVE = ("pending", "transfer_in", "running", "transfer_out")
FINISHED = ("done", "failed", "cancelled")

_lock = threading.RLock()
_member_state = {}     # vmid → {"ok", "state", "seen", "error"}
_tasks = {}            # job id → transfert en cours (thread hôte ou rapatriement par un transcoder)
_started = False


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ─── Base ─────────────────────────────────────────────────────────────────────
def ensure_tables():
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS farm_members (
            vmid     INTEGER PRIMARY KEY,
            enabled  INTEGER NOT NULL DEFAULT 1,
            added_at TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS farm_jobs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id     INTEGER,
            src_kind       TEXT NOT NULL,          -- share | local
            src_grant_id   INTEGER,
            src_node_id    INTEGER,
            src_path       TEXT NOT NULL,          -- relatif au partage / au dossier du projet
            job_type       TEXT NOT NULL,
            params         TEXT,
            output_dir     TEXT,
            priority       INTEGER NOT NULL DEFAULT 0,
            status         TEXT NOT NULL DEFAULT 'pending',
            access         TEXT,                   -- direct | stream | copy
            worker_vmid    INTEGER,
            worker_node_id INTEGER,
            worker_job_id  INTEGER,
            server_vmid    INTEGER,                -- transcoder du nœud source (stream, rapatriement)
            token          TEXT,
            work_input     TEXT,
            work_output    TEXT,
            progress       REAL NOT NULL DEFAULT 0,
            phase          TEXT,
            out_node_id    INTEGER,
            out_path       TEXT,
            error          TEXT,
            info           TEXT,
            attempts       INTEGER NOT NULL DEFAULT 0,
            lost_since     REAL,
            created_by     TEXT,
            created_at     TEXT,
            started_at     TEXT,
            completed_at   TEXT)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_farm_jobs_status ON farm_jobs(status)")
        # PROXY DE LECTURE de la base des médias : le job produit une INSTANCE `kind=proxy` du média
        # `proxy_asset`, déposée à `proxy_rel` (dossier caché `.proxies/` à côté de l'original, sur le
        # nœud de la source) — pas une sortie ordinaire (pas de nouveau média, pas de filiation).
        cols = {r[1] for r in db.execute("PRAGMA table_info(farm_jobs)")}
        # PLANCHE D'IMAGES (base des médias) : `storyboard_asset` = le média dont le job fabrique la
        # planche — job `storyboard` seul (rattrapage d'un proxy existant), ou job de proxy qui la
        # fait à la suite (`params.storyboard`). Rapatriée chez l'orchestrateur à la fin
        # (app/media_storyboard.py:farm_done), jamais laissée sur le nœud.
        # `src_asset` : média de la base d'où vient la source d'un job demandé SUR UN MÉDIA (submit_asset) —
        # c'est ce qui rend le job visible à qui voit ce média par un partage sans être du projet.
        for col in ("proxy_asset", "proxy_rel", "storyboard_asset", "src_asset"):
            if col not in cols:
                db.execute("ALTER TABLE farm_jobs ADD COLUMN %s TEXT" % col)
        db.commit()


def _rows(sql, args=()):
    with get_db() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def _update(job_id, expect=None, **cols):
    """Écrit `cols` ; avec `expect`, seulement si le job est encore dans ce statut (ou l'un de
    ces statuts) — une annulation arrivée entre-temps gagne. → True si la ligne a changé."""
    keys = list(cols)
    sql = "UPDATE farm_jobs SET %s WHERE id=?" % ", ".join("%s=?" % k for k in keys)
    args = [cols[k] for k in keys] + [job_id]
    if expect:
        exp = (expect,) if isinstance(expect, str) else tuple(expect)
        sql += " AND status IN (%s)" % ",".join("?" * len(exp))
        args += list(exp)
    with get_db() as db:
        n = db.execute(sql, args).rowcount
        db.commit()
    return n > 0


def get_job(job_id):
    r = _rows("SELECT * FROM farm_jobs WHERE id=?", (job_id,))
    return r[0] if r else None


# ─── Transcoders et membres ───────────────────────────────────────────────────
def _type_of(c):
    dc = c.get("deploy_config")
    try:
        dc = json.loads(dc) if isinstance(dc, str) else (dc or {})
    except (ValueError, TypeError):
        dc = {}
    return (dc or {}).get("type")


def transcoders():
    """Tous les conteneurs transcoder, inscrits ou non : un non-inscrit peut encore SERVIR un
    fichier de son nœud (accès `stream`) ou recevoir une sortie rapatriée."""
    enabled = {r["vmid"]: bool(r["enabled"]) for r in _rows("SELECT * FROM farm_members")}
    out = []
    for c in db_get_containers() or []:
        if _type_of(c) != "transcoder":
            continue
        out.append({"vmid": c["vmid"], "hostname": c.get("hostname"), "node_id": c.get("node_id"),
                    "project_id": c.get("project_id"), "status": c.get("status"),
                    "ip": c.get("docker_ip"), "member": enabled.get(c["vmid"], False)})
    return out


def set_member(vmid, enabled):
    c = db_get_container(vmid)
    if not c or _type_of(c) != "transcoder":
        raise ValueError("pas un transcoder")
    with get_db() as db:
        db.execute("INSERT INTO farm_members(vmid, enabled, added_at) VALUES(?,?,?) "
                   "ON CONFLICT(vmid) DO UPDATE SET enabled=excluded.enabled",
                   (vmid, 1 if enabled else 0, _now()))
        db.commit()


def _rate():
    """Plafond de débit des transferts de la ferme (octets/s, None = sans) : le MÊME réglage que
    les copies de la base des médias (`media_copy_rate_mbps`, défaut 60 Mo/s). Une source lue à
    distance, une copie ou un rapatriement passent par le réseau qui porte aussi le pilotage des
    nœuds — mesuré à 78 Mo/s sans plafond sur un lien gigabit (cf. app/media_registry.py)."""
    try:
        from .media_registry import _copy_rate
        return _copy_rate()
    except Exception:
        return 60 * 10 ** 6


def _host_curl():
    """curl d'hôte à basse priorité (CPU et disque) et plafonné : un transfert de la ferme ne doit
    pas disputer le nœud aux conteneurs temps réel qui y tournent."""
    r = _rate()
    return "nice -n 19 ionice -c3 curl --fail --silent --show-error" + (" --limit-rate %d" % r if r else "")


def _headers(vmid):
    from .deploy import agent_headers
    try:
        return agent_headers(vmid)
    except Exception:
        return {}


def _base_url(t):
    """Adresse du contrôle d'un transcoder, telle que la joignent l'orchestrateur, un autre
    conteneur ou l'hôte d'un autre nœud (macvlan : tous sauf l'hôte du conteneur lui-même)."""
    return "http://%s:%d" % (t["ip"], CONTROL_PORT)


def _call(t, method, path, body=None, params=None, timeout=10):
    """Appel au contrôle :8082 d'un transcoder. → (code, json). Lève sur erreur réseau."""
    if not t.get("ip"):
        raise ConnectionError("transcoder %s sans IP" % t["vmid"])
    r = requests.request(method, _base_url(t) + path,
                         json=body, params=params, headers=_headers(t["vmid"]), timeout=timeout)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": (r.text or "")[:200]}


def _refresh_states(ts):
    now = time.time()
    for t in ts:
        if t["status"] != "running":
            _member_state[t["vmid"]] = {"ok": False, "state": {}, "seen": None,
                                        "error": "conteneur %s" % (t["status"] or "inconnu")}
            continue
        prev = _member_state.get(t["vmid"]) or {}
        try:
            code, st = _call(t, "GET", "/state", timeout=STATE_TIMEOUT_S)
            ok = code == 200
            _member_state[t["vmid"]] = {"ok": ok, "state": st if ok else {}, "seen": now if ok else prev.get("seen"),
                                        "error": None if ok else "HTTP %d" % code}
        except Exception as e:
            _member_state[t["vmid"]] = {"ok": False, "state": {}, "seen": prev.get("seen"),
                                        "error": type(e).__name__}


def _free_slots(t, reserved):
    st = (_member_state.get(t["vmid"]) or {}).get("state") or {}
    limit = int(st.get("max_concurrent") or 1)
    busy = int(st.get("jobs_running") or 0) + int(st.get("jobs_pending") or 0)
    return limit - busy - reserved.get(t["vmid"], 0)


def _farm_capable(t):
    m = _member_state.get(t["vmid"]) or {}
    return m.get("ok") and (m.get("state") or {}).get("farm_capable")


# ─── Création ─────────────────────────────────────────────────────────────────
def _clean_rel(path):
    """Chemin relatif propre. Une remontée (`..`) est REFUSÉE, pas ramenée sous la racine :
    `../etc/passwd` deviendrait `etc/passwd`, un fichier que personne n'a demandé."""
    raw = (path or "").strip()
    if ".." in raw.replace("\\", "/").split("/"):
        raise ValueError("chemin invalide")
    p = posixpath.normpath("/" + raw).lstrip("/")
    if not p or p == "." or p.startswith(".."):
        raise ValueError("chemin invalide")
    if any(part.startswith(".") for part in p.split("/")):
        raise ValueError("chemin caché refusé")
    return p


def parse_priority(v):
    if v in (None, ""):
        return 0
    s = str(v).strip().lower()
    if s in PRIORITIES:
        return PRIORITIES[s]
    try:
        n = int(s)
    except ValueError:
        raise ValueError("priorité inconnue (%s)" % ", ".join(PRIORITIES))
    if n not in PRIORITY_NAMES:
        raise ValueError("priorité inconnue (%s)" % ", ".join(PRIORITIES))
    return n


def submit(project_id, source, job_type, params=None, priority=None, output_dir="", user=None):
    """Crée un job de la ferme. `source` = {"kind": "share", "grant_id", "path"} (chemin dans le
    dossier accordé) ou {"kind": "local", "node_id", "path"} (chemin dans le dossier du projet sur
    ce nœud). Les paramètres d'encodage sont validés par le transcoder qui le prendra : un preset
    impossible échoue au moment de le confier, avec le message du transcoder. → id."""
    if job_type not in JOB_TYPES:
        raise ValueError("type invalide (%s)" % ", ".join(JOB_TYPES))
    kind = (source or {}).get("kind")
    path = _clean_rel((source or {}).get("path"))
    grant_id = node_id = None
    if kind == "share":
        grant_id = int(source.get("grant_id") or 0)
        from services import storage
        if not storage.container_entry(grant_id, project_id):
            raise ValueError("ce dossier réseau n'est pas accordé à ce projet")
    elif kind == "local":
        node_id = int(source.get("node_id") or 0)
        if not db_get_node(node_id):
            raise ValueError("nœud inconnu")
    else:
        raise ValueError("source : share ou local")
    out_dir = (output_dir or "").strip().strip("/")
    if out_dir:
        out_dir = _clean_rel(out_dir)
    params = {k: v for k, v in dict(params or {}).items() if not str(k).startswith("_")}
    params.pop("priority", None)
    prio = parse_priority(priority)
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO farm_jobs(project_id, src_kind, src_grant_id, src_node_id, src_path, job_type, "
            "params, output_dir, priority, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, kind, grant_id, node_id, path, job_type, json.dumps(params), out_dir, prio,
             (user or {}).get("username"), _now()))
        db.commit()
        return cur.lastrowid


def asset_source(asset_id, version=None):
    """Copie PRÉSENTE du média sur un nœud, lisible par la ferme → (node_id, chemin depuis la racine du
    volume). `version` : celle demandée (sinon la version par défaut, l'original d'abord)."""
    from . import media_registry as mr
    ver = mr.resolve_version(asset_id, version)
    r = mr.db().execute("""SELECT node_id, path FROM media_files WHERE asset_id=? AND state='present' AND growing=0
                           AND share_id=0 AND kind='main' AND version=? ORDER BY node_id LIMIT 1""",
                        (asset_id, ver)).fetchone()
    if not r:
        raise ValueError("aucune copie présente de ce média sur un nœud : le rapatrier d'abord")
    return r["node_id"], r["path"]


def submit_asset(asset_id, job_type, params=None, priority=None, output_dir="", user=None, version=None):
    """Job de la ferme demandé SUR UN MÉDIA de la base (y compris un média seulement PARTAGÉ avec
    l'appelant, qui n'est pas du projet qui le porte) : hors projet, source = la copie du média sur son
    nœud, sortie à côté de la source par défaut (le scan la range sous le média : une VERSION). Le droit
    d'agir sur le média est vérifié par l'appelant (media_sharing). → id."""
    if job_type not in JOB_TYPES:
        raise ValueError("type invalide (%s)" % ", ".join(JOB_TYPES))
    node_id, path = asset_source(asset_id, version)
    out_dir = (output_dir or "").strip().strip("/")
    out_dir = _clean_rel(out_dir) if out_dir else posixpath.dirname(path)
    params = {k: v for k, v in dict(params or {}).items() if not str(k).startswith("_")}
    params.pop("priority", None)
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO farm_jobs(project_id, src_kind, src_node_id, src_path, job_type, params, output_dir, priority, "
            "created_by, created_at, src_asset) VALUES(NULL,'local',?,?,?,?,?,?,?,?,?)",
            (int(node_id), _clean_rel(path), job_type, json.dumps(params), out_dir, parse_priority(priority),
             (user or {}).get("username"), _now(), asset_id))
        db.commit()
        return cur.lastrowid


PROXY_PRESETS = {"video": {"preset": "playback_proxy"}, "audio": {"audio_preset": "aac_128"}}
STORYBOARD_SPEC = {"max": 120, "w": 320}    # 120 cases au plus (une par seconde au plus), 320 px de large


def submit_proxy(asset_id, node_id, src_path, proxy_rel, kind="video", priority="low", user=None, storyboard=True):
    """Job de PROXY DE LECTURE (base des médias) : source = copie du média sur le volume du nœud
    `node_id` (chemin depuis la racine du volume : job HORS projet, pris par un transcoder sans
    projet, qui voit tout le volume) ; sortie = `proxy_rel` sur ce MÊME nœud, à l'emplacement
    exact (le préréglage fixe du transcoder : `playback_proxy`, ou `aac_128` pour l'audio seul).
    Priorité BASSE par défaut : un proxy passe après le travail demandé par quelqu'un. → id."""
    if kind not in PROXY_PRESETS:
        raise ValueError("proxy : vidéo ou audio seulement")
    path = _clean_rel(src_path)
    d, name = posixpath.split(proxy_rel or "")
    if posixpath.basename(d) != ".proxies" or not name or name.startswith("."):
        raise ValueError("emplacement de proxy invalide")
    parent = posixpath.dirname(d)
    if parent:
        _clean_rel(parent)
    if not db_get_node(int(node_id)):
        raise ValueError("nœud inconnu")
    params = dict(PROXY_PRESETS[kind])
    sb = storyboard and kind == "video"       # planche d'images à la suite, sur le proxy écrit (`_worker_params`)
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO farm_jobs(project_id, src_kind, src_node_id, src_path, job_type, params, output_dir, priority, "
            "created_by, created_at, proxy_asset, proxy_rel, storyboard_asset) VALUES(NULL,'local',?,?,?,?,?,?,?,?,?,?,?)",
            (int(node_id), path, "video" if kind == "video" else "audio", json.dumps(params), d,
             parse_priority(priority), (user or {}).get("username"), _now(), asset_id, proxy_rel,
             asset_id if sb else None))
        db.commit()
        return cur.lastrowid


def submit_storyboard(asset_id, node_id, proxy_path, priority="low", user=None):
    """Job de PLANCHE D'IMAGES seule (base des médias), à partir du PROXY existant `proxy_path` sur
    le volume du nœud `node_id` (hors projet, comme un proxy) : rattrapage des proxys faits avant
    que le transcoder sache la faire. Rien n'est déposé sur le nœud : la planche reste dans la copie
    de travail du transcoder jusqu'au rapatriement par l'orchestrateur. Priorité BASSE. → id."""
    d, name = posixpath.split((proxy_path or "").strip().lstrip("/"))
    if posixpath.basename(d) != ".proxies" or not name or name.startswith(".") or "/" in name:
        raise ValueError("planche : la source doit être un proxy (.proxies/)")
    parent = posixpath.dirname(d)
    path = (_clean_rel(parent) + "/" if parent else "") + ".proxies/" + name
    if not db_get_node(int(node_id)):
        raise ValueError("nœud inconnu")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO farm_jobs(project_id, src_kind, src_node_id, src_path, job_type, params, output_dir, priority, "
            "created_by, created_at, storyboard_asset) VALUES(NULL,'local',?,?,'storyboard',?,'',?,?,?,?)",
            (int(node_id), path, json.dumps({"storyboard": dict(STORYBOARD_SPEC)}), parse_priority(priority),
             (user or {}).get("username"), _now(), asset_id))
        db.commit()
        return cur.lastrowid


def _able(t, job):
    """Le transcoder `t` sait-il faire CE job ? Une planche seule exige un transcoder ≥ 0.11.0
    (`/state.storyboard`) : un plus ancien la refuserait (« type invalide »), et le job échouerait
    alors qu'il suffit d'attendre la mise à jour."""
    if job.get("job_type") != "storyboard":
        return True
    return bool(((_member_state.get(t["vmid"]) or {}).get("state") or {}).get("storyboard"))


# ─── Répartition ──────────────────────────────────────────────────────────────
def _candidates(job, ts, reserved):
    """Transcoders inscrits qui peuvent prendre CE job maintenant, le meilleur d'abord, avec la
    manière d'y lire la source. → [(transcoder, access, input, server)]."""
    from services import storage
    out = []
    grant = None
    if job["src_kind"] == "share":
        grant = next((g for g in storage._grants() if g["id"] == job["src_grant_id"]), None)
        if not grant:
            return []
    for t in ts:
        if not t["member"] or not _farm_capable(t) or t["project_id"] != job["project_id"] or not _able(t, job):
            continue
        if _free_slots(t, reserved) <= 0:
            continue
        if job["src_kind"] == "share":
            entry = storage.container_entry(grant["id"], job["project_id"])
            if not entry or not storage.node_has_conn(t["node_id"], grant["conn_id"]):
                continue
            out.append((t, "direct", entry + "/" + job["src_path"], None))
        elif t["node_id"] == job["src_node_id"]:
            out.append((t, "direct", job["src_path"], None))
        else:
            server = next((s for s in ts if s["node_id"] == job["src_node_id"]
                           and s["project_id"] == job["project_id"] and _farm_capable(s)), None)
            out.append((t, "stream" if server else "copy", None, server))
    rank = {"direct": 0, "stream": 1, "copy": 2}
    out.sort(key=lambda c: (rank[c[1]], -_free_slots(c[0], reserved)))
    return out


def _worker_params(job, extra):
    p = json.loads(job["params"] or "{}")
    p.update(extra)
    p["_farm_job"] = job["id"]
    p["priority"] = PRIORITY_NAMES.get(job["priority"], "normal")
    if job.get("proxy_asset"):
        p["_farm_proxy"] = job["proxy_asset"]       # pas de sidecar : instance du média, pas un nouveau média
        p["overwrite"] = True                      # « Recréer » remplace le proxy à son emplacement
    if job.get("storyboard_asset") and "storyboard" not in p:
        p["storyboard"] = dict(STORYBOARD_SPEC)    # proxy vidéo : la planche d'images à la suite
    # Qui a demandé : repris dans le sidecar de la sortie (base des médias).
    p.setdefault("_origin", {})["user"] = job.get("created_by") or ""
    return p


_IDENTITY_PY = r'''
import hashlib, json, os, re, sys, datetime
p = sys.argv[1]
st = os.stat(p)
chunk = 4 << 20
h = hashlib.blake2b(digest_size=16)
h.update(str(st.st_size).encode())
with open(p, "rb") as f:
    h.update(f.read(chunk))
    if st.st_size > chunk:
        f.seek(max(chunk, st.st_size - chunk))
        h.update(f.read(chunk))
aid = ""
try:
    with open(p + ".xml", encoding="utf-8", errors="replace") as f:
        m = re.search(r'<media\b[^>]*?\bid="([^"]+)"', f.read(65536))
        aid = m.group(1) if m else ""
except OSError:
    pass
print(json.dumps({"fingerprint": h.hexdigest(), "size": st.st_size, "asset_id": aid,
                  "mtime": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}))
'''


def _source_identity(job):
    """Identité d'une source LOCALE lue ailleurs (stream, copy) : le transcoder qui travaille ne
    voit ni son sidecar ni le fichier lui-même. Relevée sur l'hôte du nœud source, avec la MÊME
    empreinte que la base des médias (blake2b de la taille + 4 premiers et 4 derniers Mo), pour
    que la sortie garde sa filiation. Meilleur effort : sans elle, le job se fait quand même."""
    from . import node_driver
    node = db_get_node(job["src_node_id"])
    try:
        rc, out, _ = node_driver.host_exec(
            node, "python3 -c %s %s" % (shlex.quote(_IDENTITY_PY),
                                        shlex.quote(_host_abs(node, job["project_id"], job["src_path"]))),
            timeout=60)
        d = json.loads((out or "").strip().splitlines()[-1]) if rc == 0 else {}
    except Exception:
        d = {}
    extra = {"_farm_src": {"path": job["src_path"], "node": (node or {}).get("name"),
                           "fingerprint": d.get("fingerprint"), "size": d.get("size"), "mtime": d.get("mtime")}}
    if d.get("asset_id"):
        extra["_origin"] = {"asset_id": d["asset_id"], "user": job.get("created_by") or ""}
    return extra


class Rejected(Exception):
    """Le transcoder REFUSE le job (preset impossible, cadence illisible…) : le refaire ailleurs
    donnerait le même refus — échec immédiat, avec son message."""


def _submit_to_worker(job, t, inp, params):
    body = {"type": job["job_type"], "input": inp, "params": params}
    if job.get("proxy_asset"):
        # Proxy : nom de sortie IMPOSÉ — à son emplacement final (accès direct), ou sous la copie
        # de travail du worker (rapatrié ensuite sous ce même nom, dans `output_dir` = `.proxies/`).
        name = posixpath.basename(job["proxy_rel"])
        body["output"] = job["proxy_rel"] if params.get("output_dir") == job["output_dir"] else \
            posixpath.join(params.get("output_dir") or "", name)
    code, r = _call(t, "POST", "/submit", body)
    if code in (400, 404):    # paramètres refusés, source introuvable : ailleurs, même verdict
        raise Rejected(r.get("error") or "refusé (HTTP %d)" % code)
    if code != 200 or not r.get("ok"):
        raise RuntimeError(r.get("error") or "HTTP %d" % code)
    return int(r["id"])


def _dispatch(job, ts, reserved):
    cands = _candidates(job, ts, reserved)
    if not cands:
        return False
    t, access, inp, server = cands[0]
    reserved[t["vmid"]] = reserved.get(t["vmid"], 0) + 1
    base = {"worker_vmid": t["vmid"], "worker_node_id": t["node_id"], "access": access,
            "server_vmid": server["vmid"] if server else None, "error": None, "lost_since": None,
            "progress": 0, "started_at": _now()}
    name = posixpath.basename(job["src_path"])
    try:
        if access == "direct":
            jid = _submit_to_worker(job, t, inp, _worker_params(job, {"output_dir": job["output_dir"] or ""}))
            _update(job["id"], "pending", status="running", phase="queued", worker_job_id=jid,
                    work_input=inp, **base)
        elif access == "stream":
            tok = secrets.token_hex(16)
            code, r = _call(server, "POST", "/farm/share",
                            {"token": tok, "path": job["src_path"], "ttl_s": SHARE_TTL_S, "rate": _rate()})
            if code != 200:
                raise RuntimeError("partage de la source refusé par %s : %s"
                                   % (server["hostname"], r.get("error") or code))
            url = "%s/farm/file/%s" % (_base_url(server), tok)
            try:
                jid = _submit_to_worker(job, t, url, _worker_params(job, dict(_source_identity(job), **{
                    "output_dir": "%s/out/%d" % (WORK_DIR, job["id"]), "_farm_src_name": name})))
            except Exception:
                _call_quiet(server, "/farm/unshare", {"token": tok})
                raise
            _update(job["id"], "pending", status="running", phase="queued", worker_job_id=jid,
                    token=tok, work_input=url, **base)
        else:
            tok = secrets.token_hex(16)
            dest = "%s/in/%d/%s" % (WORK_DIR, job["id"], name)
            code, r = _call(t, "POST", "/farm/receive", {"token": tok, "dest": dest, "ttl_s": SHARE_TTL_S})
            if code != 200:
                raise RuntimeError("réception refusée par %s : %s" % (t["hostname"], r.get("error") or code))
            if _update(job["id"], "pending", status="transfer_in", phase="copy_in", token=tok, **base):
                _start_copy_in(get_job(job["id"]), t)
        log.info("ferme : job #%d → %s (%s)", job["id"], t["hostname"], access)
        return True
    except Rejected as e:
        _update(job["id"], "pending", status="failed", error="%s : %s" % (t["hostname"], e),
                completed_at=_now(), phase=None)
        return True
    except Exception as e:
        _requeue(job, "%s : %s" % (t["hostname"], e))
        return True


def _requeue(job, why):
    """Remet en file (worker perdu, transfert raté) ; échec définitif après MAX_ATTEMPTS."""
    n = (job.get("attempts") or 0) + 1
    _cleanup(job)
    if n >= MAX_ATTEMPTS:
        _update(job["id"], ACTIVE, status="failed", error=why, attempts=n, completed_at=_now(), phase=None)
        db_add_alert("Ferme de transcodage : job #%d en échec après %d tentatives — %s" % (job["id"], n, why),
                     "error", kind="advisory")
    else:
        _update(job["id"], ACTIVE, status="pending", error=why, attempts=n, worker_vmid=None,
                worker_node_id=None, worker_job_id=None, server_vmid=None, token=None, access=None,
                work_input=None, work_output=None, phase=None, progress=0, lost_since=None)
    log.warning("ferme : job #%d remis en file (%d) : %s", job["id"], n, why)


# ─── Transferts par l'hôte (accès `copy`) ─────────────────────────────────────
def _host_abs(node, project_id, rel):
    from .docker_compute import project_media_dir
    return project_media_dir(node, project_id).rstrip("/") + "/" + rel


def _start_copy_in(job, t):
    def run():
        from . import node_driver
        try:
            node = db_get_node(job["src_node_id"])
            src = _host_abs(node, job["project_id"], job["src_path"])
            url = "%s/farm/upload/%s" % (_base_url(t), job["token"])
            rc, out, err = node_driver.host_exec(
                node, "%s -T %s %s" % (_host_curl(), shlex.quote(src), shlex.quote(url)),
                timeout=TRANSFER_TIMEOUT_S)
            if rc != 0:
                raise RuntimeError("copie de la source échouée : %s" % ((err or out or "")[-200:]))
            code, st = _call(t, "GET", "/farm/receive", params={"token": job["token"]})
            if code != 200 or st.get("state") != "done":
                raise RuntimeError("copie non reçue : %s" % (st.get("error") or st.get("state") or code))
            cur = get_job(job["id"])
            if not cur or cur["status"] != "transfer_in":
                return    # annulé pendant la copie : _cleanup s'en est chargé
            try:
                jid = _submit_to_worker(cur, t, st["path"], _worker_params(cur, dict(_source_identity(cur), **{
                    "output_dir": "%s/out/%d" % (WORK_DIR, cur["id"]),
                    "_farm_src_name": posixpath.basename(cur["src_path"])})))
            except Rejected as e:
                _update(job["id"], "transfer_in", status="failed", error="%s : %s" % (t["hostname"], e),
                        completed_at=_now(), phase=None, work_input=st["path"])
                _cleanup(get_job(job["id"]))
                return
            _update(job["id"], "transfer_in", status="running", phase="queued", worker_job_id=jid,
                    work_input=st["path"])
        except Exception as e:
            cur = get_job(job["id"])
            if cur and cur["status"] == "transfer_in":
                _requeue(cur, str(e))
        finally:
            with _lock:
                _tasks.pop(job["id"], None)
    th = threading.Thread(target=run, daemon=True, name="farm-copy-in-%d" % job["id"])
    with _lock:
        _tasks[job["id"]] = {"kind": "copy_in", "thread": th}
    th.start()


def _start_return(job, worker):
    """Rapatrie la sortie du worker sur le nœud de la source : tirée par un transcoder de ce
    nœud s'il y en a un (`/farm/fetch`), sinon par l'hôte (`curl -o`)."""
    from . import node_driver
    tok = secrets.token_hex(16)
    code, r = _call(worker, "POST", "/farm/share", {"token": tok, "path": job["work_output"], "ttl_s": SHARE_TTL_S,
                                                    "rate": _rate()})
    if code != 200:
        raise RuntimeError("partage de la sortie refusé : %s" % (r.get("error") or code))
    url = "%s/farm/file/%s" % (_base_url(worker), tok)
    # Le sidecar (identité, filiation, traitement) suit la sortie. Absent (écriture ratée chez le
    # worker) : la sortie revient seule — la base des médias l'analysera sans filiation.
    side_tok, side_url = secrets.token_hex(16), None
    try:
        code, _ = _call(worker, "POST", "/farm/share", {"token": side_tok, "path": job["work_output"] + ".xml",
                                                        "ttl_s": SHARE_TTL_S})
        if code == 200:
            side_url = "%s/farm/file/%s" % (_base_url(worker), side_tok)
    except Exception:
        pass
    dest = posixpath.join(job["output_dir"] or "", posixpath.basename(job["work_output"]))
    ts = transcoders()
    server = next((s for s in ts if s["node_id"] == job["src_node_id"] and s["project_id"] == job["project_id"]
                   and _farm_capable(s)), None)
    if server:
        code, r = _call(server, "POST", "/farm/fetch", {"token": tok, "url": url, "dest": dest,
                                                        "sidecar_url": side_url, "rate": _rate()})
        if code != 200:
            raise RuntimeError("rapatriement refusé par %s : %s" % (server["hostname"], r.get("error") or code))
        with _lock:
            _tasks[job["id"]] = {"kind": "return_server", "server": server, "token": tok,
                                 "worker_tokens": [tok, side_tok]}
        return

    def run():
        try:
            node = db_get_node(job["src_node_id"])
            final = _host_abs(node, job["project_id"], dest)
            d, b = posixpath.split(final)
            stem, ext = posixpath.splitext(b)
            # Jamais d'écrasement d'un AUTRE fichier, comme le transcoder : nom_1, nom_2… Le fichier de
            # la MÊME version du même média (job refait à l'identique : mêmes `id` et `version` dans
            # les deux sidecars, celui qui arrive lu d'abord) est remplacé — une version n'est jamais doublée.
            script = ('set -e; D=%(d)s; mkdir -p "$D"; f=%(f)s; s="$D/.$(basename "$f").xml.new"; rm -f "$s"; '
                      '%(side)s'
                      'ident() { grep -o -m1 "<media[^>]*>" "$1" 2>/dev/null '
                      '| grep -o \' id="[^"]*"\\| version="[^"]*"\' | sort | tr -d "\\n"; }; '
                      'same=0; if [ -e "$f" ] && [ -s "$s" ] && [ -e "$f.xml" ]; then '
                      'a="$(ident "$f.xml")"; b="$(ident "$s")"; [ -n "$b" ] && [ "$a" = "$b" ] && same=1; fi; '
                      'if [ $same = 0 ]; then i=0; while [ -e "$f" ]; do i=$((i+1)); f=%(stem)s_$i%(ext)s; done; fi; '
                      't="$(dirname "$f")/.$(basename "$f").part"; '
                      '%(curl)s -o "$t" %(url)s && mv -f "$t" "$f" '
                      '|| { rm -f "$t" "$s"; exit 1; }; '
                      'if [ -s "$s" ]; then mv -f "$s" "$f.xml"; else rm -f "$s"; fi; '
                      'echo "$f"'
                      % {"d": shlex.quote(d), "f": shlex.quote(final), "curl": _host_curl(),
                         "stem": shlex.quote(d + "/" + stem), "ext": shlex.quote(ext), "url": shlex.quote(url),
                         # sidecar : meilleur effort, sous le nom FINAL du média
                         "side": ('curl --fail --silent -o "$s" %s || rm -f "$s"; ' % shlex.quote(side_url))
                         if side_url else ""})
            rc, out, err = node_driver.host_exec(node, script, timeout=TRANSFER_TIMEOUT_S)
            if rc != 0:
                raise RuntimeError("rapatriement échoué : %s" % ((err or out or "")[-200:]))
            base = _host_abs(node, job["project_id"], "").rstrip("/") + "/"
            got = (out or "").strip().splitlines()[-1]
            with _lock:
                _tasks[job["id"]] = {"kind": "return_host", "state": "done",
                                     "path": got[len(base):] if got.startswith(base) else got}
        except Exception as e:
            with _lock:
                _tasks[job["id"]] = {"kind": "return_host", "state": "failed", "error": str(e)}
    th = threading.Thread(target=run, daemon=True, name="farm-return-%d" % job["id"])
    with _lock:
        _tasks[job["id"]] = {"kind": "return_host", "state": "running", "thread": th}
    th.start()


def _cleanup(job):
    """Efface ce que le job a laissé chez les autres (meilleur effort) : partage de la source,
    copies de travail du worker. Jamais la sortie livrée."""
    ts = {t["vmid"]: t for t in transcoders()}
    worker, server = ts.get(job.get("worker_vmid")), ts.get(job.get("server_vmid"))
    try:
        if server and job.get("access") == "stream" and job.get("token"):
            _call(server, "POST", "/farm/unshare", {"token": job["token"]}, timeout=5)
    except Exception:
        pass
    if worker and job.get("access") in ("stream", "copy"):
        wo = job.get("work_output")
        for p in (wo, wo + ".xml" if wo else None, job.get("work_input")):
            if p and p.startswith(WORK_DIR + "/"):
                try:
                    _call(worker, "POST", "/farm/remove", {"path": p}, timeout=5)
                except Exception:
                    pass


# ─── Suivi ────────────────────────────────────────────────────────────────────
def _finish(job, out_node_id, out_path, info=None):
    if _update(job["id"], ("running", "transfer_out"), status="done", progress=1.0, phase=None,
               out_node_id=out_node_id, out_path=out_path, completed_at=_now(),
               **({"info": json.dumps(info)} if info is not None else {})):
        log.info("ferme : job #%d terminé → nœud %s : %s", job["id"], out_node_id, out_path)
        if job.get("access") in ("stream", "copy"):
            _cleanup(get_job(job["id"]))
        if job.get("proxy_asset"):
            try:
                from .media_registry import proxy_farm_done
                proxy_farm_done(job["id"])
            except Exception as e:                  # l'analyse suivante du nœud le rattrapera
                log.warning("ferme : proxy du job #%d non enregistré : %s", job["id"], e)
        if job.get("storyboard_asset"):
            try:
                from .media_storyboard import farm_done
                farm_done(job["id"])
            except Exception as e:                  # « Préparer les planches manquantes » la refera
                log.warning("ferme : planche du job #%d non rapatriée : %s", job["id"], e)


def _follow(job, ts):
    worker = ts.get(job["worker_vmid"])
    if job["status"] == "transfer_in":
        with _lock:
            alive = job["id"] in _tasks
        if not alive:     # orchestrateur redémarré pendant la copie
            _requeue(job, "copie interrompue (redémarrage de l'orchestrateur)")
        return
    if not worker:
        _requeue(job, "transcoder #%s disparu" % job["worker_vmid"])
        return
    m = _member_state.get(worker["vmid"]) or {}
    if not m.get("ok"):
        since = job.get("lost_since") or time.time()
        if not job.get("lost_since"):
            _update(job["id"], lost_since=since)
        if time.time() - since > MEMBER_LOST_S:
            _requeue(job, "transcoder %s injoignable (%s)" % (worker["hostname"], m.get("error")))
        return
    if job.get("lost_since"):
        _update(job["id"], lost_since=None)
    if job["status"] == "transfer_out":
        _follow_return(job, worker)
        return
    try:
        code, j = _call(worker, "GET", "/job", params={"id": job["worker_job_id"]})
    except Exception:
        return
    if code == 404:
        _requeue(job, "job disparu de la file de %s" % worker["hostname"])
        return
    if code != 200:
        return
    st = j.get("status")
    info = j.get("info") if isinstance(j.get("info"), dict) else None
    if st in ("pending", "running"):
        _update(job["id"], "running", progress=float(j.get("progress") or 0),
                phase=("queued" if st == "pending" else (j.get("phase") or "encodage")),
                **({"info": json.dumps(info)} if info else {}))
    elif st == "done":
        if job["access"] == "direct" or job["job_type"] == "storyboard":
            # Planche seule : rien à rapatrier sur le nœud (l'orchestrateur la prend au worker).
            _finish(job, worker["node_id"], j.get("output"), info)
        else:
            _update(job["id"], "running", status="transfer_out", phase="copy_out", progress=1.0,
                    work_output=j.get("output"), **({"info": json.dumps(info)} if info else {}))
    elif st == "failed":
        _update(job["id"], "running", status="failed", error=j.get("error") or "échec", phase=None,
                completed_at=_now(), **({"info": json.dumps(info)} if info else {}))
        _cleanup(job)
    elif st == "cancelled":
        _update(job["id"], "running", status="cancelled", phase=None, completed_at=_now(),
                error="annulé sur le transcoder %s" % worker["hostname"])
        _cleanup(job)


def _follow_return(job, worker):
    with _lock:
        task = _tasks.get(job["id"])
    if not task:
        try:
            _start_return(job, worker)
        except Exception as e:
            _requeue_return(job, str(e))
        return
    if task["kind"] == "return_server":
        try:
            code, st = _call(task["server"], "GET", "/farm/fetch", params={"token": task["token"]})
        except Exception:
            return
        state = st.get("state") if code == 200 else "failed"
        if state == "done":
            with _lock:
                _tasks.pop(job["id"], None)
            for wt in task["worker_tokens"]:
                _call_quiet(worker, "/farm/unshare", {"token": wt})
            _finish(job, job["src_node_id"], st.get("path"))
        elif state == "failed":
            with _lock:
                _tasks.pop(job["id"], None)
            _requeue_return(job, st.get("error") or "rapatriement échoué")
    elif task.get("state") == "done":
        with _lock:
            _tasks.pop(job["id"], None)
        _finish(job, job["src_node_id"], task["path"])
    elif task.get("state") == "failed":
        with _lock:
            _tasks.pop(job["id"], None)
        _requeue_return(job, task["error"])


def _requeue_return(job, why):
    """Un rapatriement raté se RETENTE (la sortie est encore chez le worker) ; il n'y a pas lieu
    de réencoder. Échec définitif après MAX_ATTEMPTS — la sortie reste alors chez le worker, et
    le message le dit."""
    n = (job.get("attempts") or 0) + 1
    if n >= MAX_ATTEMPTS:
        _update(job["id"], "transfer_out", status="failed", attempts=n, completed_at=_now(), phase=None,
                out_node_id=job["worker_node_id"], out_path=job["work_output"],
                error="rapatriement impossible (%s) — la sortie est restée sur le nœud du transcoder" % why)
    else:
        _update(job["id"], "transfer_out", attempts=n, error=why)


def _call_quiet(t, path, body):
    try:
        _call(t, "POST", path, body, timeout=5)
    except Exception:
        pass


def tick():
    ensure_tables()
    ts_list = transcoders()
    _refresh_states(ts_list)
    ts = {t["vmid"]: t for t in ts_list}
    for job in _rows("SELECT * FROM farm_jobs WHERE status IN ('transfer_in','running','transfer_out')"):
        try:
            _follow(job, ts)
        except Exception as e:
            log.exception("ferme : suivi du job #%d : %s", job["id"], e)
    # Places déjà promises dans ce passage, et copies en cours (le worker n'a pas encore le job).
    reserved = {}
    for j in _rows("SELECT worker_vmid FROM farm_jobs WHERE status='transfer_in'"):
        reserved[j["worker_vmid"]] = reserved.get(j["worker_vmid"], 0) + 1
    for job in _rows("SELECT * FROM farm_jobs WHERE status='pending' ORDER BY priority DESC, id"):
        try:
            if not _dispatch(job, ts_list, reserved):
                _mark_waiting(job, ts_list)
        except Exception as e:
            log.exception("ferme : répartition du job #%d : %s", job["id"], e)


def _mark_waiting(job, ts_list):
    """Pourquoi un job en file N'EST PAS parti, écrit dans `phase` : `no_transcoder` = aucun
    transcoder inscrit, joignable et apte à la ferme pour ce projet — il attendra indéfiniment
    sans qu'on le sache, ce qui doit se VOIR (fiche du média, ferme) ; `busy` = il y en a, tous
    occupés. Écrit seulement quand ça change."""
    able = any(t["member"] and _farm_capable(t) and t["project_id"] == job["project_id"] and _able(t, job)
               for t in ts_list)
    want = "busy" if able else "no_transcoder"
    if job.get("phase") != want:
        _update(job["id"], "pending", phase=want)


def _loop():
    while True:
        try:
            tick()
        except Exception as e:
            log.exception("ferme de transcodage : %s", e)
        time.sleep(TICK_S)


def start():
    """Contrôleur ACTIF seulement (main.py) : deux répartiteurs confieraient deux fois le job."""
    global _started
    if _started:
        return
    _started = True
    ensure_tables()
    threading.Thread(target=_loop, daemon=True, name="transcode-farm").start()


# ─── Actions ──────────────────────────────────────────────────────────────────
def cancel(job_id):
    job = get_job(job_id)
    if not job:
        raise LookupError("job introuvable")
    if job["status"] not in ACTIVE:
        raise ValueError("job déjà terminé")
    worker = next((t for t in transcoders() if t["vmid"] == job["worker_vmid"]), None)
    if worker and job["worker_job_id"] and job["status"] == "running":
        _call_quiet(worker, "/cancel", {"id": job["worker_job_id"]})
    _update(job_id, ACTIVE, status="cancelled", phase=None, completed_at=_now())
    with _lock:
        _tasks.pop(job_id, None)
    _cleanup(job)


def retry(job_id):
    job = get_job(job_id)
    if not job:
        raise LookupError("job introuvable")
    if job["status"] not in FINISHED:
        raise ValueError("job encore actif")
    _update(job_id, FINISHED, status="pending", attempts=0, error=None, progress=0, phase=None,
            worker_vmid=None, worker_node_id=None, worker_job_id=None, server_vmid=None, token=None,
            access=None, work_input=None, work_output=None, out_node_id=None, out_path=None,
            info=None, started_at=None, completed_at=None, lost_since=None)


def set_priority(job_id, priority):
    prio = parse_priority(priority)
    if not _update(job_id, "pending", priority=prio):
        raise ValueError("seul un job en attente change de priorité")
    return prio


def purge(project_ids=None):
    sql = "DELETE FROM farm_jobs WHERE status IN ('done','cancelled')"
    args = []
    if project_ids is not None:
        if not project_ids:
            return 0
        sql += " AND project_id IN (%s)" % ",".join("?" * len(project_ids))
        args = list(project_ids)
    with get_db() as db:
        n = db.execute(sql, args).rowcount
        db.commit()
    return n


# ─── Lecture ──────────────────────────────────────────────────────────────────
def members_view():
    from .database import db_get_nodes
    nodes = {n["id"]: n.get("name") for n in db_get_nodes() or []}
    load = {}
    for r in _rows("SELECT worker_vmid, COUNT(*) AS n FROM farm_jobs "
                   "WHERE status IN ('transfer_in','running','transfer_out') GROUP BY worker_vmid"):
        load[r["worker_vmid"]] = r["n"]
    out = []
    for t in transcoders():
        m = _member_state.get(t["vmid"]) or {}
        st = m.get("state") or {}
        out.append({"vmid": t["vmid"], "hostname": t["hostname"], "node_id": t["node_id"],
                    "node": nodes.get(t["node_id"]), "project_id": t["project_id"], "member": t["member"],
                    "reachable": bool(m.get("ok")), "error": m.get("error"),
                    "farm_capable": bool(st.get("farm_capable")),
                    "max_concurrent": st.get("max_concurrent"), "running": st.get("jobs_running"),
                    "pending": st.get("jobs_pending"), "usable_cpus": st.get("usable_cpus"),
                    "threads_per_job": st.get("threads_per_job"), "version": st.get("plugin_version"),
                    "farm_jobs": load.get(t["vmid"], 0)})
    return out


_ORDER = ("CASE status WHEN 'running' THEN 0 WHEN 'transfer_out' THEN 1 WHEN 'transfer_in' THEN 2 "
          "WHEN 'pending' THEN 3 ELSE 4 END, "
          "CASE WHEN status='pending' THEN -priority ELSE 0 END, "
          "CASE WHEN status IN ('done','failed','cancelled') THEN -id ELSE id END")


def asset_job_assets():
    """Médias qui ont des jobs de la ferme demandés sur eux (submit_asset)."""
    return [r["src_asset"] for r in _rows("SELECT DISTINCT src_asset FROM farm_jobs WHERE src_asset IS NOT NULL")]


def list_jobs(project_ids=None, status=None, q=None, limit=200, offset=0, extra_assets=()):
    """`extra_assets` : médias dont les jobs (submit_asset) sont aussi visibles — ceux que l'appelant voit
    par un partage."""
    where, args = [], []
    if project_ids is not None:
        extra = list(extra_assets or ())
        if not project_ids and not extra:
            return {"jobs": [], "total": 0, "counts": {}}
        parts = []
        if project_ids:
            parts.append("project_id IN (%s)" % ",".join("?" * len(project_ids)))
            args += list(project_ids)
        if extra:
            parts.append("src_asset IN (%s)" % ",".join("?" * len(extra)))
            args += extra
        where.append("(" + " OR ".join(parts) + ")")
    base_where, base_args = list(where), list(args)
    if status == "active":
        where.append("status IN ('pending','transfer_in','running','transfer_out')")
    elif status == "pending" or status in FINISHED:
        # « En attente » : jobs en file, pas encore confiés (y compris phase `no_transcoder`).
        where.append("status=?")
        args.append(status)
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(src_path LIKE ? ESCAPE '\\' OR out_path LIKE ? ESCAPE '\\')")
        args += ["%" + esc + "%"] * 2
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = _rows("SELECT COUNT(*) AS n FROM farm_jobs" + w, args)[0]["n"]
    jobs = _rows("SELECT * FROM farm_jobs%s ORDER BY %s LIMIT ? OFFSET ?" % (w, _ORDER),
                 args + [int(limit), int(offset)])
    bw = (" WHERE " + " AND ".join(base_where)) if base_where else ""
    counts = {r["status"]: r["n"] for r in _rows("SELECT status, COUNT(*) AS n FROM farm_jobs%s GROUP BY status" % bw,
                                                 base_args)}
    pending = [r["id"] for r in _rows("SELECT id FROM farm_jobs WHERE status='pending' ORDER BY priority DESC, id")]
    pos = {jid: i + 1 for i, jid in enumerate(pending)}
    _decorate(jobs, pos)
    return {"jobs": jobs, "total": total, "counts": counts}


def _decorate(jobs, pos=None):
    from .database import db_get_nodes
    nodes = {n["id"]: n.get("name") for n in db_get_nodes() or []}
    hosts = {t["vmid"]: t["hostname"] for t in transcoders()}
    grants = {}
    try:
        from services import storage
        conns = {c["id"]: c for c in storage._connections()}
        grants = {g["id"]: storage._grant_label(g, conns) for g in storage._grants() if g["conn_id"] in conns}
    except Exception:
        pass
    for j in jobs:
        for k in ("params", "info"):
            try:
                j[k] = json.loads(j[k]) if j.get(k) else {}
            except (ValueError, TypeError):
                j[k] = {}
        j["priority_name"] = PRIORITY_NAMES.get(j["priority"], str(j["priority"]))
        j["src_label"] = (grants.get(j["src_grant_id"], "#%s" % j["src_grant_id"]) if j["src_kind"] == "share"
                          else nodes.get(j["src_node_id"], "#%s" % j["src_node_id"]))
        j["worker"] = hosts.get(j["worker_vmid"]) if j.get("worker_vmid") else None
        j["worker_node"] = nodes.get(j["worker_node_id"]) if j.get("worker_node_id") else None
        j["out_node"] = nodes.get(j["out_node_id"]) if j.get("out_node_id") else None
        j["queue_pos"] = (pos or {}).get(j["id"])
        j.pop("token", None)    # jeton de capacité : jamais vers le navigateur


def job_detail(job_id):
    job = get_job(job_id)
    if not job:
        return None
    _decorate([job])
    if job["status"] == "running" and job.get("worker_vmid") and job.get("worker_job_id"):
        worker = next((t for t in transcoders() if t["vmid"] == job["worker_vmid"]), None)
        if worker:
            try:
                code, j = _call(worker, "GET", "/job", params={"id": job["worker_job_id"]}, timeout=4)
                if code == 200:
                    job["live"] = j.get("live")
                    job["worker_info"] = j.get("info")
                    job["worker_queue_pos"] = j.get("queue_pos")
            except Exception:
                pass
    return job


# ─── Macros ───────────────────────────────────────────────────────────────────
# Fournisseur d'actions du CŒUR (cf. core_plugins.CORE_PROVIDERS) : la ferme se pilote comme un
# service — actions dans les macros, shotbox et scénarios, états dans les conditions et les
# déclencheurs. Une macro de PROJET n'agit que sur les jobs de son projet.
_OPS = [{"value": "video", "label": "Vidéo → Vidéo"}, {"value": "audio", "label": "Audio → Audio"},
        {"value": "extract", "label": "Extraire l'audio"}]
_PRIO_OPTS = [{"value": "urgent", "label": "Urgente"}, {"value": "high", "label": "Haute"},
              {"value": "normal", "label": "Normale"}, {"value": "low", "label": "Basse"}]
_LAYOUT_OPTS = [{"value": "auto", "label": "Selon le preset"}, {"value": "tracks", "label": "Pistes telles quelles"},
                {"value": "mono", "label": "Une piste mono par canal"}, {"value": "stereo", "label": "Programme stéréo"},
                {"value": "poly", "label": "Une piste polyphonique"}, {"value": "none", "label": "Sans audio"}]
_STATUS_OPTS = ["pending", "transfer_in", "running", "transfer_out", "done", "failed", "cancelled"]
_JOB = {"key": "job", "label": "N° de job", "type": "text", "default": "{{farm_job}}"}

MACRO_PROVIDER = {
    "id": "transcode_farm",
    "label": "Ferme de transcodage",
    "actions": [
        {"id": "submit", "label": "Lancer un job", "availability": "project", "params": [
            {"key": "source", "label": "Source", "type": "select", "options_hook": True},
            {"key": "path", "label": "Fichier (chemin dans la source)", "type": "text"},
            {"key": "operation", "label": "Opération", "type": "select", "options": _OPS, "default": "video"},
            {"key": "preset", "label": "Preset", "type": "select", "options_hook": True},
            {"key": "cadence", "label": "Cadence", "type": "select", "options_hook": True, "default": ""},
            {"key": "audio", "label": "Audio", "type": "select", "options": _LAYOUT_OPTS, "default": "auto"},
            {"key": "loudness", "label": "EBU R128", "type": "bool", "default": "false"},
            {"key": "priority", "label": "Priorité", "type": "select", "options": _PRIO_OPTS, "default": "normal"},
            {"key": "output_dir", "label": "Dossier de sortie", "type": "text", "default": ""},
            {"key": "store_in", "label": "N° du job dans la variable", "type": "text", "default": "farm_job"},
            {"key": "project", "label": "Projet (macros système)", "type": "select", "options_hook": True},
        ]},
        {"id": "cancel", "label": "Annuler un job", "availability": "project", "params": [_JOB]},
        {"id": "retry", "label": "Relancer un job", "availability": "project", "params": [_JOB]},
        {"id": "priority", "label": "Changer la priorité d'un job", "availability": "project", "params": [
            _JOB, {"key": "priority", "label": "Priorité", "type": "select", "options": _PRIO_OPTS, "default": "high"}]},
        {"id": "purge", "label": "Purger les jobs terminés", "availability": "project", "params": []},
        {"id": "set_member", "label": "Inscrire / retirer un transcoder", "availability": "system", "params": [
            {"key": "transcoder", "label": "Transcoder", "type": "select", "options_hook": True},
            {"key": "enabled", "label": "Inscrit", "type": "bool", "default": "true"}]},
    ],
    # Bornés au projet de la macro (tous les projets pour une macro système). « Dernier job » =
    # le dernier CRÉÉ : « lancer, puis attendre que le dernier job soit terminé » se lit ainsi.
    "states": [
        {"id": "pending", "label": "Jobs en attente", "type": "number"},
        {"id": "active", "label": "Jobs en cours (transferts compris)", "type": "number"},
        {"id": "failed", "label": "Jobs en échec", "type": "number"},
        {"id": "done", "label": "Jobs terminés", "type": "number"},
        {"id": "last_job_id", "label": "Dernier job : n°", "type": "number"},
        {"id": "last_job_status", "label": "Dernier job : statut", "type": "enum", "options": _STATUS_OPTS},
        {"id": "last_job_output", "label": "Dernier job : sortie", "type": "text"},
        {"id": "members_ready", "label": "Transcoders inscrits joignables", "type": "number"},
    ],
}


def macro_available():
    from . import plugins
    return bool(plugins.get("transcoder"))


_presets_cache = {"t": 0, "d": None}


def _presets():
    """Presets lus chez un transcoder (source unique), gardés une minute : le catalogue des
    macros est relu à chaque ouverture de l'éditeur."""
    if time.time() - _presets_cache["t"] < 60 and _presets_cache["d"] is not None:
        return _presets_cache["d"]
    d = {}
    for t in sorted(transcoders(), key=lambda t: (not t["member"], t["status"] != "running")):
        if t["status"] != "running":
            continue
        try:
            code, r = _call(t, "GET", "/presets", timeout=3)
            if code == 200:
                d = r
                break
        except Exception:
            continue
    _presets_cache.update(t=time.time(), d=d)
    return d


def action_options(action_id, key, pid=None):
    if action_id == "submit" and key == "source":
        out = []
        try:
            from services import storage
            conns = {c["id"]: c for c in storage._connections()}
            for g in storage._grants():
                if g["conn_id"] in conns and storage.container_entry(g["id"], pid):
                    out.append({"value": "share:%d" % g["id"],
                                "label": "Dossier réseau · %s" % storage._grant_label(g, conns)})
            nodes = storage._media_nodes()
        except ImportError:
            from .database import db_get_nodes
            nodes = [n for n in db_get_nodes() or [] if n.get("media_mount")]
        out += [{"value": "local:%d" % n["id"], "label": "Volume du nœud · %s" % n.get("name")} for n in nodes]
        return out
    if action_id == "submit" and key == "preset":
        p = _presets()
        return ([{"value": x["id"], "label": "Vidéo · %s" % x["label"]} for x in p.get("video") or []]
                + [{"value": x["id"], "label": "Audio · %s" % x["label"]} for x in p.get("audio") or []])
    if action_id == "submit" and key == "cadence":
        return [{"value": "", "label": "Comme la source"}] + [{"value": c, "label": c} for c in _presets().get("cadences") or []]
    if action_id == "submit" and key == "project":
        from .database import db_get_projects
        return ([{"value": "", "label": "Projet de la macro / hors projet"}]
                + [{"value": str(p["id"]), "label": p["name"]} for p in db_get_projects() or []])
    if action_id == "set_member" and key == "transcoder":
        return [{"value": str(t["vmid"]), "label": "%s (#%d)" % (t["hostname"], t["vmid"])} for t in transcoders()]
    return []


def _defaults(action_id, params, pid):
    """Paramètres EFFECTIFS = ce que l'éditeur AFFICHE. Il n'enregistre un champ qu'à sa
    modification : un menu laissé tel quel montre sa première option sans l'avoir stockée, et
    un défaut (« {{farm_job}} ») n'a pas été passé dans les variables. Sans ce complément, la
    macro ferait autre chose que ce qu'on lit à l'écran."""
    from .macros import _tmpl
    from .database import db_project_vars
    act = next((a for a in MACRO_PROVIDER["actions"] if a["id"] == action_id), None)
    if not act:
        raise RuntimeError("action inconnue : %s" % action_id)
    variables = db_project_vars(pid) if pid is not None else {}
    out = dict(params or {})
    for p in act["params"]:
        k = p["key"]
        if k in out and out[k] is not None:
            continue
        if p.get("default") is not None:
            out[k] = _tmpl(p["default"], variables)
        else:
            opts = p.get("options") or (action_options(action_id, k, pid) if p.get("options_hook") else [])
            if opts:
                out[k] = opts[0]["value"]
    return out


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "oui", "on")


def _macro_job(params, pid):
    """Le job visé par une macro : son numéro (souvent « {{farm_job}} », déjà résolu), et
    seulement dans le projet de la macro."""
    try:
        jid = int(str(params.get("job") or "").strip())
    except ValueError:
        raise RuntimeError("n° de job invalide : « %s »" % params.get("job"))
    job = get_job(jid)
    if not job or (pid is not None and job["project_id"] != pid):
        raise RuntimeError("job n° %d introuvable dans ce projet" % jid)
    return jid


def run_action(action_id, params, ctx):
    ensure_tables()
    pid = (ctx or {}).get("project_id")
    p = _defaults(action_id, params, pid)
    if action_id == "submit":
        if pid is None and str(p.get("project") or "").strip():
            pid = int(p["project"])
        kind, _, ref = str(p.get("source") or "").partition(":")
        if kind not in ("share", "local") or not ref.isdigit():
            raise RuntimeError("source à choisir (dossier réseau ou volume d'un nœud)")
        op = p.get("operation") or "video"
        enc = {"audio": p.get("audio") or "auto"}
        if p.get("preset"):
            enc["preset" if op == "video" else "audio_preset"] = p["preset"]
        if op == "video" and p.get("cadence"):
            enc["cadence"] = p["cadence"]
        if _truthy(p.get("loudness")):
            enc["loudness"] = True
        source = {"kind": kind, "path": p.get("path") or "",
                  ("grant_id" if kind == "share" else "node_id"): int(ref)}
        try:
            jid = submit(pid, source, op, enc, p.get("priority"), p.get("output_dir") or "",
                         {"username": (ctx or {}).get("user") or "macro"})
        except ValueError as e:
            raise RuntimeError(str(e))
        var = str(p.get("store_in") or "").strip()
        if var and pid is not None:
            from .database import db_set_project_var
            db_set_project_var(pid, var, str(jid))
        return {"id": jid}
    if action_id == "cancel":
        cancel(_macro_job(p, pid))
    elif action_id == "retry":
        retry(_macro_job(p, pid))
    elif action_id == "priority":
        set_priority(_macro_job(p, pid), p.get("priority"))
    elif action_id == "purge":
        return {"removed": purge([pid] if pid is not None else None)}
    elif action_id == "set_member":
        set_member(int(p.get("transcoder") or 0), _truthy(p.get("enabled")))
    else:
        raise RuntimeError("action inconnue : %s" % action_id)
    return {"ok": True}


def read_state(state_id, ctx):
    ensure_tables()
    pid = (ctx or {}).get("project_id")
    where, args = ("WHERE project_id=?", [pid]) if pid is not None else ("", [])
    if state_id in ("pending", "failed", "done"):
        return _rows("SELECT COUNT(*) AS n FROM farm_jobs %s%s status=?" % (where, " AND" if where else "WHERE"),
                     args + [state_id])[0]["n"]
    if state_id == "active":
        return _rows("SELECT COUNT(*) AS n FROM farm_jobs %s%s status IN ('transfer_in','running','transfer_out')"
                     % (where, " AND" if where else "WHERE"), args)[0]["n"]
    if state_id.startswith("last_job_"):
        r = _rows("SELECT id, status, out_path FROM farm_jobs %s ORDER BY id DESC LIMIT 1" % where, args)
        if not r:
            return None
        return {"last_job_id": r[0]["id"], "last_job_status": r[0]["status"],
                "last_job_output": r[0]["out_path"]}.get(state_id)
    if state_id == "members_ready":
        return sum(1 for m in members_view() if m["member"] and m["reachable"] and m["farm_capable"]
                   and (pid is None or m["project_id"] == pid))
    return None
