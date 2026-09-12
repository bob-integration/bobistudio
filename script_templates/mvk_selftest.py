#!/usr/bin/env python3
"""Selftest du moteur de compositing mvcompose (fusion d'opérations de compositing).

Vérifie, pour plusieurs géométries (dont tailles impaires, petites bboxes, grandes bboxes) :
  1. chemin C (libbobi_mvk.so) ≡ références numpy, octet pour octet ;
  2. blend_u8/u16 : dst = (dst·(255−α) + src·α) / 255 ;
  3. blend_pre_u8/u16 : dst = (dst·inv_a + src_a) / 255 (opérandes pré-calculés) ;
  4. place_u8/u16 : gather nearest indices → écriture canvas ;
  5. chemin C avec col_step>0 (décimation régulière) et col_step≤0 (gather générique) ;
  6. déterminisme : résultats bit-exact avec mvk_set_threads(1) vs mvk_set_threads(4).

Usage : [BOBI_MVK_LIB=…/libbobi_mvk.so] python3 mvk_selftest.py
Sans lib chargeable : exit code 2 avec message clair.
"""
import os
import sys
import ctypes
from ctypes import POINTER, c_uint8, c_uint16, c_int32, c_int, c_int64, c_void_p
from ctypes import c_size_t, CDLL

import numpy as np

rng = np.random.default_rng(0)
FAIL = 0


def check(label, ok):
    global FAIL
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAIL += 1


def load_mvk_lib():
    """Charge libbobi_mvk.so selon la chaîne env BOBI_MVK_LIB et les fallbacks."""
    candidates = []

    # 1. env BOBI_MVK_LIB
    env_lib = os.environ.get("BOBI_MVK_LIB")
    if env_lib:
        candidates.append(env_lib)

    # 2. ./libbobi_mvk.so
    candidates.append("./libbobi_mvk.so")

    # 3. Check for AVX2 in /proc/cpuinfo
    has_avx2 = False
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if "avx2" in line.lower():
                    has_avx2 = True
                    break
    except OSError:
        pass

    # 4. /usr/local/lib/libbobi_mvk_v3.so (si avx2)
    if has_avx2:
        candidates.append("/usr/local/lib/libbobi_mvk_v3.so")

    # 5. /usr/local/lib/libbobi_mvk.so
    candidates.append("/usr/local/lib/libbobi_mvk.so")

    for path in candidates:
        try:
            lib = CDLL(path)
            print(f"Lib chargée : {path}")
            return lib
        except OSError:
            pass

    # Aucune lib trouvée
    print("ERREUR : aucune libbobi_mvk.so trouvée.", file=sys.stderr)
    print(f"Essayées : {', '.join(candidates)}", file=sys.stderr)
    sys.exit(2)


def define_mvk_bindings(lib):
    """Définit les signatures ctypes pour les fonctions C."""

    # mvk_set_threads(int n)
    lib.mvk_set_threads.argtypes = [c_int]
    lib.mvk_set_threads.restype = None

    # mvk_get_threads() -> int
    lib.mvk_get_threads.argtypes = []
    lib.mvk_get_threads.restype = c_int

    # mvk_blend_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
    #              const uint8_t *restrict src, ptrdiff_t src_stride,
    #              const uint8_t *restrict alpha, ptrdiff_t a_stride,
    #              int h, int w)
    lib.mvk_blend_u8.argtypes = [
        POINTER(c_uint8), c_int64,
        POINTER(c_uint8), c_int64,
        POINTER(c_uint8), c_int64,
        c_int, c_int
    ]
    lib.mvk_blend_u8.restype = None

    # mvk_blend_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
    #               const uint16_t *restrict src, ptrdiff_t src_stride,
    #               const uint8_t *restrict alpha, ptrdiff_t a_stride,
    #               int h, int w)
    lib.mvk_blend_u16.argtypes = [
        POINTER(c_uint16), c_int64,
        POINTER(c_uint16), c_int64,
        POINTER(c_uint8), c_int64,
        c_int, c_int
    ]
    lib.mvk_blend_u16.restype = None

    # mvk_blend_pre_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
    #                  const uint16_t *restrict inv_a, ptrdiff_t ia_stride,
    #                  const uint16_t *restrict src_a, ptrdiff_t sa_stride,
    #                  int h, int w)
    lib.mvk_blend_pre_u8.argtypes = [
        POINTER(c_uint8), c_int64,
        POINTER(c_uint16), c_int64,
        POINTER(c_uint16), c_int64,
        c_int, c_int
    ]
    lib.mvk_blend_pre_u8.restype = None

    # mvk_blend_pre_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
    #                   const uint32_t *restrict inv_a, ptrdiff_t ia_stride,
    #                   const uint32_t *restrict src_a, ptrdiff_t sa_stride,
    #                   int h, int w)
    lib.mvk_blend_pre_u16.argtypes = [
        POINTER(c_uint16), c_int64,
        POINTER(ctypes.c_uint32), c_int64,
        POINTER(ctypes.c_uint32), c_int64,
        c_int, c_int
    ]
    lib.mvk_blend_pre_u16.restype = None

    # mvk_place_u8(uint8_t *restrict dst, ptrdiff_t dst_stride,
    #              const uint8_t *restrict src, ptrdiff_t src_stride,
    #              const int32_t *restrict row_idx,
    #              int64_t col0, int64_t col_step,
    #              const int32_t *restrict col_idx,
    #              int out_h, int out_w)
    lib.mvk_place_u8.argtypes = [
        POINTER(c_uint8), c_int64,
        POINTER(c_uint8), c_int64,
        POINTER(c_int32),
        c_int64, c_int64,
        POINTER(c_int32),
        c_int, c_int
    ]
    lib.mvk_place_u8.restype = None

    # mvk_place_u16(uint16_t *restrict dst, ptrdiff_t dst_stride,
    #               const uint16_t *restrict src, ptrdiff_t src_stride,
    #               const int32_t *restrict row_idx,
    #               int64_t col0, int64_t col_step,
    #               const int32_t *restrict col_idx,
    #               int out_h, int out_w)
    lib.mvk_place_u16.argtypes = [
        POINTER(c_uint16), c_int64,
        POINTER(c_uint16), c_int64,
        POINTER(c_int32),
        c_int64, c_int64,
        POINTER(c_int32),
        c_int, c_int
    ]
    lib.mvk_place_u16.restype = None

    return lib


