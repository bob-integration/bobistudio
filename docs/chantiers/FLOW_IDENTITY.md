# Identité d'un flux MXL : découpler l'identifiant du nom lisible (relevé 2026-08-08)

> Chantier OUVERT, non commencé. Déclencheur : question utilisateur « pourquoi garde-t-on le
> hostname si le SDK ne le garde pas ? ». Réponse courte : le SDK ne le garde pas, **on le hache
> pour fabriquer l'identifiant** — donc le nom est plus porteur, pas moins. Ce document rassemble
> tout ce qu'il faut pour attaquer sans re-fouiller.

## 1. Le constat

`script_templates/bobimxl.py:319` :

```python
def flow_id(name: str) -> str:
    """UUIDv5 déterministe d'un nom de flux (= le champ `id` du flowDef, et le flowId lecteur)."""
    return str(uuid.uuid5(_NS_BOBI, name))          # _NS_BOBI défini l. 65
```

Le SDK MXL n'identifie un flux **que** par un UUID : c'est `flow_def["id"]`, c'est l'argument de
`mxlCreateFlowReader`, et c'est le nom du répertoire réel `/dev/shm/mxl/<uuid>.mxl-flow/`. Le
chemin `/dev/shm/<hostname>_<idx>` que manipule tout notre code est un **chemin fantôme**, vestige
du bus shm maison d'avant MXL. `plugins/2110_io/mtl_rx.c:414` le dit en clair :

```c
/* Nom de flux = basename du shm_path historique (strip /dev/shm/). */
```

et recalcule le même `uuid5` de son côté (`mtl_rx.c:392`, avec la mention « DOIT être identique à »
bobimxl).

**Le problème n'est pas le hostname, c'est la CONFUSION de deux rôles.** MXL offre déjà les deux
emplacements, et on les remplit avec la même chaîne — `bobimxl.py:405` et `:410` :

```python
"id":    flow_id(name),    # identité machine
"label": label or name,    # nom lisible — le champ prévu exactement pour ça
```

On dérive l'identité **du** libellé. Le modèle NMOS les sépare ; nous non.

## 2. Ce que ça coûte aujourd'hui

- **Le hostname est irrévocable.** Aucune UI ne renomme un conteneur, et c'est structurel :
  renommer change l'UUID, donc c'est un AUTRE flux, et tout lecteur câblé dessus perd sa source.
  Documenté en `app/docker_driver.py:68-73` (« à ne faire qu'à la CRÉATION, jamais sur un moteur
  en service »).
- **L'unicité doit être défendue à la main** au lieu d'être impossible par construction. Garde-fou
  posé le 2026-08-08 : `app/hostnames.py` (normalisation + validation + unicité insensible à la
  casse), branché dans `app/routes/__init__.py:creer()`. C'est ce garde-fou qui rend la situation
  actuelle tenable — il ne remplace pas ce chantier.
- **Du code relit de la sémantique dans le nom**, alors qu'elle existe déjà dans `flow_def.json`
  (`format`, `media_type`) et dans les manifestes (`essence`, `slot`) :
  - `services/rdma/__init__.py:290` `_infer_kind()` déduit l'essence par sous-chaîne
    (`"_audio_" in f`) → **un flux mal suffixé est répliqué comme de la vidéo**
  - `app/routes/home_dashboard.py:87` retrouve l'index par `rsplit("_", 1)` → `hn_audio_3` et
    `hn_3` donnent tous deux `3`
  - `app/metrics.py:790` (`_RX_SHM_SUFFIXE`) et `app/routes/nmos_detail.py:209` **reconstruisent**
    le nom depuis (hostname, essence, index) — couplage inverse, même fragilité
  - et c'est **déjà cassé** : `app/routes/cabling.py:694` documente l'abandon de la dérivation
    parce que le player produit `p1_audio`, pas `p1_audio_0`

## 3. La cible

| | Aujourd'hui | Cible |
|---|---|---|
| `flow_def["id"]` | `uuid5(NS, "<hostname>_<idx>")` | `uuid5(NS, "<instance_uuid>:<slot>")` |
| `flow_def["label"]` | même chaîne que l'id | `<hostname>` — **librement modifiable** |
| Renommer un conteneur | casse tous les liens | opération banale, l'id ne bouge pas |
| Unicité | vérifiée à la création | garantie par construction |

