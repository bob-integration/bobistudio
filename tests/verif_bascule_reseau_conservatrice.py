#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Changer de mode réseau ne demande pas l'avis du navigateur, et n'efface JAMAIS la base.

Ce que la bascule de la Z8 a coûté le 2026-10-28 à 20:45, et que cette garde interdit :

1. ★ UN APPEL API SANS `confirme` DÉTRUISAIT TOUT. Le chemin macvlan exigeait un accord
   explicite ; le chemin « réseau interne » ne l'exigeait pas. La session appelante, se fiant à
   l'interface — qui, elle, fait bien deux temps —, a envoyé un POST sans `confirme` : la bascule
   est partie aussitôt. Une confirmation qui ne vit que dans le navigateur ne protège rien, c'est
   le serveur qui détruit. Et le trou était SYMÉTRIQUE : revenir au réseau du site change le nom
   du réseau, donc la divergence calculée sur le nouveau nom ne voyait rien et les conteneurs
   attachés à l'ancien tombaient sans avoir été annoncés.

2. ★★ LES CONTENEURS ÉTAIENT SUPPRIMÉS DE LA BASE. `detruire_reseau` appelait `destroy_compute`,
   donc `db_delete_container` : 12 conteneurs de production effacés, `deploy_config` compris.
   Aucune sauvegarde n'était active ; les configurations n'ont été récupérées que parce que le WAL
   SQLite n'avait pas encore été reporté dans le fichier principal. Un paramètre de réseau qui
   change n'est pas une décision d'effacer la production : les conteneurs sont DÉTACHÉS (retirés
   de l'hôte, gardés en base avec projet, réglages et identité d'instance) puis redéployés.

