#!/usr/bin/env python3
"""Guard: creating a flow writer survives the teardown of the previous writer — within a BOUNDED window.

ORIGINAL DEFECT (Z8, mixer #227, 2026-10-29 18:16): the redeployed script reopened its CLEAN output
73 ms after the old process stopped, while the flow segment was still being torn down;
`mxlCreateFlowWriter` answered mxlStatus 1 and the script crashed at startup. The binding now
retries that transient status for ~50 ms (short sleeps), logs ONCE, then raises as before. Never
an infinite loop, never another status, never a `garbage_collect()` (global to the MXL domain).

Runs WITHOUT libmxl: `_lib` is a double. No shared memory, no database, no network.
Run: ./venv/bin/python tests/verif_bobimxl_creation_ecrivain.py
"""
import contextlib
import ctypes
import inspect
import io
import os
import re
import sys
import tempfile
import threading
import time

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_R, "script_templates"))
import bobimxl                                    # noqa: E402

FAILURES = []


def check(cond, label):
    print(("  ok    " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


class Lib:
    """libmxl double: answers the scripted statuses in turn, then the last one forever."""
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def mxlCreateFlowWriter(self, inst, flow_def, opts, out_h, _cfg, out_created):
        st = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        if st == bobimxl.MXL_STATUS_OK:
            ctypes.cast(out_created, ctypes.POINTER(ctypes.c_bool))[0] = True
        return st


class Inst:
    def __init__(self, domain):
        self.domain = domain
        self._h = None
        self.gc = 0

    def flow_def(self, name):
        return None

    def garbage_collect(self):
        self.gc += 1


def make(kind, inst):
    if kind == "video":
        return bobimxl.Writer(inst, "mixer_clean", 1920, 1080)
    return bobimxl.AudioWriter(inst, "mixer_audio")


def run(statuses, kind="video", limit_s=2.0):
    """Create a writer against the double; returns (writer|exception, calls, elapsed, stdout).
    Run in a thread with a deadline: an UNBOUNDED retry must fail the guard, not hang it."""
    lib = Lib(statuses)
    bobimxl._lib = lib
    inst = Inst(tempfile.mkdtemp())
    out = io.StringIO()
    res = {}

    def body():
        t0 = time.monotonic()
        with contextlib.redirect_stdout(out):
            try:
                res["v"] = make(kind, inst)
            except Exception as e:          # noqa: BLE001 — the verdict IS the exception
                res["v"] = e
        res["dt"] = time.monotonic() - t0

    th = threading.Thread(target=body, daemon=True)
    th.start()
    th.join(limit_s)
    if th.is_alive():
        return "HUNG", lib.calls, limit_s, out.getvalue(), inst
    return res["v"], lib.calls, res["dt"], out.getvalue(), inst


def main():
    T = bobimxl.CREATE_WRITER_TRANSIENT_STATUS
    W = bobimxl.CREATE_WRITER_RETRY_S
    print(f"  (retry window read from the binding: {W * 1000:.0f} ms, transient status {T})")

    print("\n1. Previous writer still being torn down: status 1 twice, then OK")
    v, calls, dt, out, inst = run([T, T, bobimxl.MXL_STATUS_OK])
    check(isinstance(v, bobimxl.Writer), "the video writer is created (no crash at startup)")
    check(calls == 3, f"three attempts ({calls})")
    check(out.count("retrying") == 1, f"the retry is logged ONCE ({out.count('retrying')})")
    check(inst.gc == 0, "no garbage_collect() (global to the MXL domain)")

    print("\n2. Same for the audio writer")
    v, calls, dt, out, inst = run([T, bobimxl.MXL_STATUS_OK], kind="audio")
    check(isinstance(v, bobimxl.AudioWriter), "the audio writer is created")
    check(calls == 2, f"two attempts ({calls})")

    print("\n3. Status 1 that never clears: BOUNDED, then raises as before")
    v, calls, dt, out, inst = run([T])
    check(v != "HUNG", "returns within 2 s (never an infinite retry)")
    check(isinstance(v, bobimxl.MXLError) and getattr(v, "status", None) == T,
          f"raises MXLError status {T} ({v!r})")
    check(W * 0.8 <= dt <= W + 0.25, f"gives up after the window ({dt * 1000:.0f} ms)")
    check(calls <= 2 + int(W / bobimxl.CREATE_WRITER_RETRY_SLEEP_S) * 2,
          f"attempts spaced by sleeps, not a busy loop ({calls})")
    check(out.count("retrying") == 1, f"logged once even when it fails ({out.count('retrying')})")
    check(inst.gc == 0, "still no garbage_collect()")

    print("\n4. Any OTHER status is not retried")
    v, calls, dt, out, inst = run([2])
    check(isinstance(v, bobimxl.MXLError) and getattr(v, "status", None) == 2, "raises at once")
    check(calls == 1, f"one attempt only ({calls})")
    check(out == "", "nothing logged")

    print("\n5. Every creation site goes through the bounded helper")
    src = inspect.getsource(bobimxl)
    raw = re.findall(r"_lib\.mxlCreateFlowWriter\(", src)
    check(len(raw) == 1, f"one raw mxlCreateFlowWriter call in the binding, inside the helper ({len(raw)})")
    helper = inspect.getsource(bobimxl._create_flow_writer)
    check("_lib.mxlCreateFlowWriter(" in helper, "that call is the helper's")
    check("garbage_collect" not in helper.split('"""')[-1], "the helper never collects")

    print()
    if FAILURES:
        print("FAILED — %d check(s):" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
