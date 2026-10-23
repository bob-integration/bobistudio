#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
#
# bobi-node-agent — daemon par-nœud (Phase B, squelette fonctionnel).
# Contrat figé dans NODE_AGENT.md. Stdlib UNIQUEMENT (tourne sur une box nue : python3 + docker).
# Pilote le cycle de vie des conteneurs + expose /health & /capabilities + services hôte.
# NE rend/exécute AUCUN script de plugin (ça reste l'agent PAR-CONTENEUR baké dans les images).

import hashlib
import html
import json
import hmac
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "0.29.1"  # 0.29.1 : formatage — UUID relu après mkfs (settle, relectures, blkid -p) au lieu d'abandonner ; 0.29.0 : disques locaux (/v1/host/disks, plan/execute) ; 0.28.0 : pilotes hôte — job long + journal + action en attente (redémarrage / arrêt complet) génériques (/v1/host/drivers/*, /v1/host/poweroff), 1er kind : nvidia ; 0.27.0 : port de transfert — plage FERMÉE `bytes=a-b` (206 exact, 416), connexions gardées ouvertes (keep-alive, inactivité bornée) ; 0.26.0 : envoi PAR MORCEAUX reprenable (décalage, état, finalisation, abandon, espace libre vérifié) ; 0.25.0 : dépôt d'un fichier par le contrôleur (import d'un projet avec ses médias) ; 0.24.0 : transfert de fichiers nœud → nœud (port 9101, lecture seule, autorisation signée) ; 0.23.0 : seul un CONTRÔLEUR pilote (rôle du cert client vérifié, plus seulement la chaîne) ; 0.22.0 : mounts[].options (ro, rslave…) — partages réseau vus par les conteneurs média
# 0.21.0 : chien de garde — sonde par docker exec (macvlan isole l hote)
#                     ciblé sur le bon domaine/uds, et remontée de gm.ClockClass / utcOffsetValid
# 0.18.0 : /v1/host/clock — les horloges du nœud en 2 appels système, estampillé
#                     à la réception ET à l'émission (modèle NTP à 4 estampilles côté contrôleur)
# 0.17.0 : spec["log"] = {driver, opts} → journal conteneur DURABLE (journald)
# 0.16.0 : cpu_per_core en delta (vraie charge instantanée) + /v1/host/core-snapshot
# 0.15.0 : port lié à vfio-pci (DPDK) exposé state:"vfio" au lieu de disparaître
# 0.14.0 : canary bande passante mémoire embarqué (`membw` dans /v1/health)
# 0.13.1 : bascule TLS par re-exec (os.execv) — indépendant de systemd Restart
# 0.13.0 : mTLS plan de contrôle (HTTPS conditionnel + repli HTTP, CSR/enrôlement,
#                     migration à chaud /v1/tls/*, injection spec["tls"] par-conteneur)
# 0.12.0 : santé matérielle hwmon (températures/ventilateurs/conso) dans health
# 0.11.0 : speed_mbps par interface dans le débit réseau (barres rx/tx vs lien)
# 0.10.0 : spec conteneur RDMA (entrypoint/command/devices/cap_add/ulimits) — chantier RDMA
# 0.9.0 : support GPU (--gpus dans la spec conteneur + gpus[] dans capabilities)

# ─── Configuration ────────────────────────────────────────────────────────────
# Fichier JSON (défaut /etc/bobi-node-agent/config.json), surchargé par l'environnement.
# Champs : token, port, capabilities[], mxl_mount, macvlan_network, mtl_iface, lcores,
#          media_mount, images[] (tags d'intérêt pour le rapport de présence),
#          membw_interval_s / membw_sample_mb (canary bande passante mémoire, cf. _membw_loop).
CONFIG_PATH = os.environ.get("BOBI_NODE_AGENT_CONFIG", "/etc/bobi-node-agent/config.json")


def _load_config():
    cfg = {
        "token": "", "port": 9100, "info_port": 80, "controller_url": "", "capabilities": [],
        "mxl_mount": "/dev/shm", "macvlan_network": "", "mtl_iface": "",
        "lcores": "", "media_mount": "/srv/mxl-media", "images": [],
        # mTLS du plan de contrôle : dossier du matériel TLS de l'agent-nœud
        # (node.key/node.crt/ca.crt). Vide/inexistant → l'agent sert en HTTP clair (repli).
        "tls_dir": "/etc/bobi-node-agent/tls",
    }
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f) or {})
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[config] lecture {CONFIG_PATH} échouée : {e}")
    # Surcharges d'environnement (pratique pour le dev / les tests).
    if os.environ.get("BOBI_NODE_AGENT_TOKEN"):
        cfg["token"] = os.environ["BOBI_NODE_AGENT_TOKEN"]
    if os.environ.get("BOBI_NODE_AGENT_PORT"):
        cfg["port"] = int(os.environ["BOBI_NODE_AGENT_PORT"])
    if os.environ.get("BOBI_NODE_AGENT_CAPS"):
        cfg["capabilities"] = [c.strip() for c in os.environ["BOBI_NODE_AGENT_CAPS"].split(",") if c.strip()]
    if os.environ.get("BOBI_NODE_AGENT_TLS_DIR"):
        cfg["tls_dir"] = os.environ["BOBI_NODE_AGENT_TLS_DIR"]
    return cfg


CONFIG = _load_config()
START_TS = time.time()

# Dernier contrôleur ayant fait une requête authentifiée (pour la page d'état :80).
LAST_CONTROLLER = {"ip": None, "ts": None}
# Cache de l'identité publique du contrôleur (nom/entreprise/localisation), TTL 60 s.
_CTL_ID = {"ip": None, "ts": 0.0, "data": None}

# État gardé entre deux échantillons pour les deltas (CPU %, débit réseau).
_PREV_CPU = {}   # {"total": int, "idle": int}
_PREV_CORES = {} # idx -> (total, idle) — delta par cœur pour cpu_per_core
_PREV_NET = {}   # iface -> {"rx": int, "tx": int, "ts": float}


def has_cap(name):
    return name in (CONFIG.get("capabilities") or [])


# ─── mTLS du plan de contrôle ───────────────────────────────────────────────────
# L'agent-nœud sert son API (:9100) en HTTPS + mTLS DÈS QUE son matériel TLS est présent
# (node.key + node.crt signé par la CA + ca.crt). Sinon → HTTP clair (repli/filet de sécurité :
# un push de cert raté ne doit jamais rendre le nœud injoignable). Le token X-MXL-Node-Token
# reste le second facteur dans les DEUX modes. openssl (CLI) est utilisé pour la clé/CSR — on ne
# dépend PAS de la lib python `cryptography` (pas garantie sur un nœud nu). Le MÊME port sert les
# deux modes ; la bascule HTTP→HTTPS se fait par redémarrage du process (systemd Restart=always).
def _tls_paths():
    d = (CONFIG.get("tls_dir") or "").strip() or "/etc/bobi-node-agent/tls"
    return {
        "dir": d,
        "key": os.path.join(d, "node.key"),
        "crt": os.path.join(d, "node.crt"),
        "ca": os.path.join(d, "ca.crt"),
        "csr": os.path.join(d, "node.csr"),
    }


def _readable(p):
    try:
        return os.path.isfile(p) and os.access(p, os.R_OK) and os.path.getsize(p) > 0
    except Exception:
        return False


def _tls_ready():
    """True si la clé, le cert-nœud ET la CA sont présents/lisibles/non-vides."""
    p = _tls_paths()
    return _readable(p["key"]) and _readable(p["crt"]) and _readable(p["ca"])


def _openssl(args, input_bytes=None, timeout=20):
    """Enveloppe openssl CLI. Retourne (rc, stdout, stderr)."""
    return run(["openssl", *args], timeout=timeout, input_bytes=input_bytes)


def _tls_subject_cn():
    """CN du CSR : identité stable du nœud (hostname). Le contrôleur reste maître de l'identité
    logique (il resigne comme il veut) ; le CN n'est qu'indicatif."""
    try:
        return "node-" + (socket.gethostname() or "unknown")
    except Exception:
        return "node-unknown"


def _ensure_key_and_csr():
    """Génère (si absent) la clé EC P-256 de l'agent + un CSR frais. Idempotent pour la clé
    (on NE régénère PAS une clé existante → on ne périme pas un cert déjà signé), mais on
    régénère toujours le CSR à la demande. Retourne (ok, csr_pem|err)."""
    p = _tls_paths()
    try:
        os.makedirs(p["dir"], mode=0o700, exist_ok=True)
        try:
            os.chmod(p["dir"], 0o700)
        except Exception:
            pass
    except Exception as e:
        return False, f"création dossier TLS {p['dir']} impossible : {e}"

    if not _readable(p["key"]):
        rc, _o, e = _openssl(["ecparam", "-name", "prime256v1", "-genkey",
                              "-noout", "-out", p["key"]])
        if rc != 0:
            return False, f"génération clé EC échouée : {e.strip()[:200]}"
        try:
            os.chmod(p["key"], 0o600)
        except Exception:
            pass

    rc, _o, e = _openssl(["req", "-new", "-key", p["key"],
                          "-subj", "/CN=" + _tls_subject_cn(), "-out", p["csr"]])
    if rc != 0:
        return False, f"génération CSR échouée : {e.strip()[:200]}"
    try:
        with open(p["csr"]) as f:
            return True, f.read()
    except Exception as e:
        return False, f"lecture CSR échouée : {e}"


def _install_cert_material(cert_pem, ca_pem):
    """Écrit node.crt + ca.crt (valide chaque cert via openssl x509). Retourne (ok, err)."""
    if not (cert_pem and cert_pem.strip()) or not (ca_pem and ca_pem.strip()):
        return False, "cert et ca_cert requis (PEM non vides)"
    p = _tls_paths()
    try:
        os.makedirs(p["dir"], mode=0o700, exist_ok=True)
    except Exception as e:
        return False, f"création dossier TLS {p['dir']} impossible : {e}"
    # Écriture atomique + validation avant de remplacer l'existant.
    for label, dest, pem in (("node.crt", p["crt"], cert_pem), ("ca.crt", p["ca"], ca_pem)):
        tmp = dest + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(pem if pem.endswith("\n") else pem + "\n")
            os.chmod(tmp, 0o644)
        except Exception as e:
            return False, f"écriture {label} impossible : {e}"
        rc, _o, e = _openssl(["x509", "-noout", "-in", tmp])
        if rc != 0:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return False, f"{label} invalide (openssl x509) : {e.strip()[:200]}"
        try:
            os.replace(tmp, dest)
        except Exception as e:
            return False, f"pose {label} impossible : {e}"
    return True, None


def _make_server_ssl_context():
    """Construit le SSLContext serveur mTLS (peut lever → géré par l'appelant, repli HTTP)."""
    p = _tls_paths()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=p["crt"], keyfile=p["key"])
    ctx.load_verify_locations(cafile=p["ca"])
    ctx.verify_mode = ssl.CERT_REQUIRED           # mTLS : le client (contrôleur) DOIT présenter un cert signé par la CA
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except Exception:
        pass
    return ctx


# ─── QUI a le droit de piloter ce nœud ─────────────────────────────────────────────────────────
# `CERT_REQUIRED` ne prouve qu'une chose : le pair a un certificat signé par NOTRE CA. Or cette CA
# signe aussi les NŒUDS (`bobi://node/<id>`) et les CONTENEURS (`bobi://container/<vmid>`) — qui
# exécutent du code de plugin tiers, et dont la clé est posée dans le conteneur. Jusqu'en 0.22.0,
# un conteneur compromis passait donc la poignée de main de n'importe quel agent-nœud de la flotte ;
# seul le jeton applicatif l'arrêtait encore. Le rôle est désormais vérifié, pas seulement la chaîne.
#
#   · URI `bobi://controller/…` → contrôleur. Seul le code du contrôleur émet ce marqueur.
#   · URI de nœud ou de conteneur → REFUS, quel que soit le CN.
#   · CN `bobi-controller` SANS aucune URI de rôle → contrôleur d'AVANT le marqueur (2026-10-10).
#     Sûr : tout certificat de nœud ou de conteneur porte son URI de rôle depuis l'origine du mTLS,
#     et le contrôleur refuse de signer ce CN à la demande d'un CSR (app/ca.py:sign_csr). Le
#     contrôleur pose son marqueur au démarrage ; ce repli ne sert qu'à la transition.
# Les deux dernières règles se recouvrent EXPRÈS : chacune suffit seule à refuser un conteneur qui
# porterait le CN du contrôleur (mesuré par mutation : il faut retirer les deux pour qu'il passe).
# ÉCHAPPATOIRE : `"verify_controller_identity": false` dans la config → chaîne seule (historique).
CONTROLLER_URI_PREFIX = "bobi://controller/"
LEGACY_CONTROLLER_CN = "bobi-controller"
REFUSED_URI_PREFIXES = ("bobi://node/", "bobi://container/")
_REFUS_JOURNAL = {}          # (ip, motif) → dernier print : un pair insistant ne noie pas journald


def _peer_identities(cert):
    """(CN, URI) d'un dict `getpeercert()`."""
    cns, uris = [], []
    for rdn in (cert or {}).get("subject", ()):
        for key, val in rdn:
            if key == "commonName":
                cns.append(val)
    for typ, val in (cert or {}).get("subjectAltName", ()):
        if typ == "URI":
            uris.append(val)
    return cns, uris


def _peer_refusal(conn):
    """None si le pair TLS est un contrôleur (ou si l'agent sert en clair), sinon le motif du refus."""
    if CONFIG.get("verify_controller_identity") is False:
        return None
    getpeercert = getattr(conn, "getpeercert", None)
    if getpeercert is None:
        return None              # HTTP clair (repli sans matériel TLS) : le jeton seul, comme avant
    cert = getpeercert()
    if not cert:
        return "connexion TLS sans certificat client"
    cns, uris = _peer_identities(cert)
    if any(str(u).startswith(CONTROLLER_URI_PREFIX) for u in uris):
        return None
    if any(str(u).startswith(p) for u in uris for p in REFUSED_URI_PREFIXES):
        return "certificat de nœud ou de conteneur (URI=%s) : pas un contrôleur" % ",".join(uris)
    if LEGACY_CONTROLLER_CN in cns and not any(str(u).startswith("bobi://") for u in uris):
        return None
    return "identité inattendue (CN=%s, URI=%s)" % (",".join(cns) or "-", ",".join(uris) or "-")


def _schedule_restart(delay=1.0):
    """Programme un redémarrage du process après flush de la réponse HTTP, pour repartir en HTTPS
    via _tls_ready(). RE-EXEC (os.execv) plutôt qu'un simple exit : l'agent se relance LUI-MÊME,
    indépendamment du réglage systemd du nœud (n'exige PAS Restart=always). Le socket d'écoute est
    CLOEXEC → il se ferme au exec et le nouveau process rebinde. Filet : si l'exec échoue, os._exit(0)
    (repli sur le superviseur systemd)."""
    def _go():
        time.sleep(delay)
        try:
            print("[bobi-node-agent] redémarrage (bascule TLS) — re-exec de l'agent.", flush=True)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print("[bobi-node-agent] re-exec échoué (%s) — exit, systemd doit relancer." % e, flush=True)
            os._exit(0)
    threading.Thread(target=_go, daemon=True).start()


# ─── Exécution de commandes hôte (argv, jamais de shell) ────────────────────────
def run(argv, timeout=60, input_bytes=None):
    """Exécute argv (liste). Retourne (rc, stdout, stderr). Jamais shell=True (anti-injection)."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, input=input_bytes)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    except FileNotFoundError:
        return 127, "", f"introuvable : {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout ({timeout}s) : {' '.join(argv[:3])}…"
    except Exception as e:
        return 1, "", str(e)


def docker(*args, timeout=60):
    return run(["docker", *args], timeout=timeout)


# ─── Détection hôte (santé / capacités) ─────────────────────────────────────────
def _docker_info():
    rc, out, _ = docker("version", "--format", "{{.Server.Version}}", timeout=8)
    return {"ok": rc == 0, "version": out.strip() if rc == 0 else None}


def _hugepages():
    total = free = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("HugePages_Total:"):
                    total = int(line.split()[1])
                elif line.startswith("HugePages_Free:"):
                    free = int(line.split()[1])
    except Exception:
        return None
    return {"total": total, "free": free}


def _nic(iface):
    if not iface:
        return None
    out = {"iface": iface, "link": None, "model": None, "queues": None}
    # Port lié à vfio-pci (chemin DPDK) : plus de netdev dans /sys/class/net → ethtool/operstate
    # muets. On le signale explicitement (state:"vfio") plutôt que de renvoyer des None ambigus ;
    # l'orchestrateur croise avec node_interfaces.pmd. Champ absent = netdev présent (inchangé).
    if not os.path.exists(f"/sys/class/net/{iface}"):
        out["state"] = "vfio"
        return out
    rc, o, _ = run(["ethtool", "-i", iface], timeout=5)
    if rc == 0:
        m = re.search(r"^driver:\s*(\S+)", o, re.M)
        out["model"] = m.group(1) if m else None
    rc, o, _ = run(["cat", f"/sys/class/net/{iface}/operstate"], timeout=3)
    if rc == 0:
        out["link"] = o.strip() == "up"
    rc, o, _ = run(["ethtool", "-l", iface], timeout=5)
    if rc == 0:
        cur = re.search(r"Current hardware settings:.*?Combined:\s*(\d+)", o, re.S)
        mx = re.search(r"Pre-set maximums:.*?Combined:\s*(\d+)", o, re.S)
        if cur or mx:
            out["queues"] = {"max": int(mx.group(1)) if mx else None,
                             "current": int(cur.group(1)) if cur else None}
    return out


def _all_nics():
    """Inventaire des interfaces réseau (hors lo + virtuelles docker/veth) pour que l'orchestrateur
    propose le choix du PARENT macvlan APRÈS l'enrôlement (réseau containers assigné depuis l'UI, pas
    figé à l'install). name/mac/up/driver/speed/addrs + flag physical (False = sous-if VLAN, bridge…)."""
    nics = []
    rc, o, _ = run(["ls", "/sys/class/net"], timeout=5)
    if rc != 0:
        return nics
    for name in sorted(o.split()):
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr")):
            continue
        rc2, _, _ = run(["test", "-e", f"/sys/class/net/{name}/device"], timeout=3)
        info = {"name": name, "physical": rc2 == 0, "mac": None, "up": None,
                "driver": None, "speed_mbps": None, "addrs": []}
        rc3, mac, _ = run(["cat", f"/sys/class/net/{name}/address"], timeout=3)
        if rc3 == 0:
            info["mac"] = mac.strip()
        rc4, st, _ = run(["cat", f"/sys/class/net/{name}/operstate"], timeout=3)
        if rc4 == 0:
            info["up"] = st.strip() == "up"
        rc5, drv, _ = run(["ethtool", "-i", name], timeout=5)
        if rc5 == 0:
            m = re.search(r"^driver:\s*(\S+)", drv, re.M)
            info["driver"] = m.group(1) if m else None
        rc6, sp, _ = run(["cat", f"/sys/class/net/{name}/speed"], timeout=3)
        if rc6 == 0 and sp.strip().lstrip("-").isdigit():
            info["speed_mbps"] = int(sp.strip())
        rc7, a, _ = run(["ip", "-o", "-4", "addr", "show", "dev", name], timeout=5)
        if rc7 == 0:
            for ln in a.splitlines():
                parts = ln.split()
                if "inet" in parts:
                    info["addrs"].append(parts[parts.index("inet") + 1])
        nics.append(info)
    # Interface DÉCLARÉE (mtl_iface) mais absente de /sys/class/net : port passé en vfio-pci
    # (chemin DPDK). Au lieu de la faire disparaître de l'inventaire (l'orchestrateur croirait
    # la NIC débranchée), on l'expose avec state:"vfio" — croisée côté contrôleur avec
    # node_interfaces.pmd (les compteurs viennent alors du moteur, cf. docs/chantiers/DPDK_NARROW.md).
    declared = (CONFIG.get("mtl_iface") or "").strip()
    if declared and not any(n.get("name") == declared for n in nics):
        nics.append({"name": declared, "physical": True, "mac": None, "up": None,
                     "driver": None, "speed_mbps": None, "addrs": [], "state": "vfio"})
    return nics


