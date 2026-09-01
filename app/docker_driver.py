# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Driver de déploiement Docker — premier citoyen multi-nœud de Bobi.Studio.

Pilote des conteneurs Docker sur un nœud distant (hôte MTL) via le canal
SSH-host déjà utilisé par ptp/mtl (`host_ops.ssh_run`). Modèle validé :
E810 + AF_XDP sur PF + `--network host --privileged` (cf. mémoires mtl-pivot-docker-pf-dpdk).

Le conteneur exécute un CONTRÔLEUR (plugins/<type>/docker/controller.py) qui joue le rôle
agent+contrôleur : il sert `:8080` (métriques) et `:8081/nmos/subscribe` (réception du SDP
IS-05), exactement le contrat que l'orchestrateur attend déjà des LXC. Avec `--network host`,
l'IP du conteneur = l'hôte du nœud → métriques/NMOS/liveness fonctionnent sans changement.

v1 : 1 session active par conteneur/port (un PF = un mtl_init). Multi-session = suivi.
"""

import base64
import ipaddress as _ipaddress
from .numerotation import cle_tx_shm, cle_tx_audio_shm, cle_tx_anc_shm, slot_tx, slot_rx
import json
import logging
import shlex
import threading

import requests

from .host_ops import ssh_run
from .database import (db_get_container, db_get_node, db_upsert_container_docker,
                       db_update_status, db_update_source, db_update_deploy_config,
                       db_update_node, db_delete_container, db_add_alert)
from . import plugins, settings
from . import journal as _journal
from . import allocations

log = logging.getLogger(__name__)


def _pcount(params, key, default):
    """Lit un compteur de slots (active_rx_count / active_tx_count …) en PRÉSERVANT un 0 explicite.

    L'idiome `int(params.get(key) or default)` est PIÉGEUX pour ces compteurs : il traite un 0
    VOULU (« aucun slot actif ») comme « non défini » et le remplace par `default` (souvent 6) →
    un moteur sans TX se retrouve avec 6 sorties black-fallback parasites qui saturent les files
    AF-XDP. Ici on ne retombe sur `default` QUE si la clé est absente/None ; 0 reste 0."""
    v = params.get(key)
    return default if v is None else int(v)


# ─── Helpers ─────────────────────────────────────────────────────────
def is_docker(vmid_or_row):
    """True si la ligne container existe. Conservé pour les appelants : depuis le retrait du
    backend LXC, TOUT conteneur est Docker — le prédicat ne discrimine plus que l'existence."""
    c = vmid_or_row if isinstance(vmid_or_row, dict) else db_get_container(vmid_or_row)
    return bool(c)


def node_for(vmid):
    """Nœud (dict) rattaché au container, ou None."""
    c = db_get_container(vmid) or {}
    return db_get_node(c.get("node_id")) if c.get("node_id") else None


def _name(vmid, hostname=None):
    return f"bobi-mtl-{vmid}"


# ─── Nom du moteur 2110 (identité TECHNIQUE, pas un libellé) ─────────────────
# Le hostname du moteur n'est PAS décoratif : il sert de racine aux noms de flux MXL
# (`/dev/shm/<hostname>_<idx>`, `_audio_<idx>`, `_anc_<idx>`, cf. le `wiring` du manifeste) ET de
# graine aux SSRC des émissions RTP (controller.py). Le renommer renomme donc les flux — tout
# consommateur câblé dessus perd sa source — et change les SSRC vus par les récepteurs distants.
# À ne faire qu'à la CRÉATION, jamais sur un moteur en service.
#
# Le défaut historique `mtlrx<vmid>` était dérivé du VMID, décrit dans ce projet comme « un handle
# local jetable » : deux moteurs successifs sur le même nœud portaient des noms différents, et le
# nom ne disait rien de l'endroit où tourne le moteur. Comme il y a exactement UN moteur par nœud
# (cf. ensure_node_engine), le nom naturel est celui du nœud.
_NOM_MOTEUR_PREFIXE = "2110-io"

def _slug(s):
    """Normalise pour un composant de CHEMIN (`/dev/shm/...`) : sans accent, minuscules, et rien
    d'autre que [a-z0-9-]. Un nom de nœud peut contenir espaces, accents ou majuscules."""
    import unicodedata, re
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", s).strip("-").lower()
    return re.sub(r"-{2,}", "-", s)


def nom_moteur_defaut(node, vmid=None):
    """Nom par défaut du moteur 2110 d'un nœud : `2110-io-<nœud>` (ex. `2110-io-dl360-1`).

    Repli sur `2110-io-<vmid>` si le nom de nœud est vide ou se normalise à rien (nœud nommé
    uniquement avec des caractères non ASCII) — jamais un nom vide, qui produirait des flux
    `_0`, `_audio_0` sans préfixe et collisionnerait entre nœuds."""
    base = _slug((node or {}).get("name"))
    if not base:
        base = _slug(vmid if vmid is not None else (node or {}).get("id"))
        return f"{_NOM_MOTEUR_PREFIXE}-{base}" if base else _NOM_MOTEUR_PREFIXE
    # Unicité CLUSTER : deux noms de nœuds distincts peuvent se normaliser pareil (« DL360__1 » et
    # « dl360-1 » → même slug). Les flux MXL sont répliqués entre nœuds (RDMA mxl-fabrics), donc
    # des flux homonymes sur deux nœuds collisionneraient à la réplication. En cas d'ambiguïté, on
    # désambiguïse par l'id du nœud — stable, contrairement au vmid.
    try:
        from .database import db_get_nodes
        nid = (node or {}).get("id")
        if sum(1 for n in (db_get_nodes() or []) if _slug(n.get("name")) == base) > 1 and nid is not None:
            base = f"{base}-{nid}"
    except Exception:
        pass
    return f"{_NOM_MOTEUR_PREFIXE}-{base}"


def _hostname_moteur(vmid, params=None, container=None):
    """Hostname EFFECTIF d'un moteur existant : celui persisté (params puis table `containers`)
    fait foi — il est la racine des flux déjà publiés. Le défaut dérivé n'est qu'un dernier
    recours. Sans ça, un repli calculé différemment de la valeur persistée renommerait les flux
    en silence au redéploiement."""
    hn = ((params or {}).get("hostname") or "").strip()
    if hn:
        return hn
    c = container if container is not None else (db_get_container(vmid) or {})
    hn = (c.get("hostname") or "").strip()
    if hn:
        return hn
    return nom_moteur_defaut(db_get_node(c.get("node_id")) if c.get("node_id") else None, vmid)


def _xdp_off(node):
    """OBLIGATOIRE après tout stop/rm : MtlManager meurt avant de détacher le prog XDP
    (docker stop sort en ~0.26 s) → résidu sur l'iface. Voir mémoire mtl-pivot-docker-pf-dpdk.
    ⚠️ déstabilise transitoirement ptp4l ~15 s (auto-recovery) — OK sur hôte MTL dédié."""
    iface = (node or {}).get("mtl_iface")
    if not (node and iface):
        return
    rc, _, err = ssh_run(node["host"], f"ip link set dev {shlex.quote(iface)} xdp off", timeout=10)
    if rc not in (0,):
        log.info("xdp off %s sur %s: rc=%s %s", iface, node["host"], rc, err[:120])


def media_ip_addr(node):
    """IP (sans masque) du plan média 2110 de ce nœud, dérivée de `node.media_ip` (CIDR). '' si non
    configurée. Sert de `sip` au moteur MTL (IGMPv3 SSM côté RX, source TX)."""
    cidr = ((node or {}).get("media_ip") or "").strip()
    return cidr.split("/")[0].strip() if cidr else ""


def _purge_ip_elsewhere_cmd(iface, cidr):
    """Snippet sh qui retire l'IP `cidr` de TOUTE interface ≠ `iface` avant de la (re)poser.
    Sans cette purge, une IP média déplacée d'une NIC à une autre (reconfig node_interfaces,
    renommage d'iface…) SUBSISTE sur l'ancienne NIC : les `ip addr add` sont idempotents mais
    rien ne supprime jamais. Conséquence observée (Horace 2026-07) : IP dupliquée sur 2 NIC →
    le join IGMP de libmtl (ip_mreq.imr_interface = IP, résolution par ADRESSE, pas ifindex)
    part sur la mauvaise NIC → le switch forwarde ailleurs que là où la fdir écoute → slot RX
    définitivement muet, et le résidu survit à tous les redéploiements/réalignements."""
    ip_only = shlex.quote(cidr.split("/")[0])
    return (f"ip -4 -o addr show to {ip_only} | "
            f"while read -r _ ifn _ acidr _; do "
            f"[ \"$ifn\" != {shlex.quote(iface)} ] && ip addr del \"$acidr\" dev \"$ifn\"; "
            f"done; ")


def ensure_media_ip(node):
    """Assigne (idempotent) l'IPv4 du plan média 2110 sur `mtl_iface` du nœud. Sans elle, le PF n'a
    pas d'IPv4 → MTL annonce sip=0.0.0.0 (TX rejeté/SSM KO) et la jointure IGMPv3 source-specific
    échoue → rx_gbps=0 (free-run noir). Le chemin LXC dérivait cette IP du pool VF ; ici on la pose
    sur le kernel netdev (le moteur l'auto-détecte via _detect_iface_ip). Ré-appelée à chaque
    (re)déploiement → survit aux reboots (cohérent avec le re-déploiement requis après reboot).
    Retourne (ok, msg). No-op silencieux si aucune IP média n'est configurée."""
    iface = (node or {}).get("mtl_iface")
    cidr  = ((node or {}).get("media_ip") or "").strip()
    if not (node and iface and cidr):
        return (True, "")
    # Socle full-PF DPDK : un port pmd=dpdk est bindé vfio-pci → il n'a PLUS de netdev kernel
    # (`ip addr add` échoue « Cannot find device », alerte bruyante à chaque déploiement). L'IP média
    # d'un port DPDK est portée par libmtl (sip auto-détecté depuis node_interfaces.ip_cidr), pas par
    # le kernel → no-op ici. On skippe donc l'application kernel quand l'iface est déclarée pmd=dpdk.
    try:
        from .database import db_get_node_interfaces
        if any((r.get("ifname") == iface and (r.get("pmd") or "").strip().lower() == "dpdk")
               for r in (db_get_node_interfaces(node.get("id")) or [])):
            return (True, cidr)
    except Exception as e:
        log.debug("ensure_media_ip: check pmd dpdk (%s): %s", iface, e)
    # Purge d'abord l'IP d'éventuelles autres NIC (résidu de reconfig), puis pose idempotente :
    # rc=2 « File exists » si déjà posée sur la bonne iface → toléré.
    cmd = (_purge_ip_elsewhere_cmd(iface, cidr) +
           f"ip addr add {shlex.quote(cidr)} dev {shlex.quote(iface)} 2>&1; "
           f"ip link set dev {shlex.quote(iface)} up")
    rc, out, err = ssh_run(node["host"], cmd, timeout=12)
    blob = (out or "") + (err or "")
    if rc == 0 or "File exists" in blob or "RTNETLINK answers: File exists" in blob:
        return (True, cidr)
    log.warning("media_ip %s sur %s (%s): rc=%s %s", cidr, iface, node.get("host"), rc, blob[:160])
    return (False, blob[:160])


PERSIST_DIR = "/etc/network/interfaces.d"


def _persist_iface_cmd(iface, cidr):
    """Shell qui GRAVE la configuration d'une interface, en plus de l'appliquer à chaud.

    `ip addr add` / `ip link set up` ne vivent qu'en mémoire : au redémarrage du nœud la carte
    repart administrativement DOWN et sans adresse. Pour le rôle `rdma`, c'est invisible et
    trompeur — le port IB retombe DOWN, `ethtool` répond « Link detected: no » alors que le
    câble est parfaitement branché (une carte down ne peut pas détecter de porteuse), et le lien
    passe pour un problème de câblage. Constaté en prod : configuré les 23-24/07, perdu au reboot
    du 25, jamais réappliqué.

    Pourquoi seulement ici et pas pour le média : `ensure_media_ip` est rappelée à CHAQUE
    (re)déploiement du moteur, ce qui la repose de fait. Le RDMA n'a aucun chemin équivalent.

    Le nœud utilise ifupdown, et `/etc/network/interfaces` fait déjà `source interfaces.d/*`.
    Un fichier par interface, réécrit intégralement → idempotent, et retirable proprement."""
    fichier = f"{PERSIST_DIR}/60-bobi-{iface}"
    stanza = (f"# Posé par Bobi.Studio — NE PAS ÉDITER À LA MAIN (réécrit à chaque enregistrement\n"
              f"# de l'interface dans Réglages → Nœuds). Sans cette persistance, l'adresse et\n"
              f"# l'état UP disparaissent au redémarrage et le lien paraît débranché.\n"
              f"auto {iface}\n"
              f"iface {iface} inet static\n"
              f"    address {cidr}\n")
    return (f"mkdir -p {shlex.quote(PERSIST_DIR)}; "
            f"printf '%s' {shlex.quote(stanza)} > {shlex.quote(fichier)}; ")


def oublier_iface_persistante(node, iface):
    """Retire la configuration gravée d'une interface (changement de rôle, suppression).

    Une stanza orpheline n'est pas inerte : si la carte est plus tard bindée à vfio-pci pour
    DPDK, elle n'a plus de netdev kernel et `ifup -a` échoue au démarrage sur une interface
    introuvable. On nettoie donc au retrait, jamais « au cas où »."""
    iface = (iface or "").strip()
    if not (node and node.get("host") and iface):
        return (True, "")
    rc, out, err = ssh_run(node["host"],
                           f"rm -f {shlex.quote(PERSIST_DIR)}/60-bobi-{shlex.quote(iface)}", timeout=12)
    return (rc == 0, ((out or "") + (err or ""))[:160])


def ensure_iface_ip(node, iface, cidr, persist=False):
    """Assigne (idempotent) une IPv4 CIDR sur une interface de l'hôte ET la monte (`ip link set up`).
    Générique (rôle `rdma` : sans IP + interface UP sur la NIC mlx5/RoCE, aucun endpoint fabric
    n'est joignable et le port IB reste DOWN). Même choke-point que `ensure_media_ip` (ssh_run →
    agent host_exec ou SSH legacy). Retourne (ok, msg|cidr). No-op si paramètres incomplets.

    `persist=True` grave en plus la configuration dans /etc/network/interfaces.d (cf.
    `_persist_iface_cmd`) : à réserver aux interfaces qui gardent un netdev kernel — surtout
    PAS un port destiné à DPDK, qui n'en a plus une fois bindé à vfio-pci."""
    iface = (iface or "").strip()
    cidr = (cidr or "").strip()
    if not (node and node.get("host") and iface and cidr):
        return (True, "")
    cmd = ((_persist_iface_cmd(iface, cidr) if persist else "") +
           _purge_ip_elsewhere_cmd(iface, cidr) +
           f"ip addr add {shlex.quote(cidr)} dev {shlex.quote(iface)} 2>&1; "
           f"ip link set dev {shlex.quote(iface)} up")
    rc, out, err = ssh_run(node["host"], cmd, timeout=12)
    blob = (out or "") + (err or "")
    if rc == 0 or "File exists" in blob:
        return (True, cidr)
    log.warning("iface_ip %s sur %s (%s): rc=%s %s", cidr, iface, node.get("host"), rc, blob[:160])
    return (False, blob[:160])


def media_port_pairs(node):
    """Paires SMPTE 2022-7 COMPLÈTES (red, blue) déclarées sur le nœud : [(ifA, ifB), …].
    Partagé entre le run du moteur (env PORT_PAIRS) et l'UI/les budgets « pair-aware »
    (en 2022-7 une paire = UNE capacité utile, pas deux)."""
    groups = {}
    for e in (_media_ifaces(node) or []):
        if e.get("pair_group") and e.get("pair_role") in ("red", "blue"):
            groups.setdefault(e["pair_group"], {})[e["pair_role"]] = e["ifname"]
    return [(g["red"], g["blue"]) for g in groups.values()
            if g.get("red") and g.get("blue")]


def _media_ifaces(node):
    """NIC média 2110 du nœud (`node_interfaces` role=media2110) pour le moteur MULTI-PORT, sous forme
    de dicts {ifname, ip(sans masque), cidr, network_id}. La NIC primaire (= node.mtl_iface) est placée
    EN TÊTE → IFACE/SIP = 1ʳᵉ NIC (rétro-compat). Repli mono-NIC : aucune media2110 déclarée en base →
    [(mtl_iface, media_ip)]. En 2022-7 les deux legs red/blue sont des media2110 → tous deux déclarés.
    `network_id` (media_network_id) regroupe les ports d'un MÊME réseau (interchangeables pour la
    répartition auto des sessions ; cf. _build_run_cmd PORT_NETS/PRIMARY_NET)."""
    from .database import db_get_node_interfaces
    prim = ((node or {}).get("mtl_iface") or "").strip()
    rows, seen = [], set()
    nid = (node or {}).get("id")
    if nid is not None:
        for r in db_get_node_interfaces(nid):
            if r.get("role") != "media2110":
                continue
            ifn = (r.get("ifname") or "").strip()
            if not ifn or ifn in seen:
                continue
            seen.add(ifn)
            cidr = (r.get("ip_cidr") or "").strip()
            _pmd = str(r.get("pmd") or "").strip().lower() or None
            _pci = str(r.get("pci") or "").strip() or None
            _sip = cidr.split("/")[0]
            # SR-IOV (chantier narrow, cf. docs/chantiers/SRIOV_IMPL.md) : le moteur tourne sur la VF (DPDK) ; la PF
            # reste kernel (ptp4l). On présente au moteur l'identité de la VF — pmd DPDK + VF BDF +
            # VF IP (sip) — tout en gardant ifname/cidr de la PF (pour qu'ensure_iface_ip pose l'IP
            # PF sur le netdev kernel → ptp4l L4). VF non provisionnée (vf_bdf absent) → repli af_xdp.
            if _pmd == "sriov":
                _vf_bdf = str(r.get("vf_bdf") or "").strip() or None
                _vf_ip = str(r.get("vf_ip") or "").strip() or None
                if _vf_bdf:
                    _pmd, _pci, _sip = "dpdk", _vf_bdf, (_vf_ip or _sip)
                else:
                    log.warning("iface %s: pmd=sriov mais vf_bdf absent (VF non provisionnée, "
                                "host-prep SR-IOV requis) → repli af_xdp", ifn)
                    _pmd = "af_xdp"
            rows.append({"ifname": ifn, "ip": _sip, "cidr": cidr,
                         # Passerelle DÉCLARÉE de ce port (node_interfaces.gateway). Elle n'a
                         # longtemps servi qu'à réserver l'adresse dans le pool conteneurs ; elle est
                         # désormais réellement posée — routage par leg côté kernel (af_xdp) et
                         # transmise à libmtl côté DPDK, cf. ensure_media_routes / env GATEWAYS.
                         "gateway": str(r.get("gateway") or "").strip() or None,
                         "network_id": r.get("media_network_id"),
                         # PMD du port (chantier DPDK) : NULL/'af_xdp' = chemin actuel ; 'dpdk' = port
                         # vfio-pci (BDF `pci`) ; 'sriov' (remappé dpdk+VF BDF ci-dessus, PF kernel).
                         "pmd": _pmd,
                         "pci": _pci,
                         # Profil d'émetteur ST 2110-21 (chantier narrow) : ''/NULL = auto |
                         # 'narrow'/'narrow_linear' → pacing RL device-wide | 'wide' → TSC.
                         # Dérive MTL_PACING (narrow-wins) dans _build_run_cmd (device-level).
                         "output_profile": str(r.get("output_profile") or "").strip().lower() or None,
                         # Alias opérateur (« PGM-Rouge ») pour l'affichage NIC (page 2110, sélecteur port).
                         "alias": str(r.get("alias") or "").strip() or None,
                         # pair_group peut être stocké en ENTIER (champ numérique de l'UI)
                         # → coercition str avant strip (sinon AttributeError dès qu'une
                         # paire est déclarée, qui tuait /api/io/mtl).
                         "pair_role": str(r.get("pair_role") or "").strip() or None,
                         "pair_group": str(r.get("pair_group") or "").strip() or None})
    if not rows and prim:                                 # repli : la NIC primaire seule
        _c = ((node or {}).get("media_ip") or "").strip()
        rows = [{"ifname": prim, "ip": _c.split("/")[0], "cidr": _c, "network_id": None,
                 "gateway": None}]
    rows.sort(key=lambda x: (x["ifname"] != prim, x["ifname"]))   # primaire en tête, déterministe
    return rows


def _sriov_node(node):
    """True si ≥1 interface media2110 du nœud est en mode SR-IOV (pmd='sriov'). Sert à plafonner le
    budget de files au format VF (8 leaves iavf) au lieu de celui de la PF (cf. SRIOV_VF_QUEUES)."""
    from .database import db_get_node_interfaces
    try:
        return any((r.get("pmd") or "").strip().lower() == "sriov"
                   for r in db_get_node_interfaces((node or {}).get("id"))
                   if r.get("role") == "media2110")
    except Exception:
        return False


def _has_dpdk_pf(node):
    """True si ≥1 interface media2110 est en **PF pleine DPDK** (pmd='dpdk', socle narrow full-PF —
    cf. mémoire narrow-full-pf-dpdk-socle). Dans ce mode il n'y a PLUS de netdev kernel sur la PF
    média → plus de `ptp4l` kernel → la SEULE source PTP est le PTP interne libmtl (synchro
    carte-directe, `ENGINE_PTP=libmtl`). ⚠ NE PAS confondre avec 'sriov' : là, la PF reste kernel et
    porte ptp4l, la VF-DPDK lit CLOCK_REALTIME → pas de PTP interne. Le PTP interne MTL s'auto-
    configure (adopte domaine 127 + transport UDP/L2 du GM, join 224.0.1.129 ; cf. mt_ptp.c)."""
    from .database import db_get_node_interfaces
    try:
        return any((r.get("pmd") or "").strip().lower() == "dpdk"
                   for r in db_get_node_interfaces((node or {}).get("id"))
                   if r.get("role") == "media2110")
    except Exception:
        return False


