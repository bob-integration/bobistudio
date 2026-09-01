# Multiview — où passent vraiment les 20 ms

> ## ⏩ SUITE 2 (2026-07-14, multiview 0.42.0) — la métrique mentait, et le levier 2 est livré
>
> ### 1. `fps` était FAUX (artefact de fenêtre) — il ne l'est plus
> Sur le 333 : `fps` 46-48 (« chute » 20-25 % du temps) pendant que `bakes_per_s.frames` = 50,1.
> **Les deux comptaient la même chose** — les trames composées. L'écart était 100 % un artefact
> d'échantillonnage : `fps` était mesuré sur une fenêtre à **nombre de trames FIXE** (tous les 25
> composes), donc c'est sa **durée** qui variait. Un seul tick en retard (GC, hoquet de grille)
> gonflait la fenêtre de 0,5 s et faisait tomber le chiffre à 47, la fenêtre suivante rattrapant à
> 52. Le tableau de bord (poll 5 s) tombait sur une fenêtre creuse ~1 fois sur 4. Vérifié par la
> mesure (60 relevés du 333) : moyennes **49,78** (fps) vs **49,73** (`frames`) — identiques ; ce
> sont les **variances** qui diffèrent (min 47,9 vs 48,9).
>
> **0.42.0** : `fps` = débit sur une fenêtre de **TEMPS** (2 s) à partir de compteurs **monotones**,
> évalué **au scrape** (une boucle morte fait décroître le chiffre vers 0 au lieu de le figer).
> Et surtout, le vrai signal de santé n'est plus déduit d'un fps bruité, il est **compté** :
> `frames_missed` / `frames_missed_per_s` = **slots de grille genlock réellement sautés**.
> Vérifié au banc : tout relevé `fps < 49,5` correspond à **≥ 1 trame réellement perdue** (moy 1,5) ;
> tout relevé ≥ 49,5 à ~0 (moy 0,26). **Une chute de fps a désormais toujours une cause.**
> ⚠ **Entrelacé** : un mur 1080i50 compose 25 trames/s et émet 50 champs/s. `fps` expose la cadence
> de **SORTIE** (50, = la cadence du format déclaré → un mur i50 sain ne paraît pas à moitié mort) ;
> `frames_per_s` (25) expose les trames composées ; `fps_unit` (`fields`/`frames`) lève l'ambiguïté.
>
> ### 2. Levier 2 (re-bake du chrome) — LIVRÉ, mais pas comme prévu
> Le re-bake plein cadre (≈25 ms) sort de la trame par un **thread boulanger** (motif 0.40.0).
> ★ **Sortir le bake du thread ne suffit PAS** : `alpha_composite` plein cadre est un appel C qui
> **garde le GIL** de bout en bout — la boucle de compo, *même prioritaire*, ne peut pas se réveiller
> et rate quand même son slot. Mesuré : `nice(10)` sur le boulanger **ne corrige rien** (0,43 slot
> raté par bascule). Le compositing PIL est donc fait **par bandes de 120 lignes avec relâche
> explicite du GIL** entre chaque bande → le pic disparaît vraiment.
>
> Banc nœud 30 (mur genlock-grille **plein cadre CPU**, 4 cellules, **tally basculé 1×/s**) :
>
> | trames RÉELLEMENT composées/s | 0.41.0 | **0.42.0** |
> |---|---|---|
> | au repos | 49,0 | 49,2 |
> | **avec 1 bascule de tally/s** | **44,6** | **49,6** |
> | `own_latency` (bascules) | 13,0 (max 18,5) | **10,1** (max 11,8) |
> | `overlays` ms/trame (bascules) | 10,2 | **4,8** |
> | `ov_bake` ms/trame | 1,0 | **0,0** |
> | `fps` min (bascules) | 38,0 | **47,6** |
> | slots ratés **par bascule** | ~3 | **0** (noyé dans le bruit du banc) |
>
> Le coût du bake est toujours payé (`chrome_bake_ms` : 36-76 ms sur ce banc CPU, `async: true`) —
> mais **plus jamais par une trame**. Le pré-blend du fond (≈13 ms, caché dans `inputs`) sort aussi.
> **Non vérifié** : le gain sur le 333 lui-même (Horace en lecture seule) — il faut y redéployer.

