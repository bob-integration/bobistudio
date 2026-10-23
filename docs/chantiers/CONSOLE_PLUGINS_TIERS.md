# Console audio : accueil de plugins audio tiers (VST3 / LV2 / CLAP) — étude de conception

Date : 2026-10-16. Statut : ÉTUDE, aucun code. Demandée par l'exploitant « pour plus tard ».
Périmètre lu : `plugins/audio_console` (`src/engine.py`, `src/console_model.py`, `src/runtime.py`, `modules.json`).

## 0. Résumé des recommandations

| Sujet | Recommandation |
|---|---|
| Architecture | **RÉVISÉ le 2026-10-17 après banc** ([`CONSOLE_PLUGINS_TIERS_BANC0.md`](CONSOLE_PLUGINS_TIERS_BANC0.md)) : plugin tiers dans un **co-processus synchrone** du conteneur de la console (mémoire partagée + futex + échéance ; hôte en retard, bloqué ou mort → bloc SEC, relance). Exige un opcode C dans le plan. L'option initiale ci-dessous (conteneur dédié `plugin_host` branché par l'`insert` sur flux MXL) reste la voie des équipements externes. |
| Hôte | Petit hôte **C++ maison** (RT-safe, sans Python dans le chemin audio) qui charge LV2, CLAP puis VST3. Carla (headless) sert au **prototype** seulement. |
| Formats | Ordre : **LV2 + CLAP** d'abord (licences simples, plugins libres abondants), **VST3** ensuite (SDK désormais MIT). LADSPA : non. |
| Licences | Aucun obstacle côté SDK. Le point de droit est ailleurs : plugin propriétaire + GPL → l'isolation par processus est aussi la meilleure position juridique. |
| Latence | Co-processus (mesuré, banc 0) : **0 bloc ajouté**, transport ≈ 10 µs (p99 ≈ 33 µs) → **latence propre du plugin seule**. Voie par flux (estimée) : `bloc + marge + ext` ≈ **13 ms + latence du plugin**. |
| GUI | Jamais la GUI native. Liste générique de paramètres exposée par l'hôte, `param_tree` dynamique pour les macros, état opaque sauvegardé dans le document. |
| Distribution | Les plugins vivent dans un **volume de nœud en lecture seule**, géré par la page Catalogue/Réglages ; l'image `plugin_host` ne contient aucun plugin tiers. |

## 1. Formats et licences

### 1.1 État des formats (sources en fin de document)

