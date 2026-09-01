# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Calcul des VMIDs et IPs libres en croisant settings, DB locale et liste LXC Proxmox.
"""
import ipaddress
import json
import logging
from . import settings
from .database import db_get_containers

log = logging.getLogger(__name__)


def _node_ct_iface(node_id):
    """Carte containers/mgmt_containers du nœud portant une PLAGE PERSONNALISÉE complète
    (ct_ip_start ET ct_ip_end). None si aucune → le nœud alloue dans la plage cluster
    (comportement historique, zéro régression pour les nœuds non configurés)."""
    if not node_id:
        return None
    try:
        from .database import db_get_node_interfaces, role_is_containers
        for r in db_get_node_interfaces(node_id):
            if (role_is_containers(r.get("role"))
                    and (r.get("ct_ip_start") or "").strip()
                    and (r.get("ct_ip_end") or "").strip()):
                return r
    except Exception as e:
        log.warning(f"_node_ct_iface({node_id}): {e}")
    return None


def node_ip_bounds(node_id=None):
    """(start, end, scope) de la plage d'allocation EFFECTIVE d'un nœud : sa plage personnalisée
    (scope='node') si sa carte containers/mgmt_containers en porte une, sinon la plage cluster
    ip_start/ip_end (scope='cluster'). node_id=None → cluster."""
    it = _node_ct_iface(node_id)
    if it:
        return (it["ct_ip_start"].strip(), it["ct_ip_end"].strip(), "node")
    return (settings.get("ip_start"), settings.get("ip_end"), "cluster")


def _range_list(start, end):
    """Liste ordonnée des IPs de start..end ([] si bornes invalides)."""
    try:
        a = int(ipaddress.IPv4Address(start))
        b = int(ipaddress.IPv4Address(end))
    except Exception as e:
        log.warning(f"Plage IP invalide ({start} → {end}): {e}")
        return []
    if a > b:
        a, b = b, a
    return [str(ipaddress.IPv4Address(i)) for i in range(a, b + 1)]


def _ip_range(node_id=None):
    """Liste ordonnée des IPs de la plage effective du nœud (plage cluster si node_id=None ou
    sans plage personnalisée). L'occupation est comptée PAR PLAGE : deux nœuds sur des subnets
    différents ne se bloquent pas mutuellement ; deux nœuds partageant la plage cluster
    continuent de se voir (les IP prises sont globales, l'intersection avec la plage suffit)."""
    start, end, _scope = node_ip_bounds(node_id)
    return _range_list(start, end)


def _used_vmids():
    """VMIDs utilisés — DB locale UNIQUEMENT (full-Docker, D Phase 1). Le vmid est un handle local
    alloué de façon monotone ; plus de cross-check Proxmox `list_lxc` (l'orchestrateur ne contacte
    plus Proxmox)."""
    return {c.get("vmid") for c in db_get_containers() if c.get("vmid") is not None}


def _used_ips(exclude_vmid=None):
    """IPs du plan conteneurs déjà prises (DB) : `ip` (legacy) ET `docker_ip` (plan conteneurs
    macvlan, B2-2). `exclude_vmid` : ignore les IP de ce conteneur (idempotence au redeploy)."""
    used = set()
    for c in db_get_containers():
        if exclude_vmid is not None and c.get("vmid") == exclude_vmid:
            continue
        for k in ("ip", "docker_ip"):
            if c.get(k):
                used.add(c[k])
    return used


def _reserved_ips(node_id=None):
    """IPs à NE JAMAIS allouer à un conteneur (en plus des IP de conteneurs déjà prises) :
    la **passerelle**, les **IP de l'orchestrateur** (sa patte sur le subnet conteneurs ne doit pas
    être réallouée), et les **`host` des nœuds** (IP de contrôle, sur le même subnet en fusionné).
    Si le nœud a une plage personnalisée : + la passerelle et l'IP de contrôle de SA carte
    containers/mgmt_containers (le rôle combiné porte l'IP de contrôle sur le même subnet)."""
    from .database import db_get_nodes
    from . import addressing
    res = set()
    gw = (settings.get("gateway") or "").strip()
    if gw:
        res.add(gw)
    it = _node_ct_iface(node_id)
    if it:
        if (it.get("gateway") or "").strip():
            res.add(it["gateway"].strip())
        if (it.get("ip_cidr") or "").strip():
            res.add(it["ip_cidr"].strip().split("/")[0])
    try:
        res.update(addressing.controller_ipv4s())
    except Exception:
        pass
    for n in (db_get_nodes() or []):
        h = (n.get("host") or "").strip()
        if h:
            res.add(h)
    return res


def centralized_ipam(node_id=None):
    """B2-2 : l'orchestrateur alloue lui-même l'IP du plan conteneurs (au lieu de l'IPAM Docker
    par-nœud) dès que c'est requis : topologie SÉPARÉE (VLAN privé dédié), OU multi-nœud (l'IPAM
    Docker par-nœud collisionne sur un VLAN partagé), OU plage statique explicite configurée
    (cluster OU personnalisée du nœud — dans les deux cas, l'orchestrateur impose l'IP depuis
    la plage DU nœud, cf. _ip_range(node_id)).

    Ce 3ᵉ cas (plage explicite, même mono-nœud) : quand l'opérateur a réglé net_mode=static + une
    plage ip_start/ip_end valide, il DÉCLARE vouloir que l'orchestrateur attribue depuis CE bloc.
    Sans ça, l'IPAM Docker pioche les IP basses du subnet (.1/.2/.3…) et collisionne avec le
    matériel LAN existant sur un VLAN partagé (paquets routés vers le mauvais équipement → timeout).
    Honorer la plage évite la collision."""
    from .database import db_get_nodes
    if settings.get("net_topology") == "separated":
        return True
    if _node_ct_iface(node_id):
        return True
    if settings.get("net_mode") == "static" and len(_range_list(
            settings.get("ip_start"), settings.get("ip_end"))) > 0:
        return True
    return len(db_get_nodes() or []) > 1


def allocate_container_ip(vmid, node_id=None):
    """IP du plan conteneurs pour ce conteneur, dans la plage EFFECTIVE de son nœud (plage
    personnalisée de la carte containers/mgmt_containers si posée, sinon plage cluster
    `ip_start..ip_end`). `node_id` : passé par l'appelant (docker_compute) ; à défaut, relu du
    conteneur. STICKY : si le conteneur a déjà
    une IP (docker_ip) dans la plage et libre, on la CONSERVE (un redeploy ne change pas l'IP d'un
    conteneur en service → le contrôleur ne perd pas sa cible). Sinon 1ʳᵉ libre. None si épuisée.

    ATOMIQUE : l'IP retenue est RÉSERVÉE en base (ip_reservations, PK sur ip) — deux allocations
    concurrentes ne peuvent plus obtenir la même IP macvlan (le perdant sur l'INSERT retente l'IP
    suivante). La réservation est transitoire : elle est levée dès que `db_update_docker_ip` persiste
    l'IP (containers.docker_ip prend alors le relais comme source de vérité), et à la destruction du
    conteneur (db_delete_container). Voir database.db_reserve_ip."""
    from .database import (db_get_container, db_add_alert, db_reserve_ip,
                           db_used_ip_reservations, db_release_ip_reservations_for_vmid)
    c = db_get_container(vmid) or {}
    if node_id is None:
        node_id = c.get("node_id")
    rng = _ip_range(node_id)
    # Redeploy du même vmid : on relâche d'abord SES propres réservations, sinon on se bloquerait
    # nous-même (idempotence — le container ré-obtient son IP collante ou une nouvelle).
    db_release_ip_reservations_for_vmid(vmid)
    blocked = (_used_ips(exclude_vmid=vmid) | _reserved_ips(node_id)   # conteneurs pris + passerelle/orchestrateur/nœuds
               | db_used_ip_reservations())                     # + IP réservées par une alloc concurrente
    cur = c.get("docker_ip")
    if cur and cur in rng and cur not in blocked:
        # STICKY : docker_ip existant fait déjà autorité (présent dans _used_ips) → inutile de réserver.
        return cur
    for ip in rng:
        if ip in blocked:
            continue
        if db_reserve_ip(ip, vmid):   # INSERT atomique : tranche la course, le perdant continue
            return ip
    _s, _e, _scope = node_ip_bounds(node_id)
    db_add_alert("alert.net.ipam_plan_epuise", "error", vmid=vmid, kind="net",
                 params={"vmid": vmid, "scope": _scope, "s": _s, "e": _e})
    return None


def vmid_stats():
    # Allocation monotone illimitée → plus de notion de « libres / total ». On expose le plancher,
    # le prochain vmid et le nombre utilisés (≥ plancher).
    floor = int(settings.get("vmid_start"))
    used  = _used_vmids()
    above = sorted(v for v in used if isinstance(v, int) and v >= floor)
    nxt   = (above[-1] if above else floor - 1) + 1
    return {"start": floor, "next": nxt, "used_count": len(above), "used": above}


def ip_stats():
    """Stats du plan conteneurs, VENTILÉES PAR PLAGE : la plage cluster + une entrée par nœud
    portant une plage personnalisée (carte containers/mgmt_containers). Les clés historiques
    (start/end/total/free/used) restent celles de la plage cluster (compat UI)."""
    if settings.get("net_mode") == "dhcp":
        return {"mode": "dhcp", "total": 0, "free": 0, "used": [], "ranges": []}
    from .database import db_get_nodes
    used_all = _used_ips()

    def _entry(start, end, scope, label, **extra):
        rng = _range_list(start, end)
        free = [ip for ip in rng if ip not in used_all]
        e = {"scope": scope, "label": label, "start": start, "end": end,
             "total": len(rng), "free": len(free), "used": sorted(used_all & set(rng))}
        e.update(extra)
        return e

    cluster = _entry(settings.get("ip_start"), settings.get("ip_end"), "cluster", "cluster")
    ranges = [cluster]
    for n in (db_get_nodes() or []):
        it = _node_ct_iface(n.get("id"))
        if it:
            ranges.append(_entry(it["ct_ip_start"].strip(), it["ct_ip_end"].strip(), "node",
                                 n.get("name") or f"nœud {n['id']}",
                                 node_id=n["id"], ifname=it.get("ifname")))
    return {"mode": "static", "start": cluster["start"], "end": cluster["end"],
            "total": cluster["total"], "free": cluster["free"], "used": cluster["used"],
            "ranges": ranges}


def next_free_vmid():
    # Handle LOCAL : allocation MONOTONE illimitée (plus de plafond `vmid_end`). next = max(utilisés
    # ≥ plancher) + 1 → jamais d'épuisement, numéros croissants (audit clair, pas de réutilisation de
    # trous). Le cross-check Proxmox (si configuré) garantit l'absence de collision avec un LXC réel.
    #
    # ATOMIQUE : le vmid retenu est RÉSERVÉ en base (vmid_reservations, PK sur vmid) — deux créations
    # concurrentes ne peuvent plus obtenir le même numéro (le perdant sur l'INSERT incrémente et
    # retente). Le vmid étant monotone/jamais réutilisé, la réservation reste comme marqueur permanent
    # (élaguée seulement une fois le container réellement présent, cf. db_prune_consumed_vmid_reservations).
    from .database import (db_add_alert, db_reserve_vmid, db_used_vmid_reservations,
                           db_prune_consumed_vmid_reservations)
    db_prune_consumed_vmid_reservations()             # allège la table (réservations déjà consommées)
    floor = int(settings.get("vmid_start"))
    used  = _used_vmids() | db_used_vmid_reservations()   # containers réels + réservations en vol
    above = [v for v in used if isinstance(v, int) and v >= floor]
    nxt   = (max(above) if above else floor - 1) + 1
    for _ in range(100_000):          # borne anti-boucle (course extrême) ; en pratique 1 tour
        if nxt > floor + 1_000_000:   # garde-fou absurde (jamais atteint en pratique)
            msg = f"Allocation VMID anormale (next={nxt} > plancher+1e6) — vérifier la DB."
            log.error(msg)
            db_add_alert("alert.deploy.vmid_anormal", "error", kind="deploy", params={"nxt": nxt})
            return None
        if db_reserve_vmid(nxt):      # INSERT atomique : tranche la course
            return nxt
        nxt += 1                      # perdu la course sur ce numéro → suivant
    log.error("next_free_vmid: impossible de réserver un vmid après 100000 tentatives")
    return None


# ─── B2-3 : multicast cluster-unique (pool alloué dans le registre NMOS) ──────────────────────
def _registry_transports():
    """(resource_id, transport) du registre NMOS — source de vérité cluster du multicast."""
    try:
        from .database import db_nmos_resources
        return [(r["id"], r.get("transport") or {}) for r in db_nmos_resources()]
    except Exception:
        return []


def _mcast_keys(tr):
    """Clés 'ip:port' occupées par un transport (leg0 + leg1 2022-7)."""
    out = []
    ip0, p0 = tr.get("multicast_ip"), tr.get("port")
    if ip0:
        out.append(f"{ip0}:{int(p0) if p0 else ''}")
    ip1, p1 = tr.get("multicast_ip_leg1"), tr.get("dest_port_leg1")
    if ip1:
        out.append(f"{ip1}:{int(p1) if p1 else ''}")
    return out


def _used_multicasts(transports=None):
    """Ensemble des 'ip:port' déjà pris : registre NMOS (ressources confirmées/enregistrées) UNION
    ledger de réservation (mcast_allocations — connaît une adresse dès l'INSTANT où elle a été
    réservée, AVANT même que la ressource NMOS correspondante ne soit enregistrée). Le registre seul
    ne suffit pas : il n'est mis à jour qu'après coup (notify_state_change), ce qui laisse une
    fenêtre de course entre deux allocations concurrentes — d'où le ledger, réservé atomiquement."""
    from .database import db_used_mcast_allocations
    used = set()
    for _rid, tr in (_registry_transports() if transports is None else transports):
        used.update(_mcast_keys(tr))
    used.update(db_used_mcast_allocations())
    return used


def _reuse_owner_reservation(owner_ref, port, in_range=None):
    """ANTI-FUITE : réutilise la réservation existante de cet owner_ref si elle vise le même port
    (et reste dans la plage stricte `in_range=(base_int, size)` si fournie) ; sinon la libère
    avant de laisser l'appelant réallouer. Retourne (ip, port) ou None.

    Contexte : le hook before_deploy réserve AVANT que le deploy ne persiste les params dans
    deploy_config ; un deploy qui échouait ensuite (image absente, agent injoignable…) relançait
    au retry une allocation NEUVE pour le même owner_ref — owner_ref n'étant pas PK, chaque échec
    empilait une adresse orpheline « occupée » à jamais, jusqu'à épuisement du pool/de la plage."""
    from .database import db_mcast_for_owner, db_release_mcast_owner
    existing = db_mcast_for_owner(owner_ref)
    if not existing:
        return None
    eip, eport = existing
    ok = int(eport) == int(port)
    if ok and in_range is not None:
        base_int, size = in_range
        try:
            off = int(ipaddress.IPv4Address(eip)) - base_int
            ok = 1 <= off < size
        except Exception:
            ok = False
    if ok:
        return (eip, int(port))
    db_release_mcast_owner(owner_ref)   # port/plage changé → ne pas laisser d'orphelin
    return None


def allocate_multicast(port=None, owner_ref=None):
    """1ᵉʳ groupe multicast libre du pool (`mcast_pool_base .. +size`) au port donné/défaut, RÉSERVÉ
    ATOMIQUEMENT (voir database.db_reserve_mcast) pour `owner_ref` — l'INSERT en base est lui-même
    l'opération qui tranche, pas une lecture Python suivie d'une décision : deux appels concurrents
    (même processus ou non) qui viseraient la même adresse ne peuvent pas tous les deux « gagner ».
    `owner_ref` identifie de façon stable QUI détient cette adresse (ex. 'tx:302:0:video:leg0',
    'nmos:<resource_id>') — requis pour toute allocation réelle ; permet la libération ciblée
    (db_release_mcast_owner/_prefix) à la destruction du container/de la ressource.
    Retourne (ip, port) ou (None, None) si épuisé."""
    from .database import db_reserve_mcast, db_add_alert
    base = settings.get("mcast_pool_base") or "239.100.0.0"
    size = int(settings.get("mcast_pool_size") or 4096)
    port = int(port or settings.get("mcast_port_default") or 5000)
    try:
        a = int(ipaddress.IPv4Address(base))
    except Exception as e:
        log.warning(f"mcast_pool_base invalide ({base}): {e}")
        return (None, None)
    if not owner_ref:
        log.warning("allocate_multicast() appelé sans owner_ref — aucune réservation possible")
        return (None, None)
    reused = _reuse_owner_reservation(owner_ref, port)
    if reused:
        return reused
    used = _used_multicasts()
    for i in range(1, size):   # i=0 == base (adresse réseau du bloc, ex. .0) → jamais allouée
        ip = str(ipaddress.IPv4Address(a + i))
        key = f"{ip}:{port}"
        if key in used:
            continue
        if db_reserve_mcast(ip, port, owner_ref):
            return (ip, port)
    db_add_alert("alert.net.pool_multicast_epuise", "error", kind="net",
                 params={"base": base, "size": size})
    return (None, None)


def multicast_conflicts(transports=None):
    """{'ip:port': [resource_ids…]} pour les groupes multicast partagés par >1 ressource (collision).

    `transports` permet à un appelant qui a DÉJÀ lu le registre de le passer plutôt que de le faire
    relire : `/api/home/summary` appelait `multicast_conflicts()` puis `plages_epuisees()`, soit
    deux scans + re-parse JSON du registre NMOS par requête, sur une route pollée toutes les 2 s."""
    by_key = {}
    for rid, tr in (_registry_transports() if transports is None else transports):
        for k in _mcast_keys(tr):
            by_key.setdefault(k, []).append(rid)
    return {k: ids for k, ids in by_key.items() if len(ids) > 1}


def next_free_ip(node_id=None):
    """1ʳᵉ IP libre de la plage effective du nœud (plage cluster si node_id=None)."""
    if settings.get("net_mode") == "dhcp":
        return None
    used = _used_ips()
    for ip in _ip_range(node_id):
        if ip not in used:
            return ip
    return None


# ─── B2-4 : plages multicast STRICTES par port (règles réseau logique / interface physique) ───
# Un switch qui contraint les adresses multicast autorisées PAR PORT (IGMP snooping / forwarding
# statique) impose une plage stricte — pas de repli sur le pool global si la plage dédiée est
# épuisée (le switch rejetterait physiquement une adresse hors plage). Voir mcast_ranges (DB).

def _regle_correspond(r, essence, leg, fmt):
    """None si la règle `r` ne s'applique pas à (essence, leg, fmt) ; sinon un tuple de score de
    spécificité (plus haut = plus spécifique) pour départager plusieurs règles candidates."""
    if r.get("essence") and r["essence"] != essence:
        return None
    if r.get("leg") is not None and int(r["leg"]) != int(leg or 0):
        return None
    nb_crit = 0
    mj = r.get("match_json")
    if mj:
        try:
            crit = json.loads(mj) or {}
        except Exception:
            crit = {}
        fmt = fmt or {}
        for k, v in crit.items():
            if str(fmt.get(k)) != str(v):
                return None
            nb_crit += 1
    return (1 if r.get("essence") else 0, 1 if r.get("leg") is not None else 0, nb_crit, int(r.get("id") or 0))


def _meilleure_regle(rows, essence, leg, fmt):
    scored = []
    for r in rows:
        s = _regle_correspond(r, essence, leg, fmt)
        if s is not None:
            scored.append((s, r))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0])
    return scored[-1][1]


