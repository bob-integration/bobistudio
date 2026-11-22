#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The keyboard installer's screens, as a tester saw them on a Linux text console (2026-11-22).

Held here, offline — the menus are drawn into a string, the keys are doubled:
  1. no superscript letter and no ❯ on the text console: its font has neither (« 1ʳᵉ » showed as
     « 1◊◊ », the cursor as a diamond);
  2. a long question WRAPS inside the frame instead of pushing its right border out;
  3. a description wraps instead of being cut with « … » into a useless half;
  4. the services screen accepts Enter with nothing ticked (it did nothing: Ctrl-C, which cancels
     everything, was the only way out); a screen that needs a box says so instead of staying mute;
  5. each install path ends on an explicit « Installation terminée. »;
  6. a refused enrolment token asks for the TOKEN again, with the reason (it went back to the
     capabilities, the first question), and the token is explained;
  7. after an uninstall inventory, the real removal is offered instead of quitting;
  8. the clock setup prints no systemctl/chronyc noise (« Failed to disable unit » in red);
  9. the web interface starts in the installer's language; the setup page shows no « <br> »;
 10. the removal scripts speak the installer's language (they were French only), and their
     banner fits its frame;
 11. the Proxmox path is gone (removed 2026-11-22: the product no longer uses LXC).

    ./venv/bin/python tests/verif_installeur_ecran.py
