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
  4) Désinstaller

Stdlib uniquement (tourne sur une box nue avant tout venv). Navigation clavier en mode brut
(termios/tty) avec repli en saisie numérotée si stdin/stdout n'est pas un terminal.
Cf. NODE_AGENT.md (§9b), node_agent/REINSTALL.md. The Proxmox path (orchestrator in an LXC
container, `install_proxmox.py`) was removed on 2026-11-22: the product no longer uses LXC.
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
import uuid
import zipfile

R, BOLD, DIM, REVERSE = "\033[0m", "\033[1m", "\033[2m", "\033[7m"
GREEN, RED, YELLOW, CYAN, WHITE = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[97m"


def run(cmd, check=True, capture=False, input_data=None):
    """Runs a command. Returns (returncode, stdout) — stdout only when capture=True.
    ⚠ No `cwd`: the child inherits the installer's current directory."""
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True, input=input_data)
        return r.returncode, r.stdout.strip()
    r = subprocess.run(cmd, text=True, input=input_data)
    if check and r.returncode != 0:
        raise RuntimeError(f"Commande échouée : {' '.join(str(x) for x in cmd)}")
    return r.returncode, ""


def run_stream(cmd, prefix="     "):
    """Runs a command and prints its output line by line."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        print(f"{prefix}{DIM}{line.rstrip()}{R}", flush=True)
    proc.wait()
    return proc.returncode

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
        "ua_missing": "Sans clavier, cette réponse manque : {q}. La fournir par {opt}.",
        "ua_bad": "Réponse « {v} » refusée pour {opt} : attendu {ok}.",
        "ua_enroll": "Le contrôleur a refusé l'enrôlement : vérifier --controller et le jeton.",
        "ua_mode": "Installation sans clavier : réponses de {src}.",
        "ua_partial": "Installation incomplète : code de sortie 3.",
        "ua_args": "{e}",
        "ua_answers": "Fichier de réponses illisible ({p}) : {e}",
        "ua_token_file": "Fichier du jeton illisible ({p}) : {e}",
        "ua_missing_many": "Sans clavier, ces réponses manquent : {opts}. Rien n'a été modifié.",
        "ua_uninstall": "--inventory (inventaire seul) ou --confirm RETIRER (retrait réel)",
        "nav_single": "↑/↓ déplacer · Entrée valider · ← retour",
        "nav_single_top": "↑/↓ déplacer · Entrée valider",
        "nav_multi": "↑/↓ déplacer · Espace cocher · Entrée valider · ← retour",
        "nav_multi_none": "↑/↓ déplacer · Espace cocher · Entrée valider (rien coché = aucun)",
        "multi_need_one": "Cochez au moins une case (Espace), puis Entrée.",
        "install_done": "Installation terminée.",
        "plugin_auto_ok": "Plugin {p} {v} installé depuis le catalogue (requis par la capacité du nœud).",
        "plugin_auto_fail": "Plugin {p} non installé ({e}) : l'installer depuis Réglages → Mises à jour et catalogue.",
        "plugin_auto_unknown": "Plugins requis par les capacités du nœud : résultat illisible, vérifier Réglages → Mises à jour et catalogue.",
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
        "del_images": "Supprimer aussi les images Docker de Bobi.Studio ? Ce sont les modèles à partir desquels les conteneurs de traitement sont créés (plusieurs Go). Les garder accélère une réinstallation ; les supprimer libère la place, et elles seront reconstruites au besoin.",
        "del_media": "Supprimer aussi les MÉDIAS stockés sur cette machine (irréversible) ?",
        "del_confirm_hint": "Le script demandera de taper RETIRER pour confirmer.",
        "e_delscript": "script de désinstallation introuvable (%s) : ni dans l'archive téléchargée, ni dans /opt/bobistudio/node_agent. Reconstruire le paquet de distribution (Réglages → Déploiement) puis relancer.",
        "del_fallback": "absent de l'archive — on utilise la copie installée : {d}",
        "del_done": "Retrait terminé.",
        "del_now": "Inventaire terminé, rien n'a été modifié. Retirer maintenant ce qui vient d'être listé ?",
        "del_dry_done": "Inventaire terminé : rien n'a été modifié.",
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
        "enroll_tok": "Jeton d'enrôlement",
        "enroll_help": "Le nœud va s'annoncer à ce contrôleur. Il faudra ensuite un jeton "
                       "d'enrôlement, créé dans son interface web (Réglages → Nœuds & Matériel → "
                       "Déploiement, « + Nouveau jeton »).",
        "enroll_err": "Annonce au contrôleur échouée : {e}",
        "enroll_fail": "Annonce impossible — vérifie l'adresse/le jeton, puis relance.",
        "enroll_tok_help": "Le jeton d'enrôlement est un code à usage unique qui autorise cette machine à rejoindre l'orchestrateur, comme un code d'invitation. Il se crée sur l'orchestrateur, dans l'interface web : Réglages → Nœuds & Matériel → Déploiement, « Enrôlements en attente », bouton « + Nouveau jeton ». Copiez-le, puis collez-le ici.",
        "enroll_tok_refused": "L'orchestrateur n'a pas accepté cette machine : jeton erroné, expiré ou déjà utilisé, ou adresse injoignable ({url}). Saisissez le jeton à nouveau, ou ← pour corriger l'adresse.",
        "h_node_install": "Installation du nœud (bobi-node-agent)",
        "node_ready": "Nœud installé et annoncé au contrôleur.",
        "node_register": "→ Le nœud apparaît tout seul dans le web (Monitoring → Serveurs / "
                         "Réglages → Nœuds & Matériel). Rien d'autre à saisir.",
        "io2110_pending": "io2110 : choisis la carte E810 et active le PTP depuis l'orchestrateur "
                          "(Réglages → Nœuds & Matériel) une fois le nœud enrôlé. "
                          "L'orchestrateur installe de lui-même le plugin 2110_io depuis le catalogue ; "
                          "il restera à construire l'image bobi-mtl (Réglages → Nœuds & Matériel → Déploiement → Build).",
        # contrôleur
        "h_ctrl": "Orchestrateur (contrôleur Flask)",
        "ctrl_exists": "{d} contient déjà du code — mettre à jour (config/DB conservées) ?",
        "code_deployed": "Code déployé dans {d}",
        "ctrl_ready": "Orchestrateur prêt.",
        "access": "Accès : {url}  (créer le compte admin à la 1re visite)",
        "update_default": "Vérification des mises à jour : ACTIVÉE par défaut (une fois par jour, lecture des versions publiées sur GitHub, rien n'est envoyé ni installé). Désactivable à la 1re visite, ou dans Réglages → Mises à jour.",
        "aio_ready": "Tout-en-un prêt (orchestrateur + nœud local).",
        "gestes_title": "Il reste ceci à faire pour qu'un conteneur puisse tourner :",
        "services_title": "Services utiles, éteints par défaut. Cochez ceux à activer :",
        "services_none": "Aucun service activé — ils restent disponibles dans Réglages → Services.",
        "services_ok": "{n} service(s) activé(s).",
        "services_partiel": "Service(s) NON activé(s) : {cles} — à faire depuis Réglages → Services.",
        "services_bad": "Liste des services proposables illisible — à voir dans Réglages → Services.",
        "gestes_none": "Rien ne manque : un premier conteneur peut être déployé.",
        "gestes_flous": "Non mesuré : {codes} — l'agent du nœud n'a pas répondu. « non mesuré » ne veut pas dire « rien à faire » : à revérifier dans l'interface.",
        "gestes_bad": "Les gestes restants n'ont pas pu être calculés — à vérifier dans l'interface (Réglages → Déploiement). Ce n'est PAS « rien à faire ».",
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
        "build_kept": "build_info.json déjà présent — conservé",
        "build_written": "identité posée : {l}",
        "cfg_kept": "config_local.py déjà présent — conservé",
        "cfg_written": "config_local.py écrit (sans Proxmox)",
        "agent_local_ok": "Agent local enregistré (127.0.0.1:9100)",
        "agent_local_bad": "Agent local NON enregistré : aucun nœud déclaré, rien ne pourra être déployé.",
        "agent_local_fix": "À réparer — depuis /opt/bobistudio, en root : ./venv/bin/python -c \"from app import node_driver as n; print(n.register('127.0.0.1', 9100, '<jeton de /etc/bobi-node-agent/config.json>', name='local'))\"  ·  ou par Réglages → Nœuds & Matériel.",
        "aio_partial": "Tout-en-un INCOMPLET : l'orchestrateur tourne, le nœud local n'est pas déclaré.",
        "aio_partial_net": "Tout-en-un INCOMPLET : le nœud local est déclaré, mais le réseau des conteneurs n'a PAS pu être créé — rien n'y sera déployable. Réglages → Nœuds → ⚙ Configuration → « Réseau containers » pour le créer, et l'erreur ci-dessus en dit la cause.",
        # Réseau des conteneurs (tout-en-un) — cf. app/container_net.py
        "net_title": "Réseau des conteneurs",
        "net_interne_l": "Réseau interne (recommandé)",
        "net_interne_d": "Les conteneurs vivent dans un sous-réseau privé à ce serveur, sortie par NAT. Rien à demander à l'administrateur réseau : ça marche tout de suite.",
        "net_site_l": "Réseau du site",
        "net_site_d": "Chaque conteneur prend une IP sur votre LAN. À choisir si des équipements extérieurs (pupitres, récepteurs, contrôle tiers) doivent les joindre directement. À finir dans Réglages → Nœuds & Matériel (carte réseau et plage d'IP).",
        "net_interne_ok": "Réseau interne « {name} » créé ({subnet}).",
        "net_interne_bad": "Réseau interne NON créé : {err}",
        "net_site_todo": "Mode « réseau du site » retenu — à terminer dans Réglages → Nœuds & Matériel : carte réseau des conteneurs et plage d'IP.",
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
        "ua_missing": "Without a keyboard, this answer is missing: {q}. Provide it with {opt}.",
        "ua_bad": "Answer “{v}” refused for {opt}: expected {ok}.",
        "ua_enroll": "The controller refused the enrolment: check --controller and the token.",
        "ua_mode": "Unattended installation: answers from {src}.",
        "ua_partial": "Installation incomplete: exit code 3.",
        "ua_args": "{e}",
        "ua_answers": "Answers file unreadable ({p}): {e}",
        "ua_token_file": "Token file unreadable ({p}): {e}",
        "ua_missing_many": "Without a keyboard, these answers are missing: {opts}. Nothing was changed.",
        "ua_uninstall": "--inventory (inventory only) or --confirm RETIRER (real removal)",
        "nav_single": "↑/↓ move · Enter select · ← back",
        "nav_single_top": "↑/↓ move · Enter select",
        "nav_multi": "↑/↓ move · Space toggle · Enter confirm · ← back",
        "nav_multi_none": "↑/↓ move · Space toggle · Enter confirm (nothing ticked = none)",
        "multi_need_one": "Tick at least one box (Space), then Enter.",
        "install_done": "Installation finished.",
        "plugin_auto_ok": "Plugin {p} {v} installed from the catalogue (required by the node's capability).",
        "plugin_auto_fail": "Plugin {p} not installed ({e}): install it from Settings → Updates & catalogue.",
        "plugin_auto_unknown": "Plugins required by the node's capabilities: unreadable result, check Settings → Updates & catalogue.",
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
        "del_images": "Also delete the Bobi.Studio Docker images? They are the templates the processing containers are created from (several GB). Keeping them speeds up a reinstall; deleting them frees the space, and they are rebuilt when needed.",
        "del_media": "Also delete the MEDIA stored on this machine (irreversible)?",
        "del_confirm_hint": "The script will ask you to type RETIRER to confirm.",
        "e_delscript": "uninstall script not found (%s): neither in the downloaded archive nor in /opt/bobistudio/node_agent. Rebuild the distribution package (Settings → Deployment) then retry.",
        "del_fallback": "missing from the archive — using the installed copy: {d}",
        "del_done": "Removal finished.",
        "del_now": "Inventory finished, nothing was changed. Remove what was just listed now?",
        "del_dry_done": "Inventory finished: nothing was changed.",
        "del_failed": "The removal reported an error — read the output above.",
        "h_node": "Process node",
        "caps_title": "Node capabilities — toggle with Space",
        "cap_compute_d": "numpy workloads (mixer, multiview, UDC, corrector…).",
        "cap_io2110_d": "ST 2110 I/O (E810 NIC).",
        "cap_media_d": "Playback / recording / transcoding / stills.",
        "cap_webrtc_d": "WebRTC gateway (MediaMTX).",
        "need_one": "Select at least one capability.",
        "ctrl_url": "Controller address — IP or host (e.g. x.x.x.x)",
        "enroll_tok": "Enrollment token",
        "enroll_help": "The node will announce itself to this controller. It will then need an "
                       "enrollment token, created in its web interface (Settings → Nodes & Hardware "
                       "→ Deployment, \"+ New token\").",
        "enroll_err": "Announcing to controller failed: {e}",
        "enroll_fail": "Announce failed — check the address/token, then retry.",
        "enroll_tok_help": "The enrollment token is a single-use code that lets this machine join the orchestrator, like an invitation code. Create it on the orchestrator, in the web interface: Settings → Nodes & Hardware → Deployment, \"Pending enrollments\", \"+ New token\" button. Copy it, then paste it here.",
        "enroll_tok_refused": "The orchestrator did not accept this machine: wrong, expired or already used token, or unreachable address ({url}). Enter the token again, or ← to fix the address.",
        "h_node_install": "Node installation (bobi-node-agent)",
        "node_ready": "Node installed and announced to the controller.",
        "node_register": "→ The node shows up on its own in the web UI (Monitoring → Servers / "
                         "Settings → Nodes & Hardware). Nothing else to enter.",
        "io2110_pending": "io2110: pick the E810 card and enable PTP from the orchestrator "
                          "(Settings → Nodes & Hardware) once the node is enrolled. "
                          "The orchestrator installs the 2110_io plugin from the catalogue by itself; "
                          "the bobi-mtl image will then need building (Settings → Nodes & Hardware → Deployment → Build).",
        "h_ctrl": "Orchestrator (Flask controller)",
        "ctrl_exists": "{d} already contains code — update it (config/DB kept)?",
        "code_deployed": "Code deployed to {d}",
        "ctrl_ready": "Orchestrator ready.",
        "access": "Access: {url}  (create the admin account on first visit)",
        "update_default": "Update check: ON by default (once a day, reads the versions published on GitHub, nothing is sent or installed). Can be turned off on first visit, or in Settings → Updates.",
        "aio_ready": "All-in-one ready (orchestrator + local node).",
        "gestes_title": "What is left to do before a container can run:",
        "services_title": "Useful services, off by default. Tick the ones to enable:",
        "services_none": "No service enabled — they stay available in Settings → Services.",
        "services_ok": "{n} service(s) enabled.",
        "services_partiel": "Service(s) NOT enabled: {cles} — to be done from Settings → Services.",
        "services_bad": "The list of offerable services could not be read — see Settings → Services.",
        "gestes_none": "Nothing is missing: a first container can be deployed.",
        "gestes_flous": "Not measured: {codes} — the node agent did not answer. “not measured” does not mean “nothing to do”: check again in the interface.",
        "gestes_bad": "The remaining steps could not be computed — check in the interface (Settings → Deployment). This is NOT “nothing to do”.",
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
        "build_kept": "build_info.json already present — kept",
        "build_written": "identity recorded: {l}",
        "cfg_kept": "config_local.py already present — kept",
        "cfg_written": "config_local.py written (no Proxmox)",
        "agent_local_ok": "Local agent registered (127.0.0.1:9100)",
        "agent_local_bad": "Local agent NOT registered: no node declared, nothing can be deployed.",
        "agent_local_fix": "To fix — from /opt/bobistudio, as root: ./venv/bin/python -c \"from app import node_driver as n; print(n.register('127.0.0.1', 9100, '<token from /etc/bobi-node-agent/config.json>', name='local'))\"  ·  or via Settings → Nodes & Hardware.",
        "aio_partial": "All-in-one INCOMPLETE: the orchestrator runs, the local node is not declared.",
        "aio_partial_net": "All-in-one INCOMPLETE: the local node is declared, but the container network could NOT be created \u2014 nothing can be deployed on it. Settings \u2192 Nodes \u2192 \u2699 Configuration \u2192 \u00ab Container network \u00bb to create it; the error above states the cause.",
        # Container network (all-in-one) — see app/container_net.py
        "net_title": "Container network",
        "net_interne_l": "Internal network (recommended)",
        "net_interne_d": "Containers live in a subnet private to this server, with NATed egress. Nothing to ask the network administrator: it works right away.",
        "net_site_l": "Site network",
        "net_site_d": "Each container takes an IP on your LAN. Choose this if outside equipment (panels, receivers, third-party control) must reach them directly. To be finished in Settings → Nodes & Hardware (network card and IP range).",
        "net_interne_ok": "Internal network « {name} » created ({subnet}).",
        "net_interne_bad": "Internal network NOT created: {err}",
        "net_site_todo": "« Site network » mode selected — to be finished in Settings → Nodes & Hardware: container network card and IP range.",
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


# ─── Installation SANS CLAVIER : réponses fournies d'avance (2026-11-21) ─────
# CI, recette scriptée, déploiement en série. Chaque question porte un NOM ; un nom = une option
# = une clé du fichier de réponses (`--answers fichier.json`, les options l'emportent sur lui).
# Le mode sans clavier s'active dès qu'une réponse est fournie, et il ne DEVINE rien : une réponse
# absente arrête l'installation en nommant l'option à fournir (code 2). Seules ont un défaut les
# réponses sans conséquence (langue) ou du côté sûr (ne rien purger, garder l'archive). Le repli
# numéroté lu sur l'entrée standard reste, mais il dépend de l'ORDRE des questions : une question
# ajoutée y décale toutes les réponses sans prévenir — c'est pourquoi on nomme.
# Le jeton d'enrôlement n'a PAS d'option en clair (il se lirait dans `ps` et l'historique) :
# `--token-file`, la variable BOBI_ENROLL_TOKEN, ou la clé `token` du fichier.
OPTIONS = {
    "lang": "--lang", "role": "--role", "caps": "--caps", "controller": "--controller",
    "token": "--token-file", "update": "--update", "net": "--net", "services": "--services",
    "target": "--target", "inventory": "--inventory", "confirm": "--confirm",
    "purge_images": "--purge-images", "purge_media": "--purge-media", "purge_data": "--purge-data",
}
ANSWERS = {}
UNATTENDED = False
ANSWERS_SRC = ""
EXIT_MISSING, EXIT_PARTIAL = 2, 3
_PARTIAL = [False]          # an outcome the installer itself calls incomplete
_NO_DEFAULT = object()


def _ua_exit(msg, code):
    print(); err(msg); print(); sys.exit(code)


def parse_args(argv):
    """Options → ANSWERS. Renvoie le dict des réponses (vide = installation au clavier)."""
    import argparse
    # allow_abbrev=False: `--token <secret>` would otherwise be taken for `--token-file`.
    p = argparse.ArgumentParser(prog="install.py", allow_abbrev=False, description="Bobi.Studio installer. Any "
                                "answer given here switches to unattended mode: a missing "
                                "answer stops with the option to provide (exit 2).")
    p.add_argument("--answers", metavar="FILE", help="JSON answers file (keys = option names)")
    p.add_argument("--lang", choices=["fr", "en"])
    p.add_argument("--role", choices=["node", "controller", "all", "uninstall"])
    p.add_argument("--caps", help="node capabilities, comma-separated (compute,io2110,media,webrtc)")
    p.add_argument("--controller", metavar="URL", help="controller to enrol with (node)")
    p.add_argument("--token-file", metavar="FILE", help="file holding the enrolment token (node)")
    p.add_argument("--update", choices=["yes", "no"], help="update an existing installation")
    p.add_argument("--net", choices=["interne", "site"], help="container network (all-in-one)")
    p.add_argument("--services", help="services to turn on, comma-separated, or none")
    p.add_argument("--target", choices=["node", "controller", "both"], help="what to uninstall")
    p.add_argument("--inventory", action="store_true", help="uninstall: inventory only")
    p.add_argument("--confirm", metavar="RETIRER", help="uninstall: real removal")
    p.add_argument("--purge-images", action="store_true")
    p.add_argument("--purge-media", action="store_true")
    p.add_argument("--purge-data", action="store_true", help="uninstall: no data archive")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        sys.exit(EXIT_MISSING if e.code else 0)
    ans = {}
    if a.answers:
        try:
            with open(a.answers) as f:
                ans = json.load(f)
            if not isinstance(ans, dict):
                raise ValueError("an object {name: answer} is expected")
        except (OSError, ValueError) as e:
            _ua_exit(t("ua_answers", p=a.answers, e=e), EXIT_MISSING)
        if ans.get("token_file") and not ans.get("token"):
            ans["token"] = _read_token(ans.pop("token_file"))
    for nom in OPTIONS:
        v = getattr(a, nom, None) if nom != "token" else None
        if v not in (None, False):
            ans[nom] = v
    if a.token_file:
        ans["token"] = _read_token(a.token_file)
    elif not ans.get("token") and os.environ.get("BOBI_ENROLL_TOKEN"):
        ans["token"] = os.environ["BOBI_ENROLL_TOKEN"].strip()
    return ans


def _read_token(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError as e:
        _ua_exit(t("ua_token_file", p=path, e=e), EXIT_MISSING)


def _ua_value(name, question, default=_NO_DEFAULT):
    if name in ANSWERS and ANSWERS[name] not in (None, ""):
        return ANSWERS[name]
    if default is not _NO_DEFAULT:
        return default
    _ua_exit(t("ua_missing", q=re.sub(r"\x1b\[[0-9;]*m", "", str(question)), opt=OPTIONS.get(name, name)),
             EXIT_MISSING)


def _ua_select(name, question, items, default=_NO_DEFAULT):
    v = _ua_value(name, question, default)
    keys = [k for k, _l, _d in items]
    if isinstance(v, bool) or set(keys) == {True, False}:
        b = {"yes": True, "oui": True, "true": True, "1": True,
             "no": False, "non": False, "false": False, "0": False}.get(str(v).strip().lower())
        if b is not None and b in keys:
            return b
    for k in keys:
        if str(k).lower() == str(v).strip().lower():
            return k
    attendu = "yes | no" if set(keys) == {True, False} else " | ".join(map(str, keys))
    _ua_exit(t("ua_bad", v=v, opt=OPTIONS.get(name, name), ok=attendu), EXIT_MISSING)


def _ua_multi(name, question, items, default=_NO_DEFAULT):
    v = _ua_value(name, question, default)
    vals = v if isinstance(v, list) else [x.strip() for x in str(v).split(",")]
    vals = [x for x in vals if x and str(x).lower() not in ("none", "aucun")]
    keys = [k for k, _l, _d in items]
    bad = [x for x in vals if x not in keys]
    if bad:
        _ua_exit(t("ua_bad", v=",".join(map(str, bad)), opt=OPTIONS.get(name, name),
                   ok=",".join(map(str, keys)) + " | none"), EXIT_MISSING)
    return [k for k in keys if k in vals]


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
# The Linux text console (TERM=linux) has no ❯ in its font: it showed as a diamond (tester
# photos, 2026-11-22). Same for superscript letters, which the strings no longer use.
PTR = ">" if os.environ.get("TERM") == "linux" else "❯"
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


def _menu_lines(title, items, idx, checks=None, multi=False, allow_back=True, width=70,
                allow_empty=False, notice=""):
    """Construit le corps du menu (sans le cadre). items=[(key,label,desc)].

    The title and the descriptions WRAP to the frame (tester photos, 2026-11-22: a long question
    pushed the right border out, and descriptions were cut with « … » into useless halves).
    Descriptions only fall back to one truncated line when the wrapped menu would not fit the
    terminal height."""
    has_desc = any(d for _, _, d in items)
    iw = width - 2
    lines = [f"{BOLD}{tl}{R}" for tl in (textwrap.wrap(str(title), iw - 2) or [""])] + [""]
    wrapped = {i: textwrap.wrap(d or "", iw - 4) or [""] for i, (_k, _l, d) in enumerate(items)}
    _cols, rows = _term_size()
    if has_desc and len(lines) + sum(1 + len(w) for w in wrapped.values()) + 12 > rows:
        wrapped = {i: [_trunc(d or "", iw - 4)] for i, (_k, _l, d) in enumerate(items)}
    for i, (key, label, desc) in enumerate(items):
        cur = (i == idx)
        box = ""
        if multi:
            box = f"{GREEN}[x]{R} " if checks and checks[i] else f"{DIM}[ ]{R} "
        lab = _trunc(label, iw - 6)
        if cur:
            lines.append(f"{CYAN}{BOLD}{PTR}{R} {box}{REVERSE}{BOLD} {lab} {R}")
        else:
            lines.append(f"  {box}{lab}")
        if has_desc:
            lines += [f"    {DIM}{dl}{R}" for dl in wrapped[i]]
    if multi:
        hint = t("nav_multi_none") if allow_empty else t("nav_multi")
    else:
        hint = t("nav_single") if allow_back else t("nav_single_top")
    lines += [""] + [f"{DIM}{hl}{R}" for hl in textwrap.wrap(hint, iw - 2)]
    if notice:
        lines += [f"{YELLOW}{nl}{R}" for nl in textwrap.wrap(notice, iw - 2)]
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


def menu_select(title, items, default=0, allow_back=True, name=None, ua_default=_NO_DEFAULT):
    """Sélection unique au curseur, panneau plein écran. Renvoie la clé, ou BACK. Repli numéroté hors TTY.
    `name` : la réponse fournie d'avance qui y répond sans clavier (cf. OPTIONS)."""
    if UNATTENDED:
        return _ua_select(name, title, items, ua_default)
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


