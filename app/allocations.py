# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Calcul des VMIDs et IPs libres en croisant settings, DB locale et liste LXC Proxmox.
"""
import contextlib
import ipaddress
import json
import logging
import math
import threading
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


def interne_bounds(node_id):
    """(start, end) allouables dans le subnet privé d'un nœud en mode « réseau interne ».

    La passerelle du bridge est le PREMIER hôte du subnet (c'est l'interface que Docker pose sur
    l'hôte) : on commence donc au deuxième, et on s'arrête à l'avant-dernier — la dernière
    adresse d'un /24 est l'adresse de diffusion. Sur un subnet mal formé on rend ("", ""), ce qui
    épuise la plage et déclenche l'alerte d'épuisement, plutôt que d'inventer des bornes.
    """
    from .database import db_get_node
    from .config import NET_INTERNE_SUBNET_DEFAUT
    sub = (((db_get_node(node_id) or {}).get("container_net_subnet") or "").strip()
           or NET_INTERNE_SUBNET_DEFAUT)
    try:
        hosts = list(ipaddress.ip_network(sub, strict=False).hosts())
    except Exception as e:
        log.warning("interne_bounds(%s): subnet « %s » invalide : %s", node_id, sub, e)
        return ("", "")
    if len(hosts) < 3:
        log.warning("interne_bounds(%s): subnet « %s » trop petit", node_id, sub)
        return ("", "")
    return (str(hosts[1]), str(hosts[-1]))


def node_ip_bounds(node_id=None):
    """(start, end, scope) de la plage d'allocation EFFECTIVE d'un nœud.

    Trois cas, dans cet ordre, et l'ordre EST la règle :
    1. nœud en « réseau interne » → le subnet PRIVÉ du nœud (scope='interne') ;
    2. sinon, plage personnalisée de sa carte containers/mgmt_containers (scope='node') ;
    3. sinon, plage cluster ip_start/ip_end (scope='cluster'). node_id=None → cluster.

    ★ POURQUOI LE MODE PASSE AVANT LA PLAGE DE LA CARTE. En mode interne, les conteneurs ne sont
    plus sur le LAN : la plage de la carte du site désigne des adresses qui n'existent pas dans le
    bridge. Or `centralized_ipam()` est VRAI dès qu'une plage personnalisée est posée, donc
    l'orchestrateur imposerait `docker run --ip <IP du site>` dans un subnet privé — refusé par
    Docker, déploiement mort, et le message ne parle ni de mode ni de plage. La plage de la carte
    n'est pas effacée pour autant : elle redevient effective à la seconde où le nœud repasse en
    mode site.
    """
    from .database import node_net_interne
    if node_id and node_net_interne(node_id):
        s, e = interne_bounds(node_id)
        return (s, e, "interne")
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


def _used_ips(exclude_vmid=None, node_id=None):
    """IPs du plan conteneurs déjà prises (DB) : `ip` (legacy) ET `docker_ip` (plan conteneurs
    macvlan, B2-2). `exclude_vmid` : ignore les IP de ce conteneur (idempotence au redeploy).

    `node_id` : ne compter que les conteneurs DE CE NŒUD. Réservé aux plages à portée de nœud
    (mode « réseau interne ») : un subnet privé ne sort pas de la machine, donc 172.30.0.10 sur
    deux nœuds sont deux adresses différentes et les compter ensemble réduirait sans raison le
    nombre de conteneurs déployables. Pour une plage de SITE, l'occupation reste globale — là,
    deux conteneurs à la même adresse, ce sont deux machines en conflit sur le LAN.
    """
    used = set()
    for c in db_get_containers():
        if exclude_vmid is not None and c.get("vmid") == exclude_vmid:
            continue
        if node_id is not None and c.get("node_id") != node_id:
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
        # IP du shim macvlan d'un nœud (cf. container_net) : elle vit sur le MÊME subnet que les
        # conteneurs et n'est pas dans la plage par construction — mais la plage peut être
        # élargie plus tard, et l'attribuer à un conteneur mettrait deux machines à la même
        # adresse sur le LAN, au moment précis où l'exploitant croit agrandir son pool. On la
        # réserve pour TOUS les nœuds, pas seulement celui qu'on alloue : le subnet est partagé.
        sh = (n.get("container_net_shim_ip") or "").strip()
        if sh:
            res.add(sh)
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
    from .database import db_get_nodes, node_net_interne
    if settings.get("net_topology") == "separated":
        return True
    # Mode « réseau interne » : l'orchestrateur attribue dans le subnet PRIVÉ du nœud. On ne laisse
    # pas l'IPAM de Docker choisir, pour la même raison qu'ailleurs — une IP collante survit au
    # redéploiement, donc le contrôleur ne perd pas sa cible :8081 à chaque recréation.
    if node_id and node_net_interne(node_id):
        return True
    if _node_ct_iface(node_id):
        return True
    if settings.get("net_mode") == "static" and len(_range_list(
            settings.get("ip_start"), settings.get("ip_end"))) > 0:
        return True
    return len(db_get_nodes() or []) > 1


def allocate_container_ip(vmid, node_id=None, held_on_node=None):
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
    conteneur (db_delete_container). Voir database.db_reserve_ip.

    `held_on_node` ({ip: docker name}, from the node itself): IPs Docker already gives out on the
    node's network. The database cannot see a container it forgot (failed destroy, hand-made
    container); Docker refuses its IP with « Address already in use ». Those IPs are skipped, and
    each one held by a container unknown here raises an alert naming it (2026-11-21)."""
    from .database import (db_get_container, db_add_alert, db_reserve_ip,
                           db_used_ip_reservations, db_release_ip_reservations_for_vmid)
    c = db_get_container(vmid) or {}
    if node_id is None:
        node_id = c.get("node_id")
    rng = _ip_range(node_id)
    # Portée de l'occupation = portée de la plage. Un subnet privé (mode « réseau interne ») ne
    # sort pas du nœud : on ne compte que SES conteneurs. Une plage de site est partagée : on
    # compte tout le monde. `_reserved_ips` reste global dans les deux cas — en interne il ne
    # peut que protéger, au cas où le subnet privé recouvrirait le LAN.
    _prive = node_ip_bounds(node_id)[2] == "interne"
    # Redeploy du même vmid : on relâche d'abord SES propres réservations, sinon on se bloquerait
    # nous-même (idempotence — le container ré-obtient son IP collante ou une nouvelle).
    db_release_ip_reservations_for_vmid(vmid)
    blocked = (_used_ips(exclude_vmid=vmid, node_id=node_id if _prive else None)
               | _reserved_ips(node_id)        # passerelle / orchestrateur / hôtes des nœuds
               | db_used_ip_reservations())    # + IP réservées par une alloc concurrente
    held = held_on_node or {}
    if held:
        known = _used_ips(exclude_vmid=None, node_id=None) | db_used_ip_reservations()
        for ip, nm in sorted(held.items()):
            if ip in rng and ip not in known:
                db_add_alert("alert.net.ip_tenue_hors_base", "warning", vmid=vmid, node_id=node_id,
                             kind="net", params={"ip": ip, "c": nm or "?", "vmid": vmid})
        blocked |= set(held)
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

    def _entry(start, end, scope, label, used=None, **extra):
        # `used` : jeu d'IP occupées à considérer pour CETTE plage. Par défaut, toutes celles du
        # parc — mais une plage privée (mode interne) ne doit compter que les conteneurs de son
        # nœud, sinon un conteneur d'un autre nœud ferait paraître l'adresse prise ici.
        occ = used_all if used is None else used
        rng = _range_list(start, end)
        free = [ip for ip in rng if ip not in occ]
        e = {"scope": scope, "label": label, "start": start, "end": end,
             "total": len(rng), "free": len(free), "used": sorted(occ & set(rng))}
        e.update(extra)
        return e

    cluster = _entry(settings.get("ip_start"), settings.get("ip_end"), "cluster", "cluster")
    ranges = [cluster]
    from .database import node_net_interne
    for n in (db_get_nodes() or []):
        # Un nœud en « réseau interne » a sa propre plage, qu'il faut montrer pour la même raison
        # que les autres : sans ligne à lui, la page Réglages affiche un pool qui ne le concerne
        # plus et donne à croire qu'il y puise.
        if node_net_interne(n):
            s_i, e_i = interne_bounds(n.get("id"))
            if s_i:
                ranges.append(_entry(s_i, e_i, "interne", n.get("name") or f"nœud {n['id']}",
                                     used=_used_ips(node_id=n["id"]), node_id=n["id"]))
            continue
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
# ─── Read snapshot ─────────────────────────────────────────────────────────────────────────
# A SIMULATION (plan preview) asks "which groups are held?" once per flow. Each answer re-read
# and re-decoded the whole NMOS registry and the whole ledger: 133 times for one preview of a
# 132-flow engine, about 1 s under the interpreter lock, replayed at every keystroke of the plan
# editor (measured 2026-11-11: the orchestrator answered in seconds while the editor was open).
# Inside `read_snapshot()` both reads are taken once. ONLY for code that writes nothing: an
# allocator that reserves must see the ledger as it is now, never a copy.
_snapshot = threading.local()


@contextlib.contextmanager
def read_snapshot():
    if getattr(_snapshot, "data", None) is not None:      # nested: keep the outer one
        yield
        return
    _snapshot.data = {}
    try:
        yield
    finally:
        _snapshot.data = None


def _registry_transports():
    """(resource_id, transport) du registre NMOS — source de vérité cluster du multicast."""
    snap = getattr(_snapshot, "data", None)
    if snap is not None and "registry" in snap:
        return snap["registry"]
    try:
        from .database import db_nmos_resources
        out = [(r["id"], r.get("transport") or {}) for r in db_nmos_resources()]
    except Exception:
        out = []
    if snap is not None:
        snap["registry"] = out
    return out


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


# ─── Group exclusivity: one multicast GROUP per flow, whatever the port ──────────────────────────
# IGMP subscribes to a group, not to a port: two flows on the same address that differ only by
# port cannot be separated by a receiver (it gets both). Measured on a live engine: the 2022-7
# leg-1 audios of TX1 and TX2 both landed on the leg-0 video group of TX1, because the scan only
# skipped an occupied `ip:port`. Every allocator below therefore treats an address as free only if
# NO other flow holds it, on any port. Existing allocations are left as they are.

def _ledger_rows(ip=None):
    """Ledger rows `(rowid, ip, port, owner_ref)`, in insertion order (rowid), optionally for one
    address only."""
    from .database import get_db
    snap = getattr(_snapshot, "data", None)
    if snap is not None:
        if "ledger" not in snap:
            with get_db() as db:
                snap["ledger"] = [(int(r["rid"]), r["ip"], int(r["port"]), r["owner_ref"]) for r in db.execute(
                    "SELECT rowid AS rid, ip, port, owner_ref FROM mcast_allocations ORDER BY rowid").fetchall()]
        return [r for r in snap["ledger"] if ip is None or r[1] == ip]
    q = "SELECT rowid AS rid, ip, port, owner_ref FROM mcast_allocations"
    args = ()
    if ip is not None:
        q += " WHERE ip=?"
        args = (ip,)
    with get_db() as db:
        return [(int(r["rid"]), r["ip"], int(r["port"]), r["owner_ref"])
                for r in db.execute(q + " ORDER BY rowid", args).fetchall()]


def _groups_held_by_others(owner_ref, transports=None):
    """Addresses (groups) held by a flow OTHER than `owner_ref`, on any port: ledger rows of other
    owners, plus NMOS registry transports whose exact `ip:port` is absent from the ledger (an
    address imposed by hand, never reserved). A registry `ip:port` that IS in the ledger belongs to
    its ledger owner — otherwise a flow would be refused the address its own NMOS resource shows.
    `owner_ref` may be a set of owner refs (the legs of one flow, see `_flow_refs`)."""
    mine = owner_ref if isinstance(owner_ref, (set, frozenset)) else {owner_ref}
    taken = set()
    ledger_keys = set()
    for _rid, ip, port, owner in _ledger_rows():
        ledger_keys.add(f"{ip}:{port}")
        if owner not in mine:
            taken.add(ip)
    for _res_id, tr in (_registry_transports() if transports is None else transports):
        for k in _mcast_keys(tr):
            if k not in ledger_keys:
                taken.add(k.rsplit(":", 1)[0])
    return taken


def _release_exact(ip, port, owner_ref):
    """Release exactly this (ip, port) row of this owner (never another owner's row)."""
    from .database import get_db
    with get_db() as db:
        db.execute("DELETE FROM mcast_allocations WHERE ip=? AND port=? AND owner_ref=?",
                   (ip, int(port), owner_ref))
        db.commit()


def _claim_group(ip, port, owner_ref):
    """Reserve (ip, port) for `owner_ref` and keep it only if no other owner holds the GROUP.

    The ledger's uniqueness is on (ip, port): two concurrent allocators picking the same ip with
    different ports both get their INSERT accepted. So after a successful reservation, re-read the
    rows of that ip. Deterministic loser: the row inserted LAST (higher rowid) backs off. Both
    allocators see the same rowid order; the later inserter necessarily reads after the earlier
    one's commit (SQLite serializes writes), so it sees it and yields, while the earlier one sees
    either nothing or a later row and keeps its address. Exactly one wins; neither loops forever.
    Returns True if the address is ours."""
    from .database import db_reserve_mcast
    if not db_reserve_mcast(ip, port, owner_ref):
        return False
    rows = _ledger_rows(ip)
    mine = [rid for rid, _ip, p, o in rows if o == owner_ref and p == int(port)]
    if not mine:                      # released under our feet: treat as lost
        return False
    if any(o != owner_ref and rid < mine[0] for rid, _ip, _p, o in rows):
        _release_exact(ip, port, owner_ref)
        log.info("multicast %s: group %s taken concurrently by another flow — trying next", owner_ref, ip)
        return False
    return True


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
    """First free multicast group of the global pool (`mcast_pool_base .. +size`) at the given/default
    port, reserved ATOMICALLY (see database.db_reserve_mcast) for `owner_ref` — the INSERT itself
    decides, not a Python read followed by a decision: two concurrent calls (same process or not)
    aiming at the same address cannot both "win". A group is free only if no other flow holds it on
    ANY port (see `_claim_group`). `owner_ref` identifies WHO holds the address in a stable way
    (e.g. 'tx:302:0:video:leg0', 'nmos:<resource_id>') — required for any real allocation; it allows
    targeted release (db_release_mcast_owner/_prefix) when the container/resource is destroyed.
    Returns (ip, port), or (None, None) if exhausted."""
    from .database import db_add_alert
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
    taken = _groups_held_by_others(owner_ref)
    for i in range(1, size):   # i=0 == base (network address of the block, e.g. .0) → never allocated
        ip = str(ipaddress.IPv4Address(a + i))
        if ip in taken:
            continue
        if _claim_group(ip, port, owner_ref):
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

# Relative tolerance for numeric format criteria: fractional rates are written in several ways
# (59.94 in a rule, 60000/1001 = 59.94005994 on a slot: relative gap 1e-6) and must match, while
# two distinct broadcast rates (59.94 vs 60: relative gap 1e-3) must not.
MATCH_REL_TOL = 1e-4


def _as_number(v):
    """`v` as a finite float if it is a number or a numeric string, else None. Booleans are not
    numbers here (True would otherwise equal 1)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _criterion_matches(actual, expected):
    """One `match_json` criterion. A key absent from the flow format (None) never matches. Two
    numeric values (or numeric strings) compare as numbers within MATCH_REL_TOL: the settings UI
    stores `{"fps": 50}` while a TX slot carries `50.0`, and the old text comparison ("50" vs
    "50.0") made the rule silently never apply. Anything else compares as text, as before."""
    if actual is None:
        return False
    a, e = _as_number(actual), _as_number(expected)
    if a is not None and e is not None:
        return math.isclose(a, e, rel_tol=MATCH_REL_TOL, abs_tol=1e-9)
    return str(actual) == str(expected)


def _regle_correspond(r, essence, leg, fmt):
    """None if rule `r` does not apply to (essence, leg, fmt); otherwise a specificity score tuple
    (higher = more specific) used to rank several candidate rules."""
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
            if not _criterion_matches(fmt.get(k), v):
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
    plan = _flow_plan(node_id, ifname, media_network_id)
    if plan is not None:
        if essence == "audio":            # callers pass a partial format: complete it (ptime…)
            from .io2110_flows import audio_format
            fmt = dict(audio_format({}), **(fmt or {}))
        rule = _plan_line(plan, essence, fmt)
    else:
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
    """Rank (integer offset from base_ip) the plan imposes, or None when the plan does not apply —
    the caller then falls back to the historical "first free address" scan:
      - no known output number (`slot=None`): call outside the TX tree (NMOS, global pool);
      - essence unknown to the plan;
      - leg 1 (2022-7) on a rule NOT specific to the leg: both legs would share the range, hence
        the planned rank — leg 1 keeps the scan until it has ITS range.
    Rank 0 (network address of a block, e.g. .0) is never planned: the plan starts at slot+1. An
    explicit range ("first-last", `explicit_span`) has no network address: it starts at slot."""
    if slot is None or essence not in MCAST_PLAN_DEFAUT:
        return None
    if (rule or {}).get("rank_mode") == "counter":    # no planned rank: next free group of the range
        return None
    if int(leg or 0) != 0 and (rule or {}).get("leg") is None:
        return None
    d = plan_decalages(rule)
    # Output N (slot N-1) at rank N in a block (rank 0 = network address), at rank N-1 in an
    # explicit range, whose first address is usable.
    off = d[essence] + slot + (0 if (rule or {}).get("explicit_span") else 1)
    if essence == "audio":
        off += int(sub_index or 0) * d["pas_audio"]
    return off


# ─── Per-range plan axes ─────────────────────────────────────────────────────────────────────────
# Sites address multicast in many ways; a plan is a COMBINATION of independent per-range settings,
# never one scheme. Each axis is NULL by default, and NULL keeps the historical behaviour:
#   rank_mode    'slot' (rank from the output number, `_plan_offset`) | 'counter' (next free group);
#   port_mode    'increment' (the caller's port) | 'fixed' (one port per essence, the rule's);
#   reserve_last the last address of the block is never allocated (the first never is);
#   base_ip_b    2022-7 leg B = base_ip_b + (A − base_ip); equal to base_ip = same group on both
#                fabrics.

def _alloc_span(rule):
    """`(first, end)` offsets allocatable in the rule's A block, `end` exclusive: rank 0 (network
    address) is never allocated, and `reserve_last` withholds the last one too. An EXPLICIT range
    (plan line `explicit_span`, typed "first-last") is allocatable end to end: `(0, size)`."""
    r = rule or {}
    size = max(int(r.get("size") or 0), 0)
    if r.get("explicit_span"):
        return 0, size
    return 1, max(size - 1 if r.get("reserve_last") else size, 1)


def range_capacity(rule):
    """Number of allocatable A addresses of a rule (first/last reservations deducted)."""
    first, end = _alloc_span(rule)
    return max(end - first, 0)


def range_used(rule, taken):
    """Number of groups of `taken` (strings) that fall inside the rule's A block."""
    try:
        a = int(ipaddress.IPv4Address(rule["base_ip"]))
    except Exception:
        return 0
    size = int(rule.get("size") or 0)
    n = 0
    for ip in taken:
        try:
            n += a <= int(ipaddress.IPv4Address(ip)) < a + size
        except Exception:
            continue
    return n