> ## ⏩ SUITE (2026-07-14, multiview 0.40.0) — le mur 333 est à 50 fps
> Le correctif TSL de cette nuit (0.39.2) a bien tué le churn de chrome (`bakes_per_s.chrome = 0`),
> mais le mur est resté à **45-46 fps**. La mesure a désigné un **deuxième** pic, de même nature :
> **la recomposition d'une FRISE** (`hist_bake_ms.max = 29,6 ms`), qui tombait elle aussi ENTIÈRE
> dans la trame, 5 à 7 fois par seconde. Même arithmétique, même verdict : le mur n'était **pas
> compute-bound** (own 12,1 ms pour un budget de 20), il était **assommé périodiquement**.
>
> **Correctif (0.40.0) : la recomposition sort de la boucle de composition** — un *thread boulanger*
> fabrique la tuile hors trame et la publie prête (échange atomique). Mesuré sur le 333 :
>
> | | 0.39.2 | **0.40.0** |
> |---|---|---|
> | fps (médiane / min) | 46,3 / 39,9 | **50,0 / 46,0** |
> | `own_latency_ms` | 12,1 | **9,3** |
> | `ov_hist` (coût vu par la trame) | 1,45 | **0,10** |
> | `ov_meters` | 2,4 | **0,50** |
> | `hist_bake_ms` | 29,6 **dans la trame** | 31→47 **hors trame** (`async: true`) |
>
> Les leviers 3, 4 et 7 du §8 ci-dessous sont **traités** ; le levier 2 (re-bake incrémental du
> chrome) reste ouvert mais n'est plus urgent. ⚠ **Contention découverte** : sur Horace, le
> streamer `bobi-cmp-171` a le cpuset `19-47,67-95`, qui **recouvre** celui du mur 333 (`19-21`)
> — c'est le `core_pool` qui est en cause, pas le mur. Sur cœurs dédiés, le 333 tient
> **50,0 fps avec 0 chute sur 50 relevés** ; sur son cpuset de prod, il chute encore ~20 % du
> temps sous 49,5. **Corriger `core_pool` est le prochain gain gratuit.**

**Nuit du 2026-07-13 → 07-14.** Diagnostic chiffré du mur GPU 333 (`multiviewHorace2`, nœud 31 /
Horace, Tesla T4) qui tenait 28-36 fps au lieu de 50. Tout ce qui est affirmé ici est mesuré ; ce
qui ne l'est pas est signalé comme tel (§7).

---

## 1. Conclusion (à lire en 30 secondes)

**Le mur n'était limité ni par le GPU, ni par le compositing, ni par les entrées. Il était limité
par un RE-BAKE D'HABILLAGE PLEIN CADRE déclenché 8 à 10 fois par seconde par le service TSL.**

Le distributeur TSL central (`services/tsl/__init__.py`, `_distributor`) tourne sur un
`wait(timeout=0.1)` : il repasse **toutes les 100 ms même quand rien n'a changé**, et repousse un
paquet `tally_bulk` identique. Côté mur, le handler `/tally_bulk` marquait l'habillage « sale »
**à chaque paquet, sans jamais comparer les valeurs** (`ov_changed = True` inconditionnel). Le mur
recomposait donc ses couches cachées PLEIN CADRE (PIL `Image.new` 1920×1080 + `alpha_composite` +
RGBA→YUV + upload GPU) **~10×/s**. Chaque re-bake coûte ~25-30 ms et **tombe entièrement dans une
trame** : cette trame rate son budget de 20 ms, le mur perd 1 à 2 slots de genlock. 10 fois par
seconde. → 50 fps devient 28-36 fps, et `own_latency` moyen se cale à ~20 ms.

