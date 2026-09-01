# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Projet Bobi.Studio

## Architecture
- **Full-Docker** : conteneurs Docker sur des **nœuds** enrôlés (table `nodes`, pilotés par
  `node_driver` + agent-nœud). Le backend LXC/Proxmox a été **retiré** : plus de `proxmox.py`, plus de
  clonage de template ; création via `docker_driver` (MTL) / `docker_compute` (compute/média).
- Orchestrateur Flask central (rôle contrôleur) ; les nœuds exécutent les conteneurs.
- Pipeline vidéo/audio ST 2110 sur le **bus MXL** (SDK MXL, domaine `/dev/shm/mxl`) — production broadcast.

## Types de containers

**Il n'y a pas de liste fixe, et il ne faut pas en écrire une.** Tout type est un **plugin**
(cf. « Système de plugins » plus bas) : le registre les découvre au scan de `plugins/`, et ce
qui est installé varie d'une instance à l'autre — un paquet `.mxlplugin` ou la page Catalogue
en ajoute sans toucher au cœur. La source de vérité est `plugins/*/plugin.json`, jamais ce
fichier.

Ce qui est stable, ce sont les **rubriques** dans lesquelles ils se rangent (champ `nav.section`
du manifeste) : `sources`, `traitements`, `composition`, `medias`, `streams`, `mesure`. Un
plugin **sans `nav`** est rendu et versionné mais n'émet ni chip de palette ni entrée de menu.

Une seule exception mérite d'être nommée ici, parce qu'elle n'est pas un traitement comme les
autres : **`2110_io`**, le moteur ST 2110 bi-rôle (RX + TX) via MTL/DPDK en kernel-bypass. Il
est Docker-only, tourne sur la PF en AF-XDP, et c'est lui qui porte l'entrée/sortie du signal —
les autres plugins se contentent du bus MXL.

> Les VMID ne sont plus fixes (plage allouée dynamiquement ; un vmid = handle local jetable).

## Stack technique
- Python 3.13, Flask, SQLite
- Scripts vidéo : bus MXL (SDK MXL via `script_templates/bobimxl.py`), FFmpeg/GStreamer, numpy
- Agent dans chaque conteneur : port 8081 (deploy/start/stop) ; agent-nœud : lifecycle Docker + host-ops
- Métriques fps : port 8080

## Structure fichiers