def _primary_network_id(mifs, node):
    """Réseau (media_network_id) de la NIC primaire = réseau « par défaut » de la répartition auto."""
    prim = ((node or {}).get("mtl_iface") or "").strip()
    for e in mifs:
        if e["ifname"] == prim:
            return e.get("network_id")
    return mifs[0].get("network_id") if mifs else None


def media_capacity_units(node):
    """**Unités de CAPACITÉ** média d'un nœud : `[{key, ifaces, kind, label}, …]`.

    C'est la granularité à laquelle on DÉCLARE (modèle de carte) et on BUDGÈTE, par opposition à
    l'ifname brut. Deux formes :
      · `kind="port"` — port autonome : une unité, un ifname ;
      · `kind="pair"` — paire SMPTE 2022-7 (red+blue) : **UNE seule** unité pour deux ifnames.

    Pourquoi une paire n'est pas deux unités : les deux legs portent le MÊME contenu (redondance de
    chemin, pas capacité supplémentaire). Chaque session y consomme une feuille RL sur CHACUN des
    deux ports — c'est déjà ce que compte le contrôleur (`_d[_ifc2] += 1`). Déclarer un modèle par
    port ferait donc déclarer deux fois la même chose, et afficherait une capacité double de la
    réalité. Corollaire commode : une paire se budgète EXACTEMENT comme un port isolé (mêmes slots,
    même coût en files, même débit sur chaque leg) — d'où une seule et même arithmétique.

    `key` = ifname CANONIQUE de l'unité (le leg `red` pour une paire) : les clés de layout déjà
    stockées (`tx_layout_<node>_<ifname>`) restent donc valides sans migration."""
    mifs = _media_ifaces(node) or []
    pairs = media_port_pairs(node)
    paired = {}
    for red, blue in pairs:
        paired[red] = (red, blue)
        paired[blue] = (red, blue)
    units, seen = [], set()
    for e in mifs:
        ifn = e["ifname"]
        if ifn in seen:
            continue
        if ifn in paired:
            red, blue = paired[ifn]
            seen.update((red, blue))
            units.append({"key": red, "ifaces": [red, blue], "kind": "pair",
                          "label": "%s + %s (2022-7)" % (red, blue)})
        else:
            seen.add(ifn)
            units.append({"key": ifn, "ifaces": [ifn], "kind": "port", "label": ifn})
    return units


def unit_of_iface(node, iface):
    """Unité de capacité qui CONTIENT `iface` (cf. media_capacity_units), ou None. Sert à accepter
    un ifname venant de l'UI (l'exploitant clique un port) et à le ramener à son unité de
    déclaration — en 2022-7, cliquer le leg blue doit ouvrir le modèle de la PAIRE."""
    for u in media_capacity_units(node):
        if iface in u["ifaces"]:
            return u
    return None


def engine_slot_ports(node, params, pins_key="tx_pins"):
    """Résolveur **slot → port** d'un moteur 2110 multi-port : `(auto_ports, slot_port_fn)`.

    MIROIR EXACT de `controller._tx_iface`/`_auto_iface` (plugins/2110_io/docker/controller.py) :
    épinglage explicite (`params[pins_key][str(i)]`) s'il désigne un port DÉCLARÉ, sinon répartition
    automatique `auto_ports[i % len(auto_ports)]` sur les ports du réseau PRIMAIRE. Toute vue qui
    raisonne « par port » (budget de files, layout, maintenance) DOIT passer par ici : une seconde
    implémentation dériverait du moteur, et une vue par-port fausse fait décider sur du vent —
    c'est le bug de la page « Modèles de carte 2110 » (2026-07-27), qui attribuait TOUS les slots à
    la NIC primaire alors que le moteur les alternait entre les deux ports.

    Mono-port (ou aucune NIC média) → `([ifname] , lambda i: ifname)` : le port unique possède tout.
    `slot_port_fn(i)` renvoie l'ifname, ou None si le nœud n'a aucune NIC média."""
    mifs = _media_ifaces(node) or []
    if not mifs:
        return [], (lambda i: None)
    if len(mifs) == 1:
        only = mifs[0]["ifname"]
        return [only], (lambda i: only)
    prim = _primary_network_id(mifs, node)
    auto_ports = [e["ifname"] for e in mifs if e.get("network_id") == prim] \
                 or [e["ifname"] for e in mifs]
    declared = {e["ifname"] for e in mifs}
    pins = (params or {}).get(pins_key) or {}

    def _slot_port(i):
        p = pins.get(str(i))
        if p in declared:
            return p
        return auto_ports[int(i) % len(auto_ports)] if auto_ports else None
    return auto_ports, _slot_port


def ensure_media_ips(node):
    """Pose (idempotent) l'IPv4 média de CHAQUE NIC media2110 + la monte. Multi-NIC : sans IP+UP sur
    chaque PF média, sip=0.0.0.0 sur cette NIC → TX rejeté / RX free-run. La primaire garde le message
    historique (`ensure_media_ip`). Retourne (ok_global, premier_msg_erreur)."""
    ok_all, first_err = True, ""
    pok, pmsg = ensure_media_ip(node)                     # primaire (media_ip de node)
    if not pok:
        ok_all, first_err = False, pmsg
    prim = ((node or {}).get("mtl_iface") or "").strip()
    for ent in _media_ifaces(node):
        if ent["ifname"] == prim or not ent["cidr"]:
            continue
        # Socle full-PF DPDK : un port pmd=dpdk est bindé vfio-pci → plus de netdev kernel
        # (`ip addr add` → « Cannot find device », alerte « RX/TX indisponibles » trompeuse à
        # chaque déploiement — vu sur dl360-1). L'IP média DPDK est portée par libmtl (sip
        # depuis node_interfaces.ip_cidr) → skip kernel, même logique que ensure_media_ip.
        if (ent.get("pmd") or "") == "dpdk":
            continue
        iok, imsg = ensure_iface_ip(node, ent["ifname"], ent["cidr"])
        if not iok:
            ok_all = False
            if not first_err:
                first_err = imsg
    return (ok_all, first_err)


# Table de routage dédiée par leg média. Base choisie hors des tables réservées (local=255,
# main=254, default=253) et hors de ce que posent ifupdown/Docker — un numéro, pas un nom, pour ne
# pas dépendre de /etc/iproute2/rt_tables.
_RT_TABLE_BASE = 210


def _media_route_cmd(iface, cidr, gateway, table):
    """Shell idempotent qui pose le routage d'UN leg média dans SA table dédiée, plus la règle qui
    l'y envoie.

    POURQUOI UNE TABLE PAR LEG. En 2022-7 le nœud a deux legs sur deux fabrics, chacun avec SA
    passerelle. Deux routes par défaut dans `main` ne cohabitent pas : la seconde est refusée (ou
    pire, l'emporte selon la métrique) et tout le trafic sortant d'un leg part par l'autre — un
    plan média asymétrique, qui « marche » jusqu'au jour où un des deux liens tombe. Le routage par
    source (`ip rule from <ip>`) est le seul montage qui tienne les deux legs simultanément.

    PAS DE PERSISTANCE, ET C'EST DÉLIBÉRÉ : comme l'IP média (`ensure_media_ip`), ces routes sont
    reposées à CHAQUE (re)déploiement du moteur — or sans moteur déployé il n'y a pas de plan média
    du tout. Le RDMA, lui, n'a aucun chemin équivalent : c'est pour ça que LUI est gravé."""
    ifq, gwq = shlex.quote(iface), shlex.quote(gateway)
    ip = cidr.split("/")[0]
    t = str(int(table))
    # `ip route` REFUSE un préfixe dont les bits d'hôte sont posés (« Invalid prefix for given
    # prefix length ») : 192.168.10.2/30 est une adresse, pas une route. On route le RÉSEAU.
    reseau = str(_ipaddress.ip_network(cidr, strict=False))
    return (
        # 1) le sous-réseau du leg, joignable directement (source forcée : sans `src`, le noyau
        #    choisirait l'IP primaire de l'hôte et le routeur verrait une source hors segment)
        f"ip route replace {shlex.quote(reseau)} dev {ifq} src {shlex.quote(ip)} table {t}; "
        # 2) la sortie par défaut DE CE LEG
        f"ip route replace default via {gwq} dev {ifq} table {t}; "
        # 3) la règle qui aiguille vers la table — `ip rule add` empile les doublons, on teste avant
        f"ip rule list | grep -qE 'from {ip} lookup {t}\\b' || "
        f"ip rule add from {shlex.quote(ip)} lookup {t}; ")


def ensure_media_routes(node):
    """Pose (idempotent) la passerelle DÉCLARÉE de chaque NIC média, en routage par leg.

    Jusqu'ici `node_interfaces.gateway` était stocké, affiché… et jamais appliqué : il ne servait
    qu'à réserver l'adresse dans le pool conteneurs. Fonctionnellement, le plan média n'en a pas
    besoin (l'IGMPv3 part en 224.0.0.22, le multicast TX dérive sa MAC du groupe, et un ping depuis
    le routeur directement attaché passe par la route connectée). Mais un port en /30 dont l'hôte
    doit joindre un unicast hors segment, lui, en a besoin — et l'intégration sur un plant
    micro-segmenté l'exige explicitement, une passerelle par fabric.

    Ports pmd=dpdk : IGNORÉS ICI, sans échec — ils n'ont plus de netdev, il n'y a rien à router
    dans le noyau. Leur passerelle part au moteur (env GATEWAYS/NETMASKS → mtl_init_params), qui
    est le seul à porter leur couche 3. Retourne (ok_global, premier_msg_erreur)."""
    if not (node and node.get("host")):
        return (True, "")
    parts = []
    for i, ent in enumerate(_media_ifaces(node)):
        if not (ent.get("cidr") and ent.get("gateway")):
            continue
        if (ent.get("pmd") or "") == "dpdk":
            continue                                  # pas de netdev : voir docstring
        parts.append(_media_route_cmd(ent["ifname"], ent["cidr"], ent["gateway"],
                                      _RT_TABLE_BASE + i))
    if not parts:
        return (True, "")
    # `set -e` OBLIGATOIRE : les commandes sont chaînées par `;`, donc le code de retour observé
    # est celui de la DERNIÈRE. Sans lui, un `ip route replace` refusé (préfixe invalide, iface
    # absente, passerelle hors segment) rendait rc=0 — l'échec passait pour un succès et la seule
    # trace partait sur stderr, que personne ne lisait. Mesuré au banc le 2026-08-22.
    rc, out, err = ssh_run(node["host"], "set -e; " + "".join(parts), timeout=20)
    blob = ((out or "") + (err or "")).strip()
    if rc == 0:
        return (True, "")
    log.warning("media_routes sur %s: rc=%s %s", node.get("host"), rc, blob[:200])
    return (False, blob[:200])


def _resolve_bdf_operstate(node, ifaces):
    """En UN SEUL SSH, pour chaque ifname ENCORE sur le driver kernel `ice` : résout le BDF PCI
    (basename readlink /sys/class/net/<if>/device) et l'operstate. Renvoie {ifname: {bdf, operstate}}.
    Un port DÉJÀ bindé vfio-pci n'a plus de netdev → absent du résultat (on retombera alors sur
    node_interfaces.pci pour le BDF). Best-effort : SSH KO → dict vide."""
    out = {}
    if not (node and node.get("host") and ifaces):
        return out
    parts = []
    for ifn in ifaces:
        q = shlex.quote(ifn)
        parts.append(
            f'b=$(basename "$(readlink /sys/class/net/{q}/device 2>/dev/null)" 2>/dev/null); '
            f's=$(cat /sys/class/net/{q}/operstate 2>/dev/null); '
            f'printf "%s|%s|%s\\n" {q} "$b" "$s"')
    rc, txt, err = ssh_run(node["host"], "; ".join(parts), timeout=15)
    for line in (txt or "").splitlines():
        f = line.strip().split("|")
        if len(f) == 3 and f[0]:
            out[f[0]] = {"bdf": (f[1] or "").strip(), "operstate": (f[2] or "").strip()}
    return out


def ensure_vfio_binds(node):
    """AUTO-BIND vfio-pci des PF media2110 en pmd=dpdk, AVANT le run du moteur 2110_io.

    Une PF déclarée pmd=dpdk mais restée sur le driver kernel `ice` (jamais bindée vfio-pci) fait
    crash-looper le moteur DPDK — SILENCIEUSEMENT — en « dev_eal_init fail -1 » (l'EAL ne peut pas
    s'attacher à un port kernel ; vécu dl360-1). On binde donc ici chaque PF dpdk pas-encore-vfio via
    `mtl.vfio_bind_apply` (idempotent : skip si déjà vfio ; persiste au boot). SR-IOV exclu (pmd=='sriov'
    ≠ 'dpdk' : la PF y RESTE kernel pour ptp4l, la VF est bindée par un autre chemin). Best-effort
    tolérant : un échec de bind ALERTE (jamais muet) mais ne plante pas le déploiement.
    Renvoie (ok_global, [ifnames bindés])."""
    from . import mtl
    from .database import db_get_node_interfaces
    nom = (node or {}).get("name") or (node or {}).get("host") or "?"
    rows = [r for r in (db_get_node_interfaces((node or {}).get("id")) or [])
            if r.get("role") == "media2110"
            and str(r.get("pmd") or "").strip().lower() == "dpdk"]
    if not rows:
        return (True, [])

    # PRÉCONDITION prép hôte : sans IOMMU actif + module vfio-pci + hugepages 1G, le bind (et le
    # moteur DPDK) est voué à l'échec → NE PAS lancer un déploiement crash-loop : alerte claire (quoi
    # régler) et on S'ABSTIENT du bind.
    prep = mtl.verifier_node(node)
    if prep.get("error"):
        db_add_alert("alert.docker.prep_non_sondable", "error",
                     node_id=(node or {}).get("id"), kind="prep",
                     params={"n": nom, "e": prep['error']})
        return (False, [])
    manques = [k for k in ("iommu_active", "vfio_present", "hugepages_size_ok") if not prep.get(k)]
    if manques:
        db_add_alert(
            "alert.docker.prep_incomplete",
            "error", node_id=(node or {}).get("id"), kind="prep",
            params={"n": nom, "m": ", ".join(manques)})
        return (False, [])

    # BDF + operstate résolus tant que le port est ENCORE sur ice (après bind il perd son netdev).
    live = _resolve_bdf_operstate(node, [(r.get("ifname") or "").strip() for r in rows])
    bindes, ok_all = [], True
    for r in rows:
        ifn = (r.get("ifname") or "").strip()
        info = live.get(ifn) or {}
        bdf = (str(r.get("pci") or "").strip()) or info.get("bdf") or ""
        if not bdf:
            db_add_alert("alert.docker.bdf_introuvable", "error",
                         node_id=(node or {}).get("id"), kind="prep",
                         params={"n": nom, "ifn": ifn})
            ok_all = False
            continue
        # Lien DOWN au moment du bind (lisible sur ice AVANT bind) : ni PTP ni trafic 2110 ne
        # passeront tant que le câble/switch n'est pas up → on prévient, sans bloquer.
        op = info.get("operstate")
        if op and op not in ("up", "unknown"):
            db_add_alert("alert.docker.pf_lien_down", "warning",
                         node_id=(node or {}).get("id"), kind="net",
                         params={"n": nom, "ifn": ifn, "op": op})
        try:
            bok, bmsg, _checks = mtl.vfio_bind_apply(node, bdf)
        except mtl.GardeFouVfio as g:
            db_add_alert("alert.docker.bind_refuse", "error",
                         node_id=(node or {}).get("id"), kind="prep",
                         params={"n": nom, "ifn": ifn, "bdf": bdf, "e": str(g)})
            ok_all = False
            continue
        except Exception as ex:
            db_add_alert("alert.docker.bind_erreur", "error",
                         node_id=(node or {}).get("id"), kind="prep",
                         params={"n": nom, "ifn": ifn, "bdf": bdf, "e": str(ex)})
            ok_all = False
            continue
        if bok:
            bindes.append(ifn)
        else:
            db_add_alert("alert.docker.bind_echoue", "error",
                         node_id=(node or {}).get("id"), kind="prep",
                         params={"n": nom, "ifn": ifn, "bdf": bdf, "msg": bmsg})
            ok_all = False
    if bindes:
        db_add_alert("alert.docker.bind_ok", "info",
                     node_id=(node or {}).get("id"), kind="prep",
                     params={"n": nom, "b": ", ".join(bindes)})
    return (ok_all, bindes)


# ─── Image ───────────────────────────────────────────────────────────
def _resolve_mtl_image(node):
    """Tag bobi-mtl du nœud : `node.image` si défini, sinon AUTO-DÉTECTION du tag réellement
    présent sur l'hôte (`docker images bobi-mtl`). Nécessaire car un nœud io2110 RÉ-ENRÔLÉ a
    souvent `image=NULL` : `node_driver.register` dérive l'image de l'inventaire agent, qui ne
    suit qu'un tag figé (`bobi-mtl:latest`) et rate donc le tag buildé (`bobi-mtl:0.22.x`). La
    valeur trouvée est persistée dans la DB → déploiements suivants + badge UI cohérents."""
    image = node.get("image")
    if image:
        return image
    host = node.get("host")
    if not host:
        return None
    rc, out, _ = ssh_run(host, "docker images --format '{{.Repository}}:{{.Tag}}' bobi-mtl 2>/dev/null",
                         timeout=15)
    tags = [t.strip() for t in (out or "").splitlines()
            if t.strip().startswith("bobi-mtl:") and not t.strip().endswith(":<none>")]
    if not tags:
        return None
    tags.sort(key=lambda t: t.endswith(":latest"))   # préfère un tag versionné à :latest
    found = tags[0]
    try:
        if node.get("id"):
            db_update_node(node["id"], image=found)
            node["image"] = found
    except Exception:
        log.warning("persist image auto-détectée %s pour nœud %s échouée", found, node.get("id"))
    return found


def verify_image(node):
    """(ok, msg) — l'image du nœud est-elle présente sur l'hôte Docker ?"""
    if not node or not node.get("host"):
        return (False, "nœud sans hôte")
    image = _resolve_mtl_image(node)
    if not image:
        return (False, "nœud sans image bobi-mtl (à builder sur le nœud)")
    rc, _, err = ssh_run(node["host"], f"docker image inspect {shlex.quote(image)} >/dev/null 2>&1",
                         timeout=20)
    if rc == 0:
        return (True, image)
    return (False, f"image {image} absente sur {node['host']} — la builder sur le nœud")


# ─── Cycle de vie ────────────────────────────────────────────────────
# Sérialise la garde « 1 conteneur MTL par nœud » : le check (SELECT) et l'INSERT de la ligne
# container n'étaient pas atomiques → deux créations concurrentes sur le même nœud passaient
# toutes les deux (conflit :8080/:8081/PF, --network host). Un seul processus orchestrateur
# écrit la table → lock in-process suffisant.
_create_mtl_lock = threading.Lock()

# Anti-spam de l'alerte « moteur à redéployer » (durcissement B, auto-provision) : on ne ré-alerte
# QUE si l'ensemble des NIC média non couvertes par le moteur EN MARCHE change (une édition
# d'interface qui n'y touche pas ne re-spamme pas). node_id → frozenset(ifnames manquants).
_engine_gap_state = {}
_engine_gap_lock = threading.Lock()


def _is_probe_type(type_):
    """Vrai si le type MTL est une SONDE à ports contrôleur offsetés (coexistable avec un moteur
    sur le même nœud). Détecté par le manifeste (deploy_defaults.probe_mode / controller_port_base)
    — pas d'égalité codée en dur sur « probe_2110 » (un futur type de sonde hérite du comportement)."""
    dd = (plugins.get(type_) or {}).get("deploy_defaults") or {}
    return bool(dd.get("probe_mode")) or dd.get("controller_port_base") is not None


def creer_container_docker(node_id, hostname=None, deploy_type="2110_io"):
    """Alloue un vmid synthétique (unicité globale → NMOS/PK/topologie inchangés) et
    enregistre une ligne backend='docker' rattachée au nœud. Pas de docker run ici
    (équivalent du clone LXC : l'exécution vient au deploy)."""
    node = db_get_node(node_id)
    if not node:
        db_add_alert("alert.docker.noeud_introuvable", "error",
                     node_id=node_id, kind="deploy", params={"node_id": node_id})
        return None

    ok, msg = verify_image(node)
    if not ok:
        db_add_alert("alert.docker.creation_echouee", "error",
                     node_id=node_id, kind="deploy", params={"n": node["name"], "msg": msg})
        return None

    from .database import db_get_containers
    from .docker_compute import is_mtl_type, _type_of
    with _create_mtl_lock:
        # v1 : 1 conteneur MTL « plein » par nœud (--network host → :8080-8082 + PF uniques).
        # EXCEPTION : une SONDE (probe_2110) coexiste avec le moteur sur le même nœud (banc de
        # conformité loopback : générateur port A + sonde port B) car elle offsette ses ports
        # contrôleur (CONTROLLER_PORT_BASE) ET tourne sur SA PF vfio dédiée distincte. La garde
        # n'interdit donc QUE deux moteurs « pleins » (mêmes ports 8080-8082, mêmes PF). On ne
        # compte que les containers MTL (pas les compute/macvlan coexistants).
        new_is_probe = _is_probe_type(deploy_type)
        for c in db_get_containers():
            if c.get("node_id") == node_id and is_mtl_type(_type_of(c)):
                if new_is_probe or _is_probe_type(_type_of(c)):
                    continue   # au moins l'un des deux est une sonde à ports offsetés → sûr
                db_add_alert("alert.docker.deja_moteur", "error",
                             node_id=node_id, kind="deploy",
                             params={"n": node["name"], "vmid": c["vmid"]})
                return None

        vmid = allocations.next_free_vmid()
        if vmid is None:
            return None   # plage de VMID épuisée → alerte déjà émise par next_free_vmid ; pas de ligne vmid=None
        if not hostname:
            hostname = nom_moteur_defaut(db_get_node(node_id), vmid)
        db_upsert_container_docker(vmid, hostname, node_id, _name(vmid, hostname), status="created")
        # Persiste le type DANS le verrou (revue M1 — anti-TOCTOU) : la garde « 1 moteur/nœud »
        # ci-dessus lit le type via _type_of(deploy_config). Tant que ce write n'a pas eu lieu, une
        # 2ᵉ création CONCURRENTE (auto-provision de 2 ports média configurés en rafale) lit un type
        # vide → is_mtl_type("") == False → franchit la garde → DEUX moteurs sur le nœud. On écrit
        # donc le type AVANT de relâcher `_create_mtl_lock`. (Sans ça il serait aussi perdu : db_upsert
        # n'écrit pas deploy_config → deploy_docker retomberait en dur sur MTL.)
        if deploy_type:
            db_update_deploy_config(vmid, deploy_type, {})
    db_update_node(node_id, status="up")
    db_add_alert("alert.docker.cree", "info",
                 vmid=vmid, node_id=node_id, kind="deploy",
                 params={"h": hostname, "vmid": vmid, "n": node["name"]})
    return vmid


