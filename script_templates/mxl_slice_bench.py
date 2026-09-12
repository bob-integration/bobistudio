#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS

"""
mxl_slice_bench — harnais de BANC (jetable) pour la Phase 3 du chantier DPDK/latence
(« mode tranche » : latence SOUS-TRAME via le bus MXL, sans aucune NIC 2110).

Modèle : un writer 1080p50 planar écrit sa trame par BANDES (N tranches égales, défaut 8)
avec COMMIT PROGRESSIF (`validSlices = i+1` à chaque bande, libmxl ≥ v1.1.0 + patch
mxl-planar-slices pour déclarer N slices sur video/x-mxl-planar) ; un reader consomme
chaque bande DÈS qu'elle est visible (futex `mxlFlowReaderGetGrainSlice`, réveillé à
chaque commit partiel). Chaque bande porte en tête un horodatage posé juste avant le
commit → latence « commit bande → observation » mesurée bout-en-bout inter-conteneurs.

Layout applicatif : le payload planar est découpé en N tranches d'OCTETS contigus ;
la bande i occupe [i*S, (i+1)*S). (En prod, la bande i contiendra Y_i+Cb_i+Cr_i contigus.)

Mesures : p50/p99/max « commit→observe » par bande, « 1ʳᵉ bande → dernière bande » par
trame, stalls. Comparer N=8 (tranches) vs N=1 (référence trame entière).

Usage (2 conteneurs, MÊME --domain et --name, domaine ISOLÉ hors prod) :
  python3 mxl_slice_bench.py writer --slices 8 --seconds 30
  python3 mxl_slice_bench.py reader --slices 8 --seconds 30
  python3 mxl_slice_bench.py unpack --slices 8 --seconds 10   # dé-packing 2110-20 (sans MXL)
  python3 mxl_slice_bench.py gc
"""

import argparse
import json
import os
import struct
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bobimxl  # noqa: E402

# En-tête posé en tête de CHAQUE bande : uint64 seq | uint32 bande | uint64 ts_ns (posé
# juste avant le commit de la bande — CLOCK_REALTIME, writer et reader sur le MÊME hôte).
_HDR = struct.Struct("<QIQ")


def _pcts(samples):
    if not samples:
        return (0.0, 0.0, 0.0)
    a = np.asarray(samples, dtype=np.float64)
    return (float(np.percentile(a, 50)), float(np.percentile(a, 99)), float(a.max()))


def _common_args(p):
    p.add_argument("--domain", default=os.environ.get("MXL_DOMAIN", "/dev/shm/mxl-bench-slice"))
    p.add_argument("--name", default="slice-video")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--fps", type=int, default=50)
    p.add_argument("--slices", type=int, default=8, help="N bandes par trame (1 = référence)")
    p.add_argument("--seconds", type=float, default=30.0)


# ------------------------------------------------------------------------------- writer