**Mesuré au banc (nœud 30, réplique de la config du 333) :** `bakes_per_s.chrome = 8,0-8,8`,
`ov_bake = 2,0-2,5 ms/trame` et `ov_bg = 2,2-2,4 ms/trame` **en moyenne sur TOUTES les trames**,
soit **≈ 13 ms + 13 ms par re-bake**. Retirer l'overlay sourcé TSL → `bakes_per_s = 0` et ces
deux postes tombent à **0,0 ms**.

**Correctif livré** (multiview **0.39.2** + `services/tsl`) : ne marquer sale que sur **changement
réel de valeur**, et ne pas ré-émettre un paquet identique (re-synchro de sécurité toutes les 5 s).
Vérifié au banc : **`bakes_per_s` 8,5 → 0** avec l'overlay TSL toujours en place, re-bake immédiat
au moindre vrai changement.

**Gain attendu sur le 333 : `own_latency` 19,9 → ~10-11 ms, fps 50 stable.** À confirmer par un
redéploiement de la prod (non fait — Horace était en lecture seule cette nuit). Au banc, le travail
d'habillage par trame passe de **7,4 à 1,3 ms** (mesuré, §4).

Les deux autres questions, en une ligne chacune :
- **`mvk = false` sur le mur GPU est NORMAL** : `_MVK` est **CPU-only par construction** (`(not GPU)
  and mvk_available()`) — sur GPU les mêmes blends tournent en kernels cupy. Le kernel C **est déjà
  utilisé** là où il compte sur un mur GPU : les conversions hôte (`rgba_to_yuv`) via `_MVK_HOST`.
  Vérifié : le `.so` de l'image `bobi-compute-gpu:0.3` **expose bien `mvk_rgba2yuv_u8_u8` (ABI 2)**,
  et le mur 0.39.1 déployé au banc renvoie `mvk_host: true`. **Rien à activer de ce côté.**
- **Le GPU n'est pas mal utilisé, il est SOUS-utilisé parce qu'il n'y a presque rien à faire pour
  lui.** 5-7 % d'utilisation, 137 Mo / 15 Go. Le conteneur consomme **136 % de CPU** : le mur est
  **borné par le travail sérialisé de la boucle de composition sur UN cœur**, et ce travail est
  presque entièrement du PIL/numpy hôte.

---

## 2. Ventilation du temps (mur 333, avant correctif)

Relevé `:8080` (deux échantillons, 21 h et 01 h) :

| poste | ms/trame | ce que c'est |
|---|---|---|
| `inputs` | 4,7 - 5,5 | lecture 1 tuile + placement **+ re-bake `bg` (voir ci-dessous)** |
| `overlays` | 13,7 - 15,0 | habillage |
| ├ `ov_render` | 3,8 - 4,7 | rendu PIL par trame : VU (0), frise audio, horloge |
| ├ `ov_convert` | 0,2 - 0,3 | blend du chrome (opérandes pré-calculés) |
| ├ `ov_blend` | 2,8 - 3,4 | blend des tuiles (frise, horloge) |
| └ **NON COMPTÉ** | **5,9 - 7,2** | **← le trou** |
| `output` | 1,6 - 1,8 | D2H épinglé + commit MXL |
| **`own_latency`** | **19,9 - 22,3** | budget 50p = **20,0 ms** |

★ **Le trou de 5,9-7,2 ms/trame était le re-bake du chrome.** C'est le seul code entre
`_t_after_inputs` et `_ts_ov0` (script.py : bloc `if _chrome_dirty:`) et il n'était mesuré par
aucune sous-ligne. C'est exactement le piège que l'énoncé redoutait : la moyenne `overlays` était
« élevée sans cause visible ».

Arithmétique de cohérence (fps observé 31, 10 re-bakes/s) :
`7,2 ms × 31 trames/s ÷ 10 re-bakes/s ≈ **22 ms par re-bake de chrome**`, plus ~2-3 ms/trame de
re-bake `bg` caché dans `inputs` (≈ 8 ms par re-bake). Une trame qui prend 20 + 30 = **50 ms** rate
2 slots de la grille 50p → 10 trames/s perdues × 2 → **fps ≈ 30**. C'est ce qu'on observe.

