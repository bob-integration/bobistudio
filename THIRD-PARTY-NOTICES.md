# Composants tiers

Bobi.Studio est distribué sous **GPL-3.0-or-later** (cf. [`LICENSE`](LICENSE)). Il s'appuie
sur les composants tiers listés ci-dessous, qui restent soumis à **leur propre licence** et
au copyright de leurs auteurs respectifs.

Ce fichier est une aide à la conformité, pas un avis juridique.

---

## Composants intégrés aux images d'exécution

Compilés ou installés dans les images Docker distribuées avec le produit.

| Composant | Version épinglée | Rôle | Licence | Source |
|---|---|---|---|---|
| **MXL SDK** (`libmxl`) | `v1.1.0-rc1` | Bus vidéo/audio en mémoire partagée — transport interne | **Apache-2.0** | <https://github.com/dmf-mxl/mxl> |
| **Intel Media Transport Library** (`libmtl`) | `32b1b4e9` | Émission/réception ST 2110 en kernel-bypass | **BSD-3-Clause** | <https://github.com/OpenVisualCloud/Media-Transport-Library> |
| **DPDK** | via MTL | Pilotes réseau en espace utilisateur | BSD-3-Clause | <https://www.dpdk.org/> |
| **FFmpeg** | `7:7.1.5` (Debian trixie) | Encodage, décodage, conversion (processus séparé) | **GPL-2.0+** — voir ci-dessous | <https://ffmpeg.org/> |
| **GStreamer** | `1.26.2` / `1.26.3` (Debian trixie) | Pipelines média (player, recorder) — jeux `base`, `good`, `bad`, `ugly`, `libav` | **LGPL-2.0+** pour l'essentiel — voir ci-dessous | <https://gstreamer.freedesktop.org/> |
| **x264** | `0.164.3108` (Debian trixie) | Encodage H.264 | **GPL-2.0+**, *Copyright 2003-2022 x264 project* | <https://www.videolan.org/developers/x264.html> |
| **x265** | `4.1-2` (Debian trixie) | Encodage H.265 / HEVC | **GPL-2.0+** | <https://www.videolan.org/developers/x265.html> |
| **libsrt** | paquet Debian | Transport SRT | **MPL-2.0** | <https://github.com/Haivision/srt> |
| **CuPy** (`cupy-cuda12x`) | `13.4.1` | Calcul GPU (image `_compute_gpu_runtime`) | **MIT** — ⚠️ voir ci-dessous | <https://github.com/cupy/cupy> |
| **NumPy** | via pip | Calcul numérique dans les scripts de plugin | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | <https://numpy.org/> |

### Mentions à conserver

**Intel Media Transport Library** — BSD-3-Clause, *Copyright (c) 2022, Intel Corporation.*
Cette licence impose de reproduire la mention de copyright, la liste des conditions et
l'avertissement de garantie dans toute redistribution, **source comme binaire**. Le texte
intégral est fourni dans `licenses/BSD-3-Clause-MTL.txt`.

