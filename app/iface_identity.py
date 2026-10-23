# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Identité STABLE des interfaces déclarées d'un nœud (Réglages → Nœuds → Réseau).

★ POURQUOI. Tout ce qui touche une carte réseau — IP RDMA gravée, parent macvlan, liens de
réplication, moteur 2110 — la désigne par son NOM noyau. Or un nom « prévisible » (`enp132s0`)
encode l'emplacement PCI : ajouter une carte dans le serveur peut renuméroter les bus et RENOMMER
les autres. Vécu le 2026-10-20 sur dell-1 : la Quad 2 posée au slot 4 a fait passer la Mellanox de
`84:00` à `88:00`, `enp132s0` est devenue `enp136s0`, les deux IP RDMA ont disparu, et tous les
liens de réplication ont échoué en boucle pendant deux heures — affichés « running » pour certains.
Ni le nom ni le BDF persistés ne permettaient de retrouver la carte.

Trois mécanismes, du plus préventif au plus défensif :

1. **MAC enregistrée** à la déclaration (colonne `node_interfaces.mac`, jusqu'ici jamais écrite) —
   l'adresse PERMANENTE de la carte, qui la suit quel que soit le slot ;
2. **nom épinglé** par un fichier `.link` systemd (`/etc/systemd/network/10-bobi-<if>.link`) :
   « la carte de MAC X s'appelle <if> ». Appliqué par udev à l'apparition du netdev, il garde le
   nom à travers un changement de slot — c'est le mécanisme qui ÉVITE la panne ;
3. **réconciliation** au fil de la santé des nœuds (`reconcile`) : interface déclarée absente →
   on cherche la carte de même MAC, on lui REND son nom déclaré et sa configuration ; sinon alerte.
   Elle complète aussi la MAC des déclarations d'avant ce module (backfill), tant que le nom est
   encore juste — vérifié par le BDF.

Les ports média 2110 en DPDK (vfio-pci) n'ont plus de netdev : ni MAC lisible ni nom à épingler.
Ils restent identifiés par leur BDF, comme avant.
"""
import logging
import re
import shlex
import threading
import time

from .database import (db_add_alert, db_get_node_interfaces,
                       db_upsert_node_interface)

log = logging.getLogger(__name__)

LINK_DIR = "/etc/systemd/network"
RECONCILE_INTERVAL_S = 300

# Rôles sans netdev à suivre : la BMC est hors bande (pas une interface de l'hôte).
_NO_NETDEV_ROLES = ("bmc", "unused")

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

# Une ligne par netdev PHYSIQUE (qui a un `device`) : nom|adresse courante|adresse permanente|BDF.
# `ethtool -P` donne la MAC gravée dans la carte ; l'adresse courante peut avoir été changée
# (bond, réglage manuel). Puis la liste des `.link` déjà posés par nous.
_PROBE = (
    "for p in /sys/class/net/*; do n=${p##*/}; [ -e \"$p/device\" ] || continue; "
    "a=$(cat \"$p/address\" 2>/dev/null); "
    "pm=$(ethtool -P \"$n\" 2>/dev/null | awk '{print $NF}'); "
    "b=$(basename \"$(readlink -f \"$p/device\")\"); "
    "echo \"IF|$n|$a|$pm|$b\"; done; "
    "for f in %s/10-bobi-*.link; do [ -e \"$f\" ] || continue; "
    "echo \"LINK|$(basename \"$f\")|$(grep -m1 -i '^PermanentMACAddress=' \"$f\" | cut -d= -f2)\"; done"
    % LINK_DIR
)

_lock = threading.Lock()
_due = {}            # node_id → monotone du prochain passage
_missing = {}        # node_id → {ifname: raison} (interfaces déclarées introuvables)


def _norm_mac(m):
    m = (m or "").strip().lower()
    if not _MAC_RE.match(m) or m == "00:00:00:00:00:00":
        return ""
    return m


def probe(node):
    """Inventaire des netdevs physiques du nœud : ({ifname: {mac, pci}}, {fichier .link: mac}).
    `mac` = adresse permanente, repli sur l'adresse courante. (None, None) si le nœud ne répond pas."""
    from . import node_driver
    rc, out, _err = node_driver.host_exec(node, _PROBE, timeout=30)
    if rc != 0:
        return None, None
    nics, links = {}, {}
    for line in (out or "").splitlines():
        parts = line.strip().split("|")
        if parts[0] == "IF" and len(parts) >= 5:
            nics[parts[1]] = {"mac": _norm_mac(parts[3]) or _norm_mac(parts[2]), "pci": parts[4]}
        elif parts[0] == "LINK" and len(parts) >= 3:
            links[parts[1]] = _norm_mac(parts[2])
    return nics, links


def _link_file(ifname):
    return "%s/10-bobi-%s.link" % (LINK_DIR, ifname)


def _pin_cmd(ifname, mac):
    """Shell qui pose le `.link` « MAC → nom ». Réécrit seulement si le contenu change ; dans ce
    cas l'initramfs est régénéré en arrière-plan : un pilote chargé tôt nommerait la carte AVANT
    que la racine ne soit montée, sans voir le fichier."""
    body = ("# Posé par Bobi.Studio — NE PAS ÉDITER À LA MAIN.\n"
            "# Garde le nom de cette carte quand une autre carte est ajoutée ou déplacée\n"
            "# (le nom « prévisible » dépend de l'emplacement PCI). Retiré quand l'interface\n"
            "# n'a plus de rôle dans Réglages → Nœuds.\n"
            "[Match]\n"
            "PermanentMACAddress=%s\n"
            "\n"
            "[Link]\n"
            "Name=%s\n" % (mac, ifname))
    f = shlex.quote(_link_file(ifname))
    return ("mkdir -p %s; t=$(mktemp); printf '%%s' %s > \"$t\"; "
            "if cmp -s \"$t\" %s; then rm -f \"$t\"; echo SAME; else mv \"$t\" %s; chmod 644 %s; "
            "udevadm control --reload 2>/dev/null; "
            # ⚠ SÉRIALISÉ : une passe épingle plusieurs interfaces d'affilée, et deux
            # update-initramfs concurrents se marchent dessus (`.new` renommé par l'autre).
            # Vécu au premier déploiement, 2026-10-20 : trois en parallèle sur dl360-1 ont laissé
            # l'initramfs du noyau EN COURS illisible — le nœud ne serait pas reparti.
            "command -v update-initramfs >/dev/null && "
            "(nohup flock /run/bobi-initramfs.lock update-initramfs -u "
            ">>/var/log/bobi-initramfs.log 2>&1 &); echo WROTE; fi"
            % (shlex.quote(LINK_DIR), shlex.quote(body), f, f, f))


def pin(node, ifname, nics=None):
    """Enregistre la MAC de `ifname` et épingle son nom. Retourne (ok, message)."""
    if nics is None:
        nics, _links = probe(node)
    if nics is None:
        return (False, "nœud injoignable")
    info = nics.get(ifname)
    if not info:
        return (False, "%s absente du nœud" % ifname)
    mac = info.get("mac")
    if not mac:
        return (False, "MAC de %s illisible" % ifname)
    from . import node_driver
    rc, out, err = node_driver.host_exec(node, _pin_cmd(ifname, mac), timeout=30)
    if rc != 0:
        return (False, ((err or out or "").strip() or "échec")[:160])
    db_upsert_node_interface(node["id"], ifname, mac=mac)
    return (True, mac)


def unpin(node, ifname):
    """Retire le `.link` de `ifname` (interface supprimée ou sans rôle). Best-effort."""
    from . import node_driver
    try:
        node_driver.host_exec(node, "rm -f %s && udevadm control --reload 2>/dev/null; true"
                              % shlex.quote(_link_file(ifname)), timeout=15)
    except Exception as e:                                          # noqa: BLE001
        log.debug("iface_identity.unpin %s/%s: %s", node.get("id"), ifname, e)


def is_missing(node_id, ifname):
    """Raison si l'interface déclarée `ifname` est introuvable sur le nœud au dernier passage."""
    with _lock:
        return (_missing.get(node_id) or {}).get(ifname)


def schedule_now(node_id):
    """Avance le prochain passage (appelé au redémarrage détecté d'un nœud)."""
    with _lock:
        _due[node_id] = 0.0


def check(node):
    """Point d'entrée cadencé (sampler santé). Best-effort, ne lève jamais."""
    nid = node.get("id")
    try:
        now = time.monotonic()
        with _lock:
            if now < _due.get(nid, 0.0):
                return
            _due[nid] = now + RECONCILE_INTERVAL_S
        reconcile(node)
    except Exception as e:                                          # noqa: BLE001
        log.debug("iface_identity node %s: %s", nid, e)


def _restore_config(node, row, ifname):
    """Remet l'interface dans son état déclaré après l'avoir renommée."""
    from . import docker_driver as _dd
    if row.get("role") == "rdma" and row.get("ip_cidr"):
        return _dd.ensure_iface_ip(node, ifname, row["ip_cidr"], persist=True)
    # Autres rôles : la configuration vit dans ifupdown (stanza à son nom), on la rejoue.
    from . import node_driver
    rc, out, err = node_driver.host_exec(
        node, "ifup %s 2>&1 || ip link set dev %s up" % (shlex.quote(ifname), shlex.quote(ifname)),
        timeout=30)
    return (rc == 0, ((out or "") + (err or "")).strip()[:160])


def _rename_back(node, current, wanted):
    """Renomme le netdev `current` en `wanted` (lien descendu le temps du renommage)."""
    from . import node_driver
    c, w = shlex.quote(current), shlex.quote(wanted)
    rc, out, err = node_driver.host_exec(
        node, "ip link set dev %s down && ip link set dev %s name %s && ip link set dev %s up"
              % (c, c, w, w), timeout=30)
    return (rc == 0, ((out or "") + (err or "")).strip()[:160])


def reconcile(node):
    """Confronte les interfaces déclarées du nœud à ses netdevs réels. Voir l'en-tête du module."""
    nid = node.get("id")
    name = node.get("name") or node.get("host") or "nœud %s" % nid
    nics, links = probe(node)
    if nics is None:
        return
    by_mac = {}
    for n, i in nics.items():
        if i.get("mac"):
            by_mac.setdefault(i["mac"], n)
    rows = db_get_node_interfaces(nid)
    declared = {r.get("ifname") for r in rows}
    missing = {}
    for r in rows:
        ifname = (r.get("ifname") or "").strip()
        role = r.get("role") or "unused"
        if not ifname or role in _NO_NETDEV_ROLES:
            continue
        if role == "media2110" and (r.get("pmd") or "") == "dpdk":
            continue                         # vfio-pci : pas de netdev, identifié par son BDF
        mac = _norm_mac(r.get("mac"))
        live = nics.get(ifname)
        if live:
            if not mac:
                # Backfill d'une déclaration d'avant ce module — SEULEMENT si le nom désigne encore
                # la même carte (BDF inchangé) : sinon on graverait la MAC d'une autre carte.
                if r.get("pci") and live.get("pci") and r["pci"] != live["pci"]:
                    missing[ifname] = "pci"
                    _alert_once(nid, ifname, "pci", "alert.net.iface_pci_change", "error",
                                {"n": name, "if": ifname, "old": r["pci"], "new": live["pci"]})
                    continue
                pin(node, ifname, nics)
            elif live.get("mac") and live["mac"] != mac:
                # Le nom est porté par une AUTRE carte. On ne touche à rien : renommer une carte qui
                # n'est pas la nôtre pourrait couper ce qu'elle porte.
                missing[ifname] = "other_card"
                _alert_once(nid, ifname, "other_card", "alert.net.iface_autre_carte", "error",
                            {"n": name, "if": ifname, "mac": mac, "mac_live": live["mac"]})
                continue
            elif links.get("10-bobi-%s.link" % ifname) != mac:
                pin(node, ifname, nics)
            _alert_cleared(nid, ifname, name)
            continue
        # ── Interface déclarée ABSENTE ──
        other = by_mac.get(mac) if mac else None
        if other and other not in declared:
            ok, msg = _rename_back(node, other, ifname)
            if ok:
                ok2, msg2 = _restore_config(node, r, ifname)
                pin(node, ifname, probe(node)[0])
                db_add_alert("alert.net.iface_renommee", "warning" if ok2 else "error",
                             node_id=nid, kind="net",
                             params={"n": name, "if": ifname, "autre": other, "mac": mac,
                                     "conf": "" if ok2 else msg2})
                _alert_cleared(nid, ifname, name, quiet=True)
                continue
            missing[ifname] = "rename_failed"
            _alert_once(nid, ifname, "rename_failed", "alert.net.iface_renommage_echec", "error",
                        {"n": name, "if": ifname, "autre": other, "err": msg})
            continue
        missing[ifname] = "absent"
        _alert_once(nid, ifname, "absent", "alert.net.iface_absente", "error",
                    {"n": name, "if": ifname, "role": role,
                     "mac": mac or "?"})
    with _lock:
        _missing[nid] = missing


_alerted = {}        # (node_id, ifname) → motif déjà alerté (alerte à TRANSITION)


def _alert_once(nid, ifname, motif, key, level, params):
    if _alerted.get((nid, ifname)) == motif:
        return
    _alerted[(nid, ifname)] = motif
    db_add_alert(key, level, node_id=nid, kind="net", params=params)


def _alert_cleared(nid, ifname, name, quiet=False):
    if _alerted.pop((nid, ifname), None) and not quiet:
        db_add_alert("alert.net.iface_retrouvee", "info", node_id=nid, kind="net",
                     params={"n": name, "if": ifname})
