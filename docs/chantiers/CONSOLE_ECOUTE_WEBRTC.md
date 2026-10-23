# Écoute de la console audio en WebRTC (Opus) — étude et réalisation

*2026-10-16. Journal de chantier : il décrit ce qui a été mesuré et décidé ce jour-là.*

## Besoin

Aujourd'hui, l'écoute d'une console (`plugins/audio_console`) passe par `GET :8082/listen.pcm`, relayé
par l'orchestrateur (`app/plugin_streams.py`) : du L16 stéréo 48 kHz non compressé, soit **1 536 kbit/s
de charge utile par auditeur** (plus l'en-tête TCP/HTTP), lu par l'AudioWorklet du navigateur. Le sens
retour, l'ordre (talkback), passe par `POST /talk.pcm` : int16 mono 48 kHz, soit **768 kbit/s en montée**.

L'exploitant demande une alternative compressée pour les cas où le non compressé ne passe pas (liaison
distante, 4G, VPN). La console garde le L16 : c'est le chemin de référence sur le réseau local.

## Les trois voies examinées

### (a) Flux MXL du bus d'écoute → `streamer` → passerelle → WHEP — écartée

- **Le `streamer` ne sait pas faire de l'audio seul.** Il lance toujours ffmpeg avec
  `-f rawvideo -i pipe:0` en entrée 0. Son `_audio_plan` suppose que le flux 0 est la vidéo. Son ancre A/V
  (`_av["anchor_ns"]`) est posée à la PREMIÈRE écriture vidéo (`_StdinTemoin.write`), et le feeder audio
  attend cette ancre : sans vidéo, il ne coulerait jamais. Ajouter un mode audio seul toucherait le cœur
  d'un script de 1 762 lignes, dont le calage A/V a demandé des semaines de mesures.
- **Il exige un `expose` sur le bus d'écoute.** C'est un changement de structure de la console, donc une
  décision de l'exploitant. Sur la console de banc (1160), le bus d'écoute personnel « Monitoring Cyril
  Mazouer » n'est pas exposé.
- **Le coût est disproportionné.** Il faut un conteneur `media` par console, voire par utilisateur
  (`monitor-u<uid>` porte déjà la vidéo de l'utilisateur : le réutiliser pour l'audio couperait son
  moniteur vidéo). `core_pool` réserve 4 cœurs par `streamer` (`resources.cores: 4`), pour ~6 % d'un
  cœur de travail réel.

### (b) Encodage Opus dans le conteneur de la console — écartée

- L'image de la console (`bobi-compute:0.60`, vérifié sur 1160) ne contient **ni ffmpeg ni libopus**.
- La console est épinglée sur **un cœur** (`resources: cores 1, pin true`), et son fil audio est la
  ressource critique : 130 ms de GC suffisaient à le faire décrocher. Y ajouter un encodeur Opus et une
  pile WebRTC complète (ICE, DTLS, SRTP) reviendrait à mettre du réseau public dans le processus temps réel.

### (c) Relais à la demande DANS LA PASSERELLE, alimenté par `/listen.pcm` — **retenue**

La passerelle MediaMTX déjà déployée (Réglages → WebRTC) déclare un chemin régulier
`cl-<a>-<b>-<c>-<d>-<bus>`. Le premier lecteur WHEP de ce chemin lui fait lancer, par `runOnDemand`,
**une seule** commande ffmpeg :

```
ffmpeg … -f s16le -ar 48000 -ac 2 -i http://$G1.$G2.$G3.$G4:8082/listen.pcm?bus=$G5 \
       -c:a libopus -b:a 96k -vbr constrained -application lowdelay -frame_duration 10 \
       -f rtsp rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH
```

MediaMTX arrête cette commande 10 s après le départ du dernier lecteur (`runOnDemandCloseAfter`), et la
relance si elle meurt, par exemple au redémarrage de la console (`runOnDemandRestart`).

Pourquoi c'est la bonne voie :