**MXL SDK** — Apache-2.0, *Copyright 2025 Contributors to the Media eXchange Layer project*
(initiative Dynamic Media Facility de l'EBU et de la NABA, <https://github.com/dmf-mxl/mxl>).
Le dépôt suit la convention REUSE et contient également des éléments sous MIT et CC-BY-4.0.

⚠️ **Sources modifiées.** Apache-2.0 §4(b) impose de signaler de façon bien visible les fichiers
que nous modifions. Les images runtime compilent `libmxl` depuis les sources amont **après
application de nos correctifs**, qui sont versionnés et lisibles dans
`plugins/_compute_runtime/patches/` :

| Correctif | Ce qu'il change |
|---|---|
| `mxl-planar-type.patch` | type d'essence planar |
| `mxl-planar-slices.patch` | lecture/écriture par tranches sur le planar |
| `mxl-fabrics-slice-reset.patch` | remise à zéro de tranche dans `libmxl-fabrics` |

Aucun autre fichier du SDK n'est modifié. Le texte intégral de la licence est fourni dans
`licenses/Apache-2.0.txt`, comme l'exige §4(a).

> Le dépôt amont ne contient pas de fichier `NOTICE` (vérifié le 2026-08-30) : l'obligation
> §4(d) est donc sans objet. À reprendre s'il en ajoute un.

### FFmpeg est en GPL, pas en LGPL

FFmpeg est LGPL-2.1+ à la base, mais le paquet Debian utilisé par les images est construit avec
`--enable-gpl --enable-libx264 --enable-libx265`. Il incorpore donc x264 et x265, tous deux en
GPL-2.0+ : **les binaires qui en résultent sont sous GPL-2.0+**, et non LGPL. C'est compatible
avec la GPL-3.0-or-later de Bobi.Studio, et sans effet sur le reste du produit puisque FFmpeg
est invoqué comme **processus séparé**, jamais lié à notre code.

À noter : le paquet est lié à **GnuTLS** et non à OpenSSL, ce qui évite l'incompatibilité
historique entre la licence OpenSSL et la GPL.

### GStreamer : LGPL pour l'essentiel, et un point de vigilance qui n'est pas la licence

L'image média installe `gstreamer1.0-plugins-{base,good,bad,ugly}` et `gstreamer1.0-libav`.
Le relevé des fichiers de copyright Debian donne, en nombre de déclarations :

| Jeu | Composition |
|---|---|
| `base` (1.26.2) | LGPL-2.0+ quasi exclusivement, plus BSD-2/3-clause et domaine public |
| `good` (1.26.2) | LGPL-2.0+ / LGPL-2.1+, 3 fichiers GPL-2.0+ |
| `bad` (1.26.2) | LGPL-2.0+ majoritaire, 8 fichiers GPL-2.0+ et 5 GPL-3.0+ |
| `ugly` (1.26.3) | LGPL-2.0+ majoritaire, 4 fichiers GPL-2.0+ |
| `libav` (1.26.2) | LGPL-2.0+ / LGPL-2.1+ |

Les fragments sous GPL sont **marginaux et périphériques** : scripts de construction
(`ltmain.sh`, outils de couverture), exemples de test, et quelques éléments de niche
(visualiseur *synaesthesia*, client VNC `librfb`, accès conditionnel DVB, `tta`, `mpegpsmux`).
Tous sont en GPL-2.0+ ou GPL-3.0+, donc compatibles avec la GPL-3.0-or-later.

**Le vrai point de vigilance n'est pas la licence : ce sont les brevets.** Debian décrit le jeu
`ugly` comme « des greffons de bonne qualité qui pourraient poser des problèmes de
distribution » — il s'agit de codecs couverts par des brevets logiciels dans certaines
juridictions, indépendamment de leur licence libre. Cela ne concerne pas le droit d'auteur et
n'a aucun effet sur la GPL de Bobi.Studio, mais c'est à évaluer avant toute **distribution
publique d'images binaires**, en particulier hors d'Europe. Le jeu `ugly` peut être retiré de
l'image média s'il n'est pas requis par les pipelines réellement utilisés.

### ⚠️ `libmtl` et DPDK sont modifiés

L'image du moteur ST 2110 applique des **correctifs** à l'Intel Media Transport Library et à
DPDK avant compilation ; ces correctifs sont versionnés dans ce dépôt. La redistribution de
code BSD-3-Clause modifié reste soumise aux obligations de mention ci-dessus.

### ⚠️ CuPy et les bibliothèques CUDA

CuPy lui-même est sous licence MIT, mais les roues `cupy-cuda12x` embarquent des bibliothèques
**CUDA de NVIDIA**, soumises à l'accord de licence propriétaire de NVIDIA et **non** à une
licence libre. L'image GPU ne doit donc pas être redistribuée publiquement sans vérifier les
conditions de redistribution de NVIDIA. Voir <https://docs.nvidia.com/cuda/eula/>.

---

## Composants téléchargés à l'exécution

Non redistribués avec le produit : récupérés depuis leur source officielle au premier lancement.

| Composant | Rôle | Licence | Source |
|---|---|---|---|
| **MediaMTX** | Passerelle WebRTC / RTSP (ingest et lecture WHEP) | **MIT** | <https://github.com/bluenviron/mediamtx> |

---

## Outils de construction

Utilisés pour produire les images ; non embarqués dans le produit distribué.

| Composant | Licence | Source |
|---|---|---|
| **vcpkg** | **MIT** | <https://github.com/microsoft/vcpkg> |

---

## Dépendances Python du contrôleur

⚠️ **Ces paquets sont redistribués.** `app/offline_bundle.py` télécharge les roues de
`requirements.txt` dans `vendor/wheels/` pour permettre l'installation sans accès Internet :
le bundle hors-ligne **contient** ces bibliothèques, ainsi que leurs dépendances transitives
(`cffi`, `pycparser`…). Les licences permissives ci-dessous imposent de conserver les mentions
de copyright et le texte de licence — ils sont présents dans les métadonnées de chaque roue
(`*.dist-info/`), qu'il ne faut donc pas dépouiller.

Licences relevées sur les versions effectivement installées :

| Paquet | Version | Licence |
|---|---|---|
| blinker | 1.9.0 | MIT |
| certifi | 2026.5.20 | MPL-2.0 |
| cffi | 2.0.0 | MIT |
| charset-normalizer | 3.4.7 | MIT |
| click | 8.4.1 | BSD-3-Clause |
| cryptography | 49.0.0 | Apache-2.0 OR BSD-3-Clause |
| Flask | 3.1.3 | BSD-3-Clause |
| idna | 3.16 | BSD-3-Clause |
| ifaddr | 0.2.0 | MIT |
| itsdangerous | 2.2.0 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD-3-Clause |
| Markdown | 3.10.2 | BSD-3-Clause |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| Pillow | 12.2.0 | MIT-CMU |
| pycparser | 3.0 | BSD-3-Clause |
| requests | 2.34.2 | Apache-2.0 |
| urllib3 | 2.7.0 | MIT |
| waitress | 3.0.2 | ZPL-2.1 |
| Werkzeug | 3.1.8 | BSD-3-Clause |
| **zeroconf** | 0.149.16 | **LGPL-2.1-or-later** |

`zeroconf` est la seule dépendance Python sous copyleft. Compatible avec la GPL-3.0, mais si
elle venait à être liée statiquement ou modifiée, les obligations de la LGPL s'appliqueraient
séparément.

---

## Spécifications et modèles AMWA NMOS

L'implémentation NMOS s'appuie sur les spécifications **AMWA** (IS-04, IS-05, IS-12, MS-05-02,
BCP-002, BCP-008), publiées par l'Advanced Media Workflow Association sous **Apache-2.0**.
Les modèles de données vendorisés dans `services/nmos/is12_models/` conservent leurs mentions
de copyright d'origine. Voir <https://specs.amwa.tv/nmos/>.

---

## Micrologiciel redistribué dans le dépôt

Contrairement à tout ce qui précède, ce fichier est **versionné dans le dépôt lui-même**
(`node_agent/firmware/ice/`), pas construit dans une image ni téléchargé à l'exécution.

| Composant | Version | Rôle | Licence |
|---|---|---|---|
| **Intel Ethernet 800 Series DDP** (`ice_comms-1.3.63.0.pkg`) | `1.3.63.0` | Profil *Dynamic Device Personalization* chargé par le pilote noyau `ice` dans une NIC Intel E810. Sans lui, la carte démarre en **Safe Mode** : pas d'horodatage PTP matériel, pas de *steering* des flux ST 2110. | **Licence propriétaire Intel** — voir ci-dessous |

### ⚠️ Ce fichier n'est PAS sous GPL-3.0

Il est couvert par sa **propre licence**, reproduite intégralement dans
`licenses/Intel-DDP-800-series.txt` et à côté du fichier lui-même
(`node_agent/firmware/ice/Intel_800_series_market_segment_DDP_license.txt`).

*Copyright (c) 2020 Intel Corporation.*

Elle autorise la redistribution **sous forme binaire, sans modification**, à la seule condition
d'être utilisée **avec des produits Intel** — ce qui est le cas ici : le profil ne se charge que
dans une NIC de la série 800. Elle exige en contrepartie que la mention de copyright et
l'avertissement de garantie soient reproduits dans la documentation accompagnant la distribution
— c'est l'objet de cette section. Elle **interdit** la rétro-ingénierie, la décompilation et le
désassemblage, et ne concède **aucun droit sur le matériel**.

Le fichier est distribué **tel qu'Intel le publie** : il n'a jamais été modifié.

**Pourquoi c'est licite dans un dépôt GPL-3.0** : il s'agit d'une *agrégation simple* au sens de
l'article 5 de la GPL. Ce micrologiciel n'est lié à aucun de nos programmes ; il est transmis
tel quel au matériel par le pilote `ice` du noyau. C'est la même construction que celle retenue
par Debian (`non-free-firmware`) et par `linux-firmware`. En revanche, il ne pourrait pas être
*intégré* à un programme sous GPL — d'où cette section séparée plutôt qu'une ligne dans le
tableau du haut.

**AVERTISSEMENT DE GARANTIE.** Ce logiciel est fourni par les détenteurs du copyright et les
contributeurs « EN L'ÉTAT », et toute garantie expresse ou implicite, y compris, sans s'y
limiter, les garanties implicites de qualité marchande et d'adéquation à un usage particulier,
est exclue. En aucun cas le détenteur du copyright ou les contributeurs ne sauraient être tenus
responsables d'un dommage direct, indirect, accessoire, spécial, exemplaire ou consécutif
(y compris, sans s'y limiter, la fourniture de biens ou services de substitution, la perte
d'usage, de données ou de profits, ou l'interruption d'activité), quelle qu'en soit la cause.
*(Le texte anglais faisant foi figure dans `licenses/Intel-DDP-800-series.txt`.)*

## Compatibilité des licences

Toutes les licences libres listées ci-dessus (Apache-2.0, BSD, MIT, MPL-2.0, LGPL, ZPL,
GPL-2.0+) sont compatibles avec une distribution sous **GPL-3.0-or-later**.

Apache-2.0 — la licence du SDK MXL et des spécifications AMWA, c'est-à-dire le cœur du
produit — est compatible avec la GPLv3 mais **incompatible avec la GPLv2 seule**. Le choix de
la version 3 n'est donc pas indifférent : il ne peut pas être rétrogradé sans revoir
entièrement cet inventaire.

Deux réserves, toutes deux hors du champ de la GPL et signalées comme telles :

- les **bibliothèques CUDA de NVIDIA** dans l'image GPU (cf. plus haut), qui ne sont pas libres ;
- le **micrologiciel DDP d'Intel** versionné dans le dépôt (section précédente), sous licence
  propriétaire, présent par *agrégation simple* au sens de l'article 5 de la GPL.

Aucun des deux n'est lié à nos programmes, et aucun ne peut être relicencié sous GPL.

## Signaler un oubli

Un composant manquant ou une licence mal attribuée dans ce fichier est un bug. Merci d'ouvrir
une issue.
