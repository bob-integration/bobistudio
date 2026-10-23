#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Calage A/V d'une mire avsync MESURÉ SUR LE BUS, sans consommateur entre deux : pour chaque flash,
l'instant TAI du 1er grain vidéo clair (index de grain → temps) contre l'instant TAI du 1er
échantillon du bip (index d'échantillon → temps). Lecture seule. À lancer dans un conteneur qui
voit le domaine MXL. Usage : banc_avsync_tai.py [NOM_FLUX] [SECONDES]"""
import sys, time
import numpy as np
import bobimxl

name = sys.argv[1] if len(sys.argv) > 1 else "avsync"
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 6
inst = bobimxl.Instance()
vr = bobimxl.Reader(inst, name); ar = bobimxl.AudioReader(inst, name + "_audio")
f = vr.format(); w, h = f["width"], f["height"]
k = vr.head_index() + 1
a = ar.head_index()
t_end = time.time() + secs
flashes, beeps = [], []
prev_lum = None; prev_e = 0.0
while time.time() < t_end:
    g = vr.get(k, timeout_ns=200_000_000)
    if g is not None:
        lum = float(g[2][:w * h:97].mean())
        if prev_lum is not None and lum > 128 and prev_lum <= 128:
            flashes.append(vr.index_time_ns(k))
        prev_lum = lum; k += 1
    while True:
        r = ar.read_from(a, 48)
        if r is None:
            break
        e = float((r[:, 0] ** 2).sum())
        if e > 1.0 and prev_e <= 1.0:
            # 1er échantillon au-dessus du seuil dans ce bloc d'1 ms
            i = int(np.argmax(np.abs(r[:, 0]) > 0.05))
            beeps.append((a + i) * 1_000_000_000 // 48000)
        prev_e = e; a += 48
print("flashes=%d bips=%d" % (len(flashes), len(beeps)))
for fl in flashes:
    b = min(beeps, key=lambda x: abs(x - fl), default=None)
    if b is not None and abs(b - fl) < 400_000_000:
        print("bip − flash = %+.2f ms" % ((b - fl) / 1e6))
