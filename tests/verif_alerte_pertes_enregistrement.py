#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : un enregistreur qui perd UNE image le dit tout de suite (demandé le 2026-11-08).

Banc Z8 du 2026-11-08 : un recorder sur un cœur a jeté 89 % de ses trames pendant dix minutes en
affichant 50 fps, `recording` vrai et aucune erreur ; trois fichiers sont sortis illisibles avec
`record_error` vide. Les compteurs étaient publiés sur :8080, personne ne les lisait.

  1. orchestrateur (`metrics._noter_pertes_enregistrement`, relevés :8080 simulés) :
     - zéro perte → aucune alerte ; la PREMIÈRE image jetée ou répétée → alerte d'erreur ;
     - pertes qui continuent → un rappel par minute au plus, rien si elles ont cessé ;
     - arrêt avec pertes → bilan ; arrêt sans perte → rien ;
     - nouvel enregistrement → réarmé (première perte ré-alertée) ;
     - orchestrateur relancé en plein enregistrement déjà en perte → alerte ; enregistrement
       terminé vu après coup → rien ;
     - `record_error` qui apparaît → alerte, une seule fois ; conteneur qui n'est pas un recorder
       → ignoré ;
  2. cadence : un recorder ARRÊTÉ (0 fps par construction) ne lève plus « cadence non tenue » ;
  3. recorder (script rendu) : un fichier que l'encodeur n'a pas clos proprement remplit
     `record_error` au lieu de le laisser vide ; un fichier bien clos ne touche à rien ; le début
     de session est publié (`rec_started`).

Base jetable (jamais la base de prod).

    ./venv/bin/python tests/verif_alerte_pertes_enregistrement.py
