# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Client contrôleur ↔ agent-nœud (`bobi-node-agent`, cf. NODE_AGENT.md /v1).

Amorce de la séparation control-plane / node-plane : quand une ligne `nodes` porte un
`agent_url`, le contrôleur pilote ce nœud via l'API HTTP de l'agent (token) au lieu du
root-SSH/Proxmox. **Coexistence** : un nœud SANS `agent_url` garde son comportement legacy
(`docker_compute`/`docker_driver` en `ssh_run`, ou Proxmox pour `proxmox-lxc`). Aucun
big-bang — l'intégration se fait point par point (1er point : `docker_compute.deploy_compute`).

Stdlib uniquement (urllib), comme `peers.py`/`updater.py`.
"""

import json
import logging
import threading
import shlex
import time
import urllib.request
import urllib.error
from datetime import datetime

from . import ca
from .database import (db_get_node, db_get_node_by_host, db_add_node, db_update_node,
                       db_add_alert)

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-MXL-Node-Token"


def has_agent(node):
    """Vrai si ce nœud est piloté par un agent-nœud (vs legacy ssh/Proxmox)."""
    return bool((node or {}).get("agent_url"))


# ─── mTLS : transport conditionnel HTTP↔HTTPS ────────────────────────────────
# DÉCISION agent_url : on GARDE `agent_url` stocké en `http://ip:port` (jamais réécrit à la
# migration) et on DÉRIVE le schéma `https://` à la volée selon `tls_ready`. L'agent-nœud
# bascule son listener HTTPS sur le MÊME port (:9100) → seul le schéma change, pas le port.
# Le token applicatif (X-MXL-Node-Token) reste envoyé EN PLUS du mTLS (double facteur : TLS
# chiffre+authentifie le canal, le token authentifie l'appel applicatif).
def _tls_on(node):
    """True si ce nœud parle HTTPS : cert signé installé (tls_ready) ET CA dispo côté contrôleur."""
    try:
        return bool((node or {}).get("tls_ready")) and ca.ca_available()
    except Exception:
        return False


def _agent_base(node):
    """URL de base de l'agent avec le BON SCHÉMA selon tls_ready (agent_url reste stocké en http://)."""
    base = (node or {}).get("agent_url") or ""
    if _tls_on(node) and base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


def _requests_tls(node):
    """kwargs TLS pour `requests` (verify=ca_path, cert=(cert,key)) en HTTPS, sinon {}."""
    if not _tls_on(node):
        return {}
    cert, key, ca_path = ca.controller_client_files()
    return {"verify": ca_path, "cert": (cert, key)}


def node_capabilities(node):
    """Liste des capacités du nœud (depuis la colonne JSON `capabilities`)."""
    raw = (node or {}).get("capabilities")
    if not raw:
        return []
    try:
        return json.loads(raw) if isinstance(raw, str) else list(raw)
    except Exception:
        return []


# ─── Transport ───────────────────────────────────────────────────────────────
# DISJONCTEUR par agent. Un nœud dont l'agent ne répond pas ne se signale qu'au bout du timeout
# de connexion (~3 s mesurées sur r620-1 éteint, Errno 113). Chaque appel le repayait : `/api/nodes`
# enchaînait 16 host_exec dont l'essentiel visait CE nœud-là, soit 10 s par requête, et la page
# Monitoring la poll toutes les 5 s (mesuré 2026-08-19).
#
# On mémorise donc l'échec de TRANSPORT quelques secondes et on rend immédiatement la même erreur.
# Trois garde-fous, parce qu'un disjoncteur qui ment est pire que la lenteur qu'il évite :
#   · seuls les échecs de TRANSPORT arment le disjoncteur — un HTTPError signifie que l'agent a
#     répondu, donc qu'il est vivant, et ne doit surtout pas le couper ;
#   · fenêtre COURTE (5 s) : un nœud qui revient est repris presque tout de suite, et le sampler de
#     santé (~5 s) le rétablit de lui-même dès son premier succès ;
#   · tout succès efface l'entrée.
# `bypass_breaker=True` pour un geste d'exploitation explicite (l'opérateur qui retente veut une
# vraie tentative, pas un souvenir).
_breaker = {}          # base_url → (monotone de l'échec, message)
_BREAKER_S = 30.0      # la redécouverte ne dépend PAS de cette durée : le sampler de santé sonde
                       # toutes les ~5 s en bypass et son premier succès referme le disjoncteur.
