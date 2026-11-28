#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The DeckLink driver job must SAY when it installs on a PREEMPT_RT kernel.

Measured 2026-11-25 (dell-1, Precision 7910 + Duo 2) and 2026-11-27 (HP Z8 G4 + Quad 2): Desktop
Video 16.4a1 and 16.5a1 install, build and load cleanly under PREEMPT_RT — the job ends « DONE »,
the card enumerates — then fault in `bmio_notify_add` on the FIRST frame, output or input, with
nothing but the Blackmagic SDK. The card stays unusable and the node only reboots when forced.
The same machines are clean on the non-RT kernel.

Not a refusal: the install itself works, and a fixed driver would make the warning wrong, not the
install. What must never happen again is an operator reading « DONE » and « all up to date » with
no word about it.

  ./venv/bin/python tests/verif_pilote_blackmagic_rt.py
"""
import json
import os
import subprocess
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "node_agent", "host_drivers"))

import blackmagic as B                                                        # noqa: E402

_echecs = []


def dire(ok, titre, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", titre, (" — " + detail) if detail else ""))
    if not ok:
        _echecs.append(titre)


# A node with the driver installed and one card up to date: the state that reads « nothing to do ».
_SORTIES = {"list": (0, "Available Devices:\n * DeckLink Quad 2 (Gf0aaa170)\n     Firmware is up to date\n"),
            "tool": (0, "/usr/bin/DesktopVideoUpdateTool"), "package": (0, "16.5a1"),
            "devdir": (0, "8"), "modules": (0, "2"), "boot_id": (0, "b"), "files": (0, ""),
            "dkms": (0, "/usr/sbin/dkms"), "headers": (0, "/lib/modules/k/build"),
            "headers_meta": (0, ""), "libgl": (0, ""), "pci": (0, "1"), "built": (0, "2")}


def sonde(rt):
    k = "6.12.118+deb13-rt-amd64" if rt else "6.12.118+deb13-amd64"

    def probe(nom):
        if nom == "preempt_rt":
            return (0, "") if rt else (1, "")
        if nom == "kernel":
            return 0, k + "\n"
        return _SORTIES[nom]
    return probe


print("1. state() : avertissement sous RT, aucun sinon")
st = B.state(sonde(True))
dire(st["status"] == "ok", "le plan reste « ok » (pas un refus)", st["status"])
dire(st.get("warnings") == [{"code": "preempt_rt", "params": {"kernel": "6.12.118+deb13-rt-amd64"}}],
     "noyau RT : avertissement `preempt_rt` avec le noyau", repr(st.get("warnings")))
st = B.state(sonde(False))
dire(st.get("warnings") == [], "noyau normal : aucun avertissement", repr(st.get("warnings")))
dire(json.loads(json.dumps(st)) == st, "l'état reste sérialisable en JSON", "")

print("2. run() : dit au DÉBUT et à la FIN, et ne change pas le code de sortie")
for rt in (True, False):
    dits = []
    vrai_say, vrai_run = B.say, B._run
    B.say = dits.append
    B._run = lambda probe=None: (dits.append("<work>"), B.EXIT_REBOOT)[1]
    try:
        rc = B.run(sonde(rt))
    finally:
        B.say, B._run = vrai_say, vrai_run
    i = dits.index("<work>")
    avant = [d for d in dits[:i] if "PREEMPT_RT" in d]
    apres = [d for d in dits[i + 1:] if "PREEMPT_RT" in d]
    dire(rc == B.EXIT_REBOOT, "%s : code de sortie inchangé" % ("RT" if rt else "normal"), str(rc))
    if rt:
        dire(len(avant) == 1 and len(apres) == 1, "RT : avertissement avant ET après le travail",
             "%d avant, %d après" % (len(avant), len(apres)))
        dire("6.12.118+deb13-rt-amd64" in avant[0] if avant else False, "RT : le noyau est nommé", "")
    else:
        dire(not avant and not apres, "noyau normal : rien n'est dit", repr(dits))

print("3. la sonde réelle : vraie seulement sur un noyau RT")
for realtime, uname_v, attendu in (("1", "#1 SMP PREEMPT_DYNAMIC Debian", True),
                                   (None, "#1 SMP PREEMPT_RT Debian 6.12.118-1", True),
                                   (None, "#1 SMP PREEMPT_DYNAMIC Debian 6.12.118-1", False),
                                   ("0", "#1 SMP PREEMPT_DYNAMIC Debian", False)):
    # The probe is rewritten onto a fake /sys and a fake `uname`: the real ones would only ever
    # answer for the kernel this test happens to run on.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        rt_file = os.path.join(d, "realtime")
        if realtime is not None:
            open(rt_file, "w").write(realtime + "\n")
        bindir = os.path.join(d, "bin")
        os.mkdir(bindir)
        open(os.path.join(bindir, "uname"), "w").write("#!/bin/sh\necho '%s'\n" % uname_v)
        os.chmod(os.path.join(bindir, "uname"), 0o755)
        cmd = B.PROBES["preempt_rt"].replace("/sys/kernel/realtime", rt_file)
        rc = subprocess.run(["bash", "-c", cmd], env=dict(os.environ, PATH=bindir + ":" + os.environ["PATH"])).returncode
    dire((rc == 0) == attendu, "realtime=%s, uname -v « %s » → %s" % (realtime, uname_v, attendu), "rc=%d" % rc)

print("4. textes : FR et EN, noyau substitué")
for lang in ("fr", "en"):
    d = json.load(open(os.path.join(RACINE, "i18n", "%s.json" % lang), encoding="utf-8"))
    v = d.get("settings.hostdrv.blackmagic.warn.preempt_rt", "")
    dire("{kernel}" in v and "PREEMPT_RT" in v, "%s : clé présente, avec {kernel}" % lang, v[:60])

print()
if _echecs:
    print("ÉCHEC : %d vérification(s)" % len(_echecs))
    sys.exit(1)
print("OK")