def ranges_with_usage(ranges, transports=None):
    """`ranges` (dicts) each completed with `capacity` and `used` (groups currently held in its A
    block, ledger + registry, as the allocators see them)."""
    taken = _groups_held_by_others(None, transports)
    return [dict(r, capacity=range_capacity(r), used=range_used(r, taken)) for r in ranges]


def rule_port(rule, essence, port):
    """Port a flow of `essence` gets under `rule`: with `port_mode='fixed'` the rule's essence port
    (fallback `port_default`) whatever the caller passed, otherwise the caller's `port`."""
    r = rule or {}
    if r.get("port_mode") == "fixed":
        fixed = (essence and r.get(f"port_default_{essence}")) or r.get("port_default")
        if fixed:
            return int(fixed)
    return port


def derive_leg_b(rule, ip_a):
    """Leg-B address of a flow whose leg-0 address is `ip_a`, or None (rule without `base_ip_b`,
    or `ip_a` outside the rule's A block)."""
    r = rule or {}
    if not r.get("base_ip_b") or not ip_a:
        return None
    try:
        off = int(ipaddress.IPv4Address(ip_a)) - int(ipaddress.IPv4Address(r["base_ip"]))
        if not 0 <= off < int(r.get("size") or 0):
            return None
        return str(ipaddress.IPv4Address(int(ipaddress.IPv4Address(r["base_ip_b"])) + off))
    except Exception:
        return None


