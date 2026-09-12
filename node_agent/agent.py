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

VERSION = "0.21.0"  # 0.21.0 : chien de garde — sonde par docker exec (macvlan isole l hote)
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
        argv += ["-v", f"{mnt['host']}:{mnt['container']}"]

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
    docker("rm", "-f", name, timeout=30)                  # réconciliation : repart propre
    argv = _build_run_argv(spec)
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
        if not self._auth_ok():
            return self._send(401, {"ok": False, "error": "token invalide ou absent"})
        if path == "/v1/host/clock":
            return self._send_clock(recu_utc, recu_tai)
        if path == "/v1/capabilities":
            return self._send(200, capabilities_payload())
        if path == "/v1/health":
            return self._send(200, health_payload())
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
        if not self._auth_ok():
            return self._send(401, {"ok": False, "error": "token invalide ou absent"})

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
