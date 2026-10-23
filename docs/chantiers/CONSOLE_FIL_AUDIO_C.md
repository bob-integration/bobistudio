# Console audio : le fil audio entier en C — plan (2026-10-17)

Décidé le 2026-10-17 (« il me paraît logique que tout l'audio reste en C »), après les plugins tiers.
Plan validé par l'exploitant le 2026-10-17. Avancement au soir du 2026-10-17 :
- étape 1 (banc de référence) : FAITE, §1 bis ;
- étape 2 (préambule du bloc dans le plan) : FAITE — R128 (OP_R128, 0.4.53), fondus, AFV, portes d'inserts,
  échéance des plugins (PLAN_ABI 11, 0.4.54) : un bloc = UN appel `dsp_plan_run` ;
- étape 3 (échanges sans verrou) : modules FAITS, non branchés — ordre web `tools/talk_jitter.c`, écoute / clips
  vocaux / vu-mètres `tools/rt_rings.c` ;
- étape 4 (MXL en C) : module FAIT, non branché — `tools/mxl_rt.c`, vérifié contre la VRAIE libmxl 1.1.0 au banc
  (`tools/bancs/banc_mxl_rt.py`, conteneur 1165), appels libmxl sous la garde SIGBUS (`tools/rt_guard.h`) ;
- étape 5 (le fil lui-même, qui branche tout) : FAITE (0.4.55) — `tools/audio_rt.c` (fil POSIX, pacer dans le
  fil, bascule de plan par pointeur atomique + accusé, relève des vu-mètres par le fil), contrôle Python
  `src/audio_rt.py`, réglage caché `audio_thread: "python" | "c"` (défaut "python"), repli DIT si le C refuse,
  mode synchrone `audio_rt_step_once` ; `tests/verif_console_fil_c.py` (Python == C au bit, fil réel, refus, clés
  de /state). Non éprouvé sur un nœud : le banc A/B de l'étape 6 reste à faire ;
- étape 6 (bascule par réglage, A/B, puis défaut) : à faire.

## 1. Où on en est

Le calcul est DÉJÀ en C : `dsp_plan_run` (tools/audio_plan.c) parcourt le plan en un appel par bloc, et les
inserts plugins passent par `dsp_plugin_run`, en C lui aussi. Ce qui reste en Python, à CHAQUE bloc de 5 ms,
dans `runtime.Console.step()` et `engine.Plan._run_block()` :

| Étape par bloc | Où | Ce qu'elle touche |
|---|---|---|
| Attente de l'échéance (grille TAI, `sleep_until_ns`), retards, réancrage | runtime.step | horloge MXL |
| Bascule de plan (`_pending` → `plan`) | runtime.step, sous verrou | objets Python |
| Lecture de chaque flux d'entrée (`AudioReader.read_from`, transposition, copie) | mxl_io.Io.read via bobimxl (ctypes) | libmxl |
| Ordre web (tampon de gigue `TalkJitter.pull`), clips d'identification vocale | runtime.step | tampons numpy |
| Fondus comptés en blocs, pas d'AFV, portes des inserts, échéance des plugins | engine._run_block | objets Python |
| `dsp_plan_run` | C | pools numpy |
| Accumulation R128 (`r.add_block`) | engine._process_c, Python | |
| Copie des flux publiés (`flows_out … .copy()`) | engine.process_published | numpy |
| Écoute web (`ListenHub.push`) | runtime.step | files Python |
| Sorties : file du pacer, puis un SECOND fil Python qui écrit les tranches de 10 ms à leur heure (`AudioWriter.write`) | mxl_io.Pacer | libmxl |
| Fenêtre CPU, relevé des vu-mètres toutes les N secondes | runtime.step | |

Mesuré au repos sur la console 1160 (19 tranches, 8 bus, exécuteur C, 2026-10-17 après la 0.4.51) :
**0 bloc en retard par minute, 0,33 ms de calcul par bloc de 5 ms**, ramasse-miettes en mode manuel
(0,07 ms au pire). Au repos, Python suffit. Ce qui a coûté des blocs, c'est la CHARGE à côté du fil audio,
toujours par le verrou de l'interpréteur (GIL) :
- ramasse-miettes : 130 ms dans le fil audio (console 0.4.7, corrigé par le mode manuel et `gc.freeze`) ;
- `/doc` interrogé à 10 Hz : de 4 à 17 blocs en retard par minute sur le banc à un cœur (2026-10-16, corrigé
  par une vue de 3 Ko) ;
- compilation d'un plan pendant qu'on joue ;
- le réglage du GIL (`gil_switch_ms`) a été mesuré sans gain net (0.4.47).

Chaque correction a été locale. Le fil en C supprime la CLASSE de problème : le fil audio ne prend plus jamais
le GIL, donc rien de ce que fait Python (HTTP, compilation, ramasse-miettes, sauvegarde de l'état d'un plugin)
ne peut le retarder.

## 1 bis. Banc de référence — FAIT le 2026-10-17 (étape 1)

Console de banc `acon-banc` (vmid 1165, dl360-1, hors des cœurs isolés), MÊME document que la 1160 (19 tranches,
8 bus), console 0.4.52 (`/state.sched` : µs depuis l'échéance, sur les 60 dernières s). Phases de 90 s, charge
envoyée en direct sur :8082 depuis le contrôleur. Script : `banc_charge.py` (scratchpad de la session).

| Phase | Blocs en retard / min | Réveil p50 / p99 / p99,9 / max (µs) | Fin de bloc p99,9 / max (µs) | Calcul moyen |
|---|---|---|---|---|
| repos | 0 | 209 / 310 / 1 227 / 5 752 | 1 581 / 6 116 | 0,36 ms |
| `/doc` à 10 Hz | **10** | 209 / 1 430 / 4 757 / **16 768** | 5 189 / 17 284 | 0,33 ms |
| compilation de plan à 1 Hz (`/structure/validate`) | 1,3 | 206 / 1 418 / 4 987 / 7 105 | 5 527 / 7 615 | 0,32 ms |
| 4 écoutes web | 0 | 205 / 293 / 2 775 / 5 754 | 3 147 / 6 221 | 0,36 ms |
| tout ensemble | **16,7** | 206 / 2 098 / 6 425 / **14 700** | 7 114 / 15 918 | 0,37 ms |

Lecture : le CALCUL ne bouge pas (0,32 à 0,37 ms par bloc de 5 ms) ; c'est le RÉVEIL qui dérape — le fil audio
attend le GIL. La médiane reste à 0,2 ms, toute la dégradation est dans la queue (p99 ×7, max ×3). Un bloc de 5 ms
avec 3 ms de marge de lecture ne tolère pas un réveil à 15 ms. Critère de l'étape 6 : sous « tout ensemble »,
0 bloc en retard et réveil p99,9 sous 1 ms.

## 1 ter. Banc A/B — FAIT le 2026-10-17 (étape 6)

Même document, même nœud (dl360-1, hors cœurs isolés), consoles 0.4.55 d'UN cœur, même script de charge que
§1 bis. A = boucle Python (1165), B = fil C sans priorité (1166), C = fil C en SCHED_FIFO `rt_priority: 10` (1167).

| Phase | A Python : retard/min · réveil p99,9 · max | B fil C : retard/min · p99,9 · max | C fil C + FIFO : retard/min · p99,9 · max |
|---|---|---|---|
| repos | 0 · 1,3 ms · 4,8 ms | 0,7 · 0,85 ms · 4,8 ms | **0 · 17 µs · 0,12 ms** |
| `/doc` 10 Hz | 6 · 3,8 ms · 21,2 ms | 0,7 · 1,7 ms · 2,8 ms | **0 · 20 µs · 1,07 ms** |
| compilation 1/s | 2 · 5,3 ms · 7,7 ms | 19,3 · 2,8 ms · 5,8 ms | **0 · 17 µs · 0,13 ms** |
| 4 écoutes | 0 · 1,4 ms · 6,0 ms | 0,7 · 1,0 ms · 2,8 ms | **0 · 19 µs · 0,23 ms** |
| tout ensemble | 16,7 · 8,0 ms · 26,9 ms | 17,3 · 3,8 ms · 5,8 ms | **0 · 17 µs · 95 µs** |

Lecture : le fil C supprime l'attente du GIL (max 27 → 6 ms) mais, sur UN cœur sans priorité, l'ordonnanceur du
noyau lui retire encore le CPU quand Python compile (B : 19 blocs en retard/min). En SCHED_FIFO (capacité SYS_NICE
déjà accordée par le manifeste), plus rien ne le retarde : **critère de l'étape 6 atteint** (0 bloc en retard,
réveil p99,9 sous 1 ms — ici 17 µs ; fin de bloc max 0,4 ms pour 5 ms de budget). NON mesuré : la boucle Python EN
SCHED_FIFO (elle attendrait toujours le GIL). Décision à prendre par l'exploitant : `audio_thread: "c"` +
`rt_priority` par défaut (le passage d'une console vivante = redéploiement, coupure audio).

## 2. Cible

Un fil POSIX créé par le noyau C (`audio_rt_start`), qui ne touche AUCUN objet Python. Par bloc : attendre
l'échéance → lire les flux MXL → préparer les entrées spéciales → `dsp_plan_run` → R128, vu-mètres, écoute →
écrire les sorties à leur heure. Python ne fait plus que le contrôle : compiler les plans, ouvrir et rouvrir les
flux, servir HTTP, lire les compteurs. Les échanges passent par de la mémoire partagée à un seul écrivain
(anneaux SPSC, mots atomiques, doubles tampons) — le modèle déjà éprouvé avec l'hôte de plugins (`pst`, futex).

Ce qui NE change PAS : le plan (tables, pools) reste compilé par Python et exécuté par le même `dsp_plan_run` ;
les sorties restent bit à bit identiques. C'est la garde principale de tout le chantier.

## 3. Étapes

Chaque étape est livrable seule, garde la console en service, et se vérifie par les tests C = Python au bit
existants (`verif_console_moteur`, `plan_c`, `features`, `plugins_moteur`).

1. **Banc de charge, AVANT tout code.** Sur un nœud de banc (jamais les cœurs isolés 25-36 de dl360-1) :
   blocs en retard par minute et écart à l'échéance (p99,9, max), sous charge : `/doc` à 10 Hz, une
   compilation de plan par seconde, sauvegarde d'état de plugin, 4 écoutes web. C'est la référence que le fil
   C doit battre ; sans elle, on ne saura pas si le chantier a servi.
2. **Le préambule du bloc en C.** Fondus, pas d'AFV, portes des inserts, échéance des plugins, accumulation
   R128 deviennent des opérations du plan (nouveaux OP_*, PLAN_ABI 10). `_run_block` se réduit à un seul
   appel C. Vérifié au bit contre l'exécuteur Python, qui reste la RÉFÉRENCE des tests.
3. **Entrées et sorties spéciales par anneaux.** Ordre web : le fil HTTP écrit dans un anneau, le C le lit
   (la logique de gigue et de dérive de `TalkJitter` passe en C). Écoute web : anneau C → Python. Clips
   vocaux : tampon partagé lu en boucle par le C. Vu-mètres : double tampon publié par le C.
4. **MXL en C.** Python ouvre les lecteurs et les écrivains (bobimxl, inchangé) et passe leurs POINTEURS au
   C, avec les fonctions de libmxl résolues dans la bibliothèque DÉJÀ chargée (`dlsym`, jamais une seconde
   copie de libmxl). Le C lit et écrit ; une erreur ou un flux périmé met un mot d'état du flux à « en
   faute », et c'est le fil de ménage Python qui rouvre, comme aujourd'hui (paliers, recréation après 30 s
   de silence).
5. **Le fil lui-même.** `audio_rt_start` crée le fil (SCHED_FIFO si la capacité est là, état publié sinon),
   attend l'échéance sur la grille TAI, et bascule de plan par pointeur atomique avec ACCUSÉ : Python ne
   démonte un plan qu'après l'accusé (la liste `_retired` existe déjà). Le pacer passe dans le même fil :
   il dort déjà jusqu'à l'échéance, la libération des tranches de 10 ms s'y loge.
6. **Bascule par réglage, mesure, puis défaut.** `audio_thread: "python" | "c"`, caché. Banc A/B de l'étape 1
   sur deux consoles. Le C devient le défaut s'il gagne, puis la boucle Python disparaît du chemin de
   production (elle reste comme exécuteur de RÉFÉRENCE des tests, jamais comme repli silencieux).

## 4. Risques — à trancher

- **SIGBUS.** Un flux MXL recréé pendant qu'on le lit fait lever SIGBUS sur le mapping. Aujourd'hui, Python
  intercepte le signal et rouvre (« Problèmes connus résolus » du CLAUDE.md). Dans un fil C, un SIGBUS non
  traité TUE le conteneur. Proposition : gestionnaire SIGBUS avec `sigsetjmp` propre au fil (variable de
  fil), qui marque le flux en faute et passe la tranche en silence. À éprouver par un test qui détruit un
  flux sous le lecteur.
- **Durée de vie des plans.** Les tables et les pools appartiennent à des tableaux numpy. Le C garde des
  pointeurs : Python ne doit rien libérer avant l'accusé de bascule. Une garde ASan (ou un test qui force la
  bascule en boucle) est à écrire à l'étape 5.
- **ABI de libmxl.** Les pointeurs de fonction viennent du binding Python ; un changement de libmxl change
  leur signature sans que le C le voie. Contrôle de version au démarrage du fil (refus explicite, jamais un
  appel sur une signature inconnue).
- **Observabilité.** Ce que Python lisait dans ses objets (compteurs par flux, état de la grille) doit être
  publié par le C dans une zone partagée, sinon `/state` devient aveugle — l'échec silencieux qu'on combat.

## 5. Volume et ordre

Étape 1 : une demi-journée. Étapes 2 et 3 : deux à trois jours. Étape 4 : deux jours, SIGBUS compris.
Étapes 5 et 6 : deux jours, mesures comprises. Soit environ une semaine et demie de travail, dont chaque étape
reste déployable seule. La protection au rappel reste en attente (`app/routes/__init__.py` pris par une autre
session) et ne bloque rien ici.

## 6. Dettes relevées en route (2026-10-17)

- **R128 (0.4.53)** : commandes et lectures prennent un petit verrou à attente active, partagé avec le fil audio.
  Section critique de quelques dizaines de µs, mais un fil de contrôle préempté en la tenant ferait attendre
  le fil audio. À remplacer à l'étape 5 par une boîte aux lettres de commandes (lue par le fil audio en début
  de bloc) et une lecture par double tampon ou seqlock : aucun verrou dans le fil audio. **RÉSOLU (0.4.55)** :
  `dsp_r128_post` / `dsp_r128_acked` (boîte aux lettres lue en tête de `dsp_r128_add`), `dsp_r128_read` en
  verrou de séquence ; application directe seulement quand aucun bloc ne tourne (`Dsp.r128_async` faux).
