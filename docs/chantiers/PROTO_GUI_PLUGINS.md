# Prototype : interfaces natives LV2 dans un navigateur (2026-10-17)

Banc JETABLE, rien d'intégré au produit. Décision de l'exploitant : « lançons des prototypes juste
pour tester ». Conteneur `proto-lv2gui` sur **dell-1** (nœud 35), cpuset 81,83,85,87, hors
orchestrateur. Sources : `/root/proto_gui/` sur dell-1.

## Montage
- Un Xvfb par plugin (`1280x900x24+32`), Mesa llvmpipe pour l'OpenGL.
- **Deux processus par plugin** : `dsphost` (lilv, traitement seul, temps réel 256 éch. @ 48 kHz,
  signal synthétique) et `uihost` (suil, interface X11 native SANS instance du plugin dans le
  processus, ni instance-access ni data-access). Ils échangent des **ports** sur un socket UNIX :
  contrôles en float, ports atome en `atom:eventTransfer`. Table d'URID partagée par fichier (les
  atomes traversent la frontière avec les mêmes identifiants).
- Transmission : x11vnc (un par écran) + noVNC/websockify. Page de banc sur `:6080`.

## Résultats mesurés
| Plugin | Affichage | GUI→DSP | DSP→GUI | geste→image (VNC, médiane / p90) | CPU interface au repos / en manipulation* |
|---|---|---|---|---|---|
| ZamComp (DPF, OpenGL) | oui | oui | oui | 59 / 65 ms | 6 % / 8 % |
| x42 fil4 (robtk, OpenGL) | oui | oui | oui | 13 / 22 ms | 0,2 % / 12 % |
| LSP Compressor stereo | oui | oui | oui | 16 / 25 ms | 19 % / 26 % |
| LSP Para EQ x8 stereo | oui | oui | oui | 19 / 23 ms | 7 % / 17 % |

\* % d'un cœur, processus `uihost` seul. S'y ajoutent par écran ouvert : Xvfb 1-6 %, x11vnc 2-12 %
(proportionnel à ce qui bouge : le graphe temporel du compresseur LSP pousse ~620 kB/s en ZRLE
même au repos), dsphost 1-4 %. Mémoire (PSS) : uihost 72-117 Mio, Xvfb 26-30, x11vnc 8-10,
websockify 22 (Python), dsphost 2-10.

