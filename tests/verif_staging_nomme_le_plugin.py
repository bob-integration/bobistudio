#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""A refused build context NAMES its cause: a missing plugin is not a stale instance.

★ WHY (2026-11-01). `_stage_tar` refused any missing path with a SINGLE message, the one meant
for a deployed instance whose image contexts were never embedded. But the most common cause for a
newcomer is a different one entirely: the image is the runtime of a PLUGIN that is not installed.
A plugin's sources are not in the main repository (they are submodules, and a GitHub source
archive carries none of their content — see INSTALL.md, "Ce qui est récupéré"), and the intended
route is the Catalogue page.

Nothing checks that a plugin is installed, and the gate for an image is the NODE CAPABILITY, which
the operator declares: this refusal is therefore reached having done nothing wrong. An outside
user lost six weeks to it (issue #1, 2026-09-23) — he concluded the product could not be built,
where he was in fact missing a plugin. The cost was in the message, not in the defect.

What this guard requires: the refusal names the plugin and points to the Catalogue when the plugin
is ABSENT, and does NOT do so in the other cases (plugin installed but context incomplete; missing
path outside any plugin). A message pointing at the wrong cause is exactly the defect being fixed,
and pointing at the Catalogue wrongly would recreate it mirrored.

Offline: temporary trees, throwaway database, no network, no docker, no node.

    ./venv/bin/python tests/verif_staging_nomme_le_plugin.py
"""
import os
import sys
import tempfile

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)
_TMP = tempfile.mkdtemp(prefix="verif_staging_plugin_")
from app import config, database                                          # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(_TMP, "db.sqlite")  # BEFORE any access

from app.routes import images as IM                                       # noqa: E402

ko = []


def ok(cond, title, detail=""):
    print("%s %s" % ("  ok  " if cond else " FAILED", title))
    if not cond:
        if detail:
            print("         %s" % (detail,))
        ko.append(title)


def refusal(which, root):
    """Return the message of `_stage_tar`'s refusal, or None if it did not refuse."""
    real = IM._repo_root
    IM._repo_root = lambda: root
    try:
        IM._stage_tar(which)
        return None
    except FileNotFoundError as e:
        return str(e)
    finally:
        IM._repo_root = real


def tree(without=(), empty_dirs=()):
    """A tree carrying EVERY source of every image, minus `without`.

    No fake content is produced: empty files are enough, since `_stage_tar` only refuses on
    EXISTENCE at this stage. `empty_dirs` creates a plugin's directory without its content — the
    "installed but incomplete" case, which must NOT point at the Catalogue.
    """
    d = tempfile.mkdtemp(dir=_TMP)
    for which in IM._IMAGES:
        for src, _dst in IM._IMAGES[which]["stage"]:
            if any(src == s or src.startswith(s.rstrip("/") + "/") for s in without):
                continue
            p = os.path.join(d, src)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if not os.path.exists(p):
                open(p, "w").close()
    for rel in empty_dirs:
        os.makedirs(os.path.join(d, rel), exist_ok=True)
    return d


# ─── 0. The stage is the real one ────────────────────────────────────────────────────
print("\n0. The stage is the product's own")
ok("mtl" in IM._IMAGES, "the mtl image is declared", sorted(IM._IMAGES))
_mtl_srcs = [s for s, _ in IM._IMAGES["mtl"]["stage"]]
ok(any(s.startswith("plugins/2110_io/") for s in _mtl_srcs),
   "mtl's staging does read the 2110_io plugin's sources (otherwise this guard proves nothing)")
ok(refusal("mtl", tree()) is None,
   "a COMPLETE tree triggers no refusal (otherwise every case below is meaningless)")

# ─── 1. ★ Plugin ABSENT: the refusal names the plugin and the Catalogue ──────────────
print("\n1. ★ The plugin is not installed (the case of issue #1)")
msg = refusal("mtl", tree(without=["plugins/2110_io"]))
ok(msg is not None, "the context is refused", "no refusal at all!")
msg = msg or ""
# ⚠ WE MATCH THE SENTENCE, NOT THE NAME (mutation M5, 2026-11-01). Searching for "2110_io"
# anywhere in the message found it in the LIST OF MISSING FILES (plugins/2110_io/docker/...), so
# the guard stayed green while the message no longer named the plugin at all.
ok("runtime of plugin 2110_io" in msg, "the refusal NAMES the missing plugin", msg[:160])
ok("Catalogue" in msg, "it points to the Catalogue page (the intended route)", msg[:160])
# ⚠ This assertion is in FRENCH on purpose: the OTHER message is a neighbour we did not rewrite,
# and it is still French. Translating these refusals is queued in TODO.md (i18n keys).
ok("Mettre à jour l'instance" not in msg,
   "it no longer talks about updating the instance (that was the other cause)", msg[:160])

print("\n1 bis. Same for another plugin image (the rule is not wired to mtl)")
msg_dk = refusal("decklink", tree(without=["plugins/decklink_io"])) or ""
ok("runtime of plugin decklink_io" in msg_dk and "Catalogue" in msg_dk,
   "decklink absent: the plugin is named and the Catalogue pointed at", msg_dk[:160])

# ─── 2. Plugin INSTALLED but context incomplete: the other message ──────────────────
print("\n2. The plugin is there, but its context is incomplete")
msg2 = refusal("mtl", tree(without=["plugins/2110_io"], empty_dirs=["plugins/2110_io"])) or ""
ok(msg2 != "", "the context is refused too")
ok("Catalogue" not in msg2,
   "it does NOT point at the Catalogue: the plugin IS installed, installing it again would do "
   "nothing", msg2[:160])
ok("Mettre à jour l'instance" in msg2, "it points at updating the instance", msg2[:160])

# ─── 3. Missing outside any plugin, and the shared `_*` runtimes ─────────────────────
print("\n3. A missing path outside any plugin")
msg3 = refusal("compute", tree(without=["plugins/_compute_runtime/Dockerfile"])) or ""
ok(msg3 != "", "the context is refused")
ok("Catalogue" not in msg3,
   "⚠ `plugins/_compute_runtime` is NOT a plugin: its name starts with `_`, and mistaking it for "
   "one would send the operator installing a package that does not exist", msg3[:160])
ok("Mettre à jour l'instance" in msg3, "it points at updating the instance", msg3[:160])

print("\n3 bis. ★ A shared `_*` runtime whose DIRECTORY is entirely missing")
# Without this case the `_` rule is never exercised: removing ONE file from _compute_runtime
# leaves the directory in place, so the presence check alone rules out the plugin branch and the
# mutation that deletes the exclusion stays inert (mutation M2, 2026-11-01).
msg4 = refusal("compute", tree(without=["plugins/_compute_runtime"])) or ""
ok(msg4 != "", "the context is refused")
ok("Catalogue" not in msg4,
   "a missing shared runtime does NOT send anyone to the Catalogue: `_compute_runtime` is not an "
   "installable package, and offering it would be a dead end", msg4[:160])
ok("Mettre à jour l'instance" in msg4, "it points at updating the instance", msg4[:160])

print()
if ko:
    print("✗ %d failure(s): %s" % (len(ko), " · ".join(ko[:4])))
    sys.exit(1)
print("✓ all green")
sys.exit(0)
