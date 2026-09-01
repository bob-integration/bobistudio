# Analyseur / sonde de flux ST 2110 (`probe_2110`)

Outil de mesure et de monitoring des flux ST 2110, né du chantier DPDK/narrow (besoin de valider
la conformité 2110-21 sans scope matériel) mais **produit à part entière**. Chantier de conformité
narrow associé : `docs/chantiers/DPDK_NARROW.md`.

## Orientation (2026-07-07)

Deux usages complémentaires :
- **Analyseur ponctuel généraliste, piloté par NMOS** (usage de base) : on braque l'outil sur
  n'importe quel flux déclaré au registre IS-04 → abonnement IS-05 → rapport complet en direct.
- **Surveillance longue durée** sur les signaux importants, avec **journal d'événements horodatés**
  (silence, black, freeze, hors-normes vidéo/audio, pertes, sortie du gabarit narrow, PTP…).

## Fait structurant : le palier conformité est DÉJÀ dans libmtl (wrapping, pas build)

`lib/src/st2110/st_rx_timing_parser.c` (SHA MTL épinglé 32b1b4e) : `rv_tp_on_packet` calcule
Cinst/VRX/IPT/FPT/latency par paquet ; `rv_tp_compliant` rend un verdict par trame
`FAILED|WIDE|NARROW` + `failed_cause[64]`, seuils narrow/wide dérivés du format selon 2110-21.
Activé par le flag public `ST20P_RX_FLAG_TIMING_PARSER_META` → lu via `st_frame_tp_meta()`
(st20_api.h:451, `st20_rx_tp_meta`). Réimplémentation fidèle du modèle EBU pi-list. Audio couvert
(dpvr/tsdf, `ST30_RX_FLAG_TIMING_PARSER_*`). ANC 2110-40 : pas de gabarit -21 (non couvert).
On compile déjà libmtl dans l'image `bobi-mtl` → l'analyseur est un **wrapper**. EBU LIST (stack
web+Mongo+Influx) écarté comme moteur, gardé comme oracle de validation croisée pcap ponctuel.

## Cœur posé + première mesure narrow réelle (2026-07-07)

**Cœur de la sonde committé** (0.39.4, `mtl_rx.c`) : parser env-gaté `TIMING_PARSER=1` (défaut OFF)
→ `MTL_FLAG_ENABLE_HW_TIMESTAMP` + `ST20P_RX_FLAG_TIMING_PARSER_META` + lecture `st_frame_tp_meta`
→ `:8080` par receiver : `compliant`/`failed_cause`/`cinst_max-avg`/`vrx_max-min-avg`/**`vrx_span`**/
`fpt`/`latency`.

**Banc loopback (câble direct 2 ports E810 dl360-1)** : générateur bars 1080p50 422-10 port A →
câble → analyseur `TIMING_PARSER=1` port B. Verdict par mode de pacing TX :

| Pacing TX | `cinst_max` | `vrx_span` (intra-trame) | Lecture |
|---|---|---|---|
| **rl** (matériel) | **1** stable | **5** stable | **NARROW franc** — étalon HW quasi idéal |
| **tsc_narrow** | **2–4** | **10–16** (rare pic 322) | **Narrow exploitable mais marginal** (~2–3× RL, dépasse un peu la cible ~8) |
| **tsc** (plain) | 4–7 | **~920 000** (trame en rafale) | **WIDE/burst** — parser classe bien wide |

- **RL = vrai narrow, confirmé.** À préférer quand le budget files le permet (≤7 TX/port mono-port
  avant patchs #13/#18). tsc_narrow = repli narrow SANS plafond de files, mais plus lâche → à
  valider contre le récepteur cible ; pas équivalent au RL matériel.
- L'écart RL(5)→tsc_narrow(10–16)→tsc(920k) couvre 5 ordres de grandeur → le parser discrimine bien.

**⚠ Caveat méthodo — le verdict `compliant` ABSOLU exige PTP.** Sur ce banc loopback SANS
grandmaster (ptp4l/phc2sys coupés, ports en vfio), l'horloge de capture RX (PHC E810) tourne libre
face à l'horloge d'émission TX → `fpt`/`latency`/`vrx` ABSOLUS pollués → le parser rend `failed
(fpt exceed tr_offset)` pour les 3 modes. On s'appuie donc sur les métriques **invariantes à la
dérive** : **Cinst** (différences intra-trame) et **`vrx_span`** (amplitude VRX intra-trame). **En
prod** (ptp4l+phc2sys → REALTIME=TAI=grandmaster), le verdict absolu est valable. Durcissement banc
(hors périmètre) : aligner l'horloge de pacing TX sur le PHC de capture (les 2 ports partagent le
PHC clock 4) via `ptp_get_time_fn` utilisateur ou phc2sys → verdict `compliant` absolu au loopback.

