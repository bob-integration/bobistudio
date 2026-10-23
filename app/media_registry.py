# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
"""REGISTRE DES MÉDIAS — l'identité d'un média, indépendante de l'endroit où il est stocké.

Chantier : docs/chantiers/MEDIAS_REGISTRE.md. Résumé :

- Un MÉDIA (`media_assets`) a un identifiant UUID, des métadonnées (fiche technique, champs de
  base et personnalisés, vignette choisie) et une empreinte de contenu. Il survit au déplacement,
  à la copie, au changement de serveur.
- Un FICHIER (`media_files`) est ce que l'analyse a vu sur le volume d'un nœud (`node_id`), ou
  la source d'un import depuis un partage réseau (`share_id`, `node_id` = 0 : le partage n'est
  jamais indexé en entier, seules ses copies importées y sont suivies — ce qui permet, en y
  naviguant, de voir ce qui est déjà dans la base). `asset_id` renseigné = une copie du média ;
  NULL = fichier NON RÉFÉRENCÉ. Rien n'entre dans la base sans
  geste explicite (import) — les partages réseau ne sont jamais indexés en entier.
- Un média a des VERSIONS (règle du 2026-10-16) : l'ORIGINAL (la référence : empreinte, fiche
  technique et taille de `media_assets`) et ses autres essences — transcodage de diffusion, MXF de
  livraison… (`media_versions`, clé = nom court du préréglage : `h264`, `xdcam`). Chaque copie
  (`media_files.version`) est celle d'UNE version ; une sortie du transcodeur est rangée sous le
  média de sa source, jamais en fiche à part. Champs, bins, partages et projets sont ceux de la
  FICHE (toutes versions). Charger un média = sa version PAR DÉFAUT (`default_version`, l'original
  sauf choix), ou une version précise. L'empreinte qui identifie un contenu est celle de la
  version : un fichier identique à une version est une copie de plus de CETTE version.
- Un média se reconnaît après un déplacement par son identité écrite dans le sidecar `.xml`
  (qui voyage avec le fichier), et à défaut par son empreinte (taille + 4 premiers et 4 derniers
  Mo).

Base DÉDIÉE (`db_medias.db`, à côté de la base principale) : sauvegarde, export et import
propres, et une croissance qui ne pèse pas sur la base principale. Contrepartie assumée : pas de
transaction commune avec les projets — les références croisées sont vérifiées par `integrity()`.

L'analyse d'un volume tourne SUR LE NŒUD, dans un conteneur jetable de l'image média (python3,
ffprobe, ffmpeg) : `script_templates/media_scan.py`.
"""
import io
import json
import logging
import os
import re
import shlex
import sqlite3
import tarfile
import threading
import time
import uuid

from . import media_naming
from .config import DB_PATH
from .media_naming import ORIGINAL

log = logging.getLogger(__name__)

MEDIA_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "db_medias.db")
THUMB_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "media_thumbs")
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
THUMB_W = 480
SCHEMA_VERSION = 1
FIELD_TYPES = ("text", "longtext", "number", "date", "enum", "bool")
BASE_FIELDS = ("title", "description", "keywords")
_DEFAULT_MOUNT = "/srv/mxl-media"
_SCAN_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "script_templates", "media_scan.py")

# Connexion PAR THREAD et PAR CHEMIN : la clé est le chemin, pas seulement le thread — sinon un
# test qui change MEDIA_DB_PATH après un premier accès continuerait d'écrire dans la vraie base
# (piège déjà rencontré avec database.get_db).
_tls = threading.local()
_scan_lock = threading.Lock()


def db():
    path = MEDIA_DB_PATH
    conns = getattr(_tls, "conns", None)
    if conns is None:
        conns = _tls.conns = {}
    c = conns.get(path)
    if c is None:
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _init(c)
        conns[path] = c
    return c