def _rule_contains(rule, ip):
    try:
        off = int(ipaddress.IPv4Address(ip)) - int(ipaddress.IPv4Address(rule["base_ip"]))
    except Exception:
        return False
    return 0 <= off < int(rule.get("size") or 0)


def _derivation_rule(node_id, ifname, media_network_id, essence, fmt, ip_a):
    """The rule deriving leg B of a flow whose leg-0 address is `ip_a`: a rule with `base_ip_b`
    whose A block holds `ip_a`. `ifname`/`media_network_id` are the LEG-1 port; the leg-0 port is
    its paired interface, whose applicable rule is tried first, then every rule of either port."""
    from .database import db_get_mcast_ranges

    def derives(r):
        return bool(r and r.get("base_ip_b")) and _rule_contains(r, ip_a)

    pair = interface_appairee(node_id, ifname)
    ifn0, net0 = (pair["ifname"], pair.get("media_network_id")) if pair else (ifname, media_network_id)
    r = _plage_applicable(node_id, ifn0, net0, essence, 0, fmt)
    if derives(r):
        return r
    rows = []
    if node_id:
        for ifn in {ifn0, ifname} - {None}:
            rows += db_get_mcast_ranges(node_id=node_id, ifname=ifn)
    for net in {net0, media_network_id} - {None}:
        rows += db_get_mcast_ranges(media_network_id=net)
    return _meilleure_regle([r for r in rows if derives(r)], essence, 0, fmt)


def _slot_plan(node_id, params, slot_i):
    """`(plan, fabric of leg 0)` addressing TX slot `slot_i`, or None (no plan: legacy rules)."""
    ifn0, net0 = _egress_iface(node_id, params, slot_i, leg=0)
    plan = _flow_plan(node_id, ifn0, net0)
    return (plan, fabric_of(node_id, ifn0)) if plan is not None else None


def _expected_leg_b(node_id, params, slot_i, essence, fmt, ip_a):
    """Leg-B address the rules derive for TX slot `slot_i` from its leg-0 address, or None when no
    derivation rule covers it. Under a plan: the other fabric's block at the rank leg 0 holds in
    its own fabric's block (None if leg 0 is outside it)."""
    sp = _slot_plan(node_id, params, slot_i)
    if sp is not None:
        plan, fab0 = sp
        view = _plan_line(plan, essence, fmt)
        if view is None:
            return None
        return _fabric_addr(view, _other_fabric(fab0), _rank_in(view, fab0, ip_a))
    ifn1, net1 = _egress_iface(node_id, params, slot_i, leg=1)
    rule = _derivation_rule(node_id, ifn1, net1, essence, fmt, ip_a)
    return derive_leg_b(rule, ip_a) if rule else None


def _flow_refs(owner_ref, sub_index=0):
    """Ledger owner refs of BOTH legs of the flow `owner_ref` belongs to ('tx:{vmid}:{slot}:
    {essence}[:{aidx}]:leg{0|1}'), operator overrides included ('…:{essence}:override:{rank}:
    leg{n}', see routes/mtl_engine.api_mtl_tx_dest). The legs of one flow may share a group."""
    if not owner_ref or not owner_ref.endswith((":leg0", ":leg1")):
        return {owner_ref}
    refs = {owner_ref[:-1] + "0", owner_ref[:-1] + "1"}
    parts = owner_ref.split(":")
    if len(parts) >= 5 and parts[0] == "tx":
        rank = int(sub_index or 0) if parts[3] == "audio" else 0
        for leg in (0, 1):
            refs.add(f"tx:{parts[1]}:{parts[2]}:{parts[3]}:override:{rank}:leg{leg}")
    return refs


def _leg0_address(owner_ref, sub_index=0):
    """`(ip, port)` held in the ledger by leg 0 of the flow whose leg-1 owner is `owner_ref`, or
    None. An operator override wins over the automatic reservation it superseded (which stays in
    the ledger until the container is destroyed)."""
    from .database import db_mcast_for_owner
    parts = owner_ref.split(":")
    cands = []
    if len(parts) >= 5 and parts[0] == "tx":
        rank = int(sub_index or 0) if parts[3] == "audio" else 0
        cands.append(f"tx:{parts[1]}:{parts[2]}:{parts[3]}:override:{rank}:leg0")
    cands.append(owner_ref[:-1] + "0")
    for ref in cands:
        got = db_mcast_for_owner(ref)
        if got:
            return (got[0], int(got[1]))
    return None


