# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France

"""Boot réseau PXE / UEFI HTTP Boot — Phase 0 (preuve de chaîne, sans licence iLO ni DHCP).

Le contrôleur sert l'arbre netboot Debian + un grub.cfg + un preseed + le payload node_agent, le tout
en HTTP (réutilise Flask). Un nœud HPe Gen10 démarre dessus via RBSU → UEFI HTTP Boot (URL explicite) ;
l'installeur d-i tire le preseed puis le payload du contrôleur, et la chaîne d'enrôlement existante
(bobi-node-bootstrap → install-node.sh → /api/nodes/enroll) prend le relais — inchangée.

Phase 0 = UN nœud « armé » à la fois (manuel, séquentiel). L'armement génère le grub.cfg/preseed/payload
pour le enroll_token de ce nœud. L'industrialisation zéro-touch (proxyDHCP + auto-enrôlement par MAC)
est la Phase 1+ (cf. PXE_ANALYSIS.md).

Sécurité : les fichiers netboot (noyau/initrd/efi) ne sont pas secrets. Le seul secret est le
enroll_token (dans enroll.conf servi au nœud armé) — one-time, consommé à l'enrôlement, même modèle
que la clé USB / l'ISO iLO.
"""
import os
import logging
import tarfile
import threading
import time

from . import settings

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE_AGENT = os.path.join(ROOT, "node_agent")
PRESEED_SRC = os.path.join(NODE_AGENT, "iso", "preseed.cfg")
# Arbre netboot Debian extrait (debian-installer/amd64/{linux,initrd.gz,bootnetx64.efi,grub/…}).
PXE_ROOT = os.path.join(ROOT, "pxe_root")

# Port du serveur HTTP dédié au boot réseau (app/pxe_server.py). Sert /pxe/* en HTTP/1.1 KEEP-ALIVE :
# le firmware UEFI HTTP Boot (HPe Gen10) réutilise la connexion entre son HEAD et son GET et échoue
# (« Failed to download the URI file ») sur le « Connection: close » forcé par le serveur Werkzeug de
# l'orchestrateur (:5000). Le boot pointe donc ce port, pas le :5000 du contrôleur.
PXE_HTTP_PORT = 8000

# Fichiers du payload servis tels quels depuis node_agent/ (enroll.conf est généré dynamiquement).
_PAYLOAD_STATIC = ("install-node.sh", "agent.py", "bobi-node-agent.service",
                   "node-bootstrap.sh", "bobi-node-bootstrap.service")

# État global d'un (unique) téléchargement de netboot en cours.
_dl_status = {"state": "idle", "msg": "", "pct": 0, "at": 0.0}
_dl_lock = threading.Lock()


def _human(n):
    n = float(n)
    for u in ("o", "Ko", "Mo", "Go"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "o" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} To"


# ─── Téléchargement / extraction de l'arbre netboot Debian ─────────────────────

def download_status():
    with _dl_lock:
        return dict(_dl_status)


def _dl_set(state, msg, pct=None):
    with _dl_lock:
        _dl_status.update({"state": state, "msg": msg, "at": time.time()})
        if pct is not None:
            _dl_status["pct"] = pct
    log.info("pxe.download: [%s] %s", state, msg)


