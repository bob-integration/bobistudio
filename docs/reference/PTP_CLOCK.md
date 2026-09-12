# Modèle d'horloge, PTP et synchro — Bobi.Studio

Conception validée en discussion (2026-07-08). Référence pour le chantier « horloge ». Objectif :
un système broadcast **validable en labo strict**, **multi-domaine PTP** possible, où **aucun
plugin ne fait de PTP**, où **l'horloge système n'est pas dans le chemin média**, et qui **respecte
MXL à la lettre** (tout est grain + `media_ts`). Lié : [[docs/chantiers/DPDK_NARROW.md]] (§#20, verdict absolu),
[[docs/reference/PROBE_2110.md]] (monitoring/conformité), mémoire `av-sync-deterministic-phaselock`.

## ★ CONCLUSION (2026-07-08) : SR-IOV = la voie de référence (PF kernel-PTP + VF DPDK-narrow)

Après avoir déroulé PTP/2022-7/narrow puis 2 agents d'investigation (build + source), **l'architecture
cible est SR-IOV** — et elle **supersède la décision « port DPDK → PTP libmtl »** des sections plus bas.

**Le principe** : sur UN port E810, une **PF sur le driver `ice` kernel** (ptp4l/phc2sys disciplinent
le PHC partagé clock4 en fréquence via adjfine — comme aujourd'hui) **+** une **VF en DPDK/vfio** (le
moteur 2110_io : narrow rate-limit matériel, flow steering). Sur la même carte bi-port en 2022-7 :
port0/port1 chacun = PF-PTP (kernel) + VF-narrow (DPDK) → **2022-7 + narrow HW + PTP kernel fiable +
PTP par-port (pas de SPOF) + zéro port dédié + zéro carte en plus**.

**Pourquoi ça lève le verrou n°1 (dérive fpt), et la vraie raison (Agent B) :** la dérive n'était PAS
intrinsèque à MTL — c'était un **artefact du full-vfio** : en full-vfio sur la PF, ptp4l perd le
netdev → clock4 free-run → REALTIME dérive → fpt dérive. Le moteur lit `CLOCK_REALTIME` (pas le PTP
interne libmtl ; et sur VF le timesync HW est PF-only de toute façon → horloge interne = TSC). **SR-IOV
garde la PF sur le kernel → ptp4l reste vivant → REALTIME reste = TAI → le pacing narrow (ancré
REALTIME) ne dérive plus.** Donc pas besoin de `MTL_FLAG_PTP_ENABLE`, ni du patch adjust_freq (voie A
abandonnée), ni de la correction soft (voie C, réservée éventuellement à une sonde full-DPDK isolée).

**Faisabilité (Agent A, build sur dl360-1 / noyau réel `6.12.94+deb13-amd64`, PAS 6.14-pve) :**
in-tree = FDIR VF + max_tx_rate statique mais PAS le runtime-RL-narrow → insuffisant. **Out-of-tree
`ice` 2.6.6 (github.com/intel/ethernet-linux-ice) + patches Kahawai `patches/ice_drv/…` = BUILDE
PROPREMENT** (vermagic exact, symboles `ice_vc_add_fdir_fltr`/`ice_vc_cfg_q_bw`, burst 2 KB narrow ;
zéro mismatch d'API — le mur de juin sur 7.0.2-pve a disparu). Côté DPDK iavf : patches déjà dans
l'arbre MTL (rate-limit runtime iavf) → **narrow RL + FDIR + RL-runtime supportés sur VF**.

**Recette** : hôte tourne le **driver `ice` patché Kahawai** (pas le stock, sinon RL VF → fallback
TSC silencieux → dérive) ; `script/nicctl.sh create_vf <PF_BDF>` → bind vfio-pci → passer le **BDF de
la VF** au moteur (déjà agnostique BDF→DPDK, PMD auto `net_iavf`) ; DDP hérité de la PF (COMMS 1.3.63).

**BANC VF SR-IOV 2026-07-08 (VF débloquée, MMIO OK) — VERDICT NET :**
- ✅ **Cœur PROUVÉ** : `ice` Kahawai 2.6.6 chargé, **VF créée + moteur RX dessus à 50 fps**, PF reste
  kernel avec **ptp4l LOCKÉ** (rms 35 ns, phc2sys REALTIME ±1-11 ns). Le « garder ptp4l vivant sur la
  PF » marche.
- ✅ **VF narrow PRODUCTION PROUVÉ** : RX sur VF `cinst_max=1`/`vrx_span=2` (narrow franc du flux
  entrant). **ET RL MATÉRIEL TX CONFIRMÉ sur la VF iavf** (2ᵉ passe) : `dev_rl_init_nonleaf_nodes`
  (arbre TM Kahawai), `dev_tx_queue_set_rl_rate q1 link to shaper`, `mt_dev_create feature 0x34, tx
  pacing ratelimit`, `tv_attach pacing way: ratelimit` — **AUCUN `fallback to tsc`/`rl init fail`**.
  TX émet 50 fps / 2172 Mb/s, RestartCount 0, ptp4l reste locké sur la PF. Le patch Kahawai (RL +
  TM hierarchy) **tient sur VF**. (NB déploiement : générateur TX = `POST :8081/tx {gen_enabled}` ;
  `TX_COUNT`≥1 requis pour créer un slot, pas seulement `ACTIVE_TX_COUNT`.)
- ❌ **MESURE de conformité sur VF NE MARCHE PAS** : le parser marque les mbuf **`untrusted … pkts time
  for timing parser`** en continu (la VF n'a PAS le timesync HW, PF-only → pas de timestamp discipliné
  par le PHC) → `fpt=17 ms` qui dérive (~17,6 µs/s), `compliant=failed`. **La gap 1 (Agent B) est
  confirmée : un VF ne peut pas fournir un timestamp GM-discipliné au parser.**
- **CONCLUSION nuancée** : SR-IOV **valide pour la PRODUCTION narrow** (moteurs TX/RX sur VF, narrow
  franc, PTP kernel vivant, zéro tax de port, 2022-7). **Mais la SONDE de conformité (verdict absolu)
  exige une PF** (HW timesync) — pas une VF. C'est cohérent : la sonde est un instrument dédié → sur PF.
  Reste ouvert pour le verdict absolu en DPDK : PF avec ptp4l discipliné (via port frère kernel) OU la
  correction soft (voie C). Le média de prod, lui, n'en dépend pas.

**Bonus** : le piège vfio→ice qui perd le PHC (`PHC=none`, cf. docs/chantiers/DPDK_NARROW.md) **disparaît** (la PF ne
bascule jamais en vfio). **Contrainte** : la VF ne peut jamais être un maître PTP autonome (timesync
PF-only) → la discipline vient toujours de ptp4l/PF-kernel ; cohérent avec le modèle, c'est son intérêt.

**BANC SR-IOV 2026-07-08 — PARTIEL : moitié PF PROUVÉE, VF gated BIOS.**
- ✅ **PROUVÉ** : driver `ice` Kahawai 2.6.6 build+charge sur le noyau réel `6.12.94+deb13`, DDP COMMS
  1.3.63 OK, et **la PF reste sur ice-kernel avec ptp4l/phc2sys LOCKÉS sous le driver patché** (rms
  13-49 ns). Le cœur de l'archi (PF ne bascule jamais vfio, ptp4l ne fault pas) est confirmé.
- ❌ **BLOQUÉ (plateforme, pas driver)** : `sriov_numvfs` → `not enough MMIO resources for SR-IOV
  (-ENOMEM)`. La fenêtre préfetchable root-port = **36 Mo 32-bit**, saturée par les 2 PF (32 Mo
  chacun) → zéro place pour l'aperture VF ; **pas de MMIO 64-bit above-4G** exposé par le firmware.
  → **GATE 0 du banc VF = RBSU HPE « Above 4G Decoding / PCIe MMIO above 4GB » + SR-IOV activé +
  reboot** (± `pci=realloc`). Action opérateur (console/iLO). Tant que non fait, aucun banc VF possible.
  **✅ RÉSOLU (2026-07-08) via iLO Redfish** : sur HPE Gen10 il n'y a pas de toggle « Above 4G » ;
  le levier = attribut BIOS **`PciResourcePadding: Normal → High`** (`Sriov` déjà Enabled). PATCH
  Redfish `/redfish/v1/systems/1/bios/settings/` + `ComputerSystem.Reset` → au reboot, **VF créée
  sans `-ENOMEM`** (`Enabling 1 VFs with 17 vectors`, vs 65 vectors/-ENOMEM avant). `pci=realloc`
  NON nécessaire. iLO du nœud = <adresse iLO> (Redfish OK, creds en DB nodes.ilo_*).
- ⚠ La mesure décisive (dérive fpt disparue, narrow RL sur VF, timestamp RX VF) reste **OUVERTE**
  (gated sur ce reboot). Procédure A→B prête. Piège : irdma in-tree ne recharge pas contre l'ice 2.6.6
  out-of-tree (`Unknown symbol ice_*_rdma_qset`) → « laisser l'ice patché » = irdma cassé.

**Ce que ça révise** : « full-DPDK partout » et « port DPDK → PTP libmtl » étaient de mauvaises cibles
(elles créaient le verrou n°1 et le tax PTP+2022-7). **La bonne cible = SR-IOV par carte** : kernel pour
l'horloge (linuxptp éprouvé, par-port, redondant), DPDK/VF pour le data-plane narrow. Les phases §7
ci-dessous restent valides pour la partie MXL/genlock/réf-maison ; seule la ligne « qui possède le PTP »
bascule sur « ptp4l kernel sur la PF, toujours ».

## 0. Le recadrage : 3 plans distincts (ne pas les confler)

Le piège historique = confondre **temps média** et **horloge système**. Ce sont trois plans séparés :

| Plan | Rôle | GM-exact ? | Porté par |
|---|---|---|---|
| **1. Discipline PHC** (par interface) | verrouiller l'horloge *de chaque carte* sur son GM | **oui, dur** | un servo logiciel : `ptp4l` kernel *ou* PTP interne libmtl |
| **2. Temps média** (sur le bus MXL) | dire à chaque grain quel instant GM il représente | oui (hérité du plan 1) | le **`media_ts`** des grains + un **flux de référence maison** |
| **3. Horloge système** (une par nœud) | logs, fraîcheur MXL, coordination | **non** | NTP (slew) — *jamais une référence média* |
| **(transverse) Monitoring** | diagnostic strict | — | télémétrie par interface (offset/GM/holdover) ⊕ conformité sonde |

**Réalisation porteuse** : dès qu'un nœud sert **plusieurs domaines PTP**, l'horloge système (unique)
ne peut pas être la référence média — elle ne suit qu'un GM. Donc **le média ne passe JAMAIS par
l'horloge système** ; il passe par le PHC *de son interface*, propagé via `media_ts`. Le multi-domaine
devient gratuit, et MXL est respecté (tout roule sur les timestamps de grains).

## 1. Contraintes matérielles (à poser noir sur blanc)

- **Les PF d'une même carte partagent UN PHC** (E810 : les 2 ports = clock 4). Donc « interfaces sur
  des PTP différents » ⇒ **cartes différentes**. Deux réseaux média à domaines distincts sur la même
  carte = impossible (un PHC ne suit qu'un GM). **Garde-fou UI obligatoire.**
- Un port en **vfio (DPDK)** n'a plus de netdev kernel → `ptp4l` kernel impossible dessus → c'est le
  **PTP interne libmtl** qui discipline son PHC. Un port **AF_XDP** garde `ptp4l` kernel.
  → **le propriétaire du servo se dérive du PMD de l'interface.**
- Round-trip **vfio→ice** fait perdre le PHC du PF secondaire (récup : `echo 1 > …/remove ; echo 1
  > /sys/bus/pci/rescan`, cf. docs/chantiers/DPDK_NARROW.md).

## 2. Décisions actées (2026-07-08)

| Sujet | Décision |
|---|---|
| **Multi-domaine** | Très souvent **un seul** domaine/nœud. Concevoir **per-interface dès le départ** (multi-ready), déployer un domaine au début. Le « choix d'une référence » ne concerne QUE l'horloge système, **pas** le média (per-interface automatique). |
| **Producteurs sans source** (player playout, générateurs) | **Genlock au TX + flux de référence maison.** Les producteurs n'ont besoin que d'une **cadence**, ni PTP ni horloge système. |
| **Horloge système** | **Découplée (NTP slew).** Le média n'en dépend jamais. (Impact quasi nul, cf. §5.) |
| **Servo PTP** | **Par interface selon le PMD** : `ptp4l` kernel sur AF_XDP, PTP interne libmtl sur DPDK/vfio. |

## 3. Le pivot unique dont tout découle

> **Le moteur 2110_io lit le PHC de l'interface pour stamper `media_ts`, plus `CLOCK_REALTIME`.**

Aujourd'hui libmtl lit `CLOCK_REALTIME` (`ptp_from_real_time`, mt_ptp.c) → d'où la dépendance à
`phc2sys → REALTIME`. Le pivot :
- port **DPDK/vfio** → `MTL_FLAG_PTP_ENABLE` (+ `MTL_FLAG_PTP_PI`) : libmtl **verrouille ET lit** le
  PHC lui-même (esclave PTPv2 domaine du GM). Optionnellement `MTL_FLAG_PHC2SYS_ENABLE` s'il faut
  garder REALTIME GM-scale (confort, non-média).
- port **AF_XDP** → lire le **PHC kernel** (`/dev/ptpN` de l'iface, discipliné par `ptp4l`) au lieu de
  REALTIME. (Question ouverte, cf. §9.)

Ce pivot est **le préalable** au découplage système (§5) et au multi-domaine (un REALTIME unique ne
peut pas servir 2 domaines ; des PHC per-interface, oui).

> **⚠ Gate 0 (2026-07-08) : le pivot 0.39.11 (`ENGINE_PTP=libmtl`) ne fait que la MOITIÉ du chemin.**
> Mesuré au banc (full-DPDK, flux réel narrow VTX-02) : libmtl **verrouille bien le GM** (auto L2→L4
> pour SMPTE 2059-2, lock esclave ~30 ns, domaine 127) — l'horloge INTERNE libmtl (epoch/pacing TX)
> est GM-alignée. **MAIS il ne discipline PAS le PHC de la NIC** qui timestampe les mbuf RX (aucun
> `rte_eth_timesync_adjust`/phc2sys loggé) → le PHC RX free-run à ~1,44 ppm → **`fpt` dérive
> ~1 440 ns/s** (pire que les 200 ns/s du §20). Conséquence : **la Phase 1 doit explicitement écrire
> la discipline sur le PHC de l'interface RX** (côté libmtl : steering du PHC PMD via
> `rte_eth_timesync_adjust_freq`, ou `ptp_get_time_fn` lisant le PHC PMD + seeding), pas seulement sur
> l'horloge interne. Le lock GM L4 est acquis et réutilisable.
>
> **RACINE EXACTE (banc diagnostic 2026-07-08, RX-only) — pas ce que je croyais.** Le timesync
> **s'ACTIVE bien** : `dev_start_timesync()` OK, `feature 0x76` (bit `MT_IF_FEATURE_TIMESYNC=0x02`
> posé), `no_timesync=false`, PTP L4 locké ~30 ns. Donc PAS « timesync_enable échoue ». Le vrai
> coupant : **`MTL_HAS_DPDK_TIMESYNC_ADJUST_FREQ` n'est PAS compilé** dans notre build (0 occurrence
> « adjust freq » dans `libmtl.so`). Cause : `rte_eth_timesync_adjust_freq` est une **API NON standard
> ABSENTE du DPDK vanilla** (rte_ethdev.h) ; le HW ice sait discipliner la fréquence du PHC (le driver
> KERNEL le fait via `adjfine` pour ptp4l/phc2sys) mais le **PMD DPDK ice ne l'expose pas**, et MTL ne
> ship aucun patch DPDK pour ça sur 26.03. → Le servo PI de libmtl calcule une correction de FRÉQUENCE
> mais n'a **aucun chemin HW** pour l'appliquer → le PHC RX free-run à **~1,15 ppm** → **fpt dérive
> ~1 150 ns/s** (mesuré : 773→541 µs sur 201 s), mbuf RX `untrusted`. cinst=1/vrx_span=1 (narrow franc)
> tout du long — seule la PHASE absolue dérive.
> **Investigation (A) 2026-07-08 : AUCUN patch tout prêt.** `MTL_HAS_DPDK_TIMESYNC_ADJUST_FREQ` +
> `rte_eth_timesync_adjust_freq` n'existent dans MTL QUE pour le driver **igc/TSN** (DPDK 23.03/23.07,
> cartes I225/I226 LaunchTime/Qbv) — PAS pour ice/E810, PAS pour 26.03. DPDK vanilla n'a pas cette API.
> → un E810 en **pur DPDK ne peut PAS discipliner la fréquence de son PHC** avec la stack actuelle (le
> KERNEL le fait via adjfine, DPDK non). Voie (A) = **écrire un nouveau patch PMD ice** (dev DPDK réel).
> **RECADRAGE structurant** : ce gap frappe les **timestamps mbuf RX HW** → la **MESURE de conformité
> (verdict absolu)**. Il ne frappe PROBABLEMENT PAS : (i) le **TX narrow de prod** — l'epoch TX utilise
> l'horloge INTERNE libmtl, freq-asservie par le servo (lock ~30 ns STABLE ⇒ fréquence asservie) → sortie
> GM-exacte ; (ii) la **conso RX normale** (multiview) — phase-lock sur le contenu, pas le temps absolu.
> À VALIDER : mesurer la conformité de la sortie TX narrow (hypothèse « TX OK en full-DPDK »).
> **Le verrou n°1 = discipliner la FRÉQUENCE du PHC en DPDK.** 3 voies (cf. §7 note Gate 0) :
> (A) **patcher le PMD DPDK ice** pour exposer `rte_eth_timesync_adjust_freq` (le HW le supporte ;
> Kahawai/MTL l'a fait sur d'anciennes versions) → DPDK self-suffisant, la vraie voie full-DPDK, mais
> la plus lourde ; (B) **kernel discipline le PHC PARTAGÉ via un PF frère** (une PF sur ice+ptp4l, la
> PF média en DPDK lit le même PHC freq-discipliné) → contourne le gap, mais exige que le frère voie le
> GM (fragile, spécifique carte) ; (C) **corriger les timestamps mbuf en SOFT** dans le moteur avec
> l'offset PTP de libmtl (pas de patch DPDK, code moteur/parser ciblé, mais approximatif).

