# Console audio — VCA (groupes de commande) : étude de conception

Date : 2026-10-16. Statut : ÉTUDE, aucun code. Plugin `audio_console` 0.4.31 au moment de l'écriture.
Références : `docs/chantiers/CONSOLE_AUDIO.md` (§ 5 plan C, § 10 zones, § 12 interface),
`plugins/audio_console/src/console_model.py` (docstring = modèle), `src/engine.py` (`Gain`, `_chain`,
`_Ctl`), `app/plugin_zones.py`, `app/console_layouts.py`, `plugin.json:param_tree`.

## 0. Résumé des recommandations

| Sujet | Recommandation |
|---|---|
| Nature | un VCA n'a ni audio, ni chaîne, ni tampon : c'est un **gain de commande en dB** (+ un mute) sommé au fader de chaque membre |
| Document | liste `vcas[]` (ids `v_*`) ; l'appartenance est un champ **du membre** (`vca: "v_xxxx"\|null`) : un seul VCA par membre, par construction |
| Membres | tranches, bus `group` et `aux`, autres VCA (imbrication, profondeur ≤ 3). Pas master, matrice, N-1, écoute |
| Moteur | gain VCA **plié dans la CIBLE du `Gain` du fader du membre** (somme en dB, côté Python) ; **aucun changement d'ABI, aucun opérateur C nouveau** |
| −∞ | absorbant : fader du VCA à −120 dB (= −∞ du modèle) ⇒ membres à zéro ; la somme est plafonnée à +12 dB (maximum du fader) |
| Mute VCA | un mute de plus, posé **à l'endroit du mute du membre** (rampe d'un bloc), jamais écrit dans le `mute` du membre |
| Interface | rangée « VCA » en tête de la vue Console + **déploiement (spill)** qui filtre la vue sur les membres ; fader du membre **non mobile** avec repère du niveau effectif |
| Couleur | vert clair `#86d9a0` (seule teinte libre, plus claire que tous les autres rôles) |
| Zones | un VCA est un élément de zone comme une tranche ; le déplacer agit sur ses membres même hors zone (c'est son but) ; l'affectation est de la structure (`console.build`) |
| Macros | `param_tree` : `vcas.<id>.level_db` / `.mute` ; `/mutables` y ajoute les VCA ; 2 actions d'affectation en lot restent hors v1 |
| Réalisation | 5 étapes, 0.4.32 → 0.4.36 |

## 1. Ce que font les consoles de référence

Honnêteté sur les sources : les manuels de Calrec (Apollo/Artemis) et de SSL System T sont des PDF
volumineux ou des aides en ligne dont la récupération a échoué en partie. Ce qui suit distingue ce qui est
**cité** d'une source lue de ce qui est de **mémoire de métier, à vérifier** avant d'en faire un argument.

**Principe commun (cité).** Calrec : « les membres d'un groupe VCA sont les *slaves*, le fader qui pilote
le groupe est le *master* » ; un groupe VCA ne porte pas d'audio, il ne lie que des niveaux de fader et ne
peut agir ni sur le routage ni sur le traitement de ses membres. Les slaves peuvent rester **immobiles**
pendant que le master bouge (les LED donnent le niveau réel) — réglage « VCA slaves move with VCA
masters » côté Apollo. Calrec : un slave qui était *off* à la création reste *off* même master à 0 dB ; les
slaves suivent aussi l'état des touches **Cut, AFL, PFL** du master, pas seulement son niveau ; un master peut
lui-même être slave d'un autre master (imbrication).
Sources : Calrec, manuels Apollo/Artemis/Summa/Brio 36 (calrec.com, `Apollo Operator Manual 926-137`,
`Brio 36 User Manual 926-219`, chapitres VCA) telles que relevées par recherche ; non relues en PDF
complet (Artemis : fichier trop gros pour le récupérateur).

**Lawo mc²** (documentation en ligne mc²56, « VCA Groups », lue) : un canal ne peut être affecté qu'à **un
seul** VCA ; **tout type de canal** peut l'être, groupes, sommes, masters d'aux et **autres VCA** ;
l'imbrication est permise sans limite technique, la page recommande moins de six niveaux pour garder la
maîtrise du gain ; « *channel MUTE is switched on from a VCA master but not off* » : le mute du VCA
coupe les membres mais le démute du VCA ne force pas un membre coupé à la main à parler (le mute VCA est
une couche, pas une écriture). Affectation stockée dans les snapshots et productions. Solo, −∞, spill :
non traités par cette page (donc non documentés ici). Lawo **Diamond** (radio) est plus strict : pas
d'imbrication, jusqu'à 16 VCA, touche ON du VCA qui fait suivre les ON des membres, faders des membres
**non mobiles** (docs.lawo.com, `diamond-vca-grouping`).

**SSL** (aide en ligne des consoles live, page Mute, lue) : « *VCA mute, activated using the Mute button on
a VCA control channel to which the channel belongs* » — affiché **comme un mute group** (même couleur
orange sur la touche Mute du membre) et **supplantable par Mute Safe**. System T : non vérifié en
source ; de mémoire, mêmes principes (VCA = canal de contrôle, déploiement par la touche du VCA).

**DiGiCo** (SD) : les manuels listent « Control Groups » ; contenu non récupéré. De mémoire : groupes de
contrôle à deux faces (VCA et groupes de mute), pas d'audio, *spill* de la surface sur les membres. **À ne pas
citer comme source.**

**Spill (déploiement)** — attesté hors broadcast (Harrison Mixbus : le bouton Spill du VCA masque les
canaux qui n'y sont pas attachés, un seul spill actif à la fois ; Mixbus aussi : le mute du VCA coupe tous
les membres). C'est le comportement qu'on retrouve sur les surfaces à couches de ce type (Calrec,
Lawo, SSL : la touche du VCA ramène les membres sur la couche).

**Solo/PFL d'un VCA.** Cité Calrec : les slaves suivent la touche PFL/AFL du master. Pro Tools (DAW) fait
le contraire (solo d'un slave = ce slave seul), non transposable ici.

**Fader à −∞.** Aucune source lue ne le traite explicitement. Comportement universel d'un VCA analogique :
la tension de commande additionne des décibels, donc −∞ gagne. Retenu tel quel (§ 3).

**VCA et automation/mémoires.** Cité Lawo : affectations stockées dans snapshots et productions. Notre
« mémoire » (rappel simple par id, § 10 du document de base) sera traitée au § 2.6.

**Recommandation 1.** Calquer le modèle commun, retenir les points où les trois constructeurs lus convergent :
VCA sans audio ; membres à faders **non mobiles** ; **un seul VCA par membre** ; imbrication permise et
bornée ; mute VCA comme **couche** (mute-group-like), pas comme réécriture du mute du membre ;
PFL du VCA = PFL de ses membres.

## 2. Modèle de données

### 2.1 Champ et ids

Nouveau préfixe d'id `PREFIX_VCA = "v_"` (libre : s_ b_ e_ m_ x_ z_ o_ i_ d_ g_ sont pris), ajouté à `PREFIXES`.

```
doc.vcas : [{id: "v_*", label, short, level_db, mute, order?}]
strip.vca : null | "v_*"           (absent = null)
bus.vca   : null | "v_*"           (group, aux seulement)
vca.vca   : null | "v_*"           (imbrication : le parent)
```

- `level_db` : plage −120..+10 dB, pas 0,1, défaut 0 (même −120 = −∞ que le fader ; +10 plutôt que +12 :
  un VCA n'a pas à pousser au-delà de la marge de tout fader).
- `mute` : booléen **persisté** (comme le mute d'une tranche), défaut false.
- `label`/`short` : mêmes règles que tranches et bus (`bad_short`).

**Pourquoi le champ côté membre et pas `members[]` côté VCA** (contraire des zones, qui sont
multi-appartenance) : « un membre = un seul VCA » devient impossible à violer. Avec une liste, il faudrait
un code `vca_member_duplicate` et un arbitrage ; avec le champ, le document ne peut pas porter le cas.
Inconvénient : l'éditeur d'affectation passe par les membres — c'est de toute façon le geste (« affecter ces
tranches à ce VCA »).

### 2.2 Validation — codes d'erreur (un code stable par règle, chacun testé)

| Code | Règle |
|---|---|
| `bad_vca` | `vcas` non liste, entrée non objet, champ de type faux, `level_db` hors bornes (par `_check_value`), `mute` non booléen |
| `vca_unknown` | `vca` d'un membre ne désigne aucun VCA du document |
| `vca_member_kind` | `vca` sur un bus de genre autre que `group`/`aux` (master, matrice, N-1, écoute) |
| `vca_member_no_fader` | membre sans élément `fader` dans sa chaîne (rien où plier le gain) |
| `vca_cycle` | la chaîne de `vca` d'un VCA boucle (ou un VCA est son propre parent) |
| `vca_too_deep` | imbrication > 3 niveaux (VCA → VCA → VCA → membre) |
| `duplicate_id` / `bad_prefix` / `missing_id` | existants, réutilisés pour les ids `v_*` |
| `zone_member_unknown` | existant ; son ensemble « connu » est étendu aux ids de VCA |

La docstring dit aujourd'hui « topologie acyclique par construction, pas de détection de cycle » : `vca_cycle`
est la **première** règle de cycle du modèle, elle est localisée à cette arête (marche de parent en parent,
bornée par 4).

`vca_member_no_fader` a une conséquence d'édition : retirer le fader d'un membre est refusé tant qu'il est
affecté (la structure se rejette entière à la validation, comme pour les autres règles).

### 2.3 Canonicalisation et migration

- `canonicalize` : `d.setdefault("vcas", [])` ; `id` manquant tiré avec `new_id(PREFIX_VCA, taken)` ; `label`,
  `short` = "" ; `level_db` = 0.0 ; `mute` = false ; `vca` absent des membres = `None`.
- **Aucune migration** : un document sans `vcas` reste valide et canonique avec `[]` ; aucun ancien chemin
  ne change, aucune forme n'est réécrite. Un moteur 0.4.32 qui reçoit un document d'avant n'y voit aucune
  différence.
- Descente de version : un document avec `vcas` rechargé par un plugin plus ancien est refusé en
  `bad_document`/champ inconnu selon l'ancienne tolérance ; à vérifier dans la première étape (test de
  retour arrière) — le document de la console est de toute façon sauvegardé avec la version qui l'a écrit.

### 2.4 Zones et droits

- `control.zones.set.owner` : étendre la regex à `^(?:strips|buses|vcas)\.([^.]+)`. Sans cela, tout chemin
  `vcas.*` est « impossible à rattacher » et REFUSÉ à un opérateur restreint (règle déjà en place :
  jamais laissé passer faute de savoir à qui il appartient). C'est le comportement sûr par défaut, mais
  il rendrait les VCA inutilisables par un opérateur à zones.
- Un VCA peut être membre d'une zone (`zones[].members` accepte les ids `v_*`). **Recommandation** : un
  opérateur qui détient la zone du VCA le manipule, **même si certains membres sont hors de sa zone** — c'est
  la définition d'un groupe de commande, et l'inverse viderait l'objet. La frontière est donc posée là où
  on décide qui est membre : **l'affectation est de la structure**, donc `console.build` (déjà le cas :
  tout passe par `/structure/apply`). L'éditeur affiche un avertissement non bloquant quand un VCA
  rassemble des membres de zones différentes (`vca_cross_zone`, information, pas une erreur de
  validation).
- Un VCA hors de toute zone reste ouvert à tous (règle générale).
- Le mute (`/mute`, `id_field` = `id`) passe par `element_zones(doc, id)` : comme les VCA sont des
  objets d'id propre au niveau racine, rien à changer côté `plugin_zones` ; seule la regex ci-dessus.
- Rappel de mémoire filtré par zone (lot ultérieur du § 10) : le niveau et le mute d'un VCA sont des
  éléments à part entière du filtre.

### 2.5 Sélections nommées et sélection multiple

- Une sélection nommée liste des ids de tranches (`console_layouts.check_selection`) : **ne pas y mêler les
  VCA** (on ne sélectionne pas un VCA pour lui appliquer l'action de barre : mute/PFL sur le VCA sont ses
  propres touches). Aucun changement de `console_layouts.py`.
- Nouveau geste dans la barre de sélection multiple : **« Affecter à un VCA… »** (liste des VCA +
  « aucun ») qui écrit `vca` sur chaque cible via `/structure/apply`, en respectant `canOperate` (cible hors
  zone : sautée, comptée dans le message existant « n sautée(s) »). Les sélections nommées deviennent ainsi la
  manière de peupler un VCA en une fois.
- Les sélections et dispositions mémorisent des ids ; un VCA supprimé ne casse rien (pas d'id de VCA à
  l'intérieur).
- Suppression d'un VCA : l'interface passe les `vca` de ses membres à `null` **dans la même structure**
  (sinon `vca_unknown`), et renvoie les enfants VCA au parent supprimé (ou à `null`) — c'est décidé :
  on ne laisse pas un membre à 0 dB effectif sans que l'exploitant le voie.

### 2.6 Mémoires

`level_db` et `mute` d'un VCA sont des valeurs vives du document : elles sont capturées et rappelées comme
n'importe quel paramètre vif. L'**affectation** (champ `vca`) est de la structure : une mémoire ne la
rappelle pas (comme elle ne rappelle ni câblage ni chaîne). Rappeler une mémoire où un VCA est à −6 dB
remet donc le VCA à −6 dB sans toucher le fader des membres : exactement le comportement « non mobile ».

## 3. Moteur

### 3.1 Où s'applique le gain

Constat de lecture (`engine.py:_chain`, ~l. 481–500) : le fader d'une chaîne est un `Gain` (un
`float64[2]` courant/cible) dont la cible est calculée **côté Python** par une fermeture
`tgt = lambda: db_lin(p["gain_db"])`, et que le noyau C ramène linéairement du courant à la cible sur le
bloc (`gain_ramp` en exécuteur Python, `OP_GAIN` avec l'adresse de `gain.a` en exécuteur C). Le noyau ne
voit jamais les dB.

**Recommandation : plier le VCA dans cette fermeture.**

```
cible_fader(membre) = db_lin( clamp( gain_db_fader + Σ niveau_dB(ancêtres VCA), −120, +12 ) )
                      → 0.0 si l'un des niveaux de la chaîne est ≤ −120 dB
```

Parce que :

1. **Zéro changement d'ABI.** `PLAN_ABI = 6` reste, `OP_DTYPE` aussi : l'opérateur `OP_GAIN` existant lit la
   même case. Aucun nouvel opérateur, aucun nouveau tampon, **aucun coût CPU par bloc** (le travail se fait à
   chaque RÉGLAGE, pas à chaque bloc : une somme de quelques flottants par membre).
2. **Somme en dB, pas produit de gains linéaires** : c'est identique, et ça rend le −∞ exact (un niveau à
   −120 dB court-circuite en 0.0, sans produit de 1e-6 qui laisserait filtrer du bruit).
3. **Les points de prélèvement restent corrects sans rien écrire** : le PFL est pris AVANT le premier
   mute/fader (`pfl_tap`) — il n'est donc pas affecté par le VCA, ce qui est ce que veut un PFL ; l'AFL
   (fin de chaîne), les départs après le fader, les affectations vers groupe/master (fin de chaîne) et le
   vumètre « post » voient le gain VCA. Les départs aux pris AVANT le fader ne bougent pas — c'est le
   comportement attendu d'un départ pré-fader sur toute console. Les groupes, eux, sont des bus à fader :
   un membre dans un groupe lui-même sous VCA est atténué deux fois (dB additionnés), comme sur une console.
4. **Pas de rampe nouvelle** : le moteur rampe déjà tout `Gain` linéairement sur un bloc (240 échantillons,
   5 ms). Une rampe de 5 ms sur un coup de fader de VCA est celle d'un fader de tranche. Les N membres
   reçoivent leur nouvelle cible **sous le même `self.lock` et dans le même appel `set()`**, donc ils
   montent **ensemble dans le même bloc** (cohérence de phase entre membres d'un même VCA, ce que fait un
   vrai VCA).

### 3.2 Mécanisme interne

- `_Ctl` (point d'entrée d'un réglage) : le `refresh` de `vcas.<vid>.level_db` / `.mute` appelle les `refresh`
  des fader de tous ses **descendants** (membres directs et indirects). Le plan garde, à la compilation, un
  dictionnaire `vca_dependents[vid] = [refresh_fader, …]` construit pendant `_build` (le `_chain` connaît le
  `vca` de la tranche/du bus qu'il compile ; il enregistre son `refresh` et son `Gain` auprès de son
  parent).
- La fermeture du fader lit le **document vivant** (parent → niveau, mute) : un seul endroit sait calculer
  le gain effectif d'un membre, `effective_vca_db(doc, id)` placé dans `console_model.py` (fonction pure,
  testable hors moteur, partagée avec l'orchestrateur pour l'arbre de macros et avec l'interface via
  `/state`).
