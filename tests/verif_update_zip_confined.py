#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# An update archive is extracted over the installation (app/updater.py). Two promises:
#   1. no member is ever written outside the installation — by its name (`..`, absolute) or
#      through a link already present in the tree;
#   2. the local state of the instance (site configuration, databases, uploads, builds) is
#      never overwritten, however the member that names it is spelled.
# An archive holding one member of the first kind is refused WHOLE, before the backup and
# before anything is written. A member of the second kind is skipped, the rest is applied.
#
# Nothing here touches the real installation: ROOT and the state files are redirected to a
# throwaway tree BEFORE any call, the service restart and the backup are replaced, and the
# test stops if the redirection is not in place. Every "outside" path it probes is inside the
# throwaway tree as well.
#
#   $ ./venv/bin/python tests/verif_update_zip_confined.py
import logging
import os
import shutil
import sys
import warnings
import zipfile

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── HERMETIC: throwaway database, throwaway installation ────────────────────────────────────
import tempfile                                                      # noqa: E402
TMP = os.path.realpath(tempfile.mkdtemp(prefix="verif_update_zip_"))
from app import config, database, media_registry                     # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(TMP, "principale.db")
media_registry.MEDIA_DB_PATH = os.path.join(TMP, "db_medias.db")
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import updater                                              # noqa: E402

INSTALL = os.path.join(TMP, "depth1", "depth2", "install")           # `../..` stays in TMP
OUTSIDE = os.path.join(TMP, "outside")
updater.ROOT = INSTALL
updater.PENDING_PATH = os.path.join(INSTALL, "UPDATE_PENDING")
updater.DEPLOY_INFO_PATH = os.path.join(INSTALL, "deploy_info.json")
events = []
updater.restart_service = lambda: events.append("restart")
updater.backup_code = lambda: events.append("backup")
updater.record_deploy = lambda *a, **k: events.append("deploy recorded")
updater._ensure_requirements = lambda zpath: (True, "")


class _NoProcess:
    def __getattr__(self, name):
        raise RuntimeError("the test must never start a process from the updater")


updater.subprocess = _NoProcess()
if not os.path.realpath(updater.ROOT).startswith(TMP + os.sep):
    sys.exit("installation root not redirected: refusing to run")

failures, passes = [], []


def check(title, condition, why=""):
    (passes if condition else failures).append(title)
    print("  %-5s %s" % ("OK" if condition else "FAIL", title))
    if not condition and why:
        print("        -> %s" % why)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def tree(top):
    out = {}
    for dp, _, files in os.walk(top, followlinks=False):
        for fn in files:
            p = os.path.join(dp, fn)
            with open(p, encoding="utf-8", errors="replace") as f:
                out[os.path.relpath(p, top)] = f.read()
    return out


LOCAL = {"config_local.py": "LOCAL SECRETS\n", "db_bobistudio.db": "LOCAL DB\n",
         "static/uploads/logo.png": "LOCAL LOGO\n", "dist/build.zip": "LOCAL BUILD\n",
         "build_manifest.json": "LOCAL MANIFEST\n", "deploy_info.json": "LOCAL DEPLOY\n",
         "main.py": "OLD\n", "app/core.py": "OLD\n",
         "plugins/here/plugin.json": "OLD\n", "services/here/manifest.json": "OLD\n"}
PROTECTED = [k for k in LOCAL if LOCAL[k].startswith("LOCAL")]


def fresh():
    """A clean installation, a clean outside, and a link in the tree that points outside."""
    for d in (INSTALL, OUTSIDE):
        shutil.rmtree(d, ignore_errors=True)
    for rel, text in LOCAL.items():
        write(os.path.join(INSTALL, rel), text)
    write(os.path.join(OUTSIDE, "keep.txt"), "OUTSIDE\n")
    os.symlink(OUTSIDE, os.path.join(INSTALL, "link"))
    del events[:]


n_zip = [0]


def archive(members):
    n_zip[0] += 1
    path = os.path.join(TMP, "update-%d.zip" % n_zip[0])
    with zipfile.ZipFile(path, "w") as z:
        for name, text in members.items():
            z.writestr(zipfile.ZipInfo(name), text)      # ZipInfo keeps the name as given
    return path


def extract(members, **kw):
    """→ (refused by ValueError, installation after, outside after)."""
    fresh()
    refused = False
    try:
        updater._extract_over(archive(members), **kw)
    except ValueError:
        refused = True
    except Exception as e:                                           # noqa: BLE001
        refused = "exception %s" % type(e).__name__
    return refused, tree(INSTALL), tree(OUTSIDE)


def untouched(inst, keys=PROTECTED):
    return all(inst.get(k) == LOCAL[k] for k in keys)


BASE = {"main.py": "NEW\n", "app/core.py": "NEW\n"}
OUT_OK = {"keep.txt": "OUTSIDE\n"}

print("Update archive: extraction confined to the installation, local state preserved\n")