## 4. Le flux de référence maison (genlock logiciel = « black burst »)

Pièce **centrale**, pas un accessoire : la source de genlock de **tout producteur** (mélangeur,
multiview, player, générateur).

### Qui le publie
Le **moteur 2110_io** (seul à posséder le PHC), **un flux par (domaine × cadence)**. Aucune
machinerie de timing nouvelle : sa **boucle TX tique déjà sur l'époque PHC** → il suffit d'écrire un
grain de référence à chaque époque. Existe **en permanence**, même sans média réel.

```
sur chaque époque PHC K :          # la boucle TX fait déjà ce tick
    placer les trames TX sur le fil   (existant)
    ref_writer.open_grain(index=K)    # NOUVEAU : 1 grain de réf/époque
    ref_writer.commit(payload_K)
```

### Contenu d'un grain de référence
Pas de pixels — sa valeur est son **timing**. Grain minuscule portant : **index de grille K**,
**media_ts** (TAI ns), **grain_rate**, + bonus **santé** : **identité GM**, **domaine**, **état servo
PTP** (locké/holdover), **offset courant**. → double emploi comme **signal de santé genlock/PTP** sur
le bus (monitoring gratuit).

### Cadence
Cadence média (entrelacé → cadence **champ**, 1 grain = 1 champ). Une installation = **une cadence
dominante** → **un seul flux** en pratique. Cadences multiples → un flux par cadence ; sous-multiple
(25 depuis réf 50) → 1 grain sur 2. Multi-domaine → un flux par domaine, le producteur choisit celui
de son **domaine de SORTIE**.