def _plage_applicable(node_id, ifname, media_network_id=None, essence=None, leg=0, fmt=None):
    """Résout la règle `mcast_ranges` la plus spécifique applicable à un port physique donné, ou
    None si aucune règle n'existe (signal : repli sur le pool global historique). Priorité :
    règle INTERFACE (node_id, ifname) exacte &gt; règle RÉSEAU (media_network_id)."""
    from .database import db_get_mcast_ranges
    if node_id and ifname:
        rule = _meilleure_regle(db_get_mcast_ranges(node_id=node_id, ifname=ifname), essence, leg, fmt)
        if rule is not None:
            return rule
    if media_network_id is not None:
        rule = _meilleure_regle(db_get_mcast_ranges(media_network_id=media_network_id), essence, leg, fmt)
        if rule is not None:
            return rule
    return None


def port_default_for(node_id, ifname, media_network_id, essence, leg, fmt, fallback):
    """Port de BASE pour cette essence (2110-20/30/40) selon la règle applicable au port (si une
    règle pose `port_default_<essence>` — ou l'ancien `port_default` générique en repli), sinon
    `fallback` (comportement historique). L'appelant applique ensuite son propre décalage par slot/
    index par-dessus (le port de base ne suffit pas seul à distinguer plusieurs flux de la même
    essence — cf. hooks.py)."""
    rule = _plage_applicable(node_id, ifname, media_network_id, essence, leg, fmt)
    if rule is None:
        return fallback
    return int(rule.get(f"port_default_{essence}") or rule.get("port_default") or fallback)