"""
import os
import sys
import tempfile
import threading
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


import app.database as database                                          # noqa: E402
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="rec_loss_db_"), "principale.db")   # jamais la base de prod
database.init_db()
from app import metrics                                                   # noqa: E402

ALERTS = []
metrics.db_add_alert = lambda key, level="info", **kw: ALERTS.append((key, level, kw.get("params") or {}))
metrics.db_get_container = lambda vmid: {"hostname": "rec-%s" % vmid, "node_id": 1}
metrics._node_de = lambda vmid: 1
CLOCK = [1_000_000.0]
metrics.time = types.SimpleNamespace(time=lambda: CLOCK[0], monotonic=lambda: CLOCK[0])


def poll(vmid, rec, drop=0, rep=0, err="", started=1.0, dt=5.0):
    CLOCK[0] += dt
    before = len(ALERTS)
    metrics._noter_pertes_enregistrement(vmid, {"recording": rec, "dropped_frames": drop,
                                                "video_repeated": rep, "record_error": err,
                                                "rec_started": started, "fps": 50.0})
    return [a[0] for a in ALERTS[before:]]


print("1. Orchestrateur")
V = 501
check(poll(V, True) == [] and poll(V, True) == [], "enregistrement sain : aucune alerte")
got = poll(V, True, drop=1)
check(got == ["alert.fps.rec_pertes"] and ALERTS[-1][1] == "error", "UNE image jetée : alerte d'erreur aussitôt (%s)" % got)
check(ALERTS[-1][2].get("drop") == 1 and ALERTS[-1][2].get("h") == "rec-501", "l'alerte nomme l'enregistreur et compte")
T_ALERT = CLOCK[0]
check(poll(V, True, drop=40) == [], "pertes qui continuent : pas une ligne par relevé")
reminders = []
for i in range(14):                                   # still losing, +9 per poll, 70 s
    if poll(V, True, drop=49 + 9 * i):
        reminders.append(CLOCK[0] - T_ALERT)
check(len(reminders) == 1 and reminders[0] >= 60, "toujours en perte : UN rappel, après une minute (%s s)" % reminders)
check(ALERTS[-1][2].get("plus", 0) > 0, "le rappel dit combien de plus")
got = []
for _ in range(15):
    got += poll(V, True, drop=175)
check(got == [], "pertes arrêtées : plus de rappel")
check(poll(V, False, drop=175) == ["alert.fps.rec_fin_pertes"], "arrêt avec pertes : bilan")
check(poll(V, False, drop=175) == [], "bilan une seule fois")
check(poll(V, True, started=2.0) == [], "nouvel enregistrement, compteurs remis à zéro : rien")
check(poll(V, True, rep=1, started=2.0) == ["alert.fps.rec_pertes"], "nouvel enregistrement : première perte ré-alertée (image répétée)")
check(poll(V, False, rep=1, started=2.0) == ["alert.fps.rec_fin_pertes"], "…et son bilan")
check(poll(V, True, started=3.0) == [] and poll(V, False, started=3.0) == [], "enregistrement sans perte : ni alerte ni bilan")
check(poll(V, True, started=4.0, drop=3) == ["alert.fps.rec_pertes"],
      "même si la première image perdue tombe avant le premier relevé de la session")
check(poll(V, True, started=5.0, drop=9) == ["alert.fps.rec_pertes"],
      "arrêt et redémarrage entre deux relevés (seul l'instant de départ a changé) : réarmé")

W = 502
check(poll(W, True, drop=7) == ["alert.fps.rec_pertes"], "orchestrateur relancé en plein enregistrement en perte : alerte")
X = 503
check(poll(X, False, drop=7) == [], "enregistrement déjà terminé vu après coup : rien")
check(poll(X, False, drop=7, err="ffmpeg s'est arrêté (code -9)") == ["alert.fps.rec_erreur"], "record_error qui apparaît : alerte")
check(poll(X, False, drop=7, err="ffmpeg s'est arrêté (code -9)") == [], "…une seule fois")
before = len(ALERTS)
metrics._noter_pertes_enregistrement(504, {"fps": 50.0, "frame_index": 3})
metrics._noter_pertes_enregistrement(505, {"recording": True})
check(len(ALERTS) == before, "pas un recorder (ou sans compteurs) : ignoré")

print("2. Cadence")
src = open(os.path.join(ROOT, "app", "metrics.py"), encoding="utf-8").read()
_loop = src[src.index("    m = get_metrics(ip)\n"):src.index("\ndef pyramide_overview")]
check("\n    _noter_pertes_enregistrement(vmid, m)\n" in _loop, "branché sur le relevé :8080 de chaque conteneur")
metrics.db_get_setting = None
before = len(ALERTS)
c = {"status": "running", "hostname": "rec", "node_id": 1}
dc = {"type": "recorder", "params": {"fps": 50}}
metrics._cadence_cible = lambda dc: 50.0
for _ in range(40):
    CLOCK[0] += 5
    metrics._check_cadence(601, c, dc, 0.0, {"recording": False, "dropped_frames": 0, "fps": 0.0})
check(len(ALERTS) == before, "recorder ARRÊTÉ à 0 fps : pas de « cadence non tenue »")
for _ in range(40):
    CLOCK[0] += 5
    metrics._check_cadence(602, c, dc, 0.0, {"fps": 0.0})
check(any(a[0].startswith("alert.fps.non_tenue") for a in ALERTS[before:]),
      "un autre conteneur à 0 fps alerte toujours (la garde ne vise que le recorder arrêté)")

print("3. Recorder")
try:
    import numpy  # noqa: F401
except ImportError:
    print("numpy absent : partie 3 sautée")
else:
    from unittest import mock
    bm = types.ModuleType("bobimxl")
    bm.Instance = bm.Reader = bm.AudioReader = mock.MagicMock()
    bm.lib_info = lambda: None
    bm.MXL_UNDEFINED_INDEX = (1 << 64) - 1
    bm.MXL_GRAIN_FLAG_INVALID = 1
    bm.now_tai = lambda: 0
    sys.modules["bobimxl"] = bm
    from app import plugins
    tmp = tempfile.mkdtemp(prefix="rec_loss_")
    rsrc = plugins.render_script("recorder", {"video_shm": "x", "media_root": tmp}, "rec-1")
    MARK = "try: os.makedirs(MEDIA_ROOT, exist_ok=True)"
    check(rsrc.count(MARK) == 1, "point d'arrêt avant les serveurs repéré")
    g = {"__name__": "recorder_loss"}
    exec(compile(rsrc.split(MARK)[0], "recorder/script.py", "exec"), g)
    g["log"] = lambda *a, **k: None

    def finish(rc):
        sess = types.SimpleNamespace(err="", loop_lock=threading.Lock(), closed=[], loop_s=0)
        seg = types.SimpleNamespace(pipes=[], proc=types.SimpleNamespace(returncode=rc), wrap=None, fifos=[],
                                    path=os.path.join(tmp, "absent.mov"), name="clip.mov", frames=10,
                                    sess=sess, files=lambda: [], _sidecar=lambda *a: None)
        g["_finish_ffmpeg"] = lambda p: None
        g["metrics"]["record_error"] = ""
        g["_Segment"].finish(seg)
        return sess.err, g["metrics"]["record_error"]
    e, pub = finish(-9)
    check("mal clos" in e and pub == e, "encodeur tué en fin de fichier : record_error rempli (%r)" % pub)
    e, pub = finish(0)
    check(e == "" and pub == "", "fichier bien clos : rien")
    check('metrics["rec_started"] = time.time()' in rsrc, "début de session publié sur :8080")

print()
if fails:
    print("%d échec(s)" % len(fails))
    sys.exit(1)
print("Pertes d'enregistrement : tous les cas tiennent.")
