# Socle narrow DPDK — doc de référence du chantier

Branche : `feat/2110-dpdk-narrow` (parent) + sous-module `plugins/2110_io`.
Objectif du chantier : faire tourner le moteur ST 2110 (`2110_io`) en **narrow 2110-21 matériellement
garanti**, avec **PTP fiable** et **latence minimale**, en production, sans SR-IOV.

> Ce document synthétise l'état **au 2026-07-09/10** (commits `988a2a8`/`e965a12` parent,
> `bf20ea0`/`b6b814e` sous-module, image `bobi-mtl` 0.39.13). Il remplace les notes de travail
> antérieures. `docs/chantiers/SRIOV_IMPL.md` conserve les faits durs SR-IOV mais son architecture est
> **abandonnée** (bandeau superseded). L'analyseur de conformité est décrit dans `docs/reference/PROBE_2110.md`.

---

## 1. Décision socle : full-PF DPDK

**Le média tourne sur la PF PLEINE en DPDK/vfio** (driver `ice` en vfio-pci, moteur MTL user-space),
avec pacing **RL (rate-limit matériel)** pour le narrow. Conséquences :

- **RL narrow HW = PF-only.** Le SR-IOV a été abandonné (2026-07-09) : sur la vraie VF iavf,
  `pacing=rl` **SEGFAULTE** (`rte_tm` de l'iavf cassé, `testpmd show port tm cap` hange sur la VF),
  seul `pacing=tsc` tourne. Les anciennes mesures « RL OK sur VF » étaient faites sur PF-vfio, pas
  sur la vraie VF. → le narrow matériel n'existe que sur la **PF ice**.
- **1 IP par port** (fin de la prolifération d'IP du multi-VF : plus de PF-IP + VF-IP).
- Capacité RL sur PF ice = **~128 feuilles** possibles (mur pratique mesuré 63-64, cf. §4) → 2022-7
  tient sur une seule carte.
- **PTP découplé de la carte média impossible en SR-IOV** l'était par une PF kernel ; ici la PF est
  en vfio (pas de netdev) → le PTP passe par le **PTP interne libmtl** (§2).

| Mode datapath | PF | PTP | Narrow HW (RL) | Statut |
|---|---|---|---|---|
| `af_xdp` (prod actuelle) | kernel `ice` | ptp4l/phc2sys kernel | non (TSC soft) | défaut, intouché |
| **`dpdk` (socle narrow)** | **vfio, MTL user** | **PTP interne libmtl** | **oui** | **cible** |
| `sriov` | kernel + VF vfio | ptp4l kernel (PF) | **non (crash iavf)** | abandonné |

---

## 2. PTP carte-directe (`ENGINE_PTP=libmtl`)

En full-PF DPDK, **il n'y a plus de netdev kernel** sur la PF média → plus de `ptp4l`/`phc2sys`
kernel. Le moteur fait donc **son propre PTP** : libmtl devient **esclave PTPv2** sur le port DPDK
(`MTL_FLAG_PTP_ENABLE | MTL_FLAG_PTP_PI`), discipline+lit le PHC via `ptp_from_eth`. MTL s'auto-
configure (domaine 127, transport, join `224.0.1.129`) à partir du GM.

- `ENGINE_PTP=libmtl` → active le PTP interne (mtl_rx.c:1773).
- `ENGINE_PHC2SYS=1` → libmtl discipline **AUSSI `CLOCK_REALTIME`** depuis le PHC. **Indispensable** :
  toute la flotte MXL lit `CLOCK_REALTIME` (`bobimxl.now_tai`) pour indexer les grains (`media_ts`).
  Sans lui le moteur serait synchro mais pas le reste du nœud.
- Défaut OFF (0.39.11) : le mode historique lit `CLOCK_REALTIME` discipliné par le kernel.

### Les deux blocages RX résolus (patchs libmtl vendorés, 0.39.12/0.39.13)

Le PTP interne ne recevait **aucun paquet** sur E810/ice DPDK. Deux causes distinctes, chacune
prouvée strictement nécessaire par ablation (banc dl360-1 2026-07-09) :

1. **Option IP Router Alert (RFC 2113)** — `patch_igmp_router_alert.py` (`mt_mcast.c`).
   Les reports IGMPv3 forgés par libmtl avaient un en-tête IP 20 octets (IHL=5, **sans** option).
   Le kernel Linux met **toujours** le Router Alert ; un switch à IGMP snooping conforme (le Cisco
   du plant) **ignore** un report sans Router Alert → ne snoope pas le join → ne forwarde pas le
   mcast (dont le PTP) à la PF DPDK. Symptôme : `ptp4l` kernel locke (14 ns) mais le PTP libmtl reste
   « not connected » sur le même port/sip. Capture : report kernel IHL=24 + `94040000`, report libmtl
   IHL=20 absent (seul delta). Fix : IHL=6, option `0x94 0x04 0x00 0x00`, IGMP décalé de 4,
   `total_length +4`, `l3_len += 4`. **Sans → rx 6, pas de lock.**

2. **Règle rte_flow PTP → queue CNI** — `patch_ptp_mcast_flow.py` (« Fix A », `mt_ptp.c` + `mt_main.h`).
   Sur E810/ice, c'est une **règle rte_flow** (dst-IP mcast + UDP dst-port → QUEUE) qui admet le mcast
   dans le pipeline NIC. Une session `st20p_rx` en pose une et reçoit ; mais la branche CNI de
   `ptp_init` ne faisait que `mt_mcast_join` + `mt_mcast_l2_join`, **aucune** règle rte_flow (la queue
   système CNI saute la création de flow) → le PTP n'est jamais steeré vers le tasklet CNI. Fix : après
   `mt_mcast_l2_join`, poser 2 règles rte_flow (event 319 / general 320) vers la rxq CNI via
   `mt_rx_flow_create`, libérées dans `ptp_uinit`. **Sans → rx 8, pas de lock.**

### Admission du mcast : `NIC_PROMISCUOUS=1`

Sur ice DPDK, **ni `mac_addr_add` ni `set_mc_addr_list` n'admettent le mcast** au niveau port
(`rx_packets=0`). L'ablation (dl360-1 2026-07-09) désigne `MTL_FLAG_NIC_RX_PROMISCUOUS`
(env `NIC_PROMISCUOUS=1`, mtl_rx.c:1752) comme **le seul mécanisme** qui laisse passer le mcast.
**« Fix B » = `set_mc_addr_list` → dead-end** (essayé, n'admet pas). Posé automatiquement par
l'orchestrateur sur toute PF dpdk (§5). Sans objet en AF-XDP (le noyau programme le filtre).

### Ablation — les 3 leviers sont tous nécessaires
Router Alert **ET** rte_flow PTP→CNI **ET** promiscuous : retirer l'un → pas de lock RX PTP.

### Résultat mesuré
Lock **~30 ns** au grandmaster, validé live sur le banc. `ptp4l` kernel n'est plus dans la boucle
(la PF est en vfio, pas de netdev).

### ⚠ Piège diagnostic
Un **SPAN switch résiduel** (Eth1/53 en source, laissé `admin shut`) cassait la réception du port et
avait **faussé tout le diagnostic amont**. Le retirer a débloqué le forward. → toujours vérifier
qu'aucun SPAN/mirror ne pollue le port avant de conclure un `rx=0`.

---

## 3. Conformité 2110-21

Mesure via la **sonde embarquée** (`TIMING_PARSER=1`, 0.39.4) : `MTL_FLAG_ENABLE_HW_TIMESTAMP` +
`ST20P_RX_FLAG_TIMING_PARSER_META` par session → Cinst/VRX/FPT/latency par trame + verdict
`narrow|wide|failed` sur `:8080` (cf. `docs/reference/PROBE_2110.md`). Le verdict de fenêtre = le PIRE observé.

- **Narrow gapping PROUVÉ sur socle réel** : banc GM bi-port (2026-07-10, TX narrow RL sur `ens1f1np1`
  + sonde DPDK/vfio sur `ens1f0np0` joignant `239.0.0.1:20000`, les DEUX ports PTP-lockés au GM
  ~30-35 ns, concordance inter-ports ~23 µs) : **`cinst_max=1`, `vrx_span=4-5`**. Le flux **EST narrow**
  — identique à l'étalon RL loopback (cinst 1-2 / vrx_span 4-5). C'est le neuf : le narrow tient avec la
  VRAIE horloge GM, pas seulement des horloges libres de loopback.
- **Le `fpt` absolu exige une source genlockée à l'epoch PTP — NON atteint sur le banc synthétique :**
  - Notre banc GM bi-port : bien que les 2 ports soient PTP-lockés au GM (~30 ns), le `fpt` reste
    **14-15 ms (`compliant=failed`, cause `fpt exceed tr_offset`)** parce que la SOURCE du banc
    (`mxl_bench` writer **free-run** ; `index_mode=tai` n'y change rien → `mtl_rx` TX ignore l'epoch du
    grain) **n'est pas genlockée** : le TX démarre la trame quand un grain arrive, pas à l'epoch. Le
    gapping intra-trame (cinst/vrx_span) reste parfait — il est **invariant à la phase** — seul l'absolu
    est faux. **C'est une limite du BANC synthétique, pas du socle.**
  - En **PROD** (le vrai moteur genlocke via `media_ts`), le `fpt` a été mesuré **~782 µs ≈ tr_offset**
    (1080p50 = 782 222 ns) — conformité absolue acquise SÉPARÉMENT (cf. mémoire
    `dpdk-narrow-lowlatency-chantier`). Les deux pièces se rejoignent : **gapping narrow (banc réel) +
    fpt absolu (prod)**. Reste ouvert : une mesure conformité ABSOLUE **unifiée** sur le socle exige un
    TX genlocké — le banc `mtl_rx`/`mxl_bench` free-run ne le fournit pas (cf. §7).
- **Mécanisme vs classe** : le VRX filaire est dominé par le **mécanisme** (rl/tsc/tsc_narrow) ;
  la **classe** 2110-21 par session (`ops.transport_pacing`, #26) pose la cible/budget VRX interne
  (log-observable, VRX interne wide=130 vs narrow=8), sans élargir proportionnellement l'émission
  réelle en loopback. La preuve #26 repose sur le log libmtl + le VRX interne, pas sur le span sonde.

---

## 4. Capacité

- **Mur des 8 files TX RL LEVÉ** (0.39.6, `patch_tm_hierarchy.py`). Le « 8 » n'était pas une limite
  E810 mais la **forme** de l'arbre TM construit par libmtl (toutes les feuilles sous un unique nœud
  queue-group → fan-out 8, 9ᵉ file rejetée `ice_tx_queue_start: Failed to add lan txq`). Patch : arbre
  **ramifié** (P = ceil(nb_tx_q/8) nœuds QG, feuilles réparties) → jusqu'à `MT_MAX_RL_ITEMS=128`
  RL/port. Clamp controller `RL_TX_QUEUES_CAP=63` (plafond HW réel E810 = 64 files).
- **~63 senders narrow RL/PF** (mur HW 64 leaves, cap gracieux). Banc dl360-1 : arbre 3 parents à
  tx_queues=18, max 63 ; rafale 16 TX RL + RX même port stable 170 s, 0 crash.
- **RX+TX RL sur le même port** débloqué (0.39.5, `patch_rx_resetting_guard.py`) : le commit RL stoppe
  le port (`dev_started=0`) le temps du `rte_tm_hierarchy_commit`, mais seul `mt_cni.c` lisait le flag
  `inf->resetting` → les bursts RX **et** TX pollaient un port not-ready → SIGSEGV. La garde couvre les
  DEUX sens (flag port-wide). Réserve : le commit fige le port ~100 ms→s (dégradé toléré, 0 perte en
  régime établi) → créer les TX **avant** d'abonner la RX / de façon échelonnée.
- **RX borné CPU/mémoire**, pas par les files RL (le shaper est un mécanisme d'égress TX ; RX jamais
  concernée). Le cap `RL_TX_QUEUES_CAP` doit être re-gaté sur `pacing==RL` seul (tsc/tsc_narrow ne
  construisent aucun arbre TM → jamais bornés).
- **Bande passante = le vrai mur vidéo** : E810-C `[8086:1592]`, lien PCIe **Gen3 x16** (downgradé de
  Gen4 sur ce serveur, choix assumé) ≈ 126 Gb/s/sens → ~100G pratique/sens. ~22-45 flux 1080p50/carte
  selon 2022-7. 2022-7 (`num_port=2`) = **2× PCIe mesuré** (2 DMA) → mono-carte ~50-60G utile ;
  2 cartes = ~100G utile + vraie redondance.

---

## 5. Auto-provisionnement (1 moteur/nœud)

Décision produit 2026-07-09 : **un seul moteur `2110_io` (bi-rôle RX+TX, multi-ports) par nœud**,
provisionné automatiquement à la configuration d'un port média 2110. Plus de création manuelle.

- `docker_driver.ensure_node_engine(node_id)` (idempotent, best-effort, thread) :
  - ≥1 `media2110` **avec IP** + aucun moteur → `creer_container_docker` + `deploy_docker`.
  - Plus aucun port média + moteur → **arrête le script** (agent `:8081/stop`), pas de destruction ;
    repart à la reconfig d'un port.
  - Moteur présent + port média : **ne coupe PAS à chaud**. Vérifie que le moteur EN MARCHE couvre
    toutes les NIC média (env baked `IFACES`/`IFACE` via `docker inspect`) ; si une NIC ajoutée n'est
    pas couverte → **alerte « redéploiement requis »** (jamais de redeploy silencieux), anti-spam sur
    changement de l'ensemble manquant (durcissement B).
  - Ne touche **jamais** une sonde `probe_2110` (coexiste légitimement, garde de `creer_container_docker`).
- **Backfill au boot** (`backfill_node_engines`, thread différé 45 s dans `main.py` sous `is_active`) :
  provisionne les moteurs manquants sur les nœuds ayant déjà un port média (garde `verify_image`
  silencieuse → un nœud down ne génère qu'une alerte `warning` différée, pas le spam « image absente »).
- **Anti-TOCTOU** : le type du moteur est persisté DANS `_create_mtl_lock` — sinon deux ports média
  configurés en rafale franchiraient tous deux la garde « 1 moteur/nœud » (le 2ᵉ lit un type encore
  vide) → 2 moteurs. Les hooks ne se déclenchent que sur un changement touchant un port média.
- Hooks : `node_network.api_node_interface_set` (upsert + delete) → thread `ensure_node_engine`
  (l'IP média est posée juste avant ; gate sur rôle média nouveau/ancien).
- Commits : `e965a12` (feature) · `665ea06` (fix TOCTOU M1 + gates m2/m3) · `7fef679` (backfill A +
  alerte attache-NIC B) · sous-module `b6b814e` (flag `auto_provision`).
- `routes.creer` rejette la création manuelle de `2110_io` (probe épargnée).
- `plugin.json: auto_provision: true` (flag générique) → `forms.html` + `deploy_palette.html`
  masquent la puce « créer », gardent la section Sources et l'édition d'un moteur existant.
- Câblage PTP auto (`_build_run_cmd`, `_has_dpdk_pf`) : sur toute PF `pmd=dpdk`, pose automatiquement
  `ENGINE_PTP=libmtl` + `ENGINE_PHC2SYS=1` + `NIC_PROMISCUOUS=1`. **AF_XDP et SR-IOV inchangés
  (docker run octet-identique).**

---

## 6. Exploitation

- **Image `bobi-mtl`** (par-nœud, build E810-only). Patchs libmtl **vendorés**, appliqués au build
  **avant** `./build.sh`, fail-fast idempotents contre le SHA épinglé. `routes/images.py` ajoute les
  2 nouveaux patchs (Router Alert, rte_flow PTP) au stage de build.
  - **MTL_REF épinglé : `32b1b4e937c834353536a9e84e04f2988754951a`** (= `origin/main` au 2026-07-06,
    base des builds verts ; tag amont le plus proche v26.01 = 58eaa08). Toute montée = décision
    explicite + re-run des patchs.
  - Patchs vendorés : `patch_st40_afxdp_port`, `patch_afxdp_tx_link_drop`, `patch_rx_resetting_guard`
    (0.39.5), `patch_tm_hierarchy` (0.39.6), `patch_igmp_router_alert` (0.39.12),
    `patch_ptp_mcast_flow` (0.39.13).
- **Reboot d'un nœud E810** : passer par **iLO Redfish `ForceRestart`**. `sysrq` et `systemctl reboot`
  ne suffisent pas de façon fiable dans ce contexte (état carte/vfio).
- **Bind PF → vfio = SÛR** (méthode soft : `driver_override` + `unbind` + `vfio-pci/bind`, **sans**
  PCI remove/rescan) — validé cette session sur f0 (propriétaire du PHC, seul dans son groupe IOMMU) ET
  f1, **zéro wedge**. **Le TEARDOWN vfio→ice est RISQUÉ** : un `echo 1 > .../remove` + `rescan` PCI a
  **WEDGÉ le nœud** (deadlock rtnl). ⇒ **NE PAS** utiliser remove/rescan pour rendre la carte au kernel.
  Récup propre = **reboot iLO Redfish `ForceRestart`** (restaure f0/f1→ice + PHC en ~1-2 min ; faire un
  `sync` avant, ForceRestart = hard). (`modprobe -r ice` impossible de toute façon : tenu par `irdma`.)
- **⚠ Caveat PHC E810** : rebinder un port E810 (ice↔vfio) **pendant que ptp tourne sur le port frère**
  réinitialise le PHC partagé → ptp4l décroche. Ne jamais rebinder avec ptp actif sur un port de la
  même carte.
- **⚠ Piège orchestrateur (banc sur nœud enrôlé)** : `rename`+`stop` du conteneur moteur déclenche la
  surveillance prod qui le **détruit** (~5 s) sans le recréer (`--rm`, ports vfio). La surveillance
  n'auto-recrée **pas** un moteur MTL disparu. Parade : maintenance/désenrôlement, ou recréer
  manuellement depuis l'`inspect`. Relance côté contrôleur via `redemarrer_container(<vmid>)`, jamais
  par SSH.
- **Perte d'`ethtool -S`/`tcpdump`** sur le port vfio → diag via le contrat stats MTL :
  `mtl_rx` écrit `/tmp/mtl_ports.json` (relayé sur `:8080` `nic.ports[i].mtl_stats`,
  `rx_gbps`/`tx_gbps` basculés dessus quand `pmd=dpdk`).

---

## 7. Reste à faire

- **Bascule prod** : écrire la colonne `nodes.image` (image 0.39.13) + `node_interfaces.pmd=dpdk`
  sur le(s) nœud(s) cible(s) ; garantir le **repli AF-XDP** (chemin par défaut) tant que la batterie
  de conformité complète (entrelacé, 2022-7 hitless, restart/recovery, redeploy à chaud, formats par
  flux) n'a pas repassé sur le socle dpdk.
- **Banc capacité multi-TX à l'échelle** : caractériser N senders narrow RL en régime réel, coût
  CPU/mémoire, contention. **CHIFFRE FIABLE = 16 TX narrow stable (VRAI moteur)** ; plafond supérieur
  NON mesuré proprement. ⚠ **`mtl_rx --config` brut N'EST PAS l'outil** (banc 2026-07-10, rafale +
  étalé + sources dédiées, tous échouent avant un régime stable) : la création crée bien les N sessions
  (reconcile correct — `st20p_tx_create` RL lent ~qq s, sérialisé, fige le port à chaque commit), mais
  (a) les `st20_tx_queue_fatal_error` s'accumulent sous la rafale de commits RL → backstop `TX FIGÉ`
  → exit (q33 à tx_q=34, q42 à tx_q=44 ; ce n'est PAS le mur leaves — l'arbre RL a la capacité), et
  (b) la livraison de frames se dégrade (2 sessions/sources dédiées → 0,3 Gb/s au lieu de ~4,3).
  → **le banc à l'échelle doit passer par le VRAI moteur (`controller.py`)** qui ÉTALE les créations via
  l'API de câblage `io2110_flows`/:8082 (d'où son « 16 TX stable »). À reprendre comme chantier dédié.
  Reste à mesurer : plafond réel (BP 100G ≈ 38 vidéo 1080p50, ou les leaves RL) + coût CPU/mémoire.
  - **Banc `tools/tx_scale_bench.py` (2026-07-10)** — empile les senders via `:8081/tx` (chemin du vrai
    moteur, pas de rafale `--config`), source = mire txgen auto-générée. **N'A PAS mesuré le plafond RL**,
    pour DEUX raisons dures identifiées au banc (dl360-1, image 0.39.13, mono-port dpdk, `SIPS` lu dans
    `node_interfaces`=198.51.100.229, `PORT_RESERVE tx=42` pré-réservé) :
    1. **La mire txgen est un thread PYTHON/slot (GIL, qq lcores) → elle n'alimente pas 50 fps au-delà de
       ~2 senders.** À 16 : `TX_st20p frame get try 10 succ 0-1` = SOURCE affamée (pas le TX), fps→~1-2,
       `tx~0`. Le « 16 TX stable » du vrai moteur vient de **flux MXL CÂBLÉS produits en C**, jamais de
       mires Python. ⇒ un banc capacité DOIT nourrir les senders par de vrais producteurs MXL C.
    2. **Ajouter un sender à un moteur VIF déclenche `mt_dev_tx_queue_fatal_error` + relance `mtl_init`**
       (observé dès le 3ᵉ, même avec la réserve de files OK — c'est le shaper RL ice qui masque une queue
       au commit d'un leaf dans l'arbre live, pas le budget). **Chaque relance re-discipline le PHC → re-
       lock PTP ~1,5-2 min** (le TX ne produit qu'une fois locké). Le moteur RÉCUPÈRE (1 et 2 senders
       PROUVÉS propres : 50 fps, 2,23 Gb/s/sender, 4,46 Gb/s à deux), mais tous les senders blippent.
    ⇒ **Acquis** : TX narrow création+émission FONCTIONNE (démenti « TX cassé depuis le moteur ») ; le
    plafond N-large et le coût du hot-add restent à mesurer via un banc à **sources MXL câblées C** (+ GM
    pour un re-lock quasi instantané). Le hot-add disruptif (blip PTP flotte entière) est en soi à traiter.
  - **CORRECTION source (essai avsync, 2026-07-10)** : un conteneur avsync (image compute, flux MXL
    unique, câblé sur les 16 TX via `:8082/input`) élimine la famine Python multi-thread — MAIS le
    **cold-batch de 16 TX s'écroule pareil** (6/16 à ~2 fps, `tx_st20p_create_transport fail`, moteur 1
    cœur). Donc le mur du BURST est la **création RL en rafale**, PAS la source (reproduit avec source
    propre). ⇒ le seul chemin qui scale est **incrémental UN-PAR-UN avec settle long (≥150 s** couvrant
    relance+relock ; 1-2 prouvés, 3 échouait sur timeout pas sur mur). Chaque ajout = 1 relance + re-lock
    PTP ~2 min ⇒ atteindre N coûte N blips flotte. NB : avsync plafonne ~24 fps en 1080p 10-bit (rendu
    Python lourd) — pour 50 fps soutenu, source C (ou mire txgen légère à 1 flux).
  - **★ CAUSE RACINE du fatal_error/hot-add + FIX écrit (2026-07-10)** — la chaîne exacte :
    créer une session RL TX → `dev_tx_queue_set_rl_rate` → `rte_tm_hierarchy_commit`, que le PMD ice
    implémente en **STOPPANT tout le port** (`dev_started=0`) ~100 ms→1 s. `patch_rx_resetting_guard`
    rend ce stop survivable (`mt_txq_burst`→0 au lieu de segfault) MAIS ce 0 fait tomber les AUTRES
    sessions TX vives dans **`video_trs_burst_fail`** (`st_video_transmitter.c`) : si le stop dépasse
    `tx_hang_detect_time_thresh` (défaut **1000 ms**) → `st20_tx_queue_fatal_error` → masque la queue →
    backstop « TX FIGÉ » de `mtl_rx.c` (`_exit`) → relance `mtl_init` → **re-lock PTP, flotte blippe**.
    **FIX** : `docker/patch_tx_hang_resetting_guard.py` (vidéo + audio) rend le détecteur de hang TX
    conscient de `inf->resetting` — pendant un reset transitoire (commit d'une AUTRE session), on
    repousse `last_burst_succ_time_tsc` et on skippe SANS fatal ; l'émission reprend seule à la fin du
    commit. Le vrai détecteur de wedge (lien mort) reste intact. Complète `patch_tm_hierarchy` (mur des
    8) + `patch_rx_resetting_guard` (segfault).
  - **★ FIX BUILDÉ + VALIDÉ (image `bobi-mtl:0.39.14-txhang2`, banc dl360-1 2026-07-10)** — DEUX couches :
    1. **libmtl** `docker/patch_tx_hang_resetting_guard.py` (vidéo+audio) : rend `video_trs_burst_fail` /
       `st_audio_trs_burst_fail` conscients de `inf->resetting` → **`st20_tx_queue_fatal_error` ÉLIMINÉ**
       (`fatal Δ0` sur tous les ajouts, tous les tests). Câblé au Dockerfile après `patch_rx_resetting_guard`.
    2. **mtl_rx.c** (garde de grâce du backstop) : après un create TX, `g_tx_add_grace_ns = mono_ns()+20 s`
       suspend le backstop « TX FIGÉ » → le port-off du commit RL ne redémarre plus le daemon. VALIDÉ :
       la 1ʳᵉ relance passe du slot 1 (avant) à > slot 4 (après) ; les relances résiduelles (slot 5+) ne
       viennent PAS du commit mais du point 3 ci-dessous.
    ⇒ **le hot-add ne fait plus fatal_error ni relance/re-lock pour le port-off du commit** (objectif atteint).
  - **★ 3ᵉ problème DISTINCT découvert (pas les fixes) : un flux MXL ne sert qu'UN lecteur TX.** Câbler N
    sorties TX sur UNE source (avsync, txgen, OU conteneur prod `stream-in` 50 fps genlock indépendant)
    → **un seul sender émet** ; ajouter un slot VOLE le lecteur au précédent (`fps=[0,50]` puis `[0,0,50]`),
    l'ancien gèle → backstop (légitime, après grâce). D'où le « un seul à 50 fps » de TOUS les bancs
    mono-source. ⇒ un banc capacité N-TX exige **N sources distinctes** (chaque TX son flux) — c'est le
    banc à sources câblées C que §7 nommait.
    - **Diag CONFIRMÉ (source libmxl v1.1.0-beta-1)** : `Instance::getFlowReader(flowId)` renvoie un reader
      PARTAGÉ refcompté par (instance, flowId) (map `_readers` + `addReference`). Donc **une source → N
      sorties TX DANS LE MÊME MOTEUR** partagent un curseur → seule la dernière est servie (vérifié : couper
      la sortie 0 en ajoutant la 1, SANS relance). ≠ le cas normal 1 flux → N **conteneurs** (instances
      distinctes) qui marche (bus MXL). Fix éventuel = lire le grain 1× par flux + fan-out (mtl_rx.c).
  - **★ CAPACITÉ RL post-fix (cold-batch, image `bobi-mtl:0.39.14-txhang2`, 2026-07-10)** : monter N sessions
    d'un COUP (1 seule init, 1 seul lock) contourne le PTP re-lock de l'ajout incrémental. **24/24 sessions
    narrow RL montent simultanément** (640×360, sources distinctes), `fatal_error=0` — vs ~6-7 AVANT le fix.
    ⇒ **plafond RL ≥ 24 sessions** (vraisemblablement jusqu'aux 128 feuilles `MT_MAX_RL_ITEMS` ou la réserve
    de files) ; la BW borne à ~44 @ 1080p50 (arithmétique). Instabilité résiduelle = **source Python txgen
    (GIL) affame par intermittence → backstop**, PAS le chemin RL. Un N-stable propre + le vrai plafond BW
    exigent des **sources C** à l'échelle. ⚠ L'ajout INCRÉMENTAL reste disruptif : chaque commit RL DÉ-LOCKE
    le PTP ~20-30 s (PHC perturbé par le port-off) → la flotte gèle le temps du re-lock (grâce backstop 20 s
    insuffisante pour ça) → préférer le **cold-boot** pour empiler.
  - **★★ DÉCOUPLAGE source↔session TX — IMPLÉMENTÉ + VALIDÉ (image `bobi-mtl:0.39.14-decouple`, 2026-07-10)** :
    la SOURCE (`tg[].shm_path`) sort de `compute_sig` pour un TX → **changer la source ne re-crée plus la
    session libmtl** (pas de `st20p_tx_create` → pas de commit RL → **pas de dé-lock PTP**). reconcile pose la
    nouvelle source (`tx_set_source`, seqlock) ; le thread TX (vidéo/audio/ANC — 3 essences) la prend
    (`tx_take_source`) et rouvre son reader. Source vidée `""` → thread muet (slot silencieux, 0 Gb/s, occupe
    une feuille RL). **Banc** : 4 permutations de source d'un sender vivant (`stream-in`↔`avsync`) → **`mtl_init
    Δ0`, PTP `locked` en continu, fps 50 ininterrompu**, aucune re-création. ⇒ **modèle broadcast réalisé** :
    pré-provisionner les sorties (typées V/A/ANC) au boot, **router le contenu à chaud sans disruption**. Seul
    le boot (cold-create) + l'ajout d'une NOUVELLE destination paient encore le dé-lock PTP. **RESTE** : banc
    N-sources C (plafond BW + N-stable) ; fix reader MXL partagé (1 source → N TX d'un moteur) ; éventuel
    PTP-hold pendant le commit (ajout non-disruptif).
  - **★★ SLOTS SILENCIEUX + PLAFOND DE CRÉATION MESURÉ (image `bobi-mtl:0.39.14-silent`, 2026-07-10)** :
    `controller.py` émet une session TX PROVISIONNÉE sans source (`_tx[i]["provisioned"]`, settable via
    `:8081/tx`) et `mtl_rx.c` (parse) accepte une cible TX sans `shm` → **slot créé + feuille RL allouée,
    thread muet (0 Gb/s) en attente de câblage**. Le contenu se route ensuite par swap (découplage). **Banc
    ceiling** (slots silencieux en cold-batch) : **PLAFOND = 64 files TX E810 DPDK** (`dev_if_init_tx_queues
    tx_queues 64 malloc succ` — le PMD ice clampe à 64, PAS aux 128 `MT_MAX_RL_ITEMS`) → **~63 sessions TX
    concurrentes** (1 file = contrôle) ; **60 CONFIRMÉES stables** (1 seule init, `fatal=0`). Au-delà de 64 :
    le contrôleur boucle en relance (demande > réserve) → **à plafonner proprement** (clamp ACTIVE_TX ≤ files).
    ⇒ **capacité nœud** : ~63 sorties TX au total ; dont ≤ ~44 vidéo actives @ 1080p50 (BW 100G), le reste en
    audio/ANC (BW négligeable). RX et TX ont des files SÉPARÉES (banc : `tx_queues 64` + `rx_queues 10` alloués
    ensemble) → les 63 TX n'entament PAS le budget RX.
  - **★★ CLAMP unifié `RL_TX_QUEUES_CAP` piloté par une BIBLIOTHÈQUE DE CARTES (implémenté)** : `controller.py`
    borne les sessions TX émises (vidéo+audio+ANC) à `RL_TX_QUEUES_CAP` (env) — la MÊME manette qui capait déjà
    la réserve (le trou était : réserve capée mais PAS la demande → demande > réserve → **boucle de relance**).
    `docker_driver` l'**injecte en env** depuis `mtl.nic_rl_tx_cap(model)` (biblio sur `node_interfaces.model`).
    ⚠ **La valeur est PROPRE À LA CARTE et NON auto-découvrable** : `rte_eth_dev_info.max_tx_queues` rapporte le
    mur ice natif **8** (que `patch_tm_hierarchy` transcende jusqu'à 64) → la capacité EFFECTIVE est une propriété
    **mtl+patch**, invisible au PMD (tentative de découverte runtime revertée). ⇒ elle doit être **CONNUE**.
    **Biblio** (`app/mtl.py:NIC_RL_TX_CAP`) : `e810-c`→63 **MESURÉ** (dl360-1) ; toute carte non profilée (dont
    E810-XXVDA4 4-port, files partagées → probablement <63) → **plancher sûr 7** (ne re-boucle JAMAIS) jusqu'à
    qualification. Keyer à terme (modèle **+ firmware**). Banc : `RL_TX_QUEUES_CAP=20` → 80 slots → config STABLE
    20, `mtl_init=1` (le clamp suit la carte).
  - **★★ TABLE `nic_profiles` + SUITE DE QUALIFICATION (implémentées)** : `database.py` table `nic_profiles`
    keyée (device_id + firmware) — un profil MESURÉ prime sur la biblio statique au déploiement
    (`docker_driver._node_rl_tx_cap` : profil > biblio > plancher). `app/nic_qualify.py` : parseurs (device_id
    via `lspci -nn`, firmware NVM via `ethtool -i` sur un port ice sœur — le port média est en vfio, pas de
    netdev, du log libmtl « tx_queues N malloc succ » = files ALLOUÉES) → écrit `rl_tx_cap = N−1` (measured=1)
    **UNIQUEMENT SUR PREUVE DE CLAMP** (cf. ci-dessous).
    Route `POST /api/nodes/<id>/qualify-nic` (trouve le moteur 2110_io du nœud, qualifie). Testé sur les vraies
    sorties dl360-1 : 0x1592 / fw 4.80 / 64 files → cap 63. Le profil capture aussi **ptp_ok** (lock PHC,
    du log), **ddp_ok** + version (`devlink`), et **narrow_ok** (`parse_conformity` : cinst_max≤1 ET
    vrx_span∈[1,5] des receivers :8080 TIMING_PARSER — on NE lit PAS `compliant`, le `failed` sans GM est
    structurel). narrow_ok n'est renseigné QUE si un verdict est présent (sonde). **Run live narrow_ok** :
    le loopback mono-port ne capte PAS (le switch ne réfléchit pas le mcast au port source) → exige le
    **banc bi-port §8** (bind f0→vfio) — NON fait sur nœud partagé (cleanup vfio→ice = reboot iLO, §6). À
    faire sur nœud dédié / avec analyseur externe. **RESTE** : auto-qualif à l'enrôlement ; UI page Recette.
  - **★★ « tx_queues N malloc succ » N'EST PAS UN PLAFOND — garde de CLAMP (corrigé 2026-07-27)** : cette
    ligne rapporte ce que libmtl a **demandé ET obtenu**, pas la capacité de la carte. La demande suit le
    nombre de sessions du moteur (`mtl_rx.c` : « daemon up (… tx_q[0]=N) » = `p.tx_queues_cnt[0]`, l'ARGUMENT
    de `mtl_init`). La mesure d'origine n'était valide que parce que le banc cold-batch **sur-demandait**
    (80 slots → clamp PMD ice à 64 → cap 63). L'auto-qualif (`_auto_qualify_nic`, 90 s après déploiement) a
    repris le parseur SANS la sur-demande : sur un moteur peu chargé elle relisait sa propre demande et
    **rabaissait le cap autoritaire d'un cran à chaque passage, en silence** — dl360-1 : 63 → 41 → 21 → **14**
    (profil measured=1 primant sur la biblio), ce qui bridait `RL_TX_QUEUES_CAP` sur le moteur et mettait la
    page « Modèles de carte 2110 » entièrement en rouge (14 files pour un layout qui en demande plus).
    ⇒ `nic_qualify.measured_tx_cap()` est désormais le **point d'entrée unique** : il exige `alloué < demandé`
    (témoin de clamp, `parse_requested_tx_queues`) et **refuse d'écrire un cap** sinon (`(None, raison)`), le
    reste du profil (PTP/DDP/narrow/stack) étant enregistré normalement → `rl_tx_cap` reste NULL et
    `_node_rl_tx_cap` retombe sur la biblio. Refus REMONTÉ (alerte `warning` sur la route, log sur
    l'auto-qualif) — jamais muet. Le profil dl360-1 a été corrigé en base (`rl_tx_cap=NULL`, note d'invalidation).
    ⚠ **Conséquence pratique** : requalifier une carte pour de bon exige toujours le banc cold-batch qui
    sur-demande ; l'auto-qualif ne peut, par construction, que confirmer PTP/DDP.
  - **★ SUPERVISION UI DPDK — barre « Queues XDP » remplacée (implémenté, à valider au banc)** : sous PF
    vfio `xdp.hw_max_combined`=null (pas de netdev → `ethtool -l` sans objet) — la barre « Queues XDP »
    des pages Sources/Destinations était vide/mensongère et le bouton « + Ajouter un TX » se plafonnait
    sur le repli réglage 48 files AF-XDP. Nouveau : le contrôleur (**0.39.16**, REBUILD requis) expose
    sur `:8080` un bloc `rl` {active, pacing, tx_cap_per_port, tx_sessions, rx_sessions, **tx_dropped**
    (sessions TX > cap IGNORÉES — avant : seulement loggé), rx/tx_queues_alloc} + par port dpdk
    `rl_tx_cap`/`tx_sessions_active`/`rx_sessions_active` (legs 2022-7 comptés sur leur NIC). Côté
    orchestrateur (`nmos_detail.py`) : passthrough `rl_*` (repli biblio/profil via `_node_rl_tx_cap`
    quand le moteur est muet) ; `tx_count` (= plafond du bouton « + Ajouter un TX ») se borne désormais
    sur le **budget RL** (`_mtl_rl_tx_budget` = cap/port × ports média utiles, pair-aware 2022-7) via
    `_mtl_active_caps(tx_budget=…)` — budgets RX (RSS) et TX (feuilles RL) DÉCOUPLÉS, conformes au banc
    (« les 63 TX n'entament PAS le budget RX »). UI (`io2110.js`/`control.js`, i18n `js.io2110.rl_*`) :
    barres « Sessions TX (RL) » (utilisées/cap + badge SUR-CAPACITÉ sur `tx_dropped`) et « Files RX
    (RSS) » ; un nœud af_xdp garde la barre « Queues XDP » historique (`rl.active`=false). **À VALIDER
    au banc narrow** : valeurs live du bloc `rl`, badge sur-capacité en dépassement réel, plafonnage du
    bouton « + » à 63/port, comptage 2022-7.
- **fpt unifié** : durcir le verdict conformité absolu (tolérance FPT `≥`/marge de propagation) et
  vérifier la stabilité du lock PTP interne en régime prolongé (dérive ~200 ns/s observée quand le
  PHC free-run ; le lock GM la corrige).
- **Mode tranche (Phase 3, sous-trame)** : GO mesuré (libmxl commit progressif `mxlFlowReaderGetGrainSlice`,
  gain structurel ~17,5 ms/étage) mais NON implémenté — gros chantier séparé touchant tous les
  consommateurs ; utiliser la sémantique slice **standard MXL** (1 slice = 1 ligne).

---

## 8. Bancs & commandes (recettes reproductibles)

**Nœud de banc** : dl360-1 (node 30, 192.0.2.251). E810 bi-port `ens1f0np0` (`0000:11:00.0`) /
`ens1f1np1` (`0000:11:00.1`), PHC partagé (clock 4). Le port DPDK a été `ens1f1np1` (seul câblé sur
le switch média). Sonde possible sur `ens1f0np0` (PF vfio) joignant le mcast de `ens1f1np1`.

### Bind d'une PF en vfio-pci
```
echo vfio-pci > /sys/bus/pci/devices/0000:11:00.1/driver_override
echo 0000:11:00.1 > /sys/bus/pci/devices/0000:11:00.1/driver/unbind
echo 0000:11:00.1 > /sys/bus/pci/drivers/vfio-pci/bind
# ⚠ rollback SÛR = reboot iLO ForceRestart. NE PAS tenter le teardown vfio→ice manuel
#   (remove/rescan) : il a WEDGÉ le nœud (deadlock rtnl, cf. §6). Le reboot restaure f0/f1→ice.
```
Prérequis déjà en cmdline sur le banc : IOMMU + hugepages 1G, `vfio-pci` chargé (pas de reboot).

### docker run du moteur full-PF DPDK (mono-port)
```
docker run -d --name bobi-mtl-<vmid> --network host --privileged \
  --log-opt max-file=5 --log-opt max-size=50m \
  -v /dev/shm:/dev/shm -v /dev/hugepages:/dev/hugepages \
  -v /run/bobi-tls/bobi-mtl-<vmid>:/etc/bobi-tls:ro -v /dev/vfio:/dev/vfio \
  -v /lib/firmware/intel/ice/ddp/ice.pkg:/lib/firmware/intel/ice/ddp/ice.pkg:ro \
  -e IFACES=ens1f1np1 -e SIPS=<IP réelle du port> -e PORT_PMDS=dpdk \
  -e PORT_BDFS=0000:11:00.1 -e PORT_NETS=1 \
  -e ENGINE_PTP=libmtl -e ENGINE_PHC2SYS=1 -e NIC_PROMISCUOUS=1 \
  -e MTL_PACING=rl \
  bobi-mtl:<tag>
```
⚠ Le `sip` d'un port dpdk **DOIT** être l'IP réelle de segment du port (en DPDK, libmtl forge lui-même
l'IGMPv3 depuis le sip → le switch ne forwarde que si le report vient d'une IP réelle du port). Un
mismap SIPS↔IFACES donne rx=0 (masqué en AF-XDP par le netdev kernel). En prod ces envs sont posés
automatiquement (§5) — ne les mettre à la main que pour un banc.

### Config JSON bi-port (2022-7 / sonde+générateur)
- 2022-7 : `PORT_PAIRS` dérivé de `node_interfaces.pair_group`/`pair_role` (`num_leg=2`, red/blue).
- Sonde + générateur coexistant sur un même nœud : `CONTROLLER_PORT_BASE` (0.39.7) décale les 3 serveurs
  HTTP du contrôleur (BASE métriques / BASE+1 agent / BASE+2 contrôle). Émis seulement si
  `params.controller_port_base` posé → un nœud mono-moteur reste octet-identique.

### Sonde de conformité (verdict narrow/wide)
```
# côté conteneur sonde : -e TIMING_PARSER=1  (+ PF en vfio pour le HW timestamp)
# lecture verdict sur :8080 (ou BASE si offset), par receiver vidéo :
#   compliant (narrow|wide|failed), failed_cause, cinst_max/avg, vrx_max/min/avg/span, fpt, latency
# interpréter : cinst_max=1 + vrx_span 1-5 = narrow franc ; le failed (fpt exceed tr_offset)
# sans grandmaster est STRUCTUREL — lire cinst/vrx_span (invariants à la dérive), pas `compliant`.
```
**Banc GM bi-port validé (socle, 2026-07-10)** : bind f0 ET f1 → vfio, puis UN SEUL `mtl_rx --config`
bi-port (TX narrow RL sur f1 + sonde `TIMING_PARSER` sur f0 joignant le mcast) avec `ENGINE_PTP=libmtl`
+ `NIC_PROMISCUOUS=1` → le **PHC partagé** de l'E810 se discipline au GM (les 2 ports lockent ~30 ns) →
le TX pace sur l'epoch GM et la sonde horodate sur la même horloge. **Pas de ptp4l** (obsolète en socle
DPDK). Verdict lu dans le fichier `stats` de la cible sonde. ⚠ Le TX du banc lit un flow `mxl_bench`
**free-run** → le `fpt` absolu n'est pas mesurable ici (cf. §3) ; le gapping cinst/vrx_span l'est.
(Réserve loopback câble-direct historique : le lien n'est UP que si les 2 ports sont `mtl_init`'és →
abonner la sonde avant que le générateur linke, sinon `dev_detect_link fail -5`.)

### Générateur de flux narrow (banc)
Moteur `2110_io` avec un slot TX câblé + `MTL_PACING=rl` + `PORT_PROFILES=narrow` ; ou endpoints de
contrôle `/tone_tx` (attend `idx`/`ai`). Démo wide franche pour A/B : `MTL_PACING=tsc` plain +
`PORT_PROFILES=wide` → cinst 5 / vrx_span ~920k (5 ordres de grandeur vs narrow).

### mxl_bench writer (mode tranche)
`script_templates/mxl_bench.py` + patch `mxl-planar-slices.patch` (N tranches via `slice_height`).
Repère mesuré 1080p50 N=8 : commit→observe p50 0,14 ms / p99 0,27 ms / 0 stall ;
1ʳᵉ→dernière bande p50 17,5 ms (= gain structurel/étage) ; dé-packing BE par bande 449 Mo/s.

---

## 9. Recette dpdk — plan de bascule prod (par nœud)

**Objet** : la checklist ORDONNÉE qui transforme « le narrow marche au banc » en « on peut déployer ».
Tant qu'elle n'est pas verte sur ≥1 nœud, le **repli AF-XDP reste** (mode prod actuel, intouché).

### Principe
- **Bascule PAR NŒUD**, jamais la flotte d'un coup. Un nœud en dpdk, le reste en AF-XDP.
- **Rollback** = repli soft : `nodes.image` ← ancienne + `node_interfaces.pmd=af_xdp` → redeploy (le port
  revient au kernel `ice`). Si le port vfio est wedgé (teardown vfio→ice, cf. §6) : **reboot iLO ForceRestart**.
- **Ne PAS retirer le repli AF-XDP** (généraliser) avant recette G complète + soak 24-48 h.

### Prérequis nœud cible
- Carte **qualifiée** (`nic_profiles` : `ddp_ok`, `ptp_ok`, `rl_tx_cap` connu — sinon `POST /api/nodes/<id>/qualify-nic`).
- **GM PTP** joignable sur le réseau média (le socle dpdk n'a plus de ptp4l kernel → lock via libmtl, §2).
- Hugepages 1G + IOMMU en cmdline, `vfio-pci` chargé ; port média bindable vfio (§8) ; `sip` = IP réelle du segment.

### Étape 0 — Bascule
| # | Action | Critère |
|---|---|---|
| B0 | `nodes.image`=dpdk (0.39.13+) + `node_interfaces.pmd=dpdk` sur le port média, redéployer le moteur | Conteneur up, `:8081/status` OK, port `pmd=dpdk` dans `mtl_ports.json` |

### Étape R — Régressions (DÉJÀ prouvées ailleurs — re-vérifier après bascule)
| # | Item | Procédure | Critère | Statut socle |
|---|---|---|---|---|
| R1 | Lock PTP carte-directe | GM présent, `ENGINE_PTP=libmtl` | `system clock offset … locked`, offset stable < qq 100 ns | ✅ prouvé |
| R2 | RX complète | abonner un flux 2110 réel | fps stable, pas de gel, `signal` frais | ✅ prouvé |
| R3 | TX narrow | 1 sender RL + sonde/log | `cinst_max=1`, `vrx_span 4-5` (sonde) ; pas de `fatal_error` | ✅ prouvé (banc GM) |
| R4 | Capacité | empiler jusqu'au `rl_tx_cap` de la carte | pas de boucle de relance ; N sessions stables | ✅ ~63 mesuré |

### Étape G — Batterie GATE (à RE-VALIDER sur le socle dpdk — le vrai bloquant)
| # | Item | Procédure | Critère | Statut socle |
|---|---|---|---|---|
| G1 | **Entrelacé 1080i50** | RX 1080i50 → TX passthrough → moniteur/analyseur | pas de peigne, parité champ conservée, `exactframerate=25;interlace` dans le SDP | ✅ data 2026-07-11 (SDP TX0 = 1920x1080 interlace, parité 250/250, 0 drop) ; peigne visuel = user |
| G2 | **2022-7 hitless** | 2 legs red/blue, débrancher un câble en direct | AUCUNE coupure visible ; reprise silencieuse au retour lien | ✅✅ 2026-07-11 HITLESS FRANC (coupure physique : 0 unrecovered, fps stable ; retour lien : re-join auto ~30s) sur bi-port même carte E810 (ring 4096 + RX sched dédié, 0.39.18) |

**G2 run 2026-07-11 (bi-port DPDK f0 blue + f1 red, même carte E810, RX 1080i50 DUP) :**
- 2022-7 **fonctionne** : les deux legs délivrent, reconstruction OK ; en TX léger `rx_hw_dropped=0`, coupure quasi-hitless (~500 unrecovered transitoire).
- **Limite trouvée** : sous TX lourd co-localisé (6 mires ~13 Gbps), ~0,18 % `rx_hw_dropped` sur les DEUX ports, **corrélé** (mêmes paquets perdus sur les 2 legs au même instant → non récupérable). Cause = contention PCIe/DMA/cœurs **intra-carte** (les 2 PF partagent la carte), PAS la diversité de chemin. Coupure sous TX lourd = ~3292 unrecovered transitoire.
- **Findings** : (a) au retour du lien, le port DPDK ne re-joint PAS le mcast tout seul (reprise silencieuse KO — à corriger) ; (b) le normaliseur deploy recalcule active_tx_count depuis tx_slots (empêche un vrai RX-only via config).
- **Leviers d'amélioration** (chantiers, pas one-liners) : 1) isoler files/cœurs RX du TX (scheduler MTL) ; 2) augmenter profondeur ring RX (patch libmtl) ; 3) exploitation : nœud récepteur = pas de TX lourd, ou 2 cartes pour indépendance ressources.
- Verdict : 2022-7 mono-carte VALIDÉ pour un usage récepteur ; hitless-sous-charge-TX nécessite l'isolation RX/TX.
- ★★ **HITLESS FRANC prouvé (coupure physique, 0.39.18 ring4096+RX-sched-dédié)** : 0 `unrecovered`, fps rock-stable 50, save_rate 100% pendant toute la transition ; retour du lien = re-join auto ~30s, redondance restaurée sans glitch. Progression coupures : ring2048/6mires=3292 unrec (fps→33) ; ring2048/TX-léger=500 (fps→42) ; ring4096+RX-dédié=**0** (fps stable).
- ★ **RÉSOLU 2026-07-11 (0.39.17)** : la perte corrélée venait du **ring de descripteurs RX trop court** (2048), PAS de la topologie. `nb_rx_desc` porté à **4096** (env RX_NB_DESC, défaut DPDK) → à charge identique (6 mires ~13 Gbps), `rx_hw_dropped` **0,18 % → 0,0000 %** sur les 2 ports, `unrecovered` **1290/fenêtre → 0**, `save_rate` **66,8 % → 100 %**. Le 2022-7 sur UNE carte E810 bi-port est propre sous TX lourd. Levier #2 (isolation scheduler) non nécessaire. Reste : finding « un leg ne re-joint pas toujours après restart » (indépendant).
| G3 | **Restart moteur** | `redemarrer` le moteur en charge | RX+TX repris, TX re-poussés, pas de gel résiduel | ✅ 2026-07-11 (12 sessions, 6 TX ré-émis, PTP re-locké 232 ns, 0 backstop) |
| G4 | **Recovery reboot nœud** | reboot du nœud | auto-recovery flotte (node_recovery), moteur+flux remontent | ✅ COMPLET 2026-07-11 (0.39.16 : f1 vfio auto + recovery + PTP locké 219 ns à froid + 0 backstop) |
| G5 | **Redeploy à chaud** | changer la config (ajout/retrait flux) | les flux NON touchés ne blippent pas ; le nouveau monte | ✅ 2026-07-11 (hot-add wire : RX 2022-7 0 perte/0 drop, PTP jamais dé-locké, nouveau flux monté ; résidu 1 mire repli décroche vs 5 avant ring4096) |

**G4 run 2026-07-10 23:14 (node 30, dl360-1) — ÉCHEC, 2 causes distinctes :**
1. **`auto_recovery_enabled` = OFF** (défaut) : le reboot est bien détecté (alerte 23:14:26,
   `node_recovery.on_health_snapshot`) mais la recovery ne se lance pas — et `recovered_boot_ts`
   est avancé, donc activer le réglage APRÈS coup ne rattrape pas le boot déjà passé (by design).
   → Pour la prod dpdk : activer le réglage (global ou override node) AVANT de compter sur G4.
2. **Bind vfio non persistant** : le bind du banc avait été fait à la main (sysfs direct) — au
   reboot f1 (0000:11:00.1) ET f0 reviennent sur `ice`. La persistance EXISTE dans le produit
   (`mtl.vfio_bind_apply` → `/etc/bobi/vfio-binds` + unit `bobi-vfio-bind.service`, route
   `POST /api/nodes/<id>/dpdk-prep {bdf}`) mais n'avait jamais été appliquée sur node 30.
   → Remédiation : passer par la route dpdk-prep (pas de bind manuel), puis re-jouer G4.
   NB : même auto_recovery ON n'aurait PAS suffi — `start_docker` re-run le moteur depuis
   deploy_config mais ne re-bind pas le vfio ; la persistance boot (unit) est le mécanisme prévu.
3. **BUG démarrage à froid (trouvé au relèvement, fixé 0.39.15)** : au boot à froid, libmtl n'émet
   AUCUNE frame TX avant PTP interne stable (`mt_ptp_wait_stable`, jusqu'à 180 s — PHC loin du GM
   après reboot) ; le backstop « TX FIGÉ » (5 s + grâce 20 s) redémarrait le daemon en boucle
   (~100 s/cycle, sync cnt 16-19 puis `_exit(3)`) → le PTP ne locke JAMAIS s'il existe ≥1 session TX
   émettrice (ici : 6 mires black du banc de capacité restées en deploy_config). Le banc passait car
   le PHC restait discipliné entre les runs. Fix 2110_io `594abab` : grâce +200 s au démarrage quand
   `ENGINE_PTP=libmtl` + max monotone au create TX (**REBUILD image requis**). Contournement config
   sans rebuild : `tx_fallback='none'` (posture prod slots silencieux) → pas d'émission → pas de
   backstop → le PTP converge.

**G4 re-run 2026-07-11 00h01 (après remédiation) — ✅ PASS INFRA :**
- bind vfio persistant : f1 revenu `vfio-pci` AU BOOT (unit `bobi-vfio-bind`), SSH ~80 s ;
- auto-recovery (réglage activé) : reboot détecté à uptime 14 s, grace 45 s, moteur redéployé et
  up à ~1 min ; les 11 compute relevés par Docker (`unless-stopped`). Alerte bilan « 1 relevé ».
- Lock PTP libmtl vérifié AVANT le reboot (offset max 128 ns, mode l4) + carte QUALIFIÉE
  (nic_profiles : 0x1592 E810-C, rl_tx_cap=50, ptp_ok=1).
- RESTE pour un G4 COMPLET : rebuild image 0.39.15 (fix cold-start) puis re-run avec TX émetteur
  au boot. Résiduels notés : (a) recovery/_check_ptp relance encore le ptp4l kernel (réglage nœud
  `ptp_enabled` à basculer, modèle = PTP libmtl) ; (b) alerte « IP média Cannot find device » sur
  port vfio (cosmétique, deploy ne doit pas poser d'IP kernel sur pmd=dpdk) ; (c) endpoint agent
  `/logs` ne renvoie QUE stdout (lignes MTL sur stderr perdues → qualify_node_via_agent aveugle) ;
  (d) vestige `bobi-sriov-vf.service` + `/etc/bobi/sriov-vfs` à purger sur le nœud (SR-IOV abandonné).
| G6 | **Formats par flux** | mix i/p et résolutions par slot | chaque session au bon format ; changement = recréation propre | ✅ partiel 2026-07-11 (SDP i50 correct, chgt p→i = recréation propre) ; FINDING hot-add écroule mires voisines (cf. G5/E2) |
| G7 | **Monitoring / preview** | multiview + monitor WebRTC lisent les flux dpdk | preview correcte, pas de « No Signal » | ✅ data 2026-07-11 (monitor WebRTC sur mtlrx95_0, entrelacé détecté, hot+publishing, 0 No Signal) ; visuel = user |

### Étape E — Exploitation (finitions, non bloquantes pour la faisabilité)
| # | Item | Critère | Statut |
|---|---|---|---|
| E1 | Routage de contenu à chaud (swap de source) | `mtl_init Δ0`, PTP jamais dé-locké, émission continue | ✅ validé (découplage) |
| E2 | Ajout d'une NOUVELLE destination | blip flotte ~20-30 s (dé-lock PTP au commit RL) — MESURER l'impact acceptable, sinon traiter (PTP-hold) | ✅ MESURÉ 2026-07-11 : flag `locked` JAMAIS tombé (découplage), dé-lock transitoire ~15 s auto-résorbé (offset pic ~1 ms → <µs en ~90 s), 0 backstop, pas de coupure flotte dure |

### Sortie de recette
Repli AF-XDP retirable sur un nœud quand **R1-R4 + G1-G7 verts + soak 24-48 h sans incident**. E2 =
décision produit (acceptable en l'état, ou on traite le PTP-hold avant généralisation). Généralisation
flotte = nœud par nœud, chacun repassant R+G.

> Les items G1/G2/G6 ont des bancs de référence côté AF-XDP (page Recette `/tests`, `app/testplan.py`)
> — la recette dpdk = **rejouer ces mêmes cas sur un nœud `pmd=dpdk`** et cocher le même critère.
