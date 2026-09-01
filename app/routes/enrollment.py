# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Enrôlement zéro-touch d'un nœud : jeton/profil, clé USB préseedée, ISO iLO (Virtual
Media Redfish), boot réseau PXE/UEFI HTTP. Quatre voies différentes vers le même
`_enroll_finalize` (déclenché par POST /api/nodes/enroll, appelé par le nœud vierge)."""

import os
import re
import secrets
import threading

from flask import jsonify, request, send_file, Response

from . import bp
from .images import _provision_shared_images
from ..auth import require_perm
from ..database import (db_get_node, db_add_node, db_update_node, db_add_alert,
                        db_get_node_by_enroll_token)


# ─── Enrôlement zéro-touch (clé USB préseedée) ─────────────────────────────────
def _enroll_finalize(node_id, ip):
    """Après enrôlement d'un nœud vierge : attend que son agent réponde (install Debian + Docker +
    image + agent = plusieurs minutes), pousse au mieux les images présentes côté contrôleur, puis
    enregistre le nœud (status=up). Thread (long). Cf. NODE_AGENT.md / node_driver."""
    import time, json as _json, subprocess
    from .. import node_driver
    node = db_get_node(node_id) or {}
    try:
        prof = _json.loads(node.get("enroll_profile") or "{}")
    except Exception:
        prof = {}
    token = node.get("agent_token") or ""
    port = int(prof.get("agent_port") or 9100)
    agent_url = f"http://{ip}:{port}"
    up = False
    for _ in range(180):                     # ~30 min : le 1er boot installe tout
        if node_driver.ping(agent_url):
            up = True
            break
        time.sleep(10)
    if not up:
        db_update_node(node_id, status="down")
        db_add_alert("alert.enrolement.agent_injoignable", "warning", node_id=node_id, kind="node",
                     params={"node_id": node_id, "url": agent_url})
        return
    db_update_node(node_id, agent_url=agent_url, agent_token=token)
    node = db_get_node(node_id)
    # ★ PRÉFIXE DE LIBELLÉS SEMÉ PAR NŒUD (BCP-002-01, 2026-08-22). Sans override, tous les nœuds
    # partagent le littéral « 2110 » : deux moteurs 2110 sur deux nœuds émettent alors le même
    # couple groupe:rôle sous un Device cluster UNIQUE, et le MUST d'unicité du rôle dans un
    # groupe tombe — un contrôleur tiers fusionne les Rx des deux nœuds. Semer ici corrige la
    # CAUSE ; le garde-fou d'écriture (db_nmos_resource_upsert) ne rattrape que le symptôme.
    # Semé UNE FOIS et seulement si rien n'est posé : l'exploitant reste maître du libellé.
    try:
        from ..database import db_get_node_setting, db_set_node_setting, _NODE_SETTING_SENTINEL
        if db_get_node_setting(node_id, "nmos_label_prefix",
                               _NODE_SETTING_SENTINEL) is _NODE_SETTING_SENTINEL:
            _nom = (node or {}).get("name") or ""
            if _nom.strip():
                db_set_node_setting(node_id, "nmos_label_prefix", _nom.strip())
    except Exception as _e:
        import logging
        logging.getLogger(__name__).debug(
            "enrôlement : préfixe de libellés non semé pour le nœud %s : %s", node_id, _e)
    # Provisionne les images PARTAGÉES (compute/media/webrtc) requises par les capacités, depuis
    # l'HÔTE DE BUILD (local OU export depuis le nœud de build) — pas seulement le docker local de
    # l'orchestrateur. `mtl` se build par-nœud (carte du nœud), pas poussé ici.
    try:
        d_ok, d_fail = _provision_shared_images(node)
        if d_ok or d_fail:
            db_add_alert("alert.enrolement.images_provisionnees", "info" if not d_fail else "warning",
                         node_id=node_id, kind="node",
                         params={"node_id": node_id, "ok": d_ok, "fail": d_fail})
    except Exception as e:
        db_add_alert("alert.enrolement.provision_images_erreur", "warning", node_id=node_id, kind="node",
                     params={"node_id": node_id, "e": str(e)})
    ok, res = node_driver.register(ip, port, token, name=node.get("name"))
    db_update_node(node_id, status="up" if ok else "down")
    db_add_alert("alert.enrolement.reussi" if ok else "alert.enrolement.register_echoue",
                 "info" if ok else "warning", node_id=node_id, kind="node",
                 params=({"n": node.get("name")} if ok
                         else {"n": node.get("name"), "res": res}))
    # Pool de cœurs : posé DÈS L'ENRÔLEMENT, sinon un nœud neuf ne peut rien réserver — ses
    # conteneurs restent tous partagés et le bouton « Garantir » se refuse, sans que rien n'indique
    # qu'il manque un réglage. Constaté le 2026-08-02 : cinq nœuds sur six sans pool.
    # Dérivé selon la NATURE du nœud (cf. core_pool.pool_par_defaut) — donc APRÈS `register`, qui
    # écrit les capacités en base.
    if ok:
        try:
            from .. import core_pool
            frais = db_get_node(node_id)
            carte = core_pool.read_cpu_core_map(frais) or {}
            if carte:
                pool = core_pool.ensure_compute_cpuset(node_id, len(carte), core_of=carte)
                if pool:
                    db_add_alert("alert.enrolement.pool_coeurs", "info", node_id=node_id, kind="prep",
                                 params={"node_id": node_id, "pool": pool})
            else:
                db_add_alert("alert.enrolement.topologie_illisible", "warning", node_id=node_id, kind="prep",
                             params={"node_id": node_id})
        except Exception as e:
            db_add_alert("alert.enrolement.pool_coeurs_erreur", "warning", node_id=node_id, kind="prep",
                         params={"node_id": node_id, "e": str(e)})

    # Bascule mTLS automatique une fois le nœud enregistré (si la CA interne est initialisée).
    # rotate_tls : init CSR → signe → install → l'agent redémarre en HTTPS. Filet interne (reste en
    # HTTP si un pas échoue). Séquentiel après register → aucune course avec les sondes HTTP ci-dessus.
    from .. import ca
    if ok and ca.ca_available():
        try:
            node = db_get_node(node_id)
            # `rotate_tls` retourne un TUPLE (ok, msg) : le tester directement rendait la condition
            # TOUJOURS vraie (un tuple non vide est truthy) et l'enrôlement annonçait « mTLS activé »
            # même quand la bascule avait échoué et que le nœud restait en HTTP. C'est ainsi que
            # dl360-1 et dell-1 sont restés sans certificat, enrôlés APRÈS l'automatisation, sans
            # que rien ne le dise. Le journal affirmait le contraire de la base.
            ok_tls, msg_tls = node_driver.rotate_tls(node)
            if ok_tls:
                db_add_alert("alert.node.mtls_active", "info", node_id=node_id, kind="node",
                             params={"n": node.get("name")})
            else:
                db_add_alert("alert.node.mtls_echouee", "warning", node_id=node_id, kind="node",
                             params={"n": node.get("name"), "msg": msg_tls})
        except Exception as e:
            db_add_alert("alert.node.mtls_activation_erreur", "warning", node_id=node_id, kind="node",
                         params={"node_id": node_id, "e": str(e)})


@bp.route("/api/nodes/token", methods=["POST"])
@require_perm("settings.edit")
def api_node_token():
    """Jeton d'enrôlement NU : pré-déclare un nœud (profil VIDE) + un enroll_token one-time, sans
    aucune config (capacités/réseau). Le nœud se configure ailleurs (installeur / preseed). `name`
    optionnel. Découple « créer un jeton » de « configurer un nœud » — cf. carte « Jeton d'enrôlement »."""
    import json as _json
    d = request.json or {}
    name = (d.get("name") or "").strip() or f"node-{secrets.token_hex(3)}"
    enroll_token = secrets.token_urlsafe(18)
    agent_token = secrets.token_urlsafe(24)
    nid = db_add_node(name, "", kind="docker")
    db_update_node(nid, status="pending", enroll_token=enroll_token, agent_token=agent_token,
                   enroll_profile=_json.dumps({"agent_port": 9100}))
    db_add_alert("alert.enrolement.jeton_cree", "info", kind="node", params={"n": name})
    return jsonify({"ok": True, "node_id": nid, "enroll_token": enroll_token, "name": name})


