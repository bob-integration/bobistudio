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


# ─── Adresse de l'hôte d'un nœud VUE DU SITE (navigateurs, pupitres, encodeurs) ───────────────
# ★ UN NŒUD PEUT ÊTRE ENRÔLÉ SOUS UNE BOUCLE LOCALE (2026-10-28). Sur un tout-en-un, `nodes.host`
# vaut couramment `127.0.0.1` : c'est l'adresse par laquelle l'ORCHESTRATEUR le joint, et elle est
# juste pour lui. Envoyée à un navigateur, elle désigne le poste du visiteur — donc un panneau noir
# sans erreur ni alerte, exactement le symptôme que `adresse_navigateur` existe pour éviter.
# Le mode « réseau interne » rend ce cas obligatoire : l'IP du conteneur étant privée au nœud,
# TOUTE URL passe par l'hôte. Relevé sur la Z8 (moniteur WebRTC invisible depuis le LAN).

_NOMS_BOUCLE = ("localhost", "localhost.localdomain", "ip6-localhost")
_ADRESSES_NULLES = ("0.0.0.0", "::", "::0")


def adresse_locale_seulement(host):
    """`host` ne désigne-t-il RIEN pour un navigateur distant ? (boucle locale ou adresse nulle)

    Les trois formes se rencontrent pour de vrai : `127.0.0.1` (enrôlement tout-en-un),
    `localhost` (saisie à la main), `::1` (résolution IPv6). `0.0.0.0` n'est pas une boucle
    mais une adresse d'ÉCOUTE : la joindre n'a pas plus de sens, on la traite pareil."""
    import ipaddress as _ip
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False                      # pas d'adresse du tout : ce n'est pas notre affaire
    if h in _NOMS_BOUCLE or h in _ADRESSES_NULLES:
        return True
    try:
        return _ip.ip_address(h).is_loopback
    except ValueError:
        return False                      # un nom d'hôte quelconque : on ne devine pas


_CACHE_LAN = {"t": 0.0, "ip": None}
_TTL_CACHE_LAN = 30.0


def ip_lan_controleur(cible="1.1.1.1"):
    """IP SOURCE que l'hôte de l'orchestrateur emploie pour sortir vers le site (`ip route get`).

    C'est une MESURE, pas une déduction : on demande au noyau par quelle adresse il parlerait au
    monde. Aucun paquet n'est émis, la machine peut être hors ligne — seule la table de routage
    est lue. Deux refus explicites :
    - une source sur une interface QUE NOUS AVONS CRÉÉE (`bobi-*`, p.ex. le shim macvlan en /32)
      n'est pas une adresse du nœud et ne doit pas sortir d'ici ;
    - une source de boucle locale (pas de route par défaut) ne vaut pas mieux que ce qu'on
      cherche à remplacer.
    Rend None quand la mesure n'aboutit pas : on ne fabrique pas une adresse."""
    import subprocess as _sp
    import time as _t
    now = _t.monotonic()
    if _CACHE_LAN["ip"] is not None and (now - _CACHE_LAN["t"]) < _TTL_CACHE_LAN:
        return _CACHE_LAN["ip"]
    ip = None
    try:
        r = _sp.run(["ip", "route", "get", cible], capture_output=True, text=True, timeout=4)
        toks = (r.stdout or "").split()
        if "src" in toks:
            cand = toks[toks.index("src") + 1]
            dev = toks[toks.index("dev") + 1] if "dev" in toks else ""
            from .container_net import interface_a_nous
            if not interface_a_nous(dev) and not adresse_locale_seulement(cand):
                ip = cand
    except Exception:
        ip = None
    if ip:
        _CACHE_LAN.update({"t": now, "ip": ip})
    return ip


def _ip_role_management(node_id):
    """IP déclarée de la carte de MANAGEMENT du nœud (repli de dernier recours, données déclarées).

    ⚠ On écarte les interfaces `bobi-*` : le shim macvlan porte une adresse, parfois même un rôle
    posé par erreur, et elle n'est joignable de personne (du /32 dans le subnet des conteneurs)."""
    from .database import db_get_node_interfaces, role_is_management
    from .container_net import interface_a_nous
    for it in (db_get_node_interfaces(node_id) or []):
        if not role_is_management(it.get("role")) or interface_a_nous(it.get("ifname")):
            continue
        cidr = (it.get("ip_cidr") or "").strip().split("/")[0]
        if cidr and not adresse_locale_seulement(cidr):
            return cidr
    return None


