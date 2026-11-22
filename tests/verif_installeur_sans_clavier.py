#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""install.py runs without a keyboard, and never guesses (2026-11-21).

Nothing let `install.py` run unattended (CI, scripted acceptance, series deployment): without a
terminal its numbered fallback read stdin, and an empty stdin cancelled the install. Held here,
offline — the install paths themselves are doubled, nothing is installed:
  1. options and answers file give the same names; an option wins over the file; the token comes
     from a file or BOBI_ENROLL_TOKEN, never from a plain option;
  2. a missing answer stops (exit 2) naming the option; a wrong one names what is expected;
  3. every question of the installer carries a name — a new one without it would break the
     unattended mode, so it is caught here;
  4. the role dispatches to the same paths as the keyboard menu; Proxmox is refused;
  5. uninstalling never removes by default: inventory or `--confirm RETIRER` (→ `--yes`);
  6. a refused enrolment stops instead of looping; an incomplete all-in-one exits 3;
  7. get.sh forwards what follows `--`.

    ./venv/bin/python tests/verif_installeur_sans_clavier.py
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "install"))
import install as I                                                          # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


def sortie(fn, *a, **k):
    """(exit code or None, printed text, return value)."""
    buf = io.StringIO()
    code, ret = None, None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            ret = fn(*a, **k)
        except SystemExit as e:
            code = e.code
    return code, re.sub(r"\x1b\[[0-9;]*m", "", buf.getvalue()), ret


def mode(**answers):
    I.ANSWERS.clear(); I.ANSWERS.update(answers)
    I.UNATTENDED = bool(answers)
    I._PARTIAL[0] = False


TMP = tempfile.mkdtemp(prefix="verif_install_ua_")

print("\n1. options and answers file")
f = os.path.join(TMP, "rep.json")
json.dump({"role": "node", "caps": ["compute", "media"], "controller": "https://a:5000",
           "token": "from-file"}, open(f, "w"))
a = I.parse_args(["--answers", f, "--controller", "https://b:5000"])
verifie(a["role"] == "node" and a["caps"] == ["compute", "media"], "the file gives the answers", a)
verifie(a["controller"] == "https://b:5000", "an option wins over the file")
tf = os.path.join(TMP, "jeton"); open(tf, "w").write("s3cret\n")
verifie(I.parse_args(["--role", "node", "--token-file", tf])["token"] == "s3cret",
        "token read from --token-file")
os.environ["BOBI_ENROLL_TOKEN"] = "from-env"
verifie(I.parse_args(["--role", "node"])["token"] == "from-env", "… or from BOBI_ENROLL_TOKEN")
del os.environ["BOBI_ENROLL_TOKEN"]
code, out, _ = sortie(I.parse_args, ["--token", "s3cret"])
verifie(code == 2, "no plain --token option (it would show in ps and the history)", code)
verifie(I.parse_args([]) == {}, "no option: keyboard install, unchanged")

print("\n2. missing and wrong answers")
mode(role="all")
code, out, _ = sortie(I.menu_multi, "Capacités", I._cap_items(), name="caps")
verifie(code == 2 and "--caps" in out and ("manque" in out or "missing" in out),
        "missing answer: exit 2, said MISSING, the option is named", (code, out))
mode(caps="compute,gpu")
code, out, _ = sortie(I.menu_multi, "Capacités", I._cap_items(), name="caps")
verifie(code == 2 and "gpu" in out and "io2110" in out, "wrong answer: what is expected is listed", out)
mode(caps="media, compute")
verifie(sortie(I.menu_multi, "c", I._cap_items(), name="caps")[2] == ["compute", "media"],
        "a valid list is taken (in the menu's order)")
mode(services="none")
verifie(sortie(I.menu_multi, "s", [("a", "", ""), ("b", "", "")], name="services")[2] == [],
        "services: none")
mode(update="no")
verifie(sortie(I.ask_yn_menu, "maj ?", name="update")[2] is False, "yes/no answers")
mode(role="all")
verifie(sortie(I.ask_yn_menu, "q", name="purge_images", ua_default=False)[2] is False,
        "a safe default applies when the answer is absent")

