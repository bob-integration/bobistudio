#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Garde du « MONITORING FLUIDE » (app/monitor.py) — préférence PAR UTILISATEUR, hors ligne.
#
# ★ CE QUE ÇA PROTÈGE : le moniteur WebRTC sort à 25 images/s PAR DÉFAUT (décision d'exploitation :
# un encodeur par utilisateur, souvent logiciel). L'option « fluide » fait suivre la cadence
# déclarée de la source, plafonnée à 60, GOP = 1 s ; une source entrelacée reste à 25.
#
# ★ CE QUI EST VÉRIFIÉ :
#   1. output_rate : défaut 25 ; fluide → cadence de la source (entiers, décimales, fractions
#      « 30000/1001 »), plafond 60, entrelacé → 25, cadence inconnue → 25 ; GOP = cadence ;
#   2. _params : video.fps / video.gop suivent ; débit ×1,5 à 50 (encodeur en débit constant), 8M à 25 ;
#   3. la préférence est persistée par utilisateur (réglage `monitor_fluid_users`), sans fuite d'un
#      utilisateur à l'autre ;
#   4. set_source / set_fluid : basculer l'option REDÉPLOIE l'encodeur à la nouvelle cadence ; une
#      source 25p n'en redémarre rien ; passer d'une source 50p à une 25p de même format en mode
#      fluide n'est PAS un hot-swap (la cadence de sortie change) ; deux sources 50p → hot-swap ;
#   5. macros : action `monitor.set_fluid` pour l'utilisateur qui lance la macro, refusée sans
#      utilisateur (déclencheur, pupitre) ; fournisseur inscrit dans core_plugins.CORE_PROVIDERS.
#
#   $ ./venv/bin/python tests/verif_monitor_fluide.py
import os
import sys
import tempfile
import threading
import types

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import database as _database                                  # noqa: E402
TMP = tempfile.mkdtemp(prefix="verif_monfluide_")
_database.DB_PATH = os.path.join(TMP, "principale.db")                  # jamais la base de prod
_database.init_db()

from app import monitor as mon                                         # noqa: E402
from app import core_plugins                                           # noqa: E402

ECHECS = []


def ok(cond, msg):
    print(("  ok  " if cond else "  ÉCHEC ") + msg)
    if not cond:
        ECHECS.append(msg)


def fmt(fps, scan="p", w=1920, h=1080):
    return {"w": w, "h": h, "chroma": "422", "bit_depth": 8, "scan": scan,
            "field_order": "tff" if scan == "i" else "", "colorimetry": "", "fps": fps}


print("1. output_rate")
ok(mon.output_rate(fmt("50"), False) == (25, 25), "défaut : 50p → 25")
ok(mon.output_rate(None, True) == (25, 25), "fluide, format inconnu → 25")
cas = [("50", 50), (50, 50), ("25", 25), ("24", 24), ("29.97", 30), ("30000/1001", 30),
       ("60000/1001", 60), ("59.94", 60), ("60", 60), ("120", 60), ("100/1", 60), ("50/1", 50),
       ("", 25), ("abc", 25), ("0", 25), ("1/0", 25)]
for v, attendu in cas:
    got = mon.output_rate(fmt(v), True)
    ok(got == (attendu, attendu), "fluide %r → %s (obtenu %s)" % (v, attendu, got))
ok(mon.output_rate(fmt("50", scan="i"), True) == (25, 25), "fluide, 1080i50 → 25")
ok(mon.output_rate(fmt("25", scan="i"), True) == (25, 25), "fluide, entrelacé déclaré 25 → 25")

print("2. _params")
p = mon._params(2, "src", fmt=fmt("50"), hot=True)
ok(p["video"]["fps"] == 25 and p["video"]["gop"] == 25, "défaut : fps/gop 25")
p = mon._params(2, "src", fmt=fmt("50"), hot=True, fluid=True)
ok(p["video"]["fps"] == 50 and p["video"]["gop"] == 50, "fluide 50p : fps/gop 50")
p = mon._params(2, "src", fmt=fmt("30000/1001"), hot=True, fluid=True)
ok(p["video"]["fps"] == 30 and p["video"]["gop"] == 30, "fluide 29.97 : fps/gop 30")
ok(mon._params(2)["video"]["fps"] == 25, "création (sans source) : 25")
# Débit : l'encodeur est en débit CONSTANT — sans hausse, 50 i/s = moitié moins de bits par image.
ok(mon._params(2, "src", fmt=fmt("50"), hot=True)["video"]["bitrate"] == "8M", "défaut : 8M inchangé")
ok(mon._params(2, "src", fmt=fmt("50"), hot=True, fluid=True)["video"]["bitrate"] == "12000k", "fluide 50 : 12 Mb/s")
ok(mon._params(2, "src", fmt=fmt("60"), hot=True, fluid=True)["video"]["bitrate"] == "13600k", "fluide 60 : 13,6 Mb/s")
ok(mon._params(2, "src", fmt=fmt("25"), hot=True, fluid=True)["video"]["bitrate"] == "8M", "fluide 25p : 8M")
ok(mon._params(2, "src", fmt=fmt("24"), hot=True, fluid=True)["video"]["bitrate"] == "8M", "fluide 24p : 8M (jamais moins)")