def test_blend_u8(lib):
    """Test mvk_blend_u8 : dst = (dst·(255−α) + src·α) / 255"""
    print("-- test_blend_u8")

    for (h, w) in [(5, 7), (37, 113), (1080, 1920)]:
        # Données aléatoires
        dst_init = rng.integers(0, 256, (h, w), dtype=np.uint8)
        src = rng.integers(0, 256, (h, w), dtype=np.uint8)
        alpha = rng.integers(0, 256, (h, w), dtype=np.uint8)

        # Référence numpy
        ref = ((dst_init.astype(np.uint32) * (255 - alpha.astype(np.uint32))
                + src.astype(np.uint32) * alpha.astype(np.uint32)) // 255).astype(np.uint8)

        # Test avec threads=1 et threads=4
        for num_threads in [1, 4]:
            dst = dst_init.copy()

            # Vérifie la contiguïté du dernier axe
            assert dst.strides[-1] == dst.itemsize, f"dst non contigu : stride[-1]={dst.strides[-1]} != itemsize={dst.itemsize}"
            assert src.strides[-1] == src.itemsize, f"src non contigu"
            assert alpha.strides[-1] == alpha.itemsize, f"alpha non contigu"

            # Strides en ÉLÉMENTS (pas en octets)
            dst_stride = dst.strides[0] // dst.itemsize
            src_stride = src.strides[0] // src.itemsize
            a_stride = alpha.strides[0] // alpha.itemsize

            lib.mvk_set_threads(num_threads)
            lib.mvk_blend_u8(
                dst.ctypes.data_as(POINTER(c_uint8)), dst_stride,
                src.ctypes.data_as(POINTER(c_uint8)), src_stride,
                alpha.ctypes.data_as(POINTER(c_uint8)), a_stride,
                h, w
            )

            ok = np.array_equal(dst, ref)
            check(f"blend_u8 {h}x{w} threads={num_threads}", ok)


def test_blend_u16(lib):
    """Test mvk_blend_u16 : dst = (dst·(255−α) + src·α) / 255 (données uint16)"""
    print("-- test_blend_u16")

    for (h, w) in [(5, 7), (37, 113), (1080, 1920)]:
        # Données 10-bit (0..1023)
        dst_init = rng.integers(0, 1024, (h, w), dtype=np.uint16)
        src = rng.integers(0, 1024, (h, w), dtype=np.uint16)
        alpha = rng.integers(0, 256, (h, w), dtype=np.uint8)

        # Référence numpy
        ref = ((dst_init.astype(np.uint32) * (255 - alpha.astype(np.uint32))
                + src.astype(np.uint32) * alpha.astype(np.uint32)) // 255).astype(np.uint16)

        for num_threads in [1, 4]:
            dst = dst_init.copy()

            assert dst.strides[-1] == dst.itemsize
            assert src.strides[-1] == src.itemsize
            assert alpha.strides[-1] == alpha.itemsize

            dst_stride = dst.strides[0] // dst.itemsize
            src_stride = src.strides[0] // src.itemsize
            a_stride = alpha.strides[0] // alpha.itemsize

            lib.mvk_set_threads(num_threads)
            lib.mvk_blend_u16(
                dst.ctypes.data_as(POINTER(c_uint16)), dst_stride,
                src.ctypes.data_as(POINTER(c_uint16)), src_stride,
                alpha.ctypes.data_as(POINTER(c_uint8)), a_stride,
                h, w
            )

            ok = np.array_equal(dst, ref)
            check(f"blend_u16 {h}x{w} threads={num_threads}", ok)


def test_blend_pre_u8(lib):
    """Test mvk_blend_pre_u8 : dst = (dst·inv_a + src_a) / 255

    inv_a, src_a = uint16 (pré-calculés).
    """
    print("-- test_blend_pre_u8")

    for (h, w) in [(5, 7), (37, 113), (1080, 1920)]:
        dst_init = rng.integers(0, 256, (h, w), dtype=np.uint8)
        alpha = rng.integers(0, 256, (h, w), dtype=np.uint8)
        src = rng.integers(0, 256, (h, w), dtype=np.uint8)

        # Pré-calcul des opérandes
        inv_a = (255 - alpha).astype(np.uint16)
        src_a = (src.astype(np.uint16) * alpha.astype(np.uint16))

        # Référence numpy
        ref = ((dst_init.astype(np.uint16) * inv_a + src_a) // 255).astype(np.uint8)

        for num_threads in [1, 4]:
            dst = dst_init.copy()

            assert dst.strides[-1] == dst.itemsize
            assert inv_a.strides[-1] == inv_a.itemsize
            assert src_a.strides[-1] == src_a.itemsize

            dst_stride = dst.strides[0] // dst.itemsize
            ia_stride = inv_a.strides[0] // inv_a.itemsize
            sa_stride = src_a.strides[0] // src_a.itemsize

            lib.mvk_set_threads(num_threads)
            lib.mvk_blend_pre_u8(
                dst.ctypes.data_as(POINTER(c_uint8)), dst_stride,
                inv_a.ctypes.data_as(POINTER(c_uint16)), ia_stride,
                src_a.ctypes.data_as(POINTER(c_uint16)), sa_stride,
                h, w
            )

            ok = np.array_equal(dst, ref)
            check(f"blend_pre_u8 {h}x{w} threads={num_threads}", ok)


def test_blend_pre_u16(lib):
    """Test mvk_blend_pre_u16 : dst = (dst·inv_a + src_a) / 255

    inv_a, src_a = uint32 (pré-calculés).
    """
    print("-- test_blend_pre_u16")

    for (h, w) in [(5, 7), (37, 113), (1080, 1920)]:
        dst_init = rng.integers(0, 1024, (h, w), dtype=np.uint16)
        alpha = rng.integers(0, 256, (h, w), dtype=np.uint8)
        src = rng.integers(0, 1024, (h, w), dtype=np.uint16)

        # Pré-calcul des opérandes (uint32)
        inv_a = (255 - alpha).astype(np.uint32)
        src_a = (src.astype(np.uint32) * alpha.astype(np.uint32))

        # Référence numpy
        ref = ((dst_init.astype(np.uint32) * inv_a + src_a) // 255).astype(np.uint16)

        for num_threads in [1, 4]:
            dst = dst_init.copy()

            assert dst.strides[-1] == dst.itemsize
            assert inv_a.strides[-1] == inv_a.itemsize
            assert src_a.strides[-1] == src_a.itemsize

            dst_stride = dst.strides[0] // dst.itemsize
            ia_stride = inv_a.strides[0] // inv_a.itemsize
            sa_stride = src_a.strides[0] // src_a.itemsize

            lib.mvk_set_threads(num_threads)
            lib.mvk_blend_pre_u16(
                dst.ctypes.data_as(POINTER(c_uint16)), dst_stride,
                inv_a.ctypes.data_as(POINTER(ctypes.c_uint32)), ia_stride,
                src_a.ctypes.data_as(POINTER(ctypes.c_uint32)), sa_stride,
                h, w
            )

            ok = np.array_equal(dst, ref)
            check(f"blend_pre_u16 {h}x{w} threads={num_threads}", ok)


def test_place_u8(lib):
    """Test mvk_place_u8 : dst[r,c] = src[row_idx[r], col_idx[c] ou col0+c*col_step]"""
    print("-- test_place_u8")

    for (src_h, src_w, out_h, out_w) in [(37, 113, 5, 7), (1080, 1920, 540, 960)]:
        src = rng.integers(0, 256, (src_h, src_w), dtype=np.uint8)
        row_idx = (np.arange(out_h) * src_h // out_h).astype(np.int32)

        # Test 1 : col_step > 0 (décimation régulière)
        col0 = 0
        col_step = src_w // out_w

        dst_init = np.zeros((out_h, out_w), dtype=np.uint8)

        # Référence numpy
        ref = src[np.ix_(row_idx, (col0 + np.arange(out_w) * col_step).astype(int))]

        for num_threads in [1, 4]:
            dst = dst_init.copy()

            assert dst.strides[-1] == dst.itemsize
            assert src.strides[-1] == src.itemsize

            dst_stride = dst.strides[0] // dst.itemsize
            src_stride = src.strides[0] // src.itemsize

            lib.mvk_set_threads(num_threads)
            lib.mvk_place_u8(
                dst.ctypes.data_as(POINTER(c_uint8)), dst_stride,
                src.ctypes.data_as(POINTER(c_uint8)), src_stride,
                row_idx.ctypes.data_as(POINTER(c_int32)),
                col0, col_step,
                None,  # col_idx unused when col_step > 0
                out_h, out_w
            )

            ok = np.array_equal(dst, ref)
            check(f"place_u8 {src_h}x{src_w}->{out_h}x{out_w} col_step threads={num_threads}", ok)

        # Test 2 : col_step <= 0 (gather générique)
        col_idx = (np.arange(out_w) * src_w // out_w).astype(np.int32)
        ref = src[np.ix_(row_idx, col_idx)]

        for num_threads in [1, 4]:
            dst = dst_init.copy()

            assert col_idx.strides[-1] == col_idx.itemsize
            col_idx_stride = col_idx.strides[0] // col_idx.itemsize

            lib.mvk_set_threads(num_threads)
            lib.mvk_place_u8(
                dst.ctypes.data_as(POINTER(c_uint8)), dst_stride,
                src.ctypes.data_as(POINTER(c_uint8)), src_stride,
                row_idx.ctypes.data_as(POINTER(c_int32)),
                0, 0,  # col_step = 0 triggers col_idx path
                col_idx.ctypes.data_as(POINTER(c_int32)),
                out_h, out_w
            )

            ok = np.array_equal(dst, ref)
            check(f"place_u8 {src_h}x{src_w}->{out_h}x{out_w} col_idx threads={num_threads}", ok)


def test_place_u16(lib):
    """Test mvk_place_u16 : dst[r,c] = src[row_idx[r], col_idx[c] ou col0+c*col_step]"""
    print("-- test_place_u16")

    for (src_h, src_w, out_h, out_w) in [(37, 113, 5, 7), (1080, 1920, 540, 960)]:
        src = rng.integers(0, 1024, (src_h, src_w), dtype=np.uint16)
        row_idx = (np.arange(out_h) * src_h // out_h).astype(np.int32)

        # Test 1 : col_step > 0
        col0 = 0
        col_step = src_w // out_w

        dst_init = np.zeros((out_h, out_w), dtype=np.uint16)
        ref = src[np.ix_(row_idx, (col0 + np.arange(out_w) * col_step).astype(int))]

        for num_threads in [1, 4]:
            dst = dst_init.copy()

            assert dst.strides[-1] == dst.itemsize
            assert src.strides[-1] == src.itemsize

            dst_stride = dst.strides[0] // dst.itemsize
            src_stride = src.strides[0] // src.itemsize

            lib.mvk_set_threads(num_threads)
            lib.mvk_place_u16(
                dst.ctypes.data_as(POINTER(c_uint16)), dst_stride,
                src.ctypes.data_as(POINTER(c_uint16)), src_stride,
                row_idx.ctypes.data_as(POINTER(c_int32)),
                col0, col_step,
                None,
                out_h, out_w
            )

            ok = np.array_equal(dst, ref)
            check(f"place_u16 {src_h}x{src_w}->{out_h}x{out_w} col_step threads={num_threads}", ok)

        # Test 2 : col_step <= 0
        col_idx = (np.arange(out_w) * src_w // out_w).astype(np.int32)
        ref = src[np.ix_(row_idx, col_idx)]

        for num_threads in [1, 4]:
            dst = dst_init.copy()

            lib.mvk_set_threads(num_threads)
            lib.mvk_place_u16(
                dst.ctypes.data_as(POINTER(c_uint16)), dst_stride,
                src.ctypes.data_as(POINTER(c_uint16)), src_stride,
                row_idx.ctypes.data_as(POINTER(c_int32)),
                0, 0,
                col_idx.ctypes.data_as(POINTER(c_int32)),
                out_h, out_w
            )

            ok = np.array_equal(dst, ref)
            check(f"place_u16 {src_h}x{src_w}->{out_h}x{out_w} col_idx threads={num_threads}", ok)


def test_strided_view(lib):
    """Test avec dst = vue stridée d'un canvas plus grand"""
    print("-- test_strided_view")

    h, w = 5, 7
    canvas_h, canvas_w = 13, 20
    offset_r, offset_c = 4, 6

    # Canvas grand avec vue stridée
    canvas = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    dst_init = canvas[offset_r:offset_r+h, offset_c:offset_c+w].copy()

    src = rng.integers(0, 256, (h, w), dtype=np.uint8)
    alpha = rng.integers(0, 256, (h, w), dtype=np.uint8)

    ref = ((dst_init.astype(np.uint32) * (255 - alpha.astype(np.uint32))
            + src.astype(np.uint32) * alpha.astype(np.uint32)) // 255).astype(np.uint8)

    dst = canvas[offset_r:offset_r+h, offset_c:offset_c+w]

    # Vérifie que la vue partage la mémoire
    assert dst.base is canvas or dst.base is not None
    assert dst.strides[-1] == dst.itemsize

    dst_stride = dst.strides[0] // dst.itemsize
    src_stride = src.strides[0] // src.itemsize
    a_stride = alpha.strides[0] // alpha.itemsize

    lib.mvk_set_threads(1)
    lib.mvk_blend_u8(
        dst.ctypes.data_as(POINTER(c_uint8)), dst_stride,
        src.ctypes.data_as(POINTER(c_uint8)), src_stride,
        alpha.ctypes.data_as(POINTER(c_uint8)), a_stride,
        h, w
    )

    ok = np.array_equal(dst, ref)
    check(f"strided_view blend_u8 {h}x{w} in {canvas_h}x{canvas_w}", ok)


def test_abi23_wrappers():
    """ABI 2 (rgba2yuv) + ABI 3 (mixf/mixmap) via les wrappers bobimxl — le chemin que la
    prod utilise. SKIP (pas FAIL) sur un .so plus vieux : le binding est tolérant par contrat."""
    print("-- test_abi23 (wrappers bobimxl)")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bobimxl
    if bobimxl._mvk_load() is None:
        print("  SKIP lib introuvable via bobimxl"); return

    # rgba2yuv : C ≡ formules numpy float32 (mêmes ordres), 4 combos entrée/sortie.
    if getattr(bobimxl, "_MVK_HAS_R2Y", False):
        def r2y_ref(arr, dt, scale, maxv, cw, ch):
            r = arr[..., 0].astype(np.float32); g = arr[..., 1].astype(np.float32)
            b = arr[..., 2].astype(np.float32)
            y = ((0.299 * r + 0.587 * g + 0.114 * b) * scale).clip(0, maxv).astype(dt)
            u = ((-0.169 * r - 0.331 * g + 0.500 * b + 128) * scale).clip(0, maxv).astype(dt)
            v = ((0.500 * r - 0.419 * g - 0.081 * b + 128) * scale).clip(0, maxv).astype(dt)
            def sub(p):
                pp = p.astype(np.uint32)
                if cw == 2: pp = (pp[:, 0::2] + pp[:, 1::2] + 1) // 2
                if ch == 2: pp = (pp[0::2, :] + pp[1::2, :] + 1) // 2
                return pp.astype(dt)
            return y, sub(u), sub(v)
        for indt, dt, scale, maxv in ((np.uint8, np.uint8, 1, 255),
                                      (np.uint8, np.uint16, 4, 1023),
                                      (np.float32, np.uint8, 1, 255),
                                      (np.float32, np.uint16, 4, 1023)):
            for cw, ch in ((2, 2), (2, 1), (1, 1)):
                a8 = rng.integers(0, 256, size=(72, 128, 4), dtype=np.uint8)
                arr = a8 if indt == np.uint8 else a8.astype(np.float32)
                got = bobimxl.mvk_rgba2yuv(arr, dt, scale, maxv, cw, ch)
                exp = r2y_ref(arr, dt, scale, maxv, cw, ch)
                check(f"rgba2yuv {indt.__name__}->{dt.__name__} {cw}x{ch}",
                      got is not None and all(np.array_equal(e, g) for e, g in zip(exp, got)))
    else:
        print("  SKIP rgba2yuv (ABI < 2)")

    # mixf / mixmap : C ≡ formules float32 du mixer (dissolve/additif/wipe), alias dst=a.
    if getattr(bobimxl, "_MVK_HAS_MIX", False):
        for dt, maxv in ((np.uint8, 255), (np.uint16, 1023)):
            for alpha in (0.0, 0.13, 0.5, 0.777, 1.0):
                a = rng.integers(0, maxv + 1, size=(108, 192), dtype=dt)
                b = rng.integers(0, maxv + 1, size=(108, 192), dtype=dt)
                ref = (a.astype(np.float32) * (1.0 - alpha)
                       + b.astype(np.float32) * alpha).astype(dt)
                d = np.empty_like(a)
                ok = bobimxl.mvk_mixf_into(d, a, b, 1.0 - alpha, alpha) \
                    and np.array_equal(ref, d)
                ca = min(1.0, 2.0 * (1.0 - alpha)); cb = min(1.0, 2.0 * alpha)
                ref2 = np.clip(a.astype(np.float32) * ca + b.astype(np.float32) * cb,
                               0, maxv).astype(dt)
                d2 = np.empty_like(a)
                ok = ok and bobimxl.mvk_mixf_into(d2, a, b, ca, cb, clip=True, maxv=maxv) \
                    and np.array_equal(ref2, d2)
                d3 = a.copy()
                ok = ok and bobimxl.mvk_mixf_into(d3, d3, b, 1.0 - alpha, alpha) \
                    and np.array_equal(ref, d3)
                m = rng.uniform(0, 1, size=(108, 192)).astype(np.float32)
                refm = (a.astype(np.float32) * (1.0 - m)
                        + b.astype(np.float32) * m).astype(dt)
                dm = np.empty_like(a)
                ok = ok and bobimxl.mvk_mixmap_into(dm, a, b, m) and np.array_equal(refm, dm)
                ah = a[::2, ::2].copy(); bh = b[::2, ::2].copy(); mh = m[::2, ::2]
                refh = (ah.astype(np.float32) * (1.0 - mh)
                        + bh.astype(np.float32) * mh).astype(dt)
                dh = np.empty_like(ah)
                ok = ok and bobimxl.mvk_mixmap_into(dh, ah, bh, m[::2], m_colstep=2) \
                    and np.array_equal(refh, dh)
                check(f"mixf/mixmap {dt.__name__} a={alpha}", ok)
    else:
        print("  SKIP mixf/mixmap (ABI < 3)")


def test_abi4_spl_wrappers():
    """ABI 4 (mvk_spl_* : compositing du plugin split) via les wrappers bobimxl. Arithmétique
    256e (>> 8), accumulation uint32 — RÉFÉRENCE = les expressions numpy de plugins/split/
    script.py (_adv_plane / _blend_rows / _paint_shadow_rows). SKIP sur .so plus vieux."""
    print("-- test_abi4 spl (wrappers bobimxl)")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bobimxl
    if bobimxl._mvk_load() is None:
        print("  SKIP lib introuvable via bobimxl"); return
    if not getattr(bobimxl, "_MVK_HAS_SPL", False):
        print("  SKIP spl (ABI < 4)"); return

    for dt, maxv in ((np.uint8, 255), (np.uint16, 1023)):
        for (h, w) in ((64, 96), (31, 47), (200, 260)):
            src = rng.integers(0, maxv + 1, size=(h + 20, w + 30), dtype=np.uint16).astype(dt)
            flat = rng.integers(0, src.size, size=(h, w)).astype(np.int64)
            riy = rng.integers(0, src.shape[0], size=h).astype(np.int32)
            cix = rng.integers(0, src.shape[1], size=w).astype(np.int32)
            ring_a = rng.integers(0, 257, size=(h, w)).astype(np.uint16)
            ring_c = rng.integers(0, maxv + 1, size=(h, w), dtype=np.uint16).astype(dt)
            alpha = rng.integers(0, 257, size=(h, w)).astype(np.uint16)
            dst0 = rng.integers(0, maxv + 1, size=(h, w), dtype=np.uint16).astype(dt)

            for mode in ("flat", "idx"):
                kw = ({"flat": flat} if mode == "flat" else {"row_idx": riy, "col_idx": cix})
                k0 = (np.take(src.ravel(), flat) if mode == "flat" else src[riy][:, cix])

                # (a) gather + ruban + masque
                ref = k0.astype(np.uint32)
                ref = ((ref * (256 - ring_a) + ring_c.astype(np.uint32) * ring_a) >> 8)
                ref = ((dst0.astype(np.uint32) * (256 - alpha) + ref * alpha) >> 8).astype(dt)
                ref = np.where(alpha >= 256,
                               ((k0.astype(np.uint32) * (256 - ring_a)
                                 + ring_c.astype(np.uint32) * ring_a) >> 8).astype(dt), ref)
                d = dst0.copy()
                ok = bobimxl.mvk_spl_compose_into(d, src, ring_a=ring_a, ring_c=ring_c,
                                                  alpha=alpha, **kw)
                check(f"spl_compose masque {dt.__name__} {h}x{w} [{mode}]",
                      ok and np.array_equal(d, ref))

                # (b) alpha SCALAIRE (opacité globale), sans ruban
                av = 77
                ref = ((dst0.astype(np.uint32) * (256 - av)
                        + k0.astype(np.uint32) * av) >> 8).astype(dt)
                d = dst0.copy()
                ok = bobimxl.mvk_spl_compose_into(d, src, a_scalar=av, **kw)
                check(f"spl_compose scalaire {dt.__name__} {h}x{w} [{mode}]",
                      ok and np.array_equal(d, ref))

                # (c) copie directe (alpha 256)
                d = dst0.copy()
                ok = bobimxl.mvk_spl_compose_into(d, src, a_scalar=256, **kw)
                check(f"spl_compose copie {dt.__name__} {h}x{w} [{mode}]",
                      ok and np.array_equal(d, k0))

                # (d) chemin FUSIONNÉ
                inv_a = (256 - alpha).astype(np.uint16)
                a1 = ((alpha.astype(np.uint32) * (256 - ring_a)) >> 8).astype(np.uint16)
                c2 = ((ring_c.astype(np.uint32)
                       * ((alpha.astype(np.uint32) * ring_a) >> 8)) >> 8).astype(np.uint32)
                ref = (((dst0.astype(np.uint32) * inv_a) >> 8)
                       + ((k0.astype(np.uint32) * a1) >> 8) + c2).clip(0, maxv).astype(dt)
                d = dst0.copy()
                ok = bobimxl.mvk_spl_fused_into(d, src, inv_a, a1, c2, maxv=maxv, **kw)
                check(f"spl_fused {dt.__name__} {h}x{w} [{mode}]", ok and np.array_equal(d, ref))

            # (e) couleur unie sous masque (ombre portée)
            val = 33
            ref = ((dst0.astype(np.uint32) * (256 - alpha) + val * alpha) >> 8).astype(dt)
            d = dst0.copy()
            ok = bobimxl.mvk_spl_solid_into(d, alpha, val)
            check(f"spl_solid {dt.__name__} {h}x{w}", ok and np.array_equal(d, ref))

    # Vue STRIDÉE du canvas (cas réel : bbox d'une box) + déterminisme multi-thread.
    canvas = rng.integers(0, 256, size=(300, 400), dtype=np.uint8)
    src = rng.integers(0, 256, size=(300, 400), dtype=np.uint8)
    flat = rng.integers(0, src.size, size=(120, 150)).astype(np.int64)
    alpha = rng.integers(0, 257, size=(120, 150)).astype(np.uint16)
    outs = []
    for n in (1, 4):
        bobimxl.mvk_set_threads(n)
        c = canvas.copy()
        v = c[40:160, 30:180]
        assert bobimxl.mvk_spl_compose_into(v, src, flat=flat, alpha=alpha)
        outs.append(c)
    check("spl_compose vue stridée + déterminisme 1 vs 4 threads",
          np.array_equal(outs[0], outs[1]))
    ref = canvas.copy()
    v = ref[40:160, 30:180]
    k = np.take(src.ravel(), flat)
    v[:] = np.where(alpha >= 256, k,
                    ((v.astype(np.uint32) * (256 - alpha)
                      + k.astype(np.uint32) * alpha) >> 8).astype(np.uint8))
    check("spl_compose vue stridée ≡ numpy", np.array_equal(outs[0], ref))

    # Refus PROPRE (→ repli numpy à l'appelant) : dtype/forme non conformes.
    bad = np.zeros((10, 10), dtype=np.int16)
    check("spl_compose refuse un dtype non supporté",
          bobimxl.mvk_spl_compose_into(bad, bad) is False)


def test_abi5_gradient_wrappers():
    """ABI 5 (mvk_spl_gradient : fond dégradé/wipe du split) via le wrapper bobimxl. Reproduit le
    kernel en float64 (le kernel calcule en double) et exige C ≡ numpy AU BIT. SKIP sur .so vieux."""
    print("-- test_abi5 gradient (wrappers bobimxl)")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bobimxl
    if bobimxl._mvk_load() is None:
        print("  SKIP lib introuvable via bobimxl"); return
    if not getattr(bobimxl, "_MVK_HAS_GRAD", False):
        print("  SKIP gradient (ABI < 5)"); return

    bayer8 = np.array([[0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
                       [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
                       [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
                       [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]], np.float32)
    bay = (bayer8 - np.float32(31.5)) / np.float32(64.0)
    bay_flat = np.ascontiguousarray(bay.ravel(), np.float32)
    AMP = 1.0

    def grad_eval(t, pos, yv, uv, vv, soft):
        n = pos.shape[0]
        p = pos.astype(np.float64); Y0 = yv.astype(np.float64); U0 = uv.astype(np.float64); V0 = vv.astype(np.float64)
        Y = np.full(t.shape, Y0[n - 1]); U = np.full(t.shape, U0[n - 1]); V = np.full(t.shape, V0[n - 1])
        for i in range(n - 1):
            seg = p[i + 1] - p[i]
            local = (t - p[i]) / seg if seg > 1e-9 else np.zeros_like(t)
            w = np.where(local < 0.5, 0.0, 1.0) if soft <= 1e-6 else np.clip((local - 0.5) / soft + 0.5, 0.0, 1.0)
            iw = 1.0 - w; m = (t >= p[i]) & (t < p[i + 1])
            Y[m] = (Y0[i] * iw + Y0[i + 1] * w)[m]
            U[m] = (U0[i] * iw + U0[i + 1] * w)[m]
            V[m] = (V0[i] * iw + V0[i + 1] * w)[m]
        le0 = t <= p[0]; ge1 = t >= p[n - 1]
        for A, Av in ((Y, Y0), (U, U0), (V, V0)):
            A[le0] = Av[0]; A[ge1] = Av[n - 1]
        return Y, U, V

    def f32(x):
        return float(np.float32(x))

    for dt, maxv in ((np.uint8, 255), (np.uint16, 1023)):
        for (W, H, cw, ch) in ((256, 144, 2, 2), (128, 72, 2, 1), (96, 48, 1, 1)):
            for angle in (0.0, 27.0, 90.0, 200.0):
                for soft, pos, cols in ((0.0, [0.0, 1.0], [(20, 40, 60), (200, 210, 220)]),        # wipe
                                        (1.0, [0.0, 1.0], [(16, 128, 128), (235, 40, 220)]),        # dégradé
                                        (0.4, [0.0, 0.5, 1.0], [(30, 200, 60), (128, 128, 128), (210, 60, 200)])):
                    pos = np.array(pos, np.float32)
                    yv = np.array([c[0] for c in cols], np.float32) * (maxv // 255 if maxv > 255 else 1)
                    uv = np.array([c[1] for c in cols], np.float32) * (maxv // 255 if maxv > 255 else 1)
                    vv = np.array([c[2] for c in cols], np.float32) * (maxv // 255 if maxv > 255 else 1)
                    th = np.radians(angle); cos = f32(np.cos(th)); sin = f32(np.sin(th))
                    projs = [x * cos + y * sin for x in (0.0, float(W)) for y in (0.0, float(H))]
                    pmin = f32(min(projs)); pmax = f32(max(projs))
                    inv = f32(1.0 / (pmax - pmin)) if pmax > pmin else 0.0
                    y = np.zeros((H, W), dt); u = np.zeros((H // ch, W // cw), dt); v = np.zeros((H // ch, W // cw), dt)
                    ok = bobimxl.mvk_spl_gradient_into(y, u, v, W, H, cw, ch, 0, H, cos, sin, pmin, inv,
                                                       pos, yv, uv, vv, f32(soft), maxv, AMP, bay_flat)
                    # référence numpy (float64), MÊME formule que _bg_gradient_np
                    dcos = np.float64(cos); dsin = np.float64(sin); dpm = np.float64(pmin); dinv = np.float64(inv)
                    amp = np.float64(AMP); soft64 = float(np.float64(f32(soft))); bayd = bay.astype(np.float64)
                    ys = np.arange(0, H, dtype=np.float64)[:, None]; xs = np.arange(W, dtype=np.float64)[None, :]
                    t = np.clip(((xs + 0.5) * dcos + (ys + 0.5) * dsin - dpm) * dinv, 0.0, 1.0)
                    Y, _, _ = grad_eval(t, pos, yv, uv, vv, soft64)
                    dy = amp * bayd[(np.arange(H)[:, None] & 7), (np.arange(W)[None, :] & 7)]
                    ry = np.clip(np.floor(Y + dy + 0.5), 0, maxv).astype(dt)
                    cy = np.arange(0, H // ch, dtype=np.float64)[:, None]; cx = np.arange(W // cw, dtype=np.float64)[None, :]
                    t2 = np.clip(((cx * cw + cw * 0.5) * dcos + (cy * ch + ch * 0.5) * dsin - dpm) * dinv, 0.0, 1.0)
                    _, U, Vv2 = grad_eval(t2, pos, yv, uv, vv, soft64)
                    dc = amp * bayd[(np.arange(H // ch)[:, None] & 7), (np.arange(W // cw)[None, :] & 7)]
                    ru = np.clip(np.floor(U + dc + 0.5), 0, maxv).astype(dt)
                    rv = np.clip(np.floor(Vv2 + dc + 0.5), 0, maxv).astype(dt)
                    good = ok and np.array_equal(y, ry) and np.array_equal(u, ru) and np.array_equal(v, rv)
                    check("gradient %s %dx%d cw%d ch%d a=%g soft=%g n=%d" % (dt.__name__, W, H, cw, ch, angle, soft, len(cols)), good)
    # refus propre
    bad = np.zeros((8, 8), np.int16)
    p2 = np.array([0.0, 1.0], np.float32)
    check("gradient refuse un dtype non supporté",
          bobimxl.mvk_spl_gradient_into(bad, bad, bad, 8, 8, 1, 1, 0, 8, 1.0, 0.0, 0.0, 1.0,
                                        p2, p2, p2, p2, 1.0, 255, 1.0, bay_flat) is False)


def main():
    lib = load_mvk_lib()
    lib = define_mvk_bindings(lib)

    print(f"Threads disponibles : {lib.mvk_get_threads()}")
    print()

    test_blend_u8(lib)
    print()

    test_blend_u16(lib)
    print()

    test_blend_pre_u8(lib)
    print()

    test_blend_pre_u16(lib)
    print()

    test_place_u8(lib)
    print()

    test_place_u16(lib)
    print()

    test_strided_view(lib)
    print()

    test_abi23_wrappers()
    print()

    test_abi4_spl_wrappers()
    print()

    test_abi5_gradient_wrappers()
    print()

    print(f"Échecs totaux : {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
