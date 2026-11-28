#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
"""Mixer: OpenMP team size of the C kernels defaults to 1, settable, published (0.23.74).

WHAT THIS GUARD HOLDS (2026-11-28). Measured on dell-1 (mixer 0.23.73, bench vmid 1426, cpuset
1,3,5, SCHED_FIFO 5, 1080p50): the PGM loop's lost time follows the team size (p99.9 0.2-0.9 ms
with 1 thread, ~1.2 ms with 3) and one thread holds the budget. So:
  1. with nothing set, the team is 1 thread (not one per physical core of the cpuset), source auto;
  2. the hidden param `mvk_threads` (>= 1) sets it, source config; 0 = auto; it is declared in the
     schema hidden, as a bounded number, so coerce_config clamps it;
  3. env BOBI_MVK_THREADS (>= 1) wins over the param; a malformed value falls to the next level;
  4. the count is APPLIED to the library (mvk_set_threads), not only computed;
  5. the effective sizes (read back from the library) are published as `mvk_teams` in /state, on
     :8080, and in loop_timing.threads; per_team is False (one global count in libbobi_mvk ABI 8).

Run: ./venv/bin/python tests/verif_mixer_fils_mvk.py   (FM_MUTATION=<name>: cf. MUTATIONS)
Verdict = exit code (0 green, 1 failure, 2 mutation anchor not found).
"""
import json
import os
import pathlib
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import verif_mixer_delais as H                                           # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "plugins/mixer/plugin.json").read_text())

MUTATIONS = {
    # auto back to the library default (one thread per physical core)
    "auto_not_one": ("    return 1\n\n\ndef _mvk_threads_resolve", "    return 3\n\n\ndef _mvk_threads_resolve"),
    # the param is ignored
    "config_ignored": ('        n = int(cfg.get("mvk_threads") or 0)', "        n = 0"),
    # the env no longer wins: param checked first
    "config_beats_env": ('    if n >= 1:\n        return min(n, MVK_THREADS_MAX), "env"',
                         '    if n >= 1 and not cfg.get("mvk_threads"):\n        return min(n, MVK_THREADS_MAX), "env"'),
    # the count is computed but not applied
    "not_applied": ("if _MVK:\n    bobimxl.mvk_set_threads(MVK_THREADS)\n", ""),
    # not published in /state
    "no_state": ('    s["mvk_teams"] = _mvk_teams()', "    pass"),
    # not published on :8080
    "no_metrics": ('        payload["mvk_teams"] = _mvk_teams()', "        pass"),
    # not in loop_timing
    "no_loop_timing": ("        threads=_mvk_teams(),\n", ""),
    # published value = requested, not read back from the library
    "not_read_back": ('    n = int(getattr(bobimxl, "mvk_threads", lambda: 0)()) if _MVK else 0',
                      "    n = MVK_THREADS"),
}
MUT = os.environ.get("FM_MUTATION")
if MUT:
    if MUT not in MUTATIONS:
        print("unknown mutation", MUT); sys.exit(2)
    a, b = MUTATIONS[MUT]
    if H.SRC.count(a) != 1:
        print("ANCHOR MISSING: mutation %s found %d times" % (MUT, H.SRC.count(a))); sys.exit(2)
    H.SRC = H.SRC.replace(a, b)

FAILS = []


def check(ok, msg):
    print(("  ok    " if ok else "  FAIL  ") + msg)
    if not ok:
        FAILS.append(msg)


class FakeMvk:
    """The library side: a global count, like libbobi_mvk (starts at 3, the old cpuset default)."""

    def __init__(self):
        self.n = 3
        self.sets = []

    def set(self, n):
        self.sets.append(int(n))
        self.n = max(1, int(n))