def _engine_default_params():
    """Params par défaut du moteur 2110_io (deploy_defaults du plugin + coerce), comme le fait la
    route creer() pour une création palette. Repli {} si le plugin est absent."""
    try:
        from . import plugins
        if plugins.is_plugin("2110_io"):
            m = plugins.get("2110_io")
            return plugins.coerce_config("2110_io", dict(m.get("deploy_defaults") or {}))
    except Exception:
        pass
    return {}


def _stop_engine_script(vmid):
    """Arrête le SCRIPT du moteur (agent :8081/stop) SANS détruire le container : il reste `running`
    → la boucle de surveillance ne le relance pas (elle ne relance que sur statut Docker ≠ running),
    et il repart à la reconfig d'un port média (redéploiement). Best-effort."""
    try:
        from .addressing import get_container_ip
        from . import deploy
        ip = get_container_ip(vmid)
        if ip:
            deploy.agent_session().post(deploy.agent_url(ip, "/stop"), timeout=5,
                                        headers=deploy.agent_headers(vmid))
    except Exception:
        pass


def _engine_covered_ifaces(node, c):
    """Ensemble des ifnames que le conteneur moteur EN MARCHE couvre RÉELLEMENT, lu depuis son env
    baked (IFACES CSV en multi-port, sinon IFACE scalaire — cf. _build_run_cmd). set() vide si le
    conteneur est injoignable/absent (best-effort : on s'abstient alors d'alerter)."""
    host = (node or {}).get("host")
    if not host:
        return set()
    name = c.get("docker_name") or _name(c.get("vmid"))
    rc, out, _ = ssh_run(
        host, f"docker inspect -f '{{{{json .Config.Env}}}}' {shlex.quote(name)} 2>/dev/null",
        timeout=10)
    if rc != 0 or not (out or "").strip():
        return set()
    try:
        envlist = json.loads(out) or []
    except Exception:
        return set()
    envd = {}
    for kv in envlist:
        if isinstance(kv, str) and "=" in kv:
            k, v = kv.split("=", 1)
            envd[k] = v
    ifaces = (envd.get("IFACES") or "").strip()
    if ifaces:
        return {x.strip() for x in ifaces.split(",") if x.strip()}
    one = (envd.get("IFACE") or "").strip()
    return {one} if one else set()


def ensure_node_engine(node_id):
    """Auto-provisionne LE moteur 2110_io UNIQUE du nœud (bi-rôle RX+TX, multi-ports) selon ses ports
    média 2110 — idempotent, best-effort, à lancer dans un THREAD depuis le hook de config d'interface :
      - ≥1 interface role='media2110' AVEC IP  ET  aucun moteur → CRÉE + DÉPLOIE le moteur du nœud.
      - AUCUNE interface média            ET  moteur existant  → ARRÊTE son script (décision 2026-07-09 :
        arrêt, PAS destruction ; il repart à la reconfig d'un port).
      - moteur déjà présent avec port(s) média → NO-OP (l'attache d'une NIC ajoutée est prise au prochain
        (re)déploiement — on ne coupe pas les flux à chaud ici).
    Ne touche JAMAIS une sonde probe_2110 (elle coexiste légitimement avec le moteur, cf. garde de
    creer_container_docker)."""
    try:
        node = db_get_node(node_id)
        if not node:
            return
        from .database import db_get_containers, db_get_node_interfaces
        from .docker_compute import is_mtl_type, _type_of
        media = [r for r in db_get_node_interfaces(node_id)
                 if r.get("role") == "media2110" and (r.get("ip_cidr") or "").strip()]

        def _engine():
            return next((c for c in db_get_containers()
                         if c.get("node_id") == node_id
                         and is_mtl_type(_type_of(c)) and not _is_probe_type(_type_of(c))), None)

        engine = _engine()
        if media and not engine:
            # creer_container_docker prend _create_mtl_lock EN INTERNE (+ garde « 1 moteur/nœud ») → NE
            # PAS le reprendre ici (verrou non-réentrant). Sa garde couvre la course multi-interfaces :
            # la 2ᵉ création concurrente est refusée proprement (retourne None), on s'arrête alors.
            vmid = creer_container_docker(node_id, deploy_type="2110_io")
            if not vmid:
                return
            deploy_docker(vmid, _engine_default_params(), type_script="2110_io")
            db_add_alert("alert.docker.auto_provisionne", "info",
                         vmid=vmid, node_id=node_id, kind="deploy",
                         params={"vmid": vmid, "n": node["name"]})
        elif media and engine:
            # Moteur DÉJÀ présent : NE PAS redéployer à chaud (couperait tous les flux RX/TX). On
            # vérifie seulement que le moteur EN MARCHE couvre toutes les NIC média actuelles du nœud ;
            # si une NIC a été ajoutée et n'est pas encore prise en compte → ALERTE, PAS de coupure.
            # Anti-spam : n'émet qu'au CHANGEMENT de l'ensemble manquant (durcissement B).
            covered = _engine_covered_ifaces(node, engine)
            if covered:
                missing = {e["ifname"] for e in _media_ifaces(node)} - covered
                key = frozenset(missing)
                with _engine_gap_lock:
                    changed = _engine_gap_state.get(node_id) != key
                    _engine_gap_state[node_id] = key
                if missing and changed:
                    db_add_alert(
                        "alert.docker.redeploiement_requis_nic",
                        "warning", vmid=engine.get("vmid"), node_id=node_id, kind="deploy",
                        params={"vmid": engine.get("vmid"), "n": node["name"],
                                "m": ", ".join(sorted(missing))})
        elif not media and engine:
            _stop_engine_script(engine.get("vmid"))
            with _engine_gap_lock:
                _engine_gap_state.pop(node_id, None)   # moteur arrêté → purge l'état d'alerte B
            db_add_alert("alert.docker.arrete_sans_media", "info",
                         vmid=engine.get("vmid"), node_id=node_id, kind="deploy",
                         params={"vmid": engine.get("vmid"), "n": node["name"]})
    except Exception as e:
        db_add_alert("alert.docker.auto_provision_erreur", "error",
                     node_id=node_id, kind="deploy",
                     params={"node_id": node_id, "e": str(e)})


_reconcile_last = {}      # node_id → monotone du dernier passage (throttle)
_reconcile_seen = {}      # node_id → dernier cpuset moteur réconcilié (anti-répétition)


def reconcile_engine_pinning(node_id, throttle_s=60.0):
    """Réconcilie `node_core_alloc` + `nodes.compute_cpuset` avec le cpuset RÉELLEMENT POSÉ sur le
    conteneur moteur (docker inspect) — la SEULE source de vérité.

    Sans ça, la vérité de core_pool n'est rafraîchie qu'au (re)déploiement du moteur : une dérive
    née d'un changement de réglage (`mtl_service_cores`, `mtl_lcore_max`) ou d'un `docker update`
    manuel persiste indéfiniment, invisible — c'est exactement le bug de dl360-1 (2026-07-14 :
    core_pool croyait le moteur sur 0-18, Docker le pinnait sur 0-21, dont 19-21 « dédiés » à un mur
    multiview qui mettait 1,3-2 s par trame). Lecture SEULE côté nœud (docker inspect) : ne touche
    aucun conteneur, ne redéploie rien — seule la base est remise d'aplomb (+ alertes).
    Throttlé par nœud ; no-op si le cpuset n'a pas changé depuis le dernier passage."""
    import time as _t
    now = _t.monotonic()
    if now - _reconcile_last.get(node_id, 0.0) < throttle_s:
        return
    _reconcile_last[node_id] = now
    node = db_get_node(node_id)
    if not node or not node.get("host"):
        return
    from .database import db_get_containers
    from .docker_compute import is_mtl_type, _type_of
    eng = next((c for c in (db_get_containers() or [])
                if c.get("node_id") == node_id
                and is_mtl_type(_type_of(c)) and not _is_probe_type(_type_of(c))), None)
    if not eng or eng.get("status") != "running":
        return
    name = eng.get("docker_name") or _name(eng.get("vmid"))
    rc, out, _ = ssh_run(node["host"],
                         f"docker inspect -f '{{{{.HostConfig.CpusetCpus}}}}' {shlex.quote(name)}",
                         timeout=10)
    reel = (out or "").strip()
    if rc != 0 or not reel:
        return                          # moteur sans cpuset (mtl_pin_cores off) → rien à réconcilier
    if _reconcile_seen.get(node_id) == reel:
        return
    from . import core_pool
    cores = sorted(core_pool.parse_cpuset(reel))
    if not cores:
        return
    connus = core_pool.allocations_by_vmid(node_id).get(eng["vmid"]) or []
    _reconcile_seen[node_id] = reel
    if sorted(connus) == cores:
        return                          # base déjà conforme à la réalité
    log.warning("réconciliation moteur 2110 (nœud %s, vmid %s) : cpuset RÉEL %s ≠ enregistré %s",
                node_id, eng["vmid"], reel, core_pool.fmt_cpuset(connus))
    db_add_alert("alert.docker.derive_pinning", "warning",
                 vmid=eng["vmid"], node_id=node_id, kind="resource",
                 params={"n": node.get("name"), "vmid": eng["vmid"], "reel": reel,
                         "enr": core_pool.fmt_cpuset(connus) or "—"})
    core_pool.reserve_engine_cores(node_id, eng["vmid"], cores,
                                   core_of=core_pool.core_map_cached(node_id))


_sizing_last = {}         # node_id → monotone du dernier passage (throttle)
_sizing_seen = {}         # node_id → dernier ensemble d'écarts alerté (anti-répétition)


def _engine_expected_sizing(node, params):
    """Variables d'env qui DIMENSIONNENT le moteur 2110, recalculées avec la configuration COURANTE.

    Toutes sont posées au `docker run` et **relues nulle part ensuite** : un changement de réglage
    (format vidéo par défaut, quota par scheduler, cap de files TX, compteurs de slots) ne les
    rattrape qu'au prochain (re)déploiement. D'où le détecteur `reconcile_engine_sizing`.

    On appelle ici les MÊMES fonctions PURES que `_build_run_cmd` (`_auto_lcores`, `_node_rl_tx_cap`,
    `_default_video_format`, `_pcount`) — pas de recopie de formule : deux calculs séparés divergent,
    et c'est précisément ce qu'on cherche à détecter. Aucune n'a d'effet de bord (pas de réservation
    de cœurs, pas d'écriture) : sûr à appeler depuis la boucle de surveillance."""
    _df = _default_video_format()
    exp = {
        "LCORES":            _auto_lcores(node, params),
        "MTL_SCH_QUOTA_MBS": str(int(settings.get("mtl_sch_quota_mbs") or 2500)),
        "ACTIVE_RX_COUNT":   str(_pcount(params, "active_rx_count", 6)),
        "ACTIVE_TX_COUNT":   str(_pcount(params, "active_tx_count", 6)),
        "WIDTH":             str(int(params.get("width") or _df["width"])),
        "HEIGHT":            str(int(params.get("height") or _df["height"])),
        "FPS":               str(params.get("fps") or _df["fps"]),
    }
    # RL_TX_QUEUES_CAP n'est émis qu'en pacing narrow (RL) — même condition que _build_run_cmd.
    try:
        if (_derive_pacing(node) or (None, None))[0] == "rl":
            exp["RL_TX_QUEUES_CAP"] = str(_node_rl_tx_cap(node))
    except Exception:
        pass
    return exp


def _sizing_equal(a, b):
    """Égalité TOLÉRANTE à la mise en forme : « 50 » == « 50.0 », « 1,2,3 » == « 3,2,1 ».
    Sans ça le détecteur crierait sur un simple changement de formatage (float vs int) — un
    détecteur qui crie à tort est désarmé au bout de deux jours."""
    a, b = (a or "").strip(), (b or "").strip()
    if a == b:
        return True
    if "," in a or "," in b:      # listes de cœurs : comparer les ENSEMBLES
        try:
            return (sorted(int(x) for x in a.split(",") if x.strip())
                    == sorted(int(x) for x in b.split(",") if x.strip()))
        except ValueError:
            return False
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def reconcile_engine_sizing(node_id, throttle_s=300.0):
    """Détecte la DÉRIVE DE DIMENSIONNEMENT d'un moteur 2110 en marche : env réellement posé au
    `docker run` vs ce que la configuration courante impose. Lecture SEULE — ne redéploie rien,
    ne coupe rien : ALERTE (le redéploiement, disruptif, reste une décision de l'exploitant).

    POURQUOI (incident dl360-1, 2026-07-27) : le format vidéo par défaut du site est passé de 25 à
    50 fps APRÈS la création du moteur. `_auto_lcores` dimensionne au DÉBIT — 9 lcores suffisaient à
    25 fps, il en faut 15 à 50. Le conteneur a continué de tourner avec ses 9 lcores : libmtl a servi
    les 6 premières sessions puis refusé toutes les suivantes (`mt_sch_add_quota fail -12`,
    `no available lcore`) → RX 3 à 6 mortes, watchdog en boucle de recréation perpétuelle. Rien ne
    reliait la cause (un réglage changé) à l'effet (des RX qui ne démarrent pas) : c'est le trou que
    ce détecteur ferme. Il couvre la CLASSE entière — format, quota, `mtl_lcore_max`, compteurs de
    slots, cap de files TX — parce qu'il compare l'env RÉEL au recalcul, sans présumer du déclencheur.

    Le motif est celui de `ensure_node_engine` pour une NIC média ajoutée : signaler, pas couper.
    Throttlé par nœud ; ré-alerte uniquement quand l'ENSEMBLE des écarts change."""
    import time as _t
    now = _t.monotonic()
    if now - _sizing_last.get(node_id, 0.0) < throttle_s:
        return
    _sizing_last[node_id] = now
    node = db_get_node(node_id)
    if not node or not node.get("host"):
        return
    from .database import db_get_containers
    from .docker_compute import is_mtl_type, _type_of
    eng = next((c for c in (db_get_containers() or [])
                if c.get("node_id") == node_id
                and is_mtl_type(_type_of(c)) and not _is_probe_type(_type_of(c))), None)
    if not eng or eng.get("status") != "running":
        return
    try:
        params = (json.loads(eng.get("deploy_config") or "{}") or {}).get("params") or {}
    except Exception:
        return
    if not params:
        return                       # moteur jamais déployé → rien à comparer
    name = eng.get("docker_name") or _name(eng.get("vmid"))
    rc, out, _ = ssh_run(node["host"],
                         "docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' %s"
                         % shlex.quote(name), timeout=10)
    if rc != 0 or not (out or "").strip():
        return
    reel = {}
    for line in out.splitlines():
        k, _sep, v = line.partition("=")
        if _sep:
            reel[k.strip()] = v.strip()
    try:
        attendu = _engine_expected_sizing(node, dict(params))
    except Exception as e:
        log.warning("dérive dimensionnement moteur (nœud %s) : recalcul impossible : %s", node_id, e)
        return
    ecarts = [(k, reel.get(k, "—"), v) for k, v in attendu.items()
              if k in reel and not _sizing_equal(reel.get(k), v)]
    cle = frozenset(ecarts)
    if _sizing_seen.get(node_id) == cle:
        return
    _sizing_seen[node_id] = cle
    if not ecarts:
        return
    detail = " ; ".join(f"{k} : posé {r} → requis {a}" for k, r, a in ecarts)
    # Le manque de lcores est la dérive qui CASSE (sessions refusées) — les autres dégradent ou
    # mentent sur le format. On monte en 'error' dans ce cas pour que ça ne se noie pas.
    _lc = next((e for e in ecarts if e[0] == "LCORES"), None)
    grave = _lc is not None
    consequence = ""
    if _lc:
        _n_reel = len([x for x in _lc[1].split(",") if x.strip()])
        _n_att = len([x for x in _lc[2].split(",") if x.strip()])
        if _n_att > _n_reel:
            consequence = (f" Le moteur tourne avec {_n_reel} lcore(s) pour {_n_att} requis : libmtl "
                           f"REFUSERA les sessions au-delà de sa capacité de schedulers "
                           f"(« mt_sch_add_quota fail -12 » / « no available lcore ») — RX/TX "
                           f"muettes, watchdog en boucle.")
    log.warning("dérive dimensionnement moteur 2110 (nœud %s, vmid %s) : %s",
                node_id, eng["vmid"], detail)
    # `consequence` est une sous-phrase française conditionnelle (manque de lcores) : deux clés
    # complètes plutôt qu'un fragment collé dans un paramètre.
    _params = {"n": node.get("name"), "vmid": eng["vmid"], "detail": detail}
    if consequence:
        db_add_alert("alert.deploy.moteur_dimensionne_perime_lcores",
                     "error" if grave else "warning", vmid=eng["vmid"], node_id=node_id,
                     kind="deploy", params={**_params, "n_reel": _n_reel, "n_att": _n_att})
    else:
        db_add_alert("alert.deploy.moteur_dimensionne_perime",
                     "error" if grave else "warning", vmid=eng["vmid"], node_id=node_id,
                     kind="deploy", params=_params)


def backfill_node_engines():
    """Backfill au BOOT (best-effort, hors chemin critique, durcissement A) : un nœud qui a DÉJÀ ≥1
    interface media2110 AVEC IP mais AUCUN moteur ne se corrigeait qu'à la prochaine édition
    d'interface. On balaie tous les nœuds enrôlés et on (ré)enclenche ensure_node_engine sur ceux qui
    matchent. Idempotent (ensure_node_engine no-op si un moteur existe). NON bloquant, tolérant aux
    nœuds injoignables : on NE pose PAS d'alerte error pour un nœud simplement down au boot (garde
    verify_image silencieuse → alerte 'warning' différée au lieu du spam 'image absente' que
    cracherait creer_container_docker). À lancer dans un thread depuis main.py, APRÈS un délai laissant
    la flotte/les agents remonter."""
    from .database import db_get_nodes, db_get_node_interfaces, db_get_containers
    from .docker_compute import is_mtl_type, _type_of
    try:
        nodes = db_get_nodes()
        containers = db_get_containers()
    except Exception as e:
        log.error("backfill moteurs 2110_io : lecture DB échouée : %s", e)
        return
    for node in nodes:
        nid = node.get("id")
        try:
            media = [r for r in db_get_node_interfaces(nid)
                     if r.get("role") == "media2110" and (r.get("ip_cidr") or "").strip()]
            if not media:
                continue
            engine = next((c for c in containers
                           if c.get("node_id") == nid
                           and is_mtl_type(_type_of(c)) and not _is_probe_type(_type_of(c))), None)
            if engine:
                continue    # moteur déjà là → rien à faire (l'arrêt/l'attache sont gérés au hook d'édition)
            if not (node.get("host") or "").strip():
                continue    # nœud sans hôte → on ne peut rien provisionner
            # Garde de joignabilité SILENCIEUSE (verify_image ne pose aucune alerte). Nœud down au boot
            # → alerte 'warning' différée, PAS le 'error' de creer_container_docker → pas de spam.
            ok, msg = verify_image(node)
            if not ok:
                db_add_alert("alert.docker.provision_differe", "warning",
                             node_id=nid, kind="deploy",
                             params={"n": node.get("name"), "c": len(media), "msg": msg})
                continue
            log.info("backfill : nœud %s a %d port(s) média sans moteur → ensure_node_engine",
                     node.get("name"), len(media))
            ensure_node_engine(nid)
        except Exception as e:
            log.error("backfill moteur 2110_io (nœud %s) : %s", nid, e)


def _default_video_format():
    """Format vidéo par défaut des RÉGLAGES. Délègue à scripts.get_default_video_format (source
    de vérité partagée avec normalize_receiver_params)."""
    from .scripts import get_default_video_format
    return get_default_video_format({"video_formats": settings.get("video_formats"),
                                     "video_format_default": settings.get("video_format_default")})


def _mtl_reserves(node):
    """Réserves de files AF-XDP du moteur 2110 PAR INTERFACE media2110 (réglées dans node_interfaces,
    onglet Réglages → Réseau). Retourne (port_reserve, total_rx, total_tx) :
      - port_reserve = {iface: {rx,tx,hr}} — seules les clés EXPLICITEMENT réglées sont présentes
        (le moteur applique son plancher par défaut pour les autres). Passé en env PORT_RESERVE.
      - total_rx / total_tx = somme des réserves RX/TX explicites (0 si aucune) — sert à dimensionner
        les LCORES sur la capacité réservée (sinon on saturerait les cœurs avant les files).
    """
    try:
        from .database import db_get_node_interfaces
        ifaces = [r for r in db_get_node_interfaces(node["id"]) if r.get("role") == "media2110"]
    except Exception:
        ifaces = []
    port_reserve = {}
    total_rx = total_tx = 0
    for r in ifaces:
        nm = (r.get("ifname") or "").strip()
        if not nm:
            continue
        rx, tx, hr = r.get("rx_reserve"), r.get("tx_reserve"), r.get("queue_margin")
        ent = {}
        if rx is not None:
            ent["rx"] = int(rx); total_rx += int(rx)
        if tx is not None:
            ent["tx"] = int(tx); total_tx += int(tx)
        if hr is not None:
            ent["hr"] = int(hr)
        if ent:
            port_reserve[nm] = ent
    return port_reserve, total_rx, total_tx