- Changer l'**affectation** (champ `vca`) est une structure : le plan est reconstruit à côté et basculé
  entre deux blocs (§ 7.3 du document de base), les `Gain` de fader sont REPRIS (états réutilisés par id),
  leur cible change d'un coup et monte en rampe — pas de clic. Aucune latence ne change (un VCA n'ajoute
  aucun module) donc `accept_latency_jump` n'est jamais demandé.
- Coût d'un coup de fader de VCA de 64 membres : 64 écritures dans des `float64`, quelques microsecondes sous
  le verrou du plan, comparé à ~1 ms par appel aujourd'hui pour les autres `set`. Négligeable ; à mesurer
  quand même (étape 1, § 6).

### 3.3 Mute VCA

**Recommandation : le mute du VCA se plie dans la cible de l'élément `mute` du membre**
(`0.0 if p["on"] or vca_muted(membre) else 1.0`), c'est-à-dire **au même point de la chaîne qu'un mute manuel**,
avec la même rampe d'un bloc. Pourquoi là et pas dans le fader :

- Un mute coupe aussi les départs pré-fader pris après lui dans l'ordre du signal ; plier le mute VCA dans le
  fader laisserait vivre un départ aux pris entre mute et fader — surprenant : « VCA muté » doit sonner comme
  « chaque tranche muette ».
