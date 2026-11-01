# Réseau des conteneurs : deux modes par nœud — chantier du 2026-10-27

Déclenché par un constat du testeur (recette, élément `custom-ff5bd032bb`, 27/10) sur un serveur
tout-en-un : conteneurs créés, `running`, et l'orchestrateur ne les joint pas.

## Le constat et sa cause

Le noyau Linux **isole une interface parente de ses enfants macvlan**. Un paquet émis par l'hôte
depuis `eno1` ne redescend jamais vers un enfant macvlan de `eno1`. Sur un tout-en-un,
l'orchestrateur EST sur l'hôte parent : il ne joint aucun de ses conteneurs — alors que
`docker ps` les dit `running`, que l'agent du nœud va bien, et qu'un autre poste du LAN les
joint parfaitement. Rien n'échoue ; tout est vert et rien ne marche.

## Les deux modes (décision de l'utilisateur, 27/10)

| | « Réseau du site » (`site`) | « Réseau interne » (`interne`) |
|---|---|---|
| Pilote Docker | `macvlan` | `bridge` |
| Adresse du conteneur | une IP du LAN | un subnet privé au nœud (défaut `172.30.0.0/24`) |
| Joint par l'hôte | **non** — shim requis | oui, nativement |
| Joint depuis l'extérieur | oui, directement | non, sauf port publié |
| Demande au réseau du site | une plage d'IP, une carte | **rien** |

Le mode vit dans `nodes.container_net_mode`. **Défaut `site`, aucune migration de valeur** : un
nœud installé avant ce chantier reste exactement comme il était. Changer de mode détruit les
conteneurs du nœud (un réseau Docker ne se modifie pas sous ses attachés) — c'est donc un geste
d'exploitant, annoncé comme tel, jamais un effet de bord de mise à jour.

## ★ Le mode interne n'a de sens que sur un tout-en-un

Ce n'est pas une précaution, c'est le **plan de contrôle**. L'orchestrateur ne passe pas par
l'agent du nœud pour parler à un conteneur : il l'appelle en direct sur `docker_ip:8081`
(agent par-conteneur) et `:8080` (métriques), cf. `addressing.get_container_ip`. Un subnet privé
ne sort pas de la machine : depuis un contrôleur situé ailleurs, ces deux ports sont
injoignables, et les conteneurs apparaîtraient `running` sans jamais recevoir leur script —
la panne même que ce chantier répare, reproduite par l'autre bout.

`container_net.mode_interne_possible()` refuse donc, en nommant la raison.

⚠ **Haute dispo** : un nœud est tout-en-un *pour le contrôleur d'aujourd'hui*. Après un
basculement, ses conteneurs en mode interne ne sont plus joignables depuis le contrôleur devenu
actif. Ce n'est pas interdit (un site peut l'assumer) mais c'est **dit** : la route rend un
avertissement quand `ha.peer_url()` est renseigné.

## Le shim macvlan (mode site, nœud portant l'orchestrateur)

Une interface macvlan de PLUS, côté hôte, sur le même parent : les enfants macvlan se parlent
entre eux, donc l'hôte passe par le shim pour atteindre ses conteneurs. `app/container_net.py`,
unité systemd `bobi-shim`, garde `tests/verif_shim_macvlan.py`.

**Deux façons de casser le LAN du site avec ce shim, et il faut tenir les deux** :

1. une **route** vers le subnet (`ip route add 192.0.2.0/24 dev bobi-shim`) ;
2. une **adresse** au préfixe du subnet (`ip addr add 192.0.2.254/24 dev bobi-shim`), qui crée
   la même route connectée sans jamais écrire le mot « route ».

Dans les deux cas, le routeur, les pupitres et les postes du site passent par une interface
macvlan qui ne les sert pas. D'où : adresse en **/32**, et une route par CIDR de la **seule
plage des conteneurs** (`summarize_address_range`), avec `src` explicite — sans elle le noyau
sourcerait depuis la carte parente, qu'un conteneur ne peut pas joindre.

## Inventaire : ce qui doit être publié en mode interne (27/10)