_breaker_lock = threading.Lock()


_SIGNATURES_TLS_EN_CLAIR = ("WRONG_VERSION_NUMBER", "UNKNOWN_PROTOCOL", "record layer failure",
                            "packet length too long")
# Pendant du cas précédent : le nœud parle bien TLS, mais avec un certificat que NOTRE autorité
# ne reconnaît pas — c'est le nœud ré-enrôlé sur une AUTRE CA (l'installeur en avertit).
# Symptôme voisin, remède OPPOSÉ : là il ne faut surtout pas repasser en clair, il faut
# redistribuer la bonne autorité ou ré-enrôler sur la nôtre.
_SIGNATURES_AUTRE_CA = ("CERTIFICATE_VERIFY_FAILED", "unknown ca", "self signed certificate",
                        "certificate verify failed")


def _signature_tls_en_clair(exc):
    """Vrai si l'erreur ressemble à « on a parlé TLS à un serveur en clair »."""
    t = str(exc)
    return any(sig in t for sig in _SIGNATURES_TLS_EN_CLAIR)


def _signature_autre_ca(exc):
    """Vrai si le pair présente un certificat que notre autorité ne reconnaît pas."""
    t = str(exc)
    return any(sig.lower() in t.lower() for sig in _SIGNATURES_AUTRE_CA)


def _agent_repond_en_clair(url_https, token, timeout):
    """Re-sonde le MÊME agent en HTTP clair. Vrai s'il répond — alors le nœud est vivant et c'est
    bien le transport qui diverge, pas l'agent qui est mort. Sonde COURTE et sans disjoncteur :
    elle ne sert qu'à qualifier une erreur déjà survenue."""
    if not url_https.startswith("https://"):
        return False
    base = "http://" + url_https[len("https://"):]
    base = base[:base.index("/v1")] if "/v1" in base else base
    try:
        req = urllib.request.Request(base.rstrip("/") + "/v1/ping", method="GET")
        if token:
            req.add_header(TOKEN_HEADER, token)
        with urllib.request.urlopen(req, timeout=min(4, timeout)) as r:   # noqa: S310
            return r.status < 500
    except urllib.error.HTTPError:
        return True          # il a répondu, même en refusant : il est vivant et il parle en clair
    except Exception:
        return False


