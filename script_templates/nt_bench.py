# SPDX-License-Identifier: GPL-3.0-or-later
"""Micro-banc MAGASINS NON TEMPORELS (movnt) contre écritures normales, sur le motif RÉEL
d'écriture d'un grain MXL.

## Ce qu'on cherche à trancher

Tous nos producteurs écrivent une trame pleine dans le ring MXL par `vw[:n] = <octets>` — une
copie linéaire de 4 Mo vers de la tmpfs que l'écrivain NE RELIT JAMAIS. Avec des magasins
normaux, le CPU LIT d'abord chaque ligne de cache qu'il va pourtant écraser entièrement
(read-for-ownership) : écrire 200 Mo/s génère ~400 Mo/s de trafic. Les magasins en flux
(`movntdq`/`vmovntdq`) suppriment cette lecture et ne polluent pas le L3.

**Indice matériel** : glibc bascule TOUT SEUL en non temporel au-delà de
`glibc.cpu.x86_non_temporal_threshold` — 6,875 Mo sur dell-1 (Xeon 6240R, L3 110 Mio). Une trame
1080p 4:2:2 **8 bits** fait 3,96 Mo → **sous le seuil**, donc écritures normales, donc RFO. La
même trame en **10 bits** fait 7,91 Mo → au-dessus, donc déjà non temporelle. Le banc doit voir
cette marche : si elle n'apparaît pas, l'hypothèse entière tombe.

## Pourquoi la concurrence est la moitié de la mesure

Le gain d'un magasin non temporel est du TRAFIC MÉMOIRE, pas des cycles CPU. Sur une machine au
repos, un seul copieur n'est borné ni par le bus ni par le L3 : movnt peut y paraître neutre,
voire plus lent (il court-circuite un cache qui aurait servi). Le régime qui nous intéresse est
celui de la prod — N producteurs qui se disputent le bus (cf. multiview memory-bandwidth bound).
D'où le balayage 1/2/4/8/16 processus : on regarde le débit AGRÉGÉ et le coût CPU PAR TRAME, pas
le débit d'un copieur isolé.

## Protocole (les pièges qu'il évite)

- **Destination en tmpfs, en ANNEAU** : un vrai writer tourne sur N grains. Écrire toujours le
  même buffer le laisserait résident en L1/L2 et rendrait la copie normale artificiellement
  rapide. Anneau de 10 grains, comme le ring MXL.
- **Source relue**, destination jamais relue — le motif exact du producteur.
- **Temps mural ET temps CPU** (`CLOCK_PROCESS_CPUTIME_ID`) : c'est le coût CPU qu'on facture au
  cœur alloué, pas le débit.
- **Pinning** (`taskset`) et **même socket** : un cpuset à cheval sur 2 nœuds NUMA fait varier la
  latence mémoire d'un facteur 2 (cf. numa-blind-core-pool-halves-gpu-walls) et noierait l'effet.
- **Médiane de R passes**, jamais un point unique.

Usage (sur le NŒUD, pas sur l'orchestrateur) :
    python3 nt_bench.py                      # balayage complet
    python3 nt_bench.py --sizes 4147200      # une seule taille de trame
    python3 nt_bench.py --procs 1,8          # concurrence choisie
"""
import argparse
import ctypes
import json
import mmap
import os
import statistics
import subprocess
import sys
import tempfile
import time

# --------------------------------------------------------------------------- noyaux C