def _ptp_unites():
    """Unités ptp4l ACTIVES, et pour chacune (conf, domaine, uds) lus dans sa ligne de commande.

    Les unités sont nommées PAR RÉSEAU (`mxl-ptp4l-net<id>`) depuis le multi-NIC. Interroger le
    nom nu `mxl-ptp4l` demande à systemd l'état d'une unité qui n'existe pas : réponse « inactive »
    quel que soit l'état réel. On énumère donc par glob, puis on lit le `-f <conf>` de l'ExecStart
    pour en tirer `domainNumber` et `uds_address` — sans quoi `pmc` interroge le domaine 0 sur le
    socket par défaut et ne reçoit **aucune réponse**, ce qui laisse offset et grandmaster vides
    alors que ptp4l est verrouillé (constaté en prod, Horace 2026-07-28)."""
    trouve = []
    rc, o, _ = run(["systemctl", "list-units", "mxl-ptp4l*", "--state=active",
                    "--no-legend", "--plain", "--type=service"], timeout=6)
    unites = [l.split()[0] for l in (o or "").splitlines() if l.strip()]
    if not unites and run(["systemctl", "is-active", "ptp4l"], timeout=4)[1].strip() == "active":
        unites = ["ptp4l.service"]          # déploiement ptp4l « nu », hors orchestrateur
    for u in unites:
        conf, dom, uds = None, 0, None
        rc, o, _ = run(["systemctl", "show", u, "-p", "ExecStart"], timeout=5)
        m = re.search(r"-f\s+(\S+\.conf)", o or "")
        if m:
            conf = m.group(1)
            try:
                with open(conf) as f:
                    for ligne in f:
                        c = ligne.split("#", 1)[0].strip()
                        if c.startswith("domainNumber"):
                            dom = int(c.split()[1])
                        elif c.startswith("uds_address"):
                            uds = c.split()[1]
            except Exception:
                pass
        trouve.append({"unit": u, "conf": conf, "domain": dom, "uds": uds})
    return trouve


def _pmc(query, dom, uds, timeout=5):
    argv = ["pmc", "-u", "-b", "0", "-d", str(int(dom))]
    if uds:
        argv += ["-s", uds]
    rc, o, _ = run(argv + [query], timeout=timeout)
    return o if rc == 0 else ""


def _ptp():
    """État PTP (io2110). Dégrade en {running:false} si ptp4l/pmc absents.

    ⚠ `locked` ne dit QUE la qualité du verrou. Un esclave se verrouille aussi bien sur un
    grandmaster en roue libre que sur du GPS — d'où `gm_clock_class` / `utc_offset_valid`, qui
    seuls disent si la référence vaut quelque chose (cf. `app/ptp.py:gm_reference_saine`)."""
    st = {"running": False, "locked": False, "gm_id": None, "offset_ns": None,
          "iface": CONFIG.get("mtl_iface") or None, "unit": None, "domain": None,
          "gm_clock_class": None, "utc_offset_valid": None, "phc2sys_running": False}
    unites = _ptp_unites()
    st["running"] = bool(unites)
    rc, o, _ = run(["systemctl", "list-units", "mxl-phc2sys*", "--state=active",
                    "--no-legend", "--plain", "--type=service"], timeout=6)
    st["phc2sys_running"] = bool((o or "").strip())
    if not unites or not shutil.which("pmc"):
        return st
    u = unites[0]
    st["unit"], st["domain"] = u["unit"], u["domain"]
    o = _pmc("GET TIME_STATUS_NP", u["domain"], u["uds"])
    m = re.search(r"master_offset\s+(-?\d+)", o)
    if m:
        st["offset_ns"] = int(m.group(1))
        st["locked"] = abs(st["offset_ns"]) < 1000  # < 1 µs ≈ locké
    g = re.search(r"gmIdentity\s+(\S+)", o)
    if g:
        st["gm_id"] = g.group(1)
    o = _pmc("GET PARENT_DATA_SET", u["domain"], u["uds"])
    m = re.search(r"gm\.ClockClass\s+(\d+)", o)
    if m:
        st["gm_clock_class"] = int(m.group(1))
    o = _pmc("GET TIME_PROPERTIES_DATA_SET", u["domain"], u["uds"])
    m = re.search(r"currentUtcOffsetValid\s+(\d+)", o)
    if m:
        st["utc_offset_valid"] = bool(int(m.group(1)))
    return st


# ─── Horloge du nœud (mesure de précision) ────────────────────────────────────
# CLOCK_TAI est la grille de la flotte MXL ; CLOCK_REALTIME sert à situer le nœud par rapport à
# l'UTC du contrôleur. Les deux sont lues d'affilée, et LEUR ÉCART EN SECONDES ENTIÈRES EST le
# `tai_offset` du noyau — inutile d'appeler adjtimex pour le redemander.
#
# Pourquoi un endpoint natif plutôt que la sonde shell qu'utilisait le contrôleur : cette lecture
# doit coûter deux appels système et RIEN d'autre. Faire remonter l'heure par `host/exec`, c'est y
# ajouter un `sh -c` puis un démarrage d'interpréteur Python — des dizaines de millisecondes qui
# tombent toutes sur l'ALLER, donc un biais qu'aucune moyenne ne rattrape.
CLOCK_TAI = getattr(time, "CLOCK_TAI", 11)


def _horloges_ns():
    return time.clock_gettime_ns(time.CLOCK_REALTIME), time.clock_gettime_ns(CLOCK_TAI)


def _docker_networks():
    rc, o, _ = docker("network", "ls", "--format", "{{.Name}} {{.Driver}}", timeout=8)
    nets = []
    if rc == 0:
        for line in o.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("macvlan", "ipvlan"):
                sub = None
                rc2, o2, _ = docker("network", "inspect", "-f",
                                    "{{range .IPAM.Config}}{{.Subnet}}{{end}}", parts[0], timeout=8)
                if rc2 == 0:
                    sub = o2.strip() or None
                nets.append({"name": parts[0], "driver": parts[1], "subnet": sub})
    return nets


def _images_present():
    out = []
    for tag in (CONFIG.get("images") or []):
        rc, _, _ = docker("image", "inspect", tag, timeout=8)
        out.append({"tag": tag, "present": rc == 0})
    return out


def _cpu_real_pct():
    """CPU % réel = 1 - delta(idle)/delta(total) sur /proc/stat entre deux appels.
    None au tout premier appel (pas de référence). État gardé dans _PREV_CPU."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()  # cpu  user nice system idle iowait irq softirq steal …
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        total = sum(vals)
    except Exception:
        return None
    prev = _PREV_CPU.get("total")
    pct = None
    if prev is not None:
        dt = total - prev
        di = idle - _PREV_CPU.get("idle", 0)
        if dt > 0:
            pct = round((1.0 - di / dt) * 100, 1)
    _PREV_CPU["total"], _PREV_CPU["idle"] = total, idle
    return pct


def _cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def _resources():
    res = {"cpu_pct": None, "cpu_pct_real": None, "loadavg": None,
           "cpu_per_core": None, "cpu_count": os.cpu_count(), "cpu_model": _cpu_model(),
           "mem_used_mb": None, "mem_total_mb": None,
           "swap_used_mb": None, "swap_total_mb": None}
    try:
        with open("/proc/meminfo") as f:
            mi = {}
            for line in f:
                k, _, v = line.partition(":")
                mi[k] = int(v.split()[0]) if v.split() else 0
        total = mi.get("MemTotal", 0) // 1024
        avail = mi.get("MemAvailable", 0) // 1024
        res["mem_total_mb"] = total
        res["mem_used_mb"] = total - avail
        sw_total = mi.get("SwapTotal", 0) // 1024
        sw_free = mi.get("SwapFree", 0) // 1024
        res["swap_total_mb"] = sw_total
        res["swap_used_mb"] = sw_total - sw_free
    except Exception:
        pass
    try:
        la = os.getloadavg()
        res["loadavg"] = [round(x, 2) for x in la]
        res["cpu_pct"] = round(la[0] / (os.cpu_count() or 1) * 100, 1)  # rétro-compat (loadavg)
    except Exception:
        pass
    res["cpu_pct_real"] = _cpu_real_pct()
    res["cpu_per_core"] = _cpu_per_core()
    return res


def _read_per_core_stat():
    """Lignes cpuN de /proc/stat → liste de (total, idle+iowait) indexée par cœur."""
    out = []
    with open("/proc/stat") as f:
        for line in f:
            if re.match(r"^cpu\d+ ", line):
                vals = [int(x) for x in line.split()[1:]]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                out.append((sum(vals), idle))
    return out


def _cpu_per_core():
    """Charge par cœur = 1 - delta(idle)/delta(total) sur les lignes cpuN de /proc/stat entre
    deux appels (même modèle que _cpu_real_pct, état dans _PREV_CORES). Au premier appel
    (pas de référence, ex. redémarrage de l'agent) : repli sur la valeur cumulée depuis le
    boot — moins parlant mais évite une rangée vide côté UI. Best-effort, None si indisponible."""
    try:
        snap = _read_per_core_stat()
    except Exception:
        return None
    cores = []
    for i, (total, idle) in enumerate(snap):
        prev = _PREV_CORES.get(i)
        if prev and total > prev[0]:
            dt, di = total - prev[0], idle - prev[1]
            cores.append(round((1.0 - di / dt) * 100, 1))
        else:
            cores.append(round((1.0 - idle / total) * 100, 1) if total else 0.0)
        _PREV_CORES[i] = (total, idle)
    return cores or None


def _docker_pid_owners():
    """PID → nom de conteneur Docker, pour TOUS les conteneurs qui tournent. Cascade :
    cgroup v2 driver systemd (docker-<id>.scope), puis driver cgroupfs, puis repli
    `docker top` (plus lent, et ne liste que les process — les threads sont énumérés
    ensuite via /proc/<pid>/task/). Best-effort par conteneur."""
    owners = {}
    rc, out, _ = docker("ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}", timeout=10)
    if rc != 0:
        return owners
    for line in out.splitlines():
        cid, _, name = line.partition("\t")
        cid, name = cid.strip(), name.strip()
        if not cid or not name:
            continue
        pids = []
        for procs in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cgroup.procs",
                      f"/sys/fs/cgroup/docker/{cid}/cgroup.procs"):
            try:
                with open(procs) as f:
                    pids = [int(x) for x in f.read().split()]
                break
            except Exception:
                continue
        if not pids:
            rc2, o2, _ = docker("top", cid, "-eo", "pid", timeout=10)
            if rc2 == 0:
                pids = [int(x) for x in o2.split()[1:] if x.isdigit()]
        for pid in pids:
            owners[pid] = name
    return owners


def _read_task_stat(pid, tid):
    """/proc/<pid>/task/<tid>/stat → (comm, utime+stime, processor). Le comm peut contenir
    espaces/parenthèses → parse par rsplit(')', 1)."""
    with open(f"/proc/{pid}/task/{tid}/stat") as f:
        line = f.read()
    comm = line[line.index("(") + 1:line.rindex(")")]
    f_ = line.rsplit(")", 1)[1].split()
    # f_[0] = champ 3 (state) → utime=champ 14 → f_[11] ; stime → f_[12] ; processor=champ 39 → f_[36]
    return comm, int(f_[11]) + int(f_[12]), int(f_[36])


def _core_snapshot(duration=0.5, top_n=3, min_pct=1.0):
    """Instantané des cœurs : % réel par cœur (delta /proc/stat sur ~duration s) + threads
    les plus consommateurs par cœur (PSR), rattachés à leur conteneur Docker. Sert le
    diagnostic de pinning depuis la page Monitoring. Best-effort intégral : un thread ou
    un conteneur disparu entre t0 et t1 est simplement ignoré."""
    owners = _docker_pid_owners()
    t0_wall = time.monotonic()
    try:
        cores_t0 = _read_per_core_stat()
    except Exception:
        return {"ok": False, "error": "/proc/stat illisible"}
    t0_ticks = {}  # (pid, tid) -> ticks
    for pid in owners:
        try:
            for tid in os.listdir(f"/proc/{pid}/task"):
                try:
                    _, ticks, _ = _read_task_stat(pid, int(tid))
                    t0_ticks[(pid, int(tid))] = ticks
                except Exception:
                    continue
        except Exception:
            continue
    time.sleep(duration)
    elapsed = time.monotonic() - t0_wall
    try:
        cores_t1 = _read_per_core_stat()
    except Exception:
        return {"ok": False, "error": "/proc/stat illisible"}
    clk = os.sysconf("SC_CLK_TCK") or 100
    per_core = {}  # idx -> [{container, tid, comm, pct}]
    for (pid, tid), ticks0 in t0_ticks.items():
        try:
            comm, ticks1, psr = _read_task_stat(pid, tid)
        except Exception:
            continue
        pct = (ticks1 - ticks0) / (clk * elapsed) * 100
        if pct < min_pct:
            continue
        per_core.setdefault(psr, []).append(
            {"container": owners[pid], "tid": tid, "comm": comm, "pct": round(pct, 1)})
    cores = []
    for i in range(min(len(cores_t0), len(cores_t1))):
        dt = cores_t1[i][0] - cores_t0[i][0]
        di = cores_t1[i][1] - cores_t0[i][1]
        pct = round((1.0 - di / dt) * 100, 1) if dt > 0 else 0.0
        top = sorted(per_core.get(i, []), key=lambda x: -x["pct"])[:top_n]
        cores.append({"cpu": i, "pct": pct, "top": top})
    return {"ok": True, "agent_version": VERSION, "duration_ms": int(elapsed * 1000),
            "cores": cores}


def _disks():
    """Remplissage des points de montage clés : /dev/shm (pipeline 2110), racine /, média.
    {mount: {used, total, pct}} en octets. Best-effort par point (un échec n'efface pas les autres)."""
    out = {}
    mounts = {"shm": "/dev/shm", "root": "/"}
    if has_cap("media"):
        mounts["media"] = CONFIG.get("media_mount") or "/srv/mxl-media"
    for key, path in mounts.items():
        try:
            u = shutil.disk_usage(path)
            out[key] = {"path": path, "used": u.used, "total": u.total,
                        "pct": round(u.used / u.total * 100, 1) if u.total else None}
        except Exception:
            continue
    return out


def _net_throughput():
    """Débit rx/tx par interface (bps) = delta des compteurs /sys/class/net/*/statistics sur le temps
    écoulé. None au 1er passage par interface. État dans _PREV_NET. Mêmes interfaces que _all_nics."""
    now = time.time()
    out = {}
    rc, o, _ = run(["ls", "/sys/class/net"], timeout=5)
    if rc != 0:
        return out
    for name in sorted(o.split()):
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr")):
            continue
        try:
            with open(f"/sys/class/net/{name}/statistics/rx_bytes") as f:
                rx = int(f.read().strip())
            with open(f"/sys/class/net/{name}/statistics/tx_bytes") as f:
                tx = int(f.read().strip())
        except Exception:
            continue
        prev = _PREV_NET.get(name)
        if prev:
            dt = now - prev["ts"]
            if dt > 0:
                # speed_mbps : vitesse négociée du lien (None/-1 si down) → barres rx/tx vs lien (UI).
                spd = None
                try:
                    with open(f"/sys/class/net/{name}/speed") as f:
                        v = int(f.read().strip())
                        spd = v if v > 0 else None
                except Exception:
                    spd = None
                out[name] = {"rx_bps": round((rx - prev["rx"]) * 8 / dt),
                             "tx_bps": round((tx - prev["tx"]) * 8 / dt),
                             "speed_mbps": spd}
        _PREV_NET[name] = {"rx": rx, "tx": tx, "ts": now}
    return out


def _host_uptime_s():
    """Uptime de l'HÔTE (≠ uptime de l'agent). None si /proc/uptime illisible."""
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return None


# ─── Canary bande passante mémoire (0.14.0) ────────────────────────────────────
# memcpy mono-thread d'un gros buffer, échantillonné en tâche de fond : le débit atteint CHUTE
# quand le bus RAM est saturé par la flotte → indicateur de *headroom* mémoire. L'agent ne remonte
# que le débit brut dans /v1/health ; la référence par nœud, le ratio et les alertes restent côté
# contrôleur (`app/membw.py`, qui garde aussi un canary par exec en repli pour les agents < 0.14.0).
# ctypes.memmove relâche le GIL → l'API HTTP reste réactive pendant la copie (~0,3 s / minute).
# Config : membw_interval_s (défaut 60 ; ≤ 0 désactive), membw_sample_mb (défaut 128, min 16).
_MEMBW = {}   # {"gbps": float, "ts": float, "sample_mb": int} — dernier échantillon


def _membw_measure(mb):
    import ctypes
    n = mb * 1024 * 1024
    a = ctypes.create_string_buffer(n)
    b = ctypes.create_string_buffer(n)
    mm = ctypes.memmove
    mm(b, a, n)                                   # échauffement (fautes de page hors mesure)
    r = 8
    t = time.perf_counter()
    for _ in range(r):
        mm(b, a, n)
    return round(r * n / (time.perf_counter() - t) / 1e9, 3)


def _membw_loop():
    while True:
        try:
            interval = int(CONFIG.get("membw_interval_s") or 60)
        except (TypeError, ValueError):
            interval = 60
        if interval <= 0:
            _MEMBW.clear()
            time.sleep(60)
            continue
        try:
            mb = max(16, int(CONFIG.get("membw_sample_mb") or 128))
            _MEMBW.update({"gbps": _membw_measure(mb), "ts": time.time(), "sample_mb": mb})
        except Exception as ex:
            print(f"[membw] mesure échouée : {ex}")
        time.sleep(interval)


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _read_str(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _sensors():
    """Santé matérielle via /sys/class/hwmon (sans dépendance, lm-sensors NON requis) :
    températures (°C), ventilateurs (RPM), puissance (W). Best-effort, {} si rien d'exploitable.
    Sur certains serveurs (ex. HPE), les ventilateurs/conso passent par l'IPMI/BMC et n'apparaissent
    pas en hwmon → on remonte alors juste les températures coretemp/k10temp dispo."""
    base = "/sys/class/hwmon"
    temps, fans, power = [], [], None
    try:
        hwdirs = sorted(os.listdir(base))
    except Exception:
        return {}
    for d in hwdirs:
        hw = os.path.join(base, d)
        chip = _read_str(os.path.join(hw, "name")) or d
        try:
            entries = os.listdir(hw)
        except Exception:
            continue
        for e in entries:
            if e.startswith("temp") and e.endswith("_input"):
                v = _read_int(os.path.join(hw, e))
                if v is None:
                    continue
                lbl = _read_str(os.path.join(hw, e.replace("_input", "_label"))) or e[:-6]
                temps.append({"chip": chip, "label": lbl, "c": round(v / 1000.0, 1)})
            elif e.startswith("fan") and e.endswith("_input"):
                v = _read_int(os.path.join(hw, e))
                if not v or v <= 0:
                    continue
                lbl = _read_str(os.path.join(hw, e.replace("_input", "_label"))) or e[:-6]
                fans.append({"label": lbl, "rpm": v})
            elif e.startswith("power") and e.endswith("_input"):
                v = _read_int(os.path.join(hw, e))   # µW instantané
                if v:
                    power = round((power or 0) + v / 1e6, 1)
    out = {}
    if temps:
        out["temps"] = temps
    if fans:
        out["fans"] = fans
    if power is not None:
        out["power_w"] = power
    return out


def _versions():
    """Inventaire logiciel hôte : noyau + OS (os-release). Docker reporté à part dans health."""
    import platform
    v = {"kernel": platform.release(), "os": None}
    try:
        with open("/etc/os-release") as f:
            kv = {}
            for line in f:
                k, _, val = line.partition("=")
                kv[k.strip()] = val.strip().strip('"')
        v["os"] = kv.get("PRETTY_NAME") or kv.get("NAME")
    except Exception:
        pass
    return v


def _list_containers():
    rc, o, _ = docker("ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}", timeout=10)
    out = []
    if rc == 0:
        for line in o.splitlines():
            p = line.split("\t")
            if len(p) >= 3:
                out.append({"name": p[0], "status": p[1], "image": p[2]})
    return out


def _gpus():
    """GPU NVIDIA présents (via nvidia-smi). [] si pas de GPU / pilote absent. Bonus : l'orchestrateur
    sait aussi détecter par SSH ; ce champ évite ce round-trip. Cf. NODE_AGENT.md §4.4 (chantier GPU)."""
    rc, out, _ = run(["nvidia-smi", "--query-gpu=index,name,memory.total",
                      "--format=csv,noheader,nounits"], timeout=8)
    if rc != 0:
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0].isdigit():
            try:
                gpus.append({"index": int(parts[0]), "name": parts[1], "mem_mb": int(float(parts[2]))})
            except ValueError:
                continue
    return gpus


def capabilities_payload():
    return {
        "capabilities": CONFIG.get("capabilities") or [],
        "host": os.uname().nodename,
        "controller_url": CONFIG.get("controller_url") or "",
        "mxl_mount": CONFIG.get("mxl_mount"),
        "networks": _docker_networks(),
        "nics": _all_nics(),
        "images": _images_present(),
        "nic": _nic(CONFIG.get("mtl_iface")) if has_cap("io2110") else None,
        "lcores": CONFIG.get("lcores") or None,
        "gpus": _gpus(),
        "agent_version": VERSION,
    }


def health_payload():
    h = {
        "ok": True, "agent_version": VERSION, "uptime_s": int(time.time() - START_TS),
        "host_uptime_s": _host_uptime_s(),
        "docker": _docker_info(),
        "resources": _resources(),
        "disks": _disks(),
        "net": _net_throughput(),
        "versions": _versions(),
        "containers": _list_containers(),
        "sensors": _sensors(),
        "watchdog": _wd_report(),
    }
    if _MEMBW:
        h["membw"] = dict(_MEMBW)
    if has_cap("io2110"):
        h["hugepages"] = _hugepages()
        h["nic"] = _nic(CONFIG.get("mtl_iface"))
        h["ptp"] = _ptp()
    return h


# ─── Cycle de vie des conteneurs ────────────────────────────────────────────────
# Racine des dossiers TLS par-conteneur (montés read-only dans les conteneurs).
CONTAINER_TLS_ROOT = "/run/bobi-tls"


def _materialize_container_tls(name, tls):
    """Écrit le trio PEM d'un conteneur (spec["tls"] = {cert,key,ca}) dans
    /run/bobi-tls/<name>/ (cert.pem, key.pem en 600, ca.pem) et renvoie ce dossier.
    None si pas de matériel (ou incomplet) → le conteneur tourne sans mTLS (inchangé)."""
    if not isinstance(tls, dict):
        return None
    cert, key, ca = tls.get("cert"), tls.get("key"), tls.get("ca")
    if not (cert and key and ca):
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name)) or "container"
    d = os.path.join(CONTAINER_TLS_ROOT, safe)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        for fname, pem, mode in (("cert.pem", cert, 0o644),
                                 ("key.pem", key, 0o600),
                                 ("ca.pem", ca, 0o644)):
            fp = os.path.join(d, fname)
            with open(fp, "w") as f:
                f.write(pem if pem.endswith("\n") else pem + "\n")
            os.chmod(fp, mode)
    except Exception as e:
        print(f"[tls] matérialisation TLS conteneur {name} échouée : {e}")
        return None
    return d


