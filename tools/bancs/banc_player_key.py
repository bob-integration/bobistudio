#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Banc de la SORTIE KEY de l'élément bobiplayout (≥ 0.3.4) — exige l'image média et un domaine MXL.

Lit le fill et le key au MÊME index et compare le key à l'alpha de l'image source (ffmpeg
alphaextract), au pixel près ; puis : clip sans alpha → key opaque ; noir → key transparent ;
flux 10 bits ; entrelacé ; sans `key-flow-def` → comportement inchangé. Les formats proposés
sont ceux du player (sans alpha d'abord, avec alpha ensuite).

Dans un conteneur de l'image média (ou bobi-rust-dev avec GST_PLUGIN_PATH vers l'élément compilé),
DOSSIER contenant bobimxl.py (script_templates/), un QuickTime Animation ARGB d'1 s à 60 i/s
`qtrle_1s.mov` et un clip sans alpha `noalpha_1s.mov` :

    python3 banc_player_key.py DOSSIER

Mesuré le 2026-10-28 sur dl360-1 (bobi-rust-dev, élément 0.3.4) : tout tient ; key = alpha de la
source au pixel près en 8 bits, ±1 en 10 bits (mise à l'échelle de videoconvert). Mutation (alpha
lu sur le plan de luminance) : 2 échecs.
"""
import os, subprocess, sys, time
D = sys.argv[1]
sys.path.insert(0, D)
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import numpy as np
import bobimxl

Gst.init(None)
W, H = 1920, 1080
FAIL = []


def check(ok, msg):
    print(("  ok    " if ok else "  ÉCHEC ") + msg)
    if not ok:
        FAIL.append(msg)


def run(name, path, bits, fps, scan="progressive", key=True, black=False):
    pipe = Gst.Pipeline.new("p" + name)
    po = Gst.ElementFactory.make("bobiplayout", "po")
    fill_id, key_id = "bk_" + name, "bk_" + name + "_key"
    po.set_property("video-flow-def", bobimxl.build_flow_def(fill_id, W, H, "422", bits, fps, 1, interlace=scan))
    if key:
        po.set_property("key-flow-def", bobimxl.build_flow_def(key_id, W, H, "422", bits, fps, 1, interlace=scan))
    pipe.add(po)
    pipe.set_state(Gst.State.PLAYING)
    fmts = "{ Y42B, I422_10LE, I422_12LE, A422, A422_10LE, A422_12LE }" if bits == 8 else "{ I422_10LE, A422_10LE }"
    vcaps = "video/x-raw,format=%s,width=%d,height=%d,framerate=%d/1,pixel-aspect-ratio=1/1" % (fmts, W, H, fps)
    dec = Gst.ElementFactory.make("uridecodebin", "dec_A")
    dec.set_property("uri", Gst.filename_to_uri(path))
    pipe.add(dec)
    weave = "interlace field-pattern=1:1 top-field-first=true ! " if scan.startswith("interlaced") else ""
    vb = Gst.parse_bin_from_description(
        "queue ! videoconvert ! videoscale ! %svideorate ! capsfilter caps=\"%s\"" % (weave, vcaps), True)
    pipe.add(vb)
    vb.get_static_pad("src").link(po.request_pad_simple("video_A"))
    neg = {}

    def on_pad(_d, pad):
        k = (pad.get_current_caps() or pad.query_caps(None)).get_structure(0).get_name()
        if k.startswith("video/") and not vb.get_static_pad("sink").is_linked():
            pad.link(vb.get_static_pad("sink"))
        else:
            fs = Gst.ElementFactory.make("fakesink", None)
            fs.set_property("async", False)
            pipe.add(fs); fs.sync_state_with_parent(); pad.link(fs.get_static_pad("sink"))
    dec.connect("pad-added", on_pad)
    vb.sync_state_with_parent(); dec.sync_state_with_parent()
    po.set_property("active-deck", "A")
    time.sleep(0.6)
    if black:
        po.set_property("hold-black", True)
    else:
        po.set_property("paused", True)          # image FIGÉE : fill et key comparables à loisir
    time.sleep(0.6)
    caps = vb.get_static_pad("src").get_current_caps()
    neg["fmt"] = caps.get_structure(0).get_string("format") if caps else None
    res = {"fmt": neg["fmt"], "alpha": po.get_property("key-alpha") if key else None}
    import json
    ws = json.loads(po.get_property("sync-state") or "{}")
    res["pts"] = ws.get("pts")
    inst = bobimxl.Instance()
    rf = bobimxl.Reader(inst, fill_id)
    res["fill_head"] = rf.head_index()
    if key:
        rk = bobimxl.Reader(inst, key_id)
        # Têtes lues dans l'ordre key, fill, key : le fill d'un créneau est écrit AVANT son key, donc
        # k1 ≤ f et f − k2 ≤ un créneau (2 champs en entrelacé). Une lecture isolée de chaque tête
        # mesurait l'instant de lecture, pas la sortie.
        lags = []
        for _ in range(200):
            k1 = rk.head_index(); f = rf.head_index(); k2 = rk.head_index()
            lags.append((f - k1, f - k2))
            time.sleep(0.003)
        res["lags"] = lags
        res["key_head"] = rk.head_index()
        g = rk.get_latest()
        fd = inst.flow_def(key_id)
        res["key"] = g
        res["key_fd"] = fd
    pipe.set_state(Gst.State.NULL)
    return res


def same_slot(r, interlaced=False):
    lim = 2 if interlaced else 1
    bad = [x for x in r["lags"] if x[0] < 0 or x[1] > lim]
    eq = sum(1 for x in r["lags"] if x[1] == 0)
    return not bad and eq >= len(r["lags"]) * 0.8, "%d/%d lectures à index égal, %d hors bornes" % (eq, len(r["lags"]), len(bad))


def key_planes(g, bits, interlaced=False):
    """Grain key → plan Y (H, W) en entiers. En entrelacé, un grain = un champ (H/2 lignes)."""
    buf = g[2] if isinstance(g, (tuple, list)) else g
    h = H // 2 if interlaced else H
    dt = np.uint8 if bits == 8 else np.dtype("<u2")
    a = np.frombuffer(bytes(buf), dtype=dt)
    y = a[:W * h].reshape(h, W)
    c = a[W * h:W * h + 2 * (W // 2) * h]
    return y.astype(np.int32), c.astype(np.int32)


def alpha_ref(path, pts, fps_src=60):
    n = int(round(pts * fps_src))
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "select=eq(n\\,%d),alphaextract,format=gray" % n,
                          "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True).stdout
    return np.frombuffer(out, dtype=np.uint8).reshape(H, W).astype(np.int32), n


print("1. QuickTime Animation ARGB, flux 8 bits 4:2:2 50p")
r = run("q8", os.path.join(D, "qtrle_1s.mov"), 8, 50)
check(r["fmt"] == "A422", "format négocié avec alpha : %s" % r["fmt"])
check(r["alpha"] is True, "propriété key-alpha vraie")
ok_, why = same_slot(r)
check(ok_, "key écrit au créneau de son fill (%s)" % why)
y, c = key_planes(r["key"], 8)
ref, n = alpha_ref(os.path.join(D, "qtrle_1s.mov"), r["pts"])
diff = int(np.abs(y - ref).max())
check(diff == 0, "key = alpha de l'image source n°%d au pixel près (écart max %d, moyenne %.2f / %.2f)" % (
    n, diff, y.mean(), ref.mean()))
check(int(c.min()) == 128 and int(c.max()) == 128, "chroma du key neutre (128)")

print("2. Clip SANS alpha (ProRes 422), flux 8 bits")
r = run("n8", os.path.join(D, "noalpha_1s.mov"), 8, 50)
y, c = key_planes(r["key"], 8)
check(r["alpha"] is False, "propriété key-alpha fausse (format %s)" % r["fmt"])
check(int(y.min()) == 255 and int(y.max()) == 255, "key OPAQUE (Y = 255 partout)")

print("3. Noir (hold-black)")
r = run("b8", os.path.join(D, "qtrle_1s.mov"), 8, 50, black=True)
y, c = key_planes(r["key"], 8)
check(int(y.max()) == 0 and int(c.min()) == 128 == int(c.max()), "key TRANSPARENT (Y = 0), chroma neutre")

print("4. Flux 10 bits")
r = run("q10", os.path.join(D, "qtrle_1s.mov"), 10, 50)
y, c = key_planes(r["key"], 10)
ref, n = alpha_ref(os.path.join(D, "qtrle_1s.mov"), r["pts"])
# videoconvert ARGB 8 bits → A422_10LE : alpha mis à l'échelle (a << 2 | a >> 6)
exp = (ref << 2) | (ref >> 6)
check(r["fmt"] == "A422_10LE" and r["alpha"] is True, "format %s, key-alpha %s" % (r["fmt"], r["alpha"]))
check(int(np.abs(y - exp).max()) <= 1 and int(c.min()) == 512 == int(c.max()),
      "key 10 bits = alpha mis à l'échelle (écart max %d), chroma 512" % int(np.abs(y - exp).max()))
r = run("n10", os.path.join(D, "noalpha_1s.mov"), 10, 50)
y, _c = key_planes(r["key"], 10)
check(int(y.min()) == 1023 == int(y.max()), "clip sans alpha en 10 bits : key = 1023")

print("5. Entrelacé 1080i25 (source 50p tissée)")
r = run("i8", os.path.join(D, "qtrle_1s.mov"), 8, 25, scan="interlaced_tff")
ok_, why = same_slot(r, interlaced=True)
check(r["alpha"] is True and ok_, "key-alpha vrai, key au créneau de son fill (%s)" % why)

print("6. Sans key-flow-def : comportement inchangé")
r = run("x8", os.path.join(D, "noalpha_1s.mov"), 8, 50, key=False)
check(r["fill_head"] and r["fill_head"] > 0, "fill écrit sans sortie key (index %s)" % r["fill_head"])

print()
print("ÉCHECS : %d" % len(FAIL) if FAIL else "Banc key : tout tient.")
sys.exit(1 if FAIL else 0)