@bp.route("/api/nodes/profile", methods=["POST"])
@require_perm("settings.edit")
def api_node_profile():
    """Pré-déclare un nœud (profil) pour l'enrôlement zéro-touch. Génère un enroll_token (one-time)
    + un agent_token, et stocke le profil (capacités + macvlan + ptp…). Retourne le jeton à graver
    dans la clé USB (build-node-iso.sh --enroll-token)."""
    import json as _json
    d = request.json or {}
    name = (d.get("name") or "").strip()
    caps = d.get("capabilities") or []
    if not name or not caps:
        return jsonify({"ok": False, "error": "name et capabilities requis"}), 400
    # MAC du port de gestion (multi-NIC) : épingle d-i sur la bonne carte à l'install. Optionnel.
    mgmt_mac = (d.get("mgmt_mac") or "").strip().lower()
    if mgmt_mac and not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", mgmt_mac):
        return jsonify({"ok": False, "error": "adresse MAC de gestion invalide (ex : aa:bb:cc:dd:ee:ff)"}), 400
    enroll_token = secrets.token_urlsafe(18)
    agent_token = secrets.token_urlsafe(24)
    profile = {
        "capabilities": caps,
        "macvlan_subnet": d.get("macvlan_subnet") or "",
        "macvlan_gateway": d.get("macvlan_gateway") or "",
        "macvlan_vlan": d.get("macvlan_vlan") or "",
        "macvlan_name": d.get("macvlan_name") or "bobimacvlan",
        "ptp_domain": d.get("ptp_domain") or 127,
        "hugepages": d.get("hugepages") or 2048,
        "lcores": d.get("lcores") or "",
        "registry": d.get("registry") or "",
        "agent_port": int(d.get("agent_port") or 9100),
        # Plan de CONTRÔLE : pour mémoire/affichage (l'IP réelle reste apprise du remote_addr à
        # l'enrôlement). Vide = DHCP ; sinon IP statique gravée dans le preseed via build-node-iso.sh.
        "mgmt_ip": d.get("mgmt_ip") or "",
        "mgmt_netmask": d.get("mgmt_netmask") or "",
        "mgmt_gateway": d.get("mgmt_gateway") or "",
        "mgmt_dns": d.get("mgmt_dns") or "",
        "mgmt_mac": mgmt_mac,
    }
    nid = db_add_node(name, "", kind="docker")
    db_update_node(nid, status="pending", enroll_token=enroll_token, agent_token=agent_token,
                   capabilities=_json.dumps(caps), enroll_profile=_json.dumps(profile))
    db_add_alert("alert.enrolement.profil_cree", "info", kind="node", params={"n": name})
    return jsonify({"ok": True, "node_id": nid, "enroll_token": enroll_token})

