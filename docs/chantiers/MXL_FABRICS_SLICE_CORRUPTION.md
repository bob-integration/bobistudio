# Réplication fabrics d'un flux TRANCHÉ : grains corrompus + ~12 images de retard

**Statut : reproduit, isolé, non corrigé. À remonter au projet MXL (bêta).**
Mesuré le 2026-08-11 sur MXL `v1.1.0-beta-1` (`mxl-info` : `1.1.0-beta-1+0 g81738a15adb5`).

---

## Le symptôme

Un flux vidéo publié **en tranches** (commit progressif, `validSlices` croissant) ne se réplique
pas correctement par `mxl-fabrics-demo`. Sa réplique porte :

- des grains **corrompus** — un même grain contient des bandes venant de **trames différentes** ;
- environ **12 images de retard** de contenu.

Un flux **monolithique**, sur le même lien, ne présente ni l'un ni l'autre.

## La preuve

Mire avec **timecode incrusté** et barre verticale mobile. Lecture d'un grain à `tête − 8` —
donc achevé depuis 8 trames, il **ne peut pas** être en cours d'écriture :

| | image | retard réel du contenu |
|---|---|---|
| source (nœud producteur) | **propre** | **1,8 image** |
| réplique (nœud distant) | **déchirée** : un tiers supérieur portant une trame de flash, collé sur une autre trame | **11,7 images** |

L'hypothèse « artefact de lecture d'un grain partiel » est donc **écartée** : à cette profondeur
le grain est complet, et il contient malgré tout deux trames.

Méthode : âge ABSOLU du contenu — heure TAI lue une fois, moins le timecode affiché ; contrôle
intégré (la source doit ressortir à ~2 images, ce qu'elle fait).

## A/B, une seule variable

Même lien, même tout, seul le `slice_mode` du producteur amont change :

    source TRANCHÉE ....... 11,75 images, image DÉCHIRÉE
    source monolithique ...  1,75 image,  image propre

## Ce qui a été éliminé par mesure

| piste | résultat |
|---|---|
| `slice_height` de la RÉPLIQUE (créée tranchée ou monolithique) | 1,85 vs 1,80 — **aucun effet** |
| `maxSyncBatchSizeHint` 2 → 30 (15 lots/trame → 1) | 12,1 images — **aucun effet** |
| âge du lien (recréé 100 s plus tôt) | 11,75 — **pas une dérive** |
| décrochage d'index de l'initiateur | tête de la réplique **sur la grille** (−1) — il ne décroche pas |
| profondeur de l'anneau TX, mode tranche du moteur/mur | sans rapport |

## L'initiateur EST HORS DE CAUSE — instrumenté, pas déduit

Build de diagnostic (mêmes sources, `SPDLOG_ACTIVE_LEVEL=SPDLOG_LEVEL_DEBUG` + `spdlog::set_level`
ajouté dans `main()`) : les `MXL_DEBUG` de `runDiscrete` deviennent visibles.

Ce que le journal montre, sur un flux tranché en défaut :

    20:11:13.771  grain …688  slices 20-22
    20:11:13.773  grain …688  slices 28-30      les 15 lots d'un grain en 3 ms
    20:11:13.790  grain …689  slices 0-2        puis 17 ms d'attente du grain suivant
    20:11:13.793  grain …689  slices 28-30

L'initiateur suit le producteur à sa cadence, sans peiner. Un second build ajoutant un
rattrapage borné sur `headIndex` a servi de SONDE : **zéro rattrapage déclenché sur 48 050 lignes
de journal**, donc l'initiateur n'est jamais plus de 2 grains derrière la tête. Le patch de
rattrapage a été retiré : il ne corrigeait rien.

⚠ Une mesure EXTERNE donnait « −10 grains » de façon stable et reproductible. C'était un
artefact : lire le journal puis la tête dans deux appels successifs met ~0,3 s entre les deux,
soit 15 grains. **Toute comparaison d'index entre deux processus doit être faite DANS le même
processus**, ou par une sonde interne comme celle-ci.

→ La corruption est donc EN AVAL de l'initiateur : côté cible, ou dans la sémantique du transfert
par tranches. C'est là qu'il faut chercher.

## Piste non vérifiée

`RCInitiator::transferGrain` transfère de la case locale `grainIndex % N` vers la case distante de
même rang, **sans retenir le grain source**. Le RDMA lit la mémoire de façon asynchrone : rien
n'empêche le producteur de réécrire la case pendant que le transfert est en vol. En image entière
la fenêtre est de l'ordre de la milliseconde ; en tranche, l'initiateur ouvre un transfert par lot
et la fenêtre s'étale sur toute la période de trame. **Hypothèse, non démontrée.**

## Contexte utile

`tools/mxl-fabrics-demo/demo.cpp:runDiscrete()` avance `grainIndex` de 1 par grain et ne se
recale (`mxlGetCurrentIndex`) que sur `MXL_ERR_OUT_OF_RANGE_TOO_LATE` — le chemin audio
(`runContinuous`) a, lui, un recalage explicite sur `headIndex`. Nous avions patché ce point
(image 0.18.0) **puis retiré le patch** (0.19.0) : l'amorçage sur `mxlGetCurrentIndex()` est
conforme au modèle de temps MXL (`docs/Timing.md`), c'étaient nos producteurs à index libre qui ne
l'étaient pas. Ils sont depuis tous sur la grille TAI, et **le décrochage d'index n'est pas la
cause ici** (mesuré ci-dessus).

## Ce qu'on a fait en attendant

Aucun contournement de comportement — seulement une **alerte** à l'établissement de tout lien
répliquant un flux tranché (`services/rdma`). Sans elle, le réglage global de tranche s'active d'un
clic et un producteur tranché dont le consommateur est sur un autre nœud paie douze images **et des
images fausses**, sans qu'aucun compteur ne bronche : cadence nominale, lien `running`, aucune
erreur journalisée.

## Pour aller plus loin

`MXL_DEBUG("Transferred grain index={} slices {}-{}")` existe déjà dans `runDiscrete`, mais c'est
un `SPDLOG_DEBUG` compilé hors du binaire en Release, et `Logging.cpp` ne règle aucun niveau à
l'exécution. Un build de diagnostic demande donc **deux** changements : `SPDLOG_ACTIVE_LEVEL` à la
compilation **et** un `spdlog::set_level` à l'exécution. C'est la prochaine étape si le projet ne
reproduit pas de son côté.