- **Elle n'ajoute aucun conteneur et aucun reaper.** Le cycle de vie est celui des lecteurs, tenu par
  MediaMTX lui-même. Le modèle `app/monitor.py` (conteneur + heartbeat + reaper) n'est pas nécessaire.
- **Le contenu est exactement celui du L16** : même bus, même contrôle de moniteur (niveau, dim, cut,
  mono), même contrat `/listen.pcm`. Aucun `expose` n'est requis et rien ne change dans la console.
- **Elle passe à l'échelle.** Un seul des 4 écouteurs permis par bus (`mxl_io.MAX_LISTENERS`) est
  consommé par (console, bus), quel que soit le nombre d'auditeurs WebRTC. MediaMTX fait la diffusion.
  En L16, le 5ᵉ auditeur reçoit un 503.
- **Elle ne demande aucune coordination.** L'adresse de la console est dans le nom du chemin (groupes de
  l'expression régulière), donc l'orchestrateur n'a aucune table à pousser à la passerelle. Ce que la
  passerelle ignore, la console le refuse elle-même (bus inconnu → 404, ffmpeg s'arrête, le lecteur reçoit
  `relay_failed`).

**Sécurité.** La commande est figée : port 8082, chemin `/listen.pcm`. Seuls quatre groupes de chiffres et
un identifiant `[A-Za-z0-9_]{1,64}` y entrent. MediaMTX développe les variables puis découpe la commande
comme un shell, mais sans shell. Aucun nom de chemin accepté ne peut donc ajouter d'argument
(`tests/verif_console_ecoute_webrtc.py` le vérifie, regex jugée en sémantique RE2). Ce relais n'ouvre
rien de nouveau : la passerelle accepte déjà lecture et publication sans authentification, et le `:8082`
de la console est déjà joignable directement sur ce réseau. La signalisation, elle, passe par
l'orchestrateur, avec **les mêmes droits que `/listen.pcm`** (son entrée `control.streams`, vérifiée par
`plugin_streams._deny`).

