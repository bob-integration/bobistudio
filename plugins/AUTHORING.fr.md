# Écrire un plugin Bobi.Studio

*[English version](AUTHORING.md)*

Un plugin est un **type de conteneur Docker** piloté par l'orchestrateur. Il tient dans un
dossier `plugins/<type>/`, versionné dans son propre dépôt git (sous-module).

> **Commencez par lire du code, pas ce guide.** `plugins/hello_world/` est la référence
> **exécutable** du contrat : il montre les trois essences en entrée comme en sortie, le mode
> tranche, l'exposition aux macros, la page publique et les métriques — chaque règle y est
> commentée avec le *pourquoi* et ce qui casse **en silence** quand on l'oublie. Ce guide-ci
> donne la carte ; l'exemple donne le terrain.
>
> `tests/verif_plugin_hello_world.py` vérifie cet exemple en intégration continue. Un guide,
> lui, ne s'exécute pas : si vous trouvez ici quelque chose que le code dément, **c'est le code
> qui a raison**, et ce fichier est à corriger.

---

## Structure

```
plugins/<type>/
├── plugin.json       ← manifeste (obligatoire)
├── script.py         ← le plugin, exécuté DANS le conteneur (obligatoire)
├── hooks.py          ← logique exécutée DANS L'ORCHESTRATEUR (optionnel)
├── control.html/.js/.css  ← console de la page du plugin (optionnel)
├── i18n/{fr,en}.json ← libellés de la console (obligatoire si console)
├── help.md           ← article de la page Aide (optionnel)
├── meta.json         ← version + changelog
└── versions/         ← versions archivées, gérées par l'orchestrateur
```

---

## Les cinq règles qui comptent

Le reste du guide est de la référence. Ces cinq-là décident si votre plugin est utilisable en
production, et leur oubli ne produit **aucune erreur** — juste un produit qui ment.

### 1. Mode tranche — obligatoire pour tout nouveau plugin

Lire l'entrée bande par bande (`get_slice`) et publier en commit progressif
(`commit(gi, valid_slices=k)`), au lieu d'attendre la trame entière.

Un plugin qui travaille en image entière ajoute **une trame de latence** à toute chaîne qui le
traverse, et cette dette n'apparaît sur **aucun compteur** : le plugin affiche une cadence
parfaite. Mesuré sur le plugin `scope` : le calcul restant après l'arrivée de la dernière ligne
passe de 5,09 à 1,48 ms, à cadence égale.

**Contrat** : `k` tranches valides ⇔ lignes `[0, k × slice_height)` écrites **sur les trois
plans** (Y, Cb, Cr). Un consommateur qui lit `k` tranches doit trouver les trois plans cohérents
jusqu'à cette ligne, sinon il déchire au bord chroma.

**Condition d'éligibilité** : le traitement doit être **ligne-local** — chaque ligne de sortie ne
dépend que de la même ligne d'entrée et du numéro de ligne. Un flou, un désentrelacement, tout ce
qui regarde les lignes voisines ne l'est pas : restez en image entière, et **écrivez-le dans le
code**. L'entrelacé et la sélection de ligne sont les exceptions documentées.

`slice_mode` va dans `config_schema` en `hidden: true` : le réglage qui compte est le commutateur
global Réglages → Vidéo. Un plugin qui exposerait le sien laisserait un parc réglé au hasard.

### 2. Tout exposer aux macros

Toute fonction ou paramètre non exposé au système de macros est une **capacité morte** : elle
existe, personne ne peut la déclencher, et rien ne le signale.

| Ce que vous ajoutez | Où le déclarer |
|---|---|
| paramètre continu réglable à chaud | `param_tree` (élément → groupe → paramètre typé/borné) |
| action discrète, chargement, rappel | `actions[]`, avec `options_endpoint` pour une liste vivante |
| état lisible en **condition** | publié dans `/state`, déclaré dans `control.read_endpoints` |

Toute cible d'action ou de `param_tree` doit figurer dans `control.endpoints` : le proxy
**refuse** un chemin non déclaré, et la macro « réussit » alors sans rien changer.

