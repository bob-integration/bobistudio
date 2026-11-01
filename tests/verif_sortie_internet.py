#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""« Sortie Internet » : une carte dédiée, et des conteneurs qui sortent par elle.

LE CHOIX DE CONCEPTION QUE CE BANC PROTÈGE (décidé le 2026-10-27, après mesure).
Le plan d'origine donnait au conteneur un SECOND réseau Docker portant sa route par défaut.
Mesuré sur le parc : Docker **26.1.5**, où `--gw-priority` n'existe pas (absent de l'aide de
`docker run` comme de `docker network connect`). Avec deux réseaux, il n'y aurait donc AUCUN
moyen de choisir lequel porte la route par défaut : le choix appartiendrait à libnetwork, sans
contrat, et le symptôme d'une erreur serait « le flux part par le mauvais lien », sans trace.

On fait donc l'inverse : UN seul réseau par conteneur, et c'est l'HÔTE qui route sa sortie, par
routage par source (`ip rule from <ip> lookup <table>`). Ce que ce banc tient :

1. ★ La passerelle est RELUE à chaque passage — elle vient d'un bail DHCP. La figer serait juste
   jusqu'au premier renouvellement qui change de routeur, puis fausse en silence.
2. ★ La table de routage PRINCIPALE de l'hôte n'est jamais touchée. Retirer une route par défaut
   sous un contrôleur de production pour « bien faire » est un geste dont le symptôme n'a plus
   aucun rapport avec la cause.
3. ★ Les règles EN TROP sont retirées : un conteneur repassé en sortie « nœud » dont la règle
   survivrait continuerait de sortir par Internet, sans que rien ne le dise.
4. La capacité est exposée aux MACROS (contrat CLAUDE.md) : une bascule qu'on ne peut pas
   automatiser au bon moment est une capacité morte.

