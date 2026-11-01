#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Un nœud enrôlé sous une BOUCLE LOCALE : l'URL donnée au navigateur ne doit pas pointer son poste.

Le défaut (relevé sur la Z8 le 2026-10-28) : en « réseau interne », toute URL donnée à un
navigateur passe par l'HÔTE du nœud. Sur un tout-en-un, `nodes.host` vaut `127.0.0.1` — c'est
l'adresse par laquelle l'ORCHESTRATEUR le joint, et elle est juste pour lui. Envoyée à un poste
distant, elle désigne CE poste : panneau noir, aucune erreur, aucune alerte, passerelle WebRTC
parfaitement saine. C'est le même échec silencieux que la substitution d'hôte corrigeait déjà,
d'un cran plus loin.

Ce que cette garde tient, par ordre de gravité :
1. ★ une boucle locale ne sort JAMAIS comme adresse de navigateur quand on a su mesurer mieux ;
2. la mesure est bornée au cas de la boucle locale — sur un nœud DISTANT, elle rendrait l'adresse
   du contrôleur, c'est-à-dire un faux aplomb ;
3. l'override par nœud prime, y compris sur un `host` routable (NAT, nom publié) ;
4. zéro régression : mode site inchangé, `host` normal inchangé ;
5. quand rien n'aboutit, on rend None — l'appelant garde ce qu'il avait, il ne reçoit pas une
   invention (et un avertissement est journalisé).

Les interfaces `bobi-*` sont écartées à DEUX endroits (la mesure et le repli de rôle) : le shim
macvlan porte une adresse en /32 dans le subnet des conteneurs, parfois même un rôle posé par
erreur, et elle n'est joignable de personne. Une garde qui ne le vérifie pas laisserait passer
l'adresse la plus plausible et la plus fausse.

Hors ligne : base jetable, aucun réseau (la mesure `ip route get` est DOUBLÉE — la vraie route de
la machine de CI n'a rien à dire sur la logique testée).
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

# ⚠ AVANT tout accès : une connexion SQLite est tenue PAR THREAD et réutilisée — surcharger
# DB_PATH après un premier accès n'isole plus rien (cf. CLAUDE.md, « Modèle de threading »).
from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_adrnav_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database.init_db()

from app import addressing, settings                                           # noqa: E402

echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + detail) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


# ── Doublure de la MESURE. On remplace `ip route get` par une valeur qu'on choisit, sinon le
# verdict dépendrait du réseau de la machine qui lance la garde. `_mesure` est aussi un COMPTEUR :
# plusieurs contrôles tiennent que la mesure n'a PAS été consultée.
_mesure = {"appels": 0, "rend": "192.0.2.10"}


def _fausse_mesure(cible="1.1.1.1"):
    _mesure["appels"] += 1
    return _mesure["rend"]


_VRAIE_MESURE = addressing.ip_lan_controleur   # la section 8 appelle la VRAIE
addressing.ip_lan_controleur = _fausse_mesure


def _nouveau_noeud(nom, host, mode):
    nid = database.db_add_node(nom, host)
    database.db_update_node(nid, container_net_mode=mode)
    return nid, database.db_get_node(nid)


print("\n1. Reconnaître une adresse qui ne désigne rien pour un navigateur distant")
for h in ("127.0.0.1", "127.0.1.1", "localhost", "LocalHost", "::1", "[::1]", "0.0.0.0", "::"):
    verifie(addressing.adresse_locale_seulement(h) is True,
            "1a. « %s » est inutilisable depuis un autre poste" % h)
for h in ("192.0.2.5", "10.0.0.1", "serveur.studio.local", ""):
    verifie(addressing.adresse_locale_seulement(h) is False,
            "1b. « %s » n'est pas écarté (ni boucle locale, ni adresse nulle)" % h)

print("\n2. ★ Nœud tout-en-un enrôlé en 127.0.0.1, mode interne")
nid_l, n_l = _nouveau_noeud("tout-en-un", "127.0.0.1", database.NET_MODE_INTERNE)
_mesure["appels"] = 0
r = addressing.resolution_adresse_navigateur(n_l)
verifie(r["adresse"] == "192.0.2.10" and r["source"] == "mesure",
        "2a. ★ l'adresse MESURÉE sur la route par défaut remplace la boucle locale", str(r))
