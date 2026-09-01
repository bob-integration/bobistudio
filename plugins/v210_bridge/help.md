# Pont v210 (interop MXL inter-éditeurs)

Passerelle d'interopérabilité entre le bus MXL de Bobi.Studio et un **autre éditeur MXL**
présent sur le **même serveur** (même domaine `/dev/shm/mxl`). Le pont convertit entre le
format interne **planar** (celui de toute la flotte) et le format standard **`video/v210`**
du SDK MXL stock — le seul format qu'un container tiers non patché sait lire ou écrire.
Bidirectionnel : un pont **exporte** ou **importe**, jamais les deux à la fois. Réservé au
**4:2:2 progressif** (v1) — pas d'entrelacé, pas d'autre chroma.

## Sens du pont

- **Export** (par défaut) : câbler une source planar interne (page **Câbles**) → le pont
  publie son **miroir** au format `video/v210`, lisible par le container tiers. Ce flux
  miroir **n'apparaît pas dans Câbles** (nos lecteurs attendent du planar, pas du v210) : le
  tiers le cible par son **flowId** (UUID), affiché sur `/state` (`out_flow_id`) — c'est
  cette valeur qu'on communique à l'autre éditeur, pas un nom.
- **Import** : le pont lit un flux `video/v210` publié par le tiers ailleurs sur le domaine,
  et publie sa conversion **planar interne** sous `{hostname}` — un flux normal, câblable
  comme n'importe quelle source Bobi.Studio.

Changer le sens redéploie le pont (nouvelle direction = nouveau rôle de flux).

## Export : nom du flux miroir

**Nom du flux miroir v210** (`out_name`) : nom du flux publié pour le tiers. Vide =
`<hostname>_v210`. Une entrée 8 bits est **promue 10 bits** (`<<2`) au passage, le v210
étant nativement 10 bits — la sortie miroir est donc toujours 10 bits quelle que soit la
profondeur d'entrée.

## Import : trouver et cibler la source tierce

**Flux v210 source** (`import_flow`) : le **flowId UUID brut** du flux tiers à importer (ou,
plus rarement, un nom de flux maison). `GET /flows` liste les flux `video/v210` découverts
sur le domaine — sources candidates avec leur UUID, y compris ceux publiés par nos propres
ponts en export (`is_ours: true`, à ignorer ici : s'importer soi-même n'a pas de sens).

**Profondeur de la sortie planar** (`import_bit_depth`) :

| Valeur | Comportement |
|--------|--------------|
| **8 bits** (défaut) | pipeline `force8` : la profondeur 10 bits native du v210 est ramenée à 8 (`v >> 2`) |
| **10 bits** | sortie `PLANAR10LE`, pleine précision |

## Débit et méthode

La conversion (`libbobi_v210`, SIMD) tourne directement dans la vue du grain de sortie
(zéro-copie), ~2-5 ms/image en 1080p. Repli numpy automatique si la bibliothèque SIMD n'est
pas chargée (`simd: false` sur `/state` — plus lent mais fonctionnel, jamais un échec muet).
L'**index de grain est propagé 1:1** entre entrée et sortie (même grille) : un consommateur
qui suit la tête de la sortie voit la même cadence que l'entrée, sans resynchronisation.

## Limites (v1)

- **4:2:2 progressif uniquement.** Une entrée entrelacée, ou un flux tiers `interlace_mode`
  autre que `progressive`, est **refusé** (raison publiée sur `/state.reason`, pas d'échec
  muet — le pont reste inactif tant que la source ne convient pas).
- **Largeur paire obligatoire** (contrainte 4:2:2).
- Le grain est commité **trame entière** même si l'amont ou l'aval travaille en tranches
  (re-tranchage sémantique « ligne » non traité en v1).

## Diagnostiquer un pont inactif

`/state.active` vaut `false` tant que `/state.reason` n'est pas vide. Causes courantes :

- **Export** : `input_shm` non câblé (« aucune source ») ; entrée entrelacée ou chroma ≠
  4:2:2 ; largeur impaire.
- **Import** : `import_flow` vide (renseigner le flowId via `GET /flows`) ; flux introuvable
  ou son flowDef pas encore prêt ; `media_type` du flux cible ≠ `video/v210` (le pont refuse
  d'importer autre chose) ; flux entrelacé.
- Une source qui **change de format en cours de route** (grain plus court que prévu) ou un
  producteur **recréé** (shm re-publié sous le même nom, index reparti en arrière) sont gérés
  automatiquement : le pont ferme, relit le nouveau format et rouvre — invisible en
  fonctionnement normal, visible dans le journal (`log_level: debug` pour le détail).

## Contrôle

- `GET /state` — sens, entrée/sortie, flowId du flux publié, format effectif, `active` /
  `reason`, `simd` (chemin de conversion réellement utilisé).
- `GET /flows` — flux `video/v210` découverts sur le domaine (candidats à l'import).
- `POST /input` — `{shm}` : re-câble l'entrée planar en export (hot-wire, sans redéploiement).

## Notes

- Un pont ne sert que pour un **échange local, même serveur** : ce n'est pas un transport
  réseau (cf. RDMA pour la réplication inter-nœuds, ou 2110_io pour le réseau ST 2110).
- Le flux miroir exporté n'étant pas dans Câbles, il ne peut pas être recâblé accidentellement
  vers un consommateur Bobi.Studio — seul un container tiers peut s'y abonner par UUID.
