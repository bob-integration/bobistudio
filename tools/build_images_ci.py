#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Build the runtime images from a plain checkout — the CI step, runnable by hand.

WHY THIS EXISTS. An outside contributor (issue #1, 2026-09-23) could not build the product
from a source archive and spent six weeks on it. The root defect was that the SHARED base
image `bobi-compute` staged sources belonging to an OPTIONAL plugin, so nobody without that
plugin could build anything. Nothing in the test suite could have caught it: the suite never
assembles a build context, and the CI never ran Docker at all. The reporter's own suggestion
was the right one — have the CI build the images — and this is that step.

WHAT IT DOES, and what it deliberately does NOT do. It calls the PRODUCT's own staging
(`app.routes.images._stage_tar`), never a copy of it: a context assembled differently from the
way the orchestrator assembles it would prove nothing. It then runs `docker build` on that
context. It never contacts a node, never reads the orchestrator database for build targets and
never pushes anything — a build target and an image distribution need a cluster, which a CI
runner is not.

ABSTENTION, NOT FAILURE, FOR A MISSING PLUGIN. An image can be the runtime of a plugin whose
sources are not in this tree (they are separate repositories, and the published tree carries
only two of them). That is the NORMAL state of a public checkout, not a regression: the image
is then reported as `NOT CHECKED` and the run stays green. Everything else fails the run,
including a context that cannot be staged for any other reason — that class of defect is
exactly issue #1.

⚠ `NOT CHECKED` is the wording on purpose: the CI recap greps case-insensitively for
"NON MESURÉ|SAUTÉ|skipped|not checked" to list what a runner did not measure. Reword it into
something that list does not catch and an untested image starts passing for a tested one.

Usage:
    python tools/build_images_ci.py --list
    python tools/build_images_ci.py --stage-only
    python tools/build_images_ci.py --skip mtl
    python tools/build_images_ci.py --only compute,compute-gpu --timeout 1800
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.routes.images import _IMAGES, _image_tag, _repo_root, _stage_tar  # noqa: E402


def owning_plugin(which):
    """`plugins/<name>` whose sources this image needs, or None for a core runtime.

    Derived from the image's own `meta` path, so a new image declares this by existing rather
    than by being added to a list here — a hand-kept list of "images that need a plugin" is the
    kind that goes stale silently. `plugins/_*` are shared runtimes, always in the tree.
    """
    parts = _IMAGES[which]["meta"].split("/")
    if len(parts) > 2 and parts[0] == "plugins" and not parts[1].startswith("_"):
        return os.path.join("plugins", parts[1])
    return None


def build_order(names):
    """Thin layers after their base: `FROM bobi-compute:<ver>` only resolves once that tag is
    in the local daemon (`_stage_tar` rewrites ARG BASE_IMAGE to the base's CURRENT tag, so the
    base must have been built in THIS run — a stale local tag would be built against silently).
    """
    return sorted(names, key=lambda w: (1 if _IMAGES[w].get("base_from") else 0, w))


def docker_build(ctx, tag, timeout):
    """Extract the staged context and build it, exactly as the orchestrator does.

    ⚠ NOT `docker build -` with the tar on stdin, although that would be shorter. The product's
    own local build (`images._build_local_stream`) extracts to a directory and builds with
    `-f <dir>/Dockerfile <dir>`; this step exists to exercise what the product does, so it runs
    the same invocation, with the same DOCKER_BUILDKIT=1. Output goes straight to our stdout, so
    the CI log carries the compiler errors and not just a return code.
    """
    import io
    import shutil
    import tarfile
    import tempfile

    d = tempfile.mkdtemp(prefix="bobi-img-")
    try:
        with tarfile.open(fileobj=io.BytesIO(ctx), mode="r:gz") as t:
            t.extractall(d)   # noqa: S202 (context produced by us, a few lines above)
        env = dict(os.environ, DOCKER_BUILDKIT="1")
        proc = subprocess.Popen(["docker", "build", "--progress=plain", "-t", tag,
                                 "-f", os.path.join(d, "Dockerfile"), d],
                                stdout=sys.stdout, stderr=subprocess.STDOUT, env=env)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return "timeout after %ds" % timeout
        return None if proc.returncode == 0 else "docker build exited %d" % proc.returncode
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated image names (default: all)")
    ap.add_argument("--skip", default="", help="comma-separated image names to leave out")
    ap.add_argument("--stage-only", action="store_true",
                    help="assemble the build contexts and stop — no Docker daemon needed")
    ap.add_argument("--timeout", type=int, default=3600, help="per-image build timeout, seconds")
    ap.add_argument("--list", action="store_true", help="print the images and their tags, then exit")
    args = ap.parse_args()

    root = _repo_root()
    if args.list:
        for which in build_order(_IMAGES):
            owner = owning_plugin(which)
            print("%-12s %-28s %s" % (which, _image_tag(which), owner or "core runtime"))
        return 0

    wanted = [w.strip() for w in args.only.split(",") if w.strip()] or list(_IMAGES)
    skipped = {w.strip() for w in args.skip.split(",") if w.strip()}
    unknown = [w for w in list(wanted) + sorted(skipped) if w not in _IMAGES]
    if unknown:
        print("unknown image(s): %s — known: %s" % (", ".join(unknown), ", ".join(sorted(_IMAGES))))
        return 2

    built, not_checked, failed = [], [], []
    for which in build_order([w for w in wanted if w not in skipped]):
        owner = owning_plugin(which)
        print("\n── %s (%s)" % (which, _IMAGES[which]["label"]), flush=True)
        if owner and not os.path.isdir(os.path.join(root, owner)):
            # Normal in a public checkout: the plugin is installed from the Catalogue page, and
            # its sources are a separate repository. The orchestrator says the same thing when
            # the build is started from the Images page.
            print("  NOT CHECKED  %s" % which)
            print("               → runtime of plugin %s, which is not in this tree"
                  % os.path.basename(owner))
            not_checked.append(which)
            continue

        t0 = time.time()
        try:
            tag = _image_tag(which)
            ctx = _stage_tar(which)
        except Exception as exc:          # staging refused, or meta.json inconsistent
            print("  ✗ FAILED  context: %s" % exc)
            failed.append(which)
            continue
        print("  context staged: %d file(s), %.1f MiB → %s"
              % (len(_IMAGES[which]["stage"]), len(ctx) / 1048576.0, tag), flush=True)
        if args.stage_only:
            built.append(which)
            continue

        err = docker_build(ctx, tag, args.timeout)
        if err:
            print("  ✗ FAILED  %s (%.0fs)" % (err, time.time() - t0))
            failed.append(which)
        else:
            print("  ✓ built %s (%.0fs)" % (tag, time.time() - t0))
            built.append(which)

    print("\n── summary")
    print("   %-13s: %s" % ("staged" if args.stage_only else "built",
                            ", ".join(built) or "none"))
    print("   NOT CHECKED  : %s" % (", ".join(not_checked) or "none"))
    print("   failed       : %s" % (", ".join(failed) or "none"))
    for which in skipped:
        # A skip is a decision taken OUTSIDE this script (the workflow leaves out `mtl`, which
        # clones MTL and DPDK and does not fit a runner's budget). It is still an image nobody
        # measured, so it is announced in the same vocabulary rather than passed over in silence.
        print("   NOT CHECKED  %s — left out by --skip" % which)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
