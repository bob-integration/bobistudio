# Banc de performance du multiview — nœud 30 (CPU pur)

> ## ⏩ SUITE (2026-07-14, multiview 0.40.0) — les deux recommandations chaudes sont livrées
> - **Reco n°4 (VU-mètres : fond caché en YUV, barres peintes en YUV)** — ✅ **livrée**, validée par
>   l'utilisateur. Mesuré sur le mur de PROD 333 : `ov_meters` **2,4 → 0,50 ms**. Micro-banc (tuile
>   44×540, 2 canaux) : **1,118 → 0,098 ms/tuile (11×)**. L'écart de bit-exactitude annoncé au §6.1
>   a été **chiffré** : luma et alpha **bit-exactes** (max|Δ| = 0 sur 4 géométries × 84 niveaux ×
>   2-8 canaux) ; seul le chroma d'un **bord de barre** devient franc au lieu de moyenné —
>   **0,1 %** des échantillons chroma (2 canaux), **0,6 %** au pire (8 canaux). Repli bit-exact
>   intégral conservé : `meters_pil=true`. La reco n°5 (uint8 au lieu de float32) devient **sans
>   objet** : il n'y a plus de conversion du tout.
> - **Recos n°6 et 7 (le PIC de recomposition des frises)** — ✅ **traitées, mais autrement** :
>   plutôt que de baisser la cadence (n°6, arbitrage visuel) ou de faire défiler la tuile (n°7,
>   chantier), la recomposition a été **SORTIE de la boucle de composition** (thread boulanger,
>   0.40.0). Le pic ne tombe plus dans la trame : `ov_hist` **1,45 → 0,10 ms** sur le 333, et
>   `hist_bake_ms` porte désormais `async: true`. `AH_RECOMPOSE_S` reste à 0,2 s (5 Hz) — inutile
>   de dégrader la frise, son coût ne se voit plus.
> - **Conséquence contre-intuitive, mesurée** : la qualité des vignettes de la frise vidéo a été
>   **remontée** (256×144 + LANCZOS au lieu de 160×54 au plus proche voisin), ce qui rend le bake
>   **50 % plus cher** (`hist_bake_ms.max` 31 → 47 ms)… **sans aucun effet sur la trame**
>   (own 8,5 → 8,4 ms, 50,0 fps, 0 chute / 50 relevés sur cœurs dédiés). Le §5b (« la moyenne
>   ment, c'est un PIC ») reste vrai — mais le pic ne coûte plus rien puisqu'il n'est plus dans
>   la trame.

> Banc mené dans la nuit du 2026-07-13 au 2026-07-14 sur le nœud 30 (dl360-1, 48 threads / 24 cœurs
> physiques, **sans GPU**). Conteneur de banc dédié : **vmid 345 `mvbench`**, épinglé sur **4 cœurs
> physiques isolés (cpuset `8-11,32-35`)**, hors du pool compute partagé du nœud.
> Tous les chiffres viennent du `:8080` du conteneur (médiane de 20 relevés/s sur 20 s, après 14 s
> de chauffe). Charge utile = **reproduction exacte de la config du mur de prod 333** (Horace) :
> 1920×1080p50, 4 fenêtres 1080p, modèle de PiP complet (vidéo bordée + UMD + 2 barres tally +
> VU-mètres 2 canaux), 1 horloge PTP, 1 texte, 1 frise audio pleine largeur 120 s.

---

## 1. Conclusion (à lire en premier)

**Un bug a été trouvé et corrigé : le noyau de fusion C (`mvk`) ne s'appliquait JAMAIS au plan Y
des tuiles d'habillage.** `rgba_to_yuv` renvoyait l'alpha sous la forme d'une vue stridée
(`arr[..., 3]` dans un tampon RGBA entrelacé) ; les kernels mvk exigent un dernier axe contigu, donc
ils renvoyaient `False` **silencieusement** et chaque blend de tuile per-frame (frises, horloges,
bandeaux ANC) retombait sur le blend **numpy — 20× plus lent**. Mesuré : **26 % des blends** d'une
trame partaient au repli. **Corrigé, commité, gain mesuré −1,2 à −2,8 ms d'`own_latency`** selon la
charge (0.39.3). *N'affecte que les murs CPU* — sur un mur GPU les blends passent par cupy.

**Le coût de l'habillage n'est PAS là où on le croyait.** Sur le mur de prod reproduit (4 fenêtres,
1080p50), la ventilation réelle du surcoût d'habillage :

| Élément d'habillage | Surcoût mesuré (4 fenêtres) | Part |
|---|---|---|
| **VU-mètres** (2 ch/cellule) | **+2,75 ms** | **le plus cher, de loin** |
| Frise audio pleine largeur | +1,0 ms (amorti) — **mais pic de 6 à 15 ms** | 2ᵉ |
| Chrome statique (bordure + UMD + tally) | +0,9 ms | 3ᵉ |
| Horloge + texte | ≈ 0,0 ms | négligeable |
| Frise vidéo (2ᵉ frise) | +0,4 ms | négligeable |

**Les VU-mètres coûtent 3× la frise.** Et 86 % de leur coût est une **conversion RGBA→YUV refaite
chaque trame sur toute la tuile du mètre**, alors que seules les barres changent et qu'elles sont
d'une **couleur unie** (donc de YUV constant). C'est l'optimisation à faire ensuite : gain potentiel
mesuré **2,3 → ~0,3 ms** (voir §6.1). Non commitée (elle n'est pas bit-exacte sur le chroma des
bords de barre — décision produit à prendre).

**Le mur tient 50 fps jusqu'à 9 fenêtres sur 4 cœurs, pas 16.** Voir la courbe §4.

**Ce qui ne coûte rien** (contrairement aux soupçons) : le chrome statique EST bien caché (0 re-bake
par seconde au repos, mesuré `bakes_per_s`), l'horloge et le texte sont cachés par signature de
valeur, l'étiquette SILENCE/ABSENCE des VU coûte < 0,05 ms, et le blend du chrome est borné à sa bbox.

**Attention méthodologique majeure (§7)** : le nœud 30 est **massivement sur-souscrit** (moteur MTL
sur les cœurs 0-21, 7 conteneurs compute empilés sur `19-23,43-47`). Les toutes premières mesures,
faites sur le cpuset partagé, donnaient n'importe quoi (le même mur mesurait 18 fps puis 50 fps).
**Aucune mesure de perf sur ce nœud n'a de sens sans cpuset dédié.**

---

## 2. Le correctif livré (0.39.3) — chiffré

`plugins/multiview/script.py`, `rgba_to_yuv` : l'alpha est compactée (`np.ascontiguousarray`) une
fois au bake, au lieu d'être renvoyée comme vue de pas 4.

Pourquoi c'était invisible : les plans **chroma** passaient au kernel C (leur alpha sous-échantillonnée
est un tableau neuf), seul le plan **Y** — le plus gros — partait au repli numpy. Aucun symptôme,
juste des millisecondes.

