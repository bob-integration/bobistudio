# Interface Bobi.Tools → Bobi.Studio : télémétrie d'appareils tiers (proposition, 2026-10-17)

Premier besoin : afficher sur une tranche de la console audio l'état du récepteur HF qui l'alimente (batterie,
RF, mute de l'émetteur, nom du canal), et remonter les alertes. Les pilotes d'appareils (Shure, Sennheiser…)
vivent dans Bobi.Tools ; Bobi.Studio ne consomme qu'une interface GÉNÉRIQUE, indépendante de la marque. Rien de
spécifique au HF dans le transport : un autre type d'appareil (intercom, réseau, alimentation) passe par la même
interface.

## Principes

- **HTTP + JSON, versionné** (`/api/v1/…`), et un **flux poussé SSE** (Server-Sent Events) pour l'état vivant.
  - SSE plutôt que WebSocket : sens unique (on lit), reconnexion automatique, texte, rien de plus qu'un GET.
  - Pas de MQTT : il ajoute un broker à installer, superviser et sécuriser, pour un seul consommateur.
- **Instantané + deltas, avec un compteur `rev`.** À la connexion, Bobi.Tools envoie l'instantané complet, puis
  seulement les changements. Après une coupure, le client se reconnecte avec le dernier `rev` vu : Bobi.Tools
  rejoue ce qui manque, ou renvoie un instantané s'il ne l'a plus.
- **Identité stable** d'un appareil : `<vendor>:<model>:<serial>`, jamais l'adresse IP (elle change). Un canal
  = son index dans l'appareil.
- **Vocabulaire normalisé** des attributs, commun à toutes les marques, plus un champ `raw` libre pour le
  propre à la marque. Bobi.Studio n'interprète que le vocabulaire normalisé.
- **Valeurs brutes et horodatées ; les ALERTES sont décidées par Bobi.Studio**, qui compare à l'intention : une
  batterie faible sur un micro en service alerte, sur un micro rangé non. Bobi.Tools transmet aussi les
  avertissements que l'appareil émet lui-même (`device_warnings`), sans les reclasser.
- **Lecture seule en v1.** Les commandes (renommer un canal, couper un émetteur) viendront plus tard, sur la
  même base.
- **Sécurité** : HTTPS, jeton d'accès (`Authorization: Bearer …`) propre à chaque client, révocable.

## Routes

`GET /api/v1/devices` → inventaire :
```json
{"rev": 1834, "devices": [
  {"id": "shure:ulxd4q:2X4471", "vendor": "shure", "model": "ULXD4Q", "name": "Récepteur plateau",
   "address": "192.0.2.21", "online": true, "firmware": "2.7.6",
   "channels": [{"index": 1, "kind": "wireless_rx", "name": "PRESENTATEUR"}, {"index": 2, "kind": "wireless_rx", "name": "INVITE 1"}]}]}
```

`GET /api/v1/devices/{id}/state` → instantané d'un appareil (même forme que l'événement `snapshot`).

`GET /api/v1/stream?since_rev=<n>&devices=<id,id>` → flux SSE (`text/event-stream`). Événements :
- `snapshot` : `{rev, ts, devices: {<id>: {online, channels: {<index>: {<attr>: {value, unit?, ts}}}}}}`
- `delta` : `{rev, ts, device, channel, attrs: {<attr>: {value, unit?, ts}}}` (seulement ce qui a changé)
- `device` : `{rev, ts, device, online: true|false}` (appareil apparu, perdu, revenu)
- `inventory` : `{rev, ts}` (un appareil ajouté ou retiré : relire /devices)
- `heartbeat` : `{rev, ts}` toutes les 5 s (le client sait que le flux vit)

`rev` croît strictement sur toute l'interface ; `ts` = millisecondes UTC (horloge de Bobi.Tools, synchronisée).

## Vocabulaire normalisé, kind `wireless_rx` (v1)

