#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du TALLY ENTRANT : d'une activation IS-05 jusqu'à un niveau alimenté.
#
# LE MONTAGE. Notre propre émetteur IS-07 tient lieu d'émetteur tiers : on active un de nos
# Receivers de tally par un PATCH IS-05 pointant dessus, et on vérifie qu'un tally publié là
# ressort sur le NIVEAU qu'un exploitant a affecté. Les deux bouts sont dissymétriques, donc
# c'est un vrai test, et il ne demande ni réseau ni matériel tiers.
#
# CE QU'IL PROTÈGE, et qui casse en silence :
#   · un Receiver IS-07 qui tomberait dans la logique RTP (`vmid`, `recv_idx` inexistants) —
#     KeyError au milieu d'une activation qu'un contrôleur croit réussie ;
#   · une activation SANS niveau affecté : le tally reçu n'irait nulle part, et un 200 le
#     laisserait croire branché ;
#   · le niveau choisi par le CONTRÔLEUR au lieu de l'exploitant — ce serait lui donner la main
#     sur une chaîne de destination qui n'est pas la sienne ;
#   · une désactivation qui LAISSE la lampe allumée.
#
# ⚠ CE BANC OUVRE `nmos_is07`, `nmos_is07_entrant` ET le serveur WS, puis referme dans `finally`.
#
#   $ ./venv/bin/python tools/verif_is07_entrant.py
import importlib
import os
import sys
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


def jusqua(predicat, delai=8.0, pas=0.05):
    fin = time.monotonic() + delai
    while time.monotonic() < fin:
        if predicat():
            return True
        time.sleep(pas)
    return predicat()


from app.database import (db_set_setting, db_get_setting,                     # noqa: E402
                          db_get_tally_levels)

print("IS-07 entrant — d'une activation IS-05 jusqu'à un niveau alimenté\n")

