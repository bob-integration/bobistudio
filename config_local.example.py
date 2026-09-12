***REMOVED*** SPDX-License-Identifier: GPL-3.0-or-later
***REMOVED*** Copyright (C) 2026 BOBI SAS, France
***REMOVED*** Auteur : Cyril Mazouer, pour le compte de BOBI SAS
***REMOVED*** Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

***REMOVED*** Modèle de configuration locale.
***REMOVED*** Copier ce fichier en config_local.py et remplir les valeurs.

***REMOVED*** Full-Docker, host-ops PAR-NŒUD : les hôtes (SSH par clé autorisée) sont déclarés via
***REMOVED*** Déploiement → Nœuds (table `nodes`). Plus de PROXMOX_* ici.

***REMOVED*** Réseau des containers : la carte parent macvlan ET le VLAN (si trunk) se règlent PAR NŒUD
***REMOVED*** (carte du nœud, onglet Nœuds). Le subnet/passerelle/pool d'IP restent des réglages cluster (UI).