- Le champ `mute.on` du membre n'est **jamais écrit** : démuter le VCA rend à chaque membre son état
  propre (mute manuel conservé). C'est la couche « mute group » de SSL. La nuance de Lawo (le démute du VCA
  ne démute pas un membre coupé à la main) est retrouvée gratuitement.
- Membre sans élément `mute` dans sa chaîne : repli sur le fader (cible 0). Ce cas existe (le `mute` est
  facultatif) ; il est signalé en information dans l'éditeur.

Les indicateurs de l'interface doivent distinguer « coupé par moi » et « coupé par le VCA » (même idée que
SSL : même teinte, libellé différent). Pas de Mute Safe en v1 (question ouverte n° 2).

### 3.4 Comportement −∞

- Niveau de VCA ≤ −120 dB ⇒ cible 0.0 pour tous les descendants, quel que soit le fader du membre. Ce
  n'est pas un mute (le champ `mute` est indépendant) : remonter le VCA rend le son.
- Le fader d'un membre à son maximum (+12) et un VCA à +10 : l'effectif est plafonné à +12 dB. Le niveau
  effectif est publié dans `/state` (`strips.<id>.vca_gain_db`, valeur après plafond) pour que l'interface
  dise la vérité quand le plafond mord. **Pas de plafond sur le VCA lui-même.**
