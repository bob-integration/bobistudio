# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Inputs whose format is set by a VIDEO MODE (a DeckLink SDI output: `outputs[].mode`).

## The problem (2026-11-21)

The cabling gate compares a source with the consumer's top-level `width`/`height`/`fps`. A
DeckLink output has none: its format is the `mode` of ONE entry of its `outputs` list. A 1080i50
capture wired to an output set to 1080p50 was therefore accepted, and the output wove the two
fields into progressive frames, each sent twice — a combed picture at the wrong rate, and nothing
said so. The same blind spot hid a later change of either side (source switching, output mode
edited).

## What this module does

A list port that declares `"format_field": "<entry key>"` in its manifest gets its expected format
parsed from that entry (`1080i50`, `720p59.94`, `1920x1200p60`…; an unknown name gives None, and
nothing is claimed about it). Then, exactly as for a 2110 TX slot or a container input:
  · at cabling, `cabling._format_gate` refuses a measured mismatch (warns on a declared one);
  · in operation, `scan_once` (run by the tx_format_watch loop) compares each wired port with the
    source's real flow_def and raises a warning naming both formats, once per mismatch.
No conversion is inserted: converting would hide a wrong setting, and the choice (UDC, or fix the
output mode or the source) belongs to the operator.
"""

import json
import logging
import re

log = logging.getLogger(__name__)

# DeckLink display-mode names (IDeckLinkDisplayMode::GetName) and the usual labels.
_MODE_RE = re.compile(r"^(?:\d+K)?(?:(\d{3,4})x)?(\d{3,4})(p|i|PsF)(\d+(?:\.\d+)?)$", re.I)
_WIDTH_OF = {480: 720, 486: 720, 576: 720, 720: 1280, 1080: 1920, 2160: 3840, 4320: 7680}


def mode_format(label):
    """{width, height, scan, fps, field_order, bit_depth} of a video-mode label, or None.
    `fps` is the rate in the label (field rate for interlaced, as the profession writes it);
    `tx_maintenance.format_diff` normalises both sides to the frame rate."""
    m = _MODE_RE.match(str(label or "").strip())
    if not m:
        return None
    w, h = (int(m.group(1)) if m.group(1) else 0), int(m.group(2))
    w = w or _WIDTH_OF.get(h, 0)
    if not w:
        return None
    scan = "i" if m.group(3).lower() == "i" else "p"      # PsF carries progressive frames
    return {"width": w, "height": h, "scan": scan, "fps": float(m.group(4)),
            "field_order": "", "bit_depth": 0}


def _ports(to_type, hostname, params, kind="video"):
    from . import plugins as _pl
    try:
        w = _pl.derive_wiring(to_type, hostname or "", params or {})
    except Exception:
        return []
    return [x for x in (w.get("consumes") or [])
            if (x.get("essence") or "video") == kind and x.get("format_field") and x.get("from_list")]


def _entry(params, port):
    lst = (params or {}).get(port["from_list"]) or []
    i = port.get("index")
    return lst[i] if isinstance(i, int) and 0 <= i < len(lst) and isinstance(lst[i], dict) else {}


def expected(to_type, params, kind="video", to_slot=None, shm=None, hostname=""):
    """(format, port label) expected by the mode-driven port designated by `to_slot`, else by the
    port already fed with `shm`. (None, None) when the consumer has no such port."""
    from . import project_ports
    ports = _ports(to_type, hostname, params, kind)
    port = None
    if to_slot is not None:
        ws = project_ports.norm_slot(to_slot)
        port = next((p for p in ports if p.get("slot") == ws), None)
    if port is None and shm:
        port = next((p for p in ports if p.get("shm") == shm), None)
    if port is None:
        return None, None
    fmt = mode_format(_entry(params, port).get(port["format_field"]))
    return fmt, port.get("label") or str(port.get("slot"))


def label(f):
    """« 1920x1080i50 »: the rate as the profession writes it — field rate when interlaced, which
    is both what a mode name carries and a flow_def's grain rate (1 grain = 1 field)."""
    if not f:
        return "?"
    try:
        fps = float(f.get("fps") or f.get("fps_num") or 0)
        if f.get("fps_den") and f.get("fps_num"):
            fps = float(f["fps_num"]) / float(f["fps_den"])
    except (TypeError, ValueError):
        fps = 0.0
    return "%sx%s%s%s" % (f.get("width"), f.get("height"), f.get("scan") or "p",
                          ("%g" % round(fps, 2)) if fps else "?")


def mismatch(real, want):
    """Axes on which a measured source differs from a mode-driven port (tx_maintenance rules)."""
    from .tx_maintenance import format_diff, _norm_fps
    if not real or not want:
        return []
    w = dict(want)
    w["fps"] = _norm_fps(w.get("fps"), w.get("scan"))
    return format_diff(real, w)


# ── In operation ───────────────────────────────────────────────────────────────────────────
from .episodes import EtatEpisodes as _Episodes
_episodes = _Episodes("sdi_format")


def purger_episodes():
    try:
        from .database import db_get_containers
        alive = {str(c.get("vmid")) for c in (db_get_containers() or [])}
    except Exception:
        return
    _episodes.purger(lambda k: k.split("\x1f")[0] in alive)


def scan_once():
    """One pass over every mode-driven port that is wired. Returns the mismatches seen."""
    from .database import db_get_containers, db_add_alert
    from . import plugins as _pl
    from .routes.cabling import _flow_def_format
    from .tx_format_watch import _producer_of
    seen = []
    for c in db_get_containers() or []:
        try:
            dc = json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        t = dc.get("type")
        if not t or not _pl.is_plugin(t):
            continue
        params = dc.get("params") or {}
        for port in _ports(t, c.get("hostname"), params):
            shm = port.get("shm")
            if not shm:
                continue
            want = mode_format(_entry(params, port).get(port["format_field"]))
            src = _producer_of(shm) if want else None
            real = _flow_def_format(src, shm) if src else None
            if not real:
                continue                       # unknown on either side: nothing to claim
            key = (c["vmid"], port.get("slot"))
            axes = mismatch(real, want)
            if not axes:
                _episodes.retirer(key)
                continue
            sig = "%s|%s" % (label(real), label(want))
            if _episodes.get(key) == sig:
                continue
            _episodes.poser(key, sig)
            seen.append({"vmid": c["vmid"], "slot": port.get("slot"), "shm": shm, "axes": axes})
            db_add_alert("alert.sortie.format_inattendu", "warning", vmid=c["vmid"],
                         node_id=c.get("node_id"), kind="deploy",
                         params={"h": c.get("hostname") or "#%s" % c["vmid"],
                                 "label": port.get("label") or str(port.get("slot")), "shm": shm,
                                 "reel": label(real), "attendu": label(want)})
    # Written now, not on the 30 s debounce: nothing else may flush it before a restart.
    _episodes.flush(force=True)
    return seen
