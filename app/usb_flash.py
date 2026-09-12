# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France

"""Gravure de la clé USB d'enrôlement DEPUIS le contrôleur (option « graver sur le serveur »).

Détecte les clés USB amovibles branchées sur l'hôte du contrôleur, construit l'ISO préseedée
(délégué à node_agent/iso/build-node-iso.sh) avec les paramètres du profil de nœud, puis la grave
(`dd`). Opération DESTRUCTIVE → garde-fous : seuls les périphériques AMOVIBLES (rm=1 / bus usb) de
type disk sont proposés, le ou les disques système sont exclus, et le device cible est RE-VALIDÉ
côté serveur contre la liste fraîche avant tout `dd` (jamais de confiance au chemin client).

Prérequis hôte : `xorriso` (paquet) + une ISO netinst Debian source (chemin en réglage `node_iso_src`).
"""
import json
import os
import shlex
import subprocess
import threading
import time
import logging

from . import settings

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_SCRIPT = os.path.join(ROOT, "node_agent", "iso", "build-node-iso.sh")
# Dossier où l'ISO netinst Debian SOURCE est téléchargée (champ « depuis une URL »). Séparé du cache
# des ISO préseedées par-nœud (node_iso_cache/) qui est purgé par token. gitignored.
ISO_SRC_DIR = os.path.join(ROOT, "iso_src")

# État global d'un (unique) téléchargement d'ISO source en cours.
_dl_status = {"state": "idle", "msg": "", "pct": 0, "at": 0.0}
_dl_lock = threading.Lock()

# État global d'une (unique) gravure en cours. Une seule à la fois (un seul dd hôte).
_status = {"state": "idle", "msg": "", "device": None, "node_id": None, "at": 0.0}
_lock = threading.Lock()


def _human(n):
    n = float(n)
    for u in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "o" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} Po"


def _system_disks():
    """Noms des disques (type=disk) portant un montage système (/, /boot, swap) → JAMAIS gravables,
    même s'ils se déclarent amovibles. Garde-fou ultime contre l'effacement du contrôleur."""
    sysd = set()
    try:
        out = subprocess.run(["lsblk", "-J", "-o", "NAME,MOUNTPOINT,TYPE"],
                             capture_output=True, text=True, timeout=10).stdout
        data = json.loads(out or "{}")

        def walk(dev, disk):
            here = dev.get("name") if dev.get("type") == "disk" else disk
            mp = dev.get("mountpoint") or ""
            if mp == "/" or mp == "[SWAP]" or mp.startswith("/boot"):
                if here:
                    sysd.add(here)
            for ch in dev.get("children") or []:
                walk(ch, here)

        for d in data.get("blockdevices") or []:
            walk(d, None)
    except Exception as e:
        log.warning("usb_flash._system_disks: %s", e)
    return sysd


def list_removable():
    """Clés amovibles candidates : type=disk, amovible (rm) OU bus usb, hors disque système."""
    devs = []
    sysd = _system_disks()
    try:
        out = subprocess.run(["lsblk", "-J", "-b", "-d", "-o", "NAME,SIZE,MODEL,TRAN,RM,TYPE,VENDOR"],
                             capture_output=True, text=True, timeout=10).stdout
        data = json.loads(out or "{}")
        for d in data.get("blockdevices") or []:
            if d.get("type") != "disk":
                continue
            removable = bool(d.get("rm")) or d.get("tran") == "usb"
            if not removable or d.get("name") in sysd:
                continue
            size = int(d.get("size") or 0)
            label = " ".join(s for s in [(d.get("vendor") or "").strip(),
                                         (d.get("model") or "").strip()] if s) or "USB"
            devs.append({"name": d.get("name"), "path": f"/dev/{d.get('name')}",
                         "size": size, "size_h": _human(size), "model": label,
                         "tran": d.get("tran") or ""})
    except Exception as e:
        log.warning("usb_flash.list_removable: %s", e)
    return devs


def _have(binary):
    return subprocess.run(["bash", "-lc", f"command -v {shlex.quote(binary)}"],
                          capture_output=True).returncode == 0


def prereqs():
    iso = (settings.get("node_iso_src") or "").strip()
    return {"xorriso": _have("xorriso"),
            "iso_src": iso,
            "iso_ok": bool(iso) and os.path.isfile(iso),
            "build_script": os.path.isfile(BUILD_SCRIPT)}


def status():
    with _lock:
        return dict(_status)


