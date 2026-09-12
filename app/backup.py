# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Sauvegarde de la base SQLite : copie cohérente à chaud (API online `sqlite3.backup`),
rétention par nombre de fichiers, et déclenchement quotidien piloté par les réglages
`backup_enabled` / `backup_time` / `backup_retention`.

Tout l'état tient dans un seul fichier (`DB_PATH`) → un backup = une copie de ce fichier.
"""
import os
import sqlite3
import threading
from datetime import datetime

from .config import DB_PATH
from . import settings
from .database import db_add_alert, db_set_setting

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
_PREFIX = "db_bobistudio-"
_SUFFIX = ".db"
_lock = threading.Lock()   # sérialise backup manuel (requête) et quotidien (surveillance)


def list_backups():
    """Sauvegardes présentes, les plus récentes d'abord (le nom = timestamp triable)."""
    out = []
    try:
        for fn in os.listdir(BACKUP_DIR):
            if fn.startswith(_PREFIX) and fn.endswith(_SUFFIX):
                p = os.path.join(BACKUP_DIR, fn)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                out.append({"name": fn, "size": sz})
    except FileNotFoundError:
        pass
    out.sort(key=lambda x: x["name"], reverse=True)
    return out


def prune(keep):
    """Ne conserve que les `keep` sauvegardes les plus récentes."""
    keep = max(1, int(keep or 1))
    for f in list_backups()[keep:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, f["name"]))
        except OSError:
            pass


def _record(date_str, status, fname):
    db_set_setting("backup_last_date", date_str)
    db_set_setting("backup_last_status", status)
    db_set_setting("backup_last_file", fname)


def run_backup():
    """Crée une copie cohérente de la DB dans BACKUP_DIR, applique la rétention,
    met à jour l'état (`backup_last_*`) et journalise. Renvoie le chemin produit."""
    with _lock:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"{_PREFIX}{ts}{_SUFFIX}")
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(dest)
            try:
                with dst:
                    src.backup(dst)        # copie page-à-page, cohérente même sous écritures
            finally:
                dst.close()
        finally:
            src.close()
        prune(settings.get("backup_retention"))
        name = os.path.basename(dest)
        _record(datetime.now().strftime("%Y-%m-%d"), "ok — " + name, name)
        db_add_alert("alert.backup.creee", "info", kind="node", params={"name": name})
        return dest


def maybe_daily_backup():
    """Appelée fréquemment (boucle surveillance). Ne déclenche un backup qu'UNE fois
    par jour, à partir de l'heure configurée (heure locale serveur). Silencieuse si
    désactivée. Catch-up : si l'app démarre après l'heure, le backup du jour est fait."""
    if not settings.get("backup_enabled"):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if settings.get("backup_last_date") == today:
        return
    sched = str(settings.get("backup_time") or "02:00")
    if datetime.now().strftime("%H:%M") < sched:
        return
    try:
        run_backup()
    except Exception as e:
        _record(today, f"échec : {e}", "")   # marque le jour pour éviter une boucle d'échecs
        db_add_alert("alert.backup.quotidienne_echouee", "error", kind="node", params={"e": str(e)})
