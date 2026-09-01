# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Réseau par nœud : plan conteneurs (macvlan, assigné après l'enrôlement), carte E810/PTP
(io2110), et vue « interface → rôle » (management/containers/media2110/rdma/bmc)."""

import json
import logging
import re

from flask import jsonify, request

from . import bp, _node_host
from .shared import _fetch_host_nics, _mtl_total_queues, _ptp_apply_core
from .images import _build_host, _ssh_bin, _present_tags, _image_tag, _image_present
from .nodes import _node_build_status
from ..auth import require_perm, require_login
from ..addressing import (controller_ipv4s as _controller_ipv4s,
                          controller_on_subnet as _controller_on_subnet,
                          controller_route_to as _controller_route_to)
log = logging.getLogger(__name__)

from ..database import (db_get_node, db_get_nodes, db_get_containers, db_update_node,
                        db_add_alert, db_get_node_interfaces, db_upsert_node_interface,
                        db_delete_node_interface, get_db)


# ─── Réseau containers par nœud (assigné APRÈS l'enrôlement, via l'agent) ───────
# Phase 2 : le réseau containers (macvlan) n'est plus figé à l'install. L'agent remonte l'inventaire
# NIC (/v1/capabilities) → l'opérateur choisit le parent depuis l'UI → l'orchestrateur crée le réseau
# via /v1/host/networks/ensure. subnet/gw/range = réglages CLUSTER (pool d'IP) ; carte parent + VLAN
# (si trunk) = PAR-NŒUD (saisis dans le form de la carte du nœud).
def _controller_reach(subnet, gateway=""):
    """Synthèse de la joignabilité orchestrateur → plan conteneurs : IP DIRECTE (L2) OU ROUTE (L3).
    {on_subnet, route:{has_route,direct,via,dev}, probe_ip}. probe_ip = passerelle (ou 1er host du
    subnet) = cible des tests route/ping. Une route présente suffit à joindre les conteneurs même sans
    IP directe sur le subnet (cas routage)."""
    import ipaddress as _ip
    subnet = (subnet or "").strip()
    probe = (gateway or "").strip()
    if not probe and subnet:
        try:
            probe = str(next(_ip.ip_network(subnet, strict=False).hosts()))
        except Exception:
            probe = ""
    return {"on_subnet": _controller_on_subnet(subnet),
            "route": _controller_route_to(probe), "probe_ip": probe}


@bp.route("/api/net/reach-test", methods=["GET"])
@require_perm("settings.edit")
def api_net_reach_test():
    """Test de joignabilité RÉEL du plan conteneurs depuis l'orchestrateur : ping de la passerelle (L3)
    + connexion TCP :8081 vers un conteneur déployé sur le subnet (preuve de bout en bout) si présent."""
    from ..addressing import ping as _ping, tcp_reachable as _tcp
    import ipaddress as _ip
    sug = _macvlan_suggest()
    gw = sug.get("gateway") or ""
    subnet = sug.get("subnet") or ""
    out = {"gateway": gw, "subnet": subnet, "gateway_ping": (_ping(gw) if gw else None)}
    target = None
    try:
        net = _ip.ip_network(subnet, strict=False) if subnet else None
        for c in db_get_containers():
            dip = (c.get("docker_ip") or "").strip()
            if dip and (net is None or _ip.ip_address(dip) in net):
                target = dip
                break
    except Exception:
        target = None
    out["container"] = ({"ip": target, "port": 8081, "ok": _tcp(target, 8081)} if target else None)
    return jsonify(out)


@bp.route("/api/nodes/<int:node_id>/container-network", methods=["GET"])
@require_perm("settings.edit")
def api_node_container_network_get(node_id):
    import shlex as _sh
    from .. import node_driver, settings as _st
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    if not (node.get("agent_url") or "").strip():
        return jsonify({"error": "nœud sans agent (enrôlement non finalisé ?)"}), 409
    caps = node_driver.capabilities(node) or {}
    sug = _macvlan_suggest(node_id)
    # Divergence entre le réseau RÉELLEMENT posé et ce que les rôles déclarent. Sans ce constat, un
    # nœud dont le macvlan pend dans le vide s'affiche parfaitement sain : il répond au ping, son
    # agent va bien, ses capacités sont déclarées — et aucun de ses conteneurs n'est joignable.
    etat_res = node_driver.etat_reseau_conteneurs(node)
    return jsonify({
        "nics": caps.get("nics") or [],
        "networks": caps.get("networks") or [],
        "current": node.get("docker_network") or "",
        "parent_declare": etat_res["declare"],
        "parent_reel": etat_res["reel"],
        "parent_porteuse": etat_res["porteuse"],
        # `derive` = le réseau contredit les rôles ; `sans_lien` = il pointe une carte sans porteuse.
        # Deux défauts distincts : le premier se corrige en recréant le réseau, le second au câblage.
        "parent_derive": etat_res["derive"],
        "parent_sans_lien": etat_res["sans_lien"],
        "suggest": sug,
        "controller_on_subnet": _controller_on_subnet(sug.get("subnet")),
        "controller_reach": _controller_reach(sug.get("subnet"), sug.get("gateway")),
        "controller_ips": _controller_ipv4s(),
    })

@bp.route("/api/nodes/<int:node_id>/container-network", methods=["POST"])
@require_perm("settings.edit")
def api_node_container_network_set(node_id):
    import shlex as _sh
    from .. import node_driver, settings as _st
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    if not (node.get("agent_url") or "").strip():
        return jsonify({"error": "nœud sans agent (enrôlement non finalisé ?)"}), 409
    d = request.json or {}
    parent = (d.get("parent") or "").strip()
    declare = _parent_declare(node_id)
    if not parent:
        parent = declare                       # défaut : ce que les rôles déclarent
    if not parent:
        return jsonify({"error": "interface parent requise — ou déclarer le rôle « Management + "
                                 "Containers » sur la carte concernée (Réseau → Interfaces)"}), 400
    # Un parent CONTRAIRE au rôle déclaré est refusé, pas appliqué en silence : c'est exactement la
    # divergence qui a rendu r620-3 inutilisable pendant des jours. Comparaison sur la carte de base
    # (un VLAN taggé donne `eno1.20`, qui reste porté par `eno1`).
    if declare and parent.split(".")[0] != declare:
        return jsonify({"error": "parent « %s » contraire au rôle déclaré : la carte des conteneurs "
                                 "de ce nœud est « %s » (Réseau → Interfaces). Corriger le rôle, ou "
                                 "choisir cette carte." % (parent, declare)}), 400
    sug = _macvlan_suggest(node_id)
    name    = (d.get("name") or sug.get("name") or "bobimacvlan").strip()
    subnet  = (d.get("subnet") or sug.get("subnet") or "").strip()
    gateway = (d.get("gateway") or sug.get("gateway") or "").strip()
    iprange = (d.get("ip_range") or sug.get("ip_range") or "").strip()
    if not subnet:
        return jsonify({"error": "subnet introuvable — configurer le pool d'IP (Réseau → IP statiques)"}), 400
    # Passerelle macvlan : un conteneur macvlan ne peut JAMAIS joindre son hôte parent. Si la passerelle
    # est vide ou pointe sur une IP de l'hôte du nœud, on la DÉRIVE de la route par défaut du nœud (le vrai
    # routeur du segment). Garde-fou : refuser explicitement une passerelle == IP de l'hôte (sinon les
    # conteneurs n'ont aucune route off-subnet → l'orchestrateur ne les joint pas, et inversement).
    _hip = _node_host_ipv4s(node.get("host"))
    _ngw = _node_default_gateway(node.get("host"))
    if (not gateway or gateway in _hip) and _ngw and _ngw not in _hip:
        gateway = _ngw
    if gateway and gateway in _hip:
        return jsonify({"error": "passerelle %s = une IP de l'hôte du nœud : invalide en macvlan (un "
                        "conteneur ne peut pas joindre son hôte parent, donc aucune route hors subnet). "
                        "Utilise le routeur du segment%s — Réglages → Réseau → passerelle."
                        % (gateway, (" (ex. %s)" % _ngw) if _ngw else "")}), 400
    # VLAN taggé (réglage PAR-NŒUD : seulement si la carte parent est un port trunk) : parent =
    # <nic>.<vid>. La sous-interface est créée sur le nœud via l'agent ET persistée (unité systemd
    # oneshot, comme install-node.sh) → survit au reboot. Sinon (port access/untagged), parent nu.
    vlan = str(d.get("vlan") or "").strip()
    if vlan and vlan != "0" and "." not in parent:
        vif = "%s.%s" % (parent, vlan)
        script = "/usr/local/sbin/bobi-vlan-%s.sh" % vif
        cmd = (
            "set -e; modprobe 8021q 2>/dev/null || true; "
            "cat > %s <<'SH'\n"
            "#!/bin/sh\n"
            "ip link show %s >/dev/null 2>&1 || ip link add link %s name %s type vlan id %s\n"
            "ip link set %s up; ip link set %s up\n"
            "SH\n"
            "chmod +x %s; sh %s; "
            "cat > /etc/systemd/system/bobi-vlan-%s.service <<UNIT\n"
            "[Unit]\nDescription=Sous-interface VLAN %s (parent macvlan bobi)\n"
            "After=network-pre.target\nWants=network-pre.target\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=%s\n"
            "[Install]\nWantedBy=multi-user.target\nUNIT\n"
            "systemctl daemon-reload; systemctl enable bobi-vlan-%s.service"
            % (script, vif, parent, vif, vlan, parent, vif, script, script,
               vif, vif, script, vif)
        )
        ok, res = node_driver.host_exec(node, cmd, timeout=25)
        if not ok or (isinstance(res, dict) and res.get("rc")):
            return jsonify({"error": "création/persistance VLAN %s échouée : %s" % (vif, res)}), 500
        parent = vif
    else:
        # Pas de VLAN : la carte parent macvlan n'a pas besoin d'IP, mais doit être MONTÉE (porteur).
        # On la monte + persiste (unité oneshot, comme le chemin VLAN) → up au reboot. Best-effort :
        # `docker network create` peut réussir si elle est déjà up, on ne bloque pas la création.
        unit = "bobi-link-%s" % parent
        cmd = (
            "ip link set %s up 2>/dev/null || true; "
            "cat > /etc/systemd/system/%s.service <<UNIT\n"
            "[Unit]\nDescription=Carte parent macvlan %s (lien UP)\n"
            "After=network-pre.target\nWants=network-pre.target\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/sbin/ip link set %s up\n"
            "[Install]\nWantedBy=multi-user.target\nUNIT\n"
            "systemctl daemon-reload; systemctl enable %s.service 2>/dev/null || true"
            % (parent, unit, parent, parent, unit)
        )
        try:
            node_driver.host_exec(node, cmd, timeout=20)
        except Exception:
            pass
    # IPAM CENTRALISÉ (séparé OU multi-nœud) : l'orchestrateur impose chaque IP (`docker run --ip`),
    # qui doit juste être dans le `--subnet`. On N'impose PAS `--ip-range` (le plus grand bloc CIDR du
    # pool ne couvre pas forcément tout `ip_start..ip_end` → un --ip hors range échouerait). En IPAM
    # Docker (simple mono-nœud), on GARDE `--ip-range` pour confiner l'auto-allocation au pool.
    from .. import allocations
    iprange_eff = "" if allocations.centralized_ipam(node_id) else iprange
    # RECRÉATION SI PARAMÈTRES CHANGÉS : `ensure_network` est idempotent (create-si-absent). Un réseau
    # déjà présent avec une AUTRE passerelle/subnet/parent ne serait donc JAMAIS mis à jour (cas vécu :
    # passerelle restée à l'IP de l'hôte). On inspecte l'existant ; s'il diffère, on le DÉTRUIT (avec ses
    # conteneurs attachés, forcément à redéployer) puis on le recrée avec les bons paramètres.
    import json as _json
    rc_i, out_i, _e = node_driver.host_exec(node, "docker network inspect %s 2>/dev/null" % _sh.quote(name), timeout=15)
    try:
        _arr = _json.loads(out_i) if (out_i or "").strip() else []
        existing_net = _arr[0] if _arr else None
    except Exception:
        existing_net = None
    if existing_net:
        _cfg = (existing_net.get("IPAM", {}).get("Config") or [{}])[0]
        cur_gw = _cfg.get("Gateway") or ""
        cur_sub = _cfg.get("Subnet") or ""
        cur_parent = (existing_net.get("Options") or {}).get("parent") or ""
        if (gateway and cur_gw != gateway) or (subnet and cur_sub != subnet) or (parent and cur_parent != parent):
            att_names = {a.get("Name") for a in (existing_net.get("Containers") or {}).values() if a.get("Name")}
            from ..docker_compute import destroy_compute
            removed = [cc["vmid"] for cc in db_get_containers()
                       if cc.get("node_id") == node_id
                       and cc.get("docker_name") in att_names]
            for v in removed:
                try: destroy_compute(v)
                except Exception: pass
            # rm résiduel (conteneurs non suivis en DB) + suppression du réseau
            node_driver.host_exec(node,
                "for c in $(docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' %s 2>/dev/null); do "
                "docker rm -f \"$c\" >/dev/null 2>&1; done; docker network rm %s >/dev/null 2>&1"
                % (_sh.quote(name), _sh.quote(name)), timeout=60)
            db_add_alert("alert.net.macvlan_recree", "warning", node_id=node.get("id"), kind="net",
                         params={"name": name, "n": node.get("name"), "gw_avant": cur_gw or "?",
                                 "gw": gateway, "count": len(removed)})
    ok, res = node_driver.ensure_network(node, name, parent=parent, subnet=subnet,
                                         gateway=gateway, ip_range=iprange_eff)
    if not ok:
        return jsonify({"error": "création réseau via agent échouée : %s" % res}), 500
    db_update_node(node_id, docker_network=name)
    # Joignabilité orchestrateur ↔ subnet conteneurs (le point clé du mode fusionné).
    reach = _controller_reach(subnet, gateway)
    on_subnet = reach["on_subnet"]
    has_route = bool(reach["route"].get("has_route"))
    allinone = (node.get("host") or "") in _controller_ipv4s()
    warning = None
    # Rouge seulement si NI IP directe NI route : sinon le routage (L3) peut suffire.
    if on_subnet is False and not has_route:
        warning = ("l'orchestrateur n'a ni IP ni route vers %s → il ne pourra pas joindre les conteneurs "
                   "(fusionné : mets l'orchestrateur sur ce LAN ; séparé : provisionne sa patte "
                   "conteneurs ou un routage — Réglages → Réseau)" % subnet)
    elif allinone:
        warning = ("orchestrateur et nœud sur la même machine : l'isolation macvlan empêche l'hôte de "
                   "joindre ses propres conteneurs (shim macvlan requis)")
    # `warning` alimente AUSSI `resp["warning"]` (retour UI) : on ne le touche pas — l'alerte est
    # composée à part, en clé complète par variante (ce n'est PAS une liste de données, mais deux
    # diagnostics distincts, cf. piège n°3).
    if on_subnet is False and not has_route:
        _cle_net = "alert.net.macvlan_pret_sans_route"
        _params_net = {"name": name, "n": node.get("name"), "parent": parent, "subnet": subnet}
    elif allinone:
        _cle_net = "alert.net.macvlan_pret_allinone"
        _params_net = {"name": name, "n": node.get("name"), "parent": parent}
    else:
        _cle_net = "alert.net.macvlan_pret"
        _params_net = {"name": name, "n": node.get("name"), "parent": parent}
    db_add_alert(_cle_net, "warning" if warning else "info", node_id=node.get("id"), kind="net",
                 params=_params_net)
    resp = {"ok": True, "name": name, "parent": parent, "agent": res}
    if warning:
        resp["warning"] = warning
    return jsonify(resp)

@bp.route("/api/net/topology-status", methods=["GET"])
@require_perm("settings.edit")
def api_net_topology_status():
    """Statut live du plan réseau pour le bloc Réglages → Réseau cluster (mode séparé) : topologie,
    subnet conteneurs (= pool d'IP), et joignabilité de l'orchestrateur (sa patte conteneurs).
    En séparé, cette patte est provisionnée par l'OPÉRATEUR ; on ne fait que la vérifier + guider."""
    from .. import settings as _st
    sug = _macvlan_suggest()
    subnet = sug.get("subnet") or ""
    return jsonify({
        "topology": _st.get("net_topology") or "simple",
        "container_subnet": subnet,
        "container_gateway": sug.get("gateway") or "",
        "container_range": sug.get("ip_range") or "",
        "controller_on_subnet": _controller_on_subnet(subnet),
        "controller_reach": _controller_reach(subnet, sug.get("gateway")),
        "controller_ips": _controller_ipv4s(),
    })

# ─── io2110 d'un nœud : choix de la carte E810 + PTP (config différée post-enrôlement) ──────────
# Phase 2 : l'install io2110 est différée (pas de carte/PTP) ; ici on choisit l'E810 (inventaire
# remonté par l'agent) et on pousse le PTP. Réutilise _ptp_apply_core (cœur PTP) + ptp.py.
def _push_agent_mtl_iface(node, iface):
    """Écrit mtl_iface dans /etc/bobi-node-agent/config.json du nœud + restart agent (relit CONFIG
    chargé une fois à l'import) → corrige l'auto-report agent + la page :80. Best-effort : l'orchestrateur
    reste la source de vérité pour le PTP même si ça échoue (deploy/status passent l'iface explicitement).
    Le restart est DIFFÉRÉ via `systemd-run` (unité transitoire HORS du cgroup de l'agent) : un
    `systemctl restart` lancé DANS le handler exec de l'agent se tuerait lui-même en plein vol (cgroup
    stoppé) et pourrait laisser l'agent arrêté. À N'APPELER QU'EN DERNIER (l'agent devient injoignable
    ~2 s)."""
    from .. import node_driver
    import shlex as _sh
    py = ("import json;p='/etc/bobi-node-agent/config.json';"
          "c=json.load(open(p));c['mtl_iface']=%r;"
          "json.dump(c,open(p,'w'),indent=2)" % iface)
    cmd = ("python3 -c %s && systemd-run --on-active=2 --quiet "
           "systemctl restart bobi-node-agent" % _sh.quote(py))
    try:
        node_driver.host_exec(node, cmd, timeout=20)
    except Exception:
        pass

@bp.route("/api/nodes/<int:node_id>/io2110", methods=["POST"])
@require_perm("settings.edit")
def api_node_io2110(node_id):
    from .. import node_driver, settings as _st, ptp
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    if not (node.get("agent_url") or "").strip():
        return jsonify({"error": "nœud sans agent (enrôlement non finalisé ?)"}), 409
    d = request.json or {}
    mtl_iface  = (d.get("mtl_iface") or "").strip()
    enable_ptp = bool(d.get("enable_ptp"))
    media_ip   = (d.get("media_ip") or "").strip()   # IP du plan média 2110 (CIDR), '' = inchangé
    if not mtl_iface:
        return jsonify({"error": "carte E810 (mtl_iface) requise"}), 400
    if media_ip:
        import re as _re
        if not _re.match(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$", media_ip):
            return jsonify({"error": "IP média invalide — attendu en CIDR (ex. 198.51.100.60/24)"}), 400
    # 1) Persister la carte choisie + l'IP média sur le nœud (whitelist db_update_node).
    db_update_node(node_id, mtl_iface=mtl_iface)
    if media_ip:
        db_update_node(node_id, media_ip=media_ip)
    # 2) Migration + hygiène horloge (via l'agent, AVANT de déployer les mxl-*) :
    #    - couper d'éventuelles vieilles unités ptp4l/phc2sys (nœuds pré-Phase-2), sinon deux
    #      servos se battent pour le PHC ;
    #    - quand on prend la main sur l'horloge (PTP activé), couper aussi les démons NTP
    #      concurrents (timesyncd/chrony/ntp) qui ramèneraient CLOCK_REALTIME à l'UTC contre
    #      phc2sys → servo en butée, jamais calé (PROD-009). ptp.start() le refait par SSH (filet
    #      pour le bouton « Appliquer »), mais on le pose ici explicitement au « Rendre Opérationnel ».
    #    No-op si absents.
    _hygiene = "systemctl disable --now ptp4l phc2sys 2>/dev/null || true"
    if enable_ptp:
        _hygiene += ("; timedatectl set-ntp false 2>/dev/null || true"
                     "; systemctl disable --now " + " ".join(ptp.COMPETING_TIMESYNC) + " 2>/dev/null || true")
    try:
        node_driver.host_exec(node, _hygiene, timeout=20)
    except Exception:
        pass
    # 2b) Assigner l'IP du plan média 2110 sur la carte (idempotent, via l'agent). AVANT le restart
    #     agent. Sans elle : sip=0.0.0.0 → TX rejeté + IGMPv3 SSM KO → rx_gbps=0 (free-run noir).
    if media_ip:
        from .. import docker_driver as _dd
        _node_fresh = dict(node); _node_fresh["mtl_iface"] = mtl_iface; _node_fresh["media_ip"] = media_ip
        _mok, _mmsg = _dd.ensure_media_ip(_node_fresh)
    host = _node_host(node_id) or node.get("host")
    if enable_ptp:
        ptp.install(host)                                    # idempotent
    # 3) Appliquer le PTP via le cœur commun (domaine cluster par défaut). TOUTES les ops agent
    #    (disable/install/deploy) se font AVANT le restart de l'agent (étape 4), sinon elles tapent
    #    l'agent pendant son redémarrage → Connection refused.
    domain_default = int(_st.get("ptp_domain") or 127)
    data = {
        "enabled": enable_ptp, "ifname": mtl_iface,
        "domain": d.get("domain", domain_default),
        "hw_ts":  d.get("hw_ts", True),
        "priority1": d.get("priority1"), "priority2": d.get("priority2"),
        "log_announce": d.get("log_announce"), "log_sync": d.get("log_sync"),
        "log_delay_req": d.get("log_delay_req"), "announce_to": d.get("announce_to"),
        "delay_thresh": d.get("delay_thresh"), "utc_offset": d.get("utc_offset"),
    }
    ok, msg, code = _ptp_apply_core(node_id, data)
    # 4) EN DERNIER : pousser mtl_iface dans la config agent + restart différé (l'agent devient
    #    injoignable ~2 s — plus aucune op agent après ce point).
    _push_agent_mtl_iface(node, mtl_iface)
    # Deux conditions indépendantes, chacune avec un VERDICT (OK/échec) : ni l'une ni l'autre ne
    # peut voyager en paramètre (piège n°3) → une clé complète par combinaison (3 états PTP ×
    # 3 états média = 9). `_mmsg` (diagnostic dynamique de `ensure_media_ip`) reste, lui, un
    # paramètre — comme `{e}` ailleurs, ce n'est pas une phrase figée d'ici.
    _ptp_etat = ("ptpok" if (enable_ptp and ok) else "ptpko" if enable_ptp else "noptp")
    _media_etat = ("mediaok" if (media_ip and _mok) else "mediako" if media_ip else "nomedia")
    _cle_io = f"alert.net.io2110_carte_{_ptp_etat}_{_media_etat}"
    _params_io = {"n": node.get("name"), "iface": mtl_iface}
    if media_ip and not _mok:
        _params_io["mmsg"] = _mmsg
    db_add_alert(_cle_io, "info" if ok else "error", node_id=node_id, kind="net", params=_params_io)
    body = {"ok": ok, "mtl_iface": mtl_iface}
    body["msg" if ok else "error"] = msg
    return jsonify(body), code


# ─── Vue réseau par nœud : modèle « interface → rôle » (refonte réseau) ──────
# mgmt_containers = rôle COMBINÉ « Management + Containers » : la carte porte à la fois l'IP de
# contrôle du nœud et le parent macvlan des conteneurs (nœud sur un autre LAN que le cluster).
# Tests de rôle : database.role_is_management / role_is_containers (jamais de littéral).
NODE_IFACE_ROLES = ("management", "mgmt_containers", "containers", "media2110", "rdma", "bmc", "unused")

def _engine_state(node_id):
    """État du moteur 2110_io du nœud, pour la vue réseau.

    `present` répond à une question que l'UI ne pouvait pas poser : le moteur est le SEUL type que
    la palette refuse de créer (manifeste `auto_provision`, rejet 400 dans `routes.creer`). Détruit
    — par un RAZ, ou depuis la page Containers — il ne revenait que par un effet de bord : ré-éditer
    un port média, ou attendre `backfill_node_engines` 45 s après un redémarrage de l'orchestrateur.
    Un nœud avec des ports média et sans moteur est donc un ÉTAT ANORMAL qui doit se voir et se
    réparer d'un clic (`POST /api/nodes/<id>/engine`)."""
    from ..database import db_get_containers, db_get_node_interfaces
    from ..docker_compute import is_mtl_type, _type_of
    from ..docker_driver import _is_probe_type
    try:
        media = [r for r in db_get_node_interfaces(node_id)
                 if r.get("role") == "media2110" and (r.get("ip_cidr") or "").strip()]
        eng = next((c for c in db_get_containers()
                    if c.get("node_id") == node_id
                    and is_mtl_type(_type_of(c)) and not _is_probe_type(_type_of(c))), None)
        return {"present": bool(eng),
                "vmid": eng.get("vmid") if eng else None,
                "media_ports": len(media),
                # Le seul cas qui appelle une action : des ports média, pas de moteur.
                "manquant": bool(media) and not eng}
    except Exception as e:
        log.warning("état moteur 2110 (nœud %s) : %s", node_id, e)
        return {"present": None, "vmid": None, "media_ports": 0, "manquant": False}


@bp.route("/api/nodes/<int:node_id>/engine", methods=["POST"])
@require_perm("containers.deploy")
def api_node_engine_provision(node_id):
    """(Re)provisionne LE moteur 2110_io du nœud — l'action de rattrapage qui manquait.

    Même chemin que l'auto-provisionnement (`docker_driver.ensure_node_engine`, idempotent) : on
    n'ouvre pas une seconde façon de créer un moteur, on rend JOIGNABLE celle qui existe. Le moteur
    revient aux `deploy_defaults` du plugin — ses flux RX/TX sont à reconfigurer, c'est le prix de
    la destruction, pas de cette route. Long (création + déploiement) → thread + alertes
    (`alert.docker.auto_provisionne` / `auto_provision_erreur`), comme le hook d'interface."""
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    etat = _engine_state(node_id)
    if etat.get("present"):
        return jsonify({"ok": True, "deja": True, "vmid": etat.get("vmid")})
    if not etat.get("media_ports"):
        return jsonify({"error": "aucun port média 2110 avec IP sur ce nœud — configurez-en un "
                                 "avant de provisionner le moteur"}), 409
    import threading as _t
    from .. import docker_driver as _dd
    _t.Thread(target=_dd.ensure_node_engine, args=(node_id,), daemon=True).start()
    return jsonify({"ok": True, "lance": True}), 202


@bp.route("/api/nodes/<int:node_id>/interfaces", methods=["GET"])
@require_perm("settings.edit")
def api_node_interfaces(node_id):
    """Vue réseau d'un nœud : fusion config (node_interfaces) + état live (lien/IP/débit via SSH) +
    lock PTP des media2110 + tuile BMC. Alimente la vue graphique par nœud."""
    from .. import ptp, ilo as bmc
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    host = _node_host(node_id) or node.get("host")
    cfg = {r["ifname"]: r for r in db_get_node_interfaces(node_id) if r.get("ifname")}
    from ..database import db_get_media_networks as _dgmn, db_media_network_in_use as _dmniu
    _all_nets = _dgmn()
    for _n in _all_nets:                 # nb de NIC rattachées (tous nœuds) — colonne + garde ✕
        _n["in_use"] = _dmniu(_n["id"])
    _nets_by_id = {n["id"]: n for n in _all_nets}
    ok_live, live_err, nics = _fetch_host_nics(host) if host else (False, "hôte indéterminé", [])
    live = {n["name"]: n for n in nics if n.get("name")}
    # Cartes bindées vfio-pci (moteur DPDK) : pas de netdev → keyées sur le BDF. Servent à retrouver
    # une media2110 qui a « disparu » de /sys/class/net parce qu'elle est passée en vfio-pci.
    vfio_by_bdf = {n["pci"]: n for n in nics if n.get("vfio_bound") and n.get("pci")}
    # Lock PTP (par-hôte aujourd'hui ; étiquette les media2110). cached_status = lecture cache, pas de SSH.
    try:
        ptp_st = ptp.cached_status(node_id) or {}
    except Exception:
        ptp_st = {}
    # Synchro au GM (ptp.clock_ok) — `locked` seul est le critère de l'ère AF_XDP et étiquetait
    # « non verrouillé » les media2110 d'un nœud DPDK parfaitement synchronisé.
    ptp_locked = ptp.clock_ok(ptp_st)
    _ptp_port_states = ptp_st.get("port_states") or {}    # {ifname -> SLAVE|PASSIVE|LISTENING|…}
    # État des ports IB relevé par le sampler RDMA (cache, pas de SSH) → {ifname: device}.
    try:
        from services import rdma as _rdma_svc
        _rdma_ports = {d.get("net"): d for d in (_rdma_svc.stats_for_node(node_id).get("devices") or [])
                       if d.get("net")}
    except Exception:
        _rdma_ports = {}

    # Union des interfaces connues (config ∪ live). Le BMC est out-of-band → ligne config sans live.
    names = list(dict.fromkeys(list(cfg.keys()) + [n["name"] for n in nics if n.get("name")]))
    # Plafond de files AF-XDP par port (E810). Budget live du moteur si dispo, sinon réglage
    # `mtl_xdp_total_queues` (défaut 48). Sert à borner les champs « files à réserver » des media2110.
    _xdp_hw = _mtl_total_queues()
    out = []
    for name in names:
        c = cfg.get(name) or {}
        lv = live.get(name) or {}
        role = c.get("role") or "unused"
        # Carte media2110 « disparue » de /sys/class/net car bindée vfio-pci : on la retrouve par son
        # BDF persisté (`pci`, figé tant qu'elle était sur ice). Le moteur DPDK la pilote → PRÉSENTE.
        _vf = vfio_by_bdf.get(c.get("pci")) if (role == "media2110" and c.get("pci")) else None
        ip_cidr = c.get("ip_cidr")
        if not ip_cidr and lv.get("ipv4"):
            a0 = lv["ipv4"][0]
            ip_cidr = "%s/%s" % (a0.get("local"), a0.get("prefix"))
        out.append({
            "ifname":      name,
            "mac":         c.get("mac") or lv.get("mac"),
            "pci":         lv.get("pci") or c.get("pci"),
            "role":        role,
            "pair_role":   c.get("pair_role"),
            "pair_group":  c.get("pair_group"),
            "ip_cidr":     ip_cidr,
            "gateway":     c.get("gateway"),
            "vlan":        c.get("vlan"),
            "ptp_enabled": bool(c.get("ptp_enabled")),
            "ptp_domain":  c.get("ptp_domain"),
            "media_network_id":   c.get("media_network_id"),
            "media_network_name": (_nets_by_id.get(c.get("media_network_id")) or {}).get("name"),
            # Réserve de files 2110 par interface (capacité « à chaud » choisie ; NULL = auto) + plafond
            # de files du port (E810). N'a de sens que sur les media2110.
            "rx_reserve":   c.get("rx_reserve"),
            "tx_reserve":   c.get("tx_reserve"),
            "queue_margin": c.get("queue_margin"),
            # Profil d'émetteur ST 2110-21 (chantier narrow) + alias opérateur (schéma partagé,
            # commit 7975640). N'ont de sens que sur les media2110 (profil) ; alias affiché partout.
            "output_profile": c.get("output_profile") if role == "media2110" else None,
            # Mode PMD (af_xdp | dpdk) : détermine si le rate limiter matériel (narrow) EXISTE sur ce
            # port — donc si le profil d'émission ci-dessus est actif ou inerte (cf. docs/reference/TX_LAYOUTS.md).
            "pmd":            (c.get("pmd") or "af_xdp") if role == "media2110" else None,
            "alias":          c.get("alias"),
            # Plage IP conteneurs PAR NŒUD (cartes containers/mgmt_containers uniquement).
            "ct_ip_start":  c.get("ct_ip_start"),
            "ct_ip_end":    c.get("ct_ip_end"),
            "xdp_hw":       _xdp_hw if role == "media2110" else None,
            "mtu":         lv.get("mtu") or c.get("mtu"),
            "configured":  name in cfg,
            # DPDK (vfio-pci) : la carte n'a plus de netdev kernel — c'est NORMAL, le moteur la
            # pilote. On la marque présente (link « up » logique) au lieu de down/absente.
            "dpdk_bound":  bool(_vf),
            "link_up":     True if _vf else lv.get("link"),
            "operstate":   "dpdk" if _vf else lv.get("operstate"),
            "rx_bytes":    lv.get("rx_bytes"),
            "tx_bytes":    lv.get("tx_bytes"),
            "is_pf":       lv.get("is_pf"),
            "is_vf":       lv.get("is_vf"),
            "ptp_locked":  ptp_locked if role == "media2110" else None,
            "ptp_port_state": _ptp_port_states.get(name) if role == "media2110" else None,
            # Capacités matérielles (sonde /sys) : groupement par carte + badges 2110/RDMA/débit.
            "card_id":     lv.get("card_id") or (_vf and _vf.get("card_id")),
            "driver":      lv.get("driver") or (_vf and "vfio-pci"),
            "speed_mbps":  lv.get("speed_mbps"),
            "max_speed_mbps": lv.get("max_speed_mbps"),
            "rdma":        bool(lv.get("rdma")),
            "rdma_kind":   lv.get("rdma_kind"),
            # État RDMA RÉEL du port, seulement là où il est promis (rôle rdma) : device verbs
            # présent ? module IB attendu/chargé ? état du port IB (sampler). Sans ça, la ligne
            # n'affichait que la porteuse Ethernet — un point vert sur une carte incapable de RoCE.
            "rdma_state":  ({
                "verbs":            bool(lv.get("rdma")),
                "module":           lv.get("rdma_module"),
                "module_loaded":    bool(lv.get("rdma_module_loaded")),
                "module_available": bool(lv.get("rdma_module_available")),
                "kind":             lv.get("rdma_kind"),
                "port_state":       (_rdma_ports.get(name) or {}).get("state"),
                "rate_gbps":        (_rdma_ports.get(name) or {}).get("rate_gbps"),
            } if role == "rdma" else None),
            "model":       lv.get("model") or (_vf and _vf.get("model")),
            "nic_2110":    bool(lv.get("nic_2110") or (_vf and _vf.get("nic_2110"))),
            "port_medium": lv.get("port_medium"),
        })
    # Persistance opportuniste : le probe vient de résoudre modèle (lspci) + vitesse (ethtool) de
    # chaque carte. On les fige dans node_interfaces pour les interfaces DÉJÀ configurées → la page
    # Sources/Destinations 2110 lira le modèle exact + l'agrégat sans SSH (cf. _compute_receivers_detail).
    if ok_live:
        for name in cfg:
            lv = live.get(name) or {}
            _m = lv.get("model"); _sp = lv.get("speed_mbps"); _pci = lv.get("pci")
            _row = cfg.get(name) or {}
            _upd = {}
            if _m and _m != _row.get("model"):
                _upd["model"] = _m
            if _sp and _sp != _row.get("speed_mbps"):
                _upd["speed_mbps"] = int(_sp)
            # Fige le BDF tant que la carte est sur ice → on la retrouvera quand elle passera en
            # vfio-pci (plus de netdev), keyée sur l'ifname qu'elle avait (cf. vfio_by_bdf ci-dessus).
            if _pci and _pci != _row.get("pci"):
                _upd["pci"] = _pci
            if _upd:
                db_upsert_node_interface(node_id, name, **_upd)
    bmc_tile = bmc.status(node) if (node.get("ilo_host") or "").strip() else None
    # « Réseaux 2110 » : liste GLOBALE (dropdown) + groupes du nœud (un par réseau) avec statut
    # live par réseau (agrégat caché, keyé network_id) + réseau primaire du nœud. Alimente le
    # panneau « Réseaux 2110 ».
    from .. import settings as _st
    _net_st = {d.get("network_id"): d for d in (ptp_st.get("domains") or [])}
    # Surcharges PTP par réseau : exposer ptp_params parsé (dict) pour la modale d'édition.
    for _n in _all_nets:
        try:
            _n["ptp_params"] = json.loads(_n["ptp_params"]) if _n.get("ptp_params") else {}
        except Exception:
            _n["ptp_params"] = {}
    # Valeurs par défaut SMPTE 2059-2 d'un réseau (valeurs initiales du formulaire). Un réseau
    # définit son propre profil — il n'hérite PAS du nœud.
    ptp_defaults = dict(ptp.SMPTE_DEFAULTS)
    ptp_block = {
        "primary_network": _st.setting_for("ptp_primary_network", node_id),
        "node_domain":    int(_st.setting_for("ptp_domain", node_id) or 127),
        "enabled":        bool(_st.setting_for("ptp_enabled", node_id)),
        "networks":       _all_nets,
        "defaults":       ptp_defaults,
        "groups": [{"network_id": g["network_id"], "name": g["name"], "domain": g["domain"],
                    "ifaces": g["ifaces"], "primary": g["primary"],
                    "status": _net_st.get(g["network_id"])}
                   for g in ptp.groups_from_node_interfaces(node_id)],
    }
    return jsonify({
        "ok": True, "node_id": node_id, "name": node.get("name"), "host": host,
        "live_ok": ok_live, "live_error": live_err,
        # Rôles ASSIGNABLES depuis la liste : on retire 'bmc' — un BMC dédié n'est pas un netdev
        # de l'OS (donc absent de la liste) et est déjà représenté par la tuile Redfish. Le rôle
        # reste accepté côté POST (cas LOM partagé NC-SI via API) et rendu s'il existe déjà.
        "roles": [r for r in NODE_IFACE_ROLES if r != "bmc"],
        # Plage cluster (affichage de l'option « Plage du cluster » du bloc plage conteneurs).
        "cluster_ip_range": {"start": (_st.get("ip_start") or "").strip(),
                             "end": (_st.get("ip_end") or "").strip()},
        "interfaces": out,
        "ptp": ptp_block,
        "bmc": bmc_tile,
        "engine": _engine_state(node_id),
        "timestamp_ms": int(__import__("time").time() * 1000),
    })

@bp.route("/api/nodes/<int:node_id>/interfaces/<path:ifname>", methods=["POST"])
@require_perm("settings.edit")
def api_node_interface_set(node_id, ifname):
    """Upsert d'une interface (rôle, IP, gateway, VLAN, appariement red/blue, PTP). Resynchronise le
    pont de compat nodes.mtl_iface/media_ip depuis la ligne media2110/red. Applique l'IP média si fournie."""
    import re as _re
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    ifname = (ifname or "").strip()
    if not ifname:
        return jsonify({"error": "interface requise"}), 400
    d = request.json or {}
    # Rôle courant (AVANT modif) : sert à ne déclencher l'auto-provisionnement QUE pour un changement
    # touchant un port média 2110 (revue m2 — sinon chaque édition d'interface anodine, management/rdma,
    # spawn un thread ensure_node_engine → verify_image inutile + aggrave la fenêtre TOCTOU M1).
    from ..database import db_get_node_interfaces as _dgni
    _old_role = next((r.get("role") for r in _dgni(node_id) if r.get("ifname") == ifname), None)

    def _reprovision_if_media(new_role):
        if new_role == "media2110" or _old_role == "media2110":
            import threading as _t
            from .. import docker_driver as _dd
            _t.Thread(target=_dd.ensure_node_engine, args=(node_id,), daemon=True).start()

    if d.get("delete"):
        db_delete_node_interface(node_id, ifname)
        # Même raison qu'au changement de rôle : ne pas laisser derrière soi une stanza qui
        # ferait échouer `ifup -a` si la carte est un jour bindée à vfio-pci.
        if _old_role == "rdma":
            try:
                from .. import docker_driver as _dd
                _dd.oublier_iface_persistante(node, ifname)
            except Exception:
                pass
        # Le port retiré peut être le DERNIER média 2110 → reconcile (arrête le moteur s'il ne reste
        # plus aucun port média). Idempotent, en thread. Uniquement si le port supprimé ÉTAIT média.
        _reprovision_if_media(None)
        return jsonify({"ok": True, "deleted": ifname})

    role = (d.get("role") or "").strip() or None
    if role and role not in NODE_IFACE_ROLES:
        return jsonify({"error": "rôle invalide : %s" % role}), 400
    # ★ Activer le rôle rdma = PRÉPARER la carte, puis vérifier. On charge le module IB qui va avec
    # son driver (mlx5_ib, bnxt_re, irdma…) et on le grave pour le prochain boot ; s'il n'en sort
    # AUCUN device verbs, la carte ne fera jamais de RoCE et on refuse (409) avec la raison plutôt
    # que d'enregistrer un rôle inerte — le point vert de la ligne se lisait « tout va bien ».
    # `force` = passer outre en connaissance de cause (l'UI le propose après avoir montré la raison).
    rdma_msg = ""
    if role == "rdma" and _old_role != "rdma":
        try:
            from services import rdma as _rdma
            _rok, _rcode, _rdetail = _rdma.ensure_rdma_stack(node, ifname)
        except Exception as e:
            _rok, _rcode, _rdetail = False, "unreachable", str(e)
        if _rok:
            rdma_msg = " — " + _rdetail
        elif _rcode == "unreachable":
            rdma_msg = " — ⚠ pile RDMA non vérifiée (%s)" % _rdetail
        elif not d.get("force"):
            return jsonify({"ok": False, "code": "no_rdma_device", "reason": _rcode,
                            "error": "%s ne peut pas faire de RDMA : %s" % (ifname, _rdetail)}), 409
        else:
            rdma_msg = " — ⚠ rôle rdma forcé : %s" % _rdetail
    ip_cidr = (d.get("ip_cidr") or "").strip()
    if ip_cidr and not _re.match(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$", ip_cidr):
        return jsonify({"error": "IP invalide — attendu en CIDR (ex. 198.51.100.60/24)"}), 400
    pair_role = (d.get("pair_role") or "").strip().lower() or None
    if pair_role and pair_role not in ("red", "blue"):
        return jsonify({"error": "pair_role doit être 'red' ou 'blue'"}), 400
    pg = d.get("pair_group")
    pair_group = int(pg) if (pg is not None and str(pg) != "") else None
    # ★ TOUT OU RIEN. Le groupe n'existe QUE pour apparier un leg red à un leg blue : l'un sans
    # l'autre ne veut rien dire, et ce demi-état était librement enregistrable. Vécu sur dl360-1 :
    # deux ports en `pair_group=1` sans aucun rôle — inerte pour `media_port_pairs` (qui exige les
    # deux), mais pas partout : `mtl._vfio_precheck` teste `pair_group` SEUL et annonçait une
    # « paire 2022-7 incohérente » à propos d'une paire inexistante. Et surtout c'est un piège armé :
    # compléter le rôle manquant transforme d'un coup deux capacités en une seule (une paire porte le
    # MÊME flux sur ses deux legs), ce qui divise par deux la capacité déclarable du nœud.
    if (pair_group is None) != (pair_role is None):
        return jsonify({"error":
            "Redondance 2022-7 : le groupe et le leg vont ENSEMBLE. "
            + ("Un groupe est renseigné sans leg (red/blue) : un groupe seul n'apparie rien."
               if pair_role is None else
               "Un leg est renseigné sans groupe : un leg seul n'a pas de partenaire.")
            + " Renseigner les deux, ou vider les deux (pas de 2022-7)."}), 400
    # Réseau 2110 de la NIC (id de media_networks). Le domaine PTP en est DÉRIVÉ (pont de compat
    # ptp_domain). Vide → la NIC n'est rattachée à aucun réseau.
    from ..database import db_get_media_networks
    mn = d.get("media_network_id")
    media_network_id = None
    network_domain = None
    if mn is not None and str(mn) != "":
        try:
            media_network_id = int(mn)
        except (TypeError, ValueError):
            return jsonify({"error": "réseau invalide"}), 400
        _net = next((n for n in db_get_media_networks() if n["id"] == media_network_id), None)
        if not _net:
            return jsonify({"error": "réseau 2110 introuvable"}), 404
        network_domain = int(_net["domain"])

    gateway = (d.get("gateway") or "").strip() or None
    vlan    = (d.get("vlan") or "").strip() or None
    # Réserve de files 2110 par interface (capacité « à chaud » choisie par l'opérateur). Présent vide
    # → NULL (auto). Entier ≥ 0. Bornée au budget de files du port — la file 0 est réservée au
    # kernel/PTP (steering RSS) → max = xdp_hw - 1.
    def _opt_q(key):
        if key not in d:
            return (False, None)              # absent → ne pas toucher
        s = str(d.get(key) if d.get(key) is not None else "").strip()
        if s == "":
            return (True, None)               # présent vide → vidage (NULL = auto)
        if not s.isdigit():
            raise ValueError(key)
        return (True, int(s))
    try:
        rx_present, rx_reserve   = _opt_q("rx_reserve")
        tx_present, tx_reserve   = _opt_q("tx_reserve")
        mg_present, queue_margin = _opt_q("queue_margin")
    except ValueError:
        return jsonify({"error": "réserve de files invalide (entier ≥ 0 attendu)"}), 400
    # Profil d'émetteur ST 2110-21 (chantier narrow) : classe de sender par interface média.
    # ''/absent → auto (NULL). Hors média 2110 → forcé NULL (le pacing ne concerne que la TX 2110).
    output_profile = (str(d.get("output_profile") or "")).strip().lower() or None
    if output_profile and output_profile not in ("narrow", "narrow_linear", "wide"):
        return jsonify({"error": "profil d'émission invalide (narrow | narrow_linear | wide)"}), 400
    if role != "media2110":
        output_profile = None
    # Mode PMD du port média : 'af_xdp' (défaut : pacing logiciel TSC — aucun rate limiter, donc
    # aucune action TX n'est perturbatrice) ou 'dpdk' (PF pleine DPDK = socle narrow, rate limiter
    # matériel). C'est CE champ qui rend `output_profile` effectif : sans lui, le sélecteur de profil
    # d'émission était un contrôle MUET (déclaré, sans effet, sans l'expliquer — cf. docs/reference/TX_LAYOUTS.md
    # étage 2). Pris en compte au prochain (re)déploiement du moteur 2110_io. 'sriov' reste posable
    # en base (chantier historique) mais n'est pas proposé à l'UI.
    pmd = (str(d.get("pmd") or "")).strip().lower() or None
    if pmd and pmd not in ("af_xdp", "dpdk", "sriov"):
        return jsonify({"error": "mode PMD invalide (af_xdp | dpdk)"}), 400
    if role != "media2110":
        pmd = None
    # ★ Cohérence MODE ↔ HORLOGE, refusée ICI (à l'enregistrement) et plus seulement au déploiement
    # (mtl.py:_vfio_gardefous). `ptp_enabled` = « ptp4l tourne sur CETTE interface » — en DPDK la PF
    # passe en vfio-pci : plus de netdev noyau, donc plus de PHC → ptp4l ne PEUT pas y tourner. Le PTP
    # ne disparaît pas pour autant : en DPDK l'horloge est portée par le moteur 2110 (libmtl, client
    # PTP interne — c'est aussi le GM des ts-refclk SDP). Sans ce garde-fou, la combinaison était
    # acceptée puis échouait bien plus tard, au (re)déploiement du moteur.
    # État EFFECTIF après enregistrement : champ du payload s'il est présent, sinon valeur en base
    # (un POST partiel ne doit pas pouvoir contourner le garde-fou).
    _row = next((r for r in _dgni(node_id) if r.get("ifname") == ifname), {}) or {}
    _pmd_eff = pmd if "pmd" in d else (_row.get("pmd") or None)
    _ptp_eff = bool(d["ptp_enabled"]) if "ptp_enabled" in d else bool(_row.get("ptp_enabled"))
    if (role or _row.get("role")) == "media2110" and _pmd_eff == "dpdk" and _ptp_eff:
        return jsonify({"error": "combinaison impossible : mode DPDK + ptp4l sur la même carte. En "
                                 "DPDK la carte passe en vfio-pci (plus de netdev noyau, plus de PHC) "
                                 "— ptp4l ne peut pas y tourner. L'horloge PTP est alors portée par le "
                                 "moteur 2110 (libmtl). Retirer « ptp4l sur cette carte », ou rester "
                                 "en AF-XDP."}), 400
    # Alias opérateur libre (« PGM-Rouge ») — sur toute interface, affiché à côté du nom.
    alias = (str(d.get("alias") or "")).strip() or None
    # Plage IP conteneurs PAR NŒUD : uniquement sur une carte containers/mgmt_containers. Les deux
    # bornes vont ensemble ; validées dans le subnet de la carte (si ip_cidr posé), début ≤ fin, et
    # sans chevaucher l'IP de contrôle de la carte, sa passerelle, ni l'IP `host` du nœud.
    from ..database import role_is_containers as _role_is_ct
    ct_start = (str(d.get("ct_ip_start") or "")).strip() or None
    ct_end   = (str(d.get("ct_ip_end") or "")).strip() or None
    if not _role_is_ct(role):
        ct_start = ct_end = None
    if bool(ct_start) != bool(ct_end):
        return jsonify({"error": "plage conteneurs incomplète — IP de début ET de fin requises"}), 400
    if ct_start:
        import ipaddress as _ipa
        try:
            _a = _ipa.IPv4Address(ct_start); _b = _ipa.IPv4Address(ct_end)
        except Exception:
            return jsonify({"error": "plage conteneurs invalide — deux IPv4 attendues"}), 400
        if int(_a) > int(_b):
            return jsonify({"error": "plage conteneurs invalide — l'IP de début doit être ≤ l'IP de fin"}), 400
        _blockers = []                      # IP à ne pas englober : contrôle carte, passerelle, host nœud
        if ip_cidr:
            try:
                _net = _ipa.ip_network(ip_cidr, strict=False)
            except Exception:
                _net = None
            if _net and (_a not in _net or _b not in _net):
                return jsonify({"error": "plage conteneurs hors du subnet de la carte (%s)" % _net}), 400
            _blockers.append(("IP de la carte", ip_cidr.split("/")[0]))
        if gateway:
            _blockers.append(("passerelle", gateway))
        if (node.get("host") or "").strip():
            _blockers.append(("IP de contrôle du nœud", node["host"].strip()))
        for _lbl, _ips in _blockers:
            try:
                if int(_a) <= int(_ipa.IPv4Address(_ips)) <= int(_b):
                    return jsonify({"error": "plage conteneurs chevauche %s (%s)" % (_lbl, _ips)}), 400
            except Exception:
                pass
    _xdp_hw = _mtl_total_queues()
    _sum_q = (rx_reserve or 0) + (tx_reserve or 0) + (queue_margin or 0)
    if _sum_q > max(1, _xdp_hw - 1):
        return jsonify({"error": "réserve totale RX+TX+marge (%d) dépasse le budget de files du port "
                                 "(%d, file 0 réservée PTP/kernel)" % (_sum_q, _xdp_hw - 1)}), 400
    # Vidage explicite : un champ PRÉSENT dans le payload mais vide → remis à NULL (sinon None =
    # « ne pas toucher » et on ne pourrait jamais retirer p.ex. l'appariement red/blue ou le réseau).
    clear = [k for k, v in (("ip_cidr", ip_cidr or None), ("gateway", gateway), ("vlan", vlan),
                            ("pair_role", pair_role), ("pair_group", pair_group),
                            ("media_network_id", media_network_id))
             if k in d and v is None]
    # Plage conteneurs : champ présent mais vide (ou rôle non containers) → retour plage cluster.
    for _ck, _cv in (("ct_ip_start", ct_start), ("ct_ip_end", ct_end)):
        if _ck in d and _cv is None:
            clear.append(_ck)
    for _qk, _qp, _qv in (("rx_reserve", rx_present, rx_reserve),
                          ("tx_reserve", tx_present, tx_reserve),
                          ("queue_margin", mg_present, queue_margin)):
        if _qp and _qv is None:
            clear.append(_qk)
    if "media_network_id" in d and media_network_id is None:
        clear.append("ptp_domain")        # plus de réseau → plus de domaine dérivé
    db_upsert_node_interface(
        node_id, ifname, clear=clear,
        role=role, ip_cidr=ip_cidr or None,
        gateway=gateway, vlan=vlan,
        pair_role=pair_role, pair_group=pair_group,
        ptp_enabled=(1 if d.get("ptp_enabled") else 0) if "ptp_enabled" in d else None,
        media_network_id=media_network_id, ptp_domain=network_domain,
        mac=(d.get("mac") or "").strip() or None,
        pci=(d.get("pci") or "").strip() or None,
        notes=(d.get("notes") or "").strip() or None,
        # Modèle (lspci) + vitesse de lien (ethtool) résolus par le probe du front → persistés pour
        # l'affichage NIC de la page 2110 (modèle exact + agrégat = somme des vitesses), sans re-SSH.
        model=(d.get("model") or "").strip() or None,
        speed_mbps=(int(d["speed_mbps"]) if str(d.get("speed_mbps") or "").isdigit() else None),
        rx_reserve=rx_reserve, tx_reserve=tx_reserve, queue_margin=queue_margin,
        ct_ip_start=ct_start, ct_ip_end=ct_end,
        pmd=pmd,                       # (dans NODE_IFACE_FIELDS → écrit par l'upsert)
    )

    # output_profile / alias : colonnes du schéma partagé (commit 7975640) HORS NODE_IFACE_FIELDS →
    # db_upsert_node_interface ne les écrit pas. On les pose ici en SQL direct (database.py hors du
    # périmètre de ce lot). ⚠ CONSOLIDATION recommandée : ajouter 'output_profile','alias' au tuple
    # NODE_IFACE_FIELDS et router par l'upsert (signalé dans la revue #24). La ligne existe déjà
    # (créée/mise à jour par l'upsert ci-dessus, role toujours fourni). N'écrit que les clés PRÉSENTES
    # dans le payload (absente → « ne pas toucher »).
    _sets, _args = [], []
    if "output_profile" in d:
        _sets.append("output_profile=?"); _args.append(output_profile)
    if "alias" in d:
        _sets.append("alias=?"); _args.append(alias)
    if _sets:
        with get_db() as _db:
            _db.execute("UPDATE node_interfaces SET %s WHERE node_id=? AND ifname=?" % ", ".join(_sets),
                        _args + [node_id, ifname])
            _db.commit()

    # Pont de compatibilité : la ligne media2110/red reste la source de mtl_iface/media_ip que lit le
    # chemin de déploiement 2110_io (docker_driver.ensure_media_ip). On resynchronise après upsert.
    media_msg = ""
    if role == "media2110" and (pair_role in (None, "red")):
        db_update_node(node_id, mtl_iface=ifname)
        if ip_cidr:
            db_update_node(node_id, media_ip=ip_cidr)
            # Applique l'IP sur la carte (idempotent, via l'agent) si l'enrôlement est finalisé.
            if (node.get("agent_url") or "").strip():
                try:
                    from .. import docker_driver as _dd
                    _nf = dict(node); _nf["mtl_iface"] = ifname; _nf["media_ip"] = ip_cidr
                    _mok, _mmsg = _dd.ensure_media_ip(_nf)
                    media_msg = " — IP média appliquée" if _mok else (" — IP média ÉCHEC (%s)" % _mmsg)
                except Exception as e:
                    media_msg = " — IP média ÉCHEC (%s)" % e
    elif role == "media2110" and ip_cidr and (node.get("agent_url") or "").strip():
        # NIC média SECONDAIRE (multi-NIC) : pose son IPv4 immédiatement (le moteur multi-port la
        # déclare comme port distinct ; sans IP+UP, sip=0.0.0.0 → TX/RX KO sur cette NIC).
        try:
            from .. import docker_driver as _dd
            _iok, _imsg = _dd.ensure_iface_ip(node, ifname, ip_cidr)
            media_msg = " — IP média appliquée" if _iok else (" — IP média ÉCHEC (%s)" % _imsg)
        except Exception as e:
            media_msg = " — IP média ÉCHEC (%s)" % e

    # Rôle RDMA : pose l'IP + monte l'interface sur l'hôte (mlx5/RoCE). Sans ça la NIC reste admin
    # DOWN sans IP → port IB DOWN, aucun endpoint fabric joignable (cf. chantier RDMA). Idempotent.
    elif role == "rdma" and ip_cidr and (node.get("agent_url") or "").strip():
        try:
            from .. import docker_driver as _dd
            # persist=True : sans gravure, l'adresse et l'état UP disparaissent au reboot du nœud
            # et le lien RDMA paraît débranché (une carte down ne détecte aucune porteuse).
            _rok, _rmsg = _dd.ensure_iface_ip(node, ifname, ip_cidr, persist=True)
            media_msg = " — IP RDMA appliquée + interface montée" if _rok else (" — IP RDMA ÉCHEC (%s)" % _rmsg)
        except Exception as e:
            media_msg = " — IP RDMA ÉCHEC (%s)" % e
        # lldpd : détection du voisin (direct vs switch) dans la Vue d'ensemble. Best-effort.
        try:
            from services import rdma as _rdma
            _rdma.ensure_lldpd(node)
        except Exception:
            pass

    # Auto-provisionnement du moteur 2110_io UNIQUE du nœud : dès qu'un port média 2110 (avec IP) est
    # configuré → garantir l'existence du moteur ; si un rôle média est retiré et qu'il ne reste plus
    # aucun port média → arrêter le moteur. Idempotent (no-op si déjà cohérent), en thread (creer/deploy
    # lourds) — l'IP média vient d'être posée juste au-dessus. Uniquement si le changement touche un
    # port média (nouveau ou ancien rôle media2110, revue m2).
    # L'interface QUITTE le rôle rdma : retirer sa configuration gravée. Une stanza orpheline
    # n'est pas inerte — si la carte est plus tard bindée à vfio-pci pour DPDK, elle n'a plus de
    # netdev kernel et `ifup -a` échoue au démarrage sur une interface introuvable.
    if _old_role == "rdma" and role != "rdma":
        try:
            from .. import docker_driver as _dd
            _dd.oublier_iface_persistante(node, ifname)
        except Exception:
            pass

    _reprovision_if_media(role)

    return jsonify({"ok": True, "ifname": ifname, "role": role,
                    "msg": "interface enregistrée" + media_msg + rdma_msg})

@bp.route("/api/nodes/<int:node_id>/ptp-primary-network", methods=["POST"])
@require_perm("settings.edit")
def api_node_ptp_primary_network(node_id):
    """Définit le réseau 2110 PRIMAIRE d'un nœud (celui qui discipline l'horloge système en
    multi-NIC). Vide → repli automatique (réseau du domaine ptp_domain du nœud, sinon plus petit id)."""
    if not db_get_node(node_id):
        return jsonify({"error": "nœud introuvable"}), 404
    from ..database import db_set_node_setting
    v = (request.json or {}).get("network_id")
    if v is None or str(v).strip() == "":
        db_set_node_setting(node_id, "ptp_primary_network", "")   # "" → repli auto côté pilote
        return jsonify({"ok": True, "primary_network": None})
    try:
        net_id = int(v)
    except (TypeError, ValueError):
        return jsonify({"error": "réseau invalide"}), 400
    db_set_node_setting(node_id, "ptp_primary_network", net_id)
    return jsonify({"ok": True, "primary_network": net_id})


# ─── Réseau Docker macvlan (création sur l'hôte + report dans les nœuds) ───────
# Réutilise le pool d'IP des réglages (ip_start/ip_end/gateway) pour dériver subnet/gateway/range.
# Seul l'`parent` (interface L2 de l'hôte) n'est pas dérivable → saisi par l'utilisateur.
def _parent_declare(node_id):
    """Alias de `node_driver.parent_declare` — l'interface qui, D'APRÈS LES RÔLES DÉCLARÉS, porte
    les conteneurs de ce nœud (`containers` ou `mgmt_containers`). "" si aucune.

    C'est la source de vérité : elle est en base, éditable, et par nœud. Le `parent` du réseau
    macvlan en venait NULLE PART — il était saisi une fois puis appliqué tel quel. Sur un parc où
    les machines ne sont pas câblées à l'identique, ça donne un réseau accroché à une carte morte,
    sans que rien ne le signale : constaté sur r620-3 le 2026-08-02, dont la carte conteneurs est
    `eno1` (elle porte l'IP du nœud et sa route par défaut) alors que son macvlan pointait `eno4`,
    sans porteuse. Les conteneurs y démarraient, prenaient leur IP, et n'étaient JAMAIS joignables —
    avec un statut Docker « running » parfaitement vert.
    """
    from .. import node_driver as _nd
    return _nd.parent_declare(node_id)


def _macvlan_suggest(node_id=None):
    """Suggestion subnet/gateway/plage du réseau macvlan. PAR NŒUD : si la carte containers/
    mgmt_containers du nœud porte une plage personnalisée (ct_ip_start/ct_ip_end), subnet et
    passerelle sont dérivés de SON ip_cidr/gateway et la plage = sa plage perso. Sinon (ou
    node_id=None) : valeurs CLUSTER (comportement historique)."""
    from .. import settings as _st, allocations
    import ipaddress as _ip
    it = allocations._node_ct_iface(node_id) if node_id else None
    if it:
        start = it["ct_ip_start"].strip(); end = it["ct_ip_end"].strip()
        gw = (it.get("gateway") or "").strip()   # vide → dérivée de la route par défaut du nœud (POST)
        subnet = ""; iprange = ""
        try:
            a = _ip.IPv4Address(start); b = _ip.IPv4Address(end)
            if int(a) > int(b):
                a, b = b, a
            # Subnet : celui de la carte (ip_cidr) si posé — sinon /24 autour de la plage.
            ref = (it.get("ip_cidr") or "").strip() or (str(a) + "/24")
            subnet = str(_ip.ip_network(ref, strict=False))
            cidrs = list(_ip.summarize_address_range(a, b))
            if cidrs:
                iprange = str(max(cidrs, key=lambda n: n.num_addresses))
        except Exception:
            pass
        return {"name": "bobimacvlan", "subnet": subnet, "gateway": gw, "ip_range": iprange,
                "scope": "node", "ifname": it.get("ifname"), "ip_start": start, "ip_end": end}
    start = (_st.get("ip_start") or "").strip()
    end   = (_st.get("ip_end") or "").strip()
    gw    = (_st.get("gateway") or "").strip()
    subnet = ""; iprange = ""
    try:
        a = _ip.IPv4Address(start); b = _ip.IPv4Address(end)
        if int(a) > int(b):
            a, b = b, a
        ref = gw or str(a)
        subnet = str(_ip.ip_network(ref + "/24", strict=False))
        cidrs = list(_ip.summarize_address_range(a, b))
        if cidrs:
            iprange = str(max(cidrs, key=lambda n: n.num_addresses))   # plus grand bloc du pool
    except Exception:
        pass
    return {"name": "bobimacvlan", "subnet": subnet, "gateway": gw, "ip_range": iprange,
            "scope": "cluster", "ip_start": start, "ip_end": end}

def _node_default_gateway(host):
    """Passerelle par défaut RÉELLE du nœud (`ip route show default` → via X) — le vrai routeur du
    segment, à utiliser comme passerelle macvlan (PAS l'IP de l'hôte). None si introuvable."""
    if not host:
        return None
    from ..host_ops import ssh_run
    rc, out, _ = ssh_run(host, "ip route show default 2>/dev/null", timeout=10)
    toks = (out or "").split()
    return toks[toks.index("via") + 1] if "via" in toks else None

def _node_host_ipv4s(host):
    """IPv4 portées par le nœud LUI-MÊME (toutes interfaces) — pour détecter une passerelle macvlan
    invalide (== IP de l'hôte parent : un conteneur macvlan ne joint jamais son parent)."""
    if not host:
        return set()
    from ..host_ops import ssh_run
    rc, out, _ = ssh_run(host, "ip -o -4 addr show 2>/dev/null", timeout=10)
    ips = set()
    for line in (out or "").splitlines():
        parts = line.split()
        try:
            ips.add(parts[parts.index("inet") + 1].split("/")[0])
        except Exception:
            pass
    return ips

def _host_interfaces(host, gateway):
    """Interfaces L2 de l'hôte (via ssh) + suggestion du parent macvlan (= interface qui ROUTE
    vers la passerelle). Retourne (interfaces:[{name,addrs}], suggested:str)."""
    from ..host_ops import ssh_run
    import shlex as _sh
    seen = {}
    rc, out, _ = ssh_run(host, "ip -o link show 2>/dev/null", timeout=15)
    for line in (out or "").splitlines():
        # "2: eth0: <...>" ou "5: eth0.10@eth0: <...>"
        try:
            name = line.split(":")[1].strip().split("@")[0]
        except Exception:
            continue
        if name and name != "lo":
            seen.setdefault(name, [])
    rc, out, _ = ssh_run(host, "ip -o -4 addr show 2>/dev/null", timeout=15)
    for line in (out or "").splitlines():
        parts = line.split()
        try:
            name = parts[1]
            cidr = parts[parts.index("inet") + 1]
        except Exception:
            continue
        seen.setdefault(name, [])
        if cidr not in seen[name]:
            seen[name].append(cidr)
    interfaces = [{"name": n, "addrs": a} for n, a in sorted(seen.items())]
    suggested = ""
    if gateway:
        rc, out, _ = ssh_run(host, "ip route get %s 2>/dev/null" % _sh.quote(gateway), timeout=10)
        toks = (out or "").split()
        if "dev" in toks:
            suggested = toks[toks.index("dev") + 1]
    return interfaces, suggested

@bp.route("/api/docker-network/suggest", methods=["GET"])
@require_perm("settings.edit")
def api_docker_network_suggest():
    host = _build_host()
    sug = _macvlan_suggest()
    exists = False
    interfaces = []; suggested = ""
    if host:
        import shlex as _sh
        if sug.get("name"):
            rc, _ = _ssh_bin(host, "docker network inspect %s >/dev/null 2>&1" % _sh.quote(sug["name"]), timeout=20)
            exists = rc == 0
        try:
            interfaces, suggested = _host_interfaces(host, sug.get("gateway"))
        except Exception:
            pass
    return jsonify({"host": host, "suggest": sug, "exists": exists,
                    "interfaces": interfaces, "suggested_parent": suggested})

def _ensure_vlan_parent(host, parent):
    """Si `parent` est une sous-interface VLAN `<base>.<vid>` absente de l'hôte, la crée (runtime)
    et la PERSISTE dans /etc/network/interfaces (sauvegarde + strophe ifupdown2). Idempotent.
    Indispensable pour macvlan sur un VLAN taggé (cf. piège VLAN natif). Retourne (ok, msg)."""
    from ..host_ops import ssh_run
    import shlex as _sh
    if "." not in parent:
        return True, "interface physique/bridge (pas de VLAN à créer)"
    base, vid = parent.rsplit(".", 1)
    if not vid.isdigit():
        return True, "parent non-VLAN"
    rc, _, _ = ssh_run(host, "ip link show %s >/dev/null 2>&1" % _sh.quote(parent), timeout=10)
    if rc != 0:
        create = ("ip link add link %s name %s type vlan id %s && ip link set %s up && "
                  "bridge vlan add vid %s dev %s self 2>/dev/null || true"
                  % (_sh.quote(base), _sh.quote(parent), vid, _sh.quote(parent), vid, _sh.quote(base)))
        rc, out, err = ssh_run(host, create, timeout=20)
        if rc != 0:
            return False, "création %s échouée : %s" % (parent, (err or out)[:200])
    # Persistance idempotente (ifupdown2). Tab d'indentation comme Proxmox.
    persist = (
        "grep -q 'iface %s ' /etc/network/interfaces || { "
        "cp -a /etc/network/interfaces /etc/network/interfaces.bak-bobistudio-$(date +%%s); "
        "printf '\\nauto %s\\niface %s inet manual\\n\\tpost-up bridge vlan add vid %s dev %s self "
        "2>/dev/null || true\\n' >> /etc/network/interfaces; }"
        % (parent, parent, parent, vid, base))
    ssh_run(host, persist, timeout=15)
    return True, "%s prête + persistée" % parent

@bp.route("/api/docker-network/create", methods=["POST"])
@require_perm("settings.edit")
def api_docker_network_create():
    import shlex as _sh
    d = request.json or {}
    host = _build_host()
    if not host:
        return jsonify({"error": "hôte non configuré (Réglages → Proxmox)"}), 400
    name   = (d.get("name") or "bobimacvlan").strip()
    parent = (d.get("parent") or "").strip()
    subnet = (d.get("subnet") or "").strip()
    gateway = (d.get("gateway") or "").strip()
    iprange = (d.get("ip_range") or "").strip()
    if not (name and parent and subnet):
        return jsonify({"error": "nom, interface parent et subnet requis"}), 400
    # Crée/persiste l'interface VLAN parent si nécessaire (macvlan sur VLAN taggé).
    ok, vmsg = _ensure_vlan_parent(host, parent)
    if not ok:
        return jsonify({"error": vmsg}), 500
    opts = "--subnet %s " % _sh.quote(subnet)
    if gateway:
        opts += "--gateway %s " % _sh.quote(gateway)
    if iprange:
        opts += "--ip-range %s " % _sh.quote(iprange)
    opts += "-o parent=%s " % _sh.quote(parent)
    # Idempotent : ne (re)crée pas s'il existe déjà.
    remote = ("docker network inspect %s >/dev/null 2>&1 || "
              "docker network create -d macvlan %s%s") % (_sh.quote(name), opts, _sh.quote(name))
    rc, out = _ssh_bin(host, remote, timeout=60)
    if rc != 0:
        return jsonify({"error": out.decode("utf-8", "replace")[-500:]}), 500
    # Report dans les nœuds dont le réseau Docker est vide.
    filled = 0
    for n in db_get_nodes():
        if not (n.get("docker_network") or "").strip():
            db_update_node(n["id"], docker_network=name)
            filled += 1
    # `filled` (compte de nœuds) est une DONNÉE, pas une phrase : toujours en paramètre — la clé
    # l'affiche systématiquement (léger reformulage : « 0 nœud(s) » remplace la clause absente).
    db_add_alert("alert.net.macvlan_manuel_pret", "info", kind="net",
                 params={"name": name, "host": host, "parent": parent, "vmsg": vmsg,
                         "filled": filled})
    return jsonify({"ok": True, "name": name, "filled": filled, "parent_msg": vmsg})


def _parse_ethtool_module(text):
    """Parse la sortie `ethtool -m <if>` (EEPROM SFF-8472/8636 + DOM) en un résumé : type
    (DAC/AOC/optique), vendor/PN/SN, longueur, longueur d'onde, et niveaux optiques Tx/Rx (dBm),
    température, Vcc, bias si le module supporte le DOM. `fields` = paires brutes pour le détail."""
    fields = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            if k:
                fields[k] = v.strip()
    g = lambda *keys: next((fields[k] for k in keys if fields.get(k)), None)
    conn  = g("Connector") or ""
    ttype = g("Transceiver type") or ""
    ctech = g("Cable technology") or ""
    wavelength = g("Laser wavelength", "Wavelength")
    blob = (conn + " " + ttype + " " + ctech).lower()
    copper = ("copper" in blob) or ("cable" in conn.lower() and not wavelength and not g("Laser output power"))
    active = "active" in blob
    if copper:
        medium = "DAC actif (cuivre)" if active else "DAC passif (cuivre)"
    elif "active" in blob and (wavelength or g("Laser output power")):
        medium = "AOC (optique actif)"
    elif wavelength or g("Laser output power"):
        medium = "Transceiver optique"
    else:
        medium = None
    def _dbm(s):
        if not s: return None
        m = re.search(r"(-?\d+\.?\d*)\s*dBm", s)
        return round(float(m.group(1)), 2) if m else None
    def _num(s):
        if not s: return None
        m = re.search(r"(-?\d+\.?\d*)", s)
        return float(m.group(1)) if m else None
    dom = {
        "temp_c":  _num(g("Module temperature")),
        "vcc_v":   _num(g("Module voltage")),
        "tx_dbm":  _dbm(g("Laser output power", "Transmit avg optical power", "Tx output power")),
        "rx_dbm":  _dbm(g("Receiver signal average optical power", "Rcvr signal avg optical power",
                          "Rx input power")),
        "bias_ma": _num(g("Laser bias current")),
    }
    has_dom = any(v is not None for v in dom.values())
    length = g("Length (Copper or Active cable)", "Length (SMF,km)", "Length (SMF)",
               "Length (OM3)", "Length (OM4)", "Length (50um)", "Length (62.5um)", "Length (OM1)")
    return {
        "present":     True,
        "identifier":  g("Identifier"),
        "connector":   conn or None,
        "transceiver": ttype or None,
        "medium":      medium,
        "vendor":      g("Vendor name"),
        "pn":          g("Vendor PN"),
        "sn":          g("Vendor SN"),
        "rev":         g("Vendor rev"),
        "wavelength":  wavelength,
        "length":      length,
        "dom":         dom if has_dom else None,
        "fields":      fields,
    }

@bp.route("/api/nodes/<int:node_id>/interfaces/<path:ifname>/module", methods=["GET"])
@require_perm("settings.edit")
def api_node_interface_module(node_id, ifname):
    """Infos du module SFP/QSFP inséré (ethtool -m) : type DAC/AOC/optique, vendor/PN/SN +
    niveaux optiques DOM (Tx/Rx dBm, température, Vcc, bias) si supportés. À LA DEMANDE (lecture
    I2C lente — jamais dans le poll 2 s). Best-effort : module absent / non lisible → present:False."""
    import shlex as _sh
    from ..host_ops import ssh_run
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    host = _node_host(node_id) or node.get("host")
    if not host:
        return jsonify({"error": "hôte indéterminé"}), 400
    ifn = (ifname or "").strip()
    if not ifn:
        return jsonify({"error": "interface requise"}), 400
    rc, out, err = ssh_run(host, "ethtool -m %s 2>&1" % _sh.quote(ifn), timeout=12)
    low = ((out or "") + " " + (err or "")).lower()
    # Port en DPDK (vfio-pci) : plus de netdev kernel → `ethtool -m` échoue « no such device » (erreur
    # netlink). On le DIT proprement au lieu de laisser fuir l'erreur brute : le module optique n'est
    # pas interrogeable via ethtool dans ce mode (la carte est pilotée par le moteur en kernel-bypass).
    if "no such device" in low or "no device matches" in low:
        from ..database import db_get_node_interfaces
        _dpdk = any((r.get("ifname") == ifn and (r.get("pmd") or "").strip().lower() == "dpdk")
                    for r in (db_get_node_interfaces(node_id) or []))
        if _dpdk:
            return jsonify({"ok": True, "present": False, "dpdk": True,
                            "reason": "Module non lisible en mode DPDK : la carte est en vfio-pci (pas "
                                      "de netdev kernel). L'EEPROM du module optique n'est pas "
                                      "interrogeable via ethtool dans ce mode."})
    if rc != 0 or "cannot get module" in low or "operation not supported" in low \
       or "no such device" in low or "invalid argument" in low:
        return jsonify({"ok": True, "present": False,
                        "reason": (out or err).strip()[:200] or "module non lisible / absent"})
    mod = _parse_ethtool_module(out)
    mod["ok"] = True
    return jsonify(mod)


# ─── Préflight nœud : prérequis hôte pour le déploiement Docker ────────────────
_SHM_MIN_GIB = 1.0   # seuil d'alerte pour /dev/shm (rings vidéo MXL)

def _node_preflight(host):
    """Checks exécutés sur l'HÔTE via ssh : moteur Docker, taille /dev/shm, accès Internet.
    Retourne une liste de {key, label, status(ok|warn|fail), msg}."""
    from ..host_ops import ssh_run
    checks = []
    if not host:
        return [{"key": "host", "label": "Hôte", "status": "fail",
                 "msg": "hôte non configuré (Réglages → Proxmox)"}]

    # 1) Moteur Docker (présent + daemon up).
    rc, out, _ = ssh_run(host, "docker version --format '{{.Server.Version}}' 2>/dev/null", timeout=20)
    ver = (out or "").strip()
    if rc == 0 and ver:
        checks.append({"key": "docker", "label": "Moteur Docker", "status": "ok",
                       "msg": "daemon up · v%s" % ver})
    else:
        rc2, _, _ = ssh_run(host, "command -v docker >/dev/null 2>&1", timeout=15)
        msg = "binaire absent" if rc2 != 0 else "installé mais daemon arrêté/injoignable"
        checks.append({"key": "docker", "label": "Moteur Docker", "status": "fail", "msg": msg})

    # 2) /dev/shm (pipeline MXL) : place dispo.
    rc, out, _ = ssh_run(host, "df -PB1 /dev/shm 2>/dev/null | awk 'NR==2{print $2, $4}'", timeout=15)
    try:
        total, avail = (int(x) for x in (out or "").split()[:2])
        gib = avail / (1024.0 ** 3)
        status = "ok" if gib >= _SHM_MIN_GIB else "warn"
        checks.append({"key": "shm", "label": "/dev/shm (pipeline MXL)", "status": status,
                       "msg": "%.1f Gio dispo / %.1f Gio (seuil %.0f Gio)"
                              % (gib, total / (1024.0 ** 3), _SHM_MIN_GIB)})
    except Exception:
        checks.append({"key": "shm", "label": "/dev/shm (pipeline MXL)", "status": "warn",
                       "msg": "taille indéterminée"})

    # 3) Accès Internet sortant (apt + clone libmtl au build d'image).
    net_cmd = ("bash -c 'getent hosts deb.debian.org >/dev/null 2>&1 && "
               "timeout 5 bash -c \"exec 3<>/dev/tcp/deb.debian.org/443\" && echo NETOK'")
    rc, out, _ = ssh_run(host, net_cmd, timeout=15)
    if "NETOK" in (out or ""):
        checks.append({"key": "internet", "label": "Accès Internet (hôte)", "status": "ok",
                       "msg": "DNS + HTTPS sortant OK"})
    else:
        checks.append({"key": "internet", "label": "Accès Internet (hôte)", "status": "warn",
                       "msg": "DNS/HTTPS sortant non confirmé (requis pour builder les images)"})

    # 4) Cohérence hôte de build/réseau (proxmox_host) vs hôtes des nœuds (côté orchestrateur).
    node_hosts = sorted({(n.get("host") or "").strip() for n in db_get_nodes() if n.get("host")})
    if node_hosts and host not in node_hosts:
        checks.append({"key": "host_match", "label": "Hôte build vs nœuds", "status": "warn",
                       "msg": "build/réseau sur %s mais nœud(s) sur %s → en multi-nœud, builder par nœud"
                              % (host, ", ".join(node_hosts))})
    else:
        checks.append({"key": "host_match", "label": "Hôte build vs nœuds", "status": "ok",
                       "msg": "build et déploiement sur le même hôte (%s)" % host})
    return checks

@bp.route("/api/node-preflight", methods=["GET"])
@require_perm("settings.edit")
def api_node_preflight():
    host = _build_host()
    return jsonify({"host": host, "checks": _node_preflight(host)})

@bp.route("/api/docker/install", methods=["POST"])
@require_perm("settings.edit")
def api_docker_install():
    from ..host_ops import ssh_run
    host = _build_host()
    if not host:
        return jsonify({"ok": False, "msg": "hôte non configuré"}), 400
    # Debian 13 (trixie) a scindé `docker.io` : le client `docker` est dans `docker-cli` (Recommends)
    # et le builder `buildx` dans `docker-buildx` (non tiré non plus). Avec --no-install-recommends il
    # faut les nommer explicitement, sinon le daemon tourne mais `docker` manque (preflight boucle) et
    # `docker build` échoue (« buildx component is missing »).
    cmd = ("export DEBIAN_FRONTEND=noninteractive; "
           "apt-get update && apt-get install -y --no-install-recommends docker.io docker-cli docker-buildx && "
           "systemctl enable --now docker")
    rc, out, err = ssh_run(host, cmd, timeout=600)
    ok = (rc == 0)
    db_add_alert("alert.node.docker_install_ok" if ok else "alert.node.docker_install_echec",
                 "info" if ok else "error", kind="prep", params={"host": host})
    return jsonify({"ok": ok, "msg": (out or err or "")[-500:]})

@bp.route("/api/nodes/<int:node_id>/readiness", methods=["GET"])
@require_perm("settings.edit")
def api_node_readiness(node_id):
    """Agrégat de readiness d'un nœud pour la carte Déploiement → Nœuds : réseau containers + io2110
    (E810/PTP) + preflight + images. Chaque sous-sonde est best-effort (try/except) : une lenteur agent
    ne doit pas 500 la carte. Valeurs CLUSTER (subnet/passerelle/plage/VLAN/domaine PTP) en lecture seule."""
    from .. import node_driver, settings as _st, ptp, mtl
    node = db_get_node(node_id)
    if not node:
        return jsonify({"error": "nœud introuvable"}), 404
    has_agent = bool((node.get("agent_url") or "").strip())
    try:
        caps_list = json.loads(node.get("capabilities") or "[]")
    except Exception:
        caps_list = []
    live = {}
    try:
        live = node_driver.capabilities(node) or {}
    except Exception:
        live = {}
    # Auto-réparation : nœud enrôlé mais register raté (capacités DB vides) → ré-enregistrer + relire.
    try:
        if not caps_list and node_driver.ensure_registered(node):
            node = db_get_node(node_id) or node
            caps_list = json.loads(node.get("capabilities") or "[]")
    except Exception:
        pass
    # Repli : si la DB est encore vide, utiliser les capacités LIVE de l'agent (résilience d'affichage).
    if not caps_list:
        caps_list = live.get("capabilities") or []
    nics = live.get("nics") or []
    host = _node_host(node_id) or node.get("host")
    sug = _macvlan_suggest(node_id)
    out = {
        "node_id": node_id, "host": host, "has_agent": has_agent,
        "capabilities": caps_list,
        "container_network": {
            "configured": bool((node.get("docker_network") or "").strip()),
            "name": node.get("docker_network") or "",
            "nics": nics,
            # subnet/passerelle/plage = CLUSTER (lecture seule) ; VLAN = saisi PAR-NŒUD dans le form.
            "cluster": sug,
            # Joignabilité orchestrateur ↔ subnet conteneurs : IP directe (L2) OU route (L3).
            "controller_on_subnet": _controller_on_subnet(sug.get("subnet")),
            "controller_reach": _controller_reach(sug.get("subnet"), sug.get("gateway")),
            "allinone": (node.get("host") or "") in _controller_ipv4s(),
        },
    }
    # La CAPACITÉ DÉCLARÉE fait foi, pas la présence d'une carte : `mtl_capable` est vrai dès qu'une
    # E810 OU une ConnectX-4+ est détectée (mtl.py) — or une ConnectX peut n'être là que pour le RDMA
    # (mxl-fabrics), pas le MTL. Gater l'UI 2110 sur la présence produisait un faux positif sur un nœud
    # RDMA-only jamais déclaré io2110 (feedback utilisateur 2026-07-23).
    if "io2110" in caps_list:
        mtl_iface = (node.get("mtl_iface") or "").strip()
        prep, e810 = {}, []
        try:
            v = mtl.verifier(host)
            if not v.get("error"):
                prep = {k: v.get(k) for k in ("iommu_cmdline", "iommu_active", "hugepages_total",
                        "hugepages_size_ok", "ice_present", "vfio_present", "rdma_unit",
                        "reboot_needed", "bootloader", "sriov", "cpufreq",
                        # ⚠ toute clé ABSENTE de cette liste blanche est filtrée ici et devient
                        # INVISIBLE côté UI (échec silencieux d'affichage).
                        "isolation", "isolated_cpus")}
                e810 = [n for n in (v.get("nics") or [])
                        if n.get("mtl_capable") or n.get("family") == "e810"]
        except Exception:
            pass
        if not e810:                                          # repli : cartes pilotées par `ice`
            e810 = [{"iface": n.get("name"), "model": n.get("driver") or "", "driver": n.get("driver"),
                     "mtl_capable": n.get("driver") == "ice"}
                    for n in nics if n.get("driver") == "ice"]
        ptp_st = None
        try:
            ptp_st = ptp.cached_status(node_id)
            if ptp_st is None and mtl_iface:
                ptp_st = ptp.status_for_node(node_id, host, int(_st.setting_for("ptp_domain", node_id) or 127))
        except Exception:
            ptp_st = None
        try:
            ptp_enabled = bool(_st.setting_for("ptp_enabled", node_id))
        except Exception:
            ptp_enabled = False
        huge = live.get("hugepages") if isinstance(live.get("hugepages"), dict) else \
            {"total": prep.get("hugepages_total") or 0}
        # IP du plan média 2110 : valeur configurée (DB) + IP réellement posée sur la carte (live).
        media_ip_cfg = (node.get("media_ip") or "").strip()
        media_ip_live = ""
        if mtl_iface:
            try:
                import shlex as _sh
                from ..host_ops import ssh_run as _ssh_run
                rc, mout, _ = _ssh_run(host, "ip -4 -o addr show %s" % _sh.quote(mtl_iface), timeout=8)
                if rc == 0:
                    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", mout or "")
                    media_ip_live = m.group(1) if m else ""
            except Exception:
                media_ip_live = ""
        out["io2110"] = {
            "mtl_iface": mtl_iface, "iface_set": bool(mtl_iface),
            "e810_candidates": e810, "all_nics": nics,
            "ptp": ptp_st, "ptp_enabled": ptp_enabled,
            "media_ip": media_ip_cfg, "media_ip_live": media_ip_live,
            "media_ip_ok": bool(media_ip_live),
            "cluster": {"ptp_domain": int(_st.get("ptp_domain") or 127)},
            "hugepages": huge, "mtl_prep": prep,
            "mtl_build": _node_build_status(node_id, "mtl"),   # build en cours/ok/échec (suivi + reload)
        }
    try:
        checks = _node_preflight(host)
        pf = {c["key"]: c for c in checks}
        out["preflight"] = {
            "docker_ok": pf.get("docker", {}).get("status") == "ok",
            "shm": pf.get("shm", {}).get("status"),
            "internet": pf.get("internet", {}).get("status") == "ok",
            "checks": checks,
        }
    except Exception:
        out["preflight"] = {}
    # Présence des images : on inspecte DIRECTEMENT le tag ATTENDU sur le nœud (autoritatif).
    # L'inventaire agent (`live.images`) est peu fiable ici : c'est une liste de {tag, present}
    # (≠ liste de chaînes — l'ancien `str(t).startswith(...)` matchait donc TOUJOURS faux → image
    # « toujours absente »), et l'agent ne suit qu'une liste de tags figée (souvent `:latest`),
    # pas le tag réellement buildé. `_image_present` règle les deux : `docker image inspect <tag attendu>`.
    present_tags = _present_tags(live.get("images"))
    def _img_ok(which):
        tag = _image_tag(which)
        if tag in present_tags:                       # l'agent confirme le tag exact
            return True
        return _image_present(host, tag)              # sinon, inspection autoritative du tag attendu
    out["images"] = {
        "compute": _img_ok("compute") if "compute" in caps_list else False,
        "media":   _img_ok("media") if "media" in caps_list else False,
        "webrtc":  _img_ok("webrtc") if "webrtc" in caps_list else False,
        "mtl":     _img_ok("mtl") if "io2110" in caps_list else False,
    }
    return jsonify(out)

@bp.route("/api/nodes/<int:node_id>/docker-install", methods=["POST"])
@require_perm("settings.edit")
def api_node_docker_install(node_id):
    """Installe Docker sur l'hôte du nœud via l'agent (ssh_run agent-aware)."""
    from ..host_ops import ssh_run
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    host = _node_host(node_id) or node.get("host")
    # Debian 13 : client `docker` dans `docker-cli`, builder dans `docker-buildx` (cf. api_docker_install).
    # Les nommer explicitement, sinon --no-install-recommends laisse le nœud sans `docker` ni `docker build`.
    cmd = ("export DEBIAN_FRONTEND=noninteractive; "
           "apt-get update && apt-get install -y --no-install-recommends docker.io docker-cli docker-buildx && "
           "systemctl enable --now docker")
    rc, out, err = ssh_run(host, cmd, timeout=600)
    ok = (rc == 0)
    db_add_alert("alert.node.docker_install_noeud_ok" if ok else "alert.node.docker_install_noeud_echec",
                 "info" if ok else "error", node_id=node_id, kind="prep", params={"n": node.get("name")})
    return jsonify({"ok": ok, "msg": (out or err or "").strip()[-300:]})

# ─── Prérequis stockage externe (cifs-utils / nfs-common) — LOCAL à l'orchestrateur ──
# Le montage des partages externes (Gestionnaire de Médias) se fait DANS le process orchestrateur
# (mount local), donc ces outils doivent être présents ICI (pas sur proxmox_host).
@bp.route("/api/storage-prereq", methods=["GET"])
@require_perm("settings.edit")
def api_storage_prereq():
    import shutil
    return jsonify({"cifs": bool(shutil.which("mount.cifs")), "nfs": bool(shutil.which("mount.nfs"))})

@bp.route("/api/storage-prereq/install", methods=["POST"])
@require_perm("settings.edit")
def api_storage_prereq_install():
    import subprocess
    cmd = ("export DEBIAN_FRONTEND=noninteractive; apt-get update && "
           "apt-get install -y --no-install-recommends cifs-utils nfs-common")
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=600)
        ok = (r.returncode == 0)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500
    db_add_alert("alert.node.storage_prereq_ok" if ok else "alert.node.storage_prereq_echec",
                 "info" if ok else "error", kind="prep")
    return jsonify({"ok": ok, "msg": (r.stdout or r.stderr or "")[-500:]})
