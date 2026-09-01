#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du registre IS-04 embarqué (`services/nmos/registre.py`).
#
# Deux parties, et la seconde ne remplace pas la première :
#   · logique PURE (cascade, expiration, rattachement) — en processus, instantané, toujours jouée ;
#   · protocole HTTP RÉEL (codes 201/200/204/404, en-tête Location, battement) contre le service
#     qui tourne — seule façon de vérifier ce qu'un contrôleur tiers verra vraiment.
# La partie HTTP est SAUTÉE si le service ne répond pas, plutôt que de faire échouer le banc pour
# une raison qui n'est pas un défaut du code.
#
# ⚠ Le banc ACTIVE puis REMET le réglage `nmos_registre` à son état d'origine.
#
#   $ ./venv/bin/python tools/verif_nmos_registre.py
import json
import os
import sys
import time
import urllib.error
import urllib.request

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

BASE = "http://127.0.0.1:5000"
REG = BASE + "/x-nmos/registration/v1.3"
QRY = BASE + "/x-nmos/query/v1.3"

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(
        intitule if condition else (intitule, explication))
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from services.nmos import registre as R                            # noqa: E402

# ── Jeu d'essai : un Node tiers plausible ────────────────────────────────────
NID = "11111111-0000-4000-8000-000000000001"
DID = "11111111-0000-4000-8000-000000000002"
SID = "11111111-0000-4000-8000-000000000003"
NODE = {"id": NID, "label": "Éditeur tiers", "href": "http://10.0.0.9/"}
DEV = {"id": DID, "label": "Device tiers", "node_id": NID}
SND = {"id": SID, "label": "Sender tiers", "device_id": DID,
       "transport": "urn:x-nmos:transport:mxl"}

# ══════════════════════════════════════════════════════════════════════════════
# 1. Logique pure
# ══════════════════════════════════════════════════════════════════════════════
print("registre IS-04 — logique\n")
R.vider()
with R._verrou:
    R._res["node"][NID] = {"data": NODE}
    R._sante[NID] = time.monotonic()
    R._res["device"][DID] = {"data": DEV}
    R._res["sender"][SID] = {"data": SND}

controle("le rattachement d'un Sender remonte au Node via son Device",
         R._node_proprietaire("sender", SND) == NID,
         "sans ça, la ressource n'expirerait JAMAIS avec son Node")
controle("une ressource dont le Device est inconnu n'a pas de Node",
         R._node_proprietaire("sender", {"id": "x", "device_id": "inconnu"}) is None,
         "on remonte la chaîne DANS le registre, on ne fait pas confiance à un node_id déclaré")

# Expiration : « both the Node and all registered sub-resources SHOULD be removed »
with R._verrou:
    R._sante[NID] = time.monotonic() - (R._gc_s() + 1)
    morts = R._expirer()
controle("un Node sans battement expire", morts == [NID])
controle("★ son Device ET son Sender disparaissent avec lui",
         not R._res["device"] and not R._res["sender"],
         "un enfant survivant serait un fantôme : plus aucun battement ne le démentirait, et "
         "il resterait dans l'inventaire pour toujours")

# Suppression explicite : même cascade, immédiate
R.vider()
with R._verrou:
    R._res["node"][NID] = {"data": NODE}
    R._res["device"][DID] = {"data": DEV}
    R._res["sender"][SID] = {"data": SND}
    R._supprimer("node", NID)
controle("DELETE sur un parent retire la descendance immédiatement",
         not any(R._res[t] for t in R.TYPES))

controle("le délai de ramasse-miettes ne peut pas descendre sous deux battements",
         R._gc_s() >= 4,
         "un GC plus court que l'intervalle de battement ferait expirer des Nodes vivants")
R.vider()

# ══════════════════════════════════════════════════════════════════════════════
# 1bis. Annonce DNS-SD — sans elle, personne ne TROUVE le registre
# ══════════════════════════════════════════════════════════════════════════════
# Un registre qu'il faut configurer à la main dans chaque équipement ne tient pas la promesse du
# chantier : « un tiers apparaît sans qu'on l'ait déclaré ». C'est le bootstrapping DNS-SD qui la
# tient. On éprouve la COMPOSITION de l'annonce ici (pure) ; sa publication réelle sur le LAN a
# été vérifiée à la main le 2026-08-31 — les trois services sont bien vus par un navigateur mDNS.
from services import nmos as N                                     # noqa: E402
import socket as _socket                                           # noqa: E402

