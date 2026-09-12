# INFRASTRUCTURE.md — dimensionnement matériel et réseau

> Une fois le matériel choisi et câblé, la mise en service est décrite dans
> [`INSTALL.md`](INSTALL.md).

Ce document répond à une seule question : **qu'acheter et comment câbler, avant d'installer quoi
que ce soit.** Chaque contrainte citée est vérifiable dans le code (fichier, constante, garde-fou,
message d'erreur) — pas une recommandation broadcast générique. Quand une valeur n'est pas
imposée par le code mais relève d'un ordre de grandeur non vérifié, c'est dit explicitement.

Aucune valeur propre à un site (IP, hostnames, tokens) n'apparaît ici : elle vit dans
`config_local.py`, non versionné (cf. CLAUDE.md § Sécurité).

## Vue d'ensemble : trois rôles de machine

1. **Contrôleur** — Flask + SQLite, ne fait QUE piloter (déploiement, monitoring, API). N'exécute
   jamais de conteneur de production.
2. **Nœuds** — machines enrôlées qui exécutent les conteneurs Docker (moteur 2110, traitements,
   médias, GPU). Un nœud porte une ou plusieurs **capacités** (`io2110`, `compute`, `media`,
   `webrtc`, `gpu`) posées à l'enrôlement (`node_agent/install-node.sh`), ajoutables après coup
   mais **jamais retirables** ("on ne désinstalle pas un pilote, ne rend pas des hugepages, ne
   détruit pas un macvlan").
3. **Réseau** — trois plans physiquement ou logiquement distincts (contrôle, ST 2110, conteneurs
   partagés), détaillés plus bas.

---

## 1. Contrôleur

- **Rôle strictement de pilotage** : Flask (`main.py`), SQLite (`db_bobistudio.db`), thread de
  surveillance qui poll les nœuds toutes les 5 s (`CHECK_INTERVAL`). Aucune charge vidéo ne
  transite par le contrôleur — les flux ST 2110 restent sur le bus MXL et le réseau média, entre
  nœuds. Un contrôleur qui exécuterait aussi des conteneurs de production romprait l'isolation de
  panne (une charge de calcul ou une saturation réseau sur cette machine mettrait en péril le
  pilotage de toute la flotte) et n'a de toute façon **aucun besoin matériel spécial** (pas de NIC
  ST 2110, pas de GPU, pas de hugepages).
- **Dimensionnement** : rien dans le code n'exprime de minimum CPU/RAM pour le contrôleur —
  charge dominante = SQLite (connexion neuve par appel, `get_db()`) + threads de sondage HTTP vers
  les nœuds (`node_health.py`, `membw.py`, `ptp.py`) + rendu Jinja. Une VM ou un petit serveur
  généraliste suffit ; c'est un ordre de grandeur non vérifié dans le dépôt, pas un chiffre issu du
  code.
- **Disque** : SQLite + sauvegardes (`backup.py`, `HA.md` : snapshot complet poussé à intervalle
  `ha_replicate_interval_min`) + éventuels uploads (`static/uploads/`). Pas d'exigence de
  performance disque codée — pas de tmpfs ni de bus temps réel côté contrôleur.
- **Réseau** : une seule patte sur le plan de contrôle (IP statique, cf. § Réseau). Le contrôleur
  n'a pas besoin d'être raccordé au plan média ST 2110 ni au plan macvlan conteneurs.
- **Pourquoi jamais de conteneurs de production dessus** : l'allocation de cœurs isolés, les
  hugepages 1G, le pinning DPDK/AF-XDP (§ 2 et § 3) sont des ressources **exclusives** posées sur
  un nœud à l'enrôlement — le contrôleur ne les porte jamais et n'a pas vocation à les porter.

---

## 2. Nœuds de calcul — CPU, RAM, NUMA

### CPU : réservation, isolation, NUMA

- Chaque nœud publie un **pool de cœurs** (`nodes.compute_cpuset`) que `core_pool.py` répartit par
  conteneur, sans chevauchement, de façon idempotente au redéploiement (`allocate_cores`).
- **Le CPU logique 0 est systématiquement exclu du pool générique** (« c'est le cœur de service du
  noyau », `pool_par_defaut`) — sur un nœud sans capacité `io2110`, le pool part de la machine
  entière moins le CPU 0 et moins la bande isolée.
- **Empreinte du moteur ST 2110** (nœud `io2110`) : CPU 0 à `mtl_lcore_base + mtl_lcore_max + service`
  (défauts : base=1, max=16, service=1) — soit **jusqu'à 18 CPU logiques réservés au moteur** dans
  la configuration par défaut, dont les lcores busy-poll DOIVENT être isolés (`isolcpus`/`nohz_full`)
  et les cœurs de service NE DOIVENT PAS l'être (sinon les threads de service DPDK s'entassent sur
  le seul cœur non isolé restant — incident constaté, un nœud réduit à 1 cœur ordonnançable pour
  253 threads).
