# Pyramide de proxies

La **pyramide** pré-calcule des versions réduites (« proxies ») de chaque source vidéo, pour que
les consommateurs (multiviewers, monitoring…) lisent une image **déjà à la bonne taille** au lieu de
redimensionner la source pleine à chaque trame. Le redimensionnement est ainsi fait **une seule
fois, mutualisé**, et **hors de la boucle temps réel** du multiview.

> Bénéfice typique : sur une mosaïque de tuiles réduites, le temps de traitement du multiview chute
> fortement (≈ −65 à −70 % sur la lecture+redimensionnement des entrées) — il tient le 1080p50 là où
> il décrochait.

## Deux familles de proxies

1. **Octaves** (socle générique, toujours là) : ½, ¼, ⅛, 1/16 de la source. Filet immédiat utilisable
   par n'importe quel consommateur, sans configuration.
2. **Tailles sur-mesure** (à la demande) : la taille **exacte** dont une tuile a besoin (ex. une
   tuile de quad fait ~`952×536`, jamais pile un octave à cause des bordures/nom/tally). Lue en
   **copie pure** (zéro redimensionnement). Générées automatiquement quand un besoin se répète.

Pour une source dont le shm s'appelle `S` : octaves `S__p2 / S__p4 / S__p8 / S__p16`, sur-mesure
`S__s<largeur>x<hauteur>` (ex. `S__s952x536`). Le format (chroma, profondeur) est conservé ; seules
les dimensions changent.

## Auto-câblage (piloté par la demande)

Tu n'as **pas besoin de câbler la pyramide à la main**. Dès qu'un consommateur (multiview, monitoring)
référence une source, l'orchestrateur **assigne automatiquement** cette source à un slot libre de
pyramide du nœud (à chaud, sans coupure) et **libère** le slot quand plus personne ne la lit. Le format
de la source est résolu via la base (qui sait quel conteneur produit quoi) et **poussé** à la pyramide —
elle n'a rien à deviner. Si toutes les sources demandées dépassent le **nombre de slots** (`n_inputs`),
une alerte invite à augmenter `n_inputs` ou à ajouter une pyramide.

## Cœurs dynamiques

Les **cœurs CPU** de la pyramide sont dimensionnés **à la charge** : `cœurs = plancher + ` **`cores_per_1080p_input`** ` × Σ(résolution_source / 1080p)`, clampé au pool de cœurs du nœud. Une source 4K
pèse ~4× une 1080p. Le plancher est `resources.cores` (6). Comme le cpuset d'un conteneur Docker n'est
**pas modifiable à chaud**, les cœurs sont fixés au **(re)déploiement** ; si la charge dérive ensuite
(sources câblées après coup), l'orchestrateur **recrée automatiquement** la pyramide *si elle n'a aucun
consommateur*, sinon il émet une **alerte** (jamais de coupure subie). Le **cap réactif** déleste des
proxies à chaud si un worker frôle son budget de trame (latence).

## Réglages

- **Cœurs par source 1080p** (`cores_per_1080p_input`, défaut 0,5) : ratio de dimensionnement
  dynamique des cœurs (voir ci-dessus). `0` = cœurs fixes (plancher seul). Petit = nœud léger ;
  grand = nœud dédié multiview.
- **Socle d'octaves** (`base_octaves`) : `Complet` (½ ¼ ⅛ 1/16, défaut), `Minimal` (½ seul) ou
  `Aucun` (pur à la demande, zéro coût de base). Réduire le socle économise du CPU si tu comptes sur
  les tailles sur-mesure.
- **Seuil sur-mesure** (`custom_size_threshold`, défaut 2) : **seuil SOUPLE**. Toutes les tailles
  demandées sont désormais servies **jusqu'au cap (6 par source)** — y compris une taille réclamée
  par une seule tuile, car la lire depuis un octave/le plein en ratio non-entier coûte un *gather*
  (exactement ce que la pyramide doit éliminer). Ce seuil ne **départage le surplus** que s'il y a
  **plus de tailles distinctes que le cap** : on ne garde alors que les tailles demandées ≥ seuil,
  puis les plus demandées. En pratique (≤ 6 tailles/source) il n'écarte plus rien.
- **Nombre de sources** (`n_inputs`) : nombre d'entrées câblables.
- **Ring de sortie** (`ring`, défaut 4, 2 à 8) : profondeur du ring buffer MXL des flux de proxies
  produits. Un ring plus profond tolère un consommateur momentanément en retard sans perdre
  d'image, au prix d'un peu plus de RAM par proxy ; la valeur par défaut convient à l'usage normal
  (consommateurs sur le même nœud ou en LAN direct).

