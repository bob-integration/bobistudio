# Versions d'un média — modèle de référence

Règle du 2026-10-16 : **toutes les essences d'un média sont sous une seule fiche.** Ce document fait
foi sur le modèle ; le code est `app/media_registry.py` (§ « Versions d'un média »), la règle de nom
`app/media_naming.py`, la garde `tests/verif_media_versions.py` (et `tests/verif_code_media_nom.py`
pour la concordance des noms).

## Modèle

| Objet | Où | Ce qu'il porte |
|---|---|---|
| Fiche (média) | `media_assets` | identité (UUID), nom, champs, vignette, projets, bins, partages — **et l'original** : empreinte, fiche technique, taille |
| Version (hors original) | `media_versions` (clé `asset_id`, `key`) | libellé, préréglage, traitement (`process_json`), fiche technique, empreinte, taille, producteur, origine |
| Copie | `media_files` | `asset_id` + `version` (`original` par défaut) : une copie est celle d'UNE version |
| Version par défaut | `media_assets.default_version` | NULL = l'original |

- L'original n'a pas de ligne dans `media_versions` : il est la fiche (une seule source pour la référence).
- Clé de version : minuscules et chiffres, une lettre d'abord, 16 caractères au plus ; jamais
  `original`, `proxy` ni une piste Avid (`v1`, `a12`) — `media_naming.valid_version`.
- Clé d'une sortie du transcodeur : nom court du préréglage (`VERSION_KEYS` de
  `plugins/transcoder/script.py` : `h264`, `h265`, `xdcam`, `avci100`, `proreshq`, `dnxhrhq`, `ffv1`,
  `wav`…), ou le paramètre `version` du job.
- **Version par défaut effective** : celle choisie si elle existe encore ; sinon l'original ; sauf une
  fiche née d'une version seule (original jamais vu sur un volume) : sa première version.
- Champs, partages, bins, projets : **au niveau de la fiche** (toutes versions). Pas de champ par version.
- Les variantes de l'**éditeur d'image** ne sont pas des versions : un autre contenu, une autre fiche
  (`derived_from`, relation `edit`).
- **Sortie du transcodeur : version OU nouvelle fiche** (décision du 2026-10-29, qui amende la règle du
  16/10). Paramètre `as` du job : `version` (défaut du conteneur — macros du transcoder, dossiers de
  dépôt, « Transcoder au format du player ») range la sortie sous la fiche de sa source ; `asset`
  (défaut de la fenêtre « Transcoder… » et de la macro de la ferme) en fait une **fiche à part**,
  reliée à son original (`derived_from` dans le sidecar), d'identité STABLE
  `media_naming.derived_id(source, clé)` — un job refait remplace sa fiche au lieu d'en créer une
  autre. Nom : `<base>_<clé>__<code de la nouvelle fiche>.<ext>` (`media_naming.derived_file_name`).
- **Bin de destination** (fenêtre « Transcoder… ») : la fiche produite (la nouvelle, ou celle de la
  source pour une version) est rangée dans le bin choisi quand le job est terminé ET le fichier indexé
  (`transcode_farm._place_in_bins`) ; le dossier sur le disque n'est qu'un réglage avancé.

## Nom de fichier

`<base>__<code>.<ext>` pour l'original, `<base>__<code>_<clé>.<ext>` pour une version : même code
(6 caractères hexadécimaux de l'identité, 8 si un autre média occupe le nom), la longueur du code de
la source est gardée. Proxy : `.proxies/<base>__<code>_proxy.mp4`. À la sortie du nœud (export,
téléchargement) code ET clé de version sont retirés (`export_name`). Implémentations qui doivent
concorder : `app/media_naming.py`, `plugins/recorder/script.py`, `plugins/transcoder/script.py`,
et les expressions d'affichage (`plugins/transcoder/control.js`, `plugins/player/control.js`,
`plugins/player/script.py`, `static/js/media_upload.js`).

## Identification d'un contenu

`find_by_fingerprint(fp)` → l'original d'un média, sinon une de ses versions. C'est la règle de
l'analyse (`scan_node`), de l'envoi depuis le poste (`media_upload`) et de l'import d'un partage : un
contenu identique à une version est une copie de plus de **cette** version (nom codé avec sa clé,
sidecar tamponné avec l'identité et la version).

## Sidecar

`<media id="<identité de la fiche>" version="<clé>" …>` ; sans `version` = l'original. Le transcodeur
écrit l'identité du média **source** (posée par l'orchestrateur, ou lue dans le sidecar de l'entrée).
Source sans identité (dossier de dépôt, partage) : identité stable tirée de l'empreinte de l'entrée
(`uuid5("bobi-media-fp:" + empreinte)`) ; à l'analyse, la base range la sortie sous le média de ce
contenu s'il est connu (et remplace l'identité orpheline dans le sidecar : `stamp … replace`),
sinon la fiche naît de la version avec l'empreinte et la fiche technique de la source — la source
analysée plus tard en devient l'original.

## Job refait à l'identique

Même source, même clé de version → même nom de fichier : le transcodeur remplace SON fichier (même
identité et même version dans le sidecar), la ferme aussi au rapatriement. Contenu changé : la
version prend la nouvelle empreinte, sa fiche technique et son traitement.

## Chargement, API, macros

- Charger un média (player, images fixes, rapatriement, envoi, copie) = sa version par défaut ;
  `version` explicite : `POST /api/media/send`, `/api/media/fetch`, `/api/media/copy`. Une clé que le
  média n'a pas est refusée en le disant.
- `POST /api/media/assets/<id>/default_version {version}` ; `DELETE /api/media/assets/<id>/versions/<clé>`
  (copies sur les nœuds effacées ; jamais l'original).
- Macros (fournisseur `media_registry`) : `send_media` (conteneur, média, version, geste ; rapatrié
  d'abord au besoin, puis chargé), `set_default_version`.
- Recherche : `versions` (nombre), `version` (clés) ; les champs de traitement (préréglage,
  loudness, demandeur…) valent ceux de la version la plus récente qui les porte.

## Projet et archive

Les versions voyagent avec la fiche (`project_export_media` : `versions`, `default_version`).
L'archive embarque une copie par version, la version sous `media/<id>/@<clé>/…`, réimportée sous son
nom codé.