- **NUMA** : `core_pool.py` place préférentiellement les cœurs d'un conteneur sur un seul nœud
  NUMA, en priorisant le socket du GPU s'il y en a un. Un mauvais placement (traversée QPI/UPI)
  mesuré : latence `inputs` doublée (8,4 ms → 21,9 ms), fps divisé par ~2 sur un mur de compositing.
  **Conséquence achat** : préférer une topologie où les NIC ST 2110/RDMA et les GPU sont rattachés
  au **même socket** que les cœurs qui les servent — un serveur bi-socket mal peuplé (NIC sur un
  socket, GPU sur l'autre) dégrade mécaniquement les performances, sans qu'aucune alerte logicielle
  ne le signale avant `placement.py` (constat a posteriori, pas prévention à l'achat).
- **Hyperthreading** : `core_pool.py` est HT-aware et évite de placer un conteneur compute sur le
  sibling HT d'un lcore moteur (contention du busy-poll DPDK). HT sur les lcores eux-mêmes est une
  piste explicitement abandonnée par le projet (mémoire d'équipe : gains nuls, complexité inutile).
- **Fréquence des cœurs isolés** : une unité systemd dédiée (`bobi-cpufreq-perf.service`) épingle
  la fréquence des cœurs isolés — sans elle, `isolcpus`/`nohz_full` laisse le gouverneur cpufreq
  redescendre la fréquence en l'absence de charge perçue par le noyau, ce qui régresse le débit du
  bus MXL par hoquets. **Achat** : un CPU dont le gouverneur de fréquence peut être figé en mode
  performance (pas de dépendance à un turbo dynamique agressif imprévisible) est préférable à un
  CPU dont le comportement de fréquence est opaque.
- **Aucun minimum de cœurs par type de conteneur n'est codé en dur** — chaque plugin déclare son
  besoin (`plugin.json:resources`), confronté a posteriori par mesure continue
  (`cpu_profiles.py`, échantillonnage 60 s, fenêtre glissante 4 h) plutôt que par une table
  statique. Un type en régime **bimodal** (`split`, DVE) peut coûter jusqu'à **12×** plus cher en
  trame animée qu'en trame fixe — un dimensionnement au repos sous-estime largement le besoin réel.
- **Instructions CPU requises par le bus MXL** : `libmxl` charge une variante `x86-64-v3`
  (glibc-hwcaps, **AVX2**) si le CPU le permet, sinon une variante `baseline`. Un CPU sans AVX2 fait
  tourner cette variante baseline — le projet documente un serveur en production (architecture
  Sandy Bridge, antérieure à AVX2) devenu **inutilisable** pour le pipeline MXL de fait (pas par un
  refus explicite codé, mais par la charge que représente la variante baseline). **Achat : exiger
  AVX2 au minimum** (toute génération Haswell/2013 ou plus récente convient ; Sandy Bridge/Ivy
  Bridge à proscrire). Un garde-fou existe côté logiciel dans l'autre sens (`placement.py`,
  `variante_mxl`) : détecter un CPU AVX2-capable qui charge par erreur la variante baseline
  (surcoût mesuré ~20 % CPU) — mais rien n'empêche activement le déploiement sur un CPU sans AVX2.

### RAM et bande passante mémoire

- Le bus MXL vit dans `/dev/shm` (tmpfs = RAM). La ressource qui plafonne réellement le
  compositing multiview n'est **pas le CPU** mais la **bande passante mémoire** : un test de charge
  a montré des cœurs à ~80 % idle au moment même où le débit décroche — c'est le bus RAM qui
  sature, invisible aux compteurs CPU classiques.
- `membw.py` mesure ce paramètre en continu via un canary memcpy (128 Mo par défaut, 8 répétitions,
  toutes les 60 s), compare au meilleur débit jamais observé sur le nœud (référence apprise au
  repos), et alerte à **50 % de la référence** (warning) et **30 %** (error), avec hystérésis de
  clôture à 115 % pour éviter l'oscillation. **Conséquence achat directe** : la bande passante
  mémoire du serveur (nombre de canaux DDR peuplés, fréquence RAM, architecture NUMA) est un
  critère de sélection à part entière pour tout nœud `compute` de multiview — au même titre que le
  nombre de cœurs, sinon davantage. Aucune valeur seuil en Go/s absolue n'est codée (le seuil est
  relatif à une baseline mesurée par machine) : pas de chiffre à citer ici, seulement le principe —
  peupler **tous** les canaux mémoire du/des socket(s), pas un DIMM par canal en configuration
  dégradée.
- **Hugepages** (nœud `io2110` uniquement) : **1G exclusivement** — `default_hugepagesz=1G
  hugepagesz=1G hugepages=N`, défaut applicatif **N=16, soit 16 Go de RAM réservés en hugepages** au
  moteur ST 2110. Un résidu de configuration en hugepages 2M (`vm.nr_hugepages` d'une install
  antérieure) est réinterprété avec `default_hugepagesz=1G` comme des pages de 1G — un nœud peut se
  retrouver avec quasi toute sa RAM gelée (~2 Go libres restants) si ce garde-fou n'est pas
  respecté à l'installation. **Achat : prévoir la RAM du nœud `io2110` en plus des 16 Go (ou de la
  valeur retenue) de hugepages** — cette réservation est soustraite du pool disponible pour le
  reste du système et des autres conteneurs colocalisés.