C_SRC = r"""
#include <immintrin.h>
#include <stdint.h>
#include <string.h>

/* Copie de référence : memcpy glibc (= ce que fait numpy sur une assignation contiguë). */
void bobi_copy_plain(void *d, const void *s, size_t n) { memcpy(d, s, n); }

/* Copie NON TEMPORELLE SSE2 (movntdq) — disponible sur TOUT x86-64, y compris les R620
   Sandy Bridge qui n'ont pas AVX2. Tête non alignée traitée en memcpy, queue idem, sfence final
   (les magasins en flux sont faiblement ordonnés : sans barrière, le commit du grain pourrait
   devenir visible avant les octets). */
void bobi_copy_nt16(void *d, const void *s, size_t n)
{
    uint8_t *dp = (uint8_t *)d;
    const uint8_t *sp = (const uint8_t *)s;
    size_t head = (16 - ((uintptr_t)dp & 15)) & 15;
    if (head > n) head = n;
    if (head) { memcpy(dp, sp, head); dp += head; sp += head; n -= head; }
    size_t nb = n & ~(size_t)63;
    for (size_t i = 0; i < nb; i += 64) {
        __m128i a = _mm_loadu_si128((const __m128i *)(sp + i));
        __m128i b = _mm_loadu_si128((const __m128i *)(sp + i + 16));
        __m128i c = _mm_loadu_si128((const __m128i *)(sp + i + 32));
        __m128i e = _mm_loadu_si128((const __m128i *)(sp + i + 48));
        _mm_stream_si128((__m128i *)(dp + i), a);
        _mm_stream_si128((__m128i *)(dp + i + 16), b);
        _mm_stream_si128((__m128i *)(dp + i + 32), c);
        _mm_stream_si128((__m128i *)(dp + i + 48), e);
    }
    if (n > nb) memcpy(dp + nb, sp + nb, n - nb);
    _mm_sfence();
}

/* Même chose en AVX2 (vmovntdq, 32 o par magasin, 128 o par tour). */
__attribute__((target("avx2")))
static void copy_nt32_impl(void *d, const void *s, size_t n)
{
    uint8_t *dp = (uint8_t *)d;
    const uint8_t *sp = (const uint8_t *)s;
    size_t head = (32 - ((uintptr_t)dp & 31)) & 31;
    if (head > n) head = n;
    if (head) { memcpy(dp, sp, head); dp += head; sp += head; n -= head; }
    size_t nb = n & ~(size_t)127;
    for (size_t i = 0; i < nb; i += 128) {
        __m256i a = _mm256_loadu_si256((const __m256i *)(sp + i));
        __m256i b = _mm256_loadu_si256((const __m256i *)(sp + i + 32));
        __m256i c = _mm256_loadu_si256((const __m256i *)(sp + i + 64));
        __m256i e = _mm256_loadu_si256((const __m256i *)(sp + i + 96));
        _mm256_stream_si256((__m256i *)(dp + i), a);
        _mm256_stream_si256((__m256i *)(dp + i + 32), b);
        _mm256_stream_si256((__m256i *)(dp + i + 64), c);
        _mm256_stream_si256((__m256i *)(dp + i + 96), e);
    }
    if (n > nb) memcpy(dp + nb, sp + nb, n - nb);
    _mm_sfence();
}

/* Aiguillage à l'exécution : sans AVX2 on retombe sur la variante SSE2 (pas d'illegal
   instruction sur les R620 — cf. libmxl-requires-avx2). */
void bobi_copy_nt32(void *d, const void *s, size_t n)
{
    if (__builtin_cpu_supports("avx2")) copy_nt32_impl(d, s, n);
    else bobi_copy_nt16(d, s, n);
}

int bobi_has_avx2(void) { return __builtin_cpu_supports("avx2") ? 1 : 0; }

/* Boucle de banc EN C : R itérations sur un anneau de `grains` grains. Garder la boucle en C
   évite de mesurer l'interpréteur Python (l'appel ctypes coûte ~1 µs, soit 5 % d'une copie de
   4 Mo — assez pour brouiller un écart de 10 %). Renvoie les ns CPU consommés. */
typedef void (*copyfn)(void *, const void *, size_t);

uint64_t bobi_bench(int kind, void *ring, const void *src, size_t n, int grains, int iters)
{
    copyfn f = (kind == 0) ? bobi_copy_plain : (kind == 1) ? bobi_copy_nt16 : bobi_copy_nt32;
    struct timespec t0, t1;
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t0);
    for (int i = 0; i < iters; i++)
        f((uint8_t *)ring + (size_t)(i % grains) * n, src, n);
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t1);
    return (uint64_t)(t1.tv_sec - t0.tv_sec) * 1000000000ull + (t1.tv_nsec - t0.tv_nsec);
}
"""