def _init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS media_meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS media_assets (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        origin        TEXT,
        fingerprint   TEXT,
        size          INTEGER,
        tech_json     TEXT,
        meta_json     TEXT,
        thumb_t       REAL,
        thumb_custom  INTEGER DEFAULT 0,
        thumb_v       TEXT
    );
    CREATE INDEX IF NOT EXISTS media_assets_fp ON media_assets(fingerprint);
    CREATE TABLE IF NOT EXISTS media_files (
        node_id     INTEGER NOT NULL DEFAULT 0,
        share_id    INTEGER NOT NULL DEFAULT 0,
        path        TEXT NOT NULL,
        size        INTEGER,
        mtime       INTEGER,
        fingerprint TEXT,
        tech_json   TEXT,
        sidecar_id  TEXT,
        asset_id    TEXT REFERENCES media_assets(id) ON DELETE SET NULL,
        kind        TEXT DEFAULT 'main',
        state       TEXT DEFAULT 'present',
        growing     INTEGER DEFAULT 0,
        seen_at     TEXT,
        verified_at TEXT,
        PRIMARY KEY (node_id, share_id, path)
    );
    CREATE INDEX IF NOT EXISTS media_files_asset ON media_files(asset_id);
    CREATE INDEX IF NOT EXISTS media_files_fp ON media_files(fingerprint);
    CREATE TABLE IF NOT EXISTS media_fields (
        key       TEXT PRIMARY KEY,
        label     TEXT NOT NULL,
        type      TEXT NOT NULL,
        options   TEXT,
        position  INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS media_bins (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        parent_id  INTEGER REFERENCES media_bins(id) ON DELETE CASCADE,
        project_id INTEGER,
        query_json TEXT,
        position   INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS media_bin_items (
        bin_id   INTEGER NOT NULL REFERENCES media_bins(id) ON DELETE CASCADE,
        asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
        position INTEGER DEFAULT 0,
        added_at TEXT,
        PRIMARY KEY (bin_id, asset_id)
    );
    CREATE TABLE IF NOT EXISTS media_versions (
        asset_id     TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
        key          TEXT NOT NULL,
        label        TEXT,
        preset       TEXT,
        origin       TEXT,
        producer     TEXT,
        fingerprint  TEXT,
        size         INTEGER,
        tech_json    TEXT,
        process_json TEXT,
        created_at   TEXT,
        updated_at   TEXT,
        PRIMARY KEY (asset_id, key)
    );
    CREATE INDEX IF NOT EXISTS media_versions_fp ON media_versions(fingerprint);
    CREATE TABLE IF NOT EXISTS project_media (
        project_id INTEGER NOT NULL,
        asset_id   TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
        added_at   TEXT,
        PRIMARY KEY (project_id, asset_id)
    );
    """)
    # Colonnes ajoutées après la création de la base : migration idempotente.
    cols = {r[1] for r in c.execute("PRAGMA table_info(media_assets)")}
    if "producer" not in cols:
        c.execute("ALTER TABLE media_assets ADD COLUMN producer TEXT")      # conteneur qui l'a produit
    for col, typ in (("derived_from", "TEXT"), ("relation", "TEXT"), ("process_json", "TEXT"),
                     ("default_version", "TEXT")):
        if col not in cols:
            c.execute("ALTER TABLE media_assets ADD COLUMN %s %s" % (col, typ))   # filiation, traitement, version
    cols = {r[1] for r in c.execute("PRAGMA table_info(media_files)")}
    for col in ("producer", "derived_from", "process_json", "origin"):
        if col not in cols:
            c.execute("ALTER TABLE media_files ADD COLUMN %s TEXT" % col)
    if "version" not in cols:            # version du média que porte la copie (cf. media_versions)
        c.execute("ALTER TABLE media_files ADD COLUMN version TEXT NOT NULL DEFAULT 'original'")
    # `kind` : 'main' (copie du média) ou 'proxy' (proxy de lecture). Toute requête qui compte des
    # copies filtre `kind='main'` : une ligne NULL passerait entre les deux.
    c.execute("UPDATE media_files SET kind='main' WHERE kind IS NULL")
    c.execute("INSERT OR IGNORE INTO media_meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
    c.commit()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _j(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


# ─── Nœuds ──────────────────────────────────────────────────────────────────────────────────
def _node(node_id):
    from .database import db_get_node
    return db_get_node(int(node_id))


def _mount(node):
    return (node.get("media_mount") or "").strip() or _DEFAULT_MOUNT


def _run_on_node(node, req, timeout=900, extra_mounts=()):
    """Lance script_templates/media_scan.py sur le nœud, dans un conteneur jetable de l'image
    média, le volume monté en /m. Renvoie le JSON de sortie, ou lève RuntimeError."""
    from . import node_driver
    image = (node.get("media_image") or "").strip()
    if not image:
        raise RuntimeError("nœud %s : aucune image média" % node.get("name"))
    with open(_SCAN_SCRIPT, "r", encoding="utf-8") as f:
        script = f.read()
    mounts = "".join(" -v %s:%s:ro" % (shlex.quote(h), shlex.quote(c)) for h, c in extra_mounts)
    cmd = ("docker run --rm -i --network none -v %s:/m%s --entrypoint python3 %s -c %s"
           % (shlex.quote(_mount(node)), mounts, shlex.quote(image), shlex.quote(script)))
    rc, out, err = node_driver.host_exec(node, cmd, input_data=json.dumps(req), timeout=timeout)
    if rc != 0:
        raise RuntimeError("nœud %s : analyse en échec (rc=%s) %s" % (node.get("name"), rc, (err or "")[-400:]))
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError("nœud %s : sortie illisible %r" % (node.get("name"), (out or "")[:200]))


# ─── Analyse et réconciliation ──────────────────────────────────────────────────────────────
def scan_node(node_id, probe_max=200):
    """Relevé du volume d'un nœud + réconciliation avec la base. Renvoie un résumé.

    - fichier déjà connu, inchangé : vu ;
    - fichier nouveau ou modifié : rattaché à un média par l'identité de son sidecar, sinon par
      son empreinte ; sinon NON RÉFÉRENCÉ (asset_id NULL) ;
    - copie d'un média absente du volume : `missing` (le média et ses métadonnées restent) ;
      fichier non référencé disparu : oublié."""
    node = _node(node_id)
    if not node:
        raise RuntimeError("nœud %s introuvable" % node_id)
    c = db()
    known = {r["path"]: [r["size"], r["mtime"], r["fingerprint"], bool(r["tech_json"])]
             for r in c.execute("SELECT * FROM media_files WHERE node_id=? AND share_id=0 AND kind='main'", (node_id,))}
    # PROXYS : rangés dans des dossiers cachés que le relevé ne parcourt pas — ils sont vérifiés par
    # LEUR chemin (existence, taille), jamais par la réconciliation des fichiers principaux (qui les
    # aurait marqués absents, ou oubliés).
    proxies = [r["path"] for r in c.execute("SELECT path FROM media_files WHERE node_id=? AND share_id=0 AND kind='proxy'",
                                            (node_id,))]
    with _scan_lock:
        res = _run_on_node(node, {"mode": "scan", "known": known, "probe_max": probe_max, "proxies": proxies})
    now = _now()
    seen, summary = set(), {"node": node.get("name"), "files": 0, "attached": 0, "unreferenced": 0,
                            "missing": 0, "probed": res.get("probed", 0)}
    assets = {r["id"] for r in c.execute("SELECT id FROM media_assets")}
    closed = []                  # copies vidéo qui viennent de se FERMER (fin d'enregistrement) : proxy auto
    for f in res.get("files") or []:
        seen.add(f["path"])
        summary["files"] += 1
        row = c.execute("SELECT * FROM media_files WHERE node_id=? AND share_id=0 AND path=?", (node_id, f["path"])).fetchone()
        aid = row["asset_id"] if row else None
        version = row["version"] if row and aid else ORIGINAL
        if aid and row["growing"] and not f.get("growing"):
            closed.append(aid)
        if aid is None:
            # Rattachement : l'identité (et la version) du sidecar, sinon l'empreinte — celle de
            # l'original d'un média OU d'une de ses versions (find_by_fingerprint).
            if f.get("id") in assets:
                aid, version = f["id"], f.get("version") or ORIGINAL
            elif f.get("fp"):
                aid, version = find_by_fingerprint(f["fp"], c)
                version = version or ORIGINAL
            if aid:
                summary["attached"] += 1
        tech = json.dumps(f["tech"]) if f.get("tech") else (row["tech_json"] if row else None)
        c.execute("""INSERT INTO media_files(node_id, path, size, mtime, fingerprint, tech_json, sidecar_id,
                        asset_id, state, growing, seen_at, producer, derived_from, process_json, origin, version)
                     VALUES (?,?,?,?,?,?,?,?,'present',?,?,?,?,?,?,?)
                     ON CONFLICT(node_id, share_id, path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
                        fingerprint=COALESCE(excluded.fingerprint, fingerprint), tech_json=excluded.tech_json,
                        sidecar_id=excluded.sidecar_id, asset_id=excluded.asset_id, state='present',
                        growing=excluded.growing, seen_at=excluded.seen_at, producer=excluded.producer,
                        derived_from=excluded.derived_from, process_json=excluded.process_json, origin=excluded.origin,
                        version=excluded.version""",
                  (node_id, f["path"], f["size"], f["mtime"], f.get("fp"), tech, f.get("id"), aid,
                   1 if f.get("growing") else 0, now, f.get("producer"), f.get("derived_from"),
                   json.dumps(f["process"], ensure_ascii=False) if f.get("process") else None, f.get("origin"),
                   version if aid else ORIGINAL))
        if aid is None:
            summary["unreferenced"] += 1
        elif version != ORIGINAL:
            # Copie d'une VERSION : elle existe dans la fiche dès sa première copie vue (sortie du
            # transcodeur rangée sous sa source). Refaite à l'identique par son producteur (même
            # nom, contenu changé) : la version prend le nouveau contenu.
            changed = bool(row) and f.get("fp") and row["fingerprint"] and f["fp"] != row["fingerprint"]
            _ensure_version(aid, version, f.get("process"), origin=f.get("origin"), producer=f.get("producer"),
                            fingerprint=f.get("fp"), size=f["size"] if f.get("fp") else None, tech=f.get("tech"),
                            replace=bool(changed and f.get("process")))
            ppid = _producer_project(f.get("producer")) if f.get("producer") else None
            if ppid:
                c.execute("INSERT OR IGNORE INTO project_media(project_id, asset_id, added_at) VALUES (?,?,?)", (ppid, aid, now))
        else:
            if f.get("tech"):
                c.execute("UPDATE media_assets SET tech_json=?, updated_at=? WHERE id=? AND tech_json IS NULL",
                          (tech, now, aid))
            # Fichier enregistré pendant qu'il s'écrivait (recorder) : l'empreinte n'existait pas
            # encore, elle est posée à la première analyse après la fermeture.
            if f.get("fp"):
                c.execute("UPDATE media_assets SET fingerprint=?, size=? WHERE id=? AND fingerprint IS NULL",
                          (f["fp"], f["size"], aid))
    # IMPORT AUTOMATIQUE des fichiers d'un PRODUCTEUR du système : le recorder écrit lui-même
    # l'identité et `origin="recorder"` dans son sidecar (il le réécrit chaque seconde pendant
    # l'enregistrement — une identité tamponnée par la base y serait effacée). Enregistrer est un
    # geste explicite ; ces fichiers-là en sont un. Exclus : les tampons de boucle non sauvegardés
    # (`<loop>` hors dossier `boucle_…`), que la rétention efface d'elle-même.
    auto = [f for f in res.get("files") or [] if f.get("origin") in AUTO_ORIGINS and f.get("id")
            and f["id"] not in assets and not (f.get("loop") and "/boucle_" not in "/" + f["path"])]
    if auto:
        c.commit()
        for org in AUTO_ORIGINS:
            ps = [f["path"] for f in auto if f.get("origin") == org]
            if ps:
                import_files(node_id, ps, origin=org)
        summary["auto_imported"] = len(auto)
    for r in c.execute("SELECT path, asset_id FROM media_files WHERE node_id=? AND share_id=0 AND kind='main'", (node_id,)).fetchall():
        if r["path"] in seen:
            continue
        if r["asset_id"]:
            c.execute("UPDATE media_files SET state='missing' WHERE node_id=? AND path=?", (node_id, r["path"]))
            summary["missing"] += 1
        else:
            c.execute("DELETE FROM media_files WHERE node_id=? AND path=?", (node_id, r["path"]))
    # DÉPLACEMENT sur ce volume : une copie devenue absente dont le média a, sur ce même nœud, une
    # copie présente de même empreinte (ou portant son identité) a été déplacée, pas perdue.
    for r in c.execute("""SELECT m.path FROM media_files m WHERE m.node_id=? AND m.state='missing' AND m.kind='main'
                            AND EXISTS (SELECT 1 FROM media_files p WHERE p.node_id=m.node_id AND p.asset_id=m.asset_id
                                        AND p.state='present' AND p.path != m.path AND p.kind='main' AND p.version=m.version
                                        AND (p.fingerprint = m.fingerprint OR p.sidecar_id = m.asset_id))""",
                       (node_id,)).fetchall():
        c.execute("DELETE FROM media_files WHERE node_id=? AND path=?", (node_id, r["path"]))
        summary["missing"] -= 1
        summary["moved"] = summary.get("moved", 0) + 1
    _reconcile_proxies(node_id, res.get("proxies"), summary)
    c.commit()
    # Une copie rattachée par empreinte n'a pas encore l'identité dans son sidecar : on l'y écrit.
    _stamp_pending(node_id)
    for aid in closed:
        proxy_auto(aid, node_id)
    return summary


def scan_all(probe_max=200):
    from .database import db_get_nodes
    out = []
    for n in db_get_nodes():
        if not (n.get("media_image") or "").strip():
            continue
        try:
            out.append(scan_node(n["id"], probe_max=probe_max))
        except Exception as e:                                             # noqa: BLE001
            out.append({"node": n.get("name"), "error": str(e)})
    return out


SCAN_EVERY_S = 120
# Producteurs du système dont les fichiers entrent d'eux-mêmes dans la base : ils écrivent leur
# identité (et `origin`) dans le sidecar. Le transcodeur y ajoute la filiation et tout son traitement ;
# le cartouchier (0.1.3, « Enregistrer comme nouveau média » de l'éditeur de clip) la filiation
# (`derived_from`) — un geste explicite de l'exploitant, comme un enregistrement.
AUTO_ORIGINS = ("recorder", "transcoder", "cartouchier")
_periodic_started = False


def start_periodic_scan():
    """Analyse légère de tous les volumes toutes les SCAN_EVERY_S : les clips du recorder entrent
    dans la base sans geste, et une copie disparue est signalée. Un conteneur jetable par nœud
    média, quelques secondes chacun ; l'empreinte n'est recalculée que pour ce qui a changé."""
    global _periodic_started
    if _periodic_started:
        return
    _periodic_started = True

    def run():
        time.sleep(30)
        try:
            migrate_legacy_bins()        # reprise des anciens bins, puis retrait de leurs tables
        except Exception as e:                                             # noqa: BLE001
            log.warning("registre médias : reprise des anciens bins : %s", e)
        while True:
            try:
                scan_all(probe_max=50)
            except Exception as e:                                         # noqa: BLE001
                log.warning("registre médias : analyse périodique : %s", e)
            try:                         # planches manquantes (si `media_proxy_auto`), basse priorité
                from . import media_storyboard as _sbm
                _sbm.auto_tick()
            except Exception as e:                                         # noqa: BLE001
                log.warning("registre médias : planches automatiques : %s", e)
            time.sleep(SCAN_EVERY_S)
    threading.Thread(target=run, daemon=True, name="media-scan").start()


def _stamp_pending(node_id, thumbs=()):
    """Écrit l'identité dans le sidecar des copies qui ne la portent pas encore, et calcule les
    vignettes demandées (liste de (asset_id, path, t))."""
    c = db()
    rows = c.execute("""SELECT f.path, f.asset_id, f.version, f.sidecar_id, COALESCE(f.tech_json, a.tech_json) AS tech,
                               (f.sidecar_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM media_assets o WHERE o.id=f.sidecar_id))
                               AS orphan
                        FROM media_files f JOIN media_assets a ON a.id=f.asset_id
                        WHERE f.node_id=? AND f.state='present' AND f.growing=0 AND f.share_id=0 AND f.kind='main'
                          AND (f.sidecar_id IS NULL OR f.sidecar_id != f.asset_id)""", (node_id,)).fetchall()
    if not rows and not thumbs:
        return {}
    node = _node(node_id)
    # Une identité de sidecar qui ne désigne AUCUN média (sortie d'un transcodage dont la source
    # était inconnue, rangée depuis sous le média de cette source) est remplacée ; celle d'un autre
    # média connu est gardée (conflit journalisé).
    req = {"mode": "stamp",
           "stamp": [{"path": r["path"], "id": r["asset_id"], "tech": _j(r["tech"], None),
                      "version": r["version"] if r["version"] != ORIGINAL else None, "replace": bool(r["orphan"])}
                     for r in rows],
           "thumbs": [{"id": a, "path": p, "t": t, "w": THUMB_W} for (a, p, t) in thumbs]}
    with _scan_lock:
        res = _run_on_node(node, req)
    for path, st in (res.get("stamped") or {}).items():
        if st in ("created", "stamped", "kept"):
            r = c.execute("SELECT asset_id FROM media_files WHERE node_id=? AND path=?", (node_id, path)).fetchone()
            if r:
                c.execute("UPDATE media_files SET sidecar_id=? WHERE node_id=? AND path=?", (r["asset_id"], node_id, path))
        else:
            log.warning("registre médias : sidecar %s (nœud %s) : %s", path, node_id, st)
    c.commit()
    for aid, b64 in (res.get("thumbs") or {}).items():
        if b64:
            _thumb_store(aid, b64)
    return res


# ─── Import (le geste explicite qui fait entrer un fichier dans la base) ────────────────────
def import_files(node_id, paths, origin="import", project_id=None, want_ids=None):
    """Fait entrer des fichiers NON RÉFÉRENCÉS d'un nœud dans la base. L'identité déjà écrite
    dans un sidecar (fichier venant d'une autre installation, ou d'un projet importé) est reprise
    telle quelle : c'est elle qui permet de retrouver ses métadonnées. `want_ids` {chemin: identité} :
    identité RÉSERVÉE avant le nom de fichier (qui porte son code, cf. app/media_naming.py) — envoi
    depuis le poste, copie depuis un partage. Le NOM du média est celui du fichier SANS le code.
    Renvoie les ids."""
    c = db()
    now, ids, thumbs, closed = _now(), [], [], []
    for p in paths:
        r = c.execute("SELECT * FROM media_files WHERE node_id=? AND share_id=0 AND kind='main' AND path=?", (node_id, p)).fetchone()
        if not r:
            continue
        if r["asset_id"]:
            ids.append(r["asset_id"])
            continue
        aid = r["sidecar_id"] or (want_ids or {}).get(p) or str(uuid.uuid4())
        version = r["version"] or ORIGINAL
        tech = _j(r["tech_json"], {})
        dur = (tech or {}).get("duration") or 0
        proc = _j(r["process_json"], None)
        if (proc or {}).get("version") and media_naming.valid_version(proc["version"]):
            version = proc["version"]
        if version != ORIGINAL and not c.execute("SELECT 1 FROM media_assets WHERE id=?", (aid,)).fetchone():
            # VERSION d'un média que la base ne connaît pas sous cette identité : sortie d'un
            # transcodage dont la source n'avait pas d'identité (dossier de dépôt, partage). Sa
            # source est retrouvée par l'EMPREINTE du fichier d'entrée (même méthode que la base) :
            # la sortie rejoint SON média. Inconnue : la fiche naît de la version, avec l'empreinte et
            # la fiche technique de la source — la source importée plus tard en sera l'original.
            src_fp = ((proc or {}).get("source") or {}).get("fingerprint")
            found, _v = find_by_fingerprint(src_fp, c) if src_fp else (None, None)
            if found:
                aid = found
            else:
                srcp = (proc or {}).get("source") or {}
                stech = {k: srcp.get(k) for k in ("video", "audio", "duration") if srcp.get(k) is not None} or None
                c.execute("""INSERT INTO media_assets(id, name, created_at, updated_at, origin, fingerprint, size,
                                tech_json, meta_json, thumb_t, producer)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                          (aid, media_naming.media_name(os.path.basename(srcp.get("path") or p)), now, now,
                           r["origin"] or origin, src_fp, srcp.get("size"), json.dumps(stech) if stech else None,
                           json.dumps((proc or {}).get("meta") or {}, ensure_ascii=False),
                           min(5.0, dur * 0.1) if dur else 1.0, r["producer"]))
        if not c.execute("SELECT 1 FROM media_assets WHERE id=?", (aid,)).fetchone():
            # Média DÉRIVÉ (variante de l'éditeur d'image : un autre contenu, une autre fiche) : il
            # hérite des métadonnées de son original, puis de celles posées par son producteur.
            src = r["derived_from"] if r["derived_from"] and c.execute(
                "SELECT 1 FROM media_assets WHERE id=?", (r["derived_from"],)).fetchone() else None
            meta = _j(c.execute("SELECT meta_json FROM media_assets WHERE id=?", (src,)).fetchone()["meta_json"], {}) if src else {}
            meta.update((proc or {}).get("meta") or {})
            c.execute("""INSERT INTO media_assets(id, name, created_at, updated_at, origin, fingerprint, size,
                            tech_json, meta_json, thumb_t, producer, derived_from, relation, process_json)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (aid, media_naming.media_name(p, aid), now, now, r["origin"] or origin, r["fingerprint"], r["size"],
                       r["tech_json"], json.dumps(meta, ensure_ascii=False), min(5.0, dur * 0.1) if dur else 1.0,
                       r["producer"], src, None, r["process_json"]))
        if version != ORIGINAL:
            _ensure_version(aid, version, proc, origin=r["origin"] or origin, producer=r["producer"],
                            fingerprint=r["fingerprint"], size=r["size"], tech=tech or None)
        # Projet du PRODUCTEUR (le transcodeur, un recorder rattaché à un projet) : le média y entre.
        ppid = _producer_project(r["producer"])
        if ppid and not project_id:
            c.execute("INSERT OR IGNORE INTO project_media(project_id, asset_id, added_at) VALUES (?,?,?)", (ppid, aid, now))
        c.execute("UPDATE media_files SET asset_id=?, version=? WHERE node_id=? AND path=?", (aid, version, node_id, p))
        if project_id:
            c.execute("INSERT OR IGNORE INTO project_media(project_id, asset_id, added_at) VALUES (?,?,?)",
                      (int(project_id), aid, now))
        # Vignette et proxy viennent de l'ORIGINAL ; une version seule (source encore inconnue) les
        # fournit en attendant.
        orig_present = version == ORIGINAL or not c.execute(
            "SELECT 1 FROM media_files WHERE asset_id=? AND kind='main' AND version=? AND state='present'",
            (aid, ORIGINAL)).fetchone()
        if orig_present and not r["growing"] and ((tech or {}).get("video") or not tech):
            thumbs.append((aid, p, min(5.0, dur * 0.1) if dur else 1.0))
        if orig_present and not r["growing"]:
            closed.append(aid)
        ids.append(aid)
    c.commit()
    if ids:
        # Les fichiers d'un producteur portent DÉJÀ leur identité (sidecar_id = asset_id) : rien
        # à tamponner pour eux, seulement les vignettes.
        _stamp_pending(node_id, thumbs=thumbs)
    for aid in closed:
        proxy_auto(aid, node_id)
    return ids


def _producer_project(hostname):
    """Projet du conteneur `hostname` (producteur d'un média), ou None."""
    if not hostname:
        return None
    from .database import db_get_containers
    for ct in db_get_containers():
        if ct.get("hostname") == hostname:
            return ct.get("project_id")
    return None


def import_all_unreferenced(node_id=None, project_id=None):
    """Reprise de l'existant : importe tous les fichiers non référencés (d'un nœud, ou de tous)."""
    c = db()
    q = "SELECT node_id, path FROM media_files WHERE asset_id IS NULL AND state='present' AND share_id=0 AND kind='main'"
    args = ()
    if node_id is not None:
        q += " AND node_id=?"
        args = (int(node_id),)
    by_node = {}
    for r in c.execute(q, args):
        by_node.setdefault(r["node_id"], []).append(r["path"])
    n = 0
    for nid, paths in by_node.items():
        for i in range(0, len(paths), 100):        # par lots : un lot = un conteneur sur le nœud
            n += len(import_files(nid, paths[i:i + 100], origin="import", project_id=project_id))
    return n


# ─── Partages réseau : copies suivies, sans indexation ──────────────────────────────────────
def record_share_copy(aid, share_id, path, size=None, mtime=None, fingerprint=None, version=ORIGINAL):
    """Enregistre le fichier SOURCE d'un import depuis un partage comme copie du média (de sa
    version `version`)."""
    c = db()
    c.execute("""INSERT INTO media_files(node_id, share_id, path, size, mtime, fingerprint, asset_id, state, seen_at, version)
                 VALUES (0,?,?,?,?,?,?,'present',?,?)
                 ON CONFLICT(node_id, share_id, path) DO UPDATE SET asset_id=excluded.asset_id, size=excluded.size,
                    mtime=excluded.mtime, fingerprint=COALESCE(excluded.fingerprint, fingerprint),
                    state='present', seen_at=excluded.seen_at, version=excluded.version""",
              (int(share_id), path, size, mtime, fingerprint, aid, _now(), version or ORIGINAL))
    c.commit()


def share_status(share_id, paths):
    """Pour la navigation dans un partage : {chemin: id du média} des fichiers déjà importés."""
    if not paths:
        return {}
    c = db()
    out = {}
    for i in range(0, len(paths), 500):
        part = paths[i:i + 500]
        for r in c.execute("SELECT path, asset_id FROM media_files WHERE share_id=? AND asset_id IS NOT NULL AND path IN (%s)"
                           % ",".join("?" * len(part)), [int(share_id)] + list(part)):
            out[r["path"]] = r["asset_id"]
    return out


# ─── Vue d'un conteneur sur la base ─────────────────────────────────────────────────────────
def container_view(vmid):
    """(node_id, préfixe) : ce que voit le conteneur `vmid` sous /mnt/media. Le volume du nœud
    entier, ou le sous-dossier de son projet (même règle que docker_compute au montage)."""
    from .database import db_get_container, db_get_project
    c = db_get_container(int(vmid)) or {}
    prefix = ""
    if c.get("project_id"):
        proj = db_get_project(c["project_id"]) or {}
        if proj.get("media_path"):
            prefix = os.path.basename(proj["media_path"].rstrip("/")) + "/"
    return c.get("node_id"), prefix


def local_path(copies, node_id, prefix="", version=None):
    """Chemin, relatif à la racine média du conteneur, d'une copie présente sur SON nœud et dans
    SON champ de vision (préfixe du projet) — None sinon. `version` : une copie de CETTE version
    seulement (l'appelant passe la version résolue : par défaut, ou celle demandée)."""
    for c in copies or []:
        if version is not None and c.get("version", ORIGINAL) != version:
            continue
        if c.get("node_id") == node_id and c.get("state") == "present" and not c.get("share_id") \
                and c["path"].startswith(prefix):
            return c["path"][len(prefix):]
    return None


def _scope_of(vmid):
    """Portée réseau : celle du projet du conteneur `vmid`, ou la portée globale (vmid None —
    Gestionnaire de médias : les dossiers accordés à tous)."""
    from services import storage
    from .database import db_get_container
    c = db_get_container(int(vmid)) if vmid is not None else None
    return storage._scope_for_project((c or {}).get("project_id"))


def _ext_resolve(vmid, ext_rel):
    """Chemin vu par un conteneur sous /mnt/ext (« <entrée>/<sous-chemin> ») → (conn_id, chemin
    dans la connexion, entrée, sous-chemin). Même table de vues que le montage (service storage).
    `vmid` None : la portée globale."""
    from services import storage
    scope = _scope_of(vmid)
    rel = storage._clean_rel(ext_rel)
    entry, _, sub = rel.partition("/")
    src = (storage._views().get(scope) or {}).get(entry)
    if not src or not sub:
        raise ValueError("chemin réseau inconnu : %s" % ext_rel)
    conn_id, _, base = src.partition("/")
    return int(conn_id), (base + "/" + sub if base else sub), entry, sub


def import_from_share(vmid, ext_rel, node_id=None):
    """Import d'un fichier de partage réseau POUR un conteneur (player…), le geste « Charger »
    d'un serveur réseau : vérifie à la source, copie sur le volume du nœud du conteneur, enregistre
    le média (la source restant suivie comme copie « partage »). Déjà importé et présent pour ce
    conteneur : rien n'est recopié. Renvoie {asset_id, local_path, copied}."""
    from services import storage
    # Pour un conteneur (son nœud, le dossier de son projet), ou vers un NŒUD choisi
    # (Gestionnaire : vmid None, portée globale, racine du volume).
    if vmid is not None:
        node_id, prefix = container_view(vmid)
    else:
        prefix = ""
    node = _node(node_id)
    conn_id, share_rel, entry, sub = _ext_resolve(vmid, ext_rel)
    c = db()
    # Déjà dans la base ?
    known = c.execute("SELECT asset_id, version FROM media_files WHERE share_id=? AND path=? AND asset_id IS NOT NULL",
                      (conn_id, share_rel)).fetchone()
    if known:
        lp = local_path(_files_of(known["asset_id"]), node_id, prefix, version=known["version"])
        if lp:
            if vmid is not None:
                link_container_project(known["asset_id"], vmid)
            return {"asset_id": known["asset_id"], "local_path": lp, "copied": False}
    # Vérification à la SOURCE (partage monté sur le nœud) : un fichier illisible n'est pas copié.
    host_src = storage.node_path({"conn_id": conn_id, "path": ""}, share_rel)
    chk = _run_on_node(node, {"mode": "check", "path": "/x/" + share_rel},
                       extra_mounts=[(storage.NODE_BASE + "/conn/%d" % conn_id, "/x")], timeout=300)
    if not chk.get("ok"):
        raise RuntimeError("fichier illisible, non importé : %s" % chk.get("error"))
    # Identité AVANT le nom de fichier (il porte son code) : celle d'un contenu déjà connu (même
    # empreinte : l'original d'un média, ou une de ses VERSIONS — dont la clé suit alors le code),
    # sinon une nouvelle.
    want, wver = find_by_fingerprint(chk.get("fp"), c)
    want, wver = want or str(uuid.uuid4()), wver or ORIGINAL
    dest = media_naming.coded_name(prefix + "Copies réseau/" + entry + "/" + sub, want, version=wver)
    dst_full = _mount(node).rstrip("/") + "/" + dest
    from . import node_driver
    tmp = dst_full + ".partial"
    cmd = ("set -e; mkdir -p %s; if [ ! -e %s ]; then cp --preserve=timestamps %s %s && mv -n %s %s; fi"
           % (shlex.quote(os.path.dirname(dst_full)), shlex.quote(dst_full), shlex.quote(host_src),
              shlex.quote(tmp), shlex.quote(tmp), shlex.quote(dst_full)))
    rc, out, err = node_driver.host_exec(node, cmd, timeout=6 * 3600)
    if rc != 0:
        node_driver.host_exec(node, "rm -f %s" % shlex.quote(tmp), timeout=20)
        raise RuntimeError("copie en échec : %s" % (err or out or "")[-300:])
    scan_node(node_id, probe_max=20)
    row = c.execute("SELECT asset_id, fingerprint FROM media_files WHERE node_id=? AND path=?", (node_id, dest)).fetchone()
    if not row:
        raise RuntimeError("copie faite mais introuvable à l'analyse : %s" % dest)
    if row["fingerprint"] and row["fingerprint"] != chk.get("fp"):
        raise RuntimeError("la copie ne correspond pas à la source (empreinte différente)")
    aid = row["asset_id"] or import_files(node_id, [dest], origin="share", want_ids={dest: want})[0]
    ver = c.execute("SELECT version FROM media_files WHERE node_id=? AND path=?", (node_id, dest)).fetchone()
    record_share_copy(aid, conn_id, share_rel, chk.get("size"), chk.get("mtime"), chk.get("fp"),
                      version=ver["version"] if ver else ORIGINAL)
    if vmid is not None:
        link_container_project(aid, vmid)
    return {"asset_id": aid, "local_path": dest[len(prefix):], "copied": True}


BROWSE_EXTS = (".mxf", ".mov", ".mp4", ".mkv", ".avi", ".ts", ".mts", ".m2ts", ".m4v", ".webm",
               ".wav", ".mp3", ".aac", ".flac", ".m4a",
               ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff")


def browse_share(vmid, rel=""):
    """Un niveau d'un serveur réseau TEL QUE LE VOIT le conteneur `vmid` (sa portée de projet),
    lu sur les montages de l'orchestrateur — même vue partout (player, images fixes, gestionnaire).
    `rel` = « » (les entrées accordées) ou « <entrée>/<sous-dossier> ». Chaque fichier dit s'il est
    déjà dans la base."""
    from services import storage
    views = storage._views().get(_scope_of(vmid)) or {}
    rel = storage._clean_rel(rel or "")
    if not rel:
        return {"path": "", "dirs": sorted(views), "files": []}
    entry, _, sub = rel.partition("/")
    src = views.get(entry)
    if not src:
        raise ValueError("serveur réseau inconnu : %s" % entry)
    conn_id, _, base = src.partition("/")
    full = os.path.join(storage.ORCH_BASE, "conn", conn_id, base, sub)
    dirs, files = [], []
    for e in sorted(os.scandir(full), key=lambda x: x.name.lower()):
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir():
                dirs.append(e.name)
            elif e.name.lower().endswith(BROWSE_EXTS):
                st = e.stat()
                files.append({"name": e.name, "size": st.st_size, "mtime": int(st.st_mtime)})
        except OSError:
            continue
    status = share_status_for(vmid, [rel + "/" + f["name"] for f in files])
    for f in files:
        f["asset_id"] = status.get(rel + "/" + f["name"])
    return {"path": rel, "dirs": dirs, "files": files}


_SSH_OPT = "-o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=8"


def _ssh_stream(src_host, src_abs, dst_host, dst_abs, timeout):
    """Copie d'un nœud à l'autre PAR l'orchestrateur (`ssh src cat | ssh dst cat >`) : aucune
    confiance nœud ↔ nœud requise, rien de stocké au milieu. ⚠ Root-SSH : l'agent de nœud n'a pas
    encore d'API fichiers en flux (dette notée au chantier) — même transport que le Gestionnaire."""
    import subprocess
    dst_remote = "mkdir -p %s && cat > %s" % (shlex.quote(os.path.dirname(dst_abs)), shlex.quote(dst_abs))
    cmd = "ssh %s root@%s %s | ssh %s root@%s %s" % (
        _SSH_OPT, shlex.quote(src_host), shlex.quote("cat " + shlex.quote(src_abs)),
        _SSH_OPT, shlex.quote(dst_host), shlex.quote(dst_remote))
    r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return r.returncode == 0, (r.stdout or b"").decode("utf-8", "replace")[-300:]


COPY_RATE_DEFAULT_MBPS = 60.0


def _copy_rate():
    """Plafond de débit d'une copie entre nœuds (octets/s) : réglage `media_copy_rate_mbps` (Mo/s,
    0 = sans plafond). Défaut 60 Mo/s ≈ la moitié d'un lien gigabit : la copie passe par le réseau
    de CONTRÔLE (1 Gb/s sur dl360-1 et dell-1, mesuré le 2026-10-11 : 78 Mo/s sans plafond), qui
    porte aussi le pilotage des nœuds et le monitoring — une copie ne doit pas le saturer."""
    try:
        from . import settings as _st
        v = float(_st.get("media_copy_rate_mbps") if _st.get("media_copy_rate_mbps") not in (None, "") else COPY_RATE_DEFAULT_MBPS)
    except Exception:                                                      # noqa: BLE001
        v = COPY_RATE_DEFAULT_MBPS
    return int(v * 1e6) if v > 0 else None


def _agent_copy(sn, dn, src_rel, dest_rel, size, mtime, progress=None):
    """Copie DIRECTE nœud → nœud par les agents : le nœud cible télécharge chez le nœud source
    (mTLS, autorisation signée, basse priorité, plafond de débit, reprise). L'orchestrateur ne fait
    qu'autoriser et suivre."""
    from . import node_driver
    tid = uuid.uuid4().hex
    ok, d = node_driver.transfer_fetch(dn, tid, sn, src_rel, dest_rel, size=size, mtime=mtime, rate=_copy_rate())
    if not ok or not (d or {}).get("ok"):
        raise RuntimeError("transfert refusé par %s : %s" % (dn.get("name"), (d or {}).get("error") if isinstance(d, dict) else d))
    last_err = 0
    while True:
        time.sleep(2)
        ok, st = node_driver.transfer_status(dn, tid)
        if not ok:
            last_err += 1
            if last_err > 30:                     # une minute sans réponse de l'agent cible
                raise RuntimeError("agent de %s injoignable pendant la copie" % dn.get("name"))
            continue
        last_err = 0
        if progress and size:
            progress(min(1.0, float(st.get("done") or 0) / float(size)))
        if st.get("state") == "done":
            return
        if st.get("state") in ("failed", "cancelled"):
            raise RuntimeError("copie en échec : %s" % (st.get("error") or st.get("state")))


def copy_asset(aid, dest_node_id, dest_rel=None, progress=None, version=None):
    """Copie VÉRIFIÉE d'un média vers un nœud : fichier + sidecar (qui porte l'identité et la
    version), puis analyse du nœud cible et comparaison d'empreinte — celle de la VERSION copiée
    (`version`, défaut : la version par défaut du média). Une copie de cette version déjà présente
    sur ce nœud est réutilisée. Renvoie le chemin (relatif au volume) de la copie.

    Transport : DIRECT entre agents quand les deux le permettent (agent ≥ 0.24.0, TLS) ; sinon
    repli root-SSH relayé par l'orchestrateur (agents plus anciens)."""
    from . import node_driver
    c = db()
    if not c.execute("SELECT 1 FROM media_assets WHERE id=?", (aid,)).fetchone():
        raise ValueError("média inconnu")
    version = resolve_version(aid, version)
    have = c.execute("""SELECT path FROM media_files WHERE asset_id=? AND node_id=? AND state='present' AND share_id=0
                        AND growing=0 AND kind='main' AND version=?""", (aid, int(dest_node_id), version)).fetchone()
    if have and (dest_rel is None or have["path"] == dest_rel):
        return have["path"]
    src = c.execute("""SELECT node_id, path, mtime, size FROM media_files WHERE asset_id=? AND state='present' AND share_id=0
                       AND growing=0 AND kind='main' AND version=? AND node_id!=? ORDER BY node_id LIMIT 1""",
                    (aid, version, int(dest_node_id))).fetchone()
    if not src:
        raise RuntimeError("aucune copie complète sur un autre nœud" +
                           ("" if version == ORIGINAL else " (version %s)" % version))
    sn, dn = _node(src["node_id"]), _node(dest_node_id)
    if not (sn and dn and sn.get("host") and dn.get("host")):
        raise RuntimeError("nœud source ou cible sans adresse")
    # Nom sur le nœud cible : celui de la source s'il porte déjà le code de CE média (même nom
    # partout), sinon le nom codé (une copie est un fichier CRÉÉ par le système, cf. media_naming).
    rel = media_naming.name_on_node(dest_rel or src["path"], aid, src["path"], version=version)
    if ".." in rel.split("/"):
        raise ValueError("chemin invalide")

    def owner(cand):
        """Identité du média qui occupe `cand` sur le nœud cible (registre, sinon sidecar), "" si
        inconnue, None si le nom est libre."""
        d = _mount(dn).rstrip("/") + "/" + cand
        rc, out, _e = node_driver.host_exec(dn, "test -e %s && { grep -o -m1 '<media[^>]* id=\"[^\"]*\"' %s.xml 2>/dev/null; true; }"
                                            % (shlex.quote(d), shlex.quote(d)), timeout=20)
        if rc != 0:
            return None
        r = c.execute("SELECT asset_id FROM media_files WHERE node_id=? AND share_id=0 AND path=?",
                      (int(dest_node_id), cand)).fetchone()
        if r and r["asset_id"]:
            return r["asset_id"]
        m = re.search(r'id="([^"]+)"', out or "")
        return m.group(1) if m else ""
    # Nom déjà pris sur le nœud cible : par le MÊME média → la copie est déjà là (vérifiée ci-dessous,
    # rien n'est recopié) ; par un AUTRE → son code diffère, sinon code à 8 (media_naming.pick_name).
    who = owner(rel)
    if who is not None and str(who).lower() != str(aid).lower():
        rel = media_naming.pick_name(rel, aid, owner, version=version)
        who = owner(rel)
    already = who is not None
    s_abs = _mount(sn).rstrip("/") + "/" + src["path"]
    d_abs = _mount(dn).rstrip("/") + "/" + rel
    if already:
        pass
    elif node_driver.transfer_capable(sn) and node_driver.transfer_capable(dn):
        # Direct : le fichier, puis son sidecar (qui porte l'identité). La date d'origine est
        # reposée par l'agent cible.
        _agent_copy(sn, dn, src["path"], rel, src["size"], src["mtime"], progress)
        rc, _o, _e = node_driver.host_exec(sn, "test -e %s" % shlex.quote(s_abs + ".xml"), timeout=20)
        if rc == 0:
            _agent_copy(sn, dn, src["path"] + ".xml", rel + ".xml", None, None)
    else:
        ok, msg = _ssh_stream(sn["host"], s_abs, dn["host"], d_abs + ".partial", timeout=6 * 3600)
        if not ok:
            node_driver.host_exec(dn, "rm -f %s" % shlex.quote(d_abs + ".partial"), timeout=20)
            raise RuntimeError("copie en échec : %s" % msg)
        # Date d'origine REPOSÉE : un fichier daté de « maintenant » passe pour un fichier en cours
        # d'écriture, et l'analyse ne calcule pas son empreinte — la vérification sautait en silence
        # (constaté au premier essai réel, 2026-10-11).
        node_driver.host_exec(dn, "mv -n %s %s && touch -d @%d %s" % (shlex.quote(d_abs + ".partial"), shlex.quote(d_abs),
                                                                       int(src["mtime"] or 0), shlex.quote(d_abs)), timeout=30)
        _ssh_stream(sn["host"], s_abs + ".xml", dn["host"], d_abs + ".xml", timeout=120)
    scan_node(dest_node_id, probe_max=10)
    row = c.execute("SELECT asset_id, fingerprint, version FROM media_files WHERE node_id=? AND path=?",
                    (int(dest_node_id), rel)).fetchone()
    fp = _version_fp(aid, version)
    if not row or row["asset_id"] != aid:
        raise RuntimeError("copie faite mais non rattachée au média (sidecar ?)")
    if row["version"] != version:
        raise RuntimeError("copie faite mais rattachée à une autre version (%s) que %s" % (row["version"], version))
    if not row["fingerprint"]:
        raise RuntimeError("copie faite mais empreinte non calculée : vérification impossible")
    if fp and row["fingerprint"] != fp:
        c.execute("UPDATE media_files SET state='corrupt' WHERE node_id=? AND path=?", (int(dest_node_id), rel))
        c.commit()
        raise RuntimeError("la copie ne correspond pas à l'original (empreinte différente)")
    c.execute("UPDATE media_files SET verified_at=? WHERE node_id=? AND path=?", (_now(), int(dest_node_id), rel))
    c.commit()
    return rel


def fetch_for_container(aid, vmid, progress=None, version=None):
    """Rapatrie un média pour un conteneur (« Charger » un média sans copie sur son nœud) : copie
    vérifiée dans SON champ de vision (dossier de son projet) — de sa version par défaut, ou de
    `version`. Renvoie le chemin vu par lui. `progress(fraction)` : transmis à copy_asset (suivi
    par app/media_fetch.py)."""
    node_id, prefix = container_view(vmid)
    version = resolve_version(aid, version)
    lp = local_path(_files_of(aid), node_id, prefix, version=version)
    if lp:
        link_container_project(aid, vmid)
        return lp
    src = db().execute("""SELECT path FROM media_files WHERE asset_id=? AND state='present' AND share_id=0 AND kind='main'
                          AND version=? LIMIT 1""", (aid, version)).fetchone()
    if not src:
        raise RuntimeError("aucune copie présente à rapatrier" + ("" if version == ORIGINAL else " (version %s)" % version))
    rel = src["path"] if src["path"].startswith(prefix) else prefix + src["path"]
    rel = copy_asset(aid, node_id, rel, progress=progress, version=version) or rel  # nom codé : peut différer de la source
    link_container_project(aid, vmid)
    return rel[len(prefix):]


def copy_bin(bin_id, node_id=None, vmid=None, children=True, progress=None, scope=None):
    """Copie VÉRIFIÉE de tout le contenu d'un bin (et de ses sous-bins si `children`) : vers le
    nœud `node_id` (Gestionnaire), ou dans le champ de vision du conteneur `vmid` (rapatriement
    pour un player…). Ce qui y est déjà n'est pas recopié. `progress(fait, total, nom)`.
    Renvoie {copied, already, errors:{nom: raison}}."""
    if (node_id is None) == (vmid is None):
        raise ValueError("node_id ou vmid requis")
    assets = bin_assets(bin_id, children=children, scope=scope)
    copied, already, errors = [], [], {}
    if vmid is not None:
        nid, prefix = container_view(vmid)
    else:
        nid, prefix = int(node_id), ""
    for k, a in enumerate(assets):
        if progress:
            progress(k, len(assets), a["name"])
        try:
            if local_path(_files_of(a["id"]), nid, prefix, version=default_version(a["id"])) is not None:
                already.append(a["id"])
                if vmid is not None:
                    link_container_project(a["id"], vmid)
                continue
            if vmid is not None:
                fetch_for_container(a["id"], vmid)
            else:
                copy_asset(a["id"], nid)
            copied.append(a["id"])
        except (ValueError, RuntimeError) as e:
            errors[a["name"]] = str(e)
    if progress:
        progress(len(assets), len(assets), "")
    return {"copied": copied, "already": already, "errors": errors, "total": len(assets)}


def _sed_lit(s, repl=False):
    """Texte littéral dans une expression sed (BRE, séparateur |) : motif, ou remplacement."""
    special = "\\&|" if repl else "\\.*[]^$|"
    return "".join("\\" + ch if ch in special else ch for ch in s)


def rename_copy(aid, node_id, path, new_name):
    """Renomme UNE copie sur son nœud : le fichier ET son sidecar `.xml`, dans le même dossier.
    L'identité ne change pas (elle est dans le sidecar, qui suit) ; le NOM du média (sa fiche)
    non plus — c'est un autre champ. Refusé si la cible existe (fichier ou sidecar), si la copie
    est en cours d'écriture, ou pour une copie sur un partage. Renvoie le nouveau chemin."""
    import xml.sax.saxutils as sx
    from . import node_driver
    new_name = (new_name or "").strip()
    if (not new_name or "/" in new_name or "\0" in new_name or new_name in (".", "..")
            or new_name.startswith(".") or new_name.lower().endswith(".xml")):
        raise ValueError("nom de fichier invalide")
    c = db()
    r = c.execute("SELECT * FROM media_files WHERE asset_id=? AND node_id=? AND share_id=0 AND kind='main' AND path=?",
                  (aid, int(node_id), path)).fetchone()
    if not r:
        raise ValueError("copie inconnue")
    if r["state"] != "present":
        raise ValueError("copie absente : rien à renommer")
    if r["growing"]:
        raise RuntimeError("fichier en cours d'écriture : renommage refusé")
    if ".." in path.split("/"):
        raise ValueError("chemin invalide")
    # Seule la BASE d'un nom codé se renomme : le code du média, sa version (ou piste Avid, proxy)
    # et l'extension sont repris de l'ancien nom — on ne les réécrit pas à la main (décision
    # 2026-10-18), quel que soit ce que l'appelant envoie.
    new_name = media_naming.rename_stem(os.path.basename(path), new_name, aid)
    if not new_name or new_name.startswith("."):
        raise ValueError("nom de fichier invalide")
    d = os.path.dirname(path)
    new_rel = (d + "/" if d else "") + new_name
    if new_rel == path:
        return path
    if c.execute("SELECT 1 FROM media_files WHERE node_id=? AND share_id=0 AND path=?", (int(node_id), new_rel)).fetchone():
        raise ValueError("un fichier porte déjà ce nom : %s" % new_name)
    node = _node(node_id)
    base = _mount(node).rstrip("/") + "/"
    o, n = shlex.quote(base + path), shlex.quote(base + new_rel)
    # rc 17 = la cible existe (fichier ou sidecar) ; le sidecar suit le fichier, et son <file>
    # (nom affiché par les outils qui le lisent) est mis à jour au mieux.
    sed = "s|<file>%s</file>|<file>%s</file>|" % (_sed_lit(sx.escape(os.path.basename(path))), _sed_lit(sx.escape(new_name), repl=True))
    cmd = ("if [ -e %(n)s ] || [ -e %(n)s.xml ]; then exit 17; fi; mv -n -- %(o)s %(n)s || exit 1; "
           "if [ -e %(o)s.xml ]; then mv -n -- %(o)s.xml %(n)s.xml && sed -i %(sed)s %(n)s.xml || true; fi"
           % {"o": o, "n": n, "sed": shlex.quote(sed)})
    rc, _out, err = node_driver.host_exec(node, cmd, timeout=60)
    if rc == 17:
        raise ValueError("un fichier porte déjà ce nom sur %s : %s" % (node.get("name"), new_name))
    if rc != 0:
        raise RuntimeError("renommage en échec : %s" % (err or "")[-300:])
    c.execute("UPDATE media_files SET path=? WHERE node_id=? AND share_id=0 AND path=?", (new_rel, int(node_id), path))
    c.commit()
    return new_rel


SIDECAR_MAX = 256 * 1024


def read_sidecar(aid, node_id, path, share_id=0):
    """Contenu BRUT du sidecar `.xml` d'une copie (lecture seule) : lu sur le nœud, ou sur le
    montage de l'orchestrateur pour une copie de partage. None s'il n'y en a pas."""
    from . import node_driver
    r = db().execute("SELECT 1 FROM media_files WHERE asset_id=? AND node_id=? AND share_id=? AND kind='main' AND path=?",
                     (aid, int(node_id or 0), int(share_id or 0), path)).fetchone()
    if not r:
        raise ValueError("copie inconnue")
    if ".." in path.split("/"):
        raise ValueError("chemin invalide")
    if share_id:
        from services import storage
        full = os.path.join(storage.ORCH_BASE, "conn", str(int(share_id)), path) + ".xml"
        try:
            with open(full, "rb") as f:
                return f.read(SIDECAR_MAX).decode("utf-8", "replace")
        except OSError:
            return None
    node = _node(node_id)
    full = _mount(node).rstrip("/") + "/" + path + ".xml"
    rc, out, _e = node_driver.host_exec(node, "test -e %s || exit 3; head -c %d -- %s"
                                        % (shlex.quote(full), SIDECAR_MAX, shlex.quote(full)), timeout=20)
    if rc == 3:
        return None
    if rc != 0:
        raise RuntimeError("lecture du sidecar en échec")
    return out if isinstance(out, str) else (out or b"").decode("utf-8", "replace")


def container_context(vmid):
    """Ce que la bibliothèque d'un conteneur doit savoir de lui : son nœud, son projet."""
    from .database import db_get_container, db_get_project
    ct = db_get_container(int(vmid)) or {}
    pid = ct.get("project_id")
    proj = (db_get_project(pid) or {}) if pid else {}
    return {"vmid": int(vmid), "node_id": ct.get("node_id"), "project_id": pid or None,
            "project_name": proj.get("name") if pid else None}


def migrate_legacy_bins():
    """Reprise UNE FOIS des bins de l'ancien Gestionnaire (base principale, items par nœud +
    chemin) en bins de la base des médias (items par identité). Un fichier qui n'est pas dans la
    base n'est pas repris (compté). Tolérante à des tables déjà retirées ; une fois la reprise
    faite, les anciennes tables sont SUPPRIMÉES de la base principale (`drop_legacy_bins`)."""
    from .database import get_db
    c = db()
    if c.execute("SELECT value FROM media_meta WHERE key='legacy_bins_migrated'").fetchone():
        drop_legacy_bins()
        return None
    try:
        old = get_db().execute("SELECT id, name, parent_id FROM media_bins").fetchall()
        items = get_db().execute("SELECT bin_id, node_id, path FROM media_bin_items").fetchall()
    except sqlite3.Error:
        old, items = [], []
    idmap, done, skipped = {}, 0, 0
    pending = list(old)
    while pending:                       # parents avant enfants
        progressed = False
        for b in list(pending):
            pid = b["parent_id"]
            if pid and pid not in idmap and any(x["id"] == pid for x in old):
                continue
            idmap[b["id"]] = save_bin(b["name"], parent_id=idmap.get(pid))
            pending.remove(b)
            progressed = True
        if not progressed:
            for b in pending:
                idmap[b["id"]] = save_bin(b["name"])
            break
    for it in items:
        r = c.execute("SELECT asset_id FROM media_files WHERE node_id=? AND path=? AND asset_id IS NOT NULL AND kind='main'",
                      (it["node_id"], it["path"])).fetchone()
        if r and it["bin_id"] in idmap:
            bin_add(idmap[it["bin_id"]], [r["asset_id"]])
            done += 1
        else:
            skipped += 1
    c.execute("INSERT OR REPLACE INTO media_meta(key, value) VALUES ('legacy_bins_migrated', ?)", (_now(),))
    c.commit()
    drop_legacy_bins()
    return {"bins": len(idmap), "items": done, "skipped": skipped}


def drop_legacy_bins():
    """Retire de la BASE PRINCIPALE les tables des bins de l'ancien Gestionnaire (`media_bins`,
    `media_bin_items`, clé nœud + chemin), UNE FOIS, et seulement après la reprise. Les tables du
    même nom de la base des médias (`db_medias.db`) ne sont pas concernées : autre fichier."""
    from .database import get_db
    c = db()
    if c.execute("SELECT 1 FROM media_meta WHERE key='legacy_bins_dropped'").fetchone():
        return False
    if not c.execute("SELECT 1 FROM media_meta WHERE key='legacy_bins_migrated'").fetchone():
        return False
    g = get_db()
    g.execute("DROP TABLE IF EXISTS media_bin_items")
    g.execute("DROP TABLE IF EXISTS media_bins")
    g.commit()
    c.execute("INSERT OR REPLACE INTO media_meta(key, value) VALUES ('legacy_bins_dropped', ?)", (_now(),))
    c.commit()
    return True


def share_status_for(vmid, ext_paths):
    """Navigation dans un serveur réseau depuis un conteneur : {chemin /mnt/ext: id du média} des
    fichiers déjà importés."""
    out = {}
    by_conn = {}
    for p in ext_paths or []:
        try:
            conn_id, share_rel, _e, _s = _ext_resolve(vmid, p)
        except ValueError:
            continue
        by_conn.setdefault(conn_id, {})[share_rel] = p
    for conn_id, m in by_conn.items():
        for share_rel, aid in share_status(conn_id, list(m)).items():
            out[m[share_rel]] = aid
    return out


# ─── Portée d'un utilisateur (décision du 2026-10-12 : « Bibliothèque : il faut restreindre ») ──
# `scope` = auth.scoped_project_ids() : None = accès global (tout), sinon l'ensemble des projets de
# l'utilisateur. Un utilisateur restreint ne voit et n'atteint QUE les médias rattachés
# (`project_media`) à l'un de ses projets — un média SANS projet lui est invisible, comme dans la
# ferme. Un bin rattaché à un projet hors portée est invisible ; un bin sans projet reste visible,
# mais n'y paraissent que les médias de la portée (compteurs compris). Toute fonction de lecture
# prend `scope` ; les routes le passent, et répondent 404 (on ne confirme pas l'existence) pour un
# média ou un bin hors portée.
# PARTAGES (2026-10-12, app/media_sharing.py) : la portée d'un restreint est alors un `MediaScope` —
# toujours l'ensemble de ses projets, plus `assets` / `bins` partagés avec lui. Les quatre fonctions
# ci-dessous les ajoutent ; un frozenset nu (portée de base) n'a rien de partagé.
def _scope_ids(scope):
    return None if scope is None else sorted({int(p) for p in scope})


def _shared_assets(scope):
    return getattr(scope, "assets", None) or {}


def scope_sql(scope, alias="a", col="id"):
    """(clause SQL, arguments) qui restreint `alias.col` (identité du média) à la portée ; ('', [])
    pour l'accès global."""
    ids = _scope_ids(scope)
    if ids is None:
        return "", []
    parts, args = [], []
    if ids:
        parts.append("EXISTS (SELECT 1 FROM project_media ps WHERE ps.asset_id=%s.%s AND ps.project_id IN (%s))"
                     % (alias, col, ",".join("?" * len(ids))))
        args += ids
    shared = _shared_assets(scope)
    if shared:
        parts.append("%s.%s IN (SELECT value FROM json_each(?))" % (alias, col))
        args.append(json.dumps(sorted(shared)))
    if not parts:
        return "0", []
    return "(" + " OR ".join(parts) + ")", args


def asset_visible(aid, scope):
    """Le média existe ET est dans la portée."""
    if not aid:
        return False
    if scope is None or aid in _shared_assets(scope):
        return db().execute("SELECT 1 FROM media_assets WHERE id=?", (aid,)).fetchone() is not None
    ids = _scope_ids(scope)
    if not ids:
        return False
    return db().execute("SELECT 1 FROM project_media WHERE asset_id=? AND project_id IN (%s)" % ",".join("?" * len(ids)),
                        [aid] + ids).fetchone() is not None


def visible_ids(ids, scope):
    """Sous-ensemble de `ids` dans la portée (ordre conservé)."""
    ids = [x for x in ids if isinstance(x, str)]
    if scope is None or not ids:
        return ids
    sid = _scope_ids(scope)
    shared = _shared_assets(scope)
    if not sid:
        return [x for x in ids if x in shared]
    ok = set(x for x in ids if x in shared)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        ok |= {r["asset_id"] for r in db().execute(
            "SELECT asset_id FROM project_media WHERE asset_id IN (%s) AND project_id IN (%s)"
            % (",".join("?" * len(part)), ",".join("?" * len(sid))), part + sid)}
    return [x for x in ids if x in ok]


def project_in_scope(pid, scope):
    return scope is None or (pid is not None and int(pid) in {int(p) for p in scope})


def _visible_bin_ids(scope):
    """Bins visibles : le sien sans projet ou d'un projet de la portée, et tous ses ancêtres aussi
    (un bin global rangé sous le bin d'un autre projet disparaît avec lui)."""
    rows = {r["id"]: r for r in db().execute("SELECT id, parent_id, project_id FROM media_bins")}
    if scope is None:
        return set(rows)
    sid = set(_scope_ids(scope))
    out, memo = set(), {}

    def ok(b, depth=0):
        if b in memo:
            return memo[b]
        r = rows.get(b)
        v = bool(r) and depth < 64 and (r["project_id"] is None or r["project_id"] in sid) and \
            (r["parent_id"] is None or r["parent_id"] not in rows or ok(r["parent_id"], depth + 1))
        memo[b] = v
        return v
    for b in rows:
        if ok(b):
            out.add(b)
    out |= {b for b in (getattr(scope, "bins", None) or {}) if b in rows}       # bins partagés (et leurs sous-bins)
    return out


def bin_visible(bin_id, scope):
    try:
        return int(bin_id) in _visible_bin_ids(scope)
    except (TypeError, ValueError):
        return False


def annotate_names(assets, scope):
    """Homonymes : plusieurs projets ont légitimement des clips du même nom. Chaque média reçoit
    `projects` (ids de SES projets, bornés à la portée) et `homonyms` (combien d'AUTRES médias de la
    portée portent le même nom, casse ignorée). S'il y en a, `name_hint` dit lequel c'est : les
    noms de ses projets, à défaut le nœud et le dossier de sa première copie. Rien d'autre n'est
    affiché quand le nom est unique."""
    if not assets:
        return assets
    c = db()
    sid = _scope_ids(scope)
    names = sorted({(a.get("name") or "").lower() for a in assets})
    counts = {}
    ssql, sargs = scope_sql(scope)
    for i in range(0, len(names), 500):
        part = names[i:i + 500]
        sql = "SELECT LOWER(a.name) AS n, COUNT(*) AS k FROM media_assets a WHERE LOWER(a.name) IN (%s)" % ",".join("?" * len(part))
        if ssql:
            sql += " AND " + ssql
        counts.update({r["n"]: r["k"] for r in c.execute(sql + " GROUP BY LOWER(a.name)", part + sargs)})
    ids = [a["id"] for a in assets]
    projs = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in c.execute("SELECT asset_id, project_id FROM project_media WHERE asset_id IN (%s) ORDER BY project_id"
                           % ",".join("?" * len(part)), part):
            if sid is None or r["project_id"] in sid:
                projs.setdefault(r["asset_id"], []).append(r["project_id"])
    pnames = None
    for a in assets:
        a["projects"] = projs.get(a["id"], [])
        a["homonyms"] = max(0, counts.get((a.get("name") or "").lower(), 1) - 1)
        if not a["homonyms"]:
            continue
        if pnames is None:
            try:
                from .database import db_get_projects
                pnames = {p["id"]: p.get("name") for p in (db_get_projects() or [])}
            except Exception:                                              # noqa: BLE001
                pnames = {}
        hint = ", ".join(str(pnames.get(p) or "#%s" % p) for p in a["projects"])
        if not hint:
            f = c.execute("SELECT node_id, path FROM media_files WHERE asset_id=? AND kind='main' AND share_id=0 "
                          "ORDER BY state='present' DESC, node_id LIMIT 1", (a["id"],)).fetchone()
            if f:
                n = _node(f["node_id"]) or {}
                d = os.path.dirname(f["path"])
                hint = (n.get("name") or "#%s" % f["node_id"]) + (" · " + d if d else "")
        a["name_hint"] = hint
    return assets


# ─── Recherche ──────────────────────────────────────────────────────────────────────────────
# Une requête = {"q": texte libre, "kinds": [...], "match": "all"|"any", "items": [condition | groupe]} ;
# une condition = {"field", "op", "value"}, un groupe = {"match", "items"} (même forme, imbriqué —
# « (A ET B) OU C »). Un groupe vide est NEUTRE : retiré de son parent, il ne filtre rien.
# Ancien format, encore lu partout (bins intelligents enregistrés, API) : {"all": [...], "any": [...]}
# = toutes les `all` ET au moins une des `any` — converti à la lecture (`query_tree`), jamais réécrit.
# Les champs sont une LISTE BLANCHE traduite en SQL paramétré (jamais de texte de l'utilisateur
# dans la requête) ; les champs personnalisés s'y ajoutent depuis `media_fields`.
_J = "json_extract(a.%s, '%s')"
_KIND = ("COALESCE(json_extract(a.tech_json, '$.kind'), CASE WHEN json_extract(a.tech_json, '$.video') IS NOT NULL "
         "THEN 'video' WHEN COALESCE(json_array_length(a.tech_json, '$.audio'), 0) > 0 THEN 'audio' END)")
_PROC = ("COALESCE(json_extract(a.process_json, '{0}'), (SELECT json_extract(v.process_json, '{0}') FROM media_versions v "
         "WHERE v.asset_id=a.id AND json_extract(v.process_json, '{0}') IS NOT NULL ORDER BY v.created_at DESC LIMIT 1))").format
SEARCH_FIELDS = {
    "name":         ("text",   "a.name"),
    "created_at":   ("date",   "a.created_at"),
    "updated_at":   ("date",   "a.updated_at"),
    "origin":       ("enum",   "a.origin"),
    "producer":     ("enum",   "a.producer"),
    "size":         ("number", "a.size"),
    "duration":     ("number", _J % ("tech_json", "$.duration")),
    "width":        ("number", _J % ("tech_json", "$.video.width")),
    "height":       ("number", _J % ("tech_json", "$.video.height")),
    "fps":          ("number", _J % ("tech_json", "$.video.fps")),
    "scan":         ("enum",   _J % ("tech_json", "$.video.scan")),
    "vcodec":       ("enum",   _J % ("tech_json", "$.video.codec")),
    "container":    ("text",   _J % ("tech_json", "$.container")),
    "timecode":     ("text",   _J % ("tech_json", "$.timecode")),
    "audio_tracks": ("number", "COALESCE(json_array_length(a.tech_json, '$.audio'), 0)"),
    "audio_codec":  ("text",   "(SELECT group_concat(json_extract(x.value, '$.codec')) FROM json_each(a.tech_json, '$.audio') x)"),
    "has_video":    ("bool",   "(json_extract(a.tech_json, '$.video') IS NOT NULL)"),
    # Type : écrit par l'analyse depuis l'ajout des images ; déduit pour les fiches plus anciennes.
    "kind":         ("enum",   _KIND),
    "title":        ("text",   _J % ("meta_json", "$.title")),
    "description":  ("text",   _J % ("meta_json", "$.description")),
    "keywords":     ("text",   _J % ("meta_json", "$.keywords")),
    "copies":       ("number", "(SELECT COUNT(*) FROM media_files f WHERE f.asset_id=a.id AND f.state='present' AND f.share_id=0 AND f.kind='main')"),
    # Proxy de lecture présent (sur au moins un nœud).
    "has_proxy":    ("bool",   "EXISTS (SELECT 1 FROM media_files f WHERE f.asset_id=a.id AND f.kind='proxy' AND f.state='present')"),
    # Filiation (variantes de l'éditeur d'image) et VERSIONS (sorties du transcodeur, rangées sous
    # leur source). Un champ de traitement vaut celui de la fiche (variante), sinon celui de sa
    # version la plus récente qui le porte.
    "derived":      ("bool",   "(a.derived_from IS NOT NULL)"),
    "relation":     ("enum",   "a.relation"),
    "versions":     ("number", "(SELECT COUNT(*) FROM media_versions v WHERE v.asset_id=a.id)"),
    "version":      ("text",   "(SELECT group_concat(v.key, ', ') FROM media_versions v WHERE v.asset_id=a.id)"),
    "preset":       ("text",   _PROC("$.preset")),
    "loudness_lufs": ("number", _PROC("$.loudness.measured_lufs")),
    "true_peak":    ("number", _PROC("$.loudness.true_peak_dbtp")),
    "realtime_factor": ("number", _PROC("$.result.realtime_factor")),
    "submitted_by": ("text",   _PROC("$.submitted_by")),
    "watchfolder":  ("text",   _PROC("$.watchfolder.name")),
    "on_node":      ("node",   None),       # copie présente sur ce nœud (valeur = id de nœud)
    "on_share":     ("share",  None),       # copie suivie sur ce partage réseau (valeur = id de connexion)
    "project":      ("project", None),      # rattaché à ce projet (valeur = id de projet)
}
OPS = {
    "text":    ("contains", "not_contains", "eq", "ne", "empty", "not_empty"),
    "enum":    ("eq", "ne", "empty", "not_empty"),
    "number":  ("eq", "ne", "gt", "gte", "lt", "lte", "empty", "not_empty"),
    "date":    ("within", "not_within", "after", "before", "empty", "not_empty"),
    "bool":    ("is_true", "is_false"),
    "node":    ("eq", "ne"),
    "share":   ("eq", "ne"),
    "project": ("eq", "ne"),
}
_UNITS = {"minutes": 60, "hours": 3600, "days": 86400}


def search_fields():
    """Catalogue des champs interrogeables (pour construire l'éditeur de recherche)."""
    out = [{"key": k, "type": t, "ops": list(OPS[t]), "custom": False} for k, (t, _) in SEARCH_FIELDS.items()]
    ftype = {"text": "text", "longtext": "text", "number": "number", "date": "date", "enum": "enum", "bool": "bool"}
    for f in list_fields():
        t = ftype.get(f["type"], "text")
        out.append({"key": "meta." + f["key"], "label": f["label"], "type": t, "ops": list(OPS[t]),
                    "options": f.get("options") or [], "custom": True})
    return out


VALUES_MAX = 300


def search_values(field, limit=VALUES_MAX, scope=None):
    """Valeurs DISTINCTES présentes dans la base pour `field` (aide au remplissage de la recherche
    avancée), les plus fréquentes d'abord : [{value, label?, count}]. Nœuds, partages et projets
    portent leur nom en `label`. Un champ date ou booléen n'a pas de liste (vide). Portée `scope` :
    valeurs et comptes tirés des seuls médias de la portée ; projets = ceux de l'utilisateur."""
    c = db()
    limit = max(1, min(int(limit or VALUES_MAX), 2000))
    ssql, sargs = scope_sql(scope)
    fsql, fargs = scope_sql(scope, "f", "asset_id")
    fand = (" AND " + fsql) if fsql else ""
    aand = (" AND " + ssql) if ssql else ""
    if field == "on_node":
        from .database import db_get_nodes
        names = {n["id"]: n.get("name") for n in db_get_nodes()}
        rows = c.execute("""SELECT node_id AS v, COUNT(DISTINCT asset_id) AS n FROM media_files f
                            WHERE asset_id IS NOT NULL AND state='present' AND share_id=0 AND kind='main'%s GROUP BY node_id""" % fand,
                         fargs).fetchall()
        out = [{"value": r["v"], "label": names.get(r["v"], "#%s" % r["v"]), "count": r["n"]} for r in rows]
        seen = {r["v"] for r in rows}
        out += [{"value": i, "label": nm, "count": 0} for i, nm in names.items() if i not in seen]
        return sorted(out, key=lambda x: (-x["count"], str(x["label"]).lower()))[:limit]
    if field == "on_share":
        names = _share_names()
        rows = c.execute("""SELECT share_id AS v, COUNT(DISTINCT asset_id) AS n FROM media_files f
                            WHERE asset_id IS NOT NULL AND share_id!=0%s GROUP BY share_id ORDER BY n DESC""" % fand,
                         fargs).fetchall()
        return [{"value": r["v"], "label": names.get(r["v"], "#%s" % r["v"]), "count": r["n"]} for r in rows][:limit]
    if field == "project":
        from .database import db_get_projects
        names = {p["id"]: p.get("name") for p in (db_get_projects() or []) if project_in_scope(p["id"], scope)}
        rows = {r["v"]: r["n"] for r in c.execute("SELECT project_id AS v, COUNT(*) AS n FROM project_media GROUP BY project_id")}
        out = [{"value": i, "label": nm, "count": rows.get(i, 0)} for i, nm in names.items()]
        return sorted(out, key=lambda x: (-x["count"], str(x["label"]).lower()))[:limit]
    if field == "audio_codec":
        rows = c.execute("""SELECT json_extract(x.value, '$.codec') AS v, COUNT(DISTINCT a.id) AS n
                            FROM media_assets a, json_each(a.tech_json, '$.audio') x
                            WHERE v IS NOT NULL AND v != ''%s GROUP BY v ORDER BY n DESC, v LIMIT ?""" % aand,
                         sargs + [limit]).fetchall()
        return [{"value": r["v"], "count": r["n"]} for r in rows]
    t, expr = _field_sql(field)
    if t not in ("text", "enum", "number"):
        return []
    rows = c.execute("SELECT %s AS v, COUNT(*) AS n FROM media_assets a WHERE v IS NOT NULL AND v != ''%s "
                     "GROUP BY v ORDER BY n DESC, v LIMIT ?" % (expr, aand), sargs + [limit]).fetchall()
    out = [{"value": r["v"], "count": r["n"]} for r in rows]
    if field.startswith("meta."):
        f = next((x for x in list_fields() if x["key"] == field[5:]), None)
        have = {str(x["value"]) for x in out}
        out += [{"value": o, "count": 0} for o in (f or {}).get("options") or [] if str(o) not in have]
    return out


def _field_sql(field):
    if field.startswith("meta."):
        key = field[5:]
        f = next((x for x in list_fields() if x["key"] == key), None)
        if not f:
            raise ValueError("champ inconnu : %s" % field)
        t = {"longtext": "text"}.get(f["type"], f["type"])
        return t, "json_extract(a.meta_json, '$.\"%s\"')" % key.replace('"', "")
    if field not in SEARCH_FIELDS:
        raise ValueError("champ inconnu : %s" % field)
    return SEARCH_FIELDS[field]


# Précision d'AFFICHAGE des champs numériques non entiers : la comparaison se fait à ce grain.
# Durée : à la seconde (la liste affiche m:ss arrondi) ; cadence : au centième (« 29.97p »).
_NUM_ROUND = {"duration": 0, "fps": 2, "loudness_lufs": 1, "true_peak": 1, "realtime_factor": 2}


def _parse_number(field, val):
    """Nombre saisi → float. Une durée accepte aussi « m:ss » ou « h:mm:ss », comme elle s'affiche."""
    if field == "duration" and isinstance(val, str) and ":" in val:
        secs = 0.0
        for part in val.strip().split(":"):
            secs = secs * 60 + float(part.replace(",", ".") or 0)
        return secs
    if isinstance(val, str):
        val = val.strip().replace(",", ".")
    return float(val)


def _cond_sql(cond):
    field, op, val = cond.get("field") or "", cond.get("op") or "", cond.get("value")
    t, expr = _field_sql(field)
    if op not in OPS.get(t, ()):
        raise ValueError("opérateur %s impossible sur %s" % (op, field))
    if t == "node":
        sql = "EXISTS (SELECT 1 FROM media_files f WHERE f.asset_id=a.id AND f.state='present' AND f.share_id=0 AND f.kind='main' AND f.node_id=?)"
        return ("NOT " if op == "ne" else "") + sql, [int(val)]
    if t == "share":
        sql = "EXISTS (SELECT 1 FROM media_files f WHERE f.asset_id=a.id AND f.share_id=?)"
        return ("NOT " if op == "ne" else "") + sql, [int(val)]
    if t == "project":
        sql = "EXISTS (SELECT 1 FROM project_media p WHERE p.asset_id=a.id AND p.project_id=?)"
        return ("NOT " if op == "ne" else "") + sql, [int(val)]
    if op == "empty":
        return "(%s IS NULL OR %s = '')" % (expr, expr), []
    if op == "not_empty":
        return "(%s IS NOT NULL AND %s != '')" % (expr, expr), []
    if op == "is_true":
        return "COALESCE(%s, 0) IN (1, 'true', 'oui')" % expr, []
    if op == "is_false":
        return "COALESCE(%s, 0) NOT IN (1, 'true', 'oui')" % expr, []
    if op in ("contains", "not_contains"):
        return ("%s(LOWER(COALESCE(%s, '')) LIKE ?)" % ("NOT " if op == "not_contains" else "", expr),
                ["%" + str(val or "").lower() + "%"])
    if op in ("within", "not_within"):
        # « dans les N dernières heures » : la borne est calculée À L'ÉVALUATION — un bin
        # intelligent reste juste sans être réenregistré.
        v = val if isinstance(val, dict) else {"n": val, "unit": "hours"}
        secs = float(v.get("n") or 0) * _UNITS.get(v.get("unit") or "hours", 3600)
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - secs))
        return ("%s %s ?" % (expr, ">=" if op == "within" else "<")), [since]
    sqlop = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "after": ">", "before": "<"}[op]
    if t == "number":
        try:
            val = _parse_number(field, val)
        except (TypeError, ValueError):
            raise ValueError("%s : nombre attendu" % field)
        # On compare ce que l'interface AFFICHE : une durée de 8,44 s se lit « 0:08 », donc
        # « durée = 8 » doit la trouver (recette du 2026-10-12 : « rien ne correspond »).
        if field in _NUM_ROUND:
            expr = "ROUND(%s, %d)" % (expr, _NUM_ROUND[field])
            val = round(val, _NUM_ROUND[field])
    elif t in ("text", "enum") and op in ("eq", "ne"):
        return "LOWER(COALESCE(%s, '')) %s LOWER(?)" % (expr, sqlop), [str(val or "")]
    return "%s %s ?" % (expr, sqlop), [val]


# Bornes d'une requête arborescente : profondeur (racine comprise) et nombre de conditions.
TREE_MAX_DEPTH = 4
TREE_MAX_CONDS = 50
_TREE_MAX_NODES = 100


def query_tree(query):
    """Arbre {"match", "items"} d'une requête, quel qu'en soit le format (cf. plus haut) ; valide la
    forme et les bornes (ValueError). Ne touche pas aux conditions elles-mêmes (cf. _cond_sql)."""
    query = query or {}
    if not isinstance(query, dict):
        raise ValueError("requête invalide")
    items = []
    if "items" in query or "match" in query:
        items.append({"match": query.get("match") or "all", "items": query.get("items") or []})
    items += list(query.get("all") or [])
    if query.get("any"):
        items.append({"match": "any", "items": list(query["any"])})
    root = items[0] if len(items) == 1 and isinstance(items[0], dict) and "items" in items[0] else {"match": "all", "items": items}
    count = {"conds": 0, "nodes": 0}

    def walk(node, depth):
        if depth > TREE_MAX_DEPTH:
            raise ValueError("groupes imbriqués sur plus de %d niveaux" % TREE_MAX_DEPTH)
        if (node.get("match") or "all") not in ("all", "any"):
            raise ValueError("combinaison inconnue : %s" % node.get("match"))
        if not isinstance(node.get("items") or [], list):
            raise ValueError("groupe invalide")
        out = []
        for it in node.get("items") or []:
            if not isinstance(it, dict):
                raise ValueError("critère invalide")
            count["nodes"] += 1
            if "items" in it or "match" in it:
                out.append(walk(it, depth + 1))
            else:
                count["conds"] += 1
                out.append({"field": it.get("field"), "op": it.get("op"), "value": it.get("value")})
            if count["conds"] > TREE_MAX_CONDS or count["nodes"] > _TREE_MAX_NODES:
                raise ValueError("plus de %d critères" % TREE_MAX_CONDS)
        return {"match": node.get("match") or "all", "items": out}
    return walk(root, 1)


def _tree_sql(node):
    """SQL paramétré d'un groupe ; (None, []) s'il est vide (neutre : son parent l'ignore)."""
    parts, args = [], []
    for it in node["items"]:
        sql, a2 = _tree_sql(it) if "items" in it else _cond_sql(it)
        if sql:
            parts.append(sql)
            args += a2
    if not parts:
        return None, []
    if len(parts) == 1:
        return parts[0], args
    return "(" + (" OR " if node["match"] == "any" else " AND ").join("(%s)" % p for p in parts) + ")", args


def _query_where(query):
    """Clauses (ET) d'une requête : types de média, texte libre, arbre des critères."""
    query = query or {}
    where, args = [], []
    kinds = [k for k in (query.get("kinds") or []) if k in ("video", "audio", "image")]
    if kinds:
        # Ce qu'un conteneur sait lire : les images pour Images fixes, vidéo + audio pour le player.
        where.append("%s IN (%s)" % (_KIND, ",".join("?" * len(kinds))))
        args += kinds
    q = (query.get("q") or "").strip().lower()
    if q:
        where.append("(LOWER(a.name) LIKE ? OR LOWER(COALESCE(a.meta_json, '')) LIKE ?)")
        args += ["%" + q + "%"] * 2
    sql, a2 = _tree_sql(query_tree(query))
    if sql:
        where.append(sql)
        args += a2
    return where, args


def search(query=None, project_id=None, limit=2000, also=None, scope=None):
    """Médias correspondant à `query` (cf. plus haut) ET, si donnée, à la requête `also` (filtre
    appliqué dans un bin intelligent), du projet `project_id` ou de toute la base — bornés à la
    portée `scope` (les deux filtres se cumulent : aucun n'élargit l'autre).
    Renvoie la même forme que list_assets."""
    where, args = [], []
    ssql, sargs = scope_sql(scope)
    if ssql:
        where.append(ssql)
        args += sargs
    if project_id is not None:
        where.append("EXISTS (SELECT 1 FROM project_media p WHERE p.asset_id=a.id AND p.project_id=?)")
        args.append(int(project_id))
    for qq in (query, also):
        w2, a2 = _query_where(qq)
        where += w2
        args += a2
    sql = "SELECT a.* FROM media_assets a" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY a.created_at DESC LIMIT ?"
    rows = db().execute(sql, args + [int(limit)]).fetchall()
    return _with_copies(rows)


def _with_copies(rows):
    """Lignes de `media_assets` → la forme de list_assets : UNE ligne par média, ses copies (toutes
    versions, chacune dit la sienne), la liste de ses versions et sa version par défaut — la ligne
    vaut la version par défaut (copie locale, nœuds, envoi)."""
    files, keys, vtech = {}, {}, {}
    ids = [r["id"] for r in rows]
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for f in db().execute("""SELECT asset_id, node_id, share_id, path, state, growing, version FROM media_files
                                 WHERE kind='main' AND asset_id IN (%s)""" % ",".join("?" * len(part)), part):
            files.setdefault(f["asset_id"], []).append({"node_id": f["node_id"], "share_id": f["share_id"], "path": f["path"],
                                                        "state": f["state"], "growing": bool(f["growing"]),
                                                        "version": f["version"] or ORIGINAL})
        for v in db().execute("SELECT asset_id, key, tech_json FROM media_versions WHERE asset_id IN (%s) ORDER BY created_at, key"
                              % ",".join("?" * len(part)), part):
            keys.setdefault(v["asset_id"], []).append(v["key"])
            vv = (_j(v["tech_json"], None) or {}).get("video") or {}
            if vv:
                vtech.setdefault(v["asset_id"], {})[v["key"]] = {k: vv.get(k) for k in ("width", "height", "fps", "scan", "codec")}
    px, sb = _proxied(ids), _storyboarded()
    out = []
    for r in rows:
        d = _asset_dict(r, files.get(r["id"], []), r["id"] in px, r["id"] in sb)
        d["versions"] = [ORIGINAL] + keys.get(r["id"], [])
        # Format vidéo de chaque VERSION (hors original, qui est `tech`) : un player choisit et
        # signale la version au format de sa sortie sans ouvrir la fiche de chaque média.
        d["version_video"] = vtech.get(r["id"], {})
        d["default_version"] = _effective_default(r["default_version"], keys.get(r["id"], []),
                                                  any(f["version"] == ORIGINAL for f in d["copies"]))
        out.append(d)
    return out


def _storyboarded():
    """Médias qui ont une PLANCHE d'images (app/media_storyboard.py) : une lecture du dossier pour
    toute la liste, jamais une requête par ligne."""
    from .media_storyboard import ids as _sb_ids
    return _sb_ids()


def _proxied(ids=None):
    """Médias ayant un proxy PRÉSENT (sur au moins un nœud)."""
    rows = db().execute("SELECT DISTINCT asset_id FROM media_files WHERE kind='proxy' AND state='present'").fetchall()
    out = {r["asset_id"] for r in rows}
    return out if ids is None else out & set(ids)


# ─── Bins ───────────────────────────────────────────────────────────────────────────────────
def list_bins(project_id=None, scope=None):
    """Bins globaux + ceux du projet. `smart` = recherche enregistrée ; `count` évalué maintenant.
    Portée `scope` : bins visibles seulement, et compteurs bornés aux médias de la portée."""
    c = db()
    # Bins partagés avec l'utilisateur : listés quel que soit leur projet (app/media_sharing.py).
    rows = c.execute("SELECT * FROM media_bins WHERE project_id IS NULL OR project_id=? "
                     "OR id IN (SELECT value FROM json_each(?)) ORDER BY position, name",
                     (project_id if project_id is not None else -1,
                      json.dumps(sorted(getattr(scope, "bins", None) or {})))).fetchall()
    vis = _visible_bin_ids(scope) if scope is not None else None
    ssql, sargs = scope_sql(scope, "i", "asset_id")
    out = []
    for r in rows:
        if vis is not None and r["id"] not in vis:
            continue
        smart = bool(r["query_json"])
        if smart:
            try:
                n = len(search(_j(r["query_json"], {}), limit=100000, scope=scope))
            except ValueError:
                n = None
        else:
            n = c.execute("SELECT COUNT(*) FROM media_bin_items i WHERE i.bin_id=?" + (" AND " + ssql if ssql else ""),
                          [r["id"]] + sargs).fetchone()[0]
        out.append({"id": r["id"], "name": r["name"], "parent_id": r["parent_id"], "project_id": r["project_id"],
                    "smart": smart, "query": _j(r["query_json"], None), "count": n})
    return out


def save_bin(name, parent_id=None, project_id=None, query=None, bin_id=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("nom requis")
    if query is not None:
        search(query, limit=1)                  # valide la requête (champs, opérateurs) avant d'enregistrer
    c = db()
    qj = json.dumps(query, ensure_ascii=False) if query else None
    if bin_id:
        c.execute("UPDATE media_bins SET name=?, parent_id=?, query_json=? WHERE id=?",
                  (name, parent_id, qj, int(bin_id)))
    else:
        cur = c.execute("INSERT INTO media_bins(name, parent_id, project_id, query_json, created_at) VALUES (?,?,?,?,?)",
                        (name, parent_id, project_id, qj, _now()))
        bin_id = cur.lastrowid
    c.commit()
    return int(bin_id)


def delete_bin(bin_id):
    """Supprime le bin (et ses sous-bins) — jamais les médias qu'il contient."""
    c = db()
    c.execute("DELETE FROM media_bins WHERE id=?", (int(bin_id),))
    c.commit()


def bin_add(bin_id, asset_ids, on=True):
    c = db()
    r = c.execute("SELECT query_json FROM media_bins WHERE id=?", (int(bin_id),)).fetchone()
    if not r:
        raise ValueError("bin inconnu")
    if r["query_json"]:
        raise ValueError("bin intelligent : son contenu vient de sa recherche")
    for aid in asset_ids:
        if on:
            c.execute("INSERT OR IGNORE INTO media_bin_items(bin_id, asset_id, added_at) VALUES (?,?,?)",
                      (int(bin_id), aid, _now()))
        else:
            c.execute("DELETE FROM media_bin_items WHERE bin_id=? AND asset_id=?", (int(bin_id), aid))
    c.commit()


def bin_subtree(bin_id):
    """Ids du bin et de tous ses descendants (parent avant enfants)."""
    kids = {}
    for r in db().execute("SELECT id, parent_id FROM media_bins"):
        kids.setdefault(r["parent_id"], []).append(r["id"])
    out, stack = [], [int(bin_id)]
    while stack:
        x = stack.pop(0)
        if x in out:
            continue
        out.append(x)
        stack += kids.get(x, [])
    return out


def bin_assets(bin_id, extra=None, children=False, scope=None):
    """Contenu d'un bin : sa recherche réévaluée MAINTENANT (bin intelligent), ou ses médias.
    `extra` : recherche supplémentaire appliquée au contenu (filtre dans un bin). `children` :
    le contenu de ses sous-bins aussi (manuels et intelligents), sans doublon."""
    c = db()
    r = c.execute("SELECT * FROM media_bins WHERE id=?", (int(bin_id),)).fetchone()
    if not r:
        raise ValueError("bin inconnu")
    vis = _visible_bin_ids(scope) if scope is not None else None
    if vis is not None and int(bin_id) not in vis:
        raise ValueError("bin inconnu")
    if r["query_json"] and not children:
        # Recherche du bin ET filtre, chacun avec son arbre (l'ancienne fusion des listes all/any
        # perdait les `any` du filtre dès que le bin en avait).
        return search(_j(r["query_json"], {}), also=extra or None, scope=scope)
    ids = set()
    for bid in (bin_subtree(bin_id) if children else [int(bin_id)]):
        b = c.execute("SELECT query_json FROM media_bins WHERE id=?", (bid,)).fetchone()
        if b and b["query_json"]:
            try:
                ids |= {a["id"] for a in search(_j(b["query_json"], {}), limit=100000)}
            except ValueError:
                continue                 # recherche d'un sous-bin devenue invalide (champ retiré)
        else:
            ids |= {x["asset_id"] for x in c.execute("SELECT asset_id FROM media_bin_items WHERE bin_id=?", (bid,))}
    found = search(extra or {}, limit=100000, scope=scope)
    return [a for a in found if a["id"] in ids]


# ─── Envoi vers un conteneur (Player, Images fixes) ─────────────────────────────────────────
# Ce qu'on peut envoyer, par type : action → (endpoint, construction du corps). Le chemin passé est
# celui que voit le conteneur (racine média = volume du nœud, ou dossier de son projet).
SEND_ACTIONS = {
    "player": {"load": "/file", "playlist": "/pl_add"},
    "stills": {"show": "/select"},
    "transcoder": {"transcode": "/submit"},
}


def resolve_for_container(vmid, asset_ids=(), items=(), scope=None, version=None):
    """Médias → chemins vus par le conteneur : la copie de la version PAR DÉFAUT de chaque média, ou
    de `version` (une clé que le média n'a pas : il est compté sans copie, en le disant). `items` =
    [{node_id, path}] (fichiers d'un volume, même hors base). Renvoie (chemins, [noms sans copie
    accessible], {chemin: identité du média}).
    Portée `scope` : un média hors portée (identité, ou copie d'un fichier de la base) est refusé
    comme « inconnu », sans son nom — l'envoyer le ferait ENTRER dans le projet du conteneur
    (link_container_project), donc le rendrait visible."""
    node_id, prefix = container_view(vmid)
    c = db()
    paths, missing = [], []
    origins = {}                 # chemin → identité du média (filiation d'une sortie du transcodeur)
    for aid in asset_ids:
        if scope is not None and not asset_visible(aid, scope):
            missing.append("média inconnu")
            continue
        r = c.execute("SELECT name FROM media_assets WHERE id=?", (aid,)).fetchone()
        try:
            v = resolve_version(aid, version)
        except ValueError:
            missing.append("%s (%s ?)" % (r["name"] if r else aid, version))
            continue
        lp = local_path(_files_of(aid), node_id, prefix, version=v)
        if lp:
            paths.append(lp)
            origins[lp] = aid
        else:
            missing.append(r["name"] if r else aid)
    for it in items:
        nid, p = it.get("node_id"), it.get("path") or ""
        if scope is not None:
            # Fichier d'un média de la base hors portée : refusé même s'il est dans le champ du conteneur.
            r = c.execute("SELECT asset_id FROM media_files WHERE node_id=? AND path=? AND asset_id IS NOT NULL AND kind='main'",
                          (nid, p)).fetchone()
            if r and not asset_visible(r["asset_id"], scope):
                missing.append("média inconnu")
                continue
        if int(nid or 0) == int(node_id or -1) and p.startswith(prefix):
            paths.append(p[len(prefix):])
            continue
        # Fichier d'un autre nœud : la copie locale de la MÊME version que lui.
        r = c.execute("SELECT asset_id, version FROM media_files WHERE node_id=? AND path=? AND asset_id IS NOT NULL AND kind='main'",
                      (nid, p)).fetchone()
        lp = local_path(_files_of(r["asset_id"]), node_id, prefix, version=r["version"]) if r else None
        if lp:
            paths.append(lp)
            origins[lp] = r["asset_id"]
        else:
            missing.append(os.path.basename(p))
    return paths, missing, origins


def send_targets(user_can):
    """Conteneurs Player / Images fixes DÉMARRÉS, que l'utilisateur peut piloter (`user_can(vmid)`)."""
    import json as _json
    from .database import db_get_containers, db_get_nodes
    names = {n["id"]: n.get("name") for n in db_get_nodes()}
    out = []
    for ct in db_get_containers():
        try:
            dc = _json.loads(ct.get("deploy_config") or "{}")
        except (TypeError, ValueError):
            continue
        t = dc.get("type")
        if t not in SEND_ACTIONS or (ct.get("status") or "") != "running" or not user_can(ct["vmid"]):
            continue
        # `uuid` : identité d'INSTANCE (survit au recreate) — un choix mémorisé (double-clic du
        # Gestionnaire) la vise, pas le vmid, handle jetable.
        out.append({"vmid": ct["vmid"], "uuid": ct.get("instance_uuid"), "hostname": ct.get("hostname"), "type": t,
                    "node": names.get(ct.get("node_id")), "actions": list(SEND_ACTIONS[t])})
    return sorted(out, key=lambda x: (x["type"], x["hostname"] or ""))


def send_to(vmid, action, paths, params=None, origins=None, user=""):
    """Poste au conteneur (contrôle :8082). Player : `load` charge le premier (calé sur sa première
    image), `playlist` ajoute tout ; Images fixes : `show` affiche le premier."""
    import json as _json
    import requests
    from .database import db_get_container
    from .addressing import get_container_ip
    ct = db_get_container(int(vmid)) or {}
    t = (_json.loads(ct.get("deploy_config") or "{}") or {}).get("type")
    ep = (SEND_ACTIONS.get(t) or {}).get(action)
    if not ep:
        raise ValueError("action « %s » impossible sur %s" % (action, t or "ce conteneur"))
    ip = get_container_ip(int(vmid))
    if not ip:
        raise RuntimeError("conteneur injoignable")
    if action == "transcode":
        # Un travail par média ; le préréglage vient du Gestionnaire (`params.preset`).
        # `_origin` : le média source et qui demande — le transcodeur les recopie dans le sidecar
        # de sa sortie (filiation, « soumis par »).
        bodies = [{"type": "video", "input": p, "params": {"preset": (params or {}).get("preset"),
                   "_origin": {"asset_id": (origins or {}).get(p) or "", "user": user}}} for p in paths]
    else:
        bodies = ([{"file": paths[0], "cue": True}] if action == "load"
                  else [{"file": p} for p in paths] if action == "playlist"
                  else [{"file": paths[0], "folder": os.path.dirname(paths[0])}])
    for b in bodies:
        r = requests.post("http://%s:8082%s" % (ip, ep), json=b, timeout=10)
        if r.status_code >= 400:
            raise RuntimeError("%s a refusé : %s" % (ct.get("hostname"), r.text[:200]))
    for p in paths:
        if (origins or {}).get(p):
            link_container_project(origins[p], vmid)
    return len(bodies)


# ─── Lecture ────────────────────────────────────────────────────────────────────────────────
def _asset_dict(r, files=None, has_proxy=None, has_storyboard=None):
    d = {"id": r["id"], "name": r["name"], "created_at": r["created_at"], "updated_at": r["updated_at"],
         "origin": r["origin"], "producer": r["producer"], "fingerprint": r["fingerprint"], "size": r["size"],
         "derived_from": r["derived_from"], "relation": r["relation"],
         "tech": _j(r["tech_json"], None), "meta": _j(r["meta_json"], {}),
         "thumb": {"t": r["thumb_t"], "custom": bool(r["thumb_custom"]), "v": r["thumb_v"]}}
    if files is not None:
        d["copies"] = files
    if has_proxy is not None:
        d["has_proxy"] = bool(has_proxy)
    if has_storyboard is not None:
        d["has_storyboard"] = bool(has_storyboard)
    return d


def _share_names():
    try:
        from .database import get_db
        return {r["id"]: r["name"] for r in get_db().execute("SELECT id, name FROM storage_connections")}
    except sqlite3.Error:
        return {}


def _files_of(aid, kind="main", version=None):
    """Copies du média (`kind` = main), ou ses proxys (`kind` = proxy) — jamais les deux mêlés :
    tout ce qui compte, choisit ou envoie une copie lit les copies principales seulement. Chaque
    copie dit sa `version` ; `version` donnée : les copies de cette version seulement."""
    from .database import db_get_nodes
    names = {n["id"]: n.get("name") for n in db_get_nodes()}
    shares = _share_names()
    fps = {ORIGINAL: _asset_fp(aid)}
    fps.update({r["key"]: r["fingerprint"] for r in db().execute(
        "SELECT key, fingerprint FROM media_versions WHERE asset_id=?", (aid,))})
    return [{"node_id": f["node_id"], "share_id": f["share_id"], "version": f["version"] or ORIGINAL,
             "location": "share" if f["share_id"] else "node",
             "node": (shares.get(f["share_id"], "partage #%s" % f["share_id"]) if f["share_id"]
                      else names.get(f["node_id"], "#%s" % f["node_id"])),
             "path": f["path"], "size": f["size"], "kind": f["kind"], "state": f["state"],
             "growing": bool(f["growing"]), "seen_at": f["seen_at"], "verified_at": f["verified_at"],
             "fingerprint_ok": (f["fingerprint"] is None or fps.get(f["version"] or ORIGINAL) is None
                                or f["fingerprint"] == fps.get(f["version"] or ORIGINAL)),
             # Proxy : le job de la ferme qui l'a produit, et où (transcoder, nœud).
             **({"made": _j(f["process_json"], {})} if kind == "proxy" else {})}
            for f in db().execute("SELECT * FROM media_files WHERE asset_id=? AND kind=? ORDER BY share_id, node_id, path",
                                  (aid, kind))
            if version is None or (f["version"] or ORIGINAL) == version]


def _asset_fp(aid):
    r = db().execute("SELECT fingerprint FROM media_assets WHERE id=?", (aid,)).fetchone()
    return r["fingerprint"] if r else None


# ─── Versions d'un média ────────────────────────────────────────────────────────────────────
# Cf. l'en-tête. L'ORIGINAL n'a pas de ligne dans `media_versions` : il est la fiche elle-même
# (empreinte, fiche technique, taille de `media_assets`) — une seule source pour la référence.
def find_by_fingerprint(fp, c=None):
    """(identité, version) du contenu d'empreinte `fp` : l'original d'un média, sinon une de ses
    versions ; (None, None) si inconnu. La règle d'identification de l'envoi, de l'import et de
    l'analyse : un contenu identique à une version est une copie de plus de CETTE version."""
    if not fp:
        return None, None
    c = c or db()
    r = c.execute("SELECT id FROM media_assets WHERE fingerprint=? ORDER BY created_at LIMIT 1", (fp,)).fetchone()
    if r:
        return r["id"], ORIGINAL
    r = c.execute("SELECT asset_id, key FROM media_versions WHERE fingerprint=? ORDER BY created_at LIMIT 1", (fp,)).fetchone()
    return (r["asset_id"], r["key"]) if r else (None, None)


def _version_fp(aid, version=None):
    """Empreinte de référence d'une version (celle de la fiche pour l'original)."""
    if version in (None, "", ORIGINAL):
        return _asset_fp(aid)
    r = db().execute("SELECT fingerprint FROM media_versions WHERE asset_id=? AND key=?", (aid, version)).fetchone()
    return r["fingerprint"] if r else None


def _effective_default(stored, keys, original_has_files):
    """Version par défaut : celle choisie (si elle existe encore), sinon l'original — sauf une fiche
    née d'une version seule (original jamais vu) : sa première version."""
    if stored and stored in keys:
        return stored
    if original_has_files or not keys:
        return ORIGINAL
    return keys[0]


def _version_keys(aid):
    return [r["key"] for r in db().execute("SELECT key FROM media_versions WHERE asset_id=? ORDER BY created_at, key", (aid,))]


def default_version(aid):
    """Version que charge « Charger ce média » (player, rapatriement, envoi, macros)."""
    c = db()
    r = c.execute("SELECT default_version FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not r:
        return ORIGINAL
    orig = c.execute("SELECT 1 FROM media_files WHERE asset_id=? AND kind='main' AND version=? LIMIT 1",
                     (aid, ORIGINAL)).fetchone() is not None
    return _effective_default(r["default_version"], _version_keys(aid), orig)


def resolve_version(aid, version=None):
    """Clé de version demandée → clé existante. Vide ou « default » : la version par défaut ;
    ValueError pour une version que ce média n'a pas."""
    v = str(version or "").strip().lower()
    if v in ("", "default"):
        return default_version(aid)
    if v == ORIGINAL:
        return ORIGINAL
    if db().execute("SELECT 1 FROM media_versions WHERE asset_id=? AND key=?", (aid, v)).fetchone():
        return v
    raise ValueError("version inconnue pour ce média : %s" % version)


def _ensure_version(aid, key, process=None, origin=None, producer=None, fingerprint=None, size=None, tech=None,
                    replace=False):
    """Crée la version `key` du média `aid` (elle existe dès sa première copie vue). Existante :
    complétée (ce qui manquait) — ou, `replace`, prend le contenu neuf (refaite par son producteur)."""
    if key in (None, "", ORIGINAL):
        return
    if not media_naming.valid_version(key):
        raise ValueError("clé de version invalide : %s" % key)
    proc = process if isinstance(process, dict) else None
    label = (proc or {}).get("version_label") or None
    preset = (proc or {}).get("preset") or None
    tj = json.dumps(tech) if tech else None
    pj = json.dumps(proc, ensure_ascii=False) if proc else None
    now = _now()
    upd = ("label=COALESCE(excluded.label, label), preset=COALESCE(excluded.preset, preset), "
           "fingerprint=COALESCE(excluded.fingerprint, fingerprint), size=COALESCE(excluded.size, size), "
           "tech_json=COALESCE(excluded.tech_json, tech_json), process_json=COALESCE(excluded.process_json, process_json), "
           "producer=COALESCE(excluded.producer, producer), updated_at=excluded.updated_at") if replace else (
           "label=COALESCE(label, excluded.label), preset=COALESCE(preset, excluded.preset), "
           "fingerprint=COALESCE(fingerprint, excluded.fingerprint), size=COALESCE(size, excluded.size), "
           "tech_json=COALESCE(tech_json, excluded.tech_json), process_json=COALESCE(process_json, excluded.process_json), "
           "producer=COALESCE(producer, excluded.producer)")
    db().execute("""INSERT INTO media_versions(asset_id, key, label, preset, origin, producer, fingerprint, size,
                        tech_json, process_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(asset_id, key) DO UPDATE SET """ + upd,
                 (aid, key, label, preset, origin, producer, fingerprint, size, tj, pj, now, now))


def versions_of(aid, files=None):
    """Versions de la fiche : l'original, puis les autres dans l'ordre de leur création — chacune
    avec sa fiche technique, sa taille, son empreinte, son traitement, ses copies et les nœuds où
    elle est présente ; `default` marque la version par défaut."""
    c = db()
    a = c.execute("SELECT * FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not a:
        return []
    files = _files_of(aid) if files is None else files
    out = [{"key": ORIGINAL, "label": None, "preset": None, "origin": a["origin"], "producer": a["producer"],
            "fingerprint": a["fingerprint"], "size": a["size"], "tech": _j(a["tech_json"], None), "process": None,
            "created_at": a["created_at"]}]
    for v in c.execute("SELECT * FROM media_versions WHERE asset_id=? ORDER BY created_at, key", (aid,)):
        out.append({"key": v["key"], "label": v["label"], "preset": v["preset"], "origin": v["origin"],
                    "producer": v["producer"], "fingerprint": v["fingerprint"], "size": v["size"],
                    "tech": _j(v["tech_json"], None), "process": _j(v["process_json"], None), "created_at": v["created_at"]})
    dflt = _effective_default(a["default_version"], [v["key"] for v in out[1:]],
                              any(f.get("version", ORIGINAL) == ORIGINAL for f in files))
    for v in out:
        cps = [f for f in files if f.get("version", ORIGINAL) == v["key"]]
        v["copies"] = cps
        v["present"] = sum(1 for f in cps if f["state"] == "present")
        v["nodes"] = sorted({f["node"] for f in cps if f["state"] == "present"})
        v["default"] = v["key"] == dflt
        v["original"] = v["key"] == ORIGINAL
    return out


def set_default_version(aid, version):
    """Choisit la version par défaut (l'original : retour au cas général)."""
    c = db()
    if not c.execute("SELECT 1 FROM media_assets WHERE id=?", (aid,)).fetchone():
        raise ValueError("média inconnu")
    key = resolve_version(aid, version or ORIGINAL)
    c.execute("UPDATE media_assets SET default_version=?, updated_at=? WHERE id=?",
              (None if key == ORIGINAL else key, _now(), aid))
    c.commit()
    return key


def delete_version(aid, version, with_files=True):
    """Supprime UNE version : ses copies sur les nœuds (fichier + sidecar), puis la version. Les
    copies sur partage réseau ne sont jamais touchées (seulement oubliées). L'original ne se
    supprime pas ici : c'est la fiche (« Supprimer le média »)."""
    key = resolve_version(aid, version)
    if key == ORIGINAL:
        raise ValueError("l'original ne se supprime pas seul : supprimer le média")
    c = db()
    if with_files:
        for f in c.execute("""SELECT node_id, path FROM media_files WHERE asset_id=? AND version=? AND kind='main'
                              AND share_id=0 AND state='present'""", (aid, key)).fetchall():
            delete_copy(aid, f["node_id"], f["path"])
    c.execute("DELETE FROM media_files WHERE asset_id=? AND version=? AND kind='main'", (aid, key))
    c.execute("DELETE FROM media_versions WHERE asset_id=? AND key=?", (aid, key))
    c.execute("UPDATE media_assets SET default_version=NULL, updated_at=? WHERE id=? AND default_version=?",
              (_now(), aid, key))
    c.commit()
    return key


def list_assets(project_id=None, q=None, limit=2000, scope=None):
    c = db()
    sql = "SELECT a.* FROM media_assets a"
    args, where = [], []
    if project_id is not None:
        sql += " JOIN project_media p ON p.asset_id=a.id AND p.project_id=?"
        args.append(int(project_id))
    if q:
        where.append("(a.name LIKE ? OR a.meta_json LIKE ?)")
        args += ["%" + q + "%", "%" + q + "%"]
    ssql, sargs = scope_sql(scope)
    if ssql:
        where.append(ssql)
        args += sargs
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.created_at DESC LIMIT ?"
    args.append(int(limit))
    return _with_copies(c.execute(sql, args).fetchall())


def get_asset(aid, scope=None):
    """Fiche complète. Portée `scope` : None si le média est hors portée ; ses projets, sa source
    et ses dérivés sont bornés à la portée (le nom d'un média invisible n'est jamais rendu)."""
    if scope is not None and not asset_visible(aid, scope):
        return None
    r = db().execute("SELECT * FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not r:
        return None
    d = _asset_dict(r, _files_of(aid))
    d["versions"] = versions_of(aid, d["copies"])
    d["default_version"] = next(v["key"] for v in d["versions"] if v["default"])
    # Proxys de lecture (instances `kind=proxy` du même média) et générations en file / en cours.
    try:
        _proxy_sync(aid)
    except Exception as e:                                                 # noqa: BLE001
        log.warning("proxys de %s : %s", aid, e)
    d["proxies"] = _files_of(aid, "proxy")
    d["has_proxy"] = any(p["state"] == "present" for p in d["proxies"])
    d["proxy_jobs"] = proxy_jobs(asset_id=aid)
    # Planche d'images (mode « Pellicule ») : sa description, et les jobs qui la font.
    from . import media_storyboard as _sbm
    d["storyboard"] = _sbm.get(aid)
    d["has_storyboard"] = d["storyboard"] is not None
    d["storyboard_jobs"] = _sbm.jobs(asset_id=aid, limit=10)
    d["projects"] = [x["project_id"] for x in db().execute(
        "SELECT project_id FROM project_media WHERE asset_id=?", (aid,)) if project_in_scope(x["project_id"], scope)]
    # Filiation dans les deux sens, et le traitement qui a produit ce média.
    d["process"] = _j(r["process_json"], None)
    s2 = db().execute("SELECT id, name FROM media_assets WHERE id=?", (r["derived_from"],)).fetchone() if r["derived_from"] else None
    if s2 and scope is not None and not asset_visible(s2["id"], scope):
        s2 = None                                  # source hors portée : ni son nom, ni son identité
        d["derived_from"] = None
    d["source"] = {"id": s2["id"], "name": s2["name"]} if s2 else ({"id": r["derived_from"], "name": None} if d["derived_from"] else None)
    der = db().execute("SELECT id, name, relation, process_json FROM media_assets WHERE derived_from=? ORDER BY created_at",
                       (aid,)).fetchall()
    vis = set(visible_ids([x["id"] for x in der], scope))
    d["derived"] = [{"id": x["id"], "name": x["name"], "relation": x["relation"],
                     "preset": (_j(x["process_json"], {}) or {}).get("preset")}
                    for x in der if x["id"] in vis]
    return d


def update_meta(aid, meta, name=None):
    """Champs de base et personnalisés. Une clé à None est retirée."""
    c = db()
    r = c.execute("SELECT meta_json FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not r:
        return None
    cur = _j(r["meta_json"], {})
    allowed = set(BASE_FIELDS) | {f["key"] for f in list_fields()}
    for k, v in (meta or {}).items():
        if k not in allowed:
            continue
        if v is None or v == "":
            cur.pop(k, None)
        else:
            cur[k] = v
    sets, args = ["meta_json=?", "updated_at=?"], [json.dumps(cur, ensure_ascii=False), _now()]
    if name and str(name).strip():
        sets.append("name=?")
        args.append(str(name).strip())
    c.execute("UPDATE media_assets SET %s WHERE id=?" % ", ".join(sets), args + [aid])
    c.commit()
    return get_asset(aid)


# ─── Champs personnalisés (Réglages) ────────────────────────────────────────────────────────
def list_fields():
    return [{"key": r["key"], "label": r["label"], "type": r["type"], "options": _j(r["options"], []),
             "position": r["position"]}
            for r in db().execute("SELECT * FROM media_fields ORDER BY position, label")]


def save_field(key, label, type_, options=None, position=0):
    import re
    key = (key or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key) or key in BASE_FIELDS:
        raise ValueError("clé invalide (minuscules, chiffres, _ ; pas title/description/keywords)")
    if type_ not in FIELD_TYPES:
        raise ValueError("type inconnu : %s" % type_)
    if not (label or "").strip():
        raise ValueError("libellé requis")
    c = db()
    c.execute("""INSERT INTO media_fields(key, label, type, options, position) VALUES (?,?,?,?,?)
                 ON CONFLICT(key) DO UPDATE SET label=excluded.label, type=excluded.type,
                    options=excluded.options, position=excluded.position""",
              (key, label.strip(), type_, json.dumps(options or []), int(position or 0)))
    c.commit()


def delete_field(key):
    """Retire la DÉFINITION du champ. Les valeurs déjà saisies restent dans les médias (elles
    réapparaissent si le champ est recréé) : supprimer un champ ne doit pas détruire de données."""
    c = db()
    c.execute("DELETE FROM media_fields WHERE key=?", (key,))
    c.commit()


# ─── Vignettes (centrales, sur l'orchestrateur) ─────────────────────────────────────────────
def thumb_path(aid):
    return os.path.join(THUMB_DIR, "%s.jpg" % aid)


def _thumb_store(aid, b64):
    import base64
    os.makedirs(THUMB_DIR, exist_ok=True)
    p = thumb_path(aid)
    with open(p + ".tmp", "wb") as f:
        f.write(base64.b64decode(b64))
    os.replace(p + ".tmp", p)
    c = db()
    c.execute("UPDATE media_assets SET thumb_v=? WHERE id=?", ("%x" % int(time.time() * 1000), aid))
    c.commit()


def _present_copy(aid, prefer_node=None):
    # L'ORIGINAL d'abord (la vignette d'un média est celle de sa référence), puis le nœud préféré.
    rows = db().execute("""SELECT node_id, path FROM media_files WHERE asset_id=? AND state='present'
                           AND growing=0 AND share_id=0 AND kind='main' ORDER BY (version='original') DESC, (node_id=?) DESC""",
                        (aid, prefer_node or -1)).fetchall()
    return (rows[0]["node_id"], rows[0]["path"]) if rows else (None, None)


def set_thumb(aid, t=None, reset=False):
    """Vignette à l'instant t (secondes) ; `reset` revient à l'instant par défaut. Calculée sur un
    nœud qui porte une copie, stockée ici."""
    c = db()
    r = c.execute("SELECT tech_json FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not r:
        raise ValueError("média inconnu")
    dur = (_j(r["tech_json"], {}) or {}).get("duration") or 0
    if reset or t is None:
        t, custom = (min(5.0, dur * 0.1) if dur else 1.0), 0
    else:
        t, custom = max(0.0, float(t)), 1
    nid, path = _present_copy(aid)
    if nid is None:
        raise RuntimeError("aucune copie présente : vignette impossible à calculer")
    c.execute("UPDATE media_assets SET thumb_t=?, thumb_custom=?, updated_at=? WHERE id=?", (t, custom, _now(), aid))
    c.commit()
    res = _stamp_pending(nid, thumbs=[(aid, path, t)])
    if not (res.get("thumbs") or {}).get(aid):
        raise RuntimeError("image impossible à extraire à %.2f s" % t)
    return get_asset(aid)


def frame(aid, t, width=480):
    """Image du média à l'instant t, calculée sur un nœud qui porte une copie, NON stockée (choix
    de la vignette hors d'un conteneur qui voit le fichier). Octets JPEG, ou None."""
    import base64
    nid, path = _present_copy(aid)
    if nid is None:
        return None
    res = _run_on_node(_node(nid), {"mode": "stamp", "thumbs": [{"id": aid, "path": path, "t": float(t), "w": int(width)}]},
                       timeout=60)
    b64 = (res.get("thumbs") or {}).get(aid)
    return base64.b64decode(b64) if b64 else None


def ensure_thumb(aid):
    """Chemin de la vignette, calculée si absente. None si impossible."""
    p = thumb_path(aid)
    if os.path.exists(p):
        return p
    r = db().execute("SELECT thumb_t FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not r:
        return None
    nid, path = _present_copy(aid)
    if nid is None:
        return None
    try:
        _stamp_pending(nid, thumbs=[(aid, path, r["thumb_t"] or 1.0)])
    except Exception as e:                                                 # noqa: BLE001
        log.warning("vignette %s : %s", aid, e)
    return p if os.path.exists(p) else None


# ─── Projets ────────────────────────────────────────────────────────────────────────────────
def link_project(aid, project_id, on=True):
    c = db()
    if on:
        c.execute("INSERT OR IGNORE INTO project_media(project_id, asset_id, added_at) VALUES (?,?,?)",
                  (int(project_id), aid, _now()))
    else:
        c.execute("DELETE FROM project_media WHERE project_id=? AND asset_id=?", (int(project_id), aid))
    c.commit()


def link_container_project(aid, vmid):
    """Un média chargé, rapatrié ou envoyé par un conteneur d'un projet ENTRE dans ce projet : c'est
    l'usage qui fait l'appartenance, sans geste de plus."""
    from .database import db_get_container
    from . import media_sharing
    if media_sharing.link_suppressed(aid, vmid):
        return                   # média vu par un PARTAGE seulement : chargé, pas rattaché (il en sortirait)
    try:
        pid = (db_get_container(int(vmid)) or {}).get("project_id")
    except (TypeError, ValueError):
        return
    if pid and aid:
        link_project(aid, pid)


# ─── Projets : les médias voyagent avec le projet ───────────────────────────────────────────
# Un projet exporté emporte la FICHE de chacun de ses médias (identité, empreinte, métadonnées,
# vignette, filiation, traitement) ; sur un autre serveur, ces médias entrent dans la base et
# retrouvent leurs copies à l'analyse (identité du sidecar, sinon empreinte). Sans copie : hors
# ligne, mais avec toute leur fiche. Option « embarquer les médias » : les fichiers eux-mêmes,
# dans une archive lue directement sur les nœuds.
_ASSET_COLS = ("id", "name", "created_at", "updated_at", "origin", "fingerprint", "size", "tech_json", "meta_json",
               "thumb_t", "thumb_custom", "thumb_v", "producer", "derived_from", "relation", "process_json",
               "default_version")
_ID_OK = __import__("re").compile(r"^[0-9A-Za-z_-]{6,64}$")


def project_export_media(pid):
    """{"media": [fiches], "media_fields": [définitions des champs personnalisés utilisés]}."""
    import base64
    out, keys = [], set()
    for r in db().execute("""SELECT a.* FROM media_assets a JOIN project_media p ON p.asset_id=a.id
                             WHERE p.project_id=? ORDER BY a.name""", (int(pid),)):
        rec = {k: r[k] for k in _ASSET_COLS if k not in ("tech_json", "meta_json", "process_json")}
        rec["tech"] = _j(r["tech_json"], None)
        rec["meta"] = _j(r["meta_json"], {})
        rec["process"] = _j(r["process_json"], None)
        # Ses VERSIONS voyagent avec la fiche (les copies, elles, se retrouvent à l'analyse).
        rec["versions"] = [{"key": v["key"], "label": v["label"], "preset": v["preset"], "origin": v["origin"],
                            "producer": v["producer"], "fingerprint": v["fingerprint"], "size": v["size"],
                            "tech": _j(v["tech_json"], None), "process": _j(v["process_json"], None),
                            "created_at": v["created_at"]}
                           for v in db().execute("SELECT * FROM media_versions WHERE asset_id=? ORDER BY created_at, key",
                                                 (r["id"],))]
        keys |= set(rec["meta"])
        if os.path.exists(thumb_path(r["id"])):
            with open(thumb_path(r["id"]), "rb") as f:
                rec["thumb_jpg"] = base64.b64encode(f.read()).decode()
        out.append(rec)
    return {"media": out, "media_fields": [f for f in list_fields() if f["key"] in keys]}


def project_import_media(pid, records, fields=()):
    """Fait entrer les fiches d'un projet importé dans la base et les rattache au projet `pid`.
    Un média déjà connu (même identité, sinon même empreinte) n'est pas écrasé : sa fiche locale
    garde la main, seuls les champs VIDES sont complétés. Renvoie {new, known, skipped, ids}."""
    c = db()
    n = {"new": 0, "known": 0, "skipped": 0, "ids": {}}
    for f in fields or ():
        if isinstance(f, dict) and f.get("key") and f.get("label") and f.get("type"):
            c.execute("INSERT OR IGNORE INTO media_fields(key, label, type, options, position) VALUES (?,?,?,?,?)",
                      (str(f["key"]), str(f["label"]), str(f["type"]), json.dumps(f.get("options") or []),
                       int(f.get("position") or 0)))
    now = _now()
    for rec in records or ():
        aid = str((rec or {}).get("id") or "")
        if not _ID_OK.match(aid):
            n["skipped"] += 1
            continue
        row = c.execute("SELECT * FROM media_assets WHERE id=?", (aid,)).fetchone()
        if not row and rec.get("fingerprint"):
            row = c.execute("SELECT * FROM media_assets WHERE fingerprint=?", (rec["fingerprint"],)).fetchone()
        meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
        if row:
            cur = _j(row["meta_json"], {})
            merged = dict(meta, **{k: v for k, v in cur.items() if v not in (None, "")})
            c.execute("""UPDATE media_assets SET meta_json=?, tech_json=COALESCE(tech_json, ?),
                            process_json=COALESCE(process_json, ?), derived_from=COALESCE(derived_from, ?),
                            relation=COALESCE(relation, ?), producer=COALESCE(producer, ?),
                            fingerprint=COALESCE(fingerprint, ?), size=COALESCE(size, ?), updated_at=? WHERE id=?""",
                      (json.dumps(merged, ensure_ascii=False),
                       json.dumps(rec["tech"]) if rec.get("tech") else None,
                       json.dumps(rec["process"], ensure_ascii=False) if rec.get("process") else None,
                       rec.get("derived_from"), rec.get("relation"), rec.get("producer"),
                       rec.get("fingerprint"), rec.get("size"), now, row["id"]))
            aid = row["id"]
            n["known"] += 1
        else:
            c.execute("""INSERT INTO media_assets(id, name, created_at, updated_at, origin, fingerprint, size, tech_json,
                            meta_json, thumb_t, thumb_custom, thumb_v, producer, derived_from, relation, process_json)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (aid, str(rec.get("name") or aid), rec.get("created_at") or now, now, rec.get("origin") or "import",
                       rec.get("fingerprint"), rec.get("size"), json.dumps(rec["tech"]) if rec.get("tech") else None,
                       json.dumps(meta, ensure_ascii=False), rec.get("thumb_t"), 1 if rec.get("thumb_custom") else 0,
                       rec.get("thumb_v"), rec.get("producer"), rec.get("derived_from"), rec.get("relation"),
                       json.dumps(rec["process"], ensure_ascii=False) if rec.get("process") else None))
            n["new"] += 1
        for v in rec.get("versions") or ():
            if isinstance(v, dict) and media_naming.valid_version(v.get("key")):
                c.execute("""INSERT OR IGNORE INTO media_versions(asset_id, key, label, preset, origin, producer, fingerprint,
                                size, tech_json, process_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (aid, v["key"], v.get("label"), v.get("preset"), v.get("origin"), v.get("producer"),
                           v.get("fingerprint"), v.get("size"), json.dumps(v["tech"]) if v.get("tech") else None,
                           json.dumps(v["process"], ensure_ascii=False) if v.get("process") else None,
                           v.get("created_at") or now, now))
        if rec.get("default_version") and not row:
            c.execute("UPDATE media_assets SET default_version=? WHERE id=?", (rec["default_version"], aid))
        if rec.get("thumb_jpg") and not os.path.exists(thumb_path(aid)):
            try:
                _thumb_store(aid, rec["thumb_jpg"])
            except (ValueError, OSError):
                pass
        c.execute("INSERT OR IGNORE INTO project_media(project_id, asset_id, added_at) VALUES (?,?,?)", (int(pid), aid, now))
        n["ids"][str(rec.get("id"))] = aid
    c.commit()
    return n


def _project_prefix(pid):
    """Dossier du projet sous le volume média (même règle que container_view)."""
    from .database import db_get_project
    proj = db_get_project(int(pid)) or {}
    return (os.path.basename(proj["media_path"].rstrip("/")) + "/") if proj.get("media_path") else ""


def project_archive_plan(pid):
    """Pour l'option « embarquer les médias » : UNE copie présente par média (nœud dont l'agent
    sert le transfert d'abord), le chemin dans l'archive, et le sidecar (lu en entier, il est
    petit). Les médias sans copie présente ne sont pas embarqués (ils arriveront hors ligne)."""
    from . import node_driver
    prefix = _project_prefix(pid)
    plan, nodes = [], {}
    for a in db().execute("SELECT a.id FROM media_assets a JOIN project_media p ON p.asset_id=a.id WHERE p.project_id=?",
                          (int(pid),)).fetchall():
        best = {}                # version → (score, nœud, copie) : UNE copie par version du média
        for f in db().execute("""SELECT node_id, path, size, mtime, version FROM media_files WHERE asset_id=? AND state='present'
                                 AND share_id=0 AND growing=0 AND kind='main'""", (a["id"],)):
            n = nodes.get(f["node_id"]) or nodes.setdefault(f["node_id"], _node(f["node_id"]) or {})
            if not n.get("host"):
                continue
            score = (1 if node_driver.transfer_capable(n) else 0, 1 if f["path"].startswith(prefix) else 0)
            if f["version"] not in best or score > best[f["version"]][0]:
                best[f["version"]] = (score, n, f)
        for ver, (_sc, n, f) in sorted(best.items(), key=lambda kv: (kv[0] != ORIGINAL, kv[0])):
            rel = f["path"][len(prefix):] if prefix and f["path"].startswith(prefix) else f["path"]
            # Le fichier SORT du stockage du nœud : son nom perd le code du média et la clé de version
            # (le sidecar, dans l'archive, garde l'identité et la version ; l'import les rajoute). Une
            # version est rangée sous `@<clé>/` : deux versions peuvent porter le même nom exporté.
            rel = media_naming.export_name(rel, a["id"])
            arc = "media/%s/%s%s" % (a["id"], "" if ver == ORIGINAL else "@%s/" % ver, rel)
            rc, xml, _e = node_driver.host_exec(n, "cat %s" % shlex.quote(_mount(n).rstrip("/") + "/" + f["path"] + ".xml"),
                                                timeout=20)
            plan.append({"id": a["id"], "version": ver, "node": n, "path": f["path"], "size": int(f["size"] or 0),
                         "mtime": f["mtime"], "arc": arc,
                         "sidecar": _export_sidecar(xml, f["path"], rel) if rc == 0 and xml else None})
    return plan


def _export_sidecar(xml, path, rel):
    """Sidecar dans l'archive : son `<file>` suit le nom SANS code (l'identité, elle, reste)."""
    import xml.sax.saxutils as sx
    old, new = sx.escape(os.path.basename(path)), sx.escape(os.path.basename(rel))
    return xml.replace("<file>%s</file>" % old, "<file>%s</file>" % new, 1).encode()


def _read_node_file(node, rel, size):
    """Flux des octets d'un fichier du volume d'un nœud : port de transfert (agent ≥ 0.24.0),
    sinon root-SSH (agents plus anciens)."""
    import subprocess
    from . import node_driver
    if node_driver.transfer_capable(node):
        r = node_driver.transfer_open(node, rel, size)
        if r is None:
            raise RuntimeError("%s : lecture de %s refusée" % (node.get("name"), rel))
        try:
            for b in r.iter_content(1 << 20):
                yield b
        finally:
            r.close()
        return
    proc = subprocess.Popen(["ssh"] + _SSH_OPT.split() + ["root@" + node["host"],
                            "cat " + shlex.quote(_mount(node).rstrip("/") + "/" + rel)], stdout=subprocess.PIPE)
    try:
        while True:
            b = proc.stdout.read(1 << 20)
            if not b:
                break
            yield b
    finally:
        proc.kill()
        proc.wait()


def _tar_member(name, size, mtime):
    import tarfile
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime = int(size), 0o644, int(mtime or time.time())
    return ti.tobuf(format=tarfile.PAX_FORMAT)


def _tar_pad(size):
    return b"\0" * ((512 - size % 512) % 512)


def project_archive_stream(manifest, plan):
    """Archive tar EN FLUX : `projet.bsproj.json`, puis chaque média et son sidecar. Rien n'est
    stocké sur l'orchestrateur. Un fichier qui ne fait plus la taille annoncée interrompt l'archive
    (elle serait corrompue) : le téléchargement échoue au lieu de livrer un faux."""
    body = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    yield _tar_member("projet.bsproj.json", len(body), time.time()) + body + _tar_pad(len(body))
    for it in plan:
        yield _tar_member(it["arc"], it["size"], it["mtime"])
        got = 0
        for b in _read_node_file(it["node"], it["path"], it["size"]):
            got += len(b)
            if got > it["size"]:
                raise RuntimeError("%s a changé pendant l'export" % it["path"])
            yield b
        if got != it["size"]:
            raise RuntimeError("%s : %d octets lus sur %d" % (it["path"], got, it["size"]))
        yield _tar_pad(got)
        if it["sidecar"]:
            yield _tar_member(it["arc"] + ".xml", len(it["sidecar"]), it["mtime"]) + it["sidecar"] + _tar_pad(len(it["sidecar"]))
    yield b"\0" * 1024


def _put_on_node(node, rel, fobj, size, mtime):
    """Dépose un fichier sur le volume d'un nœud : agent ≥ 0.25.0, sinon root-SSH."""
    import subprocess
    from . import node_driver
    if node_driver.upload_capable(node):
        ok, msg = node_driver.upload_file(node, rel, fobj, size, mtime=mtime)
        if not ok:
            raise RuntimeError("%s : dépôt de %s refusé : %s" % (node.get("name"), rel, msg))
        return
    d_abs = _mount(node).rstrip("/") + "/" + rel
    remote = "mkdir -p %s && cat > %s && mv -n %s %s%s" % (
        shlex.quote(os.path.dirname(d_abs)), shlex.quote(d_abs + ".partial"), shlex.quote(d_abs + ".partial"),
        shlex.quote(d_abs), " && touch -d @%d %s" % (int(mtime), shlex.quote(d_abs)) if mtime else "")
    proc = subprocess.Popen(["ssh"] + _SSH_OPT.split() + ["root@" + node["host"], remote], stdin=subprocess.PIPE)
    while True:
        b = fobj.read(1 << 20)
        if not b:
            break
        proc.stdin.write(b)
    proc.stdin.close()
    if proc.wait(timeout=600) != 0:
        raise RuntimeError("%s : dépôt de %s en échec" % (node.get("name"), rel))


_project_jobs = {}            # pid → état de l'import des fichiers d'une archive de projet


def project_job(pid):
    return dict(_project_jobs.get(int(pid)) or {})


def project_import_archive(pid, tar_path, node_id, ids):
    """Fichiers d'une archive de projet → volume du nœud choisi, dans le dossier du projet, sous
    leur nom CODÉ (code de l'identité locale du média, cf. app/media_naming.py). Rien n'est écrasé :
    la même copie déjà là n'est pas redéposée ; un autre fichier sous ce nom → code à 8 caractères. Puis analyse du nœud : les copies rejoignent leurs médias
    par l'identité du sidecar. Tâche longue, à lancer dans un fil ; l'archive est effacée à la fin."""
    import tarfile
    from . import node_driver
    node = _node(node_id)
    job = _project_jobs[int(pid)] = {"state": "running", "done": 0, "total": 0, "error": "", "node": (node or {}).get("name")}
    try:
        if not node:
            raise RuntimeError("nœud %s introuvable" % node_id)
        prefix = _project_prefix(pid)
        with tarfile.open(tar_path, "r:") as tar:
            mem = [m for m in tar.getmembers() if m.isfile() and m.name.startswith("media/")]
            job["total"] = sum(1 for m in mem if not m.name.endswith(".xml"))
            renamed = {}
            for m in mem:
                parts = m.name.split("/", 2)
                if len(parts) < 3 or parts[1] not in ids or ".." in parts[2].split("/") or parts[2].startswith("/"):
                    continue
                is_xml = parts[2].endswith(".xml")
                base = parts[2][:-4] if is_xml else parts[2]
                # `@<clé>/<nom>` : une VERSION du média (cf. project_archive_plan) — même code, sa clé en suffixe.
                ver = None
                if base.startswith("@") and "/" in base:
                    ver, base = base[1:].split("/", 1)
                    if not media_naming.valid_version(ver):
                        continue
                rel = renamed.get(m.name[:-4] if is_xml else m.name)
                if not rel:
                    # Nom codé de l'identité LOCALE du média (une archive ancienne peut porter un code :
                    # il est remplacé, pas cumulé). Pris par un autre fichier → code à 8, puis suffixe.
                    def taken(cand):
                        rc, _o, _e = node_driver.host_exec(
                            node, "test -e %s" % shlex.quote(_mount(node).rstrip("/") + "/" + cand), timeout=20)
                        if rc != 0:
                            return None
                        r = db().execute("SELECT asset_id FROM media_files WHERE node_id=? AND share_id=0 AND path=?",
                                         (int(node_id), cand)).fetchone()
                        return (r["asset_id"] or "") if r else ""
                    rel = media_naming.pick_name(prefix + base, ids[parts[1]], taken, version=ver)
                    if media_naming.code_of(rel, ids[parts[1]]) and taken(rel) == ids[parts[1]]:
                        job["done"] += 0 if is_xml else 1
                        continue                   # cette copie du média est DÉJÀ là, sous ce nom
                    renamed[m.name] = rel
                _put_on_node(node, rel + (".xml" if is_xml else ""), tar.extractfile(m), m.size, m.mtime)
                if not is_xml:
                    job["done"] += 1
        job["scan"] = scan_node(int(node_id))
        job["state"] = "done"
    except Exception as e:                                                 # noqa: BLE001
        job.update(state="failed", error=str(e))
    finally:
        try:
            os.remove(tar_path)
        except OSError:
            pass
    return job


# ─── Proxys de lecture ──────────────────────────────────────────────────────────────────────
# Un PROXY est une INSTANCE `kind='proxy'` du média (même asset_id), jamais un média à part : MP4
# H.264 4:2:0 540 p + AAC stéréo (M4A pour un média audio seul), pour LIRE le média dans le
# navigateur (fiche Infos). Rangé sur le nœud de la copie source, dans un dossier caché à côté de
# l'original (`<dossier>/.proxies/<nom>_proxy.mp4`) que le relevé ne parcourt pas ; vérifié à
# chaque analyse par son propre chemin (`_reconcile_proxies`).
#
# GÉNÉRATION : un job de la FERME DE TRANSCODAGE (app/transcode_farm.py), priorité basse, préréglage
# fixe du transcoder (`playback_proxy` / `aac_128`) — on sait où c'est fait (transcoder, nœud), la
# file, les priorités, l'accès à la source (direct, lecture à distance, copie) et le rapatriement
# sont ceux de la ferme, et le nombre de jobs simultanés et de fils, ceux du transcoder. Aucun
# transcoder inscrit : le job attend, et la fiche le DIT (« en attente d'un transcoder »).
# Pendant la génération, la ligne du proxy est `creating` et porte le n° du job
# (`process_json.farm_job`) ; la fin du job l'enregistre (`proxy_farm_done`).
PROXY_DIR = ".proxies"
PROXY_PRIORITY = "low"


def _media_kind(tech):
    tech = tech or {}
    if tech.get("kind") in ("video", "audio", "image"):
        return tech["kind"]
    return "video" if tech.get("video") else ("audio" if tech.get("audio") else "")


def proxy_rel(path, kind="video", taken=(), aid=None):
    """Chemin du proxy de `path` (relatif au volume) : `<dossier>/.proxies/<nom>__<code>_proxy.mp4`
    (.m4a pour l'audio seul) — il suit son média : nom du fichier SANS son code éventuel, puis le
    code du média `aid` (cf. app/media_naming.py). `taken` : chemins déjà pris par le proxy d'un
    AUTRE média → code à 8, puis suffixe ; jamais d'écrasement. Sans `aid` : `<nom>_proxy` (ancien)."""
    d, n = os.path.split(path)
    stem = os.path.splitext(media_naming.strip_code(n, aid) if aid else n)[0]
    ext = ".m4a" if kind == "audio" else ".mp4"
    pre = (d + "/" if d else "") + PROXY_DIR + "/"
    if aid:
        taken = set(taken)
        return media_naming.pick_name(pre + stem + ext, aid, lambda r: "" if r in taken else None, proxy=True)
    base = pre + stem + "_proxy"
    rel, i = base + ext, 2
    while rel in taken:
        rel, i = "%s_%d%s" % (base, i, ext), i + 1
    return rel


def proxy_auto_enabled():
    try:
        from . import settings as _st
        v = _st.get("media_proxy_auto")
        # ACTIVÉ PAR DÉFAUT (décision du 2026-10-12) : non défini → activé ; un « 0 » explicite reste respecté.
        return str("1" if v in (None, "") else v).strip().lower() in ("1", "true", "on", "oui", "yes")
    except Exception:                                                      # noqa: BLE001
        return False


def proxy_auto(aid, node_id=None):
    """Déclencheur AUTOMATIQUE (réglage `media_proxy_auto`, ACTIVÉ par défaut) : à l'import d'un média
    vidéo, à la fermeture d'un enregistrement, à l'arrivée d'une sortie du transcodeur. Un média
    qui a déjà son proxy (ou en génération), ou qui n'est pas une vidéo, est laissé tel quel."""
    if not proxy_auto_enabled():
        return None
    try:
        r = db().execute("SELECT tech_json FROM media_assets WHERE id=?", (aid,)).fetchone()
        if not r or _media_kind(_j(r["tech_json"], None)) != "video":
            return None
        if db().execute("SELECT 1 FROM media_files WHERE asset_id=? AND kind='proxy' AND state IN ('present','creating')",
                        (aid,)).fetchone():
            return None
        return enqueue_proxy(aid, node_id=node_id, auto=True)
    except (ValueError, RuntimeError) as e:
        log.info("proxy automatique %s : %s", aid, e)
        return None


def _farm():
    from . import transcode_farm
    transcode_farm.ensure_tables()
    return transcode_farm


def enqueue_proxy(aid, node_id=None, auto=False, user=""):
    """Confie à la ferme la génération du proxy de `aid`, sur le nœud `node_id` (sinon celui de sa
    première copie complète). Déjà en cours pour ce média sur ce nœud : le même job. « Recréer » :
    l'ancien fichier est retiré d'abord (le transcoder ne l'écrase pas en rapatriant).
    Refus : média inconnu, image fixe, fiche technique inconnue, copie en cours d'écriture."""
    c = db()
    a = c.execute("SELECT name, tech_json FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not a:
        raise ValueError("média inconnu")
    copies = c.execute("""SELECT node_id, path, growing, tech_json FROM media_files WHERE asset_id=? AND state='present'
                          AND share_id=0 AND kind='main' ORDER BY (node_id=?) DESC, (version='original') DESC, growing, node_id""",
                       (aid, int(node_id) if node_id is not None else -1)).fetchall()
    if node_id is not None:
        copies = [r for r in copies if r["node_id"] == int(node_id)]
    if not copies:
        raise RuntimeError("aucune copie présente sur un nœud" + (" choisi" if node_id is not None else ""))
    src = next((r for r in copies if not r["growing"]), None)
    if not src:
        raise RuntimeError("fichier en cours d'écriture : le proxy sera fait à la fermeture")
    tech = _j(src["tech_json"], None) or _j(a["tech_json"], None)
    kind = _media_kind(tech)
    if kind == "image":
        raise ValueError("image fixe : pas de proxy (la vignette suffit)")
    if kind not in ("video", "audio"):
        raise ValueError("fiche technique inconnue : analyse du fichier à refaire")
    nid = src["node_id"]
    for j in proxy_jobs(asset_id=aid, node_id=nid):
        if j["state"] in ("queued", "running"):
            return j
    taken = {r["path"] for r in c.execute("SELECT path FROM media_files WHERE node_id=? AND kind='proxy' AND asset_id!=?",
                                          (nid, aid))}
    if c.execute("SELECT 1 FROM media_files WHERE asset_id=? AND node_id=? AND kind='proxy'", (aid, nid)).fetchone():
        delete_proxy(aid, nid)
    out = proxy_rel(src["path"], kind, taken, aid=aid)
    jid = _farm().submit_proxy(aid, nid, src["path"], out, kind, priority=PROXY_PRIORITY,
                               user={"username": user or ("auto" if auto else "")})
    c.execute("""INSERT INTO media_files(node_id, share_id, path, asset_id, kind, state, growing, seen_at, process_json)
                 VALUES (?,0,?,?,'proxy','creating',1,?,?)
                 ON CONFLICT(node_id, share_id, path) DO UPDATE SET asset_id=excluded.asset_id, kind='proxy',
                    state='creating', growing=1, process_json=excluded.process_json""",
              (nid, out, aid, _now(), json.dumps({"farm_job": jid, "auto": bool(auto)})))
    c.commit()
    return next(j for j in proxy_jobs(asset_id=aid) if j["id"] == jid)


_FARM_STATE = {"pending": "queued", "transfer_in": "running", "running": "running", "transfer_out": "running",
               "done": "done", "failed": "failed", "cancelled": "cancelled"}


def proxy_jobs(asset_id=None, node_id=None, limit=50):
    """Jobs de proxy de la ferme (les plus récents d'abord), vus par la base des médias : état
    (queued / running / done / failed / cancelled), avancement, OÙ c'est fait (transcoder, nœud),
    et pourquoi un job en file n'est pas parti (`waiting` : aucun transcoder inscrit à la ferme)."""
    farm = _farm()
    where, args = ["proxy_asset IS NOT NULL"], []
    if asset_id is not None:
        where.append("proxy_asset=?")
        args.append(asset_id)
    if node_id is not None:
        where.append("src_node_id=?")
        args.append(int(node_id))
    rows = farm._rows("SELECT * FROM farm_jobs WHERE %s ORDER BY id DESC LIMIT ?" % " AND ".join(where), args + [int(limit)])
    farm._decorate(rows)
    names = {}
    ids = list({r["proxy_asset"] for r in rows})
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        names.update({x["id"]: x["name"] for x in db().execute(
            "SELECT id, name FROM media_assets WHERE id IN (%s)" % ",".join("?" * len(part)), part)})
    out = []
    for r in rows:
        st = _FARM_STATE.get(r["status"], r["status"])
        out.append({"id": r["id"], "asset_id": r["proxy_asset"], "name": names.get(r["proxy_asset"], r["src_path"]),
                    "node_id": r["src_node_id"], "node": r["src_label"], "path": r["src_path"], "out": r["proxy_rel"],
                    "state": st, "farm_status": r["status"], "phase": r.get("phase"),
                    "waiting": st == "queued" and r.get("phase") == "no_transcoder",
                    "progress": float(r.get("progress") or 0), "worker": r.get("worker"), "worker_vmid": r.get("worker_vmid"),
                    "worker_node": r.get("worker_node"), "access": r.get("access"), "queue_pos": r.get("queue_pos"),
                    "error": r.get("error") or "", "created": r.get("created_at"), "ended": r.get("completed_at"),
                    "encode_s": ((r.get("info") or {}).get("result") or {}).get("encode_s"),
                    "auto": (r.get("created_by") or "") == "auto"})
    return out


def proxy_cancel(jid):
    """Annule la génération (job de la ferme). La ligne `creating` est retirée."""
    farm = _farm()
    job = farm.get_job(int(jid))
    if not job or not job.get("proxy_asset"):
        raise ValueError("tâche inconnue")
    try:
        farm.cancel(int(jid))
    except ValueError:
        pass                                   # déjà terminé
    _proxy_sync(job["proxy_asset"])
    return next((j for j in proxy_jobs(asset_id=job["proxy_asset"]) if j["id"] == int(jid)), {})


def _job_of_row(r):
    return (_j(r["process_json"], {}) or {}).get("farm_job")


def _proxy_sync(asset_id=None):
    """Lignes `creating` ↔ jobs de la ferme : job terminé → proxy enregistré ; job en échec,
    annulé ou disparu → ligne retirée (rien n'a été livré à cet emplacement)."""
    farm = _farm()
    q, args = "SELECT * FROM media_files WHERE kind='proxy' AND state='creating'", []
    if asset_id is not None:
        q += " AND asset_id=?"
        args.append(asset_id)
    c = db()
    for r in c.execute(q, args).fetchall():
        jid = _job_of_row(r)
        job = farm.get_job(int(jid)) if jid else None
        if job and job["status"] in farm.ACTIVE:
            continue
        if job and job["status"] == "done":
            try:
                proxy_farm_done(job["id"])
                continue
            except Exception as e:                                         # noqa: BLE001
                log.warning("proxy du job #%s : %s", jid, e)
                continue
        c.execute("DELETE FROM media_files WHERE node_id=? AND share_id=0 AND path=? AND state='creating'",
                  (r["node_id"], r["path"]))
    c.commit()


def proxy_farm_done(job_id):
    """Fin d'un job de proxy de la ferme : la sortie (là où la ferme l'a livrée, normalement
    `proxy_rel` sur le nœud de la source) devient le proxy PRÉSENT du média — taille relevée sur le
    nœud, jamais déduite. Appelé par la ferme (`_finish`) et, à défaut, à la synchronisation."""
    from . import node_driver
    job = _farm().get_job(int(job_id))
    if not job or job["status"] != "done" or not job.get("proxy_asset"):
        return None
    aid, nid = job["proxy_asset"], int(job["out_node_id"] or job["src_node_id"])
    path = (job["out_path"] or job["proxy_rel"]).lstrip("/")
    node = _node(nid)
    rc, out, _e = node_driver.host_exec(node, "stat -c '%%s %%Y' -- %s" % shlex.quote(_mount(node).rstrip("/") + "/" + path),
                                        timeout=30)
    if rc != 0:
        raise RuntimeError("proxy livré introuvable sur %s : %s" % ((node or {}).get("name"), path))
    size, mtime = (int(x) for x in out.split()[:2])
    info = _j(job.get("info"), {}) or {}
    farm = _farm()
    worker = next((t["hostname"] for t in farm.transcoders() if t["vmid"] == job.get("worker_vmid")), None)
    wnode = _node(job["worker_node_id"]) if job.get("worker_node_id") else None
    c = db()
    c.execute("DELETE FROM media_files WHERE asset_id=? AND node_id=? AND share_id=0 AND kind='proxy' AND path!=?",
              (aid, nid, path))
    c.execute("""INSERT INTO media_files(node_id, share_id, path, asset_id, kind, state, growing, size, mtime, seen_at, verified_at, process_json)
                 VALUES (?,0,?,?,'proxy','present',0,?,?,?,?,?)
                 ON CONFLICT(node_id, share_id, path) DO UPDATE SET asset_id=excluded.asset_id, kind='proxy', state='present',
                    growing=0, size=excluded.size, mtime=excluded.mtime, seen_at=excluded.seen_at,
                    verified_at=excluded.verified_at, process_json=excluded.process_json""",
              (nid, path, aid, size, mtime, _now(), _now(),
               json.dumps({"farm_job": job["id"], "worker": worker, "worker_vmid": job.get("worker_vmid"),
                           "worker_node": (wnode or {}).get("name"), "worker_node_id": job.get("worker_node_id"),
                           "access": job.get("access"), "encode_s": (info.get("result") or {}).get("encode_s"),
                           "video": info.get("video"), "audio": info.get("audio")}, ensure_ascii=False)))
    c.commit()
    return path


def _reconcile_proxies(node_id, stats, summary=None):
    """Proxys d'un nœud, vérifiés par LEUR chemin (relevé `proxies` du script de nœud : [taille,
    date] ou None). Présent et de la taille attendue : présent ; d'une autre taille : `corrupt` ;
    absent : `missing` (« Recréer » dans la fiche). Une ligne `creating` suit son job de la ferme
    (`_proxy_sync`). Jamais compté comme fichier du volume."""
    if stats is None:
        return
    c = db()
    if c.execute("SELECT 1 FROM media_files WHERE node_id=? AND kind='proxy' AND state='creating'", (node_id,)).fetchone():
        try:
            _proxy_sync()
        except Exception as e:                                             # noqa: BLE001
            log.warning("proxys : synchronisation avec la ferme : %s", e)
    for r in c.execute("SELECT path, state, size FROM media_files WHERE node_id=? AND share_id=0 AND kind='proxy'",
                       (node_id,)).fetchall():
        st = stats.get(r["path"], "?")
        if st == "?" or r["state"] == "creating":
            continue
        if st:
            state = "present" if (not r["size"] or int(st[0]) == int(r["size"])) else "corrupt"
            c.execute("UPDATE media_files SET state=?, growing=0, mtime=?, seen_at=? WHERE node_id=? AND share_id=0 AND path=?",
                      (state, st[1], _now(), node_id, r["path"]))
        else:
            c.execute("UPDATE media_files SET state='missing' WHERE node_id=? AND share_id=0 AND path=?", (node_id, r["path"]))
            if summary is not None:
                summary["proxy_missing"] = summary.get("proxy_missing", 0) + 1


def delete_proxy(aid, node_id):
    """Supprime le proxy de `aid` sur `node_id` (fichier + ligne). Une génération en cours est annulée."""
    from . import node_driver
    for j in proxy_jobs(asset_id=aid, node_id=node_id):
        if j["state"] in ("queued", "running"):
            try:
                _farm().cancel(j["id"])
            except (LookupError, ValueError):
                pass
    c = db()
    rows = c.execute("SELECT path FROM media_files WHERE asset_id=? AND node_id=? AND share_id=0 AND kind='proxy'",
                     (aid, int(node_id))).fetchall()
    if not rows:
        return 0
    node = _node(node_id)
    if node:
        base = _mount(node).rstrip("/") + "/"
        paths = [r["path"] for r in rows if ".." not in r["path"].split("/")]
        if paths:
            rc, _o, err = node_driver.host_exec(node, "rm -f -- " + " ".join(shlex.quote(base + p) for p in paths), timeout=60)
            if rc != 0:
                raise RuntimeError("suppression du proxy en échec : %s" % (err or "")[-300:])
    c.execute("DELETE FROM media_files WHERE asset_id=? AND node_id=? AND share_id=0 AND kind='proxy'", (aid, int(node_id)))
    c.commit()
    return len(rows)


def proxy_file(aid, node_id=None):
    """Le proxy PRÉSENT à lire : {node_id, path, size, mime} (celui de `node_id` si donné), ou None."""
    rows = db().execute("""SELECT node_id, path, size FROM media_files WHERE asset_id=? AND kind='proxy' AND state='present'
                           AND share_id=0 ORDER BY (node_id=?) DESC, node_id""",
                        (aid, int(node_id) if node_id is not None else -1)).fetchall()
    if node_id is not None:
        rows = [r for r in rows if r["node_id"] == int(node_id)]
    if not rows:
        return None
    r = rows[0]
    return {"node_id": r["node_id"], "path": r["path"], "size": int(r["size"] or 0),
            "mime": "audio/mp4" if r["path"].endswith(".m4a") else "video/mp4"}


def parse_range(header, size):
    """En-tête `Range` → (début, fin incluse), None (pas de plage : fichier entier) ou False
    (plage non satisfaisable → 416). Une seule plage (la première) : c'est ce que demande un
    lecteur HTML5. Formes : `bytes=a-b`, `bytes=a-`, `bytes=-n` (les n derniers octets)."""
    h = (header or "").strip()
    if not h.startswith("bytes="):
        return None
    first = h[6:].split(",", 1)[0].strip()
    a, sep, b = first.partition("-")
    if not sep:
        return False
    try:
        if a.strip() == "":
            n = int(b)
            if n <= 0 or size <= 0:
                return False
            return max(0, size - n), size - 1
        start = int(a)
        end = int(b) if b.strip() else size - 1
    except ValueError:
        return False
    if start < 0 or start >= size or end < start:
        return False
    return start, min(end, size - 1)


# Plus grande plage demandée à un agent ≥ 0.27.0 en une requête. Un fichier entier (200 sans
# `Range`) est lu en plages successives de cette taille : chacune est lue JUSQU'AU BOUT, la
# connexion retourne au pool entre deux (cf. aussi PROXY_RANGE_MAX dans routes/media_registry_api.py).
NODE_RANGE_CHUNK = 1 << 20


def read_node_range(node, rel, size, start, length):
    """Octets [start, start+length) d'un fichier du volume d'un nœud.
    · agent ≥ 0.27.0 : plages EXACTES `bytes=a-b` sur le pool de connexions gardées du nœud
      (node_driver.transfer_read_range), par morceaux de NODE_RANGE_CHUNK ;
    · agent ≥ 0.24.0 : `Range: bytes=<début>-` (début seulement) — la fin est tenue ICI, en coupant
      le flux (connexion perdue à chaque lecture) ;
    · plus ancien : root-SSH, `tail -c +N | head -c L`."""
    import subprocess
    from . import node_driver
    if length <= 0:
        return
    if node_driver.transfer_range_capable(node):
        pos, stop = start, start + length
        while pos < stop:
            end = min(stop, pos + NODE_RANGE_CHUNK) - 1
            yield from node_driver.transfer_read_range(node, rel, size, pos, end)
            pos = end + 1
        return
    if node_driver.transfer_capable(node):
        import requests
        from urllib.parse import quote
        from . import ca
        cert, key, cafile = ca.controller_client_files()
        url = "https://%s:%d/v1/transfer/read?g=%s" % (node.get("host"), node_driver.TRANSFER_PORT,
                                                       quote(node_driver.transfer_grant(node, rel, size, ttl_s=600)))
        r = requests.get(url, stream=True, timeout=30, cert=(cert, key), verify=cafile,
                         headers={"Range": "bytes=%d-" % start} if start else {})
        try:
            if r.status_code not in (200, 206) or (start and r.status_code != 206):
                raise RuntimeError("%s : lecture de %s refusée (%s)" % (node.get("name"), rel, r.status_code))
            left = length
            for b in r.iter_content(256 * 1024):
                if len(b) > left:
                    b = b[:left]
                left -= len(b)
                yield b
                if left <= 0:
                    break
        finally:
            r.close()
        return
    full = _mount(node).rstrip("/") + "/" + rel
    proc = subprocess.Popen(["ssh"] + _SSH_OPT.split() + ["root@" + node["host"],
                            "tail -c +%d %s | head -c %d" % (start + 1, shlex.quote(full), length)], stdout=subprocess.PIPE)
    try:
        while True:
            b = proc.stdout.read(256 * 1024)
            if not b:
                break
            yield b
    finally:
        proc.kill()
        proc.wait()


# ─── Macros : la base des médias, fournisseur d'actions du cœur ─────────────────────────────
# Cf. CLAUDE.md « Exposer aux macros » et app/core_plugins.py (CORE_PROVIDERS). « Créer le proxy »
# confie un job à la ferme : les actions de la ferme (annuler, priorité…) valent aussi pour lui.
MACRO_PROVIDER = {
    "id": "media_registry",
    "label": "Base des médias",
    "actions": [
        {"id": "create_proxy", "label": "Créer le proxy d'un média", "availability": "project", "params": [
            {"key": "asset", "label": "Média", "type": "select", "options_hook": True},
            {"key": "node", "label": "Nœud (vide : celui de sa copie)", "type": "select", "options_hook": True, "default": ""}]},
        # Planche d'images (app/media_storyboard.py) : job basse priorité de la ferme, tiré du proxy.
        {"id": "create_storyboard", "label": "Préparer la planche d'images d'un média", "availability": "project", "params": [
            {"key": "asset", "label": "Média", "type": "select", "options_hook": True}]},
        # VERSIONS : charger un média dans un conteneur (sa version par défaut, ou une version
        # précise) — rapatrié d'abord s'il n'a pas de copie sur le nœud du conteneur, puis chargé.
        {"id": "send_media", "label": "Charger un média dans un conteneur (Player, Images fixes)", "availability": "project",
         "params": [
            {"key": "container", "label": "Conteneur", "type": "select", "options_hook": True},
            {"key": "asset", "label": "Média", "type": "select", "options_hook": True},
            {"key": "version", "label": "Version (vide : la version par défaut)", "type": "select", "options_hook": True,
             "default": ""},
            {"key": "action", "label": "Geste", "type": "select", "default": "load", "options": [
                {"value": "load", "label": "Charger (player)"}, {"value": "playlist", "label": "Ajouter à la playlist (player)"},
                {"value": "show", "label": "Afficher (images fixes)"}]}]},
        {"id": "set_default_version", "label": "Choisir la version par défaut d'un média", "availability": "project",
         "params": [
            {"key": "asset", "label": "Média", "type": "select", "options_hook": True},
            {"key": "version", "label": "Version", "type": "select", "options_hook": True}]},
    ],
    "states": [
        {"id": "proxy_queued", "label": "Proxys en attente", "type": "number"},
        {"id": "proxy_running", "label": "Proxys en cours de génération", "type": "number"},
        {"id": "proxy_waiting", "label": "Proxys en attente d'un transcoder (aucun inscrit à la ferme)", "type": "number"},
        {"id": "proxy_failed", "label": "Proxys en échec (200 derniers jobs)", "type": "number"},
        {"id": "storyboard_missing", "label": "Médias à proxy vidéo sans planche d'images", "type": "number"},
    ],
}


MACRO_ASSET_OPTIONS_MAX = 500


def macro_asset_options(pid=None, limit=MACRO_ASSET_OPTIONS_MAX):
    """Sélecteur de média d'une macro : la VALEUR stockée est l'identité (un homonyme ajouté plus
    tard ne change pas la cible), le libellé « nom — projet(s) ». Macro de projet : les médias de ce
    projet ; macro système : toute la base (les plus récents d'abord, bornés à `limit`)."""
    from .database import db_get_projects
    pn = {p["id"]: p.get("name") for p in (db_get_projects() or [])}
    c = db()
    if pid is not None:
        rows = c.execute("""SELECT a.id, a.name FROM media_assets a JOIN project_media p ON p.asset_id=a.id
                            WHERE p.project_id=? ORDER BY LOWER(a.name), a.created_at DESC LIMIT ?""", (int(pid), int(limit))).fetchall()
    else:
        rows = c.execute("SELECT id, name FROM media_assets ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        rows = sorted(rows, key=lambda r: ((r["name"] or "").lower(), r["id"]))
    projs = {}
    for r in c.execute("SELECT asset_id, project_id FROM project_media ORDER BY project_id"):
        projs.setdefault(r["asset_id"], []).append(r["project_id"])
    out = []
    for r in rows:
        where = ", ".join(str(pn.get(p) or "#%s" % p) for p in projs.get(r["id"], [])) or "sans projet"
        out.append({"value": r["id"], "label": "%s — %s" % (r["name"], where)})
    seen = {}
    for o in out:
        seen[o["label"]] = seen.get(o["label"], 0) + 1
    for o in out:                        # homonymes dans le même projet : l'identité courte les sépare
        if seen[o["label"]] > 1:
            o["label"] += " (%s)" % o["value"][:8]
    return out


def macro_version_options(pid=None, with_default=True):
    """Versions proposées à une macro : « par défaut » (la version que choisit la fiche), l'original,
    puis les clés de version présentes dans la base (celles des médias du projet de la macro). La
    clé est la même d'un média à l'autre (nom court du préréglage) ; un média qui ne l'a pas est
    refusé à l'exécution, en le disant."""
    c = db()
    if pid is not None:
        rows = c.execute("""SELECT DISTINCT v.key, v.label FROM media_versions v JOIN project_media p ON p.asset_id=v.asset_id
                            WHERE p.project_id=? ORDER BY v.key""", (int(pid),)).fetchall()
    else:
        rows = c.execute("SELECT DISTINCT key, label FROM media_versions ORDER BY key").fetchall()
    seen, out = set(), ([{"value": "", "label": "Version par défaut du média"}] if with_default else [])
    out.append({"value": ORIGINAL, "label": "Original"})
    for r in rows:
        if r["key"] not in seen:
            seen.add(r["key"])
            out.append({"value": r["key"], "label": r["key"] + (" — %s" % r["label"] if r["label"] else "")})
    return out


def _macro_containers(pid=None):
    """Conteneurs qu'une macro peut charger (types de SEND_ACTIONS hors transcodeur) — ceux de son projet."""
    import json as _json
    from .database import db_get_containers
    out = []
    for ct in db_get_containers():
        try:
            t = (_json.loads(ct.get("deploy_config") or "{}") or {}).get("type")
        except (TypeError, ValueError):
            continue
        if t in SEND_ACTIONS and t != "transcoder" and (pid is None or ct.get("project_id") == int(pid)):
            out.append({"value": str(ct["vmid"]), "label": "%s (%s)" % (ct.get("hostname") or ct["vmid"], t)})
    return sorted(out, key=lambda o: o["label"].lower())


def action_options(action_id, key, pid=None):
    if key == "asset" and action_id in ("create_proxy", "create_storyboard", "send_media", "set_default_version"):
        return macro_asset_options(pid)
    if key == "version" and action_id in ("send_media", "set_default_version"):
        return macro_version_options(pid, with_default=action_id == "send_media")
    if key == "container" and action_id == "send_media":
        return _macro_containers(pid)
    if action_id == "create_proxy" and key == "node":
        from .database import db_get_nodes
        return [{"value": "", "label": "Nœud de sa copie"}] + [
            {"value": str(n["id"]), "label": n.get("name") or "#%s" % n["id"]}
            for n in db_get_nodes() if (n.get("media_image") or "").strip()]
    return []


def _resolve_asset(ref, pid=None, vmid=None):
    ref = str(ref or "").strip()
    if not ref:
        raise RuntimeError("média à préciser")
    c = db()
    r = c.execute("SELECT id FROM media_assets WHERE id=?", (ref,)).fetchone()
    if not r:
        # Nom tapé à la main : jamais refusé pour cause d'homonymes — règle connue (projet, puis
        # nœud, puis le plus récent) et choix journalisé (app/media_naming.resolve_name). Borné au
        # projet de la macro quand elle en a un.
        aid, _note = media_naming.resolve_name(ref, c, project_id=pid, only_project=pid is not None, vmid=vmid)
        r = {"id": aid} if aid else None
    if not r:
        raise RuntimeError("média introuvable : %s" % ref)
    if pid is not None and not c.execute("SELECT 1 FROM project_media WHERE project_id=? AND asset_id=?",
                                         (int(pid), r["id"])).fetchone():
        raise RuntimeError("média hors du projet de la macro : %s" % ref)
    return r["id"]


def run_action(action_id, params, ctx):
    pid = (ctx or {}).get("project_id")
    if action_id == "create_proxy":
        aid = _resolve_asset((params or {}).get("asset"), pid)
        node = str((params or {}).get("node") or "").strip()
        try:
            j = enqueue_proxy(aid, node_id=int(node) if node.isdigit() else None, user=(ctx or {}).get("user") or "macro")
        except ValueError as e:
            raise RuntimeError(str(e))
        return {"ok": True, "job": j["id"], "state": j["state"]}
    if action_id == "create_storyboard":
        from . import media_storyboard as _sbm
        aid = _resolve_asset((params or {}).get("asset"), pid)
        try:
            j = _sbm.enqueue(aid, user=(ctx or {}).get("user") or "macro")
        except ValueError as e:
            raise RuntimeError(str(e))
        return {"ok": True, "job": j["id"], "state": j["state"]}
    if action_id == "set_default_version":
        aid = _resolve_asset((params or {}).get("asset"), pid)
        try:
            return {"ok": True, "version": set_default_version(aid, (params or {}).get("version") or ORIGINAL)}
        except ValueError as e:
            raise RuntimeError(str(e))
    if action_id == "send_media":
        return _macro_send_media(params or {}, pid, (ctx or {}).get("user") or "macro")
    raise RuntimeError("action inconnue : %s" % action_id)


FETCH_WAIT_S = 6 * 3600          # un rapatriement suivi par une macro : abandonné au-delà


def _macro_send_media(params, pid, user):
    """Charge (`load` / `playlist` / `show`) la version demandée d'un média dans un conteneur.
    Sans copie sur son nœud : rapatriement suivi (app/media_fetch.py), puis chargement à la fin —
    la macro rend la main tout de suite (`state: fetching`)."""
    from . import media_fetch
    raw = str(params.get("container") or "").strip()
    if not raw.isdigit():
        raise RuntimeError("conteneur à préciser")
    vmid = int(raw)
    if pid is not None and not any(o["value"] == raw for o in _macro_containers(pid)):
        raise RuntimeError("conteneur hors du projet de la macro : %s" % raw)
    action = str(params.get("action") or "load")
    aid = _resolve_asset(params.get("asset"), pid, vmid=vmid)
    try:
        version = resolve_version(aid, params.get("version"))
    except ValueError as e:
        raise RuntimeError(str(e))
    paths, missing, origins = resolve_for_container(vmid, [aid], version=version)
    if paths:
        send_to(vmid, action, paths, origins=origins, user=user)
        return {"ok": True, "state": "sent", "version": version, "path": paths[0]}
    j = media_fetch.start(aid, vmid, version=version)
    if j["state"] == "done":
        send_to(vmid, action, [j["path"]], origins={j["path"]: aid}, user=user)
        return {"ok": True, "state": "sent", "version": version, "path": j["path"]}

    def follow():
        t0 = time.time()
        while time.time() - t0 < FETCH_WAIT_S:
            time.sleep(2)
            cur = media_fetch.get(j["job"]) or {}
            if cur.get("state") == "done":
                try:
                    send_to(vmid, action, [cur["path"]], origins={cur["path"]: aid}, user=user)
                except (ValueError, RuntimeError) as e:
                    log.warning("macro send_media : chargement après rapatriement : %s", e)
                return
            if cur.get("state") in (None, "failed", "unknown"):
                log.warning("macro send_media : rapatriement en échec : %s", cur.get("error"))
                return
    threading.Thread(target=follow, daemon=True, name="macro-send-%s" % j["job"]).start()
    return {"ok": True, "state": "fetching", "version": version, "job": j["job"]}


def read_state(state_id, ctx):
    if state_id == "storyboard_missing":
        from . import media_storyboard as _sbm
        return _sbm.status()["missing"]
    js = proxy_jobs(limit=200)
    if state_id == "proxy_waiting":
        return sum(1 for j in js if j["waiting"])
    if state_id == "proxy_queued":
        return sum(1 for j in js if j["state"] == "queued")
    if state_id == "proxy_running":
        return sum(1 for j in js if j["state"] == "running")
    if state_id == "proxy_failed":
        return sum(1 for j in js if j["state"] == "failed")
    return None


# ─── Suppression ────────────────────────────────────────────────────────────────────────────
def delete_copy(aid, node_id, path):
    """Supprime UNE copie (fichier + sidecar sur le nœud). Le média et ses métadonnées restent."""
    from . import node_driver
    c = db()
    r = c.execute("SELECT * FROM media_files WHERE asset_id=? AND node_id=? AND share_id=0 AND kind='main' AND path=?",
                  (aid, int(node_id), path)).fetchone()
    if not r:
        raise ValueError("copie inconnue")
    if r["growing"]:
        raise RuntimeError("fichier en cours d'écriture : suppression refusée")
    node = _node(node_id)
    full = _mount(node).rstrip("/") + "/" + path
    if ".." in path.split("/"):
        raise ValueError("chemin invalide")
    rc, _o, err = node_driver.host_exec(node, "rm -f -- %s %s" % (shlex.quote(full), shlex.quote(full + ".xml")), timeout=60)
    if rc != 0:
        raise RuntimeError("suppression en échec : %s" % (err or "")[-300:])
    c.execute("DELETE FROM media_files WHERE node_id=? AND share_id=0 AND kind='main' AND path=?", (int(node_id), path))
    c.commit()
    # Le proxy de ce nœud part avec sa dernière copie principale sur ce nœud.
    if not c.execute("SELECT 1 FROM media_files WHERE asset_id=? AND node_id=? AND share_id=0 AND kind='main' AND state='present'",
                     (aid, int(node_id))).fetchone():
        delete_proxy(aid, int(node_id))


def forget_copy(aid, node_id, path):
    """Oublie une copie ABSENTE (perdue, ou déplacée hors du système) : rien n'est touché sur
    disque, seule la ligne part. Refusé pour une copie présente — c'est delete_copy qui s'en charge."""
    c = db()
    r = c.execute("SELECT state FROM media_files WHERE asset_id=? AND node_id=? AND kind='main' AND path=?",
                  (aid, int(node_id), path)).fetchone()
    if not r:
        raise ValueError("copie inconnue")
    if r["state"] == "present":
        raise ValueError("copie présente : utiliser la suppression")
    c.execute("DELETE FROM media_files WHERE node_id=? AND kind='main' AND path=?", (int(node_id), path))
    c.commit()


def delete_asset(aid, with_files=True):
    """Supprime le média ENTIER : toutes ses copies présentes (si `with_files`), ses métadonnées,
    sa vignette, ses rattachements aux projets."""
    c = db()
    for j in proxy_jobs(asset_id=aid):
        if j["state"] in ("queued", "running"):
            proxy_cancel(j["id"])
    if with_files:
        for f in c.execute("SELECT node_id, path FROM media_files WHERE asset_id=? AND state='present' AND share_id=0 AND kind='main'",
                           (aid,)).fetchall():
            delete_copy(aid, f["node_id"], f["path"])
        for n in {r["node_id"] for r in c.execute("SELECT node_id FROM media_files WHERE asset_id=? AND kind='proxy'", (aid,))}:
            delete_proxy(aid, n)
    c.execute("DELETE FROM media_files WHERE asset_id=?", (aid,))
    c.execute("DELETE FROM media_assets WHERE id=?", (aid,))
    c.commit()
    try:
        os.remove(thumb_path(aid))
    except OSError:
        pass
    from . import media_storyboard as _sbm      # planche d'images : jamais sans son média
    _sbm.delete(aid)


# ─── Intégrité ──────────────────────────────────────────────────────────────────────────────
def integrity():
    """État de la base, dans les deux sens :
    - référencé mais introuvable : média sans aucune copie présente ; copie attendue absente ;
      référence de projet vers un média inconnu ; projet disparu de la base principale ;
    - présent mais non référencé : fichiers vus sur les volumes, hors base ;
    - douteux : copie dont l'empreinte ne correspond plus au média ; vignette manquante."""
    from .database import db_get_nodes, get_db
    c = db()
    names = {n["id"]: n.get("name") for n in db_get_nodes()}
    try:
        projects = {r["id"]: r["name"] for r in get_db().execute("SELECT id, name FROM projects")}
    except sqlite3.Error:
        projects = {}
    assets = {r["id"]: r for r in c.execute("SELECT * FROM media_assets")}
    present = {}
    # Une copie sur un PARTAGE compte comme « non perdu » (on peut la rapatrier), pas comme
    # disponible à la lecture : le rapport la distingue.
    for f in c.execute("SELECT asset_id, COUNT(*) n FROM media_files WHERE asset_id IS NOT NULL AND state='present' AND share_id=0 AND kind='main' GROUP BY asset_id"):
        present[f["asset_id"]] = f["n"]
    rep = {"counts": {"assets": len(assets)}, "offline": [], "missing_copies": [], "dangling_project_refs": [],
           "unknown_projects": [], "unreferenced": [], "fingerprint_mismatch": [], "no_thumb": 0}
    for aid, a in assets.items():
        if not present.get(aid):
            on_share = c.execute("SELECT COUNT(*) FROM media_files WHERE asset_id=? AND share_id!=0", (aid,)).fetchone()[0]
            rep["offline"].append({"id": aid, "name": a["name"], "on_share": bool(on_share),
                                   "projects": [projects.get(p["project_id"], p["project_id"]) for p in
                                                c.execute("SELECT project_id FROM project_media WHERE asset_id=?", (aid,))]})
        if not os.path.exists(thumb_path(aid)):
            rep["no_thumb"] += 1
    for f in c.execute("SELECT f.*, a.name FROM media_files f JOIN media_assets a ON a.id=f.asset_id WHERE f.state='missing' AND f.share_id=0 AND f.kind='main'"):
        rep["missing_copies"].append({"id": f["asset_id"], "name": f["name"], "node_id": f["node_id"],
                                      "node": names.get(f["node_id"], f["node_id"]),
                                      "path": f["path"], "seen_at": f["seen_at"]})
    for p in c.execute("SELECT project_id, asset_id FROM project_media"):
        if p["project_id"] not in projects:
            rep["unknown_projects"].append({"project_id": p["project_id"], "asset_id": p["asset_id"]})
    for f in c.execute("SELECT node_id, path, size, mtime FROM media_files WHERE asset_id IS NULL AND state='present' AND share_id=0 AND kind='main' ORDER BY node_id, path"):
        rep["unreferenced"].append({"node_id": f["node_id"], "node": names.get(f["node_id"], f["node_id"]),
                                    "path": f["path"], "size": f["size"]})
    # Empreinte comparée à celle de la VERSION que porte la copie (l'original : celle de la fiche).
    for f in c.execute("""SELECT f.node_id, f.path, f.asset_id, f.version, a.name FROM media_files f
                          JOIN media_assets a ON a.id=f.asset_id
                          LEFT JOIN media_versions v ON v.asset_id=f.asset_id AND v.key=f.version
                          WHERE f.fingerprint IS NOT NULL AND f.kind='main' AND f.growing=0
                            AND COALESCE(CASE WHEN f.version='original' THEN a.fingerprint ELSE v.fingerprint END, f.fingerprint)
                                != f.fingerprint"""):
        rep["fingerprint_mismatch"].append({"id": f["asset_id"], "name": f["name"], "version": f["version"],
                                            "node": names.get(f["node_id"], f["node_id"]), "path": f["path"]})
    rep["counts"].update({k: len(v) for k, v in rep.items() if isinstance(v, list)})
    rep["counts"]["no_thumb"] = rep["no_thumb"]
    return rep


# ─── Sauvegarde, export, import ─────────────────────────────────────────────────────────────
def export_bytes():
    """Archive .tar.gz : copie cohérente de la base (API online de SQLite) + vignettes."""
    buf = io.BytesIO()
    tmp = MEDIA_DB_PATH + ".export"
    dst = sqlite3.connect(tmp)
    try:
        db().backup(dst)
    finally:
        dst.close()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(tmp, arcname="db_medias.db")
        if os.path.isdir(THUMB_DIR):
            for fn in os.listdir(THUMB_DIR):
                if fn.endswith(".jpg"):
                    tar.add(os.path.join(THUMB_DIR, fn), arcname="media_thumbs/" + fn)
    os.remove(tmp)
    return buf.getvalue()


def backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = "db_medias-%s.tar.gz" % time.strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(BACKUP_DIR, name), "wb") as f:
        f.write(export_bytes())
    return name


def list_backups():
    try:
        return sorted(({"name": fn, "size": os.path.getsize(os.path.join(BACKUP_DIR, fn))}
                       for fn in os.listdir(BACKUP_DIR) if fn.startswith("db_medias-") and fn.endswith(".tar.gz")),
                      key=lambda x: x["name"], reverse=True)
    except FileNotFoundError:
        return []


def import_bytes(data, mode="merge"):
    """Importe une archive d'export. `merge` : ajoute ou complète (médias par identité, champs par
    clé, fichiers par (nœud, chemin)) sans rien retirer ; `replace` : remplace la base (une
    sauvegarde de l'état courant est faite avant)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar.getmembers():
                if m.name not in ("db_medias.db",) and not (m.name.startswith("media_thumbs/") and m.name.endswith(".jpg")
                                                            and "/" not in m.name[len("media_thumbs/"):]):
                    continue
                if not m.isfile():
                    continue
                tar.extract(m, td, filter="data")
        src_path = os.path.join(td, "db_medias.db")
        if not os.path.exists(src_path):
            raise ValueError("archive sans db_medias.db")
        src = sqlite3.connect(src_path)
        src.row_factory = sqlite3.Row
        c = db()
        saved = backup() if mode == "replace" else None
        if mode == "replace":
            for t in ("project_media", "media_files", "media_versions", "media_fields", "media_assets"):
                c.execute("DELETE FROM %s" % t)
        n = {"assets": 0, "versions": 0, "fields": 0, "files": 0, "projects": 0}
        # Les EMPLACEMENTS (numéros de nœud) n'ont de sens que sur l'installation d'origine : ils ne
        # sont repris qu'en restauration (`replace`). Venus d'un autre serveur, les médias
        # retrouvent leurs copies à la prochaine analyse, par l'identité écrite dans les sidecars.
        tables = [("media_assets", "assets"), ("media_versions", "versions"), ("media_fields", "fields"),
                  ("project_media", "projects")]
        if mode == "replace":
            tables.insert(3, ("media_files", "files"))
        for t, key in tables:
            rows = src.execute("SELECT * FROM %s" % t).fetchall()
            for r in rows:
                cols = r.keys()
                c.execute("INSERT OR IGNORE INTO %s(%s) VALUES (%s)" % (t, ",".join(cols), ",".join("?" * len(cols))),
                          tuple(r))
                n[key] += c.execute("SELECT changes()").fetchone()[0]
        c.commit()
        src.close()
        os.makedirs(THUMB_DIR, exist_ok=True)
        tdir = os.path.join(td, "media_thumbs")
        if os.path.isdir(tdir):
            for fn in os.listdir(tdir):
                dst = os.path.join(THUMB_DIR, fn)
                if mode == "replace" or not os.path.exists(dst):
                    os.replace(os.path.join(tdir, fn), dst)
    return {"imported": n, "backup_before": saved}
