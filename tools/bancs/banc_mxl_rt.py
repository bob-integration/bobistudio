#!/usr/bin/env python3
"""Banc de plugins/audio_console/tools/mxl_rt.{h,c} contre la VRAIE libmxl (pendant de
tests/verif_console_mxl_rt.py, qui tourne contre une fausse libmxl en CI).

À LANCER À LA MAIN dans un conteneur bobi-compute (image runtime : python3, numpy, libmxl, de préférence
gcc). Jamais en CI, jamais sur un conteneur de production : le banc travaille dans UN DOMAINE PRIVÉ
(`--domain`, défaut /dev/shm/mxl_banc_rt) et ne touche à aucun flux vivant.

MODE D'EMPLOI
  1. Copier dans le conteneur (conteneur de BANC, à créer pour l'occasion) :
       docker cp <repo>/script_templates/bobimxl.py            <c>:/tmp/banc_rt/
       docker cp <repo>/plugins/audio_console/src/mxl_io.py    <c>:/tmp/banc_rt/
       docker cp <repo>/plugins/audio_console/tools/mxl_rt.c   <c>:/tmp/banc_rt/
       docker cp <repo>/plugins/audio_console/tools/mxl_rt.h   <c>:/tmp/banc_rt/
       docker cp <repo>/plugins/audio_console/tools/rt_guard.h <c>:/tmp/banc_rt/
       docker cp <repo>/tools/bancs/banc_mxl_rt.py             <c>:/tmp/banc_rt/
  2. Dans le conteneur :
       cd /tmp/banc_rt && python3 banc_mxl_rt.py
     (libmxl : MXL_LIB_PATH si elle n'est pas dans le chemin du chargeur ; sans gcc, compiler ailleurs avec
        cc -std=gnu11 -O2 -fPIC -shared -pthread -Wall -Wextra -Werror -o mxl_rt.so mxl_rt.c
      puis `--so /tmp/banc_rt/mxl_rt.so`.)
  3. Sortie « ok … / FAIL … », « ALL OK », code de sortie 1 au moindre échec.

CE QU'IL VÉRIFIE (avec la vraie bibliothèque)
  - ABI : mxlGetVersion de la vraie libmxl est acceptée ; les tailles ctypes de bobimxl égalent celles du C.
  - Lecture : un AudioWriter bobimxl écrit des motifs connus ; le C lit les mêmes index que
    AudioReader.read_from(start, n) — exact au bit, (canaux, n) ; manques (trop vieux / trop neuf) comptés.
  - Écriture : le C écrit (contigu, trou comblé, recouvrement, saut) dans un flux ; un AudioReader bobimxl
    relit et compare. Mêmes compteurs que mxl_io.OutPort.put sur la même séquence (OutPort réel, binding réel).
  - Décalage d'index (ONE-PAST-THE-END) : si le C se trompait de sens, les deux relectures croisées échoueraient.
  - Faute : phase `--phase sigbus` (sous-processus, car une faute DANS libmxl n'est pas gardée — limite connue) :
    le fichier de données du flux est tronqué sous le lecteur C ; on constate le comportement réel
    (faute gardée sur NOTRE copie, ou arrêt du processus si c'est libmxl qui touche la page) et on le rapporte.
"""
import argparse
import ctypes
import os
import random
import shutil
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--dir", default=HERE, help="dossier contenant bobimxl.py, mxl_io.py, mxl_rt.[ch], rt_guard.h")
ap.add_argument("--domain", default="/dev/shm/mxl_banc_rt")
ap.add_argument("--so", default=None, help="mxl_rt.so déjà compilé (sinon compilé ici avec cc)")
ap.add_argument("--phase", default="main", choices=["main", "sigbus"])
args = ap.parse_args()
sys.path.insert(0, args.dir)

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " FAIL ", titre))
    if not cond:
        print("         %s" % (detail,))
        ko.append(titre)


import bobimxl  # noqa: E402
import mxl_io   # noqa: E402

bobimxl._load()
LIB = bobimxl._lib
VOID = ctypes.c_void_p


def build_so():
    if args.so:
        return args.so
    cc = shutil.which("gcc") or shutil.which("cc")
    if not cc:
        print(" FAIL ni gcc ni cc : compiler mxl_rt.so ailleurs et passer --so")
        sys.exit(1)
    out = os.path.join(tempfile.mkdtemp(prefix="mxl_rt_"), "mxl_rt.so")
    r = subprocess.run([cc, "-std=gnu11", "-O2", "-fPIC", "-shared", "-pthread", "-Wall", "-Wextra", "-Werror",
                        "-I", args.dir, "-o", out, os.path.join(args.dir, "mxl_rt.c")], capture_output=True, text=True)
    if r.returncode:
        print(" FAIL compilation de mxl_rt.c\n" + r.stderr[:1500])
        sys.exit(1)
    return out


