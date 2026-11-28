#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Host driver `irq_managed`: kernel-managed IRQs of disk controllers made movable (2026-11-25).

dell-1: megaraid_sas spreads one queue per CPU and the kernel MANAGES them (affinity not writable):
25 IRQ/s on the Dedicated cores of a player, which the agent could not steer. The fix is a module
parameter on the boot line; the product sets it (button of the aptitude sheet, install-node.sh),
the operator is never left with GRUB. Held here, offline, nothing touches the host:
  1. decide, on facts replayed from dell-1 (state run there on 2026-11-25) and variations: apply,
     pending_reboot, active after the reboot, ignored parameter, nvme never touched, controller
     without interrupts ignored, smartpqi's inverted value, no bootloader;
  2. run, end to end in a sandbox (the REAL probes, paths moved): only GRUB_CMDLINE_LINUX is
     written (GRUB_CMDLINE_LINUX_DEFAULT belongs to app/mtl.py), backup kept, update-grub called,
     every 10_linux entry read back, exit 10, idempotent; an /etc/default/grub.d override is caught;
     systemd-boot writes /etc/kernel/cmdline; revert takes it back out;
  3. the table agrees with app/node_aptitude.py, and the bootloader rule with app/mtl.py;
  4. aptitude replayed: before → action irq_managed, fixable (the button); after the reboot the
     agent steers the queues (agent._iso_irqs on a fake /proc/irq) → the `irq` line is ok;
  5. install-node.sh: resolve_irq_managed run for real in bash (new node → applied, reinstall →
     unchanged, flags win), provision_irq_managed calls the SAME script.

MUTATIONS, IRQM_MUT=<name> — each must give rc=1:
    default_line  the parameter goes into GRUB_CMDLINE_LINUX_DEFAULT
    no_verify     grub.cfg is not read back after update-grub
    value         the parameter's current value is ignored (a fixed controller looks managed)
    no_irqs       a loaded module without interrupts counts as a controller
    honoured      a parameter ignored by the driver is written again
    nvme          nvme gets a « parameter » like the others
    table         smartpqi's values are inverted
    fixable       the aptitude line offers the button for nvme alone
    reinstall     install-node.sh applies it on a reinstall too
    revert_regen  revert does not regenerate grub.cfg

    ./venv/bin/python tests/verif_irq_managed_driver.py
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
MUT = os.environ.get("IRQM_MUT", "")
DRV_PATH = os.path.join(ROOT, "node_agent", "host_drivers", "irq_managed.py")
DRV_MUT = {
    "default_line": ('            if var == "GRUB_CMDLINE_LINUX":\n                last = i\n',
                     '            if var == "GRUB_CMDLINE_LINUX_DEFAULT":\n                last = i\n'),
    "no_verify": ("        miss = [t for t in want if t not in ln]\n", "        miss = []\n"),
    "value": ('        st = "managed" if v == managed else', '        st = "managed" if True else'),
    "no_irqs": ('        if m not in f["loaded"] or not f["irq_count"].get(m):\n',
                '        if m not in f["loaded"]:\n'),
    "honoured": ("    if ignored:\n", "    if False:\n"),
    "nvme": ('OTHERS = {"nvme": ("nvme",)}\n', 'OTHERS = {}\nDRIVERS["nvme"] = (("nvme",), "poll_queues", "0", "1")\n'),
    "table": ('"smartpqi": (("smartpqi",), "disable_managed_interrupts", "0", "1"),',
              '"smartpqi": (("smartpqi",), "disable_managed_interrupts", "1", "0"),'),
    "revert_regen": ("            _regenerate(f, probe, (), gone_keys=keys)\n", "            pass\n"),
}
APT_MUT = {"fixable": ('fixable=any(e["managed"] for e in managed),', 'fixable=bool(managed),')}
SH_MUT = {"reinstall": ('  if [ "$PRE_INSTALLED" = 1 ]; then\n    IRQ_MANAGED=0', '  if false; then\n    IRQ_MANAGED=0')}
if MUT and MUT not in DRV_MUT and MUT not in APT_MUT and MUT not in SH_MUT:
    print("unknown mutation:", MUT)
    sys.exit(2)
echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


def load(path, name, mut=None):
    src = open(path, encoding="utf-8").read()
    if mut:
        if src.count(mut[0]) != 1:
            print("mutation %s: pattern found %d times — the guard is stale" % (MUT, src.count(mut[0])))
            sys.exit(1)
        src = src.replace(mut[0], mut[1])
        print("MUTATION active:", MUT)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod


R = load(DRV_PATH, "irq_managed_under_test", DRV_MUT.get(MUT))
O_GRUB, O_CFG, O_KCMD = R.GRUB_DEFAULTS, R.GRUB_CFG, R.KERNEL_CMDLINE      # baked into R.PROBES
TOK = "megaraid_sas.smp_affinity_enable=0"

# ─── facts: dell-1, 2026-11-25 (state run there: 89 megasas IRQs, smp_affinity_enable=1, GRUB) ─────
UUID = "77e916b9-1a66-488b-b0b1-82307301971c"
KERNELS = ("6.12.116+deb13-rt-amd64", "6.12.116+deb13-amd64", "6.12.96+deb13-amd64")


def facts(**kw):
    f = {"cmdline": ["BOOT_IMAGE=/boot/vmlinuz-%s" % KERNELS[0], "root=UUID=" + UUID, "ro", "quiet"],
         "bootloader": "grub", "grub_linux": [], "grub_default": ["quiet"],
         "grub_cfg": [["linux", "/boot/vmlinuz-" + k, "root=UUID=" + UUID, "ro", "quiet"] for k in KERNELS],
         "kernel_cmdline": [], "loaded": {"megaraid_sas"}, "irq_count": {"megaraid_sas": 89},
         "params": {"megaraid_sas": "1"}}
    f.update(kw)
    return f


print("\n1. decide")
p = R.decide(facts())
verifie(p["status"] == "install" and p["code"] == "apply" and p["tokens"] == [TOK]
        and p["steps"] == ["cmdline", "bootloader"] and p["needs_reboot"],
        "dell-1 today: apply %s, two steps, reboot required" % TOK, p)
withtok = [ln + [TOK] for ln in facts()["grub_cfg"]]
p = R.decide(facts(grub_linux=[TOK], grub_cfg=withtok))
verifie(p["status"] == "pending_reboot", "configured and generated, not booted yet: pending_reboot", p)
p = R.decide(facts(grub_linux=[TOK]))
verifie(p["status"] == "install" and p["code"] == "regenerate" and p["steps"] == ["bootloader"],
        "configured but grub.cfg not regenerated: regenerate only", p)
p = R.decide(facts(grub_linux=["megaraid_sas.smp_affinity_enable=1"]))
verifie(p["code"] == "apply", "the key with ANOTHER value is not « configured »", p)
p = R.decide(facts(grub_default=["quiet", TOK], grub_cfg=withtok))
verifie(p["code"] == "apply", "found only in GRUB_CMDLINE_LINUX_DEFAULT (app/mtl.py's): moved to _LINUX", p)
after = facts(cmdline=facts()["cmdline"] + [TOK], grub_linux=[TOK], grub_cfg=withtok,
              params={"megaraid_sas": "0"})
p = R.decide(after)
verifie(p["status"] == "ok" and p["code"] == "active", "★ after the reboot: parameter reads 0 → ok/active", p)
p = R.decide(facts(cmdline=facts()["cmdline"] + [TOK], grub_linux=[TOK], grub_cfg=withtok))
verifie(p["status"] == "refused" and p["code"] == "not_honoured",
        "on the running line but still managed: refused, not written again", p)
p = R.decide(facts(loaded={"nvme"}, irq_count={"nvme": 33}, params={}))
verifie(p["status"] == "ok" and p["code"] == "nvme_only" and not p["tokens"],
        "★ nvme alone: nothing written (no parameter makes its queues movable)", p)