### Comment un plugin s'y verrouille
On remplace le mode d'index « tai » (`mxlGetCurrentIndex`, qui lit l'horloge **système**) par un mode
**« genlock »** qui lit l'index de la **référence** (qui vient du **PHC**) :

```python
ref = Reader(inst, ref_name_for(output_domain, rate))
writer = Writer(..., index_mode="genlock", ref=ref)
last_k = None
while running:
    k = ref.wait_next_index(last_k)      # BLOQUE jusqu'au prochain tick de grille (PHC)
    last_k = k
    inputs = [rd.get_latest() for rd in input_readers]   # dernier grain dispo/entrée
    frame  = composite(inputs, k)                        # répète/fallback si retard/absent
    idx, gi, view = writer.open_grain(index=k)           # sortie sur le MÊME index K
    render_into(view, frame); writer.commit()
```

Propriétés qui tombent toutes seules : cadence = PHC (régulière, GM-exacte) ; index sortie = index
grille (media_ts correct, phase-aligné avec **tous** les producteurs) ; **zéro appel à l'horloge
système** (`mxlGetCurrentIndex`/`now_tai` disparaissent → débloque le découplage NTP).

### Cas limites (broadcast-grade)
| Cas | Comportement |
|---|---|
| Démarrage, réf absente | **free-run** cadence nominale, puis **snap** dès que la réf apparaît |
| Entrée en retard/absente | rendu au tick (répétition / fallback noir-gel) — jamais de blocage |
| **⚠ Réf qui s'arrête** (PHC perd le lock / moteur down) | **HOLDOVER** : timeout ~2-3 trames → **free-run cadence nominale** (la sortie CONTINUE) **+ alerte**. Non-négociable labo. |
| Cadence sous-multiple | tick 1 grain sur N |
| Multi-domaine | réf du domaine de sortie ; entrées d'un autre domaine prises au grain le plus proche du tick |

