# Médias : une identité qui survit au déplacement

Chantier proposé le 2026-10-11, à la demande de l'exploitation : « on arrive à suivre un média qui
serait déplacé entre des serveurs, et qu'il garde sa vignette. Si on charge un projet, il faudra
bien qu'on conserve ses médias avec leurs métadonnées, même si on change de serveur. »

Statut : **décidé le 2026-10-11**, étape 1 en cours. Décisions en fin de document.

## Ce qui existe (relevé du 2026-10-11)

- Un média n'est connu que par **(nœud, chemin)**. Le déplacer, c'est en faire un autre ; rien ne
  relie la copie de B à l'original de A.
- Les médias vivent sur le disque **local** de chaque nœud (`nodes.media_mount`, défaut
  `/srv/mxl-media`). Aucun volume partagé entre nœuds ; les partages SMB/NFS (`services/storage`)
  sont montés en lecture seule et ne servent qu'à copier vers un nœud.
- `services/media_manager` liste les volumes à chaque fois (`find` par commande distante), lit les
  métadonnées dans les sidecars `.xml` (écrits par le recorder, sinon par ffprobe), copie entre
  nœuds (`ssh cat | ssh cat`, sans contrôle d'intégrité), renomme, gère des chutiers
  (`media_bins`, `media_bin_items` en (node_id, path)). Son `media_asset_cache` est un cache de
  métadonnées clé (node_id, path), sans identité. Ni vignette, ni suppression, ni déplacement.
- Les projets : `projects.media_path` (un sous-dossier par nœud, sans création explicite côté
  nœud). L'export `.bsproj.json` ne porte **aucune** référence aux médias — choix documenté dans
  `docs/reference/PROJETS.md`, que ce chantier remet en cause.
- L'agent de nœud n'a pas d'API fichiers : tout passe par `/v1/host/exec` (shell).
- Le player 0.35 stocke ses vignettes dans `.thumbs/` à la racine du volume (local au nœud) : trop
  local pour cet objectif, à ramener au rang de cache.

## Modèle

**Média (asset)** — l'identité, indépendante de l'emplacement :
`media_assets(id UUID, name, created_at, origin {recorder|upload|network|scan}, project_id NULL,
fingerprint, tech_json, thumb_t, thumb_custom, meta_json)`.

**Emplacement (instance)** — une copie concrète :
`media_files(node_id, path, size, mtime, fingerprint, tech_json, sidecar_id, asset_id NULL,
kind {main|proxy}, state {present|missing|copying|corrupt}, seen_at, verified_at)` — tout ce que
l'analyse a vu sur les volumes. `asset_id` renseigné = une copie du média ; NULL = fichier non
référencé (gardé pour ne pas recalculer son empreinte à chaque passage). Un média peut en avoir plusieurs ; le jour où il
y aura des proxys, ce seront des instances de `kind = proxy`, rattachées au même média.

**Champs personnalisés** — définis dans les Réglages :
`media_fields(key, label, type {text|longtext|number|date|enum|bool}, options, ordre)`. Les
valeurs vivent dans `media_assets.meta_json`. Titre, description et mots-clés sont des champs de
base, toujours présents.

**Vignette** — centrale, sur l'orchestrateur (`data/media_thumbs/<id>.jpg`, 480×270), choisie par
instant (`thumb_t`) ; elle est calculée sur le nœud qui porte une copie (ffmpeg via l'agent ou le
conteneur), puis stockée centralement. Le `.thumbs/` des volumes devient un cache.

## Ce qui entre dans la base (décision du 2026-10-11)

**Rien n'y entre sans un geste explicite.** Un fichier présent sur un volume n'est pas pour autant
un média de la base.

- **Partages SMB/NFS** : jamais indexés en entier. On y navigue et on choisit ce qu'on importe
  (copie sur le volume d'un nœud + enregistrement).
- **Volumes des nœuds** : l'analyse suit les médias de la base (copies présentes, absentes,
  déplacées). Un fichier inconnu est relevé comme « non référencé » par l'outil d'intégrité, avec
  l'action « importer » (et « tout importer » pour la reprise de l'existant).
- **Recorder** : ses clips entrent dans la base d'eux-mêmes, dès le début de l'enregistrement.
- **Transcoder** : des dossiers de dépôt configurables ; ce qui y est déposé est importé (et
  converti si on le demande).

## Reconnaître un média qui a bougé

1. **Identité dans le sidecar.** L'UUID est écrit dans le `.xml` du média (`<media id="…">`). Le
   sidecar voyage déjà avec le fichier (copies du Gestionnaire, recorder). C'est le cas courant.
2. **Empreinte du contenu**, pour un fichier déplacé hors du système ou sans sidecar : taille +
   hash (xxh64) des 4 premiers et 4 derniers Mo. Quelques millisecondes même sur 100 Go, stable
   tant que le contenu ne change pas.
3. *(Plus tard)* l'UMID / Material Package UID des MXF, que bmx écrit déjà : un 3e indice, utile
   avec des outils tiers (Avid, Media Composer).

