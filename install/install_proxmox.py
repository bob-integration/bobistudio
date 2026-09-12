#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Bobi.Studio — Bootstrapper Proxmox
Tourne en root sur le nœud Proxmox. Crée la VM orchestrateur, déploie le code,
installe les dépendances et configure le service systemd.

Usage : python3 install_proxmox.py
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import urllib.request
import urllib.error
import ssl
import zipfile

# ── ANSI ──────────────────────────────────────────────────────────────────────
R = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BLUE = "\033[34m"
WHITE = "\033[97m"
BG_DARK = "\033[48;5;234m"

def _c(color, text): return f"{color}{text}{R}"

def ok(msg):   print(f"  {GREEN}✓{R}  {msg}")
def err(msg):  print(f"  {RED}✗{R}  {RED}{msg}{R}")
def info(msg): print(f"  {CYAN}·{R}  {msg}")
def warn(msg): print(f"  {YELLOW}!{R}  {YELLOW}{msg}{R}")
def log(msg):  print(f"     {DIM}{msg}{R}")

def sep(): print(f"  {DIM}{'─' * 54}{R}")

# ── En-tête ───────────────────────────────────────────────────────────────────
def print_header():
    w = 52
    print()
    print(f"  {BOLD}{CYAN}╔{'═' * w}╗{R}")
    title = "Bobi.Studio — Installation"
    pad = (w - len(title)) // 2
    print(f"  {BOLD}{CYAN}║{' ' * pad}{WHITE}{title}{CYAN}{' ' * (w - pad - len(title))}║{R}")
    print(f"  {BOLD}{CYAN}╚{'═' * w}╝{R}")
    print()

def print_step(n, total, title):
    print()
    bar = f"[{n}/{total}]"
    print(f"  {BOLD}{BLUE}{bar}{R} {BOLD}{WHITE}{title}{R}")
    sep()

def print_done(title):
    print(f"  {GREEN}{BOLD}✓ {title}{R}")

def print_fail(title):
    print(f"  {RED}{BOLD}✗ {title}{R}")

# ── Spinner ───────────────────────────────────────────────────────────────────
SPIN_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

class Spinner:
    def __init__(self, label):
        self.label = label
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        i = 0
        while not self._stop.is_set():
            c = SPIN_CHARS[i % len(SPIN_CHARS)]
            print(f"\r  {CYAN}{c}{R}  {self.label}…", end="", flush=True)
            time.sleep(0.1)
            i += 1

    def __enter__(self):
        self._t.start(); return self

    def __exit__(self, *_):
        self._stop.set(); self._t.join()
        print("\r" + " " * (len(self.label) + 12) + "\r", end="", flush=True)

# ── Saisie ────────────────────────────────────────────────────────────────────
# Sentinelle renvoyée par ask() quand l'utilisateur tape '<' (revenir au champ
# précédent), si allow_back=True.
_BACK = object()

def ask(prompt, default=None, secret=False, required=True, validate=None, allow_back=False):
    """Saisit une valeur.
    - validate : callable(val) -> (ok: bool, msg: str). Redemande si invalide.
    - allow_back : si l'utilisateur tape '<', renvoie _BACK (navigation arrière).
    """
    hint = f" [{DIM}{default}{R}]" if default is not None else ""
    req_mark = f" {RED}*{R}" if required and default is None else ""
    full = f"  {BOLD}{prompt}{R}{hint}{req_mark} : "
    while True:
        try:
            if secret:
                import getpass
                val = getpass.getpass(full)
            else:
                val = input(full).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            bail("Installation annulée.")
        if allow_back and val == "<":
            return _BACK
        if not val:
            if default is not None:
                val = default
            elif not required:
                return ""
            else:
                warn("Ce champ est obligatoire.")
                continue
        if validate:
            okv, msg = validate(val)
            if not okv:
                warn(msg)
                continue
        return val


# ── Validateurs (callable(val) -> (ok, msg)) ────────────────────────────────────
def _v_ipv4(cidr_ok=False):
    import ipaddress
    def _v(val):
        try:
            if "/" in val:
                if not cidr_ok:
                    return False, "préfixe CIDR non attendu ici (ex. 10.0.0.1)"
                ipaddress.ip_interface(val)
            else:
                ipaddress.ip_address(val)
            return True, ""
        except ValueError:
            ex = "x.x.x.x/24" if cidr_ok else "x.x.x.x"
            return False, f"adresse IPv4 invalide (ex. {ex})"
    return _v

def _v_int(lo, hi):
    def _v(val):
        try:
            n = int(val)
        except (TypeError, ValueError):
            return False, "valeur entière attendue"
        if not (lo <= n <= hi):
            return False, f"hors plage ({lo}–{hi})"
        return True, ""
    return _v

def _v_hostname(val):
    import re as _re
    if _re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", val):
        return True, ""
    return False, "hostname invalide (lettres, chiffres, tirets ; pas de point)"

