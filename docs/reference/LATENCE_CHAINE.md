# Latence de la chaîne — ce qu'on mesure, ce qu'on affiche, ce qui manque

> **État au 2026-08-18.** Document de RÉFÉRENCE : il fait foi sur la sémantique des grandeurs de
> latence. Écrit après le constat suivant, page Câbles, mode « Cumulé » : le mur affiche **4,3 ms**
> alors qu'il ajoute **4,0 trames (80 ms)** à la chaîne — un facteur ~19. Ce n'est pas un bug de
> calcul : c'est qu'on additionne une grandeur qui n'est pas celle qu'on croit lire.

---

## 1. Trois grandeurs, à ne JAMAIS confondre

| grandeur | définition | unité | répond à |
|---|---|---|---|
| **Temps de calcul** `own_latency_ms` | `ts_out − ts_cycle_start` | ms | « ce plugin a-t-il de la MARGE ? » |
| **Transit d'arête** `latency_ms` | `ts_read(conso) − ts_in(prod)` | ms | « le consommateur lit-il vite ? » |
| **Délai d'étage** `delai_etage_trames` | `index_sortie − index_entrée` | **trames** | « combien de temps le SIGNAL met-il ? » |

Les deux premières sont des **durées de travail**, sous-trame. La troisième est un **âge de contenu**,
en trames entières. **Additionner les deux premières ne donne pas la troisième**, et aucun réglage
ne les réconciliera : un étage peut calculer en 4 ms et retarder le signal de 2 trames, parce que
ce qui domine est la QUANTIFICATION par la cadence (lire un contenu daté N, publier en N+2), pas
la durée du calcul.

Le plugin multiview le dit dans son propre code :

