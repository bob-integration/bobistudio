#!/usr/bin/env python3
"""Selftest du convertisseur v210↔planar (bobimxl.v210_pack/v210_unpack).

Vérifie, pour plusieurs géométries (dont largeur non multiple de 6 → queue de groupe) :
  1. chemin C (libbobi_v210.so) ≡ repli numpy, octet pour octet (pack ET unpack) ;
  2. round-trip 10 bits : planar → v210 → planar identique ;
  3. round-trip 8 bits : planar8 → v210 (<<2) → planar8 (>>2) identique ;
  4. padding de stride à zéro ;
puis mesure le débit 1080p (pack, unpack, aller-retour) sur le chemin actif.

Usage : PYTHONPATH=script_templates [BOBI_V210_LIB=…/libbobi_v210.so] python3 v210_selftest.py
Sans lib chargeable, les tests 2-4 tournent sur le repli numpy (le test 1 est sauté).
"""
import os
import sys
import time

import numpy as np

import bobimxl as bx

rng = np.random.default_rng(20260712)
FAIL = 0


def check(label, ok):
    global FAIL
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAIL += 1


def rand_planar(w, h, bit_depth):
    if bit_depth <= 8:
        return rng.integers(0, 256, bx.frame_bytes(w, h, "422", 8), dtype=np.uint8)
    a = rng.integers(0, 1024, bx.frame_bytes(w, h, "422", 10) // 2, dtype=np.uint16)
    return a


def with_fallback(fn, *a, **kw):
    """Exécute fn en forçant le repli numpy, puis restaure le chemin C."""
    saved = bx._v210_lib
    bx._v210_lib = False
    try:
        return fn(*a, **kw)
    finally:
        bx._v210_lib = saved


def main():
    have_c = bx._v210_load() is not None
    print(f"chemin C : {'libbobi_v210 chargée' if have_c else 'ABSENTE (repli numpy seul)'}")

    for (w, h) in [(1920, 1080), (720, 576), (1286, 4), (48, 2), (100, 3)]:
        print(f"-- {w}x{h}")
        for bd in (10, 8):
            src = rand_planar(w, h, bd)
            packed = bx.v210_pack(src, w, h, bit_depth=bd)
            unpacked = bx.v210_unpack(packed, w, h, bit_depth=bd)
            check(f"round-trip {bd}b", np.array_equal(unpacked, src))
            if have_c:
                check(f"pack C ≡ numpy {bd}b",
                      np.array_equal(packed, with_fallback(bx.v210_pack, src, w, h,
                                                           bit_depth=bd)))
                check(f"unpack C ≡ numpy {bd}b",
                      np.array_equal(unpacked, with_fallback(bx.v210_unpack, packed,
                                                             w, h, bit_depth=bd)))
        # padding : les octets au-delà des groupes utiles doivent être à zéro
        stride, ngrp = bx.v210_stride(w), -(-w // 6)
        if 16 * ngrp < stride:
            pad = packed.reshape(h, stride)[:, 16 * ngrp:]
            check("padding stride à zéro", not pad.any())

    # -- débit 1080p (chemin actif)
    w, h, n = 1920, 1080, 50
    src10 = rand_planar(w, h, 10)
    packed = bx.v210_pack(src10, w, h, bit_depth=10)
    out = np.empty(bx.v210_frame_bytes(w, h), dtype=np.uint8)
    for label, f in [
        ("pack10  ", lambda: bx.v210_pack(src10, w, h, bit_depth=10, out=out)),
        ("unpack10", lambda: bx.v210_unpack(packed, w, h, bit_depth=10)),
        ("unpack8 ", lambda: bx.v210_unpack(packed, w, h, bit_depth=8)),
    ]:
        f()                                     # chauffe
        t0 = time.perf_counter()
        for _ in range(n):
            f()
        dt = (time.perf_counter() - t0) / n * 1e3
        print(f"  {label} 1080p : {dt:6.2f} ms/image")

    print("échecs :", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
