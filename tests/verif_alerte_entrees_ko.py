#!/usr/bin/env python3
"""Vérifie que les entrées déclarées INEXPLOITABLES par un plugin (`entrees_ko` sur :8080)
deviennent des alertes — une seule par échec, avec son retour à la normale.

Pourquoi (2026-10-24) : le plugin `pyramide` publie depuis longtemps un témoin exemplaire — slot,
flux, motif, tentatives, durée — avec cette justification dans son code : « une absence ne se
voit pas ; une entrée qui dit "je n'y arrive pas depuis 43 h" se voit ». **Mais personne ne le
lisait.** La pyramide 1153 publiait quatre entrées en échec, dont une depuis 8,9 h et 914
tentatives, sans qu'aucune alerte n'existe. Un témoin publié et non consommé ne protège de rien.

Ce que la garde verrouille :
  (1) front MONTANT : une entrée en échec lève exactement UNE alerte ;
  (2) pas de rafale : le même échec réobservé n'en lève pas une deuxième (914 tentatives ≠ 914
      alertes) ;
  (3) réarmement : le même slot recâblé sur une AUTRE source est un échec neuf, donc réalerté ;
  (4) retour à la normale : la disparition du slot lève une alerte `info` ;
  (5) le `kind` appartient au vocabulaire fermé `ALERT_KINDS` — hors liste, il serait stocké NULL ;
  (6) les messages sont des clés i18n présentes en FR **et** EN, avec les mêmes champs ;
  (7) un plugin qui ne publie pas ce témoin ne déclenche rien.

Aucun réseau, aucune base : doublures posées sur le module.

    ./venv/bin/python tests/verif_alerte_entrees_ko.py
"""
import json
import os
import re
import sys

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)

from app import metrics as M                            # noqa: E402
from app.database import ALERT_KINDS                    # noqa: E402

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


alertes = []


def _poser_doublures():
    sauve = (M.db_add_alert, M.db_get_container, M._node_de)
    M.db_add_alert = lambda msg, niveau="info", **k: alertes.append((msg, niveau, k))
    M.db_get_container = lambda vmid: {"hostname": "pyr-test"}
    M._node_de = lambda vmid: 34
    return sauve


def _rendre(sauve):
    (M.db_add_alert, M.db_get_container, M._node_de) = sauve


def ko_entry(shm, motif="flux introuvable", n=1, depuis=5):
    return {"shm": shm, "motif": motif, "tentatives": n, "depuis_s": depuis}


