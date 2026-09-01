#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Bobi.Studio — Installeur unifié (sans Proxmox)

Point d'entrée unique de la commande « Déploiement → Nouveau serveur ». Demande la langue puis
présente un menu à navigation clavier (curseur ↑/↓, retour ←, cases à cocher pour les capacités),
et provisionne SUR CETTE MACHINE l'un de :
  1) Nœud de process (bobi-node-agent seul, bare Debian)
  2) Orchestrateur (contrôleur Flask :5000)
  3) Tout-en-un (orchestrateur + nœud local, « collapse 1-box »)
  4) Orchestrateur sur une VM Proxmox (chemin legacy → install_proxmox.main())

Stdlib uniquement (tourne sur une box nue avant tout venv). Navigation clavier en mode brut
(termios/tty) avec repli en saisie numérotée si stdin/stdout n'est pas un terminal.
Réutilise le flux Proxmox de `install_proxmox.py`. Cf. NODE_AGENT.md (§9b), node_agent/REINSTALL.md.
"""
import glob
import json
import os
import re
import secrets
import select
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
import zipfile

try:
    import install_proxmox as ip
except ImportError:
    print("install_proxmox.py introuvable à côté de install.py — installeur incomplet.", file=sys.stderr)
    sys.exit(1)

run, run_stream = ip.run, ip.run_stream
R, BOLD, DIM = ip.R, ip.BOLD, ip.DIM
GREEN, RED, YELLOW, CYAN, WHITE = ip.GREEN, ip.RED, ip.YELLOW, ip.CYAN, ip.WHITE
REVERSE = "\033[7m"

APP_DIR = "/opt/bobistudio"
NODE_SRC = "/opt/bobi-node-src"
BACK = object()   # sentinelle « revenir en arrière »

# ─── i18n minimal de l'installeur (stdlib, pas de dépendance) ────────────────
LANG = "fr"
STR = {
    "fr": {
        "installer": "Installeur",
        "default": "défaut", "choice": "Choix", "invalid_choice": "Choix invalide.",
        "cancelled": "Installation annulée.",
        "nav_single": "↑/↓ déplacer · Entrée valider · ← retour",
        "nav_single_top": "↑/↓ déplacer · Entrée valider",
        "nav_multi": "↑/↓ déplacer · Espace cocher · Entrée valider · ← retour",
        "nav_input": "Entrée valider · ⌫ effacer",
        "nav_input_back": "Entrée valider · ⌫ effacer · ← retour",
        "back_hint": "(« < » pour revenir)", "yes": "Oui", "no": "Non",
        "lang_title": "Langue · Language",
        "menu_title": "Que voulez-vous installer sur cette machine ?",
        "o_node_l": "Nœud de process",
        "o_node_d": "Agent seul — exécute les traitements vidéo (calcul, 2110, média…).",
        "o_ctrl_l": "Orchestrateur",
        "o_ctrl_d": "Interface de contrôle web (port 5000) qui pilote la flotte.",
        "o_all_l": "Tout-en-un",
        "o_all_d": "Orchestrateur + nœud de process sur la même machine.",
        "o_pve_l": "Orchestrateur sur une VM Proxmox",
        "o_pve_d": "Déploiement via hyperviseur Proxmox (avancé, hérité).",
        "o_del_l": "Désinstaller",
        "o_del_d": "Retirer Bobi.Studio de CETTE machine (nœud et/ou orchestrateur).",
        "del_title": "Que faut-il retirer de cette machine ?",
        "del_node_l": "Le nœud de process",
        "del_node_d": "Agent, services, conteneurs, réseau et réglages hôte posés par l'installation.",
        "del_ctrl_l": "L'orchestrateur",
        "del_ctrl_d": "Application, service web, base — les données sont archivées dans /root.",
        "del_both_l": "Les deux (machine tout-en-un)",
        "del_both_d": "Retire l'orchestrateur ET l'agent-nœud de cette machine.",
        "del_hdr": "Retrait de Bobi.Studio",
        "del_warn_nodes": "Les NŒUDS déjà enrôlés ne sont pas touchés : leurs conteneurs continueront de tourner, et la liste de ces nœuds disparaît avec la base. Les retirer AVANT si toute l'installation part au rebut.",
        "del_dryrun": "Faire d'abord un inventaire (sans rien modifier) ?",
        "del_archive": "Conserver une archive des données (base, sauvegardes, certificats) dans /root ?",
        "del_images": "Supprimer aussi les images Docker bobi-* (plusieurs Go) ?",
        "del_media": "Supprimer aussi les MÉDIAS stockés sur cette machine (irréversible) ?",
        "del_confirm_hint": "Le script demandera de taper RETIRER pour confirmer.",
        "e_delscript": "script de désinstallation introuvable (%s) : ni dans l'archive téléchargée, ni dans /opt/bobistudio/node_agent. Reconstruire le paquet de distribution (Réglages → Déploiement) puis relancer.",
        "del_fallback": "absent de l'archive — on utilise la copie installée : {d}",
        "del_done": "Retrait terminé.",
        "del_failed": "Le retrait a signalé une erreur — relire la sortie ci-dessus.",
        # nœud
        "h_node": "Nœud de process",
        "caps_title": "Capacités du nœud — cochez avec Espace",
        "cap_compute_d": "Traitements numpy (mixer, multiview, UDC, correcteur…).",
        "cap_io2110_d": "Entrées/sorties ST 2110 (carte E810).",
        "cap_media_d": "Lecture / enregistrement / transcodage / stills.",
        "cap_webrtc_d": "Passerelle WebRTC (MediaMTX).",
        "need_one": "Sélectionnez au moins une capacité.",
        "ctrl_url": "Adresse du contrôleur — IP ou hôte (ex. x.x.x.x)",
        "enroll_tok": "Jeton d'enrôlement (web → Réglages → Déploiement → Nœuds → « Jeton d'enrôlement »)",
        "enroll_help": "Le nœud va s'annoncer à ce contrôleur. Génère le jeton en 1 clic dans le web "
                       "(carte « Jeton d'enrôlement ») et colle-le ici.",
        "enroll_err": "Annonce au contrôleur échouée : {e}",
        "enroll_fail": "Annonce impossible — vérifie l'adresse/le jeton, puis relance.",
        "h_node_install": "Installation du nœud (bobi-node-agent)",
        "node_ready": "Nœud installé et annoncé au contrôleur.",
        "node_register": "→ Le nœud apparaît tout seul dans le web (Monitoring → Serveurs / "
                         "Réglages → Déploiement → Nœuds). Rien d'autre à saisir.",
        "io2110_pending": "io2110 : choisis la carte E810 et active le PTP depuis l'orchestrateur "
                          "(Réglages → Déploiement → Nœuds) une fois le nœud enrôlé.",
        # contrôleur
        "h_ctrl": "Orchestrateur (contrôleur Flask)",
        "ctrl_exists": "{d} contient déjà du code — mettre à jour (config/DB conservées) ?",
        "code_deployed": "Code déployé dans {d}",
        "ctrl_ready": "Orchestrateur prêt.",
        "access": "Accès : {url}  (créer le compte admin à la 1ʳᵉ visite)",
        "aio_ready": "Tout-en-un prêt (orchestrateur + nœud local).",
        # deps / service
        "h_deps": "Dépendances (Python, venv, paquets système)",
        "apt_update": "apt-get update…", "apt_install": "Installation des paquets système…",
        "apt_offline": "Installation des paquets système (bundle hors-ligne)…",
        "pkgs_ok": "Paquets système installés",
        "venv": "Création du venv Python…", "pip": "pip install requirements.txt…",
        "pip_offline": "pip install (roues embarquées, hors-ligne)…",
        "pydeps_ok": "Dépendances Python installées",
        "db_init": "Initialisation de la base de données…", "db_ok": "Base de données initialisée",
        "h_svc": "Service systemd (bobistudio)", "svc_copied": "Fichier service copié",
        "svc_active": "Service bobistudio : actif",
        "svc_bad": "Service bobistudio : {s} — voir : journalctl -u bobistudio -n 30",
        "cfg_kept": "config_local.py déjà présent — conservé",
        "cfg_written": "config_local.py écrit (sans Proxmox)",
        "agent_local_ok": "Agent local enregistré (127.0.0.1:9100)",
        "agent_local_bad": "Agent local à enregistrer depuis l'UI (Réglages → Déploiement → Nœuds).",
        # erreurs
        "e_aptupdate": "apt-get update a échoué.", "e_aptinstall": "apt-get install a échoué.",
        "e_venv": "Création du venv échouée.", "e_pip": "pip install a échoué.",
        "e_initdb": "init_db() a échoué.", "e_zip": "bobistudio.zip introuvable à côté de l'installeur ({d}).",
        "e_zipbad": "bobistudio.zip invalide (main.py absent).", "e_svc": "bobistudio.service introuvable dans {d}",
        "h_clock": "Horloge (grille TAI)",
        "clock_ok": "chrony actif, tai_offset=37 — horloge sur la grille TAI.",
        "clock_pending": ("tai_offset={v} au lieu de 37 : chrony ne l'a pas encore posé (il le fait "
                          "après sa première synchro). Revérifier dans Réglages → Réseau → Horloges ; "
                          "s'il reste à 0, vérifier le paquet tzdata."),
        "clock_nochrony": "chrony non installé — le contrôleur restera sur sa synchro actuelle.",
        "e_nonmos": ("source incomplète : services/nmos est absent ou vide. L'orchestrateur ne "
                     "démarrerait pas (main.py l'importe au démarrage), et l'erreur ne nommerait "
                     "pas la cause. Relancer get.sh, qui le récupère ; les autres services et les "
                     "plugins s'installent ensuite depuis la page Catalogue."),
        "e_installnode": "install-node.sh a échoué.", "e_nonode": "node_agent/install-node.sh absent de l'archive.",
        "must_root": "Ce script doit être exécuté en root.",
    },
    "en": {
        "installer": "Installer",
        "default": "default", "choice": "Choice", "invalid_choice": "Invalid choice.",
        "cancelled": "Installation cancelled.",
        "nav_single": "↑/↓ move · Enter select · ← back",
        "nav_single_top": "↑/↓ move · Enter select",
        "nav_multi": "↑/↓ move · Space toggle · Enter confirm · ← back",
        "nav_input": "Enter confirm · ⌫ erase",
        "nav_input_back": "Enter confirm · ⌫ erase · ← back",
        "back_hint": "(\"<\" to go back)", "yes": "Yes", "no": "No",
        "lang_title": "Langue · Language",
        "menu_title": "What do you want to install on this machine?",
        "o_node_l": "Process node",
        "o_node_d": "Agent only — runs the video workloads (compute, 2110, media…).",
        "o_ctrl_l": "Orchestrator",
        "o_ctrl_d": "Web control interface (port 5000) that drives the fleet.",
        "o_all_l": "All-in-one",
        "o_all_d": "Orchestrator + process node on the same machine.",
        "o_pve_l": "Orchestrator on a Proxmox VM",
        "o_pve_d": "Deployment via the Proxmox hypervisor (advanced, legacy).",
        "o_del_l": "Uninstall",
        "o_del_d": "Remove Bobi.Studio from THIS machine (node and/or orchestrator).",
        "del_title": "What should be removed from this machine?",
        "del_node_l": "The processing node",
        "del_node_d": "Agent, services, containers, network and host settings laid down at install.",
        "del_ctrl_l": "The orchestrator",
        "del_ctrl_d": "Application, web service, database — data is archived to /root.",
        "del_both_l": "Both (all-in-one machine)",
        "del_both_d": "Removes the orchestrator AND the node agent from this machine.",
        "del_hdr": "Removing Bobi.Studio",
        "del_warn_nodes": "Already-enrolled NODES are untouched: their containers keep running, and the list of those nodes disappears with the database. Remove them FIRST if the whole install is being scrapped.",
        "del_dryrun": "Start with an inventory (changes nothing)?",
        "del_archive": "Keep an archive of the data (database, backups, certificates) in /root?",
        "del_images": "Also delete the bobi-* Docker images (several GB)?",
        "del_media": "Also delete the MEDIA stored on this machine (irreversible)?",
        "del_confirm_hint": "The script will ask you to type RETIRER to confirm.",
        "e_delscript": "uninstall script not found (%s): neither in the downloaded archive nor in /opt/bobistudio/node_agent. Rebuild the distribution package (Settings → Deployment) then retry.",
        "del_fallback": "missing from the archive — using the installed copy: {d}",
        "del_done": "Removal finished.",
        "del_failed": "The removal reported an error — read the output above.",
        "h_node": "Process node",
        "caps_title": "Node capabilities — toggle with Space",
        "cap_compute_d": "numpy workloads (mixer, multiview, UDC, corrector…).",
        "cap_io2110_d": "ST 2110 I/O (E810 NIC).",
        "cap_media_d": "Playback / recording / transcoding / stills.",
        "cap_webrtc_d": "WebRTC gateway (MediaMTX).",
        "need_one": "Select at least one capability.",
        "ctrl_url": "Controller address — IP or host (e.g. x.x.x.x)",
        "enroll_tok": "Enrollment token (web → Settings → Deployment → Nodes → \"Enrollment token\")",
        "enroll_help": "The node will announce itself to this controller. Generate the token in one "
                       "click in the web UI (\"Enrollment token\" card) and paste it here.",
        "enroll_err": "Announcing to controller failed: {e}",
        "enroll_fail": "Announce failed — check the address/token, then retry.",
        "h_node_install": "Node installation (bobi-node-agent)",
        "node_ready": "Node installed and announced to the controller.",
        "node_register": "→ The node shows up on its own in the web UI (Monitoring → Servers / "
                         "Settings → Deployment → Nodes). Nothing else to enter.",
        "io2110_pending": "io2110: pick the E810 card and enable PTP from the orchestrator "
                          "(Settings → Deployment → Nodes) once the node is enrolled.",
        "h_ctrl": "Orchestrator (Flask controller)",
        "ctrl_exists": "{d} already contains code — update it (config/DB kept)?",
        "code_deployed": "Code deployed to {d}",
        "ctrl_ready": "Orchestrator ready.",
        "access": "Access: {url}  (create the admin account on first visit)",
        "aio_ready": "All-in-one ready (orchestrator + local node).",
        "h_deps": "Dependencies (Python, venv, system packages)",
        "apt_update": "apt-get update…", "apt_install": "Installing system packages…",
        "apt_offline": "Installing system packages (offline bundle)…",
        "pkgs_ok": "System packages installed",
        "venv": "Creating the Python venv…", "pip": "pip install requirements.txt…",
        "pip_offline": "pip install (bundled wheels, offline)…",
        "pydeps_ok": "Python dependencies installed",
        "db_init": "Initializing the database…", "db_ok": "Database initialized",
        "h_svc": "systemd service (bobistudio)", "svc_copied": "Service file copied",
        "svc_active": "bobistudio service: active",
        "svc_bad": "bobistudio service: {s} — see: journalctl -u bobistudio -n 30",
        "cfg_kept": "config_local.py already present — kept",
        "cfg_written": "config_local.py written (no Proxmox)",
        "agent_local_ok": "Local agent registered (127.0.0.1:9100)",
        "agent_local_bad": "Register the local agent from the UI (Settings → Deployment → Nodes).",
        "e_aptupdate": "apt-get update failed.", "e_aptinstall": "apt-get install failed.",
        "e_venv": "venv creation failed.", "e_pip": "pip install failed.",
        "e_initdb": "init_db() failed.", "e_zip": "bobistudio.zip not found next to the installer ({d}).",
        "e_zipbad": "invalid bobistudio.zip (main.py missing).", "e_svc": "bobistudio.service not found in {d}",
        "h_clock": "Clock (TAI grid)",
        "clock_ok": "chrony running, tai_offset=37 — clock on the TAI grid.",
        "clock_pending": ("tai_offset={v} instead of 37: chrony has not set it yet (it does so after "
                          "its first sync). Re-check in Settings → Network → Clocks; if it stays at "
                          "0, check the tzdata package."),
        "clock_nochrony": "chrony not installed — the controller keeps its current time sync.",
        "e_nonmos": ("incomplete source: services/nmos is missing or empty. The orchestrator would "
                     "not start (main.py imports it at boot) and the error would not name the "
                     "cause. Re-run get.sh, which fetches it; other services and plugins are "
                     "installed afterwards from the Catalogue page."),
        "e_installnode": "install-node.sh failed.", "e_nonode": "node_agent/install-node.sh missing from the archive.",
        "must_root": "This script must be run as root.",
    },
}


def t(key, **kw):
    s = STR.get(LANG, STR["fr"]).get(key, STR["fr"].get(key, key))
    return s.format(**kw) if kw else s


def ok(m):   print(f"  {GREEN}✓{R}  {m}")
def info(m): print(f"  {CYAN}·{R}  {m}")
def warn(m): print(f"  {YELLOW}!{R}  {YELLOW}{m}{R}")
def err(m):  print(f"  {RED}✗{R}  {RED}{m}{R}")
def bail(m): print(); err(m); print(); sys.exit(1)


def _line(w=54): return "─" * w


def _banner():
    w = 54
    bar = "═" * w
    def center(s, color=WHITE):
        pad = (w - len(s)) // 2
        return f"  {CYAN}{BOLD}║{R}{' ' * pad}{color}{BOLD}{s}{R}{' ' * (w - pad - len(s))}{CYAN}{BOLD}║{R}"
    print()
    print(f"  {CYAN}{BOLD}╔{bar}╗{R}")
    print(center("B O B I . S T U D I O"))
    print(center(t("installer"), color=CYAN))
    print(f"  {CYAN}{BOLD}╚{bar}╝{R}")


# ─── Navigation clavier (mode brut termios/tty) ──────────────────────────────
def _interactive():
    try:
        import termios  # noqa: F401
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


class _Raw:
    """Met le terminal en mode brut le temps d'un menu + masque le curseur."""
    def __enter__(self):
        import termios, tty
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        sys.stdout.write("\033[?25l"); sys.stdout.flush()
        return self
    def __exit__(self, *a):
        import termios
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
        sys.stdout.write("\033[?25h"); sys.stdout.flush()


