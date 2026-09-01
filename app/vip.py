# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""VIP de management (keepalived / VRRP) — bascule AUTOMATIQUE de l'adresse, pas du rôle.

Complément de `app/ha.py` : la paire de contrôleurs reste en bascule MANUELLE (le pilotage ne
change de machine que sur décision d'un opérateur, cf. HA.md), mais l'ADRESSE que visent les
opérateurs, elle, suit toute seule. Deux mécanismes, un seul fichier de conf :

  · bascule PLANIFIÉE  → la priorité VRRP est dérivée du rôle (`control_role`), et la conf est
    re-rendue à chaque promote/demote : l'adresse migre avec le pilotage sans toucher au réseau.
  · actif qui MEURT    → le `track_script` (Flask ne répond plus sur 127.0.0.1:5000) fait chuter
    la priorité ; le survivant prend l'adresse en ~5 s.

Conséquence assumée : après une panne, la VIP mène à un contrôleur en LECTURE SEULE. C'est
voulu — c'est exactement la page qui porte l'alarme « l'actif ne répond plus » et le bouton
« Promouvoir ». Une adresse qui mène au bouton vaut mieux qu'une adresse qui ne mène nulle part.

keepalived n'est PAS installé par défaut : `apply(install=True)` l'installe (apt), sur clic
explicite. Tout ici s'exécute sur le contrôleur lui-même (root, systemd) — rien ne passe par les
agents-nœuds.
"""
import logging
import os
import re
import secrets
import shutil
import subprocess

from . import settings

log = logging.getLogger(__name__)

CONF_PATH = "/etc/keepalived/keepalived.conf"
SERVICE = "keepalived"
INSTANCE = "BOBI_VIP"
# Marqueur d'en-tête : on ne réécrit JAMAIS une conf keepalived qui n'est pas la nôtre (un site
# peut avoir un VRRP préexistant — l'écraser en silence serait une panne réseau offerte).
MARKER = "# Généré par Bobi.Studio (Réglages → Haute disponibilité → VIP)"


def _run(cmd, timeout=120):
    """Exécute et retourne (rc, sortie fusionnée). Jamais d'exception : l'appelant décide."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"commande introuvable : {cmd[0]}"
    except Exception as e:
        return 1, str(e)


def installed():
    return bool(shutil.which("keepalived"))


def _curl():
    """Chemin de curl. keepalived exige un chemin ABSOLU : un `curl` nu ne serait jamais trouvé,
    le track_script échouerait en permanence et la priorité resterait au plancher des DEUX côtés
    — soit une VIP qui ne se pose nulle part, sans que rien ne le dise."""
    return shutil.which("curl") or "/usr/bin/curl"


def _conf_is_ours():
    """Vrai si le fichier absent ou porteur de notre marqueur (donc réécrivable)."""
    if not os.path.exists(CONF_PATH):
        return True
    try:
        with open(CONF_PATH) as f:
            return MARKER in f.read(4096)
    except OSError:
        return False


def config_values():
    """Réglages effectifs + priorité dérivée du rôle courant. `auth_pass` inclus (secret :
    l'appelant est déjà derrière `settings.edit`) — il doit être recopié sur l'autre contrôleur."""
    from . import ha
    active = ha.is_active()
    return {
        "enabled":   bool(settings.get("vip_enabled")),
        "address":   (settings.get("vip_address") or "").strip(),
        "interface": (settings.get("vip_interface") or "").strip(),
        "vrid":      int(settings.get("vip_vrid") or 51),
        "auth_pass": (settings.get("vip_auth_pass") or "").strip(),
        "role":      ha.role(),
        "priority":  int(settings.get("vip_priority_active" if active else "vip_priority_standby")
                         or (150 if active else 100)),
    }


def validate(v):
    """Retourne une liste de problèmes (vide = bon). Refuser AVANT d'écrire : une conf keepalived
    invalide ne fait pas échouer le service bruyamment, elle le fait démarrer sans porter la VIP."""
    pbs = []
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$", v["address"] or ""):
        pbs.append("adresse VIP attendue en notation CIDR (ex. x.x.x.x/24)")
    if not re.match(r"^[A-Za-z0-9_.:@-]{1,15}$", v["interface"] or ""):
        pbs.append("interface de management manquante ou invalide (ex. eth0)")
    elif not os.path.exists(f"/sys/class/net/{v['interface']}"):
        pbs.append(f"interface « {v['interface']} » inconnue de cette machine")
    if not (1 <= v["vrid"] <= 255):
        pbs.append("identifiant VRRP (vrid) attendu entre 1 et 255")
    if not shutil.which("curl"):
        pbs.append("curl absent : le témoin de vie du track_script ne pourrait pas s'exécuter "
                   "(`apt install curl`)")
    if not v["auth_pass"]:
        pbs.append("secret VRRP vide")
    elif len(v["auth_pass"]) > 8:
        # Le protocole tronque à 8 octets : accepter plus long, c'est laisser deux contrôleurs
        # « au même secret » diverger sur le 9ᵉ caractère et ne jamais se voir.
        pbs.append("secret VRRP limité à 8 caractères (contrainte du protocole)")
    return pbs


def render_config(v=None):
    """Rend le keepalived.conf (texte). Aperçu dans l'UI avant d'écrire quoi que ce soit."""
    v = v or config_values()
    return f"""{MARKER}
# NE PAS ÉDITER À LA MAIN : ce fichier est réécrit à chaque promote/demote (la priorité VRRP
# est dérivée du rôle de contrôle). Rôle au moment du rendu : {v['role']}.

vrrp_script chk_bobistudio {{
    # L'orchestrateur répond-il ? (identité publique, sans secret) — si non, priorité en chute
    # et l'autre contrôleur prend l'adresse.
    script "{_curl()} -sf -m 2 -o /dev/null http://127.0.0.1:5000/api/update/ping"
    interval 2
    fall 2
    rise 2
    weight -60
}}

vrrp_instance {INSTANCE} {{
    state BACKUP
    interface {v['interface']}
    virtual_router_id {v['vrid']}
    priority {v['priority']}
    advert_int 1
    authentication {{
        auth_type PASS
        auth_pass {v['auth_pass']}
    }}
    virtual_ipaddress {{
        {v['address']} dev {v['interface']}
    }}
    track_script {{
        chk_bobistudio
    }}
}}
"""


def _ensure_auth_pass():
    """Sème le secret VRRP s'il est vide (8 caractères = maximum du protocole)."""
    from .database import db_set_setting
    if (settings.get("vip_auth_pass") or "").strip():
        return
    db_set_setting("vip_auth_pass", secrets.token_urlsafe(8)[:8])


def holds_vip():
    """Cette machine porte-t-elle l'adresse en ce moment ? (source de vérité : le noyau.)"""
    addr = (settings.get("vip_address") or "").split("/")[0].strip()
    if not addr:
        return False
    rc, out = _run(["ip", "-o", "-4", "addr", "show"], timeout=10)
    return rc == 0 and re.search(rf"\binet {re.escape(addr)}/", out) is not None


def status():
    """État complet pour l'UI — ce qui est CONFIGURÉ vs ce qui TOURNE vs qui porte l'adresse."""
    v = config_values()
    rc, out = _run(["systemctl", "is-active", SERVICE], timeout=10)
    running = (out or "").strip() == "active"
    conf_ok = None
    if os.path.exists(CONF_PATH):
        conf_ok = _conf_is_ours()
    return {"installed": installed(), "running": running, "enabled": v["enabled"],
            "address": v["address"], "interface": v["interface"], "vrid": v["vrid"],
            "role": v["role"], "priority": v["priority"],
            "conf_present": os.path.exists(CONF_PATH), "conf_ours": conf_ok,
            "holds_vip": holds_vip(), "problems": validate(v) if v["address"] else []}


def install():
    """Installe keepalived (apt). Action explicite de l'utilisateur — jamais implicite."""
    if installed():
        return True, "keepalived déjà installé"
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    try:
        subprocess.run(["apt-get", "update"], capture_output=True, text=True, timeout=300, env=env)
        p = subprocess.run(["apt-get", "install", "-y", "keepalived"],
                           capture_output=True, text=True, timeout=600, env=env)
        if p.returncode != 0:
            return False, "apt-get install keepalived : " + (p.stderr or p.stdout or "échec")[-400:]
    except Exception as e:
        return False, f"installation keepalived : {e}"
    return (True, "keepalived installé") if installed() else (False, "keepalived toujours absent après apt")


def apply(install_pkg=False):
    """Écrit la conf + (re)démarre keepalived. Retourne (ok, msg).

    Idempotent : appelé aussi par promote/demote pour re-rendre la priorité. Ne fait rien (et le
    dit) si la VIP est désactivée ou si la conf présente n'est pas la nôtre."""
    from .database import db_add_alert
    if not settings.get("vip_enabled"):
        return None, "VIP désactivée (réglage `vip_enabled`)"
    _ensure_auth_pass()
    v = config_values()
    pbs = validate(v)
    if pbs:
        return False, " ; ".join(pbs)
    if install_pkg and not installed():
        ok, msg = install()
        if not ok:
            return False, msg
    if not installed():
        return False, ("keepalived n'est pas installé sur ce contrôleur "
                       "(bouton « Installer keepalived », ou `apt install keepalived`)")
    if not _conf_is_ours():
        return False, (f"{CONF_PATH} existe et n'a pas été écrit par Bobi.Studio — "
                       "un VRRP préexistant ? Déplace-le avant d'activer la VIP ici.")
    try:
        os.makedirs(os.path.dirname(CONF_PATH), exist_ok=True)
        tmp = CONF_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(render_config(v))
        os.replace(tmp, CONF_PATH)
    except OSError as e:
        return False, f"écriture de {CONF_PATH} : {e}"
    _run(["systemctl", "enable", SERVICE], timeout=60)
    rc, out = _run(["systemctl", "restart", SERVICE], timeout=60)
    if rc != 0:
        return False, f"systemctl restart {SERVICE} : {out[-300:]}"
    db_add_alert("alert.node.vip_appliquee", "info", kind="node",
                 params={"address": v['address'], "interface": v['interface'],
                         "priority": v['priority'], "role": v['role']})
    return True, f"appliqué — priorité {v['priority']} ({v['role']})"


def refresh_for_role():
    """Re-rend la conf après un changement de rôle (promote/demote). Best-effort et SILENCIEUX en
    cas d'échec : une VIP mal repositionnée ne doit jamais empêcher une bascule de pilotage."""
    try:
        ok, msg = apply()
        if ok is False:
            log.error("VIP : re-rendu après changement de rôle échoué — %s", msg)
        elif ok:
            log.info("VIP : conf re-rendue pour le rôle courant (%s)", msg)
    except Exception as e:
        log.error("VIP : re-rendu après changement de rôle échoué — %s", e)


def remove():
    """Arrête keepalived et retire NOTRE conf (laisse le paquet installé)."""
    _run(["systemctl", "disable", "--now", SERVICE], timeout=60)
    if os.path.exists(CONF_PATH) and _conf_is_ours():
        try:
            os.remove(CONF_PATH)
        except OSError as e:
            return False, f"suppression de {CONF_PATH} : {e}"
    return True, "keepalived arrêté et conf retirée (l'adresse retombe sur l'autre contrôleur)"