# ─── PLAN D'ADRESSAGE multicast : UNE ADRESSE PAR FLUX, déduite du rang ──────────────────────────
# La granularité d'un abonnement IGMP est le GROUPE, pas le port : deux flux qui partagent une
# adresse et ne diffèrent que par le port ne sont PAS dissociables côté récepteur. Un décodeur qui
# s'abonne à l'audio d'une sortie encaisse alors AUSSI la vidéo (~2,6 Gb/s en 1080p50 10 bits) du
# même groupe — vécu en prod : le scan « première (ip, port) libre » empilait TOUS les audios et
# TOUTES les ANC du moteur sur l'adresse de la vidéo TX0 (seul le port variait), et un EVS Neuron
# n'arrivait pas à s'abonner. Le plan rend l'adresse DÉDUCTIBLE du rang du flux :
#
#     adresse = base_plage + décalage_essence + (n° d'audio × pas_audio) + (n° de sortie + 1)
#
# Convention par défaut (réglable par plage, cf. mcast_ranges.ip_offset_*) : vidéo en .1, .2, …,
# ANC en .51, .52, …, 1ᵉʳ audio en .101, .102, …, 2ᵉ audio en .201, .202, …
MCAST_PLAN_DEFAUT = {"video": 0, "anc": 50, "audio": 100}
MCAST_PLAN_PAS_AUDIO_DEFAUT = 100


