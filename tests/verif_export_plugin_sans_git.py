# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Plugin export leaves out what is not the plugin (2026-11-25).

The export zipped the whole plugin folder, git repository included: audio_console came out at
71 MB for a 1 MB plugin, over the 20 MB import cap, and could not be imported on another instance.
  1. full export (.mxlplugin): plugin files and versions/, no .git (folder or submodule file),
     no __pycache__ / .pyc;
  2. export of the current version (.mxlversion): same, and no versions/ either;
  3. if the audio_console submodule is checked out: its export fits under the import cap.
    ./venv/bin/python tests/verif_export_plugin_sans_git.py
"""
import io
import os
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import config                                # noqa: E402
config.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="export_plugin_db_"), "t.db")
from app.routes.plugin_registry import _zip_plugin_dir  # noqa: E402

ko = 0


def ok(cond, label, detail=""):
    global ko
    print(("  ok  " if cond else " ÉCHEC ") + label)
    if not cond:
        print("         " + str(detail))
        ko += 1


def names(buf):
    return sorted(zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist())


def write(path, data="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(data)


d = tempfile.mkdtemp(prefix="export_plugin_")
for rel in ("plugin.json", "script.py", "ui/panel.js", "versions/0.1.0/script.py",
            ".git/objects/pack/big.pack", "__pycache__/hooks.cpython-313.pyc",
            "tools/__pycache__/x.pyc", "tools/helper.py"):
    write(os.path.join(d, rel))

full = names(_zip_plugin_dir(d, keep_versions=True))
ok(full == ["plugin.json", "script.py", "tools/helper.py", "ui/panel.js", "versions/0.1.0/script.py"],
   "1. full export: plugin files and versions/, nothing from git or Python", full)
flat = names(_zip_plugin_dir(d, keep_versions=False))
ok(flat == ["plugin.json", "script.py", "tools/helper.py", "ui/panel.js"],
   "2. current version export: no versions/ either", flat)

# A submodule checkout carries .git as a FILE, not a folder.
sub = tempfile.mkdtemp(prefix="export_plugin_sub_")
write(os.path.join(sub, "plugin.json"))
write(os.path.join(sub, ".git"), "gitdir: ../../.git/modules/plugins/x")
ok(names(_zip_plugin_dir(sub, keep_versions=True)) == ["plugin.json"],
   "1b. submodule checkout: the .git file is left out too")

import inspect                                         # noqa: E402
from app.routes import plugin_registry as reg           # noqa: E402
src_full, src_ver = inspect.getsource(reg.plugins_export), inspect.getsource(reg.plugins_export_version)
ok("_zip_plugin_dir(d, keep_versions=True)" in src_full and "os.walk" not in src_full,
   "1c. the .mxlplugin route goes through the filtered zip")
ok("_zip_plugin_dir(d, keep_versions=not flat)" in src_ver and "os.walk" not in src_ver,
   "2b. the .mxlversion route drops versions/ for the current version")

ac = os.path.join(ROOT, "plugins", "audio_console")
if os.path.isfile(os.path.join(ac, "plugin.json")):
    size = len(_zip_plugin_dir(ac, keep_versions=True).getvalue())
    ok(size < 20 * 1024 * 1024, "3. audio_console export under the 20 MB import cap (%.1f MB)"
       % (size / 1048576), size)
else:
    print("  --  3. audio_console not checked out: skipped")

sys.exit(1 if ko else 0)
