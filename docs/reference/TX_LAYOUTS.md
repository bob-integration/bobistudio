# Layouts TX/RX par carte — isolation des sorties 2110 (chantier)

> Créé 2026-07-13. Contexte : sur le socle narrow (PMD ice + pacing ratelimit), toute création de
> session TX déclenche `rte_tm_hierarchy_commit` qui **stoppe/redémarre le port entier** (~100 ms-1 s).
> Conséquences mesurées (moteur 140, nœud 30) : les sessions TX déjà vivantes peuvent y perdre les
> mbufs de leurs mempools → mort **silencieuse et permanente** (`build ret -207`), invisible du hang
> detector MTL (qui ne vit que dans le chemin burst) comme du backstop mtl_rx (le feeder resynchronise
> et paraît vivant). Aucun filet ne rattrape un TX de prod tué ainsi. Cf. docs/chantiers/DPDK_NARROW.md §7.

## Exigence produit (2026-07-13)

**Une action sur un TX ne doit jamais se voir sur un autre TX** — sauf événement de maintenance
explicitement **déclaré, avertissant et soumis à validation**. Les vrais équipements broadcast pacent
par-flux en FPGA ; sur E810 l'isolation n'existe que si l'arbre RL est **figé au runtime**.

## Décisions actées (utilisateur, 2026-07-13)

1. **Le layout vit dans Réglages**, adossé à la bibliothèque de cartes (`nic_profiles`) — pas dans la
   page Destinations 2110 (déjà chargée). La page **Destinations 2110 affiche** le layout appliqué
   (lecture seule) + lien « Modifier dans Réglages » ; édition inline possible pour les
   **administrateurs** seulement.
2. **Fenêtre de maintenance** retenue (en plus de la confirmation simple) : différer l'application
   (« appliquer à HH:MM » / grouper les changements en attente) pour ne payer qu'un seul blip.
3. **Compositions libres** validées par budget (queues vidéo+audio+ANC ≤ `rl_tx_cap`, BP ≤ port,
   `narrow_ok`), **sauvegardables** et déployables ; le système fournit des **exemples** (presets
   suggérés par modèle de carte, non contraignants).

## Les 5 étages

### Étage 0 — Filet : patch libmtl « famine builder → auto-récupération » ✅ LIVRÉ (0.48.0)
Si `rte_pktmbuf_alloc_bulk` échoue en continu hors commit (`inf->resetting`), déclencher la
récupération EXISTANTE de MTL (`st20_tx_queue_fatal_error` vidéo / `st_audio_queue_fatal_error`
audio : purge rings + nouvelle queue + reset mempool). Une mort silencieuse devient une coupure
auto-réparée sur la seule session touchée. Patch `patch_tx_builder_famine_recovery.py` (penser aux
**3 endroits** : fichier + Dockerfile + stage builder `app/routes/images.py`).

**Le filet n'a réellement tenu qu'en 0.48.0** — les 0.44.x→0.47.0 étaient neutralisées par deux
bugs, tous deux mesurés au banc (moteur 140, nœud 30) et corrigés ensemble :

1. **Auto-deadlock du lcore** (le vrai tueur, présent depuis 0.44.0). `tx_audio_sessions_tasklet`
   tient `mgr->mutex[sidx]` (`rte_spinlock`, pris par `tx_audio_session_try_get`) pendant TOUT
   l'appel au builder ; `st_audio_queue_fatal_error` reboucle sur toutes les sessions du mgr avec
   `tx_audio_session_get()` = `rte_spinlock_lock()` **bloquant**. `rte_spinlock_t` n'est pas
   récursive ⇒ **le lcore spinne sur son propre verrou, définitivement**, dès la 1ʳᵉ famine audio.
   Preuve : backtrace gdb de `mtl_sch_0` (état R, 100 % CPU, figé 10 h) `#0 st_audio_queue_fatal_error
   ← #1 tx_audio_session_tasklet_frame ← #2 tx_audio_sessions_tasklet`. Tout le tableau clinique en
   découlait : lcore mort ⇒ toutes les sessions de ce sch à 0 fps ; `stat_build_ret_code` figé à
   -207 et réimprimé toutes les 10 s par le thread de stats (les « 420 -207 / 10 min » n'étaient
   PAS 420 famines, mais UNE famine figée réimprimée) ; le filet lui-même (famine check + zombie
   retry) vit dans la boucle du lcore mort ⇒ plus jamais atteint. Le call site natif de libmtl
   (`st_audio_transmitter.c:58`) est un tasklet différent sans mutex de session — upstream est sain,
   c'était bien NOTRE point d'appel qui était illégal.
   **Fix** : récupération audio **différée hors spinlock** — le builder ne fait que poser une
   demande (`mgr->bobi_famine_pending[port]`), un consommateur en FIN de `tx_audio_sessions_tasklet`
   (après tous les `tx_audio_session_put`, seul point du lcore sans verrou tenu) applique le
   garde-fou global et appelle la récupération. Le chemin vidéo est sain tel quel
   (`st20_tx_queue_fatal_error` ne prend aucun mutex de session).
