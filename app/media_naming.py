# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
"""NOM DE FICHIER d'un média sur le stockage d'un nœud — règle du 2026-10-12.

Un média se désigne par son IDENTITÉ (UUID de `media_assets`) ; son NOM (fiche, libre, homonymes
permis) n'est qu'un affichage. Le NOM DE FICHIER, lui, est technique : tout fichier que le SYSTÈME
crée sur le volume d'un nœud porte le CODE du média à la fin de sa base,

    <base>__<code>.<ext>          code = 6 premiers caractères hexadécimaux de l'identité
                                  (8 si un AUTRE fichier du dossier porte déjà ce nom)

— même média, même code partout : une copie ou un rapatriement garde le même nom de fichier.
Avid OP-Atom : le code va sur la base du clip, avant le suffixe de piste (`<clip>__<code>_v1.mxf`) ;
proxy : `.proxies/<base>__<code>_proxy.mp4`.

VERSIONS d'un média (règle du 2026-10-16) : un transcodage n'est pas un autre média mais une
VERSION du même — même fiche, MÊME code, la clé de la version en suffixe :
`clip__a1b2c3.mov` (l'original), `clip__a1b2c3_h264.mp4`, `clip__a1b2c3_xdcam.mxf`. Une clé de
version (`valid_version`) : minuscules et chiffres, commence par une lettre, 16 caractères au plus,
jamais `original`, `proxy` ni une piste Avid (`v1`, `a12`) — sans quoi le nom serait ambigu. Ne portent PAS de code : les fichiers des partages
réseau (y compris un enregistrement écrit directement sur un partage), les fichiers déjà présents
(rien de rétroactif) et ceux importés d'un volume existant (on ne renomme pas les fichiers de
l'utilisateur).

À la SORTIE du stockage du nœud (export de projet, téléchargement, copie vers un partage), le code
est RETIRÉ, avec le suffixe de version : `export_name`. Le sidecar garde l'identité et la version.

Le recorder et le transcodeur (qui tournent dans leur conteneur, sans ce module) appliquent la MÊME
règle avec leur propre copie de ces quelques lignes ; tests/verif_code_media_nom.py vérifie que les
trois implémentations donnent les mêmes noms.

RÉSOLUTION d'un nom tapé à la main (macros, API, systèmes externes) : `resolve_name` — jamais de
refus pour cause d'homonymes : d'abord le média du projet du conteneur, puis celui présent sur son
nœud, puis le plus récent ; le choix est journalisé.
"""
import logging
import os
import re
import time

log = logging.getLogger(__name__)

CODE_LEN = 6
CODE_LEN_LONG = 8
SEP = "__"
ORIGINAL = "original"
# Suffixes qui restent APRÈS le code : piste Avid OP-Atom (`_v1`, `_a12`) et proxy de lecture (ils
# font partie du nom, même à l'export), ou la clé d'une VERSION (retirée avec le code). L'ordre de
# l'alternative compte : une piste Avid ou `_proxy` n'est jamais lue comme une version.
_VERSION = r"[a-z][a-z0-9]{0,15}"
_TAIL = r"(?:(?P<tail>_[va]\d{1,3}|_proxy)|_(?P<version>" + _VERSION + r"))?"
_CODED = re.compile(r"^(?P<stem>.+?)__(?P<code>[0-9a-f]{8}|[0-9a-f]{6})" + _TAIL + r"(?P<ext>\.[^./]{1,8})?$")
_RESERVED_VERSION = re.compile(r"^(?:original|proxy|[va]\d{1,3})$")
_AVID = re.compile(r"^(?P<stem>.+?)(?P<tail>_[va]\d{1,3})(?P<ext>\.mxf)$", re.I)


def _hex(aid):
    """Chiffres hexadécimaux de l'identité (UUID). Une identité qui n'en a pas assez (importée d'un
    autre système, banc) donne un code tiré de son empreinte : stable, jamais une exception."""
    s = str(aid or "").strip().lower()
    if not s:
        raise ValueError("identité de média vide")
    if re.fullmatch(r"[0-9a-f-]+", s) and len(s.replace("-", "")) >= CODE_LEN_LONG:
        return s.replace("-", "")
    import hashlib
    return hashlib.blake2b(s.encode(), digest_size=8).hexdigest()


