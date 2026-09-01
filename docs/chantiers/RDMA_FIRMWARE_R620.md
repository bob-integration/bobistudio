# Flash du firmware Broadcom de r620-1 (nœud 42) — procédure

**Date** : 2026-08-07 · **Opérateur** : à faire à la main · **Machine** : r620-1, `192.168.1.3`

## Pourquoi

r620-1 n'établit **aucun** lien RDMA depuis son enrôlement (lien #92 créé le 2026-08-03, jamais
passé `running`). L'initiateur échoue sur `ibv_create_cq: Input/output error (5)` et le noyau
répète, sur les deux ports :

```
bnxt_en 0000:41:00.0: QPLIB: cmdq[0xd3a]=0x9 status 0x3
bnxt_en 0000:41:00.0 rocep65s0f0: Failed to create HW CQ
```

C'est le **chip** qui refuse la commande, pas Linux : la carte annonce `max_cq = 65535` et n'arrive
pas à en créer une seule, donc ce n'est pas un épuisement de ressources.

La comparaison avec r620-2, qui réplique sans problème, ne laisse qu'**une seule variable** :

| | r620-1 (KO) | r620-2 (OK) |
|---|---|---|
| **firmware** | **21.85.21.91** (`fw.mgmt` 218.0.219.13) | **23.11.16.22** (`fw.mgmt` 231.0.153.0) |
| carte | BCM957414, asic 16D7 rev **B1**, sous-système `14e4:4141` | **identique** |
| noyau / pilotes | 6.12.96+deb13-amd64, `bnxt_en` + `bnxt_re` | **identiques** |
| port | ACTIVE, LinkUp, 25 Gb/s, RoCE v1+v2, MTU 1500 | **identique** |
| SR-IOV, memlock | aucun VF, 4 Go | **identiques** |

Les deux cartes étant strictement le même matériel, la cible est le firmware de r620-2 :
**23.11.16.22**.

## Impact et filet

- Le plan de **contrôle** de r620-1 passe par `eno1`/`eno4` (1 GbE), pas par la carte 25 G. Même
  une carte briquée ne coupe pas l'accès au nœud — c'est ce qui rend l'opération sûre.
- Un seul conteneur tourne sur le nœud : `Monitoring-KHamidou` (vmid 904). Le redémarrage coupe
  le monitoring de cet utilisateur, rien d'autre.
