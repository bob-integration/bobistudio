# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Mise à jour entre instances Bobi.Studio (pull / push) sur le réseau local.

Modèle : une instance « serveur » expose son code (zip builder, déjà sans secret) +
un manifeste (version + sha256). Une autre instance tire ce zip, vérifie le checksum,
sauvegarde son arbre code, applique par-dessus (sans toucher l'état local), puis relance
son service. Le « push » = appeler `apply_update` à distance sur le pair (auth par SON token).

Sécurité : token partagé obligatoire (vérifié côté routes) + sha256 du zip vérifié avant
extraction. L'extraction **préserve** config_local.py, les bases *.db et static/uploads/.
"""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime

from . import builder

log = logging.getLogger(__name__)

ROOT = builder.ROOT
DIST_DIR = builder.DIST_DIR
# Zip dédié à la mise à jour inter-instances : copie FIDÈLE (tous plugins/services),
# distinct du zip de distribution sélectif servi par /install.
ZIP_PATH = os.path.join(DIST_DIR, "bobistudio-update.zip")
SERVICE = "bobistudio"
PENDING_PATH = os.path.join(ROOT, "UPDATE_PENDING")
# Horodatage du DERNIER déploiement appliqué SUR cette instance (distinct de built_at,
# qui date la construction du zip). Écrit par apply_update, lu par _my_identity/ping.
DEPLOY_INFO_PATH = os.path.join(ROOT, "deploy_info.json")

# Chemins jamais écrasés lors de l'extraction (état local de l'instance cible).
# Le zip ne contient déjà ni secrets ni .db, mais il EMBARQUE static/uploads/ → on protège.
def _protected(arc):
    a = arc.replace("\\", "/")
    return (a == "config_local.py" or a.endswith(".db")
            or a.startswith("static/uploads/") or a.startswith("dist/")
            or a == "build_manifest.json" or a == "deploy_info.json")


# ─── Côté serveur : manifeste + zip à jour ───────────────────────────────────

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_mtime():
    """mtime le plus récent de l'arbre code (hors dist/venv/caches) → détecte un changement."""
    newest = 0.0
    for d in builder.CORE_DIRS + ["plugins", "services"]:
        ap = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(ap):
            dirnames[:] = [x for x in dirnames if x not in builder.EXCLUDE_DIRS]
            for fn in filenames:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dirpath, fn)))
                except OSError:
                    pass
    return newest


def ensure_build():
    """Garantit un zip de mise à jour à jour : reconstruit (TOUS plugins/services) seulement si
    le zip manque ou si du code a changé depuis sa dernière génération.

    L'identité (`build_info.json`) est RÉÉCRITE à cette occasion. Elle ne l'était pas
    auparavant (`stamp=False`, « identité stable ») : le contenu du zip suivait le code, mais
    l'étiquette restait figée au dernier build explicite. La page Flotte affichait donc « même
    version » des deux côtés alors que l'artefact envoyé contenait autre chose — ce qui pousse
    soit à ne pas mettre à jour quand il le faudrait, soit à le faire sans savoir ce qu'on envoie.
    Une étiquette qui ne décrit pas ce qu'elle désigne ne vaut pas mieux que pas d'étiquette.

    Il n'y a pas de risque d'emballement : on ne reconstruit QUE si le code a bougé, et
    `_code_mtime()` ne scanne que les dossiers de code — `build_info.json` vit à la racine, donc
    l'écrire ne re-déclenche pas un build."""
    # Deux raisons de reconstruire, et il FAUT les deux :
    #  · le code a changé (mtime) → le contenu de l'artefact n'est plus le bon ;
    #  · l'identité git a changé alors que les fichiers, eux, n'ont pas bougé — c'est le cas après
    #    un COMMIT : le contenu reste identique mais l'étiquette dit encore l'ancien hash (et un
    #    `-dirty` qui n'a plus lieu d'être). Sans ce second test, on repart pour un libellé faux,
    #    simplement dans l'autre sens.
    _hash_ok = (builder.current_build_info() or {}).get("git_hash") == builder._git_hash()
    if os.path.exists(ZIP_PATH) and os.path.getmtime(ZIP_PATH) >= _code_mtime() and _hash_ok:
        return None
    av = builder.available()
    plugins = [p["type"] for p in av.get("plugins", []) if p.get("type")]
    services = [s["id"] for s in av.get("services", []) if s.get("id")]
    return builder.build(plugins=plugins, services=services, dest=ZIP_PATH, stamp=True)