def plan_decalages(rule):
    """Décalages effectifs du plan pour cette règle de plage : valeurs de la règle si posées,
    sinon la convention par défaut. Une règle SPÉCIFIQUE À UNE ESSENCE (l'opérateur a déjà séparé
    les plages par essence) part de 0 — sinon on décalerait deux fois."""
    r = rule or {}
    par_essence = bool(r.get("essence"))
    out = {}
    for _e, _d in MCAST_PLAN_DEFAUT.items():
        v = r.get(f"ip_offset_{_e}")
        out[_e] = int(v) if v is not None else (0 if par_essence else _d)
    v = r.get("ip_step_audio")
    out["pas_audio"] = int(v) if v is not None else MCAST_PLAN_PAS_AUDIO_DEFAUT
    return out


def _plan_offset(rule, essence, slot, sub_index=0, leg=0):
    """Rang (décalage entier depuis base_ip) imposé par le plan, ou None si le plan ne s'applique
    pas — auquel cas l'appelant retombe sur le scan historique « première adresse libre » :
      - pas de n° de sortie connu (`slot=None`) : appel hors arbre TX (NMOS, pool global) ;
      - essence inconnue du plan ;
      - leg 1 (2022-7) sur une règle NON spécifique au leg : les deux legs partageraient la plage,
        donc le rang planifié — le leg 1 garde le scan tant qu'il n'a pas SA plage.
    Le rang 0 (adresse réseau du bloc, ex. .0) n'est jamais planifié : le plan démarre à slot+1."""
    if slot is None or essence not in MCAST_PLAN_DEFAUT:
        return None
    if int(leg or 0) != 0 and (rule or {}).get("leg") is None:
        return None
    d = plan_decalages(rule)
    off = d[essence] + (slot + 1)
    if essence == "audio":
        off += int(sub_index or 0) * d["pas_audio"]
    return off


