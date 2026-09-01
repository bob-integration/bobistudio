# Gestion du CPU — état, règles et méthode

> Rédigé le 2026-08-01 au terme d'une journée de mesures sur le parc. Ce document dit **ce qu'on
> sait maintenant**, **ce qu'on ne savait pas hier**, et **comment mesurer** pour ne pas retomber
> dans les erreurs qui ont coûté cette journée. Il complète `TODO.md`, qui porte les actions.

---

## 1. Le constat qui recadre tout

L'attribution CPU d'un nœud 2110 en production, relevée par delta de `/proc/*/stat` :

```
mtl_rx            962,2 %   ← 97 % de TOUTE la consommation du nœud
mxl-fabrics-demo   13,7 %   ← 12 liens RDMA, 9,0 Gb/s répliqués
python3 (contrôleur)7,8 %
dockerd             2,0 %
TOTAL             991,3 %
```

**La question CPU de ce projet, c'est le dimensionnement du moteur 2110.** Le reste est du bruit de
mesure. Une journée entière avait été consacrée au placement de conteneurs qui pèsent 3 % du nœud.

Et le partage de la machine (dl360-1, 48 CPU) :

```
42 CPU isolés          21 % chacun   ← 10 boucles busy-poll, 32 CPU réservés pour rien
 6 CPU ordonnançables  95 % chacun   ← noyau, dockerd, conteneurs, threads de service
```

…pour un `cpu_pct` global de 23,7 %, parfaitement au vert. **88 % de la machine est réservée à dix
boucles**, et les six CPU restants portent tout le reste.

---

## 2. Les trois notions qui manquaient

### 2.1 Un cœur ATTRIBUÉ n'est pas un cœur UTILISABLE

`isolcpus=domain` ne rend pas un cœur inutilisable : il le retire de l'**équilibrage de charge**.
Un thread qu'on y affine explicitement (un lcore DPDK) y tourne ; un thread ordinaire n'y est
jamais **migré**. Un conteneur peut donc recevoir seize cœurs et n'en avoir qu'un pour de vrai —
c'était le cas du moteur de dl360-1, avec **274 threads sur le cœur 0**.

→ `core_pool.ordonnancables()` ; le partage est publié par `node_health` (`cpu_partage`), affiché
sur la carte CPU de Monitoring, et historisé (`cpu_ord`, la seule courbe qui dise quelque chose sur
un nœud isolé). `capacite()` et `cores_status()` ne comptent plus que des cœurs ordonnançables.

### 2.2 Un placement CALCULÉ n'est pas un placement CONSTATÉ

`core_pool` a reçu neuf correctifs, chacun rattrapant une qualité du cœur que le modèle ignorait
(jumeau HT, nœud NUMA, dédié/partagé, fréquence, cpuset absent…). **Les neuf calculent en amont ;
aucun ne regardait le résultat.** La dixième espèce — « le cœur peut être isolé » — a donc vécu
invisible jusqu'à un relevé manuel d'une ligne.

→ `app/placement.py` **constate** : bande isolée réelle, cpuset réellement posé sur **chaque
conteneur Docker du nœud** (y compris ceux que l'orchestrateur n'a pas créés), répartition réelle
des threads. Deux invariants, sans seuil :

- **I1** `cpuset ∩ bande_isolée = ∅` — tout cœur donné doit être un cœur où l'ordonnanceur ira ;
- **I2** `threads(conteneur ≠ moteur) ∩ bande_isolée = ∅` — la bande appartient au moteur.

### 2.3 Un coût DÉCLARÉ n'est pas un coût MESURÉ

Les profils `resources.cores` des manifestes n'étaient confrontés à rien. `streamer` annonce
4 cœurs depuis 2026-06 ; personne n'avait vérifié.

→ `app/cpu_profiles.py` accumule ce que l'agent remonte déjà (`containers.cpu_percent`), par
`(type, nœud)`, sur 4 h glissantes, et le confronte au manifeste : `conforme` /
`sur_dimensionne` / `sous_dimensionne` / `sature` / `non_declare`. `projects.verifier_capacite`
consomme le p95 **mesuré** quand il existe et **nomme la source** de chaque chiffre dans son refus.

---

## 3. Coûts mesurés (2026-08-01)

| charge | coût | note |
|---|---|---|
| `streamer` 1080p50 h264 ultrafast | **≈ 3 cœurs** | ce qu'un NVENC libérerait |
| moteur 2110 (10 lcores + services) | **≈ 9,3 cœurs** | 97 % du nœud |
| lien RDMA (verbs) | **1,2 %** d'un cœur | **indépendant du débit** |
| `avsync` 1080p50 — Cascade Lake 4,0 GHz | 36 % | |
| `avsync` 1080p50 — Broadwell 3,6 GHz | 44 % | |
| `avsync` 1080p50 — Broadwell 3,5 GHz | 50-56 % | |
| `avsync` 1080p50 — Sandy Bridge 2,8 GHz | 74 % | |

**Loi d'échelle d'un producteur MXL** : ≈ 8,8 % de socle fixe + **0,34 % par Mpx/s**. À 1080p50,
80 % du coût est proportionnel au volume écrit — du déplacement de données, pas du calcul. Établi
en comparant deux formats à débit de pixels voisin mais à cadence double : **B ≡ C au dixième
près**, donc aucun coût par trame.

