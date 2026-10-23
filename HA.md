# HA — Paire de contrôleurs (warm-standby) & bascule manuelle

Bobi.Studio se déploie en **paire de contrôleurs** : un **actif** (pilote la production — surveillance
des nœuds, NMOS/Ember+/ATEM/TSL, sampler PTP, sauvegardes) et un **standby** (boote passif, UI en
**lecture seule**, ne pilote rien). La bascule du **pilotage** est **manuelle** (warm-standby) — pas de
quorum, pas d'auto-failover : c'est un choix (un broadcast n'a pas besoin d'un basculement automatique
qui pourrait partir en split-brain ; l'opérateur décide).

Deux mécanismes complètent ce choix sans le contredire :

- **Le standby ne se tait plus.** Il sonde l'actif toutes les 15 s (`/api/ha/peer`, en mTLS) et, après `ha_watchdog_fails` échecs (~60 s), lève une **alarme** : badge de navigation rouge
  et clignotant sur toutes les pages, bandeau dans Réglages → Haute disponibilité. Il ne se promeut
  toujours pas tout seul — mais l'opérateur n'a plus à découvrir la panne par lui-même.
- **La VIP, elle, bascule toute seule** (keepalived/VRRP, opt-in — cf. plus bas).

Implémentation : `app/ha.py` (rôle, réplication, chien de garde) + `app/ha_appairage.py` (partage
de l'autorité) + `app/ha_tls.py` (transport mTLS) + `app/vip.py` (keepalived) + Réglages → **Haute
disponibilité** (section de l'onglet Réglages → Cluster & Réseau → Cluster ; c'est elle que ce
document désigne par « Réglages → Haute disponibilité »).

## Modèle

| Rôle | `control_role` | Pilote ? | UI |
|------|----------------|----------|-----|
| Actif | `active` (défaut) | oui | complète |
| Standby | `standby` | non (garde 409 sur les mutateurs `/api/*`) | lecture seule + bascule |

- **Réplication d'état** (B3-2b) : l'actif pousse un **snapshot SQLite cohérent** vers le standby
  (`ha_standby_url`) toutes les `ha_replicate_interval_min` minutes. Le standby **stage** le snapshot
  (`ha_staging/db_replica.db`) — il ne l'applique PAS tant qu'on ne le promeut pas.
- **Une seule autorité pour la paire.** Depuis la migration mTLS du plan de contrôle (2026-08-30), un
  agent-nœud n'accepte que les contrôleurs signés par la CA qui l'a enrôlé. Les deux contrôleurs
  doivent donc partager **la même CA**, faute de quoi un standby promu ne pilote **aucun** nœud — ce
  qui était le cas jusqu'au 2026-10-10 : rien ne la transmettait. C'est l'**appairage** qui la
  transmet (cf. pré-requis).
- **Transport mTLS dédié** : la réplication et la sonde de pair passent par une écoute à part
  (`ha_tls_port`, **5444** par défaut) qui n'accepte **que des certificats de contrôleur** de la
  CA commune — pas ceux des nœuds ni des conteneurs, pourtant signés par la même CA. Le secret
  partagé (`update_token`) ne sert **plus** à la HA ; il reste celui des mises à jour entre instances.
  Jusqu'au 2026-10-10, la base entière partait en HTTP clair avec ce secret en en-tête, rejouable.
- **VIP de management** : une IP flottante que la production/les opérateurs visent. Avec keepalived
  configuré (ci-dessous) elle **suit toute seule** ; sans, elle se déplace **à la main**. Le plan
  ST 2110 (médias) n'est pas concerné.

## Pré-requis (à froid, une fois)

1. **Appairer** les deux contrôleurs (Réglages → Haute disponibilité → Appairage) :
   - sur **l'actif** : « Générer un code d'appairage » — un code de 32 caractères, valable
     **10 minutes**, **une seule fois** ;
   - sur **le standby** : l'adresse de l'actif et ce code, puis « Appairer ».

   Le standby adopte la CA de l'actif ; **l'ancienne est conservée** dans `tls.avant-appairage-<horodatage>`
   (réversible). Vérifier que **les deux écrans affichent la même empreinte d'autorité** — c'est la
   vraie preuve de l'appairage. Le code ne passe jamais sur le réseau : il sert à prouver la demande,
   à authentifier l'actif en retour, et à chiffrer l'autorité pendant son transfert. Cinq preuves
   fausses le détruisent (alerte dans la piste d'audit).

   **Pare-feu** : ouvrir le port `ha_tls_port` (5444) entre les deux contrôleurs, dans les deux sens.
2. Sur **l'actif** : `ha_standby_url = http://<standby>:5000` (seul l'**hôte** compte : la réplication passe par `ha_tls_port`) + intervalle (Réglages → Haute
   disponibilité → Enregistrer). Vérifier que le statut passe à « ↑ dernier push OK ».
3. Sur **le standby** : `control_role = standby` (Réglages → Haute disponibilité → Appliquer) puis
   **redémarrer le service**. Le badge **STANDBY** apparaît ; le statut montre « ↓ replica reçu il y a … ».
4. Choisir la **VIP** (ex. `192.0.2.250/24`) et l'interface management (ex. `eth0`). Soit on la pose
   à la main sur l'actif, soit on active keepalived (ci-dessous) sur les **deux** contrôleurs.

## VIP automatique (keepalived / VRRP) — opt-in

Réglages → Haute disponibilité → **VIP de management**. À configurer sur les **deux** machines, avec
la **même** adresse, le **même** VRID et le **même** secret (8 caractères max — le protocole tronque
au-delà, et deux contrôleurs « au même secret » ne se verraient jamais). Bouton **Installer keepalived
et appliquer** au premier passage ; ensuite **Enregistrer et appliquer**.

Ce que fait la conf générée (`/etc/keepalived/keepalived.conf`, marquée en en-tête — un fichier qui
n'est pas le nôtre n'est **jamais** écrasé) :

- **priorité VRRP dérivée du rôle** (`vip_priority_active` 150 / `vip_priority_standby` 100), et conf
  re-rendue à chaque promote/demote **et au boot** → une bascule planifiée déplace l'adresse sans
  toucher au réseau ;
- **`track_script`** qui interroge `http://127.0.0.1:5000/api/update/ping` : orchestrateur muet →
  priorité − 60 → l'autre contrôleur prend l'adresse en ~5 s.

> **La VIP suit la DISPONIBILITÉ, pas le pilotage.** Après une panne de l'actif, elle mène à un
> contrôleur en **lecture seule** : c'est voulu — c'est la page qui porte l'alarme et le bouton
> **Promouvoir**. Une adresse qui mène au bouton vaut mieux qu'une adresse qui ne mène nulle part.
>
> Corollaire : un ancien actif qu'on rallume **sans l'avoir rétrogradé** repart avec la priorité 150.
> Il ne volera pas l'adresse au maître en place (VRRP ne préempte qu'à priorité *strictement*
> supérieure), mais il repart en **pilotant** — c'est le split-brain que la règle d'or interdit.
> Rétrograder avant de rallumer.

**La VIP est inscrite dans le certificat des DEUX contrôleurs.** En mode web `tls`, le navigateur
compare l'adresse visée aux adresses du certificat : sans elle, la VIP donnerait un cadenas rouge
(dès que la CA interne est installée sur les postes), et pareil sur le standby après bascule. Elle y
entre toute seule — à l'enregistrement de la VIP (même sans keepalived : une VIP posée à la main
compte aussi), au démarrage de chaque contrôleur, et dans le certificat que l'actif émet au standby
à l'appairage. Même clé, même échéance ; les écoutes HTTPS et HA servent le nouveau certificat
sans redémarrage. Ajout seulement : une ancienne VIP reste dans le certificat (elle n'ouvre rien
sans notre clé). Les agents-nœuds et la réplication ne vérifient aucune adresse : rien ne change
pour eux.

