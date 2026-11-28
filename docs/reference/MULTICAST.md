# docs/reference/MULTICAST.md — Plans d'adressage multicast

> Document de référence (2026-11-11). Il décrit ce que fait le code à cette date : `app/mcast_plans.py`
> (plans, types de flux, aperçu), `app/allocations.py` (allocation, re-planification),
> `app/mcast_audit.py` (audit), `app/routes/mcast_ranges.py` (API). Les références citent des NOMS de
> fonction : les numéros de ligne se périment. Interface : Réglages → Multicast.

## 1. Pourquoi un plan, et pourquoi les sites diffèrent

Un plan d'adressage dit quelle adresse multicast porte quel flux ST 2110. Deux sites ne l'écrivent
pas de la même façon. Deux formes reviennent :

- **Un bloc par équipement.** Chaque équipement a son bloc (par exemple un /24). Le rang du flux dans
  le bloc est le numéro de sortie : la sortie 3 prend le rang 3. L'essence décale le rang (la vidéo
  en `.1`, `.2`…, l'ANC en `.51`…, le premier audio en `.101`…).
- **Une plage par type de flux.** Une plage pour la vidéo 1080p50, une pour l'audio 8 canaux, une pour
  l'ANC. Chaque flux prend la prochaine adresse libre de la plage de son type.

Ce qui varie aussi d'un site à l'autre :

- **Les deux fabrics ST 2022-7.** Le premier fabric (celui qui porte les adresses A) est le rouge sur
  un site, le bleu sur un autre. Cela se règle par réseau média.
- **Le bloc B.** Il se déduit du bloc A en décalant un octet, ou il porte le même groupe que A, ou il
  est saisi à la main.
- **Le port UDP.** Un port fixe par essence, ou un port qui s'incrémente par flux.

Un plan est donc une combinaison de réglages indépendants, jamais un schéma imposé.

## 2. Le modèle

### 2.1 La cible

Un plan a **une cible**. Une cible porte au plus un plan.

| Cible | `scope` | S'applique à |
|---|---|---|
| Réseau média | `network` | Toutes les interfaces du réseau, appairées ou non |
| Paire 2022-7 | `pair` | Les deux interfaces rouge et bleue d'une paire d'un nœud |
| Interface sans paire | `interface` | Une interface média non appairée d'un nœud |

Une interface appairée ne reçoit pas de plan d'interface : le plan se pose sur sa paire.

### 2.2 Les lignes

Une ligne associe un **type de flux** à une plage sur le fabric A, et à une plage sur le fabric B. Les
lignes sont des enregistrements `mcast_ranges` qui portent un `plan_id`. Les règles sans `plan_id` sont
les anciennes règles (§6).

Les blocs A de deux lignes d'un plan ne se chevauchent pas. Un bloc B est identique à son propre bloc A
(même groupe), ou disjoint de tous les blocs A. Les blocs B ne se chevauchent pas. Un chevauchement
avec le plan d'une autre cible n'est pas refusé : l'aperçu le signale.

### 2.3 Les types de flux

Le catalogue vient de `mcast_plans.flow_classes()`.

| Clé | Essence | Critères |
|---|---|---|
| `video_all` | vidéo | aucun (tous les formats) |
| `video_1080i50` | vidéo | entrelacé, 1920×1080, 25 images |
| `video_1080p50` | vidéo | progressif, 1920×1080, 50 images |
| `video_2160p50` | vidéo | progressif, 3840×2160, 50 images |
| `audio_all` | audio | aucun (tous les formats) |
| `audio_2ch_1ms` | audio | 2 canaux, paquets de 1 ms |
| `audio_8ch_1ms` | audio | 8 canaux, 1 ms |
| `audio_16ch_125us` | audio | 16 canaux, 0,125 ms |
| `audio_64ch_125us` | audio | 64 canaux, 0,125 ms |
| `anc` | ANC | aucun |

Une ligne sans type de flux s'applique à toutes les essences. Le catalogue donne aussi un débit
nominal par type (`bitrate_mbps`, calculé à partir du format) et un type de charge utile RTP. Le débit
est une information du catalogue : l'allocation ne s'en sert pas et l'éditeur ne l'affiche pas.

