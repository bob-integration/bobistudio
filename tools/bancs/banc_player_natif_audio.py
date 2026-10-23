#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Banc AUDIO de bobiplayout (cf. docs/chantiers/PLAYER_NATIF.md), même environnement que
banc_player_natif.py. Pistes vues, routage / mute / solo / gain (niveaux), avance et alignement
temporel audio/vidéo sur le bus, continuité de l'audio à travers des boucles par tête armée.
    banc_player_natif_audio.py /mnt/media/<fichier>.mxf progressive|interlaced_tff <fps>"""
import os, sys, time
sys.path.insert(0, "/src/gst-bobimxl/harness")
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import numpy as np
import bobimxl

Gst.init(None)
path, scan, fps = sys.argv[1], sys.argv[2], int(sys.argv[3])
W, H = 1920, 1080
pipe = Gst.Pipeline.new("playout")
po = Gst.ElementFactory.make("bobiplayout", "po")
po.set_property("video-flow-def", bobimxl.build_flow_def("bancnatif", W, H, "422", 10, fps, 1, interlace=scan))
po.set_property("audio-flow-def", bobimxl.build_audio_flow_def("bancnatif_audio", 8, 48000))
pipe.add(po); pipe.set_state(Gst.State.PLAYING)
VCAPS = "video/x-raw,format=I422_10LE,width=%d,height=%d,framerate=%d/1,pixel-aspect-ratio=1/1,colorimetry=bt709" % (W, H, fps)
decks = {}

def add_deck(name):
    dec = Gst.ElementFactory.make("uridecodebin", "dec_" + name)
    dec.set_property("uri", Gst.filename_to_uri(path))
    pipe.add(dec)
    st = {"video": False, "bins": [], "pads": [], "na": 0}
    def on_pad(_d, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        kind = caps.get_structure(0).get_name()
        if kind.startswith("video/") and not st["video"]:
            st["video"] = True
            b = Gst.parse_bin_from_description("queue max-size-time=300000000 ! videoconvert ! videoscale ! videorate ! capsfilter caps=\"%s\"" % VCAPS, True)
            sp = po.request_pad_simple("video_" + name)
        elif kind.startswith("audio/"):
            ok, ch = caps.get_structure(0).get_int("channels")
            b = Gst.parse_bin_from_description("queue max-size-time=300000000 ! audioconvert ! audioresample ! capsfilter caps=\"audio/x-raw,format=F32LE,rate=48000,layout=interleaved,channels=%d\"" % (ch if ok else 2), True)
            sp = po.request_pad_simple("audio_%s_%d" % (name, st["na"])); st["na"] += 1
        else:
            b = Gst.ElementFactory.make("fakesink", None); b.set_property("async", False); sp = None
        pipe.add(b); b.sync_state_with_parent()
        if sp is not None:
            b.get_static_pad("src").link(sp); st["pads"].append(sp)
        pad.link(b.get_static_pad("sink")); st["bins"].append(b)
    dec.connect("pad-added", on_pad)
    dec.sync_state_with_parent()
    decks[name] = (dec, st)

def remove_deck(name):
    dec, st = decks.pop(name)
    for e in [dec] + st["bins"]:
        e.set_state(Gst.State.NULL)
    for sp in st["pads"]:
        peer = sp.get_peer()
        if peer: peer.unlink(sp)
        po.release_request_pad(sp)
    for e in [dec] + st["bins"]:
        pipe.remove(e)

p = po.get_property
lv = lambda: [round(float(x), 2) for x in p("audio-levels").split(",")]
inst = bobimxl.Instance()
add_deck("A"); po.set_property("active-deck", "A"); time.sleep(5)
print("[pistes]   audio-tracks=%r  route=%s" % (p("audio-tracks"), p("audio-route")))
print("[niveaux]  routage défaut :", lv())
po.set_property("audio-route", "3,4,1,2,0,0,0,0"); time.sleep(0.6); print("[niveaux]  route 3,4,1,2 :", lv())
po.set_property("audio-route", "1,1,1,1,1,1,1,1"); time.sleep(0.6); print("[niveaux]  source 1 partout :", lv())
po.set_property("audio-mute", "1,0,0,0,0,0,0,0"); time.sleep(0.6); print("[niveaux]  mute sortie 1 :", lv())
po.set_property("audio-mute", "0,0,0,0,0,0,0,0"); po.set_property("audio-solo", "0,1,0,0,0,0,0,0"); time.sleep(0.6); print("[niveaux]  solo sortie 2 :", lv())
po.set_property("audio-solo", "0,0,0,0,0,0,0,0"); po.set_property("audio-gains", "0.5,1,1,1,1,1,1,1"); time.sleep(0.6); print("[niveaux]  gain 0,5 sortie 1 :", lv())
po.set_property("audio-gains", "1,1,1,1,1,1,1,1"); po.set_property("audio-route", "1,2,3,4,5,6,7,8")

# Bus : avance et alignement temporel vidéo / audio (têtes en TAI)
ar = bobimxl.AudioReader(inst, "bancnatif_audio"); vr = bobimxl.Reader(inst, "bancnatif")
a1, v1 = ar.head_index(), vr.head_index(); time.sleep(2); a2, v2 = ar.head_index(), vr.head_index()
ta = ar.index_time_ns(a2); tv = vr_t = None
w = bobimxl.Writer.__new__(bobimxl.Writer)
rate_v = bobimxl.mxlRational(fps * (2 if scan.startswith("interlaced") else 1), 1)
tv = int(bobimxl._lib.mxlIndexToTimestamp(bobimxl.ctypes.byref(rate_v), int(v2)))
print("[bus]      audio +%d échantillons/s, vidéo +%d grains/s ; fin audio − début du dernier grain vidéo = %.1f ms"
      % ((a2 - a1) / 2, (v2 - v1) / 2, (ta - tv) / 1e6))

# Continuité de l'audio à travers une boucle (clip court seulement) : on lit tout le flux entre deux
# instants et on vérifie qu'aucun bloc ne manque (index contigus côté écrivain = lecture sans trou).
import subprocess
dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path], capture_output=True, text=True).stdout or 0)
if 0 < dur <= 30:
    m0, r0 = p("slots-missed"), p("repeated-frames")
    for nxt, cur in (("B", "A"), ("C", "B")):
        add_deck(nxt); po.set_property("next-deck", nxt)
        t = time.time()
        while p("active-deck") != nxt and time.time() - t < 60: time.sleep(0.05)
        print("[boucle %s] niveaux juste après la bascule : %s" % (nxt, lv()))
        # Crêtes par bloc de 20 ms sur la voie 1, lues SUR LE BUS, de 0,3 s avant à 0,3 s après la jointure.
        time.sleep(0.4)
        h = ar.head_index()
        blk = ar.read_from(h - 48000, 48000)
        if blk is not None:
            pk = [round(float(np.abs(blk[i:i + 960, 0]).max()), 2) for i in range(0, 48000, 960)]
            print("[jointure %s] crêtes /20 ms (voie 1, dernière seconde du bus) : %s" % (nxt, pk))
        remove_deck(cur)
        time.sleep(2.5); print("[boucle %s] niveaux 2,5 s après la bascule : %s" % (nxt, lv()))
    print("[boucles]  2 bascules : répétées=%d manqués=%d audio-tracks=%r niveaux=%s" % (p("repeated-frames") - r0, p("slots-missed") - m0, p("audio-tracks"), lv()))
pipe.set_state(Gst.State.NULL)