Racine `/opt/bobistudio/` : `main.py` (point d'entrée Flask), les deux AMORCES d'installation
(`install.sh` — source locale ; `get.sh` — depuis GitHub, machine vierge ; toutes deux se bornent à
réunir les prérequis puis à lancer `install/install.py`), `venv/`, la DB
`db_bobistudio.db`, puis les dossiers `app/`, `services/`, `templates/`, `static/`, `plugins/`,
`node_agent/`, `i18n/`, `script_templates/`, `install/` (installeur unifié + flux Proxmox
hérité), `old/`. Le **module Python est `app/`** (tous les
`.py` ci-dessous y vivent ; `main.py` reste à la racine et importe `app`).

```
app/                  ← module Python principal (liste non-exhaustive)
  config.py           ← DB_PATH + défauts neutres (valeurs site : config_local.py, cf. Sécurité)
  database.py         ← SQLite (helpers get/set settings, containers, nodes, alerts, projets…)
  node_driver.py      ← pilote de nœud (agent-nœud : host-exec, images, networks, lifecycle)
  docker_driver.py    ← création/destruction conteneurs MTL (bobi-mtl, AF-XDP)
  docker_compute.py   ← création/destruction conteneurs compute/média (macvlan)
  containers.py       ← détruire/redémarrer (délègue aux drivers Docker)
  metrics.py          ← fps, IP
  scripts.py          ← render scripts (normalize_worker_udp_params, normalize_receiver_params)
  plugins.py          ← registre des plugins (scan, render, wiring, coerce_config, hooks)
  deploy.py           ← déploiement via agent
  allocations.py      ← VMIDs/IPs libres
  routes/             ← API REST Flask, PAQUET de 38 modules (il n'y a PLUS de `routes.py` unique).
                        `routes/__init__.py` enregistre le Blueprint ; chaque module porte
                        un domaine (`plugin_registry.py`, `catalogue_api.py`, `roles_api.py`…)
  catalogue.py        ← catalogue des plugins/services publiés (org GitHub de confiance,
                        `config.CATALOGUE_ORG`) — installe un paquet depuis la page Réglages
  routes/roles_api.py ← EMPLACEMENTS (table `production_roles`) : identité FONCTIONNELLE,
                        stable au remplacement — cf. « Identité d'un conteneur » plus bas
  settings.py         ← settings DB (get/set)
  auth.py             ← login/permissions
  backup.py           ← sauvegarde
  monitor.py          ← moniteur WebRTC par utilisateur (encodeur dédié + reaper)
  projects.py         ← projets snapshot/restore
  ptp.py              ← PTP par nœud (ptp4l/phc2sys via host-ops/agent, sampler, événements)
  io2110_flows.py     ← flux composables Sources/Destinations 2110 (modèle RX/TX du moteur)
  core_pool.py        ← allocateur de cœurs CPU par nœud (profil ressources par type)
  placement.py        ← CONSTAT du placement CPU réel (pendant de core_pool : lui calcule AVANT,
                        celui-ci vérifie APRÈS — bande isolée, cpuset posé, threads par cœur)
  host_ops.py         ← host-ops par nœud (ex-template_recreate ; exec via l'agent)
  node_health.py      ← sampler santé des nœuds (CPU/RAM/disque/PTP/GPU/RDMA/capteurs)
  mtl.py              ← préparation hôte MTL (hugepages, DDP, queues)
  gpu.py / gpu_pool.py← détection + allocation GPU NVIDIA (multiview GPU)
  membw.py            ← mesure/alerte bande passante mémoire par nœud (canary memcpy)
  ha.py               ← haute dispo contrôleur actif/standby (cf. HA.md)
  pxe.py / pxe_server.py ← boot réseau UEFI HTTP (install nœud sans USB)
  testplan.py         ← page /tests « Recette » (suivi de campagne + Q/R). OPTIONNELLE et
                        ÉTEINTE sur une installation neuve (réglage + onglet Réglages →
                        Recette) ; la campagne elle-même vit dans `testplan_seed.py`
  i18n.py             ← catalogue FR/EN (`_()` Jinja + js_catalog → window.t)

services/    ← intégrations en SOUS-MODULES git (un dossier = un service, blueprint
               auto-enregistré au boot) : nmos — IS-04/05, plus un socle de CONTRÔLE MS-05-02
               (`ncp.py`) dont le modèle vivant est `modele.py` (compteur de références) et que
               DEUX transports publient : **IS-12** (`is12.py`, WebSocket RFC 6455 sur port dédié
               5010) et **IS-14** (`is14.py`, REST sous `/x-nmos/configuration/`, avec
               sauvegarde/restauration `bulkProperties`). Monitors BCP-008-01/02 des Rx/Tx dans
               `monitors.py` ; modèles AMWA vendorisés dans `nc_models/` (ne pas éditer) ;
               BCP-002-01 grouping **immuable** (figé au registre `nmos_resources`, cf.
               `db_nmos_resource_upsert`) et BCP-002-02 identité d'asset via `nmos.asset_info()`
               — source UNIQUE partagée avec le `NcDeviceManager`. Bancs (à lancer à la main) :
               `is12_bench.py`, `is14_bench.py`, `bench_telemetrie.py`, `bench_bcp002.py`.
               emberplus, tsl, atem, skaarhoj,
               sap — SAP/SDP (RFC 2974) : annonce de nos senders + découverte des flux tiers,
               le SEUL mécanisme que partagent AES67 / Ravenna / Dante-en-mode-AES67 (aucun ne
               parle NMOS, et aucun ne peut s'abonner à ce qui n'est pas ANNONCÉ). Confronte les
               flux découverts au format que le moteur sait recevoir ET au domaine/GM PTP de nos
               propres annonces. Passe par la pile noyau → aveugle sur un port lié à vfio-pci.
               snmp — agent SNMPv3 `authPriv` en LECTURE SEULE (`pysnmp`), servi par le
               contrôleur ACTIF seul (la MIB demandée est une MIB de CLUSTER : la donnée agrégée
               est déjà en RAM ici). Les TRAPS sont un autre chemin : canal de `alerting` qui
               délègue au service. PEN IANA 66633 (attribué le 2026-08-26) → UN SEUL littéral d'OID racine dans
               tout le produit (`services/snmp/mib.py:PEN`) ; l'engine ID l'embarque, donc il est
               régénéré à l'attribution. Spec : `docs/chantiers/SNMP.md` (interne),
               webrtc_gateway (passerelle MediaMTX), rdma (réplication MXL inter-nœuds),
               files, media_manager, storage. Import : `from services import nmos` etc.
node_agent/  ← agent-nœud (agent.py, HTTP :9100 par défaut, token par nœud) + install-node.sh + iso/
               (contrat documenté dans NODE_AGENT.md)

templates/   ← HTML Jinja2 : layout.html (parent), home.html, cards.html, forms.html,
               containers.html, settings.html, cables.html, login.html, monitoring.html,
               projects.html, aide.html, public_watch.html, deploy_palette.html, tests.html,
               i18n.html, labels.html, setup*.html,
               plugin_section.html (template générique pour TOUTES les rubriques plugin :
               Traitements, Médias, Sources, Streams, Destinations — chaque route délègue
               à _render_plugin_section(section_id) dans routes/plugin_registry.py), … (non exhaustif)
static/      ← scripts.js (tout le JS global), css/ (base.css, nav.css, theme-*.css), uploads/
plugins/     ← UN PLUGIN PAR TYPE DE CONTAINER (manifeste + script + UI, sous-modules git),
               tous runtime=docker. N'ÉNUMÉREZ PAS les types ici : ils se découvrent au scan et
               varient d'une instance à l'autre (`ls plugins/*/plugin.json` fait foi). Les
               dossiers `_*` ne sont PAS des plugins mais des runtimes partagés (images Docker
               et bancs).
               (NB : receiver_2110/sender_2110, les ex-plugins LXC, ont été retirés — supersédés
               par 2110_io ; ServeurStream a été déplacé en service, cf. services/webrtc_gateway)
script_templates/ ← CODE EXÉCUTÉ DANS LE CONTENEUR, pas dans l'orchestrateur. Trois familles,
               et c'est la famille qui compte, pas la liste :
               (1) les deux fondations — `agent.py` (agent par-conteneur, contrat :8081, sert le
                   HTTPS et valide le CN du contrôleur) et `bobimxl.py` (binding du SDK MXL :
                   lecteurs/écrivains, mode tranche, générations). TOUT plugin vidéo importe le
                   second ; `deploy.py` le pousse dans le conteneur au déploiement ;
               (2) des noyaux C/CUDA compilés à la volée là où Python ne tient pas le budget
                   (composition du mur, copie non-temporelle, scope, conversion v210) ;
               (3) des bancs et auto-tests lancés À LA MAIN dans un conteneur. Aucun code ne les
                   appelle — donc rien ne les empêche de se périmer : à vérifier avant de s'y fier.
```

## Où vit la documentation (rangé 2026-08-05)

**Racine = doc PUBLIQUE uniquement**, plus les deux fichiers de travail. Tout le reste est
sous `docs/`. Un nouveau document se range selon son PUBLIC, pas selon son sujet :

| Emplacement | Contenu | Se maintient ? |
|---|---|---|
| racine | `README`, `INSTALL`, `INFRASTRUCTURE`, `HA`, `NODE_AGENT`, `THIRD-PARTY-NOTICES`, `CHANGELOG`, `CONTRIBUTING` | oui — c'est ce que voit un visiteur GitHub |
| racine | `CLAUDE.md` (ce fichier), `TODO.md` | oui — mémoire de travail |
| `docs/design/` | `DESIGN` (charte UI), `PRODUCT` (doctrine UX), `CONTEXT` (contexte métier) | oui |
| `docs/reference/` | ce qui FAIT FOI sur un sujet (formats TX, horloge PTP, interop MXL, projets, témoins, MIB…) | **oui — ces documents font autorité** |
| `docs/chantiers/` | journaux de chantier datés | non — on n'y revient pas |
| `old/` | clos ou supersédé : analyses closes, modes opératoires exécutés, code pré-split | non |

⚠ **Ce fichier décrit le dépôt de TRAVAIL.** Le dépôt public en est une dérivation filtrée
(`tools/publier.sh`, interne) : `TODO.md`, `old/`, quelques journaux de chantier et la
campagne de recette (`app/testplan_seed.py`) n'y sont pas. Si vous lisez ceci depuis le
dépôt public, ces chemins-là n'existent pas chez vous — tout le reste, si.

Trois règles qui vont avec :
- **Ne recopiez jamais ici la liste des fichiers** d'un de ces dossiers : `ls docs/reference/`
  et `ls docs/chantiers/` font foi. L'énumération qui figurait dans ce tableau nommait 7
  chantiers sur 16 et oubliait un document de référence — une liste tenue à la main se périme.
- **`INSTALL.md`, `INFRASTRUCTURE.md`, `HA.md`, `NODE_AGENT.md` et `THIRD-PARTY-NOTICES.md`
  sont rendus dans la page Aide** via `/api/doc/<name>` (liste blanche `_DOCS_RACINE` dans
  `app/routes/pages.py`), sur le modèle de `/api/changelog`. Source unique : le fichier
  versionné. Ne jamais recopier leur contenu dans `templates/aide.html`.
- **`app/builder.py` embarque par LISTE BLANCHE** (`CORE_DIRS` / `CORE_FILES`). Un document
  rendu par l'aide et absent de cette liste donne un 404 sur toute instance installée, alors
  que tout marche en dev. `docs/` est dans `CORE_DIRS` ; les .md publics sont dans `CORE_FILES`.

> **Convention de nommage** : **anglais** pour tout nouveau symbole — cf. la section
> « Convention de nommage » plus bas. Les **ids de type** sont nommés librement
> (`2110_io`) — pas forcément snake_case.

## Système de plugins (TOUS les types de containers)

Depuis 2026-05, **chaque type de container est un plugin** dans `plugins/<type>/`
(`plugin.json` + `script.py` + UI optionnelle). Registre : `app/plugins.py`
(`is_plugin`, `get`, `render_script`, `derive_wiring`, `sections`, `coerce_config`).
`scripts.generer_script` ne fait plus que déléguer : `if is_plugin(t): render_script(...)`.

- **`script.py`** = template `str.format` avec **3 placeholders seulement** : `{config}`
  (= `repr(params)`), `{hostname}`, `{plugin_version}`. **Toute accolade littérale doit
  être doublée `{{ }}`** (corps, commentaires, f-strings). Garde-fou : `plugins._scan()`
  fait un dry-run `.format` et **skip** un plugin dont une accolade n'est pas doublée.
- **`plugin.json`** : `type`, `label`, `version`, `script_template`, `deploy_defaults`,
  `wiring` (`produces`/`consumes`/`mode`), `control.endpoints`, `config_schema` (Tier 1),
  `nav` (optionnel). **Sans `nav`** → le plugin est rendu/versionné mais n'émet **ni chip
  de palette ni lien nav** (réservé aux types à palette/déploiement bespoke).
  Le contrôle live passe par le proxy générique `/api/containers/<vmid>/plugin/<path>`
  (`plugin_proxy`, valide contre `control.endpoints`) ; les GET listés dans
  `control.read_endpoints` (état, preview) sont accessibles avec le login seul, le
  reste exige la permission `containers.deploy`.
- **Pattern HYBRIDE** (types cœur migrés) : le plugin porte le **script** + le manifeste ;
  mais la logique non déclarative reste **bespoke côté orchestrateur** (renommée au type
  du plugin) : hook deploy (`deploy.py` : normalisation, autoalloc multicast, compteurs
  NMOS), intégration **NMOS** (`services/nmos/`, test `dc_type == "<type>"`), Ember+, topologie/
  câblage (`routes/`), et les **sections de palette riches** (slots de simu receiver,
  vidéo/audios du sender) + pages dédiées (composer multiview, T-bar mixer, page Streams).
  **Aucun code de plugin n'est exécuté in-process** (sécurité : les identifiants du contrôleur
  ne fuitent pas) — le code plugin ne tourne que dans le conteneur via l'agent.
- **Renommage d'un type** = chaîne de type dans `deploy_config` (migration DB idempotente
  dans `init_db`, motif répété) + tous les `== "<type>"` (deploy/nmos/routes/projects/
  monitor/emberplus + JS/templates). Les **UUID NMOS sont keyés sur le VMID** (pas le type)
  → un renommage ne casse pas les enregistrements IS-04/05. Ne PAS toucher les types de
  **ressource** IS-04 (`_register_one(..., "receiver"/"sender", ...)`).
- **Versioning** : `plugin.json:version` → `params.plugin_version` (persisté) + `:8080`.
- **Mode tranche (OBLIGATOIRE pour tout NOUVEAU plugin)** : lire l'entrée par tranches
  (`head_index` + `get_slice`, repli `get_latest` si la source n'est pas tranchée) et publier la
  sortie en commit progressif (`slice_height` au flowDef + `commit(gi, valid_slices=k)`). Contrat :
  k tranches valides ⇔ lignes `[0, k·slice_height)` écrites **sur les trois plans**. `slice_mode`
  va dans `config_schema` en `hidden: true` — le réglage qui compte est le switch global
  Réglages → Vidéo. Exceptions à écrire dans le code : **entrelacé** et **sélection de ligne**
  restent en image entière. Un plugin whole-frame ajoute UNE TRAME de latence à toute chaîne qui
  le traverse, et cette dette n'apparaît sur aucun compteur — le plugin affiche une cadence
  parfaite. Modèle : `color_corrector` (entrée) et `scope` (accumulation par bandes).
- **Page publique (`ui.public_page`)** : mécanisme d'ORCHESTRATEUR (`/p/<jeton>`, relais
  lecture seule borné par `control.read_endpoints`, fragments d'UI servis par le jeton, bandeau
  rétractable avec les réglages `brand_*`). Le plugin n'a rien à écrire d'autre que le CONTRAT :
  `mount(el, vmid, ctx)` doit honorer `ctx.base` — **toutes** les URL passent par une fonction
  unique, jamais un `if` par appel — et déclarer `ui.public_page: true`. L'orchestrateur REFUSE
  de créer un lien pour un plugin qui ne le déclare pas, plutôt que d'en livrer un qui échoue en
  401. Les appels hors proxy de plugin (`/api/containers/<vmid>/…`) doivent être SAUTÉS en mode
  public, pas tentés. Modèle : `scope`.
- **Exposer aux macros (OBLIGATOIRE)** : toute nouvelle fonction/paramètre d'un plugin DOIT
  être pilotable par le système de macros/déclencheurs — sinon la capacité est « morte »
  (échec silencieux fonctionnel). Paramètre continu réglable à chaud → `param_tree`
  (arbre élément→groupe→paramètre typé/borné ; bornes déclarées ou via `/state.caps` ;
  résolveur `app/macros.py:param_tree()`). Action discrète / chargement de fichier /
  pilotage d'état (start-stop, charger, changer un texte, rappel) → `actions[]` (avec
  `options_endpoint` pour listes vivantes, `body` pour champs fixes). État lisible en
  condition → publier dans `/state` + `control.read_endpoints`. Modèles : `split`
  (param_tree + caps live), `mixer` (bornes déclarées + surcharge `path`), `multiview`
  (params par élément + actions horloge/texte).

## Problèmes connus résolus
- Bus error streamer (ex-worker UDP) → signal SIGBUS intercepté, reconnexion auto
- Bus MXL recréé au redémarrage d'un producteur → les consommateurs gèrent la reconnexion (SIGBUS)

## Lancement

Pas de build. Un seul venv à `./venv`.

Il y a en revanche **48 tests hors ligne** et une **CI** (`.github/workflows/ci.yml`)
qui les exécute tous, en bloc et de façon bloquante — plus le smoke import, le scan des
plugins, la portée des clés i18n et la conformité du plugin d'exemple. `submodules-doctor`
et `pyflakes` sont *advisory*.

**Trois dossiers, une question chacun** :

| | |
|---|---|
| `tests/` | « est-ce correct ? » — aucun effet de bord, base jetable, aucun réseau. Tourne en CI |
| `tools/` | FAIT un travail : `build_dist`, `create_admin`, `ca-init`, `publier*` |
| `tools/bancs/` | exige un système VIVANT (nœud, orchestrateur, flux MXL) — ne peut pas tourner en CI |

Un fichier qui rend un verdict va dans `tests/`, sans exception : la suite a existé sans
tourner nulle part, et cinq de ses fichiers ont échoué des semaines durant sans que
personne le sache. Avant de pousser : `./venv/bin/python tests/<le vôtre>.py`, et
`pyflakes` après tout retrait de code — il attrape les orphelins.

```bash
./venv/bin/python main.py     # Flask sur 0.0.0.0:5000 + thread surveillance()
```

## Convention de nommage : **anglais**

**Tout nouveau symbole s'écrit en anglais** — fonctions, variables, classes, colonnes,
clés de réglage, routes. Le dépôt est public et s'adresse à un public international
(AMWA/NMOS, broadcast) ; un contributeur ne devrait pas avoir à deviner ce que
`detruire` veut dire.

Ce n'est pas un virage : **96 % du code est déjà en anglais** (2519 fonctions contre 86).
La règle « français partout » décrivait l'intention des premiers mois, pas le code — elle
se contredisait d'ailleurs avec la dérogation notée plus haut.

**Les symboles français existants RESTENT.** Les renommer coûterait cher pour rien :
`surveillance` (143 occurrences), `deployer_script` (72), `verrou_vmid` (58),
`ajouter_alerte` (26), `detruire_container` (21) — et un renommage à l'aveugle casse
silencieusement les appels dans les gabarits Jinja et le JS, que pyflakes ne voit pas.
On les renomme seulement quand on réécrit le fichier de toute façon.

Deux choses ne sont PAS des noms de code et ne bougent pas : les **valeurs** d'un
vocabulaire fermé déjà persistées en base (niveaux d'alerte `info|warning|error`,
`database.ALERT_KINDS`), et les **ids de type** de plugin, nommés librement (`2110_io`).

L'INTERFACE reste bilingue et passe par `i18n/` — un libellé n'est jamais écrit en dur.

## Contrat HTTP de l'agent par-conteneur

L'agent vit dans l'image runtime (hors de ce repo). Chaque conteneur managé est supposé exposer :

- `POST :8081/deploy` `{path, content}` — écrit le script sur disque
- `POST :8081/start` / `POST :8081/stop` — contrôle le script
- `GET  :8081/status` → `{running: bool}` — liveness agent + état script
- `GET  :8080/`      → `{fps, frame_index}` — métriques émises par le script déployé lui-même

Les `plugins/<type>/script.py` embarquent inline le HTTPServer `:8080`. Toute modification du contrat impacte à la fois `deploy.py` / `metrics.py` / `routes/` **et** les scripts de plugin — bouger les deux côtés ensemble.

## Boucle de surveillance

`main.py:surveillance()` tourne dans un thread daemon, poll toutes les `CHECK_INTERVAL = 5s` :
- Container dont le statut Docker (`docker inspect`) n'est pas `running` → `redemarrer_container` + alerte
- `metrics.rafraichir_metrics` ajoute un pseudo-statut `script_stopped` quand le conteneur est up mais l'agent dit `running:false`

Ajouter un nouveau statut implique d'ajouter le badge CSS correspondant dans `static/css/base.css` (le dashboard, rendu via `templates/layout.html`, se rafraîchit toutes les 5s en JS via `/api/containers` + `/api/alerts`).

## Modèle de threading

Les routes Flask retournent immédiatement et dispatchent dans `threading.Thread`. Les opérations
de cycle de vie sont **sérialisées par VMID** depuis 2026-07-04 : `app/vmlocks.py` (`verrou_vmid`,
`verrou_vmids`, `est_verrouille`), un RLock par vmid pris DANS le thread de fond — 36 sites d'appel
(destroy, restart, deploy, resize, recreate, wire/unwire, compose, migrate, tx-push, config…).
Des vmid différents restent parallèles.

⚠ **Deux limites à connaître, toutes deux assumées** :
- Le registre est **en mémoire de processus**. Un déploiement lancé hors du processus Flask (script
  d'administration, ou le CONTRÔLEUR DE SECOURS en HA) lui est invisible. Ne jamais se fier au
  verrou seul : re-vérifier **l'état observé** (l'agent) sous verrou, jamais ce que la base prétend.
- **Dégradation sur timeout** : verrou non acquis en 120 s → alerte + exécution quand même
  (best-effort). Laisser tomber un destroy/deploy silencieusement serait pire.

SQLite ouvre une connexion fraîche par appel (`get_db()`), donc les writes depuis les threads de
fond sont OK, mais il n'y a **pas de transaction entre helpers** — c'est la race qui reste.

## DB : `init_db` + migrations de type

`init_db()` crée désormais `containers` **et** `alerts` (table durcie en 2026-05 ; auparavant
`alerts` n'était jamais créée et casser la DB cassait silencieusement les alertes). Fichier DB :
`db_bobistudio.db` (`database.DB_PATH`). Schéma alerts : `alerts(id INTEGER PK AUTOINCREMENT,
message TEXT, niveau TEXT DEFAULT 'info', timestamp TEXT, vmid INTEGER, node_id INTEGER,
kind TEXT)`.

**Contexte structuré des alertes** (2026-07) : `vmid`/`node_id`/`kind` sont OPTIONNELS (NULL) et
posés par le producteur — `db_add_alert(message, niveau="info", vmid=None, node_id=None, kind=None)`
reste rétrocompatible avec les appels à deux arguments. `kind` = **vocabulaire FERMÉ**
`database.ALERT_KINDS` (une valeur hors liste est refusée, stockée NULL + log). Les consommateurs
(`services/alerting/context.py`, `_alerteVmid` dans `static/scripts.js`) **préfèrent la colonne** et
ne retombent sur la déduction textuelle que si elle est nulle — ce repli reste nécessaire pour les
lignes écrites avant la migration, et il échoue là où la colonne réussit (conteneur DÉTRUIT, plus de
hostname en base). Filtres : `db_get_alerts(vmid=/node_id=/kind=)` et `/api/alerts?vmid=&node_id=&kind=`.
Pas de reprise rétroactive (décision) : la rétention à 1000 lignes renouvelle le parc en ~2 jours.

`init_db()` contient aussi les **migrations de renommage de type** (idempotentes, au boot) :
`worker_udp→streamer`, `worker_2110_sender→sender_2110`, `receiver→receiver_2110`,
`ServeurStream→webrtc_gateway` (service consolidé), `receiver_2110_mtl→2110_io`
(scan `deploy_config LIKE '%…%'` + égalité stricte sur `type`).
Ajouter une migration ici quand un type est renommé.

## Encodage source/shm_out (couplage front ↔ back)

`deploy.py` dénormalise les params de script dans les colonnes `source` / `shm_out` selon le type :
- `multiview` → `source = "{cols}x{rows}"`, `shm_out = shm_out`
- `streamer` → `source = shm_name (+ audio_shm)`, `shm_out = liste des destinations (`udp h:p · srt h:p · webrtc:path`)`

**Encodeur (deploy) et décodeur (front) doivent rester synchros.**

### streamer (ex-worker_udp) : schéma multi-destinations normalisé

`streamer` n'a plus une seule sortie UDP. Son `deploy_config.params` suit désormais
`{shm_name, audio_shm, video:{codec(h264|h265),bitrate,preset,gop,width,height,fps},
audio:{enabled,codec,bitrate}, destinations:[{type:udp|srt|webrtc, …}]}`. La fonction
`scripts.normalize_worker_udp_params()` (nom conservé) **migre l'ancien schéma plat**
(`dest_ip`/`dest_port`/`bitrate`/…) et est appelée par le **hook deploy** (`deploy.py`) avant le
rendu plugin → les vieux containers tournent sans resave et sont migrés au prochain déploiement.
La palette de la page Containers émet encore l'ancien schéma plat (toléré par le normaliseur) ; **la page Streams
(`/streams`) est l'éditeur riche** (encodage + destinations). Un seul encode ffmpeg → fan-out
via le muxer `tee` (UDP+SRT en mpegts, +branche `rtsp`/`whip` pour WebRTC). `onfail=ignore`
sur chaque branche : une destination morte ne tue pas les autres.

**Audio** : entrée 8ch (L24/48k) câblée via la page Câbles (`streamer` a un port d'entrée
audio en plus de la vidéo ; `_apply_wire` `kind=audio` → `params.audio_shm`). `audio.tracks` =
liste de pistes, chaque piste = 1 canal (mono) ou 2 (stéréo) parmi les 8 (indices 0-based).
Activé par défaut (1 piste stéréo ch0-1). Mapping via `-filter_complex` (`pan`/`asplit`), **codec
par destination** : **AAC** (toutes les pistes) pour UDP/SRT, **Opus** (1ʳᵉ piste seulement) pour
WebRTC — encode vidéo unique, routage par `tee select=<indices>`. L'`audio_feeder` ouvre le fifo
immédiatement et écrit du **silence** quand pas de frame audio fraîche (ne jamais bloquer la vidéo).

**Auto-détection résolution** :
si `video.width`/`height` valent `0`, l'encodeur déduit WxH de la taille du shm (YUV420, ring=10,
ratio 16:9) — utilisé par le monitoring pour prévisualiser une source de résolution inconnue.

### Monitoring WebRTC par utilisateur (`app/monitor.py`)

Panneau latéral global (dans `templates/layout.html`, objet JS `window.MXLMonitor`) présent sur toutes les pages, qui embarque un flux WebRTC. **Un encodeur monitor par utilisateur** : un container `streamer` (hostname `monitor-u<uid>`, où `uid = current_user()["id"]`) qui pousse un path WebRTC fixe `monitor-u<uid>` vers la passerelle. Créé à la demande (1er usage, streamé). Re-pointé sur n'importe quel shm via les boutons « 📺 Monitoring » des pages productrices (`MXLMonitor.send(shm,label)` ou `MXLMonitor.monitorVmid(vmid)` qui lit les `produces[]` de `/api/home/summary`). **Reaper** (`start_reaper`, lancé depuis `main.py`) : coupe le script (`:8081/stop`) après 10 min sans heartbeat ; réactivé (`:8081/start`) à la réouverture. Routes `/api/monitor/{status,create,source,activate,heartbeat}` (`@require_login`). Le path WebRTC étant constant par utilisateur, changer de source ne recharge pas l'`<iframe>` (anti-reconnexion). Prérequis : passerelle WebRTC déployée + activée.

### Passerelle WebRTC (MediaMTX) — service `webrtc_gateway`

WebRTC = un container dédié exécutant **MediaMTX** (ingest RTSP/WHIP + playout WHEP + page
de lecture embarquable). C'est le **service `services/webrtc_gateway/`** (sous-module :
`script.py` + `manifest.json` + `settings_tab.html` ; l'ex-plugin `ServeurStream` a été
consolidé là, migration de type `ServeurStream→webrtc_gateway` dans `init_db`) : déployé/
configuré via **Réglages → WebRTC** (bouton « Déployer la passerelle »), pas via la palette.
Settings `webrtc_*`. Le script **télécharge le binaire mediamtx au 1er lancement** s'il est
absent (accès Internet sortant requis ; erreur remontée sur `:8080` sinon). Les `streamer`
poussent vers la passerelle (`deploy._resolve_webrtc_destinations` injecte `ingest_url`/`whep_url`/
`embed_url` depuis les settings `webrtc_*`), et la page Streams embarque la preview WHEP en `<iframe>`.

## Identité d'un conteneur : trois barreaux

1. **`vmid`** — handle local jetable (réattribué, change au recreate). Interne : chemins d'API,
   verrous, logs. **Jamais** une adresse exposée à l'extérieur.
2. **`containers.instance_uuid`** — identité d'INSTANCE, survit recreate/restore/import de projet.
   Utilisée par NMOS (`bind_instance_uuid` + slot), les vues de projet, les macros
   (`macros._resolve_vmid`). Ne survit PAS au remplacement (autre conteneur pour la même fonction).
3. **Emplacement** (table `production_roles`, `app/routes/roles_api.py`) — identité **FONCTIONNELLE**
   (« MULTIVIEW RÉGIE 1 »). `num` AUTOINCREMENT **jamais réattribué** (supprimer laisse un trou :
   un numéro recyclé re-pointerait un pupitre sur autre chose), `key` immuable, `label` renommable,
   `instance_uuid` = conteneur servant (NULL = hors ligne). **Créé EXPLICITEMENT par un humain**
   (Réglages → Ember+ → Emplacements) : un emplacement est une position de production, donc une
   décision. Le semage automatique au premier déploiement a été RETIRÉ le 2026-08-30 (`b74dadb`) —
   il confondait ce 3e barreau avec le 2e et produisait comme libellé le hostname, précisément ce
   qu'un emplacement ne doit pas être. Mesuré alors : 282 emplacements semés pour 8 servis, dont
   173 par les shards éphémères du tissu compositeur — et `num` n'étant jamais réattribué, la table
   ne pouvait que croître. La colonne `containers.role_seeded` subsiste en base : vestige, plus
   personne ne l'écrit.

**C'est le barreau 3 que tout système de contrôle externe doit adresser** : Ember+ le fait
(arbre `emplacements.<num>`, cf. `services/emberplus`) ; TSL, le pont ATEM et les macros publiées
visent encore le vmid/l'uuid — à migrer sur les emplacements, pas à re-dériver une identité.

## Réseau des conteneurs (full-Docker)

Trois plans : (1) **ST 2110** (NIC dédiée, AF-XDP/DPDK pour `2110_io`) ; (2) **conteneurs partagés**
via réseau **macvlan** (une IP par conteneur, subnet du cluster — l'orchestrateur joint les conteneurs
en direct, l'agent ne gère que le lifecycle) ; (3) **contrôle** (IP statique du nœud, posée au preseed).
`net_mode` (`dhcp`/`static`, Réglages) pilote l'allocation d'IP macvlan (`allocations.py`). La
passerelle macvlan est auto-dérivée de la route par défaut du nœud (≠ l'IP de l'hôte parent).
Le pool SR-IOV (`nic_pool.py`) a été **retiré** : le moteur `2110_io` tourne sur la PF en AF-XDP.
L'allocation **multicast** est centralisée (tables `mcast_ranges`/`mcast_allocations`, réservation
atomique dans `allocations.py`). Aucune valeur de site n'est codée en dur (cf. `config_local.py`).

## Piège des scripts de plugin `str.format`

Les fichiers `plugins/<type>/script.py` sont passés dans `str.format()` par `plugins.render_script`
(placeholders `{config}`/`{hostname}`/`{plugin_version}`). Tout `{` `}` littéral dans le code (dicts,
sets, f-strings, **commentaires**) doit être doublé `{{` `}}`. Garde-fou : `plugins._scan()` fait un
dry-run `.format` au démarrage et **skip** (log) un plugin dont une accolade n'est pas doublée — donc
un plugin cassé n'apparaît simplement pas dans le registre plutôt que de planter au déploiement.

## Sécurité

- Les **valeurs propres au site** (hôtes/SSH, tokens d'agent-nœud, secrets) vivent dans
  **`config_local.py`** à la racine (non versionné, `.gitignore`). `app/config.py` ne porte que des
  défauts neutres, surchargés au boot. Ne rien logger ni copier ailleurs.
- Les appels host-ops/agent-nœud passent par `node_driver` (token HTTP par nœud), plus de client
  API Proxmox. TLS interne désactivé là où c'est intentionnel (réseau interne).
- **Token de l'agent par-conteneur (`:8081`)** : `containers.agent_token` (aléatoire par conteneur)
  **injecté en `MXL_AGENT_TOKEN` au `docker run`** — `docker_compute` (chemin agent-nœud ET chemin
  ssh legacy) + `docker_driver`. C'est CETTE injection qui rend l'auth effective : l'agent n'exige
  l'en-tête `X-MXL-Agent-Token` que si la variable est posée. **Tout appel `:8081` DOIT joindre
  `deploy.agent_headers(vmid)`** — un seul chemin sans en-tête = un conteneur impilotable après
  redéploiement. Repli automatique sur le token DÉRIVÉ (`HMAC(flask_secret_key, "agent:<vmid>")`)
  pour les conteneurs d'avant la migration → aucun redéploiement forcé, bascule au fil de l'eau.
  Tant qu'il reste des `agent_auth == "derived"` (champ exposé par conteneur ; agrégat
  `database.db_agent_token_etat()`), `flask_secret_key` **n'est pas rotable**. Échappatoire :
  réglage `agent_token_inject=0` (aucune injection → agent ouvert, comportement historique).
  Distinct du token de l'agent-NŒUD (`:9100`, `nodes.agent_token`) et du mTLS (premier facteur).

## Référence

`old/orchestrateur.py` est le monolithe pré-split : utile pour retrouver une intention
d'origine, importé nulle part. **Absent du dépôt public** (cf. l'avertissement de la section
« Où vit la documentation »).
