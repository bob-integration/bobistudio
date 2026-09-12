# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Service `files` : gestionnaire de fichiers générique, multi-racines.

Service IN-APP (servi par l'orchestrateur, pas de conteneur) : navigation, recherche, tri,
édition de fichiers texte. Deux types de racines :
  - local : système de fichiers de l'orchestrateur (configuration), confiné à une base.
  - node  : répertoire média d'un nœud (media_mount), listé/lu/écrit via ssh sur l'hôte du nœud.
Le gating fin se fait PAR RACINE (chaque racine porte la permission requise + l'autorisation
d'écriture). Anti-traversal strict, détection binaire et borne de taille pour read/write.
"""
import logging
import os
import shlex
import shutil

log = logging.getLogger(__name__)

PLUGIN_VERSION = "0.1.0"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MAX_EDIT_BYTES = 2 * 1024 * 1024          # 2 Mio : borne lecture/édition texte
# Fichiers JAMAIS exposés (secrets) — masqués au listing et refusés en lecture/écriture.
_DENY_NAMES = {"config_local.py"}
_DENY_EXTS  = {".db", ".db-wal", ".db-shm", ".pem", ".key"}


def _roots():
    """Racines déclarées, visibles selon les droits. `kind` = local|node ; `perm` requise ;
    `writable` autorise l'édition. Les racines média des nœuds sont dérivées de la table nodes."""
    from app.database import db_get_nodes
    roots = [
        {"id": "config", "label": "Configuration (orchestrateur)", "kind": "local",
         "base": _REPO_ROOT, "perm": "settings.edit", "writable": True},
    ]
    try:
        for n in db_get_nodes():
            if not n.get("host"):
                continue
            # Racine média du nœud : media_mount réglé, sinon défaut /srv/mxl-media (le bind par
            # défaut du chemin compute) → le dossier des médias est toujours atteignable.
            mm = (n.get("media_mount") or "").strip() or "/srv/mxl-media"
            roots.append({
                "id": "media_%s" % n["id"], "label": "Média — %s" % n["name"],
                "kind": "node", "host": n["host"], "base": mm,
                "perm": "files.access", "writable": True})
    except Exception as e:
        log.warning("files: énumération nœuds échouée : %s", e)
    return roots


# Options ssh communes (clé seule, comme les autres actions hôte).
_SSH_OPTS = ["-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=8"]


def _visible_roots():
    from app.auth import has_perm
    return [r for r in _roots() if has_perm(r["perm"])]


def _root_by_id(rid):
    from app.auth import has_perm
    r = next((x for x in _roots() if x["id"] == rid), None)
    if not r or not has_perm(r["perm"]):
        return None
    return r


def _safe_rel(rel):
    """Normalise un chemin relatif et REFUSE toute évasion (.. / chemin absolu). '' = racine."""
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        return ""
    parts = []
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("chemin invalide")
        parts.append(seg)
    return "/".join(parts)


def _denied(name):
    base = os.path.basename(name)
    return base in _DENY_NAMES or os.path.splitext(base)[1].lower() in _DENY_EXTS


# ─── Accès LOCAL (système de l'orchestrateur) ────────────────────────────────
def _local_abspath(root, rel):
    base = os.path.realpath(root["base"])
    full = os.path.realpath(os.path.join(base, rel))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("hors racine")
    return full


def _list_local(root, rel):
    full = _local_abspath(root, rel)
    if not os.path.isdir(full):
        raise ValueError("pas un dossier")
    items = []
    for name in os.listdir(full):
        if _denied(name):
            continue
        p = os.path.join(full, name)
        try:
            st = os.stat(p)
            items.append({"name": name, "type": "dir" if os.path.isdir(p) else "file",
                          "size": st.st_size, "mtime": int(st.st_mtime),
                          "ext": os.path.splitext(name)[1].lower()})
        except OSError:
            pass
    return items


def _read_local(root, rel):
    full = _local_abspath(root, rel)
    if _denied(full) or not os.path.isfile(full):
        raise ValueError("fichier introuvable")
    if os.path.getsize(full) > _MAX_EDIT_BYTES:
        raise ValueError("fichier trop volumineux (> 2 Mio)")
    with open(full, "rb") as f:
        data = f.read()
    if b"\x00" in data:
        raise ValueError("fichier binaire (édition refusée)")
    return data.decode("utf-8", "replace")


def _write_local(root, rel, content):
    full = _local_abspath(root, rel)
    if _denied(full) or os.path.isdir(full):
        raise ValueError("cible invalide")
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)


# ─── Accès NODE (média d'un nœud, via ssh sur l'hôte) ────────────────────────
def _node_abspath(root, rel):
    # base + rel déjà sanitisé (_safe_rel) → pas d'évasion. On garde une base sans / final.
    base = root["base"].rstrip("/")
    return base + ("/" + rel if rel else "")


def _ssh(host, cmd, input_data=None, timeout=30):
    from app.host_ops import ssh_run
    return ssh_run(host, cmd, input_data=input_data, timeout=timeout)


def _list_node(root, rel):
    full = _node_abspath(root, rel)
    # GNU find : type(%y) \t taille(%s) \t mtime(%T@) \t nom(%f)
    cmd = ("find %s -mindepth 1 -maxdepth 1 -printf '%%y\\t%%s\\t%%T@\\t%%f\\n' 2>/dev/null"
           % shlex.quote(full))
    rc, out, _ = _ssh(root["host"], cmd, timeout=20)
    items = []
    for line in (out or "").splitlines():
        try:
            ty, size, mtime, name = line.split("\t", 3)
        except ValueError:
            continue
        if _denied(name):
            continue
        items.append({"name": name, "type": "dir" if ty == "d" else "file",
                      "size": int(size or 0), "mtime": int(float(mtime or 0)),
                      "ext": os.path.splitext(name)[1].lower()})
    return items


def _read_node(root, rel):
    full = _node_abspath(root, rel)
    if _denied(full):
        raise ValueError("fichier refusé")
    # garde-fou taille puis lecture (tête binaire détectée côté contenu).
    rc, out, _ = _ssh(root["host"], "stat -c %%s %s 2>/dev/null" % shlex.quote(full), timeout=15)
    try:
        if int((out or "0").strip()) > _MAX_EDIT_BYTES:
            raise ValueError("fichier trop volumineux (> 2 Mio)")
    except ValueError as e:
        if "volumineux" in str(e):
            raise
    rc, out, _ = _ssh(root["host"], "cat %s 2>/dev/null" % shlex.quote(full), timeout=20)
    if "\x00" in (out or ""):
        raise ValueError("fichier binaire (édition refusée)")
    return out or ""


def _write_node(root, rel, content):
    full = _node_abspath(root, rel)
    if _denied(full):
        raise ValueError("cible refusée")
    rc, out, err = _ssh(root["host"], "cat > %s" % shlex.quote(full),
                        input_data=content, timeout=30)
    if rc != 0:
        raise ValueError("écriture échouée : %s" % (err or out)[:200])


# ─── Opérations fichiers (download / upload / rename / delete) ────────────────
import subprocess


def _ssh_argv(host, remote_cmd):
    return ["ssh"] + _SSH_OPTS + ["root@%s" % host, remote_cmd]


def _safe_name(name):
    """Nom de fichier simple (basename, ni / ni .. ni vide)."""
    n = os.path.basename((name or "").strip())
    if not n or n in (".", "..") or "/" in n:
        raise ValueError("nom invalide")
    return n


def download_stream(root, rel):
    """Retourne (generator, filename). Local : lecture disque ; node : ssh cat en streaming."""
    name = os.path.basename(rel)
    if root["kind"] == "node":
        full = _node_abspath(root, rel)
        if _denied(full):
            raise ValueError("fichier refusé")
        proc = subprocess.Popen(_ssh_argv(root["host"], "cat " + shlex.quote(full)),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def gen_node():
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try: proc.stdout.close(); proc.wait(timeout=5)
                except Exception: pass
        return gen_node(), name
    full = _local_abspath(root, rel)
    if _denied(full) or not os.path.isfile(full):
        raise ValueError("fichier introuvable")

    def gen_local():
        with open(full, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk
    return gen_local(), name


def upload(root, rel_dir, filename, fileobj):
    """Enregistre `fileobj` dans rel_dir/filename. fileobj = flux (werkzeug FileStorage)."""
    name = _safe_name(filename)
    rel = (rel_dir + "/" + name) if rel_dir else name
    if root["kind"] == "node":
        full = _node_abspath(root, rel)
        if _denied(full):
            raise ValueError("cible refusée")
        proc = subprocess.Popen(_ssh_argv(root["host"], "cat > " + shlex.quote(full)),
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        try:
            while True:
                chunk = fileobj.read(65536)
                if not chunk:
                    break
                proc.stdin.write(chunk)
            proc.stdin.close()
        except Exception as e:
            raise ValueError("upload échoué : %s" % e)
        if proc.wait(timeout=600) != 0:
            raise ValueError("upload échoué (ssh)")
    else:
        full = _local_abspath(root, rel)
        if _denied(full):
            raise ValueError("cible refusée")
        # `fileobj` est un FLUX (l'appelant passe `f.stream`), pas le `FileStorage` de Werkzeug :
        # `.save()` n'existe donc pas dessus et tout envoi vers une racine LOCALE échouait
        # (« 'SpooledTemporaryFile' object has no attribute 'save' » au-delà de 500 ko, « BytesIO »
        # en dessous). La branche « nœud » ci-dessus lit déjà le flux par morceaux ; on fait pareil
        # ici, ce qui aligne les deux chemins sur le même contrat et borne la mémoire.
        with open(full, "wb") as sortie:
            shutil.copyfileobj(fileobj, sortie, 1024 * 1024)
    return rel


def rename(root, rel, new_name):
    name = _safe_name(new_name)
    parent = "/".join(_safe_rel(rel).split("/")[:-1])
    new_rel = (parent + "/" + name) if parent else name
    if root["kind"] == "node":
        old = _node_abspath(root, rel); new = _node_abspath(root, new_rel)
        if _denied(old) or _denied(new):
            raise ValueError("cible refusée")
        rc, out, err = _ssh(root["host"], "mv -n %s %s" % (shlex.quote(old), shlex.quote(new)), timeout=20)
        if rc != 0:
            raise ValueError("renommage échoué : %s" % (err or out)[:200])
    else:
        old = _local_abspath(root, rel); new = _local_abspath(root, new_rel)
        if _denied(old) or _denied(new):
            raise ValueError("cible refusée")
        if os.path.exists(new):
            raise ValueError("destination existante")
        os.rename(old, new)
    return new_rel


def delete(root, rel):
    if root["kind"] == "node":
        full = _node_abspath(root, rel)
        if _denied(full):
            raise ValueError("cible refusée")
        # fichier → rm -f ; dossier vide → rmdir (jamais récursif, sécurité).
        rc, out, err = _ssh(root["host"],
                            "if [ -d %s ]; then rmdir %s; else rm -f %s; fi"
                            % (shlex.quote(full), shlex.quote(full), shlex.quote(full)), timeout=20)
        if rc != 0:
            raise ValueError("suppression échouée : %s" % (err or out)[:200])
    else:
        full = _local_abspath(root, rel)
        if _denied(full):
            raise ValueError("cible refusée")
        if os.path.isdir(full):
            os.rmdir(full)   # dossier vide uniquement
        else:
            os.remove(full)


# ─── Routes ───────────────────────────────────────────────────────────────────
def register_routes(bp):
    from flask import request, jsonify
    from app.auth import require_perm

    def _resolve(rid, rel, need_write=False):
        root = _root_by_id(rid)
        if not root:
            return None, None, (jsonify({"error": "racine inconnue ou accès refusé"}), 403)
        if need_write and not root.get("writable"):
            return None, None, (jsonify({"error": "racine en lecture seule"}), 403)
        try:
            return root, _safe_rel(rel), None
        except ValueError as e:
            return None, None, (jsonify({"error": str(e)}), 400)

    @bp.route("/api/files/roots", methods=["GET"])
    @require_perm("files.access")
    def files_roots():
        return jsonify({"roots": [{"id": r["id"], "label": r["label"], "kind": r["kind"],
                                   "writable": bool(r.get("writable"))} for r in _visible_roots()]})

    @bp.route("/api/files/list", methods=["GET"])
    @require_perm("files.access")
    def files_list():
        root, rel, err = _resolve(request.args.get("root"), request.args.get("path"))
        if err:
            return err
        try:
            items = _list_node(root, rel) if root["kind"] == "node" else _list_local(root, rel)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        q = (request.args.get("q") or "").strip().lower()
        if q:
            items = [it for it in items if q in it["name"].lower()]
        items.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))
        return jsonify({"root": root["id"], "path": rel, "writable": bool(root.get("writable")),
                        "items": items})

    @bp.route("/api/files/read", methods=["GET"])
    @require_perm("files.access")
    def files_read():
        root, rel, err = _resolve(request.args.get("root"), request.args.get("path"))
        if err:
            return err
        if not rel:
            return jsonify({"error": "chemin manquant"}), 400
        try:
            content = _read_node(root, rel) if root["kind"] == "node" else _read_local(root, rel)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"root": root["id"], "path": rel, "content": content})

    @bp.route("/api/files/write", methods=["POST"])
    @require_perm("files.access")
    def files_write():
        body = request.json or {}
        root, rel, err = _resolve(body.get("root"), body.get("path"), need_write=True)
        if err:
            return err
        if not rel:
            return jsonify({"error": "chemin manquant"}), 400
        content = body.get("content")
        if not isinstance(content, str):
            return jsonify({"error": "contenu invalide"}), 400
        if len(content.encode("utf-8")) > _MAX_EDIT_BYTES:
            return jsonify({"error": "contenu trop volumineux (> 2 Mio)"}), 400
        try:
            (_write_node if root["kind"] == "node" else _write_local)(root, rel, content)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        from app.database import db_add_alert
        db_add_alert("Fichier édité : %s/%s" % (root["id"], rel), "info")
        return jsonify({"ok": True})

    @bp.route("/api/files/download", methods=["GET"])
    @require_perm("files.access")
    def files_download():
        from flask import Response
        root, rel, err = _resolve(request.args.get("root"), request.args.get("path"))
        if err:
            return err
        if not rel:
            return jsonify({"error": "chemin manquant"}), 400
        try:
            gen, name = download_stream(root, rel)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return Response(gen, mimetype="application/octet-stream",
                        headers={"Content-Disposition": "attachment; filename=\"%s\"" % name.replace('"', '')})

    @bp.route("/api/files/upload", methods=["POST"])
    @require_perm("files.access")
    def files_upload():
        root, rel, err = _resolve(request.form.get("root"), request.form.get("path"), need_write=True)
        if err:
            return err
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "fichier manquant"}), 400
        try:
            new_rel = upload(root, rel, f.filename, f.stream)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "path": new_rel})

    @bp.route("/api/files/rename", methods=["POST"])
    @require_perm("files.access")
    def files_rename():
        body = request.json or {}
        root, rel, err = _resolve(body.get("root"), body.get("path"), need_write=True)
        if err:
            return err
        if not rel:
            return jsonify({"error": "chemin manquant"}), 400
        try:
            new_rel = rename(root, rel, body.get("new_name") or "")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "path": new_rel})

    @bp.route("/api/files/delete", methods=["POST"])
    @require_perm("files.access")
    def files_delete():
        body = request.json or {}
        root, rel, err = _resolve(body.get("root"), body.get("path"), need_write=True)
        if err:
            return err
        if not rel:
            return jsonify({"error": "chemin manquant"}), 400
        try:
            delete(root, rel)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})