def menu_multi(title, items, preselected=None, allow_back=True, name=None, ua_default=_NO_DEFAULT,
               allow_empty=False):
    """Cases à cocher, panneau plein écran. Renvoie la liste des clés cochées, ou BACK. Repli CSV hors TTY.

    `allow_empty`: Enter with nothing ticked answers « none ». Without it, Enter on an empty
    selection used to do NOTHING, silently — on the services screen that was a dead end, whose
    only way out was Ctrl-C, which cancels everything (tester, 2026-11-22; noted 2026-11-10)."""
    if UNATTENDED:
        return _ua_multi(name, title, items, ua_default)
    if not _interactive():
        return _multi_fallback(title, items, preselected, allow_back)
    pre = set(preselected or [])
    checks = [it[0] in pre for it in items]
    idx = 0
    notice = ""
    with _AltScreen(), _Raw():
        while True:
            _frame(_menu_lines(title, items, idx, checks=checks, multi=True, width=_panel_width(),
                               allow_empty=allow_empty, notice=notice))
            notice = ""
            k = _read_key()
            if k == "up":
                idx = (idx - 1) % len(items)
            elif k == "down":
                idx = (idx + 1) % len(items)
            elif k == "space":
                checks[idx] = not checks[idx]
            elif k == "enter":
                sel = [items[i][0] for i in range(len(items)) if checks[i]]
                if sel or allow_empty:
                    return sel
                notice = t("multi_need_one")
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
        for i, para in enumerate(help.split("\n\n")):          # paragraphs stay apart
            if i:
                lines.append("")
            lines += [f"{DIM}{hl}{R}" for hl in textwrap.wrap(para, iw - 2)]
    lines.append("")
    text = buf.decode("utf-8", "ignore")
    if text:
        field = _trunc("•" * len(text) if secret else text, iw - 4)
    elif default is not None:
        field = f"{DIM}{_trunc(str(default), iw - 4)}{R}"
    else:
        field = ""
    lines.append(f"{CYAN}{PTR}{R} {field}{REVERSE} {R}")          # bloc inversé = faux curseur
    lines += ["", f"{DIM}{t('nav_input_back') if allow_back else t('nav_input')}{R}"]
    return lines


