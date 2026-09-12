# Chantier — Observabilité / outillage de debug

Ouvert le **2026-08-13**. Rétrospective : au vu de tous les chantiers menés (2110, MXL, multiview,
RDMA, PTP, CPU), quelles capacités de **monitoring** nous ont manqué ? Chaque famille ci-dessous
est adossée à des incidents RÉELS — c'est le critère de sélection, pas la complétude théorique.

Statut : **à faire**, rien d'implémenté. Ce document est la spécification d'intention.

> **Fait le 2026-08-15, en préalable** : l'anti-rebond des alertes (`database._antirebond`, table
> `alert_episodes`, `GET /api/alerts/episodes`). Ce n'est aucune des douze familles ci-dessous —
> c'est ce qui les rend lisibles. Le journal comptait 10 068 lignes dont ~80 % faites de quatre
> messages répétés : construire un détecteur plus fin au-dessus d'un fil noyé n'aurait rien donné
> à voir. Une alerte de la machine s'écrit maintenant à la TRANSITION, puis se compte.

---

## 0. Le principe directeur

Notre supervision répond à « est-ce que ça tourne ? ». Aucun de nos gros bugs n'a été détecté par
là — ils tournaient tous. Les questions auxquelles il faut savoir répondre sont :

1. Est-ce que ça fait **ce qu'on a demandé** ? (écart intention/réalisé)
2. Est-ce que la donnée servie est **fraîche** et **juste** ? (âge, contenu)
3. Est-ce que le chemin emprunté est **celui qu'on croit** ? (repli silencieux, graphe réel)
4. Est-ce que la ressource **allouée** est la ressource **obtenue** ?

Cf. mémoires `alarm-must-compare-to-intent`, `silent-failure-antipattern`,
`cpu-consumption-must-be-measured-not-inferred`.

---

## 1. Écart intention → réalisé (socle)

Table unique par conteneur : `demandé | mesuré | écart`, pour fps, format, scan, cadence,
cœurs, débit, profondeur d'anneau, résolution. **L'alarme porte sur l'écart**, jamais sur la
valeur absolue.

Incidents couverts :
- `streamer-announces-50fps-delivers-25` — annonce 50, livre 25.
- `shm-video-ring-param-is-ignored` — paramètre accepté, jamais appliqué.
- `format-preset-select-silently-changes-scan` — le sélecteur change le scan en douce.
- `reserved-cores-not-exclusive-in-practice` — `garantir()` réserve, la réalité partage.
- `wall-fps-deficit-is-cpufreq-not-work` — le mur sous sa cible sans que rien ne l'indique.

## 2. Âge absolu du grain, de bout en bout

