# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Éditeur d'image de la base des médias : une RECETTE (cadre, zoom, rotation, fond, calques)
appliquée à une image source, rendue à pleine résolution, enregistrée comme NOUVELLE image
dérivée (filiation `derived_from`, relation « edit », recette dans `process_json`).

LA RECETTE (définition UNIQUE, partagée avec static/js/image_editor.js qui l'aperçoit en direct) —
tout est NORMALISÉ, donc indépendant de la résolution de rendu (aperçu 960 px ou pleine taille) :

  out    {w, h}                         format de sortie en pixels
  view   {x, y, k, rot, flipH, flipV}   x, y = point de la source (retournée) placé au CENTRE du
                                        cadre, en unités de LARGEUR SOURCE (x_px = x·sw, y_px = y·sw) ;
                                        k = largeur de la source dans le cadre / largeur du cadre
                                        (zoom = k·W/sw, en pixels de sortie par pixel source) ;
                                        rot = degrés, sens horaire ; flipH/V = miroirs de la SOURCE,
                                        appliqués avant tout (zones comprises : elles suivent)
  bg     {mode: color|blur|image|none, color, blur, dim, image}
                                        blur : la source (zones appliquées), orientée au quart de
                                        tour le plus proche, agrandie pour COUVRIR le cadre
                                        (× (1 + 4σ/min(W,H)) pour que le flou ne s'efface pas sur
                                        les bords), floutée σ = blur·u, assombrie de dim (0..1)
  adjust {brightness, contrast, saturation}  -100..100 ; facteur 1 + v/100, formules des filtres CSS
  frame  {border:{w,color}, radius, shadow:{on,dx,dy,blur,opacity,color}}
  zones  [{id, shape: rect|ellipse, x, y, w, h, a, mode: blur|pixel|fill, amount, color, soft}]
                                        posées sur la SOURCE non retournée : centre (x, y) et taille
                                        (w, h) en unités de largeur source, a = angle (degrés
                                        horaires) ; flou σ = amount·sw/3000, blocs = amount·sw/2500 ;
                                        soft (0..100) adoucit le bord : σ = soft/100·min(w,h)·sw/4
  texts  [{id, text, x, y, font, size, color, bold, align, stroke:{w,color},
           shadow:{on,dx,dy,blur,opacity,color}, box:{on,color,opacity,pad}}]
                                        x, y = ancre en fraction du cadre (x selon align, y = milieu
                                        du bloc) ; ligne i : base = haut + i·1,2·size + 0,6·size +
                                        0,35·size ; bold = contour de 0,03·size (la police n'a qu'une
                                        graisse) ; font = clé de app/fonts.py
  layers [{id, type: image|zone|text, visible}]   ordre de rendu, du bas vers le haut

Unité de sortie u = min(W, H) / 1080 : tout ce qui est en « pixels » dans la recette (bordure,
rayon, ombre, taille de police, flou du fond…) se multiplie par u. Géométrie : sortie = centre +
zoom·R(rot)·(source − (x, y)), R = rotation horaire en axes écran (y vers le bas).
"""
import io
import json
import logging
import math
import os
import threading
import time
import uuid

log = logging.getLogger(__name__)

MAX_OUT_PIXELS = 40_000_000
MAX_SIDE = 8192
MAX_SOURCE_BYTES = 400 * 1024 * 1024
SS = 3                      # sur-échantillonnage des masques de forme

BG_MODES = ("color", "blur", "image", "none")
FORMATS = {"hd": (1920, 1080), "uhd": (3840, 2160), "square": (1080, 1080), "vertical": (1080, 1920)}

_BUILTIN_FILES = {
    "dejavu-sans": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "dejavu-sans-bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "dejavu-serif": ["/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"],
    "dejavu-mono": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
    "liberation-sans": ["/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
    "liberation-sans-bold": ["/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    "liberation-mono": ["/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"],
    "inter": ["/usr/share/fonts/truetype/inter/Inter-Regular.otf", "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
              "/usr/share/fonts/truetype/inter/InterVariable.ttf"],
    "roboto": ["/usr/share/fonts/truetype/roboto/unhinted/Roboto-Regular.ttf",
               "/usr/share/fonts/truetype/roboto/hinted/Roboto-Regular.ttf"],
    "firacode": ["/usr/share/fonts/truetype/firacode/FiraCode-Regular.ttf"],
}
_DEFAULT_FONT = "dejavu-sans-bold"

OUTPUT_FICHE = "asset"          # réglage `media_edit_output` : une image retouchée devient une fiche à part (défaut)
OUTPUT_VERSION = "version"      # … ou une version de son original (media_versions, clé `edit`, `edit2`…)
VERSION_PREFIX = "@"            # id d'une retouche-version dans variants / `overwrite` : "@edit2"
VERSION_KEY = "edit"


def output_mode():
    """Réglage `media_edit_output` : OUTPUT_FICHE (défaut, comportement historique) ou OUTPUT_VERSION."""
    try:
        from . import settings as _st
        v = str(_st.get("media_edit_output") or "").strip().lower()
    except Exception:                                                          # noqa: BLE001
        v = ""
    return OUTPUT_VERSION if v == OUTPUT_VERSION else OUTPUT_FICHE


def next_version_key(aid):
    """Première clé libre de `edit`, `edit2`, `edit3`… parmi les versions du média."""
    from . import media_registry as mr
    taken = set(mr._version_keys(aid))
    k, n = VERSION_KEY, 1
    while k in taken:
        n += 1
        k = "%s%d" % (VERSION_KEY, n)
    return k


# ─── Polices ────────────────────────────────────────────────────────────────────────────────
def font_file(key):
    """Chemin du fichier de la police `key` (builtin ou `lib:…`), repli DejaVu Bold ; None si rien."""
    from . import fonts
    if fonts.is_library_key(key):
        row = fonts.resolve(key)
        if row and os.path.exists(fonts.font_path(row)):
            return fonts.font_path(row)
    for p in _BUILTIN_FILES.get(key, []):
        if os.path.exists(p):
            return p
    for p in _BUILTIN_FILES[_DEFAULT_FONT]:
        if os.path.exists(p):
            return p
    return None


def font_catalog():
    """Polices proposées à l'éditeur : celles de la bibliothèque + les polices d'image que
    l'orchestrateur sait lire (c'est lui qui rend à pleine résolution)."""
    from . import fonts
    out = []
    for f in fonts.catalog():
        if not f.get("builtin") or any(os.path.exists(p) for p in _BUILTIN_FILES.get(f["key"], [])):
            out.append({"key": f["key"], "name": f["name"], "builtin": bool(f.get("builtin"))})
    return out


# ─── Recette : validation ───────────────────────────────────────────────────────────────────
def _num(v, d, lo=None, hi=None):
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = d
    if v != v or v in (float("inf"), float("-inf")):
        v = d
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _col(v, d="#000000"):
    s = str(v or "").strip()
    if s.startswith("#") and len(s) in (4, 7) and all(c in "0123456789abcdefABCDEF" for c in s[1:]):
        if len(s) == 4:
            s = "#" + "".join(c * 2 for c in s[1:])
        return s.lower()
    return d


def _shadow(s, d):
    s = s if isinstance(s, dict) else {}
    return {"on": bool(s.get("on", d["on"])), "dx": _num(s.get("dx"), d["dx"], -500, 500),
            "dy": _num(s.get("dy"), d["dy"], -500, 500), "blur": _num(s.get("blur"), d["blur"], 0, 300),
            "opacity": _num(s.get("opacity"), d["opacity"], 0, 1), "color": _col(s.get("color"), d["color"])}


def normalize_recipe(r):
    """Recette brute (venue du navigateur) → recette complète, bornée. Lève ValueError."""
    if not isinstance(r, dict):
        raise ValueError("recette invalide")
    out = r.get("out") or {}
    W, H = int(_num(out.get("w"), 1920, 16, MAX_SIDE)), int(_num(out.get("h"), 1080, 16, MAX_SIDE))
    if W * H > MAX_OUT_PIXELS:
        raise ValueError("format de sortie trop grand")
    v = r.get("view") or {}
    view = {"x": _num(v.get("x"), 0.5, -50, 50), "y": _num(v.get("y"), 0.28, -50, 50),
            "k": _num(v.get("k"), 1.0, 0.001, 200), "rot": _num(v.get("rot"), 0, -3600, 3600),
            "flipH": bool(v.get("flipH")), "flipV": bool(v.get("flipV"))}
    b = r.get("bg") or {}
    bg = {"mode": b.get("mode") if b.get("mode") in BG_MODES else "color", "color": _col(b.get("color")),
          "blur": _num(b.get("blur"), 40, 0, 200), "dim": _num(b.get("dim"), 0, 0, 1),
          "image": str(b.get("image") or "")[:64]}
    a = r.get("adjust") or {}
    adjust = {k: _num(a.get(k), 0, -100, 100) for k in ("brightness", "contrast", "saturation")}
    f = r.get("frame") or {}
    bd = f.get("border") or {}
    frame = {"border": {"w": _num(bd.get("w"), 0, 0, 500), "color": _col(bd.get("color"), "#ffffff")},
             "radius": _num(f.get("radius"), 0, 0, 3000),
             "shadow": _shadow(f.get("shadow"), {"on": False, "dx": 0, "dy": 12, "blur": 24, "opacity": 0.5, "color": "#000000"})}
    zones, seen = [], set()
    for z in (r.get("zones") or [])[:64]:
        if not isinstance(z, dict):
            continue
        zid = str(z.get("id") or uuid.uuid4().hex[:8])[:32]
        if zid in seen:
            continue
        seen.add(zid)
        zones.append({"id": zid, "shape": "ellipse" if z.get("shape") == "ellipse" else "rect",
                      "x": _num(z.get("x"), 0.5, -50, 50), "y": _num(z.get("y"), 0.3, -50, 50),
                      "w": _num(z.get("w"), 0.2, 0.001, 50), "h": _num(z.get("h"), 0.1, 0.001, 50),
                      "a": _num(z.get("a"), 0, -3600, 3600),
                      "mode": z.get("mode") if z.get("mode") in ("blur", "pixel", "fill") else "blur",
                      "amount": _num(z.get("amount"), 40, 1, 100), "color": _col(z.get("color")),
                      "soft": _num(z.get("soft"), 0, 0, 100)})
    texts = []
    for t in (r.get("texts") or [])[:64]:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or uuid.uuid4().hex[:8])[:32]
        if tid in seen:
            continue
        seen.add(tid)
        st = t.get("stroke") or {}
        bx = t.get("box") or {}
        texts.append({"id": tid, "text": str(t.get("text") or "")[:2000], "x": _num(t.get("x"), 0.5, -2, 3),
                      "y": _num(t.get("y"), 0.5, -2, 3), "font": str(t.get("font") or _DEFAULT_FONT)[:40],
                      "size": _num(t.get("size"), 64, 4, 1500), "color": _col(t.get("color"), "#ffffff"),
                      "bold": bool(t.get("bold")), "align": t.get("align") if t.get("align") in ("left", "center", "right") else "center",
                      "stroke": {"w": _num(st.get("w"), 0, 0, 100), "color": _col(st.get("color"))},
                      "shadow": _shadow(t.get("shadow"), {"on": False, "dx": 3, "dy": 3, "blur": 6, "opacity": 0.7, "color": "#000000"}),
                      "box": {"on": bool(bx.get("on")), "color": _col(bx.get("color")),
                              "opacity": _num(bx.get("opacity"), 0.6, 0, 1), "pad": _num(bx.get("pad"), 16, 0, 500)}})
    ids = {"image": "image"}
    ids.update({z["id"]: "zone" for z in zones})
    ids.update({t["id"]: "text" for t in texts})
    layers, used = [], set()
    for ly in (r.get("layers") or []):
        if isinstance(ly, dict) and ly.get("id") in ids and ly["id"] not in used:
            used.add(ly["id"])
            layers.append({"id": ly["id"], "type": ids[ly["id"]], "visible": ly.get("visible") is not False})
    if "image" not in used:                        # la photo manquante se range EN BAS
        layers.insert(0, {"id": "image", "type": "image", "visible": True})
    for i, t in ids.items():                       # autre élément absent de la liste : en haut, visible
        if i not in used and i != "image":
            layers.append({"id": i, "type": t, "visible": True})
    return {"v": 1, "out": {"w": W, "h": H}, "view": view, "bg": bg, "adjust": adjust, "frame": frame,
            "zones": zones, "texts": texts, "layers": layers}


# ─── Rendu (Pillow) ─────────────────────────────────────────────────────────────────────────
def _rgb(c):
    c = _col(c)
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def outline(cx, cy, theta_deg, lw, lh, r, n=14):
    """Contour (liste de points) d'un rectangle lw×lh centré en (cx, cy), tourné de theta (horaire),
    à coins arrondis de rayon r. MÊME construction dans image_editor.js."""
    r = max(0.0, min(r, lw / 2, lh / 2))
    hw, hh = lw / 2, lh / 2
    pts = []
    if r <= 0.01:
        pts = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    else:
        for (ox, oy, a0) in ((hw - r, -hh + r, -90), (hw - r, hh - r, 0), (-hw + r, hh - r, 90), (-hw + r, -hh + r, 180)):
            for i in range(n + 1):
                a = math.radians(a0 + 90.0 * i / n)
                pts.append((ox + r * math.cos(a), oy + r * math.sin(a)))
    th = math.radians(theta_deg)
    c, s = math.cos(th), math.sin(th)
    return [(cx + x * c - y * s, cy + x * s + y * c) for x, y in pts]


def _poly_mask(size, pts):
    from PIL import Image, ImageDraw
    W, H = size
    m = Image.new("L", (W * SS, H * SS), 0)
    if pts:
        ImageDraw.Draw(m).polygon([(x * SS, y * SS) for x, y in pts], fill=255)
    return m.resize((W, H), Image.BOX)


def _apply_zones(src, recipe):
    """Zones floutées / pixelisées / aplat sur la SOURCE (RGBA), dans l'ordre des calques."""
    from PIL import Image, ImageFilter
    zs = {z["id"]: z for z in recipe["zones"]}
    sw, sh = src.size
    src = src.copy()
    for ly in recipe["layers"]:
        z = zs.get(ly["id"]) if ly["type"] == "zone" and ly["visible"] else None
        if not z:
            continue
        cx, cy = z["x"] * sw, z["y"] * sw
        zw, zh = z["w"] * sw, z["h"] * sw
        if z["shape"] == "ellipse":
            pts = [(zw / 2 * math.cos(2 * math.pi * i / 72), zh / 2 * math.sin(2 * math.pi * i / 72)) for i in range(72)]
            th = math.radians(z["a"])
            c, s = math.cos(th), math.sin(th)
            pts = [(cx + x * c - y * s, cy + x * s + y * c) for x, y in pts]
        else:
            pts = outline(cx, cy, z["a"], zw, zh, 0)
        feather = z["soft"] / 100.0 * min(zw, zh) / 4.0
        sigma = z["amount"] * sw / 3000.0
        margin = int(3 * max(feather, sigma) + 4)
        x0 = max(0, int(math.floor(min(p[0] for p in pts))) - margin)
        y0 = max(0, int(math.floor(min(p[1] for p in pts))) - margin)
        x1 = min(sw, int(math.ceil(max(p[0] for p in pts))) + margin)
        y1 = min(sh, int(math.ceil(max(p[1] for p in pts))) + margin)
        if x1 <= x0 or y1 <= y0:
            continue
        crop = src.crop((x0, y0, x1, y1))
        if z["mode"] == "blur":
            eff = crop.filter(ImageFilter.GaussianBlur(sigma))
        elif z["mode"] == "pixel":
            blk = max(2, int(round(z["amount"] * sw / 2500.0)))
            small = crop.resize((max(1, crop.width // blk), max(1, crop.height // blk)), Image.BOX)
            eff = small.resize(crop.size, Image.NEAREST)
        else:
            eff = Image.new("RGBA", crop.size, _rgb(z["color"]) + (255,))
        mask = _poly_mask(crop.size, [(x - x0, y - y0) for x, y in pts])
        if feather > 0.3:
            mask = mask.filter(ImageFilter.GaussianBlur(feather))
        src.paste(Image.composite(eff, crop, mask), (x0, y0))
    return src


def _adjust(img, adj):
    """Luminosité, contraste, saturation = formules des filtres CSS (aperçu navigateur identique)."""
    import numpy as np
    from PIL import Image
    if not any(adj.get(k) for k in ("brightness", "contrast", "saturation")):
        return img
    a = np.asarray(img.convert("RGBA"), dtype=np.float32) / 255.0
    rgb, al = a[..., :3], a[..., 3:]
    if adj["brightness"]:
        rgb = np.clip(rgb * (1 + adj["brightness"] / 100.0), 0, 1)
    if adj["contrast"]:
        c = 1 + adj["contrast"] / 100.0
        rgb = np.clip(rgb * c + (0.5 - 0.5 * c), 0, 1)
    if adj["saturation"]:
        s = 1 + adj["saturation"] / 100.0
        m = np.array([[0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s],
                      [0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s],
                      [0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s]], dtype=np.float32)
        rgb = np.clip(rgb @ m.T, 0, 1)
    return Image.fromarray((np.concatenate([rgb, al], axis=2) * 255 + 0.5).astype(np.uint8), "RGBA")


def _flip(img, h, v):
    from PIL import Image
    if h:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if v:
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    return img


def geometry(recipe, sw, sh):
    """(zoom, centre sortie, P0 = centre de la source dans la sortie) — la définition unique."""
    W, H = recipe["out"]["w"], recipe["out"]["h"]
    v = recipe["view"]
    z = v["k"] * W / sw
    th = math.radians(v["rot"])
    c, s = math.cos(th), math.sin(th)
    dx, dy = sw / 2 - v["x"] * sw, sh / 2 - v["y"] * sw
    p0 = (W / 2 + z * (c * dx - s * dy), H / 2 + z * (s * dx + c * dy))
    return z, (W / 2, H / 2), p0


def _background(recipe, zoned, bg_img):
    from PIL import Image, ImageFilter
    W, H = recipe["out"]["w"], recipe["out"]["h"]
    bg = recipe["bg"]
    u = min(W, H) / 1080.0
    if bg["mode"] == "none":
        return Image.new("RGBA", (W, H), (0, 0, 0, 0))
    base = Image.new("RGBA", (W, H), _rgb(bg["color"]) + (255,))
    if bg["mode"] == "color":
        return base
    if bg["mode"] == "blur":
        im = _flip(zoned, recipe["view"]["flipH"], recipe["view"]["flipV"])
        q = int(round(recipe["view"]["rot"] / 90.0)) % 4
        if q:
            im = im.transpose({1: Image.ROTATE_270, 2: Image.ROTATE_180, 3: Image.ROTATE_90}[q])
    else:
        if bg_img is None:
            return base
        im = bg_img
    sigma = bg["blur"] * u
    cover = max(W / im.width, H / im.height) * (1 + 4 * sigma / min(W, H) if bg["mode"] == "blur" else 1)
    nw, nh = max(1, round(im.width * cover)), max(1, round(im.height * cover))
    if cover < 0.5:                                        # grande réduction : flou d'abord n'a pas de sens, BOX avant
        im = im.reduce(max(1, int(1 / cover / 1.0)))
    im = im.resize((nw, nh), Image.LANCZOS if cover < 1 else Image.BICUBIC)
    left, top = (nw - W) // 2, (nh - H) // 2
    im = im.crop((left, top, left + W, top + H))
    if sigma > 0:
        im = im.filter(ImageFilter.GaussianBlur(sigma))
    if bg["dim"] > 0:
        im = Image.blend(im, Image.new("RGBA", im.size, (0, 0, 0, 255)), bg["dim"])
    base.alpha_composite(im.convert("RGBA"))
    return base


def _draw_image_layer(canvas, recipe, zoned):
    from PIL import Image, ImageFilter
    W, H = canvas.size
    u = min(W, H) / 1080.0
    v = recipe["view"]
    sw, sh = zoned.size
    img = _flip(zoned, v["flipH"], v["flipV"])
    z, ctr, p0 = geometry(recipe, sw, sh)
    th = math.radians(v["rot"])
    c, s = math.cos(th), math.sin(th)
    # Pré-réduction : transform() n'a pas de filtre passe-bas, une forte réduction crénèlerait.
    kk = 1.0
    if z < 0.75:
        kk = max(z, 1.0 / max(sw, sh))
        img = img.resize((max(1, round(sw * kk)), max(1, round(sh * kk))), Image.LANCZOS)
    ez = z / (img.width / sw)                        # zoom effectif sur l'image réduite
    cxp, cyp = v["x"] * sw * (img.width / sw), v["y"] * sw * (img.width / sw)
    a, b, d, e = c / ez, s / ez, -s / ez, c / ez
    c0 = cxp - (a * W / 2 + b * H / 2)
    f0 = cyp - (d * W / 2 + e * H / 2)
    photo = img.transform((W, H), Image.AFFINE, (a, b, c0, d, e, f0), Image.BICUBIC)
    photo = _adjust(photo, recipe["adjust"])
    lw, lh = sw * z, sh * z
    fr = recipe["frame"]
    pts = outline(p0[0], p0[1], v["rot"], lw, lh, fr["radius"] * u)
    mask = _poly_mask((W, H), pts)
    sh_ = fr["shadow"]
    if sh_["on"] and sh_["opacity"] > 0:
        sm = _poly_mask((W, H), [(x + sh_["dx"] * u, y + sh_["dy"] * u) for x, y in pts])
        if sh_["blur"] > 0:
            sm = sm.filter(ImageFilter.GaussianBlur(sh_["blur"] * u))
        sm = sm.point(lambda p: int(p * sh_["opacity"] + 0.5))
        canvas.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (W, H), _rgb(sh_["color"])).split(), sm)))
    al = photo.getchannel("A")
    from PIL import ImageChops
    photo.putalpha(ImageChops.multiply(al, mask))
    canvas.alpha_composite(photo)
    bw = fr["border"]["w"] * u
    if bw > 0.2:
        inner = _poly_mask((W, H), outline(p0[0], p0[1], v["rot"], max(1, lw - 2 * bw), max(1, lh - 2 * bw),
                                           max(0.0, fr["radius"] * u - bw)))
        ring = ImageChops.subtract(mask, inner)
        canvas.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (W, H), _rgb(fr["border"]["color"])).split(), ring)))