def _v_host_reachable(val):
    """IP ou hostname du nœud Proxmox, mais refuse le loopback : l'orchestrateur,
    DANS le container, doit pouvoir joindre ce host (ssh root@host pour le template,
    bind /dev/shm, NIC, PTP). 127.0.0.1 boucle sur le container → cassé."""
    low = val.strip().lower()
    if low in ("localhost", "::1") or low.startswith("127."):
        return False, ("loopback refusé : l'orchestrateur (dans le container) doit joindre "
                       "ce host — saisir l'IP du nœud sur le réseau du container")
    import ipaddress
    try:
        ipaddress.ip_address(val)
        return True, ""
    except ValueError:
        # pas une IP → accepter un hostname
        return _v_hostname(val.split(".")[0]) if "." in val else _v_hostname(val)


def ask_form(fields, state):
    """Parcourt une liste de champs avec navigation arrière ('<').
    fields = [{key, prompt, default, validate, secret}]. state pré-rempli
    (réédition : valeur déjà saisie = nouveau défaut). Retourne state complété."""
    i = 0
    while i < len(fields):
        f = fields[i]
        cur = state.get(f["key"], f.get("default"))
        nav = "  (< pour revenir)" if i > 0 else ""
        val = ask(f["prompt"] + nav, default=cur, secret=f.get("secret", False),
                  required=f.get("required", True), validate=f.get("validate"),
                  allow_back=(i > 0))
        if val is _BACK:
            i -= 1
            continue
        state[f["key"]] = val
        i += 1
    return state

def ask_yn(prompt, default=True):
    hint = "[O/n]" if default else "[o/N]"
    full = f"  {BOLD}{prompt}{R} {DIM}{hint}{R} : "
    try:
        val = input(full).strip().lower()
    except (KeyboardInterrupt, EOFError):
        print(); bail("Installation annulée.")
    if not val:
        return default
    return val in ("o", "oui", "y", "yes")

def choose(prompt, options, default_idx=0, labels=None, allow_back=False):
    """Affiche une liste numérotée et renvoie l'option choisie (valeur de `options`).
    `labels` (optionnel) = libellés d'affichage parallèles à `options`."""
    disp = labels or options
    print()
    for i, lbl in enumerate(disp):
        mark = f" {DIM}(défaut){R}" if i == default_idx else ""
        print(f"     {DIM}{i+1}.{R} {lbl}{mark}")
    print()
    def _v(val):
        try:
            n = int(val)
        except (TypeError, ValueError):
            return False, "numéro attendu"
        if not (1 <= n <= len(options)):
            return False, f"hors plage (1–{len(options)})"
        return True, ""
    val = ask(prompt, default=str(default_idx + 1), validate=_v, allow_back=allow_back)
    if val is _BACK:
        return _BACK
    return options[int(val) - 1]

def _confirm_recap(title, fields):
    """Affiche un récapitulatif (liste de (label, valeur)) et demande validation.
    Retourne True si l'utilisateur valide, False pour re-saisir l'étape."""
    print()
    print(f"  {BOLD}Récapitulatif — {title}{R}")
    width = max((len(lbl) for lbl, _ in fields), default=0)
    for lbl, val in fields:
        print(f"     {DIM}{lbl.ljust(width)}{R}  {val}")
    print()
    return ask_yn("Valider ces valeurs ?", default=True)

def bail(msg):
    print()
    err(msg)
    print()
    sys.exit(1)

# ── Subprocess ────────────────────────────────────────────────────────────────
def run(cmd, check=True, capture=False, input_data=None):
    """Lance une commande. Retourne (returncode, stdout) si capture=True."""
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True, input=input_data)
        return r.returncode, r.stdout.strip()
    r = subprocess.run(cmd, text=True, input=input_data)
    if check and r.returncode != 0:
        raise RuntimeError(f"Commande échouée : {' '.join(str(x) for x in cmd)}")
    return r.returncode, ""

def run_stream(cmd, prefix="     "):
    """Lance une commande et affiche sa sortie ligne par ligne."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )
    for line in proc.stdout:
        print(f"{prefix}{DIM}{line.rstrip()}{R}", flush=True)
    proc.wait()
    return proc.returncode

def pct_exec(vmid, *cmd, stream=False, capture=False, input_data=None):
    """Exécute une commande dans le container via pct exec."""
    full = ["pct", "exec", str(vmid), "--"] + list(cmd)
    if stream:
        return run_stream(full)
    if capture:
        return run(full, capture=True)
    return run(full)

# ── Proxmox API (depuis le nœud, sans token — pvesh local) ───────────────────
def pvesh_get(path):
    rc, out = run(["pvesh", "get", path, "--output-format", "json"],
                  check=False, capture=True)
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None

def vmid_free(node, vmid):
    data = pvesh_get(f"/nodes/{node}/lxc") or []
    used = {int(c.get("vmid", 0)) for c in data}
    return int(vmid) not in used

def list_templates(node):
    """Retourne les images LXC disponibles dans les storages (content=vztmpl)."""
    storages = pvesh_get(f"/nodes/{node}/storage") or []
    templates = []
    for s in storages:
        sid = s.get("storage")
        if not sid:
            continue
        content = pvesh_get(f"/nodes/{node}/storage/{sid}/content?content=vztmpl") or []
        for item in content:
            volid = item.get("volid", "")
            if volid:
                templates.append(volid)
    return templates

def list_storages(node, content=None):
    """Liste les storages du nœud. Si `content` (ex. 'rootdir', 'images', 'vztmpl'),
    ne garde que ceux qui le déclarent dans leur champ `content`."""
    data = pvesh_get(f"/nodes/{node}/storage") or []
    out = []
    for s in data:
        sid = s.get("storage")
        if not sid:
            continue
        if content and content not in (s.get("content") or "").split(","):
            continue
        out.append(s)
    return out

def test_proxmox_token(host, tok_id, tok_sec):
    """Teste l'API Proxmox HTTPS avec le token, exactement comme le fera
    l'orchestrateur (port 8006, TLS auto-signé non vérifié). Retourne (ok, msg)."""
    url = f"https://{host}:8006/api2/json/version"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"PVEAPIToken={tok_id}={tok_sec}")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            data = json.loads(r.read().decode())
        ver = (data.get("data") or {}).get("version", "?")
        return True, f"API Proxmox joignable (pve-manager {ver})"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, ("401 — token refusé : vérifier le Token ID / secret, et que "
                           "Privilege Separation est DÉSACTIVÉ à la création du token")
        return False, f"HTTP {e.code} — {e.reason}"
    except Exception as e:
        return False, f"connexion impossible à {host}:8006 ({e})"