- Un membre dont le fader est à −120 dB reste à zéro quel que soit le VCA.

### 3.5 Vumètres, état publié

`/state` gagne `vcas: {<vid>: {level_db, mute, members: n, effective_mute: bool}}` et, par tranche/bus
affecté, `vca: <vid>`, `vca_gain_db`, `vca_muted`. Pas de vumètre de VCA : il n'y a pas de signal. (Un
« vumètre de somme des membres » ne serait qu'une décoration trompeuse.)

### 3.6 Ce qu'on évite

- **Écrire le gain VCA dans le `gain_db` du fader des membres** : compose mal (niveaux perdus au
  retrait du VCA, deux VCA qui se disputent), et fait « bouger » les faders. Rejeté.
- **Un opérateur C « VCA »** ou un second `Gain` en série dans la chaîne : ABI à changer pour un gain qu'on sait
  déjà plier ; un gain de plus par membre = un passage de plus sur le tampon (coût mémoire, ce que
  CONSOLE_AUDIO.md § 14 et les mesures du mur montrent comme le vrai prix).

## 4. Interface (iPad = référence tactile, cibles de 44 px)

### 4.1 Où apparaissent les VCA dans la vue Console (vue D)

- **Une rangée de colonnes « VCA » à gauche de la vue**, avant les tranches (les consoles les mettent
  près des masters ; ici la vue défile horizontalement et l'exploitation veut le VCA sous la main, à
  l'extrémité qui ne défile pas — même mécanisme que la zone `ac-fixedzone`, déjà utilisée pour les colonnes
  fixes). Une colonne de VCA = nom, mute, PFL, fader (même composant `fader`), un bouton **DÉPLOYER**
  (spill), le nombre de membres. Cibles ≥ 44 px, comme les autres colonnes.