def _leg_b_alert(owner_ref, sub_index, ip_b, rule, holders):
    """Alert: the derived leg-B address of a flow is held by another flow, so leg B is left empty."""
    from .database import db_add_alert, db_get_container
    parts = owner_ref.split(":")
    vmid, flow = None, owner_ref
    if len(parts) >= 5 and parts[0] == "tx":
        try:
            vmid = int(parts[1])
            flow = "TX%d %s%s (B)" % (int(parts[2]) + 1, parts[3],
                                      str(int(sub_index or 0) + 1) if parts[3] == "audio" else "")
        except ValueError:
            pass
    host = ((db_get_container(vmid) or {}).get("hostname") if vmid else None) or str(vmid or "?")
    label = rule.get("label") or f"{rule.get('scope')}#{rule.get('id')}"
    db_add_alert("alert.net.mcast_jambe_b_hors_regle", "warning", vmid=vmid, kind="net",
                 params={"h": host, "n": 1,
                         "detail": "%s: %s (%s) ← %s" % (flow, ip_b, label, ", ".join(sorted(holders)))})


def _allocate_leg_b(node_id, ifname, media_network_id, essence, fmt, owner_ref, sub_index=0):
    """Leg 1 of a flow whose leg-0 port is covered by a rule with `base_ip_b`: B is DERIVED from the
    leg-0 address (same rank in the B block), with the leg-0 port. Returns None when no derivation
    rule applies (the caller then allocates as before), else `(ip, port)` or `(None, None)`.

    Never a fallback: a derived address held by another flow is refused with an alert, and leg B
    stays empty, rather than landing on an address the switch of fabric B was not set up for.
    Same group on both legs (`base_ip_b == base_ip`): the ledger key is (ip, port) and both legs
    have the same port, so leg 1 records NOTHING; the leg-0 row holds the group for the flow, and
    releasing it (destroy, replan) releases both legs."""
    from .database import db_release_mcast_owner
    if not owner_ref.endswith(":leg1"):
        return None
    leg0 = _leg0_address(owner_ref, sub_index)
    if leg0 is None:
        # Leg 0 unknown (its allocation failed, or it predates the ledger): if its port derives
        # leg B, nothing can be derived and nothing else may be picked.
        pair = interface_appairee(node_id, ifname)
        r0 = _plage_applicable(node_id, pair["ifname"], pair.get("media_network_id"),
                               essence, 0, fmt) if pair else None
        if r0 and r0.get("base_ip_b"):
            log.warning("multicast %s: leg 0 has no address, leg B cannot be derived (rule #%s)",
                        owner_ref, r0.get("id"))
            return (None, None)
        return None
    ip_a, port_a = leg0
    rule = _derivation_rule(node_id, ifname, media_network_id, essence, fmt, ip_a)
    ip_b = derive_leg_b(rule, ip_a)
    if ip_b is None:
        return None
    refs = _flow_refs(owner_ref, sub_index)
    holders = {o for _rid, ip, _p, o in _ledger_rows(ip_b) if o not in refs}
    if not holders and ip_b in _groups_held_by_others(refs):
        holders = {"nmos"}                       # registry-only holder (address imposed by hand)
    if holders:
        _leg_b_alert(owner_ref, sub_index, ip_b, rule, holders)
        return (None, None)
    if ip_b == ip_a:
        db_release_mcast_owner(owner_ref)        # a stale leg-1 row of this flow, if any
        return (ip_a, port_a)
    already = any(ip == ip_b and p == port_a and o == owner_ref
                  for _rid, ip, p, o in _ledger_rows(ip_b))
    if already or _claim_group(ip_b, port_a, owner_ref):
        db_release_mcast_owner(owner_ref, keep=(ip_b, port_a))
        return (ip_b, port_a)
    _leg_b_alert(owner_ref, sub_index, ip_b, rule,
                 {o for _rid, _ip, _p, o in _ledger_rows(ip_b) if o not in refs})
    return (None, None)


# ─── Fabrics and multicast PLANS ──────────────────────────────────────────────────────────────────
# Sites define multicast ranges per FABRIC (the A blocks live on one switch fabric, the B blocks on
# the other) and declare redundancy per PAIR of interfaces (`node_interfaces.pair_group` +
# `pair_role` red|blue). Which colour is "first" varies by site: `media_networks.first_fabric`.
# A flow's leg 0 is the interface its slot is PINNED to, which can be either colour; so a plan
# addresses by fabric, never by leg: the RANK is a property of the flow, shared by both legs, and
# the address on fabric a is A base + rank, on fabric b B base + rank.
#
# A plan (`mcast_plans`) = one target (media network | node pair | unpaired interface) + shared
# settings (b_rule, rank_mode, port_mode, reserve_last) + lines (`mcast_ranges` rows with
# `plan_id`). Rows without `plan_id` are LEGACY rules and keep their behaviour (`_plage_applicable`).

def _media_ifaces(node_id):
    """Media (ST 2110) interfaces of a node, [] on error."""
    if not node_id:
        return []
    try:
        from .database import db_get_node_interfaces
        return [r for r in db_get_node_interfaces(node_id) if r.get("role") == "media2110"]
    except Exception as e:
        log.warning("_media_ifaces(node %s): %s", node_id, e)
        return []


def _is_paired(row):
    return bool(row) and row.get("pair_group") is not None and bool(row.get("pair_role"))


def _first_colours():
    """{media_network_id: 'red'|'blue'} — the colour that is fabric a on each network."""
    try:
        from .database import db_get_media_networks
        return {n["id"]: n.get("first_fabric") or "red" for n in db_get_media_networks()}
    except Exception:
        return {}


def pair_members(node_id, pair_group, rows=None):
    """`{'a': row, 'b': row}` of a red/blue pair of a node, or None if the pair is incomplete.

    Fabric a is the member whose colour is the `first_fabric` of its own media network. Members on
    two networks that disagree (both, or neither, claim fabric a) are settled by the network of the
    red member, so the answer never depends on which member is asked."""
    rows = rows if rows is not None else _media_ifaces(node_id)
    mates = [r for r in rows if _is_paired(r) and r.get("pair_group") == pair_group]
    reds = [r for r in mates if r.get("pair_role") == "red"]
    blues = [r for r in mates if r.get("pair_role") == "blue"]
    if not reds or not blues:
        return None
    red, blue = reds[0], blues[0]
    first = _first_colours()
    votes = [m for m in (red, blue) if m.get("pair_role") == first.get(m.get("media_network_id"), "red")]
    a_colour = votes[0].get("pair_role") if len(votes) == 1 \
        else first.get(red.get("media_network_id"), "red")
    return {"a": red, "b": blue} if a_colour == "red" else {"a": blue, "b": red}


def fabric_of(node_id, ifname):
    """'a' | 'b': the fabric of a media interface. An unpaired (or unknown) interface is fabric a.
    THE single source of the fabric of an interface — allocation, re-planning, audit, preview."""
    rows = _media_ifaces(node_id)
    me = next((r for r in rows if r.get("ifname") == ifname), None)
    if not _is_paired(me):
        return "a"
    mem = pair_members(node_id, me.get("pair_group"), rows)
    if not mem:
        return "a"
    return "a" if mem["a"].get("ifname") == ifname else "b"


def _other_fabric(fabric):
    return "b" if fabric == "a" else "a"


def _plans(candidate=None):
    """Stored plans (with lines); `candidate` (an unsaved or edited plan, same shape) replaces the
    stored plan of the same id, or is added — used by the preview, nothing is written."""
    try:
        from .database import db_get_mcast_plans
        plans = db_get_mcast_plans()
    except Exception as e:
        log.warning("mcast plans unreadable: %s", e)
        plans = []
    if candidate is not None:
        cid = candidate.get("id")
        plans = [p for p in plans if cid is None or p["id"] != cid] + [candidate]
    return plans


def _flow_plan(node_id, ifname, media_network_id=None, candidate=None):
    """The plan that addresses a flow leaving by `ifname` (either leg: the answer is the same for
    both legs of a pair), or None. Priority: plan of its PAIR > plan of its (unpaired) INTERFACE >
    plan of its media NETWORK (for a pair: the fabric-a member's network, then the other's)."""
    plans = _plans(candidate)
    if not plans:
        return None
    rows = _media_ifaces(node_id)
    me = next((r for r in rows if r.get("ifname") == ifname), None) if ifname else None

    def find(scope, **kw):
        return next((p for p in plans if p.get("scope") == scope
                     and all(p.get(k) == v for k, v in kw.items())), None)

    if _is_paired(me):
        p = find("pair", node_id=node_id, pair_group=me.get("pair_group"))
        if p:
            return p
        mem = pair_members(node_id, me.get("pair_group"), rows)
        nets = [mem["a"].get("media_network_id"), mem["b"].get("media_network_id")] if mem \
            else [me.get("media_network_id")]
    elif me:
        p = find("interface", node_id=node_id, ifname=ifname)
        if p:
            return p
        nets = [me.get("media_network_id")]
    else:
        nets = [media_network_id]
    for net in nets:
        if net is not None:
            p = find("network", media_network_id=net)
            if p:
                return p
    return None