def _request(base_url, method, path, token=None, body=None, timeout=15, tls=False,
             bypass_breaker=False):
    """Appel HTTP(S) à l'agent. Retourne (ok, data|err_str). Jamais d'exception remontée.
    tls=True → schéma forcé en https:// + contexte mTLS (vérifie le pair CA, présente le cert
    client du contrôleur)."""
    _cle = (base_url or "").rstrip("/")
    if not bypass_breaker:
        with _breaker_lock:
            _hit = _breaker.get(_cle)
        if _hit and (time.monotonic() - _hit[0]) < _BREAKER_S:
            return False, _hit[1]
    url = base_url.rstrip("/") + path
    context = None
    if tls:
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        context = ca.controller_client_context()
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers[TOKEN_HEADER] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:   # noqa: S310 (réseau interne)
            txt = r.read().decode()
            with _breaker_lock:
                _breaker.pop(_cle, None)          # l'agent répond : on referme le disjoncteur
            return True, (json.loads(txt) if txt else {})
    except urllib.error.HTTPError as e:
        # L'agent a RÉPONDU (même en erreur) : il est vivant, on n'arme pas le disjoncteur.
        try:
            return False, (json.loads(e.read().decode()).get("error") or f"HTTP {e.code}")
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:
        _msg = f"agent injoignable : {e}"
        # ★ RÉINSTALLER UN NŒUD EFFACE SON mTLS, ET PERSONNE NE PRÉVIENT LE CONTRÔLEUR.
        # `install-node.sh` propose de supprimer le certificat de l'enrôlement précédent
        # (« l'agent repartira en HTTP clair ») et fait ce qu'il annonce. Mais en base le nœud
        # garde `tls_ready=1` + `node_cert` : on continue de l'appeler en HTTPS, l'agent répond
        # en clair, et OpenSSL rend `WRONG_VERSION_NUMBER`. Le nœud passe « down » et l'exploitant
        # lit « agent injoignable » + « images absentes » — DEUX affirmations fausses : l'agent
        # tourne et les images sont là. Le message envoie chercher à l'opposé de la cause
        # (recette Valentin, r620-3-test, 2026-08-21).
        # On ne DEVINE pas : on REDEMANDE en clair. Si l'agent répond, le diagnostic est certain.
        if tls and _signature_tls_en_clair(e):
            if _agent_repond_en_clair(url, token, timeout):
                _msg = (_MARQUE_TLS_CLAIR + "le nœud répond en HTTP CLAIR alors que le contrôleur "
                        "l'appelle en HTTPS — son certificat mTLS a été effacé (réinstallation ?) "
                        "tandis que la base le croit encore en TLS. L'agent est VIVANT. Remède : "
                        "ré-enrôler le nœud, ou effacer `tls_ready`/`node_cert` pour repasser en "
                        "clair. Aucune rétrogradation automatique n'est faite : un pair capable de "
                        "répondre en clair pourrait sinon forcer l'abandon du canal chiffré.")
        elif tls and _signature_autre_ca(e):
            # Le nœud parle TLS, mais sous une autorité que nous ne reconnaissons pas. NE PAS
            # repasser en clair ici : le canal chiffré existe, c'est la confiance qui manque.
            _msg = (_MARQUE_AUTRE_CA + "le nœud présente un certificat signé par une AUTRE "
                    "autorité que la nôtre — typiquement un ré-enrôlement sur une autre CA. "
                    "L'agent est vivant et parle bien TLS. Remède : redistribuer notre autorité "
                    "au nœud, ou le ré-enrôler ici. Ne pas repasser en clair : le chiffrement "
                    "fonctionne, c'est la confiance qui manque.")
        with _breaker_lock:
            _breaker[_cle] = (time.monotonic(), _msg)
        return False, _msg


# Marqueurs internes : `_request` ne connaît que l'URL, `_call` connaît le NŒUD. On étiquette le
# message pour que l'alerte puisse être posée là où l'identifiant du nœud est disponible.
_MARQUE_TLS_CLAIR = "\x00tlsclair\x00"
_MARQUE_AUTRE_CA = "\x00autreca\x00"
_derniere_alerte_tls = {}      # node_id -> monotone
_ALERTE_TLS_PERIODE_S = 600.0  # une alerte toutes les 10 min par nœud : le sampler passe toutes


def _call(node, method, path, body=None, timeout=15, bypass_breaker=False):
    ok, data = _request(_agent_base(node), method, path,
                        token=node.get("agent_token"), body=body, timeout=timeout,
                        tls=_tls_on(node), bypass_breaker=bypass_breaker)
    if not ok and isinstance(data, str) and (_MARQUE_TLS_CLAIR in data or _MARQUE_AUTRE_CA in data):
        cle = "alert.node.tls_en_clair" if _MARQUE_TLS_CLAIR in data else "alert.node.tls_autre_ca"
        data = data.replace(_MARQUE_TLS_CLAIR, "").replace(_MARQUE_AUTRE_CA, "")
        nid = (node or {}).get("id")
        if nid and (time.monotonic() - _derniere_alerte_tls.get(nid, 0.0)) > _ALERTE_TLS_PERIODE_S:
            _derniere_alerte_tls[nid] = time.monotonic()
            try:
                from .database import db_add_alert
                db_add_alert(cle, "warning", node_id=nid, kind="node",
                             params={"n": (node or {}).get("name") or nid})
            except Exception:
                pass
    return ok, data


