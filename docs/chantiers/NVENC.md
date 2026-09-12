# Portage NVENC du `streamer` — dossier de relecture

> Préparé le 2026-08-02. **Rien n'est appliqué** : ni le plugin, ni l'orchestrateur, ni les images.
> Ce document porte les faits vérifiés, les changements exacts, et les arbitrages qui te reviennent.

---

## 1. Ce qui est vérifié (mesuré, pas supposé)

**L'image média a déjà les encodeurs matériels.** Aucun rebuild n'est nécessaire.

```
bobi-media:0.12 → h264_nvenc · hevc_nvenc · av1_nvenc  (+ qsv, vaapi)
```

⚠ Attention au piège : l'image **compute** n'en a aucun. `streamer` et `transcoder` tournent sur
l'image **média** (`variant == "media"`), c'est ce qui rend le portage possible sans toucher au
build. Ma première vérification portait sur la mauvaise image et concluait l'inverse.

**Le seul verrou était une variable d'environnement.** `--gpus device=0` seul donne les capacités
`compute,utility` : `libnvidia-encode.so` n'est PAS injectée et ffmpeg échoue sur
`Invalid argument (-22)` — un message qui ne dit rien de la cause. Avec
`NVIDIA_DRIVER_CAPABILITIES=video,compute,utility`, la bibliothèque apparaît et l'encodage part.

**Gain mesuré au niveau du CONTENEUR** — remesuré le 2026-08-02 après la correction de cadence
(0.16.0), deux `streamer` simultanés sur dell-1 (P5000), même source `avsync` 1080p50, un **seul**
paramètre d'écart. Coût en pourcentage d'un cœur, delta `usage_usec` du cgroup, 3 relevés de 30 s :

| Configuration | x264 `ultrafast` | `h264_nvenc` p1/ull | Écart |
|---|---|---|---|
| `slice_mode` actif des deux côtés | **161,0 %** | **116,5 %** | **0,45 cœur (−28 %)** |
| `slice_mode` coupé côté x264 | 187,7 % | 108,2 % | 0,79 cœur (−42 %) |

Témoin (le générateur, inchangé) : 45,1 % puis 50,2 % — il a dérivé de 11 % entre les deux
campagnes. Les comparaisons **dans** un relevé valent ; d'un relevé à l'autre, non.

⚠ **Ne pas surestimer.** Le portage ne déplace que l'encodage : lecture MXL, conversion de chroma
et tuyau stdin restent sur le CPU et dominent le reste. NVENC coûte encore ~1,1 cœur par flux.

### Le chiffre de 1,97 cœur (commit 0.15.0) est FAUX

Le message de version 0.15.0 annonce « 353,6 % en x264 contre 156,1 % en NVENC, soit 1,97 cœur
rendu (−56 %) ». Non reproductible : x264 coûte 161 %, pas 353 %. Deux causes, dont une seule est
démontrée.

**Démontrée** — cette campagne comparait `slice_mode` **en même temps** que l'encodeur : coupé
côté x264, actif côté NVENC. Le biais existe, mais il ne vaut que ~0,27 cœur (mesure ci-dessus,
lignes 1 vs 2) : un quart de l'écart annoncé.

**Non démontrée** — le reste tient très probablement au bug de cadence lui-même, corrigé en
0.16.0 : l'ancien script alimentait ffmpeg en `-r 25` plus un filtre `fps=50` qui **dupliquait**
chaque image, soit une copie de trame complète 50 fois par seconde. Le vérifier exigerait de
redéployer l'ancienne version du plugin ; ça n'a pas été fait.

Conséquence à retenir : **le bug de cadence coûtait environ un cœur par flux**. Le correctif
0.16.0 est un gain de performance autant qu'un correctif de qualité — et il rend caduque toute
mesure de `streamer` antérieure.