def build_node_iso(node, enroll_token, controller_url, out_path):
    """Construit l'ISO préseedée par-nœud (délègue à build-node-iso.sh / xorriso) vers `out_path`.
    Réutilisé par la gravure USB ET par le service HTTP pour iLO (cf. app/node_iso.py).
    Retourne (ok, msg). Vérifie les prérequis (xorriso + ISO source) avant de lancer le build."""
    pr = prereqs()
    if not pr["build_script"]:
        return False, "build-node-iso.sh introuvable sur le contrôleur"
    if not pr["xorriso"]:
        return False, "xorriso non installé (Réglages → Préparer l'hôte de gravure)"
    if not pr["iso_ok"]:
        return False, "ISO Debian source absente — définir son chemin"
    prof = {}
    try:
        prof = json.loads(node.get("enroll_profile") or "{}")
    except Exception:
        pass
    cmd = ["bash", BUILD_SCRIPT, "--src-iso", pr["iso_src"], "--out", out_path,
           "--controller-url", controller_url, "--enroll-token", enroll_token]
    if prof.get("mgmt_ip"):
        cmd += ["--ip", prof["mgmt_ip"],
                "--netmask", prof.get("mgmt_netmask") or "",
                "--gateway", prof.get("mgmt_gateway") or ""]
        if prof.get("mgmt_dns"):
            cmd += ["--nameservers", prof["mgmt_dns"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return False, "délai dépassé pendant le build de l'ISO"
    if r.returncode != 0:
        return False, "build ISO échoué : " + ((r.stderr or r.stdout)[-300:] or "?")
    return True, "ok"


def _set(state, msg):
    with _lock:
        _status.update({"state": state, "msg": msg, "at": time.time()})
    log.info("usb_flash: [%s] %s", state, msg)


def start_flash(node, enroll_token, controller_url, device):
    """Démarre build+gravure en tâche de fond. Retourne (ok, msg). Refuse si une gravure tourne déjà."""
    with _lock:
        if _status["state"] == "running":
            return False, "une gravure est déjà en cours"
        _status.update({"state": "running", "msg": "démarrage…", "device": device,
                        "node_id": node.get("id"), "at": time.time()})
    threading.Thread(target=_run, args=(node, enroll_token, controller_url, device),
                     daemon=True).start()
    return True, "started"


def _run(node, enroll_token, controller_url, device):
    import tempfile
    from .database import db_add_alert
    out_iso = None
    try:
        # Garde-fou : le device DOIT figurer dans la liste amovible FRAÎCHE (jamais le chemin client brut).
        if device not in [d["path"] for d in list_removable()]:
            return _set("error", f"{device} n'est pas une clé amovible détectée — gravure refusée")
        out_iso = tempfile.NamedTemporaryFile(suffix=".iso", delete=False).name
        _set("running", "construction de l'ISO préseedée…")
        ok, msg = build_node_iso(node, enroll_token, controller_url, out_iso)
        if not ok:
            return _set("error", msg)
        _set("running", f"gravure sur {device} en cours — ne pas retirer la clé…")
        dd = subprocess.run(["dd", f"if={out_iso}", f"of={device}", "bs=4M",
                             "oflag=sync", "status=none"],
                            capture_output=True, text=True, timeout=2400)
        if dd.returncode != 0:
            return _set("error", "dd échoué : " + ((dd.stderr or "")[-300:] or "?"))
        subprocess.run(["sync"], timeout=120)
        _set("done", f"clé prête sur {device} — bootable. Insérer dans le serveur cible et démarrer dessus.")
        db_add_alert("alert.prep.usb_gravee", "info", node_id=node.get("id"), kind="prep",
                     params={"n": node.get("name"), "device": device})
    except subprocess.TimeoutExpired:
        _set("error", "délai dépassé pendant le build ou la gravure")
    except Exception as e:
        _set("error", str(e))
    finally:
        if out_iso and os.path.isfile(out_iso):
            try:
                os.unlink(out_iso)
            except Exception:
                pass


def download_status():
    with _dl_lock:
        return dict(_dl_status)


def _dl_set(state, msg, pct=None):
    with _dl_lock:
        _dl_status.update({"state": state, "msg": msg, "at": time.time()})
        if pct is not None:
            _dl_status["pct"] = pct
    log.info("usb_flash.download: [%s] %s", state, msg)


def start_download(url):
    """Télécharge l'ISO netinst Debian SOURCE depuis `url` en tâche de fond, puis renseigne le réglage
    `node_iso_src`. Retourne (ok, msg). Refuse si un téléchargement tourne déjà."""
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "URL http(s) requise"
    with _dl_lock:
        if _dl_status["state"] == "running":
            return False, "un téléchargement est déjà en cours"
        _dl_status.update({"state": "running", "msg": "démarrage…", "pct": 0, "at": time.time()})
    threading.Thread(target=_download, args=(url,), daemon=True).start()
    return True, "started"


def _download(url):
    import requests
    tmp = None
    try:
        os.makedirs(ISO_SRC_DIR, exist_ok=True)
        name = os.path.basename(url.split("?")[0]) or "debian-netinst.iso"
        if not name.endswith(".iso"):
            name += ".iso"
        dest = os.path.join(ISO_SRC_DIR, name)
        tmp = dest + ".part"
        _dl_set("running", f"téléchargement de {name}…", 0)
        with requests.get(url, stream=True, timeout=60) as r:
            if r.status_code != 200:
                return _dl_set("error", f"HTTP {r.status_code} sur {url}")
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = int(done * 100 / total)
                        _dl_set("running", f"{_human(done)} / {_human(total)}", pct)
                    else:
                        _dl_set("running", _human(done), 0)
        # Garde-fou : un .iso doit commencer par la signature ISO9660 « CD001 » (offset 0x8001).
        with open(tmp, "rb") as f:
            f.seek(0x8001)
            if f.read(5) != b"CD001":
                os.unlink(tmp)
                return _dl_set("error", "le fichier téléchargé n'est pas une ISO valide (signature absente)")
        os.replace(tmp, dest)
        settings.set("node_iso_src", dest)
        _dl_set("done", f"ISO source prête : {dest}", 100)
    except Exception as e:
        if tmp and os.path.isfile(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
        _dl_set("error", str(e))


def install_tools():
    """Installe xorriso sur le contrôleur (apt). Retourne (ok, msg). Root requis."""
    try:
        r = subprocess.run(["bash", "-lc", "apt-get update -qq && apt-get install -y -qq xorriso"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout)[-300:]
        return True, "xorriso installé"
    except Exception as e:
        return False, str(e)
