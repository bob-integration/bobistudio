#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Générateur de flux ANC SYNTHÉTIQUE (outil de banc, NON déployé sur la flotte) — écrit un shm
# ANC au format de `plugins/2110_io/mtl_rx.c` (data_rx_thread) avec un paquet RP188/ATC dont le
# timecode s'incrémente à la cadence demandée. Permet de valider l'horloge « source ANC » du
# multiview SANS matériel MTL/E810.
#
# Le multiview dérive le shm ANC du shm vidéo câblé : '/dev/shm/<name>_<n>' → '<name>_anc_<n>'.
# Donc pour une entrée vidéo 'demo_0', lancez :  python3 tools/anc_tc_gen.py demo_0
# (crée /dev/shm/demo_anc_0). Doit tourner dans le MÊME /dev/shm que le conteneur multiview
# (sur le nœud, ou via le bind-mount mxl du conteneur compute).
#
# Usage : anc_tc_gen.py <shm_video_name> [--fps 25] [--start HH:MM:SS:FF] [--df] [--once]
import argparse, mmap, os, re, struct, time

ANC_SLOT, ANC_RING, ANC_HDR = 8192, 8, 64
ANC_TOTAL = ANC_HDR + ANC_RING * ANC_SLOT


def derive_anc(video_name):
    m = re.match(r"(.+?)_(\d+)$", video_name)
    return f"{m.group(1)}_anc_{m.group(2)}" if m else (video_name + "_anc_0")


def parse_tc(s):
    f = [int(x) for x in str(s or "0").split(":") if x != ""]
    while len(f) < 4:
        f = [0] + f
    return f[-4:]   # hh, mm, ss, ff


def rp188_udw(hh, mm, ss, ff, df):
    """8 octets BCD ATC → 16 UDW (1 octet/UDW, low nibble utile). Miroir exact de decode_atc."""
    def bcd(v, tens_mask):
        return (v % 10) | (((v // 10) & tens_mask) << 4)
    b = [bcd(ff, 0x03) | ((1 if df else 0) << 6), 0,
         bcd(ss, 0x07), 0, bcd(mm, 0x07), 0, bcd(hh, 0x03), 0]
    udw = bytearray(16)
    for i in range(8):
        udw[2 * i] = b[i] & 0x0f
        udw[2 * i + 1] = (b[i] >> 4) & 0x0f
    return bytes(udw)


def write_grain(mm_, ci, hh, mn_, ss, ff, df):
    slot = ci % ANC_RING
    base = ANC_HDR + slot * ANC_SLOT
    udw = rp188_udw(hh, mn_, ss, ff, df)
    meta = struct.pack("<8H", 0x60, 0x60, 9, 0, 16, 0, 0, 0)   # did,sdid,line,hori,udw_size,udw_offset,c,s
    payload = struct.pack("<II", 1, 16) + meta + udw
    mm_[base:base + len(payload)] = payload
    mm_[0:8] = struct.pack("<Q", ci)            # en-tête : index → pointe le dernier grain écrit
    mm_[8:16] = struct.pack("<Q", time.time_ns())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_name", help="nom du shm vidéo (ex. demo_0) ; l'ANC dérivé est demo_anc_0")
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--start", default="00:00:00:00")
    ap.add_argument("--df", action="store_true", help="drapeau drop-frame")
    ap.add_argument("--once", action="store_true", help="écrit un seul grain et sort")
    args = ap.parse_args()

    path = "/dev/shm/" + derive_anc(args.video_name)
    with open(path, "wb") as f:
        f.write(b"\x00" * ANC_TOTAL)
    f = open(path, "r+b")
    mm_ = mmap.mmap(f.fileno(), ANC_TOTAL)

    fps_i = max(1, round(args.fps))
    hh, mn_, ss, ff = parse_tc(args.start)
    ci = 0
    period = 1.0 / args.fps
    print(f"ANC synthétique → {path} (fps={args.fps}, start={hh:02d}:{mn_:02d}:{ss:02d}:{ff:02d}, df={args.df})")
    try:
        while True:
            write_grain(mm_, ci, hh, mn_, ss, ff, args.df)
            if args.once:
                print("grain écrit (--once)."); break
            ci += 1
            ff += 1
            if ff >= fps_i:
                ff = 0; ss += 1
                if ss >= 60:
                    ss = 0; mn_ += 1
                    if mn_ >= 60:
                        mn_ = 0; hh = (hh + 1) % 24
            time.sleep(period)
    except KeyboardInterrupt:
        print("\narrêt.")


if __name__ == "__main__":
    main()
