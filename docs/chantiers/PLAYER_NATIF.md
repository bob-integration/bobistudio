# Player : le chemin temps réel en élément GStreamer natif

Chantier ouvert le 2026-10-10, sur décision de l'exploitation (« vas-y pour l'élément natif, têtes
A/B incluses »). Il suit une journée de banc qui a corrigé, dans le chemin temps réel Python du
player, une cadence divisée par deux, une image figée en lecture, une reprise bloquée et des TC ANC
décalés — cf. `plugins/player/meta.json` 0.24.0 → 0.25.1.

## Pourquoi

Le HD tient en Python (1080p50 : < 1 cœur, 50 trames neuves sur 50). Ce qui ne tient pas :

- **le GIL** : les threads Python se préemptent ; des créneaux manqués ont été mesurés sous
  contention (fondu de stills épinglé sur 3 cœurs, 1 à 6 manqués à la reprise du player) ;
- **la boucle audio à 1 kHz en Python** sur de petits tableaux numpy ;
- **les copies de 8 Mo tenues sous le GIL** pour sortir chaque trame de l'appsink — 33 Mo en
  UHD 10 bits, jamais mesuré ;
- et un **passage au v210** aux frontières, que l'élément MXL amont sait déjà écrire.

## Ce qu'on garde du SDK MXL, et ce qu'on n'en garde pas

`rust/gst-mxl-rs` (v1.1.0) fournit `mxlsink` : un `BaseSink` qui écrit un grain **quand un tampon
arrive**, cadencé par l'horloge GStreamer, un flux par élément, v210 seulement. C'est l'inverse de
ce qu'il faut : notre sortie est cadencée par la **grille TAI**, écrit dans **tous** les états
(lecture, pause, chargement, fin), et porte **quatre flux** (vidéo, audio, deux ANC) au même index.

→ On **n'étend pas** `mxlsink`. On écrit notre élément en réutilisant la crate **`mxl`** (bindings
Rust de libmxl) : instance, écrivain de flux depuis un flowDef JSON, `open_grain(index)` /
`commit(valid_slices)`, écrivain d'échantillons, index courant de la grille, index ↔ TAI.
`mxl-sys` charge libmxl **dynamiquement** (`libloading`, feature `mxl-not-built`) → l'élément
utilise **notre libmxl patchée** (planar), déjà dans l'image. Les patchs planar ne touchent que
`lib/internal/src/FlowParser.cpp` : les en-têtes publics, lus par bindgen, sont ceux de l'amont.

## Architecture

```
Python (plugins/player/script.py) — PILOTAGE
  API :8082, playlist, rundown, macros, état, OP-47, ffprobe, vignettes
  construit :  uridecodebin ─▶ queue ─▶ videoconvert ─▶ videoscale ─▶ videorate ─▶ ┐
               (par piste)  ─▶ queue ─▶ audioconvert ─▶ audioresample ──────────────┤
                                                                         bobiplayout (Rust)
                                                                                    │
  pilote par propriétés : paused, route, gains, TC, flowDefs…                       ▼
                                                              bus MXL : vidéo · audio · anc_1 · anc_2
```

**`bobiplayout`** (plugin `bobimxl`, crate à nous dans `plugins/_media_runtime/gst-bobimxl/`) :

- pads `video` (toujours) et `audio_%u` (à la demande, un par piste du fichier) ;
- leurs fonctions `chain` déposent les tampons dans des **files bornées** et **bloquent** quand
  elles sont pleines → contre-pression jusqu'au décodeur (remplace appsink + file Python) ;
- un **thread temps réel** à lui fait la boucle-grille : une trame par créneau dans tous les
  états, choix de la trame par l'horloge de présentation, grains-champs en entrelacé, tranches,
  bloc audio routé, deux grains ANC — **au même index** ;
- FLUSH / nouveau segment sur un pad → nouvelle génération, ré-ancrage (seek, boucle) ;
- propriétés en lecture : `slots-missed`, `fresh-fps`, `position` (temps média de la trame
  affichée), `eos` ;
- **les flowDefs JSON sont fournis par Python** (bobimxl) : identité et nom des flux STRICTEMENT
  ceux d'aujourd'hui — un consommateur ne doit voir aucune différence (cf. FLOW_IDENTITY.md).

