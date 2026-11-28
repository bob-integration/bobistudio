#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Composition: what the instance-list sort needs from the server, and where its control shows.

The list can be sorted by name, project or CAPACITY (asked by the tester on 2026-11-27). Capacity
is the number of MANDATORY input ports the manifest's wiring resolves against the deployed params
(`plugin_registry._count_inputs`): one per multiview tile, `n_inputs` for a mixer or a pyramid.
The sort itself is checked in `verif_composition_tri.js`; here:
  1. `/api/plugins/instances` publishes `n_inputs`, following the deployed params;
  2. optional ports do not count (a tile's audio/ANC rides along the tile);
  3. a wiring that cannot be resolved gives None (sorted last), not a 500;
  4. the sort control is on the Composition page and NOT on another rubrique;
  5. its labels exist in both languages.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _composants import exiger                                     # noqa: E402

exiger("plugins/pyramide")   # a "repeat: n_inputs" wiring, shipped with the public copy

from app import config, database                                   # noqa: E402

FAILURES = []


def check(cond, label):
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        FAILURES.append(label)


def main():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    config.DB_PATH = path
    database.DB_PATH = path
    database._tls.__dict__.pop("conn", None)
    database.init_db()

    import main as _main
    from app import plugins
    from app.auth import hash_password
    from app.routes import plugin_registry as pr

    def ctr(vmid, params):
        return {"vmid": vmid, "hostname": "pyr-%d" % vmid, "status": "running", "project_id": None,
                "deploy_config": json.dumps({"type": "pyramide", "params": params})}

    pr.db_get_containers = lambda: [ctr(1, {"n_inputs": 8}), ctr(2, {"n_inputs": 3})]
    database.db_fabric_all = lambda node_id=None: []

    database.db_create_user("trial", hash_password("Tulipe-Vent-9312"), "admin", None, None, None)
    cli = _main.app.test_client()
    with cli.session_transaction() as sess:
        sess["user_id"] = database.db_get_user("trial")["id"]

    got = {x["vmid"]: x.get("n_inputs") for x in cli.get("/api/plugins/instances?type=pyramide").get_json()}
    check(got == {1: 8, 2: 3}, "n_inputs follows the deployed params: %r" % got)

    # 2 — optional ports are left out (multiview-like wiring, built by hand: the plugin may be absent).
    real = plugins.derive_wiring
    plugins.derive_wiring = lambda *a, **k: {"consumes": [
        {"essence": "video"}, {"essence": "video"}, {"essence": "audio", "optional": True}]}
    check(pr._count_inputs("x", "h", {}) == 2, "optional ports do not count")
    # 3 — unresolvable wiring → None, and the list still answers.
    def boom(*a, **k):
        raise ValueError("bad manifest")
    plugins.derive_wiring = boom
    r = cli.get("/api/plugins/instances?type=pyramide")
    check(r.status_code == 200 and all(x.get("n_inputs") is None for x in r.get_json()),
          "an unresolvable wiring gives None, not a 500")
    plugins.derive_wiring = real

    # 4 — the control is on Composition only.
    comp = cli.get("/composition").get_data(as_text=True)
    other = cli.get("/traitements").get_data(as_text=True)
    check('id="tp-sort"' in comp and 'id="tp-sort-menu"' in comp, "Composition carries the sort control")
    check('id="tp-sort"' not in other, "another rubrique does not")
    check(all('data-sort="%s"' % k in comp for k in ("deploy", "alpha", "project", "inputs")),
          "the four orders are offered")

    # 5 — labels in both languages.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    keys = ["section.sort." + k for k in ("title", "deploy", "alpha", "project", "inputs")]
    for lang in ("fr", "en"):
        cat = json.load(open(os.path.join(root, "i18n", lang + ".json"), encoding="utf-8"))
        check(all(cat.get(k) for k in keys), "labels present in %s" % lang)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