Un fichier en cours d'écriture (enregistrement, copie) n'a pas d'empreinte définitive : on la
calcule à la fermeture.

## Réconciliation

Un passage par nœud (au démarrage, sur demande, puis périodique et léger — date + taille d'abord,
empreinte seulement pour ce qui a changé) :
- fichier inconnu → rattaché à un média existant (UUID du sidecar, sinon empreinte), sinon
  nouveau média ;
- copie attendue absente → `missing` (le média reste, avec ses métadonnées) ;
- même empreinte à deux endroits → deux instances du même média.

## L'écran « Infos » d'un média

Remplace le bouton « Vignette… » de la bibliothèque du player, et sert aussi dans le Gestionnaire
de Médias. Une fenêtre à onglets :

| Onglet | Contenu |
|---|---|
| Infos | nom, durée, vidéo (définition, cadence, balayage, codec, colorimétrie), audio (pistes, canaux), TC de départ, origine, dates |
| Métadonnées | champs de base + champs personnalisés des Réglages, éditables |
| Emplacements | chaque copie : nœud ou partage, chemin, état, dernière vérification ; actions copier / déplacer / vérifier ; proxy de lecture (état, recréer, supprimer) |
| Vignette | le choix actuel (curseur, image du player, défaut) |

## Projets

- `project_media(project_id, asset_id)` : le projet sait de quoi il a besoin. Le recorder d'un
  projet y range ses clips ; ajouter un média à une playlist d'un player du projet l'y ajoute.
- **Export** : les fiches des médias (identité, empreinte, métadonnées, vignette) — pas les
  fichiers, sauf option.
- **Import / ouverture sur un autre serveur** : chaque média est cherché par UUID puis empreinte
  sur les nœuds et partages. Absent → « hors ligne », avec « rapatrier depuis… » (autre nœud,
  partage).
- **Playlists du player** : elles référencent le média (`asset:<id>`) ; au chargement, le player
  prend la copie présente sur son nœud ou demande un rapatriement.

## Bins et recherche (demandé le 2026-10-11)

- **Bins** dans la base des médias (`media_bins`, `media_bin_items` par IDENTITÉ de média) :
  manuels (on y range), imbriqués ; par projet ou globaux.
- **Recherche sur n'importe quel champ** : nom, dates, origine, fiche technique (durée,
  définition, cadence, balayage, codec, pistes audio, conteneur, TC), métadonnées de base et
  champs personnalisés, disponibilité (copie locale, sur tel nœud, hors ligne), projet ; opérateurs
  contient / égal / comparaisons / vide / « dans les N dernières heures ou jours ».
- **Bin intelligent** = une recherche enregistrée, réévaluée à chaque affichage (« créés dans la
  dernière heure » reste juste).
- Les bins du Gestionnaire de Médias (clé nœud + chemin) seront migrés quand il passera sur la base.

## Copies et déplacements

