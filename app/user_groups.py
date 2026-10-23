# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""GROUPES D'UTILISATEURS nommés (2026-10-12) — destinataires d'un partage de médias.

Rien à voir avec les « habilitations » (app/auth.py : un rôle = un ensemble de DROITS) : un groupe
est un ensemble de PERSONNES (« Rédaction sport », « Prestataires montage »), qui ne donne aucun
droit par lui-même. Il ne sert qu'à adresser un partage (app/media_sharing.py) à plusieurs
personnes d'un geste, et à ce que le partage suive les arrivées et départs du groupe.

Tables dans la base PRINCIPALE (elles référencent `users`), créées ici, idempotentes, au premier
appel — app/database.py n'en sait rien. Un utilisateur supprimé laisse une ligne d'appartenance
orpheline : `users.id` est AUTOINCREMENT (jamais réattribué), et toute lecture joint `users`."""
import threading
from datetime import datetime

from . import database

NAME_MAX = 80
_ready = set()
_ready_lock = threading.Lock()


def _db():
    c = database.get_db()
    key = (database.DB_PATH, id(c))
    if key not in _ready:
        with _ready_lock:
            c.execute("""CREATE TABLE IF NOT EXISTS user_groups (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT,
                created_by TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS user_group_members (
                group_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_user_group_members_user ON user_group_members(user_id)")
            c.commit()
            _ready.add(key)
    return c


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _clean_name(name):
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("name_required")
    if len(name) > NAME_MAX:
        raise ValueError("name_too_long")
    return name


def display_name(u):
    """« Prénom Nom (login) », à défaut le login — même forme partout où l'on choisit quelqu'un."""
    full = " ".join(x for x in ((u.get("prenom") or "").strip(), (u.get("nom") or "").strip()) if x)
    return "%s (%s)" % (full, u.get("username")) if full else (u.get("username") or "#%s" % u.get("id"))


def list_groups():
    """[{id, name, members:[{id, username, label}], created_at, created_by}] par nom."""
    c = _db()
    groups = [dict(r) for r in c.execute("SELECT * FROM user_groups ORDER BY name COLLATE NOCASE")]
    members = {}
    for r in c.execute("""SELECT m.group_id, u.id, u.username, u.prenom, u.nom FROM user_group_members m
                          JOIN users u ON u.id = m.user_id ORDER BY u.username COLLATE NOCASE"""):
        members.setdefault(r["group_id"], []).append({"id": r["id"], "username": r["username"], "label": display_name(dict(r))})
    for g in groups:
        g["members"] = members.get(g["id"], [])
    return groups


def get_group(gid):
    try:
        gid = int(gid)
    except (TypeError, ValueError):
        return None
    return next((g for g in list_groups() if g["id"] == gid), None)


def group_names(ids):
    ids = [int(i) for i in ids or () if str(i).lstrip("-").isdigit()]
    if not ids:
        return {}
    return {r["id"]: r["name"] for r in _db().execute(
        "SELECT id, name FROM user_groups WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}


def create_group(name, by=""):
    name = _clean_name(name)
    c = _db()
    if c.execute("SELECT 1 FROM user_groups WHERE name=?", (name,)).fetchone():
        raise ValueError("name_taken")
    cur = c.execute("INSERT INTO user_groups(name, created_at, created_by) VALUES (?,?,?)", (name, _now(), by or ""))
    c.commit()
    return cur.lastrowid


def rename_group(gid, name):
    name = _clean_name(name)
    c = _db()
    if not c.execute("SELECT 1 FROM user_groups WHERE id=?", (int(gid),)).fetchone():
        raise LookupError("group_unknown")
    if c.execute("SELECT 1 FROM user_groups WHERE name=? AND id!=?", (name, int(gid))).fetchone():
        raise ValueError("name_taken")
    c.execute("UPDATE user_groups SET name=? WHERE id=?", (name, int(gid)))
    c.commit()


def set_members(gid, user_ids):
    """Remplace les membres du groupe par `user_ids` (les identifiants inconnus sont ignorés)."""
    c = _db()
    if not c.execute("SELECT 1 FROM user_groups WHERE id=?", (int(gid),)).fetchone():
        raise LookupError("group_unknown")
    ids = sorted({int(u) for u in user_ids or () if str(u).lstrip("-").isdigit()})
    known = {r["id"] for r in c.execute("SELECT id FROM users WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)} if ids else set()
    c.execute("DELETE FROM user_group_members WHERE group_id=?", (int(gid),))
    c.executemany("INSERT INTO user_group_members(group_id, user_id) VALUES (?,?)", [(int(gid), u) for u in sorted(known)])
    c.commit()
    return sorted(known)


def delete_group(gid):
    """Supprime le groupe et ses appartenances. Les partages qui lui étaient adressés deviennent
    inertes (app/media_sharing.py les ignore et les retire au passage)."""
    c = _db()
    n = c.execute("DELETE FROM user_groups WHERE id=?", (int(gid),)).rowcount
    c.execute("DELETE FROM user_group_members WHERE group_id=?", (int(gid),))
    c.commit()
    return n > 0


def groups_of_user(uid):
    """Identifiants des groupes dont l'utilisateur est membre."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return set()
    return {r["group_id"] for r in _db().execute(
        "SELECT m.group_id FROM user_group_members m JOIN user_groups g ON g.id = m.group_id WHERE m.user_id=?", (uid,))}
