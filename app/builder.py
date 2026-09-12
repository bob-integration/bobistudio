# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Génération du paquet de distribution (`dist/bobistudio.zip`).

Source unique de vérité du build : appelée par l'UI (Réglages → Déploiement) et par
le wrapper CLI `tools/build_dist.py`. Produit un zip ne contenant QUE le code (cœur +
plugins/services sélectionnés), à l'exclusion stricte de tout secret ou état local
(`config_local.py`, bases `*.db`, `backups/`, venv, caches…).

Garde-fou : après écriture, le zip est relu et le build échoue si un motif sensible
y est détecté — on n'expose jamais le token Proxmox ni une base de données.
"""
import json
import os
import subprocess
import uuid
import zipfile
from datetime import datetime

# Racine du dépôt = parent de app/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(ROOT, "dist")
DEFAULT_DEST = os.path.join(DIST_DIR, "bobistudio.zip")
MANIFEST_PATH = os.path.join(DIST_DIR, "build_manifest.json")
# Identité de build embarquée dans le zip (lue par une instance pour rapporter sa version).
BUILD_INFO_PATH = os.path.join(ROOT, "build_info.json")

# ── Cœur : toujours inclus ───────────────────────────────────
# Dossiers embarqués intégralement (hors motifs exclus ci-dessous).
CORE_DIRS = ["app", "templates", "static", "script_templates", "i18n", "node_agent", "tools",
             # docs/ : conception, références et dossiers de chantier. Embarqué parce que la page
             # Aide rend certains de ces markdown depuis la SEULE source versionnée (/api/doc).
             "docs",
             # licenses/ : textes intégraux des licences tierces. Apache-2.0 §4(a) et BSD-3-Clause
             # exigent qu'une COPIE accompagne la redistribution — les omettre de la liste blanche
             # rendrait chaque paquet installé non conforme, alors que le dépôt l'est.
             "licenses"]
# Contextes de build des images runtime Docker (PAS des plugins : pas de plugin.json, donc absents
# de la liste sélectionnable). Toujours embarqués : l'onglet Réglages → Déploiement → Local en a
# besoin pour builder bobi-compute/bobi-media sur l'hôte. (Le contexte MTL voyage avec le plugin
# 2110_io/docker.)
RUNTIME_IMAGE_DIRS = ["plugins/_compute_runtime", "plugins/_compute_gpu_runtime",
                      "plugins/_media_runtime", "plugins/_webrtc_runtime"]
# Agent-nœud + installeur (bobi-node-agent) : embarqués pour que le contrôleur puisse les
# distribuer/installer sur les nœuds (séparation control/node-plane, cf. NODE_AGENT.md).
# Fichiers individuels à la racine.
CORE_FILES = [
    "main.py", "requirements.txt", "bobistudio.service",
    "config_local.example.py", "install.sh", "install/install.py",
    "install/install_proxmox.py", "build_info.json",
    # Doc publique de la racine. INSTALL/INFRASTRUCTURE/HA/THIRD-PARTY sont rendus par la page
    # Aide (/api/doc) : les OMETTRE ici casserait l'aide en ligne sur toute instance installée.
    "LICENSE", "CHANGELOG.md", "CLAUDE.md", "README.md", "NODE_AGENT.md",
    "INSTALL.md", "INFRASTRUCTURE.md", "HA.md", "THIRD-PARTY-NOTICES.md",
    "CONTRIBUTING.md",
    "plugins/AUTHORING.md", "plugins/AUTHORING.fr.md",   # rendu par la page Aide (article « Contribuer ») → doit être embarqué
]

# Sélection par défaut (paquet « broadcast » minimal).
DEFAULT_PLUGINS = ["2110_io", "streamer"]
DEFAULT_SERVICES = ["nmos", "webrtc_gateway", "files", "media_manager"]

# ── Exclusions (n'importe où dans le chemin relatif) ─────────
# Secrets / état local : JAMAIS dans le zip.
# ⚠ `static/uploads/` en fait partie : c'est de l'état d'instance, pas un secret au sens strict,
# mais le publier revient à diffuser ce que les utilisateurs de cette installation ont déposé.
# Le mettre ICI en plus de `EXCLUDE_PATHS` est délibéré : l'exclusion peut être défaite par une
# refonte de la liste blanche, le garde-fou de sortie, lui, REFUSE le build. Deux mécanismes
# indépendants pour la même règle, parce que celle-ci ne pardonne pas.
SECRET_PATTERNS = ["config_local.py", "backups/", "static/uploads/"]   # + tout .db (cf. _is_secret)

# Bruit / non pertinent au déploiement.
EXCLUDE_DIRS = {
    "venv", ".git", "__pycache__", ".claude", ".agents", "_infos",
    "old", "dist", "node_modules", ".pytest_cache",
}
# `web/` n'a pas besoin d'y figurer : CORE_DIRS est une liste BLANCHE et ne le nomme pas.
# (`push_to_github.sh` et `skills-lock.json` ont été supprimés le 2026-09-01 ; `sync_repos.sh`
#  est parti dans old/, déjà couvert par EXCLUDE_DIRS.)
EXCLUDE_FILES = {
    "config_local.py", ".impeccable",
}
# ── Chemins d'ÉTAT D'INSTANCE, exclus par PRÉFIXE ────────────────────────────
#
# ⚠ `static/` est embarqué en entier par CORE_DIRS, et `static/uploads/` s'y trouve — or ce
# dossier est GITIGNORÉ : ce n'est pas du code, c'est ce que les utilisateurs de CETTE
# installation y ont déposé. Logo de marque, polices téléversées, et surtout les images servies
# depuis l'interface : sur le contrôleur de l'éditeur, 76 fichiers nommés d'après des machines
# de production, soit 14 Mo. Un paquet construit depuis le dépôt de travail les emportait donc
# dans une release publique — vérifié le 2026-09-03, à un clic près.
#
# Les releases publiques échappaient au problème PAR ACCIDENT DE MÉTHODE : elles sont
# construites depuis l'arbre de publication, qui ne contient que du versionné et n'a donc pas ce
# dossier. Un garde-fou qui dépend de l'endroit d'où l'on lance la commande n'en est pas un.
#
# Exclu par CHEMIN et non par nom : `EXCLUDE_DIRS` écarterait tout dossier « uploads », y
# compris celui qu'un plugin aurait le droit de porter.
EXCLUDE_PATHS = (
    "static/uploads",
)
EXCLUDE_EXT = {".db", ".log", ".pyc", ".pyo"}


def _is_secret(rel):
    """Vrai si le chemin relatif est un secret/état local interdit (garde-fou)."""
    low = rel.replace("\\", "/").lower()
    if low.endswith(".db"):
        return True
    return any(p in low for p in (p_.lower() for p_ in SECRET_PATTERNS))


def _excluded(rel):
    """Vrai si le chemin relatif doit être écarté du zip."""
    norm = rel.replace("\\", "/")
    if any(norm == p or norm.startswith(p + "/") for p in EXCLUDE_PATHS):
        return True
    parts = norm.split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    base = parts[-1]
    if base in EXCLUDE_FILES:
        return True
    if os.path.splitext(base)[1].lower() in EXCLUDE_EXT:
        return True
    return False


def _add_dir(zf, abs_dir, arc_prefix):
    """Ajoute récursivement un dossier au zip en filtrant les exclusions."""
    for dirpath, dirnames, filenames in os.walk(abs_dir):
        # Élagage des dossiers exclus (perf + cohérence). On élague AUSSI sur le chemin
        # d'archive, sans quoi `os.walk` descendrait dans `static/uploads` pour ne rien en
        # garder — inutile, et trompeur à la lecture.
        arc_dir = os.path.join(arc_prefix, os.path.relpath(dirpath, abs_dir)).replace("\\", "/")
        arc_dir = arc_dir[2:] if arc_dir.startswith("./") else arc_dir
        dirnames[:] = [d for d in dirnames
                       if d not in EXCLUDE_DIRS and not _excluded(os.path.join(arc_dir, d))]
        for fn in filenames:
            absf = os.path.join(dirpath, fn)
            rel = os.path.relpath(absf, abs_dir)
            arc = os.path.join(arc_prefix, rel)
            if _excluded(arc):
                continue
            zf.write(absf, arc)


def _git_hash():
    """Short hash git du dépôt, suffixé `-dirty` si l'arbre de travail diverge du commit.

    Sans ce suffixe, deux artefacts au CONTENU différent porteraient le même identifiant dès
    qu'on build sans avoir commité — et l'interface de mise à jour afficherait « même version »
    en envoyant pourtant autre chose. Le hash doit désigner le contenu, pas seulement le dernier
    commit. None hors dépôt git."""
    try:
        out = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        h = out.stdout.strip()
        if not h:
            return None
        # `--porcelain` : non vide = arbre modifié (fichiers suivis). Les sous-modules « sales »
        # comptent aussi — un plugin modifié change bien le contenu de l'artefact.
        st = subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"],
                            capture_output=True, text=True, timeout=10)
        if st.returncode == 0 and st.stdout.strip():
            h += "-dirty"
        return h
    except Exception:
        return None


def current_build_info():
    """Identité de build de CETTE instance (build_info.json), ou un repère 'dev' si absent."""
    try:
        with open(BUILD_INFO_PATH) as f:
            return json.load(f)
    except Exception:
        gh = _git_hash()
        return {"build_id": None, "label": f"dev{(' ' + gh) if gh else ''}",
                "built_at": None, "git_hash": gh}


def available():
    """Liste les plugins et services installés, pour la sélection UI."""
    from . import plugins, core_plugins
    plugs = []
    for m in plugins.all():
        plugs.append({"type": m.get("type"), "label": m.get("label") or m.get("type"),
                      "version": m.get("version", "")})
    plugs.sort(key=lambda p: p["type"] or "")
    servs = []
    for m in core_plugins.manifest_list():
        sid = m.get("id") or m.get("name")
        servs.append({"id": sid, "label": m.get("label") or sid,
                      "description": m.get("description", ""),
                      "version": m.get("version", "")})
    servs.sort(key=lambda s: s["id"] or "")
    return {"plugins": plugs, "services": servs}


def last_selection():
    """Dernière sélection utilisée (build_manifest.json) ou défauts."""
    try:
        with open(MANIFEST_PATH) as f:
            m = json.load(f)
        return {"plugins": m.get("plugins") or DEFAULT_PLUGINS,
                "services": m.get("services") or DEFAULT_SERVICES,
                "built_at": m.get("built_at")}
    except Exception:
        return {"plugins": DEFAULT_PLUGINS, "services": DEFAULT_SERVICES, "built_at": None}


def build(plugins=None, services=None, dest=DEFAULT_DEST, stamp=True, offline=False,
          images=False, log=None):
    """Construit le zip de distribution. Retourne un dict résumé.

    plugins/services : listes d'ids à inclure (None → défauts).
    stamp : si True (build explicite = nouvelle release), (re)génère l'identité de build
    (build_info.json). Si False (build paresseux pour servir un zip manquant), réutilise
    l'identité existante afin que la version de l'instance reste stable.
    offline : si True, pré-télécharge les dépendances (roues pip + .deb système) dans `vendor/`
    et les EMBARQUE dans le zip → l'installeur pourra déployer SANS réseau. Zip beaucoup plus
    lourd (~300 Mo). Défaut False = zip léger (code seul, deps installées en ligne).
    images : si True, `docker save` les images runtime Docker dans `vendor/images/` et les EMBARQUE
    (chargées via `docker load` à l'install du nœud) → nœud opérationnel sans registre. Alourdit
    fortement le zip (plusieurs Go). Implique l'inclusion de `vendor/` dans le zip.
    log : callable(str) optionnel, pour remonter la progression du pré-téléchargement.
    Lève RuntimeError si le garde-fou anti-secret se déclenche, ou si le pré-téléchargement
    hors-ligne échoue.
    """
    plugins = list(plugins) if plugins is not None else list(DEFAULT_PLUGINS)
    services = list(services) if services is not None else list(DEFAULT_SERVICES)

    # Pré-téléchargement des dépendances (+ images) pour l'embarquement (avant d'ouvrir le zip).
    embed_vendor = offline or images
    if offline:
        from . import offline_bundle
        res = offline_bundle.ensure_all(log=log, images=images)
        if not res.get("ok"):
            err = (res.get("wheels", {}).get("error") or res.get("debs", {}).get("error")
                   or (res.get("images") or {}).get("error")
                   or "échec du pré-téléchargement hors-ligne")
            raise RuntimeError("Bundle hors-ligne : " + err)
    elif images:
        # Images sans deps hors-ligne : matérialiser seulement les tars.
        from . import offline_bundle
        res = offline_bundle.ensure_images(log=log)
        if not res.get("ok"):
            raise RuntimeError("Bundle images : " + (res.get("error") or "aucune image matérialisable"))

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)

    # Identité de build : écrite AVANT le zip pour être embarquée (build_info.json est
    # dans CORE_FILES). Permet à l'instance déployée de rapporter sa version.
    existing = current_build_info() if os.path.exists(BUILD_INFO_PATH) else None
    if stamp or not existing or not existing.get("build_id"):
        built_at = datetime.now().isoformat(timespec="seconds")
        git_hash = _git_hash()
        build_id = uuid.uuid4().hex
        label = built_at + (f" · {git_hash}" if git_hash else "")
        try:
            with open(BUILD_INFO_PATH, "w") as f:
                json.dump({"build_id": build_id, "label": label,
                           "built_at": built_at, "git_hash": git_hash}, f, indent=2)
        except Exception:
            pass
    else:
        build_id = existing["build_id"]; label = existing.get("label")
        built_at = existing.get("built_at"); git_hash = existing.get("git_hash")

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in CORE_DIRS:
            absd = os.path.join(ROOT, d)
            if os.path.isdir(absd):
                _add_dir(zf, absd, d)
        for f in CORE_FILES:
            absf = os.path.join(ROOT, f)
            if os.path.isfile(absf) and not _excluded(f):
                zf.write(absf, f)
        for p in plugins:
            absd = os.path.join(ROOT, "plugins", p)
            if os.path.isdir(absd):
                _add_dir(zf, absd, os.path.join("plugins", p))
        # Contextes d'images runtime (toujours, indépendamment de la sélection de plugins).
        for d in RUNTIME_IMAGE_DIRS:
            absd = os.path.join(ROOT, d)
            if os.path.isdir(absd):
                _add_dir(zf, absd, d)
        for s in services:
            absd = os.path.join(ROOT, "services", s)
            if os.path.isdir(absd):
                _add_dir(zf, absd, os.path.join("services", s))
        # services/__init__.py est nécessaire pour que `services` soit un package.
        init_abs = os.path.join(ROOT, "services", "__init__.py")
        if os.path.isfile(init_abs):
            zf.write(init_abs, os.path.join("services", "__init__.py"))
        # Bundle embarqué : roues pip + .deb système + images Docker (vendor/), déploiement sans réseau.
        if embed_vendor:
            vendor_abs = os.path.join(ROOT, "vendor")
            if os.path.isdir(vendor_abs):
                _add_dir(zf, vendor_abs, "vendor")

    # ── Garde-fou : aucun secret ne doit avoir fui ───────────
    with zipfile.ZipFile(tmp) as zf:
        names = zf.namelist()
        leaked = [n for n in names if _is_secret(n)]
    if leaked:
        os.remove(tmp)
        raise RuntimeError("Build refusé — fichiers sensibles détectés dans le zip : "
                           + ", ".join(leaked[:10]))

    os.replace(tmp, dest)

    # Rafraîchir les installeurs servis à côté du zip (/install/install.py + install_proxmox.py).
    import shutil
    for _inst in ("install/install.py", "install/install_proxmox.py"):
        _src = os.path.join(ROOT, _inst)
        if os.path.isfile(_src):
            # basename : la ROUTE sert « /install/install.py » depuis DIST_DIR à plat, et
            # l'installeur téléchargé doit atterrir À CÔTÉ du zip (cf. install.py:_find_source).
            shutil.copy2(_src, os.path.join(DIST_DIR, os.path.basename(_inst)))

    size = os.path.getsize(dest)
    offline_info = {}
    if embed_vendor:
        from . import offline_bundle
        offline_info = offline_bundle.status()
    # Manifeste de build (mémorise la sélection pour l'UI).
    try:
        with open(MANIFEST_PATH, "w") as f:
            json.dump({"plugins": plugins, "services": services,
                       "built_at": built_at, "file": os.path.basename(dest),
                       "size": size, "count": len(names), "offline": bool(offline),
                       "images": bool(images),
                       "offline_wheels": offline_info.get("wheels"),
                       "offline_debs": offline_info.get("debs"),
                       "offline_images": offline_info.get("images"),
                       "build_id": build_id, "label": label, "git_hash": git_hash}, f, indent=2)
    except Exception:
        pass

    return {"ok": True, "file": os.path.basename(dest), "path": dest,
            "size": size, "count": len(names), "offline": bool(offline),
            "images": bool(images),
            "offline_wheels": offline_info.get("wheels"),
            "offline_debs": offline_info.get("debs"),
            "offline_images": offline_info.get("images"),
            "plugins": plugins, "services": services, "built_at": built_at,
            "build_id": build_id, "label": label, "git_hash": git_hash}
