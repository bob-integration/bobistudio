***REMOVED*** ST-2022-7 : un drapeau global sur une carte à quatre ports

Relevé le 2026-09-11, à partir d'une question d'exploitation : « une carte comme celle d'Horace a
quatre ports. On peut très bien déclarer une paire en 2022-7 et deux autres sans. À quoi sert le
réglage dans le moteur ? Ça peut laisser croire que tout est 2022-7. »

L'intuition est juste, et le défaut est plus grave que l'affichage.

***REMOVED******REMOVED*** Ce que le drapeau fait réellement

`params.smpte_2022_7` est **un booléen unique par moteur**. Il est lu à deux endroits, ni l'un ni
l'autre par flux :

- `plugins/2110_io/hooks.py` — au déploiement, il déclenche l'allocation d'une seconde adresse
  multicast pour **chaque** slot TX, dans une boucle sur tous les slots ;
- `services/nmos/__init__.py` — il fait exposer **tous** les récepteurs avec deux jeux de
  paramètres de transport en IS-05.

L'appariement, lui, est **par interface** : `node_interfaces.pair_role` (red/blue) et
`pair_group`. La granularité des deux mécanismes ne coïncide pas.

***REMOVED******REMOVED*** Le repli qui fabrique une fausse redondance

`app/allocations.py:_egress_iface(..., leg=1)` cherche l'interface de même `pair_group` et de
`pair_role` opposé. Quand elle n'existe pas :

```python
return (row0["ifname"], row0.get("media_network_id"))   ***REMOVED*** la MÊME interface que le leg0
```

C'est documenté comme un repli délibéré. Mais sur un port non apparié, avec le drapeau armé, le
résultat est un flux dont les deux jambes sortent **par le même port physique**, sur deux adresses
multicast distinctes. Ce n'est pas du ST-2022-7 : c'est le même flux émis deux fois.

Les conséquences se cumulent, et aucune ne se voit :

1. **Aucune redondance.** La panne d'un port emporte les deux jambes — c'est précisément ce contre
   quoi le 2022-7 protège.
2. **Le double de bande passante** sur ce port, et une session TX de plus.
3. **Une adresse multicast consommée** pour rien.
4. **Et surtout, NMOS l'annonce comme redondant.** Un contrôleur externe voit deux jambes, croit
   le flux protégé, et n'a aucun moyen de constater qu'elles empruntent le même câble.

C'est un échec silencieux au sens strict : la fonction est annoncée, l'annonce est crue, et rien
ne dit qu'elle est vide.

***REMOVED******REMOVED*** Ce qu'il faudrait

**Le principe** : ne jamais fabriquer une seconde jambe qui ne protège de rien. Mieux vaut un flux
mono-chemin honnêtement déclaré qu'un faux double-chemin.

Trois gestes, par ordre de coût :

1. **Refuser le repli.** Si le drapeau est armé et que le port du leg0 n'a pas de paire déclarée,
   ne pas allouer de leg1 pour ce slot, et ne pas l'annoncer sur deux jambes en IS-05. Le flux
   reste mono-chemin, ce qui est la vérité.
2. **Le dire.** Une alerte au déploiement — « slot N : 2022-7 demandé, port `ensXfY` sans paire
   déclarée, jambe unique » — plutôt qu'un silence.
3. **Passer le réglage par paire, pas par moteur.** C'est la vraie correction : la redondance est
   une propriété d'un chemin, pas d'un équipement. Un moteur peut légitimement porter des flux
   protégés et des flux qui ne le sont pas.

⚠ Le premier geste **change une allocation existante** : des déploiements en place peuvent avoir
des adresses de leg1 allouées sur le repli. Le correctif doit donc être arbitré, et sans doute
accompagné d'un inventaire des slots concernés avant application.

***REMOVED******REMOVED*** Ce qui a déjà été fait le même jour

`docker_driver.derive_config_moteur()` signale désormais qu'une configuration réseau enregistrée
n'est **pas appliquée** par le moteur en marche — l'autre moitié de la même question
d'exploitation. Il pourrait porter ce diagnostic-ci aussi : « paire red/blue déclarée, double
chemin non armé », et son symétrique.

---

***REMOVED*** Le retirer, et suivre les interfaces — ce que ça demande

Proposition d'exploitation : supprimer le réglage du moteur et **dériver la redondance de la
configuration des interfaces**. C'est la bonne conception, pour une raison simple : **déclarer une
paire red/blue n'est pas un accident**. Personne ne pose `pair_role` par distraction — c'est déjà
l'expression de l'intention. Un second drapeau ne sert qu'à laisser deux vérités diverger.

***REMOVED******REMOVED*** C'est plus petit qu'il n'y paraît

La granularité par flux **existe déjà** dans le modèle :

