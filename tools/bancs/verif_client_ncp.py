#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc des CLIENTS MS-05-02 : `services/nmos/client_ncp.py` (IS-14, REST) et
# `services/nmos/client_is12.py` (IS-12, WebSocket).
#
# CONTRE QUI ON TESTE. Contre NOUS-MÊMES : notre serveur IS-14/IS-12 est un pair conforme, et
# c'est le seul moyen honnête de savoir si nos clients marchent avant de les brancher sur le
# matériel de quelqu'un d'autre. Un client qu'on n'a jamais fait parler à personne n'est pas un
# client, c'est une intention.
#
# ⚠ Le banc ACTIVE temporairement IS-14 (et a besoin d'IS-12 en marche), puis restaure les
# réglages. Les parties qui exigent un service injoignable sont SAUTÉES, pas mises en échec.
#
#   $ ./venv/bin/python tools/verif_client_ncp.py
import json
import os
import sys
import threading
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule if condition else (intitule, explication))
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from services.nmos import client_ncp as cl                          # noqa: E402
from services.nmos.client_is12 import Client, ErreurIS12            # noqa: E402

BASE_NODE = "http://127.0.0.1:5000"

# ══════════════════════════════════════════════════════════════════════════════
# 1. Logique pure — aucun pair requis
# ══════════════════════════════════════════════════════════════════════════════
print("clients MS-05-02 — logique\n")

_dev = {"controls": [{"type": "urn:x-nmos:control:sr-ctrl/v1.1", "href": "http://x/is05"},
                     {"type": cl.TYPE_IS14, "href": "http://x/config/"}]}
controle("le point de contrôle est trouvé sur le TYPE normalisé",
         cl.point_de_controle(_dev) == "http://x/config",
         "on ne devine jamais l'URL : un appareil qui n'annonce pas son point de contrôle n'en a "
         "pas, et fabriquer l'adresse produirait des 404 pris pour des pannes")
controle("un Device sans point de contrôle rend None",
         cl.point_de_controle({"controls": []}) is None)
controle("un type DIFFÉRENT n'est pas confondu avec celui qu'on cherche",
         cl.point_de_controle(_dev, cl.TYPE_IS12) is None)

# ★ Le piège classique de MS-05-02 : le transport réussit, la commande échoue.
try:
    cl._verifier({"status": 417, "errorMessage": "hors bornes"}, "essai")
    controle("★ un NcMethodResult en erreur DANS un 200 HTTP est détecté", False,
             "il n'a pas levé")
except cl.ErreurTiers as e:
    controle("★ un NcMethodResult en erreur DANS un 200 HTTP est détecté",
             "417" in str(e),
             "le verdict est dans le CORPS : ne regarder que le code HTTP ferait passer un refus "
             "pour un succès")
try:
    cl._verifier({"status": 200, "value": 1}, "essai")
    controle("et un résultat valide passe", True)
except cl.ErreurTiers:
    controle("et un résultat valide passe", False)

# ══════════════════════════════════════════════════════════════════════════════
# 2. IS-14 contre notre propre serveur
# ══════════════════════════════════════════════════════════════════════════════
print("\nclient IS-14 — contre notre propre serveur\n")
from app.database import db_get_setting, db_set_setting                       # noqa: E402
_avant14 = db_get_setting("nmos_is14_enabled", None)
_joignable = False
try:
    db_set_setting("nmos_is14_enabled", True)
    pts, _pourquoi = [], ""
    try:
        pts = cl.points_de_controle_du_node(BASE_NODE)
        _joignable = bool(pts)
        if not pts:
            _pourquoi = ("aucun Device n'annonce %s — IS-14 n'a pas été démarré dans le "
                         "processus Flask (le réglage ne suffit pas, il faut un redémarrage)"
                         % cl.TYPE_IS14)
    except cl.ErreurTiers as err:
        _pourquoi = str(err)[:90]
    if not _joignable:
        # ⚠ Une section qui ne dit RIEN se lit comme une section réussie. On énonce le saut, et
        # sa raison : c'est la différence entre « non testé » et « testé, ça marche ».
        print("  SAUTÉ  client IS-14 — %s" % _pourquoi)
    if _joignable:
        base = pts[0][1]
        controle("le point de contrôle IS-14 est découvert depuis le Node", base.endswith(
            "/x-nmos/configuration/v1.0"), "obtenu %s" % base)
        ch = cl.chemins(base)
        controle("le modèle du pair s'énumère", len(ch) > 3 and "root" in ch,
                 "obtenu %d chemins" % len(ch))
        d = cl.descripteur(base, "root") or {}
        d = d.get("value", d)
        controle("le descripteur de `root` est un NcBlock", d.get("classId") == [1, 1],
                 "obtenu %s" % d.get("classId"))
        props = cl.proprietes(base, "root")
        controle("ses propriétés s'énumèrent et se lisent",
                 bool(props) and cl.lire(base, "root", props[0]) is not None)
        try:
            cl.lire(base, "root", "9p9")
            controle("une propriété inexistante lève une erreur claire", False, "pas d'erreur")
        except cl.ErreurTiers:
            controle("une propriété inexistante lève une erreur claire", True)
