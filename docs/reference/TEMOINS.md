# Quel témoin croire, et pour quelle question

**Ce document fait foi.** Il ne décrit pas un composant : il dit **à quel instrument se fier**
selon la question posée, et **lesquels mentent** — avec la raison, pour qu'on puisse juger si
elle vaut encore.

Il est né d'une campagne d'optimisation du multiview (7-9 août 2026) où **cinq fausses pistes sur
six venaient d'un chiffre dont le contrat n'était pas écrit là où on le lit**. Aucune ne venait
d'un raisonnement fautif : toutes venaient d'une mesure qu'on croyait comprendre.

---

## La règle générale

> **Un chiffre sans contrat n'est pas une mesure, c'est une opinion.**
> Avant de conclure sur une métrique, chercher ce qu'elle mesure *exactement*, ce qu'elle
> **n'inclut pas**, et **quand** sa définition a été écrite.

Trois corollaires, chacun payé cher :

1. **Une moyenne ne dit rien d'une cadence.** Sur une grille de 20 ms, c'est la QUEUE de la
   distribution qui perd les trames.
2. **Un compteur d'ÉVÉNEMENTS bat une fenêtre glissante.** `repeats`, `late`, `frames_missed`
   sont des faits ; `fps`, `own_latency_ms` sont des estimations sur une fenêtre.
3. **Une mesure datée n'est pas une vérité.** Un « X coûte Y, ne pas optimiser » reste vrai tant
   que son CONTEXTE tient. Écrire la date et la condition d'invalidation, ou ne rien écrire.

---

## Par question

### « Le fil porte-t-il la cadence nominale ? »

**Témoin : un récepteur TIERS.** En pratique l'EVS Neuron
(`http://192.0.2.221:5055/neuron.json`, cf. `docs/reference/` et la note de mémoire associée) :
`Video Format Status`, `RTP Sequence Error Count`, `Data Rate`, `Average Skew`.

**Pourquoi pas nous** : nos ports média sont en `vfio-pci` pour DPDK, donc **sans netdev** — ni
capture, ni mDNS, ni compteur par flux. Et `nic.mtl_stats` est agrégé au device (valeurs
identiques sur les deux ports) : inutilisable pour attribuer un débit à un TX donné.

⛔ **NE PAS utiliser `fps` du sender** — voir ci-dessous.

### « Le TX émet-il du contenu NEUF, ou rejoue-t-il ? »

**Témoin : `repeats` et `late`**, des compteurs cumulés. Un `fps` bas ne prouve rien.

⚠ Ni le débit ni le format ne distinguent une trame de tenue d'une trame fraîche : elles partent
**identiques** sur le fil. Le seul juge final est l'œil sur le retour.

### « Un mur perd-il des trames ? »

**Témoin : `frames_missed` / `frames_missed_per_s`** — les slots de grille genlock réellement
sautés. Pas `fps` (fenêtre glissante, « chute » de 20-25 % purement artefactuelle), pas
`own_latency_ms` (moyenne).

### « Où part le temps dans un mur ? »

**Deux questions, deux instruments** :

| question | métrique | quand |
|---|---|---|
| qu'est-ce qui fait DÉBORDER une trame ? | `piles_pic` (profil des trames lentes) | quand on perd des trames |
| où part le temps EN RÉGIME ? | `piles_tout` (profil de toutes les trames) | quand on cherche de la MARGE |

Le premier est aveugle à un poste qui coûte cher à **toutes** les trames sans en faire déborder
aucune — l'étiquette SILENCE des VU-mètres pesait ainsi **19,6 %** du temps sans jamais apparaître
dans `piles_pic`. Et à faible taux de perte il ne récolte plus assez d'échantillons : deux fenêtres
consécutives ont donné le même appel à 5 % puis à 32 %.

### « La réplication RDMA délivre-t-elle le flux ? »

**Témoin : le CONTENU** — empreintes des grains de l'anneau comparées sur les deux nœuds.

⛔ **PAS l'index de grain** : la cible remplit la grille TAI, donc l'index avance à la cadence
nominale **même sans contenu**. Un écart d'index nul ne prouve rien.

---

## Les faux témoins connus

| ce qu'on lit | ce que c'est vraiment | pourquoi |
|---|---|---|
| `fps` d'un sender 2110 | trames **neuves prises par le worker** | le rejeu n'est pas comptabilisable (libmtl n'offre aucun signal fiable) ; sous-estime le fil quand la source est déficitaire — `mtl_rx.c:write_stats` |
| index de tête d'un flux répliqué | **la grille TAI**, pas le contenu | la cible RDMA remplit la grille |
| `own_latency_ms` | une **moyenne** | ce sont les pics au-delà du budget qui perdent les trames |
| `fps` d'un mur | fenêtre glissante | « chutes » de 20-25 % sans aucune trame perdue ; croiser avec `frames_missed` |
| mtimes de `grains/` | **rien** | un writer MXL écrit en mémoire mappée, sans appel système — ni en local, ni sous RDMA |
| `nic.mtl_stats` par port | agrégé au **device** | mêmes valeurs sur les deux ports |

---

## Les métriques dérivées sont des bombes à retardement

Une métrique **calculée** à partir d'une formule devient fausse dès qu'on change ce qu'elle
suppose, et **rien ne le signale**. Vécu : `ov_tiles.lancements_gpu` valait `tuiles × 3` ; le
groupement des blends par couches l'a rendue fausse d'un facteur 10 (21 annoncés, 2 réels), sous
un commentaire qui promettait pourtant « on publie ce qui se passe, pas une formule ».

> **Compter, pas déduire.** Et quand deux métriques doivent concorder, le dire dans le code :
> `lancements_gpu` doit égaler `blend_couches` tant que `blend_lot_miss` vaut 0 — un désaccord
> devient alors visible au lieu d'être silencieux.

---

## Deux prédicats pour la même notion = un bug qui vous attend

Le 8 août, `reconcilier_cables` jugeait « ce flux est-il consommé ? » sur le CÂBLE, et
`purger_liens_sans_consommateur` sur `consumes[].shm`. Le second ne résolvait pas les
`state_field`, donc aucun émetteur 2110 n'était jamais compté. Les deux se sont battus toutes les
deux minutes pendant des heures — création, échec, purge, recréation — et une sortie 2110 de
production est restée sans image, sans que rien ne le signale.

Le commentaire accompagnant la purge affirmait : « les deux prédicats sont le même, donc ils ne
peuvent pas se contredire ».

> **Une même notion, une seule fonction.** Si deux endroits doivent répondre à la même question,
> l'un appelle l'autre — la contradiction devient impossible au lieu d'être improbable.

---

## Voir aussi

- `docs/reference/PROBE_2110.md` — l'analyseur de flux (conformité 2110-21)
- `docs/chantiers/MULTIVIEW_CADENCE_2026-08-08.md` — le journal daté de la campagne