def media_code(aid, n=CODE_LEN):
    """Code du média : les `n` premiers caractères hexadécimaux de son identité (UUID)."""
    return _hex(aid)[:n]


def valid_version(key):
    """Clé de version utilisable dans un nom de fichier (cf. l'en-tête). `original` n'en est pas une :
    l'original ne porte pas de suffixe."""
    k = str(key or "")
    return bool(re.fullmatch(_VERSION, k)) and not _RESERVED_VERSION.match(k)


def parse(name):
    """`(stem, code, tail, ext)` d'un nom de fichier codé (base seule, sans dossier), ou None.
    `tail` = piste Avid ou `_proxy` ; la version éventuelle : `version_of`."""
    m = _CODED.match(os.path.basename(str(name or "")))
    if not m:
        return None
    return m.group("stem"), m.group("code"), m.group("tail") or "", m.group("ext") or ""


def version_of(name, aid=None):
    """Clé de version portée par un nom codé (`clip__a1b2c3_h264.mp4` → `h264`), `original` pour un
    nom codé sans version, None pour un nom sans code (ou, avec `aid`, le code d'un autre média)."""
    m = _CODED.match(os.path.basename(str(name or "")))
    if not m or (aid is not None and code_of(name, aid) is None):
        return None
    return m.group("version") or ORIGINAL


def code_of(name, aid=None):
    """Code porté par `name` — None s'il n'en porte pas, ou (avec `aid`) si ce n'est pas celui de ce média."""
    p = parse(name)
    if not p:
        return None
    if aid is not None:
        try:
            if not _hex(aid).startswith(p[1]):
                return None
        except ValueError:
            return None
    return p[1]


def strip_code(name, aid=None):
    """Nom SANS le code du média — ni la clé de version qui le suit (`clip__a1b2c3_h264.mp4` →
    `clip.mp4`). Avec `aid`, seul le code de CE média est retiré (un fichier de l'utilisateur qui
    finirait par `__abcdef` sans rapport avec le média n'est pas touché). Le dossier éventuel est
    conservé."""
    d, b = os.path.split(str(name or ""))
    p = parse(b)
    if not p or (aid is not None and code_of(b, aid) is None):
        return name
    stem, _code, tail, ext = p
    return os.path.join(d, stem + tail + ext) if d else stem + tail + ext


def fixed_suffix(name, aid=None):
    """Partie AUTOMATIQUE d'un nom codé, que l'humain ne réécrit jamais : `__<code>`, la piste Avid,
    `_proxy` ou la clé de version, et l'extension (`clip__a1b2c3_h264.mp4` → `__a1b2c3_h264.mp4`).
    "" pour un nom sans code (ou, avec `aid`, le code d'un autre média)."""
    b = os.path.basename(str(name or ""))
    m = _CODED.match(b)
    if not m or (aid is not None and code_of(b, aid) is None):
        return ""
    return b[len(m.group("stem")):]


def rename_stem(old, new, aid=None):
    """Nouveau nom d'un fichier codé quand l'humain le renomme : seule la BASE change, la partie
    automatique (`fixed_suffix`) est reprise de l'ancien nom telle quelle. `new` peut être la base
    seule ou un nom complet (son code, sa version et son extension éventuels sont ignorés).
    Nom sans code : `new` tel quel."""
    fixed = fixed_suffix(old, aid)
    new = os.path.basename(str(new or "").strip())
    if not fixed:
        return new
    m = _CODED.match(new)
    if m:
        new = m.group("stem")
    else:
        ext = os.path.splitext(fixed)[1]
        if ext and new.lower().endswith(ext.lower()):
            new = new[:-len(ext)]
    new = new.rstrip("_ ")
    return (new + fixed) if new else ""


