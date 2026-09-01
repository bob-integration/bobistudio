#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS

"""
v210_slice_bench — banc jetable PLANAR vs V210 sur le VRAI bus MXL en mode TRANCHE.

Répond au TODO de la mesure 2026-07-11 (« recalculer après la phase tranches ») : les bancs
historiques simulaient les bandes en numpy/C hors bus (fmt_bench.py, v210_simd.c) ; ici la
chaîne est réelle — grains MXL, commit progressif validSlices, futex get_slice, conversion
par la VRAIE lib SIMD bit-exacte (libbobi_v210 via bobimxl.v210_pack/v210_unpack) :

    srcA ─┐
          ├─ stage (blend A+B par bande) ─→ out ─→ sink
    srcB ─┘

4 formats de BUS comparés (le travail utile — blend planar par bande — est identique) :
  planar8   : video/x-mxl-planar 8 b, slice_height=36 (patch mxl-planar-slices, N=30 tranches)
  planar10  : idem en PLANAR10LE (uint16, 2 o/sample)
  v210x8    : video/v210 STOCK (1 slice par LIGNE, totalSlices=hauteur) ; pipeline interne 8 b
              → chaque étage dé-packe/re-packe SES bandes (SIMD)
  v210x10   : idem, pipeline interne 10 b (uint16)

Mesures :
  - sink  : latence commit@src → observe@sink par bande (à TRAVERS l'étage), p50/p99/max ;
            coût de dé-packing consommateur (v210) ;
  - stage : travail par trame (unpack×2 + blend + pack vs blend seul), p50/p99 ;
  - src   : coût producteur par trame (memcpy vs pack SIMD) ;
  - octets bus par trame (empreinte mémoire/membw).

Usage (DANS un conteneur bobi-compute ≥ 0.10, domaine ISOLÉ hors prod) :
  python3 v210_slice_bench.py run --fmt planar8 --seconds 20
  python3 v210_slice_bench.py run --fmt v210x10 --seconds 20
  python3 v210_slice_bench.py all --seconds 20          # les 4 formats, JSON final agrégé
  python3 v210_slice_bench.py gc                        # nettoie le domaine du banc

Caveat de représentativité : le « travail utile » est un blend 50/50 par bande (1 op numpy) —
un étage réel (multiview compose mvk, correction colo) travaille plus, ce qui DILUE le surcoût
relatif de conversion v210 ; les latences bande-à-bande, elles, sont réalistes.
"""

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bobimxl  # noqa: E402

W, H, FPS = 1920, 1080, 50
BAND_LINES = 36                    # = slice_lines prod (tissu en tranches)
NB = H // BAND_LINES               # 30 bandes
DOMAIN_DEFAULT = "/dev/shm/mxl-bench-v210"

# En-tête posé en tête de CHAQUE bande (écrase les 20 premiers octets du payload de la bande —
# sans importance : le banc mesure des temps, la bit-exactitude est prouvée par v210_selftest) :
# uint64 seq | uint32 bande | uint64 ts_ns (CLOCK_REALTIME juste avant le commit CHEZ LA SOURCE,
# recopié verbatim par l'étage → le sink mesure la latence de CHAÎNE src→stage→sink).
_HDR = struct.Struct("<QIQ")

FMTS = ("planar8", "planar10", "v210x8", "v210x10")


def _is_v210(fmt):
    return fmt.startswith("v210")


def _depth(fmt):
    return 10 if fmt.endswith("10") else 8


def _bus_frame_bytes(fmt):
    if _is_v210(fmt):
        return bobimxl.v210_frame_bytes(W, H)
    return bobimxl.frame_bytes(W, H, "422", _depth(fmt))


def _band_bytes(fmt):
    return _bus_frame_bytes(fmt) // NB


def _total_slices(fmt):
    # v210 stock : 1 slice par LIGNE ; planar patché : N tranches (slice_height).
    return H if _is_v210(fmt) else NB


def _valid_after_band(fmt, i):
    return (i + 1) * (BAND_LINES if _is_v210(fmt) else 1)