_MOUNT_OPTS = {"ro", "rw", "rslave", "slave", "rshared", "shared", "rprivate", "private"}


def _build_run_argv(spec):
    """Traduit une spec de conteneur (NODE_AGENT.md §4.4) en argv `docker run`.
    Reproduit fidèlement les deux profils actuels (compute macvlan / MTL host+privileged)."""
    name = spec["name"]
    image = spec["image"]
    argv = ["docker", "run", "-d", "--name", name]

    if spec.get("autoremove"):
        argv.append("--rm")                       # MTL : --rm (pas de --restart en parallèle)
    else:
        argv += ["--restart", spec.get("restart_policy") or "unless-stopped"]

    # Journal DURABLE (chantier journald) : `log` = {"driver": "journald"|"json-file",
    # "opts": {...}} → `--log-driver <d> --log-opt k=v …`. Le pilote `journald` fait vivre le
    # journal dans systemd-journald (l'HÔTE) : il survit à la destruction du conteneur et au
    # reboot du nœud, alors que le json-file part avec /var/lib/docker/containers/<id>/.
    # Clé ABSENTE (contrôleur antérieur) → aucun flag, pilote par défaut du daemon (rétro-compat).
    logspec = spec.get("log") or {}
    if isinstance(logspec, dict) and logspec.get("driver"):
        argv += ["--log-driver", str(logspec["driver"])]
        for k, v in (logspec.get("opts") or {}).items():
            argv += ["--log-opt", f"{k}={v}"]

    net = spec.get("network") or "host"
    argv += ["--network", net]
    # B2-2 : IPAM centralisé orchestrateur → IP fixe imposée (`--ip`). Sans ça, Docker IPAM choisit
    # (collisions multi-nœud sur VLAN partagé, IP non déterministe en séparé). Pas de `--ip` en host.
    if spec.get("ip") and net != "host":
        argv += ["--ip", str(spec["ip"])]
    if spec.get("privileged"):
        argv.append("--privileged")

    res = spec.get("resources") or {}
    if res.get("cpuset"):
        argv += ["--cpuset-cpus", str(res["cpuset"])]
    elif res.get("cpus"):
        argv += ["--cpus", str(res["cpus"])]
    if res.get("memory_mb"):
        argv += ["--memory", f"{int(res['memory_mb'])}m"]
    if res.get("cpu_shares"):
        argv += ["--cpu-shares", str(int(res["cpu_shares"]))]

    # GPU NVIDIA (chantier multiview-GPU) : `gpus` (ex. "all", "device=0") → `--gpus <val>`.
    # Nécessite nvidia-container-toolkit + runtime nvidia sur l'hôte (cas dl360-2). Émis UNIQUEMENT
    # si la spec porte le champ → un nœud sans GPU ne le reçoit jamais (rien ne casse). NODE_AGENT.md §4.4.
    gpus = spec.get("gpus")
    if isinstance(gpus, str) and gpus.strip():
        argv += ["--gpus", gpus.strip()]

    for mnt in (spec.get("mounts") or []):
        # `options` (agent ≥ 0.22.0) : liste blanche de drapeaux de bind Docker. `rslave` est ce
        # qui fait apparaître dans un conteneur DÉJÀ démarré un partage monté après lui.
        opts = [o.strip() for o in str(mnt.get("options") or "").split(",") if o.strip()]
        bad = [o for o in opts if o not in _MOUNT_OPTS]
        if bad:
            raise ValueError("options de montage refusées : %s" % ",".join(bad))
        argv += ["-v", f"{mnt['host']}:{mnt['container']}" + (":" + ",".join(opts) if opts else "")]

    # mTLS par-conteneur (contrat inter-agent) : si le spec porte `tls` = {cert,key,ca} (PEM),
    # on matérialise ce trio dans un dossier hôte par-conteneur et on le bind read-only sur
    # /etc/bobi-tls → l'agent PAR-CONTENEUR (baké dans l'image) y trouve son matériel. Absent →
    # comportement inchangé (on ne casse jamais une spec sans tls).
    tls_dir = _materialize_container_tls(name, spec.get("tls"))
    if tls_dir:
        argv += ["-v", f"{tls_dir}:/etc/bobi-tls:ro"]

    for k, v in (spec.get("env") or {}).items():
        argv += ["-e", f"{k}={v}"]

    # Chantier RDMA (NODE_AGENT.md §4.4) : conteneur mxl-fabrics-demo. Devices/capacités/ulimits +
    # override d'entrypoint requis pour le RDMA verbs (le conteneur ne lance PAS l'agent par-conteneur).
    for dev in (spec.get("devices") or []):
        argv += ["--device", str(dev)]                       # ex. /dev/infiniband (uverbs)
    for cap in (spec.get("cap_add") or []):
        argv += ["--cap-add", str(cap)]                      # ex. IPC_LOCK (mémoire RDMA épinglée)
    for k, v in (spec.get("ulimits") or {}).items():
        argv += ["--ulimit", f"{k}={v}"]                     # ex. memlock=-1 (pas de plafond)
    ep = spec.get("entrypoint")
    if isinstance(ep, str) and ep.strip():
        argv += ["--entrypoint", ep.strip()]                 # remplace l'ENTRYPOINT de l'image

    argv.append(image)
    # `command` = argv passé APRÈS l'image (et après l'entrypoint s'il est surchargé).
    for a in (spec.get("command") or []):
        argv.append(str(a))
    return argv


def _container_status(name):
    rc, o, _ = docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", name, timeout=8)
    if rc != 0:
        return {"status": "absent", "exit_code": None}
    parts = o.strip().split()
    return {"status": parts[0] if parts else "unknown",
            "exit_code": int(parts[1]) if len(parts) > 1 else None}


def _container_ip(name, network):
    if not network or network == "host":
        return None
    fmt = '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}'
    rc, o, _ = docker("inspect", "-f", fmt, name, timeout=8)
    ip = o.strip()
    return ip if (rc == 0 and ip and ip != "<no value>") else None


# ─── Mémoire locale des specs (permet de RECRÉER un conteneur en l'absence du contrôleur) ──────
# Seuls les conteneurs en `--rm` (moteur MTL) en ont besoin : les autres portent une politique
# `--restart`, donc Docker les relève seul. Un conteneur en --rm qui meurt DISPARAÎT — sans spec
# gardée sur place, plus personne ne sait quoi relancer.
# ⚠ La spec contient le matériel mTLS du conteneur (clé privée) et son jeton d'agent : dossier 0700,
# fichiers 0600, et OUBLI à l'arrêt comme à la destruction. Le cert d'un conteneur porte l'URI
# `bobi://container/<vmid>`, précisément l'identité que les autres agents REFUSENT — la portée d'une
# fuite reste donc ce conteneur-là. C'est ce qui rend le compromis acceptable ; il reste réel.
SPEC_DIR = "/var/lib/bobi-node-agent/specs"


def _spec_path(name):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "")) or "container"
    return os.path.join(SPEC_DIR, safe + ".json")


def _spec_save(spec):
    try:
        os.makedirs(SPEC_DIR, mode=0o700, exist_ok=True)
        p = _spec_path(spec.get("name"))
        with open(p, "w") as f:
            json.dump(spec, f)
        os.chmod(p, 0o600)
    except Exception as e:
        print("[spec] sauvegarde %s impossible : %s" % (spec.get("name"), e), flush=True)


def _spec_load(name):
    try:
        with open(_spec_path(name)) as f:
            return json.load(f)
    except Exception:
        return None


def _spec_forget(name):
    """Appelé sur stop ET destroy : dans les deux cas l'INTENTION devient « arrêté ». Recréer
    derrière un arrêt volontaire serait exactement le second décideur qu'on refuse."""
    try:
        os.remove(_spec_path(name))
    except OSError:
        pass


def create_container(spec):
    """Idempotent : rm -f l'ancien puis run. Renvoie (ok, payload|err)."""
    name = spec.get("name"); image = spec.get("image")
    if not name or not image:
        return False, "name et image requis"
    try:
        argv = _build_run_argv(spec)                      # AVANT le rm : une spec refusée ne
    except ValueError as e:                               # doit pas coûter le conteneur en place
        return False, str(e)
    docker("rm", "-f", name, timeout=30)                  # réconciliation : repart propre
    rc, out, err = run(argv, timeout=90)
    if rc != 0:
        return False, f"docker run échoué : {(err or out).strip()[:300]}"
    _spec_save(spec)
    # IP macvlan (peut mettre un instant à être renseignée).
    ip = None
    if (spec.get("network") or "host") != "host":
        for _ in range(10):
            ip = _container_ip(name, spec["network"])
            if ip:
                break
            time.sleep(0.5)
    st = _container_status(name)
    _WD_RECENT_CREATE[name] = time.time()   # cycle de vie en vol → le chien de garde passe son tour
    return True, {"name": name, "ip": ip, **st}


# ─── Chien de garde de SCRIPT (quand le contrôleur est absent) ──────────────────
# CE QUE ÇA COUVRE, ET RIEN D'AUTRE : un script mort à l'intérieur d'un conteneur qui, lui, tourne
# toujours. Docker ne voit pas ce niveau-là (son `--restart` ne surveille que le PID 1, l'agent
# par-conteneur, qui va très bien) et l'orchestrateur, seul à relever ce cas, est absent par
# hypothèse. C'est exactement le trou : `script_stopped` non relevé pendant une coupure de contrôle.
#
# TROIS GARDE-FOUS, parce qu'un second décideur est plus dangereux qu'un trou :
#  1. INHIBITION — on n'agit que si le contrôleur n'a rien demandé depuis WATCHDOG_GRACE_S.
#     LAST_CONTROLLER est déjà tenu à jour par _auth_ok : aucun protocole à inventer.
#  2. INTENTION — on ne relance QUE ce qu'on a vu tourner au tour précédent. Un script arrêté
#     volontairement (par le contrôleur, avant sa disparition) est observé « arrêté » et le reste :
#     on ne le ressuscite pas. C'est ce qui remplace un drapeau `supervise` qu'il faudrait
#     persister, synchroniser et qui mentirait au premier redémarrage d'agent.
#  3. PLAFOND — backoff exponentiel puis abandon définitif à WATCHDOG_MAX_TRIES. Un script qui
#     meurt en boucle est un diagnostic, pas une chose à relancer indéfiniment.
#
# CE QU'IL NE FAIT PAS, par construction : redéployer (rendre un script exige `deploy_config` et le
# registre de plugins, donc la base — l'agent ne l'a pas et ne doit pas l'avoir : `/status.path`
# vide = on constate et on attend le contrôleur) ; toucher aux conteneurs en `--network host` (le
# moteur 2110 a son propre :8081 hors de ce contrat, et relancer un moteur DPDK à l'aveugle masque
# une panne matérielle) ; recâbler, réallouer, décider quoi que ce soit d'orchestration.
WATCHDOG_PERIOD_S   = 15
WATCHDOG_GRACE_S    = 60      # silence du contrôleur avant de s'autoriser à agir
WATCHDOG_MAX_TRIES  = 5
WATCHDOG_BACKOFF_S  = 10      # doublé à chaque tentative, plafonné à 300 s
WATCHDOG_CREATE_HOLD_S = 120  # après un docker run, on laisse le conteneur s'installer

_WD_STATE = {}            # name -> {prev_running, tries, next_try, scheme, note}
_WD_EVENTS = []           # journal borné, remonté au contrôleur dans health_payload
_WD_RECENT_CREATE = {}    # name -> ts du dernier docker run lancé par NOUS


def _wd_enabled():
    """Réglage `watchdog` du config.json (défaut ACTIF). Coupe-circuit local si un site n'en veut pas."""
    v = CONFIG.get("watchdog", True)
    return str(v).strip().lower() not in ("0", "false", "off", "no")


def _wd_note(msg):
    """Trace bornée : le contrôleur découvre à son retour ce qui a été fait en son absence.
    Une action muette serait pire que pas d'action du tout."""
    print("[watchdog] %s" % msg, flush=True)
    _WD_EVENTS.append({"ts": time.time(), "msg": msg})
    del _WD_EVENTS[:-50]


def _wd_controleur_silencieux():
    """True si le contrôleur ne s'est pas manifesté depuis WATCHDOG_GRACE_S. Jamais vu du tout
    (agent fraîchement démarré, ts None) → on considère qu'il est absent : c'est le cas du nœud
    qui reboote pendant que le contrôleur est en panne, précisément celui qu'on veut couvrir."""
    ts = LAST_CONTROLLER.get("ts")
    return (time.time() - ts) >= WATCHDOG_GRACE_S if ts else True