def resolution_adresse_navigateur(node):
    """{adresse, source, host} : par quelle adresse le SITE joint l'hôte de ce nœud, et d'où on la tient.

    `source` ∈ `override` (réglage `browser_host`, par nœud) | `host` (`nodes.host`, le cas normal)
    | `mesure` (route par défaut de l'hôte de l'orchestrateur) | `role` (carte management déclarée)
    | None (on n'a pas su : l'appelant garde ce qu'il avait, il ne reçoit pas une invention).

    L'override passe AVANT `host` : il sert aussi quand `host` est routable pour l'orchestrateur
    mais pas pour le site (NAT, nom publié, second pare-feu).

    La mesure n'est consultée que pour une boucle locale, et c'est ce qui la rend légitime : un
    nœud que l'orchestrateur joint en `127.0.0.1` EST la machine de l'orchestrateur, donc lire la
    route par défaut locale, c'est mesurer la bonne machine. Sur un nœud distant, ce serait
    l'adresse du contrôleur — un faux aplomb. D'où le gate, et non un repli général.
    """
    node = node or {}
    host = (node.get("host") or "").strip()
    nid = node.get("id")
    if nid is not None:
        from . import settings as _st
        ov = (_st.setting_for("browser_host", nid) or "").strip()
        if ov:
            return {"adresse": ov, "source": "override", "host": host}
    if host and not adresse_locale_seulement(host):
        return {"adresse": host, "source": "host", "host": host}
    mesure = ip_lan_controleur()
    if mesure:
        return {"adresse": mesure, "source": "mesure", "host": host}
    role = _ip_role_management(nid) if nid is not None else None
    if role:
        return {"adresse": role, "source": "role", "host": host}
    log.warning("adresse navigateur du nœud %s : host=%r est une boucle locale et aucune IP de site "
                "n'a pu être mesurée — poser le réglage browser_host sur ce nœud", nid, host)
    return {"adresse": None, "source": None, "host": host}


def adresse_navigateur(ip_conteneur, vmid=None):
    """Adresse par laquelle un NAVIGATEUR — ou un équipement du site — joint un conteneur.

    ★ LE COROLLAIRE SILENCIEUX DU « RÉSEAU INTERNE » (2026-10-27). En mode interne, l'IP d'un
    conteneur est privée au nœud. `get_container_ip` reste juste pour l'ORCHESTRATEUR, qui est
    sur la même machine — mais une URL construite avec cette IP et envoyée à un navigateur
    pointe une adresse qui n'existe pas pour lui. Le symptôme : le panneau de monitoring ou la
    page de lecture reste NOIRE, sans erreur, sans alerte, avec une passerelle WebRTC qui va
    parfaitement bien et un flux qui arrive. Publier le port sans corriger l'URL n'aurait servi
    à rien du tout.

    En mode interne, l'adresse est donc celle de l'HÔTE du nœud, où le port publié aboutit
    (cf. `container_net.ports_a_publier` : le port hôte est le même que celui du conteneur,
    précisément pour que l'URL ne change que d'adresse, jamais de port).

    Sans `vmid`, on rend l'adresse telle quelle : on ne DEVINE pas de quel nœud il s'agit —
    une mauvaise substitution serait pire que l'absence de substitution.
    """
    if not ip_conteneur or not vmid:
        return ip_conteneur
    from .database import db_get_container, db_get_node, node_net_interne
    c = db_get_container(vmid) or {}
    n = db_get_node(c.get("node_id")) if c.get("node_id") else None
    if n and node_net_interne(n):
        # Pas `n["host"]` tel quel : il vaut souvent `127.0.0.1` sur un tout-en-un, et une URL
        # bâtie dessus envoie le navigateur sur LUI-MÊME (cf. resolution_adresse_navigateur).
        return resolution_adresse_navigateur(n)["adresse"] or ip_conteneur
    return ip_conteneur


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