Copie entre nœuds vérifiée (empreinte complète à l'arrivée), déplacement = copie vérifiée +
suppression de la source. Les deux mettent à jour les instances au lieu de créer un média. À
terme : une API fichiers dans l'agent de nœud (copie nœud à nœud directe, reprise), plutôt que le
relais par l'orchestrateur.

## Avancement (2026-10-11)

- Étapes 1, 2, 3 faites : registre, bibliothèque commune (Player, Images fixes, Recorder,
  Gestionnaire), import automatique des clips du recorder, copies vérifiées entre nœuds
  (empreinte comparée à l'arrivée ; date d'origine reposée, sinon la copie passe pour un fichier en
  cours d'écriture et n'est pas vérifiée), rapatriement pour un conteneur, envoi vers un conteneur,
  reprise des anciens bins du Gestionnaire.
- Copie entre nœuds DIRECTE (agent ≥ 0.24.0, 2026-10-11) : le nœud cible télécharge chez le nœud
  source sur un port dédié (9101, mTLS : nœuds et contrôleur, jamais un conteneur ; le port de
  pilotage 9100 reste réservé au contrôleur), avec une autorisation signée par l'orchestrateur
  (jeton du nœud source, un fichier, une taille, une échéance). curl à basse priorité (nice,
  ionice), plafond de débit (`media_copy_rate_mbps`, défaut 60 Mo/s), reprise d'un `.partial`.
  Réseau de CONTRÔLE (1 Gb/s ici : 78 Mo/s sans plafond, mesuré dl360-1 → dell-1). Le root-SSH
  par l'orchestrateur ne reste qu'en REPLI pour un nœud dont l'agent est plus ancien.
  Garde : tests/verif_transfert_noeuds.py (vrai agent, vraie CA, vrai curl ; mutations vérifiées).
- Rapatriement SUIVI (2026-10-12, `app/media_fetch.py`) : `POST /api/media/fetch {vmid, asset_id}`
  répond tout de suite `{job, state}` (copie déjà là : `state: done` + `path`/`local_path`) ;
  `GET /api/media/fetch/<job>` → `state` queued|copying|verifying|done|failed, `done_bytes`,
  `total_bytes`, `rate` (MESURÉ sur 12 s, pas le plafond réglé), `eta_s`, `error`, `path`. Tâche
  par (média, nœud, champ de vision du conteneur) : une seconde demande rejoint la tâche en cours ;
  2 copies à la fois, au-delà `queued` ; oubliée 10 min après la fin ; en mémoire seulement. Le
  rapatriement ne prend plus l'emplacement unique des opérations de la base (/api/media/scan) : il
  ne bloque plus une analyse et n'est plus refusé en 409 pendant une autre opération. Player
  ≥ 0.39.4 (ligne de playlist et « Suivant : ») et bibliothèque (bande de tâche) affichent
  l'avancement. Avancement par octets seulement en copie DIRECTE entre agents ; en repli root-SSH,
  « rapatriement… » sans pourcentage. Garde : tests/verif_media_fetch.py.
- Étape 4, projets (2026-10-11) :
  - APPARTENANCE par l'usage : un média chargé, rapatrié, importé d'un partage ou envoyé par un
    conteneur d'un projet entre dans ce projet ; la fiche Infos (onglet Emplacements) montre ses
    projets, on l'y ajoute ou l'en retire à la main.
  - EXPORT (`.bsproj.json`, schéma v3) : la fiche de chaque média du projet (identité, empreinte,
    métadonnées, vignette, filiation, traitement) et la définition des champs personnalisés
    UTILISÉS. Option « avec les médias » : archive `.bsproj.tar` produite EN FLUX — le projet en
    tête, puis une copie présente de chaque média et son sidecar, lus sur les nœuds par le port de
    transfert (le certificat du contrôleur y est accepté) ; rien n'est stocké sur l'orchestrateur.
  - IMPORT : les fiches entrent dans la base ; un média déjà connu (identité, sinon empreinte)
    garde sa fiche LOCALE, seuls les champs vides sont complétés. Sans copie : hors ligne, avec
    toute sa fiche ; ses copies sont retrouvées à l'analyse. Une archive dépose ses fichiers dans le
    dossier du NOUVEAU projet sur le nœud choisi (agent ≥ 0.25.0 : `/v1/host/transfer/upload` ;
    repli root-SSH sinon), jamais par-dessus un fichier existant (suffixe), puis analyse le nœud.
  - Essai réel : export avec médias lu sur dl360-1, import déposé sur dell-1, copies rattachées,
    empreintes vérifiées (puis tout retiré).
  - Pas fait : les playlists du player référencent encore un chemin, pas `asset:<id>`.
  - Filtre « projet » posé d'office dans la bibliothèque d'un conteneur de projet (2026-10-11,
    `/api/media/context?vmid=`) : visible en puce, retirable.
- Bibliothèque commune, retours de l'exploitation (2026-10-11) :
  - sélection au clic sur la LIGNE (Ctrl/Cmd, Maj, Ctrl+A) — plus de cases ; glisser une ligne
    sélectionnée emporte toute la sélection (`application/x-bobi-assets` / `-media-list`, JSON) ;
  - clic droit : menu de la ligne, de la sélection, et d'un BIN. Il n'avait jamais existé dans le
    composant commun (le navigateur ouvrait le sien) ;
  - colonne « Copies » = un NOMBRE ; « présent en local » = une marque ✓ (conteneur seulement) ;
  - bins pliables (état par poste), option « avec le contenu des sous-bins »
    (`bin_assets(children=True)`, bins intelligents compris) ;
  - « Copier tout le contenu » d'un bin : vers un nœud (Gestionnaire) ou en local (conteneur),
    copies vérifiées, avancement publié (`/api/media/bins/<id>/copy`, `progress` du job) ;
  - recherche avancée : une ligne [champ][opérateur][valeur] alignée, champs groupés, valeurs
    PROPOSÉES depuis la base (`/api/media/search_values?field=` : distinctes, comptées), ET/OU
    lisible, conditions actives en puces retirables ; nouveau champ « copie sur le partage » ;
  - renommer le FICHIER d'une copie (et son sidecar, dont `<file>`) sans toucher l'identité ni
    le nom du média ; refus si la cible existe ; sidecar brut consultable copie par copie ;
  - Gestionnaire : pleine hauteur, PLUSIEURS PANNEAUX (côte à côte / superposés, déplaçables,
    redimensionnables, disposition retenue par poste) ; dépôt dans la liste d'un panneau qui
    affiche un bin manuel = l'y ranger ; « Envoyer vers » en multiple : la playlist seulement.
  - Nettoyage : les anciens bins du Gestionnaire (routes `/api/media-manager/bins*`, tables
    `media_bins` / `media_bin_items` de la base PRINCIPALE) sont retirés ; tables supprimées une
    fois la reprise faite (`drop_legacy_bins`, au démarrage). Garde : tests/verif_media_bibliotheque.py.
- Étape 5 faite pour l'essentiel (import choisi depuis les partages ; sorties des dossiers de
  dépôt du Transcoder enregistrées avec filiation et traitement).