def _images_attendues(caps):
    """Tags des images runtime que ce nœud doit avoir, d'après ses capacités. Chaîne séparée par
    des espaces (format attendu par `install-node.sh --images`) ; vide si rien n'est déterminable.
    `compute-gpu` est volontairement EXCLU : cette image se build par nœud APRÈS l'enrôlement, la
    réclamer à l'install produirait un avertissement pour une absence normale."""
    try:
        from .images import _image_tag
    except Exception:
        return ""
    which_par_cap = {"compute": "compute", "media": "media", "webrtc": "webrtc", "io2110": "mtl"}
    tags = []
    for cap in caps or []:
        which = which_par_cap.get(cap)
        if not which:
            continue
        try:
            tag = _image_tag(which)
        except Exception:
            tag = ""
        if tag and tag not in tags:
            tags.append(tag)
    return " ".join(tags)


@bp.route("/api/nodes/enroll", methods=["POST"])
def api_node_enroll():
    """Appelé par un nœud vierge au 1er boot (auth = X-MXL-Enroll-Token). Renvoie son profil
    (capacités, macvlan, agent_token, ptp…) et lance la finalisation côté contrôleur (attente agent
    → push images → register). Le enroll_token est consommé (one-time)."""
    import json as _json
    token = request.headers.get("X-MXL-Enroll-Token", "")
    node = db_get_node_by_enroll_token(token) if token else None
    if not node or (node.get("status") or "") not in ("pending", "enrolling"):
        return jsonify({"ok": False, "error": "jeton d'enrôlement invalide ou déjà consommé"}), 401
    from .. import host_ops, settings as _st
    ip = request.remote_addr
    facts = request.json or {}            # faits matériels postés par node-bootstrap.sh (nics/ice/mac)
    try:
        prof = _json.loads(node.get("enroll_profile") or "{}")
    except Exception:
        prof = {}
    # host = IP source du nœud ; consomme le jeton (one-time) ; passe en 'enrolling'.
    # ★ Clé d'hôte SSH : une machine RÉINSTALLÉE présente une nouvelle clé pour la même IP. Le
    # `known_hosts` du contrôleur porte encore l'ancienne → « HOST IDENTIFICATION HAS CHANGED » et
    # TOUT chemin ssh du contrôleur vers ce nœud échoue durement (host_ops, services/files,
    # media_manager, build/inspect d'images), sans rapport apparent avec l'installation. Un
    # enrôlement EST le moment légitime pour oublier l'ancienne identité de cette adresse.
    if ip:
        try:
            import subprocess as _sp
            _sp.run(["ssh-keygen", "-R", ip], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=10)
        except Exception:
            pass
    upd = {"host": ip or "", "status": "enrolling", "enroll_token": ""}
    # Capture l'inventaire matériel pour la config réseau POST-enrôlement (choix du parent macvlan
    # depuis l'UI). ice_iface (E810) → mtl_iface direct ; nics/mac mémorisés dans le profil.
    if facts.get("ice_iface"):
        upd["mtl_iface"] = facts["ice_iface"]
    if facts.get("nics") or facts.get("mac"):
        prof["enroll_facts"] = {"nics": facts.get("nics"), "mac": facts.get("mac"),
                                "ice_iface": facts.get("ice_iface")}
        upd["enroll_profile"] = _json.dumps(prof)
    db_update_node(node["id"], **upd)
    # Le jeton est consommé → l'URL ISO iLO expire ; purge l'ISO cachée (cf. node_iso).
    try:
        from .. import node_iso
        node_iso.purge(token)
    except Exception:
        pass
    # mTLS : l'enrôlement se fait TOUJOURS en HTTP (le nœud n'a pas encore de cert). La bascule
    # HTTPS est déclenchée par le contrôleur À LA FIN de _enroll_finalize (rotate_tls), une fois le
    # nœud enregistré — séquentiel, donc pas de course de timing avec les sondes HTTP de finalize.
    # (On ne signe PAS de cert ici : un cert renvoyé à l'enroll ferait basculer le nœud en HTTPS
    # pendant que finalize sonde encore en HTTP → nœud faussement « down ».)
    threading.Thread(target=_enroll_finalize, args=(node["id"], ip), daemon=True).start()
    db_add_alert("alert.enrolement.en_cours", "info", node_id=node.get("id"), kind="node",
                 params={"n": node.get("name"), "ip": ip})
    return jsonify({
        "capabilities": prof.get("capabilities") or [],
        "macvlan_subnet": prof.get("macvlan_subnet") or "",
        "macvlan_gateway": prof.get("macvlan_gateway") or "",
        "macvlan_vlan": prof.get("macvlan_vlan") or "",
        "macvlan_name": prof.get("macvlan_name") or "bobimacvlan",
        "agent_token": node.get("agent_token") or "",
        "ptp_domain": prof.get("ptp_domain") or 127,
        "hugepages": prof.get("hugepages") or 2048,
        "lcores": prof.get("lcores") or "",
        "registry": prof.get("registry") or "",
        # Tags d'images ATTENDUS, dérivés des meta.json (source unique, cf. images._image_tag) et
        # filtrés par les capacités du profil. Sans ça l'installeur retombait sur des tags codés en
        # dur et périmés (bobi-compute:0.2 face à une flotte en 0.29) : faux « image ABSENTE » à
        # chaque install, tags périmés persistés dans config.json, et — avec un registry — un pull
        # RÉEL d'une version périmée annoncée comme bonne.
        "images": _images_attendues(prof.get("capabilities") or []),
        "agent_port": prof.get("agent_port") or 9100,
        # Clé publique du contrôleur → authorized_keys du nœud (SSH root d'ops, le bootstrap l'installe).
        "controller_ssh_key": host_ops.controller_ssh_pubkey(),
        # Noyau compatible MTL (io2110) : réglage CLUSTER, non figé. Vide = aucun épinglage (avertissement).
        "kernel_pkg": _st.get("io2110_kernel_pkg") or "",
        "kernel_apt": _st.get("io2110_kernel_apt") or "",
    })