**✅ CONFIRMÉ EN PROD AVEC GRANDMASTER (2026-07-08, cf. docs/chantiers/DPDK_NARROW.md §#20).** Sur un vrai flux
narrow (VTX-02, `239.4.21.2:2120`, GM domaine 127) reçu en sonde DPDK/vfio, `fpt` passe de multi-ms
(loopback) à **~782 µs STABLE ≈ tr_offset** (collapse ×1000), `cinst_max=1`, `vrx_span=1` = narrow
franc. Le `failed (fpt exceed tr_offset)` résiduel est **STRUCTUREL libmtl↔libmtl** : l'émetteur pose
son 1ᵉʳ paquet à `epoch+tr_offset−vrx·trs` (st_tx_video_session.c:66) → `fpt` PILE à tr_offset ; le
parser teste `fpt > tr_offset` STRICT (st_rx_timing_parser.c:79) → straddle d'une pointe à la
frontière. **→ Décision de conception sonde** : classer « narrow » un flux `cinst=1`/`vrx_span=1`
dont seul le FPT frôle tr_offset (lecture avec tolérance `≥` + marge de propagation réseau), sinon
la sonde rend « failed » sur des flux narrow parfaitement conformes. NB : le moteur ne pose pas
`MTL_FLAG_PTP_ENABLE` (lit CLOCK_REALTIME) → en DPDK, une fois le port en vfio, ptp4l perd le fil et
`fpt` dérive ~200 ns/s → pour un verdict rock-stable, activer le PTP interne libmtl (mt_ptp.c,
domaine 127) sur le port sonde. **Mesurer vite** après bascule vfio.

## Les trois paliers de mesure et leurs prérequis

| Palier | Ce qu'il sort | Prérequis | Où c'est gratuit |
|---|---|---|---|
| **Transport** | pertes, hors-séquence, redondants, trames incomplètes, débit, RTP↔PTP | compteurs libmtl (`port_user_stats`), aucun horodatage | **chaque RX, AF_XDP inclus** |
| **Conformité** | Cinst/VRX, narrow/wide/failed, FPT, latency | **timestamp HW du mbuf** (`MTL_FLAG_ENABLE_HW_TIMESTAMP`) → **DPDK/vfio** (pas fiable AF_XDP) | **chaque RX migré DPDK, en ligne** |
| **Contenu** | black, freeze, silence, loudness R128, niveaux/gamut illégaux | essence décodée = déjà dans le shm (2110 brut) → numpy/ffmpeg | chaque RX (coût CPU modéré) |

**Contenu = peu coûteux sur 2110 brut** (pas de transcodage) : black/freeze = numpy sur les trames
planar (luma max, diff inter-trame), silence = sur les samples PCM, hors-norme = min/max/plage
Y/U/V, loudness = R128 → surveiller plusieurs signaux en parallèle reste raisonnable.

## Architecture : moteur d'événements PARTAGÉ + sonde « receiver de mesure »

Le **journal d'événements** (seuils → événements horodatés → alertes + persistance) est une capacité
réutilisable sur **n'importe quelle session RX**, pas propre à la sonde :
- un signal important **déjà reçu en production** (par un `2110_io`) est journalisable *là où il est*
  → surveillance longue durée gratuite sur les feeds déjà présents ;
- la **sonde `probe_2110`** = « un receiver dont le but est la mesure », qui en plus sait choisir un
  flux via NMOS et tenir des sessions purement d'analyse (spot ou longue durée).

**Conséquence clé** : le port dédié n'est nécessaire QUE pour analyser un flux **qu'on ne reçoit
pas déjà** (notre propre TX ; une source à laquelle aucun receiver n'est abonné). Pour tout flux
qu'un RX consomme déjà, transport+contenu (partout) et conformité (si DPDK) sont **en ligne**.

## Le point dur : la capture

Le parser conformité tourne DANS une session RX MTL → il faut **recevoir vraiment le flux** sur une
PF (un `mtl_init`/PF, jamais celle du moteur). Chemins :
- **(b) receiver actif sur sa propre PF vfio** — le chemin recommandé (reuse max) : session st20p RX
  + flag parser. Réserve 1 PF E810 dédiée. Mesure le flux tel qu'il arrive au NIC → **la bonne**
  mesure côté RX ; pour un verdict sur NOTRE sender, câbler au plus près (**câble direct TX→sonde**,
  un switch chargé reshape le timing et peut faire paraître wide un flux narrow).
- **(a) port-miroir SPAN → NIC kernel, pcap** — **écarté pour la conformité** (SPAN re-sérialise →
  détruit l'inter-packet timing) ; valable seulement transport/contenu.
- **(c) 2ᵉ nœud sonde** — variante de (b) sur une autre machine.

**Banc narrow autonome (dl360-1)** : les 2 ports E810, le 2ᵉ (`ens1f0np0`) débranché → un **câble
direct entre les 2 ports** = banc de conformité self-contained (moteur mire-TX narrow port A vfio →
câble direct → sonde port B vfio, parser Cinst/VRX), sans switch ni scope. Câble à poser (l'utilisateur
peut le faire).

## Design du plugin

- **Type** `probe_2110`, runtime docker, **réutilise l'image `bobi-mtl`** (même libmtl → aucune image
  à builder). Fork RX-only de `plugins/2110_io/mtl_rx.c` + flag timing parser.
- **Réserve** : 1 PF vfio dédiée (`node_interfaces` pmd=dpdk/pci), hugepages, lcores légers via
  `core_pool.py`. Jamais la PF du moteur. Multi-sessions RX possible (budget de files) pour surveiller
  N signaux.
- **Découverte/abonnement** : réutilise `services/nmos` (registre IS-04) + le path IS-05
  `:8081/nmos/subscribe` existant (controller.py).
- **:8080** : verdict conformité (`compliant`/`failed_cause`/cinst/vrx/fpt/latency) + transport
  (`port_user_stats` libmtl) + contenu (black/freeze/silence/loudness/niveaux).
- **Persistance** : table `alerts` existante (info/warning/error) + table dédiée `probe_events`
  (timeline d'incidents par signal). Pattern sampler = `app/node_health.py`.
- **UI** : page live (spot) façon feux narrow/wide/failed + jauges vs seuils ; tableau de bord
  monitoring avec timeline d'incidents par signal.
- **Alertes** : narrow→wide=warning, →failed=error(+cause) ; silence/black/freeze/pertes>seuil=error.

## Plan par phases (priorité : ponctuel généraliste, puis monitoring)

- **Phase A — Analyseur ponctuel généraliste NMOS** : sélection d'un flux IS-04 → abonnement →
  rapport live (transport + conformité si DPDK + contenu). Un flux à la fois par port. Prérequis
  physique : 1 PF vfio dédiée + idéalement câble direct depuis le TX pour un verdict sender.
- **Phase B — Surveillance longue durée + journal** : abonnements persistants sur les signaux
  importants, moteur d'événements → `alerts` + `probe_events`, tableau de bord timeline. Multi-flux.
- **Phase C** — raffinements : SPAN/pcap (flux non-abonnables, transport/contenu), oracle EBU
  (validation croisée), analyse PTP/2059 fine.
- **Track parallèle (complémentaire)** : greffer le moteur d'événements (transport+contenu, gratuit ;
  conformité si RX en DPDK) aux **receivers de prod** → journalisation des signaux déjà reçus.

## Fichiers de référence
Moteur à forker : `plugins/2110_io/mtl_rx.c` (`st20p_rx_create`, `st20p_rx_get_frame`, stats writer),
`plugins/2110_io/plugin.json`, `plugins/2110_io/docker/controller.py` (relais :8080, /nmos/subscribe).
libmtl (32b1b4e) : `lib/src/st2110/st_rx_timing_parser.c`, `include/st20_api.h:451`,
`include/st_pipeline_api.h` (flags + `st_frame_tp_meta`), `include/st30_api.h` (audio),
`lib/src/mt_ptp.c:1621` (source timestamp HW). Persistance : `app/database.py` (table alerts),
`app/node_health.py` (sampler). Découverte : `services/nmos`.
