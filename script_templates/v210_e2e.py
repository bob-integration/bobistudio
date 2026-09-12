#!/usr/bin/env python3
"""E2E interop v210 — À EXÉCUTER DANS un conteneur bobi-compute:0.10 (libmxl + bobimxl + SIMD).

Chaîne réelle sur le bus MXL, noms préfixés v210test_ (aucun flux prod touché) :
  1. producteur PLANAR       → v210test_src        (video/x-mxl-planar, N trames motif connu)
  2. pont EXPORT             → v210test_src_v210   (video/v210, flowDef STOCK, même index)
  3. pont IMPORT (par-flowId brut du miroir) → v210test_back (planar) ; round-trip bit-exact
  4. découverte : discover_flows() voit-il le miroir en video/v210 ?
  5. SIMD chargé ? (doit être True dans l'image 0.10)

Sortie : lignes "OK/FAIL ...", et "RESULT: PASS|FAIL". Nettoie ses propres shm en sortant.
"""
import sys, time
import numpy as np
import bobimxl as bx

W, H, N = 1920, 1080, 8
FAIL = [0]
def chk(label, ok):
    print(("OK   " if ok else "FAIL ") + label);
    if not ok: FAIL[0] += 1

inst = bx.Instance()

def planar_frame(k, bit_depth):
    """Motif déterministe (gradient + numéro de trame) pour comparer bit-exact."""
    dt = np.uint8 if bit_depth <= 8 else np.uint16
    maxv = (1 << bit_depth) - 1
    n = bx.frame_bytes(W, H, "422", bit_depth) // (1 if bit_depth <= 8 else 2)
    a = (np.arange(n, dtype=np.uint64) + np.uint64(k * 2654435761)) & np.uint64(maxv)
    return a.astype(dt)

def run(bit_depth):
    tag = "%db" % bit_depth
    src_name = "v210test_src_%s" % tag
    v210_name = src_name + "_v210"
    back_name = "v210test_back_%s" % tag

    print("SIMD:", bx._v210_load() is not None)
    chk("SIMD lib chargée (%s)" % tag, bx._v210_load() is not None)

    # 1. producteur planar
    wsrc = bx.Writer(inst, src_name, W, H, "422", bit_depth, 50, 1, index_mode="free")
    frames = [planar_frame(k, bit_depth) for k in range(N)]
    for k in range(N):
        wsrc.write(frames[k], index=k)

    # 2. export planar → v210 (au même index)
    rsrc = bx.Reader(inst, src_name)
    fmt = rsrc.format()
    chk("format source lisible (%s)" % tag, bool(fmt) and fmt["bit_depth"] == bit_depth)
    wv = bx.Writer(inst, v210_name, W, H, "422", 10, 50, 1,
                   index_mode="free", media_type="video/v210")
    for k in range(N):
        got = rsrc.get(k, timeout_ns=200_000_000)
        chk("lecture planar idx %d (%s)" % (k, tag), got is not None)
        _i, gi, view = got
        _oi, ogi, ovw = wv.open_grain(index=k)
        bx.v210_pack(view[:bx.frame_bytes(W, H, "422", bit_depth)], W, H,
                     bit_depth=bit_depth, out=ovw)
        wv.commit(ogi)

    # 3. découverte : le miroir doit apparaître en video/v210
    flows = {f["id"]: f for f in bx.discover_flows()}
    fid_mirror = bx.flow_id(v210_name)
    seen = flows.get(fid_mirror)
    chk("miroir découvert en video/v210 (%s)" % tag,
        bool(seen) and seen["media_type"] == "video/v210")

    # 4. import PAR-FLOWID BRUT du miroir → planar (comme un flux tiers)
    rv = bx.Reader(inst, fid_mirror, by_id=True)
    fmt_v = rv.format()
    chk("format miroir lisible par-flowId (%s)" % tag,
        bool(fmt_v) and fmt_v["width"] == W and fmt_v["height"] == H)
    wb = bx.Writer(inst, back_name, W, H, "422", bit_depth, 50, 1, index_mode="free")
    for k in range(N):
        got = rv.get(k, timeout_ns=200_000_000)
        chk("lecture v210 idx %d (%s)" % (k, tag), got is not None)
        _i, gi, view = got
        _oi, ogi, ovw = wb.open_grain(index=k)
        bx.v210_unpack(view[:bx.v210_frame_bytes(W, H)], W, H,
                       bit_depth=bit_depth, out=ovw)
        wb.commit(ogi)

    # 5. round-trip bit-exact planar→v210→planar
    rb = bx.Reader(inst, back_name)
    dt = np.uint8 if bit_depth <= 8 else np.uint16
    alleq = True
    for k in range(N):
        got = rb.get(k, timeout_ns=200_000_000)
        if got is None:
            alleq = False; break
        _i, gi, view = got
        back = view.view(dt)[:frames[k].size]
        # 10 bits : identité stricte. 8 bits : v210_pack(<<2) puis v210_unpack(>>2) = identité.
        if not np.array_equal(back, frames[k] & 0xFF if bit_depth <= 8 else frames[k]):
            alleq = False
            print("   diff trame %d : %d/%d samples" %
                  (k, int(np.sum(back != (frames[k] & 0xFF if bit_depth <= 8 else frames[k]))),
                   frames[k].size))
            break
    chk("round-trip planar↔v210 bit-exact (%s)" % tag, alleq)

    for w in (wsrc, wv, wb):
        try: w.close()
        except Exception: pass
    for r in (rsrc, rv, rb):
        try: r.close()
        except Exception: pass

try:
    run(10)
    run(8)
finally:
    try: inst.garbage_collect()
    except Exception: pass
    inst.close()

print("RESULT:", "PASS" if FAIL[0] == 0 else "FAIL (%d)" % FAIL[0])
sys.exit(1 if FAIL[0] else 0)