### 3. Des métriques qui disent si l'étage fait ce qu'on lui a demandé

`fps` dit seulement que la boucle tourne. Publiez au moins :

- `slice_mode` — la sortie est-elle **réellement** publiée en tranches ;
- `own_latency_ms` — le temps de calcul par trame, donc la marge ;
- `source` — d'où vient l'image.

Et par entrée quand il y en a plusieurs : « rien n'arrive » ne dit pas **laquelle** manque, et
« non câblée » n'est pas « câblée mais muette » — ce sont deux pannes opposées, l'une se répare
au câblage, l'autre chez le producteur.

### 4. La sortie ne dépend pas du producteur

Publiez vos flux **même sans aucune entrée** : fond de couleur, silence audio, ANC régénéré. Un
aval abonné ne doit pas voir sa chaîne s'éteindre parce qu'une source amont est tombée.

C'est aussi ce qui rend un plugin déployable **sans rien câbler** — donc utilisable comme test de
fumée le jour de l'installation, quand justement aucune chaîne n'existe encore.

### 5. Survivre à SIGBUS et aux exceptions

Un producteur qui recrée son flux invalide la projection mémoire des lecteurs. Le piège : la
génération morte reste **lisible** — des grains sont servis, index figé, aucune exception. Sans
gestionnaire `SIGBUS`, le processus meurt sur un signal et Docker le relance en boucle sans que
personne comprenne pourquoi.

Et une exception non rattrapée dans la boucle fait redémarrer le conteneur sans jamais dire
**pourquoi** : l'exploitant lit « redémarré » dans les alertes, rien de plus.

---

## `script.py` — le gabarit

Le script est un **gabarit `str.format()`** avec exactement trois substitutions :

| Substitution | Valeur injectée |
|---|---|
| `{config}` | `repr(params)` — dict Python des paramètres déployés |
| `{hostname}` | nom d'hôte du conteneur |
| `{plugin_version}` | version du plugin au moment du déploiement |

**Règle critique** : toute accolade littérale doit être **doublée**, y compris dans les
commentaires et les f-strings.

```python
# ✅ correct
state = {{"running": False, "fps": 0}}
url = f"http://{{ip}}:{{port}}/path"

# ❌ le plugin disparaît du registre
state = {"running": False}
```

**Garde-fou** : `plugins._scan()` fait un dry-run `.format()` au démarrage. Un plugin dont une
accolade n'est pas doublée est **écarté** — il n'apparaît ni dans la palette, ni dans la nav, et
c'est bien plus long à diagnostiquer qu'une erreur franche. `tests/check_plugins.py` le vérifie
en CI.

⚠ Se rendre ne suffit pas : un script qui se rend peut très bien ne pas **compiler**, et le
conteneur boucle alors en silence. Vérifiez les deux :

```bash
./venv/bin/python - <<'EOF'
import json
m = json.load(open("plugins/mon_plugin/plugin.json"))
r = open("plugins/mon_plugin/script.py", encoding="utf-8").read().format(
    config=repr(dict(m["deploy_defaults"])), hostname="essai", plugin_version=m["version"])
compile(r, "<rendu>", "exec")
print("rendu + compile OK")
EOF
```

### Accès aux paramètres

```python
CONFIG = {config}
HOSTNAME = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

ma_valeur = CONFIG.get("ma_cle") or "defaut"
```

### Les deux ports

| Port | Rôle |
|---|---|
| `8080` | métriques — `GET /` rend le JSON lu par l'orchestrateur |
| `8082` | contrôle — les endpoints déclarés dans `control.endpoints` |

Le port 8080 reçoit aussi les **pousses du service TSL** (tally et libellés vivants) : un serveur
en GET seul rendrait 501 et le tally n'arriverait jamais, sans que rien ne le signale.

---

## `hooks.py` — ce qui tourne dans l'orchestrateur

C'est l'**unique exception** à la règle « aucun code de plugin dans le contrôleur ». Le fichier
est importé et exécuté par l'orchestrateur, avec ses droits : la base, les jetons d'agent, le
réseau de contrôle. Écrivez-le en conséquence — frugal, sans effet de bord, sans blocage : il est
appelé sur le chemin d'un geste d'exploitation.