`gpu = true`, `gpu_util = 5-7 %`, `mem = 137 Mo / 15 Go`, `docker stats` : **136 % CPU** sur un
cpuset de 3 cœurs. Un seul thread fait le compose.

## 3. Instrumentation ajoutée (multiview 0.39.1)

Édition chirurgicale de `plugins/multiview/script.py` (aucun changement de rendu) :

- `compose_breakdown_ms.ov_bake` — coût du re-bake du chrome (le trou ci-dessus) ;
- `compose_breakdown_ms.ov_bg` — coût du re-bake fond/texte statique (`overlay_dirty`), qui tombe,
  lui, dans `inputs` ;
- `bakes_per_s` — **fréquence** des re-bakes par seconde, ventilée par déclencheur
  (`chrome` / `bg` / `tally` / `geom` / `info`) + `frames`. **C'est la fréquence, pas le coût
  unitaire, qui trahit un churn** — sans ce compteur le bug est invisible ;
- `mvk_host` — le kernel C est-il disponible pour les conversions hôte (vrai même sur mur GPU).

## 4. La preuve (banc nœud 30, réplique du 333)

Mur jetable `mvperf` (vmid 346), config **clonée du 333** (4 cellules dont 3 masquées, frise audio,
horloge PTP, overlay texte sourcé TSL), sources recâblées sur `/dev/shm/avsync`.

| variante | `bakes/s` chrome | `bakes/s` bg | `ov_bake` | `ov_bg` | `overlays` | `inputs` | fps | own |
|---|---|---|---|---|---|---|---|---|
| **avant** — config du 333 (overlay texte **TSL**), 0.39.1 | **8,0** | **8,0** | 2,0 | 2,2 | 3,2 | 3,2 | 50,7 | 15,6 |
| idem, overlay texte **retiré** | **0,0** | **0,0** | 0,0 | — | — | — | *(nœud saturé, §7)* | |
| **après** — même config + overlay TSL, **0.39.2** | **0,0** | **0,0** | **0,0** | — | **1,3** | 3,4 | 50,0 | 15,5 |

→ **travail d'habillage supprimé : 3,2 + 2,0 + 2,2 = 7,4 ms/trame → 1,3 ms/trame** (−6,1 ms/trame
de travail hôte, dont ~26 ms concentrés dans une trame sur six).

⚠ **Nuance honnête** : sur ce mur de banc (CPU, mode **tranche** + cadence `flow`), le fps était
déjà à 50 **avec** le churn — les pics étaient absorbés par l'attente de sortie (`output` = 9-11 ms
d'attente d'epoch, qui sert de tampon) et le mur rattrapait. Le 333, lui, est **GPU, image entière,
genlock grille** : il n'a pas ce tampon, et un pic de 26 ms lui fait rater un slot. C'est pour ça
que le même défaut coûte 0 fps au banc et ~20 fps à Horace. **Ce que le banc prouve, c'est le
mécanisme et le volume de travail (8,5 re-bakes/s → 0, −6,1 ms/trame) ; le gain en fps ne se lit
que sur un mur au budget saturé.**

`tally / geom / info` sont restés à **0,0** dans tous les cas : le déclencheur est **uniquement**
`overlay_dirty`, c'est-à-dire la branche « overlays » du handler `/tally_bulk`.

Test de non-régression du correctif : 20 paquets `tally_bulk` **identiques** d'affilée → aucun
re-bake ; **un** changement réel de texte/état → re-bake immédiat (l'habillage réagit toujours).

Coût unitaire d'un re-bake, déduit des moyennes (`ms/trame × fps ÷ bakes/s`) sur le banc CPU :
**chrome ≈ 13 ms, bg ≈ 13 ms**, soit **~26 ms de travail hôte injectés dans une seule trame,
8 à 10 fois par seconde**.

Décomposition d'un re-bake (micro-banc `scratchpad/bake_bench.py`, contrôleur — CPU lent, valeurs
à lire en proportions, pas en absolu) :

