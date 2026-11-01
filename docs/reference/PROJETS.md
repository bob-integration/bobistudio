# docs/reference/PROJETS.md — Le projet comme espace de travail

> Document de design (2026-07-05), remis à jour le 2026-11-01 après le chantier des ports.
> Statut : **chantiers 1 à 6 livrés** (cf. les « État d'avancement » en fin de document) ; le §6
> (`project_control_map`) a été SUPPLANTÉ par les emplacements, pas réalisé. Les références de code
> citent des NOMS de fonction : les numéros de ligne se périment.

## 1. Vision

Un **projet** cesse d'être un simple snapshot pour devenir un **espace de travail** :

- Un utilisateur « projet » (non technique) ne voit plus l'UI technique au login : il arrive
  sur une **page d'accueil projets** listant les projets dont il est membre.
- En entrant dans un projet, il accède à des **vues** : des interfaces composées en piochant
  parmi les widgets des plugins du projet (UI complètes ou contrôles fins), sauvegardables,
  partageables aux autres membres.
- Le projet est **chargeable / déchargeable** comme une unité : charger = instancier son
  snapshot en containers live (à côté des autres projets actifs, **sans conflit**),
  décharger = détruire ses containers (le snapshot reste la vérité).
- Le projet est **isolé** mais possède une frontière explicite : des **sources/destinations
  virtuelles** que l'admin rattache aux ressources physiques (ou à d'autres projets).
- Les surfaces de contrôle broadcast (**TSL, Ember+**) parlent à des identités **stables au
  niveau du projet**, pas aux vmids : un projet vient « se loger » dans un ensemble de
  paramètres existants du contrôleur broadcast, dont la logique persiste.

Décisions actées :
- Constructeur/consommateur : **chaque utilisateur peut composer sa vue** ; une vue partagée
  peut être modifiable par certains et seulement utilisable par d'autres.
- Ressources : **espace isolé complet**, avec câbles inter-projets réservés à l'admin et
  accès aux sources/destinations autorisées via les ports virtuels.
- UI technique actuelle **conservée pour les rôles élevés** ; bascule par un **flag par
  utilisateur** `interface: technique|projets` (défaut selon rôle, modifiable par l'admin).
- Conflit de ressources au chargement : **refus bloquant avec détail** (pas de chargement
  partiel silencieux, pas de préemption).
- Terminaux cibles : **poste fixe grand écran + tablette tactile** (téléphone hors scope).

## 2. Ce que l'existant fournit déjà

| Brique | État | Référence |
|---|---|---|
| Snapshot/restore avec pré-vol (remap VMID, conflits hostname/SHM) | ✅ | `app/projects.py` (`planifier_restore`), `restaurer_projet` |
| Préfixage hostnames + réécriture des SHM internes au clonage | ✅ | `_prefix_snapshot` (`app/projects.py`) |
| Identité portable des containers | ✅ | `containers.instance_uuid` |
| Rattachement container→projet + média par projet | ✅ | `containers.project_id`, `/srv/mxl-media/<slug>/` |
| Système de widgets de fait : fragment HTML versionné + mount JS | ✅ | `GET /api/plugins/<type>/ui/html?vmid=X`, `tpEnsureAsset`, `window.MXLPlugins[type].mount(el, vmid, ctx)` / `unmount()` / `listPreview()` (`templates/plugin_section.html`) |
| Proxy de contrôle sécurisé (whitelist endpoints, read au login, actions sous `plugins.operate`) | ✅ | `plugin_proxy` (`app/routes/plugin_registry.py`) |
| Tuile vidéo live | ✅ | iframe WHEP (`MXLMonitor`, `templates/layout.html`) ; monitor déjà par-utilisateur (`monitor-u<uid>`) |
| Primitives CSS dashboard (grille avec spans, tuiles KPI+sparkline) | ✅ | `.home-panels`, `.stat-card` (`static/css/base.css`) |
| Rôle « pilotage sans déploiement » + scope de champ user/system | ✅ | rôle `exploitant`, `plugin_config` (commit 40de898) |
| Allocateurs centraux (cœurs, GPU, multicast, IP) | ✅ | `core_pool.py`, `gpu_pool.py`, `allocations.py` |
| Membres / ACL / propriétaire de projet | ✅ (ch. 1) | `project_members`, `auth.require_project_role` |
| Scoping des API par ressource (un membre ne pilote que SON projet) | ✅ (ch. 1) | `auth.vmid_project_ids`, `check_vmid_access` |
| Projet « actif » / cycle de vie / verrou de chargement | ✅ (ch. 2) | `projects.state` + verrou (`app/projects.py`) |
| Pré-vol de **capacité** (cœurs, queues, GPU) | ✅ (ch. 2) | `verifier_capacite`, `capacity_issues` de `planifier_restore` |
| Réallocation multicast au chargement | ⚠️ à vérifier/garantir | `mcast_allocations` |

## 3. Modèle de données cible

```
projects            += owner_id, slug, state (saved|loading|active|error|unloading)
                       -- niveau de tally : une ligne `tally_levels` (owner_kind='project'),
                       -- cf. §6 ; la colonne tally_base subsiste, plus personne ne la lit
project_members     (project_id, user_id, role)         -- owner|editor|operator|viewer
project_views       (id, project_id, name, owner_id,
                     visibility private|project, edit_shared BOOL, layout JSON)
project_versions    (id, project_id, created_at, label NULL, snapshot JSON)  -- historique
project_macros      (id, project_id NULL, name, owner_id, graph JSON,
                     published_to JSON)   -- macro/scénario ; project_id NULL = macro
                                          -- système (admin, inter-projets), cf. §7
project_vars        (project_id, name, type, value)                  -- variables, cf. §7
project_triggers    (id, project_id, name, enabled, condition JSON,
                     macro_id, cooldown_ms)                          -- règles, cf. §7
project_ports       (id, project_id, kind source|dest, media video|audio|anc,
                     name, ord, channel_labels JSON, binding JSON,
                     label_source port|origin, tsl_in port|external,
                     parent_id NULL, suffix NULL,        -- virtuelle composée (A1, A2…, Anc)
                     reusable BOOL)                      -- renvoi interne d'une destination
port_links          (port_id, consumer_uuid, kind, to_slot, shm, rev,
                     UNIQUE(consumer_uuid, kind, to_slot))   -- qui suit quel port, cf. §5
-- project_control_map (prévue au §6) n'a jamais existé : supplantée par `production_roles`
```

- **Rôle projet** ≠ rôle global. Le rôle global gouverne l'accès à l'UI technique ;
  le rôle projet gouverne ce qu'on fait *dans* le projet (`owner` tout, `editor` compose
  vues + câblage interne, `operator` pilote via les vues, `viewer` regarde).
- **`project_views.layout`** = liste de widgets positionnés en grille :
  `{widget: plugin_ui|plugin_widget|preview|stat|alerts, type, instance: <instance_uuid>,
  x, y, w, h, params}`. Les widgets référencent les containers par **`instance_uuid`**
  (jamais par vmid) → une vue survit au décharge/recharge du projet.
- Les **vues, les fiches médias et les ports font partie de l'export** `.bsproj.json` :
  schéma `bobi.studio.project.v4` (v2 vues, v3 médias, v4 ports + `port_links`, depuis le
  2026-10-30 — `projects.export_ports`). Ports recréés avec des ids NEUFS à l'import, liens
  re-mappés.

## 4. Cycle de vie : charger / décharger sans conflit

**Charger** = instancier le snapshot en containers live tagués `project_id`, coexistant avec
les autres projets actifs. **Décharger** = `detruire_containers_projet` (existe). Exigences :