- **VST3** — le SDK VST 3.8 (changelog : 2025-10-20 ; communiqué Steinberg : 2025-10-29) est passé à la
  **licence MIT**, en remplacement du double régime antérieur (propriétaire Steinberg / GPLv3). Ce point
  est CONFIRMÉ par plusieurs sources de presse et le communiqué ; je n'ai pas lu moi-même le fichier
  `LICENSE.txt` du dépôt : **à vérifier sur l'étiquette `v3.8.0` avant d'intégrer** (et à ne pas confondre
  avec le SDK **ASIO**, séparément passé en GPLv3 optionnel). MIT est compatible GPL-3.0-or-later (on peut
  l'incorporer dans un binaire GPL) ; plus besoin de la « Steinberg VST3 usage agreement » ni de la
  clause GPLv3 de l'ancien SDK. 3.8 ajoute MIDI 2.0 et un aperçu Wayland (inutile pour nous).
- **LV2** — spécification sous **licence ISC** (permissive, compatible GPL) ; version courante 1.18.x.
  Bibliothèque d'hôte : `lilv`/`serd`/`sord` (ISC). Source de vérité du format sur Linux.
- **CLAP** — **MIT** ; en-têtes seuls (C), aucun SDK à lier. Spécification plus récente (u-he/Bitwig, 2022),
  modèle de threads explicite (thread principal / thread audio), état et paramètres bien définis.
  Le catalogue Linux est plus court que LV2 mais croît vite (LSP, DISTRHO, u-he…).
- **LADSPA** — domaine public/LGPL selon les en-têtes, mais **sans état, sans latence déclarée normalisée,
  sans paramètres typés riches** : trop pauvre pour une console (on ne pourrait ni compenser ni
  sauvegarder proprement). **Recommandation : ne pas le supporter** (les plugins utiles ont une version LV2).

### 1.2 Compatibilité avec GPL-3.0-or-later

- Nos dépendances d'hôte (MIT, ISC) sont **compatibles** avec la GPL : aucune friction dans l'image.
- Le vrai sujet n'est pas le format, c'est **le plugin lui-même**. Un plugin GPL est sans problème. Un
  plugin **propriétaire** chargé par `dlopen` dans un processus GPL est la zone grise classique : la
  position de la FSF est que « liaison dynamique + structures partagées = un seul programme », ce que
  des développeurs audio contestent (le même format est implémenté par des hôtes de licences variées) ;
  **aucune décision de justice ne tranche**. Ce n'est pas un avis juridique.
- **Recommandation** : le plugin tiers s'exécute dans un **processus séparé** (hôte d'un conteneur dédié)
  qui ne partage avec le moteur que des flux audio sur le bus MXL et du JSON de contrôle. C'est le
  même régime que n'importe quel équipement externe branché sur un insert : le moteur GPL ne charge
  jamais de code tiers. L'hôte de plugins lui-même est GPL (ou MIT) ; ce qu'il charge est une
  « aggregation » de fait. On **ne redistribue jamais** un plugin propriétaire avec Bobi.Studio
  (l'exploitant le fournit — cf. §5), ce qui évite aussi la clause de redistribution.
- Un paquet de plugins libres qu'on voudrait livrer au catalogue : vérifier plugin par plugin que la
  licence autorise la redistribution (noise-repellent est LGPL-3.0-or-later ; x42 est GPL ; LSP est **LGPL-3+** d'après le `copyright` Debian — corrigé au banc 0).

### 1.3 Plugins propriétaires : activation, iLok

- **iLok / PACE** : aucune source consultée n'atteste d'un support natif Linux ; les témoignages parlent
  de Wine, avec des résultats variables. **Hypothèse de travail : les plugins sous iLok ne sont pas
  utilisables sur nœud Linux** ; ne pas promettre.
- Les plugins propriétaires natifs Linux existent mais sont rares et leurs schémas d'activation varient
  (fichier de licence liée à la machine, compte en ligne, gestionnaire propre). Un conteneur sans accès
  sortant (§4.4) rend l'**activation en ligne impossible** : prévoir l'**activation hors ligne** (fichier
  déposé dans le volume de licences) et une empreinte machine **stable** au recreate d'un conteneur
  (volume de nœud, pas le rootfs éphémère — cf. mémoire « container-rootfs-ephemeral-state-volume »).
- Pas de pont Windows (yabridge/Wine) : surface, latence et stabilité hors maîtrise. Écarté du périmètre.

### 1.4 Disponibilité réelle de plugins Linux utiles (diffusion)

Les résultats web sont partiels ; l'inventaire suivant est à confirmer sur banc d'essai.
- **Loudness / mesure** : x42 `meters.lv2` (EBU R128, true-peak, scopes), `libebur128` (déjà une brique
  courante). NB : la console a déjà un R128 intégré — un plugin tiers de mesure n'apporte rien.
- **Dynamique** : LSP Plugins (compresseurs, multibande, expandeurs, portes, limiteurs, sidechain ;
  LV2, CLAP, VST3, LADSPA), x42 (`darc`, `dpl` limiteur, `fil4` égaliseur), Calf, Zam.
- **Réduction de bruit** : `noise-repellent` (LV2, LGPL-3.0+, spectral ; **≈ 23 ms de latence** annoncée
  à 44,1 kHz) — à signaler à l'exploitant : c'est typiquement un traitement qui coûte plus que
  l'insert lui-même.
- **De-esser, égalisation dynamique** : NON confirmés par mes recherches pour le catalogue libre
  (LSP : pas de de-esser nommé dans les résultats). C'est la **question de valeur n°1** : les produits
  de référence sont propriétaires et en majorité Windows/macOS. À faire vérifier par l'exploitant
  avant de dimensionner l'effort (§6).

## 2. Architecture d'accueil

> **2026-10-17 — banc de faisabilité fait : voir [`CONSOLE_PLUGINS_TIERS_BANC0.md`](CONSOLE_PLUGINS_TIERS_BANC0.md).**
> L'exploitant a jugé « beaucoup » les ≈ 13 ms de l'option B. Une option **D — co-processus synchrone dans
> le conteneur de la console** a été mesurée : le moteur dépose le bloc en mémoire partagée, réveille
> l'hôte (futex) et l'attend jusqu'à une échéance ; dépassement, blocage ou plantage → le bloc passe SEC à
> latence inchangée, l'hôte est relancé. Mesuré sur 36 000 blocs par cas : **0 bloc de latence ajouté,
> transport ≈ 10 µs (p99 ≈ 33 µs) sur le même cœur**, `kill -9` = 6–14 blocs secs et 0 retard du moteur,
> relance 34–66 ms. Elle **remplace l'option B pour les plugins tiers** ; ses contreparties : un opcode C
> dans le plan (ABI 7), et l'isolation du plugin à construire DANS le conteneur de la console (§4.4
> ci-dessous reste le cahier des charges, en plus difficile). Les §2.2 à §2.5 sont conservés comme
> historique de l'option B, qui reste celle des équipements externes.

### 2.1 Ce que le moteur impose

- Bloc par défaut **240 échantillons à 48 kHz = 5 ms** (`modules.json:console.block_samples`), marge de
  lecture 144 ech. = 3 ms. Latence d'un insert = `B + M + ext_latency_ms` (`console_model.module_latency_samples`,
  règle `insert`), compensée par les groupes de calage (`latency_plan`) — **mécanisme déjà complet**.
- Plan compilé en lignes de 96 octets (`OP_DTYPE`, `PLAN_ABI = 6`, 21 `OP_*`) exécuté par le noyau C
  (`audio_dsp.so`). Aucun `OP_*` n'appelle de code externe ; ajouter un opcode « appelle ce pointeur de
  fonction » serait un changement d'ABI.
- Le fil audio est du Python : `gc.collect(); gc.freeze(); gc.disable()` (`runtime.py`) a été
  nécessaire après un blocage de 130 ms par le GC (mémoire « gc-python-bloque-fil-audio-130ms »). Le
  fil audio ne doit donc ni allouer, ni attendre.