### Disque

- **Aucune exigence de type de disque (NVMe/SSD) n'est codée dans le dépôt** (`app/`,
  `services/storage`, `services/files`, `services/media_manager` ne portent aucun contrôle de
  performance disque). Le bus MXL lui-même est en tmpfs (RAM), donc hors disque.
- Un incident documenté (mémoire d'équipe) concerne un **disque saturé par des logs Docker non
  tournés** — c'est une exigence d'exploitation (rotation de logs configurée), pas une exigence de
  performance matérielle.
- Pour le stockage média (`recorder`, `services/media_manager`, `services/storage`) : le code ne
  formule pas de contrainte de débit disque minimal. Un SSD/NVMe est un choix raisonnable pour de
  l'enregistrement broadcast multi-flux, mais ce n'est **pas un chiffre issu du code** — c'est un
  ordre de grandeur non vérifié, à traiter comme tel.

---

## 3. Cartes réseau — trois usages, trois profils différents

### Plan ST 2110 (nœuds `io2110`)

- Le plugin `2110_io` déclare explicitement `needs_nic: true, needs_dpdk: true` — **NIC dédiée,
  kernel-bypass obligatoire**, pas de carte partagée avec un autre trafic.
- **Cartes supportées** (`mtl.py`, capacité `mtl_capable`) :
  - **Intel E810** (driver `ice`) — carte de référence du projet. Plancher qualifié en TX narrow :
    **E810-C-Q2, 63 sessions par port** (64 files matérielles moins 1 de contrôle, mesuré en
    production). Une carte E810 non profilée retombe sur un plancher sûr (7 sessions annoncées,
    limite native `ice` = 8) — sous-dimensionner le nombre de flux attendus par port si la carte
    précise n'a pas été qualifiée.
  - **Mellanox/NVIDIA ConnectX-4 à ConnectX-7** (driver `mlx5_core`) — supportées par le code de
    détection de capacité, sans le même niveau de qualification chiffrée que l'E810.
- **Mode d'accès** : le moteur tourne en **AF-XDP full-PF** en régime normal (le pool SR-IOV a été
  retiré du projet — pas de VF pour le trafic média courant). Un chantier séparé (« DPDK narrow »)
  garde le PF en PTP kernel et bind une VF en `vfio-pci` pour un second moteur DPDK à basse
  latence — configuration avancée, pas le cas par défaut à l'achat.
- **Firmware DDP requis** (E810) : sans le blob DDP déposé à l'enrôlement, la carte `ice` démarre
  en **Safe Mode** — ni PTP matériel, ni steering de paquets. Un nœud `io2110` sans DDP appliqué
  n'est pas un nœud `io2110` fonctionnel.