Un hook qui lève est ignoré avec un avertissement au journal ; il ne bloque pas le déploiement.

### Les hooks reconnus

| Hook | Quand |
|---|---|
| `before_deploy(params, context)` | avant le rendu du script — normaliser, résoudre, injecter |
| `wire_followers(kind, shm, slot, params, ctx)` | câbler une essence en fait suivre d'autres |
| `wire_input` / `unwire_input` | câblage et décâblage d'une entrée |
| `consumes` / `consumed_shms` / `produced_shms` / `produced_flow_count` / `source_shm` | topologie et câblage |
| `topology_ports` | ports affichés sur la page Câbles |
| `tally_targets(params, context)` | ce que le distributeur TSL doit résoudre |
| `ember_sources` / `ember_targets` / `ember_clear_slot` | arbre Ember+ |
| `control_action` / `sync_state` | actions et état côté orchestrateur |

Consultez `plugins/hello_world/hooks.py` : il en implémente trois (`before_deploy`,
`wire_followers`, `tally_targets`) avec, pour chacun, la raison de son existence.

⚠ **Ajouter ou modifier `hooks.py` exige un rechargement du registre** — l'orchestrateur
l'importe une fois, au scan. Sans rechargement, le hook ne se déclenche **jamais** : panne
parfaitement silencieuse. Réglages → Plugins → *Recharger*, ou `POST /api/plugins/reload`.

### Ce qu'un hook a le droit de faire

Contrairement à ce que disait une version précédente de ce guide, un hook **peut** lire la base
et les réglages : c'est même souvent sa raison d'être, puisque le conteneur, lui, n'y a aucun
accès. `hello_world` s'en sert pour résoudre le format vidéo depuis la liste des Réglages, le
fuseau horaire de l'orchestrateur et la langue par défaut du système.

Ce qu'un hook ne doit pas faire : bloquer (appel réseau long, verrou), avoir des effets de bord
durables, ou dépendre d'un état global mutable.

---

## `plugin.json` — le manifeste

Champs obligatoires : `type`, `label`, `version`, `script_template`.

Notables :

| Champ | Rôle |
|---|---|
| `deploy_defaults` | paramètres par défaut à la création |
| `wiring` | `produces` / `consumes` / `mode` — chaque entrée déclare son `state_field` |
| `config_schema` | champs de la palette de déploiement (Tier 1) |
| `control.endpoints` | liste blanche des chemins du proxy — sinon 403 |
| `control.read_endpoints` | sous-ensemble lisible avec le login seul (état, aperçu) |
| `param_tree` / `actions` | surface exposée aux macros |
| `ui.public_page` | autorise un lien public `/p/<jeton>` |
| `nav` | rubrique et route. **Sans `nav`**, ni chip de palette ni lien de nav |
| `help.category` / `help.order` | rangement de l'article d'aide |

---

## Console de contrôle

`control.js` expose `window.MXLPlugins.<type> = { mount, unmount }`.

Quatre règles, chacune pour un défaut déjà constaté :

1. **Les contrôles viennent du catalogue** (`window.MXLControls`), jamais réécrits. Rotatifs,
   interrupteurs, jauges et barres d'outils sont déjà dessinés, accessibles au clavier et
   cohérents entre les pages. L'inventaire vivant est dans Réglages → Contrôles.
   **Avant d'en créer un nouveau : demander.**
2. **La géométrie vient du moteur partagé** `window.MXLLayout` — alignements, égalisation,
   répartition, aimantage, sélection multiple. Quatre copies en avaient divergé.
3. **Une seule fonction construit les URL.** Une console peut être montée derrière un jeton
   public avec une base d'API différente (`ctx.base`) : un `if (public)` oublié vise l'API privée
   et échoue en 401 sans rien expliquer.
4. **Le sondage s'arrête au démontage** (`clearInterval` dans `unmount`), sinon il survit à la
   page et se cumule à chaque montage.