# Plafond de files TX du mécanisme RL sur E810 dpdk (patch libmtl « arbre TM ramifié », 0.39.6 :
# le mur des 8 est levé, cf. docs/chantiers/DPDK_NARROW.md § Capacité RL). N'est un plafond QUE sous narrow (RL) —
# tsc/tsc_narrow ne construisent aucune hiérarchie TM → jamais bornés. Miroir de controller.py
# (RL_TX_QUEUES_CAP, env même nom) : sert au garde-fou contextuel côté orchestrateur.
RL_TX_QUEUES_CAP = 63

# SR-IOV (chantier narrow, cf. docs/chantiers/SRIOV_IMPL.md + banc 2026-07-08) : une VF iavf plafonne son arbre RL à
# 8 leaves PAR VF (« too many TCs » — 1 TC/VF, le multi-TC ADQ n'est pas négocié par le PMD DPDK).
# Donc par VF-port : budget de 8 files, dont 1 réservée système → 7 sessions TX narrow utilisables.
# ⚠ BORNE DURE : sur-demander fait échouer TOUT mtl_init (fatal, pas de dégradation) → on plafonne
# total_q ET le cap RL à ces valeurs quand pmd=sriov. Scaling = MULTI-VF (8 leaves/port, scheduler
# carte prouvé ≥128 leaves = 16 VF ; le vrai mur est hugepages/cœurs), PAS le VF multi-TC.
SRIOV_VF_QUEUES  = 8     # leaves RL par VF iavf (budget de files/port)
SRIOV_VF_TX_CAP  = 7     # sessions TX narrow utilisables/VF (8 − 1 file système)


def _node_rl_tx_cap(node):
    """Sessions TX narrow (RL) MAX par port pour la carte média du nœud. Le max EFFECTIF n'est PAS
    lisible du PMD (dev_info rapporte le mur ice natif 8 que patch_tm_hierarchy transcende ; cf.
    docs/chantiers/DPDK_NARROW.md §7) → connu, pas découvert. Priorité :
      1. profil MESURÉ (table nic_profiles, rempli par la qualification) — MATCH par modèle ;
      2. bibliothèque STATIQUE (mtl.nic_rl_tx_cap) — modèles connus ;
      3. plancher sûr (défaut mtl). SR-IOV = borne DURE VF (sur-demander fait échouer mtl_init)."""
    if _sriov_node(node):
        return SRIOV_VF_TX_CAP
    try:
        from .database import db_get_node_interfaces, db_all_nic_profiles
        from . import mtl as _mtl
        model = ""
        for r in db_get_node_interfaces(node["id"]):
            if r.get("role") == "media2110" and (r.get("model") or "").strip():
                model = r["model"].strip()
                break
        if model:
            # 1) profil MESURÉ (prime sur la biblio) : match par sous-chaîne de modèle (les deux
            #    viennent de la même détection → cohérents). Le plus spécifique gagne (modèle le + long).
            ml = model.lower()
            best = None
            for p in db_all_nic_profiles():
                if not (p.get("measured") and p.get("rl_tx_cap") and (p.get("model") or "")):
                    continue
                pm = p["model"].strip().lower()
                if pm in ml or ml in pm:
                    if best is None or len(pm) > len(best["model"].strip()):
                        best = p
            if best:
                return int(best["rl_tx_cap"])
            # 2) bibliothèque statique
            return _mtl.nic_rl_tx_cap(model)
    except Exception:
        pass
    return RL_TX_QUEUES_CAP


def _derive_pacing(node):
    """Dérive le MÉCANISME de pacing TX (MTL_PACING, device-level) du profil d'émetteur ST 2110-21
    (`output_profile`) des NIC média du nœud. **DÉFAUT = narrow** : le narrow (RL matériel) est sans
    contrainte réelle (≤63 senders vidéo/port, RX+TX stable) → une interface non configurée compte
    comme 'narrow'. Deux étages dans libmtl :
      - la CLASSE 2110-21 (narrow/NL/wide, cible VRX) est PAR SESSION (`ops.pacing`) → vrai per-port,
        câblé côté moteur (mtl_rx.c, à venir) ;
      - le MÉCANISME (RL vs TSC, `mtl_init_params.pacing`) est DEVICE-level → calculé ici :
          · 'rl' dès qu'une iface effective est narrow/NL (défaut inclus) — RL supporte des cibles
            VRX narrow ET wide par session, donc n'impose PAS le narrow aux autres ports ;
          · 'tsc' seulement si TOUTES les media2110 sont explicitement 'wide' (aucun besoin de RL).
    Émis SEULEMENT si le nœud a ≥1 port média **dpdk** : sur af_xdp le pacing est TSC d'office → on
    n'émet rien (ligne `docker run` OCTET-IDENTIQUE, anti-régression). Retourne (pacing|None, profs)."""
    try:
        from .database import db_get_node_interfaces
        rows = [r for r in db_get_node_interfaces(node["id"]) if r.get("role") == "media2110"]
    except Exception:
        return None, []
    if not rows:
        return None, []
    # Le pacing n'a d'effet qu'en DPDK ('dpdk' PF-vfio OU 'sriov' = VF DPDK) ; sur af_xdp pur on
    # n'émet rien (iso-comportement). 'sriov' → le moteur tourne sur la VF DPDK (RL narrow possible).
    if not any((r.get("pmd") or "").strip().lower() in ("dpdk", "sriov") for r in rows):
        return None, []
    # Profil effectif par iface : valeur explicite, sinon DÉFAUT narrow.
    profs = [(str(r.get("output_profile") or "").strip().lower() or "narrow") for r in rows]
    profs = [p if p in ("narrow", "narrow_linear", "wide") else "narrow" for p in profs]
    return ("tsc" if all(p == "wide" for p in profs) else "rl"), profs


def _est_video_mbs(fmt):
    """Débit fil ST 2110-20 estimé d'UNE session vidéo (Mb/s) pour le format `fmt`
    (`{width,height,fps}` ; clés absentes → format vidéo par défaut du site).

    Le transport st20 est TOUJOURS 4:2:2 10 bits (20 bits/pixel) quel que soit le pipeline shm,
    + ~10 % d'en-têtes RTP/UDP/IP. Entrelacé : fps = cadence TRAME (les 2 champs portent le même
    volume) — un 1080i50 (25 trames/s) pèse donc la MOITIÉ d'un 1080p50. Sert au dimensionnement des
    lcores — MTL fait ensuite sa propre comptabilité exacte par session (st20_get_bandwidth)."""
    _df = _default_video_format()
    fmt = fmt or {}
    w = int(fmt.get("width") or _df["width"])
    h = int(fmt.get("height") or _df["height"])
    fps = float(fmt.get("fps") or _df["fps"])
    return (w * h * fps * 20 * 1.10) / 1e6


def _slot_video_formats(params, cap_rx, cap_tx):
    """Formats vidéo RÉELS des slots RX/TX du moteur, un par slot ((liste_rx, liste_tx) de dicts).

    ★ NE PAS dimensionner sur le format GLOBAL (`params.width/height/fps`) : sur un moteur à entrées
    MIXTES il ne décrit AUCUN slot en particulier, et il n'est même pas stable —
    `services/nmos:_propagate_sdp_format` le réécrit depuis le SDP du DERNIER récepteur activé
    (le garde `slot_only` ne couvre que la repropagation au boot). Un resync qui réabonne 6 entrées
    dont la dernière est 1080i50 fait donc basculer le global à 25 fps « i », et tout calcul qui s'y
    fie sous-dimensionne le moteur de moitié. Vécu le 2026-07-27 sur dl360-1 : 8 lcores calculés au
    lieu des 11 nécessaires, sessions RX/TX refusées (`mt_sch_add_quota fail -12`).

    Sources de vérité PAR SLOT, les mêmes que celles que le moteur réalise :
      · RX → `params['rx_fmt'][str(idx)]` (posé du SDP à l'abonnement, par flux) ;
      · TX → `params['tx_slots'][i]` (format déclaré de la sortie).
    Slot sans format connu (réserve de files au-delà des flux déclarés) → format par défaut du site,
    qui est celui qu'un nouveau slot recevra.

    Bénéfice de bord : l'estimation par slot rejoint la comptabilité de libmtl (qui remplit ses
    schedulers au débit RÉEL de chaque session) — deux 1080i50 tiennent sur un scheduler là où le
    calcul global en réservait deux."""
    rx_fmt = params.get("rx_fmt") or {}
    tx_slots = params.get("tx_slots") or []
    rx = [dict(rx_fmt.get(str(i)) or {}) for i in range(max(0, int(cap_rx)))]
    tx = [{k: (tx_slots[i] or {}).get(k) for k in ("width", "height", "fps")}
          if i < len(tx_slots) else {} for i in range(max(0, int(cap_tx)))]
    return rx, tx


def sch_quota_mbs(node):
    """Quota d'un scheduler libmtl (Mb/s) POUR CE NŒUD — cascade, du plus spécifique au plus général :

      1. `nodes.sch_quota_mbs` — surcharge explicite de l'exploitant sur cette machine ;
      2. profil du MODÈLE de CPU (`cpu_profiles`, cf. database.init_db) ;
      3. réglage global `mtl_sch_quota_mbs` — défaut de site ;
      4. plancher 2500.

    POURQUOI une cascade. La capacité d'un scheduler est une propriété physique du cœur (fréquence,
    IPC, cache, bande passante mémoire). Elle vivait dans un réglage GLOBAL appliqué à toute la
    flotte : sur un nœud plus rapide c'est pessimiste (on gaspille des cœurs), sur un nœud plus lent
    c'est optimiste — et là on sous-dimensionne le moteur, ce qui ne se voit pas immédiatement mais se
    paie en sessions refusées ou en wedges sous jitter.

    ⚠ La valeur 2500 du réglage global n'est PAS une mesure : c'est un repli de sécurité choisi après
    l'incident Horace (2026-07-06), et son intention documentée (« ≈ 2×1080p50 ») contredit son effet
    réel — une session 1080p50 pèse 2281 Mb/s, donc à 2500 il n'en tient qu'UNE. Tant qu'aucune
    campagne de charge n'a ancré un profil, on reste sur ce repli prudent, et `measured` le dit."""
    try:
        v = int((node or {}).get("sch_quota_mbs") or 0)
        if v > 0:
            return max(500, v)
    except (TypeError, ValueError):
        pass
    try:
        from .database import db_get_cpu_profile
        prof = db_get_cpu_profile((node or {}).get("cpu_model") or "")
        if prof and prof.get("quota_mbs"):
            return max(500, int(prof["quota_mbs"]))
    except Exception as e:
        log.debug("sch_quota_mbs: profil CPU indisponible : %s", e)
    return max(500, int(settings.get("mtl_sch_quota_mbs") or 2500))


_lcore_clamp_seen = {}   # node_id → dernière troncature de lcores signalée (anti-répétition)


def _auto_lcores(node, params):
    """Liste de cœurs (lcores DPDK) du moteur MTL, DIMENSIONNÉE au DÉBIT à traiter.

    Le facteur limitant d'un scheduler libmtl (= 1 lcore) est le débit de paquets à parser/copier,
    pas le nombre de sessions — MTL remplit d'ailleurs ses schedulers par quota Mb/s
    (`data_quota_mbs_per_sch` = réglage `mtl_sch_quota_mbs`, injecté en env au déploiement).
    On dimensionne donc avec la MÊME manette : schedulers = ceil(débit total estimé / quota).
    Avant (« mtl_sessions_per_lcore ») les deux calculs se contredisaient — dimensionnement
    « 2 sessions/lcore » mais remplissage au quota 5000 → MTL tassait 4-5 sessions 1080p50 par
    scheduler en laissant le reste des lcores inutilisés → scheduler au bord de la saturation
    (boucle ≈ inter-paquet) → n'importe quel jitter faisait franchir la fenêtre OFO (~40 ms) →
    wedge des sessions en cascade (incident lab Horace, 14 h). Sans auto-dimensionnement du tout
    (ancien défaut figé `1,2,3`), une RX/TX en trop échouait à `no available lcore`.

    Si `node.lcores` est EXPLICITE (≠ vide/'auto'), on le respecte tel quel (override opérateur)."""
    explicit = (node.get("lcores") or "").strip()
    if explicit and explicit.lower() != "auto":
        return explicit
    import math
    from .routes.mtl_engine import _mtl_per_source_sessions
    quota = sch_quota_mbs(node)
    # Sessions RÉELLES (1 vidéo + N audio + M ANC par slot) : un slot consomme
    # `_mtl_per_source_sessions` sessions. Les auxiliaires (audio ~10 Mb/s, ANC ~1 Mb/s) pèsent
    # peu en débit mais existent dans la comptabilité MTL → forfait AUX_MBS chacune.
    AUX_MBS = 15
    per_rx = _mtl_per_source_sessions(params, "rx")
    per_tx = _mtl_per_source_sessions(params, "tx")
    # Capacité À COUVRIR = max(usage actif, réserve de files réglée par interface). Sinon, en réservant
    # des files pour une capacité supérieure à active_rx_count, on saturerait les LCORES (no available
    # lcore) avant les files lors d'un ajout à chaud. Les lcores suivent donc la capacité réservée.
    _, _res_rx, _res_tx = _mtl_reserves(node)
    cap_rx = max(_pcount(params, "active_rx_count", 6), _res_rx)
    cap_tx = max(_pcount(params, "active_tx_count", 6), _res_tx)
    n_aux = cap_rx * max(0, per_rx - 1) + cap_tx * max(0, per_tx - 1)   # audio/ANC en sus
    # Débit vidéo = SOMME des débits RÉELS slot par slot (1 session vidéo par slot), PAS
    # `n_slots × format_global` : le global ne décrit aucun slot sur un moteur mixte et bascule au
    # gré des abonnements (cf. _slot_video_formats).
    _fx, _ft = _slot_video_formats(params, cap_rx, cap_tx)
    total_mbs = sum(_est_video_mbs(f) for f in _fx + _ft) + n_aux * AUX_MBS
    n_workers = max(1, math.ceil(total_mbs / quota))
    base = max(1, int(settings.get("mtl_lcore_base") or 1))   # cœur de départ (jamais 0 = housekeeping noyau)
    cap  = max(2, int(settings.get("mtl_lcore_max")  or 16))  # plafond de sécurité (cœurs réservables MTL)
    # +1 lcore pour le CNI : depuis MTL_FLAG_DEDICATED_SYS_LCORE (mtl_rx.c), le scheduler système
    # (CNI/PTP/IGMP/ARP) ne PARTAGE plus son lcore avec la 1ʳᵉ session RX vidéo — il en consomme un
    # à lui seul. Sans ce +1, la dernière RX échouerait à « no available lcore » (le levier
    # MTL_FLAG_RX_SEPARATE_VIDEO_LCORE demande déjà 1 scheduler par RX vidéo). Cf. banc 2026-07-14 :
    # sch0 = CNI + RX vidéo #0 → 3-6 trames incomplètes / 10 s ; les RX seules → 50,000 fps, 0 perte.
    besoin = 1 + n_workers + 1 + 1                            # CNI + manager + schedulers + 1 marge
    n_lcores = min(cap, besoin)
    # ★ TRONCATURE JAMAIS SILENCIEUSE (incident dl360-1, 2026-07-27 23:55). Deux modèles de 32 sorties
    # vidéo, un par carte, passent CHACUN la validation de leur port (32 files sur 63, 66 Gb/s sur
    # 100) : le budget est PAR PORT et il était respecté. Mais leur somme — 64 sessions vidéo sur le
    # nœud — demande ~70 schedulers, et `min(cap, …)` ramenait ça à 16 SANS UN MOT. Le moteur partait
    # structurellement incapable de servir : 204 échecs de création dans son log, les 6 RX mortes,
    # tableau de bord au vert jusqu'à ce qu'on regarde les flux. Le plafond reste (c'est un garde-fou
    # de cœurs réels), mais il s'ANNONCE — le déploiement continue, l'exploitant sait pourquoi son
    # moteur ne servira pas tout ce qu'il a déclaré.
    if besoin > n_lcores:
        _sig = (node.get("id"), besoin, n_lcores)
        if _lcore_clamp_seen.get(node.get("id")) != _sig:
            _lcore_clamp_seen[node.get("id")] = _sig
            db_add_alert(
                "alert.docker.sous_dimensionne",
                "error", node_id=node.get("id"), kind="resource",
                params={"n": node.get("name") or node.get("id"), "besoin": besoin,
                        "rx": cap_rx, "tx": cap_tx, "lc": n_lcores, "cap": cap})
    else:
        _lcore_clamp_seen.pop(node.get("id"), None)
    return ",".join(str(c) for c in range(base, base + n_lcores))


def lcore_demand(node, params):
    """`(besoin, plafond, tronqué)` — combien de lcores le moteur RÉCLAMERAIT pour `params`, quel est
    le plafond `mtl_lcore_max`, et si le premier dépasse le second. Fonction PURE (aucune alerte,
    aucune écriture) : sert au PRÉ-VOL, pour refuser ou avertir AVANT d'écrire une déclaration que le
    nœud ne pourra pas servir. Le budget de files de `validate_slots` est PAR PORT et ne voit pas
    cette limite-là, qui est à l'échelle du nœud."""
    import math
    from .routes.mtl_engine import _mtl_per_source_sessions
    quota = sch_quota_mbs(node)
    per_rx = _mtl_per_source_sessions(params, "rx")
    per_tx = _mtl_per_source_sessions(params, "tx")
    _, _res_rx, _res_tx = _mtl_reserves(node)
    cap_rx = max(_pcount(params, "active_rx_count", 6), _res_rx)
    cap_tx = max(_pcount(params, "active_tx_count", 6), _res_tx)
    n_aux = cap_rx * max(0, per_rx - 1) + cap_tx * max(0, per_tx - 1)
    _fx, _ft = _slot_video_formats(params, cap_rx, cap_tx)
    total_mbs = sum(_est_video_mbs(f) for f in _fx + _ft) + n_aux * 15
    besoin = 1 + max(1, math.ceil(total_mbs / quota)) + 1 + 1
    cap = max(2, int(settings.get("mtl_lcore_max") or 16))
    return besoin, cap, besoin > cap


def _tls_host_dir(name):
    """Dossier HÔTE (nœud) où sont écrits les 3 PEM bind-montés dans le conteneur MTL."""
    return f"/run/bobi-tls/{name}"


def _tls_inject(vmid, name):
    """mTLS du moteur MTL (:8081, --network host). Le contrôleur (ce process) est l'autorité :
    il génère un cert feuille signé par la CA interne et l'injecte sur l'HÔTE du nœud (via le canal
    ssh déjà utilisé : le `docker run` est lancé en SSH, pas via l'agent-nœud). Convention IDENTIQUE
    au chemin conteneur standard : /etc/bobi-tls/{cert,key,ca}.pem → controller.py sert :8081 en
    HTTPS/mTLS quand ces fichiers existent, HTTP clair sinon.

    Retourne (prefix_shell, run_arg) :
      - prefix_shell : commandes à exécuter AVANT `docker run` (écrit les PEM en base64, key en 600) ;
      - run_arg      : l'argument `-v <dir>:/etc/bobi-tls:ro` (ou "" si pas de CA → rétro-compat HTTP).
    CA absente → ("", "") : le moteur reste en HTTP clair, rien ne change."""
    try:
        from . import ca
        if not ca.ca_available():
            return "", ""
        cert_pem, key_pem = ca.generate_leaf(
            common_name=f"mtl{vmid}", node_id=None, uri=f"bobi://container/{vmid}")
        _, _, ca_path = ca.controller_client_files()
        with open(ca_path, "rb") as f:
            ca_pem = f.read()
    except Exception as e:
        log.warning("mTLS moteur MTL non injecté (vmid %s): %s — repli HTTP clair", vmid, e)
        return "", ""
    d = _tls_host_dir(name)
    qd = shlex.quote(d)
    def _w(fname, data, mode):
        b64 = base64.b64encode(data).decode("ascii")
        p = shlex.quote(f"{d}/{fname}")
        # base64 -d sur l'hôte (évite tout souci de quoting du PEM multi-ligne)
        return (f"printf %s {shlex.quote(b64)} | base64 -d > {p}; chmod {mode} {p}; ")
    prefix = (f"rm -rf {qd}; mkdir -p {qd}; chmod 700 {qd}; "
              + _w("cert.pem", cert_pem, "644")
              + _w("key.pem",  key_pem,  "600")
              + _w("ca.pem",   ca_pem,   "644"))
    return prefix, f"-v {qd}:/etc/bobi-tls:ro "