- **BIOS/firmware serveur** : prérequis matériel signalé par le code pour les chemins DPDK/narrow —
  `MmioAbove4GB=Enabled` (Dell) ou `PciResourcePadding=High` (HPE). À vérifier et poser au BIOS
  avant l'enrôlement (le nœud le sonde en lecture seule, sans garantie de correction automatique).
- **Noyau** : la pile MTL/DPDK (ice + AF_XDP) « exige un noyau validé (6.14 sur la pile de
  référence) » — le noyau Debian stock n'apporte pas nativement cette version, un
  paquet noyau épinglé est installé si configuré à l'enrôlement.
- **IOMMU/hugepages** requis dans la cmdline noyau : `intel_iommu=on iommu=pt` en plus des hugepages
  1G (§ 2).
- **1 conteneur moteur MTL par nœud** (contrat NODE_AGENT.md), en `--network host --privileged` —
  pas de partage du moteur ST 2110 entre plusieurs instances sur le même nœud.
- **Débit** : le code ne fixe pas de débit de lien minimal en dur (10G/25G/100G) — c'est le
  nombre et la définition des flux ST 2110 portés (résolution, profondeur, fps, nombre de
  sessions simultanées) qui dimensionnent le lien, via le plancher de sessions par port cité
  plus haut. Un lien 100G a été observé en production (mémoire d'équipe, incident de link-training
  E810 100G) — présenté ici comme fait observé, pas comme prescription générale.

### Plan conteneurs partagés (macvlan)

- Une IP par conteneur sur un subnet du cluster — l'orchestrateur joint les conteneurs
  directement, l'agent-nœud ne gère que le lifecycle Docker.
- **La passerelle macvlan est auto-dérivée de la route par défaut du nœud** (`node-bootstrap.sh` :
  `ip route show default`), jamais codée en dur — la carte parente et son commutateur doivent
  fournir cette route par défaut de façon cohérente avec le subnet choisi.
- **Piège de câblage documenté** (mémoire d'équipe) : la plage IP du macvlan ne doit **pas**
  chevaucher le LAN existant du site — un chevauchement casse silencieusement le routage.
- IP réservées automatiquement exclues du pool d'allocation : passerelle, IP de contrôleur sur ce
  subnet, IP hôte de contrôle du nœud.
- Ce plan porte les conteneurs `compute`, `media`, `webrtc`, `gpu` — pas de kernel-bypass, une
  carte réseau standard convient, dimensionnée au débit agrégé des flux non-2110 attendus
  (streaming de sortie, RDMA excepté — voir § RDMA).

### Plan de contrôle

- IP statique posée au preseed pour chaque nœud, port agent-nœud **9100** par défaut (token par
  nœud). Trafic : API HTTP orchestrateur ↔ agents-nœuds, polling santé/PTP/membw. Volumétrie
  faible, aucune exigence de débit particulière trouvée dans le code — une carte de gestion
  standard 1G suffit largement au regard de la nature du trafic (API REST, pas de flux média).

---

## 4. GPU — quand, et sous quelles contraintes

- **Quand** : compositing multiview accéléré (mode GPU), kernels CUDA du plugin `split` (DVE)
  compilés à chaud par NVRTC. Un nœud porte la capacité `gpu` en plus de `compute` — le GPU n'est
  jamais le seul rôle d'un nœud.
- **Détection/télémétrie** (`gpu.py`, via `nvidia-smi`) : le module souligne explicitement que le
  goulot pertinent est le **lien PCIe** (transfert RAM↔GPU), pas le calcul SM — il relève
  `pcie.link.gen`/`pcie.link.width` en continu. Débit indicatif par génération PCIe et par voie
  (table du code) : gen1 250 Mo/s, gen2 500 Mo/s, gen3 985 Mo/s, gen4 1969 Mo/s, gen5 3938 Mo/s par
  voie — un GPU en x16 gen3 plafonne à ~15,8 Go/s utiles. **Achat : privilégier un slot PCIe pleine
  largeur (x16) et une génération récente** — un GPU bridé en x8 ou sur une génération ancienne
  réduit directement le débit de compositing utile, cohérent avec une mesure du projet montrant
  qu'un transfert PCIe generation 3 étroit dégrade nettement les performances par rapport à un GPU
  dédié moins contraint côté bus.
- **Allocation** : un GPU par vmid en round-robin par occupation (pas d'exclusivité stricte —
  time-slicing NVIDIA ; contexte mesuré ~158 Mio par conteneur, contention jugée bénigne au banc).
  Plusieurs conteneurs peuvent donc partager un même GPU physique.
- **Driver/CUDA** : l'image runtime GPU (`plugins/_compute_gpu_runtime`) embarque `cupy-cuda12x`
  épinglé, runtime CUDA 12.8 — **exige un driver NVIDIA ≥ 570** sur l'hôte (contrainte explicite du
  Dockerfile). Bancs validés dans le dépôt : Tesla T4 avec driver 550/CUDA 12.4, et une carte
  consumer avec driver 580. **Achat : vérifier la disponibilité d'un driver NVIDIA récent (≥570)
  pour le modèle de carte envisagé sur Debian** (paquets DKMS `contrib`/`non-free`/
  `non-free-firmware`) avant de choisir la carte — un GPU trop ancien pour ce plancher de driver
  est écarté de fait.
