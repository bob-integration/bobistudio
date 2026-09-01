# NMOS pour le bus MXL — le contrat que nous publions

**Ce document FAIT FOI** (cf. `CLAUDE.md`, « Où vit la documentation »). Il décrit ce qu'un
**contrôleur ou un intégrateur tiers** trouve en interrogeant Bobi.Studio — pas comment c'est
construit, ni ce qui reste à faire. Le journal de travail du chantier est dans
`TODO.md` § « NMOS DANS LES CONTENEURS » ; ne pas y chercher la même chose.

Relevé sur le code au **2026-08-31**. Toute valeur citée ici est vérifiable dans les sources
indiquées : si l'une diverge, c'est le code qui a raison et ce document qui doit être corrigé.

---

## 1. Ce que Bobi.Studio expose, et sur quoi

Le bus interne **MXL** (mémoire partagée, un domaine par nœud) est publié en ressources NMOS
standard, à côté des flux ST 2110 déjà exposés. Un contrôleur voit donc **deux familles** sur le
même Node :

| Famille | `transport` | Ce que c'est |
|---|---|---|
| ST 2110 | `urn:x-nmos:transport:rtp.mcast` | les flux réseau du moteur `2110_io` |
| **MXL** | **`urn:x-nmos:transport:mxl`** | le graphe interne : mixer, multiview, streamer, player, recorder, ports shm du moteur |

Correspondance : **une sortie de plugin** (`produces`) devient Source + Flow + **Sender** ; **une
entrée** (`consumes`) devient un **Receiver**.

Base : `services/nmos/mxl.py`. Réglage `nmos_mxl` (**activé** par défaut).

## 2. Ce que BCP-007-03 impose, et que nous respectons