Le réglage `mcast_flow_classes` (liste JSON) remplace le catalogue. Une entrée invalide est ignorée et
journalisée. Aucun écran ne l'édite.

### 2.4 Les réglages communs au plan

| Réglage | Valeurs | Effet |
|---|---|---|
| Règle pour B (`b_rule`) | `octet`, `same`, `none` | Comment le bloc B se déduit du bloc A (§2.5) |
| Mode de rang (`rank_mode`) | `slot`, `counter` | `slot` : le rang vient du numéro de sortie. `counter` : prochaine adresse libre de la plage |
| Mode de port (`port_mode`) | `fixed`, `increment` | `fixed` : un port par essence, celui de la ligne. `increment` : le port que l'appelant propose |
| Dernière adresse réservée (`reserve_last`) | oui / non | La dernière adresse d'un bloc /xx n'est jamais allouée |

Valeurs par défaut de l'API : `slot`, `increment`, non. Un nouveau plan créé dans l'éditeur démarre en
`counter`, `fixed`, dernière adresse réservée.

Le port d'une ligne se règle par essence (`port_video`, `port_audio`, `port_anc`). Sans port de ligne,
l'appelant propose ses ports de repli (5000 vidéo, 5004 audio, 5008 ANC).

### 2.5 La règle pour B

| Mode | Bloc B de la ligne |
|---|---|
| `octet` (octet 1 à 4, décalage non nul) | Le bloc A dont l'octet choisi est décalé. Le bloc B a la même taille |
| `same` | Le même bloc que A : un seul groupe pour les deux fabrics |
| `none` | Aucune déduction : chaque ligne reçoit un B saisi à la main, ou n'en a pas |

L'enregistrement du plan recalcule le B de chaque ligne qui suit la règle. La valeur envoyée par le
client est ignorée pour ces lignes. Une ligne marquée « saisie à la main » garde son B, qui doit avoir
autant d'adresses que son A.

Refus à l'enregistrement :

- un octet dans la partie hôte d'un bloc /xx (le B ne garderait plus les rangs) ;
- un octet qui sort de 0–255 ;
- un bloc B hors de 224.0.0.0/4.

Une plage « première – dernière » n'a pas de préfixe : seul son chevauchement avec les autres plages
est contrôlé.

L'enregistrement d'un plan range aussi sa règle comme règle proposée pour le prochain plan du même
réseau média (`media_networks.default_b_rule`).

### 2.6 La plage : `/xx` ou première – dernière

| Saisie | Première adresse | Dernière adresse |
|---|---|---|
| `239.20.5.0/24` | Jamais allouée (adresse de réseau) | Allouée, sauf si « dernière adresse réservée » |
| `239.20.5.1-239.20.5.50` | Allouée | Allouée. « Dernière adresse réservée » est sans effet |

Le rang 0 d'un bloc /xx n'existe donc pas. Dans une plage explicite, la sortie 1 prend la première
adresse. Dans un bloc /xx, elle prend le rang 1.

Les deux saisies se convertissent dans l'éditeur. La conversion /xx vers « première – dernière » affiche
les adresses réellement attribuables.

### 2.7 Le rang, quand il vient du numéro de sortie

Avec `rank_mode = slot`, le rang vaut :

`décalage de l'essence + n° de sortie (+ 1 dans un bloc /xx) + (n° d'audio × pas audio)`

Le n° de sortie compte à partir de 1 pour l'opérateur. Les décalages et le pas sont réglables par ligne.
Valeurs par défaut :

| | Vidéo | ANC | Audio |
|---|---|---|---|
| Décalage d'essence | 0 | 50 | 100 |
| Pas par audio | | | 100 |

Une ligne qui porte un type de flux part de 0 pour son essence (l'opérateur a déjà séparé les
plages). Le pas audio vaut 100 même sur une ligne typée. Sur un petit bloc, le second audio sort du bloc :
l'allocation émet l'alerte « hors plage », puis prend la première adresse libre. Réglez le pas, ou
passez en `counter`.