- **Installation nœud** : capacité `gpu` à l'enrôlement = pilote NVIDIA Debian DKMS + en-têtes
  noyau + `nvidia-container-toolkit` (dépôt NVIDIA, accès Internet sortant requis) + runtime Docker
  `nvidia` + image buildée localement sur le nœud. **Reboot requis** après installation.

---

## 5. RDMA — réplication inter-nœuds du bus MXL

- **Quand** : réplication de flux MXL entre nœuds (`services/rdma`), technologie `mxl-fabrics`
  (libfabric), providers `tcp` ou `verbs` (RoCEv2). Pas nécessaire pour un nœud isolé qui ne fait
  que produire/consommer localement.
- **Câblage** : conteneurs RDMA en `--network host` (le device verbs est adressé directement, pas
  via macvlan) — la carte RDMA doit donc être visible au niveau hôte, pas seulement au niveau
  conteneur. Sur les cartes Mellanox, une unité systemd dédiée (`rdma-netns-exclusive.service`)
  scope une VF mlx5 dans le netns d'un conteneur pour ce chemin.
- **Agrégation multi-liens : PAS de LACP.** Le projet a explicitement écarté le bonding LACP niveau
  noyau pour l'agrégation de liens RDMA (jugé « mort » en pratique) au profit d'une **répartition
  applicative des flux entre chemins** : chaque paire d'interfaces partageant un sous-réseau entre
  deux nœuds constitue un chemin candidat (capacité = min des vitesses des deux extrémités),
  chargé de façon dirigée (les deux sens comptent séparément), et le chemin le moins chargé et
  prouvé vivant est choisi par flux. **Conséquence câblage : deux nœuds RDMA reliés par plusieurs
  liens physiques ne doivent PAS être bondés en LACP côté OS/switch — chaque lien doit rester une
  interface distincte** pour que la répartition applicative fonctionne.
- Un port RDMA détecté "No cable" est explicitement écarté des chemins candidats — un port
  connecté mais dont l'état est indéterminé reste candidat en second choix seulement.
- Aucune exigence de type de carte (RoCE-capable spécifique) n'est listée au-delà de la mention
  `verbs`/RoCEv2 — en pratique une carte de la même famille Mellanox/ConnectX que celle validée
  pour le plan média est le choix cohérent avec le reste de la stack.

---

## 6. PTP / horloge

- **Profil** : SMPTE 2059-2 (ST 2110-10) sur `ptp4l` en BMCA actif — priorités et intervalles par
  défaut définis dans le code (`priority1=128, priority2=128, log_sync=-3, log_delay_req=-3,
  announce_timeout=3, delay_thresh=800, utc_offset=37`).
- **Réseau requis** : rien dans le code n'impose explicitement un commutateur PTP-aware
  (boundary/transparent clock) — mais le profil SMPTE 2059-2 et le seuil de verrouillage serré
  (offset < **1 ms** pour être considéré "Locked") rendent un réseau à latence/gigue maîtrisée
  indispensable en pratique. Un commutateur multi-sauts sans support PTP dégrade l'asymétrie du
  chemin aller/retour (mesure du projet : des écarts de 20-30 ms observés provenaient d'une
  **asymétrie de RTT**, pas d'une dérive d'horloge — un révélateur direct de topologie réseau
  inadaptée au PTP, pas un défaut du logiciel).
