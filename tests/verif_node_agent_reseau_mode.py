#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Mode du réseau des conteneurs : « réseau du site » (macvlan) ou « réseau interne » (bridge).

Ce que ce banc protège, et pourquoi il APPELLE au lieu de lire (2026-10-27) :

1. `/v1/host/networks/ensure` écrivait « -d macvlan » EN DUR. Un contrôleur demandant un
   réseau interne aurait obtenu un macvlan, avec un `ok: true` : la panne la plus coûteuse
   est celle qui réussit. On appelle donc le handler pour de vrai, avec `docker`/`run`
   simulés, et on regarde la LIGNE DE COMMANDE construite — pas le texte du fichier.
2. L'inventaire `_docker_networks()` filtrait par LISTE BLANCHE de pilotes. Un bridge n'y
   figurait pas : le nœud se déclarait sans réseau des conteneurs, donc inéligible, sans que
   rien n'échoue. Même règle que [[liste-exclusion-ne-voit-pas-le-nouveau]], par l'autre bout :
   une liste blanche ne voit pas ce qui est nouveau.
3. Un agent antérieur à 0.30.0 IGNORE `driver` et crée un macvlan en répondant « ok ».
   `node_driver.ensure_network` doit s'en apercevoir sur la RÉPONSE de l'agent, pas seulement
   sur la version déclarée — `agent_version` peut manquer.
4. `register()` prenait `nets[0]`. L'inventaire contenant désormais plusieurs réseaux, un
   resync pouvait faire changer de réseau un nœud en service, en silence.

