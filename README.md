# Bobi.Studio

> ## Version bêta
>
> Bobi.Studio tourne en production, quotidiennement, chez son éditeur. Cette publication est
> la première hors de ce cadre : votre installation sera la première sur un autre parc.
>
> Deux choses à savoir avant de commencer :
>
> - **Les API ne sont pas encore figées** : le proxy de plugins, les endpoints de contrôle et
>   le format `.mxlplugin` peuvent évoluer. Les publications sont datées plutôt que numérotées
>   tant que ce périmètre n'est pas arrêté — un numéro sémantique engagerait une compatibilité
>   qu'on préfère promettre une fois pour toutes, et la tenir.
> - **Éprouvez-le hors antenne d'abord.** C'est un produit de diffusion : ce qui s'y casse se
>   voit en direct.
>
> Les retours nous sont utiles, et particulièrement deux : ce qui bloque à l'installation, et
> ce que la documentation ne dit pas. Ouvrez une issue.

Interface web d'orchestration du pipeline vidéo **ST 2110** en architecture **full-Docker**.
Un orchestrateur Flask central (contrôleur) pilote des **nœuds** enrôlés qui exécutent les
conteneurs de production : réception/émission 2110, mixage, multiview, encodage, enregistrement.
Le transport vidéo/audio interne passe par le **bus MXL** (SDK MXL, mémoire partagée `/dev/shm/mxl`).

## Fonctionnalités principales

