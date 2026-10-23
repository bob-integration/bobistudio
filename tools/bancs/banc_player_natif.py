#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Banc de l'élément natif bobiplayout (plugins/_media_runtime/gst-bobimxl) — cf. docs/chantiers/PLAYER_NATIF.md.

Exige un système VIVANT : à lancer dans le conteneur de dev `bobi-rust-dev` (dl360-1), avec
GST_PLUGIN_PATH pointant le .so construit et bobimxl.py copié dans /src/gst-bobimxl/harness/ :
    banc_player_natif.py /mnt/media/<fichier>.mxf progressive|interlaced_tff <fps>
Mesure : lecture (neuves, manqués, répétées), boucles par tête ARMÉE (clips ≤ 30 s), pause/reprise,
tête du flux sur le bus. Toutes les mesures viennent des compteurs de l'élément lui-même."""
import os, sys, time
sys.path.insert(0, "/src/gst-bobimxl/harness")
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import bobimxl

Gst.init(None)
path, scan, fps = sys.argv[1], sys.argv[2], int(sys.argv[3])
W, H = 1920, 1080
fdef = bobimxl.build_flow_def("bancnatif", W, H, "422", 10, fps, 1, interlace=scan)
opts = bobimxl._flow_options()

pipe = Gst.Pipeline.new("playout")
po = Gst.ElementFactory.make("bobiplayout", "po")
po.set_property("video-flow-def", fdef)
if opts:
    po.set_property("writer-options", opts.decode())
pipe.add(po)
pipe.set_state(Gst.State.PLAYING)

VCAPS = "video/x-raw,format=I422_10LE,width=%d,height=%d,framerate=%d/1,pixel-aspect-ratio=1/1,colorimetry=bt709" % (W, H, fps)
decks = {}

def add_deck(name):
    """Une tête : uridecodebin → (vidéo) conversions → video_<name> ; audio → fakesink (phase 1)."""
    dec = Gst.ElementFactory.make("uridecodebin", "dec_" + name)
    dec.set_property("uri", Gst.filename_to_uri(path))
    vb = Gst.parse_bin_from_description(
        "queue max-size-time=300000000 ! videoconvert ! videoscale ! videorate ! capsfilter caps=\"%s\"" % VCAPS, True)
    pipe.add(dec); pipe.add(vb)
    sinkpad = po.request_pad_simple("video_" + name)
    vb.get_static_pad("src").link(sinkpad)
    state = {"video": False, "extra": []}
    def on_pad(_d, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        kind = caps.get_structure(0).get_name()
        if kind.startswith("video/") and not state["video"]:
            state["video"] = True
            pad.link(vb.get_static_pad("sink"))
        else:
            fs = Gst.ElementFactory.make("fakesink", None)
            fs.set_property("sync", False); fs.set_property("async", False)
            pipe.add(fs); fs.sync_state_with_parent(); pad.link(fs.get_static_pad("sink"))
            state["extra"].append(fs)
    dec.connect("pad-added", on_pad)
    vb.sync_state_with_parent(); dec.sync_state_with_parent()
    decks[name] = (dec, vb, sinkpad, state)

def remove_deck(name):
    dec, vb, sinkpad, state = decks.pop(name)
    for e in [dec, vb] + state["extra"]:
        e.set_state(Gst.State.NULL)
    vb.get_static_pad("src").unlink(sinkpad)
    po.release_request_pad(sinkpad)
    for e in [dec, vb] + state["extra"]:
        pipe.remove(e)

def p(name):
    return po.get_property(name)

def snap():
    return "fps=%.1f neuves=%.1f manqués=%d répétées=%d bascules=%d pos=%.2f file=%d eos=%s" % (
        p("fps"), p("fresh-fps"), p("slots-missed"), p("repeated-frames"), p("deck-switches"),
        p("position"), p("queued-frames"), p("eos"))

# 1. Lecture continue
add_deck("A"); po.set_property("active-deck", "A")
time.sleep(6)
base_rep = p("repeated-frames"); base_miss = p("slots-missed")
time.sleep(5)
print("[lecture] ", snap(), "| répétées en 5 s :", p("repeated-frames") - base_rep, "| manqués :", p("slots-missed") - base_miss)

# 2. Boucles par tête ARMÉE : on prépare la suivante (même fichier) pendant que l'active joue.
# Seulement sur un clip COURT : chaque bascule attend la fin naturelle du clip (au plus 60 s).
import subprocess
dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                            "default=nw=1:nk=1", path], capture_output=True, text=True).stdout or 0)
names = ["B", "C", "D", "E"] if 0 < dur <= 30 else []
if not names:
    print("[boucles]  ignorées : clip de %.0f s (> 30 s)" % dur)
cur = "A"
r0, m0, sw0 = p("repeated-frames"), p("slots-missed"), p("deck-switches")
for nxt in names:
    add_deck(nxt); po.set_property("next-deck", nxt)
    t = time.time()
    while p("active-deck") != nxt and time.time() - t < 60:
        time.sleep(0.05)
    if p("active-deck") != nxt:
        print("[boucles]  ÉCHEC : la tête %s n'a pas pris l'antenne en 60 s" % nxt); break
    remove_deck(cur); cur = nxt
if names: print("[boucles]  %d bascules, trames répétées = %d, créneaux manqués = %d | %s" % (
    p("deck-switches") - sw0, p("repeated-frames") - r0, p("slots-missed") - m0, snap()))

# 3. Pause / reprise
m0 = p("slots-missed"); lat = []
for i in range(5):
    po.set_property("paused", True); time.sleep(0.6)
    pos = p("position"); time.sleep(0.4)
    figee = abs(p("position") - pos) < 1e-6
    t = time.time(); po.set_property("paused", False)
    while not p("playing") and time.time() - t < 3:
        time.sleep(0.002)
    lat.append((time.time() - t) * 1e3); time.sleep(1)
print("[pause]    figée en pause=%s, reprise %.0f-%.0f ms, manqués=%d" % (figee, min(lat), max(lat), p("slots-missed") - m0))

# Tête du flux sur le bus (vu par un lecteur bobimxl)
rd = bobimxl.Reader(bobimxl.Instance(), "bancnatif")
h1 = rd.head_index(); time.sleep(1); h2 = rd.head_index()
print("[bus]      tête du flux : +%d grains en 1 s (%s)" % (h2 - h1, scan))
pipe.set_state(Gst.State.NULL)