verifie(_mesure["appels"] == 1, "2b. la mesure est bien celle qui a répondu (sinon la garde ne "
        "mesure rien)", str(_mesure["appels"]))
verifie(addressing.adresse_locale_seulement(r["adresse"]) is False,
        "2c. ★ et ce qui sort n'est en aucun cas une boucle locale")

database.db_upsert_container_docker(2101, "gw", nid_l, "bobi-2101")
database.db_update_docker_ip(2101, "172.30.0.5")
verifie(addressing.adresse_navigateur("172.30.0.5", vmid=2101) == "192.0.2.10",
        "2d. ★ bout en bout : l'URL du navigateur porte l'adresse de site, pas 127.0.0.1 ni l'IP "
        "privée du conteneur", str(addressing.adresse_navigateur("172.30.0.5", vmid=2101)))

print("\n3. La mesure est BORNÉE au cas de la boucle locale")
nid_d, n_d = _nouveau_noeud("distant", "192.0.2.77", database.NET_MODE_INTERNE)
_mesure["appels"] = 0
r = addressing.resolution_adresse_navigateur(n_d)
verifie(r["adresse"] == "192.0.2.77" and r["source"] == "host",
        "3a. nœud distant : c'est SON hôte, inchangé", str(r))
verifie(_mesure["appels"] == 0,
        "3b. ★ et la mesure n'est même pas consultée — sur un nœud distant elle rendrait "
        "l'adresse du CONTRÔLEUR, une erreur d'autant plus durable qu'elle a l'air juste",
        str(_mesure["appels"]))

print("\n4. Override par nœud : il prime, et il se retire")
settings.set("browser_host", "")              # le global reste vide : l'override est PAR NŒUD
database.db_set_node_setting(nid_d, "browser_host", "studio.example.net")
verifie(addressing.resolution_adresse_navigateur(n_d)["source"] == "override"
        and addressing.resolution_adresse_navigateur(n_d)["adresse"] == "studio.example.net",
        "4a. ★ l'override passe AVANT un `host` routable (NAT, nom publié, second pare-feu)",
        str(addressing.resolution_adresse_navigateur(n_d)))
database.db_set_node_setting(nid_l, "browser_host", "regie.example.net")
_mesure["appels"] = 0
verifie(addressing.resolution_adresse_navigateur(n_l)["adresse"] == "regie.example.net"
        and _mesure["appels"] == 0,
        "4b. sur le tout-en-un aussi : l'override court-circuite la mesure")
database.db_delete_node_setting(nid_l, "browser_host")
database.db_delete_node_setting(nid_d, "browser_host")
verifie(addressing.resolution_adresse_navigateur(n_l)["source"] == "mesure"
        and addressing.resolution_adresse_navigateur(n_d)["source"] == "host",
        "4c. override retiré → la résolution automatique reprend (vide ≠ adresse vide)")

print("\n5. Repli déclaré quand la mesure n'aboutit pas — et le shim n'est PAS une carte du nœud")
_mesure["rend"] = None
# Le shim est posé d'abord, et avec un rôle de management : c'est l'erreur réelle (relevée sur la
# Z8), et c'est exactement l'adresse qu'un repli naïf choisirait.
database.db_upsert_node_interface(nid_l, "bobi-shim", role="management", ip_cidr="192.168.1.6/32")
r = addressing.resolution_adresse_navigateur(n_l)
verifie(r["adresse"] is None and r["source"] is None,
        "5a. ★ le shim macvlan n'est JAMAIS rendu comme adresse de site, même porteur d'un rôle "
        "de management posé par erreur", str(r))
database.db_upsert_node_interface(nid_l, "eno1", role="mgmt_containers", ip_cidr="192.0.2.31/24")
r = addressing.resolution_adresse_navigateur(n_l)
verifie(r["adresse"] == "192.0.2.31" and r["source"] == "role",
        "5b. la carte de management déclarée sert de dernier recours (rôle COMBINÉ compris)",
        str(r))