@bp.route("/api/nodes/<int:node_id>/tls/rotate", methods=["POST"])
@require_perm("settings.edit")
def api_node_tls_rotate(node_id):
    """Migre à chaud un nœud DÉJÀ enrôlé (tournant en HTTP+token) vers HTTPS mTLS. Via le canal
    HTTP+token actuel : l'agent génère clé+CSR (/v1/tls/init), le contrôleur signe et pousse
    cert+CA (/v1/tls/install), puis marque tls_ready=1. À la moindre erreur, tls_ready reste 0 et
    le nœud demeure joignable en HTTP (filet de sécurité côté agent). Cf. node_driver.rotate_tls."""
    from .. import node_driver
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    if not node_driver.has_agent(node):
        return jsonify({"ok": False, "error": "nœud sans agent (legacy) — mTLS non applicable"}), 409
    ok, msg = node_driver.rotate_tls(node)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 502)


# ─── Gravure de la clé USB d'enrôlement DEPUIS le contrôleur ───────────────────
# Détecte les clés amovibles branchées sur l'hôte du contrôleur, construit l'ISO préseedée et la
# grave (dd). Destructif → garde-fous dans app/usb_flash (amovible only, disque système exclu, device
# re-validé serveur). Alternative à la commande build-node-iso.sh à taper.
@bp.route("/api/usb/devices", methods=["GET"])
@require_perm("settings.edit")
def api_usb_devices():
    from .. import usb_flash
    return jsonify({"devices": usb_flash.list_removable(), "prereqs": usb_flash.prereqs()})

