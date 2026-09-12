# Textes de licence des composants tiers

Ce dossier existe pour une raison précise : **Apache-2.0 §4(a) et BSD-3-Clause exigent qu'une
copie de la licence accompagne toute redistribution**, source comme binaire. Les citer dans
`THIRD-PARTY-NOTICES.md` ne suffit pas, il faut les fournir.

| Fichier | Composant | Où il est redistribué |
|---|---|---|
| `Apache-2.0.txt` | **MXL SDK** — <https://github.com/dmf-mxl/mxl> | `libmxl`, `libmxl-common`, `libmxl-fabrics` compilés dans les images `bobi-compute` et `bobi-media` |
| `BSD-3-Clause-MTL.txt` | **Intel Media Transport Library** — *Copyright (c) 2022, Intel Corporation* | image `bobi-mtl` (moteur `2110_io`) |

Les textes sont repris **tels quels depuis les dépôts amont**, sans reformatage : une licence
retouchée n'est plus la licence.

> Le dépôt MXL amont ne contient **pas** de fichier `NOTICE` (vérifié le 2026-08-30). L'obligation
> d'Apache-2.0 §4(d) de reproduire un `NOTICE` est donc sans objet ici. Si le projet en ajoute un,
> il devra être copié dans ce dossier.

Les licences des autres composants (FFmpeg, GStreamer, CuPy, NumPy…) sont fournies par leurs
propres paquets Debian ou Python dans les images, à leur emplacement habituel.