# ─── Découverte / enregistrement ────────────────────────────────────────────
def ping(agent_url, timeout=4, tls=False):
    """Liveness sans token. Retourne le dict {agent,version} ou None. tls=True → HTTPS mTLS
    (pour sonder un nœud DÉJÀ migré ; en enrôlement le nœud est encore en HTTP → tls=False)."""
    ok, data = _request(agent_url, "GET", "/v1/ping", timeout=timeout, tls=tls,
                        bypass_breaker=True)      # sonde de liveness : même raison que health()
    return data if ok else None


def register(host, port, token, name=None, node=None):
    """Sonde `/v1/capabilities` et upsert la ligne `nodes` (clé = host). Mappe les capacités
    de l'agent vers les colonnes existantes (compute_image/media_image/docker_network/mtl_*),
    pour rester compatible avec l'éligibilité legacy. Retourne (ok, node|err).

    ⚠ Le TLS ne se DEVINE pas depuis (host, port) : un nœud passé en mTLS n'écoute qu'en HTTPS
    avec exigence de certificat client, et un appel en clair y échoue en « Connection refused ».
    Le symptôme est trompeur au possible — « resync des capacités échouée (agent injoignable) »
    alors que le même agent répond parfaitement par `host_exec`, qui passe par `_agent_base` et
    `_requests_tls`. Constaté sur r620-2 le 2026-08-02, lors d'un rattrapage de capacité.
    D'où la relecture de la ligne existante (clé = host, comme l'upsert) : `node` en argument si
    l'appelant l'a déjà, sinon retrouvée en base. Seul l'ENRÔLEMENT initial part légitimement en
    clair — le nœud n'a alors ni certificat ni ligne."""
    agent_url = f"http://{host}:{int(port)}"
    if node is None:
        try:
            node = db_get_node_by_host(host)
        except Exception:
            node = None
    ok, caps = _request(agent_url, "GET", "/v1/capabilities", token=token, timeout=8,
                        tls=_tls_on(node))
    if not ok:
        return False, caps
    cap_list = caps.get("capabilities") or []
    nets = caps.get("networks") or []
    imgs = [i.get("tag") for i in (caps.get("images") or []) if i.get("present")]
    nic = caps.get("nic") or {}

    def _img(prefix):
        return next((t for t in imgs if t.startswith(prefix)), None)

    fields = {
        "agent_url": agent_url,
        "agent_token": token,
        "agent_version": caps.get("agent_version"),
        "capabilities": json.dumps(cap_list),
        "mxl_mount": caps.get("mxl_mount") or "/dev/shm",
        "docker_network": (nets[0]["name"] if nets else None),
        "compute_image": _img("bobi-compute"),
        "media_image": _img("bobi-media"),
        "mtl_capable": 1 if "io2110" in cap_list else 0,
        "mtl_iface": nic.get("iface"),
        "lcores": caps.get("lcores"),
        "status": "up",
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }
    existing = db_get_node_by_host(host)
    if existing:
        db_update_node(existing["id"], **fields)
        nid = existing["id"]
    else:
        nid = db_add_node(name or caps.get("host") or host, host, kind="docker",
                          mxl_mount=fields["mxl_mount"])
        db_update_node(nid, **fields)
    db_add_alert("alert.node.enregistre_agent", "info", node_id=nid, kind="node",
                 params={"n": name or host, "cap": ', '.join(cap_list) or '—'})
    return True, db_get_node(nid)


def ensure_registered(node):
    """Auto-réparation : un nœud À AGENT mais SANS capacités en DB (enrôlé mais `register` raté au
    1er boot — timing) est ré-enregistré (sync depuis l'agent). Idempotent + cheap si déjà enregistré
    (aucun appel agent). Retourne True si (re)synchronisé."""
    if not has_agent(node):
        return False
    caps = (node.get("capabilities") or "").strip()
    if caps and caps not in ("[]", "null"):
        return False                                  # déjà enregistré → no-op
    import urllib.parse
    host = node.get("host")
    if not host:
        return False
    try:
        port = int(urllib.parse.urlsplit(node["agent_url"]).port or 9100)
    except Exception:
        port = 9100
    ok, _ = register(host, port, node.get("agent_token") or "", name=node.get("name"))
    return bool(ok)


