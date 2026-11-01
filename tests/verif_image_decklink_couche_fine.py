#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The shared `bobi-compute` base image no longer depends on an OPTIONAL plugin (2026-11-01).

★ WHAT THIS GUARD PROVES, AND WHY IT EXISTS.
The four Blackmagic binaries (decklink_mxl, decklink_tx, decklink_probe, decklink_profile) were
compiled INSIDE `bobi-compute`, the base image of EVERY non-2110 plugin. Two effects, both at odds
with what the product claims — you install what you need:

  · a node with no DeckLink card carried the binaries anyway;
  · above all, the base image could not even be STAGED without that plugin's sources:
    `_stage_tar` raises as soon as one path of the list is missing. The public repository ships the
    recipe but not the plugin (private submodule) → NO image could be built, and the product did
    nothing. Reported from the OUTSIDE on 2026-09-23 and left six weeks without an answer: nothing
    here could see it, since our working tree always has every submodule.

The central check (section 2) therefore replays that situation instead of describing it: a
temporary tree carrying everything the compute image requires EXCEPT `plugins/decklink_io/`, and
the staging must succeed. Putting `_DECKLINK` back into the compute image's list makes it fail.

Offline: throwaway database, no network, no node, no docker. Sections:
  0. the stage is the real one (the lists read are the product's own);
  1. the `decklink` image exists, as a node_only thin layer gated on the capability;
  2. ★ the base image stages WITHOUT the plugin — the reporter's situation;
  3. the thin layer's recipe is complete (every COPY has its staging entry);
  4. the base image carries neither the stage nor the binaries any more;
  5. the plugin targets this image, and the capability requires "compute";
  6. the two lists that FILTER name the new image (Nodes page, variants);
  7. the deployment refusal without the capability is translated, FR and EN.

    ./venv/bin/python tests/verif_image_decklink_couche_fine.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)
_TMP = tempfile.mkdtemp(prefix="verif_decklink_couche_")
from app import config, database                                          # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(_TMP, "db.sqlite")  # BEFORE any access (get_db pins)

from app.routes import images as IM                                       # noqa: E402
from app.routes import nodes as ND                                        # noqa: E402

ko = []
not_checked = []

# ★ WHAT THIS GUARD MUST DO IN THE PUBLIC REPOSITORY (2026-11-01). `plugins/decklink_io` is a
# PRIVATE submodule: the public tree does not carry it, and that is the NORMAL situation there, not
# an anomaly. Three sections read its files — they abstain explicitly rather than turn a CI red for
# something it can do nothing about. The CENTRAL check (section 2) runs in both trees, and it is
# precisely the one the public repository needs: it proves the shared base image stages without
# this plugin — the defect reported from the outside on 2026-09-23.
#
# ⚠ A SILENT abstention would be worse than a failure: it would suggest everything was measured.
# "NOT CHECKED" is matched by the CI recap, which greps case-insensitively for
# "NON MESURÉ|SAUTÉ|skipped|not checked" (see .github/workflows/ci.yml, "PLUSIEURS VOCABULAIRES,
# PAS UN SEUL") — do not reword it into something that list does not catch.
_PLUGIN = os.path.isdir(os.path.join(_R, "plugins/decklink_io"))


def abstain(title, why):
    print("  NOT CHECKED  %s" % title)
    print("               → %s" % why)
    not_checked.append(title)


def ok(cond, title, detail=""):
    print("%s %s" % ("  ok  " if cond else " FAILED", title))
    if not cond:
        if detail:
            print("         %s" % (detail,))
        ko.append(title)


def read(rel):
    with open(os.path.join(_R, rel), encoding="utf-8", errors="replace") as f:
        return f.read()


# ─── 0. The stage is the real one ────────────────────────────────────────────────────
print("\n0. The stage is the product's own")
ok("compute" in IM._IMAGES and "gui" in IM._IMAGES,
   "the product's images are read from _IMAGES", sorted(IM._IMAGES))
ok(isinstance(IM._DECKLINK, list) and len(IM._DECKLINK) >= 6,
   "_DECKLINK still enumerates the plugin's sources", repr(IM._DECKLINK)[:120])
if _PLUGIN:
    print("  ok   the decklink_io submodule is present: the 3 sections reading it will run")
else:
    abstain("sections 3 and 5 read the decklink_io submodule",
            "tree WITHOUT that submodule (public repository) — section 2 is still measured, "
            "and it carries the verdict that matters here")