> Leçon de méthode, la même que pour les trois inférences fausses du 2026-07-31 : deux paramètres
> qui changent ensemble ne produisent pas une mesure, mais une histoire. Ici le témoin a fait son
> travail — il a signalé une dérive de 11 % du nœud entre deux campagnes, qui aurait autrement été
> lue comme un effet de l'encodeur.

---

## 2. Les changements, dans l'ordre

### 2.1 Orchestrateur — `app/docker_compute.py` (~ligne 336)

Le chemin GPU actuel **remplace l'image par `compute_gpu_image`**. Appliqué tel quel au `streamer`,
il le ferait tourner sur l'image compute — qui n'a pas ffmpeg. Il faut n'allouer que le GPU quand
le plugin est de variante média :

```python
    gpu_sel = None
    if deploy_type and plugins.wants_gpu(deploy_type) and node.get("gpu_capable"):
        if variant == "media":
            # Le plugin média porte DÉJÀ ses encodeurs (ffmpeg/NVENC dans bobi-media) : on garde
            # son image et on n'alloue que le GPU. Basculer sur compute_gpu_image le priverait de
            # ffmpeg — le chemin GPU historique visait les plugins compute (cupy), pas ceux-ci.
            gpu_sel = gpu_pool.allocate_gpu(node["id"], vmid)
        elif node.get("compute_gpu_image"):
            image = node.get("compute_gpu_image")
            gpu_sel = gpu_pool.allocate_gpu(node["id"], vmid)
```

Et poser la capacité `video` sur le conteneur GPU (sinon `libnvidia-encode` reste absente) :

```python
        if gpu_sel:
            env["NVIDIA_DRIVER_CAPABILITIES"] = "video,compute,utility"
```

⚠ **Extension du contrat agent** : l'agent traduit déjà `spec["gpus"]` en `--gpus` (cf.
`NODE_AGENT.md`). Vérifier qu'il propage aussi l'environnement — sinon la variable n'arrive pas et
l'échec est le `-22` opaque ci-dessus.

### 2.2 Plugin `streamer` — `script.py`

Aujourd'hui (ligne 242) :

```python
VCODEC = {"h264": "libx264", "h265": "libx265"}.get(VIDEO_CFG.get("codec", "h264"), "libx264")
```

Les options NVENC ne sont **pas** celles de x264 : `-preset` prend `p1`…`p7` (et non `ultrafast`),
`-tune zerolatency` n'existe pas (c'est `-tune ll` / `ull`), et le contrôle de débit se pose par
`-rc cbr`. Passer les options x264 à NVENC échoue. Le bloc `vopts` (ligne 551) doit donc se
brancher selon la famille d'encodeur :

```python
_NVENC = {"h264": "h264_nvenc", "h265": "hevc_nvenc"}
_X26X  = {"h264": "libx264",    "h265": "libx265"}
_ENC   = str(VIDEO_CFG.get("encoder", "cpu")).lower()      # "cpu" | "nvenc"
VCODEC = (_NVENC if _ENC == "nvenc" else _X26X).get(VIDEO_CFG.get("codec", "h264"), "libx264")

# …puis, à la construction de vopts :
if _ENC == "nvenc":
    vopts = ["-c:v", VCODEC,
             "-preset", str(VIDEO_CFG.get("nvenc_preset", "p4")),   # p1 rapide … p7 qualité
             "-tune", str(VIDEO_CFG.get("nvenc_tune", "ll")),        # ll = faible latence
             "-rc", "cbr", "-b:v", str(VIDEO_CFG.get("bitrate", "4M")),
             "-pix_fmt", OUT_PIX_FMT,
             "-g", str(GOP), "-keyint_min", str(GOP), "-no-scenecut", "1"]
else:
    vopts = [...]   # bloc actuel, inchangé
```

**Repli** : si `encoder=nvenc` mais qu'aucun GPU n'est alloué, ffmpeg échouera. Le script doit
détecter l'absence de `libnvidia-encode` au démarrage et **le dire** (état `:8080` + journal)
plutôt que de boucler sur un `-22`. Cf. l'anti-patron de l'échec silencieux — c'est exactement ce
qui m'a coûté vingt minutes ce soir.

