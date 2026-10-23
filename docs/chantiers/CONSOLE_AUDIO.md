# Console audio modulaire — note de conception

> Rédigée le 2026-10-12 et mise au point le même jour avec l'exploitant, en quatre tours de
> questions. Statut : **conception validée dans ses grandes lignes, aucune ligne de code**. Les
> points encore ouverts sont au § 15.
>
> Maquettes d'interface : canevas https://claude.ai/artifact/A3S7e3T4knvDcpjvbtE2oX (privé).

## 1. Objectif et principes

Un plugin `audio_console` : une console de mixage audio sur le bus MXL dont la **structure est une
donnée**, pas du code. L'exploitant ajoute une tranche, un bus, un auxiliaire, un N-1, ou insère un
traitement là où il en a besoin, à chaud, sans couper les sorties existantes.

Principes, qui conditionnent tout le reste :

1. **Pas de graphe libre.** Le modèle est celui d'une console : tranches → bus → sorties, avec des
   chaînes de traitement libres à chaque étage. Les boucles, l'ordre de calcul variable et les
   latences imprévisibles d'un graphe libre coûtent cher pour un gain faible.
2. **Identifiants stables.** Tout élément porte un id attribué à la création, jamais réutilisé
   (§ 3.1). Tout ce qui l'adresse passe par cet id, jamais par sa position.
3. **Persister avant d'appliquer.** Le document de la console, persisté par l'orchestrateur, est la
   source de vérité — pas l'état du conteneur (§ 7).
4. **Aucun code de plugin dans l'orchestrateur.** La règle du système de plugins est maintenue :
   ce qui manque à l'orchestrateur s'ajoute sous forme de capacité GÉNÉRIQUE et DÉCLARATIVE,
   utile à tous les plugins (§ 7.1).
5. **Budget mesuré, pas supposé.** Une modification qui ferait craquer le son est refusée avant
   d'être appliquée (§ 14).
6. **Pas de console de référence à copier.** On part de ce qui est pertinent pour l'utilisateur.

## 2. Périmètre

| | V1 | Plus tard |
|---|---|---|
| Entrées | flux MXL audio, tranches mono ou stéréo, choix direct + vue XY ; générateur interne (1 kHz, bruit rose) | surround, liens de tranches (VCA, groupes de mute) |
| Bus | `group`, `master`, `aux`, `matrix`, `mixminus` (N-1), sortie MXL sur tout bus | bus 5.1 / 8 canaux |
| Sorties | sorties de bus, sorties directes par tranche, sorties N-1 | |
| Traitements | modules intégrés (§ 4.1) | modules hébergés LV2 / CLAP / VST3 Linux, insert externe (§ 4.2–4.3), automixer |
| Mesure | crête + RMS, réduction de gain, loudness EBU R128 sur les masters, corrélateur de phase sur les bus stéréo, charge CPU | |
| Écoute | écoute web PCM sans perte sur WebSocket (réseau local), sortie MXL d'écoute | solo / PFL avec bus d'écoute, écoute web WebRTC (Opus, audio seul) hors réseau local |
| Ordre (talkback) | vers les N-1, source micro MXL ou micro du navigateur | |
| Mémoires | rappel simple par id | isolation par élément, fondu entre mémoires |
| Exploitation | plusieurs opérateurs, zones, droits fins | droits des macros |
| Pilotage | interface + macros | surfaces physiques, HUI, Ember+ |
| Résilience | conçue pour le secours (§ 13) | console de secours sur un autre nœud |

## 3. Modèle de données

La console est UN document JSON, persisté dans `deploy_config.params.console`.

```json
{
  "version": 12,
  "block_samples": 240,
  "strips": [
    {"id": "s_k3f9", "label": "Micro plateau 1", "width": 1,
     "input": {"flow": "studio-mics", "channels": [0]},
     "chain": [
       {"id": "e_trim", "kind": "trim",   "params": {"gain_db": 2.0}},
       {"id": "m_a1",   "kind": "module", "type": "filter", "params": {"mode": "hpf", "freq": 80, "q": 0.707}},
       {"id": "m_a2",   "kind": "module", "type": "eq4",    "params": {"b1": {"freq": 200, "gain": -2, "q": 1.0}}},
       {"id": "m_a3",   "kind": "module", "type": "comp",   "params": {"threshold": -18, "ratio": 3, "attack_ms": 5, "release_ms": 120}},
       {"id": "e_mute", "kind": "mute",   "params": {"on": false}},
       {"id": "e_fad",  "kind": "fader",  "params": {"gain_db": -3.0}},
       {"id": "e_pan",  "kind": "pan",    "params": {"pos": 0.0}}
     ],
     "sends": [
       {"id": "x_1", "bus": "b_aux1", "after": "m_a3",  "level_db": -6.0},
       {"id": "x_2", "bus": "b_pgm",  "after": "e_pan", "level_db": 0.0},
       {"id": "x_3", "bus": "b_nm1",  "after": "e_pan", "level_db": 0.0}
     ],
     "direct_out": {"after": "m_a2", "chain": [{"id": "m_d1", "kind": "module", "type": "delay", "params": {"ms": 40}}],
                    "compensation": "minimal"},
     "mixminus_out": null}
  ],
  "buses": [
    {"id": "b_grp1", "kind": "group",    "label": "Micros",         "width": 2, "chain": [], "route_to": ["b_pgm"], "output": true},
    {"id": "b_pgm",  "kind": "master",   "label": "PGM",            "width": 2,
     "chain": [{"id": "m_l1", "kind": "module", "type": "limiter", "params": {"ceiling_db": -1.0}}], "output": true},
    {"id": "b_aux1", "kind": "aux",      "label": "Retour plateau", "width": 1, "chain": [], "output": true, "compensation": "minimal"},
    {"id": "b_mx1",  "kind": "matrix",   "label": "Enregistrement", "width": 2, "sources": [{"bus": "b_pgm", "level_db": 0}], "output": true},
    {"id": "b_nm1",  "kind": "mixminus", "label": "N-1 plateau",    "width": 2}
  ],
  "zones": [{"id": "z_plat", "label": "Plateau", "members": ["s_k3f9", "b_aux1"]}]
}
```

Le schéma exact se fixe au lot 1 ; l'exemple montre les notions, pas les noms de champ définitifs.

### 3.1 Identifiants stables

Chaque tranche, bus, élément de chaîne, départ et zone porte un `id` **attribué à la création et
jamais réutilisé** (aléatoire court, préfixé `s_` / `b_` / `e_` / `m_` / `x_` / `z_`).