- PROXYS de lecture (2026-10-12) — instances `kind = proxy` du média, jamais un média à part
  (nom : `.proxies/<base>__<code>_proxy.mp4` depuis la règle du code, ci-dessous) :
  - format FIXE : MP4 H.264 High 4:2:0 8 bits, hauteur 540 (paire, jamais agrandie, largeur selon
    le rapport d'AFFICHAGE, pixels carrés), cadence d'origine (bwdif une image par trame si
    entrelacé), une image clé par seconde, `+faststart`, AAC 128k stéréo = les deux premiers canaux
    de la 1re piste (deux pistes mono : les deux premières). Audio seul : M4A. Image fixe : refusé ;
  - rangé à côté de l'original sur le nœud de la copie : `<dossier>/.proxies/<nom>_proxy.mp4`
    (suffixe si un autre média a le même nom de base). Le relevé ignore les dossiers cachés : le
    proxy est vérifié par SON chemin à chaque analyse (`proxies` du script de nœud) — absent →
    `missing`, autre taille → `corrupt`, génération interrompue sans tâche → ligne retirée. Toute
    requête qui compte, choisit ou envoie une copie filtre `kind='main'` (colonne Copies, « sur le
    nœud », intégrité, vignette, envoi, copie, rapatriement, export de projet, tampon du sidecar) ;
  - génération : par la FERME DE TRANSCODAGE (décision de l'exploitation, 2026-10-12 : « ça reste
    automatique, mais au moins on sait où c'est fait ») — un job de la ferme, priorité BASSE, hors
    projet (pris par un transcoder inscrit SANS projet, qui voit tout le volume), préréglage FIXE du
    transcoder (`playback_proxy`, `aac_128` pour l'audio seul, transcoder ≥ 0.10.0), sortie IMPOSÉE
    à l'emplacement du proxy (accès direct) ou rapatriée sous ce nom (lecture à distance, copie),
    écrasement permis, SANS sidecar. File, priorités, accès à la source, rapatriement : ceux de la
    ferme ; jobs simultanés et fils : ceux du transcoder. Fin du job → ligne `present`, taille
    relevée sur le nœud, « fait par » (transcoder, nœud, n° de job). Aucun transcoder inscrit : le
    job attend avec `phase = no_transcoder`, affiché dans la fiche (« en attente d'un transcoder »)
    et dans la ferme. Une première version générait dans un conteneur jetable de l'image média
    (mesuré : PGM 742 s en 403 s sur un cœur) — remplacée, sans trace dans le code ;
  - déclencheurs : menu d'une ligne / d'une sélection / d'un bin, fiche Infos (« Créer le proxy »,
    Emplacements : « Recréer », « Supprimer le proxy », avancement, « Annuler ») ; automatique
    (réglage `media_proxy_auto`, Réglages → Base des médias, ACTIVÉ par défaut depuis le 2026-10-12 — une valeur `0` explicite reste respectée) à l'import d'un
    média vidéo, à la fermeture d'un enregistrement, à l'arrivée d'une sortie du transcodeur ;
    critère de recherche « a un proxy » ;
  - lecture : `GET /api/media/assets/<id>/proxy` (connecté), `Range` respecté (206 + Content-Range
    exact, 416), lu par le port de transfert — l'agent ne connaît que le DÉBUT d'une plage
    (`bytes=a-`) : la fin est tenue par l'orchestrateur, qui coupe le flux. Repli root-SSH pour un
    agent ancien ;
  - copie vers un autre nœud : le proxy ne suit PAS ; supprimer la dernière copie principale d'un
    nœud supprime son proxy ; supprimer le média supprime ses proxys ;
  - macros : `media_registry.create_proxy` (identité ou nom exact, borné au projet) et états de la
    file (`MACRO_PROVIDER` de `app/media_registry.py`) ;
  - garde : tests/verif_media_proxy.py (génération RÉELLE par ffmpeg local, mutations vérifiées).
  - Essai réel (2026-10-12) : PGM 742 s (H.264 1080p25, 1,29 Go) sur dl360-1, fait par le
    transcoder de r620-2 (accès copie : source envoyée par l'hôte, sortie rapatriée) en 351 s
    d'encodage (~15 cœurs), 98 Mo, h264 High yuv420p 960×540 25 i/s + AAC stéréo, une clé par
    seconde ; le player-banc de dl360-1, qui lisait ce même fichier, n'a perdu aucune image
    (slots_missed 6 → 6, repeated 0). Lecture HTTP : Range exacts (empreinte md5 égale à la
    lecture sur le nœud), 416 hors fichier.
  - Limites connues : seul un transcoder SANS projet prend un proxy (les chemins de la base sont
    relatifs à la racine du volume) ; la copie d'un média vers un autre nœud n'emporte pas son proxy.
- PLANCHES D'IMAGES (2026-10-12, `app/media_storyboard.py`, transcoder ≥ 0.11.0) — pour le mode
  « Pellicule » de la bibliothèque (8 images par média) et, plus tard, le survol instantané :
  - n images (120 au plus, au plus une par image clé du proxy, donc une par seconde) réparties
    uniformément (centre de chaque part), en UNE grille JPEG de 10 cases de large, cases de 320 px
    (hauteur selon le rapport), `-q:v 5` ; seules les images clés sont décodées (`-skip_frame nokey`) ;
    expression `select` en arbre équilibré (une chaîne de 120 termes dépasse l'analyseur de ffmpeg) ;
  - faite par le TRANSCODER : à la fin d'un job de proxy vidéo de la ferme (`params.storyboard`,
    posé au moment de confier le job), sur le proxy tout juste écrit ; ou job `storyboard` seul,
    priorité basse, source = le proxy (rattrapage). Colonne `farm_jobs.storyboard_asset`. Un job de
    planche n'est confié qu'à un transcoder qui annonce `/state.storyboard` : sinon il ATTEND
    (`no_transcoder`, « en attente d'un transcoder »), jamais un échec ; un job de proxy fait par un
    transcoder antérieur (pas de planche dans `info`) déclenche une planche seule ;
  - écrite chez le transcoder sous `.farm/storyboards/job<n>.jpg`, RAPATRIÉE par l'orchestrateur
    par le contrôle :8082 du transcoder (`/farm/share` + `/farm/file/<jeton>`, taille vérifiée) puis
    effacée (`/farm/remove`) — pas par le port de transfert du nœud : le worker n'est pas toujours
    sur le nœud de la source (accès stream / copy), et c'est lui qui a le fichier. Rien sur le nœud ;
  - gardée CENTRALEMENT à côté des vignettes : `media_storyboards/<id>.jpg` + `<id>.json` (écrit
    après l'image, sa présence fait foi). `has_storyboard` sur chaque ligne de liste / recherche
    par UNE lecture du dossier (relue s'il change) — aucune requête par ligne ;
  - routes : `GET /api/media/assets/<id>/storyboard.json` (connecté) → `{n, cols, rows, w, h,
    times, url}`, 404 sans planche ; `GET …/storyboard.jpg` (ETag, 304, `max-age=300` : l'URL ne
    change pas quand la planche est refaite) ; `POST …/storyboard` (une planche) ; `GET|POST
    /api/media/storyboards` (avancement / « Préparer les planches manquantes ») ;
  - proxy recréé → planche refaite ; média supprimé → planche supprimée ; rattrapage automatique
    (au plus toutes les 10 min, dans l'analyse périodique) quand `media_proxy_auto` est actif ;
  - macros : `media_registry.create_storyboard` et état `storyboard_missing` ;
  - gardes : tests/verif_transcoder_storyboard.py (fabrication réelle par ffmpeg),
    tests/verif_media_storyboard.py (ferme, rapatriement, contrat, has_storyboard sans N+1,
    rattrapage, cascade) — mutées ;
  - mesuré (2026-10-12, transcoder 1137 sur r620-2, 23 fils) : proxy du PGM (742 s, 98 Mo) →
    120 cases 10×12, image 3200×2160, 1,07 Mo, 14,8 s (743 images clés) ; clip de 10 s → 10 cases
    sur une rangée, 3200×180, 28 Ko, 1,1 s.

## Nom de fichier : le code du média (décisions du 2026-10-12)

1. Un média se désigne par son **identité** partout (playlists, macros, bins, liens, préréglages) ;
   le nom n'est qu'un affichage. Le **nom du média** (fiche, libre, homonymes permis) est distinct du
   **nom de fichier** (technique).
2. **Tout fichier créé par le système sur le stockage d'un nœud** s'appelle `<base>__<code>.<ext>`,
   code = les 6 premiers caractères hexadécimaux de l'identité (8 si un AUTRE média occupe déjà ce
   nom dans le dossier). Même média → même code partout : une copie, un rapatriement gardent le nom.
   L'identité existe AVANT le nom de fichier. Module : `app/media_naming.py` (`coded_name`,
   `pick_name`, `strip_code`, `export_name`, `media_name`, `resolve_name`) ; le recorder et le
   transcodeur, qui tournent dans leur conteneur, en ont leur copie (parité vérifiée par la garde).
   - envoi depuis le poste : identité RÉSERVÉE à l'ouverture de la session (`media_uploads.asset_id`)
     — celle d'un contenu identique déjà connu (empreinte calculée par la page, transmise à
     `/upload/start`), sinon une nouvelle ; `import_files(..., want_ids=)` la reprend ;
   - recorder ≥ 0.27.0 : identité tirée à l'ouverture du segment, puis le nom, puis le sidecar ; Avid
     OP-Atom : `<clip>__<code>_v1.mxf`, `_aN.mxf`, le NOM DU CLIP (Media Composer) sans code ;
   - transcodeur ≥ 0.12.0 : toutes les sorties (opérations, dossiers de dépôt, ferme) ; le code d'une
     source ne se cumule pas ; les proxys de la ferme gardent le nom imposé par la base ;
   - variantes de l'éditeur d'image : `<base>_edit<N>__<code>.png` (N = rang de la variante) ;
   - copies entre nœuds et rapatriements : le nom codé de la source (ou codé s'il ne l'était pas) ;
     nom pris par le MÊME média → rien n'est recopié (vérifié) ; par un autre → code à 8 ;
   - copie depuis un partage (import) et import d'archive de projet : codé avec l'identité LOCALE ;
   - proxys : `.proxies/<base>__<code>_proxy.mp4` ; planches : centrales (`media_storyboards/<id>`).
   **Ne portent PAS de code** : les partages réseau (y compris un enregistrement écrit directement
   sur un partage — détecté par le type de système de fichiers, nfs/cifs/smb…, dans /proc/mounts du
   conteneur : le monteur ne voit pas le code), les fichiers déjà présents (aucun renommage
   rétroactif), les fichiers d'un volume importés tels quels (on ne renomme pas les fichiers de
   l'utilisateur). Renommer à la main le fichier d'une copie garde son code.
3. **À la sortie** du stockage du nœud, le code est RETIRÉ : archive d'export de projet (le sidecar
   garde l'identité), téléchargement par l'explorateur de fichiers (`services/files`, seulement pour
   une copie de la base portant CE code). Il n'existe pas de copie VERS un partage (les partages sont
   montés en lecture seule) : rien à faire tant qu'elle n'existe pas — `export_name` est prêt.
4. **Conflits de nom** : impossibles entre médias sur un nœud ; la fenêtre « nom déjà pris » de
   l'envoi ne s'affiche plus (`check_names` ne rend plus `exists`) — reste « ce contenu est déjà
   là ». La reprise d'un envoi interrompu est inchangée (session reconnue par nom voulu + taille + date).
5. **Nom tapé à la main** (macros, API, systèmes externes) : jamais refusé pour homonymie — d'abord
   le média du projet du conteneur, puis celui présent sur son nœud, puis le plus récent ; un nom de
   fichier codé désigne le média de ce code ; le choix est journalisé (alerte d'information
   `alert.media.name_ambiguous_*`, une fois l'heure par nom et choix). Une macro d'un projet reste
   bornée à ce projet. Dans l'interface : liste de choix « nom — projet ».
6. **Affichage** : le NOM DU MÉDIA (fiche) partout — bibliothèque, player (playlist, « Suivant : »,
   options de macro), recorder (fichier en cours, `/clips[].label`), transcodeur (liste des jobs),
   envoi ; le nom de fichier complet dans Infos → Emplacements et en infobulle.

Garde : tests/verif_code_media_nom.py (règle, parité recorder / transcodeur / JS, collisions,
Avid, stockage réseau, résolution par nom et journal, envoi, copies, archive, proxy, rien de
rétroactif, téléchargement) — mutée.

## Portée par projets (décision du 2026-10-12 : « Bibliothèque : il faut restreindre »)

Un utilisateur à portée **restreinte** (`auth.scoped_project_ids()` non `None` : rôle sans accès
global) ne voit et n'atteint que les médias rattachés (`project_media`) à l'un de **ses** projets.
Accès global (admin, operator) : rien ne change.

- **Un média sans projet est hors portée** d'un restreint (même règle que la ferme de transcodage).
- **Lecture hors portée → 404** (l'existence n'est pas confirmée) : fiche, vignette, image à
  l'instant, sidecar, proxy (flux), planche (`.json`/`.jpg`), proxys en file, éditeur d'image (le
  média ET son original). La fiche borne ses projets, sa source et ses dérivés à la portée.
- **Actions hors portée → 404** (projet ou bin d'un autre projet visé → 403) : métadonnées,
  vignette, suppression, oubli d'une copie, renommage de fichier, rattachement à un projet, proxy,
  planche, copie, rapatriement, envoi à un conteneur (par identité OU par chemin d'une copie : un
  envoi fait ENTRER le média dans le projet du conteneur, il ne doit pas servir à s'en emparer),
  rangement dans un bin, import d'un fichier qui est déjà un média hors portée.
- **Listes filtrées**, compteurs compris : liste, recherche, valeurs proposées de la recherche
  avancée, projets proposés, bins (manuels et intelligents, réévalués dans la portée), proxys en file.
  Le filtre d'un CONTENEUR (son projet) et celui de l'utilisateur se cumulent : aucun n'élargit
  l'autre.
- **Bins** : un bin d'un projet hors portée est invisible (avec ses sous-bins) ; un bin sans projet
  reste visible mais ne montre que les médias de la portée. Supprimer un bin dont un sous-bin est
  hors portée : 403.
- **Créer un média** : il doit entrer dans un projet de l'utilisateur, sinon il lui serait aussitôt
  invisible. Envoi depuis le poste : projet demandé, sinon celui du bin, du conteneur, ou son projet
  unique ; à défaut, 400 `project_required`. Import de fichiers d'un volume : même règle. Import
  depuis un partage : pour un conteneur rattaché à un projet seulement. Recorder, transcodeur, ferme :
  projet du conteneur producteur. Éditeur d'image : projets de l'original.
- **Global par nature, réservé à l'accès global même avec `media.manage`** : intégrité, export /
  import / sauvegardes de la base, champs personnalisés, réglages (débit, proxys auto), analyse de
  tous les volumes, reprise de l'existant, rattrapage et état des planches, import d'un partage vers
  un nœud, navigation réseau hors conteneur, et les routes « fichiers » de l'ancien Gestionnaire
  (`/api/media-manager/*` : volumes entiers par nœud et chemin). La page du Gestionnaire reste
  ouverte à `media.manage` : sa bibliothèque est filtrée.
- **Tâche de fond partagée** (`GET /api/media/scan`) : un restreint ne lit le résultat que d'une
  tâche qu'il a lancée.
- **Export d'un projet** : membre du projet (il était ouvert à tout connecté).
- **Macros** : une macro de projet ne résout que les médias de son projet (inchangé) ; le sélecteur
  de média stocke l'IDENTITÉ, libellé « nom — projet » (homonymes d'un même projet : identité courte
  en plus). Un nom tapé à la main reste résolu comme avant : dans le projet de la macro, sinon dans
  toute la base ; plusieurs médias de ce nom → refus « plusieurs médias s'appellent … : donner son
  identité ».
- **Homonymes** : chaque ligne porte `homonyms` (autres médias du même nom DANS la portée) et, s'il y
  en a, `name_hint` (ses projets, à défaut nœud · dossier), affiché en petit à côté du nom.

Cas limites connus : un conteneur rattaché à un projet par SNAPSHOT seulement (pas de
`containers.project_id`) ne range pas ses médias dans ce projet → invisibles d'un restreint ;
envoyer depuis le poste un contenu IDENTIQUE à un média hors portée (« l'envoyer quand même ») le
rattache, par l'empreinte, à ce média existant, qui entre alors dans le projet de l'utilisateur ;
l'import d'un projet (`projects.manage`) peut, par l'identité écrite dans le fichier, rattacher au
nouveau projet un média existant. Garde : `tests/verif_media_portee.py`.

## Partage avec un utilisateur, un groupe, un projet (décision du 2026-10-12)

But : « donner l'accès à des médias hors projets » (médias sans projet compris). Module
`app/media_sharing.py`, routes `app/routes/media_sharing_api.py`, groupes `app/user_groups.py`.

- **Destinataires** : un utilisateur, un **groupe d'utilisateurs** (nouveau : un ensemble de
  PERSONNES, sans aucun droit — distinct des habilitations ; tables `user_groups` /
  `user_group_members` dans la base principale ; page `/groups`, réservée à `settings.edit`), ou
  les membres d'un **projet** (`project_members`). Le partage suit le groupe et le projet : qui y
  entre l'obtient, qui en sort le perd.
- **Deux niveaux** : « Consulter et utiliser » (`use` : voir, lire, proxy, planche, charger dans
  ses conteneurs, rapatrier, copier, mettre en playlist) et « Modifier » (`edit` : en plus, la
  fiche — métadonnées, vignette, proxy, planche — et, pour un bin, y ranger / en retirer).
  **Jamais** par un partage : supprimer le média ou une copie, oublier une copie, renommer un
  fichier, rattacher à un projet, supprimer un proxy, annuler sa génération, créer / renommer /
  supprimer un bin, importer, ouvrir l'éditeur d'image, gérer les partages — c'est le **périmètre
  propriétaire** (ses projets, ou l'accès global). Niveau insuffisant : 403 `shared_read_only`.
  Un bin partagé en « Modifier » ne rend pas ses médias modifiables (ils y sont en « use »).
- **Qui partage** : `media.manage` ET la cible dans son périmètre propriétaire. Un partage reçu ne
  se repartage pas (ce qui coupe aussi toute récursion). Retrait : le propriétaire de la cible
  (même règle) ou l'auteur du partage.
- **Portée** : `media_sharing.user_scope()` remplace `auth.scoped_project_ids()` dans les routes de
  la base. Il rend None (accès global, rien ne change) ou un `MediaScope` : l'ensemble des projets
  (tout le code de portée de d2fcc73 le lit comme avant) plus `assets` / `bins` partagés.
  `scope_sql`, `asset_visible`, `visible_ids`, `_visible_bin_ids` et `list_bins` les ajoutent :
  listes, recherche, valeurs proposées, compteurs, fiche, vignette, proxy, planche suivent. Aucune
  seconde fonction de portée. Calculé une fois par requête, jamais gardé d'une requête à l'autre :
  un retrait vaut immédiatement.
- **Bin partagé = bin VIVANT** : ses sous-bins et ce qui y entre ensuite. Évalué à chaque requête
  dans la portée de base du **partageur**, telle qu'elle est maintenant : un sous-bin qu'il ne voit
  pas n'est pas partagé ; un média d'un autre projet rangé dans le bin n'est pas exposé ; un **bin
  intelligent** ne livre que les résultats que le partageur verrait (sa recherche évaluée dans SA
  portée, puis bornée encore par `visible_ids`). Le destinataire qui ouvre ce bin intelligent y
  voit en outre les médias de SES projets qui répondent à la recherche — qu'il voit de toute façon.
  Partageur supprimé, ou qui ne voit plus la cible : partage inerte.
- **Macro sans utilisateur** (déclencheur, pupitre) : la portée du projet de la macro (macro
  système : globale) est figée dans le partage (`sharer_projects`).
- **Charger un média partagé** dans un conteneur : autorisé en « use » ; il est chargé (ou
  rapatrié, copié en local avec un bin) **sans être rattaché** au projet du conteneur — ce
  rattachement l'exposerait à tous les membres du projet, donc le ferait sortir du partage. Les
  routes marquent (média, conteneur) avant d'agir ; `link_container_project` consomme la marque
  (6 h au plus). Conséquence : la bibliothèque d'un player FILTRÉE par son projet ne le liste pas ;
  on le charge depuis « Partagés avec moi » ou la bibliothèque générale.
- **Date de fin** facultative (date seule = fin de ce jour, heure locale du serveur) ; échue :
  retirée et journalisée. **Journal** `media_share_log` (base des médias) : partage, modification,
  retrait, échéance — qui, quoi, avec qui, quand. Tables `media_shares` / `media_share_log` dans
  `db_medias.db` (créées par le module, idempotentes) : elles suivent sauvegarde / export / import
  de la base des médias ; les identifiants d'utilisateurs, de groupes et de projets y sont ceux de
  cette installation.
- **Interface** : clic droit sur un média, une sélection ou un bin → « Partager… » (destinataires
  cherchés parmi utilisateurs, groupes, projets ; niveau ; date de fin ; partages en cours avec
  niveau modifiable et retrait ; journal). Arbre des bins : nœud « Partagés avec moi » (médias
  partagés un par un ; bins partagés dessous, avec leurs sous-bins), marqués « ↗ partageur ».
  Fiche « Infos » → onglet « Partages ». Les gestes que le niveau n'autorise pas sont masqués.
- **Macros** : fournisseur du cœur `media_sharing` (`CORE_PROVIDERS`) — `share_bin` (bin, groupe /
  utilisateur / projet, niveau, durée en jours) et `unshare_bin` ; état `active_shares`. Lancée par
  un utilisateur : ses droits et sa portée font foi.
- **Non couvert** : envoi depuis le poste et import vers un bin partagé (périmètre propriétaire) ;
  la ferme de transcodage et l'éditeur d'image gardent la portée par projets.

Garde : `tests/verif_media_partage.py` (mutée : 15 mutations, toutes mordent ou sont masquées par
une seconde barrière nommée).

## Étapes

1. Registre (tables, cœur) + identité dans le sidecar + empreinte + réconciliation + vignettes
   centrales + champs personnalisés (Réglages).
2. Écran « Infos » ; bibliothèque du player et Gestionnaire de Médias lus depuis le registre ;
   le recorder enregistre le média dès le début de l'enregistrement.
3. Copies et déplacements vérifiés.
4. Projets : médias référencés, export/import, hors ligne, playlists par identité.
5. Import depuis les partages (choisi à la main) et dossiers de dépôt du Transcoder.

## Décisions (2026-10-11)

- **Tables dans le cœur**, dans une **base dédiée** `db_medias.db` (module `app/media_registry.py`),
  avec sauvegarde, export et import propres. Pas de transaction commune avec la base principale
  (projets) : les références croisées sont surveillées par l'outil d'intégrité.
- **Outil d'intégrité** : médias référencés mais introuvables (projet → média sans copie présente,
  copie attendue absente), et l'inverse (fichiers présents sur un volume mais inconnus du registre,
  médias sans copie ni projet), copies dont l'empreinte ne correspond plus, vignettes manquantes.
- **Export de projet** : références seules par défaut ; option « embarquer les médias ».
- **Transcodeur** (2026-10-11) : ses sorties entrent d'elles-mêmes dans la base, avec filiation et
  traitement complet. Le fichier DÉPOSÉ dans un dossier de dépôt, lui, n'y entre PAS (décision :
  « non » à l'import automatique de la source). Sa source est retrouvée par l'empreinte de l'entrée
  si elle est déjà dans la base ; sinon la sortie reste sans filiation mais dit d'où elle arrive
  (dossier de dépôt, fichier déposé, taille, date).
- **Suppression** : au choix, une copie (le média et ses métadonnées restent tant qu'il en reste
  une) ou le média entier (toutes ses copies).
- **Player** : plus d'onglet « Volume média » — la bibliothèque liste les médias du registre (du
  projet du player, ou tous sans projet), avec une colonne « copie locale » (présente sur ce
  nœud / sur un autre nœud / hors ligne). Charger un média sans copie locale la rapatrie d'abord.

## 2026-10-16 — Versions d'un média sous une seule fiche

Décision de l'utilisateur : « toutes les essences d'un seul média doivent être sous la même fiche ».
Une sortie du transcodeur n'est plus un nouveau média (fiche + code + `derived_from`) mais une
VERSION du média source (`media_versions`, `media_files.version`), nommée `<base>__<code>_<clé>.<ext>`.
Le défaut constaté avant (job relancé à l'identique → second fichier rattaché par empreinte au premier
média alors que son nom portait un autre code) disparaît : même nom, même version. Modèle de
référence : `docs/reference/MEDIAS_VERSIONS.md`. Pas de code de reprise dans le produit : les fiches
dérivées existantes ont été repliées UNE fois, à la main (script ponctuel hors dépôt).
