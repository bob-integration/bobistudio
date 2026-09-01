#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS

"""
mxl_bench — harnais de BANC (jetable) pour la Phase 0 de la migration MXL.

But : valider les critères d'acceptation Phase 0 (cf. plan) sur un nœud disposant de libmxl +
d'un tmpfs MXL (banc E810, HORS ANTENNE). N'importe AUCUN plugin de prod, écrit dans un
domaine MXL ISOLÉ (défaut /dev/shm/mxl-bench) → ne touche jamais les flows de prod.

Critères couverts :
  1. PREUVE FREE-RUN (go/no-go) : writer free-run + reader get_latest() NON-BLOQUANT →
     latence publish→observe ≤ poll mmap actuel (1-2 ms), à plein débit 1080p50, 0 stall.
  2. Grille TAI : writer index_mode=tai + injection de trous (--gap) → comportement défini.
  3. Futex vs poll : reader blocking (get) vs latest (poll-like) → latence de réveil + CPU.
  4. GC inter-conteneurs : writer qui meurt → `gc` récupère le flow (anti-fuite tmpfs).
  5. Zéro-copie : payload écrit/lu via vue numpy directe (pas de memcpy parasite).

Usage (2 conteneurs / 2 terminaux, MÊME --domain et --name) :
  # producteur free-run 1080p50 pendant 30 s
  python3 mxl_bench.py writer --seconds 30
  # consommateur free-run (preuve N°1)
  python3 mxl_bench.py latest --seconds 30
  # consommateur calé (futex)            | GC après mort du writer
  python3 mxl_bench.py blocking --seconds 30 ; python3 mxl_bench.py gc

Variables d'env utiles : MXL_LIB_PATH (libmxl.so), MXL_DOMAIN (override --domain).
"""

import argparse
import os
import struct
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bobimxl  # noqa: E402

# En-tête applicatif posé en tête de payload (pour mesurer la latence bout-en-bout) :
#   uint64 seq | uint64 write_ts_ns (CLOCK_REALTIME du writer)
_HDR = struct.Struct("<QQ")


def _fmt_args(p):
    p.add_argument("--domain", default=os.environ.get("MXL_DOMAIN", "/dev/shm/mxl-bench"))
    p.add_argument("--name", default="bench-video")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--chroma", default="422")
    p.add_argument("--bit-depth", type=int, default=10)  # v210 = 10-bit (seul type vidéo MXL v1.0)
    p.add_argument("--fps", type=int, default=50)
    p.add_argument("--seconds", type=float, default=30.0)


def _pcts(samples):
    if not samples:
        return (0.0, 0.0, 0.0)
    a = np.sort(np.asarray(samples, dtype=np.float64))
    return (float(np.percentile(a, 50)), float(np.percentile(a, 99)), float(a.max()))


# ------------------------------------------------------------------------------- writer

def cmd_writer(args):
    inst = bobimxl.Instance(args.domain)
    w = bobimxl.Writer(inst, args.name, args.width, args.height, args.chroma,
                       args.bit_depth, args.fps, 1, index_mode=args.index_mode)
    period = 1.0 / args.fps
    deadline = time.monotonic() + args.seconds
    seq = 0
    # Motif de remplissage léger : on n'écrit QUE l'en-tête (zéro-copie en place) + un octet
    # marqueur — pas besoin de peindre toute la frame pour mesurer la latence/cadence.
    print(f"[writer] flow={args.name} id={bobimxl.flow_id(args.name)} "
          f"size={w.frame_size}o mode={args.index_mode} created={w.created}", flush=True)
    next_t = time.monotonic()
    while time.monotonic() < deadline:
        # --gap N : saute 1 index tous les N (test trous / grains manquants, critère 2)
        if args.gap and seq and seq % args.gap == 0:
            w._counter += 1  # trou volontaire en mode free
        idx, gi, view = w.open_grain()
        _HDR.pack_into(view, 0, seq, time.time_ns())  # écriture DIRECTE dans le grain (zéro-copie)
        w.commit(gi)
        seq += 1
        next_t += period
        sleep = next_t - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_t = time.monotonic()  # on a pris du retard : re-cale, ne s'accumule pas
    w.close()
    inst.close()
    print(f"[writer] terminé : {seq} grains en {args.seconds:.0f}s "
          f"(~{seq/args.seconds:.1f} fps)", flush=True)


# ------------------------------------------------------------------------------- reader latest (free-run)

