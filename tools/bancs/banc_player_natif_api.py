#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Banc du PLAYER (moteur natif bobiplayout), piloté par son API :8082 et lu sur :8080 :
les 6 MXF du corpus, routage, pause/reprise, seek, pas à pas, vitesse, boucle et playlist sans
trou. À lancer dans le conteneur d'un player ≥ 0.26 (cf. docs/chantiers/PLAYER_NATIF.md).
Usage : banc_player_natif_api.py [SOUS-DOSSIER] — sous-dossier du corpus sous la racine média."""
import json, sys, time, urllib.request
D = (sys.argv[1].rstrip("/") + "/") if len(sys.argv) > 1 else ""
def call(method, path, body=None, port=8082):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method,
                                 data=(json.dumps(body).encode() if body is not None else None),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")
for _ in range(60):
    try: call("GET", "/state"); break
    except Exception: time.sleep(0.5)
m = lambda: call("GET", "/", port=8080)
print("[moteur]", m().get("engine"))

for f in ["avci_16mono.mxf", "dnxhd_8mono.mxf", "avci_1x8ch.mxf", "mpeg2_stereo.mxf", "avci_sans_audio.mxf", "dnxhd_1080p50.mxf"]:
    call("POST", "/file", {"file": D + f}); time.sleep(5)
    s = call("GET", "/state"); mm = m()
    print("[fichier] %-20s playing=%s fps=%s neuves=%s pistes=%s niveaux=%s manqués=%s" % (
        f, s.get("playing"), mm.get("fps"), mm.get("fresh_fps"), s.get("audio_tracks"),
        [round(x, 2) for x in (s.get("levels") or [])][:4], mm.get("slots_missed")))

# Routage à chaud
call("POST", "/file", {"file": D + "dnxhd_8mono.mxf"}); time.sleep(4)
call("POST", "/audio_route", {"route": [3, 4, 1, 2, 0, 0, 0, 0]}); time.sleep(1)
print("[routage] 3,4,1,2 →", [round(x, 2) for x in call("GET", "/state")["levels"]][:4])
call("POST", "/audio_route", {"route": [1, 2, 3, 4, 5, 6, 7, 8]})

# Pause / reprise
r0, m0 = m().get("repeated_frames"), m().get("slots_missed"); lat = []
for i in range(5):
    call("POST", "/pause"); time.sleep(0.6)
    p0 = call("GET", "/state")["position"]; time.sleep(0.4); fig = call("GET", "/state")["position"] == p0
    t = time.time(); call("POST", "/play")
    while not m().get("playing") and time.time() - t < 3: time.sleep(0.005)
    lat.append((time.time() - t) * 1e3); time.sleep(1)
print("[pause]   figée=%s reprise %.0f-%.0f ms répétées=%s manqués=%s" % (fig, min(lat), max(lat),
      m().get("repeated_frames") - r0, m().get("slots_missed") - m0))

# Seek, pas à pas, vitesse
call("POST", "/seek", {"seconds": 20.0}); time.sleep(1.5)
print("[seek]    20 s → position %.2f" % call("GET", "/state")["position"])
call("POST", "/pause"); time.sleep(0.3); p0 = call("GET", "/state")["position"]
call("POST", "/step", {"frames": 10}); time.sleep(1.5); s = call("GET", "/state")
print("[step]    +10 trames : %.2f → %.2f, en pause=%s" % (p0, s["position"], s["paused"]))
call("POST", "/play"); call("POST", "/speed", {"speed": 2.0}); time.sleep(2)
a = call("GET", "/state")["position"]; time.sleep(1); b = call("GET", "/state")["position"]
print("[vitesse] ×2 : %.2f s média par seconde" % (b - a))
call("POST", "/speed", {"speed": 1.0})

# Boucle sans trou (clip court en boucle) : bascules et trames répétées pendant 20 s
call("POST", "/loop", {"loop": True}); call("POST", "/file", {"file": D + "dnxhd_1080p50.mxf"}); time.sleep(3)
r0, sw0, m0 = m().get("repeated_frames"), m().get("deck_switches"), m().get("slots_missed")
time.sleep(25)
print("[boucle]  25 s sur un clip de 11 s : bascules=%s répétées=%s manqués=%s eof=%s" % (
    m().get("deck_switches") - sw0, m().get("repeated_frames") - r0, m().get("slots_missed") - m0,
    call("GET", "/state").get("eof")))

# Playlist : trois clips enchaînés
call("POST", "/playlist", {"files": [D + "dnxhd_1080p50.mxf", D + "avci_16mono.mxf", D + "dnxhd_1080p50.mxf"]})
call("POST", "/plopts", {"auto": True, "loop": False}); call("POST", "/plgoto", {"index": 0, "play": True})
time.sleep(2); r0, sw0 = m().get("repeated_frames"), m().get("deck_switches")
time.sleep(30)
s = call("GET", "/state")
print("[playlist] 3 clips : bascules=%s répétées=%s index=%s eof=%s" % (
    m().get("deck_switches") - sw0, m().get("repeated_frames") - r0, s.get("pl_index"), s.get("eof")))
print("[journal]", (m().get("ffmpeg_log") or [])[-4:])
