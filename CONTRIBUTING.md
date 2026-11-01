# Contribuer à Bobi.Studio

Guide court et pratique. Pour l'architecture, voir `CLAUDE.md` (source de vérité).

## Convention de nommage FR / EN

Le code mélange français et anglais **par époque**, pas par caprice :

- **Tout nouveau symbole = anglais**, sans exception de fichier. Y compris quand vous
  ajoutez une fonction *dans* un module historiquement francophone : un fichier mixte se
  lit très bien, du français qui continue de se répandre, non. Le dépôt est public et
  s'adresse à un public international — un contributeur ne devrait pas avoir à deviner ce
  que `detruire` veut dire.
  Ce n'est pas un virage : **l'essentiel du code est déjà en anglais**.
- **Le français existant reste.** `surveillance`, `deployer_script`, `verrou_vmid`,
  `ajouter_alerte`, `detruire_container`, très employés partout : voir la règle d'or
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
il faut suivre toute la chaîne : appels, JS, templates, et les données déjà persistées en base).
Renommer à la volée casse les diffs, les grep et les données persistées pour zéro bénéfice.

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

**Ce que le conteneur renvoie à l'écran compte aussi** : une erreur, une option de liste ou un état
qu'un plugin renvoie en JSON et que l'interface affiche se publie en **code stable + valeurs**, et la
phrase vit dans l'`i18n/` du plugin (même doctrine que les alertes : `app/i18n.py:rendre_alerte`). Une
phrase française dans une réponse JSON est un texte en dur.

## Journaux techniques : en anglais

Les journaux (logs : `log.info(...)`, `log(...)` d'un script de plugin, `print` de diagnostic) s'écrivent
**en anglais** — ils servent au diagnostic, comme le code, et ne sont pas une interface. Décidé le
2026-10-15. Les journaux français existants passent en anglais quand on retouche leur fichier (stock
recensé dans `TODO.md`). Les **alertes**, elles, sont une interface : clé i18n + paramètres.

## Garde-fous locaux (avant de pousser)

Il n'y a pas de build, mais il y a une **suite de tests hors ligne** et une CI
(`.github/workflows/ci.yml`) qui l'exécute **en bloc et de façon bloquante** : tous les
`tests/*.py` et `tests/*.js`, plus le smoke import, le scan des plugins, la portée des clés i18n,
le contrôle des adresses de site et la conformité du plugin d'exemple. `submodules-doctor` et
`pyflakes` y sont *advisory*.

**Trois dossiers, une question chacun :**

| | |
|---|---|
| `tests/` | « est-ce correct ? » — aucun effet de bord, base jetable, aucun réseau. Tourne en CI |
| `tools/` | FAIT un travail : `build_dist`, `create_admin`, `ca-init`, `publier*` |
| `tools/bancs/` | exige un système VIVANT (nœud, orchestrateur, flux MXL) — ne peut pas tourner en CI |

Un fichier qui rend un verdict va dans `tests/`, sans exception : un test hors de ce dossier
ne tourne nulle part et finit par échouer sans que personne le sache.

Avant de pousser, lancez le test qui couvre votre changement, et `pyflakes` après tout retrait
de code (il attrape les symboles devenus orphelins) :

```bash
./venv/bin/python tests/<le vôtre>.py
./venv/bin/python -m pyflakes <fichiers modifiés>

# Smoke import — attrape les circularités d'import de app/
./venv/bin/python -c "import app.routes, app.plugins, app.database, app.deploy"

# Scan des plugins — un plugin dont une accolade { } littérale n'est pas doublée
# dans script.py est silencieusement écarté du registre ; ce script le fait échouer.
./venv/bin/python tests/check_plugins.py
```

`ls tests/` fait foi sur ce qui existe. Ne lancez pas deux suites complètes en même temps sur
la même machine : certains tests partagent des ressources locales.

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