def refresh(node):
    """Heartbeat : /v1/health → met à jour status + last_seen (remplace le sondage SSH/Proxmox
    dans surveillance() pour les nœuds à agent). Retourne le dict health ou None."""
    ok, data = _call(node, "GET", "/v1/health", timeout=6)
    db_update_node(node["id"],
                   status="up" if ok and data.get("ok") else "down",
                   last_seen=datetime.now().isoformat(timespec="seconds"))
    return data if ok else None


def health(node):
    """Sonde de liveness (/v1/health) — le sampler de santé et fleet_status. Elle IGNORE le
    disjoncteur de `_request` : c'est ELLE qui décide si le nœud est là, et son succès referme le
    disjoncteur pour tous les autres appelants. Sans ce bypass, un nœud revenu ne serait jamais
    redécouvert — le disjoncteur se rearmerait sur son propre souvenir."""
    ok, data = _call(node, "GET", "/v1/health", timeout=8, bypass_breaker=True)
    return data if ok else None


def capabilities(node):
    ok, data = _call(node, "GET", "/v1/capabilities", timeout=8)
    return data if ok else None


# ─── Cycle de vie des conteneurs (via agent) ─────────────────────────────────
def run_container(node, spec):
    """POST /v1/containers (idempotent côté agent). Retourne (ok, {name,ip,status}|err)."""
    return _call(node, "POST", "/v1/containers", body=spec, timeout=90)


def container_status(node, name):
    ok, data = _call(node, "GET", f"/v1/containers/{name}/status", timeout=8)
    return data if ok else {"status": "absent", "exit_code": None}


def container_action(node, name, action):
    """action ∈ {start, stop, destroy}. Retourne (ok, err|None)."""
    ok, data = _call(node, "POST", f"/v1/containers/{name}/{action}", timeout=45)
    return ok, (None if ok else data)


def container_logs(node, name, tail=100):
    ok, data = _call(node, "GET", f"/v1/containers/{name}/logs?tail={int(tail)}", timeout=15)
    return (data.get("lines") if ok else []) or []


def list_containers(node):
    """Inventaire docker du nœud (`GET /v1/containers` = docker ps -a) → [{name,status,image}]
    ou None si agent injoignable (≠ liste vide = nœud joignable sans conteneur). Une liste par
    nœud remplace N `docker inspect` par-vmid dans la surveillance (audit B5) et fournit les
    deux directions de la réconciliation DB↔réalité (audit B2 : disparu / orphelin)."""
    ok, data = _call(node, "GET", "/v1/containers", timeout=8)
    return (data.get("containers") or []) if ok else None


