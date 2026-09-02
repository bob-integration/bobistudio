# Installation et mise en service — Bobi.Studio

> Guide de bout en bout : d'une machine nue à un premier flux ST 2110 qui passe. Pour le
> dimensionnement matériel (serveurs, cartes réseau, cluster), voir **`INFRASTRUCTURE.md`** —
> ce document n'aborde que le déploiement logiciel.
>
> Vocabulaire : le **contrôleur** est la machine qui exécute l'orchestrateur Flask (ce dépôt).
> Un **nœud** est une machine Debian 13 enrôlée qui exécute les conteneurs Docker de
> production, pilotée par un **agent-nœud** (`bobi-node-agent`, port 9100). Contrôleur et
> nœud peuvent être la même machine (« tout-en-un »).

## Table des matières

1. [Pré-requis](#1-pré-requis)
2. [Installation du contrôleur](#2-installation-du-contrôleur)
3. [Réglages initiaux indispensables](#3-réglages-initiaux-indispensables)
4. [Enrôlement d'un nœud](#4-enrôlement-dun-nœud)
5. [Préparation d'un nœud média (ST 2110)](#5-préparation-dun-nœud-média-st-2110)
6. [Premier flux de bout en bout](#6-premier-flux-de-bout-en-bout)
7. [Vérification et diagnostic](#7-vérification-et-diagnostic)
8. [Mise à jour et sauvegarde](#8-mise-à-jour-et-sauvegarde)

---

## 1. Pré-requis

Le matériel (serveurs, cartes réseau Intel E810 pour le plan média, topologie cluster,
commutation multicast/PTP) est détaillé dans **`INFRASTRUCTURE.md`**. Ne pas commencer une
installation sans l'avoir lu : les points d'attention ci-dessous supposent que le matériel est
déjà conforme (NIC E810/`ice` pour un nœud ST 2110, réseau multicast routé si besoin, etc.).

Résumé logiciel minimal :

- **Contrôleur** : Debian 13 (trixie) avec Python 3.13, root (ou sudo), accès réseau vers les nœuds.
- **Nœud** : Debian 13 (trixie), root, Docker (installé automatiquement par l'installeur de nœud
  s'il est absent).
- Un navigateur pour l'interface web (aucun logiciel client à installer).

---

## 2. Installation du contrôleur

Deux chemins : l'**installeur unifié** (`install.py`, recommandé — menu interactif, gère
aussi les nœuds) et l'**amorce locale** (`install.sh`, depuis une source déjà présente : clone
git, archive dépliée, clé USB). Les deux aboutissent au même résultat, et pour cause : `install.sh`
vérifie python3 — en proposant de l'installer s'il manque — puis passe la main au même
`install/install.py` : venv Python, `config_local.py`, service
systemd `bobistudio`, base SQLite initialisée.

### 2.0 Depuis GitHub, sur une machine vierge (le plus court)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobistudio/main/get.sh)
```

Équivalent, sans orchestrateur préexistant, du one-liner que sert une instance déjà installée
(`bash <(curl -fsSL http://<orchestrateur>:5000/install.sh)`). Le script **liste les versions
publiées et vous laisse choisir** (défaut : la plus récente ; `d` pour la branche de
développement), récupère la source, puis lance le même menu qu'au §2.1.

Il ne demande **ni `git`, ni paquet préconstruit** : GitHub sert une archive par dépôt, donc
`curl` et `tar` suffisent. Options : `--liste` (voir les versions sans rien installer),
`--ref <tag>` (épingler sans question), `--dry-run` (récupérer et vérifier seulement),
`GITHUB_TOKEN=…` tant que les dépôts sont privés, `BOBI_CODELOAD=` / `BOBI_API=` pour un miroir
interne ou un GitHub Enterprise.

**Ce qui est récupéré, et ce qui ne l'est pas.** Les plugins et services sont des sous-modules, et
une archive de code source GitHub ne contient pas leur contenu. `get.sh` prend donc le dépôt
principal *plus* `services/nmos`, et rien d'autre : c'est le seul service que `main.py` importe au
démarrage — un dossier vide serait traité par Python comme un *namespace package*, l'import
réussirait, le module serait creux, et le démarrage casserait plus loin sur une erreur qui ne
nomme pas la cause. Tout le reste (les autres services, tous les plugins) s'installe ensuite depuis
la **page Catalogue** de l'interface, qui lit la même organisation GitHub. C'est le chemin prévu
pour un exploitant : il n'a pas à cloner un dépôt pour ajouter un traitement vidéo.

### 2.1 Installeur unifié (recommandé)

```bash
git clone --recurse-submodules https://github.com/bob-integration/bobistudio /opt/bobistudio
cd /opt/bobistudio
python3 install/install.py
```

`install/install.py` doit être lancé **en root** (il refuse net sinon) et **à côté d'un
`bobistudio.zip`** — l'archive de distribution produite par `tools/build_dist.py`
(`python3 tools/build_dist.py --all`) sur une machine de build. Un clone git brut ne suffit
donc pas pour ce chemin : c'est le mode de déploiement pensé pour un paquet livré (avec ou
sans bundle hors-ligne, cf. §8). Si vous partez d'un clone git sans zip, utilisez
l'installation manuelle (§2.2).

Le menu (navigation clavier ↑/↓/Entrée, repli en saisie numérotée hors TTY) propose :

- **Nœud de process** : pose uniquement l'agent-nœud sur cette machine et l'**annonce** à un
  contrôleur déjà installé (adresse + jeton d'enrôlement, cf. §4).
- **Orchestrateur** : contrôleur seul (port 5000).
- **Tout-en-un** : contrôleur + agent-nœud local (loopback `127.0.0.1:9100`), pour un labo ou
  un petit site — même chemin de code qu'un nœud distant, aucun cas particulier.
- **Orchestrateur sur une VM Proxmox** : chemin hérité (`install_proxmox.py`), à ne choisir que
  pour une installation Proxmox existante.
- **Désinstaller** : retire Bobi.Studio de CETTE machine — nœud, orchestrateur, ou les deux. Le
  menu propose de commencer par un **inventaire** (aucune modification), puis pose les questions
  qui engagent (images Docker, médias, archive des données), et laisse la confirmation finale au
  script lui-même (`RETIRER` à taper). Détail des deux scripts —
  `node_agent/uninstall-node.sh` et `node_agent/uninstall-controller.sh` — dans NODE_AGENT.md §7b.
  ⚠ Retirer l'orchestrateur ne touche PAS aux nœuds enrôlés : leurs conteneurs continuent de
  tourner, et la liste de ces nœuds disparaît avec la base. Les retirer d'abord.

Piège : la fonction `write_config_local()` écrit un `config_local.py` **sans clés Proxmox**
seulement s'il n'existe pas déjà — si un `config_local.py` traîne d'une tentative précédente,
il est conservé tel quel (« config_local.py déjà présent — conservé »), y compris s'il est
incomplet.

### 2.2 Installation manuelle

```bash
cd /opt/bobistudio
cp config_local.example.py config_local.py
nano config_local.py         # valeurs de site (cf. §2.3)
bash install.sh               # amorce : python3 si besoin, puis install/install.py
systemctl start bobistudio
systemctl status bobistudio
```

`install.sh` doit être lancé **depuis la racine de la source** (il fait `cd "$(dirname "$0")"`) et
avec les droits root pour les étapes système (paquets, systemd, chrony). Il :

1. crée le venv et installe `requirements.txt` ;
2. copie `config_local.example.py` → `config_local.py` s'il est absent (sinon le garde) ;
3. installe et active le service systemd `bobistudio` ;
4. installe **chrony** et pose l'offset TAI du noyau (voir piège ci-dessous) ;
5. initialise la base SQLite si `db_bobistudio.db` n'existe pas encore.

**Piège horloge du contrôleur.** Le bus MXL indexe ses grains sur `CLOCK_TAI`
(`CLOCK_TAI = CLOCK_REALTIME + tai_offset`), et **ni `systemd-timesyncd` ni `chrony` ne posent
cet offset tout seuls** — une Debian fraîche a `tai_offset=0`, donc `CLOCK_TAI` vaut l'UTC,
soit **37 secondes** d'écart avec la grille média. `install.sh` installe chrony avec la
directive `leapseclist /usr/share/zoneinfo/leap-seconds.list` (fournie par `tzdata`) et
**vérifie** le résultat via `adjtimex` — s'il affiche `⚠ tai_offset=? au lieu de 37`, le
paquet `tzdata` n'est probablement pas à jour ou le fichier `leap-seconds.list` est absent :
il faut le corriger avant d'aller plus loin, sans quoi **toutes** les mesures d'horloge de
Réglages → Réseau → Horloges seront faussées de 37 s. Ce même piège existe côté nœud (§5), à
la différence près qu'un nœud `io2110` ne doit **pas** avoir chrony actif (l'horloge y est
disciplinée par le client PTP interne de libmtl, pas par NTP).

Le contrôleur lui-même **ne produit aucun flux MXL**, mais c'est l'horloge par laquelle
passent toutes les mesures affichées : sa dérive se reporte sur chaque ligne de la page
Horloges.

### 2.3 `config_local.py`

Fichier **non versionné**, à la racine, pour les valeurs propres au site (secrets, hôtes).
Le modèle `config_local.example.py` est volontairement quasi vide : depuis le retrait du
backend Proxmox/LXC, les hôtes vivent dans la table `nodes` (déclarés depuis l'interface,
cf. §4), pas dans un fichier. `app/config.py` porte les défauts neutres (chemins DB/logs,
`TLS_DIR`) ; toute clé posée dans `config_local.py` les surcharge à l'import.

### 2.4 Service systemd

```ini
[Service]
WorkingDirectory=/opt/bobistudio
ExecStart=/opt/bobistudio/venv/bin/python main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
```

Le journal applicatif (`/opt/bobistudio/bobistudio.log`) appartient au **handler de logging
Python**, qui l'ouvre, le fait tourner et l'archive selon Réglages → Système → Journalisation.
La sortie standard du service part, elle, dans **journald** — et surtout pas dans le même
fichier : un `append:` partagé écrit chaque ligne deux fois, et garde son descripteur à travers
la rotation, donc continue d'alimenter l'archive que plus rien ne borne.

```bash
systemctl {start|stop|restart|status} bobistudio
journalctl -u bobistudio -f     # démarrages, arrêts, et ce qui échappe au logging applicatif
tail -f /opt/bobistudio/bobistudio.log   # le journal applicatif lui-même
```

Le process écoute en **HTTP simple sur `0.0.0.0:5000`** (Waitress si disponible, sinon repli
sur le serveur de développement Werkzeug avec un avertissement dans les logs — installer
`waitress` pour la production, c'est déjà dans `requirements.txt`). Aucun TLS frontal n'est
géré par l'application elle-même : placer un reverse-proxy devant si l'exposition doit être
chiffrée/authentifiée en amont.

### 2.5 Premier compte administrateur

Deux chemins, au choix :

- **Web** : ouvrir `http://<contrôleur>:5000/`. Tant qu'**aucun** utilisateur n'existe en
  base, `/login` redirige automatiquement vers `/setup`, qui crée le premier compte (rôle
  `admin` imposé) puis enchaîne directement sur `/setup/wizard` (identité, réseau, format
  vidéo — cf. §3).
- **CLI** :
  ```bash
  ./venv/bin/python tools/create_admin.py
  ```
  Interactif (demande username/mot de passe) ou non-interactif :
  ```bash
  ./venv/bin/python tools/create_admin.py --username alice --password 'changeme'
  ./venv/bin/python tools/create_admin.py --username alice --password 'nouveau' --reset   # réinitialise un mot de passe existant
  ```

L'assistant `/setup/wizard` ne se réaffiche plus une fois terminé (réglage
`setup_completed`), sauf rappel explicite `/setup/wizard?force=1` depuis Réglages.

---

## 3. Réglages initiaux indispensables

Ces réglages n'ont **pas de valeur de site par défaut** (ou une valeur neutre qui empêchera
tout déploiement tant qu'elle n'est pas posée). À faire **avant** de déployer le moindre
conteneur — plusieurs sont lus au moment du déploiement, pas seulement à l'usage.

### Réseau conteneurs (Réglages → Réseau)

- `net_mode` (`dhcp` par défaut) et, si `static`, `ip_start`/`ip_end`/`netmask_bits`/`gateway` :
  pool d'adresses pour le réseau **macvlan** des conteneurs compute/média/webrtc. Sans ce plan
  posé cohérent avec le VLAN réellement câblé sur le nœud, la création du réseau macvlan
  échoue silencieusement côté agent (avertissement, pas d'erreur bloquante) et **aucun**
  conteneur compute ne pourra démarrer sur ce nœud.
- `mcast_pool_base` / `mcast_pool_size` / `mcast_port_default` : pool multicast ST 2110,
  alloué de façon centralisée et atomique par l'orchestrateur. Le défaut (`239.100.0.0`/4096)
  n'est valable que si ce bloc n'est pas déjà routé/utilisé ailleurs sur le réseau média du
  site — à vérifier avec l'équipe réseau avant le premier flux.
- `timezone` : vide = fuseau de l'OS du contrôleur. À poser explicitement (nom IANA,
  ex. `Europe/Paris`) si l'OS n'est pas déjà sur le bon fuseau — ce réglage pilote logs,
  horodatage des alertes et dates UI d'un seul geste, mais **ne réinterprète pas** les
  horodatages déjà en base.
- **Horloges du cluster** (Réglages → Réseau → Horloges) : poser une **source NTP commune** à
  tous les nœuds (`ntp_servers`) — vide = chaque nœud garde sa propre source, ce qui rend les
  écarts entre nœuds impossibles à diagnostiquer (deux idées différentes du temps, sans savoir
  laquelle a raison). Sans effet sur un nœud `io2110` (son heure vient du grandmaster PTP).

### PTP (Réglages → Réseau → PTP)

`ptp_enabled` est `False` par défaut et `ptp_ifname` vide : le PTP par nœud ne démarre nulle
part tant que ces réglages ne sont pas posés — un nœud `io2110` sans PTP verrouillé **peut
quand même émettre**, mais hors de la grille temporelle du cluster (dérive silencieuse, pas
d'erreur). `ptp_domain` (défaut 127, profil SMPTE 2059-2) doit correspondre au domaine du
grandmaster du site. Le profil ptp4l posé par `install-node.sh` (voir §5) est déjà aligné sur
ces mêmes valeurs — les deux sources doivent rester synchrones si l'une est modifiée après
coup.

### NMOS (Réglages → Protocoles → NMOS)

Tous les réglages `nmos_*` sont désactivés/vides par défaut (`nmos_enabled: false`,
`nmos_registry_url: ""`). Sans `nmos_registry_url` renseigné et `nmos_enabled` activé, aucun
Node/Device/Receiver/Sender n'est publié — ce n'est pas bloquant pour un flux purement
interne (câblage manuel dans Bobi.Studio), mais indispensable dès qu'un système de contrôle
externe (routeur broadcast, régie tierce) doit découvrir les flux via IS-04/05.

### Formats vidéo (Réglages → Vidéo)

`video_formats` porte une palette par défaut (test 640×360p25, HD 1080i50, UHD 2160p50…) et
`video_format_default` est **vide** — sans sélection explicite d'un format par défaut, la
palette de déploiement demande à chaque fois de choisir. À adapter à la matrice de formats
réellement utilisée sur le site (ligne `Nom;Largeur;Hauteur;FPS;Scan;Chroma;BitDepth;Colorimétrie`).

### Sauvegarde (Réglages → Système → Sauvegarde)

`backup_enabled` est `False` par défaut : sans l'activer explicitement (+ `backup_time`,
`backup_retention`), **aucune sauvegarde automatique** de la base SQLite n'est produite. Voir
§8 pour la sauvegarde manuelle.

### Alarmes (Réglages → Système → Alarmes)

`alert_webhook_enabled` est `0` par défaut : toute la chaîne d'alerte reste en **pull** (il
faut ouvrir la page pour la voir) tant qu'aucun canal (webhook, e-mail) n'est activé — c'est
la cause documentée d'une panne restée huit jours sans réaction sur une installation existante.
À activer dès la mise en service si une astreinte doit être notifiée.

---

## 4. Enrôlement d'un nœud

Un nœud = une machine Debian 13 + l'agent `bobi-node-agent` + un jeu de **capacités**
choisies à l'installation : `io2110` (E810, moteur ST 2110), `compute` (traitements numpy),
`media` (lecture/enregistrement/transcodage), `webrtc` (passerelle MediaMTX), `gpu` (pilote
NVIDIA). Les capacités sont **rattrapables après coup** (§4.3) mais jamais retirées par ce
mécanisme (pas de désinstallation automatique).

### 4.1 Installation directe (`install-node.sh`)

```bash
./node_agent/install-node.sh --with compute,media \
    --macvlan-parent eno1 --macvlan-subnet 10.x.x.0/24 --macvlan-gateway 10.x.x.254
```

```bash
./node_agent/install-node.sh --with io2110,compute --mtl-iface ens1f0np0 --ptp-domain 127 \
    --hugepages 2048 --macvlan-parent ens1f0np0 --macvlan-subnet 10.x.x.0/24 …
```

À lancer **en root**, sur la machine nœud, avec `agent.py` présent à côté du script (fourni
par le paquet de distribution ou le dossier `node_agent/`). En fin d'exécution, le script
affiche `agent_url` et le `token` généré — à saisir ensuite dans Réglages → Déploiement →
Nœuds (ou laissés à l'enrôlement zéro-touch, §4.2).

Points d'attention :

- **Réseau containers différé** : si `--macvlan-parent`/`--macvlan-subnet` sont omis, le
  script **ne crée pas** le réseau macvlan (avertissement, pas d'échec) — l'orchestrateur
  l'assignera après l'enrôlement, en choisissant la carte parent dans l'inventaire remonté par
  l'agent (Réglages → nœud → Réseau). C'est le chemin recommandé pour `io2110` : la carte
  média ne se devine pas.
- **Certificat mTLS d'un enrôlement précédent** : le script **ne vide jamais** son dossier TLS
  (il vit dans `/etc`, hors du dossier applicatif). Si un certificat d'un enrôlement antérieur
  y traîne, l'agent redémarre verrouillé sur l'**ancienne CA** et refuse tout ré-enrôlement
  vers un autre contrôleur, en silence. En TTY, le script demande s'il faut le supprimer ; en
  non-interactif, il **ne touche à rien** et le dit — utiliser `--reset-tls` pour une
  réinstallation propre, ou `--keep-tls` pour confirmer explicitement qu'on garde le
  certificat existant.
- **Docker sur Debian 13 (trixie)** : le paquet `docker.io` a été scindé — le client `docker`
  vient de `docker-cli`, le builder de `docker-buildx`, tous deux en `Recommends` seulement.
  Le script les installe explicitement ; une installation manuelle de Docker qui omettrait ces
  deux paquets laisserait le daemon tourner mais `docker build` échouerait
  (« buildx component is missing »).
- **Journal systemd durable** : le script pose un stockage persistant (`/var/log/journal`) et
  **désactive la limitation de débit** (`RateLimitBurst=0`) — sans ça, journald jette
  silencieusement les messages d'un conteneur bavard au-delà du burst, précisément les lignes
  qui expliqueraient sa panne.
- **Horloge du nœud** : même piège TAI que le contrôleur (§2.2) — sauf sur un nœud `io2110`,
  où le script **n'installe volontairement pas chrony** (l'horloge y est disciplinée par le
  client PTP interne de libmtl ; faire battre chrony ET libmtl sur la même horloge ramènerait
  `REALTIME` vers l'UTC, soit 37 s d'erreur pour le moteur, sans aucun signal).

### 4.2 Enrôlement zéro-touch (recommandé pour plusieurs nœuds)

Depuis Réglages → Déploiement → Nœuds, générer un **jeton d'enrôlement** (one-time), puis sur
le nœud vierge (via `install.py` → « Nœud de process », qui demande l'adresse du contrôleur et
ce jeton), ou en repassant le script `install-node.sh` avec les arguments renvoyés par
l'annonce (`POST /api/nodes/enroll`). Le nœud **s'annonce** au contrôleur, qui lui retourne un
profil (capacités, réseau, PTP, lcores) déjà configuré côté serveur — rien d'autre à saisir
manuellement, le nœud apparaît de lui-même dans Monitoring → Serveurs.

### 4.3 Rattraper une capacité oubliée (`--add-caps`)

```bash
./node_agent/install-node.sh --add-caps io2110 --mtl-iface ens1f0np0
```

Ne provisionne **que** la capacité demandée et **fusionne** dans `config.json` (le reste —
token, TLS, réseau déjà configuré — reste intact à l'octet près). Piloté depuis l'UI
(Réglages → nœud → « ＋ Ajouter une capacité »), qui pipe le script sur `stdin` sans dépendre
d'un fichier présent sur le nœud. Ne **jamais** relancer l'installation complète pour ajouter
une capacité à un nœud déjà enrôlé : sans reproduire tous les arguments d'origine (le parent
macvlan, par exemple, n'est pas stocké en base), elle blanchirait la configuration réseau
existante, et **sans `--token` elle régénère le token de l'agent** — le contrôleur perd le
nœud.

### 4.4 Vérifier que l'enrôlement a réussi

- Le nœud apparaît dans **Monitoring → Serveurs** avec un `last_seen` récent (heartbeat
  `GET /v1/health` périodique).
- Réglages → Déploiement → Nœuds → le nœud liste ses **capacités déclarées** — comparer à ce
  qui a réellement été demandé à l'installation ; une capacité manquante se rattrape via §4.3.
- Pour `io2110` : le vert de « Rendre Opérationnel » / « Détecter GPU » (pour `gpu`) reflète
  une **sonde réelle** (nvidia-smi + runtime + CUDA userspace pour le GPU ; hugepages/IOMMU/DDP
  pour io2110), jamais le seul succès du script d'installation — un provisioning « ✓ fait »
  peut coexister avec une capacité inutilisable si le code de sortie du script est resté à 0
  sur un avertissement non bloquant (réseau containers différé, carte E810 non fournie…).

### 4.5 Installation sans accès direct au nœud (PXE / USB / iLO)

> ⚠ **CES TROIS CHEMINS NE SONT PAS VALIDÉS DE BOUT EN BOUT. Aucun n'a encore abouti à un nœud
> réellement enrôlé.** Utilisez `install-node.sh` lancé à la main (§4.1) pour toute mise en
> service dont vous dépendez ; ce qui suit est un chantier avancé, pas un mode opératoire.
>
> État exact, au 2026-09-02 :
>
> - **PXE/UEFI HTTP** — c'est le chemin le plus avancé, et sa **chaîne firmware est prouvée** sur
>   un DL360 Gen10 : shim → grub → `grub.cfg` → noyau → initrd s'enchaînent. Ce qui n'a **jamais**
>   été exercé, c'est tout ce qui vient après : le tirage du preseed par l'installeur Debian, le
>   partitionnement, la pose des composants apt, la `late_command`, le bootstrap, puis le POST
>   d'enrôlement. Trois pièges de transport ont déjà été payés pour arriver là (keep-alive
>   HTTP/1.1 obligatoire — d'où `app/pxe_server.py` sur son propre port ; service à la RACINE et
>   non sous `/pxe` ; 404 **avec corps**, sans quoi shim reste bloqué sur la connexion).
> - **Média USB** — l'image se construit et se flashe, mais aucune installation menée jusqu'à
>   l'enrôlement n'a été observée. Même phase `d-i` non exercée que ci-dessus.
> - **Prise en main iLO** — **bloquée par la licence** sur la flotte actuelle, et ce n'est pas
>   corrigeable côté code : le Virtual Media par URL est une fonction **iLO Advanced**, un iLO
>   Standard répond `iLO.2.25.LicenseKeyRequired`. La découverte matérielle Redfish (inventaire
>   disques/NIC) et la console HTML5, elles, fonctionnent sans cette licence.
>
> Contrainte matérielle à connaître avant de tenter le test : la phase 0 repose sur l'**UEFI HTTP
> Boot avec URL explicite**, qui n'existe qu'à partir des Gen10 / 14G. Les PowerEdge R620 (12G,
> iDRAC7) ne peuvent donc pas servir de banc — leur PXE UEFI exige DHCP option 66/67 + TFTP, que
> le contrôleur ne sert pas.
>
> **Ce qui a réellement été essayé, et sur quoi.** Un seul matériel : un **HPE DL360 Gen10, iLO 5
> firmware 3.17**. Rien d'autre. Ni une autre génération d'iLO, ni **aucune version d'iDRAC**, ni
> aucun contrôleur de gestion d'un autre constructeur. Les échanges Redfish sont écrits contre ce
> seul exemplaire, et Redfish laisse aux constructeurs assez de latitude pour qu'un autre modèle
> se comporte différemment — sur l'inventaire matériel comme sur le contrôle du boot.
>
> **Vos retours nous intéressent.** Si vous tentez l'un de ces chemins, dites-nous ce qui s'est
> passé — que ça marche ou non — en ouvrant une issue :
> <https://github.com/bob-integration/bobistudio/issues>. Ce qui aide le plus : le modèle exact
> du serveur, la génération et la version de firmware du contrôleur de gestion (iLO, iDRAC,
> autre), et l'étape précise où ça s'arrête. Un échec documenté vaut mieux qu'un succès supposé :
> c'est exactement ce qui manque pour sortir cette section de l'état de chantier.

Pour les nœuds qu'on ne peut pas atteindre en SSH avant enrôlement (salle machine, pas de
clavier), trois aiguillages depuis Réglages → Déploiement → Nœuds, tous pilotés depuis un
**jeton d'enrôlement** pré-généré :

- **PXE/UEFI HTTP** (`app/pxe.py`) : le contrôleur sert `grub.cfg` + un `preseed.cfg` Debian
  gardés par le jeton — le nœud boote sur le réseau, s'installe et s'enrôle seul. Nécessite
  d'« armer » le PXE (jeton + URL contrôleur) et un boot réseau supporté côté BIOS/UEFI du
  nœud.
- **Média USB** (`app/usb_flash.py`, `app/node_iso.py`) : construit une image ISO d'installation
  pré-configurée (jeton + profil du nœud embarqués) et propose de la flasher directement sur
  un périphérique amovible détecté par le contrôleur, ou de la télécharger pour la graver
  ailleurs.
- **Prise en main iLO** (routes `/api/nodes/<id>/ilo/*`) : dépose l'ISO générée en CD/DVD
  virtuel via l'interface de gestion HPE iLO du serveur, sans média physique.

Ces trois chemins **visent** le même résultat que `install-node.sh` lancé à la main : un nœud
qui s'annonce avec le profil préparé côté contrôleur. Pas de commande à retenir ici — le détail
opérationnel (choix de l'interface, du disque, du profil réseau) se pilote entièrement depuis
l'interface web. Rappel de l'avertissement en tête de section : aucun des trois n'a encore été
mené jusqu'à un nœud enrôlé.

---

## 5. Préparation d'un nœud média (ST 2110)

Un nœud avec la capacité `io2110` a besoin de plus que l'agent : le moteur MTL/DPDK
(`2110_io`) exige un état hôte précis, vérifiable et corrigeable depuis Réglages → nœud →
Préparation MTL (`app/mtl.py:verifier`/`appliquer`, lecture seule puis application).

### Ce qui doit être vrai

1. **IOMMU actif** : `intel_iommu=on iommu=pt` dans le cmdline kernel — sans lui, DPDK ne peut
   pas mapper la mémoire des NIC en espace utilisateur.
2. **Hugepages 1G réservées au boot** — pas seulement demandées au runtime : le nombre exact
   n'est **fiable qu'au boot** (mémoire non fragmentée). `appliquer()` écrit le cmdline
   (`default_hugepagesz=1G hugepagesz=1G hugepages=N`) et **ne redémarre jamais tout seul** ;
   un redémarrage explicite est requis pour que les pages soient garanties.
3. **Cœurs isolés** (`isolcpus=domain,managed_irq`, `nohz_full`, `rcu_nocbs`) sur la bande de
   cœurs que le moteur utilisera en busy-poll — dérivée de la topologie HT réelle du nœud
   (`plan_isolation`), **jamais d'une bande fixe** : sans la carte des siblings HT (sysfs
   `thread_siblings_list` illisible, ex. conteneur restreint), le calcul **replie sur un modèle
   plat** et le signale — dans ce cas les jumeaux HT des cœurs isolés restent au housekeeping
   et continuent de recevoir les IRQ, ce qui peut faire chuter la capacité réelle du moteur.
4. **Driver `ice` (E810) avec le DDP chargé.** Sans le package DDP Intel
   (`ice_comms-*.pkg`, vendoré dans `node_agent/firmware/ice/`), le driver `ice` démarre en
   **Safe Mode** : plus d'horloge PTP matérielle (`ethtool -T` → « PTP Hardware Clock: none »,
   `ptp4l` échoue à créer l'horloge) ET plus de flow steering/RSS — la capacité `io2110` serait
   déclarée mais **inopérante**. Le DDP est lu **au probe** du driver : un reboot (déjà requis
   par ailleurs) le charge.
5. **PTP configuré et verrouillé** (`ptp4l`/`phc2sys`, profil SMPTE 2059-2 posé par
   `install-node.sh` sur l'interface E810, cf. §3). Un moteur peut démarrer sans PTP verrouillé
   — il émettra simplement hors de la grille temporelle du cluster, sans erreur visible ; c'est
   Réglages → nœud → PTP qui donne le statut de lock réel (`locked`, `gm_id`, `offset_ns`).
6. **Un noyau compatible MTL** (validé sur la pile de référence — non figé dans le dépôt,
   fourni par `--kernel-pkg`/`--kernel-apt` en réglage cluster). Sans paquet fourni, le script
   avertit et garde le noyau Debian courant : à vérifier manuellement contre la matrice de
   compatibilité MTL avant de compter sur le nœud pour de la production.
7. **Durcissement ARP** (`arp_ignore=1`, `arp_announce=2`) sur un nœud multi-homé (plusieurs
   ports RDMA agrégés) : sans lui, le nœud répond à une requête ARP pour n'importe laquelle de
   ses adresses depuis n'importe quelle interface — le pair distant apprend alors la mauvaise
   MAC, et **la moitié de la bande passante disparaît sans la moindre erreur** (deux liens
   restent « actifs », tout le trafic ressort par un seul fil). Posé inconditionnellement par
   `install-node.sh`, sans effet sur un nœud mono-port.

### Flux recommandé

1. Enrôler le nœud avec la capacité `io2110` (§4), carte E810 identifiée
   (`--mtl-iface` ou différé + choix dans l'UI).
2. Réglages → nœud → Préparation MTL → **Vérifier** (lecture seule, aucune modification) :
   lit l'état réel (IOMMU, hugepages, isolation, DDP, PTP) sans rien poser.
3. **Appliquer** si nécessaire (cmdline IOMMU + hugepages 1G + isolation + sysctl) — pose la
   configuration mais **ne redémarre pas** le nœud.
4. **Redémarrer le nœud** explicitement (bouton dédié ou `reboot` manuel) — obligatoire pour
   que cmdline, hugepages 1G et DDP prennent effet.
5. Revérifier après reboot : tous les indicateurs doivent passer au vert avant de déployer un
   moteur `2110_io` en production sur ce nœud.

---

## 6. Premier flux de bout en bout

Séquence minimale pour voir une image, une fois un nœud `compute` et un nœud `io2110` prêts
(peuvent être le même nœud) :

1. **Déployer le moteur `2110_io`** sur le nœud `io2110` depuis la palette de déploiement.
   Le moteur est **bi-rôle** (RX + TX) : une fois démarré, il expose ses entrées/sorties
   composables (flux vidéo/audio/ANC) — attendre que `:8081/status` réponde `running:true`
   avant de câbler quoi que ce soit dessus (`mtl_init` peut prendre 60-90 s sur un lien E810
   100G en cours d'entraînement — c'est `mtl_init_wait_s`, réglage par défaut 90 s, qui borne
   cette attente côté contrôleur).
2. **Déclarer une entrée** (Sources 2110 / flux composable RX) avec le multicast/port
   correspondant à une caméra ou un générateur de test déjà présent sur le réseau média — ou
   utiliser une source de test/mire interne si aucune source externe n'est encore câblée.
3. **Déployer un consommateur** simple pour vérifier visuellement le flux — `multiview` (mur
   d'images) ou `recorder` sont les plus directs pour un premier contrôle.
4. **Câbler** (page Câbles) la sortie de la source RX vers l'entrée du consommateur. Un
   contrôle de format s'applique par défaut (`wire_format_gating`) : une source dont le format
   ne correspond pas à ce qu'attend le consommateur est **refusée avec une raison explicite**
   plutôt qu'acceptée en silence — insérer un UDC si la conversion est nécessaire.
5. **Vérifier** : le badge du conteneur consommateur doit passer à `running`, ses métriques fps
   (port 8080 de son agent embarqué) doivent afficher un `fps` proche de la cadence câblée. Le
   monitoring WebRTC (panneau latéral, bouton « 📺 Monitoring » sur la page productrice) permet
   une vérification visuelle immédiate sans ouvrir une session dédiée — nécessite la passerelle
   WebRTC déployée et activée (Réglages → WebRTC).

Chaque étape se vérifie indépendamment (cf. §7) avant de passer à la suivante — un flux qui ne
passe pas se diagnostique presque toujours en isolant l'étage en cause (moteur pas prêt,
source non câblée, format incompatible, réseau multicast non routé) plutôt qu'en revérifiant
l'ensemble.

---

## 7. Vérification et diagnostic

- **Dashboard principal** (`/`) : statut de chaque conteneur (`running`, `stopped`,
  `script_stopped` — conteneur Docker up mais agent embarqué qui répond `running:false`),
  rafraîchi toutes les 5 s (`/api/containers` + `/api/alerts`).
- **Journal d'alertes** : `/api/alerts` (filtrable par `vmid`/`node_id`/`kind`) — c'est le
  fil qui remonte les décrochages de cadence, pertes PTP, nœuds tombés, échecs de sauvegarde.
- **Monitoring → Serveurs** : santé par nœud (CPU/RAM/disque, capteurs, PTP, GPU, RDMA, bande
  passante mémoire) — un nœud sans heartbeat récent (`last_seen`) est un nœud tombé, pas
  seulement lent.
- **Réglages → nœud → PTP** : statut de lock réel (`locked`, `gm_id`, `offset_ns`) — un moteur
  `io2110` peut tourner sans PTP verrouillé, ce qui n'apparaît **nulle part ailleurs** que sur
  cette page.
- **Réglages → nœud → Préparation MTL** : re-vérifier après tout changement matériel ou noyau
  (page en lecture seule, sans effet de bord — peut être relancée à volonté).
- **Logs applicatifs** : `journalctl -u bobistudio -f` (contrôleur), `journalctl -u
  bobi-node-agent -f` (agent-nœud), logs par conteneur via `/api/containers/<vmid>/logs`
  (pilote `journald` recommandé — cf. §3 — pour que les logs survivent à la destruction du
  conteneur et au reboot du nœud ; le pilote `json-file` reste borné en dur pour éviter la
  saturation disque, mais n'est pas persistant).
- **Page `/tests` (« Recette »)** : suivi de campagne de validation avec check-list — utile
  pour une mise en service structurée plutôt qu'un contrôle ad hoc.

---

## 8. Mise à jour et sauvegarde

### 8.1 Sauvegarde de la base

La base SQLite porte **tout** l'état applicatif (conteneurs, nœuds, réglages, alertes,
projets) — une sauvegarde = une copie cohérente de ce fichier unique
(`sqlite3.backup`, API en ligne, pas d'arrêt de service requis).

- **Automatique** : Réglages → Système → Sauvegarde — désactivée par défaut
  (`backup_enabled=False`, cf. §3). Une fois activée, tourne quotidiennement à `backup_time`
  (heure locale du serveur) avec rattrapage si le service redémarre après l'heure prévue, et
  purge au-delà de `backup_retention` sauvegardes conservées.
- **Manuelle** : bouton dédié dans la même page, ou directement en Python
  (`app.backup.run_backup()`).
- Les sauvegardes vivent dans `backups/` à la racine du dépôt — à répliquer hors de la
  machine si la rétention locale ne suffit pas comme politique de sauvegarde.

### 8.2 Mise à jour entre instances (push/pull)

`app/updater.py` tire une nouvelle version depuis une autre instance Bobi.Studio servant son
propre code (`update_server_enabled` + `update_token`, cf. Réglages → Système → Mise à jour) :
téléchargement de `bobistudio.zip`, vérification du **checksum sha256** contre le manifeste
annoncé, installation des dépendances Python manquantes **avant** d'appliquer quoi que ce
soit (une dépendance ajoutée par la nouvelle version qui échouerait à s'installer bloque la
mise à jour plutôt que de casser le service au redémarrage), sauvegarde du code courant, puis
extraction et redémarrage asynchrone du service.

Point d'attention : la mise à jour est **composée par instance** — le zip source contient
tous les plugins/services connus de l'émetteur, mais seuls ceux **déjà installés localement**
(ou explicitement cochés comme nouveaux composants dans l'aperçu Push/Pull) sont appliqués. Un
plugin nouveau côté source n'apparaît jamais tout seul sur un site qui ne l'avait pas.

Un **rollback** restaure la dernière sauvegarde de code (`app/updater.rollback()`) et relance
le service — à utiliser si une mise à jour laisse le service dans un état dégradé.

### 8.3 Installation sans accès Internet (bundle hors-ligne)

Pour un site sans sortie réseau vers PyPI/apt/Docker Hub, `tools/build_dist.py --offline`
(exécuté sur une machine **de build**, avec accès réseau et root pour apt) pré-télécharge :

- les roues pip de `requirements.txt` (`vendor/wheels/`) ;
- la clôture récursive des paquets système requis côté contrôleur et côté nœud
  (`vendor/debs/` + index `Packages.gz`) ;
- optionnellement, les images Docker runtime (`--images` — plusieurs Go, permet un nœud
  opérationnel sans registre).

Le paquet résultant embarque ces dossiers dans `bobistudio.zip`. `install.py` (contrôleur) et
`install-node.sh` (nœud) les **détectent automatiquement** dans l'archive extraite et basculent
en mode hors-ligne : dépôt apt local `file://` (apt ne touche que ce qui manque, zéro
downgrade de ce qui est déjà satisfait) et `pip install --no-index --find-links`. Sans ces
dossiers, les deux installeurs gardent leur chemin en ligne normal — aucune option à passer
explicitement à l'installation, la présence du bundle suffit.

**Piège de plateforme** : la machine de build hors-ligne **doit être identique** (même
Debian, même architecture, même version de Python) à la machine cible — les roues binaires et
les `.deb` sont spécifiques à la plateforme ; un bundle construit sur une autre version de
Debian échouera silencieusement à satisfaire certaines dépendances côté cible. Le noyau MTL
(`io2110`) reste **hors bundle** dans tous les cas (version-spécifique, fourni par
`--kernel-pkg`/`--kernel-apt` en réglage cluster) : un nœud `io2110` installé hors-ligne
nécessite malgré tout un accès réseau ponctuel pour ce paquet, sauf à le déposer manuellement.