- **Moteur ST 2110 bi-rôle** (`2110_io`) : réception + émission via MTL/DPDK (AF-XDP, kernel-bypass), flux composables vidéo/audio/ANC, classes d'émission narrow/wide, chaîne entrelacée champ-natif
- **Streams** : encodage multi-destinations (UDP / SRT / WebRTC), audio multi-pistes, preview WHEP
- **Système de plugins** : chaque type de container est un plugin versionné (`plugins/<type>/`) — déployable, mis à jour et distribué indépendamment
- **Câblage live** : page Câbles avec topologie graphique, bascule de source à chaud, insertion automatique d'UDC en cas de formats incompatibles
- **NMOS** : **IS-04** (enregistrement/découverte), **IS-05** (connexion), **IS-07** (événements — client, entrant et sortant), **IS-09** (configuration système), **IS-12** (contrôle MS-05-02 sur WebSocket) et **IS-14** (le même modèle en REST, avec sauvegarde/restauration `bulkProperties`). Plus les BCP : **002-01/02** (grouping et identité d'asset), **003-02** (autorisation), **004-01** (capacités de récepteur), **008-01/02** (moniteurs de santé des Rx/Tx)
- **Autres protocoles** : SAP/SDP (annonce et découverte AES67/Ravenna), Ember+, TSL 5.0 (tally/UMD), SNMPv3 en lecture seule avec traps, PTP par nœud (ptp4l/phc2sys)
- **Pupitres** : émulateur switcher ATEM (UDP 9910) + intégration Skaarhoj Quick Bar (Raw Panel Protocol TCP, presets nommés, routing XY)
- **Emplacements** : identité **fonctionnelle** de production, stable au remplacement du conteneur — c'est elle qu'adressent les systèmes de contrôle externes (arbre Ember+)
- **Macros et déclencheurs** : enchaînements d'actions et de paramètres continus sur les conteneurs d'un projet, toute capacité de plugin étant pilotable sans passer par l'interface
- **Monitoring WebRTC** : panneau latéral global par utilisateur (flux MediaMTX embarqué), boutons Monitoring sur chaque page productrice
- **Multi-nœuds** : enrôlement de nœuds Docker (agent-nœud), réplication **RDMA** inter-nœuds du bus MXL, allocation multicast centralisée, haute disponibilité warm-standby (cf. `HA.md`)
- **Ressources mesurées** : profils CPU par type de conteneur, allocation de cœurs isolés consciente du NUMA, étalonnage en charge réelle et constat du placement effectif — pas d'allocation à l'estime
- **Santé de la flotte** : supervision par nœud (CPU, RAM, disque, capteurs, PTP, GPU, RDMA, bande passante mémoire), détection de nœud tombé, alertes contextualisées et notifications e-mail
- **Installation des nœuds** : installeur dédié, démarrage réseau PXE/UEFI HTTP ou média USB, prise en main iLO — enrôlement sans clé USB
- **Journalisation** : logs des conteneurs, journal d'exploitation et piste d'audit des actions utilisateurs
- **Projets** : snapshots du câblage et de la configuration, rappel avec progression, accès restreint par utilisateur
- **i18n** : interface bilingue FR/EN

## Architecture

- **Contrôleur** : application Flask (port 5000) + thread de surveillance. Aucun conteneur de production ne tourne dessus (sauf mode « tout-en-un »).
- **Nœuds** : machines Debian enrôlées (table `nodes`), pilotées par `app/node_driver.py` via un **agent-nœud** HTTP (`node_agent/agent.py`, token par nœud) qui gère le lifecycle Docker et les opérations hôte.
- **Conteneurs** : créés par `app/docker_driver.py` (moteur ST 2110 MTL, NIC dédiée AF-XDP) et `app/docker_compute.py` (compute/média, réseau **macvlan** — une IP par conteneur).

## Documentation

| Document | Pour qui |
|---|---|
| [`INFRASTRUCTURE.md`](INFRASTRUCTURE.md) | **Avant d'acheter** : choix CPU, RAM et bande passante mémoire, cartes réseau, GPU, RDMA, PTP, les trois plans réseau, redondance |
| [`INSTALL.md`](INSTALL.md) | **Mise en service** : de la machine nue au premier flux, avec les points d'attention qui font échouer une installation |
| [`NODE_AGENT.md`](NODE_AGENT.md) | Contrat HTTP de l'agent-nœud |
| [`HA.md`](HA.md) | Haute disponibilité du contrôleur (warm-standby) |
| [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) | Composants tiers et licences |

Ces documents sont aussi rendus **dans l'interface**, page **Aide** — même source, pas de copie
à maintenir. L'aide en ligne couvre en plus une rubrique par plugin (`plugins/<type>/help.md`).

## Déploiement

### Sur une machine vierge, en une commande

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobistudio/main/get.sh)
```

C'est le chemin le plus court, et il ne demande **ni `git`, ni paquet préconstruit** : GitHub
sert une archive par dépôt, donc `curl` et `tar` suffisent. Le script liste les versions
publiées et vous laisse choisir (défaut : la plus récente ; `d` pour la branche de
développement), récupère la source, puis ouvre le menu de l'installeur unifié décrit ci-dessous.

Options utiles : `--liste` affiche les versions disponibles et s'arrête, `--ref <tag|branche>`
en vise une précise sans passer par le menu, et `--dry-run` récupère et vérifie la source **sans
rien installer** — de quoi regarder avant de se lancer. Détail complet dans
[INSTALL.md](INSTALL.md#20-depuis-github-sur-une-machine-vierge-le-plus-court).

### Installeur unifié

`install.py` s'exécute **en root, à côté d'un `bobistudio.zip`** : c'est un installeur de
paquet, pas un script à lancer depuis un clone git. `get.sh` ci-dessus le met en place pour
vous ; sinon, deux façons de l'obtenir :

```bash
# Depuis un contrôleur déjà en service (l'installeur et le zip y sont servis) :
curl -O http://<controleur>:5000/install/install.py
curl -O http://<controleur>:5000/install/bobistudio.zip
sudo python3 install.py

# Ou en construisant le paquet depuis les sources :
python3 tools/build_dist.py      # produit dist/bobistudio.zip + dist/install.py
cd dist && sudo python3 install.py
```

Menu interactif (stdlib uniquement) qui provisionne la machine en :
**nœud de process** (agent-nœud seul), **orchestrateur** (contrôleur Flask),
ou **tout-en-un** (orchestrateur + nœud local).

Pour installer **depuis un clone git**, lancer `bash install.sh` : l'amorce vérifie python3 (et propose de l'installer), puis passe la main à l'installeur unifié `install/install.py`.

### Installation manuelle du contrôleur

```bash
cd /opt/bobistudio
cp config_local.example.py config_local.py
nano config_local.py     # valeurs de site (hôtes, tokens, secrets)
bash install.sh          # venv + dépendances + service systemd
systemctl start bobistudio
```

### Enrôler un nœud

```bash
# Sur la machine nœud (Debian/Ubuntu), capacités à la carte :
./node_agent/install-node.sh --with compute,media \
    --macvlan-parent eno1 --macvlan-subnet 10.x.x.0/24 --macvlan-gateway 10.x.x.254
