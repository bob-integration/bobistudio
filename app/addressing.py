# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Résolution d'adresse d'un conteneur (cross-backend, full-Docker).

Relocalisé depuis l'ex-`proxmox.py` (D Phase 2a) : `get_container_ip` est le point unique par lequel
deploy/metrics/routes/monitor/mtl/projects/NMOS atteignent un conteneur (métriques :8080, agent :8081,
SDP IS-05). Plus aucun appel Proxmox — l'orchestrateur est full-Docker.
"""
import logging

log = logging.getLogger(__name__)


def controller_ipv4s():
    """IPv4 (sans masque) de l'hôte de l'ORCHESTRATEUR (lecture LOCALE, pas ssh_run). Sert à vérifier
    qu'il a bien une patte sur le subnet conteneurs — sinon il ne peut pas joindre les conteneurs
    macvlan (:8080/:8081/:8082). En fusionné, c'est son IP de contrôle ; en séparé, sa patte
    conteneurs (provisionnée par l'opérateur). Aussi : IP à EXCLURE du pool d'allocation conteneurs."""
    import subprocess as _sp
    out = []
    try:
        r = _sp.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=5)
        for ln in (r.stdout or "").splitlines():
            parts = ln.split()
            if len(parts) > 1 and parts[1] != "lo" and "inet" in parts:
                out.append(parts[parts.index("inet") + 1].split("/")[0])
    except Exception:
        pass
    return out


def controller_on_subnet(subnet):
    """Vrai si une IP locale de l'orchestrateur ∈ `subnet` (CIDR) ; None si subnet vide/invalide."""
    import ipaddress as _ip
    subnet = (subnet or "").strip()
    if not subnet:
        return None
    try:
        net = _ip.ip_network(subnet, strict=False)
    except Exception:
        return None
    for ip in controller_ipv4s():
        try:
            if _ip.ip_address(ip) in net:
                return True
        except Exception:
            continue
    return False


def controller_route_to(ip):
    """Comment l'orchestrateur atteindrait `ip` (via `ip route get`) : {has_route, direct, via, dev}.
    `direct` = sur le même lien (pas de passerelle) ; `via` = routé par une passerelle (L3). Permet de
    valider la joignabilité du plan conteneurs même sans IP directe sur le subnet (routage présent)."""
    import subprocess as _sp
    res = {"has_route": False, "direct": False, "via": None, "dev": None}
    ip = (ip or "").strip()
    if not ip:
        return res
    try:
        r = _sp.run(["ip", "route", "get", ip], capture_output=True, text=True, timeout=4)
        out = (r.stdout or "").strip()
    except Exception:
        return res
    if not out or "unreachable" in out:
        return res
    toks = out.split()
    res["has_route"] = True
    if "via" in toks:
        res["via"] = toks[toks.index("via") + 1]
    else:
        res["direct"] = True
    if "dev" in toks:
        res["dev"] = toks[toks.index("dev") + 1]
    return res


def ping(ip, timeout=1):
    """Ping ICMP unique depuis l'orchestrateur (best-effort ; l'ICMP peut être filtré → faux négatif)."""
    import subprocess as _sp
    ip = (ip or "").strip()
    if not ip:
        return False
    try:
        r = _sp.run(["ping", "-c", "1", "-W", str(int(timeout)), ip],
                    capture_output=True, timeout=int(timeout) + 2)
        return r.returncode == 0
    except Exception:
        return False


def tcp_reachable(ip, port, timeout=2):
    """Connexion TCP de test depuis l'orchestrateur (preuve de joignabilité réelle d'un conteneur)."""
    import socket as _sock
    ip = (ip or "").strip()
    if not ip:
        return False
    try:
        with _sock.create_connection((ip, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def get_container_ip(vmid):
    """IP joignable d'un conteneur.
    - Docker « compute » (macvlan/ipvlan) : IP propre du conteneur (`docker_ip`).
    - Docker MTL (`--network host`) : IP = hôte du nœud.
    - Sinon (legacy/inconnu) : None (plus d'interrogation Proxmox)."""
    from .database import db_get_container, db_get_node
    _c = db_get_container(vmid)
    if _c:
        from .docker_compute import is_compute_container
        if is_compute_container(_c):
            return _c.get("docker_ip")
        _n = db_get_node(_c.get("node_id")) if _c.get("node_id") else None
        return (_n or {}).get("host")
    return None


# ─── Résolution de l'HÔTE SSH d'un nœud (host-ops : VF SR-IOV, PTP, prép MTL, binds) ──────────
# B1a : remplace le réglage GLOBAL `proxmox_host` par l'hôte DU NŒUD concerné. À 1 nœud, identique
# (proxmox_host == l'unique node.host) ; habilite le multi-nœud. `primary_host()` = repli de
# transition tant que l'op n'a pas de nœud explicite (l'UI par-nœud arrive en B1b).

def node_host(node_id):
    """Hôte SSH d'un nœud (table nodes), ou None."""
    if not node_id:
        return None
    from .database import db_get_node
    return (db_get_node(node_id) or {}).get("host")


def primary_host():
    """Hôte d'host-ops par défaut : l'unique nœud s'il n'y en a qu'un, sinon None (multi-nœud → le
    nœud doit être choisi explicitement ; 0 nœud sur le dev local → pas d'host-ops). B1b-2 : le repli
    `proxmox_host` a été RETIRÉ — la table `nodes` est la seule source de vérité des hôtes."""
    from .database import db_get_nodes
    nodes = db_get_nodes() or []
    if len(nodes) == 1 and nodes[0].get("host"):
        return nodes[0]["host"]
    return None


def host_for_vmid(vmid):
    """Hôte SSH du nœud qui héberge ce conteneur (release VF, scoping…). Repli primary_host."""
    from .database import db_get_container
    c = db_get_container(vmid) or {}
    return node_host(c.get("node_id")) or primary_host()