- Colonnes **étroites** par défaut (ce sont des commandes, pas des chaînes) : la disposition « Phone »
  montre les VCA au lieu de la première rangée de tranches.
- Dans la colonne d'un membre : **un liseré aux couleurs du VCA** sous le nom (comme `ac-zone`, qui trace un
  trait coloré par zone) et, au-dessus du fader, une **puce** « VCA 2 −3,0 dB » (touche : déploie ce VCA).
  Le fader du membre ne bouge pas ; un **repère** (petit trait de la couleur VCA) marque la position du niveau
  effectif sur la règle du fader, et la valeur numérique affiche l'effectif entre parenthèses. Si le plafond
  de +12 dB mord, le repère s'arrête et la puce passe en avertissement.

### 4.2 Déploiement (spill)

**Recommandation : le déploiement est un FILTRE de la vue, pas une opération sur la surface.**
Toucher DÉPLOYER sur un VCA :

1. met la vue sur « membres de ce VCA » (récursif : les VCA enfants s'y affichent, repliés, avec leur
   propre bouton), dans l'ordre du document ;
2. garde le VCA épinglé en tête (pour le régler sans quitter le spill) ;
3. un bandeau « VCA 2 déployé · ✕ » sort du mode ; un seul déploiement à la fois (comme Mixbus) ;
4. l'état est **par navigateur** (localStorage, comme `layChoice`) et non dans le document : deux opérateurs
   voient chacun ce qu'ils veulent.