2. **Chaque récupération brûlait une file TX HW.** `mt_dev_tx_queue_fatal_error()` ne fait QUE poser
   `tx_queue->fatal_error = true` (aucun reset matériel) et `mt_dev_put_tx_queue()` ne l'efface
   jamais, alors que `mt_dev_get_tx_queue()` skippe toute file marquée ⇒ ressource bornée (64 files
   sur E810) épuisée en quelques minutes par un filet qui retente → `get new txq fail` → sessions
   mortes pour de bon. **Fix** : `put` efface `fatal_error` (la file est de toute façon entièrement
   ré-initialisée par le `get` suivant : `set_rl_rate` → commit TM → stop/start du port).
   ⚠ Ceci **ré-explique a posteriori docs/chantiers/DPDK_NARROW.md §7** (« les `st20_tx_queue_fatal_error`
   s'accumulent sous la rafale de commits RL → backstop TX FIGÉ → exit, q33 à tx_q=34, q42 à
   tx_q=44 ; ce n'est PAS le mur des leaves ») : c'était un épuisement de files par quarantaine
   cumulative, mal attribué à l'époque.

**Banc 0.48.0** (moteur 140, cold-batch 6 TX, `tx_fallback=black`) — AVANT : slots `[0, —, —, 0, —, —]`,
420 × `build ret -207` / 10 min. APRÈS : **6/6 slots vidéo à 50 fps + 6/6 audio à 1000 fps**,
5 récupérations au boot (espacées de 10 s = garde-fou global) **toutes réussies**, 0 résurrection
nécessaire, **`build ret -207` = 0 sur 10 min glissantes**, 0 `get new txq fail`, tous les lcores vivants.

> ⚠ **PÉRIMÉ depuis 0.50.0** (cf. « ★★ La cause première : TROUVÉE ET SUPPRIMÉE » plus bas) — le
> filet reste en ceinture mais **ne se déclenche plus au commit TM** (0 déclenchement mesuré).

**Cause première de la vidange — toujours ouverte, mais localisée.** La télémétrie DPDK montre que
les mempools d'origine (`recovery_idx 0`) restent **alloués et vides** (0-3 mbufs libres sur 2047)
à côté des pools `_1_` neufs et sains (1275-1951 libres). Une session en régime établi n'utilise que
~770 mbufs ⇒ **~1280 mbufs par session sont perdus pendant la fenêtre de commits du cold-batch**, et
restent piégés dans l'ancien pool (`rte_mempool_free` ne peut pas le rendre tant que des mbufs sont
in-use). Piste dominante, non prouvée formellement : les mbufs déjà postés dans les **descripteurs
TX** au moment du stop de port du commit TM sont perdus sans free (memset de `ice_reset_tx_queue`).
Le filet les rattrape désormais ; supprimer la chute reste préférable à la rattraper.

### Étage 1 — Layout TX déclaré + arbre statique au boot ✅ LIVRÉ (cb36565, vérifié au banc 2026-07-14)
Au deploy du moteur : créer TOUTES les sessions du layout (flag `provisioned` du controller, déjà
codé mais jamais posé), **silencieuses d'abord**, brancher les feeders (fallback black, câbles)
seulement une fois l'arbre complet. Activation/désactivation d'une sortie = swap de source, zéro
commit. Orchestrateur + controller.py seulement, pas de rebuild libmtl.
UI : layout défini dans **Réglages** (section carte/nœud, à côté des réserves `node_interfaces`),
affiché en lecture seule sur Destinations 2110 (+ édition admin), état « appliqué / en attente »,
bouton « Appliquer le layout » = événement de maintenance (étage 2).

### Étage 2 — Classification des actions + validation avec avertissement ✅ LIVRÉ (2026-07-14)
Module : `app/tx_maintenance.py`. Le verdict est **CALCULÉ**, jamais codé en dur :

> **Perturbatrice ⟺ l'action fait apparaître une signature de session TX _vidéo_ qui n'existait pas,
> sur un port en rate limiter** (`node_interfaces.pmd=dpdk` + pacing `rl`). Tout le reste est sûr.

Trois faits **mesurés au banc** (moteur 140, nœud 30 ; discriminant = `mt_dev_get_tx_queue` dans les
logs = `set_rl_rate` → `rte_tm_hierarchy_commit` → stop/start du port) :