- Les deux liens RDMA en attente (#92, #113) ne transportent rien aujourd'hui : ils échouent déjà.

## Étape 0 — le coup gratuit : cycle d'alimentation À FROID

À faire avant le flash, ça prend cinq minutes. Un `reboot` **ne suffit pas** : il ne réinitialise
pas le chip Broadcom, qui garde son état entre deux démarrages à chaud. Il faut une coupure
d'alimentation réelle — via iDRAC : *Power → Power Cycle System (cold boot)*, ou physiquement.

Peu probable que ça règle le problème (l'erreur est présente depuis l'enrôlement, à travers
plusieurs redémarrages), mais c'est sans risque et ça élimine l'hypothèse « chip dans un état
bancal » avant de toucher à la NVRAM. Vérifier ensuite avec le script de contrôle plus bas : si
`Failed to create HW CQ` a disparu, s'arrêter là.

## Étape 1 — récupérer le paquet firmware

Version cible **23.11.16.22**, exactement celle de r620-2.

Source la plus directe — Dell, identifiant de pilote **2WPNV**, fichier
`Network_Firmware_2WPNV_LN64_23.11.16.22.BIN` :
<https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=2wpnv>

C'est cohérent avec la carte : sa VPD porte `MN 1028` (Dell) et `DSV1028VPDR`, et r620-2 tourne
précisément sur cette version. À défaut, le même paquet existe chez Broadcom (bundle NetXtreme-E
pour la famille BCM5741X) et chez HPE.

Copier le fichier sur le nœud, par exemple dans `/root/`.

## Étape 2 — flasher

### Voie A (à essayer d'abord) — l'exécutable Dell

```bash
chmod +x Network_Firmware_2WPNV_LN64_23.11.16.22.BIN
./Network_Firmware_2WPNV_LN64_23.11.16.22.BIN            # vérifie la compatibilité, puis flashe
```

S'il refuse au motif que la carte n'est pas reconnue comme Dell (le sous-système annonce Broadcom
`14e4:4141`, pas un identifiant Dell), passer à la voie B plutôt que de forcer.

### Voie B — extraire le `.pkg` et passer par devlink

`devlink` est disponible et fonctionnel sur le nœud (vérifié : `devlink dev info` répond).
Attention, `ethtool -f` n'est **pas** compilé dans le `ethtool` de ce système — c'est bien
`devlink` qu'il faut utiliser.

```bash
./Network_Firmware_2WPNV_LN64_23.11.16.22.BIN --extract /root/fw
find /root/fw -name '*.pkg'                     # typiquement BCM5741x…pkg

cp /root/fw/<le fichier>.pkg /lib/firmware/bnxt_57414.pkg
devlink dev flash pci/0000:41:00.0 file bnxt_57414.pkg
```

Deux points à ne pas manquer :

- le chemin passé à `devlink` est **relatif à `/lib/firmware`**, pas absolu ;
- les deux ports partagent **une seule NVRAM** : on flashe uniquement la fonction `.0`
  (`0000:41:00.0`). Refaire l'opération sur `.1` est inutile.

Ne pas interrompre : l'écriture dure une à deux minutes.

## Étape 3 — cycle d'alimentation à froid, obligatoire

Le nouveau firmware ne prend effet qu'après une coupure d'alimentation réelle. Un `reboot` laisse
tourner l'ancien. Même manœuvre qu'à l'étape 0 (iDRAC *Power Cycle System*).

## Étape 4 — vérification

Sur le nœud :

```bash
devlink dev info pci/0000:41:00.0 | grep -A3 running     # doit afficher fw 23.11.16.22
dmesg | grep -c 'Failed to create HW CQ'                 # doit rester à 0 après plusieurs minutes
```

Attendu : `fw 23.11.16.22`, `fw.mgmt 231.0.153.0`, plus aucune trace de `Failed to create HW CQ`,
et les liens #92 et #113 qui passent `pending → running` d'eux-mêmes — la réconciliation les
retente toutes les ~63 s, il n'y a rien à relancer à la main.

Depuis le contrôleur, le contrôle complet :

```bash
./venv/bin/python - <<'PY'
import sys, sqlite3; sys.path.insert(0, "/opt/bobistudio")
from app.database import db_get_nodes
from app.node_driver import host_exec
n = [x for x in db_get_nodes() if x["name"] == "r620-1"][0]
r = host_exec(n, "devlink dev info pci/0000:41:00.0 | grep -A4 running; "
                 "echo '-- erreurs CQ --'; dmesg | grep -c 'Failed to create HW CQ'", timeout=30)
print(r[1] + r[2])
db = sqlite3.connect("/opt/bobistudio/db_bobistudio.db")
print("liens vers r620-1 :",
      list(db.execute("select id, status from rdma_links where dst_node_id = 42")))
PY
```

## Si le flash ne change rien

Alors l'hypothèse firmware tombe et il reste deux pistes, dans cet ordre :

1. **Provisionnement RoCE en NVRAM.** Sur ces cartes, le pool de ressources RoCE par fonction est
   configuré dans la NVRAM. S'il est à zéro sur r620-1, le pilote enregistre bien le périphérique
   IB (ce qu'on observe) mais aucune CQ matérielle ne peut être allouée — exactement le symptôme.
   Se compare et se règle avec `bnxtnvm` / `niccli` (`nvm get`/`nvm set`), à confronter aux valeurs
   de r620-2.
2. **Carte défectueuse.** Le test décisif est de permuter les cartes entre r620-1 et r620-2 : si la
   panne suit la carte, c'est le matériel ; si elle reste sur le châssis, c'est la plateforme.
