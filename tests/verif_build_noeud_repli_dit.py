#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The image build node chosen by the operator, when unavailable, is no longer ignored in silence.

`image_build_node` = '<node_id>' of a node gone or without agent used to build on the controller
without a word (found 2026-11-23). The fallback stays (a dozen callers expect a target), but it is
SAID: one alert per hour naming the node. Held here, offline:
  1. a node with an agent: built there, nothing said;
  2. a node without agent / unknown: on the controller, alerted once, not again within the hour;
  3. « local » or auto: nothing said; labels in both languages.

MUTATION, BUILD_MUTATION=muet — must give rc=1 (the fallback silent again).

    ./venv/bin/python tests/verif_build_noeud_repli_dit.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tempfile                                                               # noqa: E402
from app import config, database                                              # noqa: E402
_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_build_repli_"), "t.db")
config.DB_PATH = database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()
from app.routes import images                                                 # noqa: E402

if os.environ.get("BUILD_MUTATION") == "muet":
    _src = open(images.__file__, encoding="utf-8").read()
    _old = "        _say_build_fallback(sel, n)\n"
    if _src.count(_old) != 1:
        print("mutation: pattern stale")
        sys.exit(1)
    exec(compile(_src.replace(_old, ""), images.__file__, "exec"), images.__dict__)
    print("MUTATION active: muet")

ko = 0


def verifie(cond, libelle, detail=None):
    global ko
    print(("  ✓ " if cond else "  ✗ ") + libelle + ("" if cond or detail is None else f"  [{detail}]"))
    if not cond:
        ko += 1


from app import settings                                                      # noqa: E402
SET = {}
settings.get = lambda k, d=None: SET.get(k, d)
NODES = {3: {"id": 3, "name": "r620-x", "agent_url": "https://a:9100"}, 4: {"id": 4, "name": "z8", "agent_url": ""}}
images.db_get_node = lambda nid: NODES.get(int(nid))
said = []
images.db_add_alert = lambda key, *a, **k: said.append((key, k.get("params")))

SET["image_build_node"] = "3"
verifie(images._build_target() == ("node", NODES[3]) and not said, "a node with an agent: there, silent")
SET["image_build_node"] = "4"
verifie(images._build_target() == ("local", None) and len(said) == 1
        and said[0][0] == "alert.image.build_node_indisponible" and said[0][1]["n"] == "z8"
        and said[0][1]["why"] == "agent", "★ no agent: controller, and said, naming the node", said)
images._build_target()
images._build_target()
verifie(len(said) == 1, "not again within the hour", len(said))
SET["image_build_node"] = "9"
images._build_target()
verifie(len(said) == 2 and said[1][1]["why"] == "unknown", "an unknown node: said too", said[-1:])
said.clear()
SET["image_build_node"] = "local"
images._build_target()
SET["image_build_node"] = ""
images._primary_host = lambda: ""
images._build_target()
verifie(not said, "« local » and auto: nothing to say")
for lang in ("fr", "en"):
    cat = json.load(open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8"))
    v = cat.get("alert.image.build_node_indisponible") or ""
    verifie("{n}" in v and "{why}" in v, "%s: label" % lang)

print("\n%s" % ("OK" if not ko else "%d ÉCHEC(S)" % ko))
sys.exit(1 if ko else 0)
