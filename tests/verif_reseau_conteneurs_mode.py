#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Mode du réseau des conteneurs, côté orchestrateur : l'ALLOCATION D'IP suit le mode.

Le piège que ce banc tient fermé (relevé le 2026-10-27, avant d'être écrit) :

`centralized_ipam()` est vrai dès qu'une carte containers porte une plage personnalisée.
Un nœud basculé en « réseau interne » en gardant cette plage aurait donc reçu
`docker run --ip <adresse du LAN>` dans un bridge privé — refusé par Docker, déploiement
mort, et le message d'erreur ne parle ni de mode ni de plage. La règle est donc que le MODE
passe avant la plage de la carte, et c'est exactement ce qu'on mesure ici.

Et son symétrique, aussi silencieux : un subnet privé ne sort pas du nœud, donc deux nœuds
peuvent porter 172.30.0.10 sans conflit. Compter leurs conteneurs ensemble diviserait le
nombre de conteneurs déployables par le nombre de nœuds, sans que rien ne le dise.

Hors ligne : base JETABLE posée avant tout accès, aucun réseau, aucun Docker.
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import database                                                      # noqa: E402

# Base jetable, AVANT tout accès : la connexion SQLite est par THREAD, après c'est trop tard.
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_net_mode_"), "t.db")
database.init_db()

from app import allocations, settings                                          # noqa: E402
from app.config import NET_INTERNE_SUBNET_DEFAUT                               # noqa: E402

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ─── 0. centralized_ipam, ISOLÉ — à faire d'abord, et c'est la raison de l'ordre ─────────────
# `centralized_ipam()` devient vrai par QUATRE chemins : topologie séparée, mode interne, plage
# personnalisée, multi-nœud. Testé plus bas, avec un pool statique réglé et trois nœuds en base,
# un cas « mode interne » serait vrai de toute façon : la mutation qui retire la branche du mode
# ne mordrait pas, et la garde serait verte en ne mesurant rien. On l'isole donc ICI, à un seul
# nœud, sans plage et sans réglage de réseau — là, seul le mode peut répondre.
_seul = database.db_add_node("seul-interne", "192.0.2.4", kind="docker")
verifie(allocations.centralized_ipam(_seul) is False,
        "0a. témoin : à un nœud, sans plage ni mode, l'IPAM reste celui de Docker",
        str(allocations.centralized_ipam(_seul)))
database.db_update_node(_seul, container_net_mode="interne")
verifie(allocations.centralized_ipam(_seul) is True,
        "0b. ★ le mode interne À LUI SEUL fait allouer l'orchestrateur (IP collante)",
        str(allocations.centralized_ipam(_seul)))
database.db_delete_node(_seul)


# ─── décor : deux nœuds, une plage de site, une plage personnalisée ──────────────────────────
settings.set("ip_start", "192.0.2.100")
settings.set("ip_end", "192.0.2.199")
settings.set("gateway", "192.0.2.1")
settings.set("net_mode", "static")
settings.set("net_topology", "simple")

n_site = database.db_add_node("site", "192.0.2.5", kind="docker")
n_int = database.db_add_node("interne", "192.0.2.6", kind="docker")

# Le nœud « interne » porte AUSSI une plage personnalisée sur sa carte containers : c'est
# précisément la configuration qui faisait échouer le déploiement en silence.
database.db_upsert_node_interface(n_int, ifname="eno1", role=database.ROLE_MGMT_CONTAINERS,
                                  ip_cidr="192.0.2.6/24", gateway="192.0.2.1",
                                  ct_ip_start="192.0.2.150", ct_ip_end="192.0.2.180")
database.db_upsert_node_interface(n_site, ifname="eno1", role=database.ROLE_MGMT_CONTAINERS,
                                  ip_cidr="192.0.2.5/24", gateway="192.0.2.1")


print("\n1. Le mode est une valeur de base, et son défaut ne déplace personne")
verifie(database.node_net_mode(n_site) == "site",
        "1a. un nœud créé sans rien dire est en « réseau du site » (mode historique)",
        database.node_net_mode(n_site))
database.db_update_node(n_int, container_net_mode="interne")
verifie(database.node_net_mode(n_int) == "interne" and database.node_net_interne(n_int),
        "1b. le mode se persiste et se relit par nœud", database.node_net_mode(n_int))
database.db_update_node(n_int, container_net_mode="ZZZ")
verifie(database.node_net_mode(n_int) == "site",
        "1c. une valeur inconnue en base vaut « site » — le seul repli qui ne déplace rien",
        database.node_net_mode(n_int))
database.db_update_node(n_int, container_net_mode="interne")


print("\n2. Les bornes d'allocation suivent le MODE, pas la carte")
s_i, e_i, sc_i = allocations.node_ip_bounds(n_int)
verifie(sc_i == "interne", "2a. ★ nœud interne : la portée devient « interne »", sc_i)
verifie(s_i == "172.30.0.2" and e_i == "172.30.0.254",
        "2b. ★ les bornes sortent du subnet PRIVÉ, pas de la plage du site (192.0.2.150-180)",
        "%s → %s" % (s_i, e_i))
verifie(allocations.node_ip_bounds(n_site)[2] == "cluster",
        "2c. le nœud resté en mode site puise au pool du cluster (zéro régression)",
        str(allocations.node_ip_bounds(n_site)))

database.db_update_node(n_int, container_net_mode="site")
verifie(allocations.node_ip_bounds(n_int)[:2] == ("192.0.2.150", "192.0.2.180"),
        "2d. retour en mode site : la plage de la carte redevient effective, intacte",
        str(allocations.node_ip_bounds(n_int)))
database.db_update_node(n_int, container_net_mode="interne")

database.db_update_node(n_int, container_net_subnet="192.168.77.0/24")
verifie(allocations.interne_bounds(n_int) == ("192.168.77.2", "192.168.77.254"),
        "2e. le subnet privé est réglable par nœud", str(allocations.interne_bounds(n_int)))
database.db_update_node(n_int, container_net_subnet="")
verifie(allocations.interne_bounds(n_int)[0].startswith(
            NET_INTERNE_SUBNET_DEFAUT.split(".0/")[0]),
        "2f. vide = le défaut du produit (%s)" % NET_INTERNE_SUBNET_DEFAUT,
        str(allocations.interne_bounds(n_int)))

database.db_update_node(n_int, container_net_subnet="pas-un-subnet")
verifie(allocations.interne_bounds(n_int) == ("", ""),
        "2g. subnet illisible : aucune borne inventée (l'épuisement sera dit, pas deviné)",
        str(allocations.interne_bounds(n_int)))
database.db_update_node(n_int, container_net_subnet="10.9.9.0/31")
verifie(allocations.interne_bounds(n_int) == ("", ""),
        "2h. subnet trop petit pour une passerelle + un conteneur : refusé de même",
        str(allocations.interne_bounds(n_int)))
database.db_update_node(n_int, container_net_subnet="")


print("\n3. L'IP attribuée est DANS le bridge — le défaut qui tuait le déploiement")
verifie(allocations.centralized_ipam(n_int) is True,
        "3a. l'orchestrateur attribue lui-même en mode interne (IP collante au redéploiement)")
database.db_upsert_container_docker(1101, "c1101", n_int, "bobi-1101")
ip = allocations.allocate_container_ip(1101, node_id=n_int)
verifie(ip is not None and ip.startswith("172.30.0."),
        "3b. ★ l'IP sort du subnet privé — PAS de la plage du site, que le nœud porte encore",
        str(ip))
database.db_update_docker_ip(1101, ip)

database.db_upsert_container_docker(1102, "c1102", n_int, "bobi-1102")
ip2 = allocations.allocate_container_ip(1102, node_id=n_int)
verifie(ip2 != ip and ip2.startswith("172.30.0."),
        "3c. deux conteneurs du même nœud ne partagent pas une adresse", "%s / %s" % (ip, ip2))
database.db_update_docker_ip(1102, ip2)

verifie(allocations.allocate_container_ip(1101, node_id=n_int) == ip,
        "3d. STICKY : un redéploiement rend la même IP (le contrôleur ne perd pas sa cible :8081)")

# Le nœud resté en mode site continue d'allouer sur le LAN.
database.db_upsert_container_docker(1201, "c1201", n_site, "bobi-1201")
ip_site = allocations.allocate_container_ip(1201, node_id=n_site)
verifie(ip_site is not None and ip_site.startswith("192.0.2."),
        "3e. sur le nœud en mode site, rien ne change : l'IP reste celle du LAN", str(ip_site))
database.db_update_docker_ip(1201, ip_site)


print("\n4. Un subnet privé est propre à son nœud")
n_int2 = database.db_add_node("interne2", "192.0.2.7", kind="docker")
database.db_update_node(n_int2, container_net_mode="interne")
database.db_upsert_container_docker(1301, "c1301", n_int2, "bobi-1301")
ip_b = allocations.allocate_container_ip(1301, node_id=n_int2)
verifie(ip_b == ip,
        "4a. ★ deux nœuds internes peuvent porter LA MÊME adresse — le subnet ne sort pas du nœud",
        "%s vs %s" % (ip_b, ip))

# ★ CE CONTENEUR SEUL REND LE CAS 4c DISCRIMINANT. Sans une adresse que l'autre nœud est le
# seul à porter, « occupation du nœud » et « occupation globale » donnent le même ensemble sur
# cette plage, et retirer le filtrage par nœud ne se voit pas.
database.db_upsert_container_docker(1303, "c1303", n_int2, "bobi-1303")
database.db_update_docker_ip(1303, "172.30.0.77")

stats = {e["label"]: e for e in allocations.ip_stats()["ranges"]}
verifie("interne" in stats and stats["interne"]["scope"] == "interne",
        "4b. la page Réglages montre la plage privée du nœud (sinon on croit qu'il puise au pool)",
        str(sorted(stats)))
verifie(stats.get("interne", {}).get("used") == sorted([ip, ip2]),
        "4c. l'occupation affichée d'une plage privée ne compte QUE les conteneurs du nœud",
        str(stats.get("interne", {}).get("used")))
verifie(stats["cluster"]["used"] == [ip_site],
        "4d. la plage du site, elle, reste comptée globalement (deux fois la même IP = conflit)",
        str(stats["cluster"]["used"]))


print("\n5. Épuisement : dit, pas deviné")
database.db_update_node(n_int2, container_net_subnet="10.9.9.0/31")
database.db_upsert_container_docker(1302, "c1302", n_int2, "bobi-1302")
verifie(allocations.allocate_container_ip(1302, node_id=n_int2) is None,
        "5a. subnet inexploitable : aucune IP rendue (plutôt qu'une adresse hors bridge)")
# L'alerte est KEYÉE : la clé vit dans `msg_key`, `message` ne porte que la forme canonique
# française. Chercher dans `message` ne trouverait rien et ferait croire au silence.
alertes = [a for a in database.db_get_alerts(vmid=1302)
           if "ipam_plan_epuise" in ((a.get("msg_key") or "") + (a.get("message") or ""))]
verifie(bool(alertes), "5b. et une alerte le dit, avec la plage concernée",
        str(database.db_get_alerts(vmid=1302)[:1]))
verifie(bool(alertes) and "interne" in (alertes[0].get("msg_params") or ""),
        "5c. l'alerte nomme la PORTÉE (« interne ») : sans ça on cherche dans le pool du site",
        str(alertes[0].get("msg_params") if alertes else None))


print("\n" + ("─" * 70))
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ allocation d'IP : le mode du réseau des conteneurs fait loi")
sys.exit(0)
