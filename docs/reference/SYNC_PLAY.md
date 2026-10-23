# Lecture synchrone

*Fait foi pour `app/sync_groups.py`, le mode synchrone du player (`/sync/*`, player ≥ 0.41.3) et les
commandes datées de l'élément `bobiplayout` (0.3.2, image `bobi-media` ≥ 0.28). Décidé et mesuré le
2026-10-17. Gardes : `tests/verif_sync_groups.py`, `tests/verif_banc_sync_play.py` (hors ligne, mutés) ; banc vivant
`tools/bancs/banc_sync_play.py` (lit le bus MXL).*

## Le modèle

Des players **ordinaires** (un conteneur = un canal) qui lisent **ensemble** : même image au même instant
sur tous les canaux, à l'image près, audio compris. Objet **distinct** des groupes A/B
(`PLAYOUT_GROUPS.md`) : aucun canal ne prend l'antenne à la place d'un autre. Un player ne sert qu'un groupe
synchrone, et jamais en même temps un groupe A/B.

Playlist en **lignes** : une ligne = un clip par canal (cellule vide = noir), lancés ensemble. Identité des
membres : `instance_uuid`. Table `sync_groups` (créée par `init_db`).

## Le mécanisme : une ancre datée commune

Chaque player sort une image par créneau de la grille TAI de son nœud. L'image écrite au créneau `G` est
choisie par l'horloge de présentation `media_time(G) = m0 + v·(G − g0)`. Si N players ont la **même ancre**
`(g0, m0, v)`, ils écrivent la même image au même créneau — par construction.