def coded_name(name, aid, n=CODE_LEN, avid=False, proxy=False, version=None):
    """`<base>__<code><ext>` (dossier conservé). Un code déjà présent est d'abord retiré (le nom
    d'une SOURCE codée ne se cumule pas : `clip__a1b2c3_tc` → `clip_tc__d4e5f6`, pas deux codes).
    `avid` : le suffixe de piste `_v1`/`_aN` reste en fin de nom (`<clip>__<code>_v1.mxf`) ;
    `proxy` : `<base>__<code>_proxy<ext>` ; `version` (autre que l'original) :
    `<base>__<code>_<version><ext>` — ValueError pour une clé invalide."""
    d, b = os.path.split(str(name or ""))
    b = strip_code(b)
    stem, ext = os.path.splitext(b)
    tail = ""
    if avid:
        m = _AVID.match(b)
        if m:
            stem, tail, ext = m.group("stem"), m.group("tail"), m.group("ext")
    if proxy:
        tail = "_proxy"
    elif version and version != ORIGINAL:
        if not valid_version(version):
            raise ValueError("clé de version invalide : %r" % version)
        tail = "_" + version
    out = "%s%s%s%s%s" % (stem, SEP, media_code(aid, n), tail, ext)
    return os.path.join(d, out) if d else out


def pick_name(name, aid, taken, avid=False, proxy=False, version=None):
    """Nom codé LIBRE pour le média `aid` : code à 6, à 8 si un AUTRE média occupe déjà ce nom.
    `taken(candidat)` → identité du média qui porte déjà ce nom (chaîne), "" pour un fichier
    d'identité inconnue, None si le nom est libre. Le même média à la même place → même nom (copie
    déjà là). Ultime recours (code à 8 caractères lui aussi pris) : suffixe `_2`, `_3`… avant le code."""
    for n in (CODE_LEN, CODE_LEN_LONG):
        cand = coded_name(name, aid, n, avid=avid, proxy=proxy, version=version)
        owner = taken(cand)
        if owner is None or (owner and str(owner).lower() == str(aid).lower()):
            return cand
    d, b = os.path.split(str(name or ""))
    stem, ext = os.path.splitext(strip_code(b))
    for i in range(2, 1000):
        cand = coded_name(os.path.join(d, "%s_%d%s" % (stem, i, ext)), aid, CODE_LEN_LONG, avid=avid, proxy=proxy,
                          version=version)
        owner = taken(cand)
        if owner is None or (owner and str(owner).lower() == str(aid).lower()):
            return cand
    raise RuntimeError("aucun nom libre pour %s" % name)


def name_on_node(name, aid, src_name=None, version=None):
    """Nom d'une COPIE ou d'un RAPATRIEMENT du média sur un nœud : celui de la source s'il porte
    déjà le code de ce média (même nom partout), sinon le nom codé à 6 (avec la clé de `version`)."""
    if src_name and code_of(src_name, aid):
        return os.path.join(os.path.dirname(name), os.path.basename(src_name)) if os.path.dirname(name) \
            else os.path.basename(src_name)
    return coded_name(name, aid, version=version)


def version_file_name(src_name, aid, version, ext, n=None):
    """Nom de la VERSION `version` du média `aid`, tirée du fichier source `src_name` (dossier
    conservé) : base de la source sans code, MÊME longueur de code que la source quand elle porte
    celui de ce média (sinon `n`, 6 par défaut), extension `ext` de la version
    (`rec/clip__a1b2c3.mov` + h264 + .mp4 → `rec/clip__a1b2c3_h264.mp4`)."""
    d, b = os.path.split(str(src_name or ""))
    code = code_of(b, aid)
    stem = os.path.splitext(strip_code(b, aid) if code else b)[0]
    return coded_name(os.path.join(d, stem + ext) if d else stem + ext, aid,
                      len(code) if code else (n or CODE_LEN), version=version)


def export_name(name, aid=None):
    """Nom à la SORTIE du stockage du nœud (archive, téléchargement, partage) : le code retiré, et
    la clé de version avec lui."""
    return strip_code(name, aid)


def media_name(filename, aid=None):
    """Nom de FICHE par défaut d'un média qui entre dans la base : le nom du fichier sans le code
    (l'extension reste, comme avant la règle)."""
    return strip_code(os.path.basename(str(filename or "")), aid)


# ─── Résolution d'un NOM tapé à la main ─────────────────────────────────────────────────────
_last_alert = {}
_ALERT_KEY = {"project": "alert.media.name_ambiguous_project", "node": "alert.media.name_ambiguous_node",
              "recent": "alert.media.name_ambiguous_recent"}
_WHY = {"project": "projet du conteneur", "node": "présent sur son nœud", "recent": "le plus récent"}
ALERT_EVERY_S = 3600


