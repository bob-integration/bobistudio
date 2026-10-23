#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Host driver kind `nvidia` : detect, decide, install/upgrade the NVIDIA driver of a node.

ONE implementation shared by the two callers, so they cannot drift:
  * `node_agent/install-node.sh` (`provision_gpu`)  -> `nvidia.py run`
  * the node agent's generic host-driver job layer   -> `nvidia.py state` / `nvidia.py run`

Contract of a host-driver kind (see NODE_AGENT.md, "Host drivers"):
  state   print ONE JSON document on stdout:
            {kind, version, status, rows:[{key,value}], plan:{status,code,params,steps}, can_run}
          `code`/`params`/`key`/step ids are CODES, translated by the UI (never a sentence).
  run     do the work; progress goes to stdout/stderr as human-readable ENGLISH lines (the agent
          captures them as the job log). Exit status:
            0   done, nothing pending          10  done, a REBOOT is required
            11  done, a full POWER-OFF is required (not used by this kind)    other  failure

Only the standard library. The decision logic (`decide`) is pure: it takes the facts gathered by
`gather_facts` (which reads the host through one injectable `probe`), so tests replay real probes.

Who decides what (decisions taken with the operator, do not reopen):
  Turing and newer (compute capability >= 7.5)  -> NVIDIA apt repo (Debian), OPEN kernel modules
  Maxwell / Pascal / Volta                      -> NVIDIA .run installer, branch 580 (last to support them), --dkms
  Kepler and older                              -> refused