p = R.decide(facts(loaded={"megaraid_sas", "nvme"}, irq_count={"megaraid_sas": 89, "nvme": 33}))
verifie(p["tokens"] == [TOK] and p["params"]["others"] == "nvme", "megasas + nvme: megasas only, nvme said", p)
p = R.decide(facts(irq_count={}))
verifie(p["status"] == "ok" and p["code"] == "none", "module loaded, no controller interrupts: nothing", p)
p = R.decide(facts(loaded={"smartpqi"}, irq_count={"smartpqi": 16}, params={"smartpqi": "0"}))
verifie(p["tokens"] == ["smartpqi.disable_managed_interrupts=1"], "smartpqi: 0 = managed → =1", p)
p = R.decide(facts(loaded={"smartpqi"}, irq_count={"smartpqi": 16}, params={"smartpqi": "1"}))
verifie(p["status"] == "ok", "smartpqi already =1: ok", p)
p = R.decide(facts(bootloader=None))
verifie(p["status"] == "refused" and p["code"] == "no_bootloader", "no bootloader tool: refused", p)
p = R.decide(facts(params={"megaraid_sas": None}))
verifie(p["status"] == "refused" and p["code"] == "param_unreadable", "unreadable parameter: refused", p)

print("\n2. run in a sandbox (real probes, moved paths)")
TMP = tempfile.mkdtemp(prefix="verif_irqm_")
GRUB = os.path.join(TMP, "etc/default/grub")
GRUBD = GRUB + ".d"
CFG = os.path.join(TMP, "boot/grub/grub.cfg")
KCMD = os.path.join(TMP, "etc/kernel/cmdline")
SYSMOD = os.path.join(TMP, "sys/module")
INTR = os.path.join(TMP, "proc/interrupts")
for d in (GRUBD, os.path.dirname(CFG), os.path.dirname(KCMD), SYSMOD, os.path.dirname(INTR)):
    os.makedirs(d, exist_ok=True)
DELL1_GRUB = """# If you change this file or any /etc/default/grub.d/*.cfg file,
# run 'update-grub' afterwards to update /boot/grub/grub.cfg.

GRUB_DEFAULT=saved
GRUB_TIMEOUT=5
GRUB_DISTRIBUTOR=`( . /etc/os-release && echo ${NAME} )`
GRUB_CMDLINE_LINUX_DEFAULT="quiet"
GRUB_CMDLINE_LINUX=""

#GRUB_DISABLE_OS_PROBER=false
"""
SB = {"cmdline": " ".join(facts()["cmdline"]), "bootloader": "grub", "calls": []}


def set_sysmod(mod, param, value):
    d = os.path.join(SYSMOD, mod, "parameters")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, param), "w") as fh:
        fh.write(value + "\n")


def reset():
    with open(GRUB, "w") as fh:
        fh.write(DELL1_GRUB)
    for x in os.listdir(GRUBD):
        os.remove(os.path.join(GRUBD, x))
    for x in os.listdir(os.path.dirname(GRUB)):
        if ".bak." in x:
            os.remove(os.path.join(os.path.dirname(GRUB), x))
    shutil.rmtree(SYSMOD)
    set_sysmod("megaraid_sas", "smp_affinity_enable", "1")
    set_sysmod("megaraid_sas", "msix_vectors", "-1")
    with open(INTR, "w") as fh:
        fh.write("           CPU0       CPU1\n")
        for n in range(4):
            fh.write(" %d:          0       12   IR-PCI-MSIX-0000:02:00.0  %d-edge      megasas0-msix%d\n" % (43 + n, n, n))
        fh.write(" 120:        5        0   IR-PCI-MSI  eno1\n")
    SB.update(cmdline=" ".join(facts()["cmdline"]), bootloader="grub", calls=[])
    update_grub()


def moved(cmd):
    """A probe with every host path moved into the sandbox (one pass: no path is rewritten twice)."""
    table = {"/etc/default/grub.d/": GRUBD + "/", O_GRUB: GRUB, O_CFG: CFG, O_KCMD: KCMD,
             "/sys/module": SYSMOD, "/proc/interrupts": INTR}
    return re.sub("|".join(re.escape(k) for k in sorted(table, key=len, reverse=True)),
                  lambda m: table[m.group(0)], cmd)