DSP→image (paramètre changé côté traitement → bouton redessiné) : 57-66 ms médiane, dont ~30 ms
voulus (le DSP ne renvoie les contrôles qu'à ~30 Hz).

Poids des paquets (installés, `--no-install-recommends`) ajoutés à `bobi-compute:0.63` :
xvfb + x11vnc 237 Mo, novnc + websockify 111 Mo, Mesa llvmpipe 196 Mo, lilv/suil ~0 ; total
pile graphique 349 Mo. Les plugins Zam + x42 + LSP : 403 Mo.

## Pièges trouvés
- **x11vnc tourne à 100 % d'un cœur et n'accepte aucun client** sous Docker : le `RLIMIT_NOFILE`
  énorme de Docker lui fait parcourir tous les descripteurs. `ulimit -n 4096` avant de le lancer.
- `Xvfb -screen …x24` donne un tampon **24 bpp tassé** : captures `xwd` à 3/4 de largeur, franges
  colorées. `x24+32` force 32 bpp.
- LSP refuse de travailler avec un contrôleur suil NULL (`[ERR] ctl=(nil)`) : passer un pointeur
  non nul. LSP affiche une fenêtre de dons à la première ouverture d'une version ; elle disparaît
  une fois sa configuration écrite.
- x11vnc par défaut : ~25 ms de geste→image ; `-defer 5 -wait 5 -nonap` : ~14 ms.
- ZamComp reste à ~59 ms même réglé : l'entrée arrive en ~11 ms (écriture du port), le reste est
  dans le chemin de rafraîchissement DPF/OpenGL sous llvmpipe. Cause exacte non cherchée.

## Décisions de l'exploitant (2026-10-18)
- Plugins essayés dans la console : vus et réglés sans problème.
- **Mode réglage natif : oui, à la demande, dans un PLUGIN SÉPARÉ** (décidé dans un second temps : la
  pile graphique n'entre ni dans la console ni dans bobi-compute ; la console n'a que le bouton). Un bouton dans la fiche du plugin ouvre son interface
  native dans un panneau ; on la ferme après usage. Droit : celui d'exploiter les plugins
  (`plugins.operate`). **Une seule interface ouverte à la fois par console** ; en ouvrir une autre
  ferme la précédente, avec un avertissement.
- La tranche de retour d'un bus reste créée **coupée**.
- Les noms d'éditeurs des plugins (x42, LSP, ZamComp, BBC PPM…) restent affichés.
- **Nous ne livrons aucun plugin**, ni aucun fichier de réponse impulsionnelle : l'exploitant va les
  chercher et les installe par Réglages. Le produit ne fournit que l'hôte et les bibliothèques
  système dont les plugins ont besoin.
- Ordre des chantiers : interface native, puis fil de travail à part (`worker`, qui ouvre
  Dragonfly), puis préréglages d'usine, puis choix d'un fichier de réponse impulsionnelle dans les
  médias (LSP Impulse Reverb), puis CLAP / VST3.
- Prototype supprimé de dell-1 le 2026-10-18 : conteneur, image et dossier.

## Mode réglage natif : construit (2026-10-18)

Plugin SÉPARÉ `plugins/plugin_gui` (décision : ne pas alourdir la console), image `bobi-gui`, routes
d'orchestrateur `app/native_gui.py` + `app/routes/native_gui_api.py`, bouton dans la fiche plugin de la
console (0.4.91). Garde : `tests/verif_plugin_gui.py` (16 mutations, 15 mordent ; la 16e — retirer
`@require_login` d'une route — est couverte par la seconde barrière, `check_vmid_access` rend 401 sans session).

Ce qui a changé par rapport au prototype :
- **Pas de dsphost** : le plugin tourne dans la console (son hôte en bac à sable). L'interface lit et écrit
  ses PORTS DE CONTRÔLE par l'API de contrôle de la console (`/plugins/params?values=1` relu 25 fois par
  seconde, `/set` au chemin de la fiche). Conséquence : **pas d'atomes** (l'hôte de la console n'a pas de
  transport d'atomes) — les graphes LSP envoyés par messages restent vides ; boutons, réglages, mesures OK.
- **Pas de noVNC ni websockify** : Waitress n'a pas de WebSocket. x11vnc écoute sur une socket UNIX, le
  service relit le RFB brut et ré-encode les zones changées (PNG petites zones, JPEG grandes zones qui
  bougent, reprises en PNG à l'arrêt) dans un flux HTTP relayé par `plugin_streams.relay_get` ; gestes
  par `relay_post`. Aucun port d'affichage sur le réseau.
- **Interfaces externes** (`kx:Widget`) prises en charge : les mesureurs x42 n'ont QUE celle-là.
- Piège trouvé : **`PR_SET_PDEATHSIG` vise le FIL parent**, pas le processus. Lancés depuis le fil d'une
  requête HTTP, Xvfb et l'interface recevaient SIGKILL dès la réponse partie. Tout est lancé par un fil
  « spawner » à vie longue.
- Piège trouvé : **arènes malloc par fil** : +18 Mo de PSS par interface ouverte puis fermée, jamais rendus
  (8 sessions : 22 → 165 Mo). `mallopt(M_ARENA_MAX, 2)` au démarrage : plafond à 31 Mo.
- Piège trouvé : Xvfb lance xkbcomp en repassant par `setuid(getuid())` : refusé par seccomp, le clavier ne
  se compile pas et le serveur meurt. Les set*id sont permis à la pile d'affichage (uid sans capacité,
  no_new_privs), refusés à l'interface. Le connect abstrait de Xlib est évité par `-nolisten local`.

Mesures (dell-1, console de banc acon-banc-c sur dl360-1, 2026-10-18) :

| | ZamComp | x42 BBC (externe) | LSP Compressor | x42 fil4 |
|---|---|---|---|---|
| ouverture (`/open` → prête) | 457 ms | 460 ms | 550 ms | 606 ms |
| geste → valeur dans la console, direct / via relais orchestrateur | 18 / 23 ms | — | — | — |
| geste → image, direct / via relais | 59 / 72 ms | — | 79 ms | — |
| valeur dans la console → image (relais) | 56 ms | 25 ms (p90 35) | — | — |
| au repos, panneau ouvert : images/s, débit | 13-18 /s, 11-13 kB/s | 3 /s, 3 kB/s | 27-29 /s, 23-25 kB/s | 0 |
| CPU (% d'un cœur, service + Xvfb + interface + x11vnc) | 10 % | 5 % | 15 % | 4 % |
| mémoire de la session (anon + shm) | ~115 Mo | ~100 Mo | ~130 Mo | ~110 Mo |

Service sans session : 0 % CPU, 17 Mo, aucun autre processus. Fermeture : 0,3 s. Battement perdu : arrêt à
15,0 s mesuré. Console : 11,4 → 13,9 % d'un cœur avec une interface LSP ouverte (relecture complète, avant
`values=1`). Le saut réseau (orchestrateur) ajoute ~5 ms au geste → valeur et ~13 ms au geste → image.
Image : bobi-gui 540 Mo dont **234 Mo** au-dessus de bobi-compute 0.63 (le prototype : 349 Mo avec noVNC).

Restent ouverts : atomes (graphes LSP) ; fenêtre de dons LSP à chaque ouverture (la configuration n'est pas
écrite par LSP dans le bac à sable, cause non trouvée) ; les réglages de l'interface native ne passent pas
par le journal d'exploitation de l'orchestrateur (conteneur → console direct) ; essai visuel sur iPad.

## Suite décidée par l'exploitant (2026-10-18, après la livraison)
- Messages LV2 « atom » entre le plugin et son interface native : lancé (EBU R128 figé, courbes LSP vides).
- Finitions de l'interface native, à lancer APRÈS les atomes (même code de plugin_gui) : capacité « gui » déclarable
  depuis la page Nœuds, survie à une bascule HA (liaison session ↔ conteneur en base), fenêtre de dons LSP, réglages
  faits dans l'interface native au journal d'exploitation.
- Chantier suivant : CLAP / VST3 (licence du SDK VST3 à vérifier), après les atomes.
- Messages génériques « Modification appliquée » : laissés tels quels.
- Cœurs dédiés aux plugins : à éprouver sur la console de banc 1167 (en cours).

## Atomes : construits (2026-10-18, console 0.4.104, plugin_gui 0.1.1, bobi-gui 0.1.1)

Constat de départ : l'EBU R128 de x42 figé à +23 LU, les courbes LSP vides — ils parlent à leur interface par
messages LV2 (atomes), l'hôte de la console n'échangeait que les ports de contrôle.

**Fil audio.** Deux anneaux sans verrou dans le segment partagé (protocole 2, `tools/plugin_shm.h`) : sortie
512 Kio (hôte → moteur), entrée 64 Kio (moteur → hôte), compteurs d'octets libres, enregistrement = longueur,
emplacement du port, atome. L'hôte copie les atomes de sortie et dépose ceux d'entrée APRÈS avoir rendu le bloc
(`done` + réveil), jamais entre `req` et `done` : le moteur ne les attend pas. Pas d'allocation, pas de verrou ;
file pleine, atome trop gros (128 Kio) ou plus de 64 Kio dans un bloc : jeté et compté. **Aucune copie sans
interface** : la console écrit `sub_until` (horloge monotone) pour 2 s à chaque échange ; au-delà l'hôte ne
copie plus rien (choisi plutôt qu'un anneau que personne ne lit : zéro octet déplacé, un test de 2 mots par bloc).
Les tampons atome suivent `rsz:minimumSize` (LSP : 200 Kio en sortie), 1 Mio au plus.

**URID.** L'atome garde sa forme binaire LV2, octet pour octet ; chaque champ URID (type, classe et id d'objet,
clé et contexte de propriété, valeur URID, unité de séquence, type d'élément de vecteur, datatype / langue d'un
littéral) est réécrit par des tables de SESSION, une par sens, définies par celui qui envoie et transmises avec le
premier message qui les emploie. L'hôte publie sa table (id → URI) dans le segment ; la console traduit
URID hôte → URI → id de session ; bobi_ui_host id de session → URI → son URID, et dans l'autre sens ses propres
URID servent d'ids. Pourquoi : un spectre de 2 000 points coûte ses 8 000 octets (+ base64), pas un texte par
flottant ; une URI LSP (60-90 caractères) passe une fois par session ; et le parcours qui réécrit EST l'analyse
défensive (tailles contre le conteneur, profondeur 8, id inconnu : message refusé entier), le même en Python
(`src/atom_wire.py`) et en C (`image/atom_wire.h`), comparés à l'octet par le test. Une URI que le plugin n'a
jamais mappée n'est pas inventée : il ne pourrait pas la reconnaître ; message jeté (`in_unknown_uri`).

**Transport.** Le chemin des valeurs de ports : conteneur plugin_gui → console :8082 en direct, `POST
/plugins/atoms` à la cadence de relecture (plus tôt quand l'interface a écrit) — un échange porte les messages
de l'interface et rapporte ceux du plugin. La session de l'utilisateur et `plugins.operate` sont vérifiées par
l'orchestrateur à l'ouverture (seule porte, inchangée) ; le port de contrôle de la console n'est pas plus ouvert
qu'avant (`/set` y était déjà, sans authentification, réseau du cluster). Plafond vers l'interface 1 Mio/s par
session : au-delà, seul le plus récent de chaque sorte (classe + id d'objet, propriété d'un patch:Set) part, les
sortes servies il y a le plus longtemps d'abord — un message envoyé au seul changement (courbe d'un filtre LSP)
n'est pas affamé par ceux envoyés à chaque bloc. Vers le plugin : 256 Kio/s côté interface, 96 Kio de base64 par
échange, 16 Kio par atome.

Mesures (banc : console 1167 sur dl360-1, plugin_gui 1177 sur dell-1, 2026-10-18) :

| | EBU R128 (x42) | LSP Para EQ x8 stéréo |
|---|---|---|
| émis par le plugin | ~200 messages/s, 52 Ko/s | ~1 390 messages/s, **3,7 Mo/s** (6 spectres de 5,2 Ko, mesures et time:Position à chaque bloc) |
| envoyé à l'interface | tout : 52 Ko/s (70 Ko/s en base64) | 0,41 Mo/s (0,51 en base64), le reste remplacé par plus récent |
| interface → plugin | 2 messages à l'ouverture, 1 par appui sur Reset | 1 message à l'ouverture |
| aller-retour d'un échange | 2 ms | 2-4 ms |
| CPU du conteneur console (% d'un cœur) | 10,6 → 12,3 | 10,6 → 13,0 |
| blocs en retard / secs | 0 / 0 | 0 / 0 |

Preuves au banc : le R128 affiche -4,1 LU, puis -14,1 LU quand le trim de la tranche passe de -7,1 à -17,1 dB ;
Reset remet le maximum à la valeur courante. LSP : courbes des filtres (cloche +12 dB à 250 Hz, -12 dB à 4 kHz),
courbe totale et spectre (raie du générateur à 1 kHz) affichés. Débordement : abonné sans vider pendant 3 × 1,8 s,
+7 016 messages jetés côté hôte, aucun bloc perdu (`dry_late` = `dry_busy` = 0). Garde : `tests/verif_native_gui_atoms.py`
(15 mutations, toutes mordent, dont une de l'hôte Rust : copie sans abonné).

Journal d'exploitation : les échanges d'atomes ne sont PAS journalisés (30 par seconde ; `/plugins/atoms` à false
dans `control.journal.endpoints`). La console compte les patch:Set venus de l'interface (`stats.patch_set`,
`last_patch_set` : propriété, port, instant) : c'est le point d'entrée pour les y faire entrer — un événement
`/events` de la console (code « réglage de l'interface native », sujet = l'élément, propriété en libellé),
regroupé en fin de geste comme le journal le fait déjà pour les faders, que l'orchestrateur relit avec les autres.

Restent ouverts : ce journal des patch:Set ; les réglages par PORTS de l'interface native, toujours hors journal ;
la fenêtre de dons LSP ; l'iPad ; le réglage `gui_image` de l'orchestrateur à passer à bobi-gui:0.1.1.

## Finitions : construites (2026-10-18, console 0.4.109, plugin_gui 0.1.2, image bobi-gui 0.1.1 inchangée)

**Capacité « gui » déclarable** (Réglages → Nœuds → ＋ Ajouter). Rien à installer sur l'hôte : `install-node.sh
--add-caps gui` ne fait que fusionner la capacité dans `config.json` ; elle exige `compute` (refus du contrôleur et du
script, message traduit). Elle fait du nœud la cible du build node_only `bobi-gui` (page Images) et la seule cible de
création d'un `plugin_gui` (refus 400 traduit à la création, alerte au déploiement, nœud grisé dans la palette).
Trois défauts trouvés en chemin : `atom_wire.h` manquait au staging de l'image (la garde COPY ne lisait que la forme
à une source : corrigée) ; un build node_only réussi de bobi-gui écrivait la colonne inexistante `gui_image`
(avalée par `db_update_node`) au lieu du réglage ; et la resynchronisation d'un rattrapage lisait l'ANCIEN agent
(redémarré 2 s plus tard) quand la capacité ne provisionne rien — vécu sur dell-1, base sans « gui » alors que le nœud
l'annonçait. dell-1 a maintenant « gui » dans sa liste (rattrapage réel, puis resynchronisation).
Garde : `tests/verif_capacite_gui.py` (14 mutations, toutes mordent).

**Survie à une bascule de contrôleur.** Choix : la table `native_gui_sessions` en base (le cache mémoire reste devant)
ET la reconstruction depuis le conteneur. La base seule ne suffit pas : la réplique HA est un instantané toutes les
N minutes, une session ouverte depuis n'existe pas sur le standby promu. Le conteneur seul ne suffit pas non plus :
le journal doit nommer l'auteur à partir d'un enregistrement du contrôleur, pas de la parole du conteneur. Un sid
inconnu est donc demandé aux conteneurs (`POST /lookup`), pour la console de la route et pour l'utilisateur qui l'a
ouvert seulement. Limite : le conteneur coupe l'interface 15 s après le dernier battement ; une bascule plus longue
(manuelle) la ferme quand même — la liaison survit, pas la session. Le panneau retente l'image pendant 20 s.
`/status` ne liste plus de sid (secret) mais le tag de session. Garde : `tests/verif_native_gui_ha.py` (12 + 3 mutations ;
une ne mord pas, équivalente : écraser au lieu de compléter un fichier qu'on n'ouvre que s'il est absent).

**Fenêtre de dons LSP.** Cause mesurée par strace depuis l'hôte : à la fermeture de la fenêtre, LSP 1.2.21 veut
sauver sa configuration et commence par OUVRIR le fichier existant ; absent, il abandonne sans rien écrire. Le service
pré-crée `~/.config/lsp-plugins/lsp-plugins.cfg` vide dans le home des interfaces (volume d'état) ; LSP y écrit sa
configuration (3,8 ko, `lsp_plugins_version = "1.2.21"`). Banc dell-1 : 1re ouverture fenêtre, fermée ; 2e : plus de
fenêtre.

**Journal d'exploitation.** Tag de session non secret donné à l'ouverture, passé à chaque `/set` et `/plugins/atoms`
(avec messages) ; la console groupe en UN événement `native_set` par geste (clôture 1,5 s après la dernière valeur,
coupure à 5 min, dernière valeur gardée) ; `app/ops_journal.py` l'attribue à l'utilisateur de la session, catégorie
réglage, nom lisible de l'élément (« Présentateur · Comp LSP · Threshold → -19.9 (interface native) »). Banc (1167 +
1177) : Ratio de LSP Compressor tiré 2 s = 40 valeurs = 1 événement. Attribution par l'orchestrateur : prouvée hors
ligne seulement (orchestrateur non redémarré). Garde : `tests/verif_native_gui_journal.py` (14 mutations, toutes mordent).

Restent ouverts : patch:Set au banc (aucun plugin installé n'en émet sur un geste simple ; prouvé hors ligne) ; l'iPad.

## Taille juste et panneau mobile : construits (2026-10-18, plugin_gui 0.1.3, image bobi-gui 0.1.2, console 0.4.131)

Demande de l'exploitant : le panneau était un cadre fixe (1120 x 640 en bas à droite) qui ne collait jamais à
l'interface ; il doit prendre la taille du plugin, suivre ses changements, et se déplacer / se redimensionner.

Constat d'abord : l'hôte lisait DÉJÀ la bonne taille initiale (mesuré au banc avec l'ancienne image : ZamCompX2 800 x
107, LSP Para EQ x8 881 x 796, fil4 800 x 418, EBU R128 367 x 542…, identiques à la nouvelle). Le défaut était surtout
dans le PANNEAU, de taille fixe, et dans l'absence de suivi quand l'interface se redimensionne elle-même (seuls ui:resize
et les fenêtres externes étaient suivis).

**D'où vient la taille.** bobi_ui_host publie `geom W H source fixed` avant `ready` et à chaque changement : `resize` quand
l'interface a demandé exactement cette taille (ui:resize), `x11` sinon (coin le plus lointain de ses fenêtres VISIBLES sous
la nôtre, XGetWindowAttributes ; une fenêtre d'aide < 8 px, non affichée ou InputOnly ne compte pas), `external` pour une
interface kx:Widget (sa fenêtre de premier niveau), `initial` si rien n'est mesurable après 300 ms ; `fixed` = ui:fixedSize ou
ui:noUserResize déclarés. Suivi : SubstructureNotify sur notre fenêtre + relecture toutes les 0,5 s (externe : 0,25 s) ; notre
fenêtre prend la taille mesurée, donc la zone capturée aussi. L'écran virtuel reste 1920 x 1200 : la zone capturée est
bornée à l'écran (`clipped`), la vraie taille est publiée à côté. `size W H` reste écrit pour un service plus ancien ; un
service 0.1.3 sur une image 0.1.1 lit encore `ready` / `size`. Publiée dans `/status` (size_info), la réponse de `/open` et
les trames HELLO / SIZE du flux (charge JSON) — l'orchestrateur n'a rien à changer, il relaie le flux tel quel.

**Panneau.** À la taille de l'interface + son cadre, dans la fenêtre ; ouvert en bas à droite (coin qui reste fixe tant
que l'exploitant ne l'a pas déplacé, coin haut-gauche ensuite) ; image 1:1 quand elle tient, réduite proportionnellement
sinon (« Ajuster », jamais agrandie), « 1:1 » avec défilement, − / + et pincement en zoom manuel. Clics et touchers :
convertis depuis la boîte DESSINÉE du canevas (bord exclu), pas depuis la valeur de zoom — l'ancien calcul comptait le
bord de 1 px dans le dessin. Déplacé par la barre de titre (souris tout de suite ; doigt après 350 ms d'appui immobile,
un glissement avant ne déplace rien ; boutons de 44 px ; pointer events, pas de glisser HTML5), redimensionné par une
poignée de 44 px (double appui : retour à la taille de l'interface). Mémoire du navigateur (localStorage
`bobi.ngui.panel`) : la POSITION, et la TAILLE choisie par plugin (40 au plus) — une taille unique aurait contredit
« prendre la taille du plugin » ; relue sans confiance ; ramené dans l'écran quand la fenêtre rétrécit (sans oublier la
position voulue). Pied du panneau : taille, échelle, source (infobulle). Console 0.4.130 : passe l'URI du plugin au
panneau (une console plus ancienne marche encore, l'URI vient alors du flux).

Mesures (banc : conteneur jetable `bench-gui013` sur dell-1, image bobi-gui 0.1.2, réseau partagé avec 1177, ports
9082 / 9080 ; console de banc 1167, 7 plugins ajoutés en bypass sur s_in1). « Réelle » = arbre X11 relevé par un AUTRE
client dans le conteneur (ctypes + libX11), pas par bobi_ui_host :

| Plugin | publiée | source | fenêtre réelle (plugin / la nôtre) | ouverture |
|---|---|---|---|---|
| ZamCompX2 (DPF) | 800 x 107 | x11 | 800 x 107 / 800 x 107 | 506 ms |
| LSP Para EQ x8 stéréo | 881 x 796 | x11 | 881 x 796 / 881 x 796 | 612 ms |
| x42 fil4 stéréo (robtk) | 800 x 418 | resize | 800 x 418 / 800 x 418 | 614 ms |
| x42 EBU R128 (externe) | 367 x 542 | external | 367 x 542 (premier niveau) | 477 ms |
| ZamGEQ31 | 590 x 170 | x11 | 590 x 170 / 590 x 170 | 457 ms |
| LSP Compressor stéréo | 1082 x 525 | x11 | 1082 x 525 / 1082 x 525 | 574 ms |
| x42 VU stéréo (externe) | 600 x 170 | external | 600 x 170 (premier niveau) | 508 ms |

Suivi : la fenêtre du plugin redimensionnée de l'extérieur (fil4 → 1000 x 500, LSP Compressor → 900 x 450, ZamCompX2 →
1000 x 200) : nouvelle taille publiée en 55, 3 et 2 ms, notre fenêtre suit. LSP Compressor a ensuite redemandé sa taille par
ui:resize sans redimensionner sa fenêtre : on a publié `resize` 1082 x 525 puis de nouveau 900 x 450 mesurés (la mesure fait
foi). Première ouverture LSP sur un home neuf : la fenêtre de dons apparaît comme une SECONDE fenêtre de premier niveau
(715 x 455), hors de la zone de l'interface ; disparue à la seconde ouverture (comportement connu). Sonde du bac à sable
avec la nouvelle image : couches 1278, rien de manquant, seules « read /usr/lib », « write home », « socket unix »
réussissent (inchangé). Compilation `-Wall -Wextra` : 0 avertissement.

Garde : `tests/verif_native_gui_taille.py` — hôte (partie taille de uihost.c compilée avec -DPROBE_ONLY), service, panneau
dans node avec un DOM simulé (échelle, clic au milieu et au dernier pixel d'un dessin réduit, déplacement souris / doigt,
poignée, mémoire, fenêtre qui rétrécit, trame SIZE), console (l'URI part avec l'ouverture). 30 mutations : 29 sur
plugin_gui, toutes mordent (une 15e survivait — le bord du canevas compté dans le dessin — avant d'ajouter le clic au
dernier pixel) ; 1 sur la console (ancienne 0.4.129 : 5a et 5b échouent).

Restent ouverts : essai à la main dans un vrai navigateur et sur iPad (aucun navigateur ici, panneau vérifié dans node
seulement) ; la poignée recouvre ~20 px du coin bas-droit de l'image ; une interface qui demande ui:resize en attendant que
L'HÔTE redimensionne sa fenêtre est ramenée à la taille mesurée (aucun cas vu sur les 7) ; interfaces plus grandes que
1920 x 1200 rognées (écran virtuel fixe) ; redimensionner le panneau ne redimensionne pas l'interface (pas demandé) ;
réglage `gui_image` à passer à bobi-gui:0.1.2 et 1177 à redéployer en 0.1.3 (intégration).

## VST3 et CLAP : construits (2026-10-18, plugin_gui 0.1.4, image bobi-gui 0.1.3, console inchangée)

Demande : « Interface native » pour les VST3 et les CLAP, même panneau que LV2. Ni l'orchestrateur
(`app/native_gui.py`) ni la console n'avaient à changer : ils ne connaissent que des URI, et celles que liste
plugin_gui sont, octet pour octet, celles de l'hôte de la console (`vst3:<paquet>#<classe>`, `clap:<fichier>#<id>`).

**Pourquoi une seconde instance.** Une interface LV2 n'a besoin d'aucun plugin dans son processus : elle parle des
ports. L'éditeur d'un VST3 fait partie du contrôleur du plugin, la GUI d'un CLAP fait partie du plugin : sans instance,
pas d'interface. bobi_ui_host crée donc la SIENNE (`image/vst3_gui.h`, `image/clap_gui.h`, API écrites à la main comme
dans `host/src/vst3.rs` / `clap.rs`, aucun en-tête de SDK), le DSP reste dans la console, et les deux sont tenues au pas
par le chemin des ports LV2 (`/plugins/params?values=1`, `/set`), symboles `p<id>`, valeurs dans le domaine de la
console (VST3 : plain, ou index de pas quand les valeurs plain d'un paramètre à pas ne sont pas les entiers 0..n —
la même règle que `vst3.rs`, recalculée avec le même binaire).

**Préambule.** Avant de créer le plugin, le service lit la vue de la console (bornes, drapeaux, valeur de chaque
paramètre) et son ÉTAT de l'instance (`POST /plugins/save_state`) et les écrit sur stdin (`d …`, puis `s <base64>`).
L'instance part de cet état (VST3 : composant -> composant et contrôleur, contrôleur -> contrôleur ; CLAP : blob
`clap.state`), jamais de paramètres posés un à un — Surge XT réinitialise les réglages de son effet dès qu'on lui POSE
un type, même le même (règle déjà apprise dans `vst3.rs`). Un paramètre dont nos bornes diffèrent de celles de la
console n'est relayé dans aucun sens (compté, journalisé).

