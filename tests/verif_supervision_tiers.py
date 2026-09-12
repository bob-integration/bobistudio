#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc de `services/nmos/supervision_tiers.py` — les statuts BCP-008 d'un appareil TIERS
# traduits en alertes chez nous.
#
# CE QUI EST ÉPROUVÉ ICI. Les RÈGLES de traduction, qui sont le cœur du raccordement et qui sont
# pures : quel statut mérite une alerte, à quel niveau, et combien de fois. Se tromper là ne
# casse rien visiblement — ça noie l'exploitant sous des alertes qui ne veulent rien dire, ou ça
# reste muet sur une panne. Les deux se découvrent trop tard.
#
# La partie vivante (découverte du pair, session IS-12, abonnement) a été éprouvée le 2026-08-31
# contre notre propre orchestrateur enregistré dans son propre registre : 1 session ouverte,
# monitors trouvés par classId, abonnement accepté, états initiaux lus.
#
#   $ ./venv/bin/python tools/verif_supervision_tiers.py
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule if condition else (intitule, explication))
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from services.nmos import supervision_tiers as S                    # noqa: E402

print("supervision des tiers — règles de traduction\n")

# On capture les alertes au lieu de les écrire.
ALERTES = []
import app.database as _db                                          # noqa: E402
_vrai_add = _db.db_add_alert
_db.db_add_alert = lambda msg, niveau="info", **kw: ALERTES.append((msg, niveau, kw.get("kind")))

try:
    S._dernier.clear()
    URL, LBL = "ws://pair/x", "Éditeur tiers"

    # ── Ce qui NE doit PAS alerter ────────────────────────────────────────────
    S._signaler(URL, LBL, 1, "rx1", "rx", S.INACTIVE, None)
    controle("★ un monitor INACTIF ne produit AUCUNE alerte", not ALERTES,
             "un receiver inactif décrit une ressource qu'on n'a pas demandé d'utiliser — "
             "l'alerter ferait du bruit permanent sur tout appareil ayant des entrées libres")
    S._signaler(URL, LBL, 2, "rx2", "rx", S.HEALTHY, None)
    controle("un monitor SAIN non plus", not ALERTES)

    # ── Ce qui doit alerter, et à quel niveau ─────────────────────────────────
    S._signaler(URL, LBL, 3, "rx3", "rx", S.PARTIALLY_HEALTHY, "perte partielle")
    controle("« partiellement dégradé » → warning",
             len(ALERTES) == 1 and ALERTES[0][1] == "warning", "obtenu %s" % ALERTES)
    controle("le message nomme l'appareil, le sens et le rôle",
             all(x in ALERTES[0][0] for x in (LBL, "receiver", "rx3")),
             "une alerte qu'on ne peut pas rattacher à un équipement est inexploitable — "
             "obtenu %r" % ALERTES[0][0])
    controle("et le motif du pair est joint", "perte partielle" in ALERTES[0][0])
    controle("le `kind` est dans le vocabulaire FERMÉ des alertes",
             ALERTES[0][2] in _db.ALERT_KINDS,
             "un kind hors liste est refusé et stocké NULL — obtenu %r" % ALERTES[0][2])

    ALERTES.clear()
    S._signaler(URL, LBL, 4, "tx1", "tx", S.UNHEALTHY, None)
    controle("« en panne » → error", len(ALERTES) == 1 and ALERTES[0][1] == "error")
    controle("un sender est nommé « sender », pas « receiver »", "sender" in ALERTES[0][0])

    # ── Répétition et retour à la normale ─────────────────────────────────────
    ALERTES.clear()
    for _ in range(4):
        S._signaler(URL, LBL, 4, "tx1", "tx", S.UNHEALTHY, None)
    controle("★ le MÊME statut répété n'alerte qu'une fois (ici zéro : déjà signalé)",
             not ALERTES,
             "à la reconnexion on relit l'état courant ; sans mémoire, chaque hoquet réseau "
             "ré-alerterait et finirait par masquer les vraies alertes")

    S._signaler(URL, LBL, 4, "tx1", "tx", S.HEALTHY, None)
    controle("le retour à la normale ne crée pas d'alerte « tout va bien »", not ALERTES)
    S._signaler(URL, LBL, 4, "tx1", "tx", S.UNHEALTHY, None)
    controle("★ mais une NOUVELLE dégradation alerte de nouveau",
             len(ALERTES) == 1,
             "le retour à la normale doit être MÉMORISÉ, sinon la rechute passerait pour un "
             "doublon et resterait silencieuse")

    # ── Découverte des monitors : par classId, jamais par le nom du rôle ──────
    class _FauxClient:
        def commander(self, oid, mid, args=None):
            return {"status": 200, "value": [
                {"oid": 10, "role": "receivers", "classId": [1, 1]},
                {"oid": 11, "role": "peu_importe", "classId": S.CLASSE_MONITOR_RX},
                {"oid": 12, "role": "receiver_truc", "classId": [1, 2]},
                {"oid": 13, "role": "autre_chose", "classId": S.CLASSE_MONITOR_TX},
            ]}

    mons = S._monitors(_FauxClient())
    controle("★ les monitors sont reconnus par leur classId, pas par leur nom de rôle",
             sorted(mons) == [(11, "peu_importe", "rx"), (13, "autre_chose", "tx")],
             "un éditeur nomme ses blocs comme il veut : filtrer sur « receiver » dans le rôle "
             "marcherait chez nous et nulle part ailleurs — obtenu %s" % mons)

    # ── Découverte des pairs : sur le type de contrôle, depuis le registre ────
    from services.nmos import registre as R
    from services.nmos.client_ncp import TYPE_IS12, TYPE_IS14
    R.vider()
    with R._verrou:
        R._res["device"]["d1"] = {"data": {"id": "d1", "label": "Avec IS-12", "controls": [
            {"type": TYPE_IS14, "href": "http://x/config"},
            {"type": TYPE_IS12, "href": "ws://x/ncp"}]}}
        R._res["device"]["d2"] = {"data": {"id": "d2", "label": "Sans IS-12", "controls": [
            {"type": TYPE_IS14, "href": "http://y/config"}]}}
    p = S.pairs()
    controle("seuls les pairs annonçant IS-12 sont supervisés",
             p == [("Avec IS-12", "ws://x/ncp")],
             "un appareil sans point de contrôle IS-12 ne peut pas notifier — obtenu %s" % p)
    R.vider()

    # ── Le réglage ferme bien la surface ─────────────────────────────────────
    from app.database import db_get_setting, db_set_setting
    avant = db_get_setting("nmos_supervision_tiers", None)
    try:
        db_set_setting("nmos_supervision_tiers", False)
        controle("fermé par défaut, aucune session n'est ouverte", S.demarrer() == 0,
                 "ouvrir des sessions WebSocket permanentes vers des équipements tiers n'est pas "
                 "un défaut raisonnable")
    finally:
        db_set_setting("nmos_supervision_tiers",
                       avant if avant is not None else False)
finally:
    _db.db_add_alert = _vrai_add
    S._dernier.clear()

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