# SONDE EXÉCUTÉE DANS LE CONTENEUR. Pourquoi pas un simple appel HTTP depuis ici : nos conteneurs
# sont en macvlan, et une interface macvlan enfant ne parle JAMAIS à la pile de son interface
# parente. Un nœud joint les conteneurs de ses voisins, jamais les siens (mesuré sur dl360-1 :
# 100 % de perte vers son propre conteneur, 0 % vers celui d'en face — EHOSTUNREACH). On passe donc
# par `docker exec` et on appelle 127.0.0.1 depuis l'espace de noms réseau du conteneur lui-même.
# Le matériel TLS et le jeton sont DÉJÀ là-dedans (/etc/bobi-tls, $MXL_AGENT_TOKEN) : rien à
# distribuer, rien à router, et ça marchera à l'identique en ipvlan, en bridge ou en topologie
# séparée. Stdin plutôt qu'un `-c "…"` : aucun échappement à faire.
_WD_SONDE = r'''
import json, os, ssl, sys, urllib.error, urllib.request
chemin = os.environ.get("WD_PATH", "/status")
methode = os.environ.get("WD_METHOD", "GET")
tls, ctx, schema = "/etc/bobi-tls", None, "http"
try:
    if os.path.exists(tls + "/cert.pem") and os.path.exists(tls + "/key.pem"):
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=tls + "/ca.pem")
        ctx.load_cert_chain(tls + "/cert.pem", tls + "/key.pem")
        ctx.check_hostname = False
        schema = "https"
except Exception as e:
    print(json.dumps({"erreur": "tls: %s" % e})); sys.exit(0)
req = urllib.request.Request("%s://127.0.0.1:8081%s" % (schema, chemin), method=methode,
                             data=b"" if methode == "POST" else None)
jeton = os.environ.get("MXL_AGENT_TOKEN")
if jeton:
    req.add_header("X-MXL-Agent-Token", jeton)
try:
    with urllib.request.urlopen(req, timeout=4, context=ctx) as r:
        print(json.dumps({"code": r.status, "corps": r.read(8192).decode("utf-8", "replace")}))
except urllib.error.HTTPError as e:
    print(json.dumps({"code": e.code, "corps": ""}))
except Exception as e:
    print(json.dumps({"erreur": str(e)}))
'''


def _wd_sonder(name, path, method="GET", timeout=15):
    """Interroge le `:8081` d'un conteneur DEPUIS L'INTÉRIEUR (docker exec).
    Retourne (code, dict) ou (None, err_str)."""
    argv = ["docker", "exec", "-i", "-e", "WD_PATH=" + path, "-e", "WD_METHOD=" + method,
            name, "python3", "-"]
    rc, out, err = run(argv, timeout=timeout, input_bytes=_WD_SONDE.encode())
    if rc != 0:
        return None, ("docker exec rc=%s %s" % (rc, (err or out).strip()[:120]))
    try:
        res = json.loads((out or "").strip().splitlines()[-1])
    except Exception:
        return None, "sortie de sonde illisible : %s" % (out or "")[:120]
    if res.get("erreur"):
        return None, res["erreur"]
    try:
        return int(res.get("code")), json.loads(res.get("corps") or "{}")
    except Exception:
        return int(res.get("code") or 0), {}


_WD_INSPECT_CACHE = {}    # name -> network mode ; invalidé quand le conteneur disparaît


def _wd_reseau_cached(name):
    """Mode réseau d'un conteneur, mémoïsé pour sa VIE (il est figé au `docker run`). Le relire à
    chaque tour coûtait un `docker inspect` par conteneur toutes les 15 s — sur un nœud à 47
    conteneurs, ~3 processus docker par seconde en permanence, sur la machine qui compose en direct.
    L'entrée est purgée avec le conteneur (fin de `_wd_tick`) : une recréation relit forcément.
    Seul `host` nous intéresse (moteur 2110 → hors périmètre) ; l'IP et le jeton ne servent plus,
    la sonde s'exécute DANS le conteneur et y trouve les siens."""
    if name not in _WD_INSPECT_CACHE:
        rc, o, _ = docker("inspect", "-f", "{{.HostConfig.NetworkMode}}", name, timeout=8)
        _WD_INSPECT_CACHE[name] = (o or "").strip() if rc == 0 else None
    return _WD_INSPECT_CACHE[name]


def _wd_tick():
    """Un tour : on OBSERVE toujours, on n'AGIT que si le contrôleur est silencieux. Observer en
    permanence est ce qui donne son sens au garde-fou n°2 — sans état antérieur, impossible de
    distinguer « mort pendant la coupure » de « arrêté exprès avant elle »."""
    silencieux = _wd_controleur_silencieux()
    maintenant = time.time()
    vivants = set()
    for c in _list_containers():
        name = c.get("name") or ""
        if not name.startswith("bobi-") or not (c.get("status") or "").startswith("Up"):
            continue
        if _wd_reseau_cached(name) == "host":
            continue                       # moteur 2110 / --network host : hors périmètre, cf. entête
        vivants.add(name)
        st = _WD_STATE.setdefault(name, {"prev_running": None, "tries": 0, "next_try": 0.0,
                                         "note": "", "muet": 0, "next_poll": 0.0})
        # Conteneur qui ne répond JAMAIS (image antérieure à l'exemption loopback : la sonde y est
        # refusée) : sans ce ralentissement, on lui rejouerait un `docker exec` voué à l'échec
        # toutes les 15 s, pour toujours. On espace jusqu'à 10 min ; une réponse remet à zéro.
        # Vaut aussi pour un conteneur simplement en train de démarrer.
        if maintenant < st.get("next_poll", 0.0):
            continue
        code, data = _wd_sonder(name, "/status")
        if code != 200:
            st["muet"] = st.get("muet", 0) + 1
            st["next_poll"] = maintenant + min(WATCHDOG_PERIOD_S * (2 ** st["muet"]), 600)
            st["note"] = "injoignable (%s)" % (data if isinstance(data, str) else code)
            continue
        st["muet"] = 0
        st["next_poll"] = 0.0
        running = bool(data.get("running"))
        chemin = data.get("path") or ""
        avant = st["prev_running"]
        st["prev_running"] = running
        if running:
            if st["tries"]:
                _wd_note("%s : script revenu, compteur remis à zéro" % name)
            st["tries"] = 0
            st["next_try"] = 0.0
            st["note"] = ""
            continue
        # Script arrêté à partir d'ici.
        if not silencieux:
            st["note"] = "arrêté — le contrôleur est joignable, c'est son affaire"
            continue
        if avant is not True:
            st["note"] = "arrêté avant la coupure — laissé tel quel (arrêt probablement voulu)"
            continue
        if maintenant - _WD_RECENT_CREATE.get(name, 0.0) < WATCHDOG_CREATE_HOLD_S:
            st["note"] = "création récente — on laisse le conteneur s'installer"
            continue
        if not chemin:
            st["note"] = "script absent du disque — redéploiement nécessaire (contrôleur)"
            continue
        if st["tries"] >= WATCHDOG_MAX_TRIES:
            st["note"] = "abandon après %d tentatives — diagnostic requis" % st["tries"]
            continue
        if maintenant < st["next_try"]:
            continue
        code, data = _wd_sonder(name, "/start", method="POST")
        st["tries"] += 1
        st["next_try"] = maintenant + min(WATCHDOG_BACKOFF_S * (2 ** (st["tries"] - 1)), 300)
        if code == 200:
            st["note"] = "relancé (tentative %d)" % st["tries"]
            _wd_note("%s : script relancé (tentative %d/%d), contrôleur absent"
                     % (name, st["tries"], WATCHDOG_MAX_TRIES))
        else:
            st["note"] = "relance refusée (%s)" % (data if isinstance(data, str) else code)
            _wd_note("%s : relance refusée (%s)" % (name, st["note"]))
            if st["tries"] >= WATCHDOG_MAX_TRIES:
                _wd_note("%s : abandon après %d échecs" % (name, st["tries"]))
    for disparu in set(_WD_STATE) - vivants:
        _WD_STATE.pop(disparu, None)
        _WD_INSPECT_CACHE.pop(disparu, None)   # une recréation doit relire réseau/IP/jeton
    if silencieux:
        _wd_recreer_disparus()


def _wd_recreate_enabled():
    """Réglage `watchdog_recreate` — DÉFAUT DÉSACTIVÉ, et volontairement. Ce chemin ne sert que le
    moteur MTL (--rm) : un moteur DPDK relancé à l'aveugle reprend la PF, les hugepages et les
    lcores, et peut masquer une panne matérielle en boucle silencieuse. On l'arme site par site,
    en connaissance de cause."""
    v = CONFIG.get("watchdog_recreate", False)
    return str(v).strip().lower() in ("1", "true", "on", "yes")


_WD_RECREATED = set()     # une seule tentative par nom et par vie d'agent


def _wd_recreer_disparus():
    """Conteneur en --rm dont la spec est connue mais qui a DISPARU de Docker : une tentative de
    recréation, jamais deux. Les conteneurs à politique `--restart` ne passent pas par ici — Docker
    les relève déjà, et le faire nous-mêmes doublerait le décideur."""
    if not _wd_recreate_enabled():
        return
    try:
        fichiers = os.listdir(SPEC_DIR)
    except OSError:
        return
    presents = {c.get("name") for c in _list_containers()}
    for f in fichiers:
        if not f.endswith(".json"):
            continue
        spec = _spec_load(f[:-5])
        if not spec:
            continue
        name = spec.get("name")
        if not name or name in presents or name in _WD_RECREATED:
            continue
        if not spec.get("autoremove"):
            continue          # politique --restart : l'affaire de Docker, pas la nôtre
        _WD_RECREATED.add(name)
        ok, res = create_container(spec)
        _wd_note("%s : disparu et recréé depuis la spec locale (%s) — UNE seule tentative"
                 % (name, "ok" if ok else "échec : %s" % res))


def _wd_loop():
    while True:
        try:
            if _wd_enabled():
                _wd_tick()
        except Exception as e:
            print("[watchdog] tour en échec : %s" % e, flush=True)
        time.sleep(WATCHDOG_PERIOD_S)


def _wd_report():
    """État remonté au contrôleur (health_payload) : ce que le chien de garde a vu et fait."""
    return {
        "enabled": _wd_enabled(),
        "recreate_enabled": _wd_recreate_enabled(),
        "controller_silent": _wd_controleur_silencieux(),
        "grace_s": WATCHDOG_GRACE_S,
        "containers": {n: {"tries": s["tries"], "note": s["note"]}
                       for n, s in _WD_STATE.items() if s["tries"] or s["note"]},
        "events": list(_WD_EVENTS),
    }


# ─── Pilotes hôte (agent >= 0.28.0) — mécanisme GÉNÉRIQUE de « job long + journal + action en attente » ──
# Un « kind » de pilote hôte = un script `<AGENT_DIR>/host_drivers/<kind>.py` qui respecte le contrat :
#   `<script> state`  -> UN document JSON sur stdout (statut, plan, lignes d'affichage — des CODES) ;
#   `<script> run`    -> travaille, écrit un journal lisible sur stdout/stderr, et sort avec :
#                        0 = fini ; 10 = REDÉMARRAGE requis ; 11 = ARRÊT COMPLET (hors tension) requis ;
#                        autre = échec.
# L'agent ne connaît AUCUN kind : il liste le dossier, lance, journalise, mémorise l'action en attente.
# Aujourd'hui un seul kind (`nvidia`) ; un second (p.ex. Blackmagic Desktop Video + firmware DeckLink, qui
# exige 11 = arrêt complet) se branche en déposant son script — voir NODE_AGENT.md §4.6.
HD_DIR = os.environ.get("BOBI_HOST_DRIVERS_DIR", "/opt/bobi-node-agent/host_drivers")
HD_STATE_DIR = os.environ.get("BOBI_HOST_DRIVERS_STATE", "/var/lib/bobi-node-agent/host-drivers")
HD_FILES_DIR = os.environ.get("BOBI_HOST_DRIVERS_FILES", "/var/lib/bobi-node-agent/drivers")
HD_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
HD_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
HD_UPLOAD_EXT = (".run", ".deb", ".tar", ".gz", ".tgz", ".zip", ".bin", ".rpm")
HD_UPLOAD_MAX = 2 << 30
HD_EXIT_REBOOT, HD_EXIT_SHUTDOWN = 10, 11
HD_LOG_CHUNK = 64 * 1024
_HD_LOCK = threading.Lock()


def _hd_kinds():
    try:
        return sorted(f[:-3] for f in os.listdir(HD_DIR)
                      if f.endswith(".py") and HD_KIND_RE.match(f[:-3]))
    except OSError:
        return []


def _hd_script(kind):
    if not HD_KIND_RE.match(kind or ""):
        return None
    p = os.path.join(HD_DIR, kind + ".py")
    return p if os.path.isfile(p) else None


def _hd_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _hd_paths(kind):
    return (os.path.join(HD_STATE_DIR, kind + ".json"), os.path.join(HD_STATE_DIR, kind + ".log"),
            os.path.join(HD_STATE_DIR, kind + ".rc"))


def _hd_pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        with open("/proc/%d/stat" % int(pid)) as f:
            return f.read().split(") ")[-1].split()[0] != "Z"      # zombie = fini
    except (OSError, ValueError, IndexError):
        return False


def _hd_job_save(kind, job):
    os.makedirs(HD_STATE_DIR, exist_ok=True)
    jp = _hd_paths(kind)[0]
    with open(jp + ".tmp", "w") as f:
        json.dump(job, f)
    os.replace(jp + ".tmp", jp)


def _hd_job_load(kind):
    """État du dernier job, FINALISÉ paresseusement : un job lancé avant un redémarrage de l'agent
    n'a plus de thread qui l'attend, son code retour est dans le fichier .rc que le shell d'enveloppe
    écrit — on le lit ici, donc l'état est juste quoi qu'il soit arrivé à l'agent entre-temps."""
    jp, _lp, rp = _hd_paths(kind)
    try:
        with open(jp) as f:
            job = json.load(f)
    except (OSError, ValueError):
        return None
    if job.get("status") == "running" and not _hd_pid_alive(job.get("pid") or 0):
        rc = None
        try:
            with open(rp) as f:
                rc = int(f.read().strip())
        except (OSError, ValueError):
            pass
        job["ended"] = time.time()
        job["rc"] = rc if rc is not None else -1
        job["pending"] = {HD_EXIT_REBOOT: "reboot", HD_EXIT_SHUTDOWN: "shutdown"}.get(rc, "")
        job["status"] = "done" if rc in (0, HD_EXIT_REBOOT, HD_EXIT_SHUTDOWN) else "error"
        job["boot_id"] = _boot_id()
        _hd_job_save(kind, job)
    return job


def _hd_poweroff_marker():
    return os.path.join(HD_STATE_DIR, "poweroff_from_boot")


def _hd_pending(job):
    """Action en attente EFFECTIVE : 'reboot' | 'shutdown' | ''.

    · reboot   : levée dès que le boot_id change (le noyau a redémarré, le pilote est rechargé) ;
    · shutdown : un simple redémarrage NE la lève PAS (le micrologiciel d'une carte ne se recharge
                 qu'à la remise sous tension). Elle n'est levée que si CET agent a lui-même demandé
                 l'arrêt (marqueur écrit par /v1/host/poweroff, pour le boot_id du job) ET que le
                 boot_id a changé depuis."""
    if not job or not job.get("pending"):
        return ""
    if job.get("boot_id") == _boot_id():
        return job["pending"]
    if job["pending"] == "reboot":
        return ""
    try:
        with open(_hd_poweroff_marker()) as f:
            if f.read().strip() == job.get("boot_id"):
                return ""
    except OSError:
        pass
    return job["pending"]


def _hd_job_public(kind):
    job = _hd_job_load(kind)
    if not job:
        return {"status": "none", "pending": ""}
    pub = {k: job.get(k) for k in ("status", "started", "ended", "rc", "kind")}
    pub["pending"] = _hd_pending(job)
    pub["elapsed"] = int((job.get("ended") or time.time()) - (job.get("started") or time.time()))
    return pub


def _hd_state(kind):
    script = _hd_script(kind)
    if not script:
        return 404, {"ok": False, "error": "host_driver_missing", "kind": kind}
    rc, out, err = run([sys.executable or "python3", script, "state"], timeout=90)
    try:
        doc = json.loads(out)
    except ValueError:
        return 500, {"ok": False, "error": "state_failed", "detail": (err or out)[-400:]}
    doc["ok"] = True
    doc["script_sha256"] = _hd_sha(script)
    doc["job"] = _hd_job_public(kind)
    return 200, doc


def _hd_run(kind):
    script = _hd_script(kind)
    if not script:
        return 404, {"ok": False, "error": "host_driver_missing", "kind": kind}
    with _HD_LOCK:
        cur = _hd_job_load(kind)
        if cur and cur.get("status") == "running":
            return 409, {"ok": False, "error": "job_running"}
        _jp, lp, rp = _hd_paths(kind)
        os.makedirs(HD_STATE_DIR, exist_ok=True)
        for p in (rp, lp):
            try:
                os.unlink(p)
            except OSError:
                pass
        logf = open(lp, "ab", buffering=0)
        # `sh -c '…; echo $? > rc'` : le code retour survit à un redémarrage de l'agent (cf. _hd_job_load).
        wrapper = '"$@"; echo $? > "%s.tmp" && mv "%s.tmp" "%s"' % (rp, rp, rp)
        proc = subprocess.Popen(["sh", "-c", wrapper, "sh", sys.executable or "python3", script, "run"],
                                stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True,
                                env=dict(os.environ, PYTHONUNBUFFERED="1"))
        _hd_job_save(kind, {"kind": kind, "status": "running", "started": time.time(), "pid": proc.pid,
                            "pending": "", "boot_id": _boot_id()})
        threading.Thread(target=proc.wait, daemon=True).start()        # moissonne le zombie
    return 200, {"ok": True, "job": _hd_job_public(kind)}


def _hd_log(kind, offset):
    lp = _hd_paths(kind)[1]
    try:
        size = os.path.getsize(lp)
        offset = max(0, min(int(offset), size))
        with open(lp, "rb") as f:
            f.seek(offset)
            chunk = f.read(HD_LOG_CHUNK)
    except (OSError, ValueError):
        return {"ok": True, "job": _hd_job_public(kind), "log": "", "next_offset": 0}
    return {"ok": True, "job": _hd_job_public(kind), "log": chunk.decode(errors="replace"),
            "next_offset": offset + len(chunk)}


def _hd_upload(h, kind, u):
    """Dépose un fichier (installeur .run, paquet…) dans `HD_FILES_DIR/<kind>/` — corps BINAIRE lu par
    morceaux (jamais en mémoire), nom validé, place disque vérifiée AVANT d'écrire."""
    name = (parse_qs(u.query).get("name") or [""])[0]
    cl = int(h.headers.get("Content-Length") or 0)
    if not HD_KIND_RE.match(kind) or not HD_FILE_RE.match(name) or not name.lower().endswith(HD_UPLOAD_EXT):
        return h._send(400, {"ok": False, "error": "bad_name"})
    if cl <= 0 or cl > HD_UPLOAD_MAX:
        return h._send(400, {"ok": False, "error": "bad_size"})
    d = os.path.join(HD_FILES_DIR, kind)
    os.makedirs(d, exist_ok=True)
    free = shutil.disk_usage(d).free
    if free < cl + (512 << 20):
        return h._send(507, {"ok": False, "error": "disk_full", "free": free})
    dest = os.path.join(d, name)
    remaining, sha = cl, hashlib.sha256()
    try:
        with open(dest + ".part", "wb") as f:
            while remaining > 0:
                chunk = h.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                remaining -= len(chunk)
        if remaining:
            os.unlink(dest + ".part")
            return h._send(400, {"ok": False, "error": "truncated"})
        os.replace(dest + ".part", dest)
    except OSError as e:
        try:
            os.unlink(dest + ".part")
        except OSError:
            pass
        return h._send(500, {"ok": False, "error": "write_failed", "detail": str(e)})
    return h._send(200, {"ok": True, "name": name, "size": cl, "sha256": sha.hexdigest()})


