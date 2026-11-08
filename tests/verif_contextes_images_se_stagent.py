#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Every runtime image's build context can be assembled, for EVERY image (2026-11-02).

★ WHY THIS GUARD EXISTS — a measured gap, not a precaution.
`_stage_tar` carries two safeguards that matter: it refuses a context with a missing source, and
it refuses a Dockerfile that `COPY`s something the staging list does not provide. Both only fire
when SOMEONE CALLS `_stage_tar`. Measured on 2026-11-02, the offline suite called it for three
images out of seven:

    compute      verif_image_decklink_couche_fine, verif_staging_nomme_le_plugin
    gui          verif_capacite_gui
    decklink     verif_image_decklink_couche_fine
    media, webrtc, compute-gpu, mtl    — NOBODY

So a `COPY` added to `plugins/_media_runtime/Dockerfile` without its staging entry broke nothing
here: it was discovered by a human clicking “Build” on the Images page, and buildkit then reported
only `lstat …/buildkit-mount…: no such file or directory`. That exact defect has already been paid
three times (`_NTCOPY`, `_SPLIT_GPU`, and `atom_wire.h`, which left bobi-gui unbuildable from
2026-10-18). This guard closes the list rather than one more image: it walks `_IMAGES`, so a new
image is covered by existing, not by someone remembering to add it here.

IT IS THE CHEAP HALF OF THE CI IMAGE STEP. `.github/workflows/images.yml` really builds the images
(tens of minutes, path-filtered, nightly); this runs in the blocking suite on every push and needs
no Docker daemon. The expensive step catches what only a compiler can catch; this one catches the
staging mistakes, immediately.

ABSTENTION, NOT FAILURE, FOR A MISSING PLUGIN. An image can be the runtime of a plugin whose
sources are a separate repository, absent from the published tree. That is its NORMAL state there,
so the image is reported `NOT CHECKED`. An image whose plugin IS present but incomplete FAILS —
that is a broken tree, and telling the two apart is the point of the `plugins/<name>` test below.

⚠ “NOT CHECKED” is matched by the CI recap, which greps case-insensitively for
“NON MESURÉ|SAUTÉ|skipped|not checked” (see .github/workflows/ci.yml, “PLUSIEURS VOCABULAIRES,
PAS UN SEUL”) — do not reword it into something that list does not catch.

Offline: throwaway database, no network, no node, no docker. Sections:
  0. the images are read from the product, and every one of them gets a verdict;
  1. each image: its tag resolves and its build context assembles (or abstains, named);
  2. a thin layer's `FROM` is rewritten to the base image's CURRENT tag.

    ./venv/bin/python tests/verif_contextes_images_se_stagent.py
"""
import io
import os
import re
import sys
import tarfile
import tempfile

_R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _R)
_TMP = tempfile.mkdtemp(prefix="verif_contextes_images_")
from app import config, database                                          # noqa: E402
config.DB_PATH = database.DB_PATH = os.path.join(_TMP, "db.sqlite")  # BEFORE any access (get_db pins)

from app.routes import images as IM                                       # noqa: E402

ko = []
not_checked = []


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


def owning_plugin(which):
    """`plugins/<name>` whose sources this image needs, or None for a core runtime.

    Derived from the image's own `meta` path — the same rule as `tools/build_images_ci.py`, and
    for the same reason: a hand-kept list of “images that need a plugin” goes stale in silence.
    `plugins/_*` are shared runtimes, always in the tree.
    """
    parts = IM._IMAGES[which]["meta"].split("/")
    if len(parts) > 2 and parts[0] == "plugins" and not parts[1].startswith("_"):
        return os.path.join("plugins", parts[1])
    return None


def staged_dockerfile(tar_bytes):
    """The Dockerfile AS STAGED (so, after any rewrite), or None."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as t:
        try:
            f = t.extractfile("Dockerfile")
        except KeyError:
            return None
        return f.read().decode("utf-8", "replace") if f else None


# ─── 0. The images are the product's own, and none escapes a verdict ─────────────────
print("\n0. The images are read from the product")
ok(len(IM._IMAGES) >= 5, "the image list comes from images._IMAGES", sorted(IM._IMAGES))
# Thin layers after their base, like the build itself: `_stage_tar` rewrites a thin layer's
# BASE_IMAGE to the base's CURRENT tag, so section 2 needs the base's tag resolved first.
order = sorted(IM._IMAGES, key=lambda w: (1 if IM._IMAGES[w].get("base_from") else 0, w))
seen = []

