#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Banc ANC de bobiplayout (cf. docs/chantiers/PLAYER_NATIF.md), même environnement que
banc_player_natif.py : têtes vidéo / anc_1 / anc_2 au même index, TC décodé par bobimxl.anc_unpack
= TC annoncé, OP-47 présent, TC antenne montant / décompte / remise à zéro.
    banc_player_natif_anc.py /mnt/media/<fichier>.mxf progressive|interlaced_tff <fps>"""
import sys, time
sys.path.insert(0, "/src/gst-bobimxl/harness")
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import bobimxl

Gst.init(None)
path, scan, fps = sys.argv[1], sys.argv[2], int(sys.argv[3])
W, H = 1920, 1080
pipe = Gst.Pipeline.new("playout")
po = Gst.ElementFactory.make("bobiplayout", "po")
po.set_property("video-flow-def", bobimxl.build_flow_def("bancnatif", W, H, "422", 10, fps, 1, interlace=scan))
po.set_property("anc1-flow-def", bobimxl.build_data_flow_def("bancnatif_anc_1", fps, 1))
po.set_property("anc2-flow-def", bobimxl.build_data_flow_def("bancnatif_anc_2", fps, 1))
pipe.add(po); pipe.set_state(Gst.State.PLAYING)
VCAPS = "video/x-raw,format=I422_10LE,width=%d,height=%d,framerate=%d/1,pixel-aspect-ratio=1/1,colorimetry=bt709" % (W, H, fps)
dec = Gst.ElementFactory.make("uridecodebin", "dec_A"); dec.set_property("uri", Gst.filename_to_uri(path)); pipe.add(dec)
vb = Gst.parse_bin_from_description("queue ! videoconvert ! videoscale ! videorate ! capsfilter caps=\"%s\"" % VCAPS, True)
pipe.add(vb); vb.get_static_pad("src").link(po.request_pad_simple("video_A"))
def on_pad(_d, pad):
    k = (pad.get_current_caps() or pad.query_caps(None)).get_structure(0).get_name()
    if k.startswith("video/") and not vb.get_static_pad("sink").is_linked():
        pad.link(vb.get_static_pad("sink"))
    else:
        fs = Gst.ElementFactory.make("fakesink", None); fs.set_property("async", False)
        pipe.add(fs); fs.sync_state_with_parent(); pad.link(fs.get_static_pad("sink"))
dec.connect("pad-added", on_pad); vb.sync_state_with_parent(); dec.sync_state_with_parent()

tc_fps = fps
base = 10 * 3600 * tc_fps                          # TC embarqué fictif : 10:00:00:00
po.set_property("deck-meta", "A|%d|20.0|60.0" % base)   # fin du clip à 20 s, 60 s après
op47 = bobimxl.op47_encode_lignes(["BANC NATIF"], magazine=8)
po.set_property("anc-extra", '[{"did":67,"sdid":2,"line":20,"hori":4095,"udw":"%s"}]' % bytes(op47).hex())
po.set_property("active-deck", "A")
time.sleep(4)
inst = bobimxl.Instance()
rv = bobimxl.Reader(inst, "bancnatif"); r1 = bobimxl.Reader(inst, "bancnatif_anc_1"); r2 = bobimxl.Reader(inst, "bancnatif_anc_2")
for _ in range(3):
    hv, h1, h2 = rv.head_index(), r1.head_index(), r2.head_index()
    vframe = hv // 2 if scan.startswith("interlaced") else hv
    print("[têtes]    vidéo(trame)=%d anc_1=%d anc_2=%d  écart=%d,%d" % (vframe, h1, h2, h1 - vframe, h2 - vframe))
    time.sleep(0.2)
for name, rd, prop in (("anc_1 (clip)", r1, "tc-clip"), ("anc_2 (antenne)", r2, "tc-antenna")):
    got = rd.get_latest()
    pk = bobimxl.anc_unpack(got[2], rd.inst.flow_def(rd.name)) if got else []
    tc = bobimxl.anc_atc_decode(pk)
    dids = sorted({(p["did"], p["sdid"]) for p in pk})
    print("[%s] TC décodé=%s  annoncé=%s  paquets(did,sdid)=%s  checksums=%s" % (
        name, "%02d:%02d:%02d:%02d" % tc[:4] if tc else None, po.get_property(prop), dids,
        all(p.get("checksum_ok", True) for p in pk)))
pos = po.get_property("position")
print("[antenne]  up : %s ; position %.2f s" % (po.get_property("tc-antenna"), pos))
po.set_property("antenna-mode", "down"); time.sleep(0.3)
pos = po.get_property("position")
print("[antenne]  down : %s ; attendu ≈ %.2f s (20 − pos + 60)" % (po.get_property("tc-antenna"), 20 - pos + 60))
po.set_property("antenna-mode", "up"); po.set_property("antenna-reset", True); time.sleep(1)
print("[antenne]  up après remise à zéro + 1 s : %s" % po.get_property("tc-antenna"))
pipe.set_state(Gst.State.NULL)
