# Cadence des murs — où on en est, et ce qu'il reste

**8 août 2026.** Séance d'optimisation du multiview, du diagnostic à l'état actuel, puis les
pistes restantes avec leurs contreparties. Tous les chiffres viennent de relevés sur le parc, avec
écart-type et nombre d'échantillons — la variance de ces murs est du même ordre que les effets
recherchés, et l'ignorer m'a fait tirer trois conclusions fausses dans la soirée.

## Où on en est

| | cadence | `own` | pic | trames perdues/s |
|---|---|---|---|---|
| **906** assembleur | 50,0 ±0,0 | 11,1 ±0,2 | 14,1 | **0,00** |
| **926** shard | 48,2 ±0,6 | 12,9 ±0,6 | 30,8 | 1,76 ±0,61 |
| **927** shard | 48,4 ±0,4 | 11,1 ±0,3 | 27,4 | 1,61 ±0,42 |

*(20 relevés par mur, plugin 0.79.1, après stabilisation complète.)*

Point de départ de la soirée : 34 et 40 fps, avec 10 à 27 trames perdues par seconde.

**La moyenne n'est plus le problème** — 11 à 13 ms pour un budget de 20. Ce qui coûte encore des
trames, ce sont les **pics à 27-33 ms**, qui dépassent le créneau et en font rater un.

## Ce qui a payé, et ce qui n'a rien donné

| changement | effet mesuré | verdict |
|---|---|---|
| Textes à variables hors du chrome plein cadre | re-bake de 60 ms/s **supprimé** | ★ cause racine |
| Placement GPU fusionné, indices cachés | `in_place` 5-8 → **1,3 ms** | ★ gros gain |
| Atlas de glyphes du bandeau ANC | bake 8,2 → **1,4 ms** | ★ gain net |
| Vignettes de frise mises à l'échelle une fois | dessin 19,7 → **6,7 ms** | ★ gain net |
| Cache VRAM des tuiles d'habillage | transfert 4,4 → **1,1 ms** | ★ gain net |
| Cache de tuiles par élément | pic `ov_clock` 20,4 → **12,3 ms** | gain sur les pics |
| Signatures séparées horloges / ANC | `own` 22,7 → 15,4 ms | ★ gain net |
| Boulanger dédié pour l'ANC | `own` 18,8 → **32,9 ms** | ⛔ écarté |
| Cœur réservé à la composition | 43,7 → **39,2 fps** | ⛔ écarté |
| Frise audio ralentie à 1 Hz | +1 fps, image saccadée | ⛔ écarté |

**Trois échecs, une seule leçon.** Aucune des trois tentatives de parallélisme n'a déplacé les
pics d'un millimètre. L'explication qui les couvre est le **GIL** : un rendu PIL de 40 ms qui ne
relâche pas le verrou bloque la composition quels que soient son cœur et sa priorité. La voie qui
marche n'est pas de déplacer le travail mais de **le supprimer** — c'est ce qu'ont fait tous les
gains ci-dessus.

## Où vont les 13,3 ms qui restent (shard 926)

```
ov_blend     5,02 ms   ← le plus gros poste
ov_render    3,41        dont meters 1,39 · clock 1,84 · frises 0,20
in_read      1,75
in_place     1,28        (était 5-8 ce matin)
output       1,14
ov_convert   0,29
```

Et sur l'assembleur 906, dont le profil est différent (il recopie deux trames pleines au lieu de
redimensionner des tuiles) : `ov_meters` 3,24 et `output` 2,18 y dominent — c'est là qu'il faudrait
regarder s'il devenait un jour le maillon lent, ce qu'il n'est pas aujourd'hui.

## Les pistes qui restent

### 1. Grouper les lancements de blend — le plus gros poste

`ov_blend` fait 5,3 ms de moyenne et pique à 12,6. Chaque tuile d'habillage coûte trois
lancements de kernel (un par plan) ; à dix tuiles, une trentaine par trame, pour 345 kpixels —
soit 42 Mpixel/s sur un GPU qui en fait des milliers. **C'est du lancement, pas du calcul.**

*Avantage* — le poste le plus gros, et la technique est éprouvée : c'est exactement ce qui a fait
tomber `in_place` de 5-8 ms à 1,3. Gain attendu de l'ordre de 3 à 4 ms de moyenne, et autant sur
le pic.

*Inconvénient* — les tuiles ont des tailles et des positions différentes, donc le kernel groupé
demande une table d'offsets et un index par tuile. C'est plus délicat que le gather de placement,
et une erreur d'offset se voit à l'écran sous forme de tuile décalée. À faire avec un repli
compté, comme pour le placement.

### 2. Rendre le texte à variables moins cher — le pic restant

Le pic d'`ov_clock` (12,3 ms) est maintenant celui de ce seul élément : un texte multi-ligne
re-rendu toutes les 2 secondes dans une boîte large.

*Trois façons, par effort croissant :*

- **Espacer le rafraîchissement à 5 ou 10 s.** Une ligne de code, aucun risque. Un indicateur de
  charge n'a pas besoin d'être frais à 2 secondes près. Diviserait la fréquence du pic par 3 à 5.
  *Inconvénient* : la valeur affichée vieillit — sans conséquence ici, mais c'est un arbitrage à
  assumer.