Spécification : [AMWA BCP-007-03 v1.0.0](https://specs.amwa.tv/bcp-007-03/), publiée le 2026-08-18.

- `transport` = `urn:x-nmos:transport:mxl` — la BCP impose ce **littéral**, elle ne délègue pas au
  registre des transports.
- `interface_bindings` = **tableau vide** : MXL est de la mémoire partagée, il ne sort par aucune
  interface réseau.
- `manifest_href` = **null**, et `GET …/single/{senders,receivers}/<id>/transportfile` → **404** :
  MXL n'a pas de fichier de transport.
- `transport_params` = `{mxl_domain_id, mxl_flow_id}`, avec une **asymétrie** :

  | | `null` | `"auto"` | UUID |
  |---|---|---|---|
  | `sender.mxl_domain_id` | ✔ | ✔ | ✔ |
  | `sender.mxl_flow_id` | ✔ | ✔ | ✔ |
  | `receiver.mxl_domain_id` | ✔ | ✔ | ✔ |
  | `receiver.mxl_flow_id` | ✔ | **✘** | ✔ |

- `"auto"` est accepté en `/staged` mais **ne s'énumère jamais** dans `/constraints`.
  Dans `/active`, il est **résolu** vers la valeur réelle.

## 3. Localité — la contrainte qu'il faut comprendre

**Le bus MXL est local à un nœud.** Un Receiver ne peut lire que les flux publiés sur **son**
domaine. `/constraints` le dit : il n'énumère **qu'un seul** `mxl_domain_id` (celui du nœud du
conteneur servant) et ne liste que les flux de ce nœud.

Un flux d'un autre nœud n'est atteignable que s'il a été **répliqué** (RDMA) — la réplique est
alors un flux **local**, avec son propre `mxl_flow_id`.

## 4. Écriture : lecture seule aujourd'hui

Tout `PATCH …/staged` visant une ressource MXL répond **405**, avec le motif et la marche à
suivre. Cela vaut pour le PATCH unitaire **et pour les endpoints `bulk`**.

Le câblage se fait par l'orchestrateur (page Câbles). Le réglage `nmos_mxl_ecriture`
(**fermé** par défaut) lève ce verrou ; il est destiné aux essais, pas à l'exploitation, tant que
l'autorité du routage n'a pas été formellement basculée vers IS-05.

Les ressources ST 2110 ne sont **pas** concernées : leur IS-05 reste ouvert.

## 5. Grouping BCP-002-01

Chaque ressource MXL porte un `urn:x-nmos:tag:grouphint/v1.0` de la forme `<groupe>:<rôle>`.
Les flux vidéo ouvrent les bundles ; audio et données rejoignent le bundle de **même rang** dans
leur propre essence. Un conteneur à une seule vidéo forme un bundle unique.

> ⚠ **Le câblage est groupé côté orchestrateur** : poser la vidéo pose aussi l'audio et l'ANC
> associés. Le group hint est là pour que ce comportement soit lisible plutôt que surprenant.

## 6. Registre IS-04 embarqué

Réglage `nmos_registre` (**fermé** par défaut). Base : `services/nmos/registre.py`, version d'API
`v1.3`. **Fermé, TOUS les endpoints répondent 501** — écriture comme lecture, Query API comprise. Un service « ouvert mais vide » serait indiscernable d'un service ouvert et cassé.

**Registration API** — `/x-nmos/registration/v1.3/`

| Endpoint | Comportement |
|---|---|
| `POST /resource` | **201** à la création, **200** à la mise à jour, en-tête `Location` |
| `POST /resource` avec un parent non enregistré | **400** — refusé, pour ne pas créer d'orphelin ineffaçable |
| `DELETE /resource/<type>s/<id>` | **204**, avec suppression **immédiate de la descendance** |
| `POST /health/nodes/<id>` | **200** + `{"health": "<epoch>"}` ; **404** si le Node est inconnu (→ se ré-enregistrer) |
| `GET /health/nodes/<id>` | état + délai avant expiration |

Types acceptés : `node`, `device`, `source`, `flow`, `sender`, `receiver`.

**Ramasse-miettes** : `nmos_registre_gc_s`, **12 s** par défaut (valeur recommandée par IS-04),
plancher à 4 s. À l'expiration, le Node **et toutes ses sous-ressources** sont retirés.

**Query API** — `/x-nmos/query/v1.3/` : collections par type, accès par id, filtres `?id=` et
`?label=`. Registre **ouvert**, `/subscriptions` rend une **liste vide** (les abonnements
WebSocket ne sont pas implémentés) plutôt qu'une erreur, pour ne pas faire échouer une découverte
entière.

**Le stockage est en mémoire, volontairement** : un registre décrit qui est vivant *maintenant*.
Après un redémarrage du contrôleur, les Nodes se ré-enregistrent — c'est le comportement prévu par
la spécification.

## 7. Découverte DNS-SD

Réglage `nmos_mdns_enabled`. Quand il est actif, sont annoncés :

- `_nmos-node._tcp` — TXT `api_proto`, `api_ver`, `api_auth`
- `_nmos-register._tcp` et `_nmos-query._tcp` — **seulement si le registre est ouvert**, TXT
  identiques **plus `pri`**

> ⚠ **`pri` vaut 100 par défaut, c'est-à-dire « développement »** au sens d'IS-04 (« Values 100+
> are reserved for development work to avoid colliding with a live system »). Un contrôleur strict
> **ignore** donc notre registre tant que `nmos_registre_pri` n'a pas été abaissé entre 0 et 99.
> Ce défaut est délibéré : s'annoncer d'emblée en priorité de production détournerait vers nous
> les Nodes d'un registre déjà en place sur le même réseau.

## 8. Modèle de contrôle MS-05-02 (IS-12 / IS-14)

Réglage `nmos_plugins_ncp` (**fermé** par défaut). Base : `services/nmos/plugins_ncp.py`.
Les objets vivent dans le bloc `plugins` de l'appareil.

**Trois classes non standard**, aux `classId` **fixes** :

| Classe | `classId` | Dérive de | Propriétés | Méthodes |
|---|---|---|---|---|
| `NcBobiPlugin` | `[1, 1, 0, 1]` | `NcBlock` | `pluginType`, `vmid`, `hostname`, `instanceUuid` | — |
| `NcBobiParametre` | `[1, 2, 0, 1]` | `NcWorker` | `key`, `groupLabel`, **`value`**, `valueType`, `minimum`, `maximum`, `step`, `defaultValue` | — |
| `NcBobiAction` | `[1, 2, 0, 2]` | `NcWorker` | `actionId`, `label`, `argumentFields`, `fixedBody` | `Invoke(argumentsJson)` |

Toutes les propriétés sont au **niveau 3**. **`value` est la seule propriété inscriptible** du
modèle : c'est par elle que passe tout le pilotage de paramètre. Les bornes publiées
(`minimum`/`maximum`, ou l'énumération) sont **appliquées** : une valeur au-delà est refusée.

`Invoke` prend un objet JSON sérialisé ; les champs attendus sont décrits par `argumentFields`,
chacun pouvant porter un `optionsEndpoint` à interroger pour obtenir la **liste vivante** de ses
valeurs. Un argument inconnu est **refusé**, en nommant les clés attendues.

Un refus du conteneur remonte en **erreur** : un `Set` ou un `Invoke` qui répond OK a bien été
appliqué.

### Clé d'autorité — ce qui changera

Le troisième élément des `classId` ci-dessus (`0`) est la **clé d'autorité** MS-05-02. La spec
impose l'identifiant IEEE de l'organisation, négativé, ou **`0`** pour une organisation qui n'en
possède pas.

> **Un CID IEEE a été demandé le 2026-08-30 et n'est pas encore attribué.** À l'attribution, la clé
> passera de `0` à `-<CID>` — **les trois `classId` changeront donc**. Un contrôleur qui les aurait
> mémorisés devra être resynchronisé. C'est pourquoi ce modèle est **fermé par défaut** : ne
> l'ouvrir à un tiers qu'en connaissance de cette échéance.
>
> La clé n'existe qu'en **un seul littéral** dans tout le produit :
> `services/nmos/plugins_ncp.py:CLE_AUTORITE`.

## 9. Types de média et formats publiés

| Essence | `format` | `media_type` | Au registre NMOS ? |
|---|---|---|---|
| vidéo | `urn:x-nmos:format:video` | `video/x-mxl-planar` | **non** — voir ci-dessous |
| audio | `urn:x-nmos:format:audio` | `audio/float32` | oui |
| données (ANC) | `urn:x-nmos:format:data` | `video/smpte291` | oui |

## 10. Limites assumées

Elles sont ici pour qu'un intégrateur les découvre **avant** de câbler, pas après.

1. **`urn:x-nmos:transport:mxl` n'est pas au registre des transports AMWA** (vérifié le
   2026-08-31). La BCP impose pourtant ce littéral. Un contrôleur qui valide strictement les
   transports contre le registre peut refuser nos ressources MXL.
2. **`video/x-mxl-planar` n'est pas au registre des types de média.** Le planar est un choix
   interne assumé et chiffré ; publier du `video/v210` sur un flux planar serait une
   non-conformité pire. Un miroir v210 peut être inséré au cas par cas quand un tiers doit lire un
   flux précis (cf. `docs/reference/MXL_INTEROP.md`).
3. **`NcBobiParametre.value` est publié en `NcString`.** MS-05-02 n'a pas de type variant, et nos
   paramètres sont tantôt nombres, tantôt énumérations, tantôt booléens. `valueType` indique
   comment coercer. Publier trois propriétés typées mutuellement exclusives aurait laissé deux
   valeurs nulles sans dire laquelle fait foi.
4. **Aucune autorisation IS-10.** Les TXT DNS-SD annoncent honnêtement `api_auth=false`. La
   sécurisation repose sur le réseau et le mTLS du plan de contrôle, pas sur BCP-003-02.
5. **Les abonnements WebSocket de la Query API ne sont pas implémentés** — interrogation
   périodique uniquement.
6. **L'écriture IS-05 sur la surface MXL est fermée** (§ 4).

## 11. Réglages

| Réglage | Type | Défaut | Effet |
|---|---|---|---|
| `nmos_mxl` | bool | **`true`** | publie la surface MXL en IS-04/IS-05 |
| `nmos_mxl_ecriture` | bool | `false` | lève le verrou de lecture seule (essais) |
| `nmos_registre` | bool | `false` | sert la Registration API et la Query API |
| `nmos_registre_gc_s` | int | `12` | délai d'expiration d'un Node sans battement (plancher 4 s) |
| `nmos_registre_pri` | int | `100` | priorité DNS-SD ; **abaisser entre 0 et 99 pour un usage réel** |
| `nmos_plugins_ncp` | bool | `false` | publie le modèle MS-05-02 des plugins |

Déclarés dans `services/nmos/manifest.json` (`settings_keys`).

## 12. Vérifier une instance

Quatre bancs, exécutables sur une instance installée (`tools/` est embarqué par `app/builder.py`) :

```bash
./venv/bin/python tools/verif_nmos_mxl.py           # dérivation + gardes de lecture seule (HTTP)
./venv/bin/python tools/verif_nmos_registre.py      # registre : logique, HTTP réel, annonce DNS-SD
./venv/bin/python tools/verif_nmos_plugins_ncp.py   # le contrat MS-05-02 publié
./venv/bin/python tools/banc_nmos_mxl_live.py --go  # parc réel : deux conteneurs jetables
```

⚠ Le dernier **crée et détruit des conteneurs** (d'où `--go`). Les trois autres ne mutent rien de
durable : ceux qui touchent un réglage le restaurent dans un `finally`.
