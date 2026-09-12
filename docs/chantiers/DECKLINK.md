***REMOVED*** Chantier `decklink_io` — entrées/sorties Blackmagic

Ouvert le 2026-09-07.

> **Périmètre corrigé le 2026-09-07 : on vise TOUT ce que le SDK sert, pas un modèle de carte.**
> Le manuel est explicit (§1.1.1) : « DeckLink » y est un **terme générique**, et l'API couvre les
> lignes **DeckLink, DeckLink IP, Intensity et UltraStudio**, plus le Media Player 10G et
> l'ATEM Mini Extreme ISO G2. Donc pas seulement des cartes PCIe, et pas seulement du SDI —
> certains de ces appareils n'ont que du HDMI, d'autres sont en Thunderbolt ou USB. Le chantier
> s'appelle `decklink_io` pour cette raison, et non `sdi_io` : le connecteur n'est pas l'identité
> du plugin.

**Origine** : deux besoins qui convergent. (1) Il reste, sur les sites, des sources et des
destinations SDI ; aujourd'hui elles n'entrent chez nous qu'à travers un convertisseur, avec
le coût, la latence et le point de panne que ça implique. (2) L'appel d'offres France
Télévisions, dont le périmètre exige de l'E/S SDI native.

> ⚠ **Ce document est une SPÉCIFICATION.** Aucun banc n'a été passé, aucune carte n'a été
> montée. Rien ici ne doit être cité comme une mesure. Les valeurs de latence, de coût CPU
> et de débit PCIe sont des inconnues à établir — les cases sont laissées vides exprès.

---

***REMOVED******REMOVED*** 1. Ce que le plugin fait, et ce qu'il ne fait pas

**Fait**, sur une carte DeckLink montée dans un nœud :

| | |
|---|---|
| Entrée | chaque sous-périphérique capable de capture → un flux vidéo MXL, plus l'audio embarqué et l'ANC |
| Sortie | chaque sous-périphérique capable de lecture → consomme des flux MXL et les émet sur le fil |
| Les deux sens | **MXL → fil autant que fil → MXL** : le plugin est bi-rôle, comme `2110_io` |
| Détection | format d'entrée détecté à l'exécution (`IDeckLinkInput::VideoInputFormatChanged`) |
| Genlock | l'appareil se cale sur la référence maison ; le plugin **constate** le verrou, il ne l'asservit pas |
| Absence de signal | état publié, pas de trame fabriquée en silence |

**« Entrée » et « sortie » ne sont pas des propriétés du connecteur, mais ce que le
sous-périphérique DÉCLARE.** Un appareil peut être capture seule, lecture seule
(`bmdDuplexSimplex`), ou bi-directionnel : c'est `BMDDeckLinkVideoIOSupport` qui tranche, jamais
une supposition tirée du modèle.