> « Délai que CET étage ajoute à la chaîne, en TRAMES (index de sortie − index d'entrée).
> **C'est la seule mesure directe de la latence d'un étage ; tout le reste est un modèle.** »

### Le temps de calcul reste PRÉCIEUX — mais pas comme une latence

Il ne faut pas le retirer : c'est l'indicateur de **charge et de marge**. Un mur à 4,3 ms sur un
budget de trame de 20 ms (50 fps) consomme 21 % de son créneau — il a 79 % de marge. C'est cette
lecture-là qui dit si un étage va décrocher, et elle n'a rien à voir avec le retard du signal.

**Il doit donc être affiché comme un RATIO DE BUDGET, pas comme un délai.** Un « 4,3 ms » sur une
page de latence se lit inévitablement comme un retard ; « 4,3 / 20 ms — 21 % du budget » ne peut
pas être mal lu.

---

## 2. Ce que la page Câbles additionne aujourd'hui

`app/routes/home_dashboard.py:967`, `_delay_out(vmid)`, le long de l'entrée de RÉFÉRENCE :

```
delay_total = base(récursif amont) + transit(arête de réf) + own_latency_ms(ce nœud)
```

Donc le mode « Cumulé » accumule des **temps de calcul** et des **transits**. Il ne contient
aucune trame. Relevé sur le parc, serveur en cours :

| nœud | `own_latency_ms` | `delai_etage_trames` (publié par le plugin) |
|---|---|---|
| multiview | 4,3 ms | **4,0 trames = 80 ms** |
| pyramide | 0,6 ms | non publié |
| avsync | 4,3 ms | non publié |

`grep -rn delai_etage_trames app/ templates/ static/` → **aucune occurrence**. La seule mesure
directe de la chaîne est publiée sur `:8080` et **jetée**.

### Les shards SONT comptés

Contrairement à ce qu'on pourrait croire en voyant les internes du tissu repliés dans l'interface :
`_delay_out` traverse les shards, parce qu'ils sont dans `topology.nodes`/`edges` et que le repli
est purement **visuel** (filtre front, appliqué APRÈS que le serveur a calculé `cum_ms`). Le chiffre
n'est donc pas « la composition seule » — il est faux pour une autre raison, la même à chaque étage.

---

## 3. Les deux bouts manquants

| segment | réalité mesurée | dans le cumul |
|---|---|---|
| réception 2110 | **19,3 ms ≈ 1,00 trame** (`rx_latency_ms`, publié par le moteur) | **exclu** — badge ⇣ séparé, jamais additionné |
| traitement | trames entières | remplacé par le temps de calcul |
| émission 2110 | **~20 ms ≈ 1,00 trame** | **non modélisé** — aucune valeur, nulle part |

`_delay_out` renvoie `0.0` pour un nœud sans arête vidéo entrante (« source → origine ») : la
capture 2110 est donc structurellement hors du cumul. Le TX, lui, est un sink terminal : on calcule
le délai à son ENTRÉE (`delay_in_ms`), jamais le coût de sa mise sur le fil.

**Conséquence : le mode « Cumulé » ne peut pas, par construction, converger vers le fil-à-fil.**

---

## 4. Un bug : la collision de vmid RX/TX corrompt le cumul

Les deux moitiés du moteur 2110 sont **deux nœuds de topologie portant le MÊME vmid** (619 —
« (RX) » et « (TX) »). `_delay_out` est keyé sur le vmid seul (`_memo[vmid]`, `in_v_edges[vmid]`).
Partant du RX, il emprunte donc l'arête d'entrée du **TX** (`multiview → 619`), remonte dans le mur,
et en rapporte le temps de calcul :

```
2110-io-dl360-1 (RX)   delay_total des sorties = [4,3]   ← le temps de calcul du MUR
arête 619 → 983 (RX vers le mur)        cum = 4,3        ← avant que le mur ait rien fait
```

C'est circulaire. Le garde-fou anti-cycle (`stack`) empêche la récursion infinie, mais laisse une
valeur absurde au lieu de signaler. **C'est ce qu'on lit « à droite » sur la page.**

Correctif : clé de cumul = **(vmid, rôle)** et non vmid seul, partout où le moteur est scindé.

---

## 5. Référence mesurée — la chaîne réelle

Mesure aux bandeaux-sonde du **2026-08-12** (âge absolu du contenu contre horloge TAI ; la seule
méthode qui ait un contrôle interne, cf. `latence-age-absolu-contre-horloge-tai`) :

| étage | trames | ms |
|---|---|---|
| réception 2110 | 1,00 | 19,3 |
| pyramide (proxy) | **0,00** | 0 |
| réplication RDMA (aller) | 0,08 | 1,7 |
| composition du mur | **2,00** | 40 |
| réplication RDMA (retour) | 0,09 | 1,8 |
| émission 2110 | 1,00 | 20 |
| **fil à fil** | **4,0** | **80** |

⚠ Le 2,00 du mur **contient** la pyramide et la réplication aller — ne pas additionner la colonne
naïvement. Segments composables : réception + [source → sortie du mur] + émission.

⚠ Le 1,00 de l'émission est **déduit par soustraction** de la boucle mur→TX→fil→RX, pas mesuré.

---

## 6. Le segment TX est-il mesurable ? Oui — et les pièces existent

La question ouverte était « on ne peut pas vraiment mesurer le TX ». C'est inexact : ce n'est pas
impossible, c'est **jamais fait**. État du moteur (`plugins/2110_io/mtl_rx.c`) :

**Chemin TRANCHE (`SLICE_MODE=1`) — l'essentiel est déjà là :**
- `notify_frame_done` est câblé (`tx_sl_frame_done`, l.2117) ;
- `meta->epoch` donne l'instant fil exact, et le code s'en sert déjà (`_lead`, l.2004) ;
- un accumulateur `sl_emit_ns` mesure déjà « 1ʳᵉ sollicitation → frame_done » (l.768).

Il manque à confronter cet epoch au **TAI du grain** — l'index de grain MXL étant normativement
dérivé du temps, l'âge du contenu au moment du départ fil est directement calculable.

**Chemin TRAME PLEINE (`st20p`) — plus de travail :**
- aucun `frame_done` n'est câblé ;
- l'attente de pacing est *à l'intérieur* du `st20p_tx_get_frame()` **bloquant** — mesurer au retour
  de `get_frame` raterait l'attente qui suit `put_frame`, où la trame patiente jusqu'à son epoch.
  Il faut ajouter le callback pour lire l'epoch réellement utilisé.

Le modèle à répliquer est `accum_rx_latency` (l.997) : différence d'horodatages avec retrait de
l'écart TAI↔UTC par arrondi seconde. Le helper de conversion (`media_ts_to_tai`, l.291) existe, et
l'horodatage matériel est déjà activé sous `tp_wanted()` (`MTL_FLAG_ENABLE_HW_TIMESTAMP`, l.3929).

**Verdict : du C borné dans un seul fichier, pas un problème de recherche.** À valider contre la
sonde à bandeaux — aucun compteur interne ne fait foi seul, c'est la leçon des quatre conclusions
fausses du 2026-08-11.

**En attendant** : afficher l'émission comme **constante déclarée, explicitement étiquetée
« estimé, non mesuré »** (1,00 trame, cohérent avec la mesure d'août). Une constante annoncée comme
telle est honnête ; une constante silencieuse est un mensonge.

---

## 7. Ce qu'il faut afficher — deux axes, pas un

Le tort de l'interface actuelle est d'avoir **un seul sélecteur** (« Par étape ⇄ Cumulé ») pour
deux questions différentes. Il en faut deux, explicitement nommés :

**Axe A — CHARGE (« ce plugin tient-il ? »)** — l'existant, reformulé en budget :
- par nœud : `own_latency_ms` / période de trame → `4,3 / 20 ms · 21 %`
- par arête : transit
- alerte quand le ratio approche 100 % — c'est là que se lit le décrochage à venir

**Axe B — DÉLAI (« combien de temps le signal met-il ? »)** — nouveau, en TRAMES :
- par nœud : `delai_etage_trames` (mesuré) ou **« non mesuré »** — jamais un chiffre de calcul
  présenté comme un délai
- cumul de bout en bout : réception + Σ étages + émission
- chaque terme étiqueté **mesuré** / **estimé** / **non mesuré**

Règle non négociable, héritée des alarmes : **absence de mesure = absence de chiffre.** Un total
partiel doit se présenter comme partiel (« 2,0 trames + émission non mesurée »), jamais comme un
total.

---

## 8. Plan

1. **Corriger la collision vmid RX/TX** (clé `(vmid, rôle)`) — bug franc, indépendant du reste.
2. **Intégrer le segment A** : `rx_latency_ms` entre dans le cumul au lieu d'être un badge isolé.
3. **Remonter `delai_etage_trames`** du multiview jusqu'à la page ; « non mesuré » ailleurs.
4. **Séparer les deux axes** dans l'interface ; le temps de calcul devient un ratio de budget.
5. **Propager `delai_etage_trames`** aux 8 autres plugins de traitement — `avsync`,
   `color_corrector`, `delay`, `mixer`, `pyramide`, `split`, `udc`, `v210_bridge`. Le patron est
   uniforme et peu coûteux : tout plugin qui lit et écrit des grains MXL a les deux indices.
   ⚠ Touche 8 sous-modules, avec bump de version et redéploiement sur un parc de PRODUCTION.
6. **Mesurer le TX** (§6), chemin tranche d'abord. Jusque-là, constante étiquetée.

Les étapes 1 à 4 ne touchent aucun conteneur : orchestrateur et gabarits seulement.

---

## Voir aussi

`docs/reference/PROBE_2110.md`, `docs/reference/TX_LAYOUTS.md`, `docs/chantiers/CPU.md`.
Mémoires : `latence-chaine-2110-etat-et-leviers` (état mesuré), `latence-age-absolu-contre-horloge-tai`
(la méthode qui boucle), `cables-latency-semantics` (le modèle de 2026-06-13),
`tx-ring-courbe-complete-4-est-le-defaut` (⚠ corrige « la profondeur d'anneau est la latence »).