def update_grub():
    """What update-grub does with our part: GRUB_CMDLINE_LINUX in every 10_linux entry (recovery too)."""
    out = subprocess.run(["bash", "-c", moved(R.PROBES["grub_effective"])], capture_output=True, text=True).stdout
    v = dict(x.split("=", 1) for x in out.splitlines() if x[:2] in ("L=", "D="))
    txt = "### BEGIN /etc/grub.d/05_debian_theme ###\n### END /etc/grub.d/05_debian_theme ###\n"
    txt += "### BEGIN /etc/grub.d/10_linux ###\n"
    for k in KERNELS:
        txt += "menuentry 'Debian' {\n\tlinux\t/boot/vmlinuz-%s root=UUID=%s ro %s %s\n}\n" % (k, UUID, v.get("L", ""), v.get("D", ""))
        txt += "menuentry 'Debian (recovery)' {\n\tlinux\t/boot/vmlinuz-%s root=UUID=%s ro single %s\n}\n" % (k, UUID, v.get("L", ""))
    txt += "### END /etc/grub.d/10_linux ###\n### BEGIN /etc/grub.d/40_custom ###\n" \
           "menuentry 'custom' {\n\tlinux\t/boot/vmlinuz-x\n}\n### END /etc/grub.d/40_custom ###\n"
    with open(CFG, "w") as fh:
        fh.write(txt)


def probe(name):
    if name == "cmdline":
        return 0, SB["cmdline"]
    if name == "bootloader":
        return 0, SB["bootloader"]
    cmd = moved(R.PROBES[name])
    rest = re.sub(re.escape(TMP) + r"\S*", "", cmd)
    assert not re.search(r"/(etc|sys|proc|boot)/", rest), cmd             # never a host path
    return R._sh(cmd)


def fake_cmd(argv, check=True, timeout=600, desc=None):
    SB["calls"].append(argv[0])
    if argv[0] == "update-grub":
        update_grub()
    elif argv[0] == "proxmox-boot-tool":
        pass
    else:
        raise AssertionError("unexpected command %r" % argv)
    return 0


R.GRUB_DEFAULTS, R.GRUB_CFG, R.KERNEL_CMDLINE = GRUB, CFG, KCMD
R._run_cmd = fake_cmd
R.os.geteuid = lambda: 0
R.say = lambda msg: None

reset()
f0 = R.gather_facts(probe)
verifie(f0["irq_count"] == {"megaraid_sas": 4} and f0["params"] == {"megaraid_sas": "1"}
        and f0["grub_default"] == ["quiet"] and f0["grub_linux"] == [] and len(f0["grub_cfg"]) == 6,
        "the real probes read the sandbox (IRQ, parameter, sourced GRUB lines, 10_linux only)",
        {k: f0[k] for k in ("irq_count", "params", "grub_linux", "grub_default")})
rc = R.run(probe)
g = open(GRUB).read()
verifie(rc == 10, "run → exit 10 (reboot required, none done)", rc)
verifie('GRUB_CMDLINE_LINUX="%s"' % TOK in g and 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"' in g,
        "★ written into GRUB_CMDLINE_LINUX, GRUB_CMDLINE_LINUX_DEFAULT untouched", g)
verifie(g.replace('GRUB_CMDLINE_LINUX="%s"' % TOK, 'GRUB_CMDLINE_LINUX=""') == DELL1_GRUB,
        "nothing else in the file changed")
verifie(any(".bak.irq_managed." in x for x in os.listdir(os.path.dirname(GRUB))), "backup kept")
verifie(SB["calls"] == ["update-grub"], "update-grub called once", SB["calls"])
verifie(R.decide(R.gather_facts(probe))["status"] == "pending_reboot", "state after run: pending_reboot")
SB["calls"] = []
before = open(GRUB).read()
rc = R.run(probe)
verifie(rc == 10 and open(GRUB).read() == before and SB["calls"] == [],
        "★ idempotent: a second run changes nothing and still says reboot", (rc, SB["calls"]))
