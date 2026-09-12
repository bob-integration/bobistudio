# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France

"""Serveur HTTP dédié au boot réseau PXE — HTTP/1.1 KEEP-ALIVE, sert le netboot à la RACINE.

Pourquoi un serveur séparé du Flask de l'orchestrateur : le firmware UEFI HTTP Boot (HPe Gen10/iLO5)
télécharge le NBP (bootnetx64.efi) via un HEAD puis un GET en RÉUTILISANT la même connexion TCP. Le
serveur de dev Werkzeug force « Connection: close » de façon inconditionnelle (werkzeug/serving.py)
→ le GET ne part jamais (« Failed to download the URI file »). Ici protocol_version=HTTP/1.1 → keep-alive.

Deux pièges tranchés au 1er test hardware (dl360-1) :
 1. **Racine, pas /pxe** : bootnetx64.efi est SHIM ; il charge ensuite grubx64.efi puis grub charge ses
    modules au prefix COMPILÉ `/debian-installer/amd64/grub` (sans /pxe). Namespacer sous /pxe casse la
    chaîne. On sert donc l'arbre netboot tel quel, à la racine.
 2. **404 avec corps** : shim réclame `revocations.efi` (SBAT) ; absent → 404. Un 404 SANS corps
    (Content-Length: 0) bloque le client HTTP de shim sur la connexion keep-alive → il n'enchaîne jamais
    sur grubx64.efi. SimpleHTTPRequestHandler renvoie un 404 AVEC corps → shim le lit et continue. On
    réutilise donc sa machinerie pour le statique ET les 404 ; on n'intercepte que les 3 endpoints
    dynamiques (grub.cfg / preseed.cfg / payload/*) générés par app.pxe.

Seul le POST d'enrôlement final vise l'API :5000 (curl/wget gèrent « close »), d'où enroll.conf qui
garde l'URL contrôleur. Lancé en thread daemon depuis main.py ; un échec de bind est loggé, jamais fatal.
"""
import os
import logging
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import pxe
from .database import db_get_node_by_enroll_token

log = logging.getLogger(__name__)


class _Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # KEEP-ALIVE — toute la raison d'être de ce serveur

    def __init__(self, *a, **k):
        super().__init__(*a, directory=pxe.PXE_ROOT, **k)

    def log_message(self, fmt, *a):
        log.info("pxe-http %s - %s", self.client_address[0], fmt % a)

    def guess_type(self, path):
        # octet-stream EXIGÉ par le firmware UEFI HTTP Boot (rejette application/efi & co.).
        return "application/octet-stream"

    def _text(self, body):
        data = (body or "").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _dynamic(self):
        """Sert grub.cfg/preseed/payload générés. Retourne True si pris en charge (sinon → statique)."""
        token, api_ctrl = pxe.armed()
        filename = urlparse(self.path).path.lstrip("/")
        base = "http://" + (self.headers.get("Host") or "")   # CE serveur (keep-alive)
        name = filename.rsplit("/", 1)[-1]

        if name == "grub.cfg":
            # NB : un grub.cfg stock existe dans l'arbre → on DOIT l'intercepter pour servir le nôtre.
            node = db_get_node_by_enroll_token(token) if token else None
            self._text(pxe.grub_cfg(base, prefix="", node=node) if api_ctrl else "")
            return True
        if filename in ("preseed.cfg", "preseed-manual.cfg"):
            node = db_get_node_by_enroll_token(token) if token else None
            manual = filename == "preseed-manual.cfg"
            self._text(pxe.preseed(node, base, prefix="", manual=manual) if node else "")
            return True
        if filename.startswith("payload/"):
            pname = filename[len("payload/"):]
            node = db_get_node_by_enroll_token(token) if token else None
            if not node:
                self._text("")
                return True
            if pname == "enroll.conf":
                # CONTROLLER_URL = API :5000 (POST /api/nodes/enroll), pas ce serveur PXE.
                self._text(pxe.enroll_conf(node, api_ctrl, token))
                return True
            p = pxe.payload_path(pname)
            if p:
                with open(p, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                return True
            # payload inconnu → laisse SimpleHTTPRequestHandler renvoyer un 404 (avec corps).
        return False

    def do_GET(self):
        if not self._dynamic():
            super().do_GET()

    def do_HEAD(self):
        if not self._dynamic():
            super().do_HEAD()


def start(port=None):
    """Démarre le serveur PXE keep-alive en thread daemon. Ne lève jamais (log l'erreur)."""
    port = port or pxe.PXE_HTTP_PORT
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except Exception as e:
        log.error("Serveur PXE keep-alive NON démarré (port %d) : %s", port, e)
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="pxe-http", daemon=True).start()
    log.info("Serveur PXE keep-alive sur http://0.0.0.0:%d/ (racine)", port)
    return srv