**Têtes de lecture (decks) — structurantes dès la phase 1** (révisé le 2026-10-10) : l'élément
doit SURVIVRE aux changements de clip, sinon la sortie s'interrompt au chargement (aujourd'hui,
c'est le thread Python, extérieur au pipeline, qui la tient). Le pipeline devient PERMANENT ; un
clip = une **tête** (uridecodebin + conversions) branchée sur des pads à la demande
`video_<tête>` / `audio_<tête>_<n>`, débranchée quand elle a fini.
- `active-deck` : la tête à l'antenne ; `next-deck` : la tête ARMÉE, qui prend l'antenne
  d'elle-même, à la trame près, quand l'active a fini (EOS et file vide) — boucle et enchaînement
  de playlist sans trame tenue ;
- la tête suivante se remplit pendant que l'active joue (sa file pleine la bloque : coût = l'état
  du décodeur et ~0,5 s de trames).
- Effet de bord heureux : l'élément n'est pas un `BaseSink` → aucun préroll asynchrone dans le
  pipeline, donc plus d'interblocage de mise en pause (cf. player 0.25.1).

## Phases

| Phase | Contenu | Critère de sortie (banc, sans écran) |
|---|---|---|
| 0 | chaîne Rust, squelette qui se construit et se charge (`gst-inspect`) dans l'image média | élément listé, libmxl patchée chargée |
| 1 | vidéo : grille, maintien, pause, champs, compteurs, **têtes** (active / armée, bascule à la trame) | 25/50 i/s, `fresh-fps` = cadence, `slots-missed` = 0, pause/reprise ≤ 1 trame, **boucle par tête armée : 0 trame répétée** |
| 2 | audio multi-pistes + routage + gains, ANC TC (+ paquets fournis par Python : OP-47) | niveaux routés corrects, têtes vidéo/audio/ANC au même index |
| 3 | bascule du player sur l'élément derrière un réglage `engine` (python / natif), repli python | les 6 MXF du corpus, en 1080i25 / 1080p25 / 1080p50, mêmes bancs que le 2026-10-10 |
| 4 | enchaînement de playlist par têtes (logique côté Python : armer la suivante, retirer la finie) | 0 trame répétée à travers les changements de clip, formats différents compris |
| 5 | image `bobi-media` (étage Rust, tag), retrait du moteur Python une fois validé | build reproductible, `Cargo.lock` versionné |

## Points d'attention

- **Rust ≥ 1.87** (édition 2024 côté SDK) : celui de trixie (1.85) est trop ancien → chaîne
  épinglée dans l'étage de construction. `gstreamer-rs` 0.24 contre GStreamer 1.26 (trixie).
- `Cargo.lock` versionné ; dépendances épinglées.
- Licence : amont Apache-2.0, compatible GPLv3 ; en-têtes SPDX dans nos fichiers.
- Le moteur Python reste **le défaut** jusqu'à la fin de la phase 3 : rien ne part en production
  sans le banc complet.

## Journal