C'est le geste de la sélection multiple inversé : au lieu de « je choisis des tranches pour un VCA », « je
choisis un VCA pour voir ses tranches ». Les deux partagent le même filtre `itemsOf()`. Sur iPad : un touch
sur DÉPLOYER, un touch sur le bandeau ; aucun glisser.

### 4.3 Lien avec les dispositions

Une disposition est une liste de **rangées par tranche** (`name`, `mute`, `fader`…) : c'est du contenu de
colonne. Les VCA n'y entrent pas comme rangée. Deux ajouts bornés :

- un type de rangée `vca` (la puce et le liseré du § 4.1), posable ou non selon la disposition. Le serveur
  garde les clés inconnues, un ancien navigateur les saute (règle de `console_layouts.py`) ;
- une option de disposition `vca_strip: "left" | "hidden"` pour masquer la rangée de colonnes VCA dans la
  disposition « Phone ».

`console_layouts.py` n'a pas à changer : il valide la forme, pas le vocabulaire des rangées.

### 4.4 Éditeur

- Création/suppression/renommage d'un VCA : dans l'écran « Zones » existant (même gabarit : liste,
  nom, supprimer), section « VCA ».
- Affectation : (a) sélection multiple + « Affecter à un VCA… » (§ 2.5), (b) dans la fiche d'une tranche, un
  sélecteur « VCA ». Les deux écrivent `vca` par `editStructure`.
- Garde-fous visibles : un membre sans fader est grisé avec la raison (`vca_member_no_fader`) ; un membre
  d'une autre zone que celle du VCA est signalé (`vca_cross_zone`).

### 4.5 Couleur

Palette décidée : master `#e5605a`, groupe `#5b9be0`, aux `#e2c84a`, matrice `#b483f0`, N-1 `#45c8c8`,
Direct Out gris ; orange = sélection. Les teintes libres restent peu nombreuses (vert, rose-magenta,
brun) ; le rose se confond avec le violet matrice et le rouge master à l'échelle d'un trait de 2 px.

**Recommandation : vert clair `#86d9a0`** (teinte libre ; luminosité estimée nettement supérieure à celle du bleu groupe et du rouge master, proche du jaune aux mais d'une autre teinte — à vérifier à l'écran). Deux
garde-fous :

- la couleur n'est **jamais le seul porteur** : le VCA porte aussi un motif (liseré double, ou « ‖ » devant
  le nom) pour ne pas dépendre de la vision des couleurs ni de l'écran ;
- le vert est aussi la couleur d'état « en marche » de l'interface (`--status-running-fg`). Dans la vue
  Console il n'est pas utilisé pour un état (le point de connexion est dans l'entête) ; ne jamais l'employer
  pour un état dans une colonne de la vue Console. Si l'exploitant n'aime pas ce partage : repli sur un
  ivoire `#e8e1c8` (question ouverte n° 3).

