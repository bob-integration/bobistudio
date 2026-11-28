#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Host driver kind `rt_kernel` : put a node on Debian's PREEMPT_RT kernel, with its out-of-tree modules.

Same two callers as the other kinds, so they cannot drift:
  * `node_agent/install-node.sh --rt-kernel` (`provision_rt`)  -> `rt_kernel.py run`
  * the node agent's generic host-driver job layer             -> `rt_kernel.py state` / `run`
Contract (NODE_AGENT.md §4.6): `state` prints ONE JSON document whose status/code/keys/step ids are
CODES translated by the UI; `run` logs ENGLISH lines and exits 0 (done), 10 (reboot required) or
other (failure).

Why (measured on dell-1, 2026-11-11 → 12, same load both nights): an audio console on an isolated
core missed 7 360 blocks in a night on the stock kernel and 0 out of 217 M on linux-image-rt-amd64.
On the stock kernel a softirq (MCE polling) or a kworker (DRM output polling) runs to completion on
the isolated core; on PREEMPT_RT both are threads the console's FIFO thread outranks.

What it does, in this order, and nothing else:
  1. installs `linux-image-rt-amd64` + `linux-headers-rt-amd64` (Debian's own, signed);
  2. builds for that kernel every out-of-tree module the RUNNING kernel uses:
       - DKMS modules (DeckLink…)            `dkms autoinstall -k <rt>`
       - NVIDIA                             DKMS when registered there, else built from /usr/src/nvidia-<ver>
       - ice Kahawai 2.6.6 patched for MTL  rebuilt from the same sources app/mtl.py uses
     NVIDIA refuses PREEMPT_RT unless IGNORE_PREEMPT_RT_PRESENCE=1: set for every build here. It is
     not supported by NVIDIA; it ran a Quadro P5000 (580.105.08) for 20 h without a single Xid;
  3. makes that EXACT kernel the GRUB default (`GRUB_DEFAULT=saved` + `grub-set-default`), so a
     later apt kernel never becomes the boot kernel behind our back — it shows up here instead
     (`modules_missing` for the new RT kernel, or a default that is no longer the newest RT).
The node is never rebooted: exit 10, the operator reboots from the node card.

REFUSED on a node with a DeckLink card (PCI vendor 0xbdbd, 2026-11-25): the Blackmagic drivers
16.4a1 and 16.5a1 crash under PREEMPT_RT as soon as the card is opened (measured on dell-1,
2026-11-23). The node card button used to put such a node on RT without a word; `install-node.sh`
already kept the normal kernel there. Only an explicit `install-node.sh --rt-kernel` still imposes
it, through `run --allow-decklink`.

Only the standard library. `decide` is pure; tests replay facts (tests/verif_rt_kernel_driver.py).
"""
import json
import os
import re
import subprocess
import sys

KIND = "rt_kernel"
SCRIPT_VERSION = "1.2.0"
EXIT_OK, EXIT_REBOOT = 0, 10

RT_PACKAGES = ("linux-image-rt-amd64", "linux-headers-rt-amd64")
ICE_TAG = "Kahawai_2.6.6"                   # same driver app/mtl.py (INSTALL_PATCHED_ICE) installs
ICE_GIT = "https://github.com/intel/ethernet-linux-ice"
MTL_GIT = "https://github.com/OpenVisualCloud/Media-Transport-Library"
NV_ENV = {"IGNORE_PREEMPT_RT_PRESENCE": "1"}
GRUB_DEFAULTS = "/etc/default/grub"
GRUB_CFG = "/boot/grub/grub.cfg"

PROBES = {
    "os": ". /etc/os-release; echo \"$ID $VERSION_ID\"; uname -m; uname -r; uname -v; "
          "command -v apt-get >/dev/null && echo APT || echo NOAPT",
    # one line per kernel: <release> <has vmlinuz> <has headers (build AND source)> <has initrd>
    "kernels": "for d in /lib/modules/*/; do k=$(basename $d); "
               "echo \"$k $([ -e /boot/vmlinuz-$k ] && echo 1 || echo 0) "
               "$([ -e $d/build ] && [ -e $d/source ] && echo 1 || echo 0) "
               "$([ -e /boot/initrd.img-$k ] && echo 1 || echo 0)\"; done",
    # dpkg state of the RT kernel packages: anything but `ii` is an install that stopped half-way
    "rt_pkgs": "dpkg-query -W -f='${Package} ${db:Status-Abbrev}\\n' 'linux-image-*rt-amd64' "
               "'linux-headers-*rt-amd64' 2>/dev/null || true",
    "dkms": "dkms status 2>/dev/null || true",
    "modules": "cut -d' ' -f1 /proc/modules",
    # per kernel: is there an NVIDIA module (nvidia.ko, or nvidia-current.ko from Debian's packages) / which ice
    "kmods": "for d in /lib/modules/*/; do k=$(basename $d); "
             "nv=$(find $d \\( -name 'nvidia.ko*' -o -name 'nvidia-current.ko*' \\) 2>/dev/null | head -1); "
             "ice=$(modinfo -k $k -F version ice 2>/dev/null | head -1); "
             "echo \"$k nvidia=$([ -n \"$nv\" ] && echo 1 || echo 0) ice=${ice:--}\"; done",
    "nvidia_src": "ls -d /usr/src/nvidia-* 2>/dev/null || true",
    "nvidia_ver": "cat /sys/module/nvidia/version 2>/dev/null || true",
    "grub_default": "grep -E '^GRUB_DEFAULT=' %s 2>/dev/null || true" % GRUB_DEFAULTS,
    "grubenv": "grub-editenv list 2>/dev/null || true",
    "grub_cfg": "cat %s 2>/dev/null || true" % GRUB_CFG,
    # DeckLink cards on the bus, whether or not their driver is loaded
    "decklink": "grep -l 0xbdbd /sys/bus/pci/devices/*/vendor 2>/dev/null | wc -l",
    "secureboot": "if [ -d /sys/firmware/efi ]; then mokutil --sb-state 2>/dev/null || "
                  "od -An -t u1 -j4 -N1 /sys/firmware/efi/efivars/SecureBoot-* 2>/dev/null || "
                  "{ ls /sys/firmware/efi/efivars/SecureBoot-* >/dev/null 2>&1 || echo no-efi; }; "
                  "else echo no-efi; fi",          # UEFI without a SecureBoot variable (R620): none
}


def _sh(cmd, timeout=60):
    try:
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + ("" if p.returncode == 0 else (p.stderr or ""))
    except Exception as e:                              # noqa: BLE001
        return 255, str(e)


def live_probe(name):
    return _sh(PROBES[name])


def kver(release):
    """Sort key of a kernel release: 6.12.116+deb13-rt-amd64 → (6, 12, 116, 13)."""
    return tuple(int(x) for x in re.findall(r"\d+", release.split("-")[0]))


def is_rt(release):
    return "-rt-" in release


def grub_entries(cfg):
    """{kernel release: saved_entry id} from grub.cfg, in the `submenu>entry` form grub-set-default
    takes. Only the plain (non-recovery) entries of the "Advanced options" submenu."""
    m = re.search(r"submenu [^\n]*\$menuentry_id_option '(gnulinux-advanced-[^']+)'", cfg)
    if not m:
        return {}
    sub = m.group(1)
    out = {}
    for e in re.finditer(r"\$menuentry_id_option '(gnulinux-(\S+?)-advanced-[^']+)'", cfg):
        out[e.group(2)] = "%s>%s" % (sub, e.group(1))
    return out


def _parse_dkms(text):
    """[(module, version, kernel, status)] from `dkms status` (dkms 3: `name/ver, kernel, arch: status`)."""
    rows = []
    for line in (text or "").splitlines():
        m = re.match(r"^([^/,\s]+)/([^,\s]+),\s*(\S+?),\s*\S+?:\s*(\w+)", line.strip())
        if m:
            rows.append(m.groups())
    return rows


def gather_facts(probe=live_probe):
    f = {}
    _rc, o = probe("os")
    ln = (o or "").splitlines() + [""] * 5
    f["os"] = ln[0].split()[0] if ln[0] else ""
    f["arch"], f["running"] = ln[1].strip(), ln[2].strip()
    f["running_preempt_rt"] = "PREEMPT_RT" in ln[3]
    f["apt"] = ln[4].strip() == "APT"
    kernels = {}
    for line in (probe("kernels")[1] or "").splitlines():
        p = line.split()
        if len(p) >= 3:
            kernels[p[0]] = {"boot": p[1] == "1", "headers": p[2] == "1", "nvidia": False, "ice": "-",
                             "initrd": len(p) < 4 or p[3] == "1"}
    for line in (probe("kmods")[1] or "").splitlines():
        p = line.split()
        if p and p[0] in kernels:
            kv = dict(x.split("=", 1) for x in p[1:] if "=" in x)
            kernels[p[0]]["nvidia"] = kv.get("nvidia") == "1"
            kernels[p[0]]["ice"] = kv.get("ice", "-")
    f["kernels"] = kernels
    # Packages dpkg knows but did not finish configuring (state other than `ii`; `un` = not installed).
    f["rt_broken"] = sorted(p[0] for p in (x.split() for x in (probe("rt_pkgs")[1] or "").splitlines())
                            if len(p) >= 2 and p[1][1:2] in tuple("HUFWt"))
    f["dkms"] = _parse_dkms(probe("dkms")[1])
    f["loaded"] = set((probe("modules")[1] or "").split())
    f["nvidia_src"] = [x.strip() for x in (probe("nvidia_src")[1] or "").splitlines() if x.strip()]
    f["nvidia_ver"] = (probe("nvidia_ver")[1] or "").strip()
    gd = (probe("grub_default")[1] or "").strip()
    f["grub_default"] = gd.split("=", 1)[1].strip().strip('"') if "=" in gd else ""
    env = dict(x.split("=", 1) for x in (probe("grubenv")[1] or "").splitlines() if "=" in x)
    f["saved_entry"] = env.get("saved_entry", "")
    f["entries"] = grub_entries(probe("grub_cfg")[1] or "")
    try:
        f["decklink"] = int((probe("decklink")[1] or "0").strip() or 0)
    except ValueError:
        f["decklink"] = 0
    sb = (probe("secureboot")[1] or "").lower()
    byte = re.search(r"^\s*([01])\s*$", sb, re.M)       # efivars byte, when mokutil is missing
    f["secure_boot"] = True if ("secureboot enabled" in sb or (byte and byte.group(1) == "1")) else (
        False if ("disabled" in sb or "no-efi" in sb or "not supported" in sb or byte) else None)
    return f


def _plan(status, code, **kw):
    p = {"status": status, "code": code, "params": {}, "target": None, "steps": [], "needs": [],
         "needs_reboot": False}
    p.update(kw)
    return p


def needed_modules(f, target):
    """What the running kernel uses that `target` lacks: 'dkms:<name>', 'nvidia', 'ice'."""
    run_k, k = f["running"], f["kernels"].get(target) or {}
    need = []
    built = {(m, kk) for m, _v, kk, st in f["dkms"] if st == "installed"}
    dkms_mods = sorted({m for m, _v, kk, st in f["dkms"] if kk == run_k and st == "installed"})
    for m in dkms_mods:
        if (m, target) not in built:
            need.append("dkms:" + m)
    # Debian's own packages register the driver in DKMS as `nvidia-current` (module nvidia-current.ko,
    # aliased nvidia): DKMS rebuilds it, building it a second time by hand would collide.
    if "nvidia" in f["loaded"] and not k.get("nvidia") and not any(n.startswith("dkms:nvidia") for n in need):
        need.append("nvidia")
    if ICE_TAG in ((f["kernels"].get(run_k) or {}).get("ice") or "") and ICE_TAG not in (k.get("ice") or ""):
        need.append("ice")
    return need


def decide(f, allow_decklink=False):
    plan = _decide(f)
    # Last, so it only ever stops a CHANGE: a node already running RT keeps reading « ok ».
    if plan["status"] == "install" and f.get("decklink") and not allow_decklink:
        return _plan("refused", "decklink", target=plan.get("target"),
                     params=dict(plan.get("params") or {}, cards=f["decklink"]))
    return plan


def _decide(f):
    if not f["apt"] or f["os"] != "debian":
        return _plan("refused", "not_debian", params={"os": f["os"] or "?"})
    if f["arch"] != "x86_64":
        return _plan("refused", "arch", params={"arch": f["arch"]})
    rts = sorted((k for k, v in f["kernels"].items() if is_rt(k) and v["boot"]), key=kver)
    if not rts:
        if f["secure_boot"] and ("nvidia" in f["loaded"] or any(m for m, _v, kk, _s in f["dkms"]
                                                                if kk == f["running"])):
            return _plan("refused", "secure_boot_modules")
        return _plan("install", "rt_not_installed", needs_reboot=True,
                     steps=["apt_update", "install_kernel", "modules", "grub_default"])
    target = rts[-1]
    params = {"target": target}
    # A kernel install that stopped half-way (postinst failed, so no initramfs and no grub update):
    # finish it first. Seen on r620-2 (2026-11-15) when DKMS NVIDIA refused PREEMPT_RT in the postinst;
    # the old plan went straight to `modules` + `grub_default` and pinned a kernel without initrd.
    if f.get("rt_broken") or not f["kernels"][target].get("initrd", True):
        return _plan("install", "rt_incomplete", target=target,
                     params=dict(params, packages=", ".join(f.get("rt_broken") or []) or "-"),
                     needs_reboot=f["running"] != target,
                     steps=["install_kernel", "modules", "grub_default"])
    need = needed_modules(f, target)
    if need and f["secure_boot"]:
        return _plan("refused", "secure_boot_modules", target=target, params=params, needs=need)
    entry = f["entries"].get(target)
    default_ok = f["grub_default"] == "saved" and entry is not None and f["saved_entry"] == entry
    if need:
        return _plan("install", "modules_missing", target=target, params=dict(params, needs=",".join(need)),
                     needs=need, needs_reboot=f["running"] != target, steps=["modules", "grub_default"])
    if entry is None:
        return _plan("refused", "no_grub_entry", target=target, params=params)
    if not default_ok:
        return _plan("install", "set_default", target=target, params=params,
                     needs_reboot=f["running"] != target, steps=["grub_default"])
    if f["running"] != target:
        return _plan("pending_reboot", "pending_reboot", target=target, params=params)
    return _plan("ok", "ok", target=target, params=params)


def rows_for_ui(f, plan):
    t = plan.get("target")
    rows = [{"key": "running", "value": "%s%s" % (f["running"], " (PREEMPT_RT)" if f["running_preempt_rt"] else "")},
            {"key": "target", "value": t or "-"},
            {"key": "boot_default", "value": next((k for k, e in f["entries"].items()
                                                   if e == f["saved_entry"]), None)
             or ("GRUB_DEFAULT=%s" % (f["grub_default"] or "?"))}]
    if plan.get("needs"):
        rows.append({"key": "modules", "value": ", ".join(plan["needs"])})
    rows.append({"key": "secure_boot", "value": {True: "enabled", False: "disabled", None: "?"}[f["secure_boot"]]})
    return rows


def state(probe=live_probe):
    f = gather_facts(probe)
    plan = decide(f)
    f2 = dict(f, loaded=sorted(f["loaded"] & {"nvidia", "ice", "blackmagic", "blackmagic_io"}),
              dkms=[list(x) for x in f["dkms"]])
    return {"kind": KIND, "version": SCRIPT_VERSION, "status": plan["status"], "plan": plan,
            "rows": rows_for_ui(f, plan), "can_run": plan["status"] == "install", "facts": f2}


# ─── run ───────────────────────────────────────────────────────────────────────────────────────────
class StepError(Exception):
    pass


def say(msg):
    print(msg, flush=True)


def _apt_opts():
    return [x for x in os.environ.get("BOBI_APT_OPTS", "").split() if x]


def _run_cmd(argv, check=True, env=None, timeout=3600, desc=None, cwd=None):
    say("$ " + " ".join(argv))
    e = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    if env:
        e.update(env)
    p = subprocess.run(argv, env=e, timeout=timeout, cwd=cwd, stdout=sys.stdout, stderr=subprocess.STDOUT)
    if check and p.returncode != 0:
        raise StepError("%s failed (rc=%d)" % (desc or argv[0], p.returncode))
    return p.returncode


def _apt(*args, check=True, env=None):
    return _run_cmd(["apt-get"] + _apt_opts() + list(args), check=check, env=env, desc="apt-get " + args[0])


def step_apt_update(ctx):
    _apt("update", "-qq", check=False)


def step_install_kernel(ctx):
    # Finish any half-configured package first, with the same environment (its postinst is the one
    # that failed), then install: apt refuses to go on while dpkg has unconfigured packages.
    _run_cmd(["dpkg", "--configure", "-a"], env=NV_ENV, desc="dpkg --configure -a")
    # The kernel's postinst runs `dkms autoinstall` for it: without NV_ENV a DKMS-registered NVIDIA
    # refuses PREEMPT_RT, dpkg fails and the whole install stops here (r620-2, 2026-11-15).
    _apt("install", "-y", "-qq", *RT_PACKAGES, env=NV_ENV)
    f = gather_facts(ctx["probe"])
    rts = sorted((k for k, v in f["kernels"].items() if is_rt(k) and v["boot"]), key=kver)
    if not rts:
        raise StepError("linux-image-rt-amd64 installed but no RT kernel in /boot")
    ctx["f"], ctx["target"] = f, rts[-1]
    say("RT kernel: %s" % ctx["target"])


def _build_nvidia(ctx, target):
    ver = ctx["f"]["nvidia_ver"]
    src = "/usr/src/nvidia-%s" % ver
    if not ver or not os.path.isdir(src):
        raise StepError("the running NVIDIA driver %s is neither in DKMS nor in %s: reinstall it from the "
                        "node card (NVIDIA driver), then run this again" % (ver or "?", src))
    work = "/var/tmp/bobi-nvidia-rt"
    _run_cmd(["rm", "-rf", work])
    _run_cmd(["cp", "-a", src, work])
    ksrc = os.path.realpath("/lib/modules/%s/source" % target)
    _run_cmd(["make", "-j%d" % (os.cpu_count() or 4), "SYSSRC=" + ksrc, "SYSOUT=/lib/modules/%s/build" % target,
              "modules"], env=NV_ENV, cwd=work, desc="NVIDIA module build for " + target)
    dest = "/lib/modules/%s/updates/nvidia" % target
    os.makedirs(dest, exist_ok=True)
    kos = [x for x in os.listdir(work) if x.endswith(".ko")]
    if "nvidia.ko" not in kos:
        raise StepError("nvidia.ko was not produced")
    for ko in kos:
        _run_cmd(["install", "-m", "0644", os.path.join(work, ko), os.path.join(dest, ko)])
    _run_cmd(["rm", "-rf", work])


def _build_ice(ctx, target):
    work = "/var/tmp/bobi-ice-rt"
    _run_cmd(["rm", "-rf", work])
    os.makedirs(work)
    _run_cmd(["git", "clone", "-q", "--depth", "1", MTL_GIT, "MTL"], cwd=work, desc="clone MTL")
    _run_cmd(["git", "clone", "-q", "--depth", "1", "-b", "v2.6.6", ICE_GIT, "ice"], cwd=work, desc="clone ice")
    pdir = os.path.join(work, "MTL/patches/ice_drv/2.6.6")
    for p in sorted(os.listdir(pdir)):
        if p.endswith(".patch"):
            _run_cmd(["bash", "-c", "patch -p1 --fuzz=3 < %s" % os.path.join(pdir, p)],
                     cwd=os.path.join(work, "ice"), desc="patch " + p)
    ksrc = os.path.realpath("/lib/modules/%s/source" % target)
    srcdir = os.path.join(work, "ice/src")
    _run_cmd(["make", "-j%d" % (os.cpu_count() or 4), "KSRC=" + ksrc, "KOBJ=/lib/modules/%s/build" % target],
             cwd=srcdir, desc="ice build for " + target)
    rc, v = _sh("modinfo -F version %s/ice.ko" % srcdir)
    if ICE_TAG not in (v or ""):
        raise StepError("ice.ko built but not %s (%s)" % (ICE_TAG, (v or "").strip()))
    os.makedirs("/lib/modules/%s/updates" % target, exist_ok=True)
    _run_cmd(["install", "-m", "0644", os.path.join(srcdir, "ice.ko"), "/lib/modules/%s/updates/ice.ko" % target])
    _run_cmd(["rm", "-rf", work])


def _lift_rt_exclusion(module, f):
    """Debian's NVIDIA package declares `BUILD_EXCLUSIVE_CONFIG="!CONFIG_PREEMPT_RT …"`, so DKMS SKIPS it
    on an RT kernel (r620-2, 550.163.01, 2026-11-14) without an error. DKMS reads /etc/dkms/<module>.conf
    after the module's own dkms.conf: the override clears that one directive and nothing else."""
    ver = next((v for m, v, kk, st in f["dkms"] if m == module and kk == f["running"]), None)
    conf = "/usr/src/%s-%s/dkms.conf" % (module, ver)
    try:
        txt = open(conf).read()
    except OSError:
        return
    if not re.search(r'^BUILD_EXCLUSIVE_CONFIG=.*PREEMPT_RT', txt, re.M):
        return
    path = "/etc/dkms/%s.conf" % module
    with open(path + ".tmp", "w") as fh:
        fh.write("# Written by the bobi host driver rt_kernel: the packaged dkms.conf excludes PREEMPT_RT\n"
                 "# kernels; the build is run with IGNORE_PREEMPT_RT_PRESENCE=1 instead (see rt_kernel.py).\n"
                 'BUILD_EXCLUSIVE_CONFIG=""\n')
    os.replace(path + ".tmp", path)
    say("%s: PREEMPT_RT exclusion of the packaged dkms.conf lifted (%s)" % (module, path))


def step_modules(ctx):
    f, target = ctx["f"], ctx["target"]
    if not f["kernels"][target]["headers"]:
        _apt("install", "-y", "-qq", "linux-headers-" + target)
    need = needed_modules(f, target)
    if not need:
        say("no out-of-tree module to build for %s" % target)
        return
    say("to build for %s: %s" % (target, ", ".join(need)))
    for n in need:
        if n.startswith("dkms:nvidia"):
            _lift_rt_exclusion(n[len("dkms:"):], f)
    if any(n.startswith("dkms:") for n in need):
        _run_cmd(["dkms", "autoinstall", "-k", target], env=NV_ENV, check=False)
    if "nvidia" in need:
        _build_nvidia(ctx, target)
    if "ice" in need:
        _build_ice(ctx, target)
    _run_cmd(["depmod", target])
    f2 = gather_facts(ctx["probe"])
    left = needed_modules(f2, target)
    if left:
        raise StepError("still missing for %s after the build: %s (see the log above)" % (target, ", ".join(left)))
    ctx["f"] = f2
    if _sh("command -v update-initramfs")[1].strip():
        mode = "-u" if os.path.exists("/boot/initrd.img-%s" % target) else "-c"
        _run_cmd(["update-initramfs", mode, "-k", target], check=False)


def step_grub_default(ctx):
    _pin(ctx["target"], ctx["probe"])


def _pin(target, probe):
    """GRUB_DEFAULT=saved + saved_entry = the exact entry of `target`, verified."""
    if not os.path.exists("/boot/initrd.img-%s" % target) and target != os.uname().release:
        # Pinning it would leave a node that cannot find its root disk at the next reboot.
        raise StepError("%s has no initramfs (/boot/initrd.img-%s): not made the boot default" % (target, target))
    rc, cur = _sh("grep -E '^GRUB_DEFAULT=' %s" % GRUB_DEFAULTS)
    if cur.strip() != "GRUB_DEFAULT=saved":
        say("GRUB_DEFAULT was %r: set to saved" % cur.strip())
        if cur.strip():
            _run_cmd(["sed", "-i", "s/^GRUB_DEFAULT=.*/GRUB_DEFAULT=saved/", GRUB_DEFAULTS])
        else:
            _run_cmd(["bash", "-c", "echo GRUB_DEFAULT=saved >> %s" % GRUB_DEFAULTS])
    _run_cmd(["update-grub"], desc="update-grub")
    entry = gather_facts(probe)["entries"].get(target)
    if not entry:
        raise StepError("no GRUB entry for %s after update-grub" % target)
    _run_cmd(["grub-set-default", entry])
    if _sh("grub-editenv list")[1].find("saved_entry=" + entry) < 0:
        raise StepError("grub-set-default did not record %s" % entry)
    say("boot default: %s" % target)


STEPS = {"apt_update": step_apt_update, "install_kernel": step_install_kernel, "modules": step_modules,
         "grub_default": step_grub_default}


def run(probe=live_probe, allow_decklink=False):
    say("== bobi rt_kernel host driver %s ==" % SCRIPT_VERSION)
    if os.geteuid() != 0:
        say("ERROR: must run as root")
        return 1
    f = gather_facts(probe)
    plan = decide(f, allow_decklink=allow_decklink)
    say("state: %s (%s) %s" % (plan["status"], plan["code"], json.dumps(plan["params"], sort_keys=True)))
    if plan["status"] == "refused":
        say("REFUSED: %s - nothing was changed" % plan["code"])
        return 1
    if plan["status"] == "pending_reboot":
        say("nothing to do: %s is the boot default, reboot the node to run it" % plan["target"])
        return EXIT_REBOOT
    if plan["status"] != "install":
        say("nothing to do (%s)" % plan["status"])
        return EXIT_OK
    ctx = {"f": f, "target": plan["target"], "probe": probe}
    # FIRST pin what runs now. apt may add kernels ahead of it in GRUB (on r620-2 the new RT kernel,
    # still without NVIDIA, became entry 0 under GRUB_DEFAULT=0): a step failing later must leave a node
    # that boots what it booted before. grub_default moves the pin to the RT kernel once all is built.
    try:
        _pin(f["running"], probe)
    except StepError as e:
        say("FAILED before any change: %s" % e)
        return 1
    steps = plan["steps"]
    for i, name in enumerate(steps, 1):
        say("")
        say("[step %d/%d] %s" % (i, len(steps), name))
        try:
            STEPS[name](ctx)
        except StepError as e:
            say("FAILED at step '%s': %s" % (name, e))
            return 1
        except subprocess.TimeoutExpired:
            say("FAILED at step '%s': timeout" % name)
            return 1
    say("")
    if ctx["f"]["running"] != ctx["target"]:
        say("DONE - reboot the node to run %s" % ctx["target"])
        return EXIT_REBOOT
    say("DONE")
    return EXIT_OK


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "state"
    if cmd == "state":
        print(json.dumps(state(), sort_keys=True))
        return 0
    if cmd == "run":
        return run(allow_decklink="--allow-decklink" in argv[2:])
    print("usage: rt_kernel.py state|run [--allow-decklink]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
