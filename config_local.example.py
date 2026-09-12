# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

# Modèle de configuration locale.
# Copier ce fichier en config_local.py et remplir les valeurs.

# Full-Docker, host-ops PAR-NŒUD : les hôtes (SSH par clé autorisée) sont déclarés via
# Déploiement → Nœuds (table `nodes`). Plus de PROXMOX_* ici.

# Réseau des containers : la carte parent macvlan ET le VLAN (si trunk) se règlent PAR NŒUD
# (carte du nœud, onglet Nœuds). Le subnet/passerelle/pool d'IP restent des réglages cluster (UI).
