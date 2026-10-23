# Console audio : plugins tiers — banc de faisabilité n° 0 (co-processus synchrone)

Date : **2026-10-17** (mesures de la nuit du 16 au 17). Statut : BANC, aucune modification de la console,
du moteur ni de l'orchestrateur. Fait suite à l'étude [`CONSOLE_PLUGINS_TIERS.md`](CONSOLE_PLUGINS_TIERS.md)
et à la décision de l'exploitant du 2026-10-16 : la voie « hôte dans un conteneur séparé branché par flux
MXL » (≈ 13 ms + latence du plugin) est jugée trop chère ; existe-t-il un milieu entre « lié au moteur »
et ce coût ?

Code : `tools/bancs/plugins_tiers/` (C pour tout ce qui est temps réel, Python pour piloter).
Résultats bruts : `tools/bancs/plugins_tiers/resultats_2026-10-17/` (un JSON par scénario).

## 0. Réponse courte

**Oui. Le co-processus synchrone tient les chiffres, et de loin.**

| | Mesuré (3 min = 36 000 blocs de 5 ms par cas, SCHED_FIFO) |
|---|---|
| Latence ajoutée | **0 bloc**. Seule s'ajoute la latence propre du plugin (x42-dpl : 64 éch. = 1,33 ms) |
| Coût du transport, moteur et hôte **sur le même cœur** | **p50 ≈ 10 µs, p99 ≈ 33 µs, max 55–68 µs** par bloc (0,2 % à 1,4 % du bloc) |
| Même chose, hôte sur un **autre cœur** | p50 ≈ 47 µs, p99 ≈ 78 µs (réveil d'un cœur endormi en C6) — **moins bien** |
| `kill -9` de l'hôte (24 fois en 2 min) | le moteur **ne rate aucun bloc** ; 6 à 7 blocs passent SECS ; relance en **34 ms** (x42-dpl) à **66 ms** (LSP multibande) |
| Plugin trop lent (3 ms, puis 12 ms, 1 bloc sur 200) | 1 puis 3 blocs secs par incident ; **0 dépassement** du moteur (en in-process : 120 blocs en retard) |
| Plugin bloqué (sommeil infini) | chien de garde 50 ms → 17 blocs secs (85 ms), relance, 0 dépassement |
| Plugin bloqué en **boucle active** sur le cœur du moteur | **piège trouvé** : avec un hôte SCHED_FIFO, le bridage RT du noyau retarde le moteur (25 blocs en retard jusqu'à 26 ms). Corrigé de deux façons mesurées (§3.4) |
| Plantage du plugin en in-process | le moteur meurt (SIGSEGV) — confirmé pour mémoire |
| Coût CPU de l'hôte en plus du plugin | ≈ 0,15 % d'un cœur ; 2 changements de contexte par bloc (400 /s) |

Recommandation (§5) : retenir le **co-processus synchrone dans le conteneur de la console**, hôte sur le
**même cœur**, hôte en **SCHED_OTHER** (ou FIFO strictement inférieur au moteur **et** chien de garde
dans le fil audio), **échéance absolue partagée de 2 ms par bloc** pour l'ensemble des inserts tiers,
budget par insert fixé par qualification (2 × p99,9 mesuré + 100 µs, plafonné à 1,5 ms). Le risque qui
reste n'est plus la latence ni la robustesse : c'est l'**isolation** (le plugin dans le conteneur de la
console voit ce que voit la console, §4.3) et le **branchement dans le plan C** (nouvel opcode, §4.1).

## 1. Ce qui a été construit

```
tools/bancs/plugins_tiers/
  plugcore.c    hôte LV2 minimal (lilv) en bibliothèque libplugcore.so — MÊME code dans les deux modes
  engine.c      « moteur » factice : blocs de 240 éch. stéréo float32 sur horloge absolue (5 ms),
                modes inproc / coproc / async, superviseur + chien de garde, statistiques, JSON
  host.c        hôte co-processus : attend un bloc (futex), le traite, rend
  shm.h         contrat de mémoire partagée (un seul bloc en vol, numéros de séquence)
  run_bench.py  pilote : scénarios, inventaire et sondage de tous les plugins installés
  campaign.sh   campagne complète (≈ 1 h 45)
  Dockerfile    image JETABLE Debian trixie + paquets de plugins libres (main)
```

**Les trois modes** :
- **inproc** : `dlopen("libplugcore.so")` dans le processus du moteur, appel direct par bloc.
- **coproc** (la piste à démontrer) : à chaque bloc, le moteur copie l'entrée dans la mémoire partagée,
  incrémente `req` et fait `FUTEX_WAKE` ; l'hôte, endormi sur `req`, traite, écrit `out`, puis
  `out_seq = done = req` et `FUTEX_WAKE`. Le moteur attend sur `done` avec `FUTEX_WAIT_BITSET` et une
  **échéance absolue** (début de bloc + 2 ms). Échéance dépassée, hôte encore occupé par un bloc
  précédent, ou hôte absent → **le bloc passe SEC** (même latence, aucun trou). Une sortie n'est servie
  que si `out_seq` égale le numéro soumis (jamais la sortie d'un bloc abandonné ou d'un hôte mort).
  Un superviseur (`wait4`) relance l'hôte dès sa mort ; un chien de garde tue un hôte qui garde un bloc
  plus de 50 ms.
- **async** (simulation de la voie de l'étude) : l'hôte tourne sur sa **propre** horloge de bloc,
  décalée de 2,5 ms ; le moteur ne lit le retour du bloc *n* qu'au bloc *n+1*.

**Plugins mesurés en continu** (choisis pour couvrir léger → lourd) : x42-dpl stéréo (limiteur, déclare
64 éch.), LSP Compressor Stereo, LSP Multiband Compressor Stereo x8, Calf Deesser, Dragonfly Room
Reverb, et un « plugin » `null` (recopie) pour isoler le transport.

**Où** : nœud de banc **dell-1** — 2 × Xeon E5-2699 v4 @ 2,2 GHz (Broadwell, 88 fils),
noyau 6.12.96, Debian 13.6, gouverneur `performance`, **C6 actif** (sortie 133 µs), aucun cœur isolé.
Cœur moteur **81**, cœur « autre » **83**, sondes sur **85** (NUMA 1). Conteneur jetable
`--network none --cap-add SYS_NICE --ulimit memlock=-1`, épinglé sur 79-85. Moteur SCHED_FIFO 71,
hôte SCHED_FIFO 70 sauf mention, `mlockall`. Le nœud portait ses conteneurs habituels (charge ≈ 0,5),
non touchés ; ni dl360-1 (et ses cœurs isolés 25-36), ni Horace. Conteneur, image et fichiers supprimés
après la campagne.

## 2. Aller-retour : co-processus contre in-process

Microsecondes par bloc de 240 échantillons stéréo. « In-process » = le coût du plugin seul.
« Surcoût » = aller-retour − temps de `pc_process` mesuré dans l'hôte (le transport pur).
**Aucun bloc sec, aucun bloc en retard dans les 18 cas.**

| Plugin | In-process p50 / p99 / max | Co-proc **même cœur** p50 / p99 / p99,9 / max | Surcoût p50 / p99 / max | Co-proc **autre cœur** p50 / p99 / max | Surcoût p50 / p99 / max |
|---|---|---|---|---|---|
| `null` (recopie) | 0,4 / 0,9 / 26 | 10,1 / 33,3 / 41,4 / 54,7 | 9,7 / 32,5 / 54,3 | 46,9 / 80,2 / 110,8 | 46,4 / 79,4 / 109,8 |
| x42-dpl | 3,9 / 21,2 / 36 | 13,7 / 43,3 / 49,5 / 63,9 | 9,7 / 33,1 / 60,1 | 53,0 / 88,0 / 504,7 | 47,8 / 78,2 / 500,5 |
| LSP Compressor | 16,5 / 45,3 / 79 | 27,6 / 70,7 / 82,4 / 103,6 | 11,1 / 35,1 / 59,1 | 64,4 / 111,0 / 186,9 | 46,2 / 77,4 / 126,5 |
| Calf Deesser | 13,5 / 43,4 / 63 | 23,7 / 68,3 / 74,6 / 87,4 | 9,8 / 32,8 / 53,2 | 63,3 / 111,6 / 145,4 | 46,9 / 77,3 / 131,1 |
| LSP Multiband Comp. x8 | 208 / 506 / 1208 | 225 / 518 / 559 / 600 | 12,4 / 36,5 / 59,4 | 313 / 559 / 638 | 75,5 / 122 / 164 |
| Dragonfly Room Reverb | 108 / 294 / 1114 | 128 / 320 / 964 / 1119 | 10,7 / 36,4 / 68,2 | 168 / 403 / 1175 | 47,9 / 115 / 147 |

Hôte en **SCHED_OTHER** sur le même cœur (moteur toujours FIFO), x42-dpl : aller-retour
17,1 / 51,1 / 60,7 / 91,8 µs, surcoût 13,1 / 41,3 / 81,6 µs — à peine plus cher.

Lectures :
- **Le transport coûte ~10 µs**, quel que soit le plugin. C'est le prix des deux changements de contexte
  (relevé : 36 000 commutations volontaires + 36 000 involontaires de l'hôte en 3 min, soit 2 par bloc).
- **Même cœur bat autre cœur** (10 contre 47 µs) : sur un autre cœur, l'hôte dort en C6 entre deux blocs
  et le réveil coûte une sortie d'état profond + une IPI. C'est aussi la configuration naturelle : la
  console a **un** cœur épinglé (`plugin.json:resources.cores = 1`).
- **La queue est celle du plugin, pas du transport** : le max de la réverbe (1,1 ms) et du multibande
  (1,2 ms) existe déjà en in-process. Le p99 vaut 2,5 à 5 × le p50 partout, y compris pour `null`
  (cache froid après 5 ms de sommeil) : **une échéance se dimensionne sur le max d'un plugin, jamais sur
  sa moyenne** (risque R4 de l'étude, confirmé).
- **CPU** : hôte `null` = 0,18 % d'un cœur ; x42-dpl 0,28 % ; LSP multibande 4,8 % (contre 5,06 % pour le
  même plugin in-process). Le surcoût CPU du co-processus est négligeable ; c'est le plugin qui coûte.

### 2.1 Sans temps réel (SCHED_OTHER partout, comme le fil audio actuel de la console)

Le fil audio de la console est aujourd'hui SCHED_OTHER (seuls ses fils annexes passent en `nice 10`,
`runtime.py`). Même campagne en SCHED_OTHER pour moteur et hôte, 3 min par cas :

| | aller-retour p50 / p99 / max | fin de bloc max | blocs en retard |
|---|---|---|---|
| in-process `null` | 0,4 / 0,9 / 18 | **2 035** | 0 |
| co-proc même cœur `null` | 11,1 / 34,7 / **2 056** | 2 138 | 0 |
| co-proc même cœur x42-dpl | 15,4 / 47,1 / **4 987** | 5 067 | **1** |
| co-proc même cœur LSP multibande | 225 / 511 / 2 275 | 2 356 | 0 |
| co-proc autre cœur LSP multibande | 301 / 559 / 3 459 | 5 333 | **2** (secs) |

Les médianes ne bougent pas ; **la queue passe à 2–5 ms**, et elle existe aussi **en in-process**
(fin de bloc 2 ms pour un plugin qui ne fait rien) : c'est l'ordonnanceur et les voisins (les conteneurs
du nœud ont un cpuset 1-87), pas le transport. Conséquence pour l'intégration : sans temps réel, un bloc
de temps en temps passera sec parce que l'hôte aura été préempté. **La voie co-processus pousse à donner
enfin une priorité temps réel au fil audio de la console** (bénéfice qui dépasse les plugins).

## 3. Pannes : le moteur continue-t-il sans trou ?

Co-processus, moteur et hôte sur le même cœur, échéance 2 ms, chien de garde 50 ms, x42-dpl sauf
mention. « Secs » = échéance / hôte occupé / hôte absent.

| Scénario | Durée | Blocs secs | Max consécutifs | Relance (min / méd / max) | Blocs du moteur en retard | Fin de bloc max |
|---|---|---|---|---|---|---|
| `kill -9` toutes les 5 s (24 morts) | 120 s | 6 / 0 / 152 | 7 | 31,0 / 34,4 / 36,9 ms (dont lilv 28,6 ms) | **0** | 2,07 ms |
| idem, LSP multibande | 120 s | 23 / 1 / 301 | 14 | 62,6 / 66,0 / 67,9 ms (dont lilv 51 ms) | **0** | 2,34 ms |
| plugin lent 3 ms, 1 bloc / 200 | 60 s | 60 / 0 / 0 | **1** | — | **0** | 2,15 ms |
| plugin lent 12 ms, 1 bloc / 200 | 60 s | 59 / 118 / 0 | 3 | — | **0** | 2,14 ms |
| plugin bloqué (sommeil infini), 6 fois | 60 s | 6 / 60 / 36 | 17 | 32,3 / 32,8 / 33,6 ms | **0** | 2,10 ms |
| **in-process**, lent 3 ms | 60 s | — | — | — | 0 (mais bloc à 3,16 ms) | 3,16 ms |
| **in-process**, lent 12 ms | 60 s | — | — | — | **120** | 12,2 ms |
| **in-process**, plantage (SIGSEGV) | — | — | — | — | moteur **mort** (rc −11) | — |

- **Plantage** : le temps de relance est dominé par l'**instanciation lilv** (28 ms pour x42-dpl, 51 ms
  pour LSP), alors même que `LV2_PATH` est restreint au seul bundle du plugin (un `load_all` de 595
  plugins coûterait plus). Un hôte « de réserve » déjà instancié ramènerait la panne à 1–2 blocs ; à
  garder pour plus tard (coût : un plugin chargé deux fois, état à resynchroniser).
- **Lenteur** : le bloc trop long passe sec, l'hôte rattrape au bloc suivant. Rien n'est jamais en retard
  côté moteur : l'échéance le protège. En in-process, la même lenteur se paie en blocs perdus.
- Le plugin voit des **trous** dans son flux (blocs non soumis) : ses états internes (enveloppes,
  réverbe) en gardent une discontinuité. Inaudible derrière le fondu sec ↔ traité déjà présent dans
  l'insert de la console, mais à **compter et alarmer** (comparer à l'intention : insert actif).

