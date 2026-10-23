#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Garde OFFLINE : audit de l'adressage multicast des sorties 2110 (app/mcast_audit.py).
#
# Horace, 2026-10-08 : l'audio de TX6 était émis sur le groupe de la vidéo de TX2 (239.141.1.2),
# l'audio de TX2 sur celui de la vidéo de TX1 (239.141.1.1). Le SNP signalait « Video Out Of Range
# Packets » sur TX2. Ces partages dataient de juillet ; aucune alarme en dix semaines.
#   A. Inventaire : chaque flux (vidéo, audios, ANC, chemin B 2022-7) est compté.
#   B. Détection : la configuration d'Horace AVANT le plan lève exactement ses groupes partagés ;
#      celle d'APRÈS n'en lève aucun ; deux moteurs sur la même adresse sont vus aussi.
#   C. Transitions : une alarme par épisode, re-signalée si l'ensemble change, close à la résolution.
#   D. Écart au plan : même discipline, à partir de la simulation du plan.
#   E. Branchement : la surveillance l'appelle, et l'appel ne bloque jamais.
#   F. Messages déclarés en FR et EN, mêmes paramètres.
import inspect
import json
import os
import re
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import mcast_audit as ma  # noqa: E402

n = 0


def check(label, cond, got=None):
    global n
    n += 1
    print(("  [OK] " if cond else "  [KO] ") + label + ("" if cond else f"  → {got!r}"))
    assert cond, label


def slot(video, audio=None, anc=None, video_b=None):
    s = {"multicast_ip": video, "dest_port": 2120}
    if audio:
        s["audios"] = [{"multicast_ip": audio[0], "dest_port": audio[1]}]
    if anc:
        s["anc_multicast_ip"], s["anc_dest_port"] = anc
    if video_b:
        s["multicast_ip_leg1"], s["dest_port_leg1"] = video_b, 2120
    return s


# Les six premières sorties d'Horace telles qu'en base AVANT le plan (relevé du 2026-10-08).
HORACE_AVANT = {"tx_slots": [
    slot("239.141.1.1", ("239.141.1.1", 2130), ("239.141.1.51", 2140)),
    slot("239.141.1.2", ("239.141.1.1", 2132), ("239.141.1.52", 2142)),
    slot("239.141.1.3", ("239.141.1.1", 2134), ("239.141.1.53", 2144)),
    slot("239.141.1.4", ("239.141.1.1", 2136), ("239.141.1.54", 2146)),
    slot("239.141.1.5", ("239.141.1.1", 2138), ("239.141.1.55", 2148)),
    slot("239.141.1.6", ("239.141.1.2", 2140), ("239.141.1.56", 2150)),
]}
HORACE_APRES = {"tx_slots": [
    slot("239.141.1.%d" % i, ("239.141.1.%d" % (100 + i), 2128 + 2 * i), ("239.141.1.%d" % (50 + i), 2138 + 2 * i))
    for i in range(1, 7)]}

# ── A. Inventaire ──────────────────────────────────────────────────────────────────────────────
print("A. inventaire des destinations")
d = ma.tx_destinations(HORACE_AVANT)
check("3 flux par sortie × 6", len(d) == 18, len(d))
check("chemin B 2022-7 inclus",
      len(ma.tx_destinations({"tx_slots": [slot("239.1.1.1", video_b="239.2.1.1")]})) == 2)
check("libellé 1-based, comme les SDP",
      ma.flow_label("moteur", {"slot": 5, "essence": "audio", "sub": 0, "leg": 0}) == "moteur TX6 audio1")

# ── B. Détection ───────────────────────────────────────────────────────────────────────────────
print("B. détection des groupes partagés")
part = ma.shared_groups({360: ("2110-io-dl360", HORACE_AVANT)})
check("Horace avant le plan : exactement .1.1 et .1.2", sorted(part) == ["239.141.1.1", "239.141.1.2"], sorted(part))
check("le groupe de la vidéo TX2 porte l'audio de TX6",
      sorted(l for _v, l in part["239.141.1.2"]) == ["2110-io-dl360 TX2 video", "2110-io-dl360 TX6 audio1"],
      part["239.141.1.2"])
check("CONTRÔLE : Horace après le plan → aucun partage",
      ma.shared_groups({360: ("m", HORACE_APRES)}) == {})
deux = ma.shared_groups({1: ("A", {"tx_slots": [slot("239.9.9.9")]}),
                         2: ("B", {"tx_slots": [slot("239.9.9.9")]})})
check("deux moteurs sur la même adresse → vus", list(deux) == ["239.9.9.9"], deux)

# ── C. Transitions ─────────────────────────────────────────────────────────────────────────────
print("C. une alarme par épisode")
alerts = []


def add_alert(key, level, **kw):
    alerts.append((key, level, kw))