**Règles du relais** (`image/native_fmt.h`, toutes gardées) : jamais reposer une valeur que les deux côtés tiennent
(dernière valeur échangée, puis ce que notre instance tient) ; l'écho de JUCE — qui ré-annonce, depuis sa minuterie,
donc EN RETARD, ce que l'hôte a posé — ignoré pour les 4 dernières valeurs posées ; dans une rafale de la console, les
paramètres à pas d'abord ; ce que le plugin change de lui-même relayé (performEdit, `restartComponent(kParamValuesChanged)`,
`rescan(CLAP_PARAM_RESCAN_VALUES)`). Jamais de relecture de notre initiative : essayée au banc (relire 150 et 600 ms
après une valeur de la console), elle a posté les valeurs de l'ANCIEN effet de Surge par-dessus le nouveau — retirée.

**Instance active, nourrie de silence.** Deux constats de banc : (1) Dragonfly en CLAP (DPF) ne rend les gestes de sa
GUI qu'à `process()` (`DistrhoPluginCLAP.cpp` : la file des événements de l'interface n'est vidée que là, jamais par
`params.flush`) — instance inactive, un bouton tourné n'arrivait jamais à la console ; (2) Surge XT Effects en VST3
applique un changement de type dans `process()` — posé sur le seul contrôleur, son éditeur restait sur l'ancien effet
5 s après. D'où : instance activée (bus stéréo comme la console) et nourrie de blocs silencieux de 32 échantillons,
50 fois par seconde (~3 % du temps réel) ; une valeur de la console va au contrôleur ET au processeur (IParameterChanges
du bloc suivant, CLAP : événement du bloc), comme le fait un hôte VST3. Un CLAP qui refuse `activate()` passe par
`params.flush()`. Fil : thread-check dit « audio » pendant `process()` et seulement là.