print("\n6. Rien n'aboutit : aucune invention")
nid_v, n_v = _nouveau_noeud("vide", "127.0.0.1", database.NET_MODE_INTERNE)
r = addressing.resolution_adresse_navigateur(n_v)
verifie(r["adresse"] is None and r["source"] is None,
        "6a. ★ None plutôt qu'une adresse fabriquée (l'appelant garde ce qu'il avait)", str(r))
database.db_upsert_container_docker(2102, "gw2", nid_v, "bobi-2102")
database.db_update_docker_ip(2102, "172.30.0.7")
verifie(addressing.adresse_navigateur("172.30.0.7", vmid=2102) == "172.30.0.7",
        "6b. et l'appelant retombe sur l'IP du conteneur, pas sur une exception en pleine page")

print("\n7. Zéro régression sur le mode SITE")
_mesure["rend"] = "192.0.2.10"
nid_s, n_s = _nouveau_noeud("site", "127.0.0.1", database.NET_MODE_SITE)
database.db_upsert_container_docker(2103, "gw3", nid_s, "bobi-2103")
database.db_update_docker_ip(2103, "192.0.2.120")
_mesure["appels"] = 0
verifie(addressing.adresse_navigateur("192.0.2.120", vmid=2103) == "192.0.2.120",
        "7a. mode site : l'IP du conteneur est déjà une adresse du LAN — rien à substituer, même "
        "sur un nœud enrôlé en boucle locale",
        str(addressing.adresse_navigateur("192.0.2.120", vmid=2103)))
verifie(_mesure["appels"] == 0, "7b. et aucune mesure n'est payée en mode site")

print("\n8. La MESURE elle-même : ses deux refus (sinon personne ne les tient)")
# ★ Les sections 2 à 7 doublent `ip_lan_controleur` — donc rien n'y vérifie ce que la mesure
# REFUSE. Une doublure ne peut pas contredire son auteur : c'est la vraie fonction qu'il faut
# appeler ici, avec un `ip route get` doublé à sa place.
import subprocess as _sp_mod                                                  # noqa: E402


class _Faux:
    def __init__(self, sortie):
        self.stdout = sortie
        self.returncode = 0


def _mesure_reelle(sortie):
    vrai_run = _sp_mod.run
    _sp_mod.run = lambda *a, **k: _Faux(sortie)
    addressing._CACHE_LAN.update({"t": 0.0, "ip": None})     # le cache survivrait d'un cas à l'autre
    try:
        return _VRAIE_MESURE()
    finally:
        _sp_mod.run = vrai_run
        addressing._CACHE_LAN.update({"t": 0.0, "ip": None})


verifie(_mesure_reelle("1.1.1.1 via 192.0.2.254 dev eno1 src 192.0.2.31 uid 0 \n    cache \n")
        == "192.0.2.31",
        "8a. route par défaut normale : l'adresse source est rendue")
verifie(_mesure_reelle("1.1.1.1 via 192.168.1.1 dev bobi-shim src 192.168.1.6 uid 0 \n") is None,
        "8b. ★ une source portée par une interface QUE NOUS AVONS CRÉÉE (`bobi-*`) est refusée : "
        "le shim est un /32 dans le subnet des conteneurs, joignable de personne",
        str(_mesure_reelle("1.1.1.1 via 192.168.1.1 dev bobi-shim src 192.168.1.6 uid 0 \n")))
verifie(_mesure_reelle("local 127.0.0.1 dev lo src 127.0.0.1 uid 0 \n") is None,
        "8c. ★ une source de boucle locale est refusée : elle ne vaut pas mieux que ce qu'on "
        "cherche à remplacer")
verifie(_mesure_reelle("") is None,
        "8d. sortie vide (pas de route par défaut) : None, pas une chaîne bancale")


print("\n" + ("─" * 70))
if echecs:
    print("✗ %d contrôle(s) en échec :" % len(echecs))
    for e in echecs:
        print("   · " + e)
    sys.exit(1)
print("✓ adresse navigateur : la boucle locale ne sort jamais, la mesure est bornée, "
      "rien n'est inventé")
