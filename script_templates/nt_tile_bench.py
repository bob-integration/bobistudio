# SPDX-License-Identifier: GPL-3.0-or-later
"""Le non temporel paie-t-il sur une écriture de TUILE (le motif des gros consommateurs) ?

`bobimxl.blit` ne concerne que les producteurs qui déversent une trame linéaire. Les plugins qui
dominent réellement le CPU — multiview, mixer, pyramide — n'écrivent JAMAIS ainsi : ils ouvrent le
grain, en prennent des vues par plan et composent EN PLACE, par tuiles (mosaïque) ou par bandes.

Or un magasin non temporel n'évite le read-for-ownership que s'il couvre une ligne de cache
ENTIÈRE (64 o). Une ligne de tuile de 480 px en 8 bits fait 480 o = 7,5 lignes, dont les deux
extrémités sont partielles et désalignées à chaque rangée. Le gain peut donc s'inverser.

Trois motifs comparés, à géométrie de mur réelle :
  - TRAME    : une écriture linéaire plein-plan (référence, ce que fait un producteur)
  - TUILES   : N×M tuiles écrites rangée par rangée dans le plan de sortie (multiview/pyramide)
  - BANDES   : bandes pleine largeur (mixer, commit progressif) — lignes contiguës longues

Usage (sur le NŒUD) : python3 nt_tile_bench.py [--grille 4x4] [--largeur 1920] [--hauteur 1080]
"""
import argparse
import ctypes
import os
import statistics
import subprocess
import sys
import tempfile
import time

C_SRC = r"""
#include <immintrin.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

__attribute__((target("avx2")))
static void nt_row(uint8_t *d, const uint8_t *s, size_t n)
{
    size_t head = (32 - ((uintptr_t)d & 31)) & 31;
    if (head > n) head = n;
    if (head) { memcpy(d, s, head); d += head; s += head; n -= head; }
    size_t nb = n & ~(size_t)127;
    for (size_t i = 0; i < nb; i += 128) {
        _mm256_stream_si256((__m256i *)(d + i), _mm256_loadu_si256((const __m256i *)(s + i)));
        _mm256_stream_si256((__m256i *)(d + i + 32), _mm256_loadu_si256((const __m256i *)(s + i + 32)));
        _mm256_stream_si256((__m256i *)(d + i + 64), _mm256_loadu_si256((const __m256i *)(s + i + 64)));
        _mm256_stream_si256((__m256i *)(d + i + 96), _mm256_loadu_si256((const __m256i *)(s + i + 96)));
    }
    if (n > nb) memcpy(d + nb, s + nb, n - nb);
}

/* UNE itération = UNE TRAME ENTIÈRE, dans tous les motifs — condition pour que les motifs soient
   comparables ET pour que l'ensemble de travail dépasse le L3.
   Deux versions précédentes de ce banc ont été fausses pour l'avoir manqué : (1) destination
   unique de 2 Mo, résidente en L3 (memcpy à 48 Go/s, impossible) ; (2) anneau correct mais une
   seule TUILE écrite par itération, donc 570 Ko touchés — memcpy deux fois plus rapide en 6x6
   qu'en 4x4 pour le même volume. Un motif qui n'écrit pas la trame complète ne mesure rien.
   tw=W,th=H → trame linéaire ; tw=W,th=H/8 → bandes ; tw,th → mosaïque. */
uint64_t bobi_frame_bench(int kind, uint8_t *ring, const uint8_t *src,
                          size_t W, size_t H, size_t tw, size_t th,
                          int iters, int grains)
{
    struct timespec t0, t1;
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t0);
    for (int it = 0; it < iters; it++) {
        uint8_t *frame = ring + (size_t)(it % grains) * W * H;
        for (size_t ty = 0; ty + th <= H; ty += th) {
            for (size_t tx = 0; tx + tw <= W; tx += tw) {
                uint8_t *dp = frame + ty * W + tx;
                const uint8_t *sp = src;
                for (size_t r = 0; r < th; r++) {
                    if (kind == 0) memcpy(dp, sp, tw);
                    else nt_row(dp, sp, tw);
                    dp += W; sp += tw;
                }
            }
        }
        if (kind) _mm_sfence();
    }
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t1);
    return (uint64_t)(t1.tv_sec - t0.tv_sec) * 1000000000ull + (t1.tv_nsec - t0.tv_nsec);
}
"""


def build(tmp):
    src = os.path.join(tmp, "tb.c"); so = os.path.join(tmp, "libtb.so")
    open(src, "w").write(C_SRC)
    p = subprocess.run(["gcc", "-O2", "-fPIC", "-shared", "-o", so, src],
                       capture_output=True, text=True)
    if p.returncode:
        sys.exit(p.stderr)
    lib = ctypes.CDLL(so)
    lib.bobi_frame_bench.restype = ctypes.c_uint64
    lib.bobi_frame_bench.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
                                     ctypes.c_size_t, ctypes.c_int, ctypes.c_int]
    return lib


def buf(n, shm=False):
    import mmap
    if shm:
        fd = os.open("/dev/shm", os.O_TMPFILE | os.O_RDWR)
        os.ftruncate(fd, n)
        m = mmap.mmap(fd, n, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        os.close(fd)
    else:
        m = mmap.mmap(-1, n)
    m.write(b"\0" * n); m.seek(0)
    return m


def addr(b):
    return ctypes.addressof(ctypes.c_char.from_buffer(b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grille", default="4x4")
    ap.add_argument("--largeur", type=int, default=1920)
    ap.add_argument("--hauteur", type=int, default=1080)
    ap.add_argument("--iters", type=int, default=100)
    ap.add_argument("--repeats", type=int, default=5)
    a = ap.parse_args()
    cols, rows_g = (int(x) for x in a.grille.lower().split("x"))
    W, H = a.largeur, a.hauteur
    tw, th = W // cols, H // rows_g

    tmp = tempfile.mkdtemp(prefix="tb-")
    lib = build(tmp)
    # Plan Y de sortie en tmpfs (comme un grain) + source contiguë généreuse.
    GRAINS = 10          # anneau, comme le ring MXL : la destination ne doit PAS tenir en L3
    dst = buf(W * H * GRAINS, shm=True)
    src = buf(W * H)
    ad, asrc = addr(dst), addr(src)

    motifs = [
        ("TRAME  (1 ecriture lineaire)", W, H),
        ("TUILES (%dx%d de %dx%d)" % (cols, rows_g, tw, th), tw, th),
        ("BANDES (pleine largeur, %d l.)" % (H // 8), W, H // 8),
    ]
    print("# %s  %dx%d  tuile %dx%d  anneau %d plans (%.0f Mo)  [1 iter = 1 trame complete]"
          % (os.uname().nodename, W, H, tw, th, GRAINS, W * H * GRAINS / 1e6))
    print("%-34s %12s %12s %8s" % ("motif", "memcpy us", "non temp. us", "ecart"))
    for nom, mw, mh in motifs:
        res = {}
        for kind in (0, 1):
            vals = []
            for _ in range(a.repeats):
                lib.bobi_frame_bench(kind, ad, asrc, W, H, mw, mh, 3, GRAINS)
                ns = lib.bobi_frame_bench(kind, ad, asrc, W, H, mw, mh, a.iters, GRAINS)
                vals.append(ns / a.iters / 1000.0)
            res[kind] = statistics.median(vals)
        print("%-34s %12.1f %12.1f %7.0f%%"
              % (nom, res[0], res[1], (res[1] - res[0]) / res[0] * 100))


if __name__ == "__main__":
    main()