def cmd_latest(args):
    inst = bobimxl.Instance(args.domain)
    r = bobimxl.Reader(inst, args.name)
    deadline = time.monotonic() + args.seconds
    lat = []
    last_seq = -1
    seen = 0
    stalls = 0
    cpu0 = time.process_time()
    print(f"[latest] free-run get_latest() non-bloquant sur {args.name}", flush=True)
    while time.monotonic() < deadline:
        got = r.get_latest()
        if got is None:
            stalls += 1
            time.sleep(0.0005)
            continue
        _idx, _gi, view = got
        seq, wts = _HDR.unpack_from(view, 0)
        if seq != last_seq:
            lat.append((time.time_ns() - wts) / 1e6)  # ms publish→observe
            last_seq = seq
            seen += 1
        # boucle serrée (poll-like) ; pas de sleep quand on suit le flux
    cpu = time.process_time() - cpu0
    p50, p99, mx = _pcts(lat)
    r.close()
    inst.close()
    print(f"[latest] frames vues={seen} fps≈{seen/args.seconds:.1f} stalls={stalls} "
          f"cpu={cpu:.2f}s", flush=True)
    print(f"[latest] LATENCE publish→observe ms : p50={p50:.3f} p99={p99:.3f} max={mx:.3f}",
          flush=True)
    print(f"[latest] >>> CRITÈRE N°1 free-run : p50 ≤ ~2 ms attendu ({'OK' if p50 <= 2 else 'À VÉRIFIER'})",
          flush=True)


# ------------------------------------------------------------------------------- reader blocking (futex)

def cmd_blocking(args):
    inst = bobimxl.Instance(args.domain)
    r = bobimxl.Reader(inst, args.name)
    deadline = time.monotonic() + args.seconds
    lat = []
    seen = 0
    timeouts = 0
    cpu0 = time.process_time()
    # On se cale : suivre head, puis attendre head+1 via futex (get bloquant).
    idx = r.head_index()
    if idx == bobimxl.MXL_UNDEFINED_INDEX:
        idx = 0
    print(f"[blocking] futex get(index, timeout) à partir de {idx}", flush=True)
    while time.monotonic() < deadline:
        got = r.get(idx + 1, timeout_ns=100_000_000)
        if got is None:
            timeouts += 1
            idx = r.head_index()
            if idx == bobimxl.MXL_UNDEFINED_INDEX:
                idx = 0
            continue
        ridx, _gi, view = got
        seq, wts = _HDR.unpack_from(view, 0)
        lat.append((time.time_ns() - wts) / 1e6)
        seen += 1
        idx = ridx
    cpu = time.process_time() - cpu0
    p50, p99, mx = _pcts(lat)
    r.close()
    inst.close()
    print(f"[blocking] frames vues={seen} fps≈{seen/args.seconds:.1f} timeouts={timeouts} "
          f"cpu={cpu:.2f}s (doit être << cpu du latch poll)", flush=True)
    print(f"[blocking] LATENCE réveil futex ms : p50={p50:.3f} p99={p99:.3f} max={mx:.3f}",
          flush=True)


# ------------------------------------------------------------------------------- GC

def cmd_gc(args):
    inst = bobimxl.Instance(args.domain)
    before = sorted(os.listdir(args.domain)) if os.path.isdir(args.domain) else []
    inst.garbage_collect()
    after = sorted(os.listdir(args.domain)) if os.path.isdir(args.domain) else []
    inst.close()
    removed = [x for x in before if x not in after]
    print(f"[gc] domaine={args.domain}", flush=True)
    print(f"[gc] avant={before}", flush=True)
    print(f"[gc] après={after}", flush=True)
    print(f"[gc] récupérés={removed or '(aucun)'}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Harnais de banc MXL — Phase 0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pw = sub.add_parser("writer", help="producteur (free-run par défaut)")
    _fmt_args(pw)
    pw.add_argument("--index-mode", default="free", choices=["free", "tai"])
    pw.add_argument("--gap", type=int, default=0, help="saute 1 index tous les N (test trous)")
    pw.set_defaults(func=cmd_writer)

    pl = sub.add_parser("latest", help="consommateur free-run (preuve N°1)")
    _fmt_args(pl)
    pl.set_defaults(func=cmd_latest)

    pb = sub.add_parser("blocking", help="consommateur calé (futex)")
    _fmt_args(pb)
    pb.set_defaults(func=cmd_blocking)

    pg = sub.add_parser("gc", help="garbage-collect des flows morts")
    pg.add_argument("--domain", default=os.environ.get("MXL_DOMAIN", "/dev/shm/mxl-bench"))
    pg.set_defaults(func=cmd_gc)

    args = ap.parse_args()
    print(f"[bench] libmxl version = {bobimxl.lib_version()}", flush=True)
    args.func(args)


if __name__ == "__main__":
    main()