Mesures AVANT/APRÈS, même session, même cpuset, script reverté puis remis (`ba.log`) :

| Cas | `own` avant | `own` après | `ov_blend` avant | `ov_blend` après |
|---|---|---|---|---|
| 4 fenêtres + habillage + 1 frise | 6,7 ms | **5,5 ms** | 3,2 ms | **0,5 ms** |
| 4 fenêtres + habillage + 2 frises | 8,6 ms | **5,8 ms** | 5,3 ms | **0,9 ms** |
| 16 fenêtres + habillage + 1 frise | 7,25 ms | **5,7 ms** | 3,0 ms | **0,5 ms** |

Replis mvk comptés (nouveau champ `mvk_miss` sur :8080) : **5 850 sur 21 900 blends → 0**.

*(Ces trois points ont été relevés pendant une absence du flux audio — les VU n'y sont pas rendus,
cf. §7.4 — d'où un `own` plus bas qu'au §3. Sans effet sur la validité du delta : le correctif touche
les tuiles de **frises/horloges**, dont l'alpha vient de `rgba_to_yuv`. Les tuiles de VU passent par
un autre convertisseur (`_rgba_to_yuv_xp`) dont l'alpha était déjà compactée — vérifié via `mvk_why`,
qui n'a listé QUE les tuiles de frise (250×1920) et d'horloge (102×442).)*

