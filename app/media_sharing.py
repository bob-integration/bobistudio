# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""PARTAGE d'un média ou d'un bin avec un utilisateur, un groupe ou les membres d'un projet
(décision du 2026-10-12 : « cela permet de donner l'accès à des médias hors projets »).

Le partage ÉTEND la portée de la base des médias (cf. docs/chantiers/MEDIAS_REGISTRE.md, « Portée
par projets ») — il n'en crée pas une seconde. `user_scope()` remplace `auth.scoped_project_ids()`
pour la base des médias : il renvoie None (accès global, rien ne change) ou un `MediaScope`, qui EST
l'ensemble des projets de l'utilisateur (toutes les fonctions de portée existantes le lisent comme
avant) et PORTE en plus les médias et les bins qu'on lui a partagés. Les fonctions de portée de
app/media_registry.py (`scope_sql`, `asset_visible`, `visible_ids`, `_visible_bin_ids`) lisent ces
deux attributs : listes, recherche, compteurs, fiche, vignette, proxy, planche… suivent d'elles-mêmes.

NIVEAUX : « use » (Consulter et utiliser : voir, lire, proxy, charger dans ses conteneurs,
rapatrier, mettre en playlist) et « edit » (Modifier : en plus, la fiche — métadonnées, vignette,
proxy, planche — et, pour un bin, y ranger / en retirer des médias). JAMAIS par un partage :
supprimer le média ou une copie, renommer ses fichiers, le rattacher à un projet, gérer ses
partages. Ces gestes restent au PÉRIMÈTRE PROPRIÉTAIRE : les projets de l'utilisateur (portée de
base, `base_scope`) ou l'accès global.

