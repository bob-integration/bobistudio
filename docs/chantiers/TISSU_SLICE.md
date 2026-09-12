# Tissu de composition en mode tranche — plan de conception (sémantique « flow »)

> Chantier latence sous-trame MXL, phase tissu. Décisions utilisateur 2026-07-11 :
> sémantique (c) directement (pas d'étape intermédiaire barrière-(a)), GPU à migrer,
> rollout opt-in. Prérequis livrés : moteur 2110 RX/TX slice (0.40.0), multiview 0.24.0,
> pyramide 0.7.0, UDC 0.6.0 (optimisation resize en cours).

## 0. État mesuré (banc dl360-1, 1080p50, phases médianes par période de 20 ms)

| Étage | 1ʳᵉ bande dispo | whole-frame équiv. |
|---|---|---|
| RX 2110 → MXL | +1,7 ms | ~+20 ms |
| Pyramide (proxy) | +1,4 ms | ~+20 ms |
| Multiview (composite 2×2) | +2,2 ms | ~+20-40 ms |
| UDC (identité ; conversion en cours d'optimisation) | +0,9 ms | ~+20 ms |
| TX 2110 (fil) | quantifié à l'epoch (+1 trame) | +1 à +2 trames |

Enseignement structurant : les étages **internes** MXL coûtent ~1-2 ms chacun en tranche ;
seul le TX 2110 re-quantifie à la frontière d'epoch (pacing narrow). Un mur shardé à
N étages internes reste donc à ~1 trame de latence totale au lieu de N×20 ms.
(Piste ultérieure hors tissu : epoch-shift TX `rtp_timestamp_delta_us` pour rattraper la
phase de chaîne et gagner la dernière trame.)

## 1. Objectif

Le tissu (`app/compositor_fabric.py` : dédup par signature + shards guillotine + assembleur)
matérialise aujourd'hui des multiviews `cadence="input"` **whole-frame** : chaque étage
(pyramide → shard → assembleur) attend le grain complet amont. Objectif : chaîne interne
du tissu **entièrement par bande** — latence interne d'un mur shardé ≈ Σ(~2 ms) au lieu
de ~40-60 ms, sans changer le planificateur (signatures/partition inchangées).

## 2. Sémantique « flow » (la (c) actée) — cahier des charges

Nouveau mode `cadence="flow"` dans le script multiview (les modes `genlock` et `input`
restent STRICTEMENT inchangés — compat murs existants). Data-flow pur par bande, plus de
barrière globale :

1. **Déclenchement** : un cycle de composition démarre dès que la PREMIÈRE entrée vivante
   ouvre son grain suivant (avance de tête = 1ʳᵉ tranche), borné par :
   - clamp d'intervalle MIN = 0,9×T_sortie (une source rapide ne tire jamais le mur
     au-dessus de sa cadence nominale) ;
   - deadline MAX = T_sortie sans aucune entrée vivante (mur sans source : cadence
     nominale, sortie noire/figée — comportement historique conservé).
2. **Suivi du grain-à-venir PAR TUILE** : au déclenchement, chaque tuile vise son PROPRE
   prochain grain ; une tuile dont le grain n'a pas encore commencé est suivie *pendant*
   le compose (get_slice bloquant par bande) — pas de décalage systématique d'une image
   pour les sources en retard de phase (≠ le « any » naïf).
3. **Budgets PAR TUILE** (remplace le budget global actuel, qui couple les tuiles) :
   une tuile qui épuise son budget bascule sur son dernier grain complet pour le RESTE
   de la trame, sans entamer le budget des autres.
4. **Backoff entrées mortes** : une tuile en timeout de 1ʳᵉ bande sur 2 trames
   consécutives passe « dormante » (dernier grain complet, zéro sonde) jusqu'à la
   prochaine avance de sa tête. Couvre définitivement le cas « producteur mort
   mi-grain » (aujourd'hui : ~2 s de sondes coûteuses jusqu'au freeze-drop).
5. **Indexation de sortie = grille TAI** (`index_mode="tai"`), plus le compteur libre
   de l'input-locked : comme le compose suit le fil en sous-trame, l'index TAI du grain
   de sortie d'un shard == l'epoch de son contenu → l'ASSEMBLEUR compose le grain k en
   lisant les shards AU MÊME INDEX k (`get_slice(k, …)`) : alignement inter-shards
   PARFAIT (fini le `get_latest` désaligné), repli k-1 si un shard est en retard.
6. Écriture de sortie inchangée (open_grain 1×, commit validSlices=1..N).

## 3. Modifications par composant

**A. plugins/multiview (script.py)** — le gros du travail (~200-250 lignes) :
- mode `cadence="flow"` (points 1-5) ; état par-tuile persistant entre trames
  (budget, backoff, dernier index suivi) logé dans `sources[i]` ;
- assembleur : tuiles 1:1 (in==out) → le fast-path vue stridée dégénère en copie de
  bande pure (memcpy), déjà couvert ;
- **P0 (préalable, à faire dès maintenant)** : exclure les attentes get_slice de
  `own_latency_ms` (comme fait pour la pyramide) — le tissu lit cette métrique pour ses
  décisions de sharding (`deploy.py:~1294`) et le monitoring l'affiche ; en slice elle
  vaut ~T actuellement (faux signal de saturation) ;
- promouvoir les compteurs de debug `_sl_dbg` (valid0/waits/replis/backoffs) en
  métriques :8080 (observabilité de recette).

**B. app/compositor_fabric.py** — petit (~30 lignes) :
- `_mv_params(..., slice_mode, slice_lines, cadence)` : les nœuds du tissu reçoivent
  `cadence="flow"` + `slice_mode=true` quand le tissu est en mode tranche ;
- activation : setting global `fabric_slice_mode` (défaut OFF) — rollout opt-in acté ;
  les murs logiques (assembleur = flux externe) héritent aussi du slice pour la sortie ;
- aucun changement au planificateur (signatures, guillotine, registre).

**C. pyramide / UDC / moteur** : déjà migrés. Les proxies sur-mesure du tissu
(`extra_sizes`) passent par `_proxy_slice_h` (toute hauteur) — rien à faire.

## 4. GPU (décision : migrer)

Phase dédiée, APRÈS le tissu CPU (la sensibilité des transferts H2D au découpage est le
risque n°1 — banc Phase 0 : un H2D par tuile faisait régresser le GPU, d'où l'upload
groupé épinglé actuel).
- **Banc gate GO/NOGO (dl360-2/T4)** : coût H2D par bande (pinned, streams cupy) vs
  groupé ; si le bandé fin (30×) ne tient pas, variante « méga-bandes » (2-4 tranches
  de ~270-540 lignes) qui garde ~10-15 ms de gain pour un coût de transfert quasi groupé.
- Design candidat : pipeline double-buffer — H2D bande k+1 pendant compose bande k
  (streams), compose bandé en VRAM, D2H bandé épinglé vers la vue du grain.
- Le fallback CPU-whole-frame actuel reste le comportement par défaut GPU jusqu'au GO.

## 5. Bancs et recette

- **B1 nominal** : 2 murs partageant des cellules (dédup + 2 shards + assembleur) sur
  dl360-1, chaîne RX→pyramide→shards→assembleur→TX en tranche ; mesure des phases par
  étage (attendu : shard +2 ms, assembleur +1 ms, alignement inter-shards à l'index).
- **B2 churn** : édition à chaud du mur → re-plan fabric → recréation de shards sous
  slice ; vérifier générations de flux (piège orphelins connu : GC + stale-reopen).
- **B3 dégradés** : tuer une source (les 3 cas : flux absent / ring orphelin / mort
  mi-grain) → backoff par tuile, AUCUN impact sur les autres tuiles ; formats mixtes.
- **B4 charge** : 16-20 murs (le plateau mesuré est memory-bound) ; vérifier que les
  réveils futex par bande × nœuds ne pèsent pas (mitigation : slice_lines=72 pour les
  nœuds du tissu) et que la bande passante mémoire ne bouge pas (mêmes octets).
- Recette visuelle : utilisateur.

## 6. Risques identifiés

- **R1 réveils par bande × N nœuds** : charge context-switch — mesuré en B4, mitigation
  slice_lines plus grand côté tissu.
- **R2 grille TAI partagée** : l'indexation TAI suppose CLOCK_REALTIME discipliné sur
  chaque nœud compute (mono-nœud : OK via ENGINE_PHC2SYS ; multi-nœud : à vérifier par
  nœud avant d'étendre — sinon l'assembleur retombe sur le suivi de tête par tuile,
  dégradation propre prévue au design).
- **R3 churn fabric × readers** : recréations de shards sous le même nom → générations
  orphelines (piège documenté) ; couvert par GC/stale-reopen existants, validé en B2.
- **R4 métriques** : P0 (own_latency sans attentes) OBLIGATOIRE avant d'activer le
  slice sur un nœud du tissu — sinon fausse saturation lue par l'orchestrateur.
- **R5 compat** : « flow » est un NOUVEAU mode ; `input` et `genlock` intacts —
  aucun mur existant ne change de comportement sans opt-in.

## 6bis. RÉALISÉ (2026-07-12, banc B1) — design affiné à l'implémentation

La sémantique « flow » livrée est PLUS SIMPLE que le cahier des charges §2 : comme tous les
flux du tissu sont sur la grille TAI, le « déclenchement à la première entrée » est équivalent
à CADENCER SUR LA GRILLE ELLE-MÊME (tick d'epoch, ~1,6 ms avant l'arrivée des grains) et à
CIBLER L'INDEX D'EPOCH fi_out : chaque tuile lit LE grain fi_out de sa source et le suit par
bandes ; la sortie est écrite à ce même index. Plus de barrière du tout, alignement par
construction. Les clamps min/max du §2.1 deviennent inutiles (le tick paced). Les points 2-4
(suivi par tuile, budgets par tuile, backoff) sont livrés tels quels (0.24.1/0.25.x).

MESURÉ (B1, dl360-1, mire 1080p50, phases médianes) : RX 1,67 ms → pyramide +1,42 → shard
+0,67 → assembleur +0,70 ; mur shardé complet dispo en 1ʳᵉ bande à 4,46 ms de l'epoch ;
alignement d'index PARFAIT (head = epoch sur les 5 flux de la chaîne) ; 50 fps partout.

Trois pièges corrigés au banc (0.25.1 / pyramide 0.7.1) :
1. Rattrapage de grille : suivre un grain occupe ~toute la période — le recale « saut à la
   prochaine frontière » faisait retomber le tick en fin de période (whole-frame déguisé,
   valid0=30). En flow : rattrapage immédiat si retard < 1 période.
2. Génération périmée détectée PAR LA GRILLE (tête figée > 5 s de retard) → drop + reopen —
   le grain orphelin restant lisible, ni « got is None » ni le freeze ne déclenchaient.
3. GC ENTRE close et reopen, PARTOUT (moteur ✓, multiview _drop_input ✓, pyramide watchdog ✓) :
   sans GC, la réouverture par nom retombe sur l'orphelin qu'on vient de lâcher. R3 confirmé
   à tous les étages ; parade générique désormais uniforme. (UDC : à vérifier au même titre.)

Reconfiguration : le fabric reconfigure les assembleurs À CHAUD (style/flux seulement) — la
cadence/slice d'un mur logique existant ne bascule qu'à son REDÉPLOIEMENT (documenté §3.B).

## 6ter. Bancs B2-B4 — RÉALISÉS (2026-07-12 soir)

**B2 churn (3/3 PASS)** : (1) recréation pyramide → shard revenu en suivi ; (2) recréation du
shard (nouvelle génération fab) → assembleur reconnecté, **8,1 s** bout-en-bout redéploiement
compris, l'aval fil resté à 50 fps (gel d'image TX pendant la transition) ; (3) résouscription
de la source → chaîne complète en suivi en **1,6 s**. Zéro intervention manuelle.

**B3 dégradés (PASS, avec nuances)** : les producteurs stoppés (pyramide, UDC) sont RELANCÉS
par la surveillance en < 20 s — l'auto-réparation plateforme ferme la fenêtre avant que le
backoff soit nécessaire. Pendant la fenêtre : tuile source-figée = repli collect borné (~3 ms,
grain complet gelé), AUCUN impact sur les autres tuiles ni sur la cadence (50 fps constants).
Le backoff/dormante ne se déclenche que sur mort MI-GRAIN (tête partielle figée) — non
reproduit en conditions réelles (producteurs sains meurent proprement), couvert par le design.

**B4 charge (PASS)** : 8 murs flow 2×2 1080p50 AJOUTÉS à la chaîne tissu + prod : 8/8 en suivi
actif (valid0=1, waits≈31, 0 replis) à 50 fps, own 4,4-5,2 ms ; moteur intact (PTP, 4 RX 50 fps) ;
coût ≈ +0,8 cœur et +4,4k cs/s par mur (réveils futex par bande), idle 67 % — R1 absorbé sans
mitigation (slice_lines=72 pas nécessaire à cette échelle).

**⚠ 2 findings PLAN DE CONTRÔLE (hors slice, à traiter)** :
1. 8 créations de conteneurs EN RAFALE wedgent le Flask orchestrateur (UI/API morts jusqu'au
   restart service ; le plan de données continue). Sérialiser/queue côté client en attendant.
2. Sous cette rafale, les deploy_params POSTés sont PERDUS pour la plupart des conteneurs
   (déployés avec les seuls défauts : flux_config vidé, shm_out=hostname) — corruption
   silencieuse. À investiguer côté route creer/threads (même famille que le wedge).

## 7. Ordre de réalisation

1. **P0** : own_latency multiview sans attentes + promotion des compteurs slice en
   métriques (court, à glisser dans la vague durcissements avec le watchdog TX 0.40.1
   et les budgets par tuile — points 3-4 du cahier des charges, communs).
2. `cadence="flow"` complet dans multiview (+ bancs unitaires hors-ligne).
3. Propagation compositor_fabric + setting `fabric_slice_mode`.
4. B1 → B4, itérations.
5. GPU : banc gate puis implémentation selon verdict.
6. Doc exploitant + config_schema UI (slice_mode/cadence) + décision de généralisation.

## 8. Epoch-shift TX (banc 2026-07-11, moteur 0.41.0) — RÉALISÉ

Choix PAR DESTINATION (Destinations 2110 → sélecteur par carte TX) entre « ⏱ image
suivante » (défaut : émission alignée sur l'epoch nominal — une chaîne interne en phase
> tr_offset paie +1 trame) et « ⚡ émission décalée +N µs » : la fenêtre d'émission recule
de N µs après l'epoch, le timestamp RTP reste sur l'epoch NOMINAL (sync A/V intacte) et le
TROFF est déclaré au SDP (`a=troff:` en ticks 90 kHz = 3440/fps + N×0,09) — conforme
ST 2110-21 ; coût côté récepteur = marge tampon équivalente.

Mécanique : patch libmtl `patch_epoch_shift.py` (convention `ops.rtp_timestamp_delta_us`
NÉGATIF ⇒ `pacing->bobi_epoch_shift_ns`, point unique `tai_from_frame_count`, stamp restauré
par le delta existant, `tr_offset` intact, clamp période/2) ; mtl_rx clé de session
`epoch_shift_us` (whole-frame ET tranche, dans compute_sig → recréation propre) ; route
orchestrateur `POST /api/mtl/<vmid>/tx/<slot>/pacing` (0-15000 µs). ⚠ ne JAMAIS poser de
delta négatif sur l'audio st30 (stamp reculé sans décalage de grille).

**Banc TX5 (mvslice_out → 239.10.1.6, chaîne tranche complète), shift 9000 µs** :
- retard d'index TX-entrée → RX-sortie = **0 (même epoch)** — le « +1 epoch » historique
  est éliminé ; 1ʳᵉ bande à RX2 : époch +10,7 ms (= shift 9 + FPT ~1,7) contre ~+21,7 ms
  (epoch suivant) avant → **gain ≈ 11 ms** ; trame pleine +29,1 ms (vs ~+40).
- santé : 50,0 fps, **0 trou** (6000/6000 trames RX en 120 s), 0 epoch_drop/mismatch libmtl
  hors transitoire de recréation. SDP vérifié `a=troff:878`@9 ms.
- calibrage : shift ≥ phase p95 de la 1ʳᵉ bande source (+ marge) ; à 6 ms (< phase 7,8 ms
  de mvslice_out) ça marche mais sans marge.
- ⚠ compteur `late` (stats :8080) : avec shift il tick ~1,7/min (vs 0,3/min à 0) SANS
  perte réelle (0 trou au fil) — artefact de la contre-pression fb décalée sur le seuil
  1,5 T du compteur d'alimentation. À affiner (exclure l'attente de slot du gap) si le
  badge ⚠ UI devient bruyant.