## 3. Comment un flux reçoit son adresse

### 3.1 Quel plan s'applique

L'allocateur (`allocate_multicast_for`) prend, dans l'ordre :

1. le plan de la **paire** de l'interface de sortie ;
2. le plan de l'**interface** (interface sans paire) ;
3. le plan du **réseau média** (pour une paire : le réseau du membre du fabric A, puis celui de l'autre) ;
4. les **anciennes règles** `mcast_ranges` sans plan (interface avant réseau) ;
5. le **pool global**.

Dès qu'un plan couvre le port, il décide seul. Il n'y a jamais de repli vers un autre plan, vers une
ancienne règle ou vers le pool.

### 3.2 Quelle ligne

La ligne la plus spécifique gagne : essence, puis nombre de critères de format qui correspondent. Les
critères se comparent comme des nombres (50 vaut 50.0). Le format d'un flux audio est lu à une source
unique, `io2110_flows.audio_format` (canaux, temps de paquet), quel que soit ce que l'appelant passe.

### 3.3 Quel fabric

Le fabric d'une interface vient de **sa couleur** et du **premier fabric de son réseau média** :

- sur une paire, le membre dont la couleur est le premier fabric de son réseau est le fabric A ;
- si les deux membres sont sur deux réseaux en désaccord, le réseau du membre rouge tranche ;
- une interface sans paire est toujours le fabric A.

Le fabric ne vient **pas** de l'indice de la jambe. La jambe 0 d'un flux est l'interface sur laquelle
sa sortie est épinglée, rouge ou bleue. Une sortie épinglée sur le second fabric reçoit une adresse du
bloc B. Source unique : `allocations.fabric_of`.

### 3.4 Le rang est celui du flux

Le rang est une propriété du flux, partagée par ses deux jambes. L'adresse sur le fabric A est la base
du bloc A + rang. L'adresse sur le fabric B est la base du bloc B + rang.

La première jambe allouée choisit le rang, quelle que soit sa couleur. La seconde le suit, avec le même
port. Si les deux fabrics portent le même groupe (`same`), la seconde jambe n'écrit rien dans le registre
de réservation.

Avec `rank_mode = slot`, la première jambe prend le rang planifié. S'il est tenu par un autre flux, elle
prend le premier rang libre. Pour une paire dont les blocs A et B diffèrent, chaque rang essayé doit être libre
sur les deux fabrics.

### 3.5 Un groupe par flux

Un abonnement IGMP porte sur un **groupe**, pas sur un port. Deux flux sur la même adresse et sur deux
ports ne sont pas séparables côté récepteur : un décodeur abonné à l'audio reçoit aussi la vidéo. Une
adresse est donc libre seulement si **aucun autre flux** ne la tient, sur aucun port. La règle vaut
pour les plans, les anciennes règles et le pool.

La réservation est atomique (table `mcast_allocations`). Si deux allocateurs prennent le même groupe
en même temps, le dernier inscrit cède. Les adresses déjà allouées ne sont pas réécrites.

### 3.6 Refus

Un refus ne retombe jamais sur une autre adresse. Le flux reste sans adresse et une alerte le dit.

| Cas | Alerte |
|---|---|
| Aucune ligne du plan ne correspond au flux | `alert.net.mcast_plan_unmatched` |
| Le flux sort sur le fabric B et la ligne n'a pas de bloc B | `alert.net.mcast_plan_no_b_block` |
| L'adresse B déduite est tenue par un autre flux (la jambe B reste vide) | `alert.net.mcast_jambe_b_hors_regle` |
| Plage épuisée | `alert.net.plage_multicast_epuisee` |
| Rang planifié hors du bloc (puis balayage de la plage) | `alert.net.plan_multicast_hors_plage` |

## 4. Les adresses saisies à la main

Une adresse saisie par l'opérateur (route `POST /api/mtl/<vmid>/tx/<slot>/dest`, ou une édition
différée de `tx_maintenance`) est marquée `<clé>_manual` quand elle diffère de l'adresse enregistrée.
Renvoyer la même adresse laisse le marqueur tel quel.

