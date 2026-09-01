# Agent-nœud Bobi.Studio — contrat d'API & modèle de capacités

> **Statut : IMPLÉMENTÉ (en production)** — ce document est le **contrat d'API** d'une
> implémentation vivante. Version courante : **0.14.x**. Code : `node_agent/agent.py`
> (côté nœud) ; client côté contrôleur : `app/node_driver.py`. Origine : spec Phase A de la
> séparation control-plane / node-plane (cf. mémoires `control-node-separation-next`,
> `standalone-node-agent-design`) — le contrat ci-dessous fait foi et suit le code.

## 1. Rôle & positionnement

L'**agent-nœud** (`bobi-node-agent`) est un **daemon unique par machine**, seul composant à
installer sur un hôte Linux nu pour en faire un nœud Bobi.Studio **sans Proxmox**. Il offre au
contrôleur une **API HTTP scopée par token** qui remplace l'accès **root-SSH + API Proxmox**
utilisé aujourd'hui.

Il **absorbe** tout ce que le contrôleur fait actuellement en `ssh_run` sur l'hôte :
- `docker run/stop/rm/inspect` (cf. `docker_compute.deploy_compute`, `docker_driver.deploy_docker`) ;
- services hôte : `ip link … xdp off` (teardown MTL), `mkdir` de binds, montage `/dev/hugepages` ;
- **PTP** (`ptp4l`/`phc2sys`) aujourd'hui piloté en SSH par `app/ptp.py`.

### Ce qu'il N'EST PAS (garde-fou de périmètre)
- **≠ agent par-conteneur** (`script_templates/agent.py`, `:8081 deploy/start/stop/status`,
  `:8080` métriques) : celui-ci reste **dans** chaque conteneur et exécute le `script.py` du
  plugin. L'agent-nœud ne rend/exécute **aucun** script de plugin.
- **≠ contrôleur MTL** (`plugins/2110_io/docker/controller.py`, `:8080`/`:8081/nmos/subscribe`)
  qui vit **dans** le conteneur MTL.
- L'agent-nœud ne prend **aucune décision de routage broadcast** : il exécute des ordres de
  cycle de vie + expose santé/capacités. La logique reste au contrôleur.

## 2. Modèle de capacités

Un nœud = un **hôte** + un **ensemble de capacités** choisies **à l'installation** (et
ré-éditables en relançant l'installeur). L'agent ne provisionne et n'expose que le sélectionné.

| Capacité  | Provisionne (hôte) | Image(s) | Réseau conteneur | Notes |
|-----------|--------------------|----------|------------------|-------|
| `io2110`  | E810/ice, file queues, **hugepages**, bpffs, **PTP** (ptp4l+phc2sys sur PHC E810), MtlManager | `bobi-mtl` | `host` (`--privileged`) | 1 conteneur MTL/nœud (`--network host`). Requiert NIC compatible. |
| `compute` | réseau **macvlan** | `bobi-compute` | macvlan | plugins numpy (color_corrector, mixer, multiview, udc, avsync, delay, split, pyramide…) |
| `media`   | macvlan + dossier média (`/mnt/media`) | `bobi-media` | macvlan | player/recorder/transcoder/stills (GStreamer+ffmpeg) |
| `webrtc`  | macvlan | `bobi-webrtc` | macvlan | passerelle MediaMTX (binaire pré-baké) |
| `gpu`     | pilote NVIDIA Debian (DKMS) + `nvidia-smi` + `libcuda1` + `nvidia-container-toolkit` + runtime docker `nvidia` | `bobi-compute-gpu` (buildée SUR le nœud) | macvlan | **REBOOT requis** (chargement du module). Rattrapable après coup, cf. ci-dessous. |

- **PTP** est rattaché à `io2110` (le nœud qui fait du 2110 doit être PTP-locké). Un futur drapeau
  pourra distinguer « source PTP » vs « esclave ».
- Les capacités macvlan (`compute`/`media`/`webrtc`) partagent le même réseau macvlan du nœud.
- `io2110` (host-net) et les capacités macvlan **coexistent** sur la même machine (nœud tout-en-un).
- **Réseau containers différé (phase 2)** : le macvlan n'est plus figé à l'install. `install-node.sh`
  saute sa création si parent/subnet absents ; l'orchestrateur l'assigne APRÈS l'enrôlement en
  choisissant la carte parent dans l'inventaire `nics` de `/v1/capabilities`, puis crée le réseau via
  `/v1/host/networks/ensure`. subnet/passerelle = pool d'IP cluster ; VLAN = `net_vlan_tag`.

### Rattraper une capacité oubliée à l'enrôlement (`--add-caps`)

Le profil d'enrôlement est **consommé une fois** : une capacité non cochée n'était pas rattrapable
sans réinstaller. Une liste `capabilities` périmée n'est pas cosmétique — elle est MUETTE et
structurante : `node_health` ne sonde pas le PTP d'un nœud sans `io2110` déclaré,
`images._node_needs_image` ne le compte pas comme cible de build, `node_recovery` s'y réfère.