@bp.route("/api/usb/status", methods=["GET"])
@require_perm("settings.edit")
def api_usb_status():
    from .. import usb_flash
    return jsonify(usb_flash.status())

@bp.route("/api/usb/iso-src", methods=["POST"])
@require_perm("settings.edit")
def api_usb_iso_src():
    from .. import settings as _st
    path = ((request.json or {}).get("path") or "").strip()
    _st.set("node_iso_src", path)
    import os as _os
    return jsonify({"ok": True, "iso_ok": bool(path) and _os.path.isfile(path)})

@bp.route("/api/usb/iso-download", methods=["POST"])
@require_perm("settings.edit")
def api_usb_iso_download():
    """Télécharge l'ISO netinst Debian SOURCE depuis une URL sur le contrôleur (tâche de fond),
    puis renseigne le réglage node_iso_src."""
    from .. import usb_flash
    url = ((request.json or {}).get("url") or "").strip()
    ok, msg = usb_flash.start_download(url)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)

@bp.route("/api/usb/iso-download/status", methods=["GET"])
@require_perm("settings.edit")
def api_usb_iso_download_status():
    from .. import usb_flash
    return jsonify(usb_flash.download_status())

@bp.route("/api/usb/install-tools", methods=["POST"])
@require_perm("settings.edit")
def api_usb_install_tools():
    from .. import usb_flash
    ok, msg = usb_flash.install_tools()
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 500)

@bp.route("/api/usb/flash", methods=["POST"])
@require_perm("settings.edit")
def api_usb_flash():
    from .. import usb_flash
    d = request.json or {}
    node = db_get_node(int(d.get("node_id") or 0)) if d.get("node_id") else None
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    token = node.get("enroll_token") or ""
    if not token:
        return jsonify({"ok": False, "error": "jeton d'enrôlement déjà consommé — recréer un profil"}), 409
    device = (d.get("device") or "").strip()
    if not device:
        return jsonify({"ok": False, "error": "clé USB cible requise"}), 400
    controller_url = (d.get("controller_url") or request.host_url or "").rstrip("/")
    ok, msg = usb_flash.start_flash(node, token, controller_url, device)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)