Hors ligne : ni Docker, ni réseau, ni base. Les deux modules sont chargés tels quels et leurs
points de sortie (le `docker` de l'agent, le `_call` et les helpers de base du contrôleur)
sont remplacés par des doublures qui ENREGISTRENT ce qu'on leur demande.
"""
import importlib.util
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ─── chargement de l'agent (fichier isolé, hors paquet) ──────────────────────────────────────
def charger_agent():
    chemin = os.path.join(RACINE, "node_agent", "agent.py")
    spec = importlib.util.spec_from_file_location("bobi_node_agent_sous_test", chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Journal:
    """Doublure de `docker()` et `run()` : répond ce qu'on lui dit, note ce qu'on lui demande."""

    def __init__(self, inspect_driver=None, inspect_rc=1):
        self.inspect_driver = inspect_driver      # pilote du réseau DÉJÀ présent (None = absent)
        self.inspect_rc = 0 if inspect_driver else inspect_rc
        self.creations = []                       # argv de chaque `docker network create`
        self.appels = []                          # toutes les commandes vues

    def docker(self, *args, **kw):
        self.appels.append(("docker",) + args)
        if args[:2] == ("network", "inspect"):
            return (self.inspect_rc, self.inspect_driver or "", "")
        return (0, "", "")

    def run(self, argv, **kw):
        self.appels.append(tuple(argv))
        if argv[:3] == ["docker", "network", "create"]:
            self.creations.append(argv)
        return (0, "", "")


def appel_ensure(agent, corps, inspect_driver=None):
    """Appelle VRAIMENT do_POST sur /v1/host/networks/ensure. Rend (code, reponse, journal)."""
    j = Journal(inspect_driver=inspect_driver)
    agent.docker, agent.run = j.docker, j.run
    h = agent.Handler.__new__(agent.Handler)
    h.path = "/v1/host/networks/ensure"
    recu = {}

    h._peer_refused = lambda: False
    h._auth_ok = lambda: True
    h._body = lambda: corps
    h._send = lambda code, obj=None: recu.update({"code": code, "obj": obj or {}})
    h.do_POST()
    return recu.get("code"), recu.get("obj", {}), j


def argv_str(argv):
    return " ".join(argv)


# ─── 1. l'endpoint : le pilote demandé est le pilote posé ────────────────────────────────────
print("\n1. /v1/host/networks/ensure — le MODE demandé est le mode posé")
agent = charger_agent()

code, rep, j = appel_ensure(agent, {"name": "bobimacvlan", "parent": "eno1",
                                    "subnet": "10.0.0.0/24", "gateway": "10.0.0.1"})
verifie(code == 200 and len(j.creations) == 1, "1a. sans `driver`, un réseau est bien créé",
        "code=%s créations=%d" % (code, len(j.creations)))
cmd = argv_str(j.creations[0]) if j.creations else ""
verifie("-d macvlan" in cmd and "parent=eno1" in cmd,
        "1a bis. le DÉFAUT reste macvlan avec son parent (contrôleur antérieur à 0.30.0)", cmd)

code, rep, j = appel_ensure(agent, {"name": "bobi-interne", "driver": "bridge",
                                    "subnet": "172.30.0.0/24", "gateway": "172.30.0.1"})
cmd = argv_str(j.creations[0]) if j.creations else ""
verifie("-d bridge" in cmd, "1b. `driver: bridge` crée un BRIDGE (et non un macvlan en douce)", cmd)
verifie("parent=" not in cmd, "1b bis. aucun `parent=` sur un bridge (option macvlan)", cmd)
verifie("com.docker.network.bridge.name=bobi-interne" in cmd,
        "1c. le pont hôte porte un nom STABLE (pare-feu, NAT, diagnostic)", cmd)
verifie("--subnet 172.30.0.0/24" in cmd and "--gateway 172.30.0.1" in cmd,
        "1d. subnet et passerelle suivent en mode interne comme en mode site", cmd)

long_nom = "bobi-reseau-interne-tres-long"
code, rep, j = appel_ensure(agent, {"name": long_nom, "driver": "bridge"})
cmd = argv_str(j.creations[0]) if j.creations else ""
pont = [a for a in (j.creations[0] if j.creations else []) if "bridge.name=" in a]
verifie(bool(pont) and len(pont[0].split("=", 1)[1]) <= 15,
        "1e. le nom du pont est borné à 15 caractères (IFNAMSIZ — le noyau refuse au-delà)",
        str(pont))

code, rep, j = appel_ensure(agent, {"name": "x", "driver": "ipvlan"})
verifie(code == 400 and not j.creations,
        "1f. un pilote hors du vocabulaire fermé est REFUSÉ, et rien n'est créé",
        "code=%s créations=%d" % (code, len(j.creations)))

code, rep, j = appel_ensure(agent, {"name": "bobi-interne", "driver": "bridge"},
                            inspect_driver="bridge")
verifie(code == 200 and rep.get("created") is False and not j.creations,
        "1g. réseau déjà présent dans le BON mode : idempotent, rien n'est recréé", str(rep))

code, rep, j = appel_ensure(agent, {"name": "bobimacvlan", "driver": "bridge"},
                            inspect_driver="macvlan")
verifie(code == 409 and not j.creations,
        "1h. ★ présent sous un AUTRE pilote → 409, jamais un « created:false » qui ment",
        "code=%s" % code)
verifie(rep.get("driver") == "macvlan",
        "1h bis. le refus NOMME le pilote en place (sinon l'exploitant ne sait pas quoi détruire)",
        str(rep))
verifie(not any("rm" in " ".join(a) for a in j.appels),
        "1i. l'agent ne DÉTRUIT rien de lui-même : lui seul ignore quels conteneurs tombent")

code, rep, j = appel_ensure(agent, {"name": "bobi-interne", "driver": "bridge"})
verifie(rep.get("driver") == "bridge",
        "1j. la réponse porte le pilote posé — c'est le contrat dont dépend le contrôleur",
        str(rep))


# ─── 2. l'inventaire voit le bridge ──────────────────────────────────────────────────────────
print("\n2. /v1/capabilities — un réseau interne EXISTE pour le contrôleur")
agent2 = charger_agent()
lignes = "bridge bridge\nhost host\nnone null\nbobi-interne bridge\nbobimacvlan macvlan\n"


def faux_docker(*args, **kw):
    if args[:2] == ("network", "ls"):
        return (0, lignes, "")
    if args[:2] == ("network", "inspect"):
        nom = args[-1]
        return (0, ("10.0.0.0/24|eno1" if nom == "bobimacvlan" else "172.30.0.0/24|"), "")
    return (0, "", "")


agent2.docker = faux_docker
nets = agent2._docker_networks()
noms = {n["name"]: n for n in nets}
verifie("bobi-interne" in noms and noms.get("bobi-interne", {}).get("driver") == "bridge",
        "2a. ★ un réseau des conteneurs en mode interne (bridge) APPARAÎT dans l'inventaire",
        str(noms))
verifie(not ({"bridge", "host", "none"} & set(noms)),
        "2b. les trois réseaux intégrés de Docker restent exclus (sinon enrôlés par erreur)",
        str(sorted(noms)))
verifie(noms.get("bobimacvlan", {}).get("parent") == "eno1",
        "2c. le parent d'un macvlan est remonté (diagnostic « réseau qui pend dans le vide »)",
        str(noms.get("bobimacvlan")))
verifie(noms.get("bobi-interne", {}).get("subnet") == "172.30.0.0/24",
        "2d. le subnet est remonté pour les deux modes", str(noms.get("bobi-interne")))


# ─── 3. le contrôleur n'accepte pas un mode qu'il n'a pas obtenu ─────────────────────────────
print("\n3. node_driver.ensure_network — un agent trop ancien ne passe pas pour neuf")
from app import node_driver                                            # noqa: E402

emis = []


def faux_call(node, methode, chemin, body=None, timeout=None, **kw):
    emis.append(body)
    return True, reponse_agent[0]


reponse_agent = [{"ok": True, "created": True, "driver": "macvlan"}]
node_driver._call = faux_call

emis.clear()
ok, res = node_driver.ensure_network({"agent_version": "0.29.2"}, "bobi-interne",
                                     driver="bridge")
verifie(ok is False and not emis,
        "3a. agent 0.29.2 + mode interne : refus AVANT l'appel, rien n'est tenté",
        "ok=%s émis=%d" % (ok, len(emis)))
verifie(isinstance(res, str) and "0.30.0" in res,
        "3a bis. le refus dit la version qu'il faut (sinon l'exploitant cherche)", str(res))

emis.clear()
reponse_agent[0] = {"ok": True, "created": True, "driver": "macvlan"}
ok, res = node_driver.ensure_network({"agent_version": "0.31.0"}, "bobi-interne",
                                     driver="bridge")
verifie(ok is False,
        "3b. ★ l'agent répond « ok » mais a posé un MACVLAN : c'est un échec, pas un succès",
        "ok=%s res=%s" % (ok, res))

emis.clear()
reponse_agent[0] = {"ok": True, "created": True, "driver": "bridge"}
ok, res = node_driver.ensure_network({"agent_version": "0.30.0"}, "bobi-interne",
                                     driver="bridge")
verifie(ok is True and emis and emis[0].get("driver") == "bridge",
        "3c. nominal : le mode demandé est transmis à l'agent et accepté", str(emis))

emis.clear()
reponse_agent[0] = {"ok": True, "created": True, "driver": "macvlan"}
ok, res = node_driver.ensure_network({"agent_version": "0.28.0"}, "bobimacvlan",
                                     parent="eno1", driver="macvlan")
verifie(ok is True,
        "3d. le mode site continue de marcher avec un agent ancien (zéro régression)", str(res))

emis.clear()
reponse_agent[0] = {"ok": True, "created": True}          # agent 0.29.x : pas de `driver`
ok, res = node_driver.ensure_network({"agent_version": "0.29.2"}, "bobimacvlan",
                                     parent="eno1", driver="macvlan")
verifie(ok is True,
        "3e. une réponse SANS `driver` vaut macvlan — pas un refus sur un agent d'hier", str(res))


# ─── 4. un resync ne change pas le réseau d'un nœud en service ───────────────────────────────
print("\n4. node_driver.register — le réseau d'un nœud configuré ne bouge pas tout seul")
ecrits = {}


def monter_doublures(ligne_existante):
    node_driver._request = lambda *a, **kw: (True, caps[0])
    node_driver.db_get_node_by_host = lambda h: ligne_existante
    node_driver.db_update_node = lambda nid, **f: ecrits.update(f)
    node_driver.db_add_node = lambda *a, **kw: 1
    node_driver.db_add_alert = lambda *a, **kw: None
    node_driver.db_get_node = lambda nid: {"id": nid}


caps = [{"capabilities": ["compute"], "agent_version": "0.30.0",
         "networks": [{"name": "zz-autre", "driver": "bridge"},
                      {"name": "bobi-site", "driver": "macvlan"}],
         "images": [], "mxl_mount": "/dev/shm"}]

ecrits.clear()
monter_doublures({"id": 7, "docker_network": "bobi-site"})
node_driver.register("10.0.0.9", 9100, "jeton")
verifie(ecrits.get("docker_network") == "bobi-site",
        "4a. ★ le réseau DÉJÀ en base est conservé, quel que soit l'ordre de `docker network ls`",
        str(ecrits.get("docker_network")))

# ★ CE CAS SEUL DISTINGUE « garder l'existant » de « préférer bobi* ». Dans le cas 4a, le
# réseau en base était AUSSI le seul nommé `bobi*` : retirer la conservation ne changeait rien,
# et la mutation ne mordait pas. Ici le nœud a été configuré sur un réseau qui ne porte pas nos
# noms — ce que fait tout exploitant ayant nommé son réseau lui-même — et le repli donnerait
# l'autre. Cf. [[garde-verte-du-premier-coup-doit-etre-mutee]] : il ne suffit pas que le banc
# soit rouge quand on casse le code, il faut que le cas pointe la BONNE moitié du code.
ecrits.clear()
monter_doublures({"id": 7, "docker_network": "zz-autre"})
node_driver.register("10.0.0.9", 9100, "jeton")
verifie(ecrits.get("docker_network") == "zz-autre",
        "4a ter. un réseau configuré à la main (hors `bobi*`) est conservé, pas « corrigé »",
        str(ecrits.get("docker_network")))

ecrits.clear()
monter_doublures({"id": 7, "docker_network": "bobi-disparu"})
node_driver.register("10.0.0.9", 9100, "jeton")
verifie(ecrits.get("docker_network") == "bobi-site",
        "4b. réseau disparu du nœud : on reprend un des NÔTRES (`bobi*`), pas le premier venu",
        str(ecrits.get("docker_network")))

ecrits.clear()
monter_doublures(None)
node_driver.register("10.0.0.9", 9100, "jeton")
verifie(ecrits.get("docker_network") == "bobi-site",
        "4c. nouveau nœud : `bobi*` l'emporte sur `nets[0]` (ici « zz-autre »)",
        str(ecrits.get("docker_network")))

ecrits.clear()
caps[0]["networks"] = [{"name": "zz-autre", "driver": "bridge"}]
monter_doublures(None)
node_driver.register("10.0.0.9", 9100, "jeton")
verifie(ecrits.get("docker_network") == "zz-autre",
        "4d. aucun réseau `bobi*` : on prend ce qu'il y a (pas de nœud sans réseau du tout)",
        str(ecrits.get("docker_network")))

ecrits.clear()
caps[0]["networks"] = []
monter_doublures(None)
node_driver.register("10.0.0.9", 9100, "jeton")
verifie(ecrits.get("docker_network") is None,
        "4e. aucun réseau : la colonne reste vide (le nœud se déclare inéligible, et le dit)",
        str(ecrits.get("docker_network")))


print("\n" + ("─" * 70))
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ mode du réseau des conteneurs : agent, inventaire, contrôleur et enrôlement tiennent")
sys.exit(0)