# ─── mTLS : migration à chaud d'un nœud enrôlé (HTTP+token → HTTPS mTLS) ──────
# Contrat attendu de l'agent-nœud (implémenté par l'agent B, canal HTTP+token actuel) :
#   POST /v1/tls/init    → l'agent génère SA clé privée + un CSR LOCALEMENT (la clé ne sort
#                          jamais), retourne {"ok": true, "csr": "<PEM>"}.
#   POST /v1/tls/install  body {"cert": "<PEM signé>", "ca_cert": "<PEM CA>"} → l'agent écrit
#                          cert/clé/CA sur disque et REBASCULE son listener en HTTPS sur le
#                          MÊME port. Retourne {"ok": true}. Repli HTTP garanti si cert
#                          absent/invalide (le nœud reste joignable).
def rotate_tls(node):
    """Migre un nœud DÉJÀ enrôlé (HTTP+token) vers HTTPS mTLS, via le canal HTTP+token actuel.
    2 temps : (a) l'agent génère clé+CSR (/v1/tls/init), (b) le contrôleur signe et pousse
    cert+CA (/v1/tls/install). Marque tls_ready=1 uniquement si tout réussit. Retourne
    (ok, msg). FILET DE SÉCURITÉ : à la moindre erreur, tls_ready reste 0 + alerte (repli HTTP)."""
    if not has_agent(node):
        return False, "nœud sans agent"
    if not ca.ca_available():
        return False, "CA interne non initialisée (tools/ca-init.py)"
    nid = node["id"]
    name = node.get("name") or f"#{nid}"
    # (a) l'agent génère sa clé + CSR localement (canal HTTP+token, le nœud est encore en HTTP).
    ok, data = _call(node, "POST", "/v1/tls/init", body={}, timeout=30)
    if not ok or not isinstance(data, dict) or not data.get("csr"):
        db_add_alert("alert.node.mtls_init_echec", "warning", node_id=node.get("id"), kind="node",
                     params={"n": name, "data": str(data)})
        return False, f"tls/init : {data}"
    csr = data["csr"]
    # (b) signe le CSR (SAN fixés par nous : IP de contrôle + URI bobi://node/<id>) puis pousse.
    host = node.get("host") or ""
    try:
        cert_pem = ca.sign_csr(csr, ip=host or None, node_id=nid)
    except Exception as e:
        db_add_alert("alert.node.mtls_csr_erreur", "warning", node_id=node.get("id"), kind="node",
                     params={"n": name, "e": str(e)})
        return False, f"sign_csr : {e}"
    cert_str = cert_pem.decode()
    ca_str = ca.ca_cert_pem().decode()
    ok, data = _call(node, "POST", "/v1/tls/install",
                     body={"cert": cert_str, "ca_cert": ca_str}, timeout=30)
    if not ok:
        db_add_alert("alert.node.mtls_install_echec", "warning", node_id=node.get("id"), kind="node",
                     params={"n": name, "data": str(data)})
        return False, f"tls/install : {data}"
    # (c) l'agent écoute désormais en HTTPS → node_driver dial en HTTPS à partir d'ici.
    db_update_node(nid, tls_ready=1, node_cert=cert_str)
    db_add_alert("alert.node.mtls_migre", "info", node_id=node.get("id"), kind="node",
                 params={"n": name})
    return True, "ok"


# ─── Services hôte (via agent) ───────────────────────────────────────────────
def xdp_off(node, iface=None):
    return _call(node, "POST", "/v1/host/xdp-off", body={"iface": iface} if iface else {})


def ensure_image(node, image):
    return _call(node, "POST", "/v1/host/images/ensure", body={"image": image}, timeout=600)


def ensure_network(node, name, parent="", subnet=None, gateway=None, ip_range=None):
    body = {"name": name, "parent": parent}
    for k, v in (("subnet", subnet), ("gateway", gateway), ("ip_range", ip_range)):
        if v:
            body[k] = v
    return _call(node, "POST", "/v1/host/networks/ensure", body=body, timeout=20)


def core_snapshot(node):
    """Instantané des cœurs du nœud (mesure ~0,5 s côté agent : % par cœur + threads/conteneurs
    dessus). Renvoie (ok, data) — le 404 d'un agent < 0.16.0 doit rester distinguable (UI)."""
    return _call(node, "GET", "/v1/host/core-snapshot", timeout=15)


def ptp_status(node):
    ok, data = _call(node, "GET", "/v1/host/ptp", timeout=6)
    return data if ok else None


def ptp_start(node):
    """Relance les unités PTP (mxl-ptp4l + mxl-phc2sys) via l'agent. (ok, data)."""
    return _call(node, "POST", "/v1/host/ptp/start", timeout=20)


def host_exec(node, cmd, input_data=None, timeout=300):
    """B3-1 : exécute une commande hôte VIA L'AGENT (token HTTP) — remplace le root-SSH du contrôleur.
    Même contrat de retour que `host_ops.ssh_run` : (rc, stdout, stderr). Agent injoignable → rc 255
    (équivalent à un échec SSH), pour que les callers existants se comportent comme avant."""
    body = {"cmd": cmd, "timeout": int(timeout)}
    if input_data is not None:
        body["input"] = input_data
    ok, data = _call(node, "POST", "/v1/host/exec", body=body, timeout=int(timeout) + 10)
    if not ok:
        return (255, "", str(data))
    return (int(data.get("rc", 1)), data.get("stdout", ""), data.get("stderr", ""))