print("\n3. every question carries a name")
src = open(os.path.join(RACINE, "install", "install.py"), encoding="utf-8").read()
corps = src.split("\ndef _main_unattended", 1)[0]
appels = []
for m in re.finditer(r"(?<![\w.])(menu_select|menu_multi|ask_yn_menu|ask)\(", corps):
    debut = corps.rfind("\n", 0, m.start()) + 1
    ligne = corps[debut:m.start()]
    if ligne.lstrip().startswith("def "):
        continue
    j, prof = m.end(), 1
    while prof:
        prof += {"(": 1, ")": -1}.get(corps[j], 0); j += 1
    appels.append((corps.count("\n", 0, m.start()) + 1, m.group(1), corps[m.start():j]))
helpers = ("def _select_fallback", "def _multi_fallback", "def ask_yn_menu")
sans = []
for ligne, fn, texte in appels:
    avant = corps[:corps.find(texte)]
    dans = avant.rfind("\ndef ")
    if any(avant[dans:].startswith("\n" + h) for h in helpers):
        continue                                # internal to the prompt helpers
    if "name=" not in texte and "UNATTENDED" not in corps[max(0, corps.find(texte) - 160):corps.find(texte)]:
        sans.append((ligne, fn))
verifie(len(appels) >= 12 and not sans, "%d questions, all named" % len(appels), sans)

print("\n4. role dispatch")
vus = []
for r in ("node", "controller", "all", "uninstall"):
    setattr(I, {"node": "path_node", "controller": "path_controller", "all": "path_all_in_one",
                "uninstall": "path_uninstall"}[r], lambda z, r=r: vus.append(r))
I._find_source = lambda: ("tree", RACINE)
I.APP_DIR = TMP                                 # no main.py there: --update is not needed
COMPLET = {"node": dict(caps="compute", controller="https://x:5000", token="t"),
           "controller": dict(services="none"),
           "all": dict(caps="compute", net="interne", services="none"),
           "uninstall": dict(target="node", inventory=True)}
for r in ("node", "controller", "all", "uninstall"):
    mode(role=r, **COMPLET[r]); sortie(I.main)
verifie(vus == ["node", "controller", "all", "uninstall"], "each role takes its keyboard path", vus)
mode(role="proxmox")
code, out, _ = sortie(I.main)
verifie(code == 2 and "proxmox" in out, "the removed Proxmox path is refused", (code, out[-120:]))
mode(caps="compute")
code, out, _ = sortie(I.main)
verifie(code == 2 and "--role" in out, "no role: --role is asked for", out[-120:])
mode(role="all", lang="en")
sortie(I.main)
verifie(I.LANG == "en", "--lang applies")
mode(role="all")
os.environ.pop("BOBI_LANG", None)
sortie(I.main)
verifie(I.LANG == "fr", "no --lang: French, the one harmless default")

print("\n4b. every answer is checked BEFORE anything is installed")
# Bench VMs, 2026-11-21: `--role all` without --net installed the orchestrator, Docker and the
# agent, then stopped with "answer missing". The role's answers are now all checked up front.
for r, manque in (("all", "--net"), ("controller", "--services"), ("node", "--token-file")):
    vus.clear()
    partiel = {k: v for k, v in COMPLET[r].items() if k != {"--net": "net", "--services": "services",
                                                             "--token-file": "token"}[manque]}
    mode(role=r, **partiel)
    code, out, _ = sortie(I.main)
    verifie(code == 2 and manque in out and not vus, "%s without %s: stop, nothing run" % (r, manque),
            (code, vus, out[-160:]))
vus.clear(); mode(role="all")
code, out, _ = sortie(I.main)
verifie(code == 2 and all(o in out for o in ("--caps", "--net", "--services")) and not vus,
        "all the missing answers are named at once", out[-200:])
vus.clear(); mode(role="all", caps="compute,bidon", net="interne", services="none")
code, out, _ = sortie(I.main)
verifie(code == 2 and "bidon" in out and not vus, "an unknown capability is refused up front", out[-160:])
open(os.path.join(TMP, "main.py"), "w").close()
vus.clear(); mode(role="all", **COMPLET["all"])
code, out, _ = sortie(I.main)
verifie(code == 2 and "--update" in out and not vus, "existing code: --update asked up front", out[-160:])
vus.clear(); mode(role="all", update="yes", **COMPLET["all"])
sortie(I.main)
verifie(vus == ["all"], "existing code with --update: the path runs", vus)
os.remove(os.path.join(TMP, "main.py"))
vus.clear(); mode(role="uninstall", target="node", confirm="oui")
code, out, _ = sortie(I.main)
verifie(code == 2 and "RETIRER" in out and not vus, "uninstall: a wrong --confirm is refused up front")
code, out, _ = sortie(I.parse_args, ["--token-file", os.path.join(TMP, "absent")])
verifie(code == 2 and ("jeton" in out.lower() or "token" in out.lower()),
        "an unreadable token file is named as such", out[-160:])