Comme les couleurs d'essence sont invariantes par thème (mémoire projet), la valeur est la même en clair et
en sombre ; vérifier le contraste du texte posé dessus (texte sombre sur fond VCA plein).

## 5. Macros, NMOS, Ember+

### 5.1 Macros (obligatoire)

- **`param_tree`** : nouvelle racine `{list: "vcas", kind: "vca", label: "VCA"}` avec
  `fields: {level_db: {field: "vca.level_db", label: "Niveau"}, mute: {field: "vca.mute", label: "Mute",
  live: "/doc?live=1"}}`. Les spécifications `vca.level_db` / `vca.mute` vont dans `modules.json` (`fields`),
  comme `input.gain_db`. Chemins : `vcas.<vid>.level_db`, `vcas.<vid>.mute`, ajoutés à `param_paths` ; le
  moteur enregistre un `_Ctl` par VCA (clé `vcas.<vid>`).
- **`/mutables`** (options des actions `mute`/`unmute`) : ajoute les VCA, `kind: "vca"`, `path:
  "vcas.<vid>.mute"`. Les deux actions existantes pilotent donc les VCA sans nouvelle action. `Runtime.mute`
  les accepte par la même voie.
- **Condition** : `mute` et `level_db` du VCA sont dans `/doc?live=1`, lisibles en condition.
- **Affectation par macro** : `strips.<sid>.vca` est de la structure, non pilotable à chaud par `/set` ;
  ce n'est pas une capacité « morte » car le geste d'exploitation est le NIVEAU et le MUTE ; l'affectation
  est une opération de construction (`console.build`). À signaler en réserve : si l'exploitant veut
  « rappeler une composition de VCA » depuis une macro, il faut une action `/vca/assign` (structure en
  lot) — hors v1 (question ouverte n° 1).
- **PFL d'un VCA par macro** : voir 5.3.

### 5.2 Emplacements, Ember+, TSL

L'identité fonctionnelle d'un VCA est son **emplacement** (3ᵉ barreau, `production_roles`), pas son id
`v_*` : les emplacements sont créés explicitement par un humain. **v1 : pas d'emplacement de VCA** ;
Ember+ ne verra les VCA que le jour où la console expose un arbre d'emplacements de commande. Recommandation :
ne pas inventer une identité de plus (cf. CLAUDE.md : « ne pas re-dériver une identité »). NMOS : les
VCA n'ont pas de flux et n'apparaissent pas dans IS-04. IS-12/IS-14 (modèle MS-05-02) : hors v1 ; si la console
est un jour exposée en NcBlock de commande, un VCA est un `NcWorker` de gain sans port de signal —
noté, pas conçu ici.

### 5.3 PFL du VCA

Le PFL d'un VCA n'a pas de prise audio : il doit **solo-er ses membres** (Calrec). Implémenté en v1 **côté
interface** : la touche PFL du VCA écrit `buses.<lid>.solo.<mid>` = vrai pour chaque feuille membre
(une écriture groupée par `/set` `sets:[…]`, qui existe déjà) ; elle est allumée quand toutes ses feuilles le
sont. Pas de modèle, pas de moteur. Réserve : un membre ajouté pendant que le PFL est tenu n'est pas
inclus avant le geste suivant — acceptable pour un état momentané (les `solo` ne sont jamais persistés).
Si la macro doit en disposer : une action `vca_pfl` appuyée sur ce même lot, à reprendre en étape 5.

## 6. Plan de réalisation

Numérotation au dernier chiffre seulement (0.4.31 → 0.4.32…), pas de 0.5.0.