SB["cmdline"] += " " + TOK
set_sysmod("megaraid_sas", "smp_affinity_enable", "0")
verifie(R.state(probe)["status"] == "ok" and R.run(probe) == 0, "after the reboot: state ok, run exit 0")

reset()
with open(os.path.join(GRUBD, "50-site.cfg"), "w") as fh:
    fh.write('GRUB_CMDLINE_LINUX="console=ttyS0"\n')
rc = R.run(probe)
verifie(rc == 1, "★ an /etc/default/grub.d override is caught after update-grub (failure, not a fake success)", rc)

reset()
with open(GRUB, "w") as fh:
    fh.write(DELL1_GRUB.replace('GRUB_CMDLINE_LINUX=""', 'GRUB_CMDLINE_LINUX="$EXTRA"'))
verifie(R.run(probe) == 1 and "$EXTRA" in open(GRUB).read(), "a shell expansion in the line: refused, file kept")

reset()
with open(GRUB, "w") as fh:
    fh.write(DELL1_GRUB.replace('GRUB_CMDLINE_LINUX=""\n', ""))
verifie(R.run(probe) == 10 and 'GRUB_CMDLINE_LINUX="%s"' % TOK in open(GRUB).read(), "no GRUB_CMDLINE_LINUX line: added")

reset()
SB["bootloader"] = "systemd-boot"
with open(KCMD, "w") as fh:
    fh.write("root=ZFS=rpool/ROOT/pve-1 boot=zfs\n")
rc = R.run(probe)
verifie(rc == 10 and open(KCMD).read().split() == ["root=ZFS=rpool/ROOT/pve-1", "boot=zfs", TOK]
        and SB["calls"] == ["proxmox-boot-tool"] and "megaraid" not in open(GRUB).read(),
        "systemd-boot: /etc/kernel/cmdline + proxmox-boot-tool refresh, GRUB untouched", (rc, SB["calls"]))

reset()
R.run(probe)
SB["cmdline"] += " " + TOK
SB["calls"] = []
rc = R.revert(probe)
f1 = R.gather_facts(probe)
verifie(rc == 10 and f1["grub_linux"] == [] and not any(TOK in ln for ln in f1["grub_cfg"])
        and 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"' in open(GRUB).read(),
        "revert: taken out of the file AND of grub.cfg, exit 10 (it is still running)", (rc, f1["grub_linux"]))

print("\n3. one table, one bootloader rule")
from app import config, database                                              # noqa: E402
_DB = os.path.join(TMP, "t.db")
config.DB_PATH = _DB
database.DB_PATH = _DB
database._tls.__dict__.pop("conn", None)
database.init_db()
from app import node_aptitude as NA                                           # noqa: E402
if MUT in APT_MUT:
    _src = open(NA.__file__, encoding="utf-8").read()
    if _src.count(APT_MUT[MUT][0]) != 1:
        print("mutation %s: pattern found %d times — the guard is stale" % (MUT, _src.count(APT_MUT[MUT][0])))
        sys.exit(1)
    exec(compile(_src.replace(*APT_MUT[MUT]), NA.__file__, "exec"), NA.__dict__)
    print("MUTATION active:", MUT)
for mod, (prefixes, param, managed, ordinary) in R.DRIVERS.items():
    for pre in prefixes:
        got = NA.MANAGED_IRQ_DRIVERS.get(pre)
        verifie(got == (mod, managed, "%s.%s=%s" % (mod, param, ordinary)),
                "aptitude knows %s* as the driver does (%s)" % (pre, R.token(mod)), got)
    verifie(("/sys/module/%s/parameters/%s" % (mod, param)) in NA.PROBE, "aptitude's probe reads %s" % mod)
verifie(NA.MANAGED_IRQ_DRIVERS.get("nvme", (None,))[0] is None and "nvme" in R.OTHERS,
        "nvme: no parameter on either side")