Micro-banc des primitives dans le conteneur (`micro.py`, tuile de frise 1920×250, 3 plans) :

| Primitive | Coût |
|---|---|
| `mvk_blend_into` × 3 plans (kernel C) | **0,19 ms** |
| blend numpy × 3 plans (le repli qu'on payait) | **4,62 ms** |
| copie directe × 3 plans (borne basse théorique) | 0,03 ms |

**Nouveaux champs de diagnostic sur `:8080`** (livrés avec le correctif) :
- **`mvk_miss`** — compteur de replis numpy par famille de kernel. **DOIT rester à zéro sur un mur
  CPU.** Toute valeur non nulle = un kernel C qui ne s'applique pas = coût ×20 quelque part.
- **`hist_bake_ms` {last, max}** — coût **unitaire** du re-bake d'une frise (c'est un pic, la
  moyenne le dilue).
- **`compose_breakdown_ms`** ventile désormais `ov_render` en **`ov_meters` / `ov_hist` / `ov_clock`**.

---

## 3. Coût de l'habillage, élément par élément

4 fenêtres 1080p (source 1920×1080), sortie 1920×1080p50, 4 cœurs. Cumulatif : chaque ligne ajoute
un élément à la précédente. (`final.log`, série H — audio présent, VU réellement rendus.)

| # | Config | fps | `own` ms | inputs | overlays | output | dont VU | dont frises | dont horloges | dont blend chrome | dont blend tuiles |
|---|---|---|---|---|---|---|---|---|---|---|---|
| H1 | vidéo nue (aucun habillage) | 50,0 | **5,7** | 4,5 | 0,1 | 1,1 | 0,0 | 0,0 | 0,0 | 0,0 | 0,0 |
| H2 | + bordure viewfinder | 50,0 | 5,5 | 3,8 | 0,9 | 0,9 | 0,0 | 0,0 | 0,0 | 0,85 | 0,0 |
| H3 | + bandeau UMD | 50,0 | 5,9 | 4,1 | 0,9 | 0,9 | 0,0 | 0,0 | 0,0 | 0,8 | 0,0 |
| H4 | + 2 barres tally | 50,0 | 5,85 | 3,9 | 1,1 | 0,8 | 0,0 | 0,0 | 0,0 | 1,0 | 0,0 |
| H5 | **+ VU-mètres** | 50,0 | **8,6** | 3,75 | 3,95 | 0,9 | **2,3** | 0,0 | 0,0 | 0,9 | 0,65 |
| H6 | + horloge PTP + texte | 50,0 | 8,6 | 3,6 | 4,05 | 0,9 | 2,35 | 0,0 | 0,1 | 0,8 | 0,7 |
| H7 | **+ frise audio** (= config prod) | 49,5 | **9,6** | 3,75 | 5,0 | 0,8 | 2,3 | **0,6** | 0,1 | 1,0 | 1,0 |
| H8 | + frise vidéo (2 frises) | 50,0 | 10,0 | 3,8 | 5,4 | 0,8 | 2,3 | 0,8 | 0,1 | 1,0 | 1,3 |

**Lecture :**
- **VU-mètres = +2,75 ms** (H4→H5). C'est le poste d'habillage le plus cher, **0,7 ms par cellule**.
- **Chrome statique (bordure+UMD+tally) = +0,9 ms**, et c'est *uniquement du blend* (`ov_convert`) —
  le rendu PIL, lui, est bien caché (`ov_render` reste à 0,1). Le cache fonctionne.