def ask(prompt, default=None, secret=False, required=True, allow_back=False, help=None, name=None):
    """Saisie texte plein écran (mode brut). ← / Échap → BACK si allow_back. Repli ligne hors TTY."""
    if UNATTENDED:
        return str(_ua_value(name, prompt))
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
            line = f"  {BOLD}{prompt}{R}{hint}{bh} {CYAN}{PTR}{R} "
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


def ask_yn_menu(prompt, default=False, allow_back=True, name=None, ua_default=_NO_DEFAULT):
    """Oui/Non au curseur (réutilise menu_select). Renvoie True/False ou BACK."""
    sel = menu_select(prompt, [(True, t("yes"), None), (False, t("no"), None)],
                      default=0 if default else 1, allow_back=allow_back,
                      name=name, ua_default=ua_default)
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


# ─── Étapes bare (orchestrateur posé directement sur cette machine) ───
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
    # The web interface opens in the installer's language (tester, 2026-11-22: installed in English,
    # the setup page came up in French). Only when nothing is stored yet: an update keeps the
    # language chosen since.
    run([f"{venv}/bin/python", "-c",
         f"import sys; sys.path.insert(0,'{dest}'); from app import database as d; "
         f"d.db_get_setting('ui_lang_default') is None and d.db_set_setting('ui_lang_default', '{LANG}')"],
        check=False, capture=True)


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
    # Captured: on a Debian without timesyncd, systemctl printed « Failed to disable unit » in red
    # in the middle of a successful install (tester, 2026-11-22). The verdict below is what counts.
    run(["systemctl", "disable", "--now", "systemd-timesyncd"], check=False, capture=True)
    run(["systemctl", "restart", "chrony"], check=False, capture=True)
    # On VÉRIFIE au lieu de supposer — mais chrony ne pose l'offset qu'APRÈS sa première synchro :
    # mesurer aussitôt donnerait 0 sur une machine parfaitement saine (constaté en recette).
    run(["chronyc", "waitsync", "6", "0", "0", "5"], check=False, capture=True)
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


