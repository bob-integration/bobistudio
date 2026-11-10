#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Node image alerts must name a REAL gap (app/routes/images.py, 2026-11-09).

The base held 1 131 « images absent » alerts in 8 days, none of them a real failure. Three causes:

  1. after a build, the tag is written twice: before the distribution, then after it. The FIRST
     pass alerted « not confirmed » on every node that had not received the bits yet — that is
     the expected state (dell-1, 2026-11-07, 18 s before its distribution succeeded);
  2. the drift check compared each node to the REPOSITORY tag, i.e. the next version: bumping
     bobi-compute to 0.81 made dl360-1 and dell-1 « missing » it for six days before it was even
     built. It must check the tag the node RUNS (its column);
  3. an UNREADABLE inventory was read as « every image absent » (dl360-1, 2026-11-09 08:48).

What still has to alert, and does: a node whose column names an image its disk does not hold,
and the pass AFTER the distribution on a node the bits never reached.

    ./venv/bin/python tests/verif_images_fausses_alertes.py
"""
import ast
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                              # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_images_alertes_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

from app.routes import images as im                                           # noqa: E402

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


alertes = []
inventaire = {}            # node id -> (list of tags, err)
im.db_add_alert = lambda msg, niveau="info", **kw: alertes.append((msg, kw.get("node_id")))
im.db_update_node = lambda nid, **kw: None
im._node_expects_image = lambda which, node: which == "compute"
im._node_image_inventory = lambda node, force=False: (
    [{"tag": t} for t in inventaire[node["id"]][0]], inventaire[node["id"]][1])
im._image_tag = lambda which: "bobi-compute:0.81"


def noeud(nid, colonne):
    return {"id": nid, "name": "n%d" % nid, "agent_url": "https://192.0.2.1:9100", "compute_image": colonne}


def derive(n, tags, err=""):
    inventaire[n["id"]] = (tags, err)
    im._derive_vue.clear()
    del alertes[:]
    return im.verifier_derive_images(n)


print("\n2. drift is measured against the tag the node RUNS, not the next version")
n = noeud(1, "bobi-compute:0.80")
r = derive(n, ["bobi-compute:0.80"])
verifie(r == [] and not alertes, "column 0.80, disk 0.80, repository already at 0.81 → no alert", (r, alertes))
r = derive(n, ["bobi-compute:0.79"])
verifie(r == ["bobi-compute:0.80"] and alertes == [("alert.image.derive_noeud", 1)],
        "column 0.80, disk without it → alerted, naming 0.80", (r, alertes))
r = derive(noeud(2, ""), [])
verifie(r == ["bobi-compute:0.81"] and alertes == [("alert.image.jamais_construite", 2)],
        "empty column → the repository tag, « never built » message", (r, alertes))

print("\n3. an unreadable inventory says nothing")
r = derive(n, [], err="connection refused")
verifie(r is None and not alertes, "inventory error → no « absent » alert", (r, alertes))

print("\n1. the pass BEFORE the distribution does not alert, the one after does")
nodes = [noeud(3, "bobi-compute:0.80"), noeud(4, "bobi-compute:0.80")]
im.db_get_nodes = lambda: nodes
inventaire[3] = (["bobi-compute:0.81"], "")          # build node
inventaire[4] = (["bobi-compute:0.80"], "")          # bits not arrived yet
im._image_present_node = lambda node, tag, force=False: tag in inventaire[node["id"]][0]
del alertes[:]
im._autofill_nodes_image("compute", "bobi-compute:0.81", alert=False)
verifie(not alertes, "first pass: node 4 without the bits yet → silent", alertes)
im._autofill_nodes_image("compute", "bobi-compute:0.81")
verifie(alertes == [("alert.image.tag_non_confirme", 4)],
        "second pass: node 4 still without the bits → alerted", alertes)

# The build worker must call the silent pass before `_distribute_image` and the alerting one after.
src = open(os.path.join(RACINE, "app", "routes", "images.py"), encoding="utf-8").read()
fn = next(f for f in ast.walk(ast.parse(src))
          if isinstance(f, ast.FunctionDef) and f.name == "_build_image_worker")
appels = sorted((c.lineno, c.func.id, {k.arg: getattr(k.value, "value", None) for k in c.keywords})
                for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id in ("_autofill_nodes_image", "_distribute_image"))
ordre = [(nom, kw.get("alert", True)) for _l, nom, kw in appels]
verifie(ordre == [("_autofill_nodes_image", False), ("_distribute_image", True),
                  ("_autofill_nodes_image", True)],
        "build worker: silent pass → distribution → alerting pass", ordre)

print()
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