1. **Verrou de cycle de vie** : état `loading` en DB + refus du double-chargement (le modèle
   de threading n'a pas de lock par vmid — le verrou se fait au niveau projet). Recharger un
   projet actif = proposer « recréer les manquants » (le `only_vmids` existant).
2. **Pré-vol de capacité, refus bloquant** : étendre `planifier_restore` pour vérifier,
   par nœud, AVANT toute création : cœurs restants (`core_pool`), **budget queues AF_XDP
   2110_io (RX+TX ≤ 16)**, GPU (`gpu_pool`), RAM/hugepages, et idéalement `membw`.
   Verdict détaillé : quelle ressource manque, quel projet actif l'occupe. Jamais de
   chargement à moitié.
3. **Multicast** : les adresses du snapshot ne sont **jamais rejouées telles quelles** —
   réallocation via `mcast_allocations` au chargement (sinon deux instances du même export
   émettent sur les mêmes groupes : conflit silencieux, le pire cas). Les destinations
   *sortantes* fixées par contrat externe passent par les ports virtuels (§5), pas par le
   snapshot.
4. **Dépendances externes** : au chargement, valider les bindings des ports (§5) ; un port
   non câblé n'est pas bloquant mais doit être signalé (« entrée CAM2 non raccordée »).
5. **Namespaces** : le préfixage hostname/SHM existant reste la règle ; les VMID restent
   des handles jetables remappés.
6. **Projet vivant** : le snapshot suit le live automatiquement — toute modification d'un
   container tagué `project_id` marque le projet « dirty » et déclenche un re-snapshot
   débouncé. Décharger ne perd jamais rien. `project_versions` conserve un **historique**
   (rétention N versions auto + versions **nommées** illimitées, ex. « avant émission »)
   avec retour arrière = charger une version comme un snapshot.

## 5. Frontière du projet : sources/destinations virtuelles (ports)

**Idée centrale.** Le projet déclare ses **ports** : des entrées (« sources virtuelles » —
CAM1, CAM2, EXT…) et des sorties (« destinations virtuelles » — PGM, CLEAN, AUX1…), nommés
et stables. À l'intérieur, les containers se câblent **sur les ports** comme sur n'importe
quel flux ; le snapshot ne référence que des ports, jamais des ressources physiques.

À l'extérieur, **l'admin rattache chaque port où il veut** (binding hors snapshot) :
- une session RX / un flux TX du `2110_io` (multicast physique),
- un SHM produit par un autre projet (câble inter-projets = binding port→port),
- une destination stream (SRT/UDP/WebRTC).

Conséquences :
- **Isolation propre** : un membre du projet ne voit que ses ports ; il ne connaît pas la
  topologie physique.
- **Portabilité** : déplacer un projet sur d'autres entrées/sorties = re-binder, sans
  toucher au contenu du projet. Recharger un projet re-résout automatiquement ses bindings.
- **Inter-projets** : un câble entre projets est un objet d'admin (binding), visible dans
  la page Câbles (le filtre par projet y existe déjà), jamais un couplage interne.
  **Livré le 2026-10-24** : un port SOURCE peut suivre le port DESTINATION d'un autre projet
  (`binding: {"port": <id>}`) — il prend ce que l'autre publie EN CE MOMENT, suit sa
  republication à chaud (`replay_port` rejoue les ports qui le suivent), devient non affecté si
  l'autre ne publie plus ou disparaît. Seule une destination se suit, et une destination ne
  suit rien : pas de boucle (`project_ports.port_shm`).
- **Page Câbles, vue globale (« ▣ Vue projets », livrée le 2026-10-24)** : un projet = un bloc
  (ports nommés et leur état, résumé de contenu, « Voir l'intérieur », « Charger ») ; hors projet
  non relié replié sous une ligne ; liaison DIRECTE d'un conteneur de projet sur un flux réel
  hors port en rose pointillé — clic : « Créer le port et y rattacher ». Gestes (même règle que
  partout : lâcher REMPLACE, sans confirmation) : sortie hors projet → port source = affecter
  (admin) ; port destination → port source d'un autre bloc = suivre ; port destination → entrée
  hors projet = câblage `port:<id>`. Clic sur le câble d'un port : « Désaffecter ».
  Vue DÉTAILLÉE (filtre projet ; maquette C du 2026-10-24) : bandeau « Vous êtes dans le projet »
  + liseré aux deux bords ; les ports en DEUX cartes de même vmid (`split`) — sources en tête de la
  colonne de gauche (« Entre dans le projet »), destinations en tête de celle de droite (« Sort du
  projet »). Une virtuelle composée se lit sur UNE ligne : ses audios et son ANC rattachés sont
  superposés sur la ligne de la vidéo (`parent_port_id`, `suffix`, fournis par le serveur dans les
  deux vues) ; lâcher un audio sur la ligne l'affecte au premier rattaché libre. Chaque port dit d'où vient son signal (`origin` : producteur, ou port suivi) ou où il
  part (`targets` : lecteurs HORS du projet, et projets qui le suivent — nommés par leur port, pas
  par leurs conteneurs). Glisser un port source sur une entrée
  la câble en `port:<id>` ; lâcher une sortie sur un port destination la publie. Une entrée liée
  à un port y est raccordée par son câble (le lien n'est montré que s'il décrit ce que l'entrée
  reçoit vraiment). Code : `home_dashboard._projects_overview` / `_project_port_nodes`,
  `templates/cables.html` (`renderProjectModule`, `commitPortWire`). Test :
  `tests/verif_cables_vue_globale.py` (les gestes eux-mêmes, côté navigateur, ne sont pas
  couverts hors ligne).
- Implémentation : le binding se matérialise au chargement — côté source par la config du
  consommateur (SHM/flux résolu), côté destination par le fan-out existant (`streamer` tee,
  flux TX composables du 2110_io). Un port non bindé = source « pas de flux » (comportement
  RX déjà connu et géré).
- **Résolution « option C » (2026-10-23, `app/project_ports.py`)** : le SNAPSHOT ne retient que
  `port:<id>` pour les ENTRÉES liées (`virtualize_snapshot_entry`, plus la liste `port_links` de
  chaque conteneur) ; le CONTENEUR ne reçoit que du physique. La table `port_links`
  (instance_uuid du consommateur, essence, slot → port) dit quelles entrées suivent quel port —
  c'est elle qui fait autorité, le shm écrit dans les params vivants n'est que la résolution
  courante.
- **Les ports ne sont jamais contournés : R1 et R2 (décision de Cyril, 2026-10-31)**. Corrigé à la
  racine, **pas de réconciliateur** (Cyril y voit un contournement) :
  - **R2 — tout changement de source tient `port_links` à jour** : `input_changed`, appelée par le
    SEUL écrivain de la config d'un conteneur, `database.db_update_deploy_config`. Elle compare
    l'ancienne et la nouvelle config entrée par entrée : entrée liée qui lit le flux de son port →
    le lien reste ; entrée liée qui lit autre chose → le lien SUIT le port source du projet qui
    apporte ce flux (`moved`), sinon il est défait (`dropped`) ; entrée non liée qui se met à lire
    le flux d'un port source du projet → le lien est posé (`linked`). Câblage, Ember+, NMOS, macro,
    relais de plugin : tous passent par là.
  - **R1 — tout envoi vers un conteneur traduit depuis `port_links`** : `resolve_inputs` (chaque
    entrée liée relit le flux ACTUEL de son port, "" s'il n'est pas affecté), appelée via
    `prepare_send` par `deployer_script` et par chaque envoi qui relit la base
    (`deploy._fresh_stored_params`, `docker_driver`, redémarrage d'un compute).
  - **Toujours R2 AVANT R1** (`prepare_send`) : sinon un câblage direct serait aussitôt réécrit avec
    le flux de l'ancien port.
  - Le `POST /input` d'un plugin, via le relais ou une macro, devient un câblage
    (`cabling.wiring_input_call`) ; une garde de manifeste échoue si un plugin déclare un autre
    endpoint qui porte un flux.
  - **Envoi PÉRIMÉ (2026-11-01, option A choisie par Cyril)** : une page qui renvoie toute la config
    qu'elle a chargée renvoyait aussi l'ancien flux d'une entrée dont le port avait été réaffecté
    depuis — R2 le lisait comme un recâblage. Chaque lien retient la révision du consommateur où
    son flux a changé (`port_links.rev`) ; un `/deploy` bâti sur une révision antérieure (`base_rev`
    du composer multiview, `seen_rev` de la palette, ce dernier sans le refus 409) garde l'entrée
    sur son port (`keep_linked_inputs`). Une entrée vue puis changée reste un recâblage. Sans
    révision (appel d'API) : comportement historique.
  - **Slots qualifiés (2026-11-01)** : une liste qui reprend une essence déjà prise par une liste
    précédente reçoit des slots `"<liste>:<rang>"` (`plugins.qualified_lists`) — vu-mètres et frises
    du multiview, découpe de `decklink_io` ; la première liste garde ses slots entiers (câbles et
    UUID NMOS inchangés). Le slot départage aussi deux entrées sur le même flux au décâblage.
  Gestes qui posent ou rejouent un lien : câbler `port:<id>` (`POST /api/home/wire`) ; réaffecter
  un port (PATCH `binding` → `apply_binding` → `replay_port`, y compris depuis/vers « non
  affecté ») ; recharger le projet (`materialize_params` avant le déploiement — une entrée sur un
  port non affecté ou supprimé est vidée, un `port:<id>` hors entrée non résoluble reste tel quel
  — puis `restore_links`, qui recâble aussi les suiveurs et deux ports sur un même flux). Port
  supprimé = entrées liées vidées. Limite restante : les flux SUIVEURS (audio/ANC qui suivent la
  vidéo d'une tuile) ne sont pas virtualisés dans le snapshot, corrigés au rechargement.
  Tests : `verif_ports_virtuels`, `verif_ports_r1_envois`, `verif_ports_r2_liens`,
  `verif_ports_envoi_perime`, `verif_ports_snapshot_entrees`, `verif_ports_set_input`,
  `verif_ports_slots_qualifies`, `verif_projet_export_ports`.
- **Virtuelles composables (2026-10-28, maquette A)** : une virtuelle = une vidéo, 0 à 8 audios
  (un flux MXL de 8 canaux chacun) et un ANC. Avec une vidéo, c'est elle le PARENT : les autres
  ports lui sont rattachés (`parent_id`, `suffix` A1, A2…, Anc), se raccordent AVEC elle
  (`rebind_children`) et héritent de ses libellés (`tally._port_row`). Sans vidéo, chaque flux est
  un port indépendant. Création en lot `POST /api/projects/<pid>/ports/batch`
  (`create_virtuals`), ajout d'un rattaché `POST …/ports/<id>/children`. Test :
  `verif_ports_virtuelles`.
- **Renvois (2026-10-29)** : une destination marquée `reusable` est proposée comme SOURCE dans la
  vue Câbles de son propre projet (« ↻ Renvois du projet ») : plusieurs entrées internes la suivent
  et commutent ensemble. Un conteneur ne lit pas une destination qu'il alimente : 409 `port_loop`,
  dans les deux sens. Test : `verif_ports_renvois`.
- **« Faire entrer cette source par un port du projet » (2026-10-31)** : au lieu du seul refus
  « source hors projet », la page Câbles propose de faire entrer le flux par un port source du
  projet, réutilisé s'il en existe un affecté à ce flux, créé sinon, puis câblé
  (`POST /api/projects/<pid>/ports/admit`, `admit_flow` ; réservé aux accès globaux, comme toute
  affectation de source). Le refus 403 nomme le projet (`project_id`). Test :
  `verif_ports_admettre_source`.
- **Sélecteur de source du multiview** : `GET /api/containers/<vmid>/input_ports` propose les ports
  du projet à côté des flux.
- **Libellés des ports (livrés le 2026-10-24)** : les noms d'un port vivent dans la table des
  libellés sous la clé `port:<id>`, avec les mêmes colonnes qu'une source — chaque consommateur
  garde la colonne qu'il a choisie. Nom LONG et nom COURT = deux de ces colonnes, désignées par
  le réglage `port_label_cols` (défaut Label 2 / Label 3 ; `GET|POST /api/labels/port_cols`).
  Un port non nommé affiche son nom (long) et un court déduit. Nommés par les éditeurs du projet
  et l'admin (PATCH du port, champ `labels: {long, short}`), pas par `settings.edit`.
  Par port : `label_source` = `port` (sa ligne, défaut) ou `origin` (la ligne du flux affecté
  EN CE MOMENT — elle suit la réaffectation) ; le choix vaut pour la ligne ENTIÈRE, jamais de
  repli d'une ligne sur l'autre (`tally.row_ref`). `tsl_in` = `port` (défaut : un nom reçu d'un
  mélangeur externe en TSL n'écrit pas dans la ligne du port) ou `external` (il y écrit).
  Comme le conteneur ne connaît que le physique, on remonte du flux au port DU POINT DE VUE DU
  CONSOMMATEUR (`project_ports.label_ref_for`) : son lien d'abord, puis les ports de son projet
  (source avant destination) ; hors projet, le flux. Branché sur le distributeur (fenêtres de
  multiview, cibles de plugin), l'AFV (entrées du mélangeur) et le pupitre ATEM ; la sortie TSL
  vise déjà `port:<id>` par sa correspondance.
  **Port non affecté (2026-10-24)** : l'entrée qui le suit reste VIDE — noir au mélangeur,
  « NO SIGNAL » au multiview ; pas de mire pour l'instant (décision : le noir suffit ; la piste
  la plus économe, si on la reprend, est une image tenue en mémoire par le consommateur, pas un
  flux partagé répliqué en RDMA). Chaque colonne saisie du port porte alors « · non affectée »
  (`projects.port_unassigned`, dans la langue du SITE : le texte part vers des écrans partagés).
  Une entrée vide n'ayant plus de flux, son port se retrouve par le LIEN de l'entrée (slot) :
  tuile de multiview, entrée du mélangeur pour l'AFV, touche du pupitre ATEM.
  Pas encore : les noms des touches du mélangeur (fonction nouvelle du plugin), et les libellés de
  canaux audio — `channel_labels` est stocké mais AUCUN consommateur ne le lit, ni aucun
  équivalent côté signal réel. Test : `tests/verif_libelles_ports.py`.
- **Frontière appliquée (décision du 2026-10-24)** : une entrée d'un conteneur de projet ne lit
  QUE ce que son projet produit, ou ce qu'un port source du projet apporte — pour tout le monde,
  admin compris, et pour tous les projets. Garde unique dans `cabling._apply_wire`, par où passent
  la page Câbles, Ember+, NMOS IS-05, les instantanés de câblage, l'UDC et la résolution des ports
  (`project_ports.wire_check`) ; refus = 403 `shm_not_in_project` + alerte
  `alert.cablage.hors_projet_refuse`. Un flux extérieur qui EST l'affectation d'un port est accepté
  et enregistré comme suivant ce port ; tout autre câble direct défait le lien de l'entrée (Ember+
  et NMOS compris — la limite « un câblage venu d'ailleurs ne défait pas le lien » est levée).
  Au RECHARGEMENT, une entrée qui lisait un flux extérieur hors port est VIDÉE, pas rejouée
  (`strip_external_inputs`, alerte `alert.projet.entree_hors_projet_videe`) : un projet ancien peut
  revenir avec des entrées vides, c'est voulu. Appartenance jugée par `member_project_ids` : un
  snapshot ne rattache un conteneur que si l'entrée désigne la MÊME instance (uuid, sinon hostname)
  — le vmid seul est réattribué. Un UDC inséré devant un conteneur de projet rejoint ce projet.
  Commandes de contrôle aussi (2026-10-31) : le relais de plugin et les actions de macro refusent
  un corps qui pointe une entrée sur un flux hors projet (`project_ports.body_flow_refusal`, 403
  `shm_not_in_project`), et un `/input` devient de toute façon un câblage (cf. R2). Test :
  `tests/verif_frontiere_projet.py`, `tests/verif_ports_entree_par_cablage.py`.

## 6. Persistance TSL / Ember+ : le projet se loge dans le contrôleur

> **Supplanté (constat du 2026-11-01).** `project_control_map` / `role_ref` n'ont jamais été
> construits. L'adresse stable a été faite autrement : les **emplacements** (`production_roles`,
> `app/routes/roles_api.py`, cf. CLAUDE.md « Identité d'un conteneur »), qu'Ember+ adresse déjà
> (`emplacements.<num>`) ; TSL, le pont ATEM et les macros publiées visent encore vmid/uuid. Le
> niveau de tally par projet existe, mais sous forme de NIVEAU nommé (`tally_levels`,
> `owner_kind='project'`, uuid + rang d'affichage) et non plus de `tally_base` à pas de 3. La
> conception ci-dessous est gardée pour l'intention.

**Problème** : le contrôleur broadcast (pupitres, UMD, systèmes tiers via `services/tsl` et
`services/emberplus`) est configuré une fois — index tally TSL, arbres de paramètres Ember+.
Ces identités ne doivent PAS dépendre des vmids ni changer au rechargement d'un projet.

**Abstraction** : `project_control_map` associe des **adresses stables** (index TSL, chemins
Ember+) à des **rôles internes du projet** (`role_ref`), qui sont :
- soit un **port** (§5) : « tally de la source CAM1 », « UMD de PGM » ;
- soit un **rôle fonctionnel d'instance** : « le mixer », « la T-bar », résolu par
  `instance_uuid` au chargement.

Au chargement du projet, les services TSL/Ember+ **résolvent** la table : les paramètres
que le contrôleur broadcast connaît déjà se peuplent avec les instances fraîches. Au
déchargement, ils repassent à un état neutre (UMD vide, paramètres offline) sans disparaître
de l'arbre. Le contrôleur broadcast ne se reconfigure jamais : **le projet vient se loger
dans un ensemble de paramètres existants.**

Côté code : `services/tsl` et `services/emberplus` gagnent une couche d'indirection
(adresse → role_ref → vmid live) au lieu de référencer des containers en direct ; hook au
chargement/déchargement de projet.

### Niveau de tally par projet (décision 2026-07-05)

Chaque projet possède **son niveau de tally, créé automatiquement** (un `tally_base`
unique alloué au projet). Dans ce niveau peuvent écrire :
- un **TSL entrant** (une connexion `tsl_connections` rattachée au niveau du projet —
  le contrôleur broadcast externe reste écrivain, comme aujourd'hui) ;
- un **mélangeur du projet** : le mixer émet son tally (PGM=rouge, PVW=vert) sur un
  **niveau sélectionnable** — par défaut le niveau de son projet. Résolution : entrée
  du mixer → shm → **port** du projet → index tally (l'index d'une source dans le
  niveau projet = son port, cf. §5 — « tally de CAM1 »).

Sémantique d'écriture : fusion **par index** (dernier écrivain gagne par index) — deux
écrivains sur le même index du même niveau est une erreur de configuration à signaler,
pas à arbitrer. Les consommateurs du niveau : multiviews (distribution `tally_bulk`
existante, le niveau se choisit déjà par fenêtre), **TSL sortant** (UMD externes),
et plus tard les widgets tally du workspace (ch. 6).

## 7. Interfaces composées (vues)

- **Widgets niveau 1** (quasi gratuit, aucun plugin à modifier) : UI de plugin complète via
  `MXLPlugins[type].mount()` ; tuile preview WHEP ; tuile statut/fps (`listPreview` +
  `/api/projects/<pid>/summary`) ; tuile alertes du projet.
- **Widgets niveau 2** (contrat à créer) : `plugin.json` déclare
  `"widgets": [{id, label, min_size, …}]`, fragments versionnés servis par
  `/api/plugins/<type>/ui/widget/<id>` — même mécanique que `control.html`. Candidats :
  T-bar seule, boutons PGM/PVW, take, VU-mètre, cellule tally, transport player/recorder.
  Migration plugin par plugin, **mixer en premier**.
- **Shotbox (widget transverse)** : une grille de boutons où l'utilisateur épingle des
  **fonctionnalités de n'importe quel plugin du projet**. Un bouton = `{label, couleur,
  action}` où l'action = `{instance: <instance_uuid>, action_id, params}`.
  - **Contrat `actions` dans `plugin.json`** : chaque plugin déclare un catalogue d'actions
    « shotboxables » : `"actions": [{id, label, endpoint, params_schema, feedback}]` —
    couche curatée au-dessus de `control.endpoints` (ex. mixer : `take`, `cut`, `pgm(n)` ;
    stills : `select(file)` ; recorder : `start`/`stop` ; player : `play(clip)`).
  - **Exécution** : un bouton ne fait qu'un POST via le proxy plugin existant (whitelist +
    `plugins.operate`) — aucune nouvelle surface d'attaque, aucun code plugin in-process.
  - **Feedback** : `feedback` référence un `read_endpoint` + expression (ex. le still actif
    allume son bouton, REC allumé pendant l'enregistrement) — polling léger mutualisé par
    vue.
  - **Macros** : objets de projet à part entière (`project_macros`, cf. ci-dessous) —
    un bouton de shotbox peut porter soit une action, soit une macro
    (`{kind: action|macro}`).
- **Macros & Scénarios : un seul objet, deux éditeurs.** L'objet stocké est **toujours un
  graphe** (`graph JSON` : nœuds + arêtes ; nœuds = action, pause, condition, set_var,
  wait, parallèle, appel de macro). Nommage produit : **« Macro »** = l'éditeur à blocs
  (vue liste), **« Scénario »** = l'éditeur nodal avancé — deux vues du même objet, un
  seul moteur d'exécution.
  - **Règle de compatibilité des vues** : l'éditeur à blocs sait représenter tout graphe
    **structuré** (série-parallèle bien imbriqué) : séquence ; bloc `si/sinon` (2 sorties
    qui se rejoignent) ; bloc `choix` (N sorties qui se rejoignent) ; bloc **`en
    parallèle`** (branches simultanées affichées en colonnes côte à côte, qui se
    rejoignent — jonction implicite « quand toutes finies »). Tout ce qui se construit en
    blocs reste éditable en blocs. Un graphe édité en mode Scénario qui casse cette
    structure (sauts entre branches, entrées multiples, cycles) porte un badge « avancé »
    et ne s'ouvre plus qu'en vue nodale — jamais de conversion avec perte.
  - **Ce que la vue Scénario déverrouille** : branches parallèles libres, jonctions
    partielles, plusieurs points d'entrée (les déclencheurs deviennent des nœuds d'entrée
    du graphe), imbrication (un nœud = appel d'une autre macro/scénario), sorties
    multiples d'un nœud de choix vers des sous-graphes distincts.
  - **Pas un `services/`** (réservé aux intégrations externes) : module cœur
    **`app/macros.py`** — le moteur dépend des projets, du catalogue d'actions et du proxy,
    c'est du domaine central.
  - **Exécution côté orchestrateur** (pas côté client) : fiabilité (l'onglet fermé
    n'interrompt pas la macro), point d'exécution unique pour toutes les surfaces (shotbox,
    Stream Deck/Skaarhoj, déclencheurs Ember+/TSL futurs). Runner en thread, étapes
    séquentielles, **droits vérifiés au déclenchement** (membre du projet +
    `plugins.operate`), étapes limitées au catalogue `actions` des containers du projet.
    Une exécution à la fois par macro, annulable ; état d'avancement remonté au bouton
    (bouton « en cours » avec n° d'étape).
  - **Éditeur Macro = blocs empilés** (type Scratch simplifié / Companion) : lecture de
    haut en bas, blocs `action` / `pause` / `si … alors … sinon` (indentés) / `choix` /
    `en parallèle` (colonnes) / `poser variable` / `attendre`. Chaque bloc = les mêmes
    sélecteurs que la shotbox (container × action × params). Boutons « Tester » et
    pas-à-pas. Lisible et éditable au doigt sur tablette. **Éditeur Scénario = canvas
    nodal** pour le mode avancé (cf. règle de compatibilité ci-dessus) ; il peut arriver
    en v2, le modèle graphe étant là dès la v1.
  - **Fournisseurs d'actions/état — trois niveaux.** Le catalogue n'est pas que
    par-container ; « tout doit être disponible dans les macros » :
    1. **Plugins** (scope container) : `actions`/`state` dans `plugin.json` — take, pgm(n),
       select(still), record… et le **tally d'un multiview** (l'endpoint
       `/api/containers/<vmid>/tally` existe déjà, il suffit de le déclarer).
    2. **Services** (scope système, déclaré dans leur `manifest.json`) : actions
       transverses non rattachables à un container — **`tsl.set_label(shm, colonne,
       texte)`** (la table `source_labels` est keyée par shm, pas par container),
       **`skaarhoj.assign_key(panel, touche, action|macro)`**, `atem.*`… Même mécanique
       d'exécution (routes du service), même sélecteur dans l'éditeur.
    3. **Cœur orchestrateur** (scope système, admin) : redémarrer un container, charger/
       décharger un projet, re-binder un port… — réservé aux macros système (cf. plus bas).
    Dans un projet, l'éditeur ne montre que : les actions des containers du projet + les
    actions de service explicitement **accordées** au projet (même logique que les ports :
    l'admin décide ce qui traverse la frontière — ex. « ce projet peut écrire les labels
    de SES sources »).
  - **Labels & tally = actions et états de plein droit.** État des lieux (audit
    2026-07-05) : trois circuits de tally non unifiés — (A) TSL entrant → distributor →
    multiviews (`services/tsl`, récepteur seul, aucune émission TSL sortante) ; (B) mixer
    PGM/PVW → pupitres via poll bespoke du service atem ; (C) tally manuel par multiview.
    Le mixer n'alimente pas l'état TSL. Cible : le mixer déclare `state: pgm/pvw`, le
    multiview déclare `action: tally(fenêtre, couleur)`, le service tsl déclare
    `action: set_label` + `state: tally(index)` — et le moteur de macros devient le
    liant générique (« si mixer.pgm == 3 → tally rouge sur MV fenêtre 3 + set_label »),
    remplaçant à terme les ponts bespoke. L'émission TSL **sortante** (piloter des UMD
    externes) est un manque connu, à ajouter au service tsl.
  - **Logique (`si`)** : condition = comparaison entre une **valeur d'état d'un plugin**,
    une **variable** ou une constante (`=`, `≠`, `<`, `>`, contient). Les valeurs d'état
    viennent d'un nouveau volet du manifeste : `"state": [{id, label, endpoint, json_path,
    type}]` — le pendant lecture du catalogue `actions`, adossé aux `read_endpoints`
    existants (ex. mixer : `pgm`, `t_bar_pos` ; recorder : `recording` ; player : `clip`,
    `playing`). Même liste déroulante lisible partout (conditions, feedback shotbox,
    triggers).
  - **Variables de projet** (`project_vars`, typées string/number/bool) : posées par un
    bloc `poser variable`, lisibles dans les conditions et **injectables dans les params
    d'action** par gabarit `{{var}}` (ex. `pgm({{camera_active}})`). Portée = le projet
    (partagées entre macros, shotbox et triggers) ; affichables dans une vue via une tuile
    « variable » (lecture + saisie).
  - **Déclencheurs** (`project_triggers`) : règles permanentes « quand <condition> devient
    vraie → lancer <macro> ». Évaluées par le moteur sur un **poller d'état mutualisé** par
    projet actif (même mécanique que le feedback shotbox, ~1 s), **sur front montant**
    (edge-triggered) avec `cooldown_ms` anti-rafale. Éditeur : même constructeur de
    condition que le bloc `si`, plus un interrupteur `.ios-toggle` actif/inactif par règle.
  - **Fader start** (2026-10-16, `services/audio/fader_triggers.py`) : déclencheur dont la condition est
    `{kind:"fader", instance_uuid, target, edge:"open"|"close", threshold_db, hysteresis_db,
    respect_mute, with_vca}` — ouvrir (ou fermer) le fader d'une tranche/d'un bus lance la macro.
    Ouvert = niveau > seuil (−120 = −∞ : « dès qu'il quitte −∞ ») ET non muet (mute de la tranche,
    ou mute d'un VCA hors Mute Safe) si `respect_mute` ; fermé = niveau ≤ max(seuil − hystérésis,
    −120) OU muet. `with_vca` ajoute les niveaux de VCA au fader. Le poller 1 s ne l'évalue PAS :
    poller dédié à 100 ms, UNE lecture par console et par tick (mesuré sur 1160 : réaction médiane
    74 ms, max 119 ms). Le plugin le déclare par `control.fader_start: {endpoint}` (vue compacte
    `/doc?faders=1` de la console, ≈ 3 Ko) ; une console pas encore redéployée répond le document
    entier à la même URL → vue dérivée par les racines `param_tree.by_id`, lecture ralentie à
    250 ms (le document à 10 Hz coûte des blocs audio en retard, mesuré). Pas d'alerte par
    déclenchement (une émission ouvre des faders des centaines de fois) : la trace est le run de
    la macro + le journal. Dans une macro, `eval_cond` sur cette condition = état ouvert instantané.
  - **Lien fader start de la console et déclencheur d'INSTALLATION** (2026-10-18,
    `services/audio/console_fader_links.py`) : le lien « fader start → lecteur N » écrit ses déclencheurs (action
    directe, sans macro) dans un projet qui porte la console ET le producteur (rôle éditeur). Si NI la
    console NI le producteur ne sont dans un projet, le lien est un déclencheur d'installation :
    `project_triggers.project_id` NULL (colonne devenue nullable, migration par reconstruction de la
    table, `database._migrate_triggers_nullable_project`), droit `console.build`. Scruté, déclenché et
    retiré comme les autres ; absent des listes de déclencheurs d'un projet ; survit à la suppression
    d'un projet, qui supprime désormais explicitement SES déclencheurs (`foreign_keys` est éteint : le
    `ON DELETE CASCADE` n'a jamais tourné). Tout autre cas (l'un dans un projet et pas l'autre, deux
    projets différents) est refusé avec un code qui le nomme. Les déclencheurs ne font pas partie de
    l'export/import de projet.
  - **Garde-fous du moteur** : profondeur d'appel macro→macro bornée, cooldown par trigger
    (une macro qui modifie la condition qui la déclenche ne doit pas boucler), timeout
    par étape, journal des exécutions (qui/quoi/quand/résultat) consultable dans le
    workspace.
    Étapes v1 : `action`, `sleep`, `set_var`, `if/else` ; v2 : `wait` (attendre qu'une
    condition devienne vraie, avec timeout) + triggers. Mode « apprentissage »
    (enregistrer les actions faites dans les UI plugin) : plus tard.
  - **Macros système (administrateur, inter-projets)** : `project_macros.project_id`
    devient nullable — `NULL` = **macro système**. Création/édition réservée (permission
    `projects.manage` ou dédiée `macros.admin`) ; catalogue complet : tous les containers
    de tous les projets, toutes les actions de services, les actions cœur (charger/
    décharger un projet, redémarrer un container, re-binder un port). Cas d'usage :
    bascule de conduite entre deux projets, « fin de journée » (décharger X, charger Y,
    re-patcher les UMD), reconfiguration d'un panel Skaarhoj.
    **Publication vers un projet** : l'admin peut exposer une macro système à un projet —
    elle apparaît comme un bouton opaque (shotbox) que les membres peuvent déclencher
    mais ni lire ni éditer, et elle s'exécute **sous l'autorité de la macro** (modèle
    sudo : droits définis par l'admin à la création), pas celle de l'invocateur. Le
    journal enregistre toujours l'invocateur réel. Les macros système ne font pas partie
    des exports projet.
  - Les macros, variables et triggers **de projet** font partie de l'**export projet** et
    référencent les containers par `instance_uuid` (stables au rechargement), comme les
    vues et la shotbox.
  - **Portée au-delà de l'écran** : le même catalogue d'actions (adressé par
    `instance_uuid`/rôle, donc stable au rechargement du projet) est la cible naturelle des
    **surfaces physiques** — Stream Deck (spike prévu), Skaarhoj (`services/panel_skaarhoj`),
    et cohérent avec l'indirection TSL/Ember+ du §6 : une action = une adresse stable dans
    laquelle le projet vient se loger.
- **Éditeur de vue** : grille CSS (réutiliser `.home-panels`/tokens), drag/drop/resize en
  vanilla JS (pas de framework, conforme à l'archi front), palette latérale = containers du
  projet × widgets disponibles. **Tactile requis** (tablette) : drag pointer-events, cibles
  ≥ 44 px, T-bar utilisable au doigt.
- **Chrome projet allégé** : nom du projet, sélecteur de vues, bouton monitoring, logout.
  Pas de nav technique. i18n obligatoire (`window.t`), `.ios-toggle` pour tout booléen.

## 8. Sécurité : le chantier central

Aujourd'hui **tout est global** (`require_perm` sur le rôle, aucun scoping par ressource).
Sans scoping, le workspace est un décor : n'importe quel utilisateur loggué peut piloter
n'importe quel container via le proxy. Il faut :

- `require_project_role(pid, role)` + résolution vmid→projet ;
- appliquer aux **API sensibles** pour les non-admins : `plugin_proxy`, `plugin_config`,
  plugin store, `/api/monitor/*` (restreindre `monitorVmid` aux containers du projet),
  streams API, et un `/api/projects/<pid>/summary` dédié (ne pas exposer
  `/api/home/summary` global aux utilisateurs projet) ;
- le gating front (`window.hasPerm`) reste cosmétique — l'autorité est côté serveur.

## 9. Chantiers (séquentiels, un à la fois)

1. **Fondations** — `project_members`, `owner_id`, rôles projet, flag utilisateur
   `interface`, page d'accueil projets, redirection au login, **scoping sécurité** (§8).
2. **Workspace + vues** — `project_views`, page workspace (grille, mode édition,
   sauvegarde/partage/duplication), widgets niveau 1, `/api/projects/<pid>/summary`.
3. **Cycle de vie** — états + verrou de chargement, pré-vol de capacité refus-bloquant,
   réallocation multicast garantie, vues dans l'export v2.
4. **Ports virtuels** — `project_ports`, éditeur de bindings admin (page Câbles),
   câbles inter-projets, signalement des ports non raccordés.
5. **TSL/Ember+** — `project_control_map`, indirection dans `services/tsl` et
   `services/emberplus`, hooks chargement/déchargement.
6. **Widgets fins + shotbox + macros/scénarios** — contrats `actions` et `state` de
   `plugin.json` **et des manifestes de services** (tsl.set_label, skaarhoj.assign_key…),
   widget shotbox avec feedback, moteur graphe + éditeur Macro à blocs (`app/macros.py` :
   action/sleep/set_var/if/parallèle en v1, modèle stocké = graphe), variables de projet,
   **macros système** (admin, inter-projets, publication opaque vers les projets), puis
   widgets fins (T-bar seule, VU, tally), mixer d'abord. Suivent : nœud `wait`, éditeur
   Scénario nodal, déclencheurs permanents, actions cœur orchestrateur, émission TSL
   sortante, pont surfaces physiques (Stream Deck/Skaarhoj).

L'ordre 4↔5 peut s'inverser ; 6 peut démarrer dès que 2 est stable — la **shotbox est le
meilleur premier livrable du chantier 6** : forte valeur, contrat minimal, zéro code plugin
in-process.

## 10. Décisions complémentaires (tranchées 2026-07-05)

- **Sauvegarde = projet vivant + versions** : re-snapshot automatique débouncé (cf. §4.6),
  historique `project_versions`, points nommés, retour arrière. Pas de bouton
  « Enregistrer » obligatoire ; un bouton « Nommer cette version » à la place.
- **Partage de vue = par rôle projet + duplication** : une vue a un propriétaire, une
  visibilité (`private` | `project`) et un booléen `edit_shared` (propriétaire seul, ou
  tous les `editor` du projet). Tout membre peut **dupliquer** une vue partagée pour la
  personnaliser. Pas d'ACL par membre.
- **Pas de quotas** : l'admission au chargement (refus bloquant détaillé) est la seule
  régulation. Le pré-vol est conçu pour accepter plus tard des **réservations** (un projet
  critique déclare ses besoins et reste garanti chargeable) — API du pré-vol : penser
  `demande(projet) + réservations(actifs+dormants réservés) ≤ capacité(nœud)` dès le début.
- **Port audio = 8 canaux intégraux + labels par canal** (`channel_labels`, ex.
  « 1-2 ambiance », « 3 HF présentateur ») ; le choix des canaux reste à la consommation
  (modèle `streamer.tracks` actuel). Zéro traitement au binding. Un vrai patch audio
  (shuffle multi-sources) viendra éventuellement plus tard comme plugin dédié.

## 11. Médias : acquis et compléments (audit 2026-07-05)

**Déjà en place et conforme au design** — l'isolation média est *physique* côté conteneur :
- Bind par projet : `project_id` → montage `/srv/mxl-media/<slug>` sur `/mnt/media`
  (`docker_compute.py:286-296`, plugins déclarant `media_volume:true`).
- player/recorder/stills ne voient que `/mnt/media` (walk/anti-traversal bornés au bind) —
  **aucun changement plugin requis**.
- Bibliothèque de clips « mode plugins » : interroge chaque container via le proxy →
  scopée projet dès que le scoping du proxy (chantier 1) existe.
- Rechargement d'un projet → même `media_path` : les médias survivent au cycle
  charge/décharge (conforme « projet vivant »). Les fichiers ne sont PAS dans le snapshot
  ni dans `project_versions` (seul `media_path` l'est) — assumé : le stockage média est
  l'état persistant du projet, non versionné.

**Trous à combler** (rattachés aux chantiers §9) :
1. **Services `files` / `media_manager` / `storage` globaux** : ils opèrent sur la racine
   `/srv/mxl-media` de tous les nœuds, tous projets confondus ; le champ `project` du
   media_manager est décoratif, et le filtre projet des partages externes (`ext_shares`)
   est passé en query-string **non vérifiée** (`media_manager/__init__.py:928`).
   → **Chantier 1** : masquer ces services aux utilisateurs projet + corriger le filtre
   `ext_shares` (project_id déduit de l'appartenance, jamais du client). **Chantier 2** :
   variante scopée (biblio médias du workspace = mode « plugins » + listing serveur
   restreint au `media_path` du projet).
2. **Import/clonage ne crée pas le dossier média** : `db_import_project` laisse
   `media_path` NULL → les containers retombent sur la racine partagée (fuite
   inter-projets). → **Chantier 3** : le chargement/import garantit la création du
   dossier + `media_path` (même slugification que `POST /api/projects`).
3. **Container sans `project_id` = accès racine** (voit tous les sous-dossiers projet).
   Toléré pour l'UI technique ; invariant à tenir : **tout container créé via un projet
   porte toujours son `project_id`** (déjà le cas dans `restaurer_projet`).
4. **Suppression de projet = fichiers orphelins** (`db_delete_project` ne touche pas le
   dossier). Décision : **conserver par défaut**, signaler le dossier orphelin dans
   « Points d'attention » du monitoring, suppression explicite avec confirmation —
   jamais de destruction silencieuse de médias. → **Chantier 3**.

## 12. Plan d'implémentation — Chantier 1 (Fondations)

**Périmètre** : membres + rôles projet, flag d'interface par utilisateur, redirection au
login, accueil projets + page projet minimale, **scoping sécurité des API**. Hors
périmètre : vues/widgets (ch.2), cycle de vie/chargement (ch.3), ports (ch.4).

### Règle d'accès (la décision structurante)

- **Accès global** = rôles `admin` et `operator` (rien ne change pour eux).
- **Tout autre rôle** (`exploitant`, `multiview`, `viewer`) devient **scopé projet** : il
  n'atteint une ressource (vmid) que si le container appartient à un projet dont il est
  membre. Container sans `project_id` → refus pour les scopés (sauf son propre
  `monitor-u<uid>`).
- Rôle projet ⊂ capacités : `owner` (membres+tout), `editor` (config user des plugins,
  vues plus tard), `operator` (actions live), `viewer` (lecture/preview). Le rôle global
  reste le plafond (`plugins.operate` requis pour agir, comme aujourd'hui).
- Migration des `exploitant` existants : ils perdent l'accès global → **script/UI d'ajout
  aux projets** au déploiement (pas d'auto-enrôlement silencieux).

### A. DB (`database.py`, migrations idempotentes dans `init_db`)

1. `ALTER TABLE projects ADD COLUMN owner_id INTEGER` (NULL = legacy, admin de fait).
2. `CREATE TABLE project_members(project_id, user_id, role TEXT, PRIMARY KEY(project_id,
   user_id))` — rôles `owner|editor|operator|viewer`.
3. `ALTER TABLE users ADD COLUMN interface TEXT DEFAULT 'technique'` — les comptes
   existants ne changent pas de comportement ; défaut à la création : `technique` pour
   admin/operator, `projets` sinon.
4. Helpers : `db_project_members`, `db_set_project_member`, `db_remove_project_member`,
   `db_user_projects(uid)`, `db_project_role(pid, uid)`.

### B. Auth (`auth.py`)

1. `PROJECT_ROLES` + hiérarchie ; `has_global_access(user)` (= admin/operator).
2. `project_role_for(user, pid)` ; `require_project_role(min_role)` (décorateur, pid en
   arg de route) ; **`assert_vmid_access(vmid, min_role='viewer')`** — LE helper central :
   résout vmid→`project_id` (cache court, le proxy est un chemin chaud) → membership.
   Exception : `monitor_user_id == uid` toujours autorisé.
3. Exposer au front : `window.MXL_SCOPE` (`global|projects`) + rôles par projet.

### C. Scoping des API (le gros morceau — audit exhaustif requis à l'implémentation)

Points d'application connus (pour les utilisateurs scopés ; bypass si accès global) :
- `plugin_proxy` (`plugin_registry.py:529`) : `assert_vmid_access` — y compris les
  `read_endpoints` (aujourd'hui login seul).
- `plugin_config` (`plugin_registry.py:619`) : idem, rôle ≥ `editor`.
- Tally (`routes/__init__.py:1014`), plugin store (`__init__.py:756,774,781`), streams
  API (`streams_api.py`), `/api/containers/<vmid>/*` restants (metrics, config GET…).
- **Filtrage des listes** : `/api/containers`, `/api/plugins/instances`, `/api/alerts` →
  restreints aux projets du membre ; `/api/home/summary` → interdit aux scopés, remplacé
  par **`GET /api/projects/<pid>/summary`** (containers+statuts+fps+alertes du projet,
  même shape que home/summary pour réutiliser le JS).
- **Câblage** (`cabling.py`, fermé le 2026-10-23) : `wire`/`unwire` → consommateur dans un
  projet où l'on est ≥ `editor`, chaque shm (vidéo, audio, anc) produit par un conteneur d'un
  de CES projets, et le shm principal produit par le `from_vmid` annoncé (`_wire_scope_err`).
  `insert_udc`, `cables/clear`, snapshots de câblage → accès global seulement. Les appels
  internes à `_apply_wire` (ports, Ember+, watcher, restaurations) ne passent pas par ces
  gardes. Test : `tests/verif_cablage_scope.py`.
- Monitor (`monitor_routes.py`) : `/api/monitor/source|activate` → shm autorisé =
  produit par un container accessible (redériver depuis le vmid, ne jamais faire
  confiance au shm client).
- Pages : décorateur `@technical_page` sur les pages techniques → un `interface=projets`
  est redirigé vers l'accueil projets (l'URL directe ne suffit pas, cf. gating cosmétique).
- **Tests** : cookies forgés (cf. mémoire `flask_secret_key`) — matrice membre/non-membre/
  admin × proxy/config/tally/monitor/listes ; jamais en mutation sur la prod live.

### D. Login + chrome

1. `auth_pages.py` : après login, `interface == 'projets'` → redirect `/workspaces`
   (les techniques gardent `/`). Routes **en anglais** (décision 2026-07-05) :
   `/workspaces` (accueil) et `/workspace/<pid>` — pas de collision avec `/projects`
   (page technique de gestion des snapshots).
2. Gestion utilisateurs (Réglages) : sélecteur d'interface + **éditeur de membres par
   projet** (dans `projects.html`, visible owner/admin) — select de rôle, i18n, pas de
   checkbox nue.
3. `layout.html` : mode « chrome projet » (nav réduite : nom du projet, monitoring,
   logout) activé sur les pages projet ; nav technique masquée aux `interface=projets`.

### E. Accueil + page projet minimale

1. `GET /workspaces` → `workspaces.html` : cartes des projets du membre (nom, état
   chargé/déchargé, nb containers, rôle) ; vide → message explicite (« demandez à un
   admin »).
2. `GET /workspace/<pid>` (membre requis) : stub du futur workspace — résumé containers
   (via `/api/projects/<pid>/summary`, poll 5 s), bouton 📺 Monitoring par container,
   bandeau d'alertes du projet. C'est le squelette que le ch.2 remplira de vues.
3. i18n FR/EN complet (`window.t`, catalogue `projects.*`), thèmes OK (tokens existants).

### Ordre, dépendances, recette

A → B → C et D en parallèle → E. La recette porte sur : matrice d'accès (C, tests
forgés), parcours login/redirect/URL directe (D), accueil+page projet sur les 3 thèmes
et tablette (E), non-régression complète de l'UI technique pour admin/operator.

### État d'avancement — Chantier 6 (2026-07-06)

**Codé et testé (copie DB + container factice sans IP)** — Contrats **`actions`/`state`**
déclarés dans les manifestes mixer (take/pgm/pvw ; pgm/pvw/transition), recorder
(start/stop ; recording), player (play/pause/file/plnext/plprev ; paused/file), stills
(select ; file) — les endpoints d'action sont whitelistés au proxy. **Moteur
`app/macros.py`** : format `blocks/v1` (sérialisation du sous-ensemble structuré ;
`nodes/v2` viendra avec l'éditeur nodal), étapes action/sleep/set_var/if/parallel/macro
(profondeur ≤ 5), conditions const/var/state, gabarits `{{var}}`, exécution côté
orchestrateur en thread (1 run/macro, annulable, journal ring). **API**
(`routes/macros_api.py`) : action_catalog, actions/run (unitaire, cible bornée au
projet), states (batch feedback), vars (GET/POST), macros CRUD (**editor écrit,
operator exécute** — décision) + run/status/cancel. **Workspace** : widget **shotbox**
(boutons colorés, action ou macro, **feedback v1** — état+condition, poll mutualisé
1,5 s, bouton allumé), éditeur de bouton (catalogue container×action, params, feedback),
**éditeur Macro à blocs** (overlay ⚡ Macros, ≥ editor : action/pause/variable/si
imbriqué/appel de macro, ↑↓✕, test avec journal live). Variables posées par macros,
lisibles en conditions/gabarits.
**2e passe UX (2026-07-06)** : éditeur redessiné (blocs = phrases françaises, ajout par
boutons nommés, condition « Si <état|variable> <est égal à> <valeur> », outils au survol,
état vide pédagogique, ▶ Tester sans quitter l'éditeur) ; **choix nommés partout** —
contrat `options_from:"inputs"` (entrées câblées de l'instance, libellées par la source),
`options_endpoint` (listes VIVANTES de l'instance via proxy : fichiers/playlists du
player, images du stills, cache 15 s), `type:"bool"` (Oui/Non, coercition moteur),
`body` fixe par action (ex. delay all:true). Catalogues enrichis : mixer +keyer_toggle/
overlay/keyer_active, player 9 actions (file/loadlist en listes, plgoto, loop, speed),
stills (images en liste), delay (set_delay global), color_corrector (reset).
**3e passe (2026-07-06, deux agents en worktrees, fusion+intégration testées)** :
**macros SYSTÈME** — project_id NULL, CRUD/`/api/macros`+catalog global réservés accès
globaux, `published_to` validé, bouton OPAQUE 🛡 dans les projets destinataires
(exécutable ≥ operator, jamais le graph, invocateur journalisé), panneau admin
« Macros système » sur /projects (publication par projet en ios-toggle ; édition des
étapes : à venir via éditeur partagé) ; **déclencheurs permanents** — table
`project_triggers`, poller 1 s (front MONTANT, cooldown monotonic, ne meurt jamais,
démarré avec le contrôleur actif dans main.py), API (editor écrit, operator ne touche
que `enabled`), volet « Déclencheurs » de l'overlay ⚡ (une règle = une phrase :
« Quand <état|variable> <op> <valeur> → lancer <macro> (anti-rafale N ms) ») ;
**nœud `wait`** (moteur + bloc ⏳ Attendre dans l'éditeur, timeout journalisé).
Intégration vérifiée : trigger → macro (invocateur `trigger:<nom>`) → wait débloqué
par variable → set_var.
**4e passe (2026-07-06) — actions de SERVICES** : contrat `actions` dans les
manifest.json de services (availability project|system, options dynamiques via hook
`action_options`, exécution in-process via hook `run_action` — registre
`core_plugins.service_actions()/run_service_action()`). **tsl.set_label(ref, col,
texte)** — availability project, BORNÉE aux ressources du projet (ses ports ou les shm
produits par ses containers), réveille le distributor ; **skaarhoj.assign_key(panel,
touche, macro)** + **assign_preset** — système only, options panels/touches/presets
résolues, écrit le preset ACTIF + push live ; nouveau type de bouton Skaarhoj
**'macro'** (une touche physique lance une macro, invocateur `skaarhoj:<panel>:btn<n>`
— le pont surfaces physiques est ouvert). Moteur : étape action avec `service:` +
`exec_service_action` (gabarits {{var}} OK) ; catalogues projet/système exposent
`services` ; run unitaire accepte les actions de service ; UI : groupe « ⚙ Service »
dans les sélecteurs (macros + shotbox), params à options statiques et type `macro`.
**5e passe (2026-07-06) — bloc parallèle dans l'UI** : bloc « ∥ En parallèle » dans
l'éditeur à blocs (colonnes côte à côte, 2 branches à la création, ＋ branche / ✕ par
colonne dès la 3e, confirm si la branche a des étapes) ; chemins `N.branches.B` dans
`mSteps` ; un seul niveau de si/parallèle à l'UI (comme le si) ; i18n FR/EN ; moteur
inchangé (testé offline : 3 branches sleep 200 ms → 0,2 s, concurrentes).
**6e passe (2026-07-06) — UX shotbox** : (1) **modifier un bouton existant** — en mode
édition, un clic sur le bouton rouvre l'éditeur pré-rempli (cible, params, libellé,
couleur, feedback) et sauve en place ; (2) **quadrillage** — la shotbox devient une
grille (cases 84×56), boutons déplaçables au drag et agrandissables sur plusieurs
cases (poignée ◢, gx/gy/gw/gh persistés ; boutons existants = placement auto tant
qu'on ne les épingle pas) ; (3) **feedback par défaut cohérent avec l'action** —
nouveau volet `feedback` du contrat `actions` ({state, value|value_from, op?} — posé
sur recorder start/stop, player play/pause, stills select) + heuristique état homonyme
(mixer pgm(2) → « pgm est égal à 2 », player file) : proposé d'office dans l'éditeur,
recalculé quand le param change, personnalisable (on ne touche plus dès que
l'utilisateur modifie) ; (4) **aimantation** — select « Aimanter : libre/bas/gauche/
droite » dans l'en-tête du widget (mode édition, `dock` persisté dans la vue) ; en
vue, la shotbox aimantée quitte la grille pour une barre fixe au bord choisi, toujours
visible au défilement (padding du contenu ajusté).
**7e passe (2026-07-10, deux agents) — couverture des catalogues d'actions** :
(1) plugins jusqu'ici muets : **streamer** (re-pointer la source, état shm),
**avsync** (beat/bip/habillage/texte + 4 états), **udc** (format de sortie,
genlock), **transcoder** (bascule watchfolder + annuler un job, listes VIVANTES
via options_endpoint /watchfolders et /jobs), **stream_in** (activer/reconnecter/
changer d'URL) ; enrichis : **delay** (retard par canal, liaison), **color_corrector**
(réglages partiels, champs `optional:true` → seuls les champs saisis sont envoyés).
Écarts assumés : pas d'action sans endpoint réel côté script (destinations du
streamer figées au déploiement), pas de /submit transcoder (trop risqué pour un
bouton). (2) **multiview/split** : action **tally(fenêtre, couleur)** — support
générique d'un `port` par action (défaut 8082 ; tally sur :8080), forme simple
sans slot ajoutée au script multiview (0.23.0, redéploiement requis) ; **rappel de
mémoire/layout** via le nouveau volet déclaratif **`"core":"recall"`** (route
`recall_preset` réutilisée, résolution par id, `duration_ms` optionnel) + options
résolues côté orchestrateur (`options_from:"recall"` → options statiques au front,
zéro changement d'UI). Bilan registre : 13 plugins, 37 actions / 25 états, zéro
erreur de scan.
**8e passe (2026-07-10) — contrôlables génériques + picker recherchable** :
plus de liste figée — le catalogue DÉCOUVRE ce qu'un module expose. Chaque
entrée porte `source` (curated|config|endpoint|discovered). (1) **Réglages** :
les champs `config_schema` deviennent des contrôlables (valeur courante gatée
par plugins.operate ; écriture via le chemin plugin_config EXTRAIT en
`apply_plugin_config` — scopes user/system respectés, `containers.deploy`
capturé au déclenchement, garde 2110_io/anti-rafale conservées). (2) **Avancé** :
les `control.endpoints` non couverts par une action curatée → « action
avancée » `advanced:true` (POST params libres, whitelist inchangée ; formes
liste ET dict 2110_io normalisées par `plugins.control_post_endpoints`).
(3) **États découverts** : `GET /api/containers/<vmid>/discover_states`
interroge les `read_endpoints` (même chemin que fetch_state), aplatit le JSON
en chemins pointés (profondeur 4, 150 max, listes bornées à 8), cache 15 s,
échec silencieux + flag `partial` ; utilisables dans conditions/feedback/
triggers via l'opérande `{kind:state, endpoint, path}` (borné aux
read_endpoints). Moteur : étapes **`config`** et **`post`** (gabarits {{var}},
coercition prudente), run unitaire shotbox `kind:config|post`. **Workspace** :
les <select> cible/état (bloc ▶, bouton shotbox, si/attendre, déclencheurs)
remplacés par un **picker recherchable** (accents/casse ignorés, groupé par
machine + badge type, sections Actions/Réglages/Avancé repliée/États
découverts, clavier ↑↓⏎Échap + cibles 44 px, overlay ancré au champ) ; le
bloc ▶ mute en Réglage (champ typé, ios-toggle pour les booléens) ou Appel
avancé (clé/valeur + rappel du endpoint) ; i18n FR/EN. Testé offline : 19/19
unitaires (copie DB, HTTP mocké) + E2E Flask session forgée (catalogue 4
containers / 27 réglages / 17 avancés sur le projet réel, discover, states
batch borné, run config persisté sans deploy, non-régression curaté).
**9e passe (2026-07-10) — moteur nodal nodes/v2** : le format graphe arrive EN PLUS
de blocks/v1 (décision : les deux moteurs cohabitent, blocks/v1 STRICTEMENT inchangé —
les feuilles action/config/post/sleep/wait/set_var sont extraites telles quelles dans
`_exec_leaf_step`, partagées). **Format** : `{format:"nodes/v2", nodes:[{id,type,params,
x,y}], edges:[{from,port,to}]}` ; types entry (mode manual|trigger)/action/config/post/
sleep/set_var/wait/macro/cond (ports 0=vrai 1=faux)/choice (branches dans l'ordre +
port défaut)/join (all|any). **Interpréteur à jetons** (`_run_graph`) : un jeton par
entrée activée, fan-out = arêtes multiples d'un même port (threads, modèle du bloc
parallel), boucles autorisées mais bornées (1000 passages/nœud), annulation/journal/
profondeur macro (≤5) inchangés ; événements par nœud (started/finished/error, champ
`node`/`event` au journal) + `active_nodes` dans le snapshot status (surlignage live
de la future UI). **Choix pragmatiques documentés** : join all = compteur d'arrivées
vs nb d'arêtes entrantes ATTEIGNABLES depuis les entrées activées (statique — une
branche tuée en amont par un cond ne déclenchera pas la jointure), ré-armé après
déclenchement (boucles) ; join any = premier arrivé passe, les suivants meurent (tout
le run) ; entrées `trigger` stockées/validées mais poller DIFFÉRÉ — un trigger
classique pointant sur une macro nodale démarre ses entrées manual. **Conversion** :
`blocks_to_graph` (compilation sans perte, layout en couches gauche→droite ; un join
« any » transparent est inséré comme convergence explicite quand un parallel suit un
if), `graph_to_blocks`/`graph_is_structured` (série-parallèle bien imbriqué : une
entry manual, régions refermées, pas de saut/cycle/choice ; sinon badge « avancé » =
vue nodale seulement ; round-trip blocks→graph→blocks identique, modulo then/else/
branches absents normalisés en []). **API** : CRUD valide les deux formats
(`validate_graph` : ids uniques, arêtes/ports, ≥1 entry), `format`+`structured`
exposés (listes/GET), nouveau GET unitaire projet/système avec `?as=graph` (champ
`nodal` = compilation à la volée), run accepte `entry_id` ; run/status/cancel
inchangés pour l'appelant. Aucune migration silencieuse : une macro blocks reste en
blocks tant qu'elle est éditée en blocs. **Tests offline 12/12** (copie DB, HTTP
mocké) : validation, round-trip synthétique + toutes les macros blocks de la DB,
détection avancé (choice/multi-entrées/trigger/cycle/saut), run complet entry→action→
cond→fan-out 3 branches→join all→set_var (aval exécuté 1 fois), join any, boucle
bornée, annulation (+active_nodes), choice+défaut, entry_id, non-régression blocks
(sleep/set_var/if/parallel concurrent/wait/action), blocks→sous-macro nodale +
récursion bornée. Reste (étape suivante) : l'éditeur Scénario nodal (UI) et
l'évaluation des entrées trigger par le poller.
**10e passe (2026-07-10) — éditeur Scénario nodal (UI)** : la vue canvas de la
maquette validée, adaptée aux tokens du produit (variables OKLCH locales
`--scn-*` par type de nœud, déclinées light L−0.33 comme la charte ; canvas sur
`--canvas-bg`). **Bascule Blocs ⇄ Scénario** (segmented control `.wsp-seg`) dans
l'éditeur de macros : macro structurée = deux vues (compilation `?as=graph` ;
nouveau **`?as=blocks`** symétrique dans macros_api pour ouvrir en Blocs une
nodes/v2 structurée) ; macro avancée = Blocs désactivé AVEC explication visible
+ badge « avancé » ; sauvegarde Scénario = **nodes/v2 (x,y inclus)**, sauvegarde
Blocs = blocks/v1 — jamais de migration silencieuse (bascule cross-format =
confirm ; ▶ Tester sans modif n'écrit rien). **Canvas** : pan/zoom/recentrer,
drag des nœuds par l'en-tête, arêtes béziers (drag port ○→port avec aimantation,
sélection + ✕/Suppr), suppression de nœud avec confirm si câblé, pointer-events
tactiles (ports 40 px). **Palette** : les 12 types (dont entrée déclencheur
affichée « évalué plus tard », testable seule via `entry_id`). **Panneau
d'édition du nœud** (latéral droit, écart assumé vs l'inspecteur read-only de
la maquette) : RÉUTILISE picker recherchable/paramFieldHtml/cfgFieldHtml/
mOpSelect de l'éditeur à blocs — zéro duplication ; nœud choix = branches
＋/✕ avec réindexation des ports/arêtes. **Exécution live** : run réel + poll
→ anneau accent sur `active_nodes`, pulsation des arêtes entrantes (gatée
prefers-reduced-motion), journal `{node,event}` en tiroir bas, ✓/✕ final par
nœud, ⏹ annulation. **Macros système ÉDITABLES** (enfin) : section 🛡 de
l'overlay ⚡ (accès global, flag `is_global` posé par pages.py) → même éditeur
sur `/api/macros` + catalogue global (CATALOG/VARS swappés le temps de
l'édition ; vue Scénario seule pour elles, expliqué). i18n FR/EN
(`projects.scn_*`). **Tests offline 10/10** (tests/test_scenario_ui.py :
rendu Jinja session forgée, node --check des scripts inline RENDUS, cycle API
blocks→`?as=graph`→PATCH nodes/v2→`?as=blocks` round-trip, avancé 2 entrées,
graphe invalide 400, cycle système) + non-régression smoke 75/75 et nodal 12/12.
**Reste (ch.6 suite)** : entrées trigger du graphe évaluées par le poller
(différé 9e passe) ; actions cœur orchestrateur (charger/décharger un
projet…) ; banc réel shotbox/macros/triggers/Skaarhoj (+ redéploiement
multiview ≥ 0.23.0) + vérif visuelle du picker ET de l'éditeur Scénario sur
tablette (3 thèmes).

### État d'avancement — Chantiers 4 & 5 (2026-07-05, EN COURS)

**Fait et testé (copie DB)** — Ch.4 : table `project_ports` + CRUD + API
(`/api/ports` global, `/api/projects/<pid>/ports` : editor déclare, binding SOURCE
réservé admin, sortie interne d'une DEST par l'editor) ; **rebind à chaud**
(`_rewire_shm_consumers` via `_apply_wire`/`_collect_current_edges` — internes au
projet pour une source, externes pour une dest) ; panneau « Ports » dans le tiroir du
workspace. Ch.5 : `projects.tally_base` auto-alloué (pas de 3, évite les bases des
connexions TSL — REMPLACÉ depuis par les niveaux nommés `tally_levels`, cf. §6) ; `tsl_connections` + `direction in|out`, `dest_host`, `project_id`
(niveau = celui du projet) ; service tsl : `resolve_ref("port:<id>")` (mapping/labels
sur adresses stables), **pseudo-connexions par projet** (rouge=LH/vert=RH, index d'une
source = port.ord), **défaut : une fenêtre multiview/overlay sans niveau explicite
hérite du niveau du projet de son container** ; `_TslClient` sortant (encodeur TSL 5.0
miroir vérifié en round-trip, diff+keepalive 5 s, reconnexion 3 s) ; **publisher
mixer** (params `tally_emit`/`tally_level_base` scope system, PGM=rouge/PVW=vert,
résolution entrée→shm→port→index, purge propre par base) ; UI Réglages→TSL (Sens/Cible,
niveau=projet, ports dans le mapping) ; création d'un container directement DANS un
projet (`POST /api/containers` + `project_id` → bind média + tally du projet).

**Complété (2026-07-05, 2e passe)** — Décision : le **2110_io est LIÉ AU NŒUD, jamais à
un projet** (`PROJECT_EXCLUDED_TYPES` += 2110_io : filtré à la sauvegarde, sauté au
restore avec message, PATCH project refusé 400, sélecteur projet masqué à la création,
appartenance par snapshot legacy ignorée par type, exclu du re-snapshot vivant). En
compensation : **config-snapshots PAR CONTAINER** (génériques, tous types) —
`GET/POST /api/containers/<vmid>/config_snapshots` + `/restore` (redeploy, confirm 409
pour un 2110_io en marche) + DELETE, stockés en plugin_store scope `cfgsnap:<vmid>`,
UI dans le panneau ⚙ (visible dès `containers.deploy`, même sans champs système).
**Vue d'ensemble Câbles** : `/api/home/summary?view=projects` — chaque projet replié en
module boîte noire (vmid synthétique négatif, badge état chargé/déchargé, `consumes` =
ports sources bindés, `produces` = destinations publiées ; containers du projet masqués,
arêtes externes/inter-projets recalculées — testé : arête producteur→module OK) ;
toggle « ▣ Vue projets » sur la page Câbles (persisté). **Lignes « port:<id> » dans
/labels** (phase 1.5, rendu ⚓, labels+index TSL comme une source). Le sélecteur projet
de la palette de création est désormais visible pour tous les types (sauf 2110_io) et
transmis sur le chemin Docker.
**Reste** : banc réel TSL out (UMD) + publisher mixer + rebind à chaud sur le cluster ;
Ember+ différé (usage à documenter avec l'utilisateur).

### État d'avancement — Chantier 3 (2026-07-05)

**Codé et testé** (copie de DB, sans toucher au cluster) : colonne `projects.state`
(saved|loading|active|error|unloading) + verrou par projet (anti double-chargement,
états posés dans restaurer_projet/detruire_containers_projet, badge d'état sur
/workspaces, /projects et le summary) ; **multicast TX jamais rejoué** d'un snapshot
(`_purge_tx_multicast` avant deploy → réallocation par le hook before_deploy, sessions
RX intactes) ; **pré-vol de capacité refus-bloquant** dans `planifier_restore`
(`verifier_capacite` : cœurs agrégés si un pool cpuset existe, un seul moteur 2110_io
par nœud, adresses multicast libres vs demandées — affiché dans la preview de restore) ;
**dossier média garanti** au chargement ET à l'import (`_ensure_media_dir`) ;
**export v2** (`bobi.studio.project.v2` : + vues) avec import rétrocompatible v1 ;
**remap des vues** après chargement (widgets re-pointés vmid+instance_uuid, uniquement
si l'ancienne cible a disparu — une copie ne vole pas les vues des originaux) ;
destruction par `project_id` (couvre les vmids remappés) + snapshot vivant.
**« Projet vivant » codé et testé** (2026-07-05) : table `project_versions` (rétention
30 versions auto, nommées illimitées) ; re-snapshot **automatique débouncé 10 s** —
hook dans `db_update_deploy_config` → `notify_container_changed` → timer par projet →
`resnapshot_projet` (containers live du projet, no-op si identique ou pendant
loading/unloading, ancien snapshot poussé en version auto) ; API
`/api/projects/<pid>/versions` (liste=viewer, nommer/rollback/suppr=owner) ; rollback =
snapshot remplacé (live intact — recharger applique), état courant préservé en auto ;
UI panneau « Versions » sur /projects. Reste (différé) : banc réel de
chargement/déchargement sur le cluster, budget fin de queues XDP au pré-vol (gardé à
chaud par /flows).

### État d'avancement — Chantier 2 (2026-07-05)

**Codé et testé** : table `project_views` + CRUD ; API
`/api/projects/<pid>/views[...]` (droits : ≥ operator vues privées, ≥ editor partage
et édition des vues `edit_shared`, propriétaire/owner/admin pour partage+suppression,
duplication pour tous les membres ≥ operator) ; workspace = grille 12 colonnes
composable (drag/resize pointer-events, tactile) avec mode édition (palette latérale,
barre nom+partage `.ios-toggle`), sélecteur de vues dans le menu flottant (vue « auto »
= tuiles statut, mémoire localStorage par projet), widgets niveau 1 : `status` (tuile
badge/fps/📺), `plugin` (UI complète via `ui/html` + `MXLPlugins[type].mount`, assets
versionnés, extra multiview géré, **une UI par type et par vue** — les control.js sont
des singletons par type), `monitor` (iframe WHEP du monitor personnel + boutons source
+ heartbeat 30 s). Le summary porte `instance_uuid` ; les widgets référencent
instance_uuid avec repli vmid. Limites connues : le poll 5 s ne rafraîchit que les
tuiles statut (jamais de re-mount des plugins) ; pas encore de widgets fins (ch. 6) ni
d'alertes par-projet.

### État d'avancement — Chantier 1 (2026-07-05)

**A→E codés et testés** (sessions forgées sur une copie de la DB de prod, matrice
membre/non-membre/admin entièrement verte ; DB live non touchée — les migrations
s'appliquent au redémarrage du service). Reste : recette visuelle (3 thèmes + tablette,
faite par l'utilisateur), ajout des exploitants existants comme membres de leurs
projets, et alertes par-projet (différées au ch.2/3 — un scopé reçoit une liste vide).
Gris assumé : le plugin store (`/api/plugins/<type>/store`) reste gardé par
`plugins.operate` sans scoping par projet (presets partagés) — à raffiner au ch.2.
