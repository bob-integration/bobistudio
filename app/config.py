# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import os

# ── Valeurs par défaut ────────────────────────────────────────────────────────
# Surcharger dans config_local.py à la racine du projet (non versionné).

# B1b-2 : full-Docker, host-ops par-nœud — les hôtes vivent dans la table `nodes`. PROXMOX_* RETIRÉS.
# Plan conteneurs : subnet/passerelle/pool d'IP = cluster ; carte parent + VLAN (si trunk) = PAR-NŒUD.
# ─── Catalogue des paquets (plugins et services) ─────────────────────────────
# Organisation GitHub d'où le catalogue lit les paquets publiés. Les mises à jour
# de Bobi.Studio viennent de là, par définition ; la surcharge dans
# `config_local.py` n'existe que pour un fork ou un banc d'essai.
#
# ⚠⚠ SI VOUS CHANGEZ CETTE VALEUR, SOYEZ SÛR DE VOTRE SOURCE. Installer un plugin
# depuis le catalogue dépose son code sur le contrôleur, et son fichier `hooks.py`
# est IMPORTÉ ET EXÉCUTÉ DANS L'ORCHESTRATEUR — c'est l'unique exception à la règle
# « aucun code de plugin in-process », et elle donne à ce code les droits du
# contrôleur : la base, les jetons d'agent, le réseau de contrôle. Pointer une
# organisation dont vous ne maîtrisez pas les dépôts revient à laisser un tiers
# exécuter ce qu'il veut sur votre régie.
#
# C'est aussi pourquoi cette valeur n'est PAS un réglage d'interface : l'exposer
# dans la page Réglages ferait de la permission `settings.edit` un droit
# d'exécution de code arbitraire. La changer exige un accès au serveur — c'est
# volontaire, et c'est le seul garde-fou du mécanisme.
CATALOGUE_ORG   = "bob-integration"

CHECK_INTERVAL  = 5
DB_PATH         = "/opt/bobistudio/db_bobistudio.db"
LOG_PATH        = "/opt/bobistudio/bobistudio.log"
# Rotation du journal (anti-saturation disque). La TAILLE prime : disque borné à
# LOG_MAX_MB × (LOG_BACKUPS + 1). LOG_ROTATE_DAYS = rotation aussi par temps (0 = off). Réglables
# en base (Réglages → Système → Base / Journaux) ; ci-dessous = simples défauts.
LOG_MAX_MB      = 50
LOG_BACKUPS     = 3
LOG_ROTATE_DAYS = 7

# ── mTLS du plan de contrôle ──────────────────────────────────────────────────
# Matériel de CA interne (racine + cert contrôleur) : fichiers à droits 600, hors DB, hors git.
# Généré une fois par `tools/ca-init.py`. Répliquer ce dossier (au moins ca.key) sur le
# contrôleur standby (HA) pour qu'il puisse re-signer après un failover — jamais dans le
# snapshot SQLite. Surchargeable dans config_local.py (ex. montage chiffré dédié).
TLS_DIR         = "/opt/bobistudio/tls"

# ── Arbre servi par Flask ─────────────────────────────────────────────────────
# Racine du projet, dérivée de l'emplacement de CE fichier (qui vit dans app/). À dériver ICI et
# nulle part ailleurs : l'expression `dirname(dirname(__file__))` recopiée telle quelle dans un
# module de `app/routes/` compte un niveau de trop et désigne `app/static/uploads/`, où Flask ne
# sert rien. C'est ce qui a fait disparaître le logo d'entreprise — l'upload répondait « ok », le
# fichier était bien écrit, et l'URL enregistrée pointait sur un 404 (constaté le 2026-08-12).
BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOADS_DIR     = os.path.join(BASE_DIR, "static", "uploads")

# ── Surcharge locale ──────────────────────────────────────────────────────────
try:
    # config_local.py est à la racine du projet (dans sys.path au démarrage).
    # (Les clés Proxmox éventuelles d'un config_local.py de site sont importées mais inertes.)
    from config_local import *          # noqa: F401,F403
except ImportError:
    pass