## Comportement temps réel

- **Un thread par source**, verrouillé à l'entrée **1:1** : il attend une nouvelle image puis émet
  les proxies en **propageant la `frame_index`** de la source. Aucune grille PTP maître, aucun
  re-cadençage → ni perte ni duplication d'images.
- Sources de cadences différentes → avancent **indépendamment**.
- Couper une source → ses proxies **se figent** (le consommateur garde la dernière image), reprise
  immédiate au retour.
- Les tailles sur-mesure sont appliquées **à chaud** (sans coupure) : ajouter/retirer une taille ne
  redémarre pas la pyramide et ne perturbe pas les proxies déjà produits.

## Mode tranche (latence réduite)

La pyramide peut suivre chaque source **au fil de l'arrivée de ses bandes** et publier ses proxies
**bande par bande**, au lieu d'attendre l'image complète (mode tranche MXL) : l'étage pyramide
n'ajoute alors plus ~1 image de latence — un multiview en mode tranche démarre sa composition dès
la première bande d'un proxy.

- **Opt-in** : paramètre `slice_mode` au déploiement (désactivé par défaut — comportement
  historique strictement inchangé). C'est aussi le mode utilisé par le tissu de composition quand
  son option tranche est activée.
- Sources progressives uniquement ; en entrelacé, le chemin classique s'applique.
- Les proxies produits sont identiques au mode classique (mêmes images) ; une source en retard est
  simplement republiée depuis sa dernière image complète, sans jamais bloquer les autres.

## Journal (diagnostic)

`log_level` (défaut « Événements ») : les événements marquants (source qui apparaît/disparaît,
recalcul des tailles sur-mesure, repli sur un chemin dégradé) sont toujours écrits ; passer en
« Détaillé » ajoute un journal par trame — à n'activer que le temps d'un diagnostic, le journal
Docker n'est pas roté. Réglable à chaud (`/log_level`, sans redéploiement) ou en persistant
(config_schema, survit au redéploiement).

## Mise en service

1. **Créer** un conteneur *Pyramide* sur un nœud (Réglages → Déploiement → Nœuds — c'est un type
   Docker « compute »).
2. Sur la page **Câbles**, câbler les **mêmes sources** que celles affichées par tes consommateurs.
   ⚠️ On **ne câble pas** le multiview *vers* la pyramide : la pyramide et le multiview consomment
   tous deux la **même source** (ex. `rx1`). La pyramide produit alors `rx1__…`, que le multiview
   utilise automatiquement à la place de `rx1`.
3. **(Re)déployer le multiview** : l'utilisation des proxies est injectée **au déploiement** du
   multiview. Si tu câbles la pyramide *après*, re-déploie/re-sauve le multiview.
   - Les **octaves** sont utilisés dès ce (re)déploiement.
   - Une **taille sur-mesure** exacte apparaît après le recalcul automatique (~quelques secondes) ;
     re-déploie le multiview une 2ᵉ fois pour qu'il la consomme en copie pure.

C'est **opportuniste** : sans pyramide (ou proxy absent), le consommateur lit la source pleine comme
avant — rien ne casse, chaque tuile décide seule.

## Console de monitoring

La page du plugin (Traitements → Pyramide, sélectionner une instance) affiche, **par source** :

- la liste des proxies produits (taille, octave/sur-mesure), leur **nombre de consommateurs** et les
  **orphelins** (produits mais lus par personne) ;
- les **besoins non couverts** (tailles demandées sans proxy exact → tuiles en redimensionnement) ;
- les **KPIs** : répartition des tuiles par classe — *copie pure* / *strided* (réduction entière,
  rapide) / *gather* (réduction non entière) / *plein* ;
- un bouton **« Optimiser »** qui force le recalcul des tailles sur-mesure ;
- l'icône **📺** pour prévisualiser un proxy.

Sur le **multiview**, la case **« Proxies pyramide (ingénierie) »** (réglages globaux du composer)
affiche un badge par vignette indiquant le proxy lu et son coût : `¼ ~`, `952×536 ✓` (copie pure),
`plein ↯` (lecture de la source pleine).

Le **tableau de bord** (accueil) montre un encart *Proxies* (répartition + gaspillage), et des
**alertes** sont levées automatiquement en cas de proxies orphelins ou de besoins répétés non
couverts (« lancer Optimiser »).
