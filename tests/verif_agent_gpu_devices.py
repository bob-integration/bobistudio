#!/usr/bin/env python3
"""Node agent 0.32.6: a GPU container gets its NVIDIA device nodes as `--device` too.

With `--gpus` alone the nvidia hook opens them behind systemd's back: the scope's DeviceAllow never
lists them, and any daemon-reload or set-property revokes them ("Failed to initialize NVML: Unknown
Error" in 7 GPU containers of dell-1, 2026-11-12). Nothing here touches the host: device nodes,
/proc/driver/nvidia and nvidia-smi are all replaced.

Mutations (GPU_MUT=…): nodev (no --device emitted), allcards (every card for device=N),
nominor (index used as the device node, ignoring the driver's minor), noensure (start without
creating the missing nodes).
"""
import importlib.util
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MUT = os.environ.get("GPU_MUT", "")
if MUT not in ("", "nodev", "allcards", "nominor", "noensure"):
    print(" ÉCHEC mutation inconnue %r" % MUT)
    sys.exit(2)
echecs = []


def verifie(ok, titre, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + titre + (("  — " + str(detail)) if (detail and not ok) else ""))
    if not ok:
        echecs.append(titre)


spec = importlib.util.spec_from_file_location("bobi_node_agent_gpu_sous_test",
                                              os.path.join(RACINE, "node_agent", "agent.py"))
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

# Two cards; nvidia-smi index 1 is device minor 0 and index 0 is minor 1 (order differs on purpose).
TMP = tempfile.mkdtemp(prefix="verif_gpu_")
A.PROC_NVIDIA = os.path.join(TMP, "gpus")
for bus, minor in (("0000:3b:00.0", 1), ("0000:af:00.0", 0)):
    os.makedirs(os.path.join(A.PROC_NVIDIA, bus))
    with open(os.path.join(A.PROC_NVIDIA, bus, "information"), "w") as f:
        f.write("Model: \t Quadro P5000\nIRQ:   99\nDevice Minor: \t %d\n" % minor)
BUS = {"0": "00000000:3B:00.0", "1": "00000000:AF:00.0"}
PRESENT = {"/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools", "/dev/nvidia0", "/dev/nvidia1"}
A._dev_exists = lambda p: p in PRESENT
A._gpus = lambda: [{"index": 0, "name": "Quadro P5000", "mem_mb": 16384},
                   {"index": 1, "name": "Quadro P5000", "mem_mb": 16384}]


def fake_run(argv, timeout=60, input_bytes=None, env=None):
    if argv[:1] == ["nvidia-smi"] and "-i" in argv:
        return 0, BUS[argv[argv.index("-i") + 1]] + "\n", ""
    raise AssertionError("unexpected command %r" % (argv,))


A.run = fake_run
if MUT == "nodev":
    A._nvidia_device_nodes = lambda sel: []
elif MUT == "allcards":
    _orig = A._nvidia_device_nodes
    A._nvidia_device_nodes = lambda sel: _orig("all")
elif MUT == "nominor":
    A._nvidia_minor = lambda index: int(index)


def devices(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--device"]


print("1. device=0 → control nodes + the card's own minor")
argv = A._build_run_argv({"name": "c1", "image": "img", "gpus": "device=0"})
verifie("--gpus" in argv and argv[argv.index("--gpus") + 1] == "device=0", "1a --gpus kept")
d = devices(argv)
verifie({"/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools"} <= set(d), "1b control nodes passed", d)
verifie("/dev/nvidia-modeset" not in d, "1c absent node not passed", d)
verifie("/dev/nvidia1" in d, "1d index 0 → /dev/nvidia1 (driver minor)", d)
verifie("/dev/nvidia0" not in d, "1e the other card is NOT handed out", d)

print("2. all → both cards")
d = devices(A._build_run_argv({"name": "c2", "image": "img", "gpus": "all"}))
verifie({"/dev/nvidia0", "/dev/nvidia1"} <= set(d), "2a every card", d)

print("3. UUID selector → control nodes only")
d = devices(A._build_run_argv({"name": "c3", "image": "img", "gpus": "device=GPU-1234"}))
verifie("/dev/nvidiactl" in d and not any(x in d for x in ("/dev/nvidia0", "/dev/nvidia1")),
        "3a no card guessed", d)

print("4. no gpus → nothing")
argv = A._build_run_argv({"name": "c4", "image": "img"})
verifie("--gpus" not in argv and not devices(argv), "4a no GPU flag, no device")

print("5. after a reboot: nodes the container was created with are created again before docker start")
# dell-1, 2026-11-15: /dev/nvidia-modeset appeared 3 min after boot; `docker start` refused the GPU
# containers meanwhile and the recovery's single attempt left them down.
PRESENT.clear()
PRESENT.update({"/dev/nvidiactl", "/dev/nvidia1"})
DEVS = ('[{"PathOnHost":"/dev/nvidiactl"},{"PathOnHost":"/dev/nvidia-uvm"},{"PathOnHost":"/dev/nvidia-uvm-tools"},'
        '{"PathOnHost":"/dev/nvidia-modeset"},{"PathOnHost":"/dev/nvidia0"}]')
calls = []


def fake_run2(argv, timeout=60, input_bytes=None, env=None):
    calls.append(list(argv))
    if argv[:2] == ["docker", "inspect"]:
        return 0, DEVS + "\n", ""
    if argv[:1] == ["nvidia-modprobe"]:
        if "-m" in argv:
            PRESENT.add("/dev/nvidia-modeset")
        if "-u" in argv:
            PRESENT.update({"/dev/nvidia-uvm", "/dev/nvidia-uvm-tools"})
        for i, a in enumerate(argv):
            if a == "-c":
                PRESENT.add("/dev/nvidia" + argv[i + 1])
        return 0, "", ""
    if argv[:2] == ["docker", "start"]:
        miss = [d for d in ("/dev/nvidia-modeset", "/dev/nvidia-uvm", "/dev/nvidia0") if d not in PRESENT]
        return (1, "", "error gathering device information: %s: no such file" % miss[0]) if miss else (0, "c5\n", "")
    raise AssertionError("unexpected command %r" % (argv,))


A.run = fake_run2
if MUT == "noensure":
    A._nvidia_nodes_ensure = lambda paths: []
rc, _o, e = A._start_container("c5")
verifie(rc == 0, "5a the GPU container starts", e)
mp = [c for c in calls if c[:1] == ["nvidia-modprobe"]]
verifie(len(mp) == 1 and {"-m", "-u"} <= set(mp[0]) and mp[0].count("-u") == 1 and "0" in mp[0],
        "5b one nvidia-modprobe call: modeset, uvm (once) and card 0", mp)
calls.clear()
A._start_container("c5")
verifie(not any(c[:1] == ["nvidia-modprobe"] for c in calls), "5c nothing missing → nvidia-modprobe not run", calls)

if echecs:
    print("ÉCHEC : %d vérification(s)" % len(echecs))
    sys.exit(1)
print("OK")