**Boucle d'événements.** JUCE et DPF font tourner toute leur boucle par l'hôte : `Linux::IRunLoop` (demandé à
l'IPlugFrame) et, en CLAP, `timer-support` / `posix-fd-support`. Taille : `getSize` / `get_size` puis la mesure X11
comme pour LV2 ; `resizeView` / `request_resize` -> notre fenêtre suit, `geom W H resize`, `onSize` rappelé ;
`canResize` / `can_resize` -> `fixed`. stdout du plugin détourné sur stderr (le protocole a son descripteur).

**Catalogue.** Chaque fichier `.clap` / paquet `.vst3` listé par `bobi_ui_host list-file` dans le bac à sable, un
processus par fichier (un plugin qui plante n'emporte que sa ligne), gardé tant que sa signature (mtime, taille, binaires
du paquet) ne change pas.

Mesures (banc : conteneur jetable `bench-gui014` sur dell-1, réseau de 1177, image bobi-gui 0.1.3 ; console de banc
1167 ; plugins installés pour le banc par `app/lv2_plugins` puis retirés) :

| | Surge XT Effects VST3 | Surge XT Effects CLAP | Dragonfly Room VST3 | Dragonfly Room CLAP |
|---|---|---|---|---|
| taille publiée = fenêtre X11 réelle (autre client) | 600 x 490 resize | 600 x 490 resize | 920 x 345 resize, fixe | 920 x 345 resize, fixe |
| ouverture (`/open` -> prête) | 0,3-0,6 s | 0,4-0,6 s | 0,5 s | 0,5 s |
| geste -> valeur dans la console (médiane / max, n = 8) | 11 / 17 ms | 21 / 23 ms | 37 / 45 ms | 39 / 45 ms |
| valeur dans la console -> image (médiane / max) | 61 / 73 ms | 101 / 111 ms | 99 / 110 ms | 97 / 107 ms |
| CPU interface (% d'un cœur) + Xvfb + x11vnc + service | 1,0 + 0,6 + 1,5 + 1,8 | 4,2 + 0,5 + 1,6 + 1,7 | 5,7 + 0,4 + 1,7 + 1,7 | 5,7 + 0,6 + 1,7 + 1,6 |
| mémoire interface (anon + shm) | 21 Mo | — | — | 42 Mo |

LV2 au même banc, inchangé : ZamCompX2 800 x 107 (x11), console -> image 61 ms. Surge VST3 zoomé à 75 % depuis son menu :
450 x 367 publié « resize », trame SIZE reçue par le panneau. Changement de type de Surge (Delay -> Chorus) : fait dans
l'interface -> la fiche reprend le type et les 8-10 réglages réinitialisés ; fait dans la FICHE -> l'interface passe à
Chorus et la fiche reprend les réglages réinitialisés (VST3 et CLAP). Journal : la console publie un `native_set` par
geste avec le tag de session, comme pour LV2 (attribution par l'orchestrateur : chemin inchangé). Bac à sable :
`/proc/<pid>/status` de l'hôte VST3 = uid 64991, CapEff 0, NoNewPrivs 1, Seccomp 2, 256 fichiers, 4 Gio ; sonde
`probe` (lancée comme le service lance l'interface) : couches 1278, rien de manquant, seules « read /usr/lib »,
« write home », « socket unix » réussissent — identique.

Garde : `tests/verif_native_gui_formats.py` — l'hôte compilé avec `-DPROBE_ONLY` contre de FAUX plugins écrits en C (un
VST3 à composant et contrôleur séparés, connectés par IMessage, éditeur sur IRunLoop / IPlugFrame, processeur « Surge » ;
un CLAP avec clap.gui, timer-support, params, state, audio-ports, et sa variante qui refuse `activate()`), conduits par
le protocole réel ; et le service (préambule, catalogue, ordre d'ouverture, LV2 inchangé). 31 mutations, toutes mordent
(deux survivaient d'abord — l'écho, couvert en fait par « jamais reposer », et les événements de sortie CLAP, rattrapés
par le rescan du faux plugin — avant que le faux JUCE n'envoie ses échos EN RETARD et que les gestes soient datés).

Restent ouverts : la fiche d'un VST3 JUCE peut montrer des valeurs périmées tant que personne n'y touche (console seule :
le contrôleur JUCE de la console ne se synchronise que sur un fil de messages qu'elle n'a pas ; l'interface ouverte ne
corrige que ce qui CHANGE, jamais de relecture d'office — elle reposerait un type, donc réinitialiserait l'effet) ; un
menu plus grand que l'interface est coupé au bord de la zone capturée ; ce qu'un plugin garde hors de ses paramètres
n'est pas transmis ; intégration : fusionner la branche plugin_gui, redémarrer l'orchestrateur (liste de staging de
`app/routes/images.py`), passer `gui_image` à bobi-gui:0.1.3 (construite sur dell-1), redéployer 1177.

## LSP en VST3 : « sans interface » à tort (2026-10-18, plugin_gui 0.1.6, image bobi-gui 0.1.4)

Constat : les 128 VST3 LSP du catalogue (LSP 1.2.35, paquet `lsp-plugins.vst3`) marqués `no_ui`, les autres VST3 et
les LV2 de LSP avec leur interface. Trois causes, chacune mesurée sur dell-1 :

1. **Catalogue sans affichage.** `list-file` tournait avec `DISPLAY=""`. LSP construit son éditeur dans `createView()`
   et y ouvre son affichage (« [ERR] Can not open display ») : pas d'affichage, pas de vue. Avec un Xvfb : 128/128
   utilisables, GOTT Compressor Stereo ouvert à 959 x 795. Désormais : `list-file` ouvre `$DISPLAY` dans le bac à sable,
   une vue absente SANS affichage vaut `no_display` (jamais `no_ui`, jamais gardée, relue au regard suivant), et le
   service liste chaque paquet VST3 sur l'affichage du catalogue (`:19`, Xvfb confiné comme celui d'une session,
   démarré pour le balayage, arrêté après). CLAP inchangé (`is_api_supported` ne demande pas d'affichage).
2. **Borne de 256 classes.** Le paquet LSP en a 396 (198 effets + leurs contrôleurs, `moduleinfo.json`) : 70 effets
   n'avaient aucune ligne. Borne 4096, comme `vst3.rs`. La console en listait déjà 198.
3. **SIGFPE à l'ouverture avec l'état de la console.** Sans état, l'éditeur s'ouvrait ; avec l'état (préambule `s`),
   l'hôte mourait de SIGFPE avant `ready`. LSP applique l'état aussitôt et divise par la fréquence d'échantillonnage,
   encore nulle : l'hôte d'interface donnait l'état AVANT `setupProcessing`, la console après (`make` puis
   `restore_state` puis `activate`). Ordre de la console repris (`v3_setup` avant `v3_restore`, `setActive` après
   l'attachement comme avant). Surge XT Effects : état restauré, ouverture 0,5-0,6 s, inchangé.

Coût : lister le paquet LSP construit ~200 éditeurs (≈ 0,4 s chacun sous llvmpipe) : 75 s pour tout le catalogue au
banc. Le délai par fichier passe de 60 à 300 s et `/catalog` ne bloque plus : il rend ce qui est connu (LV2, puis le
cache par signature, puis chaque fichier listé) ; `catalog_scanning` dans `/status`.

Mesures (conteneur jetable `bench-gui016` sur dell-1, réseau de 1177, image bobi-gui 0.1.4 ; console de banc 1167
0.4.141, GOTT Compressor Stereo VST3 inséré sur s_in5 puis retiré) : catalogue 198/198 LSP VST3 utilisables, les 7
autres VST3 inchangés ; ouverture `/open` 0,6-0,7 s (n = 3), 959 x 795 « resize », « state restored » ; geste ->
valeur dans la console 28 ms médiane, 64 max (n = 16) ; valeur posée dans la console -> image 99 ms médiane, 105 max
(n = 10). Sonde du bac à sable identique (couches 1278, seules « read /usr/lib », « write home », « socket unix »).
Banc : des Dragonfly lancés l'un après l'autre sur le MÊME Xvfb meurent parfois (SIGSEGV, aussi avec l'image 0.1.3) ;
le service donne un Xvfb neuf à chaque session, où les quatre s'ouvrent à chaque fois.

Garde : `tests/verif_native_gui_formats.py` 1o, 1p1-1p3, 2n1-2n6 (13 mutations, toutes mordent). Non couvert hors
ligne : l'ouverture de `$DISPLAY` par `list-file` (X réel) — vérifiée au banc seulement.
Intégration : fusionner la branche plugin_gui `fix/lsp-vst3-catalogue-display`, passer `gui_image` à bobi-gui:0.1.4
(construite sur dell-1), redéployer 1177.
