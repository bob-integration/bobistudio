# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Helpers transversaux consommés par plusieurs domaines déjà découpés en modules
(node_network.py, plugin_routes/split.py) ET par le reste de app/routes/__init__.py.

Extrait en amont du découpage des gros domaines restants (Câblage, MTL, Plugins/proxy,
Pages, NMOS) pour ne plus dépendre de « là où la fonction se trouve encore » : ces 5
fonctions sont indépendantes les unes des autres (aucune n'appelle une autre du lot),
donc regroupables sans ordre de dépendance particulier."""

import json

from flask import jsonify


def _load_dc(c):
    """Décode `deploy_config` d'un container (JSON stocké en texte). None si absent/invalide."""
    raw = c.get("deploy_config") if c else None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def _mixer_proxy(vmid, path, method="POST", body=None):
    """Forward vers <ip>:8082<path>. Renvoie un tuple (body, status, headers) Flask."""
    from ..addressing import get_container_ip
    import requests as _req
    ip = get_container_ip(vmid)
    if not ip:
        return jsonify({"error": "IP container introuvable"}), 404
    try:
        if method == "GET":
            r = _req.get(f"http://{ip}:8082{path}", timeout=2)
        else:
            r = _req.post(f"http://{ip}:8082{path}", json=body or {}, timeout=2)
        return (r.text, r.status_code, {"Content-Type": "application/json"})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


def _mtl_total_queues(live_xdp=None):
    """Budget total de combined queues AF-XDP de la NIC (E810) — 1 file par SESSION libmtl
    (vidéo/audio/ANC), le coût par source vidéo étant variable (cf. _mtl_per_source_sessions).
    Source : valeur live du contrôleur (:8080 xdp.hw_max_combined) sinon réglage
    `mtl_xdp_total_queues` (défaut 48 = E810-CQDA2). Plafonne les sessions actives (anti-ENOMEM)."""
    try:
        if live_xdp and live_xdp.get("hw_max_combined"):
            return int(live_xdp["hw_max_combined"])
    except Exception:
        pass
    try:
        from .. import settings as _st
        return int(_st.get("mtl_xdp_total_queues") or 48)
    except Exception:
        return 48


def _ptp_apply_core(nid, data):
    """Cœur commun de l'application PTP (persistance par-nœud/global + deploy/start/stop).
    Réutilisé par /api/ptp/apply ET /api/nodes/<id>/io2110. Retourne (ok, msg, http_code).
    B1b-2 : config PTP persistée PAR-NŒUD (override) quand `nid` est fourni ; sinon global."""
    from ..addressing import node_host as _node_host, primary_host as _primary_host
    from ..database import db_set_setting
    from .. import ptp
    def _clamp(val, lo, hi, default):
        try:
            v = int(val)
            return v if lo <= v <= hi else default
        except (TypeError, ValueError):
            return default

    enabled     = bool(data.get("enabled"))
    ifname      = (data.get("ifname") or "").strip()
    domain      = _clamp(data.get("domain"),      0,   127,   127)
    # hw_ts : si absent du payload (ex. Appliquer par-nœud minimal {enabled:true} depuis le récap
    # réseau), défaut = réglage courant du nœud (ptp_hw_ts, défaut True) — capacité de la carte.
    if "hw_ts" in data:
        hw_ts = bool(data.get("hw_ts"))
    else:
        from .. import settings as _st
        hw_ts = bool(_st.setting_for("ptp_hw_ts", nid) if _st.setting_for("ptp_hw_ts", nid) is not None else True)
    priority1   = _clamp(data.get("priority1"),   0,   255,   128)
    priority2   = _clamp(data.get("priority2"),   0,   255,   128)
    log_announce= _clamp(data.get("log_announce"),-3,    4,     0)
    log_sync    = _clamp(data.get("log_sync"),    -7,    0,    -3)
    log_delay_req=_clamp(data.get("log_delay_req"),-7,   0,    -3)
    announce_to = _clamp(data.get("announce_to"),  2,   10,     3)
    delay_thresh= _clamp(data.get("delay_thresh"),100,100000,  800)
    utc_offset  = _clamp(data.get("utc_offset"),   0,  255,    37)
    # client_only : si absent du payload (ex. route io2110), défaut = réglage courant du nœud
    # (ptp_client_only, défaut True) — un nœud média ne doit jamais devenir grandmaster.
    if "client_only" in data:
        client_only = bool(data.get("client_only"))
    else:
        from .. import settings as _st
        client_only = bool(_st.setting_for("ptp_client_only", nid))

    # PTP multi-NIC : si le nœud a des NIC media2110 marquées ptp_enabled (node_interfaces),
    # on pilote en JBOD par domaine (une horloge/clockIdentity par domaine, BMCA unique) ;
    # sinon repli mono sur `ifname`. Le chemin multi reste dormant tant qu'aucune NIC n'est
    # flaggée (UI réseau, Phase C) → comportement mono inchangé pour l'existant.
    groups = ptp.groups_from_node_interfaces(nid) if nid is not None else []
    if enabled and not groups and not ifname:
        return False, "ifname requis pour activer PTP (ou marquer des NIC media2110 ptp_enabled)", 400
    if nid is not None:
        from ..database import db_set_node_setting as _setn
        _persist = lambda k, v: _setn(nid, k, v)
    else:
        _persist = db_set_setting
    _persist("ptp_enabled",      enabled)
    _persist("ptp_ifname",       ifname)
    _persist("ptp_domain",       domain)
    _persist("ptp_hw_ts",        hw_ts)
    _persist("ptp_priority1",    priority1)
    _persist("ptp_priority2",    priority2)
    _persist("ptp_log_announce", log_announce)
    _persist("ptp_log_sync",     log_sync)
    _persist("ptp_log_delay_req",log_delay_req)
    _persist("ptp_announce_to",  announce_to)
    _persist("ptp_delay_thresh", delay_thresh)
    _persist("ptp_utc_offset",   utc_offset)
    _persist("ptp_client_only",  client_only)
    host = _node_host(nid) or _primary_host()
    conf_opts = dict(priority1=priority1, priority2=priority2,
                     log_announce=log_announce, log_sync=log_sync,
                     log_delay_req=log_delay_req, announce_timeout=announce_to,
                     delay_thresh=delay_thresh, utc_offset=utc_offset,
                     client_only=client_only)
    if enabled:
        ptp.install(host)                                    # idempotent (court-circuit si déjà là)
        if groups:
            # Multi-NIC : le profil PTP vient de CHAQUE RÉSEAU (pas des réglages nœud) ; seul hw_ts
            # est node-global. Les réglages profil de cette page ne pilotent que le chemin mono.
            ok, msg = ptp.deploy_config_multi(host, groups, hw_ts=hw_ts)
            if not ok:
                return False, f"deploy_config_multi: {msg}", 500
            ok, msg = ptp.start_multi(host, groups)
        else:
            ok, msg = ptp.deploy_config(host, ifname, domain, hw_ts, **conf_opts)
            if not ok:
                return False, f"deploy_config: {msg}", 500
            ok, msg = ptp.start(host)
        return ok, msg, (200 if ok else 500)
    # Désactivation : en multi, purge complète des unités gérées (sans `groups` → reconcile vers
    # l'ensemble vide, legacy mono compris) ; sinon stop mono.
    if groups:
        ok, msg = ptp.stop_multi(host)
    else:
        ok, msg = ptp.stop(host)
    return ok, msg, 200


# ─── Inventaire NIC d'un hôte (partagé /api/ethernet/status et /api/nodes/<id>/interfaces) ──
_NIC_CAPS_PROBE = r'''
import os, json, re, subprocess
E810 = {"0x1592","0x1593","0x1599","0x159a","0x159b","0x159c","0x1891","0x188a","0x188b","0x188c",
        "0x124c","0x124d","0x124e","0x124f","0x1888"}
# Table interne d'IDs ConnectX (sans dépendre de la base pci.ids du système, souvent absente/
# périmée sur les nœuds → lspci retombe alors sur « Device <hex> » qu'on ne veut pas afficher).
CONNECTX = {"0x1003":"ConnectX-3","0x1007":"ConnectX-3 Pro","0x1013":"ConnectX-4",
            "0x1015":"ConnectX-4 Lx","0x1017":"ConnectX-5","0x1018":"ConnectX-5",
            "0x1019":"ConnectX-5 Ex","0x101a":"ConnectX-5 Ex","0x101b":"ConnectX-6",
            "0x101d":"ConnectX-6 Dx","0x101e":"ConnectX-6 Lx","0x101f":"ConnectX-6 Lx",
            "0x1021":"ConnectX-7","0x1023":"ConnectX-8"}
def _clean(s):
    # Rejette les placeholders lspci (« Device 1015 », « Vendor 15b3 ») = pci.ids absente/périmée.
    s = (s or "").strip()
    if not s or re.match(r"^(Device|Vendor)\s+[0-9a-fA-F]{4}$", s):
        return None
    return s
def sh(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return ""
# Driver ETHERNET → (module IB qui expose le device verbs, famille lisible). Le RoCE n'est pas
# l'apanage de Mellanox : Broadcom NetXtreme-E (bnxt_en → bnxt_re) et Intel (ice → irdma) en font
# aussi. Sans cette table, une carte Broadcom RDMA passait pour une carte sans RDMA.
RDMA_MODS = {
    "mlx5_core": ("mlx5_ib", "RoCE (Mellanox)"),
    "mlx4_en":   ("mlx4_ib", "RoCE (Mellanox CX-3)"),
    "mlx4_core": ("mlx4_ib", "RoCE (Mellanox CX-3)"),
    "ice":       ("irdma",   "irdma (Intel)"),
    "irdma":     ("irdma",   "irdma (Intel)"),
    "i40e":      ("irdma",   "irdma (Intel)"),
    "bnxt_en":   ("bnxt_re", "RoCE (Broadcom)"),
    "qede":      ("qedr",    "RoCE (Marvell)"),
}
_mod_av_cache = {}
def _mod_available(mod):
    """Le module IB est-il présent sur le disque du nœud (modinfo) ? Mémoïsé : une carte bi-port
    interrogerait deux fois le même module."""
    if mod not in _mod_av_cache:
        _mod_av_cache[mod] = bool(sh(["modinfo", "-F", "filename", mod]).strip())
    return _mod_av_cache[mod]
out = {}
base = "/sys/class/net"
try:
    names = sorted(os.listdir(base))
except Exception:
    names = []
for ifn in names:
    p = base + "/" + ifn
    dev = p + "/device"
    d = {"pci": None, "driver": None, "speed_mbps": None, "max_speed_mbps": None, "rdma": False,
         "rdma_kind": None, "model": None, "nic_2110": False, "vendor": None, "device": None,
         "card_id": None, "port_medium": None}
    try: d["pci"] = os.path.basename(os.readlink(dev))
    except Exception: pass
    def rd(x):
        try: return open(dev + "/" + x).read().strip().lower()
        except Exception: return ""
    vendor, device = rd("vendor"), rd("device")
    d["vendor"], d["device"] = vendor or None, device or None
    try: d["driver"] = os.path.basename(os.readlink(dev + "/driver"))
    except Exception: pass
    # `rdma` = un device verbs EXISTE pour cette carte (seule preuve qui compte : sans lui, aucun
    # lien RoCE n'est possible). `rdma_module` = le module noyau qui le fabriquerait pour ce driver
    # — distinguer « carte incapable » de « carte capable, module pas chargé » est ce qui permet
    # d'INSTALLER au lieu de laisser l'utilisateur devant un rôle rdma qui ne marchera jamais.
    d["rdma"] = os.path.isdir(dev + "/infiniband")
    drv = d["driver"] or ""
    mod, kind = RDMA_MODS.get(drv, (None, None))
    d["rdma_module"] = mod
    d["rdma_module_loaded"] = bool(mod) and os.path.isdir("/sys/module/" + mod)
    d["rdma_module_available"] = _mod_available(mod) if mod else False
    if d["rdma"]:
        d["rdma_kind"] = kind or "RDMA"
    elif mod:
        d["rdma_kind"] = kind           # capacité POTENTIELLE — pas de device verbs pour l'instant
    # Vitesse via ethtool : Speed courant (fiable, contrairement à /sys/.../speed sur E810) +
    # MAX réel des « Supported/Advertised link modes » (<N>base..., indépendant de l'état du lien).
    et = sh(["ethtool", ifn])
    if et:
        m = re.search(r"Speed:\s*(\d+)\s*Mb/s", et)
        if m: d["speed_mbps"] = int(m.group(1))
        modes = [int(x) for x in re.findall(r"(\d+)base", et)]
        if modes: d["max_speed_mbps"] = max(modes)
        # Medium du port (sans I2C) → étiquette du bouton : RJ45 (cuivre baseT, non cliquable),
        # DAC (direct attach), FIBRE (optique SFP/QSFP, cliquable pour le détail ethtool -m).
        mp = re.search(r"(?m)^\s*Port:\s*(.+?)\s*$", et)
        ms = re.search(r"Supported ports:\s*\[(.*?)\]", et)
        port = (mp.group(1).lower() if mp else "")
        sup  = (ms.group(1).lower() if ms else "")
        if "twisted pair" in port or " tp" in (" " + sup):
            d["port_medium"] = "rj45"
        elif "direct attach" in port:
            d["port_medium"] = "dac"
        elif "fibre" in port or "fibre" in sup:
            d["port_medium"] = "fibre"
        elif "backplane" in port or "backplane" in sup:
            d["port_medium"] = "backplane"
    if d["speed_mbps"] is None:                        # repli /sys (négociée) si ethtool muet
        try:
            sp = int(open(p + "/speed").read().strip())
            if sp > 0: d["speed_mbps"] = sp
        except Exception: pass
    if d["max_speed_mbps"] is None:
        d["max_speed_mbps"] = d["speed_mbps"]
    # Modèle complet via lspci : on préfère le SOUS-SYSTÈME (SDevice = réf. carte OEM, ex.
    # « Ethernet Network Adapter E810-XXVDA4 ») au device générique.
    model = None
    if d["pci"]:
        sdev = ddev = None
        for line in sh(["lspci", "-vmm", "-s", d["pci"]]).splitlines():
            if line.startswith("SDevice:"): sdev = _clean(line.split(":", 1)[1])
            elif line.startswith("Device:"): ddev = _clean(line.split(":", 1)[1])
        model = sdev or ddev                                   # nom résolu (pci.ids présente)
    if vendor == "0x8086" and device in E810:
        d["nic_2110"] = True                                   # E810 (ice) = MTL/DPDK
        model = model or "Intel E810"
    elif vendor == "0x15b3":
        # ConnectX-4+ (mlx5) = MTL/DPDK ; ConnectX-3 (mlx4) NON (incompatible DPDK/MTL).
        d["nic_2110"] = (d["driver"] or "").startswith("mlx5")
        # Repli table interne (pci.ids absente) → « Mellanox ConnectX-<gen> » plutôt que « Device <hex> ».
        model = model or ("Mellanox " + CONNECTX[device] if device in CONNECTX else "Mellanox ConnectX")
    d["model"] = model
    if d["pci"]:
        d["card_id"] = d["pci"].rsplit(".", 1)[0]
    out[ifn] = d
# NIC bindées vfio-pci (moteur DPDK) : plus AUCUN netdev kernel → invisibles de /sys/class/net.
# On les énumère via le driver vfio-pci et on ne garde que les périphériques RÉSEAU (classe PCI
# 0x02xxxx) Intel (0x8086) / Mellanox (0x15b3), pour qu'une carte 2110 en DPDK reste affichable.
vfio = []
vdrv = "/sys/bus/pci/drivers/vfio-pci"
try:
    vbdfs = [x for x in os.listdir(vdrv)
             if re.match(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$", x)]
except Exception:
    vbdfs = []
for bdf in vbdfs:
    dd = "/sys/bus/pci/devices/" + bdf
    def rdv(x):
        try: return open(dd + "/" + x).read().strip().lower()
        except Exception: return ""
    if not rdv("class").startswith("0x02"):     # 0x02 = contrôleur réseau (Ethernet)
        continue
    vendor, device = rdv("vendor"), rdv("device")
    if vendor not in ("0x8086", "0x15b3"):
        continue
    sdev = ddev = None
    for line in sh(["lspci", "-vmm", "-s", bdf]).splitlines():
        if line.startswith("SDevice:"): sdev = _clean(line.split(":", 1)[1])
        elif line.startswith("Device:"): ddev = _clean(line.split(":", 1)[1])
    model = sdev or ddev
    nic_2110 = False
    if vendor == "0x8086" and device in E810:
        nic_2110 = True; model = model or "Intel E810"
    elif vendor == "0x15b3":
        nic_2110 = True                          # ConnectX bindée vfio = utilisée pour DPDK
        model = model or ("Mellanox " + CONNECTX[device] if device in CONNECTX else "Mellanox ConnectX")
    vfio.append({"pci": bdf, "driver": "vfio-pci", "model": model, "nic_2110": nic_2110,
                 "vendor": vendor, "device": device, "card_id": bdf.rsplit(".", 1)[0]})
out["__vfio__"] = vfio
print(json.dumps(out))
'''

def _fetch_host_nics(host):
    """Inventaire live des NICs d'un hôte via SSH (ip -s link + ip addr + sonde /sys capacités).
    Retourne (ok, error, nics). Partagé par /api/ethernet/status et /api/nodes/<id>/interfaces."""
    from .. import settings as st
    from ..host_ops import ssh_run
    # Sonde best-effort (`;` + `|| true`) : si python3 manque, l'échec ne doit pas masquer
    # l'inventaire `ip`. rc≠0 ici = échec du transport SSH lui-même → on remonte l'erreur.
    cmd = ("ip -j -s link 2>/dev/null; echo '---SEP---'; ip -j addr show 2>/dev/null; "
           "echo '---SEP2---'; python3 - 2>/dev/null <<'PYEOF' || true\n" + _NIC_CAPS_PROBE + "\nPYEOF\n")
    rc, out, err = ssh_run(host, cmd, timeout=15)
    if rc != 0:
        return False, (err.strip() or out.strip()), []
    caps = {}
    body = out
    if "---SEP2---" in body:
        body, caps_json = body.split("---SEP2---", 1)
        try:
            caps = json.loads(caps_json.strip())
        except Exception:
            caps = {}
    try:
        link_json, addr_json = body.split("---SEP---", 1)
        links = json.loads(link_json)
        addrs = json.loads(addr_json)
    except Exception as e:
        return False, f"parse ip JSON: {e}", []
    addr_by = {a.get("ifname"): a for a in addrs}
    sriov_pf = st.get("nmos_2110_pf") or ""
    nics = []
    for l in links:
        name = l.get("ifname") or ""
        if name in ("lo",) or name.startswith(("veth", "fwbr", "fwln", "fwpr",
                                                "tap", "docker", "br-")):
            continue
        a = addr_by.get(name) or {}
        ipv4 = []
        for ai in a.get("addr_info") or []:
            if ai.get("family") == "inet":
                ipv4.append({"local": ai.get("local"), "prefix": ai.get("prefixlen")})
        stats64 = l.get("stats64") or {}
        rx = (stats64.get("rx") or {}).get("bytes")
        tx = (stats64.get("tx") or {}).get("bytes")
        is_pf = bool(sriov_pf and name == sriov_pf)
        is_vf = bool(sriov_pf and name.startswith(sriov_pf + "v"))
        cp = caps.get(name) or {}
        nics.append({
            "name":      name,
            "mac":       l.get("address"),
            "link":      "UP" in (l.get("flags") or []),
            "operstate": l.get("operstate"),
            "mtu":       l.get("mtu"),
            "kind":      l.get("link_type"),
            "ipv4":      ipv4,
            "rx_bytes":  rx,
            "tx_bytes":  tx,
            "is_pf":     is_pf,
            "is_vf":     is_vf,
            # Capacités matérielles (sonde /sys) : pour le groupement par carte + badges.
            "pci":            cp.get("pci"),
            "card_id":        cp.get("card_id"),
            "driver":         cp.get("driver"),
            "speed_mbps":     cp.get("speed_mbps"),
            "max_speed_mbps": cp.get("max_speed_mbps"),
            "rdma":           bool(cp.get("rdma")),
            "rdma_kind":      cp.get("rdma_kind"),
            # Capacité POTENTIELLE : module IB attendu pour ce driver, chargé ?, disponible ?
            "rdma_module":           cp.get("rdma_module"),
            "rdma_module_loaded":    bool(cp.get("rdma_module_loaded")),
            "rdma_module_available": bool(cp.get("rdma_module_available")),
            "model":          cp.get("model"),
            "nic_2110":       bool(cp.get("nic_2110")),
            "port_medium":    cp.get("port_medium"),
        })
    # NIC bindées vfio-pci (moteur DPDK) : pas de netdev → entrées synthétiques (name=None), keyées
    # sur le BDF. Restent affichables/configurables via leur ligne node_interfaces (matchée sur `pci`).
    for vf in (caps.get("__vfio__") or []):
        nics.append({
            "name": None, "mac": None, "link": False, "operstate": None, "mtu": None,
            "kind": None, "ipv4": [], "rx_bytes": None, "tx_bytes": None,
            "is_pf": False, "is_vf": False,
            "pci":            vf.get("pci"),
            "card_id":        vf.get("card_id"),
            "driver":         "vfio-pci",
            "speed_mbps":     None, "max_speed_mbps": None,
            "rdma":           False, "rdma_kind": None,
            "model":          vf.get("model"),
            "nic_2110":       bool(vf.get("nic_2110")),
            "port_medium":    None,
            "vfio_bound":     True,
        })
    return True, None, nics
