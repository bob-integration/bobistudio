# Implémentation SR-IOV (PF kernel-PTP + VF DPDK-narrow) — design

> ## ⛔ SUPERSEDED (2026-07-09) — PIVOT vers **full-PF DPDK**
> **Le SR-IOV est abandonné pour le narrow.** Banc direct `mtl_rx` (0.39.10, VF `0000:11:11.0`) : sous
> `pacing=rl` → **SEGFAULT** (rte_tm de l'iavf cassé ; corroboré par `testpmd show port tm cap` qui HANGE
> sur la VF), alors que `pacing=tsc` tourne. **Le RL (narrow HW conforme) est PF-ONLY** — il ne tourne PAS
> sur la VF iavf. Les affirmations « RL TX HW OK sur la VF » de ce doc (§0, §0.bis…) sont **INVALIDES**
> (mesures probablement faites sur PF-vfio, pas sur la vraie VF iavf).
>
> **Nouveau socle** (cf. mémoire `narrow-full-pf-dpdk-socle` + `docs/chantiers/DPDK_NARROW.md`) :
> - **Média = PF PLEINE en DPDK/vfio** (RL narrow HW OK sur ice-PF, ~128 feuilles ; 2022-7 sur 1 carte) → **1 IP/port** (fini la prolifération d'IP du multi-VF).
> - **PTP découplé de la carte média** : (A) **PTP interne libmtl** (`MTL_FLAG_PTP_ENABLE`, zéro matériel, natif MTL) OU (B) **ptp4l sur une petite NIC** (1G/10G) → `CLOCK_REALTIME` node-wide. **Jamais un 100G dédié au PTP.**
> - Reste à valider (2 bancs courts) : **A-PTP conforme** (exige un grandmaster) + **capacité PF** (max RX + max narrow-TX RL).
>
> Ce document est conservé pour ses **mesures/faits durs** (capacité VF, budget de files, IP, séquences PHC),
> mais son ARCHITECTURE (VF DPDK + PF kernel) n'est plus le plan retenu.

Rendre DÉPLOYABLE par l'orchestrateur l'architecture validée en réel (cf. `docs/reference/PTP_CLOCK.md` : banc
dl360-1 2026-07-08). Aujourd'hui c'est manuel (SSH). Objectif : le narrow HW en production, une carte,
2022-7, PTP kernel fiable, zéro port dédié.

## 0. Ce que le banc a prouvé (= ce qu'on automatise)
Sur une PF média E810 : **la PF reste sur `ice` KERNEL** (ptp4l/phc2sys disciplinent le PHC), on crée
**une VF**, bindée **vfio-pci**, et le **moteur 2110_io tourne sur la VF** en DPDK avec **RL narrow
matériel**. Prérequis : driver `ice` **patché Kahawai 2.6.6** + BIOS **`PciResourcePadding=High`** (MMIO).
Résultats : VF RX 50 fps narrow franc, RL TX HW OK (pas de fallback tsc), ptp4l reste locké sur la PF.
Limite : la **mesure de conformité** (verdict absolu) ne marche pas sur VF (mbuf untrusted) → **la
sonde va sur PF**, pas la prod (hors périmètre de CE chantier).

## 0.bis Capacité narrow SR-IOV — banc caractérisation (2026-07-08)
- **8 leaves RL PAR VF, indépendamment** (mesuré 1/2/4/8/16 VF). Le scheduler de CARTE ne s'épuise PAS
  jusqu'à **16 VF × 8 = 128 leaves** ; le vrai mur est **hugepages/cœurs/nb VF**, pas le NIC.
- **7 sessions TX narrow UTILISABLES par VF-port** (8 leaves − 1 file système). ⚠ **borne DURE** :
  `tx_queues=8` déborde → `mtl_init` **échoue** en entier (`q 8 exceeds RL branched capacity`), pas de
  dégradation gracieuse. → cap 7 enforce côté orchestrateur (SRIOV_VF_TX_CAP, docker_driver).
- **Scaling = MULTI-VF** : un SEUL moteur avec N ports VF → 8 leaves/port (testé 2 ports = 14 sessions).
  Ex. 8 VF-ports = 56 sessions narrow. **VF multi-TC (>8/VF) = dead-end** : l'ADQ multi-TC n'est pas
  négocié par le PMD iavf DPDK (config perdue au rebind vfio) → aucune modif de code libmtl/PMD.

## 0.ter Bande passante & 2022-7 (matériel dl360-1, relevé 2026-07-08)
- **Carte = Intel E810-C for QSFP `[8086:1592]`** (silicium 2×100G). FW 4.80, ice Kahawai_2.6.6.
- **Lien PCIe = Gen3 x16 (8 GT/s), downgradé** de Gen4 (LnkCap 16 GT/s). Gen3 x16 ≈ **126 Gb/s/sens** →
  plafond pratique **~100G/sens** (full-duplex : ~100 TX + ~100 RX). **CHOIX SERVEUR ASSUMÉ** (ce
  serveur n'est pas Gen4) → NE PAS investiguer ; un serveur Gen4 futur débloquera ~200G. Le « 100G »
  vient donc du PCIe, pas d'un SKU limité (le silicium E810-C fait 2×100G).
- **Pour la vidéo, la BP est LE mur** (~22-45 flux 1080p50/carte selon 2022-7), **très en dessous** du
  budget de leaves RL (≥128). Le SR-IOV/narrow n'est pas contraint par les leaves côté vidéo.
- **2022-7 = `ops.port.num_port=2`** dans mtl_rx.c = 2 chemins DMA indépendants. **MESURÉ (banc
  2026-07-08, compteur uncore IIO `bw_out_port0`) : le doublon coûte EXACTEMENT 2× PCIe** (ratio 2,00×,
  linéaire à 1 et 4 TX ; payload DMA 2 fois, aucune réplication HW). Overhead DMA ~2,7 % (2236 Mb/s PCIe
  pour 2178 Mb/s wire). → **2022-7 mono-carte = ~50-60 Gb/s utile** (PCIe/2) ; **non-redondant = ~100-120
  Gb/s** (PCIe ~126). **2 cartes = ~100G utile + vraie redondance** (chaque carte porte 1× sur SON PCIe).
  Mettre red+blue sur 2 PF de la MÊME carte ne change rien (même lien x16 partagé).

## 0.quater Stratégie pacing nœuds mixtes — tsc_narrow vs 2 moteurs (banc 2026-07-08/09)
- **tsc_narrow LÈVE le cap 8-leaves (PROUVÉ A/B)** : 20 RX + 10 TX narrow dans UN moteur (RL échoue).
  tsc/tsc_narrow ne construisent aucun arbre TM → jamais bornés (controller.py:2328). RX libre + TX
  narrow en 1 `mtl_init` = oui.
- **CONFORMITÉ NON MESURÉE (réserve) — mais MÉTHODE trouvée** : tsc_narrow vise bien narrow (VRX cible 8,
  trs narrow, pas wide=720) ; la sonde 2110-21 (Cinst/VRX) exige un RX à HW-timestamp = **PF DPDK recevant
  le flux** (VF→VF n'assemble pas). **Les 2 ports f0/f1 de dl360-1 sont sur un SWITCH** → faire de **f0
  (PF bindée vfio) une sonde MTL RX (`TIMING_PARSER=1`)** qui joint le mcast de f1. **SÉQUENCE (PHC
  partagé) : bind f0→vfio → re-lock ptp4l sur f1 → mesurer.** Contrôle en RL (doit lire « narrow »). →
  config narrow sûre, PASSAGE du gabarit à CHIFFRER par ce banc avant prod tsc_narrow. ⚠ voir caveat.
- **CPU** : tsc_narrow ≈ 3× RL ; MTL dédie ~1 lcore de pacing / ~8 sessions TX (RL ≈ gratuit).
- **Modèle 2 moteurs CONFIRMÉ** (RL TX vf0 + tsc RX vf1 simultanés, 0 interférence). Fallback solide,
  narrow HW garanti, 0 surcoût pacing, mais TX plafonné 7/VF + VF dédiées par rôle.
- **DÉCISION** : socle sur RL (narrow HW garanti) + 2 moteurs pour nœuds mixtes ; tsc_narrow = option
  future à activer après mesure conformité.
- **⚠ CAVEAT OPÉRATIONNEL** : rebinder un port E810 (ice↔vfio) PENDANT que ptp tourne sur le port frère
  RÉINITIALISE le PHC partagé → ptp4l décroche. Ne jamais rebinder un port E810 avec ptp actif sur un
  port de la même carte. Impacte l'ordre du host-prep SR-IOV (créer/binder VF avant de lancer ptp, ou
  accepter un relock).

## 1. Modèle de données (`node_interfaces`)
Aujourd'hui : `pmd ∈ {af_xdp, dpdk}` (dpdk = PF en vfio, qui TUE le PTP → chemin de banc, pas la cible).

**Proposition** : ajouter un mode **`pmd = "sriov"`** sur une interface `media2110` = « PF kernel-PTP +
VF DPDK-narrow ». Les modes deviennent :
- `af_xdp` (défaut) — PF kernel, pas de narrow HW (RL indispo). PTP kernel gratuit. Chemin actuel.
- **`sriov`** (cible narrow) — PF kernel (PTP) + VF DPDK (moteur, RL narrow). **Le nouveau.**
- `dpdk` (PF vfio) — legacy/banc, tue le PTP. À déprécier hors sonde.

Champs à ajouter à `node_interfaces` (migrations idempotentes `init_db`, motif existant) :
- `vf_bdf TEXT` — BDF de la VF créée (rempli au host-prep VF ; ex. `0000:11:11.0`).
- `vf_ip TEXT` — IP média (sip) de la VF (le trafic 2110 sort/entre par la VF). **Décision addressing
  ci-dessous.**
- (`pci` reste le BDF de la **PF** ; `ip`/`cidr` = IP de la PF, kernel, pour PTP/contrôle.)

## 2. Cycle de vie de la VF (host-prep, persistant)
La VF n'est PAS persistante (`sriov_numvfs` retombe à 0 au reboot) → il faut un **host-prep dédié +
persistance systemd** (comme `vfio_bind_plan` le fait pour le PF). Nouveau plan `mtl.sriov_vf_plan(node,
pf_bdf, ...)` (miroir de `vfio_bind_plan`, mais pour VF) :
1. `echo 1 > /sys/class/net/<pf>/device/sriov_numvfs` (idempotent : skip si déjà ≥1).
2. `ip link set <pf> vf 0 mac <MAC stable> trust on spoofchk off`.
3. Découvrir le **BDF de la VF** (sysfs `virtfn0`) → persister dans `node_interfaces.vf_bdf`.
4. Bind la **VF** en vfio-pci (réutilise la logique `vfio_bind_plan` sur le VF BDF).
5. **Persistance boot** : unité oneshot qui refait numvfs + mac/trust + bind vfio au boot (la PF, elle,
   revient sur ice toute seule).
⚠ Garde-fou inverse de `vfio_bind_plan` : ici on **NE bind PAS la PF** (elle reste kernel pour le PTP) ;
on bind la VF. Le `GardeFouVfio` anti-PTP s'applique à la PF (ne jamais la vfio-er en mode sriov).

## 3. Driver `ice` patché (host-prep)
Le RL narrow sur VF exige l'`ice` Kahawai (sinon fallback tsc). Nouveau host-prep `mtl.install_patched_ice
(node)` : build sur le nœud (clone MTL + ice 2.6.6 + patches `ice_drv/2.6.6`, `make`), install
`/lib/modules/$(uname -r)/updates/ice.ko` + depmod, charge (gérer la dépendance **irdma** : rmmod irdma
→ rmmod ice → modprobe ice patché ; ⚠ irdma ne recharge pas contre l'ice 2.6.6 → l'assumer/documenter).
Persistant (updates/ + depmod survivent au reboot). Idempotent (skip si `modinfo ice`=Kahawai_2.6.6).
**Gate** : `verifier().sriov.mmio_error` doit être faux (BIOS OK) avant — sinon la VF ne se crée pas.

## 4. Déploiement du moteur sur la VF (`docker_driver`)
`_media_ifaces` : pour une iface `pmd=sriov`, exposer au moteur le **VF BDF** (`vf_bdf`) comme port DPDK,
et le **VF IP** comme sip. Donc :
- `PORT_PMDS` = `dpdk` pour cette iface (le moteur voit un port DPDK — il est déjà agnostique BDF).
- `PORT_BDFS` = `vf_bdf` (pas le PF BDF).
- `SIPS` = `vf_ip`.
- Montages `/dev/vfio` + DDP (déjà émis quand un port dpdk existe).
- `MTL_PACING` = rl (dérivé du profil narrow, existant).
Le reste du moteur (sessions, headroom, pacing) est **inchangé** (il ne voit qu'un port DPDK).

## 5. PTP (inchangé, ou presque)
La PF reste kernel → `app/ptp.py` (ptp4l/phc2sys par nœud/iface) fonctionne **tel quel** sur la PF. Rien
à refondre côté PTP. C'est tout l'intérêt : le PTP kernel éprouvé reste en place, par-port (pas de SPOF),
compatible 2022-7. (Le moteur sur la VF lit REALTIME, discipliné par ce ptp4l — cf. banc.)

## 6. Addressing — TRANCHÉ (D1)
La PF et la VF sont 2 fonctions distinctes sur le **même subnet média** → **2 IP inhérentes** (ce n'est
pas un choix de conception, c'est ce que le réseau impose avec un PTP **L4/UDP**, le profil SMPTE 2059-2
standard vu au banc) :
- **VF** = **IP média** (`vf_ip`, nouvelle alloc) : le `sip` du 2110 (IGMP + RTP). Portée par le moteur.
- **PF** = garde son **`node_interfaces.ip`** existant, désormais utilisé pour **ptp4l L4** (kernel).
Descendre à 1 IP = seulement si un réseau tourne en **PTP L2** (PF juste up, sans IP) → optim future,
contingent au transport du GM. Défaut = 2 IP (matche le banc : PF .229 PTP / VF .230 média).

## 7. UI / config réseau — décisions (2026-07-08, validées utilisateur)
Tout se passe dans l'éditeur d'interface PAR PORT (`_netIfaceEditForm`, settings.html), là où on choisit
déjà le **rôle** et le **profil narrow** (« Profil d'émission 2110-21 », gaté sur role=media2110).
- **Rôle par port** conservé (2110 / management / RDMA / containers…). Chaque PF = fonction indépendante.
- **Mode datapath par port** : sélecteur `AF_XDP` / `SR-IOV` (le `dpdk` PF-vfio reste caché, banc/sonde).
  Sans contrainte inter-port.
- **Réglages VF par port**, au même endroit : afficher `vf_bdf` + état (VF présente/bindée), bouton
  provisionnement (routes `/sriov-vf`, `/install-ice`). Checklist SR-IOV/MMIO déjà posée.
- **CONTRAINTE même carte → même réseau 2110** : le PHC est partagé (1 carte = 1 domaine PTP). Grouper
  les ports par carte (proxy = préfixe PCI de `node_interfaces.pci`, ex. `0000:11:00.x`). Si un autre
  port de la même carte est déjà sur un réseau 2110 → **verrouiller le sélecteur « Réseau média » sur
  CE réseau** (autres grisés) + explication nommant le port/réseau/domaine (règle « expliquer un
  contrôle désactivé »). Domaine dispo via `_netViewData.ptp.groups[].domain`.
- **Gate BIOS** (mmio_error) déjà en place bloque/avertit avant `sriov`.
- **BUG à corriger (Phase 4)** : les panneaux SFP/edit se referment parfois. Cause = un garde
  anti-fermeture existe (settings.html:5242, saute le refresh périodique `sig` si `.net-mod-detail`
  ouvert ou focus dans la vue) MAIS les refresh MANUELS (après save, changement d'onglet/nœud) passent
  sans garde et réécrivent la table → panneau effacé. Fix : préserver/rouvrir le panneau `edit` ouvert
  après un refresh non-`sig`, éviter les re-render inutiles quand un edit est actif.

## 8. Phases proposées
1. ✅ **FAIT — Schéma + host-prep VF** : colonnes `vf_bdf`/`vf_ip` (+ whitelist) ; `mtl.sriov_vf_plan/
   apply` (create VF + mac déterministe + trust + bind VF vfio + persist unité `bobi-sriov-vf`) ;
   route `POST /api/nodes/<id>/sriov-vf` (persiste `vf_bdf`). **Testé bout-en-bout dl360-1** : VF
   0000:11:11.0 créée+vfio, PF reste ice/kernel (ptp4l actif), persistance OK. Commits 2d68d9b/6cee33f.
2. ✅ **FAIT — Host-prep ice patché** : `mtl.install_patched_ice` (build ice 2.6.6 + patches Kahawai,
   install `updates/`+depmod → actif au reboot, NON-disruptif, idempotent) + route `POST /api/nodes/
   <id>/install-ice`. Idempotence testée dl360-1 ; build validé (agent A). Commit 6361abf.
3. ◑ **Deploy moteur sur VF** : `docker_driver` mappe `pmd=sriov` → VF BDF/IP (PORT_BDFS=vf_bdf,
   SIPS=vf_ip) — code OK, PF/ptp intacts (test agent). **Bloquait** sur budget files PF (9) > 8 leaves
   VF → **FIX** (commit c8f876d) : `_sriov_node` → total_q=8, cap TX=7 borné dur. **Reste : re-tester
   le deploy avec le fix** (default 6 TX doit tenir dans 8 leaves).
4. ⬜ **UI** : mode `sriov` par iface + affichage VF/état (checklist SR-IOV déjà posée).
5. ⬜ **Persistance/robustesse** : unités boot (VF ✅ + ice patché ✅ via updates/), auto-recovery, doc.

## Décisions à trancher (avant de coder)
- **D1 — Addressing** : Option A (PF IP + VF IP séparée) ou B (VF prend l'IP média, PF contrôle) ?
- **D2 — Portée mode** : garde-t-on `pmd=dpdk` (PF-vfio) pour la sonde/banc, ou on le supprime au profit
  de `af_xdp` + `sriov` seulement ?
- **D3 — ice patché** : on l'installe via host-prep automatisé (build sur le nœud), ou on pré-bake une
  image de driver / un paquet (build once) ? (Le build sur nœud = ~2 min, dépend des headers.)
- **D4 — Nb de VF** : TRANCHÉ par le banc → **v1 = 1 VF/PF (7 sessions TX narrow)** ; scaling au-delà =
  **MULTI-VF par PF** (chaque +7, un seul moteur multi-ports ; scheduler carte OK ≥128 leaves). Le
  multi-VF/PF (N vf_bdf par iface) est une extension Phase 3.5 quand >7 TX/port sont requis.