**Prix.** La passerelle doit embarquer ffmpeg : image `bobi-webrtc:0.6.1` (paquet Debian `ffmpeg`, qui
contient libopus). Avec une image plus ancienne, le chemin n'est simplement pas déclaré (`listen_relay:
false` sur :8080) et l'orchestrateur répond `gateway_no_relay`. Rien ne casse.

## Mesures (2026-10-16)

Banc : console 1160 (`acon`, dl360-1), bus `b_lst`. La même commande ffmpeg a été lancée sur le
contrôleur (VM QEMU, 16 vCPU) et poussée vers la passerelle de production, puis vers un **MediaMTX 1.21.1
local** lancé avec la configuration générée par le nouveau script (empreinte de l'archive vérifiée).
L'audio a été relu en RTSP.

**Méthode de mesure de latence.** On capture en même temps `/listen.pcm` en direct (référence) et le
flux relu après le relais. Toutes les 2 s, on bascule le **dim** du bus d'écoute : une marche de −20 dB,
sans effet sur le programme. On compare l'heure d'arrivée de chaque marche sur les deux captures (enveloppe
RMS en fenêtres de 1 ms, passage à mi-hauteur en dB). Le dim est remis à faux à la fin (vérifié). Le
contenu du bus étant une tonalité stationnaire, la corrélation croisée était ambiguë : c'est ce qui a
imposé la marche.

| Grandeur | Mesure |
|---|---|
| Latence ajoutée, `/listen.pcm` → audio relu (relais sur le contrôleur, passerelle de prod) | médiane **57 ms**, 23 à 82 ms (8 marches) |
| Même chose, MediaMTX local avec la config générée (`runOnDemand`) | médiane **52 ms**, 13 à 72 ms (10 marches) |
| Débit Opus 96k `-vbr constrained` (fichier relu sur 10 s) | **98 kbit/s** (sans `constrained` : 141 kbit/s) |
| CPU du relais (ffmpeg, HTTP + Opus + RTSP) | **~6 % d'un vCPU** (58 ticks sur 10 s) |
| Démarrage à froid (offre WHEP → réponse, relais lancé) | **0,51 s** |
| Offre quand le relais tourne déjà (2ᵉ auditeur) | **13 ms** |
| Arrêt après le dernier lecteur | 10 à 11 s (journal MediaMTX : `runOnDemand command stopped`) |

D'où vient la dispersion de 13 à 82 ms : le démultiplexeur PCM de ffmpeg lit par paquets de 1 024 trames
(21 ms), la trame Opus fait 10 ms (+2,5 ms d'anticipation en `lowdelay`), et le lecteur RTSP de mesure
a sa propre granularité. L'alternance des valeurs (montées/descentes du dim) suggère aussi un biais du
détecteur. La médiane est le chiffre à retenir.

**Bout en bout estimé (non mesuré dans un navigateur)** = latence propre de la console, identique dans
les deux chemins, + relais (~55 ms) + réseau + tampon de gigue WebRTC du navigateur (NetEQ, adaptatif :
typiquement 20-60 ms en local, davantage sur 4G) + sortie audio. Ordre de grandeur, au-delà de la console :
**~80-150 ms en local, ~150-300 ms en 4G**. Le L16 reste le chemin le plus court en local, et c'est
pour cela qu'il reste le défaut.

**Débit sur le réseau.** Opus 96k en trames de 10 ms donne 100 paquets/s, et l'en-tête
IP/UDP/RTP/SRTP coûte ~40 kbit/s. Total : **~140 kbit/s par auditeur, contre ~1 580 kbit/s en L16**,
soit 11 fois moins. Des trames de 20 ms réduiraient l'en-tête à ~20 kbit/s au prix de 10 ms de latence :
non retenu, réglable dans `listen_relay_cmd` si la 4G le demande.

**Qualité.** Opus CELT à 96 kbit/s stéréo est transparent pour une écoute de contrôle, mais ce n'est pas
une écoute de référence. Le `lowdelay` coupe le SILK (voix bas débit), sans intérêt à ce débit.

**Ce qui n'a pas pu être mesuré** : le navigateur. Aucune pile WebRTC n'est disponible côté contrôleur
(pas d'aiortc), donc ni le tampon NetEQ, ni l'établissement ICE réel, ni la qualité perçue. La
signalisation WHEP a été testée de bout en bout contre MediaMTX 1.21.1 avec une offre SDP construite à la
main : réponse 201, candidats ICE, `Location`, PATCH et DELETE relayés. Le CPU du relais **sur le nœud de
la passerelle** (r620-2, Sandy Bridge) n'a pas été mesuré, parce que la passerelle de production tourne
encore l'image 0.5.

## Sens retour : l'ordre (talkback)

**Il reste en `/talk.pcm` pour l'instant.** Sa montée de 768 kbit/s passe sur un VPN ou un lien fixe ;
elle est limite en 4G, où le vrai problème est la gigue des POST HTTP successifs sur TCP (blocage en
tête de file) face au tampon de la console (cible 60 ms, maximum 250 ms, `TalkJitter`).

Le passage en WebRTC est faisable sur le même modèle, mais il demande une décision de sécurité et doit
faire l'objet d'un chantier à part :

- le navigateur publie son micro en **WHIP**, relayé par l'orchestrateur avec le droit `operate` du flux
  `/talk.pcm`, vers un chemin `ct-<ip>-<jeton>` ;
- un `runOnReady` de la passerelle lance `ffmpeg -i rtsp://127.0.0.1/$MTX_PATH -f s16le -ac 1 -ar 48000 -`,
  et un petit relais (le script de la passerelle lui-même, dans un mode dédié) poste des morceaux de
  10-20 ms numérotés sur `http://<console>:8082/talk.pcm?seq=` ;
- **le point dur** : l'ordre peut partir à l'antenne (retour plateau). Aujourd'hui, n'importe quel client
  peut publier sur n'importe quel chemin de la passerelle. Il faudrait donc réserver la publication des
  chemins `ct-` à un compte que seul l'orchestrateur connaît (`authInternalUsers` de MediaMTX ; Go n'a
  pas d'anticipation négative, il faut énumérer les autres préfixes pour l'utilisateur `any`).
  Les encodeurs actuels publient sans identifiant : c'est leur configuration qui changerait.