- **2026-10-10** — étude : `mxlsink` écarté (BaseSink cadencé par l'horloge), crate `mxl` retenue.
- **2026-10-10** — **phase 0 validée** : `plugins/_media_runtime/gst-bobimxl/` (Rust 1.88.0,
  gstreamer-rs 0.24.5, `Cargo.lock` versionné) se construit et se charge (`gst-inspect-1.0
  bobiplayout`) ; libmxl chargée PAR SON NOM, instance du domaine ouverte au passage READY.
  Environnement de dev : conteneur `bobi-rust-dev` sur dl360-1 (image bobi-media:0.20 + outils
  GStreamer de dev, clang, rustup 1.88.0 ; caches dans `/srv/bobi-dev/{cargo,rustup,target}`,
  sources dans `/srv/bobi-dev/{mxl,src}`). ⚠ Épinglé sur les cœurs **37-41** : 25-36 sont ISOLÉS
  (jumeaux hyperthread des lcores du moteur 2110), 0-17 au moteur, 18-23/42-47 au pool compute.

- **2026-10-10** — **phase 1 : premier passage réel** (banc `harness/banc_playout.py` dans
  `bobi-rust-dev`, pipeline permanent, une tête uridecodebin par clip, AVC-Intra 1 piste 8 ch) :

  | | 1080p50 | 1080i25 |
  |---|---|---|
  | lecture 5 s | 50/50 neuves, 0 manqué, 0 répétée | 25/25, 0 manqué, 0 répétée |
  | 4 boucles par tête ARMÉE | **0 trame répétée**, 3 créneaux manqués | **0 trame répétée**, 0 manqué |
  | pause / reprise ×5 | figée, reprise 4-19 ms, 0 manqué | figée, reprise 8-39 ms, 0 manqué |
  | tête du flux sur le bus | +50 grains/s | +50 grains-champs/s |

  La boucle SANS TROU est atteinte (le player Python tenait ~165 ms à chaque tour). Reste : 3
  créneaux manqués à 50 i/s pendant l'ajout/retrait d'une tête — à localiser.
  ⚠ Horloge système de dl360-1 ≈ 3 h 39 en avance sur l'horloge matérielle et le contrôleur,
  « System clock synchronized: no », NTP inactif (relevé à 01:59 CEST nœud / 22:20 contrôleur) —
  sans effet sur ce banc (tout est dans le même domaine), mais à traiter : la grille TAI d'un nœud
  décalé n'est pas celle des autres.

- **2026-10-10** — **phase 1 validée sur le corpus** (6 MXF × 1080p50 et 1080i25,
  `tools/bancs/banc_player_natif.py`) : lecture à cadence pleine, **0 trame répétée** en lecture,
  à travers 4 boucles par tête armée (clips ≤ 30 s) et après chaque reprise ; pause figée, reprise
  en 2-39 ms ; MPEG-2 « mixed », clip sans audio, 1080p50 → 25 : tous passent. Reste 1 créneau
  manqué isolé (AVC-I 16 pistes à 50 i/s, démarrage ou boucle) — à localiser.
  Deux corrections en route, trouvées par les messages de diagnostic (`GST_DEBUG=bobiplayout:5`,
  lignes BASCULE / RÉPÉTÉE) : une tête finie sans tête armée n'est plus « en lecture » (son maintien
  comptait comme répétitions) ; la reprise après pause repart de la trame SUIVANTE (l'image en
  pause sortait deux fois à chaque Play — le player Python 0.25.1 a le même décalage, à reporter).

- **2026-10-10** — **phase 2, audio validé** (`tools/bancs/banc_player_natif_audio.py`) : une piste
  par pad `audio_<tête>_<n>` (F32LE 48 kHz, canaux d'origine), file de 4 s par piste, routage des
  canaux source vers les 8 sorties, gain / mute / solo, VU. Mesuré : pistes vues (8 mono, 16 mono,
  aucune, 1 stéréo) ; routage, mute, solo et gain corrects aux niveaux ; flux audio +48 000
  échantillons/s ; **chaque bloc finit exactement une période après le début du grain vidéo de même
  rang** (20 ms en 1080p50, et en 1080i25 sur la grille champ) → audio et vidéo d'un créneau portent
  le même temps TAI ; 0 trame répétée, 0 manqué à travers les boucles.
  Défaut trouvé et corrigé en route : au créneau de bascule (dernière trame de l'ancienne tête
  encore montrée), l'audio était lu dans la NOUVELLE tête au temps de l'ancienne — silence, et le
  ménage du tampon vidait les données de la nouvelle : ~1,2 s de silence après chaque boucle.
  L'audio d'un créneau vient désormais de la tête dont la trame est montrée.

- **2026-10-10** — **phase 2, ANC validé** (`tools/bancs/banc_player_natif_anc.py`) : portage
  octet pour octet de `anc_pack_rfc8331` / `anc_atc_encode` (`src/playout/anc.rs`) ; les grains
  ANC sont écrits par la boucle-grille au MÊME index que la trame. Mesuré en 1080p50 et 1080i25 :
  têtes vidéo / anc_1 / anc_2 identiques ; TC décodé par `bobimxl.anc_unpack` = TC annoncé (base
  embarquée par tête via `deck-meta` + position de la trame montrée) ; OP-47 fourni par Python
  (`anc-extra`) présent avec l'ATC, checksums bons ; TC antenne montant, décompte (fin du clip −
  position + durée après) et remise à zéro corrects.