# ─── Servir l'ISO d'enrôlement à iLO 5 (Virtual Media URL) + montage auto Redfish ───────────────
# Au lieu de graver une clé USB, on CONSERVE l'ISO préseedée par-nœud et on l'expose en HTTP à une
# URL gardée par le enroll_token (cf. app/node_iso, app/ilo). iLO la monte en CD/DVD virtuel.

# Route PUBLIQUE (sans auth : iLO n'envoie aucun cookie de session) — gardée par le enroll_token
# one-time non devinable, dans le chemin. 404 dès que le token est consommé (URL expire d'elle-même).
# send_file(conditional=True) → Werkzeug honore les requêtes Range d'iLO (lecture partielle du CD).
@bp.route("/iso/<token>.iso", methods=["GET"])
def serve_node_iso(token):
    from .. import node_iso
    node = db_get_node_by_enroll_token(token) if token else None
    if not node or not node_iso.is_ready(token):
        return jsonify({"error": "ISO introuvable ou jeton consommé"}), 404
    return send_file(node_iso.cache_path(token), mimetype="application/octet-stream",
                     conditional=True, download_name=f"bobi-{node.get('name') or 'node'}.iso")

@bp.route("/api/nodes/<int:node_id>/iso/build", methods=["POST"])
@require_perm("settings.edit")
def api_node_iso_build(node_id):
    from .. import node_iso
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    token = node.get("enroll_token") or ""
    if not token:
        return jsonify({"ok": False, "error": "jeton d'enrôlement déjà consommé — recréer un profil"}), 409
    controller_url = ((request.json or {}).get("controller_url") or request.host_url or "").rstrip("/")
    ok, msg = node_iso.start_build(node, token, controller_url)
    resp = {"ok": ok, "msg": msg, "url": node_iso.iso_url(controller_url, token)}
    return jsonify(resp), (200 if ok else 409)

@bp.route("/api/nodes/<int:node_id>/iso/status", methods=["GET"])
@require_perm("settings.edit")
def api_node_iso_status(node_id):
    from .. import node_iso
    node = db_get_node(node_id)
    st = node_iso.status()
    token = (node or {}).get("enroll_token") or ""
    st["ready"] = bool(token) and node_iso.is_ready(token)
    return jsonify(st)

@bp.route("/api/nodes/<int:node_id>/ilo", methods=["POST"])
@require_perm("settings.edit")
def api_node_ilo_creds(node_id):
    """Enregistre les identifiants iLO (host/user/password) du nœud. Le champ mot de passe est blanc
    au rendu (sécurité) : un POST sans mot de passe NE DOIT PAS écraser celui déjà stocké (sinon
    re-sauver l'hôte vide le mot de passe → 401 NoValidSession au déploiement)."""
    if not db_get_node(node_id):
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    d = request.json or {}
    fields = {"ilo_host": (d.get("ilo_host") or "").strip(),
              "ilo_user": (d.get("ilo_user") or "").strip()}
    pwd = d.get("ilo_password")
    if pwd:   # ne mettre à jour le mot de passe que s'il est fourni non vide
        fields["ilo_password"] = pwd
    db_update_node(node_id, **fields)
    return jsonify({"ok": True})

@bp.route("/api/nodes/<int:node_id>/ilo/test", methods=["POST"])
@require_perm("settings.edit")
def api_node_ilo_test(node_id):
    from .. import ilo
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    ok, msg = ilo.test_connection(node)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 502)

@bp.route("/api/nodes/<int:node_id>/ilo/inventory", methods=["POST"])
@require_perm("settings.edit")
def api_node_ilo_inventory(node_id):
    """Découvre l'inventaire matériel (stockage + cartes réseau) du nœud via Redfish, pour présenter
    à l'opérateur les cibles d'installation et le choix du port de gestion."""
    from .. import ilo
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    inv = ilo.inventory(node)
    return jsonify(inv), (200 if inv.get("ok") else 502)

