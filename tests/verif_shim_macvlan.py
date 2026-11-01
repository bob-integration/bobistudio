#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Shim macvlan du nœud tout-en-un : il rend les conteneurs joignables SANS casser le LAN.

Ce banc existe pour deux dangers de signes opposés, et le second est le plus grave :

- **Sans shim**, sur un serveur tout-en-un, l'orchestrateur ne joint aucun conteneur (le noyau
  isole une interface parente de ses enfants macvlan) alors que tout est vert. C'est la panne
  rapportée par le testeur le 2026-10-27.
- **Avec un shim mal posé**, on détourne le LAN DU SITE. Deux chemins y mènent, et un banc qui
  n'en garde qu'un laisse l'autre ouvert :
  1. une route vers le SUBNET (`ip route add 192.0.2.0/24 dev bobi-shim`) ;
  2. une ADRESSE au préfixe du subnet (`ip addr add 192.0.2.254/24 dev bobi-shim`), qui crée
     la même route connectée sans jamais écrire le mot « route ».
  Dans les deux cas, le routeur, les pupitres et les postes du site passent par une interface
  macvlan qui ne les sert pas. Le nœud perd son réseau, et rien ne relie le symptôme au geste.

`script_shim()` étant une fonction PURE, on lit ici le script EXACT qui sera exécuté sur le nœud
avant qu'il ne le soit — pas une intention, pas un appel simulé. Hors ligne, base jetable.
"""
import ipaddress
import json
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import database                                                      # noqa: E402

database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_shim_"), "t.db")
database.init_db()

from app import allocations, container_net as cn, settings                     # noqa: E402

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


SUBNET, GW = "192.0.2.0/24", "192.0.2.1"
START, END = "192.0.2.100", "192.0.2.199"
SHIM_IP = "192.0.2.254"


print("\n1. Le script posé sur le nœud — lu avant d'être exécuté")
script, cmd = cn.script_shim("eno1", SHIM_IP, SUBNET, START, END)
lignes = [l.strip() for l in script.splitlines() if l.strip() and not l.startswith("#")]

verifie(any(l.startswith("ip link add %s link eno1 type macvlan mode bridge" % cn.SHIM_IFACE)
            or ("ip link add %s link eno1" % cn.SHIM_IFACE) in l for l in lignes),
        "1a. une interface macvlan de PLUS est posée sur le même parent (mode bridge)",
        script)
verifie(any(l.startswith("ip link show %s" % cn.SHIM_IFACE) and "||" in l for l in lignes),
        "1b. idempotent : l'interface n'est créée que si elle manque (systemd rejoue le script)",
        script)
verifie(all("ip addr add" not in l for l in lignes)
        and any("ip addr replace" in l for l in lignes),
        "1c. idempotent : `addr replace`, pas `addr add` (qui échoue au second passage)", script)
verifie(all("ip route add" not in l for l in lignes),
        "1d. idempotent : aucune `route add` — sous `set -e`, « File exists » tuerait le script "
        "au redémarrage et seuls CERTAINS conteneurs répondraient", script)


print("\n2. ★ Le LAN du site n'est jamais détourné — les deux chemins")
reseau = ipaddress.ip_network(SUBNET)
routes = [l.split()[3] for l in lignes if l.startswith("ip route replace")]
verifie(bool(routes), "2a. des routes sont bien posées vers le shim", str(routes))
verifie(str(reseau) not in routes,
        "2b. ★ CHEMIN 1 : aucune route vers le SUBNET entier (sinon tout le site passe par le shim)",
        str(routes))

adr = [l.split()[3] for l in lignes if l.startswith("ip addr replace")]
verifie(adr and all(a.endswith("/32") for a in adr),
        "2c. ★ CHEMIN 2 : l'adresse du shim est en /32 — un /24 créerait la même route "
        "connectée que 2b, sans écrire le mot « route »", str(adr))

couvert = set()
for r in routes:
    couvert |= {str(i) for i in ipaddress.ip_network(r)}
plage = set(allocations._range_list(START, END))
verifie(couvert == plage,
        "2d. les routes couvrent EXACTEMENT la plage des conteneurs : ni une adresse du site "
        "en trop, ni un conteneur injoignable en moins",
        "en trop=%s / manquant=%s" % (sorted(couvert - plage)[:4], sorted(plage - couvert)[:4]))
verifie(GW not in couvert and "192.0.2.5" not in couvert,
        "2e. ni la passerelle du segment ni un hôte du site ne tombent dans le shim")
verifie(all(("src %s" % SHIM_IP) in l for l in lignes if l.startswith("ip route replace")),
        "2f. source explicite : sans elle le noyau sourcerait depuis la carte PARENTE, qu'un "
        "conteneur ne peut pas joindre — l'aller partirait, le retour serait jeté", script)


print("\n3. Persistance : sinon le symptôme revient au prochain redémarrage")
verifie("systemd/system/%s.service" % cn.SHIM_UNIT in cmd and "systemctl enable" in cmd,
        "3a. une unité systemd oneshot est écrite ET activée", cmd[:200])
verifie("RemainAfterExit=yes" in cmd and "Type=oneshot" in cmd,
        "3b. oneshot + RemainAfterExit (le script sort, l'état reste)")
verifie("After=network-pre.target docker.service" in cmd,
        "3c. ordonnée après le réseau et Docker (le parent doit exister, et le réseau conteneurs)")
verifie("chmod +x %s" % cn.SHIM_SCRIPT in cmd and ("sh %s" % cn.SHIM_SCRIPT) in cmd,
        "3d. le script est rendu exécutable et appliqué TOUT DE SUITE, pas seulement au boot")

ret = cn.script_retrait_shim()
verifie(("ip link del %s" % cn.SHIM_IFACE) in ret and "systemctl disable" in ret
        and cn.SHIM_SCRIPT in ret,
        "3e. le retrait défait les trois objets : unité, script, interface", ret)
verifie(ret.count("|| true") >= 3,
        "3f. le retrait ne tombe pas sur ce qui a déjà disparu (sinon moitié d'interface, "
        "moitié de routes : un trou noir muet)", ret)


print("\n4. Qui a besoin d'un shim — et qui n'en a pas besoin")
settings.set("ip_start", START)
settings.set("ip_end", END)
settings.set("gateway", GW)
settings.set("net_mode", "static")

# ★ L'HÔTE EST « 127.0.0.1 », PAS UNE ADRESSE PLAUSIBLE. C'est la valeur que l'INSTALLEUR
# écrit (`install/install.py` : `register('127.0.0.1', 9100, …)`), donc celle que porte tout
# tout-en-un réellement installé. Mes premiers cas employaient « 192.0.2.5 » — une adresse
# inventée, vraisemblable, et que le produit ne produit jamais : le défaut est passé sous le
# banc et a été trouvé après déploiement par la session Z8 le 2026-10-27. Les deux fonctions
# comparaient l'hôte aux IP du contrôleur, où la boucle locale ne figure JAMAIS : tout
# tout-en-un normal était classé « orchestrateur ailleurs », sans shim et mode interne refusé —
# le cas même que ce chantier devait réparer.
n_aio = database.db_add_node("tout-en-un", "127.0.0.1", kind="docker")
n_dist = database.db_add_node("distant", "192.0.2.8", kind="docker")
CTRL = ["192.0.2.231", "172.17.0.1"]    # les IP RÉELLES d'un contrôleur : pas de boucle locale

verifie(cn.shim_requis(database.db_get_node(n_aio), controller_ips=CTRL) is True,
        "4a. ★ nœud enrôlé sous 127.0.0.1 (ce que fait l'installeur) : shim REQUIS — c'est le "
        "cas du testeur, et c'est celui qui échouait en production")
for _h in ("127.0.0.1", "127.0.1.1", "::1", "localhost"):
    verifie(cn.est_tout_en_un({"host": _h}, controller_ips=CTRL) is True,
            "4a bis. « %s » vaut tout-en-un : si le contrôleur joint l'agent par la boucle "
            "locale, l'agent EST sur sa machine" % _h)
verifie(cn.est_tout_en_un({"host": "192.0.2.231"}, controller_ips=CTRL) is True,
        "4a ter. et une IP du contrôleur compte toujours (nœud enrôlé par son adresse réelle)")
verifie(cn.shim_requis(database.db_get_node(n_dist), controller_ips=CTRL) is False,
        "4b. nœud distant : pas de shim — l'isolation ne gêne personne, et un shim masquerait "
        "un vrai défaut de routage")
database.db_update_node(n_aio, container_net_mode="interne")
verifie(cn.shim_requis(database.db_get_node(n_aio), controller_ips=CTRL) is False,
        "4c. mode interne : pas de shim — un bridge est joint nativement par l'hôte")
database.db_update_node(n_aio, container_net_mode="site")


print("\n4 bis. Un pool d'IP qui n'est pas sur le subnet de la carte est DIT, pas corrigé")
# Reproduction exacte de la Z8 (27/10) : carte conteneurs en 192.0.2.0/24, pool en 192.168.1.x.
database.db_upsert_node_interface(n_aio, ifname="eno1", role=database.ROLE_MGMT_CONTAINERS,
                                  ip_cidr="192.0.2.231/24", gateway="192.0.2.254")
carte, ifn, pool = cn.pool_hors_subnet(n_aio, "192.168.1.0/24")
verifie(pool == "192.168.1.0/24",
        "4e bis. ★ le constat NOMME AUSSI le pool proposé : sans lui, un lecteur de l'API voit "
        "« incohérent » sans les deux termes de l'incohérence (relevé par Z8 le 27/10)",
        str(pool))
verifie(carte == "192.0.2.0/24" and ifn == "eno1",
        "4f. ★ l'incohérence est détectée et NOMME les deux subnets — « Reconfigurer » aurait "
        "détruit un réseau qui marche pour le recréer là où le nœud n'est pas",
        "%s / %s" % (carte, ifn))
verifie(cn.pool_hors_subnet(n_aio, "192.0.2.0/24") == (None, None, None),
        "4g. un pool SUR le subnet de la carte ne déclenche rien")
verifie(cn.pool_hors_subnet(n_aio, "") == (None, None, None),
        "4h. pool illisible : silencieux (ce n'est pas CE défaut-là qu'on diagnostique ici)")
verifie(cn.pool_hors_subnet(n_dist, "192.168.1.0/24") == (None, None, None),
        "4i. nœud sans carte conteneurs renseignée : rien à comparer, rien d'affirmé")


print("\n4 ter. Ce que l'interface LIT doit être ce que l'API REND")
# ★ Le trou que ce contrôle ferme : un diagnostic calculé mais jamais mis dans la réponse. Le
# panneau ne l'afficherait pas, le banc de rendu resterait vert (il reçoit une charge FABRIQUÉE
# à la main), et la fonction serait testée sans être branchée — un calcul juste que personne ne
# voit. On lit donc les clés du `jsonify` de la route, par AST.
import ast as _ast                                                             # noqa: E402

_src = open(os.path.join(RACINE, "app", "routes", "node_network.py"), encoding="utf-8").read()
_fn = next(n for n in _ast.parse(_src).body
           if isinstance(n, _ast.FunctionDef) and n.name == "_cn_mode_payload")
_cles = {k.value for n in _ast.walk(_fn) if isinstance(n, _ast.Dict)
         for k in n.keys if isinstance(k, _ast.Constant) and isinstance(k.value, str)}
for _c in ("mode", "subnet_interne", "shim_ip", "shim_requis", "shim_etat",
           "interne_possible", "interne_refus", "interne_avertissement", "pool_hors_subnet"):
    verifie(_c in _cles, "4j. la réponse de l'API porte « %s »" % _c, str(sorted(_cles))[:200])

# ★ ET QUE LA CLÉ SOIT CALCULÉE, pas figée. Vérifier la seule PRÉSENCE de la clé laissait passer
# une valeur remplacée par `None` : le champ existe, l'interface lit « rien à signaler », et le
# diagnostic est mort sans que rien ne l'indique (mutation Q5, qui ne mordait pas). On exige donc
# que la valeur ne soit pas une constante — même famille que
# [[garde-qui-cherche-un-nom-attrape-sa-definition]] : viser ce qui FAIT, pas ce qui se nomme.
_valeurs = {k.value: v for n in _ast.walk(_fn) if isinstance(n, _ast.Dict)
            for k, v in zip(n.keys, n.values)
            if isinstance(k, _ast.Constant) and isinstance(k.value, str)}
for _c in ("shim_etat", "interne_possible", "interne_refus", "pool_hors_subnet"):
    _v = _valeurs.get(_c)
    verifie(_v is not None and not isinstance(_v, _ast.Constant),
            "4k. « %s » est CALCULÉ, pas une constante (une valeur figée éteint le diagnostic "
            "en laissant la clé en place)" % _c,
            type(_v).__name__)

# ★★★ ET QUE LES DEUX ROUTES LA SERVENT (ajouté le 2026-10-30, après la nouvelle Z8).
# Jusqu'ici cette section ne lisait QUE `api_node_container_network_get` — qui publiait tout
# correctement. Or AUCUN appelant de l'interface n'utilise cette route : le panneau
# (`_secNetwork`) est nourri par `/readiness`, qui ne portait aucun de ces champs. `cn.mode`
# valait donc `undefined`, le sélecteur affichait « Réseau du site » sur un nœud en mode interne,
# et le bloc du shim — gaté sur `cn.shim_requis` — ne s'affichait JAMAIS, sur aucun nœud. La
# garde était verte pendant tout ce temps : elle mesurait le PRODUCTEUR et ignorait le LECTEUR,
# cf. [[garde-sur-le-producteur-ne-prouve-pas-le-lecteur]].
for _route in ("api_node_container_network_get", "api_node_readiness"):
    _r = next((n for n in _ast.parse(_src).body
               if isinstance(n, _ast.FunctionDef) and n.name == _route), None)
    verifie(_r is not None and any(
        isinstance(y, _ast.Call)
        and (getattr(y.func, "attr", None) or getattr(y.func, "id", None)) == "_cn_mode_payload"
        for y in _ast.walk(_r)),
        "4l. ★★★ « %s » sert la MÊME source de mode/shim (`_cn_mode_payload`) — une route qui "
        "publie bien ne prouve pas qu'un lecteur la lise" % _route)


print("\n5. L'IP du shim ne peut pas être donnée à un conteneur")
suggestion = cn.shim_ip_suggeree(n_aio, SUBNET, START, END)
verifie(suggestion == SHIM_IP,
        "5a. la suggestion part du HAUT du subnet, hors de la plage des conteneurs",
        str(suggestion))
verifie(suggestion not in allocations._range_list(START, END),
        "5b. et elle n'est jamais DANS la plage", str(suggestion))

# ★ LE CAS QUI DISTINGUE. Avec une plage qui s'arrête à .199, le haut du subnet est libre de
# toute façon : retirer l'exclusion de la plage ne changerait rien et la mutation ne mordrait
# pas. Il faut une plage qui monte jusqu'en HAUT du subnet — ce qui est la configuration d'un
# exploitant qui veut le maximum de conteneurs — pour que l'exclusion ait quelque chose à faire.
sugg_haute = cn.shim_ip_suggeree(n_aio, SUBNET, "192.0.2.100", "192.0.2.254")
verifie(sugg_haute is not None
        and sugg_haute not in allocations._range_list("192.0.2.100", "192.0.2.254"),
        "5a bis. ★ plage montant jusqu'en haut du subnet : la suggestion DESCEND sous la plage "
        "au lieu de piocher une adresse promise aux conteneurs", str(sugg_haute))

database.db_update_node(n_aio, container_net_shim_ip=SHIM_IP)
verifie(SHIM_IP in allocations._reserved_ips(n_aio),
        "5c. ★ une fois posée, l'IP du shim est RÉSERVÉE — un conteneur prenant la même "
        "ferait deux machines à la même IP sur le LAN")
verifie(SHIM_IP in allocations._reserved_ips(n_dist),
        "5d. réservée pour TOUS les nœuds du subnet, pas seulement le sien")

# Plage élargie jusqu'à couvrir l'IP du shim : la réservation doit tenir quand même.
settings.set("ip_end", "192.0.2.254")
database.db_upsert_container_docker(1401, "c1401", n_aio, "bobi-1401")
ips = set()
for v in range(1401, 1406):
    database.db_upsert_container_docker(v, "c%d" % v, n_aio, "bobi-%d" % v)
    ip = allocations.allocate_container_ip(v, node_id=n_aio)
    if ip:
        database.db_update_docker_ip(v, ip)
        ips.add(ip)
toutes = {allocations.allocate_container_ip(v, node_id=n_aio) for v in range(1401, 1406)}
verifie(SHIM_IP not in (ips | toutes),
        "5e. ★ plage élargie jusqu'à .254 : l'IP du shim reste hors d'atteinte", str(sorted(ips)))

verifie(cn.shim_ip_suggeree(n_aio, "pas-un-subnet", START, END) is None,
        "5f. subnet illisible : aucune adresse inventée")


print("\n6. Refus plutôt qu'un shim inerte")
ok, msg = cn.appliquer_shim(database.db_get_node(n_aio), "eno1", SHIM_IP, SUBNET, "", "")
verifie(ok is False and "plage" in msg,
        "6a. plage illisible : refus net, pas une interface posée sans aucune route", str(msg))
ok, msg = cn.appliquer_shim(database.db_get_node(n_aio), "", SHIM_IP, SUBNET, START, END)
verifie(ok is False, "6b. parent manquant : refus", str(msg))


print("\n7. « Réseau interne » : refusé ailleurs que sur un tout-en-un")
# ★ LA RAISON DU REFUS N'EST PAS UNE PRÉCAUTION. L'orchestrateur joint chaque conteneur en
# DIRECT sur docker_ip:8081 (cf. addressing.get_container_ip) : un subnet privé au nœud est
# injoignable depuis un contrôleur situé ailleurs. Les conteneurs seraient « running » sans
# jamais recevoir leur script — la panne même que ce chantier répare, par l'autre bout.
ok_i, raison, avert = cn.mode_interne_possible(database.db_get_node(n_dist), controller_ips=CTRL)
verifie(ok_i is False, "7a. ★ nœud distant : le mode interne est REFUSÉ")
verifie(isinstance(raison, str) and "tout-en-un" in raison and ":8081" in raison,
        "7b. le refus dit POURQUOI (plan de contrôle), pas seulement « impossible »", str(raison))
ok_i, raison, avert = cn.mode_interne_possible(database.db_get_node(n_aio), controller_ips=CTRL)
verifie(ok_i is True and raison is None, "7c. nœud tout-en-un : accepté", str(raison))
# Le refus seul ne distingue rien ici (un nœud sans hôte n'est pas non plus tout-en-un) : c'est
# le MESSAGE qui doit différer, sinon on envoie l'exploitant vérifier sa topologie alors que son
# nœud n'est simplement pas enrôlé.
_r_sans_hote = cn.mode_interne_possible({}, controller_ips=CTRL)
verifie(_r_sans_hote[0] is False and "enrôlement" in (_r_sans_hote[1] or ""),
        "7d. nœud sans hôte connu : refusé, et pour LA SIENNE de raison (enrôlement), pas "
        "« pas un tout-en-un »", str(_r_sans_hote[1]))

# Le refus doit AUSSI tenir dans la route, et ne rien exécuter sur le nœud avant de refuser.
from flask import Flask                                                        # noqa: E402
from app import node_driver                                                    # noqa: E402
from app.routes import node_network as nn                                      # noqa: E402

# La route interroge les IP RÉELLES du contrôleur. Sur la machine de banc, elles n'ont aucune
# raison de contenir l'hôte fictif du nœud : sans cette doublure, la route refuserait pour la
# bonne raison mais pas celle qu'on teste, et les cas 7h/7i mesureraient autre chose.
from app import addressing                                                     # noqa: E402
addressing.controller_ipv4s = lambda: list(CTRL)

executes = []
node_driver.host_exec = lambda node, cmd, timeout=None: (executes.append(cmd), (0, "", ""))[1]
node_driver.ensure_network = lambda *a, **kw: (executes.append(("ensure",) + a), (True, {}))[1]

_app = Flask(__name__)
with _app.test_request_context(json={"mode": "interne"}):
    rep = nn._appliquer_mode_interne(database.db_get_node(n_dist), n_dist, {"mode": "interne"})
corps, code = (rep if isinstance(rep, tuple) else (rep, 200))
verifie(code == 409,
        "7e. ★ la route refuse en 409 (conflit d'état), pas en 500 ni en silence", str(code))
verifie(not executes,
        "7f. ★ et RIEN n'a été exécuté sur le nœud avant le refus — pas de réseau à moitié créé",
        str(executes))
verifie(database.node_net_mode(database.db_get_node(n_dist)) == "site",
        "7g. le mode du nœud n'a pas bougé non plus")

with _app.test_request_context(json={"mode": "interne", "subnet": "pas-un-subnet"}):
    rep = nn._appliquer_mode_interne(database.db_get_node(n_aio), n_aio,
                                     {"mode": "interne", "subnet": "pas-un-subnet"})
corps, code = (rep if isinstance(rep, tuple) else (rep, 200))
verifie(code == 400 and not executes,
        "7h. subnet illisible : refus avant toute action sur le nœud", "%s %s" % (code, executes))

with _app.test_request_context(json={"mode": "interne", "subnet": "10.9.9.0/31"}):
    rep = nn._appliquer_mode_interne(database.db_get_node(n_aio), n_aio,
                                     {"mode": "interne", "subnet": "10.9.9.0/31"})
corps, code = (rep if isinstance(rep, tuple) else (rep, 200))
verifie(code == 400,
        "7i. subnet trop petit pour une passerelle + des conteneurs : refusé de même", str(code))


print("\n8. Poser le shim est une action À PART, qui ne détruit rien")
# ★ Le shim n'était posé qu'en passant par « Reconfigurer le réseau », qui DÉTRUIT les conteneurs
# du nœud dès qu'un paramètre diffère. Faire passer un correctif inoffensif par un geste
# destructeur garantit qu'on ne le fera pas sur une machine en production — donc que le shim
# restera absent (constaté sur la Z8 le 2026-10-27, après déploiement).
_rules = [r for r in nn.bp.deferred_functions] if hasattr(nn.bp, "deferred_functions") else []
verifie(hasattr(nn, "api_node_shim_set"),
        "8a. ★ une route dédiée existe pour poser le shim, sans toucher au réseau")
_src_shim = open(os.path.join(RACINE, "app", "routes", "node_network.py"),
                 encoding="utf-8").read()
_fn_shim = next(n for n in _ast.parse(_src_shim).body
                if isinstance(n, _ast.FunctionDef) and n.name == "api_node_shim_set")
_txt_shim = _ast.get_source_segment(_src_shim, _fn_shim) or ""
verifie("destroy_compute" not in _txt_shim and "detruire_reseau" not in _txt_shim,
        "8b. ★ elle ne détruit NI conteneur NI réseau — c'est tout l'intérêt d'une action à part")
# ★ LE CONTRÔLE QUI A ÉTÉ RETOURNÉ. Il exigeait d'abord l'INVERSE : que la route refuse quand le
# pool n'est pas sur le subnet de la carte. Le motif était faux, et il interdisait la
# configuration que le shim sert — relevé par la session Tests globaux le 2026-10-27, après un
# revirement que je n'avais appliqué qu'à « Reconfigurer ». Le shim porte lui-même une adresse
# DANS le subnet des conteneurs, sur leur L2 : le nœud n'a pas à les porter.
verifie("pool_hors_subnet" not in _txt_shim,
        "8c. ★ la route NE refuse PAS sur un écart de subnet : le shim est l'enfant macvlan qui "
        "PORTE une adresse du subnet des conteneurs — c'est son rôle, pas une anomalie",
        _txt_shim[:200])
# ⚠ Sur des CONSTANTES, pas sur le texte : la phrase « aucune adresse libre » figure aussi dans
# le commentaire qui explique où est le vrai empêchement. Un contrôle textuel restait donc vert
# après retrait du code (constaté par mutation) — un commentaire n'a jamais empêché personne de
# déployer. Troisième fois que ce piège se présente dans ce chantier : viser ce qui S'EXÉCUTE.
_msgs_shim = [n.value for n in _ast.walk(_fn_shim)
              if isinstance(n, _ast.Constant) and isinstance(n.value, str)]
verifie(any("aucune adresse libre" in m for m in _msgs_shim) and 409 in [
            n.value for n in _ast.walk(_fn_shim)
            if isinstance(n, _ast.Constant) and n.value == 409],
        "8c bis. le seul empêchement RÉEL est constaté au moment d'agir : aucune adresse libre "
        "pour le shim, avec la plage concernée", str(len(_msgs_shim)))
verifie("shim_requis" in _txt_shim,
        "8d. et elle refuse sur un nœud qui n'en a pas besoin, au lieu de poser une interface "
        "inutile sur l'hôte")
verifie("container_net_shim_ip" in _txt_shim,
        "8e. l'adresse retenue est persistée (elle doit rester RÉSERVÉE à l'allocation)")
verifie("retirer" in _txt_shim,
        "8f. le retrait passe par la même route (un seul endroit qui sait défaire)")

# Source de `container_net` lue UNE fois, et réutilisée : trois ouvertures du même fichier dans
# le même banc, c'est trois occasions de lire trois états différents si quelqu'un écrit pendant.
_src_cn = open(os.path.join(RACINE, "app", "container_net.py"), encoding="utf-8").read()
_etat_src = _ast.get_source_segment(
    _src_cn, next(n for n in _ast.parse(_src_cn).body
                  if isinstance(n, _ast.FunctionDef) and n.name == "etat_shim")) or ""
verifie('"raison"' in _etat_src and "a_poser" in _etat_src,
        "8g. ★ l'état DIT pourquoi le shim est absent (code, pas phrase) — « requis: true, "
        "present: false » sans raison oblige son lecteur à aller lire le code")
# ⚠ On interroge l'AST, pas le TEXTE : le mot « pool_hors_subnet » figure encore dans le
# commentaire qui explique son retrait, et un contrôle textuel échouerait sur sa propre
# explication. Même piège que [[garde-qui-cherche-un-nom-attrape-sa-definition]], par l'autre
# bout — ici la mention survit à la disparition du code.
_fn_etat = next(n for n in _ast.parse(_src_cn).body
                if isinstance(n, _ast.FunctionDef) and n.name == "etat_shim")
_mentions = [n for n in _ast.walk(_fn_etat)
             if (isinstance(n, _ast.Call)
                 and (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
                     == "pool_hors_subnet")
             or (isinstance(n, _ast.Constant) and n.value == "pool_hors_subnet")]
verifie(not _mentions,
        "8g bis. ★ et il ne REND PLUS un motif qui masquait le bouton dans une configuration "
        "légitime : un état qui fait disparaître l'action est un refus déguisé",
        str(len(_mentions)))

# L'interface doit proposer le bouton dès que le shim est requis et absent — sans condition sur
# un motif. C'est là que le masquage se produisait.
_tpl_shim = open(os.path.join(RACINE, "templates", "settings.html"), encoding="utf-8").read()
verifie("shim_raison_pool" not in _tpl_shim,
        "8h. ★ l'interface ne branche plus sur ce motif (le bouton était MASQUÉ, et le testeur a "
        "demandé où il était)")
_bloc_shim = _tpl_shim.split("settings.net.shim_absent")[1][:600]
verifie("poserShim(" in _bloc_shim,
        "8i. et le bouton est proposé dans le cas « shim requis, absent »", _bloc_shim[:200])


print("\n8 bis. ★ Le shim n'est PAS une carte du nœud, et rien ne doit le prendre pour telle")
# CE QUE ÇA A COÛTÉ, le 2026-10-27 sur la Z8 : la ligne `bobi-shim` a fini par porter le rôle
# « conteneurs ». Le nœud a été ÉCARTÉ des déploiements (« son réseau conteneurs est accroché à
# eno1 alors que le rôle déclare bobi-shim »), la restauration d'un projet a échoué 0 sur 6, et
# l'identification par MAC a échoué en boucle — celle du shim change à chaque pose. Et comme
# « bobi-shim » se trie AVANT « eno1 », il arrivait EN TÊTE de la liste des interfaces.
verifie(cn.interface_a_nous("bobi-shim") is True,
        "8j. une interface posée par l'orchestrateur est reconnue comme telle")
verifie(cn.interface_a_nous("eno1") is False and cn.interface_a_nous("eno1.20") is False,
        "8k. une carte physique, et une sous-interface VLAN d'une carte physique, ne le sont pas "
        "(la seconde est un parent macvlan LÉGITIME)")

# Le shim inscrit avec un rôle « conteneurs » : exactement l'état de la Z8. Rien ne doit l'élire.
database.db_upsert_node_interface(n_aio, ifname=cn.SHIM_IFACE, role="containers",
                                  ip_cidr="192.0.2.254/32", gateway="192.0.2.254")
verifie(node_driver.parent_declare(n_aio) == "eno1",
        "8l. ★ le parent du macvlan reste la carte PHYSIQUE — sinon le réseau des conteneurs "
        "aurait pour parent le shim, c'est-à-dire un enfant de la vraie carte",
        node_driver.parent_declare(n_aio))
verifie(cn.pool_hors_subnet(n_aio, "192.0.2.0/24") == (None, None, None),
        "8m. ★ et le shim n'est jamais comparé au pool : son /32 est DANS le subnet des "
        "conteneurs par construction, donc aucun pool ne peut le contenir — la remarque se "
        "déclenchait sur le SUCCÈS de l'étape précédente",
        str(cn.pool_hors_subnet(n_aio, "192.0.2.0/24")))

# La route doit REFUSER d'inscrire un rôle là-dessus, quelle que soit l'origine du geste.
# ⚠ CIBLÉE PAR NOM EXACT. La version d'avant prenait « la première fonction dont le nom contient
# `interface` et qui appelle `interface_a_nous` » : dès que `api_node_interfaces` (la LECTURE,
# qui s'en sert pour étiqueter nos propres interfaces) a appelé la même fonction, la garde a
# changé de cible — et cherchait un 409 dans une route qui n'en a pas. Une garde qui désigne sa
# cible par un motif de nom suit le premier arrivant : cf.
# [[garde-qui-cherche-un-nom-attrape-sa-definition]].
_fn_role = next((x for x in _ast.parse(_src).body
                 if isinstance(x, _ast.FunctionDef) and x.name == "api_node_interface_set"
                 and any(isinstance(y, _ast.Call)
                         and (getattr(y.func, "attr", None) or getattr(y.func, "id", None))
                             == "interface_a_nous"
                         for y in _ast.walk(x))), None)
verifie(_fn_role is not None,
        "8n. ★ la route des rôles REFUSE une interface qui est à nous — garde-fou de dernier "
        "recours : l'origine du geste (formulaire, décalage de lignes) n'a jamais été établie, "
        "donc on ferme à l'entrée")
if _fn_role:
    _codes = [x.value for x in _ast.walk(_fn_role)
              if isinstance(x, _ast.Constant) and x.value in (400, 409)]
    verifie(409 in _codes, "8o. et en 409 (conflit d'état), avec la raison", str(_codes[:6]))

# L'inventaire de l'agent ne doit pas non plus les proposer.
_src_ag = open(os.path.join(RACINE, "node_agent", "agent.py"), encoding="utf-8").read()
verifie(_src_ag.count('"virbr", "bobi-"') >= 2,
        "8p. ★ l'agent ne liste plus ces interfaces (inventaire ET débits) : ne pas les proposer "
        "vaut mieux que refuser ce qu'on a proposé",
        str(_src_ag.count('"virbr", "bobi-"')))


print("\n9. ★ Ce qui est bloqué, c'est la DESTRUCTION — pas l'écart de subnet")
# HISTOIRE DE CE CONTRÔLE, parce qu'elle est le contrôle.
# Première version (27/10) : refus dès que le pool d'IP n'était pas sur le subnet de la carte.
# Renversé le même jour — le testeur VEUT ses conteneurs en 192.168.1.x sur une machine en 192.0.2.x,
# et c'est propre : les conteneurs ne sont pas sur le plan d'adressage du site, ils partagent son
# L2, et l'orchestrateur les joint par le shim. Mon refus interdisait cette configuration, sans
# échappatoire dans l'interface. J'avais aussi remplacé une MESURE (`_controller_reach` constate
# la joignabilité) par une INFÉRENCE tirée d'un écart de subnet.
# Le vrai défaut était ailleurs et reste : reconfigurer DÉTRUIT les conteneurs attachés, et
# personne ne le disait.
_fn_post = next(n for n in _ast.parse(_src).body
                if isinstance(n, _ast.FunctionDef) and n.name == "api_node_container_network_set")


def _lignes_appels(fn, nom):
    return sorted(n.lineno for n in _ast.walk(fn) if isinstance(n, _ast.Call)
                  and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == nom)


_src_post = _ast.get_source_segment(_src, _fn_post) or ""
_l_constat = _lignes_appels(_fn_post, "divergence_reseau")
verifie(bool(_l_constat),
        "9a. ★ le POST CONSTATE la divergence avant d'agir (`divergence_reseau`), au lieu de "
        "détruire d'autorité", str(_l_constat))
verifie("confirmation_requise" in _src_post and "409" in _src_post,
        "9b. ★ et il REFUSE en 409 quand des conteneurs tombent, en demandant un accord explicite")
for _geste in ("recreer_si_divergent", "detruire_reseau", "ensure_network"):
    _l_geste = _lignes_appels(_fn_post, _geste)
    verifie(not _l_geste or (_l_constat and _l_constat[0] < _l_geste[0]),
            "9c. ★ le constat précède tout appel à « %s » — l'ordre est la protection" % _geste,
            "constat=%s %s=%s" % (_l_constat, _geste, _l_geste))
verifie('d.get("confirme")' in _src_post,
        "9d. l'accord est un champ de la requête, donc traçable et explicite")
# Le refus doit PORTER la liste, calculée — une liste figée à [] laisse l'exploitant confirmer
# une destruction dont il ne sait rien, et la question de l'interface compterait zéro conteneur.
_charges = {k.value: v for n in _ast.walk(_fn_post) if isinstance(n, _ast.Dict)
            for k, v in zip(n.keys, n.values)
            if isinstance(k, _ast.Constant) and isinstance(k.value, str)}
for _c in ("conteneurs", "changements"):
    _v = _charges.get(_c)
    verifie(_v is not None and not isinstance(_v, (_ast.Constant, _ast.List)),
            "9d bis. « %s » du refus est CALCULÉ, pas une valeur figée" % _c,
            type(_v).__name__ if _v is not None else "absent")
verifie('d.get("forcer")' not in _src_post,
        "9e. ★ et l'écart de subnet ne bloque PLUS rien : une configuration légitime (conteneurs "
        "sur un subnet à eux) ne doit pas exiger une échappatoire que l'interface n'offre pas")

# `divergence_reseau` NE DOIT RIEN DÉTRUIRE : c'est toute sa raison d'être.
_fn_div = next(n for n in _ast.parse(_src_cn).body
               if isinstance(n, _ast.FunctionDef) and n.name == "divergence_reseau")
_txt_div = _ast.get_source_segment(_src_cn, _fn_div) or ""
verifie("detruire_reseau" not in _txt_div and "destroy_compute" not in _txt_div
        and "host_exec" not in _txt_div,
        "9f. ★ le constat ne détruit RIEN et n'exécute rien sur l'hôte — sinon « constater avant "
        "d'agir » serait déjà agir", _txt_div[:160])
verifie('"conteneurs"' in _txt_div and '"champs"' in _txt_div,
        "9g. il rend la LISTE des conteneurs qui tombent et CE QUI CHANGE : « êtes-vous sûr ? » "
        "sans dire ce qu'on perd ne fait prendre aucune décision")

# L'interface doit demander ET renvoyer l'accord, sinon le refus est un cul-de-sac.
_tpl = open(os.path.join(RACINE, "templates", "settings.html"), encoding="utf-8").read()
# ⚠ LA FONCTION ENTIÈRE, pas une fenêtre de N caractères. Le découpage à 2600 caractères a lâché
# dès qu'un commentaire a été ajouté dans la fonction : `confirme:true` est passé hors fenêtre et
# la garde a rougi sans qu'aucune propriété n'ait changé. On coupe à la DÉCLARATION SUIVANTE.
import re as _re                                                               # noqa: E402
_js = _re.split(r"\n(?:async )?function ",
                _tpl.split("async function creerReseauContainers")[1])[0]
verifie("confirmation_requise" in _js and "confirme:true" in _js.replace(" ", ""),
        "9h. ★ l'interface demande la confirmation PUIS renvoie l'accord — un refus sans suite "
        "laisserait l'exploitant devant un bouton qui ne marche jamais")
verifie("{count}" in _tpl.split("detruit_conteneurs")[0][-400:] or "detruit_conteneurs" in _js,
        "9i. et la question nomme le nombre de conteneurs perdus")


print("\n10. `divergence_reseau` : appelé pour de vrai, avec un `docker network inspect` simulé")
# Les contrôles de la section 9 lisent le CODE. Ceux-ci APPELLENT la fonction : c'est la seule
# façon de vérifier ce qu'elle rend — quels conteneurs tombent, et ce qui compte comme
# changement. Deux mutations (« le refus ne dit pas quels conteneurs » et « une valeur vide
# compte comme un changement ») ne mordaient pas avant cette section.
_INSPECT = json.dumps([{
    "Driver": "macvlan",
    "Options": {"parent": "eno1"},
    "IPAM": {"Config": [{"Subnet": "192.168.1.0/24", "Gateway": "192.168.1.254"}]},
    "Containers": {"a": {"Name": "bobi-1701"}, "b": {"Name": "bobi-1702"},
                   "c": {"Name": "inconnu-du-parc"}},
}])
node_driver.host_exec = lambda node, cmd, timeout=None: (0, _INSPECT, "")

database.db_upsert_container_docker(1701, "c1701", n_aio, "bobi-1701")
database.db_upsert_container_docker(1702, "c1702", n_aio, "bobi-1702")
database.db_upsert_container_docker(1703, "c1703", n_dist, "bobi-1701")   # même nom, AUTRE nœud

_ident = cn.divergence_reseau(database.db_get_node(n_aio), n_aio, "bobimacvlan", "macvlan",
                              parent="eno1", subnet="192.168.1.0/24", gateway="192.168.1.254")
verifie(_ident["diverge"] is False and _ident["conteneurs"] == [],
        "10a. paramètres identiques : aucune divergence, donc aucune destruction annoncée",
        str(_ident))

_vide = cn.divergence_reseau(database.db_get_node(n_aio), n_aio, "bobimacvlan", "macvlan",
                             parent="", subnet="", gateway="")
verifie(_vide["diverge"] is False,
        "10b. ★ une valeur voulue VIDE n'est PAS une demande de changement — sinon tout "
        "enregistrement partiel du formulaire détruirait le réseau", str(_vide))

_chg = cn.divergence_reseau(database.db_get_node(n_aio), n_aio, "bobimacvlan", "macvlan",
                            parent="eno1", subnet="192.0.2.0/24", gateway="192.0.2.254")
verifie(_chg["diverge"] is True
        and {c["quoi"] for c in _chg["champs"]} == {"subnet", "gateway"},
        "10c. le changement est NOMMÉ champ par champ (subnet, passerelle)", str(_chg["champs"]))
verifie(any(c["avant"] == "192.168.1.0/24" and c["apres"] == "192.0.2.0/24"
            for c in _chg["champs"]),
        "10d. avec l'avant ET l'après — c'est ce que l'exploitant doit lire pour décider",
        str(_chg["champs"]))
verifie(_chg["conteneurs"] == [1701, 1702],
        "10e. ★ la LISTE des conteneurs qui tombent est exacte : ceux de CE nœud attachés à CE "
        "réseau — ni le conteneur inconnu du parc, ni l'homonyme d'un autre nœud",
        str(_chg["conteneurs"]))

_mode = cn.divergence_reseau(database.db_get_node(n_aio), n_aio, "bobimacvlan", "bridge",
                             parent="", subnet="172.30.0.0/24", gateway="172.30.0.1")
verifie(_mode["diverge"] is True and any(c["quoi"] == "driver" for c in _mode["champs"]),
        "10f. le PILOTE compte (bascule de mode) : c'est le cas où tout tombe forcément",
        str(_mode["champs"]))

node_driver.host_exec = lambda node, cmd, timeout=None: (0, "", "")
_absent = cn.divergence_reseau(database.db_get_node(n_aio), n_aio, "bobimacvlan", "macvlan",
                               parent="eno1", subnet="192.0.2.0/24")
verifie(_absent["diverge"] is False and _absent["conteneurs"] == [],
        "10g. réseau ABSENT : rien à détruire, rien à confirmer (première configuration)",
        str(_absent))


print("\n" + ("─" * 70))
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ shim macvlan : les conteneurs sont joignables et le LAN du site est intact")
sys.exit(0)