RULE = "(proxmox-boot-tool status >/dev/null 2>&1 && echo systemd-boot) || (command -v update-grub >/dev/null 2>&1 && echo grub)"
mtl = open(os.path.join(ROOT, "app", "mtl.py"), encoding="utf-8").read()
verifie(RULE in mtl and R.PROBES["bootloader"].startswith(RULE),
        "bootloader chosen by the tool, the same rule as app/mtl.py", R.PROBES["bootloader"])

print("\n4. aptitude replayed: before, after the reboot, after the agent")
ALLOC = {7: 1289, 9: 1289, 11: 1289, 25: 1289, 27: 1289, 1: 1335, 3: 1335}
HITS = [(48, 9, "megasas0-msix5"), (49, 11, "megasas0-msix6"), (56, 25, "megasas0-msix13"), (57, 27, "megasas0-msix14")]


def apt_facts(mod_value, hits):
    return {"window_ms": 2507, "isolated": [], "irqmod": {"megaraid_sas": mod_value, "smartpqi": "-", "mpt3sas": "-"},
            "irqs": [{"irq": i, "cpu": c, "count": 25, "name": n} for i, c, n in hits]}


def irq_line(fa, unmovable=()):
    return next(l for l in NA.evaluate(35, fa, alloc=ALLOC, unmovable=unmovable) if l["key"] == "irq")


l0 = irq_line(apt_facts("1", HITS), unmovable={i for i, _c, _n in HITS})
verifie(l0["level"] == "warn" and l0["action"] == "irq_managed" and l0.get("fixable") is True
        and l0["cmdline"] == [TOK], "★ before: action irq_managed, fixable (the button), %s" % TOK, l0)
l_nv = irq_line({"window_ms": 1000, "isolated": [], "irqmod": {}, "irqs": [{"irq": 90, "cpu": 9, "count": 5, "name": "nvme0q10"}]})
verifie(l_nv["action"] == "irq_managed" and l_nv.get("fixable") is False,
        "★ nvme alone: advice only, no button (the driver would do nothing)", l_nv)

# After the reboot: the queues are ordinary — the agent's own code steers them (fake /proc/irq).
spec = importlib.util.spec_from_file_location("bobi_agent_irqm", os.path.join(ROOT, "node_agent", "agent.py"))
AG = importlib.util.module_from_spec(spec)
spec.loader.exec_module(AG)
AG.PROC_IRQ = os.path.join(TMP, "irq")
AG.ISO_PATH = os.path.join(TMP, "iso", "isolation.json")
os.makedirs(AG.PROC_IRQ)
with open(AG.PROC_IRQ + "/default_smp_affinity", "w") as fh:
    fh.write("ff,ffffffff")
_w0 = AG._cg_write


def kernel_write(path, value, _managed=set()):
    """The kernel refuses an affinity write on a MANAGED irq (EIO)."""
    if path.startswith(AG.PROC_IRQ) and os.path.exists(os.path.join(os.path.dirname(path), "managed")):
        return "[Errno 5] Input/output error"
    return _w0(path, value)


AG._cg_write = kernel_write


def mk_irqs(managed):
    for i, c, n in HITS:
        d = os.path.join(AG.PROC_IRQ, str(i))
        os.makedirs(os.path.join(d, n), exist_ok=True)
        with open(d + "/smp_affinity_list", "w") as fh:
            fh.write(str(c))
        if managed:
            open(d + "/managed", "w").close()
        elif os.path.exists(d + "/managed"):
            os.remove(d + "/managed")


def fired_on(i):
    with open(os.path.join(AG.PROC_IRQ, str(i), "smp_affinity_list")) as fh:
        return min(AG._parse_cpulist(fh.read().strip()))


ONLINE, LENT = set(range(40)), set(ALLOC)
mk_irqs(managed=True)
AG._IRQ_TAKEN.clear()
_m, unmov = AG._iso_irqs(LENT, _online=ONLINE, _isolated=set())
verifie(len(unmov) == 4 and all(fired_on(i) == c for i, c, _n in HITS),
        "before (managed): the agent cannot move them and says so", unmov)