ma._shared_alerted.clear()
eng = {360: ("2110-io-dl360", HORACE_AVANT)}
ma.check_shared(eng, add_alert)
check("premier passage → une alarme warning", [(a[0], a[1]) for a in alerts] ==
      [("alert.net.mcast_groupe_partage", "warning")], alerts)
check("le message cite TX6 audio1", "TX6 audio1" in alerts[0][2]["params"]["detail"], alerts[0][2]["params"])
check("type `net`, vmid posé", alerts[0][2]["kind"] == "net" and alerts[0][2]["vmid"] == 360)
alerts.clear()
ma.check_shared(eng, add_alert)
check("même état → aucune nouvelle alarme", alerts == [], alerts)
pire = {"tx_slots": HORACE_AVANT["tx_slots"] + [slot("239.141.1.7", ("239.141.1.3", 2150))]}
ma.check_shared({360: ("2110-io-dl360", pire)}, add_alert)
check("un NOUVEAU groupe partagé → re-signalé", len(alerts) == 1 and alerts[0][2]["params"]["n"] == 3, alerts)
alerts.clear()
ma.check_shared({360: ("2110-io-dl360", HORACE_APRES)}, add_alert)
check("résolu → une alarme info", [(a[0], a[1]) for a in alerts] ==
      [("alert.net.mcast_groupe_partage_resolu", "info")], alerts)
alerts.clear()
ma.check_shared({360: ("2110-io-dl360", HORACE_APRES)}, add_alert)
check("résolu et stable → silence", alerts == [], alerts)
ma.check_shared(eng, add_alert)
alerts.clear()
ma.check_shared({}, add_alert)
check("moteur détruit → état oublié, sans alarme", ma._shared_alerted == {} and alerts == [])

# ── D. Écart au plan ───────────────────────────────────────────────────────────────────────────
print("D. moteur hors plan")
ma._plan_alerted.clear()
alerts.clear()
diff25 = [{"etat": "a_changer"}] * 25 + [{"etat": "inchange"}] * 50
ma.check_plan(eng, add_alert, lambda v: (diff25, None))
check("25 flux à changer → warning avec n=25",
      [(a[0], a[1], a[2]["params"]["n"]) for a in alerts] == [("alert.net.mcast_hors_plan", "warning", 25)], alerts)
alerts.clear()
ma.check_plan(eng, add_alert, lambda v: (diff25, None))
check("même écart → silence", alerts == [], alerts)
ma.check_plan(eng, add_alert, lambda v: ([], "moteur introuvable"))
check("simulation en erreur → ni alarme ni résolution", alerts == [] and ma._plan_alerted.get(360) == 25)
ma.check_plan(eng, add_alert, lambda v: ([{"etat": "inchange"}] * 75, None))
check("aligné → info", [(a[0], a[1]) for a in alerts] == [("alert.net.mcast_hors_plan_resolu", "info")], alerts)
alerts.clear()


def boom(v):
    raise RuntimeError("base indisponible")


ma.check_plan(eng, add_alert, boom)
check("simulation qui lève → pas d'exception, pas d'alarme", alerts == [])

# ── E. Branchement ─────────────────────────────────────────────────────────────────────────────
print("E. branchement sur la surveillance")
from app import metrics  # noqa: E402

check("la surveillance des moteurs appelle maybe_audit",
      "mcast_audit.maybe_audit()" in inspect.getsource(metrics.rafraichir_metrics))
gate = threading.Event()
appels = []
ma._engines = lambda: (appels.append(1), gate.wait(5), {})[2]
ma._last_shared = ma._last_plan = -1e9
ma.maybe_audit()
ma.maybe_audit()                     # le premier tourne encore : celui-ci ne doit rien lancer
check("l'appel rend la main pendant que l'audit tourne", ma._running.locked())
gate.set()
for _t in threading.enumerate():
    if _t.name == "mcast-audit":
        _t.join(5)
check("jamais deux audits à la fois", len(appels) == 1, appels)
ma.maybe_audit()
check("échéance non passée → pas de nouvel audit", len(appels) == 1, appels)

# ── F. Messages ────────────────────────────────────────────────────────────────────────────────
print("F. messages FR et EN")
cats = {lg: json.load(open(os.path.join(ROOT, "i18n", lg + ".json"))) for lg in ("fr", "en")}
for k in ("alert.net.mcast_groupe_partage", "alert.net.mcast_groupe_partage_resolu",
          "alert.net.mcast_hors_plan", "alert.net.mcast_hors_plan_resolu"):
    ph = [set(re.findall(r"\{(\w+)\}", cats[lg].get(k, ""))) for lg in ("fr", "en")]
    check("%s : présent, mêmes paramètres" % k, all(cats[lg].get(k) for lg in cats) and ph[0] == ph[1], ph)

print("\n%d vérifications OK" % n)
