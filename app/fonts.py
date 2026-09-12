# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Bibliothèque de POLICES côté orchestrateur (Réglages → Polices).

Modèle
------
* Le FICHIER de police vit dans `static/uploads/fonts/<sha256>.<ext>` — jamais sous le nom
  fourni par l'utilisateur (qui peut porter des séquences de chemin). `static/uploads/` est
  **préservé par l'updater** (cf. app/updater.py) et par le backup → la bibliothèque survit
  aux mises à jour.
* Les MÉTADONNÉES vivent dans la table `fonts` (sha256 = clé primaire, cf. `init_db`).
* La **clé d'usage** d'une police de la bibliothèque est `lib:<sha256[:16]>` : c'est cette
  chaîne que les configs (overlays multiview, modèles de PiP, layouts) stockent dans leur
  champ `font`. Les polices **embarquées dans l'image runtime** gardent leurs identifiants
  historiques (`dejavu-sans-bold`, `inter`, …, cf. BUILTIN_FONTS) — une clé sans préfixe
  `lib:` désigne toujours une police d'image.

Distribution aux conteneurs
---------------------------
Le rootfs des conteneurs est ÉPHÉMÈRE et l'agent par-conteneur (`POST :8081/deploy`) ne
pousse que du **texte** → on suit le motif déjà éprouvé du LOGO de marque : la police est
**embarquée en base64 dans les params du script** au déploiement (`deploy.py` →
`resolve_params()`), et seules les polices **réellement référencées** par les params sont
injectées (scan récursif des `lib:*`). Le script de plugin matérialise ensuite
`CONFIG["font_library"]` sur son disque local et l'ajoute à sa table de polices.

Contrat côté script de plugin (à implémenter dans le plugin qui rend du texte) :

    CONFIG["font_library"] = [
        {"key": "lib:0123456789abcdef", "name": "Roboto Condensed",
         "family": "Roboto Condensed", "ext": "ttf", "sha256": "...", "b64": "<TTF b64>"},
        …
    ]