### 3.4 Le piège trouvé : boucle active d'un hôte temps réel sur le cœur du moteur

Scénario : le plugin part en boucle infinie **active** (calcul, pas sommeil), l'hôte est SCHED_FIFO 70,
et **tout** le processus (superviseur et chien de garde compris) est confiné au cœur du moteur, comme dans
la console réelle.

| Variante | Blocs secs (5 incidents) | Max consécutifs | Moteur en retard |
|---|---|---|---|
| hôte FIFO, chien de garde SCHED_OTHER | 5 / **950** / 0 | **191** (≈ 1 s) | **25 blocs, jusqu'à 26 ms** |
| hôte **SCHED_OTHER** | 6 / 60 / 36 | 17 | **0** |
| hôte FIFO, chien de garde **dans le fil audio** (tue après 10 blocs occupés) | 6 / 60 / 36 | 17 | **0** |

Mécanisme : un fil FIFO qui ne rend jamais la main fait jouer le **bridage RT** du noyau
(`sched_rt_runtime_us = 950000`) ; le chien de garde SCHED_OTHER n'a plus que les 50 ms de fenêtre par
seconde, et **le bridage frappe tous les fils RT du cœur — donc le moteur lui-même**. Règles qui en
sortent : (1) l'hôte ne doit jamais avoir une priorité RT qui puisse affamer quoi que ce soit sur le cœur
du moteur — SCHED_OTHER suffit (+3 µs) ; (2) la détection du blocage vit **dans le fil audio** (compter
les blocs « hôte occupé » coûte deux lectures atomiques ; `kill()` ne bloque pas), pas dans un fil annexe.