def load_image_file(node, tag, path, timeout=1800):
    """POST un tar d'image (fichier, Content-Length connu) à `/v1/host/images/load` → `docker load`
    côté agent. Source-agnostique : le tar peut venir d'un `docker save` local OU d'un export depuis
    un autre nœud. Retourne (ok, msg)."""
    import os
    import requests
    if not has_agent(node):
        return False, "nœud sans agent"
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            r = requests.post(_agent_base(node).rstrip("/") + "/v1/host/images/load",
                              data=f, timeout=timeout,
                              headers={TOKEN_HEADER: node.get("agent_token") or "",
                                       "Content-Type": "application/octet-stream",
                                       "Content-Length": str(size)},
                              **_requests_tls(node))
        j = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
        if r.status_code == 200 and j.get("ok"):
            return True, tag
        return False, (j.get("error") or r.text[:300])
    except Exception as e:
        return False, str(e)


def push_image(node, tag, timeout=1800):
    """Build-once-LOCAL-au-contrôleur → pousse l'image au nœud : `docker save <tag>` (fichier temp)
    puis `load_image_file`. Exige l'image présente côté contrôleur. Retourne (ok, msg)."""
    import os
    import subprocess
    import tempfile
    tmp = tempfile.NamedTemporaryFile(prefix="bobi-img-", suffix=".tar", delete=False)
    tmp.close()
    try:
        if subprocess.run(["docker", "save", "-o", tmp.name, tag],
                          timeout=timeout).returncode != 0:
            return False, f"docker save {tag} échoué (image présente côté contrôleur ?)"
        return load_image_file(node, tag, tmp.name, timeout=timeout)
    except Exception as e:
        return False, str(e)
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def export_image(src_node, tag, dest_path, timeout=1800):
    """Récupère une image d'un NŒUD (buildée dessus) : GET `/v1/host/images/export?tag=` (stream
    chunked = `docker save`) → écrit dans `dest_path`. Permet de distribuer une image build-on-node
    aux autres nœuds sans Docker côté orchestrateur (simple relais). Retourne (ok, msg)."""
    import urllib.parse
    import requests
    if not has_agent(src_node):
        return False, "nœud source sans agent"
    url = (_agent_base(src_node).rstrip("/") + "/v1/host/images/export?tag="
           + urllib.parse.quote(tag))
    try:
        with requests.get(url, stream=True, timeout=timeout,
                          headers={TOKEN_HEADER: src_node.get("agent_token") or ""},
                          **_requests_tls(src_node)) as r:
            if r.status_code != 200:
                try:
                    return False, (r.json().get("error") or r.text[:300])
                except Exception:
                    return False, r.text[:300]
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
        return True, tag
    except Exception as e:
        return False, str(e)


BUILD_RC_TIMEOUT = 254   # le SUIVI a expiré — le build, lui, continue sur le nœud


def build_image(node, tag, ctx_bytes, timeout=2400):
    """B3-1 : build d'image SUR LE NŒUD via l'agent (ferme le dernier root-SSH du contrôleur). Envoie
    le contexte (tar.gz) en POST binaire à `/v1/host/images/build?tag=…` ; l'agent `docker build`
    localement. Retourne (rc, output). Pas de streaming live (le résultat arrive à la fin).

    ⚠ `timeout` NE COUPE QUE L'ATTENTE HTTP — il n'interrompt PAS le `docker build` sur le nœud.
    Ce cas rend `BUILD_RC_TIMEOUT` (254), et surtout PAS un code d'échec : constaté à Horace le
    2026-08-19, `bobi-mtl:0.96.0` a été annoncée « 0/1 nœud OK en 40m00s » alors qu'elle
    compilait encore, et a abouti 13 minutes plus tard. Un faux échec sur une opération d'une
    heure invite à la relancer — donc à faire tourner DEUX compilations en parallèle sur un nœud
    qui porte l'antenne. L'appelant doit surveiller l'apparition de l'image, pas conclure."""
    import urllib.parse
    import requests
    if not has_agent(node):
        return (255, "nœud sans agent")
    url = (_agent_base(node).rstrip("/") + "/v1/host/images/build?tag="
           + urllib.parse.quote(tag))
    try:
        r = requests.post(url, data=ctx_bytes, timeout=timeout,
                          headers={TOKEN_HEADER: node.get("agent_token") or "",
                                   "Content-Type": "application/octet-stream",
                                   "Content-Length": str(len(ctx_bytes))},
                          **_requests_tls(node))
        j = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else None
        out = (j.get("output") if isinstance(j, dict) else None) or r.text[:700]
        # Pas de faux-ok : on n'accepte un succès que si l'agent l'AFFIRME explicitement
        # (`ok == True` ET `rc == 0`). Une 200 ambiguë (pas de JSON, pas de `rc`/`ok`) → échec.
        if r.status_code == 200 and isinstance(j, dict) and j.get("ok") is True and int(j.get("rc", 1)) == 0:
            return (0, out)
        rc = int(j.get("rc", 1)) if isinstance(j, dict) else 1
        return (rc if rc != 0 else 1, out)
    except requests.exceptions.Timeout as e:
        # Le nœud compile toujours : on rend un code DISTINCT pour que l'appelant surveille au
        # lieu de conclure (cf. docstring). Le confondre avec un échec réseau serait le bug.
        return (BUILD_RC_TIMEOUT, "suivi interrompu après %ss (le build continue sur le nœud) : %s"
                % (timeout, e))
    except Exception as e:
        return (255, str(e))