| étape | ms |
|---|---|
| `render_overlays_fg_static()` (PIL, plein cadre) | 3,1 |
| `render_dynamic()` (PIL, modèles de PiP) | 1,6 |
| `Image.new` RGBA 1920×1080 + `alpha_composite` × N | **8,7** |
| `getbbox()` | 0,7 |
| `crop` + `rgba_to_yuv` + opérandes (bbox 1307×535) | 17,5 |
| *(pour référence : `rgba_to_yuv` numpy PLEIN CADRE)* | *88,8* |

Le poste le plus lourd est **PIL** (composition RGBA plein cadre), pas la conversion — la
conversion est déjà fusionnée en C sur les nœuds (`mvk_host: true`).

## 5. Verdict `mvk`

- `_MVK = (not GPU) and mvk_available()` — **CPU-only par construction**. Sur un mur GPU, les blends
  et le placement passent par des kernels cupy équivalents ; le kernel C n'a pas sa place dans ce
  chemin. `mvk: false` sur le 333 est donc **attendu et sain**, ce n'est **pas** un réglage manqué
  ni une image incomplète. Il n'y a **rien à activer**.
- `_MVK_HOST` (0.32.2) gate les conversions **hôte** (`rgba_to_yuv`), qui restent numpy même sur un
  mur GPU. **Vérifié cette nuit** : `libbobi_mvk_v3.so` dans `bobi-compute-gpu:0.3` (conteneur
  `bobi-cmp-333`) **expose `mvk_rgba2yuv_u8_u8`** → ABI 2 présent → **le re-bake du 333 utilisait
  déjà le kernel C** pour sa conversion. (La note mémoire « image GPU 0.3 à rebuilder » est donc
  **périmée** : la 0.3 en vol est bien basée sur une compute ≥ 0.12.)
- Corollaire important : **le kernel C n'aurait pas sauvé ce mur.** Le coût dominant du re-bake est
  le rendu **PIL**, qui n'est fusionné nulle part. Le seul remède au re-bake est de **ne pas le
  faire** — c'est le correctif livré.

## 6. Verdict GPU

Le GPU fait exactement ce qu'on lui demande, et on lui demande très peu : 1 tuile placée, quelques
blends de bbox, un D2H. À 50 fps cela représente ~310 Mo/s de PCIe (1 H2D groupé de 3,1 Mo + 1 D2H
de 3,1 Mo par trame) et 5-7 % d'occupation.

- **Pas d'aller-retour CPU↔GPU parasite par trame** : le fond pré-blendé, les opérandes du chrome et
  les tuiles d'habillage sont **déjà résidents backend** (`_to_xp` à la re-bake, pas à la trame) ;
  l'entrée est uploadée en **un seul H2D groupé épinglé** ; la sortie descend par un `.get()`
  épinglé. Le pipeline est correct.
- **Le rendu des habillages ne peut pas être « gardé en VRAM » plus qu'il ne l'est déjà** : il l'est.
  Le problème n'était pas *où* vit le chrome, c'était **la fréquence à laquelle on le refabrique**.
- **Un mur GPU ne sera jamais rapide tant que la boucle hôte est chargée** : le compose est
  mono-thread, et le GPU n'est qu'un des maillons de cette boucle sérielle.

## 7. Ce que je n'ai PAS pu mesurer (franchise)

- **Le gain sur le 333 lui-même n'est pas mesuré.** Horace était en lecture seule ; le correctif
  n'y sera visible qu'après un redéploiement du mur (multiview ≥ 0.39.2). La valeur annoncée
  (own ~10-11 ms) est une **déduction arithmétique** de la ventilation mesurée, pas un relevé.
- **L'A/B fps « avant/après » sur le banc n'est pas exploitable** : à partir de ~01 h, un agent
  parallèle a saturé le cpuset compute du nœud 30 (load 27, 8 conteneurs sur 10 cœurs logiques,
  `output` passé de 9 à 150 ms) — les fps/own mesurés après cet instant ne veulent rien dire. Les
  **compteurs** (`bakes_per_s`, `ov_bake`, `ov_bg`), eux, restent valides : ils sont catégoriques
  (8,5 → 0) et non sensibles à la charge.
