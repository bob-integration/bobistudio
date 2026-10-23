# Lecteur « de tête » (motif get_latest des consommateurs : head puis head-1, non bloquant) :
# pour chaque head distinct vu, l'image rendue est-elle celle de son index ? Âge = i − estampille.
import sys, time, ctypes, json
import numpy as np
from collections import Counter
sys.path.insert(0, "/bench")
import bobimxl as b
name, dur = sys.argv[1], float(sys.argv[2])
inst = b.Instance(); r = b.Reader(inst, name)
seen = {}; t_end = time.monotonic() + dur
while time.monotonic() < t_end:
    h = r.head_index()
    for c in (h, h - 1):
        gi = b.mxlGrainInfo(); p = b._PU8()
        if b._lib.mxlFlowReaderGetGrainNonBlocking(r._h, c, ctypes.byref(gi), ctypes.byref(p)) == 0:
            if c not in seen:
                v = b._np_view(p, gi.grainSize); Ls = gi.grainSize // 30
                st = [int(v[s * Ls:s * Ls + 8].view(np.uint64)[0]) for s in range(30)]
                seen[c] = (c - min(st), len(set(st)) > 1, bool(gi.flags & 1))
            break
    time.sleep(0.004)
ages = Counter(a for a, t, f in seen.values())
print(json.dumps({"name": name, "images_vues": len(seen), "age_hist": dict(ages),
                  "torn": sum(t for a, t, f in seen.values()), "invalid_flag": sum(f for a, t, f in seen.values()),
                  "age_moyen": round(sum(a for a, t, f in seen.values()) / max(1, len(seen)), 2)}))