# ── 1. A plain archive ──────────────────────────────────────────────────────────────────────
refused, inst, out = extract(dict(BASE, **{
    "app/new/module.py": "NEW\n", "plugins/here/plugin.json": "NEW\n",
    "plugins/absent/plugin.json": "NEW\n", "services/here/manifest.json": "NEW\n",
    "services/absent/manifest.json": "NEW\n", "plugins/_runtime/Dockerfile": "NEW\n",
    "config_local.py": "FROM ARCHIVE\n", "other.db": "FROM ARCHIVE\n",
    "static/uploads/logo.png": "FROM ARCHIVE\n", "dist/build.zip": "FROM ARCHIVE\n",
    "build_manifest.json": "FROM ARCHIVE\n", "deploy_info.json": "FROM ARCHIVE\n",
    "static/app.js": "NEW\n", "docs/": ""}), skip_plugins={"absent"}, skip_services={"absent"})
check("code is replaced, new files and folders are created",
      refused is False and all(inst.get(k) == "NEW\n" for k in (
          "main.py", "app/core.py", "app/new/module.py", "plugins/here/plugin.json",
          "services/here/manifest.json", "plugins/_runtime/Dockerfile", "static/app.js")),
      "refused=%r" % (refused,))
check("local state is kept, components not installed here are not added",
      untouched(inst) and "other.db" not in inst and "plugins/absent/plugin.json" not in inst
      and "services/absent/manifest.json" not in inst and out == OUT_OK)

# ── 2. Local state named another way ────────────────────────────────────────────────────────
print("\n2. protected paths, spelled differently")
SPELLINGS = ["./%s", ".//%s", "././%s", "%s", "/".join(["."] * 3) + "/%s", ".\\%s"]
for spelling in SPELLINGS:
    members = dict(BASE, **{spelling % k.replace("/", "\\" if "\\" in spelling else "/"): "FROM ARCHIVE\n"
                            for k in PROTECTED})
    members["./plugins/absent/plugin.json"] = "NEW\n"
    refused, inst, out = extract(members, skip_plugins={"absent"})
    check("written as %r: local state kept, the rest applied" % (spelling % "config_local.py"),
          refused is False and untouched(inst) and inst.get("main.py") == "NEW\n"
          and "plugins/absent/plugin.json" not in inst and out == OUT_OK,
          "refused=%r changed=%r" % (refused, [k for k in PROTECTED if inst.get(k) != LOCAL[k]]))

# ── 3. Members that leave the installation: the whole archive is refused ────────────────────
print("\n3. members outside the installation")
ESCAPES = {
    "parent":              "../escaped.txt",
    "parent, deeper":      "../../escaped.txt",
    "parent, mid-path":    "app/../../escaped.txt",
    "parent, backslashes": "..\\escaped.txt",
    "back into the tree":  "app/../config_local.py",
    "back, to a database": "x/../db_bobistudio.db",
    "absolute":            os.path.join(OUTSIDE, "escaped.txt"),
    "through a link":      "link/escaped.txt",
    "through a link, ./":  "./link/keep.txt",
    "bare parent":         "..",
}
for name, member in ESCAPES.items():
    refused, inst, out = extract(dict(BASE, **{member: "FROM ARCHIVE\n"}))
    stray = sorted(k for k in tree(TMP) if os.path.basename(k) == "escaped.txt")
    check("%s: refused whole — nothing written inside, nothing outside" % name,
          refused is True and {k: v for k, v in inst.items() if not k.startswith("link")} == LOCAL
          and out == OUT_OK and not stray,
          "refused=%r, main.py=%r, config=%r, outside=%r, stray=%r" % (
              refused, inst.get("main.py"), inst.get("config_local.py"), sorted(out), stray))
    fresh()
    msg = updater._archive_error(archive(dict(BASE, **{member: "x"}))) if hasattr(updater, "_archive_error") else None
    check("... and the reason is given", bool(msg), "no reason returned")

# ── 4. The update itself refuses before the backup ──────────────────────────────────────────
print("\n4. apply_update")


def apply(members):
    fresh()
    z = archive(members)
    updater.fetch_manifest = lambda base, token: {"sha256": updater.sha256_file(z), "build_id": "b1"}
    updater._download = lambda url, token, dest, timeout=120: shutil.copy(z, dest)
    try:
        ok, msg = updater.apply_update("http://peer.invalid:5000", "t")
    except Exception as e:                                           # noqa: BLE001
        ok, msg = None, "exception %s: %s" % (type(e).__name__, e)
    return ok, msg, tree(INSTALL), tree(OUTSIDE), list(events)


ok, msg, inst, out, ev = apply(BASE)
check("a plain archive is applied: backup, extraction, restart",
      ok is True and inst.get("main.py") == "NEW\n" and ev == ["backup", "deploy recorded", "restart"],
      "ok=%r msg=%r events=%r" % (ok, msg, ev))
for name in ("parent", "back into the tree", "absolute", "through a link"):
    ok, msg, inst, out, ev = apply(dict(BASE, **{ESCAPES[name]: "FROM ARCHIVE\n"}))
    check("%s: update refused with a reason, no backup, no restart, nothing written" % name,
          ok is False and bool(msg) and ev == [] and inst.get("main.py") == "OLD\n"
          and untouched(inst) and out == OUT_OK,
          "ok=%r msg=%r events=%r main=%r" % (ok, msg, ev, inst.get("main.py")))

print("\n%d passed, %d failed" % (len(passes), len(failures)))
if failures:
    print("FAILED:\n  " + "\n  ".join(failures))
    sys.exit(1)
print("OK")