_addr = _socket.inet_aton("127.0.0.1")
_avant_reg = None
try:
    from app.database import db_get_setting as _g, db_set_setting as _s
    _avant_reg = _g("nmos_registre", "0")

    _s("nmos_registre", "0")
    _types = [t for t, _i in N._mdns_services_a_publier(_addr)]
    controle("registre fermé : on n'annonce QUE _nmos-node",
             _types == ["nmos-node"],
             "annoncer un registre fermé enverrait des Nodes vers une API qui rend 501")

    _s("nmos_registre", "1")
    _svc = N._mdns_services_a_publier(_addr)
    _types = [t for t, _i in _svc]
    controle("registre ouvert : _nmos-register ET _nmos-query sont annoncés",
             _types == ["nmos-node", "nmos-register", "nmos-query"],
             "obtenu %s — la Query API sans annonce reste introuvable" % _types)
    _txt = {t: {k: (v or b"").decode() if isinstance(v, bytes) else v
                for k, v in i.properties.items()} for t, i in _svc}
    _txt = {t: {(k.decode() if isinstance(k, bytes) else k): v for k, v in d.items()}
            for t, d in _txt.items()}
    controle("les quatre TXT exigés par IS-04 sont sur register et query",
             all({"api_proto", "api_ver", "api_auth", "pri"} <= set(_txt[t])
                 for t in ("nmos-register", "nmos-query")),
             "obtenu %s" % _txt)
    controle("`pri` n'est PAS mis sur _nmos-node", "pri" not in _txt["nmos-node"],
             "IS-04 ne le prévoit que pour Registration et Query")
    controle("★ `pri` vaut 100 par défaut, c'est-à-dire DÉVELOPPEMENT",
             _txt["nmos-register"]["pri"] == "100",
             "IS-04 : « Values 100+ are reserved for development work to avoid colliding with a "
             "live system ». S'annoncer en priorité de production détournerait vers nous les "
             "Nodes d'un registre déjà en place sur le même LAN — à l'exploitant de l'abaisser")
finally:
    if _avant_reg is not None:
        _s("nmos_registre", _avant_reg)

# ══════════════════════════════════════════════════════════════════════════════
# 2. Protocole HTTP réel
# ══════════════════════════════════════════════════════════════════════════════


def _http(methode, url, corps=None):
    d = json.dumps(corps).encode() if corps is not None else None
    rq = urllib.request.Request(url, data=d, method=methode,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(rq, timeout=8) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)
    except Exception as e:
        return None, str(e), {}


print("\nregistre IS-04 — protocole HTTP\n")
code, _b, _h = _http("GET", BASE + "/x-nmos/node/v1.3/self")
if code != 200:
    print("  (service injoignable — partie HTTP sautée, ce n'est pas un échec du code)")