`containers.instance_uuid` est le bon socle : c'est le **barreau 2** de l'identité
(`CLAUDE.md`, « Identité d'un conteneur : trois barreaux »), il survit recreate / restore /
import de projet, et il est **déjà** utilisé comme clé de stabilité par NMOS
(`services/nmos/__init__.py:302-309`, lookup par `(instance_uuid, slot_key, essence, kind)`).
Le couple `(instance_uuid, slot)` y est donc déjà éprouvé.

⚠ `instance_uuid` ne survit PAS au **remplacement** (autre conteneur pour la même fonction). Pour
ce cas, l'identité stable est le **barreau 3**, l'emplacement (`production_roles`). À trancher au
moment du chantier : keyer sur `instance_uuid` (simple, aligné NMOS) ou sur l'emplacement (survit
au remplacement, mais tous les conteneurs n'en ont pas un).

## 4. Périmètre réel de la bascule

### En base (mesuré sur `db_bobistudio.db` le 2026-08-08)

| Table.colonne | Lignes portant un nom de flux |
|---|---|
| `source_labels.shm` (+ `parent_shm`) | 65 |
| `rdma_links.src_flow` | 29 |
| `tsl_mapping.source_shm` | 6 |
| `containers.shm_out` | 5 |
| `tsl_sources.linked_shm` | 1 |
| `fabric_node_alloc.shm` | 0 |
| `probe_events.flow` | 0 |

~106 lignes au total sur cette instance — le volume n'est pas le problème, la **cohérence** l'est.

### En code

- **Fabrication des noms** : `app/plugins.py:961 derive_wiring()` (substitution `{hostname}`,
  `repeat`, `from_list`) + les `shm` de tous les `plugins/*/plugin.json` + le bloc pyramide
  (`<source>__pL`, dupliqué dans `plugins/pyramide/script.py`)
- **Les 3 sites de parsing sémantique** listés au §2 — à supprimer, pas à porter
- **Le moteur C** : `plugins/2110_io/mtl_rx.c:392,414` recalcule `uuid5` du nom ; il faut lui
  passer la nouvelle clé (ou directement l'UUID)
- **Les SSRC RTP** : `plugins/2110_io/docker/controller.py:2589 _ssrc()` = `crc32("<HOSTNAME>:tx:v:0")`.
  Les SSRC sont **annoncés en `a=ssrc` du SDP** et certains récepteurs (Blackmagic) valident les
  paquets entrants contre cette valeur → changer la graine **recrée les sessions TX**. À traiter
  comme un axe de signature (`compute_sig`, cf. `plugins/2110_io/meta.json:464`), pas comme un
  détail. **Peut être découplé du reste** : le SSRC pourrait passer sur `instance_uuid` sans
  attendre le chantier de nommage, ou rester sur le hostname si on garde `label = hostname`.
- **L'UI** : la page Câbles adresse par `data-shm`, `MXLMonitor` prend le shm comme adresse,
  `services/tsl/__init__.py:1079` (`/api/tsl/sources/by_shm`) est **indexé par nom de shm**. C'est
  là que le `label` reprend son rôle.

### Transition

Les flux existants **changent d'UUID** → coupure de tous les liens au basculement. Deux options à
arbitrer :
- **coupure franche** (fenêtre de maintenance, migration DB des ~106 lignes en une passe), ou
- **double-écriture** : le writer publie sous les deux UUID le temps que les lecteurs migrent —
  coût mémoire d'un grain dupliqué par flux, à chiffrer avant de choisir.

## 5. Ce qui n'est PAS en cause

- **La conformité MXL.** `docs/reference/MXL_INTEROP.md` classe notre identité « CONFORME
  (uuid5 = UUID RFC valide) » et le layout `/dev/shm/mxl` « CONFORME (posé par libmxl, non
  customisé) ». Rien dans le SDK ni dans BCP-007-03 n'interdit un nom lisible — le seul manque
  normatif relevé côté identité est `domain_def.json` (cf. `TODO.md`, section BCP-007-03).
- **Le hostname dans le libellé.** Il garde ses deux justifications : anticollision inter-nœuds à
  la réplication RDMA (`app/docker_driver.py:98-105`) et lisibilité dans l'UI. La cible le
  **conserve**, en `label` — elle le retire seulement de l'*identifiant*.
- **`Reader(by_id=)` / `discover_flows()`** : l'écart « nos readers ne savent cibler qu'un nom »
  relevé dans MXL_INTEROP est déjà comblé (`bobimxl.py:328,334`). Le lecteur sait déjà travailler
  sur un UUID arbitraire — c'est un prérequis du chantier, et il est acquis.

## 6. Ordre d'attaque proposé

1. **Supprimer les 3 sites de parsing sémantique** (`rdma:_infer_kind`, `home_dashboard` rsplit,
   `metrics`/`nmos_detail` reconstruction) en lisant `format`/`media_type` du `flow_def.json` et
   `essence`/`slot` du manifeste. **Indépendant du reste, aucune migration, gain immédiat** — et
   `rdma:290` est un vrai bug latent (essence devinée → réplication en vidéo).
2. Introduire `flow_key(instance_uuid, slot)` à côté de `flow_id(name)`, sans l'utiliser.
3. Publier `label = hostname` partout (déjà le cas de fait) et faire passer l'UI du `shm` au
   `label` là où c'est de l'affichage.
4. Basculer writers + readers + `mtl_rx.c` sur la nouvelle clé, migrer les ~106 lignes.
5. Ouvrir le renommage d'un conteneur dans l'UI — c'est le livrable qui justifie le chantier.

L'étage 1 vaut d'être fait **même si le reste n'est jamais entrepris**.