def _draw_text(canvas, t):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    W, H = canvas.size
    u = min(W, H) / 1080.0
    lines = (t["text"] or "").split("\n")
    if not any(l.strip() for l in lines):
        return
    sz = max(2.0, t["size"] * u)
    path = font_file(t["font"])
    font = ImageFont.truetype(path, sz) if path else ImageFont.load_default()
    lh = 1.2 * sz
    widths = [font.getlength(l) for l in lines]
    bw = max(widths)
    n = len(lines)
    ax, top = t["x"] * W, t["y"] * H - n * lh / 2
    left = ax if t["align"] == "left" else (ax - bw / 2 if t["align"] == "center" else ax - bw)
    outw = t["stroke"]["w"] * u + (0.03 * sz if t["bold"] else 0)
    boldw = 0.03 * sz if t["bold"] else 0
    bx = t["box"]
    if bx["on"] and bx["opacity"] > 0:
        pad = bx["pad"] * u
        lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(lay).rectangle([left - pad, top - pad, left + bw + pad, top + n * lh + pad],
                                      fill=_rgb(bx["color"]) + (int(255 * bx["opacity"] + 0.5),))
        canvas.alpha_composite(lay)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)
    for i, l in enumerate(lines):
        x = left + (0 if t["align"] == "left" else (bw - widths[i]) / 2 if t["align"] == "center" else bw - widths[i])
        base = top + i * lh + 0.5 * lh + 0.35 * sz
        if t["stroke"]["w"] > 0:
            dr.text((x, base), l, font=font, anchor="ls", fill=_rgb(t["stroke"]["color"]) + (255,),
                    stroke_width=outw, stroke_fill=_rgb(t["stroke"]["color"]) + (255,))
        dr.text((x, base), l, font=font, anchor="ls", fill=_rgb(t["color"]) + (255,),
                stroke_width=boldw, stroke_fill=_rgb(t["color"]) + (255,))
    sh_ = t["shadow"]
    if sh_["on"] and sh_["opacity"] > 0:
        a = layer.getchannel("A")
        if sh_["blur"] > 0:
            a = a.filter(ImageFilter.GaussianBlur(sh_["blur"] * u))
        a = a.point(lambda p: int(p * sh_["opacity"] + 0.5))
        sl = Image.merge("RGBA", (*Image.new("RGB", (W, H), _rgb(sh_["color"])).split(), a))
        canvas.alpha_composite(_shift(sl, sh_["dx"] * u, sh_["dy"] * u))
    canvas.alpha_composite(layer)