- **Horloge matérielle (PHC)** requise sur la NIC media (E810) : le moteur MTL doit tourner pour
  discipliner le PHC — l'horloge du nœud `io2110` dépend donc du moteur ST 2110 actif, pas d'un
  service NTP/PTP indépendant.
- **`io2110` désactive `chrony`** sur le nœud à l'enrôlement : deux disciplines d'horloge actives
  simultanément (chrony ramenant l'horloge REALTIME vers l'UTC, alors que le moteur porte du TAI
  via libmtl) divergeraient silencieusement de l'offset UTC/TAI (37 s à la date du profil défini
  dans le code) — à ne PAS réactiver manuellement sur un nœud `io2110`.
- **Escalade d'alerte** : `PTP_UNLOCK_ERR_S = 30 s` — au-delà de 30 s sans horloge alignée après
  perte, alerte `error` explicite (« antenne 2110 désalignée »).
- **Grand maître (GM)** : le code ne prescrit pas de modèle de GM particulier, mais un incident
  documenté (mémoire d'équipe) montre qu'un GM peut servir un service NTP source à ~100 ms
  derrière son propre PTP — vérifier, au choix du GM, que **tous** les services temps qu'il expose
  sont cohérents entre eux, pas seulement le flux PTP.

---

## 7. Multicast / IGMP

- Les flux ST 2110 utilisent des adresses multicast allouées depuis un pool dédié
  (`mcast_pool_base` par défaut `239.100.0.0`, `mcast_pool_size` par défaut **4096** adresses,
  port par défaut **5000**) — réservation atomique en base pour éviter les collisions entre
  déploiements concurrents.
- **Principe fort du code** : « la granularité d'un abonnement IGMP est le GROUPE, pas le port » —
  **une adresse multicast par flux**, pas un partage de groupe entre plusieurs flux distincts.
- **`mcast_ranges`** modélise des plages strictes par port physique / réseau logique — reflet direct
  d'un commutateur qui limite les adresses multicast autorisées par port (IGMP snooping/
  forwarding configuré côté switch). Une allocation hors plage déclarée est **refusée** par
  l'orchestrateur (le code part du principe qu'un switch réel la rejetterait physiquement).
- **Conséquence achat/config réseau** : le commutateur du plan média doit supporter l'**IGMP
  snooping** avec un **querier** actif sur le segment (sans quoi le trafic multicast serait diffusé
  en broadcast à tous les ports, ou au contraire filtré si le switch snoope sans querier) — exigence
  standard broadcast IP, confirmée ici par la modélisation explicite de plages par port dans le
  code de réservation.
- Épuisement du pool ou d'une plage → alerte `error` explicite invitant à élargir
  `mcast_pool_size` ou la plage nommée — un pool à 4096 adresses (défaut) est large, mais le
  découpage réel en plages par port/switch peut être le facteur limitant avant le pool global.

---

## 8. Redondance

### Contrôleur (HA.md)

- **Modèle : paire de contrôleurs en warm-standby, bascule MANUELLE** — pas de quorum, pas de
  failover automatique (choix assumé : "un broadcast n'a pas besoin d'un basculement automatique
  qui pourrait partir en split-brain").
- **Minimum 2 machines contrôleur**, partageant le même secret (`update_token`).
- Réplication par **snapshot SQLite complet** poussé par l'actif à intervalle réglable
  (`ha_replicate_interval_min`) — pas de réplication continue (WAL streaming). Le standby applique
  le dernier snapshot reçu seulement à la promotion.
- **VIP de management posée manuellement** (`ip addr add/del` + `arping -U`) — **pas de VRRP/
  keepalived automatique** dans l'implémentation actuelle (limite assumée, chantier séparé
  opt-in). Prévoir l'un ou l'autre process de bascule d'adresse selon la procédure d'exploitation
  retenue, mais ne pas attendre du logiciel qu'il le fasse seul.
- Le document HA.md note lui-même que le chemin de bascule **n'a pas été validé end-to-end sur un
  second boîtier physique** — testé en loopback et fichiers jetables. À traiter comme un mécanisme
  disponible mais pas éprouvé en conditions réelles multi-machines au moment de la rédaction de ce
  document.

### Flux média

