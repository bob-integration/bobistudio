#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Pilote NVIDIA d'un nœud : la DÉCISION (quelle méthode, quand ne RIEN toucher, quand refuser).

★ Pourquoi ce test. `install-node.sh --with gpu` posait le `nvidia-driver` de Debian 13 (550.163.01),
or l'image GPU épingle cupy-cuda12x==13.4.1 = runtime CUDA 12.8 = pilote >= 570 : un nœud neuf
retombait sur le CPU sans un mot. La logique est désormais UN script hôte (`node_agent/host_drivers/
nvidia.py`) partagé par l'installeur et par le bouton de l'interface ; sa partie décisionnelle est pure
(`decide(facts)`), et les faits sont lus par une sonde injectable — on rejoue donc des sondes RÉELLES.

Fixtures (`tests/fixtures/nvidia_driver/*.json`) : sorties réelles des sondes en LECTURE SEULE sur les
nœuds du banc —  dell-1 (Quadro P5000, pilote 580.105.08 posé par l'installeur .run), r620-1/2
(Tesla T4, pilote Debian 550.163.01), dl360-1 (aucun GPU). Les autres cas sont dérivés de ces sondes
en changeant UNE chose à la fois.

    ./venv/bin/python tests/verif_nvidia_driver.py
"""
import copy
import importlib.util
import json
import os
import re
import sys

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(R, "tests", "fixtures", "nvidia_driver")
SRC = os.environ.get("BOBI_NVIDIA_SRC") or os.path.join(R, "node_agent", "host_drivers", "nvidia.py")

spec = importlib.util.spec_from_file_location("nvidia_under_test", SRC)
nv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nv)

ko = []


def ok(cond, titre, detail=""):
    print("%s %s" % ("  ok  " if cond else " ÉCHEC", titre))
    if not cond:
        print("         %s" % detail)
        ko.append(titre)


def load(name):
    with open(os.path.join(FIX, name + ".json")) as f:
        return json.load(f)["probes"]


def plan_of(pr):
    f = nv.gather_facts(lambda n: tuple(pr[n]))
    return f, nv.decide(f)


def with_(pr, **over):
    p = copy.deepcopy(pr)
    for k, v in over.items():
        p[k] = [0, v] if not isinstance(v, (list, tuple)) else list(v)
    return p


def no_driver(pr):
    """Même machine, mais SANS pilote ni toolkit : le cas d'un nœud tout juste installé."""
    pk = [ln for ln in pr["dpkg"][1].splitlines()
          if not re.match(r"^(nvidia-(?!container)|libnvidia-(?!container)|libcuda1|firmware-nvidia-gsp|"
                          r"libnvcuvid1|libegl-nvidia|libgles-nvidia|libglx-nvidia|libgl1-nvidia|"
                          r"xserver-xorg-video-nvidia|glx-alternative-nvidia)", ln)]
    mods = "\n".join(m for m in pr["modules"][1].splitlines() if not m.startswith("nvidia"))
    return with_(pr, smi=[127, "bash: nvidia-smi : commande introuvable"], procver="", run_installer="",
                 dpkg="\n".join(pk) + "\n", modules=mods, libcuda="", ctk="", docker='{"runc":{}}')


def gpu_dev(pr, dev, chip):
    """Même machine avec un autre GPU (identifiant PCI + nom pci.ids), sans pilote."""
    p = no_driver(pr)
    p["pci"] = [0, "0000:03:00.0 0x10de 0x%04x 0x030200\n0000:03:00.1 0x10de 0x10f0 0x040300\n" % dev]
    p["lspci"] = [0, "03:00.0 3D controller [0302]: NVIDIA Corporation %s [10de:%04x] (rev a1)\n" % (chip, dev)]
    return p


def run():
    t4 = load("r620-1_t4_debian550")
    t4b = load("r620-2_t4_debian550")
    p5 = load("dell-1_p5000_run580")
    nogpu = load("dl360-1_no_gpu")

    # ── Le minimum vient d'UNE constante, liée à l'image GPU ─────────────────────────────────────
    src = open(SRC, encoding="utf-8").read()
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    lits = [ln for ln in code if re.search(r"\b570\b", ln) and "MIN_DRIVER_MAJOR =" not in ln]
    ok(nv.MIN_DRIVER_MAJOR == 570 and not lits,
       "le minimum 570 n'est écrit qu'UNE fois (MIN_DRIVER_MAJOR), jamais répété en dur", "autres: %s" % lits)
    dock = open(os.path.join(R, "plugins", "_compute_gpu_runtime", "Dockerfile"), encoding="utf-8").read()
    pin = nv.CUPY_PIN_COUPLED.split("==")
    ok(re.search(r"ARG CUPY_VERSION=%s" % re.escape(pin[1]), dock) and pin[0] in dock,
       "le minimum est lié au pin cupy du Dockerfile GPU (si le pin bouge, revoir MIN_DRIVER_MAJOR)",
       "CUPY_PIN_COUPLED=%s" % nv.CUPY_PIN_COUPLED)
    old = nv.MIN_DRIVER_MAJOR
    try:
        nv.MIN_DRIVER_MAJOR = 550                        # 550.163 devient suffisant : plus rien à faire
        _f, p = plan_of(t4)
        ok(p["status"] == "ok", "la décision lit MIN_DRIVER_MAJOR (550 → T4 en 550.163 conforme)",
           "status=%s" % p["status"])
        nv.MIN_DRIVER_MAJOR = 600                        # dell-1 en 580 devient trop vieux
        _f, p = plan_of(p5)
        ok(p["status"] == "upgrade" and p["code"] == "driver_too_old",
           "la décision lit MIN_DRIVER_MAJOR (600 → dell-1 en 580 trop vieux)", "plan=%s" % p["status"])
    finally:
        nv.MIN_DRIVER_MAJOR = old

    # ── Aucun GPU ────────────────────────────────────────────────────────────────────────────────
    f, p = plan_of(nogpu)
    ok(p["status"] == "no_gpu" and not p["steps"], "aucun GPU NVIDIA → rien à faire, aucune étape", "plan=%s" % p)

    # ── dell-1 : Pascal, pilote 580.105.08 posé à la main par le .run → CONFORME, INTOUCHÉ ────────
    f, p = plan_of(p5)
    ok(f["driver"]["origin"] == "run" and f["gpus"][0]["arch"] == "pascal",
       "dell-1 : Pascal détecté, pilote d'origine .run", "driver=%s gpu=%s" % (f["driver"], f["gpus"]))
    ok(p["status"] == "ok" and not p["steps"] and not p["purge"] and not p["uninstall_run"]
       and not p["needs_reboot"],
       "dell-1 (580 .run, Pascal) → OK : zéro étape, zéro purge, zéro redémarrage",
       "plan=%s" % {k: p[k] for k in ("status", "steps", "purge", "needs_reboot")})

    # ── T4 + pilote Debian 550 → mise à jour par le dépôt NVIDIA (modules ouverts), purge d'abord ─
    for pr, nom in ((t4, "r620-1"), (t4b, "r620-2")):
        f, p = plan_of(pr)
        ok(p["status"] == "upgrade" and p["method"] == "repo-open" and p["modules"] == "open"
           and p["code"] == "driver_too_old",
           "%s : T4 + Debian 550 → mise à jour dépôt NVIDIA, modules OUVERTS" % nom, "plan=%s" % p["status"])
        ok(f["driver"]["origin"] == "debian" and "nvidia-kernel-dkms" in p["purge"]
           and "nvidia-driver" in p["purge"] and "libcuda1" in p["purge"],
           "%s : purge des paquets Debian nvidia (dkms, driver, libcuda1)" % nom, "purge=%s" % p["purge"][:6])
        ok(not any("container" in x for x in p["purge"]) and "firmware-nvidia-graphics" not in p["purge"],
           "%s : la purge épargne le container toolkit et le firmware nouveau" % nom, "purge=%s" % p["purge"])
        ok(p["steps"].index("purge") < p["steps"].index("install_repo") and p["needs_reboot"],
           "%s : la purge précède l'installation ; redémarrage requis" % nom, "steps=%s" % p["steps"])
    ok(plan_of(t4)[1]["steps"][:3] == ["apt_components", "apt_update", "prereqs"],
       "les prérequis (composants apt, index, dkms/en-têtes) sont les premières étapes", "")

    # ── Pascal neuf (aucun pilote) → .run 580, modules propriétaires ──────────────────────────────
    f, p = plan_of(gpu_dev(t4, 0x1bb0, "GP104GL [Quadro P5000]"))
    ok(f["gpus"][0]["arch"] == "pascal" and p["status"] == "install" and p["method"] == "run"
       and p["modules"] == "proprietary" and p["params"]["version"] == nv.RUN_VERSION,
       "Pascal neuf → .run %s, modules propriétaires" % nv.RUN_VERSION, "plan=%s %s" % (p["method"], p["params"]))
    ok("fetch_run" in p["steps"] and p["steps"].index("fetch_run") < p["steps"].index("install_run"),
       "Pascal neuf (en ligne, sans installeur local) → téléchargement avant installation", "%s" % p["steps"])
    # installeur local déposé (téléversement / bundle) → plus de téléchargement, et la ligne hors-ligne passe
    pr = with_(gpu_dev(t4, 0x1bb0, "GP104GL [Quadro P5000]"), runfiles="%s/%s\n" % (nv.DRIVER_DIR, nv.RUN_FILENAME),
               net_run=[0, "000"], net_repo=[0, "000"])
    f, p = plan_of(pr)
    ok(p["status"] == "install" and "fetch_run" not in p["steps"] and p["run_file"] == nv.RUN_FILENAME,
       "Pascal HORS-LIGNE avec installeur local → installe sans rien télécharger", "plan=%s %s" % (p["status"], p["steps"]))
    pr = with_(gpu_dev(t4, 0x1bb0, "GP104GL [Quadro P5000]"), net_run=[0, "000"], net_repo=[0, "000"])
    f, p = plan_of(pr)
    ok(p["status"] == "refused" and p["code"] == "no_source",
       "Pascal HORS-LIGNE sans installeur local → refus net « no_source » (inviter à téléverser)", "plan=%s" % p["code"])
    # une branche >= 590 ne sait pas piloter Pascal : un installeur local en 595 est écarté
    pr = with_(gpu_dev(t4, 0x1bb0, "GP104GL [Quadro P5000]"), runfiles="%s/NVIDIA-Linux-x86_64-595.58.03.run\n" % nv.DRIVER_DIR,
               net_run=[0, "000"])
    f, p = plan_of(pr)
    ok(p["code"] == "no_source", "Pascal : un installeur local en 595 (branche sans Pascal) n'est PAS choisi", "plan=%s" % p["params"])

    # ── Pascal avec un pilote 590+ (repo NVIDIA) : il ne le pilote plus → retour 580 ─────────────
    pr = with_(p5, smi=[0, "Quadro P5000, 595.58.03, 6.1\n"])
    f, p = plan_of(pr)
    ok(p["code"] == "driver_too_new" and p["method"] == "run" and p["status"] == "upgrade",
       "Pascal + pilote 595 → « trop récent pour ce GPU », retour à la branche 580", "plan=%s" % p["code"])

    # ── Turing+ : dépôt NVIDIA, modules ouverts ; repli .run ouvert si le dépôt est injoignable ──
    f, p = plan_of(gpu_dev(t4, 0x1eb8, "TU104GL [Tesla T4]"))
    ok(p["method"] == "repo-open" and p["modules"] == "open" and p["status"] == "install",
       "T4 neuf → dépôt NVIDIA, modules ouverts", "plan=%s" % p["method"])
    f, p = plan_of(gpu_dev(t4, 0x1eb8, "TU104GL [Tesla T4]"))
    ok("repo_setup" in p["steps"] and "install_run" not in p["steps"], "T4 neuf → pas d'installeur .run", "%s" % p["steps"])
    f, p = plan_of(gpu_dev(t4, 0x1f02, "TU106 [GeForce RTX 2070]"))
    ok(f["gpus"][0]["arch"] == "turing" and p["method"] == "repo-open", "RTX 2070 (TU106) → Turing → dépôt", "")
    f, p = plan_of(gpu_dev(t4, 0x1ff2, "TU117GL [Quadro T400 4GB]"))
    ok(f["gpus"][0]["arch"] == "turing", "Quadro T400 → Turing", "")
    f, p = plan_of(gpu_dev(t4, 0x1e30, "TU102GL [Quadro RTX 6000/8000]"))
    ok(f["gpus"][0]["arch"] == "turing" and p["method"] == "repo-open", "Quadro RTX 6000 (TU102) → Turing → dépôt", "")
    f, p = plan_of(gpu_dev(t4, 0x1eb1, "TU104GL [Quadro RTX 4000]"))
    ok(f["gpus"][0]["arch"] == "turing" and p["method"] == "repo-open", "Quadro RTX 4000 (TU104) → Turing → dépôt", "")
    f, p = plan_of(gpu_dev(t4, 0x2204, "GA102 [GeForce RTX 3090]"))
    ok(f["gpus"][0]["arch"] == "ampere" and p["method"] == "repo-open", "RTX 3090 → Ampere → dépôt", "")
    pr = with_(gpu_dev(t4, 0x1eb8, "TU104GL [Tesla T4]"), net_repo=[0, "404"], runfiles="%s/%s\n" % (nv.DRIVER_DIR, nv.RUN_FILENAME),
               net_run=[0, "000"])
    f, p = plan_of(pr)
    ok(p["method"] == "run" and p["modules"] == "open" and p["params"].get("fallback") == "repo_unavailable",
       "T4 sans dépôt joignable mais avec installeur local → .run en modules OUVERTS (repli annoncé)", "plan=%s" % p["params"])

    # ── Repli par identifiant PCI quand pci.ids ne connaît pas la carte ───────────────────────────
    f, p = plan_of(gpu_dev(t4, 0x2bb1, "Device 2bb1"))
    ok(f["gpus"][0]["arch"] == "blackwell" and f["gpus"][0]["arch_source"] == "pci-id-range" and p["method"] == "repo-open",
       "carte trop récente pour pci.ids → classée par plage d'identifiant PCI (Blackwell)", "gpu=%s" % f["gpus"])
    f, p = plan_of(gpu_dev(t4, 0x7777, "Device 7777"))
    ok(p["status"] == "refused" and p["code"] == "gpu_unknown", "identifiant totalement inconnu → refus « gpu_unknown »", "plan=%s" % p["code"])

    # ── Kepler et antérieur : refusé ─────────────────────────────────────────────────────────────
    f, p = plan_of(gpu_dev(t4, 0x1180, "GK104 [GeForce GTX 680]"))
    ok(f["gpus"][0]["arch"] == "kepler" and p["status"] == "refused" and p["code"] == "gpu_too_old" and not p["steps"],
       "Kepler (GTX 680) → refusé, aucune étape", "plan=%s" % p)
    f, p = plan_of(gpu_dev(t4, 0x06c0, "GF100 [GeForce GTX 480]"))
    ok(p["status"] == "refused" and p["code"] == "gpu_too_old", "Fermi → refusé", "plan=%s" % p["code"])
    # carte mixte : la plus ANCIENNE décide
    pr = gpu_dev(t4, 0x1eb8, "TU104GL [Tesla T4]")
    pr["pci"] = [0, pr["pci"][1] + "0000:04:00.0 0x10de 0x1bb0 0x030000\n"]
    pr["lspci"] = [0, pr["lspci"][1] + "04:00.0 VGA compatible controller [0300]: NVIDIA Corporation GP104GL [Quadro P5000] [10de:1bb0] (rev a1)\n"]
    f, p = plan_of(pr)
    ok(p["method"] == "run" and p["modules"] == "proprietary", "Turing + Pascal dans la même machine → la méthode qui supporte les DEUX (.run propriétaire)", "plan=%s" % p["method"])

    # ── Secure Boot ──────────────────────────────────────────────────────────────────────────────
    pr = with_(t4, secureboot="SecureBoot enabled\n")
    f, p = plan_of(pr)
    ok(f["secure_boot"] is True and p["status"] == "refused" and p["code"] == "secure_boot" and not p["steps"],
       "Secure Boot activé (installation nécessaire) → refus net, rien n'est modifié", "plan=%s" % p["code"])
    pr = with_(p5, secureboot="SecureBoot enabled\n")
    f, p = plan_of(pr)
    ok(p["status"] == "ok", "Secure Boot activé MAIS pilote déjà conforme et chargé → OK, pas de refus gratuit", "plan=%s" % p["status"])
    pr = with_(t4, secureboot="no-efi\n")
    ok(plan_of(pr)[0]["secure_boot"] is False, "pas d'EFI → Secure Boot « désactivé »", "")
    pr = with_(t4, secureboot=[1, "\n 1\n"])
    ok(plan_of(pr)[0]["secure_boot"] is True, "mokutil absent : l'octet EFI (1) est lu → activé", "")
    pr = with_(t4, secureboot="SecureBoot disabled\n")
    ok(plan_of(pr)[0]["secure_boot"] is False, "mokutil « disabled » → désactivé", "")

    # ── Cas annexes ──────────────────────────────────────────────────────────────────────────────
    pr = with_(p5, smi=[127, "bash: nvidia-smi : commande introuvable"], procver="", modules="\n".join(
        m for m in p5["modules"][1].splitlines() if not m.startswith("nvidia")))
    f, p = plan_of(pr)
    ok(p["status"] == "pending_reboot" and not p["steps"], "pilote installé (580) mais non chargé → « redémarrage en attente », rien à relancer", "plan=%s" % p["status"])
    pr = with_(p5, ctk="", docker='{"runc":{}}')
    f, p = plan_of(pr)
    ok(p["status"] == "toolkit_only" and p["steps"] == ["toolkit", "docker_runtime"] and not p["needs_reboot"] and not p["purge"],
       "pilote conforme mais toolkit absent → seulement toolkit + runtime docker, sans redémarrage ni toucher au pilote", "plan=%s %s" % (p["status"], p["steps"]))
    pr = with_(p5, docker='{"runc":{}}')
    f, p = plan_of(pr)
    ok(p["status"] == "toolkit_only" and p["steps"] == ["docker_runtime"], "toolkit présent, runtime docker absent → seulement le runtime", "%s" % p["steps"])
    pr = with_(t4, os="debian 13\nx86_64\n6.12.96+deb13-amd64\nNOAPT\n")
    ok(plan_of(pr)[1]["code"] == "no_apt", "apt absent → refus « no_apt »", "")
    pr = with_(t4, os="debian 13\naarch64\n6.12.96+deb13-arm64\nAPT\n")
    ok(plan_of(pr)[1]["code"] == "arch_unsupported", "architecture non x86_64 → refus", "")
    pr = with_(no_driver(t4), os="ubuntu 24.04\nx86_64\n6.8.0-x\nAPT\n", net_repo=[0, "200"])
    f, p = plan_of(pr)
    ok(p["method"] == "run" and p["params"].get("fallback") == "repo_os", "hors Debian → jamais le dépôt Debian de NVIDIA (repli .run)", "plan=%s" % p["params"])
    # la version de la distribution est LUE, jamais codée : un autre numéro de Debian suit la sonde réseau
    pr = with_(no_driver(t4), os="debian 14\nx86_64\n6.99.0-amd64\nAPT\n")
    f, p = plan_of(pr)
    ok(p["method"] == "repo-open", "Debian d'un autre numéro : le dépôt est tenté (la sonde réseau tranche), aucun numéro en dur", "plan=%s" % p["method"])
    ok(nv.repo_base("debian", "14").endswith("/debian14/x86_64") and "debian" not in nv.REPO_ROOT,
       "l'URL du dépôt dérive de os-release (le numéro de version n'est pas dans la constante)", "")

    # ── Reconnaissance des composants apt : le piège de la ligne CDROM commentée ──────────────────
    cdrom = "#deb cdrom:[Debian GNU/Linux 13 _Trixie_]/ trixie contrib main non-free-firmware\ndeb http://deb.debian.org/debian trixie main\n"
    ok(not nv.component_present(cdrom, "contrib"), "« contrib » dans une ligne COMMENTÉE ne compte pas", "")
    ok(nv.component_present("deb http://x trixie main non-free-firmware\n", "non-free-firmware"), "non-free-firmware reconnu", "")
    ok(not nv.component_present("deb http://x trixie main non-free-firmware\n", "non-free"),
       "« non-free » ne se reconnaît PAS dans « non-free-firmware »", "")
    ok(nv.component_present("Types: deb\nComponents: main contrib non-free\n", "non-free") and
       nv.component_present("Types: deb\nComponents: main contrib non-free\n", "contrib"), "format deb822 (Components:) reconnu", "")

    # ── Les paquets sont demandés à apt, avec la garde du paquet purement virtuel ─────────────────
    seen = []
    old_sh = nv._sh
    nv._sh = lambda cmd, timeout=60: (seen.append(cmd) or (1, ""))
    try:
        nv.apt_knows("dkms")
    finally:
        nv._sh = old_sh
    ok(seen and "apt-cache show dkms" in seen[0] and "^Package: dkms$" in seen[0],
       "apt_knows : « apt-cache show » + grep « ^Package: <nom>$ » (un virtuel renvoie 0 sans être réel)", "cmd=%s" % seen)

    # ── Génération : codename > compute_cap > plage PCI, et classes ───────────────────────────────
    ok(nv.arch_from_compute_cap("7.5") == "turing" and nv.arch_from_compute_cap("7.0") == "volta"
       and nv.arch_from_compute_cap("6.1") == "pascal" and nv.arch_from_compute_cap("5.2") == "maxwell"
       and nv.arch_from_compute_cap("3.5") == "kepler" and nv.arch_from_compute_cap("8.9") == "ada"
       and nv.arch_from_compute_cap("12.0") == "blackwell", "compute_cap → génération (7.5 Turing, 6.1 Pascal, 3.5 Kepler…)", "")
    ok(nv.ARCH_CLASS["volta"] == nv.ARCH_CLASS["maxwell"] == nv.ARCH_CLASS["pascal"] == "legacy"
       and nv.ARCH_CLASS["turing"] == "open" and nv.ARCH_CLASS["kepler"] == "refused",
       "classes : Maxwell/Pascal/Volta = .run 580 ; Turing+ = dépôt ; Kepler = refus", "")

    # ── Les libellés de l'interface existent pour chaque code/étape (rien ne s'affiche en clé brute) ─
    codes = set(re.findall(r"_plan\(\"(?:\w+)\", \"(\w+)\"", src)) | set(re.findall(r'reason = "(\w+)"', src))
    statuses = set(re.findall(r"_plan\(\"(\w+)\"", src)) | {"install", "upgrade"}
    for lang in ("fr", "en"):
        cat = json.load(open(os.path.join(R, "i18n", lang + ".json"), encoding="utf-8"))
        miss = [c for c in sorted(codes) if "settings.hostdrv.nvidia.plan." + c not in cat]
        miss += [s for s in sorted(statuses) if "settings.hostdrv.nvidia.status." + s not in cat]
        miss += [s for s in nv.STEP_ORDER if "settings.hostdrv.nvidia.step." + s not in cat]
        miss += [k for k in ("title", "run", "open", "license") if "settings.hostdrv.nvidia." + k not in cat]
        miss += ["row." + r["key"] for r in nv.rows_for_ui(*[(lambda f: (f, nv.decide(f)))(nv.gather_facts(lambda n: tuple(p5[n])))][0])
                 if "settings.hostdrv.nvidia.row." + r["key"] not in cat]
        ok(not miss, "[%s] chaque code de plan, statut, étape et ligne a son libellé" % lang, "manquants: %s" % miss)
    ok(set(nv.STEP_ORDER) == set(nv.STEPS), "chaque étape ordonnée a son implémentation", "")
    allsteps = set()
    for pr in (t4, gpu_dev(t4, 0x1bb0, "GP104GL [Quadro P5000]"), with_(p5, ctk="")):
        allsteps |= set(plan_of(pr)[1]["steps"])
    ok(allsteps <= set(nv.STEP_ORDER), "les plans n'emploient que des étapes connues", "inconnues: %s" % (allsteps - set(nv.STEP_ORDER)))
    # ── La boucle d'exécution (étapes REMPLACÉES par des espions : rien ne touche à la machine) ───
    import io
    import contextlib
    calls = []
    saved = dict(nv.STEPS)
    for name in nv.STEPS:
        nv.STEPS[name] = (lambda n: lambda plan, f: calls.append(n))(name)

    def drive(pr):
        calls.clear()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = nv.run(probe=lambda n: tuple(pr[n]))
        return rc, list(calls), buf.getvalue()
    try:
        if os.geteuid() == 0:
            rc, c, out = drive(p5)
            ok(rc == 0 and c == [], "run sur dell-1 (conforme) : code 0, AUCUNE étape exécutée", "rc=%s calls=%s" % (rc, c))
            rc, c, out = drive(with_(t4, secureboot="SecureBoot enabled\n"))
            ok(rc == 1 and c == [] and "REFUSED" in out, "run + Secure Boot : code 1, AUCUNE étape exécutée", "rc=%s calls=%s" % (rc, c))
            rc, c, out = drive(nogpu)
            ok(rc == 0 and c == [], "run sans GPU : code 0, aucune étape", "rc=%s" % rc)
            rc, c, out = drive(t4)
            ok(rc == nv.EXIT_REBOOT and c == nv.decide(nv.gather_facts(lambda n: tuple(t4[n])))["steps"]
               and "[step 1/" in out and "reboot required" in out,
               "run sur T4+Debian 550 : toutes les étapes dans l'ordre du plan, code 10 (redémarrage), journal numéroté", "rc=%s calls=%s" % (rc, c))
            ok(c.index("purge") < c.index("install_repo") < c.index("verify_module"), "purge → installation → vérification du module", "%s" % c)

            def boom(plan, f):
                calls.append("boom")
                raise nv.StepError("simulated")
            nv.STEPS["install_repo"] = boom
            rc, c, out = drive(t4)
            ok(rc == 1 and c[-1] == "boom" and "verify_module" not in c and "toolkit" not in c and "FAILED at step 'install_repo'" in out,
               "un échec d'étape arrête TOUT (pas de toolkit posé sur un pilote absent), code 1, étape nommée", "rc=%s calls=%s" % (rc, c))
            nv.STEPS["install_repo"] = lambda plan, f: calls.append("install_repo")
            rc, c, out = drive(with_(p5, ctk="", docker='{"runc":{}}'))
            ok(rc == 0 and c == ["toolkit", "docker_runtime"], "run sur toolkit manquant : code 0 (pas de redémarrage), seulement ces deux étapes", "rc=%s %s" % (rc, c))
    finally:
        nv.STEPS.update(saved)


    # ── install-node.sh délègue au même script, et n'installe plus le pilote Debian ──────────────
    sh = open(os.path.join(R, "node_agent", "install-node.sh"), encoding="utf-8").read()
    ok("host_drivers/nvidia.py" in sh and 'python3 "$_nv" run' in sh,
       "install-node.sh : provision_gpu appelle host_drivers/nvidia.py run", "")
    ok(not re.search(r"apt-get[^\n]*install[^\n]*\bnvidia-driver\b", sh),
       "install-node.sh n'installe plus « nvidia-driver » (Debian 550, trop vieux pour CUDA 12.8)", "")


if __name__ == "__main__":
    run()
    print()
    if ko:
        print("%d ÉCHEC(S)" % len(ko))
        sys.exit(1)
    print("tout est vert")