def _build_run_cmd(vmid, node, params):
    """Construit la ligne `docker run` du contrôleur. mcast/port/pt NE sont PAS passés ici :
    ils arrivent par NMOS IS-05 → :8081/nmos/subscribe → SDP."""
    _df = _default_video_format()
    image    = node.get("image") or _resolve_mtl_image(node)   # tolère un nœud ré-enrôlé (image=NULL)
    name     = _name(vmid, params.get("hostname"))
    mxl      = node.get("mxl_mount") or "/dev/shm"
    # NIC média du nœud (primaire en tête). SOURCE DE VÉRITÉ UNIQUE de IFACE/SIP scalaires ET de
    # IFACES/SIPS/PORT_PMDS/PORT_BDFS multi-port → tout reste aligné sur la MÊME liste.
    _mifs    = _media_ifaces(node)
    # ── Sonde ST 2110 (probe_2110) : profil de MESURE, gaté sur probe_mode (un moteur 2110_io
    # normal N'a JAMAIS cette clé → sa ligne `docker run` reste OCTET-IDENTIQUE). La sonde reçoit
    # sur SA PROPRE PF vfio DÉDIÉE (jamais celle du moteur) → on RESTREINT _mifs au seul port choisi
    # (params['probe_iface'], un ifname de node_interfaces). Le garde-fou « pas la PF du moteur » est
    # appliqué à la sélection (routes/probe.py) ; ici on refuse juste un probe_iface absent/inconnu
    # (lancer la sonde sur toutes les NIC média serait un vol de la ressource du moteur). #17
    if params.get("probe_mode"):
        _pif = str(params.get("probe_iface") or "").strip()
        _match = [e for e in _mifs if e.get("ifname") == _pif] if _pif else []
        if not _match:
            raise ValueError(
                "sonde 2110 : interface média dédiée introuvable "
                + ("(probe_iface vide)" if not _pif
                   else "(« {} » n'est pas une NIC media2110 déclarée sur ce nœud)".format(_pif))
                + " — choisir une PF vfio libre distincte du moteur.")
        _mifs = _match   # la sonde ne voit QUE sa PF dédiée
    # IFACE scalaire = 1ʳᵉ NIC média DÉCLARÉE (node_interfaces), PAS node.mtl_iface : en mono-port
    # DPDK la NIC bindée par BDF peut ≠ la primaire du nœud. _media_ifaces retombe sur
    # (mtl_iface, media_ip) si aucune media2110 déclarée → strictement iso pour un nœud af_xdp bien
    # configuré (primaire == unique media2110, ou repli mono-NIC).
    iface    = (_mifs[0]["ifname"] if _mifs else node.get("mtl_iface")) or "ens1f0np0"
    lcores   = _auto_lcores(node, params)
    ring     = int(settings.get("shm_video_ring") or 8)   # suit le réglage (borné [2:8] par le formulaire)
    # Niveau de log du moteur libmtl (Réglages → MXL). "warning" = silencieux (défaut) ; ≥ INFO fait
    # sortir le dump de stats périodique de libmtl, volumineux, qui rend les logs illisibles. On
    # TRACE le niveau effectif dans params.mtl_log_level (persisté par deploy_docker →
    # db_update_deploy_config) pour qu'un voyant puisse refléter l'état RÉEL des moteurs qui
    # tournent (pas seulement le réglage courant, qui ne s'applique qu'au prochain déploiement).
    _mtl_node_id = (node or {}).get("id")
    _mtl_log_level = str(settings.setting_for("mtl_log_level", _mtl_node_id) or "warning").strip().lower()
    params["mtl_log_level"] = _mtl_log_level
    # Période du dump de stats libmtl (secondes), lue par mtl_rx.c:mtl_dump_period_env().
    #
    # ⚠ « Bobi ne consomme PAS ce dump » ÉTAIT FAUX, et l'a payé cher. Dans libmtl, la fonction qui
    # imprime les stats PTP se termine par `ptp_stat_clear(ptp)` : le dump N'EST PAS QU'UN LOG,
    # c'est aussi le SEUL endroit qui remet à zéro la fenêtre de statistiques. La repousser à ~18 h
    # « pour ne pas collecter inutilement » gelait donc deux mesures que le produit PUBLIE :
    #   · `path_delay_ns` = stat_path_delay_sum / stat_path_delay_cnt → moyenne cumulée sur toute la
    #     fenêtre ; après quelques minutes elle ne bouge plus (mesuré : 184 ns constant sur 85
    #     relevés consécutifs à 1 Hz, σ = 0). Le graphe « Mean path delay » était plat PAR
    #     CONSTRUCTION et ne pouvait signaler aucun changement de transit.
    #   · `raw_delta_ns` = stat_delta_max → le PIRE écart de la fenêtre, donc un pic de convergence
    #     du démarrage gravé pour 18 h (mesuré : 628 004 ns constant, affiché « Δ 628.00 µs » comme
    #     s'il décrivait le présent, alors que le régime est ~1,3 µs).
    # On garde donc TOUJOURS la période par défaut de la lib : c'est `MTL_LOG_LEVEL=warning` qui
    # supprime la SORTIE (ces lignes sont des `notice()`), pas la période. Le coût résiduel est un
    # parcours de stats toutes les quelques secondes, sans une ligne de journal.
    _mtl_dump_period = "0"
    env = {
        "HOSTNAME_RX": _hostname_moteur(vmid, params),
        "VIDEO_COUNT": str(int(params.get("video_count") or 1)),   # slots RX (le contrôleur lit aussi RX_COUNT)
        "AUDIO_COUNT": str(int(params.get("audio_count") or 0)),   # slots RX audio (st30) ; 0 = pas d'audio
        "ANC_COUNT":   str(int(params.get("anc_count") or 0)),     # slots RX ANC (st40) ; 0 = pas d'ANC
        "TX_COUNT":    str(int(params.get("tx_count") or 0)),      # slots TX (émetteurs) ; 0 = receiver pur
        "IFACE":       iface,
        "LCORES":      lcores,
        "RING":        str(ring),
        "WIDTH":       str(int(params.get("width") or _df["width"])),
        "HEIGHT":      str(int(params.get("height") or _df["height"])),
        "FPS":         str(params.get("fps") or _df["fps"]),
        "CHROMA":      str(params.get("chroma") or _df["chroma"]),
        "BIT_DEPTH":   str(int(params.get("bit_depth") or _df["bit_depth"])),
        # Ptime audio (ST 2110-30) par DÉFAUT, ms — repli quand le SDP n'a pas d'a=ptime (le SDP
        # prime, auto par entrée). Réglable par installation (Réglages → MXL).
        "AUDIO_PTIME":      str(settings.get("mtl_audio_ptime") or "1"),
        "ACTIVE_RX_COUNT": str(_pcount(params, "active_rx_count", 6)),
        "ACTIVE_TX_COUNT": str(_pcount(params, "active_tx_count", 6)),
        # Quota Mb/s par scheduler (lcore) libmtl — LA manette de calibrage CPU (remplissage des
        # schedulers, dimensionnement _auto_lcores et garde-fou d'admission en dérivent tous).
        # Réglable par installation (Réglages → MXL).
        "MTL_SCH_QUOTA_MBS": str(int(settings.get("mtl_sch_quota_mbs") or 2500)),
        # Niveau de log libmtl (Réglages → MXL). Défaut "warning" = supprime le dump de stats
        # volumineux. Lu par mtl_rx.c (valeur inconnue → warning).
        "MTL_LOG_LEVEL":     _mtl_log_level,
        # Période du dump de stats libmtl : neutralisé en silencieux, défaut lib en diagnostic.
        # Cf. commentaire au calcul de _mtl_dump_period ci-dessus.
        "MTL_STAT_DUMP_PERIOD": _mtl_dump_period,
        # a=source-filter (SSM) dans les SDP TX — désactivable sur fabric L2 snooping pur
        # (réglage du service NMOS : Réglages → Protocoles → NMOS).
        "SDP_SOURCE_FILTER": "1" if settings.get("nmos_sdp_source_filter", True) else "0",
    }
    # ── Sonde : active le parser de conformité 2110-21 déjà présent dans mtl_rx (env-gaté
    # TIMING_PARSER=1, défaut OFF côté image, cf. docs/reference/PROBE_2110.md § « Cœur posé »). Le contrôleur
    # bobi-mtl lance alors chaque session RX avec MTL_FLAG_ENABLE_HW_TIMESTAMP +
    # ST20P_RX_FLAG_TIMING_PARSER_META et publie le verdict (compliant/cinst/vrx/vrx_span/fpt/
    # latency) sur :8080. N'est émis QUE pour la sonde (2110_io reste inchangé). #17
    if params.get("probe_mode") or params.get("timing_parser"):
        env["TIMING_PARSER"] = "1"
    # ── Offset de ports contrôleur (--network host) : une sonde déployée sur le MÊME nœud qu'un
    # moteur (banc loopback) doit écouter sur d'autres ports que les :8080-8082 du moteur. N'émis QUE
    # si params.controller_port_base est posé (deploy_defaults de la sonde) → un moteur 2110_io normal
    # n'a JAMAIS cette clé et garde une ligne `docker run` OCTET-IDENTIQUE. Lu côté orchestrateur par
    # deploy.controller_port_base(vmid) pour cibler le bon port (métriques :base, agent :base+1). #17
    _cpb = params.get("controller_port_base")
    if _cpb:
        env["CONTROLLER_PORT_BASE"] = str(int(_cpb))
    # Réserve de files PAR INTERFACE réglée par l'opérateur (node_interfaces.media2110). Le moteur
    # l'utilise comme plancher de réserve par port → capacité « à chaud » prévisible sans ré-init.
    # Absent → le moteur applique son plancher par défaut (rétro-compat 0.34.4).
    _port_reserve, _, _ = _mtl_reserves(node)
    if _port_reserve:
        env["PORT_RESERVE"] = json.dumps(_port_reserve, separators=(",", ":"))
    # sip explicite = IP de segment de la 1ʳᵉ NIC média DÉCLARÉE (MÊME source que IFACE ci-dessus et
    # que SIPS[0] en multi-port) → SIP↔IFACE↔PORT_BDFS toujours cohérents. En PMD DPDK libmtl forge
    # lui-même l'IGMP report depuis ce sip (le kernel ne fait plus le join) : il DOIT être l'IP réelle
    # du port physique bindé, sinon le switch (IGMP snooping) ne forwarde jamais le mcast → rx=0
    # (bug dl360-1 : sip=.99 de la NIC sœur ens1f0np0 poussé sur ens1f1np1/.229, masqué en af_xdp car
    # le netdev kernel .229 faisait le join). En DPDK _detect_iface_ip est impossible (port en vfio) →
    # l'IP DOIT venir de la DB. Repli media_ip_addr uniquement si aucune NIC média déclarée (mono-NIC).
    _sip = (_mifs[0]["ip"] if _mifs else "") or media_ip_addr(node)
    if _sip:
        env["SIP"] = _sip
    # Garde-fou DPDK : un port pmd=dpdk sans IP de segment (node_interfaces.ip_cidr) est en vfio →
    # aucune auto-détection kernel possible ; pousser sip=vide/faux donne rx=0 SILENCIEUX. On refuse
    # le déploiement avec une erreur claire plutôt que de lancer un moteur muet.
    _dpdk_no_ip = [e["ifname"] for e in _mifs
                   if (e.get("pmd") or "").strip() == "dpdk" and not (e.get("ip") or "").strip()]
    if _dpdk_no_ip:
        raise ValueError(
            "port(s) DPDK sans IP de segment (node_interfaces.ip_cidr) : "
            + ", ".join(_dpdk_no_ip)
            + " — l'IP média est obligatoire en PMD dpdk (port en vfio, pas d'auto-détection kernel)")
    # Multi-NIC : déclarer TOUTES les NIC média 2110 (node_interfaces) au moteur multi-port. IFACE/SIP
    # restent la 1ʳᵉ NIC. SOCLE : pas de map d'assignation par-slot (RX/TX_IFACE_MAP) → toutes les
    # sessions tombent sur la NIC primaire (iso-comportement) ; l'UI d'assignation par-slot l'« allume ».
    # N'émis QUE si ≥2 NIC média → un nœud mono-NIC est strictement inchangé (pas d'env IFACES/SIPS).
    _n_auto_ports = 1
    # MASQUE + PASSERELLE PAR PORT → libmtl (mtl_init_params.netmask/gateway). Émis ENSEMBLE et
    # SEULEMENT si au moins une passerelle est déclarée : sans passerelle, annoncer un masque
    # changerait à lui seul la décision « sur le lien ou via le routeur » de libmtl, donc le
    # comportement ARP d'un parc qui tourne. Rien de déclaré → aucune variable, iso-comportement.
    # C'est le pendant DPDK de `ensure_media_routes` (qui, lui, ne peut rien pour un port en vfio).
    if any(e.get("gateway") for e in _mifs):
        def _mask_de(cidr):
            try:
                return str(_ipaddress.ip_network(cidr, strict=False).netmask)
            except Exception:
                return ""
        env["NETMASKS"] = ",".join(_mask_de(e.get("cidr") or "") for e in _mifs)
        env["GATEWAYS"] = ",".join(e.get("gateway") or "" for e in _mifs)
    if len(_mifs) > 1:
        env["IFACES"] = ",".join(e["ifname"] for e in _mifs)
        env["SIPS"]   = ",".join(e["ip"] for e in _mifs)
        # Réseau de chaque port (regroupe les ports interchangeables) + réseau primaire (défaut de
        # la répartition auto). Le contrôleur répartit les sessions non épinglées sur les ports du
        # réseau primaire (modulo slot) ; un slot épinglé (RX_PINS/TX_PINS) court-circuite.
        env["PORT_NETS"]   = ",".join("" if e.get("network_id") is None else str(e["network_id"]) for e in _mifs)
        _prim_net = _primary_network_id(_mifs, node)
        if _prim_net is not None:
            env["PRIMARY_NET"] = str(_prim_net)
        # Épinglages persistés (survivent au redéploiement) : {slot: ifname}. Filtrés sur les ports
        # réellement déclarés (un ifname disparu est ignoré → repli auto côté contrôleur).
        _decl = {e["ifname"] for e in _mifs}
        def _pins(key):
            raw = params.get(key) or {}
            out = {str(k): v for k, v in raw.items() if v in _decl}
            return out
        _rxp, _txp = _pins("rx_pins"), _pins("tx_pins")
        if _rxp:
            env["RX_PINS"] = json.dumps(_rxp)
        if _txp:
            env["TX_PINS"] = json.dumps(_txp)
        # Appariement SMPTE 2022-7 (red/blue) : "ifA:ifB[,ifC:ifD…]" par pair_group. Le contrôleur
        # en dérive _pair_iface(iface) → iface du leg redondant d'une session dual-leg. N'émis que
        # si une paire complète (red+blue d'un même pair_group) est déclarée.
        _pairs = [f"{a}:{b}" for a, b in media_port_pairs(node)]
        if _pairs:
            env["PORT_PAIRS"] = ",".join(_pairs)
        # Nombre de ports du réseau primaire = diviseur du headroom (la charge est répartie dessus).
        _n_auto_ports = max(1, sum(1 for e in _mifs if e.get("network_id") == _prim_net)) if _prim_net is not None \
                        else len(_mifs)
    # ── PMD par port (chantier DPDK, opt-in par interface : node_interfaces.pmd='dpdk') ──
    # PORT_PMDS/PORT_BDFS = CSV alignés sur IFACES (et sur IFACE seule en mono-NIC). N'émis QUE si
    # ≥1 port dpdk + montages vfio/DDP associés → un nœud 100 % af_xdp garde une commande
    # `docker run` OCTET-IDENTIQUE (règle anti-régression n°1 du chantier, cf. docs/chantiers/DPDK_NARROW.md).
    dpdk_v = ""
    if any((e.get("pmd") or "").strip() == "dpdk" for e in _mifs):
        env["PORT_PMDS"] = ",".join(((e.get("pmd") or "").strip() or "af_xdp") for e in _mifs)
        env["PORT_BDFS"] = ",".join(((e.get("pci") or "").strip()
                                     if (e.get("pmd") or "").strip() == "dpdk" else "")
                                    for e in _mifs)
        # CLASSE 2110-21 PAR PORT (#26) : profil d'émetteur (narrow|narrow_linear|wide) → cible VRX
        # PAR SESSION TX (ops.transport_pacing, mtl_rx.c), distinct du MÉCANISME device MTL_PACING
        # (_derive_pacing, narrow-wins). CSV aligné sur IFACES ; défaut narrow (le plus strict).
        # Émis avec les autres clés dpdk — cf. le bloc « POURQUOI » juste après le `if` : hors dpdk
        # la classe n'a AUCUN effet mesurable sur le fil, et l'émettre rendrait le SDP menteur.
        env["PORT_PROFILES"] = ",".join(
            (lambda p: p if p in ("narrow", "narrow_linear", "wide") else "narrow")(
                (e.get("output_profile") or "").strip().lower() or "narrow")
            for e in _mifs)
        # vfio : accès aux groupes IOMMU du/des BDF bindés vfio-pci (host-prep = Phase 2) ;
        # DDP : le PMD ice DPDK charge le package depuis le chemin firmware standard (sans lui,
        # E810 en Safe Mode → ni RSS ni fdir). Le conteneur est déjà --privileged.
        dpdk_v = ("-v /dev/vfio:/dev/vfio "
                  "-v /lib/firmware/intel/ice/ddp/ice.pkg:/lib/firmware/intel/ice/ddp/ice.pkg:ro ")
    # ── POURQUOI `PORT_PROFILES` N'EST ÉMIS QUE SUR UN NŒUD DPDK (mesuré, 2026-08-06) ──
    # La clé vit dans le bloc dpdk ci-dessus. J'ai cru voir là un défaut — sur un nœud 100 % AF-XDP
    # la classe est inatteignable, et `output_profile` (réglage PAR INTERFACE qui existe en base) y
    # est donc ignoré — et je l'ai « corrigé » en l'émettant inconditionnellement (e559cd0).
    # C'ÉTAIT UNE ERREUR, à deux niveaux, et la mesure a tranché les deux.
    #
    # 1. La classe ne change RIEN au fil en AF-XDP. Capture à Horace des arrivées de notre propre
    #    flux, horodatées PAR LA CARTE (les horodatages logiciels sont inutilisables : `rx-usecs=50`
    #    les fait coller par paquets) — trois classes, trois recréations du moteur, même sortie :
    #        wide          66,5 % de paquets collés au débit ligne, salves moy 3,0 / max 16
    #        narrow        66,7 %                                    salves moy 3,0 / max 14
    #        narrow_linear 65,5 %                                    salves moy 2,9 / max 15
    #    Pour mémoire, une source professionnelle du même site, même instrument, même chemin :
    #        0,0 % de paquets collés, et exactement autant de silences que de retours de trame.
    #    Le pacing logiciel émet par salves quoi qu'on lui demande ; seul le limiteur MATÉRIEL
    #    (DPDK/vfio) espace les paquets. Le commentaire de `node_network.py` avait raison de qualifier
    #    le sélecteur de profil de contrôle MUET hors dpdk.
    #
    # 2. Émettre la clé rend le SDP MENTEUR. Sans elle, `_port_profile_effectif` retombe sur `wide`
    #    — la déclaration honnête d'un port pacé en logiciel. Avec elle et un `output_profile=narrow`
    #    en base (le défaut, et la valeur réelle à Horace), le SDP repart en `TP=2110TPN` alors qu'on
    #    émet en salves : exactement la promesse que le récepteur fait payer, et que la 0.80.0 avait
    #    supprimée à raison.
    #
    # Donc : on laisse la clé dans le bloc dpdk. Le repli `wide` n'est pas un oubli, c'est le
    # comportement correct. Rendre `output_profile` effectif hors dpdk exigerait d'abord une mécanique
    # capable de tenir la classe — pas une variable d'environnement de plus.
    # Pré-réservation de files AF-XDP (MTL_*_QUEUE_HEADROOM, lus par le contrôleur). Le daemon mtl_rx
    # fixe son nombre de files à mtl_init (1ᵉ lancement, déclenché par la 1ʳᵉ activation NMOS → souvent
    # UNE seule session). Sans headroom il naît dimensionné pour ~1 file → toute activation suivante
    # échoue en `rx_create_failed` jusqu'à un relancement disruptif du daemon. On réserve donc d'emblée
    # de quoi couvrir TOUTE la capacité active provisionnée (active_rx/tx × files par source, cf.
    # _mtl_per_source_sessions), bornée par le budget de files de la NIC → activations à chaud, 0 restart.
    try:
        from .routes.mtl_engine import _mtl_per_source_sessions
        from .routes.shared import _mtl_total_queues
        per_rx = _mtl_per_source_sessions(params, "rx")
        per_tx = _mtl_per_source_sessions(params, "tx")
        active_rx = _pcount(params, "active_rx_count", 6)
        active_tx = _pcount(params, "active_tx_count", 6)
        total_q = _mtl_total_queues()
        # SR-IOV : budget de files PAR VF-port = 8 (leaves RL iavf), pas les ~48 de la PF. Borne dure :
        # au-delà, mtl_init échoue (fatal). Le headroom (clampé ci-dessous à total_q−2) tient alors
        # dans les 8 leaves de chaque VF. Scaling = plus de VF-ports (÷ _n_auto_ports déjà géré).
        if _sriov_node(node):
            total_q = min(total_q, SRIOV_VF_QUEUES)
        hr_rx = max(0, active_rx * per_rx - 1)   # −1 : ≥1 session existe déjà au lancement
        hr_tx = max(0, active_tx * per_tx)
        # Multi-ports : la charge est répartie sur _n_auto_ports → le headroom devient PAR PORT
        # (le contrôleur l'applique à chaque port auto). ÷ ports = pas de sur-réservation.
        if _n_auto_ports > 1:
            hr_rx = -(-hr_rx // _n_auto_ports)   # ceil-div (réserve suffisante par port)
            hr_tx = -(-hr_tx // _n_auto_ports)
        if hr_rx + hr_tx > total_q - 2:          # garde-fou budget NIC (anti-ENOMEM), par port
            scale = (total_q - 2) / float(hr_rx + hr_tx)
            hr_rx, hr_tx = int(hr_rx * scale), int(hr_tx * scale)
        env["MTL_RX_QUEUE_HEADROOM"] = str(hr_rx)
        env["MTL_TX_QUEUE_HEADROOM"] = str(hr_tx)
    except Exception as e:
        log.warning("MTL queue headroom non calculé (vmid %s): %s", vmid, e)
    # ── Pacing TX 2110-21 (chantier narrow) : MTL_PACING dérivé du profil d'émetteur des NIC média
    # (node_interfaces.output_profile), narrow-wins, DEVICE-LEVEL. N'émis QUE si ≥1 profil posé →
    # sans profil, aucune clé → ligne `docker run` OCTET-IDENTIQUE (nœud actuel/af_xdp intact). Le
    # contrôleur (0.39.7) applique pacing=rl → RL matériel (narrow) ; pacing=tsc → wide. #24
    _pacing, _profs = _derive_pacing(node)
    if _pacing:
        env["MTL_PACING"] = _pacing
        # Garde-fou contextuel : sous narrow (RL), le mécanisme RL est plafonné à RL_TX_QUEUES_CAP
        # files TX/port (E810 0.39.6). active_tx est le total nœud → réparti sur les ports auto.
        # Au-delà, le contrôleur borne (min tx_queues) ; le repli documenté est tsc_narrow (sans
        # plafond, coût CPU). On avertit — l'admin voit pourquoi la capacité narrow est bornée.
        if _pacing == "rl":
            _atx = _pcount(params, "active_tx_count", 6)
            _atx_port = -(-_atx // max(1, _n_auto_ports))   # ceil-div : TX/port
            # Cap RL PAR PORT depuis la BIBLIOTHÈQUE DE CARTES (mtl.nic_rl_tx_cap) : 7 sur une VF SR-IOV
            # (borne DURE), sinon selon la carte média (E810-C=63 ; autres à qualifier). Le max effectif
            # n'est pas lisible du PMD (cf. §7) → connu par modèle, pas découvert.
            _rl_cap = _node_rl_tx_cap(node)
            # ÉMIS en env : le contrôleur borne DESSUS la réserve ET les sessions émises (sinon demande >
            # réserve → boucle de relance). Card-agnostic via la biblio ; overridable (banc).
            env["RL_TX_QUEUES_CAP"] = str(_rl_cap)
            if _atx_port > _rl_cap:
                log.warning("2110_io vmid %s : profil narrow (RL) actif mais ~%d TX/port > cap RL %d "
                            "%s— capacité narrow bornée (ajouter des VF-ports pour scaler ; cf. "
                            "docs/chantiers/SRIOV_IMPL.md/DPDK_NARROW.md)", vmid, _atx_port, _rl_cap,
                            "(VF SR-IOV : sur-demander fait ÉCHOUER mtl_init) " if _sriov_node(node) else "")
    # ── PTP carte-directe (socle narrow full-PF DPDK) : sur une PF PLEINE en DPDK il n'y a plus de
    # netdev kernel → plus de ptp4l/phc2sys kernel → le moteur DOIT faire son propre PTP (esclave
    # PTPv2 sur le port DPDK, lit+discipline le PHC). C'est la SEULE horloge disponible dans ce mode.
    # MTL s'auto-configure (domaine + transport UDP/L2 + join 224.0.1.129 appris du GM). N'émis QUE
    # si ≥1 port média pmd=dpdk → nœud AF_XDP (prod actuelle) et SR-IOV (PF kernel garde ptp4l)
    # STRICTEMENT inchangés, ligne `docker run` octet-identique. Overridable par l'env ENGINE_PTP déjà
    # posé (ex. banc). Validation runtime du lock (join/switch/sip) = hors deploy, cf. socle.
    if _has_dpdk_pf(node) and "ENGINE_PTP" not in env:
        env["ENGINE_PTP"] = "libmtl"
        # + ENGINE_PHC2SYS : libmtl discipline AUSSI CLOCK_REALTIME depuis le PHC (remplace phc2sys
        # kernel). INDISPENSABLE : toute la flotte MXL lit CLOCK_REALTIME (bobimxl now_tai) pour
        # indexer les grains (media_ts) → sans ça le moteur serait synchro mais pas le reste du nœud
        # (cf. docs/chantiers/DPDK_NARROW.md §PTP : « le PTP interne libmtl ne discipline pas l'horloge nœud → ne
        # convient pas seul » ; ENGINE_PHC2SYS lève précisément cette limite).
        env.setdefault("ENGINE_PHC2SYS", "1")
        # + NIC_PROMISCUOUS : sur E810/ice en DPDK, le PMD n'admet PAS le mcast rejoint via le seul
        # filtre MAC (rte_eth_dev_mac_addr_add) → sans promiscuous, rx=0 au niveau port (aucun PTP, ni
        # session). ABLATION MESURÉE (dl360-1 2026-07-09) : promiscuous INDISPENSABLE, en plus des patchs
        # Router Alert (0.39.12) + rte_flow PTP→queue CNI (0.39.13) ; set_mc_addr_list (essayé) n'admet
        # pas non plus. Sans objet en AF_XDP (le noyau programme le filtre mcast) → posé QUE sur PF dpdk.
        env.setdefault("NIC_PROMISCUOUS", "1")
    # ── Lot de synchronisation RDMA (`maxSyncBatchSizeHint`), MÊME réglage et MÊME variable que les
    # conteneurs compute (`docker_compute`) : c'est une option de FLUX posée à la création, et le
    # moteur crée des flux par DEUX chemins qui la lisent tous les deux — `mtl_rx.c:sync_batch_opts`
    # (RX 2110, C) et `bobimxl._flow_options` (simu/txgen du contrôleur, Python).
    # POURQUOI CE SITE MANQUAIT : l'injection avait été faite côté compute seulement, alors que le
    # flux RX du moteur est répliqué par RDMA comme les autres (constaté le 2026-08-10 : lien
    # dl360-1 → dell-1 sur `2110-io-dl360-1_0`, 1080p en 30 tranches). Au défaut du SDK le lot vaut
    # `totalSlices` : l'initiateur attend la trame ENTIÈRE, donc SLICE_MODE payait le découpage sans
    # rien rendre sur le fil — 22,63 ms pour la 1ʳᵉ bande contre 0,06 ms à un lot de 1.
    # Vide = aucune clé posée = comportement historique. ⚠ N'agit que sur les flux CRÉÉS ENSUITE.
    try:
        _sb = str(settings.get("mxl_sync_batch") or "").strip()
    except Exception:
        _sb = ""
    if _sb:
        env["MXL_SYNC_BATCH"] = _sb
    # ── Env moteur ADDITIONNEL (réglage `mtl_engine_env`, JSON objet {k:v}) : passthrough générique
    # pour les capacités env-gatées du moteur (ex. SLICE_MODE=1, SLICE_LINES, TIMING_PARSER — banc/
    # essais sans code). Réglage absent/invalide → AUCUNE clé, ligne `docker run` octet-identique.
    # Ne peut qu'AJOUTER des clés (les clés dérivées ci-dessus priment : setdefault-like via filtre).
    try:
        _xenv = json.loads(settings.get("mtl_engine_env") or "{}")
        if isinstance(_xenv, dict):
            for _k, _v in _xenv.items():
                _k = str(_k).strip()
                if _k and _k not in env:
                    env[_k] = str(_v)
    except Exception:
        log.warning("mtl_engine_env illisible (JSON objet attendu) — ignoré")
    # ── MODE TRANCHE DU MOTEUR, piloté par le réglage GLOBAL (2026-08-11).
    # POURQUOI CE SITE MANQUAIT. `slice_mode_global` posait `slice_mode` sur les PLUGINS (via
    # `plugins.effective_deploy_defaults`) et ne touchait PAS le moteur, qui lisait sa propre
    # variable enfouie dans `mtl_engine_env` — un passe-plat prévu pour des essais de banc.
    # Conséquence constatée en production : le réglage global affichait « tranche activée » alors
    # que le SEUL étage portant le RX et le TX — les deux bouts de la chaîne — ne l'était pas, et
    # rien dans l'interface ne permettait de s'en apercevoir. Un opérateur ne pouvait pas le
    # deviner ; moi non plus, et ça m'a coûté plusieurs diagnostics faux le même jour.
    # RÈGLE, identique aux plugins : le GLOBAL fixe le défaut, l'EXPLICITE prime. Une clé
    # `SLICE_MODE` posée à la main dans `mtl_engine_env` reste donc souveraine (échappatoire de
    # banc), simplement elle n'est plus le seul moyen d'allumer la tranche.
    try:
        if settings.get("slice_mode_global"):
            env.setdefault("SLICE_MODE", "1")
    except Exception:                          # réglage illisible → pas de tranche, pas d'exception
        log.warning("slice_mode_global illisible — mode tranche du moteur laissé au défaut")
    # ── Auth de l'agent :8081 du moteur (SECOND FACTEUR, indépendant du mTLS de _tls_inject).
    # MÊME variable et MÊME contrat que les conteneurs compute : `MXL_AGENT_TOKEN` posé au run →
    # l'agent EXIGE l'en-tête X-MXL-Agent-Token. Un seul chemin ici : `_build_run_cmd` produit LA
    # ligne `docker run`, exécutée par `ssh_run`, qui route lui-même vers l'agent-nœud
    # (/v1/host/exec) ou le root-SSH legacy — les deux transportent la MÊME commande, donc la
    # même injection (pas de second site à ne pas oublier, contrairement à docker_compute).
    # NB : le contrôleur embarqué de l'image 2110_io (plugins/2110_io/docker/controller.py) ne lit
    # PAS encore MXL_AGENT_TOKEN — il ignore donc cette variable aujourd'hui (aucun risque de
    # verrouillage), et le jour où il l'implémentera, l'injection sera déjà en place.
    try:
        from . import deploy as _deploy
        _tok_agent = _deploy.token_a_injecter(vmid)
        if _tok_agent:
            env["MXL_AGENT_TOKEN"] = _tok_agent
    except Exception as _e:
        log.warning("io2110 %s : token d'agent non injecté (%s) — agent :8081 ouvert", vmid, _e)
    e_args = " ".join(f"-e {shlex.quote(f'{k}={v}')}" for k, v in env.items())
    # Pinning cœurs du moteur (défaut ON, réglage mtl_pin_cores) : cpuset = lcores DPDK + 2 cœurs
    # pour le contrôleur Python (simu/txgen, métriques) — sinon les threads Python seraient
    # schedulés SUR les lcores en busy-loop (contention interne). Les cœurs sont RÉSERVÉS dans
    # node_core_alloc (comptabilité core_pool : jamais donnés aux containers compute pinnés ;
    # libérés par release_cores à la destruction, comme tous les backends). Garde-fou côté nœud :
    # le cpuset n'est posé que si la machine a assez de cœurs (sinon docker run échouerait).
    cpuset_prefix = ""
    if settings.get("mtl_pin_cores", True):
        try:
            from . import core_pool
            # Anti-collision : garantir un compute_cpuset DISJOINT des lcores moteur (auto-dérivé une
            # fois par nœud si l'opérateur ne l'a pas défini). Sans pool compute, un container compute
            # non-pinné flotte sous quota sur TOUS les cœurs, dont ceux où le moteur busy-poll. On
            # SSH `nproc` UNIQUEMENT quand compute_cpuset est absent → ne tourne qu'au 1er déploiement
            # moteur du nœud (idempotent ensuite).
            if not (node.get("compute_cpuset") or "").strip():
                from .host_ops import host_cpu_count
                _okn, _ncpu, _ = host_cpu_count(node["host"])
                if _okn and _ncpu:
                    # HT-aware : lire la carte cœur-physique → exclure aussi les siblings HT des lcores
                    # moteur (un compute sur le sibling d'un lcore contend le busy-poll DPDK).
                    _core_of = core_pool.read_cpu_core_map(node)
                    core_pool.ensure_compute_cpuset(node["id"], _ncpu, core_of=_core_of)
                    node = db_get_node(node["id"]) or node          # re-lire (compute_cpuset posé)
            lcore_list = [int(x) for x in lcores.split(",") if x.strip().isdigit()]
            if lcore_list:
                # Cœurs de SERVICE (contrôleur Python + ~20 threads C de service, dont
                # audio_rx_thread qui draine le st30p toutes les ms) : `1 + mtl_service_cores`
                # cœurs après les lcores DPDK. Réglage `mtl_service_cores` (défaut 1) → 2 cœurs,
                # comportement STRICTEMENT inchangé (ancien `ctrl` fixe à 2). N>1 élargit
                # l'enveloppe de N-1 cœurs supplémentaires — famine CPU des threads de service
                # (RX audio back-pressure, framebuff pool empty) sur les nœuds à cpuset étroit.
                _svc = max(1, int(settings.get("mtl_service_cores") or 1))
                ctrl = [max(lcore_list) + i for i in range(1, _svc + 2)]
                # Le CŒUR 0 est OBLIGATOIRE dans le cpuset : MTL préfixe TOUJOURS son
                # main_lcore (jamais posé par mtl_rx → 0) à la liste `-l` de l'EAL
                # (mt_dev.c dev_eal_init : "%u,%s" % (p->main_lcore, p->lcores)) → DPDK
                # doit pouvoir s'affiner sur 0, sinon « Cannot set affinity » et
                # mtl_init échoue en boucle (vu au lab Horace, moteur 100 % à terre).
                # Le main lcore EAL dort (pas un scheduler busy-loop) : le partager
                # avec le housekeeping noyau = la situation historique sans pinning.
                pin = [0] + lcore_list + ctrl
                from . import core_pool
                # ★ Le cpuset RÉELLEMENT POSÉ ci-dessous est la SEULE source de vérité : on
                # l'enregistre TEL QUEL dans node_core_alloc (en évinçant/nommant d'éventuels
                # propriétaires) ET on rétrécit le pool compute pour l'exclure. L'ancien
                # `reserve_exact` n'enregistrait QUE les cœurs libres → core_pool croyait le moteur
                # sur 0-18 pendant que Docker le pinnait sur 0-21 (dl360-1, 2026-07-14). L'empreinte
                # dépend de réglages (`mtl_lcore_max`, `mtl_service_cores`) modifiables APRÈS la
                # dérivation du pool : sans ce rattrapage, elle grandit SILENCIEUSEMENT dans le pool.
                core_pool.reserve_engine_cores(node["id"], vmid, pin,
                                               core_of=core_pool.core_map_cached(node["id"]))
                cpuset = ",".join(str(c) for c in pin)
                cpuset_prefix = (
                    f"BOBI_CPUSET=''; "
                    f"[ \"$(nproc)\" -gt {max(pin)} ] && BOBI_CPUSET={shlex.quote('--cpuset-cpus=' + cpuset)}; "
                )
        except Exception as e:
            log.warning("pinning moteur 2110 non appliqué (vmid %s): %s", vmid, e)
    # mTLS :8081 — écrit les PEM sur l'hôte (prefix) + monte /etc/bobi-tls (tls_v). Vide si pas de CA.
    tls_prefix, tls_v = _tls_inject(vmid, name)
    return (
        tls_prefix + cpuset_prefix +
        # Arrêt GRACIEUX d'abord (SIGTERM → le contrôleur purge XDP + règles ntuple via mtl_uninit,
        # cf. _cleanup) ; sinon `rm -f` (SIGKILL) laisse des règles fdir sur le matériel → « socket
        # add flow fail » au prochain flow. `rm -f` ensuite = filet (no-op si --rm a déjà retiré).
        f"docker stop -t 12 {shlex.quote(name)} >/dev/null 2>&1; "
        f"docker rm -f {shlex.quote(name)} >/dev/null 2>&1; "
        # Le conteneur tourne avec --rm : sa suppression à l'arrêt est ASYNCHRONE, et un
        # `docker rm -f` pendant un « removal in progress » n'attend pas → le run suivant
        # échouait sur « Conflict: name already in use » (vu au redéploiement du 140,
        # 2026-07-13). On attend que le nom soit réellement libéré (≤15 s) avant le run.
        f"for _i in $(seq 1 30); do docker inspect {shlex.quote(name)} >/dev/null 2>&1 || break; sleep 0.5; done; "
        f"docker run -d --rm --name {shlex.quote(name)} $BOBI_CPUSET "
        # Journal DURABLE (cf. app/journal.py) : pilote `journald` → le journal appartient à l'HÔTE
        # et survit à la destruction du conteneur (le moteur tourne en `--rm` et est recréé à chaque
        # redéploiement : avec l'ancien `json-file`, les traces s'évaporaient exactement au moment où
        # on en avait besoin) ET au reboot du nœud. Il reste BORNÉ : `SystemMaxUse` côté journald
        # remplace le `max-size×max-file` d'avant — un moteur dégradé qui spamme (« no available
        # lcore » en boucle) ne peut plus remplir le disque comme les 225 Go de json.log en ~13 h.
        + _journal.docker_flags(name) +
        f"--network host --privileged "
        f"-v {shlex.quote(mxl)}:/dev/shm -v /dev/hugepages:/dev/hugepages "
        f"{dpdk_v}"
        f"{tls_v}"
        f"{e_args} {shlex.quote(image)}"
    )


# SERVIR LA PLUS RÉCENTE : défaut d'un slot qui ne porte PAS la clé. SOURCE UNIQUE — la même
# valeur était dérivée à deux endroits (ici et `routes/nmos_detail.py`), donc deux occasions de
# diverger ; il n'y en a plus qu'une.
#
# ★ REMIS À 0 LE 2026-08-19, sur incident de production (Horace). Le défaut valait 1 depuis le
# 2026-08-12 pour un gain réel — une époque de latence en moins, 62,5 → 42,6 ms au banc. Mais un
# défaut IMPLICITE ne s'applique pas quand on l'écrit : il s'applique quand le moteur apprend à
# le lire. Les slots d'Horace ne portaient pas la clé ; la 0.80.3 l'ignorait (la fonction
# n'existait pas), la 0.96.0 l'a honorée. Un simple rebuild d'image a donc ARMÉ le comportement
# sur une installation à l'antenne, sans que personne ne le choisisse — et une des deux sorties,
# identiques par ailleurs, est sortie STRIÉE (l'autre non : le résultat dépend de la phase entre
# la publication du mur et la lecture du TX). Diagnostic complet : mémoire
# `serve-newest-arme-en-silence-strie-une-sortie-sur-deux`.
#
# Ce que l'incident tranche, et qui vaut au-delà de ce réglage : une image cassée coûte plus cher
# que 20 ms de latence, donc le défaut d'un réglage qui touche CE QU'ON MET SUR LE FIL doit être
# le comportement historique. Le gain reste disponible, mais il se demande — par slot, à chaud
# (`POST /api/mtl/<vmid>/tx/<slot>/serve_newest`).
#
# ⚠ L'avertissement qui accompagnait le défaut à 1 disait déjà : « peu de recul, le cas non
# observé est une source IRRÉGULIÈRE ». C'était le bon doute, sur le bon réglage — il manquait
# seulement de conclure qu'un doute pareil ne se met pas dans un défaut. Ne pas le réactiver
# globalement sans une campagne sur des sources de plusieurs natures.
TX_SERVE_NEWEST_DEFAUT = 0


def tx_payloads(vmid, params=None):
    """Construit les payloads `:8081/tx` EXACTS de tous les slots TX d'un moteur, SANS effet de bord
    (aucun appel réseau). Extrait de `push_tx_slots` — qui le consomme — pour que la CLASSIFICATION des
    actions (docs/reference/TX_LAYOUTS.md étage 2, `app/tx_maintenance.py`) raisonne sur la spec réellement poussée au
    contrôleur plutôt que sur une re-dérivation approximative des params (source unique de vérité :
    un changement de payload qui ne change AUCUNE signature de session mtl_rx est SÛR).
    Retourne une liste de dicts `{i, payload, shm_in, src}` (src = format résolu du shm câblé)."""
    from . import settings as _st
    _provisioning_on = bool(_st.get("tx_layout_provisioning_enabled"))
    c = db_get_container(vmid) or {}
    if params is None:
        try:
            dc = json.loads(c.get("deploy_config") or "{}")
            params = dc.get("params") or {}
        except Exception:
            params = {}
    slots = params.get("tx_slots") or []
    if not slots:
        return []
    # « Option A » : nombre/idx des audios PAR slot pilotés par tx_flows (attached_to) — plus de
    # ratio homogène. Repli dérivé du legacy si la liste est absente.
    from . import io2110_flows as _iof
    tx_flows = _iof.active_flows(params, "tx")
    # C2b+ : transport d'une ressource NMOS bindée à un slot_key (push-down — la ressource fait
    # autorité sur mcast/port/format). None si le slot n'est pas bindé (→ valeurs du slot, auto).
    _nmos_bind = params.get("nmos_bind") or {}
    _tx_pins = params.get("tx_pins") or {}   # épinglage de port par slot TX ({slot: ifname}) ; "" = auto
    def _bound_tr(slot_key):
        rid = _nmos_bind.get(slot_key)
        if not rid:
            return None
        try:
            from .database import db_nmos_resource_get
            r = db_nmos_resource_get(rid)
            return (r or {}).get("transport") or None
        except Exception:
            return None
    out = []
    for i, t in enumerate(slots):
        shm_in = params.get(cle_tx_shm(i)) or ""   # shm câblé (state_field, persisté par _apply_wire)
        # Câblages audio/ANC des sorties (state_field tx_audio{idx}_shm / tx_anc{i}_shm) → resync.
        # idx audio = pool plat des flux attachés au slot (tx_flows), dans l'ordre (= position ai).
        _aud_idxs = _iof.tx_slot_audio_idxs(tx_flows, i)
        audio_shm_in = [params.get(cle_tx_audio_shm(aidx)) or "" for aidx in _aud_idxs]
        anc_shm_in = params.get(cle_tx_anc_shm(i)) or ""
        # Balayage du passthrough : la VÉRITÉ est le format du PRODUCTEUR du shm câblé (pas le
        # slot dest). On le résout depuis la topologie → le TX ré-émet le scan réellement reçu.
        # Sans câble : pas de scan (le slot n'émet pas). Réutilise monitor._shm_fmt (scan/field_order).
        _src = {}
        if shm_in:
            try:
                from .monitor import _shm_fmt
                _src = _shm_fmt(shm_in) or {}
            except Exception:
                _src = {}
        # Format vidéo poussé au slot : un slot en GÉN impose son format PAR-SLOT (la mire est rendue
        # à la résolution choisie) ; un slot CÂBLÉ qui SUIT sa source laisse la SOURCE gouverner
        # (w/h/fps posés par :8082/input) → on envoie 0 (le contrôleur ignore 0 → pas de clobber).
        # « En GÉN » = pas de câble OU générateur explicitement activé (bouton GEN) — dans ce dernier
        # cas la mire prime sur le câble attaché, donc le format par-slot doit gouverner. scan : le
        # slot (GÉN) ou la source (câblé suivi).
        gen = bool(t.get("gen_enabled")) or not shm_in
        if gen:
            _fw, _fh = int(t.get("width") or 0), int(t.get("height") or 0)
            _ffps    = t.get("fps") or 25
            _fscan   = str(t.get("scan") or "p")
            _ffo     = t.get("field_order") or ("tff" if _fscan == "i" else "")
        else:
            _fw = _fh = 0; _ffps = 0
            _fscan = _src.get("scan") or "p"
            _ffo   = _src.get("field_order") or ""
            # ÉTAGE 3 (docs/reference/TX_LAYOUTS.md) — DÉRIVE DE SOURCE : si le format de la source ne concorde PLUS
            # avec le format DÉCLARÉ du slot (une caméra a basculé), ne PAS lui pousser le balayage de
            # la source. MESURÉ AU BANC (moteur 140) : ce re-push (déclenché par `deploy.py` quand le
            # flux producteur est recréé) clobbe `scan`/`field_order` côté contrôleur → la SIGNATURE de
            # session change → session recréée → `rte_tm_hierarchy_commit` → stop/start du PORT ENTIER,
            # sans qu'aucun humain n'ait rien demandé. On gèle donc l'identité du slot sur ce qu'il
            # ANNONCE ; l'écart est traité par `tx_format_watch` (alerte + UDC), qui rétablit une
            # source concordante SANS jamais recréer la session. Concordant ⇒ passthrough inchangé.
            try:
                from . import tx_maintenance as _txm
                _decl = _txm.slot_format(params, i)
                if _decl and _decl.get("width") and _txm.format_diff(_src, _decl):
                    _fscan = _decl["scan"]
                    _ffo = _decl.get("field_order") or ("tff" if _decl["scan"] == "i" else "")
            except Exception as _e:
                log.warning("tx_payloads: gel du format du slot %s: %s", i, _e)
        payload = {"idx": i, "enabled": bool(shm_in),
                   # Étage 1 docs/reference/TX_LAYOUTS.md : destination valide (mcast+port) → session PRÉ-PROVISIONNÉE
                   # silencieuse dès maintenant (câblage/décâblage = swap de source, zéro commit TM).
                   "provisioned": bool(_provisioning_on and t.get("multicast_ip") and t.get("dest_port")),
                   # Épinglage de port (multi-NIC) : "" = répartition auto côté contrôleur.
                   "iface": _tx_pins.get(str(i)) or "",
                   "mcast": t.get("multicast_ip"), "udp_port": int(t.get("dest_port") or 0),
                   "pt": int(t.get("payload_type") or 96), "shm_in": shm_in,
                   "width": _fw, "height": _fh,
                   "fps": _ffps, "bit_depth": int(t.get("bit_depth") or 8),
                   "ring": int(t.get("ring") or 8),
                   # BRIDAGE D'AVANCE (mode tranche) : nombre MAXIMAL de trames prêtes que le
                   # worker s'autorise à avoir devant celle que la lib émet. 0 = désactivé
                   # (comportement historique). MESURÉ le 2026-08-12 : la file se stabilise à
                   # 3 trames avec `slot_wait_ms` à 0,0 — le worker n'est pas étranglé, il a
                   # simplement pris de l'avance au démarrage et deux débits égaux ne vident
                   # jamais une file. ⛔ MAIS réduire cette file NE RÉDUIT PAS LA LATENCE :
                   # mesuré 62,1 ms et +3 trames à profondeur 3, 2 et 1 — et à 1 le TX ne
                   # consomme plus qu'une trame source sur deux. LAISSER À 0 ; le vrai levier
                   # est `epoch_shift_us`. Conservé comme instrument de diagnostic.
                   "advance": int(t.get("advance") or 0),
                   # FREIN TEMPOREL (mode tranche) : le worker attend d'être à N µs de
                   # la prochaine époque avant de saisir la source. 0 = désactivé.
                   # Ne décale PAS l'émission (fixée par la grille PTP) : met du contenu
                   # plus frais dans le même créneau. Trop tard = sollicitation ratée =
                   # répétition ; surveiller `repeats` et `wait_pub_ms`.
                   "publish_lead_us": int(t.get("publish_lead_us") or 0),
                   # SERVIR LA PLUS RÉCENTE (cf. TX_SERVE_NEWEST_DEFAUT ci-dessus pour le défaut
                   # et l'incident qui l'a remis à 0). Le callback rend la trame la plus récemment
                   # publiée au lieu de la plus ancienne, et libère les périmées : une époque de
                   # latence en moins quand la source est régulière, une image cassée quand la
                   # phase de publication tombe mal. Se demande PAR SLOT, à chaud, jamais par
                   # omission. Surveiller `repeats` et `skipped` après l'avoir posé à 1.
                   "serve_newest": int(TX_SERVE_NEWEST_DEFAUT
                                       if t.get("serve_newest") is None
                                       else t.get("serve_newest")),
                   "scan": _fscan, "field_order": _ffo,
                   # Rythme d'émission (mode tranche) : 0 = attendre l'image suivante (défaut,
                   # émission alignée epoch) ; >0 = grille d'émission décalée de N µs (TROFF
                   # déclaré dans le SDP, timestamp RTP inchangé) — gain ~1 trame de latence TX.
                   "epoch_shift_us": int(t.get("epoch_shift_us") or 0),
                   # Dest audio (2110-30) : N flux (« Option A » — plus de cap à 2), shm audio
                   # câblé via audio_shm_in (aligné position↔position avec cette liste).
                   "audios": [
                       {"mcast": a.get("multicast_ip"), "port": int(a.get("dest_port") or 0),
                        "pt": int(a.get("payload_type") or 97),
                        "mcast2": a.get("multicast_ip_leg1") or None,
                        "port2": int(a.get("dest_port_leg1") or 0),
                        # ptime PAR-SORTIE (ms) : passthrough si déclaré dans tx_slots[].audios[].ptime ;
                        # None/absent → le contrôleur replie sur le défaut global (rétro-compatible).
                        "ptime": a.get("ptime"),
                        # Générateur de tonalité (resync de l'état persisté)
                        "tone": a.get("tone")}
                       for a in (t.get("audios") or [])
                   ],
                   # Dest ANC (2110-40) : le shm ANC suit la vidéo (dérivé côté contrôleur).
                   "anc_mcast": t.get("anc_multicast_ip"),
                   "anc_port": int(t.get("anc_dest_port") or 0),
                   "anc_pt": int(t.get("anc_payload_type") or 97),
                   # Leg1 SMPTE 2022-7 (vidéo + ANC) — None si 2022-7 désactivé
                   "mcast2": t.get("multicast_ip_leg1") or None,
                   "udp_port2": int(t.get("dest_port_leg1") or 0),
                   "anc_mcast2": t.get("anc_multicast_ip_leg1") or None,
                   "anc_port2": int(t.get("anc_dest_port_leg1") or 0),
                   # Générateur TX (mire interne sans source câblée) + repli automatique
                   "gen_enabled": bool(t.get("gen_enabled")),
                   "gen_pattern": t.get("gen_pattern") or "bars",
                   "fallback_mode": params.get("tx_fallback") or "black",
                   # IDENT user incrusté sur la sortie émise (resync de l'état persisté)
                   "ident": bool(t.get("ident")),
                   "ident_size": int(t.get("ident_size") or 0),
                   # Câblages audio/ANC indépendants (resync ; "" = non câblé → silence)
                   "audio_shm_in": audio_shm_in,
                   "anc_shm_in": anc_shm_in}
        # C2b+ push-down : si une ressource NMOS est bindée à ce slot, son transport gouverne le
        # ROUTAGE émis (mcast/port + leg1 2022-7) par essence ; en GÉN, aussi le format vidéo. Un
        # slot non bindé est inchangé (transport du slot, comportement auto).
        _vt = _bound_tr(slot_tx(i, "v"))
        if _vt:
            if _vt.get("multicast_ip"):      payload["mcast"]    = _vt["multicast_ip"]
            if _vt.get("port"):              payload["udp_port"] = int(_vt["port"])
            if _vt.get("multicast_ip_leg1"): payload["mcast2"]    = _vt["multicast_ip_leg1"]
            if _vt.get("dest_port_leg1"):    payload["udp_port2"] = int(_vt["dest_port_leg1"])
            if gen:   # mire interne → la ressource gouverne aussi le format émis (SDP fidèle)
                if _vt.get("width"):     payload["width"]     = int(_vt["width"])
                if _vt.get("height"):    payload["height"]    = int(_vt["height"])
                if _vt.get("bit_depth"): payload["bit_depth"] = int(_vt["bit_depth"])
                if _vt.get("scan"):      payload["scan"]      = _vt["scan"]
                if _vt.get("fps"):       payload["fps"]       = _vt["fps"]
                if _vt.get("field_order") is not None: payload["field_order"] = _vt["field_order"]
        for _ai in range(len(payload["audios"])):
            _at = _bound_tr(slot_tx(i, "a%d" % _ai))
            if _at:
                if _at.get("multicast_ip"): payload["audios"][_ai]["mcast"] = _at["multicast_ip"]
                if _at.get("port"):         payload["audios"][_ai]["port"]  = int(_at["port"])
        _dt = _bound_tr(slot_tx(i, "d"))
        if _dt:
            if _dt.get("multicast_ip"): payload["anc_mcast"] = _dt["multicast_ip"]
            if _dt.get("port"):         payload["anc_port"]  = int(_dt["port"])
        out.append({"i": i, "payload": payload, "shm_in": shm_in, "src": _src})
    return out


def engine_booted_active_tx(vmid):
    """`ACTIVE_TX_COUNT` FIGÉ AU BOOT du conteneur moteur 2110_io (env lu via `docker inspect`), ou
    None si indéterminable (conteneur absent / hôte injoignable). Les budgets `ACTIVE_*_COUNT` ne
    sont relus qu'au `docker run` → c'est la vérité pour décider si porter `active_tx_count` au-delà
    exige une RECRÉATION du moteur (re-réservation des files RL). Miroir de
    `routes/mtl_engine._engine_booted_env_int`, ici pour éviter que la couche modèle importe les routes."""
    import shlex as _shlex
    from .database import db_get_container, db_get_node
    try:
        c = db_get_container(vmid) or {}
        node = db_get_node(c.get("node_id"))
        if not node:
            return None
        name = c.get("docker_name") or _name(vmid)
        rc, out, _ = ssh_run(
            node["host"],
            "docker inspect %s --format '{{range .Config.Env}}{{println .}}{{end}}'" % _shlex.quote(name),
            timeout=10)
        if rc != 0:
            return None
        for line in (out or "").splitlines():
            if line.startswith("ACTIVE_TX_COUNT="):
                return int(line.split("=", 1)[1] or 0)
    except Exception:
        return None
    return None


def moteur_initialise(host, timeout_s=0):
    """Le moteur a-t-il RÉELLEMENT terminé son `mtl_init` ? (preuve : ses ports média sont publiés
    sur :8080). `timeout_s` > 0 → attend jusqu'à l'échéance ; 0 → une seule interrogation.

    ⚠ NE PAS confondre avec `deploy.attendre_controleur_pret`, qui interroge :8081/status — lequel
    répond `{"running": true}` INCONDITIONNELLEMENT dès que le serveur HTTP du contrôleur est monté
    (controller.py do_GET). C'est une sonde de VIVACITÉ, pas de DISPONIBILITÉ : elle dit « le
    processus est là », jamais « le moteur est prêt à recevoir sa configuration ». Or `mtl_init` met
    30 à 60 s sur E810 (entraînement du lien 100G compris). Tout ce qui configure le moteur juste
    après un `docker run` en se fiant à :8081 pousse donc dans le vide — c'est le mécanisme qui a
    laissé un moteur recréé sans AUCUNE sortie le 2026-07-28."""
    import time as _t
    _fin = _t.monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            r = requests.get("http://%s:8080/" % host, timeout=4)
            if r.status_code == 200:
                d = r.json() or {}
                if ((d.get("nic") or {}).get("ports")) or ((d.get("xdp") or {}).get("reserved")):
                    return True
        except Exception:
            pass
        if _t.monotonic() >= _fin:
            return False
        _t.sleep(2)


def push_tx_slots(vmid, params=None):
    """Pousse les slots TX du moteur (destination mcast/port + shm d'entrée câblé + format) au
    contrôleur :8081/tx (sur l'hôte du nœud, --network host). Appelé au déploiement (resync après
    restart, l'état _tx du contrôleur est en mémoire) et au (re)câblage. Best-effort + retry court
    (le conteneur vient peut-être de démarrer). Les payloads sont construits par `tx_payloads`.

    Étage 1 docs/reference/TX_LAYOUTS.md (arbre TX statique) : tout slot avec une destination valide (mcast+port)
    est poussé `provisioned=True`, câblé ou non — le contrôleur crée alors la session/feuille RL
    SILENCIEUSE dès que la destination est déclarée, au lieu d'attendre le câblage. Le câblage qui
    suit n'est plus qu'un SWAP DE SOURCE (zéro `rte_tm_hierarchy_commit`). Repli désactivable
    (`tx_layout_provisioning_enabled=False`, garde-fou site)."""
    import time
    c = db_get_container(vmid) or {}
    if params is None:
        try:
            dc = json.loads(c.get("deploy_config") or "{}")
            params = dc.get("params") or {}
        except Exception:
            params = {}
    node = db_get_node(c.get("node_id"))
    host = node.get("host") if node else None
    if not host or not (params.get("tx_slots") or []):
        return
    # READINESS du contrôleur avant tout push : au boot À FROID du moteur (realign/recréation),
    # :8081 ne répond qu'après mtl_init (~30-60 s) — les retries courts par-slot (5×~1-5 s)
    # rataient la fenêtre → sessions TX absentes jusqu'à un re-push manuel (vécu à la bascule
    # 0.42.0). On attend ici, UNE fois, jusqu'à ~90 s avec backoff ; au-delà on tente quand même
    # (les retries par-slot restent le filet) et on alerte.
    from . import deploy
    # Readiness = `mtl_init` TERMINÉ (ports publiés sur :8080), pas « le contrôleur répond ».
    # :8081/status dit `running:true` dès que son serveur HTTP est monté, donc bien avant que le
    # moteur accepte sa configuration — pousser à ce moment-là revient à pousser dans le vide.
    _budget = float(settings.get("mtl_tx_push_timeout_s") or 120)
    _ready = moteur_initialise(host, timeout_s=_budget)
    if not _ready:
        # (db_add_alert vient du module — un import LOCAL ici en ferait une variable locale pour
        # TOUTE la fonction, donc non liée dans les autres branches : UnboundLocalError.)
        db_add_alert("alert.docker.moteur_non_initialise", "warning",
                     vmid=vmid, kind="tx_stall",
                     params={"vmid": vmid, "s": "%.0f" % _budget})
    # ★ ÉCHÉANCE, PAS COMPTE D'ESSAIS (incident dl360-1, 2026-07-28 00:00). L'ancien `range(5)` donnait
    # 5 s par slot : après une RECRÉATION, l'agent :8081/status répond bien avant que le contrôleur
    # n'accepte /tx (mtl_init met 30-60 s). Les 5 essais s'épuisaient, EN SILENCE, pour chaque slot —
    # le moteur repartait avec ZÉRO sortie et personne ne le disait. C'est exactement la correction
    # déjà faite sur le chemin RX de `resync_moteur` (cf. son docstring), jamais reportée ici.
    _deadline = time.monotonic() + float(settings.get("mtl_tx_push_timeout_s") or 120)
    _ko = []
    _payloads = tx_payloads(vmid, params)
    for _ent in _payloads:
        i, payload, shm_in, _src = _ent["i"], _ent["payload"], _ent["shm_in"], _ent["src"]
        from . import deploy
        _ok, _err = False, ""
        while True:
            try:
                deploy.agent_session().post(deploy.agent_url(host, "/tx"), json=payload, timeout=4,
                                            headers=deploy.agent_headers(vmid))
                _ok = True
                break
            except Exception as e:
                _err = str(e)
                if time.monotonic() >= _deadline:
                    break
                time.sleep(1)
        if not _ok:
            _ko.append((i, _err))
            continue          # slot non poussé : ne pas re-câbler dans le vide
        # Câble VIDÉO d'entrée (:8082/input) APRÈS le slot : seul le `cable_shm` VIDÉO fait émettre, et
        # `:8081/tx` ne le pose PAS (il pose `shm_in`, aussitôt écrasé par `_tx_gen_apply` qui lit
        # `cable_shm`). Sans ce re-push, une sortie TX vidéo câblée ne survit pas à une recréation du
        # conteneur (symétrie manquante avec les abonnements RX re-poussés). Les câbles AUDIO/ANC sont
        # déjà restaurés par `:8081/tx` (audio_shm_in→audio_cable_shm, anc_shm_in→anc_cable_shm).
        if shm_in:
            # Contrat :8082/input = clé "slot" (cf. _plugin_input / controller do_POST /input qui lit
            # body["slot"], défaut 0). Envoyer "idx" faisait retomber TOUT câble vidéo re-poussé sur le
            # slot 0 (invisible avec 1 seul TX : idx 0 = défaut ; révélé dès Tx2 → inversion des sorties).
            _wire = {"essence": "video", "shm": shm_in, "slot": i}
            if _src:
                _wire["format"] = _src
            _wok = False
            while True:
                try:
                    requests.post(f"http://{host}:8082/input", json=_wire, timeout=4)
                    _wok = True
                    break
                except Exception as e:
                    if time.monotonic() >= _deadline:
                        _ko.append((i, "câble vidéo : %s" % e))
                        break
                    time.sleep(1)
    # ★ RENDRE COMPTE. La fonction retournait None quoi qu'il arrive : un push intégralement raté
    # était indiscernable d'un succès, y compris pour `resync_moteur` qui l'appelle.
    _tot = len(_payloads)
    if _ko:
        _ids = [str(i) for i, _ in _ko]
        _liste = ", ".join(_ids[:10])
        _reste = len(_ids) - 10
        _params = {"vmid": vmid, "n_ko": len(_ko), "n_tot": _tot, "liste": _liste}
        if _reste > 0:
            db_add_alert("alert.tx_stall.slots_non_pousses_reste", "error", vmid=vmid, kind="tx_stall",
                         params={**_params, "reste": _reste})
        else:
            db_add_alert("alert.tx_stall.slots_non_pousses", "error", vmid=vmid, kind="tx_stall",
                         params=_params)
    return {"pushed": _tot - len(_ko), "total": _tot,
            "failed": [i for i, _ in _ko]}


def sessions_rx_actives(vmid, host=None):
    """Nombre de sessions RX VIDÉO réellement ACTIVES dans le moteur (:8080), = receivers[] en
    mode "mtl". Un slot jamais abonné est en mode "idle" (fps 0) — il n'existe AUCUN autre signe
    extérieur d'un moteur revenu vide. None si le moteur ne répond pas (indéterminé ≠ vide)."""
    if host is None:
        c = db_get_container(vmid) or {}
        node = db_get_node(c.get("node_id"))
        host = node.get("host") if node else None
    if not host:
        return None
    from . import deploy as _dep
    try:
        r = requests.get(f"http://{host}:{_dep.controller_port_base(vmid)}", timeout=3)
        if r.status_code != 200:
            return None
        recs = (r.json() or {}).get("receivers")
        if not isinstance(recs, list):
            return None
        return sum(1 for x in recs
                   if x.get("essence") in (None, "video") and x.get("mode") == "mtl")
    except Exception:
        return None


def resync_moteur(vmid, params):
    """Resync COMPLET d'un moteur 2110 qui vient d'être (re)créé : readiness → slots TX →
    abonnements RX (IS-05) → **VÉRIFICATION**.

    NOTE COURSE DE RESYNC (bug de prod, mesuré au banc le 2026-07-14, reproduit 2 fois sur 3) :
    l'ancien code lançait DEUX threads indépendants juste après le `docker run` — l'un pour les
    slots TX (readiness ≤ ~90 s), l'autre pour le repush NMOS (readiness = 30 itérations de
    `sleep(1)`). Sur un port FERMÉ, `connection refused` revient instantanément → le budget du
    repush s'épuisait en ~30 s alors que le contrôleur recréé met 30-60 s à servir :8081 (mtl_init).
    Les `POST /nmos/subscribe` partaient donc dans le vide, chaque échec n'étant qu'un `warning`
    « agent injoignable » — et si l'état d'abonnement était vide dans ce process, le repush ne
    poussait RIEN et ne disait RIEN. Résultat : moteur à 0 session, murs gelés, tableau de bord
    au vert (aucun détecteur ne voyait rien : un moteur vide publie des slots "idle", et la
    branche MTL de `metrics.rafraichir_metrics` sort AVANT `_check_cadence`).

    D'où : une seule séquence, une readiness bornée par une ÉCHÉANCE (pas par un compte d'essais),
    et surtout une VÉRIFICATION finale — sessions RX attendues (état IS-05 de l'orchestrateur) vs
    sessions RX réellement actives dans le moteur. Écart ⇒ une tentative de repush, puis ALERTE
    `error` si l'écart persiste. Un moteur vide après redéploiement n'est JAMAIS silencieux."""
    import time
    from .database import db_add_alert
    from . import deploy as _dep
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id")) or {}
    host = node.get("host")
    hn = (params or {}).get("hostname") or c.get("hostname") or str(vmid)
    if not host:
        return

    if not _dep.attendre_controleur_pret(host, vmid=vmid):
        db_add_alert("alert.docker.controleur_injoignable", "error",
                     vmid=vmid, node_id=c.get("node_id"), kind="deploy",
                     params={"h": hn, "vmid": vmid})
        return

    # ★ DEUXIÈME attente, et c'est elle qui compte : :8081/status ci-dessus ne prouve que la VIVACITÉ
    # du contrôleur (il répond `running:true` dès que son serveur HTTP est monté). Le moteur, lui, met
    # 30-60 s de plus à finir `mtl_init` sur E810 — entraînement du lien 100G compris. Tout ce qui
    # suit (slots TX, abonnements RX) le CONFIGURE : le faire trop tôt, c'est pousser dans le vide.
    # On ne renonce pas si l'échéance passe (le moteur peut finir juste après, et le repli
    # d'admission de push_tx_slots a sa propre échéance) — mais on le DIT.
    if not moteur_initialise(host, timeout_s=float(settings.get("mtl_init_wait_s") or 90)):
        db_add_alert("alert.docker.mtl_init_non_termine", "warning",
                     vmid=vmid, node_id=c.get("node_id"), kind="deploy",
                     params={"h": hn, "vmid": vmid})

    # 1) Slots TX. Étage 1 docs/reference/TX_LAYOUTS.md : si un layout est déclaré pour la NIC de ce moteur et
    # pas encore entièrement appliqué, on l'applique D'ABORD (auto-alloc des destinations
    # manquantes + `provisioned=True` pour tout l'arbre déclaré) — le déploiement est le moment
    # SÛR pour recalculer l'arbre RL du port (aucune sortie n'est encore vivante). Sans layout
    # déclaré (ou déjà appliqué) : simple resync des slots existants.
    if (params or {}).get("tx_slots"):
        applied_via_layout = False
        try:
            from . import io2110_layouts as _lay
            # Multi-port : on regarde TOUTES les cartes, pas seulement la primaire — un modèle
            # déclaré sur la seconde carte était jusqu'ici invisible ici, donc jamais appliqué.
            _sts = _lay.layout_status_all(vmid) or {}
            if any(s.get("state") in ("none", "pending") for s in _sts.values()):
                # `iface=None` → applique les modèles de TOUTES les unités qui en déclarent un.
                # `redeploy=True` : on est DANS le déploiement, l'env vient d'être posé — si le total
                # déclaré diffère du booté, il faut recréer maintenant (comportement historique).
                # Ailleurs, le défaut est le report signalé par reconcile_engine_sizing.
                ok, _res = _lay.apply_layout(vmid, redeploy=True)
                applied_via_layout = ok
        except Exception as e:
            log.warning("resync moteur %s: layout TX (étage 1) : %s", vmid, e)
        if not applied_via_layout:
            try:
                # Sondes de présence signal armées par source : le moteur recréé est reparti sur
                # son défaut (tout calculer). On lui repousse la configuration — sans quoi
                # l'économie de CPU disparaît silencieusement à chaque redéploiement.
                try:
                    from .routes.mtl_engine import push_probes_all as _ppa
                    _ppa(vmid, params)
                except Exception as _e:
                    log.info("resync moteur %s : repush des sondes : %s", vmid, _e)
                _res_tx = push_tx_slots(vmid, params) or {}
                # ★ VÉRIFIER LE VERSANT TX. La vérification finale de ce resync ne compte que les
                # sessions RX : un moteur revenu avec ZÉRO sortie passait entièrement inaperçu
                # (vécu le 2026-07-28 — TX muettes jusqu'à un push manuel). `push_tx_slots` alerte
                # désormais lui-même par slot ; ici on tranche le cas TOTAL, le plus parlant.
                if _res_tx.get("total") and not _res_tx.get("pushed"):
                    db_add_alert(
                        "alert.docker.revenu_sans_sortie",
                        "error", vmid=vmid, node_id=c.get("node_id"), kind="tx_stall",
                        params={"h": hn, "vmid": vmid, "t": _res_tx["total"]})
            except Exception as e:
                db_add_alert("alert.docker.resync_tx_echoue", "error",
                             vmid=vmid, node_id=c.get("node_id"), kind="tx_stall",
                             params={"h": hn, "vmid": vmid, "e": str(e)})

    # 2) Abonnements RX (IS-05) : le contrôleur recréé a perdu ses fichiers SDP (/tmp, rootfs
    # éphémère), mais l'orchestrateur garde l'état NMOS → on restaure les receivers actifs sans
    # intervention d'un contrôleur NMOS externe.
    try:
        from services import nmos as _nmos
    except Exception as e:
        log.warning("resync moteur %s: service nmos indisponible : %s", vmid, e)
        return
    attendus = _nmos.nb_sessions_rx_attendues(vmid)
    try:
        _nmos.repush_subscriptions(vmid)
    except Exception as e:
        db_add_alert("alert.docker.repush_rx_echoue", "error",
                     vmid=vmid, node_id=c.get("node_id"), kind="rx_stall",
                     params={"h": hn, "vmid": vmid, "e": str(e)})
        return
    if not attendus:
        return   # rien d'attendu → un moteur sans session RX est NORMAL, on n'alerte pas

    # 3) VÉRIFICATION : le moteur crée ses sessions au tour suivant du _manager_loop (≤ ~0,5 s) mais
    # mtl_rx peut mettre plusieurs secondes à monter → on laisse un délai, puis on RE-POUSSE une
    # fois avant d'alerter (une seule reprise : au-delà, c'est une panne, pas une course).
    for tentative in (1, 2):
        obs = None
        for _ in range(int(_VERIF_RX_TIMEOUT_S / 2)):
            time.sleep(2)
            obs = sessions_rx_actives(vmid, host)
            if obs is not None and obs >= attendus:
                if tentative > 1:
                    db_add_alert("alert.docker.rx_retablies", "info",
                                 vmid=vmid, node_id=c.get("node_id"), kind="rx_stall",
                                 params={"h": hn, "vmid": vmid, "obs": obs, "att": attendus})
                log.info("resync moteur %s: %d/%d sessions RX actives", vmid, obs, attendus)
                return
        if tentative == 1:
            log.warning("resync moteur %s: %s/%d sessions RX après resync — re-poussée",
                        vmid, obs, attendus)
            try:
                _nmos.repush_subscriptions(vmid)
            except Exception:
                pass
    db_add_alert(
        "alert.docker.rx_revenu_vide",
        "error", vmid=vmid, node_id=c.get("node_id"), kind="rx_stall",
        params={"h": hn, "vmid": vmid, "obs": 0 if obs is None else obs, "att": attendus})


# Délai laissé au moteur pour créer ses sessions RX après un repush (mtl_rx monte en plusieurs s).
_VERIF_RX_TIMEOUT_S = 30


def deploy_docker(vmid, params, type_script=None):
    """Déploie/relance le conteneur contrôleur MTL sur le nœud. Réplique le contrat DB/NMOS du
    chemin LXC (db_update_source/deploy_config + notify nmos/emberplus).

    `type_script` est RÉSOLU (argument → deploy_config existant → défaut MTL) puis VÉRIFIÉ :
    ce driver est réservé aux types MTL (controller bâti dans l'image, hooks receiver). Un type
    non-MTL qui arriverait ici = routage incorrect → on échoue au lieu de le coercer en
    2110_io (cf. bug « le correcteur de couleur devient un receiver MTL »)."""
    from . import docker_compute
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    if not node:
        db_add_alert("alert.docker.deploiement_noeud_introuvable", "error",
                     vmid=vmid, node_id=c.get("node_id"), kind="deploy",
                     params={"vmid": vmid})
        return False
    # AGENT-NŒUD EXIGÉ (décision 2026-07-26), comme pour les conteneurs compute. Sans agent,
    # `ssh_run` retombe sur le root-SSH : le moteur démarrerait, mais privé de ce que seul le
    # contrat d'agent transporte. Un moteur 2110 à moitié configuré est pire qu'un moteur absent —
    # il produit du signal qu'on croit bon. On refuse explicitement plutôt que de déployer à demi.
    from . import node_driver as _nd
    if not _nd.has_agent(node):
        db_add_alert(
            "alert.docker.refuse_sans_agent",
            "error", vmid=vmid, node_id=node.get("id"), kind="deploy",
            params={"vmid": vmid, "n": node.get("name")})
        return False
    # Type : argument explicite → type persisté en deploy_config → défaut MTL bespoke.
    type_script = type_script or docker_compute._type_of(c) or "2110_io"
    if not docker_compute.is_mtl_type(type_script):
        db_add_alert("alert.docker.type_non_mtl", "error",
                     vmid=vmid, node_id=node.get("id"), kind="deploy",
                     params={"t": type_script, "vmid": vmid})
        return False

    db_add_alert("alert.docker.deploiement_en_cours", "info",
                 vmid=vmid, node_id=node.get("id"), kind="deploy",
                 params={"t": type_script, "n": node["name"], "vmid": vmid})

    params = dict(params or {})
    params.setdefault("hostname", _hostname_moteur(vmid, None, c))

    # Remplir les clés ABSENTES depuis deploy_defaults du manifeste (sans écraser les valeurs
    # déjà explicites en DB) : garantit qu'un nouveau champ ajouté dans plugin.json s'applique
    # même aux containers existants au prochain redéploiement.
    _p_manifest = (plugins.get(type_script) or {}).get("deploy_defaults") or {}
    for _k, _v in _p_manifest.items():
        params.setdefault(_k, _v)

    # Capacité TX = nombre de slots émetteurs provisionnés dans le contrôleur (N_TX). C'est une
    # capacité STRUCTURELLE (comme video_count côté RX) que active_tx_count plafonne ensuite par
    # le budget de queues XDP. On la migre VERS LE HAUT depuis le manifeste (jamais à la baisse) :
    # un container legacy provisionné avec peu de slots (ex. tx_count=6) adopte la capacité courante
    # au prochain redéploiement → le bouton « + Ajouter un TX » retrouve de la marge.
    _tx_default = int(_p_manifest.get("tx_count") or 0)
    if _tx_default:
        params["tx_count"] = max(int(params.get("tx_count") or 0), _tx_default)

    # Format de placeholder/simu = format vidéo PAR DÉFAUT des réglages (pas de valeur figée).
    # Injecté dans les params → atterrit dans deploy_config (topologie/moniteur avant SDP) ET
    # dans l'env du contrôleur. La réception réelle s'adapte ensuite au SDP (propagation).
    _df = _default_video_format()
    for _k in ("width", "height", "fps", "scan", "chroma", "bit_depth", "colorimetry"):
        params.setdefault(_k, _df[_k])

    # Hook before_deploy : normalise video_count/format (réutilise le receiver 2110).
    _hook = plugins.get_hook(type_script, "before_deploy")
    if _hook:
        try:
            _res = _hook(dict(params), {"vmid": vmid, "type": type_script,
                                        "hostname": params.get("hostname", ""),
                                        "node_id": c.get("node_id"),
                                        "settings": settings.all()})
            if _res is not None:
                params = _res
        except Exception as e:
            log.warning("hook before_deploy %s: %s", type_script, e)

    # Profondeur de bits du pipeline shm (force8 par défaut) — comme le chemin LXC (deploy.py).
    # Sans ça, mtl_rx écrirait du 10 bits natif alors que les consommateurs lisent du 8 bits
    # → image verte/fantôme. Le contrôleur passe ce bit_depth à mtl_rx (output_fmt) + à la simu.
    try:
        from .deploy import _apply_pipeline_bit_depth
        _apply_pipeline_bit_depth(params, settings.get("mxl_pipeline_bit_depth") or "force8")
    except Exception as e:
        log.warning("pipeline bit_depth %s: %s", vmid, e)

    ok, msg = verify_image(node)
    if not ok:
        db_add_alert("alert.docker.deploiement_image_echouee", "error",
                     vmid=vmid, node_id=node.get("id"), kind="deploy",
                     params={"vmid": vmid, "msg": msg})
        return False

    # Plan média 2110 : (ré)assigner l'IPv4 de CHAQUE PF média AVANT le run (le contrôleur les
    # auto-détecte au démarrage). Idempotent ; un échec n'empêche pas le déploiement mais est signalé.
    _mok, _mmsg = ensure_media_ips(node)
    if not _mok:
        db_add_alert("alert.docker.ip_media_non_assignee", "warning",
                     vmid=vmid, node_id=node.get("id"), kind="net",
                     params={"vmid": vmid, "msg": _mmsg, "ifn": node.get("mtl_iface")})
    # Passerelle déclarée des NIC média (routage par leg). No-op si aucune n'est renseignée —
    # c'est le cas de tout parc antérieur. Un échec est signalé, jamais avalé.
    _rok, _rmsg = ensure_media_routes(node)
    if not _rok:
        db_add_alert("alert.docker.route_media_non_posee", "warning",
                     vmid=vmid, node_id=node.get("id"), kind="net",
                     params={"vmid": vmid, "msg": _rmsg, "ifn": node.get("mtl_iface")})

    # AUTO-BIND vfio-pci des PF media2110 pmd=dpdk AVANT le run : sans ce bind, une PF dpdk restée sur
    # `ice` fait crash-looper le moteur DPDK (« dev_eal_init fail -1 ») SILENCIEUSEMENT. Idempotent ;
    # préconditions (IOMMU/vfio/hugepages) vérifiées, échecs remontés en alerte (jamais muet), mais un
    # échec de bind ne PLANTE pas le déploiement (best-effort visible).
    try:
        ensure_vfio_binds(node)
    except Exception as _ve:
        db_add_alert("alert.docker.autobind_erreur", "error",
                     vmid=vmid, node_id=node.get("id"), kind="prep",
                     params={"vmid": vmid, "e": str(_ve)})

    try:
        cmd = _build_run_cmd(vmid, node, params)
    except ValueError as _e:   # garde-fou config média (ex. port DPDK sans IP de segment)
        db_add_alert("alert.docker.refuse_config", "error",
                     vmid=vmid, node_id=node.get("id"), kind="deploy",
                     params={"vmid": vmid, "e": str(_e)})
        db_update_status(vmid, "stopped")
        return False
    rc, out, err = ssh_run(node["host"], cmd, timeout=60)
    if rc != 0:
        db_add_alert("alert.docker.deploiement_echoue", "error",
                     vmid=vmid, node_id=node.get("id"), kind="deploy",
                     params={"vmid": vmid, "msg": (err or out)[:200]})
        db_update_status(vmid, "stopped")
        return False

    db_update_status(vmid, "running")

    # source/shm via wiring déclaratif (identique au fallback LXC de deploy.py).
    hn = _hostname_moteur(vmid, params)
    try:
        w = plugins.derive_wiring(type_script, hn, params)
        prod = " · ".join(p.get("shm", "") for p in w["produces"] if p.get("shm"))
    except Exception:
        prod = f"{hn}_0"
    params["plugin_version"] = plugins.resolved_version(type_script, None)
    db_update_source(vmid, "NMOS (IS-05)", prod or f"{hn}_0")
    db_update_deploy_config(vmid, type_script, params)

    # Notify NMOS/Ember+ après écriture du deploy_config (rebuild_model lit deploy_config.type).
    for _mod, _fn in (("nmos", "notify_state_change"), ("emberplus", "notify_change")):
        try:
            from services import nmos, emberplus
            (nmos.notify_state_change if _mod == "nmos" else emberplus.notify_change)()
        except Exception:
            pass

    # Resync du moteur (slots TX + abonnements RX IS-05) : le contrôleur recréé a perdu son état
    # (_tx en mémoire, fichiers SDP dans /tmp du rootfs ÉPHÉMÈRE) → l'orchestrateur, qui ne
    # redémarre pas, le lui rend. UN SEUL thread, SÉQUENTIEL et VÉRIFIÉ (cf. _resync_moteur).
    import threading as _th
    _th.Thread(target=resync_moteur, args=(vmid, params), daemon=True).start()

    # Auto-qualification de la carte (bibliothèque nic_profiles) : au 1er déploiement d'un moteur sur
    # un nœud (par cycle de vie du contrôleur), on qualifie la carte en fond — cap RL TX + PTP + DDP,
    # lus du log/sondes du moteur qui vient de démarrer. narrow_ok reste manuel (exige une sonde
    # loopback). Best-effort, non bloquant : attend la readiness du moteur puis mesure une fois.
    try:
        if node.get("id") not in _auto_qualified_nodes:
            _auto_qualified_nodes.add(node.get("id"))
            import threading as _th
            _th.Thread(target=_auto_qualify_nic, args=(node.get("id"), vmid),
                       daemon=True, name=f"autoqualify-{vmid}").start()
    except Exception as e:
        log.debug("auto-qualify spawn %s: %s", vmid, e)

    db_add_alert("alert.docker.deploye", "info",
                 vmid=vmid, node_id=node.get("id"), kind="deploy",
                 params={"t": type_script, "n": node["name"], "vmid": vmid})
    return True


# Nœuds déjà auto-qualifiés dans ce cycle de vie du contrôleur (évite de re-qualifier à chaque
# redéploiement ; un restart du contrôleur re-qualifie une fois — acceptable). Le bouton « Qualifier »
# (route /api/nodes/<id>/qualify-nic) reste dispo pour forcer une re-mesure.
_auto_qualified_nodes = set()


def _auto_qualify_nic(node_id, vmid):
    """Thread best-effort : attend que le moteur soit prêt (log tx_queues + fenêtre PTP) puis qualifie
    la carte du nœud (nic_qualify). Silencieux si le moteur n'est pas encore mesurable — le bouton
    manuel ou le prochain déploiement rattrapera. narrow_ok non mesuré ici (pas de sonde)."""
    import time as _t
    from .database import db_get_node
    from . import nic_qualify
    node = db_get_node(node_id)
    if not node:
        return
    # Laisser le moteur démarrer (daemon mtl_rx → « tx_queues N malloc succ ») et le PTP tenter son
    # lock (~1-2 min) pour renseigner ptp_ok. Best-effort, une seule passe.
    _t.sleep(90)
    try:
        prof = nic_qualify.qualify_node_via_agent(node, _name(vmid))
        if prof:
            log.info("auto-qualif carte nœud %s : cap=%s ptp=%s ddp=%s (narrow=%s)%s", node_id,
                     prof.get("rl_tx_cap"), prof.get("ptp_ok"), prof.get("ddp_ok"), prof.get("narrow_ok"),
                     ("" if prof.get("rl_tx_cap") else
                      " — capacité TX NON mesurée : %s" % (prof.get("cap_reason") or "?")))
        else:
            _auto_qualified_nodes.discard(node_id)   # échec → réessai au prochain déploiement
    except Exception as e:
        log.warning("auto-qualif carte nœud %s : %s", node_id, e)
        _auto_qualified_nodes.discard(node_id)


def stop_docker(vmid):
    """docker stop PUIS xdp off (toujours). rm best-effort (--rm le fait déjà à l'arrêt)."""
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    name = c.get("docker_name") or _name(vmid)
    if not node:
        return (False, "nœud introuvable")
    from .database import db_set_desired_state
    db_set_desired_state(vmid, "stopped")   # arrêt VOULU → l'auto-recovery ne le relèvera pas
    rc, out, err = ssh_run(node["host"], f"docker stop {shlex.quote(name)} 2>&1", timeout=30)
    _xdp_off(node)   # impératif : MtlManager n'a pas eu le temps de détacher
    db_update_status(vmid, "stopped")
    db_add_alert("alert.docker.arrete",
                 "info" if rc == 0 else "warning",
                 vmid=vmid, node_id=c.get("node_id"), kind="deploy",
                 params={"name": name, "vmid": vmid})
    return (rc == 0, out or err)


def start_docker(vmid):
    """--rm → le conteneur arrêté est supprimé ; on RE-RUN depuis le deploy_config stocké."""
    import json
    from .database import db_set_desired_state
    db_set_desired_state(vmid, "running")   # intention opérateur, même si la tentative échoue
    c = db_get_container(vmid) or {}
    try:
        dc = json.loads(c.get("deploy_config") or "{}")
    except Exception:
        dc = {}
    params = (dc.get("params") or {}) if isinstance(dc, dict) else {}
    if not params:
        db_add_alert("alert.docker.demarrage_sans_config", "warning",
                     vmid=vmid, node_id=c.get("node_id"), kind="deploy",
                     params={"vmid": vmid})
        return False
    return deploy_docker(vmid, params)


def status_docker(vmid):
    """running | exited | absent (docker inspect). 'absent' si conteneur inconnu/SSH KO."""
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    name = c.get("docker_name") or _name(vmid)
    if not node:
        return "absent"
    rc, out, _ = ssh_run(
        node["host"],
        f"docker inspect -f '{{{{.State.Status}}}}' {shlex.quote(name)} 2>/dev/null",
        timeout=10)
    st = (out or "").strip()
    return st if (rc == 0 and st) else "absent"


def destroy_docker(vmid, progress=None):
    """Arrêt GRACIEUX puis rm -f PUIS xdp off (toujours) PUIS suppression de la ligne DB.
    Le stop -t 12 (SIGTERM → _cleanup du contrôleur : free_session sur toutes les sessions →
    Leave IGMP émis + mtl_uninit purge XDP/règles ntuple) est indispensable — un rm -f direct
    (SIGKILL) ne quitte pas les groupes multicast (le switch forwarde jusqu'au timeout du
    querier) et laisse des règles fdir sur la carte (« socket add flow fail » au prochain flow).
    Même rationale que le redéploiement (_build_run_cmd). rm -f ensuite = filet (--rm)."""
    c = db_get_container(vmid) or {}
    node = db_get_node(c.get("node_id"))
    name = c.get("docker_name") or _name(vmid)
    if node:
        ssh_run(node["host"],
                f"docker stop -t 12 {shlex.quote(name)} >/dev/null 2>&1; "
                f"docker rm -f {shlex.quote(name)} >/dev/null 2>&1; "
                # Purge du matériel mTLS bind-monté (best-effort, no-op si HTTP clair)
                f"rm -rf {shlex.quote(_tls_host_dir(name))} >/dev/null 2>&1", timeout=45)
        _xdp_off(node)
    db_delete_container(vmid)
    db_add_alert("alert.docker.detruit", "info",
                 vmid=vmid, node_id=c.get("node_id"), kind="deploy",
                 params={"name": name, "vmid": vmid})
    return True
