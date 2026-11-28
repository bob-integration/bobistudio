# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""APTITUDE OF A NODE to hold real-time containers, measured (P4 of the resource guarantee, 2026-11-23).

A reservation proves nothing about the machine under it. Measured on 2026-11-22/23: r620-2 is
compliant on paper (governor `performance`, cores reserved) and works at 1.5-1.7 GHz for 2.8 GHz,
because its BIOS keeps the frequency; « frères HT » warnings were repeated about 188 times a day,
a permanent state told as an event. This module turns each such state into ONE line of a sheet,
with what to do about it:

  kernel     PREEMPT_RT or not (a DeckLink card keeps the normal kernel: Blackmagic 16.4a1)
  freq       frequency OBTAINED under work on ONE core of the shared pool (never a dedicated one),
             read through /proc/cpuinfo (APERF/MPERF): one second fully busy, then bursts like a
             frame (ms of work, 10 ms asleep); the lower against the NOMINAL frequency — a BIOS
             that holds the frequency is seen here and nowhere else
  governor   cores not in `performance`
  ht         dedicated cores whose HT sibling is held by another reservation, left to the shared
             pool, or left to the system (outside the pool, not isolated: CPU 0's twin). A sibling
             the pool CLOSES (whole-core reservation, held empty by the agent's guard partition) or
             in the 2110 engine's footprint is not shared
  rt         the real-time throttling threshold (`sched_rt_runtime_us`)
  irq        device interrupts RECEIVED by dedicated cores during the same second (counted twice
             in /proc/interrupts): an affinity says where an interrupt may go, not what hits. Split
             into MOVABLE ones (the agent should have steered them: action `irq`) and kernel-MANAGED
             ones (one queue per CPU, they serve the I/O issued from that CPU; their affinity cannot
             be written): for those the action names the boot parameter that makes them movable
             (`irq_managed`, `fixable` when the host driver of that name can set it: a button of
             the sheet runs it, the node is never rebooted) — nothing is claimed fixed. The 2110
             engine's main and service CPUs (not isolated: ordinary threads beside the kernel, by
             design) do not count
  membw      memory bandwidth contention (app/membw.py, read from the node snapshot)

Levels: ok / warn / bad / unknown. A `bad` line makes the node « unfit »: the project verdict says
so for its Dedicated containers (app/project_resources.py), and one alert is raised when a line
turns bad, one when it is fine again — never a repeated one.

The sheet is kept in `node_aptitude` (one row per node) and refreshed every REFRESH_S by the
node_health loop, or on demand from the node sheet.
"""

import json
import logging
import threading
import time

from .database import db_add_alert, db_get_node, get_db
from .episodes import EtatEpisodes

log = logging.getLogger(__name__)

REFRESH_S = 6 * 3600
# Drivers whose queues the kernel MANAGES (one vector per CPU, affinity fixed) unless a module
# parameter says otherwise: irq name prefix → (module, value of the parameter read by the probe that
# means « managed », boot parameter that makes them ordinary, hence steerable by the agent).
# Measured on dell-1 (2026-11-25): megasas0-msix13/14/6/5 = CPUs 25/27/11/9, the player's own
# dedicated cores, 26/s — its disk reads complete on the CPU that issued them.
# The host driver `irq_managed` (node_agent/host_drivers/irq_managed.py, table DRIVERS) puts these
# parameters on the boot line: the two tables are kept in step (tests/verif_irq_managed_driver.py).
MANAGED_IRQ_DRIVERS = {
    "megasas": ("megaraid_sas", "1", "megaraid_sas.smp_affinity_enable=0"),
    "mpt3sas": ("mpt3sas", "1", "mpt3sas.smp_affinity_enable=0"),
    "mpt2sas": ("mpt3sas", "1", "mpt3sas.smp_affinity_enable=0"),
    "smartpqi": ("smartpqi", "0", "smartpqi.disable_managed_interrupts=1"),
    "nvme": (None, None, ""),          # always managed, no driver parameter
}
FREQ_BAD_RATIO = 0.9           # obtained / nominal frequency under work
FREQ_WARN_RATIO = 0.98
RT_RUNTIME_DEFAULT = 950000
BAD_KEYS = ("freq", "governor")  # lines that can be `bad` (the others warn)

_lock = threading.Lock()
_running = set()
_alerted = EtatEpisodes("node_aptitude")      # "<node_id>:<key>" → 1 while the line is bad

PROBE = r'''
c=%(core)s
echo "kernel $(uname -r) $(uname -v | grep -c PREEMPT_RT) $(cat /sys/kernel/realtime 2>/dev/null || echo 0)"
echo "rt $(cat /proc/sys/kernel/sched_rt_runtime_us 2>/dev/null) $(cat /proc/sys/kernel/sched_rt_period_us 2>/dev/null)"
n=0; p=0
for d in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do [ -d "$d" ] || continue; n=$((n+1));
  [ "$(cat $d/scaling_governor 2>/dev/null)" = performance ] && p=$((p+1)); done
echo "governor $n $p"
d=/sys/devices/system/cpu/cpu0/cpufreq
echo "fmax $(cat $d/cpuinfo_max_freq 2>/dev/null || echo 0) $(cat $d/base_frequency 2>/dev/null || echo 0)"
echo "model $(grep -m1 '^model name' /proc/cpuinfo | grep -o '@ *[0-9.]*GHz' | tr -dc '0-9.')"
echo "decklink $(grep -l 0xbdbd /sys/bus/pci/devices/*/vendor 2>/dev/null | wc -l)"
echo "isolated $(cat /sys/devices/system/cpu/isolated 2>/dev/null)"
echo "irqmod megaraid_sas $(cat /sys/module/megaraid_sas/parameters/smp_affinity_enable 2>/dev/null || echo -)"
echo "irqmod smartpqi $(cat /sys/module/smartpqi/parameters/disable_managed_interrupts 2>/dev/null || echo -)"
echo "irqmod mpt3sas $(cat /sys/module/mpt3sas/parameters/smp_affinity_enable 2>/dev/null || echo -)"
t=$(mktemp); cat /proc/interrupts > "$t"; t0=$(date +%%s%%N)
if [ -n "$c" ] && command -v taskset >/dev/null; then
  mhz() { awk -v c="$c" '/^processor/{p=$3} /^cpu MHz/{if (p==c) print $4}' /proc/cpuinfo; }
  taskset -c "$c" timeout 1 sh -c 'while :; do :; done' &
  sleep 0.7; echo "fwork $c $(mhz)"; wait
  # Bursts, like a plugin's frame: a few ms of work, 10 ms asleep (`read -t` sleeps without a fork).
  taskset -c "$c" timeout 1.5 bash -c 'exec 9<> <(:); while :; do for i in {1..3000}; do :; done; read -t 0.01 -u 9; done' &
  sleep 1.2; echo "fburst $c $(mhz)"; wait
else
  sleep 1
fi
echo "window_ms $(( ($(date +%%s%%N) - t0) / 1000000 ))"
awk 'NR==FNR { if (FNR > 1 && $1 ~ /^[0-9]+:$/) for (i = 2; i <= NF; i++) a[$1, i] = $i; next }
     FNR == 1 { for (j = 1; j <= NF; j++) cpu[j + 1] = substr($j, 4); n = NF; next }
     $1 ~ /^[0-9]+:$/ { for (i = 2; i <= n + 1; i++) { d = $i - a[$1, i];
         if (d > 0) printf "irqd %%s %%s %%d %%s\n", substr($1, 1, length($1) - 1), cpu[i], d, $NF } }' "$t" /proc/interrupts > "$t.d"
cat "$t.d"
for i in $(awk '{print $2}' "$t.d" | sort -un); do echo "irqaff $i $(cat /proc/irq/$i/smp_affinity_list 2>/dev/null)"; done
rm -f "$t" "$t.d"
'''


def ensure_table(db=None):
    db = db or get_db()
    db.execute("CREATE TABLE IF NOT EXISTS node_aptitude (node_id INTEGER PRIMARY KEY, ts REAL, "
               "data TEXT)")
    db.commit()


def _fmt(cpus):
    from .core_pool import fmt_cpuset
    return fmt_cpuset(cpus)


def _cpus(spec):
    from .core_pool import parse_cpuset
    try:
        return set(parse_cpuset(spec or ""))
    except Exception:                                          # noqa: BLE001
        return set()


def parse(out):
    """Probe output → dict of raw facts."""
    f = {"irqs": [], "irqmod": {}, "irqaff": {}}
    for line in (out or "").splitlines():
        w = line.split()
        if not w:
            continue
        try:
            if w[0] == "kernel" and len(w) >= 4:
                f["kernel"] = w[1]
                f["preempt_rt"] = w[2] != "0" or w[3] == "1"
            elif w[0] == "rt" and len(w) >= 2:
                f["rt_runtime"] = int(w[1])
            elif w[0] == "governor" and len(w) >= 3:
                f["gov_total"], f["gov_perf"] = int(w[1]), int(w[2])
            elif w[0] == "fmax" and len(w) >= 3:
                f["fmax_mhz"], f["fbase_mhz"] = int(w[1]) // 1000, int(w[2]) // 1000
            elif w[0] == "fwork" and len(w) >= 3:
                f["fwork_core"], f["fwork_mhz"] = int(w[1]), round(float(w[2]))
            elif w[0] == "fburst" and len(w) >= 3:
                f["fburst_mhz"] = round(float(w[2]))
            elif w[0] == "decklink" and len(w) >= 2:
                f["decklink"] = int(w[1])
            elif w[0] == "model" and len(w) >= 2 and w[1]:
                f["fmodel_mhz"] = round(float(w[1]) * 1000)
            elif w[0] == "isolated":
                f["isolated"] = sorted(_cpus(w[1] if len(w) > 1 else ""))
            elif w[0] == "irqmod" and len(w) >= 3:
                f["irqmod"][w[1]] = w[2]
            elif w[0] == "irqaff" and len(w) >= 2:
                f["irqaff"][int(w[1])] = w[2] if len(w) > 2 else ""
            elif w[0] == "window_ms" and len(w) >= 2:
                f["window_ms"] = int(w[1])
            elif w[0] == "irqd" and len(w) >= 4:
                f["irqs"].append({"irq": int(w[1]), "cpu": int(w[2]), "count": int(w[3]),
                                  "name": w[4] if len(w) > 4 else ""})
        except (TypeError, ValueError):
            continue
    return f


def _line(key, level, **kw):
    return {"key": key, "level": level, **kw}


def _managed_kind(q, facts, unmovable):
    """Boot parameter making this kernel-managed IRQ steerable, "" when managed without a known one,
    None when the IRQ is an ordinary (movable) one."""
    for prefix, (mod, managed_value, token) in MANAGED_IRQ_DRIVERS.items():
        if (q.get("name") or "").startswith(prefix):
            if mod is None:
                return token
            v = (facts.get("irqmod") or {}).get(mod)
            if v == managed_value:
                return token
            if v is not None and v != "-":
                return None                              # the parameter says ordinary
    return "" if q["irq"] in unmovable else None


def evaluate(node_id, facts, alloc=None, core_of=None, membw=None, decklink_node=False,
             shared=None, pool=None, engines=(), unmovable=()):
    """Facts + the node's reservations → the sheet's lines. Pure: no I/O (the guard calls it).

    `shared`: the CPUs really offered to the shared pool (`core_pool.partage_disponible`), `pool`
    the node's compute pool — without them a sibling nobody reserves is taken as the pool's (the
    old reading, which named the twins the pool closes and the engine's own). `engines`: vmids of
    2110 engines. `unmovable`: IRQ numbers the agent could not steer (`isolation.irq_unmovable`)."""
    alloc = alloc or {}            # cpu → vmid (node_core_alloc)
    isolated = set(facts.get("isolated") or ())
    engines = set(engines or ())
    lines = []
    # kernel
    if "kernel" not in facts:
        lines.append(_line("kernel", "unknown"))
    elif facts.get("preempt_rt"):
        lines.append(_line("kernel", "ok", value=facts["kernel"], rt=True))
    elif decklink_node or facts.get("decklink"):
        lines.append(_line("kernel", "ok", value=facts["kernel"], rt=False, reason="decklink"))
    else:
        lines.append(_line("kernel", "warn", value=facts["kernel"], rt=False, action="rt_kernel"))
    # frequency under work, against the NOMINAL frequency (not the turbo maximum: all cores loaded
    # never reach it — dell-1 works at 2.8 GHz for 3.6 turbo, which is fine).
    nominal = facts.get("fbase_mhz") or facts.get("fmodel_mhz") or facts.get("fmax_mhz") or 0
    # Judged on the LOWER of the two: under bursts (a plugin's frame) a BIOS that drives the
    # frequency may never ramp — r620-2 on 2026-11-23: 2.7 GHz fully loaded, 1.2 GHz in bursts.
    fwork, fburst = facts.get("fwork_mhz"), facts.get("fburst_mhz")
    if not nominal or fwork is None:
        lines.append(_line("freq", "unknown"))
    else:
        low = min(fwork, fburst) if fburst else fwork
        ratio = low / nominal
        lvl = "bad" if ratio < FREQ_BAD_RATIO else "warn" if ratio < FREQ_WARN_RATIO else "ok"
        lines.append(_line("freq", lvl, value=low, full=fwork, burst=fburst, nominal=nominal,
                           max=facts.get("fmax_mhz") or None, pct=round(100 * ratio),
                           core=facts.get("fwork_core"), action="bios" if lvl != "ok" else None))
    # governor
    tot = facts.get("gov_total")
    if tot is None:
        lines.append(_line("governor", "unknown"))
    elif tot == 0:
        lines.append(_line("governor", "ok", value=0, total=0))      # no cpufreq interface (VM)
    else:
        off = tot - (facts.get("gov_perf") or 0)
        lines.append(_line("governor", "bad" if off else "ok", value=off, total=tot,
                           action="governor" if off else None))
    # HT siblings of dedicated cores
    if alloc and not core_of:
        lines.append(_line("ht", "unknown"))
    elif alloc:
        by_core = {}
        for cpu, core in core_of.items():
            by_core.setdefault(core, []).append(cpu)
        shared_other, shared_pool, shared_system = set(), set(), set()
        pool_set = set(pool or ())
        for cpu, vmid in alloc.items():
            for sib in by_core.get(core_of.get(cpu), []):
                if sib == cpu:
                    continue
                owner = alloc.get(sib)
                if owner is not None:
                    if owner != vmid:
                        shared_other.add(vmid)
                elif shared is None or sib in shared:
                    shared_pool.add(vmid)
                elif vmid in engines or sib in pool_set or sib in isolated:
                    continue     # engine footprint / closed by the pool (guard partition) / isolated
                else:
                    shared_system.add(vmid)
        n = len(shared_other | shared_pool | shared_system)
        act = None
        if shared_other or shared_pool:
            act = "ht"
        elif shared_system:
            act = "ht_system"
        lines.append(_line("ht", "warn" if n else "ok", value=n, other=sorted(shared_other),
                           pool=sorted(shared_pool), system=sorted(shared_system), action=act))
    else:
        lines.append(_line("ht", "ok", value=0, other=[], pool=[]))
    # RT throttling
    rt = facts.get("rt_runtime")
    if rt is None:
        lines.append(_line("rt", "unknown"))
    else:
        lines.append(_line("rt", "warn" if (rt != -1 and rt < RT_RUNTIME_DEFAULT) else "ok", value=rt,
                           action="rt_runtime" if (rt != -1 and rt < RT_RUNTIME_DEFAULT) else None))
    # interrupts RECEIVED by dedicated cores during the probe's window (/proc/interrupts, twice)
    if "window_ms" not in facts:
        lines.append(_line("irq", "unknown"))
    else:
        per = {}
        unmovable = {int(x) for x in unmovable or ()}
        for q in facts["irqs"]:
            v = alloc.get(q["cpu"])
            if v is None:
                continue
            if v in engines and isolated and q["cpu"] not in isolated:
                continue      # the engine's main / service CPU: ordinary threads beside the kernel
            e = per.setdefault((q["irq"], q["name"]), {"irq": q["irq"], "name": q["name"], "count": 0,
                                                       "vmids": set(), "cpus": set(),
                                                       "managed": _managed_kind(q, facts, unmovable)})
            e["count"] += q["count"]
            e["vmids"].add(v)
            e["cpus"].add(q["cpu"])
        s_ = max(facts["window_ms"], 1) / 1000.0
        hit = sorted(per.values(), key=lambda e: -e["count"])
        rate = round(sum(e["count"] for e in hit) / s_)

        def _name(e):
            return "%s (%d/s)" % (e["name"] or e["irq"], round(e["count"] / s_))
        movable = [e for e in hit if e["managed"] is None]
        managed = [e for e in hit if e["managed"] is not None]
        cmdline = sorted({e["managed"] for e in managed if e["managed"]})
        if managed and any(not e["managed"] for e in managed):
            # managed without a driver parameter: the kernel's own best effort (only when the
            # queue's mask also holds a housekeeping CPU)
            cmdline.append("isolcpus=managed_irq,%s" % _fmt(sorted({c for e in managed for c in e["cpus"]
                                                                       if not e["managed"]})))
        lines.append(_line("irq", "warn" if hit else "ok", value=rate,
                           names=[_name(e) for e in hit[:6]],
                           movable=[_name(e) for e in movable[:6]],
                           managed=[_name(e) for e in managed[:6]],
                           cmdline=cmdline,
                           # a driver parameter is named: the host driver `irq_managed` can put it
                           # on the boot line (button of the sheet); nvme alone cannot be fixed so
                           fixable=any(e["managed"] for e in managed),
                           vmids=sorted({v for e in hit for v in e["vmids"]}),
                           action=("irq" if movable else "irq_managed" if managed else None)))
    # memory bandwidth
    # `level` is None when healthy, "warning" / "error" under contention (membw._check_alert).
    if not (membw or {}).get("gbps"):
        lines.append(_line("membw", "unknown"))
    else:
        lvl = membw.get("level")
        lines.append(_line("membw", "warn" if lvl else "ok", value=membw.get("gbps"),
                           ratio=membw.get("ratio"), action="membw" if lvl else None))
    return lines


def verdict(lines):
    """`unfit` with a bad line, `fit_with_warnings`, `fit`, or `unknown` when nothing was measured."""
    levels = {l["level"] for l in lines or []}
    if "bad" in levels:
        return "unfit"
    if not levels or levels == {"unknown"}:
        return "unknown"
    return "fit_with_warnings" if "warn" in levels else "fit"


def _shared_core(node_id):
    """One core of the shared pool, never a dedicated one (the busy loop goes there) — and, when
    there is one, a core whose HT SIBLING is shared too: loading the sibling of a dedicated core
    would slow the container on that physical core, the very thing the sheet watches for."""
    from . import core_pool
    try:
        libres = sorted(core_pool.partage_disponible(node_id)[0])
        if not libres:
            return None
        core_of = core_pool.core_map_cached(node_id) or {}
        held = {c for cs in core_pool.allocations_by_vmid(node_id).values() for c in cs}
        busy_phys = {core_of.get(c) for c in held if c in core_of}
        quiet = [c for c in libres if core_of.get(c) is None or core_of.get(c) not in busy_phys]
        return (quiet or libres)[-1]
    except Exception as e:                                     # noqa: BLE001
        log.debug("node_aptitude: no shared core on %s: %s", node_id, e)
        return None


def _engine_vmids(node_id):
    """vmids of the node's 2110 engines (their lcores are reservations, their footprint by design)."""
    from .database import db_get_containers
    out = set()
    for c in db_get_containers() or []:
        if c.get("node_id") != node_id:
            continue
        try:
            if (json.loads(c.get("deploy_config") or "{}") or {}).get("type") == "2110_io":
                out.add(c["vmid"])
        except (TypeError, ValueError):
            continue
    return out


def _unmovable(snap):
    """IRQ numbers the agent reported it could not steer ("<irq> <names> on <cpus>")."""
    out = set()
    for u in ((snap or {}).get("isolation") or {}).get("irq_unmovable") or []:
        try:
            out.add(int(str(u).split()[0]))
        except (ValueError, IndexError):
            continue
    return out


def measure(node_id):
    """Probe the node, evaluate, store, alert on transitions → the stored sheet, or None."""
    from . import core_pool, node_driver, node_health
    node = db_get_node(node_id)
    if not node:
        return None
    with _lock:
        if node_id in _running:
            return get(node_id)
        _running.add(node_id)
    try:
        core = _shared_core(node_id)
        rc, out, err = node_driver.host_exec(node, PROBE % {"core": "" if core is None else core},
                                             timeout=30)
        if rc != 0:
            log.warning("node_aptitude: probe of %s failed rc=%s %s", node.get("name"), rc,
                        (err or "")[-200:])
            return get(node_id)
        facts = parse(out)
        with get_db() as db:
            alloc = {r["core"]: r["vmid"] for r in db.execute(
                "SELECT core, vmid FROM node_core_alloc WHERE node_id=?", (node_id,)).fetchall()}
        snap = (node_health.latest().get("nodes") or {}).get(str(node_id)) or {}
        from .node_driver import node_capabilities
        core_of = core_pool.core_map_cached(node_id)
        pool = core_pool.parse_cpuset(node.get("compute_cpuset") or "")
        shared = None
        if core_of:
            shared = set(core_pool.partage_disponible(node_id, pool=pool, core_of=core_of)[0])
        lines = evaluate(node_id, facts, alloc=alloc, core_of=core_of,
                         membw=snap.get("membw"),
                         decklink_node="decklink" in (node_capabilities(node) or []),
                         shared=shared, pool=pool, engines=_engine_vmids(node_id),
                         unmovable=_unmovable(snap))
        sheet = {"node_id": node_id, "ts": time.time(), "verdict": verdict(lines), "lines": lines,
                 "facts": {k: v for k, v in facts.items() if k not in ("irqs", "irqaff")}}
        db = get_db()
        ensure_table(db)
        db.execute("INSERT OR REPLACE INTO node_aptitude (node_id, ts, data) VALUES (?, ?, ?)",
                   (node_id, sheet["ts"], json.dumps(sheet)))
        db.commit()
        _transitions(node, lines)
        return sheet
    finally:
        with _lock:
            _running.discard(node_id)


def _transitions(node, lines):
    for l in lines:
        k = "%s:%s" % (node["id"], l["key"])
        if l["level"] == "bad" and not _alerted.get(k):
            _alerted.poser(k, 1)
            db_add_alert("alert.node.aptitude_bad", "error", node_id=node["id"], kind="resource",
                         params={"n": node.get("name"), "line": l["key"]})
        elif l["level"] in ("ok", "warn") and _alerted.get(k):
            _alerted.retirer(k)
            db_add_alert("alert.node.aptitude_recovered", "info", node_id=node["id"], kind="resource",
                         params={"n": node.get("name"), "line": l["key"]})
    _alerted.flush()


def get(node_id):
    """The stored sheet, or None (never measured)."""
    db = get_db()
    try:
        ensure_table(db)
        r = db.execute("SELECT data FROM node_aptitude WHERE node_id=?", (node_id,)).fetchone()
        return json.loads(r[0]) if r else None
    except Exception as e:                                     # noqa: BLE001
        log.warning("node_aptitude: cannot read %s: %s", node_id, e)
        return None


def refresh_due(node_id):
    """Called by the node_health loop: measure in the background when the sheet is older than
    REFRESH_S (or missing). Never blocks the loop."""
    s = get(node_id)
    if s and time.time() - (s.get("ts") or 0) < REFRESH_S:
        return False
    with _lock:
        if node_id in _running:
            return False
    threading.Thread(target=measure, args=(node_id,), daemon=True, name="aptitude-%s" % node_id).start()
    return True


def unfit_lines(node_id):
    """Keys of the bad lines of the stored sheet ([] when fit or never measured)."""
    s = get(node_id)
    return [l["key"] for l in (s or {}).get("lines") or [] if l.get("level") == "bad"]
