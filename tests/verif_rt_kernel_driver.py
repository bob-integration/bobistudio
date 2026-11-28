#!/usr/bin/env python3
"""Host driver `rt_kernel`: the decision, on facts replayed from dell-1 (2026-11-14) and variations.

Pure: `decide` and `gather_facts` are fed by a fake probe, nothing touches the host.
Mutations (RTK_MUT=…): nodkms (DKMS modules ignored), nopin (default not checked against the exact
entry), noice (ice ignored), newest (oldest RT kernel targeted instead of the newest).
"""
import importlib.util
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MUT = os.environ.get("RTK_MUT", "")
if MUT not in ("", "nodkms", "nopin", "noice", "newest"):
    print(" ÉCHEC mutation inconnue %r" % MUT)
    sys.exit(2)
spec = importlib.util.spec_from_file_location("rtk_sous_test",
                                              os.path.join(RACINE, "node_agent", "host_drivers", "rt_kernel.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
R.gather_facts_orig = R.gather_facts
echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


if MUT == "nodkms":
    _n = R.needed_modules
    R.needed_modules = lambda f, t: [x for x in _n(f, t) if not x.startswith("dkms:")]
elif MUT == "noice":
    _n2 = R.needed_modules
    R.needed_modules = lambda f, t: [x for x in _n2(f, t) if x != "ice"]
elif MUT == "newest":
    R.kver = lambda r: tuple(-int(x) for x in __import__("re").findall(r"\d+", r.split("-")[0]))

U = "77e916b9-1a66-488b-b0b1-82307301971c"
RT, RT2, STD, OLD = "6.12.116+deb13-rt-amd64", "6.12.120+deb13-rt-amd64", "6.12.116+deb13-amd64", "6.12.96+deb13-amd64"


def cfg(kernels):
    s = "submenu 'Advanced options for Debian GNU/Linux' $menuentry_id_option 'gnulinux-advanced-%s' {\n" % U
    for k in kernels:
        s += "\tmenuentry 'Debian GNU/Linux, with Linux %s' --class debian $menuentry_id_option " \
             "'gnulinux-%s-advanced-%s' {\n\t}\n" % (k, k, U)
        s += "\tmenuentry 'Debian GNU/Linux, with Linux %s (recovery mode)' $menuentry_id_option " \
             "'gnulinux-%s-recovery-%s' {\n\t}\n" % (k, k, U)
    return s + "}\n"


def entry(k):
    return "gnulinux-advanced-%s>gnulinux-%s-advanced-%s" % (U, k, U)


def probe_for(running=RT, rt_kernels=(RT,), default="saved", saved=None, nvidia_rt=True, dkms_rt=True,
              ice_running=None, ice_rt=None, os_id="debian", sb="SecureBoot disabled", loaded="nvidia blackmagic_io",
              no_initrd=(), rt_pkgs=None, decklink=0):
    ks = [OLD, STD] + list(rt_kernels)
    kmods = {OLD: (True, "-"), STD: (False, "-")}
    for k in rt_kernels:
        kmods[k] = (nvidia_rt, ice_rt or "-")
    kmods[running] = (True if running != RT or nvidia_rt else False, ice_running or kmods.get(running, (0, "-"))[1])
    if ice_running:
        kmods[running] = (kmods[running][0], ice_running)
    dk = ["blackmagic-io/16.4a1, %s, x86_64: installed" % OLD, "blackmagic-io/16.4a1, %s, x86_64: installed" % STD]
    if running not in (OLD, STD):
        dk.append("blackmagic-io/16.4a1, %s, x86_64: installed" % running)
    for k in rt_kernels:
        if dkms_rt and k != running:
            dk.append("blackmagic-io/16.4a1, %s, x86_64: installed" % k)
    out = {
        "os": "%s 13\nx86_64\n%s\n#1 SMP %s\nAPT\n" % (os_id, running, "PREEMPT_RT" if "-rt-" in running else "PREEMPT_DYNAMIC"),
        "kernels": "".join("%s 1 1 %d\n" % (k, 0 if k in no_initrd else 1) for k in ks),
        "rt_pkgs": rt_pkgs if rt_pkgs is not None else "".join(
            "linux-image-%s ii \nlinux-headers-%s ii \n" % (k, k) for k in rt_kernels),
        "kmods": "".join("%s nvidia=%d ice=%s\n" % (k, 1 if v[0] else 0, v[1]) for k, v in kmods.items()),
        "dkms": "\n".join(dk) + "\n",
        "modules": "\n".join(loaded.split()) + "\n",
        "nvidia_src": "/usr/src/nvidia-580.105.08\n",
        "nvidia_ver": "580.105.08\n",
        "grub_default": "GRUB_DEFAULT=%s\n" % default if default is not None else "",
        "grubenv": "saved_entry=%s\n" % (saved if saved is not None else entry(running)),
        "grub_cfg": cfg(ks),
        "secureboot": sb + "\n",
        "decklink": "%d\n" % decklink,
    }
    return lambda name: (0, out[name])


def plan(**kw):
    p = R.decide(R.gather_facts(probe_for(**kw)))
    if MUT == "nopin" and p["code"] == "set_default":
        p = dict(p, status="pending_reboot" if p["target"] != kw.get("running", RT) else "ok", code="ok")
    return p


print("1. dell-1 today: RT running, default, modules present → ok")
p = plan()
verifie(p["status"] == "ok" and p["target"] == RT, "1a ok", p)

print("2. stock kernel, no RT installed → install the kernel")
p = plan(running=STD, rt_kernels=())
verifie(p["status"] == "install" and p["code"] == "rt_not_installed", "2a install", p)
verifie(p["steps"] == ["apt_update", "install_kernel", "modules", "grub_default"], "2b all steps", p["steps"])

print("3. RT installed, DeckLink DKMS not built for it → modules_missing")
p = plan(running=STD, dkms_rt=False)
verifie(p["code"] == "modules_missing" and "dkms:blackmagic-io" in p["needs"], "3a DKMS module named", p)

print("4. RT installed, NVIDIA (not in DKMS) missing for it → nvidia to build")
p = plan(running=STD, nvidia_rt=False)
verifie(p["code"] == "modules_missing" and "nvidia" in p["needs"], "4a nvidia named", p)

print("4b. Debian-packaged NVIDIA (DKMS `nvidia-current`) → DKMS rebuilds it, no second build by hand")
_f = R.gather_facts(probe_for(running=STD, nvidia_rt=False, dkms_rt=False))
_f["dkms"] = _f["dkms"] + [("nvidia-current", "550.163.01", STD, "installed")]
_n4 = R.needed_modules(_f, RT)
verifie("dkms:nvidia-current" in _n4 and "nvidia" not in _n4, "4b DKMS only", _n4)

print("5. MTL node: running kernel has ice Kahawai, RT has stock ice → ice to build")
p = plan(running=STD, ice_running="2.6.6-Kahawai_2.6.6", ice_rt="1.0.0", loaded="ice")
verifie(p["code"] == "modules_missing" and "ice" in p["needs"], "5a ice named", p)
p = plan(running=STD, ice_running="2.6.6-Kahawai_2.6.6", ice_rt="2.6.6-Kahawai_2.6.6", loaded="ice")
verifie("ice" not in p["needs"], "5b ice already built → not asked", p)

print("6. modules ready, GRUB still on the stock kernel → set_default")
p = plan(running=STD, saved=entry(STD))
verifie(p["status"] == "install" and p["code"] == "set_default" and p["needs_reboot"], "6a set_default", p)
p = plan(running=STD, default="0", saved=entry(RT))
verifie(p["code"] == "set_default", "6b GRUB_DEFAULT=0 is not a pin, even with the right saved_entry", p)

print("7. default set to RT, not rebooted yet → pending_reboot")
p = plan(running=STD, saved=entry(RT))
verifie(p["status"] == "pending_reboot", "7a pending_reboot", p)

print("8. a newer RT kernel arrived by apt → it is the target, default no longer matches")
p = plan(running=RT, rt_kernels=(RT, RT2), saved=entry(RT), dkms_rt=False)
verifie(p["target"] == RT2, "8a newest RT targeted", p)
verifie(p["status"] == "install", "8b work to do on the new kernel", p)

print("9. refusals")
verifie(plan(os_id="rocky")["status"] == "refused", "9a not Debian")
p = plan(running=STD, nvidia_rt=False, sb="SecureBoot enabled")
verifie(p["status"] == "refused" and p["code"] == "secure_boot_modules", "9b Secure Boot + unsigned modules", p)

print("10. grub.cfg parsing")
e = R.grub_entries(cfg([STD, RT]))
verifie(e.get(RT) == entry(RT) and len(e) == 2, "10a plain entries only, submenu>entry form", e)

print("12. an RT install that stopped half-way is finished, never pinned as is (r620-2, 2026-11-15)")
# replayed: the postinst's DKMS NVIDIA build failed → image iF, headers/meta iU, no initrd, NVIDIA not built
R620 = ("linux-headers-6.12.116+deb13-rt-amd64 iU \nlinux-headers-rt-amd64 iU \n"
        "linux-image-6.12.116+deb13-rt-amd64 iF \nlinux-image-rt-amd64 iU \nlinux-image-6.12.90+deb13-rt-amd64 rc \n")
p = plan(running=STD, saved=entry(STD), nvidia_rt=False, dkms_rt=False, no_initrd=(RT,), rt_pkgs=R620)
verifie(p["status"] == "install" and p["code"] == "rt_incomplete", "12a half-configured packages → rt_incomplete", p)
verifie(p["steps"][:1] == ["install_kernel"] and p["steps"][-1] == "grub_default",
        "12b the install step comes back before modules and the pin", p["steps"])
verifie("linux-image-6.12.116+deb13-rt-amd64" in p["params"]["packages"] and "6.12.90" not in p["params"]["packages"],
        "12c the unfinished packages are named, a removed one (rc) is not", p["params"])
p = plan(running=STD, saved=entry(STD), no_initrd=(RT,))
verifie(p["code"] == "rt_incomplete", "12d packages fine but no initrd → rt_incomplete", p)
p = plan(running=STD, saved=entry(STD), nvidia_rt=False, dkms_rt=False)
verifie(p["code"] == "modules_missing", "12e a complete install still goes to modules_missing", p)

print("13. DeckLink card on the bus: the button refuses any CHANGE (Blackmagic driver crashes under RT)")
p = plan(running=STD, rt_kernels=(), decklink=2)
verifie(p["status"] == "refused" and p["code"] == "decklink" and p["params"].get("cards") == 2,
        "13a stock kernel + 2 DeckLink → refused « decklink », cards counted", p)
p = plan(running=STD, dkms_rt=False, decklink=1)
verifie(p["status"] == "refused" and p["code"] == "decklink", "13b RT present but not booted → still refused", p)
p = plan(decklink=1)
verifie(p["status"] == "ok", "13c already running RT (imposed earlier) → ok, nothing to refuse", p)
p = R.decide(R.gather_facts(probe_for(running=STD, rt_kernels=(), decklink=1)), allow_decklink=True)
verifie(p["status"] == "install" and p["code"] == "rt_not_installed",
        "13d install-node.sh --rt-kernel (allow_decklink) → install", p)
p = plan(running=STD, rt_kernels=(), sb="SecureBoot enabled", decklink=1)
verifie(p["status"] == "refused", "13e Secure Boot + DeckLink → still refused", p)
verifie(R.gather_facts(lambda n: (0, "garbage") if n == "decklink" else probe_for()(n))["decklink"] == 0,
        "13f unreadable count → 0, never a crash", "")
import inspect as _insp
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "node_agent", "install-node.sh"),
            encoding="utf-8").read()
