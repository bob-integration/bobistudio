# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Opérations SSH sur l'HÔTE d'un nœud (full-Docker) : binds /dev/shm & média, IP de PF
(plan média 2110 / PTP), pinning CPU. Le host est passé en paramètre (résolu par-nœud, cf.
app.addressing) — plus de recréation de template LXC ni de pool VF SR-IOV.

Prérequis : clé SSH autorisée sur l'hôte du nœud (root@<host> sans mot de passe) :
    ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N "" && ssh-copy-id root@<host>
"""
import os
import subprocess
import threading
import time
import logging
from . import settings

log = logging.getLogger(__name__)

import time as _time_mod

# Dedup des alertes SSH : on n'émet pas deux fois le même message en moins de 60s.
# Si le message change, l'alerte est immédiate (= un nouveau symptôme).
_SSH_ALERT_DEBOUNCE_S = 60
_last_ssh_alert = {"ts": 0.0, "msg": ""}


def _hote_affichable(host):
    """Rendu SÛR d'un hôte pour un message d'alerte.

    ⚠ Ne JAMAIS interpoler l'objet reçu tel quel. Le 2026-08-30, un appelant a passé la LIGNE DE
    NŒUD complète au lieu de son `host` : le message d'alerte a donc publié `agent_token` et
    `node_cert` en clair dans la table `alerts` — donc dans le journal de l'interface, lisible par
    tout compte ayant accès aux alertes, et à un réglage près (canal syslog/mail/ntfy/SNMP) hors de
    la machine. Une alerte est un texte DIFFUSÉ : ce qu'on y met sort du périmètre du secret."""
    if isinstance(host, (bytes, bytearray)):
        host = host.decode("utf-8", "replace")
    if isinstance(host, str):
        return host[:80]
    # Tout ce qui n'est pas une chaîne est un BUG d'appel : on nomme le type, jamais le contenu.
    return f"<{type(host).__name__} au lieu d'une chaîne>"


def _emit_ssh_alert(host, stderr, cle="alert.node.ssh_host_echec"):
    """Émet une alerte si SSH lui-même a échoué (rc=255). Dedup par message :
    même symptôme en < 60s → silence, message différent → alerte immédiate."""
    lines = (stderr or "").strip().splitlines()
    first = lines[0][:150] if lines else "rc=255 sans stderr"
    full = f"{_hote_affichable(host)} : {first}"
    now = _time_mod.time()
    if _last_ssh_alert["msg"] == full and (now - _last_ssh_alert["ts"]) < _SSH_ALERT_DEBOUNCE_S:
        return
    _last_ssh_alert["ts"] = now
    _last_ssh_alert["msg"] = full
    try:
        from .database import db_add_alert
        db_add_alert(cle, "error", kind="node", params={"full": full})
    except Exception:
        pass


def controller_ssh_pubkey(generate=True):
    """Clé PUBLIQUE SSH du contrôleur (`~/.ssh/id_ed25519.pub`), générée si absente (`generate`).
    Sert à autoriser le contrôleur en SSH root sur les nœuds (confort d'ops/debug — l'orchestration
    elle-même passe par l'agent). Injectée à l'enrôlement → `authorized_keys` du nœud. '' si échec."""
    priv = os.path.expanduser("~/.ssh/id_ed25519")
    pub = priv + ".pub"
    try:
        if not os.path.isfile(pub) and generate:
            os.makedirs(os.path.dirname(priv), mode=0o700, exist_ok=True)
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", priv, "-N", "",
                            "-C", "bobistudio-controller"],
                           capture_output=True, timeout=15)
        if os.path.isfile(pub):
            with open(pub) as f:
                return f.read().strip()
    except Exception as e:
        log.warning("controller_ssh_pubkey: %s", e)
    return ""