- La re-planification ne réécrit **jamais** une adresse marquée. Elle la rapporte avec l'état `manuel`.
- L'aperçu la montre `manuel`.
- Sous un plan, l'adresse saisie doit tomber dans un bloc d'une ligne du plan, du fabric de l'interface de
  cette jambe. Sinon la route refuse (400).
- Une purge d'adresses de projet (restauration) retire aussi les marqueurs.

À ne pas confondre avec le « B saisi à la main » d'une ligne (§2.5) : celui-ci décrit un bloc, pas un flux.

## 5. Ce que l'enregistrement d'un plan ne fait pas

**Enregistrer, modifier ou supprimer un plan ne déplace aucun flux.** Les adresses déjà réservées
restent où elles sont, y compris après la suppression du plan. Un plan s'applique aux nouvelles
allocations : déploiement, modèle de carte (layout), flux ajouté.

Seule la **re-planification** d'un moteur déplace des adresses (Sorties 2110 → « Plan multicast »,
`POST /api/mtl/<vmid>/tx/mcast-plan`). Sans `apply:true`, elle simule. Avec, elle :

- ne renumérote que les flux qui ont déjà une adresse ;
- ne touche pas aux ports ;
- laisse en place un flux dont l'adresse planifiée est tenue par un autre (état `conflit`) ;
- ne touche pas aux adresses marquées manuelles ;
- réserve les nouvelles adresses avant de libérer les anciennes, et pousse le tout au moteur en un
  seul lot (un seul à-coup).