def candidates(name, c):
    """Médias que `name` peut désigner : identité exacte, ou nom / titre égal (casse ignorée), ou
    nom de FICHIER d'une copie (avec ou sans code). Un nom codé désigne d'abord le média de ce code."""
    ref = str(name or "").strip()
    if not ref:
        return []
    r = c.execute("SELECT id FROM media_assets WHERE id=?", (ref,)).fetchone()
    if r:
        return [r["id"]]
    code = code_of(ref)
    if code:
        rows = [x["id"] for x in c.execute("SELECT id FROM media_assets WHERE REPLACE(LOWER(id),'-','') LIKE ?",
                                           (code + "%",))]
        bare = strip_code(ref)
        named = [a for a in rows if c.execute(
            "SELECT 1 FROM media_assets WHERE id=? AND LOWER(name)=LOWER(?)", (a, bare)).fetchone()
            or c.execute("SELECT 1 FROM media_files WHERE asset_id=? AND (path=? OR path LIKE ?)",
                         (a, ref, "%/" + ref)).fetchone()]
        if named:
            return named
        ref = bare
    ids = [x["id"] for x in c.execute(
        "SELECT id FROM media_assets WHERE LOWER(name)=LOWER(?) OR LOWER(COALESCE(json_extract(meta_json, '$.title'),''))=LOWER(?)",
        (ref, ref))]
    if not ids:
        ids = sorted({x["asset_id"] for x in c.execute(
            "SELECT DISTINCT asset_id FROM media_files WHERE asset_id IS NOT NULL AND kind='main' AND (path=? OR path LIKE ?)",
            (ref, "%/" + ref))})
    return ids


def resolve_name(name, c, project_id=None, node_id=None, vmid=None, journal=True, only_project=False):
    """Identité désignée par `name` → (asset_id, note). Jamais de refus pour cause d'homonymes :
      1. le média du PROJET du conteneur (`project_id`) ;
      2. puis celui qui a une copie PRÉSENTE sur son NŒUD (`node_id`) ;
      3. puis le plus RÉCENT (date de création).
    Plusieurs candidats → `note` = « N médias portent ce nom ; pris : … », journalisée (alerte
    d'information, une fois l'heure par nom et choix). Aucun → (None, None). `only_project` : seuls
    les médias du projet comptent (macro d'un projet : elle ne désigne rien hors de lui)."""
    ids = candidates(name, c)
    if only_project and project_id:
        ids = [a for a in ids if c.execute("SELECT 1 FROM project_media WHERE project_id=? AND asset_id=?",
                                           (int(project_id), a)).fetchone()]
    if not ids:
        return None, None
    if len(ids) == 1:
        return ids[0], None

    def key(aid):
        a = c.execute("SELECT created_at FROM media_assets WHERE id=?", (aid,)).fetchone()
        in_proj = bool(project_id) and c.execute("SELECT 1 FROM project_media WHERE project_id=? AND asset_id=?",
                                                 (int(project_id), aid)).fetchone() is not None
        on_node = node_id is not None and c.execute(
            "SELECT 1 FROM media_files WHERE asset_id=? AND node_id=? AND share_id=0 AND state='present' AND kind='main'",
            (aid, int(node_id))).fetchone() is not None
        return (0 if in_proj else 1, 0 if on_node else 1, -_ts(a["created_at"] if a else None))
    ids.sort(key=key)
    chosen = ids[0]
    k = key(chosen)
    why = "project" if k[0] == 0 else ("node" if k[1] == 0 else "recent")
    note = "%d médias portent ce nom « %s » ; pris : %s (%s)" % (len(ids), name, chosen, _WHY[why])
    log.info("médias : %s", note)
    if journal:
        _journal(name, chosen, len(ids), why, vmid)
    return chosen, note


def _ts(v):
    if v in (None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _journal(name, chosen, n, why, vmid):
    now = time.time()
    k = (str(name).lower(), chosen)
    if now - _last_alert.get(k, 0) < ALERT_EVERY_S:
        return
    _last_alert[k] = now
    try:
        from .database import db_add_alert
        db_add_alert(_ALERT_KEY[why], "info", vmid=vmid,
                     params={"n": n, "name": name, "id": chosen, "code": media_code(chosen)})
    except Exception as e:                                                 # noqa: BLE001
        log.warning("médias : journal de résolution par nom : %s", e)