def ssh_run(host, cmd, input_data=None, timeout=300):
    """Exécute cmd sur l'hôte. Retourne (rc, stdout, stderr).
    B3-1 : si `host` correspond à un NŒUD-AGENT (ligne nodes avec agent_url), on passe par l'agent
    (token HTTP, /v1/host/exec) au lieu du root-SSH → fin du root-SSH pour les nœuds enrôlés. Repli
    SSH (legacy) pour les nœuds non-agent. Choke-point : tous les callers host-ops en héritent.
    Si rc=255 (SSH lui-même a échoué), émet une alerte avec debounce."""
    # Host falsy = « aucun nœud sélectionné/enrôlé » (0 nœud, ou op host-prep sans node_id), PAS une
    # panne SSH. Court-circuit AVANT tout ssh/alerte : sinon `ssh root@None` rc=255 → alerte trompeuse
    # « SSH vers l'host Proxmox a échoué — None » à chaque poll d'une route host-ops (pool NIC, preflight
    # MTL…). Les appelants gèrent déjà rc≠0 (affichent « aucun nœud »).
    if not host:
        return (255, "", "aucun hôte (pas de nœud sélectionné/enrôlé)")
    # Un hôte qui n'est pas une CHAÎNE est une erreur d'appel, pas une panne réseau — typiquement
    # la ligne de nœud passée à la place de son champ `host`. Sans ce garde-fou on forkait
    # `ssh root@{'id': 34, …, 'agent_token': …}` : le shell d'OpenSSH coupe au DERNIER `@` (celui du
    # modèle de CPU, « Xeon … @ 2.40GHz »), d'où le déroutant « remote username contains invalid
    # characters » — et surtout le dict entier, jetons compris, recopié dans l'alerte. On refuse
    # donc AVANT le fork, et on nomme l'appelant dans le journal : sans lui, ce défaut ne laisse
    # aucune trace exploitable (vécu : introuvable après coup, faute de pile).
    if not isinstance(host, (str, bytes)):
        import traceback
        appelant = "".join(traceback.format_stack()[-3:-1]).strip().replace("\n", " | ")
        log.error("ssh_run: hôte invalide (%s au lieu d'une chaîne) — appelant : %s",
                  type(host).__name__, appelant)
        _emit_ssh_alert(host, "hôte invalide passé à ssh_run (bug d'appel, pas de panne SSH)",
                        cle="alert.node.ssh_hote_invalide")
        return (255, "", f"hôte invalide : {type(host).__name__} au lieu d'une chaîne")
    try:
        from .database import db_get_node_by_host
        from . import node_driver
        _node = db_get_node_by_host(host)
        if _node and _node.get("agent_url"):
            return node_driver.host_exec(_node, cmd, input_data=input_data, timeout=timeout)
    except Exception:
        pass   # repli SSH si la résolution échoue
    full_cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=20",
                f"root@{host}", cmd]
    p = subprocess.run(full_cmd, input=input_data, capture_output=True,
                       text=True, timeout=timeout)
    if p.returncode == 255:
        _emit_ssh_alert(host, p.stderr)
    return p.returncode, p.stdout, p.stderr

# Alias rétro-compat interne
_ssh = ssh_run




# ═════════════════════════════════════════════════════════════════════
# Plan média 2110 : l'IP de la PF est posée par `docker_driver.ensure_media_ip(s)` (rappelée à
# chaque (re)déploiement du moteur), et sa PASSERELLE par `docker_driver.ensure_media_routes`
# (routage par leg) ou, pour un port en vfio-pci, par l'env GATEWAYS/NETMASKS du moteur.
# `ensure_pf_ip` vivait ici depuis le retrait du passthrough SR-IOV, sans AUCUN appelant : elle
# laissait croire qu'une passerelle média était déjà câblée alors qu'elle n'aurait posé qu'une
# route connectée. Retirée le 2026-08-22 pour ne pas laisser deux vérités.
# ═════════════════════════════════════════════════════════════════════


def parse_cpuset(s):
    """Parse format Linux cpuset ('4-6,8,10-12') → set d'entiers. None/'' → set()."""
    if not s:
        return set()
    out = set()
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def host_cpu_count(host):
    """Nombre de CPUs en ligne sur l'host. Renvoie (ok, n, msg)."""
    rc, out, err = ssh_run(host, "nproc --all 2>/dev/null || echo 0", timeout=10)
    if rc != 0:
        return False, 0, f"nproc: {err.strip()}"
    try:
        return True, int(out.strip()), "ok"
    except Exception:
        return False, 0, f"parse: {out!r}"




# ── Retiré avec le backend LXC/Proxmox (full-Docker) ─────────────────────────────────────
# `ensure_shm_bind`, `ensure_dpdk_access`, `ensure_media_bind`, `replace_media_bind`,
# `set_cpu_pinning`, `clear_cpu_pinning` écrivaient dans /etc/pve/lxc/<vmid>.conf. Plus aucun
# appelant depuis le passage full-Docker : les binds, l'accès DPDK et le cpuset sont posés au
# `docker run` par docker_compute / docker_driver (cf. core_pool.effective_cpuset).