Toute commande est une **ancre datée** : « au créneau de l'instant TAI `at`, mets cette tête à l'antenne,
avec ce temps média et cette vitesse » (`schedule` de l'élément ; vitesse 0 = image figée). L'orchestrateur
choisit `at` dans le futur (`lead_ms`, 400 ms par défaut, réglable 150–5000 ms dans ⚙) et sur un créneau, et
envoie la **même** commande à tous. Play, Pause (vitesse 0 au temps atteint), Cue (1re image figée),
recherche (nouvelle tête préparée puis ancre datée), pas à pas (même tête jusqu'à 8 images, sinon recherche),
vitesse 0,25× à 4×, boucle, changement de ligne : tout passe par là.

**Une commande appliquée en retard rattrape sur la grille, elle ne glisse pas** : l'élément pose l'ancre sur
le créneau DEMANDÉ, pas sur celui où il l'applique. Les images manquantes sont tenues (image précédente
répétée) puis le décodeur rattrape. Le témoin dit de combien de créneaux elle était en retard.

**Changement de ligne pré-programmé** : pendant qu'une ligne joue, les têtes de la suivante sont ouvertes
dans chaque player (`/sync/prepare` : décodeur ouvert, file pleine, rien à l'antenne) et la bascule datée y
est envoyée dès que tout est prêt — elle ne dépend pas de la latence de l'orchestrateur à l'instant de fin.
Une ligne pas prête à temps part dès qu'elle l'est, sur tous les canaux ensemble, et c'est signalé.

## Fin de ligne

Réglage du groupe `end_policy` : `longest` (défaut — les clips plus courts tiennent leur dernière image),
`shortest` (les autres sont coupés), `duration` (durée propre de chaque ligne ; à défaut, le plus long).
Durée inconnue (flux, sonde en échec) : fin **constatée** (tous les canaux finis au témoin), bascule à
`maintenant + lead`. `loop_mode` : `off` | `line` | `playlist`.

## L'horloge : ce qui est mesuré

| Point | Constat (2026-10-17) |
|---|---|
| Horloge de la grille du player | `CLOCK_TAI` (libmxl, `Clock::TAI`), lue par l'élément. Jamais `CLOCK_REALTIME` : elle vaut TAI sur dl360-1 et UTC ailleurs. Le player date ses réponses `/sync` avec `CLOCK_TAI` relevé à l'ENTRÉE de la requête. |
| dl360-1 | `CLOCK_TAI` à **−201 s** (18:10) puis **−236,3 s** (18:46) des autres nœuds : elle **dérive** (~1 s/min), ce n'est pas un décalage stable de 37 s. Un départ commun avec un autre nœud y est **impossible** : le player refuse l'instant (409 `clock`, « −216 s »), l'orchestrateur le signale. |
| dell-1 / r620-2 | chrony + `tai_offset = 37` : `CLOCK_TAI` juste, à 0,6 ms l'un de l'autre (± 2 ms, relevé de l'orchestrateur) ; 2,1 ms ± 2,7 selon `app/clocks.py`. |

### Tolérance ZÉRO (décision de l'utilisateur, 2026-10-17)

« Une image c'est déjà trop » : une paire **fill / key** doit être exactement ensemble. Tout écart d'au moins
une image entre canaux est un défaut (alerte + canal signalé « décalé » + état de macro `in_sync` faux). Deux
cas distincts :

- **(a) décalage qui RESTE** (`alert.sync.offset`, constaté sur deux relevés de suite) : écart de retard
  d'image entre canaux au témoin (entiers exacts, déclarés par l'élément), ou **ancre différente** — temps
  média différent sur la même image de grille, même sans retard déclaré (commande refusée ou perdue sur un
  canal) ;
- **(b) désynchro PASSAGÈRE** (`alert.sync.lag`, avec le nombre d'images) : un canal a répété des images
  (décodeur en retard) puis rattrapé — un fill qui répète pendant que le key avance. Une seule image suffit ;
  le compte s'accumule entre deux alertes (au plus une toutes les 10 s par canal), il n'est jamais perdu.

L'**horloge** des nœuds est le troisième terme (`alert.sync.skew`) : relevée par l'orchestrateur à chaque lecture
du témoin, au milieu de l'aller-retour, au plus court aller-retour sur les 12 derniers (deux canaux d'un même
nœud partagent son horloge : écart nul par construction). Défaut si l'écart d'horloge, **diminué de son
incertitude** (± RTT/2, quelques ms), atteint une image. Cette incertitude ne s'applique QU'à l'horloge :
elle ne masque jamais un écart d'image (garde `unc_masks_frames` du banc). Autres défauts signalés : commande
refusée ou jamais constatée (`alert.sync.not_applied`), appliquée en retard (`alert.sync.start_late`), canal
repris en main (`alert.sync.left`), ligne pas prête à temps (`alert.sync.next_late`). Une alerte par épisode.

Le banc vivant applique la même règle : `banc_sync_play.py` sort en erreur (code 1) au premier écart d'une
image, à la première image répétée sur un seul flux, à une rupture décalée d'un index ou à un échantillon
audio d'écart (`verdict`, gardé hors ligne et muté par `tests/verif_banc_sync_play.py`).

### Paires fill / key (décision du 2026-10-17)

Deux canaux d'un groupe peuvent être marqués **paire** (⚙ → Paires fill / key). Une paire exige, et chaque
refus est expliqué (alerte `alert.sync.refused`, bandeau, code `section.syncplay.err.pair_*`) :

| Contrôle | Quand | Refus |
|---|---|---|
| même NŒUD (horloge commune par construction) | à l'enregistrement, et à chaque départ | `pair_nodes` |
| même FORMAT de sortie (dimensions, chroma, profondeur, cadence, balayage — lu au `/sync` du player) | à chaque départ | `pair_format` / `pair_unverified` |
| sur la ligne : les deux cellules remplies ensemble, mêmes points In, même durée de clip (à ½ image près) | avant d'ouvrir / après préparation | `pair_cell`, `pair_in`, `pair_duration` |
| un canal de la paire ne s'ouvre pas | à chaque départ — refus IMPOSÉ, quel que soit `open_fail` | `pair_open` |
| les deux files PLEINES (≈ 12 images en HD, `queue_max` du player) | attendu jusqu'à 8 s | `pair_not_full` |
| décodage libre mesuré pendant le remplissage ≥ 1,1 × cadence (× vitesse) | à chaque départ | `pair_cadence` |

**En lecture**, aucune répétition n'est tolérée : une image répétée sur un des deux canaux, ou une cadence
d'images neuves (`fresh_fps`) sous celle du flux pendant deux relevés, met la paire **en défaut**
(`alert.sync.pair`, cartes liées marquées, macros `pairs_ok` / `pair_issue` / `in_sync`). **La lecture n'est
pas coupée** (couper l'antenne d'office serait pire), mais la **ligne suivante repasse le contrôle de départ** :
refusée, sa bascule n'est pas datée, la ligne en cours va au bout et TOUS les canaux tiennent leur dernière
image, ensemble (`blocked`, macro `blocked`). Corriger la ligne relance la préparation.

Mesuré le 2026-10-17 (dl360-1, 720p25) : paire sur deux H.264 720p identiques — files pleines au premier
relevé, 0 image d'écart et 0 répétition sur 254 grains ; key en ProRes 4444 UHD (10 s) — accepté au départ à
28,7 i/s, joué à 25 images neuves/s sans répétition, puis la même ligne en boucle **refusée** (20,0 i/s mesurés
pendant que la première tête jouait : sous contention, le décodage ne tient plus la marge) ; ligne aux In
différents : refusée avant toute ouverture.

**Constat « ligne déjà jouée sans défaut »** (validé le 2026-10-17). La mesure au remplissage est SOUS-ESTIMÉE
par la contention (la tête suivante se prépare pendant que la courante joue). Une ligne jouée EN ENTIER par la
paire — depuis son début, à une même vitesse, sans recherche ni pas, sans aucune image répétée ni cadence
d'images neuves en baisse — reçoit un constat (`lines[].proven`, par paire, avec l'empreinte fichiers / In /
Out / vitesse) ; il fait foi : la mesure au remplissage ne refuse plus la ligne, en boucle comme en relecture.
Il tombe à toute modification de la ligne (empreinte) et au premier défaut de paire sur cette ligne. Pour une
boucle, la lecture en cours vaut constat d'avance (« provisoire ») : un défaut avant la fin annule la bascule
datée. Le contrôle en lecture reste entier. Vue : « ✓ A+B » dans la playlist et sur les cartes ; macro
`line_proven`.

Mesuré le 2026-10-17 à 23 h (dl360-1, key ProRes 4444 UHD en boucle) : premier départ accepté au 1er essai sur
deux (mesure 25,5 puis 26,5 i/s refusées, puis acceptée) ; ensuite chaque tête de boucle mesurée à 19,0–21,3 i/s,
acceptée par le constat ; 25 images neuves/s tenues, 0 image répétée, 554 grains à 0 image d'écart et changements
de boucle au même index sur les deux canaux. Horloge de dl360-1 pendant l'essai : pas de saut (pas maximal 16 ms
par 0,5 s), mais une dérive continue d'environ 1,6 % (−491,7 → −492,4 s en 45 s) — sans effet entre deux canaux
du même nœud.

