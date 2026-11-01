#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Réseau des conteneurs : le produit MESURE au lieu d'AFFIRMER, et dit ce qu'il va détruire.

★ L'INCIDENT QUE CE BANC TIENT FERMÉ (nouvelle Z8 tout-en-un, 2026-10-30).
Installation neuve, réseau interne `bobi-interne` en 172.30.0.0/24 créé par l'installeur et
fonctionnel. Un exploitant a voulu le passer sur un autre subnet — celui qu'il se rappelait avoir
saisi à l'installation, qui était en réalité le POOL D'IP CLUSTER, inerte en mode interne. Au
bout de la chaîne : plus aucun réseau Docker, et la base qui continuait d'affirmer le contraire
pendant deux heures.

(Les adresses de ce banc sont des plages de DOCUMENTATION — RFC 5737, 192.0.2.x et
198.51.100.x. Le dépôt est public : une adresse du site y serait à la fois un renseignement
offert et un contresens pour le lecteur. Garde : `tests/check_adresses_site.py`, qui ne scanne
que les fichiers SUIVIS par git — donc un fichier neuf y échappe jusqu'à son premier commit.)

Six défauts se sont additionnés, et c'est chacun d'eux qu'on mesure ici :

1. Rien ne mesurait l'EXISTENCE du réseau. `etat_reseau_conteneurs` ne lisait que l'option
   `parent` — absente d'un bridge — donc « réseau interne en place » et « réseau absent »
   rendaient la même chose. La base faisait foi.
2. `allinone` était calculé par `host in controller_ipv4s()`. Un tout-en-un est enrôlé sous
   127.0.0.1, et la boucle locale ne figure JAMAIS dans `controller_ipv4s()` : la comparaison
   était donc fausse sur exactement le cas qu'elle devait reconnaître.
3. Nos propres interfaces (`bobi-shim`, `bobi-interne`) étaient présentées comme des cartes du
   nœud, étiquetées « Non utilisée » — la seule étiquette qui INVITE à s'en servir. C'est la
   cause humaine de l'incident. Et symétriquement, le rôle « Containers » d'une carte était
   affiché actif alors qu'il est INERTE en mode interne.
4. Le gate de confirmation était indexé sur les CONTENEURS QUI TOMBENT : zéro conteneur
   attaché ⇒ aucune confirmation, alors que détruire le seul réseau d'un nœud est un coût en soi.
5. En cas d'échec de recréation, rien n'était remis d'accord et rien n'était alerté : la base
   gardait le nom du réseau disparu, et le seul témoin de la panne était un toast dans le
   navigateur de celui qui avait cliqué.
6. Le panneau postait le champ du sous-réseau INTERNE même en mode site, et la passerelle venait
   des réglages CLUSTER : deux valeurs incompatibles, que Docker refuse APRÈS la destruction.

Hors ligne : base JETABLE posée avant tout accès, aucun réseau, aucun Docker, aucun SSH.
"""
import ast
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

# Base jetable AVANT tout accès : la connexion SQLite est par THREAD, après c'est trop tard.
_TMP = tempfile.mkdtemp(prefix="verif_net_etat_")
config.DB_PATH = database.DB_PATH = os.path.join(_TMP, "t.db")
database.init_db()

from app import container_net as cn, node_driver, settings                     # noqa: E402
from app.routes import node_network as nn                                      # noqa: E402
from flask import Flask                                                        # noqa: E402

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


_app = Flask(__name__)


def appel(vue, **kw):
    """Appelle une vue Flask hors requête → (json, code)."""
    with _app.test_request_context(json=kw.pop("json", None) or {}):
        out = vue.__wrapped__(**kw) if hasattr(vue, "__wrapped__") else vue(**kw)
    if isinstance(out, tuple):
        return out[0].get_json(), out[1]
    return out.get_json(), 200


# ─── Doublures : l'hôte répond ce que LE TEST décide, et on garde la trace des commandes ─────
# `inspect_reseau`, `conteneurs_attaches` et `etat_reseau_conteneurs` passent tous par
# `host_exec` : une seule doublure suffit, et c'est elle qui rend le banc capable de distinguer
# « réseau présent » de « réseau absent » sans Docker.
CMDS = []
RESEAUX = {}          # {nom: dict Docker} — ce que `docker network inspect` est censé rendre
HOST_LEVE = False     # True → l'hôte est injoignable (pour le tri-état « non mesuré »)


def _host_exec(node, cmd, timeout=None):
    CMDS.append(cmd)
    if HOST_LEVE:
        raise OSError("hôte injoignable (simulé)")
    import json as _json
    # Sonde de `etat_reseau_conteneurs` : « n=<nom>; … echo "$pr|$p|$c" »
    mn = re.search(r"^n=(\S+);", cmd)
    if mn and 'echo "$pr|$p|$c"' in cmd:
        nom = mn.group(1).strip("'\"")
        net = RESEAUX.get(nom)
        parent = ((net or {}).get("Options") or {}).get("parent") or ""
        return (0, "%s|%s|%s\n" % (1 if net else 0, parent, "1" if parent else ""), "")
    # `inspect_reseau` : « docker network inspect <nom> 2>/dev/null »
    mi = re.search(r"docker network inspect (\S+) 2>/dev/null\s*$", cmd)
    if mi:
        nom = mi.group(1).strip("'\"")
        net = RESEAUX.get(nom)
        return (0, _json.dumps([net]) if net else "", "")
    if "network rm" in cmd:
        for nom in list(RESEAUX):
            if nom in cmd:
                RESEAUX.pop(nom, None)
        return (0, "", "")
    return (0, "", "")


ENSURE = {"ok": True}


def _ensure_network(node, name, parent="", subnet=None, gateway=None, ip_range=None,
                    driver="macvlan"):
    if not ENSURE["ok"]:
        return False, ENSURE.get("err", "refus simulé")
    RESEAUX[name] = {"Driver": driver, "Options": ({"parent": parent} if parent else {}),
                     "IPAM": {"Config": [{"Subnet": subnet or "", "Gateway": gateway or ""}]},
                     "Containers": {}}
    return True, {"driver": driver, "name": name}


node_driver.host_exec = _host_exec
node_driver.ensure_network = _ensure_network
node_driver.capabilities = lambda node: {"nics": [], "networks": []}
nn.node_driver = node_driver

NID = database.db_add_node("z8", "127.0.0.1", kind="docker")
database.db_update_node(NID, agent_url="http://127.0.0.1:9100", agent_version="0.30.1")
settings.set("ip_start", "198.51.100.1")
settings.set("ip_end", "198.51.100.200")
settings.set("gateway", "192.0.2.254")        # ★ passerelle du SITE, hors du pool : le cas réel
settings.set("subnet", "198.51.100.0/24")
settings.set("net_mode", "static")


def noeud():
    return database.db_get_node(NID)


print("1. ★ Le réseau EXISTE-T-IL ? — mesuré, et en tri-état")
database.db_update_node(NID, docker_network="bobi-interne", container_net_mode="interne",
                        container_net_subnet="172.30.0.0/24")
RESEAUX["bobi-interne"] = {"Driver": "bridge", "Options": {},
                           "IPAM": {"Config": [{"Subnet": "172.30.0.0/24",
                                                "Gateway": "172.30.0.1"}]}, "Containers": {}}
node_driver._reseau_cache.clear()
_e = node_driver.etat_reseau_conteneurs(noeud(), force=True)
verifie(_e.get("present") is True,
        "1a. ★ réseau en place → `present` vaut True. Un bridge n'a PAS d'option `parent` : "
        "avant, « interne en place » et « absent » rendaient tous deux reel='' — rien, dans "
        "tout le produit, ne mesurait l'existence du réseau des conteneurs", str(_e))
verifie(_e.get("porteuse") is None and _e.get("reel") == "",
        "1b. ★ et un `parent` VIDE ne DÉCALE pas la porteuse : les trois champs sont séparés par "
        "« | », pas par des lignes filtrées (même leçon que la sonde de placement)", str(_e))
RESEAUX.pop("bobi-interne")
node_driver._reseau_cache.clear()
verifie(node_driver.etat_reseau_conteneurs(noeud(), force=True).get("present") is False,
        "1c. réseau absent → `present` vaut False, pas None")
HOST_LEVE = True
node_driver._reseau_cache.clear()
verifie(node_driver.etat_reseau_conteneurs(noeud(), force=True).get("present") is None,
        "1d. ★ hôte injoignable → None, JAMAIS False : « on ne sait pas » ne doit pas pouvoir "
        "être lu comme « absent »")
HOST_LEVE = False
RESEAUX["bobi-interne"] = {"Driver": "bridge", "Options": {},
                           "IPAM": {"Config": [{"Subnet": "172.30.0.0/24",
                                                "Gateway": "172.30.0.1"}]}, "Containers": {}}


print("\n2. ★ « tout-en-un » : la boucle locale compte")
_p = nn._cn_mode_payload(noeud(), NID, {"subnet": "198.51.100.0/24"})
verifie(_p.get("allinone") is True,
        "2a. ★★★ un nœud enrôlé sous 127.0.0.1 EST un tout-en-un. L'ancien calcul "
        "(`host in controller_ipv4s()`) était faux sur exactement ce cas — la boucle locale n'y "
        "figure jamais — donc le bloc du shim et sa ligne de contrôle ne s'affichaient sur aucun "
        "tout-en-un installé normalement", str(_p.get("allinone")))
_src_nn = open(os.path.join(RACINE, "app", "routes", "node_network.py"), encoding="utf-8").read()
_fn_pay = next(n for n in ast.parse(_src_nn).body
               if isinstance(n, ast.FunctionDef) and n.name == "_cn_mode_payload")
_val_allinone = next((v for n in ast.walk(_fn_pay) if isinstance(n, ast.Dict)
                      for k, v in zip(n.keys, n.values)
                      if isinstance(k, ast.Constant) and k.value == "allinone"), None)
verifie(_val_allinone is not None and "est_tout_en_un" in ast.dump(_val_allinone),
        "2b. et il passe par `est_tout_en_un`, la définition UNIQUE — pas par une comparaison "
        "d'IP réécrite sur place (c'est la duplication qui avait laissé le défaut survivre)")
verifie(_p.get("mode") == "interne",
        "2c. la charge utile porte le MODE : sans lui le panneau croyait tout nœud en « site »",
        str(_p.get("mode")))


print("\n3. ★ Les étiquettes de la page Interfaces ne mentent plus")
nn._fetch_host_nics = lambda host: (True, "", [
    {"name": "eno1", "mac": "48:9e:bd:9c:4b:a4", "link": True, "operstate": "up",
     "ipv4": [{"local": "192.0.2.31", "prefix": 24}], "pci": "0000:00:1f.6"},
    {"name": "bobi-interne", "mac": "02:42:38:11:7c:21", "link": False, "operstate": "down",
     "ipv4": [{"local": "172.30.0.1", "prefix": 24}]},
    {"name": "bobi-shim", "mac": "9a:11:22:33:44:55", "link": True, "operstate": "up",
     "ipv4": [{"local": "198.51.100.253", "prefix": 32}]},
])
database.db_upsert_node_interface(NID, "eno1", role="mgmt_containers",
                                  ip_cidr="192.0.2.31/24", gateway="192.0.2.254")
_j, _code = appel(nn.api_node_interfaces, node_id=NID)
_par = {r["ifname"]: r for r in (_j.get("interfaces") or _j.get("ifaces") or [])} if _code == 200 else {}
if not _par:            # selon la forme de la réponse, retrouver la liste quelle que soit sa clé
    for v in (_j or {}).values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "ifname" in v[0]:
            _par = {r["ifname"]: r for r in v}
            break
verifie(_par.get("bobi-interne", {}).get("a_nous") is True
        and _par["bobi-interne"].get("a_nous_quoi") == "reseau",
        "3a. ★★★ `bobi-interne` est marquée « à nous », et nommée pour ce qu'elle est. Avant, "
        "faute de ligne en base, elle recevait role='unused' → badge « Non utilisée » : la seule "
        "étiquette qui INVITE à s'en servir. C'est ce qui a déclenché l'incident du 30/10",
        str(_par.get("bobi-interne")))
verifie(_par.get("bobi-shim", {}).get("a_nous_quoi") == "shim",
        "3b. et le shim est distingué du pont du réseau interne (ils n'ont pas la même raison "
        "d'être, donc pas la même phrase)", str(_par.get("bobi-shim", {}).get("a_nous_quoi")))
verifie(_par.get("eno1", {}).get("a_nous") is False,
        "3c. témoin : une VRAIE carte n'est pas marquée « à nous »")
verifie(_par.get("eno1", {}).get("role_ct_inerte") is True,
        "3d. ★ en mode interne, le rôle « Containers » d'eno1 est INERTE — les conteneurs sont "
        "sur le pont privé. L'afficher actif fait chercher un défaut là où il n'y en a pas",
        str(_par.get("eno1")))
database.db_update_node(NID, container_net_mode="site")
_j2, _ = appel(nn.api_node_interfaces, node_id=NID)
_par2 = {}
for v in (_j2 or {}).values():
    if isinstance(v, list) and v and isinstance(v[0], dict) and "ifname" in v[0]:
        _par2 = {r["ifname"]: r for r in v}
        break
verifie(_par2.get("eno1", {}).get("role_ct_inerte") is False,
        "3e. ★ et en mode SITE le même rôle est bien actif : le drapeau suit le mode, il n'est "
        "pas posé en dur (une constante éteindrait l'information en laissant la clé)",
        str(_par2.get("eno1", {}).get("role_ct_inerte")))
database.db_update_node(NID, container_net_mode="interne")


print("\n4. ★★★ La CONFIRMATION porte sur la destruction, pas sur les conteneurs")
verifie(database.db_get_containers() == [],
        "4a. témoin : aucun conteneur en base — c'est le cas où l'ancien gate se taisait")
CMDS.clear()
_code, _ch = cn.appliquer_mode_interne(noeud(), NID, {"subnet": "198.51.100.0/24"})
verifie(_code == 409 and _ch.get("confirmation_requise"),
        "4b. ★★★ changer le subnet DÉTRUIT le réseau : refusé sans accord explicite, même avec "
        "zéro conteneur attaché. C'est le trou par lequel la Z8 a perdu son réseau le 30/10",
        "%s %s" % (_code, _ch))
verifie(_ch.get("detruit_reseau") == "bobi-interne",
        "4c. et le refus NOMME le réseau qui serait détruit — « êtes-vous sûr ? » sans dire ce "
        "qu'on perd ne fait prendre aucune décision", str(_ch.get("detruit_reseau")))
verifie("bobi-interne" in RESEAUX and not any("network rm" in c for c in CMDS),
        "4d. ★ et le refus n'a RIEN détruit : constater ne doit pas déjà agir",
        str([c[:40] for c in CMDS]))
_code, _ch = cn.appliquer_mode_interne(noeud(), NID,
                                       {"subnet": "198.51.100.0/24", "confirme": True})
verifie(_code == 200 and noeud().get("container_net_subnet") == "198.51.100.0/24",
        "4e. avec l'accord, le réglage s'applique (le gate protège, il ne bloque pas)",
        "%s %s" % (_code, _ch))
# Première configuration : le réseau n'existe pas → rien à détruire → RIEN à confirmer. Sans ce
# témoin, un gate trop large bloquerait l'installeur et toute première mise en service.
RESEAUX.clear()
database.db_update_node(NID, docker_network="")
node_driver._reseau_cache.clear()
_code, _ch = cn.appliquer_mode_interne(noeud(), NID, {"subnet": "172.30.0.0/24"})
verifie(_code == 200,
        "4f. ★ première configuration (aucun réseau) : AUCUNE confirmation demandée — sinon "
        "l'installeur, qui n'a pas d'interface pour répondre, échouerait à chaque installation",
        "%s %s" % (_code, _ch))


print("\n5. ★★★ Échec de recréation : la base se remet d'accord, et ça s'ALERTE")
database.db_update_node(NID, docker_network="bobi-interne")
RESEAUX["bobi-interne"] = {"Driver": "bridge", "Options": {},
                           "IPAM": {"Config": [{"Subnet": "172.30.0.0/24",
                                                "Gateway": "172.30.0.1"}]}, "Containers": {}}
_av = len(database.db_get_alerts(limit=500) or [])
ENSURE["ok"] = False
ENSURE["err"] = "gateway 192.0.2.254 not in subnet"
_code, _ch = cn.appliquer_mode_interne(noeud(), NID,
                                       {"subnet": "198.51.100.0/24", "confirme": True})
ENSURE["ok"] = True
verifie(_code == 500, "5a. l'échec est bien un échec (500), pas un succès silencieux", str(_code))
verifie((noeud().get("docker_network") or "") == "",
        "5b. ★★★ la base NE PRÉTEND PLUS porter un réseau disparu. C'est ce qui a rendu la Z8 "
        "illisible deux heures : `docker_network` gardait « bobi-interne » quand Docker n'avait "
        "plus que bridge/host/none, et toutes les pages affichaient « OK »",
        repr(noeud().get("docker_network")))
_al = database.db_get_alerts(limit=500) or []
# ⚠ REPÉRÉE PAR DES DONNÉES, PAS PAR UNE TRADUCTION. Les alertes sont stockées RENDUES : chercher
# un mot français dans le message aurait rendu ce contrôle faux dès qu'une instance tourne en
# anglais. On s'appuie sur ce qui ne se traduit pas — le nom du réseau et la cause de l'agent.
_mien = [a for a in _al if a.get("kind") == "net"
         and "bobi-interne" in (a.get("message") or "")
         and "gateway" in (a.get("message") or "")]
verifie(len(_al) > _av and _mien,
        "5c. ★★ et la panne laisse une TRACE dans le produit : jusqu'ici le seul témoin était un "
        "toast dans le navigateur de celui qui avait cliqué", "%d → %d" % (_av, len(_al)))
verifie(_mien and ENSURE["err"].split()[0] in (_mien[-1].get("message") or ""),
        "5d. l'alerte porte la CAUSE rendue par l'agent, pas seulement « échec » : sans elle, "
        "l'exploitant sait qu'il n'a plus de réseau mais pas pourquoi",
        str((_mien or [{}])[-1].get("message"))[:120])
verifie(_mien and (_mien[-1].get("niveau") == "error"),
        "5e. et au niveau « error » : un nœud sans réseau de conteneurs n'est pas une info")


print("\n6. ★ Passerelle hors du subnet : refusée AVANT de détruire")
database.db_update_node(NID, container_net_mode="site", docker_network="bobimacvlan")
RESEAUX["bobimacvlan"] = {"Driver": "macvlan", "Options": {"parent": "eno1"},
                          "IPAM": {"Config": [{"Subnet": "198.51.100.0/24",
                                               "Gateway": "198.51.100.1"}]}, "Containers": {}}
nn._node_host_ipv4s = lambda host: ["192.0.2.31"]
nn._node_default_gateway = lambda host: "192.0.2.254"
CMDS.clear()
_j, _code = appel(nn.api_node_container_network_set, node_id=NID,
                  json={"mode": "site", "parent": "eno1", "name": "bobimacvlan",
                        "subnet": "172.30.0.0/24", "confirme": True})
verifie(_code == 400 and "hors du subnet" in (_j.get("error") or ""),
        "6a. ★★ subnet 172.30.0.0/24 + passerelle 192.0.2.254 : refusé, et la raison nomme les "
        "DEUX valeurs. Docker les refuse aussi — mais APRÈS notre destruction, avec un message "
        "qui ne dit pas qu'elles sont simplement incompatibles", "%s %s" % (_code, _j))
verifie("bobimacvlan" in RESEAUX and not any("network rm" in c for c in CMDS),
        "6b. ★★★ et le réseau existant est INTACT : le refus arrive avant le destructeur",
        str([c[:40] for c in CMDS]))


print("\n7. L'installeur : accord explicite, et verdict honnête")
_src_i = open(os.path.join(RACINE, "install", "install.py"), encoding="utf-8").read()
_m = re.search(r"appliquer_mode_interne\(n, n\['id'\], (\{[^}]*\})\)", _src_i)
verifie(_m is not None and "confirme" in (_m.group(1) if _m else ""),
        "7a. ★ l'installeur porte `confirme` : le gate de destruction refuserait sinon une "
        "RÉINSTALLATION (la base survit à l'installation, donc un réseau d'avant peut diverger)",
        str(_m and _m.group(1)))
_fn_aio = next((n for n in ast.parse(_src_i).body
                if isinstance(n, ast.FunctionDef) and n.name == "path_all_in_one"), None)
# ⚠ ON MESURE LE BRANCHEMENT, PAS LA PRÉSENCE DU NOM. Première version de ce contrôle :
# « `net_ok` apparaît dans la fonction » — et il apparaissait toujours, puisque l'affectation
# `net_ok, detail = appliquer_reseau_conteneurs(mode)` reste là même si le verdict l'ignore. La
# mutation M11 (retour au verdict d'avant) ne mordait donc pas : garde verte sur un défaut
# rétabli, cf. [[garde-qui-cherche-un-nom-attrape-sa-definition]]. On exige maintenant que la
# branche qui annonce « prêt » soit GARDÉE par une condition où `net_ok` figure.
_porteurs = [x for x in ast.walk(_fn_aio) if isinstance(x, (ast.If, ast.IfExp))
             and any(isinstance(y, ast.Constant) and y.value == "aio_ready"
                     for y in ast.walk(x))] if _fn_aio else []
_gardes = {y.id for x in _porteurs for y in ast.walk(x.test) if isinstance(y, ast.Name)}
verifie("net_ok" in _gardes,
        "7b. ★★ la branche « Tout-en-un prêt » est CONDITIONNÉE à `net_ok`. Le verdict ne "
        "regardait que `noeud_ok` : une installation sans réseau de conteneurs annonçait "
        "« prêt », le `warn` étant noyé quelques lignes plus haut — c'est le défaut (e) que "
        "`register_local_agent` documente pour le nœud, resté en place pour le réseau",
        str(sorted(_gardes)))
_cst_aio = {y.value for y in ast.walk(_fn_aio) if isinstance(y, ast.Constant)
            and isinstance(y.value, str)} if _fn_aio else set()
verifie("aio_partial_net" in _cst_aio,
        "7c. et il le DIT avec sa propre phrase, prononcée DANS cette fonction — une clé "
        "présente au catalogue mais jamais rendue est un libellé mort",
        str(sorted(k for k in _cst_aio if k.startswith("aio"))))


print("\n8. L'interface ne poste plus le subnet interne en mode site")
_tpl = open(os.path.join(RACINE, "templates", "settings.html"), encoding="utf-8").read()
_js = re.split(r"\n(?:async )?function ",
               _tpl.split("async function creerReseauContainers")[1])[0]
verifie(re.search(r"mode\s*===?\s*'interne'\s*\)\s*\?\s*\{[^}]*subnet", _js) is not None,
        "8a. ★★ le subnet n'est envoyé QU'EN mode interne. Le champ reste dans le DOM, caché, "
        "quand le mode est « site » — et il était posté quand même, puis pris en priorité par le "
        "serveur : une reconfiguration en réseau du site partait avec 172.30.0.0/24 et la "
        "passerelle du cluster, deux valeurs que Docker refuse après la destruction",
        _js[:1][:0] or "motif absent")
verifie("corpsNet" in _js and _js.count("corpsNet") >= 2,
        "8b. et la demande d'accord renvoie LE MÊME corps : deux corps construits séparément "
        "divergeraient au premier changement (le second oubliait déjà `subnet` à sa façon)")
verifie(re.search(r"liste\s*\?", _js) is not None,
        "8c. ★ la question de confirmation s'adapte au cas SANS conteneur : « 0 conteneur(s) » "
        "ne voudrait rien dire, c'est le message du serveur qui porte alors le constat")


print("\n9. La charge utile de readiness publie la MESURE, en tri-état")
_fn_r = next((n for n in ast.parse(_src_nn).body
              if isinstance(n, ast.FunctionDef) and n.name == "api_node_readiness"), None)
_val_rp = next((v for n in ast.walk(_fn_r) if isinstance(n, ast.Dict)
                for k, v in zip(n.keys, n.values)
                if isinstance(k, ast.Constant) and k.value == "reel_present"), None)
verifie(_val_rp is not None,
        "9a. ★ `/readiness` publie `reel_present` — le CONSTAT, à côté de `configured` qui ne "
        "dit que « une colonne est remplie »")
verifie(_val_rp is not None
        and not (isinstance(_val_rp, ast.Call)
                 and getattr(_val_rp.func, "id", None) == "bool"),
        "9b. ★★ et il n'est PAS réduit par `bool(...)` : ça ferait passer « non mesuré » pour "
        "« absent », exactement la confusion que le tri-état existe pour empêcher",
        ast.dump(_val_rp)[:80] if _val_rp is not None else "absent")
verifie("settings.net.reseau_absent" in _tpl,
        "9c. et le panneau a une phrase pour « la base l'annonce, Docker ne l'a pas »")


print("\n" + "─" * 70)
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ réseau des conteneurs : mesuré, pas affirmé — et la destruction s'annonce")