- Gain attendu : ~35 kbit/s en montée au lieu de 768, FEC Opus, UDP. Latence estimée : Opus 20 ms +
  réseau + gigue RTP côté ffmpeg + tampon de la console (60 ms), du même ordre qu'aujourd'hui en local,
  et bien plus stable en 4G.

## Réalisé

- `services/webrtc_gateway/script.py` : chemin `cl-…` déclaré avec `runOnDemand`, l'horodatage absolu
  coupé sur ce chemin (sinon ~5 s de silence au démarrage, en attendant le premier SR), débit validé
  (`relay_bitrate`), détection de ffmpeg/libopus (`ffmpeg_available`). Le :8080 publie `listen_relay` et
  `listen_relay_bitrate`.
- `services/webrtc_gateway/console_listen.py` : disponibilité avec raison codée, relais de la
  signalisation WHEP (offre, PATCH, DELETE), sessions gardées côté serveur.
- `services/webrtc_gateway/__init__.py` : trois routes (voir le contrat ci-dessous). `_gateway_params`
  demande le relais (`listen_relay`, `listen_relay_bitrate`). Service en 0.3.1.
- `plugins/_webrtc_runtime` : ffmpeg ajouté à l'image, `bobi-webrtc:0.6.1` (`image_tag` changé avec la
  version, piège connu).
- `tests/verif_console_ecoute_webrtc.py` : 34 contrôles, mutés (10 mutations, toutes mordent).