print("3. persistance par utilisateur")
ok(mon.fluid_enabled(2) is False, "absent → non fluide")
mon._store_fluid(2, True)
ok(mon.fluid_enabled(2) is True and mon.fluid_enabled(3) is False, "uid 2 fluide, uid 3 non")
mon._store_fluid(3, True); mon._store_fluid(2, False)
ok(mon.fluid_enabled(2) is False and mon.fluid_enabled(3) is True, "décoché pour 2, 3 inchangé")
mon._store_fluid(3, False)

print("4. set_source / set_fluid")
SOURCES = {"a50": fmt("50"), "b50": fmt("50"), "c25": fmt("25"), "i50": fmt("50", scan="i")}
deploys, hots = [], []


class FakeThread:
    def __init__(self, target=None, args=(), daemon=None, **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


mon.threading = types.SimpleNamespace(Thread=FakeThread, Lock=threading.Lock)
mon.deployer_script = lambda vmid, t, params: deploys.append(params) or True
mon.requests = types.SimpleNamespace(
    post=lambda url, json=None, timeout=None: hots.append(json["shm"]) or types.SimpleNamespace(status_code=200))
mon._container_for = lambda uid: {"vmid": 9001, "ip": "192.0.2.1", "deploy_config": "{}"}
mon._script_running = lambda ip, vmid=None: True
mon._shm_fmt = lambda shm: dict(SOURCES[shm]) if shm in SOURCES else None
mon._shm_fmt_node = lambda c, shm: dict(SOURCES[shm]) if shm in SOURCES else None


def etape(fn, *a, **kw):
    deploys.clear(); hots.clear()
    return fn(*a, **kw)


UID = 2
mon._source.clear()
etape(mon.set_source, UID, "a50", "A")
ok(len(deploys) == 1 and deploys[0]["video"]["fps"] == 25, "1re source 50p, non fluide → déployé à 25")
r = etape(mon.set_fluid, UID, True)
ok(mon.fluid_enabled(UID), "set_fluid persiste")
ok(r["restarted"] and len(deploys) == 1 and deploys[0]["video"]["fps"] == 50
   and deploys[0]["video"]["gop"] == 50 and deploys[0]["shm_name"] == "a50",
   "cocher → redéploiement immédiat à 50 (gop 50) sur la même source")
r = etape(mon.set_source, UID, "b50", "B")
ok(r.get("hot") and hots == ["b50"] and not deploys, "50p → 50p en fluide : hot-swap")
r = etape(mon.set_source, UID, "c25", "C")
ok(not r.get("hot") and len(deploys) == 1 and deploys[0]["video"]["fps"] == 25,
   "50p → 25p (même format) en fluide : redéploiement à 25, pas de hot-swap")
r = etape(mon.set_fluid, UID, False)
ok(not r["restarted"] and not deploys, "décocher sur une source 25p : rien ne redémarre")
etape(mon.set_source, UID, "a50", "A")
r = etape(mon.set_fluid, UID, False)
ok(not r["restarted"] and not deploys, "décocher alors que déjà non fluide : rien ne redémarre")
mon.set_fluid(UID, True)
deploys.clear()
r = etape(mon.set_fluid, UID, False)
ok(r["restarted"] and deploys and deploys[0]["video"]["fps"] == 25, "décocher sur 50p → redéployé à 25")
mon.set_fluid(UID, True)
r = etape(mon.set_source, UID, "i50", "I")
ok(deploys and deploys[0]["video"]["fps"] == 25, "fluide, source 1080i50 → 25")
# Cadence absente du flowDef du nœud : repli sur la cadence déclarée en base.
mon._shm_fmt_node = lambda c, shm: dict(SOURCES[shm], fps="") if shm in SOURCES else None
r = etape(mon.set_source, UID, "a50", "A")
ok(deploys and deploys[0]["video"]["fps"] == 50, "cadence lue en base quand le flowDef n'en a pas")
st = mon.status(UID)
ok(st.get("fluid") is True, "status() expose la préférence")
mon.set_fluid(UID, False)

print("5. macros")
ok("app.monitor" in core_plugins.CORE_PROVIDERS, "fournisseur inscrit dans CORE_PROVIDERS")
ok(any(a["id"] == "set_fluid" for a in mon.MACRO_PROVIDER["actions"]), "action set_fluid déclarée")
_database.db_create_user("angele", "x", "operator")
uid_a = _database.db_get_user("angele")["id"]
mon.run_action("set_fluid", {"enabled": "true"}, {"user": "angele", "project_id": None})
ok(mon.fluid_enabled(uid_a) and not mon.fluid_enabled(UID), "macro → l'utilisateur qui la lance")
mon.run_action("set_fluid", {"enabled": "false"}, {"user": "angele"})
ok(not mon.fluid_enabled(uid_a), "macro enabled=false → décoché")
for u in ("trigger:t1", "skaarhoj:p:btn1", "", None, "inconnu"):
    try:
        mon.run_action("set_fluid", {"enabled": "true"}, {"user": u})
        ok(False, "sans utilisateur (%r) → refus" % u)
    except RuntimeError:
        ok(True, "sans utilisateur (%r) → refus" % u)

print()
if ECHECS:
    print("%d ÉCHEC(S)" % len(ECHECS))
    sys.exit(1)
print("OK")