- **Horloge + texte = gratuit** (+0,0 ms) : le cache par signature de valeur les re-rend 1×/s.
  Exception mesurée : **horloge avec le champ IMAGES (FF) → +1,6 ms/trame** (la signature change à
  chaque trame, donc plus aucun cache — `b1.log` A13). **Ne pas activer les images sur une horloge
  d'un mur chargé.**
- **1ʳᵉ frise = +1,0 ms, 2ᵉ frise = +0,4 ms** en moyenne. Voir §5 : la moyenne ment.

---

## 4. Passage à l'échelle : nombre de fenêtres

Habillage prod complet + 1 frise audio, 1920×1080p50, **4 cœurs**. Deux séries indépendantes
(`b9.log`, `final.log`) — écart entre séries ≤ 0,3 ms, donc reproductible.

| Fenêtres | fps | `own` ms | inputs | overlays | dont VU | Budget 20 ms |
|---|---|---|---|---|---|---|
| 1 | 50,0 | 6,3 – 6,45 | 2,0 | 3,5 | 1,2 | ✅ 32 % |
| 2 | 50,0 | 6,7 – 7,15 | 2,2 | 3,7 | 1,5 | ✅ 35 % |
| 4 | 50,0 | 8,85 – 9,1 | 3,6 | 4,6 | 2,3 | ✅ 45 % |
| 9 | 49,9 | 12,6 – 12,8 | 6,2 | 5,5 | 3,1 | ⚠️ 64 % |
| **16** | **41,7** | **18,35** | **10,05** | **7,2** | **4,2** | ❌ **92 % → images perdues** |

**Verdict : sur 4 cœurs, le mur tient 50 fps jusqu'à 9 fenêtres. À 16 il décroche** (`own` 18,4 ms
pour un budget de 20 ms : la moindre bouffée — un re-bake de frise, un GC — fait tomber la trame ;
mesuré 41,7 fps au lieu de 50).

Les deux postes qui explosent avec le nombre de fenêtres :
- **`inputs` (2,0 → 10,1 ms)** — lecture + redimensionnement + placement des tuiles. Linéaire en
  nombre de fenêtres (chaque fenêtre lit une trame 1080p complète et la réduit).
- **VU-mètres (1,2 → 4,2 ms)** — linéaire aussi.

---

## 5. Les deux frises : leur coût RÉEL

C'était la question explicite. Réponse en trois chiffres.

**a) En moyenne, elles sont bon marché** (après le correctif §2) : `ov_hist` ≈ **0,5-0,9 ms/trame**
pour une frise audio pleine largeur (1920×250 px, 120 s), **+0,2 ms de blend**. Une 2ᵉ frise
n'ajoute que +0,4 ms (le code n'autorise **qu'une seule recomposition de frise par trame** —
amortissement déjà en place).

Série D (`b3.log`, 4 fenêtres + habillage, on ne fait varier que les frises) :

| Frises audio | `own` ms | `ov_hist` | `ov_blend` |
|---|---|---|---|
| 0 | 5,0 | 0,0 | 0,2 |
| 1 | 5,4 | 0,9 | 0,5 |
| 2 | 5,9 | 1,4 | 0,9 |
| 4 | 6,3 | 2,1 | 1,2 |

Le coût suit l'**aire** de la frise, pas sa durée ni son nombre de canaux :

| Variante (1 frise) | `ov_hist` |
|---|---|
| référence (pleine largeur, h=0,23, 120 s, 2 ch) | 0,9 |
| moitié de hauteur | 0,65 |
| moitié de largeur | 0,5 |
| durée 10 s au lieu de 120 s | 1,2 (≈ identique, bruit) |
| opacité 85 au lieu de 100 | 1,0 (identique) |
| 8 canaux au lieu de 2 | 1,1 (identique) |

