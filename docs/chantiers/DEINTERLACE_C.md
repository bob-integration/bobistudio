# Chantier DIFFÉRÉ — désentrelaceur adaptatif (bwdif/yadif) en noyau C

> Créé 2026-07-14. Décidé après **mesure** : bwdif en numpy est deux fois hors budget de trame.
> Le portage C est le **seul** moyen d'avoir un désentrelaceur adaptatif utilisable. Rien ne presse :
> `bob` est livré (udc 0.10.0), il tient largement le budget et supprime le peigne.

## 1. Pourquoi ce chantier existe

L'UDC (`plugins/udc`) sait faire les 4 combinaisons de balayage. En **i → p**, il propose :

| Chemin (1 champ 1080i, Y+Cb+Cr, Xeon 6240R) | Coût | Budget 50p = **20 ms** |
|---|---|---|
| **tissage** (recolle les 2 champs) | 3,07 ms | ✅ mais **peigne** sur le mouvement |
| **bob** (livré, défaut) | 3,76 ms | ✅ pas de peigne, mais **½ résolution verticale** |
| **bwdif/yadif en numpy** (cœur seul, hors rééchantillonnage) | **40,6 ms** | ❌ **×2 hors budget** |
| variante allégée (sans champ n−2, spatial linéaire) | 30,4 ms | ❌ toujours hors budget |

**Le problème n'est pas l'algorithme, c'est numpy** : ≈ 10 passes mémoire pleines par champ. Un noyau C
fait ce travail en une passe, cache-friendly, vectorisée.

Précédent dans le projet : le noyau fusionné de l'UDC lui-même (`tools/fused_resize.c`) a donné **×3-4**,
et la fusion `mvk` du multiview bat numpy de **7 à 40×** (cf. `docs/chantiers/MULTIVIEW_BENCH.md`).
⇒ objectif réaliste : **bwdif < 8 ms/champ**, donc dans le budget.

## 2. Ce que fait bwdif (et pourquoi ça vaut le coup)

Désentrelaceur **adaptatif au mouvement**, décision **par pixel** :
- zone **fixe** → **tissage** : la résolution verticale est **intégralement conservée** ;
- zone en **mouvement** → interpolation : **pas de peigne**.

C'est le meilleur des deux mondes, là où `bob` sacrifie *toujours* la moitié de la résolution verticale
(même sur une image parfaitement fixe, où les 2 champs étaient pourtant parfaitement combinables →
mollesse et scintillement de ligne sur le détail fin).

Coûts intrinsèques :
- calcul lourd (détection de mouvement sur plusieurs champs) ;
- **+1 champ de latence** (~20 ms en 50i) : il a besoin du champ **suivant** pour décider.

## 3. LE MODÈLE À SUIVRE — `plugins/udc/tools/fused_resize.c` (ne réinvente rien)

Deux modèles de distribution de C coexistent dans le projet. **Prends le premier.**

### (a) ✅ `.so` pré-buildé, embarqué dans le plugin — CE QU'IL FAUT FAIRE
`plugins/udc/tools/` contient déjà : `fused_resize.c`, `build_fused.sh`, `embed_so.py`,
`bench_fused.py`, `test_integration.py`, `fused_resize.so`.

