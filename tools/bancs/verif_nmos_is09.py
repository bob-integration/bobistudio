#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc d'IS-09 — les paramètres globaux de l'installation (`services/nmos/is09.py`).
#
# CE QUI COMPTE ICI. IS-09 tient en une ressource, mais elle porte le **domaine PTP**, et un
# désaccord de domaine se manifeste par des symptômes qui ne lui ressemblent pas. Deux choses
# doivent donc être vraies : que ce qu'on PUBLIE soit ce qu'on APPLIQUE, et que ce qu'on LIT chez
# l'autre soit COMPARÉ, jamais appliqué en silence.
#
#   $ ./venv/bin/python tools/verif_nmos_is09.py
import json
import os
import sys
import urllib.error
import urllib.request

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule if condition else (intitule, explication))
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


from services.nmos import is09                                      # noqa: E402
import app.database as _db                                          # noqa: E402

print("IS-09 — paramètres globaux\n")

# ── 1. La ressource publiée ──────────────────────────────────────────────────
_avant = _db.db_get_setting("nmos_is09", None)
try:
    _db.db_set_setting("nmos_is09", True)
    g = is09.globale()
    controle("les champs REQUIS d'IS-09 sont publiés",
             {"id", "version", "label", "is04", "ptp"} <= set(g),
             "obtenu %s" % sorted(g))
    controle("le bloc ptp porte domain_number ET announce_receipt_timeout",
             set(g["ptp"]) == {"domain_number", "announce_receipt_timeout"})
    controle("le domaine est dans la plage 0-127 de la spec",
             0 <= g["ptp"]["domain_number"] <= 127)
    controle("announce_receipt_timeout est dans la plage 2-10",
             2 <= g["ptp"]["announce_receipt_timeout"] <= 10)
    controle("l'intervalle de battement IS-04 est celui qu'on applique vraiment",
             g["is04"]["heartbeat_interval"] == int(
                 __import__("services.nmos", fromlist=["nmos"]).HEARTBEAT_S),
             "publier un intervalle qu'on n'applique pas ferait battre un tiers au mauvais rythme")

    # ★ Ce qu'on publie doit être ce qu'on applique.
    controle("★ le domaine publié est le réglage GLOBAL du site",
             g["ptp"]["domain_number"] == is09.domaine_ptp())

    # ── 2. syslog : publié seulement s'il est configuré ──────────────────────
    _sl_avant = _db.db_get_setting("alerting_syslog_host", None)
    try:
        _db.db_set_setting("alerting_syslog_host", "")
        controle("★ pas de bloc syslog quand aucun collecteur n'est configuré",
                 "syslog" not in is09.globale(),
                 "un bloc vide enverrait les journaux d'un tiers vers nulle part")
        _db.db_set_setting("alerting_syslog_host", "10.0.0.9")
        _g2 = is09.globale()
        controle("et il apparaît dès qu'un collecteur l'est",
                 _g2.get("syslog", {}).get("hostname") == "10.0.0.9"
                 and isinstance(_g2["syslog"]["port"], int))
    finally:
        _db.db_set_setting("alerting_syslog_host",
                           _sl_avant if _sl_avant is not None else "")

    # ── 3. Le contrôle de cohérence — et ce qu'il NE fait PAS ────────────────
    ALERTES = []
    _vrai_add, _vrai_lire = _db.db_add_alert, is09.lire
    _db.db_add_alert = lambda m, n="info", **k: ALERTES.append((m, n, k.get("kind")))
    _avant_domaine = is09.domaine_ptp()
    # ⚠ ON RELÈVE LE DOMAINE POUR LE RENDRE, même si le code éprouvé n'est PAS censé y toucher.
    # Vécu le 2026-08-31 : une mutation qui simulait l'auto-configuration a réellement écrit
    # `ptp_domain = 0` en base. Le contrôle l'a détectée — c'était son travail — mais l'effet de
    # bord a survécu au banc. C'est au banc de savoir ce que le code sous test PEUT toucher, et de
    # le restaurer : sinon il éprouve une garde en cassant ce qu'elle protège.
    try:
        is09.lire = lambda url: {"ptp": {"domain_number": _avant_domaine}}
        r = is09.verifier("http://pair/x-nmos/system/v1.0")
        controle("domaines identiques → concordant, aucune alerte",
                 r["verdict"] == "concordant" and not ALERTES)

        is09.lire = lambda url: {"ptp": {"domain_number": (_avant_domaine + 1) % 128}}
        r = is09.verifier("http://pair/x-nmos/system/v1.0")
        controle("★ domaines DIFFÉRENTS → écart signalé", r["verdict"] == "ÉCART",
                 "obtenu %s" % r)
        controle("et une alerte est levée, avec les DEUX valeurs",
                 len(ALERTES) == 1 and str(_avant_domaine) in ALERTES[0][0],
                 "un désaccord de domaine PTP se manifeste par des symptômes qui ne lui "
                 "ressemblent pas : le nommer est tout l'intérêt — obtenu %s" % ALERTES)
        controle("son `kind` est `ptp`, dans le vocabulaire fermé",
                 ALERTES[0][2] == "ptp" and ALERTES[0][2] in _db.ALERT_KINDS)

        # ★ Le point de doctrine : on ne configure RIEN.
        controle("★ notre domaine n'a PAS été modifié par la lecture",
                 is09.domaine_ptp() == _avant_domaine,
                 "appliquer un domaine PTP, c'est reconfigurer ptp4l sur chaque nœud — une "
                 "opération sur le chemin de production. Se la faire imposer par une annonce "
                 "réseau est exactement ce qu'on ne veut pas subir en direct")

        is09.lire = lambda url: (_ for _ in ()).throw(RuntimeError("refusé"))
        r = is09.verifier("http://pair/x-nmos/system/v1.0")
        controle("une System API injoignable est dite telle, pas confondue avec un écart",
                 r["verdict"] == "System API injoignable")
    finally:
        _db.db_add_alert, is09.lire = _vrai_add, _vrai_lire
        _db.db_set_setting("ptp_domain", _avant_domaine)

    # ── 4. Découverte : la priorité de développement est ÉCARTÉE ─────────────
    controle("★ `pri` vaut 100 par défaut, c'est-à-dire DÉVELOPPEMENT",
             is09._pri() == 100,
             "une System API qui s'annonce en priorité de production détournerait les équipements "
             "d'une installation déjà en place vers NOTRE domaine PTP")
finally:
    _db.db_set_setting("nmos_is09", _avant if _avant is not None else False)

# ── 5. Sur le HTTP réel ──────────────────────────────────────────────────────
def _http(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


code, g = _http("http://127.0.0.1:5000/x-nmos/system/v1.0/global")
if code == 200:
    controle("la System API sert la ressource globale",
             isinstance(g, dict) and "ptp" in g)
elif code == 501:
    controle("IS-09 fermé → 501 (surface externe fermée par défaut)", True)
else:
    print("  SAUTÉ  orchestrateur injoignable (HTTP %s)" % code)

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
