# Journal d'exploitation des plugins — contrat `control.journal`

> Fait foi pour : ce qu'un plugin déclare pour que ses commandes apparaissent dans le journal
> d'exploitation (« qui a fait quoi »), la forme d'une ligne, les auteurs, la rétention, et le
> format des événements internes `GET /events`. Rédigé le 2026-10-17. Cahier des charges et
> décisions de l'exploitant : `docs/chantiers/JOURNAL_EXPLOITATION.md`.

Code : `app/ops_journal.py` (stockage, auteurs, portes d'écriture, lecture, état de macro),
`app/ops_journal_rules.py` (règles pures : validation, extraction, noms lisibles, différences de
structure), `app/ops_journal_events.py` (collecte des événements), `app/routes/ops_journal_api.py`
(page `/ops-journal`, API, export CSV). Banc : `tests/verif_journal_exploitation.py`.

## 1. Principe

L'orchestrateur voit passer toutes les commandes (proxy de plugin, macros, application de
structure, audio-suit-vidéo, bientôt les surfaces de contrôle) et connaît leur auteur. Le plugin,
lui, sait ce qui mérite d'être noté : il le **déclare** dans son `plugin.json`, et l'orchestrateur
applique la déclaration de la même façon pour tous. Aucun code de plugin n'est exécuté.

