# Scope — compte rendu de la session autonome du 2026-08-25/26

Journal daté, non maintenu au-delà. Il existe pour qu'on retrouve **pourquoi** chaque décision a
été prise, pas seulement ce qui a été fait.

## Ce qui a été livré

**Le plugin est passé de sept capacités déclarées-et-absentes à zéro.** Le garde-fou construit au
début de la session ne signale plus rien sur `scope`, et douze auto-contrôles rejouables
protègent les propriétés qui comptent.

### Socle (proposé en préambule, validé)

| | Ce que c'est | Où |
|---|---|---|
| **Garde-fou « déclaré et absent »** | confronte le manifeste de chaque plugin à ce que son code sert réellement | `app/plugin_audit.py`, panneau sur la page Recette |
| **Verdict de programme** | l'alarme de loudness est un jugement de PROGRAMME armé/coupé, pas une surveillance d'échantillons | `/programme`, relayé par le canal générique d'avis |
| **Auto-contrôles** | douze vérifications rejouables DANS le conteneur | `/autotest`, exposé génériquement sur la Recette |

### Fonctions

`/snapshot` (témoin horodaté avec plans bruts, persisté) · `entrelace_mode` (demi-trames par
défaut) · habillage du tracé (couleur, fond, opacité, épaisseur) · sélection de ligne avec repère
sur vignette · préréglages de disposition partagés · **sortie vidéo MXL** (six dispositions,
texte, étiquettes) · cibles couleur du vecteur-scope · **crête vraie dBTP et LRA**.

### Hors plugin

- `app/metrics.py` : un avis de plugin peut déclarer son **niveau** — un « programme conforme »
  ne doit pas partir en avertissement.
- `containers.image` : un conteneur mémorise l'image posée à son `docker run`, ce qui referme le
  trou de `requires.image_min`.
- `plugin_store` : première utilisation de la table générique créée en juillet, avec ses helpers.

## Ce qui a été mesuré

| Mesure | Résultat |
|---|---|
| Sérialisation `/scope` | 26,08 ms en JSON → **0,005 ms** en binaire sans copie |
| Charge utile | 768 Ko → **197 Ko** (rampe déplacée côté script) |
| Parade R/V/B | 29,20 ms en séquence → **9,30 ms** en trois fils (×3,14) |
| Rendu de la sortie vidéo | 22,77 ms → **7,4 ms**, cadence de mesure conservée à 50 fps |
| Filtre K (EBU Tech 3341 cas 1) | **−22,993 LUFS** pour −23,0 attendu |
| Crête d'échantillon vs vraie | l'échantillon sous-estime de **3,01 dB exactement** ; le ×4 rattrape à 0,13 dB |
| Cibles couleur | retrouvent **exactement** la table SMPTE en BT.601 |
| Phase : pouvoir discriminant | **0,77 ms** de dispersion sur un producteur asservi contre **5,99 ms** sur un récepteur logiciel |

## Décisions de conception, et leurs raisons

- **Le rendu vidéo dessine à partir des mêmes plans que la page.** Un opérateur devant un moniteur
  et un autre devant son écran doivent parler du même signal ; un rendu reconstruit à part aurait
  divergé au premier réglage.
- **Composition directe en Y/Cb/Cr**, jamais de RVB : la conversion coûterait 70 ms par trame et
  n'apporte rien, les tracés étant monochromes et la source déjà dans le bon espace.
- **L'épaisseur du tracé s'étend en NIVEAU, jamais en colonne** : l'axe horizontal d'un waveform
  est une position dans l'image, et étaler latéralement ferait mentir l'instrument sur la
  localisation du défaut.
- **Aucune matrice ni table de cibles codée en dur** : tout est dérivé de la colorimétrie déclarée,
  et le mode est REFUSÉ si elle ne l'est pas. Les tables qu'on trouve partout sont celles du
  BT.601 ; les poser sur du BT.709 fait corriger une dérive qui n'existe pas.
- **Le défaut d'entrelacé est passé à `demi_trames`** : sur le bus MXL un grain EST un champ, et
  l'aide du manifeste disait déjà que la trame tissée peigne au moindre mouvement.
- **Ce qui est partagé et ce qui ne l'est pas** : les réglages d'outils vivent dans le conteneur
  (partagés) ; la disposition de la grille vit dans le navigateur (confort de poste). Un
  préréglage nommé réunit les deux.
- **Le texte de la sortie est rafraîchi à 5 Hz** : des chiffres qui changent 50 fois par seconde
  sont illisibles. Le gain de lisibilité et le gain de coût allaient dans le même sens.