- **Build** (`build_fused.sh`) :
  ```sh
  cc -O3 -fPIC -shared -mavx2 -fno-plt -Wall -Wextra -o fused_resize.so fused_resize.c
  ```
  ⚠ `-mavx2` et **JAMAIS `-march=native`** : le `.so` est distribué, il doit charger sur **tous** les
  nœuds. À builder sur Debian trixie (glibc de l'image `bobi-compute`) ou plus ancien.
- **Distribution** : l'image `bobi-compute` n'a **ni compilateur ni numba** → le `.so` est **embarqué en
  base64 dans le script rendu** (`embed_so.py`), écrit sur disque à l'exécution, chargé par `ctypes`.
- **Gardes obligatoires** (recopie-les, elles sont toutes justifiées — `script.py` ~424-442) :
  - refus si `platform.machine() != "x86_64"` ;
  - refus si le CPU n'a pas AVX2 (lecture de `/proc/cpuinfo`) ;
  - ★ **écriture ATOMIQUE du `.so`** (`tempfile.mkstemp` + `os.replace`), **JAMAIS** `open("wb")` direct :
    tronquer un `.so` encore **mmappé** par un ancien process (restart d'agent, double exec)
    **SEGFAULTE ce process**. Le `replace` laisse l'ancien inode vivant pour les mappings existants ;
  - **toute** anomalie (arch, AVX2, ABI, écriture, chargement) → **repli numpy** + log, jamais un crash.

### (b) ❌ noyau buildé dans l'image runtime — À ÉVITER ICI
`plugins/_compute_runtime/Dockerfile` construit `libbobi_mvk.so` (`mvcompose.c`) avec deux variantes
(`-march=x86-64-v2` et `-v3`, + OpenMP). C'est le modèle du multiview.
**Inconvénient rédhibitoire pour ce chantier** : il impose un **rebuild + push de l'image runtime**
sur toute la flotte. Le modèle (a) ne demande qu'un bump de plugin.

## 4. Contrat attendu

Le côté Python existe déjà et attend le noyau :
- réglage `deint` : `weave` | `bob` | `bwdif` — **`bwdif` est aujourd'hui affiché DÉSACTIVÉ avec sa raison
  mesurée**, et `POST /params {deint:"bwdif"}` renvoie **400** (règle du projet : jamais de contrôle
  muet, jamais de repli silencieux). À rouvrir quand le noyau existe.
- `/state` expose déjà **`deint_latency_fields`** (= 0 aujourd'hui) — **prévu pour ça**.

Le noyau doit traiter **les 3 plans** (Y, Cb, Cr) et les profondeurs **8 et 10/12 bits** (u8 / u16),
comme `fused_resize`. Il a besoin des champs **n−2, n−1, n, n+1** (bwdif) ou **n−1, n, n+1** (yadif
simple) → l'appelant doit maintenir un petit anneau de champs.

## 5. ★ Le genlock — le piège de ce chantier

La sortie de l'UDC est calée en **phase sur la grille PTP** (`index_mode=tai` : l'index de trame **EST**
l'index de grille TAI). `bob` n'ajoute **aucune** latence (un champ suffit) → rien à compenser.

**bwdif ajoute +1 champ.** Cette latence doit être **assumée et intégrée au calage**, pas subie :
un champ de retard mal compensé = une image **en avance ou en retard d'une trame, en permanence**.
`deint_latency_fields` est là pour la déclarer. Vérifie l'index de sortie contre la grille TAI sur
≥ 30 s (l'agent de la 0.9.0 a mesuré `index_trame − index_grille_TAI` = **0**, min = max = 0, sur
1500 champs — garde ce niveau d'exigence).

## 6. Critères d'acceptation (chiffrés, non négociables)

1. **Coût < 8 ms/champ** en 1080i (Y+Cb+Cr), mesuré sur le banc — sinon le chantier a échoué.
2. **Score de peigne** au moins aussi bon que `bob` (métrique déjà en place :
   moyenne de `max(0, (x[y]−x[y−1])·(x[y]−x[y+1]))`). Références mesurées 1080p50 sur source à mouvement
   rapide : **tissage 957,4 → bob 21,9**. bwdif doit être ≤ bob.
3. **Résolution verticale conservée sur les zones FIXES** — c'est tout l'intérêt : le prouver
   (une mire de détail fin statique doit rester nette, là où bob la ramollit).
4. **Genlock intact** : 0 régression d'index sur 30 s, latence déclarée dans `/state`.
5. **Repli numpy** fonctionnel si le noyau ne charge pas (arch, AVX2, ABI).
6. Non-régression des 4 combinaisons de balayage (harnais 0.9.0 déjà écrit).

## 7. Pièges déjà payés (ne les repaie pas)

- **La phase de parité.** La ligne *k* du champ de parité *p* occupe la ligne de trame *2k+p* → la grille
  de rééchantillonnage est décalée de **±¼ ligne de champ** (`_plane_map(yshift=…)`). Sans cette
  correction, les 2 champs sont rendus à la même hauteur → **saut vertical d'½ ligne à 25 Hz**.
  Mesure de contrôle : barycentre vertical d'une bande fine, champ pair vs impair → doit être **0,000**.
- **Ne jamais rééchantillonner une trame tissée** : ça mélange des lignes de **deux instants** — le peigne
  n'est pas conservé, il est **étalé** sur les lignes voisines. Traiter chaque champ comme ce qu'il est :
  une image progressive de demi-hauteur.
- **La mire statique ne prouve rien** d'un désentrelaceur. Il faut du **mouvement** (le harnais existe :
  barre à 32 px/champ, `scratchpad/deint_*` et `ab_*`).

## 8. Où sont les choses

- Plugin : `plugins/udc/` (sous-module git). Script : `script.py` (passe dans `str.format` → **accolades
  littérales doublées `{{ }}`**). Noyau existant : `tools/fused_resize.c` + `build_fused.sh` + `embed_so.py`.
- Banc : nœud 30 (dl360-1, 192.0.2.251). Source entrelacée native : mur multiview 1080i50 `mvi50`.
  ⚠ Sa source bouge peu (mur d'horloges) → le peigne franc se prouve au **harnais**, le live corrobore.
- Docs liées : `docs/chantiers/MULTIVIEW_BENCH.md` (le précédent `mvk`, ×7-40), `docs/chantiers/MULTIVIEW_PERF.md`.
