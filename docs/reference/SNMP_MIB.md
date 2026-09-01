# BOBI-STUDIO-MIB — référence d'intégration SNMP

> **Ce document FAIT FOI** (cf. `CLAUDE.md`, rangement de la documentation). Il décrit ce qu'un
> intégrateur doit savoir pour brancher Bobi.Studio sur un système de supervision.
> Le chantier et ses décisions sont dans `docs/chantiers/SNMP.md` ; ce fichier-ci est la
> référence stable.

Fichier livrable : **`services/snmp/BOBI-STUDIO-MIB.mib`** (SMIv2), téléchargeable depuis
**Réglages → Protocoles → SNMP → Télécharger la MIB** (`GET /api/snmp/mib`).

---

## 1. En une page

| | |
|---|---|
| Racine | `1.3.6.1.4.1.66633.1` — PEN attribué le 2026-08-26, cf. §2 |
| Versions acceptées | **SNMPv3 uniquement.** Une requête v1/v2c reste sans réponse |
| Niveau de sécurité | **`authPriv` uniquement.** Ni `noAuthNoPriv`, ni `authNoPriv` |
| Algorithmes | SHA-2 (224/256/384/512) et SHA-1 ; AES-128/192/256. **Ni MD5, ni DES** |
| Accès | **Lecture seule.** Aucun SET instrumenté ; un SET reçoit `notWritable` |
| Portée de la vue | Notre sous-arbre seulement — ni MIB-II, ni HOST-RESOURCES, ni les tables USM de la pile |
| Qui répond | Le **contrôleur ACTIF** de la paire HA, port UDP 161 par défaut |
| Notifications | **Inform** par défaut (acquitté), trap en option — cf. §5 |

Un seul point d'interrogation pour tout le cluster : la donnée de tous les nœuds est déjà agrégée
en mémoire du contrôleur. Il n'y a pas d'agent par machine à provisionner.

---

## 2. Le PEN, et la reprise que son attribution impose

Le Private Enterprise Number de **BOBI** a été **attribué par l'IANA le 2026-08-26 : `66633`**
(contact Cyril Mazouer). L'arbre est donc publié sous `1.3.6.1.4.1.66633`, et c'est définitif.

Avant cette date, l'arbre était publié sous l'arc d'attente **99999** — un espace réservé, et
surtout **pas l'arc d'un tiers** : un OID qui collisionne dans l'NMS d'un diffuseur est un
incident client. La bascule a coûté **deux littéraux**, exactement ceux que la conception avait
isolés d'avance :

1. la constante `PEN` de `services/snmp/mib.py` (et `PEN_ASSIGNE = True`) — l'unique littéral
   d'OID racine du **code**, dont tout l'arbre est dérivé et dont le canal syslog tire aussi son
   SD-ID `bobi@66633`, par import et non par recopie ;
2. son jumeau dans le fichier `.mib`, `bobi OBJECT IDENTIFIER ::= { enterprises 66633 }` — le
   `.mib` et le code ne se compilant pas ensemble, c'est `mib.verifier_fichier_mib()` qui les
   confronte.

Aucun objet n'a été ajouté, retiré ni renommé. Mais un troisième effet suit, qui n'est pas une
ligne à changer : **le `snmpEngineID` de l'agent est régénéré**, puisque la RFC 3411 §5 y
embarque le PEN.

⚠ **Un NMS provisionné avant le 2026-08-26 est à reprendre.** Ce n'est pas une panne, c'est la
conséquence attendue de la bascule, et il y a trois gestes, pas un :

1. **recharger la MIB** — tous les OID ont changé, et la nouvelle `REVISION` du fichier le dit ;
2. **re-provisionner les identifiants USM** — ils sont *localisés* sur l'engine ID, qui a changé ;
3. **reprendre les règles de filtre syslog** qui visaient `bobi@0` : elles ne verront plus rien
   passer. C'est la plus sournoise des trois — un filtre qui ne remonte rien ressemble à du calme.

