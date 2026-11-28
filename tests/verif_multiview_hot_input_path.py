# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Multiview: a window hot-wired from the Cables page keeps its VIDEO component.

Tester report 2026-11-27 (multiview-3, PIP1, dl-360-1): wiring RX1 into a PiP from the Cables page
showed one frame, then black, until the wall was redeployed. The Composition page did not have the
problem.

Mechanism: the Cables page hot-wires a multiview window through `:8082/input`, video first
(`essence: video`) then its audio/ANC followers. The `/input` video branch only updated the reader
(`mv_state["inputs"]`) and left the window definition (`FLUX_CONFIG[idx]["path"]`) empty. Once the
audio follower set `audio_path`, `_classic_comps` saw "no video path, but an audio source" and
built an AUDIO-ONLY cell, with no video component, so the tile was no longer drawn.

This guard replays that exact sequence on the real handler, extracted from the rendered script
(same `str.format` as `plugins.render_script`), and checks the symptom itself: the cell still has
a video component. It also checks that a stale proxy list from the previous source is dropped, and
that the documented audio-only fallback still applies to a window whose video is UNWIRED.

No network, no database, no MXL: the handler only touches module state, which is stubbed here.

Usage:
    ./venv/bin/python tests/verif_multiview_hot_input_path.py
    MULTIVIEW_SCRIPT=/path/to/multiview/script.py ./venv/bin/python tests/verif_multiview_hot_input_path.py