def _shift(img, dx, dy):
    from PIL import Image
    return img.transform(img.size, Image.AFFINE, (1, 0, -dx, 0, 1, -dy), Image.BILINEAR)


def render(recipe, source, bg_image=None):
    """Recette normalisée + image source (PIL, tout mode) → image RGBA W×H."""
    recipe = normalize_recipe(recipe)
    src = source.convert("RGBA")
    zoned = _apply_zones(src, recipe)
    canvas = _background(recipe, zoned, bg_image.convert("RGBA") if bg_image is not None else None)
    texts = {t["id"]: t for t in recipe["texts"]}
    for ly in recipe["layers"]:
        if not ly["visible"]:
            continue
        if ly["type"] == "image":
            _draw_image_layer(canvas, recipe, zoned)
        elif ly["type"] == "text" and ly["id"] in texts:
            _draw_text(canvas, texts[ly["id"]])
    return canvas


# ─── Persistance : filiation, recette, tâche de fond ────────────────────────────────────────
_jobs = {}
_jobs_lock = threading.Lock()


def job(jid):
    with _jobs_lock:
        return dict(_jobs.get(jid) or {}) or None


def _job_set(jid, **kw):
    with _jobs_lock:
        _jobs.setdefault(jid, {}).update(kw)
        if len(_jobs) > 50:
            for k in sorted(_jobs, key=lambda k: _jobs[k].get("t0", 0))[:20]:
                _jobs.pop(k, None)