```

Puis déclarer le nœud (URL + token affiché en fin d'installation) dans l'interface.

### Gestion du service

```bash
systemctl {start|stop|restart|status} bobistudio
journalctl -u bobistudio -f    # logs en direct
```

### Lancement manuel (dev)

```bash
./venv/bin/python main.py      # Flask sur 0.0.0.0:5000
```

## Configuration

| Fichier | Rôle |
|---|---|
| `config_local.py` | Valeurs propres au site (hôtes, tokens, secrets) — **non versionné** |
| `config_local.example.py` | Modèle à copier |
| `app/config.py` | Valeurs par défaut neutres (sans secrets) |

Les autres réglages (réseau ST 2110, NMOS, TSL, WebRTC, PTP, thème, utilisateurs…) se
configurent depuis l'interface web — **Réglages**.

## Structure

```
main.py               ← point d'entrée Flask (port 5000) + thread de surveillance
app/                  ← modules Python (deploy, plugins, node_driver, docker_driver, macros,
                        core_pool/placement, node_health, ptp, ha…) + app/routes/ (API REST)
plugins/              ← un sous-module git par type de container (plugin.json + script.py + UI)
                        + dossiers `_*_runtime` (images Docker partagées, pas des plugins)
services/             ← services orchestrateur (nmos, emberplus, tsl, atem, skaarhoj, rdma,
                        alerting, files, media_manager, storage, webrtc_gateway…)
node_agent/           ← agent-nœud (agent.py) + installeur de nœud (install-node.sh) + iso/
templates/            ← vues HTML Jinja2
static/               ← scripts.js, CSS (base.css, nav.css, themes), uploads/
i18n/                 ← catalogues de traduction (fr.json, en.json)
script_templates/     ← agent.py (agent par-conteneur), bobimxl.py (SDK MXL), mxl_bench.py
tools/                ← utilitaires (create_admin.py, build_dist.py…)
docs/design/          ← charte UI, doctrine produit, contexte métier
docs/reference/       ← documents de référence qui font foi (layouts TX, modèle d'horloge,
                        interop MXL, sonde 2110, projets)
docs/chantiers/       ← journaux de chantier datés (mesures, portages, décisions)
docs/design/DESIGN.md             ← charte UI / système de thèmes
docs/design/CONTEXT.md            ← contexte métier et périmètre
docs/design/PRODUCT.md            ← doctrine produit et UX
HA.md                 ← haute disponibilité (warm-standby)
NODE_AGENT.md         ← contrat HTTP de l'agent-nœud
CHANGELOG.md          ← historique des versions (rendu sur la page Aide)
```

## Créer un compte administrateur

```bash
./venv/bin/python tools/create_admin.py
```

## Prérequis infrastructure

- Contrôleur : Debian avec Python 3.13 (VM ou machine dédiée)
- Nœuds : Debian/Ubuntu avec Docker ; pour le rôle `io2110`, NIC Intel E810 (MTL/DPDK AF-XDP) + hugepages + PTP
- Réseau : un plan média ST 2110, un segment macvlan pour les conteneurs compute/média, un plan de contrôle
- Pour WebRTC : la passerelle MediaMTX se déploie depuis Réglages → WebRTC

## Licence

Copyright (C) 2026 BOBI SAS, France
Auteur : Cyril Mazouer, pour le compte de BOBI SAS.

Bobi.Studio est un logiciel libre : vous pouvez le redistribuer et/ou le modifier
selon les termes de la **GNU General Public License version 3** telle que publiée par
la Free Software Foundation, soit la version 3 de la licence, soit (à votre choix)
toute version ultérieure.

Ce programme est distribué dans l'espoir qu'il sera utile, mais SANS AUCUNE GARANTIE ;
sans même la garantie implicite de QUALITÉ MARCHANDE ou d'ADÉQUATION À UN USAGE
PARTICULIER. Voir la GNU General Public License pour plus de détails.

Le texte complet est dans le fichier [`LICENSE`](LICENSE) ou sur
<https://www.gnu.org/licenses/gpl-3.0.html>.

### Composants tiers

Bobi.Studio intègre des composants tiers (SDK MXL, Intel Media Transport Library, DPDK,
FFmpeg, MediaMTX…) qui restent soumis à leur propre licence. L'inventaire, les mentions de
copyright à conserver et les points de vigilance sont dans
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

## Développement

Ce projet a été développé avec l'assistance de Claude (Anthropic)
comme outil de génération de code, sous la direction et supervision
de Cyril Mazouer.
