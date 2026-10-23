# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Envoi d'un fichier DEPUIS LE POSTE de l'utilisateur vers le volume média d'un nœud, puis import
dans la base des médias — par MORCEAUX, REPRENABLE (une vidéo de plusieurs Go).

Chemin des octets : navigateur (File.slice, CHUNK) → orchestrateur (`PUT /api/media/upload/<id>`)
→ agent-nœud (`/v1/host/transfer/upload?offset=`, ≥ 0.26.0), relayé EN FLUX : ni le fichier
entier en mémoire, ni sur le disque de l'orchestrateur (Waitress tamponne UN morceau, au plus
CHUNK_MAX, avant d'appeler la route — c'est tout).

Reprise : la SESSION (nœud, chemin, nom, taille, date de modification) est en base. Re-choisir le
même fichier (même nom + taille + date, même destination) retrouve la session, et l'envoi repart
de la taille du `.partial` DEMANDÉE AU NŒUD — jamais du compte tenu ici, qui peut être en retard
(morceau parti, réponse perdue) ou en avance (`.partial` effacé à la main).

Fin : finalisation sur le nœud (taille vérifiée, renommage sans écrasement), analyse du volume, et
IMPORT dans la base (geste explicite de l'utilisateur), origine « upload » ; rangé dans le projet
et le bin du contexte ; vignette et proxy automatique par le chemin commun de `import_files`.

NOM SUR LE NŒUD (règle du 2026-10-12, app/media_naming.py) : `<nom>__<code>.<ext>`, code = celui
de l'identité du média, RÉSERVÉE à l'ouverture de la session (`asset_id`) — celle d'un contenu
identique déjà connu (empreinte calculée par la page), sinon une nouvelle. Le nom VOULU (`name`)
reste le nom du média ; deux médias ne se disputent donc jamais un nom de fichier, et la question
« nom déjà pris » ne se pose plus (seul reste « ce contenu est déjà là », cf. `check_names`).
Contrat agent : NODE_AGENT.md § 4.3 quater."""

import logging
import os
import re
import secrets
import threading
import time

from . import media_registry as mr

log = logging.getLogger(__name__)

CHUNK = 16 << 20                 # taille d'un morceau envoyé par le navigateur
CHUNK_MAX = 64 << 20             # refus au-delà (Waitress tamponne le corps avant la route)
MAX_GB_DEFAULT = 200             # réglage `media_upload_max_gb`
FREE_MARGIN = 1 << 30            # même marge que l'agent (UPLOAD_FREE_MARGIN)
STALE_S = 7 * 86400              # session inactive depuis plus longtemps : abandonnée (le .partial est effacé)
ACTIVE = ("uploading", "finishing", "importing")
EXTS = tuple(mr.BROWSE_EXTS)     # ce que le relevé du volume reconnaît : un autre fichier n'entrerait jamais dans la base
KIND_EXTS = {
    "video": (".mxf", ".mov", ".mp4", ".mkv", ".avi", ".ts", ".mts", ".m2ts", ".m4v", ".webm"),
    "audio": (".wav", ".mp3", ".aac", ".flac", ".m4a"),
    "image": (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"),
}
_SAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_inited = set()
_import_lock = threading.Lock()


class UploadError(Exception):
    """Refus lisible : `code` HTTP à rendre, `extra` à joindre à la réponse."""

    def __init__(self, msg, code=400, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


def db():
    c = mr.db()
    key = mr.MEDIA_DB_PATH
    if key not in _inited:
        c.execute("""CREATE TABLE IF NOT EXISTS media_uploads(
            id TEXT PRIMARY KEY, user_id INTEGER, node_id INTEGER NOT NULL, folder TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL, dest TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL,
            project_id INTEGER, bin_id INTEGER, vmid INTEGER,
            state TEXT NOT NULL DEFAULT 'uploading', received INTEGER NOT NULL DEFAULT 0,
            asset_id TEXT, known INTEGER NOT NULL DEFAULT 0, error TEXT,
            created_at REAL, updated_at REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS media_uploads_user ON media_uploads(user_id, state)")
        # Nom du fichier SUR LE POSTE quand l'utilisateur l'a renommé pour l'envoi (nom déjà pris) :
        # c'est lui que la reprise reconnaît au re-choix du même fichier.
        if "source_name" not in {r[1] for r in c.execute("PRAGMA table_info(media_uploads)")}:
            c.execute("ALTER TABLE media_uploads ADD COLUMN source_name TEXT")
        c.commit()
        _inited.add(key)
    return c


def max_bytes():
    try:
        from . import settings as _st
        v = float(_st.get("media_upload_max_gb") or MAX_GB_DEFAULT)
    except (TypeError, ValueError):
        v = MAX_GB_DEFAULT
    return int(max(0.001, v) * (1 << 30))


def ext_ok(name):
    return os.path.splitext(str(name or ""))[1].lower() in EXTS


def clean_name(name):
    """Nom de fichier du poste → nom sûr (sans chemin, sans caractère de contrôle, pas caché)."""
    n = os.path.basename(str(name or "").replace("\\", "/")).strip()
    n = _SAFE.sub("_", n).lstrip(".").strip()
    if len(n) > 200:
        stem, ext = os.path.splitext(n)
        n = stem[:200 - len(ext)] + ext
    return n


def clean_folder(folder):
    """Dossier relatif au volume ; refuse toute sortie (`..`) et tout dossier caché."""
    parts = [p for p in str(folder or "").replace("\\", "/").split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or p.startswith("."):
            raise UploadError("dossier refusé : %s" % folder)
    return "/".join(parts)


def _suffixed(rel, i):
    stem, ext = os.path.splitext(rel)
    return "%s_%d%s" % (stem, i, ext)


def _resolve_target(node_id, folder, vmid):
    """(node, folder) d'une destination — même règle que `start` (contexte d'un conteneur d'abord)."""
    if vmid is not None:
        node_id, prefix = mr.container_view(vmid)
        folder = prefix.rstrip("/")
    folder = clean_folder(folder)
    return _node_or_raise(node_id), folder


def _busy_dests(node_id):
    """Chemins visés par un envoi en cours sur ce nœud (tous utilisateurs) : réservés."""
    return {r["dest"] for r in db().execute("SELECT dest FROM media_uploads WHERE node_id=? AND state IN ('uploading','finishing')",
                                            (int(node_id),))}


def _taken(node, rel, busy):
    """Pourquoi `rel` n'est pas libre : 'file' (présent sur le nœud), 'partial' (un `.partial` y est),
    'upload' (un envoi en cours le vise) — ou None s'il l'est."""
    if rel in busy:
        return "upload"
    st = _state(node, rel)
    if st.get("exists"):
        return "file"
    if st.get("partial_size"):
        return "partial"
    return None


def _free_rel(node, base, busy, reserved=()):
    """Premier nom libre `base`, `base_2`, `base_3`… (ni sur le nœud, ni visé, ni dans `reserved`)."""
    rel, i = base, 2
    while rel in reserved or _taken(node, rel, busy):
        rel, i = _suffixed(base, i), i + 1
        if i > 999:
            raise UploadError("aucun nom libre pour %s" % base, 409)
    return rel


def validate_rename(new, original):
    """Nom SAISI par l'utilisateur pour remplacer `original` → nom accepté, ou UploadError.
    Pas de nettoyage silencieux : un caractère interdit est REFUSÉ (l'utilisateur voit ce qui partira).
    Extension : sans extension, celle de l'original est ajoutée ; une autre est refusée (le contenu
    ne change pas de type en changeant de nom)."""
    n = str(new or "").strip()
    ext0 = os.path.splitext(clean_name(original))[1]
    if not n:
        raise UploadError("nom vide", 400, bad_name=True)
    bad = sorted(set(_SAFE.findall(n)))
    if bad:
        raise UploadError("caractères interdits dans un nom de fichier : %s"
                          % " ".join(c if c.isprintable() else "\\x%02x" % ord(c) for c in bad), 400, bad_name=True)
    if n.startswith("."):
        raise UploadError("un nom ne peut pas commencer par un point (fichier caché)", 400, bad_name=True)
    ext = os.path.splitext(n)[1].lower()
    if ext in EXTS and ext != ext0.lower():
        raise UploadError("l'extension doit rester %s" % ext0, 400, bad_name=True)
    if ext != ext0.lower():
        n += ext0                                                          # « mon clip » / « prise v1.2 » → + .mov
    if len(n) > 200:
        raise UploadError("nom trop long (200 caractères au plus)", 400, bad_name=True)
    return n


def _resumable(user_id, node_id, folder, name, size, mtime):
    """Session interrompue du MÊME fichier (nom sur le poste + taille + date) vers la même destination."""
    return db().execute("""SELECT * FROM media_uploads WHERE user_id IS ? AND node_id=? AND folder=? AND size=? AND mtime IS ?
                             AND (name=? OR source_name=?) AND state IN ('uploading','finishing')
                           ORDER BY updated_at DESC LIMIT 1""",
                        (user_id, int(node_id), folder, int(size), mtime, name, name)).fetchone()


def _mtime(v):
    try:
        return round(float(v), 3) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def asset_visible(asset_id, scope):
    """Portée d'un utilisateur (`media_sharing.user_scope()` : None = tout, sinon ses projets ET ce
    qui est partagé avec lui) : un média lui est visible s'il est rattaché à l'un de ses projets ou
    partagé avec lui. Source unique : media_registry.asset_visible."""
    return mr.asset_visible(str(asset_id), scope)


def same_content(fp, node_id=None, folder=None, limit=8, scope=None):
    """Médias de la base dont l'empreinte est `fp` (celle de media_scan : taille + 4 Mio de tête +
    4 Mio de queue, recalculée par la page sur le fichier du poste) et leurs copies PRÉSENTES
    → ([{asset_id, asset_name, node_id, share_id, node, location, path, same_node, same_folder}], hidden),
    celles du nœud / dossier de destination d'abord. Seuls les médias VISIBLES pour `scope` sont
    décrits ; `hidden` dit qu'il en existe d'autres (hors des projets de l'utilisateur) — sans
    nœud, chemin ni nom : leur existence seule."""
    out, hidden = [], False
    # Même contenu = l'original d'un média OU une de ses VERSIONS (même règle que l'analyse,
    # mr.find_by_fingerprint) : seules les copies de CETTE version sont décrites.
    hits = [(a["id"], a["name"], mr.ORIGINAL) for a in mr.db().execute(
        "SELECT id, name FROM media_assets WHERE fingerprint=? ORDER BY created_at", (fp,)).fetchall()]
    hits += [(v["asset_id"], v["name"], v["key"]) for v in mr.db().execute(
        """SELECT v.asset_id, v.key, a.name FROM media_versions v JOIN media_assets a ON a.id=v.asset_id
           WHERE v.fingerprint=? ORDER BY v.created_at""", (fp,)).fetchall()]
    for aid, aname, ver in hits:
        if not asset_visible(aid, scope):
            hidden = hidden or any(f["state"] == "present" for f in mr._files_of(aid, version=ver))
            continue
        for f in mr._files_of(aid, version=ver):
            if f["state"] != "present":
                continue
            same_node = f["location"] == "node" and node_id is not None and int(f["node_id"]) == int(node_id)
            out.append({"asset_id": aid, "asset_name": aname, "version": ver, "node_id": f["node_id"], "share_id": f["share_id"],
                        "node": f["node"], "location": f["location"], "path": f["path"], "same_node": same_node,
                        "same_folder": same_node and folder is not None
                        and os.path.dirname(f["path"]).strip("/") == (folder or "").strip("/")})
    out.sort(key=lambda d: (not d["same_folder"], not d["same_node"], d["location"], str(d["node"]), d["path"]))
    return out[:limit], hidden


def attach_existing(asset_id, vmid=None, project_id=None, bin_id=None, scope=None):
    """« Ne pas l'envoyer » (même contenu déjà dans la base) : le média EXISTANT est rangé comme
    l'aurait été l'envoi — projet du contexte (celui du conteneur, ou celui demandé) et bin.
    Portée `scope` : un média hors des projets de l'utilisateur est « inconnu » (404, son existence
    n'est pas confirmée) ; un projet ou un bin d'un autre projet est refusé (403)."""
    if not mr.db().execute("SELECT 1 FROM media_assets WHERE id=?", (str(asset_id),)).fetchone() \
            or not asset_visible(asset_id, scope):
        raise UploadError("média inconnu", 404)
    if vmid is not None:
        project_id = project_id or mr.container_context(vmid).get("project_id")
    if project_id and scope is not None and int(project_id) not in {int(p) for p in scope}:
        raise UploadError("projet hors de vos accès", 403)
    if bin_id:
        b = mr.db().execute("SELECT project_id FROM media_bins WHERE id=?", (int(bin_id),)).fetchone()
        if b and b["project_id"] is not None and scope is not None and int(b["project_id"]) not in {int(p) for p in scope}:
            raise UploadError("bin hors de vos accès", 403)
    done = {"asset_id": asset_id, "project_id": None, "bin_id": None}
    # Média vu SEULEMENT par un partage : le ranger dans un projet le rendrait visible de tous ses
    # membres, hors du partage (même règle que charger un média dans un conteneur : utilisé, pas
    # rattaché, cf. media_sharing.guard_container_load).
    if scope is not None and not mr.asset_visible(str(asset_id), frozenset(int(p) for p in scope)):
        done["shared_only"] = True
        return done
    if project_id:
        mr.link_project(asset_id, int(project_id))
        done["project_id"] = int(project_id)
    if bin_id:
        try:
            mr.bin_add(int(bin_id), [asset_id])
            done["bin_id"] = int(bin_id)
        except ValueError as e:                                            # bin intelligent, ou supprimé
            done["bin_error"] = str(e)
    return done


def check_names(user_id, items, node_id=None, folder="", vmid=None, reserved=(), scope=None):
    """AVANT l'envoi : pour chaque {name, size, mtime, new_name?}, le nom est-il libre dans le dossier ?
      → {node_id, node_name, folder, items:[{name, final, ok, resume, exists, reason, suggestion, error}]}
    `resume` : une session interrompue du même fichier existe — reprise, pas de question.
    `new_name` : nom saisi pour remplacer un nom pris → revalidé (caractères, extension, libre).
    `fp` (optionnel, calculé par la page sur le fichier du poste, static/js/blake2b.js) : médias de
    même contenu déjà dans la base → `duplicates` (copies présentes) + `duplicate_default`.
    `reserved` : noms déjà promis à d'autres fichiers de la même sélection (sans effet depuis le code
    du média dans le nom de fichier : gardé pour les pages ouvertes avant). Le nom de fichier réel
    (codé) est fixé par `start`."""
    node, folder = _resolve_target(node_id, folder, vmid)
    nid = int(node["id"])
    pre = (folder + "/") if folder else ""
    claimed = {pre + clean_name(r) for r in (reserved or ()) if clean_name(r)}
    out = []
    for it in items or []:
        it = it or {}
        raw = str(it.get("name") or "")
        name = clean_name(raw)
        row = {"name": raw, "final": name, "ok": False, "resume": False, "exists": False, "reason": None,
               "suggestion": None, "error": None}
        out.append(row)
        if not name or not ext_ok(name):
            row["error"] = "type de fichier non accepté"
            continue
        try:
            size = int(it.get("size"))
        except (TypeError, ValueError):
            size = -1
        if it.get("new_name") in (None, "") and size > 0:
            old = _resumable(user_id, nid, folder, name, size, _mtime(it.get("mtime")))
            if old:
                row.update(ok=True, resume=True, final=old["name"], received=old["received"])
                claimed.add(old["dest"])
                continue
        fp = str(it.get("fp") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{32}", fp) and it.get("new_name") in (None, ""):
            dups, hidden = same_content(fp, nid, folder, scope=scope)
            if dups or hidden:
                row["duplicates"] = dups
                row["duplicate_hidden"] = hidden
                # « Ne pas l'envoyer » proposé d'abord quand le contenu est DÉJÀ là où il irait (même
                # nœud, même dossier) ; ailleurs (autre dossier de projet, autre nœud, ou hors des
                # projets de l'utilisateur — il ne peut pas s'en servir) l'envoi a un sens.
                row["duplicate_default"] = "skip" if any(d["same_folder"] for d in dups) else "send"
        if it.get("new_name") not in (None, ""):
            try:
                name = validate_rename(it.get("new_name"), raw)
            except UploadError as e:
                row["error"] = str(e)
                continue
            row["final"] = name
        # Plus de « nom déjà pris » : le fichier portera le CODE de son média (`<nom>__<code>.<ext>`,
        # app/media_naming.py), deux médias n'ont jamais le même nom de fichier. Le seul conflit qui
        # reste — le même MÉDIA déjà dans ce dossier — est la question « contenu déjà présent »
        # (`duplicates` ci-dessus). `exists` / `suggestion` restent dans la réponse (toujours faux).
        row["ok"] = True
        claimed.add(pre + name)
    return {"node_id": nid, "node_name": node.get("name"), "folder": folder, "items": out}


def _node_or_raise(node_id):
    from . import node_driver
    node = mr._node(node_id) if node_id is not None else None
    if not node:
        raise UploadError("nœud introuvable", 404)
    if not node_driver.upload_resumable(node):
        raise UploadError("l'agent du nœud %s (%s) ne sait pas recevoir un envoi par morceaux : mettre à jour l'agent "
                          "(≥ %s)" % (node.get("name"), node.get("agent_version") or "?",
                                      ".".join(map(str, node_driver.RESUMABLE_UPLOAD_MIN_AGENT))), 409,
                          need_agent_update=True)
    return node


def _state(node, rel):
    from . import node_driver
    code, st = node_driver.upload_state(node, rel)
    if code != 200:
        raise UploadError("nœud %s : %s" % (node.get("name"), st.get("error") or "état illisible"), 502)
    return st


def _row(sid):
    r = db().execute("SELECT * FROM media_uploads WHERE id=?", (str(sid),)).fetchone()
    return dict(r) if r else None


def _touch(sid, **cols):
    cols["updated_at"] = time.time()
    db().execute("UPDATE media_uploads SET %s WHERE id=?" % ", ".join("%s=?" % k for k in cols),
                 list(cols.values()) + [str(sid)])
    db().commit()


def session_for(sid, user_id, is_admin=False):
    s = _row(sid)
    if not s or (s["user_id"] != user_id and not is_admin):
        raise UploadError("envoi inconnu", 404)
    return s


def public(s, **extra):
    out = {k: s.get(k) for k in ("id", "node_id", "folder", "name", "dest", "size", "mtime", "project_id",
                                 "bin_id", "vmid", "state", "received", "asset_id", "error", "source_name")}
    out["known"] = bool(s.get("known"))
    out["chunk_size"] = CHUNK
    out.update(extra)
    return out


# ─── Ouverture (ou reprise) d'une session ───────────────────────────────────────────────────
def start(user_id, name, size, mtime=None, node_id=None, folder="", vmid=None, project_id=None, bin_id=None,
          source_name=None, fp=None):
    """Ouvre un envoi, ou RETROUVE celui du même fichier (nom + taille + date, même nœud et dossier,
    même utilisateur) resté en cours. Renvoie la session avec `received` = ce que le nœud a vraiment.
    `name` = nom VOULU sur le volume ; `source_name` = nom sur le poste quand l'utilisateur l'a changé
    (nom déjà pris, cf. `check_names`) — la reprise reconnaît l'un ou l'autre."""
    raw = str(name or "")
    name = clean_name(raw)
    source = clean_name(source_name) if source_name else ""
    if source == name:
        source = ""
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise UploadError("taille invalide")
    if not name:
        raise UploadError("nom de fichier invalide")
    if not ext_ok(name):
        raise UploadError("type de fichier non accepté : %s (vidéo, audio ou image : %s)"
                          % (os.path.splitext(name)[1] or name, " ".join(EXTS)), 415, bad_type=True)
    if size <= 0:
        raise UploadError("fichier vide")
    if size > max_bytes():
        raise UploadError("fichier trop gros : %.1f Go (maximum %.0f Go, Réglages → Base des médias)"
                          % (size / (1 << 30), max_bytes() / (1 << 30)), 413, too_big=True, max_bytes=max_bytes())
    mtime = _mtime(mtime)
    # Contexte d'un CONTENEUR : son nœud, le dossier de son projet, son projet.
    if vmid is not None:
        node_id, prefix = mr.container_view(vmid)
        folder = prefix.rstrip("/")
        project_id = project_id or mr.container_context(vmid).get("project_id")
    folder = clean_folder(folder)
    node = _node_or_raise(node_id)
    node_id = int(node["id"])
    purge_stale()
    c = db()
    # Reprise PRIORITAIRE : le même fichier (sous son nom voulu, ou sous son nom du poste) vers la même destination.
    old = _resumable(user_id, node_id, folder, name, size, mtime) or (
        _resumable(user_id, node_id, folder, source, size, mtime) if source else None)
    if old:
        s = dict(old)
        st = _state(node, s["dest"])
        if st.get("exists") and not st.get("partial_size"):
            # Finalisé sur le nœud mais la réponse s'est perdue : on reprend à l'import.
            _touch(s["id"], received=size)
            return public(_row(s["id"]), resumed=True, complete=True)
        _touch(s["id"], received=int(st.get("partial_size") or 0), state="uploading",
               project_id=project_id or s["project_id"], bin_id=bin_id or s["bin_id"])
        return public(_row(s["id"]), resumed=True)
    # Nouvelle session : identité RÉSERVÉE avant le nom de fichier, qui porte son code — celle d'un
    # contenu identique déjà connu (`fp`, calculée par la page : c'est le média auquel l'analyse
    # rattachera le fichier, par la même requête que scan_node), sinon une nouvelle. Le
    # nom codé est libre par construction ; s'il ne l'est pas (même média déjà là sous ce nom, ou
    # fichier déposé à la main) → code à 8, puis suffixe — jamais d'écrasement, signalé par `renamed`.
    from . import media_naming
    import uuid as _uuid
    want, wver = None, None
    fp = str(fp or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{32}", fp):
        want, wver = mr.find_by_fingerprint(fp)      # l'original d'un média, ou une de ses versions
    want = want or str(_uuid.uuid4())
    base = (folder + "/" if folder else "") + name
    busy = _busy_dests(node_id)
    rel = media_naming.pick_name(base, want, lambda r: "" if _taken(node, r, busy) else None, version=wver)
    free = _state(node, rel).get("free")
    if free is not None and free < size + FREE_MARGIN:
        raise UploadError("espace insuffisant sur le volume de %s : %.1f Go libres, %.1f Go nécessaires"
                          % (node.get("name"), free / (1 << 30), (size + FREE_MARGIN) / (1 << 30)), 507, no_space=True)
    sid, now = secrets.token_urlsafe(12), time.time()
    c.execute("""INSERT INTO media_uploads(id, user_id, node_id, folder, name, dest, size, mtime, project_id, bin_id, vmid,
                    state, received, created_at, updated_at, source_name, asset_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,'uploading',0,?,?,?,?)""",
              (sid, user_id, node_id, folder, name, rel, size, mtime, project_id, bin_id, vmid, now, now, source or None, want))
    c.commit()
    return public(_row(sid), resumed=False, renamed=rel != media_naming.coded_name(base, want, version=wver))


# ─── Morceaux ───────────────────────────────────────────────────────────────────────────────
def put_chunk(s, offset, stream, length):
    """Relaie un morceau (lu dans `stream`, `length` octets) au nœud. Décalage ≠ taille réelle du
    `.partial` → UploadError 409 avec `received` = la taille réelle, d'où l'appelant repart."""
    from . import node_driver
    if s["state"] != "uploading":
        raise UploadError("envoi %s" % s["state"], 409, state=s["state"])
    try:
        offset, length = int(offset), int(length)
    except (TypeError, ValueError):
        raise UploadError("offset et longueur requis")
    if length <= 0 or length > CHUNK_MAX or offset < 0 or offset + length > s["size"]:
        raise UploadError("morceau hors bornes (%d octets à %d, fichier de %d)" % (length, offset, s["size"]))
    node = _node_or_raise(s["node_id"])
    code, j = node_driver.upload_chunk(node, s["dest"], offset, s["size"], stream, length)
    if code == 200:
        _touch(s["id"], received=int(j.get("partial_size") or offset + length))
        return int(j.get("partial_size") or offset + length)
    if j.get("partial_size") is not None:
        _touch(s["id"], received=int(j["partial_size"]))
    if code == 409 and j.get("exists"):
        raise UploadError("un fichier existe déjà à %s" % s["dest"], 409, exists=True)
    if code == 409:
        raise UploadError(j.get("error") or "décalage refusé", 409, received=int(j.get("partial_size") or 0),
                          busy=bool(j.get("busy")))
    if code == 507:
        raise UploadError(j.get("error") or "espace insuffisant", 507, no_space=True)
    if code == 0:
        raise UploadError(j.get("error") or "nœud injoignable", 502, retry=True)
    raise UploadError(j.get("error") or "HTTP %d" % code, 502 if code >= 500 else code, retry=True,
                      received=j.get("partial_size"))


# ─── Fin : finalisation sur le nœud, puis import ────────────────────────────────────────────
def finish(s, run_async=True):
    """Finalise sur le nœud puis importe (tâche de fond : l'analyse du volume prend des secondes)."""
    from . import node_driver
    if s["state"] not in ("uploading", "finishing", "failed"):
        return public(s)
    node = _node_or_raise(s["node_id"])
    _touch(s["id"], state="finishing")
    code, j = node_driver.upload_finish(node, s["dest"], s["size"], s["mtime"])
    if code != 200:
        st = _state(node, s["dest"])
        if not (st.get("exists") and not st.get("partial_size")):
            _touch(s["id"], state="uploading", received=int(st.get("partial_size") or 0))
            raise UploadError(j.get("error") or "finalisation refusée", 409, received=int(st.get("partial_size") or 0))
        # Le fichier est là sans `.partial` : finalisation déjà faite (réponse perdue) — on continue.
    _touch(s["id"], state="importing", received=s["size"], error=None)
    if run_async:
        threading.Thread(target=_import, args=(s["id"],), daemon=True, name="media-upload-import").start()
    else:
        _import(s["id"])
    return public(_row(s["id"]))


def _import(sid):
    s = _row(sid)
    try:
        with _import_lock:
            mr.scan_node(s["node_id"], probe_max=50)
            r = mr.db().execute("SELECT asset_id FROM media_files WHERE node_id=? AND share_id=0 AND kind='main' AND path=?",
                                (s["node_id"], s["dest"])).fetchone()
            if not r:
                raise RuntimeError("fichier arrivé mais introuvable à l'analyse : %s" % s["dest"])
            # Déjà rattaché par l'analyse : même empreinte qu'un média de la base (nouvelle copie).
            known = bool(r["asset_id"])
            aid = r["asset_id"] or (mr.import_files(s["node_id"], [s["dest"]], origin="upload", project_id=s["project_id"],
                                                   want_ids={s["dest"]: s["asset_id"]} if s.get("asset_id") else None)
                                    or [None])[0]
            if not aid:
                raise RuntimeError("import en échec : %s" % s["dest"])
            if s["project_id"]:
                mr.link_project(aid, s["project_id"])
            if s["bin_id"]:
                try:
                    mr.bin_add(s["bin_id"], [aid])
                except ValueError as e:                                    # bin intelligent, ou supprimé entre-temps
                    log.info("envoi %s : rangement dans le bin %s ignoré (%s)", sid, s["bin_id"], e)
        _touch(sid, state="done", asset_id=aid, known=1 if known else 0)
    except Exception as e:                                                 # noqa: BLE001
        log.warning("envoi %s : import en échec : %s", sid, e)
        _touch(sid, state="failed", error=str(e)[:500])


def cancel(s):
    """Annule : le `.partial` est effacé sur le nœud. Un fichier déjà finalisé n'est pas touché."""
    from . import node_driver
    if s["state"] in ("done", "cancelled"):
        return public(s)
    node = mr._node(s["node_id"])
    if node:
        node_driver.upload_abort(node, s["dest"])
    _touch(s["id"], state="cancelled")
    return public(_row(s["id"]))


def sessions(user_id, is_admin=False, limit=50):
    """Envois de l'utilisateur, les plus récents d'abord (interrompus compris : la page les propose
    à la reprise — re-choisir le même fichier)."""
    q = "SELECT * FROM media_uploads %s ORDER BY updated_at DESC LIMIT ?" % ("" if is_admin else "WHERE user_id IS ?")
    args = (int(limit),) if is_admin else (user_id, int(limit))
    return [public(dict(r)) for r in db().execute(q, args)]


def purge_stale(now=None):
    """Sessions inactives depuis STALE_S : `.partial` effacé, session abandonnée."""
    from . import node_driver
    now = now or time.time()
    for r in db().execute("SELECT * FROM media_uploads WHERE state IN ('uploading','finishing') AND updated_at < ?",
                          (now - STALE_S,)).fetchall():
        node = mr._node(r["node_id"])
        if node:
            node_driver.upload_abort(node, r["dest"])
        db().execute("UPDATE media_uploads SET state='expired', updated_at=? WHERE id=?", (now, r["id"]))
    db().commit()