def start_download(url):
    """Télécharge le netboot.tar.gz Debian depuis `url` et l'extrait dans PXE_ROOT. Retourne (ok,msg)."""
    url = (url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "URL http(s) requise"
    with _dl_lock:
        if _dl_status["state"] == "running":
            return False, "un téléchargement est déjà en cours"
        _dl_status.update({"state": "running", "msg": "démarrage…", "pct": 0, "at": time.time()})
    threading.Thread(target=_download, args=(url,), daemon=True).start()
    return True, "started"


def _download(url):
    import requests
    tmp = os.path.join(ROOT, ".netboot.tar.gz.part")
    try:
        _dl_set("running", "téléchargement du netboot Debian…", 0)
        with requests.get(url, stream=True, timeout=60) as r:
            if r.status_code != 200:
                return _dl_set("error", f"HTTP {r.status_code} sur {url}")
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / total) if total else 0
                    _dl_set("running", f"{_human(done)}" + (f" / {_human(total)}" if total else ""), pct)
        if not tarfile.is_tarfile(tmp):
            os.unlink(tmp)
            return _dl_set("error", "le fichier téléchargé n'est pas une archive tar.gz valide")
        _dl_set("running", "extraction…")
        # Extraction propre dans un PXE_ROOT neuf (purge l'ancien arbre).
        import shutil
        if os.path.isdir(PXE_ROOT):
            shutil.rmtree(PXE_ROOT, ignore_errors=True)
        os.makedirs(PXE_ROOT, exist_ok=True)
        with tarfile.open(tmp, "r:gz") as t:
            members = [m for m in t.getmembers() if _safe_member(m.name)]
            t.extractall(PXE_ROOT, members=members)
        os.unlink(tmp)
        bf = find_boot_files()
        if not bf.get("efi") or not bf.get("kernel") or not bf.get("initrd"):
            return _dl_set("error", "arbre netboot incomplet (bootnetx64.efi / linux / initrd.gz introuvables)")
        _dl_set("done", f"netboot prêt (efi: {bf['efi']})", 100)
    except Exception as e:
        if os.path.isfile(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
        _dl_set("error", str(e))


def _safe_member(name):
    """Anti path-traversal à l'extraction (refuse .. et chemins absolus)."""
    return not (name.startswith("/") or ".." in name.split("/"))


def find_boot_files():
    """Localise (chemins RELATIFS à PXE_ROOT) bootnetx64.efi / linux / initrd.gz dans l'arbre extrait."""
    out = {"efi": None, "kernel": None, "initrd": None}
    if not os.path.isdir(PXE_ROOT):
        return out
    for dirpath, _dirs, files in os.walk(PXE_ROOT):
        for fn in files:
            rel = os.path.relpath(os.path.join(dirpath, fn), PXE_ROOT)
            if fn == "bootnetx64.efi" and not out["efi"]:
                out["efi"] = rel
            elif fn == "linux" and not out["kernel"]:
                out["kernel"] = rel
            elif fn == "initrd.gz" and not out["initrd"]:
                out["initrd"] = rel
    return out


def netboot_ready():
    bf = find_boot_files()
    return bool(bf.get("efi") and bf.get("kernel") and bf.get("initrd"))


# ─── Armement d'un nœud (Phase 0 : un seul à la fois) ──────────────────────────

def armed():
    """Retourne (token, controller_url) du nœud armé, ou (None, None)."""
    return (settings.get("pxe_armed_token") or "").strip() or None, \
           (settings.get("pxe_controller_url") or "").strip() or None


def arm(token, controller_url):
    settings.set("pxe_armed_token", token or "")
    settings.set("pxe_controller_url", (controller_url or "").rstrip("/"))


def disarm():
    settings.set("pxe_armed_token", "")
    settings.set("pxe_controller_url", "")


def boot_url(controller_url):
    """URL à saisir en RBSU → UEFI HTTP Boot (NBP bootnetx64.efi). Pointe le serveur PXE keep-alive
    (PXE_HTTP_PORT), PAS le :5000 du contrôleur (cf. PXE_HTTP_PORT) : sinon « Failed to download »."""
    bf = find_boot_files()
    if not bf.get("efi"):
        return None
    from urllib.parse import urlparse
    host = urlparse((controller_url or "").rstrip("/")).hostname or ""
    # Racine (pas de /pxe) : shim/grub réclament grubx64.efi + modules au prefix compilé sans /pxe.
    return f"http://{host}:{PXE_HTTP_PORT}/{bf['efi']}"


# ─── Génération grub.cfg / preseed / enroll.conf ───────────────────────────────

def _choose_interface(node):
    """Sélecteur d'interface d-i (`netcfg/choose_interface`) : le MAC du port de gestion s'il est
    fixé au profil (multi-NIC → choix déterministe), sinon 'auto' (1ʳᵉ carte qui répond)."""
    import json as _json
    try:
        prof = _json.loads((node or {}).get("enroll_profile") or "{}")
    except Exception:
        prof = {}
    return (prof.get("mgmt_mac") or "").strip() or "auto"


def _static_net_kargs(node):
    """Paramètres noyau d-i pour une config réseau STATIQUE (depuis enroll_profile.mgmt_*), ou "" si
    pas d'IP de gestion (→ DHCP). Indispensable au boot réseau sans DHCP : applique le statique AVANT
    le tirage du preseed. Termine par un espace si non vide."""
    import json as _json
    try:
        prof = _json.loads((node or {}).get("enroll_profile") or "{}")
    except Exception:
        prof = {}
    if not prof.get("mgmt_ip"):
        return ""
    dns = prof.get("mgmt_dns") or prof.get("mgmt_gateway") or ""
    return (
        "netcfg/disable_autoconfig=true "
        f"netcfg/get_ipaddress={prof['mgmt_ip']} "
        f"netcfg/get_netmask={prof.get('mgmt_netmask') or ''} "
        f"netcfg/get_gateway={prof.get('mgmt_gateway') or ''} "
        f"netcfg/get_nameservers={dns} "
        "netcfg/confirm_static=true "
    )


def grub_cfg(controller_url, prefix="/pxe", node=None):
    """grub.cfg minimal : un seul menu, auto-boot, preseed tiré du contrôleur. Chemins HTTP
    server-absolus. `prefix` = espace de noms du serveur : "/pxe" pour la route Flask (:5000),
    "" pour le serveur PXE dédié (:8000) qui sert à la racine — IMPÉRATIF côté :8000 car shim/grub
    réclament leurs modules/grubx64.efi au `prefix` compilé `/debian-installer/amd64/grub` (sans /pxe).

    Si le profil du nœud porte une IP de gestion, on l'injecte EN PARAMÈTRES NOYAU (pas seulement dans
    le preseed) : sans DHCP, d-i doit configurer le réseau STATIQUEMENT *avant* de pouvoir tirer le
    preseed (chicken-and-egg) — sinon il bloque sur « Network autoconfiguration failed »."""
    bf = find_boot_files()
    kernel = f"{prefix}/{bf.get('kernel') or 'debian-installer/amd64/linux'}"
    initrd = f"{prefix}/{bf.get('initrd') or 'debian-installer/amd64/initrd.gz'}"
    base = (controller_url or "").rstrip("/")
    # Partie réseau commune aux deux entrées (choix d'interface + IP statique éventuelle).
    net = f"netcfg/choose_interface={_choose_interface(node)} " + _static_net_kargs(node)
    # Entrée AUTOMATIQUE : preseed complet, priority=critical → aucune question (partitionnement inclus).
    auto_kargs = (f"auto=true priority=critical preseed/url={base}{prefix}/preseed.cfg "
                  + net + "DEBIAN_FRONTEND=text ---")
    # Entrée SEMI-AUTO : preseed complet MAIS sans priority=critical → d-i déroule tout seul tant que
    # ça passe, et ne s'arrête pour demander QUE si une étape coince (réponse manquante/échec) → on
    # reprend la main au point de blocage, le reste reste automatique.
    semi_kargs = (f"auto=true preseed/url={base}{prefix}/preseed.cfg "
                  + net + "DEBIAN_FRONTEND=text ---")
    # Entrée MANUELLE : preseed SANS le bloc partman (preseed-manual.cfg) et sans priority=critical →
    # d-i pose les questions de partitionnement (le reste — locale, paquets, payload — reste auto).
    man_kargs = (f"auto=true preseed/url={base}{prefix}/preseed-manual.cfg "
                 + net + "DEBIAN_FRONTEND=text ---")
    return (
        "set timeout=10\n"
        "set default=0\n"
        'menuentry "Bobi.Studio — installation AUTOMATIQUE (sans surveillance)" {\n'
        f"    linux {kernel} {auto_kargs}\n"
        f"    initrd {initrd}\n"
        "}\n"
        'menuentry "Bobi.Studio — installation SEMI-AUTO (reprend la main si une étape coince)" {\n'
        f"    linux {kernel} {semi_kargs}\n"
        f"    initrd {initrd}\n"
        "}\n"
        'menuentry "Bobi.Studio — installation MANUELLE (partitionnement à la main)" {\n'
        f"    linux {kernel} {man_kargs}\n"
        f"    initrd {initrd}\n"
        "}\n"
    )


def _strip_partman(text):
    """Retire toutes les réponses partman préséed d'un bloc preseed (→ soit d-i pose les questions en
    mode manuel, soit on réinjecte un bloc ciblé)."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("d-i partman"))


def _partman_block(part):
    """Bloc partman ciblé sur la cible choisie par l'opérateur (découverte iLO), ou None si pas de
    choix. Ciblage par /dev/disk/by-id/<by_id> (WWN du volume = déterministe, pas de /dev/sdX deviné).
    Schéma disque-entier simple (regular/atomic). ⚠ EFFACE le disque cible."""
    if not part:
        return None
    by_id = (part.get("by_id") or "").strip()
    if not by_id:
        return None
    disk = f"/dev/disk/by-id/{by_id}"
    return (
        "### Partitionnement — cible choisie via l'inventaire iLO (EFFACE TOUT)\n"
        "d-i partman-auto/method string regular\n"
        f"d-i partman-auto/disk string {disk}\n"
        "d-i partman-auto/choose_recipe select atomic\n"
        "d-i partman-partitioning/confirm_write_new_label boolean true\n"
        "d-i partman/choose_partition select finish\n"
        "d-i partman/confirm boolean true\n"
        "d-i partman/confirm_nooverwrite boolean true\n"
        "d-i partman-md/confirm boolean true\n"
    )


def preseed(node, controller_url, prefix="/pxe", manual=False):
    """Preseed PXE : reprend le preseed ISO (locale/partition/paquets) MAIS remplace le late_command
    /cdrom par un tirage HTTP du payload depuis le contrôleur. Injecte l'IP statique si le profil en a.
    `prefix` = espace de noms du serveur HTTP (cf. grub_cfg) : "" pour le serveur PXE dédié (:8000).
    `manual=True` : retire les réponses partman préséed → d-i pose les questions de partitionnement
    (le reste reste automatisé). Sert l'entrée grub « installation MANUELLE »."""
    base = (controller_url or "").rstrip("/")
    # 1. Tronque le preseed source AVANT son bloc late_command (marqueur stable dans le fichier).
    try:
        with open(PRESEED_SRC, "r") as f:
            src = f.read()
    except Exception:
        src = ""
    marker = "### ─── late_command"
    head = src.split(marker)[0].rstrip() if marker in src else src.rstrip()

    # Choix d'interface déterministe (multi-NIC) : épingle d-i sur le MAC du port de gestion si fixé.
    # (Le karg grub prime déjà côté PXE, mais on garde le preseed cohérent.)
    iface = _choose_interface(node)
    if iface != "auto":
        head = head.replace("netcfg/choose_interface select auto",
                            f"netcfg/choose_interface select {iface}")

    # Profil d'enrôlement (choix opérateur : IP statique, MAC de gestion, cible de partitionnement).
    import json as _json
    try:
        prof = _json.loads(node.get("enroll_profile") or "{}")
    except Exception:
        prof = {}

    # 1bis. Partitionnement. MANUEL → on retire les réponses partman (d-i pose les questions). Sinon, si
    # l'opérateur a choisi une cible (découverte iLO), on remplace le bloc historique (recette atomic sur
    # /dev/sda…, qui se bloque sur RAID HPe / disques multiples) par un bloc ciblé by-id déterministe.
    # Sans choix → bloc historique inchangé.
    if manual:
        head = _strip_partman(head)
    else:
        block = _partman_block(prof.get("partition"))
        if block:
            head = _strip_partman(head) + "\n" + block

    # 2. IP statique du plan de contrôle (si profil) — sinon DHCP (réseau d'install requis).
    if prof.get("mgmt_ip"):
        dns = prof.get("mgmt_dns") or prof.get("mgmt_gateway") or ""
        head += (
            "\n### Réseau de contrôle — IP statique (profil)\n"
            "d-i netcfg/disable_autoconfig boolean true\n"
            f"d-i netcfg/get_ipaddress string {prof['mgmt_ip']}\n"
            f"d-i netcfg/get_netmask string {prof.get('mgmt_netmask') or ''}\n"
            f"d-i netcfg/get_gateway string {prof.get('mgmt_gateway') or ''}\n"
            f"d-i netcfg/get_nameservers string {dns}\n"
            "d-i netcfg/confirm_static boolean true\n"
        )

    # 3. late_command PXE : tire le payload en HTTP (curl, déjà dans pkgsel) puis pose le first-boot.
    files = " ".join(list(_PAYLOAD_STATIC) + ["enroll.conf"])
    late = (
        "\n### ─── late_command PXE : payload tiré du contrôleur en HTTP ───\n"
        "d-i preseed/late_command string \\\n"
        "  in-target mkdir -p /opt/bobi-node-src /etc/bobi-node /usr/local/sbin ; \\\n"
        f"  in-target sh -c 'for f in {files}; do "
        f"curl -fsS -o /opt/bobi-node-src/$f {base}{prefix}/payload/$f; done' ; \\\n"
        "  in-target chmod 0755 /opt/bobi-node-src/install-node.sh /opt/bobi-node-src/node-bootstrap.sh ; \\\n"
        "  in-target install -m 0755 /opt/bobi-node-src/node-bootstrap.sh /usr/local/sbin/bobi-node-bootstrap.sh ; \\\n"
        "  in-target install -m 0600 /opt/bobi-node-src/enroll.conf /etc/bobi-node/enroll.conf ; \\\n"
        "  in-target install -m 0644 /opt/bobi-node-src/bobi-node-bootstrap.service /etc/systemd/system/bobi-node-bootstrap.service ; \\\n"
        "  in-target systemctl enable bobi-node-bootstrap.service\n"
    )
    return head + "\n" + late


def enroll_conf(node, controller_url, token):
    """enroll.conf zéro-touch (consommé par node-bootstrap.sh) : URL contrôleur + enroll_token."""
    base = (controller_url or "").rstrip("/")
    return (f"# Généré par pxe.py (boot réseau) pour le nœud « {node.get('name')} »\n"
            f'CONTROLLER_URL="{base}"\n'
            f'ENROLL_TOKEN="{token}"\n')


def payload_path(name):
    """Chemin disque d'un fichier payload STATIQUE (node_agent/), ou None si non autorisé/absent."""
    if name not in _PAYLOAD_STATIC:
        return None
    p = os.path.join(NODE_AGENT, name)
    return p if os.path.isfile(p) else None