L'aperçu de l'éditeur (`POST /api/mcast_plans/preview`) simule le plan
courant, enregistré ou non, sur les flux TX des moteurs 2110 qu'il concerne. Il n'écrit et ne réserve
rien. États : `ok`, `new` (pas encore d'adresse), `move` (changera à la re-planification), `manual`,
`unmatched`. Il indique aussi la capacité de chaque ligne et les chevauchements avec d'autres plans.

## 6. Anciennes règles et pool global

**Anciennes règles** : les lignes `mcast_ranges` sans `plan_id`, portées par un réseau média ou par une
interface. Elles s'appliquent encore aux ports qu'aucun plan ne couvre (interface avant réseau, règle la
plus spécifique). L'éditeur les liste et permet de les supprimer tant qu'il en reste. Il n'y a plus de
formulaire pour en créer. L'API (`/api/mcast_ranges`) accepte encore leur création.

**Pool global** (`mcast_pool_base`, `mcast_pool_size`, par défaut 239.100.0.0 et 4096 adresses) : utilisé
quand ni un plan ni une règle ne couvre le port, et par les émetteurs créés par NMOS (§8). La première
adresse du pool n'est jamais allouée.

Le pré-vol de capacité d'un projet compte les lignes de plan comme des plages.

## 7. L'audit

`app/mcast_audit.py` tourne dans un fil, jamais dans la boucle de surveillance. Il lit la **base**, pas
le réseau : un émetteur tiers sur nos adresses reste invisible. Alerte par transition.

| Contrôle | Période | Alerte |
|---|---|---|
| Groupe partagé entre plusieurs flux (deux jambes d'un même flux sur un groupe `same` ne comptent pas) | 60 s | `mcast_groupe_partage` |
| Flux hors plan : la simulation de re-planification trouve des adresses à changer (jambe 0 seulement) | 300 s | `mcast_hors_plan` |
| Jambe B hors règle : sous un plan, l'adresse B n'est pas celle du bloc B au rang de A. Les jambes B marquées manuelles sont ignorées | 300 s | `mcast_jambe_b_hors_regle` |

## 8. Ce qui n'est pas fait

Dit sans détour. Les points côté moteur attendent le plugin `2110_io`.

- **Ordre des jambes annoncé.** Le réglage « premier fabric » ne pilote que l'orchestrateur. L'ordre des
  jambes dans le SDP et dans NMOS ne le suit pas encore.
- **TTL** du multicast : pas de réglage.
- **Abonnement SSM en réception** : pas fait.
- **Nouvel abonnement IGMP au retour d'un lien** : pas fait (cf. TODO, « le retour sur un lien rétabli
  prend 46 s »).
- **Nombre de canaux audio par flux** : le moteur n'émet que 8 canaux (L24, 48 kHz). L'orchestrateur lit
  le format par flux (`audio_format`) pour le choix de la ligne, mais le moteur ne le suit pas.
- **Émetteurs créés par NMOS** : ils prennent leur adresse dans le pool global, jamais dans un plan.
- **Import et export CSV** des plans : absents.
- **Catalogue des types de flux** : aucun écran d'édition. On le remplace par le réglage
  `mcast_flow_classes`.
- **Débit réservé par type** : le catalogue le porte, aucun mécanisme ne le contrôle.

## 9. Exemples

Toutes les adresses sont inventées. Un autre préfixe que 239 convient (par exemple 229).

### 9.1 Un bloc par équipement

Plan sur la **paire** d'un nœud. Règle pour B : octet 2, décalage +100. Mode de rang `slot`, mode de
port `fixed`, dernière adresse réservée.

| | Valeur |
|---|---|
| Ligne | Sans type de flux (toutes essences) |
| Plage A | `239.20.5.0/24` |
| Plage B (déduite) | `239.120.5.0/24` |
| Ports | vidéo 50000, audio 50010, ANC 50020 |

Pour la sortie 3, avec deux audios :

| Flux | Rang | Fabric A | Fabric B |
|---|---|---|---|
| Vidéo | 3 | `239.20.5.3` | `239.120.5.3` |
| ANC | 53 | `239.20.5.53` | `239.120.5.53` |
| Audio 1 | 103 | `239.20.5.103` | `239.120.5.103` |
| Audio 2 | 203 | `239.20.5.203` | `239.120.5.203` |

Le rang 0 (`.0`) et le rang 255 (`.255`, réservé) ne sont jamais alloués. Un second équipement a son
propre plan, sur sa propre paire, avec son propre bloc.

### 9.2 Une plage par type de flux

Plan sur le **réseau média**. Règle pour B : octet 2, décalage +100. Mode de rang `counter`, dernière
adresse réservée. Le premier fabric du réseau est le rouge : le rouge porte A, le bleu porte B.

| Type de flux | Plage A | Plage B (déduite) | Adresses allouables |
|---|---|---|---|
| Vidéo 1080p50 | `239.20.1.0/28` | `239.120.1.0/28` | 14 |
| Audio 8 canaux, 1 ms | `239.20.2.0/27` | `239.120.2.0/27` | 30 |
| ANC | `239.20.3.0/28` | `239.120.3.0/28` | 14 |

Le premier flux vidéo 1080p50 prend `239.20.1.1` (rouge) et `239.120.1.1` (bleu), le suivant `.2`, et
ainsi de suite. Une vidéo 2160p50 ne correspond à aucune ligne : elle est refusée, avec une alerte. Une
ligne « Vidéo (tous les formats) » l'accepterait.

Même plan avec la plage A saisie en « première – dernière » : `239.20.1.1-239.20.1.14` donne les
mêmes 14 adresses.

## 10. Ce qui garde le comportement

| Fichier | Garde |
|---|---|
| `tests/verif_mcast_plans.py` | Plans par paire, interface, réseau ; fabric (premier fabric bleu, sortie épinglée sur le second) ; rang partagé ; refus ; changement de règle ; aperçu ; plages explicites ; anciennes règles intactes |
| `tests/verif_mcast_plan_axes.py` | Les deux formes des exemples, par le vrai hook de déploiement ; même groupe ; B tenu par un autre ; dernière adresse réservée ; port fixe ; adresses manuelles ; audit de la jambe B |
| `tests/verif_mcast_rules.py` | Critères de format comparés comme des nombres ; un groupe par flux |
| `tests/verif_mcast_audit.py` | Les contrôles d'audit |
| `tests/verif_layout_mcast_rules.py` | Un modèle de carte alloue par les mêmes règles que le hook de déploiement |
| `tools/bancs/banc_ui_mcast/` | Banc navigateur rejouable de l'éditeur (Chromium, 1440 et 1024). Hors CI : il exige un navigateur |