**i18n obligatoire, et il vit DANS le plugin** : les libellés vont dans son propre
`i18n/{fr,en}.json` — préfixe `plugin.<type>.` pour la console, `type.<type>.*` pour la palette
et les ports de câblage. Le français reste la référence des valeurs par défaut.

⚠ **Jamais dans le catalogue du cœur** (`i18n/<lang>.json` à la racine). Deux choses cassent,
les deux en silence :

- Le plugin **voyage sans ses langues**. Installé depuis le Catalogue sur une autre instance,
  la clé n'y existe pas et `plugins._traduit` retombe sur le libellé du **manifeste** — qui est
  en français. Rien ne casse ; l'interface anglaise affiche simplement du français.
- Le cœur **prime** sur le plugin (`i18n._file_catalog_for` finit par `merged.update(core)`).
  Une clé restée au cœur **masque** celle du plugin : l'auteur édite son fichier, recharge, et
  ne voit rien changer.

`tests/check_i18n_scope.py` le vérifie en CI. Il a été écrit après avoir trouvé 444 clés de
palette au cœur pour 19 plugins sur 20 — seul `hello_world`, écrit après que la règle existe,
faisait correctement.

---

## Versions

`meta.json` porte la version et le changelog. Pour publier :

1. modifier le code ;
2. bumper `version` dans `plugin.json` **et** `meta.json`, avec une entrée de changelog ;
3. recharger le registre.

L'archivage sous `versions/<ver>/` est fait par l'orchestrateur à l'installation d'un paquet
(`install_package`) — pas à la main. Les conteneurs déjà déployés continuent de tourner sur leur
version jusqu'au prochain redéploiement : la page Plugins montre le drift.

---

## Sous-modules git

Chaque plugin est un dépôt indépendant.

```bash
cd plugins/mon_plugin
git add . && git commit -m "feat: ma modification"
git push

cd ../..
git add plugins/mon_plugin
git commit -m "chore: bump plugin mon_plugin"
```

Cloner le superprojet avec ses plugins : `git clone --recurse-submodules <url>`.

---

## Créer un plugin

1. **Copiez `plugins/hello_world/`** plutôt que de partir d'une page blanche.
2. Renommez le `type` dans `plugin.json`, videz ce qui ne sert pas.
3. Rechargez le registre — le plugin est scanné et enregistré.
4. Déployez-le sans rien câbler : il doit produire une image.
5. Vérifiez le rendu **et** la compilation du gabarit (encadré plus haut).

---

## Proposer un plugin

Le catalogue ne lit qu'une organisation GitHub de confiance, et ce n'est pas une commodité :
installer un plugin exécute son `hooks.py` **dans l'orchestrateur**. Un plugin tiers n'y apparaît
donc pas tout seul.

Deux voies :

- **Le proposer** — ouvrez une issue sur le dépôt public **avant** d'écrire mille lignes : à quoi
  il sert, quelles essences, quels réglages. S'il est retenu, développez-le dans votre dépôt,
  puis il sera repris dans l'organisation et apparaîtra dans le catalogue de tous.
- **Le distribuer vous-même** — exportez un paquet `.mxlplugin` depuis Réglages → Plugins.
  N'importe qui peut l'installer délibérément, sans rien vous demander.

Dans les deux cas : licence compatible **GPL-3.0** si le plugin doit être hébergé dans
l'organisation, et faites passer les contrôles de conformité avant de proposer.

---

## Sécurité

- `script.py` s'exécute **uniquement dans le conteneur**, jamais dans l'orchestrateur — les
  identifiants du contrôleur ne fuitent pas. `hooks.py` est l'exception, cf. plus haut.
- Les endpoints `/api/containers/<vmid>/plugin/*` sont filtrés par `control.endpoints` : tout
  chemin non déclaré rend 403.
- Les chemins d'assets UI sont assainis pour rester dans le dossier du plugin.
- Un paquet importé est extrait avec protection anti *zip-slip*, son manifeste validé et son
  gabarit rendu à blanc — **aucun code plugin n'est exécuté** à l'import.