def _line_view(line, plan):
    """A plan line as a rule dict the range helpers understand: the plan's shared settings merged
    in, no leg (addressing is by fabric), and a label naming the plan."""
    v = dict(line)
    for k in ("rank_mode", "port_mode", "reserve_last"):
        v[k] = plan.get(k)
    v["leg"] = None
    v["label"] = "%s / %s" % (plan.get("label") or "plan#%s" % plan.get("id"),
                              line.get("label") or "%s/+%s" % (line.get("base_ip"), line.get("size")))
    return v


def _plan_line(plan, essence, fmt):
    """The line of `plan` for a flow (most specific match on essence + format, like
    `_meilleure_regle`) as a rule view, or None."""
    line = _meilleure_regle(plan.get("lines") or [], essence, 0, fmt)
    return _line_view(line, plan) if line is not None else None


def _block_base(view, fabric):
    """Integer base of the line's block on `fabric` (A block on a, B block on b), or None."""
    v = (view or {}).get("base_ip" if fabric == "a" else "base_ip_b")
    try:
        return int(ipaddress.IPv4Address(v)) if v else None
    except Exception:
        return None


def _rank_in(view, fabric, ip):
    """Rank of `ip` inside the line's block on `fabric`, or None."""
    base = _block_base(view, fabric)
    if base is None or not ip:
        return None
    try:
        off = int(ipaddress.IPv4Address(ip)) - base
    except Exception:
        return None
    return off if 0 <= off < int(view.get("size") or 0) else None


def _fabric_addr(view, fabric, rank):
    """Address of `rank` on `fabric`, or None (no block on that fabric, or no rank)."""
    base = _block_base(view, fabric)
    if base is None or rank is None:
        return None
    return str(ipaddress.IPv4Address(base + rank))


def _sibling_address(owner_ref, sub_index=0):
    """`(ip, port)` held in the ledger by the OTHER leg of the flow `owner_ref` belongs to, or None.
    An operator override wins over the automatic reservation it superseded."""
    from .database import db_mcast_for_owner
    if owner_ref.endswith(":leg1"):
        return _leg0_address(owner_ref, sub_index)
    if not owner_ref.endswith(":leg0"):
        return None
    parts = owner_ref.split(":")
    cands = []
    if len(parts) >= 5 and parts[0] == "tx":
        rank = int(sub_index or 0) if parts[3] == "audio" else 0
        cands.append(f"tx:{parts[1]}:{parts[2]}:{parts[3]}:override:{rank}:leg1")
    cands.append(owner_ref[:-1] + "1")
    for ref in cands:
        got = db_mcast_for_owner(ref)
        if got:
            return (got[0], int(got[1]))
    return None


def _flow_desc(owner_ref, sub_index=0):
    """`(vmid, hostname, 'TX1 audio1 (B)')` of a ledger owner ref, for alerts."""
    from .database import db_get_container
    parts = (owner_ref or "").split(":")
    vmid, flow = None, owner_ref
    if len(parts) >= 5 and parts[0] == "tx":
        try:
            vmid = int(parts[1])
            flow = "TX%d %s%s%s" % (int(parts[2]) + 1, parts[3],
                                    str(int(sub_index or 0) + 1) if parts[3] == "audio" else "",
                                    " (B)" if owner_ref.endswith(":leg1") else "")
        except ValueError:
            pass
    host = ((db_get_container(vmid) or {}).get("hostname") if vmid else None) or str(vmid or "?")
    return vmid, host, flow


def _plan_refusal(key, owner_ref, sub_index, plan, essence):
    """Alert for a flow a plan applies to but cannot address (no line for it, or no block on its
    fabric). The allocation is refused: never a fall-through to another plan or to the pool."""
    from .database import db_add_alert
    vmid, host, flow = _flow_desc(owner_ref, sub_index)
    db_add_alert(key, "warning", vmid=vmid, kind="net",
                 params={"h": host, "flow": flow, "essence": essence or "?",
                         "plan": plan.get("label") or "plan#%s" % plan.get("id")})


def _allocate_in_plan(plan, node_id, ifname, essence, port, fmt, owner_ref, slot, sub_index):
    """Allocation of one leg of a flow under a PLAN, by fabric (see the section header).

    - the line is the most specific match; none → refusal + alert, `(None, None)`;
    - if the OTHER leg of the flow already holds an address in its fabric's block, the rank follows
      it (whichever leg came first, whichever colour leg 0 is): address = this fabric's base + rank,
      with the other leg's port. Held by another flow → refusal + alert, as `_allocate_leg_b`.
      Same group on both fabrics (B block = A block): no second ledger row, as `_allocate_leg_b`;
    - otherwise this leg picks the rank: `rank_mode='slot'` the planned rank, else (or if the
      planned groups are held) the first rank whose address on this fabric AND, for a paired
      interface with a distinct B block, on the other fabric are both free."""
    from .database import db_release_mcast_owner, db_mcast_for_owner
    view = _plan_line(plan, essence, fmt)
    if view is None:
        _plan_refusal("alert.net.mcast_plan_unmatched", owner_ref, sub_index, plan, essence)
        return (None, None)
    fab = fabric_of(node_id, ifname)
    other = _other_fabric(fab)
    base_f = _block_base(view, fab)
    if base_f is None:
        _plan_refusal("alert.net.mcast_plan_no_b_block", owner_ref, sub_index, plan, essence)
        return (None, None)
    port = int(port or (essence and view.get(f"port_default_{essence}"))
               or view.get("port_default") or settings.get("mcast_port_default") or 5000)
    port = rule_port(view, essence, port)
    refs = _flow_refs(owner_ref, sub_index)

    sib = _sibling_address(owner_ref, sub_index)
    rank = _rank_in(view, other, sib[0]) if sib else None
    if rank is not None:
        ip = _fabric_addr(view, fab, rank)
        holders = {o for _rid, i, _p, o in _ledger_rows(ip) if o not in refs}
        if not holders and ip in _groups_held_by_others(refs):
            holders = {"nmos"}
        if holders:
            _leg_b_alert(owner_ref, sub_index, ip, view, holders)
            return (None, None)
        if ip == sib[0]:                          # same group on both fabrics: no second row
            db_release_mcast_owner(owner_ref)
            return (ip, sib[1])
        already = any(i == ip and p == sib[1] and o == owner_ref for _rid, i, p, o in _ledger_rows(ip))
        if already or _claim_group(ip, sib[1], owner_ref):
            db_release_mcast_owner(owner_ref, keep=(ip, sib[1]))
            return (ip, sib[1])
        _leg_b_alert(owner_ref, sub_index, ip, view,
                     {o for _rid, _i, _p, o in _ledger_rows(ip) if o not in refs})
        return (None, None)

    first, end = _alloc_span(view)
    base_o = _block_base(view, other)
    check_other = (interface_appairee(node_id, ifname) is not None
                   and base_o is not None and base_o != base_f)
    taken = _groups_held_by_others(refs)

    def try_rank(r):
        ip = str(ipaddress.IPv4Address(base_f + r))
        already = any(i == ip and p == port and o == owner_ref for _rid, i, p, o in _ledger_rows(ip))
        if not already:
            if ip in taken or (check_other and str(ipaddress.IPv4Address(base_o + r)) in taken):
                return None
            if not _claim_group(ip, port, owner_ref):
                return None
        db_release_mcast_owner(owner_ref, keep=(ip, port))
        return (ip, port)

    off = _plan_offset(view, essence, slot, sub_index, 0)
    if off is not None and first <= off < end:
        got = try_rank(off)
        if got:
            return got
        log.info("multicast plan %s: rank %s held by another flow — scanning", owner_ref, off)
    elif off is not None:
        from .database import db_add_alert
        db_add_alert("alert.net.plan_multicast_hors_plage", "warning", kind="net",
                     params={"label": view["label"], "base": view.get("base_ip"),
                             "size": view.get("size"), "essence": essence, "off": off})
    own = db_mcast_for_owner(owner_ref)            # anti-leak: keep this leg's own reservation
    if own and int(own[1]) == port:
        r = _rank_in(view, fab, own[0])
        if r is not None and first <= r < end:
            got = try_rank(r)
            if got:
                return got
    for r in range(first, end):
        got = try_rank(r)
        if got:
            return got
    from .database import db_add_alert
    db_add_alert("alert.net.plage_multicast_epuisee", "error", kind="net",
                 params={"label": view["label"], "base": _fabric_addr(view, fab, 0),
                         "size": view.get("size"), "port": port})
    return (None, None)