- **Le coût unitaire d'un re-bake sur le GPU d'Horace** (avec ses 5 uploads d'opérandes) n'est pas
  mesuré directement — seulement déduit (22 ms) de la ventilation.
- Je n'ai **pas** touché au chemin de composition (`_place_batch`, blends, mode tranche) : c'est le
  terrain de l'agent parallèle.

## 8. Leviers restants, classés par gain estimé

| # | levier | gain estimé | risque | état |
|---|---|---|---|---|
| **1** | **Ne re-baker que sur changement réel** (TSL keepalive) | **−7 à −10 ms/trame** sur tout mur avec tally/UMD centralisé | nul (comparaison de valeur) | ✅ **livré** (0.39.2 + `services/tsl`) |
| 2 | Re-bake **incrémental** : le chrome est recomposé plein cadre alors que 99 % des changements sont locaux (un texte UMD, une bordure de tally). Ne re-baker que la bbox de la couche qui change. | −10 à −20 ms **par re-bake** (donc sur les vrais changements : bascule tally, changement d'UMD) | moyen (z-ordre) | à faire |
| 3 | `ov_render` 4,7 ms/trame sur le 333 = **frise audio + horloge**. La frise audio se recompose ~5×/s (`ov_hist` 1,9-2,7 ms/trame mesuré au banc avec l'instrumentation de l'agent parallèle). | −2 à −4 ms/trame sur les murs à frise | faible | agent parallèle |
| 4 | `ov_blend` 2,8-3,4 ms/trame pour **une horloge + une frise** : le blend de tuiles fait 3 plans × (upload + kernel) par tuile. Fusionner les tuiles d'une même trame en un seul lot. | −1 à −2 ms/trame | faible | à étudier |
| 5 | `inputs` 4,7 ms pour **une seule tuile** 1920×1080 gathered depuis un proxy : à re-profiler une fois le bruit du re-bake `bg` retiré (il en faisait partie). | inconnu | — | à mesurer **après** redéploiement |
| 6 | Le compose est **mono-thread** : sur un mur GPU, `nvidia-smi` à 5 % et 136 % de CPU disent qu'on est borné par un cœur. Toute optimisation qui ne réduit pas le travail *hôte sérialisé* ne donnera rien. | — | — | principe de conception |

## 9. Fichiers touchés

- `plugins/multiview/script.py` — 0.39.1 : instrumentation (`ov_bake`, `ov_bg`, `bakes_per_s`,
  `mvk_host`). 0.39.2 : garde de valeur dans `/tally_bulk` (tally, texte de label, overlays).
- `plugins/multiview/plugin.json`, `meta.json` — version 0.39.2 + notes.
- `services/tsl/__init__.py` — `_distributor` : pas de ré-émission d'un paquet identique
  (re-synchro toutes les 5 s). ⚠ **Nécessite un redémarrage de l'orchestrateur pour prendre effet**
  (le thread distributeur est en vol). Le correctif côté mur, lui, ne demande qu'un redéploiement
  du mur — et suffit à lui seul à tuer le churn.

## 10. Pour appliquer sur la prod

1. Redémarrer l'orchestrateur (prend le correctif `services/tsl` + le registre plugin 0.39.2).
2. Redéployer le mur 333 (`Redéployer` sur la page Traitements) — il prendra 0.39.2.
3. Vérifier sur `http://192.0.2.21:8080/` : `bakes_per_s.chrome` doit être **0,0** au repos,
   `compose_breakdown_ms.ov_bake` **0,0**, `own_latency_ms` **~10-12 ms**, `fps` **50,0**.
4. Vérifier que le tally/UMD **réagit toujours** (bascule d'une source à l'antenne : le texte et la
   couleur doivent changer instantanément — le re-bake est conservé sur changement réel).