"""
import contextlib
import io
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "install"))
os.environ["TERM"] = "linux"
import install as I                                                          # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


I.LANG = "fr"
I._term_size = lambda: (80, 50)
W = I._panel_width()

print("\n1. glyphs the text console has")
sup = re.compile("[ʰ-˿ᴬ-ᶿ⁰-₟]")
bad = [k for lang in I.STR for k, v in I.STR[lang].items() if isinstance(v, str) and sup.search(v)]
verifie(not bad, "no superscript letter in the installer strings", bad)
corps = "\n".join(I._menu_lines("Q", [("a", "A", None)], 0, width=W))
verifie("❯" not in corps and ">" in corps, "TERM=linux: the cursor is a plain >", corps[:80])

print("\n2-3. everything fits the frame")
longue = I.t("ctrl_exists", d=I.APP_DIR)
items = [("interne", I.t("net_interne_l"), I.t("net_interne_d")), ("site", I.t("net_site_l"), I.t("net_site_d"))]
for titre, its, quoi in ((longue, [(True, "Oui", None), (False, "Non", None)], "long question"),
                         (I.t("net_title"), items, "long descriptions"),
                         (I.t("del_images"), [(True, "Oui", None), (False, "Non", None)], "uninstall images")):
    lignes = I._menu_lines(titre, its, 0, width=W)
    trop = [ln for ln in lignes if I._vlen(ln) > W - 2]
    verifie(not trop, "%s: every line fits the frame" % quoi, trop[:1])
    verifie(not any("…" in I._ANSI.sub("", ln) for ln in lignes), "%s: nothing cut with …" % quoi)
lignes = I._ANSI.sub("", "\n".join(I._menu_lines(I.t("net_title"), items, 0, width=W)))
verifie(all(w in lignes for w in I.t("net_site_d").split()[-3:]), "a description is shown to its last word")
I._term_size = lambda: (80, 14)
lignes = I._menu_lines(I.t("net_title"), items, 0, width=W)
verifie(len(lignes) <= 10, "a terminal too short: descriptions fall back to one line each", len(lignes))
I._term_size = lambda: (80, 50)

print("\n4. Enter with nothing ticked")
I._interactive = lambda: True


class _Rien:
    def __enter__(self): return self
    def __exit__(self, *a): return False


I._AltScreen = I._Raw = _Rien
ecrans = []
I._frame = lambda body, color=None: ecrans.append(I._ANSI.sub("", "\n".join(body)))


def touches(*ks):
    it = iter(ks)
    I._read_key = lambda: next(it)


def lance(fn, *a, **k):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            return fn(*a, **k), None
    except SystemExit as e:
        return None, e.code


svc = [("nmos", "NMOS", "IS-04/05")]
touches("enter")
r, code = lance(I.menu_multi, I.t("services_title"), svc, allow_back=False, allow_empty=True)
verifie(r == [] and code is None, "services: Enter with nothing ticked = none, the install goes on", (r, code))
verifie("aucun" in ecrans[-1], "services: the hint says so", ecrans[-1][-120:])
ecrans.clear(); touches("enter", "quit")
r, code = lance(I.menu_multi, I.t("caps_title"), I._cap_items(), preselected=[])
verifie(any(I.t("multi_need_one") in e for e in ecrans[1:]), "capabilities: an empty Enter is explained")
src = open(os.path.join(RACINE, "install", "install.py"), encoding="utf-8").read()
appel = src[src.find('choix = menu_multi(t("services_title")'):]
verifie("allow_empty=True" in appel[:300], "the services screen is the one that allows none")

print("\n5. an explicit end")
for chemin in ("path_node", "path_controller", "path_all_in_one"):
    corps_f = src[src.find("def %s(" % chemin):]
    corps_f = corps_f[:corps_f.find("\ndef ", 10)]
    verifie("install_done" in corps_f, "%s ends on « %s »" % (chemin, I.t("install_done")))

print("\n6. a refused token asks for the token again")
demandes = []
reponses = iter(["https://ctrl:5000", "faux", "bon"])
I.menu_multi = lambda title, items, **k: demandes.append("caps") or ["compute"]


def _ask(prompt, **k):
    demandes.append(("ask", prompt, k.get("help") or ""))
    return next(reponses)


I.ask = _ask
I._enroll = lambda url, tok, caps: None if tok == "faux" else ["--x"]
I.run_install_node = lambda *a: None
I.UNATTENDED = False
lance(I.path_node, "z")
toks = [d for d in demandes if d != "caps" and d[1] == I.t("enroll_tok")]
verifie(demandes.count("caps") == 1, "the capabilities are asked once, not again after a refusal", demandes)
verifie(len(toks) == 2 and I.t("enroll_tok_refused", url="https://ctrl:5000")[:30] in toks[1][2],
        "the token is asked again, with the reason", toks[-1:])
verifie(toks and "usage unique" in toks[0][2], "the token is explained on its first ask", toks[:1])

print("\n7. inventory, then the real removal")
import importlib                                                              # noqa: E402
I = importlib.reload(I)
I.LANG = "fr"
lances, questions = [], []
I._extract = lambda *a, **k: None
I.run_tty = lambda cmd: lances.append(cmd) or 0
rep = {"del_title": "node", "del_dryrun": True, "del_now": True, "del_images": False, "del_media": False}


def _yn(prompt, **k):
    for cle, v in rep.items():
        if prompt == I.t(cle):
            questions.append(cle)
            return v
    raise AssertionError(prompt)


I.ask_yn_menu = _yn
I.menu_select = lambda title, items, **k: rep["del_title"]
lance(I.path_uninstall, RACINE)
verifie(len(lances) == 2 and "--dry-run" in lances[0] and "--dry-run" not in lances[1],
        "inventory, then « remove now? » yes: the real removal runs", lances)
verifie(questions.count("del_dryrun") == 1, "the inventory question is not asked twice", questions)
lances.clear(); questions.clear(); rep["del_now"] = False
lance(I.path_uninstall, RACINE)
verifie(len(lances) == 1 and "--dry-run" in lances[0], "« remove now? » no: only the inventory", lances)

print("\n8-9. quiet clock, web language, setup page")
bruit = re.findall(r'run\(\[(?:"systemctl", "(?:disable|restart)", "(?:--now", "systemd-timesyncd|chrony)"'
                   r'|"chronyc", "waitsync")[^\n]*', src)
verifie(bruit and all("capture=True" in ln for ln in bruit), "systemctl/chronyc output is captured", bruit)
verifie("db_get_setting('ui_lang_default') is None and d.db_set_setting('ui_lang_default'" in src,
        "the install stores its language as the web default, only when none is stored")
setup = open(os.path.join(RACINE, "templates", "setup.html"), encoding="utf-8").read()
verifie("{{ _('setup.intro') }}" not in setup and ".split('<br>')" in setup,
        "setup page: the intro's <br> is a line break, not text")

print("\n10. removal scripts in the installer's language")
import subprocess                                                             # noqa: E402
for nom in ("uninstall-node.sh", "uninstall-controller.sh"):
    chemin = os.path.join(RACINE, "node_agent", nom)
    texte = open(chemin, encoding="utf-8").read()
    debut = texte.find('case "${BOBI_LANG:-')
    bloc = texte[debut:texte.find("\n", texte.find("T(){", debut)) + 1]
    langues = {}
    for lg in ("en", "fr", ""):
        env = {k: v for k, v in os.environ.items() if k not in ("LANG", "LC_ALL", "LC_MESSAGES")}
        env["BOBI_LANG"] = lg
        r = subprocess.run(["bash", "-c", bloc + 'T "oui" "yes"'], capture_output=True, text=True, env=env)
        langues[lg] = r.stdout
    verifie(langues == {"en": "yes", "fr": "oui", "": "oui"},
            "%s: BOBI_LANG=en → English, fr or nothing → French" % nom, langues)
    sorties = [ln for ln in texte.splitlines()
               if re.match(r'\s*[^#]*\b(echo|log|ok|warn|die|item) "[^$/]*[a-zA-Zéè]{4}', ln)
               and "$(T " not in ln and "║" not in ln and "(){" not in ln]
    verifie(not sorties, "%s: every message goes through T" % nom, sorties[:2])
    cadre = re.findall(r'echo "  ([╔║╚][^"]*[╗║╝])"', texte)
    verifie(cadre and len({len(c) for c in cadre}) == 1, "%s: the banner lines are one width" % nom,
            sorted({len(c) for c in cadre}))
    verifie(subprocess.run(["bash", "-n", chemin]).returncode == 0, "%s: parses" % nom)
verifie('"BOBI_LANG": LANG' in src[src.find("def run_tty"):src.find("def path_uninstall")],
        "install.py hands its language to the removal scripts")

print("\n11. no Proxmox path")
verifie(not os.path.exists(os.path.join(RACINE, "install", "install_proxmox.py")), "install_proxmox.py is gone")
verifie("import install_proxmox" not in src and '"proxmox"' not in src, "install.py no longer offers or imports it")
servi = open(os.path.join(RACINE, "app", "routes", "updates.py"), encoding="utf-8").read()
verifie("install_proxmox" not in servi, "the orchestrator no longer serves or downloads it")

print("\n✓ all green" if not ko else "\n✗ %d failure(s)" % ko)
sys.exit(1 if ko else 0)
