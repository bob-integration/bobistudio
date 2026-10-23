# Console audio — mémoires, scènes, copier / coller (décisions du 2026-10-17)

Décisions de l'exploitant, prises en mode questions-réponses le 2026-10-17. Rien n'est encore construit : la
console n'a aujourd'hui AUCUNE mémoire (pas de `control.recall` au manifeste).

## Trois choses distinctes

| | Contenu | Rappel |
|---|---|---|
| **Scène** | les VALEURS de la console (cf. « Contenu d'une scène ») | en direct, sans coupure, avec fondu |
| **Show** | le document COMPLET : tranches, bus, patch, routage, plugins | hors antenne : recompile la console (latences) |
| **Mémoire d'outil** | les réglages d'UN outil (EQ, compresseur, gate, filtre… ou plugin tiers) | sur un outil du même genre |

## Scènes

- **Conduite ordonnée + rappel libre** : numéros (1, 2, 2.5, 3…), « suivante / précédente » pour dérouler une
  émission, et rappel direct de n'importe quelle scène.
- **Durée de fondu par scène** (0 = coupe franche). Les valeurs continues glissent (faders, pans, départs, EQ) ;
  mutes et commutateurs basculent à la fin, ou au début (réglable).
- **Contenu d'une scène** : toutes les valeurs (faders, mutes, pans, trims, départs, EQ, dynamiques, VCA), ET
  ces réglages à la frontière valeur / structure :
  - affectations aux bus (PGM, groupes, VCA), sans créer ni supprimer de bus ;
  - choix d'entrée A/B ;
  - bypass des outils et des plugins ;
  - libellés et couleurs (« Invité 1 » devient « Mme Martin »).
- **Zones** : un opérateur limité à ses zones enregistre une scène qui ne contient QUE ses tranches et bus. Au
  rappel, le reste de la console n'est pas concerné. Plusieurs opérateurs ont chacun leurs scènes sans se
  marcher dessus. Le rappel passe de toute façon par le filtre de zones (`plugin_zones`), comme la page.

### Points techniques à régler (pas des décisions)

- Une affectation qui exige un départ ABSENT du document est une structure : soit la scène ne peut l'appliquer
  qu'à un départ existant (activé ou coupé), soit le rappel crée le départ par une application de structure
  sans saut de latence. À trancher à la conception, et à SIGNALER au rappel plutôt qu'à taire.
- Libellés et couleurs vivent dans le document (structure) : il faut un chemin de rappel qui ne recompile rien.
- Le fondu se fait DANS la console (rampes du moteur), pas par une rafale de `/set` depuis l'orchestrateur.

## Protection au rappel (« safe »)

- **Par outil de tranche** : la tranche entière, ou seulement certains de ses outils (fader, EQ, dynamique,
  départs, pan, entrée…). Exemple : le fader du présentateur ne bouge pas, son EQ suit la scène.
- **Commune à la console** : un état de la console, visible par tous et valable quel que soit celui qui
  rappelle.
- **Plus des protections ponctuelles au rappel** : en rappelant une scène, l'opérateur peut ajouter des
  protections pour CE rappel seulement.

## Mémoires d'outils

- **Deux bibliothèques visibles au rappel** : une commune à l'installation (toutes les consoles, avec un
  auteur) et une personnelle par opérateur, qui le suit sur toutes les consoles.
- **Plugins tiers (LV2) compris** : leurs mémoires se rangent avec le plugin (même identifiant), à côté de ses
  présélections d'usine, et ne se proposent que là où ce plugin est inséré.

## Copier / coller

- **Un outil vers le même outil**, sur une ou plusieurs tranches.
- **Entre consoles** : le presse-papier suit l'opérateur (côté serveur, pas dans un onglet de navigateur).
- **Outils de forme différente : au mieux**, avec ce qui ne passe pas signalé. Par exemple, d'un EQ 4 bandes
  vers un EQ 6 bandes, les 4 bandes passent et les autres ne bougent pas. **Si la source et la cible ne sont
  pas le même outil ou le même plugin, on PRÉVIENT avant de coller** en annonçant ce que ça va faire (ce qui
  passe, ce qui reste tel quel) ; l'opérateur confirme.

## Hors périmètre de ces décisions (à reprendre)

