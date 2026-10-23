# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Workspace "functions": the fine-grained controls an operator picks to compose a view.

"""Derive the functions of a container from what its plugin ALREADY declares.

A workspace view used to hold either a plugin's whole UI or a shotbox of hand-made buttons.
A *function* sits in between: one coherent control (the PGM row of a mixer, REC/STOP of a
recorder, the image shown by a stills player, the duration of a recording) that an operator
drops into a panel next to functions of OTHER containers.

Nothing here asks a plugin to write UI. Everything is deduced from the manifest:

  · an action whose single CHOICE parameter is mirrored by a state → a SELECT row
    (mixer `pgm{input}` + state `pgm`; stills `select{file}` + feedback `file`);
  · actions with no required parameter → BUTTONS, grouped when several of them drive the
    same boolean state (recorder start/stop on `recording`, player play/pause on `paused`);
  · an action with a single boolean parameter → an ON/OFF pair;
  · every declared state → a DISPLAY, formatted from its type;
  · every image read endpoint → an IMAGE (low-rate thumbnail through the plugin proxy).

Bounded parameters (faders, mutes…) come from the live parameter tree, which needs the
running container: the page derives those on demand (`/api/containers/<vmid>/param_tree`).

A plugin may later EXPORT richer pieces of UI; those are the advanced case, not the base.
"""

# Actions that are diagnostics, never operator functions.
_SKIP_ACTIONS = {"log_level"}

# Choice sources that make a parameter "the thing you pick" (a row of buttons).
_LIVE_CHOICE = ("inputs", "recall", "tool_memories")


def _is_choice(p):
    return bool(p.get("options_from") in _LIVE_CHOICE or p.get("options_endpoint")
                or (p.get("options") and p.get("type") != "bool"))


def _required(p):
    """A parameter naming a target the action cannot guess (an input, a file, a strip)."""
    return "default" not in p and (p.get("options_from") in _LIVE_CHOICE
                                   or bool(p.get("options_endpoint")))


def _optional(p):
    """A parameter the action runs fine without: a default, a boolean, or a closed list of
    settings (recorder start: profile, layout…) the container already holds."""
    # A bare number (a fade time, a duration) left out means "the container's own value".
    return ("default" in p or p.get("type") in ("bool", "number") or bool(p.get("options")))


def _choice_param(action):
    """The single choice parameter of `action`, or None. Other parameters must be optional."""
    params = action.get("params") or []
    choices = [p for p in params if p.get("options_from") in _LIVE_CHOICE or p.get("options_endpoint")]
    if len(choices) != 1:
        return None
    if not all(_optional(p) for p in params if p is not choices[0]):
        return None
    return choices[0]


def _state_for(action, key, states):
    """State mirroring the value chosen through `action` (its `feedback` hint, else same id)."""
    fb = action.get("feedback") or {}
    if fb.get("state") and fb.get("value_from") == key and fb["state"] in states:
        return fb["state"]
    if action.get("id") in states:
        return action["id"]
    return None


def _display_format(s):
    fmt = s.get("format")
    if fmt:
        return fmt
    t = s.get("type")
    sid = (s.get("id") or "").lower()
    if t == "bool":
        return "bool"
    if t == "number" and (sid.endswith("_s") or "duration" in sid or sid == "position"):
        return "duration"
    if t == "number" and "bytes" in sid:
        return "bytes"
    return "number" if t == "number" else "text"


def _pair_label(labels):
    """« Démarrer l'enregistrement » + « Arrêter l'enregistrement » → « Démarrer / Arrêter
    l'enregistrement » : la fin commune n'est dite qu'une fois."""
    words = [str(lb).split() for lb in labels]
    n = 0
    while all(len(w) > n + 1 for w in words) and len({tuple(w[len(w) - n - 1:]) for w in words}) == 1:
        n += 1
    if not n:
        return " / ".join(labels)
    tail = " ".join(words[0][len(words[0]) - n:])
    return " / ".join(" ".join(w[:len(w) - n]) for w in words) + " " + tail


# Names of the image read endpoints a broadcast operator recognises (else the file stem).
_IMAGE_NAMES = {"program": "projects.fn_name_img_program", "preview": "projects.fn_name_img_preview"}


