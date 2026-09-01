# Contribuer à Bobi.Studio

Guide court et pratique. Pour l'architecture, voir `CLAUDE.md` (source de vérité).

## Convention de nommage FR / EN

Le code mélange français et anglais **par époque**, pas par caprice :

- **Tout nouveau symbole = anglais**, sans exception de fichier. Y compris quand vous
  ajoutez une fonction *dans* un module historiquement francophone : un fichier mixte se
  lit très bien, du français qui continue de se répandre, non. Le dépôt est public et
  s'adresse à un public international — un contributeur ne devrait pas avoir à deviner ce
  que `detruire` veut dire.
  Ce n'est pas un virage : **96 % du code est déjà en anglais** (2519 fonctions contre 86).
- **Le français existant reste.** `surveillance` (143 occurrences), `deployer_script` (72),
  `verrou_vmid` (58), `ajouter_alerte` (26), `detruire_container` (21) : voir la règle d'or
  ci-dessous. Le glossaire plus bas donne l'équivalent anglais quand vous en croisez un.
- **Deux choses ne sont pas des noms de code** et ne bougent donc pas : les **valeurs** d'un
  vocabulaire fermé déjà persistées en base (niveaux d'alerte `info | warning | error`,
  `database.ALERT_KINDS`) — les changer serait une migration, pas un renommage — et les
  libellés d'INTERFACE, qui passent par `i18n/` et restent bilingues.
- **IDs de type de container = libres.** Les identifiants de type récents sont nommés
  librement par l'utilisateur (`2110_io`, …) — pas forcément snake_case, pas forcément
  anglais. Ne les « corrigez » pas.

### Règle d'or : AUCUN rename opportuniste

On ne renomme **jamais** un symbole juste parce qu'il est en français « à l'ancienne ».
Un rename ne se fait **que** lors d'une réécriture complète du module concerné (et alors
il faut suivre toute la chaîne : appels, JS, templates, migrations de type dans `init_db`).
Renommer à la volée casse les diffs, les grep et les migrations DB pour zéro bénéfice.

### Mini-glossaire FR → EN (symboles cœur)

| Français (existant)      | Sens / équivalent EN            |
|--------------------------|---------------------------------|
| `detruire_container`     | destroy container               |
| `redemarrer_container`   | restart container               |
| `surveillance`           | monitoring / watch loop         |
| `ajouter_alerte`         | add alert                       |
| `niveau` (`info`/`warning`/`error`) | alert level          |
| `generer_script`         | render / generate script        |
| `deploiement` / `deploy` | deployment                      |
| `noeud` / `nodes`        | node(s)                         |
| `reglages` / `settings`  | settings                        |

Utilisez cette table comme repère de lecture, pas comme une TODO de renommage.

## Textes UI : toujours via l'i18n

Tout **nouveau texte d'interface** passe par le catalogue i18n : `_("clé")` côté Jinja,
`t("clé")` / `window.t` côté JS (voir `app/i18n.py` et le `js_catalog`). **Jamais** de
chaîne française (ou anglaise) codée en dur dans un template ou dans `static/scripts.js`.
Ajoutez la clé aux catalogues FR **et** EN. Un texte en dur = régression i18n.

## Garde-fous locaux (avant de pousser)

Il n'y a pas de build/lint/test lourd. Deux vérifications gratuites existent et tournent
aussi en CI (`.github/workflows/ci.yml`) :

```bash
# 1) Smoke import — attrape les circularités d'import de app/
./venv/bin/python -c "import app.routes, app.plugins, app.database, app.deploy"
./venv/bin/python -c "import main"

# 2) Scan des plugins — un plugin dont une accolade { } littérale n'est pas doublée
#    dans script.py est silencieusement écarté du registre ; ce script le fait échouer.
./venv/bin/python tests/check_plugins.py
```

Rappel du piège plugins : `plugins/<type>/script.py` est passé dans `str.format()`
(placeholders `{config}` / `{hostname}` / `{plugin_version}`) → **toute** accolade
littérale (dict, set, f-string, commentaire) doit être doublée `{{ }}`.

## Sous-modules git

`services/*` et `plugins/*` sont des **sous-modules** (dépôts privés).

⚠ **Sur une machine neuve, lancez `gh auth setup-git` une fois avant de cloner.**
`.gitmodules` déclare les sous-modules en **https** — c'est ce qui rend le dépôt public
clonable par un visiteur, GitHub exigeant une clé SSH même en lecture seule. Mais les
dépôts de sous-modules, eux, sont **privés** : sans *credential helper* https, un
`git clone --recursive` échoue sur chacun d'eux, avec un message qui laisse croire à un
problème de droits. La commande pose `gh` comme helper et ne répond rien quand elle
réussit. Alternative si vous préférez SSH en local, sans rien changer pour les visiteurs :
`git config --global url."git@github.com:".insteadOf https://github.com/`.

Vérifiez la cohérence des sous-modules avec :

```bash
bash tests/submodules-doctor.sh
```

Il contrôle, pour chaque sous-module : initialisé, sur la branche attendue (`branch =`
dans `.gitmodules`, `main` par défaut), working tree propre, et SHA checkout == gitlink
enregistré dans l'index du parent. Exit non-zéro = au moins un problème.

En **CI**, `actions/checkout` laisse les sous-modules en HEAD détaché : le doctor y tourne
en mode *advisory* (non bloquant). En **local**, il fait autorité — corrigez les désyncs
avant de committer un bump de sous-module.

## Git

- Ne committez / ne poussez **que** sur demande explicite.
- Un bump de sous-module = commit dans le sous-module (sur `main`) **puis** commit du
  gitlink dans le parent. Lancez `tests/submodules-doctor.sh` avant pour éviter de figer
  un SHA hors-branche.