def pveam_available_debian():
    """Templates Debian téléchargeables via pveam (section system). `pveam update`
    d'abord pour rafraîchir l'index. Retourne la liste des noms de template."""
    run(["pveam", "update"], check=False, capture=True)
    rc, out = run(["pveam", "available", "--section", "system"], check=False, capture=True)
    if rc != 0 or not out:
        return []
    tpls = []
    for line in out.splitlines():
        parts = line.split()
        if parts and "debian" in parts[-1].lower():
            tpls.append(parts[-1])
    # Plus récents d'abord (tri lexical inversé : debian-13 avant debian-12).
    return sorted(set(tpls), reverse=True)

def download_template(storage, template):
    """Télécharge un template LXC dans `storage` via pveam. Retourne (ok, volid)."""
    info(f"Téléchargement de {template} sur {storage}…")
    rc = run_stream(["pveam", "download", storage, template])
    if rc != 0:
        return False, None
    return True, f"{storage}:vztmpl/{template}"

def detect_node_ip(node):
    """IP de management du nœud Proxmox (1ère adresse non-loopback sur un bridge/iface
    active), pour proposer un défaut au champ host. None si indétectable."""
    nets = pvesh_get(f"/nodes/{node}/network") or []
    # Priorité aux bridges (vmbr*) qui portent une adresse, puis toute iface avec address.
    candidates = []
    for n in nets:
        addr = (n.get("address") or "").strip()
        if not addr or addr.startswith("127.") or ":" in addr:
            continue
        iface = n.get("iface", "")
        prio = 0 if iface.startswith("vmbr") else 1
        candidates.append((prio, addr))
    candidates.sort()
    return candidates[0][1] if candidates else None

# ── ÉTAPES ────────────────────────────────────────────────────────────────────

def step1_prerequisites():
    print_step(1, 7, "Prérequis")
    errors = []

    # pct disponible
    if shutil.which("pct"):
        ok("pct (Proxmox LXC tool) disponible")
    else:
        errors.append("pct introuvable — ce script doit tourner sur un nœud Proxmox")

    # pvesh disponible
    if shutil.which("pvesh"):
        ok("pvesh disponible")
    else:
        errors.append("pvesh introuvable")

    # SSH key
    key = "/root/.ssh/id_ed25519"
    if os.path.exists(key):
        ok(f"Clé SSH root présente ({key})")
    else:
        warn(f"Clé SSH absente ({key}) — sera nécessaire pour le template LXC")
        info("Créer avec : ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N \"\"")

    # Source : zip ou répertoire
    install_dir = os.path.dirname(os.path.abspath(__file__))
    zip_path = os.path.join(install_dir, "bobistudio.zip")

    if os.path.exists(zip_path):
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        has_main = any(n == "main.py" or n.endswith("/main.py") for n in names)
        if has_main:
            ok(f"Archive détectée : bobistudio.zip ({len(names)} fichiers)")
            plugins = sorted({n.split("/")[1] for n in names
                              if n.startswith("plugins/") and n.count("/") >= 2})
            services = sorted({n.split("/")[1] for n in names
                               if n.startswith("services/") and n.count("/") >= 2})
            info(f"Plugins inclus : {', '.join(plugins) or '—'}")
            info(f"Services inclus : {', '.join(services) or '—'}")
            src = ("zip", zip_path)
        else:
            errors.append("bobistudio.zip trouvé mais main.py absent — archive invalide")
            src = None
    elif os.path.exists(os.path.join(install_dir, "main.py")):
        ok(f"Code Bobi.Studio détecté : {install_dir}")
        src = ("dir", install_dir)
    else:
        errors.append("Ni bobistudio.zip ni main.py trouvés à côté de l'installeur")
        src = None

    if errors:
        print()
        for e in errors:
            err(e)
        bail("Prérequis manquants.")

    return src