`install-node.sh --add-caps <csv>` provisionne **la seule capacité demandée** puis sort. Il
**FUSIONNE** la capacité dans `config.json` — le reste du fichier est préservé à l'octet près. C'est
la raison d'être du mode : une install complète relancée **réécrit `config.json` en entier**
(`mtl_iface`, `lcores`, `macvlan_network` blanchis si les arguments d'origine ne sont pas tous
reproduits — or le parent macvlan n'est pas en base) et, **sans `--token`, régénère le token de
l'agent**, ce qui fait perdre le nœud au contrôleur.

- **Depuis l'UI** : Réglages → nœud → ligne « Capacités » — elle n'affiche que les capacités
  PRÉSENTES ; le bouton « ＋ Ajouter » ouvre un menu listant les manquantes (avec la mention du
  redémarrage quand la capacité l'impose). `POST/GET /api/nodes/<id>/capabilities` — le contrôleur pipe le script
  sur `stdin` de `/v1/host/exec` (aucune dépendance à un payload présent sur le nœud), puis rappelle
  `node_driver.register` pour resynchroniser la base depuis `/v1/capabilities`.
- **Les cinq capacités sont rattrapables.** Chacune est une fonction `provision_<cap>()` du script,
  appelée aussi bien par l'install complète que par `--add-caps` — un seul chemin de code.
  Un nom hors des cinq **refuse net**.
- **Les réglages amenés par la capacité voyagent avec elle** : `_add_cap_args()` reconstruit
  `--mtl-iface`/`--lcores`/`--hugepages`/`--ptp-domain`/`--kernel-pkg` (io2110, depuis `nodes` +
  `enroll_profile` + réglages cluster) et `--media-mount` (media). Ce qui n'est pas en base est
  **omis**, jamais deviné : le script bascule alors dans son mode différé et le dit.
- **`io2110` : le blob DDP E810 est déposé séparément** (`/v1/host/exec` + `base64 -d`, puis
  `DDP_SRC=…`) — 1,4 Mo vendorés dans le dépôt qui ne voyagent pas avec un script pipé sur stdin.
  Sans DDP, `ice` démarre en Safe Mode (ni PTP matériel ni steering) → dépôt raté = rattrapage
  **annulé sans toucher à l'hôte**, plutôt qu'une capacité 2110 muette.
- **★ `io2110` DÉSACTIVE chrony sur le nœud.** L'horloge d'un nœud 2110 porte du TAI, posée par le
  client PTP interne de libmtl (§2a bis) ; sur un nœud qui devient io2110 *après coup*, chrony est
  déjà installé et deux disciplines battraient sur la même horloge — chrony ramènerait REALTIME vers
  l'UTC, soit **37 s d'erreur pour le moteur**, sans que rien ne le signale. Conséquence assumée et
  annoncée : tant que le moteur MTL ne tourne pas, ce nœud n'a plus de discipline d'horloge.
- **Réseau containers absent ≠ échec.** Le macvlan est différé par conception (la carte parente se
  choisit, ne se devine pas) : le script **avertit** sans lever son drapeau d'échec, et
  l'orchestrateur relaie l'étape restante dans ses « Suites » — sortir en `rc≠0` pousserait à
  ré-appuyer sur le bouton alors que l'action utile est ailleurs.
- **Ajout seulement, jamais retrait** : on ne désinstalle pas un pilote, on ne rend pas des
  hugepages, on ne détruit pas un macvlan. Un « décocher » qui n'éditerait que la déclaration
  laisserait la base mentir sur l'état de l'hôte.
- **L'échec est porté par le code de sortie.** Les étapes restent best-effort (une install pilote
  ratée ne doit pas bloquer l'agent) mais posent un drapeau `<CAP>_FAIL` qui devient le `rc`. Sans
  ça, l'UI afficherait « ✓ fait » sur un stack absent — exactement le mensonge dell-1
  (`gpu_capable=1` alors que CUDA était cassé). Le vert GPU reste conditionné à la **sonde réelle**
  (`_probe_gpu` : nvidia-smi + runtime + CUDA userspace), jamais au succès du bouton.
- **Suites automatiques** : après un rattrapage `compute`/`media`/`webrtc` réussi, le contrôleur
  pousse les images runtime PARTAGÉES (`_provision_shared_images`, appelé **après** le resync
  puisqu'il lit les capacités relues) et signale si le réseau containers manque encore.

## 3. Modifications du schéma `nodes`

Ajouts (migrations idempotentes dans `init_db`, motif existant) :

| Colonne          | Type | Rôle |
|------------------|------|------|
| `capabilities`   | TEXT (JSON array) | ex. `["io2110","compute"]` — **source de vérité** de l'éligibilité |
| `agent_url`      | TEXT | `http://<host>:<port>` de l'agent |
| `agent_token`    | TEXT | secret partagé (ou réf. settings) |
| `agent_version`  | TEXT | version de l'agent (compat) |
| `last_seen`      | TEXT | horodatage du dernier heartbeat OK |

- Les champs ad hoc actuels (`compute_image`, `media_image`, `docker_network`, `mtl_iface`,
  `mtl_capable`, `lcores`, `mxl_mount`) **restent** (rétro-compat) mais sont **dérivés/peuplés par
  l'agent** via `/capabilities`. L'éligibilité (`docker_compute.pick_compute_node`,
  `_eligible_docker_nodes`…) migre vers un test sur `capabilities`.
- `kind` conserve sa valeur (`docker`/`docker-mtl`/`proxmox-lxc`) ; un nœud à agent ⇒ `kind`
  reste mais le dispatch passe par le client agent (cf. §7).

## 4. API HTTP — contrat `/v1`

- **Base** : `http://<host>:<port>/v1` (port par défaut **9100**, configurable).
- **Auth** : en-tête `X-MXL-Node-Token: <token>` sur **toutes** les routes (sauf `/v1/ping`).
  Comparaison à temps constant (`hmac.compare_digest`), cf. modèle `_update_token_ok`.
- **Erreurs** : JSON `{ "ok": false, "error": "<msg>" }` + code HTTP (400 requête, 401 token,
  404 introuvable, 409 conflit, 500 hôte/docker, 503 capacité non provisionnée).
- **Idempotence** : `POST /containers` avec un `name` existant **réconcilie** (recrée si la spec
  diffère, no-op sinon) — pas d'erreur 409 par défaut.

### 4.1 Découverte & santé (non authentifié : `/ping` seulement)
```
GET /v1/ping            → { "agent": "bobi-node-agent", "version": "x.y.z" }   (liveness, no token)
GET /v1/capabilities    → (cf. §5) alimente la ligne nodes
GET /v1/health          → (cf. §6) heartbeat riche pour le dashboard
```

### 4.2 Cycle de vie des conteneurs
```
POST /v1/containers                      → crée+démarre (spec §4.4) ; idempotent
GET  /v1/containers                      → [ {name,status,image,started_at} ]
GET  /v1/containers/{name}/status        → { status: running|exited|absent, exit_code, ... }
POST /v1/containers/{name}/start
POST /v1/containers/{name}/stop          → { timeout_s? }
POST /v1/containers/{name}/destroy       → { } (rm -f ; + xdp-off si conteneur io2110)
GET  /v1/containers/{name}/logs?tail=N   → { lines: [...] }   (diagnostic crash, cf. audit B3)
```
**Mémoire locale des specs (agent ≥ 0.20.0)** : `POST /v1/containers` écrit la spec reçue dans
`/var/lib/bobi-node-agent/specs/<name>.json` (dossier `0700`, fichier `0600`) ; `stop` et `destroy`
l'**oublient** — dans les deux cas l'intention devient « arrêté », et recréer derrière un arrêt
volontaire ferait de l'agent un second décideur. Sert au seul cas que Docker ne couvre pas : un
conteneur en `--rm` (moteur MTL) qui a DISPARU (cf. §4.5). ⚠ La spec contient le matériel mTLS du
conteneur et son jeton d'agent ; la portée d'une fuite reste ce conteneur-là, dont l'identité
(`bobi://container/<vmid>`) est justement celle que les agents refusent.

### 4.5 Chien de garde de script (agent ≥ 0.20.0)

**Transport : `docker exec`, pas le réseau (0.21.0).** Nos conteneurs sont en macvlan, et une
interface macvlan enfant ne parle **jamais** à la pile de son interface parente : un nœud joint les
conteneurs de ses voisins, jamais les siens (mesuré sur dl360-1 : 100 % de perte vers son propre
conteneur, 0 % vers celui d'en face). L'agent-nœud exécute donc une sonde Python **dans** le
conteneur, qui appelle `127.0.0.1:8081` avec le matériel déjà présent là (`/etc/bobi-tls`,
`$MXL_AGENT_TOKEN`). Côté conteneur, ce chemin est ouvert par l'exemption **loopback** (§8), bornée
aux deux mêmes endpoints. Rien à router, rien à distribuer, et le mécanisme reste valable en
bridge, en ipvlan et en topologie séparée. L'ouverture par cert de nœud (`bobi://node/<id>`) reste
en place pour les topologies où l'hôte peut joindre ses conteneurs.

Boucle de fond, période 15 s. Couvre **un seul cas** : un script mort à l'intérieur d'un conteneur
qui, lui, tourne toujours (`script_stopped`). Docker ne le voit pas — son `--restart` surveille le
PID 1, l'agent par-conteneur, qui va très bien — et c'est le contrôleur qui relève normalement ce
cas (`app/metrics.py`). Le chien de garde prend le relais **quand le contrôleur est absent**.

Garde-fous, dans l'ordre où ils s'appliquent :
1. **Inhibition** — n'agit que si aucune requête authentifiée du contrôleur depuis 60 s
   (`LAST_CONTROLLER`, déjà tenu par `_auth_ok`). Il **observe** en permanence, il n'**agit** que
   dans le silence.
2. **Intention** — ne relance que ce qu'il a vu tourner au tour précédent. Un script arrêté avant la
   coupure reste arrêté. C'est ce qui remplace un drapeau `supervise` qu'il faudrait persister.
3. **Plafond** — backoff exponentiel (10 s doublés, plafond 300 s) puis abandon définitif à 5
   tentatives. Un script qui meurt en boucle est un diagnostic, pas une chose à relancer.
4. **Périmètre** — conteneurs `bobi-*` en réseau macvlan uniquement. Les `--network host` (moteur
   2110) sont ignorés : leur `:8081` est hors de ce contrat, et relancer un moteur DPDK à l'aveugle
   masquerait une panne matérielle.

Ce qu'il ne fait **pas** : redéployer (rendre un script exige `deploy_config` et le registre de
plugins, donc la base — `/status.path` vide ⇒ il constate et attend), recâbler, réallouer, décider.

Coût maîtrisé (0.20.1) : réseau/IP/jeton sont figés au `docker run` → `docker inspect` **mémoïsé
pour la vie du conteneur** (les relire à chaque tour faisait ~3 processus docker par seconde sur un
nœud à 47 conteneurs, en permanence, sur la machine qui compose en direct) ; et un conteneur qui ne
répond jamais — image antérieure à l'ouverture d'identité, donc notre cert refusé — est **espacé
jusqu'à 10 min** au lieu d'être re-sollicité toutes les 15 s pour toujours.

Réglages `config.json` : `watchdog` (défaut **actif**), `watchdog_recreate` (défaut **inactif** —
recréation d'un conteneur `--rm` disparu depuis la spec locale, UNE tentative ; à armer site par
site en connaissance de cause). Tout ce qu'il fait est journalisé et remonté dans `/v1/health`.

### 4.3 Services hôte (gatés par capacité → 503 si absente)
```
POST /v1/host/xdp-off          { iface }                 (io2110)
POST /v1/host/images/ensure    { image, source? }        → pull/load si absente
GET  /v1/host/ptp              → { running, locked, gm_id, offset_ns, iface }   (io2110)
POST /v1/host/ptp/{start|stop|restart}                   (io2110)
POST /v1/host/networks/ensure  { name, parent, subnet, gateway, vlan?, ip_range? }  → macvlan idempotent
```

### 4.3 ter Horloge (`/v1/host/clock`, agent ≥ 0.18.0) — le seul endpoint où le TEMPS DE RÉPONSE FAIT PARTIE DU CONTRAT
```
GET  /v1/host/clock  → { ok, version, recv_utc_ns, recv_tai_ns, send_utc_ns, send_tai_ns }
```
`recv_*` sont lues à l'entrée du handler, `send_*` juste avant l'écriture de la réponse : le
contrôleur en tire un offset par le **modèle NTP à quatre estampilles** (`app/clocks.py`,
`_echange_clock`), ce qui **retire de la mesure le temps passé dans le nœud**.

Trois propriétés font la précision de cet endpoint. Les casser ne provoque aucune erreur — juste
une page Horloges qui remesure sa propre latence, ce qu'elle a fait pendant des mois :

1. **Deux appels système, rien d'autre.** Ni `subprocess`, ni lecture de fichier, ni journalisation
   dans le chemin. `CLOCK_TAI − CLOCK_REALTIME` **est** le `tai_offset` du noyau : le dériver évite
   un `adjtimex`.
2. **Tête et corps en UN SEUL `write`.** En deux écritures, la seconde attend l'ACK de la première
   (Nagle) : jusqu'à 40 ms de retard, soit l'ordre de grandeur qu'on prétend mesurer.
3. **`send_*` lue au plus tard**, juste avant l'écriture — ce qui la suit n'est que du formatage.

Côté contrôleur, symétriquement : la connexion (TCP + poignée de main TLS) est établie **avant**
le démarrage du chronomètre. Sinon son coût, entièrement à l'aller, redevient un biais.

Agent < 0.18.0 → 404, et le contrôleur retombe sur sa sonde shell (`host/exec`), avec ±25 ms
d'incertitude annoncée. C'est le champ `mesure` (`agent_natif` / `sonde_shell`) qui le dit.

### 4.3 bis Images & exec hôte (non gatés par capacité)
```
GET  /v1/host/images/export?tag=…   → stream binaire `docker save <tag>` (application/octet-stream)
POST /v1/host/images/load           corps = archive binaire (docker save) → `docker load`
POST /v1/host/images/build?tag=…    corps = contexte tar.gz (Dockerfile à la racine) → `docker build` local
POST /v1/host/exec                  { cmd, input?, timeout? } → { rc, stdout, stderr }
```
- **`images/export`** (GET, `?tag=` requis) : streame `docker save <tag>` en
  `application/octet-stream`. L'agent est en HTTP/1.0 → **corps délimité par la fermeture de
  connexion** (ni Content-Length ni chunked ; le client lit jusqu'à EOF). `400` sans tag, `404`
  si l'image est absente. Usage : relais **build-on-node → distribution** — l'orchestrateur
  buffer l'archive en fichier temporaire puis la POST vers les autres nœuds via `images/load`
  (évite tout registry).
- **`images/load`** (POST) : corps **binaire** (archive `docker save`), `Content-Length`
  requis (`400` sinon), lu par morceaux (pas de 2 Go en mémoire) et pipé dans `docker load`.
  Retour `{ ok, output }` (queue de la sortie, 300 caractères ; `500` si `docker load` échoue).
- **`images/build`** (POST, `?tag=` requis) : corps **binaire** = contexte de build `tar.gz`
  (avec `Dockerfile` à la racine), extrait dans un répertoire temporaire puis
  `DOCKER_BUILDKIT=1 docker build -t <tag>` **local** (timeout 2400 s). Retour toujours `200` :
  `{ ok, rc, output }` (sortie BuildKit, queue 2000 caractères — pas de live stream). Ferme le
  dernier besoin de root-SSH pour les builds sur nœud.
- **`exec`** (POST) : exec hôte générique token-gated — exécute `bash -c <cmd>` **localement**
  sur le nœud (`timeout` en secondes, défaut 300 ; `input` optionnel passé sur stdin). Retour
  `200` avec `{ rc, stdout, stderr }` (même contrat que `host_ops.ssh_run`). Les endpoints
  structurés (containers/images/networks/ptp/xdp) restent préférés ; `exec` couvre le reste
  des host-ops (MTL/VF/binds) sans énumérer chaque commande.

### 4.4 Spécification de conteneur (corps de `POST /v1/containers`)
Doit exprimer **fidèlement** les deux `docker run` actuels :
```json
{
  "name": "bobi-cmp-2003",
  "image": "bobi-media:0.1",
  "network": "host" | "<macvlan-name>",
  "privileged": false,
  "autoremove": false,            // MTL: true (--rm) ; compute: false
  "restart_policy": "unless-stopped" | "no",
  "mounts": [
    { "host": "/dev/shm", "container": "/dev/shm" },
    { "host": "/dev/hugepages", "container": "/dev/hugepages" },   // io2110
    { "host": "/srv/mxl-media/<proj>", "container": "/mnt/media" } // media
  ],
  "env": { "...": "..." },        // MTL e_args
  "resources": { "cpus": 4, "memory_mb": 1024, "cpuset": "2,3", "cpu_shares": 128 },
  "gpus": "device=0",             // optionnel (chantier multiview-GPU) ; ex. "all", "device=0,1"
  "entrypoint": "mxl-fabrics-demo",            // optionnel (chantier RDMA) : override --entrypoint
  "command": ["-d", "/dev/shm/mxl", "..."],    // optionnel : argv passé après l'entrypoint
  "devices": ["/dev/infiniband"],              // optionnel : --device par entrée
  "cap_add": ["IPC_LOCK"],                      // optionnel : --cap-add par entrée
  "ulimits": { "memlock": -1 },                // optionnel : --ulimit memlock=-1 (-1 = unlimited)
  "log": { "driver": "journald", "opts": { "tag": "{{.Name}}" } }   // optionnel : pilote de log
}
```
- **RDMA (chantier RDMA, `services/rdma`)** : **implémenté côté agent en 0.10.0** (`_docker_run_argv`).
  Le conteneur de réplication `mxl-fabrics-demo` exige
  `entrypoint`+`command` (override de l'agent.py de l'image runtime), `network:"host"` (RoCE adresse
  le device verbs, pas le macvlan), `devices:["/dev/infiniband"]`, `cap_add:["IPC_LOCK"]` et
  `ulimits:{memlock:-1}` (enregistrement mémoire RDMA épinglée). Un agent qui ignore ces champs ne
  pourra pas établir de lien RDMA (le conteneur n'aura pas accès au device verbs).
- **GPU** : si `gpus` (string non vide) est fourni → `--gpus "<valeur>"`. Absent → inchangé. Nécessite
  `nvidia-container-toolkit` + runtime nvidia sur l'hôte (n'émettre que pour un nœud GPU, ex. dl360-2).
  **Implémenté côté agent en 0.9.0.** Un agent plus ancien qui IGNORE ce champ ne casse rien : le
  conteneur démarre sans GPU, cupy est absent et le script de plugin retombe sur numpy (repli silencieux).
- **Journal (`log`, agent ≥ 0.17.0)** : `{driver, opts}` → `--log-driver <driver> --log-opt k=v …`.
  Le contrôleur envoie `journald` (+ `tag={{.Name}}`) pour que le journal du conteneur vive dans
  systemd-journald de l'**hôte** : il survit à la destruction du conteneur (`--rm`, redéploiement)
  et au reboot du nœud, et reste interrogeable par `journalctl CONTAINER_NAME=<nom>` (cf.
  `app/journal.py`). Un agent ANTÉRIEUR ignore la clé → pilote par défaut du daemon (`json-file`,
  NON durable) : dégradé, pas cassé, et la route `/api/containers/<vmid>/logs` l'annonce
  (`source: "docker"`). Le pilote est figé à la CRÉATION : il ne s'applique qu'aux conteneurs recréés.
- **mTLS par-conteneur (`tls` = `{cert, key, ca}` PEM, non montré ci-dessus)** : l'agent matérialise
  le trio dans `CONTAINER_TLS_ROOT = /run/bobi-tls/<nom>/` et le bind-monte `:/etc/bobi-tls:ro` →
  l'agent PAR-CONTENEUR sert `:8081` en mTLS. **`/run` est un tmpfs, et c'est ASSUMÉ** : la clé
  privée d'un conteneur ne doit pas se retrouver sur disque (elle porte EKU `clientAuth`, cf. §8).
  **Conséquence à connaître — elle a causé une panne de prod** : au reboot du nœud, `/run` est vidé
  *avant* que Docker relève les conteneurs `--restart unless-stopped` ; la source du bind-mount
  n'existant plus, Docker la **recrée VIDE**, l'agent du conteneur ne trouve aucun cert et sert en
  **HTTP clair** — alors que le contrôleur parle mTLS : conteneur vivant, injoignable, sans alerte.
  Le moteur 2110 y échappe (`--rm` → recréé, donc re-provisionné). La réparation est côté
  contrôleur : `app/node_recovery.py` sonde chaque conteneur compute après un reboot (verdict
  `ok` / `clair` / `injoignable`, cf. `deploy.diagnostiquer_schema_agent`) et **re-provisionne** ceux
  dont l'agent répond en clair, en effaçant leur `runtime_spec_sig` puis en redéployant (seul chemin
  qui re-matérialise le trio). Aucun repli HTTP silencieux côté contrôleur.
- **Mapping compute** : `--network <macvlan> --restart unless-stopped [--cpus|--cpuset-cpus|--memory|--cpu-shares] [--gpus <val>] -v shm:/dev/shm [-v media:/mnt/media] <image>`.
- **Mapping io2110** : `--rm --network host --privileged -v shm:/dev/shm -v /dev/hugepages:/dev/hugepages <env> <image>`.
- L'agent lit l'**IP macvlan attribuée** (IPAM Docker) et la renvoie dans `status` (remplace
  `docker_compute._read_container_ip`).

## 5. Charge utile `/v1/capabilities` (→ table `nodes`)
```json
{
  "capabilities": ["io2110", "compute", "media"],
  "host": "node-a.lan",
  "mxl_mount": "/dev/shm",
  "networks": [ { "name": "bobimacvlan", "driver": "macvlan", "subnet": "x.x.x.x/24" } ],
  "nics": [ { "name": "eno1", "physical": true, "mac": "…", "up": true, "driver": "ice", "speed_mbps": 25000, "addrs": ["x.x.x.x/24"] } ],
  "images": [ { "tag": "bobi-mtl:0.22.15", "present": true }, { "tag": "bobi-compute:0.2", "present": true } ],
  "nic": { "iface": "ens1f0np0", "model": "E810", "lcores": "2-9" },   // si io2110
  "lcores": "2-9",
  "gpus": [ { "index": 0, "name": "Tesla T4", "mem_mb": 15360 } ],     // [] si pas de GPU (nvidia-smi)
  "agent_version": "0.12.0"
}
```

## 6. Charge utile `/v1/health` (heartbeat / dashboard)
```json
{
  "ok": true, "agent_version": "0.12.0", "uptime_s": 1234,
  "docker": { "ok": true, "version": "27.x" },
  "resources": { "cpu_pct": 14.0, "mem_used_mb": 8192, "mem_total_mb": 65536 },
  "sensors": {                                                  // hwmon (ajout 0.12.0)
    "temps": [ { "chip": "coretemp", "label": "Package id 0", "c": 52.0 } ],
    "fans":  [ { "label": "fan1", "rpm": 4200 } ],
    "power_w": 118.3
  },
  "membw": { "gbps": 6.1, "ts": 1751700000.0, "sample_mb": 128 },  // canary RAM (ajout 0.14.0)
  "hugepages": { "total": 2048, "free": 1900 },                 // io2110
  "nic": { "iface": "ens1f0np0", "link": true, "queues": { "max": 48, "current": 4 } }, // io2110
  "ptp": { "running": true, "locked": true, "gm_id": "00:09:0D:FF:FE:01:14:D9", "offset_ns": 42 }, // io2110
  "containers": [ { "name": "bobi-cmp-2003", "status": "running" } ],
  "watchdog": {                                                 // chien de garde (ajout 0.20.0)
    "enabled": true, "recreate_enabled": false,
    "controller_silent": false, "grace_s": 60,
    "containers": { "bobi-cmp-2003": { "tries": 1, "note": "relancé (tentative 1)" } },
    "events": [ { "ts": 1786000000.0, "msg": "bobi-cmp-2003 : script relancé…" } ]
  }
}
```
- **`watchdog`** (ajout **0.20.0**) : ce que le chien de garde (§4.5) a vu et fait. `events` est
  borné à 50 lignes. C'est le canal par lequel le contrôleur découvre **à son retour** ce qui a été
  décidé en son absence — une action muette serait pire que pas d'action.
- **`membw`** (ajout **0.14.0**) : bande passante mémoire mesurée par un canary memcpy
  mono-thread embarqué dans l'agent (thread de fond, défaut toutes les 60 s sur 128 Mo ;
  config `membw_interval_s` — ≤ 0 désactive — et `membw_sample_mb`). Débit BRUT en Go/s :
  la référence par nœud, le ratio et les alertes restent côté contrôleur (`app/membw.py`).
  Champ absent si le canary est désactivé ou n'a pas encore tourné ; pour un agent < 0.14.0
  le contrôleur retombe sur son canary one-shot via `/v1/host/exec`.
- **`sensors`** (ajout **0.12.0**) : santé matérielle lue dans `/sys/class/hwmon` (sans
  dépendance, lm-sensors non requis) — températures (°C), ventilateurs (RPM), puissance
  instantanée (W, somme des `power*_input`). Best-effort : `{}` si rien d'exploitable ; sur
  certains serveurs (ex. HPE) ventilateurs/conso passent par l'IPMI/BMC et n'apparaissent pas
  en hwmon → seules les températures coretemp/k10temp sont remontées.

## 7. Intégration contrôleur & migration
- Nouveau `app/node_driver.py` : dispatch sur `node.kind`. Pour un nœud à agent → **client HTTP**
  (`agent_url`+token) ; pour `proxmox-lxc` → chemin Proxmox legacy ; pour `docker` sans agent →
  chemin `ssh_run` legacy. **Coexistence, pas de big-bang.**
- `docker_compute`/`docker_driver`/`ptp` : leurs appels `ssh_run` deviennent des appels au client
  agent quand le nœud a un `agent_url`.
- **Enregistrement** : l'opérateur saisit `host`+`port`+`token` (Réglages → Déploiement → Nœuds)
  → le contrôleur appelle `/v1/capabilities` → upsert de la ligne `nodes` (capacités, images,
  réseaux, NIC). Heartbeat `/v1/health` périodique → `last_seen`/`status` (remplace le sondage
  Proxmox/SSH dans `surveillance()`).

### 7b. Retrait — `node_agent/uninstall-node.sh` et `uninstall-controller.sh`
Pendant de l'installeur : ce que l'install (et l'orchestrateur via l'agent) a posé sur l'hôte, ce
script l'enlève. **Autonome** — ni contrôleur, ni agent, ni archive requis : il tourne encore sur un
nœud dont le contrôleur a disparu.

| | |
|---|---|
| `--dry-run` | inventaire seul de ce qui est RÉELLEMENT présent, ne touche à rien |
| (défaut) | conteneurs, unités systemd (`bobi-*`, `mxl-*`, `rdma-netns-exclusive`, VLAN), agent + config + mTLS, `/etc/bobi`, réglages hôte (journald, sysctl, tmpfiles, modules-load, chrony, ptp4l), réseau containers, domaine MXL, cmdline noyau, restitution des ports `vfio-pci` au noyau |
| `--purge-images` | + les images Docker `bobi-*` |
| `--purge-media` | + le CONTENU de la racine média (données, irréversible) |
| `--purge-packages` | + docker, pilote NVIDIA, container-toolkit, linuxptp, chrony |

Trois choix de conception qui expliquent le reste :
- **Confirmation obligatoire** : `RETIRER` à taper en interactif, `--yes` sinon. En non-interactif
  sans `--yes`, le script REFUSE — il est appelable à distance, le défaut doit être « ne rien faire ».
- **L'agent est retiré en DERNIER et dans un processus détaché** : le script peut avoir été lancé
  PAR l'agent (`/v1/host/exec`) ; s'arrêter soi-même en cours de route laisserait le retrait à
  moitié fait.
- **Ce qu'il ne sait pas, il le dit** : la clé SSH du contrôleur n'est identifiable avec certitude
  que si on la fournit (`--controller-key`) — son commentaire dépend de l'époque où elle a été
  générée. Sans elle et sans commentaire reconnu, le résumé final SIGNALE les clés restantes plutôt
  que d'effacer au hasard une ligne de `authorized_keys` (qui couperait l'accès d'un tiers) ou de
  laisser croire à un retrait complet alors qu'un accès root du contrôleur survit.

Ce script ne fait QUE l'hôte : **supprimer le nœud dans Réglages → Déploiement → Nœuds** reste
nécessaire côté contrôleur (c'est ce geste qui défait les liens RDMA et libère les emplacements).

**`uninstall-controller.sh`** est son pendant pour l'orchestrateur (`/opt/bobistudio`, service
`bobistudio`, VIP keepalived si la conf porte notre marqueur, partages montés sous `/mnt/ext`
démontés sans jamais toucher au distant, `/opt/bobi-node-src`). Il ajoute une règle que le nœud
n'a pas : **on ne détruit pas la base sans en laisser une copie** — par défaut il écrit une archive
`/root/bobistudio-retrait-<horodatage>.tar.gz` (base + WAL, `config_local.py`, sauvegardes, CA du
plan de contrôle, téléversements) AVANT d'effacer, et un échec d'archivage interrompt tout ;
`--purge-data` pour s'en passer sciemment. Sur une machine « tout-en-un » il enchaîne lui-même
`uninstall-node.sh` (avant d'effacer `/opt/bobistudio`, d'où ce dernier vient). Deux détails qui
ont leur importance : il **se recopie hors de `$APP_DIR` et se relance** (bash lit son script au
fil de l'exécution — s'effacer soi-même en route ferait dérailler la fin), et il **ne supprime
`/root/.ssh/id_ed25519` que s'il peut l'attribuer à Bobi.Studio** (commentaire `bobistudio-controller`)
ou sur `--purge-key` ; sinon il le SIGNALE, cette clé ouvrant encore un accès root aux nœuds.

Les deux sont accessibles sans ligne de commande : menu de l'installeur → **Désinstaller**
(cf. INSTALL.md §2.1).

## 8. Sécurité
- Token par-nœud (≥ 24 octets urlsafe), en-tête `X-MXL-Node-Token`. Stockage : `nodes.agent_token`
  (ou settings chiffrés). **Supprime le besoin de root-SSH du contrôleur vers chaque nœud** (item
  D de l'audit fiabilité).
- L'agent tourne en **root** sur le nœud (docker, hugepages, xdp, ptp) ; surface réduite à l'API.
- TLS optionnel (réseau interne) ; recommandé si le nœud n'est pas sur un segment de confiance.
- **Identité du client mTLS sur `:8081` (agent par-conteneur)** : les certs de conteneur sont émis
  avec EKU `serverAuth` **+ `clientAuth`** (`app/ca.py`) — vérifier seulement « signé par la CA »
  laisserait la clé privée d'UN conteneur piloter TOUS les agents de la flotte. `script_templates/
  agent.py` vérifie donc l'**identité du pair** : `CN=bobi-controller` (celui du cert contrôleur) ou
  l'URI SAN `bobi://controller` ; les certs de conteneur (`CN=mxl<vmid>`) sont refusés en **403**.
  **TROISIÈME IDENTITÉ (images compute ≥ 0.26.0 / média ≥ 0.15.0 / webrtc ≥ 0.3.0)** : l'agent-NŒUD
  (URI SAN `bobi://node/<id>`) est accepté, mais **restreint à `GET /status` et `POST /start`**
  (liste `NODE_ALLOWED`) — c'est le strict nécessaire à son chien de garde (§4.5). `/deploy`,
  `/stop` et `/nmos/subscribe` restent réservés au contrôleur, et les certs de conteneur
  (`bobi://container/<vmid>`) restent refusés : la barrière anti-mouvement-latéral est intacte.
  L'URI SAN est **fixée par le contrôleur** (`app/ca.py:_san_list`), jamais recopiée d'un CSR — un
  CN qui imiterait `bobi://node/1` ne passe pas. Échappatoire supplémentaire :
  `MXL_TLS_ALLOW_NODE=0` referme cette porte sans rien désarmer d'autre.
  **EXEMPTION LOOPBACK (images compute ≥ 0.27.0 / média ≥ 0.16.0 / webrtc ≥ 0.4.0)** : une connexion
  venant de `127.0.0.1`/`::1` est acceptée sur ces deux mêmes endpoints **sans vérification
  d'identité client**. C'est le chemin réel du chien de garde (§4.5) : en macvlan, l'agent-nœud ne
  peut pas nous joindre par le réseau, il passe par `docker exec` et n'a sur place que NOTRE cert de
  conteneur — celui que `_peer_role` refuse, à raison, quand il vient du réseau. Sûr parce que
  atteindre notre loopback exige d'exécuter du code DANS ce conteneur, donc d'être root sur l'hôte,
  qui peut déjà tout nous faire : l'exemption n'accorde aucun pouvoir nouveau. Un paquet venu du
  réseau avec une source 127.0.0.1 n'arrive pas là (sources martiennes jetées par le noyau).
  Échappatoire : `MXL_TLS_ALLOW_LOOPBACK=0`.
  Échappatoires (env, injectées par `docker_compute` depuis les réglages `agent_tls_client_cn` /
  `agent_tls_verify_client_cn`) : `MXL_TLS_CLIENT_CN=<cn>` pour un CN différent,
  `MXL_TLS_VERIFY_CLIENT_CN=0` pour revenir au comportement historique. Le token
  `X-MXL-Agent-Token` reste le second facteur, indépendant. **Ce contrôle est baké dans les images
  runtime** : effectif seulement après rebuild + redéploiement.
- **Token de l'agent par-conteneur `X-MXL-Agent-Token`** (à ne PAS confondre avec le token
  par-**nœud** ci-dessus, `:9100`/`X-MXL-Node-Token`/`nodes.agent_token`) : valeur **aléatoire par
  conteneur**, stockée en `containers.agent_token`, **injectée en `MXL_AGENT_TOKEN` au `docker run`**
  par `docker_compute` (chemin agent-nœud *et* chemin ssh legacy) et `docker_driver`. Sans cette
  injection l'agent (`script_templates/agent.py`) n'exige **rien** : le contrôle existait des deux
  côtés du code sans être appliqué nulle part (corrigé 2026-07). Aucun rebuild d'image n'est
  nécessaire (la variable est déjà lue). Repli : un conteneur sans token stocké (créé avant la
  migration) est joint avec le token **dérivé** historique `HMAC(flask_secret_key, "agent:<vmid>")`
  — donc `flask_secret_key` n'est rotable que lorsque toute la flotte est passée au token stocké
  (traçabilité : champ `agent_auth` de `/api/containers`, agrégat `database.db_agent_token_etat()`).
  **Échappatoire** : réglage `agent_token_inject=0` → plus aucune injection au run (agent ouvert,
  comportement historique) dès le redéploiement du conteneur concerné.

## 9b. Topologies de déploiement (co-location autorisée)

La séparation control-plane / node-plane est **logique** (deux rôles, deux contrats), **pas**
une obligation de deux machines. Les rôles se **co-localisent ou se séparent** librement :

- **Tout-en-un (1 box)** : contrôleur (Flask `:5000`) **+** `bobi-node-agent` (`:9100`) **+**
  capacités (`io2110`/`compute`/…) sur la même machine. Le contrôleur pilote l'agent **local via
  loopback** (`agent_url = http://127.0.0.1:9100`) — **exactement le même chemin** que pour un
  nœud distant, sans cas particulier. C'est le mode « collapse 1 box » prévu par la vision cluster.
- **Contrôleur dédié + N nœuds** : le contrôleur n'a aucune capacité ; les nœuds n'ont pas de rôle
  contrôle. Topologie de production.
- **Paire HA** : 2 contrôleurs (actif + warm-standby, VIP) sur des box distinctes des nœuds
  broadcast critiques.

Le fait que les rôles soient découplés est précisément **ce qui rend ces trois topologies
possibles** sans changer le code : rien n'oblige le contrôleur à être ailleurs que sur une
machine qui est aussi un nœud, ni à y être seul.

**Points de vigilance en co-location** (choix de déploiement, pas blocage) :
- *Isolation ressources* : le temps réel broadcast (cœurs épinglés, hugepages) ne doit pas être
  affamé par le contrôleur → pour un site critique, on **sépare** ; pour un labo/petit site, on
  **collapse**.
- *HA* : si le contrôleur partage la box d'un nœud et que la box tombe, on perd les deux → la
  standby vit **ailleurs**.

## 9. Décisions & questions ouvertes
**Décidé :**
- **Packaging agent = Python + systemd sur l'hôte** (venv + unit `bobi-node-agent.service`,
  `Restart=on-failure`). Réutilise les helpers existants ; accès direct docker/hugepages/xdp/
  ptp/ethtool. Même modèle que l'agent par-conteneur (`script_templates/agent.py`).
- Multi-conteneurs io2110 : **1 MTL/nœud** (`--network host`) pour v1.
- Versionnement d'API : **`/v1` figé** ; compat via champ `agent_version`.

**À trancher avant/pendant Phase B :**
- Enregistrement : **pull** (contrôleur sonde `/capabilities`) seul — défaut retenu — ou aussi
  **push** (agent s'annonce au boot) ? *(défaut : pull ; push optionnel plus tard)*.