def derive_functions(manifest, entry):
    """Functions of one container. `entry` is its action-catalog entry (actions with resolved
    recall options, states, wired inputs). Returns a list of plain dicts, JSON-ready; each
    carries a stable `fid` so a saved view can tell whether it still exists."""
    actions = [a for a in (entry.get("actions") or []) if a.get("id") not in _SKIP_ACTIONS]
    states = {s["id"]: s for s in (entry.get("state") or []) if s.get("id")}
    out = []
    used = set()
    choice_of = {}   # state id → where its values get their NAMES (a select over that state)

    # 1. SELECT rows: one choice parameter, the chosen value readable back.
    for a in actions:
        p = _choice_param(a)
        if not p:
            continue
        key = p.get("key")
        st = _state_for(a, key, states)
        label = a.get("label") or a["id"]
        if p.get("options_from") == "inputs":
            # « PGM → entrée » : sur un mélangeur, c'est la rangée PGM.
            label = label.split("→")[0].strip() or label
        fn = {"fid": "select:" + a["id"], "kind": "select", "label": label,
              "label_key": "projects.fn_name_row" if p.get("options_from") == "inputs" else None,
              "group": "command",
              "action_id": a["id"], "param": key,
              "state_id": st,
              "options_from": p.get("options_from"),
              "options_endpoint": p.get("options_endpoint"),
              "options_path": p.get("options_path"),
              "option_value": p.get("option_value"), "option_label": p.get("option_label"),
              # a picture per choice (stills, player): the view can show tiles, not names
              "option_thumb": p.get("option_thumb"),
              "options": p.get("options") or [],
              "value_type": p.get("type") or "text"}
        out.append({k: v for k, v in fn.items() if v not in (None, "")})
        used.add(a["id"])
        if st:
            choice_of.setdefault(st, {k: fn[k] for k in ("options_from", "options_endpoint", "options_path",
                                                         "option_value", "option_label", "options",
                                                         "action_id", "param") if fn.get(k)})

    # 1 bis. Rows choosing from the SAME list (stills: « preview » and « afficher » over /images)
    #        light each other's state as a second state: the image READY and the one ON AIR.
    sels = [f for f in out if f["kind"] == "select" and f.get("state_id")]
    for f in sels:
        src = (f.get("options_endpoint"), f.get("options_from"), f.get("param"))
        f["also_states"] = [g["state_id"] for g in sels
                            if g is not f and (g.get("options_endpoint"), g.get("options_from"), g.get("param")) == src]
        if not f["also_states"]:
            del f["also_states"]

    # 2. BUTTONS driving one boolean state (start/stop, play/pause): one function, one button
    #    per action, each lit when the state holds the value that action sets.
    by_state = {}
    for a in actions:
        if a["id"] in used or any(_required(p) for p in (a.get("params") or [])):
            continue
        fb = a.get("feedback") or {}
        # Parameters of such an action are its options (recorder start: loop, profile…), all
        # optional: the button runs it with the container's own settings.
        if fb.get("state") in states and "value" in fb:
            by_state.setdefault(fb["state"], []).append(a)
    for sid, group in by_state.items():
        if len(group) < 2:
            continue
        out.append({"fid": "buttons:" + sid, "kind": "buttons", "group": "command",
                    "label": _pair_label([a.get("label") or a["id"] for a in group]),
                    "buttons": [{"label": a.get("label") or a["id"], "action_id": a["id"],
                                 "lit": {"state_id": sid, "value": a["feedback"]["value"]}}
                                for a in group]})
        used.update(a["id"] for a in group)

    # 3. A single boolean parameter: an ON / OFF pair.
    for a in actions:
        if a["id"] in used:
            continue
        params = a.get("params") or []
        if len(params) == 1 and params[0].get("type") == "bool":
            key = params[0].get("key")
            fb = a.get("feedback") or {}
            sid = (fb.get("state") if fb.get("state") in states else None) or _state_for(a, key, states)
            lit = (lambda v: {"state_id": sid, "value": v}) if sid else (lambda v: None)
            btns = [{"label": "on", "label_key": "projects.fn_on", "action_id": a["id"],
                     "params": {key: True}, "lit": lit(True)},
                    {"label": "off", "label_key": "projects.fn_off", "action_id": a["id"],
                     "params": {key: False}, "lit": lit(False)}]
            out.append({"fid": "toggle:" + a["id"], "kind": "buttons", "group": "command",
                        "label": a.get("label") or a["id"],
                        "buttons": [{k: v for k, v in b.items() if v is not None} for b in btns]})
            used.add(a["id"])

    # 4. Every other action runnable as is — all its parameters optional — becomes a single
    #    button (lit from its feedback if any). One needing a value (a text) stays a shotbox
    #    matter, where the operator types that value once.
    for a in actions:
        if a["id"] in used or not all(_optional(p) for p in (a.get("params") or [])):
            continue
        fb = a.get("feedback") or {}
        lit = ({"state_id": fb["state"], "value": fb["value"]}
               if fb.get("state") in states and "value" in fb else None)
        b = {"label": a.get("label") or a["id"], "action_id": a["id"]}
        if lit:
            b["lit"] = lit
        out.append({"fid": "button:" + a["id"], "kind": "buttons", "group": "command",
                    "label": a.get("label") or a["id"], "buttons": [b]})
        used.add(a["id"])

    # 5. DISPLAYS: every declared state. One that a command reflects (the input on air, the
    #    recording flag), a yes/no or a time is an operator display; the rest — settings echoed
    #    back, counters — is diagnostics, offered under "more".
    lit_states = {b["lit"]["state_id"] for f in out for b in f.get("buttons", []) if b.get("lit")}
    for sid, s in states.items():
        fmt = _display_format(s)
        fn = {"fid": "display:" + sid, "kind": "display", "label": s.get("label") or sid,
              "state_id": sid, "format": fmt,
              "group": "display" if (sid in choice_of or sid in lit_states or fmt in ("bool", "duration"))
                       else "more"}
        if sid in choice_of:
            fn["choice"] = choice_of[sid]   # show « CAM 1 », not « 0 »
        out.append(fn)
    # A position and a duration make a countdown: what an operator watches on a player.
    if (states.get("position") or {}).get("type") == "number" and (states.get("duration") or {}).get("type") == "number":
        out.append({"fid": "countdown:position", "kind": "display", "label": "Temps restant",
                    "label_key": "projects.fn_name_remaining", "state_id": "position",
                    "total_state": "duration", "format": "remaining", "group": "display"})

    # 5 bis. Pieces of UI the plugin EXPORTS itself (`ui.widgets`): the advanced case — a block
    #        too rich to derive (the player's transport: timecode, scrub bar, in/out, speeds).
    #        Contract: plugin.json `ui.widgets: [{id, label, label_key?}]`, and its control.js
    #        `MXLPlugins[type].widgets[id].mount(el, vmid, ctx)` → handle with `unmount()`.
    for wdg in (((manifest or {}).get("ui") or {}).get("widgets") or []):
        if isinstance(wdg, dict) and wdg.get("id"):
            fn = {"fid": "widget:" + str(wdg["id"]), "kind": "widget", "widget_id": str(wdg["id"]),
                  "label": wdg.get("label") or str(wdg["id"]), "group": "command"}
            if wdg.get("label_key"):
                fn["label_key"] = wdg["label_key"]
            out.append(fn)

    # 5 ter. VIDEO of each output the container publishes (`produces`, from its wiring): a WebRTC
    #        preview per output (mixer PGM and PVW side by side), through the dedicated monitor
    #        of app/monitor.py — created on demand, one encoder per watched output.
    for pr in entry.get("produces") or []:
        if (pr.get("essence") or pr.get("kind")) == "video" and pr.get("shm") and not pr.get("monitor"):
            out.append({"fid": "video:" + pr["shm"], "kind": "video", "shm": pr["shm"],
                        "label": (pr.get("label") or "").strip() or pr["shm"],
                        "label_key": "projects.fn_name_video", "group": "display"})

    # 6. IMAGES: read endpoints serving a picture.
    for ep in ((manifest or {}).get("control") or {}).get("read_endpoints") or []:
        if str(ep).lower().endswith((".jpg", ".jpeg", ".png")):
            stem = str(ep).strip("/").rsplit(".", 1)[0]
            fn = {"fid": "image:" + ep, "kind": "image", "label": stem, "endpoint": ep, "group": "display"}
            if stem in _IMAGE_NAMES:
                fn["label_key"] = _IMAGE_NAMES[stem]
            out.append(fn)
    return out