def allocate_multicast_for(node_id, ifname, media_network_id=None, essence=None, leg=0, port=None,
                           fmt=None, owner_ref=None, slot=None, sub_index=0):
    """Alloue (ip, port) pour un flux qui va égresser sur ce port physique précis. Si une règle
    (réseau ou interface) existe pour ce port → allocation STRICTEMENT dans cette plage (jamais de
    repli au pool global). Sans règle du tout → comportement historique (allocate_multicast).
    `owner_ref` : voir allocate_multicast — réservation ATOMIQUE en base (ferme la fenêtre de course
    entre deux flux/containers dont les calculs se chevauchent, avant que l'un ou l'autre ne soit
    enregistré au registre NMOS). Retourne (None, None) si la plage/le pool est épuisé — une alerte
    est postée dans les deux cas."""
    from .database import db_reserve_mcast, db_add_alert
    rule = _plage_applicable(node_id, ifname, media_network_id, essence, leg, fmt)
    if rule is None:
        return allocate_multicast(port=port, owner_ref=owner_ref)
    if not owner_ref:
        log.warning("allocate_multicast_for() appelé sans owner_ref — aucune réservation possible")
        return (None, None)
    base = rule.get("base_ip")
    size = int(rule.get("size") or 0)
    port = int(port or (essence and rule.get(f"port_default_{essence}"))
               or rule.get("port_default") or settings.get("mcast_port_default") or 5000)
    try:
        a = int(ipaddress.IPv4Address(base))
    except Exception as e:
        log.warning(f"mcast_ranges#{rule.get('id')} base_ip invalide ({base}): {e}")
        return (None, None)
    # ── PLAN d'adressage (une adresse par flux, déduite du rang) — prioritaire sur le scan.
    # On RÉSERVE d'abord l'adresse planifiée, on rend l'ancienne ENSUITE (keep=) : l'inverse
    # ouvrirait une fenêtre où un autre flux souffle l'adresse qu'on vient de lâcher. Adresse
    # planifiée déjà tenue par un AUTRE flux → on garde l'existante / on retombe sur le scan
    # (jamais de vol : le plan converge au prochain passage, une fois l'occupant replanifié).
    off = _plan_offset(rule, essence, slot, sub_index, leg)
    if off is not None and 1 <= off < max(size, 0):
        ip_plan = str(ipaddress.IPv4Address(a + off))
        if db_reserve_mcast(ip_plan, port, owner_ref):
            from .database import db_release_mcast_owner
            db_release_mcast_owner(owner_ref, keep=(ip_plan, port))
            return (ip_plan, port)
        log.info("plan multicast %s: %s:%s déjà pris pour %s — repli scan",
                 owner_ref, ip_plan, port, essence)
    elif off is not None:
        label = rule.get("label") or f"{rule.get('scope')}#{rule.get('id')}"
        db_add_alert("alert.net.plan_multicast_hors_plage", "warning", kind="net",
                     params={"label": label, "base": base, "size": size, "essence": essence, "off": off})
    reused = _reuse_owner_reservation(owner_ref, port, in_range=(a, max(size, 0)))
    if reused:
        return reused
    used = _used_multicasts()
    for i in range(1, max(size, 0)):   # i=0 == base (adresse réseau du bloc, ex. .0) → jamais allouée
        ip = str(ipaddress.IPv4Address(a + i))
        key = f"{ip}:{port}"
        if key in used:
            continue
        if db_reserve_mcast(ip, port, owner_ref):
            return (ip, port)
    label = rule.get("label") or f"{rule.get('scope')}#{rule.get('id')}"
    db_add_alert("alert.net.plage_multicast_epuisee", "error", kind="net",
                 params={"label": label, "base": base, "size": size, "port": port})
    return (None, None)