- **2026-10-10** — **phases 3 et 4 validées en banc de développement** (player 0.26.0, réglage
  `engine`, `tools/bancs/banc_player_natif_api.py` piloté par l'API du player) : les 6 MXF en
  1080p50 et 1080i25, routage, pause/reprise, seek, pas à pas, vitesse, boucle (11 s) et playlist
  de 3 clips de formats différents — **0 trame répétée**.

- **2026-10-11** — **phase 5 : image `bobi-media:0.21`** (étage `playout-builder`, Rust 1.88.0,
  `--locked` ; 800 s sur dl360-1). Construite et posée sur **dl360-1 seul** — pas distribuée, et le
  moteur Python reste le défaut.
  **Premier passage dans un VRAI conteneur** (profil du player : 3 cœurs, 2 Go) et premier défaut
  grave, invisible en banc de développement faute de limite mémoire : **chaque tête retirée restait
  dans le pipeline** (+150 Mo par fichier chargé, OOM au 4e). Cause : le retrait passait à NULL
  AVANT de relâcher les pads, or le fil de streaming de la tête est bloqué DANS l'élément
  (contre-pression) et NULL l'attend — le retrait se figeait pour toujours. Trouvé en comptant les
  éléments du pipeline (37 → 121) ; un `weak_ref` sur le décodeur confirme la finalisation après
  correction (0.26.1). En prime : fils libav bornés (342 → 158 Mo par tête AVC-I), arènes malloc
  bornées, fils de décodage en nice 5 (créneaux manqués au chargement : 4 → 0-1).
  **Mesuré ensuite dans le conteneur, 1080p50 : banc API complet, 0 créneau manqué, 0 trame
  répétée, pic mémoire 715 Mo.** Le « créneau manqué isolé » du journal était la famine de la
  grille à l'ouverture d'une tête.

- **2026-10-11** — **premier relevé UHD (2160p50), NON TENU.** Chemin de sortie seul (mire →
  élément) : 50 écritures/s, 0 manqué, 1,25 cœur. Avec décodage (fichiers de 4 s fabriqués,
  DNxHR HQX 10 bits et H.264 4:2:2 10 bits intra, dans `/srv/mxl-media/tests-player/uhd_*`) :
  - mémoire : un décodeur AVC 4:2:2 UHD pèse **~800 Mo à lui seul** (DPB), même à 1 fil ; deux
    têtes ne tiennent pas dans 2 Go. File de l'élément désormais bornée en octets (0.26.2).
  - conversion/mise à l'échelle mono-fil : 31 images neuves/s en DNxHR, 22/s pour un 1080p remonté.
    Multi-fils (cœurs − 1) → 49-50/s.
  - **reste ouvert : la grille saute 6 à 39 créneaux par 12 s** sans trame répétée — un tour de
    grille dépasse 20 ms. Pas encore localisé : à instrumenter dans l'élément (retard au réveil vs
    durée du tour, copie de 16,6 Mo par grain) et à remesurer sur des cœurs DÉDIÉS — le conteneur
    de développement (37-41) est sur les frères HT des lcores du moteur 2110.
  - ⚠ les cœurs/mémoire d'un conteneur compute viennent du SEUL profil `resources` du manifeste :
    `PATCH /api/containers/<vmid>/resources` est persisté mais jamais lu (cf. TODO). Un player UHD
    ne peut donc pas recevoir plus que le profil.

- **2026-10-11** — **player 0.27.0 : moteur natif SEUL.** Décision utilisateur : le moteur Python
  n'apportait aucune fonction que l'élément n'ait pas (relu ligne à ligne : toute l'API passait
  déjà par `restart()` → `_native_restart`), et gardait ses deux défauts (~1 s d'image tenue à
  chaque changement de clip, image en pause sortie deux fois à la reprise). Retirés : boucles
  vidéo/audio Python, pipeline appsink, écrivains MXL Python, réglage `engine` (2333 → ~1500
  lignes). Sans l'élément, le player refuse de démarrer et le dit — fini le repli silencieux.
  Deux écarts trouvés en relisant : (1) le flowDef du natif OMETTAIT `slice_height` en mode
  tranche — rétabli ; l'élément valide toutes les tranches d'un coup (trame déjà décodée). Lu par
  un consommateur : 100/100 grains complets, 30 tranches ; (2) un média audio seul sort muet —
  dans les deux moteurs (cf. TODO). Le gestionnaire SIGBUS Python est retiré : sous l'élément, il
  aurait changé une faute de mapping en boucle infinie au lieu d'un redémarrage par l'agent.
  Banc API complet sur dl360-1 (conteneur réel) : identique à 0.26.2 — 0 créneau manqué, 0 trame
  répétée, pic 715 Mo. **Image 0.21 toujours sur dl360-1 seul** : la distribution reste à faire.