| Attribut | Type | Unité | Remarque |
|---|---|---|---|
| `name` | chaîne | | nom du canal dans l'appareil |
| `frequency_hz` | entier | Hz | |
| `tx_present` | booléen | | un émetteur est vu |
| `tx_type` | chaîne | | modèle d'émetteur (ex. « ULXD2 ») |
| `tx_muted` | booléen | | mute de l'émetteur |
| `battery_pct` | entier 0-100 | % | null si inconnu |
| `battery_minutes` | entier | min | autonomie restante, null si l'appareil ne la donne pas |
| `battery_type` | chaîne | | « li-ion », « alkaline »… |
| `rf_dbm` | nombre | dBm | niveau RF (antenne la meilleure) ; `rf_a_dbm` / `rf_b_dbm` en diversité |
| `audio_dbfs` | nombre | dBFS | niveau audio crête, 1 Hz suffit (le vu-mètre fin, c'est la console) |
| `interference` | booléen | | alerte d'interférence de l'appareil |
| `encrypted` | booléen | | |
| `device_warnings` | liste de chaînes | | avertissements émis par l'appareil, tels quels |
| `raw` | objet | | propre à la marque, libre |

Un attribut que l'appareil ne fournit pas est ABSENT, jamais à zéro (zéro serait une batterie vide).

## Cadence

Événementielle : un delta à chaque changement. Les appareils qui s'interrogent par sondage le font côté Bobi.Tools
(environ 1 s pour batterie et RF) ; Bobi.Tools ne pousse que ce qui a changé. Pas de niveau audio rapide dans ce
flux.

## Côté Bobi.Studio (pour mémoire)

- Réglages : URL de Bobi.Tools et jeton ; un service `services/bobitools` (contrôleur actif seul) tient le flux SSE
  et garde l'état en mémoire.
- Une tranche se LIE à un canal (`<device id>` + index), attaché à l'emplacement de la console.
- Affichage sur la tranche, alertes comparées à l'intention, journal d'exploitation et états pour les macros.

---

## Retenu par Bobi.Tools, avec 7 amendements (2026-10-17)

**Fait référence : `TELEMETRIE.md` §7 dans le dépôt bobi.tools.** Ce qui suit en est le résumé, pour écrire le client.

- **Base** : `http://<hôte Bobi.Tools>:5000/api/telemetry/v1/` (HTTP en v1 sur le VLAN de contrôle ; HTTPS plus tard,
  seule l'URL changera). Routes : `GET devices`, `GET devices/<id>/state`, `GET stream?devices=<id,id>` (SSE).
- **Auth** : `Authorization: Bearer btk_…`, jeton créé dans Bobi.Tools → Réglages → Système → Clients API, portée
  `telemetry.read`, montré une seule fois, révocable. **401 / 403 : ne pas boucler** — alerter et attendre que le
  réglage soit corrigé.
- **Amendements** :
  1. `epoch` (tiré au démarrage de Tools) sur chaque réponse et chaque événement : s'il change, `rev` est reparti de
     0 → jeter l'état, repartir de l'instantané.
  2. Reprise SSE native : `id: <epoch>-<rev>` ; se reconnecter avec `Last-Event-ID` (prime sur since_rev / epoch) ;
     Tools rejoue s'il a encore les événements, sinon envoie un `snapshot`.
  3. **Absent ≠ null ≠ 0** : absent = l'appareil ne fournit pas l'attribut ; `null` dans un delta = valeur plus
     connue (l'EFFACER de l'affichage, ne pas garder l'ancienne) ; 0 = vraie valeur. `tx_present: false` → tout ce
     qui vient de l'émetteur passe à `null`.
  4. `ts` = heure d'observation par Tools ; un appareil `online: false` garde ses dernières valeurs et leur `ts` :
     au client de les juger PÉRIMÉES.
  5. Batterie : `battery_bars` / `battery_bars_max` (5 sur ULX-D) ; `battery_pct` seulement si l'appareil le donne,
     jamais calculé (piles AA sur ULX-D = barres seules) → l'alerte batterie doit savoir utiliser l'un OU l'autre.
  6. Zone morte : dBm et dBFS ne produisent un delta qu'à partir d'un écart de 1 dB.
  7. Préfixe `/api/telemetry/v1/` (au lieu de `/api/v1/`).
- **Client** : `snapshot` remplace tout l'état ; `delta` se fusionne attribut par attribut (null efface) ; `device`
  met à jour `online` ; `inventory` → relire `GET devices` ; `heartbeat` toutes les 5 s — sans aucun événement
  pendant ~15 s, couper et se reconnecter avec `Last-Event-ID`.
- **Test sans matériel** : pilote `sim`, identifiants `sim:…`.
- **État** : chantier lancé côté Bobi.Tools ; l'exploitant préviendra quand ce sera en phase de test. Côté studio,
  rien n'est commencé.