- Copier une tranche entière et la copie partielle (« cocher ce qu'on emporte ») : non retenus à ce stade.
- Dispositions par opérateur et lien opérateur → monitoring côté serveur : demande séparée (surfaces de
  contrôle), détails à venir de l'exploitant.
- Exposer aux macros (règle du projet) : rappeler une scène, suivante / précédente, rappeler une mémoire d'outil
  sur une tranche, poser / ôter une protection ; état lisible : scène courante.

---

# Répartitions par opérateur et monitoring (décisions du 2026-10-17)

Demande née des surfaces de contrôle (`SURFACE_CONTROLE.md`), mais valable aussi à l'écran.

## Ce qu'est une répartition

À ne pas confondre avec la « disposition des rangées » (Standard, Vue d'ensemble…), qui existe déjà par
opérateur (`/api/console-zones/<vmid>/layouts`, mine / shared). Une RÉPARTITION dit quelles tranches se
présentent, dans quel ordre, par pages :
- **ordre libre** des tranches, indépendant de l'ordre de la console ;
- **pages / couches**, avec une **taille de page par répartition** (8, 12, 16…), la même pour toutes ses pages ;
- **elle emporte aussi la disposition des rangées** à afficher ;
- une page **mélange librement** tranches, bus et VCA ;
- les tranches rangées nulle part tombent dans une **page « Reste » automatique** : rien ne disparaît, une
  tranche ajoutée à la console y apparaît sans retoucher la répartition ;
- **le 9e fader (master) se règle par répartition** : PGM, le monitoring de l'opérateur, un VCA…, fixe quelle
  que soit la page ;
- elle peut contenir des tranches **hors des zones** de l'opérateur : visibles (vu-mètre, état), pas pilotables.

## Qui, où

- **Créées par l'opérateur et par l'admin** (droit `console.build`) : l'admin en prépare et les partage, pour
  tous ou pour un opérateur donné.
- **Écran** : trois choix — toute la console (DÉFAUT), la répartition de l'opérateur, celle de la surface. Le
  choix se fait dans la page Réglages de la console ; il est retenu PAR OPÉRATEUR côté serveur, avec une case
  « propre à cet écran » (localStorage) pour un écran qui doit montrer autre chose.
- **Surfaces** : par défaut la disposition générale ; une autre répartition se choisit dans les réglages de la
  surface. Deux surfaces du même opérateur : **liées (bancs consécutifs) ou indépendantes, au choix par
  surface**.

## Monitoring de l'opérateur, côté serveur

- Aujourd'hui retenu par le navigateur (`localStorage` `monKey()` dans `control.js`) : il passe au SERVEUR.
- **L'admin attribue** un monitoring à l'opérateur dans les zones de la console. **L'opérateur bascule** entre
  ce monitoring-là et le monitoring par défaut de la console, et rien d'autre. Son choix est retenu par le
  serveur : il le retrouve sur tout écran, et sa surface le suit.

---

# Page Réglages de la console : plan des onglets (validé 2026-10-17, « bonne base pour le moment »)

| Onglet | Contenu | Pour qui |
|---|---|---|
| Monitoring | mon monitoring (le mien / celui de la console), source, mode PFL/AFL/auto, PFL cumulatifs, repli | opérateur |
| Affichage | vue à l'écran (toute la console / ma répartition / celle de ma surface, « propre à cet écran »), dispositions des rangées, réglage au toucher, témoin de saturation | opérateur |
| Répartitions | mes répartitions, celles partagées par l'admin, éditeur | opérateur (partage : admin) |
| Scènes | fondu par défaut, bascule des commutateurs début/fin, protections communes (vue d'ensemble) | opérateur (protections : admin) |
| Shows | la console entière, hors antenne (cf. « Shows » plus bas) | tous (lecture seule sans console.build) |
| Bibliothèques | toutes les bibliothèques (mémoires d'outils, scènes, shows, répartitions, dispositions) : favoris par utilisateur, étiquettes, dossiers, tri, export / import, partage | tous (classer : qui peut écrire l'élément) |
| Opérateurs | zones, attribution des monitorings, répartitions partagées par opérateur | admin |
| Journaux | saturations 24 h, puis journal d'exploitation | tous |

La conduite des scènes (rappeler, suivante) reste une VUE d'exploitation, pas un réglage. Un onglet réservé à
l'admin est masqué pour qui n'a pas le droit (pas grisé).

---

# Shows (lot 3, construit le 2026-10-17)

Code : `app/plugin_shows.py` (logique, générique et déclarative), `app/routes/plugin_shows_api.py` (routes),
déclaration `control.shows` dans `plugins/audio_console/plugin.json`, onglet **Shows** de la page Réglages
(section `SHOWS` de `control.js`). Garde : `tests/verif_console_shows.py` (deux vraies consoles, base jetable).

## Ce qu'est un show, et ce qu'il n'est pas

| | Portée | Rappel | Lié à |
|---|---|---|---|
| **Show** | la console SEULE, document complet (tranches, bus, patch, routage, plugins, zones, conduite de scènes) | application de structure, hors antenne, rapport d'abord | rien : réutilisable d'une émission à l'autre, sur une autre console, sur une autre installation (fichier JSON) |
| Projet (`app/projects.py`) | tout un PLATEAU : conteneurs, câblage, placement | recrée les conteneurs | cette installation |
| Sauvegarde de config (`/api/containers/<vmid>/config_snapshots`) | les `params` bruts d'UN conteneur | redéploiement | ce vmid |
| Préréglage de plugin (`plugin_store`) | des réglages opaques, jamais de structure | à chaud | un type de plugin |

## Décisions de conception (à valider par l'exploitant)

- **Un show emporte sa conduite** (scènes, protections communes, réglages de scène, scène courante) : une émission
  vient avec sa conduite. Une édition de structure ordinaire, elle, GARDE la conduite vivante (`carry_scenes`) ; le
  show passe donc `replace_scenes: true` à la validation et à l'application (seul ajout côté console, 0.4.75). Un
  fondu de scène en cours est interrompu au chargement.
- **Un show emporte ses zones** (elles sont dans le document). Les ATTRIBUTIONS, elles, restent à la console (clé =
  emplacement) : une attribution dont la zone n'existe pas dans le show est SIGNALÉE au rapport (ses opérateurs ne
  piloteraient plus que les éléments hors zone), jamais une erreur.
- **Capture sans états momentanés** : l'orchestrateur lit l'instantané PERSISTABLE déclaré (`persist.snapshot`,
  `/doc`), où la console a déjà retiré PFL, ordre, dim / cut, AFV ouvert — la console reste seule à savoir ce qui est
  momentané. Le compteur de révision (`version`) n'est pas gardé.
- **Câblage** : les flux d'entrée du show (`wiring.consumes` en mode clé) sont comparés à ce que produit le plateau
  (même dérivation que la page Câbles). Absent → SIGNALÉ, gardé par défaut (la console lit du silence et compte les
  blocs manquants, comme aujourd'hui ; il se branchera s'il apparaît), ou débranché sur demande (case du rapport).
  La page Câbles ignore un consommateur sans producteur (pas de câble dessiné, rien ne casse). Les **sorties** sont
  nommées d'après le hostname de la console QUI CHARGE (`{hostname}_{key}`) : aucune usurpation. Une sortie de la
  console d'origine rebouclée sur une de ses entrées est rattachée à la sortie équivalente de la console qui charge
  (par défaut ; case du rapport pour garder le flux d'origine).
- **Versions** : un show écrit par une version plus récente du plugin est refusé (`show_newer_than_plugin`) à
  l'import et au chargement (version du conteneur cible). Un show plus ancien passe : `canonicalize` de la console
  le migre, comme tout document persisté.
- **Droits** : enregistrer, charger, importer, supprimer = `console.build` (déclaré `control.shows.permission`) ;
  lister et exporter = login. Jamais un opérateur limité à ses zones : un show remplace toute la console.
- **Macros : NON.** Charger un show recompile la console (hors antenne), peut déplacer des latences qu'un HUMAIN doit
  accepter après avoir lu le rapport, et remplace les zones de tous les opérateurs. Un déclencheur ou un pupitre n'a
  personne pour lire ce rapport — même raisonnement que `plugin_zones.macro_guard` pour la structure. Aucune action
  du manifeste ne charge un show (garde au banc). Piste possible plus tard : une macro qui PRÉSÉLECTIONNE un show
  (le rapport attend l'humain), ou un état lisible « dernier show chargé » (déjà retenu en base : `last_loaded_*`).
- Chaque chargement est journalisé (`alert.deploy.plugin_show_loaded`, acteur déduit de la requête).

## Plan des onglets : extension

Un onglet **Shows** s'ajoute après Scènes (visible par tous, en lecture seule sans `console.build`). Il ne rentrait
pas dans Scènes : ce n'est pas un réglage du rappel en direct, c'est l'inverse (hors antenne).

## Reste à faire

- AFV : les sources `mixer` (`role:` / `uuid:`) d'un show venu d'ailleurs ne sont pas vérifiées contre ce plateau.
- Plugins LV2 insérés absents du nœud cible : la validation à blanc ne lance pas les hôtes ; l'insert passera sec.
- Aide du plugin (`help*.md`) : ni scènes ni shows n'y sont encore décrits.

---

# Bibliothèques et affichage compact des mémoires (décisions du 2026-10-17)

Constat de l'exploitant : le panneau Mémoires de l'outil « prend bien trop de place ; si y'en a 20, ça va être un
enfer ».

- **Dans l'outil (popup / fiche) : compact.** Une rangée de pastilles — les FAVORIS d'abord, puis les dernières
  utilisées (6 à 8 au plus) — un toucher = rappel. Un bouton « Toutes… » ouvre un popup complet (recherche, infos,
  gestion, appliquer sur plusieurs tranches, enregistrer).
- **Réglages › Bibliothèques** : une page qui gère TOUTES les bibliothèques de la console :
  mémoires d'outils, scènes, shows, répartitions et dispositions de rangées.
- **Classement** : favoris (étoile PAR UTILISATEUR, remontés en tête partout), étiquettes libres, dossiers (un par
  mémoire), tri (nom, date, auteur, dernière utilisation, nombre d'utilisations).
- **Partage** : une mémoire personnelle se partage À TOUS (elle passe en commune — droit console.build) ou à des
  utilisateurs / groupes précis, qui la voient dans « Partagées avec moi ».
- Export / import depuis cette page (les mécanismes existants : préréglages, shows, répartitions).

## Construit le 2026-10-17

Code : `app/console_library.py` (classement), `app/routes/console_library_api.py` (routes), partage et export / import
des mémoires dans `app/console_tools.py` ; sections `TOOL MEMORIES` (rangée compacte, popup « Toutes… ») et `LIBRARIES`
(onglet Bibliothèques) de `control.js`. Garde : `tests/verif_bibliotheques.py` ; `tests/verif_console_memoires.py`
couvre l'affichage compact.

- **Le contenu ne bouge pas.** L'orchestrateur ne garde que le CLASSEMENT, dans trois tables génériques clés par
  `(genre, réf)` : `library_meta` (dossier, étiquettes, compteur, dernier rappel), `library_favorites` (étoile PAR
  UTILISATEUR), `library_shares` (partages ciblés). La réf est l'identifiant stable de chaque bibliothèque : ligne
  `plugin_store` (mémoire ; garde son id au renommage, au remplacement et au passage en commune), ligne `plugin_shows`
  (show), `(instance de la console, id de scène)`, `(portée, propriétaire, id)` pour répartitions et dispositions.
  Des tables plutôt qu'une clé de réglage : favoris et partages se cherchent PAR UTILISATEUR, et le compteur s'écrit à
  chaque rappel depuis plusieurs fils (une instruction indexée, pas un relire-modifier-écrire d'un JSON).
- **Compteur au rappel**, côté orchestrateur : mémoire (route de rappel hors aperçu, macro), scène (proxy ET macros,
  déclaré par `control.library.usage` du manifeste : l'id rendu par la console est compté, `suivante` / `précédente`
  comprises), show (chargement réussi), disposition et répartition (choisies à l'écran, `POST …/use`).
- **Partage ciblé : mémoires d'outils seulement.** Une scène appartient à la console (tous ses opérateurs la voient),
  un show à l'installation, une répartition a déjà son partage (admin, `for_user`). « À tous » = passage en commune
  (console.build), retour possible vers son auteur (console.build). Le destinataire rappelle et met en favori, ne
  renomme, ne supprime, ne classe ni ne re-partage.
- **Export / import** : mémoires d'outils (fichier JSON, dossier et étiquettes compris) ; shows par l'onglet Shows ;
  scènes avec le show ; répartitions et dispositions : pas d'export (aucun mécanisme existant).

Reste à faire : la rangée compacte compte les rappels de TOUS (dernier rappel global, pas « mes » dernières) ; les
scènes supprimées dans la console laissent une ligne de classement orpheline (jamais montrée) ; le pont Ember+ /
les surfaces qui rappellent une scène par un autre chemin que le proxy ou les macros ne sont pas comptés.