## 5. Mélangeur / multiview : sortie régulière et stable

Un compositeur **consomme N entrées ET produit une sortie**. Trois principes :

1. **Sortie sur la grille partagée, jamais en roue libre** : produit la trame **d'index K** sur la
   grille TAI/GM, tick fourni par le **flux de référence maison** (ou une entrée de référence). Index
   sortie = position grille → régulier et on-grid par construction.
2. **Ne JAMAIS bloquer sur une entrée** : au tick K, rassembler le **dernier grain dispo par entrée**
   (répétition si retard, fallback si absente), composer, émettre. Cadence sortie **découplée** du
   comportement des entrées.
3. **Le TX 2110 est l'autorité de phase FINALE** (sortie 2110) — mais **pas toujours présent**
   (sortie streamer/MXL) → la **régularité propre** du compositeur (principe 1) porte tout.

Le compositeur **ne fait aucun PTP** : le temps GM l'atteint par (a) le `media_ts`/index de ses
entrées + de la réf maison, (b) le genlock TX. Sa stabilité dépend d'une seule chose côté PTP : **le
PHC verrouillé sur le GM** (et monitoré).

### Audit — qui pace réellement sur l'horloge système (2026-07-08)
Rassurant : l'archi défaut est déjà *free-run + genlock TX*.