**b) Mais la moyenne MENT : la recomposition est un PIC qui tombe entier dans UNE trame.**
Nouveau champ `hist_bake_ms` : **`last` 5 à 9 ms, `max` 15 à 17 ms** pour une frise audio pleine
largeur. La frise se redessine ~5×/s (au changement de colonne) : **5 trames par seconde sur 50
paient 6 à 15 ms d'un coup**, sur un budget de 20 ms. C'est confirmé par un test croisé : à 25 fps
(`S2`), `ov_hist` double exactement (2,0 ms) — la même dépense étalée sur deux fois moins de trames.

**C'est là que les frises font mal**, pas dans leur moyenne. Sur le mur de prod (`own` de base
~9,6 ms), une trame qui porte un re-bake de frise est à **~16-25 ms → image perdue**.

**c) La frise vidéo est ~5× moins chère que la frise audio** (`ov_hist` 0,1-0,4 vs 0,6-1,1) : elle ne
se recompose qu'à l'arrivée d'une vignette (≈ 1 Hz) contre 5 Hz pour l'enveloppe audio.

**Ce qu'on peut faire de mieux sur les frises** (par ordre de rapport gain/risque) :

1. **Baisser la cadence de recomposition de l'enveloppe audio** : `AH_RECOMPOSE_S = 0.2` (5 Hz).
   À 2 Hz (`0.5`), l'enveloppe avance de ~5 px de plus entre deux redessins — **invisible** sur une
   frise de 120 s — et le coût moyen **et** la fréquence des pics sont divisés par 2,5.
   *Une ligne. Non commité : c'est un arbitrage visuel, à toi de trancher.*
2. **Faire défiler au lieu de redessiner** : la frise est un ruban qui glisse. Aujourd'hui elle est
   **entièrement redessinée** (dessin RGBA + conversion YUV plein tuile) à chaque changement de
   colonne. Décaler la tuile YUV cachée de N colonnes (`np.roll`-like, ~0,03 ms mesuré) et ne
   dessiner **que les colonnes neuves** ramènerait le pic de 6-15 ms à **< 0,5 ms**. C'est la vraie
   correction, mais c'est un chantier (les graduations et l'horodatage absolu défilent aussi).
3. **Étaler la recomposition sur plusieurs trames** (déjà partiellement fait : budget 1 frise/trame).
   Ne suffit pas — une seule frise suffit à faire le pic.

---

## 5 bis. Format de sortie, cadence, entrelacé, mode tranche