def _egress_iface(node_id, params, slot_i, leg=0):
    """(ifname, media_network_id) réellement utilisé par le slot TX `slot_i`/leg. leg=0 : épinglage
    explicite (`params.tx_pins`) sinon NIC média primaire du nœud. leg=1 (2022-7) : interface de
    même pair_group et pair_role opposé (red/blue) à celle du leg0 ; repli sur la même ifname si
    aucun appariement red/blue n'est déclaré. Défensif : toute erreur → (None, None) (repli pool
    global naturel via _plage_applicable)."""
    if not node_id:
        return (None, None)
    try:
        from .database import db_get_node_interfaces, db_get_node
        ifaces = [r for r in db_get_node_interfaces(node_id) if r.get("role") == "media2110"]
        if not ifaces:
            return (None, None)
        pin = (params.get("tx_pins") or {}).get(str(slot_i))
        row0 = next((r for r in ifaces if r["ifname"] == pin), None) if pin else None
        if row0 is None:
            node = db_get_node(node_id) or {}
            primary = node.get("mtl_iface")
            row0 = next((r for r in ifaces if r["ifname"] == primary), None) or ifaces[0]
        if int(leg or 0) == 0:
            return (row0["ifname"], row0.get("media_network_id"))
        if row0.get("pair_group") is not None:
            pair = next((r for r in ifaces if r.get("pair_group") == row0.get("pair_group")
                        and r.get("pair_role") and r.get("pair_role") != row0.get("pair_role")), None)
            if pair:
                return (pair["ifname"], pair.get("media_network_id"))
        return (row0["ifname"], row0.get("media_network_id"))
    except Exception as e:
        log.warning(f"_egress_iface(node {node_id}, slot {slot_i}, leg {leg}): {e}")
        return (None, None)