1. **Seule la vidéo commit.** libmtl ne pose une feuille RL que si le débit demandé est non nul :
   `if (inf->tx_pacing_way == RL && bytes_per_sec)` (`mt_dev.c:1551`). L'audio (st30) et l'ANC (st40)
   demandent 0 → file obtenue **sans** `set_rl_rate` → **aucun commit**. Câbler une sortie crée ses
   sessions audio + ANC : **0 commit mesuré**. (L'hypothèse inverse — « l'audio/ANC non provisionnés
   trouent l'étage 1 » — a été **contredite au banc**.)
2. **Le swap gratuit n'existe que si les formats CONCORDENT.** Le câblage pousse le format de la
   SOURCE dans la session (`:8082/input`) : source ≠ format provisionné du slot ⇒ signature changée
   ⇒ session recréée ⇒ commit (**mesuré : +2, puis +9 avec la casse collatérale du filet famine**).
   Formats concordants ⇒ **0 commit** (swap `tx_set_source`). ⇒ **c'est la justification de l'étage 3**.
3. **Un format poussé sur un slot CÂBLÉ est inerte** (le push envoie 0, la source gouverne) → sûr.

Le gate (`_tx_gate`, `app/routes/mtl_engine.py`) répond en 409 `needs_confirm` + `verdict` qui **NOMME
les sorties** qui vont figer (dédupliquées par sortie, pas par session) ; la modale
(`window.txMaintConfirm`, `static/scripts.js`) est partagée par Destinations 2110 et Câbles.
Permission `containers.deploy`. Sur un port **AF-XDP** (cas d'Horace, prod) tout est sûr et l'UI le dit.

**Fenêtre de maintenance** : table `tx_pending_changes` (bac persistant), routes
`/api/tx-maintenance*`, planificateur `tx_maintenance.start_scheduler` (main.py). ⚠ Le commit est fait
**par session recréée**, pas par action : grouper 3 actions sur 3 sorties différentes coûte toujours
3 recréations ; grouper 3 actions sur **la même** sortie n'en coûte **qu'une**. Le bénéfice réel =
fusion par sortie + **une seule fenêtre annoncée** au lieu de N perturbations dispersées.

### Étage 3 — Gating de format sur les TX + insertion d'UDC ✅ LIVRÉ (2026-07-14)
Gate `cabling._tx_slot_mismatch` : format RÉEL du flux (`_flow_def_format`, source de vérité) vs
format DÉCLARÉ du slot (`tx_slots[i]`). Écart ⇒ 409, **trois issues, jamais « forcer »** (un TX qui
annonce X et émet Y est une non-conformité 2110) : insérer un UDC / aligner la sortie sur la source
(→ circuit de l'étage 2) / annuler. Réglages `tx_format_gating`, `tx_format_autoudc`.

**Les 6 axes qui changent RÉELLEMENT la signature** (`compute_sig` ∩ ce que `controller.py:/input`
pousse) : `width, height, fps (TRAME), scan, field_order, bit_depth` → `tx_maintenance.SIG_FORMAT_AXES`.
La **CHROMA n'en fait PAS partie** (constante d'image du moteur, absente de compute_sig) : elle ne
bloque plus, elle avertit. *Un garde-fou qui crie pour rien est aussi nuisible qu'un garde-fou absent.*

**Mesuré au banc** (moteur 140, nœud 30, quiescence 45-70 s ; discriminant `mt_dev_get_tx_queue`) :

| Action | Commits |
|---|---|
| Câble **discordant** (mvi50 1080i25 → slot 720p50), sans gate | **2** (1 direct @2,176 g + 1 collatéral famine) |
| Même câble, **gate actif** → refusé (axes nommés) | **0** |
| Câble **concordant** (mvi50 1080i25 → slot 1080i25) | **0** ✅ la promesse de l'étage 1 tient |
| Insertion d'UDC devant un slot **silencieux** | 1 (+ famine) — voir ci-dessous |

⚠ ~~Une session provisionnée SILENCIEUSE n'a pas de feuille RL~~ — **FAUX, RÉFUTÉ AU BANC LE
2026-07-14** (cf. « Feuille RL d'un slot silencieux » ci-dessous). Les lignes `get_tx_queue …
speed 0.000000g` sont les sessions **AUDIO** (st30, qui demandent 0), jamais la vidéo. Une session
vidéo provisionnée silencieuse **A** sa feuille RL, **au débit nominal de son format déclaré**, dès
la création — et la première activation réelle coûte **0 commit** (mesuré). Le commit résiduel de
l'insertion d'UDC ci-dessus vient du **format** poussé par le câble (signature changée), pas d'une
feuille absente : c'est précisément ce que le gate de format supprime.

**Dérive de source en exploitation** (`app/tx_format_watch.py`, thread depuis `main.py`) : alerte
**d'abord** (nomme la sortie, le format annoncé et le format reçu), insertion d'UDC **ensuite**.
Deux pièges corrigés, tous deux constatés au banc : (1) deux slots tirés d'une même source créaient
deux UDC de **même hostname** = deux écrivains sur le même flux MXL (→ hostname par slot) ; (2) un
UDC devant un UDC (**cascade**) → interdit, on reconfigure celui qui est en place.

★ **Le re-push `deploy.py` (« flux source recréé ») clobbait `scan`/`field_order` avec le format
DÉRIVÉ** → signature changée → commit TM **sans qu'aucun humain n'ait rien demandé** (mesuré :
1 commit @2,176 g = 1080p50 à la bascule de la source). `docker_driver.tx_payloads` **gèle** désormais
l'identité d'un slot dont la source a dérivé : le slot reste sur ce qu'il ANNONCE, et c'est le
watcher (UDC) qui rétablit une source concordante.

### Feuille RL d'un slot silencieux — la question tranchée (banc 2026-07-14, moteur 140, nœud 30)

**Question** : faut-il patcher libmtl pour pré-provisionner la feuille de pacing au débit nominal,
afin que la PREMIÈRE ACTIVATION d'une sortie ne paie plus de commit ? **Réponse : non — c'est déjà
le cas, l'étage 1 le fait, et la première activation coûte DÉJÀ 0 commit.** Aucun patch requis.

**Preuve par le code** (libmtl @32b1b4e9) :

| Fait | Fichier:ligne |
|---|---|
| Le débit d'une session vidéo TX est posé **à la création**, jamais à la 1ʳᵉ trame : `flow.bytes_per_sec = tv_rl_bps(s)` dans `tv_init_hw` (appelé par `st20p_tx_create`, avant toute émission) | `lib/src/st2110/st_tx_video_session.c:2676` |
| `tv_rl_bps` = **fonction pure du format déclaré** (`st20_pkt_size × st20_total_pkts × fps_tm`, + `reactive` si entrelacé ≤576) — aucune dépendance à la source, au contenu ou au trafic réel | `st_tx_video_session.c:82` |
| Le court-circuit à débit nul (`&& bytes_per_sec`) ne concerne QUE les demandeurs à 0 : **audio (st30) et ANC (st40)**. La vidéo ne passe jamais par là | `lib/src/dev/mt_dev.c:1551` |
| `dev_tx_queue_set_rl_rate` sort **avant le commit** si le débit ne change pas (`if (bps == tx_queue->bps) return 0;`) → un re-set au même débit est gratuit | `lib/src/dev/mt_dev.c:706` |
| Le débit RL n'est **jamais ré-ajusté au runtime** : `mt_dev_set_tx_bps` n'a que deux appelants, tous deux au moment de la création (`tv_train_pacing`, `st_tx_audio_session`) | `mt_dev.c:1788`, `st_tx_video_session.c:487` |
| Côté moteur : la source **n'est pas dans la signature** d'une session TX → l'activer/la changer = `tx_set_source` (swap), pas de `st20p_tx_create` | `plugins/2110_io/mtl_rx.c:2328` |
| Côté orchestrateur : tout slot avec mcast+port est poussé `provisioned=True` (réglage `tx_layout_provisioning_enabled`, ON par défaut) → session + feuille RL créées **à la déclaration** | `app/docker_driver.py:1216` |

⇒ **le débit réel est identique au nominal pré-provisionné, par construction** : il est calculé du
format et de rien d'autre. Il n'y a pas de « débit réel » qui pourrait diverger et re-commiter.

**Mesures** (moteur 140 en RL/dpdk, 6 slots, quiescence 60-70 s entre actions ; discriminant
`mt_dev_get_tx_queue` = `set_rl_rate` → `rte_tm_hierarchy_commit`) :

| Action | Commits | Détail |
|---|---|---|
| Quiescence (repère) | **0** | voisins s1/s2/s3/s5 à 50 fps |
| **Création/déclaration** d'une sortie silencieuse (slot 4 → 1920×1080p50, sans source) | **3** | 1 direct (`q4 speed 2.176012g` = débit **nominal** du format, sans une seule trame émise) + 2 collatéraux (famine `-207` → récupération étage 0 sur 2 voisines) |
| Retour au **silence** (source retirée, sortie conservée) | **0** | s4 50 → 0 fps, feuille RL conservée, voisines intactes |
| **PREMIÈRE ACTIVATION RÉELLE** (câble d'une source externe `avsync` 1080p50 concordante) | **0** ✅ | s4 0 → 50 fps en ~4 s, **2178,1 Mb/s émis** = exactement la feuille pré-posée (2,176 g) ; aucun `st20p_tx_create`, aucune famine ; voisines s1/s3/s5 à 50 fps sans discontinuité |

**Corollaire** : le coût d'un commit est intégralement payé au moment **choisi** (déclaration de la
sortie, fenêtre de maintenance) ; l'exploitation (activer, couper, router) est **gratuite**. C'était
l'objectif de l'étage 1 : il est atteint et vérifié.

### ★★ Étage 0-bis — 2ᵉ mort collatérale : `build ret -203` PERMANENT (trames piégées « inflight »)
**Code livré 0.49.0 — ⚠ NON VALIDÉ AU BANC** (moteur 140 occupé par le banc HT ; recette § plus bas)

**Le symptôme.** Au commit de maintenance ci-dessus, une voisine qui émettait à 50 fps est tombée à
**0 fps définitivement**, source fraîche (latence 3,3 ms), file bien récupérée par le filet étage 0.
Signature : `build ret -203` = `STI_FRAME_APP_GET_FRAME_BUSY` (`lib/src/st2110/st_err.h:35`) —
réimprimé toutes les 10 s à vie. Reproduit 2×. Log du banc (2026-07-14) :

```
17:10:02  bobi_famine_check(0) … triggering queue fatal recovery   (voisines : -207, filet OK)
17:10:12  TX_VIDEO_SESSION(9,0:bobi_mtl_vtx_sl): build ret -203    ← la victime
17:10:22  … -203      17:10:32  … -203      17:10:42  … -203   (indéfiniment, 0 fps)
```
La victime n'a **jamais** de ligne de récupération : elle ne fait aucune famine mempool.

**Le mécanisme, PROUVÉ (libmtl @32b1b4e9).** Ce n'est pas l'alloc qui échoue, c'est le **feeder** :
`-203` = `ops->get_next_frame()` rend `-EBUSY` (`st_tx_video_session.c:1876`).

| Fait | Fichier:ligne |
|---|---|
| Une trame TX n'est rendue à l'app QUE par le refcount des mbufs : `tv_tasklet_frame` incrémente `frame->refcnt` | `st_tx_video_session.c:1906` |
| Chaque mbuf de charge utile prend une ref **extbuf** sur `frame->sh_info` | `st_tx_video_session.c:1255` |
| La **dernière** libération de mbuf déclenche `tv_frame_free_cb` → `tv_notify_frame_done` → callback app. **C'est le SEUL chemin de retour d'une trame.** | `st_tx_video_session.c:114`, `:91` |
| Le stop de port du commit TM perd les mbufs postés dans les **descripteurs TX** sans free (`ice_reset_tx_queue` memset le sw_ring) ⇒ ref extbuf **jamais rendue** ⇒ `refcnt` reste à 1 **pour toujours** ⇒ trame jamais rendue ⇒ l'app n'a plus de slot libre ⇒ plus rien à donner ⇒ `-EBUSY` à chaque passe | (= la **même fuite** que celle qui vide les mempools, cf. « cause première de la vidange » ci-dessus, **vue de l'autre côté**) |

**Pourquoi le filet étage 0 ne pouvait RIEN.** libmtl **sait** rendre les trames piégées — le bloc
« `stop frame %u` » (`tv_notify_frame_done` + dec refcnt + `rte_mbuf_ext_refcnt_set(sh_info, 0)`)
existe dans `st20_tx_queue_fatal_error` (`st_tx_video_session.c:4095-4107`). Mais il n'est
atteignable **que** par une récupération de **queue**, elle-même déclenchée par une famine d'**ALLOC**
(`-207`) ou par le hang detector de **burst**. Une session dont le mempool tient encore mais dont les
**trames** sont piégées ne déclenche **ni l'un ni l'autre** : *le seul chemin de guérison était
inaccessible depuis le seul symptôme qu'elle produit.* ⇒ ce **n'est pas** un flag jamais reclearé
(famille `resetting`) : c'est un **compteur de références jamais rendu**, et un chemin de guérison
sans déclencheur.

**Le fix (2 couches).**
1. **libmtl — `patch_tx_frame_inflight_reclaim.py`** (⚠ 3 endroits : fichier + `Dockerfile` + stage
   `app/routes/images.py` ; **doit s'appliquer APRÈS** le patch famine, ses ancres = son état de
   sortie). Sur `-203` **continu > 2 s** hors `inf->resetting`, scan des trames de la session :
   - ≥ 1 trame avec `refcnt != 0` ⇒ **piégée** (à 50 fps, une trame en vol > 2 s est une
     impossibilité physique) ⇒ **rappelée** exactement comme le fait `st20_tx_queue_fatal_error`,
     mais **SANS récupération de queue** : **zéro commit TM**, zéro stop de port, **zéro nouvelle
     victime**. C'est le point clé — étendre la récupération de *queue* à ce cas aurait fait payer un
     commit (donc de nouvelles victimes) pour un problème qui n'est pas côté queue.
   - aucune trame en vol ⇒ famine **applicative** légitime (slot silencieux, pas de source) ⇒ **rien
     à faire**, et on le **dit** (une fois par épisode — pas de repli silencieux, pas de spam).
   - ⚠ le helper tourne sous le **spinlock de session** de `tvs_tasklet_handler` et n'appelle **rien
     de bloquant** : `tv_notify_frame_done` est *exactement* ce que le chemin normal
     (`tv_frame_free_cb`) exécute déjà depuis ce même lcore à chaque trame émise. (Piège n°1 du
     chantier — un appel bloquant depuis une tasklet a figé un lcore 10 h en 0.44→0.47.)
2. **`mtl_rx.c` — le watchdog anneau slice était AVEUGLE.** Le gate `all_busy` (0.40.1) exigeait que
   **tous** les slots soient occupés. Or le slot piégé reste `stat=1` pour toujours **pendant que les
   autres finissent d'être émis et repassent `stat=0`** ⇒ `all_busy` faux ⇒ **watchdog jamais
   déclenché**, worker bloqué à vie sur ce seul slot (`prod` == slot piégé). Bon critère : « **mon**
   slot de production ne se libère pas depuis 2 s » (à 50 fps, 20 ms attendus). Filet de dernier
   recours côté app, pour toute perte de `notify_frame_done` qui ne laisserait **pas** de refcnt.

**Non couvert, dit explicitement** : (a) l'**audio** (st30) produit le même `-203`
(`st_tx_audio_session.c:745`) et la même fuite peut y piéger des trames — non traité (il faudrait le
même différé hors spinlock que la v4 du filet famine ; **non observé au banc à ce jour**) ; (b) la
**fuite elle-même** n'est pas supprimée, seulement **rattrapée** — seul l'**étage 4** la supprimerait.

**Recette de validation (à lancer moteur libre)** — cf. « Protocole de recette » en fin de document.

### ★★ La cause première : TROUVÉE ET SUPPRIMÉE (0.50.0) — et ce n'était **pas** DPDK

**L'hypothèse du chantier était FAUSSE.** « Le stop de port du commit TM perd les mbufs postés dans
les descripteurs TX sans les libérer (`memset` de `ice_reset_tx_queue`) » : c'est **faux sur DPDK
26.03**, la version que build cette image (`versions.env` du clone MTL). Le PMD `ice` **libère bien** :

| Fait | Fichier:ligne (DPDK v26.03) |
|---|---|
| `ice_tx_queue_stop()` appelle `ci_txq_release_all_mbufs(txq, false)` **AVANT** `ice_reset_tx_queue(txq)` | `drivers/net/intel/ice/ice_rxtx.c:1196` puis `:1197` |
| ce helper libère **tout** le `sw_ring`, entrée par entrée (`rte_pktmbuf_free_seg`), en scalaire comme en vectoriel | `drivers/net/intel/common/tx.h:360-393` |
| le chemin scalaire (celui de MTL : `MULTI_SEGS` activé, `mt_dev.c:968`) pose **un segment par entrée** de `sw_ring` — donc un `free_seg` par entrée est complet | `drivers/net/intel/common/tx_scalar.h:512` |
| `ice_dev_stop()` stoppe **chaque** queue TX | `drivers/net/intel/ice/ice_ethdev.c:2886` |

⇒ **aucun patch DPDK n'était justifié** (l'étage 4 « alternative moins chère » est donc caduc).

**La fuite venait de NOTRE propre patch** `patch_tx_hang_resetting_guard.py` (présent depuis 0.43) :
pendant la fenêtre de commit (`inf->resetting`), `video_trs_burst_fail()` retournait **`nb_pkts`** et
`st_audio_trs_burst_fail()` retournait **`1`** — c'est-à-dire **mentir à l'appelant** (« ces paquets
ont été émis »). Or les appelants ne ré-empilent que ce qui n'a **pas** été émis :
`video_burst_packet()` (`if (tx < bulk)`), les branches `trs_inflight`/`trs_inflight2`
(`trs_inflight_num -= tx`) et le tasklet audio (`trs->inflight[port] = NULL`). Les pointeurs étaient
donc **lâchés sans `rte_pktmbuf_free`**, à chaque burst, pendant **toute** la durée du stop de port
(mesuré : **2231 ms** sur un commit de maintenance). Une cause, **les deux morts** : mbufs hdr perdus
⇒ mempool vidé ⇒ `-207` ; mbufs de charge utile perdus ⇒ ref extbuf sur `frame->sh_info`
(`st_tx_video_session.c:1255`) jamais rendue ⇒ trame jamais rendue ⇒ `-203` permanent.
*Le transmetteur **ANC** (`st_ancillary_transmitter.c:64-66`), qui garde son pkt en inflight quand le
burst rend 0, n'a **jamais** fui : c'était le contre-exemple sous nos yeux.*

**Le fix (`patch_tx_reset_no_drop.py`, 0.50.0)** : pendant le reset, le burst rend **0** = « rien
émis, queue pleine » — sémantique que **tous** les appelants savent déjà traiter. Les paquets restent
en inflight/dans le ring et sont ré-émis au redémarrage du port. Zéro mbuf détruit, zéro trame
complétée prématurément. La ligne qui repousse `last_burst_succ_time_tsc` (anti hang-detector) est
conservée. Observabilité : une ligne par épisode, chiffrée —
`bobi: reset window over after <N> ms, <B> burst(s) deferred, 0 mbuf lost`.

**Banc (moteur 140, nœud 30, 2026-07-14 — image 0.51.0 = 0.49 + 0.50 + fix CNI)**

| Mesure | AVANT (0.48.0, live) | APRÈS (0.51.0) |
|---|---|---|
| TX vidéo à 50 fps | **5/6** (slot 2 **mort à 0 fps**) | **6/6** |
| `build ret -203` / 20 min | **120** (1 session piégée, réimprimé à vie) | **0** |
| `build ret -207` | 0 (rattrapé par le filet 0.48) | **0** |
| Filet famine (étage 0) déclenché | 5 au boot (0.48.0, cold-batch) | **0** |
| Filet rappel de trames (0-bis) déclenché | n/a (jamais compilé) | **0** |
| Commits TM (`mt_dev_get_tx_queue`) | — | 10 au boot (cold-batch), 1 sur l'action de maintenance, 3 au retour |
| Fenêtre de commit la plus longue | — | **2231 ms**, `1 097 755 burst(s) deferred, 0 mbuf lost` |
| Voisines pendant/après le commit | tombaient à 0 fps définitivement | **toutes à 50 fps**, sans discontinuité |
| Soak 5 min après 4 commits | — | 0 `-207`, 0 `-203`, 0 filet, 6/6 à 50 fps |

**Sort des filets** : **gardés en ceinture** (ils couvrent d'autres pertes possibles de mbufs — link
down, reset PMD, wedge de burst) mais ils **ne se déclenchent plus** au commit TM. Le critère de
succès « la fuite est morte » est donc rempli : *plus personne ne nous sauve, parce que plus personne
n'a besoin de nous sauver.*

**Non vérifié** : la branche « app starvation » du filet 0-bis (slot déclaré **sans** source) n'a pas
pu être exercée telle quelle — le banc tourne en `tx_fallback=black`, donc un slot sans source est
**alimenté** (mire noire) et n'affame jamais le feeder. Le test négatif a été fait dans sa version
disponible : le slot 4 (déclaré, **sans câble**) est resté **intact à 50 fps** avant/pendant/après les
commits, et **aucun** rappel de trame n'a été déclenché chez lui.

### Étage 4 (réserve) — Isolation totale : matrice de queues pré-shapées
Dans `nic_profiles` : matrice de classes de débit par modèle (ex. E810-C 41 feuilles : N×2,2G vidéo
1080, M×1,1G 720, X audio, Y anc). Toutes les queues liées à leurs shapers **au boot** ; un
changement de format = **changer de queue** (matching de profil) au lieu de recommiter l'arbre.
Patch libmtl profond (attribution de queue par matching). À scoper seulement si les étages 1-3
laissent trop de commits résiduels en pratique.

**Verdict argumenté (2026-07-14) : PAS MAINTENANT, mais ne pas le classer sans suite.**

*Contre (pourquoi ce n'est pas la priorité)* : les étages 1-3 ont ramené l'exploitation courante à
**0 commit prouvé** (activer / couper / router / câbler à format concordant). Le commit résiduel est
confiné à un **acte déclaré** (créer une sortie), déjà encadré par la fenêtre de maintenance. L'étage
4 est un patch libmtl **profond** (l'allocateur de queues devient un matcher de profils de débit ;
il faut aussi figer le fan-out de l'arbre TM et gérer l'épuisement d'une classe) — soit le patch le
plus gros du chantier, sur la brique la plus fragile, pour supprimer un événement **rare et choisi**.
Et l'étage 4 **ne rend pas les filets inutiles** : le hang detector de burst, la famine et le rappel
de trames restent nécessaires pour les autres causes de perte de mbufs (link down, reset PMD).

*Pour (ce qui plaide encore pour lui)* : les deux modes de mort collatérale (`-207`, `-203`) sont
**deux symptômes de la MÊME fuite** — les mbufs perdus au `ice_reset_tx_queue`. On les **rattrape**,
on ne les **supprime** pas ; chaque commit reste une loterie qu'on gagne parce qu'un filet a tenu.
Tant qu'un commit peut survenir en antenne, il reste un risque **résiduel non nul** sur les voisines.

*Décision recommandée* : **d'abord valider 0.49.0 au banc** (le filet couvre-t-il les 2 modes de
mort ?), **puis compter les commits en exploitation réelle sur une semaine**. Si le compte est ~0
hors maintenance déclarée, l'étage 4 ne se justifie pas. S'il apparaît des commits **non choisis**
(dérive de source, re-push, insertion d'UDC…), alors la fuite frappe **sans avertissement** et
l'étage 4 devient le seul remède structurel — et il faut le scoper. Une alternative **beaucoup moins
chère** à instruire avant lui : **supprimer la fuite plutôt que le commit**, en libérant proprement
les mbufs du sw_ring au stop de queue (patch DPDK `ice_tx_queue_stop`/`ice_reset_tx_queue` — un
`rte_pktmbuf_free` sur les entrées du sw_ring avant le memset). Si ça tient, `-207` **et** `-203`
disparaissent tous les deux à la source, pour un patch **S**, sans toucher à l'arbre TM.

## Protocole de recette (0.49.0 — à lancer quand le moteur est libre)

**Prérequis** : `bobi-mtl-140` (nœud 30) libre, image **rebuildée** (le patch touche libmtl **et**
`mtl_rx.c`), ⚠ **redémarrer l'orchestrateur avant tout redéploiement** (le registre de plugins est
scanné AU BOOT : sans restart, le déploiement rendrait le code neuf en l'estampillant 0.48.0).

**Reproduire le commit de maintenance** (c'est le déclencheur, pas le câblage) : moteur avec ≥ 3-4
sorties TX **vivantes à 50 fps** (dont au moins une en **mode tranche**, `bobi_mtl_vtx_sl` — c'est
elle qui est tombée 2×), quiescence 60 s, puis **déclarer une nouvelle sortie** (slot + mcast + port,
sans source) → `set_rl_rate` → `rte_tm_hierarchy_commit` → stop/start du port entier.

**Discriminant** (`docker logs bobi-mtl-140`) :

| Marqueur | Sens |
|---|---|
| `mt_dev_get_tx_queue … speed <x>g` | **le commit a eu lieu** (le repère de tout ce chantier) |
| `build ret -207` puis `bobi: builder famine … triggering queue fatal recovery` | mort n°1 (mempool) + filet étage 0 (**doit** être suivi de `new queue_id` **et** de `stop frame`) |
| `build ret -203` puis `bobi: inflight frame N trapped > 2s … reclaiming it (no queue recovery)` | **mort n°2 + le nouveau filet** ← ce qu'on vient valider |
| `bobi: N inflight frame(s) reclaimed, tx should resume` | la trame a été rendue au feeder |
| `bobi: get_frame busy > 2s but NO frame in flight — app starvation` | **verdict normal d'un slot silencieux** (pas une panne — ne doit apparaître qu'**une fois** par slot muet) |
| `anneau fb WEDGÉ — slot prod N jamais libéré` | le filet **app** (slice) a dû agir : le filet libmtl n'a pas suffi → **à instruire** |
| `build ret -203` qui **persiste > 30 s** sur une session à source fraîche | **ÉCHEC** : le fix ne couvre pas le cas |

**Critères de succès** : (1) toutes les voisines reviennent à 50 fps **dans les ~2-4 s** qui suivent
le commit (au lieu de 0 fps définitif) ; (2) **zéro** `-203` persistant au-delà de la fenêtre de
reclaim ; (3) **aucun commit TM supplémentaire** imputable au nouveau filet (il n'en fait aucun —
si le compte de `mt_dev_get_tx_queue` augmente, c'est le filet **famine**, pas celui-ci) ; (4) les
slots volontairement **silencieux** loggent leur verdict **une seule fois** (pas de spam).

**Compteurs à relever** : `fps` par slot (`:8080`), `stat_recoverable_error` par session,
occurrences de `mt_dev_get_tx_queue` (= commits), de `-203`, de `-207`, de `reclaiming it`,
de `get new txq fail` (doit rester 0), et la télémétrie DPDK des mempools (les pools `recovery_idx 0`
doivent rester le seul point de fuite connu).

**Test négatif obligatoire** (ne pas l'oublier) : un slot **déclaré sans source** doit rester muet et
**inchangé** — le filet ne doit **rien** rappeler chez lui (branche « app starvation »).

## Briques existantes réutilisées

| Brique | Où | État |
|---|---|---|
| Bibliothèque de cartes (`rl_tx_cap`, `narrow_ok`, qualif auto) | `nic_profiles` + Réglages | ✅ live |
| Réserves par port | `node_interfaces.rx/tx_reserve`, `queue_margin` | ✅ |
| Format par slot TX | `deploy_config.tx_slots[]` (w/h/fps/scan) | ✅ |
| Sessions provisionnées silencieuses + swap de source | controller.py `provisioned` / `tx_set_source` | ✅ codé, jamais activé |
| Gating format au câblage | `cabling._format_gate` (+ réglage `wire_format_gating`) | ✅ compute only |
| Modal + insertion auto UDC | `cabling._insert_udc` + cables.html | ✅ compute only |

## Ordre / état

| Étage | Rebuild image | Taille | État |
|---|---|---|---|
| 0 filet famine (`-207`, mempool) | oui | S | **LIVRÉ 0.48.0** — devenu **ceinture** : 0 déclenchement depuis 0.50.0 |
| 0-bis rappel de trames (`-203`, feeder) | oui (libmtl + `mtl_rx.c`) | S | **LIVRÉ 0.49.0**, compilé et déployé (0.51.0) — **ceinture** : 0 déclenchement (plus rien à rappeler) |
| **0-ter — LA CAUSE : fuite de mbufs au commit** | oui (libmtl) | S | **LIVRÉ 0.50.0, VALIDÉ AU BANC** — l'hypothèse DPDK était FAUSSE, la fuite venait de notre hang-guard (`return nb_pkts` = mensonge) ⇒ `-207` **et** `-203` supprimés à la source |
| 1 arbre statique | non | M | **LIVRÉ** (cb36565) — **feuille RL au débit nominal dès la déclaration + 1ʳᵉ activation à 0 commit PROUVÉS au banc 2026-07-14** |
| 2 validation + maintenance | non | M | **LIVRÉ 2026-07-14** (classement prouvé au banc) |
| 3 gate format TX + UDC | non | S-M | **LIVRÉ 2026-07-14** (axes de signature prouvés au banc) |
| 4 matrice de queues | oui, profond | L | réserve — **verdict : pas maintenant**, et l'argument « pour » est tombé : la fuite (le vrai risque résiduel des commits) **n'existe plus**. Le fix DPDK envisagé est **caduc** (le PMD ice libère déjà ses mbufs). |

**Dettes ouvertes du chantier** (à ne pas perdre) :
- La **famine audio** (filet étage 0) **abandonne** si `st_audio_queue_fatal_error` échoue à
  ré-acquérir queue+mempool : la demande est rejouée indéfiniment côté code, mais **aucune alerte
  n'est remontée à l'orchestrateur** — une session audio morte reste silencieuse pour l'exploitant.
- `-203` **audio** (st30) non couvert (cf. étage 0-bis).
- ~~La **fuite de mbufs** au stop de port n'est toujours pas supprimée~~ → **SUPPRIMÉE en 0.50.0**
  (validée au banc). Reste : le commit TM stoppe toujours le port (**2,2 s** mesurées) — impact
  récepteur **MESURÉ ET QUANTIFIÉ le 2026-07-15**, cf. section ci-dessous.

### Les 2,2 s de commit : impact récepteur MESURÉ (banc moteur 140, nœud 30, image 0.52.0, 2026-07-15)

**Question résolue** : après le fix 0.50.0 (0 mbuf perdu), un commit de maintenance stoppe toujours le
port ~2,2 s. Les sorties **ré-émettent-elles en rafale le retard avec des timestamps RTP en retard**
(vrai problème antenne), ou est-ce un rattrapage propre (cosmétique) ? **Réponse : ni l'un ni l'autre
tout à fait — c'est un TROU (gel), pas une rafale de rattrapage, et la reprise est DÉJÀ alignée sur la
grille TAI courante.**

**Protocole** : moteur 140 avec 6 TX vidéo à 50 fps (fallback black, bi-port). Commit provoqué en
changeant le **format** d'un slot voisin sur le même port (`ens1f1np1`) → `set_rl_rate` →
`rte_tm_hierarchy_commit` → stop/start du port. Mesure sur (a) les compteurs libmtl `TX_VIDEO_SESSION`
(fenêtres 10 s) des voisines, (b) un **RX du moteur en loopback inter-port** (RX slot pinné sur
`ens1f0np0` abonné au mcast d'une TX voisine de `ens1f1np1`, via réflexion switch).

**Faits mesurés** (commit à 23:55:50, `reset window over after 2164 ms, 0 mbuf lost` ; reproduit 4× :
2153 / 2158 / 2164 / 2218 ms) :

| Fenêtre 10 s | slot0 voisin (TX_VIDEO_SESSION 6,0) | interprétation |
|---|---|---|
| **contenant le commit** | fps **37,1** — **371 champs** (au lieu de 500) | **~129 champs NON émis ≈ 2,2 s de trou** |
| **suivante (reprise)** | fps **50,000** — **exactement 500 champs** | **reprise propre à 50 fps, AUCUN rattrapage** (sinon > 500) |

- **C'est un TROU, pas une rafale.** Les ~110-130 champs de la fenêtre de stop ne sont **jamais émis**
  (le feeder reste bloqué sur la trame en cours pendant le stop ; il ne bâtit pas 2,2 s de retard). La
  fenêtre de reprise montre **exactement 50 fps** — s'il y avait rejeu du retard, elle dépasserait 50.
- **La reprise est DÉJÀ sur la grille TAI courante.** Corroboré côté récepteur : la `rx_latency_ms` du
  RX loopback reste **stable ~21,4 ms** avant / pendant / après le commit (jamais de pic vers ~2200 ms).
  Si les trames reprises portaient des timestamps RTP en retard, la latence RX bondirait — elle ne
  bouge pas. ⇒ **la crainte « ré-émission en rafale du retard, timestamps RTP en retard » est INFIRMÉE**
  pour le gros du flux : seuls les paquets **déjà dans le ring NIC** au moment du stop (`inflight`
  ~10 champs) sont flushés au redémarrage (micro-rafale < ~200 ms), le reste reprend au présent.

**Verdict** : **vrai problème antenne, mais pas celui qu'on croyait.** Le récepteur 2110 subit un
**gel de ~2,2 s** (≈110-130 champs perdus) sur **toutes** les sorties du port commité — franc, pas
cosmétique (c'est bien pourquoi la maintenance doit rester une **fenêtre déclarée**, étage 2). En
revanche il n'y a **pas** de tempête de trames à timestamps périmés : la reprise est propre, à la
cadence et sur l'epoch courants (mesuré). La piste suggérée « reprendre aligné sur la grille TAI
plutôt que rejouer le retard » **est déjà le comportement de fait** — rien à implémenter de ce côté.

**Ce qui resterait à traiter (chiffré avant d'implémenter, PAS cette nuit)** : le **gel** lui-même. Il
est **structurel** au PMD ice (le commit de l'arbre TM stoppe le port entier ~2,2 s) — seul l'**étage 4**
(matrice de queues pré-shapées, zéro commit runtime) l'élimine. Pistes intermédiaires à instruire :
(1) **réduire les 2,2 s** — comprendre pourquoi le stop/start ice dure si longtemps (aller-retour
admin-queue firmware, cf. `patch_ice_tm_move_retry`) ; (2) **jamais deux sorties d'antenne sur le même
port RL** si l'une peut subir une maintenance (isolation par port / carte) ; (3) grouper toute la
maintenance d'un port en **une seule** fenêtre (déjà offert par `tx_pending_changes`).
**Non vérifié** : le comportement d'un récepteur 2110 tiers STRICT (un vrai décodeur broadcast) face au
trou de 2,2 s — mesuré ici côté moteur (RX libmtl) seulement, pas contre un équipement d'antenne réel.
