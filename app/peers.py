# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Registre des instances Bobi.Studio du réseau + découverte par scan.

Sert de carnet d'adresses pour la mise à jour pull/push (cf. `updater.py`). Chaque pair
= {name, url, token}. `discover(cidr)` sonde le port 5000 d'une plage IP à la recherche
de l'endpoint `/api/update/ping` d'autres instances.
"""
import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .database import get_db
from . import updater

log = logging.getLogger(__name__)

UPDATE_PORT = 5000


_COLS = "id,name,url,token,version,last_seen,deployed_at"


def _row(r):
    return {"id": r[0], "name": r[1], "url": r[2], "token": r[3],
            "version": r[4], "last_seen": r[5], "deployed_at": r[6]}


def list_peers():
    with get_db() as db:
        rows = db.execute(f"SELECT {_COLS} FROM peers ORDER BY name").fetchall()
    return [_row(r) for r in rows]


def get_peer(pid):
    with get_db() as db:
        r = db.execute(f"SELECT {_COLS} FROM peers WHERE id=?", (pid,)).fetchone()
    return _row(r) if r else None


def add_peer(name, url, token=""):
    url = (url or "").rstrip("/")
    if not url:
        raise ValueError("url requise")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO peers(name,url,token,created_at) VALUES(?,?,?,?)",
                   (name or url, url, token or "", datetime.now().isoformat(timespec="seconds")))
    return refresh_url(url)


def update_peer(pid, name=None, token=None):
    sets, args = [], []
    if name is not None:
        sets.append("name=?"); args.append(name)
    if token is not None:
        sets.append("token=?"); args.append(token)
    if sets:
        args.append(pid)
        with get_db() as db:
            db.execute(f"UPDATE peers SET {','.join(sets)} WHERE id=?", args)
    return get_peer(pid)


def delete_peer(pid):
    with get_db() as db:
        db.execute("DELETE FROM peers WHERE id=?", (pid,))


def _store_seen(url, version, deployed_at):
    with get_db() as db:
        db.execute("UPDATE peers SET version=?, last_seen=?, deployed_at=? WHERE url=?",
                   (version, datetime.now().isoformat(timespec="seconds"),
                    deployed_at, url.rstrip("/")))


def refresh_url(url):
    """Ping un pair (par URL) et mémorise sa version/last_seen/date de déploiement."""
    try:
        info = updater.ping(url)
        _store_seen(url, info.get("label") or info.get("build_id") or "?",
                    info.get("deployed_at"))
    except Exception as e:
        log.debug("ping %s: %s", url, e)
    # renvoyer la ligne à jour
    with get_db() as db:
        r = db.execute(f"SELECT {_COLS} FROM peers WHERE url=?",
                       (url.rstrip("/"),)).fetchone()
    return _row(r) if r else None


def refresh_all():
    for p in list_peers():
        refresh_url(p["url"])
    return list_peers()


def discover(cidr):
    """Scan threadé d'une plage IP : retourne les instances trouvées {url,label,build_id}.
    Ne modifie pas le registre — l'utilisateur ajoute ensuite ce qu'il veut."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        raise ValueError(f"CIDR invalide : {e}")
    hosts = list(net.hosts()) if net.num_addresses > 2 else list(net)

    def _probe(ip):
        url = f"http://{ip}:{UPDATE_PORT}"
        try:
            info = updater.ping(url, timeout=2)
            return {"url": url, "ip": str(ip),
                    "label": info.get("label"), "build_id": info.get("build_id"),
                    "name": info.get("name")}
        except Exception:
            return None

    found = []
    with ThreadPoolExecutor(max_workers=64) as ex:
        for res in ex.map(_probe, hosts):
            if res:
                found.append(res)
    return found