Ce qui est jeté à dessein, et doit l'être : l'IP (elle appartenait au réseau d'avant) et la
signature de spec (sinon le prochain déploiement croirait le conteneur conforme et ne le
recréerait pas).

Hors ligne : base jetable, aucun Docker, aucun réseau — l'hôte et l'agent sont doublés.
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_bascule_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database.init_db()

from app import container_net as cn                                            # noqa: E402
from app import node_driver                                                    # noqa: E402

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ── L'hôte est doublé : on compte ce qui lui est demandé, et rien n'est exécuté ──────────────
_hote = {"cmds": []}


def _faux_host_exec(node, cmd, timeout=None, **kw):
    _hote["cmds"].append(cmd)
    return (0, "", "")


def _faux_ensure_network(node, name, **kw):
    return True, {"name": name}


node_driver.host_exec = _faux_host_exec
node_driver.ensure_network = _faux_ensure_network
# Tout-en-un : le mode interne n'est permis que là (et c'est mesuré sur les IP du contrôleur).
cn.est_tout_en_un = lambda node, controller_ips=None: True

# Le réseau macvlan « d'avant », avec DEUX conteneurs attachés — c'est le constat que fait
# `inspect_reseau` sur l'hôte, donc le seul endroit à doubler.
_ATTACHES = {"bobimacvlan": ["bobi-2201", "bobi-2202"]}


def _faux_inspect(node, name):
    noms = _ATTACHES.get(name)
    if noms is None:
        return None
    return {"Driver": "macvlan", "IPAM": {"Config": [{"Subnet": "192.0.2.0/24",
                                                      "Gateway": "192.0.2.254"}]},
            "Options": {"parent": "eno1"},
            "Containers": {str(i): {"Name": n} for i, n in enumerate(noms)}}


cn.inspect_reseau = _faux_inspect

# ── Un nœud tout-en-un en mode site, avec deux conteneurs de « production » ─────────────────
NID = database.db_add_node("z8", "127.0.0.1")
database.db_update_node(NID, docker_network="bobimacvlan",
                        container_net_mode=database.NET_MODE_SITE,
                        container_net_shim_ip="192.0.2.9")
for vmid, nom in ((2201, "decklink-io"), (2202, "pyramide")):
    database.db_upsert_container_docker(vmid, nom, NID, "bobi-%d" % vmid)
    database.db_update_docker_ip(vmid, "192.0.2.%d" % (vmid - 2100))
    database.db_update_deploy_config(vmid, "decklink_io" if vmid == 2201 else "multiview",
                                     {"shm_out": "sortie-%d" % vmid})
    database.db_update_spec_sig(vmid, "sig-%d" % vmid)

NODE = database.db_get_node(NID)


def _etat(vmid):
    return database.db_get_container(vmid) or {}


UUID_AVANT = {v: _etat(v).get("instance_uuid") for v in (2201, 2202)}
# ★ La colonne est `runtime_spec_sig`, pas `spec_sig` : la première version de cette garde lisait
# un nom qui n'existe pas, donc toujours vide, donc un contrôle VERT QUI NE PROUVAIT RIEN (la
# mutation « signature conservée » ne mordait pas). On vérifie donc que le décor est bien posé.
assert all(_etat(v).get("runtime_spec_sig") for v in (2201, 2202)), \
    "décor invalide : la signature de spec n'est pas posée, le contrôle 2g ne mesurerait rien"

print("\n1. ★ Sans `confirme`, le serveur REFUSE — et n'a rien touché")
code, charge = cn.appliquer_mode_interne(NODE, NID, {"subnet": "172.30.0.0/24"})
verifie(code == 409 and charge.get("confirmation_requise") is True,
        "1a. ★ 409 + confirmation_requise (une confirmation qui ne vit que dans le navigateur ne "
        "protège rien : c'est le serveur qui détruit)", str(code) + " " + str(charge)[:160])
verifie(charge.get("conteneurs") == [2201, 2202],
        "1b. le refus NOMME les conteneurs qui vont tomber", str(charge.get("conteneurs")))
verifie(all(_etat(v).get("docker_ip") for v in (2201, 2202)),
        "1c. ★ aucune destruction avant l'accord : les conteneurs ont toujours leur adresse")
verifie(not any("docker rm" in c for c in _hote["cmds"]),
        "1d. ★ et l'hôte n'a reçu aucun retrait de conteneur", str(_hote["cmds"])[:160])
verifie(database.db_get_node(NID).get("container_net_mode") == database.NET_MODE_SITE,
        "1e. le mode du nœud n'a pas changé non plus")

print("\n2. ★★ Avec `confirme`, les conteneurs sont DÉTACHÉS, pas effacés")
_hote["cmds"] = []
code, charge = cn.appliquer_mode_interne(NODE, NID, {"subnet": "172.30.0.0/24",
                                                     "confirme": True, "redeployer": False})
verifie(code == 200 and charge.get("mode") == database.NET_MODE_INTERNE,
        "2a. la bascule aboutit", str(code) + " " + str(charge)[:160])
verifie(sorted(charge.get("containers_detached") or []) == [2201, 2202],
        "2b. elle rend la liste des conteneurs détachés", str(charge.get("containers_detached")))
for v in (2201, 2202):
    c = _etat(v)
    verifie(bool(c), "2c. ★★ le conteneur %d est TOUJOURS EN BASE (c'est ce qui a été perdu "
                     "le 28/10)" % v)
    verifie(bool(c.get("deploy_config")) and "shm_out" in (c.get("deploy_config") or ""),
            "2d. ★★ son `deploy_config` est intact : la configuration de production ne se "
            "reconstruit pas de mémoire (%d)" % v, str(c.get("deploy_config"))[:80])
    verifie(c.get("instance_uuid") == UUID_AVANT[v] and UUID_AVANT[v],
            "2e. ★ son identité d'INSTANCE survit (NMOS, vues de projet, macros) (%d)" % v,
            "%r → %r" % (UUID_AVANT[v], c.get("instance_uuid")))
    verifie(not (c.get("docker_ip") or ""),
            "2f. son IP est effacée — elle appartenait au réseau d'avant, et la garder ferait "
            "sonder une adresse d'un subnet qui n'existe plus (%d)" % v, repr(c.get("docker_ip")))
    verifie(not (c.get("runtime_spec_sig") or ""),
            "2g. ★ sa signature de spec est effacée : sans ça le prochain déploiement le croirait "
            "conforme et ne le recréerait pas sur le nouveau réseau (%d)" % v,
            repr(c.get("runtime_spec_sig")))
verifie(sum(1 for c in _hote["cmds"] if "docker rm -f" in c) >= 2,
        "2h. l'hôte a bien retiré les conteneurs (arrêt gracieux puis rm)", str(len(_hote["cmds"])))
verifie(any("docker stop -t" in c for c in _hote["cmds"]),
        "2i. arrêt gracieux avant le retrait (le script rend ses mappings MXL)")
verifie((database.db_get_node(NID) or {}).get("container_net_shim_ip") == "",
        "2j. le shim est retiré : sur un bridge, l'hôte joint ses conteneurs nativement")

print("\n3. Le redéploiement est LANCÉ, et chaque issue est alertée")
# `redeployer_conteneurs` est appelé en tâche de fond par la route ; on l'appelle ici
# directement, avec `start_compute` doublé — le but est de tenir le contrat (tous les
# conteneurs tentés, échec isolé, alerte dans les deux cas), pas de déployer pour de vrai.
from app import docker_compute                                                 # noqa: E402

_tentes = []
docker_compute.start_compute = lambda v: (_tentes.append(v), v != 2202)[1]
_avant = len(database.db_get_alerts(node_id=NID) or [])
ok, ko = cn.redeployer_conteneurs([2201, 2202], "garde")
verifie(_tentes == [2201, 2202],
        "3a. ★ TOUS les conteneurs sont tentés — un échec sur l'un ne doit pas priver les autres "
        "de leur redéploiement", str(_tentes))
verifie(ok == [2201] and ko == [2202],
        "3b. le verdict distingue ce qui est remonté de ce qui est resté à terre",
        "%r %r" % (ok, ko))
_cles = [a.get("message") for a in (database.db_get_alerts(vmid=2202) or [])]
verifie(any("NON redéployé" in (m or "") for m in _cles),
        "3c. ★ l'échec est ALERTÉ : un conteneur resté à terre après une bascule serait sinon un "
        "trou silencieux dans la production", str(_cles)[:200])
verifie(any("redéployé sur le nouveau réseau" in (a.get("message") or "")
            for a in (database.db_get_alerts(vmid=2201) or [])),
        "3d. le succès l'est aussi (le journal doit raconter la bascule en entier)")

print("\n4. Le constat d'attachement se fait sur DOCKER, pas sur la base")
verifie(cn.conteneurs_attaches(NODE, NID, "bobimacvlan") == [2201, 2202],
        "4a. les attachés sont lus dans l'inspection du réseau")
verifie(cn.conteneurs_attaches(NODE, NID, "reseau-inexistant") == [],
        "4b. un réseau absent ne fait tomber personne")
_ATTACHES["bobimacvlan"] = ["bobi-2201", "bobi-9999"]
verifie(cn.conteneurs_attaches(NODE, NID, "bobimacvlan") == [2201],
        "4c. un conteneur attaché mais inconnu de la base n'est pas inventé (il sera retiré de "
        "l'hôte, mais il n'y a rien à conserver)",
        str(cn.conteneurs_attaches(NODE, NID, "bobimacvlan")))
_ATTACHES["bobimacvlan"] = ["bobi-2201", "bobi-2202"]
AUTRE = database.db_add_node("autre", "192.0.2.80")
database.db_upsert_container_docker(2301, "ailleurs", AUTRE, "bobi-2201")
verifie(cn.conteneurs_attaches(NODE, NID, "bobimacvlan") == [2201, 2202],
        "4d. ★ un conteneur d'un AUTRE nœud portant le même nom Docker ne compte pas (les noms "
        "ne sont uniques que par hôte)", str(cn.conteneurs_attaches(NODE, NID, "bobimacvlan")))

print("\n5. Rien à perdre = rien à confirmer")
NID2 = database.db_add_node("neuf", "127.0.0.1")
database.db_update_node(NID2, container_net_mode=database.NET_MODE_SITE)
code, charge = cn.appliquer_mode_interne(database.db_get_node(NID2), NID2,
                                         {"subnet": "172.30.0.0/24"})
verifie(code == 200,
        "5a. un nœud sans conteneur attaché bascule sans confirmation : exiger un accord pour un "
        "geste sans coût, c'est apprendre à l'exploitant à confirmer sans lire",
        str(code) + " " + str(charge)[:120])

print("\n6. ★ Le trou SYMÉTRIQUE : revenir au réseau du site (interne → site)")
# Le chemin macvlan calculait sa divergence sur le NOUVEAU nom (« bobimacvlan ») — qui n'existe
# pas encore quand on revient du mode interne. Il ne voyait donc aucun conteneur, et ceux
# attachés à « bobi-interne » tombaient sans avoir été annoncés : le même défaut que le chemin
# interne, par l'autre bout. Ici on appelle la ROUTE, parce que c'est là que vit ce gate.
import flask                                                                   # noqa: E402
from app.routes import node_network as rnn                                     # noqa: E402

NID3 = database.db_add_node("retour", "127.0.0.1")
database.db_update_node(NID3, docker_network="bobi-interne", agent_url="http://127.0.0.1:9100",
                        container_net_mode=database.NET_MODE_INTERNE)
for vmid, nom in ((2401, "console"), (2402, "player")):
    database.db_upsert_container_docker(vmid, nom, NID3, "bobi-%d" % vmid)
    database.db_update_deploy_config(vmid, "audio_console", {"shm_out": "s-%d" % vmid})
_ATTACHES.clear()
_ATTACHES["bobi-interne"] = ["bobi-2401", "bobi-2402"]     # le nouveau nom n'existe PAS encore
rnn._parent_declare = lambda nid: "eno1"
rnn._macvlan_suggest = lambda nid=None: {"name": "bobimacvlan", "subnet": "192.0.2.0/24",
                                         "gateway": "192.0.2.254", "ip_range": ""}
app_test = flask.Flask(__name__)
with app_test.test_request_context(json={"mode": "site"}):
    rep, code = rnn.api_node_container_network_set.__wrapped__(NID3)
charge = rep.get_json()
verifie(code == 409 and charge.get("confirmation_requise") is True,
        "6a. ★ retour au réseau du site : refusé sans `confirme`, alors que le nouveau réseau "
        "n'existe pas encore et qu'aucune divergence ne pouvait le signaler",
        str(code) + " " + str(charge)[:160])
verifie(charge.get("conteneurs") == [2401, 2402],
        "6b. et le refus nomme les conteneurs de l'ANCIEN réseau",
        str(charge.get("conteneurs")))
verifie(all(database.db_get_container(v) for v in (2401, 2402)),
        "6c. rien n'a été touché")


print("\n" + ("─" * 70))
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ bascule réseau : refus sans accord explicite, configuration de production conservée, "
      "redéploiement tenté et alerté")