def _make_writer(inst, name, fmt, index_mode="free"):
    if _is_v210(fmt):
        return bobimxl.Writer(inst, name, W, H, "422", 10, FPS, 1,
                              index_mode=index_mode, media_type="video/v210")
    return bobimxl.Writer(inst, name, W, H, "422", _depth(fmt), FPS, 1,
                          index_mode=index_mode, slice_height=BAND_LINES)


def _open_reader(inst, name, tries=100):
    for _ in range(tries):
        try:
            return bobimxl.Reader(inst, name)
        except bobimxl.MXLError:
            time.sleep(0.1)
    raise SystemExit(f"flux {name} introuvable")


def _band_planar(fmt, seed):
    """Bande planar CONTIGUË de 36 lignes (Y+Cb+Cr) — contenu déterministe."""
    bd = _depth(fmt)
    dt = np.uint8 if bd <= 8 else np.uint16
    n = bobimxl.frame_bytes(W, BAND_LINES, "422", bd) // dt().nbytes
    rng = np.random.default_rng(seed)
    return rng.integers(0, (1 << bd), size=n, dtype=np.uint16).astype(dt)


def _pcts(samples):
    if not samples:
        return (0.0, 0.0, 0.0)
    a = np.asarray(samples, dtype=np.float64)
    return (float(np.percentile(a, 50)), float(np.percentile(a, 99)), float(a.max()))


def _emit(tag, obj):
    print("JSON %s %s" % (tag, json.dumps(obj)), flush=True)


# ---------------------------------------------------------------------------- src

def cmd_src(args):
    fmt = args.fmt
    inst = bobimxl.Instance(args.domain)
    w = _make_writer(inst, args.name, fmt)
    period = 1.0 / FPS
    bb = _band_bytes(fmt)
    # Contenu pré-généré (2 jeux alternés pour ne pas mesurer la génération) : bandes planar
    # contiguës — la source planar memcpy sa bande, la source v210 la PACKE (SIMD, zéro-copie
    # vers la vue grain) : c'est exactement le travail d'un producteur dans chaque monde.
    bands = [[_band_planar(fmt, s * 1000 + i) for i in range(NB)] for s in (0, 1)]
    bands_u8 = [[b.view(np.uint8).reshape(-1) for b in s] for s in bands]
    deadline = time.monotonic() + args.seconds
    seq = late = 0
    work_ns = []            # travail producteur par TRAME (memcpy/pack, hors attente)
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        frame_start = t0 + seq * period
        idx, gi, view = w.open_grain()
        if gi.totalSlices != _total_slices(fmt):
            raise SystemExit(f"[src] totalSlices={gi.totalSlices} != {_total_slices(fmt)} "
                             f"(fmt={fmt}) — libmxl sans le patch attendu ?")
        acc = 0
        for i in range(NB):
            avail = frame_start + (i + 1) * period / NB   # arrivée « ligne à ligne » (modèle RX)
            slack = avail - time.monotonic()
            if slack > 0:
                time.sleep(slack)
            else:
                late += 1
            b0 = i * bb
            t1 = time.perf_counter_ns()
            if _is_v210(fmt):
                bobimxl.v210_pack(bands[seq & 1][i], W, BAND_LINES, _depth(fmt),
                                  out=view[b0:b0 + bb])
            else:
                view[b0:b0 + bb] = bands_u8[seq & 1][i]
            acc += time.perf_counter_ns() - t1
            _HDR.pack_into(view, b0, seq, i, time.time_ns())
            w.commit(gi, valid_slices=_valid_after_band(fmt, i))
        work_ns.append(acc)
        seq += 1
        if time.monotonic() > frame_start + 2 * period:
            t0 = time.monotonic() - seq * period
    w.close()
    inst.close()
    p50, p99, mx = _pcts([x / 1e6 for x in work_ns])
    _emit("src:" + args.name, {"frames": seq, "late_bands": late,
                               "work_ms": {"p50": round(p50, 3), "p99": round(p99, 3),
                                           "max": round(mx, 3)}})


# ---------------------------------------------------------------------------- stage