def _read_key():
    """Renvoie : up|down|left|right|enter|space|back|quit|other.
    Lit l'fd brut avec os.read (PAS sys.stdin.read : son tampon « avale » la séquence
    de flèche et fausse la détection ESC via select)."""
    fd = sys.stdin.fileno()
    ch = os.read(fd, 1)
    if not ch:
        return "quit"
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b" ":
        return "space"
    if ch in (b"\x03", b"\x04"):          # Ctrl-C / Ctrl-D
        return "quit"
    if ch in (b"\x7f", b"\x08"):          # Backspace = retour
        return "back"
    if ch == b"\x1b":                     # séquence d'échappement (flèches) ou ESC seul
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            return "back"
        seq = os.read(fd, 2)
        if seq[:1] in (b"[", b"O") and len(seq) == 1:   # 'A'..'D' arrivé en retard
            seq += os.read(fd, 1)
        return {b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left",
                b"OA": "up", b"OB": "down", b"OC": "right", b"OD": "left"}.get(seq, "other")
    if ch in (b"k", b"K"):
        return "up"
    if ch in (b"j", b"J"):
        return "down"
    return "other"


class _AltScreen:
    """Bascule sur l'écran alterné du terminal (plein écran) le temps d'un menu, puis
    restaure l'écran précédent à la sortie → le scrollback du shell n'est pas pollué."""
    def __enter__(self):
        sys.stdout.write("\033[?1049h\033[2J\033[H"); sys.stdout.flush()
        return self
    def __exit__(self, *a):
        sys.stdout.write("\033[?1049l"); sys.stdout.flush()


_ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")
def _vlen(s):  return len(_ANSI.sub("", s))                       # longueur visible (sans ANSI)
def _pad(s, w): return s + " " * max(0, w - _vlen(s))            # complète à droite
def _center(s, w):                                              # centre dans w colonnes
    return " " * max(0, (w - _vlen(s)) // 2) + s
def _trunc(s, w):                                               # tronque le texte brut (… si trop long)
    s = s or ""
    return s if len(s) <= w else s[:max(0, w - 1)] + "…"


def _term_size():
    sz = shutil.get_terminal_size((80, 24))
    return sz.columns, sz.lines


def _menu_lines(title, items, idx, checks=None, multi=False, allow_back=True, width=70):
    """Construit le corps du menu (sans le cadre). items=[(key,label,desc)]."""
    has_desc = any(d for _, _, d in items)
    iw = width - 2
    lines = [f"{BOLD}{title}{R}", ""]
    for i, (key, label, desc) in enumerate(items):
        cur = (i == idx)
        box = ""
        if multi:
            box = f"{GREEN}[x]{R} " if checks and checks[i] else f"{DIM}[ ]{R} "
        lab = _trunc(label, iw - 6)
        if cur:
            lines.append(f"{CYAN}{BOLD}❯{R} {box}{REVERSE}{BOLD} {lab} {R}")
        else:
            lines.append(f"  {box}{lab}")
        if has_desc:
            lines.append(f"    {DIM}{_trunc(desc or '', iw - 4)}{R}")
    hint = t("nav_multi") if multi else (t("nav_single") if allow_back else t("nav_single_top"))
    lines += ["", f"{DIM}{hint}{R}"]
    return lines


def _frame(body_lines, color=CYAN):
    """Dessine un panneau plein écran : bannière + corps, encadré et centré (H et V)."""
    cols, rows = _term_size()
    iw = min(max(cols - 6, 32), 74)                  # largeur intérieure du cadre
    banner = [
        "",
        _center(f"{WHITE}{BOLD}B O B I . S T U D I O{R}", iw - 2),
        _center(f"{CYAN}{t('installer')}{R}", iw - 2),
        "",
        f"{CYAN}{DIM}{'─' * (iw - 2)}{R}",
        "",
    ]
    content = banner + body_lines
    top = f"{color}{BOLD}╔{'═' * iw}╗{R}"
    bot = f"{color}{BOLD}╚{'═' * iw}╝{R}"
    boxed = [top] + [f"{color}{BOLD}║{R} {_pad(ln, iw - 2)} {color}{BOLD}║{R}" for ln in content] + [bot]
    left = max(0, (cols - (iw + 2)) // 2)
    vtop = max(0, (rows - len(boxed)) // 2)
    buf = "\033[2J\033[H" + "\r\n" * vtop + "".join((" " * left) + ln + "\r\n" for ln in boxed)
    sys.stdout.write(buf); sys.stdout.flush()


def _panel_width():
    cols, _ = _term_size()
    return min(max(cols - 6, 32), 74)


def menu_select(title, items, default=0, allow_back=True):
    """Sélection unique au curseur, panneau plein écran. Renvoie la clé, ou BACK. Repli numéroté hors TTY."""
    if not _interactive():
        return _select_fallback(title, items, default, allow_back)
    idx = default
    with _AltScreen(), _Raw():
        while True:
            _frame(_menu_lines(title, items, idx, allow_back=allow_back, width=_panel_width()))
            k = _read_key()
            if k == "up":
                idx = (idx - 1) % len(items)
            elif k == "down":
                idx = (idx + 1) % len(items)
            elif k == "enter":
                return items[idx][0]
            elif k in ("back", "left") and allow_back:
                return BACK
            elif k == "quit":
                break
    print(); bail(t("cancelled"))


def menu_multi(title, items, preselected=None, allow_back=True):
    """Cases à cocher, panneau plein écran. Renvoie la liste des clés cochées, ou BACK. Repli CSV hors TTY."""
    if not _interactive():
        return _multi_fallback(title, items, preselected, allow_back)
    pre = set(preselected or [])
    checks = [it[0] in pre for it in items]
    idx = 0
    with _AltScreen(), _Raw():
        while True:
            _frame(_menu_lines(title, items, idx, checks=checks, multi=True, width=_panel_width()))
            k = _read_key()
            if k == "up":
                idx = (idx - 1) % len(items)
            elif k == "down":
                idx = (idx + 1) % len(items)
            elif k == "space":
                checks[idx] = not checks[idx]
            elif k == "enter":
                sel = [items[i][0] for i in range(len(items)) if checks[i]]
                if sel:
                    return sel
            elif k in ("back", "left") and allow_back:
                return BACK
            elif k == "quit":
                break
    print(); bail(t("cancelled"))


# Replis hors-TTY (sortie tuyautée / pas de terminal) ─────────────────────────
def _select_fallback(title, items, default, allow_back):
    print(f"\n  {BOLD}{title}{R}")
    for i, (key, label, desc) in enumerate(items):
        mark = f"  {DIM}({t('default')}){R}" if i == default else ""
        print(f"   {CYAN}{i+1}{R}  {label}{mark}")
    while True:
        raw = ask(t("choice"), default=str(default + 1), allow_back=allow_back)
        if raw is BACK:
            return BACK
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1][0]
        warn(t("invalid_choice"))


def _multi_fallback(title, items, preselected, allow_back):
    print(f"\n  {BOLD}{title}{R}")
    for key, label, desc in items:
        print(f"   · {label}  {DIM}{desc or ''}{R}")
    raw = ask(title, default=",".join(preselected or []), allow_back=allow_back)
    if raw is BACK:
        return BACK
    keys = {it[0] for it in items}
    sel = [c.strip() for c in raw.split(",") if c.strip() in keys]
    return sel or list(preselected or [])


def _utf8_pop(b):
    """Retire le dernier caractère UTF-8 du buffer d'octets (gère le multioctet)."""
    if not b:
        return b
    i = len(b) - 1
    while i > 0 and (b[i] & 0xC0) == 0x80:
        i -= 1
    return b[:i]


def _input_lines(prompt, buf, default, secret, allow_back, help=None, width=70):
    """Corps du panneau de saisie : prompt (replié) + aide + champ avec curseur."""
    iw = width - 2
    lines = [f"{BOLD}{pl}{R}" for pl in (textwrap.wrap(prompt, iw - 2) or [""])]
    if help:
        lines.append("")
        lines += [f"{DIM}{hl}{R}" for hl in textwrap.wrap(help, iw - 2)]
    lines.append("")
    text = buf.decode("utf-8", "ignore")
    if text:
        field = _trunc("•" * len(text) if secret else text, iw - 4)
    elif default is not None:
        field = f"{DIM}{_trunc(str(default), iw - 4)}{R}"
    else:
        field = ""
    lines.append(f"{CYAN}❯{R} {field}{REVERSE} {R}")          # bloc inversé = faux curseur
    lines += ["", f"{DIM}{t('nav_input_back') if allow_back else t('nav_input')}{R}"]
    return lines


def ask(prompt, default=None, secret=False, required=True, allow_back=False, help=None):
    """Saisie texte plein écran (mode brut). ← / Échap → BACK si allow_back. Repli ligne hors TTY."""
    if not _interactive():
        return _ask_fallback(prompt, default, secret, required, allow_back)
    fd = sys.stdin.fileno()
    buf = b""
    with _AltScreen(), _Raw():
        while True:
            _frame(_input_lines(prompt, buf, default, secret, allow_back, help, width=_panel_width()))
            ch = os.read(fd, 1)
            if not ch or ch in (b"\x03", b"\x04"):            # EOF / Ctrl-C / Ctrl-D
                break
            if ch in (b"\r", b"\n"):
                val = buf.decode("utf-8", "ignore").strip()
                if val:
                    return val
                if default is not None:
                    return default
                if not required:
                    return ""
                continue
            if ch in (b"\x7f", b"\x08"):                      # Backspace
                buf = _utf8_pop(buf)
            elif ch == b"\x15":                               # Ctrl-U : efface la ligne
                buf = b""
            elif ch == b"\x1b":                               # Échap seul = retour ; flèches ignorées (← = retour)
                r, _, _ = select.select([fd], [], [], 0.05)
                if not r:
                    if allow_back:
                        return BACK
                    continue
                seq = os.read(fd, 2)
                if seq == b"[D" and allow_back:               # flèche gauche
                    return BACK
            elif ch >= b"\x20":                               # caractère imprimable / octet UTF-8
                buf += ch
    print(); bail(t("cancelled"))


def _ask_fallback(prompt, default, secret, required, allow_back):
    """Saisie ligne (sortie tuyautée / pas de terminal). « < » → BACK si allow_back."""
    bh = f" {DIM}{t('back_hint')}{R}" if allow_back else ""
    hint = f" {DIM}[{default}]{R}" if default is not None else ""
    while True:
        try:
            line = f"  {BOLD}{prompt}{R}{hint}{bh} {CYAN}❯{R} "
            val = (__import__("getpass").getpass(line) if secret else input(line)).strip()
        except (EOFError, KeyboardInterrupt):
            print(); bail(t("cancelled"))
        if allow_back and val == "<":
            return BACK
        if not val:
            if default is not None:
                return default
            if not required:
                return ""
            continue
        return val


def ask_yn_menu(prompt, default=False, allow_back=True):
    """Oui/Non au curseur (réutilise menu_select). Renvoie True/False ou BACK."""
    sel = menu_select(prompt, [(True, t("yes"), None), (False, t("no"), None)],
                      default=0 if default else 1, allow_back=allow_back)
    return sel


# ─── Archive / système ───────────────────────────────────────────────────────
# Deux sources possibles, et une seule notion en aval : la SOURCE. Historiquement c'était un zip
# (paquet de distribution, servi par un orchestrateur déjà installé). Depuis `get.sh`, ce peut être
# un ARBRE déjà déplié — les archives récupérées depuis GitHub par simple curl, sans git ni zip.
# On ne duplique pas le chemin d'installation pour autant : tout ce qui suit manipule « la source »
# et ne sait pas d'où elle vient.
def _find_source():
    """(kind, chemin) — 'zip' si un bobistudio.zip est à côté de l'installeur, sinon 'tree' si
    l'installeur vit DANS un arbre source complet. Sort en erreur sinon.

    Deux dispositions, et c'est voulu :
      · TÉLÉCHARGÉ — install.py atterrit dans un dossier temporaire avec le zip à côté de lui
        (chemin servi par un orchestrateur : /install/install.py + /install/bobistudio.zip) ;
      · ARBRE — install.py vit dans `install/` d'une source dépliée (chemin get.sh), et la racine
        du produit est donc le dossier PARENT."""
    here = os.path.dirname(os.path.abspath(__file__))
    z = os.path.join(here, "bobistudio.zip")
    if os.path.isfile(z):
        with zipfile.ZipFile(z) as zf:
            if not any(n == "main.py" or n.endswith("/main.py") for n in zf.namelist()):
                bail(t("e_zipbad"))
        return ("zip", z)
    # Les trois marqueurs qui distinguent une source complète d'un dossier quelconque. On regarde
    # le parent d'abord (disposition normale), puis `here` (installeur posé à la racine, hérité).
    for racine in (os.path.dirname(here), here):
        if all(os.path.exists(os.path.join(racine, x))
               for x in ("main.py", "app", os.path.join("node_agent", "install-node.sh"))):
            return ("tree", racine)
    bail(t("e_zip", d=here))


# Ce qui ne doit JAMAIS voyager vers /opt/bobistudio : l'historique git (lourd, et il porte des
# identités), le venv de la machine d'origine (chemins absolus figés → venv cassé), les artefacts
# de build et les caches. La base et la conf de site n'y sont pas non plus : elles appartiennent à
# la machine cible, pas à la source.
_EXCLUS_ARBRE = {".git", ".github", "venv", "dist", "__pycache__", ".claude", "node_iso_cache",
                 "pxe_root", "backups", "config_local.py", "db_bobistudio.db"}


def _extract(src, dest):
    """Déploie la source dans `dest`. `src` = zip ou arbre (cf. _find_source)."""
    os.makedirs(dest, exist_ok=True)
    if os.path.isfile(src):
        with zipfile.ZipFile(src) as zf:
            zf.extractall(dest)
        return
    if os.path.abspath(src) == os.path.abspath(dest):
        return                      # déjà en place (arbre déplié directement dans la destination)
    for nom in os.listdir(src):
        if nom in _EXCLUS_ARBRE or nom.endswith(".pyc"):
            continue
        s_, d_ = os.path.join(src, nom), os.path.join(dest, nom)
        if os.path.isdir(s_):
            shutil.copytree(s_, d_, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        else:
            shutil.copy2(s_, d_)


def _host_ip():
    rc, out = run(["hostname", "-I"], check=False, capture=True)
    return (out or "").split()[0] if out else None


def _normalize_url(u):
    """Tolère une adresse saisie à la main : ajoute http:// si pas de schéma, :5000 si pas de port."""
    from urllib.parse import urlsplit, urlunsplit
    u = (u or "").strip().rstrip("/")
    if "://" not in u:
        u = "http://" + u
    p = urlsplit(u)
    netloc = p.netloc if ":" in p.netloc else p.netloc + ":5000"
    return urlunsplit((p.scheme, netloc, p.path, "", ""))


def _detect_ifaces():
    """(ice_iface, mgmt_iface) — carte ice (E810) + iface de route par défaut (cf. node-bootstrap.sh)."""
    ice = None
    try:
        for i in sorted(os.listdir("/sys/class/net")):
            if i == "lo":
                continue
            rc, drv = run(["ethtool", "-i", i], check=False, capture=True)
            if rc == 0 and "driver: ice" in (drv or ""):
                ice = i
                break
    except OSError:
        pass
    rc, out = run(["ip", "route", "show", "default"], check=False, capture=True)
    mgmt = None
    if rc == 0 and out and "dev" in out.split():
        parts = out.split()
        mgmt = parts[parts.index("dev") + 1]
    return ice, mgmt


def _hdr(title):
    print()
    print(f"  {CYAN}{_line()}{R}")
    print(f"  {BOLD}{CYAN}{title}{R}")
    print(f"  {CYAN}{_line()}{R}")


# ─── Étapes bare (transposition de install_proxmox.step5/step6, sans pct_exec) ───
# Paquets système (miroir de app/offline_bundle.APT_PACKAGES + python3 de base).
APT_PACKAGES = ["python3", "python3-venv", "python3-pip",
                "ffmpeg", "rsync", "curl", "cifs-utils", "nfs-common"]


def _install_debs_offline(debs_dir):
    """Installe les paquets système depuis un dépôt apt local file:// (bundle hors-ligne).

    apt résout depuis ce seul dépôt SANS réseau (`--no-download`) : ce qui est déjà satisfait
    sur la cible n'est pas touché (zéro downgrade), le reste vient des .deb embarqués. Le fichier
    sources temporaire évite de polluer /etc/apt. Retourne True si succès.
    """
    debs_dir = os.path.abspath(debs_dir)
    srcfile = os.path.join(tempfile.gettempdir(), "bobi-offline.list")
    with open(srcfile, "w") as f:
        f.write(f"deb [trusted=yes] file://{debs_dir} ./\n")
    apt_opts = ["-o", f"Dir::Etc::sourcelist={srcfile}",
                "-o", "Dir::Etc::sourceparts=-",
                "-o", "APT::Get::List-Cleanup=0"]
    try:
        # Indexer UNIQUEMENT le dépôt local : aucune source réseau n'est connue de ces commandes,
        # donc l'install est hors-ligne de fait (une dép absente → erreur nette, jamais de réseau).
        # NE PAS passer --no-download : sur un dépôt file://, il fait passer un chemin RELATIF à
        # dpkg (« Pathname to install is not absolute ») quand le .deb n'est pas déjà en cache.
        # Sans le flag, apt recopie le .deb localement (pas de réseau) et l'installe correctement.
        run(["apt-get"] + apt_opts + ["update"], check=False)
        rc = run_stream(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get"] + apt_opts
                        + ["install", "-y", "--no-install-recommends"]
                        + APT_PACKAGES)
        return rc == 0
    finally:
        try:
            os.remove(srcfile)
        except OSError:
            pass


def install_deps_bare(dest):
    _hdr(t("h_deps"))
    wheels_dir = os.path.join(dest, "vendor", "wheels")
    debs_dir = os.path.join(dest, "vendor", "debs")
    offline_wheels = glob.glob(os.path.join(wheels_dir, "*.whl"))
    offline_debs = glob.glob(os.path.join(debs_dir, "*.deb"))

    # ── Paquets système : bundle hors-ligne si présent, sinon apt en ligne ──
    if offline_debs:
        info(t("apt_offline"))
        if not _install_debs_offline(debs_dir):
            bail(t("e_aptinstall"))
    else:
        info(t("apt_update"))
        if run(["apt-get", "update", "-qq"], check=False)[0] != 0:
            bail(t("e_aptupdate"))
        info(t("apt_install"))
        if run_stream(["env", "DEBIAN_FRONTEND=noninteractive",
                       "apt-get", "install", "-y", "--no-install-recommends"] + APT_PACKAGES) != 0:
            bail(t("e_aptinstall"))
    ok(t("pkgs_ok"))

    venv = os.path.join(dest, "venv")
    info(t("venv"))
    if run(["python3", "-m", "venv", venv], check=False)[0] != 0:
        bail(t("e_venv"))

    # ── Roues Python : embarquées (hors-ligne) si présentes, sinon PyPI en ligne ──
    if offline_wheels:
        info(t("pip_offline"))
        rc = run_stream([f"{venv}/bin/python", "-m", "pip", "install", "--quiet",
                         "--no-index", "--find-links", wheels_dir,
                         "-r", os.path.join(dest, "requirements.txt")])
    else:
        run([f"{venv}/bin/python", "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=False)
        info(t("pip"))
        rc = run_stream([f"{venv}/bin/python", "-m", "pip", "install", "--quiet",
                         "-r", os.path.join(dest, "requirements.txt")])
    if rc != 0:
        bail(t("e_pip"))
    ok(t("pydeps_ok"))

    info(t("db_init"))
    rc = run([f"{venv}/bin/python", "-c",
              f"import sys; sys.path.insert(0,'{dest}'); from app.database import init_db; init_db()"],
             check=False)[0]
    if rc != 0:
        bail(t("e_initdb"))
    ok(t("db_ok"))


def install_clock_bare(dest):
    """Met le contrôleur sur la grille TAI (chrony + leapseclist).

    ★ POURQUOI ICI. L'orchestrateur ne produit aucun grain MXL, mais c'est SON horloge qui date
    toutes les mesures de Réglages → Réseau → Horloges : sa dérive se reporte sur chaque ligne
    (+14 ms/h mesurés sous systemd-timesyncd, qui corrige par à-coups). Et surtout : ni timesyncd
    ni chrony ne posent `tai_offset` d'eux-mêmes — sans `leapseclist`, CLOCK_TAI vaut l'UTC, soit
    37 s d'écart avec la grille média, que RIEN ne signale.

    Cette étape n'existait que dans `install.sh` : un contrôleur installé par le menu restait donc
    hors grille, alors que le même contrôleur installé « à la main » y était. C'est ce genre
    d'écart entre deux chemins qui coûte une journée de diagnostic."""
    _hdr(t("h_clock"))
    if run(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "-qq", "chrony"],
           check=False)[0] != 0:
        warn(t("clock_nochrony"))
        return
    os.makedirs("/etc/chrony/conf.d", exist_ok=True)
    with open("/etc/chrony/conf.d/bobi-tai.conf", "w") as f:
        f.write("# Bobi.Studio — offset TAI du noyau (cf. INSTALL.md).\n"
                "# Sans table de secondes intercalaires, le noyau garde tai_offset=0 et CLOCK_TAI\n"
                "# vaut l'UTC : 37 s d'écart avec la grille média, et rien ne le signale.\n"
                "leapseclist /usr/share/zoneinfo/leap-seconds.list\n")
    run(["systemctl", "disable", "--now", "systemd-timesyncd"], check=False)
    run(["systemctl", "restart", "chrony"], check=False)
    # On VÉRIFIE au lieu de supposer — mais chrony ne pose l'offset qu'APRÈS sa première synchro :
    # mesurer aussitôt donnerait 0 sur une machine parfaitement saine (constaté en recette).
    run(["chronyc", "waitsync", "6", "0", "0", "5"], check=False)
    tai = None
    for _ in range(6):
        try:
            import ctypes
            class _T(ctypes.Structure):
                _fields_ = ([("m", ctypes.c_uint)] + [(n, ctypes.c_long) for n in "o f x e".split()]
                            + [("s", ctypes.c_int)]
                            + [(n, ctypes.c_long) for n in "c p t ts tus tk pf j".split()]
                            + [("sh", ctypes.c_int)]
                            + [(n, ctypes.c_long) for n in "st jc cc ec sc".split()]
                            + [("tai", ctypes.c_int), ("pad", ctypes.c_int * 11)])
            _t = _T(); ctypes.CDLL("libc.so.6").adjtimex(ctypes.byref(_t)); tai = _t.tai
        except Exception:
            tai = None
        if tai == 37:
            break
        time.sleep(2)
    ok(t("clock_ok")) if tai == 37 else warn(t("clock_pending", v=tai))


def install_service_bare(dest):
    _hdr(t("h_svc"))
    src = os.path.join(dest, "bobistudio.service")
    if not os.path.isfile(src):
        bail(t("e_svc", d=dest))
    shutil.copy2(src, "/etc/systemd/system/bobistudio.service")
    ok(t("svc_copied"))
    run(["systemctl", "daemon-reload"], check=False)
    run(["systemctl", "enable", "bobistudio"], check=False)
    run(["systemctl", "restart", "bobistudio"], check=False)
    time.sleep(3)
    rc, state = run(["systemctl", "is-active", "bobistudio"], check=False, capture=True)
    if (state or "").strip() == "active":
        ok(t("svc_active"))
    else:
        warn(t("svc_bad", s=(state or "").strip()))


def write_config_local(dest):
    """config_local.py SANS Proxmox → image_build_local s'active (build local). N'écrase pas l'existant."""
    path = os.path.join(dest, "config_local.py")
    if os.path.isfile(path):
        ok(t("cfg_kept"))
        return
    with open(path, "w") as f:
        f.write(textwrap.dedent('''\
            # Généré par install.py — déploiement SANS Proxmox.
            # Proxmox vide → l'orchestrateur build les images en local (pas de nœud hyperviseur).
            PROXMOX_HOST  = ""
            PROXMOX_NODE  = ""
            PROXMOX_TOKEN = ""
        '''))
    ok(t("cfg_written"))


# ─── Chemin 1 : nœud de process (agent seul) ─────────────────────────────────
def _cap_items():
    return [
        ("compute", "compute", t("cap_compute_d")),
        ("io2110", "io2110", t("cap_io2110_d")),
        ("media", "media", t("cap_media_d")),
        ("webrtc", "webrtc", t("cap_webrtc_d")),
    ]


def _enroll(controller_url, token, caps_local):
    """Le nœud s'ANNONCE au contrôleur : POST /api/nodes/enroll (X-MXL-Enroll-Token). Renvoie les
    args install-node.sh (token agent + réseau venant du profil), ou None si échec. caps_local gagnent
    si le profil est nu. Calqué node-bootstrap.sh:54-144."""
    controller_url = _normalize_url(controller_url)
    ice, mgmt = _detect_ifaces()
    body = json.dumps({"hostname": os.uname().nodename, "ice_iface": ice,
                       "mac": None, "nics": [], "agent_port": 9100}).encode()
    try:
        req = urllib.request.Request(controller_url + "/api/nodes/enroll", data=body,
                                     headers={"Content-Type": "application/json",
                                              "X-MXL-Enroll-Token": token})
        with urllib.request.urlopen(req, timeout=10) as r:
            prof = json.load(r)
    except Exception as ex:
        warn(t("enroll_err", e=ex))
        return None
    args = ["--with", ",".join(prof.get("capabilities") or caps_local)]
    args += ["--controller-url", controller_url]                 # l'agent retient son contrôleur
    parent = ice or mgmt
    vlan = prof.get("macvlan_vlan")
    if parent and vlan:
        parent = f"{parent}.{vlan}"
    mapping = [("--token", prof.get("agent_token")), ("--macvlan-parent", parent),
               ("--macvlan-subnet", prof.get("macvlan_subnet")),
               ("--macvlan-gateway", prof.get("macvlan_gateway")),
               ("--macvlan-name", prof.get("macvlan_name")),
               ("--mtl-iface", ice), ("--ptp-domain", prof.get("ptp_domain")),
               ("--hugepages", prof.get("hugepages")), ("--lcores", prof.get("lcores")),
               ("--registry", prof.get("registry")),
               # Tags d'images RÉELS fournis par le contrôleur (meta.json) : sans eux l'installeur
               # retombe sur ses défauts codés en dur, périmés depuis longtemps.
               ("--images", prof.get("images")),
               ("--kernel-pkg", prof.get("kernel_pkg")), ("--kernel-apt", prof.get("kernel_apt"))]
    for flag, val in mapping:
        if val not in (None, ""):
            args += [flag, str(val)]
    key = prof.get("controller_ssh_key")
    if key:
        os.makedirs("/root/.ssh", mode=0o700, exist_ok=True)
        auth = "/root/.ssh/authorized_keys"
        existing = open(auth).read() if os.path.isfile(auth) else ""
        if key not in existing:
            with open(auth, "a") as f:
                f.write(("" if existing.endswith("\n") or not existing else "\n") + key + "\n")
            os.chmod(auth, 0o600)
    return args


def run_tty(cmd):
    """Lance une commande en lui laissant les TROIS flux (contrairement à run_stream, qui capture
    stdout) : le désinstalleur demande de taper RETIRER, et une invite sans retour à la ligne
    resterait invisible derrière un tube. La confirmation appartient au script, pas à ce menu —
    un seul endroit décide, et c'est celui qui sait ce qu'il va détruire."""
    return subprocess.call(cmd)


def path_uninstall(zip_path):
    """Retrait de Bobi.Studio de CETTE machine (nœud, orchestrateur, ou les deux)."""
    cible = menu_select(t("del_title"), [
        ("node", t("del_node_l"), t("del_node_d")),
        ("controller", t("del_ctrl_l"), t("del_ctrl_d")),
        ("both", t("del_both_l"), t("del_both_d")),
    ], default=0, allow_back=True)
    if cible is BACK:
        return BACK

    _hdr(t("del_hdr"))
    # L'avertissement « les nœuds enrôlés ne sont pas touchés » est imprimé par le script lui-même,
    # juste après son inventaire — le répéter ici le faisait apparaître DEUX FOIS à l'écran.

    # Les scripts viennent de l'ARCHIVE (donc de la version qu'on est en train de servir), pas
    # d'une copie installée qui pourrait être plus ancienne que le parc.
    _extract(zip_path, NODE_SRC)
    def _script(nom):
        """Trouve le script de retrait. L'ARCHIVE d'abord (version servie par le contrôleur), puis
        la copie INSTALLÉE sur la machine. ★ L'ordre inverse serait plus simple, mais le repli est
        indispensable : une archive plus ANCIENNE que ces scripts (paquet de distribution pas
        reconstruit) rendait la désinstallation carrément impossible — signalé en recette sur un
        dl-380, « script de désinstallation absent de l'archive ». Or on désinstalle précisément
        quand les choses vont mal : cet outil-là ne doit pas dépendre de la fraîcheur d'un paquet.
        Si les deux sources manquent, on le DIT avec l'action qui débloque, plutôt qu'un simple
        « absent »."""
        for base, origine in ((NODE_SRC, "archive"), (APP_DIR, "installation locale")):
            chemin = os.path.join(base, "node_agent", nom)
            if os.path.isfile(chemin):
                if origine != "archive":
                    warn(t("del_fallback", d=chemin))
                os.chmod(chemin, 0o755)
                return chemin
        bail(t("e_delscript") % nom)

    dry = ask_yn_menu(t("del_dryrun"), default=True, allow_back=True)
    if dry is BACK:
        return BACK

    args_node, args_ctrl = [], []
    if not dry:
        images = ask_yn_menu(t("del_images"), default=False, allow_back=True)
        if images is BACK:
            return BACK
        if images:
            args_node.append("--purge-images"); args_ctrl.append("--purge-images")
        if cible in ("node", "both"):
            media = ask_yn_menu(t("del_media"), default=False, allow_back=True)
            if media is BACK:
                return BACK
            if media:
                args_node.append("--purge-media")
        if cible in ("controller", "both"):
            archive = ask_yn_menu(t("del_archive"), default=True, allow_back=True)
            if archive is BACK:
                return BACK
            if not archive:
                args_ctrl.append("--purge-data")
        info(t("del_confirm_hint"))
    else:
        args_node.append("--dry-run"); args_ctrl.append("--dry-run")

    rc = 0
    # « Les deux » = un seul appel : le désinstalleur d'orchestrateur enchaîne lui-même celui du
    # nœud (il doit le faire AVANT d'effacer /opt/bobistudio, d'où il vient).
    if cible == "controller":
        rc = run_tty(["bash", _script("uninstall-controller.sh"), "--keep-node", *args_ctrl])
    elif cible == "both":
        # En retrait RÉEL : un seul appel — le script orchestrateur enchaîne lui-même celui du nœud
        # (il doit le faire AVANT d'effacer /opt/bobistudio, d'où ce dernier vient).
        # En INVENTAIRE : il s'arrête avant de chaîner, donc on affiche les deux à la suite.
        rc = run_tty(["bash", _script("uninstall-controller.sh"), *args_ctrl, *args_node])
        if dry and rc == 0:
            rc = run_tty(["bash", _script("uninstall-node.sh"), *args_node])
    else:
        rc = run_tty(["bash", _script("uninstall-node.sh"), *args_node])

    print()
    ok(t("del_done")) if rc == 0 else warn(t("del_failed"))
    return None


def run_install_node(zip_path, args):
    _extract(zip_path, NODE_SRC)
    script = os.path.join(NODE_SRC, "node_agent", "install-node.sh")
    if not os.path.isfile(script):
        bail(t("e_nonode"))
    os.chmod(script, 0o755)
    _hdr(t("h_node_install"))
    if run_stream(["bash", script, *args]) != 0:
        bail(t("e_installnode"))


def path_node(zip_path):
    """Pose l'agent (capacités choisies ici) et ANNONCE le nœud au contrôleur (push).
    Renvoie BACK pour revenir au menu principal."""
    while True:
        caps = menu_multi(t("caps_title"), _cap_items(), preselected=["compute"], allow_back=True)
        if caps is BACK:
            return BACK
        # La carte E810 (io2110) est auto-détectée par _enroll (driver `ice`) et le reste
        # du réseau est réglé après coup depuis l'orchestrateur → aucune saisie ici.
        url = ask(t("ctrl_url"), allow_back=True, help=t("enroll_help"))
        if url is BACK:
            continue
        tok = ask(t("enroll_tok"), allow_back=True)
        if tok is BACK:
            continue
        args = _enroll(url, tok, caps)
        if args is None:
            warn(t("enroll_fail"))
            continue
        break
    run_install_node(zip_path, args)
    print()
    ok(t("node_ready"))
    info(f"agent_url : http://{_host_ip() or os.uname().nodename}:9100")
    info(t("node_register"))
    if "io2110" in caps:
        info(t("io2110_pending"))
    return None


# ─── Chemins 2 & 3 : orchestrateur (bare) ± nœud local ───────────────────────
def install_controller(zip_path):
    _hdr(t("h_ctrl"))
    if os.path.isfile(os.path.join(APP_DIR, "main.py")):
        upd = ask_yn_menu(t("ctrl_exists", d=APP_DIR), default=True, allow_back=True)
        if upd is BACK:
            return BACK
        if not upd:
            bail(t("cancelled"))
    # ★ `main.py` importe `services.nmos` au NIVEAU MODULE. Si le dossier est vide, Python en fait
    # un « namespace package » : l'import RÉUSSIT, le module est creux, et le démarrage casse une
    # ligne plus loin sur un AttributeError qui ne nomme pas la cause. On refuse donc ici, en le
    # disant — les autres services (rdma, emberplus, atem, skaarhoj) sont importés dans des `try`
    # et s'installent après coup depuis la page Catalogue.
    _src_dir = zip_path if os.path.isdir(zip_path) else None
    if _src_dir and not os.path.isfile(os.path.join(_src_dir, "services", "nmos", "__init__.py")):
        bail(t("e_nonmos"))
    _extract(zip_path, APP_DIR)
    ok(t("code_deployed", d=APP_DIR))
    write_config_local(APP_DIR)
    install_deps_bare(APP_DIR)
    install_clock_bare(APP_DIR)
    install_service_bare(APP_DIR)
    return None


def register_local_agent(token):
    code = ("from app import database as d; d.init_db(); "
            "from app import node_driver as n; "
            f"print(n.register('127.0.0.1', 9100, '{token}', name='local'))")
    rc, out = run([f"{APP_DIR}/venv/bin/python", "-c", code], check=False, capture=True)
    ok(t("agent_local_ok")) if rc == 0 else warn(t("agent_local_bad"))


def path_controller(zip_path):
    if install_controller(zip_path) is BACK:
        return BACK
    print()
    ok(t("ctrl_ready"))
    info(t("access", url=f"{BOLD}{CYAN}http://{_host_ip() or os.uname().nodename}:5000{R}"))
    return None


def path_all_in_one(zip_path):
    # Capacités du nœud LOCAL (comme pour un nœud dédié) : compute/io2110/media/webrtc.
    caps = menu_multi(t("caps_title"), _cap_items(), preselected=["compute"], allow_back=True)
    if caps is BACK:
        return BACK
    if install_controller(zip_path) is BACK:
        return BACK
    token = secrets.token_hex(24)
    run_install_node(zip_path, ["--with", ",".join(caps) or "compute",
                                "--port", "9100", "--token", token])
    register_local_agent(token)
    print()
    ok(t("aio_ready"))
    info(t("access", url=f"{BOLD}{CYAN}http://{_host_ip() or os.uname().nodename}:5000{R}"))
    return None


# ─── Menu / boucle principale (avec retour en arrière) ───────────────────────
def main():
    global LANG
    while True:
        # Étape 1 : langue (pas de retour avant elle).
        LANG = menu_select(t("lang_title"), [("fr", "Français", None), ("en", "English", None)],
                           default=0, allow_back=False)
        _banner()
        # Étape 2 : menu principal (← revient au choix de langue ; ← d'un chemin revient ici).
        while True:
            sel = menu_select(t("menu_title"), [
                ("node", t("o_node_l"), t("o_node_d")),
                ("controller", t("o_ctrl_l"), t("o_ctrl_d")),
                ("all", t("o_all_l"), t("o_all_d")),
                ("proxmox", t("o_pve_l"), t("o_pve_d")),
                ("uninstall", t("o_del_l"), t("o_del_d")),
            ], default=0, allow_back=True)
            if sel is BACK:
                break                       # ← retour au choix de la langue
            if sel == "proxmox":
                ip.main()                   # flux legacy inchangé (crée la VM orchestrateur)
                return
            zip_path = _find_source()[1]
            res = {"node": path_node, "controller": path_controller, "all": path_all_in_one,
                   "uninstall": path_uninstall}[sel](zip_path)
            if res is BACK:
                continue                    # ← retour au menu principal
            return


if __name__ == "__main__":
    if os.geteuid() != 0:
        print(f"\n  {RED}{STR['fr']['must_root']} / {STR['en']['must_root']}{R}\n")
        sys.exit(1)
    try:
        main()
    except KeyboardInterrupt:
        print(); bail(STR["fr"]["cancelled"])
