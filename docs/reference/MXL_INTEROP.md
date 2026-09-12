# Interopérabilité MXL inter-éditeurs — audit de conformité

Objectif : échanger des grains dans un **même serveur** (même domaine `/dev/shm/mxl`) avec des
containers d'AUTRES éditeurs, via le SDK MXL (EBU DMF / Linux Foundation) → respecter le **SDK
stock** à la lettre, pas nos extensions. Cf. mémoire `mxl-interop-other-vendors-goal`.

Audit 2026-07-07 contre **dmf-mxl/mxl `v1.1.0-beta-1`** — le tag alors buildé. **Depuis le
2026-08-13, `MXL_REF = v1.1.0-rc1`** ; les conclusions d'interop ci-dessous sont inchangées (rc1
ne touche ni au type vidéo, ni au parseur ANC, ni au format audio), seul l'outillage de banc a été
re-tagué `bobi-mxl-stock:1.1-rc1`.

> ## ⚠ CORRECTION MAJEURE — BANC CROISÉ EXÉCUTÉ 2026-07-12 (lire d'abord)
>
> Le banc croisé stock↔fork a **enfin été exécuté** (image `bobi-mxl-stock:1.1` = libmxl
> v1.1.0-beta-1 SANS nos patches + sonde C `stock_probe.c` ; producteur fork publiant un flow
> planar et son miroir v210 ; sur dl360-1). Il **INFIRME le verdict ci-dessous** sur un point
> central. Résultats mesurés :
>
> | Chemin stock | Sur `video/v210` (notre miroir) | Sur `video/x-mxl-planar` |
> |---|---|---|
> | **WRITER** (`mxlCreateFlowWriter`) | OK | **REJET** — `flow.cpp:244 Unsupported video media_type` |
> | **READER** (`mxlCreateFlowReader` + `GetGrain`) | **OK** — grain 5 529 600 o lu | **OK (!)** — grain 8 294 400 o lu |
>
> 1. **Le miroir v210 EST lu par un SDK stock, données comprises** → l'interop vidéo par le pont
>    R1 est **PROUVÉE**, pas seulement plausible. Bonus : le stock calcule lui-même les slices du
>    v210 (`sliceSizes[0]=5120` = une LIGNE avec padding, `totalSlices=1080`) → la « divergence de
>    sémantique slice » (ligne vs bande d'octets) **ne se pose pas sur le flow miroir** : elle est
>    portée par libmxl, correctement. Le point 4 de « À changer » est donc auto-satisfait.
> 2. **Le rejet du planar est côté WRITER, PAS côté READER.** L'affirmation « un container tiers
>    stock CRASHE au reader sur `x-mxl-planar` → flow illisible, 0 donnée » est **FAUSSE** : le
>    reader stock ne parse PAS le `media_type` (il mappe la struct binaire `mxlFlowInfo` de la shm,
>    agnostique au format) et ouvre notre flow planar sans broncher, payload accessible.
> 3. **Conséquence — le risque est PIRE que décrit, pas moindre** : exposer du planar à un tiers ne
>    produit pas un échec franc et bruyant, mais une **CORRUPTION SILENCIEUSE** (le tiers lit
>    8 294 400 octets planar et les décode comme du v210 → image en bouillie, aucune erreur). Même
>    classe de défaut que l'ANC maison. La règle « ne JAMAIS exposer `x-mxl-planar` comme point
>    d'interop » (point 5) reste donc valable — et devient plus impérative, puisqu'aucun garde-fou
>    du SDK ne nous rattrapera.
>
> **Volet AUDIO + ANC du banc (même jour, mêmes outils) :**
>
> | Essence | Consommateur stock | Résultat |
> |---|---|---|
> | **Audio** `audio/float32` 8 ch 48 kHz | `stock_probe` (`mxlFlowReaderGetSamples`) | **LU CORRECTEMENT** — 8 canaux, stride/fragments justes, échantillons sinus conformes → **l'audit avait RAISON, audio interopérable tel quel** |
> | **ANC** `video/smpte291` (notre format maison) | `mxl-data-probe` stock (parseur RFC 8331) | **`ANC count: 0 — No ANC elements`, exit 0** |
>
> **L'ANC est confirmé comme une PERTE SILENCIEUSE, pire qu'une corruption visible** : le tiers
> ouvre le flow, lit le grain (4096 o, slices OK), parse notre en-tête maison
> `[u32 meta_num][u32 udw_fill][meta×16][udw]` comme un en-tête RFC 8331 → en déduit
> « 0 élément ANC », et **conclut sans la moindre erreur que le flux ne porte AUCUNE donnée
> ANC**. Aucun signal d'alarme, aucun code d'erreur : le tally/timecode/sous-titres
> disparaissent purement et simplement chez le tiers. C'est le point 3 de « À changer » —
> il reste ENTIER et doit être traité avant d'annoncer l'ANC interopérable.
>
> Le verdict et la matrice ci-dessous sont conservés comme trace de l'audit statique 2026-07-07 ;
> lire les lignes « Type vidéo » et « Interop concrète (a) » à la lumière de cette correction.

## Verdict

**NON-INTEROPÉRABLE sur la vidéo ; CONFORME sur audio + mécanique (temps/index/slices/domaine).**
Point de rupture unique mais total : notre type vidéo **`video/x-mxl-planar` n'existe pas dans le
SDK stock** — reconnu SEULEMENT par notre fork patché (`mxl-planar-type.patch` +
`mxl-planar-slices.patch`, appliqués au build de nos images). Le stock n'accepte que `video/v210`
et `video/v210a` (`FlowParser.cpp:374` lève `Unsupported video media_type` sur tout le reste).
*(⚠ vrai à l'ÉCRITURE seulement — cf. correction du banc croisé en tête de doc.)*

**Fait structurant** : nos images embarquent un **fork** (stock + nos 2 patches) ; un éditeur tiers
embarque le **stock**. Deux `libmxl` différents montent le même tmpfs. La conformité se juge contre
le stock.

## Matrice de conformité

| Dimension | Verdict |
|---|---|
| **Type vidéo** | **DIVERGENT — bloquant** : on émet `video/x-mxl-planar` (fork-only) ; stock = v210/v210a uniquement |
| flowDef (champs, grouphint, grain_rate, components) | CONFORME |
| Modèle index/temps (GrainIndex = Timestamp/GrainDurationNs, TAI 2059-1) | CONFORME |
| grain_rate entrelacé (cadence trame, 1 grain = 1 champ) | CONFORME |
| ABI grain `mxlGrainInfo` (4096 o, version 2, totalSlices/validSlices) | CONFORME |
| Slices — mécanique (`validSlices/totalSlices`, `mxlFlowReaderGetGrainSlice`) | CONFORME |
| Slices — sémantique vidéo | **DIVERGENT (masqué)** : stock 1 slice = 1 LIGNE ; nous = bande d'octets |
| Audio `audio/float32` (per-canal, dé-entrelacé, ring) | CONFORME (byte-identique) |
| Data/ANC — déclaration (`video/smpte291`, 4096 o) | CONFORME |
| Data/ANC — contenu | **DIVERGENT (masqué)** : stock = RFC 8331 dès le champ Length ; nous = sérialisation maison `[meta_num|udw_fill|records|udw]` |
| Identité UUID (exposition) | CONFORME (uuid5 = UUID RFC valide) |
| Identité — NOTRE lecture | **DIVERGENT (unilatéral)** : nos readers prennent un NOM → `uuid5(name)` ; incapables de cibler un flowId tiers arbitraire |
| Domaine / layout `/dev/shm/mxl` | CONFORME (posé par libmxl, non customisé) |

## Interop concrète

**(a) Un container tiers (stock) LIT un de nos flows :**
- ~~**Vidéo → ÉCHEC DUR** : `mxlCreateFlowReader` du tiers parse notre flow_def, voit
  `x-mxl-planar` → exception, flow illisible, 0 donnée. **Risque n°1 prouvé.**~~
  **❌ INFIRMÉ AU BANC (2026-07-12)** : le reader stock n'ouvre PAS le flow_def, il mappe la
  struct binaire de la shm → il LIT notre flow planar (payload 8 294 400 o accessible). Le vrai
  risque est la **corruption silencieuse** (planar décodé comme v210), pas l'échec dur. Le rejet
  `Unsupported video media_type` n'intervient qu'à la **création d'un WRITER** (`flow.cpp:244`).
  Cf. l'encadré en tête de doc.
- **Audio → SUCCÈS** : layout per-canal float32 identique, lu correctement.
- **ANC → PIÈGE** : ouvre le flow mais interprète mal les octets (attend RFC 8331, reçoit notre
  sérialisation) → **corruption silencieuse**, pas de crash.

**(b) Nous LISONS un flow qu'un tiers (stock) expose :**
- **Vidéo v210 → possible avec 2 ajouts** : (1) décodeur v210→planar côté conso (~4-5 ms/hop en
  C SIMD, cf. « Mise à jour 2026-07-12 » ; le ~33 ms initial était un artefact numpy) ;
  (2) chemin reader **par-flowId brut** (nos readers dérivent l'id d'un nom local, un flow tiers a
  un UUID non reconstructible).
- **Audio → immédiat.**
- **Découverte → à bâtir** : pas de mécanisme d'énumération du domaine (`*.mxl-flow/flow_def.json`)
  ni de résolution NMOS IS-04 → flowId. Tout passe aujourd'hui par nos noms internes.

## Direction décidée (2026-07-07, utilisateur)

**R1 uniquement pour l'instant** : garder le planar en interne, exposer un flow v210 miroir aux
SEULES frontières inter-éditeurs. **R2 (upstream) EN PAUSE** — ne RIEN demander/soumettre à
l'extérieur (comité DMF-MXL, PR, contact tiers) sans feu vert explicite de l'utilisateur.
R3 (tout-v210) = repli futur si on a le GPU.

## Recommandations (type planar)

- **R1 (RETENUE) — planar en interne + flow `v210` MIROIR aux frontières.** Garder planar pour tous
  les hops internes (0 conversion) ; pont `v210↔planar` à la SEULE frontière inter-éditeurs
  (~4-5 ms/hop en C SIMD — quasi gratuit, cf. mise à jour 2026-07-12). Effort : encodeur/décodeur v210 +
  reader par-flowId + découverte par flow_def. Le tiers voit un flow miroir v210 (publier 2 flowId).
- **R2 (EN PAUSE — moyen terme) — upstreamer le type planar** (`video/raw` planar) à la spec
  DMF-MXL. Le patch est déjà écrit comme une PR propre. Si accepté, le pont v210 disparaît. **Ne pas
  soumettre / ne contacter aucun tiers sans feu vert explicite** (décision utilisateur 2026-07-07 :
  on attend avant de demander quoi que ce soit à l'extérieur). Ne PAS faire dépendre l'interop de R2.
- **R3 (option crédible depuis 2026-07-12, plus un simple repli) — tout le bus interne en v210** :
  le « ~33 ms/hop rédhibitoire » était un artefact numpy ; en C SIMD un étage v210 complet coûte
  ~4,9 ms (vs ~1,1 ms planar) et tient large le budget 50p. Rendu transparent via bobimxl (dé-pack
  v210→planar à la lecture, re-pack à l'écriture) → les plugins restent planar en RAM, ne changent
  pas. Restes contre : (1) planar garde ~3,3× d'avance CPU par étage sur des nœuds memory-bound ;
  (2) v210 (5,53 Mo/img 1080p) = **+33 % de trafic mémoire vs le planar8 réellement utilisé**
  (pipeline `force8`, ~4,15 Mo) — à mesurer au banc membw/multiview ; (3) v210 = 10 bits natif
  (gain qualité, mais invalide l'économie force8). GPU : v210 avantageux (moins de PCIe, cf.
  `pyramide-vs-gpu-pcie-measure`). R3 se décide APRÈS bancs réels, pas sur les micro-mesures.
- **R4 (rejetée) — garder planar seul + exiger que les tiers patchent leur libmxl** : c'est de la
  captivité, pas de l'interop. Contraire à l'objectif.

## À changer avant d'ouvrir l'interop

1. **Pont v210 aux frontières** (R1) — sans lui, zéro vidéo inter-éditeurs.
2. **Reader par-flowId brut** + **découverte par énumération `*.mxl-flow/flow_def.json`** (et/ou
   résolution NMOS IS-04 → flowId) — sans ça, aucun flow tiers ingérable.
3. **ANC RFC 8331 conforme** aux frontières — sinon corruption silencieuse ; ou ne pas annoncer
   l'ANC interopérable.
4. **Slices planar** : documenter que notre sémantique (bande d'octets) ≠ sémantique ligne du SDK ;
   sur le flow miroir v210, utiliser la sémantique ligne standard.
5. **Ne JAMAIS exposer `x-mxl-planar` comme point d'interop** — type interne fork-only.

## Banc croisé stock↔fork — FAIT (2026-07-12)

Outillage versionné : **`plugins/_mxl_stock_bench/`** (Dockerfile `bobi-mxl-stock:<MXL_REF>` =
libmxl stock SANS nos patches, couches identiques au runtime compute jusqu'au configure → cache
vcpkg réutilisé ; + `stock_probe.c`, consommateur vidéo écrit contre les headers stock). Le banc
de 2026-07-12 a tourné sur `1.1` (= beta-1) ; le Dockerfile build aujourd'hui `1.1-rc1`.
Producteur du banc : `script_templates/v210_e2e.py` (E2E interne) et le producteur double-flow du
banc croisé. Exécution : conteneurs jetables, flux préfixés, GC en sortie — rien de la prod touché.

**⚠ `mxl-info` n'est PAS un test d'interop valable** : il n'affiche que le descripteur binaire de
la shm et « lit » donc *n'importe quel* flow, y compris `x-mxl-planar`. Seule une vraie sonde
(`mxlCreateFlowReader`/`mxlCreateFlowWriter`) discrimine — d'où `stock_probe.c`.

Résultats : cf. l'encadré de correction en tête de doc. En bref — miroir v210 **lu par le stock,
données et slices comprises** (interop prouvée) ; planar **rejeté à l'écriture** mais **lu sans
erreur** (⇒ corruption silencieuse si on l'expose) ; **audio float32 lu correctement** (conforme) ;
**ANC maison → `ANC count: 0` chez le tiers** (perte silencieuse totale).

### Ce qui reste à traiter (mis à jour 2026-07-12)

| # | Item | État |
|---|---|---|
| 1 | Pont v210 aux frontières | ✅ **FAIT** — plugin `v210_bridge` (export + import), interop prouvée au banc |
| 2 | Reader par-flowId + découverte du domaine | ✅ **FAIT** — `Reader(by_id=)`, `discover_flows()` |
| 3 | **ANC RFC 8331** | ✅ **FAIT + CONTRE-PROUVÉ (2026-07-13)** — format maison ABANDONNÉ, RFC 8331 partout, déployé (moteurs 0.40.0) ; `mxl-data-probe` STOCK lit le flux ANC live du bus (`avsync_anc` : « ANC count: 1, ST 12-2 ATC, DID 0x60/SDID 0x60, DC 16 » sur grains consécutifs — contre « ANC count: 0 » sur l'ancien format). Seul producteur ANC live du parc, aucun producteur non migré résiduel. L'interop ANC peut être annoncée. Caveat : ne pas juger le CONTENU des UDW avec `mxl-data-probe` (bug upstream 2 bits, cf. plus bas) — comptage/typage/DC corrects, contenu validé par notre codec ≡ libmtl octet pour octet. |
| 4 | Sémantique slice du flow miroir | ✅ **SANS OBJET** — libmxl calcule les slices du v210 (ligne + padding) |
| 5 | Ne jamais exposer `x-mxl-planar` | ⚠ **IMPÉRATIF** — aucun garde-fou du SDK côté reader |

## ANC : le format maison est ABANDONNÉ (2026-07-12)

**Décision** : contrairement au planar — qui achète un gain CPU réel (×3 à ×17) sur des trames de
plusieurs mégaoctets, et dont le fork est donc *payé* — le format ANC maison n'achetait **rien** :
un grain ANC fait 4 Ko, le bit-packing coûte des microsecondes. Il ne nous coûtait que la
non-conformité, une perte silencieuse **bidirectionnelle** et un décodeur à maintenir. On ne
construit donc **pas** de pont ANC : le format **normatif est RFC 8331, partout, à l'intérieur
comme à l'extérieur**.

- **Codec** : `bobimxl.anc_pack_rfc8331` / `anc_unpack_rfc8331` (+ `anc_atc_encode/decode` pour
  le timecode) et son jumeau C dans `mtl_rx.c` (primitives libmtl `st40_set_udw` /
  `st40_add_parity_bits` / `st40_calc_checksum`).
- **Sans perte** : un UDW de 10 bits = 8 bits de données + parité + son complément ; nos octets
  « 8 bits utiles » (comme libmtl, qui vérifie puis jette la parité) suffisent — parité et
  checksum se **recalculent**.
- **Gain collatéral** : `stream_num` est enfin porté (l'ancien format sérialisait 8 champs de méta
  là où libmtl en expose 9 — il **perdait** le numéro de flux).
- **Migration sans flag day** : le flowDef porte `bobi_anc_format` (`rfc8331` | `bobi-v1`), champ
  non standard **ignoré par un SDK stock** (même vecteur que `slice_height`). Les consommateurs
  aiguillent seuls (`bobimxl.anc_unpack`), et le TX du moteur émet correctement une source encore
  legacy → **flotte mixte supportée**.
- **Chaîne de validation** : codec `bobimxl` **identique octet pour octet** à une référence écrite
  avec les primitives libmtl ; codec C du moteur **identique octet pour octet** à `bobimxl` ;
  round-trip et `stream_num` vérifiés.

### ⚠ Bug upstream constaté dans `mxl-data-probe` (non remonté)

Le RFC 8331, libmtl (`st40_set_udw(i + 3, …)` dans `second_hdr_chunk`) et le plugin GStreamer de
MXL (`rust/gst-mxl-rs/src/format/data.rs`) font tous démarrer le flux de mots de 10 bits des UDW
au **bit 62** (les 2 derniers bits du mot d'en-tête appartiennent au premier UDW). Or
`mxl-data-probe` construit son `UdwUnpacker` avec `_accumulatedBits{0}, _bitCount{0}` à partir
d'un lecteur déjà positionné au **bit 64** → il décale toute la charge utile de 2 bits et
**mésinterprète les UDW de n'importe quel producteur conforme, libmtl compris**. Il lit en
revanche correctement `DID`/`SDID`/`Line`/`Data_Count` (situés avant la césure). Constaté au banc
le 2026-07-12. **Non signalé en amont** (consigne : ne rien soumettre à l'extérieur sans feu vert).
Conséquence pratique : ne pas utiliser `mxl-data-probe` pour juger la conformité de nos UDW.

## Fiabilité

Prouvé (source des deux côtés) : rejet planar (`FlowParser.cpp:374`), ABI grain identique,
conformité audio/grouphint/index/domaine, contenu ANC maison (`mtl_rx.c:59-63`), patches fork-only
(`_compute_runtime/Dockerfile:68-72`). Supposé (non exécuté) : comportement runtime exact du rejet.
Coût de conversion : re-mesuré 2026-07-11 (section ci-dessous) ; le 33 ms/hop initial est caduc.
Banc croisé stock↔fork à faire.

## Mise à jour 2026-07-12 — re-mesure SIMD : le coût v210 était un artefact numpy

Re-mesure 2026-07-11 (même Xeon 6240R dl360-1, C `-O3 -march=native` AVX2/512, banc
`scratchpad/v210_simd.c`, étage 2-entrées dépaq A+B + blend + repaq, 1080p 4:2:2 10 b) :

| | numpy naïf | C SIMD |
|---|---|---|
| étage planar (0 conversion) | 1,86 ms | ~1,1 ms |
| étage v210 complet | ~34,5 ms | ~4,9 ms |
| ratio conv/planar | ~17× | ~3,3× |

Conséquences sur ce doc :
- Le « ~33 ms/hop » qui fondait « R3 rédhibitoire » et le coût du pont R1 était une **pénalité
  numpy** (passes mémoire intermédiaires), pas une propriété du format. En SIMD, v210 tient large
  le budget 50p (20 ms).
- **R1 devient quasi gratuit** (~4-5 ms au point d'échange) — l'argument « conversion chère mais
  limitée aux frontières » devient « conversion pas chère du tout ».
- **R3 monte de « repli futur GPU » à « option crédible »**, arbitrée par : avantage planar résiduel
  ~3,3×/étage, +33 % de trafic mémoire vs planar8 (`force8`) sur des nœuds memory-bound, et le fait
  que la conversion SIMD n'existe que si on l'écrit en C (nos plugins numpy paient ~17×).
- Caveat banc : scatter v210 « cost-représentatif » (memcpy contigu), pas l'entrelacé 4:2:2
  bit-exact — le convertisseur réel sera un peu plus cher (ordre de grandeur FFmpeg v210 : idem).

**Chemin recommandé (proposé 2026-07-12, à valider)** : (1) convertisseur v210↔planar SIMD écrit
UNE fois, centralisé dans `script_templates/bobimxl.py` (extension C) — plugins inchangés, planar
en RAM ; (2) s'en servir d'abord pour le pont de frontière R1 ; (3) rendre le format on-shm
choisissable par flow → migration progressive mesurée au banc (membw, capacité multiview, chaîne
profonde) avant toute décision tout-v210. Argumentaire chiffré : PDF `/home/bob/Planar_V210.pdf`
et mémoire `planar-v210-cpu-measure`.

## Mise à jour 2026-07-13 — banc slice RÉEL : R3 non viable en 10 bits CPU, R1 confirmé bon marché

Le TODO « recalculer après la phase tranches » est soldé : banc `script_templates/
v210_slice_bench.py` (nouveau, consigné au repo) sur le **vrai bus MXL** de dl360-1 (conteneur
jetable bobi-compute:0.13, cœurs 30-35, domaine isolé) — chaîne réelle `srcA+srcB → étage blend
par bande → sink`, commit progressif validSlices, futex `get_slice`, conversion par la **vraie
lib SIMD bit-exacte** (`libbobi_v210`). 1080p50, bandes de 36 lignes (30/trame, = prod), 20 s
par format, ~996 trames chacun :

| bus | Mo/trame | src (travail/trame p50) | étage p50/p99 | conso unpack/trame | latence chaîne par bande p50/p99 |
|---|---|---|---|---|---|
| **planar8** (prod) | 4,15 | 0,75 ms (memcpy) | **2,9 / 4,0 ms** | 0 | 0,53 / 0,70 ms |
| planar10 | 8,29 | 1,6 ms | 7,4 / 13,4 ms | 0 | 3,9 / 5,0 ms |
| v210 (pipeline 8 b) | 5,53 | 3,8 ms (pack SIMD) | 8,2 / 15,2 ms | 4,0 ms | **0,34** / 2,4 ms |
| v210 (pipeline 10 b) | 5,53 | 4,0 ms | **15,4 / 23,8 ms** | 5,6 ms | 0,63 / **173 ms** (décroche) |

Lectures :
- **R3 (tout-v210) en 10 bits CPU : non viable en l'état** — un SEUL étage 2-entrées v210-10 b
  frôle le budget en p50 (15,4 ms) et le **dépasse en p99** (23,8 ms > 20 ms) : la chaîne décroche
  (latences en centaines de ms, trames perdues), alors que le travail utile du banc est minuscule
  (1 blend). En 8 bits, l'étage tient (8,2/15,2 ms) mais coûte **~2,8× planar8** ; la chaîne
  complète (2 src + étage + 1 conso) ≈ **4,5×** le CPU de planar8 (~20 ms vs ~4,4 ms cumulés).
- **La granularité ligne du v210 stock est un vrai atout latence** : commit par ligne → latence
  de chaîne par bande p50 *meilleure* que planar (0,34 vs 0,53 ms). L'argument latence ne
  départage plus les deux mondes (tous deux sous la milliseconde) — l'arbitrage est purement CPU.
- **Empreinte bus** : en 10 bits, v210 réduit le trafic mémoire de −33 % vs PLANAR10LE (5,53 vs
  8,29 Mo) — c'est le seul flanc où v210 gagne sur un nœud memory-bound, mais il le paie en CPU
  de conversion à chaque étage. En 8 bits (`force8` prod), planar reste plus compact (−25 %).
- **R1 (pont de frontière) confirmé** : pack producteur 3,8-4 ms/trame, unpack consommateur
  4-5,6 ms/trame — un hop de conversion au point d'échange tient très large, y compris en slice
  (le pont peut suivre la granularité ligne du v210 stock).
- Caveats : travail utile minuscule (un étage réel dilue le surcoût relatif) ; mono-thread par
  processus ; la latence planar10 élevée (3,9 ms) tient au déphasage des deux sources du banc,
  pas au format.

**Décision inchangée et renforcée : planar reste le bus interne** (en `force8` l'écart est ~3-5×
même avec la SIMD, et en 10 bits v210 casse le budget) ; v210 aux frontières via le pont
(`plugins/v210_bridge`). R3 ne redeviendrait d'actualité qu'avec un pipeline GPU (dé-paq bon
marché + PCIe réduit) ou des convertisseurs multi-threadés — à re-mesurer ce jour-là.

## Profondeur des ring buffers MXL — une DURÉE, pas un nombre de trames (2026-08-09)

Vérifié dans le SDK stock `v1.1.0-beta-1` (tag alors buildé ; `MXL_REF = v1.1.0-rc1` depuis le
2026-08-13, mécanique inchangée) : ce que le SDK **règle**
est une **durée**, en nanosecondes, par domaine. Ce que l'exploitant **observe et compte** sur le
disque (`ls <flux>.mxl-flow/grains | wc -l`) est un **nombre de grains** — la conversion se fait
par `grains = durée × cadence`, et ce nombre n'est PAS le même pour tous les flux du domaine.

| durée d'historique | grains à 50 fps | grains à 25 fps | grains audio (durée doublée, cf. plus bas) |
|---|---|---|---|
| **200 ms (défaut)** | **10** | **5** | équivalent 400 ms de buffer |
| 500 ms (exemple SDK) | 25 | 12,5 → 12* | équivalent 1 s de buffer |

\* `grainCount` est un entier (division entière, `Instance.cpp:239`) — une durée qui ne tombe pas
juste sur la cadence tronque le dernier grain partiel.

**Contre-sens à éviter, celui-là même qu'encourageait notre ancien paramètre `shm_video_ring`
(un COMPTE, par conteneur) : la profondeur en grains n'est pas une constante du système.** Deux
flux de cadences différentes dans le même domaine, réglé à la même durée d'historique, ont un
nombre de grains différent — 10 pour un flux 50p, 5 pour un flux 25p, avec la même fenêtre de
200 ms de rattrapage pour un lecteur en retard. Compter les grains sans connaître la cadence du
flux ne dit rien de la marge réelle disponible ; c'est la durée qui fait foi, le grain n'en est que
la traduction pour une cadence donnée.

- Option `urn:x-mxl:option:history_duration/v1.0`, décrite dans `docs/Configuration.md` du SDK :
  « Depth, in nanoseconds, of a ringbuffer », défaut **`200'000'000ns` (200 ms)**.
- Traduction en cases de ring buffer, côté vidéo/discret : `lib/internal/src/Instance.cpp:239`
  ```cpp
  auto const grainCount = _historyDuration * grainRate.numerator / (1'000'000'000ULL * grainRate.denominator);
  ```
  À 200 ms par défaut : **10 cases à 50 fps, 5 cases à 25 fps**. La constante posée par le SDK est
  le temps ; le nombre de cases en découle et varie avec la cadence du flux.
- Côté audio (flux continu), `Instance.cpp:266` divise par `500'000'000ULL` et non `1'000'000'000ULL` :
  ```cpp
  // The length is divided by 500M instead of 1B to effectively make it twice the
  // history duration, which is necessary, because only half of the buffer is
  // accessible for reading at any one point in time.
  auto const bufferLength = _historyDuration * sampleRate.numerator / (500'000'000ULL * sampleRate.denominator);
  ```
  L'audio reçoit donc le **double** de la durée nominale en taille de buffer — pas un traitement
  privilégié, une compensation du fait que la moitié seulement du tampon audio est lisible à un
  instant donné.
- Réglable **uniquement par domaine**, via un fichier `options.json` posé à la racine du domaine
  (chez nous : `/dev/shm/mxl/options.json` ; nom de fichier en dur dans
  `lib/internal/include/mxl-internal/PathUtils.hpp:17`, `DOMAIN_OPTIONS_FILE_NAME = "options.json"`).
  Exemple donné par le SDK pour porter la profondeur à 500 ms :
  ```json
  { "urn:x-mxl:option:history_duration/v1.0": 500000000 }
  ```
- L'option est **délibérément ignorée** si elle est passée au niveau instance (`mxlCreateInstance`).
  Commentaire du SDK, `Instance.cpp:417` (dans `Instance::parseOptions`) :
  > `// We are not considering MXL_HISTORY_DURATION_TAG here. we don't want per-instance history durations.`

  ⚠ **« par nœud » est un raccourci** : le SDK dit *par domaine*. Les deux coïncident chez
  nous parce qu'un nœud n'héberge qu'un domaine, celui par défaut — vérifié le 2026-08-09,
  aucun conteneur ne pose `MXL_DOMAIN` et l'orchestrateur ne l'injecte nulle part. Un
  conteneur pointant un autre domaine aurait son propre `options.json` (absent → 200 ms),
  invisible pour `mtl.ensure_mxl_history()` comme pour la sonde de `node_health`, toutes
  deux codées sur `/dev/shm/mxl`. Cesse de valoir dès qu'un second domaine apparaît.

  Sous cette réserve : tous les conteneurs qui partagent le domaine
  `/dev/shm/mxl` (tous les nôtres, plus tout tiers en interop) héritent de la même profondeur.
  Aucun de nos plugins ne peut la relever pour un seul flux sans l'imposer à tous les autres.
- Un changement du fichier `options.json` ne s'applique **qu'aux flux créés après** — les flux déjà
  ouverts gardent le `grainCount`/`bufferLength` calculé à leur création (`parseOptions` tourne
  une fois par instance, pas par flux). Reposer le fichier n'a d'effet qu'au prochain (re)déploiement
  des producteurs.
- `/dev/shm` est un tmpfs : `options.json` ne survit pas à un reboot du nœud et doit être reposé
  au boot si on s'en sert. **C'est fait** (2026-08-09) : `mtl.ensure_mxl_history()` écrit le
  fichier dans le tmpfs, en garde une copie persistante dans `/etc/bobi/mxl-options.json`, et pose
  l'unité `bobi-mxl-options.service` qui la recopie au boot **avant `docker.service`** — sans quoi
  les conteneurs créeraient leurs flux avant que la profondeur voulue soit lisible. La durée se
  règle dans Réglages → MXL (`mxl_history_ms`, en millisecondes) et `node_health` alerte à la
  transition si un nœud dérive de cette intention. Un nœud sans domaine `/dev/shm/mxl` n'est pas
  jugé : pas de domaine, pas d'intention.

**Notre `shm_video_ring` est un faux ami.** Ce paramètre historique par conteneur est un
**compte** de cases, pas une **durée**, et ne s'applique qu'à l'ancien shm maison pré-MXL — il n'a
jamais eu d'effet sur la profondeur d'un ring buffer MXL. Toute UI ou macro qui l'exposerait comme
un réglage de latence du bus MXL serait trompeuse.

### Pourquoi ça compte : la falaise, pas la rampe

Les 200 ms par défaut sont la fenêtre dont dispose un lecteur en retard avant de retomber sur une
case déjà recyclée par l'écrivain. **En-deçà de cette fenêtre, un retard se paie en latence** (le
lecteur lit une case plus vieille, l'image est en retard mais cohérente). **Au-delà, il ne se paie
plus en latence — il corrompt la ligne de temps** : le lecteur retombe sur des cases déjà
réécrites, potentiellement dans un ordre différent de celui où elles ont été produites.

Cas réel observé le 2026-08-09 : la réplication RDMA des flux d'un producteur en `slice_mode`
accusait 20 à 50 trames de retard (400 ms à 1 s à 50 fps), très au-delà des 200 ms par défaut
(10 cases). Symptôme : la tête du flux répliqué ne traînait pas simplement derrière la source,
elle **reculait** — index de grain observés en régression (pas mesurés : −8, −9, −18, −19 par
rapport à l'attendu) au lieu de rattraper ou de stagner. Condition d'invalidation : ce symptôme est
spécifique à un retard qui dépasse la profondeur configurée ; il disparaît si la profondeur du
domaine est augmentée au-delà du retard observé, ou si la cause du retard RDMA est traitée en amont.

Mesure de RAM le même jour (2026-08-09, un nœud du parc, à confirmer si la charge évolue) :
~333 Mo occupés par les flux MXL pour ~47 Go de tmpfs disponibles, soit environ 0,7 %. **La
profondeur du ring buffer n'est pas un levier d'économie mémoire chez nous** — la marge est
massive ; le seul risque réel d'une profondeur trop faible est la corruption de ligne de temps
ci-dessus, pas la RAM.

## Mise à jour 2026-08-15 — v210 aux frontières : ce qui reste ouvert n'est plus le CPU, c'est la CONFORMITÉ

La ligne du TODO « trancher v210 vs planar aux frontières » prêtait à confusion : **l'arbitrage
CPU est clos depuis le 2026-07-13** (section précédente, décision utilisateur) — planar reste le
bus interne, v210 aux frontières via `plugins/v210_bridge`. Ce qui restait réellement ouvert est
une question de conformité, posée par BCP-007-03 « NMOS With MXL » : cette BCP exige un
`media_type` **du registre AMWA**.

### État du registre, relevé le 2026-08-15 (clone frais de `AMWA-TV/nmos-parameter-registers`)

| Élément | État |
|---|---|
| `video/v210`, `video/v210a` | ✅ enregistrés |
| `audio/float32`, `video/smpte291` | ✅ enregistrés — nos flux audio et ANC sont conformes **d'origine** |
| `video/x-mxl-planar` | ❌ **hors registre** |
| `urn:x-nmos:transport:mxl` | ❌ toujours **absent** des transports (dash, mqtt, ndi, rtp, rtp.mcast, rtp.ucast, usb, websocket) |

Conséquence nette : le jour où BCP-007-03 sortira, **seuls nos flux vidéo seront non conformes**.

> **Mise à jour 2026-08-30.** BCP-007-03 est sortie (v1.0.0, 2026-08-18) et
> `urn:x-nmos:transport:mxl` est TOUJOURS absent du registre des transports — vérifié cette fois
> sur le README brut, la page publiée, le contenu du dossier `transports/` (un seul fichier, donc
> pas de JSON annexe), l'historique complet de ce fichier (13 commits depuis 2018, aucun MXL) et
> les PR/issues du dépôt (aucune ne mentionne MXL). Nuance importante : la BCP impose ce littéral
> elle-même (« MUST set the `transport` attribute to `urn:x-nmos:transport:mxl` »), là où elle
> DÉLÈGUE au registre pour `format` et `media_type` — la valeur est donc figée par une spec
> publiée. Le risque résiduel est le refus par un contrôleur strict, pas une valeur instable.
> Analyse complète et suites : `TODO.md` § BCP-007-03, « Réévaluation du 2026-08-30 ».

### Les trois options, et laquelle est retenue

| | Coût | Ce que ça achète | Remarque |
|---|---|---|---|
| **A. Ne rien faire** | nul | — | Coût réel **nul aujourd'hui** : la BCP n'a ni release ni transport enregistré. |
| **B. Miroir v210 à la frontière** | ~4-5 ms/trame **par flux miroité** (SIMD 8 b) + 5,53 Mo/trame de trafic bus | Conformité vidéo sur les seuls flux qu'on veut exposer | Le pont existe, est buildé et **validé E2E bit-exact en 8 et 10 bits** (`v210_bridge` 0.2.0). Rien à écrire. |
| **C. Faire enregistrer `x-mxl-planar`** | rédaction + soumission AMWA | Conformité sans conversion, pour tout le monde | **Bloqué** : R2/upstream en pause, consigne utilisateur de ne rien soumettre à l'extérieur. |

**Position retenue (utilisateur, 2026-08-15) : A maintenant, B au cas par cas.** Trois raisons :

1. Le coût de B est **par flux exposé**, pas structurel — rien n'oblige à miroiter les ~40 flux
   d'un nœud pour qu'un contrôleur tiers en lise deux.
2. Le pont est déjà prouvé : il n'y a pas de travail à anticiper, seulement un plugin à déployer.
3. Bâtir de la conformité maintenant, c'est se garantir une reprise — le transport n'étant pas au
   registre, les paramètres IS-05 bougeront encore.

**Le déclencheur qui rouvre le sujet est un BESOIN RÉEL** — un client, un contrôleur tiers à
interfacer — **pas la publication de la spec.** Et R3 (tout-v210 en interne) ne se rediscute
toujours qu'avec un pipeline GPU ou des convertisseurs multi-threadés, cf. section 2026-07-13.