"""
import json
import os
import re
import subprocess
import sys
import time

KIND = "nvidia"
SCRIPT_VERSION = "1.0.0"

# ─── THE minimum driver ───────────────────────────────────────────────────────────────────────────
# The GPU runtime image (plugins/_compute_gpu_runtime/Dockerfile) pins cupy-cuda12x==13.4.1, which
# embeds the CUDA 12.8 runtime, which needs a driver >= 570. This is the ONE place that number lives;
# tests/verif_nvidia_driver.py fails if the Dockerfile pin moves without this being revisited.
MIN_DRIVER_MAJOR = 570
CUPY_PIN_COUPLED = "cupy-cuda12x==13.4.1"       # what MIN_DRIVER_MAJOR was derived from

# Branch >= 590 drops Maxwell, Pascal and Volta (the driver loads and finds no device).
LEGACY_DRIVER_CEILING_MAJOR = 590

# .run installer for the legacy generations: 580 is the last branch that supports them. 580.105.08 is
# the exact build running on the reference Pascal node, so "known good" is a fact, not a guess.
RUN_VERSION = "580.105.08"
RUN_FILENAME = "NVIDIA-Linux-x86_64-%s.run" % RUN_VERSION
RUN_URL = "https://download.nvidia.com/XFree86/Linux-x86_64/%s/%s" % (RUN_VERSION, RUN_FILENAME)

# NVIDIA's CUDA apt repository (open kernel modules, branch >= 590 only). The release segment is READ
# from /etc/os-release (never a hardcoded codename or number) : whether NVIDIA publishes that release is
# answered by the `net_repo` probe (HTTP 200 on the keyring), and a 404 falls back to the .run installer.
REPO_ROOT = "https://developer.download.nvidia.com/compute/cuda/repos"
REPO_KEYRING_FILE = "cuda-keyring_1.1-1_all.deb"
REPO_KEYRING_PKG = "cuda-keyring"
# `nvidia-open` = nvidia-kernel-open-dkms + nvidia-driver + nvidia-driver-cuda (nvidia-smi) + ...
# Mandatory first, optional after (installed only when apt knows them, cf. apt_knows).
REPO_PACKAGES_REQUIRED = ["nvidia-open"]
REPO_PACKAGES_OPTIONAL = ["libcuda1"]


def repo_base(os_id, version_id):
    """NVIDIA repo URL for this distribution release, e.g. .../repos/debian13/x86_64."""
    return "%s/%s%s/x86_64" % (REPO_ROOT, os_id, version_id)

# Where the operator-supplied (UI upload) or bundle-supplied installer lives. The agent's upload
# endpoint and install-node.sh (vendor/nvidia) both drop files here.
DRIVER_DIR = os.environ.get("BOBI_NVIDIA_DIR", "/var/lib/bobi-node-agent/drivers/nvidia")

# Container toolkit (the Debian archive does not carry it).
CTK_KEY_URL = "https://nvidia.github.io/libnvidia-container/gpgkey"
CTK_LIST_URL = "https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list"
CTK_KEYRING = "/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
CTK_LIST = "/etc/apt/sources.list.d/nvidia-container-toolkit.list"

NOUVEAU_BLACKLIST = "/etc/modprobe.d/bobi-blacklist-nouveau.conf"

EXIT_OK, EXIT_REBOOT, EXIT_SHUTDOWN = 0, 10, 11

# ─── GPU generations ──────────────────────────────────────────────────────────────────────────────
# class: "open" = Turing+ (apt repo, open modules) ; "legacy" = .run 580 ; "refused".
ARCH_CLASS = {
    "fermi": "refused", "kepler": "refused",
    "maxwell": "legacy", "pascal": "legacy", "volta": "legacy",
    "turing": "open", "ampere": "open", "ada": "open", "hopper": "open", "blackwell": "open",
}

# lspci prints the chip codename (pci.ids) : "NVIDIA Corporation GP104GL [Quadro P5000]".
_CODENAME_RE = re.compile(r"\b(GT\d{3}|G\d{2}|GF\d{3}|GK\d{3}|GM\d{3}|GP\d{3}|GV\d{3}|TU\d{3}|GA\d{3}|"
                          r"GH\d{3}|AD\d{3}|GB\d{2,3})[A-Z]{0,2}\b")
_CODENAME_ARCH = (("GT", "fermi"), ("GF", "fermi"), ("GK", "kepler"), ("GM", "maxwell"),
                  ("GP", "pascal"), ("GV", "volta"), ("TU", "turing"), ("GA", "ampere"),
                  ("GH", "hopper"), ("AD", "ada"), ("GB", "blackwell"))

# PCI device id ranges : FALLBACK only, for a card too recent for the host's pci.ids (shown as
# "Device 2bb1"). Approximate by nature ; the codename and nvidia-smi's compute_cap come first.
_ID_RANGES = (
    (0x15f0, 0x15ff, "pascal"),     # GP100 (before the Maxwell range that contains it)
    (0x2900, 0x2fff, "blackwell"), (0x2680, 0x28ff, "ada"), (0x2300, 0x23ff, "hopper"),
    (0x2000, 0x22ff, "ampere"), (0x2400, 0x25ff, "ampere"),
    (0x2180, 0x21ff, "turing"), (0x1e00, 0x1fff, "turing"),
    (0x1d80, 0x1dff, "volta"),
    (0x1b00, 0x1d7f, "pascal"),
    (0x1340, 0x17ff, "maxwell"),
    (0x1000, 0x133f, "kepler"), (0x0fc0, 0x0fff, "kepler"),
    (0x0600, 0x0fbf, "fermi"),
)


def arch_from_codename(text):
    m = _CODENAME_RE.search(text or "")
    if not m:
        return None
    name = m.group(1)
    for prefix, arch in _CODENAME_ARCH:
        if name.startswith(prefix):
            return arch
    return "fermi" if re.match(r"^G\d{2}$", name) else None


def arch_from_pci_id(dev_id):
    for lo, hi, arch in _ID_RANGES:
        if lo <= dev_id <= hi:
            return arch
    return None


def arch_from_compute_cap(cc):
    try:
        major, minor = [int(x) for x in str(cc).strip().split(".")[:2]]
    except (ValueError, TypeError):
        return None
    if major >= 10:
        return "blackwell"
    if major == 9:
        return "hopper"
    if major == 8:
        return "ada" if minor >= 9 else "ampere"
    if (major, minor) >= (7, 5):
        return "turing"
    if major == 7:
        return "volta"
    if major == 6:
        return "pascal"
    if major == 5:
        return "maxwell"
    if major == 3:
        return "kepler"
    if major == 2:
        return "fermi"
    return None


def vparse(v):
    """'580.105.08' -> (580, 105, 8) ; None/garbage -> None."""
    try:
        return tuple(int(x) for x in str(v).strip().split(".") if x != "")
    except ValueError:
        return None


def vmajor(v):
    t = vparse(v)
    return t[0] if t else None


# ─── apt components (port of the careful logic of the old provision_gpu) ─────────────────────────
def component_present(sources_text, comp):
    """True when `comp` is a component of an ACTIVE apt source line.

    Only ACTIVE lines count: the Debian installer leaves a COMMENTED
    `#deb cdrom:[...] trixie contrib main non-free-firmware` at the top of sources.list, and a naive
    grep saw "contrib" there and concluded it was enabled. `non-free` must not match inside
    `non-free-firmware` (the dash is neither a space, a comma nor an end of line)."""
    pat = re.compile(r"(^|[ ,])%s([ ,]|$)" % re.escape(comp))
    for line in (sources_text or "").splitlines():
        if re.match(r"^\s*#", line):
            continue
        if pat.search(line):
            return True
    return False


APT_COMPONENTS = ("contrib", "non-free", "non-free-firmware")


# ─── Package sets ──────────────────────────────────────────────────────────────────────────────────
# What "the NVIDIA driver" is, as installed packages (Debian's AND NVIDIA repo's). Never the
# container toolkit (nvidia-container-*, libnvidia-container*), and never nouveau's firmware.
_DRIVER_PKG_RE = re.compile(
    r"^(nvidia-(?!container)[a-z0-9.+-]*|libnvidia-(?!container)[a-z0-9.+-]*|libcuda1|libcudadebugger1|"
    r"libnvcuvid1|libegl-nvidia0|libgles-nvidia[12]|libglx-nvidia0|libgl1-nvidia-glvnd-glx|"
    r"xserver-xorg-video-nvidia|glx-alternative-nvidia|firmware-nvidia-gsp|cuda-drivers[a-z0-9.-]*)(:\w+)?$")
_REPO_MARKERS = ("nvidia-open", "nvidia-kernel-open-dkms", "cuda-drivers", "nvidia-driver-cuda")


def driver_packages(installed):
    """Installed packages that belong to the driver stack (what a purge must remove)."""
    return sorted(p for p in installed if _DRIVER_PKG_RE.match(p))


# ─── facts ─────────────────────────────────────────────────────────────────────────────────────────
# Every probe is a READ-ONLY command, named, so a fixture can replay it.
PROBES = {
    "pci": "for d in /sys/bus/pci/devices/*; do echo \"$(basename $d) $(cat $d/vendor) $(cat $d/device) "
           "$(cat $d/class)\"; done 2>/dev/null",
    "lspci": "lspci -nn -d 10de: 2>/dev/null || true",
    "smi": "nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv,noheader,nounits 2>&1",
    "procver": "cat /proc/driver/nvidia/version 2>/dev/null || true",
    "dpkg": "dpkg-query -W -f='${Package} ${Version} ${db:Status-Abbrev}\\n' 2>/dev/null || true",
    "run_installer": "nvidia-installer --version 2>/dev/null | head -3; "
                     "test -x /usr/bin/nvidia-uninstall && echo HAS_UNINSTALL || true",
    "secureboot": "if [ -d /sys/firmware/efi ]; then mokutil --sb-state 2>&1 || "
                  "od -An -t u1 -j4 -N1 /sys/firmware/efi/efivars/SecureBoot-* 2>&1; else echo no-efi; fi",
    "modules": "cut -d' ' -f1 /proc/modules",
    "libcuda": "ldconfig -p 2>/dev/null | grep -E 'libcuda\\.so\\.1' || true",
    "ctk": "command -v nvidia-ctk || true",
    "docker": "docker info --format '{{json .Runtimes}}' 2>&1",
    "os": ". /etc/os-release; echo \"$ID $VERSION_ID\"; uname -m; uname -r; "
          "command -v apt-get >/dev/null && echo APT || echo NOAPT",
    "headers": "test -d \"/lib/modules/$(uname -r)/build\" && echo yes || echo no",
    "runfiles": "ls -1 %s/*.run 2>/dev/null || true" % DRIVER_DIR,
    "net_repo": ". /etc/os-release; curl -sI -m 6 -o /dev/null -w '%{http_code}' " + REPO_ROOT +
                "/${ID}${VERSION_ID}/x86_64/" + REPO_KEYRING_FILE + " 2>/dev/null || true",
    "net_run": "curl -sI -m 6 -o /dev/null -w '%{http_code}' " + RUN_URL + " 2>/dev/null || true",
}


def _sh(cmd, timeout=60):
    try:
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + ("" if p.returncode == 0 else (p.stderr or ""))
    except Exception as e:                              # noqa: BLE001
        return 255, str(e)


def live_probe(name):
    return _sh(PROBES[name], timeout=20 if name.startswith("net_") else 60)


def _parse_secure_boot(out):
    o = (out or "").lower()
    if "no-efi" in o:
        return False
    if "doesn't support" in o or "does not support" in o:
        return False
    if "secureboot enabled" in o or "secure boot enabled" in o:
        return True
    if "secureboot disabled" in o or "secure boot disabled" in o:
        return False
    m = re.search(r"^\s*([01])\s*$", o, re.M)         # efivars byte (mokutil missing)
    if m:
        return m.group(1) == "1"
    return None                                        # undeterminable : the plan warns, never guesses


def gather_facts(probe=live_probe):
    """Read the host through `probe(name) -> (rc, stdout)`. Works with NO driver installed."""
    def out(name):
        rc, o = probe(name)
        return o or ""

    # 1. GPUs from sysfs (display / 3D controller classes of vendor 0x10de), named by lspci.
    names = {}
    for line in out("lspci").splitlines():
        m = re.search(r"\[10de:([0-9a-f]{4})\]", line)
        if m:
            nm = re.sub(r"^\S+\s+[^:]+:\s*", "", line)                # drop "03:00.0 VGA ...:"
            nm = re.sub(r"\s*\[10de:[0-9a-f]{4}\].*$", "", nm).replace("NVIDIA Corporation ", "").strip()
            names[int(m.group(1), 16)] = nm
    gpus = []
    for line in out("pci").splitlines():
        p = line.split()
        if len(p) < 4:
            continue
        try:
            vendor, dev, cls = int(p[1], 16), int(p[2], 16), int(p[3], 16)
        except ValueError:
            continue
        if vendor != 0x10de or (cls >> 16) != 0x03:                  # display / 3D controllers only
            continue
        nm = names.get(dev, "")
        arch = arch_from_codename(nm) or arch_from_pci_id(dev)
        shown = (re.search(r"\[([^\]]+)\]", nm).group(1) if re.search(r"\[([^\]]+)\]", nm) else nm)
        gpus.append({"slot": p[0], "pci_id": "10de:%04x" % dev, "name": shown or ("Device %04x" % dev),
                     "chip": nm,
                     "arch": arch, "arch_source": "codename" if arch_from_codename(nm) else
                     ("pci-id-range" if arch else "unknown"), "compute_cap": None})

    # 2. nvidia-smi (only when a driver works) : compute_cap is authoritative.
    smi_rows, smi_rc = [], False
    rc, smi = probe("smi")
    if rc == 0 and smi and "failed" not in smi.lower() and "not found" not in smi.lower():
        for line in smi.splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 3 and vparse(parts[1]):
                smi_rows.append(parts)
        smi_rc = bool(smi_rows)
    if smi_rc and len(smi_rows) == len(gpus):
        for g, row in zip(gpus, smi_rows):
            g["compute_cap"] = row[2]
            a = arch_from_compute_cap(row[2])
            if a:
                g["arch"], g["arch_source"] = a, "compute_cap"
    running = smi_rows[0][1] if smi_rc else None
    if not running:
        m = re.search(r"Kernel Module\s+(\d+\.\d+(?:\.\d+)?)", out("procver"))
        running = m.group(1) if m else None

    # 3. packages
    pk = {}
    for line in out("dpkg").splitlines():
        p = line.split()
        if len(p) >= 3 and p[2].startswith("ii"):
            pk[p[0]] = p[1]
    drv_pkgs = driver_packages(pk)
    ri = out("run_installer")
    m = re.search(r"version\s+(\d+\.\d+(?:\.\d+)?)", ri)
    run_version = m.group(1) if m else None
    has_uninstall = "HAS_UNINSTALL" in ri
    if any(p in pk for p in _REPO_MARKERS):
        origin = "nvidia-repo"
        installed = pk.get("nvidia-open") or pk.get("nvidia-kernel-open-dkms") or pk.get("cuda-drivers")
    elif any(p.startswith("nvidia-kernel") or p == "nvidia-driver" for p in pk):
        origin = "debian"
        installed = pk.get("nvidia-kernel-dkms") or pk.get("nvidia-driver")
    elif has_uninstall:
        origin, installed = "run", run_version or running
    elif running:
        origin, installed = "unknown", running
    else:
        origin, installed = "none", None
    installed = re.sub(r"^\d+:", "", installed or "")                  # strip an epoch
    installed = (re.match(r"\d+(\.\d+)*", installed).group(0) if re.match(r"\d+", installed) else None)

    modules = set(out("modules").split())
    docker = out("docker")
    osl = out("os").split("\n")
    os_id, os_ver = (osl[0].split() + ["", ""])[:2] if osl else ("", "")
    http = lambda n: out(n).strip() in ("200", "301", "302")           # noqa: E731
    runfiles = [os.path.basename(x) for x in out("runfiles").split() if x.strip()]
    return {
        "gpus": gpus,
        "arch": (osl[1].strip() if len(osl) > 1 else ""),
        "kernel": (osl[2].strip() if len(osl) > 2 else ""),
        "os": {"id": os_id, "version_id": os_ver},
        "apt": "APT" in osl[3:] if len(osl) > 3 else False,
        "kernel_headers": out("headers").strip() == "yes",
        "secure_boot": _parse_secure_boot(out("secureboot")),
        "nouveau_loaded": "nouveau" in modules,
        "nvidia_loaded": "nvidia" in modules,
        "driver": {"origin": origin, "installed": installed, "running": running,
                   "packages": drv_pkgs},
        "libcuda": bool(out("libcuda").strip()),
        "ctk": bool(out("ctk").strip()),
        "docker_nvidia_runtime": '"nvidia"' in docker,
        "prereq_installed": sorted(p for p in ("dkms", "build-essential", "curl", "gnupg",
                                               "ca-certificates", "cuda-keyring") if p in pk),
        "run_files": sorted(f for f in runfiles if f.endswith(".run")),
        "net_repo": http("net_repo"),
        "net_run": http("net_run"),
    }


# ─── decision (PURE) ───────────────────────────────────────────────────────────────────────────────
def _plan(status, code, **kw):
    p = {"status": status, "code": code, "params": {}, "method": None, "modules": None,
         "purge": [], "uninstall_run": False, "steps": [], "run_file": None, "needs_reboot": False}
    p.update(kw)
    return p


def pick_run_file(run_files, min_major, max_major_excl=None):
    """Choose the installer present in DRIVER_DIR : the one at RUN_VERSION if usable, else the
    highest version inside [min_major, max_major_excl)."""
    best, best_v = None, None
    for f in run_files or []:
        m = re.match(r"NVIDIA-Linux-x86_64-(\d+(?:\.\d+)+)\.run$", f)
        if not m:
            continue
        v = vparse(m.group(1))
        if v[0] < min_major or (max_major_excl is not None and v[0] >= max_major_excl):
            continue
        if f == RUN_FILENAME:
            return f
        if best_v is None or v > best_v:
            best, best_v = f, v
    return best


def decide(f):
    """Facts -> plan. status in: no_gpu | ok | pending_reboot | toolkit_only | install | upgrade | refused.

    `code` is a stable reason code, `params` its values ; the UI turns them into a sentence."""
    gpus = f.get("gpus") or []
    if not gpus:
        return _plan("no_gpu", "no_gpu")
    classes = {ARCH_CLASS.get(g.get("arch") or "", "unknown") for g in gpus}
    names = ", ".join(sorted({g["name"] for g in gpus}))
    legacy = "legacy" in classes
    drv = f["driver"]
    eff = drv.get("running") or drv.get("installed")
    major = vmajor(eff)
    too_new = bool(legacy and major is not None and major >= LEGACY_DRIVER_CEILING_MAJOR)
    too_old = bool(major is not None and major < MIN_DRIVER_MAJOR)
    good = bool(eff) and not too_new and not too_old
    toolkit_ok = bool(f.get("ctk")) and bool(f.get("docker_nvidia_runtime"))

    # Nothing to do (or only the toolkit) : a node that already works is NEVER touched, whatever
    # the method that put its driver there (Debian packages, NVIDIA repo, .run).
    if good and drv.get("running") and f.get("libcuda"):
        if toolkit_ok:
            return _plan("ok", "driver_ok", params={"version": drv["running"], "gpus": names})
        steps = [] if f.get("ctk") else ["toolkit"]
        steps.append("docker_runtime")
        return _plan("toolkit_only", "toolkit_missing", steps=steps,
                     params={"version": drv["running"], "gpus": names})
    if good and not drv.get("running"):
        return _plan("pending_reboot", "installed_not_loaded", params={"version": eff, "gpus": names})

    # From here the driver must be installed / replaced : refusals first.
    if "refused" in classes:
        return _plan("refused", "gpu_too_old", params={"gpus": names})
    if "unknown" in classes:
        return _plan("refused", "gpu_unknown", params={"gpus": names})
    if f.get("arch") != "x86_64":
        return _plan("refused", "arch_unsupported", params={"arch": f["arch"]})
    if not f.get("apt"):
        return _plan("refused", "no_apt")
    if f.get("secure_boot") is True:
        return _plan("refused", "secure_boot")

    status = "upgrade" if eff else "install"
    if too_new:
        reason = "driver_too_new"
    elif too_old:
        reason = "driver_too_old"
    elif eff and not f.get("libcuda"):
        reason = "libcuda_missing"
    else:
        reason = "no_driver"
    params = {"gpus": names, "current": eff or ""}

    os_is_repo = f["os"]["id"] == "debian"              # the NVIDIA repo is consumed as a Debian one
    repo_ok = os_is_repo and f.get("net_repo")          # ...and exists for THIS release (HTTP 200)
    if legacy:
        method, modules = "run", "proprietary"
        local_run = pick_run_file(f.get("run_files"), MIN_DRIVER_MAJOR, LEGACY_DRIVER_CEILING_MAJOR)
    elif repo_ok:
        method, modules = "repo-open", "open"
        local_run = None
    else:
        # Turing+ but the repo is unusable (offline, or not Debian 13) : the same .run, open modules.
        method, modules = "run", "open"
        local_run = pick_run_file(f.get("run_files"), MIN_DRIVER_MAJOR)
        params["fallback"] = "repo_unavailable" if os_is_repo else "repo_os"

    run_file = None
    if method == "run":
        if local_run:
            run_file = local_run
        elif f.get("net_run"):
            run_file = RUN_FILENAME                      # downloaded by the fetch_run step
        else:
            return _plan("refused", "no_source", params=params)
        params["version"] = re.sub(r"^NVIDIA-Linux-x86_64-|\.run$", "", run_file)
    else:
        params["version"] = ""

    # What must leave first : a different packaging system is removed cleanly, never layered.
    origin = drv.get("origin")
    purge, uninstall_run = [], False
    if origin == "debian" or (origin == "nvidia-repo" and method == "run"):
        purge = list(drv.get("packages") or [])
    elif origin == "run" and method == "repo-open":
        uninstall_run = True

    steps = ["apt_components", "apt_update", "prereqs"]
    if purge:
        steps.append("purge")
    if uninstall_run:
        steps.append("uninstall_run")
    steps.append("nouveau")
    if method == "repo-open":
        steps += ["repo_setup", "install_repo"]
    else:
        if not local_run:
            steps.append("fetch_run")
        steps.append("install_run")
    steps.append("verify_module")
    if not f.get("ctk"):
        steps.append("toolkit")
    steps += ["docker_runtime", "initramfs"]
    return _plan(status, reason, params=params, method=method, modules=modules, purge=purge,
                 uninstall_run=uninstall_run, steps=steps, run_file=run_file, needs_reboot=True)


def rows_for_ui(f, plan):
    """Facts shown by the generic panel : [{key, value}] (label = i18n key hostdrv rows)."""
    d = f["driver"]
    rows = [{"key": "gpu", "value": "; ".join("%s (%s)" % (g["name"], g.get("arch") or "?")
                                              for g in f["gpus"]) or "-"},
            {"key": "driver", "value": "%s (%s)" % (d.get("running") or d.get("installed") or "-",
                                                    d.get("origin"))}]
    t = {"repo-open": "NVIDIA apt repo - open modules",
         "run": ".run %s - %s modules" % (RUN_VERSION, plan.get("modules"))}.get(plan.get("method"))
    rows.append({"key": "target", "value": t or ">= %d" % MIN_DRIVER_MAJOR})
    rows.append({"key": "secure_boot", "value": {True: "enabled", False: "disabled", None: "?"}[f["secure_boot"]]})
    rows.append({"key": "toolkit", "value": "%s / runtime %s" % ("yes" if f["ctk"] else "no",
                                                                 "yes" if f["docker_nvidia_runtime"] else "no")})
    rows.append({"key": "cuda", "value": "libcuda %s" % ("yes" if f["libcuda"] else "no")})
    if f.get("run_files"):
        rows.append({"key": "local_installer", "value": ", ".join(f["run_files"])})
    return rows


def state(probe=live_probe):
    f = gather_facts(probe)
    plan = decide(f)
    can_run = plan["status"] in ("install", "upgrade", "toolkit_only")
    return {"kind": KIND, "version": SCRIPT_VERSION, "status": plan["status"], "plan": plan,
            "rows": rows_for_ui(f, plan), "can_run": can_run, "facts": f,
            "min_driver_major": MIN_DRIVER_MAJOR, "upload_dir": DRIVER_DIR,
            "upload_hint": "NVIDIA-Linux-x86_64-<version>.run"}


# ─── run ───────────────────────────────────────────────────────────────────────────────────────────
class StepError(Exception):
    pass


def say(msg):
    print(msg, flush=True)


def _apt_opts():
    return [x for x in os.environ.get("BOBI_APT_OPTS", "").split() if x]


def _run_cmd(argv, check=True, env=None, timeout=3600, desc=None):
    say("$ " + " ".join(argv))
    e = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    if env:
        e.update(env)
    p = subprocess.run(argv, env=e, timeout=timeout, stdout=sys.stdout, stderr=subprocess.STDOUT)
    if check and p.returncode != 0:
        raise StepError("%s failed (rc=%d)" % (desc or argv[0], p.returncode))
    return p.returncode


def _apt(*args, check=True, desc=None):
    return _run_cmd(["apt-get"] + _apt_opts() + list(args), check=check, desc=desc or "apt-get " + args[0])


def apt_knows(pkg):
    """True when apt has a REAL package of that name. `grep "^Package: <pkg>$"` and not the return
    code alone : on a purely virtual package `apt-cache show` exits 0 with a note (same trap, and same
    cure, as the Docker block of install-node.sh) - a hardcoded package list dies the day a name changes
    between Debian releases."""
    rc, _o = _sh("apt-cache show %s 2>/dev/null | grep -q '^Package: %s$'" % (pkg, pkg))
    return rc == 0


def _dpkg_missing(pkgs):
    miss = []
    for p in pkgs:
        rc, _o = _sh("dpkg-query -W -f='${db:Status-Abbrev}' %s 2>/dev/null | grep -q '^ii'" % p)
        if rc != 0:
            miss.append(p)
    return miss


def _read_apt_sources():
    txt = ""
    for path in ["/etc/apt/sources.list"] + sorted(
            os.path.join("/etc/apt/sources.list.d", x) for x in
            (os.listdir("/etc/apt/sources.list.d") if os.path.isdir("/etc/apt/sources.list.d") else [])):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                txt += fh.read() + "\n"
        except OSError:
            pass
    return txt


def step_apt_components(plan, f):
    changed = False
    for comp in APT_COMPONENTS:
        if component_present(_read_apt_sources(), comp):
            say("apt component '%s' already enabled" % comp)
            continue
        deb822 = "/etc/apt/sources.list.d/debian.sources"
        if os.path.isfile(deb822):
            _sh("sed -i -E '/^Components:/ s/$/ %s/' %s" % (comp, deb822))
            changed = True
        elif os.path.isfile("/etc/apt/sources.list"):
            _sh("sed -i -E '/^deb .*debian/ s/$/ %s/' /etc/apt/sources.list" % comp)
            changed = True
        say("apt component '%s' enabled" % comp)
    f["_apt_changed"] = changed


def step_apt_update(plan, f):
    rc = _apt("update", "-qq", check=False)
    if rc != 0:
        # Offline node : acceptable only if every prerequisite is already present (checked next).
        say("WARNING: apt-get update failed (no network or unreachable mirror) - continuing with the local package cache")


def _kernel_headers_pkgs():
    rel = os.uname().release
    pk = ["linux-headers-" + rel]
    if rel.endswith("-amd64"):
        pk.append("linux-headers-amd64")                  # keeps headers following kernel upgrades
    return pk


def step_prereqs(plan, f):
    want = ["dkms", "build-essential", "curl", "ca-certificates", "gnupg"] + _kernel_headers_pkgs()
    miss = _dpkg_missing(want)
    say("prerequisites present: %s" % (", ".join(sorted(set(want) - set(miss))) or "none"))
    if not miss:
        return
    known = [p for p in miss if apt_knows(p)]
    for p in miss:
        if p not in known:
            say("prerequisite '%s' is unknown to apt on this system - skipped" % p)
    # Headers for the RUNNING kernel are not optional : a missing one means no module, say it now.
    hdr = "linux-headers-" + os.uname().release
    if hdr in miss and hdr not in known:
        raise StepError("%s is not available from apt (kernel no longer published, or no network). "
                        "Install a published kernel + its headers, reboot, and retry." % hdr)
    say("installing prerequisites: %s" % ", ".join(known))
    rc = _apt("install", "-y", "-qq", "--no-install-recommends", *known, check=False) if known else 0
    if rc != 0:
        raise StepError("prerequisites could not be installed (%s). Check network access to the apt "
                        "mirror, or that headers for the RUNNING kernel (%s) are still published."
                        % (", ".join(miss), os.uname().release))


def step_purge(plan, f):
    pk = plan["purge"]
    say("removing %d package(s) of the previous driver stack (container toolkit untouched)" % len(pk))
    _apt("purge", "-y", "-qq", *pk, desc="apt-get purge")


def step_uninstall_run(plan, f):
    say("removing the previous .run installation")
    _run_cmd(["/usr/bin/nvidia-uninstall", "--silent"], check=False)


def step_nouveau(plan, f):
    body = "# written by Bobi.Studio : the open nouveau driver must not claim the GPU\n" \
           "blacklist nouveau\noptions nouveau modeset=0\n"
    cur = ""
    try:
        with open(NOUVEAU_BLACKLIST) as fh:
            cur = fh.read()
    except OSError:
        pass
    if cur != body:
        with open(NOUVEAU_BLACKLIST, "w") as fh:
            fh.write(body)
        f["_initramfs"] = True
        say("nouveau blacklisted (%s)" % NOUVEAU_BLACKLIST)
    else:
        say("nouveau already blacklisted")
    if f.get("nouveau_loaded"):
        say("note: nouveau is currently loaded; it is replaced at the next boot")


def _curl(url, dest):
    _run_cmd(["curl", "-fsSL", "--retry", "2", "-m", "900", "-o", dest, url], desc="download " + url)


def step_repo_setup(plan, f):
    rc, _o = _sh("dpkg-query -W -f='${db:Status-Abbrev}' %s 2>/dev/null | grep -q '^ii'" % REPO_KEYRING_PKG)
    if rc != 0:
        tmp = "/tmp/cuda-keyring.deb"
        _curl("%s/%s" % (repo_base(f["os"]["id"], f["os"]["version_id"]), REPO_KEYRING_FILE), tmp)
        _run_cmd(["dpkg", "-i", tmp])
        os.unlink(tmp)
    else:
        say("NVIDIA repository keyring already installed")
    _apt("update", "-qq", desc="apt-get update (NVIDIA repo)")


def step_install_repo(plan, f):
    for p in REPO_PACKAGES_REQUIRED:
        if not apt_knows(p):
            raise StepError("package '%s' is not offered by the NVIDIA repository for this release" % p)
    pkgs = REPO_PACKAGES_REQUIRED + [p for p in REPO_PACKAGES_OPTIONAL if apt_knows(p)]
    say("installing %s from the NVIDIA repository (open kernel modules, built with DKMS)" % " ".join(pkgs))
    _apt("install", "-y", "-qq", *pkgs, desc="apt-get install " + pkgs[0])


def step_fetch_run(plan, f):
    os.makedirs(DRIVER_DIR, exist_ok=True)
    dest = os.path.join(DRIVER_DIR, RUN_FILENAME)
    say("downloading %s (about 400 MB)" % RUN_URL)
    _curl(RUN_URL, dest + ".part")
    os.replace(dest + ".part", dest)


def step_install_run(plan, f):
    path = os.path.join(DRIVER_DIR, plan["run_file"])
    if not os.path.isfile(path):
        raise StepError("installer %s not found in %s" % (plan["run_file"], DRIVER_DIR))
    say("verifying the installer's embedded checksum")
    _run_cmd(["sh", path, "--check"], desc="installer integrity check")
    for mod in ("nvidia_drm", "nvidia_modeset", "nvidia_uvm", "nvidia"):
        _sh("modprobe -r %s 2>/dev/null" % mod)           # best effort : a loaded module blocks the installer
    say("running the installer (%s modules, DKMS) - this takes several minutes" % plan["modules"])
    rc = _run_cmd(["sh", path, "--silent", "--accept-license", "--dkms",
                   "--kernel-module-type=%s" % plan["modules"], "--no-x-check", "--no-nouveau-check",
                   "--skip-module-load"], check=False, timeout=3600)
    if rc != 0:
        say("---- tail of /var/log/nvidia-installer.log ----")
        _run_cmd(["bash", "-c", "tail -n 40 /var/log/nvidia-installer.log 2>/dev/null || true"], check=False)
        raise StepError("NVIDIA installer failed (rc=%d)" % rc)
    f["_initramfs"] = True


def step_verify_module(plan, f):
    rc, ver = _sh("modinfo -F version nvidia 2>/dev/null")
    ver = (ver or "").strip()
    major = vmajor(ver)
    if rc != 0 or major is None:
        raise StepError("no 'nvidia' kernel module built for the running kernel %s (DKMS). See "
                        "'dkms status' and /var/lib/dkms/nvidia/*/build/make.log; kernel headers and a "
                        "compiler are required." % os.uname().release)
    if major < MIN_DRIVER_MAJOR:
        raise StepError("kernel module version %s is below the required %d" % (ver, MIN_DRIVER_MAJOR))
    say("kernel module nvidia %s is built for %s" % (ver, os.uname().release))


def step_toolkit(plan, f):
    miss = _dpkg_missing(["curl", "ca-certificates", "gnupg"])
    if miss:
        _apt("install", "-y", "-qq", "--no-install-recommends", *miss, check=False)
    if not os.path.isfile(CTK_KEYRING) or not os.path.isfile(CTK_LIST):
        say("adding the NVIDIA container toolkit repository")
        rc = _run_cmd(["bash", "-c", "curl -fsSL %s | gpg --dearmor --yes -o %s && test -s %s" %
                       (CTK_KEY_URL, CTK_KEYRING, CTK_KEYRING)], check=False)
        if rc != 0:
            _sh("rm -f %s" % CTK_KEYRING)               # never leave a half-written key
            raise StepError("could not fetch the NVIDIA container toolkit key (network access to "
                            "nvidia.github.io required); repository NOT added")
        _run_cmd(["bash", "-c", "curl -fsSL %s | sed 's#deb https://#deb [signed-by=%s] https://#g' > %s" %
                  (CTK_LIST_URL, CTK_KEYRING, CTK_LIST)])
        _apt("update", "-qq", check=False)
    _apt("install", "-y", "-qq", "nvidia-container-toolkit", desc="apt-get install nvidia-container-toolkit")


def step_docker_runtime(plan, f):
    if not _sh("command -v nvidia-ctk")[1].strip():
        raise StepError("nvidia-ctk is missing, docker runtime cannot be configured")
    _run_cmd(["nvidia-ctk", "runtime", "configure", "--runtime=docker"])
    # `runtimes` is a hot-reloadable dockerd option : reload (SIGHUP) instead of restart, which would
    # interrupt every running container. If it is not picked up, the reboot we require anyway does it.
    _sh("systemctl reload docker 2>/dev/null")
    time.sleep(2)
    _rc, o = _sh("docker info --format '{{json .Runtimes}}' 2>&1")
    if '"nvidia"' in (o or ""):
        say("docker runtime 'nvidia' active")
    else:
        say("docker runtime 'nvidia' written to daemon.json; it becomes active at the next docker start "
            "(the reboot) - docker was NOT restarted to keep running containers alive")


def step_initramfs(plan, f):
    if not f.get("_initramfs"):
        say("initramfs unchanged")
        return
    if _sh("command -v update-initramfs")[1].strip():
        _run_cmd(["update-initramfs", "-u"], check=False)


STEPS = {
    "apt_components": step_apt_components, "apt_update": step_apt_update, "prereqs": step_prereqs,
    "purge": step_purge, "uninstall_run": step_uninstall_run, "nouveau": step_nouveau,
    "repo_setup": step_repo_setup, "install_repo": step_install_repo, "fetch_run": step_fetch_run,
    "install_run": step_install_run, "verify_module": step_verify_module, "toolkit": step_toolkit,
    "docker_runtime": step_docker_runtime, "initramfs": step_initramfs,
}
STEP_ORDER = ("apt_components", "apt_update", "prereqs", "purge", "uninstall_run", "nouveau",
              "repo_setup", "install_repo", "fetch_run", "install_run", "verify_module", "toolkit",
              "docker_runtime", "initramfs")


def run(probe=live_probe):
    say("== bobi nvidia host driver %s ==" % SCRIPT_VERSION)
    if os.geteuid() != 0:
        say("ERROR: must run as root")
        return 1
    f = gather_facts(probe)
    plan = decide(f)
    say("state: %s (%s) %s" % (plan["status"], plan["code"], json.dumps(plan["params"], sort_keys=True)))
    if plan["status"] == "refused":
        say("REFUSED: %s - nothing was changed" % plan["code"])
        return 1
    if plan["status"] not in ("install", "upgrade", "toolkit_only"):
        say("nothing to do (%s)" % plan["status"])
        return EXIT_OK
    steps = plan["steps"]
    say("method: %s  modules: %s  steps: %s" % (plan["method"], plan["modules"], ", ".join(steps)))
    for i, name in enumerate(steps, 1):
        say("")
        say("[step %d/%d] %s" % (i, len(steps), name))
        try:
            STEPS[name](plan, f)
        except StepError as e:
            say("FAILED at step '%s': %s" % (name, e))
            return 1
        except subprocess.TimeoutExpired:
            say("FAILED at step '%s': timeout" % name)
            return 1
    say("")
    if plan["needs_reboot"]:
        say("DONE - reboot required to load the new driver; then run the GPU detection")
        return EXIT_REBOOT
    say("DONE")
    return EXIT_OK


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "state"
    if cmd == "state":
        print(json.dumps(state(), sort_keys=True))
        return 0
    if cmd == "run":
        return run()
    print("usage: nvidia.py state|run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