def resolve_original(aid):
    """(asset à éditer, asset d'édition ou None). Une image déjà éditée rouvre l'ORIGINALE."""
    from . import media_registry as mr
    a = mr.get_asset(aid)
    if not a:
        raise ValueError("média inconnu")
    p = a.get("process") or {}
    if a.get("relation") == "edit" and a.get("derived_from") and (p.get("recipe") is not None):
        orig = mr.get_asset(a["derived_from"])
        if not orig:
            raise ValueError("l'original de cette édition n'est plus dans la base")
        return orig, a
    return a, None


def variants(oid):
    """Éditions existantes d'une image originale (recette comprise)."""
    from . import media_registry as mr
    out = []
    for r in mr.db().execute("SELECT id, name, created_at, process_json FROM media_assets WHERE derived_from=? AND relation='edit' ORDER BY created_at", (oid,)):
        p = mr._j(r["process_json"], {}) or {}
        out.append({"id": r["id"], "name": r["name"], "created_at": r["created_at"], "recipe": p.get("recipe")})
    # Retouches rangées comme VERSION de l'original (réglage `media_edit_output=version`) : id `@<clé>`.
    for r in mr.db().execute("SELECT key, label, created_at, process_json FROM media_versions WHERE asset_id=? AND preset='edit' "
                             "ORDER BY created_at, key", (oid,)):
        p = mr._j(r["process_json"], {}) or {}
        if p.get("recipe") is not None:
            out.append({"id": VERSION_PREFIX + r["key"], "name": r["label"] or r["key"], "created_at": r["created_at"],
                        "recipe": p.get("recipe")})
    return out