mk_irqs(managed=False)
AG._IRQ_TAKEN.clear()
_m, unmov = AG._iso_irqs(LENT, _online=ONLINE, _isolated=set())
moved_hits = [(i, fired_on(i), n) for i, _c, n in HITS]
verifie(not unmov and all(c not in LENT for _i, c, _n in moved_hits),
        "after the reboot (ordinary): the agent steers every queue off the Dedicated cores", moved_hits)
l1 = irq_line(apt_facts("0", HITS))
verifie(l1["action"] == "irq" and not l1.get("fixable"),
        "after the reboot, before the agent: movable ones, action irq (no boot parameter asked)", l1)
l2 = irq_line(apt_facts("0", moved_hits))
verifie(l2["level"] == "ok" and l2["value"] == 0 and l2["action"] is None,
        "★ after the agent: the irq line of the aptitude sheet is ok", l2)

print("\n5. install-node.sh")
SH = open(os.path.join(ROOT, "node_agent", "install-node.sh"), encoding="utf-8").read()
if MUT in SH_MUT:
    if SH.count(SH_MUT[MUT][0]) != 1:
        print("mutation %s: pattern stale" % MUT)
        sys.exit(1)
    SH = SH.replace(*SH_MUT[MUT])
    print("MUTATION active:", MUT)


def fonction(nom):
    m = re.search(r"^%s\(\) \{\n.*?^\}\n" % nom, SH, flags=re.S | re.M)
    return m.group(0) if m else ""


verifie(re.search(r"^IRQ_MANAGED=auto$", SH, flags=re.M) is not None, "the default is auto")
verifie("--irq-managed) IRQ_MANAGED=1; shift;;" in SH and "--no-irq-managed) IRQ_MANAGED=0; shift;;" in SH,
        "--irq-managed and --no-irq-managed")
i_k, i_r = SH.find('|| pin_boot_kernel "${KERNEL_TARGET:-$(uname -r)}"'), SH.find("\nresolve_irq_managed\n")
verifie(0 < i_k < i_r < SH.find('if [ "$IRQ_MANAGED" = 1 ]; then provision_irq_managed; fi'),
        "applied after the kernel step (its update-grub then covers the RT kernel's entries)")
SHIM = 'ok() { echo "OK $*"; }; warn() { echo "WARN $*"; }\n'


def bash(body, env):
    return subprocess.run(["bash", "-c", SHIM + fonction("resolve_irq_managed") + fonction("provision_irq_managed") + body],
                          env=dict({"PATH": os.environ["PATH"]}, **env), capture_output=True, text=True).stdout


for pre, flag, want in (("0", "auto", "1"), ("1", "auto", "0"), ("1", "1", "1"), ("0", "0", "0")):
    out = bash('resolve_irq_managed; echo "R=$IRQ_MANAGED"', {"PRE_INSTALLED": pre, "IRQ_MANAGED": flag})
    verifie(("R=%s" % want) in out, "reinstall=%s, flag=%s → %s" % (pre, flag, want), out)
SD = os.path.join(TMP, "sd")
os.makedirs(os.path.join(SD, "host_drivers"))
for code, word in (("10", "REBOOT required"), ("0", "nothing to do"), ("3", "FAILED")):
    with open(os.path.join(SD, "host_drivers", "irq_managed.py"), "w") as fh:
        fh.write("import sys\nprint('ARGS', sys.argv[1:])\nsys.exit(%s)\n" % code)
    out = bash("provision_irq_managed; echo END", {"SCRIPT_DIR": SD, "AGENT_DIR": os.path.join(TMP, "none")})
    verifie("ARGS ['run']" in out and word in out and "END" in out,
            "provision_irq_managed runs the SAME script (`run`), exit %s → %s, install goes on" % (code, word), out)

print()
if echecs:
    print("ÉCHEC : %d vérification(s)" % len(echecs))
    sys.exit(1)
print("OK")