rt = ctypes.CDLL(build_so())


class Abi(ctypes.Structure):
    _fields_ = [("abi_tag", ctypes.c_uint32), ("lib_major", ctypes.c_uint16), ("lib_minor", ctypes.c_uint16),
                ("lib_bugfix", ctypes.c_uint16), ("pad", ctypes.c_uint16),
                ("sizeof_buffer_slice", ctypes.c_uint32), ("sizeof_wrapped_multi", ctypes.c_uint32),
                ("sizeof_mutable_multi", ctypes.c_uint32), ("offset_stride", ctypes.c_uint32),
                ("offset_count", ctypes.c_uint32), ("pad2", ctypes.c_uint32),
                ("get_samples_nb", VOID), ("open_samples", VOID), ("commit_samples", VOID),
                ("cancel_samples", VOID), ("get_time", VOID)]


class InStats(ctypes.Structure):
    _fields_ = [("state", ctypes.c_uint32), ("gen", ctypes.c_uint32), ("channels", ctypes.c_uint32),
                ("last_err", ctypes.c_int32), ("last_status", ctypes.c_int32), ("pad", ctypes.c_uint32),
                ("reads", ctypes.c_uint64), ("misses", ctypes.c_uint64), ("errors", ctypes.c_uint64),
                ("faults", ctypes.c_uint64), ("miss_streak", ctypes.c_uint64), ("last_ok_ns", ctypes.c_uint64)]


class OutStats(ctypes.Structure):
    _fields_ = [("state", ctypes.c_uint32), ("gen", ctypes.c_uint32), ("channels", ctypes.c_uint32),
                ("last_err", ctypes.c_int32), ("last_status", ctypes.c_int32), ("has_idx", ctypes.c_uint32),
                ("next_idx", ctypes.c_uint64), ("written", ctypes.c_uint64), ("filled", ctypes.c_uint64),
                ("dropped", ctypes.c_uint64), ("jumps", ctypes.c_uint64), ("errors", ctypes.c_uint64),
                ("faults", ctypes.c_uint64), ("last_write_ns", ctypes.c_uint64)]


rt.mxl_rt_init.argtypes = [ctypes.POINTER(Abi), ctypes.c_int, ctypes.c_int, ctypes.POINTER(VOID)]
rt.mxl_rt_reg_in.argtypes = [VOID, ctypes.c_int, VOID, ctypes.c_int, ctypes.c_size_t, VOID]
rt.mxl_rt_reg_out.argtypes = [VOID, ctypes.c_int, VOID, ctypes.c_int]
rt.mxl_rt_unreg_in.argtypes = rt.mxl_rt_unreg_out.argtypes = [VOID, ctypes.c_int, ctypes.c_int64]
rt.mxl_rt_in_stats.argtypes = rt.mxl_rt_out_stats.argtypes = [VOID, ctypes.c_int]
rt.mxl_rt_in_stats.restype = ctypes.POINTER(InStats)
rt.mxl_rt_out_stats.restype = ctypes.POINTER(OutStats)
rt.mxl_rt_read_in.argtypes = [VOID, ctypes.c_int, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_uint64, VOID, ctypes.c_int]
rt.mxl_rt_put.argtypes = [VOID, ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64, VOID, ctypes.c_int,
                          ctypes.c_size_t, ctypes.c_size_t]


def real_abi():
    """Comme la production : version lue par mxlGetVersion, tailles par ctypes, pointeurs DANS la libmxl chargée."""
    v = bobimxl.mxlVersionType()
    st = LIB.mxlGetVersion(ctypes.byref(v))
    assert st == 0, "mxlGetVersion -> %d" % st
    addr = lambda n: ctypes.cast(getattr(LIB, n), VOID).value        # noqa: E731
    return Abi(abi_tag=1, lib_major=v.major, lib_minor=v.minor, lib_bugfix=v.bugfix,
               sizeof_buffer_slice=ctypes.sizeof(bobimxl.mxlBufferSlice),
               sizeof_wrapped_multi=ctypes.sizeof(bobimxl.mxlWrappedMultiBufferSlice),
               sizeof_mutable_multi=ctypes.sizeof(bobimxl.mxlMutableWrappedMultiBufferSlice),
               offset_stride=bobimxl.mxlWrappedMultiBufferSlice.stride.offset,
               offset_count=bobimxl.mxlWrappedMultiBufferSlice.count.offset,
               get_samples_nb=addr("mxlFlowReaderGetSamplesNonBlocking"), open_samples=addr("mxlFlowWriterOpenSamples"),
               commit_samples=addr("mxlFlowWriterCommitSamples"), cancel_samples=addr("mxlFlowWriterCancelSamples"),
               get_time=addr("mxlGetTime")), (v.major, v.minor, v.bugfix, v.full)