- **« Aucune mesure » n'est jamais « aucun écart »** : hors bornes de plausibilité, rien n'est
  publié ; un programme de moins de 10 s ne reçoit aucun verdict ; une ligne hors image est
  signalée et jamais mesurée ailleurs.

## Ce que la vérification a rattrapé

Trois fois, une correction écrite avec assurance ne faisait rien, ou faisait le contraire :

1. **Le sous-échantillonnage de la vignette** gardait une marge de 2× au-dessus de la cible, ce qui
   donne un pas de 1 dans la disposition la plus courante — il ne s'appliquait donc jamais. Le banc
   l'a montré, pas le raisonnement.
2. **La parallélisation** mesurée sur l'orchestrateur donnait ×1,2 et j'allais l'abandonner ; dans
   le conteneur, elle donne ×3,14. Un banc de parallélisme se prend sur la machine cible.
3. **Ma dérivation du filtre K** était fausse, et seule la table normalisée à 48 kHz pouvait le
   dire.

Et dans l'autre sens : j'ai cru à tort que seules deux cibles couleur sur six apparaissaient, en
jugeant sur un JPEG à demi-résolution. La mesure des pixels montrait les six.

## Ce qui reste ouvert

- **`mixer` et `split`** : le garde-fou signale des promesses non tenues — `tally_emit` (décrit dans
  le `help.md` du mixer comme un réglage du panneau ⚙) et `tally_level_base` ; `light_altitude`,
  `transition_curve` et `transition_stagger_ms` côté split. **Non touchés : ils attendent ton
  arbitrage**, ce sont d'autres chantiers.
- **Quatre instruments choisis, aucun construit** : corrélation de phase AUDIO, gamut RVB
  (à nommer descriptivement — le risque de marque sur « Diamond Display » n'a pas pu être levé
  hors ligne), fausse couleur et zébras, ANC/sous-titres OP-47. Le décodage OP-47 est fait dans
  `bobimxl` ; côté scope il manque l'entrée ANC et la tuile.
- **Outils d'ingénierie 2110** : la phase en mode PTP en expose une partie (premier paquet,
  latence RX, verdict 2110-21, état du GM). Restent les compteurs de paquets, le 2022-7 et une
  tuile dédiée. La matière est déjà là — cf. le relevé du moteur, `app/scope_2110.py`.
- **RÉSOLU dans la nuit du 26 au 27 : le `failed` de 100 % des récepteurs actifs est un
  ARTEFACT.** Ce n'était pas 6 sur 25 mais **6 sur 6** — les dix-neuf autres sont `idle`, donc
  non mesurés. Le `fpt` dérive de **6,85 ppm**, pente constante, R² = 0,9999 sur 474 s,
  **y compris sur notre propre émission relue par notre propre récepteur** : notre TX et notre
  RX partagent la même horloge, cette boucle devrait être plate. La dérive est donc INTERNE à
  la mesure du moteur, et les sources vont bien (`cinst_max` 1, `vrx_span` 2 — meilleur que le
  gabarit narrow). Détail et méthode : mémoire `fpt-derive-7ppm-interne-au-moteur`.
  ⚠ Il faut **DÉROULER** le `fpt` avant d'en tirer une pente : il est modulo la période de
  trame, et une régression brute rend des pentes de signe opposé. C'est ce qui m'a fait
  rapporter deux fois une fausse « variation d'un port à l'autre ».
  **Reste ouvert :** le mécanisme. 6,85 ppm est un ordre de grandeur d'oscillateur libre alors
  que `ENGINE_PHC2SYS=1` est actif ; la réponse est dans `st_rx_timing_parser.c` (libmtl),
  absent de l'hôte.
- Le mode `trame` divise la cadence de mesure par deux — c'est la nature de l'opération, mais il
  faut le dire à un client.
- `i18n/*.json` porte une clé à moi (`alert.deploy.compute.image_conteneur_ancienne`) restée NON
  COMMITÉE : ces fichiers appartiennent à la campagne i18n d'une autre session, et la clé partira
  avec leur commit. L'alerte fonctionne déjà, le catalogue étant chargé au boot.

## Banc laissé en place

- **`scope-angl` #1028** — scope sur `2110-io-dl360-1_1` (1080p50) et le générateur de tonalité,
  sortie vidéo active, image `bobi-compute:0.32`.
- **`mon-scope-angl` #1026** — aperçu WebRTC dédié.

Tu m'avais dit de les garder comme banc. Ils consomment deux conteneurs et un encodeur.