@bp.route("/api/nodes/<int:node_id>/enroll-config", methods=["POST"])
@require_perm("settings.edit")
def api_node_enroll_config(node_id):
    """Fusionne dans enroll_profile les choix issus de la découverte iLO : MAC du port de gestion
    et/ou cible de partitionnement. Read-modify-write (ne touche pas les autres clés du profil)."""
    import json as _json
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    d = request.json or {}
    try:
        prof = _json.loads(node.get("enroll_profile") or "{}")
    except Exception:
        prof = {}
    # Réseau de contrôle (IP statique / DHCP) — réglé à l'étape 2 (détail du jeton).
    if "mgmt_ip" in d:
        ip = (d.get("mgmt_ip") or "").strip()
        gw = (d.get("mgmt_gateway") or "").strip()
        if ip and not gw:
            return jsonify({"ok": False, "error": "IP statique : passerelle requise"}), 400
        prof["mgmt_ip"] = ip
        prof["mgmt_netmask"] = (d.get("mgmt_netmask") or "").strip()
        prof["mgmt_gateway"] = gw
        prof["mgmt_dns"] = (d.get("mgmt_dns") or "").strip()
    if "mgmt_mac" in d:
        mac = (d.get("mgmt_mac") or "").strip().lower()
        if mac and not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", mac):
            return jsonify({"ok": False, "error": "adresse MAC invalide"}), 400
        prof["mgmt_mac"] = mac
    if "partition" in d:
        part = d.get("partition") or {}
        if part:
            by_id = (part.get("by_id") or "").strip()
            scheme = (part.get("scheme") or "atomic").strip()
            if not by_id:
                return jsonify({"ok": False, "error": "cible de partitionnement (by_id) requise"}), 400
            if scheme not in ("atomic",):
                return jsonify({"ok": False, "error": "schéma de partitionnement non supporté"}), 400
            prof["partition"] = {"by_id": by_id, "label": (part.get("label") or "").strip(),
                                 "size_gb": part.get("size_gb"), "scheme": scheme}
        else:
            prof.pop("partition", None)   # désélection
    db_update_node(node_id, enroll_profile=_json.dumps(prof))
    return jsonify({"ok": True})

@bp.route("/api/nodes/<int:node_id>/ilo/deploy", methods=["POST"])
@require_perm("settings.edit")
def api_node_ilo_deploy(node_id):
    """Construit l'ISO (si absente) puis la monte + boote via Redfish. Synchrone (séquence courte
    une fois l'ISO prête ; le build xorriso peut prendre quelques minutes)."""
    from .. import node_iso, ilo
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    token = node.get("enroll_token") or ""
    if not token:
        return jsonify({"ok": False, "error": "jeton d'enrôlement déjà consommé — recréer un profil"}), 409
    if not (node.get("ilo_host") and node.get("ilo_user")):
        return jsonify({"ok": False, "error": "identifiants iLO manquants (host/user)"}), 400
    controller_url = ((request.json or {}).get("controller_url") or request.host_url or "").rstrip("/")
    ok, msg = node_iso.build_now(node, token, controller_url)
    if not ok:
        return jsonify({"ok": False, "error": "build ISO : " + msg}), 500
    url = node_iso.iso_url(controller_url, token)
    ok, steps = ilo.deploy_node(node, url)
    steps_out = [{"label": s[0], "ok": s[1], "msg": s[2]} for s in steps]
    if ok:
        db_add_alert("alert.enrolement.ilo_lance", "info", node_id=node_id, kind="node",
                     params={"n": node.get("name"), "ilo_host": node.get("ilo_host")})
    return jsonify({"ok": ok, "url": url, "steps": steps_out}), (200 if ok else 502)

# ─── Boot réseau PXE / UEFI HTTP Boot (Phase 0, sans licence iLO) ───────────────
# Le contrôleur sert l'arbre netboot Debian + grub.cfg/preseed/payload générés en HTTP. Un nœud HPe
# Gen10 boote dessus via RBSU → UEFI HTTP Boot. Cf. app/pxe.py + PXE_ANALYSIS.md.