- **ST 2110** : le plugin `2110_io` expose une option `smpte_2022_7` (double-chemin redondant,
  ST 2022-7) dans son schéma de configuration, **désactivée par défaut**. Une redondance de flux
  au sens SMPTE existe donc dans le produit mais n'est pas activée par défaut — l'activer implique
  de câbler deux chemins réseau physiquement distincts jusqu'à la NIC concernée (deux ports, deux
  chemins de switch), cohérent avec le principe ST 2022-7.
- Repli sans signal côté sortie (`tx_fallback`) : `none` / `black` / `bars` — un flux TX peut
  continuer d'émettre un contenu statique (noir ou barres) si la source amont disparaît, plutôt que
  de couper l'émission. C'est une redondance de *contenu*, pas de *chemin réseau*.
- Aucune redondance de nœud `io2110` (bascule automatique d'un moteur ST 2110 vers un autre nœud en
  cas de panne) n'a été trouvée dans le code parcouru — un nœud `io2110` est un point de défaillance
  unique pour les flux qu'il porte, en dehors du double-chemin réseau optionnel ST 2022-7.

---

## Checklist d'achat — synthèse

| Rôle | Point dur | Source |
|---|---|---|
| Contrôleur | Jamais de conteneur de production dessus ; pas de NIC média, pas de GPU requis ; **2 machines minimum** pour la HA (bascule manuelle) | HA.md, `main.py`, `ha.py` |
| Nœud `io2110` | CPU AVX2 obligatoire (Sandy Bridge/Ivy Bridge exclus de fait) ; **16 Go de RAM en hugepages 1G** par défaut, en plus du reste ; NIC **Intel E810** (référence, `ice`) ou Mellanox ConnectX-4→7 (`mlx5_core`) dédiée, pas partagée ; firmware DDP posé (sinon Safe Mode) ; noyau 6.14 validé ; BIOS `MmioAbove4GB`/`PciResourcePadding=High` ; **CPU 0 + bande isolée = jusqu'à ~18 cœurs logiques réservés** au moteur par défaut | `mtl.py`, `core_pool.py`, NODE_AGENT.md |
| Nœud `compute` (multiview) | **Bande passante mémoire** critique (peupler tous les canaux DDR) — plus déterminante que le nombre brut de cœurs pour le compositing ; affinité NUMA cœurs/GPU sur le même socket | `membw.py`, `core_pool.py` |
| Nœud `gpu` | Driver NVIDIA **≥ 570**, slot PCIe **x16** génération récente, `nvidia-container-toolkit`, reboot après installation | `_compute_gpu_runtime/Dockerfile`, `gpu.py`, NODE_AGENT.md |
| Nœud `rdma` | Carte RoCEv2-capable en `--network host` ; **plusieurs liens physiques distincts, jamais bondés LACP** entre deux nœuds | `services/rdma/` |
| Réseau média | Switch avec **IGMP snooping + querier actif** ; plages multicast par port cohérentes avec `mcast_ranges` ; support PTP transparent/boundary clock recommandé pour tenir l'offset < 1 ms | `allocations.py`, `ptp.py` |
| Réseau conteneurs | Plan macvlan sur un subnet dédié, **sans chevauchement avec le LAN existant** ; passerelle = route par défaut du nœud | `node-bootstrap.sh`, `allocations.py` |
| Réseau contrôle | Simple, IP statique par nœud, port agent 9100, faible débit | NODE_AGENT.md |

## Points non documentables faute d'information dans le dépôt

- Débit de lien réseau minimal pour le plan ST 2110 (10G/25G/100G) : aucun chiffre plancher codé —
  dépend entièrement du nombre et de la définition des flux portés.
- Type de disque (NVMe vs SSD vs HDD) pour le stockage média : aucune contrainte codée.
- Valeur en Go/s de bande passante mémoire minimale : le mécanisme est relatif (ratio à une
  baseline mesurée par machine), pas de seuil absolu à acheter contre.
- Modèle de commutateur PTP-aware précis ou de grand maître PTP : aucune prescription de matériel
  dans le code, seule l'exigence fonctionnelle (offset < 1 ms) est vérifiable.
- Densité RAM totale par nœud (Go) recommandée hors hugepages : non codée, dépend du nombre de
  conteneurs colocalisés et de leurs profils mesurés en production (`cpu_profiles.py` mesure a
  posteriori plutôt qu'il ne prescrit a priori).