verifie(code == 2 and "réponses" not in out and "Answers" not in out,
        "... not as the answers file", out[-160:])
# With argparse abbreviations, `--token <path>` silently passed for `--token-file`: a readable
# file makes the difference visible (refused, not read).
open(os.path.join(TMP, "tok"), "w").write("t")
code, out, _ = sortie(I.parse_args, ["--token", os.path.join(TMP, "tok")])
verifie(code == 2, "--token is not an abbreviation of --token-file", (code, out[-160:]))

print("\n5. uninstall never removes by default")
import importlib                                                              # noqa: E402
I = importlib.reload(I)
lances = []
I._extract = lambda *a, **k: None
I.run_tty = lambda cmd: lances.append(cmd) or 0
mode(role="uninstall", target="both")
code, out, _ = sortie(I.path_uninstall, RACINE)
verifie(code == 2 and "--confirm RETIRER" in out and not lances,
        "neither --inventory nor --confirm: stop, nothing run", (code, lances))
mode(role="uninstall", target="node", confirm="oui")
code, out, _ = sortie(I.path_uninstall, RACINE)
verifie(code == 2 and not lances, "--confirm other than RETIRER: refused")
mode(role="uninstall", target="node", inventory=True)
sortie(I.path_uninstall, RACINE)
verifie(lances and "--dry-run" in lances[-1] and "--yes" not in lances[-1], "--inventory: dry run",
        lances[-1:])
code, out, _ = sortie(I.path_uninstall, RACINE)
verifie("rien n'a été modifié" in out and "Retrait terminé" not in out,
        "--inventory ends on « nothing was changed », not « removal finished »", out[-160:])
mode(role="uninstall", target="controller", confirm="RETIRER", purge_data=True)
sortie(I.path_uninstall, RACINE)
verifie("--yes" in lances[-1] and "--purge-data" in lances[-1] and "--purge-images" not in lances[-1],
        "--confirm RETIRER: real removal, --yes passed, only the asked purges", lances[-1])

print("\n6. failure outcomes")
I._enroll = lambda *a: None
mode(role="node", caps="compute", controller="https://x:5000", token="t")
import signal                                                                 # noqa: E402
def _boucle(*a):
    raise RuntimeError("path_node is looping")
signal.signal(signal.SIGALRM, _boucle); signal.alarm(5)
try:
    code, out, _ = sortie(I.path_node, RACINE)
except RuntimeError as e:
    code = str(e)
signal.alarm(0)
verifie(code == 1, "a refused enrolment stops (no endless retry)", code)
I.install_controller = lambda z: None
I.run_install_node = lambda *a: None
I.register_local_agent = lambda tok: False
I.proposer_services = I.dire_gestes_restants = lambda: None
I._find_source = lambda: ("tree", RACINE)
I.APP_DIR = TMP
mode(role="all", caps="compute", net="interne", services="none")
code, out, _ = sortie(I.main)
verifie(code == 3, "an incomplete all-in-one exits 3", code)

print("\n7. get.sh forwards what follows --")
g = open(os.path.join(RACINE, "get.sh"), encoding="utf-8").read()
verifie('--) shift; INSTALL_ARGS=("$@"); break;;' in g
        and 'exec python3 install/install.py "${INSTALL_ARGS[@]}"' in g, "get.sh passes them on")
verifie(subprocess.run(["bash", "-n", os.path.join(RACINE, "get.sh")]).returncode == 0, "get.sh parses")
r = subprocess.run([sys.executable, os.path.join(RACINE, "install", "install.py"), "--help"],
                   capture_output=True, text=True)
verifie(r.returncode == 0 and "--role" in r.stdout, "install.py --help works without root")

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