---

## Banc GATE du slice GPU — 2026-08-07, Tesla T4 (r620-1)

Verdict de l'outil : **GO-MEGA-540**. Résultat brut : `docs/chantiers/gate_gpu_slice_T4.json`.

```
M3 — compose (place + blend chrome + blends VU)
  pleine trame (RÉFÉRENCE)      3,013 ms
  par bande 540l (2x)           5,120 ms      ← le MOINS mauvais des découpages
  par bande 135l (8x)          13,222 ms
  par bande  36l (30x)         45,582 ms

M4 — bout-en-bout                            [latence 1ʳᵉ bande]
  pleine trame (1x)             7,562 ms      [7,559 ms]
  bande 540l (2x)               9,707 ms      [5,154 ms]
  bande  36l (30x)             55,492 ms      [0,871 ms]
```

**Ce que le GO autorise, et ce qu'il n'autorise pas.** Le gate valide que le slice GPU est
*viable* en méga-bandes de 540 lignes : son surcoût de transfert y tombe à +0,27 ms et le temps
de trame reste sous le budget. Il ne dit **pas** que le slice rend un mur plus rapide — il dit le
contraire, et sans ambiguïté : à découpage égal ou non, la pleine trame gagne toujours en temps
TOTAL (3,0 contre 5,1 ms en compose ; 7,6 contre 9,7 ms bout-en-bout). Ce que le slice achète est
la **latence de sortie de la première bande** (5,15 ms au lieu de 7,56 ; 0,87 ms en 36 lignes),
c'est-à-dire le démarrage de l'aval avant la fin de la trame. C'est une fonctionnalité de latence,
pas de débit.

**Conséquence pour les shards à 40 fps.** `gpu_slice` n'est donc PAS le levier de leur cadence :
l'activer coûterait ~2 ms de temps de trame supplémentaires sur un budget déjà tendu. La piste
reste ouverte le jour où la latence bout-en-bout de la chaîne devient le sujet.

**Ce que le banc dit VRAIMENT du problème de cadence.** Sur T4, composer une trame de 4 tuiles
1080p (placement + blend chrome + blends VU) coûte **3,0 ms**. Nos shards, sur P5000, paient
`in_place` 5,1 ms + `ov_blend` 8,1 ms = 13,2 ms pour DEUX tuiles lues en proxy demi-résolution.
Un facteur quatre que la différence de GPU n'explique pas. La divergence porte sur la manière :
le banc fait quelques gros lancements, le mur en fait une nuée — une tuile VU, une tuile horloge,
une bbox de chrome à la fois. C'est là qu'est le gisement, et il se mesure en groupant les
lancements, pas en tranchant la trame.

⚠ Le banc a tourné sur la T4 de r620-1 (le GPU visé par le gate) ; la production tourne sur le
P5000 de dell-1. Les rapports entre chemins se transposent, les valeurs absolues non.