| site | forme actuelle |
|---|---|
| `nmos/__init__.py:1308, 1395, 1438` (TX vidéo, audio, ANC) | `tslot.get("smpte_2022_7") or dc_params.get("smpte_2022_7")` |
| `nmos/__init__.py:1230, 1355` (par flux) | `v.get("smpte_2022_7")`, `a.get("smpte_2022_7")` |
| `nmos/__init__.py:1106` (RX) | `bool(dc_params.get("smpte_2022_7"))` — **le seul purement global** |
| `plugins/2110_io/hooks.py:145` | `if params.get("smpte_2022_7")` — global |

Chaque slot TX peut donc déjà porter sa propre valeur ; le drapeau du moteur n'en est que le
**défaut**. La bascule consiste à remplacer ce défaut par une dérivation, pas à créer une
granularité.

***REMOVED******REMOVED*** La forme du changement

1. **Un helper unique**, à côté de `_egress_iface` : `slot_redondant(node_id, params, slot_i)`,
   vrai si et seulement si `_egress_iface(leg=1)` rend une interface **différente** de celle du
   leg 0. C'est exactement la définition d'une paire déclarée, et ça règle du même coup le repli
   qui fabriquait de la fausse redondance.
2. **Remplacer les quatre `or dc_params.get("smpte_2022_7")`** par ce helper.
3. **Côté réception**, dériver de l'interface d'entrée du récepteur (`rx_pins`) au lieu du drapeau.
4. **`hooks.py`** : `if slot_redondant(...)` à la place du booléen global.
5. **Retirer le champ du `config_schema`**, en gardant la clé en base le temps de la migration.

Compter une demi-journée avec les tests, l'essentiel du volume étant dans un seul fichier.

***REMOVED******REMOVED*** Le seul vrai point d'arbitrage : la migration

Le changement modifie l'état de déploiements existants, **dans les deux sens** :

- un moteur **drapeau armé sur des ports non appariés** perd ses jambes 1 — c'est la correction,
  mais des adresses multicast sont libérées et l'annonce NMOS change ;
- un moteur **drapeau éteint avec des interfaces appariées** gagne la redondance — donc de
  nouvelles allocations, de nouvelles sessions TX et **le double de bande passante**, sans que
  personne ne l'ait demandé ce jour-là.

Le second cas est le plus délicat : la correction ne doit pas surprendre un site en exploitation
par une hausse de débit. D'où la marche à suivre proposée : **inventorier d'abord** — pour chaque
moteur du parc, ce que le drapeau dit et ce que les interfaces disent — puis ne basculer qu'après
avoir regardé la liste des divergences. `docker_driver.derive_config_moteur()` est le bon endroit
pour porter cet inventaire : il compare déjà le déclaré à l'exécuté.

---

***REMOVED*** Fait le 2026-09-11 : le réglage est retiré, les interfaces font foi

***REMOVED******REMOVED*** Ce qui a changé

**`app/allocations.py`** porte désormais la source unique de vérité :

- `interface_appairee(node_id, ifname)` — l'interface de même `pair_group` et de `pair_role`
  opposé, ou None ;
- `slot_redondant(node_id, params, slot_i)` — vrai si le port de sortie du slot TX est appairé ;
- `rx_redondant(node_id, params, slot_i)` — même règle à l'entrée, via `rx_pins`.

**`plugins/2110_io/hooks.py`** n'alloue plus la seconde jambe sur un booléen global mais sur
l'appariement réel du port de sortie **de ce slot**. Le repli qui fabriquait deux jambes dans le
même câble ne peut plus se produire : sans paire, pas de leg1.

**`services/nmos/__init__.py`** — côté émission, les trois sites (vidéo, audio, ANC) dérivent par
slot ; côté réception, la capacité à deux jambes suit l'existence d'une paire sur le nœud.

**`plugins/2110_io/plugin.json`** — le champ disparaît du `config_schema`, de `deploy_defaults`
et de `resources.signature_keys`. Version 0.106.1.

***REMOVED******REMOVED*** Vérifié

Sur le parc (un moteur), sans redéploiement et sans aucun drapeau armé : **18 récepteurs annoncent
deux jambes**, les 238 autres une seule (ce sont d'autres types de conteneurs). Les **émetteurs
restent à une jambe**, ce qui est correct : leur seconde adresse multicast n'existera qu'après le
déploiement qui l'alloue. Aucune sur-annonce — un émetteur ne prétend pas être redondant tant que
l'adresse n'est pas là.

***REMOVED******REMOVED*** Une conséquence à connaître

`resources.signature_keys` ne contient plus `smpte_2022_7`, donc **la signature de coût ne capture
plus le surcoût de la redondance** — un flux protégé consomme pourtant deux fois la bande passante
en émission. Cette signature ne le capturait déjà que grossièrement : un booléen global ne pouvait
pas exprimer que trois flux sur dix sont protégés. Si le profil de ressources doit en tenir compte,
il faudra le dériver du nombre de slots réellement appairés, pas d'un réglage.