verifie("--rt-kernel) RT_KERNEL=1; RT_FORCED=1;" in _src and 'run $_allow' in _src
        and '[ "$RT_FORCED" = 1 ] && _allow="--allow-decklink"' in _src,
        "13g install-node.sh passes --allow-decklink only when --rt-kernel was given", "")
verifie("allow_decklink=\"--allow-decklink\" in argv[2:]" in _insp.getsource(R.main),
        "13h `rt_kernel.py run --allow-decklink` reaches decide", "")

print("11. the RT kernel is installed with IGNORE_PREEMPT_RT_PRESENCE: its postinst builds DKMS NVIDIA")
appels = []
R._run_cmd = lambda argv, check=True, env=None, **kw: appels.append((list(argv), dict(env or {}))) or 0
R.gather_facts = lambda probe=None: R.gather_facts_orig(probe_for(running=STD))
ctx = {"probe": None}
R.step_install_kernel(ctx)
apt = [e for a, e in appels if a[:1] == ["apt-get"] and "install" in a and "linux-image-rt-amd64" in a]
verifie(len(apt) == 1 and apt[0].get("IGNORE_PREEMPT_RT_PRESENCE") == "1",
        "11a apt-get install of the RT kernel carries IGNORE_PREEMPT_RT_PRESENCE=1", appels)
verifie(ctx.get("target") == RT, "11b RT kernel found after install", ctx.get("target"))
cfg_ = [e for a, e in appels if a[:2] == ["dpkg", "--configure"]]
verifie(len(cfg_) == 1 and cfg_[0].get("IGNORE_PREEMPT_RT_PRESENCE") == "1",
        "11c a half-configured package is finished first, with the same environment", appels)
R._sh = lambda cmd, timeout=60: (0, "GRUB_DEFAULT=saved\n")
_exists = R.os.path.exists
R.os.path.exists = lambda path: False if path.startswith("/boot/initrd.img-") else _exists(path)
appels.clear()
try:
    R._pin(RT, None)
    refuse = False
except R.StepError:
    refuse = True
R.os.path.exists = _exists
verifie(refuse and not any(a[:1] == ["grub-set-default"] for a, _e in appels),
        "11d a kernel without initramfs is never made the boot default", appels)

if echecs:
    print("ÉCHEC : %d vérification(s)" % len(echecs))
    sys.exit(1)
print("OK")
