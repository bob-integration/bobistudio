# Mises à jour de l'orchestrateur

Journal des évolutions de l'orchestrateur, du plus récent au plus ancien.
Le contenu est rendu dynamiquement sur la page Aide.

---

## L'installation échouait dès qu'on choisissait une version — 2026-09-02

Signalé par un installateur le jour même. Le script d'installation propose une liste de versions ;
en choisir une le faisait échouer aussitôt après avoir récupéré le produit, sur son premier
composant obligatoire.

La cause : il cherchait ce composant **à la version du produit**. Or un service vit sur ses
propres numéros — il n'existe aucun « v0.9.3 » dans le dépôt du service NMOS. Tant qu'on
installait la branche de développement, présente partout, rien ne se voyait ; le défaut est
apparu avec le choix de version, ajouté la veille.

Le script résout maintenant, pour chaque composant, le commit exact que la version du produit
épingle — donc celui contre lequel elle a été construite. Si cette résolution est impossible
(réseau, quota d'interrogation épuisé), il retombe sur la branche principale du composant plutôt
que d'abandonner.

⚠ Le message d'erreur, lui, était clair et bien écrit : il expliquait que le composant n'est pas
optionnel et invitait à vérifier l'accès au dépôt. Il désignait la mauvaise cause, avec assurance.
Un bon message sur un mauvais diagnostic coûte plus cher que pas de message du tout — celui-ci a
envoyé chercher du côté des droits, là où il n'y avait rien.

---

## La mise à jour depuis GitHub, en entier — 2026-09-02

La 0.9.2 savait REGARDER : elle lisait les releases publiées et vous disait qu'une version
existait. Elle ne savait pas la prendre. C'était la moitié de ce qui était annoncé.

Une instance tire désormais la dernière release, vérifie son empreinte, sauvegarde son code,
applique, et relance. La sauvegarde permet un retour arrière si le redémarrage se passe mal.

**Sans empreinte, on refuse.** L'archive de source que GitHub sert d'office n'embarque ni
l'installeur ni de `SHA256SUMS`. S'en contenter reviendrait à exécuter du code non vérifié sur une
machine qui tourne en root — ce que le script d'installation s'interdit précisément. Une release
qui ne porte pas son paquet et son empreinte est donc signalée comme non applicable, plutôt que
de proposer un bouton qui échouerait.

**L'application reste un geste, pas un automatisme.** Elle redémarre le service : sur une
installation d'antenne, le moment se choisit. C'est la détection qui est automatique.

**Un catalogue qui paraissait vide.** Le quota d'interrogation de GitHub est de 60 requêtes par
heure sans compte, et lister les composants en coûte une par dépôt. Une fois épuisé, chaque
composant s'affichait « aucune release publiée » — alors que tout l'était. « Je n'ai pas pu
demander » et « il n'y en a pas » se distinguent maintenant, et le motif s'affiche.

---

## Les emplacements se créaient tout seuls, 274 sur 282 pour rien — 2026-08-30

Un emplacement est une position de production — « MULTIVIEW RÉGIE 1 » — donc une décision humaine.
Le produit en semait pourtant un automatiquement au premier déploiement de chaque conteneur, avec
le hostname pour libellé, c'est-à-dire exactement ce qu'un emplacement ne doit pas être.

Le compte a tranché : 282 emplacements, 8 servis, et 2 des survivants jamais renommés. 173 clés
commençaient par `bobi_fab_` : le tissu compositeur se re-partage à chaque redémarrage, chaque
shard éphémère semait son emplacement, et comme un numéro n'est jamais réattribué la table ne
pouvait que grossir. Un mécanisme dont 97 % de la production est à jeter ne rend pas le service
qu'il prétend rendre.

Le semage est supprimé. Les emplacements se créent depuis Réglages → Ember+, où un indice signale
les conteneurs déployés qui n'en ont pas encore : sans amorçage automatique, c'était la seule chose
qui rendait la création découvrable. Et un bouton supprime en bloc les emplacements hors ligne, en
prévenant que les numéros ne sont pas réattribués — un pupitre configuré sur l'un d'eux ne le
retrouvera pas.

---

## L'accueil montrait les dernières erreurs, pas celles en cours — 2026-08-30

Le bandeau d'alertes affichait les cinq plus saillantes parmi les mille dernières, gravité d'abord.
Une erreur close depuis trois jours y restait donc en tête indéfiniment, juste sous un en-tête
« SYSTÈME OK ». Mesuré le jour du correctif : cinq erreurs rouges affichées alors qu'aucun épisode
de niveau erreur n'était actif. Un opérateur qui arrive le matin ne pouvait pas distinguer une
panne vivante d'une cicatrice, et finissait par ne plus lire la zone.

L'accueil ne montre plus que ce qui est en cours. « En cours » reprend la définition déjà utilisée
par les épisodes d'alerte — encore émis dans la fenêtre d'anti-rebond — pour que deux endroits du
produit ne se contredisent pas. Ce n'est pas une preuve de résolution mais une absence de récidive,
et le libellé ne dit jamais « résolu ». Les lignes d'information sont écartées : un déploiement est
un événement, pas un état. L'historique complet reste dans Monitoring → Journaux, atteignable par
un lien toujours affiché, même quand il n'y a rien en cours.

---

## Les conteneurs orphelins n'existaient que dans une alerte — 2026-08-30

L'alerte « conteneur orphelin » nommait un conteneur absent de la base. Or toutes les pages listent
la base : il n'apparaissait donc nulle part, et aucun bouton ne pouvait le détruire. L'alerte disait
quoi faire sans donner le moyen de le faire.

Deux conteneurs tournaient ainsi depuis neuf jours sur un nœud, lisant un flux de production et
brûlant CPU et GPU pour une sortie que personne ne lit. Un onglet Inventaire dans la page Containers
confronte maintenant ce que chaque agent exécute réellement à ce que la base connaît, et permet de
détruire ce qui n'est plus référencé.

Le tri ne se fait pas au préfixe : un conteneur `rdma-ini-<id>` n'est légitime que tant que le lien
`<id>` existe. En croisant plutôt qu'en excluant, sept orphelins RDMA sont apparus, dont deux paires
qui répliquaient encore depuis vingt-trois heures. L'appariement se fait sur le couple nœud + nom,
jamais sur le nom seul, puisque les vmid sont réattribués.

---

## Envoyer un fichier vers une racine locale échouait toujours — 2026-08-30

La page Fichiers passait le flux brut de la requête à la fonction d'envoi, et non l'objet de
Werkzeug qui l'enveloppe. La branche « nœud » lit ce flux par morceaux et fonctionnait ; la branche
locale appelait une méthode qui n'existe que sur l'objet enveloppe, d'où un message parlant de
`SpooledTemporaryFile` au-delà de 500 ko et de `BytesIO` en dessous.

Seules les racines locales étaient touchées, ce qui explique que le défaut soit passé inaperçu :
tous les envois vers un nœud aboutissaient. Les deux branches lisent désormais le flux de la même
façon, par blocs d'un mégaoctet, ce qui borne aussi la mémoire sur les gros fichiers.

---

## Les licences des composants tiers manquaient à la redistribution — 2026-08-30

Apache-2.0 et BSD-3-Clause exigent qu'une copie du texte de licence accompagne toute
redistribution, source comme binaire. Les mentions étaient présentes, les textes non — alors que
les images runtime compilent et redistribuent `libmxl` et `libmtl`.

Un dossier `licenses/` fournit désormais les deux textes intégraux, repris tels quels depuis les
dépôts amont. Les trois correctifs que nous appliquons à libmxl sont déclarés dans
THIRD-PARTY-NOTICES, comme l'impose l'article 4(b) d'Apache-2.0. Le dépôt MXL amont ne contient pas
de fichier NOTICE, ce qui rend l'article 4(d) sans objet.

Deux pièges traités en même temps : le dossier a été ajouté à la liste blanche de l'installeur,
faute de quoi il aurait été conforme dans le dépôt et absent de chaque paquet installé ; et les
nouvelles lignes `COPY` des Dockerfiles ont été ajoutées aux listes de contexte de build, sans quoi
trois images ne se construisaient plus du tout.

---

## Les collisions de grouping BCP-002-01 se voient en exploitation — 2026-08-29

Le MUST de BCP-002-01 est l'unicité du couple `groupe:rôle` dans un même scope de Device. Un banc
le vérifiait déjà, mais un banc qu'on lance à la main ne surveille rien : l'exploitation ne voyait
pas la collision. Et comme le grouping est **figé à la première écriture** — précisément pour qu'un
rebuild ne puisse plus le changer — une collision installée est définitive.

Le contrôle tourne donc en continu et s'affiche dans Réglages → NMOS, avec les couples fautifs
**nommés** : un compteur qui dirait « 2 » n'aide personne à réparer. Trois états distincts sont
publiés — conforme, en collision, et mesure **indisponible**. Ce troisième compte : un contrôle qui
tombe ne doit pas afficher « conforme », et un orchestrateur plus ancien qui ne renvoie rien est
traité comme indisponible, jamais comme sain.

## Les autorisations deviennent modifiables — 2026-08-29

Les rôles étaient des constantes Python : ajouter « le monteur pilote les plugins mais ne déploie
pas » demandait de modifier le code et de redéployer. La page Mon compte affichait donc à chacun
des droits que personne ne pouvait ajuster.

Une table `habilitations`, semée **une seule fois** depuis les constantes — pas rôle par rôle au
démarrage, sinon un administrateur qui retire une autorisation la verrait revenir au prochain boot.
Le rechargement recopie la table en place, donc les modules qui ont importé `ROLES` voient les
changements **sans redémarrage**. Les constantes restent comme défauts, pour « rétablir l'origine ».

Au passage, un nom qui trompait : « rôle » désigne deux choses dans ce produit — l'habilitation
(ce qu'on a le droit de faire) et l'emplacement de production (« MULTIVIEW RÉGIE 1 »). Ce sont
deux notions sans rapport.

## Mot de passe : confirmation et robustesse, sur les cinq chemins — 2026-08-29

Le nouveau mot de passe se saisissait **une** fois, sans confirmation : une faute de frappe dans un
champ masqué verrouillait le compte sans que rien ne l'ait signalé. Et aucune règle de robustesse
n'existait nulle part — `admin/admin` passait par l'interface, par l'API et par l'assistant
d'installation.

Une règle unique, appliquée aux **cinq** chemins qui posent un mot de passe : création et
modification par un administrateur, changement par l'intéressé, premier admin de l'assistant (qui
se contentait de six caractères), et l'outil de dépannage en ligne de commande. Elle rend la liste
des règles enfreintes plutôt qu'un booléen — l'interface doit pouvoir les cocher une par une.
Politique : 12 signes au minimum, pas de mot de passe courant, pas de reprise de l'identifiant.

## Une page « Mon compte » — 2026-08-29

Le manque était double, et les deux moitiés se cachaient l'une l'autre. Changer son mot de passe
n'était possible que depuis Réglages → Utilisateurs, **sous la liste de tous les comptes** — le
dernier endroit où l'on cherche ses propres réglages. Et changer son e-mail n'était pas possible du
tout : il fallait déranger un administrateur pour corriger une faute de frappe dans sa propre
adresse.

La page comble le trou, et sa liste de champs modifiables est **blanche, pas noire** : prénom, nom,
e-mail, et rien d'autre. Ni le rôle, ni le nom d'utilisateur — on ne se donne pas de droits à
soi-même, et une élévation de privilège par un point d'entrée « de confort » est un classique. Une
colonne ajoutée à la table demain n'y entrera pas par inadvertance. La page dit aussi ce qui ne se
change **pas** soi-même, plutôt que de le taire.

## Deux sélecteurs de langue, de portée opposée, sur la même ligne — 2026-08-29

Il était demandé de préciser qu'un champ réglait « le défaut pour un nouvel utilisateur ». La note
n'a pas pu être écrite : elle aurait été fausse. Ce champ changeait la langue de l'utilisateur
**courant**, alors qu'il était posé juste à côté du champ Thème, qui, lui, règle bien le défaut du
système. Deux sélecteurs d'apparence identique et de portée opposée, côte à côte : personne ne
pouvait le deviner, et l'un des deux devait forcément tromper.

Le champ règle désormais le défaut du système, comme son voisin. La préférence personnelle reste là
où le code dit déjà qu'est sa place — dans le menu de l'utilisateur, exactement comme le thème.
Deux endroits pour la même chose finissent toujours par en désigner deux différentes.

## Les pages publiques suivent la langue du navigateur du visiteur — 2026-08-29

Sur une page publique, la langue du système n'est pas la bonne. L'interface interne suit la
préférence de chaque utilisateur, ce qui est juste — mais un lien public s'ouvre justement chez
quelqu'un qui n'a **pas** de compte, et parfois hors de la maison. Il recevait la langue par défaut
de l'installation quoi qu'il arrive.

Passer la langue au gabarit n'aurait rien traduit : `_()` appelle la résolution de langue courante,
et le paramètre n'aurait changé qu'une variable d'affichage en laissant tous les textes dans la
langue du système. La surcharge est donc posée au seul point qui décide, et elle ne vit que le temps
d'une requête — une variable de module aurait fui sur la requête suivante, celle d'un autre
visiteur.

## Un champ hors `.form` n'a aucun style — 2026-08-29

Signalé à l'écran, et c'est un défaut de fond. Le style de champ du produit est limité à `.form`.
Ailleurs — dans une cellule de tableau, dans un popover — le navigateur applique **ses** défauts,
donc un texte noir : sur le fond sombre du thème, le lien d'un accès public était illisible. Le
fond et la bordure avaient été donnés, pas la couleur du texte. Hors `.form`, on ne complète pas un
style : on l'écrit en entier.

Le piège a frappé trois fois dans la même soirée — le lien, les champs du contrôle d'adresses, le
titre du popover — chaque fois en donnant l'impression d'un bug de thème. D'où une classe dédiée,
aux mêmes valeurs que la règle d'origine, donc sans dérive possible, posable partout.

## Un déploiement de moteur révoquait le GPU des conteneurs en marche — 2026-08-29

Cause racine d'une panne de neuf jours, trouvée le 2026-08-28 et reproduite au banc.

Le plan de liaison VFIO réécrivait son unité systemd **inconditionnellement**, puis rechargeait le
démon — donc à chaque déploiement de moteur, même quand le fichier était identique. Or un
rechargement réapplique la politique de périphériques des cgroups et retire l'accès GPU aux
conteneurs **déjà en marche** : les `/dev/nvidia*` restent visibles, seule l'autorisation disparaît.
L'ouverture rend EPERM et NVML dit « Unknown Error ».

Le 2026-08-19 à 18:50, un redémarrage du moteur 2110 a ainsi révoqué la carte du conteneur de
monitoring d'un utilisateur ; son encodeur est mort et a été relancé en boucle pendant neuf jours.
Preuve : la date de l'unité sur le nœud est à la minute du redémarrage, et les conteneurs recréés
après n'ont rien eu. L'A/B au banc confirme les trois états — neuf, révoqué après rechargement, et
intact avec le nouveau code, qui n'écrit plus rien quand rien n'a changé.

## Un nœud synchronisé à 1 ns était annoncé « PTP ✗ » — 2026-08-29

Le badge de la page Monitoring, la pastille d'accueil, la sonde de conformité et l'étiquetage des
flux jugeaient tous la synchro sur un seul critère, hérité de l'ère AF_XDP. Sur le socle full-PF
DPDK, ce critère désigne un verrou servo **strict** calculé sur le delta brut, avec un seuil dur à
100 ns. Le PHC de l'E810 n'étant pas discipliné par un servo matériel, ce brut reste de l'ordre de
la microseconde en permanence : le verrou ne s'arme **jamais**, quelle que soit la qualité réelle de
la synchro.

Constaté sur un nœud : offset corrigé au grandmaster de 1 ns, délai de chemin 179 ns, delta brut
3402 ns. Le nœud le mieux discipliné du parc affichait « PTP ✗ », et la sonde émettait en continu
« PTP perdu ». Le bon critère existait déjà, mais seulement dans le détecteur d'alarmes et dans un
onglet de réglages : l'affichage et l'alarme ne parlaient pas de la même chose. Il devient le point
de décision **unique** du produit, et les quatre consommateurs y sont rebranchés.

## Le tampon audio de `stream_in` jetait 40 % du son — 2026-08-29

Le plafond du tampon était sous la taille des rafales de ffmpeg : 40 % de l'audio jeté, puis autant
de silence, sur **tous** les `stream_in`. Corrigé et en service.

Trois chantiers restent ouverts et sont consignés avec leurs mesures : le calage A/V du streamer,
décidé au démarrage de ffmpeg et **différent à chaque démarrage** (+58, +65, +73, +82 ms sur des
configurations identiques) ; le verrou A/V de `stream_in`, qui demande une récupération d'horloge
avant d'être activable ; et le lecteur audio de la sonde, qui republie une valeur figée après
recréation de son producteur — un piège à conclusions fausses, à traiter avant toute campagne A/V.

## Page publique par jeton, et ce qui la verrouille — 2026-08-28

Un lien `/p/<jeton>` monte la console **du plugin** du conteneur visé, quel qu'il soit. Rien dans ce
mécanisme ne connaît un plugin en particulier : l'écrire dans un plugin aurait garanti qu'on le
recopie au suivant — c'est déjà arrivé quatre fois avec l'éditeur de layout.

C'est la **page** qui est servie, pas un flux WebRTC. Le lecteur WebRTC existe déjà et réencode :
parfait pour montrer une image, destructeur pour un instrument de mesure, dont les tracés fins sont
exactement ce que l'encodeur jette. Ici la page reçoit les données et les dessine — net à toute
résolution, aucun encodage, aucun conteneur de plus.

Le relais est en **lecture seule**, et c'est sa raison d'être : la méthode (GET seul), le chemin
(uniquement ce que le manifeste déclare) et le conteneur (celui du jeton) sont verrouillés. Un
relais qui accepterait tous les points de contrôle laisserait recâbler l'entrée d'un instrument de
régie depuis un lien reçu par courriel.

Un lien peut être **restreint à des adresses**, dès sa création. Le choix de l'adresse est la
sécurité elle-même : l'en-tête `X-Forwarded-For` est envoyé par le client, donc un filtre bâti
dessus ne filtrerait rien tout en ayant l'air de filtrer. On prend le vrai pair TCP, inforgeable ;
un réglage explicite bascule sur l'adresse transmise, et n'a de sens que derrière un
reverse-proxy de confiance. Le filtre porte sur les **trois** entrées — la page, les données et les
fragments d'interface : ne protéger que la page laisserait les données à qui sait lire une URL.

Enfin, les liens sont accrochés à l'identité d'**instance**, pas au `vmid`. Le vmid est un handle
local et jetable, réattribué : un lien mourait à la recréation du conteneur, et surtout, si ce vmid
était repris par un autre conteneur du même type, le vieux jeton ouvrait la page de **cet autre
conteneur** — un accès sans identification qui se déplace tout seul. Corollaire assumé : la
destruction d'un conteneur ne révoque pas ses liens, puisque recréé il garde son identité ; le
ménage des orphelins est une action voulue, avec son compte affiché.

## L'instrument de mesure `scope` — 2026-08-25 → 2026-08-27

Un plugin d'instrumentation complet est né en trois jours : waveform, vecteur-scope, goniomètre,
histogramme, contrôle de gamut RVB, fausse couleur et zébras, bar-graphs audio avec crête vraie et
LRA, phase contre la grille TAI et écart inter-flux, inventaire ANC et sous-titres OP-47, tuile
d'ingénierie 2110, disposition libre avec texte et horloge, entrée B pour la comparaison A/B, et
sortie vidéo MXL.

Les noyaux sont en **C**, pas en numpy : 4,48 ms par trame 1080 au lieu de 12 à 18. L'écart ne vient
pas de numpy mais du tableau d'index de deux millions d'entiers que la version C n'a pas besoin de
matérialiser. Ce qui commande le coût est le **cache**, pas la fréquence — mesuré : 1920 colonnes
d'histogramme (1,97 Mo, déborde le L2) coûtent 6,88 ms, 512 colonnes (512 Ko, tient en L2) coûtent
2,63 ms. D'où 512 colonnes, qui est de toute façon la largeur d'affichage réelle d'un waveform.

**Toutes** les lignes sont mesurées, jamais de décimation : décimer ferait rater l'événement rare —
la ligne unique hors norme, l'éclair sur une ligne — qui est précisément l'objet de l'appareil.

La console web lit en binaire (26 ms de sérialisation supprimées), la rampe d'affichage est passée
côté script (768 Ko → 197 Ko), et la parade R/V/B en trois fils a supprimé le déficit de cadence
(×3,14). En UHD, les bandes parallèles font passer le 2160p50 de 95 % à 37 % du budget — et le gain
vient des **cœurs**, pas du découpage en tranches, qui seul coûtait quatre images par seconde.

## Le mode tranche devient obligatoire pour tout nouveau plugin — 2026-08-27

Le scope lit son entrée bande par bande. Mesuré en A/B : le calcul restant après l'arrivée de la
dernière ligne passe de 5,09 à 1,48 ms, à cadence inchangée.

La règle est désormais écrite : tout **nouveau** plugin doit implémenter nativement le mode tranche.
Un plugin qui travaille en image entière ajoute une trame de latence à toute chaîne qui le traverse,
et cette dette n'apparaît sur aucun compteur — le plugin affiche une cadence parfaite. C'est
exactement pour ça qu'il fallait une règle et pas un réflexe : le scope a été écrit en image entière,
et il a fallu y revenir après coup.

## Supervision SNMPv3 — 2026-08-25 → 2026-08-26

Un service d'agent SNMPv3 `authPriv` en **lecture seule**, servi par le contrôleur actif, avec sa
MIB livrable, cinq tables instrumentées et les notifications d'alarme. Deux appels d'offres
l'exigent nommément.

Le Private Enterprise Number de BOBI a été attribué par l'IANA le 2026-08-26 : **66633**. L'arbre
SNMP et l'identifiant de données structurées syslog passent à l'arc définitif. Le code, lui, n'avait
rien à changer : il n'a jamais porté d'OID en dur — c'était tout l'intérêt du littéral unique. Ce
qui restait à aligner, c'est ce que lit un humain, dont le descriptif de recette : un NMS
provisionné avant cette date est à reprendre.

## L'interface passe en anglais — 2026-08-26

Campagne de recette du 21 au 26 août. Réglages, la dernière grosse page : 159 intitulés de champs et
55 paragraphes d'explication — une clé par paragraphe, balises comprises, car les découper en
fragments donnerait une phrase anglaise recousue avec des morceaux de grammaire française. Les deux
bandeaux de panne de la haute disponibilité en font partie : ce sont eux qu'on lit quand un
contrôleur vient de tomber.

Une famille entière restait invisible : les **onglets de service**. La page de réglages agrège un
gabarit par service, et NMOS comme TSL n'avaient aucune clé — 128 textes pour NMOS seul. Le libellé
d'onglet, qui vient du manifeste d'un sous-module, est traduit centralement par convention de clé,
sans toucher au manifeste.

## Un lecteur MXL décroché se raccroche seul — 2026-08-22

Un producteur qui détruit puis recrée son flux sous le même nom laisse tous les lecteurs ouverts
accrochés à la génération morte. Le piège : cette génération reste **lisible** — des grains sont
servis, index figé, aucune erreur, aucune exception. Les reprises « sur erreur » ne se déclenchent
donc jamais et le consommateur gèle en silence, pour toujours.

La parade existait mais était facultative. L'audit a montré que **sept consommateurs sur onze** ne
l'appelaient pas. Ce n'est pas une série d'oublis : une protection facultative contre une panne
silencieuse n'a aucun retour qui rappelle qu'on l'a oubliée, donc elle sera oubliée. Constaté en
direct le même jour — la sonde lisait un anneau mort depuis 1 h 44 avec une image par ailleurs
parfaite, et le défaut était cherché dans le décodeur.

La garde tourne maintenant d'elle-même. Elle ne s'arme que si le lecteur sert déjà quelque chose :
c'est le discriminant entre les deux situations que « périmé » confond — un lecteur décroché sert des
grains à index figé, un producteur arrêté n'en sert aucun et rouvrir n'y changera rien. Sans ce
garde-fou, une prévisualisation simplement éteinte déclenchait 55 réouvertures par minute.

## Le délai d'un étage se mesure enfin — 2026-08-22

Un étage publie l'écart, en trames, entre l'index qu'il **lit** et celui qu'il **écrit**. C'est la
seule mesure directe d'un étage : le temps de calcul dit la marge disponible, pas le retard subi.
Trois états sont publiés, dont un explicite « non mesurable » — un writer qui impose son index n'a
pas de délai d'étage à annoncer, et rendre 0 serait un mensonge commode.

Les bancs qui vont avec ont révélé le leur : celui qui vérifiait les plugins annonçait « tout
compile » alors qu'un plugin était écarté du registre pour une accolade non doublée. Un banc qui ne
peut pas échouer ne vérifie rien. Et un autre refuse désormais une trame en disposition « tranche »
au lieu de la noter en échec : crier sur une entrée saine apprend à ignorer ses propres alarmes.

## Trois messages qui accusaient la mauvaise cause — 2026-08-21

Même famille, relevée trois fois en recette : le système affirme quelque chose de faux et envoie
l'exploitant à l'opposé du problème.

Une réinstallation de nœud efface son certificat mTLS sans prévenir le contrôleur, qui continue
d'appeler en HTTPS ; l'exploitant lisait « agent injoignable » et « images absentes » — deux
affirmations fausses, l'agent tourne et les images sont là. On ne devine pas : sur cette signature
d'erreur, on redemande en clair, et si l'agent répond le message nomme la cause et le remède.

Un tag d'image était inscrit sur **tous** les nœuds dès qu'un build réussissait, sans vérifier que
les bits y étaient arrivés — un nœud éteint pendant la distribution gardait ses vieilles images
pendant que la base affirmait le contraire. Le tag n'est plus écrit que si l'image est vue sur le
nœud, et une alerte **nomme** le nœud.

Enfin, le build d'une image dérivée dont l'image de base manque rendait « pull access denied » : un
message d'autorisation pour une cause de disponibilité. Le testeur avait cherché du côté des
identifiants de registre.

## Une alerte porte une clé et ses paramètres, plus une phrase française — 2026-08-21

Le bandeau « Alertes récentes » restait en français quelle que soit la langue choisie. Ce n'était
pas une clé de traduction manquante : la fonction d'écriture recevait une phrase **déjà rendue** et
la stockait telle quelle — à l'affichage il n'y avait plus rien à traduire, le texte était devenu de
la donnée.

Le rendu ne peut pas se faire à l'écriture : une alerte est écrite une fois, par un thread de fond,
et relue par N utilisateurs qui n'ont pas la même langue. La base stocke donc une clé et ses
paramètres, et le rendu est différé jusqu'à la lecture. La colonne française reste remplie, et c'est
un **choix** : c'est la forme canonique, celle que dédoublonne l'anti-rebond, que regroupe l'accueil
et que cherche la recherche. Ces trois usages ont besoin d'une représentation indépendante du
lecteur. Elle est dérivée de la clé, donc les deux formes ne peuvent pas diverger.

Une clé absente du catalogue est écrite en clair **avec** une erreur au journal, jamais repliée en
douce. Au terme de la campagne, 320 sites sur 362 sont convertis, avec 482 clés symétriques FR/EN.
Une primitive manquait au socle : « Cadence non tenue » énumère jusqu'à cinq causes indépendantes —
trop de combinaisons pour une clé par variante, et joindre les causes dans un paramètre laissait du
français au milieu d'une phrase anglaise. Un paramètre peut désormais être une **liste de clés**,
chacune rendue dans la langue du lecteur.

## La sonde de latence rendait 0 sur une chaîne réellement retardée — 2026-08-21

Une entrée passant par un délai réglé à 4 images ressortait à **0,00**. Ni le délai ni la sonde
n'étaient en panne : c'est la méthode de mesure qui était fausse.

L'âge était calculé comme **index du grain lu − index incrusté dans l'image**. Ça ne mesure pas un
âge. Le plugin `delay` **propage la coordonnée source** (`open_grain(src_index=)`) — à dessein,
pour la parité de champ et l'alignement inter-flux : le contenu repart avec son horodatage
d'origine, simplement N images plus tard. Les deux termes de la soustraction se déplacent donc
ensemble, et la différence reste nulle. La sonde voyait les étages qui **ré-horodatent** (un
mixer) et était aveugle à ceux qui **propagent**, tout en annonçant mesurer « la latence RÉELLE ».

Ironie du dossier : le plugin `delay` avait anticipé exactement ce piège pour lui-même. Son code
explique qu'il ne déduit pas son délai d'un écart d'index, *« qui rendrait 0 ici »*, et qu'afficher
0 pour le seul module dont la fonction est d'ajouter du délai *« serait le plus grossier des échecs
silencieux »*. C'est ce qui arrivait, vu de l'autre bout de la chaîne.

**L'âge se mesure désormais contre MAINTENANT**, sur la grille TAI ST 2059
(`bobimxl.current_index`, nouveau helper exposant `mxlGetCurrentIndex`). Cette référence ne dépend
d'aucune convention d'étage. C'est la règle « âge absolu contre horloge TAI » déjà établie
ailleurs dans le projet, après que le raisonnement par index eut produit quatre conclusions
successives fausses, toutes retirées.

Résultat, et le bouclage vaut mieux qu'un chiffre isolé :

| flux | avant | après |
|---|---|---|
| mire `avsync` | 0,00 | **1,02** |
| via un mixer | 1,00 | **2,16** |
| via un délai de 4 images | **0,00** | **6,20** |

L'étage délai ressort à **+5,18** images : **4 déclarées par le plugin lui-même**, et **1,2 de
transit** — exactement ce que coûte l'étage mixer (+1,14). Trois étages indépendants s'accordent
sur le prix d'une traversée ; c'est ça qui donne confiance, pas la valeur absolue.

**Contrôle intégré**, à connaître avant de lire un relevé : mesurer la mire elle-même doit rendre
**~1 image**. Si elle rend 0, la mesure est fausse — c'était la signature exacte du défaut. (La
règle générale du projet cite ~2 images, mais elle vise un timecode d'HORLOGE, qui porte l'instant
du rendu ; ici la valeur incrustée est l'index du grain, attribué à l'estampillage : un saut de
moins.)

Si le binding embarqué est trop ancien pour exposer `current_index`, la sonde **ne publie pas
d'âge** plutôt que de retomber sur l'ancienne formule. Pas de mesure vaut mieux qu'un chiffre faux
d'allure crédible.

---

## Trois réglages qui n'existaient que sur le papier — 2026-08-21

Même défaut, trois fois, sur deux plugins : une fonction implémentée dans le conteneur, déclarée
au manifeste, et impossible à activer depuis l'interface. Le code était là ; le chemin ne l'était
pas.

**Mire A/V — la case « Bandeaux-sonde » se décochait toute seule.** Cocher, Appliquer, et la case
revenait à zéro. Le hook `control_action` de l'orchestrateur filtre les réglages par **liste
blanche** (`_SOFT_KEYS`), et `probe_bands` n'y figurait pas : il n'était donc ni transmis au
conteneur, ni persisté. L'interface, qui se relit depuis `/state`, retrouvait 0 et se décochait —
un refus muet, présenté comme un clic sans effet.

Le script du conteneur savait pourtant l'appliquer à chaud (son commentaire le dit explicitement :
« allumer, mesurer, éteindre — sans redéployer une mire qui sert de référence »), et l'UI savait
l'envoyer. Sur les trois côtés du contrat, un seul avait été oublié quand la fonction a été
ajoutée. C'est le risque propre à toute liste blanche : elle échoue en silence, et du côté où
personne ne regarde.

**Sonde de latence — les régions de lecture.** Mesurer une TUILE d'un multiview plutôt que l'image
entière est la fonction phare de la 0.7.0. Elle n'était réglable nulle part : déclarée
`scope: user`, donc filtrée par la palette *et* par les réglages structurels, la convention voulant
que ces champs soient couverts par l'UI de contrôle du plugin — qui ne les proposait pas. Elles ont
désormais une colonne dans la console, une par entrée, avec un hook qui persiste et applique à
chaud.

Un piège trouvé en l'implémentant : régler une région passait par `/input`, qui posait la source à
la chaîne vide quand `shm` était absent. **Changer une région aurait débranché l'entrée qu'on est
en train de régler.** `shm` est maintenant facultatif : absent = on ne touche pas au câblage.

**La sortie vidéo est activée** sur la sonde du banc : elle publie son panneau de mesures comme
un flux câblable (`sonde-latence`, 1920×1080p50, 49,9 fps), et le port « Panneau » apparaît donc
sur la page Câbles. Le réglage existait, était implémenté, et n'attendait que d'être atteignable.

**Le panneau vidéo a sa propre langue.** Traduire la console ne traduisait pas ce qui est
*incrusté dans l'image* — en-têtes et états du panneau restaient en français sur le mur. C'est un
public différent : le panneau est vu par la régie, pas par celui qui règle la page. Sa langue est
donc un réglage du plugin (FR/EN), modifiable à chaud, **indépendant** de la langue de l'interface.
Vide au déploiement = langue du contrôleur, jamais « français » figé — ce qui aurait imposé du
français à une installation anglophone sans que personne l'ait demandé.

Pour que ce soit cohérent, les **états** (`ok`, `non câblé`, `aucun bandeau — générateur éteint ?`)
sont désormais publiés sous forme de **code stable** en plus du texte : le panneau les traduit dans
SA langue, la console dans celle de l'interface. Sans ce code, on aurait obtenu des en-têtes
anglaises au-dessus d'états français — à moitié traduit, c'est-à-dire pire.

⚠ Corrigé en chemin : les réglages du panneau (entrée affichée, titre, marque, horloge) passaient
par le proxy générique, qui **n'écrit rien en base**. Ils étaient perdus au premier redéploiement.
Ils passent maintenant par `control_action`, qui persiste ET applique à chaud.

**La console de la sonde est traduite.** Elle était intégralement en français, y compris interface
réglée en anglais — en-têtes du tableau, infobulles, bloc « sortie vidéo », texte d'aide. Elle
utilise maintenant le mécanisme d'i18n des plugins (`plugins/<type>/i18n/{fr,en}.json`, clés
`plugin.<type>.*`), déjà employé par `split`, `udc`, `multiview` et `streamer`. Le français reste
écrit en dur dans le HTML et en repli dans le JS : une clé manquante n'affiche donc jamais la clé
brute à l'exploitant.

**Et `genlock` / `refresh_hz`**, également invisibles, reclassés `scope: system` : ils sont lus au
démarrage du script, ils ne se basculent pas à chaud. Les annoncer comme réglages « user » laissait
croire à une bascule en direct qui n'existe pas.

Vérifié de bout en bout sur le banc, dans les deux sens : la bascule met à jour le conteneur ET la
base, le décochage aussi, et poser une région ne déplace pas le câblage.

**Et pendant qu'on y était : la moitié des boutons « Appliquer » n'avait pas lieu d'être.** La
console de la mire en comptait quatre. Deux commandaient des réglages appliqués **à chaud** :
faire dépendre un dial d'un clic ultérieur n'apporte rien et invite à l'erreur — on tourne, rien
ne bouge, on recommence. Ils ont disparu : le réglage part au relâchement du dial, à la molette
qui s'arrête, ou quand on quitte un champ, et la section le dit (« appliqué en direct »).

Les deux autres **recréent le conteneur** (format, police). Ils restent, et s'appellent désormais
« Appliquer et redéployer ». Couper un signal peut-être à l'antenne mérite un clic délibéré : là,
le bouton n'est pas une formalité, c'est la confirmation.

C'est pourquoi un « Appliquer » global unique aurait été le pire des trois choix : il aurait mis
un demi-décibel de bip et une recréation de conteneur derrière le même geste.

Un piège en l'implémentant : les dials sont des widgets maison qui n'émettent **pas** d'événement
`change`. Une écoute déléguée classique ne les aurait jamais vus — et comme leur bouton venait
d'être retiré, la section Timing A/V serait devenue impossible à appliquer. Ils sont branchés sur
la fin du geste, et la molette sur une temporisation de 400 ms, sans quoi un quart de tour
enverrait cent requêtes.

---

## La sonde de latence : câblage impossible, puis câbles invisibles — 2026-08-21

Câbler une entrée sur la sonde échouait systématiquement : « Erreur 502 : appel sonde_latence :
HTTP 400 ». Le 502 de l'orchestrateur n'était que l'écho d'un 400 venu du conteneur.

L'orchestrateur envoie le numéro d'entrée sous la clé **`slot`** (`routes/cabling.py:_plugin_input`),
et c'est la convention de la flotte — `delay` lit `slot`, `split` et `mixer` acceptent `slot` et
`idx`. La sonde, elle, ne lisait que **`index`**, et rejetait donc en bloc le seul corps que
l'orchestrateur sait produire. Vérifié directement sur le conteneur avant de corriger : le payload
exact de l'orchestrateur revenait en `400 {"error": "index"}`.

Le plugin accepte maintenant `slot`, puis `idx`, puis `index` — les trois clés en circulation —
pour ne dépendre ni de la version de l'orchestrateur ni de celle du script déjà déployé dans un
conteneur. Un numéro d'entrée **hors plage** est par ailleurs refusé explicitement : il créait
jusqu'ici une entrée fantôme qu'aucun worker ne lisait, donc un câble qui semblait posé et ne
mesurait rien.

Corrigé au passage : `plugin.json` annonçait 0.8.0 alors que `meta.json` en était resté à 0.7.0 —
la page Plugins attribuait donc à 0.8.0 le changelog de 0.7.0. Les deux sont réalignés en 0.8.1.
Le changelog propre à 0.8.0 n'a pas été reconstitué : il n'a jamais été écrit, et l'inventer aurait
été pire que le trou.

Vérifié de bout en bout après redéploiement, par l'API réelle : `POST /api/home/wire` → 200
(`hot_wired`), l'entrée 1 de la sonde porte le flux, `POST /api/home/unwire` → 200, retour à vide.

**Puis un second défaut, que le premier masquait.** Le câblage passait, s'appliquait dans le
conteneur… et aucun câble n'apparaissait sur la page Câbles, les entrées restant affichées
« débranchées ». Le manifeste déclare `state_field: input_1…input_N` — au PREMIER niveau de
`/state`, numérotés à partir de 1 — et c'est là que l'orchestrateur lit les entrées pour tracer
les arêtes. La sonde, elle, ne les publiait que sous `inputs`, un sous-dictionnaire indexé à
partir de 0 : invisible pour lui. Le plugin ne respectait donc pas son propre manifeste, sur deux
contrats à la fois — celui d'écriture (`/input`) et celui de lecture (`/state`).

Les entrées sont désormais aplaties au premier niveau, comme le fait `delay`, qui est la référence
de ce contrat ; `inputs` est conservé pour la page de contrôle du plugin. Vérifié sur la
topologie servie : les deux entrées câblées ressortent `disconnected: false` et deux arêtes
arrivent bien sur le 998.

> Constat annexe, non corrigé : le décâblage vide bien le shm de l'entrée mais laisse derrière lui
> les descripteurs de format (`input_<n>_fmt`, `input_format`) de la source débranchée.

**Et une troisième chose, trouvée en répondant à « il n'est pas censé y avoir une sortie
vidéo ? ».** Si : la sonde sait publier ses mesures comme un vrai flux vidéo câblable — le
« Panneau » — c'est implémenté dans le script et déclaré au manifeste. Mais l'option qui l'active
n'était réglable **nulle part**. Déclarée `scope: user`, elle était filtrée par la palette de
déploiement *et* par les réglages structurels de la page Traitements, la convention voulant que
les champs `user` soient couverts par l'UI de contrôle du plugin — laquelle n'expose que
l'affichage du panneau. La capacité existait et était morte.

Reclassée `scope: system`, comme TOUS les autres réglages de la flotte qui créent ou suppriment
un port (`n_inputs` du mixer, de la pyramide, du recorder, `outputs` du générateur de mire). C'est
le bon classement, et pas seulement par cohérence : la sortie est ouverte au démarrage du script
par un écrivain MXL dédié, elle ne se bascule pas à chaud. L'activer passe donc par le popover ⚙,
qui redéploie — ce qu'un réglage « user », censé s'appliquer en direct, aurait laissé croire
possible sans l'être. Vérifié sur la page servie : le champ est désormais rendu.

> Même trou, non corrigé, sur quatre autres réglages de ce plugin : `genlock`, `refresh_hz` et
> `region_0…3` sont déclarés `user` et n'apparaissent dans aucune interface. Les régions sont la
> fonction phare de la version 0.7.0 (mesurer à travers un multiview) : elle est, elle aussi,
> injoignable autrement qu'en éditant les params à la main.

---

## Sept correctifs nés d'une nuit de panne — 2026-08-19

Une soirée d'incident sur une installation à l'antenne a fait sortir sept défauts. Aucun n'était
visible de nos instruments : tous ont été trouvés en mesurant, et six sur sept sont des variantes
de la même faute — **un comportement qui s'applique sans que personne l'ait choisi, et un échec
qui ne dit pas son nom.**

**1. Un réglage d'émission s'armait tout seul au changement de version.** `serve_newest` — quelle
trame le moteur 2110 met sur le fil — valait 1 par défaut quand un slot ne portait pas la clé. Tant
que le moteur ne savait pas lire cette clé, elle ne faisait rien ; le jour où une nouvelle image l'a
honorée, le comportement s'est activé sur une installation en production, sans intervention. Une
des deux sorties, identiques par ailleurs, est sortie **striée** — l'autre non, parce que le
résultat dépend de la phase entre la publication du producteur et la lecture de l'émetteur. Le
défaut repasse à 0 (comportement historique), et il ne vit plus qu'à **un seul endroit** au lieu
d'être redérivé à deux. Le gain de latence reste disponible, mais il se demande maintenant, par
sortie. *Un doute — celui inscrit dans le commentaire d'origine : « peu de recul, le cas non observé
est une source irrégulière » — ne se met pas dans une valeur par défaut.*

**2. Une boucle de reconnexion sans palier a tué un nœud entier.** Deux proxys amont n'étaient plus
produits ; les murs qui les lisaient relâchaient et rouvraient leur lecteur **à chaque trame** —
7,8 et 9,0 millions de tentatives en 43 heures. Chaque cycle fuyait un descripteur de fichier, soit
2 Gio de mémoire noyau par conteneur, jusqu'à saturer leur plafond : le noyau s'est alors mis à
récupérer leurs pages, la composition est passée de 13 ms à **7,2 s par trame**. Les deux premières
tentatives restent immédiates — c'est tout l'intérêt du mécanisme — puis le délai double jusqu'à
5 s. Au passage, le ramasse-miettes du domaine partagé n'est plus rejoué à 50 Hz : **il détruisait
des flux vivants appartenant à d'autres processus**, qui restaient ensuite à 0 fps sans le savoir.

**3. Une entrée inexploitable se voit, maintenant.** Trois workers de la pyramide n'ont jamais
réussi à ouvrir leur source et ont bouclé 43 heures **sans écrire une seule ligne**. Le seul indice
était une absence : leur slot manquait dans l'état publié, pendant que l'état périodique continuait
d'annoncer les proxys correspondants. Ils tracent désormais, **publient leur échec** (`entrees_ko` :
source, motif, nombre de tentatives, ancienneté) et espacent leurs essais. Une absence ne se voit
pas ; une entrée qui dit « je n'y arrive pas depuis 43 heures » se voit.

**4. La mémoire d'un conteneur est surveillée par rapport à SON plafond.** Pendant les 40 heures de
montée, le nœud avait 160 Gio libres et 71 % de CPU au repos : toutes nos sondes étaient au vert.
La seule grandeur qui montrait quelque chose était le rapport entre la mémoire d'un conteneur et le
plafond qu'on lui a alloué — l'agent la publiait depuis toujours, personne ne la lisait. Alerte à
85 %, erreur à 95 %.

**5. Un build ne se déclare plus en échec pendant qu'il tourne.** Le délai d'attente ne coupe que
le suivi HTTP, jamais la compilation sur le nœud : une image annoncée « échec » à 40 minutes est
sortie 13 minutes plus tard. On surveille désormais son apparition au lieu de conclure — un faux
échec sur une opération d'une heure invite à la relancer, donc à faire tourner deux compilations en
parallèle sur la machine qui porte l'antenne.

**6. Recréer un conteneur sur une nouvelle image est une commande.** Un conteneur de calcul ne
change jamais d'image tout seul, et le seul chemin qui relit l'image du nœud n'était accessible par
aucune commande : adopter une image fraîchement construite demandait un retrait manuel, conteneur
par conteneur. `POST /api/containers/<vmid>/recreate` le fait proprement, en gardant l'identité, la
configuration et les jetons.

**7. Les noms de flux ne se fabriquent plus à la main.** La migration de numérotation d'août avait
converti les données, mais **quatre endroits du code continuaient de reconstruire ces noms sur
l'indice brut** — donc en ancienne convention, bien après la migration. Conséquences silencieuses :
le débit par flux et le témoin « abonné mais ne reçoit pas » étaient morts (rangés sous un nom,
relus sous un autre — une recherche qui ne trouve jamais rien ne se plaint pas), et le nom proposé
au câblage réinjectait de l'ancienne convention dans la configuration des consommateurs. Tout passe
par l'API de numérotation, et le garde-fou qui surveillait déjà les clés indexées surveille
désormais aussi les noms de flux — il ne les voyait pas.

---

## Les couleurs d'un câble veulent dire la même chose dans tous les thèmes — 2026-08-19

Deux corrections qui n'en font qu'une, parce que la première a rendu la seconde indispensable.

**L'ANC passe en trait plein.** Elle était tracée en pointillé pour marquer son essence ; à côté
des câbles vidéo et audio, pleins, elle avait l'air à moitié effacée — comme si le lien était
moins réel, ou en défaut. Les trois essences se lisent maintenant à la couleur seule, exactement
comme la vidéo et l'audio se distinguaient déjà entre elles.

**Ce qui reporte tout le poids sur la couleur — et là, il y avait un vrai problème
d'exploitation.** La vidéo était câblée sur `--accent`, c'est-à-dire sur l'IDENTITÉ DU THÈME :
bleu acier en sombre, ambre en Studio, indigo en Daylight. L'audio changeait aussi (tan, turquoise,
orange selon le thème). Un exploitant au téléphone qui dit « je suis le câble bleu » ne désigne
alors rien du tout — son interlocuteur, sur un autre thème, cherche une couleur qui n'existe pas
chez lui. L'essence d'un signal n'est pas une décoration : elle ne doit pas plus changer avec le
thème que le nom d'un flux ne change avec la langue.

Les six variables d'essence ne sont donc plus surchargées par les thèmes. **vidéo = BLEU ·
audio = VERT · ANC = VIOLET**, trois mots qui se disent au téléphone, partout les mêmes.

Choisies par mesure, pas au jugé, sous quatre contraintes simultanées :

- **contraste ≥ 3:1 sur les trois fonds** (mesuré ≥ 3,20). C'est la contrainte qui mord : une
  couleur assez claire pour le thème sombre tombe à 2,2 sur le clair. D'où des tons moyens, plus
  denses que l'ancienne palette — c'est le prix de l'invariance, et il est assumé.
- **ΔE ≥ 40 entre essences** (mesuré ≥ 52).
- **ΔE ≥ 30 de toute couleur de statut** et de l'ambre RDMA (mesuré ≥ 35). Ce critère a écarté
  plusieurs verts d'audio, trop proches du vert « en marche » : un câble qui se lit comme un état
  est pire qu'un câble terne. L'ancien audio tan, lui, frôlait la couleur d'avertissement.
- les variantes de survol gardent la **même luminance** avec plus de chroma : les éclaircir
  aurait amélioré le thème sombre en dégradant le clair.

Le pointillé libéré n'est pas repris : il sert au surlignage RDMA, et deux pointillés superposés
sur un même trait ne se distinguent pas. Le câble sans signal garde son atténuation.

Banc `tools/verif_couleurs_essences.py` : il RELIT les fichiers CSS, refuse toute surcharge par
thème et revérifie les quatre contraintes. Vérifié qu'il échoue bien si l'on réintroduit une
surcharge — un banc qu'on n'a pas vu échouer ne prouve rien.

---

## Une connexion perdue se voit enfin — 2026-08-19

Quand la liaison au serveur tombe — VPN qui saute, wifi qui décroche, orchestrateur qui
redémarre — un navigateur ne proteste pas. La page garde son dernier état, répond aux clics, et
continue de faire ce qu'elle sait faire sans réseau. Rien ne distingue une cadence de 50 fps
vivante d'une cadence de 50 fps figée depuis dix minutes. Sur une régie, c'est une décision prise
sur une image morte — l'échec silencieux dans sa forme la plus coûteuse.

Il existait bien un embryon : trois pages tenaient chacune leur compteur d'échecs, et le seul
effet visible était `opacity: 0.7` sur un titre. Autant dire rien, et seulement là où c'était
recodé.

**Un témoin global, greffé sur `fetch`.** Un seul point d'entrée couvre les ~520 appels du front,
présents et à venir, sans toucher un seul appelant. Et comme toutes les pages pollent déjà
(cf. `MXLPoll`), il **n'ajoute aucun trafic** tant que tout va bien : il écoute le trafic existant.

Ce qui distingue une vraie coupure du reste, et c'est là que tout se joue :

- `fetch` **rejette** → la requête n'a jamais atteint le serveur. C'est une coupure.
- `fetch` **résout**, même sur un 500 → le serveur a répondu, donc la liaison est bonne. Ce n'est
  pas une coupure. (Même règle que le disjoncteur de `node_driver` : répondre, fût-ce en erreur,
  prouve qu'on est là.)
- `AbortError` → c'est nous qui avons annulé, en changeant de page. Sans cette exception, toute
  navigation aurait affiché « connexion perdue ».
- **401** → le serveur va bien, c'est la *session* qui a expiré. Panne différente, geste différent
  (se reconnecter, pas attendre) : bandeau distinct, en avertissement et non en erreur.

**Et surtout — c'est le vrai piège, découvert en coupant un VPN pour de bon : une coupure ne
produit aucun événement, elle produit du SILENCE.** Quand la liaison disparaît, `fetch` ne rejette
pas, il *pend* : le noyau attend l'expiration TCP, qui se compte en minutes. Une première version
n'écoutait que les rejets et n'a donc rien vu pendant tout ce temps. Pire, `MXLPoll` n'ordonnance
la passe suivante qu'à la fin de la précédente : une requête pendue arrête tous les polls de la
page, et avec eux la moindre chance de s'en apercevoir.

On surveille donc le **temps écoulé depuis la dernière réponse reçue**, avec une horloge
indépendante que rien ne peut bloquer. Passé 4 s de silence, une sonde part — et elle est le seul
appel à porter un délai de garde (2,5 s) ; on n'en impose surtout pas aux requêtes de
l'application, un build dure légitimement des minutes. Verdict en **7 secondes**, mesuré.

Un serveur simplement lent ne déclenche pas de fausse alerte : le silence lance la sonde, la sonde
répond en 3 ms, le compteur repart. On n'accuse que si la sonde elle-même reste sans réponse — et
un incident ponctuel dont la sonde réussit ne fait rien clignoter. Tant que la coupure dure, la
sonde tourne toutes les 3 s et rétablit le témoin d'elle-même au retour. Au retour sur un onglet
laissé de côté, le verdict est rendu immédiatement, sans attendre un battement : les navigateurs
brident les minuteries des onglets cachés.

La sonde tape `/api/ping`, une route nouvelle, **publique et sans état** : ni DB, ni réglages
(3 ms). Publique à dessein — exiger une session en ferait un test de deux choses à la fois, et une
session expirée se lirait « connexion perdue ».

`navigator.onLine` n'est qu'un accélérateur, jamais le verdict : il dit qu'une interface réseau
existe, pas que le serveur est joignable — un VPN qui tombe le laisse à `true`. On s'en sert pour
sonder tout de suite, et c'est la sonde qui tranche.

Le bandeau est volontairement impossible à manquer, et non refermable tant que la coupure dure :
ce n'est pas une notification, c'est un fait. Il donne l'**heure du constat** — « constatée à
16:42:07 » — et non un chronomètre : c'est l'heure qui se recoupe avec les journaux du serveur et
le reste de la régie, là où « depuis 3 min » oblige à faire la soustraction de tête et change à
chaque repeinture. Il dit aussi que ce qui est affiché n'est plus à jour — la page entière est atténuée, pas seulement un titre : c'est l'ensemble des
chiffres qui a cessé d'être vrai. Les trois compteurs par page ont été retirés au profit de ce
propriétaire unique ; deux propriétaires d'une même classe finissent toujours par diverger.

Deux bancs rejouables, `tools/verif_temoin_connexion.js` et `…_pendu.js` : ils extraient le module
de `layout.html` à l'exécution — un banc ne doit pas pouvoir tester une copie périmée — et
couvrent les échecs francs, le blip, le 401, les annulations, et la requête qui pend. Plus une
coupure réelle du service.

---

## Les alertes de la home : un compte faux, et un coût qui n'apparaît que sous charge — 2026-08-19

`/api/home/summary` chargeait **1000 lignes d'alertes complètes** à chaque passe — toutes les 2 s,
depuis trois pages — pour en tirer cinq lignes saillantes et un total.

**Le total était faux.** Il valait `len()` de cette fenêtre plafonnée à 1000, sur une base qui en
conserve 10 000 : la home annonçait sereinement « 1000 alerte(s) au total » depuis que le parc avait
dépassé ce seuil. Un plafond d'affichage lu comme un compte. C'est désormais un vrai `COUNT(*)`
— 10 081 au moment du correctif.

**Le coût, lui, ne se voyait pas en le mesurant seul.** Mesuré isolément, l'ancien calcul prend
4,4 ms et le nouveau 4,3 : aucun gain, et j'ai failli le jeter pour cette raison. C'est en le
mesurant **sous concurrence** — la seule condition qui compte, puisque plusieurs pages le pollent —
que l'écart apparaît : à 4 requêtes simultanées, l'ancien s'écroule à 503 ms médians et 8 appels/s,
le nouveau tient à 41 ms et 104 appels/s. **×12,8 de débit.** Matérialiser mille dictionnaires
Python par passe, connexion tenue, est peu coûteux seul et catastrophique à plusieurs.

Le regroupement se fait donc là où sont les données (`database.db_alertes_groupees` : 1000 lignes →
104 groupes en SQLite). L'ordre des niveaux, lui, reste défini **côté Python et nulle part
ailleurs** — c'est la seule chose que le SQL ne doit pas savoir, sous peine de la voir diverger en
silence. Vérifié : le verdict rendu est identique à l'ancien, ligne pour ligne.

**Et les pollers restants passent à `MXLPoll`** : `source_labels.js` (2 s, chargé sur TOUTES les
pages), la vue Sondes, les flux 2110, la page Labels — dont le poll à 400 ms —, la page Recette,
l'atelier et les rubriques plugin. Deux exceptions assumées, écrites dans le code :
`monitor_popout.html` est une page AUTONOME qui n'hérite pas de `layout.html`, donc `MXLPoll` n'y
existe pas (elle garde `setInterval`, et le commentaire dit pourquoi) ; et le poll de la page
Conteneurs a un timer à deux phases référencé en quatre endroits — la garde de recouvrement est
posée dans la fonction pollée plutôt que sur le timer, pour le même effet sans la réécriture.
Les minuteries d'AFFICHAGE (chronomètres, horloges, répétition des steppers) restent en
`setInterval` : elles ne parlent à personne, `MXLPoll` n'aurait aucun sens.

Effet cumulé avec les deux correctifs précédents, sous la charge réelle de deux navigateurs :
`/api/home/summary` 3 900 → **228 ms**, `/api/alerts` 2 240 → **30 ms**, `/api/nodes` 12 000 →
**159 ms**, et le CPU de l'orchestrateur **205 % → 77 %**.

---

## `/api/nodes` : 12-16 s → 108 ms, en cessant de confirmer qu'un nœud mort est mort — 2026-08-19

La route qui alimente la page Monitoring — pollée toutes les 5 s — prenait **12 à 16 secondes à
chaque appel**. Un seul nœud éteint (r620-1) en était la cause entière : chaque tentative de le
joindre échoue au bout de son timeout de connexion (~3 s), et la requête en enchaînait **seize**,
en série. Trois défauts empilés, corrigés chacun à son étage.

**Le repli ssh n'était pas seulement lent, il était faux.** Quand l'inventaire par agent échoue,
`node_images_state` retombait sur `ssh docker image inspect`, une fois par image attendue. Or sur
un nœud enrôlé façon B3-1 il n'y a plus de root-SSH depuis le contrôleur : cette commande répond
« absente » quoi qu'il arrive — le code le documentait déjà ailleurs (`_present_on_build_target`).
On payait donc 3,1 s par image, 9,4 s au total, pour une réponse sans valeur. Le repli est
désormais réservé aux nœuds **sans agent**, où il est effectivement autoritatif ; ailleurs la même
valeur est rendue immédiatement, marquée `unknown` pour que « pas pu vérifier » ne se confonde pas
avec « vérifié absent ».

**Un cache négatif oublié.** La carte NUMA d'un nœud ne retente qu'une fois par minute quand elle
échoue — le fichier explique très bien pourquoi. La carte HT, lue juste à côté et de même nature
(de la topologie matérielle), n'avait pas ce garde-fou : elle repayait le timeout à chaque appel.
Elle suit maintenant exactement le même patron.

**Un disjoncteur dans le transport.** Le fond du problème est plus général qu'`/api/nodes` : rien
n'empêchait N appelants de redécouvrir chacun à son tour, au prix d'un timeout, qu'un agent ne
répond pas. `node_driver._request` mémorise donc l'échec de **transport** 30 s et rend
immédiatement la même erreur. Trois garde-fous, parce qu'un disjoncteur qui ment est pire que la
lenteur qu'il évite :

- un `HTTPError` **n'arme pas** le disjoncteur — l'agent a répondu, donc il est vivant ;
- les **sondes de liveness** (`health()`, `ping()`) l'ignorent : ce sont elles qui ont pour métier
  de savoir si le nœud est là. Sans cette exception, le disjoncteur se serait rearmé sur son propre
  souvenir et un nœud revenu n'aurait plus jamais été redécouvert ;
- tout succès l'efface, pour tous les appelants à la fois. La reprise ne dépend donc pas de la durée
  de la fenêtre : le sampler de santé sonde toutes les ~5 s et rouvre la voie dès son premier succès.

Ce dernier point a été **vérifié de bout en bout** (disjoncteur armé → sonde en bypass qui réussit →
appelant normal qui repasse), pas seulement relu.

Enfin, l'enrichissement par nœud de la route s'exécute **en parallèle** : un nœud lent ne fait plus
attendre les autres.

Résultat mesuré, hors contention : **12 300 ms → 108 ms** (×114). `/api/home/summary` en profite au
passage (153 → 93 ms), les mêmes nœuds étant interrogés par les deux routes.

> Ce que la mesure désigne maintenant, et qui n'est pas fait : `db_get_alerts(limit=1000)`, appelé à
> chaque `/api/home/summary` pour en tirer 5 alertes saillantes et un total, est devenu le premier
> poste de CPU du contrôleur. Et il reste une trentaine de `setInterval` non migrés vers `MXLPoll`,
> dont `static/source_labels.js` (2 s, chargé sur TOUTES les pages) qui pèse au 3ᵉ rang.

---

## Une alerte allumée en permanence, et aveugle là où elle comptait — 2026-08-18

La page Câbles porte une deuxième ligne sous la cadence d'un nœud : la **cadence de contenu neuf**.
Elle répond à une question que la cadence de composition ne pose pas — un mur peut composer 50 fois
par seconde à partir de tuiles inchangées, et publier 50 fps en toute honnêteté pendant que l'aval,
qui n'émet que sur changement, sort à 38. Un seul défaut de conception lui faisait produire deux
symptômes opposés.

**Elle était allumée en permanence.** La référence de comparaison était la cadence de composition du
nœud **lui-même**. Sur une installation dont les sources sont en 1080i25 et les murs en 1080p50, le
contenu neuf ne peut structurellement pas dépasser 25 : l'avertissement ne pouvait jamais s'éteindre.
Les quatre shards d'un parc par ailleurs sain — 50 fps tenus, zéro trame perdue, moteur à 16/16 —
l'affichaient en continu alors qu'ils relaient ~100 % de la matière disponible (24,7 sur 24,9). Le
commentaire du code énonçait pourtant la règle : *une ligne qui s'affiche toujours n'alerte personne*.

**Et elle était muette là où elle aurait servi.** Les entrées d'un assembleur de mur shardé ne sont
pas des sources, ce sont ses shards — qui réémettent un grain à chaque créneau de 20 ms que leur
contenu ait changé ou non. « Une entrée a avancé » y est donc toujours vrai. Un shard qui gèlerait
son image en continuant d'émettre laisserait les deux sorties 2110 parfaitement saines : 50 fps,
0 trame perdue, aucune alerte — pendant que la tuile serait figée à l'antenne. C'est exactement le
scénario pour lequel la métrique avait été écrite.

La référence est désormais la cadence de contenu neuf **attendue en amont**, calculée côté serveur
(seul endroit qui connaisse la topologie et le format déclaré des sources) : cadence trame de la
source pour une entrée directe, attente du producteur propagée d'étage en étage pour une entrée
interne au tissu. On propage l'**attente** et non la mesure — sinon un shard qui décroche fait
baisser d'autant la référence de son assembleur, et le défaut s'annule lui-même. La comparaison
retient le **maximum** sur les entrées : si une entrée avance à 25 Hz, le nœud doit relayer au moins
25 fois par seconde ou il jette de la matière. C'est un minorant qui ne demande aucun modèle de
phase entre les entrées.

Un mur shardé porte en plus le verdict de son **maillon faible**, nommé : le défaut d'un shard
s'affiche sur le mur, pas sur un conteneur interne replié dans l'interface — là où personne ne le
verrait. Enfin, une tolérance apparaît, contrairement à la règle d'origine : elle se défendait tant
que les deux termes sortaient du même compteur, elle n'a plus de sens dès qu'on compare deux mesures
indépendantes. Elle est large à dessein, les vrais défauts de ce genre étant grossiers — moitié de
cadence, gel complet — jamais marginaux. Vérifiable hors ligne : `tools/verif_contenu_neuf.py`.

**Troisième alerte, même famille, éteinte à la racine.** « Pyramide nœud 5 : capacité insuffisante
pour câbler 3 source(s) », toutes les 17 minutes, indéfiniment. Le multiview publiait un besoin de
proxy pour *toute* source câblée, sans vérifier que la tuile réduisait quoi que ce soit. Un
assembleur recopiant ses shards 1:1 réclamait donc à la pyramide des proxies aux dimensions exactes
de la source — des copies inutiles, qu'une pyramide déjà pleine de ses seize sources utiles ne
pouvait pas servir. Une alarme insatisfaisable par construction, que l'exploitant ne pouvait ni
corriger ni faire taire. Une tuile ne demande plus de proxy quand elle ne réduit pas (multiview
0.113.0).

---

## Les journaux : chaque ligne écrite deux fois, une rotation qui n'a jamais tourné — 2026-08-15

Trois défauts qui se tenaient par la main, et personne pour les dire.

**Chaque ligne était écrite deux fois.** L'orchestrateur pose deux handlers de journalisation, un
fichier et une console. Sous systemd, la console est redirigée… vers le même fichier
(`StandardOutput=append:/opt/bobistudio/bobistudio.log`). Résultat : 291 Mo dont exactement la
moitié était des doublons exacts. La console n'est désormais posée que si la sortie est un
**terminal** (`isatty`) : en service, le fichier n'a qu'un seul écrivain ; en développement, les
lignes défilent comme avant. La règle ne dépend plus de la façon dont l'unité systemd est écrite,
donc aucune redirection ne peut recréer le doublon.

**Et le fichier n'est plus partagé du tout.** L'unité envoie sa sortie standard à **journald**
(`StandardOutput=journal`), plus dans le journal applicatif. Ce n'était pas qu'une question de
doublons : `append:` ouvre le fichier une fois au démarrage du service et garde son descripteur,
donc après une rotation — un renommage — systemd continue d'écrire dans **l'archive**, que plus rien
ne borne. C'est ce qui explique un `bobistudio.log.3` de 2,3 Go, la taille de la tempête du
11 juillet, survivant à un réglage qui promettait 2 Go au total. Le plafond réglé n'était pas une
borne tant que deux processus écrivaient dans le même fichier ; il l'est maintenant. journald ne
récupère que ce qui **échappe** à la journalisation applicative — traceback survenu avant
l'installation des handlers, démarrage de waitress, `stderr` d'une bibliothèque C —, lisible par
`journalctl -u bobistudio`.

**La rotation par temps ne s'est jamais déclenchée.** Son point de départ était l'heure de démarrage
du process, pas la date du fichier : « rotation tous les 7 jours » exigeait donc que l'orchestrateur
tourne sept jours d'affilée, alors qu'il redémarre à chaque livraison. Les archives étaient figées
au 11 juillet, dont une de 2,3 Go — soit **3,5 Go** au total pour un réglage qui en promettait 2.
La référence est maintenant la date de modification du fichier.

**Rien ne surveillait tout ça.** Un contrôle périodique (5 min) est branché dans la boucle de
surveillance, et il pose deux alarmes `disk` distinctes, à la transition :

- **la place** — l'espace libre de la partition passe sous un plancher réglé (5 Go par défaut) :
  `error`. C'est la panne qui emporte l'orchestrateur entier, vécue le 11 juillet, et un nœud avant
  lui ;
- **le plafond** — les journaux occupent plus que ce que les réglages promettent
  (`taille max × (archives + 1)`) : `warning`. Ce n'est pas « il y a beaucoup de logs », c'est **la
  rotation ne fait pas ce qu'elle annonce**. L'alarme porte sur l'écart à l'intention, pas sur une
  valeur absolue — c'est la règle que ce projet s'impose depuis la rétrospective d'observabilité.

**Réglages.** La rotation gagne un interrupteur explicite : la désactiver est un choix légitime
(capturer un incident long sans en perdre le début), mais ce doit être un choix, pas un état dans
lequel on tombe. Le plancher d'espace libre est réglable à côté. Et les quatre valeurs s'appliquent
désormais **à chaud** : elles ne prenaient effet qu'au redémarrage, si bien qu'un opérateur pouvait
poser le bon réglage, lire « enregistré », et regarder le fichier continuer de grimper. L'encart
d'occupation affiche maintenant l'occupation *et* le plafond promis, en rouge quand les deux
divergent.

---

## Une alerte qui se répète n'est plus une alerte — 2026-08-15

Relevé du jour : **10 068 lignes** dans le journal d'alertes, dont **~80 % faites de quatre
messages**. Un lien RDMA vers un nœud éteint depuis trois jours en avait écrit 1 145 à lui seul ;
trois liens audio qui se ré-établissaient en boucle, 680 chacun ; un câblage de pyramide reperdu
toutes les 2 min 15, 792. Pendant ce temps, un « TX #1 : source INSTABLE » survenu l'après-midi
était déjà enfoui. Le journal ne manquait pas d'information : il en avait trop de la même.

**Ce qui change.** Une alerte émise par la MACHINE passe désormais par un anti-rebond, avec la règle
que `app/placement.py` appliquait déjà à son échelle — *on alerte à la transition, on compte
ensuite* :

- la **première** occurrence d'un symptôme s'écrit immédiatement, telle quelle ;
- ses répétitions dans la fenêtre (réglage `alerts_antirebond_s`, 15 min par défaut, 0 = désactivé)
  sont **comptées**, pas écrites — ni au journal, ni vers l'e-mail/webhook ;
- si le symptôme dure, **une** ligne de résumé par fenêtre : « … — répété 47 fois depuis 14:02 » ;
- après une fenêtre entière sans occurrence, l'épisode est **clos** : la reprise redevient une
  transition, donc réécrite sur-le-champ. Un incident qui repart après accalmie n'est jamais étouffé.

**Ce qui n'est PAS replié**, et c'est le cœur du réglage. Le `niveau` fait partie de la signature :
une aggravation `info → warning → error` est une transition, écrite sans délai. Une alerte portant
un **acteur** ne l'est jamais non plus — `alerts` est aussi le journal « qui a fait quoi », et deux
gestes humains identiques sont deux faits distincts. Enfin, la signature distingue un **identifiant**
d'une **quantité** : `#3800`, `audio_1`, `dl360-1` séparent deux liens, tandis que « depuis 92 479 s »
et « depuis 93 927 s » sont reconnus comme le même symptôme. Sans cette distinction, l'anti-rebond
aurait replié trois liens différents sur un seul.

**L'étouffement se voit.** Une table `alert_episodes` porte, par symptôme en cours, le compte réel
et le nombre d'occurrences tues depuis la dernière ligne — lisibles sur `GET /api/alerts/episodes`.
Un repli qu'on ne peut pas inspecter serait un échec silencieux de plus.

---

## Ne rien tenter, ni annoncer, vers un nœud éteint — 2026-08-15

La réconciliation RDMA retentait chaque minute d'établir un lien vers `r620-1`, éteint depuis trois
jours, et écrivait une alerte `error` à chaque échec. Un nœud `down` ne répondra ni à l'écriture du
`flow.json` ni au `docker run` : la tentative ne le réveille pas, elle ne fait que du bruit — 920
lignes en 24 h pour ce seul lien, un quart du fil.

L'état correct est `waiting` (« rien n'est cassé, on attend le retour »), pas `error`, et le motif
ne s'écrit qu'à la **transition**. La garde est posée aux deux endroits qui comptent : dans
`_etablir()` en ceinture, et surtout dans `verifier_liens()` — c'est **là** qu'est prise la décision
de ré-établir, et là que naissait l'autre moitié du bruit, l'annonce « source disponible,
ré-établissement automatique ». La placer dans `_etablir()` seul n'aurait fait que changer le
niveau du bruit, d'`error` à `info`. Le lien repart de lui-même au retour du nœud : le tour de
réconciliation suivant le voit `up` et enchaîne.

---

## Un nœud ne peut pas joindre ses propres conteneurs — 2026-08-14

Le chien de garde livré la veille reposait sur une hypothèse jamais vérifiée : que l'agent-nœud
pouvait atteindre le `:8081` des conteneurs de sa propre machine. Il ne le peut pas. Nos conteneurs
sont en macvlan, et une interface macvlan enfant ne parle jamais à la pile de son interface
**parente**. Mesuré sur dl360-1 : 100 % de perte vers son propre conteneur, 0 % vers celui d'un
autre nœud. Un nœud joint les conteneurs de ses voisins, jamais les siens. Personne ne l'avait
rencontré parce que le contrôleur, lui, est sur une autre machine.

Deux issues se présentaient. Un **shim macvlan** sur l'hôte aurait évité de retoucher les images —
mais le pool conteneurs (x.x.x.101-199) partage son `/24` avec les nœuds, le contrôleur et la
passerelle : une route de sous-réseau y capturerait le plan de contrôle. Il aurait fallu des routes
`/32` par conteneur, posées à la création, retirées à la destruction, reconstruites au démarrage,
plus une IP réservée par nœud — un gestionnaire de routes tenu par l'agent, sur la patte qui porte
le contrôle. Plus de machinerie que ce qu'il devait simplifier, et une panne plus grave.

**Ce qui change.** L'agent-nœud exécute sa sonde **dans** le conteneur (`docker exec`) et appelle
`127.0.0.1:8081` depuis l'espace de noms réseau de celui-ci. Le matériel TLS et le jeton sont déjà
là (`/etc/bobi-tls`, `$MXL_AGENT_TOKEN`) : rien à distribuer, rien à router. Côté conteneur, une
connexion venant du loopback est acceptée sur les deux mêmes endpoints que le nœud — `GET /status`
et `POST /start` — sans vérification d'identité client.

Cette exemption n'accorde **aucun pouvoir nouveau** : atteindre le loopback d'un conteneur exige
d'y exécuter du code, donc d'avoir Docker, donc d'être root sur l'hôte, qui peut déjà tout faire à
ce conteneur. Elle nomme un chemin qui existait. Et un paquet venu du réseau avec une source
127.0.0.1 n'arrive pas là — le noyau jette les sources martiennes sur une interface non-loopback.

L'ouverture par certificat de nœud reste en place : elle redevient le bon chemin partout où l'hôte
peut joindre ses conteneurs (bridge, ipvlan, topologie séparée). Le mécanisme, lui, ne dépend plus
du tout de la topologie réseau.

---

## Le nœud veille sur ses scripts quand le contrôleur n'est plus là — 2026-08-13

Un conteneur peut très bien tourner pendant que le script qu'il héberge est mort. Docker n'y voit
rien : sa politique `--restart` surveille le PID 1, c'est-à-dire l'agent par-conteneur, qui se
porte bien. Ce cas — l'état `script_stopped` — n'était relevé que par la boucle du contrôleur, avec
tout ce qu'il faut : backoff, seuil d'alerte, quarantaine crash-loop. Contrôleur absent, plus
personne. Le média déjà en cours continuait, mais la **première anomalie devenait définitive**.

Le seul survivant sur place, l'agent-nœud, était refusé à la porte — et pour une bonne raison. Les
certificats de conteneur portent `serverAuth` **et** `clientAuth` : ne vérifier que « signé par
notre CA » laisserait la clé d'un seul conteneur piloter tous les agents de la flotte. L'agent
par-conteneur exigeait donc `CN=bobi-controller`, et rien d'autre.

**Ce qui change.** Une troisième identité est reconnue, celle de l'agent-nœud (`bobi://node/<id>`),
**restreinte à deux endpoints** : constater (`GET /status`) et relancer (`POST /start`). `/deploy`,
`/stop` et `/nmos/subscribe` restent au contrôleur seul, et les certificats de conteneur restent
refusés. La distinction ne coûtait rien à établir : l'URI est fixée par le contrôleur à la
signature, jamais recopiée d'un CSR — un CN qui imite une URI de nœud ne passe pas.

Côté nœud, le chien de garde n'agit que sous trois conditions cumulées : le contrôleur n'a rien
demandé depuis 60 secondes, le script tournait au tour précédent, et le plafond de cinq tentatives
n'est pas atteint. La deuxième est la plus importante : elle distingue « mort pendant la coupure »
de « arrêté exprès avant elle », sans avoir à persister ni synchroniser un drapeau qui mentirait au
premier redémarrage.

Trois refus délibérés :

- **il ne redéploie pas.** Reconstruire un script exige `deploy_config` et le registre de plugins,
  donc la base. L'agent ne l'a pas et ne doit pas l'avoir : script absent du disque, il constate et
  attend.
- **il ne touche pas aux `--network host`.** Le moteur 2110 a son propre `:8081`, hors de ce
  contrat, et relancer un moteur DPDK à l'aveugle masquerait une panne matérielle en boucle muette.
- **il ne décide rien.** Il maintient la dernière intention connue ; changer l'intention reste le
  privilège du contrôleur. Un second décideur serait plus dangereux que le trou qu'on bouche.

Tout ce qu'il fait remonte dans `/v1/health` : le contrôleur découvre à son retour ce qui s'est
passé en son absence. Une action muette serait pire que pas d'action.

**Au passage, un défaut d'installation.** La bascule mTLS est automatique à l'enrôlement depuis le
4 juillet. Elle testait pourtant `if node_driver.rotate_tls(node):` — or la fonction retourne un
tuple `(ok, msg)`, toujours vrai. Depuis le premier jour, l'enrôlement annonçait « mTLS activé
(HTTPS) » même quand la bascule venait d'échouer et que le nœud restait en HTTP. C'est ainsi que
deux nœuds sur quatre — ceux qui portent 23 des 26 conteneurs — n'avaient aucun certificat, sans
que rien ne le signale. Le journal affirmait le contraire de la base. Corrigé, et les quatre nœuds
portent désormais leur identité.

---

## Le lot de synchro RDMA : un réglage qu'on espérait, désormais un fait qu'on vérifie — 2026-08-12

`maxSyncBatchSizeHint` fixe combien de tranches l'initiateur RDMA accumule avant de transférer. Au
défaut du SDK (= toutes), la réplique n'est lisible qu'une fois la trame **entière** arrivée : le
consommateur d'en face paie une trame pleine de latence (22,6 ms contre 0,06 à petit lot, mesuré au
banc le 9 août). Le réglage existe, il est à 2, et il est bien poussé aux deux bouts.

Sauf qu'une option de flux se fixe **à la création**. Une cible qui trouve la réplique déjà là la
réattache et ignore `--flow-options` : la réplique garde le lot avec lequel elle est née. Audit du
parc : **37 des 41 cibles en service** journalisaient « Reusing existing flow ». Sans conséquence ce
jour-là — toutes les répliques dataient d'après le correctif du 11 août, et la chaîne mesurait bien
0,08 trame par sens — mais rien, nulle part, n'aurait dit le contraire : ni journal, ni compteur, ni
alerte. Le réglage affiché disait 2 pendant que le terrain aurait pu faire 30.

**Ce qui change.** Le lot réellement posé est désormais enregistré par lien, au moment où la cible
crée la réplique — le seul instant où l'option est lue. À chaque ré-établissement il est confronté
au réglage, et une divergence se traite comme une définition de flux divergente : la réplique est
supprimée pour que la cible la recrée avec le bon lot. La colonne « Lot » de Monitoring → RDMA
affiche la valeur, `défaut` quand aucune option n'a été posée, `?` quand elle est inconnue.

Deux refus délibérés, parce qu'un faux témoin est pire que pas de témoin :

- une réplique **réattachée** n'inscrit rien. Y recopier le réglage ferait une colonne qui confirme
  toujours, c'est-à-dire qui ne mesure rien.
- une sonde **en échec** (agent injoignable) ne vaut pas « flux absent ». La distinction n'existait
  pas ; elle ne coûtait rien tant que la réponse ne servait qu'à décider d'une purge, elle devient
  nécessaire dès qu'on en tire une valeur qu'on va inscrire.

Les liens antérieurs restent à « inconnu » et ne sont pas purgés : couper la réplication du parc sur
une supposition coûterait plus cher que le défaut qu'on cherche. Ils se renseignent d'eux-mêmes à
leur prochaine recréation.

---


## `/api/home/summary` : 624 → 153 ms, en interrogeant moins et en parallèle — 2026-08-13

Suite du correctif de poll de la veille. Une fois les requêtes empilées supprimées, restait le
coût de la route elle-même : **624 ms**, pollée toutes les 2 s par trois pages.

**Le gros morceau, c'était l'attente d'un conteneur mort.** La route lit l'état live des plugins
sur `:8082/state`, un conteneur après l'autre, avec 0,5 s de timeout chacun. Un seul conteneur
`unreachable` — il y en avait un, `color-corrector-NJL` — coûtait donc 500 des 624 ms, **à chaque
poll**, pour un `{}` que l'orchestrateur connaissait d'avance : il affiche lui-même ce conteneur
comme injoignable dans le panneau Points d'attention. Deux corrections qui se complètent :

- on n'interroge plus que les conteneurs dont le statut dit qu'ils peuvent répondre (`running`,
  `script_stopped`) — interroger les autres, c'est payer un timeout pour confirmer ce qu'on sait ;
- les appels restants partent **en parallèle** (`_fetch_plugin_states`), si bien que le pire cas
  n'est plus la somme des timeouts mais celui de l'appel le plus lent. Ces threads attendent le
  réseau : le parallélisme y est gratuit côté GIL.

Deux redondances au passage : le **registre NMOS** était scanné et re-parsé *deux fois* par
requête (`multicast_conflicts` puis `plages_epuisees`, chacun le relisant pour son compte) — il est
maintenant lu une fois et passé aux deux ; et le **wiring** de chaque plugin, désormais dérivé par
la pré-passe, n'est plus recalculé par la boucle qui suit.

Vérifié par comparaison des charges utiles complètes avant/après : **identiques** au champ
`first_seen_ts` près. 624 ms → **153 ms**.

Les neuf `setInterval` de la page **Monitoring** passent aussi à `MXLPoll` — c'était la page du
parc qui en portait le plus, donc celle où l'empilement coûtait le plus cher.

> Reste ouvert, découvert en mesurant : **`/api/nodes` prend 12 à 16 s**, à chaque appel et sans
> mise en cache efficace — `node_images_state` retombe sur un `ssh docker image inspect` par nœud
> (~3 s pièce) quand l'inventaire par agent n'est pas disponible. Sans rapport avec ce chantier ;
> la page Monitoring le poll toutes les 5 s (sans empiler, désormais).

---

## Un poll qui ne s'attendait pas lui-même saturait tout l'orchestrateur — 2026-08-12

Symptôme : après un rechargement, la page Câbles restait des dizaines de secondes sans un seul
câble. La mesure a déplacé le problème : ce n'était pas la page. `/api/home/summary` répondait en
**11,4 s**, `/api/alerts` en 5,2 s, `/api/nodes` en 3,1 s — *toutes* les pages étaient touchées, et
l'orchestrateur brûlait 221 % de CPU en permanence.

Le chiffre qui tranche : la même route appelée **en direct dans le process** prend **0,18 s**.
Facteur 65 entre le coût de la route et son temps de réponse réel. Ce n'était donc pas la route
qui était lente, c'était le serveur qui n'arrivait plus à répondre.

**La cause.** `setInterval(f, 2000)` re-tire toutes les 2 s **sans attendre la réponse
précédente**. Tant que le serveur répond vite, ça ne se voit pas — c'est pour ça que ça a tenu des
mois. Le jour où une réponse dépasse l'intervalle, cinq requêtes sont déjà en vol, à ~180 ms de CPU
serveur chacune, et la boucle se referme : plus c'est lent, plus ça empile, plus c'est lent. Rien
ne ramène le système à l'équilibre, et deux onglets ouverts suffisent à l'y maintenir.

**Le correctif** : `window.MXLPoll(fn, ms)` (layout.html), où le délai ne repart qu'une fois la
passe **terminée**. Le rythme demandé devient un rythme *minimal entre deux fins de passe* — au
pire on poll moins souvent, jamais en parallèle de soi-même. C'est la seule lecture honnête d'un
intervalle quand la réponse peut durer plus longtemps que lui. Posé sur les deux pages qui
appellent `/api/home/summary` toutes les 2 s (Câbles, Accueil). Au passage, la page Câbles lançait
un `rafraichirCables()` nu juste avant de démarrer son poll : deux requêtes concurrentes à chaque
chargement.

Après correction : **0,15-0,26 s** sur `/api/home/summary`, 0,03 s sur `/api/alerts`, CPU à 74 %.

Corrigé aussi, et c'est le défaut d'affichage que la lenteur masquait : dans `drawTopoEdges`,
l'effacement du SVG (`svg.innerHTML = ''`) était placé **avant** le garde-fou qui abandonne la
passe quand le canvas n'est pas encore dimensionné. Une seule mesure prise avant la mise en page
vidait donc les câbles sans rien redessiner.

---

## Page Câbles : le RDMA se lit sur le trait, plus en étiquettes empilées — 2026-08-12

Chaque câble inter-nœud portait une étiquette « RDMA » posée en son milieu. Sur une topologie
réelle, une dizaine de flux répliqués suffisaient à empiler ces étiquettes les unes sur les
autres, et le graphe devenait illisible là où il fallait justement lire.

Le transport RDMA est une propriété du **chemin**, pas de l'essence du signal : il se marque
désormais par un **second trait ambré en pointillé fin, superposé au câble** — dont les tirets
défilent quand la réplication tourne. Le trait du dessous garde son identité (le pointillé large
de l'ANC reste distinct), et vingt surlignages restent lisibles parce qu'ils suivent chacun leur
câble au lieu de se disputer le même point de l'écran.

Trois modes dans la barre d'outils, mémorisés : **Trait** (défaut), **Étiquette** (le texte en
plus, pour l'ancien rendu) et **Masqué**. Avec une exception qui n'est pas négociable : un lien
RDMA **manquant** — le consommateur lit un flux qui n'existe pas sur son nœud — reste signalé en
rouge clignotant dans les trois modes. C'est une alarme, pas une décoration ; elle ne se masque
pas depuis un réglage d'affichage.

Les **délais** affichés sur les câbles gagnent la même option : « Par étape / Cumulé » se
complète d'un **Masqué**, qui coupe les étiquettes de latence sans faire oublier le mode choisi.
Même exception : une entrée **gelée** reste étiquetée « figé » dans tous les cas.

---

## La cadence affichée répond à « est-ce tenu ? », pas « quel nombre est sorti » — 2026-08-10

Un conteneur parfaitement sain affichait « 49,8 fps », puis « 50,1 », puis « 49,9 ». Le chiffre
n'était pas faux, il était **trop précis** : les plugins comptent des trames entières sur une
fenêtre d'une seconde, ce qui vaut ±1 image de troncature — soit ±1 fps — et on arrondissait le
résultat au dixième. On publiait une décimale que la mesure ne porte pas. L'exploitant lisait un
défaut là où il n'y en avait aucun, et un chiffre qui bouge en permanence finit par n'être plus
regardé du tout : c'est le même mécanisme qui avait imposé le plancher glissant aux avertissements.

Deux corrections, indépendantes.

**La mesure.** Le réflexe aurait été d'allonger la fenêtre dans les plugins ; c'est un piège, car
une fenêtre plus longue retarde d'autant la chute à zéro quand une source gèle — exactement ce que
la règle du débit glissant a été écrite pour empêcher. Or l'orchestrateur dispose déjà des deux
termes exacts : le compteur cumulé `frame_index` et l'horloge de son propre poll (~5 s). La cadence
est désormais recalculée là, par Δindex / Δt, ce qui divise le bruit par cinq **sans toucher à un
seul plugin et sans rien retarder**. Elle alimente aussi le plancher glissant, qui frôlait sinon le
seuil de « cadence tenue » (49/50 = 0,98) alors que rien ne décrochait.

**L'affichage.** Le badge montre la cadence **demandée** tant qu'elle est tenue (« 50 fps »), et la
mesure seulement lorsqu'elle s'en écarte, à côté de l'intention (« 43/50 fps », en ton
d'avertissement). Le verdict « tenue » est rendu par l'orchestrateur, avec la cible et le seuil de
l'alarme de sous-cadence : le badge et l'alarme ne peuvent plus se contredire. Un conteneur dont le
déploiement ne déclare aucune cadence s'affiche « ~50 fps » — le tilde dit qu'on ne compare à rien.

Le plugin `delay` (0.12.0) reçoit le même traitement dans son propre panneau : il publie désormais
`fps_nominal` par canal, relevé sur le format du flux d'entrée. Un délai est un relais — il n'a pas
de cadence propre, l'intention qu'il doit tenir est celle de sa source.

---

## La profondeur du bus MXL est une durée, pas un compte de trames — 2026-08-09

Un retard de lecture sur le bus MXL a longtemps été pensé en trames : combien de cases un
producteur laisse-t-il derrière lui avant d'écraser ce qu'un consommateur n'a pas encore lu.
En repartant du SDK, la vraie unité réglée par MXL est une **durée** — 200 ms par défaut, posée
par domaine, jamais par flux ni par conteneur — dont le nombre de grains n'est qu'une traduction
qui varie avec la cadence (10 cases à 50p, 5 à 25p pour la même fenêtre de 200 ms). Notre ancien
réglage `shm_video_ring`, un compte fixe hérité du shm maison pré-MXL, n'a jamais agi sur cette
profondeur et invitait à raisonner dans la mauvaise unité.

L'enjeu n'est pas la mémoire (un flux pèse quelques centaines de Mo sur des dizaines de Go de
tmpfs) : c'est qu'en dessous de la fenêtre, un retard de lecture se paie en latence ; au-delà, il
corrompt la ligne de temps du consommateur, qui retombe sur des grains déjà réécrits. C'est ce qui
s'est produit sur un flux répliqué par RDMA en retard de 400 ms à 1 s : sa tête ne traînait plus,
elle reculait. Documenté dans `docs/reference/MXL_INTEROP.md`, section « Profondeur des ring
buffers MXL ».

---

## Nos flux étaient invisibles pour tout ce qui n'est pas nous — 2026-08-08

Un récepteur AES67, Ravenna ou Dante ne parle pas NMOS. Il ne sait s'abonner qu'à ce qui lui est
**annoncé**, sur un groupe multicast, par un protocole que nous n'implémentions pas : SAP. Nos
sorties 2110 étaient donc structurellement inatteignables pour ces équipements — non pas
incompatibles, simplement invisibles. Dans l'autre sens, prendre un de leurs flux se faisait au
copier-coller de SDP, à la main, en allant le chercher dans leur propre logiciel.

Réglages → Protocoles → SAP/SDP ouvre les deux sens. On coche les sorties à publier : leur SDP part
sur le groupe SAP toutes les trente secondes, et les récepteurs d'en face les voient apparaître dans
leur liste. Décocher émet un paquet de suppression, pour que l'entrée disparaisse aussi chez eux au
lieu d'y traîner en fantôme. En réception, tout ce qui s'annonce sur le réseau est catalogué et se
route sur une entrée du moteur d'un clic.

Le tableau ne se contente pas de lister : il confronte chaque flux découvert à ce que notre moteur
sait réellement recevoir, et à l'horloge que nos propres annonces déclarent. C'est là que se joue
l'essentiel de l'interopérabilité audio, et notamment le piège le plus coûteux du Dante en mode
AES67 : il sort en général sur le **domaine PTP 0** quand un plateau ST 2110 tourne en 127. Deux
équipements parfaitement conformes, des formats identiques, et pas un échantillon qui passe — sans
que rien nulle part ne dise pourquoi. C'est désormais écrit en toutes lettres sur la ligne du flux,
avant de tenter l'abonnement.

Une limite qui n'est pas de ce service : SAP passe par la pile réseau du noyau. Un port 2110 confié
au DPDK n'a plus d'interface visible du noyau et reste donc hors de portée. En pratique ce n'est pas
gênant — les réseaux audio AES67 et Dante sont des réseaux commutés ordinaires, où le contrôleur a
sa patte.

---

## Haute disponibilité : couper l'actif ne provoquait rien, pas même une alarme — 2026-08-07

Test grandeur nature : service de l'actif arrêté, et sur le contrôleur de secours, rien. C'était
conforme à la conception — la bascule du pilotage est volontairement manuelle — mais la moitié du
problème n'en était pas moins réelle : le secours *savait* que plus rien n'arrivait et n'en disait
rien. On découvrait la panne en constatant que la production ne répondait plus.

Le contrôleur en veille sonde désormais l'actif toutes les quinze secondes. Au bout d'une minute
sans réponse, un badge rouge clignote dans la barre de navigation — sur toutes les pages, parce
qu'un opérateur peut arriver n'importe où — et mène directement au bouton « Promouvoir ». Il ne se
promeut toujours pas de lui-même : sans témoin tiers, un contrôleur isolé du réseau ne peut pas
distinguer « l'autre est mort » de « je suis seul », et se promouvoir dans le doute donnerait deux
pilotes pour une même production.

L'adresse de management, elle, bascule maintenant toute seule si on active keepalived (Réglages →
Haute disponibilité). Elle suit le rôle lors d'une bascule planifiée, et migre en quelques secondes
quand l'orchestrateur cesse de répondre. Elle mène alors à un contrôleur en lecture seule : c'est
voulu — une adresse qui mène au bouton vaut mieux qu'une adresse qui ne mène nulle part.

---

## Haute disponibilité : le secret de réplication n'était réglable nulle part — 2026-08-06

Configurer une paire de contrôleurs se terminait invariablement sur « ↑ dernier push ÉCHEC — token
absent ». Le secret partagé qui autorise la réplication n'existait que dans la section « Mise à
jour entre instances », où il n'était que **généré** — et le champ était en lecture seule. Or ce
secret doit être **le même des deux côtés** : l'actif le présente, le standby le compare au sien.
Deux générations séparées ne pouvaient donc jamais correspondre, et rien ne permettait de recopier
l'une dans l'autre.

Le champ est désormais saisissable, et présent dans la section Haute disponibilité elle-même :
on génère d'un côté, on recopie de l'autre. Il reste accessible sur un contrôleur déjà passé en
veille. Le statut annonce l'absence de secret **avant** le premier essai plutôt qu'après, et un
refus du standby ne se présente plus comme une panne réseau : il dit que les deux valeurs diffèrent.

---

## La page Aide n'affichait pas le guide d'installation — 2026-08-05

L'article « Installer et mettre en service » restait sur « Chargement… ». Le guide était pourtant
bien servi : la page le demandait sous un nom que rien ne portait, et abandonnait sans le dire —
l'échec avait l'apparence d'une lenteur.

Corrigé, et la panne suivante avec : le contenu, une fois chargé, disparaissait dès qu'on quittait
l'article puis y revenait, ou simplement en effaçant la recherche. Chaque affichage d'article
reconstruit son corps ; le document n'était rempli qu'à la navigation, et jamais aux autres
occasions. Le remplissage se fait désormais à chaque affichage, quelle qu'en soit la voie, sans
re-télécharger le document. Le changelog que vous lisez souffrait du même défaut.

---

## Préparer un patch avant la mise en service — 2026-08-05

Câbler une source dont le format ne correspond pas à ce qu'attend le consommateur était refusé. La
règle est juste : une source 25 images/s dans un mélangeur réglé en 50 ne donne pas une image
approximative, elle donne une image fausse, et elle la donne sans rien dire. Mais la règle
s'appliquait aussi à ce qui n'était pas encore là.

Pour juger, le système lit le format que porte réellement le flux. Quand la source n'est pas encore
en service — machine pas démarrée, réception sans signal — ce flux n'existe pas, et il retombait
alors sur le format *déclaré* de la source. Déclarer n'est pas mesurer : c'est une intention,
parfois périmée, jamais un constat. On refusait donc un câble sur une prédiction, ce qui interdisait
de préparer un patch avant la mise en service. Un geste d'exploitation parfaitement normal.

Désormais, ces deux situations sont distinguées. Écart constaté sur un flux qui existe : refus, comme
avant. Écart seulement prédit : le câble est posé, annoncé comme pré-câblage, et le système
**re-vérifie tout seul à l'apparition du flux**. S'il concorde, le câble est validé sans bruit ;
s'il ne concorde pas, l'écart est signalé en nommant les deux formats, et le câble reste en place —
on ne débranche pas une régie sans qu'elle l'ait demandé. Sans cette repasse, la tolérance aurait
troqué un refus explicite contre un échec silencieux.

Les sorties ST 2110, elles, gardent un refus sec, prédiction comprise : y câbler un format
divergent recrée la session d'émission et fige toutes les sorties de la carte pendant environ une
seconde. Là, le doute ne se pré-câble pas.

Deuxième correctif, sur la manière dont le refus se présente. Un écart de format ouvre normalement
une fenêtre qui montre les deux formats et propose les issues : insérer un convertisseur, réutiliser
une conversion déjà en place, ou câbler quand même. Cette fenêtre était déclenchée par une
comparaison faite dans le navigateur, à partir des formats *déclarés* de la topologie — alors que le
refus, lui, se prononce sur le format *réellement porté par le flux*. Quand les deux divergent — le
cas exact d'une réception 2110, dont le moteur annonce sa cadence globale — le navigateur ne voyait
aucun écart, et l'utilisateur n'obtenait qu'un message d'erreur dans un coin, sans porte de sortie.

Le refus transporte désormais sa propre résolution : les deux formats en cause voyagent avec lui, et
la même fenêtre s'ouvre, quelle que soit celle des deux comparaisons qui a détecté l'écart.

Corrigé au passage : ce message affichait « Erreur 409 (pas de détail) ». La raison était pourtant
bien envoyée — l'interface la perdait en relisant deux fois la réponse du serveur.

---

## Nos sorties 2110 promettaient une régularité que la carte ne pouvait pas tenir — 2026-08-04

Un flux ST 2110 annonce dans son SDP la classe de régularité de son émission : `TP=2110TPN` pour
narrow, la plus stricte. Ce n'est pas une décoration — un récepteur sérieux applique la fenêtre
correspondant à ce qu'on lui annonce, et refuse ou jette ce qui en sort. Nos sorties annonçaient
narrow. Elles n'étaient pas en mesure de le tenir.

La classe d'émission n'était transmise au moteur que sur les nœuds dont la carte est pilotée en
accès direct. Sur les autres — la majorité du parc — l'information était simplement absente, et le
moteur retombait sur sa valeur par défaut : narrow. Personne n'avait choisi cette valeur. Elle
s'appliquait par omission, sur des cartes dépourvues du limiteur de débit matériel qui est
précisément la seule mécanique capable de tenir narrow.

La différence entre l'annonce et la réalité ne se paie pas chez nous : elle se paie chez celui qui
reçoit. Sur un site, le récepteur avait compté cent trente-six millions de paquets « hors plage »
sur un seul abonnement — des paquets arrivés en dehors de la fenêtre qu'on lui avait fait attendre.

La classe annoncée est désormais déduite de ce dont le port est réellement capable. Sans limiteur
matériel, la sortie annonce `wide`, qui est la vérité. Un site qui a mesuré sa propre régularité
peut toujours déclarer narrow explicitement, et sa demande est respectée : le correctif retire une
promesse tacite, il n'enlève aucune liberté.

Le reste tient en un mot : rendre la chose visible. La classe annoncée par chaque port et la
présence ou non du limiteur matériel apparaissent maintenant dans l'état du moteur, et chaque port
écrit au démarrage la classe qu'il retient — et, s'il l'a dégradée, pourquoi. Une promesse
intenable qui n'apparaît dans aucun journal est indétectable en exploitation ; c'est exactement
ainsi que celle-ci a traversé des mois de production.

## Le même modèle de supervision, maintenant lisible en HTTP — 2026-08-05

La supervision des entrées/sorties 2110 livrée hier ne se lisait qu'en **IS-12**, c'est-à-dire par
une WebSocket qu'il faut ouvrir et tenir. Tous les contrôleurs ne veulent pas de ça : certains
outils de supervision, de recette ou d'inventaire préfèrent interroger une URL et s'en aller.

L'orchestrateur parle désormais aussi **IS-14**, la Configuration API. Ce n'est pas une seconde
implémentation : c'est le **même modèle**, les mêmes objets, les mêmes identifiants de propriétés,
servis en REST sous `/x-nmos/configuration/v1.0/`. Un objet s'y adresse par son chemin de rôles —
`root.receivers.receiver-<uuid>` — et l'on y lit une propriété, on en écrit une, on invoque une
méthode. Activable indépendamment d'IS-12, ou en même temps : le modèle n'est bâti qu'une fois,
quel que soit le nombre de protocoles qui le publient. Deux protocoles qui décriraient le même
appareil de deux façons seraient pires que rien.

IS-14 apporte en plus quelque chose qu'IS-12 n'a pas : la **sauvegarde et la restauration**. Un
contrôleur récupère l'état complet du modèle en une requête, et peut le réappliquer plus tard.
Avec une garantie qui compte : la restauration se **valide à blanc** avant d'être appliquée, et
cette validation ne modifie rien. On sait donc ce qu'une restauration ferait avant de la faire.

Ce que la restauration ne fait pas, elle le dit. Notre modèle est de la supervision : l'essentiel
de ses propriétés est en lecture seule. Une sauvegarde qui contient ces propriétés-là est acceptée,
mais chaque propriété non appliquée produit une remarque nominative dans la réponse — « propriété
en lecture seule », « propriété absente de cet objet ». Et un objet présent sur l'appareil mais
absent du jeu de sauvegarde est rapporté comme tel, plutôt que passé sous silence : une
restauration partielle doit se voir.

À activer dans Réglages → Protocoles → NMOS. Contrairement à IS-12, aucun port supplémentaire :
c'est du HTTP ordinaire, sur le port de l'interface, comme IS-04 et IS-05.

## Un groupe de production ne change plus dans le dos du contrôleur — 2026-08-04

Nos ressources NMOS portent depuis longtemps un *groupe naturel* (BCP-002-01) : c'est ce qui dit à
un contrôleur que « Rx 3 vidéo », « Rx 3 audio » et « Rx 3 ANC » forment un même ensemble, et c'est
sur cette information qu'un opérateur bâtit son nommage de production au moment du paramétrage.

Le registre NMOS pose que cette valeur est **immuable**, et il explique pourquoi : une fois que
l'opérateur a associé « caméra plateau 2 » à un groupe, personne ne viendra lui redemander son avis
si le groupe change de nom. Or chez nous le groupe dérivait du *préfixe de libellés* — un réglage
d'affichage que la page Réglages invite explicitement à modifier, en promettant qu'il ne casse
rien. La promesse était vraie pour les abonnements, fausse pour les groupes : changer le préfixe
réécrivait silencieusement le regroupement de tout le parc.

Les groupes sont désormais **figés à la création** de chaque ressource. Le préfixe reste modifiable
et continue de piloter les libellés ; les ressources créées ensuite porteront le nouveau préfixe,
les anciennes gardent leur groupe. Le texte de la page a été corrigé pour dire exactement cela.

**Une rupture unique, assumée, au premier démarrage** : les index de groupe et de rôle passent à
deux chiffres — `2110 1` devient `2110 01`, `audio 1` devient `audio 01`. Sans quoi un contrôleur
affiche « 2110 1, 2110 10, 2110 11, 2110 2 », l'ordre du dictionnaire et pas celui de la régie. Les
figer tels quels aurait gravé ce défaut pour de bon, d'où cette normalisation faite maintenant, une
fois. Elle est prudente : un préfixe qui finit par un chiffre — « REGIE 1 » — n'est pas un index et
n'est pas touché. Un contrôleur qui avait mémorisé les anciens groupes est à re-paramétrer ; c'est
la dernière fois.

Autre manque comblé : nos Node et Device ne portaient **aucune** information distinctive
(`tags: {}`). C'est le problème que décrit BCP-002-02 : devant une requête qui rend plusieurs
appareils au même libellé, l'ingénieur d'exploitation n'a aucun moyen de savoir lequel est lequel.
Fabricant, produit, identifiant d'instance et fonction sont maintenant publiés, réglables dans
Réglages → Protocoles → NMOS, et servis depuis la **même source** que l'identité annoncée en IS-12 —
deux protocoles qui décrivent le même appareil ne doivent pas en décrire deux.

## L'état de nos entrées et sorties 2110 se lit désormais depuis n'importe quel contrôleur — 2026-08-04

Un moteur 2110 sait beaucoup de choses sur ses flux : ce que reçoit chaque entrée, si l'horloge est
verrouillée, si un lien est tombé, si une sortie rejoue la même image faute de source. Jusqu'ici tout
cela ne se voyait que dans nos pages. Un exploitant qui supervise une installation depuis un
contrôleur du marché n'avait aucun moyen de l'apprendre, et une régie ne va pas surveiller deux
écrans pour un seul parc.

L'orchestrateur parle maintenant **IS-12**, le protocole de contrôle NMOS, et publie pour chaque
Receiver et chaque Sender un moniteur conforme à **BCP-008-01/02** — le vocabulaire que les
contrôleurs tiers savent déjà lire. Quatre domaines par flux : l'état des **liens** physiques, celui
de la **connexion** (ou de l'**émission** côté sortie), celui du **flux** (ou de l'**essence**
présentée à l'émission), et celui de la **synchronisation** externe, avec le grandmaster réellement
suivi. Chacun porte un message de cause en clair, un compteur des dégradations subies, et l'ensemble
se résume en un état global. Les changements sont **poussés** au contrôleur, pas interrogés en
boucle.

Trois choix méritent d'être dits, parce qu'ils décident de ce que vaut une alarme.

**Un statut se compare à l'intention, jamais à un idéal.** Une entrée sans abonnement est *inactive*,
pas en panne. Une sortie déclarée mais volontairement au repos est inactive aussi. Les drapeaux de
contenu — image figée, noir, silence — ne dégradent l'état d'un flux que si vous les avez **armés sur
cette source** : une mire figée exprès reste saine, la même image figée sur une caméra en direct ne
l'est pas. C'est le réglage que vous faites déjà par source qui tranche.

**Ce qu'on ne mesure pas, on ne le raconte pas.** Nous ne comptons pas les paquets perdus ou en
retard côté réception — le moteur travaille en trames, il constate qu'une image manque, pas combien
de paquets lui faisaient défaut. La spécification prévoit ce cas : nous répondons une collection
vide. Publier des zéros aurait affiché « aucune perte » là où il fallait lire « rien n'est mesuré ».
Côté sortie, en revanche, les trames remises en retard et les trames rejouées sont comptées, et
remises à zéro à chaque activation.

**Une sortie qui va bien peut cacher une source morte.** Nos émetteurs sont délibérément indépendants
de leur producteur : quand une source décroche, la sortie continue d'émettre à cadence parfaite en
rejouant la dernière image. C'est la bonne conception — un fil ne doit jamais s'arrêter — mais elle
rend l'incident invisible pour qui ne regarde que l'émission. D'où le domaine *essence*, distinct :
l'émission est saine, l'essence ne l'est pas, et l'état global le dit.

Le service s'active dans Réglages → Protocoles → NMOS, sur un port dédié (5010 par défaut), et une
table y montre l'état de chaque moniteur — la même vérité que celle servie aux contrôleurs. Il est en
**lecture seule** : aucun objet publié ne permet de router, de démarrer ou d'arrêter quoi que ce soit.
Comme le reste du provider NMOS, il n'est pas authentifié et suppose un réseau de contrôle fermé.

Seuls les flux servis par un moteur 2110 ont un moniteur ; les ressources NMOS sans télémétrie
derrière n'en reçoivent pas, et leur nombre est affiché plutôt que passé sous silence.

## Un pupitre visait un numéro de conteneur ; il vise maintenant une fonction — 2026-08-04

L'arbre Ember+ exposait les conteneurs par leur VMID. Or un VMID est un jeton local et jetable : il
est réattribué à la création, il change quand on recrée une machine, et il n'a jamais rien promis à
personne. Un pupitre configuré pour rappeler un layout de multiviewer visait donc le conteneur qui
exécutait ce multiviewer ce jour-là. Remplacer le conteneur, et le bouton du pupitre pointait dans le
vide — ou, pire, sur le conteneur qui avait hérité du numéro.

L'arbre est désormais celui des **emplacements**. Un emplacement est une fonction de production —
« MULTIVIEW RÉGIE 1 » — portant un numéro qui n'est jamais réattribué, même après suppression, et
une clé immuable. C'est lui que le contrôleur externe adresse ; le conteneur qui le sert n'est qu'une
liaison, réaffectée d'un geste. Recréer le multiviewer, le déplacer sur une autre machine, en
changer complètement : aucun chemin ne bouge, et rappeler un layout reste exactement le même ordre.

Un emplacement sans conteneur lié reste publié, marqué hors ligne. C'est délibéré : une branche qui
disparaît de l'arbre laisse le pupitre en face avec des boutons d'apparence normale qui ne
déclenchent plus rien, et personne ne l'apprend avant l'antenne. Une branche hors ligne se voit.

Les emplacements sont semés automatiquement au premier déploiement d'un conteneur, avec son nom
d'hôte pour libellé : rien de plus à faire dans le cas courant. Ensuite ils sont à vous — renommer,
réaffecter, supprimer — dans Réglages → Ember+. Le libellé se renomme librement, la clé et le numéro
jamais : c'est le contrat d'adressage tenu envers les machines d'en face.

Le rappel de mémoire, au passage, ne se fait plus au rang. Écrire « 3 » rappelait le troisième layout
de la liste ; insérer ou renommer un layout réordonnait cette liste, et le bouton mémorisé rappelait
alors autre chose sans qu'aucun signal ne le dise. Le rang reste accepté — traduit à la volée d'après
la liste telle qu'elle a été publiée — mais c'est le nom du layout qui décide, et un paramètre
`recall_nom` permet de le nommer directement.

Rupture assumée, sans transition : l'ancien arbre `containers.<vmid>` n'existe plus.

## Préparer une carte pour le RDMA cassait le réseau de cette carte — 2026-08-03

Activer le rôle RDMA sur une interface charge le pilote InfiniBand correspondant, puis vérifie
qu'une fonction RoCE apparaît bien ; si rien n'apparaît, la demande est refusée avec la raison,
plutôt que d'enregistrer un rôle qui ne servira jamais. Le refus était juste. Ce qu'il ne disait
pas, c'est qu'il arrivait trop tard.

Sur les cartes Broadcom NetXtreme-E, charger le pilote RoCE fait ré-enregistrer l'interface
Ethernet elle-même : le journal du noyau montre l'interface renommée puis le pilote RoCE annoncé
dans la foulée, et l'index système de l'interface passe de quatre à cent quarante-neuf. Elle revient
éteinte et sans adresse. La configuration gravée sur le disque, elle, est intacte — mais elle n'est
relue qu'au démarrage de la machine, et personne ne redémarre un serveur pour activer un rôle.

Le refus laissait donc le nœud en plus mauvais état que celui où on l'avait trouvé. Deux cartes
parfaitement câblées et fonctionnelles — vingt-cinq gigabits, un tiers de milliseconde vers leur
voisin — sont restées invisibles trois jours durant. Et le diagnostic accusait le câblage, parce
qu'une interface éteinte ne laisse lire ni sa porteuse ni le contenu de son module optique : l'outil
répond « pas de module », on conclut « pas de câble », et la conséquence est prise pour la cause.

Deux corrections. L'interface est désormais remise dans son état déclaré après tout chargement de
pilote, qu'il ait abouti ou non — c'est précisément quand il n'aboutit pas que la demande est
abandonnée et que plus personne ne repose l'adresse. Le changement d'index système est le seul signe
observable de cette recréation : il est relevé avant et après, il déclenche une alerte nommée, et il
est écrit dans le message rendu à l'exploitant.

Et lorsqu'aucune fonction RoCE n'apparaît, le pilote est déchargé au lieu d'être laissé en mémoire.
Le graver pour le prochain démarrage était déjà réservé au succès ; le décharger rend l'opération
réellement sans trace. Le message dit maintenant ce qui a été fait à la machine, parce qu'un refus
qui abîme est pire qu'un refus.

Reste, indépendamment de ce correctif : ces cartes n'exposent aucune fonction RoCE et ne l'annoncent
par aucune erreur. Elle est vraisemblablement à activer dans la configuration interne de la carte.

## Un nœud mort restait « up », ses conteneurs « running », et personne n'était prévenu — 2026-08-03

Le nœud qui porte le moteur ST 2110 s'est arrêté à 22h18, ventilateur en cause. Il était injoignable
sur ses trois réseaux et muet jusqu'en ARP, donc indiscutablement éteint. Une heure plus tard,
l'orchestrateur le donnait toujours pour joignable, ses trois conteneurs s'affichaient en marche avec
la cadence qu'ils avaient au moment de la coupure, et aucune alerte ne nommait la machine. Les seuls
signaux étaient indirects — une horloge PTP perdue, des liens de réplication en échec — et aucun ne
désignait le coupable.

Deux causes, indépendantes. D'abord, la fonction qui écrit qu'un nœud est tombé n'était appelée de
nulle part : toutes les autres écritures forcent « joignable », et les seules qui écrivent le
contraire sont des gestes manuels d'enrôlement. Le statut d'un nœud ne pouvait que monter. Ce n'est
pas resté sans conséquence : le choix de nœud écarte les nœuds marqués tombés, une condition qui
n'était donc jamais vraie — une machine morte continuait de recevoir des déploiements.

Ensuite, l'information manquante existait déjà. L'inventaire de la flotte sait à chaque tour quels
agents n'ont pas répondu ; il s'en servait uniquement pour ne pas faire transiter les conteneurs
concernés, puis la jetait. Rien n'est donc sondé de plus aujourd'hui : le relevé qui avait déjà lieu
est simplement exploité.

Un nœud injoignable bascule au bout de quinze secondes et l'incident est annoncé au bout d'une
minute — assez pour absorber un agent relancé, pas assez pour laisser passer une nuit. L'annonce
distingue deux situations que l'on confondait et dont les remèdes n'ont rien à voir : la machine
répond mais son agent s'est tu, auquel cas relancer un service suffit et les conteneurs tournent
sans doute encore ; ou bien plus rien ne répond, et il faut aller voir la machine — physiquement,
tant qu'aucune carte de gestion à distance n'est renseignée, ce qui est le cas de toute la flotte.

Les conteneurs d'un nœud absent cessent de mentir. Ils ne sont pas déclarés arrêtés — on n'en sait
rien — mais injoignables, avec la raison et l'heure exacte, et leur cadence n'est plus affichée : une
valeur figée n'est pas une mesure. Au retour, tout est rétabli et l'événement est refermé en disant
si le nœud a redémarré, ce qui distingue une simple coupure de liaison d'un reboot dont il faut
relever le moteur.

Deux enseignements sont venus de la mise à l'épreuve elle-même. Les seuils étaient d'abord comptés
en tours de boucle : or un tour dure cinq secondes en temps normal et plus d'une minute quand un nœud
mort fait traîner les sondes — un seuil compté en tours se dilate donc avec la panne qu'il doit
détecter. Ils sont en temps réel. Et parce que ces seuils ne sont évalués qu'aux instants
d'échantillon, une coupure de cent dix secondes a pu n'être vue qu'avant le seuil d'alerte puis se
résoudre, sans laisser la moindre trace. Au retour, la durée totale est connue exactement : si elle
dépassait le seuil sans avoir été annoncée, elle est signalée au passé. Mieux vaut une alerte tardive
qu'une panne qui n'a jamais existé dans le journal.

## La mire annonçait cinquante images et n'en livrait que vingt-cinq — 2026-08-02

Une campagne de nuit destinée à éprouver la flotte en charge a fait sortir un défaut qui durait
depuis deux versions et que rien ne voyait. Une mire A/V déployée en mille quatre-vingts lignes à
cinquante images par seconde en produisait exactement vingt-cinq. Pas vingt-quatre, pas
quarante-huit : vingt-cinq pile, sur trois machines de générations différentes. Un chiffre aussi
rond ne vient jamais de la charge — c'est un diviseur.

Il l'était. Un producteur cadencé demande la coordonnée de sa prochaine image, puis dort jusqu'à
l'instant où elle échoit. Pour ne pas se réveiller après l'échéance, il demandait deux créneaux
d'avance. Mais cette avance, il la consomme en dormant : il se réveille AU créneau visé, et sa
demande suivante repart d'une grille qui a déjà avancé de deux, à laquelle on rajoute deux. La tête
du flux progressait donc de deux créneaux par image. La cadence était divisée par l'avance
demandée, par construction. Sur les machines les plus anciennes, où le tour de boucle déborde d'un
créneau, elle était divisée par trois — seize virgule sept images par seconde.

Rien ne pouvait le signaler. Le flux restait sur la grille, ses index strictement croissants, et le
coût propre du rendu restait bas puisque le rendu, lui, allait vite : huit millisecondes pour un
budget de vingt. Tous les indicateurs internes étaient au vert. Seule l'alarme de cadence l'a vu,
parce qu'elle ne compare pas à un idéal mais à l'INTENTION déclarée — cinquante demandés, vingt-cinq
observés — et parce qu'on lui avait enfin donné une intention à comparer.

L'avance est ramenée à un créneau, ce qui laisse une image entière de marge. Mesuré après
correction : cinquante images par seconde pleines sur les nœuds récents. Les machines anciennes
passent de seize virgule sept à vingt-cinq, ce qui est cette fois leur plafond réel et non un
artefact. Le générateur d'images fixes portait le même idiome et le même défaut ; il est corrigé
avec. La documentation du calcul d'index, qui recommandait la mauvaise valeur et l'avait propagée,
est corrigée aussi — c'était elle, la vraie source.

## L'étalonnage ne se périme plus au premier recâblage — 2026-08-02

La signature qui identifie un profil de coût condensait tous les paramètres d'un conteneur, faute
d'une déclaration de ce qui compte. Le câblage en faisait donc partie : brancher un mélangeur sur
une autre source, même à format strictement identique, changeait sa signature et jetait son
étalonnage. Or un mélangeur est recâblé en exploitation normale — son profil n'aurait jamais servi
à rien. Le mélangeur et le compositeur déclarent désormais leurs clés déterminantes, comme le
moteur 2110 le faisait déjà. Effet visible : cinq mélangeurs réglés à l'identique sur cinq machines
portent enfin la même signature, donc des mesures comparables entre elles.

## Un nœud écarté du placement le reste sans le dire — 2026-08-02

Quand le réseau conteneurs d'un nœud dérive, il est écarté du choix automatique et l'exploitant est
prévenu — une seule fois, pour ne pas noyer le journal. Mais rien ne levait cet écart au retour à la
normale : le message restait vrai à l'écran alors qu'il ne l'était plus dans les faits, et surtout
une RÉCIDIVE ne réalertait plus jamais, le motif mémorisé étant déjà le bon. Une alarme qui ne peut
plus se redéclencher est pire que pas d'alarme, puisqu'elle donne le sentiment d'être surveillé. Le
retour à la normale est désormais annoncé, et il réarme l'alerte.

---

## Le mélangeur DVE mesuré là où personne ne regardait — 2026-08-02

Un cache qui fonctionne parfaitement peut cacher un gouffre. Le compositeur DVE précalcule pour
chaque incrustation une « empreinte » — carte d'échantillonnage, masques, ruban de bordure — et la
réutilise tant que les réglages ne bougent pas. En régime établi, la trame ne fait plus que
composer : dix-huit millisecondes pour quatre incrustations en 1080p avec tous les effets, sous le
budget. Ce chiffre était le seul qui avait jamais été mesuré.

Or l'empreinte n'est en cache que si la géométrie ne bouge pas. Pendant une transition, pendant
qu'un opérateur glisse un curseur, pendant une rotation animée, elle est reconstruite cinquante
fois par seconde. Mesuré : **deux cent vingt-sept millisecondes par trame**, dont quatre-vingt-dix
pour cent en pure reconstruction. Onze fois le budget. Le plugin documentait par ailleurs deux
affirmations fausses : que seule la rotation décrochait — sans rotation du tout l'animation coûtait
déjà le double du budget — et que les effets s'additionnaient, alors qu'une incrustation tournée
fait sortir tous les autres effets du chemin de calcul rapide.

La construction d'empreinte passe donc sur le noyau de calcul C, comme la composition l'avait fait
avant elle : la rotation animée revient de cinquante-trois à vingt et une millisecondes, et à
onze avec davantage de cœurs. Le rendu est identique **à l'octet près** — vérifié contre le code
qu'il remplace, sur des scénarios aléatoires et en régime animé.

Ce qui reste hors budget est dit aussi clairement : la composition animée la plus riche tient
encore soixante-cinq millisecondes au mieux. Après ce portage, l'essentiel de ce qui reste est de
l'arithmétique pure, sans un octet de vidéo lu — il n'y a plus de gras à retirer, c'est le coût du
modèle lui-même.

Corrigé au passage : un compositeur DVE déployé sans fond câblé redémarrait en boucle, sans jamais
composer. Le défaut existait depuis l'introduction du verrouillage de cadence sur le fond et ne
pouvait se voir qu'en déployant le plugin nu — ce que personne n'avait fait.

---

## L'orchestrateur constate enfin où tournent vraiment les conteneurs — 2026-08-01

Attribuer des cœurs à un conteneur et vérifier qu'il peut s'en servir sont deux choses différentes.
Jusqu'ici l'orchestrateur ne faisait que la première. Un moteur 2110 épinglé sur seize cœurs dont
quinze étaient **isolés du noyau** n'en avait donc qu'un seul d'utilisable — et ses 274 threads s'y
entassaient — sans qu'aucun compteur ni aucune alarme ne s'en émeuve. Le cpuset était large, les
cœurs étaient « libres », et la machine perdait un quart de sa cadence.

Le piège tient à une subtilité : isoler un cœur ne l'interdit pas, il le retire de la répartition de
charge. Un cœur isolé accueille très bien une boucle temps réel qu'on y affine explicitement, mais
l'ordonnanceur n'y enverra jamais un thread ordinaire. Un conteneur peut donc se voir attribuer
seize cœurs et n'en avoir qu'un pour de vrai.

L'orchestrateur relève désormais, sur chaque nœud, la bande de cœurs réellement isolée, le cpuset
réellement posé sur **chaque conteneur Docker** — y compris ceux qu'il n'a pas créés lui-même — et
la répartition réelle des threads cœur par cœur. Il en tire deux vérifications simples : tout cœur
donné à un conteneur doit être un cœur où ses threads peuvent aller, et la bande isolée doit rester
au seul moteur 2110. Ce qui ne tient pas est signalé nommément, une fois, avec le constat chiffré à
l'appui et le remède.

Le relevé porte sur tous les conteneurs du nœud, pas sur ceux que l'orchestrateur connaît : c'est
précisément là que se cachait le reste du problème. Sur le nœud de banc, quatorze conteneurs de
réplication créés hors de son modèle n'avaient aucune restriction de cœurs et pouvaient donc
s'exécuter sur ceux du moteur.

## Toute capacité oubliée à l'enrôlement se rattrape sans réinstaller le nœud — 2026-07-30

Les capacités d'un nœud (2110, compute, médias, WebRTC, GPU) se cochent au moment de créer son jeton
d'enrôlement, et le jeton est consommé une seule fois. Oublier « GPU » sur une machine qui en est
équipée obligeait jusqu'ici à relancer l'installation complète — avec deux pièges silencieux : elle
réécrit intégralement la configuration de l'agent, effaçant la carte 2110 et les cœurs réservés si
tous les arguments d'origine ne sont pas reproduits à l'identique, et elle régénère le jeton de
l'agent, ce qui fait tout bonnement perdre le nœud à l'orchestrateur.

La carte du nœud porte maintenant une ligne **Capacités** qui liste celles dont le nœud dispose,
et rien d'autre. Un bouton **Ajouter** ouvre un menu proposant ce qui manque — chaque entrée annonce
au passage si elle imposera un redémarrage. L'orchestrateur ne provisionne que la capacité choisie. Le reste de la configuration est préservé à l'octet près, jeton compris, et la liste connue
de l'orchestrateur est resynchronisée depuis le nœud à la fin de l'opération.

Les **cinq** capacités sont couvertes. Chacune emmène ses propres réglages, repris de la base plutôt
que redemandés : la carte 2110 et les cœurs réservés pour le 2110, la racine média pour les médias.
Ce que l'orchestrateur ne sait pas, il ne le devine pas — il l'omet et le dit. Après l'ajout d'une
capacité de traitement, de médias ou de WebRTC, les images correspondantes sont poussées dans la
foulée ; s'il reste le réseau conteneurs à configurer, c'est annoncé, avec l'endroit où le faire.

Ce n'était pas qu'un confort. Une liste de capacités périmée est **silencieuse et structurante** :
un nœud dont la capacité 2110 n'est pas déclarée ne voit pas son PTP échantillonné, et n'est pas
compté parmi les machines à qui pousser les images correspondantes. Rien ne le signale.

Le résultat affiché est celui du provisioning réel, pas celui de la commande : si l'installation du
pilote échoue, l'interface le dit et montre le journal, plutôt que d'afficher un succès sur un stack
absent. Le voyant GPU, lui, reste conditionné à la détection matérielle — pilote, intégration Docker
**et** CUDA réellement utilisable — jamais au succès du bouton. Un redémarrage du nœud reste
nécessaire pour charger le pilote ; l'interface le rappelle.

Deux effets méritent d'être connus avant d'appuyer. Ajouter la capacité **2110** à un nœud qui ne
l'avait pas **désactive sa synchronisation NTP** : l'horloge d'une machine 2110 est tenue par le
moteur, et laisser deux disciplines battre sur la même horloge la ramènerait à 37 secondes du reste
du cluster — sans que rien ne le signale. La confirmation l'annonce, et le nœud reste sans discipline
d'horloge tant que son moteur ne tourne pas. Enfin, le **réseau conteneurs** n'est volontairement pas
traité comme une panne : la carte parente ne se devine pas, elle se choisit — c'est une étape
restante, signalée comme telle, pas un échec à réessayer.

---

## Deux ports RDMA valent 20 Gb/s, à condition de répartir les liens — 2026-07-28

Un nœud plafonnait à 10 Gb/s de réplication RDMA avec six flux à répliquer : 9,95 Gb/s engagés sur
une capacité de 10, soit 99,5 % du fil. Une seconde interface a été câblée pour doubler la capacité.

**Un bond LACP n'y aurait rien changé.** En RoCE, une connexion (une QP) reste sur un seul port :
agréger deux liens physiques ne fait pas passer une réplication donnée à 20 Gb/s. Et le RoCE-over-LAG,
qui fusionne deux ports en un device verbs unique, est une fonction des cartes ConnectX-4 et
au-delà — absente du mlx4 de la carte concernée. L'agrégat s'obtient donc en distribuant les
**liens**, pas en agrégeant les ports.

Chaque lien se voit maintenant attribuer un **chemin** à sa création — une paire de ports partageant
un sous-réseau — choisi comme le moins chargé du moment, la charge étant estimée depuis le format de
chaque flux (un round-robin sur le nombre de liens mettrait trois flux UHD face à trois flux audio).
La comptabilité est **orientée** : l'Ethernet étant full-duplex, le sens aller et le sens retour d'un
même port sont deux budgets indépendants, et saturer l'un ne doit pas interdire l'autre.

Ajouter un port à un nœud déjà en service ne déplaçant rien par lui-même, un **rééquilibrage** des
liens existants est disponible depuis l'onglet RDMA : il simule d'abord, annonce quels liens bougent
et la coupure brève que leur ré-établissement implique, puis applique sur confirmation. Un tableau
des chemins rend la répartition visible — sans lui, un allocateur qui entasserait tout sur un seul
port serait indiscernable d'un fonctionnement normal.

Trois pièges du multi-port sont désormais détectés plutôt que subis, parce qu'aucun ne se manifeste
autrement que par une moitié de bande passante manquante, sans erreur ni compteur anormal :

- **L'ARP répondait par n'importe quel port.** Le défaut Linux (`arp_ignore=0`) fait annoncer une
  adresse locale depuis toutes les interfaces : le pair apprend la MAC du premier port pour l'adresse
  du second, et tout ressort par un seul fil. `arp_ignore=1` / `arp_announce=2` sont posés à
  l'installation et rejoués à chaque établissement de lien, donc réparés après un ré-enrôlement.
- **Une adresse secondaire peut capter le sous-réseau voisin.** Les interfaces portent souvent plus
  d'adresses que la base n'en déclare ; l'une d'elles peut recouvrir le sous-réseau d'un autre port.
  Seule l'observation de l'hôte le montre, et c'est maintenant signalé dans l'onglet RDMA — mais
  uniquement quand l'adresse intruse est **aussi spécifique ou plus** que celle du port légitime. Le
  noyau routant au préfixe le plus long, un `/16` traînant à côté d'un `/24` perd contre lui à chaque
  lookup et ne détourne rien : le signaler aurait été une fausse alarme, du bruit qui apprend à
  ignorer l'indicateur.
- **Le second port d'une carte bi-port était invisible.** Les ConnectX-3 exposent un device verbs à
  deux ports là où les ConnectX-4 en exposent un par port ; la sonde résolvait le netdev par device,
  attribuait donc le même nom aux deux ports, et le second n'apparaissait jamais. Il est maintenant
  résolu par port.

La capacité RDMA d'un nœud est enfin affichée comme la **somme** de ses ports et non le maximum : un
nœud qui en porte 20 n'annonce plus 10, ce qui faisait passer une saturation pour une surcharge
inexpliquée. Et la page Monitoring ventile désormais le débit **par interface** au lieu du seul total
par nœud : 6 Gb/s sur 20 peut être 6+0 — répartition morte — ou 3+3 ; seule la ventilation distingue
les deux, et un port muet pendant qu'un autre travaille est signalé comme tel.

---

## Un nœud verrouillé à 28 ns, et 16 minutes hors de la grille — 2026-07-28

Sur un site de production, la page Horloges annonçait un nœud 2110 « hors grille » et proposait de
le remettre d'aplomb en installant chrony. Le nœud n'avait aucun problème d'horloge : `ptp4l` y
était verrouillé depuis quatre jours à **28 ns**, `phc2sys` disciplinait `CLOCK_REALTIME` à ±50 ns.
Il était pourtant à **16,2 minutes** de l'UTC réel.

La cause était en amont : le grandmaster PTP élu sur le domaine annonçait `clockClass 248` —
« horloge libre, traçable à aucun temps réel » — et déclarait lui-même son décalage UTC non valide.
Le vrai grandmaster GPS n'était plus maître. Un esclave se verrouille au nanomètre sur une horloge
en roue libre exactement comme sur du GPS : **la qualité du verrou ne dit rien sur la justesse de la
référence.** Aucun indicateur du produit ne regardait cette distinction. `clockClass` est désormais
lu, affiché, et une référence en roue libre est signalée comme telle — avant tout jugement de
précision, puisqu'un nœud dans cet état obtient les meilleures notes de précision du parc.

Trois défauts indépendants s'étaient enchaînés pour raconter une histoire entièrement fausse :

- **Les sondes interrogeaient une unité qui n'existe plus.** Depuis le multi-NIC, les unités sont
  nommées par réseau (`mxl-ptp4l-net1`) ; la sonde d'horloge et l'agent-nœud demandaient l'état de
  `mxl-ptp4l` tout court. systemd répond « inactive » pour une unité inconnue — donc ptp4l était
  déclaré absent alors qu'il tournait. Les unités sont maintenant énumérées par motif.
- **`pmc` interrogeait le mauvais domaine.** L'agent demandait le domaine 0 sur le socket par
  défaut, là où ce nœud tourne en domaine 127 avec un socket dédié : aucune réponse, donc offset et
  grandmaster vides. L'agent lit désormais le domaine et le socket dans la configuration de l'unité
  qu'il vient de découvrir.
- **Le relevé fiable ne tournait jamais.** Le sampler conditionnait tout son relevé PTP à un
  réglage de nœud qui vaut « désactivé » par défaut et n'est jamais posé à l'installation — alors
  que le déploiement PTP réel, lui, est piloté par les interfaces. Quatre interfaces déclaraient
  PTP actif, ptp4l tournait, et le relevé autoritaire de l'orchestrateur — le seul qui vise le bon
  domaine — n'a pas tourné une seule fois. La présence d'interfaces PTP est un fait ; le réglage
  n'est qu'une intention, et un fait suffit désormais.

Faute de voir ptp4l, la page concluait « aucune source de temps », puis substituait « moteur 2110 »
parce qu'un moteur était déclaré sur ce nœud. Trois affirmations fausses issues d'un seul test
d'unité mal nommée.

**Le bouton « Mettre sur la grille » ne s'affiche plus quand un servo tient déjà l'horloge.** Il
n'a qu'un geste — installer chrony — et l'appliquer là où `phc2sys` discipline déjà
`CLOCK_REALTIME` met deux servos sur la même horloge. Le garde-fou existant ne couvrait que les
nœuds où le moteur écrit du TAI ; il ratait précisément ce cas. À la place, la page nomme ce
qu'elle voit : le décalage vient de la référence, pas du nœud.

---

## Les 30 ms d'écart entre machines n'existaient pas : c'était notre règle graduée — 2026-07-28

La page Horloges annonçait 20 à 30 ms d'écart entre les nœuds, sans jamais bouger et sans qu'aucune
horloge ne soit en cause. Elle mesurait sa propre latence.

L'heure d'un nœud était relevée en lui faisant exécuter un programme à distance : requête HTTP,
`sh -c`, démarrage d'un interpréteur Python, puis lecture de l'horloge. On datait ensuite le nœud
**au milieu de l'aller-retour**, comme si aller et retour coûtaient pareil. Ils ne coûtaient pas
pareil du tout : tout ce préambule est sur l'aller. Le nœud était donc déclaré systématiquement en
avance d'une vingtaine de millisecondes. Deux détails aggravaient le tableau — les horloges étaient
lues à la **fin** du programme, après des appels à `systemctl` et `chronyc`, ce qui ajoutait dix
millisecondes de retard pur ; et le lien réseau, lui, était à 0,3 ms. Rien de ce qu'on affichait ne
venait du réseau ni des horloges.

L'agent-nœud expose désormais l'heure nativement (`/v1/host/clock`), en deux appels système et rien
d'autre. Le contrôleur l'interroge selon le modèle de NTP, à quatre estampilles — émission,
réception côté nœud, réponse côté nœud, réception —, ce qui **retire de la mesure le temps passé
dans le nœud**. Le chronomètre ne démarre qu'une fois la connexion établie, poignée de main TLS
comprise : la laisser dedans reviendrait à réintroduire le biais qu'on vient d'ôter.

L'incertitude passe de ± 25 ms à **± 0,2 ms**, soit cent fois mieux. L'écart réellement mesuré entre
les deux nœuds du parc est de **0,5 ms** — les horloges n'ont jamais eu de problème.

Ce que ça débloque : la page ne se contentait plus que d'attraper les erreurs grossières (offset TAI
oublié, source de temps absente). Elle peut maintenant juger ce pour quoi elle existe — l'accord
**au grain**, 20 ms à 50 fps —, et le seuil correspondant, jusqu'ici défini mais jamais appliqué,
est activé. Il ne se déclenche que si l'écart dépasse à la fois le grain **et** l'incertitude du
relevé : sur un nœud dont l'agent n'est pas à jour, la mesure retombe sur l'ancienne sonde et
l'alarme se tait d'elle-même, parce qu'elle n'a pas de quoi conclure.

Enfin, chaque point d'historique porte la méthode qui l'a produit, et la dérive n'est calculée que
sur des points comparables. Sans ça, le passage d'une règle à l'autre aurait fabriqué une marche de
25 ms au milieu de la courbe — soit une dérive spectaculaire, entièrement inventée par le correctif
lui-même.

**Mise à jour requise** : agent-nœud ≥ 0.18.0 (Réglages → Nœuds → mettre à jour l'agent). En deçà,
tout continue de fonctionner avec l'ancienne précision, signalée dans l'interface.

---

## Un moteur 2110 ne peut plus tourner sur une configuration périmée — 2026-07-27

Le moteur 2110 est dimensionné au **débit** qu'il doit traiter : le nombre de cœurs DPDK découle du
format vidéo, du quota par scheduler et du nombre de slots actifs. Ce calcul n'a lieu qu'au démarrage
du conteneur, et les variables qui en sortent ne sont **jamais relues ensuite**.

Conséquence, vécue sur dl360-1 : le format par défaut du site est passé de 25 à 50 fps après le
démarrage du moteur. Neuf cœurs suffisaient à 25 fps, il en fallait quinze à 50. Le moteur a
tranquillement continué avec ses neuf — il a servi les six premières sessions puis refusé toutes les
suivantes. Quatre réceptions mortes, un watchdog qui retentait indéfiniment une création qui ne
pouvait pas aboutir, et **rien** qui reliait la cause (un réglage modifié) à l'effet (des entrées
muettes). C'est le pire genre de panne : chaque pièce se comporte comme prévu.

La boucle de surveillance compare désormais, toutes les cinq minutes et par nœud, l'environnement
réellement posé sur le moteur à celui que la configuration courante impose — cœurs, quota, format,
plafond de files TX, compteurs de slots. Tout écart est signalé, avec la valeur posée, la valeur
requise et la conséquence attendue. Le manque de cœurs est classé en erreur, parce que lui casse
franchement ; le reste dégrade ou ment sur le format.

La détection est en **lecture seule et ne coupe rien**. Appliquer ces variables exige de recréer le
conteneur, donc d'interrompre brièvement toutes les entrées/sorties 2110 du nœud : c'est une décision
d'exploitation, pas quelque chose qu'un détecteur doit déclencher dans le dos de l'utilisateur.

Corrigé au passage, même famille : la qualification automatique d'une carte lisait le nombre de files
TX allouées par le pilote et en déduisait la capacité de la carte. Or cette valeur n'est un plafond
que si le pilote a **bridé** la demande. Sur un moteur peu chargé, la sonde relisait sa propre
demande et rabaissait la capacité enregistrée d'un cran à chaque passage — 63, puis 41, puis 21, puis
14 — jusqu'à faire passer la page « Modèles de carte 2110 » entièrement en rouge et à brider le
moteur. La mesure exige maintenant une preuve de bridage, et refuse de conclure sans elle, en le
disant.

---

## Nouveau plugin : générateur de mire audio — 2026-07-26

Le silence ne coûte plus rien et n'a plus besoin de personne. Restait le **vrai signal**, qui n'avait
aucune raison d'être calculé une fois par destination : un générateur **mutualisé**, avec plusieurs
sorties 8 canaux **indépendantes**.

Deux sorties par défaut, et c'est délibéré : on règle l'une (fréquence, niveau, canaux, ruptage)
pendant que l'autre reste une référence stable. Seule la sortie 1 émet au départ, parce que deux
sorties qui produisent la même chose ne servent à rien.

Réglages par sortie : fréquence, niveau, activation canal par canal, et **ruptage** (coupure brève et
périodique en tête de période sur les canaux marqués, la convention qui permet d'identifier un canal
à l'oreille). Tout est pilotable par macro, paramètres continus comme actions, y compris l'ouverture
et la coupure d'un canal précis.

Le signal est pré-calculé à chaque changement de réglage puis servi par tranches de 1 ms sans aucun
calcul. La fréquence est arrondie au nombre entier de cycles sur la boucle, sans quoi le raccord
claquerait, et **l'écart est affiché** quand il existe : un réglage silencieusement différent de ce
qui sort serait un mensonge d'interface.

Côté interface, le caractère « machine audio » passe par le comportement et la géométrie, jamais par
du relief : poussoirs à accrochage avec LED, une tranche par canal sur grille stricte, bargraphes
segmentés, valeurs en monospace. La charte interdit les boutons 3D et range le corporate broadcast
lourd en anti-référence ; aucune couleur n'est écrite en dur, les trois lumières suivent.

---

## Le silence audio aussi : plus aucun producteur dans la boucle — 2026-07-26

Même raisonnement appliqué à l'audio — et réparation d'une régression introduite quelques minutes
plus tôt : en éteignant le générateur vidéo d'une sortie non câblée, le correctif précédent éteignait
aussi son générateur audio, si bien que ces sorties perdaient leur session audio (12 → 6 sessions).
Or une sortie provisionnée doit émettre du **silence**, pas **rien** : sans session, la feuille RL
disparaît et câbler l'audio plus tard imposerait une recréation, donc un arrêt de port — précisément
ce que le pré-provisionnement existe pour éviter.

La cause de fond était identique à celle de la vidéo : le silence était produit par un thread Python
écrivant des blocs de 1 ms, soit **~1000 réveils par seconde et par sortie**, pour des zéros. Le
silence n'est pas un signal, c'est une absence : il ne justifie ni producteur, ni flux, ni lecteur.
Le moteur savait déjà émettre du silence quand aucun échantillon n'arrive — il suffisait de ne plus
exiger de source. La **tonalité**, elle, reste un vrai signal et garde son producteur.

**Bilan de la journée sur ce moteur** : mêmes douze sessions qu'au matin, cadence vidéo passée de
38,1 à ~50 fps (nominal), conteneur de 803 % à 717 % de CPU, et plus **aucun** producteur Python dans
la boucle — ni vidéo, ni audio. Image `bobi-mtl:0.64.0`.

---

## Une sortie 2110 non câblée n'a plus besoin de producteur du tout — 2026-07-26

Suite du correctif précédent. La recopie par trame avait disparu, mais un slot provisionné non câblé
gardait toute une chaîne vivante pour afficher une image **fixe** : un thread Python, un flux MXL, un
writer, un reader côté moteur, et une attente de grain à chaque trame. La cadence de la sortie 2110
était donc dictée par un thread Python en concurrence avec les lcores DPDK — 45 fps au lieu de 50.

Le bon découpage n'est pas *noir vs mire*, c'est **rendu vs émission**. Le contenu d'un slot en GÉN ne
change que sur évènement : le contrôleur le rend une fois et publie le résultat dans un fichier ; le
moteur le charge sur changement de mtime et le ré-émet lui-même. La boucle n'est alors cadencée que
par la libération des framebuffers, donc par l'émission réelle. Le fichier est byte-identique au
contenu d'un grain MXL, si bien que les deux chemins d'émission le consomment avec leur code
existant — aucune connaissance de format nouvelle nulle part. C'est le contrat déjà éprouvé de
l'incrustation ident, appliqué à une trame entière.

**Le câblage n'est pas interrompu**, et c'était la vraie question : la session reste la même, seul son
alimenteur change. Pas de recréation, pas de commit RL, donc pas d'arrêt de port — donc aucun effet
sur les autres flux de la carte. Vérifié en direct dans les deux sens avec une source de même format :
aucune discontinuité, ni sur le slot ni sur les cinq autres sessions.

**Mesuré** sur six sorties 1080p50 : **45,0 → 49,9 fps** (nominal atteint), trames source ratées de
3,7/s à **0**, conteneur moteur de 803 % à 586 % de CPU, et plus aucun flux générateur. Sur l'ensemble
de la journée, ces sorties sont passées de 38,1 à 49,9 fps. Progressif uniquement, motifs animés
exclus — ces cas gardent le générateur, explicitement. Image `bobi-mtl:0.63.0`.

---

## Le générateur de mire recopiait 4 Mo par trame pour une image qui ne change jamais — 2026-07-26

Les sorties TX en GÉN — mire, et surtout le **fallback noir** d'un slot provisionné non câblé — ne
tenaient que ~38 des 50 fps demandés. La sous-cadence était **réelle**, pas un artefact de compteur :
les compteurs par port la confirment au pourcent près.

`open_grain()` rend une vue **zéro-copie** sur un slot du ring MXL, dont le contenu de la rotation
précédente est encore présent. Le générateur remplissait pourtant le grain à chaque trame, y compris
pour une mire strictement statique : ~4,15 Mo recopiés en Python pour un résultat octet pour octet
identique — **~1,24 Go/s** pour six générateurs, au service d'une image constante.

Le contenu écrit est désormais signé **par adresse de slot** (motif, géométrie, contenu de l'ident,
numéro de trame pour les motifs dynamiques). Si le slot contient déjà ce contenu, on publie sans
remplir. La cadence de grains est inchangée — seule la recopie redondante disparaît.

Sauter un remplissage repose sur une **hypothèse** (le ring conserve les octets), et une hypothèse
fausse émettrait du vide en silence. Elle est donc **prouvée, pas supposée** : à la première
réutilisation de chaque slot, l'empreinte du grain entier est recalculée et comparée — coût payé une
fois par slot — puis 64 octets témoins suffisent à chaque trame. En défaut, on remplit normalement et
on le dit dans les logs.

**Mesuré** sur six sorties 1080p50 10 bits : **38,1 → 45,0 fps** (+18 %), confirmé indépendamment sur
le fil (9,92 → 11,88 Gb/s, conforme à 1,5 % près). Le reste de l'écart à 50 n'est plus du calcul :
les threads du générateur dorment l'essentiel du temps mais partagent leur cpuset avec les lcores
DPDK du moteur, en busy-poll à 100 %. Image `bobi-mtl:0.62.0`.

---

## Alarmes : ne plus signaler comme panne ce que personne n'a demandé — 2026-07-26

Un mail « TX #3 : image figée détectée » sur un moteur dont **aucune sortie n'était activée** a servi
de fil : le fil d'alertes de la journée contenait ~70 notifications pour **trois** conditions réelles.
Trois mécaniques distinctes, une seule racine — une alarme qui compare l'observé à un idéal absolu au
lieu de le comparer à ce que l'exploitant a *demandé*.

- **Une sonde qui mesurait notre propre repli.** Sans câble, un slot TX provisionné bascule en GÉN et
  son entrée pointe vers le générateur interne du moteur, qui produit du noir. La sonde de présence
  signal s'y branchait : elle signalait comme panne un noir constant qui est noir et figé **par
  construction**. Elle se branche désormais sur le **câblage réel** (`cable_shm`), que le contrôleur
  tenait déjà à part pour cette raison. Pas de câble, pas de sortie à surveiller. Image
  `bobi-mtl:0.61.0`.
- **Une notification par oscillation.** Les alertes de présence signal exigent maintenant **3
  observations consécutives** avant de basculer, **dans les deux sens** — le pendant de l'hystérésis
  qui protégeait déjà la clôture des épisodes de panne de flux.
- **Un incident ré-annoncé à chaque redémarrage.** L'état d'une alarme edge-triggered vivait en RAM :
  au redémarrage, il repartait à vide et l'incident **toujours en cours** était re-signalé. Dix
  redémarrages dans l'après-midi ont ainsi produit 26 alertes de pool sur-souscrit, 17 de famine CPU
  et 26 d'horloge absente — pour une seule condition chacune (la signature était le délai constant
  entre le `systemd Started` et l'alerte). Nouveau module `app/episodes.py` : l'état est persisté et
  relu au boot, avec purge des objets disparus. Branché sur `core_pool` et `cpu_pressure` (le PTP
  utilise son propre journal `ptp_events`).
- **Le contexte pour de bon.** 254 des 267 points d'émission d'alerte renseignent désormais
  `vmid`/`node_id`/`kind` — donc filtrables et routables. Les deux derniers restent volontairement
  nus : aucune valeur du vocabulaire fermé ne leur correspond, et la règle du projet est qu'un
  producteur qui ne sait pas passe `None` plutôt que de deviner. Chaque site a été vérifié par une
  passe AST (aucun nom hors portée, aucun `kind` hors `ALERT_KINDS`).

---

## PTP : un nœud DPDK au repos n'avait plus d'horloge du tout — et l'alarme appelait ça un « holdover » — 2026-07-26

**Le fil d'alertes répétait « PTP dl360-1 : déverrouillé depuis 30 s — holdover, dérive libre »
plusieurs fois par heure, pendant que le journal PTP, lui, restait vide.** Ce n'était ni un incident
répété, ni un holdover : trois défauts distincts se superposaient.

- **L'horloge n'existait qu'en présence d'un flux.** Sur un port full-PF DPDK il n'y a plus de netdev
  kernel, donc plus de `ptp4l` : la seule horloge du nœud est le client PTP interne de libmtl, armé
  par `mtl_init`. Or le moteur ne lançait `mtl_rx` qu'au **premier abonnement** — un moteur au repos
  laissait le nœud **sans aucune référence de temps**, en silence, et le premier flux câblé devait
  attendre la convergence PTP. Le daemon est désormais démarré (et relancé après crash) **même à
  0 session** quand le moteur porte l'horloge (`ENGINE_PTP=libmtl` ∧ port `dpdk`) : `mtl_rx` est un
  daemon `mtl_init` à vie dont la boucle publie l'état PTP indépendamment des sessions. **AF-XDP
  inchangé** (le CNI n'y a pas de PTP interne — démarrer à vide y attacherait le XDP pour rien).
  Image `bobi-mtl:0.60.0`.
- **L'alarme et l'affichage ne parlaient pas de la même chose.** Le détecteur s'appuyait sur le lock
  servo **STRICT** de libmtl (`locked`), qui ne s'arme pas sur E810 DPDK, quand toute l'UI pilote sur
  `synced`. Une horloge disciplinée à ±51 ns était donc annoncée en dérive libre. Le critère d'alarme
  est aligné sur l'affichage, et le message **diagnostique enfin la cause** : holdover véritable,
  absence de grandmaster, ou absence pure et simple de client PTP — trois pannes qui portaient un
  seul et même texte, celui qui envoyait chercher une panne réseau inexistante.
- **Chaque redémarrage de l'orchestrateur ré-inventait l'incident.** L'escalade était edge-triggered
  **en mémoire** et n'était journalisée nulle part : au boot, seed muet puis ré-émission 30 s plus
  tard — dix redémarrages dans l'après-midi donnaient dix « erreurs PTP » pour une seule panne, et le
  journal `ptp_events` restait vide pendant ce temps. L'escalade et sa résolution sont maintenant
  écrites dans `ptp_events`, **et relues au démarrage** : un incident déjà connu ne réveille plus le
  fil d'alertes. Les alertes PTP portent enfin leur contexte (`node_id`, `kind=ptp`).

---

## mTLS conteneur : la panne muette du reboot (certs en tmpfs) + vérification d'identité du client — 2026-07-26

**Deux multiviews de production ont cessé de fonctionner sans une alerte exploitable.** L'agent-nœud
matérialise les certificats d'un conteneur dans `/run/bobi-tls/<nom>/` — et **`/run` est un tmpfs**.
Au redémarrage d'un nœud : `/run` est vidé, Docker relève les conteneurs `--restart unless-stopped`
**avant** toute reprovision, la source du bind-mount n'existe plus donc Docker la **recrée vide**,
l'agent du conteneur ne trouve aucun cert et sert en **HTTP clair** — pendant que le contrôleur, qui
a tranché HTTPS une fois pour toutes, ne renégocie jamais. Le conteneur **tourne** et devient
**injoignable définitivement**. Le moteur 2110 y échappait (`--rm` : détruit puis recréé, donc
re-provisionné) ; les conteneurs compute, simplement relevés, gardaient leur montage vide — d'où
« seuls les multiviews sont tombés ».

- **Réparation** : au reboot détecté, l'auto-recovery (`node_recovery`) sonde chaque conteneur
  compute et **re-provisionne** ceux dont l'agent répond en clair (efface la signature de spec puis
  redéploie — seul chemin qui réécrit `/run/bobi-tls`). Les clés **restent en RAM** : on ne les
  persiste pas, on les régénère. Passe one-shot par boot, verrou par nœud, une tentative par
  conteneur — pas de nouvelle boucle de recréation. Auto-recovery désactivé → le **constat** et
  l'alerte ont lieu quand même (seule la réparation est gatée).
- **Plus de conteneur mort-né** : un échec d'émission de certificat était avalé en une ligne de log
  et le conteneur était créé quand même, structurellement injoignable. Il est désormais **réessayé**,
  puis le déploiement est **refusé** avec une alerte contextualisée (`vmid`/`node_id`/`kind`).
- **Filet de diagnostic** : quand un agent est muet en HTTPS, le contrôleur sonde **une fois** en
  clair. S'il répond, le verdict est certain — désaccord de schéma, pas un conteneur mort — et il est
  **dit** (alerte datée, niveau `error`). Aucun repli HTTP silencieux : une dégradation de sécurité
  doit être visible.
- **Identité du client mTLS (durcissement)** : l'agent par-conteneur vérifiait que le cert client
  était signé par la CA, jamais **quelle identité** il portait. Or les certs de conteneur portent
  EKU `clientAuth` : la clé d'un seul conteneur permettait de piloter **tous** les agents de la
  flotte. L'agent n'accepte plus que `CN=bobi-controller` (ou l'URI SAN `bobi://controller`) et
  refuse un `CN=mxl<vmid>` en 403 ; échappatoire `MXL_TLS_VERIFY_CLIENT_CN=0` / `MXL_TLS_CLIENT_CN`
  pour ne pas se verrouiller hors d'une installation au CN différent. *Baké dans les images runtime :
  effectif après rebuild + redéploiement.*

## Moteur 2110 : fin du faux « 38 fps » + épinglage de la fréquence des cœurs isolés (2110_io 0.58.0) — 2026-07-25

**Le « hoquet ~60 s » du moteur n'existait pas.** Toutes les ~60 s, tous les flux publiaient une
fenêtre à ~76 trames au lieu de ~101 → un faux « 38 fps » alarmant, alors que `frame_index` restait
continu et `incomplete` à 0. Des jours de traque ont innocenté le PTP, les logs libmtl, le dump de
stats, le contrôle d'IP du contrôleur, `MTL_FLAG_RX_VIDEO_MIGRATE` et même l'orchestrateur (arrêté) —
normal : **il n'y avait aucun événement à trouver**. La boucle de stats mesurait son `dt` sur des
`time_t` **entiers**, donc `dt` valait toujours 2,0 quelle que soit la durée réelle ; comme la boucle
tique à ~0,5 s + ε, sa phase dérive contre la grille de la seconde et franchit périodiquement le
seuil après ~1,4 s réelles → `(1,4×50)/2,0 ≈ 38`. La période ~60 s était un **battement** de cette
dérive, pas un timer. Fenêtre de stats passée en **horloge monotone nanoseconde** (2 sites) : le fps
publié est désormais exact. Le moteur, lui, n'avait jamais ralenti. *Rebuild de l'image `bobi-mtl`
requis.*

> Enseignement (2ᵉ fois — les « chutes de fps » du multiview étaient déjà un artefact du même type) :
> **avant d'enquêter sur un creux de cadence, vérifier d'abord le dénominateur de la métrique.**

**Fréquence des cœurs isolés (nouveau, Réglages → nœud → Préparation hôte).** Isoler des cœurs DPDK
(`isolcpus`/`nohz_full`) prive `intel_pstate` du retour d'utilisation sur les cœurs *tickless* : ils
restent collés à leur **fréquence plancher** alors qu'ils sont à 100 % de busy-poll, et le moteur
s'étouffe au bout de quelques heures. **L'isolation seule est un piège.** La prép hôte pose désormais
une unité systemd qui épingle les cœurs isolés au **maximum du CPU présent** (aucune fréquence ni
bande en dur : la liste vient de `/sys/devices/system/cpu/isolated`, la cible est le `cpuinfo_max_freq`
lu sur chaque cœur), applicable **à chaud** — pas de reboot. Un nœud sans cœur isolé n'est pas touché.
La sonde de prép affiche l'état en clair et **alerte quand une bande isolée n'est pas épinglée**, avec
un bouton de réparation : sans cette visibilité, un simple reboot réintroduisait le bug en silence.

---

## Multiview : modèles de PiP à format libre + auto-réparation des scripts perdus (multiview 0.34.0) — 2026-07-13

Un modèle de PiP n'est plus figé au 16:9 : l'éditeur (Réglages → PiP) gagne un champ
« **Format du modèle** » (ratio L:H libre — 16:9, 4:3, 1.85…) et un bouton « **Rogner l'espace
inutilisé** » qui supprime le vide sur les 4 côtés (le format devient la boîte englobante des
composants — idéal pour un layout vidéo 16:9 + UMD + tally sans perdre de place). La fenêtre
**vidéo** reste verrouillée 16:9 *en pixels* quel que soit le format. Côté composer, affecter un
modèle à format libre **snape la hauteur de la tuile** au ratio natif de l'habillage, et
« Remplir » tuile au bon ratio. Moteur inchangé ; les modèles existants (16:9 implicite) ne
bougent pas.

**Fix fiabilité** : un conteneur redémarré (rootfs éphémère) perdait son script déployé et
l'auto-restart (`/start`) tournait dans le vide — un mur **shardé** restait même stoppé
définitivement (le redéploiement prenait la branche hot-apply du tissu sans jamais pousser de
script). La surveillance détecte désormais le script *perdu* (`path: null`) et **redéploie
automatiquement** depuis la config persistée ; la branche hot-apply/mur shardé est gatée par
l'état réel du script (vérifié en prod : mur 163 auto-réparé en 7 s au premier tick).

## Multiview : l'habillage de mur vit dans les modèles de PiP (multiview 0.33.0) — 2026-07-13

Le **cadre** devient une propriété du composant *vidéo* du modèle de PiP (`border` :
fixe / tally / cadre fin / bezel moniteur / viseur / soulignement — dessiné sur le rectangle
image réel, letterbox compris) et le « **texte sous l'image** » n'est plus un réglage : c'est un
*layout* de modèle (vidéo réduite + UMD dessous, cf. le modèle intégré « UMD broadcast »). Les
réglages **globaux** de mur (style de cadre, bordure, taille de texte, texte sous l'image,
format par fenêtre) disparaissent du composer ; l'éditeur de modèles (Réglages → PiP) les porte
désormais, et 4 modèles intégrés s'ajoutent (« Classique », « UMD broadcast », « Vidéo seule »,
« Moniteur »). Le chemin de rendu classique du moteur est **supprimé** : toute fenêtre rend via
le moteur de composants — sans modèle, un « Classique » est **généré** depuis les cases
par-fenêtre (Nom / Tally / VU), qui continuent donc de fonctionner. Côté **tissu**, la liste
blanche `_STYLE_FIELDS` rétrécit d'autant, et les pushes de style à chaud d'un mur **shardé**
sont désormais routés vers une re-planification du tissu (persistance + re-matérialisation par
signatures) au lieu d'atteindre l'assembleur — le cadre ne se dessine plus autour des blocs de
shards ; l'assembleur voit aussi son modèle par défaut explicitement nettoyé à la conversion.

## Multiview : conversion RGBA→YUV fusionnée en C (multiview 0.32.0, bobi-compute 0.12) — 2026-07-12

La conversion RGBA→YUV (habillage → plans vidéo) coûtait ~15 passes numpy par appel. Elle passe
dans le kernel C (`mvk_rgba2yuv`, ABI 2 de l'image **bobi-compute 0.12**, compilée
`-ffp-contract=off` pour reproduire exactement les arrondis float32 de numpy — bit-exact prouvé
sur 36 combinaisons). Bénéficiaires : les tuiles VU/horloges/ANC (mur de test avec VU 8 canaux :
`ov_render` **1,0 ms**, contre 2,3 ms en 0.31.0 et 7,0 ms en PIL 0.30.x) et surtout le **re-bake
du chrome pleine trame à chaque bascule tally** (moins de trames lentes aux commutations).
Repli numpy verbatim sur image ≤ 0.11.

## Multiview : VU-mètres sans rendu PIL par trame (multiview 0.31.0) — 2026-07-12

Le chemin CPU des VU-mètres redessinait **tout** le meter en PIL à chaque trame (fond,
graduations dB, numéros de canaux, barres) — ~1-2 ms par meter. Il passe sur la machinerie
tuile introduite pour le GPU en 0.20.0 : le statique est rendu PIL **une fois** et caché, seules
les barres + peak-hold sont peintes par trame (opérations tableau). Rendu **identique au pixel
près** (max|Δ|=0 re-prouvé sur 24 cas, 8 et 10 bits). Mesuré A/B sur mur de test (1 tuile plein
cadre + VU 8 canaux) : `ov_render` 7,0 → 2,3 ms, `own_latency` 14,8 → 9,6 ms. En cas de doute
sur un mur : redéployer avec `meters_pil=true` restaure le chemin PIL historique verbatim.
Fixes de la même session : threads OpenMP du kernel mvk = cœurs physiques réels (plafond 4),
attente passive libgomp (le spin-wait affamait le process — et le fab non épinglé piétinait
les cœurs du moteur), champ `mvk_threads` sur `:8080`.

## Multiview : compositing CPU fusionné en C (multiview 0.30.0, bobi-compute 0.11) — 2026-07-12

Le compose CPU du multiview était **borné par la bande passante mémoire** : chaque opération
numpy (placement de tuile, blend d'habillage) est une passe mémoire séparée, et un mur chargé
frôlait le budget 50p (own_latency ~19/20 ms mesuré avec une seule tuile). Les passes sont
désormais **fusionnées dans un kernel C** (`libbobi_mvk`, image bobi-compute **0.11**, variantes
SSE/AVX2 auto, OpenMP borné aux cœurs physiques du conteneur) : blend, blend pré-calculé du
chrome et placement nearest s'exécutent en **une passe mémoire chacun** — banc représentatif :
22,6 ms (numpy) → 3,1 ms (C mono-thread). Le rendu est **bit-exact** (mêmes octets, selftest
`mvk_selftest.py`), le chemin GPU est inchangé, et une vieille image retombe automatiquement
sur le code numpy d'origine. Champ `mvk: true` dans les métriques `:8080` pour vérifier
l'activation. Le kernel est prêt à accueillir un décodage V210 par ligne si le tout-v210 (R3)
est acté après les bancs. Image GPU rebasée en **bobi-compute-gpu:0.3** (base 0.11).

## Le binding MXL (bobimxl) est poussé avec les scripts — plus de rebuild d'image pour ses évolutions — 2026-07-12

Le binding Python du SDK MXL (`bobimxl.py`) était **baké dans les images runtime** : ses
évolutions purement Python (codec ANC RFC 8331, flux data…) n'atteignaient la flotte qu'au
rebuild+push des images — en pratique, le décodage ANC des containers tournait avec un binding
obsolète (écritures et décodages en échec silencieux). Désormais l'orchestrateur **pousse
`bobimxl.py` à côté du script** à chaque déploiement : il remplace le module de l'image
(compatibilité vérifiée avec la libmxl des images actuelles), et chaque redéploiement aligne le
container sur la version du contrôleur. Câblage vidéo → **l'audio et l'ANC suivent
automatiquement** sur les entrées du multiview (mêmes règles que les sorties TX du moteur).

## Mire A/V : générateur de timecode optionnel — 2026-07-12

La mire de synchro (avsync 0.11.0) peut désormais générer un **timecode** (option, panneau ⚙) :
l'heure du jour (hh:mm:ss:img, calée sur la même horloge de présentation que la trame — grille
PTP en genlock) est **incrustée sous le compteur** et **émise en flux ANC** (paquet ATC RP 188,
format normatif RFC 8331) au même index de grain que la vidéo. Le port « TC (ANC) » apparaît sur
la page Câbles quand l'option est active — câblez-le par exemple vers le port ANC d'une fenêtre
de multiview pour vérifier toute la chaîne timecode (horloge ANC, bandeau de métadonnées) avec
un TC de référence parfaitement aligné sur l'image.

## Câbles : entrées audio du multiview, badge « Tranche », et avsync au format système — 2026-07-12

- **Entrées audio du multiview sur la page Câbles** : chaque fenêtre expose désormais un vrai
  port « Audio N » câblable (comme la vidéo et l'ANC). Non câblé, il affiche le flux audio
  dérivé du nom de la source quand il existe (informatif) ; câblé, les VU-mètres lisent ce flux
  — utile quand le nom de la source ne permet pas la dérivation automatique.
- **Badge « ▤ Tranche »** : chaque tuile de la page Câbles indique discrètement si le container
  est déployé en mode tranche (composition/publication bande par bande).
- **La mire A/V (avsync) suit le format vidéo par défaut** des Réglages à la création, comme le
  mixer — plus de littéral 1280×720 quand le site est en 1080p50.

## Éditeur de modèles de PiP : composez librement l'habillage des fenêtres de multiview — 2026-07-12

Jusqu'ici, l'habillage d'une fenêtre de multiview était figé : le nom en bas, les VU-mètres à
gauche ou à droite, le bandeau ANC en haut ou en bas. Un nouvel éditeur, dans
**Réglages → PiP**, permet de **composer librement** l'intérieur d'une fenêtre et d'enregistrer
le résultat en **bibliothèque de modèles** réutilisables sur tous vos multiviews.

- **Composants disponibles** : vidéo, UMD (nom de la source, texte TSL ou texte fixe), lampes/
  pavés/cadres **tally**, **VU-mètres**, afficheurs de **métadonnées ANC**, **horloge** (PTP ou
  timecode de la source), **texte libre** et **badge format**. Chacun se place et se dimensionne
  librement (drag & drop), en coordonnées relatives : un même modèle s'applique à une tuile
  plein écran comme à une vignette de mosaïque.
- **Composants conditionnels** : chaque composant peut n'apparaître que sous condition — tally
  rouge/vert/actif, absence de signal, image figée… De quoi faire un bandeau « ON AIR » qui
  n'apparaît qu'à l'antenne, ou une alarme visuelle sur perte de source.
- **Repli petites tuiles** : un seuil « masquer sous N px » par composant évite qu'un modèle
  conçu en grand devienne illisible sur une petite fenêtre.
- **Aperçu avec simulation** : l'éditeur simule la taille de tuile, l'état du tally et l'état du
  signal pour vérifier le comportement du modèle sans toucher au mur.
- **Trois modèles d'usine** (Production, Ingénierie, Minimal) servent de point de départ —
  ils se dupliquent en un clic pour être adaptés.
- **Affectation par fenêtre** dans le composer multiview (nouveau champ « Modèle de PiP ») :
  application **à chaud**, sans coupure de la sortie. Une fenêtre sans modèle garde l'habillage
  classique, strictement inchangé. Les modèles affectés sont embarqués dans la configuration du
  container : ils sont **snapshotés et restaurés avec les projets**, et mémorisés par les layouts.
- Import/export de modèles en fichier JSON, comme les layouts.
- **Héritage par mur** : chaque multiview peut définir un **modèle de PiP par défaut**, hérité
  par toutes ses fenêtres — changer le modèle du mur rhabille tout d'un coup. Chaque fenêtre
  peut surcharger (autre modèle) ou forcer l'habillage classique.
- **Affectation des canaux audio** : chaque composant VU-mètres choisit son **premier canal**
  dans un espace de 16 (2 flux de 8) — par exemple les canaux 1-2 à gauche de la vidéo et 3-4 à
  droite, avec les numéros de canaux réels sur les graduations.
- **Bordure du composant vidéo** : fixe (couleur) ou **pilotée par le tally** (neutre au repos).
- L'éditeur reprend la présentation et les gestes du composer multiview : mêmes outils
  d'alignement/taille/distribution, multi-sélection Maj+clic, **snap magnétique** avec guides,
  steppers −/+. La fenêtre vidéo y est **verrouillée en 16:9**.

Au passage, trois corrections sur l'affichage ANC par cellule livré en 0.28.0 : les cases
cochées dans le composer n'étaient en réalité **jamais transmises** au container (ni à chaud ni
au déploiement), le choix haut/bas n'était pas appliqué à chaud, et l'incrustation plantait le
rendu de la trame une fois active (`_as_bool`). Le multiviewer passe en **0.29.0**.

## Multiview : métadonnées ANC affichables dans chaque cellule — 2026-07-12

Le multiview savait déjà lire le timecode embarqué. Maintenant qu'un flux ANC est décodé
intégralement (et plus seulement le timecode), chaque cellule peut afficher **ce que sa source
transporte vraiment** — une information qu'un mur de contrôle ne montre habituellement pas.

- **Tout est optionnel**, case par case et fenêtre par fenêtre, à la manière des VU-mètres :
  rien ne s'affiche tant que vous n'avez rien coché. Une cellule sans case cochée ne coûte rien.
- Informations disponibles : **types de métadonnées réellement portés** (timecode, sous-titres,
  AFD, format signalé, SCTE-104…), **timecode embarqué**, **sous-titres** (le texte lui-même
  quand il est décodable, sinon leur présence), **AFD** (format d'image actif), **format déclaré
  par le signal** (ST 352 — à confronter au SDP, pour repérer un désaccord), **déclencheurs
  SCTE-104**, et le nombre de **paquets au checksum invalide** — c'est-à-dire une métadonnée qui
  se corrompt en transit, signalée en rouge.
- **Un port ANC par entrée** : chaque entrée vidéo a désormais son flux ANC câblable depuis la
  page Câbles, au lieu d'une dérivation implicite. Les murs existants continuent de fonctionner
  sans rien changer (repli automatique sur l'ancien comportement).

## ANC : passage au format normatif RFC 8331 (fin d'une perte silencieuse) — 2026-07-12

Nos données auxiliaires (timecode, tally, sous-titres) voyageaient dans un **format maison** que
seuls nos propres containers comprenaient. Un banc croisé contre un SDK MXL vierge a montré le
pire scénario : le consommateur tiers ouvre le flux, le parse comme du RFC 8331, en déduit
« 0 élément ANC » et **conclut sans la moindre erreur que le flux ne porte aucune donnée**. Le
timecode disparaissait en silence — et, symétriquement, nous étions incapables de lire l'ANC d'un
tiers.

- **Le format maison est abandonné : l'ANC est désormais du RFC 8331, partout.** Contrairement au
  format vidéo planar — qui achète un vrai gain de calcul et se justifie — le format ANC maison
  n'apportait aucun gain (un grain ANC fait 4 Ko) : il ne coûtait que de la non-conformité.
- **Migration sans coupure** : chaque flux annonce son codage ; les consommateurs choisissent le
  bon décodeur, et le moteur sait encore émettre une source non migrée. Une flotte mixte
  fonctionne pendant la bascule.
- **Un bug corrigé au passage** : le numéro de flux (`stream_num`) était purement et simplement
  perdu par l'ancien format. Il est maintenant transporté de bout en bout.
- Prise d'effet au rebuild des images moteur (`bobi-mtl`) et de calcul/média.

## Interop MXL : convertisseur v210 SIMD + plugin Pont v210 (export/import) — 2026-07-12

Ouverture du chantier interopérabilité inter-éditeurs (`docs/reference/MXL_INTEROP.md`) : échanger des flux
avec les containers d'autres solutions sur le même serveur, via le SDK MXL **stock** — dont le
seul type vidéo est `video/v210`. Une re-mesure en C SIMD a montré que la conversion
v210↔planar coûte ~2-5 ms/image 1080p (le « ~33 ms rédhibitoire » historique était une
pénalité numpy, pas une propriété du format) → le pont de frontière devient quasi gratuit.

- **Convertisseur v210↔planar SIMD** : `v210convert.c` compilé dans l'image compute
  (`libbobi_v210.so` baseline + variante AVX2, choisie selon le CPU du nœud), exposé par
  `bobimxl.v210_pack/v210_unpack` (zéro-copie vers/depuis la vue grain, repli numpy bit-exact
  sur les anciennes images). Selftest bit-exact : `script_templates/v210_selftest.py`.
- **Nouveau plugin « Pont v210 »** (Traitements), bi-directionnel : **export** = miroir
  `video/v210` stock d'un flux planar interne, au même index de grain — le flowId à donner au
  tiers est sur `/state` ; **import** = un flux v210 tiers (ciblé par son flowId UUID brut,
  candidats listés par `GET /flows`) devient un flux planar interne câblable normalement.
  Limites v1 : 4:2:2 progressif, grain commité trame entière.
- **bobimxl interop** : lecture par flowId brut (`Reader(by_id=True)`), `flow_def(fid=…)`,
  et `discover_flows()` (énumération des `*.mxl-flow/flow_def.json` du domaine).
- Nécessite le rebuild `bobi-compute:0.10`. Reste à faire : banc croisé stock↔fork (un
  `mxl-info` non patché doit lire le miroir), ANC RFC 8331 si l'ANC doit passer la frontière,
  et bancs mémoire avant toute décision « tout-v210 ».

## Mode tranche : réglage global, GPU multiview et robustesse — 2026-07-12

Consolidation du chantier tranche : l'activation devient un **interrupteur unique**, le chemin
GPU est validé de bout en bout, et la chaîne encaisse un soak de 4 heures.

- **Réglages → Vidéo → « Mode tranche (latence sous-image) »** : un seul interrupteur global
  (défaut : désactivé). Activé, les containers compatibles se déploient en mode tranche et le
  tissu de composition passe en cadence flow ; les flux entrelacés restent en image entière
  (repli automatique). Prise d'effet au redéploiement de chaque container. Les réglages tranche
  par plugin sont retirés des panneaux ⚙ (ils restent pilotables par l'API/le tissu) — le seul
  réglage visible par flux reste le rythme d'émission des destinations 2110.
- **Multiview GPU en tranche** (0.27.x) : banc de qualification sur Tesla T4 → verdict
  « méga-bandes » (le PCIe est quasi gratuit, c'est le lancement des kernels qui interdit les
  bandes fines). Implémentation `gpu_batch_bands` (lots de 4 bandes, D2H recouvert par stream,
  équivalence octet stricte avec le chemin CPU). Banc managé : mur 2×2 à 50 fps avec 29 % de
  marge, 1ʳᵉ bande 2,5 ms plus tôt que le GPU image entière. Les blends fusionnés profitent
  aussi au whole-frame GPU (12,4 → 7,9 ms). Image `bobi-compute-gpu` 0.2 (la 0.1 n'avait pas le
  patch slices — 3ᵉ occurrence de cette famille de défaut d'image, désormais toutes corrigées).
- **v210 ↔ planar chiffré sur GPU** (donnée interop MXL) : conversion aller-retour 0,19 ms/trame
  1080p sur T4 contre 17,8 ms en CPU (×95) ; un bus v210 coûterait +0,41 ms par étage GPU.
  Conclusion : les conversions v210 ont vocation à vivre aux frontières (moteur SIMD, GPU),
  jamais dans les plugins numpy.
- **Soak 4 h validé** (moteur 0.42.0, émission décalée 9 ms) : 720 000 trames, 6 manquantes
  (0,0008 %), 5 imputables à la contention de builds sur le nœud. GC CPython **discipliné** dans
  l'assembleur et le shard (collecte manuelle au point sûr de fin de trame, `gc.freeze` — le
  gen2 passe de ~40 ms n'importe quand à ~0,1 ms cadencé, métrique `gc_full_ms` sur :8080).
- **Robustesse** : `push_tx_slots` attend la disponibilité du contrôleur moteur (boot à froid
  ~30-60 s) avant de pousser les slots TX — les destinations ne restent plus muettes après un
  redéploiement du moteur. Les textes d'aide des champs de configuration (config_schema `help`)
  sont enfin rendus dans la palette et les panneaux ⚙.

## Plage IP conteneurs par nœud + rôle « Management + Containers » — 2026-07-12

Un nœud d'un autre réseau peut désormais rejoindre le cluster avec ses conteneurs sur **son**
subnet (cas réel : nœud GPU dans un autre /24 que le reste du cluster).

- **Nouveau rôle de carte « Management + Containers »** : la carte porte l'IP de contrôle ET le
  réseau macvlan des conteneurs.
- Rôle Containers ou Management + Containers → choix de la **plage d'allocation** : plage du
  cluster (défaut) ou **plage personnalisée** (début/fin, validée contre le subnet de la carte,
  sans chevaucher IP de la carte, passerelle ni hôte).
- **Allocateur node-aware** : chaque nœud alloue dans sa plage effective ; les IP sont comptées
  par plage (deux subnets ne se bloquent plus mutuellement) ; stats ventilées par plage dans les
  Réglages. Un nœud sans carte containers configurée garde exactement le comportement actuel.

## Nuit d'interventions : perfs, UI et finitions — 2026-07-12

Lot de correctifs et d'améliorations traités en parallèle dans la nuit.

- **avsync ×21** (0.10.0) : le rendu de la mire convertissait RGB→YUV en flottant à chaque
  image (68 ms) — fonds précalculés en YUV + rendu zéro-copie dans le grain : 3,8 ms/image,
  cadence cible 50 fps atteinte (16,6 avant). Et la page de réglages ne perd plus les champs en
  cours d'édition au rafraîchissement.
- **UDC ×3,5** (0.7.0) : noyau bilinéaire fusionné en C (embarqué dans le script, aucun rebuild
  d'image), 121-169 fps sur un cœur contre 32-55 — le bilinéaire redevient tenable à 50p sur
  nœud chargé. Opt-in `bilinear_fast`, repli automatique sur numpy, octet-identique.
- **stream_in en mode tranche** (0.3.0) : sortie publiée par bandes comme player/stills. La
  validation a révélé que l'image `bobi-media` 0.6 n'avait pas le patch slices (les producteurs
  médias dégradaient silencieusement en image entière) → corrigé, `bobi-media` 0.7, player et
  stills re-validés dessus.
- **Page Câbles** : le détail d'un câble n'affiche plus que SA source et SA destination (au lieu
  de tous les flux des deux extrémités), et la zone de survol/clic des câbles est élargie.
- **Nommage** : les entrées s'affichent « RX #1 », « RX #2 »… (symétriques des TX #n, les noms
  techniques restent inchangés en interne) ; le badge de type affiche « 2110 » au lieu de « MTL ».
- **Mélangeur** : un mixer déployé sans format explicite prend le format par défaut des Réglages
  (fini le 720p surprise face à des sources 1080p).
- **Supervision DPDK** (branche narrow) : les pages Sources/Destinations 2110 affichent les
  sessions RL utilisées/plafond par port à la place des files XDP sur les nœuds DPDK, et le
  bouton « + Ajouter un TX » est borné sur la vraie limite de la carte.
- **Moteur 0.41.x** : logs du watchdog TX throttlés (1/min agrégé au lieu de 30), compteur
  `late` immunisé contre la contre-pression de l'émission décalée (il ne compte plus que les
  vrais retards source). **split** 0.7.1 : compteurs de supervision tranche alignés sur
  multiview/mixer. **Page Aide** mise à jour plugin par plugin (LXC/Proxmox purgés, article
  « Mode tranche », aide créée pour la sonde 2110).

## Émission décalée (TROFF) : le choix du rythme par destination 2110 — 2026-07-11

Une chaîne interne dont l'image est prête quelques millisecondes après le début du cycle 2110
payait une image entière d'attente (l'émission ne peut commencer qu'à la frontière d'epoch).
Chaque destination TX offre désormais le choix :

- **⏱ Image suivante** (défaut) : émission alignée sur l'horloge nominale — interop stricte.
- **⚡ Émission décalée +N ms** : l'image part dès que ses premières bandes sont prêtes, N ms
  après le début du cycle. Le décalage est déclaré dans le SDP (`a=troff`, ST 2110-21) et
  l'horodatage RTP ne change pas : la synchro A/V du récepteur est préservée, il lui faut
  simplement une marge de tampon équivalente.
- Mesuré sur la chaîne tranche complète (décalage 9 ms) : le contenu traverse TX→fil→RX **dans
  le même cycle** (fini le +1 image), 1ʳᵉ bande reçue à +10,7 ms du cycle au lieu de +21,7 —
  **gain ≈ 11 ms**. Mécanique portée par un patch libmtl bobi (grille d'émission décalée d'un
  bloc : fenêtre, acceptation d'epoch, rate-limiter ; le timestamp reste nominal).

## Mode tranche MXL : la latence passe sous l'image — 2026-07-11

Chantier majeur : le bus MXL et toute la chaîne de traitement passent en **publication par
bandes** (opt-in). Au lieu d'attendre une image complète à chaque étage, chaque étage publie et
consomme l'image par tranches de 36 lignes au fil de l'eau.

- **Fondation MXL** : grains publiés en N tranches (commit progressif `validSlices=1..N`),
  lecture `get_slice` réveillée à chaque commit partiel. Compatibilité totale : un consommateur
  non migré lit toujours des images complètes, chaque maillon s'active indépendamment.
- **Moteur 2110** (0.40.x → 0.42.0) : réception ET émission en tranche (st20 slice-level,
  conversion SIMD par bande) — l'émission d'une image commence avant la fin de son écriture
  amont. Gel d'image propre sur source figée, watchdog d'anneau.
- **13 plugins migrés et mesurés en direct** : multiview, pyramide, UDC, correcteur, délai,
  split, mixer, avsync, player, recorder, stills, streamer, stream_in. Un étage interne coûte
  désormais ~1 à 3 ms au lieu de 20 (en 50p) ; le streamer gagne ~18 ms de bout en bout.
- **Tissu de composition en cadence « flow »** : tick d'horloge TAI + ciblage d'index d'epoch —
  les étages du multiview composé s'alignent par construction, sans barrière. Alignement
  parfait mesuré sur la chaîne complète (retard d'index 0 entre étages).
- **Garde-fous** : budgets d'attente par tuile, repli sur la dernière image complète (jamais de
  blocage), détection des générations orphelines, signalement des sources chroniquement en
  retard (alerte orchestrateur avec suggestion de délai d'une image). Le mixer garantit une
  sortie jamais en retard (verrou de référence indexé, budget de trame dur).

## Fiabilité contrôleur : fuite de descripteurs SQLite résolue — 2026-07-11

L'orchestrateur pouvait devenir injoignable sous une rafale de créations de conteneurs
(erreur « Errno 24 », interface morte, tempête de logs). Cause racine : une connexion SQLite
ouverte à chaque appel et collectée trop tard, multipliée par la limite souple de 1024
descripteurs du service.

- Connexions SQLite **par thread** (réutilisées) au lieu de par appel : le plateau passe de
  ~400 descripteurs en croissance continue à ~60-80 stables.
- Limite du service relevée (LimitNOFILE 65536) en ceinture-bretelles.


## Stream In : refonte de l'interface + journal de connexion exportable — 2026-07-09

Le plugin `stream_in` passe en 0.2.1. Le panneau de contrôle devient l'outil d'exploitation
de la source, et le formulaire de configuration se dégonfle de 17 champs à 7.

- **Le format de sortie vient du sélecteur universel de la palette** (Réglages → Formats vidéo).
  Il était redéclaré dans le `config_schema` du plugin, si bien que `collectPluginConfig`
  **écrasait** le préset choisi : deux sources de vérité, dont une gagnait en silence.
- **Point d'entrée éditable en place** : l'adresse affichée est l'adresse réglable (port,
  application et clé RTMP, ou URL distante). Appliquée à chaud par le hook `control_action`,
  sans redéploiement ni coupure de la sortie MXL.
- **Interrupteur marche/arrêt de l'ingest**, **sources récentes** (8 dernières, par instance,
  via `plugin_store`), **format réel de la source** affiché avant conform, et **courbe du débit
  entrant sur 2 minutes** annotée des images gelées et des silences audio.
- **Journal de connexion persistant** (JSONL, roté), consultable par plage date/heure et
  **exportable en CSV** : la pièce justificative pour établir d'où venait une coupure.
  Horodatage conteneur, discipliné PTP. L'effacement demande confirmation et se consigne.
- **Nouveau `state_volume` déclaratif** (`app/plugins.py`, `app/docker_compute.py`), calqué sur
  `media_volume` : un plugin qui le déclare reçoit un dossier persistant du nœud
  (`/var/lib/bobi/state/<hostname>` → `/var/lib/bobi`). Le rootfs d'un conteneur est **recréé**
  à chaque déploiement et au boot du nœud : sans ce volume, le journal disparaissait
  précisément quand il sert. Le panneau avertit si le volume manque (conteneur ancien : le
  recréer depuis la palette).
- Correctif : les lignes `Stream #0:x` des sections `Input` et `Output` de ffmpeg ayant la même
  forme, le plugin décrivait sa propre sortie comme étant la source.
- Le rendu de `config_schema` accepte `visible_if` avec une **liste de valeurs**.

## Nouveau plugin Stream In : ingest SRT/RTMP vers le bus MXL — 2026-07-09

Nouvelle **source réseau** dans la rubrique Sources : le plugin `stream_in` (0.1.0) ingère un
flux SRT ou RTMP via ffmpeg et le publie sur le bus MXL, câblable comme n'importe quelle source.

- **4 modes d'ingest** : SRT écoute (la source pousse vers l'IP du conteneur), SRT appel,
  RTMP serveur (push OBS/encodeur), RTMP pull. L'UI affiche l'URL d'ingest à copier
  (IP macvlan de l'instance, joignable en direct sur le LAN).
- **Sortie conformée au format configuré** (résolution, cadence, chroma, profondeur,
  balayage) : le flux entrant n'a jamais besoin d'être au bon format — scale, conversion de
  cadence et désentrelacement automatiques avant écriture sur le bus.
- **Signal continu genlocké** (grille PTP, modèle player) : mire ou noir en attente de flux,
  freeze de la dernière image si le flux gèle (seuil réglable), redémarrage automatique de
  ffmpeg — le signal MXL ne s'interrompt jamais.
- **Audio 8 canaux** décodé vers un shm MXL (silence si absent, bascule vidéo seule
  automatique quand le flux n'a pas de piste audio).
- Rendu du `config_schema` : `visible_if` accepte désormais une **liste de valeurs**
  (appartenance) en plus de l'égalité simple — profite à tous les plugins.
- **Audio corrigé (0.1.6)**, trois défauts distincts :
  0. *Sautillement* — le bus MXL a un **contrat de granularité de 1 ms** : le streamer lit par
     blocs de 1 ms et injecte du silence dès qu'un passage ne trouve pas de bloc complet.
     Écrire par blocs de 5 ms faisait avancer `head_index` par sauts de 5 ms, le consommateur
     tournait à vide et bourrait du silence. Écriture ramenée à 1 ms (comme player/avsync),
     période du feeder < 1 ms. Mesuré : 0 silence injecté sur 6 s, à parité avec le player.
     Le réglage `mtl_audio_ptime` (1 ms / 125 µs) est le packet time **réseau** ST 2110-30 du
     moteur 2110_io, pas la granularité du bus — rien à suivre côté producteur MXL.
  1. *Clics/bruit* — `bobimxl.AudioWriter.write()` sans index explicite recalcule
     `mxlGetCurrentIndex() - n` à **chaque** appel : deux écritures rapprochées retombent sur le
     même index (écrasement) ou en sautent (les vieux échantillons du ring ressortent).
     ~220 discontinuités/s mesurées. Le feeder ancre maintenant une fois sur la grille
     d'échantillons TAI et écrit à un index strictement contigu.
  2. *Audio en retard* — entrée et sortie tournant toutes deux à 48 kHz, le tampon ne se vide
     jamais seul et conservait le retard déposé par la rafale de démarrage de ffmpeg (~190 ms).
     Capacité (200 ms) et latence visée (30 ms) sont désormais distinctes, avec drainage au
     seuil haut. Latence mesurée : 40–60 ms, sans drop ni famine.

  Branche audio ffmpeg : `aresample=async=1` + `-flush_packets 1`. `:8080` expose
  `audio_buf_ms`, `audio_starve_blocks`, `audio_drop_samples`, `audio_reanchors`, `audio_in_sps`.
- **Correction `deploy.py`** : le hook `source_shm` d'un plugin qui n'a **pas** de hook
  `before_deploy` levait `UnboundLocalError: _st` (import lié dans une branche conditionnelle),
  laissant les colonnes source/shm_out du dashboard à « — ».

## Réglages plugins : scope user/system + panneau Réglages sur les pages plugin — 2026-07-05

Préparation du multi-utilisateurs : chaque champ de `config_schema` porte un **scope**
(`system` par défaut — structurel, palette Containers ; `user` — exploitation, page plugin).

- **Nouvelle permission `plugins.operate`** (contrôle live + réglages user) et nouveau rôle
  **Exploitant** : pilote les plugins sans pouvoir déployer/détruire. Le proxy de contrôle
  `/api/containers/<vmid>/plugin/<path>` exige désormais `plugins.operate` (au lieu de
  `containers.deploy`), de même que `/control/<action>` (GEN/IDENT/tone) et l'écriture du
  plugin store (presets) — tout le plan « exploitation » est aligné sur la même permission.
- **Bouton « ⚙ » discret sur les pages plugin** (Traitements/Médias/IO, en haut à droite de
  l'instance sélectionnée, admin seulement) : popover des réglages **structurels** (scope
  `system` : n_inputs du mixer, dimensionnement pyramide/delay…). Les champs `user` ne sont
  pas dupliqués là : l'UI de contrôle du plugin les couvre déjà. Un plugin dont l'UI gère
  aussi sa structure (composer multiview) opte out via `settings_panel: false`. Appliquer
  redéploie le script (garde-fou de confirmation conservé pour le moteur 2110). Nouvelle
  route `POST /api/containers/<vmid>/plugin_config`, droits vérifiés PAR CLÉ côté serveur
  selon le scope. Garde-fous : container arrêté → les params sont seulement persistés (pas
  de redémarrage implicite) ; version de plugin épinglée préservée ; merge relu sous verrou
  vmid (pas d'écrasement d'un câblage concurrent) ; anti-rafale (409 si déjà en cours).
- **Palette Containers allégée** : elle ne rend plus que les champs structurels ; les champs
  scope `user` (styling multiview, délai courant, overlay mixer, mire avsync) migrent vers
  les pages plugin. La **section Simulateur du 2110_io** disparaît de la palette (pilotage
  live GÉN/IDENT depuis la page Sources 2110).
- **Fix de fond au redeploy** : `/deploy` merge désormais `deploy_defaults` ← **params
  persistés** ← POST. Un champ non réémis par la palette garde sa valeur courante au lieu
  de retomber au défaut du manifeste (ancien piège connu).

---

## Bande passante mémoire : canary embarqué dans l'agent-nœud — 2026-07-05

Le canary memcpy (indicateur de contention RAM, celui qui alerte « les multiviews vont
décrocher ») ne passe plus par un exec distant toutes les 60 s : l'**agent-nœud 0.14.0**
le mesure lui-même en tâche de fond et publie le débit dans son heartbeat `/v1/health`.

- **Zéro commande distante par mesure** : le contrôleur récupère la valeur dans le poll
  santé existant (5 s) et garde la référence par nœud, les ratios et les alertes.
- **Compatible flotte hétérogène** : un nœud à agent < 0.14.0 (ou legacy SSH) reste
  mesuré par l'ancien canary one-shot, bascule automatique dès la mise à jour de l'agent.
- Réglable côté nœud (`membw_interval_s`, `membw_sample_mb` dans la config de l'agent) ;
  l'UI (Monitoring → Serveurs, pastille santé du dashboard) est inchangée.

---

## 2110/NMOS : correctifs d'interopérabilité (diagnostic lab Horace) — 2026-07-04

Corrections issues d'un diagnostic terrain complet (switch L2 pur + convertisseurs
Blackmagic), chaque point prouvé sur le matériel.

- **SDP conformes RFC 7273** : le grandmaster PTP des lignes `a=ts-refclk` s'écrit
  désormais en **tirets** (`50-0F-80-…`), le `:` final séparant le domaine. Avec des
  `:` partout, un récepteur strict ne pouvait pas parser le domaine.
- **Filtre de source SSM désactivable** (Réglages → Protocoles → NMOS) : sur un switch L2 en IGMP
  snooping pur (sans PIM/NBM), le join SSM (S,G) déclenché par `a=source-filter` est
  enregistré mais **jamais forwardé** → 0 Mbps silencieux. Décocher le réglage omet la
  ligne des SDP TX → le récepteur retombe sur un join (*,G) qui est livré. Défaut :
  activé (bonne pratique sur fabric SSM-capable). Appliqué au redéploiement du moteur.
- **Conformité IS-05** : les paramètres du transport_file d'un receiver (multicast,
  port, source) sont désormais visibles dans `/active.transport_params` — un contrôleur
  externe ne voit plus les receivers abonnés comme « non abonnés ».
- **Défauts multicast uniques** : un sender resté aux adresses par défaut n'écrase plus
  le flux d'un autre slot (défauts dérivés de l'index du slot / du container).
- Au passage : correction d'un `TypeError` latent qui désactivait silencieusement le
  contrôle de format au câblage (`wire_format_gating`).

---

## Mélangeur : contrôle via le proxy plugin générique — 2026-07-04

Le contrôle live du mélangeur (PGM/PVW, take, T-bar, keyer, DSK, preview) passe par le
**proxy plugin générique** `/api/containers/<vmid>/plugin/*`, comme les autres plugins
(multiview, split, correcteur…). Les 13 routes dédiées `/mixer/*` de l'orchestrateur
sont supprimées (mixer 0.7.0).

- Nouveau : `control.read_endpoints` au manifeste — les endpoints de **lecture**
  (`/state`, `/preview.png`) restent accessibles à tout utilisateur connecté ; les
  actions exigent toujours la permission de déploiement.
- Aucun changement fonctionnel sur la page mélangeur ni sur le pupitre ATEM.

---

## Compatibilité Blackmagic, TSL central, IS-05 manuelle — 2026-07-03

- **SDP TX enrichis** (moteur 2110_io 0.34.10) : filtre source RFC 4570, TTL, SSRC fixe,
  attributs `fmtp` TCS/RANGE — les récepteurs **Blackmagic** acceptent désormais les
  senders du moteur.
- **Activation IS-05 manuelle** possible sur les ressources NMOS (en plus de l'auto).
- **TSL 5.0** : distribution centralisée des overlays (UMD/tally) fiabilisée — shm résolu
  par chemin, offsets d'index d'affichage corrigés, texte UMD sans couleur de tally forcée.
- Fixes : l'allocation multicast n'attribue plus l'adresse réseau d'un bloc CIDR ;
  port par défaut par essence (vidéo/audio/anc) des plages multicast ; la page d'accueil
  ne renvoie plus d'erreur quand aucune pyramide n'existe.

---

## Allocation multicast centralisée — 2026-07-01

Les flux ST 2110 sans adresse imposée reçoivent leur groupe multicast d'un **pool géré
centralement**, avec des règles fines.

- **Règles par réseau logique ou interface physique** (Réglages → Cluster & Réseau →
  Multicast), avec granularité optionnelle par format d'essence (scan/résolution/fps
  vidéo, canaux audio).
- **Réservation atomique en base** : une adresse est réservée au moment de la demande —
  fin des collisions multicast entre containers déployés au même moment.
- Validation stricte d'une destination TX saisie à la main ; alerte « Points
  d'attention » quand une plage est épuisée.

---

## Accueil & Monitoring : état multi-nœuds, Points d'attention — 2026-07-01

- **Bandeau d'accueil** refondu pour le cluster : pastille de santé globale, nœuds en
  ligne (X/Y), ressources pire-cas, GPU/RDMA/PTP agrégés — chaque item cliquable vers
  sa page de détail. La pastille « Système » ne reflète que la santé **infrastructure**
  (les anomalies de flux ne la dégradent plus).
- **Schéma pipeline** : le moteur bi-rôle 2110_io apparaît en Sources (« Rx 2110 »)
  **et** en Destinations (« Tx 2110 »).
- **Points d'attention** (Monitoring) : en plus des conteneurs en panne, liste les
  anomalies opérationnelles live — RX/TX en famine, collisions multicast, proxies
  pyramide orphelins — avec lien « Ouvrir ».
- **Entrées gelées** : une entrée dont la source est coupée (> 5 s) est marquée « figé »
  et exclue des agrégats de latence.

---

## Moteur ST 2110 : capacité à chaud + opérations protégées — 2026-06-30

- **Réserve de files par interface** (Réglages → Réseau : Files RX / Files TX / Marge) :
  tant qu'on reste dans la réserve, on ajoute/retire des flux **sans ré-initialiser le
  moteur** — donc sans geler les multiviews en aval.
- **Plafonds levés** : jusqu'à 16 RX + TX simultanés validés sur un nœud (fin de la
  limite RX ≤ 8 / éjection des sessions TX).
- **Pré-confirmation** : toute opération qui forcerait une ré-init du moteur (ajout de
  flux au-delà de la réserve, redéploiement à chaud…) demande confirmation avant d'agir.
- Reporting XDP agrégé sur les 4 ports de la carte ; détection de **famine RX et TX**
  (flux câblé mais 0 image) avec remède suggéré.

---

## Steppers −/+ sur tous les champs numériques — 2026-06-30

Le composant stepper du multiview (boutons −/+ avec appui maintenu) est généralisé à
**toute l'interface** : Réglages, onglets de services, palette de déploiement, wizard et
panneaux de contrôle plugin.

---

## ST 2110 multi-NIC : auto-répartition + épinglage — 2026-06-30

Le moteur 2110_io exploite désormais **plusieurs ports média** sur un même nœud.

- Répartition automatique des flux RX/TX entre les ports, avec **épinglage manuel** par
  flux (à chaud, sans redéploiement).
- **Stats par NIC** : débit, queues XDP multi-segments, état PTP par port ; modèle de
  carte exact et agrégat = somme des vitesses de lien réelles.
- Le RX n'émet **plus de mire par défaut** (badge « GÉN » quand le générateur est actif).
- Plan de recette « 2110 / Multi-ports » ajouté à la page Recette.

---

## Monitoring serveurs : santé matérielle + refonte du détail — 2026-06-29

- **Santé matérielle** : températures (par puce, seuils colorés), ventilateurs et
  consommation quand le matériel les expose (agent 0.12.0). Sur HPE, ventilateurs/conso
  passent par le BMC (chantier séparé).
- **Bandeau cluster** (vue d'ensemble) : N/M serveurs up, CPU moyen, RAM agrégée, RDMA,
  grandmaster PTP commun (alerte si plusieurs GM).
- Détail serveur : tuile **Docker** (version + liste des conteneurs), réseau en **barres**
  rx/tx vs vitesse du lien, stats **24 h** (min/moy/max), sélecteur de serveurs, PTP
  coloré avec mini-graphe d'offset.
- **RDMA** : onglet dédié (liens, readiness par nœud, graphes de débit Rx/Tx par nœud).

---

## Réplication RDMA inter-nœuds (mxl-fabrics) — 2026-06-28

Un flux MXL produit sur un nœud peut être **répliqué dans le domaine MXL d'un autre
nœud par RDMA** (RoCEv2), sans aller-retour ST 2110 — fondation du cluster à rôles.

- **Câblage inter-nœud transparent** : câbler un producteur d'un nœud vers un
  consommateur d'un autre nœud provisionne le lien RDMA automatiquement (et le libère
  quand plus personne ne consomme).
- Le **format du flux est lu du flow_def MXL réel** (source de vérité écrite par le
  producteur) au câblage et au monitoring — élimine les images « torturées » quand la
  config du producteur avait dérivé du flux live.
- Page Câbles : voyant « ⇄ RDMA » sur les arêtes inter-nœud. Nouveau service
  `services/rdma` + onglet Réglages → Protocoles → RDMA (dormant sans NIC dédiée).

---

## PTP par nœud : refonte, journal d'événements, logs — 2026-06-28

Le PTP est par nœud (chaque nœud a son ptp4l/phc2sys) ; l'UI le reflète enfin.

- **État + « Appliquer » par (réseau → nœud)** : récap live par nœud (port SLAVE, offset
  au GM, phc2sys), bouton Appliquer ciblé — fin du bouton global opaque.
- **Journal d'événements PTP persistant** : bascules d'état de port, changement de
  grandmaster, perte/reprise de verrou, ptp4l up/down.
- **Visualiseur de logs** ptp4l/phc2sys par nœud×réseau (via l'agent, sans SSH), intégré
  à la section Logs du Monitoring (sélecteur de source).
- États FAULTY/DISABLED affichés en **rouge** (lien coupé), pas orange.
- Fix hugepages : réconciliation du sysctl à l'application MTL — fin du faux « nœud
  plein » (RAM à 96 %) causé par un résidu de pages 2M.

---

## Multiview / pyramide / tissu : robustesse d'exploitation — 2026-06-28

- **Nettoyage automatique des shards** à la destruction d'un mur ; câblage d'un mur
  shardé via sa définition logique + réconciliation du tissu.
- **Pyramide** : reconnexion automatique quand le shm d'une source est recréé
  (watchdog), formats d'entrée persistés au câblage, et fin de la **saute entrelacée**
  (socle d'octaves désactivé par défaut).
- **VU-mètres** : fraîcheur audio réelle (indicateurs SILENCE/ABSENCE au lieu d'un
  niveau figé), étiquettes de canaux repositionnées.
- Renommage UI : « Internes du tissu » → « Détails de composition ».

---

## Nœuds : GPU NVIDIA, vue réseau par-nœud, install iLO/PXE — 2026-06-27

- **Compositing multiview sur GPU NVIDIA** (optionnel) : image runtime dédiée, plomberie
  agent (`--gpus`), réglage par mur ; tuile GPU (util/VRAM/PCIe) au Monitoring.
- **Vue réseau par-nœud** : affectation interface→rôle (contrôle, conteneurs, 2110,
  RDMA) + intégration BMC iLO/iDRAC.
- **Install sans clé USB** : service ISO via iLO Virtual Media (Redfish) et **boot
  réseau PXE/UEFI HTTP** (serveur dédié) ; refonte de la page d'enrôlement centrée
  jeton (sélection NIC, menu boot).
- Fixes d'install : provision du **DDP E810** (sinon driver en Safe Mode → pas de PTP
  matériel), linuxptp posé même en mode différé, garde-fou hugepages 1G.

---

## Entrelacé champ-natif de bout en bout — 2026-06-25

La chaîne 1080i est traitée **en champs natifs** de la réception à l'émission.

- `bobimxl.format()` interlace-aware (fondation) ; proxies pyramide, color_corrector,
  recorder/stills/player champ-natifs ; multiview : entrées entrelacées désentrelacées
  en bob progressif.
- **Fix racine de la cadence de champ TX** (moteur 0.29.4) : émission à demi-cadence de
  champ — fin du peigne/combing sur les sorties 2110 entrelacées. Mire de test de champ
  ajoutée pour valider l'appariement.
- Format RX **par flux** (`rx_fmt`) : un même moteur accepte des entrées entrelacées et
  progressives mélangées (2026-06-26).

---

## Page « Recette » — suivi des tests — 2026-06-25

Nouvelle page `/tests` pour l'équipe de recette : checklist par rubriques (dont
« Entrelacé » et « 2110 / Multi-ports »), statut par item et **fils de discussion Q/R
horodatés** par point de test.

---

## Migration SDK MXL : vidéo, audio et ANC — 2026-06-22

Le bus mémoire partagée maison est remplacé par le **SDK MXL** (libmxl) pour tous les
types de containers.

- Phase 1 : vidéo **planar** (patch libmxl) — pyramide, multiview, mixer, udc, delay,
  avsync, color_corrector, player, recorder, stills, streamer, split.
- Phases 2-3 : **audio** (float32) et **ANC/DATA** basculés sur le bus MXL.
- Le moteur 2110_io produit et consomme du MXL (0.23.x) ; coexistence PTP↔RX via RSS.
- Article d'aide « bus MXL & format vidéo » (pourquoi planar).

---

## Flux composables Sources/Destinations 2110 — 2026-06-22

- Les RX/TX du moteur s'ajoutent et se **retirent à chaud** (libération des sessions
  sans redéploiement) ; boutons « Retirer un RX/TX » sur Sources et Destinations.
- Les câbles TX/streamer sont **re-poussés automatiquement** au redéploiement d'une
  source (fin des sorties figées après édition d'un multiview).
- Auto-réparation des transportfiles SDP des senders NMOS du moteur.
- Ménage final post-migration : retrait des plugins legacy `receiver_2110`/`sender_2110`
  et des dernières références Proxmox/LXC (docs, réglages, code mort).

---

## Nœuds : assistant guidé, build & distribution d'images — 2026-06-20

- **Assistant de configuration étape par étape** d'un nœud (réseau, queues AF-XDP,
  hugepages, images) avec vérifications réelles à chaque étape.
- **Build de bobi-mtl directement sur le nœud** via l'agent (suivi du build, chrono,
  erreurs visibles) ; autres images : **build-once + auto-distribution** aux nœuds.
- **Mise à jour de l'agent-nœud depuis l'UI** (sans SSH).
- NMOS : UI registre/pool/graphe intégrée.
- Réseau macvlan durci : la passerelle conteneurs = **routeur du segment** (jamais l'IP
  de l'hôte) ; le réseau est recréé si passerelle/subnet/parent change.

---

## Installeur unifié + enrôlement des nœuds — 2026-06-19

- **Installeur unifié sans Proxmox** (menu « Nouveau serveur » : nœud / contrôleur),
  navigation clavier, écrans encadrés plein écran.
- Le nœud **s'annonce au contrôleur** à la fin de l'install (push + jeton) ; page
  d'état publique de l'agent sur `:80`.
- Clé USB d'enrôlement gravée depuis le contrôleur ; épinglage du noyau pour io2110.
- Monitoring : santé matérielle des nœuds (remplace la console Proxmox) ; `/dev/shm`
  compté en mémoire, pas en disque.

---

## Bande passante mémoire + capacité MTL réelle — 2026-06-18

- **Mesure et alerte de bande passante mémoire** par nœud (canary memcpy) — la
  contention RAM est le vrai plafond des murs multiview.
- Le cap de sessions MTL est calculé au **nombre réel de flux** (fin du ÷3 forfaitaire) ;
  files AF-XDP pré-réservées au déploiement du moteur.

---

## Réglages : refonte des menus en 7 groupes — 2026-06-17

La page **Réglages** passe d'une longue barre d'onglets à plat à une **navigation à deux
niveaux** : une barre de **groupes** au-dessus, puis les onglets du groupe en dessous.

- **7 groupes par domaine** : *Général* (Apparence, Comptes, Labels) · *Cluster & Réseau* ·
  *Nœuds & Matériel* (Réseau hôte, CPU, Déploiement) · *Signal & Format* (MXL, Vidéo) ·
  *Médias* (Fichiers, Media Manager, Stockage) · *Protocoles & Pupitres* (NMOS, Ember+, TSL,
  ATEM, Skaarhoj, WebRTC) · *Système* (Services, Plugins, Sauvegardes, RAZ).
- La **carte CPU** devient son propre onglet, **par-nœud** (sélecteur de nœud).
- Nettoyage des références **Proxmox** résiduelles (libellés, lignes d'info mortes).
- L'onglet actif est mémorisé dans l'URL (rechargement → même page).

---

## Haute disponibilité : paire de contrôleurs (warm-standby) — 2026-06-17

L'orchestrateur peut désormais fonctionner en **paire** : un contrôleur **actif** qui pilote
la production et un **standby** prêt à prendre le relais. Bascule **manuelle** (pas
d'automatisme qui pourrait partir en *split-brain*).

- **Rôle de contrôle** (Réglages → Haute disponibilité) : *actif* pilote tout ; *standby*
  démarre **passif** — interface en **lecture seule**, ne pilote aucun nœud ni service.
- **Réplication d'état** : l'actif pousse périodiquement une **copie cohérente de la base**
  vers le standby, qui la met en attente (sans l'appliquer).
- **Bascule manuelle** : *Promouvoir* applique la dernière copie reçue et redémarre en actif
  (sauvegarde de sûreté automatique) ; *Rétrograder* repasse en veille.
- **VIP de management manuelle** : procédure documentée (`HA.md`) — rétrograder l'actif →
  déplacer la VIP → promouvoir le standby.
- Le pilotage d'un nœud distant passe entièrement par l'**agent de nœud** (jeton HTTP),
  build d'images compris : **plus aucun accès SSH root** requis depuis le contrôleur.

---

## Réseau cluster : 3 plans, IPAM centralisé, multicast — 2026-06-16

Le réseau est maintenant pensé pour un **cluster multi-nœuds**.

- **Topologie réglable** (Réglages → Cluster & Réseau) : *Simple* (management + conteneurs
  sur un réseau, comme avant) ou *Séparé* (réseau de management + réseau conteneurs **privé
  dédié**). Le plan **ST 2110** reste toujours physiquement séparé (SR-IOV).
- **Allocation d'IP centralisée** du plan conteneurs (une IP par conteneur, stable au
  redéploiement) dès qu'on est en multi-nœuds ou en mode séparé.
- **Pool multicast cluster-unique** : les flux 2110 sans adresse imposée reçoivent un groupe
  multicast d'un pool géré centralement, avec **détection de collision**.

---

## Identités NMOS de niveau cluster — 2026-06-16

Les ressources NMOS (senders/receivers IS-04/05) ne sont plus liées au cycle de vie d'un
conteneur : elles vivent dans un **registre de cluster stable**.

- Un **Device cluster unique** possède tous les senders/receivers ; leurs **UUID, SDP,
  multicast et refclk sont persistés** → un recreate/restore de conteneur ne casse plus les
  abonnements des contrôleurs NMOS externes.
- Une ressource **non servie** par un conteneur reste exposée **inactive** (le routage
  survit à la disparition du conteneur).
- **Éditeur Réglages → NMOS** : renommage, suppression d'orphelines, **rebinding explicite**
  (associer un flux d'un conteneur à une ressource précise du registre).
- **Identité d'instance portable** (`instance_uuid`) embarquée dans les snapshots de projet :
  *copie* régénère l'identité, *déplacement* la conserve.

---

## Réglages par-nœud (préparation hôte multi-nœuds) — 2026-06-16

La préparation matérielle de l'hôte (Ethernet, SR-IOV, PTP, MTL, CPU) est désormais
**par-nœud**, plus « hôte global ».

- Un **sélecteur de nœud** en tête des onglets *Réseau hôte*, *CPU* et *Déploiement → Local*.
- Le **moniteur PTP** échantillonne tous les nœuds (état/historique par nœud).
- Fondations d'override de réglages **par-nœud** (stockage + résolution *override > global*).

---

## Full-Docker : fin de Proxmox / LXC — 2026-06-16

L'orchestrateur ne dépend plus de Proxmox ni des conteneurs LXC : **tous les types tournent
en Docker**, sur des nœuds pilotés par un **agent de nœud**.

- Suppression du chemin de cycle de vie LXC et du client API Proxmox (connexion, template
  299, recréation) — code et réglages associés retirés.
- Les conteneurs sont créés/détruits sur les nœuds **Docker** ; un nœud vierge s'enrôle via
  l'agent (jeton), sans Proxmox.
- Résolution d'IP de conteneur unifiée (IP propre macvlan, ou IP de l'hôte en `--network host`).

---

## VMID : allocation illimitée — 2026-06-15

La **plage de VMID** n'est plus un plafond : l'allocation est **monotone** (toujours le plus
petit identifiant libre au-dessus du plancher), un VMID redevenant un simple **handle local**
réattribué librement à l'import d'un projet. Fini l'épuisement de plage.

---

## Passerelle WebRTC sous Docker (image pré-bakée) — 2026-06-14

La passerelle WebRTC (MediaMTX) peut désormais être déployée en **conteneur Docker**
sur un nœud, en plus du mode LXC historique.

- Image dédiée **`bobi-webrtc`** avec le binaire **MediaMTX pré-baké** → démarrage
  immédiat, sans téléchargement au premier lancement ni dépendance Internet du conteneur.
- Réseau **macvlan** : la passerelle reçoit sa propre IP du réseau (suivi des IP libres
  géré par Docker), joignable directement par les encodeurs (push RTSP/WHIP) et les
  navigateurs (lecture WHEP).
- Sélecteur **« Cible »** (LXC local / nœud Docker) dans Réglages → WebRTC.
- **Indispensable** sur les nœuds tout-Docker dépourvus du template LXC 299, où la
  passerelle LXC ne pouvait plus être déployée.

---

## Streamer : encodage sur l'image média (ffmpeg) — 2026-06-14

Le **streamer** (encodeur/monitor) est désormais déployé sur l'image **`bobi-media`**
(qui embarque ffmpeg) et non plus sur l'image de calcul `bobi-compute`.

- Corrige un **crash en boucle** des encodeurs Docker : le streamer appelle ffmpeg, absent
  de l'image de calcul → le script mourait au lancement et le conteneur était recréé sans fin.
- Métriques `in_fps_seen` / `pushed_fps` / `dropped_stale_fps` désormais **renseignées en
  mode moniteur** (elles restaient figées à 0 alors que le flux tournait) : `in_fps_seen`
  reflète la cadence réelle de la source surveillée (détecte une source figée).

---

## Page Câbles : refonte complète — 2026-06-14

La page **Câbles** a été retravaillée en profondeur pour les topologies chargées.

- **Filtres** par version, par nœud et par type ; **repli** (collapse) des cartes.
- **fps par flux** affiché sur chaque liaison, en plus des badges de latence.
- **Vue libre zoomable** : disposition manuelle des cartes, zoom/pan, mémorisée.
- **Dots tally live** sur les ports de sortie (état tally en temps réel par source).
- Suite du **suivi de latence par étape** (arête = transit, nœud = traitement) avec
  bascule délai propre / délai cumulé, et badges `⧖`/`Δ`.

---

## Pyramide : nouveau plugin scaler de proxies — 2026-06-14

Nouveau type **Pyramide** : un générateur de **proxies multi-résolutions** (octaves) d'une
source, produits **à la demande** et appliqués à chaud.

- Socle d'octaves réglable ; tailles intermédiaires créées au besoin (hot-apply, sans
  redéploiement).
- **Multiview** consomme ces proxies de façon **opportuniste** (la bonne taille pour
  chaque tuile → moins de CPU de redimensionnement), avec tolérance d'upscale.
- Monitoring intégré (console des proxies, KPI/alertes).

---

## Multiview : habillages, overlays et compositing accéléré — 2026-06-14

Évolution majeure du **Multiviewer** (composer v0.14.x).

- **Overlays** texte / horloge / image par tuile, rendus côté image (bakés) ; horloge
  **source ANC** (timecode embarqué) ; chip de format et de colorimétrie.
- **Compositing ~3× plus rapide** (consolidation des couches, blend mémoire-borné) →
  cadence pleine retrouvée sur les mosaïques denses.
- **Habillages** (bandeaux, tally) avec jeton d'accent configurable ; banque d'entrées ;
  même source autorisée sur plusieurs entrées.
- **Résilience SIGBUS** : un multiview lisant le shm d'un autre module survit à la
  recréation du shm amont (handler + auto-restart).
- Persistance des réglages appliqués à chaud (fenêtre/habillage) dans la configuration.

---

## fps glissant uniformisé + refresh à chaud des consommateurs — 2026-06-14

- Le **fps** exposé par tous les plugins est désormais un **débit glissant** (sur ~1 s) et
  non plus un cumul depuis le démarrage, qui pouvait laisser croire à un goulet d'étranglement.
- Au **changement de format d'une source**, les consommateurs en aval sont **rafraîchis à
  chaud** (réouverture du shm aux nouvelles dimensions) sans redéploiement manuel.

---

## TSL 5.0 centralisé + éditeurs de sources — 2026-06-12

Refonte **source-centrique** du tally et des labels.

- Service **TSL 5.0** centralisé : multi-connexions, 10 niveaux de tally (LH/RH/TT par
  connexion), 10 colonnes de labels par source ; distribution automatique vers les multiviews.
- Page **/labels** : éditeur tableur des sources, indépendant du TSL, avec héritage du
  `parent_shm`, audio/ANC, suffixes configurables, colonne Projet, export CSV filtré et
  import avec modal de diff.
- Page **/tsl/sources** : 100 sources pré-initialisées éditables en tableur.

---

## PTP & Sources : SDP TX upgradé + format complet — 2026-06-13

- **TX ST 2110** : le SDP annoncé est upgradé avec `ts-refclk:ptp` (référence d'horloge
  PTP réelle), résolu **côté orchestrateur** par nœud du sender (le conteneur ne pouvant
  pas interroger PTP à travers son mount-namespace).
- **Sources** : le format ST 2110 complet (vidéo et audio) est désormais lu depuis le
  **SDP** côté orchestrateur (résolution, chroma, profondeur, fréquence).

---

## MTL : santé TX, lcores auto et abonnements RX — 2026-06-12

- **Sorties TX** : badge de santé (sous-cadence / « late ») et **budget de lcores** au
  garde-fou d'activation — corrige une saturation du scheduler (sautes, « RTP alignment
  failure ») quand trop de sessions TX partageaient un cœur.
- **Audio ×2 + ANC** (2110-30/40) sur les sorties TX.
- **lcores auto-dimensionnés** ; surfaçage des RX « abonnées mais sans flux » ;
  re-poussée des abonnements RX NMOS après (re)déploiement du conteneur MTL.

---

## Internationalisation : chrome complet (FR/EN) — 2026-06-12

L'**enrobage i18n** de l'interface est terminé : pages Login, Public Watch, Projets,
Monitoring, I/O, Containers et la structure commune (layout) sont traduites (FR/EN),
en plus de l'infrastructure et de l'éditeur in-app déjà en place. Le choix de la langue
est proposé en première étape de l'installation.

---

## Correcteur de couleur : color balance, LUT, glow — 2026-06-13

Le **correcteur de couleur** gagne un *color balance* en espace YUV, le support des **LUT**
et un effet *glow* activable, avec divers correctifs de robustesse au câblage.

---

## Assistant de création + ATEM 9.6.x — 2026-06-11

- **Assistant de création** en 2 étapes avec **panier** multi-containers (déploiement groupé).
- **Pupitres ATEM** : support des versions de firmware **9.6.x** et bouton « Oublier ».
- **Sorties ST 2110** : IDENT / TONE / SDP et **formats adaptatifs** de bout en bout.
- **perf** : cache PTP du sampler + endpoint `/api/sources` DB-only (page d'accueil plus réactive).

---

## Hot-swap monitor : commutation sans coupure entre sources — 2026-06-09

Le monitoring ne recharge plus l'iframe lors d'une commutation entre sources de même
format vidéo tant que la source audio reste la même (ou absente des deux côtés).

- **Avant** : toute source avec audio forçait un redéploiement complet du streamer
  monitor → noir de 3-5 s à chaque switch.
- **Après** : changer de slot vidéo (ex. receiver → mires) avec le même `audio_shm`
  déclenche un hot-swap vidéo via `:8082/input`, sans toucher ffmpeg ni WebRTC.
- Déploiement toujours en mode `hot_input=True` quand la résolution est connue, y
  compris avec audio. L'`audio_feeder` envoie du silence si la source audio cesse.
- `_warm_source()` : restauration de l'état depuis le `deploy_config` en DB au premier
  appel après un restart de l'orchestrateur, pour éviter un redéploiement inutile.

---

## Monitor personnel → Docker compute — 2026-06-09

Les containers **monitor par utilisateur** (et monitors dédiés player) sont désormais
créés en **Docker compute** plutôt qu'en LXC.

- Même contrat agent (`:8081`/`:8080`/`:8082`), même reaper 10 min, même hot-swap source.
- Suppression de la dépendance au template LXC 299 pour ces containers éphémères.
- Si aucun nœud compute n'est configuré (Réglages → Nœuds), la création échoue avec
  un message explicite.

---

## Délai propre par module (badge ⧖) — 2026-06-09

La page **Câbles** affiche maintenant le **délai de traitement propre** de chaque module
dans sa carte, en complément du délai cumulé sur la sortie.

- Badge **`⧖ N ms`** dans la ligne de badges de la carte nœud = temps de traitement
  **induit par ce seul module** (correcteur de couleur, delay, AV sync, mélangeur,
  multiview…).
- Badge **`Δ N ms`** sur le port de sortie = délai **cumulé** depuis la source (inchangé).
- Permet de distinguer « ce module ajoute 2 ms » de « le signal arrive avec 40 ms de
  retard total en sortie ».
- Seuls les modules qui mesurent leur propre latence (via `inputs_latency_ms` ou
  `channels[delay_ms]` sur `:8080`) affichent le badge.

---

## Badges Δ (délai cumulé) sur les sorties TX ST 2110 — 2026-06-08

Les **badges de délai** de la page Câbles s'affichent désormais correctement
sur les **slots TX** du moteur MTL et sur tout plugin à latence fixe (Delay, AV Sync).

- **Delay / AV Sync** : shape `channels[{delay_ms}]` non lue → badge toujours 0.
  Stockée sous clé `"*"` (fallback par vmid) ; utilisée quand la latence par shm
  est absente.
- **TX MTL** : `inputs_latency_ms` imbriqué dans chaque sender → ignoré par
  `metrics.py`. Maintenant lu. Port d'entrée TX reçoit `delay_in_ms` = délai
  cumulé total (source + chaîne + TX lui-même).
- **Label tronqué** : "Émetteur 2110-20 #N" dépassait le `max-width` CSS → badge
  caché. Label raccourci en "TX #N" ; `max-width` du label port 120 → 160 px.

---

## Sorties ST 2110 MTL : audio ×2 + ANC (2110-30/40) — 2026-06-08

Les **slots TX du moteur MTL** (`receiver_2110_mtl`) émettent désormais
**jusqu'à 2 flux audio 2110-30** et **1 flux ANC 2110-40** en parallèle de la
vidéo 2110-20, sans câblage supplémentaire.

- **Automatique** : les shm audio/ANC suivent le shm vidéo câblé
  (`mtl_0` → `mtl_audio_0/1`, `mtl_anc_0`). Pas de câble supplémentaire.
- **Destinations auto-allouées** : IP multicast distinctes par slot au déploiement
  (plages `239.10.40-41.x` audio, `239.10.50.x` ANC).
- **NMOS IS-04** : senders audio 2110-30 (×2 par slot) et ANC 2110-40 enregistrés.
- **UI** : panneau de contrôle du moteur MTL affiche une section « Sorties (TX) »
  par slot avec les destinations et les fps live des 4 flux.
- Redéployer les containers `receiver_2110_mtl` existants pour activer la feature.

---

## Page Câbles : suivi des temps de traitement (délai par signal) — 2026-06-07

La page **Câbles** affiche désormais les **temps de traitement** le long du pipeline, pour pouvoir
caler un **délai audio externe** et comprendre l'alignement du mélangeur.

- **Délai cumulé par sortie** : chaque port de SORTIE porte un badge **`Δ N ms`** = délai de
  traitement **cumulé** du signal le long de la chaîne (somme des latences de chaque étage, mesurées
  en continu). C'est le retard à compenser sur un audio externe pour rester synchrone.
- **Délai par câble** : une étiquette **`+N ms`** est affichée au milieu de chaque câble = délai
  **ajouté** par ce saut (Δ ts_out−ts_in du consommateur), colorée par seuil (vert/ambre/rouge). Le
  survol garde le détail (méthode de mesure).
- **Alignement du mélangeur / DVE** : le mélangeur « casse » l'accumulation linéaire (il aligne ses
  entrées sur une référence). Chaque ENTRÉE de mélangeur/DVE porte donc un badge : **« réf »**
  (traitée immédiatement, cadence le mix), **`+N ms`** (alignée dans le budget) ou **« attend ↺ »**
  (présentée avec l'image précédente, hors budget), + le délai propre de l'entrée → on voit l'écart
  réconcilié par le mélangeur.
- Mesure = **temps de traitement MXL** (moyenne glissante), hors transport 2110 / encodage en aval.

---

## Synchronisation broadcast : genlock de toute la chaîne + cadences fractionnaires + gating — 2026-06-07

Refonte complète de la **synchronisation** du pipeline : toute la chaîne (sources → traitements →
compositeurs → sortie) est désormais **genlockée sur le PTP**, avec **latence minimale** et **support
natif des cadences fractionnaires** (29.97/59.94). Voir Aide → *Synchronisation (Genlock)*.

- **Genlock par propagation (latence minimale)** — on ne re-quantifie PAS chaque étage sur la grille
  (qui ajouterait +1 trame par étage). On genlocke seulement aux **frontières** (source RX et sortie
  antenne TX, déjà PTP via MTL) ; les étages intermédiaires sont en **verrou d'entrée 1:1** et émettent
  dès le traitement fini → latence cumulée = **somme des temps de traitement** (souvent < 1 trame),
  pas N trames.
- **Par type de module** :
  - *Player / mire / stills / multiview / UDC* (générateurs & convertisseur) → cadence **sortie sur la
    grille PTP** (`CLOCK_REALTIME` disciplinée par phc2sys). Le **player** produit en plus un signal
    **continu et stable en permanence** (lecture, pause, freeze, chargement, changement de clip — sans
    rupture ni saut de phase) avec **verrou A/V déterministe** par media-timestamp.
  - *Correcteur de couleur, Delay* (1 entrée) → **verrou d'entrée 1:1** : une trame de sortie par trame
    d'entrée (genlock hérité de la source, latence = traitement).
  - *Mélangeur, DVE* (multi-entrées) → verrou sur une **entrée de référence** + **alignement des autres
    entrées** par media-timestamp dans une fenêtre réglable (`align_budget`). La référence du mélangeur
    est **configurable** (réglage `sync_ref`, changeable à chaud) et **fixe** : elle ne suit pas la
    commutation PGM/PVW. Skew par entrée + alerte « en retard » exposés.
  - *Enregistreur* → **frame-accurate** (1 trame fichier par trame source) + plancher de cadence.
- **Cadences fractionnaires natives** (29.97/59.94/23.976) — grille **rationnelle** (num/den exact :
  29.97 = 30000/1001…) au lieu de `1/round(fps)` (qui dérivait de 0,1 %). Catalogue de formats enrichi
  (1080i59.94, 1080p59.94, 1080p29.97).
- **Contrôle de format au câblage** — un câble dont la source est **incompatible** avec le consommateur
  (résolution, chroma, cadence) est **refusé avec la raison** (« insérer un UDC ») au lieu de produire
  une image illisible. Convertisseurs (UDC/Multiviewer) et consommateurs en auto-détection exemptés.
  Réglable (Réglages → Formats → *Contrôle de format au câblage*).
- Chaque plugin porte un commutateur `genlock` (activé par défaut, repli sur l'ancien comportement
  horloge mur en cas de besoin).

---

## Recorder : timecode (0 / 2110-40 / PTP) + Docker média — 2026-06-05

Le **recorder** (v0.5.0) enregistre une **piste timecode** et passe sous Docker média (Phase 3).

- **Source TC au choix** (`tc_source`) : `zero` (00:00:00:00), `anc` (ATC décodé d'un flux **ST 2110-40** câblé en entrée — nouveau port ANC, essence `data`), ou `ptp` (heure du jour, horloge disciplinée PTP). TC écrit en piste **tmcd** via `ffmpeg -timecode`, continu entre segments.
- **Moteur ffmpeg conservé** (décision : codecs ProRes/DNxHD/FFV1 + PCM robustes sous ffmpeg ; le recorder muxe des entrées shm déjà calées, GStreamer n'apportait pas de gain de synchro ici).
- **Docker média** : `runtime: docker`, image **bobi-media**, cœurs dédiés épinglés, volume média bind-monté. Docker-only. Décodeur ATC validé contre l'encodeur du player / `mtl_rx.c`.

---

## Player : timecode ST 2110-40 (TC clip + TC antenne) — 2026-06-05

Le **player** (v0.6.1) sort désormais du **timecode** sur deux flux ST 2110-40 indépendants (Phase 2b).

- **`anc_0` — TC clip** : timecode embarqué du clip (lu via ffprobe) + position, sinon free-run depuis `00:00:00:00`.
- **`anc_1` — TC Antenne** : niveau playlist — **ascendant** (frames jouées) ou **décompte** jusqu'à la fin de playlist ou la prochaine pause (réglage via `/antennatc`).
- Format ANC identique au receiver MTL (essence `data`/`smpte291`, paquet **ATC SMPTE 12M**, DID/SDID 0x60) → consommable par un sender 2110-40 ou le recorder (à venir, mode `anc`). Encodeur validé contre le décodeur C de `mtl_rx.c`.

---

## Player : moteur GStreamer + Docker média — 2026-06-05

Le **player** (v0.6.0) passe au moteur **GStreamer** et sous Docker média (Phase 2a).

- **Moteur GStreamer** (remplace ffmpeg) : `uridecodebin` → deux `appsink` (vidéo YUV + audio S24BE 8ch) **synchronisés sur l'horloge du pipeline** → **calage A/V déterministe** (fini les process ffmpeg `-re` indépendants). Transport, playlist/rundown, marques In/Out, vitesse variable, VU 8ch : inchangés. ffmpeg reste utilisé pour les durées (ffprobe) et les vignettes de scrub.
- **Docker média** : `runtime: docker`, image **bobi-media**, cœurs dédiés épinglés (temps réel), volume média bind-monté. Docker-only.
- ⚠️ Runtime à valider sur un vrai nœud (GStreamer non testable hors conteneur). La sortie **timecode** (flux ANC) arrive en Phase 2b.

---

## stills + transcoder sous Docker média — 2026-06-05

Premiers plugins média sur le nouveau socle Docker média (Phase 1).

- **`stills` v0.3.1** : **correction** — montait sans `/mnt/media` depuis sa migration Docker (le chemin compute ne bindait que `/dev/shm`). Le bind du volume média (socle ci-dessus) le répare. Reste sur l'image `compute` (numpy/Pillow).
- **`transcoder` v0.2.0** : `runtime: docker`, image **média** (ffmpeg), profil CPU **batch** (`--cpu-shares` bas, pas de pinning) → le transcodage fichier cède aux temps réel. Script ffmpeg inchangé. Docker-only.

---

## Socle Docker média (chemin compute) — 2026-06-05

Infrastructure pour faire tourner les plugins **média** (player/recorder/transcoder/stills) sous Docker, avec accès stockage et profils CPU adaptés. Aucun plugin migré à ce stade — uniquement le socle orchestrateur.

- **Image `bobi-media`** (`plugins/_media_runtime`, tag `bobi-media:0.1`) : variante de `bobi-compute` avec **GStreamer 1.0** (base/good/bad/ugly/libav + python3-gi) pour les moteurs temps réel et **ffmpeg** pour le transcoder.
- **Nœuds** : nouvelles colonnes `media_image` (image média du nœud) et `media_mount` (stockage **local** du nœud, bind → `/mnt/media`) — éditables Réglages → Déploiement → Nœuds.
- **Sélection d'image par plugin** : champ manifeste `image: "media"|"compute"` (`plugins.image_variant`) → `deploy_compute` choisit l'image du nœud correspondante.
- **Volume média sur le chemin compute** : `deploy_compute` bind désormais le stockage du nœud (scopé au sous-dossier projet si rattaché, sinon racine) — **corrige au passage stills** qui tournait sans `/mnt/media`.
- **Profil CPU** : `resources.priority: "batch"` → `--cpu-shares` bas + pas de pinning (transcoder cède aux temps réel) ; temps réel garde cœurs dédiés épinglés.
- **Projets** : snapshot capture `node_id` ; restore et re-scoping média routent les types docker-only vers le chemin compute (plus de vaisseau LXC).

---

## UDC géométrique pur (numpy) sous Docker — 2026-06-05

L'**UDC** (`udc` v0.2.0) est recentré sur la conversion **géométrique** (résolution / frame rate / chroma 4:2:0-4:2:2-4:4:4) et passe en **runtime Docker** (docker-only) sur le chemin compute.

- **Moteur réécrit en numpy pur** (plus de ffmpeg) : rééchantillonnage bilinéaire des plans Y/U/V (résolution + chroma) + conversion de cadence sample-and-hold (fps). Tourne sur l'image `bobi-compute` telle quelle — pas de bump d'image.
- **Conversion d'espace colorimétrique retirée** de l'UDC : elle n'était que de la signalisation (tags rawvideo perdus côté shm). Elle fera l'objet d'un **plugin dédié** ultérieur.
- **Docker-only** : créé via Réglages → Déploiement → Nœuds. L'**auto-insertion d'UDC au câblage** (formats incompatibles) est routée vers le **chemin Docker compute** (nœud auto-sélectionné) au lieu de cloner un LXC.

---

## avsync sous Docker (chemin compute) — 2026-06-05

La **mire de synchro A/V** (`avsync` v0.4.0) passe en **runtime Docker** (docker-only) sur le chemin compute : créée depuis **Réglages → Déploiement → Nœuds** sur un nœud compute (plus de vaisseau LXC), avec une IP propre (macvlan) et le `/dev/shm` du pipeline bind-monté.

- Manifeste `runtime: "docker"`. Aucun changement d'image : `avsync` n'utilise que numpy/Pillow, déjà dans `bobi-compute:0.2`.
- Aucun code orchestrateur par plugin : routage compute générique (`app/docker_compute.py`).

---

## Traitements & Médias sous Docker (chemin compute) — 2026-06-05

Six plugins de calcul passent en **runtime Docker** (docker-only) sur le chemin compute : ils se créent depuis **Réglages → Déploiement → Nœuds** sur un nœud compute (plus de vaisseau LXC), avec une IP propre (macvlan) et le `/dev/shm` du pipeline bind-monté.

- Migrés : **color_corrector** (v0.4.0), **delay** (v0.2.0), **dve** (v0.4.0), **mixer** (v0.3.0), **multiview** (v0.4.0), **stills** (v0.3.0). Chaque manifeste porte `runtime: "docker"`.
- **Image compute unique** `bobi-compute` 0.1 → **0.2** : ajout de **Pillow** (`python3-pil`) à côté de numpy → débloque mixer/multiview/stills (et avsync à venir) sans image par plugin. Rebuild requis sur le nœud + réglage `compute_image` → `bobi-compute:0.2`.
- Aucun code orchestrateur par plugin : le routage compute est générique (`app/docker_compute.py`).

---

## Profil ressources par type + allocateur de cœurs — 2026-06-05

Chaque type de container peut déclarer ses ressources (`resources: {cores, memory, pin}`) dans son manifeste, appliquées en LXC comme en Docker. `app/core_pool.py` réalise un **pinning Docker non chevauchant** (modèle `nic_pool`). Le `streamer` réclame 4 cœurs : la famine CPU était une cause de dérive A/V. Inclut le **calage A/V manuel** côté streamer (fifo borné).

---

## Chemin Docker « compute » généraliste — 2026-06-04

Deuxième chemin Docker, distinct du driver MTL : exécuter n'importe quel plugin de **calcul** en conteneur sur un nœud du cluster. Le conteneur embarque l'**agent par-conteneur** (`script_templates/agent.py`, même contrat `:8081` deploy/start/stop que les LXC) ; l'orchestrateur y pousse le `script.py` rendu exactement comme pour un LXC.

- **Image unique** `plugins/_compute_runtime` (debian-slim + python3-numpy) ; réseau **macvlan** (IP propre, `:8080/:8081/:8082` fixes), `/dev/shm` bind-monté. Pas de privileged/hugepages/DPDK.
- Réglages → Nœuds : champs `compute_image` + `docker_network`. Cycle de vie complet (`app/docker_compute.py`) : create/deploy/start/stop/status/destroy + métriques.

---

## MTL (receiver_2110_mtl) : audio, ANC et entrelacé — 2026-06-04

Le receiver ST 2110 via Media Transport Library s'étend au-delà de la vidéo :
- **Audio ST 2110-30** RX + TX (Phase B2), **ptime** réglable (Réglages → MXL).
- **ANC ST 2110-40** passthrough + timecode (Phase C).
- Gestion de l'**entrelacé 1080i50** (scan / field_order bout-en-bout).
- Mire de simulation au format vidéo par défaut des réglages ; format receiver lu du SDP actif.

---

## Sources / Destinations 2110 + câblage bi-rôle — 2026-06-04

Page **/io** scindée en onglets **Sources 2110** / **Destinations 2110**, réutilisant la carte de contrôle riche du plugin (couleurs par essence vidéo/audio/ANC, GÉN/IDENT/SDP). Un moteur MTL **bi-rôle** (`split_io`) apparaît en deux nœuds sur les Câbles (RX à gauche, TX à droite) et autorise le relais RX→TX d'un même moteur.

---

## Cluster : abstraction nœud + création unifiée LXC/Docker — 2026-06-03

Premières fondations multi-nœuds : table `nodes`, **driver de déploiement Docker** (`app/docker_driver.py`), et **UI Nœuds** (Réglages → Déploiement). La palette de création unifie LXC et Docker — un « Nœud » est une déclaration de cible. Automatisation orchestrateur du déploiement `receiver_2110_mtl` (BDF VF, cpuset, push du binaire), et **prép MTL du template** automatisée, gardée par la capacité NIC du nœud.

---

## Licence : passage en GPL v3 — 2026-06-02

Le projet est désormais publié sous **GNU General Public License v3** (ou version ultérieure).
Ajout du fichier [`LICENSE`](LICENSE) (texte officiel FSF) + section *Licence* dans le README.
`LICENSE` est inclus dans le paquet de distribution (build) et donc propagé aux instances.

---

## Déploiement → Distant : mise à jour entre instances (pull/push) — 2026-06-02

Une instance peut désormais se mettre à jour à partir d'une autre sur le réseau, sans réinstaller, depuis **Réglages → Déploiement → Distant**.

- **Mode serveur** : toggle + token partagé ; expose le code de l'instance (zip « code only », jamais les secrets/bases/uploads) via `/api/update/{manifest,download}` (auth token).
- **Registre d'instances** : ajout manuel (URL + token) ou **scan du réseau** (découverte via `/api/update/ping` sur le sous-réseau). Affiche la version de chaque pair vs la sienne.
- **Pull** (mettre à jour cette instance depuis un pair) et **Push** (mettre à jour un pair depuis soi — déclenche son pull). Auth **token + vérification sha256** du zip avant extraction.
- **Robustesse** : backup horodaté de l'arbre code avant application (`dist/backup-*.tgz`) + bouton **Rollback** ; redémarrage du service hors cgroup (`systemd-run`) pour survivre au restart ; sentinelle `UPDATE_PENDING` validée au boot. `init_db()` migre le schéma automatiquement au redémarrage.
- Identité de build (`build_info.json`, commit git + timestamp) embarquée dans le zip → une instance rapporte sa version. Modules `app/updater.py` + `app/peers.py`.

---

## Déploiement → Local : hub d'installation des prérequis hôte — 2026-06-02

L'onglet **Réglages → Déploiement** se scinde en deux sous-onglets : **Local** et **Distant**.

- **Distant** : le flux existant (build d'un zip + installation d'un nœud en une commande), inchangé.
- **Local** : nouveau hub regroupant les **actions d'installation** des prérequis de l'hôte courant — la *configuration* de chaque brique reste sur sa page dédiée. Cartes : **linuxptp** (install apt, déplacé depuis Réseau → PTP), **prép MTL/DPDK** (E810), **Template LXC 299** (recréation, déplacé depuis l'onglet Proxmox — la config du template y reste), et un emplacement **Agent / paquets host** (à venir).

Prép **MTL / E810** (Media Transport Library, Intel Tiber Broadcast Suite), cible toujours l'**hôte local** :
- **Vérifier** : checklist DPDK — IOMMU (cmdline + actif), hugepages 1G, driver `ice` (E810), `vfio-pci`, DDP package, bootloader. Liste les NIC Intel/Mellanox avec leur slot PCIe et un indicateur de compatibilité MTL (E810 + ConnectX-4/5/6 mlx5).
- **Appliquer la prép** (idempotent) : ajoute `intel_iommu=on iommu=pt` + hugepages 1G au cmdline et charge `vfio-pci` au boot, via `proxmox-boot-tool refresh` (systemd-boot) ou `update-grub`. Backup horodaté du cmdline. **Aucun reboot automatique** — bannière « reboot requis » + bouton **Redémarrer l'hôte** confirmé. Module `app/mtl.py` (SSH via `ssh_run`). Pré-requis : SSH root sans mot de passe vers l'hôte.

---

## PTP : statistiques de dérive sur 24 h — 2026-06-02

La page **Réglages → Réseau → PTP** affiche désormais, sous les graphiques Offset master et Mean path delay, la **moyenne** et le **maximum atteint** sur les dernières 24 h. Le graphique (fenêtre 10 min max) ne permettait pas de quantifier la dérive lente ; ces deux valeurs la mesurent précisément.

- **Ring 24 h en mémoire** (`app/ptp.py`) distinct du buffer du graphique : moyenne signée de l'offset, max de la valeur absolue (« dérive max »), idem pour le mean path delay. La profondeur réellement couverte est affichée (`sur Xh`) tant que le buffer se remplit.
- **Persistance légère** : le ring est flushé en bloc dans un seul `ptp_stats.json` (à côté de la DB) **toutes les 5 min** (write atomique via fichier temporaire), pas une écriture par échantillon — ~288 écritures/jour de quelques dizaines de Ko au lieu de 17 280 fsync/jour. Rechargé et refiltré à 24 h au démarrage. Au pire 5 min d'historique perdues sur crash.
- Stats exposées dans `/api/ptp/status` (polling 2 s, rafraîchissement live) et `/api/ptp/history`.

---

## Déploiement : build sélectif + installation en une commande — 2026-06-01

Nouvel onglet **Réglages → Déploiement** pour dupliquer facilement une installation sur plusieurs serveurs.

- **Build sélectif** : cocher les plugins et services à inclure, puis générer le paquet (`dist/bobistudio.zip`). Le zip ne contient que le code — `config_local.py` (token Proxmox), bases `*.db` et sauvegardes sont **toujours exclus** (garde-fou qui fait échouer le build si un secret s'y glisse). Logique centralisée dans `app/builder.py`, aussi exposée en CLI (`tools/build_dist.py --all/--plugins/--services`).
- **Installation en une commande** : l'orchestrateur héberge les fichiers d'install. Sur le nœud Proxmox :
  ```bash
  bash <(curl -fsSL http://<orchestrateur>:5000/install.sh)
  ```
  Le script télécharge l'installeur + le zip et lance l'installeur interactif. Source surchargeable via `BOBI_BASE` (point d'extension GitHub).
- **Hébergement activable** : réglage `install_hosting_enabled` (défaut activé) ; coupe les routes publiques `/install.sh` et `/install/*` quand désactivé.

---

## Premier démarrage : assistant de configuration guidé — 2026-06-01

Plus de compte `admin/bobistudio` créé en dur (risque de sécurité). À la première installation :

- **Création du compte admin** : tant qu'aucun utilisateur n'existe, l'accès redirige vers `/setup` (page de bienvenue confirmant l'installation et invitant à créer l'administrateur).
- **Assistant multi-étapes** (`/setup/wizard`) enchaîné après la création : identité (nom système/entreprise/emplacement/logo), connexion Proxmox (avec test), plages VMID & réseau, **création du template de base VMID 299** (log en direct), interface ST 2110 (détection NIC + IP/leg, option PTP), explication MXL & ring buffers, format vidéo par défaut.
- Chaque étape est **facultative et saute-able** ; la complétion est mémorisée (`setup_completed`). Assistant relançable depuis **Réglages → Personnalisation**.

---

## Réglages : réorganisation des onglets — 2026-06-01

Simplification de la barre d'onglets des Réglages :

- **Thème + Personnalisation → Personnalisation** : le sélecteur de thème est intégré en première section de l'onglet Personnalisation.
- **CPU → Proxmox** : la carte des CPUs et les épinglages sont en bas de l'onglet Proxmox.
- **Ethernet + Réseau 2110 + PTP → Réseau** : regroupés sous un onglet avec trois sous-onglets (Ethernet / ST 2110 / PTP).

---

## webrtc_gateway : migration ServeurStream (service consolidé) — 2026-06-01

Le plugin `ServeurStream` est supprimé. Son script MediaMTX vit désormais dans `services/webrtc_gateway/script.py` — le service orchestre tout : déploiement, configuration et script du container. Le type en DB migre automatiquement de `ServeurStream` → `webrtc_gateway` au démarrage.

---

## Réglages : correctifs accessibilité, performance et conformité design — 2026-06-01

Passe d'audit sur la page Réglages :

- **Performance** : `AbortController` sur le `setInterval` 2s — les requêtes réseau en vol sont annulées avant chaque nouveau cycle.
- **Anti-pattern** : `border-left: 3px` sur les cards MXL Pipeline remplacé par `border-color` + `background` teinté (conforme charte design).
- **Theming** : couleur `#222` hardcodée sur la LED Skaarhoj → `var(--bg-input)` / `var(--accent)`.
- **Accessibilité** : `aria-label` sur les boutons icône `↻` ; attribut `for` sur 64 labels de formulaire.
- **Responsive** : `min-width` fixes ≥ 200 px → `min(Npx, 100%)`.

---

## Onglet Services : versioning, export/import, toggles — 2026-06-01

L'onglet **Réglages → Services** passe au même niveau de fonctionnalité que l'onglet Plugins.

### Versioning

- Chaque service déclare un `meta.json` (version, date de publication, nouveautés, corrections, bugs connus).
- Les versions antérieures sont archivées dans `services/<nom>/versions/<ver>/`.
- Cliquer une ligne déploie le panneau de gestion des versions.
- **Activer une version** promeut une version archivée en version courante (redémarrage requis).

### Export / Import

- **Exporter** un service complet (`.mxlservice`) ou une version seule.
- **Importer** un service complet (nouveau ou mise à jour) ou une version isolée dans un service existant.
- Gestion des conflits de version : confirmation avant remplacement, proposition d'activation si version plus récente.

### Activer / désactiver

- Toggle switch dans le panneau déplié de chaque service (NMOS, Ember+, ATEM…).
- Appelle immédiatement `start()` / `stop()` du module en plus de persister le setting.

### `description` dans les manifestes

Chaque `manifest.json` porte désormais un champ `description` affiché sous le nom du service dans la liste.

### Corrections UI

- Les lignes dépliées et les `<details>` de versions restent ouverts lors du rafraîchissement automatique (toutes les 5 s).

---

## Service TSL 5.0 centralisé — 2026-06-01

Nouveau service **TSL 5.0** (`services/tsl/`) dans l'onglet **Réglages → Protocoles → TSL**.

**Avant** : chaque container multiview écoutait son propre port TCP TSL, le contrôleur devait envoyer les tallies à chaque multiview séparément.

**Maintenant** : l'orchestrateur écoute sur un seul port TCP, maintient un état tally global par index TSL (avec TTL adaptatif et gestion des keepalives), et distribue les changements à tous les multiviews concernés via `POST :8082/tally_bulk`.

**Configuration** : tableau de mapping **Index TSL → container multiview + flux #** dans Réglages → Protocoles → TSL. Un même index peut alimenter plusieurs multiviews.

**Mode orchestrateur dans le multiview** : cocher *« TSL géré par l'orchestrateur »* dans la configuration de déploiement du multiview désactive son serveur TSL local — le container reçoit les tallies de l'orchestrateur uniquement.

**Avantages** :
- Un seul point de réception pour le contrôleur
- État tally consultable en temps réel depuis l'UI (`GET /api/tsl/state`)
- Base pour alimenter Skaarhoj, ATEM et Ember+ avec le même état tally

---

## Système de services infrastructure (`services/`) — 2026-06-01

Les services infrastructure (NMOS, Ember+, ATEM, Skaarhoj, WebRTC, Stockage) sont désormais des **modules autonomes** dans `services/<nom>/`, sur le même modèle que les container plugins dans `plugins/<type>/`.

### Structure d'un service

```
services/mon-service/
  __init__.py        ← logique Python (démarrage, arrêt, routes API)
  manifest.json      ← manifeste déclaratif (id, label, version, settings)
  settings_tab.html  ← onglet Réglages (chargé dynamiquement)
  lxc_script.py      ← (optionnel) script déployé dans le container
```

### Onglet Réglages dynamique

La page Réglages **découvre automatiquement** les services au démarrage. Plus besoin de toucher `settings.html` ou `routes.py` pour ajouter un service. Les onglets *Protocoles* (NMOS + Ember+), *Pupitres* (ATEM + Skaarhoj), *WebRTC* et *Stockage* sont désormais injectés dynamiquement.

### Stockage migré

Le service Stockage (Filebrowser) quitte la palette de containers et est géré depuis **Réglages → Stockage**, comme la passerelle WebRTC. Son script LXC vit dans `services/storage/lxc_script.py`.

### Fichier de log renommé

`orchestrateur.log` → `bobistudio.log`.

---

## Intégration Skaarhoj Quick Bar — 2026-05-31

Prise en charge des panels **Skaarhoj Quick Bar** (Raw Panel Protocol TCP) depuis l'onglet **Réglages → Pupitres → Skaarhoj**.

### Mappings multiples (presets)

- Créer, nommer, dupliquer et supprimer des **mappings nommés** réutilisables.
- Chaque mapping définit le **mode** (Mélangeur, Multiview, DVE, Routing XY) et l'affectation des 6 boutons.
- **Éditeur visuel** : simulation fidèle du Quick Bar (OLED + LED RGB + bouton physique). Glisser-déposer depuis la palette d'actions → slot.
- **Palette filtrée par container** : un sélecteur réduit la liste aux ports d'un container précis (utile quand plusieurs sources ont de nombreux ports).
- Chaque port `produces` d'un container est une chip distincte (vidéo / audio séparés, code couleur).
- **Tooltip au survol** (70 ms) sur chaque slot affecté : container, flux, SHM, type d'action.

### Types de boutons

| Type | Effet sur le panel |
|------|--------------------|
| **Source** (mélangeur) | Envoie le SHM en PGM sur le mélangeur cible |
| **Source** (routing XY) | Route vers la destination armée |
| **Destination** (routing XY) | Arme la destination (LED orange) |
| **Layout** | Rappelle un layout sur le Multiview / DVE cible |
| **Rappel mapping** | Bascule le panel vers un autre mapping (chip orange dans la palette) |

### Routing XY

En mode Routing, la palette propose deux sections : **Sources** et **Destinations** (inputs des containers consommateurs). Sur le panel physique : appui destination → arme (LED orange), appui source → route le SHM vers la destination armée.

### Multi-panels

- Configurer plusieurs panels dans la section **Panels connectés** (IP, port, statut live, mapping affecté).
- Chaque panel a sa propre connexion TCP indépendante et peut utiliser le même mapping qu'un autre panel.
- Migration automatique depuis l'ancien schéma `skaarhoj_mapping` au démarrage.

---

## Accueil : alignement du header — 2026-05-31

Les trois éléments de `.home-head` (nom système, produit, horodatage) s'affichaient sur des lignes différentes à cause du curseur de placement automatique CSS Grid. Corrigé en fixant `grid-row: 1` sur chaque élément.

---

## Onglet Pupitres : sous-onglets ATEM / Skaarhoj — 2026-05-31

L'onglet **Pupitres ATEM** est renommé **Pupitres** et divisé en deux sous-onglets :
- **ATEM** : contenu existant inchangé (émulateur switcher, affectation des pupitres).
- **Skaarhoj** : nouvelle intégration Quick Bar (voir ci-dessus).

---

## RAZ & rappel de projet : destruction fiabilisée + progression verbeuse — 2026-05-30

Les opérations de masse (**RAZ** des containers et **rappel de projet**) vérifient désormais réellement chaque étape côté Proxmox et affichent leur progression en direct.

**Destruction (RAZ + projets)**
- On **attend la confirmation Proxmox** de la suppression (la tâche `DELETE` est asynchrone) au lieu de marquer « supprimé » dès l'envoi de la requête.
- L'arrêt du container est **vérifié** (`status = stopped`) avant le `DELETE` — Proxmox refusait sinon la suppression d'un container encore *running*/*locked*.
- En cas d'échec : **1 nouvelle tentative** avec **arrêt forcé** (`forceStop`). Si elle échoue encore, l'utilisateur est prévenu (alerte + log) et la base **n'est pas purgée** (le container peut encore exister côté Proxmox).
- Le RAZ affiche le **détail de chaque sous-étape** (arrêt, DELETE, attente de la tâche) et dispose d'un bouton **Interrompre**.

**Rappel de projet**
- Page **Projets** : le rappel s'affiche dans un **panneau de progression en direct** (un bloc par container : clonage, IP, agent, déploiement) avec bouton **Interrompre**.
- Chaque étape est **vérifiée** : un container qui échoue (création, IP, agent ou déploiement) n'interrompt plus le rappel — les autres continuent.
- **Bilan final** `N/M OK, K échec(s)`, avec le **motif** de chaque échec.
- S'il reste des échecs, un bouton **« Réessayer les échecs »** relance le rappel **uniquement** sur les containers concernés.

---

## Accueil : graphique des stats du pipeline MXL — 2026-05-30

La page d'accueil affiche 4 **cartes-métriques** avec **sparkline live** (historique glissant ~2 min, sans librairie externe — canvas) :

- **Flux MXL** (vidéo + audio) ;
- **Grains en mémoire** (frames + chunks présents dans les rings shm) ;
- **Débit pipeline** (débit brut shared memory : Σ résolution × chroma × cadence) ;
- **Mémoire vive** utilisée (somme des containers, sur la RAM allouée).

Tout est dérivé de `/api/home/summary` (nouvel objet `stats`, agrégé depuis la topologie déjà calculée) — **aucun coût par-frame**, rafraîchi avec le poll existant (2 s).

---

## Sources : incrustation IDENT sur les receivers — 2026-05-30

À côté du bouton **GÉN**, chaque slot **vidéo** d'un receiver a désormais un bouton **IDENT** qui incruste **3 lignes** en haut à droite de l'image (fond noir, texte blanc, **taille réglable**) :

1. nom du container + du receiver (ex. `rx1 · RX0`) ;
2. **adresse multicast** reçue, ou « **Gen : &lt;mire&gt;** » en mode générateur (sinon « (pas de flux) ») ;
3. **format vidéo** (résolution · chroma · cadence).

Bascule et taille **à chaud** depuis I/O Sources (`:8082/ident`, sans respawn ffmpeg ni coupure des autres slots) ; l'incrustation est écrite directement dans la frame du ring (plan Y noir/blanc, chroma neutre). receiver_2110 v0.4.0.

---

## Câbles : insertion automatique d'un UDC en cas de formats différents — 2026-05-30

Sur la page **Câbles**, relier une source à un consommateur à **format fixe** (mélangeur, correcteur, DVE, enregistreur…) dont le format **diffère** (résolution, chroma, cadence ou colorimétrie) proposait jusqu'ici un câble qui donnait une image illisible. Désormais une fenêtre propose d'**insérer un convertisseur UDC**, par ordre de pertinence :

- **Réutiliser la sortie d'un UDC existant** si un UDC convertit déjà cette source au format voulu (simple fan-out, instantané) ;
- **Créer un UDC** et câbler automatiquement *source → UDC → destination* (sortie = format de la destination) ;
- **Réutiliser un UDC libre** (déployé, sortie inutilisée) reconfiguré au format cible ;
- **Câbler quand même** (force le câble direct) ou **Annuler**.

Le format produit (`produces[].format`) et le format attendu (`consumes[].format`) sont comparés côté client. Les types qui **adaptent eux-mêmes** leur entrée (UDC, multiview, délai, encodeur, sender) ne déclenchent jamais la proposition (drapeau manifeste `adapts_input`). Backend : `POST /api/home/insert_udc` (création/réutilisation + câblage des deux côtés).

---

## Traitements : nouveau type UDC (convertisseur up/down/cross) — 2026-05-30

Nouveau plugin **UDC** (rubrique **Traitements**) : adapte n'importe quelle entrée vidéo vers un **format de sortie cible** — **résolution** (up/down), **cadence** (cross, ex. 25→50), **chroma** (4:2:0 / 4:2:2 / 4:4:4) et **colorimétrie** (BT.709 / BT.2020 / PQ / HLG / BT.601). Une entrée, une sortie ; moteur FFmpeg interne (shm → conversion → shm).

- **Format de sortie réglable à chaud** depuis Traitements → UDC (relance du FFmpeg interne, sans redéploiement) ; choix dans la liste **Réglages → Vidéo**.
- **Format d'entrée connu au câblage** : l'orchestrateur transmet à l'UDC le format du producteur (sinon auto-détection depuis le shm).
- HDR / tone-mapping : non géré pour l'instant (la colorimétrie ne pose que les drapeaux de signalisation).

---

## Vidéo : format de flux exposé + chroma 4:2:2 par défaut — 2026-05-30

Tout le pipeline vidéo en mémoire partagée est passé en **4:2:2 par défaut** (au lieu de 4:2:0), avec **chroma configurable** (4:2:0 / 4:2:2 / 4:4:4) — orienté réception **ST 2110-20 broadcast**. Chaque flux **porte désormais son format** (résolution, cadence, chroma, profondeur, colorimétrie), visible en badge sur la page **Câbles** et exploité pour l'insertion auto d'UDC.

- **Réglages → Vidéo** : la liste des formats prédéfinis devient un **tableau éditable** (ligne = un format, avec selects scan/chroma/bits/colorimétrie), avec **ajout / suppression / réordonnancement** des lignes. Schéma de ligne étendu : `Nom ; Largeur ; Hauteur ; FPS ; Scan ; Chroma ; BitDepth ; Colorimétrie` (anciennes lignes à 5 champs toujours valides). Format **SD-SDI PAL** ajouté.
- **Page Streams** : réglages **chroma** et **colorimétrie** par flux (en plus de codec/débit/preset/GOP).
- Migration automatique des formats déjà enregistrés (ajout des colonnes manquantes, défauts 4:2:2 / 10 bits / BT.709).

---

## Plugins : stockage générique (presets / mémoires) — 2026-05-30

Les presets du correcteur et les mémoires du DVE utilisaient des tables + routes dédiées codées en dur. Elles passent sur un **stockage générique par plugin** (`plugin_store`) : n'importe quel plugin peut désormais persister des entrées nommées (globales ou par container) via `GET/POST/PATCH/DELETE /api/plugins/<type>/store`, **sans toucher au cœur**. Données existantes migrées automatiquement (presets + mémoires conservés ; anciennes tables gardées en filet). Pour l'utilisateur : aucun changement, presets et mémoires fonctionnent comme avant.

---

## Streams : lien client public pour un flux WebRTC — 2026-05-30

Depuis la carte d'un encodeur (page **Streams**), on peut générer un **lien à envoyer à un client** ouvrant une **page publique brandée** qui lit le flux WebRTC — sans compte ni accès à l'orchestrateur.

- **Création / suppression** depuis la carte (section « Lien client », visible si une destination WebRTC est diffusée) : on saisit un **nom de flux** et un **message personnalisé**, le lien est créé et copié. Les liens existants se listent avec **Copier** / **Supprimer**.
- **URL à identifiant aléatoire** (`/w/<token>`, jeton 128 bits **non devinable / non énumérable**) et **révocable** : supprimer le lien renvoie aussitôt une 404.
- **Page publique brandée** : logo + nom de l'entreprise (Réglages → Personnalisation), nom du flux, message, et un **lecteur WHEP maison** s'appuyant sur le **lecteur natif du navigateur** (volume, plein écran, lecture/pause). La fenêtre vidéo occupe le plus grand 16:9 tenant à l'écran. Repli « flux indisponible » si la passerelle n'est pas joignable.

Le jeton protège la *page* (le flux MediaMTX reste diffusé sur la passerelle). Pré-requis : passerelle WebRTC déployée + activée, et **joignable par le client** (`gateway_ip:http_port`) — pour un client externe, cette adresse doit être routable.

## Correctif : câblage d'un plugin à entrée unique (correcteur) — 2026-05-29

Câbler une source dans un correcteur de couleur depuis la page Câbles renvoyait « n'a pas d'entrée video #0 ». Cause : le port unique était numéroté 0 par l'UI alors que l'entrée du plugin n'est pas « slottée ». Le câblage accepte désormais une entrée unique non slottée pour le slot demandé (correctif générique, vaut pour tout plugin mono-entrée).

Harmonisation : **les numéros de slot affichés commencent à 1** (le message d'erreur montrait « #0 » → désormais « #1 », comme les libellés PiP/Input/Box/Audio déjà en 1-based). La numérotation interne reste 0-based (clés, API).

## Plugins : UI embarquée — Mélangeur + Multiview (bespoke supprimés) — 2026-05-30

Les 4 types Traitements ont maintenant leur UI entièrement embarquée dans les plugins. Pages bespoke supprimées (mixer.js, multiview.js, traitements_melangeurs.html, traitements_multiview.html). multiview.js reste dans static/ (chargé dynamiquement par le plugin).

## Plugins : UI embarquée — Mélangeur + Multiview — 2026-05-30

Les deux derniers types Traitements rejoignent le modèle d'UI embarquée : **Mélangeur** (T-bar, buses PGM/PVW/Keyer, DSK, ATEM) et **Multiview** (composer 3-panneaux, drag-canvas, layouts). Le shell Traitements générique est maintenant utilisé pour les 4 types. Bespoke Mélangeur/Multiview conservés inertes jusqu'à confirmation navigateur.

## Plugins : UI embarquée — DVE / SuperSource — 2026-05-29

Le **DVE** rejoint le Correcteur : son éditeur (canvas drag/resize des 4 PiP, sources, crop, bordure, copier/coller, mémoires recall/save/export/import/envoi) vit désormais dans `plugins/dve/control.{html,js,css}` et se monte dans le shell Traitements générique. Endpoints inchangés (`/plugin/{state,box,input}` + `/dve/*` mémoires/persist). Le Correcteur est confirmé OK ; ses fichiers bespoke ont été supprimés. Reste : Mélangeur, Multiview.

## Plugins : UI de contrôle embarquée — pilote Correcteur — 2026-05-29

Un plugin peut désormais **embarquer son interface de contrôle riche** (pas seulement les plugins Médias) : un `.mxlplugin` importé amène aussi son UI. Pilote sur le **Correcteur de couleur**.

- L'éditeur du correcteur (knobs, mode avancé, color balance, presets, câblage) vit maintenant dans `plugins/color_corrector/control.{html,js,css}` et se monte dans un **shell Traitements générique** (réutilisable pour DVE/Mélangeur/Multiview ensuite).
- Aucun changement fonctionnel ni d'endpoints : mêmes réglages, mêmes presets globaux. La page **Traitements → Correcteur** est identique à l'usage.

Backend : shell `templates/traitements_plugin.html` + route inchangée d'URL. dve/mixer/multiview restent en pages bespoke (migration à suivre, même recette).

## Plugins : désactiver / supprimer — 2026-05-29

Le cycle de vie des plugins est complet depuis Réglages → Plugins (vue dépliée d'un plugin) :

- **Désactiver** : le plugin reste installé et les containers qui l'utilisent continuent de tourner, mais il **disparaît de la palette** (plus déployable sur de nouveaux containers). Réversible (« Réactiver »). Un badge « désactivé » l'indique dans le tableau.
- **Supprimer** : retire le plugin du disque. **Garde-fou** : refusé si des containers l'utilisent encore (la liste est affichée) — il faut d'abord les redéployer sur un autre type ou les supprimer.

Backend : routes `POST /api/plugins/<type>/disable` et `DELETE /api/plugins/<type>` ; état désactivé stocké en réglage `plugins_disabled` (hors fichiers du plugin). Frontend : `templates/settings.html`.

## Plugins : catégories Sources / Destinations / Streams corrigées — 2026-05-29

`receiver_2110` (Sources), `sender_2110` (Destinations) et `ServeurStream` (Streams) ont enfin leur **catégorie** au lieu de « — ». Au passage, les chips Receiver/Sender de la palette ne sont plus codés en dur : ils viennent du **registre de plugins** comme tous les autres (le chip disparaît automatiquement si le plugin est retiré/désactivé). `ServeurStream` reste hors palette (déployé via Réglages → WebRTC), avec une catégorie d'affichage « Streams ».

## Onglet Plugins : filtres + détail enrichi — 2026-05-29

- **Filtres** (comme la page Containers) au-dessus du tableau : par **Catégorie** et par **état de version** (Tous / À jour / Obsolète).
- Dans le **détail déplié**, les versions sont séparées en colonnes claires : **Version déployée** (sur le container) · **Version actuelle** (du plugin) · **Déployer** (choix de la version) · **Dernier déploiement** · Projets.
- Nouvelle donnée : la **date du dernier déploiement** de chaque container (renseignée à chaque déploiement ; « — » pour les containers pas encore redéployés).

Backend : colonne `deployed_at` (horodatée par `db_update_deploy_config`), exposée par `GET /api/plugins`. Frontend : `templates/settings.html`.

## Plugins : import / export `.mxlplugin` + activer une version — 2026-05-29

On peut désormais **installer, mettre à jour et exporter** un plugin depuis Réglages → Plugins, sans SSH ni redémarrage.

- **Exporter** : bouton dans la vue dépliée d'un plugin → télécharge un `.mxlplugin` (zip du plugin, toutes versions incluses).
- **Importer** : bouton « Importer un plugin » → upload d'un `.mxlplugin`. Import **piloté par la version** :
  - type inconnu → installé (devient la version active) ;
  - version plus récente → importée, et on propose de **la passer active** ;
  - version plus ancienne → importée (archivée), l'active ne change pas ;
  - version déjà présente → on demande **annuler / remplacer**.
- **Activer une version** : dans la vue dépliée, un bouton « activer » par version archivée la rend courante (l'ancienne courante est archivée).
- **Sécurité** : réservé admin ; protection anti zip-slip ; validation du manifeste + contrôle des accolades du script ; aucun code de plugin n'est exécuté côté orchestrateur (il ne tourne que dans le container).

Backend : `app/plugins.py` (validate/install/activate/export), routes `GET /api/plugins/<type>/export`, `POST /api/plugins/import`, `POST /api/plugins/<type>/activate`. Frontend : `templates/settings.html`.

## Onglet Plugins : tableau triable + lignes dépliables — 2026-05-29

Le tableau de Réglages → Plugins est plus lisible et actionnable.

- La colonne « Rubrique » devient **« Catégorie »**, et les entêtes (Plugin, Catégorie, Containers, Drift) sont **cliquables pour trier** (tri par catégorie par défaut).
- **Déplier** un plugin (clic sur sa ligne) liste **tous ses containers** : VMID, hostname, version déployée (marquée si en drift) et les **projets** dans lesquels ils figurent.
- Depuis cette vue dépliée, on peut **choisir une version pour un container précis et le redéployer** directement (avec suivi dans le journal). Idéal pour rejouer une version sur une seule instance sans toucher aux autres.

Backend : `GET /api/plugins` renvoie désormais le détail des instances (+ projets), nouvelle route `POST /api/containers/<vmid>/redeploy-version`. Frontend : `templates/settings.html`.

## Plugins multi-versions : choisir la version au déploiement — 2026-05-29

Un plugin peut désormais avoir **plusieurs versions installées côte à côte**, et on peut **rappeler une version précise** d'un container depuis la palette (page Surveillance).

- **Versions archivées** : à côté de la version courante (`plugins/<type>/`), on range les anciennes sous `plugins/<type>/versions/<version>/script.py`.
- **Palette** : si un type a plusieurs versions, un sélecteur **« Version du plugin »** apparaît (défaut = courante). La version déployée s'affiche aussi en pastille sur la carte du container.
- **Drift inchangé** : « Redéployer »/« Tout redéployer » visent toujours la **version courante** (mettre à jour). Choisir une ancienne version est donc volontairement « en drift ».
- **Exemple fourni — DVE v0.2.0** : le DVE passe en v0.2.0 (marqueur `variant: v2` exposé sur `:8080`) et garde la v0.1.0 archivée → de quoi tester le rappel de version (déployer un DVE en 0.1.0 puis 0.2.0 et voir la différence au monitoring).

Backend : `app/plugins.py` (`versions()`, rendu par version), propagation `version` dans `scripts.py`/`deploy.py`/`routes.py`/`main.py`. Frontend : sélecteur dans `deploy_palette.html` + `static/scripts.js`.

## Réglages → onglet « Plugins » (gestion) — 2026-05-29

Nouvel onglet **Réglages → Plugins** pour gérer les plugins de types de containers (phase 1 : visibilité + cycle de vie).

- **Liste** de tous les plugins : libellé, id, version, rubrique, nombre de containers déployés.
- **Drift** : repère les containers tournant une version antérieure au manifeste, avec un bouton **Redéployer** pour les mettre à jour (par type, seulement ceux en retard).
- **Recharger** : re-scanne le dossier `plugins/` **à chaud, sans redémarrer le service** — pratique après avoir modifié ou ajouté un plugin.
- **Journal en direct** : un redéploiement affiche dessous un journal qui suit l'avancement container par container (« en cours… », « déployé et redémarré », erreurs), avec mise à jour automatique du drift à la fin.
- **« Tout redéployer »** : un bouton global (visible seulement s'il y a du drift, avec le total) redéploie d'un coup tous les containers en retard, tous types confondus.
- **Plugins en erreur** : les plugins présents mais non chargés (manifeste invalide, accolade non doublée…) sont listés avec la raison, au lieu de devoir fouiller les logs.

Backend : `app/plugins.py` (capture des erreurs de scan), 3 routes dans `app/routes.py` (`GET /api/plugins`, `POST /api/plugins/reload`, `POST /api/plugins/<type>/redeploy`). Frontend : onglet + JS dans `templates/settings.html`.

## 🎉 Architecture : 100 % plugin — campagne terminée — 2026-05-29

**Tous les types de containers sont désormais des plugins** (`plugins/<type>/`). La série de migrations ci-dessous a transformé chaque type cœur, jusqu'au dernier, en plugin autonome et versionné — sans rien casser pour l'utilisateur.

- **9 types migrés** : `receiver_2110`, `sender_2110`, `streamer`, `mixer`, `dve`, `color_corrector`, `multiview_free`, les 4 médias (`player`/`recorder`/`storage`/`stills`), et la passerelle `ServeurStream`.
- **Bénéfices** : version par container (mise à jour type par type, détection d'écarts), UI embarquée avec le plugin, ajout d'un nouveau type sans toucher au cœur, et catégories de palette claires.
- **Pour l'utilisateur, rien ne change** : mêmes pages, mêmes réglages, mêmes câblages. Les containers existants sont **migrés automatiquement au redémarrage** ; un redéploiement les fait passer au script du plugin. Voir la nouvelle section **Aide → Système de plugins**.

## Plugins : Passerelle WebRTC migrée (→ ServeurStream) — 100% plugin — 2026-05-29

La **passerelle WebRTC** (MediaMTX) rejoint le système de plugins et est renommée **`ServeurStream`**. C'était le dernier script encore rendu en dur : **plus aucun `script_templates/*.py`** n'est rendu (il ne reste que l'agent, qui vit dans le template LXC).

- **Renommage transparent** : les passerelles existantes sont migrées automatiquement au redémarrage. Le déploiement reste **Réglages → onglet WebRTC** (inchangé pour l'utilisateur) ; le module `app/webrtc_gateway.py` et les réglages `webrtc_*` gardent leur nom.
- Sous le capot : `plugins/ServeurStream/`, migration DB + renommage dans `app/{scripts,deploy,routes,webrtc_gateway}.py`. L'ancien `script_templates/webrtc_gateway.py` est supprimé.

## Plugins : Receiver migré (→ receiver_2110) — campagne terminée — 2026-05-29

Le **receiver ST 2110** rejoint le système de plugins et est renommé **`receiver_2110`**. C'était le **dernier type cœur** : **tous les types de containers sont désormais des plugins**.

- **Renommage transparent** : les receivers existants sont **migrés automatiquement** au redémarrage (aucune action requise). Le nouveau nom `receiver_2110` apparaît partout.
- **NMOS inchangé** : la découverte IS-04/05 et le routage (le contrôleur active une réception → l'orchestrateur configure le container) fonctionnent comme avant ; les identifiants NMOS restent stables (basés sur le VMID). Vérifié : les receivers restent exposés après migration.
- **Configuration inchangée** : la palette de la page Containers reste l'éditeur (N receivers vidéo + M audio, simulation par slot testsrc/sine).
- Sous le capot, seul l'empaquetage passe au format plugin (script versionné dans `plugins/receiver_2110/`) ; la normalisation des slots de simulation est conservée à l'identique.

Backend : `plugins/receiver_2110/`, migration DB + `normalize_receiver_params` (`app/scripts.py`), renommage `app/{deploy,nmos,routes,projects,monitor,emberplus}.py`. Frontend : `static/scripts.js`, `templates/{deploy_palette,cards,forms,home,cables,projects,aide}.html`. Les anciens `script_templates/{receiver_nmos,receiver}.py` sont supprimés. **Les receivers existants sont migrés au redémarrage** ; un redéploiement les fait passer au script plugin.

## Plugins : Sender 2110 migré (→ sender_2110) — 2026-05-29

Le **sender ST 2110** (vidéo 2110-20 + audio 2110-30) rejoint le système de plugins et est renommé **`sender_2110`**. C'est le premier type **lié NMOS** migré.

- **Renommage transparent** : les senders existants sont **migrés automatiquement** au redémarrage (aucune action requise). Le nouveau nom `sender_2110` apparaît partout (cartes, filtres, palette, projets).
- **NMOS inchangé** : la découverte IS-04/05 (sources/flows/senders, SDP, transportfile, PTP) fonctionne comme avant ; les identifiants NMOS restent stables (basés sur le VMID, pas sur le type).
- **Configuration inchangée** : la palette de la page Containers reste l'éditeur (vidéo 2110-20 + jusqu'à 2 audios 2110-30, multicast/port auto). Le câblage à chaud (`:8082`) et les métriques sont identiques.
- Sous le capot, seul l'empaquetage passe au format plugin (script versionné dans `plugins/sender_2110/`).

Backend : `plugins/sender_2110/`, migration DB dans `app/database.py`, renommage `app/{deploy,nmos,routes,emberplus,metrics,projects,scripts}.py`. Frontend : `static/scripts.js`, `templates/{deploy_palette,cards,forms,home,cables,projects,aide}.html`. L'ancien `script_templates/worker_2110_sender.py` est supprimé. **Les senders existants sont migrés au redémarrage** ; un redéploiement les fait passer au script plugin.

## Plugins : Streamer migré (ex-Encoder) — 2026-05-29

L'**encodeur de streaming** rejoint le système de plugins et change de nom : **« Encoder » devient « Streamer »** (type `streamer`). C'est le dernier gros type non lié au ST 2110 à migrer.

- **Renommage transparent** : les encodeurs existants sont **migrés automatiquement** au redémarrage (aucune action requise), encodeurs de la fenêtre **Monitoring** inclus. Le nouveau libellé « Streamer » apparaît partout (cartes, filtres, palette, projets).
- **Page Streams inchangée** : l'éditeur riche (encodage, destinations UDP/SRT/WebRTC, pistes audio), le **remappage audio à chaud** et l'affichage du **débit réel** fonctionnent comme avant.
- **Câblage inchangé** : entrée vidéo (bascule sans coupure si même résolution, sinon redéploiement) + entrée audio (activée automatiquement au câblage).
- Sous le capot, l'encodage et le push vers la passerelle WebRTC sont identiques ; seule l'enveloppe passe au format plugin (script versionné, rangé dans la catégorie **Streams** de la palette).

Backend : `plugins/streamer/`, migration DB dans `app/database.py`, renommage `app/{deploy,routes,monitor,emberplus,projects}.py`. Frontend : `static/scripts.js`, `templates/{deploy_palette,cards,forms,home,cables,projects,streams,aide}.html`. L'ancien `script_templates/worker_udp.py` est supprimé. **Les Streamers existants sont migrés au redémarrage** ; un redéploiement les fait passer au script plugin.

## Plugins : Mélangeur + Multiviewer migrés (toute la rubrique Traitements) — 2026-05-29

Suite de la migration des types cœur vers le **système de plugins** : le **Mélangeur** et le **Multiviewer** rejoignent le Correcteur et le DVE → **toute la rubrique Traitements** est désormais en plugins.

- **Mélangeur** : N entrées (réglable à la création), 3 sorties PGM/CLEAN/PVW, transitions. Pilotage par la page Traitements **et le pupitre ATEM** inchangés (le pupitre parle directement au container).
- **Multiviewer** : N fenêtres disposées librement (position/taille/label/tally) → 1 sortie composite. **Composer, tally et layouts conservés** à l'identique.
- Sous le capot, le modèle de plugins gagne deux mécanismes qui profitent à tous les futurs types : un **nombre d'entrées variable** (réglé à la création) et des **entrées en liste avec géométrie** (multiviewer), plus des **champs de configuration déclaratifs** à la création.

Aucune perte de fonctionnalité ; UI Traitements/ATEM/composer/tally/layouts inchangées. **Les Mélangeurs et Multiviewers existants doivent être redéployés** pour passer au plugin. Backend : `app/plugins.py` (wiring `repeat`/`from_list` + substitution de params), `app/routes.py`, nouveaux `plugins/{mixer,multiview_free}/`.

## Plugins : config déclarative, câblage multi-entrées, DVE migré — 2026-05-29

Poursuite de la migration des types cœur vers le **système de plugins** (après le Correcteur). Deux extensions du modèle + un type migré.

- **Câblage multi-entrées** : un plugin peut déclarer **plusieurs entrées indexées** (slots) dans son wiring. Le câblage par slot, la topologie et les sauvegardes de câbles le gèrent de bout en bout. Côté page **Câbles**, tout nœud à plusieurs entrées exige désormais de cliquer sur un **slot précis** (règle générique, plus de liste codée par type).
- **Champs de configuration déclaratifs** : un plugin peut décrire ses champs de réglage de création (texte, nombre, case, liste, champs conditionnels) ; la palette les **rend automatiquement** et le backend les valide/borne. Permettra aux futurs types migrés de garder un formulaire de création sans code dédié.
- **DVE / SuperSource migré en plugin** : 4 PiP + fond (5 entrées Box 1-4 + Fond), sortie composite, géométrie/mémoires pilotées à chaud. Son UI riche (page **Traitements → DVE**, mémoires) est **conservée** ; l'intégration **ATEM SuperSource** est inchangée. Bonus : les entrées câblées sont désormais **persistées** (rétablies au redéploiement).

Backend : `app/plugins.py` (coerce_config + slots), `app/routes.py` (wiring/topologie multi-slot), nouveaux `plugins/dve/`. Frontend : `static/scripts.js` (moteur de champs), `templates/{deploy_palette,cables}.html`, `static/dve.js`. **Les containers DVE existants doivent être redéployés** pour passer au plugin.

## Streams : format de sortie adaptatif (scaling) + mode source — 2026-05-29

Sur la page **Streams**, le **Format** d'un encodeur (`worker_udp`) est désormais le **format de sortie souhaité**, **toujours appliqué**. L'**entrée** (signal reçu) est **auto-détectée** depuis le shm et **adaptée** si elle diffère : **redimensionnement** (`scale`) et **rééchantillonnage de cadence** (`fps`). Fini les images corrompues quand la résolution configurée ne correspondait pas à la source.

- La carte affiche le **signal reçu** et l'adaptation : « Signal reçu `1280×720` → redimensionné en `640×360` ». Option **« Suivre l'entrée »** pour encoder en natif sans mise à l'échelle.
- Nouveau réglage **« Mode source »** par encodeur :
  - **Adaptation auto** (défaut) — détecte et adapte l'entrée ; recâbler la source = bref redéploiement.
  - **Bascule sans coupure** (hot) — format figé (= résolution de la source, sans mise à l'échelle) ; changement de source de même résolution sans couper le flux.
- L'encodeur publie sur `:8080` les dimensions d'entrée détectées (`in_width`/`in_height`) et la cible de sortie (`out_*`). En mode auto, `:8082/input` renvoie 409 → la page Câbles redéploie proprement. Le **monitoring** reste en bascule sans coupure.

Limites : cadence d'entrée supposée 25 fps (pipeline simulé) ; déduction 16:9 quand le format configuré ne correspond pas au compte de pixels du shm.

`script_templates/worker_udp.py` (`_detect_dims`/`_video_filter`/métriques), `app/scripts.py` (`normalize_worker_udp_params`), `app/deploy.py` (défaut `hot_input=False`), `app/routes.py` (`/api/streams`), `templates/streams.html` (sélecteurs format & mode, ligne « Signal reçu »).

## Containers : champ de recherche — 2026-05-29

La barre de filtres de **Containers / Surveillance** gagne un **champ de recherche** texte (« nom, #VMID, IP, source… »). Filtrage instantané, insensible à la casse, par sous-chaîne sur **hostname, VMID, IP, source, SHM et type**.

- Se combine avec les chips Type / Statut / Projet existants ; le compteur affiche `x / total`.
- Ne persiste pas (réinitialisé à chaque visite, contrairement aux chips de filtre).

Frontend uniquement (`templates/cards.html`, `static/scripts.js` : `setSearch`/`matchesFilter`, `static/css/base.css`).

## Nouveau type de container : Images fixes (stills) — 2026-05-29

**Plugin « Images fixes »** (rubrique Médias) qui affiche une image fixe et la produit dans le pipeline MXL avec **deux sorties vidéo** : **Fill** (l'image) et **Key** (l'alpha en niveaux de gris — blanc opaque, noir transparent ; blanc plein si l'image n'a pas de transparence).

- Sur sa page : on choisit un **dossier** du stockage partagé, on voit les **vignettes** de toutes les images, on en sélectionne une.
- **Cadrage** au format vidéo configuré, 3 modes : **pixel à pixel** (taille native, centré — défaut), **au plus grand** sans déformer (letterbox/pillarbox transparent dans le key), **plein cadre** (étiré).
- Options **Position & taille** (X / Y / Size en **% du cadre**, centré) ; le redimensionnement s'applique aussi au key.
- Bouton **Monitoring** (prévisualise le Fill). Le Fill et le Key sont câblables indépendamment (page Câbles) — ex. Key vers l'entrée key d'un mélangeur.

Nouveau plugin `plugins/stills/` (décodage PIL, conversion BT.601 → YUV420p). Le proxy de contrôle des plugins transmet désormais les query params et le binaire (vignettes `image/jpeg`). Le type apparaît automatiquement à la création de container.

## Plugins de containers (PoC) + rubrique Medias : Player, Recorder, Stockage — 2026-05-29

Introduction d'un **système de plugins** pour les types de containers : chaque type devient un dossier autonome et **versionné** dans `plugins/<type>/` (manifeste + script déployé + UI), chargé par un registre (`app/plugins.py`). Coexiste avec les 8 types historiques **sans les migrer** (un type non hardcodé est rendu/déployé via le registre en repli). Objectifs : déployer/mettre à jour un type sans toucher au reste du système, connaître la version par container, embarquer l'UI avec le plugin.

Trois premiers plugins, réunis dans une nouvelle rubrique de navigation **Medias** (après Streams) :

- **Player** — lit un fichier (ou URL) et le produit dans le pipeline MXL (shm vidéo YUV420p + audio s24le 8ch). Play/pause/seek/boucle + **navigateur de fichiers** intégré (liste le volume partagé, `:8082/files`).
- **Recorder** — enregistre un flux du pipeline (vidéo + audio câblés) vers un fichier (mp4).
- **Stockage** — volume média partagé : gestionnaire de fichiers web **filebrowser** (authentifié, **mot de passe aléatoire persistant** affiché dans l'UI + bouton « ouvrir dans un onglet »).

Points clés :

- **Volume média partagé** par **bind-mount hôte** (`/srv/mxl-media` → `/mnt/media` dans chaque container média, même mécanisme que `/dev/shm`) — pas de NFS. Ajouté à la création quand le manifeste déclare `media_volume`.
- **Création depuis la page Containers uniquement** (les types plugin y sont proposés au formulaire de création, avec leurs `deploy_defaults`) ; **Medias = exploitation** (lister + piloter les instances via un proxy `:8082` générique, sans bouton créer).
- **Câbles** : les entrées/sorties des plugins sont déclarées au manifeste (`wiring`) → exposées automatiquement sur la page Câbles (produces/consumes), câblables à chaud, et capturées par les snapshots (save/restore/clear). Les containers **sans I/O** (Stockage, webrtc-gateway) n'apparaissent plus sur Câbles.
- **Versioning** : version du plugin persistée dans `deploy_config` + exposée sur `:8080`, badge sur la carte du container.
- **Offline** : mediamtx et filebrowser sont désormais **pré-installés dans le template** (recréation du template) → plus aucun téléchargement internet au 1er lancement des plugins.

Pour ajouter un futur plugin : déposer `plugins/<type>/` (manifeste `wiring`/`control`/`ui` + `script.py` + fragments UI) → il apparaît dans la nav, la création, Câbles et les snapshots **sans toucher au code**.

Fichiers : `app/plugins.py` (registre, nouveau), `plugins/{player,recorder,storage}/`, `app/{scripts,deploy,routes,containers,template_recreate}.py`, `main.py`, `templates/{layout,medias,cards,forms}.html`. **PoC** : à valider en exploitation réelle ; le serveur de stockage repose sur le bind-mount hôte (pas de NFS).

## Streams : remap des canaux audio à chaud (sans coupure) — 2026-05-29

**Changer les canaux source d'une piste audio d'un encodeur ne coupe plus le flux.** Avant, toute modification audio relançait ffmpeg → coupure brève de **toutes** les destinations (UDP/SRT/WebRTC).

- Le routage des canaux est déplacé de ffmpeg vers le **feeder Python** : ffmpeg lit des « slots » de sortie **fixes** (filtres `pan` figés) et le feeder ré-aiguille les 8 canaux source du shm vers ces slots selon un mapping mutable (gather numpy).
- Endpoint `POST :8082/audiomap {tracks}` : applique le nouveau mapping à chaud si la **forme est identique** (même nombre de pistes, même mono/stéréo). `app/routes.py` diffe l'édition reçue et choisit **hot vs redéploiement** : seul un changement d'indices de canaux à forme constante est à chaud ; ajout/suppression de piste, codec, destinations, résolution → redéploiement classique.
- Pourquoi pas zmq/sendcmd : le filtre `pan` n'est pas command-able et `zmq` exigerait de recompiler ffmpeg ; le remap côté feeder évite cette dépendance.

Fichiers : `script_templates/worker_udp.py` (feeder gather + `:8082/audiomap`), `app/routes.py`. **Les encodeurs existants sont migrés au prochain redéploiement.**

## Correctif : fps affiché (fenêtre glissante) — 2026-05-29

**Le fps affiché sous-estimait la cadence réelle** (ex. un player à 25 fps affichait 23, un encodeur « montait » de 13 vers 22 au démarrage). En cause : le fps était une **moyenne cumulée depuis le lancement du process** (`frames / temps_écoulé`), durablement tirée vers le bas par la latence de démarrage de ffmpeg et les éventuelles pauses.

- Le fps est désormais calculé sur une **fenêtre glissante (~1 s)** → il reflète la cadence instantanée réelle et se stabilise immédiatement.
- Corrigé dans `plugins/player/script.py` (player) et `script_templates/worker_udp.py` (encodeur, modes normal **et** hot-input). Le player remet aussi le fps à 0 en pause/EOF.

La lecture était déjà correcte (vérifié via `frame_index` : ~25 fps réels) ; seul l'affichage était trompeur. **Les containers existants doivent être redéployés** pour appliquer.

## Streams : preview repliable, format en liste, débit ffmpeg — 2026-05-29

Trois améliorations sur la page **Streams** :

- **Preview WebRTC repliable** : un toggle 👁 dans l'en-tête de chaque carte affiche/masque la prévisualisation. C'est une préférence d'affichage **purement client** (`localStorage`, persistée par encodeur) : masquer retire l'`<iframe>` côté navigateur **sans toucher au flux** (la destination WebRTC reste active).
- **Format vidéo en liste déroulante** : les champs Largeur/Hauteur/FPS sont remplacés par un sélecteur alimenté par les formats configurés dans **Réglages → Vidéo**. Si la config courante n'est dans aucun format, une option « Personnalisé (W×H @fps) » la préserve à l'identique.
- **Débit de sortie réel** : chaque carte affiche le **débit émis réel** total (rafraîchi toutes les 5 s) à côté des **débits configurés** vidéo/audio. Mesuré côté **réseau** (octets TX du container, `/proc/net/dev`, hors `lo`) : couvre UDP/SRT/WebRTC indépendamment du muxer et inclut l'overhead réseau (le muxer `tee` empêchant `ffmpeg -progress` de rapporter `total_size`).

Backend : `script_templates/worker_udp.py` (nouveau champ metrics `out_bitrate_kbps` via un thread de mesure réseau). Frontend : `templates/streams.html`. Aucun changement d'API (`/api/streams` recopie déjà les metrics). **Les encodeurs existants doivent être redéployés** (« Enregistrer & déployer ») pour exposer le débit.

## Monitoring : sélecteur de sources persistant — 2026-05-29

**Le choix de source d'un node multi-sorties (mélangeur PGM/CLEAN/PVW, receiver multi-flux) reste affiché.** Avant, le sélecteur disparaissait dès qu'on cliquait une source (il était rendu dans la zone message, écrasée ensuite).

- Le sélecteur est désormais une zone **persistante** (`#mon-sources`) : on bascule entre les sorties d'un clic sans recliquer « Monitoring ». La source active est **surlignée**.
- Si on monitore une source hors de cette liste (bouton d'une autre page), le sélecteur obsolète se masque. Persistance en mémoire le temps de la page (recharger réinitialise).

Frontend uniquement (`templates/layout.html`, `MXLMonitor` : `renderSources`/`highlightSource`).

## Nommage : champs utilisateur, monitor nommé, préfixe projet — 2026-05-29

**Trois améliorations de nommage.**

- **Utilisateurs Nom / Prénom / Mail** : nouveaux champs `prenom`/`nom`/`email` (migration `ALTER TABLE`), éditables dans Réglages → Utilisateurs (création + édition inline). La table des utilisateurs gagne une colonne **Monitoring**.
- **Container de monitoring nommé d'après l'utilisateur** : hostname `Monitoring-<Initiale prénom><Nom>` (ex. `Monitoring-ABernard`, sanitisé sans espace ; repli sur le username puis l'uid). Le path WebRTC reste technique/stable (`monitor-u<id>`) et le lien user↔container passe par une colonne `monitor_user_id` (robuste au renommage, compatible avec les monitors existants). Nouvelle suppression du monitor depuis la page Utilisateurs (`POST /api/monitor/destroy`, bouton dans la colonne Monitoring).
- **Restauration de projet préfixée** : les containers recréés sont préfixés par le nom du projet (`MonProjet-<hostname>`) **et les références shm internes sont réécrites** (sorties, `flux_config`, `shm_name`, `video/audios`, `input_shm`) → le câblage interne du projet reste fonctionnel ; les sources externes au projet sont laissées intactes. La détection de conflits (hostname/shm) tourne sur les nouveaux noms.

Fichiers : `app/database.py` (colonnes + helpers), `app/monitor.py` (nommage + `monitor_user_id` + `destroy`), `app/projects.py` (`_prefix_snapshot`), `app/routes.py` (users + `/api/monitor/destroy`), `templates/settings.html`. Les monitors existants gardent leur ancien hostname jusqu'à recréation.

## Création : vérification des noms + historique cumulatif — 2026-05-29

**Deux corrections du suivi de création (Containers → Création).**

- **Vérification des noms en double** : après l'appui sur « Créer », l'app interroge `/api/containers` et bloque si un hostname généré existe déjà — avertissement inline et focus sur le champ, aucune création lancée. Avant, le suivi retrouvait le container **existant** du même nom et l'affichait « prêt », masquant le doublon.
- **Historique cumulatif** : lancer une nouvelle création n'efface plus le suivi des précédentes — les nouveaux containers s'ajoutent en tête (dédupliqué, plafonné à 50). Le type demandé est mémorisé par hostname (badge « à venir » correct même en mélangeant des lots de types différents).

Frontend uniquement (`static/scripts.js`, `templates/forms.html`).

## Création : suivi persistant + bouton « Configurer » — 2026-05-29

**La liste de suivi des créations (Containers → Création) reste affichée après navigation et offre un accès direct à la configuration.** Avant, elle disparaissait dès qu'on changeait de page (état en mémoire seulement).

- **Persistance** : le suivi est sauvegardé en `localStorage` et restauré au chargement de la page → la liste des dernières créations reste visible (petit historique). Le polling reprend puis s'arrête une fois tout stabilisé. Le bouton « Réinitialiser » vide aussi l'historique.
- **Bouton « Configurer »** : dès qu'un container du lot est prêt (`running`/`script_stopped`), un bouton sur sa ligne bascule vers l'onglet Surveillance et ouvre directement sa palette de configuration (`configurerDepuisBatch` → `modifier`), même si la carte n'est pas encore dans la grille.

Frontend uniquement (`static/scripts.js`, `templates/forms.html`) — aucun changement backend.

## Hot-input généralisé : multiview, encodeur, sender 2110 — 2026-05-29

**Le changement de source sans coupure (hot-input), jusqu'ici réservé au mélangeur / DVE / correcteur (et au moniteur), est étendu au multiview, à l'encodeur `worker_udp` et au sender ST 2110.** Recâbler une entrée depuis la page Câbles ne redéploie plus le container quand la nouvelle source a la **même résolution/format** : même process, même sortie, on pointe juste un autre `/dev/shm`.

- **Politique** : hot quand la résolution correspond (lue en base via `monitor._shm_dims`), **redéploiement** sinon — l'entrée ffmpeg, le SDP du sender 2110 et la taille des fenêtres multiview sont figés à un format. L'audio du sender (format L24/48k/8ch constant) est toujours hot. Au hot-swap, le `deploy_config` est mis à jour **sans redéployer** (snapshots de câbles et redémarrage restent corrects).
- **multiview_free** (`script_templates/multiview_free.py`) : ajout d'un serveur de contrôle `:8082` (`POST /input {idx,shm}`, `GET /state`) + réouverture mmap par fenêtre (`ensure_input`). Purement additif (la géométrie reste figée, seule la source bouge).
- **worker_2110_sender** (`script_templates/worker_2110_sender.py`) : mode `HOT_INPUT` opt-in — `:8082` (`POST /input {kind,idx,shm}`) + réouverture du shm vidéo **et** audio, **sans toucher la boucle RTP ni le SDP**. Chemin non-hot strictement inchangé.
- **worker_udp** : le mode hot (déjà présent pour le moniteur) est **activé par défaut** pour Streams.
- **routes** (`app/routes.py`) : `_apply_wire`/`_apply_unwire` routent vers un POST `:8082/input` (hot) quand la résolution correspond, sinon redeploy ; helpers `_hot_input`/`_try_hot_input`/`_try_unwire_hot` ; `app/scripts.py` + `app/deploy.py` injectent le flag `hot_input` (défaut activé).

Vérifié en loopback : multiview (re-source d'une fenêtre → sortie change, même PID), sender (swap vidéo même format → même process ffmpeg conservé), et non-régression du chemin non-hot. Un container déployé avant cette fonction bascule en redeploy la première fois, puis devient hot ; redéployer une fois chaque multiview / encodeur / sender pour activer le mode.

## Fix : audio WebRTC déformé (feeder du worker_udp) — 2026-05-29

**Le son sortant d'un encodeur (ex. 221 abonné à l'audio simulé de 212) était incorrect.** Le format source est pourtant bon (le receiver écrit en **s24le / 48 kHz / 8ch**, chunk 1152, ring 100 — conforme à ce qu'attend le worker). La cause était l'`audio_feeder` (`script_templates/worker_udp.py`) : il échantillonnait le « chunk courant » à la cadence de l'horloge murale en intercalant du silence → audio haché / pitch faux. Réécrit en **streaming séquentiel fidèle** (écrit chaque nouveau `chunk_index` dans l'ordre, comme le sender 2110-30) ; le silence cadencé n'est utilisé **que** s'il n'y a pas de shm audio (pour ne pas bloquer la vidéo). Vérifié : Mire-1_audio_0 avance à ~1000 chunks/s (48 kHz), 221 republie le path WebRTC.

## Monitoring : changement de source sans coupure — 2026-05-29

**Basculer le moniteur entre deux signaux de même résolution ne coupe plus le flux.** Avant, chaque changement de source redéployait l'encodeur (stop+start ffmpeg → le path WebRTC tombait et le lecteur se rechargeait).

- **Mode hot-input du `worker_udp`** (`script_templates/worker_udp.py`, opt-in via `HOT_INPUT`) : un serveur de contrôle `:8082/input {shm}` re-câble à chaud la source lue **sans toucher au process ffmpeg ni au path WebRTC**. Frame noire si la source n'est pas prête → le path ne tombe jamais. Dimensions **fixes** dans ce mode. Le mode normal de Streams est inchangé.
- **Décision hot vs redeploy** (`app/monitor.py`) : la résolution de chaque signal est lue en **base** (`_shm_dims` parcourt les `deploy_config` des producteurs : receiver/mixer/dve/multiview/correcteur). Même résolution + vidéo seule → **hot-swap, zéro coupure** (POST `:8082/input`). Résolution différente, source audio, ou résolution inconnue → redéploiement (brève coupure, comme avant), inévitable car l'entrée ffmpeg est verrouillée à une résolution.
- **Front** (`templates/layout.html`) : `MXLMonitor.send` ne force plus le rechargement de l'iframe quand le changement est à chaud (`hot`) — bascule visuellement instantanée.

Vérifié en loopback (faux ffmpeg) : le **même process encodeur** survit au changement de source, fps continu. Le premier choix de source après création du moniteur verrouille la résolution ; les bascules suivantes en même résolution sont fluides.

## Audio des encodeurs : câblage, sélection des canaux, activé par défaut — 2026-05-29

**Les flux MXL sont en 8 canaux mais un stream H.264 n'en porte souvent que 2 → on choisit lesquels.** Audio désormais **activé par défaut** sur les encodeurs `worker_udp`, **câblable** via la page Câbles, avec **sélection des canaux** en sortie. Code en anglais (dérogation convention française).

- **Modèle** (`scripts.normalize_worker_udp_params` + `_normalize_audio`) : `audio = {enabled (défaut True), bitrate, tracks:[{channels:[…]}]}`. Chaque piste = 1 canal (mono) ou 2 (stéréo) parmi 8 (0-based). Défaut : 1 piste stéréo ch0-1.
- **Câblage audio** (`app/routes.py`) : `worker_udp` expose un **port d'entrée audio** (topologie) ; `_apply_wire`/`_apply_unwire`/`_collect_current_edges` gèrent `kind=audio` → `params.audio_shm` (+ active l'audio). Câbler une sortie `*_audio_*` d'un receiver vers l'encodeur depuis la page Câbles.
- **Worker** (`script_templates/worker_udp.py`) : `-filter_complex` (`pan`/`asplit`) mappe les canaux choisis ; **codec par destination** — **AAC** (toutes pistes) pour UDP/SRT, **Opus** (1ʳᵉ piste) pour WebRTC ; **encode vidéo unique**, routage `tee select=<indices>`. `audio_feeder` réécrit : ouvre le fifo immédiatement et **pousse du silence** si pas d'audio frais → ne bloque jamais la vidéo (correctif du blocage initial).
- **UI Streams** (`templates/streams.html`) : section Audio (activer, débit/piste, shm câblé en lecture seule) + éditeur de **pistes** (ajout, mono/stéréo, sélecteurs de canal 1-8). Validation serveur des canaux (0..7).
- **Monitor** (`app/monitor.py`) : inclut désormais l'audio quand la source en a — `MXLMonitor.monitorVmid` associe la 1ʳᵉ sortie audio du node (ex. `*_audio_0`), encodée en Opus (1 piste stéréo) pour le panneau WebRTC.
- Vérifié en live : monitor 240 (vidéo Mire-1_0 + audio) → ffmpeg tourne (libopus présent), path WebRTC publié, AAC sur UDP + Opus sur WebRTC.

**Limites** : WebRTC = 1 piste audio (les pistes >1 ne sortent que sur UDP/SRT) ; sans `audio_shm` câblé l'audio reste silencieux (pas encodé) ; A/V non synchro PTS.

### Câblage audio : cible acceptée + libellés + option auto-audio — 2026-05-29

- **Fix** : l'entrée audio d'un encodeur n'était pas câblable — `startWire` (`cables.html`) n'acceptait `worker_udp` que comme cible **vidéo**. Désormais accepte aussi l'**audio** (le `kind` vient de la source).
- **Lisibilité** : les ports d'entrée des encodeurs/senders sont étiquetés **Vidéo / Audio** (au lieu de « slot N (vide) »).
- **Option « Câbler aussi l'audio associé »** (case en haut de la page Câbles, persistée) : en câblant une sortie **vidéo** vers un encodeur `worker_udp`, l'**audio associé** du producteur (apparié par index, ex. `receiver_0` ↔ `receiver_audio_0`) est joint **dans le même appel** `/api/home/wire` (`audio_shm`) → un seul déploiement atomique (pas de race entre deux wires).

## Panneau de monitoring WebRTC (par utilisateur) — 2026-05-29

**Panneau latéral global embarquant un flux WebRTC : l'utilisateur visualise en direct le signal qu'il règle, et un bouton « 📺 Monitoring » sur les pages productrices envoie leur sortie dans ce panneau.** Code en anglais (dérogation à la convention française).

- **`app/monitor.py`** : **un encodeur monitor par utilisateur** (`worker_udp`, hostname `monitor-u<uid>`, path WebRTC `monitor-u<uid>` poussé vers la passerelle MediaMTX). `create_iter` (création à la demande, streamée, attente IP+agent), `set_source` (re-pointe l'encodeur sur un shm = redeploy ffmpeg ~3 s), `activate` (réveil), `start_reaper` (coupe le script `:8081/stop` après 10 min sans heartbeat ; réactivé à la réouverture).
- **Routes** `/api/monitor/{status,create,source,activate,heartbeat}` (`@require_login`, `uid = current_user()["id"]`).
- **Panneau global** (`templates/layout.html`, `window.MXLMonitor`) : `<aside>` repliable à droite + onglet déclencheur, état persisté en `localStorage`, heartbeat 30 s, log de création streamé, `<iframe>` WHEP. Le path étant constant par utilisateur, changer de source ne recharge pas l'iframe (anti-reconnexion).
- **Boutons « 📺 Monitoring »** : Mélangeur (sélecteur PGM/CLEAN/PVW), Correcteur / Multiview / DVE (sortie unique), Streams (entrée de l'encodeur), Sources/receivers (sélecteur de slot vidéo). `MXLMonitor.monitorVmid(vmid)` lit les `produces[]` de `/api/home/summary`.
- **`main.py`** : démarrage du reaper d'inactivité.

**Limites assumées** : un container monitor par utilisateur (script coupé si inactif, container conservé — suppression manuelle) ; bascule de source ~3 s ; appli multi-pages → l'iframe se reconnecte à chaque navigation ; nécessite la passerelle WebRTC déployée + activée.

### Correctifs monitoring (icône, layout, flux) — 2026-05-29

- **Icône retirée** des boutons « Monitoring » (pages + onglet du panneau).
- **Layout des pages traitement réparé** : le bouton inséré entre `<nav>` et `<section>` devenait un 3ᵉ item dans la grille 2 colonnes (`minmax(...) 1fr`) et cassait la mise en page. Déplacé **dans la colonne liste** (`<nav>`), pleine largeur.
- **Flux « stream not found » corrigé — double cause** :
  1. **Résolution** : le monitor codait 1280×720 en dur ; sur une source 640×360, `ouvrir_shm` rejetait le shm (« trop petit ») → 0 frame → rien publié. Ajout d'un **mode auto-détection** dans `worker_udp.py` : si `width/height = 0`, l'encodeur déduit WxH de la taille du shm (YUV420, ring=10, ratio 16:9). Le monitor utilise ce mode (`_params` width/height=0 ; `normalize_worker_udp_params` préserve le 0). Vérifié : Mire-1_0 (640×360) lu à 25 fps, path `monitor-u2` publié sur la passerelle.
  2. **Timing** : l'`<iframe>` chargeait le lecteur WHEP avant la publication (le lecteur MediaMTX ne se rétablit pas seul). Le panneau attend désormais que le flux publie (`status.publishing`, dérivé du fps `:8080` de l'encodeur) avant de (re)charger l'iframe ; message « source active ? » après timeout. `monitor.status()` expose `publishing`/`live_fps`.

## Container DVE / SuperSource + pilotage par pupitre ATEM — 2026-05-29

**Nouveau type de container `dve` (SuperSource façon ATEM) et émulateur de switcher ATEM permettant de piloter un mélangeur (et ses SuperSources) depuis un pupitre matériel Blackmagic.** Code de ces features en **anglais** (dérogation à la convention française, demandée par l'utilisateur ; helpers existants inchangés).

- **Container `dve`** (`script_templates/dve.py`) : **4 PiP (boxes) + 1 fond → 1 sortie shm** câblable comme n'importe quelle source. Chaque box = centre normalisé + taille (ratio préservé) + **crop** (t/b/l/r), composée via le moteur de `composite_keyer_yuv` du mélangeur. Contrôle live `:8082` (`/state`, `/input` slot 0‑4, `/box`, `/recall`), hot-wire des entrées, métriques `:8080`, gestion SIGBUS. Intégré comme le `mixer` dans `scripts.py`/`deploy.py`/`routes.py` (topologie « composition » à 5 entrées, câblage à chaud, page Câbles).
- **Onglet Traitements → DVE** (`templates/traitements_dve.html`, `static/dve.js`) : éditeur de composition (drag position, coin pour la taille, crop), sélecteurs de source par entrée, **persistance live sans redeploy** (`/dve/persist`).
- **Mémoires de disposition** (table `dve_memories`, par container) : enregistrer/rappeler avec **transition de durée réglable** (interpolation position/taille/crop + fondu α sur les bascules on/off). **Aperçu** miniature par mémoire, **export/import** fichier `.json` et **envoi vers un autre DVE** (format `{name, config}` interopérable).
- **Émulateur switcher ATEM** (`app/atem.py`, UDP 9910, onglet Réglages → Pupitres ATEM) : un seul endpoint, pupitres distingués par leur **IP-source**, affectables à un **mélangeur** + **1‑2 DVE** (SuperSources SS0/SS1). Program/Preview/Cut/Auto/T-bar → API mélangeur ; SuperSource box (`CSBP`) → `/box` du DVE affecté ; tally et positions de box renvoyés au pupitre. Pupitre non affecté = inerte, affiche « Connected to BobiboxMXL ».

**Limites assumées** : le protocole pupitre↔switcher ATEM (et le SuperSource en particulier) est **reverse-engineeré, non validé sans matériel** — l'émulateur est best-effort, bring-up à faire au banc avec `ATEM_DEBUG=1` (offsets `_top`/`SSBP`/`CSBP`, handshake). Le compositing DVE et les mémoires sont vérifiés en loopback.

### Choix du type dès la création du container — 2026-05-29

**On peut désormais choisir un type directement dans le formulaire de création** — fini le round-trip créer → attendre → retrouver le container → définir le type.

- **Formulaire** (`templates/forms.html`) : sélecteur « Type (optionnel) » (aucun par défaut). Avec un type choisi, le container est **déployé automatiquement** dès qu'il est prêt, avec des réglages par défaut (ex. mixer 2 entrées, DVE/correcteur 1280×720, receiver 1 vidéo) — à affiner ensuite sur la page dédiée.
- **Route** (`POST /api/containers`) : nouveaux champs optionnels `deploy_type` + `deploy_params`. Sans type → création seule (inchangé). Avec type → le thread de création attend l'IP puis l'agent (réutilise `_attendre_ip`/`_attendre_agent` du flux projets) avant d'appeler `deployer_script`. Fonctionne en **batch** (chaque container du lot est typé).
- **Suivi batch** : nouvelle colonne **Type** (badge du type déployé, ou « <type> · à venir » tant que le déploiement n'a pas abouti).

## Page Streams : encodage multi-destinations UDP / SRT / WebRTC — 2026-05-29

**Nouvelle page Streams (après Destinations) pour piloter les containers d'encodage/streaming : réglages d'encodage + plusieurs destinations par flux, avec prévisualisation WebRTC.** Code de cette feature en **anglais** (dérogation à la convention française, demandée par l'utilisateur ; les helpers existants gardent leur nom).

- **`worker_udp` étendu** (`script_templates/worker_udp.py`) : d'une sortie UDP unique à un **encode unique → fan-out** via le muxer ffmpeg `tee`. Codec **H.264/H.265**, bitrate/preset/GOP/résolution/fps réglables, **audio AAC optionnel** (2e entrée via fifo depuis un `audio_shm`). Destinations **UDP**, **SRT** (mode caller, latence/passphrase/streamid), **WebRTC** (push RTSP/WHIP vers la passerelle). `onfail=ignore` par branche.
- **Schéma normalisé + compat** (`scripts.normalize_worker_udp_params`) : `deploy_config.params` passe à `{shm_name,audio_shm,video{},audio{},destinations[]}`. Migre l'ancien schéma plat, appelé côté render ET deploy → containers 221-223 intacts (vérifié en prod : redéploiement 221 → flux restauré, SRT confirmé fonctionnel avec libsrt présent).
- **Page Streams** (`templates/streams.html`, routes `/streams`, `GET /api/streams`, `POST /api/streams/<vmid>`) : carte par encodeur, éditeur encodage + destinations dynamiques, fps live + état par destination (poll 5s), emplacement preview WebRTC.
- **Passerelle WebRTC** (`app/webrtc_gateway.py`, `script_templates/webrtc_gateway.py`, onglet Réglages → WebRTC, routes `/api/webrtc/{status,deploy,apply}`, settings `webrtc_*`) : container dédié **MediaMTX** (RTSP/WHIP in, WHEP out, page de lecture). Bouton « Déployer la passerelle » (clone + déploiement du launcher qui télécharge mediamtx au 1er lancement). `deploy._resolve_webrtc_destinations` injecte les URLs ingest/WHEP ; la preview Streams embarque le WHEP en `<iframe>`.

**Limites assumées** : a/v non synchronisés au PTS (monitoring) ; WHIP nécessite ffmpeg ≥ 7.1 (défaut **RTSP**) ; le binaire MediaMTX se télécharge au 1er lancement (le container passerelle doit avoir un accès Internet sortant) ; la preview WebRTC nécessite un codec H.264 (pas HEVC). Déploiement de la passerelle laissé à l'utilisateur (test live de la preview à faire une fois déployée).

### Déploiement passerelle : choix du VMID + suivi streamé — 2026-05-29

Suite retour utilisateur : `/api/webrtc/deploy` ne lançait qu'un thread (aucun suivi). Désormais **réponse streamée ligne par ligne** (même contrat que la recréation de template : dernière ligne ✅/❌), affichée dans un journal live sur l'onglet Réglages → WebRTC. Le **VMID cible est sélectionnable** (champ dédié, vide = auto). `webrtc_gateway.deploy_gateway_iter(vmid)` yield chaque étape (clone → IP → déploiement script → polling `:8080` jusqu'au démarrage de MediaMTX avec sa version, ou l'erreur de téléchargement). `deploy_gateway()` conservé comme wrapper bloquant. **Activation auto** : dès que MediaMTX répond `running` sur `:8080`, `webrtc_enabled` passe à `True` (plus besoin de cocher « Activer » manuellement).

### Onglet WebRTC : 2 sous-onglets + suppression du container — 2026-05-29

Refonte de l'onglet Réglages → WebRTC en deux sous-onglets : **État du serveur** (affiché en premier — badge global, container/VMID/hostname/IP, MediaMTX running + version, WebRTC activé, protocole, ports, URLs ingest/WHEP/player/API, et **table des flux actifs** : path, prêt, source, nb de lecteurs, octets reçus/émis via l'API MediaMTX `:9997`) et **Container** (déploiement avec choix du VMID + log streamé, configuration, et **bouton « Supprimer le container MediaMTX »**). `webrtc_gateway.status()` enrichi (URLs + `paths_detail`), nouveau `destroy_gateway()` (détruit le container + réinitialise `webrtc_gateway_vmid`/`_ip` + désactive `webrtc_enabled`), route `POST /api/webrtc/destroy`.

**Fix déploiement** : un container fraîchement créé n'a pas encore d'IP DHCP ni d'agent up → le déploiement du script échouait immédiatement (« agent injoignable »). `deploy_gateway_iter` attend désormais l'**IP DHCP** (poll `get_container_ip`, ~60s) puis l'**agent `:8081`** (~40s) avant de pousser le script, avec progression dans le log.

**Fix preview WebRTC qui se réinitialisait toutes les 5s** (`templates/streams.html`) : le poll live (5s) réécrivait `pv.innerHTML` → l'`<iframe>` WHEP était recréée à chaque cycle, reconnectant le flux. La preview n'est désormais re-rendue que si sa **signature** (`path` + `embed_url` + actif/inactif, via `previewSig`) change ; l'iframe reste stable tant que le flux est actif.

## Fix : sender/receiver NMOS invisible après déploiement — 2026-05-29

**Un flux 2110 fraîchement déployé (sender ou receiver) n'apparaissait ni sur la page Senders ni dans NMOS jusqu'au redémarrage du service.**

`deployer_script` (`app/deploy.py`) n'appelait `nmos.notify_state_change()` qu'en **début** de fonction (reset des compteurs receivers), donc **avant** l'écriture du `deploy_config`. Or `rebuild_model` identifie les senders/receivers depuis `deploy_config.type` (et `params.video`/`params.audios`) : le rebuild tombait sur l'ancienne config et n'exposait pas le flux. Un redémarrage du service ou un autre déploiement déclenchait un rebuild tardif qui le faisait enfin apparaître.

- **Correctif** : ajout d'un `nmos.notify_state_change()` juste **après** `db_update_deploy_config` (deploy.py:153), à côté du `emberplus.notify_change()` existant. Couvre tous les types — senders 2110 **et** receivers nouvellement créés.
- Le notify précoce en haut de fonction est conservé (reset des `nmos_receivers_count`/`nmos_audio_count` à 0 pour les types non-receiver).
- La page Senders (`/api/nmos/senders_detail`) et l'API IS-04 lisent le même modèle en mémoire `_nmos._senders` : aucun filtre séparé, le fix les corrige toutes les deux.
- Vérifié en live : `TestPaul-4` (vmid 233, sender 1 vidéo + 2 audios) apparaît immédiatement après déploiement, avec son bundle `[video, audio 1, audio 2]`.

## Natural grouping NMOS : un bundle par ensemble vidéo+audios — 2026-05-29

**Les contrôleurs NMOS n'affichaient qu'un seul bundle générique faute de tag de regroupement. Chaque ensemble « 1 vidéo + ses audios » est désormais un bundle distinct.**

Sans le tag de natural grouping, un contrôleur (Sony NCS, BBC nmos-js, etc.) ne sait pas quels flux vont ensemble et regroupe tout. Ajout du tag standard **BCP-002-01** `urn:x-nmos:tag:grouphint/v1.0` sur chaque sender et receiver, dans `app/nmos.py` (`rebuild_model`).

- **Helper `_set_grouphint(resource, group_name, role)`** : pose le tag au format `"<group-name>:<role-in-group>"` (scope `device` par défaut). Le `:` étant réservé comme séparateur, il est banni des noms/rôles (remplacé par espace). Constante `GROUPHINT_TAG`.
- **Règle de bundle = 1 vidéo + ses audios.** `group_name` = hostname du container (suffixé `{hostname} {n}` si plusieurs vidéos sur le même device). Rôles : `video`, `audio 1`, `audio 2`, …
- **Pairing receiver** (vidéo et audio = compteurs indépendants `nmos_receivers_count` / `nmos_audio_count`) : audio _j_ rattaché au groupe vidéo _j % n_video_. Donc 8v/8a → appariement 1:1 (8 bundles), et 1v/Na → tous les audios sous l'unique vidéo. Receiver audio-only → un seul bundle nommé d'après le hostname.
- **Sender** (≤ 1 vidéo par container `worker_2110_sender`) : tout le container forme un seul bundle ; sender audio-only (ex. 225) → bundle `[audio 1]`.
- Vérifié en live sur les containers réels : 211 (8v/8a) → 8 bundles `[vidéo+audio 1]`, 212 (1v/2a) → 1 bundle `[vidéo, audio 1, audio 2]`, 224/236 senders → bundle `[vidéo, audio 1, audio 2]`.

**Limites assumées** : le pairing receiver est positionnel (audio _j_ → vidéo _j % n_video_), pas de mapping explicite vidéo↔audios configurable. Le libellé de bundle affiché par le contrôleur est le hostname du container.

## Refonte UX/a11y page Multiview (audit `/impeccable`) — 2026-05-28

**Score audit 7/20 → cible ~17/20. Toutes les passes (clarify + distill + polish + adapt + colorize + optimize + onboard) menées d'un trait.**

L'écran Multiview portait encore tous les anti-patterns éliminés ailleurs (nested cards, inline styles, alert() natif, glow décoratif, glyphes sans aria-label, labels non associés, zéro responsive). Refonte complète en une session sans toucher au moteur canvas/snap/aligne.

- **Sortie de la structure HTML hors du JS** — `renderEditor()` reconstruisait tout le DOM via `el.innerHTML = …` (~150 lignes de string templates avec `style="…"` inlinés). Structure désormais déclarée dans `templates/traitements_multiview.html`, JS remplacé par `populateEditor(hostname)` qui ne fait que remplir les inputs. Conséquences : focus préservé entre les re-render, scroll préservé, et tous les `style="…"` inline éliminés (sauf un `--swatch` CSS custom property pour la pastille de couleur dynamique).
- **A11y** — Tous les `<label>` associés à leur `<input>` via `for=`/`id=` (~25 paires). Les 11 boutons d'alignement (`⇤ ⇔ ⇥ ⤒ ⇳ ⤓ ↔ ↕ ⛶ ⇶ ⇲`) reçoivent un `aria-label` français explicite, et la toolbar passe en `role="group" aria-label="Outils d'alignement des entrées"`. Hiérarchie de titres remontée : h1 page → h2 colonnes (Multiviews déployés / Layouts enregistrés) et h2 éditeur (hostname). `<input id="layout-save-name">` reçoit enfin un `<label>` visible. Canvas annoté avec `aria-label`.
- **Anti-patterns éliminés** — `backdrop-filter: var(--backdrop-blur)` × 2 supprimés (interdit docs/design/DESIGN.md). Animation `mw-editor-flash` (halo accent pulsé `box-shadow` 6px) supprimée. **Aplatissement des nested cards** : `.mw-canvas-wrap` perd sa bordure, `.mw-entry-panel` et `.align-toolbar` passent en surface tonale `bg-input` sans bordure d'encadrement — l'éditeur reste une carte unique, les sous-blocs deviennent des séparateurs.
- **Toast à la place de `alert()`** — 11 `alert()` natifs remplacés par `showToast(msg, niveau)` qui affiche un bandeau `.mw-toast` sticky en haut de l'éditeur (animation 220ms ease-out-quart, auto-dismiss 4s, `role="status" aria-live="polite"`). Niveaux `info|warning|error` cohérents avec le système d'alertes serveur.
- **Couleurs canvas tokenisées** — Nouveau token `--canvas-bg` propagé dans les trois thèmes (`#0d1117` dark neutre, `#1e1b2e` tinted indigo en Daylight, `#0f0d0a` warm en Studio). Les chromes UI (rectangles de sélection, guides de snap, texte de meta) lisent l'accent et `--text-muted` via `cssVar()` — fini les `#f97316` orange, `#58a6ff` bleu, `#8b949e` gris en dur dans `dessiner()`. Les valeurs qui miment le rendu réel (label blanc, fond bar 70% noir, badge centré) restent littérales puisqu'elles miment ce que le serveur multiview produit effectivement.
- **Responsive** — Premier breakpoint de la page. `.compose-layout` passe en `grid-template-columns: 260px 1fr 260px` puis en `1fr` (1 colonne empilée) sous 1100px, avec sidebars scrollables (max-height 340px) pour ne pas dominer la vue. Sous 640px les champs `field-grow` passent en pleine largeur et le toggle snap reflow sous la toolbar. Touch targets ≥ 32px sur les boutons d'alignement (anciennement ~22px) et 28px sur les actions de layout.
- **Empty states avec CTA** — Liste multiviews vide → lien "Déployer depuis Containers →" ; liste layouts vide → instruction "Composez puis cliquez sur Enregistrer". Éditeur sans sélection → h2 + msg + lien vers `/containers`.

## Snapshots de câblage sur la page Câbles — 2026-05-28

**~14h00 — Sauvegarder / rappeler / effacer tous les câbles du pipeline**

La page `/cables` reçoit une toolbar `[Sauvegarder le câblage…] [Rappeler ▾] [Effacer tout]` qui permet de figer une configuration de câblage, de la rappeler plus tard, et de remettre le pipeline à zéro en un clic. Utile pour basculer entre plusieurs scénarios (répétition, plateau A, démo client) sans recâbler manuellement.

- **DB** (`app/database.py`) : nouvelle table `cable_snapshots(id, name, created_at, payload)` via `init_db()`. Payload = JSON `{"edges": [{from_vmid, to_vmid, shm, kind, to_slot?}, …]}`. Helpers `db_cable_snapshot_save/_list/_get/_delete`.
- **Refacto wire/unwire** (`app/routes.py`) : extraction du gros bloc `if t == "multiview_free": … elif t == "worker_udp": …` des routes `/api/home/wire` et `/api/home/unwire` vers deux helpers Python `_apply_wire(from_vmid, to_vmid, shm, kind, to_slot)` et `_apply_unwire(to_vmid, shm, kind)`, qui retournent `(ok, status, payload)`. Les routes existantes deviennent des wrappers de 5 lignes ; restore/clear bouclent sur ces helpers en interne sans repasser par HTTP.
- **`_collect_current_edges()`** : reconstruit la liste des câbles actifs en scannant `deploy_config` de chaque container. Pour mixer / color_corrector, lit l'état runtime via leur agent HTTP (`_fetch_mixer_inputs`, `_fetch_cc_input`) — pas seulement les `params`. `to_slot` capturé pour multiview/mixer (positionnel dans `flux_config` / `n_inputs`).
- **5 nouvelles routes** (toutes `@require_perm("containers.deploy")` sauf list qui est `@require_login`) :
  - `GET  /api/cables/snapshots` : liste `{id, name, created_at, edge_count}`.
  - `POST /api/cables/snapshots {name}` : capture l'état courant et persiste.
  - `POST /api/cables/snapshots/<id>/restore` : décâble tout via `_apply_unwire` en boucle, puis recâble selon le snapshot via `_apply_wire`. Renvoie `{applied, total, errors[], clear_errors[]}`.
  - `DELETE /api/cables/snapshots/<id>` : suppression simple.
  - `POST /api/cables/clear` : boucle `_apply_unwire` sur tous les câbles courants.
- **Frontend** (`templates/cables.html`) : toolbar entre le `.home-head` et la topo, séparée par une bordure inférieure (`btn-red` "Effacer tout" poussé à droite par `margin-left: auto`). `Sauvegarder` prompt natif pour le nom. `Rappeler ▾` ouvre un popover ancré qui fetch la liste, chaque entrée = nom strong + meta mono `N câbles · ISO date` + bouton ghost `×` de suppression. `Effacer tout` et `Restore` passent par `confirm()` natif. Tous les retours affichent un toast (reuse `showToast`) avec compteurs `applied/total` et messages d'erreur partiels. Refresh `rafraichirCables` à +500ms et +2.5s pour laisser les redéploiements async se propager.
- **CSS** (`base.css`) : module `.cables-toolbar` + `.cables-recall-menu` (popover absolute, border accent, max-height 60vh, z-index 110) + `.cables-recall-row` (flex pick + delete) + `.cables-recall-name` strong, `.cables-recall-meta` mono muted. Tout sur `--space-*`.

**Limites assumées**
- Mixer / color_corrector tracés via leur agent HTTP : si un de ces containers est down au moment de Sauvegarder, ses câbles n'apparaissent pas dans le snapshot (état effectif, pas virtuel).
- Pas de batching par consumer vmid : N wires sur la même multiview_free déclenchent N redéploiements async successifs. Pour ~10-20 câbles c'est OK, le dernier write gagne après quelques secondes. À monitorer si on dépasse.

## Projets : filtre/colonne projet, export/import, remap VMID au restore — 2026-05-28

**Quatre évolutions autour des projets, du plus visible au plus structurel.**

- **Page Containers** : nouveau menu déroulant *Projet* dans la filter-bar (server-rendered avec `Tous` / `Aucun` / une option par projet) et nouvelle ligne *Projet(s)* dans `card-meta`. La liste des projets d'un container est calculée côté serveur par `_attach_projects()` (scan des `snapshot` de tous les projets) et exposée à la fois dans le rendu Jinja et dans `/api/containers` pour rester synchro pendant le polling. `matchesFilter`, `_cardSig`, `_renderCardInner` mis à jour pour gérer `projects[]`. `applyFilterChipsActive` retombe sur `all` si l'option persistée n'existe plus (projet supprimé). Style `.filter-select` aligné sur les chips.
- **Page Projets / liste des containers** : colonne *Projets* ajoutée à droite de chaque ligne du picker (`<span class="proj-picker-projects">`), avec ellipsis et largeur max 40 % pour rester lisible quand un container est dans plusieurs projets. `projects_page()` partage le même helper `_attach_projects`.
- **Export/Import de projet** : bouton **Exporter** sur chaque carte projet (visible même en lecture seule — l'export ne mute rien). `GET /api/projects/<pid>/export` renvoie un JSON `{schema: "bobiboxmxl.project.v1", name, created_at, snapshot}` servi en attachment (`Content-Disposition`). Côté import : bloc *Importer un projet…* gated par `projects.manage`, input fichier caché, prompt pour renommer (pré-rempli depuis le `name` du fichier). `POST /api/projects/import` accepte multipart (`file` + `name` optionnel) ou JSON brut, valide le snapshot et appelle la nouvelle `db_import_project(name, snapshot)`.
- **Restore : auto-remap VMID + détection de conflits** : `restaurer_projet` ne ré-utilisait plus les VMIDs du snapshot tels quels — bug latent quand on voulait faire tourner deux projets enregistrés à des moments différents. Refonte avec nouveau `planifier_restore(snapshot)` qui retourne `{remaps, hostname_conflicts, shm_conflicts, can_restore, error?}`. Les références inter-containers étant **toutes** par chaînes (hostname, shm_name), changer le VMID est sans impact sur les liens — on profite de cette propriété pour remapper automatiquement via `next_free_vmid()` en réservant les VMIDs alloués à la volée. Les conflits **non auto-résolubles** (hostname Proxmox-level, SHM `/dev/shm` au runtime) bloquent le restore avec une alerte rouge listant les collisions — l'utilisateur doit détruire/renommer. Nouvelle helper `_shm_produced(dc)` qui extrait les SHM *produits* par container selon le type (receiver `{hostname}_N`, multiview `shm_out`, mixer 3 sorties, color_corrector ; workers exclus car consommateurs purs). Route `GET /api/projects/<pid>/restore_preview` (gated `projects.manage`) consommée par `restaurerProjet` côté JS pour un pré-vol : si conflits bloquants → alert détaillé et abandon ; si remaps non triviaux → confirm listant `hostname : VMID X → Y` ; sinon confirm standard. Fallback gracieux si le preview échoue (réseau, etc.).
- **Schéma DB inchangé** : les snapshots continuent de stocker le VMID — seule la décision au restore change. Compat des projets déjà enregistrés conservée.

## Refonte UX/a11y pages Containers, Palette de déploiement et Projets — 2026-05-28

**~12h30 — Passe `/impeccable` complète sur les trois surfaces**

Pas de nouvelle feature : itération de design sur l'existant. Trois objectifs poursuivis sur toute la session : éliminer les anti-patterns du DS (em-dashes, nested cards, inline styles, monospace décorative), aligner la sémantique HTML/ARIA (cards en `<article>`, h-levels continus, labels associés, aria-live ciblés), et systématiser une échelle d'espacement explicite.

- **Échelle de tokens** : ajout de `--space-1` (4px) à `--space-7` (48px) dans `:root`. Migration progressive des magic numbers (10, 12, 14, 16, 18) vers ces tokens dans tout le bloc containers + palette + projects.
- **Page Containers (`cards.html`)** : h2 redondant supprimé, compteur intégré dans une filter-bar allégée (plus de panneau bg-elev, juste une bordure inférieure), chips de filtres groupés en `role="group"`, cards `<div>` → `<article>`, hostname en `<h2 class="card-host">` (hiérarchie h1 → h2 continue), structure interne refondue (`.card-head` flex baseline, `.card-tags`, `<dl class="card-meta">`, `.card-actions` avec wrapper `.card-actions-primary` pour le cluster non destructif + `.btn-red` flex-isolé à droite), boutons de card resserrés (~28px), `:focus-visible` accent partout, `aria-label` contextualisé sur Détruire, `aria-controls`/`aria-expanded` sur Configurer (re-synchro après chaque tick via `_syncConfigureExpanded`), sentinels typographiques (`—`) remplacés par "non assignée" / "aucun" en Inter (`.card-meta dd.dimmed`).
- **Status & mode badges** : 2 nouveaux tokens `mode-mixer` (rose OKLCH 0.78 0.05 20) et `mode-corrector` (olive OKLCH 0.80 0.05 110) propagés en theme light/studio. Mapping `STATUS_FR` symétrique côté Jinja (`STATUS_FR.get(c.status, c.status)`) et JS (`statusLabel`) : `running → en cours`, `script_stopped → script arrêté`, etc. Plus aucun underscore brut affiché. Nouvelle classe `.badge.ready` (pas de lowercase forcé, font Inter) pour "Container prêt" sur container LXC up sans deploy_config.
- **Light theme** : override complet des 6 `.mode-*` (text foncé + bg pâle, ratio ≥4.5:1) parce que les couleurs dark étaient illisibles sur `#f6f7f9`. Règle "Trois Lumières" du DS restaurée.
- **Performance Containers** : `updateContainers` passe en diff par VMID via une `Map _cardSigs` (signature concaténée des champs mutables : fps, status, ip, cpu rounded, deploy_config). Les cards inchangées entre deux ticks ne sont plus touchées. CPU bar passe de `transition: width` à `transform: scaleX()` (hors layout). Resync ARIA après chaque patch.
- **Palette de déploiement (`deploy_palette.html`)** : largeur 360 → **440px**. ~15 `style="…"` inlinés extraits en classes (`.palette-divider`, `.palette-section-head`, `.palette-hint`, `.palette-row`, `.palette-field-check`, `.palette-field-format`, `.btn-ghost-icon`). Bouton Fermer passé de `btn-red` à `btn-ghost-icon` (32×32) — fermer ≠ détruire. Sélecteur de type refondu en `palette-group` borderless avec chips touch-friendly (8×14, min-height 34) + ligne récap repliée par défaut sur container déjà déployé (`.dp-type-summary` avec mode-badge intégré aux mêmes couleurs que sur la card). Sections devenues `<fieldset class="palette-group"><legend>` avec ratio rythmique 2:1 (24px entre fieldsets, 12px entre fields). Bouton Déployer passé de `btn-orange` (= Stop sémantiquement) à `--accent`. 25 paires `for=`/`id=` ajoutées sur tous les inputs/selects. `<aside aria-labelledby="dp-palette-heading">`, h2 sémantique pour le hostname. `palette-subgroup` (Audio #1/#2 dans Sender 2110) débarrassé de son nested-card visuel.
- **Bloc Simulation receiver (rendu JS)** : module CSS dédié `.rx-sim-*`. Slots audio plus de border encadrée (séparateur top dashed). Canaux 1–8 en **pills toggle** (grid 8 colonnes, état checked = accent-soft + accent border + weight 600, checkbox native cachée mais accessible clavier). Toggles `générateur` (vidéo + audio) en pills cohérentes, taille 28px. Master switch "Activer la simulation par slot" et les 3 checkboxes `.palette-field-check` (incrustation HTML, émission 2110-20) re-stylés en **switch iOS-like** (track 36×20 + thumb 14×14, glissement 220ms ease-out-quart, accent-soft ON / bg-input OFF). **Slot teinté accent** quand `dp-rx-sim-v-on` OU `dp-rx-sim-a-on` est coché (`:has(.a:checked)` CSS, pas de JS) : repérage immédiat des slots qui produisent un signal local vs ceux qui attendent un sender NMOS.
- **Page Projets (`projects.html`)** : alignement complet sur le pattern containers (cards `<article>`, `.card-head` + `.card-meta` + `.card-actions-primary` + `.btn-red` détaché, `.card-vmid` au lieu de `<small class="meta">`, `aria-label` contextualisé sur les 3 boutons d'action `Restaurer le projet X`, etc., empty-state moderne avec h3 + message conditionnel selon permission, deux em-dashes éliminés). Hint lecture-seule explicite quand l'utilisateur n'a pas `projects.manage`. Sélecteur "Containers à inclure" totalement refait : ancien chip-cloud → **liste verticale scrollable (`<fieldset class="proj-picker">`) avec recherche par nom + chips de filtre par type (7 catégories + Aucun)** + raccourcis "Tout cocher / Tout décocher" qui agissent sur les rows visibles uniquement (intersection recherche × type) + compteur live de sélection. Rows colorées accent quand cochées (`:has(input:checked)`).
- **docs/design/DESIGN.md** : nouveaux tokens `mode-mixer` et `mode-corrector` documentés (frontmatter + section Modes) avec coordonnées OKLCH.

**Verdict audit** : containers cards 19/20, deploy_palette en cours, projects partiellement remonté. La dette restante (sticky bottom du bouton Déployer, ARIA tab-list complet sur les chips de type, accessibilité des selects pré-populés en JS) est listée dans les findings P3 pour itérations ultérieures.

## Pliage et recherche sur Sources / Destinations — 2026-05-28

**~10h57 — Vue globale plus dense, état de pliage persisté**

Les pages `/nmos/receivers` (Sources) et `/destinations` rendaient une grille de cartes ouvertes par container, ce qui demandait beaucoup de scroll dès qu'on a ~10 containers NMOS. Chaque carte est désormais repliable/dépliable d'un clic sur son en-tête, avec un champ recherche et deux boutons « Tout replier / Tout déplier ».

- **État persisté** : Set des vmids repliés sauvegardé dans `localStorage` (clés `nmr.collapsed` / `nms.collapsed`), pour survivre au refresh auto 5 s et aux rechargements de page.
- **Header carte** : chevron `▾` qui pivote de -90° en mode replié (transition 120 ms), badge de statut repoussé à droite via `margin-left:auto`. Quand replié, on n'affiche plus que `nom #vmid — N/M actif(s)` + badge — assez pour scanner l'état sans déplier.
- **Recherche** : `<input type="search">` filtre les cartes par `hostname`, `#vmid` ou IP (substring case-insensitive). `data-search` pré-calculé sur chaque carte. Les boutons « Tout replier/déplier » ignorent les cartes filtrées (`display:none`).
- Code dupliqué entre les deux templates (fonctions `toggleCollapse`/`collapseAll`/`applyFilter` symétriques, IDs `nmr-*` vs `nms-*`) — assumé : les deux pages ont des structures de données différentes (`receivers` vs `senders`) et factoriser dans un JS partagé ajouterait plus de complexité que ça n'en retire.

## Boutons NKK sur la page Mélangeurs — 2026-05-28

**~10h43 — Look broadcast pour PGM/PVW/CUT/DISSOLVE**

Les boutons de la page `/traitements/melangeurs` adoptent un style de poussoirs illuminés NKK (bezel sombre biseauté + capuchon translucide).

- `templates/traitements_melangeurs.html` : classe générique `.nkk-btn` (housing) + `.nkk-cap` (capuchon). État éteint = gris-noir terne ; états illuminés via `.on-pgm` (rouge), `.on-pvw` (vert), `.btn-cut` (rouge), `.btn-dissolve` (bleu/cyan), chacun avec halo `box-shadow` extérieur. Enfoncement à `:active` (`translateY(1px)` + ombres atténuées).
- `static/mixer.js:renderMixerEditor` : chaque bouton de slot et les deux boutons TAKE sont wrappés `<button class="nkk-btn …"><span class="nkk-cap">…</span></button>`. Le `<small>` du nom de shm câblé reste dans le capuchon.

## Suppression de l'onglet "Créer" du multiview — 2026-05-28

**~10h30 — Le Layout Editor devient la seule vue de `/traitements/multiview`**

Le déploiement d'un nouveau multiview se fait désormais exclusivement via la palette de déploiement globale (`deploy_palette.html`), l'onglet "Créer" faisait doublon.

- `templates/traitements_multiview.html` : suppression du bloc `<div class="tabs">` et du sous-onglet `#tab-create` (formulaire `mv_vmid/mv_shm_out/…` + bouton Déployer). Le panneau `#tab-compose` n'est plus masqué par défaut.
- `static/multiview.js` : `switchTab()` réduit à un appel direct à `rafraichirListeMw()` + `rafraichirListeLayouts()` (conservé en no-op pour compat avec les liens `?tab=compose` existants depuis la palette et le drag-drop home). `creerMultiview()` supprimée (plus aucun appelant).

## Latence par câble sur la page Câbles — 2026-05-28

**~04h45 — Mesure de la latence interne dans chaque script consommateur**

Chaque câble de la page `/cables` affiche désormais au milieu de son tracé SVG la latence de traitement vue par le consommateur, en `ms`. La donnée existait déjà mais n'était pas exposée : chaque frame SHM porte un header `struct.pack("QQ", frame_index, time.time_ns())`, donc on peut calculer Δ = ts_output − ts_input à chaque frame écrite.

- **Définition** : pour les nœuds avec sortie SHM (multiview, mixer, color_corrector), latence = `ts_out − ts_in_par_input` (temps de traitement propre du nœud par câble). Pour les sinks sans sortie SHM (worker_udp, worker_2110_sender), fallback sur `now − ts_in` (âge du frame consommé au moment de l'encode).
- **Helper inline `RollingMs(maxlen=30)`** dupliqué dans chaque script template (fenêtre glissante ~600 ms-1.2 s selon fps, invalide à `null` après 2 s sans push). Pas de module partagé : les scripts sont autonomes et déployés indépendamment dans chaque container, le helper se duplique de 12 lignes.
- **Scripts modifiés** :
  - `worker_udp.py` : push `age_us/1000` à chaque frame encodée, expose `inputs_latency_ms: {<shm_name>: ms}` dans `:8080/`.
  - `worker_2110_sender.py` : tracking séparé vidéo + audios, ajoute `latency_ms` et `shm_name` à chaque entrée `senders[]`.
  - `color_corrector.py` : `lire_frame_yuv` renvoie maintenant `(bytes, ts_in)`. Push Δ après l'écriture output, expose `inputs_latency_ms`.
  - `multiview_free.py` : dict `lat_in: {basename: RollingMs}`. Capture `ts_in_per_input` à chaque PiP lu, push Δ après l'écriture output. Indexé par basename (`/dev/shm/X` stripé).
  - `mixer.py` : `lire_frame_yuv` renvoie `(bytes, ts_in)`. Tracking par slot (PGM, PVW, from/to durant transition), indexé par `state["inputs"][slot]` (shm_name câblé à chaud). Les inputs câblés mais non sélectionnés n'ont pas de mesure (rien n'est consommé pour eux), donc affichent `—`.
  - `receiver.py` : aucun changement (source pure).
- **Backend** :
  - `app/metrics.py` : nouveau cache mémoire `latency_cache[vmid] = {shm: ms}`. `rafraichir_metrics()` extrait `inputs_latency_ms` (multiview/mixer/cc/worker_udp) et `senders[].latency_ms+shm_name` (sender 2110), normalise dans un seul dict par vmid. Pas de colonne DB (valeur instantanée haute-fréquence comme fps).
  - `app/routes.py:api_home_summary()` : lookup `latency_cache[edge.to][edge.shm]` et ajoute `latency_ms` sur chaque entrée de `topology.edges[]`.
- **Frontend** (`templates/cables.html`) : pour chaque arête, calcul du midpoint Bézier cubique à t=0.5 via la formule `(P0 + 3·P1 + 3·P2 + P3) / 8`, et insertion d'un `<text>` SVG centré (`text-anchor=middle`, `dominant-baseline=middle`). Texte : `XX ms` ou `—` si `null`.
- **CSS** (`base.css`) : nouvelles classes `.topo-edge-lat.{ok|warn|err|none}` avec `paint-order: stroke` + `stroke: var(--bg)` 3 px pour rester lisible quel que soit le fond. Seuils : `ok` ≤ 100 ms, `warn` 100-500 ms, `err` > 500 ms (gras), `none` muted opacity 0.7.

**Note opérationnelle** : les containers déjà déployés tournent sous l'ancienne version du script. Pour récupérer la mesure, il faut redéployer chaque consommateur (bouton Restart ou Modifier + Déployer dans Containers).

## Éclatement de la page Traitements en 3 sous-routes — 2026-05-28

**~04h35 — Fix Jinja2 : 3 `{% endblock %}` dans le bloc `content`**

- Symptôme : `/multiview` renvoyait 500 `TemplateSyntaxError: Encountered unknown tag 'endblock'`. Cause : à chaque ajout d'onglet (Mélangeur puis Correcteurs), j'avais refermé prématurément le bloc `content` avant d'ouvrir le suivant, finissant avec 3 `{% endblock %}` au lieu d'un seul.
- Fix immédiat : suppression des 2 `{% endblock %}` orphelins dans `templates/multiview.html`.

**~04h40 — Refactor : 1 fichier monolithique → layout + 3 sous-pages**

La page Traitements groupait 3 traitements distincts dans 4 onglets (Créer/Layout Editor pour le multiview, Mélangeur, Correcteurs) dans un seul `multiview.html` de ~175 lignes. Éclaté en 4 fichiers + 4 routes :

- `templates/traitements_layout.html` — extends `layout.html`, définit le tab bar partagé (3 onglets cliquables vers `/traitements/{multiview,melangeurs,correcteurs}`) et 3 blocs Jinja (`traitements_head`, `traitement_content`, `traitement_scripts`).
- `templates/traitements_multiview.html` — page Multiview avec ses 2 sous-tabs internes inchangés (Créer + Layout Editor), charge `multiview.js`.
- `templates/traitements_melangeurs.html` — page Mélangeur, charge `mixer.js` + appel `rafraichirListeMixers()` au DOM ready.
- `templates/traitements_correcteurs.html` — page Correcteurs, charge `color_corrector.js` + appel `rafraichirListeCorrecteurs()` au DOM ready.

**Routes** (`app/routes.py`) :
- `/traitements` → 302 vers `/traitements/multiview` (préserve la query string).
- `/traitements/multiview`, `/traitements/melangeurs`, `/traitements/correcteurs` — 3 nouvelles routes.
- `/multiview` → 301 vers `/traitements/multiview` en préservant la query string (bookmarks utilisateurs + legacy drag-drop home `?tab=compose&vmid=…&add_shm=…` continuent à fonctionner).

**Propagation** : nav `layout.html` → `/traitements`, lien dynamique dans `static/scripts.js`, lien statique dans `templates/deploy_palette.html`, simplification de `switchTab()` dans `static/multiview.js` (ne gère plus que les 2 sous-tabs internes Créer/Layout Editor, plus les 3 top-level). Auto-switch sur `?tab=compose` ajouté au DOMContentLoaded pour le drag-drop home.

**Bilan** : URLs auto-descriptives et bookmarkables, 1 fichier par traitement (plus facile à éditer), JS de chaque traitement isolé. Le tab bar reste visuellement identique mais les onglets sont maintenant des `<a href>` au lieu de boutons JS.

## Correcteur de couleur (numpy live) — 2026-05-28

**~04h15 — Nouveau type de container : color_corrector**

Nouveau traitement 1→1 qui applique de la color correction en temps réel, sans relance de process à chaque changement de slider.

- **Compositing en numpy** (option A choisie après comparaison avec ffmpeg+zmq) : la boucle principale lit les params depuis un dict partagé sous lock à chaque frame. Slider bougé = effet visible dès la frame suivante, zéro process à relancer, aucune dépendance ajoutée.
- **Fast path YUV** (gratuit, ops sur les planes Y et U/V uniquement) pour brightness, contrast, saturation, gamma global et hue rotation. Hue rotation = rotation 2D des planes (U-128, V-128) avec sin/cos.
- **Slow path RGB** (roundtrip 0→255) activé conditionnellement uniquement si gamma R/G/B ou colorbalance non-identité. Conversion BT.601 explicite. Quand tous les params avancés sont à l'identité, on n'y va pas → coût négligeable.
- **17 paramètres** :
  - Basics (5) : brightness (-1..1), contrast (0..2), saturation (0..3), gamma (0.1..10), hue (-180..180°)
  - Avancés (12) : gamma R/G/B + colorbalance 3 axes (R/V/B) × 3 zones (ombres/midtones/hautes lum.). Algo colorbalance approximé : pondération par luma — shadow_w = clip(1-2L, 0, 1), hi_w = clip(2L-1, 0, 1), mid_w = 1 - shadow_w - hi_w. C' = C + (Σ poids·offset) × 128.
- **HTTP control** sur `:8082` : `GET /state` (params + defaults + input_shm + shm_out), `POST /params` (PATCH merge, partial updates), `POST /reset` (tout aux defaults), `POST /input {shm}` (câblage à chaud).

**~04h20 — Backend, presets globaux et intégration câblage**

- `app/scripts.py` : nouveau case `color_corrector` (params `shm_out`, `input_shm`, `cc_params`, format vidéo).
- `app/deploy.py` : source = input_shm, shm = shm_out (défaut `<hostname>_cc`). Reset des compteurs NMOS comme les autres non-receivers.
- `app/database.py` : nouvelle table `cc_presets(id, name UNIQUE, params JSON, created_at, updated_at)` + helpers `cc_presets_list/create/update/delete`.
- `app/routes.py` :
  - 4 endpoints proxy `/api/containers/<vmid>/color_corrector/{state,params,reset,input}` factorisés via `_cc_proxy()`.
  - 4 endpoints presets globaux `/api/color_correctors/presets[/<id>]` (GET/POST/PATCH/DELETE).
  - Intégration topologie : `color_corrector` rejoint la colonne "composition" avec produces=shm_out (vidéo) et consumes=input_shm (vidéo, `disconnected:true` si non câblé). `_fetch_cc_input(ip)` hit `:8082/state` pour lire l'input courant.
  - Wire/unwire à chaud via `_cc_set_input()`, même modèle que le mixer — pas de redeploy.

**~04h25 — Onglet "Correcteurs de couleur" dans Traitements**

- Nouvel onglet (3e tab) dans `templates/multiview.html` + `static/color_corrector.js` (~250 lignes).
- Layout 2 colonnes : liste des correcteurs déployés à gauche, éditeur à droite.
- Éditeur :
  - Bandeau d'état (entrée câblée, sortie) + champ de câblage avec bouton "Appliquer".
  - **Toolbar** : dropdown des presets globaux + Charger / Enregistrer sous… / Supprimer · "Tout réinitialiser" · checkbox "Mode avancé".
  - **Réglages de base** (toujours visibles) : 5 sliders couplés range+number, bouton ↺ par slider pour reset individuel à la valeur par défaut lue du backend.
  - **Mode avancé** (révélé sur clic checkbox) : section gamma par canal (3 sliders) + section colorbalance (grille 3×3, ranges -1..1).
  - Commit instantané : `input` du slider met juste à jour le number frère ; `change` (relâchement) déclenche `POST /params {key: value}` qui merge côté script. Pas de debounce nécessaire vu qu'il n'y a pas de relance.
  - Presets globaux (DB) chargeables sur n'importe quel correcteur — partage du look entre containers.
- Palette de déploiement : option "Correcteur de couleur" dans le `<select>`, section minimale (juste shm sortie). Le câblage et les params se font à chaud depuis l'onglet.
- Filter chip + mode-badge "Correcteur" dans `cards.html` et `scripts.js`.

## Système design, page Câbles, câblage interactif — 2026-05-28

**~01h45 — Système de design formalisé (docs/design/PRODUCT.md + docs/design/DESIGN.md)**

- Nouveaux fichiers à la racine : `docs/design/PRODUCT.md` (registre `product`, personas, anti-réfs Linear/Grafana/Tailscale, 5 principes design dont "Précision sans densité" et "Cohérence française") et `docs/design/DESIGN.md` (format Stitch : YAML frontmatter + 6 sections normatives). Sidecar `.impeccable/design.json` avec tonal ramps OKLCH, shadows, motion, 8 composants HTML/CSS pour rendu live.
- Tokens couleur formalisés : Slate Signal (default), Daylight Indigo, Studio Amber, palettes statuts (running/stopped/warning/unknown), palettes modes (receiver/multiview/stream/sender). Règles nommées : "Statut > Couleur", "Trois Lumières", "Saturation Mesurée", "Pas de Glassmorphism".

**~01h55 — Suppression complète du thème Aurora**

- Aurora (glassmorphism + dégradé violet) violait la règle "Pas de Glassmorphism". Retiré partout :
- `static/css/theme-aurora.css` supprimé.
- Entrée `aurora` retirée de `app/settings.py` (THEMES + commentaire) et de `templates/settings.html` (option `<select>`).
- Doc `templates/aide.html` recalée (descriptions des 2 thèmes restants : Studio, Daylight).
- Garde-fou existant `main.py:60` (`theme not in valid → 'classic'`) prend en charge automatiquement les DB qui auraient `theme='aurora'` stocké.

**~02h00 — Polish : élimination des side-stripe borders**

- Les `border-left/right ≥ 2px` colorées sont un anti-pattern interdit par docs/design/DESIGN.md. 5 occurrences corrigées :
- `base.css` `.alert-{info,warning,error}` : `border-left: 2px` → full border colorée 1px + fond teinté via tokens de statut.
- `multiview.css` `.mw-entry-row.is-primary` : `box-shadow inset 2px 0 0` (stripe latérale) → `inset 0 0 0 1px` (anneau full) + cellule 1 en bold accent.
- `aide.html` 3 callouts (`.aide-note`, `.aide-warning`, `.changelog-entry`) : `border-left: 3px` → full border 1px + bg teinté. Au passage hardcode `#e8a33d` remplacé par token `--status-warning-fg`.

**~02h05 — Critique UX de la home (heuristiques Nielsen)**

- Score 22/40. P0 identifié : double violation des bans absolus impeccable (hero-metric template + identical card grids). 12 cards-compteurs sans hiérarchie ne servent ni la persona "spectateur démo EBU" (qui doit comprendre la topologie en 5s) ni "opérateur en diag" (qui ne trouve pas l'info qu'il cherche).
- Snapshot persisté : `.impeccable/critique/2026-05-27T23-46-47Z__templates-home-html.md`.

**~02h15 — Refonte de la home en vue topologique pipeline**

- Remplacement complet des 12 cards-compteurs par une **topologie 3 colonnes** : Sources / Composition / Sorties. Chaque container = un nœud avec hostname, vmid, badge statut, mode-badge, fps live. Click sur nœud → drill-down vers `/containers#c-X`.
- **Barre santé système** en une ligne fine au-dessus du graphe : PTP / mDNS / SR-IOV / Containers / NMOS avec dots colorés et tooltips natifs définissant les acronymes (lisibilité démo).
- **SVG inline** pour les arêtes : courbes Bézier cubiques entre les nœuds, theme-aware via `var(--*)`, pas de canvas. Tooltip flottant au survol d'une arête : nom du flux, source → destination, statut, fps.
- **Backend** : `/api/home/summary` étendu avec `topology: {nodes, edges}` ; topologie dérivée du `deploy_config` (shms produits/consommés par container).
- **Markup** : ajout `id="c-{vmid}"` dans `cards.html` pour le drill-down via fragment.
- États couverts : empty total, empty par colonne (ghost-label dashed), erreur fetch (3 échecs = `body.home-stale`), `prefers-reduced-motion` désactive la pulse des arêtes actives, responsive ≤900px (cols stack, SVG masqué).

**~02h25 — Arêtes audio/vidéo en couleurs distinctes + ports nommés**

- Nouveaux tokens CSS `--topo-flow-video` / `--topo-flow-audio` (+ variantes `-active`), override par thème :
  - Classic : vidéo = slate, audio = ocre `#d6b08a`.
  - Daylight : vidéo = indigo, audio = orange brûlé `#c2410c`.
  - Studio : vidéo = amber, audio = cyan menthe `#5eead4`.
- Backend `produces`/`consumes` portent désormais `{shm, kind: 'video'|'audio'}`. Edges héritent du kind, rendus avec stroke distincte + chip `VIDÉO`/`AUDIO` dans le tooltip.
- Chaque nœud expose désormais sa **liste de ports** (entrées à gauche, sorties à droite, débordant `-16px` du card pour que les arêtes y atterrissent) avec un dot coloré par kind. Plus de superposition quand plusieurs shms vont entre la même paire de nœuds.

**~02h35 — Câblage interactif (click source → click destination)**

- Sortie inutilisée = dot en anneau vide cliquable. Click → wire mode actif avec bannière en bas (shm en cours + `Échap` pour annuler), polling pausé pour éviter les rerenders qui détruiraient les highlights.
- Cibles compatibles (multiview / worker_udp / worker_2110_sender) ressortent en bordure accent + halo, les autres nœuds passent à 45% opacity et `pointer-events: none`.
- Drop précis sur port-in : flash animé du dot (`topo-port-drop`) + commit. Hover sur cible grossit le dot à 16px pour clarifier le hotspot.
- Suppression de câble : click sur arête → popover [Supprimer] / [Annuler].
- Limites typage : audio shm → uniquement worker_2110_sender ; vidéo shm → tous les consommateurs vidéo.

**~02h55 — Câblage in-place via API REST (workflow 1 temps)**

- Première implémentation faisait une redirection vers `/multiview` ou `/containers` avec query params (`?wire_shm=…`) et pré-remplissait la palette ; l'utilisateur devait cliquer Deploy. Trop long. Nouveau workflow : click destination = câble créé immédiatement.
- Nouveaux endpoints `POST /api/home/wire` et `POST /api/home/unwire` qui mettent à jour `deploy_config` du consommateur et redéploient le script en thread async. Réponse immédiate côté UI.
- Toasts en haut à droite : bleu pendant l'appel, vert "Câble créé" en succès, rouge "Erreur : …" sinon. Refresh topologie à 300ms puis 2s pour voir l'arête apparaître.
- Conversion `<a href>` → `<div role="link" data-href>` sur les nœuds pour éliminer tout risque de navigation native parasite pendant le wire mode. Drill-down hors wire mode géré entièrement en JS.

**~03h05 — Multiview : préservation de la géométrie au câblage et au décâblage**

- Wire sur un slot multiview ne change QUE le `path` ET le `name` rendu. Géométrie (x, y, w, h), label_source, audio meters, tally, color, in_w/in_h : tout conservé. Backend recalcule `name` selon `label_source` (`hostname` → lookup du producteur, `mxl_path` → shm brut, `protocol` → placeholder pour TSL runtime).
- Unwire ne supprime PAS l'entrée du `flux_config` : il vide juste `path = ""` et `name = ""`. Le slot reste à sa place avec sa géométrie. Le script multiview rend du **noir pur** (Y=0, contre Y=64 / gris avant) pour les slots sans source via deux modifs dans `script_templates/multiview_free.py`.
- **Composer multiview** : la source devient facultative à la création d'un PiP. Option `— laisser vide pour un PiP non câblé —` dans le `<select>`, bouton `+ Ajouter PiP` (renommé). On peut composer un layout entier (géométries, labels, meters) avant tout câblage et brancher les sources depuis la home.

**~03h15 — Indexation PiP 1, PiP 2 … stable + fan-out**

- Les ports d'entrée d'un multiview affichent `PiP N · shm_name` (ou `PiP N (vide)` si disconnected) au lieu du nom du shm. Indexation stable : débrancher le PiP 1 ne décale pas les PiP 2/3/4.
- Badge `+N PiP libre(s)` (au lieu de "slot libre(s)") pour signaler les slots à créer dans le composer.
- Edge tooltip et popover de suppression mentionnent désormais "vers PiP N" quand le consommateur est un multiview.
- **Sources réutilisables** : toute sortie (libre ou déjà utilisée) est cliquable comme source de wire. Fan-out d'un même shm vers plusieurs consommateurs autorisé. Le visuel distingue libre (anneau) vs utilisée (rond plein).
- Slots **placeholders** (PiP au-delà du `flux_config` actuel, jusqu'à `max_inputs`) restent visibles mais en `cursor: not-allowed` opacity 45% pendant le wire mode ; click → toast invitant à passer par le composer pour fixer la géométrie.

**~03h40 — Page dédiée `/cables`**

- Nouvelle entrée de nav entre Destinations et Projets : `Câbles`. Route `/cables` avec `Cache-Control: no-store`, template `cables.html` qui porte désormais toute la topologie + le câblage interactif + les toasts + le popover edge.
- `home.html` réduite à la barre santé + alertes (dashboard système light). Plus de topologie sur la home — à remplacer plus tard par autre chose.

**~03h50 — Intégration du mélangeur dans la page Câbles**

- Le type `mixer` (nouveau container ajouté plus tôt dans la journée) est désormais affiché en colonne Composition à côté des multiviews.
- **3 sorties** rendues avec labels explicites : `PGM`, `CLEAN`, `PVW`. Le shm réel est en tooltip et sert pour le matching d'edges (nouveau champ optionnel `label` sur les ports).
- **N inputs** rendus comme `Input 1`, `Input 2`, … `Input N`. Les inputs du mélangeur sont contrôlés à chaud (pas dans deploy_config), donc backend GET `http://{ip}:8082/state` à chaque refresh de topologie (timeout 0.5s) pour récupérer les shms câblés.
- **Câblage / décâblage à chaud** : wire sur un Input n'appelle pas le redeploy script mais POST `http://{ip}:8082/input` avec `{input: slot, shm: …}` directement au mélangeur. Pas d'interruption du PGM/PVW pendant le câblage.

**~03h55 — Sanitizer du champ Hostname (création de container)**

- `templates/forms.html` : nouveau handler `oninput="this.value = sanitizeHostname(this.value)"` sur le champ `#hostname`, plus tooltip explicatif.
- `static/scripts.js` : fonction `sanitizeHostname(v)` qui décompose les accents (NFD + strip des combining marks U+0300-U+036F), convertit espaces et underscores en tirets, retire tout caractère hors `[A-Za-z0-9-]`, collapse les tirets multiples, strip les tirets en début/fin. La casse est préservée. Exemples : `Caméra Régie 1 (Béta)` → `Camera-Regie-1-Beta`, `cam_1` → `cam-1`, `mxl@local` → `mxllocal`. Sanitization aussi appliquée à la soumission dans `creerMultiple()` en défense contre les copier-coller.

---

## Mélangeur PGM/PVW + streamer fps adaptatif — 2026-05-28

**~03h25 — Streamer worker_udp : framerate adaptatif**

- Bug : tous les `worker_udp` (encoder/streamer) tournaient à 12.5 fps quelle que soit la source. Cause : décimation hard-codée `frame_index % 2 == 0` + ffmpeg figé sur `-r 25 -g 25 -keyint_min 25`.
- Fix : suppression de la décimation ; nouveau param `FPS` injecté au déploiement ; ffmpeg utilise `-r FPS -g FPS -keyint_min FPS` (GOP = 1 s indépendamment du framerate).
- `app/scripts.py` passe `fps` (arrondi entier, défaut 25) au template.
- **Containers déjà déployés à redéployer** pour bénéficier du fix — la palette envoie déjà `fmt.fps` dans les params depuis la refonte du sélecteur de format.

**~03h30 — Nouveau type de container : mélangeur**

Nouveau script `script_templates/mixer.py` (~330 lignes) — un mélangeur N→1 avec switching frame-accurate et calque HTML.

- **Inputs** : 1 à 8 slots, chacun câblable à chaud sur n'importe quel shm. `ensure_input(idx)` (re-)ouvre les mmap quand le câblage change (lazy, par slot).
- **Compositing** en YUV pour le fast path (cut sans overlay = copie de bytes ; dissolve = alpha-blend des planes Y/U/V). Roundtrip RGB uniquement quand l'overlay HTML est actif.
- **Modèle PGM/PVW** : `state.pgm` = à l'antenne, `state.pvw` = preview, `state.transition` pour les dissolves en cours. Après un take, flip-flop automatique (ancien PGM devient PVW).
- **Overlay HTML** via Playwright + Chromium headless : thread dédié qui screenshot la page en RGBA toutes les ~40 ms (viewport WIDTH×HEIGHT, `omit_background`, fond doc transparent forcé via `evaluate`). Compositing alpha sur la frame PGM.
- **HTTP control** sur `:8082` : `POST /pgm`, `POST /pvw`, `POST /take {duration_ms?}` (0 = cut, sinon dissolve), `POST /input {idx, shm}` pour le câblage à chaud, `POST /overlay {url?, enabled?}` (PATCH-like, les deux indépendants), `GET /state`.
- **Métriques** sur `:8080/` : fps, frame_index, pgm, pvw.

Backend :
- `app/scripts.py` : nouveau case `mixer` avec params `n_inputs`, `shm_out_*`, `width/height/fps`, `dissolve_ms`, `overlay_url`, `overlay_enabled`.
- `app/deploy.py` : dénormalisation `source = "N entrée(s) [+ overlay]"`, `shm = "PGM:… · clean:… · PVW:…"`. Reset `nmos_*_count` à 0 pour ce type.
- `app/routes.py` : 6 endpoints proxy `/api/containers/<vmid>/mixer/{state,pgm,pvw,take,input,overlay}` factorisés via `_mixer_proxy()`.

**~03h35 — Template VMID 299 : Playwright + Chromium**

- Le LXC template doit fournir Playwright et Chromium headless pour que les mélangeurs aient l'overlay HTML. Modifications de `app/template_recreate.py` :
- Ajout dans `PROVISION_SCRIPT` : `python3-pip` + libs runtime Chromium (`libnss3`, `libatk1.0-0`, `libatk-bridge2.0-0`, `libcups2`, `libxkbcommon0`, `libxcomposite1`, `libxdamage1`, `libxrandr2`, `libgbm1`, `libpango-1.0-0`, `libcairo2`, `libasound2t64`, `fonts-liberation`).
- `pip3 install --break-system-packages playwright` puis `playwright install chromium` vers `/opt/playwright-browsers` ; env var aussi définie via `/etc/profile.d/playwright.sh`.
- `mixer.py` force `os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/playwright-browsers")` avant l'import (les scripts tournent sous systemd qui ne source pas `/etc/profile.d/`).
- Nouvelle étape "Installation Playwright + Chromium" dans la preview de `recreate_template_iter()`.
- **Action utilisateur requise** : aller dans Réglages → Proxmox → Recréer le template (VMID 299). Opération plus longue (~5 min, ~300 Mo de dépendances).

**~03h45 — Onglet Mélangeur dans la page Traitements**

- La palette de déploiement reste minimale : nb entrées (1–8), 3 noms de shm sortie (PGM/Clean/PVW, optionnels — auto-générés à partir du hostname), dissolve par défaut, URL overlay HTML, checkbox d'activation. Plus de textarea pour les inputs : le câblage se fait à chaud.
- Nouvel onglet **Mélangeur** dans `templates/multiview.html` (page Traitements) avec `static/mixer.js` :
  - Colonne de gauche : liste des mélangeurs déployés (filtre `deploy_config.type == "mixer"`).
  - Éditeur droit (mélangeur sélectionné) :
    - Ligne **PGM** (libellé rouge), 1 bouton par slot, bouton plein rouge sur l'entrée à l'antenne. Clic = cut direct (`POST /pgm`).
    - Ligne **PVW** (libellé vert), même structure, bouton vert sur l'entrée en preview. Clic = `POST /pvw`.
    - Sous chaque bouton, en mono : le shm câblé (ou "(non câblé)").
    - Bloc TAKE : gros boutons `CUT` (gris, `take {duration_ms:0}`) et `DISSOLVE` (bleu, `take {duration_ms:input}`) + champ de durée pré-rempli avec le défaut du déploiement.
    - Bloc **Incrustation HTML** : checkbox `overlay_enabled` (POST instantané) + champ URL + bouton "Appliquer URL".
    - Bloc **Câblage** : un input texte par slot + bouton "Câbler" → `POST /input {idx, shm}`.
  - Polling de `/state` toutes les 2 s pour refléter les changements externes.

**~03h55 — 3 sorties shm distinctes : PGM / Clean / PVW**

- Le mélangeur expose désormais 3 buffers shm en parallèle :
  - `SHM_OUT_PGM` — vidéo composite (active input ± dissolve) + **overlay HTML** appliqué.
  - `SHM_OUT_CLEAN` — même vidéo que PGM, **sans** l'overlay (pour archive / encodeur clean).
  - `SHM_OUT_PVW` — passthrough de l'entrée en preview (clean également).
- Renommage du param `shm_out` → `shm_out_pgm` au passage. Naming par défaut basé sur le hostname (`<hn>_pgm`, `<hn>_clean`, `<hn>_pvw`) — évite les collisions entre plusieurs mélangeurs sur le même nœud.
- Boucle principale : la frame "clean" est calculée d'abord en YUV ; si overlay actif, roundtrip RGB pour produire la frame PGM ; PVW est calculée indépendamment depuis l'entrée preview. Les 3 sont écrites avec les mêmes `frame_index`/`time_ns()`.
- La card container affiche `shm = "PGM:… · clean:… · PVW:…"` pour la lisibilité.

## Sources / Destinations + générateur par slot — 2026-05-28

**~02h00 — Refonte du menu principal**

- Réorganisation de la nav : `Accueil · Containers · Sources · Traitements · Destinations · Projets · Aide`.
- "Receivers NMOS" devient **Sources** (libellé seul, route `/nmos/receivers` inchangée), "Multiview" devient **Traitements** (route `/multiview` inchangée). Les identifiants techniques (`multiview_free`, permission `multiview.edit`, classes CSS) sont conservés.
- Nouvelle page **Destinations** (`/destinations`) miroir de Sources, côté senders NMOS.

**~02h30 — Page Destinations**

- Nouvelle route `GET /api/nmos/senders_detail` : agrège par container les senders depuis `nmos._senders` + state IS-05 (`_send_state` → multicast/port, essence, audio_idx, subscription receiver_id) + fetch live des fps sur `:8080` du container.
- Template `destinations.html` : carte par container, table par sender (essence vidéo/audio #idx, état active/idle, fps, multicast:port, receiver subscribed). Refresh 5s.

**~02h50 — Adresses MXL + groupement par ensemble**

- Sources et Destinations affichent maintenant les chemins `/dev/shm/...` (MXL) en plus des adresses multicast 2110. Sens des flèches cohérent : Sources `↘ réseau → MXL`, Destinations `MXL → ↗ réseau`.
- Receivers : `shm_path` dérivé de `{hostname}_{idx}` (vidéo) ou `{hostname}_audio_{idx}` (audio).
- Senders : `shm_path` dérivé de `params.video.shm_name` ou `params.audios[i].shm_name` depuis `deploy_config`.
- Regroupement visuel en **Ensembles** (1 vidéo + N audio) : pour les receivers, vidéo[i] est appairé avec audio[i] (audio surnuméraire rattaché au dernier ensemble) ; pour les senders, un seul ensemble par container puisqu'il y a au max 1 vidéo + 2 audio.
- Distinction essence : fond bleu + bordure gauche bleue (vidéo), fond vert + bordure gauche verte (audio), tags `VIDÉO #i` / `AUDIO #i`.
- Fix collision audio/vidéo : les métriques `/8080` sont désormais indexées par `(essence, idx)` au lieu de `idx` seul.

**~03h00 — Générateur par slot (refonte importante)**

- Anciennement : un unique flag `SIMULATION` activait testsrc/sine sur **tous** les pipelines simultanément, et le masque 8 canaux audio s'appliquait globalement. Confus quand on avait plusieurs receivers audio dans le même container.
- Nouveau schéma de params (avec master switch + override par slot) :
  - `sim_master` (bool) — master switch global. Si OFF, tous les slots sont forcés en NMOS.
  - `sim_video_slots: [{enabled, pattern}, …]` (longueur = `video_count`)
  - `sim_audio_slots: [{enabled, freq, level_db, active[8], rupted[8]}, …]` (longueur = `audio_count`)
- Script `receiver_nmos.py` : décision NMOS vs générateur faite par `_video_slot(idx)["enabled"]` / `_audio_slot(idx)["enabled"]` dans `_ffmpeg_video`, `_ffmpeg_audio`, `pipeline_video` (attente SDP) et `pipeline_audio`. Plus de `SIMULATION` global.
- UI palette Deploy : master switch suivi de deux tableaux dynamiques (un par slot vidéo, un par slot audio) — checkbox "générateur" + pattern pour vidéo, checkbox + freq + niveau dBFS + masque 8 canaux actifs + masque 8 canaux ruptés pour audio. Les tableaux se re-rendent au changement de `video_count`/`audio_count` en préservant les saisies existantes.
- **Backward compat** : `app/scripts.py` accepte encore les anciens params globaux (`simulation`, `sim_video_pattern`, `sim_audio_freq/level_db/active/rupted`) et les explose en slots si les nouvelles listes ne sont pas fournies. Les containers déjà déployés restent fonctionnels jusqu'au prochain redéploiement.

**~03h10 — Icône générateur sur Sources**

- Petite icône ⚙ orange à gauche du badge d'état pour chaque slot dont le générateur est activé. Tooltip explicite : "Générateur de mire/sine local activé — ce receiver n'attend pas de sender NMOS". Permet de ne pas se faire surprendre quand un flux a l'air "actif" mais sort en réalité du générateur local.
- `/api/nmos/receivers_detail` expose `simulated: bool` par receiver (dérivé de `deploy_config.params.sim_master AND sim_(video|audio)_slots[idx].enabled`).

**~03h15 — Format audio à la place du fps**

- Pour les lignes audio (Sources et Destinations), la colonne fps (qui n'a pas de sens en audio : ce sont des chunks/s) est remplacée par `48K / L24` (fréquence d'échantillonnage / quantification).
- L'affichage reflète la **réalité du flux**, comme les fps en vidéo :
  - `48K / L24` en vert quand le `/metrics` du container remonte des chunks (fps > 0 = audio qui coule réellement).
  - `— / —` en rouge quand rien n'arrive (silence côté pipeline).
- Format codé en dur car le 2110-30 est fixé à L24/48k/8ch dans `receiver_nmos.py` et `worker_2110_sender.py`. À ouvrir si on veut supporter L16 ou d'autres sample rates plus tard.

## Refonte palette de déploiement + renommage projet — 2026-05-27

- **Renommage projet** : "Orchestrateur MXL" → **BobiBox MXL** (BBM). Logo/titre dans la nav mis à jour. La technologie MXL (pipeline vidéo ST 2110) reste inchangée.
- **DB renommée** : `orchestrateur.db` → `db_bobiboxmxl.db`.
- **Aide** : ajout de la rubrique "Obtenir de l'aide" (société Bobi, contact@bob-i.tv).
- **Palette de déploiement repensée** : script en premier, ressources LXC (cores/RAM/pinning) repliées en bas. Sélecteur de format vidéo unique (W/H/fps/balayage), configurable dans Réglages → Vidéo. Présélection automatique à l'ouverture d'un container déjà configuré.
- **Noms des types de scripts** harmonisés : Receiver 2110 · Multiviewer · Encoder / Streamer · Sender 2110 (palette, badges tuiles, chips de filtre).
- **Statut batch** : "script_stopped" sans config affiché "Container prêt". Étapes intermédiaires plus détaillées (Initialisation → Clonage → Configuration → Démarrage → Container prêt).
- Onglet **Surveillance** défini par défaut dans la page Containers.

## Robustesse création de containers — 2026-05-27

- **Visibilité des erreurs batch** : les lignes en échec sont maintenant mises en évidence (fond rouge, texte rouge). La page scroll automatiquement vers le tableau dès qu'une erreur est détectée. Les premiers polls passent à 1 s (×6) avant de revenir à 3 s pour détecter les erreurs de clonage plus vite.
- **Message d'erreur Proxmox enrichi** : quand le clonage échoue avec "does not exist" (template absent), le message d'alerte indique explicitement d'aller créer le template dans Réglages → Proxmox.
- **Protection contre l'écrasement involontaire du template** : avant de recréer le template, l'outil vérifie si le VMID cible est déjà utilisé. Si c'est un container managé par notre outil → bouton désactivé, message bloquant. Si c'est un container Proxmox externe inconnu de notre DB → confirmation explicite avec nom et statut du container avant destruction.
- Suppression de la phrase redondante dans la section template de Réglages → Proxmox.

## Création de containers en masse — 2026-05-27

- La page Containers est restructurée en deux onglets principaux : **Création** et **Surveillance**.
- **Onglet Création** : formulaire avec un champ "Hostname (préfixe)" et un champ "Nombre" (1–20). Lors d'un batch, les containers sont nommés `<préfixe>-1`, `<préfixe>-2`, etc. (si N=1, le hostname est utilisé tel quel).
- Les N créations sont lancées **en parallèle** (`Promise.all`) pour minimiser le délai total.
- Une zone "Batch en cours" apparaît sous le formulaire après le lancement : table hostname / VMID / statut + log timestampé de chaque requête. Polling automatique toutes les 3 s jusqu'à ce que tous les containers soient `running`.
- **Onglet Surveillance** : reprend l'ancienne vue "Containers surveillés" (grille de cards + palette de déploiement). Les logs système restent toujours visibles en bas de page, accessibles depuis les deux onglets.

## Routage MXL via Ember+ — 2026-05-27

Journée complète de développement et debug sur le routing Ember+.

**13h13 — Première implémentation de la matrice de routage**

- Nouveau sous-arbre `[2] routing` dans le provider Ember+.
- `_gather_routing_sources()` : sources = shm vidéo de chaque receiver (NMOS ou simulation) + outputs des multiviews.
- `_gather_routing_targets()` : destinations = fenêtres multiview, workers UDP, workers 2110 Sender.
- `apply_connect()` : connexion VSM → mise à jour `deploy_config` en DB → redéploiement asynchrone du container cible.
- Normalisation des chemins shm : `_shm_full()` / `_shm_bare()` — workers stockent le nom bare, multiviews le chemin complet `/dev/shm/...`.

**13h24 — Fix : matrix retournée uniquement sur GetDirectory, pas dans le push initial**

- VSM boucle sur `GetDirectory([])` si la QualifiedMatrix est dans la `RootElementCollection` du push initial → la matrix n'est envoyée qu'en réponse à `GetDirectory([2])`.

**13h28 — Arbre Node+Param navigable dans le tree browser VSM**

- Sous-arbre Node+Param complet inclus dans le push initial : sources (`[2,2]`) et destinations (`[2,3]`) avec leurs paramètres (shm, source numéro).
- VSM navigue et subscribe aux paramètres sans déconnexion.

**14h40 — Correction bug critique : GlowType_QualifiedMatrix = App 17, pas 14**

- Bug découvert grâce à la doc Lawo (`libember_slim/Source/glow.h`) : `GlowType_QualifiedMatrix = APPLICATION 17`, pas 14.
- `G_QUAL_MATRIX` était à 14 (= `GlowType_Target`) depuis le début → la matrice s'encodait comme un signal target errant → VSM déconnectait ou bouclait.
- Fix : `G_QUAL_MATRIX = 17`, `G_TARGET = 14`. La matrix est maintenant correctement encodée.
- Restructuration des chemins : sources = `[2,2]`, targets = `[2,3]`, matrix réservée à `[2,1]`.

**État final :** le nœud `routing` est visible et navigable dans le tree browser VSM. Chaque destination expose un paramètre "source (numéro)" modifiable depuis VSM (0 = déconnecté, N = numéro de la source). La QualifiedMatrix `[App 17]` est disponible sur `GetDirectory([2])` pour les consumers routing natifs.

## Niveau dBFS configurable du sine audio simulé — 2026-05-26 23:25

- Nouveau champ "Niveau sine audio (dBFS)" dans la section simulation du receiver. Par défaut **-18 dBFS** (EBU alignment level standard).
- Avant : la sine était à 0 dBFS (full scale) → ça saturait les meters en permanence. Maintenant, le sine descend à -18 dBFS → utile pour tester un signal de référence broadcast et voir si les meters affichent correctement le niveau attendu.
- Range : -60 à 0 dBFS. Côté ffmpeg, l'expression devient `<amp>*sin(2*PI*freq*t)` où `amp = 10^(level_db/20)` (ex -18 → 0.125893).

## Numéro de canal sous chaque meter + rupture per-canal — 2026-05-26 23:00

- **Numéro de canal** : sous chaque barre du peak meter, label "1" à "8" pour identifier facilement les canaux. Bande de 12 px réservée en bas du meter (les barres et graduations s'affichent au-dessus).
- **Option "Rupté" par canal** dans le générateur audio simulation : 8 nouvelles checkboxes (en plus des 8 d'activation). Cochée → le canal coupe 100 ms toutes les 1 s (90% ON, 10% OFF). Utile pour identifier visuellement quel canal est quel sur les peak meters d'un multiview (chaque canal rupté flash visiblement).
- Côté ffmpeg : `aevalsrc` avec expression conditionnelle par canal `if(lt(mod(t\,1)\,0.9), sin(...), 0)` pour les canaux ruptés. Virgules échappées (`\,`) car elles servent de séparateurs d'arguments dans l'expression ffmpeg.
- Trois états par canal : silence (case active décochée) / sine continu / sine rupté.

## Graduations meters étendues + sélection canaux audio simulés — 2026-05-26 22:30

- **Graduations meters** : dBFS = ticks à 0/-3/-6/-9/-12/-18/-20/-30/-40/-50, EBU PPM = +12/+9/+6/+3/0/-3/-6/-9/-12. Petite ligne par tick + label texte filtré (apparaît si espace ≥ 9 px), donc les meters compacts gardent les valeurs clés (0/-12/-30/-60) et les meters hauts affichent toutes les marques.
- **Choix canaux audio sim** : 8 checkboxes dans la section simulation du receiver. Cochée = sine 1 kHz sur ce canal, décochée = silence. Le stream RTP reste L24/48k/8ch (canaux désactivés émettent du silence dans le packet).
- **Backend** : ffmpeg utilise maintenant `aevalsrc=exprs=…` avec une expression par canal (`sin(2*PI*freq*t)` ou `0`). Permet la sélection per-canal sans changer le format stream.

## Fix : config peak meters perdue après deploy multiview — 2026-05-26 22:00

- Symptôme : configurer les meters dans le Layout Editor, déployer, revenir plus tard → meters désactivés.
- Cause : `deployerEditor()` dans `multiview.js` reconstruit `flux_config` champ-par-champ (au lieu de tout copier) — les 5 nouveaux champs meter (`meter_channels`, `meter_position`, `meter_inside`, `meter_opacity`, `meter_scale`) étaient strippés à l'envoi vers le backend.
- Fix : ajout explicite des 5 champs dans le `.map()`.
- Action requise : Ctrl+R navigateur pour charger le nouveau JS, puis reconfigurer + redéployer.

## Fix CPU 99% sur receivers simu + multiview source path cassé — 2026-05-26 21:30

- **CPU des générateurs en simulation** : `ffmpeg -f lavfi` (sine/testsrc) tournait sans `-re` → générait son flux à plein régime, ~187% CPU pour le sine 8ch, ~75% pour le pipeline Python derrière. Ajout de `-re` aux deux ffmpeg lavfi dans `receiver_nmos.py` — résout à 5-10% CPU.
- **Multiview qui ne voit plus rien** : le composer utilisait directement `c.shm_out` du container source pour construire le path `/dev/shm/<shm>`. Avec le receiver unifié, `shm_out` est devenu une chaîne agrégée style `"Mire_0 · Mire_audio_0"`, créant un path invalide. Le composer splitte maintenant sur ` · ` et déplie les ranges `_0..N-1`, filtre les shms audio (un multiview ne consomme que de la vidéo). Lookup container ↔ shm flexible (matche les shms individuels avec un container agrégé).
- **Action requise** : redéployer les receivers simu existants pour appliquer le `-re`, et reconfigurer les multiviews dont les fenêtres pointent vers des paths agrégés cassés.

## Peak meters audio sur les multiviews — 2026-05-26 21:00

- Chaque fenêtre du multiview peut afficher un peak meter audio (2 / 4 / 6 / 8 canaux). Configuration par fenêtre dans le Layout Editor (panneau de la fenêtre sélectionnée).
- Paramètres : nombre de canaux, position (gauche / droite), placement (overlay dans l'image avec opacité 10-100%, ou hors image en réduisant la zone vidéo), graduation (dBFS 0/-60 ou EBU PPM ±12).
- Source audio dérivée automatiquement du shm vidéo : `<base>_<idx>` → `<base>_audio_<idx>`. Si l'audio est absent, le meter affiche les barres à -60 dB (cohérent, pas de crash).
- Ballistique broadcast standard : peak instantané + peak hold avec decay 20 dB/s.
- Couleurs broadcast : vert jusqu'à -20 dBFS (+6 EBU), jaune jusqu'à -6 dBFS (+15 EBU), rouge ensuite. Ligne blanche fine = peak hold.
- Preview live dans le Layout Editor (canvas JS) — pas de vrai signal mais représentation visuelle de la position/dimension.
- Re-rendu par frame (25 fps), parallèle aux layers static (bordures, labels) et dynamic (tally/labels protocole).

## Unification receiver / sender (vidéo + audio) + simulation toggle — 2026-05-26 20:30

**Receivers**

- Un seul type `receiver` au lieu de `receiver` + `audio_receiver`. Paramètres : `video_count` (N receivers vidéo NMOS) + `audio_count` (M receivers audio NMOS). Chacun peut être 0
- Un même container peut donc recevoir vidéo ET audio en parallèle. Le script lance N threads vidéo + M threads audio
- **Simulation = case à cocher** indépendante des réglages NMOS. Cochée → chaque thread utilise `testsrc2` (vidéo) ou `sine` (audio) au lieu d'attendre le SDP. Décochée → comportement NMOS normal. Les counts/NMOS state sont préservés à travers les toggles
- Pattern vidéo et fréquence sine audio configurables (testsrc/testsrc2/smptebars/rgbtestsrc, fréquence en Hz)
- Shms : vidéo dans `<hostname>_<idx>`, audio dans `<hostname>_audio_<idx>` (pas de collision)

**Senders**

- Un seul type `worker_2110_sender` au lieu de `worker_2110_sender` + `worker_2110_audio_sender`. Paramètres : `video: {…} | null` (0 ou 1 flux vidéo) + `audios: [...]` (max 2 flux audio)
- Le script lance 1 ffmpeg vidéo (si configuré) + 0/1/2 ffmpegs audio en parallèle
- Auto-allocation distincte des multicast : `239.10.10.X:5000` pour vidéo, `239.10.20.X:5004` puis `:5006` pour les audios
- SDP par flux dans `/tmp/nmos_sender_video.sdp` et `/tmp/nmos_sender_audio_<idx>.sdp`, agrégés dans `/metrics`

**Contrat agent /nmos/subscribe étendu**

- Body inclut maintenant `essence: "video"|"audio"`. Le SDP est écrit dans `/tmp/nmos_recv_v_<idx>.sdp` ou `/tmp/nmos_recv_a_<idx>.sdp` selon essence
- Évite la collision quand un container reçoit les deux essences en parallèle (mêmes index 0..N-1 par essence)

**DB**

- Nouvelle colonne `nmos_audio_count` ; `nmos_receivers_count` reste = compteur vidéo (pas de rename, simple ajout)

**Suppressions**

- Templates `audio_receiver_nmos.py` et `worker_2110_audio_sender.py` supprimés (fonctions absorbées dans les templates unifiés)
- Types `audio_receiver` et `worker_2110_audio_sender` retirés du select UI
- Code dédié dans `scripts.py`, `deploy.py`, `nmos.py` supprimé
- Pas de migration auto des anciens deploys (l'utilisateur recrée les containers)

## SSH : détection d'échec réactive (plus de polling) — 2026-05-26 22:30

- Retrait du health check SSH proactif au démarrage et de la card "SSH host Proxmox" sur la home
- `ssh_run` détecte désormais rc=255 (SSH process error, vs erreur du remote command) et émet une alerte `error` automatiquement
- Dedup 60 s sur message identique : si SSH casse et que plusieurs opérations échouent en cascade, une seule alerte est émise ; si le message change (nouvelle cause), alerte fraîche immédiate
- L'orchestrateur ne SSH plus que pour des opérations utiles — pas de polling périodique
- Aide mise à jour : pré-requis orchestrateur + section dépannage refocalisée sur l'alerte plutôt que la card

## Journal de changements dynamique — 2026-05-26 22:10

- Nouveau fichier `CHANGELOG.md` à la racine du projet (ce fichier)
- Endpoint `GET /api/changelog` lit le markdown et le rend en HTML via la lib Python `markdown`
- Page Aide → section "Mises à jour" en fin de sommaire, contenu chargé dynamiquement
- Convention : nouvelles entrées en tête, format `## Titre — YYYY-MM-DD HH:MM` + liste à puces
- Plus besoin d'éditer le template HTML pour ajouter des entrées de changelog

## Health check SSH proactif — 2026-05-26 21:50

- **Card "SSH host Proxmox"** ajoutée en tête de la home (verte ✓ OK / rouge ✕ CASSÉ + message d'erreur)
- Vérification au démarrage de Flask : si SSH cassé, alerte `error` + log immédiat
- Cache 30 s côté serveur pour ne pas surcharger SSH avec le refresh dashboard 2 s
- `ssh_run` a maintenant un `ConnectTimeout=5` (évite les blocages réseau)
- Aide → dépannage : nouvelle section "Card SSH host Proxmox rouge" avec checklist

## Page Aide — 2026-05-26 21:30

- Nouvelle route `/aide` + lien dans la nav du haut
- 13 chapitres : pré-requis, workflows par type, NMOS / SR-IOV / PTP / CPU pinning expliqués, dépannage, lexique
- Sommaire latéral sticky avec ancres internes

## Home dashboard data-driven — 2026-05-26 21:00

- Suppression des raccourcis statiques
- Endpoint unique `/api/home/summary` agrège PTP, NMOS counts (vidéo / audio), containers, flux MXL, mDNS, SR-IOV, alertes
- 4 sections sur la home : Synchronisation, Flux NMOS, Pipeline MXL, Système
- Refresh 2 s + timestamp de dernière mise à jour

## ST 2110-30 (audio L24/48k/8ch) — 2026-05-26 20:00

- Nouveau type `audio_receiver` (multi-pipeline) + `worker_2110_audio_sender`
- Format shm audio dédié : chunks 1 ms (1152 B), ring 100 ms, naming `<hostname>_audio_<idx>`
- NMOS provider étendu : Source / Flow audio (format=audio, media_type=audio/L24), receivers avec `fmt='audio'`
- Allocation multicast distincte de la vidéo : audio = `239.10.20.X:5004`
- SDP injection PTP commune aux senders vidéo et audio
- ST 2110-40 (ancillary) explicitement skip : ffmpeg ne supporte pas RFC 8331 nativement

## PTP (SMPTE 2059-2 / IEEE 1588) — 2026-05-26 19:30

- Nouveau module `ptp.py` : install `linuxptp`, deploy unités systemd `mxl-ptp4l` + `mxl-phc2sys`, status via `pmc`
- Architecture host : `ptp4l` sur l'host, containers héritent CLOCK_REALTIME (ffmpeg → timestamps PTP-alignés)
- SDP injection 2110 : `a=ts-refclk:ptp=IEEE1588-2008:<gm-id>:<domain>` + `a=mediaclk:direct=0` ajoutés au transportfile si PTP locked
- Onglet PTP dans Réglages : settings (enabled / ifname / domain / hw_ts), boutons install / apply, état live (port_state, offset, mean path delay, GM ID, badge LOCKED)
- Limite documentée : ffmpeg utilise CLOCK_REALTIME (pas PHC direct) → sub-µs avec HW timestamping, pas hard-real-time

## CPU pinning + édition cores/RAM — 2026-05-26 18:30

- Édition cores / RAM live d'un container existant via palette "Configurer" (stop → reconfigure → start)
- Épinglage CPU via `lxc.cgroup2.cpuset.cpus` en raw .conf (format Linux cpuset : `4,5,6` ou `4-6`)
- Onglet **CPU** dans Réglages : carte des cores (libre / épinglé / conflit orange) + table containers épinglés
- Affichage `⚲ CPU pinning : 1,2` sur les cards containers
- Choix de design : épinglage souple, pas `isolcpus` strict
- Validé bout-en-bout sur 261 (cgroup effective = 1-2 confirmé)

## SR-IOV + nouveaux onglets réseau — 2026-05-26 17:30

- Plumbing SR-IOV complet (testable à l'arrivée de la ConnectX-4) :
    - Settings `nmos_2110_enabled`, `nmos_2110_pf`, `nmos_2110_vf_count`
    - Helpers SSH : `ensure_sriov_pool`, `list_vfs`, `attach_vf_to_lxc`, `allocate_free_vf`, `reconcile / fix_vf_assignments`
    - Container avec checkbox "2110 NIC" → attache une VF (eth0 + nmos0)
- Onglet **Ethernet** : table live des NICs Proxmox avec MAC / IPv4 / BW ↓↑ (delta 2 s)
- Onglet **Réseau 2110** : mini-table filtrée PF+VFs + bloc SR-IOV (init / reconcile / fix)
- Affichage CPU% + barre colorée sur les cards containers
- Page **Receivers NMOS** dédiée listant pipelines + subscription state

## Bind mount /dev/shm corrigé — 2026-05-26 16:30

- Diagnostic : la recréation des containers avait perdu le bind `/dev/shm`, le pipeline MXL ne fonctionnait plus entre containers
- `pct set --mp0 /dev/shm` ne marche pas (Proxmox veut un block device) → édition raw `lxc.mount.entry` via SSH
- Helper `ensure_shm_bind` appelé automatiquement à la création + à la recréation du template
- Wipe des fichiers fantômes UID 100000 (anciens containers unprivileged) pour éviter les conflits de permission
- Template recreate : streaming live de chaque étape (au lieu d'un dump à la fin)

## NMOS Phase 2 : worker_2110_sender + IS-05 bulk + mDNS — 2026-05-26 15:30

- Nouveau type `worker_2110_sender` : lit shm, ffmpeg yuv422p10le → RTP raw RFC 4175
- NMOS provider étendu : Source / Flow (2110-20) / Sender exposés en IS-04 ; routes IS-05 single/senders (constraints / staged / active / transportfile)
- `/transportfile` fetch live le SDP depuis le container (champ `sdp` du `/metrics`)
- IS-05 bulk endpoints : `POST /bulk/{receivers,senders}` acceptent un array `[{id, params}]`
- mDNS (zeroconf) : annonce `_nmos-node._tcp.local.` avec TXT records IS-04
- Multi-pipeline receiver (`receiver_nmos.py`) : N threads parallèles, shm `<hostname>_<idx>`, /metrics liste `receivers[]`
- Allocation multicast vidéo : `239.10.10.<vmid%254+1>:5000`