**Mise en service** (actions d'exploitation, **non faites** cette nuit pour ne pas couper le moniteur
vidéo en cours) : Réglages → Images → construire `bobi-webrtc` 0.6.1, puis Réglages → WebRTC →
redéployer la passerelle. L'opération recrée le conteneur et coupe les moniteurs WebRTC quelques
secondes (+ ~5 s d'horodatage absolu). Avant : `GET /api/webrtc/console-listen/1160` répond
`gateway_no_relay` (vérifié sur l'instance vivante).

## Contrat pour l'interface de la console

Tout passe par l'**origine de l'orchestrateur** : pas de contenu mixte sous HTTPS, pas de CORS. Le média
(ICE/UDP, port 8189 de la passerelle) va directement du navigateur à la passerelle.

**1. Savoir si l'alternative existe** — `GET /api/webrtc/console-listen/<vmid>[?bus=<id>]`
(login + accès viewer au projet, comme `/listen.pcm`), sans effet de bord :

```json
{"available": true, "reason": null, "vmid": 1160, "bus": "b_lst",
 "buses": {"b_lst": "Écoute", "b_bhcf5": "Monitoring Cyril Mazouer"},
 "transport": "webrtc", "signalling": "whep",
 "whep_url": "/api/webrtc/console-listen/1160/whep?bus=b_lst",
 "codec": "opus", "bitrate_kbps": 96, "frame_ms": 10, "channels": 2, "sample_rate": 48000,
 "path": "cl-10-29-4-121-b_lst"}
```

Sans `bus`, c'est le premier bus d'écoute de la console (le même défaut que `/listen.pcm`).
`available: false` vient toujours avec un `reason` (code à traduire par l'interface, jamais affiché tel
quel) :

| reason | sens |
|---|---|
| `gateway_not_deployed` | pas de passerelle déployée/activée (Réglages → WebRTC) |
| `gateway_unreachable` / `gateway_not_running` | passerelle en panne |
| `gateway_no_relay` | passerelle sans ffmpeg (image < 0.6.1) ou script antérieur au service 0.3.1 |
| `console_no_ip` / `console_unreachable` | console sans adresse / injoignable |
| `no_listen_bus` | le bus demandé n'est pas un bus d'écoute de cette console (ou il n'y en a aucun) |
| `bad_bus` | identifiant de bus hors `[A-Za-z0-9_]{1,64}` |
| `no_listen_stream` (HTTP 400) | ce plugin ne sert pas de `/listen.pcm` L16 stéréo |
| `unknown_container` (HTTP 404) | vmid inconnu |

L'état de la passerelle est mis en cache 10 s côté orchestrateur. Interroger à l'ouverture du sélecteur
suffit : pas de sondage périodique.

**2. Écouter (WHEP, brouillon IETF draft-ietf-wish-whep, tel que MediaMTX le sert)** :

```js
const pc = new RTCPeerConnection();
pc.addTransceiver("audio", { direction: "recvonly" });
pc.ontrack = (e) => { audioEl.srcObject = e.streams[0]; audioEl.play(); };
await pc.setLocalDescription(await pc.createOffer());
await new Promise(r => pc.iceGatheringState === "complete" ? r()
  : pc.addEventListener("icegatheringstatechange", () => pc.iceGatheringState === "complete" && r()));
const r = await fetch(info.whep_url, { method: "POST", headers: { "Content-Type": "application/sdp" },
                                       body: pc.localDescription.sdp });
// 201 → réponse SDP ; r.headers.get("Location") = /api/webrtc/console-listen/session/<jeton>
await pc.setRemoteDescription({ type: "answer", sdp: await r.text() });
```

- Réponses d'erreur (JSON `{"error": code}`) : `409` + l'un des `reason` ci-dessus ; `400
  sdp_offer_expected` ; `502 relay_failed`, quand la passerelle n'a pas pu démarrer le relais en 10 s,
  par exemple parce que la console refuse un 5ᵉ écouteur sur ce bus ou que le bus a disparu ; `502
  gateway_unreachable`. Droits : identiques au GET `/listen.pcm` (403 sinon).
- **Fin** : `DELETE <Location>` (libère la session tout de suite ; sinon ICE expire, puis le relais
  s'arrête 10 s après le dernier auditeur). Seul l'utilisateur qui a ouvert la session peut la fermer
  (403 `not_your_session`) ; `404 unknown_session` si elle est déjà oubliée.
- **Trickle ICE** facultatif : `PATCH <Location>` avec `Content-Type: application/trickle-ice-sdpfrag`.
  L'exemple ci-dessus attend la fin de la collecte ICE et n'en a pas besoin.
- Premier son ~0,5 s après l'offre à froid ; immédiat si quelqu'un écoute déjà ce bus.
- **Aucun heartbeat** n'est requis : le cycle de vie est celui de la connexion WebRTC.

**3. Choisir L16 ou WebRTC.** Il n'existe pas de bascule automatique fiable. Le choix revient à
l'utilisateur et se garde par navigateur (`localStorage`, une commodité, par exemple
`ac.listen.transport = "l16" | "webrtc"`). Le défaut reste **L16**. On ne propose WebRTC que si
`available` est vrai. Une suggestion peut toutefois se fonder sur ce que le L16 mesure déjà : si les
`under` / `dropped` de l'AudioWorklet grimpent, proposer « Écoute compressée (Opus) ». Les métriques de
l'écoute WebRTC se lisent par `pc.getStats()` (`inbound-rtp` : `jitterBufferDelay /
jitterBufferEmittedCount`, `packetsLost`, `concealedSamples`) et alimentent les mêmes champs
latence / sous-alimentations que l'interface affiche pour le L16.

**4. Ce qui ne change pas** : `/talk.pcm` (l'ordre) et `/listen.pcm` restent tels quels. L'écoute WebRTC
reproduit le bus d'écoute **après** contrôles de moniteur, comme le L16 ; changer dim, cut ou niveau
s'entend dans les deux.

## Constaté en passant

- `services/webrtc_gateway.status()` interroge l'API MediaMTX (`:9997/v3/paths/list`) depuis
  l'orchestrateur, qui reçoit un **401** : depuis 1.x, l'API n'est permise par défaut que depuis
  localhost. `paths_detail` est donc toujours `null` dans Réglages → WebRTC, sans erreur visible.
  Non corrigé. Deux corrections possibles : un `authInternalUsers` avec un compte d'API, ou relayer la
  liste par le :8080 du script.