AVANT = {k: db_get_setting(k) for k in ("nmos_is07", "nmos_is07_entrant", "nmos_is07_ws")}
cid = rid = None
try:
    for k in AVANT:
        db_set_setting(k, "1")
    from services import tsl                                          # noqa: E402
    from services import nmos                                         # noqa: E402
    from services.nmos import is07, is07_entrant                      # noqa: E402
    from app.database import (db_upsert_is07_connection,
                              db_set_is07_mapping_for_source)
    importlib.reload(is07)

    nmos.rebuild_model()
    is07.demarrer()
    controle("★★ le serveur IS-07 écoute", jusqua(lambda: is07.etat_ws().get("actif")))

    srcs = is07._sources()
    if not srcs:
        raise SystemExit("aucune Source IS-07 : rien à écouter")
    shm_src, idx_src, niveau, _ = srcs[0]
    sid = is07._sid(shm_src, niveau)

    # ── Une connexion, comme en TSL : un nom, un niveau ──────────────────
    cid = db_upsert_is07_connection({"name": "banc VSM", "enabled": 1, "level_uuid": niveau})
    rid = is07._rid_conn(cid)
    nmos.rebuild_model()
    publies = [r for r, st in nmos._recv_state.items() if st.get("is07")]
    controle("★★★ UN Receiver par connexion, pas un par sortie", publies == [rid],
             "on en publiait 99 pour 6 utiles : la question n'est pas « quelles sorties peuvent "
             "recevoir un tally » mais « quel protocole écrit dans quel niveau ». Obtenu %d"
             % len(publies))

    uri = "ws://127.0.0.1:%d/" % is07._port()
    patch = {"master_enable": True,
             "transport_params": [{"connection_uri": uri}],
             "activation": {"mode": "activate_immediate"}}

    # ── Correspondance VIDE : on refuse, et on dit pourquoi ──────────────
    diag = is07_entrant.activer(rid, True, uri, None)
    controle("★★★ sans correspondance, l'activation REFUSE et le dit",
             diag == "correspondance vide" and rid not in
             [r for r, e in is07_entrant.etat().items() if e.get("connecte")],
             "un contrôleur verrait un 200 et une lampe éteinte, sans rien pour relier les deux. "
             "Obtenu %r" % diag)

    # ── Avec la correspondance : Source de l'émetteur → notre signal ─────
    db_set_is07_mapping_for_source(cid, shm_src, sid)
    code, _ = nmos._apply_receiver_staged(rid, dict(patch))
    controle("★★★ le PATCH IS-05 est accepté", code == 200, "obtenu %s" % code)
    controle("★★★ l'écoute est ouverte et connectée",
             jusqua(lambda: (is07_entrant.etat().get(rid) or {}).get("connecte")),
             "état : %r" % is07_entrant.etat().get(rid))

    cle = (idx_src, niveau)
    src_ecrit = "is07:%s" % rid

    tsl.poser_tally("banc:emetteur", {})
    tsl.poser_tally(src_ecrit, {})
    tsl.poser_tally("banc:emetteur", {cle: "red"})
    is07._pousser([sid])
    controle("★★★ un tally publié revient par la connexion et alimente SON niveau",
             jusqua(lambda: (tsl.sources_du_tally().get("%d_%s" % cle) or {}).get(src_ecrit)
                    == "red"),
             "c'est la boucle complète : Source IS-07 → trame WS → correspondance → index → "
             "niveau. Sources : %r" % tsl.sources_du_tally().get("%d_%s" % cle))

    controle("★★★ elle écrit sous SA propre source, elle n'écrase personne",
             set((tsl.sources_du_tally().get("%d_%s" % cle) or {})) == {"banc:emetteur", src_ecrit},
             "deux protocoles doivent pouvoir alimenter le même niveau : ils se cumulent. "
             "Obtenu %r" % tsl.sources_du_tally().get("%d_%s" % cle))

    # ── Désactiver ÉTEINT ────────────────────────────────────────────────
    code, _ = nmos._apply_receiver_staged(
        rid, {"master_enable": False, "activation": {"mode": "activate_immediate"}})
    controle("★★★ désactiver éteint la contribution de la connexion",
             code == 200 and jusqua(
                 lambda: src_ecrit not in (tsl.sources_du_tally().get("%d_%s" % cle) or {})),
             "une lampe qui reste allumée après qu'on a coupé l'écoute est le pire des deux "
             "états. Sources : %r" % tsl.sources_du_tally().get("%d_%s" % cle))
    controle("★★ ...sans toucher à l'autre écrivain",
             (tsl.sources_du_tally().get("%d_%s" % cle) or {}).get("banc:emetteur") == "red")

    # ── Une connexion désactivée n'est plus publiée ──────────────────────
    db_upsert_is07_connection({"id": cid, "name": "banc VSM", "enabled": 0,
                               "level_uuid": niveau})
    nmos.rebuild_model()
    controle("★★★ une connexion désactivée disparaît d'IS-04",
             not [r for r, st in nmos._recv_state.items() if st.get("is07")],
             "laisser la ressource publiée promettrait un abonnement que personne ne sert")

    # ── Le niveau vient de la CONNEXION, pas du contrôleur ───────────────
    db_upsert_is07_connection({"id": cid, "name": "banc VSM", "enabled": 1,
                               "level_uuid": niveau})
    nmos.rebuild_model()
    autre = next((n["uuid"] for n in db_get_tally_levels() if n["uuid"] != niveau), None)
    if autre:
        nmos._apply_receiver_staged(rid, dict(
            patch, transport_params=[{"connection_uri": uri, "ext_tally_level": autre}]))
        controle("★★★ un contrôleur ne peut pas choisir le niveau par le PATCH",
                 (is07_entrant.etat().get(rid) or {}).get("niveau") == niveau,
                 "IS-05 décrit une connexion, pas une intention d'exploitation : le laisser "
                 "choisir le niveau lui donnerait la main sur une autre production. "
                 "Obtenu %r" % (is07_entrant.etat().get(rid) or {}).get("niveau"))
finally:
    try:
        from services.nmos import is07_entrant as _e
        _e.arreter_tout()
    except Exception:
        pass
    try:
        from services.nmos import is07 as _i
        _i.arreter()
    except Exception:
        pass
    try:
        from services import tsl as _t
        _t.poser_tally("banc:emetteur", {})
        if rid:
            _t.poser_tally("is07:%s" % rid, {})
        residu = _t.get_tally_state()
    except Exception:
        residu = "?"
    try:
        if cid:
            from app.database import db_delete_is07_connection as _d
            _d(cid)
    except Exception:
        pass
    for k, v in AVANT.items():
        db_set_setting(k, "0" if v in (None, "", 0, "0", False) else v)
    print("\n  réglages restaurés : %s · tally résiduel : %r"
          % ({k: db_get_setting(k) for k in AVANT}, residu))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