def plages_epuisees(transports=None):
    """Règles `mcast_ranges` actuellement à 0 adresse libre (état LIVE) — alimente le panneau
    Points d'attention. Vérifie sur le `port_default` de la règle si posé, sinon sur le port par
    défaut global (simplification déjà présente dans `allocate_multicast()` : l'épuisement réel
    est de toute façon signalé en direct par une alerte au moment de l'allocation qui échoue)."""
    from .database import db_get_mcast_ranges
    used = _used_multicasts(transports)
    out = []
    for r in db_get_mcast_ranges():
        size = int(r.get("size") or 0)
        if size <= 0:
            continue
        try:
            a = int(ipaddress.IPv4Address(r["base_ip"]))
        except Exception:
            continue
        port = int(r.get("port_default") or settings.get("mcast_port_default") or 5000)
        libre = any(f"{ipaddress.IPv4Address(a + i)}:{port}" not in used for i in range(size))
        if not libre:
            out.append(r)
    return out


# ─── Re-planification d'un moteur 2110 déjà adressé ───────────────────────────────────────────────

def _plan_ip_flux(node_id, params, slot_i, essence, sub_index=0, fmt=None):
    """Adresse que le PLAN impose à ce flux (slot/essence/rang d'audio), ou None si le plan ne
    s'applique pas (aucune règle de plage sur le port d'égression, rang hors plage…)."""
    ifn, netid = _egress_iface(node_id, params, slot_i, leg=0)
    rule = _plage_applicable(node_id, ifn, media_network_id=netid, essence=essence, leg=0, fmt=fmt)
    if rule is None:
        return None
    off = _plan_offset(rule, essence, slot_i, sub_index, leg=0)
    size = int(rule.get("size") or 0)
    if off is None or not (1 <= off < size):
        return None
    try:
        return str(ipaddress.IPv4Address(int(ipaddress.IPv4Address(rule["base_ip"])) + off))
    except Exception as e:
        log.warning("plan multicast slot %s/%s: base_ip invalide: %s", slot_i, essence, e)
        return None