## 4. Ce que l'intégration demandera (non fait, aucune modification de la console)

### 4.1 Dans le moteur
- **Un opcode C** (`OP_EXT` ou nom à choisir, `PLAN_ABI` 6 → 7) qui fait, sans Python, sans GIL, sans
  allocation : copie vers la mémoire partagée, `FUTEX_WAKE`, `FUTEX_WAIT_BITSET` jusqu'à l'échéance,
  contrôle de `out_seq`, **contrôle de finitude** du retour, recopie ou sec. Le GC Python (130 ms déjà
  vus) ne peut pas l'atteindre puisque l'attente est dans le noyau C. Le chemin Python de secours du
  plan (lambdas `Op`) doit avoir son équivalent.
- **Le modèle de latence change** : un insert co-processus a pour latence **celle du plugin seule**
  (`L = latence déclarée`), **au lieu de** `B + M + ext` pour l'insert par flux. La ligne à retard du sec existe déjà
  (`_bypass_ops` avec `L > 0`). L'insert par flux MXL reste valable pour un équipement externe.
- **Inserts multiples** : les attentes s'additionnent dans le bloc (le plan est séquentiel). D'où
  l'échéance **absolue partagée** proposée au §5, et l'idée, plus tard, de soumettre en même temps les
  inserts indépendants d'un même niveau du plan.