def diff_manifests(old, new):
    """Compare deux manifestes (cible `old` → source `new`) et renvoie le détail des
    plugins/services qui changent. Fonction PURE (pas d'I/O) → testable.

    Renvoie {"components": [{kind, id, label, from, to, status}], "counts": {...}}
    avec status ∈ added | updated | removed | unchanged. Tolère des manifestes
    partiels (pair injoignable) : une liste absente est traitée comme vide.
    """
    old = old or {}
    new = new or {}
    components = []
    counts = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}

    def _index(manifest, list_key, id_key):
        out = {}
        for it in (manifest.get(list_key) or []):
            key = it.get(id_key)
            if key:
                out[key] = it
        return out

    for kind, list_key, id_key in (("plugin", "plugins", "type"),
                                   ("service", "services", "id")):
        o = _index(old, list_key, id_key)
        n = _index(new, list_key, id_key)
        for key in sorted(set(o) | set(n)):
            ov = o.get(key, {}).get("version") or ""
            nv = n.get(key, {}).get("version") or ""
            label = (n.get(key) or o.get(key) or {}).get("label") or key
            if key not in o:
                status = "added"
            elif key not in n:
                status = "removed"
            elif ov != nv:
                status = "updated"
            else:
                status = "unchanged"
            counts[status] += 1
            components.append({"kind": kind, "id": key, "label": label,
                               "from": ov, "to": nv, "status": status})
    return {"components": components, "counts": counts}


def current_manifest():
    """Manifeste servi à un pair : identité de build + sha256 du zip + versions.

    Recharge d'abord les registres plugins/services depuis le disque : sinon les
    numéros de version rapportés viendraient du cache mémoire figé au démarrage
    (un bump de plugin.json non suivi d'un restart afficherait une version périmée
    dans l'aperçu Push/Pull et tromperait le diff)."""
    from . import plugins, core_plugins
    plugins.reload()
    core_plugins.reload()
    ensure_build()
    info = builder.current_build_info()
    av = builder.available()
    return {
        "build_id": info.get("build_id"),
        "label":    info.get("label"),
        "built_at": info.get("built_at"),
        "git_hash": info.get("git_hash"),
        "sha256":   sha256_file(ZIP_PATH),
        "size":     os.path.getsize(ZIP_PATH),
        "plugins":  av.get("plugins", []),
        "services": av.get("services", []),
    }


# ─── Côté client : récupération + application ────────────────────────────────

def _http_json(url, token, timeout=15):
    req = urllib.request.Request(url, headers={"X-MXL-Update-Token": token or ""})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (réseau interne)
        return json.loads(r.read().decode())


def fetch_manifest(base_url, token):
    return _http_json(base_url.rstrip("/") + "/api/update/manifest", token)


def ping(base_url, token=None, timeout=4):
    return _http_json(base_url.rstrip("/") + "/api/update/ping", token, timeout=timeout)


def _download(url, token, dest, timeout=120):
    req = urllib.request.Request(url, headers={"X-MXL-Update-Token": token or ""})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:  # noqa: S310
        shutil.copyfileobj(r, f)