def info(aid):
    orig, edit = resolve_original(aid)
    tech = orig.get("tech") or {}
    t = tech.get("video") or {}
    if tech.get("kind") != "image":
        raise ValueError("l'éditeur ne traite que les images fixes")
    vs = variants(orig["id"])
    cur = next((v for v in vs if edit and v["id"] == edit["id"]), None)
    return {"original": {"id": orig["id"], "name": orig["name"], "w": t.get("width"), "h": t.get("height")},
            "editing": ({"id": cur["id"], "name": cur["name"]} if cur else None),
            "recipe": cur["recipe"] if cur else None,
            "variants": [{"id": v["id"], "name": v["name"], "created_at": v["created_at"]} for v in vs],
            "formats": {k: {"w": w, "h": h} for k, (w, h) in FORMATS.items()}}


def _read_image(aid, what="image"):
    from PIL import Image, ImageOps
    from . import media_registry as mr
    nid, path = mr._present_copy(aid)
    if nid is None:
        raise RuntimeError("%s : aucune copie présente sur un nœud" % what)
    row = mr.db().execute("SELECT size FROM media_files WHERE asset_id=? AND node_id=? AND path=?", (aid, nid, path)).fetchone()
    size = int(row["size"] or 0)
    if size > MAX_SOURCE_BYTES:
        raise RuntimeError("%s : fichier trop gros (%d Mo)" % (what, size >> 20))
    data = b"".join(mr._read_node_file(mr._node(nid), path, size))
    Image.MAX_IMAGE_PIXELS = 200_000_000
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    return im.convert("RGBA"), nid, path