### 4.2 Pièges relevés au sondage des 531 plugins (§6)
- **NaN** : ZamEQ2 aux réglages par défaut renvoie 48 000 échantillons non finis en 1 s. Un retour non
  fini doit passer sec et alarmer, sinon il empoisonne le bus.
- **Latences déclarées douteuses** : Calf Limiter déclare 0 alors qu'il a une anticipation ; le
  « Lookahead limiter » SWH déclare 48 024 échantillons (1 s) par défaut ; LSP Multiband Clipper
  5 892 (123 ms). La compensation doit se **vérifier par mesure** (méthode du timecode incrusté), pas se
  croire.
- **Tailles de bloc** : ZamVerb refuse d'initialiser son moteur de convolution à 240 échantillons ; les
  plugins exigeant `powerOf2BlockLength` ne peuvent pas tourner à 240. Prévoir un tampon d'adaptation
  (qui rajoute de la latence) ou les refuser explicitement.

### 4.3 Isolation : le vrai prix d'abandonner le conteneur séparé
Dans le conteneur de la console, l'hôte voit par défaut le système de fichiers du conteneur, le domaine
`/dev/shm/mxl` (tous les flux du nœud) et l'environnement (`MXL_AGENT_TOKEN`). À faire, **non mesuré
ici** : uid distinct, environnement vidé, mémoire partagée passée par descripteur (`memfd`, pas un nom dans
`/dev/shm`), MXL rendu illisible pour cet uid, seccomp, limites (mémoire, processus, fichiers). Docker
ne donne pas d'espace de noms de montage sans privilège : la faisabilité de « MXL invisible pour l'hôte »
dans le même conteneur est **la question ouverte n° 1** de l'étape suivante. Côté licence, la séparation
par processus garde la position de l'étude (le plugin n'est pas lié au moteur GPL) ; la mémoire partagée
ne transporte que des tampons audio et deux compteurs. Ce n'est pas un avis juridique.