def _hd_route(h, method, path, u):
    """Routes /v1/host/drivers/* et /v1/host/poweroff. Vrai si la requête a été traitée."""
    if path == "/v1/host/poweroff" and method == "POST":
        if not h._body().get("confirm"):
            h._send(400, {"ok": False, "error": "confirm_required"})
            return True
        # Le marqueur dit « cet arrêt vient de nous » : seul un arrêt demandé ici lève une action
        # 'shutdown' en attente (cf. _hd_pending). Posé AVANT l'ordre, jamais après.
        os.makedirs(HD_STATE_DIR, exist_ok=True)
        with open(_hd_poweroff_marker(), "w") as f:
            f.write(_boot_id())
        run(["systemd-run", "--on-active=3", "--quiet", "systemctl", "poweroff"], timeout=10)
        h._send(200, {"ok": True, "scheduled": "poweroff"})
        return True
    if not path.startswith("/v1/host/drivers"):
        return False
    if path == "/v1/host/drivers" and method == "GET":
        h._send(200, {"ok": True, "kinds": [{"kind": k, "script_sha256": _hd_sha(_hd_script(k))}
                                            for k in _hd_kinds()]})
        return True
    m = re.match(r"^/v1/host/drivers/([a-z][a-z0-9_]*)/(state|run|job|upload)$", path)
    if not m:
        h._send(404, {"ok": False, "error": "route inconnue"})
        return True
    kind, op = m.groups()
    if method == "GET" and op == "state":
        code, doc = _hd_state(kind)
        h._send(code, doc)
    elif method == "GET" and op == "job":
        if not _hd_script(kind):
            h._send(404, {"ok": False, "error": "host_driver_missing", "kind": kind})
        else:
            h._send(200, _hd_log(kind, (parse_qs(u.query).get("offset") or ["0"])[0]))
    elif method == "POST" and op == "run":
        code, doc = _hd_run(kind)
        h._send(code, doc)
    elif method == "POST" and op == "upload":
        _hd_upload(h, kind, u)
    else:
        h._send(405, {"ok": False, "error": "méthode non permise"})
    return True