def load(cfg_extra=None, env=None, lib_floor=None):
    """Render and exec the mixer with a fake bobimxl that HAS the kernels. Returns (ns, lib)."""
    lib = FakeMvk()
    old_env = os.environ.get("BOBI_MVK_THREADS")
    if env is None:
        os.environ.pop("BOBI_MVK_THREADS", None)
    else:
        os.environ["BOBI_MVK_THREADS"] = env
    real_types_module = types.ModuleType

    def hook(name):
        m = real_types_module(name)
        if name == "bobimxl":
            m.mvk_available = lambda: True
            m.mvk_has_key = lambda: True
            m.invalid_grains_report = lambda: {}
            m.index_report = lambda: {}
            m.mvk_set_threads = lib.set
            # lib_floor simulates a library that clamps (read-back may differ from the request)
            m.mvk_threads = (lambda: lib.n if lib_floor is None else max(lib.n, lib_floor))
        return m
    types.ModuleType = hook
    cfg_src = H.SRC
    try:
        if cfg_extra:
            H.SRC = cfg_src.replace("CONFIG         = {config}",
                                    "CONFIG         = {config}\nCONFIG.update(%s)"
                                    % repr(cfg_extra).replace("{", "{{").replace("}", "}}"), 1)
        ns, _r, _p = H.make_ns({0: H.G, 1: H.G})
    finally:
        H.SRC = cfg_src
        types.ModuleType = real_types_module
        if old_env is None:
            os.environ.pop("BOBI_MVK_THREADS", None)
        else:
            os.environ["BOBI_MVK_THREADS"] = old_env
    return ns, lib


def metrics_payload(ns):
    h = ns["MetricsHandler"].__new__(ns["MetricsHandler"])
    out = []
    h.path = "/"
    h.send_response = lambda c: None
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.wfile = types.SimpleNamespace(write=lambda b: out.append(b))
    h.do_GET()
    return json.loads(b"".join(out).decode())


def main():
    print(__doc__.splitlines()[0])
    # ── schema: hidden, bounded number, 0 = auto
    f = [x for x in MANIFEST.get("config_schema", []) if x.get("key") == "mvk_threads"]
    check(len(f) == 1 and f[0].get("hidden") is True and f[0].get("type") == "number"
          and f[0].get("min") == 0 and f[0].get("default") == 0 and (f[0].get("max") or 0) >= 1,
          "config_schema: mvk_threads hidden, number, min 0, default 0, bounded")

    # ── 1. default
    ns, lib = load()
    t = ns["_mvk_teams"]()
    check(lib.sets and lib.sets[-1] == 1, "default: the library is set to 1 thread (sets %s)" % lib.sets)
    check(t["pgm"] == 1 and t["pvw"] == 1 and t["source"] == "auto",
          "default published pgm 1 / pvw 1, source auto: %s" % t)
    check(t["per_team"] is False, "per_team False (one global count)")

    # ── 2. param
    ns, lib = load({"mvk_threads": 3})
    t = ns["_mvk_teams"]()
    check(lib.n == 3 and t["pgm"] == 3 and t["pvw"] == 3 and t["source"] == "config",
          "mvk_threads 3 -> 3/3, source config: %s" % t)
    ns, lib = load({"mvk_threads": 0})
    check(ns["_mvk_teams"]()["pgm"] == 1 and ns["MVK_THREADS_SOURCE"] == "auto", "mvk_threads 0 = auto = 1")
    ns, lib = load({"mvk_threads": 999})
    check(ns["MVK_THREADS"] == ns["MVK_THREADS_MAX"], "clamped to MVK_THREADS_MAX: %s" % ns["MVK_THREADS"])

    # ── 3. env wins
    ns, lib = load({"mvk_threads": 3}, env="2")
    t = ns["_mvk_teams"]()
    check(lib.n == 2 and t["pgm"] == 2 and t["source"] == "env", "env 2 beats param 3: %s" % t)
    ns, lib = load({"mvk_threads": 3}, env="abc")
    check(ns["_mvk_teams"]()["source"] == "config" and lib.n == 3, "malformed env falls to the param")
    ns, lib = load(env="0")
    check(ns["_mvk_teams"]()["source"] == "auto" and lib.n == 1, "env 0 = not set -> auto 1")

    # ── 4/5. read back + published
    ns, lib = load(lib_floor=2)
    check(ns["_mvk_teams"]()["pgm"] == 2 and ns["_mvk_teams"]()["requested"] == 1,
          "published = read back from the library (2), not the request (1)")
    ns, lib = load()
    s = ns["_state_doc"]()
    check(s.get("mvk_teams", {}).get("pgm") == 1 and s["mvk_teams"].get("pvw") == 1, "/state mvk_teams 1/1")
    p = metrics_payload(ns)
    check(p.get("mvk_teams", {}).get("pgm") == 1 and p["mvk_teams"].get("pvw") == 1, ":8080 mvk_teams 1/1")
    lt = ns["_lt_report"](force=True)
    check(isinstance(lt, dict) and lt.get("threads", {}).get("pgm") == 1, "loop_timing.threads published")

    print("FAILED: %d" % len(FAILS) if FAILS else "ALL GREEN")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