def _select_storage(node):
    """Liste les storages capables de porter les disques LXC (content=rootdir) et
    laisse choisir. Repli sur tous les storages, puis sur une saisie libre."""
    info("Recherche des storages disponibles…")
    storages = list_storages(node, content="rootdir") or list_storages(node)
    if not storages:
        warn("Aucun storage détecté — saisie manuelle.")
        return ask("Storage Proxmox pour les disques LXC", default="local-lvm")
    sids = [s["storage"] for s in storages]
    labels = [f"{s['storage']}  {DIM}({s.get('type','?')}, content: {s.get('content','?')}){R}"
              for s in storages]
    default_idx = sids.index("local-lvm") if "local-lvm" in sids else 0
    return choose("Storage pour les disques LXC", sids, default_idx, labels=labels)

def _select_or_download_template(node):
    """Liste les templates LXC présents (vztmpl), Debian en priorité. Si aucun
    template Debian n'est présent, propose de le télécharger via pveam."""
    info("Recherche des templates LXC présents…")
    present = list_templates(node)
    debian = [t for t in present if "debian" in t.lower()]

    if debian:
        # Plus récents d'abord (debian-13 avant debian-12).
        debian.sort(reverse=True)
        others = [t for t in present if t not in debian]
        options = debian + others
        return choose("Template LXC", options, 0)

    # Aucun template Debian présent.
    warn("Aucun template Debian trouvé sur ce nœud.")
    if present:
        info("Templates présents (non-Debian) :")
        for t in present:
            print(f"     {DIM}·{R} {t}")
    info("Bobi.Studio cible Debian (l'agent et le venv sont prévus pour Debian).")

    if ask_yn("Télécharger un template Debian maintenant (via pveam) ?", default=True):
        with Spinner("Récupération de la liste des templates Debian"):
            avail = pveam_available_debian()
        if not avail:
            warn("Impossible de récupérer la liste (pas d'accès Internet sur l'hôte ?).")
        else:
            # Storage destination = un storage qui accepte vztmpl (souvent 'local').
            vz = [s["storage"] for s in list_storages(node, content="vztmpl")]
            if not vz:
                warn("Aucun storage n'accepte les templates (content=vztmpl).")
                info("Activer le contenu « Container template » sur un storage "
                     "(Datacenter → Storage → Edit → Content) puis relancer.")
            else:
                dflt_s = vz.index("local") if "local" in vz else 0
                dst = choose("Storage de destination du template", vz, dflt_s)
                # Défaut = 1er Debian « standard » récent si présent dans la liste.
                std = [t for t in avail if "standard" in t.lower()]
                ordered = std + [t for t in avail if t not in std]
                tpl = choose("Template Debian à télécharger", ordered, 0)
                okd, volid = download_template(dst, tpl)
                if okd:
                    ok(f"Template téléchargé : {volid}")
                    return volid
                warn("Échec du téléchargement.")

    # Repli : saisie manuelle (ou ajout via l'UI Proxmox puis ressaisie du volid).
    info("Vous pouvez aussi ajouter un template via l'UI Proxmox "
         "(<node> → local → CT Templates → Templates) puis saisir son volid ici.")
    return ask("Template LXC (volid)",
               default="local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst")

def step2_proxmox_config(node_auto):
    print_step(2, 7, "Configuration Proxmox")

    info("Le token Proxmox doit être créé manuellement dans l'UI Proxmox :")
    info("  Datacenter → Permissions → API Tokens → Add")
    info("  User: root@pam  |  Token ID: bobistudio  |  Privilege Separation: NON")

    # Défaut host = IP réelle du nœud (pas loopback : le container doit la joindre).
    host_default = detect_node_ip(node_auto)
    if host_default:
        info(f"IP du nœud détectée : {host_default}")

    state = {}
    while True:
        # 1) Connexion Proxmox (node / host / token) + test API HTTPS.
        conn_fields = [
            {"key": "node",    "prompt": "Nom du nœud Proxmox", "default": node_auto,
             "validate": _v_hostname},
            {"key": "host",    "prompt": "IP du nœud Proxmox (joignable depuis le container)",
             "default": host_default, "validate": _v_host_reachable},
            {"key": "tok_id",  "prompt": "Token ID", "default": "root@pam!bobistudio"},
            {"key": "tok_sec", "prompt": "Token secret (UUID)", "secret": True},
        ]
        print()
        ask_form(conn_fields, state)

        print()
        with Spinner("Test de connexion à l'API Proxmox"):
            okv, msg = test_proxmox_token(state["host"], state["tok_id"], state["tok_sec"])
        if okv:
            ok(msg)
        else:
            err(msg)
            if ask_yn("Re-saisir les identifiants Proxmox ?", default=True):
                continue
            if not ask_yn("Continuer malgré l'échec du test (déconseillé) ?", default=False):
                continue

        node = state["node"]

        # 2) Storage pour les disques LXC (liste des storages existants).
        print()
        state["storage"] = _select_storage(node)

        # 3) Template LXC (liste des templates ; téléchargement Debian si absent).
        print()
        state["template"] = _select_or_download_template(node)

        # 4) Récapitulatif.
        masked = (state["tok_sec"][:4] + "…" + state["tok_sec"][-2:]) if len(state["tok_sec"]) > 6 else "••••"
        recap = [
            ("Nœud Proxmox",   state["node"]),
            ("Hôte",           state["host"]),
            ("Token ID",       state["tok_id"]),
            ("Token secret",   masked),
            ("Storage",        state["storage"]),
            ("Template LXC",   state["template"]),
        ]
        if _confirm_recap("Configuration Proxmox", recap):
            return {
                "node": state["node"], "host": state["host"], "tok_id": state["tok_id"],
                "tok_sec": state["tok_sec"], "storage": state["storage"],
                "template": state["template"],
                "proxmox_token": f"PVEAPIToken={state['tok_id']}={state['tok_sec']}",
            }