Le produit ne bascule pas l'engine ID en silence : il détecte que celui qui est stocké n'est plus
compatible avec le PEN courant, le régénère, le persiste et **pose une alerte** disant que les
identifiants USM sont à re-provisionner. Vérifié le 2026-08-26 en base isolée, pas seulement lu :
`8001869f…` (arc 99999) → `80010449…` (arc 66633), avec l'alerte.

---

## 3. Ce qui répond aujourd'hui

La MIB décrit l'arbre **complet**. Tout n'est pas encore instrumenté, et le fichier le dit à deux
endroits : la conformance `bsImplementedCompliance`, et la `DESCRIPTION` de chaque table non
peuplée.

| Sous-arbre | OID relatif | État |
|---|---|---|
| `bsNotifications` | `.0` | **répond** (sauf `bsAlarmClear` et `bsAgentStart`, cf. §5) |
| `bsSystem` | `.1` | **répond** — 10 scalaires du contrôleur |
| `bsNodeTable` | `.2` | **répond** — 18 colonnes, index `bsNodeIndex` |
| `bsSensorTable` | `.3` | définie, **vide** |
| `bsIfTable` | `.4` | définie, **vide** |
| `bsPtpTable` | `.5` | **répond** — index `bsNodeIndex.bsPtpDomain` |
| `bsFlowTable` | `.6` | **répond** — index `moteur.sens.essence.rang.sous-rang` |
| `bsContainerTable` | `.7` | **répond** — index `bsContainerIndex` |
| `bsAlarmTable` | `.8` | **répond** — alarmes actives, resynchronisation |
| `bsConformance` | `.9` | — |
| `bsAlarmInfo` | `.10` | **répond** — varbinds des notifications (`accessible-for-notify`) |

Ne restent non peuplées que `bsSensorTable` (capteurs matériels) et `bsIfTable` (interfaces et
modules optiques) : elles demandent des collectes que le produit ne fait pas encore, et non du
câblage. La conformance `bsImplementedCompliance` du fichier `.mib` dit exactement la même chose.

**Aucune de ces tables ne déclenche de collecte.** Elles lisent les caches que la boucle de
surveillance remplit toutes les ~5 s. Un `snmpwalk` complet ne provoque aucune E/S réseau — c'est
une propriété de sécurité autant que de performance : sans elle, n'importe quel NMS pourrait
saturer l'orchestrateur en interrogeant plus vite.

Publier l'arbre entier plutôt qu'un arbre tronqué est un choix : l'intégrateur voit où il va, et
les OIDs ne bougeront plus quand les tables seront peuplées.

---

## 4. Cinq pièges d'exploitation

Ils sont écrits dans les `DESCRIPTION` de la MIB elle-même — c'est l'intégrateur du client qui
écrira les règles, pas nous.

**1. Ne jamais alarmer sur `bsPtpLocked`.** C'est le verrou *strict* du servo. Sur les cartes
pilotées en kernel-bypass, il reste souvent faux alors que la synchronisation est bonne et que le
système produit normalement. Une règle sur cet objet sonnerait en permanence sur une installation
saine. **L'objet d'alarme est `bsPtpSynced`** ; `bsPtpLocked` est un objet de diagnostic.

**2. `bsPtpEngineSessions = 0` doit INHIBER l'alarme PTP.** Un moteur sans session n'a pas
d'horloge du tout — ce n'est pas une horloge qui perd sa référence.

**3. Toujours lire la colonne `…Age`.** Chaque table en porte une : secondes depuis le dernier
échantillon. Sans elle, une panne de *collecte* se lit exactement comme une panne de
*production*. `bsIfSfpAge` est structurellement plus grand que les autres (la lecture d'un module
optique est lente et faite à cadence réduite) : en tenir compte dans les seuils optiques.

⚠ **`bsNodeSampleAge = 4294967295` est une sentinelle : « jamais échantillonné ».** La ligne d'un
nœud existe dès son enrôlement, avant tout relevé. Y lire un âge de 0 ferait croire à une mesure
fraîche annonçant un nœud injoignable, alors que la vérité est qu'il n'y a *aucune* mesure —
`Gauge32` n'ayant pas de valeur nulle, son maximum sert de sentinelle.