def plan_tx_multicast(vmid, appliquer=False):
    """Recalcule les adresses multicast des sorties TX d'un moteur 2110 selon le PLAN (cf.
    _plan_offset) et retourne le diff `[{slot, essence, audio_idx, port, de, vers, etat}]`.

    `appliquer=False` (défaut) = SIMULATION : rien n'est réservé, rien n'est persisté, rien n'est
    poussé au moteur. `appliquer=True` réserve les nouvelles adresses (owner_ref identique à
    l'allocation automatique → la libération à la destruction du conteneur les ramasse), réécrit
    `tx_slots`, persiste, puis pousse EN UN SEUL LOT (`push_tx_slots`) — les changements de
    destination changent la signature des sessions, donc un commit TM = **un seul blip** au lieu
    d'un par flux. Les ports ne sont PAS touchés : seule l'adresse de groupe est replanifiée.

    Le plan ne renumérote QUE les flux qui ont déjà une destination (renumérotation, pas
    provisionnement) et laisse en place tout flux dont l'adresse planifiée est tenue par un autre
    (`etat='conflit'`) — jamais de vol d'adresse."""
    import json as _json
    from .database import (db_get_container, db_update_deploy_config, db_reserve_mcast,
                           db_release_mcast_owner, db_release_mcast_addr, db_add_alert)
    from . import io2110_flows as _iof

    c = db_get_container(vmid) or {}
    if not c:
        return [], "moteur introuvable"
    try:
        dc = _json.loads(c.get("deploy_config") or "{}") or {}
    except Exception:
        dc = {}
    ctype = dc.get("type") or "2110_io"
    params = dict(dc.get("params") or {})
    slots = [dict(s or {}) for s in (params.get("tx_slots") or [])]
    if not slots:
        return [], "aucune sortie TX sur ce moteur"
    node_id = c.get("node_id")

    diff = []
    for i, t in enumerate(slots):
        fmt_v = {"scan": t.get("scan"), "width": t.get("width"),
                 "height": t.get("height"), "fps": t.get("fps")}
        cibles = []
        if t.get("multicast_ip"):
            cibles.append(("video", 0, t.get("multicast_ip"), t.get("dest_port"),
                           f"tx:{vmid}:{i}:video:leg0", fmt_v))
        # owner_ref audio : indexé par l'idx FLAT du flux (comme l'allocation automatique du hook
        # 2110_io) — pas par le rang dans le slot, sinon la réservation replanifiée ne retomberait
        # pas sur la même clé que celle du prochain déploiement (ledger dédoublé).
        aud_idxs = _iof.tx_slot_audio_idxs(params.get("tx_flows") or [], i)
        for ai, a in enumerate(t.get("audios") or []):
            if a.get("multicast_ip"):
                aidx = aud_idxs[ai] if ai < len(aud_idxs) else ai
                cibles.append(("audio", ai, a.get("multicast_ip"), a.get("dest_port"),
                               f"tx:{vmid}:{i}:audio:{aidx}:leg0", {"channels": 8}))
        if t.get("anc_multicast_ip"):
            cibles.append(("anc", 0, t.get("anc_multicast_ip"), t.get("anc_dest_port"),
                           f"tx:{vmid}:{i}:anc:leg0", None))

        for essence, sub, ip_cur, port, owner_ref, fmt in cibles:
            ip_plan = _plan_ip_flux(node_id, params, i, essence, sub, fmt)
            if not ip_plan:
                continue
            ligne = {"slot": i, "essence": essence, "audio_idx": sub, "port": port,
                     "de": ip_cur, "vers": ip_plan,
                     "etat": "inchange" if ip_plan == ip_cur else "a_changer"}
            if appliquer and ligne["etat"] == "a_changer":
                if not db_reserve_mcast(ip_plan, port, owner_ref):
                    ligne["etat"] = "conflit"     # tenue par un autre flux → on ne touche à rien
                else:
                    db_release_mcast_owner(owner_ref, keep=(ip_plan, port))
                    # Rendre l'ANCIENNE adresse : son owner_ref historique peut différer de celui
                    # de l'allocation courante (':layout', ancien index de flux…) — sans ça elle
                    # resterait marquée occupée par un flux qui ne l'émet plus.
                    db_release_mcast_addr(ip_cur, port, owner_prefix=f"tx:{vmid}:")
                    if essence == "video":
                        t["multicast_ip"] = ip_plan
                    elif essence == "anc":
                        t["anc_multicast_ip"] = ip_plan
                    else:
                        auds = list(t.get("audios") or [])
                        auds[sub] = dict(auds[sub]); auds[sub]["multicast_ip"] = ip_plan
                        t["audios"] = auds
                    ligne["etat"] = "applique"
            diff.append(ligne)

    if not appliquer:
        return diff, None
    if not any(d["etat"] == "applique" for d in diff):
        return diff, None

    params["tx_slots"] = slots
    db_update_deploy_config(vmid, ctype, params)
    n = sum(1 for d in diff if d["etat"] == "applique")
    try:
        from . import docker_driver
        docker_driver.push_tx_slots(vmid, params)
    except Exception as e:
        log.warning("plan_tx_multicast %s: push_tx_slots: %s", vmid, e)
        return diff, f"adresses persistées mais push au moteur en échec : {e}"
    try:
        from services import nmos as _nmos
        _nmos.notify_state_change()
    except Exception:
        pass
    db_add_alert("alert.net.plan_multicast_applique", "info",
                 vmid=vmid, kind="net",
                 params={"h": c.get('hostname') or vmid, "n": n})
    return diff, None


def preview_plan_params(vmid, params):
    """Params RÉSULTANTS d'une re-planification (rien n'est réservé ni persisté) — sert au verdict
    de pré-vol de `tx_maintenance` (quelles sorties vont figer le temps du commit TM)."""
    diff, _err = plan_tx_multicast(vmid, appliquer=False)
    out = dict(params or {})
    slots = [dict(s or {}) for s in (out.get("tx_slots") or [])]
    for d in diff:
        if d.get("etat") != "a_changer" or not (0 <= d["slot"] < len(slots)):
            continue
        t = slots[d["slot"]]
        if d["essence"] == "video":
            t["multicast_ip"] = d["vers"]
        elif d["essence"] == "anc":
            t["anc_multicast_ip"] = d["vers"]
        else:
            auds = [dict(a or {}) for a in (t.get("audios") or [])]
            if d["audio_idx"] < len(auds):
                auds[d["audio_idx"]]["multicast_ip"] = d["vers"]
                t["audios"] = auds
    out["tx_slots"] = slots
    return out