# Route PUBLIQUE (le firmware/installeur n'authentifie pas). Sert l'arbre netboot, MAIS intercepte
# grub.cfg / preseed.cfg / payload/* qui sont GÉNÉRÉS pour le nœud armé. Le seul secret (enroll_token,
# dans enroll.conf) est one-time, consommé à l'enrôlement — même modèle que la clé USB / l'ISO iLO.
@bp.route("/pxe/<path:filename>", methods=["GET"])
def serve_pxe(filename):
    from flask import send_from_directory, Response, abort
    from .. import pxe
    token, ctrl = pxe.armed()
    base = filename.rsplit("/", 1)[-1]
    # grub.cfg (quel que soit le sous-chemin demandé par bootnetx64.efi selon son prefix compilé)
    if base == "grub.cfg":
        if not ctrl:
            abort(404)
        node = db_get_node_by_enroll_token(token) if token else None
        return Response(pxe.grub_cfg(ctrl, node=node), mimetype="text/plain")
    if filename in ("preseed.cfg", "preseed-manual.cfg"):
        node = db_get_node_by_enroll_token(token) if token else None
        if not node:
            abort(404)
        return Response(pxe.preseed(node, ctrl, manual=(filename == "preseed-manual.cfg")),
                        mimetype="text/plain")
    if filename.startswith("payload/"):
        name = filename[len("payload/"):]
        node = db_get_node_by_enroll_token(token) if token else None
        if not node:
            abort(404)
        if name == "enroll.conf":
            return Response(pxe.enroll_conf(node, ctrl, token), mimetype="text/plain")
        p = pxe.payload_path(name)
        if not p:
            abort(404)
        return send_file(p, mimetype="application/octet-stream", conditional=True)
    # Sinon : fichier statique de l'arbre netboot (noyau/initrd/efi…), avec garde anti-traversal.
    # Content-Type FORCÉ à application/octet-stream : le firmware UEFI HTTP Boot (HPe Gen10/iLO5)
    # exige ce type pour le NBP (bootnetx64.efi) et abandonne silencieusement sur tout autre type
    # (ex. application/efi déduit de l'extension) → écran « booting URL file » figé après le HEAD.
    if not os.path.isdir(pxe.PXE_ROOT):
        abort(404)
    return send_from_directory(pxe.PXE_ROOT, filename, conditional=True,
                               mimetype="application/octet-stream")

@bp.route("/api/pxe/download", methods=["POST"])
@require_perm("settings.edit")
def api_pxe_download():
    from .. import pxe
    url = ((request.json or {}).get("url") or "").strip()
    ok, msg = pxe.start_download(url)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 409)

@bp.route("/api/pxe/status", methods=["GET"])
@require_perm("settings.edit")
def api_pxe_status():
    from .. import pxe
    st = pxe.download_status()
    st["ready"] = pxe.netboot_ready()
    return jsonify(st)

@bp.route("/api/pxe/state", methods=["GET"])
@require_perm("settings.edit")
def api_pxe_state():
    from .. import pxe
    token, ctrl = pxe.armed()
    node = db_get_node_by_enroll_token(token) if token else None
    return jsonify({"ready": pxe.netboot_ready(),
                    "armed_node_id": (node or {}).get("id"),
                    "armed_node_name": (node or {}).get("name"),
                    "boot_url": pxe.boot_url(ctrl) if ctrl else None})

@bp.route("/api/nodes/<int:node_id>/pxe/arm", methods=["POST"])
@require_perm("settings.edit")
def api_node_pxe_arm(node_id):
    from .. import pxe
    node = db_get_node(node_id)
    if not node:
        return jsonify({"ok": False, "error": "nœud introuvable"}), 404
    token = node.get("enroll_token") or ""
    if not token:
        return jsonify({"ok": False, "error": "jeton d'enrôlement déjà consommé — recréer un profil"}), 409
    if not pxe.netboot_ready():
        return jsonify({"ok": False, "error": "arbre netboot absent — télécharger le netboot Debian d'abord"}), 409
    controller_url = ((request.json or {}).get("controller_url") or request.host_url or "").rstrip("/")
    pxe.arm(token, controller_url)
    db_add_alert("alert.enrolement.pxe_arme", "info", node_id=node_id, kind="node",
                 params={"n": node.get("name")})
    return jsonify({"ok": True, "boot_url": pxe.boot_url(controller_url)})

@bp.route("/api/pxe/disarm", methods=["POST"])
@require_perm("settings.edit")
def api_pxe_disarm():
    from .. import pxe
    pxe.disarm()
    return jsonify({"ok": True})