**Ne fait pas** : pas de conversion de format (l'UDC existe déjà et se câble derrière), pas de
routage interne appareil-à-appareil, pas de 8K en première version.

Le **HDMI n'est pas exclu par principe** — ce serait exclure des appareils entiers. Le type de
connecteur se lit dans `BMDDeckLinkVideoInputConnections` / `...OutputConnections` (un champ de
bits : SDI, HDMI, SDI optique, composantes, composite, S-Video) et se traite comme une donnée,
pas comme une hypothèse. La sonde le relève déjà.

---

***REMOVED******REMOVED*** 2. La décision structurante : un conteneur par CARTE, pas par connecteur

C'est le profil qui l'impose, pas une préférence d'architecture.

> Réécrit le 2026-09-07 après lecture du **DeckLink SDK Manual (mars 2026), §2.4.11** — qui fait
> foi. La première rédaction, tirée de sources secondaires, se trompait sur deux points ; ils sont
> signalés ci-dessous.

Le nombre de périphériques que le SDK **énumère** dépend du profil courant, et un changement de
profil s'applique à **tout le groupe** :

Deux exemples, à lire comme des **illustrations** et non comme la liste des cas à coder — un
appareil sans gestionnaire de profils est légitime et fréquent (la sonde rend alors `profils:
null`), et un profil inconnu doit être porté tel quel plutôt que rejeté :

| Appareil | Portée d'un profil | Profils |
|---|---|---|
| Quad 2 | les 2 sous-périphériques qui partagent les mêmes connecteurs | `TwoSubDevicesHalfDuplex` → 8 sous-périph. (SDI 1 à 8, chacun entrée **ou** sortie) ; `OneSubDeviceFullDuplex` → 4 sous-périph. |
| 8K Pro | **les 4 sous-périphériques**, donc la carte entière | `FourSubDevicesHalfDuplex`, `TwoSubDevicesFullDuplex`, `OneSubDeviceFullDuplex` (quad-link), `OneSubDeviceHalfDuplex` |

**Correction n° 1 — le full-duplex n'est pas une recopie, c'est du key/fill.** J'avais écrit
qu'en profil full-duplex la paire devenait « une entrée plus sa recopie en sortie ». C'est faux :
le manuel donne, sur la Quad 2, `SDI 1 (in/key) & SDI 2 (out/fill)`. Le profil full-duplex est
**ce qui donne accès à l'incrustation** — les attributs `SupportsInternalKeying` /
`SupportsExternalKeying` changent avec le profil. L'arbitrage réel est donc : **8 entrées/sorties
indépendantes OU 4 paires d'incrustation**, pas « indépendant ou redondant ».

**Correction n° 2 — sur la 8K Pro, le profil est global à la carte**, pas par paire. La
conclusion « un conteneur par carte » en sort renforcée, pas affaiblie.

**Les deux modes sont OFFERTS, pas choisis à notre place** (décidé le 2026-09-07). Le plugin
expose le profil et son coût — sur une Quad 2, `2 sous-périph. half-duplex` donne **8 connecteurs
indépendants sans incrustation**, `1 sous-périph. full-duplex` donne **4 paires key/fill**. C'est
l'exploitant qui arbitre, par carte, au déploiement. Trois choses en découlent :

- **le rayon d'action d'un changement diffère selon la carte** : une paire de connecteurs sur
  Quad 2 (les trois autres paires ne bougent pas), la carte entière sur 8K Pro. La sonde rend
  déjà ce nombre — c'est le champ `pairs` de chaque profil — précisément pour qu'on puisse
  l'annoncer avant d'agir, et non le découvrir après ;
- **c'est une opération DESTRUCTIVE**, pas un basculement : `ProfileChanging` prévient que les
  flux vont être arrêtés de force (`streamsWillBeForcedToStop`), et des sous-périphériques
  peuvent devenir inactifs. Elle se démonte proprement avant, sous le verrou de vmid, et elle
  demande une confirmation explicite ;
- ⚠ **RÈGLE : ne jamais identifier un flux par l'index de sous-périphérique.** Un changement de
  profil change le nombre de sous-périphériques et ce que chaque index désigne. Un flux câblé sur
  « l'index 2 » se retrouverait branché sur un autre connecteur, silencieusement — c'est
  exactement le mode de panne déjà payé sur le moteur 2110, où recréer le moteur décalait ses
  abonnements d'un slot sans que rien ne le signale. La clé stable est le
  **`BMDDeckLinkPersistentID`** du sous-périphérique (à défaut le `TopologicalID`) ; la sonde les
  relève tous les deux, et c'est pour ça.

Ce que le manuel ajoute et qu'il faut traiter :

- `IDeckLinkProfile::GetPeers` énumère les sous-périphériques d'un groupe — c'est la primitive
  qui dit ce qu'un changement de profil va perturber ;
- un changement de profil peut rendre des sous-périphériques **inactifs**, et il change les
  **modes vidéo supportés** : il faut re-tester `DoesSupportVideoMode` après, sur chaque
  sous-périphérique. Un cache de capacités posé avant le changement de profil est un piège ;
- le réglage `bmdDeckLinkConfigCaptureGroup` / `PlaybackGroup` permet de **démarrer et arrêter
  plusieurs sous-périphériques ensemble** (§2.4.13). C'est ce qu'il faut pour que 8 entrées
  partent sur la même trame plutôt qu'à 8 instants voisins. Détail à retenir : ce réglage est
  persistant jusqu'au redémarrage de la machine — donc un état hôte à réconcilier, pas à
  supposer.

Trois conséquences, toutes contraignantes :

1. **Le profil est un réglage de déploiement**, posé une fois, pas un basculement à chaud. Le
   sens d'un connecteur ne se change pas en cours d'exploitation sans réinitialiser sa paire.
2. **Un seul process ouvre la carte.** Deux conteneurs qui se disputeraient des sous-périphériques
   de la même paire se marcheraient dessus au premier changement de profil. Donc : un conteneur
   possède la carte entière, expose N connecteurs, et c'est lui l'autorité sur le profil.
3. **Rien ne se code en dur.** Nombre de sous-périphériques, bidirectionnalité, formats
   supportés : tout s'interroge à l'exécution (`IDeckLinkProfileAttributes`). Le même code doit
   servir une Quad 2 et une 8K Pro G2 sans branche par modèle — c'est la condition pour qu'un
   parc mixte fonctionne, et c'est aussi ce qui nous évite de réécrire à la carte suivante.

Le modèle de flux est celui de `2110_io` (`app/io2110_flows.py`) : un pool de slots, des flux
composables `{id, essence, idx, attached_to, label}`, l'audio et l'ANC rattachés à la vidéo par
défaut et détachables. **Connecteurs numérotés à partir de 1**, conformément à
`app/numerotation.py`.

---

***REMOVED******REMOVED*** 2 bis. Aucune branche par modèle — ce que ça veut dire concrètement

C'est la contrainte la plus structurante du chantier, et elle est facile à trahir sans s'en
rendre compte. **Rien dans le code ne doit tester un nom de modèle.** Tout se demande à
l'appareil, à l'exécution :

| Question | Ce qu'on interroge | Ce qu'on ne fait PAS |
|---|---|---|
| Combien de sous-périphériques ? | `BMDDeckLinkNumberOfSubDevices` | déduire d'un nom |
| Ce sous-périphérique est-il seulement ACTIF ? | `BMDDeckLinkDuplex != bmdDuplexInactive` — **à tester en premier** | croire `VideoIOSupport`, qui reste vrai sur un sous-périphérique désactivé (constaté §9 ter) |
| Capture, émet, ou les deux ? | `BMDDeckLinkVideoIOSupport`, une fois l'inactivité écartée | supposer bi-directionnel |
| Quels connecteurs ? | `...VideoInputConnections` / `...VideoOutputConnections` | supposer du SDI |
| Quels profils, et quel rayon d'action ? | `IDeckLinkProfileManager`, `GetPeers` | supposer qu'il y en a |
| Quels formats ? | `DoesSupportVideoMode`, **re-testé après tout changement de profil** | une table de formats par modèle |
| Incrustation ? | `SupportsInternalKeying` / `ExternalKeying` | la déduire du profil |
| Identité stable d'un flux ? | `BMDDeckLinkPersistentID` | l'index de sous-périphérique |

Deux règles qui en découlent :

- **une valeur inconnue se transporte, elle ne se rejette pas.** Un `BMDProfileID` que nous ne
  savons pas nommer doit apparaître tel quel (la sonde rend son four-cc et le libellé
  « inconnu ») : un appareil sorti après notre code doit rester utilisable, quitte à être mal
  étiqueté dans l'interface ;
- **le banc ne prouve la compatibilité que du matériel qu'il a vu.** Ce chantier sera vérifié
  avec un ou deux appareils ; il faut donc que le code n'ait rien à apprendre des autres, plutôt
  que d'espérer les avoir tous essayés.

---

***REMOVED******REMOVED*** 3. L'horloge : un décalage constant, pas un asservissement

Décidé le 2026-09-07 : **la carte est genlockée sur la même source que le GM PTP.** C'est ce qui
rend le chantier abordable.

Sans cette hypothèse, l'horloge de la carte dériverait contre la grille TAI et il faudrait
rattraper la dérive en dupliquant ou en sautant une trame de temps en temps. Avec elle, il ne
reste qu'un **décalage fixe** : le retard entre le front de synchro et le moment où le SDK nous
livre la trame. Ce décalage est une constante à mesurer une fois par modèle de carte et par
format, puis à appliquer à l'estampille.

**Le SDK fournit le pont, mais pas l'origine — et il ne suit PAS l'horloge de l'hôte**
(SDK Manual §2.5.3.30 ; deux fois corrigé par la mesure, cf. §9 quinquies puis §9 unvicies) :
`GetFrameCompletionReferenceTimestamp` rend, dans `ScheduledFrameCompleted`, un horodatage dont
la valeur **part de zéro au démarrage de la lecture**. Le manuel le dit « verrouillé sur
l'horloge système » ; **la mesure dit autre chose** — il suit l'horloge de la CARTE (genlockée
quand une référence est présente, son quartz sinon). C'est précisément ce qui en fait un
instrument utile : le comparer à `CLOCK_REALTIME`, disciplinée par PTP, mesure l'écart entre la
synchro maison et le PTP.

Trois pièges à ne pas confondre :

- `GetHardwareReferenceClock` est une méthode **voisine et inutilisable pour dater** : le manuel
  dit explicitement que ses valeurs absolues sont dénuées de sens et que seules les différences
  entre deux appels ont un sens. Deux noms proches, un seul horodateur ;
- côté **entrée**, `IDeckLinkVideoInputFrame::GetHardwareReferenceTimestamp` existe, mais le
  manuel **ne dit pas** qu'il est verrouillé sur l'horloge système, contrairement à son homologue
  de sortie. **Ne pas supposer la symétrie** — c'est à vérifier au banc, et c'est la première
  chose à établir quand la carte arrive ;
- la méthode maison (timecode incrusté, âge absolu contre l'horloge TAI) reste la **vérification
  indépendante**. Elle ne doit pas devenir la mesure primaire, mais elle ne doit pas disparaître :
  un horodatage fourni par le SDK qui serait faux ne se verrait par aucun autre moyen.

| Mesure | Valeur |
|---|---|
| Décalage capture (front de synchro → livraison SDK) | à mesurer |
| Décalage lecture (soumission → front de sortie) | à mesurer |
| Dérive résiduelle sur 24 h, genlock commun | à mesurer (attendu : nulle) |

Ce que le plugin doit publier, en plus : l'état du verrou de genlock. Une carte qui perd sa
référence continue de sortir des trames — sur une horloge libre. C'est exactement le genre de
panne qui ne se voit sur aucun compteur si on ne la publie pas. **Le témoin existe, ne pas le
réinventer** : `IDeckLinkOutput::GetReferenceStatus`, et le statut
`bmdDeckLinkStatusReferenceSignalLocked` ; côté entrée, `bmdDeckLinkStatusVideoInputSignalLocked`.

---

***REMOVED******REMOVED*** 4. La latence : pas de mode tranche, et c'est structurel

La règle de la maison est que tout nouveau plugin lit et publie en tranches. **Ce plugin ne peut
pas la tenir**, et l'exception doit être écrite dans le code, au même titre que l'entrelacé :

- en **capture**, l'API DeckLink livre une trame complète dans le callback. Il n'existe pas
  d'accès à la trame en cours d'acquisition. On ne peut pas publier avant de l'avoir reçue ;
- en **lecture**, la lecture ordonnancée (`ScheduleVideoFrame`) veut une pré-charge de plusieurs
  trames avant de démarrer, et cette file *est* de la latence.

**Précision du manuel (§2.5.3.13 et les procédures de lecture), lue le 2026-09-07** : la
pré-charge n'est **pas une obligation d'API**. Le manuel écrit noir sur blanc que « si la
pré-charge n'est pas requise », les `ScheduleVideoFrame` initiaux et le couple
`BeginAudioPreroll`/`EndAudioPreroll` peuvent être omis. C'est donc une **marge choisie**, pas un
plancher imposé — ce qui conforte la décision de la mesurer plutôt que de la fixer.
Il existe aussi `DisplayVideoFrameSync`, un affichage immédiat hors ordonnancement : piste de
latence basse à évaluer au banc, avec ses risques propres (aucune protection contre la famine).

Donc une chaîne SDI→MXL→SDI coûtera sensiblement plus qu'une chaîne 2110. C'est acceptable — le
SDI remplace un convertisseur qui coûtait déjà — mais **le chiffre doit être affiché**, sans quoi
il devient une dette invisible : le plugin affichera une cadence parfaite tout du long.

**Décidé le 2026-09-07 : la profondeur de pré-charge se TESTE.** Ce n'est pas un réglage à
choisir sur le papier — c'est le compromis entre la latence et le risque de famine en sortie, et
il se mesure. Le protocole : descendre la pré-charge jusqu'à ce que la sortie décroche, remonter
d'un cran, et retenir la valeur avec sa marge. À faire par format et par cadence, le résultat
étant à consigner ici. Ce qu'il faut surveiller pendant la mesure, c'est le compteur de sous-
alimentation du SDK (`ScheduledFrameCompleted` / `bmdOutputFrameDisplayedLate` et
`bmdOutputFrameDropped`), pas l'œil : une famine occasionnelle ne se voit pas à l'écran mais
signe une profondeur trop basse.

| Format | Pré-charge retenue | Coût en images | Décrochage observé à |
|---|---|---|---|
| à remplir au banc | | | |

---

***REMOVED******REMOVED*** 5. Le pilote sur l'hôte

Desktop Video est un module noyau propriétaire hors-arbre, recompilé par DKMS à chaque noyau, et
**non redistribuable**.

**Décidé le 2026-09-07 : il s'installe À LA MAIN sur le nœud.** Nous ne l'automatisons pas et
nous ne l'embarquons nulle part — c'est ce qui évacue entièrement la question de la
distribution : rien de Blackmagic ne transite par nos images ni par notre zip. Un nœud qui doit
faire du SDI est un nœud qu'on prépare, au même titre qu'on y monte la carte.

Ce que ça laisse à faire, quand même :

- **Secure Boot** : un module DKMS non signé ne charge pas si Secure Boot est actif. À constater
  sur le nœud retenu avant le montage — et à trancher alors (signature locale, ou Secure Boot
  désactivé sur ce nœud) ;
- **la mise à jour de noyau reste le mode de panne**, et l'installation manuelle ne le supprime
  pas : elle le rend même plus silencieux, puisque personne ne repasse derrière. Le nœud doit
  donc le DIRE. C'est le pendant de la décision ci-dessus : présence du module et santé DKMS
  relevées par le sampler de santé (`app/node_health.py`), au même titre que le GPU et le RDMA.
  Un plugin `decklink_io` déployé sur un nœud dont le module ne charge plus doit produire une alerte,
  pas un flux muet ;
***REMOVED******REMOVED******REMOVED*** Installation du 2026-09-07 sur dell-1 (faite)

Desktop Video **16.4** (`desktopvideo_16.4a1_amd64.deb`, archive de 2,08 Go dont seul ce paquet
est utile — le reste est l'interface graphique et MediaExpress). Résultat :

- les deux modules DKMS, `blackmagic` et `blackmagic-io`, **se construisent et se chargent** sur
  le noyau 6.12.96 de Debian 13. Le risque relevé plus haut — la série 6.13 casse la
  construction — reste devant nous, il n'est pas déclenché ;
- le service `DesktopVideoHelper` est actif, `libDeckLinkAPI.so` est en place et vue par
  `ldconfig` ;
- **`/dev/blackmagic*` n'existe pas**, et c'est normal : les nœuds de périphérique
  n'apparaissent qu'avec une carte. Conséquence concrète pour plus tard — le `spec["devices"]`
  du conteneur ne peut pas être posé à l'avance ni supposé présent ;
- la sonde `decklink_probe`, compilée ici contre les en-têtes **12.2.2**, s'exécute sans broncher
  contre la bibliothèque **16.4** du nœud et répond `no_devices`. C'est une **vérification
  d'ABI** qu'on n'aurait pas eue autrement : les en-têtes issus de Debian sont utilisables
  contre le pilote courant.

**Redémarrage vérifié le 2026-09-07** (dell-1 avait 41 jours d'uptime ; il est revenu en 114 s).
Le DKMS survit — `dkms status` reste `installed` sur 6.12.96 et `modprobe` charge les deux
modules. Mais :

> **Les modules ne se rechargent PAS tout seuls au démarrage.** Rien dans `/etc/modules-load.d/`,
> et la règle udev ne les charge pas non plus : sans carte, aucun `modalias` PCI ne les appelle.
> C'est probablement sans conséquence une fois la carte montée — le noyau chargera le module sur
> correspondance PCI — mais **ce n'est pas vérifié**, et c'est le premier point à contrôler le
> jour du montage.

Ça a révélé un défaut de la sonde, corrigé le jour même : dans cet état, `libDeckLinkAPI.so`
répond parfaitement et annonce **zéro carte**. Sur une machine sans matériel c'est juste ; avec
une carte et le module déchargé, ç'aurait été un diagnostic faux, indiscernable de la panne
réelle. La sonde rend désormais un état `module_absent` distinct (rc 4), exercé au banc **et**
constaté sur dell-1 en déchargeant le module.

Une réserve subsiste :

- le paquet tire `libgl1` en dépendance, donc de la pile graphique sur un nœud sans écran. Sans
  conséquence connue, mais c'est une surface installée en plus.

---

***REMOVED******REMOVED******REMOVED*** Ce que la bibliothèque 16.4 a appris (2026-09-07, mesuré sur dell-1)

Trois constats tirés de la bibliothèque installée, tous vérifiables en une commande.

**1. La compatibilité ascendante est réelle, et c'est pourquoi nos en-têtes 12.2.2 marchent.**
`libDeckLinkAPI.so` 16.4 exporte encore les fabriques des générations précédentes —
`CreateDeckLinkIteratorInstance_0002`, `_0003` et `_0004`, celle que réclame notre dispatch.
Blackmagic ne retire pas les anciens points d'entrée. Ce n'est donc pas un coup de chance ; mais
c'est une propriété **constatée**, pas promise : à revérifier si le pilote du nœud change de
version majeure.

**2. Le 2110 est DANS cette bibliothèque.** Les symboles `IPFlow` / `IPExtensions` y sont
présents. Ce qui manque pour le §8 bis, ce n'est donc ni un autre pilote ni une autre
bibliothèque — ce sont seulement les **déclarations**, absentes de nos en-têtes 12.2.2. Le jour
où la question sera tranchée dans ce sens, le coût est celui d'en-têtes récents, pas d'une
réinstallation.

**3. Les nœuds de périphérique sont `/dev/blackmagic/dv<N>` et `/dev/blackmagic/io<N>`** (plus
`ttydv`/`ttyio` pour le contrôle série). Deux conséquences pour le déploiement :

- Docker veut des fichiers de périphérique, pas un répertoire : le `spec["devices"]` devra
  lister les nœuds **un par un** ;
- ces nœuds **n'existent qu'avec une carte présente**. Le hook de déploiement du plugin devra
  donc les **énumérer sur l'hôte** au moment du déploiement, et non les supposer — un chemin
  codé en dur donnerait un conteneur qui démarre et ne voit rien.

**La sonde tourne dans un conteneur.** Vérifié sur dell-1 avec l'image `bobi-compute` : en liant
seulement `libDeckLinkAPI.so` en lecture seule, la sonde répond correctement depuis l'intérieur.
Le contrôle du module noyau fonctionne aussi de là — `/proc/modules` vu d'un conteneur est celui
de l'hôte. C'est le plan de déploiement du futur plugin validé avant la carte : bibliothèque
liée depuis l'hôte, périphériques passés un par un.

---

***REMOVED******REMOVED******REMOVED*** Relevé de flotte du 2026-09-07 (mesuré)

Interrogation en lecture seule des quatre nœuds via `node_driver.host_exec`.

| Nœud | Secure Boot | Slots libres | dkms | Verdict |
|---|---|---|---|---|
| dell-1 | désactivé | **5** (4× x8, 1× x16), tous *Long* | absent | **candidat naturel** |
| dl360-1 | désactivé | 1 (x8, ***Short***) | absent | à vérifier physiquement |
| r620-1 | non supporté | 0 | présent (mft, nvidia) | plein |
| r620-2 | non supporté | 0 | présent (nvidia) | plein |

Trois enseignements :

1. **Secure Boot n'est un problème sur aucun nœud** — désactivé sur les deux Dell/HPE récents,
   non supporté par les R620. La question posée à l'ouverture du chantier est close : le module
   DKMS non signé chargera.
2. **`dkms` n'est pas installé** sur les deux nœuds candidats (les en-têtes noyau, si). C'est un
   prérequis à poser avant l'installation manuelle du pilote, et il ne se voit pas avant d'avoir
   essayé — d'où son relevé ici.
3. **dell-1 est le nœud d'accueil**, et de loin : cinq slots libres pleine longueur, contre un
   seul sur dl360-1 — encore ce dernier est-il annoncé *Short* (mi-longueur, 1U), ce qui demande
   de confronter le format physique de la carte avant d'y compter. Les deux R620 sont pleins.

Ce n'est pas neutre pour la suite : dell-1 porte déjà le GPU P5000 et la ConnectX-3 Pro (RDMA),
et n'a **pas** la capacité `io2110`. Une chaîne SDI→2110 passera donc par le bus MXL entre deux
nœuds, pas à l'intérieur d'un seul — ce qui est cohérent avec le §7, mais s'ajoute au budget de
latence du §4.

- le passage au conteneur ne demande aucune extension de contrat : `spec["devices"]` existe déjà
  côté agent-nœud (`node_agent/agent.py`, ajouté au chantier RDMA).

---

***REMOVED******REMOVED*** 6. Essences

- **Vidéo** : la carte délivre du v210 (10 bits) nativement. **Décidé le 2026-09-07 : la
  conversion est un RÉGLAGE du plugin — planar par défaut, v210 brut au choix.** Le défaut sert
  la chaîne maison (tous nos plugins lisent du planar, et `v210_bridge` a déjà chiffré ce que
  coûte la conversion) ; l'option sert les consommateurs TIERS sur le bus MXL, qui peuvent
  vouloir le v210 tel qu'il sort du fil, sans aller-retour. Les deux modes doivent être
  exercés — un réglage qui n'a jamais servi n'est pas un réglage.
  L'écriture vers MXL est non temporelle, comme partout ailleurs.
- **Audio** : embarqué, jusqu'à 16 canaux (64 sur la 8K Pro G2, réparties sur les 4 connecteurs).
  Sort en flux audio MXL. L'index audio MXL est un « un-après-la-fin » — piège déjà documenté,
  à ne pas repayer.
- **ANC** : le VANC capturé (timecode, sous-titres, sADM éventuel) passe par le chemin RFC 8331
  déjà en place.

---

***REMOVED******REMOVED*** 7. NMOS : un plugin MXL comme les autres

**Tranché le 2026-09-07.** `decklink_io` n'a droit à aucun traitement particulier : ses flux se
publient sur le Device IS-04 du bus MXL, exactement comme ceux de n'importe quel autre plugin
producteur. Un connecteur SDI n'est pas un transport IP et n'a pas à se déguiser en sender 2110.

La conséquence est nette et il faut l'assumer : **ce qui est routable par un contrôleur NMOS
tiers, c'est le flux 2110 produit par un `2110_io` placé derrière** — pas l'entrée SDI
elle-même. Le SDI entre sur le bus, le bus sort en 2110 ; c'est la même chaîne que pour toute
autre source, et elle n'a pas besoin d'exception.

Rien de spécifique à écrire côté `services/nmos/` : si le plugin se comporte en producteur MXL
ordinaire, il est publié par le chemin générique. **C'est le contrôle à faire à l'étape 9** —
si le NMOS demande un cas particulier pour le SDI, c'est que le plugin ne se comporte pas en
producteur ordinaire, et c'est le plugin qu'il faut corriger.

---

***REMOVED******REMOVED*** 8. Licence et distribution

Réglé par la décision du §5 : le pilote s'installe à la main, donc **rien de Blackmagic ne se
distribue**. Restent deux points :

- ~~les **en-têtes du SDK**~~ — **réglé le 2026-09-07** : ils sont **embarqués dans le dépôt**
  (`plugins/decklink_io/decklink_sdk/`). Leur licence Blackmagic autorise explicitement la
  redistribution par un tiers, notice conservée — c'est à ce titre que Debian les publie dans
  `main`, au sein de la source `gst-plugins-bad1.0`, d'où ils viennent (SDK **12.2.2**). Ce
  n'est donc pas le pilote qui est non redistribuable *et* les en-têtes avec : seul le pilote
  l'est.

  **Couverture vérifiée le 2026-09-07** (`make couverture`) : les 19 symboles dont le chantier
  dépend — horodatage de fin de trame, statut de genlock, groupes de capture/lecture,
  `PersistentID`, keying, paquets ANC, profils, v210, conversions — sont tous déclarés en 12.2.2,
  avec les bonnes signatures. Le contrôle est une COMPILATION, pas un `grep` : il exige la
  signature entière. **Il ne sera donc pas nécessaire de récupérer le SDK officiel pour écrire le
  plugin.** Le seul manque connu reste les interfaces SMPTE 2110, sans objet depuis que le §8 bis
  est tranché en option A ;
- **pas de FFmpeg pour le chemin de données** : le `decklink` de libavdevice impose un build
  `--enable-nonfree`, rédhibitoire pour ce qu'on distribue. Chemin natif, sur le patron de
  `mtl_rx.c`.

L'entrée correspondante dans `THIRD-PARTY-NOTICES.md` est écrite (section « En-têtes tiers
versionnés dans le dépôt »).

---

***REMOVED******REMOVED*** 8 bis. Ce que le manuel révèle et qui dépasse le chantier

Deux trouvailles de la lecture du 2026-09-07 qui ne relèvent pas de l'implémentation mais de la
stratégie, et qu'il faut porter devant quelqu'un plutôt que trancher ici.

> **TRANCHÉ le 2026-09-07 : option A — le SDI entre et sort par le bus MXL.** Le SDI est une
> SOURCE du produit (et une destination), pas un flux qui traverse. Ce qui suit reste consigné
> parce que la carte IP existe et resservira peut-être ; ce n'est plus une question ouverte.

**Le SDK DeckLink pilote des flux SMPTE 2110** (§2.4.15). Il existe des cartes DeckLink *IP*
(ex. DeckLink IP/SDI HD) qui font passerelle SDI ↔ 2110 nativement : on leur écrit le SDP du
pair (`bmdDeckLinkIPFlowPeerSDP`), on lit le SDP de nos senders, on active le flux. Le manuel
note d'ailleurs que « la même fonctionnalité s'obtient avec un contrôleur NMOS du commerce ».

C'était à la fois une opportunité — cette carte fait, dans un seul PCIe, ce que notre relevé de
flotte impose de répartir sur deux nœuds (§5) — et un concurrent de notre propre chaîne. Le motif
qui a tranché : **cette carte est un convertisseur, simplement déplacé dans le serveur**, alors
que le chantier existe pour s'en passer. Un SDI qui entre en 2110 n'est accessible ni au
multiview, ni au scope, ni au mixer, ni à l'enregistrement sans redescendre par un `2110_io` — on
paierait la chaîne 2110 en plus, sans maîtriser l'horodatage. Et le manuel note lui-même que
« la même fonctionnalité s'obtient avec un contrôleur NMOS du commerce » : la livrer ne nous
distingue de personne.

**Le SDK fournit des conversions de format accélérées SIMD** (`IDeckLinkVideoConversion`,
§2.4.14). Pertinent pour le v210 → planar du §6 : à confronter à notre propre conversion avant
d'écrire la nôtre, sachant que ça consomme du CPU sur le nœud comme la nôtre.

---

***REMOVED******REMOVED*** 9. Étapes, chacune avec un jalon vérifiable

| ***REMOVED*** | Étape | Jalon |
|---|---|---|
| 1 | Pilote posé à la main sur un nœud | ✅ **fait et vérifié au redémarrage le 2026-09-07 sur dell-1** (Desktop Video **16.4**) — cf. §5 |
| 2 | Énumération | ✅ **fait le 2026-09-07** — `plugins/decklink_io/tools/probe/`, sans branche par modèle, exécutée contre la VRAIE bibliothèque 16.4. Reste à confronter à une carte |
| 3 | Profil | ✅ **fait le 2026-09-08** — `plugins/decklink_io/tools/profile`, attente de `ProfileActivated`, balayage de TOUS les groupes |
| 4 | Capture d'un connecteur | ✅ **COMPLET le 2026-09-08** — 1080p50 détecté seul, écrit en grains MXL, **lu par un consommateur à 50,2 grains/s** |
| 5 | Décalage horloge | les deux constantes du §3 mesurées, appliquées, revérifiées |
| 6 | Audio + ANC | ✅ **COMPLET le 2026-09-08** — audio à −11 dBFS lus, timecode ATC synthétisé et décodé en aller-retour |
| 7 | Sortie | ✅ **fait le 2026-09-08** — 603 trames à 50,25/s, 0 perdue, 6 répétées (dérive sans genlock) |
| 7 bis | Pré-charge | le tableau du §4 rempli : décrochage provoqué, marge retenue |
| 8 | Densité | ✅ **fait le 2026-09-08** — plafond entre 829 Mo/s et 1 106 Mo/s : une Duo 2 ne fait PAS 4 × 1080p50. Le CPU n'est pas le facteur |
| 9 | Intégration | câblage, palette, projets, macros — et NMOS publié SANS cas particulier (§7) |
| 9 bis | Déployable | hook de déploiement (énumération des `/dev` sur l'hôte), image publiée, `nav` au manifeste |

Plus aucune étape n'attend une décision : les quatre points ouverts sont tranchés (§10).

L'étape 2 est **écrite et vérifiée sans matériel** : `plugins/decklink_io/tools/probe/` compile contre les
en-têtes seuls (la bibliothèque n'est chargée qu'à l'exécution, par `dlopen`), et sa vérification
exerce pour de vrai les trois réponses possibles en l'absence de carte — « pas de bibliothèque »,
« bibliothèque et zéro carte », « bibliothèque sans le symbole ». Les deux premières se
ressemblent assez pour qu'une sonde qui les confondrait passe pour bonne jusqu'au montage ; la
vérification a été **mutée** pour s'assurer qu'elle les sépare. Ce que la sonde ne peut pas
prouver sans carte : que ce qu'elle affiche d'une VRAIE carte est juste.

Tout le reste dépend d'une carte montée sur un nœud.

---

***REMOVED******REMOVED*** 9 bis. Le jour du montage — dans cet ordre

Tout ce qui précède a été préparé sans carte. Ce qui suit est la séquence à suivre quand elle
arrive, avec ce qu'il faut regarder à chaque pas. L'ordre compte : chaque étape invalide la
suivante si elle est sautée.

1. **Charger le module avant de conclure quoi que ce soit.** Les modules ne se rechargent pas au
   démarrage sans carte (§5) ; avec une carte, le noyau devrait les charger sur correspondance
   PCI — **c'est à vérifier, ce n'est pas acquis**. Contrôle : `plugins/decklink_io/tools/probe` doit
   répondre autre chose que `module_absent`.
2. **Prévoir une mise à jour de micrologiciel.** Le paquet embarque `DesktopVideoUpdateTool` et
   une dizaine de blobs. Une carte neuve en réclame souvent une, et elle demande un redémarrage.
   À faire AVANT de mesurer quoi que ce soit — un micrologiciel changé invalide les mesures.
3. **Passer la sonde** et lire ce qu'elle dit des profils : `pairs` donne le rayon d'action d'un
   changement, `persistent_id` la clé d'identité des flux (§2).
4. **Refaire le relevé de ce que la bibliothèque ouvre.** Le tracé du 2026-09-07 (`strace`) ne
   montre AUCUN accès hors nos propres lectures — mais il a été fait avec **zéro périphérique**,
   donc il ne prouve rien sur le comportement avec une carte. La question ouverte est de savoir
   si la bibliothèque dialogue avec le démon `DesktopVideoHelper` : si oui, le conteneur devra
   atteindre ce canal, et pas seulement les nœuds `/dev`. **À refaire avec la carte, avant
   d'écrire le plugin.**
5. **Puis seulement** : capture d'un connecteur (jalon 4), les deux constantes d'horloge (§3),
   et le tableau de pré-charge (§4).

Ce qui est déjà acquis et n'aura pas à être refait : les nœuds de périphérique sont créés en
`MODE="0666"` par la règle udev, donc **aucun montage de permissions n'est à prévoir** — passer
le périphérique au conteneur suffit, sans uid, gid ni groupe supplémentaire.

---

***REMOVED******REMOVED*** 9 ter. Premier montage — DeckLink Duo 2 sur dell-1, le 2026-09-08

Séquence du §9 bis suivie dans l'ordre. Ce qu'elle a donné :

**1. Le chargement automatique du module fonctionne.** Le noyau a chargé `blackmagic_io` sur
correspondance PCI au démarrage, sans rien dans `modules-load.d`. La question laissée ouverte le
2026-09-07 est close. Et les permissions sont bien celles prévues : `crw-rw-rw-` sur `io0` à
`io3` — **aucun montage de droits à prévoir pour le conteneur**.

**2. Le micrologiciel était périmé, et c'est ce qui bloquait tout.**

```
BlackmagicIO: WARNING: "DeckLink Duo 2" has a firmware version mismatch
              (valid: 1 device: 0x0123 driver: 0x0128)
```

Le pilote active la carte et peuple `/dev/blackmagic/`, mais **le SDK refuse de l'énumérer**.
`DesktopVideoUpdateTool --update --all` a écrit le nouveau micrologiciel ; l'outil réclame
ensuite un arrêt complet (« Please shut down your computer »), mais **un simple redémarrage a
suffi** — à ne pas généraliser, c'est une observation sur une machine, pas une règle.

> ★ **La sonde a menti, et c'est le défaut le plus utile qu'elle nous ait rendu.** Dans cet état
> elle répondait « aucune carte — réponse correcte sur une machine sans matériel », avec une
> carte dans le châssis. Elle ne croisait pas ce que le NOYAU expose avec ce que le SDK énumère.
> Corrigé le jour même : un cinquième état `non_enumere` (rc 5) dit le désaccord au lieu de
> choisir le camp du SDK, et pointe le micrologiciel. C'est exactement la panne qu'un exploitant
> rencontrera au déballage d'une carte neuve.

**3. La sonde énumère les 4 sous-périphériques.** Profil actif `1dfd`
(`OneSubDeviceFullDuplex`), deux profils disponibles, `pairs: 1` — le groupe est bien une paire.

> ⚠ **Piège relevé, à ne pas répéter dans le plugin.** Les sous-périphériques 2 et 3 sont
> `duplex: "inactive"` — le profil full-duplex les désactive, comme le manuel l'annonçait. Or
> `BMDDeckLinkVideoIOSupport` continue d'annoncer `capture: true, playback: true` **pour ces
> sous-périphériques inactifs**. C'est une capacité STATIQUE du matériel, pas une disponibilité.
> Un plugin qui se fierait à `VideoIOSupport` seul offrirait 4 entrées là où 2 fonctionnent.
> **Filtrer d'abord sur `BMDDeckLinkDuplex != bmdDuplexInactive`.** Le §2 bis est corrigé en
> conséquence.

L'incrustation suit la même ligne : `keying_interne`/`externe` sont vrais sur les deux
sous-périphériques actifs et faux sur les inactifs — cohérent avec le manuel, qui dit que le
keying dépend du profil.

**4. À faire encore** : refaire le relevé `strace` maintenant qu'une carte est là (celui du
2026-09-07, sans matériel, ne prouvait rien), pour savoir si la bibliothèque dialogue avec
`DesktopVideoHelper` — ce qui déciderait si un conteneur a besoin d'autre chose que les `/dev`.

---

***REMOVED******REMOVED*** 9 quater. Exigence d'interface : l'état du micrologiciel se voit et se corrige

Demandé le 2026-09-08, à la suite de ce qui précède. Un micrologiciel périmé rend la carte
**invisible au SDK** tout en la laissant présente pour le noyau : sans rien dans l'interface,
l'exploitant voit « aucune carte » et n'a aucun moyen de comprendre.

- **Voir** : l'état du micrologiciel est remonté par nœud et par carte, au même titre que le
  verrou de genlock. `DesktopVideoUpdateTool --list` en donne le verdict en clair
  (`Firmware is out of date` / `up to date`) ; l'état `non_enumere` de la sonde en est le
  symptôme visible côté SDK.
- **Corriger** : proposer la mise à jour depuis l'interface, sans ligne de commande.
- **Trois précautions**, parce que c'est une écriture matérielle : opération explicite et
  confirmée, jamais automatique ni déclenchée par un déploiement ; annoncer d'emblée qu'un
  **redémarrage du nœud** sera nécessaire pour qu'elle prenne effet, et donc que les conteneurs
  du nœud tomberont ; et signaler que la carte reste annoncée « périmée » entre l'écriture et le
  redémarrage — un exploitant qui relance la mise à jour en boucle à cause de ça serait de notre
  faute, pas de la sienne.

---

***REMOVED******REMOVED*** 9 quinquies. Banc de sortie du 2026-09-08 — le pont d'horloge, mesuré

`plugins/decklink_io/tools/out` : émet un motif sur le premier sous-périphérique ACTIF capable de
lecture, et compare trame par trame `GetFrameCompletionReferenceTimestamp` à `CLOCK_REALTIME`.
Aucune entrée SDI requise — c'est ce qui a permis de le passer avant tout câblage.

**Première mesure (Duo 2, 299 trames, pré-charge 3, sans référence branchée)** :

| | |
|---|---|
| Santé de la lecture | **299 complétées, 0 en retard, 0 perdue, 0 vidangée** |
| Écart carte ↔ horloge système | **amplitude 0,109 ms** sur 10 s |
| Dérive | **+0,01 ppm** |

> ★ **CORRECTION du §3.** J'avais écrit que `GetFrameCompletionReferenceTimestamp` rend « un
> horodatage verrouillé sur l'horloge système », donc directement la relation carte ↔ TAI. La
> mesure dit autre chose : la valeur rendue part de **zéro au démarrage de la lecture**. Elle est
> verrouillée sur le **RYTHME** de l'horloge système, pas sur son **époque**. Le pont vers TAI
> existe donc bien, mais il faut relever l'origine une fois, au premier callback — ce n'est pas
> l'API qui la donne.
>
> La bonne nouvelle est que cette origine est **stable** : l'écart ne bouge que de 0,109 ms sur
> 10 s, et la dérive de 0,01 ppm est le signe que l'horodatage est bien dérivé de l'horloge de
> l'hôte et non du quartz de la carte (un quartz libre dériverait de plusieurs dizaines de ppm).

> ⚠ **Ce que ce banc NE mesure PAS, et qu'il ne faut pas lui faire dire.** Il compare l'instant
> où le pilote *rapporte* la trame à l'instant où nous lisons l'horloge. Il ne dit rien de
> l'instant où la trame sort ÉLECTRIQUEMENT du connecteur. Le décalage « soumission → front de
> sortie » du §3 reste **non mesuré** : il demande un bouclage physique d'une sortie vers une
> entrée, donc un câble.

**Deux enseignements pour le plugin, tirés du banc lui-même :**

- **la pré-charge de 3 suffit** dans ce régime — 0 famine sur 299 trames. Ce n'est qu'un point de
  la courbe du §4 ; le protocole reste de descendre jusqu'au décrochage ;
- **« le premier mode supporté » est une mauvaise politique.** Le banc, faute de mieux, prend le
  premier mode que la carte déclare — et tombe sur du `525i59.94 NTSC 720x486`. C'est correct au
  sens de la règle « ne rien coder en dur », mais ça montre que le plugin devra **choisir**
  (réglage explicite, ou format détecté à l'entrée), jamais prendre le premier venu.

---

***REMOVED******REMOVED*** 9 sexies. Jalons 3 et 4 — profil basculé, signal capturé (2026-09-08)

***REMOVED******REMOVED******REMOVED*** Le signal était là, sur un connecteur que le profil avait ÉTEINT

Le testeur annonçait du signal ; les deux entrées actives n'en voyaient aucun. Ma première
hypothèse — « le câble est sur un connecteur qui est une SORTIE dans ce profil » — était à côté.
La vraie raison est pire, et plus instructive : en profil `1dfd`, les sous-périphériques 2 et 3
sont **inactifs**, donc leurs connecteurs sont **morts**, ni entrée ni sortie. Le câble était sur
l'un d'eux.

Après bascule en `2dhd` (4 connecteurs indépendants), le signal apparaît immédiatement :
**1080p50, 1920×1080, détecté tout seul**, 230 trames valides en 5 s.

> ★ **À retenir pour l'interface.** Un connecteur peut être *éteint par le profil*, et rien sur
> le châssis ne le dit. Un exploitant qui branche là ne verra jamais rien et n'aura aucune piste.
> L'interface doit montrer, par connecteur, s'il est **entrée / sortie / éteint** — sans quoi on
> reproduira exactement cette demi-heure perdue, à chaque site.

***REMOVED******REMOVED******REMOVED*** Un appareil peut porter PLUSIEURS groupes de profils

`plugins/decklink_io/tools/profile` (jalon 3) bascule le profil et **attend `ProfileActivated`** — le
manuel prévient que l'activation n'est pas terminée au retour de `SetActive`, et croire ce retour
mène à ré-énumérer trop tôt.

La première bascule n'a changé que **la moitié** de la carte :

| | avant | après la 1ʳᵉ bascule |
|---|---|---|
| idx 0 (persistent …136) | `1dfd` full | **`2dhd` half** |
| idx 1 (…137) | `1dfd` full | `1dfd` full |
| idx 2 (…138) | `1dfd` inactive | **`2dhd` half** |
| idx 3 (…139) | `1dfd` inactive | `1dfd` inactive |

Une Duo 2 porte **deux groupes de deux sous-périphériques**, et activer un profil sur l'un ne
touche pas l'autre. L'outil balaie donc jusqu'à ce que tous les groupes soient sur le profil visé,
en repartant d'un **itérateur neuf** à chaque passe.

> ★ **Et le groupe n'est pas fait d'index voisins** : c'est {0, 2} et {1, 3}, pas {0, 1} et
> {2, 3}. Un code qui aurait supposé « les sous-périphériques vont par paires consécutives »
> aurait basculé le mauvais connecteur. Encore une raison de ne jamais raisonner sur l'index —
> seul `GetPeers` dit qui va avec qui.

***REMOVED******REMOVED******REMOVED*** Le piège de la reconfiguration en boucle

`VideoInputFormatChanged` demande de reconfigurer l'entrée sur le format détecté, sinon les
trames restent invalides. Mais reconfigurer **re-déclenche** la notification : première mesure,
**66 « changements de format » en 3 secondes** sur une source parfaitement stable, avec des
trames invalides à chaque tour de manège.

Correction : ne reconfigurer que si le mode diffère réellement du mode courant. 66 → **1**, et
les trames valides passent de 65 à 230 sur une fenêtre à peine plus longue. **Le plugin doit
porter cette garde dès la première version** — sans elle, une source stable a l'air instable, et
la cadence s'effondre sans que rien ne soit signalé.

---

***REMOVED******REMOVED*** 9 septies. Jalon 4 COMPLET — SDI → MXL, lu par un consommateur (2026-09-08)

`plugins/decklink_io` capture en v210 10 bits, convertit en planar et écrit des grains MXL.
Vérifié **de bout en bout**, producteur dans un conteneur, consommateur dans un autre :

| | |
|---|---|
| Grains distincts lus en 4 s | **201 → 50,2/s** (cadence pleine 1080p50, aucun déficit) |
| flowDef lu par le consommateur | 1920×1080, `video/x-mxl-planar`, Y 1920 / Cb 960 / Cr 960, 10 bits |
| Luma | min 4, max 1019, moyenne 318 — dynamique réelle |
| Chroma Cb | min 195, max 748, moyenne 536 (≈ 512, neutre) |

> ★★★ **LE PIÈGE : un grain validé sans `validSlices` est écrit et reste INVISIBLE.**
> Première exécution : le producteur annonçait **380 grains écrits**, le consommateur en lisait
> **0**. Aucune erreur nulle part — `mxlFlowWriterCommitGrain` rend `MXL_STATUS_OK`, le flux
> existe, sa définition se lit parfaitement, les compteurs du producteur sont flatteurs. Un grain
> n'est lisible que lorsque `validSlices == totalSlices`, et `OpenGrain` laisse `validSlices` à 0.
>
> Il manquait **une ligne**. Ce qui l'a révélée n'est pas le producteur — il n'avait rien à dire —
> mais le fait d'avoir branché un vrai consommateur au lieu de croire les compteurs.
> **Aucun producteur MXL ne doit être déclaré fonctionnel sur la foi de ses propres compteurs.**

***REMOVED******REMOVED******REMOVED*** Comment l'image est construite, et pourquoi ainsi

`libmxl` n'est **pas** reconstruite : elle est reprise telle quelle de l'image d'exécution
(`COPY --from=bobi-compute`), ce qui garantit l'ABI et ramène la construction à quelques secondes
au lieu d'une quarantaine de minutes. Seuls les **en-têtes** sont apportés, depuis le dépôt MXL
public à la version de l'image.

Et **rien de Blackmagic n'est embarqué** : `libDeckLinkAPI.so` est montée depuis l'hôte au
`docker run`, conformément au §5. Seuls les en-têtes DeckLink entrent dans le contexte, ce que
leur licence autorise (§8). Le contrôle de dépendances (`ldd | grep "not found"`) est fait dans
l'image **finale**, seul endroit où il veut dire quelque chose — placé dans l'étage de
compilation, il rendait un faux positif.

***REMOVED******REMOVED******REMOVED*** Dettes ouvertes à la sortie de ce jalon

- **`grain_rate` vaut `50000/1000`** et non `50/1` : je prends le rationnel brut de DeckLink,
  là où `mtl_rx.c` canonicalise. Le rapport est identique et la grille d'index aussi, mais deux
  producteurs qui annoncent la même cadence différemment finiront par gêner un comparateur
  (NMOS, appariement de flux). **À canonicaliser.**
- **L'index de grain reste `mxlGetCurrentIndex`**, pas l'horodatage de la trame (cf. §9
  quinquies) — la latence n'est donc pas encore chiffrable.
- **Le flowDef reste dupliqué** depuis `mtl_rx.c` ; seul l'UUID est couvert par un contrôle.
- **Audio et ANC ne sont pas capturés** — la trame vidéo seule est écrite.

---

***REMOVED******REMOVED*** 9 octies. Le plugin existe et tourne en conteneur (2026-09-08)

`plugins/decklink_io` est un **sous-module** (dépôt privé
`bob-integration/bobistudio-plugin-decklink_io`). Il porte le chemin de données natif, le
superviseur, le manifeste, l'image et les bancs.

Vérifié en conteneur, avec la carte :

| | |
|---|---|
| `:8080` | `{"fps": 50.01, "frame_index": 371, "plugin_version": "0.1.0"}` — contrat de flotte |
| `:8082/state` | `mode "actif"`, signal true, 1920×1080, 0 redémarrage |
| Entrée **sans** signal | `mode "pas de signal"`, fps 0 — **distinct d'un plantage** (§1) |
| Binaire tué (`-9`) | `mode "arrêté (code -9) — relance"`, `redemarrages: 1`, puis retour à 49,98 |

> ★ **Un test destructeur qui ne détruit rien rend un vert qui ne prouve rien.** Les deux
> premières tentatives pour tuer le binaire n'ont rien tué — `docker exec` ne lance pas de shell
> et `kill` y est une primitive. L'état affichait « 0 redémarrage », ce qui ressemblait à une
> réussite. C'est le `frame_index` qui continuait de monter qui l'a démenti.

**Deux choix du superviseur qui méritent d'être dits** : la cadence publiée sur `:8080` est celle
de l'entrée la plus **lente**, pas la moyenne — sur une carte à quatre entrées, une moyenne
masquerait le décrochage de l'une d'elles. Et les **redémarrages sont comptés et publiés** : sans
ce compteur, un binaire qui redémarre en boucle produit une cadence qui a seulement l'air
« instable », et personne ne va lire les journaux du conteneur pour comprendre.

**Pas de `nav` dans le manifeste, délibérément.** Le registre versionne le plugin mais n'émet ni
puce de palette ni entrée de menu. Il manque le hook de déploiement — qui devra **énumérer**
`/dev/blackmagic/*` sur l'hôte, ces nœuds n'existant qu'avec une carte — et l'image publiée.

---

***REMOVED******REMOVED*** 9 nonies. Le dernier verrou : faire passer les périphériques au conteneur

Le plugin tourne quand on lui monte les `/dev` à la main. Pour qu'il soit **déployable depuis
l'orchestrateur**, il manque une seule chose — et elle n'est pas dans le plugin.

**L'agent-nœud sait déjà le faire** : `spec["devices"]` est honoré depuis la version 0.10.0
(`node_agent/agent.py`, chantier RDMA) et se traduit en `docker run --device`. **Mais
`docker_compute` ne pose jamais cette clé.** Rien, aujourd'hui, ne relie un besoin de
périphérique déclaré par un plugin au conteneur qu'on crée pour lui.

***REMOVED******REMOVED******REMOVED*** Ce que le hook du plugin ne peut PAS faire (constaté, pas supposé)

J'avais prévu de valider les périphériques dans `before_deploy`. Deux limites l'interdisent :

- **le hook ne peut pas refuser un déploiement** — `deploy.py` avale ses exceptions et poursuit
  (« un hook qui lève une exception est ignoré ; le deploy continue ») ;
- **il ne reçoit pas le nœud** (`{{vmid, type, hostname, settings}}`), donc il ne peut pas
  interroger la carte.

Une validation écrite là aurait été **silencieusement ignorée**. Le partage retenu : le hook
NORMALISE (ce qui ne demande que les params), le superviseur DIAGNOSTIQUE dans le conteneur (ce
qui demande la carte) et publie son verdict sur `:8082`.

***REMOVED******REMOVED******REMOVED*** La décision à prendre, et sa part de sécurité

Il faut un mécanisme générique, et le choix n'est pas neutre :

| Piste | Ce qu'elle vaut | Ce qu'elle coûte |
|---|---|---|
| **Manifeste déclaratif** (`host_devices: ["/dev/blackmagic/io*"]`), `docker_compute` énumère sur le nœud et injecte | déclaratif, aucun code de plugin en process, lisible | **un plugin peut demander n'importe quel `/dev`** |
| **Hook dédié** rendant la liste des périphériques | souple | fait tourner du code de plugin dans l'orchestrateur, pour une décision de sécurité |
| **Réglage d'exploitant** par conteneur | l'humain tranche | pénible, et il faudra bien lui proposer une liste |

⚠ **Le point qui compte** : la page Catalogue installe des plugins depuis GitHub. Un manifeste
tiers qui déclarerait `host_devices: ["/dev/*"]` obtiendrait, sans que personne ne le lise, un
conteneur avec accès aux disques du nœud. Quelle que soit la piste retenue, il faut une **liste
blanche de préfixes** côté orchestrateur, et le refus doit être visible.

**Non tranché — c'est une décision de produit, pas d'implémentation.**

---

***REMOVED******REMOVED*** 9 decies. Audio embarqué capturé (2026-09-08)

`--audio <2|8|16>` publie l'audio embarqué en flux MXL `<nom>_audio`.

| | |
|---|---|
| Échantillons | 284 175 sur 16 canaux |
| Réellement écrits dans l'anneau | **4 546 800 flottants** = 284 175 × 16 — aucun perdu |
| `slc.count` | 16, conforme aux canaux demandés |
| Côté consommateur | **−11 dBFS sur les canaux 1 et 2**, les autres silencieux |

**Le bus MXL porte du float32 PLANAR par canal**, pas du L24 entrelacé comme le fil 2110 : la
carte livre des entiers 32 bits entrelacés, la conversion est à notre charge. Écrire les octets
tels quels donnerait un flux qui se lit sans erreur et ne contient que du bruit.

> ★ **Deux fois, une fenêtre de mesure trop courte m'a fait conclure au silence.** Une lecture
> de 100 ms donnait « tous les canaux à zéro » ; une crête brute relevée sur un passage calme
> donnait −80 dBFS, que j'ai d'abord lue comme « la source est muette ». C'est en cumulant la
> crête sur 2 s que l'audio est apparu, à −11 dBFS.
>
> D'où `audio_peak_raw` dans les stats — la crête **avant** conversion. C'est la seule mesure qui
> départage « la source n'envoie rien » de « notre conversion écrase » : deux pannes qui
> produisent exactement le même silence chez le consommateur. Sans elle, j'aurais cherché un bug
> qui n'existait pas.

Deux autres décisions : un trou est comblé par du **silence** (jusqu'à 250 ms ; au-delà,
ré-ancrage franc), sans quoi le consommateur relit des échantillons périmés — du son ancien qui
repasse, qui s'entend et ne se voit sur aucun compteur. Et un nombre de canaux hors {2, 8, 16}
est **refusé** plutôt que corrigé en douce : qui demande 6 canaux doit savoir qu'il n'en aura
pas 6.

**Reste pour l'essence** : l'ANC (timecode, sous-titres) n'est pas capturé.

---

***REMOVED******REMOVED*** 9 undecies. ANC : l'empaqueteur est vérifié, le chemin n'a pas encore vu un paquet (2026-09-08)

`--anc` publie les paquets ANC en flux `<nom>_anc` (`video/smpte291`, `bobi_anc_format:
rfc8331`). L'empaqueteur natif est un miroir de `bobimxl.anc_pack_rfc8331`, **contrôlé octet
pour octet** sur 66 cas — zéro paquet, UDW vide, `Data_Count` maximal, tous les champs à fond,
et plusieurs paquets (c'est là que l'alignement 32 bits se teste). Muté sur quatre points
distincts, les quatre échouent.

> Ce contrôle n'est pas du zèle : **un grain ANC mal sérialisé se lit sans erreur**. Le
> consommateur y voit « ANC count: 0 » ou des paquets fantaisistes, et rien ne signale qu'un
> octet était faux.

**Et l'état honnête : 281 grains ANC écrits, 0 paquet.** La carte déclare ne pas exiger de
trames 10 bits pour le VANC (`vanc_exige_10bit: false`) et nous capturons en v210 de toute
façon : la configuration est satisfaite. Deux causes restent **non départagées** :

1. la source de test n'embarque aucun ANC — le plus probable ;
2. l'ANC visé est en **HANC**, dont la configuration (`SupportsHANCInput`, filtres DID/SDID)
   **n'existe pas dans les en-têtes 12.2.2** que nous embarquons : ces attributs sont apparus
   après. Cela **corrige** la conclusion du §8 : la couverture des en-têtes était vérifiée pour
   les 19 symboles connus À CE MOMENT-LÀ, pas pour le HANC, que je ne connaissais pas encore.

Une source portant du timecode tranchera — c'est un item de recette, pas une mesure que la
machine peut faire seule.

---

***REMOVED******REMOVED*** 9 duodecies. Jalon 7 — sortie MXL → SDI, le bi-rôle est complet (2026-09-08)

`decklink_tx` lit un flux MXL planar 10 bits et l'émet en v210. Chaîne complète vérifiée :
capture sur un connecteur → flux MXL → réémission sur un autre connecteur de la même carte.

| | |
|---|---|
| Trames émises | **603 en 12 s** → 50,25/s, cadence pleine |
| Répétées (source sans neuf) | **6** sur 603, soit 1 % |
| En retard ou perdues | **0** |

> ★ **Ces 6 répétitions valent une mesure.** Ce n'est pas un défaut du code : c'est la dérive
> entre le quartz de la carte et l'horloge de la source, **sans référence de genlock branchée**.
> C'est la démonstration empirique de ce que `Ref In` apportera — et un contrôle bien plus
> parlant qu'une dérive en ppm : le chiffre doit tomber à **zéro** une fois la référence câblée.
> L'item `dl-genlock` de la recette gagne donc un critère net.

**Le TX est indépendant du producteur** (§ chantier `tx-output-must-be-independent-of-producer`
côté 2110) : il émet à la cadence de la CARTE, pas du flux. Source qui décroche → ré-émission de
la dernière trame connue, plutôt qu'une sortie figée ou trouée. Une sortie SDI qui s'arrête est
un incident d'antenne ; une trame répétée ne l'est pas. **Les répétitions sont comptées et
publiées** — sans ce compteur, une source morte donnerait une sortie parfaitement cadencée et
parfaitement fausse.

Deux refus délibérés : un grain dont `validSlices != totalSlices` n'est pas émis (pas de
demi-trame sur le fil), et le mode de sortie est **demandé** explicitement — « le premier
supporté » tombe sur du NTSC SD, comme le banc de sortie l'avait montré.

---

***REMOVED******REMOVED*** 9 terdecies. ANC tranché : le pilote ne livre pas le timecode en paquet (2026-09-08)

L'exploitant a précisé que **le timecode est le seul ANC rencontré en exploitation**. Il n'a pas
fallu attendre une autre source : celle de test en portait déjà. Trois portes essayées sur la
**même trame** :

| Chemin | Résultat |
|---|---|
| `GetTimecode(bmdTimecodeRP188Any)` — API dédiée | **`00:01:17:01`** |
| `GetPacketIterator` — itérateur de paquets ANC | **0 paquet** |
| `GetFirstPacketByID(0x60, 0x60)` — recherche directe de l'ATC | **absent** |

Sur cette carte et ce pilote, le timecode est **consommé par le pilote** et n'est exposé que par
l'API dédiée.

> ★ **Ce qui a tranché n'est pas une source différente, mais la comparaison de DEUX chemins sur
> la MÊME trame.** J'avais annoncé « la source n'embarque aucun ANC » comme l'hypothèse la plus
> probable — c'était faux, et j'allais faire câbler quelqu'un pour le vérifier. Une porte fermée
> ne prouve rien tant qu'on n'a pas essayé les autres.

Ça rend aussi **sans objet**, pour le timecode, la piste « il faudrait la configuration HANC des
en-têtes récents » : le problème n'était pas là.

**Suite** : pour le timecode, la porte est `GetTimecode`, et c'est à nous de **synthétiser** le
paquet ATC (SMPTE 12M-2, DID 0x60 / SDID 0x60) dans le flux ANC. L'empaqueteur RFC 8331 étant
vérifié octet pour octet, il ne reste que la charge utile à construire. Le chemin « itérateur »
est conservé pour les autres types d'ANC, s'il en vient.

---

***REMOVED******REMOVED*** 9 quaterdecies. Jalon 6 COMPLET — timecode synthétisé, aller-retour vérifié (2026-09-08)

Le pilote ne livrant pas l'ATC en paquet (§9 terdecies), on lit `GetTimecode` et on **synthétise**
le paquet — DID 0x60 / SDID 0x60, 16 UDW, ligne 9 — pour que le flux ANC porte la même chose
quelle que soit la carte. Il n'est ajouté **que si l'itérateur n'en a pas déjà livré un** : sur
une carte qui les expose, ajouter le nôtre le doublerait, et un consommateur lirait deux
timecodes.

**Aller-retour vérifié**, producteur et consommateur dans deux conteneurs :

```
grain 89443553498 → 00:00:19:13   paquets [(0x60, 0x60, 16 UDW)]
grain 89443553500 → 00:00:19:14
grain 89443553503 → 00:00:19:15
```

La charge utile est contrôlée contre `bobimxl.anc_atc_encode` sur 16 timecodes, dont **les deux
bugs historiques du 2026-08-07** (quartets bas → timecode figé ; deux quartets recollés →
dizaines d'images perdues). Les deux mutations correspondantes échouent.

> ★ **Une troisième mutation ne mordait pas** — élargir le masque des heures de `0x3` à `0x7`.
> Sur des timecodes valides, 23 h donne le même résultat des deux façons : mon jeu de cas ne
> pouvait pas la distinguer. J'ai ajouté des **valeurs hors bornes** plutôt que de conclure que
> le contrôle couvrait ce cas. Une mutation non attrapée est une information sur le TEST, pas un
> quitus pour le code.

> ⚠ **Bug trouvé en chemin, corrigé** : `tools/bancs/anc_tc_gen.py` était resté sur la
> disposition d'avant le correctif du 2026-08-07 — il écrivait dans les quartets bas alors que
> `decode_atc` lit les quartets hauts. **Tout timecode qu'il produisait était décodé
> `00:00:00:00` par notre propre moteur**, et son en-tête affirmait pourtant « miroir exact de
> decode_atc ». Corrigé et vérifié contre `bobimxl`. Un commentaire qui décrit une intention non
> tenue est pire que pas de commentaire.

---

***REMOVED******REMOVED*** 9 quindecies. Jalon 8 — la densité bute sur la BANDE PASSANTE, pas sur le CPU (2026-09-08)

Un seul connecteur porte du signal ; les quatre ont tout de même été exercés — une capture, plus
des sorties rejouant ce flux sur les autres connecteurs.

| Configuration | Débit v210 | Résultat |
|---|---|---|
| 1 capture + **2** sorties, 1080p50 | 829 Mo/s | **50,0 i/s, 0 perte** |
| 1 capture + **3** sorties, 1080p50 | 1 106 Mo/s | **44 i/s, ~95 % de trames en retard** |
| 1 capture + 3 sorties, **1080i50** | 553 Mo/s | **25,0 i/s, 0 perte** |

**Le CPU n'y est pour rien** : 15 % d'un cœur pour la capture (avec audio 16 canaux et ANC),
8 à 10 % par sortie — sur 88 cœurs, charge moyenne 0,64. Et **la pré-charge non plus** : 3 ou 10,
même effondrement.

> ★ **C'est la moitié du débit qui a tranché.** À cadence moitié, les quatre flux passent sans
> une perte ; à pleine cadence, trois sorties suffisent à faire tomber l'ensemble. Le facteur
> n'est donc ni le nombre de processus ni le calcul, mais les **octets par seconde**.
> Le `dmesg` de la carte le corrobore : `x4/5 GT/s` — un lien **PCIe gen2 ×4**.

**Plafond utile mesuré : entre 829 Mo/s (tient) et 1 106 Mo/s (ne tient pas).** Sur cette carte
dans ce slot, **une Duo 2 ne fait pas 4 × 1080p50 simultanés**.

Ce qu'il faut en retenir pour le dimensionnement : compter en **octets par seconde**, pas en
connecteurs. Une trame v210 1920×1080 fait 5 529 600 octets ; à 50 i/s c'est 276 Mo/s par flux.
Le profil de ressources du plugin devra porter cette contrainte, faute de quoi on vendra quatre
entrées HD sur une carte qui n'en soutient que trois.

> ⚠ **Limite de la mesure** : elle a été faite avec quatre PROCESSUS distincts, alors que le
> plugin n'en aura qu'un (règle « un conteneur par carte »). Pour la bande passante PCIe ça ne
> change rien — les octets traversent le même lien. Mais si un jour le plafond se révélait lié
> au nombre de contextes plutôt qu'au débit, il faudrait refaire la mesure en mono-processus.
> Le test à cadence moitié rend cette hypothèse peu probable, il ne l'élimine pas.

---

***REMOVED******REMOVED*** 9 sexdecies. Le côté SORTIE entre dans le plugin (2026-09-08)

Point 3 du jalon 9, celui qui ne dépendait de personne. `plugin.json` 0.2.0 déclare `outputs`, le
hook les normalise, le superviseur lance un `decklink_tx` par sortie. Vérifié en conteneur —
capture ET sortie dans le **même** conteneur, sur deux connecteurs de la même carte :

```
:8080   {"fps": 49.99, "frame_index": 673, "plugin_version": "0.2.0"}
entrée  mode=actif  fps=49.99  signal=true
sortie  mode=émet   fps=50.0   0 perdue, 5 répétées (dérive attendue sans genlock)
```

**Le câblage se déplie depuis les listes** (`from_list`), rien n'est codé en dur : une vidéo par
entrée, l'audio et l'ANC des entrées qui les activent, une vidéo consommée par sortie.

> ★ **Un détail qui aurait mordu tard.** `derive_wiring` prend le nom de flux TEL QUEL et ne sait
> pas suffixer : `<flux>_audio` et `<flux>_anc` n'étaient donc pas exprimables. Plutôt que
> d'ajouter un `shm_suffix` au cœur, le hook **matérialise** les listes `audio_ports` /
> `anc_ports`. Ça ne tient que parce que `deploy.py` réécrit le `deploy_config` **après** le
> hook — sinon le câblage aurait fonctionné au déploiement puis serait devenu vide, ce qui est
> le pire des deux mondes. Vérifié dans le code avant de s'y fier.

Sans cette matérialisation, les flux audio et ANC existeraient sur le bus **sans être câblables
depuis l'interface** — une capacité morte, exactement ce que la règle des macros interdit.

**Il reste donc, pour le jalon 9** : le passage des `/dev` (§9 nonies, bloqué sur une décision),
le `nav` au manifeste, l'image publiée, l'exposition aux macros, la vérification NMOS, et le
profil de ressources qui devra porter la contrainte de DÉBIT du §9 quindecies.

---

***REMOVED******REMOVED*** 9 septdecies. Macros et NMOS — deux points du jalon 9 (2026-09-08)

**NMOS : vérifié en exécutant, pas en lisant.** `services/nmos/mxl.py` dérive Sources, Flows et
Senders depuis `derive_wiring`, sans aucun test de type — le commentaire du fichier dit « pas une
seconde dérivation maison qui divergerait au premier plugin ajouté ». Passé sur une configuration
à deux entrées et une sortie, NMOS publierait **4 senders et 1 receiver**, sans cas particulier
nulle part. La décision du §7 tient.

**Macros : deux actions**, `output_source` (re-pointer une sortie sur un autre flux MXL — la
fonction de routage) et `output_enable`, avec la liste des sorties servie en direct.

> ★ **L'intention posée par macro n'est pas persistée, et l'état le DIT.** `flow` = la source
> configurée, `flow_actif` = celle qui tourne, `derive` = vrai quand elles diffèrent. Un
> redéploiement ramènera la configurée. C'est écrit dans la réponse de l'action et visible dans
> l'état — sans ces trois champs, une macro de routage serait défaite en silence par le prochain
> déploiement.

> ★ **Un diagnostic ne doit pas clignoter.** En exerçant l'action vers un flux inexistant, le TX
> part en boucle de relance et la première version repassait à « en cours » à chaque tentative :
> l'état OSCILLAIT entre le vrai motif et un libellé rassurant. Un exploitant regardant au
> mauvais moment aurait cru la sortie saine. Le motif tient désormais jusqu'à ce que des
> statistiques arrivent — vérifié sur trois relevés successifs, puis retour à « émet » dès
> qu'une source valide est remise.
>
> Le TX rend en outre un code de sortie **distinct** (7) pour « flux introuvable », la faute
> d'exploitation la plus probable (un nom mal saisi dans une macro), pour que le superviseur la
> **nomme** au lieu d'afficher un code sur une boucle que personne ne saurait interpréter.

**Reste au jalon 9** : le passage des `/dev` (bloqué sur décision), le `nav`, l'image publiée,
`meta.json`, et le profil de ressources portant la contrainte de débit du §9 quindecies.

---

***REMOVED******REMOVED*** 9 vicies. Où en est le chantier au soir du 2026-09-08 — et par quoi reprendre

**Acquis, vérifié sur le matériel** : le plugin se déploie depuis l'orchestrateur (capacité
`needs_decklink`, périphériques énumérés sur le nœud, image compute 0.35) ; capture 1080p50 →
grains MXL lus par un consommateur à 50,2/s ; audio 16 canaux à −11 dBFS ; timecode ATC
synthétisé et décodé en aller-retour ; sortie MXL → SDI à 50,25 i/s sans perte ; genlock établi
et tenu (179 relevés sur 179) sur les modes 50 Hz ; plafond de débit mesuré entre 829 Mo/s et
1 106 Mo/s.

**Bloqué faute de source** : plus aucun signal sur les quatre entrées depuis la fin d'après-midi,
et le câble ne revient que le lendemain.

***REMOVED******REMOVED******REMOVED*** Par quoi reprendre, dans l'ordre

1. **Vérifier l'écriture par CHAMP** (§9 sexdecies bis) : elle est écrite et compile, elle n'a
   **jamais tourné**. Deux points en particulier — la **parité** des champs (une erreur donne une
   image correcte à l'arrêt et saccadée en mouvement, donc qui passe une relecture rapide) et la
   cadence, qui doit être la cadence CHAMP.
2. **Reconstruire l'image compute** : la 0.35 sur dell-1 embarque les binaires d'AVANT le
   garde-fou de débordement, l'écriture par champ, la cadence canonique et les deux correctifs
   ANC. Le conteneur déployé (vmid 1105) tourne donc du code périmé.
3. **La dérive résiduelle du genlock** (§3) : trois mesures de 90 s ont donné +1,60, +0,29 et
   +0,43 ppm. Cette dispersion est celle de la méthode. Il faut des fenêtres de plusieurs minutes
   pour dire si la synchro maison et le PTP sont la même source — question posée à l'exploitant,
   sans réponse à ce jour.
4. **Le bouclage SDI** (item `dl-boucle`), toujours pas câblé : c'est le seul moyen d'obtenir le
   décalage « soumission → front de sortie », et la ligne correspondante du §3 est toujours vide.
5. **L'ANC autre que le timecode** : question ouverte à l'exploitant.

***REMOVED******REMOVED******REMOVED*** Ce que cette journée a coûté et appris

> ★★★ **Deux conventions non reprises du producteur existant ont coûté chacune plusieurs
> heures**, et les deux se ressemblent : `validSlices` (sans quoi les grains sont écrits et
> INVISIBLES) et le garde-fou de débordement (sans quoi une trame entière écrase l'anneau sur une
> source entrelacée, et le symptôme n'apparaît qu'à l'exécution suivante, ailleurs).
>
> **Écrire un second producteur natif, ce n'est pas écrire du code neuf : c'est hériter d'un
> contrat.** `mtl_rx.c` porte ces garde-fous avec leurs commentaires ; les relire ligne à ligne
> AVANT d'écrire aurait été moins cher que de les redécouvrir par les symptômes.

> ★★ **Un banc qui choisit sa cible tout seul mesure ce qu'il veut, pas ce qu'on lui demande.**
> Le banc de sortie prenait le premier mode déclaré et le premier sous-périphérique actif : trois
> essais ont conclu « pas asservi » en mesurant le mauvais mode au mauvais endroit. Et il lisait
> le verrou AVANT de démarrer la lecture, alors qu'il s'établit ensuite.

---

***REMOVED******REMOVED*** 9 unvicies. L'instrument ne mesurait pas la bonne chose (2026-09-08) — §3 à refaire

Deux fenêtres **indépendantes de 15 minutes**, sur le sous-périphérique asservi, en 1080p50 :

| | Fenêtre 1 | Fenêtre 2 |
|---|---|---|
| Référence présente et sortie asservie | **1791 / 1791** | **1791 / 1791** |
| Trames | 45 000, 0 en retard, 0 perdue | 45 000, 0 en retard, 0 perdue |
| **Dérive** | **+1,90 ppm** | **+1,60 ppm** |

**Reproductible : ≈ +1,75 ppm.** La dispersion des mesures de 90 s (+1,60 / +0,29 / +0,43) était
bien du bruit de méthode ; à dix fois la fenêtre, le chiffre se tient.

> ⚠⚠ **CONCLUSION RETIRÉE.** J'avais écrit ici que la mesure infirmait l'hypothèse du §3 — que la
> synchro maison et le PTP n'étaient pas la même source. **Ce n'est pas établi**, et l'annoncer
> était une faute de méthode. Deux raisons :
>
> 1. **dell-1 n'est pas disciplinée par PTP.** Elle tourne sous `chronyd`/NTP (`ptp4l` et
>    `phc2sys` inactifs ; `dl360-1` n'a même aucun des trois). La mesure comparait donc la carte à
>    une horloge NTP, pas à PTP. Le titre de cette section disait le contraire.
> 2. **Il manquait le CONTRÔLE.** Aucune mesure longue n'avait été prise SANS asservissement. Sans
>    elle, impossible de dire si ces 1,75 ppm viennent de la synchro maison ou simplement du
>    quartz de la carte — auquel cas le genlock n'influencerait pas cet horodatage du tout.
>
> Élément troublant qui commande la prudence : `chrony` rapporte l'oscillateur de dell-1 à
> **1,580 ppm lent**, et la carte dérive de **+1,75 ppm**. Deux chiffres trop proches pour être
> traités comme indépendants sans vérification.
>
> **CONTRÔLE PASSÉ, et il tranche** :
>
> | | Asservissement | Dérive sur 900 s |
> |---|---|---|
> | `525i59.94 NTSC` | **0 / 1791** | **+1,54 ppm** |
> | `1080p50` | **1791 / 1791** | +1,90 et +1,60 ppm |
>
> **Le même chiffre des deux côtés.** `GetFrameCompletionReferenceTimestamp` **ne suit pas la
> référence** : il donne l'écart entre l'horloge de la carte et celle du nœud, rien de plus.
>
> ★★★ **Ce n'est donc pas la conclusion qui était fausse, c'est l'INSTRUMENT.** Toute la méthode
> du §3 — comparer cet horodatage à `CLOCK_REALTIME` pour établir la relation entre synchro
> maison et horloge système — est invalide. Le §3 est à refaire avec une autre méthode.
>
> Et le contrôle a évité pire : `slip_h`, livré vingt minutes plus tôt dans l'état du plugin,
> était CALCULÉ depuis cette dérive. Il aurait annoncé un glissement toutes les 3,2 h là où il
> n'y en a peut-être aucun, et un seuil d'alerte s'en serait servi. Le glissement se **mesure**
> désormais sur les répétitions réellement survenues — l'écart de cadence réel entre la source et
> la sortie, genlock-sensible par construction.

**Ce que ça coûterait, SI la dérive s'avérait être celle de la référence :**

| Cadence | Glissement |
|---|---|
| 50 Hz | **une image toutes les 3,2 h** |
| 25 Hz | une image toutes les 6,3 h |

**Ce que ça ne remet PAS en cause** : le §4 avait décidé que le TX serait indépendant du
producteur et ré-émettrait la dernière trame connue plutôt que de trouer la sortie, **en comptant
ces répétitions**. C'est exactement le mécanisme qui absorbe cette dérive — et le compteur
`repeated` est l'instrument qui la rendra visible en exploitation, au lieu de la laisser passer
pour une instabilité de source. La décision tient donc, mais pour une raison qu'on n'avait pas
prévue.

**Réponse de l'exploitant (2026-09-08)** : le générateur de synchro peut normalement être asservi
au même GM. Et surtout — « il faut dans tous les cas **monitorer** ce qui se passe » : la dérive
et le glissement doivent être des grandeurs PUBLIÉES et alertables, pas des choses qu'on
découvre après coup.

> ★ **Et une question d'architecture qu'il pose, qui dépasse ce chantier** : sur une infra SANS
> 2110, la référence de la carte devrait devenir la référence du SYSTÈME. C'est faisable, et le
> relevé ci-dessus la rend plus urgente qu'il n'y paraît — **aucun nœud de la flotte ne fait
> tourner `ptp4l` ni `phc2sys`** ; dell-1 est sur NTP, dl360-1 sur rien. Or les index de grain
> MXL sont normativement dérivés du temps.
>
> Le mécanisme serait l'exact pendant de `phc2sys`, la carte jouant le rôle du PHC : mesurer
> l'écart (`decklink_out` le fait déjà) puis corriger la fréquence système (`ntp_adjtime`).
> Trois réserves : la synchro donne une **fréquence, pas une époque** (l'heure absolue doit venir
> d'ailleurs) ; l'**exclusivité** avec chronyd/phc2sys est obligatoire, deux disciplines sur la
> même horloge oscillent ; et la **résolution** de l'horodatage de la carte reste à caractériser.
> **À ouvrir comme chantier propre, pas à greffer ici.**

---

***REMOVED******REMOVED*** 9 duovicies. Le §3 mesuré autrement : moins de 1 ppm (2026-09-08, soir)

L'ancienne méthode étant morte (§9 unvicies), la relation d'horloge se mesure désormais par le
**comptage de trames capturées rapporté au temps du nœud** — genlock-sensible par construction,
puisque la capture est cadencée par le signal entrant.

| | |
|---|---|
| Fenêtre | **1277 s (21,3 min)**, 63 848 trames |
| Cadence mesurée | **50,000008 i/s** (nominal 50,000000) |
| Écart | **+0,155 ppm** |
| Résolution de la méthode | ±0,08 à ±0,16 ppm (gigue du callback) |

> **Conclusion honnête : l'écart est INFÉRIEUR À 1 ppm**, pas « de 0,155 ppm ». La mesure est au
> bord de sa propre résolution, et annoncer trois décimales serait reproduire l'erreur de
> l'après-midi.

**Deux instruments indépendants concordent** : le compteur de répétitions du TX donnait déjà
`slip_hours = 0` sur une fenêtre de 300 s. Aucun glissement observé, aucune dérive mesurable.
C'est un résultat CROISÉ, pas une mesure isolée — ce qui, vu la journée, n'est pas un luxe.

***REMOVED******REMOVED******REMOVED*** Ce que ça mesure exactement, et ce que ça ne mesure pas

★ **C'est la cadence de la SOURCE contre l'horloge du nœud.** Que cette source soit elle-même
asservie à la synchro maison n'est pas établi — c'est un générateur externe dont on ne sait pas
s'il est genlocké. Le résultat est donc pleinement valable pour ce qui détermine le glissement en
CAPTURE, et il ne dit rien de définitif sur la relation synchro-maison ↔ nœud.

**Pour cette dernière**, il faut appliquer la même méthode côté SORTIE : la sortie, elle, est
asservie à la référence (1791 relevés sur 1791). Compter ses trames contre le temps du nœud
donnerait directement l'écart cherché. `decklink_tx` publie déjà `frame_index` ; il lui manque
le `t_frame_ns` pris dans le callback, comme sur la capture. **C'est le prochain geste.**

***REMOVED******REMOVED******REMOVED*** Trois erreurs de mesure, et ce qui les a démasquées

| Erreur | Symptôme | Ce qui l'a démasquée |
|---|---|---|
| Horodatage de carte insensible au genlock | +1,75 ppm « du genlock » | **Contrôle** : mesurer sans genlock, même chiffre |
| Compte daté depuis l'orchestrateur | +674 ppm sur 120 s | **Variation de fenêtre** : +107 ppm sur 300 s — un artefact décroît, une dérive non |
| Horloge prise dans la boucle, pas le callback | 22 ppm de biais potentiel | **Calcul de résolution AVANT de mesurer** |

Aucune n'a été trouvée par le raisonnement. Toutes l'ont été en **faisant varier une condition**
— la présence du genlock, la longueur de la fenêtre, le lieu de la prise de temps.

---

***REMOVED******REMOVED*** 9 tervicies. §3 RÉPONDU : la synchro maison et l'horloge du nœud sont cohérentes (2026-09-08)

Mesure des DEUX cadences contre l'horloge du nœud, paire (compte, instant) prise dans le
callback, fenêtre de **1577 s (26 min)** :

| | Trames | Cadence | Écart |
|---|---|---|---|
| Capture (source) | 78 869 | 50,000007 i/s | **+0,135 ppm** |
| **Sortie (ASSERVIE à la référence)** | 78 869 | 50,000002 i/s | **+0,035 ppm** |

Résolution de la méthode sur cette fenêtre : ±0,06 à ±0,13 ppm. **Les deux chiffres sont dans le
bruit : l'écart est indiscernable de zéro.**

> ★★★ **L'hypothèse du §3 TIENT, et c'est mon instrument qui mentait.** L'exploitant avait affirmé
> le 2026-09-07 que la carte serait genlockée sur la même source que l'horloge système. Ma mesure
> de l'après-midi disait +1,75 ppm et j'en avais conclu le contraire — jusqu'à ce qu'un contrôle
> montre que l'horodatage employé ne voyait pas le genlock. La bonne méthode donne **+0,035 ppm
> sur la sortie asservie** : soit une image de glissement toutes les **159 heures**, autant dire
> aucune.

**Trois instruments indépendants concordent** — et après la journée qu'il a fallu, ce n'est pas
un luxe :

1. la cadence de SORTIE contre l'horloge du nœud : +0,035 ppm ;
2. la cadence de CAPTURE : +0,135 ppm, et **exactement le même nombre de trames** que la sortie
   sur la fenêtre (78 869 des deux côtés) — la source est donc elle aussi asservie à la même
   synchro, ce qu'on ne savait pas ;
3. le compteur de répétitions du TX : `slip_hours = 0`, les 23 répétitions datant de l'amorçage.

**Conséquence pour le produit** : la ré-émission de trame du TX (§4) reste le bon mécanisme, mais
elle n'aura pratiquement jamais à servir sur cette installation. Et les grandeurs publiées
(`genlock`, `slip_hours`, `repeated`) sont ce qui préviendra le jour où la synchro décrochera.

---

***REMOVED******REMOVED*** 9 quatervicies. `card_vs_node_ppm` mesurait le mauvais objet (2026-09-08)

La grandeur publiée par le TX était dérivée de `GetFrameCompletionReferenceTimestamp`. Le contrôle
du §9 tervicies a montré que **cet horodatage ne suit pas la référence** : asservi ou non, il donne
le même +1,5 à +1,9 ppm. On publiait donc du bruit sous un nom de mesure — et un seuil d'alerte
posé dessus aurait crié pour rien, ou pire, se serait tu pendant un vrai décrochage.

Elle se calcule désormais sur la **cadence réellement émise** : le rythme d'émission est imposé par
l'horloge de sortie de la carte, donc asservie à la référence quand il y en a une. Compte de trames
et instant sont pris **ensemble, dans le callback**.

Vérifié sur l'image compute **0.41**, conteneur 1105 :

```
card_vs_node_ppm = 0.09   (fenêtre 300 s) · genlock = true · slip_hours = 0
```

à comparer aux +0,035 et +0,094 ppm calculés hors ligne sur 1577 s et 1830 s. Les trois concordent.

---

***REMOVED******REMOVED*** 9 quinvicies. La fenêtre de publication, calibrée par la mesure (2026-09-09)

Le §9 quatervicies avait réparé *ce que* `card_vs_node_ppm` mesure. Restait *sur combien de temps*.
La fenêtre de 300 s était héritée de l'ancienne méthode. Neuf fenêtres consécutives, chaîne saine :

```
−1,30 · +1,46 · −0,62 · +1,23 · −0,60 · −0,04 · −0,84 · +0,89 · −0,57 ppm
moyenne −0,04 · écart-type 0,94 · étendue 2,8 ppm
```

pour une chaîne dont la fenêtre longue (7443 s) donne **−0,06 ppm**. Autrement dit : la valeur
publiée était du **bruit d'amplitude 1,5 ppm autour de zéro**. Un seuil d'alerte à 1 ppm aurait crié
plusieurs fois par heure sans qu'il ne se passe rien — j'aurais remplacé un indicateur faux par un
indicateur bruyant, ce qui n'est pas un progrès.

**La cause est la gigue d'ordonnancement du callback** : ~300 µs entre l'émission réelle et
`clock_gettime`. Elle borne la résolution à `gigue / fenêtre` :

| Fenêtre | Résolution |
|---|---|
| 300 s | ±1,0 ppm |
| 600 s | ±0,50 ppm |
| 1200 s | ±0,25 ppm |
| **1800 s** | **±0,17 ppm** |
| 3600 s | ±0,08 ppm |

Retenu : **1800 s** (image compute 0.42). Trente minutes avant la première valeur, `null` d'ici là.

> ★ **Une fenêtre de mesure se calibre, elle ne se choisit pas.** C'est la troisième fois sur ce
> chantier qu'une fenêtre trop courte produit un chiffre plausible et faux — après les 90 s du §9
> et les 100 ms de l'audio au §6. Le geste qui tranche est toujours le même et il est bon marché :
> répéter la mesure et regarder la DISPERSION. Elle dit la résolution ; la moyenne ne la dit pas.

---

***REMOVED******REMOVED*** 9 sexvicies. La première fenêtre mentait, pas le modèle (2026-09-09)

La fenêtre de 1800 s à peine posée, sa **première** valeur est tombée à **−1,12 ppm**. J'y ai lu la
réfutation du modèle de gigue du §9 quinvicies et je l'ai annoncé comme tel. C'était faux : les
fenêtres SUIVANTES, sur la même chaîne et sans rien changer, ont donné **0,00** puis **+0,11 ppm**.

| Contrôle indépendant | Fenêtre | Écart |
|---|---|---|
| paire (compte, instant) | 1200 s | −0,027 ppm |
| " | 2401 s | +0,072 ppm |
| " | 3602 s | +0,039 ppm |

**Le modèle tient, et 1800 s est la bonne fenêtre.** Ce qui ne tenait pas, c'est l'**ancre** : elle
était prise sur la toute première trame émise, donc pendant la pré-charge — sortie déjà cadencée,
source pas encore là, horodatages irréguliers. Un transitoire de 2 ms sur l'ancre suffit à produire
−1,12 ppm sur 1800 s.

Deux défauts, une seule cause, corrigés ensemble (image compute **0.43**) :

1. l'ancre saute les **250 premières trames** (5 s) ;
2. le compteur de répétitions est ancré **en même temps** que la fenêtre. Sans quoi la première
   fenêtre comptait les 23 répétitions d'amorçage et publiait `slip_hours = 0,02` — « un glissement
   toutes les 72 secondes » sur une chaîne saine. Le commentaire du calcul affirmait que l'amorçage
   était « derrière nous puisque la fenêtre fait 300 s » : vrai de toutes les fenêtres **sauf de la
   première**, et c'est celle-là que l'exploitant regarde en déployant.

> ★ **Une chaîne de mesure ne doit pas s'ancrer sur son propre démarrage.** Le régime transitoire
> ressemble à un signal et se publie comme lui. Trois indicateurs de ce chantier en sont morts —
> `slip_h`, `slip_hours`, `card_vs_node_ppm` — et à chaque fois le premier chiffre publié était le
> plus faux, alors que c'est celui qu'on regarde. Aussi : j'ai encore une fois conclu sur UN relevé.
> Le second l'a démenti quarante minutes plus tard.

---

***REMOVED******REMOVED*** 9 septvicies. Le glissement est un SOLDE (2026-09-09)

Le correctif d'ancre de 0.43 a d'abord produit **pire** que le défaut : écrite
`if (!ancre && emises > 250) … else …`, la garde envoyait les 250 premières trames dans la branche
de **calcul**, ancre à zéro — `dt` valait l'époque UNIX entière, donc dépassait la fenêtre, donc on
publiait **−1 000 000 ppm**, une demi-heure durant. Les deux conditions n'étaient pas de même
nature : « pas encore d'ancre » choisit la branche, « assez de trames » n'est qu'un délai
**à l'intérieur**. Corrigé en 0.44, avec un refus de publier toute valeur absurde — un `null` se lit
comme un blanc, `−1 000 000` se lit comme une mesure. Première fenêtre de 0.44 : **+0,03 ppm**.

Restait `slip_hours`, déduit du compteur de **répétitions**. Il se trompait d'un facteur 240 :

| Sur 2101 s | |
|---|---|
| Trames émises | 105 052 |
| Répétitions | 5 → « un glissement toutes les 420 s » |
| Équivalent en dérive | **47,6 ppm** |
| Cadence RÉELLEMENT mesurée | **+0,20 ppm** |

**Une répétition est presque toujours de la gigue de phase** : la source n'avait rien de neuf à cet
instant précis, et elle livrera deux grains au suivant. Répétitions et sauts se compensent ; seul
leur **solde** est du glissement. Il se mesure donc (0.45) comme l'écart entre l'avance de l'index
de grain **source** et le nombre de trames **émises** sur la fenêtre.

**Vérifié sur 0.45**, première fenêtre close (1800 s), source vivante :

```
card_vs_node_ppm = −0,01   ·   slip_hours = 0   ·   repeated : 24 → 27
```

Trois répétitions sur la fenêtre, **et un glissement net nul** : elles étaient bien de la gigue,
intégralement compensées. L'ancienne grandeur aurait annoncé « un glissement toutes les 10 minutes ».

> ★ **Trois indicateurs d'horloge, trois erreurs de nature différente, un même symptôme.** Le
> premier mesurait le mauvais objet (§9 quatervicies), le deuxième sur une fenêtre trop courte
> (§9 quinvicies), le troisième comptait des événements qui se compensent. Aucun ne se voyait sans
> une grandeur de référence à confronter — et à chaque fois, c'est la CADENCE mesurée qui a servi
> d'étalon. Un compteur qui n'a rien contre quoi se vérifier n'est pas un instrument.

---

***REMOVED******REMOVED*** 9 octovicies. Le bouclage n'existait pas — et quatre heures pour le voir (2026-09-09)

Le testeur annonce un câble BNC « en boucle fermée » entre deux prises. Toute la matinée y passe :
émissions sur chaque sous-périphérique, entrées réellement ouvertes, tableaux de verrouillage —
et **rien n'arrive jamais nulle part**. La réponse tombe quand il débranche ce câble pour y mettre
son écran : **le sous-périphérique 3 perd sa source**. Ce câble n'était pas une boucle, **c'est lui
qui amenait la source dans la carte**. Aucune sortie de cette carte n'est câblée vers aucune de ses
entrées, et il n'y a jamais eu de bouclage à mesurer.

Trois erreurs de méthode s'y sont ajoutées, toutes les miennes :

| Ce que j'ai fait | Ce que ça a produit |
|---|---|
| Annoncé une mire lancée en arrière-plan sans vérifier qu'elle vivait | Le processus mourait avec le shell distant. Le testeur pouvait faire tout le tour des prises pour rien |
| Émis sur DEUX sous-périphériques à la fois | En half-duplex, émettre **désactive l'entrée** : je détruisais ce que je cherchais |
| Conclu d'un tableau dont deux lignes sur quatre n'avaient jamais émis | `EnableVideoOutput a échoué`, vu seulement en relisant les journaux après coup |

> ★★ **L'ORCHESTRATEUR RELANCE LE SCRIPT 4 SECONDES APRÈS UN ARRÊT.** Mesuré : `running=False` à
> +2 s, `running=True` à +4 s. Toutes mes tentatives de libérer une prise en arrêtant le conteneur
> échouaient donc pour cette seule raison — la carte n'a jamais été libre une seule fois. J'avais
> d'abord accusé des processus orphelins ; en regardant leur **état** et non leur nombre, ils
> étaient **zombies**, et un zombie ne tient aucune ressource. Libérer le matériel d'un conteneur
> demande une intention **persistée**, pas un `POST /stop`.

**Un défaut réel en est quand même sorti** (plugin **0.5.1**) : les enfants n'étaient jamais
fauchés — les superviseurs sont des threads démons, tués avant leur `terminate()`, et le chemin
d'arrêt normal ne faisait suivre aucun `wait()`. Treize zombies accumulés pour un processus vivant.

**Et un item de recette s'est validé tout seul** : `dl-debranche`. Pendant les cinquante minutes
sans câble, **85 631 trames « sans source » comptées, aucune écrite** sur le bus — ni image
fabriquée, ni image figée — et la capture est repartie **seule** au rebranchement, sans
redéploiement. Personne ne l'avait prévu ; c'est le meilleur test qu'on ait eu de la journée.

---

***REMOVED******REMOVED*** 10. Ce qui reste ouvert

Les quatre points laissés en suspens à l'ouverture ont été tranchés le 2026-09-07 : surface NMOS
(§7), v210 réglable (§6), pré-charge mesurée et non choisie (§4), pilote installé à la main (§5).

Restent, et ce sont des mesures ou des constats, pas des décisions :

- ~~les deux constantes d'horloge du §3~~ — **répondu le 2026-09-08** (§9 tervicies) : sortie
  +0,035 ppm, capture +0,135 ppm, tout sous la dispersion de la méthode. La synchro maison et
  l'horloge du nœud sont cohérentes, et la source est asservie à la même référence que la sortie.
  Reste, mais sans enjeu d'exploitation : l'HORODATAGE d'entrée est-il verrouillé sur l'horloge
  système ? On sait maintenant que la CADENCE l'est, ce qui était la vraie question ; et
  l'expérience du §9 quatervicies invite à se méfier des horodatages de la carte ;
- le tableau de pré-charge du §4 ;
- ~~Secure Boot et les slots PCIe sur la flotte~~ — **relevé fait le 2026-09-07**, cf. §5 ;
- ~~où vivent les en-têtes du SDK~~ — **réglé** (§8) ;
- ~~la question du §8 bis~~ — **tranchée le 2026-09-07 : option A**, le SDI entre et sort par le
  bus MXL. Le profil de carte, lui, reste un choix offert à l'exploitant (§2).