| Étape | Version | Contenu | Livrable vérifiable |
|---|---|---|---|
| 1. Modèle | 0.4.32 | `PREFIX_VCA`, `vcas[]`, `vca` des membres, validation (7 codes), canonicalisation, `effective_vca_db()`, `param_paths`, spécifications `modules.json`, clés i18n des codes `verr.*` | `tests/verif_console_modele.py` étendu, un test par code ; document ancien identique après canonicalisation |
| 2. Moteur | 0.4.33 | cible du fader et du mute pliée au VCA, `vca_dependents`, `_Ctl` `vcas.<vid>`, `/state` (§ 3.5), `/mutables` | `verif_console_moteur.py` : gains exacts, −∞, plafond +12, imbrication, mute couche ; `verif_console_plan_c.py` : exécuteur C identique à Python bloc par bloc ; `PLAN_ABI` inchangé (assertion) |
| 3. Droits et macros | 0.4.34 | regex `owner` du manifeste, zones étendues aux VCA, `param_tree`, actions mute/unmute, test de non-régression des zones | `verif_console_zones.py` (opérateur restreint : VCA hors zone refusé, dans zone accepté, membre hors zone non filtré) ; test de l'arbre de macros |
| 4. Interface | 0.4.35 | colonnes VCA, puce, repère de niveau effectif, déploiement, éditeur, « Affecter à un VCA… », couleur | contrôle visuel de l'exploitant (iPad) ; test statique des clés i18n FR/EN |
| 5. Finitions | 0.4.36 | PFL du VCA, rangée `vca` de disposition + option, sélecteur dans la fiche, action `vca_pfl` si demandée, aide (`help.md`/`help.fr.md`) | tests de lot de PFL ; garde-fou d'aide |

Tests à écrire (tous hors ligne, base jetable ; **les muter** une fois verts — consigne du projet : une garde verte du
premier coup est suspecte) :
1. validation : un test par code du § 2.2 + cas limites (cycle de longueur 1, 2 ; profondeur 4 ; membre
   `master`) ;
2. moteur : (a) fader −10 + VCA −6 = −16 exact en amplitude ; (b) VCA −120 ⇒ échantillons exactement nuls ;
   (c) plafond +12 ; (d) imbrication 3 niveaux ; (e) mute VCA coupe, démute rend le mute manuel intact ;
   (f) deux membres d'un VCA montent dans le MÊME bloc (cohérence de phase) ; (g) changement
   d'affectation sans clic (aucun saut > rampe attendue) ;
3. exécuteur C = exécuteur Python sur un document à VCA ;
4. zones : regex `owner` ; muter la regex pour vérifier que le test mord ;
5. rétrocompatibilité : le document de démarrage de `plugin.json` et les golden existants inchangés ;
6. i18n : toutes les clés `verr.<code>` des nouveaux codes en FR et EN.

Risques :
- **Plafond silencieux** : un exploitant pousse le VCA et rien ne bouge au-delà de +12 ⇒ puce
  d'avertissement obligatoire (§ 4.1), sinon c'est un échec silencieux.
- **Double atténuation par imbrication groupe + VCA** (§ 3.1.3) : conforme aux consoles, mais surprenant ;
  le dire dans l'aide.
- **Rafale d'écritures** : un fader de VCA sur iPad émet des `/set` rapprochés ; chaque `set` rafraîchit N
  membres. Vérifier qu'on reste sous le budget (étape 2) et que l'interface regroupe déjà ses envois.
- **Cycle de compilation** : l'affectation passe par `/structure/apply` (50–120 ms de Python sur le cœur
  unique, mesuré 2026-10-14). Le fil audio cède déjà la main (`time.sleep(0)` par chaîne) ; une affectation de
  64 tranches à la suite ne doit pas dépasser un seul appliqué : un lot, pas 64 applications.
- **Descente de version** (§ 2.3) à vérifier.
- **Prise d'un droit** : un opérateur d'une zone agit sur des membres hors zone via le VCA (§ 2.4) — à
  confirmer par l'exploitant (question n° 4).

## 7. Questions ouvertes à l'exploitant

1. Faut-il pouvoir **rappeler une composition de VCA par macro** (réaffecter des tranches à un VCA depuis une
   macro), ou l'affectation reste-t-elle un acte de construction (`console.build`) ? Recommandation : construction.
2. Le **mute d'un VCA** doit-il pouvoir être **ignoré par une tranche protégée** (« Mute Safe » de SSL, pour un
   micro d'antenne qui ne doit jamais être coupé par un groupe) ? Recommandation : pas en v1.
3. La **couleur** : vert clair `#86d9a0` (recommandé) ou ivoire `#e8e1c8` (évite toute confusion avec les états) ?
4. Un opérateur limité à sa zone **peut-il bouger un VCA dont des membres sont hors de sa zone** ? Recommandation :
   oui (c'est le sens d'un groupe de commande) ; la protection est à l'affectation.