Méthode : on ne cherche pas « quels ports existent » mais **qui appelle qui**. En mode interne
sur un tout-en-un, l'orchestrateur et les conteneurs sont sur la même machine ; seul ce qui est
joint depuis **une autre machine** a besoin d'un port publié.

**Ne demandent RIEN** (et c'est la plus grande partie du parc) :

- `:8081` (agent par-conteneur) et `:8080` (métriques) — appelés par l'orchestrateur, qui est
  sur l'hôte. C'est précisément ce qui rend le mode interne praticable ici, et impraticable
  ailleurs ;
- `:8082` (contrôle live des plugins : mixer, split, scope, stream_in, decklink_io, 2110_io…) —
  passe par le **proxy de l'orchestrateur** (`/api/containers/<vmid>/plugin/<path>`), jamais
  depuis le navigateur ;
- toutes les **UI de plugin** (`control.html`/`control.js`) et les **pages publiques**
  (`/p/<jeton>`) — servies et relayées par l'orchestrateur ;
- `2110_io` et `decklink_io` — `--network host` (`docker_driver`), donc hors du mode ;
- les **sorties** d'un `streamer` (UDP, SRT appelant, WHIP vers la passerelle) — du trafic
  SORTANT, que le NAT couvre ;
- les **services** (`nmos`, `files`, `media_manager`, `snmp`, `emberplus`, `tsl`…) — ils vivent
  dans l'orchestrateur, pas dans un conteneur.

**Demandent une publication de port** — la liste complète, trois entrées :

| Quoi | Port | Qui appelle depuis l'extérieur |
|---|---|---|
| passerelle WebRTC (`webrtc_gateway`) | `webrtc_http_port` (8889) | **le navigateur**, en direct (WHEP + page embarquée) |
| passerelle WebRTC — ingest | `webrtc_rtsp_port` (8554) | un encodeur TIERS qui pousse vers nous (nos propres `streamer` sont sur le même bridge) |
| `stream_in` en mode écoute | `listen_port` (9000, SRT) ou RTMP serveur | la **source** qui pousse (OBS, encodeur de reportage) |

Plus un cas conditionnel : `multiview` avec `tsl_port` ≠ 0 (mode TSL **Direct**) écoute en TCP
un pupitre du site. En `tsl_mode: central` — le défaut — c'est l'orchestrateur qui reçoit, et il
n'y a rien à publier.

`webrtc_api_port` (9997) est appelé par l'orchestrateur seul : rien à publier.

**Conséquence de cet inventaire** : la publication ne peut PAS être une propriété du mode (« en
interne, on publie tout »), parce que la liste dépend du **plugin** et de sa **configuration**
(`stream_in` en appel ne publie rien, en écoute si ; `multiview` selon `tsl_mode`). Elle doit
être **déclarée par le manifeste** et résolue au déploiement, comme le reste du contrat de
plugin. C'est le point à implémenter ensuite.

## « Sortie Internet » : le choix de conception, et ce qui l'a tranché

Besoin : dédier une carte (ex. `eno2`) en DHCP vers Internet, et dire à un conteneur de
streaming de sortir par là plutôt que par le réseau du site.

**Le plan d'origine** donnait au conteneur un **second réseau Docker** portant sa route par
défaut. **Mesuré sur le parc le 2026-10-27** : Docker **26.1.5** (version Debian 13) sur tous les
nœuds, et `--gw-priority` **n'existe pas** — absent de l'aide de `docker run` comme de
`docker network connect`. Avec deux réseaux, il n'y aurait donc **aucun moyen** de choisir lequel
porte la route par défaut : le choix appartiendrait à libnetwork, hors contrat, et le symptôme
d'une erreur serait « le flux part par le mauvais lien », sans trace.

`gw-priority` est arrivé en **Docker 28.0 (février 2025)**, soit **deux branches majeures** après
la nôtre. L'obtenir demanderait de quitter le paquet `docker.io` de Debian pour `docker-ce` sur
chaque nœud — paquets en conflit, redémarrage du daemon, donc **tous les conteneurs du nœud
tombent** (sur un nœud MTL, le pipeline 2110 s'arrête). Ce n'est pas une mise à jour, c'est une
fenêtre de maintenance par machine. Et **même en 28**, attacher un réseau APRÈS le démarrage
([backend.ai#13106](https://github.com/lablup/backend.ai/issues/13106)) **tue les ports publiés** :
le second réseau demanderait en plus de passer les deux réseaux dès le `docker run`, en syntaxe
étendue — une nouvelle extension du contrat de l'agent. Docker 28 randomise aussi les MAC des
conteneurs sur bridge **et macvlan** : rien chez nous n'en dépend (vérifié), mais la politique du
switch du site, peut-être.

**Retenu : routage par source.** Un seul réseau par conteneur, et l'HÔTE route sa sortie :
`ip rule from <ip du conteneur> lookup 1031`, table dont la route par défaut passe par la carte
Internet.

| | Routage par source (retenu) | Second réseau Docker |
|---|---|---|
| Choix de la passerelle | aucune ambiguïté (un seul réseau) | **impossible sur 26.1.5** |
| Changer la sortie | à chaud, rien à recréer | connecter/déconnecter → recréation |
| NAT | déjà là (MASQUERADE du bridge) | à faire (pas de DHCP Docker en macvlan) |
| Contrat de l'agent | rien à ajouter | attacher un 2ᵉ réseau |
| Entrant depuis Internet | non | oui |
| Le conteneur voit sa sortie | non (c'est sur l'hôte) | oui |
| Fonctionne sur un nœud distant | non (exige le mode interne) | oui |

B reste le bon modèle le jour où il faudra un **entrant** sur le lien de sortie ou une IP
routable par conteneur — les deux ne s'excluent pas, le choix restant par conteneur.

### Ce que le routage par source impose, et pourquoi

- **Exige le mode interne** : en mode site (macvlan) le conteneur a sa propre passerelle et ne
  traverse jamais l'hôte, donc aucune règle de l'hôte ne peut changer son chemin. On refuse,
  plutôt que de poser des règles sans effet.
- **La passerelle est relue à chaque passage** (bail DHCP) par un minuteur systemd de 30 s. La
  figer serait juste jusqu'au premier renouvellement qui change de routeur — puis faux en
  silence, des semaines après le geste.
- **La table de routage principale de l'hôte n'est jamais touchée.** Si le client DHCP de la
  carte Internet y installe une route par défaut concurrente, c'est la configuration réseau du
  nœud : on la SIGNALE (`etat_sortie().concurrente`), on ne la réarrange pas. Retirer une route
  par défaut sous un contrôleur de production est un geste dont le symptôme n'a plus aucun
  rapport avec la cause.
- **Les règles sont réconciliées sur un fichier** (`/etc/bobi-sortie/ips`), écrit par
  l'orchestrateur : on ajoute ce qui manque et on **retire ce qui n'y est plus**. Les `ip rule` ne
  survivent pas à un redémarrage, et une règle qui survivrait à son conteneur ferait sortir par
  Internet l'adresse réattribuée à un autre.
- Le **DNS** continue par le résolveur de l'hôte : on change le chemin des données, pas celui des
  noms.
- Le choix est **par conteneur** (`containers.egress`, défaut `node`, aucune migration), réglable
  dans la palette et **pilotable par macro** (`container_net` → `set_egress`) — une bascule qu'on
  ne peut pas automatiser au bon moment est une capacité morte.

Garde : `tests/verif_sortie_internet.py`.

## État au 2026-10-27

Fait : agent 0.30.0 (`driver` sur `/v1/host/networks/ensure`, inventaire sans liste blanche),
mode en base + allocation d'IP qui le suit, shim macvlan, refus du mode interne hors tout-en-un,
choix du mode dans Réglages et dans l'installeur tout-en-un.

Reste : rien du périmètre décidé. La « Sortie Internet » est faite (routage par source, cf. la
section ci-dessus).

Gardes : `verif_node_agent_reseau_mode`, `verif_reseau_conteneurs_mode`, `verif_shim_macvlan`,
`verif_install_reseau_mode`, et la section « réseau des conteneurs » de `verif_rendu_settings`.