Chaque étage publie pour le grain qu'il vient de servir : `index`, `timestamp d'origine`,
`âge à la sortie`. Vue cascade : une barre par étage → on lit **où** la latence est dépensée.
Plus un détecteur de **contenu périmé sous index frais** (empreinte contenu vs index attendu).

On l'a fait à la main (timecode incrusté, âge contre horloge TAI) sur chaque chantier latence :
cf. `latence-mesure-par-timecode-incruste`, `latence-age-absolu-contre-horloge-tai`,
`sonde-latence-bandeaux-methode`. Ça doit devenir natif.

Incidents couverts :
- `tx-servait-la-trame-la-plus-ancienne`, `tx0-serves-one-frame-in-four-repeated`.
- `rdma-replique-contenu-vieux-sous-index-frais` — index frais, contenu vieux.
- `flux-repliquee-detruite-sert-anneau-perime`.
- `rdma-source-tranchee-replique-12-images-torn`.

## 3. Graphe vivant OBSERVÉ (≠ graphe déclaré)

Construire le graphe producteurs/consommateurs depuis les **mappings shm réellement ouverts**
(et les sessions 2110/RDMA actives), pas depuis `deploy_config`. Diff permanent
« déclaré vs observé », arêtes orphelines en rouge.

Incidents couverts :
- `tx-2110-never-counted-as-flow-consumer` — un TX consommait sans être compté.
- `io2110-recreate-drops-runtime-wiring-resync`, `pyramide-runtime-wiring-lost-on-restart`,
  `fabric-assembler-script-loss-restart`.
- `orphan-pyramide-invisible-ui`.

## 4. Détecteur de chemin dégradé (repli silencieux)

Chaque étage déclare dans `/state` le **chemin effectivement pris** (`gpu_slice`, `cpu`,
`fallback strided`, `AVX2`, `scalaire`, `blend groupé` / `blend naïf`) **et la raison du refus**
du chemin rapide. Bandeau global : « N conteneurs en repli ».

C'est le mode de panne le plus coûteux du projet : ça marche, mais par le mauvais chemin.

Incidents couverts :
- `mvk-strided-alpha-silent-fallback` — `mvk: False`, jamais remonté.
- `slice-path-never-got-the-grouped-blend`, `tally-invisible-en-mode-tranche`.
- `gpu-slice-dead-on-p5000-gate-ran-on-unusable-card` — gate validé sur une carte inutilisable.
- `libmxl-requires-avx2-sandy-bridge-nodes-unusable` (SIGILL, pas un repli mais même famille :
  capacité matérielle non vérifiée avant usage).

## 5. Ressources : alloué ≠ obtenu

Par conteneur : cpuset **posé** vs demandé, **fréquence réelle** des cœurs, nœud NUMA effectif vs
celui de la mémoire, GPU encore visible par le processus, bande passante mémoire consommée,
threads par cœur (HT). Alerte sur toute **révocation à chaud** d'une ressource d'un processus vivant.

Incidents couverts :
- `daemon-reload-revokes-gpu-from-running-containers`, `monolith-restore-does-not-give-back-gpu`.
- `numa-blind-core-pool-halves-gpu-walls`.
- `isolated-cores-must-pin-frequency`, `wall-fps-deficit-is-cpufreq-not-work`.
- `mtl-hyperthreading-lcores-dead`.
- `node-host-ram-bandwidth-dl360`, `multiview-loadtest-memory-bound`.

## 6. Détection de la MORT (pas seulement de la vie)

Watchdog symétrique : l'absence de heartbeat doit produire un **état descendant** explicite
(nœud, conteneur, flux). Distinguer trois choses qu'on confond aujourd'hui : *processus vivant*,
*script vivant*, *données qui avancent* (index qui progresse). Le décrochage de génération
(`lastWriteTime` qui stagne) est un signal de premier ordre.

Incidents couverts :
- `no-node-down-detection-status-only-goes-up` — le statut ne savait que monter.
- `horace-rx-freeze-igmp-join-wrong-nic`, `mtl-rx-incomplete-network-loss`.
- `multiview-stale-proxy-no-signal` — « No Signal » sur proxy périmé.
- `mxl-generation-strand-detection-lastwritetime`.

## 7. Horloge unique et timeline corrélée

Une timeline unique (orchestrateur, agents-nœuds, conteneurs, PTP, Docker) sur **une référence
temporelle explicitement étiquetée** — jamais devinée. Marqueurs d'événements superposés aux
courbes : déploiement, restart, recreate, `daemon-reload`, changement de câblage. 90 % du debug
c'est « qu'est-ce qui a changé juste avant ». Plus la santé de l'horloge elle-même : GM réel vs
GM annoncé, RTT asymétrique, dérive.

Incidents couverts :
- `node-logs-tai-mislabelled-utc` — journaux TAI étiquetés UTC.
- `clock-skew-was-asymmetric-rtt-not-clocks` — 20-30 ms qui étaient du RTT asymétrique.
- `gm-ntp-service-100ms-behind-its-own-ptp`.
- `sdp-tsrefclk-announces-boundary-clock-not-gm`.
- `daemon-reload-revokes-gpu-from-running-containers` (l'événement déclencheur était invisible).

## 8. Témoin de contenu (voir, pas compter)

Capture à la demande d'une trame de n'importe quel flux (bricolé : `grab` depuis le conteneur +
`static/uploads`), plus une empreinte de contenu pour détecter **sans œil humain** : trame
identique à la précédente, trame répétée périodiquement, déchirure (discontinuité à une frontière
de plan ou de shard).

Un **témoin TIERS** complète le dispositif : lire nos flux avec une pile qui n'est pas la nôtre.
On a déjà `plugins/_mxl_stock_bench` (libmxl stock) ; le SDK fournit aussi `mxl-gst-sink`
(cf. § « mxl-gst » plus bas) et `neuron` expose un endpoint témoin JSON
(`neuron-json-witness-endpoint`).

Incidents couverts :
- `tx0-serves-one-frame-in-four-repeated`, `slice-planar-layout-tears-at-chroma-boundary`,
  `io2110-interlace-tx-halfrate-fix`, `multiview-interlace-lie-and-consumer-fallout`.

## 9. Boîte noire (enregistreur en anneau)

Anneau permanent de N minutes de métriques haute fréquence + derniers événements, **vidé sur
disque à tout incident** (crash, chute de fps, alerte). Post-mortem attaché à l'alerte : on ouvre
l'alerte, on a les 60 s qui précèdent.

Incidents couverts : `mxl-mapping-freed-under-compose-loop` (SIGSEGV du mur),
`fps-dip-quantized-dt-artifact` (hoquets de 60 s), `rdma-cm-teardown-hangs-d-state-r620`.

## 10. Instrumentation qui ne tue pas le patient

Leçon dure : la mesure a ÉTÉ la panne. Budget explicite pour l'observabilité (coût CPU de chaque
sonde, affiché). Toute sonde en best-effort non bloquant, jamais dans le chemin critique, avec
backpressure.

Incidents couverts :
- `metric-line-crashed-the-wall-stale-rolling-avg` — une ligne de métrique a fait tomber le mur.
- `polling-without-backpressure-collapses-orchestrator`.
- `rdma-reconcile-inline-blocked-surveillance-loop`.
- `orchestrator-fd-leak-sqlite`, `bobistudio-log-lines-are-duplicated`.

## 11. Diff de configuration et d'artefacts

« Ce que la base dit » vs « ce qui est réellement déployé » : version de plugin, tag d'image,
empreinte du script servi par l'agent, params rendus. Plus un historique : qui a changé quoi,
quand, depuis quelle page.

Incidents couverts :
- `plugin-registry-stale-version-stamp`, `image-tag-from-meta-json-numbering-drift`.
- `deployer-script-hot-apply-skips-if-db-written-first`.
- `node-hugepages-runtime-vs-cmdline-drift`.

## 12. Injecteur de pannes + bancs rejouables

Provoquer à la demande : perte multicast, nœud coupé, GM perdu, disque plein, RDMA arraché,
redémarrage producteur, `daemon-reload`. Rejouer un scénario à l'identique pour valider un
correctif — fait à la main sur chaque chantier jusqu'ici.

---

## Priorisation

Si on n'en construit que quatre :

1. **§1 écart intention/réalisé** — socle ; transforme toutes les alarmes existantes.
2. **§2 âge absolu du grain** — seul outil qui nous ait jamais fait avancer sur la latence,
   encore artisanal aujourd'hui.
3. **§3 graphe vivant observé** — tue toute la famille « câblage perdu au restart ».
4. **§4 détecteur de repli silencieux** — tue toute la famille « ça marche, mais 10× trop lent ».

Les trois premiers sont largement à portée : la donnée existe déjà côté agents/plugins, il manque
la collecte transverse et la vue. Le quatrième demande d'imposer une **convention aux plugins**
(déclarer le chemin effectif dans `/state`) — c'est autant du contrat que de l'outillage, et ça
rejoint la règle « exposer aux macros » (`expose-plugin-features-to-macros`).

---

## Annexe — les briques GStreamer du SDK MXL (vérifié le 2026-08-13, `main` et `v1.1.0-rc1`)

Le SDK en contient **deux**, à ne pas confondre :

- **`tools/mxl-gst`** — pas un plugin. Aucun `GST_PLUGIN_DEFINE` ni `gst_element_register` : trois
  **exécutables** (`mxl-gst-testsrc`, `mxl-gst-sink`, `mxl-gst-looping-filesrc`) qui montent un
  pipeline par `gst_parse_launch` et pontent vers libmxl via `appsink`/`appsrc` — exactement
  l'architecture de notre `player` (uridecodebin → appsink → `bobimxl.Writer`).
- **`rust/gst-mxl-rs`** — le **vrai plugin** (Rust) : `libgstmxl.so`, éléments **`mxlsrc`** et
  **`mxlsink`**, visibles par `gst-inspect-1.0`. Propriétés `domain`, `flow-id` (sink),
  `video-flow-id`/`audio-flow-id`/`data-flow-id` (src). Il est **déjà dans notre `MXL_REF`
  (v1.1.0-beta-1)** — on ne le construit simplement pas, nos Dockerfiles ne buildent pas `rust/`.

Limite commune : **v210 uniquement** (plus `audio/float32` et `video/smpte291`), donc aveugles à
notre `video/x-mxl-planar` (cf. `docs/reference/MXL_INTEROP.md`), sans genlock PTP maison, sans
grains-champs entrelacés. Aucun des deux ne remplace notre `player`.

Intérêt pour CE chantier (§8) : **`mxlsrc` et `mxl-gst-sink` sont des consommateurs TIERS** — ils
lisent nos flux avec une pile qui n'est pas la nôtre, au même titre que `plugins/_mxl_stock_bench`.
C'est du témoin, pas du moteur runtime. Construire `libgstmxl.so` coûte un étage Rust dans
l'image de banc (pas dans les images de production).
