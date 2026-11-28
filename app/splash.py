# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""STARTUP SPLASH (2026-11-24, asked by Cyril): what an output shows while its plugin opens the
inputs, so the first composed frame is a clean one.

One setting for the whole cluster (Settings → Branding, `splash_*`): background, logo size, text,
text size and colour. No wait to set: an absent input holds the splash one frame (2026-11-25). Only the text differs between
containers when it is the container's name — its emplacement label when it serves one, else its
hostname. Rendered once at start by the container (`bobimxl.render_splash`, ~30 ms at 1080p).

Sent only to plugins whose manifest declares `startup_splash: true`, and never to a fabric shard
(`bobi-fab-*`): inside a wall on air its zone would show the splash, where a frozen image is less
visible. Sent at render time like the brand logo, never persisted in `deploy_config`. A change
applies to containers deployed or redeployed after it.
"""

import base64
import glob
import logging
import os
import re

log = logging.getLogger(__name__)

DEFAULTS = {
    "splash_enabled": True,
    "splash_bg": "#000000",
    "splash_logo_pct": 25,        # logo height, % of the frame height; 0 = no logo
    "splash_text": "name",        # none | name | custom
    "splash_text_custom": "",
    "splash_text_pct": 5,         # text height, % of the frame height
    "splash_text_color": "#FFFFFF",
}
TEXT_MODES = ("none", "name", "custom")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_RANGES = {"splash_logo_pct": (0, 90), "splash_text_pct": (1, 30)}


def validate(data):
    """Normalises the `splash_*` keys of a settings POST in place; returns an error or None."""
    for k in [k for k in data if k.startswith("splash_")]:
        v = data[k]
        if k not in DEFAULTS:
            return "%s: unknown" % k
        if k == "splash_enabled":
            data[k] = bool(v) and str(v).strip().lower() not in ("0", "false", "no", "off")
        elif k in ("splash_bg", "splash_text_color"):
            if not isinstance(v, str) or not _HEX.match(v.strip()):
                return "%s: #RRGGBB" % k
            data[k] = v.strip().upper()
        elif k == "splash_text":
            if v not in TEXT_MODES:
                return "%s: %s" % (k, ", ".join(TEXT_MODES))
        elif k == "splash_text_custom":
            data[k] = str(v or "").strip()[:120]
        else:
            lo, hi = _RANGES[k]
            try:
                n = float(v)
            except (TypeError, ValueError):
                return "%s: %s-%s" % (k, lo, hi)
            if not lo <= n <= hi:
                return "%s: %s-%s" % (k, lo, hi)
            data[k] = int(n)
    return None


def current():
    """The effective settings (stored value, else default)."""
    from . import settings as st
    out = {}
    for k, d in DEFAULTS.items():
        v = st.get(k)
        out[k] = d if (v is None or (v == "" and k != "splash_text_custom")) else v
    return out


def _logo():
    from . import config
    for fp in glob.glob(os.path.join(config.UPLOADS_DIR, "brand-logo.*")):
        try:
            with open(fp, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
        except OSError as e:
            log.warning("splash: brand logo unreadable: %s", e)
    return ""


def _name(cont):
    from .database import db_role_for_instance
    try:
        role = db_role_for_instance((cont or {}).get("instance_uuid"))
    except Exception:                                          # noqa: BLE001
        role = None
    return ((role or {}).get("label") or (cont or {}).get("hostname") or "").strip()


def for_container(cont, type_):
    """`params["splash"]` for this container, or None when its plugin has no splash."""
    from . import plugins
    if not ((plugins.get(type_) or {}).get("startup_splash")):
        return None
    if str((cont or {}).get("hostname") or "").startswith("bobi-fab-"):
        return None
    s = current()
    if not s["splash_enabled"]:
        return {"enabled": False}
    text = {"name": _name(cont), "custom": s["splash_text_custom"]}.get(s["splash_text"], "")
    return {"enabled": True, "bg": s["splash_bg"],
            "logo_b64": _logo() if float(s["splash_logo_pct"]) > 0 else "",
            "logo_pct": s["splash_logo_pct"], "text": text, "text_pct": s["splash_text_pct"],
            "text_color": s["splash_text_color"]}