## 5. Recommandation

1. **La voie co-processus synchrone tient les chiffres** : 0 bloc de latence ajoutée, ~10 µs de transport
   (p99 ≈ 33 µs, max < 70 µs sur 36 000 blocs), panne d'hôte = quelques blocs secs, jamais un trou ni un
   retard du moteur. Elle remplace la voie « conteneur + flux » pour les plugins tiers ; la voie par flux
   reste celle des équipements externes.
2. **Échéance proposée** : **absolue, 2 ms après le début du bloc**, partagée par tous les inserts tiers
   de la console (40 % du bloc ; le reste est au plan de la console, dont le temps réel par bloc reste à
   relever en service). Par insert, un budget de qualification `min(1,5 ms, 2 × p99,9 + 100 µs)` mesuré
   au chargement ; refus de configurer si la somme dépasse 2 ms ; alarme si le p99 courant dépasse 50 %
   du budget. Exemples mesurés : x42-dpl ≈ 0,2 ms, LSP multibande ≈ 1,2 ms, Dragonfly Room 1,5 ms
   (plafond — sa queue à 1,1 ms est rare mais réelle).
3. **Placement** : hôte sur le **même cœur** que la console, en **SCHED_OTHER** ; chien de garde **dans
   le fil audio** (≥ 10 blocs occupés → `SIGKILL`) ; superviseur de relance dans un fil annexe ; limite de
   relances (3 en 60 s, comme l'étude) puis insert « en panne » et alarme. Le profil `core_pool` de la
   console doit compter la charge des plugins.
4. **Passer le fil audio de la console en temps réel** (SCHED_FIFO) — utile en soi, nécessaire pour que
   les queues de 2–5 ms observées en SCHED_OTHER ne transforment pas des blocs en blocs secs.
5. **Risques restants**, par ordre : isolation dans le même conteneur (§4.3) ; plugins non temps réel
   (allocations : la queue du plugin est la vraie limite) ; latences déclarées fausses ; NaN ; changement
   d'ABI du plan.

Étape suivante proposée : un hôte co-processus réel (LV2 d'abord) branché par un opcode dans une console
de **banc**, mesuré par timecode incrusté ; et, en parallèle, l'essai d'isolation dans le conteneur.

## 6. Inventaire réel du libre sous Linux

**Méthode** : image Debian trixie (main) avec 18 paquets de plugins → **595 plugins LV2** recensés
(`resultats_2026-10-17/inventory.json`). Les **531** qui ont au moins une entrée et une sortie audio ont
été chargés un par un par l'hôte minimal, 1 s chacun, réglages par défaut
(`resultats_2026-10-17/probe.json`) : **494 tournent**, 37 échouent (16 dépassent les 512 ports de mon
hôte, ~19 bibliothèques Invada/SWH dont une dépendance manque dans l'image minimale, 2 Rubber Band R3).
Latence = valeur du port de latence **après** un `run()`, en échantillons à 48 kHz. Coût = µs par bloc
de 240 éch. stéréo sur Broadwell 2,2 GHz (p50 / p99). Licences : fichiers `copyright` Debian.
Les mêmes suites existent aussi en CLAP et VST3 dans Debian (`lsp-plugins-clap/-vst3`,
`dpf-plugins-clap/-vst3`, `dragonfly-reverb-clap/-vst3`, `zam-plugins`) ; le SDK VST3 est empaqueté
(`libvst3sdk-dev`).

### 6.1 Par besoin

| Besoin | Disponible (paquet, licence Debian) | Latence déclarée | Coût p50 / p99 µs |
|---|---|---|---|
| **Réduction de bruit** | **Rien dans Debian** (ni trixie ni sid). Hors Debian : `noise-repellent` (LGPL-3.0+ selon Arch, spectral, ≈ 23 ms à 44,1 kHz selon un empaquetage Fedora ancien), `noise-suppression-for-voice` (RNNoise, GPL-3, LV2/LADSPA/VST3, 48 kHz), DeepFilterNet LADSPA (MIT ou Apache-2.0, latence non documentée : STFT + anticipation). Bibliothèques : `librnnoise0` (BSD-3, sid), `libspeexdsp1` (BSD) | non mesurée | non mesuré |
| **De-esser** | Calf Deesser (calf-plugins, LGPL-2+/GPL) ; MDA De-ess (mda-lv2, GPL-3+, rudimentaire) ; **LSP Deesser + SC Deesser** (LSP ≥ 1.2.34 du 2026-08-15 ; présents en sid 1.2.35, **absents de trixie** 1.2.21) | Calf 0, MDA 0 | Calf 13,8 / 42 ; MDA 1,7 / 4,5 |
| **EQ dynamique** | ZamDynamicEQ (zam-plugins, GPL-2+ ; **une seule bande**, mono + sidechain). Rien d'autre de libre trouvé. (LSP « Matcher », sid, = égalisation d'appariement, pas dynamique — non mesuré) | 0 | 28,6 / 82 |
| **Loudness** | Mesure : x42 meters (EBU R128 3,4 / 9,7 ; true-peak 57 / 157), DR-14. Réglage automatique : LSP Autogain (LGPL-3+, 0, 33,6 / 109). **master_me** (GPL-3, LV2/CLAP/VST3, orienté diffusion, préréglages EBU R128, « sans latence » selon l'auteur) — **hors Debian**, non mesuré. LSP Loudness Compensator = courbes isosoniques, pas de la normalisation (4 096 éch. !) | | |
| **Limiteur** | x42-dpl (GPL) | 64 (1,3 ms) | 4,2 / 21,5 |
| | LSP Limiter / Multiband Limiter (LGPL-3+) | 240 / 480 | 14,6 / 40 ; 129 / 344 |
| | ZaMaximX2 (GPL-2+) ; SWH Fast lookahead (GPL) ; Calf Limiter (déclare 0 : douteux) | 480 ; 240 ; 0 | 272 / 564 ; 4 / 10,6 ; 21,5 / 69 |
| **Compresseur** | LSP Compressor ; x42-darc ; Calf Compressor ; ACE Compressor (Ardour) ; ZamComp/ZaMultiComp ; Dyson | 0 | 17 / 49 ; 7,8 / 25 ; 15,7 / 43 ; 4 / 19 ; 18 / 50 ; 37 / 112 |
| **Multibande** | LSP Multiband Compressor/Expander/Gate x8 ; Calf Multiband Compressor/Limiter/Gate ; ZaMultiCompX2 ; MDA MultiBand ; LSP GOTT ; LSP Multiband Clipper | 0 sauf GOTT 240, Clipper **5 892** | LSP ≈ 210 / 500 ; Calf 116–171 / 312–450 ; Zam 25,6 / 73 ; MDA 3,5 / 9,4 ; GOTT 194 / 453 |
| **Porte / expandeur** | LSP Gate/Expander ; Calf Gate ; ZamGate ; abGate ; ACE Expander | 0 | 15 / 39 ; 9 / 31 ; 26 / 77 ; 0,8 / 1,8 ; 4 / 11 |
| **Égaliseur** | x42-fil4 ; LSP Parametric EQ x8 ; Calf EQ 5/8/12 bandes ; EQ10Q ; ACE EQ ; ZamGEQ31 ; ZamEQ2 (**NaN** par défaut) ; Calf EQ 30 bandes | 0 | 27,5 / 79 ; 71 / 234 ; 12–16 / 64–87 ; 23 / 67 ; 10 / 27 ; 8,4 / 26 ; — ; **326 / 612** |
| **Réverbe** | Dragonfly Room / Hall / Plate (GPL-2+/3+) ; Calf Reverb ; ACE Reverb ; LSP Room Builder / Impulse Reverb (à configurer) ; IR.lv2 ; MVerb ; ZamVerb (**échoue à 240 éch.**) | 0 | 109 / 286 ; 112 / 295 ; 21 / 56 ; 43 / 120 ; 7,4 / 17 |
| **Transitoires** | Calf Transient Designer ; MDA Transient | 0 | 24 / 66 ; 2,2 / 5,6 |
| Hors Debian, non mesuré | Airwindows Consolidated (MIT ; CLAP natif, VST3, LV2 pour 155 effets sur 420) ; Surge XT effets, ChowDSP : non vérifiés | | |

Constat : **le libre couvre très bien la dynamique, l'égalisation et la réverbe ; mal la réduction de
bruit (rien d'empaqueté), l'EQ dynamique (une bande) et le de-esser (Calf, et LSP tout récent)** — soit
précisément les besoins cités par l'exploitant. Les produits de référence pour ces trois-là restent
propriétaires et, pour la plupart, sans version Linux (cf. étude §1.3).

### 6.2 Ce qui serait raisonnable de faire EN INTERNE dans le moteur

Le moteur a déjà `gain`, `filter`, `eq4`, `comp` (avec `key_hpf`), `gate`, `limiter` (anticipation),
`delay` (`modules.json`). À partir de ces briques, sans latence (filtres IIR) :
- **De-esser** : détecteur sur clé passe-bande réglable (4–10 kHz) + mode « bande seule » (réduction
  appliquée à la seule bande aiguë, recombinée). C'est le `comp` avec une clé filtrée et un séparateur
  de bande : **le meilleur rapport valeur/effort**, et il manque au libre.
- **EQ dynamique** : une bande de `eq4` dont le gain suit un détecteur d'enveloppe sur sa propre bande
  (seuil, ratio, attaque/relâche) — 2 à 4 bandes. Même détecteur que `comp`.
- **Multibande** (2–4 bandes) : séparateurs Linkwitz-Riley 4ᵉ ordre (somme passe-tout, 0 latence) +
  `comp` par bande. Coût faible comparé aux ≈ 210 µs des LSP x8.
- **Régulateur de loudness** (« leveler ») : gain lent piloté par la mesure R128 déjà intégrée à la
  console, cible LUFS, plafond — ce que fait master_me, dont la console a déjà la moitié.
- **Expandeur** (porte à ratio) et **transitoires** : variantes de détecteurs existants.
- **À laisser aux plugins tiers** : réduction de bruit spectrale ou par réseau de neurones (FFT ou
  trames de 10 ms → latence ≥ 10–20 ms, CPU élevé ; RNNoise BSD-3 serait intégrable un jour, mais pas
  « pour tous les jours »), réverbes à convolution, traitements créatifs.

## 7. Ce qui n'a pas pu être mesuré

- La console **réelle** : pas d'opcode, pas de mesure par timecode incrusté, pas le temps de calcul du
  plan de la console par bloc (nécessaire pour fixer le budget global).
- L'**isolation** dans le même conteneur (uid, memfd, MXL invisible, seccomp) — §4.3.
- CLAP et VST3 (même principe d'hôte, non écrit) ; plugins hors Debian (noise-repellent, master_me,
  Airwindows, DeepFilterNet, RNNoise) ; LSP Deesser (sid seulement).
- Un hôte « de réserve » pré-instancié (relance en 1–2 blocs au lieu de 7–14).
- Un nœud de production (dl360-1) avec cœur isolé et fréquence épinglée : dell-1 a C6 actif et des
  voisins sur tous les cœurs, ce qui est plutôt **défavorable** — les chiffres devraient s'y améliorer.
- Le paramétrage : tous les coûts du §6 sont aux réglages par défaut (une réverbe ou un multibande
  réglés autrement peuvent coûter beaucoup plus).

## 8. Reproduire

Sur un nœud de banc disposant de Docker et d'un accès sortant (jamais le contrôleur, jamais les cœurs
isolés d'un nœud de production) :
```
docker build -t banc-plugins-tiers:0 tools/bancs/plugins_tiers/
docker run -d --name banc-plugins-tiers --cpuset-cpus 79,81,83,85 --cap-add SYS_NICE \
    --ulimit rtprio=99 --ulimit memlock=-1 --network none banc-plugins-tiers:0 sleep infinity
docker exec banc-plugins-tiers python3 run_bench.py inventory --out results
docker exec banc-plugins-tiers ./campaign.sh 81 83 85        # ≈ 1 h 45
docker cp banc-plugins-tiers:/banc/results . ; docker rm -f banc-plugins-tiers ; docker rmi banc-plugins-tiers:0
```
Les scénarios `fault-spin-onecore-*` et `coproc-same-hostother-dpl` (ajoutés après la campagne) se
lancent par `python3 run_bench.py run <nom> --cpus 81,83 --fifo 70`.

Sources web consultées pour l'inventaire hors Debian : [master_me](https://github.com/trummerschlunk/master_me),
[master_me, Libre Arts](https://librearts.org/2022/09/introducing-master-me/),
[noise-repellent (Arch)](https://archlinux.org/packages/extra/x86_64/noise-repellent/),
[noise-repellent, spec Fedora](https://copr-be.cloud.fedoraproject.org/results/ycollet/linuxmao/fedora-29-x86_64/01139504-noise-repellent-lv2/noise-repellent.spec),
[noise-suppression-for-voice (SUSE)](https://packagehub.suse.com/packages/noise-suppression-for-voice),
[noise-suppression-for-voice (Chimera)](https://pkgs.chimera-linux.org/package/current/main/x86_64/noise-suppression-for-voice),
[deepfilternet-ladspa (Arch)](https://archlinux.org/packages/extra/x86_64/deepfilternet-ladspa/),
[DeepFilterNet](https://github.com/rikorose/deepfilternet),
[LSP, annonce 1.2.34](https://www.lsp-plug.in/?page=news),
[Airwindows Consolidated](https://airwindows.com/consolidated/),
[lv2-airwindows (SUSE)](https://packagehub.suse.com/packages/lv2-airwindows/).
Disponibilité Debian : `apt-cache policy` dans `debian:trixie-slim` et `debian:sid-slim` le 2026-10-16.