- L'insert fait déjà du **secours automatique** : retour absent ou `on=false` → le sec passe, à la même
  latence, jamais de silence (`_insert_ops`, `_bypass_ops`, compteur `missing_blocks`).

### 2.2 Options

| | A. Dans le processus du moteur | B. Hôte séparé sur l'insert (conteneur) | C. Hôte existant (Carla, mod-host, Ardour) |
|---|---|---|---|
| Plantage plugin | **tue la console** (SIGSEGV dans le fil audio) | tue l'hôte seul ; l'insert retombe sur le sec | idem B si lancé en conteneur à part |
| Blocage / RT | un plugin qui alloue ou verrouille retarde le bloc ; Python+GIL+GC | isolé : au pire, blocs manquants sur CET insert | variable selon l'hôte |
| Latence | 0 de plus | +1 bloc hôte (voir §2.4) | +1 bloc, souvent plus (JACK/PipeWire) |
| Sécurité | code tiers avec les droits du moteur et ses secrets | sandbox complète possible | selon montage |
| Licence | le moteur GPL charge du code propriétaire | séparation nette | idem B |
| Changement dans la console | ABI du plan, nouveau OP, gestion RT | **aucun** (insert existant) | aucun |
| Coût | 1 cœur partagé avec le moteur | 1 conteneur + RAM + un cœur | idem + dépendance lourde |