sauve = _poser_doublures()
try:
    M._entrees_ko_etat.clear()
    V = 1153

    # (1) front montant
    del alertes[:]
    M._noter_entrees_ko(V, {"entrees_ko": {"5": ko_entry("dl_b3", n=914, depuis=32152)}})
    ok(len(alertes) == 1, "front montant : une entrée en échec lève UNE alerte", str(alertes))
    if alertes:
        msg, niveau, kw = alertes[0]
        ok(msg == "alert.signal.entree_ko", "le message est une CLÉ i18n, pas une phrase", msg)
        ok(niveau == "warning", "niveau warning", niveau)
        ok(kw.get("kind") in ALERT_KINDS, "le kind est dans ALERT_KINDS", str(kw.get("kind")))
        ok(kw.get("vmid") == V, "l'alerte est rattachée au vmid", str(kw.get("vmid")))
        pr = kw.get("params") or {}
        ok(pr.get("shm") == "dl_b3" and pr.get("n") == 914 and pr.get("depuis_s") == 32152,
           "les params portent flux, tentatives et durée", str(pr))

    # (2) pas de rafale — même échec vu 5 fois de plus
    del alertes[:]
    for i in range(5):
        M._noter_entrees_ko(V, {"entrees_ko": {"5": ko_entry("dl_b3", n=914 + i, depuis=32152 + i)}})
    ok(alertes == [], "pas de rafale : le même échec ne réalerte pas", str(alertes))

    # (3) réarmement si le slot change de source
    del alertes[:]
    M._noter_entrees_ko(V, {"entrees_ko": {"5": ko_entry("autre_flux")}})
    ok(len(alertes) == 1, "slot recâblé sur une AUTRE source : échec neuf, réalerté", str(alertes))

    # (4) retour à la normale
    del alertes[:]
    M._noter_entrees_ko(V, {"entrees_ko": {}})
    ok(len(alertes) == 1 and alertes[0][0] == "alert.signal.entree_ok",
       "retour à la normale : une alerte de rétablissement", str(alertes))
    ok(alertes and alertes[0][1] == "info", "le rétablissement est en info", str(alertes))
    # …et il ne se répète pas non plus
    del alertes[:]
    M._noter_entrees_ko(V, {"entrees_ko": {}})
    ok(alertes == [], "le rétablissement ne se répète pas", str(alertes))

    # (7) plugin sans témoin : rien, et surtout pas de faux rétablissement
    del alertes[:]
    M._noter_entrees_ko(1248, {"fps": 50.0})
    M._noter_entrees_ko(1248, {})
    ok(alertes == [], "un plugin qui ne publie pas `entrees_ko` ne déclenche rien", str(alertes))

    # (8) UNE ABSENCE N'EST PAS UNE BONNE NOUVELLE. Un plugin qui publiait le témoin puis cesse
    # de le publier (métriques partielles, :8080 qui répond court, redéploiement vers une version
    # sans témoin) ne doit PAS être lu comme « tout va bien » : on garde l'état et on se taît.
    # Sans ce cas, traiter un champ absent comme un dict vide passait la garde (mutant M6).
    del alertes[:]
    M._entrees_ko_etat.clear()
    M._noter_entrees_ko(V, {"entrees_ko": {"5": ko_entry("dl_b3")}})
    ok(len(alertes) == 1, "échec initial signalé (préparation du cas 8)", str(alertes))
    del alertes[:]
    M._noter_entrees_ko(V, {"fps": 0.0})            # le témoin a DISPARU des métriques
    ok(alertes == [], "témoin disparu des métriques : AUCUN faux rétablissement", str(alertes))
    ok(M._entrees_ko_etat.get(V, {}).get("5") == "dl_b3",
       "l'état est CONSERVÉ quand le témoin disparaît", str(M._entrees_ko_etat.get(V)))
    del alertes[:]
    M._noter_entrees_ko(V, {"entrees_ko": {}})      # là, le plugin dit vraiment « plus rien »
    ok(len(alertes) == 1 and alertes[0][0] == "alert.signal.entree_ok",
       "le rétablissement n'est annoncé que sur un témoin VIDE, pas absent", str(alertes))

    # plusieurs slots d'un coup → une alerte CHACUN, pas une globale
    del alertes[:]
    M._entrees_ko_etat.clear()
    M._noter_entrees_ko(V, {"entrees_ko": {"0": ko_entry("a"), "1": ko_entry("b"),
                                           "2": ko_entry("c")}})
    ok(len(alertes) == 3, "trois slots en échec → trois alertes distinctes", str(len(alertes)))
finally:
    _rendre(sauve)
    M._entrees_ko_etat.clear()

# ── (6) les clés i18n existent des deux côtés, avec les mêmes champs ────────────────
fr = json.load(open(os.path.join(_R, "i18n", "fr.json"), encoding="utf-8"))
en = json.load(open(os.path.join(_R, "i18n", "en.json"), encoding="utf-8"))
for cle in ("alert.signal.entree_ko", "alert.signal.entree_ok"):
    ok(cle in fr, "clé %s présente en FR" % cle)
    ok(cle in en, "clé %s présente en EN" % cle)
    if cle in fr and cle in en:
        # Les champs interpolés doivent coïncider : un `{n}` présent d'un seul côté donne un
        # KeyError à l'affichage dans une seule langue — le genre de panne qu'on ne voit qu'en
        # anglais, sur l'instance d'un client.
        cf = set(re.findall(r"\{(\w+)", fr[cle]))
        ce = set(re.findall(r"\{(\w+)", en[cle]))
        ok(cf == ce, "mêmes champs interpolés FR/EN pour %s" % cle,
           "FR %s / EN %s" % (sorted(cf), sorted(ce)))

# le pont est bien appelé depuis la boucle de métriques (site d'appel, pas la définition)
src = open(os.path.join(_R, "app", "metrics.py"), encoding="utf-8").read()
corps = src.split("def rafraichir_metrics(")[-1]
ok("_noter_entrees_ko(vmid, m)" in corps,
   "le pont est APPELÉ depuis rafraichir_metrics", "appel absent du corps")

print()
print("ÉCHEC : %d" % len(ko) if ko else "TOUT VERT")
sys.exit(1 if ko else 0)