Hors ligne : base jetable, aucun Docker, aucun réseau, aucun `ip`.
"""
import ast
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

# ★ TROIS CHEMINS À DÉTOURNER, ET LA SECTION 5 EN EST LA RAISON. Elle interroge le catalogue des
# macros, qui importe TOUS les fournisseurs du cœur — dont `media_registry`, qui a sa propre base
# (`db_medias.db`, dérivée de `config.DB_PATH`) et crée ses tables à l'import. Ne détourner que
# `database.DB_PATH` laissait donc le banc écrire 136 fois dans la base de PRODUCTION des médias
# (attrapé par le garde-fou d'écriture, 2026-10-27). Les chemins dérivés sont calculés à
# l'IMPORT : il faut poser `config.DB_PATH` AVANT que le module ne soit importé.
_TMP = tempfile.mkdtemp(prefix="verif_sortie_")
config.DB_PATH = database.DB_PATH = os.path.join(_TMP, "t.db")
database._tls.__dict__.pop("conn", None)       # la connexion est par THREAD : après, trop tard
database.init_db()

from app import container_net as cn, node_driver                               # noqa: E402

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


print("\n1. Le script posé sur l'hôte — lu avant d'être exécuté")
script, cmd = cn.script_sortie("eno2")
lignes = [l.strip() for l in script.splitlines() if l.strip() and not l.startswith("#")]

verifie(any("ip -4 route show default dev" in l and "GW=" in l for l in lignes),
        "1a. ★ la passerelle est RELUE depuis la carte à chaque passage (bail DHCP)", script)
verifie(not any(("ip route" in l and "table" not in l and "show" not in l) for l in lignes),
        "1b. ★ aucune écriture de route HORS de la table dédiée — la table principale de l'hôte "
        "n'est jamais touchée", script)
verifie(any("ip route replace default" in l and "table" in l for l in lignes),
        "1c. la route par défaut est posée dans la table dédiée", script)
verifie(all("ip route del" not in l and "ip route flush" not in l for l in lignes),
        "1d. et rien n'est SUPPRIMÉ du routage de l'hôte par le script périodique", script)
verifie(any("ip rule add from" in l for l in lignes) and any("ip rule del from" in l for l in lignes),
        "1e. ★ les règles sont réconciliées sur la liste : ajout de ce qui manque, RETRAIT de ce "
        "qui n'y est plus", script)
verifie(cn.SORTIE_LISTE in script,
        "1f. la liste des conteneurs est un FICHIER — les `ip rule` ne survivent pas à un "
        "redémarrage, et c'est lui que le minuteur rejoue", script)
verifie("[ -n \"$GW\" ]" in script or 'if [ -n "$GW" ]' in script,
        "1g. pas de passerelle lue (lien débranché, bail non obtenu) : on ne casse rien", script)

verifie(".timer" in cmd and "OnUnitActiveSec" in cmd,
        "1h. ★ un MINUTEUR rafraîchit : sans lui le correctif tient jusqu'au premier "
        "renouvellement de bail — des heures ou des semaines, donc une panne loin du geste", cmd[:200])
verifie("systemctl enable --now %s.timer" % cn.SORTIE_UNIT in cmd,
        "1i. et il est activé tout de suite, pas seulement au prochain démarrage")

ret = cn.script_retrait_sortie()
verifie(ret.index("ip rule del") < ret.index("ip route flush"),
        "1j. ★ au retrait, les RÈGLES partent avant la table : l'inverse laisserait le trafic "
        "des conteneurs dans un trou noir", ret[:160])
verifie(ret.count("|| true") >= 4, "1k. le retrait ne tombe pas sur ce qui a déjà disparu", ret)


print("\n2. Qui peut offrir une Sortie Internet")
n = database.db_add_node("aio", "127.0.0.1", kind="docker")
node = database.db_get_node(n)
ok, raison = cn.sortie_possible(node)
verifie(ok is False and "interne" in (raison or ""),
        "2a. ★ mode site : REFUS — en macvlan le conteneur a sa propre passerelle et ne traverse "
        "pas l'hôte, aucune règle de l'hôte ne peut changer son chemin", str(raison))

database.db_update_node(n, container_net_mode="interne")
node = database.db_get_node(n)
ok, raison = cn.sortie_possible(node)
verifie(ok is False and "Internet" in (raison or ""),
        "2b. mode interne mais aucune carte « Internet » déclarée : refus, avec le geste à faire",
        str(raison))

database.db_upsert_node_interface(n, ifname="eno2", role=cn.ROLE_INTERNET)
node = database.db_get_node(n)
verifie(cn.iface_internet(n) == "eno2", "2c. la carte dédiée est retrouvée par son rôle")
verifie(cn.sortie_possible(node) == (True, None), "2d. et la sortie devient possible",
        str(cn.sortie_possible(node)))


print("\n3. La liste des conteneurs qui sortent par Internet")
database.db_upsert_container_docker(2101, "str-1", n, "bobi-2101")
database.db_update_docker_ip(2101, "172.30.0.10")
database.db_upsert_container_docker(2102, "str-2", n, "bobi-2102")
database.db_update_docker_ip(2102, "172.30.0.11")
n2 = database.db_add_node("autre", "192.0.2.9", kind="docker")
database.db_upsert_container_docker(2103, "ailleurs", n2, "bobi-2103")
database.db_update_docker_ip(2103, "172.30.0.10")

verifie(cn.ips_sortie_internet(n) == [],
        "3a. aucun conteneur ne sort par Internet par DÉFAUT — déployer une version ne déplace "
        "aucun trafic")
database.db_set_container_egress(2101, "internet")
verifie(cn.ips_sortie_internet(n) == ["172.30.0.10"],
        "3b. seul le conteneur désigné y figure", str(cn.ips_sortie_internet(n)))
database.db_set_container_egress(2103, "internet")
verifie(cn.ips_sortie_internet(n) == ["172.30.0.10"],
        "3c. ★ et seulement ceux de CE nœud : les règles sont par hôte, et deux nœuds peuvent "
        "porter la même IP privée", str(cn.ips_sortie_internet(n)))
database.db_set_container_egress(2101, "node")
verifie(cn.ips_sortie_internet(n) == [],
        "3d. repassé en sortie « nœud », il quitte la liste (donc sa règle sera retirée)")

# Valeur inconnue en base — écrite DIRECTEMENT, en contournant le helper qui normalise : c'est le
# seul moyen de produire ce que produirait une migration tierce ou une édition à la main. Sans ce
# cas, la normalisation n'est jamais mise à l'épreuve (mutation `v or EGRESS_NODE` : ne mordait pas).
with database.get_db() as _db:
    _db.execute("UPDATE containers SET egress='ZZZ' WHERE vmid=?", (2101,))
    _db.commit()
verifie(database.container_egress(database.db_get_container(2101)) == "node",
        "3e. ★ une valeur inconnue en base vaut « réseau du nœud » — le seul repli qui ne déplace "
        "aucun trafic", database.container_egress(database.db_get_container(2101)))
verifie(cn.ips_sortie_internet(n) == [],
        "3f. et elle ne met donc personne dans la liste de la Sortie Internet")
database.db_set_container_egress(2101, "node")


print("\n4. Poser la sortie d'un conteneur : un seul point d'entrée, qui refuse")
appels = []
node_driver.host_exec = lambda nd, cmd, timeout=None: (appels.append(cmd), (0, "", ""))[1]

ok, msg = cn.poser_sortie(2101, "internet")
verifie(ok is True and database.container_egress(database.db_get_container(2101)) == "internet",
        "4a. nominal : la sortie est posée et persistée", str(msg))
verifie(any(cn.SORTIE_LISTE in c for c in appels) and any(cn.SORTIE_SCRIPT in c for c in appels),
        "4b. ★ la liste ET le script partent dans le MÊME appel hôte — en deux appels, un échec "
        "entre les deux laisserait une liste que rien n'applique", str(len(appels)))

ok, msg = cn.poser_sortie(2101, "zz")
verifie(ok is False, "4c. une sortie inconnue est refusée", str(msg))
ok, msg = cn.poser_sortie(999999, "internet")
verifie(ok is False and "introuvable" in msg, "4d. conteneur inconnu : refusé", str(msg))

database.db_upsert_container_docker(2104, "sans-ip", n, "bobi-2104")
ok, msg = cn.poser_sortie(2104, "internet")
verifie(ok is False and "IP" in msg,
        "4e. ★ conteneur sans IP : REFUS — enregistrer l'intention ferait croire à une sortie "
        "Internet alors qu'il sortirait par le site", str(msg))

# Échec de l'hôte → la base ne doit pas mentir sur l'état réel.
node_driver.host_exec = lambda nd, cmd, timeout=None: (1, "", "ip: operation not permitted")
avant = database.container_egress(database.db_get_container(2102))
ok, msg = cn.poser_sortie(2102, "internet")
verifie(ok is False, "4f. échec sur l'hôte : rendu comme un échec", str(msg))
verifie(database.container_egress(database.db_get_container(2102)) == avant,
        "4g. ★ et la base est REMISE comme avant — une base qui dit « Internet » sur un hôte qui "
        "route par le site est pire qu'un refus",
        database.container_egress(database.db_get_container(2102)))

database.db_update_node(n, container_net_mode="site")
node_driver.host_exec = lambda nd, cmd, timeout=None: (0, "", "")
ok, msg = cn.poser_sortie(2101, "internet")
verifie(ok is False and "interne" in msg,
        "4h. nœud repassé en mode site : la sortie Internet est refusée, avec sa raison", str(msg))
database.db_update_node(n, container_net_mode="interne")


print("\n5. La capacité est pilotable par les MACROS (sinon elle est morte)")
from app import core_plugins                                                   # noqa: E402

cat = [e for e in core_plugins.service_actions() if e.get("service") == "container_net"]
verifie(bool(cat), "5a. ★ le fournisseur apparaît dans le catalogue des macros", str(len(cat)))
acts = {a["id"] for e in cat for a in e.get("actions") or []}
verifie("set_egress" in acts, "5b. l'action de bascule y est", str(acts))
_p = next((p for e in cat for a in e.get("actions") or [] if a["id"] == "set_egress"
           for p in a.get("params") or [] if p["key"] == "vmid"), None)
verifie(_p is not None and isinstance(_p.get("options"), list) and len(_p["options"]) >= 2,
        "5c. ★ la liste des conteneurs est VIVANTE (hook `action_options`) — un nom de hook "
        "approchant ne serait jamais appelé et le menu resterait vide, sans erreur",
        str((_p or {}).get("options"))[:120])

ok_m = cn.run_action("set_egress", {"vmid": 2101, "egress": "internet"}, {})
verifie(ok_m.get("ok") is True, "5d. la macro pose bien la sortie", str(ok_m))
try:
    cn.run_action("set_egress", {"vmid": 2104, "egress": "internet"}, {})
    verifie(False, "5e. une macro qui échoue LÈVE (sinon macro « réussie », effet absent)")
except RuntimeError as e:
    verifie("IP" in str(e), "5e. ★ une macro qui échoue LÈVE, avec la raison", str(e))

verifie(cn.read_state("sortie_internet_conteneurs", {}) >= 1,
        "5f. l'état compte les conteneurs concernés (condition de macro)",
        str(cn.read_state("sortie_internet_conteneurs", {})))
node_driver.host_exec = lambda nd, cmd, timeout=None: (0, "\n--\n0\n--\n", "")
verifie(cn.read_state("sortie_internet_prete", {}) is False,
        "5g. ★ « opérationnelle » est FAUX sans passerelle lue : la configuration ne suffit "
        "pas, on exige la mesure (câble, bail)")
node_driver.host_exec = lambda nd, cmd, timeout=None: (0, "192.168.9.1\n--\n2\n--\nenabled", "")
verifie(cn.read_state("sortie_internet_prete", {}) is True,
        "5h. et VRAI quand la passerelle est effectivement là")


print("\n5 bis. Le rôle « internet » est DÉCLARABLE depuis l'interface")
# ★ LE TROU QUE CE CONTRÔLE FERME, trouvé juste après avoir écrit le reste : tout fonctionnait
# par l'API, et le rôle était ABSENT de `NODE_IFACE_ROLES`. La route de sauvegarde des rôles
# refuse une valeur hors liste (400) : la fonction entière aurait été inatteignable depuis
# l'interface. Un banc de module ne pouvait pas le voir — `db_upsert_node_interface` ne valide
# pas, lui, et c'est par là que ce banc crée ses interfaces.
from app.routes.node_network import NODE_IFACE_ROLES                           # noqa: E402

verifie(cn.ROLE_INTERNET in NODE_IFACE_ROLES,
        "5i. ★ le rôle est dans le vocabulaire de la route (sinon 400 à l'enregistrement, et la "
        "fonction est morte pour un exploitant)", str(NODE_IFACE_ROLES))
_cat = json.load(open(os.path.join(RACINE, "i18n", "fr.json"), encoding="utf-8"))
_cat_en = json.load(open(os.path.join(RACINE, "i18n", "en.json"), encoding="utf-8"))
_cle = "settings.js.net.role_%s" % cn.ROLE_INTERNET
verifie(_cle in _cat and _cle in _cat_en,
        "5j. et il a un libellé dans les DEUX langues — sans quoi le menu afficherait la clé",
        _cle)


print("\n6. Le déploiement repose les règles, la destruction les libère")
_src = open(os.path.join(RACINE, "app", "docker_compute.py"), encoding="utf-8").read()
_arbre = ast.parse(_src)


def _appelle(nom_fn, cible):
    fn = next((x for x in ast.walk(_arbre)
               if isinstance(x, ast.FunctionDef) and x.name == nom_fn), None)
    if not fn:
        return False
    return any((getattr(c.func, "attr", None) or getattr(c.func, "id", None)) == cible
               for c in ast.walk(fn) if isinstance(c, ast.Call))


verifie(_appelle("deploy_compute", "appliquer_sortie"),
        "6a. ★ le déploiement repose la règle : un conteneur recréé garde son IP mais pas "
        "forcément sa règle (nœud redémarré), et sortirait par le site en silence")
verifie(_appelle("destroy_compute", "appliquer_sortie"),
        "6b. ★ la destruction rafraîchit la liste : sinon la règle survit au conteneur, et l'IP "
        "réattribuée ferait sortir un AUTRE conteneur par Internet sans que personne l'ait voulu")


print("\n" + ("─" * 70))
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ Sortie Internet : routage par source, table dédiée, hôte intact, pilotable par macro")
sys.exit(0)