def bits(a):
    return np.ascontiguousarray(a).view(np.uint32)


def rand_block(rng, n, ch):
    return rng.integers(0, 2 ** 32, size=(n, ch), dtype=np.uint64).astype(np.uint32).view(np.float32)


FS = 48000
CH = 4


def phase_main():
    os.makedirs(args.domain, exist_ok=True)
    inst = bobimxl.Instance(args.domain)
    abi, ver = real_abi()
    print("libmxl %d.%d.%d (%s)" % ver)
    T = VOID()
    rc = rt.mxl_rt_init(ctypes.byref(abi), 2, 2, ctypes.byref(T))
    ok(rc == 0, "ABI : la vraie libmxl %d.%d.%d est acceptée" % ver[:3], rc)
    if rc:
        return
    rng = np.random.default_rng(7)

    # --- lecture : écrivain bobimxl, lecteur C vs lecteur bobimxl ---
    name = "banc-rt-in-%d" % os.getpid()
    w = bobimxl.AudioWriter(inst, name, channels=CH, sample_rate=FS, index_mode="tai")
    r = bobimxl.AudioReader(inst, name)
    head = int(LIB.mxlGetCurrentIndex(ctypes.byref(bobimxl.mxlRational(FS, 1))))
    start0 = head - 4800
    data = rand_block(rng, 4800, CH)
    for i in range(0, 4800, 48):
        w.write(data[i:i + 48], index=start0 + i)
    dst = np.zeros(8 * 480, np.float32)
    ok(rt.mxl_rt_reg_in(T, 0, r._h.value, 8, 480, dst.ctypes.data) == 0, "enregistrement du vrai lecteur")
    out = np.zeros((8, 240), np.float32)
    got = rt.mxl_rt_read_in(T, 0, start0 + 1000, 240, 0, out.ctypes.data, 8)
    ref = r.read_from(start0 + 1000, 240)
    ok(got == CH and ref is not None, "lecture C et lecture bobimxl réussissent", (got, ref is None))
    if got == CH and ref is not None:
        ok(np.array_equal(bits(out[:CH, :240]), bits(ref.T)), "C == AudioReader.read_from, exact au bit (même index, même sens)")
        ok(np.array_equal(bits(out[:CH, :240]), bits(data[1000:1240].T)), "C == données écrites aux index d'origine")
    s = rt.mxl_rt_in_stats(T, 0).contents
    ok(s.reads >= 1 and s.state == 1, "compteurs publiés", (s.reads, s.state))
    miss_old = rt.mxl_rt_read_in(T, 0, start0 - 10 ** 7, 240, 0, out.ctypes.data, 8)
    miss_new = rt.mxl_rt_read_in(T, 0, head + 10 ** 7, 240, 0, out.ctypes.data, 8)
    ok(miss_old == -13 and miss_new == -13, "trop vieux / trop neuf -> manque (la vraie libmxl refuse en non bloquant)", (miss_old, miss_new))
    s = rt.mxl_rt_in_stats(T, 0).contents
    ok(s.misses == 2 and s.state == 1, "manques comptés sans mise en faute", (s.misses, s.state))

    # --- écriture : séquence identique via mxl_io.OutPort (binding réel) et via le C ---
    io = mxl_io.Io(bobimxl, args.domain)
    py = mxl_io.OutPort(io, 0, "banc-rt-py-%d" % os.getpid(), CH)
    cname = "banc-rt-c-%d" % os.getpid()
    cw = bobimxl.AudioWriter(inst, cname, channels=CH, sample_rate=FS, index_mode="tai")
    ok(rt.mxl_rt_reg_out(T, 0, cw._h.value, CH) == 0, "enregistrement du vrai écrivain")
    cr = bobimxl.AudioReader(inst, cname)
    rnd = random.Random(11)
    pos = int(LIB.mxlGetCurrentIndex(ctypes.byref(bobimxl.mxlRational(FS, 1)))) - 20000
    blocks = []
    for _ in range(60):
        n = rnd.choice([48, 240, 480])
        m = rnd.random()
        start = pos if m < 0.6 else pos + rnd.randrange(1, 4000) if m < 0.8 else pos - rnd.randrange(1, n) if m < 0.9 else pos + 9601 + rnd.randrange(0, 500)
        blk = rand_block(rng, n, CH).T.copy()                # (canaux, n)
        py.put(start, blk)
        rt.mxl_rt_put(T, 0, start, 0, blk.ctypes.data, CH, n, n)
        blocks.append((start, blk))
        pos = py.next_idx if py.next_idx is not None else start + n
    o = rt.mxl_rt_out_stats(T, 0).contents
    cmp_c = dict(written=o.written, filled=o.filled, dropped=o.dropped, jumps=o.jumps, errors=o.errors)
    cmp_p = dict(written=py.written, filled=py.filled, dropped=py.dropped, jumps=py.jumps, errors=py.errors)
    ok(cmp_c == cmp_p and o.next_idx == py.next_idx, "mêmes compteurs que OutPort.put (vraie libmxl)", (cmp_c, cmp_p))
    # relecture croisée : le flux du C relu par bobimxl == le flux de OutPort relu par bobimxl, sur les index écrits
    pyr = bobimxl.AudioReader(inst, py.name)
    diff = 0
    checked = 0
    for start, blk in blocks[-10:]:
        n = blk.shape[1]
        a = cr.read_from(start, n)
        b = pyr.read_from(start, n)
        if a is not None and b is not None:
            checked += 1
            diff += int(not np.array_equal(bits(a), bits(b)))
    ok(checked >= 3 and diff == 0, "relecture bobimxl : flux du C == flux de OutPort sur %d blocs (même sens d'index)" % checked, (checked, diff))
    rt.mxl_rt_unreg_in(T, 0, 0)
    rt.mxl_rt_unreg_out(T, 0, 0)
    for obj in (cr, pyr, r, w, cw):
        try:
            obj.close()
        except Exception:
            pass
    for p in list(io.ports.values()):
        p.close()
    inst.close()