**A est écartée** : elle réunit les trois risques que le projet a déjà payés (GC, GIL, tuer l'antenne).

**C (Carla/mod-host/Ardour)** : Carla est GPL, charge LADSPA/DSSI/LV2/VST2/VST3/AU, pilotable par OSC, a un
mode sans interface ; son support CLAP était annoncé « expérimental » pour la 2.6 (je n'ai pas pu en
confirmer l'état final) et ses ponts (bridge) sont décrits comme expérimentaux. Elle suppose JACK ou
PipeWire (une pile de plus à faire tenir dans un conteneur, sans garantie temps réel). `mod-host` est
LV2 seul. Ardour/LV2 headless est un DAW entier. **Recommandation** : s'en servir pour **valider le
principe en phase 0**, mais ne pas en dépendre en production.

**Recommandation : option B avec un hôte C++ maison** (`plugin_host`), car :
1. l'hôte est petit (charger, `activate`, boucle `process`, paramètres, état, latence) ;
2. il parle MXL directement (lecteur/écrivain audio), sans JACK ;
3. on maîtrise son comportement RT (pas d'allocation, cœur épinglé, mlock) ;
4. une seule base de code pour LV2 (`lilv`), CLAP (en-têtes) et VST3 (SDK MIT).

### 2.3 Schéma

```
console (moteur)                                plugin_host (conteneur, 1 par chaîne)
  tranche ── envoi insert "<sid>/<eid>" ──MXL──▶ lit flux  ─▶ [plugin 1 → plugin 2 …] ─▶ écrit flux
  tranche ◀── retour (prise `return.flow`) ◀──MXL────────────────────────────────────────┘
  contrôle JSON  ◀──────────── proxy générique /api/containers/<vmid>/plugin/… ───────────▶ hôte
```
- L'**envoi** est le signal `<sid>/<eid>` publié par le patch de sortie (déjà existant), le **retour**
  est une prise d'entrée (`return:{kind:flow, src, channels}`). Aucune modification du modèle de
  document pour l'audio ; l'exploitant câble sur la page Câbles comme pour un équipement externe.
- Un hôte peut enchaîner plusieurs plugins (chaîne) ou un conteneur par plugin : choix en §2.5.
- L'hôte déclare sa latence totale (somme des latences des plugins + son bloc) ; l'orchestrateur la
  **recopie dans `ext_latency_ms`** de l'insert (voir §4.1) pour que la compensation reste exacte.

### 2.4 Latence ajoutée (estimation de l'option B ; option D MESURÉE : 0 bloc, cf. banc 0)

Hypothèses : bloc console 5 ms, marge de lecture 3 ms, hôte traitant par blocs de 5 ms (aligné sur le
bloc du flux), même nœud (mémoire partagée, pas de réseau).

- Part déjà due à tout insert (mécanisme actuel) : `B + M` = **8 ms**.
- Part due à l'hôte : lecture d'un bloc complet puis écriture = **≈ 5 ms** (une période). Si le flux
  d'envoi est consommé par tranches (mode tranche MXL), on peut descendre vers **2–3 ms**, au prix d'un
  hôte qui traite des sous-blocs — plusieurs plugins exigent des tailles de bloc fixes/minimales :
  **ne pas promettre**.
- Total d'un insert avec hôte : **≈ 13 ms + latence propre du plugin** (limiteur look-ahead 1–5 ms ;
  réduction de bruit spectrale ≈ 20+ ms ; EQ en phase linéaire : 10–50 ms ou plus).
- Un chemin tranche → insert → bus paie ces 13 ms ; les groupes de calage évitent que les autres
  groupes (une source externe, un retour studio) les paient. **La compensation est exacte seulement si
  `ext_latency_ms` est juste** : à valider par la méthode de mesure par timecode incrusté (mémoire
  « latence-mesure-par-timecode-incruste »), pas par la déclaration du plugin seule.
- Variation (gigue) : un hôte qui rate un bloc fait basculer l'insert sur le sec avec fondu — pas de
  silence, mais un son sans traitement : **alarme obligatoire** (`missing_blocks` > 0 → alerte,
  comparer à l'intention : l'insert est « actif » et ne reçoit plus de retour).
- Si l'exploitant exige < 8 ms bout en bout sur un chemin donné : un plugin tiers n'y entrera pas ;
  c'est la raison de garder les modules internes (C) pour les traitements de tous les jours.

### 2.5 Granularité : un conteneur par insert ou par chaîne

**Recommandation : un conteneur par insert (par chaîne de plugins de cet insert).** L'unité de panne est
l'insert (un plugin qui plante n'en emporte pas d'autres), le profil de ressources est simple, et le
cycle de vie suit l'emplacement. Coût : quelques dizaines de Mo de RAM par hôte (hors plugin). Une
mutualisation « un hôte pour N inserts » est une optimisation ultérieure, après mesure.

## 3. Interfaces, paramètres, présélections, état

### 3.1 Pas de GUI native

Une GUI VST3/CLAP/LV2 native (X11, Wayland, parfois GL) est **hors de portée** du navigateur et de
l'iPad (référence tactile du projet). **Recommandation : ne jamais l'afficher.** On expose ce que
chaque format donne sans GUI :
- **Paramètres** : VST3 `IEditController`, CLAP `clap_plugin_params`, LV2 ports de contrôle +
  `lv2:Parameter` : l'hôte les énumère en `{id stable, nom, groupe, unité, min/max/défaut, pas,
  discret/continu, automatisable, valeur affichée en texte}`.
- Interface : une **fiche générique** (curseurs, interrupteurs, listes, valeurs textuelles) dans la
  fiche de tranche, fenêtre détachable (la fiche décrochable existe depuis 0.4.31). Plugins à
  centaines de paramètres : recherche + groupes + « favoris » (sous-ensemble choisi par l'exploitant).
- **Limites assumées** : les plugins dont l'intérêt est visuel (courbe d'EQ interactive, sidechain
  filtré à main levée, spectre) y perdent beaucoup. À dire dans l'aide.

### 3.2 Macros (OBLIGATOIRE dans le projet)

- Paramètres continus → `param_tree` : l'arbre est **dynamique** (dépend du plugin chargé), donc fourni
  par l'hôte via `/state.caps` comme le fait `split` (« caps live ») et non par `modules.json`.
  Chaque feuille porte les bornes du plugin et un chemin stable `plugin.<pid>.<param_id>`.
- Actions discrètes → `actions[]` : « charger un plugin », « rappeler une présélection »
  (`options_endpoint` = liste vivante), « bypass », « réinitialiser ».
- État lisible → `/state` + `control.read_endpoints` : plugin chargé, latence déclarée, bloc manquants,
  charge CPU de l'hôte (en % du bloc), `crashed`.
- **Identifiant stable** : l'identifiant de paramètre doit survivre à une mise à jour du plugin
  (VST3 `ParamID`, CLAP `param id`, LV2 symbole du port) — ne jamais indexer par position. Un
  paramètre disparu fait échouer la macro à voix haute, pas en silence.

### 3.3 Présélections et sauvegarde d'état

- Chaque format a un **état opaque** (VST3 `IComponent::getState`/`IEditController::setState`, CLAP
  `clap_plugin_state`, LV2 `state:interface`). **Recommandation** : le document de la console stocke,
  par insert, `plugin: {format, uri_ou_uid, version, state_b64, state_sha256, params:{id:valeur}}`.
  - l'état opaque est la vérité (il contient ce que les paramètres ne disent pas : fichiers de
    réponse impulsionnelle, profil de bruit, modèle) ;
  - les valeurs de paramètres servent à l'affichage, aux macros et au diff d'une version à l'autre.
- Taille : l'état d'un plugin peut atteindre des Mo (profil de bruit, IR). Seuil proposé : 1 Mo dans le
  document ; au-delà, **blob dans un stockage nommé du nœud** référencé par hash (pas dans le JSON du
  document, qui est rejoué et diffé en direct). Question ouverte n°3.
- Les **projets** (snapshot/restore) embarquent l'état ; une restauration sur un nœud sans le plugin
  produit un insert « plugin manquant » (voir §5), jamais un échec de chargement du projet.
- Présélections propres au plugin (usine) : listées par l'hôte (VST3 `IUnitInfo`, CLAP
  `preset-discovery`, LV2 `pset:Preset`), rappelées par action. Les présélections de l'exploitant se
  rangent avec les présélections de la console (mécanisme d'échange déjà présent en cours de
  chantier) plutôt que dans un format parallèle.
- **Mise à jour d'un plugin** : un état écrit par la version N peut ne pas se charger en N+1 :
  l'hôte renvoie `state_load_failed`, l'insert reste sur le sec et alerte ; on ne remplace jamais
  l'état stocké par celui d'un chargement raté.

## 4. Latence, CPU, sécurité

### 4.1 Latence et compensation

- Le plugin déclare sa latence (VST3 `getLatencySamples`, CLAP `clap_plugin_latency`, LV2
  `lv2:reportsLatency`). Elle peut **changer à chaud** (ex. mode « qualité »). L'hôte la publie sur
  `/state` ; l'orchestrateur l'applique à `ext_latency_ms`.
- **Un changement de latence à chaud ne doit pas être appliqué en silence** : il décale la phase de la
  tranche vis-à-vis de son groupe. Politique proposée : pas de recalage automatique à l'antenne ;
  l'alarme « latence déclarée ≠ latence compensée » s'affiche, l'exploitant recale à son heure
  (cohérent avec la décision « jamais de bascule automatique » sur les entrées A/B).
- `ext_latency_ms` est un réel en ms alors que les plugins parlent en échantillons : l'arrondi à
  l'échantillon dans `module_latency_samples` est suffisant à 48 kHz ; l'hôte doit être à 48 kHz (pas de
  rééchantillonnage caché).

### 4.2 CPU

- Profil de ressources par type (`core_pool`) : nouveau type `plugin_host`, **cœur isolé épinglé**
  (comme les types temps réel), fréquence épinglée (mémoire « isolated-cores-must-pin-frequency »),
  `SCHED_FIFO` bas ou `SCHED_RR` si le nœud l'autorise.
- Le coût d'un plugin est **inconnu a priori** (un convolueur ou un NR spectral peuvent prendre un
  cœur entier) : l'hôte mesure son temps de traitement par bloc (moyenne, max, % du bloc) et publie
  `load_pct` ; au-delà de ~70 % du bloc, alerte. L'allocation par type suppose un profil moyen —
  pour ce type, **allouer un cœur entier par hôte** par défaut (mémoire « cpu-consumption-must-be-
  measured »).
- Plugins multi-fil (NR, convolution) : fixer le nombre de fils dans l'hôte, pas dans le plugin.

### 4.3 Robustesse

- Hôte à deux processus : **superviseur** (parle MXL et contrôle) + **fils de travail** (le plugin) ;
  si le plugin plante (SIGSEGV), le superviseur le relance, l'insert est en `crashed`, le sec passe.
  Limite de relances (3 en 60 s) puis arrêt avec alerte.
- Aucune allocation ni verrou dans le chemin audio de l'hôte : pré-allouer les tampons ; `mlockall`.
- **Analyse des plugins** (scan) faite dans un sous-processus jetable avec délai : un plugin qui plante
  au chargement ne doit pas rendre la liste vide.

### 4.4 Sécurité (code tiers arbitraire)

Un plugin est du code natif arbitraire. Posture proposée, par défaut :
- conteneur **sans accès réseau sortant** (seul le segment de contrôle vers l'orchestrateur), pas de
  route par défaut ; une activation en ligne est donc impossible par construction (voir §1.3) ;
- utilisateur non privilégié, `no-new-privileges`, toutes capacités retirées, **rootfs en lecture
  seule**, `tmpfs` pour le travail, profil **seccomp** restrictif (pas de `ptrace`, `mount`,
  `bpf`, `perf_event_open`, `kexec`, création de sockets sauf locaux) ;
- **aucun secret dans l'environnement visible du plugin** : le conteneur reçoit `MXL_AGENT_TOKEN`
  (CLAUDE.md) ; l'hôte le lit puis l'efface et exécute le plugin dans un fil/processus enfant à
  l'environnement nettoyé, d'un autre uid ; aucun accès aux clés de nœud ni aux certificats ;
- volume de plugins monté en **lecture seule** ; volume d'état/licences séparé, par instance ;
- limites cgroup : mémoire, nombre de processus, fichiers ouverts (mémoire « reconnexion-sans-palier »,
  « inotify-instances-cap » : un plugin qui fuit ne doit pas affamer le nœud) ;
- accès MXL : le domaine `/dev/shm/mxl` est partagé ; **le plugin ne doit pas le voir** (il le
  lirait en entier, tous flux confondus). Seul le superviseur le monte ; le plugin reçoit ses tampons
  par un canal local (mémoire partagée privée). C'est la principale difficulté d'isolation (§6, risque R2).
- Un plugin tiers reste du code à haut privilège de fait sur le signal d'antenne : mention explicite
  dans l'interface (« code non vérifié ») et droit d'installation distinct (`console.build` ou pire).

## 5. Distribution et installation

- **Modèle** : l'image `plugin_host` ne contient **aucun plugin tiers** (taille, licence, mises à jour).
  Les plugins vivent dans un **volume de nœud** `/var/lib/bobistudio/audio-plugins/<format>/<id>/…`
  monté en lecture seule dans le conteneur.
- **Installation** : paquet `.mxlplugin`-like dédié, ou dépôt manuel par l'exploitant dans le dossier
  surveillé, via la page Réglages (téléversement). Les plugins LIBRES recommandés (LSP, x42, noise-
  repellent…) peuvent être proposés par la page **Catalogue** comme paquets installables (hors ligne
  exportables, comme `tts`), à condition que leur licence autorise la redistribution.
  **Un plugin propriétaire est toujours fourni par l'exploitant**, jamais par Bobi.Studio.
- **Sur quels nœuds** : ils doivent exister sur **chaque nœud pouvant héberger l'hôte**. Table de
  l'orchestrateur `node_audio_plugins(node_id, uid, format, version, sha256)` alimentée par le
  scan de l'hôte (jamais saisie à la main). Le choix du nœud d'un insert filtre sur « possède ce
  plugin à cette version ». Un plugin absent après migration/restauration : insert `missing_plugin`,
  sec, alerte (jamais une erreur silencieuse).
- **Empreinte** : `sha256` du binaire dans l'état sauvegardé ; version différente → avertissement avant
  chargement de l'état.
- **Licences d'activation** : volume séparé `…/licenses/<plugin>/`, sauvegardé avec le nœud ; jamais
  dans le document de console ni les projets.
- Les plugins sont de l'architecture x86-64 ; les nœuds Sandy Bridge sans AVX2 sont déjà inutilisables
  pour libmxl, donc non concernés.

## 6. Plan en étapes, risques, questions

### 6.1 Étapes livrables

0. **Banc de faisabilité — FAIT le 2026-10-17** ([`CONSOLE_PLUGINS_TIERS_BANC0.md`](CONSOLE_PLUGINS_TIERS_BANC0.md), co-processus synchrone mesuré ; reste la mesure par timecode dans une vraie console). Libellé d'origine : **Banc de faisabilité (1–2 j de travail)** — Carla ou un `lv2host` minimal en conteneur, un LSP
   compresseur LV2 sur un insert d'une console de test ; mesurer par timecode incrusté : latence totale,
   gigue, `missing_blocks`, CPU. Livrable : chiffres réels remplaçant le §2.4. **Décision d'aller / ne
   pas aller** prise ICI.
1. **`plugin_host` v0 — LV2 seul** : conteneur, MXL in/out audio, chargement d'UN plugin, paramètres
   génériques, `/state` (latence, charge, crash), profil de ressources, supervision/relance. Aucune
   modification de la console sauf une action « associer un hôte à cet insert » (recopie de la latence).
2. **Fiche générique + macros** : `caps` dynamiques → `param_tree` ; `actions[]` (charger, bypass,
   présélection) ; test de macro comme `split`. i18n FR/EN.
3. **État dans le document et les projets** : `state_b64`, hash, plugin manquant, mise à jour de
   version ; tests de restauration sur nœud sans le plugin.
4. **Distribution** : volume, scan, table `node_audio_plugins`, page de gestion, paquets du Catalogue
   (libres uniquement).
5. **Durcissement** : seccomp, uid séparé, MXL invisible du plugin, limites cgroup, alarmes
   (latence déclarée ≠ compensée, `crashed`, `missing_blocks` avec insert actif).
6. **CLAP** puis **VST3** (SDK MIT, vérifier la licence au tag) ; Carla/autres : jamais en production.
7. **Optimisations** (à décider après mesure) : traitement par tranches, mutualisation d'hôtes,
   bascule à chaud de latence assistée.

Chaque étape est livrable seule et testable hors ligne côté orchestrateur (garde-fous de macros,
modèle de document) ; seul l'étage 0 et 5 exigent un système vivant (`tools/bancs/`).

### 6.2 Risques

- **R1 — Valeur** : les de-esser/EQ dynamiques de référence sont propriétaires, souvent absents de
  Linux ; l'effort peut produire une capacité peu utilisée. Atténuation : étape 0 + inventaire de
  l'exploitant avant l'étape 1.
- **R2 — Isolation MXL** : un plugin dans le même conteneur que le lecteur MXL pourrait lire d'autres flux
  (le domaine `/dev/shm/mxl` est commun). Atténuation : deux processus, uid distincts, montages
  distincts, tampons passés par `memfd`/socketpair.
- **R3 — Latence plus forte que prévu** (§2.4) : le coût d'un bloc d'hôte est une estimation.
- **R4 — Plugins non RT-safe** (allocation dans `process`) → blocs manquants intermittents, donc
  « de temps en temps » en production. Atténuation : mesurer `max` du temps de traitement par bloc,
  pas la moyenne (leçon de la mémoire « bimodal-cost-types »).
- **R5 — Juridique** : plugin propriétaire + GPL (§1.2). L'isolation par processus est la meilleure
  réponse technique, pas une garantie. À faire relire avant toute distribution à des tiers.
- **R6 — Alarme muette** : un hôte arrêté, l'insert servant le sec, sans que personne ne le voie. Toute
  alarme doit comparer l'état à l'**intention** (insert actif vs retour présent), cf. mémoire
  « alarm-must-compare-to-intent » ; et jamais d'échec silencieux.
- **R7 — Dépendance à l'état opaque** : un état propriétaire qu'on ne sait pas relire est un état perdu
  (sauvegarder aussi les paramètres lisibles).

### 6.3 Questions ouvertes (précises)

1. Quels traitements l'exploitant attend-il en priorité (de-esser ? EQ dynamique ? NR ?) et leur
   disponibilité sous Linux est-elle acquise (version native, activation hors ligne possible) ?
2. Budget de latence acceptable sur un chemin avec plugin tiers : 13 ms + latence du plugin (§2.4) est-il
   admissible, ou le besoin se limite-t-il aux retours/enregistrements ?
3. Stockage des états volumineux : seuil dans le document (1 Mo proposé) et emplacement des blobs
   au-delà, compte tenu de l'export/import des projets.
4. Accepte-t-on qu'un plugin tiers soit non vérifié mais installé par un utilisateur disposant du droit
   `console.build`, ou faut-il un droit dédié (« installer du code ») ?

## Sources consultées

- Steinberg VST 3 sous licence MIT : [heise.de](https://heise.de/-10963451), [Sonic State, 2025-10-30](https://sonicstate.com/news/2025/10/30/vst-3-now-available-under-mit-license), [Rekkerd](https://rekkerd.org/vst-3-now-available-under-mit-license), [forum Renoise](https://forum.renoise.com/t/steinberg-vst3-asio-are-now-open-source/78840), [communiqué Steinberg 2025-10-29 (PDF)](https://ocl-steinberg-live.steinberg.net/_storage/asset/819253/storage/master/Press%20Release%20-%202025-10-29%20-%20VST%203.8%20-%20EN.pdf), [forum Steinberg, VST 3.8.0 SDK](https://forums.steinberg.net/t/vst-3-8-0-sdk-released/1011988), [Synthtopia](https://www.synthtopia.com/?p=150952). Fichier `LICENSE.txt` du SDK NON lu directement.
- LV2 (ISC) : [Wikipedia LV2](https://en.wikipedia.org/wiki/LV2), [paquet Alpine](https://pkgs.alpinelinux.org/package/edge/community/x86/lv2), [paquet Fedora 43](https://pkgdex.org/fedora/43/x86_64/everything/lv2-1.18.10-3.fc43.rpm.html). Site lv2plug.in non consulté.
- CLAP (MIT) : [Wikipedia CLAP](https://en.wikipedia.org/wiki/CLever_Audio_Plug-in), [SlackBuilds clap](https://slackbuilds.org/repository/15.0/audio/clap/), [CDM](https://cdm.link/clap-is-a-new-open-source-plug-in-format-from-bitwig-u-he-do-we-need-it-and-who-will-use-it/).
- Carla : [dépôt falkTX/Carla](https://github.com/falkTX/Carla), [Ildaeil/CLAP dans Carla 2.6 (KXStudio)](https://git.kx.studio/KXStudio/Website/commit/976b88bf1c4d00f4d66c20c63502296937642abb), [Cardinal, état du support](https://git.kx.studio/DISTRHO/Cardinal/commit/68c059991923b235fe6b0a781f52ebfd05acf3e8).
- GPL et plugins : [LAD, « GPL and plugins »](https://lists.linuxaudio.org/archives/linux-audio-dev/2010-June/027293.html), [LWN 819101](https://lwn.net/Articles/819101). FAQ GPL de la FSF non lue directement.
- iLok sous Linux : [Ardour, VST avec iLok](https://discourse.ardour.org/t/vst-plug-ins-with-ilok-on-ubuntu-studio/82969), [Gearspace, plugins compatibles Linux](https://gearspace.com/threads/linux-compatible-hardware-software-plugins.1449241/), [KVR](https://www.kvraudio.com/forum/viewtopic.php?p=9175959). Témoignages, pas de document PACE.
- Plugins Linux : [LSP Plugins (paquet Debian)](https://packages.debian.org/sid/lsp-plugins), [LSP vst3 (paquet Debian)](https://packages-picconi.debian.org/en/sid/lsp-plugins-vst3), [x42-plugins](https://www.mankier.com/package/lv2-x42-plugins), [meters.lv2](https://librearts.org/2013/10/meters-lv2-plugins-released-to-provide-advanced-audio-level-meters/), [noise-repellent (Arch)](https://archlinux.org/packages/extra/x86_64/noise-repellent/), [noise-repellent suite](https://www.linuxlinks.com/noise-repellent-suite-lv2-plugins/), [libebur128](https://archlinux.org/packages/extra/x86_64/libebur128).
- Code lu : `plugins/audio_console/src/engine.py` (`PLAN_ABI`, `OP_*`, `_insert_ops`, `_bypass_ops`), `src/console_model.py` (`insert`, `module_latency_samples`, `latency_plan`), `src/runtime.py` (GC), `modules.json` (`console.block_samples`, `read_margin_samples`).

## Isolation sous Docker — mesurée le 2026-10-17 (dell-1, conteneur `bobi-compute:0.61`, profil seccomp PAR DÉFAUT de Docker, `--network none`)

| Brique | Résultat | Conséquence |
|---|---|---|
| Landlock | ✅ `landlock_create_ruleset` → ABI **6** (fichiers, TCP, signaux / sockets abstraites) | liste blanche de chemins en lecture (plugins LV2, bibliothèques) ; `/dev/shm/mxl` et le reste FERMÉS |
| Espaces de noms (`unshare` user / net / ns) | ❌ EPERM | pas de bac à sable par espaces de noms dans un conteneur ordinaire |
| seccomp (filtre BPF après `PR_SET_NO_NEW_PRIVS`) | ✅ installé ; `socket()` refusé (EACCES) | réseau, ptrace, process_vm_*, mount, bpf, keyctl… interdits par appel système |
| Changer d'utilisateur | ✅ (root dans le conteneur) | hôte sous un utilisateur dédié |

**Bac à sable retenu** (sans espaces de noms) : `setuid` vers un utilisateur dédié → `PR_SET_NO_NEW_PRIVS` → environnement VIDE (`execve` avec `LV2_PATH` seul : plus de `MXL_AGENT_TOKEN`) → Landlock (lecture seule des dossiers de plugins et des bibliothèques, rien d'autre ; signaux et sockets abstraites limités au processus) → seccomp (liste noire des appels dangereux, `socket` compris) → `RLIMIT` (mémoire, fichiers, processus). La mémoire d'échange audio est un `memfd` hérité par descripteur : Landlock ne restreint pas un descripteur déjà ouvert. L'hôte REFUSE de charger un plugin si une brique manque (noyau sans Landlock, filtre refusé) — et le dit (code d'erreur), jamais en silence.

## Avancement — 2026-10-17 (branche `feat/plugins-tiers` du sous-module audio_console)

| Étape | Fait | Preuve |
|---|---|---|
| Bac à sable (re-exec env vide, fds, rlimits, uid 64990, no_new_privs, Landlock, seccomp avec clone filtré : threads oui, processus non) | ✅ | `probe` : 15/15 tentatives refusées sur dell-1 et le contrôleur ; témoin `probe --control` : 13 réussissent sans bac à sable |
| Protocole moteur ↔ hôte (memfd scellé, futex, ports décrits puis valeurs du moteur, qualification, latence mesurée par impulsion) | ✅ | `host/verif_console_plugins_tiers.py`, muté ×3 |
| Chargeur LV2 (liblilv par dlopen APRÈS le bac à sable ; urid map/unmap, options, bounded/fixedBlockLength, isLive) | ✅ | eg-amp −6 dB = 0,5012, mda, fomp, swh ; `list` : 167 plugins, 154 utilisables |
| Échange temps réel en C (`dsp_plugin_run`, OP_PLUGIN, PLAN_ABI 9), superviseur (relance avec délai, erreur définitive jamais relancée) | ✅ | |
| Module `plugin` du moteur (uri, values, state, latency_samples, bypass) ; latence TENUE quand le plugin décroche | ✅ | `host/verif_console_plugins_moteur.py` : C = Python au bit, muté ×3 |
| API : `/plugins/catalog`, `/state.plugins` (latency_mismatch), `/plugins/save_state` | ✅ | |
| Image `bobi-compute:0.62` = liblilv seul, `/usr/local/lib/lv2` (plugins distribués par l'orchestrateur, décision 2026-10-17) | ✅ | build 1 min 25 s sur dl360-1 ; essai de bout en bout dans un conteneur 0.62 sur dell-1 : 3 vrais plugins traités, 6 couches posées |
| Distribution des plugins par l'orchestrateur (archive validée → `lv2_plugins/` → `<media>/.bobi/lv2` des nœuds → montage lecture seule) | ✅ | `tests/verif_lv2_plugins.py` ; ajout et retrait d'un plugin factice depuis l'onglet, copiés puis retirés des 4 nœuds |
| Interface : ajout dans la chaîne, panneau généré, Compenser, Enregistrer l'état (console 0.4.48) ; Réglages → Signal → Plugins audio, droit `audio_plugins.manage` | ✅ | iPad simulé |
| Macros : `/state.caps.plugin_ports` + `param_tree_id.map_caps` (une feuille par réglage, rien deviné instance éteinte) — console 0.4.49 | ✅ | `tests/verif_param_tree_id.py` (muté), `verif_console_plugins_moteur.py` |
| HA : la bibliothèque suit le standby (canal mTLS de réplication, à l'empreinte) | ✅ | `tests/verif_lv2_plugins.py` (muté) ; bascule réelle sur 2 machines NON éprouvée |
| Interface d'état LV2 (données internes, propriétés POD par URI) — console 0.4.50 | ✅ | `tests/verif_console_plugins_lv2_state.py` : vrai plugin LV2 compilé, chargé par liblilv (muté) |
| Latence > 1 s : décidé de NE PAS l'étendre — le document ne compense pas plus (latency_samples.max = 1 s) et une telle latence n'a rien à faire à l'antenne ; la fiche dit « ne peut pas être calé » (latency_too_long) ou « latence non vérifiée » (pas de réponse à l'impulsion, rien déclaré) — console 0.4.51 | ✅ | `verif_console_plugins_moteur.py` |

Pièges rencontrés (à ne pas retomber dedans) :
- `raise(SIGSEGV)` est AVALÉ une fois par le gestionnaire SIGSEGV du runtime Rust (détection de débordement de pile) : un plugin de test qui « plante » ainsi ne plante pas. Vraie faute mémoire.
- `RLIMIT_FSIZE=0` tue par SIGXFSZ un hôte dont la sortie standard est redirigée vers un FICHIER : le moteur lit l'hôte par un tube, jamais par un fichier.
- Un dossier LV2 illisible pour l'uid de l'hôte se présentait comme « plugin non installé » : le message nomme désormais les dossiers illisibles.
- Une latence de plugin n'est JAMAIS reportée automatiquement dans le document : `latency_mismatch` la signale, l'exploitant décide (saut de latence à l'antenne).