**Conséquence** : ne pas déporter un producteur sur GPU. Le ring MXL vit en mémoire hôte ; il
faudrait recopier D2H ce qui doit y atterrir de toute façon. Le GPU paie sur forte intensité
arithmétique par octet déplacé (composition N→1) ou sur asymétrie favorable (encodage : trames
en entrée, flux compressé en sortie).

---

## 4. La méthode — et ses pièges

> Trois inférences se sont trompées dans la même journée, dont une d'un facteur 30. **Un écart se
> mesure toutes choses égales par ailleurs, ou ne se déclare pas.**

**Mesurer un coût** : delta `usage_usec` du cgroup du conteneur
(`/sys/fs/cgroup/system.slice/docker-<ID_COMPLET>.scope/cpu.stat`), fenêtre ~30 s.
⚠ `docker ps` rend l'ID **court**, le cgroup porte l'ID **complet** → glob sur le préfixe.

**Ne PAS mesurer avec `containers.cpu_percent`** : arrondi au dixième **puis** normalisé par
`cpu_count`. Sur une machine à 88 CPU le pas vaut **8,8 % d'un cœur** — deux crans consécutifs
s'affichent « +50 % ». Utile pour une tendance, inutilisable pour un écart.
Coût **absolu** = `cpu_percent × cpu_count` (le moteur y ressort à « 58 % » alors qu'il prend
9 cœurs). L'agent **écrête à 100** : un conteneur saturant son cpuset rend un **minorant**.

**Comparer deux variantes** : déployer les deux **simultanément**, même machine, même source, mêmes
paramètres. Et garder un **témoin** — la même variante des deux côtés — pour connaître le plancher
de bruit. Mesuré ici : ±8 % sur une charge de 290 %, 0,3 % sur une charge de 17 %.

**Vérifier que le conteneur TRAVAILLE avant d'interpréter** (`containers.fps`). Deux régimes sur
cinq mesuraient des conteneurs au repos, câblage inopérant.

**Vérifier que la variante testée est bien celle déployée.** L'image d'un conteneur vient de
`nodes.compute_image`, lu **au moment du `docker run` asynchrone** : basculer le champ puis le
remettre trop tôt fait repartir le conteneur sur l'ancienne image. Contrôler par
`docker inspect -f '{{.Config.Image}}'`, jamais par intention.

---

## 5. Ce qui reste à faire, par ordre de valeur

1. **Redimensionner la bande isolée.** `mtl_lcore_max` = 16 réserve 42 CPU pour 10 lcores réels.
   Un réglage, un reboot, vérification par le partage du §1. C'est le seul levier à l'échelle du
   nœud. ⚠ Élargir le cpuset du moteur **ne suffit pas** — mesuré : 36,2 vs 36,4 fps, aucun effet.
   On redistribue une pénurie ; il faut la supprimer.
2. **Refuser la capacité `compute` à un CPU sans AVX2/FMA** (`app/cpu_qualify.py`). Trois nœuds du
   parc acceptent des déploiements qu'ils ne peuvent pas exécuter (SIGILL dans `libmxl`), et
   l'orchestrateur n'affiche que `script_stopped`.
3. **Faire remonter le code de sortie du script dans le statut.** `script_stopped` sans cause est
   ce qui a coûté le plus de temps ; le `-4` (SIGILL) n'était visible que dans `docker logs`.
4. **Borner les conteneurs hors modèle** (`rdma-*`) : ils naissent sans cpuset et peuvent tourner
   sur les lcores busy-poll. Coût CPU négligeable, mais I2 violé.
5. **Confronter les profils `resources.cores` aux mesures** une fois la collecte mûre, et corriger
   les manifestes sur-dimensionnés — ils font refuser des déploiements pour du vide.
6. **Le choix du nœud ignore la capacité** (constaté 2026-08-02). `pick_compute_node()` rend « le
   premier nœud éligible », c'est-à-dire le premier par identifiant. `monitor.py` l'appelle sans
   préférence → **tout monitor atterrit sur dl360-1**, le nœud le plus contraint du parc (42 CPU
   isolés sur 48, moteur 2110 à 9,3 cœurs, pool de 2 cœurs physiques) alors qu'un monitor est un
   `streamer` d'environ 3 cœurs. dell-1 (88 CPU, sans moteur) reste inutilisé.

   ⚠ **Le correctif évident ne marche pas** : trier par `physical_free` choisirait encore dl360-1,
   parce que les autres nœuds n'ont **aucun `compute_cpuset` déclaré** et que `capacite()` lit
   « pas de pool » comme « zéro capacité » — alors que ça veut dire « machine entière ». Il faut
   donc (a) faire choisir le nœud sur les cœurs ORDONNANÇABLES réellement libres, et (b) traiter
   l'absence de pool comme « tous les CPU moins la bande isolée ».

7. **Puis seulement** : faut-il 10 cœurs à 100 % en permanence ? `mtl_sch_quota_mbs` est la
   manette. Ne pas y toucher avant 1-6.
