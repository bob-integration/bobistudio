# Producteur de BANC (tools/bancs/rdma_tranche) : 1080p50 planar 4:2:2 8 bits, grille TAI, chaque
# tranche (en OCTETS, découpage du patch mxl-planar-slices) estampillée : 8 o d'index de grain
# + 8 o de n° de tranche au DÉBUT et à la FIN de la tranche, reste rempli de (index & 0xff).
# usage : prod.py NAME SLICES(0=monolithique|30) PACE(fast|spread) DUREE_S
import sys, time, os
STALL = int(os.environ.get("STALL_EVERY", "0"))   # toutes les N trames, retard de 45 ms → 2 index sautés
import numpy as np
sys.path.insert(0, "/bench")
import bobimxl as b

name, nsl, pace, dur = sys.argv[1], int(sys.argv[2]), sys.argv[3], float(sys.argv[4])
W, H = 1920, 1080
inst = b.Instance()
w = b.Writer(inst, name, W, H, "422", 8, 50, 1, slice_height=(H // nsl if nsl else 0))
N = nsl or 1
period = 20_000_000
t_end = time.monotonic() + dur
n = 0
while time.monotonic() < t_end:
    idx, gi, view = w.open_grain()
    tot = gi.totalSlices
    L = gi.grainSize // tot
    stamp_k = 30                       # tranches logiques estampillées (indépendant de N)
    Ls = gi.grainSize // stamp_k
    per_commit = stamp_k // tot
    t0 = time.monotonic_ns()
    for s in range(stamp_k):
        seg = view[s * Ls:(s + 1) * Ls]
        seg[:] = idx & 0xff
        seg[:16].view(np.uint64)[:] = (idx, s)
        seg[-16:].view(np.uint64)[:] = (idx, s)
        if (s + 1) % per_commit == 0:
            k = (s + 1) // per_commit
            if k < tot:
                w.commit(gi, valid_slices=k)
            if pace == "spread":
                time.sleep(0.0005)
    w.commit(gi)
    n += 1
    if STALL and n % STALL == 0:
        time.sleep(0.045)
    # dormir jusqu'au créneau suivant de la grille
    nxt = w.index_time_ns(idx + 1)
    d = (nxt - b.now_tai()) / 1e9
    if d > 0:
        time.sleep(d)
print("frames", n, flush=True)
