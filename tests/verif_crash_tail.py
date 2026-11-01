# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Guard — last output of a script that died ON ITS OWN is kept by the controller (app/crash_tail.py).

Origin: Z8, mixer #227, 29/10 18:16:57 — the script stopped by itself (« script terminé (code 1) »)
right after a redeploy, the container was recreated at 21:30, and the traceback was lost with it.

Exercises the REAL functions, offline, on a throwaway database (DB_PATH set before any access —
the production base is never opened for writing):
  1. the real per-container agent (script_templates/agent.py, imported, real subprocesses): a
     spontaneous death sets `last_exit_ts`, a `/stop` (`_stop_proc`) does NOT — the discriminator
     the controller relies on (`crash_tail.is_new_death`);
  2. masking of the obvious secrets (`redact`);
  3. size bound: 200 lines, 64 KiB, the END kept (`bound`);
  4. persistence + dedup on (vmid, last_exit_ts) + `unavailable` row when the output cannot be
     read + retention per vmid + readable after the container row is gone (`capture`, `get`);
  5. node-agent answer carrying a docker daemon error is NOT stored as output (`fetch_lines`);
  6. `metrics._cause_arret` uses the captured tail and does not query the node a second time.

Usage: ./venv/bin/python tests/verif_crash_tail.py   — exit 0 if everything passes, 1 otherwise.
"""
import importlib.util
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES = []


def check(name, cond, detail=""):
    print(("[OK  ] " if cond else "[FAIL] ") + name + (f" — {detail}" if (detail and not cond) else ""))
    if not cond:
        FAILURES.append(name)


_tmp = tempfile.mkdtemp(prefix="verif_crash_tail_")
_db_path = os.path.join(_tmp, "test.db")
import app.database as database          # noqa: E402
database.DB_PATH = _db_path
import app.config as config              # noqa: E402
config.DB_PATH = _db_path
database.init_db()
assert os.path.realpath(database.DB_PATH) == os.path.realpath(_db_path)

from app import crash_tail               # noqa: E402

# ─── 1. Real agent: spontaneous death vs /stop ───────────────────────────────────────────────
spec = importlib.util.spec_from_file_location("bobi_ct_agent", os.path.join(ROOT, "script_templates", "agent.py"))
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _status():
    run = agent._is_running()
    return {"running": run, "last_exit": agent.DERNIERE_MORT["code"],
            "last_signal": agent.DERNIERE_MORT["signal"], "last_exit_ts": agent.DERNIERE_MORT["ts"]}


dying = os.path.join(_tmp, "dying.py")
with open(dying, "w") as f:
    f.write("raise RuntimeError('boom from crash-tail guard')\n")
sleeper = os.path.join(_tmp, "sleeper.py")
with open(sleeper, "w") as f:
    f.write("import time\nwhile True:\n    time.sleep(0.1)\n")

agent.LAST_PATH = dying
agent._start_proc()
_wait(lambda: agent.PROC is not None and agent.PROC.poll() is not None)
st1 = _status()
check("agent: spontaneous death → running false, exit code 1, last_exit_ts set",
      st1["running"] is False and st1["last_exit"] == 1 and st1["last_exit_ts"] is not None, str(st1))
check("controller: spontaneous death is NEW", crash_tail.is_new_death(st1, None))
check("controller: same death seen twice is not new", not crash_tail.is_new_death(st1, st1["last_exit_ts"]))

agent.LAST_PATH = sleeper
agent._start_proc()
_wait(lambda: bool(agent._script_pids()))
check("agent: sleeper running", _status()["running"] is True)
agent._stop_proc()
st2 = _status()
check("agent: /stop does NOT set a new last_exit_ts",
      st2["running"] is False and st2["last_exit_ts"] == st1["last_exit_ts"], f"{st1} → {st2}")
check("controller: death after /stop is not new", not crash_tail.is_new_death(st2, st1["last_exit_ts"]))
check("controller: a death older than MAX_AGE_S is not new (orchestrator restarted)",
      not crash_tail.is_new_death({"last_exit_ts": time.time() - crash_tail.MAX_AGE_S - 60}, None))
check("controller: old agent without the field → never 'new'", not crash_tail.is_new_death({"running": False}, None))

# ─── 2. Masking ─────────────────────────────────────────────────────────────────────────────
SECRETS = {
    "token=": ("GET /api?token=S3cr3tTok&x=1", "S3cr3tTok"),
    "Authorization: Bearer": ("Authorization: Bearer eyJhbGciOiJ.abc.def", "eyJhbGciOiJ.abc.def"),
    "Authorization: Basic": ("authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    "MXL_AGENT_TOKEN env": ("MXL_AGENT_TOKEN=9f8e7d6c5b4a", "9f8e7d6c5b4a"),
    "X-MXL-Agent-Token header": ("X-MXL-Agent-Token: abcdef0123", "abcdef0123"),
    "password=": ("db password=hunter2 ok", "hunter2"),
    "json password": ('{"password": "pa ss", "user": "u"}', "pa ss"),
    "repr token": ("{'api_key': 'K-123'}", "K-123"),
    "url credentials": ("srt://admin:Pw0rd@10.0.0.1:9000", "Pw0rd"),
}
for name, (line, secret) in SECRETS.items():
    out = crash_tail.redact(line)
    check(f"redact: {name}", secret not in out and crash_tail.MASK in out, out)
plain = 'Traceback (most recent call last):\n  File "/opt/script/main.py", line 3\nRuntimeError: boom'
check("redact: a plain traceback is untouched", crash_tail.redact(plain) == plain)

# ─── 3. Size bound ──────────────────────────────────────────────────────────────────────────
txt, tr = crash_tail.bound([f"line {i}" for i in range(500)])
ls = txt.splitlines()
check("bound: at most 200 lines, the LAST ones", len(ls) == 200 and ls[-1] == "line 499" and ls[0] == "line 300"
      and tr, f"{len(ls)} {ls[:1]} {ls[-1:]}")
big = [("é" * 600) + f" #{i}" for i in range(150)]          # ~180 KB of UTF-8
txt, tr = crash_tail.bound(big)
check("bound: at most 64 KiB, the END kept, valid UTF-8, flagged",
      len(txt.encode("utf-8")) <= crash_tail.MAX_BYTES and txt.endswith("#149") and tr
      and txt.splitlines()[0].endswith(tuple(f"#{i}" for i in range(150))),
      f"{len(txt.encode('utf-8'))} bytes")
txt, tr = crash_tail.bound(["a", "b"])
check("bound: small output unchanged, not flagged", txt == "a\nb" and not tr)

# ─── 4. Persistence, dedup, unavailable, retention ─────────────────────────────────────────
cont = {"vmid": 227, "hostname": "mixer-test", "node_id": 1, "docker_name": "bobi-cmp-227",
        "instance_uuid": "uuid-227"}
calls = []


def fake_fetch(lines):
    def f(c):
        calls.append(c)
        return list(lines), None
    return f


output = ["starting", "token=LEAKME", "Traceback (most recent call last):",
          '  File "/opt/script/main.py", line 3, in <module>', "RuntimeError: boom"]
st = {"running": False, "last_exit": 1, "last_signal": None, "last_exit_ts": 1761758217.5}
row = crash_tail.capture(227, cont, st, source="test", fetch=fake_fetch(output))
check("capture: row written, status ok", row is not None and row.get("status") == "ok" and row.get("id"), str(row))
stored = crash_tail.get(row["id"]) if row else None
check("capture: text persisted, ends with the exception",
      stored is not None and stored["tail"].endswith("RuntimeError: boom") and stored["exit_code"] == 1)
check("capture: secret masked BEFORE persisting", stored is not None and "LEAKME" not in (stored["tail"] or ""))
raw = database.get_db().execute("SELECT group_concat(tail) FROM script_crash_tails").fetchone()[0] or ""
check("capture: secret absent from the database file content", "LEAKME" not in raw)

row2 = crash_tail.capture(227, cont, st, source="deploy", fetch=fake_fetch(["other"]))
check("capture: same (vmid, last_exit_ts) → same row, node not asked again",
      row2 is not None and row2.get("existing") and row2["id"] == row["id"] and len(calls) == 1,
      f"{row2} calls={len(calls)}")

st_b = dict(st, last_exit_ts=st["last_exit_ts"] + 10)
row3 = crash_tail.capture(227, cont, st_b, fetch=lambda c: (None, "node agent: timeout"))
check("capture: output unreadable → 'unavailable' row with the reason (no silent failure)",
      row3 is not None and row3["status"] == "unavailable" and "timeout" in (row3.get("error") or ""), str(row3))

database.get_db().execute("DELETE FROM containers WHERE vmid=227")
check("capture: still readable once the container is gone", crash_tail.get(row["id"]) is not None)

for i in range(crash_tail.KEEP_PER_VMID + 5):
    crash_tail.capture(300, dict(cont, vmid=300), dict(st, last_exit_ts=1000.0 + i), fetch=fake_fetch([f"x{i}"]))
items = crash_tail.list_for(300, limit=1000)
check("retention: at most KEEP_PER_VMID rows per vmid, newest kept",
      len(items) == crash_tail.KEEP_PER_VMID and items[0]["exit_ts"] == 1000.0 + crash_tail.KEEP_PER_VMID + 4,
      f"{len(items)}")
check("listing: no text in the list", all("tail" not in i for i in items))

# ─── 5. fetch_lines: daemon error is not output ────────────────────────────────────────────
import app.node_driver as node_driver   # noqa: E402
_orig_call, _orig_get_node = node_driver._call, database.db_get_node
try:
    database.db_get_node = lambda nid: {"id": nid, "name": "n", "host": "127.0.0.1"}
    node_driver._call = lambda node, m, p, **kw: (True, {"lines": ["Error response from daemon: No such container: bobi-cmp-227"]})
    lines, err = crash_tail.fetch_lines(cont)
    check("fetch_lines: docker daemon error → unavailable, not stored as output",
          lines is None and "No such container" in (err or ""), f"{lines} {err}")
    node_driver._call = lambda node, m, p, **kw: (False, "connection refused")
    lines, err = crash_tail.fetch_lines(cont)
    check("fetch_lines: node agent down → reason given", lines is None and "refused" in (err or ""))
    node_driver._call = lambda node, m, p, **kw: (True, {"lines": ["a", "b"]})
    lines, err = crash_tail.fetch_lines(cont)
    check("fetch_lines: lines returned", lines == ["a", "b"] and err is None)
finally:
    node_driver._call, database.db_get_node = _orig_call, _orig_get_node

# ─── 6. _cause_arret reuses the capture ────────────────────────────────────────────────────
import app.metrics as metrics            # noqa: E402
_orig_exec = node_driver.host_exec
asked = []
node_driver.host_exec = lambda *a, **k: (asked.append(a), (0, "should not be used", ""))[1]
try:
    cause = metrics._cause_arret(227, cont, st, tail_row=stored)
finally:
    node_driver.host_exec = _orig_exec
check("_cause_arret: last line of the capture, node not queried again",
      cause is not None and "RuntimeError: boom" in cause and not asked, f"{cause} asked={len(asked)}")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    sys.exit(1)
print("ALL OK")
sys.exit(0)
