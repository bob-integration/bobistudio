#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The RT kernel is the DEFAULT of a new node (decided with Cyril, 2026-11-23).

« auto » keeps the normal kernel in two cases only: a DeckLink card (the Blackmagic driver 16.4a1
crashes on PREEMPT_RT, measured on dell-1 with and without SCHED_FIFO) and a reinstall (a node's
kernel is not changed by an update that did not ask for it). Held here, offline:
  1. install-node.sh: `resolve_rt_kernel` run for real in bash on a fake PCI tree — new node → RT,
     DeckLink card or `decklink` capability → normal, reinstall → unchanged, --rt-kernel /
     --no-rt-kernel win; the default is auto; it runs before the kernel is provisioned;
  2. enrollment: `rt_kernel_choice` maps the three UI values; the profile served to the installer
     keeps the choice (None for an old profile = auto);
  3. install.py: True → --rt-kernel, False → --no-rt-kernel, anything else → no flag;
  4. the enrollment form offers auto / rt / normal, auto selected; labels in both languages.

    ./venv/bin/python tests/verif_noyau_rt_par_defaut.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


SH = open(os.path.join(ROOT, "node_agent", "install-node.sh"), encoding="utf-8").read()


def fonction(nom):
    m = re.search(r"^%s\(\) \{\n.*?^\}\n" % nom, SH, flags=re.S | re.M)
    return m.group(0) if m else ""


print("\n1. install-node.sh")
verifie(re.search(r"^RT_KERNEL=auto;", SH, flags=re.M) is not None, "the default is auto")
verifie("--no-rt-kernel) RT_KERNEL=0; shift;;" in SH and "--rt-kernel) RT_KERNEL=1; RT_FORCED=1; shift;;" in SH,
        "--rt-kernel and --no-rt-kernel")
i_res, i_prov = SH.find("\nresolve_rt_kernel\n"), SH.find('if [ "$RT_KERNEL" = 1 ]; then provision_rt; fi')
verifie(0 < i_res < i_prov, "resolved right before the kernel is provisioned")
i_pre, i_write = SH.find('PRE_INSTALLED=0; [ -f "$CONF_DIR/config.json" ] && PRE_INSTALLED=1'), \
    SH.find('> "$CONF_DIR/config.json"')
i_conf = SH.find('CONF_DIR="${CONF_DIR:-/etc/bobi-node-agent}"')
verifie(0 < i_conf < i_pre < i_write, "a reinstall is seen after CONF_DIR is set, BEFORE config.json is written")
# The script runs under `set -u`: run it for real up to its argument parsing.
_r = subprocess.run(["bash", os.path.join(ROOT, "node_agent", "install-node.sh"), "--option-inconnue"],
                    env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
verifie("unbound variable" not in _r.stderr and "option inconnue" in _r.stderr,
        "the script starts (no unbound variable) and reaches its argument parsing", _r.stderr[-300:])
funcs = fonction("has_decklink") + fonction("resolve_rt_kernel")
verifie(funcs.count("() {") == 2, "both functions found")


def run(rt="auto", pre=0, caps="compute", decklink=False):
    with tempfile.TemporaryDirectory() as d:
        for i, vendor in enumerate(["0x8086", "0x10de"] + (["0xbdbd"] if decklink else [])):
            os.makedirs(os.path.join(d, "0000:%02x:00.0" % i))
            with open(os.path.join(d, "0000:%02x:00.0" % i, "vendor"), "w") as f:
                f.write(vendor + "\n")
        script = ("warn(){ echo \"W:$*\"; }; ok(){ echo \"O:$*\"; }\n"
                  "has_cap(){ case \",$CAPS,\" in *\",$1,\"*) return 0;; *) return 1;; esac; }\n"
                  + funcs + "\nRT_KERNEL=%s; PRE_INSTALLED=%d; CAPS=%s; resolve_rt_kernel; echo \"R=$RT_KERNEL\"\n"
                  % (rt, pre, caps))
        out = subprocess.run(["bash", "-c", script], env={"PCI_SYSFS": d, "PATH": os.environ["PATH"]},
                             capture_output=True, text=True).stdout
    return re.search(r"R=(\S+)", out).group(1) if "R=" in out else None, out


verifie(run()[0] == "1", "a new node: real time", run()[1])
r, out = run(decklink=True)
verifie(r == "0" and "DeckLink" in out, "a DeckLink card (PCI 0xbdbd): normal, and said", out)
verifie(run(caps="compute,decklink")[0] == "0", "the decklink capability: normal")
r, out = run(pre=1)
verifie(r == "0" and "réinstallation" in out, "a reinstall: kernel unchanged, and said", out)
verifie(run(rt="1", decklink=True)[0] == "1", "--rt-kernel wins over a DeckLink card")
verifie(run(rt="0")[0] == "0", "--no-rt-kernel wins on a new node")

print("\n2. Enrollment profile")
from app import config, database                                              # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="verif_rt_def_"), "t.db")
database._tls.__dict__.pop("conn", None)                     # never the production base
database.init_db()
from app.routes import enrollment                                            # noqa: E402
ch = enrollment.rt_kernel_choice
verifie([ch("rt"), ch(True), ch("normal"), ch(False), ch("auto"), ch(None), ch("x")]
        == [True, True, False, False, "auto", "auto", "auto"], "auto / rt / normal (and the booleans)")
src = open(os.path.join(ROOT, "app", "routes", "enrollment.py"), encoding="utf-8").read()
verifie('"rt_kernel": rt_kernel_choice(d.get("rt_kernel")),' in src, "the profile stores the choice")
verifie('"rt_kernel": prof.get("rt_kernel"),' in src and 'bool(prof.get("rt_kernel"))' not in src,
        "served to the installer as stored (an old profile without it = auto, not « normal »)")

print("\n3. install.py")
ins = open(os.path.join(ROOT, "install", "install.py"), encoding="utf-8").read()
blk = ins[ins.find('if prof.get("rt_kernel") is True:'):][:400]
verifie('args.append("--rt-kernel")' in blk and 'elif prof.get("rt_kernel") is False:' in blk
        and 'args.append("--no-rt-kernel")' in blk, "True → --rt-kernel, False → --no-rt-kernel, else nothing")

print("\n4. Enrollment form and labels")
tpl = open(os.path.join(ROOT, "templates", "settings.html"), encoding="utf-8").read()
sel = tpl[tpl.find('<select id="np_rt_kernel"'):][:600]
verifie('value="auto" selected' in sel and 'value="rt"' in sel and 'value="normal"' in sel,
        "auto / rt / normal, auto selected")
verifie("rt_kernel: document.getElementById('np_rt_kernel').value" in tpl, "the form sends the value")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    missing = [k for k in ("settings.st.rt_kernel_auto", "settings.st.rt_kernel_on",
                           "settings.st.rt_kernel_off", "settings.st.rt_kernel_title") if not cat.get(k)]
    verifie(not missing, "%s: labels" % lang, missing)

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