KINDS = {0: "memcpy", 1: "movnt-sse2", 2: "movnt-avx2"}

# Tailles de trame RÉELLES du parc (planar contigu Y+Cb+Cr, cf. bobimxl.frame_bytes).
SIZES = {
    "720p50 422 8b":   1280 * 720 * 2,
    "1080p50 422 8b":  1920 * 1080 * 2,
    "1080p50 422 10b": 1920 * 1080 * 2 * 2,
    "2160p50 422 8b":  3840 * 2160 * 2,
}

RING_GRAINS = 10          # comme le ring MXL


def build(tmpdir):
    """Compile les noyaux. -O2 sans -march : le code SIMD est explicite, on ne veut PAS que gcc
    auto-vectorise memcpy autrement que ne le fait la glibc de production."""
    src = os.path.join(tmpdir, "ntcopy.c")
    so = os.path.join(tmpdir, "libntcopy.so")
    with open(src, "w") as f:
        f.write("#include <time.h>\n" + C_SRC)
    p = subprocess.run(["gcc", "-O2", "-fPIC", "-shared", "-o", so, src],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit("gcc: " + p.stderr)
    lib = ctypes.CDLL(so)
    for fn in ("bobi_copy_plain", "bobi_copy_nt16", "bobi_copy_nt32"):
        getattr(lib, fn).argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        getattr(lib, fn).restype = None
    lib.bobi_bench.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_size_t, ctypes.c_int, ctypes.c_int]
    lib.bobi_bench.restype = ctypes.c_uint64
    lib.bobi_has_avx2.restype = ctypes.c_int
    return lib