def backup_code():
    """Archive l'arbre code courant dans dist/backup-<ts>.tgz (pour rollback)."""
    os.makedirs(DIST_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(DIST_DIR, f"backup-{ts}.tgz")
    members = builder.CORE_DIRS + ["plugins", "services"] + [
        f for f in ("main.py", "requirements.txt", "build_info.json") if os.path.exists(os.path.join(ROOT, f))
    ]
    with tarfile.open(dest, "w:gz") as tar:
        for m in members:
            ap = os.path.join(ROOT, m)
            if os.path.exists(ap):
                tar.add(ap, arcname=m, filter=_tar_filter)
    return dest


def _tar_filter(ti):
    # Ne pas embarquer venv/caches/uploads/db dans le backup non plus.
    name = ti.name.split("/", 1)[-1] if "/" in ti.name else ti.name
    parts = ti.name.split("/")
    if any(p in builder.EXCLUDE_DIRS for p in parts) or "uploads" in parts:
        return None
    if ti.name.endswith((".db", ".pyc", ".log")):
        return None
    return ti


def latest_backup():
    try:
        bks = sorted(b for b in os.listdir(DIST_DIR) if b.startswith("backup-") and b.endswith(".tgz"))
        return os.path.join(DIST_DIR, bks[-1]) if bks else None
    except FileNotFoundError:
        return None


def _local_component_ids():
    """Composants INSTALLÉS localement, lus sur le DISQUE (pas le registre en mémoire : un
    plugin cassé — accolade non doublée, manifeste invalide… — est absent du registre alors
    qu'il est bien installé, et c'est souvent la mise à jour qui le répare). Un plugin =
    plugins/<type>/plugin.json ; un service = services/<id>/ (dossier). Les dossiers `_…`
    (runtimes partagés) ne sont pas des composants : toujours mis à jour avec le cœur."""
    plugs, servs = set(), set()
    try:
        pdir = os.path.join(ROOT, "plugins")
        for d in os.listdir(pdir):
            if d.startswith(("_", ".")):
                continue
            if os.path.exists(os.path.join(pdir, d, "plugin.json")):
                plugs.add(d)
    except OSError:
        pass
    try:
        sdir = os.path.join(ROOT, "services")
        for d in os.listdir(sdir):
            if d.startswith(("_", ".")) or d == "__pycache__":
                continue
            if os.path.isdir(os.path.join(sdir, d)):
                servs.add(d)
    except OSError:
        pass
    return plugs, servs


def _extract_over(zip_path, skip_plugins=None, skip_services=None):
    """Extrait le zip par-dessus ROOT en sautant les chemins protégés (état local) et les
    composants exclus (composition par instance : plugins/services non installés ici)."""
    skip_plugins = skip_plugins or set()
    skip_services = skip_services or set()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if _protected(name):
                continue
            parts = name.split("/", 2)
            if len(parts) >= 2 and ((parts[0] == "plugins" and parts[1] in skip_plugins)
                                    or (parts[0] == "services" and parts[1] in skip_services)):
                continue
            target = os.path.join(ROOT, name)
            os.makedirs(os.path.dirname(target) or ROOT, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _missing_requirements(req_text):
    """Lignes de requirements.txt dont le paquet n'est PAS installé dans ce venv.
    Vérification de PRÉSENCE seulement (pas la version exacte : les pins sont des snapshots
    d'un venv sain — une autre version déjà installée fait l'affaire, on ne churne pas un
    site hors-ligne). → liste de specs à installer (avec leur pin, pour un install propre)."""
    import re as _re
    import importlib.metadata as _md
    missing = []
    for line in (req_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = _re.split(r"[<>=!\[;]", line, 1)[0].strip()
        if not name:
            continue
        try:
            _md.version(name)
        except _md.PackageNotFoundError:
            missing.append(line)
    return missing


def _ensure_requirements(zpath):
    """Garde-fou dépendances (post-mortem « waitress/cryptography ») : une mise à jour qui
    AJOUTE une dépendance Python casserait le boot du service au restart (import top-level).
    On lit requirements.txt DANS le zip et on installe les paquets manquants dans le venv
    AVANT de toucher au code ; échec (site hors-ligne, pip cassé) → la mise à jour est
    REFUSÉE proprement, l'instance reste sur son code actuel. → (ok, msg)."""
    try:
        with zipfile.ZipFile(zpath) as zf:
            if "requirements.txt" not in zf.namelist():
                return True, ""
            req_text = zf.read("requirements.txt").decode("utf-8", "replace")
    except Exception as e:
        return False, f"requirements.txt illisible dans l'archive : {e}"
    missing = _missing_requirements(req_text)
    if not missing:
        return True, ""
    pip = os.path.join(ROOT, "venv", "bin", "pip")
    if not os.path.exists(pip):
        return False, ("dépendances manquantes ({}) et venv/bin/pip introuvable — installer "
                       "manuellement puis relancer la mise à jour".format(", ".join(missing)))
    log.info("mise à jour : installation des dépendances manquantes : %s", ", ".join(missing))
    try:
        r = subprocess.run([pip, "install", "--no-input", *missing],
                           capture_output=True, text=True, timeout=300)
    except Exception as e:
        return False, f"pip install {' '.join(missing)} : {e}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return False, ("dépendances manquantes non installables ({}) — accès Internet/miroir pip "
                       "requis. {}".format(", ".join(missing), " | ".join(tail)))
    still = _missing_requirements(req_text)
    if still:
        return False, "dépendances toujours manquantes après installation : " + ", ".join(still)
    try:
        from .database import db_add_alert
        db_add_alert("alert.update.deps_installees", "info",
                     params={"deps": ", ".join(missing)})
    except Exception:
        pass
    return True, ""


def restart_service():
    """Relance le service hors de notre cgroup (sinon le restart nous tue avant l'heure)."""
    # systemd-run lance un transient timer indépendant → notre réponse HTTP part d'abord.
    try:
        subprocess.Popen(["systemd-run", "--on-active=2s", "--quiet",
                          "systemctl", "restart", SERVICE])
        return
    except FileNotFoundError:
        pass
    subprocess.Popen(["bash", "-c", f"sleep 2; systemctl restart {SERVICE}"],
                     start_new_session=True)


def apply_update(source_url, token, install_new=None):
    """Tire le code depuis `source_url`, vérifie le checksum, sauvegarde, applique, relance.
    Renvoie (ok, msg). Le restart est asynchrone (le service repart avec le nouveau code).

    COMPOSITION PAR INSTANCE : le zip flotte contient TOUS les plugins/services (identité de
    build stable), mais on n'applique que le cœur + les composants DÉJÀ INSTALLÉS ici — un
    plugin nouveau n'apparaît jamais tout seul sur un site. `install_new` = opt-in explicite
    ({"plugins": [ids], "services": [ids]}, coché dans l'aperçu Push/Pull) pour en adopter."""
    inew = install_new or {}
    new_p = {str(x) for x in (inew.get("plugins") or [])}
    new_s = {str(x) for x in (inew.get("services") or [])}
    base = source_url.rstrip("/")
    try:
        man = fetch_manifest(base, token)
    except Exception as e:
        return False, f"manifeste injoignable : {e}"
    expected = man.get("sha256")
    if not expected:
        return False, "manifeste sans sha256"

    tmpdir = tempfile.mkdtemp(prefix="mxlupd-")
    try:
        zpath = os.path.join(tmpdir, "bobistudio.zip")
        try:
            _download(base + "/api/update/download", token, zpath)
        except Exception as e:
            return False, f"téléchargement échoué : {e}"
        got = sha256_file(zpath)
        if got != expected:
            return False, f"checksum invalide (attendu {expected[:12]}…, reçu {got[:12]}…)"
        # sanity : le zip doit contenir main.py
        with zipfile.ZipFile(zpath) as zf:
            if "main.py" not in zf.namelist():
                return False, "archive invalide (main.py absent)"

        # Dépendances Python AVANT d'appliquer : une dépendance ajoutée par cette version
        # doit être installable ici, sinon on refuse (le restart casserait le service).
        ok_req, req_msg = _ensure_requirements(zpath)
        if not ok_req:
            return False, req_msg

        # Composition par instance : composants du manifeste SOURCE ni installés ici ni
        # opt-in → exclus de l'extraction. Tout le reste (cœur, runtimes `_…`) passe.
        loc_p, loc_s = _local_component_ids()
        src_p = {p.get("type") for p in (man.get("plugins") or []) if p.get("type")}
        src_s = {s.get("id") for s in (man.get("services") or []) if s.get("id")}
        skip_p = {p for p in src_p if p not in loc_p and p not in new_p}
        skip_s = {s for s in src_s if s not in loc_s and s not in new_s}

        backup_code()
        _extract_over(zpath, skip_plugins=skip_p, skip_services=skip_s)
        with open(PENDING_PATH, "w") as f:
            f.write(man.get("build_id") or man.get("label") or "?")
        record_deploy(man.get("build_id"), man.get("label"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    restart_service()
    skipped = len(skip_p) + len(skip_s)
    added = sorted(new_p | new_s)
    extra = ""
    if added:
        extra += f" · nouveaux composants installés : {', '.join(added)}"
    if skipped:
        extra += f" · {skipped} composant(s) non installé(s) ici ignoré(s)"
    return True, f"mise à jour appliquée (v {man.get('label') or '?'}) — redémarrage en cours{extra}"


def rollback():
    """Restaure le dernier backup puis relance le service."""
    bk = latest_backup()
    if not bk:
        return False, "aucun backup disponible"
    with tarfile.open(bk, "r:gz") as tar:
        tar.extractall(ROOT)   # noqa: S202 (archives produites par nous-mêmes)
    clear_pending()
    restart_service()
    return True, f"rollback depuis {os.path.basename(bk)} — redémarrage en cours"


def record_deploy(build_id=None, label=None):
    """Mémorise la date/heure du déploiement appliqué sur CETTE instance."""
    try:
        with open(DEPLOY_INFO_PATH, "w") as f:
            json.dump({"deployed_at": datetime.now().isoformat(timespec="seconds"),
                       "build_id": build_id, "label": label}, f, indent=2)
    except Exception as e:
        log.debug("record_deploy: %s", e)


def deploy_info():
    """Infos du dernier déploiement appliqué localement, ou {} si jamais déployé (dev)."""
    try:
        with open(DEPLOY_INFO_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def clear_pending():
    try:
        os.remove(PENDING_PATH)
    except FileNotFoundError:
        pass


def pending_build_id():
    """Renvoie le build_id ciblé par une mise à jour en attente de validation, ou None."""
    try:
        with open(PENDING_PATH) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def confirm_boot_ok():
    """Appelée au démarrage : si l'app remonte, la mise à jour en attente est validée."""
    if os.path.exists(PENDING_PATH):
        log.info("Mise à jour confirmée au boot (build %s)", pending_build_id())
        clear_pending()
