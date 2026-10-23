# Lecteur de BANC : pour chaque grain COMPLET i (get bloquant), relève les 30 estampilles
# (début ET fin de tranche) : écart i − estampille (contenu vieux sous index frais), déchirure
# (estampilles différentes), et instant de disponibilité (TAI local − instant de grille de i).
# usage : rd.py NAME DUREE_S [offset_ns à SOUSTRAIRE de l'horloge locale]
import sys, time, json
import numpy as np
sys.path.insert(0, "/bench")
import bobimxl as b
name, dur = sys.argv[1], float(sys.argv[2])
off = int(sys.argv[3]) if len(sys.argv) > 3 else 0
inst = b.Instance()
r = None
t_end = time.monotonic() + 3
while r is None and time.monotonic() < t_end:
    try:
        r = b.Reader(inst, name)
    except Exception:
        time.sleep(0.1)
h = r.head_index()
i = h + 1
res = []
t_end = time.monotonic() + dur
while time.monotonic() < t_end:
    g = r.get(i, timeout_ns=200_000_000)
    tnow = b.now_tai() - off
    if g is None:
        hh = r.head_index()
        res.append({"i": i, "miss": True, "head": hh})
        i = max(i + 1, hh)
        continue
    _, gi, v = g
    Ls = gi.grainSize // 30
    st = [int(v[s * Ls:s * Ls + 8].view(np.uint64)[0]) for s in range(30)]
    en = [int(v[(s + 1) * Ls - 16:(s + 1) * Ls - 8].view(np.uint64)[0]) for s in range(30)]
    st2 = [int(v[s * Ls:s * Ls + 8].view(np.uint64)[0]) for s in range(30)]
    res.append({"i": i, "vs": gi.validSlices, "inv": bool(gi.flags & 1), "tot": gi.totalSlices, "hidx": gi.index,
                "st": st, "en": en, "race": st != st2,
                "avail_ms": (tnow - r.index_time_ns(i)) / 1e6})
    i += 1
ok = [x for x in res if not x.get("miss")]
okv = [x for x in ok if not x["inv"]]
inv = [x for x in ok if x["inv"]]
lag = [i_ - s for x in ok for i_, s in [(x["i"], min(x["st"] + x["en"]))]]
torn = [x for x in ok if len(set(x["st"] + x["en"])) > 1]
exact = [x for x in ok if set(x["st"] + x["en"]) == {x["i"]}]
av = sorted(x["avail_ms"] for x in ok)
from collections import Counter
print(json.dumps({
    "name": name, "grains": len(ok), "miss": len(res) - len(ok),
    "exact": len(exact), "invalid_flagged": len(inv), "stale_unflagged": sum(1 for x in okv if min(x["st"]+x["en"]) != x["i"]), "stale_flagged": sum(1 for x in inv if min(x["st"]+x["en"]) != x["i"]), "torn": len(torn), "races": sum(x["race"] for x in ok),
    "lag_hist(i-min_stamp)": dict(Counter(lag).most_common(8)),
    "avail_ms_p10_50_90": [round(av[int(len(av) * q)], 2) for q in (0.1, 0.5, 0.9)] if av else None,
    "vs_hist": dict(Counter(x["vs"] for x in ok)),
    "torn_examples": [{"i": x["i"], "st": [x["i"] - s for s in x["st"]], "en": [x["i"] - s for s in x["en"]]} for x in torn[:3]],
}))
