#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : une version reconstituée depuis git est son état FINAL (2026-11-14).

stills 0.19.10 avait été reconstituée depuis le dernier commit qui touchait `plugin.json`, et
ratait les commits suivants qui ne changeaient que le script, dont l'anonymisation : un nom de
client est revenu dans l'arbre et a bloqué la publication.

Historique simulé : V1, puis V2, puis un correctif du script seul, puis un commit de
manifeste qui garde V2, puis un autre correctif seul, puis V3.

    ./venv/bin/python tests/verif_version_reconstituee_etat_final.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


import app.database as database                                          # noqa: E402
database.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="vfinal_db_"), "principale.db")   # jamais la base de prod
from app import plugins                                                   # noqa: E402

D = tempfile.mkdtemp(prefix="vfinal_")


def git(*a):
    return subprocess.run(("git", "-C", D) + a, capture_output=True, text=True, check=True).stdout.strip()


def put(version, mark, extra=""):
    """version None: the script alone changes, the manifest is left as it is."""
    if version:
        with open(os.path.join(D, "plugin.json"), "w") as f:
            f.write('{"type": "vfinal", "version": "%s"%s}' % (version, extra))
    with open(os.path.join(D, "script.py"), "w") as f:
        f.write("MARK = %r\n" % mark)
    git("add", "-A")
    git("commit", "-q", "-m", "%s %s" % (version or "fix", mark))
    return git("rev-parse", "HEAD")


git("init", "-q", "-b", "main")
git("config", "user.email", "garde@example.invalid")
git("config", "user.name", "Garde")
put("1.0.0", "V1")
put("1.1.0", "V2")
put(None, "V2-fix")                          # script only
put("1.1.0", "V2-fix", ', "label": "x"')     # manifest touched, same version
fin = put(None, "V2-final")                  # script only
put("1.2.0", "V3")

check(plugins._final_commit_for_version(D, "1.1.0") == fin,
      "état final de V2 : le dernier correctif, pas le dernier commit du manifeste")
check(git("show", "%s:script.py" % plugins._final_commit_for_version(D, "1.0.0")) == "MARK = 'V1'",
      "V1 : son seul commit")
check(plugins._final_commit_for_version(D, "1.2.0") == git("rev-parse", "HEAD"), "version courante : HEAD")
check(plugins._final_commit_for_version(D, "9.9.9") is None, "version inconnue : rien")

plugins.REGISTRY["vfinal"] = {"_dir": D, "version": "1.2.0", "script_template": "script.py"}
check(plugins.ensure_version("vfinal", "1.1.0"), "V2 reconstituée")
with open(os.path.join(D, "versions", "1.1.0", "script.py")) as f:
    check(f.read().strip() == "MARK = 'V2-final'", "et le script du cache est l'état final")

print()
if fails:
    print("%d échec(s)" % len(fails))
    sys.exit(1)
print("Reconstitution : l'état final de chaque version.")