Pourquoi : l'arbre de paramètres actuel (`app/macros.py:param_tree`) adresse par **position**
(`boxes.{i}`). Sur une console, insérer une tranche devant une autre est le geste normal ; une
macro « fader de la tranche 3 » viserait alors une autre source, en silence. C'est la leçon des
emplacements (CLAUDE.md, « Identité d'un conteneur ») appliquée à l'intérieur d'un conteneur.
L'ordre d'affichage est une donnée séparée, jamais une identité.

### 3.2 La chaîne est une liste ordonnée

Dans une tranche comme dans un bus, la chaîne est UNE liste ordonnée où **tout est élément** :
les modules de traitement ET les éléments de tranche (`trim`, `mute`, `fader`, `pan`). Les
éléments de tranche sont **uniques et déplaçables** ; les modules sont libres.

Chaque départ désigne son **point de prélèvement** : `after` = l'id de l'élément après lequel il
est pris. La question « le mute coupe-t-il ce départ ? » n'a donc pas de réponse globale : elle
dépend de la position du prélèvement par rapport au mute, et elle se lit dans le dessin (§ 12,
vue A). Même chose pour pré/post-fader, pré/post-EQ, etc.

Cette forme est celle de la maquette retenue : glisser un module entre deux éléments, déplacer le
mute, voir qu'un départ est « avant le mute » se traduit directement dans le document.

### 3.3 Types de bus et topologie

| Type | Alimenté par | Sortie MXL | Rôle |
|---|---|---|---|
| `group` | tranches | optionnelle (pour enregistrer) | sous-groupe, route vers un ou plusieurs masters |
| `master` | tranches, groupes | oui | programme(s) |
| `aux` | tranches | oui | retours, départs d'effets |
| `matrix` | masters, groupes, aux | oui | mélanges de bus (diffusion, enregistrement, salle) |
| `mixminus` | tranches | une par tranche abonnée | N-1 (§ 3.4) |

Ordre fixe, routage toujours vers l'étage supérieur — **aucune boucle possible par
construction**, donc pas de détection de cycle à écrire, et un ordre de calcul fixe :

```
tranches ──► groupes ──► masters ──► matrices
    │                       ▲           ▲
    ├──► aux ───────────────┼───────────┤
    ├──► N-1                │
    └───────────────────────┘
```

La validation refuse tout le reste.

**Tranche de retour (2026-10-17, 0.4.87).** Une tranche peut prendre pour SOURCE (entrée A ou B) un bus de la
console : `{kind: "bus", bus: <id>}` — demande de l'exploitant, pour poser la reverb SUR une tranche (aux → tranche
de retour → master). Genres permis (`INPUT_BUS_KINDS`) : aux, groupe, master, matrice — tout bus qui porte UN mix et
une chaîne ; ni le N-1 (une somme par tranche, pas un signal) ni l'écoute (un contrôle, calculé en dernier, dont les
entrées suivent le PFL). L'affirmation « aucune boucle par construction » ne tient donc plus seule :
- une **boucle interne** (la tranche renvoie, par départ en marche, affectation, route de groupe ou source de matrice
  en marche, vers le bus qui l'alimente) est cherchée sur le graphe du signal — la MÊME fonction que la boucle externe,
  `console_model.signal_loops`, codes `internal_loop` (le document suffit : `validate`) et `external_loop` (il faut les
  noms des flux publiés : le runtime) ; rallumer à chaud un départ ou une source qui la fermerait est refusé
  (`Console._loop_guard`) ;
- l'**ordre de calcul** n'est plus fixe pour ces documents : `console_model.compute_order` fait un tri topologique des
  bus et des tranches de retour, STABLE sur les étages ci-dessus (sans tranche de retour : l'ordre d'avant, au bit).
  Un départ coupé qui fermerait un cycle est « en retard » (calculé après son bus, contribution nulle tant qu'il est
  coupé, jamais rallumable). Aucune opération C nouvelle (OP_COPY, OP_MIXF) : l'ABI du plan ne change pas.

### 3.4 N-1 (mix-minus)

Un bus `mixminus` est une **somme propre, sans traitement**, alimentée par les tranches au moyen
d'un départ (par défaut au même point et au même niveau que leur départ PGM). Chaque tranche
marquée « sortie N-1 » reçoit SA sortie : la somme **moins sa propre contribution**.

- Coût : une soustraction par sortie, total linéaire au lieu de quadratique.
- Exactitude : garantie parce qu'aucun traitement non linéaire n'intervient AVANT la soustraction.
  C'est pourquoi on ne fait pas « PGM moins la tranche » : le limiteur du master la fausserait.
- Un traitement par sortie (limiteur…) reste possible APRÈS la soustraction.
- **Ordre (talkback)** : la régie parle dans une sortie N-1 avec atténuation du programme (§ 9).

### 3.5 Sorties

- Tout bus peut sortir sur le MXL (`output`), y compris un groupe, pour l'enregistrer.
- Chaque tranche peut avoir une **sortie directe**, prise au point de prélèvement choisi, avec sa
  propre chaîne (un retard, typiquement).
- Le **retard** est un module comme un autre, placé où l'on veut, ajouté au besoin à la
  construction.
- Nom de flux : `{hostname}_{id}`. L'UUID d'un flux étant dérivé de son nom, un id stable donne un
  flux stable : renommer le LIBELLÉ ne casse aucun abonné ; supprimer puis recréer, si.

### 3.6 Entrées et libellés

- Une tranche choisit directement son flux et ses canaux ; une **vue XY** montre aussi toutes les
  sources × toutes les tranches, avec filtres et tris.
- Une seule fréquence par console (48 kHz). Un flux à une autre fréquence est refusé au câblage,
  pas rééchantillonné en silence.
- **Générateur interne** (1 kHz, bruit rose) utilisable comme source de tranche, pour les
  alignements.
- **Libellés à plusieurs niveaux** : source NMOS, niveaux de libellés Bobi.Studio, libellé
  personnalisé dans la console.

## 4. Traitements : trois familles de modules

Chaque type de module déclare à un seul endroit : `type`, ses `params` (schéma typé et BORNÉ :
min, max, pas, unité, défaut — source de l'UI ET de l'arbre des macros), `latency_samples(params)`,
son coût mesuré (§ 14), et son état (mémoire de filtre, enveloppe), conservé tant que l'id existe.
Ajouter un type de traitement = ajouter une entrée à cette bibliothèque.

### 4.1 Modules intégrés (V1)

Gain / polarité, filtres passe-haut et passe-bas, EQ paramétrique 4 bandes, compresseur, gate,
limiteur, retard. Calcul dans le noyau C (§ 5).

- **Rampes** : tout changement de gain (fader, mute, départ, pan) est interpolé sur un bloc, sinon
  chaque mouvement produit un clic.
- **Pan** : tranche mono vers bus stéréo en loi −3 dB ; tranche stéréo vers bus stéréo en balance.
- Les tranches sont mono ou stéréo ; pas de liaison de deux tranches mono (décidé).

### 4.2 Modules hébergés (plus tard)

Pour les traitements tiers en plugin. Ordre proposé :

1. **LV2** : natif Linux et ouvert. Chaque plugin décrit ses paramètres (bornes, unités) en
   métadonnées → traduction directe en `param_tree`, donc macros, Workspace et contrôles
   universels sans code spécifique. Catalogue libre de qualité (LSP, x42, Calf, ZAM, Dragonfly).
2. **CLAP** : ouvert, moderne.
3. **VST3**, uniquement pour les plugins qui existent en version Linux.

Pas de VST Windows sous Wine en production. Garde-fous : coût **mesuré à l'insertion** (refus si
le budget est dépassé), **processus isolé** possible (un plantage ne coupe que le plugin), latence
déclarée intégrée à `D`, état sauvegardé dans le document. Pas d'interface graphique de plugin
(conteneur sans écran) : on pilote par paramètres.