### 2.3 Manifeste `plugin.json`

```jsonc
"resources": { "cores": 4, "memory": 1024, "pin": false, "gpu": 1 },
```

⚠ `resources.gpu` déclenche l'allocation pour **toutes** les instances du type, y compris celles en
x264. Deux options, à trancher : rendre `gpu` conditionnel (l'orchestrateur ne le sait pas faire
aujourd'hui), ou accepter qu'un `streamer` réserve un GPU même sans NVENC. **La seconde gaspille
une ressource rare** — je penche pour ajouter au registre la notion de GPU *optionnel*, lu depuis
les params, mais c'est une modification du modèle qui mérite ta décision.

### 2.4 Exposition aux macros — **obligatoire**

Toute fonction de plugin doit être pilotable par macro, sinon la capacité est morte. `encoder`,
`nvenc_preset` et `nvenc_tune` doivent apparaître dans le `config_schema` **et** être joignables :
`param_tree` pour les valeurs continues, `actions[]` pour la bascule CPU↔GPU. Le manifeste actuel
n'a que `input` et `log_level` — il n'y a donc rien à étendre, tout est à créer.

---

## 3. Décisions prises (2026-08-02)

| point | décision |
|---|---|
| repli sans GPU | **trois modes** : `cpu` (x264 imposé) · `nvenc` (refus explicite sans GPU) · `auto` (prend ce qui est disponible **et le dit**) |
| preset par défaut | **`p1` + `-tune ull`** — cohérent avec l'`ultrafast -tune zerolatency` actuel. Valeur de `deploy_defaults` uniquement : modifiable par instance ET à chaud via `param_tree` |
| `resources.gpu` | **GPU optionnel** : le manifeste déclare que le type PEUT en tirer parti, l'allocation se décide sur les params de l'instance |
| monitor | **mode `auto`** : NVENC si un nœud à GPU est libre, x264 sinon — sans jamais bloquer la création |
| placement | le type déclare s'il exploite un GPU → **attirance** s'il l'exploite, **répulsion** sinon (un monitor ne doit pas occuper la seule T4 qu'un multiview cherche) |

Reste à toi : **la validation visuelle** de la qualité NVENC contre x264 à débit donné. Aucun de mes
chiffres ne dit quoi que ce soit là-dessus.

## 4. Ce qui reste ouvert

**La qualité — ta validation.** NVENC à 4 Mb/s ne rend pas la même image que x264 à 4 Mb/s. Sur du
contenu broadcast, l'écart se voit sur les dégradés, les mouvements rapides et les bas débits.
Aucun de mes chiffres ne dit quoi que ce soit là-dessus. Le banc préparera les deux sorties côte à
côte, le jugement est le tien — et il peut faire bouger le preset retenu.

**Le nombre de sessions simultanées.** Les cartes grand public plafonnent à 3-5 encodages ; les
Quadro et Tesla n'ont pas cette limite. P5000 et T4 sont bonnes, la K620 (Maxwell, 2 Go) est
ancienne et **n'a pas de HEVC** — à refléter dans l'allocation, sinon un `streamer` en h265 sur un
nœud K620 échouera à l'exécution.

---

## 5. Suite proposée

1. Les arbitrages du §3 sont pris ; reste ta validation visuelle.
2. J'applique — orchestrateur d'abord, plugin ensuite, sur une branche.
3. Banc : un `streamer` NVENC et un x264 **simultanés** sur dell-1, même source, mêmes paramètres,
   coût comparé par delta cgroup, avec un témoin. Puis ta validation visuelle sur la sortie.
4. Si c'est concluant : les deux T4 des R620 — qui tournent depuis le correctif AVX2 — deviennent
   les nœuds d'encodage naturels du parc.