def _png(img):
    b = io.BytesIO()
    img.save(b, "PNG", compress_level=4)
    b.seek(0)
    return b


def _free_rel(nid, rel_dir, stem, new_id, oid=None):
    """`<stem>_edit<N>__<code>.png` (code du NOUVEAU média, règle de app/media_naming.py) : N = rang
    de la variante (nom du média lisible : « photo_edit3.png »), le code rend le nom unique ; libre
    dans la base ET sur le disque du nœud (sinon code à 8 caractères)."""
    import shlex
    from . import media_registry as mr, media_naming, node_driver
    node = mr._node(nid)
    n = len(variants(oid)) + 1 if oid else 1

    def taken(rel):
        r = mr.db().execute("SELECT asset_id FROM media_files WHERE node_id=? AND share_id=0 AND path=?", (nid, rel)).fetchone()
        if r:
            return r["asset_id"] or ""
        rc, _o, _e = node_driver.host_exec(node, "test -e %s" % shlex.quote(mr._mount(node).rstrip("/") + "/" + rel), timeout=20)
        return "" if rc == 0 else None
    return media_naming.pick_name("%s%s_edit%d.png" % (rel_dir, media_naming.strip_code(stem), n), new_id, taken)


def save(jid, aid, recipe, overwrite=None, as_version=False, link_projects=None):
    """Tâche de fond : rendu pleine résolution, dépôt à côté de l'original, import, filiation.
    `overwrite` = id d'une édition EXISTANTE de cet original (confirmation faite par l'appelant) ;
    sinon une nouvelle variante. L'original n'est jamais touché.
    `as_version` : la sortie est rangée comme VERSION de l'original (clé `edit`, `edit2`…) au lieu
    d'une fiche à part ; `overwrite` vaut alors `@<clé>`. `link_projects` : projets où ranger une NOUVELLE
    fiche (None = ceux de l'originale) — l'éditeur d'un média seulement PARTAGÉ avec lui n'est pas dans
    les projets de l'originale."""
    from . import media_registry as mr
    _job_set(jid, state="running", step="load", pct=5, t0=time.time())
    try:
        orig, _e = resolve_original(aid)
        recipe = normalize_recipe(recipe)
        edit = None
        vkey = None
        if overwrite and str(overwrite).startswith(VERSION_PREFIX):
            vkey = str(overwrite)[1:]
            vrow = mr.db().execute("SELECT 1 FROM media_versions WHERE asset_id=? AND key=? AND preset='edit'",
                                   (orig["id"], vkey)).fetchone()
            if not vrow:
                raise ValueError("la retouche à écraser n'appartient pas à cette image")
            overwrite = None
        if overwrite:
            edit = mr.get_asset(overwrite)
            if not edit or edit.get("derived_from") != orig["id"] or edit.get("relation") != "edit":
                raise ValueError("l'édition à écraser n'appartient pas à cette image")
        src, nid, path = _read_image(orig["id"], "original")
        _job_set(jid, step="bg", pct=25)
        bg_img = None
        if recipe["bg"]["mode"] == "image" and recipe["bg"]["image"]:
            bg_img = _read_image(recipe["bg"]["image"], "image de fond")[0]
        _job_set(jid, step="render", pct=40)
        img = render(recipe, src, bg_img)
        _job_set(jid, step="upload", pct=75)
        buf = _png(img)
        size = buf.getbuffer().nbytes
        now = int(time.time())
        process = {"type": "image_edit", "preset": "edit", "version": 1, "recipe": recipe,
                   "source": {"id": orig["id"], "name": orig["name"], "fingerprint": orig.get("fingerprint")}}
        tech = {"container": "png_pipe", "duration": None, "audio": [], "timecode": None, "kind": "image",
                "video": {"codec": "png", "width": img.width, "height": img.height, "fps": 25.0, "scan": "",
                          "pix_fmt": "rgba", "primaries": None, "transfer": None, "matrix": "gbr"}}
        node = mr._node(nid)
        c = mr.db()
        if as_version or vkey:
            from . import media_naming
            key = vkey or next_version_key(orig["id"])
            oid = orig["id"]
            process["version"] = key
            process["version_label"] = key
            old = c.execute("SELECT node_id, path FROM media_files WHERE asset_id=? AND version=? AND kind='main' AND share_id=0",
                            (oid, key)).fetchone() if vkey else None
            if old:
                enid, rel = old["node_id"], old["path"]
                node = mr._node(enid)
                mr.delete_copy(oid, enid, rel)
            else:
                enid = nid
                rel = media_naming.version_file_name(path, oid, key, ".png")
            mr._put_on_node(node, rel, buf, size, now)
            c.execute("""INSERT OR REPLACE INTO media_files(node_id, share_id, path, size, mtime, fingerprint, tech_json, sidecar_id,
                            asset_id, kind, state, growing, seen_at, derived_from, process_json, origin, version)
                         VALUES (?,0,?,?,?,NULL,?,NULL,?,'main','present',0,?,NULL,?,'edit',?)""",
                      (enid, rel, size, now, json.dumps(tech), oid, mr._now(), json.dumps(process, ensure_ascii=False), key))
            mr._ensure_version(oid, key, process, origin="edit", size=size, tech=tech, replace=True)
            c.commit()
            _job_set(jid, step="stamp", pct=90)
            mr._stamp_pending(enid)
            c.commit()
            _job_set(jid, state="done", step="done", pct=100, result={"id": oid, "version": key, "path": rel, "node_id": enid,
                                                                       "overwritten": bool(vkey), "w": img.width, "h": img.height})
            return
        if edit:
            cp = next((f for f in edit["copies"] if f["state"] == "present" and not f["share_id"]), None)
            if not cp:
                raise RuntimeError("l'édition à écraser n'a plus de copie présente")
            enid, rel, aid2 = cp["node_id"], cp["path"], edit["id"]
            node = mr._node(enid)
            mr.delete_copy(aid2, enid, rel)
            mr._put_on_node(node, rel, buf, size, now)
            c.execute("""INSERT OR REPLACE INTO media_files(node_id, share_id, path, size, mtime, fingerprint, tech_json, sidecar_id,
                            asset_id, kind, state, growing, seen_at, derived_from, process_json, origin)
                         VALUES (?,0,?,?,?,NULL,?,NULL,?,'main','present',0,?,?,?,'edit')""",
                      (enid, rel, size, now, json.dumps(tech), aid2, mr._now(), orig["id"], json.dumps(process, ensure_ascii=False)))
            c.execute("UPDATE media_assets SET size=?, tech_json=?, process_json=?, fingerprint=NULL, updated_at=? WHERE id=?",
                      (size, json.dumps(tech), json.dumps(process, ensure_ascii=False), mr._now(), aid2))
            c.commit()
            _job_set(jid, step="thumb", pct=90)
            mr._stamp_pending(enid, thumbs=[(aid2, rel, 1.0)])
            new_id = aid2
        else:
            d, base = os.path.split(path)
            # Identité de la variante AVANT son nom de fichier : il porte son code.
            want_id = str(uuid.uuid4())
            rel = _free_rel(nid, (d + "/") if d else "", os.path.splitext(base)[0], want_id, orig["id"])
            mr._put_on_node(node, rel, buf, size, now)
            # `sidecar_id` = l'identité voulue : import_files la reprend (au lieu d'en tirer une autre).
            c.execute("""INSERT OR REPLACE INTO media_files(node_id, share_id, path, size, mtime, fingerprint, tech_json, sidecar_id,
                            asset_id, kind, state, growing, seen_at, derived_from, process_json, origin)
                         VALUES (?,0,?,?,?,NULL,?,?,NULL,'main','present',0,?,?,?,'edit')""",
                      (nid, rel, size, now, json.dumps(tech), want_id, mr._now(), orig["id"], json.dumps(process, ensure_ascii=False)))
            c.commit()
            _job_set(jid, step="import", pct=88)
            new_id = mr.import_files(nid, [rel], origin="edit")[0]
            from . import media_naming
            c.execute("UPDATE media_assets SET relation='edit', derived_from=?, process_json=?, name=? WHERE id=?",
                      (orig["id"], json.dumps(process, ensure_ascii=False), media_naming.media_name(rel, new_id), new_id))
            # Le sidecar n'existe pas encore sur le nœud : le faire écrire (identité) au prochain tampon.
            c.execute("UPDATE media_files SET sidecar_id=NULL WHERE node_id=? AND share_id=0 AND path=?", (nid, rel))
            c.commit()
            mr._stamp_pending(nid)
            c.commit()
            for pid in (orig.get("projects") or []) if link_projects is None else link_projects:
                mr.link_project(new_id, pid)              # projets de l'originale (ou de l'éditeur si elle lui est partagée)
        _job_set(jid, state="done", step="done", pct=100, result={"id": new_id, "path": rel, "node_id": nid if not edit else enid,
                                                                   "overwritten": bool(edit), "w": img.width, "h": img.height})
    except Exception as e:                                                   # noqa: BLE001
        log.warning("éditeur d'image : %s", e, exc_info=True)
        _job_set(jid, state="failed", step="failed", error=str(e))


def start_save(aid, recipe, overwrite=None, as_version=False, link_projects=None):
    jid = uuid.uuid4().hex[:12]
    _job_set(jid, state="queued", step="queued", pct=0, t0=time.time())
    threading.Thread(target=save, args=(jid, aid, recipe, overwrite, as_version, link_projects), daemon=True, name="media-edit").start()
    return jid
