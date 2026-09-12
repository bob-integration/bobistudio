# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France

"""Service HTTP de l'ISO d'enrôlement par-nœud — pour iLO 5 « Virtual Media URL ».

Construit la MÊME ISO préseedée que la gravure USB (cf. usb_flash.build_node_iso →
node_agent/iso/build-node-iso.sh, ISO9660 hybride El Torito BIOS+UEFI), mais au lieu de la `dd`
sur une clé puis la jeter, elle est CONSERVÉE dans un cache disque et exposée en HTTP à une URL
stable `/iso/<enroll_token>.iso` (cf. routes). iLO la monte alors comme CD/DVD virtuel.

L'URL est gardée par le enroll_token (one-time, urlsafe) : elle n'est PAS devinable et expire au
1er enrôlement (le token est consommé → la route renvoie 404 → purge du cache). L'ISO porte les
mêmes secrets que la clé USB : modèle de confiance inchangé.

Une seule construction `xorriso` à la fois (lourd) : état global type usb_flash.
"""
import os
import threading
import time
import logging

from . import usb_flash

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO_CACHE = os.path.join(ROOT, "node_iso_cache")

# État global d'une (unique) construction en cours (xorriso lourd, un seul build à la fois).
_status = {"state": "idle", "msg": "", "node_id": None, "url": None, "at": 0.0}
_lock = threading.Lock()


def _set(state, msg, **extra):
    with _lock:
        _status.update({"state": state, "msg": msg, "at": time.time(), **extra})
    log.info("node_iso: [%s] %s", state, msg)


def status():
    with _lock:
        return dict(_status)


def cache_path(token):
    return os.path.join(ISO_CACHE, f"{token}.iso")


def iso_url(controller_url, token):
    return f"{(controller_url or '').rstrip('/')}/iso/{token}.iso"


def _build_inputs_mtime():
    """mtime le plus récent des entrées de build (script + preseed source). Sert à INVALIDER l'ISO
    cachée quand le menu de boot ou le preseed a changé (sinon le déploiement iLO ressert une vieille
    ISO indéfiniment via le cache)."""
    paths = [os.path.join(ROOT, "node_agent", "iso", "build-node-iso.sh"),
             os.path.join(ROOT, "node_agent", "iso", "preseed.cfg")]
    m = 0.0
    for p in paths:
        try:
            m = max(m, os.path.getmtime(p))
        except Exception:
            pass
    return m


def is_ready(token):
    p = cache_path(token)
    if not (os.path.isfile(p) and os.path.getsize(p) > 0):
        return False
    # Cache périmé si une entrée de build est plus récente que l'ISO → forcer une reconstruction.
    try:
        if os.path.getmtime(p) < _build_inputs_mtime():
            return False
    except Exception:
        pass
    return True


def purge(token):
    """Supprime l'ISO cachée d'un token (appelé à la consommation du jeton / au boot)."""
    try:
        p = cache_path(token)
        if token and os.path.isfile(p):
            os.unlink(p)
            log.info("node_iso: ISO cache purgée pour le token %s…", token[:6])
    except Exception as e:
        log.warning("node_iso.purge: %s", e)


def purge_orphans(valid_tokens):
    """Purge best-effort des ISO du cache dont le token n'est plus un enroll_token vivant (boot)."""
    try:
        if not os.path.isdir(ISO_CACHE):
            return
        valid = set(valid_tokens or [])
        for fn in os.listdir(ISO_CACHE):
            if not fn.endswith(".iso"):
                continue
            tok = fn[:-4]
            if tok not in valid:
                try:
                    os.unlink(os.path.join(ISO_CACHE, fn))
                    log.info("node_iso: ISO orpheline purgée (%s)", fn)
                except Exception:
                    pass
    except Exception as e:
        log.warning("node_iso.purge_orphans: %s", e)


def start_build(node, enroll_token, controller_url):
    """Démarre la construction de l'ISO en tâche de fond. Retourne (ok, msg).
    Refuse si une construction tourne déjà."""
    with _lock:
        if _status["state"] == "running":
            return False, "une construction d'ISO est déjà en cours"
        _status.update({"state": "running", "msg": "démarrage…", "node_id": node.get("id"),
                        "url": None, "at": time.time()})
    threading.Thread(target=_run, args=(node, enroll_token, controller_url),
                     daemon=True).start()
    return True, "started"


def build_now(node, enroll_token, controller_url):
    """Construit l'ISO SYNCHRONEMENT (réutilisée par le déploiement iLO Redfish qui a besoin de
    l'ISO prête avant de monter le média). Retourne (ok, msg). Réutilise le cache si déjà prête."""
    if is_ready(enroll_token):
        return True, "déjà en cache"
    return _build(node, enroll_token, controller_url)


def _build(node, enroll_token, controller_url):
    os.makedirs(ISO_CACHE, exist_ok=True)
    out = cache_path(enroll_token)
    tmp = out + ".part"
    ok, msg = usb_flash.build_node_iso(node, enroll_token, controller_url, tmp)
    if not ok:
        try:
            if os.path.isfile(tmp):
                os.unlink(tmp)
        except Exception:
            pass
        return False, msg
    os.replace(tmp, out)
    return True, "ok"


def _run(node, enroll_token, controller_url):
    from .database import db_add_alert
    try:
        _set("running", "construction de l'ISO préseedée…")
        ok, msg = _build(node, enroll_token, controller_url)
        if not ok:
            return _set("error", msg)
        url = iso_url(controller_url, enroll_token)
        _set("done", "ISO prête — copier l'URL dans iLO → Virtual Media.", url=url)
        db_add_alert("alert.prep.iso_ilo_prete", "info", node_id=node.get("id"), kind="node",
                     params={"n": node.get("name")})
    except Exception as e:
        _set("error", str(e))