def cmd_stage(args):
    fmt = args.fmt
    bd = _depth(fmt)
    inst = bobimxl.Instance(args.domain)
    ra = _open_reader(inst, args.name + "-a")
    rb = _open_reader(inst, args.name + "-b")
    w = _make_writer(inst, args.name + "-out", fmt)
    bb = _band_bytes(fmt)
    # Scratchs par bande (préalloués : rien d'autre que le travail dans la boucle chaude).
    dt = np.uint8 if bd <= 8 else np.uint16
    npx = bobimxl.frame_bytes(W, BAND_LINES, "422", bd) // dt().nbytes
    bufA = np.empty(npx, dtype=dt)
    bufB = np.empty(npx, dtype=dt)
    bufO = np.empty(npx, dtype=dt)
    deadline = time.monotonic() + args.seconds
    frames = stalls = 0
    work_ns = []            # travail par TRAME : (unpack×2 + blend + pack) | blend seul
    idx = ra.head_index()
    idx = 0 if idx == bobimxl.MXL_UNDEFINED_INDEX else idx + 1
    while time.monotonic() < deadline:
        oidx, ogi, oview = w.open_grain()
        acc = 0
        done = 0
        for i in range(NB):
            want = _valid_after_band(fmt, i)
            ga = ra.get_slice(idx, want, timeout_ns=200_000_000)
            gb = rb.get_slice(idx, want, timeout_ns=200_000_000) if ga is not None else None
            if ga is None or gb is None:
                stalls += 1
                head = ra.head_index()
                if head != bobimxl.MXL_UNDEFINED_INDEX and head > idx:
                    break              # la source est déjà plus loin : trame abandonnée
                if time.monotonic() >= deadline:
                    break
                continue
            _, _, va = ga
            _, _, vb = gb
            b0 = i * bb
            hdr = bytes(va[b0:b0 + _HDR.size])      # ts source recopié verbatim
            t1 = time.perf_counter_ns()
            if _is_v210(fmt):
                bobimxl.v210_unpack(va[b0:b0 + bb], W, BAND_LINES, bd, out=bufA)
                bobimxl.v210_unpack(vb[b0:b0 + bb], W, BAND_LINES, bd, out=bufB)
                np.add(bufA >> 1, bufB >> 1, out=bufO)          # travail utile (identique)
                bobimxl.v210_pack(bufO, W, BAND_LINES, bd, out=oview[b0:b0 + bb])
            else:
                a = va[b0:b0 + bb].view(dt)
                b = vb[b0:b0 + bb].view(dt)
                np.add(a >> 1, b >> 1, out=oview[b0:b0 + bb].view(dt))
            acc += time.perf_counter_ns() - t1
            oview[b0:b0 + _HDR.size] = np.frombuffer(hdr, dtype=np.uint8)
            w.commit(ogi, valid_slices=_valid_after_band(fmt, i))
            done += 1
        if done == NB:
            frames += 1
            work_ns.append(acc)
        else:
            w.commit(ogi)   # grain incomplet : marqué complet pour ne pas bloquer le ring
            head = ra.head_index()
            if head != bobimxl.MXL_UNDEFINED_INDEX and head > idx:
                idx = head
                continue
        idx += 1
    ra.close(); rb.close(); w.close(); inst.close()
    p50, p99, mx = _pcts([x / 1e6 for x in work_ns])
    _emit("stage", {"frames": frames, "stalls": stalls,
                    "work_ms": {"p50": round(p50, 3), "p99": round(p99, 3),
                                "max": round(mx, 3)}})


# ---------------------------------------------------------------------------- sink