### 4.3 Insert externe (plus tard)

L'objectif à terme est de proposer des **marques reconnues** (Dugan, CEDAR, Waves) pour rassurer
les exploitants. Elles n'existent pas en plugin Linux hébergeable (à notre connaissance, à
vérifier auprès de chacune) : Waves passe en direct par ses serveurs **SoundGrid**, Dugan et CEDAR
proposent des boîtiers Dante.

D'où une troisième famille : l'**insert externe**, un bloc de la chaîne comme un autre, qui
**envoie** le signal de ce point sur le réseau et **récupère le retour** traité.

- Latence aller-retour **mesurée** (comme les grandes consoles pour leurs inserts), compensée dans
  `D`, affichée sur le bloc (« externe · 4,2 ms aller-retour »).
- Transport déjà disponible : le moteur `2110_io` émet et reçoit du 2110-30 (compatible AES67) ;
  le service `sap` découvre les flux Dante en mode AES67.
- Couvre un serveur SoundGrid (via un pont AES67/Dante ou MADI vers SoundGrid), un Dugan ou un
  CEDAR Dante, et tout matériel.
- Pilotage des appareils : adaptateurs par marque, sur le modèle des services `atem` / `skaarhoj`,
  qui versent leurs réglages dans le `param_tree`.
- Limite à rendre visible : un insert externe placé avant le prélèvement d'un retour oreillette ou
  d'un N-1 dégrade ce chemin « direct ».
- À long terme, une licence directe (Dugan vend son algorithme sous licence) donnerait un module
  natif : décision commerciale, pas technique.

## 5. Calcul : un plan compilé en C

Les filtres (biquads) et les dynamiques (suiveur d'enveloppe) sont **récursifs** : numpy ne les
vectorise pas, une boucle Python par échantillon ne tient pas le temps réel, et scipy n'est pas
dans l'image compute.

Et à 64 tranches, une boucle Python qui appellerait le C module par module ferait de l'ordre de
130 000 appels ctypes par seconde — un coût d'appel comparable au calcul lui-même (estimation, non
mesurée). Donc :

- la structure est **compilée en un plan** (tableaux C : opérations, coefficients, états, tampons) ;
- le plan est exécuté par **un seul appel C par bloc** ;
- Python ne garde que la reconfiguration (construire un nouveau plan, § 7.3).

Noyau `audio_dsp.c` : biquad en forme transposée directe II (coefficients RBJ « Audio EQ
Cookbook »), enveloppes crête/RMS, gains avec rampe, sommes, soustraction N-1, loudness R128,
corrélateur, générateur. Livré en `.so` pré-construit embarqué dans le script, sur le patron de
`color_corrector` (écriture atomique dans `/tmp`, `ctypes.CDLL`). **Pas de repli numpy
silencieux** : noyau absent → la console refuse de démarrer et le dit, plutôt que de tourner à une
fraction de la cadence.

## 6. Temps, alignement, latence

- **Lecture par index**, jamais par « dernier bloc » : `AudioReader.read_from(index, n)` avec un
  index commun à toutes les entrées, dérivé de l'heure TAI. Un échantillon d'une source se somme
  avec l'échantillon CONTEMPORAIN d'une autre.
- **Sortie** : `AudioWriter.write(bloc, index=idx_entrée + D)`. Une sortie a un index TAI cohérent,
  mesurable par la chaîne de latence existante.
- **`D`** = latence fixe de la console : bloc + marge de lecture + latence maximale des chemins
  alignés. Chaque chemin plus court est retardé de la différence.
- **Compensation « alignée » ou « minimale »** :
  - **tout ce qui entre dans un master est aligné, sans réglage possible** — arrivé au master, on
    doit être raccord avec le reste ;
  - la compensation « minimale » n'existe que sur les **sorties terminales** qui ne reviennent pas
    au programme : aux, N-1, sorties directes. Usage type : un retour oreillette ;
  - **chaque sortie affiche sa latence et son mode** (« aligné 12,0 ms », « direct 3,1 ms »).
- Modifier un module qui change sa latence change `D` : c'est une reconfiguration de structure
  (§ 7), et le saut de latence des sorties existantes est signalé, appliqué seulement à la demande.
- **Taille de bloc** : globale, réglable (240 échantillons = 5 ms par défaut). Le plan compilé rend
  des blocs de 1 ms abordables — à mesurer au banc.
- **Entrée absente ou en retard** : silence pour ce bloc, compteur par tranche dans `/state`,
  jamais de blocage des autres tranches.
