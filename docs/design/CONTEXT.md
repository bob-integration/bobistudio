# Contexte — Bobi.Studio

> Mis à jour 2026-11-01. Contexte métier et périmètre du projet ; le détail technique
> (architecture, structure des fichiers, conventions) vit dans `CLAUDE.md`.
> Version historique (ère Proxmox/LXC) : `old/CONTEXT-2026-05-31.md`.

## Ce qu'est le produit

Bobi.Studio est un **orchestrateur de production vidéo broadcast IP** : il pilote un
pipeline **SMPTE ST 2110** (ingest → traitements → sorties) sur un **cluster de nœuds
Docker** enrôlés, depuis une interface web unique. Cible : régies et infrastructures
de production broadcast (environnement PTP, multicast, NMOS).

## Grandes briques

- **Contrôleur** : application Flask centrale (ce dépôt), DB SQLite, rôle control-plane
  uniquement — les nœuds exécutent les conteneurs. HA possible en paire actif/standby.
- **Nœuds** : serveurs Debian enrôlés (agent-nœud HTTP, installeur dédié / PXE / iLO),
  NIC Intel E810 pour le plan média 2110 (MTL/DPDK, AF-XDP), PTP par nœud.
- **Bus MXL** : les flux vivent en mémoire partagée (`/dev/shm/mxl`, SDK MXL) ; la
  réplication inter-nœuds passe par **RDMA** (mxl-fabrics).
- **Types de containers** : tous sont des **plugins Docker** découverts au scan de `plugins/`
  (`ls plugins/*/plugin.json` fait foi, la liste varie d'une instance à l'autre). Rubriques :
  sources, traitements, composition, médias, streams, mesure. Le moteur `2110_io` (ST 2110
  bi-rôle RX+TX, kernel-bypass) est le seul à porter l'entrée/sortie du signal ; les autres
  se contentent du bus MXL.
- **Services** (`services/`, sous-modules ; `ls services` fait foi) : NMOS (IS-04/05, IS-12,
  IS-14…), Ember+, TSL, ATEM, Skaarhoj, SAP/SDP, SNMP, alerting, passerelle WebRTC (MediaMTX),
  RDMA, fichiers/médias, stockage, console audio (`audio`, `control_surface`, `tts`).

## État du chantier (juillet 2026, repris le 2026-11-01 pour les listes ci-dessus)

- Migration **full-Docker terminée** (2026-06-16) : plus de Proxmox/LXC nulle part.
- Migration **SDK MXL terminée** (vidéo planar, audio, ANC) pour tous les plugins.
- Chaîne **entrelacée champ-natif** (1080i) fonctionnelle RX→traitements→TX.
- Multi-NIC 2110 (auto-répartition + épinglage), allocation multicast centralisée,
  monitoring multi-nœuds (PTP, RDMA, GPU, santé matérielle), i18n FR/EN.
- Une **installation de production** existe en parallèle du serveur de développement ;
  la page `/tests` (« Recette ») suit la campagne de validation avec l'équipe.

## Environnement de travail

- Dépôt : `/opt/bobistudio` (module Python `app/` ; plugins et services sont des
  sous-modules git, cf. `git submodule status`). Lancement : `./venv/bin/python main.py` (port 5000).
- Les valeurs propres au site (hôtes, tokens, secrets) vivent dans `config_local.py`
  (non versionné) — ne jamais les recopier dans la documentation.
- Convention de code française historique ; nouveau code (plugins, services, UI
  récente) en anglais ; toute chaîne UI passe par l'i18n (`_()` / `t()`).