| Qui | Dépend de l'horloge système GM ? | Impact passage NTP |
|---|---|---|
| Producteurs MXL mode `free` (**DÉFAUT**) | NON (compteur libre interne) | **AUCUN** — genlockés au TX |
| Fraîcheur / `last_write_time` | NON (différence sur la même horloge) | **AUCUN** — NTP-slew suffit |
| Plan de contrôle (watchdog, sampler, cfg_stamp, SDP origin-id) | NON (housekeeping) | **AUCUN** |
| Producteurs mode `tai` (`mxlGetCurrentIndex`) | OUI (grille = horloge système) | → rebrancher sur la **réf maison** |
| `media_ts` du moteur (`ptp_from_real_time`) | OUI | **le pivot §3** → lire le PHC |

Donc découpler l'horloge système en NTP est **quasi sans impact** : seuls le `media_ts` du moteur
(qu'on veut de toute façon passer sur le PHC) et les rares producteurs `tai` (→ réf maison) exigent
le temps GM. Fraîcheur MXL et contrôle : NTP en **slew** (pas de step) suffit.

## 6. Monitoring (exigence labo)

Télémétrie **par interface** unifiée quel que soit le servo : offset au GM, path-delay, identité/
domaine GM, état servo (lock/holdover), transitions. Collecteur = `app/ptp.py` (sampler + journal
d'événements). Si libmtl possède le PTP → brancher son callback **`ptp_sync_notify`** dans le même
pipeline. **Corréler** avec le FPT/conformité de la sonde (PROBE_2110) : c'est le couple
**PTP-nœud ⊕ conformité-flux** qui donne le diagnostic strict. Le payload « santé » du flux de
référence (§4) alimente une tuile genlock/PTP.

## 7. Séquencement (du pivot vers le reste)

| Phase | Contenu | Note |
|---|---|---|
| **1 — Pivot** | moteur lit le **PHC de l'interface** (dpdk `MTL_FLAG_PTP_ENABLE` ; af_xdp `/dev/ptp`) | gros morceau ; préalable au découplage. Tant que non fait, garder phc2sys→REALTIME (mono-domaine) |
| **2 — Genlock/réf maison** | flux de référence par domaine (boucle TX) + `Writer(index_mode="genlock")`/`Reader.wait_next_index`/holdover dans bobimxl + bascule des producteurs | **bobimxl PROTOTYPÉ** (commit df88b27, additif/opt-in, non branché) ; RESTE = publieur de réf côté moteur + bascule multiview/mixer/player |
| **3 — Découplage système** | REALTIME → **NTP slew** ; retrait de la dépendance média à phc2sys | débloqué par Phases 1+2 |
| **4 — Monitoring unifié** | `ptp_sync_notify` libmtl → `ptp.py` ; tuile santé réf ; corrélation sonde | |
| **Transverse — schéma** | `node_interfaces.ptp_domain` (+ `ptp_owner` dérivé du `pmd`) + garde-fous UI (1 carte = 1 PHC = 1 domaine) | |

> **Gate 0 — VERDICT 2026-07-08 : NO-GO full-DPDK maintenant (2 verrous caractérisés).**
> 1. **Pivot PTP incomplet** (encadré §3) : GM locké (~30 ns) mais PHC RX non discipliné → fpt dérive
>    ~1 440 ns/s. Reste = discipline PHC de l'interface RX (travail libmtl).
> 2. **Charge TX** : 6 TX RL en rafale au boot → crash-loop daemon (~45 s, issue #13 TM-hierarchy).
>    RX-only STABLE (50 fps, narrow franc, RestartCount 0). Reste = échelonner création TX + patch #13.
> **Acquis** : lock PTP L4/SMPTE-2059 ~30 ns en DPDK ; RX DPDK 50 fps narrow franc. **Repli hybride
> confirmé sûr.** NB : en AF_XDP/hybride le PHC est discipliné par ptp4l KERNEL → le verrou n°1 ne se
> pose PAS (la prod actuelle en témoigne) ; il est spécifique au full-DPDK.

## 8. Ce que ça donne

- Média **per-interface, GM-exact**, porté par les PHC et les `media_ts`.
- **Aucun conteneur aval ne fait de PTP** (ils rident la réf maison / sont genlockés au TX).
- Horloge système **citoyen de seconde zone** (NTP, jamais dans le chemin média).
- **Multi-domaine gratuit** (chaque carte son GM).
- **MXL respecté à la lettre** (grain + `media_ts`).
- **Monitoring par-interface + corrélation sonde** → exigence labo couverte.

Le seul vrai chantier est la **Phase 1** (moteur lit le PHC) ; le reste en découle, incrémental.

## 9. Question ouverte

Sur les ports **AF_XDP** : le moteur lit-il le **PHC kernel** (`/dev/ptp`, découplé de REALTIME dès
maintenant, plus propre) ou garde-t-on `CLOCK_REALTIME` tant qu'on n'est pas en DPDK (moins de
changement mais reste couplé au système) ? — à trancher avant d'attaquer la Phase 1.