else:
    from app.database import db_get_setting, db_set_setting
    # ⚠ TOUT réglage touché est relevé AVANT le try et restauré dans le finally. Une restauration
    # posée au fil du try saute dès qu'un contrôle lève — et laisserait ici un ramasse-miettes à
    # 4 secondes dans la configuration du site, ce qui ferait expirer des Nodes parfaitement
    # vivants. Un banc qui abîme la configuration qu'il éprouve est pire qu'un banc absent.
    avant = db_get_setting("nmos_registre", "0")
    gc_avant = db_get_setting("nmos_registre_gc_s", "")
    try:
        # Le réglage est lu à CHAQUE requête (pas au boot) → pas besoin de redémarrer.
        db_set_setting("nmos_registre", "1")
        code, corps, _h = _http("POST", REG + "/resource", {"type": "node", "data": NODE})
        controle("enregistrement d'un Node → 201", code == 201, "obtenu %s : %s" % (code, corps[:120]))
        controle("l'en-tête Location pointe la ressource",
                 "/resource/nodes/" + NID in (_h.get("Location") or ""),
                 "obtenu %r" % _h.get("Location"))

        code, _c, _h = _http("POST", REG + "/resource", {"type": "node", "data": NODE})
        controle("ré-enregistrement du MÊME Node → 200 (pas 201)", code == 200,
                 "201 ferait croire à un nouveau Node à chaque rafraîchissement")

        code, corps, _h = _http("POST", REG + "/resource", {"type": "sender", "data": SND})
        controle("un Sender dont le Device n'est pas enregistré est REFUSÉ (400)", code == 400,
                 "l'accepter créerait un orphelin que le ramasse-miettes ne reprendrait jamais")

        code, _c, _h = _http("POST", REG + "/resource", {"type": "device", "data": DEV})
        controle("enregistrement du Device → 201", code == 201)
        code, _c, _h = _http("POST", REG + "/resource", {"type": "sender", "data": SND})
        controle("le Sender passe une fois son Device connu → 201", code == 201)

        code, corps, _h = _http("POST", REG + "/health/nodes/" + NID)
        controle("battement sur un Node connu → 200 avec health", code == 200 and "health" in corps,
                 "obtenu %s : %s" % (code, corps[:100]))
        code, _c, _h = _http("POST", REG + "/health/nodes/00000000-0000-4000-8000-00000000ffff")
        controle("battement sur un Node INCONNU → 404", code == 404,
                 "c'est ce 404 qui dit au Node de se ré-enregistrer ; un 200 de complaisance "
                 "laisserait un Node absent se croire présent")

        code, corps, _h = _http("GET", QRY + "/senders")
        controle("le Sender tiers apparaît dans la Query API",
                 code == 200 and SID in corps)
        code, corps, _h = _http("GET", QRY + "/senders?id=" + SID)
        controle("le filtre ?id= de la Query API fonctionne",
                 code == 200 and len(json.loads(corps)) == 1)
        code, corps, _h = _http("GET", QRY + "/subscriptions")
        controle("/subscriptions rend une liste vide, pas une erreur", code == 200,
                 "un 501 ferait échouer la découverte entière d'un contrôleur pour une "
                 "capacité optionnelle")

        code, _c, _h = _http("DELETE", REG + "/resource/nodes/" + NID)
        controle("DELETE du Node → 204", code == 204)
        code, corps, _h = _http("GET", QRY + "/senders")
        controle("★ son Sender a disparu de la Query API avec lui",
                 code == 200 and SID not in corps)

        # ── Expiration DE BOUT EN BOUT, par le thread ─────────────────────────────────────
        # ⚠ Les contrôles pures plus haut appellent `_expirer()` DIRECTEMENT : ils passeraient
        # même si le ramasse-miettes ne démarrait jamais. Or `_assurer_reaper()` est appelé au
        # premier enregistrement, et un thread qui ne part pas est exactement le genre de panne
        # muette qu'on ne verrait qu'en exploitation, des semaines plus tard, sur un registre qui
        # accumule des Nodes morts. On l'éprouve donc pour de vrai, avec un GC raccourci.
        db_set_setting("nmos_registre_gc_s", "4")
        _http("POST", REG + "/resource", {"type": "node", "data": NODE})
        _http("POST", REG + "/resource", {"type": "device", "data": DEV})
        _http("POST", REG + "/resource", {"type": "sender", "data": SND})
        code, corps, _h = _http("GET", QRY + "/nodes")
        present = code == 200 and NID in corps
        time.sleep(9)                       # > GC(4 s) + période du reaper (2 s), avec marge
        code, corps, _h = _http("GET", QRY + "/nodes")
        code2, corps2, _h = _http("GET", QRY + "/senders")
        controle("★ le ramasse-miettes RETIRE vraiment un Node qui ne bat plus",
                 present and code == 200 and NID not in corps,
                 "le thread de ramasse-miettes ne tourne pas : le registre accumulerait les "
                 "Nodes morts sans que rien ne le signale")
        controle("★ et sa descendance part avec lui",
                 code2 == 200 and SID not in corps2)
        db_set_setting("nmos_registre", "0")
        code, _c, _h = _http("POST", REG + "/resource", {"type": "node", "data": NODE})
        controle("registre désactivé → 501 à l'écriture", code == 501)
        # ⚠ La LECTURE doit se fermer aussi. Un premier jet ne gardait que l'écriture : la Query
        # API rendait 200 avec des listes vides alors que l'exploitant croyait la surface fermée.
        # « Ouvert mais vide » est indiscernable d'« ouvert et cassé ».
        c1, _b1, _h = _http("GET", QRY + "/nodes")
        c2, _b2, _h = _http("GET", QRY + "/subscriptions")
        c3, _b3, _h = _http("GET", REG + "/resource/nodes/" + NID)
        controle("★ registre désactivé → 501 à la LECTURE aussi (Query API comprise)",
                 c1 == 501 and c2 == 501 and c3 == 501,
                 "obtenu nodes=%s subscriptions=%s resource=%s" % (c1, c2, c3))
    finally:
        db_set_setting("nmos_registre", avant)
        db_set_setting("nmos_registre_gc_s", gc_avant)
        R.vider()

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