def cmd_writer(args):
    n = int(args.slices)
    inst = bobimxl.Instance(args.domain)
    w = bobimxl.Writer(inst, args.name, args.width, args.height, "422", 10,
                       args.fps, 1, index_mode="free",
                       slice_height=(args.height // n) if n > 1 else 0)
    period = 1.0 / args.fps
    print(f"[writer] flow={args.name} size={w.frame_size}o N={n} "
          f"(bande={w.frame_size // n}o) fps={args.fps}", flush=True)
    deadline = time.monotonic() + args.seconds
    seq = 0
    late = 0
    band_bytes = None
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        frame_start = t0 + seq * period
        idx, gi, view = w.open_grain()
        if gi.totalSlices != n:
            raise SystemExit(f"[writer] totalSlices={gi.totalSlices} != N={n} — patch "
                             f"mxl-planar-slices absent de la libmxl de cette image ?")
        if band_bytes is None:
            band_bytes = view.size // n
        for i in range(n):
            # La bande i « arrive » à (i+1)/N de la période (modèle RX 2110 ligne à ligne).
            avail = frame_start + (i + 1) * period / n
            slack = avail - time.monotonic()
            if slack > 0:
                time.sleep(slack)
            else:
                late += 1
            b0 = i * band_bytes
            # Remplissage de TOUTE la bande (simule le coût mémoire du dé-packing en place).
            view[b0:b0 + band_bytes] = (seq + i) & 0xFF
            _HDR.pack_into(view, b0, seq, i, time.time_ns())  # ts JUSTE avant le commit
            w.commit(gi, valid_slices=i + 1)
        seq += 1
        if time.monotonic() > frame_start + 2 * period:
            t0 = time.monotonic() - seq * period  # gros retard : re-cale la grille
    w.close()
    inst.close()
    print(f"[writer] terminé : {seq} trames (~{seq / args.seconds:.1f} fps), "
          f"bandes en retard={late}", flush=True)


# ------------------------------------------------------------------------------- reader

def cmd_reader(args):
    n = int(args.slices)
    if not bobimxl.HAS_SLICES:
        raise SystemExit("[reader] libmxl sans mxlFlowReaderGetGrainSlice (HAS_SLICES=False)")
    inst = bobimxl.Instance(args.domain)
    # Le flux peut ne pas encore exister (writer pas démarré) → retry ~10 s.
    r = None
    for _ in range(100):
        try:
            r = bobimxl.Reader(inst, args.name)
            break
        except bobimxl.MXLError:
            time.sleep(0.1)
    if r is None:
        raise SystemExit(f"[reader] flux {args.name} introuvable dans {args.domain}")
    deadline = time.monotonic() + args.seconds
    lat = []          # ms commit bande → observation (toutes bandes)
    lat_last = []     # ms pour la DERNIÈRE bande seulement (≈ latence pleine trame)
    spans = []        # ms 1ʳᵉ bande observée → dernière bande observée (par trame)
    stalls = 0        # timeouts futex (bande pas arrivée à temps)
    frames = 0
    band_bytes = None
    # Se caler sur head+1 (premier grain FRAIS — head peut être déjà entamé/complet).
    idx = r.head_index()
    idx = 0 if idx == bobimxl.MXL_UNDEFINED_INDEX else idx + 1
    cpu0 = time.process_time()
    print(f"[reader] N={n} à partir de idx={idx}", flush=True)
    while time.monotonic() < deadline:
        seen = 0
        t_first = None
        gave_up = False
        while seen < n:
            got = r.get_slice(idx, seen + 1, timeout_ns=200_000_000)
            t_obs = time.time_ns()
            if got is None:
                stalls += 1
                head = r.head_index()
                if head != bobimxl.MXL_UNDEFINED_INDEX and head > idx:
                    gave_up = True   # le writer est déjà plus loin : grain abandonné
                    break
                if time.monotonic() >= deadline:
                    gave_up = True
                    break
                continue
            _i, gi, view = got
            if band_bytes is None:
                band_bytes = view.size // n
            for j in range(seen, min(int(gi.validSlices), n)):
                seq, band, wts = _HDR.unpack_from(view, j * band_bytes)
                if band != j:
                    continue  # grain recyclé/écrasé — on ne compte pas de fausse mesure
                lat.append((t_obs - wts) / 1e6)
                if j == n - 1:
                    lat_last.append((t_obs - wts) / 1e6)
                if t_first is None:
                    t_first = t_obs
            seen = min(int(gi.validSlices), n)
        if seen == n and t_first is not None:
            spans.append((t_obs - t_first) / 1e6)
            frames += 1
        idx += 1
        if gave_up and time.monotonic() >= deadline:
            break
    cpu = time.process_time() - cpu0
    r.close()
    inst.close()
    p50, p99, mx = _pcts(lat)
    s50, s99, smx = _pcts(spans)
    l50, l99, lmx = _pcts(lat_last)
    budget_ms = 1000.0 / args.fps / n
    print(f"[reader] trames complètes={frames} (~{frames / args.seconds:.1f} fps) "
          f"bandes mesurées={len(lat)} stalls={stalls} cpu={cpu:.2f}s", flush=True)
    print(f"[reader] COMMIT→OBSERVE par bande ms : p50={p50:.3f} p99={p99:.3f} max={mx:.3f}",
          flush=True)
    print(f"[reader] dernière bande seule    ms : p50={l50:.3f} p99={l99:.3f} max={lmx:.3f}",
          flush=True)
    print(f"[reader] 1ʳᵉ→dernière bande      ms : p50={s50:.3f} p99={s99:.3f} max={smx:.3f}",
          flush=True)
    print(f"[reader] >>> CRITÈRE : p99 commit→observe < {budget_ms:.2f} ms (période/N) : "
          f"{'OK' if p99 < budget_ms else 'ÉCHEC'} ; stalls={stalls}", flush=True)
    print("[reader] JSON " + json.dumps({
        "n": n, "frames": frames, "bands": len(lat), "stalls": stalls, "cpu_s": round(cpu, 2),
        "lat_ms": {"p50": round(p50, 4), "p99": round(p99, 4), "max": round(mx, 4)},
        "span_ms": {"p50": round(s50, 4), "p99": round(s99, 4), "max": round(smx, 4)},
    }), flush=True)


# --------------------------------------------------------------- dé-packing 2110-20 par bande
# Générateur synthétique de lignes ST 2110-20 (pgroups 4:2:2 10-bit big-endian, 5 octets pour
# 2 pixels : Cb Y0 Cr Y1 sur 40 bits) + unpack numpy BE→planar 10-bit (uint16) par bande.
# AUCUN MXL requis : mesure pure CPU/mémoire sur 1 cœur (pinner le conteneur : --cpuset-cpus).

def _unpack_band(src_band, y_out, cb_out, cr_out):
    """src_band : uint8 (n_pgroups*5,) — unpack vers les vues planar (déjà dimensionnées).
    5 octets → 4 échantillons 10 bits : s0=Cb s1=Y0 s2=Cr s3=Y1."""
    b = src_band.reshape(-1, 5).astype(np.uint16)
    cb_out[:] = (b[:, 0] << 2) | (b[:, 1] >> 6)
    y = y_out.reshape(-1, 2)
    y[:, 0] = ((b[:, 1] & 0x3F) << 4) | (b[:, 2] >> 4)
    cr_out[:] = ((b[:, 2] & 0x0F) << 6) | (b[:, 3] >> 2)
    y[:, 1] = ((b[:, 3] & 0x03) << 8) | b[:, 4]


def cmd_unpack(args):
    w, h, n = int(args.width), int(args.height), int(args.slices)
    line_bytes = (w // 2) * 5                     # 4800 o pour 1920 (pgroup 4:2:2-10)
    frame_in = h * line_bytes                     # 5 184 000 o (payload RTP d'une trame)
    band_lines = h // n
    rng = np.random.default_rng(7)
    src = rng.integers(0, 256, size=frame_in, dtype=np.uint8)
    y = np.empty(w * h, dtype=np.uint16)
    cb = np.empty((w // 2) * h, dtype=np.uint16)
    cr = np.empty((w // 2) * h, dtype=np.uint16)
    need_mbps = frame_in * args.fps / 1e6         # besoin 1080p50 : ~259 Mo/s d'entrée
    # warmup (alloc/JIT numpy)
    _unpack_band(src[:band_lines * line_bytes], y[:band_lines * w],
                 cb[:band_lines * (w // 2)], cr[:band_lines * (w // 2)])
    deadline = time.monotonic() + args.seconds
    frames = 0
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        for i in range(n):
            l0 = i * band_lines
            _unpack_band(src[l0 * line_bytes:(l0 + band_lines) * line_bytes],
                         y[l0 * w:(l0 + band_lines) * w],
                         cb[l0 * (w // 2):(l0 + band_lines) * (w // 2)],
                         cr[l0 * (w // 2):(l0 + band_lines) * (w // 2)])
        frames += 1
    dt = time.monotonic() - t0
    mbps = frames * frame_in / dt / 1e6
    print(f"[unpack] N={n} : {frames} trames en {dt:.2f}s → {frames / dt:.1f} fps, "
          f"entrée {mbps:.0f} Mo/s ({mbps * 8 / 1000:.2f} Gb/s)", flush=True)
    print(f"[unpack] besoin 1080p{args.fps} = {need_mbps:.0f} Mo/s → marge ×{mbps / need_mbps:.2f} "
          f"(critère ≥ 1,5 : {'OK' if mbps >= 1.5 * need_mbps else 'ÉCHEC'})", flush=True)
    print("[unpack] JSON " + json.dumps({
        "n": n, "fps": round(frames / dt, 1), "in_mBps": round(mbps, 1),
        "need_mBps": round(need_mbps, 1), "margin": round(mbps / need_mbps, 2)}), flush=True)


def cmd_gc(args):
    inst = bobimxl.Instance(args.domain)
    before = sorted(os.listdir(args.domain)) if os.path.isdir(args.domain) else []
    inst.garbage_collect()
    after = sorted(os.listdir(args.domain)) if os.path.isdir(args.domain) else []
    inst.close()
    print(f"[gc] domaine={args.domain} avant={before} après={after}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Banc MXL mode tranche — Phase 3 DPDK/latence")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("writer", cmd_writer), ("reader", cmd_reader), ("unpack", cmd_unpack)):
        p = sub.add_parser(name)
        _common_args(p)
        p.set_defaults(func=fn)
    pg = sub.add_parser("gc")
    pg.add_argument("--domain", default=os.environ.get("MXL_DOMAIN", "/dev/shm/mxl-bench-slice"))
    pg.set_defaults(func=cmd_gc)
    args = ap.parse_args()
    if args.cmd != "unpack":
        print(f"[bench] libmxl={bobimxl.lib_version()} HAS_SLICES={bobimxl.HAS_SLICES}",
              flush=True)
    args.func(args)


if __name__ == "__main__":
    main()