# ── Réseau des conteneurs : ce qui est DÉCLARÉ vs ce qui est POSÉ ─────────────────────────────
# Un nœud dont le macvlan pend dans le vide reste parfaitement sain de l'extérieur : il répond au
# ping, son agent va bien, ses capacités sont déclarées. Seuls ses conteneurs sont muets — et leur
# statut Docker affiche « running ». D'où ce constat, partagé par la fiche du nœud ET par le choix
# de nœud : sans lui, un déploiement automatique repart sur une machine où rien ne sera joignable.
_RESEAU_TTL_S = 120.0
_reseau_cache = {}          # node_id → (ts, état)


def parent_declare(node_id):
    """Interface qui, d'après les RÔLES déclarés, porte les conteneurs (`containers` /
    `mgmt_containers`). "" si aucune. Source de vérité : en base, par nœud, éditable."""
    try:
        from .database import db_get_node_interfaces, role_is_containers
        for r in db_get_node_interfaces(node_id) or []:
            if role_is_containers(r.get("role")) and (r.get("ifname") or "").strip():
                return r["ifname"].strip()
    except Exception as e:
        log.debug("parent_declare(%s): %s", node_id, e)
    return ""


def etat_reseau_conteneurs(node, force=False):
    """{declare, reel, porteuse, derive, sans_lien} — caché `_RESEAU_TTL_S`.

    DEUX défauts distincts, jamais confondus : `derive` (le réseau contredit les rôles → le
    recréer) et `sans_lien` (la carte n'a pas de porteuse → câblage). Les mélanger laisserait
    l'exploitant sans action possible.
    """
    nid = (node or {}).get("id")
    if not nid:
        return {"declare": "", "reel": "", "porteuse": None, "derive": False, "sans_lien": False}
    now = time.time()
    hit = _reseau_cache.get(nid)
    if hit and not force and (now - hit[0]) < _RESEAU_TTL_S:
        return hit[1]
    declare = parent_declare(nid)
    nom = (node.get("docker_network") or "bobimacvlan").strip()
    reel, porteuse = "", None
    try:
        cmd = ("p=$(docker network inspect -f '{{index .Options \"parent\"}}' %s 2>/dev/null); "
               "echo \"$p\"; [ -n \"$p\" ] && cat /sys/class/net/${p%%.*}/carrier 2>/dev/null"
               % shlex.quote(nom))
        rc, out, _err = host_exec(node, cmd, timeout=20)
        lignes = [l.strip() for l in (out or "").splitlines() if l.strip()]
        reel = lignes[0] if lignes else ""
        porteuse = (lignes[1] == "1") if len(lignes) > 1 else None
    except Exception as e:
        log.debug("etat_reseau_conteneurs(%s): %s", nid, e)
    etat = {"declare": declare, "reel": reel, "porteuse": porteuse,
            "derive": bool(declare and reel and reel.split(".")[0] != declare),
            "sans_lien": (porteuse is False)}
    _reseau_cache[nid] = (now, etat)
    return etat