Export / import
---------------
`export_bundle(refs)` renvoie les polices utilisées sous la forme
`{name, family, ext, sha256, ttf_b64}` (motif identique aux images d'overlay) ;
`import_bundle(bundle)` **déduplique par HASH, jamais par nom** : sha connu → on référence
la police existante ; sha inconnu → on l'ajoute (nom suffixé en cas de collision) ;
référence ni connue ni embarquée → repli DejaVu + avertissement explicite.
"""

import base64
import hashlib
import io
import logging
import os
import re

log = logging.getLogger(__name__)

from . import config

FONTS_DIR = os.path.join(config.UPLOADS_DIR, "fonts")

# Garde-fous d'upload (un upload de fichier est une surface d'attaque).
MAX_FONT_BYTES = 4 * 1024 * 1024       # 4 Mo par police (une TTF réaliste : 50 Ko – 1,5 Mo)
MAX_LIBRARY_BYTES = 64 * 1024 * 1024   # bibliothèque entière (garde-fou disque)
MAX_INJECT_BYTES = 8 * 1024 * 1024     # total embarqué dans les params d'UN script
ALLOWED_EXTS = ("ttf", "otf", "ttc")
# Signatures sfnt réelles (le type est validé sur le CONTENU, pas sur l'extension) :
#   \x00\x01\x00\x00 TrueType · 'true' TrueType (Apple) · 'OTTO' CFF/OpenType · 'ttcf' collection
_SFNT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf")

# Police de repli (celle qui est codée en dur aujourd'hui dans les scripts de plugin).
DEFAULT_FONT_KEY = "dejavu-sans-bold"

# Polices EMBARQUÉES dans les images runtime — miroir de `_FONT_FILES` du script multiview.
# Sert à alimenter un sélecteur (builtins + bibliothèque) sans interroger les conteneurs.
BUILTIN_FONTS = [
    {"key": "dejavu-sans",          "name": "DejaVu Sans"},
    {"key": "dejavu-sans-bold",     "name": "DejaVu Sans Bold"},
    {"key": "dejavu-serif",         "name": "DejaVu Serif"},
    {"key": "dejavu-mono",          "name": "DejaVu Sans Mono"},
    {"key": "liberation-sans",      "name": "Liberation Sans"},
    {"key": "liberation-sans-bold", "name": "Liberation Sans Bold"},
    {"key": "liberation-mono",      "name": "Liberation Mono"},
    {"key": "inter",                "name": "Inter"},
    {"key": "roboto",               "name": "Roboto"},
    {"key": "firacode",             "name": "Fira Code"},
]
BUILTIN_KEYS = {f["key"] for f in BUILTIN_FONTS}

_KEY_RE = re.compile(r"^lib:([0-9a-f]{16})$")


# ─── Clés & chemins ──────────────────────────────────────────

def font_key(sha256):
    """Clé d'usage d'une police de la bibliothèque (celle stockée dans les configs)."""
    return "lib:" + sha256[:16]


def is_library_key(key):
    return bool(_KEY_RE.match(str(key or "")))


def _sha_prefix(key):
    m = _KEY_RE.match(str(key or ""))
    return m.group(1) if m else None


def font_path(row):
    return os.path.join(FONTS_DIR, "%s.%s" % (row["sha256"], row.get("ext") or "ttf"))


# ─── Bibliothèque ────────────────────────────────────────────

def _public(row):
    """Ligne DB → objet d'API (sans le binaire)."""
    return {"key": font_key(row["sha256"]), "sha256": row["sha256"], "name": row["name"],
            "family": row.get("family") or "", "style": row.get("style") or "",
            "ext": row.get("ext") or "ttf", "size": row.get("size") or 0,
            "created_at": row.get("created_at") or "", "builtin": False,
            "url": "/static/uploads/fonts/%s.%s" % (row["sha256"], row.get("ext") or "ttf")}


def list_fonts():
    """Bibliothèque téléversée (sans les builtins)."""
    from .database import db_list_fonts
    return [_public(r) for r in db_list_fonts()]


def catalog():
    """Catalogue COMPLET pour un sélecteur : polices d'image (builtin) + bibliothèque."""
    builtins = [dict(f, builtin=True, family=f["name"], sha256="", size=0, ext="", url="")
                for f in BUILTIN_FONTS]
    return builtins + list_fonts()


def resolve(key):
    """Ligne DB de la police `key` (`lib:<sha16>`), ou None (clé builtin/inconnue)."""
    from .database import db_list_fonts
    pref = _sha_prefix(key)
    if not pref:
        return None
    for r in db_list_fonts():
        if r["sha256"].startswith(pref):
            return r
    return None


def library_bytes():
    """Taille totale de la bibliothèque (garde-fou disque)."""
    from .database import db_list_fonts
    return sum(int(r.get("size") or 0) for r in db_list_fonts())


# ─── Validation & ajout ──────────────────────────────────────

class FontError(ValueError):
    """Upload refusé (message destiné à l'utilisateur, déjà i18n-isé par l'appelant)."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code          # clé i18n courte : too_big | bad_type | unreadable | library_full
        self.detail = detail


def _probe(data):
    """Valide que `data` est une VRAIE police chargeable, renvoie (family, style).
    Deux barrières : signature sfnt sur le contenu, puis chargement Pillow effectif."""
    if data[:4] not in _SFNT_MAGIC:
        raise FontError("bad_type")
    try:
        from PIL import ImageFont
        f = ImageFont.truetype(io.BytesIO(data), 24)
        name = f.getname()          # (family, style)
        f.getbbox("Ag0")            # rendu réel : une police tronquée échoue ici
    except FontError:
        raise
    except Exception as e:
        raise FontError("unreadable", str(e))
    family = (name[0] or "").strip() if name else ""
    style = (name[1] or "").strip() if name and len(name) > 1 else ""
    return family, style


def _unique_name(base):
    """Nom d'affichage unique dans la bibliothèque (suffixe « (2) », « (3) »… en collision)."""
    from .database import db_list_fonts
    taken = {(r.get("name") or "").lower() for r in db_list_fonts()}
    base = (base or "Police").strip()[:80] or "Police"
    if base.lower() not in taken:
        return base
    for i in range(2, 100):
        cand = "%s (%d)" % (base, i)
        if cand.lower() not in taken:
            return cand
    return "%s (%s)" % (base, os.urandom(2).hex())


def add_font(data, filename="", created_by="", name=None):
    """Ajoute une police à la bibliothèque. DÉDUP PAR HASH : si le sha existe déjà, la police
    existante est renvoyée telle quelle (aucun doublon, aucun renommage). Renvoie
    (font_public, created: bool). Lève FontError si le fichier est refusé."""
    from .database import db_add_font, db_get_font
    if not data:
        raise FontError("unreadable", "fichier vide")
    if len(data) > MAX_FONT_BYTES:
        raise FontError("too_big")
    family, style = _probe(data)

    sha = hashlib.sha256(data).hexdigest()
    existing = db_get_font(sha)
    if existing:
        return _public(existing), False
    if library_bytes() + len(data) > MAX_LIBRARY_BYTES:
        raise FontError("library_full")

    # Extension dérivée du CONTENU (jamais du nom fourni) ; le nom d'origine ne sert qu'à
    # proposer un libellé lisible en repli, et n'est jamais utilisé comme chemin.
    ext = "ttc" if data[:4] == b"ttcf" else ("otf" if data[:4] == b"OTTO" else "ttf")
    label = (name or "").strip() or " ".join(x for x in (family, style) if x).strip()
    if not label:
        label = os.path.splitext(os.path.basename(str(filename)))[0][:80] or "Police"
    label = _unique_name(re.sub(r"[\x00-\x1f]", "", label))

    os.makedirs(FONTS_DIR, exist_ok=True)
    dest = os.path.join(FONTS_DIR, "%s.%s" % (sha, ext))
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    row = db_add_font(sha, label, family, style, ext, len(data), created_by)
    log.info("police ajoutée : %s (%s, %d o)", label, sha[:12], len(data))
    return _public(row), True


def delete_font(key, force=False):
    """Supprime une police. REFUSE (sans `force`) si elle est UTILISÉE — renvoie
    (ok: bool, usage: list). Pas de suppression silencieuse qui casserait un mur."""
    from .database import db_delete_font
    row = resolve(key)
    if not row:
        return False, []
    used = usage(font_key(row["sha256"]))
    if used and not force:
        return False, used
    db_delete_font(row["sha256"])
    try:
        os.remove(font_path(row))
    except OSError:
        pass
    return True, used


# ─── Références (scan récursif des configs) ──────────────────

def collect_refs(obj, out=None):
    """Toutes les clés `lib:<sha16>` référencées quelque part dans une structure JSON
    (params de container, config de layout, config de modèle de PiP…)."""
    if out is None:
        out = set()
    if isinstance(obj, str):
        if is_library_key(obj):
            out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            collect_refs(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            collect_refs(v, out)
    return out


def rewrite_refs(obj, mapping, fallback=DEFAULT_FONT_KEY):
    """Remplace les clés `lib:*` d'une structure par `mapping[key]`. Une clé absente du
    mapping (police ni connue ni embarquée) retombe sur `fallback` (DejaVu) — l'appelant
    doit AVERTIR (cf. import_bundle)."""
    if isinstance(obj, str):
        if is_library_key(obj):
            return mapping.get(obj, fallback)
        return obj
    if isinstance(obj, dict):
        return {k: rewrite_refs(v, mapping, fallback) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rewrite_refs(v, mapping, fallback) for v in obj]
    return obj


def usage(key):
    """Où cette police est-elle utilisée ? Liste de {kind, id, name} (containers déployés,
    modèles de PiP, layouts de multiview). Sert à refuser une suppression destructrice."""
    import json
    from .database import (db_get_containers, db_get_pip_templates, db_get_layouts)
    key = str(key or "")
    out = []
    if not is_library_key(key):
        return out
    for c in db_get_containers() or []:
        try:
            dc = json.loads(c.get("deploy_config") or "{}")
        except Exception:
            continue
        if key in collect_refs(dc):
            out.append({"kind": "container", "id": c.get("vmid"),
                        "name": c.get("hostname") or str(c.get("vmid"))})
    for t in db_get_pip_templates() or []:
        if key in collect_refs(t.get("config")):
            out.append({"kind": "pip_template", "id": t.get("id"), "name": t.get("name")})
    for l in db_get_layouts() or []:
        if key in collect_refs(l.get("config")):
            out.append({"kind": "layout", "id": l.get("id"), "name": l.get("name")})
    return out


# ─── Distribution aux conteneurs (embarqué dans les params) ──

def resolve_params(params):
    """Hook de déploiement : injecte `params["font_library"]` = les polices RÉELLEMENT
    référencées par ces params (base64), pour que le script les matérialise localement
    (rootfs éphémère → on ne peut rien laisser sur le disque du conteneur). Ne touche pas
    aux params quand aucune police de la bibliothèque n'est référencée."""
    refs = collect_refs(params)
    if not refs:
        return params
    lib, total = [], 0
    for key in sorted(refs):
        row = resolve(key)
        if not row:
            log.warning("police %s référencée mais absente de la bibliothèque → repli DejaVu", key)
            continue
        try:
            with open(font_path(row), "rb") as f:
                data = f.read()
        except OSError as e:
            log.warning("police %s illisible (%s) → repli DejaVu", key, e)
            continue
        total += len(data)
        if total > MAX_INJECT_BYTES:
            log.warning("budget polices dépassé (%d o) → %s non injectée", total, key)
            break
        lib.append({"key": font_key(row["sha256"]), "name": row["name"],
                    "family": row.get("family") or "", "ext": row.get("ext") or "ttf",
                    "sha256": row["sha256"],
                    "b64": base64.b64encode(data).decode("ascii")})
    params = dict(params)
    params["font_library"] = lib
    return params


# ─── Export / import (dédup par HASH) ────────────────────────

def export_bundle(obj):
    """Polices utilisées par `obj` (layout, modèle de PiP, projet…), embarquées :
    [{name, family, ext, sha256, ttf_b64}] — même motif que les images d'overlay."""
    out = []
    for key in sorted(collect_refs(obj)):
        row = resolve(key)
        if not row:
            continue
        try:
            with open(font_path(row), "rb") as f:
                data = f.read()
        except OSError:
            continue
        out.append({"name": row["name"], "family": row.get("family") or "",
                    "ext": row.get("ext") or "ttf", "sha256": row["sha256"],
                    "ttf_b64": base64.b64encode(data).decode("ascii")})
    return out


def import_bundle(bundle, refs=(), created_by=""):
    """Importe les polices embarquées d'un export. **Dédup par HASH, pas par nom** :
      · sha déjà en bibliothèque → on référence l'existante (aucun doublon, nom local gardé) ;
      · sha inconnu → ajoutée (nom suffixé en cas de collision de nom) ;
      · référence présente dans `refs` mais ni connue ni embarquée → repli DejaVu + AVERTISSEMENT.
    Renvoie {"mapping": {clé d'origine → clé locale}, "added": [...], "reused": [...],
             "warnings": [{"code": "missing_font", "key": …}]}.
    L'appelant passe le résultat à `rewrite_refs()` avant de persister la config."""
    mapping, added, reused, warnings = {}, [], [], []
    for ent in (bundle or []):
        if not isinstance(ent, dict):
            continue
        b64 = ent.get("ttf_b64") or ent.get("b64") or ""
        declared = str(ent.get("sha256") or "")
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            warnings.append({"code": "bad_font_payload", "name": ent.get("name") or declared[:12]})
            continue
        real = hashlib.sha256(data).hexdigest()
        if declared and declared != real:
            # Le hash annoncé ne correspond pas au contenu : on fait foi au CONTENU (le hash
            # est l'identité), mais on le signale — export corrompu ou trafiqué.
            warnings.append({"code": "font_hash_mismatch", "name": ent.get("name") or ""})
        src_key = font_key(declared) if declared else font_key(real)
        try:
            pub, created = add_font(data, filename=ent.get("name") or "",
                                    created_by=created_by, name=ent.get("name"))
        except FontError as e:
            warnings.append({"code": "font_rejected", "name": ent.get("name") or "",
                             "detail": e.code})
            continue
        mapping[src_key] = pub["key"]
        mapping[font_key(real)] = pub["key"]
        (added if created else reused).append(pub)
    for key in sorted(set(refs) | set()):
        if not is_library_key(key) or key in mapping:
            continue
        row = resolve(key)                       # déjà en bibliothèque (export sans binaire)
        if row:
            mapping[key] = font_key(row["sha256"])
        else:
            warnings.append({"code": "missing_font", "key": key})   # → repli DejaVu
    return {"mapping": mapping, "added": added, "reused": reused, "warnings": warnings}