# ─── 1. Each image: tag + build context ──────────────────────────────────────────────
print("\n1. Each image's tag resolves and its build context assembles")
tars = {}
for which in order:
    seen.append(which)
    spec = IM._IMAGES[which]
    owner = owning_plugin(which)
    if owner and not os.path.isdir(os.path.join(_R, owner)):
        abstain("the “%s” image's build context" % which,
                "runtime of plugin %s, whose sources are not in this tree (installed from the "
                "Catalogue page)" % os.path.basename(owner))
        continue
    # The staging list must name a Dockerfile: without it `_stage_tar`'s COPY safeguard has
    # nothing to read, and the build would have no recipe at all.
    ok(any(dst == "Dockerfile" for _s, dst in spec["stage"]),
       "“%s”: the staging list provides a Dockerfile" % which)
    try:
        tag = IM._image_tag(which)
        ok(bool(tag) and ":" in tag and tag.startswith(spec["prefix"]),
           "“%s”: the tag resolves to %s" % (which, tag), repr(tag))
    except Exception as e:                                         # noqa: BLE001 — this is the verdict
        # `_image_tag` RAISES on a meta.json where `version` and `image_tag` disagree; that is a
        # refusal we want to see here, not a tag chosen silently (nearly overwrote the fleet's
        # reference image on 2026-09-12).
        ok(False, "“%s”: the tag resolves" % which, "%s: %s" % (type(e).__name__, e))
        continue
    try:
        tars[which] = IM._stage_tar(which)
        ok(isinstance(tars[which], bytes) and len(tars[which]) > 0,
           "“%s”: the build context assembles (%d entries)" % (which, len(spec["stage"])))
    except Exception as e:                                         # noqa: BLE001 — this is the verdict
        ok(False, "“%s”: the build context assembles" % which, "%s: %s" % (type(e).__name__, e))

ok(sorted(seen) == sorted(IM._IMAGES),
   "every declared image got a verdict (measured or abstained)",
   sorted(set(IM._IMAGES) - set(seen)))

# ─── 2. A thin layer's FROM points at the base image's CURRENT tag ───────────────────
print("\n2. A thin layer's FROM is rewritten to the base image's current tag")
# ⚠ ANCHORED ON THE DOCKERFILE, NOT ON THE DECLARATION. Asking “for each image declaring
# base_from…” would go green by DROPPING `base_from` — which is the very desync being guarded
# against (a thin layer left on a frozen BASE_IMAGE pulls a nonexistent image from docker.io and
# fails to authenticate). So the question is asked the other way round: a Dockerfile that carries
# an `ARG BASE_IMAGE=` MUST have a `base_from`, and the staged copy must carry the base's tag.
_thin = 0
for which in order:
    if which not in tars:
        continue
    df = staged_dockerfile(tars[which])
    if df is None or not re.search(r"(?m)^ARG\s+BASE_IMAGE=", df):
        continue
    _thin += 1
    base = IM._IMAGES[which].get("base_from")
    ok(bool(base), "“%s”: its Dockerfile has ARG BASE_IMAGE → base_from is declared" % which,
       "without base_from the ARG stays at whatever the file says")
    if not base:
        continue
    want = IM._image_tag(base)
    ok(re.search(r"(?m)^ARG\s+BASE_IMAGE=" + re.escape(want) + r"\s*$", df) is not None,
       "“%s”: the staged Dockerfile points at %s (the base's current tag)" % (which, want),
       (re.search(r"(?m)^ARG\s+BASE_IMAGE=.*$", df) or [""])[0] if df else "")
if _thin:
    print("  (%d thin layer(s) measured)" % _thin)
else:
    abstain("the thin layers' FROM rewrite (section 2)",
            "no image with an ARG BASE_IMAGE could be staged in this tree")

print()
if not_checked:
    print("── NOT CHECKED on this tree (%d): %s" % (len(not_checked), " · ".join(not_checked)))
if ko:
    print("✗ %d failure(s): %s" % (len(ko), " · ".join(ko[:4])))
    sys.exit(1)
print("✓ all green%s" % (" (with %d abstention(s) above)" % len(not_checked) if not_checked else ""))
sys.exit(0)