"""
import ast
import io
import json
import os
import re
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.environ.get("MULTIVIEW_SCRIPT") or os.path.join(ROOT, "plugins", "multiview", "script.py")

# Module-level definitions taken from the real script (functions and the handler class).
WANTED_FUNCS = ("_as_bool", "_derive_audio_name", "_audio_name_for", "_classic_comps",
                "_tpl_dict_comps", "_tpl_comps", "_tpl_video_comp")
WANTED_ASSIGNS = ("AUDIO_PATH_NONE", "_CLASSIC_KEYS")
WANTED_CLASS = "MvControlHandler"

FAILURES = []


def check(cond, label):
    print(("[ OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        FAILURES.append(label)


def load():
    if not os.path.exists(SCRIPT):
        print(f"[SKIP] multiview script not found: {SCRIPT} (set MULTIVIEW_SCRIPT)")
        sys.exit(0)
    with open(SCRIPT, encoding="utf-8") as fh:
        rendered = fh.read().format(config=repr({}), hostname="mv-test", plugin_version="test")
    tree = ast.parse(rendered)
    picked = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCS:
            picked.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == WANTED_CLASS:
            picked.append(node)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name) and node.targets[0].id in WANTED_ASSIGNS):
            picked.append(node)
    names = {getattr(n, "name", None) or n.targets[0].id for n in picked}
    missing = (set(WANTED_FUNCS) | set(WANTED_ASSIGNS) | {WANTED_CLASS}) - names
    if missing:
        print(f"[FAIL] definitions not found in the script: {sorted(missing)}")
        sys.exit(1)

    class _BaseHandler:      # stands in for http.server.BaseHTTPRequestHandler
        pass

    ns = {"json": json, "re": re, "threading": threading, "BaseHTTPRequestHandler": _BaseHandler,
          "state_lock": threading.Lock(), "geom_dirty": threading.Event(),
          "tally_dirty": threading.Event(), "DEFAULT_TEMPLATE": None, "audio_states": {},
          "METER_BLOCKS": [], "VHIST_BLOCKS": [], "AHIST_BLOCKS": [], "_hist_cache": {}}
    exec(compile(ast.Module(body=picked, type_ignores=[]), SCRIPT, "exec"), ns)
    return ns


def window(path="", **extra):
    w = {"path": path, "hidden": False, "label_source": "hostname", "name": "", "in_w": 0, "in_h": 0,
         "x": 0, "y": 0, "w": 640, "h": 360, "show_label": True, "show_tally": False,
         "meter_channels": 0, "template": None, "template_ref": ""}
    w.update(extra)
    return w


def post(ns, body):
    h = object.__new__(ns[WANTED_CLASS])
    h.path = "/input"
    h._json = lambda: dict(body)
    h.send_response = lambda code: setattr(h, "code", code)
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.wfile = io.BytesIO()
    h.do_POST()
    return h.code


def reset(ns, windows):
    ns["FLUX_CONFIG"] = windows
    ns["mv_state"] = {"inputs": [w.get("path", "") for w in windows]}
    ns["geom_dirty"].clear()
    ns["tally_dirty"].clear()


def has_video(ns, cfg):
    return ns["_tpl_video_comp"](cfg) is not None


def main():
    ns = load()

    # 1. The reported case: an UNWIRED PiP, wired from the Cables page (video, then audio follower).
    reset(ns, [window()])
    check(post(ns, {"idx": 0, "shm": "rx-node_1", "essence": "video"}) == 200, "video /input accepted")
    check(has_video(ns, ns["FLUX_CONFIG"][0]), "after video wiring: the cell has a video component")
    check(post(ns, {"idx": 0, "shm": "rx-node_audio_1", "essence": "audio"}) == 200,
          "audio follower /input accepted")
    cfg = ns["FLUX_CONFIG"][0]
    check(cfg.get("path") == "/dev/shm/rx-node_1",
          "the window definition follows the wired video (FLUX_CONFIG path)")
    check(ns["mv_state"]["inputs"][0] == "/dev/shm/rx-node_1", "the reader input is the wired video")
    check(has_video(ns, cfg),
          "after the audio follower: the cell STILL has a video component (no 'one frame then black')")
    check(ns["geom_dirty"].is_set(), "the static layer is re-baked (label/template follow the new source)")

    # 2. Re-wiring a window that had a source: the previous source's pyramid proxies are dropped.
    reset(ns, [window("/dev/shm/old-src_1",
                      proxies=[{"path": "/dev/shm/old-src_1__p2", "w": 960, "h": 540}])])
    post(ns, {"idx": 0, "shm": "new-src_1", "essence": "video"})
    check("proxies" not in ns["FLUX_CONFIG"][0],
          "proxies of the previous source are dropped on re-wiring")

    # 3. Same source wired again: nothing to re-bake (an idempotent hot-input stays cheap).
    reset(ns, [window("/dev/shm/same_1")])
    post(ns, {"idx": 0, "shm": "same_1", "essence": "video"})
    check(not ns["geom_dirty"].is_set(), "re-wiring the same source does not trigger a re-bake")

    # 4. The documented fallback is untouched: video UNWIRED + audio still wired = audio-only cell.
    reset(ns, [window("/dev/shm/src_1", audio_path="/dev/shm/src_audio_1")])
    post(ns, {"idx": 0, "shm": "", "essence": "video"})
    check(ns["FLUX_CONFIG"][0].get("path") == "", "unwiring the video clears the window path")
    check(not has_video(ns, ns["FLUX_CONFIG"][0]),
          "video unwired with audio kept: audio-only cell (documented fallback preserved)")

    # 5. Out-of-range index is still refused.
    reset(ns, [window()])
    check(post(ns, {"idx": 5, "shm": "x_1", "essence": "video"}) == 400, "out-of-range idx refused")

    # 6. The label follows a hot-wire: the orchestrator sends the name it computed.
    reset(ns, [window()])
    post(ns, {"idx": 0, "shm": "rx-node_1", "essence": "video", "name": "2110-io-node"})
    check(ns["FLUX_CONFIG"][0].get("name") == "2110-io-node", "hot-wire carries the tile NAME")
    reset(ns, [window("/dev/shm/same_1", name="old")])
    post(ns, {"idx": 0, "shm": "same_1", "essence": "video", "name": "new"})
    check(ns["FLUX_CONFIG"][0].get("name") == "new" and ns["geom_dirty"].is_set(),
          "a name change alone (same source) re-bakes the label")
    reset(ns, [window("/dev/shm/same_1", name="same")])
    post(ns, {"idx": 0, "shm": "same_1", "essence": "video", "name": "same"})
    check(not ns["geom_dirty"].is_set(), "same source and same name: no re-bake")
    reset(ns, [window("/dev/shm/a_1", name="kept")])
    post(ns, {"idx": 0, "shm": "b_1", "essence": "video"})
    check(ns["FLUX_CONFIG"][0].get("name") == "kept", "older core (no name key): name untouched")

    # 7. Core side: the Cables page puts the name in the hot body (app/routes/cabling.py).
    csrc = open(os.path.join(ROOT, "app", "routes", "cabling.py"), encoding="utf-8").read()
    fn = [n for n in ast.parse(csrc).body
          if isinstance(n, ast.FunctionDef) and n.name == "_hot_list_body"]
    check(bool(fn), "cabling._hot_list_body exists")
    if fn:
        g = {}
        exec(compile(ast.Module(body=fn, type_ignores=[]), "cabling", "exec"), g)
        body = g["_hot_list_body"]("idx", 2, "rx_1", "video", {"name": "RX 1"}, "name")
        check(body == {"idx": 2, "shm": "rx_1", "essence": "video", "name": "RX 1"},
              "hot body carries the entry name when the list declares name_field")
        check("name" not in g["_hot_list_body"]("idx", 0, "x", "video", {"name": "n"}, None),
              "no name_field: no name key")
    check(csrc.count("_hot_list_body(ikey, idx, shm, kind, entry, nf)") == 1,
          "the list-entry hot-wire uses _hot_list_body")

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