Une ligne = une commande acceptée (réponse 2xx du conteneur) : heure, instance (`instance_uuid`
+ nom au moment de l'action), auteur, type d'action, action lisible, chemins d'ids.

**Règle générique, non négociable : seule une valeur DISCRÈTE est notée** (booléen ou chaîne). Un
nombre — fader, pan, trim, niveau de départ, bouton d'EQ ou de dynamique — n'est **jamais** noté,
pas même résumé. Un objet ou une liste non plus. Un plugin n'a donc rien à déclarer pour exclure
un geste continu : c'est le type de la valeur qui décide.

## 2. Déclaration

```json
"control": {
  "endpoints": ["/set", "/mute", "/loudness", "/structure/validate", "/structure/apply", …],
  "persist": {"param": "console", …, "structure": {"apply": "/structure/apply", "validate": "/structure/validate"}},
  "journal": {
    "names":   {"param": "console", "ref_fields": ["bus"], "quiet_keys": ["params", "on"],
                "kind_names": {"mute": "Mute"}, "key_names": {"sends": "Départ"}},
    "exclude": ["^buses\\.[^.]+\\.solo\\."],
    "endpoints": {
      "/set":             {"mode": "set", "batch": "sets", "category": "setting"},
      "/mute":            {"mode": "fields", "target": "id", "fields": {"on": "Mute"}, "category": "mute"},
      "/loudness":        {"mode": "fields", "target": "bus", "fields": {"action": "Loudness"}, "category": "command"},
      "/structure/apply": {"mode": "structure"},
      "/structure/validate": false
    },
    "events": {"endpoint": "/events", "rev_field": "event_rev"}
  }
}
```

| Clé | Sens |
|---|---|
| `endpoints` | Par endpoint POST (doit figurer dans `control.endpoints`) : une règle, ou `false`. **Un endpoint absent n'est pas journalisé** (`false` sert à le dire explicitement). Les GET ne le sont jamais. |
| `mode: "set"` | Corps `{path, value}` ou lot `{<batch>: [{path, value}, …]}`. `path_field` / `value_field` (défaut `path` / `value`), `batch` (nom du champ de lot). Une entrée par valeur discrète ; le lot donne **une** ligne. |
| `mode: "fields"` | Les champs nommés dans `fields` (`{champ: libellé}`, chemin pointé admis : `tool.type`) sont notés s'ils sont discrets ; `target` = le champ qui porte l'id de l'élément visé (`/mute {id, on}` → « Entrée 1 · Mute : activé »). Aucun champ discret présent : pas de ligne. |
| `mode: "action"` | (2026-10-17) **Une ligne par commande acceptée, toujours** — corps vide (`/scenes/next`) ou champs déclarés absents compris. `label` (requis) = libellé fixe de la commande ; `target` facultatif ; les `fields` présents s'ajoutent en complément : « Rappel de scène — scène : Interview ». |
| `value_ref` | (modes `fields` et `action`) `{champ: référence}` : la valeur est une RÉFÉRENCE, traduite en nom depuis le document persisté, repli sur la valeur. `"scenes"` = collection du document, correspondance sur `id` ; `{"collection": "scenes", "match": "number"}` = sur un autre champ ; `{"path": true}` = la valeur est un chemin d'ids (`strips.s_in1.chain.m_eq1` → « Entrée 1 · EQ »). Seule exception à « jamais un nombre » : un champ déclaré `value_ref` peut être un entier ou une liste (un numéro de scène désigne, il ne se règle pas). |
| `skip_when` | (modes `fields` et `action`) `{champ: valeur}` : si TOUS correspondent, rien n'est noté (`{"dry_run": true}` : un collage à blanc n'a pas eu lieu). |
| `mode: "structure"` | Réservé à `control.persist.structure.apply` (refusé ailleurs) : la ligne porte le **résumé des différences** entre le document persisté AVANT et le document canonique appliqué (§ 4). |
| `category` | Type d'action, vocabulaire FERMÉ : `setting`, `mute`, `command`, `structure`, `event`. Défaut : `set` → `setting`, `fields` → `command`, `structure` → `structure`. |
| `exclude` | Motifs (regex) de chemins JAMAIS notés, ni par `/set` ni dans un diff de structure (la sélection PFL). |
| `names.param` | Clé de `deploy_config.params` où vit le document persisté (défaut : `control.persist.param`). Les noms lisibles en sont tirés. |
| `names.label_fields` | Champs de libellé d'un élément (défaut `label`, `name`, `short`). Le premier sert aussi à détecter un renommage. |
| `names.kind_fields` | Nature d'un élément sans libellé (défaut `type` puis `kind`). |
| `names.ref_fields` | Champs qui désignent un autre élément par son id (un départ : `bus`) → « Départ → Aux 1 ». |
| `names.quiet_keys` | Clés de chemin qui ne s'écrivent pas (défaut `params`). |
| `names.kind_names`, `names.key_names` | Noms d'affichage d'une nature (`mute` → « Mute ») ou d'une clé (`sends` → « Départ »). `key_names` s'applique à TOUTE clé du chemin, y compris absente du document (alias du moteur : `listen_mode` → « mode d'écoute »). |
| `events` | Optionnel : le plugin publie ses événements internes (§ 6). `endpoint` doit être un des `read_endpoints`. |

Une déclaration invalide fait **refuser le plugin au scan** (comme `control.persist`, `control.zones`),
avec la raison dans `SCAN_ERRORS`. Un plugin qui ne déclare rien n'est jamais affecté.

## 3. Noms lisibles

Le chemin d'ids est traduit à partir du document persisté : une liste d'objets à `id` est une
collection (son nom ne s'écrit pas), un élément s'écrit par son libellé, à défaut sa nature, à défaut
l'id. Exemple : `strips.s_in1.chain.e_mute1.params.on` → « Entrée 1 · Mute ». Le chemin d'ids
reste dans la ligne (colonne `target`, détail de la page, CSV) : un libellé se renomme, un id non.
Repli : un segment introuvable s'écrit tel quel — jamais d'échec, jamais de ligne vide.

## 4. Différences de structure

Calculées génériquement (aucun nom de collection codé) : éléments **ajoutés / retirés / renommés**
de toute liste à `id` (tranches, bus, VCA, éléments de chaîne — inserts, modules, plugins —, départs,
entrées, sorties…) ; scalaires **discrets** changés (affectations `vca`, `automix`, `align`, `src`
d'un patch, `bypass`, `on`, modes) avec ancien → nouveau, l'id remplacé par son nom ; listes sans id
(patch d'une sortie, canaux) : « modifié » ; liste d'ids (`route_to`) : `+Nom` / `−Nom`. Les nombres
ne sont jamais comparés. Au plus 40 entrées par ligne (`(+N autres)` ensuite). Une application
sans différence discrète est quand même notée : qui l'a appliquée est un fait.

## 5. Auteurs

`actor = {kind, name, detail}` ; colonnes `actor_kind`, `actor_name` (filtre « opérateur »), `actor_detail`.

| `kind` | Qui | Posé par |
|---|---|---|
| `user` | l'opérateur de l'interface | `plugin_proxy`, shotbox, pages Câbles / Libellés — déduit de la requête |
| `macro` | `name` = qui a lancé, `detail` = la macro | `macros.exec_action` / `exec_post` (`_run_actor(run)`) |
| `trigger` | `name` = le déclencheur, `detail` = la macro | idem, quand `_Run.user` vaut `trigger:<nom>` |
| `surface` | `name` = l'opérateur de la surface, `detail` = la surface | `ops_journal.surface_actor(surface, operator)` — à passer en `actor=` par `services/control_surface` (à venir) |
| `auto` | l'orchestrateur (`detail` = la fonction, ex. `afv`) | AFV ; tout appel hors requête sans auteur explicite |
| `plugin` | événement interne du plugin | collecteur `/events` |

Un chemin hors requête qui connaît son auteur le passe explicitement : `record_post(…, actor=…)`,
`plugin_persist.structure_apply(…, actor=…)`, `macros.exec_action(…, actor=…)`. On ne devine jamais un humain.

## 6. Événements internes — `GET /events?since=<ms>` (proposé à la session Audio)

Ce que l'orchestrateur ne voit pas passer (saturations, flux perdu / retrouvé, blocs en retard, refus
du temps réel, démarrage). Même modèle que `/clips` + `clip_rev`.

```
GET /events?since=1760700000000
→ 200 {"boot": "k3f9q2", "rev": 42, "now": 1760700012345, "keep_s": 86400,
       "events": [
         {"seq": 17, "ts": 1760700001234, "code": "input_lost", "level": "warning",
          "subject": "i_qroq", "label": "tg-a_audio_1", "params": {"flow": "player_audio"}},
         {"seq": 18, "ts": 1760700003000, "end": 1760700004500, "code": "clip", "level": "warning",
          "subject": "s_in1", "label": "Entrée 1", "n": 12}
       ]}
/state → {…, "event_rev": 42}
```

- **`boot` = identifiant de la SÉQUENCE persistée, pas du processus** (acté le 2026-10-17 avec la
  console 0.4.79) : tiré à la naissance de la séquence et gardé AVEC les événements sur le volume
  d'état. Un redémarrage du plugin resert les événements rechargés sous leur clé d'origine et fait
  sauter `seq` (la console : +10 000) pour qu'une clé servie mais non écrite ne soit jamais réutilisée.
  Un `boot` par processus ferait relire ces événements sous une nouvelle clé : en double. Sans volume,
  chaque démarrage ouvre une nouvelle séquence (événements en mémoire seulement).
- `seq` : entier croissant DANS un `boot` (des sauts sont admis). La paire (`boot`, `seq`) est la clé
  d'unicité côté orchestrateur, par `instance_uuid` (à défaut par vmid) : relire une fenêtre qui
  chevauche, ou tout relire après un redémarrage de l'orchestrateur, ne double rien.
- `ts` : début, ms depuis l'époque (horloge murale) ; `since` filtre sur `ts` (ou sur `end` pour un
  épisode, comme `/clips`).
- Un événement est **immuable une fois publié**. Un épisode (saturation) est publié **à sa clôture**
  avec `ts`, `end`, `n` ; ou en deux événements (`input_lost` puis `input_restored`).
- `code` : vocabulaire fermé du plugin, en snake_case anglais ; libellé traduit par le catalogue du
  plugin, clé `plugin.<type>.event.<code>` (paramètres : `params` + `label`). Sans traduction, la
  page affiche « label · code ».
- `level` : `info` | `warning` | `error`. `params` : valeurs discrètes ou mesures utiles (pas de flot).
- `rev` : change à chaque nouvel événement (l'interface relit quand `event_rev` bouge) ; 24 h
  gardées, sur le volume d'état si possible (comme `/clips`).

Codes de la console (0.4.79) : `started`, `rt_refused` (temps réel refusé), `late_blocks` (épisode,
`n`), `input_lost` / `input_restored` (`subject` = prise), `clip` (épisode).
`native_set` (console 0.4.109) : un réglage fait dans l'INTERFACE NATIVE d'un plugin (plugin `plugin_gui`),
un événement par geste (`subject` = l'élément, `label` = port ou propriété, `params.value` = dernière valeur,
`params.gui` = tag de la session). Seule exception à l'auteur « plugin » : `app/ops_journal.py` l'attribue à
l'utilisateur qui a ouvert la session (`services/audio/native_gui.author`, table `native_gui_sessions`), catégorie
« réglage », endpoint `native-gui` ; tag inconnu : auteur « interface native » sans nom.

Côté orchestrateur : `app/ops_journal_events.Collector` interroge `/events` toutes les 5 s pour
chaque instance dont le plugin déclare `control.journal.events`, via
l'interface `Source` (remplacée par un faux dans le banc). La console 0.4.79 le déclare : elle est
interrogée.

## 7. Stockage, rétention, sorties

Table dédiée `ops_journal` (créée au premier accès), PAS un `kind` de plus dans `alerts` : le volume
d'une console active (des centaines de lignes par heure) chasserait les alertes de leur rétention
en lignes et noierait la page Alertes ; la rétention voulue est en jours ; les filtres veulent des
colonnes et des index. Rétention : réglage **`ops_journal_retention_days`** (défaut 7, borné 1..366),
purge au plus une fois par minute, par tranches de 5 000 lignes, plafond dur 500 000 lignes.
Sorties : réglage **`ops_journal_forward`** (défaut éteint) → `services.alerting.notify(…, "info",
kind="ops")`, donc syslog / SNMP (et tout canal dont le seuil est « info »).

## 8. Macros

Fournisseur du cœur `ops_journal` (`core_plugins.CORE_PROVIDERS`) : états `last_action` /
`last_actor` (projet de la macro), et par instance journalisée `last_action:<instance_uuid>` /
`last_actor:<instance_uuid>` (texte « HH:MM:SS — action » / auteur).

## 9. Limites

- Une commande envoyée directement au conteneur (`:8082`) sans passer par l'orchestrateur n'apparaît
  pas (chemin non authentifié aujourd'hui, cf. `app/deploy.py`).
- Le document persisté peut avoir quelques secondes de retard sur le conteneur (instantané par lot,
  `plugin_persist`) : un réglage discret posé juste avant une application de structure peut
  réapparaître dans le diff.
- Chemins d'écriture vers `:8082` encore NON raccordés : `app/voice_ident.py` (annonce vocale),
  services sous-modules qui postent eux-mêmes (`emberplus`, `skaarhoj`, `atem`) ; IS-12 passe par
  `macros.exec_post` sans auteur → « automatique ».