# ─── 1. The image exists, as a thin layer ───────────────────────────────────────────
print("\n1. The “decklink” image is a node_only thin layer")
spec = IM._IMAGES.get("decklink") or {}
ok(bool(spec), "the “decklink” image is declared in _IMAGES")
ok(spec.get("node_only") is True, "node_only: built on the node, never pushed to the fleet")
ok(spec.get("base_from") == "compute", "base_from = compute (the FROM is rewritten at staging)")
ok(spec.get("cap") == "decklink", "gated on the “decklink” capability")
ok(spec.get("setting") == "decklink_image" and spec.get("field") == "decklink_image",
   "shared tag in the decklink_image setting (no per-node column)")
ok((spec.get("prefix") or "") == "bobi-decklink", "tag prefix bobi-decklink")

# ─── 2. ★ THE CENTRAL CHECK: the base image stages WITHOUT the plugin ────────────────
print("\n2. ★ The shared base image builds WITHOUT the plugin's sources (the reporter's case)")
fake_tree = os.path.join(_TMP, "tree-without-decklink")
missing = []
for src, _dst in IM._IMAGES["compute"]["stage"]:
    s = os.path.join(_R, src)
    d = os.path.join(fake_tree, src)
    if not os.path.exists(s):
        missing.append(src)
        continue
    os.makedirs(os.path.dirname(d), exist_ok=True)
    if os.path.isdir(s):
        shutil.copytree(s, d, dirs_exist_ok=True)
    else:
        shutil.copy2(s, d)
ok(not missing, "every source of the compute image exists in the repository", missing)
ok(not os.path.exists(os.path.join(fake_tree, "plugins/decklink_io")),
   "the test tree carries NO file of plugins/decklink_io")

_real_root = IM._repo_root
IM._repo_root = lambda: fake_tree
try:
    tar = IM._stage_tar("compute")
    ok(isinstance(tar, bytes) and len(tar) > 0,
       "bobi-compute's build context is assembled without the plugin", "empty tar")
except Exception as e:                                             # noqa: BLE001 — this is the verdict
    ok(False, "bobi-compute's build context is assembled without the plugin",
       "%s: %s" % (type(e).__name__, e))
finally:
    IM._repo_root = _real_root

# ─── 3. The thin layer's recipe is complete ──────────────────────────────────────────
print("\n3. bobi-decklink's recipe is complete")


def section_3():
    """Everything that READS the private submodule: absent from the public repository (_PLUGIN)."""
    try:
        tar = IM._stage_tar("decklink")
        ok(isinstance(tar, bytes) and len(tar) > 0,
           "bobi-decklink's build context is assembled (every COPY is staged)")
    except Exception as e:                                         # noqa: BLE001 — this is the verdict
        ok(False, "bobi-decklink's build context is assembled (every COPY is staged)",
           "%s: %s" % (type(e).__name__, e))

    dk = read("plugins/decklink_io/image/Dockerfile")
    ok(re.search(r"(?m)^ARG\s+BASE_IMAGE=", dk) is not None,
       "the Dockerfile carries an `ARG BASE_IMAGE=` (without it base_from rewrites nothing)")
    ok(dk.count("FROM ${BASE_IMAGE}") >= 2,
       "both stages start from the base image (builder + runtime)")
    # ⚠ LOOKING FOR THE NAMES "ldd" AND "libDeckLinkAPI" IN THE FILE PROVES NOTHING: they appear in
    # the COMMENT explaining the safeguard, so the guard stayed green while the condition had been
    # replaced by `if false` (mutation M13, 2026-11-01). We therefore read the CODE with comments
    # stripped, and require the condition itself.
    _code = "\n".join(l for l in dk.splitlines() if not l.lstrip().startswith("#"))
    ok(re.search(r'if\s+ldd\s+/usr/local/bin/\$b.*grep\s+-qv\s+"libDeckLinkAPI"', _code) is not None,
       "the ldd safeguard is a CONDITION in the code (libDeckLinkAPI excluded: a net, see dlopen)")
    ok("exit 1" in _code, "and it fails the build (exit 1), not just prints a message")

    meta_dk = json.loads(read("plugins/decklink_io/image/meta.json"))
    ok(bool(meta_dk.get("version")) and bool(meta_dk.get("image_tag")),
       "the image's meta.json carries both version AND image_tag")
    # ★ We do not invent the agreement rule between the two: `_image_tag` carries it, and it RAISES
    # on a divergence (bumping `version` while forgetting `image_tag` nearly overwrote the fleet's
    # reference image on 2026-09-12, see tests/verif_images_tags.py). So we call it.
    try:
        tag = IM._image_tag("decklink")
        ok(tag == meta_dk["image_tag"],
           "the tag resolves, and `version` does not diverge from `image_tag`", repr(tag))
    except Exception as e:                                         # noqa: BLE001 — this is the verdict
        ok(False, "the tag resolves, and `version` does not diverge from `image_tag`",
           "%s: %s" % (type(e).__name__, e))