def write_build_info(dest):
    """Écrit `build_info.json` avec CE QUE L'INSTALLEUR SAIT : la ref demandée et son commit.

    ★ POURQUOI ICI. Une installation depuis l'archive GitHub n'a pas de `.git`, et
    `build_dist` n'a pas tourné : `builder.current_build_info()` retombait donc sur son repère
    « dev », sans `build_id`, sans empreinte — alors que `v0.9.15` avait été demandé
    explicitement. Deux contrôleurs installés ainsi, avec du code DIFFÉRENT, se présentaient
    tous les deux comme « dev » et se comparaient comme identiques dans la page Flotte et dans
    l'aperçu Push/Pull. Mesuré le 2026-11-10 sur une installation vierge.

    L'installeur est le SEUL moment où cette information existe : `get.sh` a choisi la ref et
    demandé son SHA à l'API. Après lui, plus personne ne peut la retrouver.

    ⚠ ON N'ÉCRASE JAMAIS un `build_info.json` existant : une archive produite par `build_dist`
    en embarque un, et il décrit mieux son contenu que nous. On ne l'écrit pas non plus quand
    `get.sh` ne nous a rien dit (installation depuis une source locale) : l'arbre a alors son
    `.git`, et le repli de `builder` sait lire l'empreinte tout seul.

    ⚠ `git_hash` PEUT RESTER None — quota d'API épuisé, réseau coupé. Une ref sans empreinte
    reste très au-dessus de « dev » : on écrit ce qu'on a plutôt que rien.
    """
    ref = (os.environ.get("BOBI_SRC_REF") or "").strip()
    sha = (os.environ.get("BOBI_SRC_SHA") or "").strip() or None
    if not ref:
        return
    path = os.path.join(dest, "build_info.json")
    if os.path.isfile(path):
        ok(t("build_kept"))
        return
    # Le libellé porte la ref, qui est ce que l'exploitant a CHOISI, et l'empreinte courte en
    # second — l'inverse mettrait en avant ce qu'aucun humain ne retient.
    label = ref + ((" · %s" % sha[:12]) if sha else "")
    try:
        with open(path, "w") as f:
            json.dump({"build_id": uuid.uuid4().hex, "label": label,
                       "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "git_hash": sha}, f, indent=2)
    except OSError:
        # Pas de quoi arrêter une installation par ailleurs saine.
        return
    ok(t("build_written", l=label))


def write_config_local(dest):
    """Amorce `config_local.py` (valeurs de SITE : hôtes, jetons, secrets). N'écrase pas l'existant.

    N'y écrit AUCUN réglage : `app/config.py` porte des défauts neutres, et ce fichier n'est là
    que pour les surcharger quand le site l'exige. Les trois `PROXMOX_*` vides qu'on y posait
    jusqu'au 2026-10-10 ont été retirées — le backend Proxmox a disparu et PLUS AUCUN code ne
    les lisait (seuls les deux installeurs les écrivaient encore, l'un pour l'autre).
    La docstring promettait en outre que « Proxmox vide → image_build_local s'active » : c'était
    faux. `image_build_local` est un réglage en BASE (`app/routes/images.py:_build_target`) et
    n'a jamais rien déduit de ces constantes. Une valeur morte est un piège ; une valeur morte
    documentée comme agissante en est un pire."""
    path = os.path.join(dest, "config_local.py")
    if os.path.isfile(path):
        ok(t("cfg_kept"))
        return
    with open(path, "w") as f:
        f.write(textwrap.dedent('''\
            # Valeurs de SITE de cette installation — NON versionné (cf. .gitignore).
            # Surcharge les défauts neutres de app/config.py ; vide est un état normal.
            # Rien ici ne doit être recopié ailleurs : hôtes, jetons d'agent, secrets.
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
    # Kernel: RT forced, normal forced, or nothing = install-node.sh decides (« auto », the default
    # since 2026-11-23: RT unless a DeckLink card, a reinstall keeps its kernel).
    if prof.get("rt_kernel") is True:
        args.append("--rt-kernel")                               # noyau PREEMPT_RT, fixé au démarrage
    elif prof.get("rt_kernel") is False:
        args.append("--no-rt-kernel")
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
    # BOBI_LANG: the removal scripts speak the installer's language (they were French only).
    return subprocess.call(cmd, env={**os.environ, "BOBI_LANG": LANG})


def path_uninstall(zip_path):
    """Retrait de Bobi.Studio de CETTE machine (nœud, orchestrateur, ou les deux)."""
    cible = menu_select(t("del_title"), [
        ("node", t("del_node_l"), t("del_node_d")),
        ("controller", t("del_ctrl_l"), t("del_ctrl_d")),
        ("both", t("del_both_l"), t("del_both_d")),
    ], default=0, allow_back=True, name="target")
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

    # After an inventory the installer used to QUIT: the operator had to run the curl command again
    # to get back here and remove for real (tester, 2026-11-22). It now offers the removal at once.
    real_now = False
    while True:
        if UNATTENDED:
            # Without a keyboard, a REAL removal is never a default: it takes `--confirm RETIRER`,
            # the word typed at the scripts' own prompt (they get `--yes`, which they require then).
            _c = ANSWERS.get("confirm")
            if _c not in (None, "", "RETIRER"):
                _ua_exit(t("ua_bad", v=_c, opt="--confirm", ok="RETIRER"), EXIT_MISSING)
            if _c == "RETIRER":
                dry = False
            elif ANSWERS.get("inventory"):
                dry = True
            else:
                _ua_exit(t("ua_missing", q=t("del_dryrun"), opt=t("ua_uninstall")), EXIT_MISSING)
        elif not real_now:
            dry = ask_yn_menu(t("del_dryrun"), default=True, allow_back=True, name="inventory")
        else:
            dry = False
        if dry is BACK:
            return BACK

        args_node, args_ctrl = [], []
        if not dry:
            images = ask_yn_menu(t("del_images"), default=False, allow_back=True,
                                 name="purge_images", ua_default=False)
            if images is BACK:
                return BACK
            if images:
                args_node.append("--purge-images"); args_ctrl.append("--purge-images")
            if cible in ("node", "both"):
                media = ask_yn_menu(t("del_media"), default=False, allow_back=True,
                                    name="purge_media", ua_default=False)
                if media is BACK:
                    return BACK
                if media:
                    args_node.append("--purge-media")
            if cible in ("controller", "both"):
                archive = (not ANSWERS.get("purge_data")) if UNATTENDED else \
                    ask_yn_menu(t("del_archive"), default=True, allow_back=True)
                if archive is BACK:
                    return BACK
                if not archive:
                    args_ctrl.append("--purge-data")
            if UNATTENDED:
                args_node.append("--yes"); args_ctrl.append("--yes")
            else:
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
        ok(t("del_dry_done" if dry else "del_done")) if rc == 0 else warn(t("del_failed"))
        if dry and rc == 0 and not UNATTENDED:
            real_now = ask_yn_menu(t("del_now"), default=False, allow_back=False) is True
            if real_now:
                continue
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
    # One step at a time, and ← goes back ONE step. A refused token used to send the operator
    # back to the capabilities screen, the very first question (tester, 2026-11-22): it is the
    # token that was wrong, so it is the token that is asked again, with the reason.
    step, refused = "caps", False
    while True:
        if step == "caps":
            caps = menu_multi(t("caps_title"), _cap_items(), preselected=["compute"], allow_back=True,
                              name="caps")
            if caps is BACK:
                return BACK
            step = "url"
            continue
        if step == "url":
            # La carte E810 (io2110) est auto-détectée par _enroll (driver `ice`) et le reste
            # du réseau est réglé après coup depuis l'orchestrateur → aucune saisie ici.
            url = ask(t("ctrl_url"), allow_back=True, help=t("enroll_help"), name="controller")
            step = "caps" if url is BACK else "token"
            refused = False
            continue
        aide = t("enroll_tok_help")
        if refused:
            aide = t("enroll_tok_refused", url=url) + "\n\n" + aide
        tok = ask(t("enroll_tok"), allow_back=True, help=aide, name="token")
        if tok is BACK:
            step = "url"
            continue
        args = _enroll(url, tok, caps)
        if args is None:
            if UNATTENDED:
                _ua_exit(t("ua_enroll"), 1)        # the same answers would fail again: no loop
            refused = True
            continue
        break
    run_install_node(zip_path, args)
    print()
    ok(t("node_ready"))
    info(f"agent_url : http://{_host_ip() or os.uname().nodename}:9100")
    info(t("node_register"))
    if "io2110" in caps:
        info(t("io2110_pending"))
    print(); ok(f"{BOLD}{t('install_done')}{R}"); print()
    return None


# ─── Chemins 2 & 3 : orchestrateur (bare) ± nœud local ───────────────────────
def install_controller(zip_path):
    _hdr(t("h_ctrl"))
    if os.path.isfile(os.path.join(APP_DIR, "main.py")):
        upd = ask_yn_menu(t("ctrl_exists", d=APP_DIR), default=True, allow_back=True, name="update")
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
    write_build_info(APP_DIR)
    write_config_local(APP_DIR)
    install_deps_bare(APP_DIR)
    install_clock_bare(APP_DIR)
    install_service_bare(APP_DIR)
    return None


def appliquer_reseau_conteneurs(mode):
    """Pose le MODE du réseau des conteneurs sur le nœud local. Rend (ok, détail).

    ★ POURQUOI C'EST ICI ET PAS « PLUS TARD DANS L'INTERFACE ». En mode interne, le réseau ne
    demande rien au site : ni carte parent, ni plage d'IP, ni accord de l'administrateur réseau.
    Il peut donc être créé MAINTENANT, et l'installation se termine sur un serveur réellement
    déployable. En mode site, au contraire, il faut choisir une carte et une plage — ça reste
    dans Réglages, et on le DIT au lieu de laisser croire que tout est fini.

    On passe par un processus enfant dans APP_DIR, comme `register_local_agent`, et pour les
    mêmes raisons qu'elle documente : `app` ne s'importe que depuis là, et on juge sur le
    RÉSULTAT (la colonne en base) plutôt que sur un code de sortie.
    """
    code = (
        "import os, sys, json\n"
        "from app import database as d, container_net as cn\n"
        "d.init_db()\n"
        "n = d.db_get_node_by_host('127.0.0.1')\n"
        "if not n:\n"
        "    print('aucun nœud local en base', file=sys.stderr); sys.exit(1)\n"
        "mode = os.environ['BOBI_NET_MODE']\n"
        "if mode != d.NET_MODE_INTERNE:\n"
        "    d.db_update_node(n['id'], container_net_mode=mode); sys.exit(0)\n"
        # `confirme` : le gate de `appliquer_mode_interne` refuse désormais tout geste DESTRUCTEUR
        # sans accord explicite, même sans conteneur attaché (corrigé le 2026-10-30). Ici l'accord
        # existe — l'exploitant vient de choisir le mode à l'écran précédent — et il faut le
        # passer, sinon une RÉINSTALLATION échouerait : la base survit à l'installation
        # (`_EXCLUS_ARBRE`), donc un réseau de l'installation d'avant peut diverger.
        "code, charge = cn.appliquer_mode_interne(n, n['id'], {'confirme': True})\n"
        "print(json.dumps(charge))\n"
        "sys.exit(0 if code == 200 else 1)\n"
    )
    r = subprocess.run([f"{APP_DIR}/venv/bin/python", "-c", code],
                       cwd=APP_DIR, capture_output=True, text=True,
                       env={**os.environ, "BOBI_NET_MODE": mode})
    detail = {}
    for ligne in (r.stdout or "").splitlines():
        if ligne.startswith("{"):
            try:
                detail = json.loads(ligne)
            except Exception:
                pass
    if r.returncode == 0:
        return True, detail
    return False, (detail.get("error")
                   or ((r.stderr or "").strip().splitlines() or ["?"])[-1])


def installer_plugins_requis():
    """Plugins the local node's capabilities require, fetched from the catalogue (io2110 →
    2110_io). Without it, the 2110 engine and the 2110 probe have no image to run on, and
    nothing said so (tester, 2026-11-22). Same code as an enrolment: app/plugin_autoinstall.py."""
    code = ("import json\n"
            "from app import plugin_autoinstall as p\n"
            "print(json.dumps({k: list(v) for k, v in p.ensure('install').items()}))\n")
    r = subprocess.run([f"{APP_DIR}/venv/bin/python", "-c", code], cwd=APP_DIR,
                       capture_output=True, text=True)
    res = None
    for ligne in (r.stdout or "").splitlines():
        if ligne.startswith("{"):
            try:
                res = json.loads(ligne)
            except Exception:
                pass
    if res is None:
        warn(t("plugin_auto_unknown"))
        return
    for type_, (ok_, detail) in res.items():
        if ok_:
            ok(t("plugin_auto_ok", p=type_, v=detail))
        else:
            warn(t("plugin_auto_fail", p=type_, e=detail))


def register_local_agent(token):
    """Enrôle l'agent LOCAL, et DIT la vérité sur le résultat. Rend True si la ligne existe.

    ★ CINQ DÉFAUTS CORRIGÉS ICI, tous d'une même panne : 2026-10-23, tout-en-un sur une station
    Debian 13. L'installation affichait « prêt », `bobistudio.service` et `bobi-node-agent`
    tournaient tous les deux — et la table `nodes` était VIDE. « Aucun nœud déclaré » à l'écran,
    donc rien de déployable. Les trois premiers viennent de ce que cette fonction jugeait sur le
    code de sortie d'un processus, et non sur ce qu'elle voulait obtenir.

    (a) `run()` NE FIXE PAS LE RÉPERTOIRE COURANT (`run()` ci-dessus, aucun `cwd`). L'enfant
        importe `app`, introuvable ailleurs que depuis APP_DIR : lancé depuis `/tmp` — le cas
        NORMAL quand `get.sh` déroule dans un répertoire temporaire — l'import meurt en
        `ModuleNotFoundError: No module named 'app'`.
    (b) `run(..., capture=True)` NE RENVOIE QUE `stdout`. La trace de l'erreur partait donc à la
        poubelle : c'est ce qui a rendu la panne muette. Il restait un `warn` noyé dans la sortie,
        puis « prêt » juste après.
    (c) LE CODE DE SORTIE NE DISAIT RIEN DU RÉSULTAT. `node_driver.register()` rend `(False, err)`
        SANS lever : `print(...)` sortait donc 0 même agent injoignable, et « Agent local
        enregistré » s'affichait à tort. ⚠ C'est le cas INVERSE du précédent, et il était plus
        trompeur encore — une panne annoncée comme un succès. On juge donc sur LA LIGNE EN BASE,
        qui est le but réel, pas sur « le processus a réussi à afficher un tuple ».
    (d) LE JETON ÉTAIT INTERPOLÉ DANS DU SOURCE (`'{token}'`) : une apostrophe ou un antislash
        cassait le script, ou y injectait du code. Il passe par l'ENVIRONNEMENT.
    (e) L'APPELANT ANNONÇAIT « prêt » SANS CONDITION (cf. `path_all_in_one`). Une installation
        dont le nœud manque n'est pas prête : elle est incomplète, et doit le dire.
    """
    code = (
        "import os, sys\n"
        "from app import database as d\n"
        "d.init_db()\n"
        "from app import node_driver as n\n"
        "ok, res = n.register('127.0.0.1', 9100, os.environ['BOBI_AGENT_TOKEN'], name='local')\n"
        "if not ok:\n"
        "    print('register() a refusé : %s' % (res,), file=sys.stderr)\n"
        "ligne = d.db_get_node_by_host('127.0.0.1')\n"
        "print('node_id=%s' % (ligne or {}).get('id'))\n"
        "sys.exit(0 if (ok and ligne) else 1)\n"
    )
    r = subprocess.run([f"{APP_DIR}/venv/bin/python", "-c", code],
                       cwd=APP_DIR, capture_output=True, text=True,
                       env={**os.environ, "BOBI_AGENT_TOKEN": token})
    if r.returncode == 0:
        ok(t("agent_local_ok"))
        return True
    warn(t("agent_local_bad"))
    for ligne in ((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-6:]:
        print(f"     {DIM}{ligne}{R}")
    info(t("agent_local_fix"))
    return False


def proposer_services():
    """Propose d'ALLUMER les services utiles, à la fin de l'installation.

    ★ POURQUOI (demande de Cyril, 2026-11-08). Les dix services qui portent une clé `*_enabled`
    sont TOUS éteints par défaut, et un service éteint est retiré des onglets de Réglages : rien
    dans le parcours d'installation ne dit qu'ils existent. La passerelle WebRTC en est le cas
    coûteux — sans elle le panneau de monitoring reste noir sur toutes les pages.

    ★ ON PROPOSE, ON N'ALLUME PAS D'OFFICE. Plusieurs de ces services PARLENT sur le réseau dès
    l'allumage (mDNS pour NMOS, SDP pour SAP) : les activer sans que personne ne l'ait demandé
    change le comportement réseau d'un site. C'est une décision d'exploitation, et c'est ici
    qu'on est sûr que quelqu'un regarde.

    ★ LA LISTE VIENT DES MANIFESTES (`install_offer`), pas d'ici : cf.
    `app/core_plugins.install_offers`. Un service ajouté demain se propose tout seul ; une liste
    tenue dans l'installeur aurait péri au premier.

    Non interactif (sortie tuyautée) : le repli de `menu_multi` prend les PRÉ-COCHÉS par défaut.
    """
    code = (
        "import json, os\n"
        "from app import database as d\n"
        "d.init_db()\n"
        "from app import core_plugins as cp\n"
        "from app.i18n import t\n"
        "lang = (os.environ.get('BOBI_WIZ_LANG') or 'fr')\n"
        "from app import settings as st\n"
        "out = []\n"
        "for o in cp.install_offers():\n"
        "    if st.get(o['key']):\n"
        "        continue\n"                 # déjà allumé : ne pas le proposer
        "    pourquoi = t(o['why'], lang)\n"
        "    out.append({**o, 'why_text': ('' if pourquoi == o['why'] else pourquoi)})\n"
        "print(json.dumps(out))\n"
    )
    r = subprocess.run([f"{APP_DIR}/venv/bin/python", "-c", code], cwd=APP_DIR,
                       capture_output=True, text=True,
                       env={**os.environ, "BOBI_WIZ_LANG": LANG})
    offres = None
    for ligne in (r.stdout or "").splitlines():
        if ligne.startswith("["):
            try:
                offres = json.loads(ligne)
            except Exception:
                pass
    if offres is None:
        warn(t("services_bad"))
        for ligne in (r.stderr or "").strip().splitlines()[-4:]:
            print(f"     {DIM}{ligne}{R}")
        return
    if not offres:
        return                               # rien à proposer : on ne pose pas une question vide
    items = [(o["id"], o["label"], o.get("why_text") or "") for o in offres]
    choix = menu_multi(t("services_title"), items,
                       preselected=[o["id"] for o in offres if o.get("on")], allow_back=False,
                       name="services", allow_empty=True)
    cles = [o["key"] for o in offres if o["id"] in (choix or [])]
    if not cles:
        info(t("services_none"))
        return
    code2 = (
        "import json, os\n"
        "from app import database as d\n"
        "d.init_db()\n"
        "from app import settings as st\n"
        "cles = json.loads(os.environ['BOBI_SERVICES'])\n"
        "for c in cles:\n"
        "    st.set(c, True)\n"
        # On JUGE SUR LE RÉSULTAT, pas sur le code de sortie : même leçon que
        # `register_local_agent` (un enfant qui réussit à afficher ne prouve rien).
        "print(json.dumps({c: bool(st.get(c)) for c in cles}))\n"
    )
    r2 = subprocess.run([f"{APP_DIR}/venv/bin/python", "-c", code2], cwd=APP_DIR,
                        capture_output=True, text=True,
                        env={**os.environ, "BOBI_SERVICES": json.dumps(cles)})
    etat = {}
    for ligne in (r2.stdout or "").splitlines():
        if ligne.startswith("{"):
            try:
                etat = json.loads(ligne)
            except Exception:
                pass
    poses = [c for c in cles if etat.get(c)]
    rates = [c for c in cles if not etat.get(c)]
    if poses:
        ok(t("services_ok", n=len(poses)))
    if rates:
        warn(t("services_partiel", cles=", ".join(rates)))
        for ligne in (r2.stderr or "").strip().splitlines()[-3:]:
            print(f"     {DIM}{ligne}{R}")


def dire_gestes_restants():
    """Annonce CE QUI RESTE À FAIRE, mesuré par le produit qu'on vient d'installer.

    ★ POURQUOI (2026-11-08). L'installation se terminait sur « Tout-en-un prêt » alors
    qu'AUCUNE image d'exécution n'est présente : le chemin tout-en-un n'appelle jamais
    `install-node.sh --registry`, l'absence d'image ne produit qu'un `warn` à une quarantaine
    de lignes du verdict, et ce `warn` ne nomme aucun endroit du produit. Rien n'était donc
    déployable, et le seul geste qui le débloque n'était annoncé nulle part — ni ici, ni dans
    l'assistant de premier démarrage, qui réclamait l'enrôlement d'un nœud que l'installeur
    venait justement de faire. Mesuré sur une installation d'essai en VM le même jour.

    ★ UNE SEULE SOURCE, ET C'EST LE PRODUIT. La liste et les libellés viennent de
    `app/install_gestures.py` et du catalogue i18n, via un processus enfant dans APP_DIR —
    même procédé que `register_local_agent`, et pour la même raison (`app` ne s'importe que de
    là). Recopier la liste ici la ferait mentir au premier changement : c'est exactement ce
    qu'on vient de corriger dans l'assistant.

    On ne BLOQUE rien là-dessus : un verdict d'installation ne doit pas dépendre de la
    capacité à lire une liste. Illisible → on le DIT, on n'invente pas « rien à faire ».
    """
    code = (
        "import json, os\n"
        "from app import database as d\n"
        "d.init_db()\n"
        "from app import install_gestures as g\n"
        "from app.i18n import t\n"
        "lang = (os.environ.get('BOBI_WIZ_LANG') or 'fr')\n"
        "m = g.remaining_gestures()\n"
        "out = []\n"
        "for x in (m.get('gestures') or []):\n"
        "    cle = 'wiz.done.geste_' + x['code']\n"
        "    out.append({'bloquant': bool(x.get('blocking')),\n"
        "                'texte': t(cle, lang, **(x.get('params') or {})),\n"
        "                'ou': t(cle + '_link', lang)})\n"
        "print(json.dumps({'gestes': out, 'flous': sorted({f.get('code','?') "
        "for f in (m.get('unmeasured') or [])})}))\n"
    )
    r = subprocess.run([f"{APP_DIR}/venv/bin/python", "-c", code], cwd=APP_DIR,
                       capture_output=True, text=True,
                       env={**os.environ, "BOBI_WIZ_LANG": LANG})
    charge = None
    for ligne in (r.stdout or "").splitlines():
        if ligne.startswith("{"):
            try:
                charge = json.loads(ligne)
            except Exception:
                pass
    if charge is None:
        warn(t("gestes_bad"))
        for ligne in (r.stderr or "").strip().splitlines()[-4:]:
            print(f"     {DIM}{ligne}{R}")
        return
    print()
    gestes = charge.get("gestes") or []
    bloquants = [g for g in gestes if g.get("bloquant")]
    if bloquants:
        print(f"  {BOLD}{t('gestes_title')}{R}")
        for i, g in enumerate(bloquants, 1):
            print(f"   {CYAN}{i}{R}  {g.get('texte','')}  {BOLD}{g.get('ou','')}{R}")
    else:
        ok(t("gestes_none"))
    for g in (g for g in gestes if not g.get("bloquant")):
        info(f"{g.get('texte','')}  {BOLD}{g.get('ou','')}{R}")
    if charge.get("flous"):
        warn(t("gestes_flous", codes=", ".join(charge["flous"])))


def path_controller(zip_path):
    if install_controller(zip_path) is BACK:
        return BACK
    print()
    ok(t("ctrl_ready"))
    proposer_services()
    dire_gestes_restants()
    info(t("access", url=f"{BOLD}{CYAN}http://{_host_ip() or os.uname().nodename}:5000{R}"))
    info(t("update_default"))
    print(); ok(f"{BOLD}{t('install_done')}{R}"); print()
    return None


def path_all_in_one(zip_path):
    # Capacités du nœud LOCAL (comme pour un nœud dédié) : compute/io2110/media/webrtc.
    caps = menu_multi(t("caps_title"), _cap_items(), preselected=["compute"], allow_back=True,
                      name="caps")
    if caps is BACK:
        return BACK
    if install_controller(zip_path) is BACK:
        return BACK
    token = secrets.token_hex(24)
    run_install_node(zip_path, ["--with", ",".join(caps) or "compute",
                                "--port", "9100", "--token", token])
    noeud_ok = register_local_agent(token)
    if noeud_ok:
        installer_plugins_requis()
    # Le mode réseau se demande APRÈS l'enrôlement : sans ligne de nœud en base, il n'y a rien
    # sur quoi le poser. Et on ne le demande pas du tout si l'enrôlement a échoué — ce serait
    # une question dont la réponse ne pourrait aller nulle part.
    # ★ LE VERDICT TIENT COMPTE DU RÉSEAU (corrigé le 2026-10-30). Il ne regardait que
    # `noeud_ok` : une installation dont le réseau des conteneurs n'avait PAS pu être créé
    # annonçait « Tout-en-un prêt », avec pour seule trace un `warn` noyé quelques lignes plus
    # haut. C'est exactement le défaut (e) que `register_local_agent` documente pour le nœud, et
    # il était resté en place pour le réseau — vécu sur la nouvelle Z8 le 30/10 : installation
    # déclarée prête, aucun réseau, rien de déployable.
    net_ok, net_demande = True, False
    if noeud_ok:
        mode = menu_select(t("net_title"), [
            ("interne", t("net_interne_l"), t("net_interne_d")),
            ("site", t("net_site_l"), t("net_site_d")),
        ], default=0, allow_back=False, name="net")
        net_demande = (mode == "interne")
        net_ok, detail = appliquer_reseau_conteneurs(mode)
        if mode == "interne" and net_ok:
            ok(t("net_interne_ok", name=detail.get("name", "bobi-interne"),
                 subnet=detail.get("subnet", "?")))
        elif mode == "interne":
            warn(t("net_interne_bad", err=detail))
        else:
            info(t("net_site_todo"))
    print()
    if not noeud_ok:
        warn(t("aio_partial"))
        _PARTIAL[0] = True
    elif net_demande and not net_ok:
        warn(t("aio_partial_net"))
        _PARTIAL[0] = True
    else:
        ok(t("aio_ready"))
    # Les services utiles sont TOUS éteints par défaut : on propose, à l'endroit où quelqu'un
    # regarde. Avant l'annonce des gestes, pour que celle-ci tienne compte du choix fait ici.
    proposer_services()
    # « Prêt » ne veut pas dire « déployable » : les images d'exécution ne sont pas là, et c'est
    # ici que l'exploitant regarde. Mesuré, pas supposé — cf. `dire_gestes_restants`.
    dire_gestes_restants()
    info(t("access", url=f"{BOLD}{CYAN}http://{_host_ip() or os.uname().nodename}:5000{R}"))
    info(t("update_default"))
    print(); ok(f"{BOLD}{t('install_done')}{R}"); print()
    return None


# ─── Menu / boucle principale (avec retour en arrière) ───────────────────────
def _ua_preflight(role):
    """Every answer the role will need, checked BEFORE anything is installed.

    Each question still checks its own answer when it comes; this only moves the verdict
    forward. Without it, `--role all` without `--net` installed the orchestrator, Docker and
    the agent, then stopped with "answer missing" (bench VMs, 2026-11-21). `services` is
    required even when the list offered may turn out empty: which services are offered is only
    known once the orchestrator runs, and `none` is a valid answer."""
    need = {"node": ["caps", "controller", "token"],
            "controller": ["services"],
            "all": ["caps", "net", "services"],
            "uninstall": ["target"]}[role]
    if role in ("controller", "all") and os.path.isfile(os.path.join(APP_DIR, "main.py")):
        need.append("update")
    missing = [OPTIONS[n] for n in need if ANSWERS.get(n) in (None, "", [])]
    if role == "uninstall" and not ANSWERS.get("confirm") and not ANSWERS.get("inventory"):
        missing.append(t("ua_uninstall"))
    if missing:
        _ua_exit(t("ua_missing_many", opts=", ".join(missing)), EXIT_MISSING)
    if "caps" in need:
        _ua_multi("caps", t("caps_title"), _cap_items())
    if role == "uninstall" and ANSWERS.get("confirm") not in (None, "", "RETIRER"):
        _ua_exit(t("ua_bad", v=ANSWERS["confirm"], opt="--confirm", ok="RETIRER"), EXIT_MISSING)


def _main_unattended(lang_herite):
    """Même chemin qu'au clavier, sans menu ni retour : les réponses viennent d'OPTIONS."""
    global LANG
    LANG = _ua_select("lang", "lang", [("fr", "", None), ("en", "", None)], lang_herite)
    _banner()
    info(t("ua_mode", src=ANSWERS_SRC))
    sel = _ua_value("role", t("menu_title"))
    sel = _ua_select("role", t("menu_title"), [("node", "", None), ("controller", "", None),
                                                ("all", "", None), ("uninstall", "", None)])
    _ua_preflight(sel)
    zip_path = _find_source()[1]
    {"node": path_node, "controller": path_controller, "all": path_all_in_one,
     "uninstall": path_uninstall}[sel](zip_path)
    if _PARTIAL[0]:
        _ua_exit(t("ua_partial"), EXIT_PARTIAL)


def main():
    global LANG
    # ★ NE PAS REPOSER UNE QUESTION DÉJÀ RÉPONDUE. `get.sh` demande la langue avant de nous
    # lancer et la transmet par BOBI_LANG : la redemander ici donnait deux fois la même question
    # à trois secondes d'intervalle, ce qui fait douter que la première ait servi à quelque chose.
    #
    # ⚠ SEULEMENT AU PREMIER PASSAGE. Reculer depuis le menu principal ramène ici — c'est le seul
    # moyen de changer de langue une fois entré. La sauter DÉFINITIVEMENT enfermerait dans un
    # choix fait à l'étape d'avant, éventuellement par erreur.
    _herite = (os.environ.get("BOBI_LANG") or "").strip().lower()
    _premier = _herite in ("fr", "en")
    if _premier:
        LANG = _herite
    if UNATTENDED:
        return _main_unattended(_herite if _premier else "fr")
    while True:
        # Étape 1 : langue (pas de retour avant elle).
        if _premier:
            _premier = False
        else:
            LANG = menu_select(t("lang_title"), [("fr", "Français", None), ("en", "English", None)],
                               default=(1 if LANG == "en" else 0), allow_back=False)
        _banner()
        # Étape 2 : menu principal (← revient au choix de langue ; ← d'un chemin revient ici).
        while True:
            sel = menu_select(t("menu_title"), [
                ("node", t("o_node_l"), t("o_node_d")),
                ("controller", t("o_ctrl_l"), t("o_ctrl_d")),
                ("all", t("o_all_l"), t("o_all_d")),
                ("uninstall", t("o_del_l"), t("o_del_d")),
            ], default=0, allow_back=True)
            if sel is BACK:
                break                       # ← retour au choix de la langue
            zip_path = _find_source()[1]
            res = {"node": path_node, "controller": path_controller, "all": path_all_in_one,
                   "uninstall": path_uninstall}[sel](zip_path)
            if res is BACK:
                continue                    # ← retour au menu principal
            return


if __name__ == "__main__":
    # Options first: `--help` answers without root, and a bad option costs nothing.
    ANSWERS.update(parse_args(sys.argv[1:]))
    UNATTENDED = bool(ANSWERS)
    ANSWERS_SRC = " ".join(a for a in sys.argv[1:] if a.startswith("--")) or "?"
    if os.geteuid() != 0:
        print(f"\n  {RED}{STR['fr']['must_root']} / {STR['en']['must_root']}{R}\n")
        sys.exit(1)
    try:
        main()
    except KeyboardInterrupt:
        print(); bail(STR["fr"]["cancelled"])