## Bascule planifiée (maintenance de l'actif) — ordre anti-split-brain

> Règle d'or : **un seul actif à la fois** et **un seul porteur de VIP à la fois**.

1. **Vérifier la réplication** : sur le standby, statut « ↓ replica reçu il y a < intervalle ».
2. **Rétrograder l'actif** : Réglages → Haute disponibilité → **Rétrograder (passer en veille)**.
   (= `control_role=standby` + redémarrage ; il cesse de piloter.)
3. **Déplacer la VIP** — *rien à faire si keepalived est configuré* (l'étape 2 a déjà re-rendu la
   priorité et l'adresse a migré). Sinon, à la main :
   ```bash
   # sur l'ANCIEN actif (qu'on vient de démettre) :
   ip addr del <VIP>/<bits> dev <mgmt-if>
   # sur le FUTUR actif (le standby) :
   ip addr add <VIP>/<bits> dev <mgmt-if>
   # (rafraîchir le cache ARP des voisins)
   arping -U -c 3 -I <mgmt-if> <VIP>   # ou ndppd/gratuitous selon le réseau
   ```
4. **Promouvoir le standby** : Réglages → Haute disponibilité → **Promouvoir (devenir actif)**.
   (= applique le dernier replica stagé sur sa DB **après un backup de sûreté** `db_bobistudio-
   prepromote-<ts>.db`, `control_role=active`, redémarrage en mode pilotant.)
5. **Vérifs post-bascule** sur le nouvel actif : rôle ACTIF (badge disparu), NMOS
   (`/x-nmos/node/v1.3/{senders,receivers}`), PTP locké, conteneurs des nœuds présents/au débit.

## Bascule d'urgence (actif HS / injoignable)

L'ancien actif est déjà down → pas de démote possible. Le standby a levé l'alarme « L'ACTIF NE
RÉPOND PLUS » (badge rouge sur toutes les pages) au bout d'une minute environ.
1. **S'assurer qu'il est bien hors-ligne** (sinon couper son alim/réseau) — sinon double pilotage.
2. **Poser la VIP** sur le standby — *automatique avec keepalived* (le `track_script` l'a déjà
   déplacée en ~5 s : c'est d'ailleurs pour ça que la page qui affiche l'alarme est joignable sur
   la VIP). Sinon, à la main : `ip addr add …` + `arping -U`.
3. **Promouvoir** le standby (Réglages → Haute disponibilité → Promouvoir). Il applique le dernier
   replica reçu — l'écart de fraîcheur = au plus `ha_replicate_interval_min` minutes.

## Retour arrière

- Le **backup de sûreté** pré-promote est dans `backups/db_bobistudio-prepromote-<ts>.db` : pour annuler
  une promotion, arrêter le service, restaurer ce fichier sur `db_bobistudio.db`, remettre
  `control_role` au besoin, redémarrer.
- Tant qu'un standby n'est pas promu, il est sans risque (lecture seule) — on peut le laisser tourner.

## Limites connues (assumées)

- **Bascule du pilotage manuelle** (par conception : pas de quorum → pas d'auto-promotion). Le standby
  alerte, il ne décide pas. La VIP, elle, bascule automatiquement dès que keepalived est configuré.
- Le chien de garde et keepalived sondent **la même chose vue de deux endroits** (l'orchestrateur
  répond-il ?) : une coupure purement réseau *entre* les deux contrôleurs armera l'alarme et déplacera
  l'adresse alors que l'actif va très bien. D'où la règle d'or, inchangée — vérifier que l'ancien actif
  est bien hors-ligne **avant** de promouvoir.
- La fraîcheur du standby = bornée par l'intervalle de réplication (snapshot complet, pas du streaming
  WAL) — adapter `ha_replicate_interval_min` au volume de changements.
- E2E réel (promote → reboot → pilotage) à valider avec un **2ᵉ box de contrôle** (le code est testé en
  loopback + sur fichiers jetables).