def _flow_audio_fmt(owner_ref, sub_index, fmt):
    """Audio format of the flow `owner_ref` ('tx:{vmid}:{slot}:audio:{aidx}:…') read from its engine
    (`io2110_flows.audio_format`), overriding the caller's `fmt`; if the engine or the entry cannot
    be found, the caller's `fmt` completed with the defaults."""
    from . import io2110_flows as _iof
    parts = (owner_ref or "").split(":")
    if len(parts) >= 5 and parts[0] == "tx" and parts[3] == "audio":
        try:
            from .database import db_get_container
            vmid, slot_i, aidx = int(parts[1]), int(parts[2]), int(parts[4])
            c = db_get_container(vmid) or {}
            params = (json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
            if c and params:
                return dict(fmt or {}, **_iof.audio_format(
                    _audio_entry(params, slot_i, sub_index, aidx), params))
        except (ValueError, TypeError) as e:
            log.debug("audio format of %s: %s", owner_ref, e)
    return dict(_iof.audio_format({}), **(fmt or {}))


def _audio_entry(params, slot_i, ai, aidx):
    """Declared format fields of audio `ai` (flat index `aidx`) of TX slot `slot_i`: its `tx_flows`
    entry overlaid by its `tx_slots[].audios[]` entry."""
    out = {}
    flow = next((f for f in (params.get("tx_flows") or [])
                 if f.get("essence") == "audio" and f.get("idx") == aidx), None)
    out.update(flow or {})
    slots = params.get("tx_slots") or []
    auds = (slots[slot_i] or {}).get("audios") or [] if 0 <= slot_i < len(slots) else []
    if 0 <= int(ai or 0) < len(auds):
        out.update(auds[int(ai or 0)] or {})
    return out


def plan_dest_check(node_id, params, slot_i, leg, essence, ip):
    """Operator-typed destination under a plan: `(applies, error)`. `applies` is False when no plan
    addresses this slot (the legacy range check then runs); otherwise `error` is None when `ip` lies
    in one of the plan's blocks for the fabric of that leg's interface."""
    ifn, net = _egress_iface(node_id, params, slot_i, leg=leg)
    plan = _flow_plan(node_id, ifn, net)
    if plan is None:
        return False, None
    fab = fabric_of(node_id, ifn)
    for line in plan.get("lines") or []:
        if _rank_in(_line_view(line, plan), fab, ip) is not None:
            return True, None
    return True, "adresse hors des blocs du plan « %s » sur la fabric %s (%s)" % (
        plan.get("label") or plan.get("id"), fab.upper(), ifn or "?")


def set_manual_dest(entry, ip_key, port_key, mcast, port):
    """Write an operator-typed destination into a `tx_slots` entry (a slot, or one of its `audios`).
    An address that differs from the stored one is marked `<ip_key>_manual`: the plan never
    rewrites it (`plan_tx_multicast` reports it as 'manuel'). The same address submitted again
    (the page echoing the current value) leaves the marker as it was."""
    if entry.get(ip_key) != mcast:
        entry[ip_key + "_manual"] = True
    entry[ip_key] = mcast
    entry[port_key] = port
    return entry


def allocate_multicast_for(node_id, ifname, media_network_id=None, essence=None, leg=0, port=None,
                           fmt=None, owner_ref=None, slot=None, sub_index=0):
    """Allocate (ip, port) for a flow that will egress on this exact physical port. If a rule
    (network or interface) exists for that port → allocation STRICTLY inside that range (never a
    fallback to the global pool). No rule at all → historical behaviour (allocate_multicast).
    `owner_ref`: see allocate_multicast — ATOMIC reservation in the database (closes the race window
    between two flows/containers whose computations overlap before either is registered in the NMOS
    registry). Whether planned or scanned, a new address is a GROUP no other flow holds on any port
    (see `_claim_group`). Returns (None, None) if the range/pool is exhausted — an alert is posted
    in both cases.

    Per-range plan axes (all NULL = the behaviour above): `rank_mode='counter'` skips the plan,
    `port_mode='fixed'` imposes the rule's essence port whatever `port` says, `reserve_last` never
    allocates the last address, and `base_ip_b` makes a leg-1 address DERIVED from its leg 0 (see
    `_allocate_leg_b`) instead of allocated.

    A multicast PLAN covering the port (`_flow_plan`) takes precedence over every rule and over the
    pool, and addresses by fabric (`_allocate_in_plan`). An audio flow's format is read from its
    engine when `owner_ref` identifies it (`_flow_audio_fmt`), whatever `fmt` the caller passed."""
    from .database import db_add_alert
    if essence == "audio" and owner_ref:
        fmt = _flow_audio_fmt(owner_ref, sub_index, fmt)
    plan = _flow_plan(node_id, ifname, media_network_id)
    if plan is not None:
        if not owner_ref:
            log.warning("allocate_multicast_for() called without owner_ref — no reservation possible")
            return (None, None)
        return _allocate_in_plan(plan, node_id, ifname, essence, port, fmt, owner_ref, slot, sub_index)
    if int(leg or 0) == 1 and owner_ref:
        derived = _allocate_leg_b(node_id, ifname, media_network_id, essence, fmt, owner_ref, sub_index)
        if derived is not None:
            return derived
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
    port = rule_port(rule, essence, port)
    first, end = _alloc_span(rule)
    try:
        a = int(ipaddress.IPv4Address(base))
    except Exception as e:
        log.warning(f"mcast_ranges#{rule.get('id')} base_ip invalide ({base}): {e}")
        return (None, None)
    # ── ADDRESSING PLAN (one address per flow, derived from its rank) — takes precedence over the
    # scan. The planned address is RESERVED first and the old one released AFTERWARDS (keep=): the
    # reverse would open a window where another flow grabs the address just let go. Planned group
    # already held by ANOTHER flow, on any port → keep the existing one / fall back to the scan
    # (never steal: the plan converges on a later pass, once the occupant has been replanned).
    # An owner that already holds exactly (ip_plan, port) keeps it as before, even if an older
    # allocation shares its group: existing allocations are not rewritten here.
    off = _plan_offset(rule, essence, slot, sub_index, leg)
    if off is not None and first <= off < end:
        ip_plan = str(ipaddress.IPv4Address(a + off))
        from .database import db_release_mcast_owner
        already = any(ip == ip_plan and p == port and o == owner_ref
                      for _rid, ip, p, o in _ledger_rows(ip_plan))
        if already or (ip_plan not in _groups_held_by_others(owner_ref)
                       and _claim_group(ip_plan, port, owner_ref)):
            db_release_mcast_owner(owner_ref, keep=(ip_plan, port))
            return (ip_plan, port)
        log.info("multicast plan %s: group %s already held by another flow for %s — falling back to scan",
                 owner_ref, ip_plan, essence)
    elif off is not None:
        label = rule.get("label") or f"{rule.get('scope')}#{rule.get('id')}"
        db_add_alert("alert.net.plan_multicast_hors_plage", "warning", kind="net",
                     params={"label": label, "base": base, "size": size, "essence": essence, "off": off})
    reused = _reuse_owner_reservation(owner_ref, port, in_range=(a, end))
    if reused:
        return reused
    # Scan: first GROUP no other flow holds on any port (a leg-1 flow on a range shared by both
    # legs lands here, and used to pile onto the leg-0 video group when only `ip:port` was checked).
    taken = _groups_held_by_others(owner_ref)
    for i in range(first, end):   # rank 0 (network address of the block, e.g. .0) never allocated
        ip = str(ipaddress.IPv4Address(a + i))
        if ip in taken:
            continue
        if _claim_group(ip, port, owner_ref):
            return (ip, port)
    label = rule.get("label") or f"{rule.get('scope')}#{rule.get('id')}"
    db_add_alert("alert.net.plage_multicast_epuisee", "error", kind="net",
                 params={"label": label, "base": base, "size": size, "port": port})
    return (None, None)


def interface_appairee(node_id, ifname):
    """L'interface média appairée à `ifname` (même `pair_group`, `pair_role` opposé), ou None.

    ★ C'EST LA SOURCE UNIQUE DE LA REDONDANCE ST-2022-7. Déclarer une paire red/blue sur la page
    des interfaces n'est pas un geste accidentel : c'est déjà l'expression de l'intention « ces
    deux ports portent les mêmes flux, l'un protégeant l'autre ». Un second réglage par moteur ne
    servirait qu'à laisser deux vérités diverger — et c'est ce qui arrivait : un booléen global
    sur une carte à quatre ports armait la redondance pour TOUS les flux, y compris ceux qui
    sortaient d'un port sans paire, à qui l'on allouait alors une seconde adresse multicast SUR LE
    MÊME PORT. Deux jambes dans le même câble : aucune protection, le double de débit, et NMOS qui
    l'annonçait comme redondant. Cf. docs/chantiers/2026-09-11-2022-7-global-vs-par-port.md.
    """
    if not node_id or not ifname:
        return None
    try:
        from .database import db_get_node_interfaces
        ifaces = [r for r in db_get_node_interfaces(node_id) if r.get("role") == "media2110"]
        moi = next((r for r in ifaces if r.get("ifname") == ifname), None)
        if not moi or moi.get("pair_group") is None or not moi.get("pair_role"):
            return None
        return next((r for r in ifaces
                     if r.get("pair_group") == moi.get("pair_group")
                     and r.get("pair_role") and r.get("pair_role") != moi.get("pair_role")), None)
    except Exception as e:
        log.warning(f"interface_appairee(node {node_id}, {ifname}): {e}")
        return None


def slot_redondant(node_id, params, slot_i):
    """Le slot TX `slot_i` sort-il par une interface APPAIRÉE ? (donc ST-2022-7 pour ce flux)

    Remplace le booléen global `smpte_2022_7` du moteur : la redondance est une propriété d'un
    CHEMIN, pas d'un équipement. Un même moteur peut légitimement porter des flux protégés et des
    flux qui ne le sont pas."""
    ifn0, _ = _egress_iface(node_id, params, slot_i, leg=0)
    return interface_appairee(node_id, ifn0) is not None


def rx_redondant(node_id, params, slot_i):
    """Le slot RX `slot_i` entre-t-il par une interface APPAIRÉE ? Même règle que l'émission,
    l'épinglage se lisant dans `rx_pins` au lieu de `tx_pins`."""
    if not node_id:
        return False
    try:
        from .database import db_get_node_interfaces, db_get_node
        pin = (params.get("rx_pins") or {}).get(str(slot_i))
        if not pin:
            ifaces = [r for r in db_get_node_interfaces(node_id) if r.get("role") == "media2110"]
            if not ifaces:
                return False
            primary = (db_get_node(node_id) or {}).get("mtl_iface")
            row = next((r for r in ifaces if r.get("ifname") == primary), None) or ifaces[0]
            pin = row.get("ifname")
        return interface_appairee(node_id, pin) is not None
    except Exception as e:
        log.warning(f"rx_redondant(node {node_id}, slot {slot_i}): {e}")
        return False


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
    """`mcast_ranges` rules that currently have 0 free address (LIVE state) — feeds the Points
    d'attention panel. Same notion of "free" as the allocators: a GROUP no flow holds on any port
    (see `_claim_group`), from rank 1 (rank 0, the block's network address, is never allocated —
    counting it made a full range look free forever)."""
    from .database import db_get_mcast_ranges, db_get_mcast_plans
    taken = _groups_held_by_others(None, transports)
    out = []
    rules = list(db_get_mcast_ranges())
    for p in db_get_mcast_plans():                     # plan lines: their A block, plan label
        rules += [_line_view(ln, p) for ln in p.get("lines") or []]
    for r in rules:
        size = int(r.get("size") or 0)
        if size <= 0:
            continue
        try:
            a = int(ipaddress.IPv4Address(r["base_ip"]))
        except Exception:
            continue
        first, end = _alloc_span(r)
        libre = any(str(ipaddress.IPv4Address(a + i)) not in taken for i in range(first, end))
        if not libre:
            out.append(r)
    return out


# ─── Re-planification d'un moteur 2110 déjà adressé ───────────────────────────────────────────────

def _flow_rule0(node_id, params, slot_i, essence, fmt=None):
    """Rule applicable to leg 0 of a flow of TX slot `slot_i` (its egress port), or None."""
    ifn, netid = _egress_iface(node_id, params, slot_i, leg=0)
    return _plage_applicable(node_id, ifn, media_network_id=netid, essence=essence, leg=0, fmt=fmt)


def _plan_ip_flux(node_id, params, slot_i, essence, sub_index=0, fmt=None, rule=None):
    """Address the PLAN imposes on this flow (slot/essence/audio rank), or None if the plan does
    not apply (no range rule on the egress port, 'counter' range, rank outside the range…)."""
    rule = rule if rule is not None else _flow_rule0(node_id, params, slot_i, essence, fmt)
    if rule is None:
        return None
    off = _plan_offset(rule, essence, slot_i, sub_index, leg=0)
    first, end = _alloc_span(rule)
    if off is None or not (first <= off < end):
        return None
    try:
        return str(ipaddress.IPv4Address(int(ipaddress.IPv4Address(rule["base_ip"])) + off))
    except Exception as e:
        log.warning("multicast plan slot %s/%s: invalid base_ip: %s", slot_i, essence, e)
        return None


def _plan_rank(view, fabric, essence, slot, sub, ip_cur):
    """Rank a plan line gives a flow at re-planning: `rank_mode='slot'` the planned rank (None if
    outside the block); 'counter' keeps the rank the flow holds, read in its fabric's block, or in
    the OTHER fabric's block (a flow addressed on the wrong fabric is moved to its own)."""
    first, end = _alloc_span(view)
    if view.get("rank_mode") != "counter":
        r = _plan_offset(view, essence, slot, sub, 0)
        return r if r is not None and first <= r < end else None
    r = _rank_in(view, fabric, ip_cur)
    return r if r is not None else _rank_in(view, _other_fabric(fabric), ip_cur)


def _tx_flows_of(vmid, params, slots):
    """Every TX flow of an engine: `(slot, essence, sub, entry, ip_key, port_key, ref_base, fmt)`.
    `entry` is the dict (slot, or one of its `audios`, copied in place) the flow's keys live in;
    `ref_base + ':leg0'` / `':leg1'` are its ledger owner refs — audio indexed by its FLAT flow index,
    as the deploy hook does, so a replanned reservation lands on the key of the next deployment."""
    from . import io2110_flows as _iof
    out = []
    for i, t in enumerate(slots):
        fmt_v = {"scan": t.get("scan"), "width": t.get("width"),
                 "height": t.get("height"), "fps": t.get("fps")}
        out.append((i, "video", 0, t, "multicast_ip", "dest_port", f"tx:{vmid}:{i}:video", fmt_v))
        aud_idxs = _iof.tx_slot_audio_idxs(params.get("tx_flows") or [], i)
        if t.get("audios"):
            t["audios"] = [dict(a or {}) for a in t["audios"]]
            for ai, a in enumerate(t["audios"]):
                aidx = aud_idxs[ai] if ai < len(aud_idxs) else ai
                fmt_a = _iof.audio_format(dict(_audio_entry(params, i, ai, aidx), **a), params)
                out.append((i, "audio", ai, a, "multicast_ip", "dest_port",
                            f"tx:{vmid}:{i}:audio:{aidx}", fmt_a))
        out.append((i, "anc", 0, t, "anc_multicast_ip", "anc_dest_port", f"tx:{vmid}:{i}:anc", None))
    return out


def _apply_leg_b(vmid, ref1, sub, ip_b_cur, port_b, ip_b_new, ip_a):
    """Move leg B of a flow to `ip_b_new` in the ledger (re-planning). False if another flow holds
    that group. Same group as leg 0 → no leg-1 row (see `_allocate_leg_b`). The old leg-B row is
    released only if it belongs to this flow's leg 1 (never the leg-0 row, never another flow)."""
    from .database import db_reserve_mcast, db_release_mcast_owner
    refs = _flow_refs(ref1, sub)
    if ip_b_new in _groups_held_by_others(refs):
        return False
    if ip_b_new == ip_a:
        db_release_mcast_owner(ref1)
    else:
        if not db_reserve_mcast(ip_b_new, port_b, ref1):
            return False
        db_release_mcast_owner(ref1, keep=(ip_b_new, port_b))
    for _rid, _ip, p, o in _ledger_rows(ip_b_cur):
        if p == int(port_b) and o in refs and o.endswith(":leg1") and ip_b_cur != ip_b_new:
            _release_exact(ip_b_cur, p, o)
    return True


def plan_tx_multicast(vmid, appliquer=False):
    """Recompute the multicast addresses of a 2110 engine's TX outputs according to the PLAN (see
    _plan_offset) and return the diff `[{slot, essence, audio_idx, leg, fabric, port, de, vers,
    etat}]` (`fabric` 'a'|'b' = fabric of that leg's interface, see `fabric_of`).

    `appliquer=False` (default) = SIMULATION: nothing reserved, persisted or pushed.
    `appliquer=True` reserves the new addresses (same owner_ref as the automatic allocation → the
    release on container destruction collects them), rewrites `tx_slots`, persists, then pushes IN
    ONE BATCH (`push_tx_slots`) — destination changes change the session signature, so a TM commit
    = **one blip** instead of one per flow. Ports are NOT touched: only the group address is
    replanned. A flow whose port differs from a `port_mode='fixed'` rule carries `port_rule` (the
    rule's port) as information.

    The plan only renumbers flows that already have a destination (renumbering, not provisioning)
    and leaves in place any flow whose planned address is held by another one (`etat='conflit'`) —
    never an address theft. An address typed by an operator (`<key>_manual`) is never rewritten:
    `etat='manuel'`. Leg B (2022-7) follows leg A when a rule derives it (`base_ip_b`); a
    'counter' range has no plan, so only its leg B can be re-derived. Under a multicast PLAN both
    legs follow the flow's rank, each on its own fabric's block (`_plan_rank`)."""
    import json as _json
    from .database import (db_get_container, db_update_deploy_config, db_reserve_mcast,
                           db_release_mcast_owner, db_release_mcast_addr, db_add_alert)

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
    slot_ctx = {}                         # slot → (plan context, fabric of leg 0, fabric of leg 1)
    for i, essence, sub, e, kip, kport, ref_base, fmt in _tx_flows_of(vmid, params, slots):
        ip_cur = e.get(kip)
        if not ip_cur:
            continue
        port = e.get(kport)
        owner_ref = ref_base + ":leg0"
        if i not in slot_ctx:
            ifn0, _n0 = _egress_iface(node_id, params, i, leg=0)
            ifn1, _n1 = _egress_iface(node_id, params, i, leg=1)
            slot_ctx[i] = (_slot_plan(node_id, params, i),
                           fabric_of(node_id, ifn0), fabric_of(node_id, ifn1))
        sp, fab0, fab1 = slot_ctx[i]
        if sp is not None:
            # Under a plan: the rank is the flow's, the address its fabric's (see _allocate_in_plan).
            rule0 = _plan_line(sp[0], essence, fmt)
            if rule0 is None:
                continue                  # no line for this flow: the allocator refuses it too
            ip_plan = _fabric_addr(rule0, fab0, _plan_rank(rule0, fab0, essence, i, sub, ip_cur))
        else:
            rule0 = _flow_rule0(node_id, params, i, essence, fmt)
            ip_plan = _plan_ip_flux(node_id, params, i, essence, sub, fmt, rule=rule0)
        ligne = {"slot": i, "essence": essence, "audio_idx": sub, "leg": 0, "fabric": fab0,
                 "port": port, "de": ip_cur, "vers": ip_cur}
        fixed = rule_port(rule0, essence, None) if rule0 else None
        if fixed and port and int(port) != int(fixed):
            ligne["port_rule"] = int(fixed)
        ip_a = ip_cur                     # the leg-0 address leg B must follow
        if e.get(kip + "_manual"):
            ligne["etat"] = "manuel"
        elif ip_plan:
            ligne["vers"] = ip_plan
            ligne["etat"] = "inchange" if ip_plan == ip_cur else "a_changer"
            if ligne["etat"] == "a_changer" and not appliquer:
                ip_a = ip_plan
            elif ligne["etat"] == "a_changer":
                if sp is not None:
                    # Under a plan, a flow addressed before it can hold its two addresses on the
                    # wrong fabrics (leg 0 pinned to the second fabric got the A address): leg 0's
                    # planned address is then held by this flow's own leg 1, which moves right
                    # after to the address leg 0 leaves. Release that leg-1 row first.
                    for _rid, _ip, _p, _o in _ledger_rows(ip_plan):
                        if _o in _flow_refs(owner_ref, sub) and _o.endswith(":leg1"):
                            _release_exact(_ip, _p, _o)
                if not db_reserve_mcast(ip_plan, port, owner_ref):
                    ligne["etat"] = "conflit"     # held by another flow → leave it alone
                else:
                    db_release_mcast_owner(owner_ref, keep=(ip_plan, port))
                    # Release the OLD address: its historical owner_ref may differ from the
                    # current one (':layout', an older flow index…) — otherwise it would stay
                    # marked as held by a flow that no longer sends it.
                    db_release_mcast_addr(ip_cur, port, owner_prefix=f"tx:{vmid}:")
                    e[kip] = ip_a = ip_plan
                    ligne["etat"] = "applique"
        elif "port_rule" in ligne:
            ligne["etat"] = "inchange"
        else:
            ligne = None
        if ligne:
            diff.append(ligne)

        ip_b = e.get(kip + "_leg1")
        if not ip_b:
            continue
        port_b = e.get(kport + "_leg1") or port
        lb = {"slot": i, "essence": essence, "audio_idx": sub, "leg": 1, "fabric": fab1,
              "port": port_b, "de": ip_b, "vers": ip_b}
        if e.get(kip + "_leg1_manual"):
            lb["etat"] = "manuel"
            diff.append(lb)
            continue
        exp = _expected_leg_b(node_id, params, i, essence, fmt, ip_a)
        if not exp:
            continue
        lb["vers"] = exp
        lb["etat"] = "inchange" if exp == ip_b else "a_changer"
        if appliquer and lb["etat"] == "a_changer":
            if _apply_leg_b(vmid, ref_base + ":leg1", sub, ip_b, port_b, exp, ip_a):
                e[kip + "_leg1"] = exp
                lb["etat"] = "applique"
            else:
                lb["etat"] = "conflit"
        diff.append(lb)

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


def leg_b_mismatches(vmid, node_id, params):
    """Flows whose leg-B address does not follow its derivation rule (`base_ip_b`) given the CURRENT
    leg-0 address: `[{slot, essence, sub, ip, expected}]`. Manual leg-B addresses are skipped."""
    out = []
    slots = [dict(s or {}) for s in ((params or {}).get("tx_slots") or [])]
    for i, essence, sub, e, kip, _kp, _ref, fmt in _tx_flows_of(vmid, params or {}, slots):
        ip_a, ip_b = e.get(kip), e.get(kip + "_leg1")
        if not ip_a or not ip_b or e.get(kip + "_leg1_manual"):
            continue
        exp = _expected_leg_b(node_id, params, i, essence, fmt, ip_a)
        if exp and exp != ip_b:
            out.append({"slot": i, "essence": essence, "sub": sub, "ip": ip_b, "expected": exp})
    return out


def leg_b_mismatches_for(vmid):
    """`leg_b_mismatches` of an engine read from the database ([] if absent)."""
    import json as _json
    from .database import db_get_container
    c = db_get_container(vmid) or {}
    try:
        params = (_json.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
    except Exception:
        params = {}
    return leg_b_mismatches(vmid, c.get("node_id"), params) if c else []


def preview_plan_params(vmid, params):
    """Params RESULTING from a re-planning (nothing reserved or persisted) — feeds the
    `tx_maintenance` pre-flight verdict (which outputs will freeze during the TM commit). Leg-B
    rows (`leg=1`) land in the `_leg1` keys."""
    diff, _err = plan_tx_multicast(vmid, appliquer=False)
    out = dict(params or {})
    slots = [dict(s or {}) for s in (out.get("tx_slots") or [])]
    for d in diff:
        if d.get("etat") != "a_changer" or not (0 <= d["slot"] < len(slots)):
            continue
        t = slots[d["slot"]]
        sfx = "_leg1" if d.get("leg") else ""
        if d["essence"] == "video":
            t["multicast_ip" + sfx] = d["vers"]
        elif d["essence"] == "anc":
            t["anc_multicast_ip" + sfx] = d["vers"]
        else:
            auds = [dict(a or {}) for a in (t.get("audios") or [])]
            if d["audio_idx"] < len(auds):
                auds[d["audio_idx"]]["multicast_ip" + sfx] = d["vers"]
                t["audios"] = auds
    out["tx_slots"] = slots
    return out