def step3_create_vm(cfg, src):
    print_step(3, 7, "VM Orchestrateur")

    node = cfg["node"]

    def _v_vmid(val):
        okv, msg = _v_int(100, 999999)(val)
        if not okv:
            return False, msg
        if not vmid_free(node, val):
            return False, f"VMID {val} déjà utilisé sur ce nœud"
        return True, ""

    state = {}
    fields = [
        {"key": "vmid",     "prompt": "VMID de la VM orchestrateur", "default": "1000",
         "validate": _v_vmid},
        {"key": "hostname", "prompt": "Hostname de la VM", "default": "bobistudio",
         "validate": _v_hostname},
        {"key": "vm_ip",    "prompt": "IP de la VM (CIDR, ex. x.x.x.x/24)",
         "validate": _v_ipv4(cidr_ok=True)},
        {"key": "gateway",  "prompt": "Gateway", "validate": _v_ipv4()},
        {"key": "bridge",   "prompt": "Bridge réseau", "default": "vmbr1"},
        {"key": "vlan_tag", "prompt": "VLAN tag", "validate": _v_int(1, 4094)},
        {"key": "ram",      "prompt": "RAM (Mo)", "default": "2048",
         "validate": _v_int(512, 1048576)},
        {"key": "cores",    "prompt": "Cores CPU", "default": "2",
         "validate": _v_int(1, 256)},
    ]
    while True:
        print()
        ask_form(fields, state)
        recap = [
            ("VMID",       state["vmid"]),
            ("Hostname",   state["hostname"]),
            ("IP",         state["vm_ip"]),
            ("Gateway",    state["gateway"]),
            ("Bridge",     state["bridge"]),
            ("VLAN tag",   state["vlan_tag"]),
            ("RAM (Mo)",   state["ram"]),
            ("Cores CPU",  state["cores"]),
        ]
        if _confirm_recap("VM Orchestrateur", recap):
            break

    vmid     = state["vmid"];     hostname = state["hostname"]
    vm_ip    = state["vm_ip"];    gateway  = state["gateway"]
    bridge   = state["bridge"];   vlan_tag = state["vlan_tag"]
    ram      = state["ram"];      cores    = state["cores"]

    print()
    info(f"Création du container {vmid} ({hostname})…")

    net_cfg = f"name=eth0,bridge={bridge},ip={vm_ip},gw={gateway},tag={vlan_tag}"

    cmd = [
        "pct", "create", str(vmid), cfg["template"],
        "--hostname", hostname,
        "--memory", str(ram),
        "--cores", str(cores),
        "--net0", net_cfg,
        "--storage", cfg["storage"],
        "--rootfs", f"{cfg['storage']}:8",
        "--unprivileged", "0",
        "--features", "nesting=1",
        "--start", "1",
        "--ostype", "debian",
    ]

    rc = run_stream(cmd)
    if rc != 0:
        bail("Échec de la création du container.")
    ok(f"Container {vmid} créé et démarré")

    # Attendre que le réseau soit disponible
    info("Attente du démarrage réseau…")
    for attempt in range(30):
        with Spinner(f"Ping {vm_ip.split('/')[0]}"):
            time.sleep(2)
        rc, _ = run(["pct", "exec", str(vmid), "--", "true"], check=False, capture=True)
        if rc == 0:
            break
    else:
        bail("Le container ne répond pas après 60s.")
    ok("Container accessible via pct exec")

    return {
        "vmid":     int(vmid),
        "vm_ip":    vm_ip.split("/")[0],
        "hostname": hostname,
        "bridge":   bridge,
        "vlan_tag": int(vlan_tag),
    }