4 fenêtres, habillage + horloge + texte + 1 frise. ⚠️ **Ces points ont été relevés pendant une
disparition du flux audio du nœud (§7.4) : les VU-mètres n'y sont donc PAS rendus** — il faut y
ajouter ~+2,7 ms pour les comparer aux tableaux §3/§4. Entre eux, ils sont comparables (deux séries
indépendantes, `final.log` et `fill.log`, s'accordent à ±0,3 ms sauf `1080i50`).

| Format | fps | `own` ms | inputs | overlays | output |
|---|---|---|---|---|---|
| 1280×720p50 | 50,0 | **4,1** | 1,8 | 1,6 | 0,7 |
| 1920×1080p50 (référence) | 50,0 | 5,4 | 2,3 | 1,8 | 1,3 |
| 1920×1080**i**50 | 50,0 | 4,2 – 5,6 | 1,8 | 1,3 | 1,0 |
| 1920×1080p**25** | 25,0 | 6,8 | 2,6 | 2,9 | 1,3 |
| 3840×2160p50 | **44,6** | **11,4** | 4,8 | 2,9 | **3,8** |
| 1080p50 **`slice_mode=on`** | 50,1 | **12,8** | 6,6 | 0,7 | **5,7** |

- **La 4K ne tient pas 50 fps sur 4 cœurs** (44,6 fps) : l'étage `output` triple (3,8 ms) et
  l'`inputs` double. Avec les VU (+2,7 ms) elle serait à ~14 ms, encore dans le budget mais sans
  marge — sur un nœud contendu, elle décroche.
- **1080p25 : `ov_hist` DOUBLE** (1,9-2,0 contre 0,9). C'est la démonstration la plus nette du §5b :
  le re-bake de frise est un **coût par seconde**, pas par trame — à cadence moitié, chaque trame en
  porte deux fois plus. **La preuve que la moyenne `ov_hist` masque un pic.**
- **`slice_mode=on` coûte +3,7 ms de CPU** sur la même charge (`output` 0,9 → 5,7 ms : le commit MXL
  progressif fait ~30 commits par trame au lieu d'un). Le mode tranche **achète de la latence
  sous-trame, il ne rend pas le mur plus rapide** — au contraire. À ne pas activer sur un mur déjà
  près de son budget.

---

## 6. Points chauds restants, chiffrés

### 6.1 VU-mètres : 86 % du coût est une conversion RGBA→YUV inutile — **la meilleure optimisation restante**

Micro-banc dans le conteneur (`micro4.py`), coût **par mètre et par trame** :

| Tuile de VU | copie du statique + peinture des barres | **conversion RGBA→YUV** | total |
|---|---|---|---|
| mur 1 fenêtre (88×1080) | 0,051 ms | **0,578 ms** (92 %) | 0,63 ms |
| mur 4 fenêtres (44×540) | 0,039 ms | **0,238 ms** (86 %) | 0,28 ms |
| mur 16 fenêtres (22×270) | 0,012 ms | **0,071 ms** (86 %) | 0,08 ms |

Le chemin actuel (0.31.0) fait, **chaque trame et pour chaque mètre** : copie du fond statique
caché (RGBA) → peinture des barres → **conversion RGBA→YUV de TOUTE la tuile**. Or :
- le fond statique ne change jamais → **son YUV pourrait être caché** ;
- les barres sont d'une **couleur unie** → leur YUV est une **constante** ;
- donc la conversion par trame est **entièrement redondante**.

**Proposition** : cacher le fond du mètre **en YUV** (3 plans + alpha), et peindre les barres
**directement dans les plans YUV** avec la constante de couleur pré-calculée. Coût par trame :
copie de 3 plans + écriture de quelques rectangles ≈ **0,03-0,05 ms au lieu de 0,28**.
**Gain projeté : `ov_meters` 2,3 → ~0,4 ms sur un mur 4 fenêtres, 4,2 → ~0,7 ms sur un mur 16.**
Sur le mur 16 fenêtres (aujourd'hui à 18,4 ms, hors budget) cela rendrait **~3,5 ms**.

⚠️ **NON COMMITÉ, et voici pourquoi** : peindre les barres directement en chroma sous-échantillonné
n'est **pas bit-exact** avec le chemin actuel (aujourd'hui le chroma des **bords de barre** est
*moyenné* par la conversion 4:2:0 ; en peignant en YUV il serait *franc*). L'écart est d'un pixel de
chroma sur le bord d'une barre de VU — invisible en pratique, mais le projet tient à la
bit-exactitude CPU/GPU. **Décision produit requise.** Le chantier est petit (`_meter_tile_gpu`).

*Note secondaire mesurée* : `_rgba_to_yuv_xp` convertit la tuile en **float32** avant l'appel du
kernel (0,238 ms) alors que l'appel direct en **uint8** coûte 0,189 ms — **−20 % gratuitement**, sans
aucun changement de rendu, si la tuile est déjà en uint8. À vérifier côté `_meter_tile_gpu`.

### 6.2 Le re-bake du chrome coûte une trame entière — mais il est bien caché

Micro-banc plein cadre 1920×1080 (`micro.py`) :

| Opération du re-bake | Coût |
|---|---|
| `PIL Image.new` + `alpha_composite` ×3 couches | **62,5 ms** (!) |
| `np.array(PIL RGBA)` | 2,6 ms |
| `mvk_rgba2yuv` (kernel C) | 6,8 ms |
| conversion RGBA→YUV en numpy pur (repli sans mvk) | **39,0 ms** (!) |

**Un re-bake de chrome plein cadre coûte donc de 10 à 70 ms** — de 0,5 à 3,5 trames. Sur le banc,
`bakes_per_s` est à **0** au repos : le cache tient, ce n'est pas un problème *ici*. (C'était le
problème du mur de prod 333 — churn TSL — traité en parallèle par l'autre agent en 0.39.2.)
Ce tableau donne l'ordre de grandeur du **prix d'un re-bake** : tout ce qui salit l'habillage à la
trame est catastrophique.

### 6.3 Étage `output` : 0,8-1,3 ms, rien à gratter

`np.concatenate` des 3 plans = 0,246 ms mesuré, la copie dans le grain MXL le reste. Une écriture
directe dans un buffer pré-alloué (sans `concatenate`) coûte **exactement pareil** (0,246 ms) — testé,
**aucun gain**. Ne pas y toucher.

### 6.4 Étage `inputs` : c'est lui qui explose avec le nombre de fenêtres

2,0 ms (1 fenêtre) → 10,1 ms (16 fenêtres). `mvk_place_into` (redimensionnement + placement fusionné
en C) est déjà 2× plus rapide que le numpy stridé (0,118 vs 0,223 ms sur un plan Y 1080p→540p) et
`mvk_miss.place` est à **0** : le kernel C s'applique bien. Le coût restant est celui de la
**lecture** (`bytes(src_view)` = copie de 3,1 Mo par fenêtre et par trame en mode whole-frame).
**Piste non explorée faute de temps** : lire les plans **sans copie** (`np.frombuffer` sur la vue du
grain) sur le chemin whole-frame — le mode tranche le fait déjà (vues zéro-copie).

---

## 7. Ce que ce banc a appris sur la MÉTHODE (important)

1. **Le nœud 30 est sur-souscrit et rend toute mesure absurde sans cpuset dédié.** Le moteur MTL tient
   les cœurs 0-21 (3 cœurs à 100 % en polling) ; 7 conteneurs compute sont empilés sur `19-23,43-47`.
   Sur ce cpuset partagé, le **même mur** a mesuré **18 fps puis 50 fps** à quelques minutes
   d'intervalle. Avec 4 cœurs dédiés (`8-11,32-35`), le même point rendu 3 fois donne
   **50,0 / 50,0 / 50,35 fps** et `own` **6,5 / 6,4 / 6,5 ms**. Bruit résiduel sur `own` : ±0,7 ms
   (porté par l'étage `inputs`) ; les sous-lignes de `overlays` sont stables à ±0,1 ms.
2. **`docker update --cpuset-cpus` ne survit pas à un déploiement** (le déploiement RECRÉE le
   conteneur). Le banc le repose après chaque déploiement, puis **relance le script** — sinon
   `bobimxl` dimensionne ses threads OpenMP sur le mauvais cpuset.
3. **Attendre que le déploiement ait vraiment atterri.** Le premier jeu de mesures était **décalé
   d'un cas** : la route de déploiement rend la main tout de suite (thread), et le banc mesurait
   encore la config précédente. Corrigé en attendant le changement de `StartedAt` du conteneur.
4. **`ov_meters = 0` ne veut PAS dire « les VU sont gratuits » — ça veut dire qu'ils ne sont pas
   rendus du tout.** Le flux `avsync_audio` du nœud 30 **apparaît et disparaît** (période observée
   ~10 min). Quand il est absent, le multiview ne produit **aucune tuile de VU** et le chrome du
   mètre disparaît aussi : le mur est **2,7 ms moins cher — et faux**. Plusieurs points des premières
   séries ont dû être jetés pour cette raison. **Toute campagne future doit vérifier `ov_meters > 0`
   avant de valider un point avec VU.**

---

## 8. Ce que je n'ai PAS pu faire / hypothèses écartées

- **Le mur de prod 333 n'a pas été touché** (lecture seule, consigne). Le correctif §2 **ne
  l'améliorera pas** : c'est un mur GPU, ses blends passent par cupy. Il bénéficie en revanche du
  correctif de churn TSL (0.39.2) traité par l'autre agent. **En clair : mon correctif sert tous les
  murs CPU du parc (tissu, shards, murs de nœud), pas Horace.**
- **Hypothèse ÉCARTÉE — l'étiquette SILENCE/ABSENCE des VU-mètres.** Je la croyais re-rendue en PIL
  chaque trame (elle l'est) et donc coûteuse (elle ne l'est pas) : mesurée en forçant le statut dans
  la boucle, **< 0,05 ms/trame**, même sur 16 cellules. La cacher n'apporte **rien**. Un prototype de
  cache a été écrit, mesuré, **jeté**. La note est consignée dans le code pour éviter que quelqu'un
  refasse le trajet.
- **Non mesuré : 25 et 36 fenêtres avec VU** — le flux audio a disparu pendant ces points (§7.4), et
  il n'est pas revenu avant la fin de la nuit. Deux tentatives de reprise, deux échecs. L'extrapolation
  de la courbe §4 donne `own` ≈ 26-28 ms à 25 fenêtres (largement hors budget).
- **Le tableau §5bis (formats) est lui aussi sans VU** pour la même raison. Ajouter ~+2,7 ms pour le
  comparer aux autres tableaux. Les comparaisons **entre formats** restent valides.
- **Pas de vérification VISUELLE du rendu.** Je n'ai pas dumpé d'image du mur de banc (l'utilisateur
  fait les vérifs visuelles). Les mesures sont donc « le code a bien exécuté ces chemins », pas
  « le mur est joli ».
- **Non testé : le comportement multi-murs sur un même nœud** (contention mémoire), déjà couvert par
  le banc de capacité multiview existant (mémoire `multiview-server-capacity-bench`).

---

## 9. Recommandations

| # | Action | Gain | Risque | Statut |
|---|---|---|---|---|
| 1 | Correctif alpha contiguë (mvk s'applique enfin au plan Y) | −1,2 à −2,8 ms | nul (bit-exact) | ✅ **commité 0.39.3** |
| 2 | Redéployer **tous les murs CPU** du parc pour qu'ils prennent 0.39.3 | idem | nul | à faire |
| 3 | Surveiller `mvk_miss` (doit rester à 0) sur tout mur CPU | — | — | ✅ exposé |
| 4 | VU-mètres : cacher le fond en YUV, peindre les barres en YUV (§6.1) | **−2 ms (4 fen.), −3,5 ms (16 fen.)** | non bit-exact (chroma des bords de barre) | ⏸ **décision produit** |
| 5 | VU-mètres : appeler le kernel en uint8 au lieu de float32 (§6.1) | −20 % du coût VU | nul | ⏸ à vérifier |
| 6 | Frise audio : recomposition 5 Hz → 2 Hz (`AH_RECOMPOSE_S`) | pic ÷2,5 | visuel (invisible à mon avis) | ⏸ **décision produit** |
| 7 | Frise : défilement au lieu de redessin complet (§5) | pic 6-15 ms → < 0,5 ms | chantier | ⏸ à planifier |
| 8 | Ne jamais activer le champ **IMAGES (FF)** d'une horloge sur un mur chargé | +1,6 ms/trame évités | nul | doc |
| 9 | Corriger l'allocation de cœurs du nœud 30 (`core_pool`) | conditionne toute mesure | — | déjà connu (mémoire `node30-cpu-contention-canary`) |

---

## 10. Reproduire le banc

Tout est dans le scratchpad de la session (`mvbench.py`, `run_*.py`, `micro*.py`, `*.log`).

```python
import mvbench as B                      # déploie sur le vmid 345, repose le cpuset, relance, échantillonne
B.run("mon cas", B.build(n=4,            # 4 fenêtres
                         parts=("video","border","umd","tally","meters"),
                         overlays=("clock","text"), ahist=1))
```

`B.build()` construit la config prod-like ; `B.deploy()` attend la recréation du conteneur, repose
le cpuset `8-11,32-35` et relance le script ; `B.sample()` médiane 20 relevés sur 20 s.
**Vérifier `ov_meters > 0`** avant de croire un point comportant des VU-mètres.