**4. Router sur `bsAlarmMsgKey`, pas sur `bsAlarmText`.** Le texte est la forme canonique
française, translittérée en ASCII — `DisplayString` est de l'ASCII NVT (RFC 2579), pas de
l'UTF-8. Une reformulation du libellé ne doit pas casser une règle NMS ; la clé, elle, est stable.

**4 bis. `bsContainerCpuPerMille` dépasse 1000, et c'est normal** : c'est du pour mille d'*un*
cœur, et un mur d'images en occupe une dizaine. La valeur n'est volontairement pas bornée ; la
lire avec `bsContainerCpuCount`, sans quoi elle n'a pas d'unité exploitable.

**5. Corréler sur `bsAlarmRoleNum` / `bsFlowRoleNum` / `bsContainerRoleNum`,
pas sur les identifiants de conteneur.** Le numéro d'EMPLACEMENT est l'identité *fonctionnelle* :
il survit au remplacement du conteneur et n'est jamais réattribué. `bsAlarmContainerVmid` est un
handle interne, réattribuable — une règle accrochée dessus finit par désigner autre chose.

---

## 5. Notifications

Neuf notifications typées + `bsGenericAlarm`, toutes portant **le même jeu de varbinds** :
`bsAlarmKind`, `bsAlarmSeverity`, `bsAlarmNodeIndex`, `bsAlarmContainerVmid`, `bsAlarmRoleNum`,
`bsAlarmSince`, `bsAlarmText`, `bsAlarmMsgKey`. Une règle écrite pour l'une se transpose aux
autres.

| OID relatif | Notification | Déclenchée par |
|---|---|---|
| `.0.1` | `bsFlowStall` | flux ST 2110 absent ou figé (RX ou TX) |
| `.0.2` | `bsSignalFault` | noir, image gelée, silence |
| `.0.3` | `bsFpsFault` | cadence non tenue |
| `.0.4` | `bsContainerFault` | redémarrages en boucle, quarantaine, agent injoignable |
| `.0.5` | `bsNodeFault` | nœud injoignable, redémarrage, préparation d'hôte incorrecte |
| `.0.6` | `bsNetFault` | lien, adressage, port muet |
| `.0.7` | `bsDiskFault` | remplissage d'un système de fichiers |
| `.0.8` | `bsPtpFault` | synchronisation (voir pièges 1 et 2) |
| `.0.9` | `bsResourceFault` | CPU, mémoire, bande passante mémoire, GPU |
| `.0.10` | `bsGenericAlarm` | tout le reste — la nature est dans `bsAlarmKind` |
| `.0.11` | `bsAlarmClear` | **définie, PAS ENCORE ÉMISE** — voir ci-dessous |
| `.0.12` | `bsAgentStart` | **définie, PAS ENCORE ÉMISE** |

### Inform ou trap

**Inform par défaut, et c'est un choix de fiabilité.** Un trap est un datagramme non acquitté :
sa perte est silencieuse. Un inform est acquitté ; s'il ne l'est pas, l'échec est compté, affiché
dans Réglages → Alertes, et remonté localement.

⚠ **Si vous choisissez le mode trap**, c'est l'**émetteur** qui fait autorité au sens USM : le
récepteur doit connaître **notre `snmpEngineID`**, sinon il jette le trap **sans un mot** — et
comme un trap n'est pas acquitté, l'émission se déclare réussie de notre côté. L'interface
affiche l'engine ID à provisionner dès que « trap » est sélectionné. Avec net-snmp :

```
createUser -e 0x<engineID> <utilisateur> SHA-256 "<passphrase-auth>" AES "<passphrase-priv>"
authUser log,execute <utilisateur> authPriv
```

En mode **inform**, c'est le récepteur qui fait autorité : un `createUser` sans `-e` suffit,
la découverte d'engine ID est automatique.

### Ce que `bsAlarmClear` ne fait pas encore

Le produit émet bien des événements de résolution (« flux rétabli après 4 min de panne »), mais
**rien ne les distingue structurellement** d'une alarme ordinaire : ni la nature, ni le niveau,
ni une colonne. Les reconnaître demanderait d'interpréter leur libellé — ce qui manquerait en
silence tout libellé ajouté ensuite.

