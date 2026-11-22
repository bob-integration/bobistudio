#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Builds `catalog.json`, the static index of the catalogue (2026-11-22).

Run by the publication AFTER each release (core, plugin or service), then committed to the
`bobistudio-catalog` repository of the organisation. Installed instances read it from
raw.githubusercontent — a CDN, outside the GitHub API quota — instead of spending one API request
per repository (cf. `app/catalogue.py`, « Static INDEX »). Without it they still work, through
the API, and hit the 60 requests/hour of an anonymous address much sooner.

    GITHUB_TOKEN=… ./venv/bin/python tools/catalogue_index.py -o /path/to/bobistudio-catalog/catalog.json

The token only raises the API quota for THIS run (≈ 1 + one request per repository); the index
holds nothing secret. The output is refused when an entry could not be read (no release found
for a reason other than « none published »): publishing an index that hides a component would be
worse than keeping the previous one.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    from app import catalogue as cat
    from app import config
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--org", default=config.CATALOGUE_ORG)
    p.add_argument("-o", "--output", default="catalog.json")
    a = p.parse_args()
    jeton = (os.environ.get("GITHUB_TOKEN") or "").strip() or None
    cat._jeton = lambda: jeton                   # no Flask user here: the token of this run
    idx = cat.construire_index(a.org)
    douteux = [e["depot"] for e in idx["entries"]
               if not e.get("tag") and (e.get("indisponible") or "").startswith("interrogation impossible")]
    if douteux:
        sys.exit("index refused — releases unreadable for: %s (quota? network?)" % ", ".join(douteux))
    if not idx.get("core"):
        sys.exit("index refused — the core release is unreadable")
    with open(a.output, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print("%s: %d entries, core %s, org %s" % (a.output, len(idx["entries"]),
                                               idx["core"]["tag"], a.org))


if __name__ == "__main__":
    main()