def step4_deploy_code(cfg, vm_cfg, src):
    print_step(4, 7, "Déploiement du code")
    vmid = vm_cfg["vmid"]
    src_type, src_path = src

    # Créer /opt/bobistudio dans le container
    pct_exec(vmid, "mkdir", "-p", "/opt/bobistudio")
    ok("Répertoire /opt/bobistudio créé")

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        if src_type == "zip":
            # Extraire le zip vers un dossier temporaire puis retar en .tar.gz
            info("Préparation de l'archive depuis le zip…")
            with Spinner("Extraction + recompression"):
                extract_dir = tempfile.mkdtemp()
                try:
                    with zipfile.ZipFile(src_path) as zf:
                        zf.extractall(extract_dir)
                    with tarfile.open(tmp_path, "w:gz") as tar:
                        for entry in os.scandir(extract_dir):
                            tar.add(entry.path, arcname=entry.name)
                finally:
                    shutil.rmtree(extract_dir, ignore_errors=True)
        else:
            # Tar du répertoire source directement
            info("Création de l'archive depuis le répertoire source…")
            excludes = {"venv", "__pycache__", "db_bobistudio.db",
                        "bobistudio.log", ".git", "bobistudio_install"}
            # Plugins et services inclus dans cette distribution
            PLUGINS_INCLUDE  = {"receiver_2110", "sender_2110", "streamer"}
            SERVICES_INCLUDE = {"nmos", "webrtc_gateway"}

            def _add_filtered(tar, path, arcname):
                """Ajoute récursivement en excluant __pycache__ et .pyc."""
                for root, dirs, files in os.walk(path):
                    dirs[:] = [d for d in dirs if d != "__pycache__"]
                    for fn in files:
                        if fn.endswith(".pyc"):
                            continue
                        full = os.path.join(root, fn)
                        rel  = os.path.relpath(full, path)
                        tar.add(full, arcname=os.path.join(arcname, rel))

            with Spinner("Compression"):
                with tarfile.open(tmp_path, "w:gz") as tar:
                    for entry in os.scandir(src_path):
                        if entry.name in excludes or entry.name.endswith(".pyc"):
                            continue
                        if entry.name == "plugins":
                            for pl in os.scandir(entry.path):
                                if pl.name in PLUGINS_INCLUDE:
                                    _add_filtered(tar, pl.path,
                                                  os.path.join("plugins", pl.name))
                        elif entry.name == "services":
                            for svc in os.scandir(entry.path):
                                if svc.name in SERVICES_INCLUDE:
                                    _add_filtered(tar, svc.path,
                                                  os.path.join("services", svc.name))
                        else:
                            tar.add(entry.path, arcname=entry.name)

        ok(f"Archive prête ({os.path.getsize(tmp_path) // 1024} Ko)")

        # Pousser l'archive
        info("Transfert vers le container…")
        run(["pct", "push", str(vmid), tmp_path, "/tmp/bobistudio.tar.gz"])
        ok("Archive transférée")

        # Extraire
        pct_exec(vmid, "tar", "xzf", "/tmp/bobistudio.tar.gz", "-C", "/opt/bobistudio")
        pct_exec(vmid, "rm", "/tmp/bobistudio.tar.gz")
        ok("Code extrait dans /opt/bobistudio")

    finally:
        os.unlink(tmp_path)

    # Écrire config_local.py
    info("Écriture de config_local.py…")
    config_content = textwrap.dedent(f"""\
        PROXMOX_HOST  = "{cfg['host']}"
        PROXMOX_NODE  = "{cfg['node']}"
        PROXMOX_TOKEN = "{cfg['proxmox_token']}"
        PROXMOX_URL   = f"https://{{PROXMOX_HOST}}:8006/api2/json"
        STORAGE       = "{cfg['storage']}"
        TEMPLATE_VMID = 1001
        NET_BRIDGE    = "{vm_cfg['bridge']}"
        NET_VLAN_TAG  = {vm_cfg['vlan_tag']}
    """)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
        tmp.write(config_content)
        tmp_path = tmp.name
    try:
        run(["pct", "push", str(vmid), tmp_path, "/opt/bobistudio/config_local.py"])
    finally:
        os.unlink(tmp_path)
    ok("config_local.py écrit")

    # ── Clé SSH container → hôte Proxmox ──────────────────────────────────────
    # L'orchestrateur exécute `ssh root@<host>` pour les bind mounts /dev/shm,
    # l'inventaire NIC, PTP, la recréation du template. On génère la clé DANS le
    # container et on autorise sa clé publique sur l'hôte (l'installeur tourne sur
    # le nœud, accès local à /root/.ssh).
    info("Configuration de la clé SSH container → hôte…")
    # 1. Générer la clé dans le container si absente (idempotent)
    pct_exec(vmid, "mkdir", "-p", "/root/.ssh")
    pct_exec(vmid, "sh", "-c",
             "test -f /root/.ssh/id_ed25519 || "
             "ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519 -q")
    # 2. Récupérer la clé publique du container
    rc, pubkey = pct_exec(vmid, "cat", "/root/.ssh/id_ed25519.pub", capture=True)
    pubkey = (pubkey or "").strip()
    if rc != 0 or not pubkey:
        warn("Impossible de lire la clé publique du container — SSH à configurer manuellement.")
        return
    # 3. Autoriser la clé sur l'hôte Proxmox (local) — idempotent. Perms strictes :
    #    sshd (StrictModes) refuse silencieusement la clé si .ssh ≠ 700 / authorized_keys ≠ 600.
    auth = "/root/.ssh/authorized_keys"
    os.makedirs("/root/.ssh", mode=0o700, exist_ok=True)
    os.chmod("/root/.ssh", 0o700)
    existing = ""
    if os.path.exists(auth):
        with open(auth) as f:
            existing = f.read()
    if pubkey not in existing:
        with open(auth, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(pubkey + "\n")
        ok("Clé publique du container autorisée sur l'hôte")
    else:
        ok("Clé publique déjà autorisée sur l'hôte")
    os.chmod(auth, 0o600)
    # 4. Test : ssh container → hôte (accept-new pour enregistrer le known_hosts).
    #    Échec = bloquant fonctionnellement (recréation template, NIC, PTP cassés) :
    #    on le signale fort et on mémorise pour le répéter dans le récap final.
    test_cmd = (f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                f"-o ConnectTimeout=5 root@{cfg['host']} true")
    rc, _ = pct_exec(vmid, "sh", "-c", test_cmd, capture=True)
    if rc == 0:
        ok(f"SSH container → {cfg['host']} fonctionnel")
        vm_cfg["ssh_ok"] = True
    else:
        vm_cfg["ssh_ok"] = False
        warn(f"SSH container → hôte NON fonctionnel (rc={rc}).")
        info("Causes possibles : sshd PermitRootLogin (doit autoriser la clé), "
             "pare-feu, ou host injoignable depuis le VLAN du container.")
        info(f"Réparer depuis le container : pct exec {vmid} -- "
             f"ssh-copy-id root@{cfg['host']}")

def _check_internet(vmid):
    """Vérifie qu'Internet est joignable DEPUIS le container avant tout apt.
    Retourne (ok, raison). Distingue panne réseau, DNS et HTTP(S) sortant."""
    # 1. Route/IP sortante : ping de la gateway puis d'une IP publique (DNS Google).
    rc, _ = pct_exec(vmid, "sh", "-c",
                     "ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 || ping -c1 -W2 8.8.8.8 >/dev/null 2>&1",
                     capture=True)
    if rc != 0:
        return False, "pas de connectivité IP sortante (gateway/route ?)"
    # 2. Résolution DNS.
    rc, _ = pct_exec(vmid, "sh", "-c",
                     "getent hosts deb.debian.org >/dev/null 2>&1",
                     capture=True)
    if rc != 0:
        return False, "résolution DNS impossible (deb.debian.org) — vérifier /etc/resolv.conf"
    # 3. Accès HTTP(S) sortant vers les dépôts (proxy/pare-feu).
    rc, _ = pct_exec(vmid, "sh", "-c",
                     "(command -v curl >/dev/null && curl -fsS --max-time 8 -o /dev/null "
                     "http://deb.debian.org/debian/dists/stable/Release) "
                     "|| (command -v wget >/dev/null && wget -q -T8 -O /dev/null "
                     "http://deb.debian.org/debian/dists/stable/Release)",
                     capture=True)
    # curl/wget peuvent être absents sur un template minimal → on ne bloque que si présents et KO.
    if rc != 0:
        rc2, _ = pct_exec(vmid, "sh", "-c", "command -v curl >/dev/null || command -v wget >/dev/null",
                          capture=True)
        if rc2 == 0:
            return False, "dépôts Debian injoignables en HTTP (proxy/pare-feu sortant ?)"
    return True, ""


def step5_install_deps(vm_cfg):
    print_step(5, 7, "Installation des dépendances")
    vmid = vm_cfg["vmid"]

    info("Vérification de la connectivité Internet du container…")
    net_ok, net_msg = _check_internet(vmid)
    if net_ok:
        ok("Internet joignable depuis le container")
    else:
        err(f"Connectivité Internet absente : {net_msg}")
        info("L'installation des dépendances (apt, pip, binaires média) nécessite Internet.")
        info(f"Diagnostiquer : pct exec {vmid} -- ping 1.1.1.1 ; "
             f"pct exec {vmid} -- cat /etc/resolv.conf")
        info("Vérifier la gateway/VLAN du container et l'accès sortant, puis relancer.")
        bail("Pas d'accès Internet depuis le container.")

    info("apt-get update…")
    rc = pct_exec(vmid, "apt-get", "update", "-qq", stream=True)
    if rc != 0:
        bail("apt-get update a échoué.")

    info("Installation Python + paquets système…")
    rc = pct_exec(vmid,
        "env", "DEBIAN_FRONTEND=noninteractive",
        "apt-get", "install", "-y", "--no-install-recommends",
        "python3", "python3-venv", "python3-pip",
        "ffmpeg", "rsync", "curl",
        "cifs-utils", "nfs-common",   # montage des partages externes (Gestionnaire de Médias)
        stream=True)
    if rc != 0:
        bail("apt-get install a échoué.")
    ok("Paquets système installés")

    info("Création du venv Python…")
    rc = pct_exec(vmid, "python3", "-m", "venv", "/opt/bobistudio/venv", stream=True)
    if rc != 0:
        bail("Création du venv échouée.")

    info("pip install requirements.txt…")
    rc = pct_exec(vmid,
        "/opt/bobistudio/venv/bin/pip", "install", "--quiet", "--upgrade", "pip",
        stream=True)
    rc = pct_exec(vmid,
        "/opt/bobistudio/venv/bin/pip", "install", "--quiet",
        "-r", "/opt/bobistudio/requirements.txt",
        stream=True)
    if rc != 0:
        bail("pip install a échoué.")
    ok("Dépendances Python installées")

    info("Initialisation de la base de données…")
    rc = pct_exec(vmid,
        "/opt/bobistudio/venv/bin/python", "-c",
        "import sys; sys.path.insert(0,'/opt/bobistudio'); from app.database import init_db; init_db()",
        stream=True)
    if rc != 0:
        bail("init_db() a échoué.")
    ok("Base de données initialisée")

def step6_service(vm_cfg, src):
    print_step(6, 7, "Service systemd")
    vmid = vm_cfg["vmid"]
    src_type, src_path = src

    # Extraire bobistudio.service depuis le zip ou le répertoire
    if src_type == "zip":
        with tempfile.NamedTemporaryFile(suffix=".service", delete=False) as tmp:
            service_tmp = tmp.name
        try:
            with zipfile.ZipFile(src_path) as zf:
                with zf.open("bobistudio.service") as f:
                    with open(service_tmp, "wb") as out:
                        out.write(f.read())
            service_src = service_tmp
        except KeyError:
            bail("bobistudio.service absent du zip")
    else:
        service_src = os.path.join(src_path, "bobistudio.service")
        service_tmp = None
        if not os.path.exists(service_src):
            bail(f"bobistudio.service introuvable dans {src_path}")

    try:
        run(["pct", "push", str(vmid), service_src, "/etc/systemd/system/bobistudio.service"])
    finally:
        if src_type == "zip" and service_tmp:
            os.unlink(service_tmp)
    ok("Fichier service copié")

    pct_exec(vmid, "systemctl", "daemon-reload")
    pct_exec(vmid, "systemctl", "enable", "bobistudio")
    ok("Service activé (enable)")

    pct_exec(vmid, "systemctl", "start", "bobistudio")
    ok("Service démarré")

    # Vérification
    time.sleep(3)
    rc, state = pct_exec(vmid, "systemctl", "is-active", "bobistudio",
                         capture=True)
    if state.strip() == "active":
        ok(f"Service bobistudio : {GREEN}active{R}")
    else:
        warn(f"Service bobistudio : {YELLOW}{state.strip()}{R} (vérifier les logs)")
        info(f"  pct exec {vmid} -- journalctl -u bobistudio -n 30")

def step7_template():
    print_step(7, 7, "Template LXC (optionnel)")
    info("Le template LXC (agent pré-installé) peut être créé depuis l'interface web.")
    info("Une fois l'app démarrée :")
    info("  Réglages → onglet Système → Recréer le template")
    print()
    ok("Étape ignorée — à faire depuis l'UI")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print_header()

    # Auto-détection du nom du nœud
    _, node_auto = run(["hostname", "-s"], capture=True)
    node_auto = node_auto or "pve"

    try:
        src = step1_prerequisites()
        print_done("Prérequis OK")

        cfg = step2_proxmox_config(node_auto)
        print_done("Configuration Proxmox OK")

        vm_cfg = step3_create_vm(cfg, src)
        print_done(f"VM {vm_cfg['vmid']} créée ({vm_cfg['hostname']})")

        step4_deploy_code(cfg, vm_cfg, src)
        print_done("Code déployé")

        step5_install_deps(vm_cfg)
        print_done("Dépendances installées")

        step6_service(vm_cfg, src)
        print_done("Service systemd configuré")

        step7_template()

    except KeyboardInterrupt:
        print()
        bail("Installation interrompue.")

    # Récap final
    print()
    print(f"  {BOLD}{GREEN}╔{'═' * 50}╗{R}")
    print(f"  {BOLD}{GREEN}║{'Installation terminée !':^50}║{R}")
    print(f"  {BOLD}{GREEN}╚{'═' * 50}╝{R}")
    print()
    ip = vm_cfg['vm_ip'].split('/')[0]
    info(f"Bobi.Studio accessible sur : {BOLD}{CYAN}http://{ip}:5000{R}")
    info("Premier accès : la page de bienvenue vous invitera à créer le compte administrateur.")
    info(f"Logs : pct exec {vm_cfg['vmid']} -- journalctl -u bobistudio -f")
    info(f"Shell : pct enter {vm_cfg['vmid']}")
    print()
    warn("Si une DB existante a été importée, redémarrez l'orchestrateur pour")
    warn("appliquer les migrations (renommages de type, etc.) :")
    info(f"  pct exec {vm_cfg['vmid']} -- systemctl restart bobistudio")
    print()
    if vm_cfg.get("ssh_ok") is False:
        warn("⚠ SSH container → hôte NON fonctionnel : la recréation du template LXC")
        warn("  (Réglages → Système) et les opérations 2110/PTP échoueront. Réparez avec :")
        info(f"  pct exec {vm_cfg['vmid']} -- ssh-copy-id root@{cfg['host']}")
        print()

if __name__ == "__main__":
    if os.geteuid() != 0:
        print(f"\n  {RED}Ce script doit être exécuté en root.{R}\n")
        sys.exit(1)
    main()