**Conséquence pour l'exploitation : une alarme résolue reste ouverte côté NMS.** La parade est
`bsAlarmTable` (walk de resynchronisation) — qui n'est pas encore peuplée non plus. En attendant,
la fermeture d'alarme est à faire côté NMS sur expiration, ou manuellement.

C'est une limite connue, écrite ici plutôt que découverte en recette.

---

## 6. Exemple d'interrogation

```
snmpwalk -v3 -l authPriv -u <utilisateur> \
         -a SHA-256 -A <passphrase-auth> \
         -x AES -X <passphrase-priv> \
         <controleur>:161 1.3.6.1.4.1.66633.1
```

La ligne exacte, pré-remplie avec la configuration courante, est affichée dans
Réglages → Protocoles → SNMP.

---

## 7. Matrice de flux

| Source | Destination | Proto | Port | Sens | Objet | Chiffré |
|---|---|---|---|---|---|---|
| NMS | Contrôleur (VIP) | UDP | 161 | NMS → Bobi | Interrogation SNMPv3 | oui (AES) |
| Contrôleur (VIP) | NMS | UDP | 162 | Bobi → NMS | Notifications SNMPv3 | oui (AES) |

Deux flux, un seul point d'entrée, une seule identité USM à provisionner. Rien à ouvrir vers les
nœuds : ils ne parlent pas SNMP.

---

## 8. Concordance code ↔ MIB

Le fichier `.mib` et le code de l'agent **ne se compilent pas ensemble** : rien n'empêcherait
structurellement l'un de dériver de l'autre, et la divergence serait invisible chez nous — très
visible chez le client, sous la forme d'objets qui ne répondent plus.

Deux garde-fous, tous deux exécutés **au démarrage de l'agent** (ils journalisent, ils ne bloquent
pas : un écart de documentation ne doit pas priver l'exploitant de sa supervision) :

- `mib.verifier_fichier_mib()` — confronte chaque OID publié dans le `.mib` à la constante
  correspondante du code ;
- `mib.verifier_vocabulaire()` — vérifie que l'énumération `BsAlarmNature` couvre encore le
  vocabulaire d'alertes réel du produit.

Le bouton **« Vérifier la concordance »** de l'onglet SNMP pose la même question à la demande
(`GET /api/snmp/mib/check`).

**Validation du fichier** : `smilint -s -l 6` → 0 message, et compilation `pysmi` réussie.

---

## 9. Haute disponibilité

L'agent tourne sur le **contrôleur actif seulement**. Le rôle ne bascule **jamais à chaud** :
`promote()` et `demote()` redémarrent le service, et `main.py` ne démarre l'agent que sous
`ha.is_active()`. Deux agents ne peuvent donc pas répondre en même temps sur la même adresse.

L'**engine ID** est un réglage en base, donc **répliqué vers le standby** : il suit la VIP, pas la
machine. Après un basculement, l'agent se présente avec le même `snmpEngineID` — les identifiants
USM localisés côté NMS restent valides, et une supervision par informs ne voit qu'une coupure.

Si l'agent est lié à l'adresse de la **VIP** plutôt qu'à `0.0.0.0`, il **patiente jusqu'à 120 s**
que l'adresse apparaisse avant d'abandonner : au démarrage comme après un basculement, keepalived
peut la poser après le service.

> ⚠ Ce scénario **n'a jamais été exécuté sur une vraie paire**. Il est décrit ici tel qu'il découle
> du code, et il figure au plan de recette (`/tests`, item « Basculement du contrôleur »).

---

## 10. Coût du sondage

Mesuré, pas supposé : **13 walks complets consécutifs (~4 900 OID) ne déclenchent aucune connexion
réseau, aucune requête HTTP et aucun sous-processus** côté orchestrateur. L'agent lit les caches
que la boucle de surveillance remplit déjà toutes les ~5 s.

C'est une propriété de **sécurité** autant que de performance : sans elle, n'importe quel NMS
pourrait saturer l'orchestrateur en interrogeant plus vite.