def phase_sigbus():
    """Tronque le fichier de données du flux sous un lecteur C. Peut tuer le processus (faute dans libmxl,
    non gardée) : c'est précisément ce qu'on veut savoir, d'où le sous-processus."""
    os.makedirs(args.domain, exist_ok=True)
    inst = bobimxl.Instance(args.domain)
    abi, ver = real_abi()
    T = VOID()
    assert rt.mxl_rt_init(ctypes.byref(abi), 1, 0, ctypes.byref(T)) == 0
    name = "banc-rt-sigbus-%d" % os.getpid()
    w = bobimxl.AudioWriter(inst, name, channels=CH, sample_rate=FS, index_mode="tai")
    r = bobimxl.AudioReader(inst, name)
    head = int(LIB.mxlGetCurrentIndex(ctypes.byref(bobimxl.mxlRational(FS, 1))))
    rng = np.random.default_rng(3)
    for i in range(0, 4800, 48):
        w.write(rand_block(rng, 48, CH), index=head - 4800 + i)
    dst = np.zeros(8 * 480, np.float32)
    rt.mxl_rt_reg_in(T, 0, r._h.value, 8, 480, dst.ctypes.data)
    out = np.zeros((8, 240), np.float32)
    assert rt.mxl_rt_read_in(T, 0, head - 3000, 240, 0, out.ctypes.data, 8) == CH
    fid = bobimxl.flow_id(name)
    cut = 0
    for root, _, files in os.walk(args.domain):
        if fid in root:
            for f in files:
                if f in ("data", "channels", "grains") or "chan" in f or f.endswith(".data"):
                    os.truncate(os.path.join(root, f), 0)
                    cut += 1
    print("fichiers tronqués : %d" % cut)
    rc = rt.mxl_rt_read_in(T, 0, head - 3000, 240, 0, out.ctypes.data, 8)
    s = rt.mxl_rt_in_stats(T, 0).contents
    print("SIGBUS_RESULT rc=%d state=%d faults=%d errors=%d last_err=%d" % (rc, s.state, s.faults, s.errors, s.last_err))


if args.phase == "sigbus":
    phase_sigbus()
    sys.exit(0)

phase_main()

print("-- phase faute (sous-processus) --")
r = subprocess.run([sys.executable, os.path.abspath(__file__), "--dir", args.dir, "--domain", args.domain,
                    "--phase", "sigbus"] + (["--so", args.so] if args.so else []), capture_output=True, text=True)
line = [x for x in r.stdout.splitlines() if x.startswith("SIGBUS_RESULT")]
if line:
    print("   " + line[0])
    ok("state=2" in line[0] or "rc=-13" in line[0] or "rc=%d" % 4 in line[0],
       "troncature sous le lecteur : le processus a survécu, comportement observé ci-dessus")
else:
    print("   le sous-processus s'est arrêté (code %s) : faute DANS libmxl (non gardée, limite connue) ou fichiers non trouvés" % r.returncode)
    print(r.stdout[-400:], r.stderr[-400:])
    ok(False, "la troncature sous le lecteur tue le processus : à traiter (garde autour des appels libmxl) avant l'étape 5")
shutil.rmtree(args.domain, ignore_errors=True)

print()
if ko:
    print("FAIL — %d échec(s):\n  - %s" % (len(ko), "\n  - ".join(ko)))
    sys.exit(1)
print("ALL OK")