# ─── Serveur HTTP ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = f"bobi-node-agent/{VERSION}"

    def log_message(self, *a):
        pass  # silence (journalisé par systemd au besoin)

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _peer_refused(self):
        """Envoie un 403 et → True si le pair TLS n'est pas un contrôleur (cf. _peer_refusal)."""
        motif = _peer_refusal(self.connection)
        if motif is None:
            return False
        cle, now = (self.client_address[0], motif), time.time()
        if now - _REFUS_JOURNAL.get(cle, 0) > 60:
            if len(_REFUS_JOURNAL) > 1000:
                _REFUS_JOURNAL.clear()
            _REFUS_JOURNAL[cle] = now
            print(f"[bobi-node-agent] REFUS {self.client_address[0]} : {motif}", flush=True)
        self._send(403, {"ok": False, "error": "seul un contrôleur pilote ce nœud"})
        return True

    def _auth_ok(self):
        expected = CONFIG.get("token") or ""
        given = self.headers.get("X-MXL-Node-Token", "")
        ok = bool(expected) and hmac.compare_digest(str(expected), str(given))
        if ok:   # mémorise le contrôleur (IP source) pour la page d'état :80
            LAST_CONTROLLER["ip"] = self.client_address[0]
            LAST_CONTROLLER["ts"] = time.time()
        return ok

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _send_clock(self, recu_utc, recu_tai):
        """Réponse de /v1/host/clock — le seul endroit de l'agent où l'ORDRE des lignes compte.

        Deux précautions, sans lesquelles l'endpoint ne vaudrait pas mieux que la sonde shell :

        · l'estampille d'émission est lue au plus tard, juste avant l'écriture — ce qui la suit
          n'est que du formatage (~20 µs, mille fois sous l'écart qu'on cherche à voir) ;
        · tête et corps partent en UN SEUL `write`. En deux écritures, la seconde attendrait l'ACK
          de la première (Nagle) : jusqu'à 40 ms de retard, soit exactement la grandeur qu'on
          prétend mesurer, ajoutée à la mesure elle-même.

        On n'utilise donc pas `_send` (qui écrit tête puis corps, et journalise au passage)."""
        env_utc, env_tai = _horloges_ns()
        corps = ('{"ok":true,"version":"%s","recv_utc_ns":%d,"recv_tai_ns":%d,'
                 '"send_utc_ns":%d,"send_tai_ns":%d}'
                 % (VERSION, recu_utc, recu_tai, env_utc, env_tai)).encode()
        tete = ("HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n" % len(corps)).encode()
        self.close_connection = True
        self.wfile.write(tete + corps)

    # — Routage —
    def do_GET(self):
        # Lues AVANT tout le reste (parsing d'URL, comparaison de token) : sur la mesure d'horloge,
        # ces quelques microsecondes sont du retard pur ajouté à l'aller. Coût pour les autres
        # routes : deux appels système, ~100 ns.
        recu_utc, recu_tai = _horloges_ns()
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if path == "/v1/ping":                       # liveness, sans token
            return self._send(200, {"agent": "bobi-node-agent", "version": VERSION})
        if self._peer_refused():
            return
        if not self._auth_ok():
            return self._send(401, {"ok": False, "error": "token invalide ou absent"})
        if path == "/v1/host/clock":
            return self._send_clock(recu_utc, recu_tai)
        if path == "/v1/host/transfer/status":
            code, obj = transfer_status((parse_qs(u.query).get("id") or [""])[0])
            return self._send(code, obj)
        if path == "/v1/host/transfer/upload/state":
            code, obj = upload_state(parse_qs(u.query))
            return self._send(code, obj)
        if _hd_route(self, "GET", path, u):          # pilotes hôte (0.28.0)
            return
        if path == "/v1/capabilities":
            return self._send(200, capabilities_payload())
        if path == "/v1/health":
            return self._send(200, health_payload())
        r = _dsk_http("GET", path, None)             # disques locaux (section _dsk_)
        if r:
            return self._send(*r)
        if path == "/v1/host/core-snapshot":
            # GET (mesure sans effet de bord) : reste utilisable derrière le garde-fou
            # HA readonly du contrôleur. Bloque ~0,5 s — OK, serveur threadé.
            return self._send(200, _core_snapshot())
        if path == "/v1/containers":
            return self._send(200, {"containers": _list_containers()})
        m = re.match(r"^/v1/containers/([^/]+)/status$", path)
        if m:
            return self._send(200, _container_status(m.group(1)))
        m = re.match(r"^/v1/containers/([^/]+)/logs$", path)
        if m:
            tail = (parse_qs(u.query).get("tail") or ["100"])[0]
            rc, o, e = docker("logs", "--tail", str(int(tail)), m.group(1), timeout=15)
            return self._send(200, {"lines": (o or e).splitlines()})
        if path == "/v1/host/ptp":
            if not has_cap("io2110"):
                return self._send(503, {"ok": False, "error": "capacité io2110 non provisionnée"})
            return self._send(200, _ptp())
        # Export d'image (relais build-on-node → distribution) : stream `docker save <tag>`.
        # L'agent est en HTTP/1.0 → corps délimité par FERMETURE de connexion (pas de Content-Length
        # ni chunked ; le client lit jusqu'à EOF). L'orchestrateur le buffer en fichier temp puis le
        # POST vers les autres nœuds (/v1/host/images/load). Évite tout registry.
        if path == "/v1/host/images/export":
            tag = (parse_qs(u.query).get("tag") or [""])[0]
            if not tag:
                return self._send(400, {"ok": False, "error": "tag requis (?tag=…)"})
            if docker("image", "inspect", tag, timeout=10)[0] != 0:
                return self._send(404, {"ok": False, "error": "image absente: %s" % tag})
            proc = subprocess.Popen(["docker", "save", tag], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True            # corps terminé par la fermeture (HTTP/1.0)
            try:
                while True:
                    chunk = proc.stdout.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            finally:
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
            return
        return self._send(404, {"ok": False, "error": "route inconnue"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if self._peer_refused():
            return
        if not self._auth_ok():
            return self._send(401, {"ok": False, "error": "token invalide ou absent"})

        # Dépôt d'un fichier PAR LE CONTRÔLEUR (import d'un projet avec ses médias) : corps BINAIRE,
        # lu par morceaux comme un chargement d'image, écrit en `.partial` puis renommé ; jamais
        # par-dessus un fichier existant ; date d'origine reposée. Sous le volume média seulement.
        if path == "/v1/host/transfer/upload":
            q = parse_qs(urlparse(self.path).query)
            if q.get("offset"):                     # 0.26.0 : morceau d'un envoi reprenable
                cl = int(self.headers.get("Content-Length") or 0)
                code, obj, nread = upload_chunk(q, self.rfile, cl)
                # Refus AVANT lecture (409, 507…) : le corps est vidé, sinon l'appelant, encore en
                # train d'écrire, prendrait une connexion coupée au lieu de lire la réponse.
                left = cl - nread
                while left > 0:
                    b = self.rfile.read(min(1 << 20, left))
                    if not b:
                        break
                    left -= len(b)
                return self._send(code, obj)
            dest = _media_path((q.get("dest") or [""])[0])
            cl = int(self.headers.get("Content-Length") or 0)
            if not dest or cl < 0:
                return self._send(400, {"ok": False, "error": "dest (sous le volume média) requis"})
            if os.path.exists(dest):
                return self._send(409, {"ok": False, "error": "un fichier existe déjà à la destination"})
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            part = dest + ".partial"
            _ioprio_idle()
            n = 0
            with open(part, "wb") as f:
                while n < cl:
                    b = self.rfile.read(min(1 << 20, cl - n))
                    if not b:
                        break
                    f.write(b)
                    n += len(b)
            if n != cl:
                os.remove(part)
                return self._send(400, {"ok": False, "error": "corps incomplet (%d/%d octets)" % (n, cl)})
            os.replace(part, dest)
            try:
                mt = float((q.get("mtime") or [""])[0])
                os.utime(dest, (mt, mt))
            except ValueError:
                pass
            return self._send(200, {"ok": True, "size": n})
        if path == "/v1/host/transfer/upload/finish":
            code, obj = upload_finish(self._body())
            return self._send(code, obj)
        if path == "/v1/host/transfer/upload/abort":
            code, obj = upload_abort(self._body())
            return self._send(code, obj)
        if path == "/v1/host/transfer/fetch":
            code, obj = transfer_fetch(self._body())
            return self._send(code, obj)
        if path == "/v1/host/transfer/cancel":
            code, obj = transfer_cancel(str(self._body().get("id") or ""))
            return self._send(code, obj)
        # Pilotes hôte (0.28.0) : AVANT _body() — l'upload est un corps binaire.
        if _hd_route(self, "POST", path, urlparse(self.path)):
            return

        # Chargement d'image : corps BINAIRE (docker save) → `docker load`. Traité AVANT _body()
        # (qui lirait le flux comme du JSON). Lu par morceaux → pas de 2 Go en mémoire.
        if path == "/v1/host/images/load":
            cl = int(self.headers.get("Content-Length") or 0)
            if cl <= 0:
                return self._send(400, {"ok": False, "error": "corps vide (Content-Length requis)"})
            proc = subprocess.Popen(["docker", "load"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            remaining = cl
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    remaining -= len(chunk)
                proc.stdin.close()
                out = proc.stdout.read().decode(errors="replace")
                rc = proc.wait(timeout=120)
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})
            return self._send(200 if rc == 0 else 500,
                              {"ok": rc == 0, "output": out.strip()[-300:]})

        # B3-1 : build d'image — corps BINAIRE (contexte tar.gz), AVANT _body() (qui le lirait en JSON).
        # `docker build` LOCAL avec le contexte en stdin → ferme le dernier root-SSH (build sur le nœud
        # piloté par l'agent). tag = query ?tag=…. Sortie buildkit renvoyée (tail) — pas de live stream.
        if path == "/v1/host/images/build":
            tag = (parse_qs(urlparse(self.path).query).get("tag") or [""])[0]
            if not tag:
                return self._send(400, {"ok": False, "error": "tag requis (?tag=…)"})
            cl = int(self.headers.get("Content-Length") or 0)
            if cl <= 0:
                return self._send(400, {"ok": False, "error": "contexte vide (Content-Length requis)"})
            remote = ('D=$(mktemp -d) && tar -xzf - -C "$D" && '
                      'DOCKER_BUILDKIT=1 docker build --progress=plain -t %s -f "$D/Dockerfile" "$D" 2>&1; '
                      'rc=$?; rm -rf "$D"; exit $rc') % __import__("shlex").quote(tag)
            proc = subprocess.Popen(["bash", "-c", remote], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            remaining = cl
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    remaining -= len(chunk)
                proc.stdin.close()
                out = proc.stdout.read().decode(errors="replace")
                rc = proc.wait(timeout=2400)
            except Exception as e:
                return self._send(500, {"ok": False, "rc": 1, "output": str(e)})
            return self._send(200, {"ok": rc == 0, "rc": rc, "output": out[-2000:]})

        body = self._body()

        if path == "/v1/containers":
            ok, res = create_container(body)
            return self._send(200 if ok else 500, res if ok else {"ok": False, "error": res})

        m = re.match(r"^/v1/containers/([^/]+)/(start|stop|destroy)$", path)
        if m:
            name, action = m.group(1), m.group(2)
            if action == "start":
                rc, o, e = docker("start", name, timeout=30)
            elif action == "stop":
                rc, o, e = docker("stop", "-t", str(int(body.get("timeout_s") or 10)), name, timeout=40)
                _spec_forget(name)      # intention = arrêté (cf. _spec_forget)
            else:  # destroy
                rc, o, e = docker("rm", "-f", name, timeout=30)
                _spec_forget(name)
            return self._send(200 if rc == 0 else 500,
                              {"ok": rc == 0, "error": (e or o).strip()[:300] if rc else None})

        if path == "/v1/host/xdp-off":
            if not has_cap("io2110"):
                return self._send(503, {"ok": False, "error": "capacité io2110 non provisionnée"})
            iface = body.get("iface") or CONFIG.get("mtl_iface")
            rc, o, e = run(["ip", "link", "set", "dev", iface, "xdp", "off"], timeout=10)
            return self._send(200 if rc == 0 else 500, {"ok": rc == 0, "error": (e or o).strip()[:200] if rc else None})

        if path == "/v1/host/images/ensure":
            tag = body.get("image")
            if not tag:
                return self._send(400, {"ok": False, "error": "image requise"})
            rc, _, _ = docker("image", "inspect", tag, timeout=8)
            if rc == 0:
                return self._send(200, {"ok": True, "present": True, "pulled": False})
            rc, o, e = docker("pull", tag, timeout=600)
            return self._send(200 if rc == 0 else 500,
                              {"ok": rc == 0, "present": rc == 0, "pulled": rc == 0,
                               "error": (e or o).strip()[:300] if rc else None})

        if path == "/v1/host/networks/ensure":
            name = body.get("name")
            if not name:
                return self._send(400, {"ok": False, "error": "name requis"})
            rc, _, _ = docker("network", "inspect", name, timeout=8)
            if rc == 0:
                return self._send(200, {"ok": True, "created": False})
            argv = ["docker", "network", "create", "-d", "macvlan",
                    "-o", f"parent={body.get('parent','')}"]
            if body.get("subnet"):
                argv += ["--subnet", body["subnet"]]
            if body.get("gateway"):
                argv += ["--gateway", body["gateway"]]
            if body.get("ip_range"):
                argv += ["--ip-range", body["ip_range"]]
            argv.append(name)
            rc, o, e = run(argv, timeout=20)
            return self._send(200 if rc == 0 else 500,
                              {"ok": rc == 0, "created": rc == 0, "error": (e or o).strip()[:300] if rc else None})

        m = re.match(r"^/v1/host/ptp/(start|stop|restart)$", path)
        if m:
            if not has_cap("io2110"):
                return self._send(503, {"ok": False, "error": "capacité io2110 non provisionnée"})
            # Pilote les DEUX unités (mxl-ptp4l + mxl-phc2sys), alignées sur app/ptp.py.
            rc, o, e = run(["systemctl", m.group(1), "mxl-ptp4l", "mxl-phc2sys"], timeout=15)
            return self._send(200 if rc == 0 else 500, {"ok": rc == 0, "error": (e or o).strip()[:200] if rc else None})

        # B3-1 : exec hôte générique (token-gated) — pont qui remplace le root-SSH du contrôleur par
        # le canal agent (token HTTP). Exécute `bash -c cmd` LOCALEMENT sur le nœud et renvoie
        # rc/stdout/stderr (même contrat que host_ops.ssh_run). Les endpoints structurés (containers/
        # images/networks/ptp/xdp) restent préférés ; exec couvre le reste des host-ops (MTL/VF/binds)
        # sans énumérer chaque commande. Durcissement structuré ultérieur possible.
        if path.startswith("/v1/host/disks"):        # disques locaux (section _dsk_)
            r = _dsk_http("POST", path, body)
            if r:
                return self._send(*r)

        if path == "/v1/host/exec":
            cmd = body.get("cmd")
            if not cmd:
                return self._send(400, {"ok": False, "error": "cmd requis"})
            inp = body.get("input")
            if isinstance(inp, str):
                inp = inp.encode()
            rc, o, e = run(["bash", "-c", cmd], timeout=int(body.get("timeout") or 300),
                           input_bytes=inp)
            return self._send(200, {"rc": rc, "stdout": o, "stderr": e})

        # ─── mTLS : migration à chaud pilotée par le contrôleur (canal HTTP+token) ───
        # /v1/tls/init : l'agent génère (idempotent pour la clé) sa clé + un CSR et le RENVOIE.
        # Ne bascule PAS encore en HTTPS (pas de cert signé). Le contrôleur fait signer, puis rappelle
        # /v1/tls/install avec {cert, ca_cert}.
        if path == "/v1/tls/init":
            ok, res = _ensure_key_and_csr()
            if not ok:
                return self._send(500, {"ok": False, "error": res})
            return self._send(200, {"ok": True, "csr": res, "already_tls": _tls_ready()})

        # /v1/tls/install {cert, ca_cert} : écrit node.crt + ca.crt (validés), puis programme un
        # redémarrage du process → l'agent repart en HTTPS/mTLS (repli HTTP conservé si le wrap échoue).
        if path == "/v1/tls/install":
            ok, err = _install_cert_material(body.get("cert"), body.get("ca_cert"))
            if not ok:
                return self._send(400, {"ok": False, "error": err})
            restart = _tls_ready()
            resp = {"ok": True, "installed": True, "tls_ready": restart,
                    "restart": "scheduled" if restart else "not_needed",
                    "note": "l'agent redémarre pour servir en HTTPS/mTLS" if restart
                            else "matériel incomplet — reste en HTTP"}
            self._send(200, resp)
            if restart:
                _schedule_restart()          # après flush de la réponse
            return

        return self._send(404, {"ok": False, "error": "route inconnue"})


# ─── Disques locaux : inventaire, formatage, montage persistant, mise en veille ──────────────
# SECTION AUTONOME (0.22.0). Tout vit sous le préfixe `_dsk_` ; le Handler n'en connaît que
# `_dsk_http`. Contrat : NODE_AGENT.md §« Disques locaux ».
#
# DOCTRINE — l'agent est la SEULE autorité. Le contrôleur ne fait que relayer : tout est
# re-vérifié ICI, au moment d'exécuter, depuis un `lsblk` frais (disque système, partition
# montée, série, taille). Un plan est un APERÇU, jamais un jeton : `execute` ne le reçoit pas,
# il le recalcule. Aucune commande ne passe par un shell ; le périphérique n'est jamais pris tel
# que fourni mais retrouvé dans l'inventaire (une chaîne inconnue = `disk_not_found`).
#
# Les refus sont des CODES (vocabulaire fermé `_DSK_REFUSALS`), jamais des phrases : c'est
# l'interface qui les traduit (cf. i18n/ `storage.disks.refusal.*`).
#
# ⚠ Pas de `systemctl daemon-reload` après une édition de fstab : il révoque le GPU des
# conteneurs en cours. `mount <cible>` lit fstab directement ; systemd regénère ses unités au
# prochain boot, ce qui est tout ce qu'on lui demande.
_DSK_FSTAB = "/etc/fstab"
_DSK_HDPARM_CONF = "/etc/hdparm.conf"
_DSK_DEFAULT_ARCHIVE = "/srv/archive"
_DSK_MARK = "# bobi-storage"
_DSK_LSBLK_COLS = ("NAME,PATH,KNAME,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA,RO,RM,FSTYPE,UUID,LABEL,"
                   "MOUNTPOINTS,PKNAME")
_DSK_MOUNTABLE_FS = ("ext2", "ext3", "ext4", "xfs", "btrfs")
# Membres d'un empilement (LVM, RAID, chiffrement, ZFS…) : on ne les formate pas d'un clic.
_DSK_HELD_FS = ("LVM2_member", "linux_raid_member", "crypto_LUKS", "zfs_member", "bcache",
                "isw_raid_member", "ddf_raid_member")
_DSK_SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", "/usr", "/var", "/etc", "[SWAP]")
_DSK_MOUNT_PREFIXES = ("/srv/", "/mnt/", "/media/", "/data/")
_DSK_ROLES = ("media", "archive", "data")
_DSK_REFUSALS = (
    "bad_request", "disk_not_found", "partition_not_found", "system_disk", "in_use", "held",
    "read_only", "serial_missing", "serial_mismatch", "size_mismatch", "confirm_required", "bad_role", "bad_mountpoint",
    "bad_label", "bad_minutes", "no_filesystem", "unsupported_fs", "ambiguous_partition",
    "mount_busy", "fstab_conflict", "target_not_empty", "media_containers_running",
    "not_rotational", "tool_missing", "nothing_to_do",
)
_dsk_lock = threading.Lock()


def _dsk_run(argv, timeout=60, input_bytes=None):       # couture de test
    return run(argv, timeout=timeout, input_bytes=input_bytes)


def _dsk_which(name):                                    # couture de test
    return shutil.which(name) is not None


def _dsk_statvfs(path):                                  # couture de test
    try:
        st = os.statvfs(path)
        return {"size": st.f_blocks * st.f_frsize, "avail": st.f_bavail * st.f_frsize,
                "used": (st.f_blocks - st.f_bfree) * st.f_frsize}
    except OSError:
        return None


def _dsk_byid(path):                                     # couture de test
    """Chemin stable /dev/disk/by-id/… d'un disque (ata-/nvme- de préférence au wwn-)."""
    base = "/dev/disk/by-id"
    try:
        cands = []
        for n in os.listdir(base):
            if "-part" in n:
                continue
            if os.path.realpath(os.path.join(base, n)) == os.path.realpath(path):
                cands.append(n)
    except OSError:
        return None
    cands.sort(key=lambda n: (n.startswith("wwn-") or n.startswith("nvme-eui"), n))
    return os.path.join(base, cands[0]) if cands else None


def _dsk_lsblk():
    rc, o, e = _dsk_run(["lsblk", "-J", "-b", "-o", _DSK_LSBLK_COLS], timeout=20)
    if rc != 0:
        # util-linux < 2.37 n'a pas MOUNTPOINTS (pluriel) : repli sur MOUNTPOINT.
        rc, o, e = _dsk_run(["lsblk", "-J", "-b", "-o",
                             _DSK_LSBLK_COLS.replace("MOUNTPOINTS", "MOUNTPOINT")], timeout=20)
    if rc != 0:
        return None
    try:
        return json.loads(o).get("blockdevices") or []
    except ValueError:
        return None


def _dsk_mounts_of(node):
    mp = node.get("mountpoints")
    if mp is None and node.get("mountpoint"):
        mp = [node["mountpoint"]]
    return [m for m in (mp or []) if m]


def _dsk_walk(node):
    """Le nœud lsblk et tous ses descendants (partitions, LV, md, crypt…)."""
    yield node
    for c in node.get("children") or []:
        yield from _dsk_walk(c)


def _dsk_is_system_mount(m):
    return m in _DSK_SYSTEM_MOUNTS or m.startswith("/boot/")


def _dsk_fstab_entries(path=None):
    """[(index, source, target, fstype, opts, marked)] des lignes actives du fstab."""
    out = []
    try:
        with open(path or _DSK_FSTAB) as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        p = s.split()
        if len(p) < 3:
            continue
        marked = i > 0 and lines[i - 1].strip().startswith(_DSK_MARK)
        out.append((i, p[0], p[1], p[2], p[3] if len(p) > 3 else "", marked))
    return out


def _dsk_inventory():
    """Disques entiers, annotés. None si lsblk est inutilisable (on REFUSE alors d'agir)."""
    tree = _dsk_lsblk()
    if tree is None:
        return None
    media = (CONFIG.get("media_mount") or "/srv/mxl-media").rstrip("/") or "/srv/mxl-media"
    fstab = _dsk_fstab_entries()
    disks = []
    for d in tree:
        if d.get("type") != "disk":
            continue
        nodes = list(_dsk_walk(d))
        mounts = sorted({m for n in nodes for m in _dsk_mounts_of(n)})
        # `[SWAP]` figure dans les montages tant que le swap est ACTIF ; une partition swap
        # inerte (ancienne installation) ne rend pas le disque « système ».
        system = any(_dsk_is_system_mount(m) for m in mounts)
        parts = []
        for n in (nodes[1:] if len(nodes) > 1 else ([d] if d.get("fstype") else [])):
            mps = _dsk_mounts_of(n)
            role = None
            for m in mps:
                if m.rstrip("/") == media:
                    role = "media"
                elif m.rstrip("/") == _DSK_DEFAULT_ARCHIVE:
                    role = "archive"
                else:
                    role = role or "data"
            uuid = n.get("uuid")
            ft = [e for e in fstab if uuid and e[1] in ("UUID=" + uuid, "/dev/disk/by-uuid/" + uuid)]
            parts.append({
                "path": n.get("path"), "type": n.get("type"), "size": n.get("size"),
                "fstype": n.get("fstype"), "uuid": uuid, "label": n.get("label"),
                "mountpoints": mps, "role": role,
                "fstab_target": ft[0][2] if ft else None,
                "usage": _dsk_statvfs(mps[0]) if mps and not mps[0].startswith("[") else None,
            })
        held = any((n.get("fstype") in _DSK_HELD_FS)
                   or (n is not d and n.get("type") not in ("part", "loop")) for n in nodes)
        disks.append({
            "path": d.get("path"), "by_id": _dsk_byid(d.get("path")), "size": d.get("size"),
            "model": (d.get("model") or "").strip() or None,
            "serial": (d.get("serial") or "").strip() or None,
            "transport": d.get("tran"), "rotational": bool(d.get("rota")),
            "removable": bool(d.get("rm")), "read_only": bool(d.get("ro")),
            "partitions": parts, "mountpoints": mounts,
            "system": system, "in_use": bool(mounts), "held": held,
        })
    return disks


def _dsk_power_state(disk):
    """État d'alimentation d'un disque rotatif ('active/idle', 'standby'…). `hdparm -C` ne le réveille pas."""
    if not (disk.get("rotational") and _dsk_which("hdparm")):
        return None
    rc, o, e = _dsk_run(["hdparm", "-C", disk["path"]], timeout=15)
    m = re.search(r"drive state is:\s*(\S+)", o or "")
    return m.group(1) if (rc == 0 and m) else "unknown"


def _dsk_spindown_conf(by_id, path=None):
    """Valeur hdparm (-S) persistée pour ce disque dans hdparm.conf, ou None."""
    if not by_id:
        return None
    try:
        with open(path or _DSK_HDPARM_CONF) as f:
            txt = f.read()
    except OSError:
        return None
    m = re.search(re.escape(_DSK_MARK) + r" begin " + re.escape(by_id)
                  + r"\n.*?spindown_time\s*=\s*(\d+)", txt, re.S)
    return int(m.group(1)) if m else None


def _dsk_list():
    disks = _dsk_inventory()
    if disks is None:
        return {"ok": False, "error": "lsblk_failed"}
    for d in disks:
        d["power_state"] = _dsk_power_state(d)
        d["spindown_value"] = _dsk_spindown_conf(d["by_id"]) if d["rotational"] else None
    return {"ok": True, "disks": disks,
            "media_mount": CONFIG.get("media_mount") or "/srv/mxl-media",
            "archive_mount": _DSK_DEFAULT_ARCHIVE,
            "tools": {t: _dsk_which(t) for t in ("sfdisk", "mkfs.ext4", "hdparm", "findmnt")}}


# — validations —
def _dsk_valid_mountpoint(mp, allow_any_prefix=False):
    if not isinstance(mp, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", mp):
        return None
    norm = os.path.normpath(mp)
    if norm != mp.rstrip("/") or ".." in mp.split("/"):
        return None
    if norm.startswith("/var/lib/docker"):
        return None
    if not allow_any_prefix and not (norm + "/").startswith(_DSK_MOUNT_PREFIXES):
        return None
    if norm in ("/srv", "/mnt", "/media", "/data") or norm.count("/") < 2:
        return None
    return norm


def _dsk_valid_label(s, default):
    if s in (None, ""):
        return default
    if isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,16}", s):
        return s
    return None


def _dsk_spindown_value(minutes):
    """Minutes → argument de `hdparm -S` (0 = jamais ; 1..240 ×5 s ; 241..251 ×30 min)."""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        return None
    if minutes == 0:
        return 0
    if minutes <= 20:
        return minutes * 12
    if minutes % 30 == 0 and minutes <= 330:
        return 240 + minutes // 30
    return None


def _dsk_target_for(role, mountpoint):
    """(mountpoint, refus). media → media_mount de la config, sans discussion."""
    if role == "media":
        mp = _dsk_valid_mountpoint(CONFIG.get("media_mount") or "/srv/mxl-media", allow_any_prefix=True)
        return mp, (None if mp else "bad_mountpoint")
    if role == "archive" and not mountpoint:
        return _DSK_DEFAULT_ARCHIVE, None
    mp = _dsk_valid_mountpoint(mountpoint)
    return mp, (None if mp else "bad_mountpoint")


def _dsk_media_containers(target):
    """Conteneurs EN COURS qui montent `target` (ou un sous-dossier) en bind."""
    rc, o, e = _dsk_run(["docker", "ps", "-q"], timeout=15)
    ids = o.split() if rc == 0 else []
    if not ids:
        return []
    rc, o, e = _dsk_run(["docker", "inspect", "--format",
                         "{{.Name}}|{{range .Mounts}}{{.Source}};{{end}}", *ids], timeout=20)
    names = []
    for ln in (o or "").splitlines():
        name, _, srcs = ln.partition("|")
        if any(s == target or s.startswith(target.rstrip("/") + "/") for s in srcs.split(";") if s):
            names.append(name.lstrip("/"))
    return sorted(names)


def _dsk_pick_partition(disk, want):
    """La partition (ou le disque lui-même s'il porte un système de fichiers) visée."""
    parts = [p for p in disk["partitions"] if p.get("fstype")]
    if want:
        for p in disk["partitions"]:
            if p["path"] == want:
                return p, None
        return None, "partition_not_found"
    if len(parts) == 1:
        return parts[0], None
    return None, ("no_filesystem" if not parts else "ambiguous_partition")


def _dsk_media_warn(plan, target, req):
    """Conteneurs qui montent le volume média en bind : ils ne verront PAS le changement
    (un bind-mount garde l'ancien dossier). On le dit, et on exige un accusé explicite."""
    names = _dsk_media_containers(target)
    if names:
        plan["media_containers"] = names
        plan["warnings"].append("media_containers_running")
        if not req.get("ack_media_containers"):
            plan["refusals"].append("media_containers_running")


def _dsk_plan_target(plan, req, mp, role, part=None):
    """Contrôles propres à la CIBLE de montage (déjà occupée ? non vide ? conteneurs média ?)."""
    ref = plan["refusals"]
    rc, o, e = _dsk_run(["findmnt", "-n", "-o", "SOURCE,UUID", "--mountpoint", mp], timeout=10)
    cur = o.split() if rc == 0 else []
    if cur:
        if part and part["uuid"] and len(cur) > 1 and cur[1] == part["uuid"]:
            plan["already_mounted"] = True
        else:
            ref.append("mount_busy")
            return
    elif os.path.isdir(mp) and os.listdir(mp):
        plan["warnings"].append("target_not_empty")
        if not req.get("allow_non_empty"):
            ref.append("target_not_empty")
    for ent in _dsk_fstab_entries():
        mine = bool(part and part["uuid"] and ent[1] == "UUID=" + part["uuid"])
        if ent[2].rstrip("/") == mp.rstrip("/") and not (ent[5] or mine):
            ref.append("fstab_conflict")
            return
    if role == "media" and not plan.get("already_mounted"):
        _dsk_media_warn(plan, mp, req)


# — plan : calcule ce qui arriverait, et les refus. Rappelé tel quel par `execute`. —
def _dsk_plan(req):
    action = req.get("action")
    plan = {"ok": True, "action": action, "refusals": [], "warnings": [], "steps": [],
            "disk": None,
            # format : série SAISIE + taille ; mount/unmount : `confirm: true` explicite ; spindown : rien.
            "confirmation": {"format": "serial", "mount": "confirm", "unmount": "confirm"}.get(action)}
    ref = plan["refusals"]
    if action not in ("format", "mount", "unmount", "spindown"):
        ref.append("bad_request")
        return plan
    disks = _dsk_inventory()
    if disks is None:
        ref.append("disk_not_found")
        plan["detail"] = "lsblk_failed"
        return plan
    disk = next((d for d in disks if d["path"] == req.get("device")), None)
    if not disk:
        ref.append("disk_not_found")
        return plan
    plan["disk"] = {k: disk[k] for k in ("path", "size", "serial", "model", "transport", "rotational")}
    if disk["system"]:
        ref.append("system_disk")
    if disk["read_only"]:
        ref.append("read_only")
    if action == "format" and not disk["serial"]:
        ref.append("serial_missing")          # rien à faire saisir à l'opérateur : on refuse
    if ref:
        return plan
    role = req.get("role") or "data"

    if action == "format":
        if disk["in_use"]:
            ref.append("in_use")
        if disk["held"]:
            ref.append("held")
        if role not in _DSK_ROLES:
            ref.append("bad_role")
        mp, r = _dsk_target_for(role, req.get("mountpoint"))
        if r:
            ref.append(r)
        label = _dsk_valid_label(req.get("label"), "bobi-" + role)
        if label is None:
            ref.append("bad_label")
        for t in ("sfdisk", "mkfs.ext4", "findmnt"):
            if not _dsk_which(t):
                ref.append("tool_missing")
                plan["detail"] = t
                break
        if ref:
            return plan
        plan["target"], plan["label"], plan["role"] = mp, label, role
        plan["steps"] = ["wipe", "gpt", "partition", "mkfs_ext4", "fstab", "mount"]
        plan["destroys"] = [p["path"] for p in disk["partitions"]] or [disk["path"]]
        _dsk_plan_target(plan, req, mp, role)
        return plan

    if action == "mount":
        part, r = _dsk_pick_partition(disk, req.get("partition"))
        if r:
            ref.append(r)
            return plan
        if part["fstype"] not in _DSK_MOUNTABLE_FS:
            ref.append("unsupported_fs")
        if not part["uuid"]:
            ref.append("no_filesystem")
        if role not in _DSK_ROLES:
            ref.append("bad_role")
        mp, r = _dsk_target_for(role, req.get("mountpoint"))
        if r:
            ref.append(r)
        if part["mountpoints"] and mp not in part["mountpoints"]:
            ref.append("mount_busy")          # déjà monté ailleurs : on ne le déplace pas en douce
        if ref:
            return plan
        plan["partition"], plan["target"], plan["role"] = part["path"], mp, role
        plan["steps"] = ["fstab", "mkdir", "mount", "verify"]
        _dsk_plan_target(plan, req, mp, role, part=part)
        return plan

    if action == "unmount":
        part, r = _dsk_pick_partition(disk, req.get("partition"))
        if r:
            ref.append(r)
            return plan
        fst = [e for e in _dsk_fstab_entries()
               if part["uuid"] and e[1] in ("UUID=" + part["uuid"], "/dev/disk/by-uuid/" + part["uuid"])]
        if not part["mountpoints"] and not fst:
            ref.append("nothing_to_do")
            return plan
        plan["partition"] = part["path"]
        plan["target"] = (part["mountpoints"] or [fst[0][2]])[0]
        plan["steps"] = (["umount"] if part["mountpoints"] else []) + (["fstab"] if fst else [])
        if plan["target"].rstrip("/") == (CONFIG.get("media_mount") or "/srv/mxl-media").rstrip("/"):
            _dsk_media_warn(plan, plan["target"], req)
        return plan

    # spindown
    if not disk["rotational"]:
        ref.append("not_rotational")
    if _dsk_spindown_value(req.get("minutes")) is None:
        ref.append("bad_minutes")
    if not _dsk_which("hdparm"):
        ref.append("tool_missing")
        plan["detail"] = "hdparm"
    if not ref:
        plan["value"] = _dsk_spindown_value(req.get("minutes"))
        plan["steps"] = ["hdparm_apply", "hdparm_conf"]
        plan["power_state"] = _dsk_power_state(disk)
    return plan


# — fstab —
def _dsk_fstab_line(uuid, target, fstype, role):
    return (f"{_DSK_MARK} role={role}\n"
            f"UUID={uuid} {target} {fstype} defaults,nofail,noatime,x-systemd.device-timeout=10s 0 2")


def _dsk_fstab_rewrite(drop, add_block=None, path=None):
    """Réécrit fstab ATOMIQUEMENT (tmp + replace, droits conservés, sauvegarde .bobi-bak).
    `drop(source, target, marked)` → True pour retirer la ligne (et son marqueur, si c'est le nôtre)."""
    path = path or _DSK_FSTAB
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    kill = set()
    for (i, src, tgt, _ft, _o, marked) in _dsk_fstab_entries(path):
        if drop(src, tgt, marked):
            kill.add(i)
            if marked:
                kill.add(i - 1)
    new = [ln for i, ln in enumerate(lines) if i not in kill]
    if add_block:
        new += add_block.splitlines()
    text = "\n".join(new) + "\n"
    try:
        shutil.copy2(path, path + ".bobi-bak")
    except OSError:
        pass
    tmp = path + ".bobi-tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    except OSError:
        os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _dsk_fstab_set(uuid, target, fstype, role):
    """Pose (ou remplace) NOTRE ligne pour ce volume. Idempotent : N appels = 1 ligne.
    Une ligne NON marquée vers la même cible est refusée en amont (`fstab_conflict`)."""
    _dsk_fstab_rewrite(lambda src, tgt, marked: src == "UUID=" + uuid
                       or (marked and tgt.rstrip("/") == target.rstrip("/")),
                       _dsk_fstab_line(uuid, target, fstype, role))


def _dsk_fstab_unset(uuid, target):
    _dsk_fstab_rewrite(lambda src, tgt, marked: src in ("UUID=" + uuid, "/dev/disk/by-uuid/" + uuid)
                       and tgt.rstrip("/") == target.rstrip("/"))


def _dsk_fstab_forget(uuids):
    """Retire NOS lignes dont le volume vient d'être détruit (jamais celles de l'exploitant)."""
    keys = {"UUID=" + u for u in uuids if u}
    if keys:
        _dsk_fstab_rewrite(lambda src, tgt, marked: marked and src in keys)


# — exécution —
def _dsk_fail(res, code, detail=None):
    res["ok"] = False
    res["error"] = code
    if detail:
        res["detail"] = str(detail)[:300]
    return res


def _dsk_refuse(res, code):
    res["ok"] = False
    res["error"] = "refused"
    res["refusals"] = [code]
    return res


def _dsk_mount_now(res, uuid, target):
    """mkdir + `mount <cible>` (lit fstab) + vérification par findmnt. Idempotent."""
    rc, o, e = _dsk_run(["findmnt", "-n", "-o", "UUID", "--mountpoint", target], timeout=10)
    if rc == 0 and o.strip() == uuid:
        res["steps_done"].append("already_mounted")
        return True
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as ex:
        _dsk_fail(res, "mount_failed", ex)
        return False
    res["steps_done"].append("mkdir")
    rc, o, e = _dsk_run(["mount", target], timeout=60)
    if rc != 0:
        _dsk_fail(res, "mount_failed", e or o)
        return False
    rc, o, e = _dsk_run(["findmnt", "-n", "-o", "UUID", "--mountpoint", target], timeout=10)
    if rc != 0 or o.strip() != uuid:
        _dsk_fail(res, "verify_failed", o or e)
        return False
    res["steps_done"].append("mount")
    return True


def _dsk_execute(req):
    with _dsk_lock:
        plan = _dsk_plan(req)          # RECALCULÉ ici : le contrôleur ne dicte rien
        res = {"ok": True, "action": plan["action"], "steps_done": [], "warnings": plan["warnings"],
               "reboot_required": False, "changed": False}
        if plan["refusals"]:
            res["ok"] = False
            res["error"] = "refused"
            res["refusals"] = plan["refusals"]
            res["detail"] = plan.get("detail")
            res["media_containers"] = plan.get("media_containers")
            return res
        disk = plan["disk"]
        if plan["confirmation"] == "confirm" and req.get("confirm") is not True:
            return _dsk_refuse(res, "confirm_required")
        if plan["confirmation"] == "serial":
            if str(req.get("confirm_serial") or "").strip() != disk["serial"]:
                return _dsk_refuse(res, "serial_mismatch")
            cs = req.get("confirm_size")
            if isinstance(cs, bool) or cs != disk["size"]:
                return _dsk_refuse(res, "size_mismatch")
        res["disk"] = disk
        act = plan["action"]
        if act == "format":
            return _dsk_do_format(plan, res)
        if act == "mount":
            return _dsk_do_mount(plan, res)
        if act == "unmount":
            return _dsk_do_unmount(plan, res)
        return _dsk_do_spindown(plan, res)


def _dsk_disk_now(dev):
    return next((x for x in (_dsk_inventory() or []) if x["path"] == dev), None)


def _dsk_do_format(plan, res):
    dev = plan["disk"]["path"]
    old = _dsk_disk_now(dev)
    sfd = "label: gpt\nstart=1MiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4\n"
    rc, o, e = _dsk_run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", dev],
                        timeout=60, input_bytes=sfd.encode())
    if rc != 0:
        return _dsk_fail(res, "partition_failed", e or o)
    res["steps_done"] += ["wipe", "gpt", "partition"]
    res["changed"] = True
    _dsk_fstab_forget([p["uuid"] for p in (old or {}).get("partitions", [])])
    _dsk_run(["udevadm", "settle", "--timeout=15"], timeout=20)
    # Le noyau a-t-il relu la table ? Sinon la nouvelle partition n'existe pas : reboot.
    part = None
    for _ in range(10):
        d = _dsk_disk_now(dev)
        if d and d["partitions"]:
            part = d["partitions"][0]["path"]
            break
        _dsk_run(["partprobe", dev], timeout=15)
        time.sleep(0.5)
    if not part:
        res["reboot_required"] = True
        return _dsk_fail(res, "partition_not_visible")
    # Dernier contrôle avant le geste irréversible : la partition neuve est-elle bien libre ?
    d = _dsk_disk_now(dev)
    if not d or d["in_use"] or d["system"]:
        return _dsk_fail(res, "in_use")
    rc, o, e = _dsk_run(["mkfs.ext4", "-F", "-m", "0", "-L", plan["label"], part], timeout=900)
    if rc != 0:
        return _dsk_fail(res, "mkfs_failed", e or o)
    res["steps_done"].append("mkfs_ext4")
    uuid = _dsk_new_uuid(dev, part)
    if not uuid:
        return _dsk_fail(res, "verify_failed", "uuid")
    _dsk_fstab_set(uuid, plan["target"], "ext4", plan["role"])
    res["steps_done"].append("fstab")
    if _dsk_mount_now(res, uuid, plan["target"]):
        res.update({"target": plan["target"], "uuid": uuid, "partition": part})
    return res


def _dsk_new_uuid(dev, part):
    """UUID du système de fichiers que mkfs vient d'écrire sur `part`.

    lsblk lit l'UUID dans la base d'udev, et udev ne l'a pas encore relu quand mkfs rend la main :
    une lecture immédiate rend None. Constaté le 2026-10-23 sur la Z8 (HDD WD 1 To) — formatage
    abandonné après mkfs, sans fstab ni montage, alors que l'UUID apparaissait quelques secondes
    plus tard. D'où : settle, relectures, puis blkid -p (sonde le superbloc, sans udev)."""
    _dsk_run(["udevadm", "settle", "--timeout=15"], timeout=20)
    for _ in range(10):
        d = _dsk_disk_now(dev)
        uuid = next((p["uuid"] for p in (d or {}).get("partitions", []) if p["path"] == part), None)
        if uuid:
            return uuid
        time.sleep(0.5)
    rc, o, _e = _dsk_run(["blkid", "-p", "-s", "UUID", "-o", "value", part], timeout=15)
    if rc != 0:
        return None
    return (o or "").strip() or None


def _dsk_do_mount(plan, res):
    d = _dsk_disk_now(plan["disk"]["path"])
    part = next(p for p in d["partitions"] if p["path"] == plan["partition"])
    _dsk_fstab_set(part["uuid"], plan["target"], part["fstype"], plan["role"])
    res["steps_done"].append("fstab")
    res["changed"] = not plan.get("already_mounted")
    if _dsk_mount_now(res, part["uuid"], plan["target"]):
        res.update({"target": plan["target"], "uuid": part["uuid"], "partition": part["path"]})
        if plan.get("media_containers"):
            res["restart_containers"] = plan["media_containers"]
    return res


def _dsk_do_unmount(plan, res):
    d = _dsk_disk_now(plan["disk"]["path"])
    part = next(p for p in d["partitions"] if p["path"] == plan["partition"])
    for mp in part["mountpoints"]:
        rc, o, e = _dsk_run(["umount", mp], timeout=60)
        if rc != 0:
            return _dsk_fail(res, "umount_busy", e or o)
        res["steps_done"].append("umount")
        res["changed"] = True
    if "fstab" in plan["steps"] and part["uuid"]:
        _dsk_fstab_unset(part["uuid"], plan["target"])
        res["steps_done"].append("fstab")
        res["changed"] = True
    if plan.get("media_containers"):
        res["restart_containers"] = plan["media_containers"]
    return res


def _dsk_hdparm_conf_set(by_id, value, path=None):
    path = path or _DSK_HDPARM_CONF
    try:
        with open(path) as f:
            txt = f.read()
    except FileNotFoundError:
        txt = ""
    txt = re.sub(re.escape(_DSK_MARK) + r" begin " + re.escape(by_id) + r"\n.*?" + re.escape(_DSK_MARK)
                 + r" end\n?", "", txt, flags=re.S)
    if value:
        if txt and not txt.endswith("\n"):
            txt += "\n"
        txt += (f"{_DSK_MARK} begin {by_id}\n{by_id} {{\n\tspindown_time = {value}\n}}\n"
                f"{_DSK_MARK} end\n")
    tmp = path + ".bobi-tmp"
    with open(tmp, "w") as f:
        f.write(txt)
    os.replace(tmp, path)


def _dsk_do_spindown(plan, res):
    dev, value = plan["disk"]["path"], plan["value"]
    rc, o, e = _dsk_run(["hdparm", "-S", str(value), dev], timeout=20)
    if rc != 0:
        return _dsk_fail(res, "hdparm_failed", e or o)
    res["steps_done"].append("hdparm_apply")
    by_id = _dsk_byid(dev)
    if by_id:
        _dsk_hdparm_conf_set(by_id, value)
        res["steps_done"].append("hdparm_conf")
    else:
        res["warnings"].append("no_by_id_not_persisted")
    res["changed"] = True
    res["value"] = value
    return res


def _dsk_http(method, path, body):
    """Aiguillage HTTP. Retourne (code, objet) ou None si la route n'est pas à nous."""
    if method == "GET" and path == "/v1/host/disks":
        r = _dsk_list()
        return (200 if r["ok"] else 500), r
    if method == "POST" and path in ("/v1/host/disks/plan", "/v1/host/disks/execute"):
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "bad_request"}
        if path.endswith("/plan"):
            return 200, _dsk_plan(body)
        r = _dsk_execute(body)
        print(f"[disks] {body.get('action')} {body.get('device')} -> "
              f"{'ok' if r['ok'] else r.get('error')} {r.get('refusals') or ''}", flush=True)
        return 200, r
    return None


# ─── Page d'état publique (:80) ──────────────────────────────────────────────
def _fmt_dur(s):
    if s is None:
        return "—"
    s = int(s)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d} j {h} h"
    if h:
        return f"{h} h {m} min"
    return f"{m} min"


def _fmt_gb(mb):
    return "—" if mb is None else f"{mb / 1024:.1f} Go"


def _bar(pct):
    if pct is None:
        return ""
    pct = max(0, min(100, pct))
    col = "#e0533b" if pct >= 90 else ("#e0a93b" if pct >= 75 else "#3bce82")
    return (f'<div class="bar"><div class="fill" style="width:{pct:.0f}%;background:{col}"></div></div>'
            f'<span class="pct">{pct:.0f}%</span>')


def _ctl_base():
    """URL de base du contrôleur : la config (mémorisée à l'install via --controller-url) en priorité,
    sinon le dernier contrôleur vu (ip:5000). None si on ne sait rien."""
    u = (CONFIG.get("controller_url") or "").strip().rstrip("/")
    if u:
        return u if "://" in u else "http://" + u
    ip = LAST_CONTROLLER.get("ip")
    return f"http://{ip}:5000" if ip else None


def _controller_identity(base_url):
    """Identité publique du contrôleur (GET <base>/api/identity), cachée 60 s. None si injoignable."""
    if not base_url:
        return None
    now = time.time()
    if _CTL_ID["ip"] == base_url and now - _CTL_ID["ts"] < 60:
        return _CTL_ID["data"]
    data = None
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/identity", timeout=2) as r:
            data = json.load(r)
    except Exception:
        data = None
    _CTL_ID.update(ip=base_url, ts=now, data=data)
    return data


def render_status_html():
    """Page HTML publique (sans token, sans secret) : identité + contrôleur + santé."""
    e = html.escape
    cap = capabilities_payload()
    h = health_payload()
    res = h.get("resources") or {}
    disks = h.get("disks") or {}
    vers = h.get("versions") or {}
    caps = cap.get("capabilities") or []
    host = cap.get("host") or "node"
    # IPs du nœud (depuis l'inventaire NIC).
    ips = []
    for nic in (cap.get("nics") or []):
        ips += nic.get("addrs") or []
    # Contrôleur : URL mémorisée à l'install (priorité), sinon dernier vu.
    base = _ctl_base()
    ctl_ip = LAST_CONTROLLER.get("ip")
    ctl_ts = LAST_CONTROLLER.get("ts")
    ctl = "—  (non configuré ; aucune requête authentifiée reçue)"
    ident = _controller_identity(base)
    if base:
        name = (ident or {}).get("name") or base
        seen = f" · vu il y a {_fmt_dur(time.time() - ctl_ts)}" if ctl_ts else " · pas encore contacté"
        ctl = (f'<a href="{e(base)}" target="_blank" rel="noopener">{e(name)}</a>'
               f'  <span class="dim">({e(base)}{seen})</span>')

    rows = []
    rows.append(("Hôte", e(host)))
    rows.append(("Adresses IP", e(", ".join(ips) or "—")))
    rows.append(("Agent", f"v{VERSION}  ·  uptime {_fmt_dur(h.get('uptime_s'))}"))
    rows.append(("Hôte démarré depuis", _fmt_dur(h.get("host_uptime_s"))))
    rows.append(("Contrôleur", ctl))
    if ident:
        if ident.get("org"):
            rows.append(("Entreprise", e(ident["org"])))
        if ident.get("location"):
            rows.append(("Localisation", e(ident["location"])))
        if ident.get("version"):
            rows.append(("Version contrôleur", e(ident["version"])))
    dock = h.get("docker") or {}
    rows.append(("Docker", e(dock.get("version") or "—")))
    rows.append(("Noyau", e(vers.get("kernel") or "—")))
    rows.append(("OS", e(vers.get("os") or "—")))
    info_rows = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)

    caps_html = "".join(f'<span class="cap">{e(c)}</span>' for c in caps) or "—"

    cpu = res.get("cpu_pct_real")
    cpu = res.get("cpu_pct") if cpu is None else cpu
    mem_pct = round(res.get("mem_used_mb") / res["mem_total_mb"] * 100, 1) if res.get("mem_total_mb") else None
    usage = [
        ("CPU", _bar(cpu), f"{(res.get('cpu_count') or '?')} cœurs"),
        ("RAM", _bar(mem_pct), f"{_fmt_gb(res.get('mem_used_mb'))} / {_fmt_gb(res.get('mem_total_mb'))}"),
    ]
    for key, label in (("shm", "/dev/shm"), ("root", "/"), ("media", "média")):
        d = disks.get(key)
        if d:
            usage.append((label, _bar(d.get("pct")), e(d.get("path") or "")))
    usage_html = "".join(
        f'<div class="u"><span class="ul">{e(lbl)}</span>{bar}<span class="us">{sub}</span></div>'
        for lbl, bar, sub in usage)

    conts = h.get("containers") or []
    if conts:
        cont_html = "".join(
            f'<tr><td>{e(c.get("name",""))}</td><td>{e(c.get("status",""))}</td>'
            f'<td class="dim">{e(c.get("image",""))}</td></tr>' for c in conts)
        cont_html = f'<table class="conts"><tr><th>Conteneur</th><th>État</th><th>Image</th></tr>{cont_html}</table>'
    else:
        cont_html = '<p class="dim">Aucun conteneur.</p>'

    ptp_html = ""
    if cap.get("nic") is not None or "io2110" in caps:
        p = h.get("ptp") or {}
        lock = "verrouillé ✓" if p.get("locked") else "non verrouillé ✗"
        ptp_html = (f'<h2>PTP</h2><table><tr><th>État</th><td>{e(lock)}</td></tr>'
                    f'<tr><th>Grandmaster</th><td>{e(str(p.get("gm_id") or "—"))}</td></tr>'
                    f'<tr><th>Offset</th><td>{e(str(p.get("offset_ns"))) if p.get("offset_ns") is not None else "—"} ns</td></tr></table>')

    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>{e(host)} — bobi-node-agent</title>
