# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Backup / Restauration de la DB."""

import os
import shutil
from datetime import datetime

from flask import jsonify, request, send_file

from . import bp
from ..auth import require_perm
from ..config import DB_PATH
from ..backup import BACKUP_DIR, run_backup, list_backups


@bp.route("/api/backup/status")
@require_perm("backup.manage")
def backup_status():
    from .. import settings as _st
    return jsonify({
        "enabled":     bool(_st.get("backup_enabled")),
        "time":        _st.get("backup_time"),
        "retention":   _st.get("backup_retention"),
        "last_date":   _st.get("backup_last_date"),
        "last_status": _st.get("backup_last_status"),
        "last_file":   _st.get("backup_last_file"),
        "backups":     list_backups(),
    })

@bp.route("/api/backup/create", methods=["POST"])
@require_perm("backup.manage")
def backup_create():
    try:
        dest = run_backup()
        return jsonify({"ok": True, "message": "sauvegarde créée",
                        "file": os.path.basename(dest)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@bp.route("/api/backup/download/<name>")
@require_perm("backup.manage")
def backup_download(name):
    # Anti-traversal : seul un nom de fichier simple dans BACKUP_DIR est servi.
    if "/" in name or "\\" in name or not name.endswith(".db"):
        return jsonify({"error": "nom invalide"}), 400
    path = os.path.join(BACKUP_DIR, name)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(BACKUP_DIR) or not os.path.isfile(path):
        return jsonify({"error": "introuvable"}), 404
    return send_file(path, as_attachment=True, download_name=name,
                     mimetype="application/octet-stream")

@bp.route("/api/db/download")
@require_perm("backup.manage")
def download_db():
    return send_file(DB_PATH, as_attachment=True,
                     download_name="db_bobistudio.db",
                     mimetype="application/octet-stream")

@bp.route("/api/db/restore", methods=["POST"])
@require_perm("backup.manage")
def restore_db():
    f = request.files.get("db")
    if not f:
        return jsonify({"error": "fichier 'db' manquant"}), 400
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"orchestrateur-{ts}.db")
    # Backup atomique de l'existant
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup_path)
    # Écrase la DB courante
    f.save(DB_PATH)
    return jsonify({"status": "ok", "backup_path": backup_path})
