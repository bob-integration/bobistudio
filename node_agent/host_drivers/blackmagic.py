#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Host driver kind `blackmagic` : install the Desktop Video DRIVER and update card FIRMWARE.

Two jobs, one kind, because they are the same question for the operator ("why does this node not
see my card?") and because they chain: without the driver there is no tool to read the firmware
with, and a freshly installed driver usually makes a card's firmware outdated.

WHY THIS EXISTS (2026-10-20). A DeckLink whose firmware does not match the installed driver is
enabled by the kernel — it populates /dev/blackmagic — but the SDK REFUSES to enumerate it. The
card is in the chassis, the operator sees nothing, and every layer above reports "no card". It
happened twice: at the first Duo 2 mount, and the day a Quad 2 was added next to a working Duo 2.
Both times the fix was one command on the host, which means it needed someone with a shell. A
tester should not have to wait for one.

Contract of a host-driver kind (see NODE_AGENT.md, "Host drivers"):
  state   print ONE JSON document on stdout:
            {kind, version, status, rows:[{key,value}], plan:{status,code,params,steps}, can_run}
          `code`/`params`/`key`/step ids are CODES, translated by the UI (never a sentence).
  run     do the work; progress goes to stdout/stderr as human-readable ENGLISH lines. Exit:
            0   done, nothing pending          10  done, a REBOOT is required
            11  done, a full POWER-OFF is required                           other  failure

★ TWO DIFFERENT ENDINGS, AND THEY ARE NOT INTERCHANGEABLE. Installing the driver needs a REBOOT
  (the kernel modules are built by DKMS and must be loaded). Flashing firmware needs a full
  POWER-OFF. When a run does both, the power-off wins: it is the stronger of the two, and a
  reboot afterwards would be a second interruption for nothing.

★ FOR THE FIRMWARE STEP, EXIT 11, NEVER 10. Blackmagic's own tool says it: "Please shut down your
  computer to complete the software update." A warm reboot does not re-load the card's flash —
  the card has to lose power. Reporting 10 here would send the operator through a reboot that
  changes nothing, and the card would come back still out of date.

★ AND `--list` KEEPS SAYING "out of date" UNTIL THAT POWER-OFF, because it reads the firmware
  that is RUNNING, not the one that is stored. Measured on the Quad 2: the update completed, the
  tool said so, and `--list` was unchanged. Without the boot marker below, `state` would still
  say "update needed" right after a successful run, and an operator would flash in a loop.

Only the standard library. `decide` is pure: it takes the facts gathered by `gather_facts`
(which reads the host through one injectable `probe`), so tests replay real probes.
"""
import json
import os
import re
import subprocess
import sys

KIND = "blackmagic"
SCRIPT_VERSION = "1.3.0"

EXIT_OK, EXIT_REBOOT, EXIT_SHUTDOWN = 0, 10, 11

TOOL = "DesktopVideoUpdateTool"
# Marqueur « mise à jour faite, en attente de coupure », porté par l'identifiant de démarrage :
# il se périme TOUT SEUL au prochain boot, sans rien à nettoyer. Un fichier daté, lui, aurait
# survécu à la coupure et caché une carte réellement périmée.
STATE_DIR = os.environ.get("BOBI_HOST_DRIVERS_STATE", "/var/lib/bobi-node-agent/host-drivers")
MARKER = os.path.join(STATE_DIR, "blackmagic.flashed")
# Where the agent stores what the operator uploads. THIS directory is what makes the install
# possible on a node with no Internet access, which is the normal case here. The archive itself
# is fetched by hand from their site: a product registration form opens, and its "Download only"
# link is enough — no account, no agreement (this comment claimed both until 2026-10-31).
FILES_DIR = os.path.join(os.environ.get("BOBI_HOST_DRIVERS_FILES",
                                        "/var/lib/bobi-node-agent/drivers"), KIND)
# Ce qu'on sait reconnaître dans ce dossier. L'archive officielle est un .tar.gz ; un .deb seul
# est accepté aussi, parce que c'est ce qu'un exploitant extrait parfois lui-même.
ARCHIVES = (".tar.gz", ".tgz", ".tar")
# How the driver's own .deb is recognised: that is the name Blackmagic gives it inside the
# archive, and `installer_pilote` installs nothing else. Any OTHER .deb dropped in the same
# directory is a prerequisite carried in by hand (cf. prereq_debs) — a node with no outbound
# access cannot apt-get, which is the normal case here. Until 1.3.0 `installable()` answered
# « the driver to install » for a dropped `libgl1_*.deb`.
DRIVER_DEB = "desktopvideo"

PROBES = {
    "list":    "%s --list 2>&1" % TOOL,
    "tool":    "command -v %s 2>/dev/null" % TOOL,
    "package": "dpkg-query -W -f='${Version}' desktopvideo 2>/dev/null",
    "devdir":  "ls -1 /dev/blackmagic/ 2>/dev/null | wc -l",
    "modules": "grep -c '^blackmagic' /proc/modules 2>/dev/null",
    "boot_id": "cat /proc/sys/kernel/random/boot_id 2>/dev/null",
    "files":   "ls -1 %s 2>/dev/null" % FILES_DIR,
    "dkms":    "command -v dkms 2>/dev/null",
    "headers": "ls -d /lib/modules/$(uname -r)/build 2>/dev/null",
    # ★ DEUX SONDES AJOUTÉES LE 2026-11-13, APRÈS UN ÉCHEC EN VRAI. `headers` ci-dessus mesure ce
    #   dont DKMS a besoin pour compiler — et c'est bien ce qu'il faut. Mais ce n'est pas ce que
    #   DPKG exige : le paquet desktopvideo dépend d'une ALTERNATIVE de méta-paquets
    #   (`linux-headers-amd64 | linux-headers-arm64 | …`), qu'un `linux-headers-6.12.90+deb13-amd64`
    #   installé seul ne satisfait PAS. Résultat mesuré : la page affichait « kernel headers yes »
    #   et dpkg répondait « Le paquet linux-headers-amd64 n'est pas installé », en laissant
    #   desktopvideo non configuré. Une sonde qui mesure autre chose que la condition réelle est
    #   pire qu'une sonde absente : elle affirme.
    #   `libgl1` est l'autre dépendance déclarée, qu'on n'installait pas du tout.
    "headers_meta": "dpkg-query -W -f='${Status}' linux-headers-$(dpkg --print-architecture) "
                    "2>/dev/null | grep -q 'install ok installed'",
    "libgl":   "dpkg-query -W -f='${Status}' libgl1 2>/dev/null | grep -q 'install ok installed'",
}


def _sh(cmd, timeout=120):
    try:
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + ("" if p.returncode == 0 else (p.stderr or ""))
    except Exception as e:                              # noqa: BLE001
        return 255, str(e)


def prereq_packages():
    """What the Blackmagic package needs in order to build: DKMS and the RUNNING kernel's headers.

    The headers are version-pinned: `linux-headers-6.12.43+deb13-amd64`, not a meta-package. A
    node booted on another kernel than the one installed last needs the headers of the kernel it
    is running NOW, which is why this is computed and not written down.
    """
    # Les DEUX formes d'en-têtes sont nécessaires, et pour deux raisons différentes :
    #   - `linux-headers-<version>` : ce contre quoi DKMS compile, pour le noyau EN COURS ;
    #   - `linux-headers-<arch>` (méta) : ce que la dépendance du paquet desktopvideo réclame.
    # Poser seulement le premier donnait un écran vert et un dpkg qui refuse (mesuré le 13/11).
    arch = _architecture_noeud() or "amd64"
    return ["dkms", "libgl1", "linux-headers-%s" % os.uname().release, "linux-headers-%s" % arch]


def install_prereqs(sh=None, fichiers=None, probe=None):
    """Install the prerequisites. Returns (ok, message).

    ★ THE SCREEN THAT DIAGNOSED AND STOPPED (2026-11-13). `run()` used to answer « Nothing to do:
      install_prereqs_missing » and return success, while the page showed « dkms no, kernel
      headers no » with no button to press. The product knew the exact name of what was missing,
      knew how to install it, and left the operator to find that out elsewhere — on a screen whose
      whole purpose is installing this driver. Diagnosing without offering the act is not caution,
      it is an unfinished feature: the operator asked for the driver, and the prerequisites are
      part of installing the driver.
      A node with no outbound access cannot apt-get. That is a real case here (nodes are often
      kept off the Internet), and it is why the failure NAMES the two packages: they can be
      carried in by hand. What it must never do is fail without saying which.

    ★ AND THE NODE WITH NO INTERNET MUST BE ABLE TO GET THERE TOO (2026-11-14). The .deb dropped
      in the upload directory are installed FIRST, with no network, by the same
      `apt-get install ./…` that resolves them against each other. APT is tried afterwards only
      for what is still missing — and on an offline node `apt-get update` only spends its
      timeout. Which is why this re-reads the facts when it can (`probe`) instead of assuming.
    """
    sh = sh or _sh
    pkgs = prereq_packages()
    deposes = prereq_debs(fichiers)
    if deposes:
        chemins = " ".join(os.path.join(FILES_DIR, x) for x in deposes)
        say("Installing %d prerequisite package(s) dropped on this node: %s"
            % (len(deposes), ", ".join(deposes)))
        rc_l, out_l = sh("DEBIAN_FRONTEND=noninteractive apt-get install -y "
                         "--no-install-recommends %s 2>&1" % chemins, timeout=1800)
        for ligne in (out_l or "").splitlines()[-12:]:
            if ligne.strip():
                say("  " + ligne.strip())
        if rc_l != 0:
            say("  the dropped package(s) did not install (exit %d)" % rc_l)
        if probe is not None:
            reste = missing_prereqs(gather_facts(probe))
            if not reste:
                say("Prerequisites satisfied from the dropped package(s) - APT was not needed.")
                return True, "prerequisites installed from the dropped packages"
            say("Still missing after the dropped package(s): %s" % ", ".join(reste))
    say("Installing prerequisites: %s" % ", ".join(pkgs))
    sh("DEBIAN_FRONTEND=noninteractive apt-get update 2>&1 | tail -3", timeout=300)
    rc, out = sh("DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends %s "
                 "2>&1 | tail -20" % " ".join(pkgs), timeout=1800)
    for ligne in (out or "").splitlines()[-12:]:
        if ligne.strip():
            say("  " + ligne.strip())
    if rc == 0:
        return True, "prerequisites installed"
    msg = ("could not install %s (apt exit %d). If this node has no outbound access, build that "
           "set on a CONNECTED Debian of the same release - `mkdir -p d/partial && apt-get "
           "install -y --reinstall --download-only -o Dir::Cache::archives=\"$PWD/d\" %s` - then "
           "drop the .deb in the panel's upload field (they land in %s) and run this again. The "
           "headers must match the running kernel exactly (%s)."
           % (", ".join(pkgs), rc, " ".join(pkgs), FILES_DIR, os.uname().release))
    say("FAILED - " + msg)
    return False, msg


def live_probe(name):
    return _sh(PROBES[name])


# ─── lecture ──────────────────────────────────────────────────────────────────────────────────────
# Sortie de l'outil, relevée le 2026-10-20 sur dell-1 :
#
#   Available Devices:
#    * DeckLink Duo 2 (Gf0d8aa00)
#        Firmware is up to date
#    * DeckLink Quad 2 (Gf0aaa170)
#        Firmware is out of date
#
# Le modèle et l'identifiant sont sur la ligne « * », l'état sur la SUIVANTE. On ne devine pas
# l'état depuis le modèle : deux cartes du même modèle peuvent diverger.
_LIGNE_CARTE = re.compile(r"^\s*\*\s+(.+?)\s+\(([A-Za-z0-9]+)\)\s*$")


def parse_list(sortie):
    cartes, courante = [], None
    for ligne in (sortie or "").splitlines():
        m = _LIGNE_CARTE.match(ligne)
        if m:
            courante = {"model": m.group(1).strip(), "id": m.group(2), "state": "unknown"}
            cartes.append(courante)
            continue
        if courante is None:
            continue
        bas = ligne.strip().lower()
        if "up to date" in bas:
            courante["state"] = "ok"
        elif "out of date" in bas:
            courante["state"] = "outdated"
    return cartes


def gather_facts(probe=live_probe):
    rc_tool, tool = probe("tool")
    rc_list, liste = probe("list")
    _, paquet = probe("package")
    _, devdir = probe("devdir")
    _, mods = probe("modules")
    _, boot = probe("boot_id")
    _, fichiers = probe("files")
    rc_dkms, _dk = probe("dkms")
    rc_hdr, _hd = probe("headers")
    rc_meta, _mt = probe("headers_meta")
    rc_gl, _gl = probe("libgl")
    try:
        noeuds = int((devdir or "0").strip() or 0)
    except ValueError:
        noeuds = 0
    try:
        modules = int((mods or "0").strip() or 0)
    except ValueError:
        modules = 0
    return {"tool": bool((tool or "").strip()) and rc_tool == 0,
            "cards": parse_list(liste) if rc_list == 0 else [],
            "list_rc": rc_list,
            "package": (paquet or "").strip() or None,
            "dev_nodes": noeuds,
            "modules": modules,
            "boot_id": (boot or "").strip(),
            "files": [x.strip() for x in (fichiers or "").splitlines() if x.strip()],
            "dkms": rc_dkms == 0,
            "headers": rc_hdr == 0,
            "headers_meta": rc_meta == 0,
            "libgl": rc_gl == 0,
            "flashed_boot": _marqueur_lu()}


def installable(fichiers):
    """Le fichier déposé qu'on sait installer, ou None. On NE DEVINE PAS la version depuis le
    nom : l'archive porte des .deb, et c'est dpkg qui tranche. Le nom ne sert qu'à choisir."""
    for f in sorted(fichiers or [], reverse=True):      # le plus récent d'abord, par tri du nom
        bas = f.lower()
        # ★ UNE ARCHIVE DÉJÀ POSÉE EST ÉCARTÉE. Sans ça elle reste dans le dossier et le pilote
        #   propose de l'installer indéfiniment : après une première pose, l'état revenait à
        #   « à installer » au lieu de passer au micrologiciel, et la séquence pilote → flash
        #   ne se terminait jamais. Trouvé par la garde, pas à la lecture.
        if bas.endswith(".installed"):
            continue
        if bas.endswith(ARCHIVES):
            return f
        if bas.endswith(".deb") and bas.startswith(DRIVER_DEB):
            return f
    return None


def prereq_debs(fichiers):
    """Prerequisite .deb dropped on the node, in the SAME upload directory as the archive.

    ★ NAMING WHAT IS MISSING IS NOT THE SAME AS LETTING SOMEONE BRING IT (2026-11-14). The
      failure message listed the packages, which tells an operator WHAT to carry in; nothing in
      the product accepted it. On a node kept off the Internet — the normal case here — that
      left exactly one route: a shell on the host, which is what this whole screen exists to
      avoid. The upload door already existed for the archive, so it takes these too.
    """
    out = []
    for f in sorted(fichiers or []):
        bas = f.lower()
        if bas.endswith(".installed") or not bas.endswith(".deb") or bas.startswith(DRIVER_DEB):
            continue
        out.append(f)
    return out


def missing_prereqs(f):
    """What is missing, as CODES for the UI. One definition, read by `decide` and by the install:
    two lists would drift, and the one that drifted would be the one the operator reads."""
    return [n for n, ok in (("dkms", f["dkms"]), ("headers", f["headers"]),
                            ("headers meta-package", f.get("headers_meta", True)),
                            ("libgl1", f.get("libgl", True))) if not ok]


def _marqueur_lu():
    try:
        with open(MARKER) as f:
            return f.read().strip()
    except OSError:
        return ""


def _marqueur_pose(boot_id):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = MARKER + ".tmp"
        with open(tmp, "w") as f:
            f.write(boot_id or "?")
        os.replace(tmp, MARKER)
    except OSError as e:                                # noqa: BLE001
        say("WARNING: could not write the pending-power-off marker (%s) - the panel may offer "
            "the update again before the power cycle" % e)


# ─── décision ─────────────────────────────────────────────────────────────────────────────────────
def _plan(status, code, **kw):
    return {"status": status, "code": code, "params": kw.pop("params", {}),
            "steps": kw.pop("steps", []), "cards": kw.pop("cards", [])}


def decide(f):
    """Facts -> plan. status in:
    install | install_needs_file | missing_prereqs | no_tool | no_card | ok | pending_shutdown
    | update | unreadable."""
    # ★ L'ABSENCE DE PILOTE PASSE AVANT TOUT. Sans lui il n'y a ni outil, ni /dev/blackmagic, ni
    #   la moindre information sur les cartes — et c'est la situation d'un nœud neuf. La traiter
    #   en dernier reviendrait à répondre « aucune carte » à quelqu'un qui vient d'en poser une.
    if not f["package"]:
        fichier = installable(f["files"])
        if not fichier:
            # On DEMANDE le fichier au lieu de tenter un téléchargement : l'archive Blackmagic
            # n'est pas récupérable sans accepter un contrat sur leur site, et le nœud peut
            # n'avoir aucun accès sortant. C'est le cas NORMAL, pas une erreur.
            return _plan("install_needs_file", "driver_missing_upload_needed")
        manque = missing_prereqs(f)
        if manque:
            # Le paquet Blackmagic se construit par DKMS contre les en-têtes du noyau courant.
            # Sans eux, `dpkg -i` laisse un paquet « configuré à moitié » et aucun module. On le
            # DIT au lieu de lancer une installation qui échouera à mi-chemin.
            # `steps` n'est pas décoratif : c'est lui qui dit à l'écran qu'un geste existe.
            # Sans lui la page affichait un constat et aucun bouton.
            return _plan("missing_prereqs", "install_prereqs_missing",
                         params={"missing": ", ".join(manque), "file": fichier,
                                 "packages": " ".join(prereq_packages())},
                         steps=[{"id": "prereqs",
                                 "params": {"packages": " ".join(prereq_packages())}},
                                {"id": "install", "params": {"file": fichier}}])
        return _plan("install", "driver_install_from_file", params={"file": fichier},
                     steps=[{"id": "install", "params": {"file": fichier}}])
    # ★ IL N'EXISTE PAS DE « FICHIER DE MICROLOGICIEL » CHEZ BLACKMAGIC (vérifié le 20/10) :
    #   les images de flash voyagent DANS le paquet du pilote
    #   (`/usr/lib/blackmagic/DesktopVideo/Firmware/data-*.bin`). Obtenir un micrologiciel plus
    #   récent, c'est donc installer un PILOTE plus récent — et pas importer un fichier à part.
    #   D'où : on accepte un dépôt d'archive même quand un pilote est déjà là, sinon la seule
    #   façon de mettre à jour un micrologiciel serait de désinstaller le pilote d'abord.
    _fichier = installable(f["files"])
    if _fichier:
        return _plan("upgrade", "driver_upgrade_from_file",
                     params={"file": _fichier, "installed": f["package"]},
                     steps=[{"id": "install", "params": {"file": _fichier}}])
    if not f["tool"]:
        # Paquet présent mais outil absent : installation incomplète, pas « aucune carte ».
        return _plan("no_tool", "tool_missing")
    if f["list_rc"] != 0:
        return _plan("unreadable", "list_failed")

    perimees = [c for c in f["cards"] if c["state"] == "outdated"]

    # L'ordre compte : le marqueur passe AVANT « périmée », sinon une carte flashée mais pas
    # encore coupée s'annonce à mettre à jour, et on reflashe en rond.
    if perimees and f["flashed_boot"] and f["flashed_boot"] == f["boot_id"]:
        return _plan("pending_shutdown", "flashed_awaiting_power_off",
                     params={"cards": ", ".join(c["model"] for c in perimees)}, cards=perimees)
    if not f["cards"]:
        if f["dev_nodes"] > 0:
            # Le noyau voit du matériel que l'outil ne liste pas : ce désaccord EST l'information.
            return _plan("unreadable", "kernel_sees_cards_tool_does_not",
                         params={"nodes": f["dev_nodes"]})
        return _plan("no_card", "no_card_present")
    if not perimees:
        return _plan("ok", "all_up_to_date",
                     params={"count": len(f["cards"])})
    return _plan("update", "firmware_outdated",
                 params={"cards": ", ".join("%s (%s)" % (c["model"], c["id"]) for c in perimees)},
                 steps=[{"id": "update", "params": {"card": c["model"]}} for c in perimees],
                 cards=perimees)


def rows_for_ui(f, plan):
    rows = []
    for c in f["cards"]:
        rows.append({"key": "card", "value": "%s (%s) - %s" % (c["model"], c["id"], c["state"])})
    if not f["cards"]:
        rows.append({"key": "card", "value": "-"})
    rows.append({"key": "package", "value": f["package"] or "-"})
    rows.append({"key": "files", "value": ", ".join(f["files"]) or "-"})
    # ★ THE ROW NAMED TWO CONDITIONS OUT OF FOUR (2026-11-13), and the two it left out were
    #   exactly the ones dpkg refused on. The screen read « dkms yes, kernel headers yes » while
    #   the install failed on `libgl1` and the headers meta-package. A status row that shows a
    #   subset of what is checked does not reassure, it misleads.
    rows.append({"key": "prereqs", "value": ", ".join(
        "%s %s" % (nom, "yes" if ok else "no")
        for nom, ok in (("dkms", f["dkms"]), ("kernel headers", f["headers"]),
                        ("headers meta-package", f.get("headers_meta", True)),
                        ("libgl1", f.get("libgl", True))))})
    rows.append({"key": "kernel", "value": "%d module(s), %d device node(s)"
                 % (f["modules"], f["dev_nodes"])})
    return rows


def state(probe=live_probe):
    f = gather_facts(probe)
    plan = decide(f)
    return {"kind": KIND, "version": SCRIPT_VERSION, "status": plan["status"], "plan": plan,
            "rows": rows_for_ui(f, plan), "facts": f,
            "can_run": plan["status"] in ("update", "install", "upgrade", "missing_prereqs"),
            # Ce que l'interface doit proposer de déposer, et où ça atterrit. `install_needs_file`
            # est le SEUL état où l'absence de fichier EST le problème : c'est là que la zone de
            # dépôt doit apparaître, pas en permanence.
            # Le dépôt reste offert MÊME QUAND TOUT VA BIEN : c'est le seul chemin pour amener
            # un micrologiciel plus récent, puisqu'il arrive dans le paquet du pilote.
            # `missing_prereqs` en fait partie depuis 1.3.0 : c'est l'état où un nœud hors
            # ligne a quelque chose à déposer, et le seul où la zone de dépôt attend autre chose
            # que l'archive.
            "wants_upload": plan["status"] in ("install_needs_file", "ok", "update", "no_tool",
                                               "missing_prereqs"),
            "upload_dir": FILES_DIR,
            "upload_hint": ("dkms_*.deb, libgl1_*.deb, linux-headers-*.deb"
                            if plan["status"] == "missing_prereqs"
                            else "Blackmagic_Desktop_Video_Linux_<version>.tar.gz")}


# ─── exécution ────────────────────────────────────────────────────────────────────────────────────
def say(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _architecture_noeud():
    """L'architecture dpkg de CE nœud, pas celle qu'on suppose. `dpkg --print-architecture`
    est la seule source : `uname -m` dit `x86_64` là où dpkg dit `amd64`, et comparer les deux
    vocabulaires est exactement la faute qu'on corrige."""
    rc, out = _sh("dpkg --print-architecture", timeout=30)
    return (out or "").strip() if rc == 0 else ""


def _filtrer_architecture(debs):
    """Partage les .deb trouvés en (à poser, écartés) selon l'architecture du nœud.

    Nommage Debian : `nom_version_arch.deb`. `all` est accepté — un paquet indépendant de
    l'architecture est posable partout. Si l'architecture du nœud est INCONNUE (dpkg muet), on
    ne jette rien : mieux vaut laisser dpkg trancher que d'écarter le bon paquet sur une
    comparaison faite à vide.
    """
    arch = _architecture_noeud()
    if not arch:
        return list(debs), []
    gardes, ecartes = [], []
    for d in debs:
        base = os.path.basename(d)
        suffixe = base[:-4].rsplit("_", 1)[-1] if base.lower().endswith(".deb") else ""
        (gardes if suffixe in (arch, "all") else ecartes).append(d)
    return gardes, ecartes


def installer_pilote(fichier):
    """Pose le paquet Desktop Video depuis le fichier DÉPOSÉ, sans aucun accès réseau.

    ★ ON NE DEVINE PAS LE CHEMIN DANS L'ARCHIVE. Blackmagic a déjà changé l'arborescence de ses
      tarballs entre deux versions ; coder « deb/x86_64/… » en dur, c'est se préparer un échec
      silencieux à la prochaine. On extrait, puis on CHERCHE le .deb, et on dit ce qu'on a trouvé.

    ★ EXTRACTION DANS UN DOSSIER NEUF (`mktemp -d`), jamais par-dessus un dossier qu'on effacerait
      d'abord : ce script tourne en root sur un nœud, et un effacement récursif dont le chemin se
      construit par concaténation n'a pas sa place ici.
    """
    chemin = os.path.join(FILES_DIR, fichier)
    if fichier.lower().endswith(".deb"):
        debs = [chemin]
    else:
        rc, dest = _sh("mktemp -d -t bobi-bmd-XXXXXXXX", timeout=30)
        dest = (dest or "").strip()
        if rc != 0 or not dest:
            say("FAILED: could not create a temporary directory")
            return False
        rc, out = _sh("tar -xf %s -C %s 2>&1" % (chemin, dest), timeout=600)
        if rc != 0:
            say("FAILED to extract %s: %s" % (fichier, (out or "").strip()[:400]))
            return False
        # Le paquet GUI tire des dépendances graphiques dont un nœud n'a que faire : on ne pose
        # QUE `desktopvideo`. Sur une machine sans serveur X, l'autre échouerait et ferait passer
        # une installation réussie pour un échec.
        rc, out = _sh("find %s -name 'desktopvideo_*.deb' | sort" % dest, timeout=120)
        debs = [x.strip() for x in (out or "").splitlines() if x.strip()]
    if not debs:
        say("FAILED: no desktopvideo_*.deb found in %s" % fichier)
        return False

    # ★★ L'ARCHIVE PORTE PLUSIEURS ARCHITECTURES, ET ON LES PASSAIT TOUTES À `dpkg` (25/10).
    #    Vécu par le testeur sur la Z8 avec l'archive officielle 16.4 : « ce n'est pas la bonne
    #    version, ce n'est pas de l'amd64 ». Un seul paquet étranger dans la ligne de commande
    #    fait refuser dpkg, donc le paquet amd64 présent juste à côté n'était pas posé non plus.
    #    On garde l'architecture du NŒUD, jamais celle qu'on espère — et `all` est légitime.
    debs, ecartes = _filtrer_architecture(debs)
    if not debs:
        say("FAILED: none of the packages found match this node's architecture (%s)"
            % _architecture_noeud())
        for d in ecartes:
            say("  found: %s" % os.path.basename(d))
        return False
    for d in ecartes:
        say("Skipping %s (other architecture)" % os.path.basename(d))

    say("Installing %s" % ", ".join(os.path.basename(d) for d in debs))
    # ⚠ PAS DE TUBE ICI. `dpkg -i … | tail -40` rend le code de `tail`, soit 0 QUOI QU'IL
    #   ARRIVE : l'échec de dpkg était donc annoncé comme une réussite, et l'archive était
    #   renommée `.installed` juste en dessous — une reprise ne trouvait plus rien à poser.
    #   C'est ce qui rendait la panne de la Z8 muette alors que le message d'erreur de dpkg
    #   s'affichait dans le journal du job. On tronque en Python, après avoir lu le code.
    # ★★ `dpkg -i` NE RÉSOUT AUCUNE DÉPENDANCE (mesuré le 2026-11-13, en vrai, sur un nœud).
    #    Il dépaquette, constate que `libgl1` et le méta-paquet d'en-têtes manquent, et laisse
    #    desktopvideo « non configuré » — c'est-à-dire présent pour `dpkg-query` mais sans aucun
    #    module construit. La sonde `package` le voyait alors comme INSTALLÉ, et l'écran passait
    #    au plan suivant sur une installation qui n'avait rien installé.
    #    `apt-get install ./x.deb` pose le paquet LOCAL et tire ses dépendances depuis le dépôt.
    #    Le repli `dpkg -i` + `apt-get -f install` existe pour les apt trop anciens pour la forme
    #    locale ; il réalise la même chose en deux temps.
    #    ⚠ PAS DE TUBE : `… | tail` rendrait le code de `tail`, soit 0 quoi qu'il arrive.
    locaux = " ".join(d if d.startswith("/") else "./" + d for d in debs)
    rc, out = _sh("DEBIAN_FRONTEND=noninteractive apt-get install -y "
                  "--no-install-recommends %s 2>&1" % locaux, timeout=1800)
    if rc != 0:
        say("  apt-get could not install the local package(s) (exit %d), retrying with dpkg + "
            "dependency repair" % rc)
        rc_d, out_d = _sh("dpkg -i %s 2>&1" % " ".join(debs), timeout=1800)
        rc_f, out_f = _sh("DEBIAN_FRONTEND=noninteractive apt-get -f install -y 2>&1", timeout=1800)
        out = (out or "") + "\n" + (out_d or "") + "\n" + (out_f or "")
        rc = rc_f if rc_d != 0 else rc_d
    lignes = [l.strip() for l in (out or "").splitlines() if l.strip()]
    for ligne in lignes[-40:]:
        say("  " + ligne)
    if rc != 0:
        say("FAILED: the package could not be configured (exit %d). Its declared dependencies "
            "are libgl1 and one of the linux-headers meta-packages; a node with no access to its "
            "APT repository cannot pull them, and dpkg alone will not." % rc)
        return False
    # ★ UN PAQUET « NON CONFIGURÉ » N'EST PAS UN PAQUET INSTALLÉ. Sans ce contrôle, la sonde
    #   `package` (qui lit la VERSION) rendait un numéro pour un paquet à moitié posé, et tout
    #   l'écran raisonnait ensuite sur une installation qui n'avait pas eu lieu.
    rc_st, st = _sh("dpkg-query -W -f='${Status}' desktopvideo 2>/dev/null", timeout=60)
    if "install ok installed" not in (st or ""):
        say("FAILED: desktopvideo is unpacked but NOT configured (dpkg status: %s)"
            % (st or "unknown").strip())
        return False
    # Archive CONSOMMÉE : on la renomme plutôt que de l'effacer — elle dit quelle version a été
    # posée, et un dossier qui garde sa trace vaut mieux qu'un dossier vide dont personne ne
    # sait ce qui y est passé.
    _sh("mv -f %s %s.installed 2>/dev/null" % (chemin, chemin), timeout=60)
    return True


def run(probe=live_probe):
    f = gather_facts(probe)
    plan = decide(f)
    besoin_redemarrage = False

    # Les prérequis font PARTIE de l'installation demandée : on les pose, puis on relit les faits
    # et on reprend le cours normal. En cas d'échec on s'arrête ici — mais en ayant nommé ce qui
    # manque et comment l'apporter à la main (cf. install_prereqs).
    if plan["status"] == "missing_prereqs":
        ok, msg = install_prereqs(fichiers=f["files"], probe=probe)
        if not ok:
            return 1
        f = gather_facts(probe)
        plan = decide(f)
        if plan["status"] == "missing_prereqs":
            say("FAILED - prerequisites still missing after install: %s"
                % plan["params"].get("missing"))
            return 1

    if plan["status"] in ("install", "upgrade"):
        if not installer_pilote(plan["params"]["file"]):
            return 1
        besoin_redemarrage = True
        # Le paquet vient d'arriver : l'état d'AVANT ne vaut plus rien. On relit, car c'est très
        # souvent ici qu'on découvre un micrologiciel périmé — un pilote neuf ne s'entend pas
        # forcément avec la flash d'une carte posée il y a deux ans.
        f = gather_facts(probe)
        plan = decide(f)

    if plan["status"] != "update":
        if besoin_redemarrage:
            say("")
            say("DONE - driver installed. A REBOOT is required to load the kernel modules.")
            return EXIT_REBOOT
        say("Nothing to do: %s" % plan["code"])
        return EXIT_OK

    # ★ UNE CARTE À LA FOIS, DÉSIGNÉE PAR SON IDENTIFIANT — jamais `--all`. Une machine peut
    #   porter une carte À JOUR ET EN SERVICE à côté de celle qu'on met à jour ; `--all` ne lui
    #   ferait rien, mais on ne lance pas une écriture de micrologiciel à portée d'une carte qui
    #   diffuse pour économiser une option.
    echecs = []
    for c in plan["cards"]:
        say("Updating firmware of %s (%s)..." % (c["model"], c["id"]))
        rc, out = _sh("%s --device %s --update 2>&1 | tr '\\r' '\\n' | tail -40" % (TOOL, c["id"]),
                      timeout=1800)
        for ligne in (out or "").splitlines():
            if ligne.strip() and not re.match(r"^Updating .*\.\.\. [0-9.]+%$", ligne.strip()):
                say("  " + ligne.strip())
        if rc != 0:
            echecs.append(c["model"])
            say("FAILED on %s (exit %d)" % (c["model"], rc))
    if echecs:
        say("FAILED: %s" % ", ".join(echecs))
        return 1

    _marqueur_pose(f["boot_id"])
    say("")
    say("DONE - a full POWER-OFF is required, not a reboot: the card only loads its new firmware "
         "after losing power. Until then the tool keeps reporting the firmware that is RUNNING, "
         "so it will still read 'out of date' - that is expected.")
    return EXIT_SHUTDOWN


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "state"
    if cmd == "state":
        print(json.dumps(state(), sort_keys=True))
        return 0
    if cmd == "run":
        return run()
    print("usage: blackmagic.py state|run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