def make_ring(n, grains, shm_dir):
    """Anneau de `grains` grains en TMPFS (comme /dev/shm/mxl) — pas un buffer anonyme : le
    support mémoire d'un fichier tmpfs n'a pas les mêmes propriétés de faute de page ni de
    politique NUMA qu'un tas privé."""
    fd, path = tempfile.mkstemp(dir=shm_dir, prefix="ntbench-")
    os.unlink(path)                       # anonyme mais toujours en tmpfs
    total = n * grains
    os.ftruncate(fd, total)
    mm = mmap.mmap(fd, total, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    os.close(fd)
    mm.write(b"\0" * total)               # pré-faute : ne pas mesurer l'allocation
    mm.seek(0)
    return mm


def addr(buf):
    return ctypes.addressof(ctypes.c_char.from_buffer(buf))


def run_one(lib, kind, n, iters, shm_dir):
    """Une passe : renvoie (Go/s mural, µs CPU par trame)."""
    ring = make_ring(n, RING_GRAINS, shm_dir)
    src = mmap.mmap(-1, n)
    src.write(bytes(range(256)) * (n // 256) + b"\0" * (n % 256))
    a_ring, a_src = addr(ring), addr(src)
    lib.bobi_bench(kind, a_ring, a_src, n, RING_GRAINS, 5)   # chauffe
    t0 = time.perf_counter()
    cpu_ns = lib.bobi_bench(kind, a_ring, a_src, n, RING_GRAINS, iters)
    wall = time.perf_counter() - t0
    ring.close(); src.close()
    return (iters * n / wall / 1e9, cpu_ns / iters / 1000.0)


def child(lib, kind, n, iters, shm_dir, out_fd):
    # La CDLL du parent est héritée par le fork — ne PAS recompiler par enfant.
    gbps, cpu_us = run_one(lib, kind, n, iters, shm_dir)
    os.write(out_fd, json.dumps({"gbps": gbps, "cpu_us": cpu_us}).encode())
    os._exit(0)


def sweep(lib, kinds, n, nproc, iters, shm_dir, repeats):
    """Lance `nproc` copieurs SIMULTANÉS et agrège. Chaque enfant est épinglé sur un cœur du
    MÊME nœud NUMA (sinon la latence mémoire varie d'un facteur 2 entre enfants et la moyenne
    ne veut rien dire)."""
    res = {}
    for kind in kinds:
        runs = []
        for _ in range(repeats):
            pipes, pids = [], []
            for i in range(nproc):
                r, w = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(r)
                    try:
                        os.sched_setaffinity(0, {CORES[i % len(CORES)]})
                    except Exception:
                        pass
                    child(lib, kind, n, iters, shm_dir, w)
                os.close(w); pipes.append(r); pids.append(pid)
            vals = []
            for r in pipes:
                data = b""
                while True:
                    c = os.read(r, 4096)
                    if not c:
                        break
                    data += c
                os.close(r)
                vals.append(json.loads(data))
            for p in pids:
                os.waitpid(p, 0)
            runs.append({
                "gbps_total": sum(v["gbps"] for v in vals),
                "cpu_us": statistics.median(v["cpu_us"] for v in vals),
            })
        res[KINDS[kind]] = {
            "gbps_total": round(statistics.median(r["gbps_total"] for r in runs), 2),
            "cpu_us": round(statistics.median(r["cpu_us"] for r in runs), 1),
        }
    return res


def numa_cores():
    """Cœurs du nœud NUMA 0, sans HyperThread (un HT partage le même port de stockage : deux
    copieurs sur un même cœur physique mesureraient la contention du cœur, pas celle du bus)."""
    cores, seen = [], set()
    base = "/sys/devices/system/cpu"
    try:
        node0 = open("/sys/devices/system/node/node0/cpulist").read().strip()
    except Exception:
        node0 = ""
    allowed = set()
    for part in node0.split(","):
        if "-" in part:
            a, b = part.split("-"); allowed.update(range(int(a), int(b) + 1))
        elif part:
            allowed.add(int(part))
    for c in sorted(allowed):
        try:
            sibs = open(f"{base}/cpu{c}/topology/thread_siblings_list").read().strip()
        except Exception:
            sibs = str(c)
        if sibs in seen:
            continue
        seen.add(sibs); cores.append(c)
    return cores or list(range(os.cpu_count()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="", help="tailles en octets, séparées par des virgules")
    ap.add_argument("--procs", default="1,2,4,8,16")
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--shm", default="/dev/shm")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    global BUILD_DIR, CORES
    BUILD_DIR = tempfile.mkdtemp(prefix="ntbench-")
    CORES = numa_cores()
    lib = build(BUILD_DIR)
    kinds = [0, 1, 2] if lib.bobi_has_avx2() else [0, 1]

    sizes = ({f"{int(s)} o": int(s) for s in a.sizes.split(",")} if a.sizes else SIZES)
    procs = [int(p) for p in a.procs.split(",")]

    seuil = ""
    try:
        out = subprocess.run(["ld.so", "--list-tunables"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "x86_non_temporal_threshold" in line and "memset" not in line:
                seuil = line.split(":")[1].split("(")[0].strip()
    except Exception:
        pass

    meta = {"host": os.uname().nodename, "cores_numa0": len(CORES), "avx2": bool(lib.bobi_has_avx2()),
            "glibc_nt_threshold": seuil}
    out = {"meta": meta, "results": {}}

    if not a.json:
        print(f"# {meta['host']} — {len(CORES)} cœurs physiques NUMA0, AVX2={meta['avx2']}, "
              f"seuil NT glibc={seuil}")

    for label, n in sizes.items():
        out["results"][label] = {}
        if not a.json:
            au_dessus = "≥ seuil (glibc déjà NT)" if seuil and n >= int(seuil, 16) else "< seuil (RFO)"
            print(f"\n## {label} — {n/1e6:.2f} Mo/trame — {au_dessus}")
            print(f"{'procs':>6} | " + " | ".join(f"{KINDS[k]:>22}" for k in kinds))
        for nproc in procs:
            if nproc > len(CORES):
                continue
            r = sweep(lib, kinds, n, nproc, a.iters, a.shm, a.repeats)
            out["results"][label][nproc] = r
            if not a.json:
                cells = " | ".join(
                    f"{r[KINDS[k]]['gbps_total']:>8.2f} Go/s {r[KINDS[k]]['cpu_us']:>7.1f} µs"
                    for k in kinds)
                print(f"{nproc:>6} | {cells}")

    if a.json:
        print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