- **Tampon de gigue C** : il a été vérifié avec `-ffp-contract=off` (sans quoi le fondu est fusionné en FMA et
  diffère de numpy). À l'intégration dans le noyau, le même drapeau doit s'appliquer à `talk_jitter.c`, sinon
  l'identité au bit avec la référence Python tombe. **FAIT (0.4.55)** : `tools/build_dsp.sh` compile `audio_rt.c`,
  `mxl_rt.c`, `talk_jitter.c`, `rt_rings.c` en `-std=gnu11 -ffp-contract=off` ; `audio_dsp.c` / `audio_plan.c` gardent
  leurs drapeaux d'avant (sons du DSP inchangés au bit).
- **Garde SIGBUS — RÉSOLU le 2026-10-17.** Le banc contre la vraie libmxl 1.1.0 (`tools/bancs/banc_mxl_rt.py`,
  conteneur de banc 1165) a montré que la faute d'un flux tronqué arrive DANS `mxlFlowReaderGetSamplesNonBlocking`
  (tête d'anneau lue dans la mémoire mappée) : processus tué (code -7). Les sources 1.1.0 montrent que lecture,
  ouverture et validation d'écriture ne tiennent ni verrou ni allocation (seul le cas « trop tôt » construit
  un chemin : au pire une fuite) ; `rt_guarded_call` les couvre désormais, l'objet fautif est abandonné. Banc
  relancé : le processus survit. À REVÉRIFIER à chaque montée de libmxl (le raisonnement tient au code 1.1.0).
  La garde doit être installée APRÈS `faulthandler` (sinon `rt_guard_install` renvoie 1 et le fil refuse de
  démarrer).
- **Test** : `verif_console_plugins_tiers` (cas « crash ») a échoué une fois sur quatre lancé à la chaîne avec
  toute la suite, puis 3 sur 3 seul. Instabilité de minutage à surveiller, pas une régression.