def cmd_sink(args):
    fmt = args.fmt
    bd = _depth(fmt)
    inst = bobimxl.Instance(args.domain)
    r = _open_reader(inst, args.name + "-out")
    bb = _band_bytes(fmt)
    dt = np.uint8 if bd <= 8 else np.uint16
    npx = bobimxl.frame_bytes(W, BAND_LINES, "422", bd) // dt().nbytes
    scratch = np.empty(npx, dtype=dt)
    deadline = time.monotonic() + args.seconds
    lat, lat_last, unpack_ns = [], [], []
    stalls = frames = 0
    idx = r.head_index()
    idx = 0 if idx == bobimxl.MXL_UNDEFINED_INDEX else idx + 1
    while time.monotonic() < deadline:
        seen = 0
        gave_up = False
        while seen < NB:
            got = r.get_slice(idx, _valid_after_band(fmt, seen), timeout_ns=200_000_000)
            t_obs = time.time_ns()
            if got is None:
                stalls += 1
                head = r.head_index()
                if head != bobimxl.MXL_UNDEFINED_INDEX and head > idx:
                    gave_up = True
                    break
                if time.monotonic() >= deadline:
                    gave_up = True
                    break
                continue
            _, gi, view = got
            nvalid = int(gi.validSlices) // (BAND_LINES if _is_v210(fmt) else 1)
            for j in range(seen, min(nvalid, NB)):
                b0 = j * bb
                seq, band, wts = _HDR.unpack_from(view, b0)
                if band != j:
                    continue                        # grain recyclé — mesure écartée
                lat.append((t_obs - wts) / 1e6)
                if j == NB - 1:
                    lat_last.append((t_obs - wts) / 1e6)
                if _is_v210(fmt):                   # coût consommateur : dé-packing SIMD
                    t1 = time.perf_counter_ns()
                    bobimxl.v210_unpack(view[b0:b0 + bb], W, BAND_LINES, bd, out=scratch)
                    unpack_ns.append(time.perf_counter_ns() - t1)
            seen = min(nvalid, NB)
        if seen == NB:
            frames += 1
        idx += 1
        if gave_up and time.monotonic() >= deadline:
            break
    r.close(); inst.close()
    p50, p99, mx = _pcts(lat)
    l50, l99, lmx = _pcts(lat_last)
    u50, u99, umx = _pcts([x / 1e6 for x in unpack_ns])
    _emit("sink", {"frames": frames, "bands": len(lat), "stalls": stalls,
                   "lat_ms": {"p50": round(p50, 3), "p99": round(p99, 3), "max": round(mx, 3)},
                   "lat_last_ms": {"p50": round(l50, 3), "p99": round(l99, 3),
                                   "max": round(lmx, 3)},
                   "consumer_unpack_ms_per_band": {"p50": round(u50, 4), "p99": round(u99, 4)},
                   "consumer_unpack_ms_per_frame": round(u50 * NB, 3) if unpack_ns else 0.0})


# ---------------------------------------------------------------------------- run / all

def _spawn(role, args, extra):
    cmd = [sys.executable, os.path.abspath(__file__), role,
           "--domain", args.domain, "--fmt", args.fmt,
           "--seconds", str(args.seconds)] + extra
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def cmd_run(args):
    base = "v210bench-%s" % args.fmt
    procs = [
        _spawn("src", args, ["--name", base + "-a"]),
        _spawn("src", args, ["--name", base + "-b"]),
        _spawn("stage", args, ["--name", base]),
        _spawn("sink", args, ["--name", base]),
    ]
    out = {}
    logs = []
    for p in procs:
        txt = p.communicate(timeout=args.seconds + 60)[0]
        logs.append(txt)
        for line in txt.splitlines():
            if line.startswith("JSON "):
                _, tag, payload = line.split(" ", 2)
                out[tag] = json.loads(payload)
    res = {"fmt": args.fmt, "bands": NB, "band_lines": BAND_LINES,
           "bus_bytes_per_frame": _bus_frame_bytes(args.fmt),
           "simd": bobimxl._v210_load() is not None, **out}
    if args.verbose:
        print("\n".join(logs))
    print("RESULT " + json.dumps(res, ensure_ascii=False), flush=True)
    return res


def cmd_all(args):
    res = []
    for fmt in FMTS:
        a = argparse.Namespace(**{**vars(args), "fmt": fmt})
        res.append(cmd_run(a))
        time.sleep(1.0)
    print("ALL " + json.dumps(res, ensure_ascii=False), flush=True)


def cmd_gc(args):
    shutil.rmtree(args.domain, ignore_errors=True)
    print("gc ok:", args.domain)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="role", required=True)
    for role, fn in (("src", cmd_src), ("stage", cmd_stage), ("sink", cmd_sink),
                     ("run", cmd_run), ("all", cmd_all), ("gc", cmd_gc)):
        p = sub.add_parser(role)
        p.add_argument("--domain", default=os.environ.get("MXL_DOMAIN", DOMAIN_DEFAULT))
        p.add_argument("--fmt", choices=FMTS, default="planar8")
        p.add_argument("--name", default="v210bench")
        p.add_argument("--seconds", type=float, default=20.0)
        p.add_argument("--verbose", action="store_true")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