QUI PARTAGE : permission `media.manage` ET le média / le bin dans SON périmètre propriétaire (un
partage reçu ne se repartage pas — c'est aussi ce qui coupe toute récursion).

BIN PARTAGÉ = le bin VIVANT : ses sous-bins, et ce qui y entre ensuite. Il est évalué à chaque
requête dans la portée de base du PARTAGEUR, telle qu'elle est MAINTENANT : un sous-bin qu'il ne voit
pas n'est pas partagé, un bin intelligent ne livre que les résultats que LUI verrait, un média
retiré de ses projets sort du partage. Le partageur supprimé (ou qui ne voit plus le bin / le média)
rend son partage inerte. Partage par une macro sans utilisateur : la portée du projet de la macro
(macro système : globale) est figée dans le partage.

DATE DE FIN facultative ; retrait immédiat (aucun cache entre requêtes) ; JOURNAL `media_share_log`
(qui a partagé quoi avec qui, quand ; modifications, retraits, expirations).

Tables dans la base des médias (`db_medias.db`, à côté de ce qu'elles désignent), créées ici,
idempotentes. Les destinataires (utilisateurs, groupes, projets) vivent dans la base principale :
référence par identifiant, comme `project_media`."""
import json
import threading
import time
from datetime import datetime

from . import media_registry as mr

LEVELS = ("use", "edit")
TARGETS = ("asset", "bin")
RECIPIENTS = ("user", "group", "project")
_RANK = {"use": 1, "edit": 2}

_ready = set()
_ready_lock = threading.Lock()


def db():
    c = mr.db()
    key = (mr.MEDIA_DB_PATH, id(c))
    if key not in _ready:
        with _ready_lock:
            c.execute("""CREATE TABLE IF NOT EXISTS media_shares (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                target_kind    TEXT NOT NULL,
                target_id      TEXT NOT NULL,
                recipient_kind TEXT NOT NULL,
                recipient_id   INTEGER NOT NULL,
                level          TEXT NOT NULL DEFAULT 'use',
                sharer_uid     INTEGER,
                sharer_name    TEXT,
                sharer_projects TEXT,
                created_at     TEXT,
                updated_at     TEXT,
                expires_at     TEXT,
                UNIQUE (target_kind, target_id, recipient_kind, recipient_id))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_media_shares_rcpt ON media_shares(recipient_kind, recipient_id)")
            c.execute("""CREATE TABLE IF NOT EXISTS media_share_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                at             TEXT NOT NULL,
                actor          TEXT,
                action         TEXT NOT NULL,
                target_kind    TEXT,
                target_id      TEXT,
                target_name    TEXT,
                recipient_kind TEXT,
                recipient_id   INTEGER,
                recipient_name TEXT,
                level          TEXT,
                expires_at     TEXT)""")
            c.commit()
            _ready.add(key)
    return c


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ─── La portée étendue ──────────────────────────────────────────────────────────────────────
class MediaScope(frozenset):
    """Portée d'un utilisateur RESTREINT : l'ensemble de SES projets (c'est ce que lit tout le code
    de portée existant), plus `assets` {identité: niveau} et `bins` {bin: niveau} partagés avec lui,
    et de quoi marquer l'interface (`asset_by`, `bin_by` : nom du partageur ; `roots` : les partages
    tels qu'adressés). Toute opération ensembliste rend un frozenset NU : la portée de base."""

    def __new__(cls, projects=(), assets=None, bins=None, asset_by=None, bin_by=None, roots=None):
        s = super().__new__(cls, {int(p) for p in projects})
        s.assets = dict(assets or {})
        s.bins = dict(bins or {})
        s.asset_by = dict(asset_by or {})
        s.bin_by = dict(bin_by or {})
        s.roots = roots or {"assets": [], "bins": []}
        return s


def base_scope(scope):
    """Périmètre PROPRIÉTAIRE : les projets seuls, sans rien de partagé (None = accès global)."""
    return None if scope is None else frozenset(int(p) for p in scope)


def _user_base(u):
    """None (accès global) ou l'ensemble des projets de l'utilisateur."""
    from .auth import has_global_access
    from .database import db_user_projects
    if has_global_access(u):
        return None
    return frozenset(p["id"] for p in db_user_projects(u["id"]))


def user_scope(user=None):
    """Portée de la base des médias pour l'utilisateur courant (ou `user`) : None = accès global,
    sinon un MediaScope. Calculée une fois par requête (flask.g), jamais gardée d'une requête à
    l'autre : un retrait vaut tout de suite."""
    from .auth import current_user
    try:
        from flask import g, has_request_context
        in_req = has_request_context()
    except ImportError:                                                   # pragma: no cover
        in_req = False
    u = user if user is not None else current_user()
    if not u:
        return MediaScope()
    if in_req and user is None:
        cached = getattr(g, "_media_scope", None)
        if cached is not None and cached[0] == u.get("id"):
            return cached[1]
    base = _user_base(u)
    sc = None if base is None else build_scope(u["id"], base)
    if in_req and user is None:
        g._media_scope = (u.get("id"), sc)
    return sc


def _recipients_of(uid, base):
    from . import user_groups
    out = [("user", int(uid))] + [("group", gid) for gid in user_groups.groups_of_user(uid)]
    if base is None:
        from .database import db_user_projects
        out += [("project", p["id"]) for p in db_user_projects(int(uid))]
    else:
        out += [("project", int(p)) for p in base]
    return out


def _active_rows(recipients):
    if not recipients:
        return []
    purge_expired()
    sql = " OR ".join("(recipient_kind=? AND recipient_id=?)" for _ in recipients)
    args = [x for r in recipients for x in r]
    return [dict(r) for r in db().execute(
        "SELECT * FROM media_shares WHERE (expires_at IS NULL OR expires_at > ?) AND (%s) ORDER BY id" % sql,
        [_now()] + args)]


_INERT = object()


def _sharer_scope(row, memo):
    """Portée de base du partageur, MAINTENANT : None = globale ; _INERT = partageur disparu."""
    key = ("u", row["sharer_uid"]) if row["sharer_uid"] is not None else ("p", row["sharer_projects"])
    if key in memo:
        return memo[key]
    if row["sharer_uid"] is not None:
        from .database import db_get_user_by_id
        u = db_get_user_by_id(int(row["sharer_uid"]))
        v = _INERT if not u else _user_base(u)
    else:
        try:
            p = json.loads(row["sharer_projects"]) if row["sharer_projects"] else None
        except (TypeError, ValueError):
            p = []
        v = None if p is None else frozenset(int(x) for x in p)
    memo[key] = v
    return v


def bin_content(bin_id, sharer):
    """(bins, médias) d'un bin partagé, évalués dans la portée de base `sharer` du partageur :
    le bin et ses sous-bins QU'IL VOIT ; leurs médias (manuels) et résultats (intelligents) bornés
    à ce qu'il voit. Bin invisible pour lui : rien."""
    vis = None if sharer is None else mr._visible_bin_ids(sharer)
    if vis is not None and int(bin_id) not in vis:
        return [], set()
    c = mr.db()
    bins, ids = [], set()
    for b in mr.bin_subtree(bin_id):
        if vis is not None and b not in vis:
            continue
        r = c.execute("SELECT query_json FROM media_bins WHERE id=?", (b,)).fetchone()
        if not r:
            continue
        bins.append(b)
        if r["query_json"]:
            try:
                ids |= {a["id"] for a in mr.search(json.loads(r["query_json"]), limit=100000, scope=sharer)}
            except (ValueError, TypeError):
                continue                 # recherche devenue invalide (champ retiré) : rien
        else:
            ids |= {x["asset_id"] for x in c.execute("SELECT asset_id FROM media_bin_items WHERE bin_id=?", (b,))}
    if sharer is not None:
        ids = set(mr.visible_ids(sorted(ids), sharer))
    return bins, ids


def _evaluate(uid, base):
    """Ce qui est partagé avec l'utilisateur (directement, par ses groupes, par ses projets)."""
    assets, bins, asset_by, bin_by = {}, {}, {}, {}
    roots = {"assets": [], "bins": []}
    memo = {}

    def up(d, k, lvl):
        if _RANK.get(lvl, 0) > _RANK.get(d.get(k), 0):
            d[k] = lvl
    for row in _active_rows(_recipients_of(uid, base)):
        if row["sharer_uid"] is not None and int(row["sharer_uid"]) == int(uid):
            continue
        sharer = _sharer_scope(row, memo)
        if sharer is _INERT:
            continue
        who = row["sharer_name"] or ""
        lvl = row["level"] if row["level"] in LEVELS else "use"
        if row["target_kind"] == "asset":
            aid = row["target_id"]
            if not mr.asset_visible(aid, sharer):
                continue
            up(assets, aid, lvl)
            asset_by.setdefault(aid, who)
            roots["assets"].append({"share_id": row["id"], "asset_id": aid, "level": lvl, "by": who,
                                    "expires_at": row["expires_at"]})
        elif row["target_kind"] == "bin":
            try:
                bid = int(row["target_id"])
            except (TypeError, ValueError):
                continue
            sub, ids = bin_content(bid, sharer)
            if not sub:
                continue
            for b in sub:
                up(bins, b, lvl)
                bin_by.setdefault(b, who)
            for a in ids:
                up(assets, a, "use")              # un bin modifiable ne rend pas ses médias modifiables
                asset_by.setdefault(a, who)
            roots["bins"].append({"share_id": row["id"], "bin_id": bid, "level": lvl, "by": who,
                                  "expires_at": row["expires_at"]})
    return assets, bins, asset_by, bin_by, roots


def build_scope(uid, base):
    assets, bins, asset_by, bin_by, roots = _evaluate(uid, base)
    return MediaScope(base, assets, bins, asset_by, bin_by, roots)


# ─── Niveau d'un utilisateur sur un média / un bin ──────────────────────────────────────────
def owns_asset(scope, aid):
    """Le média est dans le périmètre PROPRIÉTAIRE (ses projets, ou accès global)."""
    return mr.asset_visible(aid, base_scope(scope))


def owns_bin(scope, bid):
    return mr.bin_visible(bid, base_scope(scope))


def asset_allows(scope, aid, need):
    """`need` : "use" | "edit" | "owner". Accès global : tout (si le média existe)."""
    if scope is None or owns_asset(scope, aid):
        return mr.asset_visible(aid, scope)
    lvl = getattr(scope, "assets", {}).get(aid)
    return bool(lvl) and need != "owner" and _RANK[lvl] >= _RANK.get(need, 99)


def bin_allows(scope, bid, need):
    try:
        bid = int(bid)
    except (TypeError, ValueError):
        return False
    if scope is None or owns_bin(scope, bid):
        return mr.bin_visible(bid, scope)
    lvl = getattr(scope, "bins", {}).get(bid)
    return bool(lvl) and need != "owner" and _RANK[lvl] >= _RANK.get(need, 99)


# ─── Charger un média partagé dans un conteneur ─────────────────────────────────────────────
# Charger / rapatrier / envoyer un média dans le conteneur d'un projet l'y RATTACHE d'habitude
# (mr.link_container_project : l'usage fait l'appartenance). Pour un média vu SEULEMENT par un
# partage, ce rattachement le rendrait visible de tous les membres du projet du conteneur : il
# sortirait du partage. Décision : il est chargé, PAS rattaché. Les routes marquent (média,
# conteneur) avant d'agir ; mr.link_container_project consomme la marque. Durée bornée (un
# rapatriement long se fait en tâche de fond, hors de la requête).
NO_LINK_TTL_S = 6 * 3600
_no_link = {}
_no_link_lock = threading.Lock()


def mark_no_link(aid, vmid):
    with _no_link_lock:
        now = time.time()
        for k in [k for k, t in _no_link.items() if t < now]:
            _no_link.pop(k, None)
        _no_link[(str(aid), int(vmid))] = now + NO_LINK_TTL_S


def link_suppressed(aid, vmid):
    """True (et la marque est consommée) si ce rattachement doit être sauté."""
    try:
        key = (str(aid), int(vmid))
    except (TypeError, ValueError):
        return False
    with _no_link_lock:
        t = _no_link.pop(key, None)
    return t is not None and t >= time.time()


def guard_container_load(scope, aids, vmid):
    """Avant de charger `aids` dans le conteneur `vmid` : marque ceux que l'utilisateur ne voit que
    par un partage. Renvoie leur nombre."""
    if scope is None or vmid is None:
        return 0
    n = 0
    for aid in aids:
        if aid and not owns_asset(scope, aid) and aid in getattr(scope, "assets", {}):
            mark_no_link(aid, vmid)
            n += 1
    return n


# ─── Créer, modifier, retirer ───────────────────────────────────────────────────────────────
def _parse_expiry(v):
    """'' / None → pas de fin ; 'AAAA-MM-JJ' → fin de ce jour ; 'AAAA-MM-JJTHH:MM[:SS]'."""
    if v in (None, "", 0):
        return None
    s = str(v).strip().replace(" ", "T")
    try:
        if len(s) == 10:
            d = datetime.fromisoformat(s + "T23:59:59")
        else:
            d = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError("expires_invalid")
    d = d.replace(tzinfo=None, microsecond=0)
    if d.isoformat() <= _now():
        raise ValueError("expires_past")
    return d.isoformat(timespec="seconds")


def target_name(kind, tid):
    c = mr.db()
    if kind == "asset":
        r = c.execute("SELECT name FROM media_assets WHERE id=?", (str(tid),)).fetchone()
    else:
        try:
            r = c.execute("SELECT name FROM media_bins WHERE id=?", (int(tid),)).fetchone()
        except (TypeError, ValueError):
            r = None
    return r["name"] if r else None


def recipient_name(kind, rid):
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return None
    if kind == "user":
        from .database import db_get_user_by_id
        from .user_groups import display_name
        u = db_get_user_by_id(rid)
        return display_name(u) if u else None
    if kind == "group":
        from .user_groups import group_names
        return group_names([rid]).get(rid)
    if kind == "project":
        from .database import db_get_project
        p = db_get_project(rid)
        return (p or {}).get("name") if p else None
    return None


def _log(actor, action, row, tname=None, rname=None):
    db().execute("""INSERT INTO media_share_log(at, actor, action, target_kind, target_id, target_name, recipient_kind,
                    recipient_id, recipient_name, level, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                 (_now(), actor or "", action, row["target_kind"], str(row["target_id"]),
                  tname if tname is not None else target_name(row["target_kind"], row["target_id"]),
                  row["recipient_kind"], row["recipient_id"],
                  rname if rname is not None else recipient_name(row["recipient_kind"], row["recipient_id"]),
                  row.get("level"), row.get("expires_at")))


def share(target_kind, target_id, recipient_kind, recipient_id, level="use", expires_at=None,
          sharer_uid=None, sharer_name="", sharer_projects=None):
    """Crée (ou met à jour : niveau, date de fin) le partage. Les DROITS du partageur sont vérifiés
    par l'appelant (`check_can_share`). Renvoie la ligne."""
    if target_kind not in TARGETS:
        raise ValueError("target_invalid")
    if recipient_kind not in RECIPIENTS:
        raise ValueError("recipient_invalid")
    if level not in LEVELS:
        raise ValueError("level_invalid")
    tid = str(target_id)
    tname = target_name(target_kind, tid)
    if tname is None:
        raise LookupError("target_unknown")
    rname = recipient_name(recipient_kind, recipient_id)
    if rname is None:
        raise LookupError("recipient_unknown")
    if recipient_kind == "user" and sharer_uid is not None and int(recipient_id) == int(sharer_uid):
        raise ValueError("recipient_self")
    exp = _parse_expiry(expires_at)
    c = db()
    old = c.execute("SELECT * FROM media_shares WHERE target_kind=? AND target_id=? AND recipient_kind=? AND recipient_id=?",
                    (target_kind, tid, recipient_kind, int(recipient_id))).fetchone()
    now = _now()
    sp = None if sharer_uid is not None else json.dumps(None if sharer_projects is None else sorted(int(p) for p in sharer_projects))
    if old:
        c.execute("""UPDATE media_shares SET level=?, expires_at=?, sharer_uid=?, sharer_name=?, sharer_projects=?,
                     updated_at=? WHERE id=?""", (level, exp, sharer_uid, sharer_name or "", sp, now, old["id"]))
        sid, action = old["id"], "update"
    else:
        sid = c.execute("""INSERT INTO media_shares(target_kind, target_id, recipient_kind, recipient_id, level, sharer_uid,
                           sharer_name, sharer_projects, created_at, updated_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (target_kind, tid, recipient_kind, int(recipient_id), level, sharer_uid, sharer_name or "", sp,
                         now, now, exp)).lastrowid
        action = "share"
    row = dict(c.execute("SELECT * FROM media_shares WHERE id=?", (sid,)).fetchone())
    _log(sharer_name, action, row, tname, rname)
    c.commit()
    return row


def get_share(share_id):
    try:
        r = db().execute("SELECT * FROM media_shares WHERE id=?", (int(share_id),)).fetchone()
    except (TypeError, ValueError):
        return None
    return dict(r) if r else None


def revoke(share_id, actor=""):
    row = get_share(share_id)
    if not row:
        return False
    c = db()
    c.execute("DELETE FROM media_shares WHERE id=?", (row["id"],))
    _log(actor, "revoke", row)
    c.commit()
    return True


def purge_expired():
    """Retire les partages arrivés à échéance (journalisés « expire »)."""
    c = db()
    rows = [dict(r) for r in c.execute("SELECT * FROM media_shares WHERE expires_at IS NOT NULL AND expires_at <= ?", (_now(),))]
    if not rows:
        return 0
    for r in rows:
        c.execute("DELETE FROM media_shares WHERE id=?", (r["id"],))
        _log("", "expire", r)
    c.commit()
    return len(rows)


def check_can_share(scope, perms, target_kind, target_id, level="use"):
    """None si l'utilisateur peut partager (ou retirer un partage de) cette cible, sinon la raison.
    `media.manage` ET la cible dans son périmètre PROPRIÉTAIRE (un partage reçu ne se repartage
    pas) ; « Modifier » : qu'il ait lui-même ce droit (le propriétaire l'a toujours)."""
    if "media.manage" not in (perms or ()):
        return "missing_permission"
    if target_kind == "asset":
        ok = mr.asset_visible(str(target_id), base_scope(scope))
    elif target_kind == "bin":
        ok = mr.bin_visible(target_id, base_scope(scope))
    else:
        return "target_invalid"
    if not ok:
        return "not_owner"
    if level not in LEVELS:
        return "level_invalid"
    return None


def _decorate(rows):
    now = _now()
    for r in rows:
        r["recipient_name"] = recipient_name(r["recipient_kind"], r["recipient_id"])
        r["inert"] = r["recipient_name"] is None
        r["expired"] = bool(r.get("expires_at")) and r["expires_at"] <= now
    return rows


def shares_of(target_kind, target_id):
    """Partages d'un média ou d'un bin (pour son propriétaire), destinataires nommés."""
    purge_expired()
    return _decorate([dict(r) for r in db().execute(
        "SELECT * FROM media_shares WHERE target_kind=? AND target_id=? ORDER BY recipient_kind, id",
        (target_kind, str(target_id)))])


def log_of(target_kind=None, target_id=None, limit=200):
    sql, args = "SELECT * FROM media_share_log", []
    if target_kind:
        sql += " WHERE target_kind=? AND target_id=?"
        args = [target_kind, str(target_id)]
    return [dict(r) for r in db().execute(sql + " ORDER BY id DESC LIMIT ?", args + [int(limit)])]


def shared_with_me(uid, scope):
    """« Partagés avec moi » : les partages tels qu'adressés (bins, médias), nommés. Un utilisateur
    à accès global voit tout de toute façon ; on lui liste quand même ce qu'on lui a adressé."""
    if scope is None:
        _a, _b, _ab, _bb, roots = _evaluate(uid, None)
    else:
        roots = getattr(scope, "roots", None) or {"assets": [], "bins": []}
    out = {"bins": [], "assets": []}
    seen = set()
    for r in sorted(roots["bins"], key=lambda x: -_RANK[x["level"]]):
        if r["bin_id"] in seen:
            continue
        seen.add(r["bin_id"])
        nm = target_name("bin", r["bin_id"])
        if nm is not None:
            out["bins"].append(dict(r, name=nm))
    seen = set()
    for r in sorted(roots["assets"], key=lambda x: -_RANK[x["level"]]):
        if r["asset_id"] in seen:
            continue
        seen.add(r["asset_id"])
        nm = target_name("asset", r["asset_id"])
        if nm is not None:
            out["assets"].append(dict(r, name=nm))
    out["bins"].sort(key=lambda x: (x["name"] or "").lower())
    out["assets"].sort(key=lambda x: (x["name"] or "").lower())
    return out


# ─── Marquage de l'interface ────────────────────────────────────────────────────────────────
def annotate_assets(assets, scope):
    """Chaque média vu SEULEMENT par un partage reçoit `shared: {by, level}` (l'interface le marque
    discrètement, avec le nom du partageur)."""
    if scope is None or not getattr(scope, "assets", None):
        return assets
    base = base_scope(scope)
    cand = [a["id"] for a in assets if a.get("id") in scope.assets]
    owned = set(mr.visible_ids(cand, base)) if cand else set()
    for a in assets:
        aid = a.get("id")
        if aid in scope.assets and aid not in owned:
            a["shared"] = {"by": scope.asset_by.get(aid, ""), "level": scope.assets[aid]}
    return assets


def annotate_bins(bins, scope):
    """Bins vus par un partage : `shared: {by, level, root}` — `root` : à afficher sous « Partagés
    avec moi » (son parent n'est pas dans la liste)."""
    if scope is None or not getattr(scope, "bins", None):
        return bins
    base = base_scope(scope)
    owned = mr._visible_bin_ids(base)
    ids = {b["id"] for b in bins}
    for b in bins:
        if b["id"] in scope.bins and b["id"] not in owned:
            b["shared"] = {"by": scope.bin_by.get(b["id"], ""), "level": scope.bins[b["id"]],
                           "root": b.get("parent_id") is None or b["parent_id"] not in ids
                           or b["parent_id"] not in scope.bins and b["parent_id"] not in owned}
    return bins


# ─── Macros (fournisseur du cœur, app/core_plugins.py:CORE_PROVIDERS) ─────────────────────────
MACRO_PROVIDER = {
    "id": "media_sharing",
    "label": "Partage des médias",
    "actions": [
        {"id": "share_bin", "label": "Partager un bin", "availability": "project", "params": [
            {"key": "bin", "label": "Bin", "type": "select", "options_hook": True},
            {"key": "recipient", "label": "Avec (groupe, utilisateur, projet)", "type": "select", "options_hook": True},
            {"key": "level", "label": "Niveau", "type": "select", "default": "use", "options": [
                {"value": "use", "label": "Consulter et utiliser"}, {"value": "edit", "label": "Modifier"}]},
            {"key": "days", "label": "Durée en jours (0 : sans fin)", "type": "number", "default": 0, "min": 0, "max": 3650}]},
        {"id": "unshare_bin", "label": "Retirer le partage d'un bin", "availability": "project", "params": [
            {"key": "bin", "label": "Bin", "type": "select", "options_hook": True},
            {"key": "recipient", "label": "Avec (groupe, utilisateur, projet)", "type": "select", "options_hook": True}]},
    ],
    "states": [
        {"id": "active_shares", "label": "Partages de médias actifs", "type": "number"},
    ],
}


def _macro_bins(pid):
    rows = mr.db().execute("SELECT id, name, project_id FROM media_bins ORDER BY name COLLATE NOCASE").fetchall()
    vis = mr._visible_bin_ids(None if pid is None else frozenset([int(pid)]))
    return [{"value": str(r["id"]), "label": r["name"]} for r in rows if r["id"] in vis]


def _macro_recipients():
    from .database import db_list_users, db_get_projects
    from .user_groups import list_groups, display_name
    out = [{"value": "group:%d" % g["id"], "label": "Groupe — %s" % g["name"]} for g in list_groups()]
    out += [{"value": "user:%d" % u["id"], "label": "Utilisateur — %s" % display_name(u)} for u in db_list_users()]
    out += [{"value": "project:%d" % p["id"], "label": "Projet — %s" % p.get("name")} for p in (db_get_projects() or [])]
    return out


def action_options(action_id, key, pid=None):
    if key == "bin":
        return _macro_bins(pid)
    if key == "recipient":
        return _macro_recipients()
    return []


def _macro_recipient(v):
    kind, _sep, rid = str(v or "").partition(":")
    if kind not in RECIPIENTS or not rid.strip().isdigit():
        raise RuntimeError("destinataire à préciser (groupe:<id>, user:<id> ou project:<id>)")
    return kind, int(rid)


def _macro_sharer(ctx):
    """(uid, nom, projets figés) : l'utilisateur qui lance la macro s'il est connu (sa portée
    VIVANTE), sinon la portée du projet de la macro (macro système : globale), figée."""
    from .database import db_get_user
    pid = (ctx or {}).get("project_id")
    name = str((ctx or {}).get("user") or "").strip()
    u = db_get_user(name) if name and ":" not in name else None
    if u:
        return u, u["id"], u["username"], None
    return None, None, "macro", (None if pid is None else [int(pid)])


def run_action(action_id, params, ctx):
    params = params or {}
    pid = (ctx or {}).get("project_id")
    try:
        bid = int(str(params.get("bin") or "").strip())
    except ValueError:
        raise RuntimeError("bin à préciser")
    if pid is not None and not mr.bin_visible(bid, frozenset([int(pid)])):
        raise RuntimeError("bin hors du projet de la macro")
    kind, rid = _macro_recipient(params.get("recipient"))
    u, uid, uname, sp = _macro_sharer(ctx)
    if u is not None:
        from .auth import ROLES, ROLE_INTOUCHABLE
        perms = set(ROLES.get(u.get("role"), set())) | ({"media.manage"} if u.get("role") == ROLE_INTOUCHABLE else set())
        why = check_can_share(_user_base(u), perms, "bin", bid, str(params.get("level") or "use"))
        if why:
            raise RuntimeError("partage refusé pour %s : %s" % (uname, why))
    if action_id == "share_bin":
        level = str(params.get("level") or "use")
        if level not in LEVELS:
            raise RuntimeError("niveau inconnu : %s" % level)
        try:
            days = float(params.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        exp = None
        if days > 0:
            exp = datetime.fromtimestamp(time.time() + days * 86400).isoformat(timespec="seconds")
        try:
            row = share("bin", bid, kind, rid, level, exp, sharer_uid=uid, sharer_name=uname, sharer_projects=sp)
        except (ValueError, LookupError) as e:
            raise RuntimeError("partage impossible : %s" % e)
        return {"ok": True, "share_id": row["id"], "expires_at": row["expires_at"]}
    if action_id == "unshare_bin":
        r = db().execute("SELECT id FROM media_shares WHERE target_kind='bin' AND target_id=? AND recipient_kind=? AND recipient_id=?",
                         (str(bid), kind, rid)).fetchone()
        if r:
            revoke(r["id"], actor=uname)
        return {"ok": True, "removed": bool(r)}
    raise RuntimeError("action inconnue : %s" % action_id)


def read_state(state_id, ctx):
    if state_id == "active_shares":
        purge_expired()
        return db().execute("SELECT COUNT(*) FROM media_shares").fetchone()[0]
    return None