### Canal qui ne s'ouvre pas (réglage du groupe `open_fail`, ⚙)

`proceed` (**défaut**) : les autres canaux partent ensemble, le canal fermé garde sa dernière image (ou du noir)
et c'est signalé — la continuité de l'antenne passe d'abord : refuser fige TOUS les canaux pour un seul fichier
manquant. `refuse` : aucun canal ne part (à la commande : refus affiché ; à un changement de ligne
automatique : la ligne en cours tient sa fin sur tous les canaux) — pour un mur ou une composition qu'un canal
manquant rend faux. Une PAIRE refuse toujours.

### Fill & Key : ce qui peut encore désynchroniser une paire

| Cause | Couvert par |
|---|---|
| préchargement (tête « prête » dès sa 1re image) | départ sur files pleines |
| décodage plus lent que la cadence | mesure au remplissage (refus) + `fresh_fps` en lecture (défaut) |
| commande reçue en retard par un canal | rattrapage sur la grille, mais images répétées → défaut de paire |
| nœuds différents | interdit pour une paire |
| durées / In différents | refus de la ligne |
| contention CPU en lecture (préparation de la ligne suivante, autre conteneur) | non prévenu — signalé par `fresh_fps` / répétitions |

## Mesures (2026-10-17, 720p25, clips `SyncTest/sync_test_*.mov`)

Banc `tools/bancs/banc_sync_play.py` : n° d'image codé en binaire dans les 40 premières lignes du clip, salve
audio de 10 ms au début de chaque seconde ; lit les grains vidéo et audio sur le bus MXL, au même index.

| Essai | Résultat |
|---|---|
| Même nœud (dl360-1), départ commun | 379 grains : **0 image** d'écart ; audio **0 échantillon** ; A/V +0,04 ms (seuil de détection) |
| Pause, reprise, 2×, 0,25× | 0 image d'écart sur chaque essai (audio muet hors 1× : limite connue de l'élément) |
| Commande reçue 1,56 s en retard par un canal | appliquée avec 39 créneaux de retard (témoin), 3 images répétées, puis **0 image** d'écart |
| Changement de ligne, durées 4 s / 3 s (fin au plus long) | les deux flux changent de clip au **même index de grille** ; le canal court tient sa dernière image |
| Deux nœuds (dell-1 + r620-2) | 284 grains : **0 image** d'écart sur la grille, audio 0 échantillon ; horloges à 0,6 ms ± 2 → écart physique < 3 ms |
| dl360-1 + dell-1 | refus explicite du canal dl360-1 (horloge à −216 s), alerte |

## Player (mode synchrone)

`POST /sync/enter` · `/sync/prepare {key, file|asset, in, out}` (202, non bloquant) · `/sync/at {id, at, key?,
m0, speed, black?}` (409 `clock` si `at` est à plus de 60 s devant ou 5 s derrière l'horloge du nœud, 409
`not_prepared`) · `/sync/cancel {id}` · `/sync/drop {keep}` · `/sync/leave` · `GET /sync` (témoin).
En mode synchrone le player ne décide plus rien (ni armement de la suite, ni action de fin). Tout geste de
transport ordinaire le fait **sortir** du mode (reprise en main, signalée) ; sa console l'annonce.

## Macros

Fournisseur du cœur `sync_group` : `play`, `pause`, `stop`, `cue`, `goto`, `next`, `prev`, `seek` (timecode),
`step`, `speed`, `loop`, `end_policy`, `load_playlist` ; états par groupe `state`, `line`, `line_title`,
`time`, `skew_ms`, `faulty`, `in_sync`, `speed`, `loop`, `ended`.

## Limites connues

- Audio muet hors vitesse 1 (élément : `scaletempo` casserait le calage par temps média).
- Le redémarrage de l'orchestrateur ne reprend la lecture que si tous les témoins confirment l'hypothèse
  persistée (même ancre, tête présente) ; sinon elle est considérée arrêtée (les players tiennent leur image).
- Le banc deux nœuds compare la GRILLE ; l'écart physique y ajoute l'écart des horloges — aucun flux ne peut
  le révéler seul (la grille d'un nœud EST son horloge).