- **Tranche de retour** (§ 3.3) : calculée dans le MÊME bloc que son bus, sans bloc ajouté ; elle hérite des groupes de
  calage et de la latence de son bus (`latency_plan.strip_in` = tranches → bus, plus la chaîne du bus), puis de sa
  propre chaîne (la reverb), et ses départs s'alignent comme ceux de toute tranche : au master, le chemin direct et le
  chemin aux → retour → master arrivent au même échantillon (vérifié à l'impulsion, deux exécuteurs :
  tests/verif_console_retour_bus.py). Son `align` est sans effet. Deux entrées de latences différentes (un bus et une
  prise, deux bus) : la plus tardive fait la latence de la tranche, l'autre est retardée à l'entrée (`input_pad`).
  Largeur : celle de la tranche ; bus stéréo → tranche mono = (G+D)·½ (la loi d'un départ stéréo vers un bus mono),
  bus mono → tranche stéréo = dupliqué.
- **Mode tranche** : sans objet (il concerne la vidéo ; l'audio est déjà continu par blocs).
  L'exception est écrite dans le code, comme l'exige CLAUDE.md.

## 7. Reconfiguration à chaud et persistance

| Changement | Exemples | Chemin |
|---|---|---|
| **Réglage** | fader, mute, gain d'EQ, seuil | le conteneur applique et renvoie la valeur acceptée ; l'orchestrateur persiste PAR LOT |
| **Structure** | ajouter / retirer / déplacer une tranche, un bus, un module, un départ | validation à blanc par le conteneur → persistance du document canonique → application |

### 7.1 Une capacité générique du proxy de plugin

Tout le contrôle passe déjà par le proxy générique `/api/containers/<vmid>/plugin/<chemin>`. Ce
proxy relaie et **ne retient rien** : un réglage à chaud qui doit survivre à un redéploiement
demande aujourd'hui du code bespoke (`_persist_multiview_hot`, `app/routes/plugin_registry.py`),
et les autres plugins perdent leurs réglages en silence — la panne du pilotage externe défait par
l'auto-relance.

On lui ajoute une capacité **générique et déclarative** :

- le manifeste déclare quels endpoints modifient un état à retenir, et où dans `deploy_config` ;
- réglage : le conteneur applique et renvoie la valeur acceptée, le proxy la persiste par lot (un
  mouvement de fader produit des dizaines de valeurs par seconde, seule la dernière s'écrit) ;
- structure : le proxy demande au conteneur une **validation à blanc**, persiste le document
  canonique renvoyé, puis demande l'application. La logique reste dans le plugin, l'ordre
  « persister avant d'appliquer » est tenu, et l'orchestrateur ne connaît pas la console ;
- plus tard : le même proxy diffuse chaque requête aux deux conteneurs d'une paire (§ 13).

Bonus : le multiview pourrait migrer dessus et perdre son code bespoke (chantier à part).

### 7.2 Concurrence

Plusieurs opérateurs peuvent agir en même temps. Réglages : **le dernier gagne**. Structure :
**contrôle de version** — une modification fondée sur une version périmée est refusée, avec le
nom de ce qui a changé entre-temps.

### 7.3 Côté conteneur : construire à côté, basculer entre deux blocs

1. Le conteneur reçoit le document version N+1 et compile le nouveau plan **à côté** de l'ancien,
   hors de la boucle audio.
2. Les éléments dont l'id existe déjà **reprennent leur état** : insérer un compresseur ne remet
   pas l'EQ voisin à zéro.
3. Bascule atomique au début d'un bloc. Les sorties nouvelles ouvrent leur `AudioWriter`, les
   sorties retirées ferment le leur.
4. La version appliquée est publiée dans `/state` : l'orchestrateur sait si le conteneur applique
   ce qui est persisté.

## 8. Mesure et état publié

`/state` publie : version de structure appliquée, vumètres crête + RMS, réduction de gain des
dynamiques, loudness R128 (masters), corrélation de phase (bus stéréo), compteurs de blocs
manqués par tranche, latence `D` et latence par sortie, charge CPU (temps de calcul d'un bloc ÷
durée d'un bloc).

Les vumètres (~10 Hz) ne passent pas par l'interrogation des voyants actuels (1,5 s) : il faut un
**retour d'état poussé** (WebSocket ou SSE), qui sert aussi le Workspace (chantier 1, § 16).

## 9. Écoute et ordre

- **Écoute web** :
  - V1 : **PCM sans perte sur WebSocket + AudioWorklet**, servi par la console elle-même. Latence
    estimée ~30–50 ms en réseau local, à confirmer au banc. Débit ~1,5 à 2,3 Mb/s en stéréo.
  - Plus tard : **WebRTC audio seul** (Opus, sans encodage vidéo, donc plus rapide que le moniteur
    actuel), en option activable, pour l'écoute hors réseau local.
  - Le moniteur actuel encode déjà l'audio en Opus stéréo 128 kb/s, mais il ne prend que les
    canaux 0-1 et passe par l'encodeur vidéo : inadapté à l'écoute d'une console.
- **Sortie d'écoute MXL** → 2110 ou DeckLink vers des enceintes : la meilleure écoute, recommandée.
- **Solo / PFL** : plus tard, avec un bus d'écoute.
- **Ordre (talkback) vers les N-1 : V1.** Source au choix, dans les **réglages de monitoring** :
  micro externe via MXL, ou micro du navigateur (WHIP → MediaMTX → ingest type `stream_in` →
  MXL ; quelques centaines de ms, acceptable pour un ordre puisque celui qui parle ne s'entend pas).

## 10. Plusieurs opérateurs, zones et droits

- **Permissions globales** (habilitations éditables, `auth.PERMISSIONS`) : construire une console,
  l'exploiter, rappeler une mémoire, enregistrer une mémoire, sauvegarder / rappeler des réglages.
- **Zones par console** : ensemble nommé de tranches et de bus (« Plateau », « Musique »,
  « N-1 duplex »), accordé à un utilisateur ou à un groupe (`app/user_groups.py`).
- **Les droits s'appliquent dans le proxy** — là où passe tout le contrôle, y compris celui des
  widgets du Workspace.
- **Affichage hors zone : réglable** — tout montrer (en retrait) pour comprendre, ou masquer pour
  n'avoir que ce qu'on gère.
- **Rappel de mémoire** : ne touche QUE les éléments sur lesquels l'opérateur a des droits ; le
  reste ne bouge pas, et c'est signalé.
- **Droits des macros** (une macro sans utilisateur qui pousse un fader) : reporté.

## 11. Intégration orchestrateur

- **Câblage.** Entrées : `consumes` avec `from_list` sur la liste des tranches (mécanisme présent
  dans `plugins.derive_wiring`). Sorties : `produces` dérivés des sorties déclarées. À vérifier :
  que `from_list` côté `produces` se comporte comme côté `consumes` (dans `_resolve` commun, jamais
  utilisé ainsi à ce jour).
- **Macros : extension nécessaire.** Le résolveur actuel ne connaît que des éléments fixes ou
  comptés par position. Il faut une source d'éléments **dérivée du document persisté** : chemins
  par id (`strips.<id>.chain.<id>.<param>`), bornes lues dans la bibliothèque de modules (arbre
  complet même conteneur éteint). Garde : insérer une tranche en tête ne change le chemin d'aucun
  paramètre existant.
- **Actions** : mute / démute, réglage d'un départ, ordre, rappel de mémoire.
- **Emplacement.** Les contrôles posés dans le Workspace visent un **emplacement** (3e barreau),
  pas une instance (§ 12.2). Remplacer la console sous le même emplacement doit donc **reprendre
  son document, mêmes ids** ; toute perte au remplacement est signalée, jamais tue.
- **NMOS** : les sorties sont des flux MXL ordinaires ; à vérifier qu'elles apparaissent comme
  senders audio sans code dédié.

## 12. Interface

### 12.1 La page de la console : trois vues, trois usages

| Vue | Rôle | Maquette |
|---|---|---|
| **A — synoptique horizontal** | **éditer l'ordre du signal** d'une tranche (retenue) | modules glissés entre les éléments, départs partant de leur point de prélèvement, latence « aligné / direct » sur chaque départ, panneau du module à droite |
| **B — rails** | **vue globale** de la console | une ligne par tranche avec sa chaîne compacte ; couches, filtres, tris ; tranches hors zone en retrait ; appliquer une chaîne à une sélection |
| **D — vue console** | **exploitation** | pan, mute, fader et vumètre en bas, à leur place ; la chaîne se lit au-dessus dans l'ordre réel du signal, départs compris ; sortie N-1 et bouton ORDRE |

On passe de l'une à l'autre sans perdre le contexte (un clic sur un rail de B ou sur la chaîne
d'une tranche de D ouvre A sur cette tranche). La maquette C (synoptique vertical) reste une
variante de A pour un écran en hauteur.

Autres décisions :

- **Écran variable** : l'interface s'adapte (poste de régie, tactile, tablette).
- **64 tranches et plus** : proposer plusieurs présentations (banques, défilement, vue compacte +
  détaillée) ET des **couches personnalisées** par l'utilisateur.
- Tous les libellés via `i18n/`, charte `docs/design/DESIGN.md`.

### 12.2 Workspace : chaque utilisateur construit son interface

Dans la page Workspace, l'utilisateur compose son interface en **piochant des contrôles de
n'importe quel plugin** : un réglage du correcteur de couleur, un du mélangeur, un de la console.
D'où l'intérêt de **contrôles universels** : même aspect partout, dans la page du plugin comme
dans le Workspace.

- **Granularité libre** : de la tranche complète jusqu'à un seul mute, un seul fader, un seul
  départ aux. Chaque paramètre du `param_tree` peut se poser seul ; le plugin déclare en plus des
  **composites prêts** (tranche, départ, EQ avec courbe).
- **Cible : un emplacement** (cf. § 11).
- État actuel (2026-10-12) : la page `/workspace/<projet>` est une grille de widgets (état, plugin
  entier limité à un par type, moniteur, shotbox de boutons) ; **aucun widget « un contrôle »**.
  Les briques existent : `param_tree()` (écriture ET relecture de chaque paramètre), rendu typé
  `ptWidgetHtml` de l'éditeur de macros (champ numérique, pas encore de fader), écriture par le
  chemin de la shotbox, relecture par `/api/projects/<pid>/states`.

C'est le **chantier 1** (§ 16), à faire avant l'interface de la console, qui en sera le premier
gros client.

## 13. Secours : conçu dès le départ, livré plus tard

Une console de secours sur un autre nœud, qui suit le même document et prend la relève. Pas en
V1, mais la V1 doit le permettre :

1. tout l'état est persisté par l'orchestrateur (§ 7) ;
2. **tout le contrôle passe par l'orchestrateur**, jamais navigateur → conteneur en direct : c'est
   lui qui diffusera chaque requête aux deux consoles ;
3. sorties indexées TAI et calcul déterministe à paramètres égaux.

À concevoir : l'identité des flux de sortie au basculement.

## 14. Budget CPU

- Chaque module intégré a un coût **mesuré au banc** (µs par bloc et par canal) sur la flotte réelle.
- Module hébergé ou insert externe : coût / latence **mesurés à l'insertion**.
- La console calcule le coût de sa structure et **refuse** une modification qui dépasserait le
  budget d'un bloc (avec marge), en disant laquelle et de combien. Un ajout accepté qui fait
  craquer le son est un échec silencieux côté exploitant.
- Charge réelle publiée dans `/state`, alerte au-dessus d'un seuil.
- Profil `core_pool` : un cœur pour une petite console, à ajuster d'après la mesure.
- La limite du nombre de tranches EST ce budget : 8 pour les essais, cible 32 à 64, aucune constante.

## 15. Points encore ouverts

1. **Console arrêtée** : sa structure n'est pas modifiable (personne pour valider à blanc). On
   l'accepte et on l'affiche, ou on persiste sans validation et on valide au démarrage ?
2. **Secours** : identité des flux de sortie au basculement.
3. **Droits des macros** : reporté.
4. **Modèles de console, duplication de tranche, préréglages de chaîne** : retenus, à préciser.
5. **Taille de bloc et latence réelle** : à mesurer au banc (5 ms par défaut, 1 ms visé pour les
   chemins « directs » ?).
6. **Plugins tiers** : ordre LV2 / CLAP / VST3 et choix du pont SoundGrid, quand on y viendra.

## 16. Découpage

### Chantier 1 — Contrôles universels et Workspace (utile à tous les plugins)

| Lot | Contenu |
|---|---|
| 1.1 | Bibliothèque de contrôles universels (fader, rotatif, interrupteur, liste, mute, vumètre, réduction de gain) ; indice de présentation dans le `param_tree` |
| 1.2 | Widget `control` du Workspace, ciblant un emplacement ; sélecteur de paramètre repris de l'éditeur de macros |
| 1.3 | Composites déclarés par les plugins (tranche, départ, EQ…) |
| 1.4 | Retour d'état poussé (WebSocket ou SSE) |

### Chantier 2 — Console `audio_console`

| Lot | Contenu | Garde (hors ligne) |
|---|---|---|
| 2.1 | ✅ (2026-10-12) Modèle + validation : ids, chaîne ordonnée, points de prélèvement, types de bus, topologie, règles de compensation — `src/console_model.py`, `modules.json` | `verif_console_modele.py` (34 règles, 6 mutations) |
| 2.2 | ✅ (2026-10-12) Noyau C : filtres, dynamiques, rampes, sommes, N-1, R128, corrélateur, générateur — `tools/audio_dsp.c`, `src/dsp.py` | `verif_console_dsp.py` (58 vérifications, 7 mutations) |
| 2.3 | ✅ (2026-10-12) Moteur Python : plan compilé, compensation alignée / minimale (groupe compris), sorties bus / directes / N-1, mesures — `src/engine.py`. Reste : adaptateur MXL, exécuteur C en un appel | `verif_console_moteur.py` (44 contrôles, 6 mutations) |
| 2.4 | ◐ État conservé par id à la recompilation (dans 2.3). Reste : bascule entre deux blocs dans le conteneur | insertion d'un module : l'état des voisins survit |
| 2.5 | Orchestrateur : persistance générique du proxy, route de structure, câblage, `param_tree` par id, zones et droits | insertion d'une tranche : aucun chemin de macro ne bouge |
| 2.6 | Écoute PCM WebSocket ; ordre vers les N-1 | |
| 2.7 | Interface : vues A, B, D | vérification visuelle par l'exploitant |
| 2.8 | Mémoires simples | rappel limité aux droits de l'opérateur |
| 2.9 | Banc de coût sur la flotte, budget, alertes | — (banc, `tools/bancs/`) |

Plus tard : secours, automixer, modules hébergés, insert externe, écoute WebRTC à distance,
solo / PFL, surround, isolation et fondu des mémoires.

Chaque garde sera mutée pour vérifier qu'elle mord.

## Annexe — constat d'intégration (lecture du code, 2026-10-12)

- **Proxy de plugin** (`app/routes/plugin_registry.py:809-895`) : ne prend AUCUN verrou ; la
  persistance multiview (`_persist_multiview_hot`, 935-974) part d'un instantané PÉRIMÉ de la
  requête, hors `verrou_vmid` — exposée à l'écriture concurrente que `_plugin_config_apply`
  (1047-1087, relecture fraîche sous verrou) évite. Le crochet déclaratif se placerait après le
  relais (≈ ligne 884) : réponse du conteneur → sous `verrou_vmid(op="config")` relecture
  `_load_dc` → écriture. Regroupement par lot : rien n'existe, à écrire. Migration du multiview :
  possible avec une échappatoire (persisteur nommé), elle dépend d'un booléen « changé ? ».
- **Câblage** : `from_list` marche déjà pour `produces` (`app/plugins.py:1453-1497`), mais clé de
  premier niveau seulement, sans filtre, slot = index dans la liste. À ajouter : chemin pointé
  (`console.buses`), filtre (`output: true`), slot par id, gabarit de nom `{hostname}_{id}`.
- **Macros** (`app/macros.py:477-546`) : éléments positionnels (`box:{i}`). Une source « dérivée du
  document, par id » se branche à côté de `boxes` ; le chemin construit `{prefix}.{i}` et
  `exec_post` / `_exec_leaf_step` doivent accepter un id texte. Cache à durée fixe.
- **Emplacements** : chaîne `db_role_get*` → `instance_uuid` → `db_container_by_instance` →
  vmid, sans fonction unique ; écrire `vmid_for_role()`, résolu à l'appel, jamais persisté.
- **Pages dédiées** : aucune page par type ; tout passe par `plugin_section.html`, le compositeur
  multiview y est chargé par un drapeau codé en dur (`has_mv`). Il faudra un chargeur générique
  ou une route propre.
- **Livraison** : seul `script.py` est poussé, plus `bobimxl.py` (`app/deploy.py:1606-1618`).
  Les noyaux C sont embarqués en base64 (udc, color_corrector). Une livraison de fichiers
  déclarée par le manifeste serait du code neuf dans `deploy.py`.
- **Retour d'état poussé** : aucun canal (pas de SSE ni WebSocket) ; tout est interrogation.

## Annexe — choix pris aux lots 2.1 et 2.2 (2026-10-12)

- Sortie N-1 de tranche : `mixminus_out = {bus, chain, compensation}`, sans point de prélèvement
  (elle suit la soustraction). Un bus `mixminus` ne porte pas de sortie propre (refusé).
- Seul `aux` admet la compensation « minimale » parmi les bus ; un aux qui alimente aussi une
  matrice compte dans `D` par le chemin de la matrice.
- Marge de lecture par défaut : 48 échantillons (1 ms), `modules.json:console.read_margin_samples`. **Remplacé le 2026-10-13 : 144 éch. (3 ms), cf. dernière annexe.**
- Noyau : états en float64 possédés par l'appelant, signal float32, aucun statique C ; rampe
  `g0+(g1−g0)·(i+1)/n` (dernier échantillon = g1) ; pas de `-ffast-math` (plafond du limiteur en
  IEEE strict) ; le limiteur compte ses écrêtages de sécurité (`st[4]`, doit rester à 0).
- Coût MESURÉ sur la machine de dev (scalaire, SANS AVX2 — VM QEMU —, bloc 240, hors ctypes) :
  biquad 2,0 µs, eq4 6,3 µs, compresseur 8,7 µs, limiteur (L=48) 12,5 µs. À refaire sur la flotte.
- La machine de dev n'a pas AVX2 : `build_dsp.sh` accepte `DSP_ARCH=""`, positionné par le test
  quand `/proc/cpuinfo` n'a pas avx2. Défaut : `-mavx2`.
- Détecteur de crête compresseur/gate : décroissance réglable (30 / 20 ms), ondule un peu sous
  ~100 Hz — compromis à documenter dans l'aide.

## Décision — livraison du code dans le conteneur (2026-10-12)

**Option D validée par l'exploitant** : le manifeste d'un plugin déclare des fichiers que le
déploiement pousse avec le script (généralisation de ce qui existe pour `bobimxl.py`). Le code de
la console reste découpé en vrais modules (`src/`), testés directement, au lieu d'un gabarit
`str.format` unique. Écartés : binaire en base64 dans `script.py` (modèle udc / color_corrector),
compilation au démarrage du conteneur, noyau dans l'image compute.

### Retours de l'exploitant sur ces choix (2026-10-12)

- **Sortie N-1** : « forcément après la soustraction » — confirmé. Question posée : où se fait la
  soustraction, et est-elle réglable ? Réponse : la soustraction n'a pas de position propre ; elle
  retire EXACTEMENT ce que la tranche a versé dans la somme, à l'endroit où elle l'a versé. Ce qui
  se règle, c'est le POINT DE PRÉLÈVEMENT du départ de la tranche vers le bus N-1 (déjà dans le
  modèle : `send.after`) — il décide ce que les AUTRES entendent de cette tranche (pré-fader : ils
  l'entendent quoi qu'on fasse du fader ; post-mute : couper la tranche la retire aussi des N-1 des
  autres). La sortie N-1 de la tranche elle-même ne dépend jamais de son propre mute ni de son fader.
- **Compensation « minimale » sur un groupe : accordée.** La compensation devient une propriété de
  la SORTIE, pas du bus : la sortie MXL propre d'un groupe peut être directe, son alimentation du
  master reste toujours alignée. master et matrix : alignés uniquement.
- Bus N-1 sans sortie propre : confirmé. Marge de lecture 1 ms par défaut : confirmé (porté à 3 ms le 2026-10-13 après le banc réel).
- Option « départ N-1 qui suit le niveau PGM » : écartée par l'exploitant (pas utile).

## Annexe — moteur (lot 2.3, 2026-10-12)

- Coût MESURÉ, VM de dev sans AVX2, bloc 240 (budget 5000 µs) — tranche = trim, HPF, EQ 2 bandes,
  comp, mute, fader, pan, 3 départs ; PGM stéréo + limiteur :
  8 tranches 473 µs (115 appels ctypes/bloc), 32 tranches 1139 µs (403 appels), mesures comprises.
  La colle Python reste < 25 % à 32 tranches. À refaire sur la flotte.
- Dynamiques par canal, NON liées en stéréo (le noyau traite un canal à la fois) : l'image peut
  bouger sous un limiteur — lien stéréo à ajouter.
- Tranche mono vers bus stéréo sans pan : loi centrale −3 dB ; stéréo vers bus mono : moyenne.
- `Plan.set` d'un paramètre de LATENCE (retard, anticipation) lève `StructureChange` (§ 6).
- Réglage à chaud du niveau d'un générateur non câblé (absent de `param_paths`).

## Qualité du summing (question de l'exploitant, 2026-10-12)

En numérique, sommer = additionner ; la « qualité » tient à six points, dont l'état actuel :
1. **Alignement à l'échantillon** des chemins parallèles (sinon filtrage en peigne) — le vrai
   point critique ; traité (index TAI commun + compensation, testé à l'impulsion).
2. **Marge** : interne en flottant, pas d'écrêtage dans la console ; l'écrêtage n'existe qu'à la
   conversion vers un format entier (L24 du 2110-30). À faire : témoin de dépassement 0 dBFS sur
   chaque sortie, alerte, limiteur proposé sur les masters.
3. **Précision d'accumulation** : aujourd'hui float32 (`dsp_mix_add`) ; erreur estimée très
   en dessous du plancher 24 bits (à MESURER). Proposé : accumulateurs de bus en float64, coût
   faible, plus un test « null » contre une référence float64.
4. **Lois de pan** cohérentes et documentées (−3 dB aujourd'hui) — option de loi à discuter.
5. **Rampes de gain** (pas de « zipper ») : traité.
6. **Dénormaux** : pas de FTZ/DAZ dans le noyau — sans effet audible, mais une queue de
   réverbe/filtre qui s'éteint peut faire exploser le coût CPU. À ajouter.
Dither : utile seulement vers 16 bits ; négligeable en 24 bits.
**Décidé (2026-10-12)** : points 2, 3 et 6 ajoutés au travail de la nuit, après l'exécuteur C.
Le « son » d'un summing analogique est une coloration (saturation), pas de la précision : en
numérique, c'est un traitement à ajouter volontairement s'il est voulu, jamais un défaut à subir.

## Annexe — exécuteur C et contrôles universels (2026-10-12, nuit)

- **Exécuteur C en un appel par bloc** (`tools/audio_plan.c`, `dsp_plan_run`, 15 opcodes) :
  sortie IDENTIQUE au bit près à l'exécuteur Python (60 consoles aléatoires + recompilations),
  1 seul appel C par bloc. `compile_plan(..., executor="c")` ; défaut encore "python". Mesuré
  (VM de dev sans AVX2, bloc 240, mesures comprises) : 8 tranches 193 µs, 32 tranches 594 µs,
  64 tranches 1356 µs (contre 547 / 1418 / 2996 en Python) — 64 tranches ≈ 27 % du budget d'un
  bloc sur cette VM. ~80-85 % du reste est du DSP scalaire. À refaire sur la flotte (AVX2).
- **Contrôles universels** (chantier 1.1) : `static/js/bobi-controls.js`, `static/css/bobi-controls.css`,
  démo `/static/controls_demo.html`, test `tests/verif_bobi_controls.js`. Fader à loi dB (unité à
  75 % de course, −∞ sous 0,5 %), rotatif, mute/ORDRE (maintenu ou fugitif), liste, afficheur,
  vumètre (crête + RMS, maintien, témoin d'écrêtage), réduction de gain ; clavier + ARIA ;
  couleurs dérivées des variables des thèmes (`color-mix` en OKLCH). NON VU dans un navigateur :
  vérification visuelle par l'exploitant. Restent : molette, saisie au clic, intégration
  (layout.html, `ptWidgetHtml` du Workspace, 9 clés i18n `controls.*`).

## Annexe — plugin déployable et qualité du summing (2026-10-12, nuit)

- **Plugin** : `plugin.json` (rubrique Composition, 1 cœur épinglé, `files` = 7 modules +
  `modules.json` + `kernel.json` + noyau en base64), `script.py` mince, `src/{mxl_io,runtime,
  control}.py`. Boucle : toutes les entrées lues au même index, sorties estampillées `r0+B+M`
  (le retard de compensation est DANS le contenu), écrites par tranches de 1 ms, index contigu ;
  entrée absente → silence + compteur ; producteur recréé / SIGBUS → réouverture. Noyau
  illisible ou AVX2 absent → le script reste vivant et publie l'erreur, la console ne tourne pas.
  :8082 : GET /state, /doc, /mutables ; POST /set (lot possible, 400 hors bornes, 409 si
  latence), /mute, /structure/validate, /structure/apply (409 version périmée ou saut de latence
  non accepté, 422 invalide). Macros : mute / unmute seulement (param_tree par id = lot 2.5).
  Noyau : `tools/make_kernel.sh` (lit l'ABI dans la source) → `build/audio_dsp.so` + `kernel.json`,
  à refaire à chaque changement du C. Test : `verif_console_plugin.py` (~90 contrôles, 6 mutations).
- **Summing** : accumulateurs de bus, matrices et N-1 en float64, conversion float32 une fois
  avant la chaîne du bus. MESURÉ contre `math.fsum` (64 tranches) — erreur max avant → après :
  signaux indépendants −136 → −152 dBFS ; annulation −124 → −265 dBFS ; N-1 d'une tranche forte
  contre 63 faibles −126 → −209 dBFS. Seuil : ½ ulp float32 (≈ −150,5 dBFS), 12 dB sous le LSB
  24 bits. Coût ≈ +1,5 %. FTZ/DAZ posé à l'entrée de `dsp_plan_run` et restauré à la sortie.
  Témoin de dépassement par sortie (`meters.outputs` : over, over_count, over_db). Test :
  `verif_console_summing.py` (5 mutations).
- **Lacunes pour 2.5** : câblage (page Câbles muette : sorties et entrées non déclarables),
  param_tree par id, persistance des `/set` (perdus au redéploiement), pas d'interface (`ui`),
  pas de refus de structure trop coûteuse.

## Annexe — premier déploiement réel, banc local (2026-10-13, nuit)

Nœud dell-1 (Xeon E5-2699v4, AVX2) : 2 tone_gen (1 kHz et 440 Hz à −20 dBFS) + la console,
1 cœur. Orchestrateur redémarré après smoke_test et check_plugins verts. Horace non touché.

- **Livraison (option D)** : les 10 fichiers poussés, noyau chargé (`avx2`, ABI 3).
- **Latence mesurée = `latency_report` à l'échantillon près** (front d'un trou dans la tonalité,
  flux d'entrée contre flux de sortie) : PGM 336 éch. (7,0 ms = 240 + 48 + 48), aux 288
  (6,0 ms, comme déclaré), N-1 336. Avec une marge de 3 ms : 9 / 8 / 9 ms, également comme déclaré.
- **Signal** : PGM contient les deux tonalités à −20,00 dBFS. La N-1 de A contient le 440 Hz à −20,0
  et le 1 kHz à −187 dBFS, soit environ 167 dB de rejet (zéro numérique). Le rejet tient au fader −10 dB et
  sous un balayage du fader de 600 réglages en 10 s. Le départ aux à −6 dB mesure −26,0 dBFS, sans 440 Hz.
- **À chaud** : 576 `/set` en 10 s via le proxy, 0 bloc en retard ; les valeurs hors bornes, de mauvais type
  ou sur un chemin inconnu sont refusées (400). Un `/structure/apply` qui insère un `gain` en milieu de chaîne
  réutilise 28 ids et l'état du comp et du hpf, sans saut (résidu inchangé à −157 dBFS).
  `stale_version` donne 409. Un retard de 4 ms donne 409 `latency_jump` tant que `accept_latency_jump` n'est pas passé.
- **Robustesse** : un générateur arrêté, puis tué au kill -9, donne du silence compté sans erreur ;
  le lecteur se raccroche seul.
- **CPU** (`bench_engine`, un cœur libre, mesures comprises) :

| Tranches | dell-1 Python | dell-1 C | dl360-1 Python | dl360-1 C |
|---|---|---|---|---|
| 8 | 406 µs | 152 µs | 413 µs | 184 µs |
| 32 | 1357 µs | 478 µs | 1314 µs | 508 µs |
| 64 | 2625 µs | 902 µs | 2484 µs | 997 µs |

  Console vivante avec 64 tranches : charge de 20 % (pic 26,5 %), environ 1 ms par bloc de 5 ms,
  0 bloc en retard sur 120 s.
- **Défaut : marge de lecture de 1 ms trop juste** face à des producteurs Python. La marge de 48 éch.
  donne 30 % et 11 % de blocs manqués ; 96 éch. en donne 2,2 % et 0 % ; à partir de 144 éch., 0 %.
  Décision à prendre : passer le défaut à 3 ms, ou rendre la marge réglable par entrée.
- **Défaut : `/structure/apply` non persisté** : `deploy_config` reste en version 1, si bien qu'un
  redémarrage revient au document de création. Il faut un crochet dans `plugin_proxy` (lot 2.5,
  avec la persistance des `/set`).
- Mineur : un bloc en retard au démarrage (transitoire) ; `/structure/validate` accepte une
  `base_version` périmée (seul `apply` la refuse) ; `/state` n'indique pas l'exécuteur utilisé.
- **Décision de l'exploitant (2026-10-13)** : marge de lecture par défaut **3 ms** (144 éch.,
  `modules.json:console.read_margin_samples`). Latence du PGM à l'état neuf : 384 éch. (8 ms), contre 336.
  Le plugin vit désormais dans son propre dépôt (privé), `bobistudio-plugin-audio_console`, référencé en sous-module.

## Annexe — lot 2.5, premier passage (2026-10-13)

- **Persistance générique** (`app/plugin_persist.py`, déclarée par `control.persist` du manifeste,
  refusée au scan si fausse) : réglages relus par lot via `/doc` (1 s après le dernier geste, au
  plus tard 5 s), structure validée à blanc → persistée → appliquée (refus : ancien document
  remis), même chemin pour le proxy et les macros. Le script DÉPOSÉ dans le conteneur est réécrit
  à chaque écriture (sans relance) — mesuré sur le banc : sans ça, un fader persisté à −9,5 dB
  revenait à 0 dB après un stop/start du script. Banc dell-1 : réglage −12,5 dB et structure v2
  (module inséré) conservés après stop/start ; apply périmé → 409, base inchangée.
- **Câblage dérivé du document** (`app/wiring_document.py`, `from_list` + `key` dans
  `derive_wiring`, page Câbles à slots texte) : sorties = bus `output` hors N-1, sorties directes,
  sorties N-1 ; entrées = tranches `input.kind == "flow"`, slot = id. Câbler écrit
  `input.flow` via la structure persistée. Débrancher pose `input.flow = ""` : **entrée non
  câblée** = silence voulu, sans compteur de blocs manqués (modèle et moteur, deux exécuteurs).
- **`param_tree` par id** (`app/param_tree_id.py`, source `by_id` dans `macros.param_tree`) :
  feuilles `strips.<id>.chain.<id>.params.<p>`, lues dans le document persisté (conteneur
  arrêté compris) ; insérer une tranche ne déplace aucun chemin. Arbre invalidé après une
  application de structure.
- **Zones et droits** : non faits, six décisions à prendre (où vivent les autorisations,
  défaut, correspondance requête → élément, structure = permission à part ?, rappel filtré,
  point d'accroche dans le proxy).

## Annexe — cinq fonctions d'exploitation (2026-10-13)

Départ ON/OFF, bypass de module, PFL + bus d'écoute, mesures par canal, ordre (talkback) vers les
N-1. Noyau ABI 4 (plan 3). Banc : `tests/verif_console_features.py` (deux exécuteurs comparés au bit,
16 mutations via `--mutations`), jeu d'or de l'ancien moteur `tests/golden_console_features.npz`.

- **Départ `on`** (`strips.<sid>.sends.<xid>.on`, vivant) : off = gain en rampe vers 0, `level_db`
  conservé ; un départ N-1 coupé retire la tranche des N-1 des autres sans fausser le sien.
- **Bypass** (`…chain.<eid>.params.bypass`, vivant, tout module) : le module continue de TRAITER
  (état chaud : aucun transitoire au retour, mesuré identique au bit à un module jamais bypassé),
  fondu enchaîné traité -> sec d'un bloc (`dsp_xfade`), puis copie du sec. Latence JAMAIS modifiée :
  le sec traverse la même ligne à retard (limiteur : ligne de L échantillons alimentée en continu ;
  retard : le bypass est sans effet, il EST la latence). Un bypass n'économise donc pas de CPU.
  Réduction de gain lue = 0 tant que bypassé.
- **Écoute** : bus `listen` (au plus un, largeur 1|2, `fallback` = master/groupe/aux/matrice,
  5e étage de calcul). `pfl` vivant sur les tranches et sur les bus group/master/aux/matrix. Prise AVANT
  le premier `mute`/`fader` de la chaîne (`console_model.pfl_tap`) : indépendante du mute ; un module
  placé entre mute et fader n'est pas entendu. Aucun PFL actif -> repli (fondu croisé d'un bloc).
  Non destructif : le PGM est identique au bit avec ou sans PFL. Compensation « minimale » par
  nature (monitoring : n'entre jamais dans D) ; ses sources (tous les PFL possibles + repli) sont
  alignées ENTRE ELLES, statiquement, pour que passer du repli au PFL ne décale rien — sa latence est donc
  celle du plus long de ces chemins. Coût mesuré (VM sans AVX2, exécuteur C) : +9 % à 64 tranches
  quand un bus d'écoute existe (retards d'alignement permanents), 0 sinon.
- **Mesures par canal** : slot de 13 doubles ; `peak_db_ch`, `rms_db_ch`, `gr_db_ch`, `gate_db_ch` ;
  `peak_db` / `rms_db` / `gr_db` / `gate_db` d'origine = max des canaux (le RMS stéréo n'est plus la
  moyenne des deux : c'est le canal le plus fort).
- **Ordre** : `talkback: {input:{kind:"flow", flow, channels[1|2]}, level_db, dim_db}` + `talk` vivant sur
  chaque `mixminus_out`. Injecté APRÈS la soustraction, la chaîne de sortie et le retard
  d'alignement (jamais retardé ni traité ; un limiteur de la chaîne N-1 ne le limite donc pas) :
  `sortie = programme × dim_db + ordre × level_db`, en rampe. Mono -> stéréo : loi -3 dB du moteur.
  Source = flux MXL ; le micro du navigateur viendra dans un lot ultérieur. Non câblé = aucune opération.
- **États momentanés** (`pfl`, `talk`) : jamais persistés. `/doc` les rend à faux (`/doc?live=1` les
  garde), `/structure/validate` rend un document à persister sans eux, `/structure/apply` reprend les états
  vivants. `/state` publie `listen`, `talkback`, `bypassed`, `sends_off`, `executor`.

## Annexe — section Monitoring (0.4.x, 2026-10-13)

- Moniteur d'écoute sur le bus `listen` : niveau, dim (momentané) + atténuation, mono (−3 dB par
  voie), cut (momentané), `listen_mode` PFL | AFL (prises alignées, changer de mode ne déplace
  aucune autre sortie). Noyau ABI 5.
- Flux déclarés `control.streams` (app/plugin_streams.py) à travers le proxy, sans WebSocket :
  GET /listen.pcm (L16 stéréo 48 kHz, 10 ms par morceau, 4 écouteurs) et POST /talk.pcm (micro du
  navigateur, tampon de gigue 60/250 ms, correction de dérive). Waitress 3.0.2 a send_bytes=1 :
  0,5 ms ajoutée en http comme en tls — ne pas relever send_bytes.
- Banc dell-1 : écoute 2,49 s d'audio reçus en 2,50 s (écart max 22 ms) ; ordre web 1 kHz à −23 dBFS
  dans la seule N-1 visée. **Coût du proxy par requête** : conteneur seul 3,3 ms, via
  l'orchestrateur 17,6 ms p50 / 84 ms p90 — d'où des paquets micro de 40 ms et 6 requêtes en vol
  (30 s : 750 paquets, 1 abandonné, 9 sous-alimentations, tampon ~90 ms).
- Le micro du navigateur exige un contexte sécurisé (interface en HTTPS, mode « tls ») ; l'écoute
  dans le navigateur marche aussi en HTTP (repli ScriptProcessor).
- Décision (2026-10-13) : PAS de monitoring sur la page publique — aucune raison de partager
  l'écoute sans compte. La console 0.4.2 masque la section Monitoring en mode public, le relais
  public des flux est retiré, et le scan refuse un flux déclaré dans `read_endpoints` (seule liste
  que relaie `/p/<jeton>`).