if _PLUGIN:
    section_3()
else:
    abstain("bobi-decklink's recipe (section 3)",
            "its Dockerfile and meta.json live in the private submodule")

# ─── 4. The base image carries nothing of DeckLink any more ──────────────────────────
print("\n4. The bobi-compute base image no longer carries the DeckLink path")
cr = read("plugins/_compute_runtime/Dockerfile")
ok("decklink-builder" not in cr, "no DeckLink build stage left in the base Dockerfile")
ok(not re.search(r"(?m)^COPY\s+plugins/decklink_io", cr),
   "no COPY of the plugin's sources left in the base image")
ok(not re.search(r"(?m)^COPY\s+--from=decklink-builder", cr),
   "no COPY from the DeckLink stage left in the shipped stage")
ok(not any("decklink_io" in s for s, _d in IM._IMAGES["compute"]["stage"]),
   "the compute image's staging list no longer names the plugin",
   [s for s, _d in IM._IMAGES["compute"]["stage"] if "decklink_io" in s])
ok(re.search(r"(?m)^COPY\s+--from=mxl-builder\s+/opt/mxl-install/include/", cr) is not None,
   "the MXL headers stay in the shipped image (what makes the thin layer compilable)")

# ─── 5. The plugin targets this image ────────────────────────────────────────────────
print("\n5. The decklink_io plugin declares this variant, and the capability requires “compute”")
if _PLUGIN:
    man = json.loads(read("plugins/decklink_io/plugin.json"))
    ok(man.get("image") == "decklink",
       "the manifest declares \"image\": \"decklink\"", repr(man.get("image")))
    ok(man.get("needs_decklink") is True,
       "the RUNTIME path is unchanged: needs_decklink still declared")
else:
    abstain("the plugin's manifest (section 5)", "private submodule")
from app import plugins as PL                                             # noqa: E402
_saved = PL.REGISTRY.get("decklink_io")
PL.REGISTRY["decklink_io"] = {"image": "decklink"}
try:
    ok(PL.image_variant("decklink_io") == "decklink",
       "image_variant accepts “decklink” (allow-list extended)")
    PL.REGISTRY["decklink_io"] = {"image": "anythingatall"}
    ok(PL.image_variant("decklink_io") == "compute",
       "an unknown variant still falls back to compute")
finally:
    if _saved is None:
        PL.REGISTRY.pop("decklink_io", None)
    else:
        PL.REGISTRY["decklink_io"] = _saved
ok("decklink" in ND._CAP_ADDABLE, "the “decklink” capability can be declared on a node")
ok(ND._CAP_REQUIRES.get("decklink") == "compute",
   "it requires “compute” (the thin layer is a FROM bobi-compute)")

# ─── 6. The lists that FILTER name the new image ─────────────────────────────────────
print("\n6. The lists that filter name the new image")
ok("decklink" in ND._IMG_ORDER,
   "_IMG_ORDER names it (missing from it, the image shows NOWHERE and nobody builds it)")
ok("decklink" not in ND._SHARED_IMAGES,
   "it is not among the SHARED images (node_only: never pushed to the fleet)")
ok(ND._node_wants("decklink", {}, ["compute", "decklink"]) is True
   and ND._node_wants("decklink", {}, ["compute"]) is False,
   "a node wants it ONLY if it declares the capability")

# ─── 7. The deployment refusal is translated ─────────────────────────────────────────
print("\n7. The refusal without the capability is translated, FR and EN")
dc = read("app/docker_compute.py")
ok('variant in ("gui", "decklink")' in dc,
   "docker_compute handles the thin-layer variant (setting's tag + capability check)")
ok("alert.deploy.compute.sans_capacite_decklink" in dc,
   "the alert key is written IN FULL in the code (an assembled key escapes the i18n check)")
for cat in ("fr", "en"):
    d = json.loads(read("i18n/%s.json" % cat))
    key = "alert.deploy.compute.sans_capacite_decklink"
    t = d.get(key) or ""
    ok(bool(t) and "{n}" in t and "{vmid}" in t,
       "i18n/%s.json carries the key, with the node and the vmid" % cat, repr(t)[:90])

print()
if not_checked:
    print("── NOT CHECKED on this tree (%d): %s" % (len(not_checked), " · ".join(not_checked)))
if ko:
    print("✗ %d failure(s): %s" % (len(ko), " · ".join(ko[:4])))
    sys.exit(1)
print("✓ all green%s" % (" (with %d abstention(s) above)" % len(not_checked) if not_checked else ""))
sys.exit(0)