- **Réduire la boîte à son contenu.** Le coût est proportionnel à la surface convertie.
  *Inconvénient* : change la mise en page de l'utilisateur.
- **Atlas de glyphes**, comme le bandeau ANC. *Inconvénient* : l'atlas impose une chasse fixe, ce
  qui convient à un timecode mais déforme un texte courant. Il faudrait un atlas à chasse
  variable, donc gérer l'alignement chroma par glyphe — nettement plus de travail.

### 3. Écrire la sortie directement dans le grain

`output` fait 1,16 ms : on concatène les trois plans en une trame complète, puis on recopie cette
trame dans le grain MXL. Écrire les trois plans directement dans la vue du grain économise
l'allocation et une passe mémoire.

*Avantage* — simple, ~0,6 à 0,9 ms, aucun changement visuel.
*Inconvénient* — c'est le chemin de sortie, le plus critique de tous : une erreur d'offset ne
produit pas une tuile décalée mais une image corrompue. À faire avec un contrôle de taille strict.

### 4. Supprimer la copie hôte à la lecture

`in_read` fait 1,76 ms, dont l'essentiel est une copie de chaque plan depuis le mmap du grain vers
un tableau numpy, avant le transfert épinglé vers le GPU.

*Avantage* — ~1 ms, et ça réduit aussi la pression mémoire, qui est le facteur limitant de ces
murs.
*Inconvénient* — lire le mmap directement dans le tampon épinglé demande de garantir que le
producteur ne réécrit pas le grain pendant la copie. Le contrat MXL le permet sur un grain
commité, mais c'est une garantie qu'il faut vérifier plutôt que supposer.

### 5. Sharding : ESSAI FAIT, et il s'inverse ★

Le mur est découpé en trois processus sur un seul GPU. J'ai mesuré cette nuit qu'un mur seul
occupe 16 % du GPU et trois murs 40 % : **le nombre de clients CUDA pèse**.

Surtout : quand j'ai comparé monolithe et shardé, le monolithe faisait 24 fps — mais c'était
**avant** les cinq optimisations de la soirée, qui lui profiteraient toutes. Les termes de la
comparaison ont changé, et je ne sais pas de quel côté elle penche aujourd'hui.

**Essai fait le 8 août à 10 h 39** (20 relevés, après stabilisation complète) :

| | cadence de CONTENU | `own` | pic | perdues/s | processus | cœurs | GPU |
|---|---|---|---|---|---|---|---|
| shardé | 48,2 (le shard le plus lent) | 12,9 / 11,1 | 27-31 | 1,61-1,76 | **3** | **9** | 40 % |
| **monolithe** | **48,7 ±0,3** | 14,4 ±0,2 | **23,4** | **1,30 ±0,28** | **1** | **3** | **12 %** |

**Le monolithe gagne sur tous les axes** : même cadence, moins de trames perdues, pic plus bas —
avec un tiers des processus, un tiers des cœurs et trois fois moins de GPU. Six cœurs rendus au
pool de dell-1, deux clients CUDA de moins.

C'est l'inverse du verdict d'hier soir, et pour une raison simple : le monolithe faisait alors
24 fps **avant** les optimisations de la nuit, qui lui profitent toutes. Les termes de la
comparaison avaient changé, et il fallait la refaire au lieu de raisonner sur l'ancienne.

*Reste vrai* — le tissu re-shardera de lui-même si `own` dépasse le budget de 20 ms. À 14,4 il ne
le fera pas, mais la marge est de 5,6 ms : ajouter des fenêtres ou de l'habillage à ce mur peut
l'y ramener. Le garde-fou à écrire n'est donc pas « ne jamais sharder sur GPU » (j'ai essayé, c'était
fondé sur une mesure fausse) mais « vérifier que le découpage rapporte avant de le garder ».

### 6. Ce qui est fermé

- **Paralléliser** (thread, boulanger dédié, cœur réservé) : trois échecs mesurés, cause GIL.
- **Un second GPU** : les deux T4 du parc sont sur les R620, qui n'ont pas AVX2 — un multiview y
  meurt en SIGILL. dl360-1 n'a pas de GPU.
- **`gpu_slice`** : le banc a montré que le découpage en bandes coûte du temps de trame ; c'est une
  fonctionnalité de latence, pas de débit.

## Recommandation

L'essai monolithe est **fait, et il a gagné** : le mur tourne désormais en un seul processus, et
six cœurs sont rendus au pool. C'est le plus gros gain de la matinée, et il ne coûte aucune ligne
de code.

Ensuite, dans l'ordre : **espacer le rafraîchissement du texte de ressources** (une ligne, supprime
le pic le plus fréquent), puis le **groupement des blends** (3 à 4 ms, du travail sûr mais du
travail). Les pistes 3 et 4 valent chacune environ 1 ms et touchent des chemins critiques : à ne
prendre que si les 20 ms restent hors d'atteinte après les deux premières.

Et une règle de méthode, apprise à mes dépens cette nuit : **mesurer avant de changer, et vérifier
qu'un effet sort du bruit avant de conclure**. Cinq de mes changements n'ont rien donné parce que
je déduisais les causes au lieu de les observer — le vrai coupable, un `%cpu%` qui déclenchait un
re-bake plein cadre chaque seconde, était visible dès le début dans `bakes_per_s`.