finally:
    db_set_setting("nmos_is14_enabled", _avant14 if _avant14 is not None else False)

# ══════════════════════════════════════════════════════════════════════════════
# 3. IS-12 — commandes ET notifications
# ══════════════════════════════════════════════════════════════════════════════
print("\nclient IS-12 — contre notre propre serveur\n")
URL = "ws://127.0.0.1:5010/x-nmos/ncp/v1.0"
try:
    sonde = Client(URL, timeout=4).connecter()
    sonde.fermer()
    _ws = True
except ErreurIS12 as e:
    _ws = False
    print("  (IS-12 injoignable — partie sautée : %s)" % str(e)[:70])

if _ws:
    # Un endpoint HTTP qui n'est PAS du WebSocket doit être refusé, pas interprété comme des trames.
    try:
        Client("ws://127.0.0.1:5000/x-nmos/", timeout=4).connecter()
        controle("un endpoint non-WebSocket est REFUSÉ", False, "la connexion a été acceptée")
    except ErreurIS12:
        controle("un endpoint non-WebSocket est REFUSÉ", True)

    # ★ ET LE CAS VICIEUX : un pair qui répond bien 101, mais avec une mauvaise clé
    # d'acceptation. Le contrôle précédent ne l'attrape PAS — il tombe sur le 200 d'un endpoint
    # HTTP, donc c'est le test du « 101 » qui refuse, et la vérification de la clé n'est jamais
    # sollicitée (constaté par mutation). Sans elle, on interpréterait du HTTP comme des trames
    # binaires : panne muette et illisible. On monte donc un faux pair pour l'éprouver vraiment.
    import socket as _s

    _srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    _srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    _srv.bind(("127.0.0.1", 0))
    _srv.listen(1)
    _port = _srv.getsockname()[1]

    def _faux_pair():
        try:
            conn, _ = _srv.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: MAUVAISECLE=\r\n\r\n")
            time.sleep(0.5)
            conn.close()
        except Exception:
            pass

    _t = threading.Thread(target=_faux_pair, daemon=True)
    _t.start()
    try:
        Client("ws://127.0.0.1:%d/x-nmos/ncp/v1.0" % _port, timeout=4).connecter()
        controle("★ un 101 avec une MAUVAISE clé d'acceptation est refusé", False,
                 "la connexion a été acceptée — on lirait du HTTP comme des trames")
    except ErreurIS12 as err:
        controle("★ un 101 avec une MAUVAISE clé d'acceptation est refusé",
                 "RFC 6455" in str(err) or "Accept" in str(err), str(err)[:90])
    finally:
        _srv.close()

    with Client(URL) as c:
        r = c.commander(1, (1, 1), {"id": {"level": 1, "index": 1}})
        controle("une commande Get rend un résultat 200",
                 r.get("status") == 200 and r.get("value") == [1, 1],
                 "obtenu %s" % json.dumps(r, ensure_ascii=False)[:120])
        try:
            c.commander(1, (1, 1), {"id": {"level": 9, "index": 9}})
            controle("une commande refusée par le pair LÈVE", False, "pas d'erreur")
        except ErreurIS12 as e:
            controle("une commande refusée par le pair LÈVE",
                     "refusée" in str(e) or "status" in str(e), str(e)[:90])

        membres = (c.commander(1, (2, 1), {"recurse": False}) or {}).get("value") or []
        cible = next((m for m in membres if m.get("role") == "receivers"), membres[0])
        retenus = c.abonner([cible["oid"]])
        controle("l'abonnement est accepté et le pair confirme l'oid",
                 cible["oid"] in retenus,
                 "le pair décide ce qu'il retient — le vérifier évite d'attendre des "
                 "notifications qui ne viendront jamais ; obtenu %s" % retenus)

        recues = []

        def _ecouter():
            for n in c.notifications(duree_s=8):
                recues.append(n)

        t = threading.Thread(target=_ecouter)
        t.start()
        time.sleep(1)
        with Client(URL) as c2:      # une SECONDE session provoque le changement
            c2.commander(cible["oid"], (1, 2),
                         {"id": {"level": 1, "index": 6}, "value": "banc-client-is12"})
        t.join()
        controle("★ la NOTIFICATION du changement arrive à l'abonné",
                 any(n.get("value") == "banc-client-is12"
                     and n.get("oid") == cible["oid"] for n in recues),
                 "c'est tout l'intérêt d'IS-12 sur IS-14 : les statuts BCP-008 d'un appareil "
                 "tiers arrivent par notification, jamais en réponse — obtenu %s"
                 % json.dumps(recues, ensure_ascii=False)[:150])

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
