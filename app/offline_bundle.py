# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Fabrication du bundle hors-ligne (`vendor/`) — pré-téléchargement des dépendances pour un
déploiement SANS réseau sur la cible.

Appelé côté BUILD (machine en ligne : l'orchestrateur qui génère le paquet), jamais côté
install. Produit deux dossiers, embarqués dans `dist/bobistudio.zip` quand le build « complet »
est demandé (cf. `builder.build(offline=True)`) :

  vendor/wheels/  ← roues pip de requirements.txt (binaires manylinux, aucun sdist à compiler)
  vendor/debs/    ← clôture récursive .deb des paquets système + index `Packages.gz`

L'installeur (`install.py:install_deps_bare`) détecte ces dossiers dans le paquet extrait et
bascule en mode hors-ligne : dépôt apt local `file://` (apt ne touche pas ce qui est déjà
satisfait → zéro downgrade) + `pip --no-index --find-links`. Sans eux, il garde le chemin en ligne.

Idempotent : un `stamp.json` mémorise le hash de `requirements.txt` et la liste de paquets ; un
rappel sans changement ne re-télécharge pas (sauf `force=True`).

La cible DOIT être identique à la machine de build (même Debian/archi/Python) — les roues binaires
et les .deb sont spécifiques à la plateforme. Voir la question de cadrage à la génération.
"""
import glob
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(ROOT, "vendor")
WHEELS_DIR = os.path.join(VENDOR_DIR, "wheels")
DEBS_DIR = os.path.join(VENDOR_DIR, "debs")
IMAGES_DIR = os.path.join(VENDOR_DIR, "images")
REQ_PATH = os.path.join(ROOT, "requirements.txt")
STAMP_PATH = os.path.join(VENDOR_DIR, "stamp.json")

# Image runtime → capacité de nœud qui l'exécute (cf. node_agent/install-node.sh). Sert à ne charger
# sur un nœud QUE les images de ses capacités (pas la lourde image mtl sur un nœud compute-only).
IMAGE_CAPS = {"compute": "compute", "compute-gpu": "compute",
              "media": "media", "webrtc": "webrtc", "mtl": "io2110"}

# Paquets système embarqués pour le mode hors-ligne. La clôture récursive de cette union est
# téléchargée : l'orchestrateur (install.py) et le nœud (node_agent/install-node.sh) y puisent
# chacun leur sous-ensemble depuis le MÊME dépôt local.
# Orchestrateur (cf. install.py:install_deps_bare).
ORCH_PACKAGES = ["python3", "python3-venv", "python3-pip",
                 "ffmpeg", "rsync", "curl", "cifs-utils", "nfs-common"]
# Nœud de process (cf. node_agent/install-node.sh) : l'agent est stdlib pur (aucune roue), mais il
# faut Docker + outils système. Le noyau MTL/DPDK (io2110) reste hors bundle (version-spécifique).
# docker-cli + docker-buildx explicites : Debian 13 les a scindés de docker.io (Recommends), la
# clôture .deb ne suit que les Depends → sans eux le bundle hors-ligne pose le daemon mais pas le
# binaire `docker` (preflight KO) ni le builder BuildKit (« buildx component is missing » au build).
NODE_PACKAGES = ["docker.io", "docker-cli", "docker-buildx", "ethtool", "ca-certificates", "linuxptp"]
APT_PACKAGES = sorted(set(ORCH_PACKAGES + NODE_PACKAGES))


def _req_hash():
    try:
        with open(REQ_PATH, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


def _read_stamp():
    try:
        with open(STAMP_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_stamp(**kw):
    os.makedirs(VENDOR_DIR, exist_ok=True)
    stamp = _read_stamp()
    stamp.update(kw)
    try:
        with open(STAMP_PATH, "w") as f:
            json.dump(stamp, f, indent=2)
    except OSError:
        pass


def _run(cmd, cwd=None):
    """Lance une commande. Retourne (rc, stdout, stderr) SÉPARÉS.

    Séparés impérativement : plusieurs outils (dpkg-scanpackages, apt-cache) écrivent des
    infos sur stderr — les mêler à stdout corromprait un index Packages ou une liste de paquets.
    """
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout or ""), (r.stderr or "")


# ─── Roues Python ────────────────────────────────────────────────────────────

def ensure_wheels(force=False, log=None):
    """Télécharge les roues de requirements.txt dans vendor/wheels/. Idempotent.

    Retourne un dict {ok, count, skipped, error}. log : callable(str) optionnel.
    """
    _log = log or (lambda m: None)
    req_h = _req_hash()
    existing = sorted(glob.glob(os.path.join(WHEELS_DIR, "*.whl")))
    stamp = _read_stamp()
    if (not force and existing and stamp.get("wheels_req_hash") == req_h):
        _log(f"vendor/wheels à jour ({len(existing)} roues) — ignoré")
        return {"ok": True, "count": len(existing), "skipped": True}

    os.makedirs(WHEELS_DIR, exist_ok=True)
    for old in existing:
        try:
            os.remove(old)
        except OSError:
            pass
    _log("Téléchargement des roues pip…")
    rc, out, err = _run([sys.executable, "-m", "pip", "download",
                         "-r", REQ_PATH, "-d", WHEELS_DIR])
    if rc != 0:
        return {"ok": False, "error": "pip download a échoué :\n" + (out + err)[-2000:]}
    wheels = sorted(glob.glob(os.path.join(WHEELS_DIR, "*.whl")))
    _write_stamp(wheels_req_hash=req_h, wheels_count=len(wheels))
    _log(f"{len(wheels)} roues téléchargées")
    return {"ok": True, "count": len(wheels), "skipped": False}


# ─── Paquets .deb ────────────────────────────────────────────────────────────

def _apt_closure(packages):
    """Liste des noms de paquets de la clôture récursive (Depends only)."""
    rc, out, err = _run(["apt-cache", "depends", "--recurse",
                         "--no-recommends", "--no-suggests", "--no-conflicts",
                         "--no-breaks", "--no-replaces", "--no-enhances",
                         "--no-pre-depends"] + list(packages))
    if rc != 0:
        return None, (out + err)
    # Les vrais noms de paquets commencent en colonne 0 ; les dépendances virtuelles / alternatives
    # sont indentées ou entre chevrons — on les écarte. On parse stdout SEUL (stderr = bruit).
    names = sorted({ln.strip() for ln in out.splitlines()
                    if ln and not ln[0].isspace() and not ln.startswith("<")})
    return names, out


def ensure_debs(force=False, log=None):
    """Télécharge la clôture .deb des paquets système + génère l'index Packages.gz.

    Nécessite root (apt) sur la machine de build. Idempotent via le stamp (hash de la liste).
    Retourne {ok, count, skipped, error}.
    """
    _log = log or (lambda m: None)
    pkgs_key = hashlib.sha256(",".join(sorted(APT_PACKAGES)).encode()).hexdigest()[:16]
    existing = sorted(glob.glob(os.path.join(DEBS_DIR, "*.deb")))
    stamp = _read_stamp()
    if (not force and existing and stamp.get("debs_pkgs_key") == pkgs_key
            and os.path.exists(os.path.join(DEBS_DIR, "Packages.gz"))):
        # Téléchargement sauté (déjà en cache), MAIS l'index est TOUJOURS régénéré : un index issu
        # d'un build antérieur (code de génération bogué) doit être réécrit proprement, jamais
        # réembarqué tel quel. La génération est rapide (~1 s) et sans réseau.
        _log(f"vendor/debs en cache ({len(existing)} paquets) — régénération de l'index")
        if not _gen_packages_index(DEBS_DIR):
            return {"ok": False, "error": "régénération de l'index Packages a échoué."}
        return {"ok": True, "count": len(existing), "skipped": True}

    if os.geteuid() != 0:
        return {"ok": False, "error": "root requis pour télécharger les .deb (apt)."}

    os.makedirs(DEBS_DIR, exist_ok=True)
    # Index apt frais : sinon un point-release peut avoir retiré du pool la version indexée (404).
    _log("apt-get update…")
    _run(["apt-get", "update", "-qq"])

    _log("Calcul de la clôture des dépendances…")
    names, out = _apt_closure(APT_PACKAGES)
    if not names:
        return {"ok": False, "error": "apt-cache depends a échoué :\n" + (out or "")[-1500:]}

    for old in existing:
        try:
            os.remove(old)
        except OSError:
            pass
    _log(f"Téléchargement de {len(names)} paquets .deb…")
    # -o APT::Sandbox::User=root : écrire les .deb dans DEBS_DIR (l'utilisateur _apt n'y a pas accès).
    rc, out, err = _run(["apt-get", "download", "-o", "APT::Sandbox::User=root"] + names,
                        cwd=DEBS_DIR)
    got = sorted(glob.glob(os.path.join(DEBS_DIR, "*.deb")))
    if not got:
        return {"ok": False, "error": "apt-get download n'a produit aucun .deb :\n" + (out + err)[-1500:]}
    # Un 404 isolé (version retirée du pool) ne doit pas tout casser : on vérifie plutôt que les
    # paquets DEMANDÉS ont bien un .deb.
    missing = [p for p in APT_PACKAGES
               if not glob.glob(os.path.join(DEBS_DIR, p.replace("+", "%2b") + "_*.deb"))
               and not glob.glob(os.path.join(DEBS_DIR, p + "_*.deb"))]
    if missing:
        return {"ok": False,
                "error": f"paquets cibles manquants après téléchargement : {', '.join(missing)}\n"
                         + (out + err)[-1000:]}

    _log("Génération de l'index Packages.gz…")
    if not _gen_packages_index(DEBS_DIR):
        return {"ok": False, "error": "génération de l'index Packages a échoué (dpkg-scanpackages)."}

    _write_stamp(debs_pkgs_key=pkgs_key, debs_count=len(got))
    _log(f"{len(got)} paquets .deb + index prêts")
    return {"ok": True, "count": len(got), "skipped": False}


def _gen_packages_index(debs_dir):
    """Écrit debs_dir/Packages(.gz) via dpkg-scanpackages, repli apt-ftparchive."""
    import gzip
    pkgs_txt = None
    # stdout SEUL : dpkg-scanpackages écrit « info: N entrées écrites… » sur stderr — l'y mêler
    # produirait une section sans en-tête Package: → apt échoue (« section with no Package: header »).
    rc, out, err = _run(["dpkg-scanpackages", "--multiversion", "."], cwd=debs_dir)
    if rc == 0 and out.strip():
        pkgs_txt = out
    else:
        rc, out, err = _run(["apt-ftparchive", "packages", "."], cwd=debs_dir)
        if rc == 0 and out.strip():
            pkgs_txt = out
    if not pkgs_txt:
        return False
    try:
        with open(os.path.join(debs_dir, "Packages"), "w") as f:
            f.write(pkgs_txt)
        with gzip.open(os.path.join(debs_dir, "Packages.gz"), "wt") as f:
            f.write(pkgs_txt)
    except OSError:
        return False
    return True


# ─── Images Docker runtime ───────────────────────────────────────────────────

def ensure_images(which_list=None, log=None):
    """`docker save` chaque image runtime dans vendor/images/<which>.tar + manifeste.

    Réutilise images._materialize_image_tar : `docker save` local SI l'orchestrateur a Docker,
    SINON export depuis le nœud de build (relais) — donc marche même sans Docker côté contrôleur.
    Une image non matérialisable (absente partout : mtl/gpu buildés par-nœud, pas encore construits)
    est SIGNALÉE et sautée, pas silencieusement ignorée. Retourne {ok, images:[…], count, missing:[…]}.
    """
    _log = log or (lambda m: None)
    try:
        from .routes import images as img_routes
    except Exception as ex:
        return {"ok": False, "error": f"import images impossible : {ex}", "images": [], "count": 0}

    keys = which_list if which_list is not None else list(img_routes._IMAGES.keys())
    os.makedirs(IMAGES_DIR, exist_ok=True)
    # Purge des anciens tars (un rebuild doit refléter l'état courant, jamais un résidu).
    for old in glob.glob(os.path.join(IMAGES_DIR, "*.tar")):
        try:
            os.remove(old)
        except OSError:
            pass

    manifest, missing = [], []
    for which in keys:
        try:
            tag = img_routes._image_tag(which)
        except Exception as ex:
            missing.append({"which": which, "reason": f"tag: {ex}"})
            continue
        fname = which + ".tar"
        dest = os.path.join(IMAGES_DIR, fname)
        _log(f"docker save {tag} → images/{fname}…")
        try:
            ok, msg = img_routes._materialize_image_tar(which, tag, dest)
        except Exception as ex:
            ok, msg = False, str(ex)
        size = os.path.getsize(dest) if os.path.exists(dest) else 0
        if ok and size > 0:
            manifest.append({"which": which, "tag": tag, "file": fname,
                             "capability": IMAGE_CAPS.get(which, which), "size": size})
            _log(f"  {tag} embarquée ({size // (1024*1024)} Mo)")
        else:
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            missing.append({"which": which, "tag": tag, "reason": msg})
            _log(f"  image {which} ({tag}) NON embarquée : {msg}")

    try:
        with open(os.path.join(IMAGES_DIR, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
    except OSError as ex:
        return {"ok": False, "error": f"manifeste images : {ex}", "images": manifest,
                "count": len(manifest), "missing": missing}

    # ok tant qu'AU MOINS une image est embarquée (sinon le bundle « images » est vide → échec net).
    return {"ok": bool(manifest), "images": manifest, "count": len(manifest), "missing": missing}


# ─── Orchestration + état ────────────────────────────────────────────────────

def ensure_all(force=False, log=None, images=False):
    """Prépare wheels + debs (+ images si demandé). Retourne {ok, wheels, debs, images}."""
    w = ensure_wheels(force=force, log=log)
    d = ensure_debs(force=force, log=log)
    ok = bool(w.get("ok") and d.get("ok"))
    result = {"ok": ok, "wheels": w, "debs": d}
    if images:
        im = ensure_images(log=log)
        result["images"] = im
        result["ok"] = ok and bool(im.get("ok"))
    return result


def status():
    """État courant du bundle (pour l'UI / le manifeste de build)."""
    wheels = glob.glob(os.path.join(WHEELS_DIR, "*.whl"))
    debs = glob.glob(os.path.join(DEBS_DIR, "*.deb"))
    images = glob.glob(os.path.join(IMAGES_DIR, "*.tar"))
    stamp = _read_stamp()
    return {
        "wheels": len(wheels),
        "debs": len(debs),
        "images": len(images),
        "has_index": os.path.exists(os.path.join(DEBS_DIR, "Packages.gz")),
        "req_hash": _req_hash(),
        "wheels_req_hash": stamp.get("wheels_req_hash"),
        "ready": bool(wheels and debs and os.path.exists(os.path.join(DEBS_DIR, "Packages.gz"))),
    }
