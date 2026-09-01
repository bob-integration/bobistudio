#!/usr/bin/env python3
"""Producteur du BANC CROISÉ interop — à lancer dans un conteneur bobi-compute (libmxl FORKÉE).

Publie en continu, pendant DURATION s, deux flux (noms préfixés v210xbench_, jamais de la prod) :
  - v210xbench_planar : video/x-mxl-planar  (notre type maison, fork-only)
  - v210xbench_v210   : video/v210          (le MIROIR, flowDef stock — ce que lit le tiers)
Imprime les deux flowId puis produit à 25 fps. Le lecteur STOCK (bobi-mxl-stock) tourne en
parallèle et doit LIRE le miroir v210 et REJETER le planar.
"""
import sys, time
import numpy as np
import bobimxl as bx

W, H, FPS, DURATION = 1920, 1080, 25, 90
PLANAR, MIRROR = "v210xbench_planar", "v210xbench_v210"

inst = bx.Instance()
wp = bx.Writer(inst, PLANAR, W, H, "422", 10, FPS, 1, index_mode="free")
wv = bx.Writer(inst, MIRROR, W, H, "422", 10, FPS, 1, index_mode="free",
               media_type="video/v210")

print("PLANAR_FLOWID=%s" % bx.flow_id(PLANAR), flush=True)
print("V210_FLOWID=%s" % bx.flow_id(MIRROR), flush=True)
print("READY", flush=True)

n = bx.frame_bytes(W, H, "422", 10) // 2
base = (np.arange(n, dtype=np.uint64) & np.uint64(1023)).astype(np.uint16)
t0 = time.time()
k = 0
while time.time() - t0 < DURATION:
    frame = (base + np.uint16(k & 1023)) & np.uint16(1023)
    wp.write(frame, index=k)                       # flux planar (type maison)
    _i, gi, ovw = wv.open_grain(index=k)           # miroir v210 (type stock)
    bx.v210_pack(frame, W, H, bit_depth=10, out=ovw)
    wv.commit(gi)
    k += 1
    time.sleep(max(0, (k / FPS) - (time.time() - t0)))
print("DONE %d trames" % k, flush=True)
wp.close(); wv.close()
inst.garbage_collect(); inst.close()