- **2026-10-11** — **player 0.28.0 + image 0.22 : lecture en différé d'un fichier en cours
  d'enregistrement.** Deuxième élément dans le plugin : `bobigrowsrc` (`src/growsrc/`, URI
  `bobigrow://`, choisi par uridecodebin d'après le schéma). Pourquoi une source : filesrc mène
  qtdemux et mxfdemux jusqu'à la fin DU MOMENT, puis EOS ; et en flux poussé (appsrc), mxfdemux
  refuse le MXF d'ffmpeg même complet. La source reste en mode TIRÉ (seeks des démuxeurs
  conservés), déclare une taille INCONNUE (aucune lecture bornée par GstBaseSrc) et fait attendre
  une lecture qui dépasse la fin. Deux pièges trouvés au banc : (1) répondre « -1 » à la requête de
  durée en octets envoyait qtdemux lire son mfro à 2^64 − 17 ; (2) ne pas répondre faisait échouer
  typefind — on répond la taille ACTUELLE. Fin : propriété `growing` posée à faux par le player
  quand le sidecar du recorder passe à « closed », ou 15 s sans octet neuf. Mesuré (conteneurs
  réels, 1080p50) en MOV ProRes / x264 et MXF DNxHD / ProRes : 50 images neuves/s, retard stable,
  seek dans la partie écrite, fin 3 s après l'arrêt du recorder. Amont, NON contourné : un seek
  dans un MXF H.264 GOP long fait planter mxfdemux 1.26.2 (même par filesrc) → le recorder n'écrit
  pas de x264 en MXF.
  Corrigé en passant, dans l'élément ET le pilote : à un changement de tête demandé (`active-deck`),
  `eos` gardait la fin de l'ANCIENNE tête jusqu'au créneau suivant, et le fil de synchronisation
  (10 ms) la prenait pour celle du clip chargé — action de fin déclenchée à tort, clip sauté en
  playlist. Et la position avançait encore après la fin d'un clip ; elle reste sur la dernière image.
  Banc API natif repassé sur dl360-1 (6 fichiers, routage, pause, seek, pas, vitesse, boucle,
  playlist) : identique. **Image 0.22 sur dl360-1 seul.**

- **2026-10-11** — **player 0.29.0 + image 0.24 : clips Avid OP-Atom.** Le recorder écrit l'Avid
  « growing » par bmx (`-t avid --avid-gf`). Relevé de structure : pendant l'écriture, la KLV
  d'essence porte une longueur NULLE (BER 0x88 + 8 octets à 0) et la partition de corps est
  marquée « pied » ; Media Composer déduit la durée de la taille du fichier, mxfdemux et ffprobe
  n'y lisent rien. `bobigrowsrc` gagne un mode UNITÉS : saut d'en-tête, tampons de taille fixe
  (image DNxHD entière — CID constant — ou 20 ms de PCM), format temps, départ/arrêt en propriétés.
  Trois pièges : la longueur écrite VAUT « clos » (sinon le pied de fichier sortait comme une
  unité) ; le seek initial de GstBaseSrc écrasait le départ ; et un segment annoncé à 0 avec des
  tampons datés plus loin faisait combler l'écart par videorate — la tête repartait à 0. Côté
  pilote : tête maison (une source par piste), plus de ffprobe pour ces clips (8 s de délai, deux
  fois par chargement), erreurs des têtes en retrait ignorées. Mesuré : DIRECT à 3,4 s du direct,
  seek, fin exacte, A/V sur le bus +0,02 ms, banc natif de non-régression identique.