<style>
:root {{ color-scheme: dark; }}
body {{ font-family: system-ui, sans-serif; background:#15171c; color:#e6e8ec; margin:0; padding:28px; }}
.wrap {{ max-width: 760px; margin: 0 auto; }}
h1 {{ font-size:1.5rem; margin:0 0 2px; }}
h1 .v {{ color:#7aa2f7; font-size:.9rem; font-weight:400; }}
h2 {{ font-size:1rem; margin:26px 0 8px; color:#9aa4b2; border-bottom:1px solid #2a2e37; padding-bottom:4px; }}
.caps {{ margin:10px 0 4px; }}
.cap {{ display:inline-block; background:#222732; color:#9ad; border-radius:12px; padding:2px 10px; font-size:.8rem; margin-right:6px; }}
table {{ border-collapse:collapse; width:100%; }}
th {{ text-align:left; color:#8b94a3; font-weight:500; padding:3px 12px 3px 0; white-space:nowrap; vertical-align:top; }}
td {{ padding:3px 0; }}
.dim {{ color:#6b7280; }}
a {{ color:#7aa2f7; }}
.conts th, .conts td {{ padding:4px 12px 4px 0; }}
.u {{ display:flex; align-items:center; gap:10px; margin:6px 0; }}
.ul {{ width:84px; color:#8b94a3; }}
.us {{ color:#8b94a3; font-size:.85rem; min-width:140px; }}
.bar {{ flex:1; height:8px; background:#262a31; border-radius:4px; overflow:hidden; }}
.fill {{ height:100%; }}
.pct {{ width:42px; text-align:right; }}
footer {{ margin-top:28px; color:#5b6270; font-size:.78rem; }}
</style></head><body><div class="wrap">
<h1>{e(host)} <span class="v">bobi-node-agent v{VERSION}</span></h1>
<div class="caps">{caps_html}</div>
<h2>Identité &amp; connexion</h2>
<table>{info_rows}</table>
<h2>Ressources</h2>
{usage_html}
<h2>Conteneurs</h2>
{cont_html}
{ptp_html}
<footer>Page d'état publique · rafraîchie toutes les 5 s · l'API de contrôle (port {int(CONFIG.get('port') or 9100)}) reste protégée par jeton.</footer>
</div></body></html>"""


# ─── TRANSFERT DE FICHIERS NŒUD → NŒUD (copie de médias) ─────────────────────────────────────
# Une copie de média entre nœuds ne passe plus par l'orchestrateur (ni par son root-SSH) : le nœud
# CIBLE télécharge le fichier directement chez le nœud SOURCE.
#
#   · Port DÉDIÉ (`transfer_port`, 9101), en mTLS : le port de pilotage (9100) n'accepte QUE le
#     contrôleur, et doit le rester. Celui-ci accepte un certificat de NŒUD (ou de contrôleur),
#     jamais de conteneur, et ne sert qu'une chose : LIRE un fichier du volume média.
#   · Chaque lecture exige une AUTORISATION signée par l'orchestrateur avec le jeton de CE nœud
#     (HMAC-SHA256) : un seul fichier, une taille, une échéance. Un nœud ne peut rien lire d'autre,
#     ni réutiliser l'autorisation après échéance.
#   · Le nœud cible télécharge avec curl à basse priorité (nice 19, ionice idle), sous un plafond de
#     débit, avec REPRISE (`-C -`) d'un `.partial` interrompu ; la date d'origine est reposée.
#     Les conteneurs en antenne passent avant la copie, pas l'inverse.
#   · Le réseau est celui de CONTRÔLE (adresse du nœud) : jamais les cartes 2110 / RDMA du direct.
import base64 as _b64

TRANSFER_PORT_DEFAULT = 9101
_transfers = {}                      # id → état d'un téléchargement (nœud cible)
_transfers_lock = threading.Lock()


def _media_root():
    return (CONFIG.get("media_mount") or "/srv/mxl-media").rstrip("/")


def _media_path(rel):
    """Chemin absolu sous le volume média, ou None s'il en sort."""
    root = os.path.realpath(_media_root())
    full = os.path.realpath(os.path.join(root, str(rel or "").lstrip("/")))
    return full if full.startswith(root + os.sep) else None


# ─── Envoi reprenable (0.26.0) : un fichier du poste d'un utilisateur, relayé par le contrôleur ──
# Le contrôleur envoie des MORCEAUX à un décalage ; chacun s'ajoute au `.partial` SEULEMENT si le
# décalage égale sa taille actuelle (sinon 409 avec la taille réelle : la reprise part de ce que le
# nœud a VRAIMENT, jamais de ce que croit l'appelant). Finalisation : taille totale vérifiée, date
# reposée, renommage SANS écrasement (lien dur : échoue si la cible existe, atomiquement).
UPLOAD_FREE_MARGIN = 1 << 30                # 1 Gio laissé libre sur le volume, en plus du reste à recevoir
_upload_locks = {}
_upload_locks_guard = threading.Lock()


def _upload_lock(dest):
    with _upload_locks_guard:
        return _upload_locks.setdefault(dest, threading.Lock())


def _partial_size(dest):
    try:
        return os.path.getsize(dest + ".partial")
    except OSError:
        return 0


def _free_bytes(dest):
    d = os.path.dirname(dest)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    return shutil.disk_usage(d or _media_root()).free


def _q1(q, k):
    v = q.get(k)
    return (v[0] if isinstance(v, list) else v) if v not in (None, "", []) else None


def upload_state(q):
    """?dest= → taille du `.partial`, cible déjà présente, espace libre."""
    dest = _media_path(_q1(q, "dest"))
    if not dest:
        return 400, {"ok": False, "error": "dest (sous le volume média) requis"}
    try:
        free = _free_bytes(dest)
    except OSError:
        free = None
    return 200, {"ok": True, "partial_size": _partial_size(dest), "exists": os.path.exists(dest), "free": free}


def upload_chunk(q, rfile, cl):
    """Morceau `cl` octets lus dans `rfile`, à écrire au décalage `offset` du `.partial` de `dest`.
    `total` (optionnel) : taille finale annoncée → vérification de l'espace libre.
    Renvoie (code, réponse, octets lus du corps)."""
    dest = _media_path(_q1(q, "dest"))
    try:
        offset = int(_q1(q, "offset"))
        total = int(_q1(q, "total")) if _q1(q, "total") is not None else None
    except (TypeError, ValueError):
        return 400, {"ok": False, "error": "offset/total entiers requis"}, 0
    if not dest or offset < 0 or cl < 0:
        return 400, {"ok": False, "error": "dest (sous le volume média) et offset requis"}, 0
    if os.path.exists(dest):
        return 409, {"ok": False, "exists": True, "error": "un fichier existe déjà à la destination"}, 0
    lk = _upload_lock(dest)
    if not lk.acquire(blocking=False):
        return 409, {"ok": False, "busy": True, "partial_size": _partial_size(dest),
                     "error": "un autre morceau de ce fichier est en cours d'écriture"}, 0
    try:
        cur = _partial_size(dest)
        if offset != cur:
            return 409, {"ok": False, "partial_size": cur,
                         "error": "décalage %d ≠ %d octets déjà reçus" % (offset, cur)}, 0
        if total is not None:
            if offset + cl > total:
                return 400, {"ok": False, "error": "morceau au-delà de la taille annoncée"}, 0
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            need, free = total - offset + UPLOAD_FREE_MARGIN, _free_bytes(dest)
            if free < need:
                return 507, {"ok": False, "no_space": True, "free": free, "needed": need,
                             "error": "espace insuffisant sur le volume média (%d octets libres, %d nécessaires)"
                                      % (free, need)}, 0
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _ioprio_idle()
        n = 0
        with open(dest + ".partial", "ab") as f:
            while n < cl:
                b = rfile.read(min(1 << 20, cl - n))
                if not b:
                    break
                f.write(b)
                n += len(b)
        # Morceau coupé en route : ce qui est arrivé RESTE (c'est un préfixe exact) ; l'appelant
        # reprend à la taille réelle.
        if n != cl:
            return 400, {"ok": False, "partial_size": offset + n,
                         "error": "morceau incomplet (%d/%d octets)" % (n, cl)}, n
        return 200, {"ok": True, "partial_size": offset + n}, n
    finally:
        lk.release()


def upload_finish(body):
    """{dest, size, mtime?} : `.partial` complet → `dest`, jamais par-dessus un fichier existant."""
    dest = _media_path(body.get("dest"))
    try:
        size = int(body.get("size"))
    except (TypeError, ValueError):
        size = -1
    if not dest or size < 0:
        return 400, {"ok": False, "error": "dest (sous le volume média) et size requis"}
    with _upload_lock(dest):
        part = dest + ".partial"
        cur = _partial_size(dest)
        if not os.path.exists(part) or cur != size:
            return 409, {"ok": False, "partial_size": cur, "error": "reçu %d octets sur %d" % (cur, size)}
        try:
            os.link(part, dest)                 # échoue (EEXIST) si la cible existe : jamais d'écrasement
        except FileExistsError:
            return 409, {"ok": False, "exists": True, "error": "un fichier existe déjà à la destination"}
        os.remove(part)
        if body.get("mtime"):
            try:
                mt = float(body["mtime"])
                os.utime(dest, (mt, mt))
            except (TypeError, ValueError, OSError):
                pass
    return 200, {"ok": True, "size": size}


def upload_abort(body):
    """{dest} : supprime le `.partial` (envoi annulé). Le fichier final, s'il existe, n'est pas touché."""
    dest = _media_path(body.get("dest"))
    if not dest:
        return 400, {"ok": False, "error": "dest (sous le volume média) requis"}
    with _upload_lock(dest):
        try:
            os.remove(dest + ".partial")
            removed = True
        except OSError:
            removed = False
    return 200, {"ok": True, "removed": removed}


def _grant_check(g):
    """Autorisation `<b64url(json)>.<hex hmac>` → payload, ou (None, motif)."""
    try:
        body, sig = str(g).rsplit(".", 1)
        want = hmac.new(str(CONFIG.get("token") or "").encode(), body.encode(), "sha256").hexdigest()
        if not CONFIG.get("token") or not hmac.compare_digest(want, sig):
            return None, "autorisation invalide"
        pl = json.loads(_b64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None, "autorisation illisible"
    if float(pl.get("exp") or 0) < time.time():
        return None, "autorisation expirée"
    return pl, None


def _transfer_peer_refusal(conn):
    """Port de transfert : un NŒUD ou le contrôleur ; jamais un conteneur."""
    getpeercert = getattr(conn, "getpeercert", None)
    cert = getpeercert() if getpeercert else None
    if not cert:
        return "connexion sans certificat client"
    _cns, uris = _peer_identities(cert)
    if any(str(u).startswith("bobi://container/") for u in uris):
        return "certificat de conteneur refusé"
    if any(str(u).startswith(("bobi://node/", CONTROLLER_URI_PREFIX)) for u in uris):
        return None
    return "identité inattendue"


def _ioprio_idle():
    """Fil courant en priorité disque « idle » (ioprio_set, x86_64 = 251) — best-effort."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall(251, 1, threading.get_native_id(), (3 << 13))
    except Exception:
        pass


def _transfer_range(header, size):
    """En-tête `Range` du port de transfert → (début, fin INCLUSE), ou None (pas de plage : fichier
    entier), ou False (416). Formes : `bytes=a-b` (0.27.0, fin bornée à la taille), `bytes=a-`,
    `bytes=-n` (les n derniers). Forme ouverte : comportement d'avant 0.27.0 CONSERVÉ — un début égal
    à la taille rend 206 vide (c'est ce que reçoit `curl -C -` sur un `.partial` déjà complet)."""
    h = (header or "").strip()
    if not h.startswith("bytes="):
        return None
    a, sep, b = h[6:].split(",", 1)[0].strip().partition("-")
    try:
        if not sep:
            return None
        if not a.strip():                                    # suffixe : les n derniers octets
            n = int(b)
            return (max(0, size - n), size - 1) if n > 0 and size > 0 else False
        start = int(a)
        if not b.strip():
            return (start, size - 1) if 0 <= start <= size else False
        end = int(b)
    except ValueError:
        return None
    if start < 0 or end < start or start >= size:
        return False
    return start, min(end, size - 1)


class TransferHandler(BaseHTTPRequestHandler):
    server_version = f"bobi-node-agent-transfer/{VERSION}"
    # HTTP/1.1 : la connexion RESTE OUVERTE entre deux lectures (chaque réponse porte sa
    # Content-Length) — le contrôleur enchaîne les plages d'un proxy (survol, lecteur) sans
    # repayer la poignée de main mTLS. `timeout` borne l'INACTIVITÉ (attente de la requête
    # suivante) et une écriture bloquée : la connexion est fermée au-delà, jamais gardée à vie.
    protocol_version = "HTTP/1.1"
    timeout = 120

    def log_message(self, *a):
        pass

    def _fail(self, code, msg, headers=None):
        body = json.dumps({"ok": False, "error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/v1/transfer/read":
            return self._fail(404, "inconnu")
        motif = _transfer_peer_refusal(self.connection)
        if motif:
            return self._fail(403, motif)
        pl, err = _grant_check((parse_qs(u.query).get("g") or [""])[0])
        if err:
            return self._fail(403, err)
        full = _media_path(pl.get("p"))
        if not full or not os.path.isfile(full):
            return self._fail(404, "fichier absent")
        size = os.path.getsize(full)
        if pl.get("sz") is not None and int(pl["sz"]) != size:
            return self._fail(409, "le fichier a changé depuis l'autorisation")
        rng = _transfer_range(self.headers.get("Range"), size)
        if rng is False:
            return self._fail(416, "plage hors fichier", {"Content-Range": "bytes */%d" % size})
        start, end = rng if rng else (0, size - 1)
        length = max(0, end - start + 1)
        _ioprio_idle()
        self.send_response(206 if rng and (start or end < size - 1) else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if rng and (start or end < size - 1):
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(full, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    b = f.read(min(1 << 20, left))
                    if not b:
                        break
                    self.wfile.write(b)
                    left -= len(b)
            if left:                        # fichier raccourci en route : la longueur annoncée est fausse
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, TimeoutError):
            self.close_connection = True    # le lecteur a coupé : rien à signaler, connexion finie


def _make_transfer_ssl_context():
    """mTLS du port de transfert : certificat client signé par notre CA EXIGÉ ; le RÔLE (nœud ou
    contrôleur, jamais conteneur) est vérifié ensuite, requête par requête."""
    p = _tls_paths()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=p["crt"], keyfile=p["key"])
    ctx.load_verify_locations(cafile=p["ca"])
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _transfer_server():
    """Port de transfert (mTLS) : démarré seulement si le matériel TLS est là — sans lui, les copies
    restent sur le repli de l'orchestrateur."""
    if not _tls_ready():
        print("[bobi-node-agent] transfert de fichiers : pas de TLS, port non ouvert")
        return
    port = int(CONFIG.get("transfer_port") or TRANSFER_PORT_DEFAULT)
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), TransferHandler)
        httpd.socket = _make_transfer_ssl_context().wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True, name="transfer").start()
        print(f"[bobi-node-agent] transfert de fichiers (lecture seule, mTLS) sur :{port}")
    except Exception as ex:
        print(f"[bobi-node-agent] ⚠ port de transfert non ouvert : {ex}")


def _fetch_run(tid):
    with _transfers_lock:
        t = _transfers[tid]
    p = _tls_paths()
    part = t["dest"] + ".partial"
    os.makedirs(os.path.dirname(t["dest"]), exist_ok=True)
    cmd = ["nice", "-n", "19", "ionice", "-c3", "curl", "-fsS", "--cert", p["crt"], "--key", p["key"],
           "--cacert", p["ca"], "-C", "-", "-o", part, t["url"]]
    if t.get("rate"):
        cmd[cmd.index("-o"):cmd.index("-o")] = ["--limit-rate", str(int(t["rate"]))]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with _transfers_lock:
        t["proc"] = proc
    _o, err = proc.communicate()
    with _transfers_lock:
        t.pop("proc", None)
        if t.get("state") == "cancelled":
            return
        if proc.returncode != 0:
            t.update(state="failed", error=(err or b"").decode("utf-8", "replace").strip()[-300:] or "curl rc=%d" % proc.returncode)
            return
    try:
        if t.get("size") is not None and os.path.getsize(part) != int(t["size"]):
            raise OSError("taille reçue %d ≠ %d attendus" % (os.path.getsize(part), int(t["size"])))
        if os.path.exists(t["dest"]):
            raise OSError("un fichier existe déjà à la destination")
        os.replace(part, t["dest"])
        if t.get("mtime"):
            os.utime(t["dest"], (float(t["mtime"]), float(t["mtime"])))
        with _transfers_lock:
            t.update(state="done", done=os.path.getsize(t["dest"]), ended=time.time())
    except OSError as e:
        with _transfers_lock:
            t.update(state="failed", error=str(e))


def transfer_fetch(body):
    """Contrôleur → nœud CIBLE : télécharger `url` (chez le nœud source) vers `dest` (relatif au
    volume média). Tâche de fond ; état sur transfer_status."""
    tid = str(body.get("id") or "")
    dest = _media_path(body.get("dest"))
    if not tid or not dest or not str(body.get("url") or "").startswith("https://"):
        return 400, {"ok": False, "error": "id, dest (sous le volume média) et url https requis"}
    with _transfers_lock:
        old = _transfers.get(tid)
        if old and old.get("state") == "running":
            return 200, {"ok": True, "id": tid, "state": "running"}
        _transfers[tid] = {"id": tid, "url": body["url"], "dest": dest, "size": body.get("size"),
                           "mtime": body.get("mtime"), "rate": body.get("rate"), "state": "running",
                           "started": time.time(), "error": ""}
    threading.Thread(target=_fetch_run, args=(tid,), daemon=True, name="fetch-" + tid).start()
    return 200, {"ok": True, "id": tid, "state": "running"}


def transfer_status(tid):
    with _transfers_lock:
        t = dict(_transfers.get(tid) or {})
    if not t:
        return 404, {"ok": False, "error": "transfert inconnu"}
    t.pop("proc", None)
    if t.get("state") == "running":
        try:
            t["done"] = os.path.getsize(t["dest"] + ".partial")
        except OSError:
            t["done"] = 0
    t.pop("url", None)
    return 200, dict(t, ok=True)


def transfer_cancel(tid):
    with _transfers_lock:
        t = _transfers.get(tid)
        if not t:
            return 404, {"ok": False, "error": "transfert inconnu"}
        t["state"] = "cancelled"
        proc = t.get("proc")
    if proc:
        proc.terminate()
    try:
        os.remove(t["dest"] + ".partial")
    except OSError:
        pass
    return 200, {"ok": True}


class InfoHandler(BaseHTTPRequestHandler):
    """Serveur public minimal (port 80) : page d'état HTML. Aucun secret, pas de contrôle ici."""
    server_version = f"bobi-node-agent-info/{VERSION}"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            try:
                body = render_status_html().encode()
            except Exception as ex:
                body = f"<pre>page d'état indisponible : {html.escape(str(ex))}</pre>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    if not CONFIG.get("token"):
        print("[bobi-node-agent] ⚠ aucun token configuré — toutes les routes (sauf /v1/ping) "
              "renverront 401. Renseignez 'token' dans " + CONFIG_PATH)
    port = int(CONFIG.get("port") or 9100)
    # Page d'état publique (best-effort : ne bloque pas l'agent si :80 est pris / interdit).
    info_port = int(CONFIG.get("info_port") or 0)
    if info_port:
        try:
            info_httpd = ThreadingHTTPServer(("0.0.0.0", info_port), InfoHandler)
            threading.Thread(target=info_httpd.serve_forever, daemon=True).start()
            print(f"[bobi-node-agent] page d'état publique sur :{info_port}")
        except OSError as ex:
            print(f"[bobi-node-agent] page d'état non démarrée sur :{info_port} ({ex})")
    threading.Thread(target=_membw_loop, daemon=True, name="membw").start()
    _transfer_server()
    if _wd_enabled():
        threading.Thread(target=_wd_loop, daemon=True, name="watchdog").start()
        print("[bobi-node-agent] chien de garde de script actif (agit après %d s de silence "
              "du contrôleur)" % WATCHDOG_GRACE_S)
    else:
        print("[bobi-node-agent] chien de garde de script DÉSACTIVÉ (réglage `watchdog`)")
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    # mTLS conditionnel : si le matériel TLS est présent, on wrappe le socket serveur en HTTPS+mTLS.
    # REPLI : toute erreur (cert corrompu, clé illisible…) → on reste en HTTP clair, JAMAIS de crash
    # (un push de cert raté ne doit pas rendre le nœud injoignable). Le token reste requis dans les 2 modes.
    scheme = "http"
    if _tls_ready():
        try:
            ctx = _make_server_ssl_context()
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            scheme = "https"
            print(f"[bobi-node-agent] TLS activé (mTLS, cert client requis) — matériel dans {_tls_paths()['dir']}")
        except Exception as ex:
            scheme = "http"
            print(f"[bobi-node-agent] ⚠ activation TLS échouée ({ex}) — REPLI HTTP clair (nœud reste joignable)")
    else:
        print(f"[bobi-node-agent] matériel TLS absent ({_tls_paths()['dir']}) — service en HTTP clair (repli)")

    print(f"[bobi-node-agent] v{VERSION} sur {scheme}://0.0.0.0:{port} — capacités={CONFIG.get('capabilities')}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
