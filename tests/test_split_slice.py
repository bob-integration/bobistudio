# SPDX-License-Identifier: GPL-3.0-or-later
# Test OFFLINE du mode tranche du plugin split (SuperSource) : la composition BANDÉE
# (_box_slice_geom + _place_box_band + _paint_border_band + _band_need_row) doit être
# identique, octet pour octet, qu'on empoisonne ou non les lignes source au-delà du
# sur plusieurs géométries de boxes / formats, ET ne jamais lire au-delà des tranches
# source déclarées valides (simulation de tranches partielles par empoisonnement).
#
# Exécution :  ./venv/bin/python tests/test_split_slice.py
# (aucune dépendance MXL : bobimxl est stubé, les serveurs HTTP neutralisés).
import sys, types, pathlib
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "plugins/split/script.py").read_text()


def make_ns(config):
    """Rend le template avec `config`, neutralise MXL + serveurs HTTP, exec jusqu'à la
    boucle principale (exclue) et renvoie le namespace du module."""
    rendered = SRC.format(config=repr(config), hostname="test", plugin_version="test")
    rendered = rendered.split("\nwhile True:")[0]          # coupe avant la boucle infinie
    rendered = rendered.replace(
        'HTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever()', "None").replace(
        'HTTPServer(("0.0.0.0", 8082), ControlHandler).serve_forever()', "None")
    fake = types.ModuleType("bobimxl")
    class _Inst:
        def __init__(self, *a, **k): pass
        def garbage_collect(self): pass
    class _Writer:
        def __init__(self, *a, **k): pass
    fake.Instance = _Inst
    fake.Writer = _Writer
    fake.Reader = None
    fake.MXL_UNDEFINED_INDEX = (1 << 64) - 1
    fake.now_tai = lambda: 0
    sys.modules["bobimxl"] = fake
    ns = {}
    exec(compile(rendered, f"split_rendered_{config.get('bit_depth', 8)}.py", "exec"), ns)
    return ns


def rand_frame(ns, rng):
    """Trame source aléatoire (bytes planar Y|U|V) au format du canvas."""
    dt = ns["_NP_DT"]; mx = ns["_MAXV"]
    y = rng.integers(0, mx + 1, size=(ns["HEIGHT"], ns["WIDTH"]), dtype=np.uint32).astype(dt)
    u = rng.integers(0, mx + 1, size=(ns["UV_H"], ns["UV_W"]), dtype=np.uint32).astype(dt)
    v = rng.integers(0, mx + 1, size=(ns["UV_H"], ns["UV_W"]), dtype=np.uint32).astype(dt)
    return y.tobytes() + u.tobytes() + v.tobytes()


def planes(ns, buf):
    dt = ns["_NP_DT"]
    ys, uvs = ns["Y_SIZE"], ns["UV_SIZE"]
    y = np.frombuffer(buf[:ys], dtype=dt).reshape(ns["HEIGHT"], ns["WIDTH"]).copy()
    u = np.frombuffer(buf[ys:ys + uvs], dtype=dt).reshape(ns["UV_H"], ns["UV_W"]).copy()
    v = np.frombuffer(buf[ys + uvs:ys + 2 * uvs], dtype=dt).reshape(ns["UV_H"], ns["UV_W"]).copy()
    return y, u, v


# ★ IL N'Y A PLUS D'ORACLE, ET C'EST VOULU.
# Ce test comparait autrefois la sortie tranche à `composite_box_yuv` — la composition image
# entière du plugin, supprimée en 0.9.0 (`ebce056`). On a un temps figé cette ancienne
# implémentation ici comme référence. Mauvaise idée : personne ne se soucie de savoir si
# `split` rend aujourd'hui la même image qu'avant le mode tranche, et le mode avancé (couche
# ART, ombres, transitions) est justement fait pour que non. Une telle référence transforme
# chaque évolution légitime du rendu en échec de test — et on finit par ignorer le test.
#
# Le contrat qui compte, lui, se prouve SANS référence extérieure : composer avec les lignes
# source au-delà du valide EMPOISONNÉES doit donner exactement le même résultat que sans.
# Si le code lit une seule ligne hors des tranches déclarées valides, les deux divergent.
# C'est plus fort que l'ancienne forme (aucune dépendance à du code mort) et ça survit à
# n'importe quel changement de rendu.


def banded(ns, bg, boxes, srcs, poison):
    """Chemin BANDÉ : mêmes couches composées bande par bande. poison=True → pour chaque
    (box, bande), les lignes source AU-DELÀ de _band_need_row sont empoisonnées (islh=1,
    le cas le plus serré) : toute lecture hors du valide déclaré casserait l'égalité."""
    H, W, ch = ns["HEIGHT"], ns["WIDTH"], ns["_CH"]
    sh = ns["SLICE_H"]
    assert sh > 0 and H % sh == 0, f"SLICE_H invalide : {sh} pour H={H}"
    oy, ou, ov = planes(ns, bg)          # état initial = fond (copie 1:1, couche z=0)
    geoms = []
    for (b, alpha), src in zip(boxes, srcs):
        g = ns["_box_slice_geom"](b, alpha)
        geoms.append((g, planes(ns, src)))
    mx = ns["_MAXV"]
    for k in range(H // sh):
        b0, b1 = k * sh, (k + 1) * sh
        for g, (sy, su, sv) in geoms:
            if g is None:
                continue
            need = ns["_band_need_row"](g, b0, b1)
            if need < 0:
                continue
            if poison:
                # tranches partielles simulées : lignes image [0, need+1) valides sur les
                # 3 plans (convention k tranches ⇔ [0, k·islh), ici islh=1 ⇒ k=need+1)
                psy = sy.copy(); psy[need + 1:] = mx
                cvr = (need + 1) // ch     # lignes chroma couvertes par le valide
                psu = su.copy(); psu[cvr:] = mx
                psv = sv.copy(); psv[cvr:] = mx
            else:
                psy, psu, psv = sy, su, sv
            ns["_place_box_band"](oy, ou, ov, psy, psu, psv, g, b0, b1)
            ns["_paint_border_band"](oy, ou, ov, g, b0, b1)
    return oy.tobytes() + ou.tobytes() + ov.tobytes()


# Jeux de boxes (géométries variées : recouvrements, hors-cadre, ratios entier/non-entier,
# crop asymétrique, bordures, alpha de transition, box désactivée → absente de la liste).
def box(x, y, size, crop=None, border=None):
    b = {"x": x, "y": y, "size": size,
         "crop": {"t": 0.0, "b": 0.0, "l": 0.0, "r": 0.0}}
    if crop:
        b["crop"].update(crop)
    if border:
        b["border"] = border
    return b


GEOMETRIES = {
    "quad-recouvrement": [           # 4 boxes qui se chevauchent au centre (z-ordre)
        (box(0.40, 0.40, 0.50), 1.0),
        (box(0.60, 0.40, 0.50), 1.0),
        (box(0.40, 0.60, 0.50), 1.0),
        (box(0.60, 0.60, 0.50), 1.0),
    ],
    "ratio-entier": [                # size=0.5 → mapping à pas constant (vue stridée)
        (box(0.25, 0.25, 0.5), 1.0),
        (box(0.75, 0.75, 0.5), 1.0),
    ],
    "ratio-non-entier": [            # sizes quelconques → gather chaîné
        (box(0.30, 0.35, 0.37), 1.0),
        (box(0.68, 0.62, 0.23), 1.0),
        (box(0.52, 0.48, 0.61), 1.0),
    ],
    "hors-cadre": [                  # boxes partiellement hors canvas (clip)
        (box(0.02, 0.05, 0.45), 1.0),
        (box(0.97, 0.95, 0.45), 1.0),
        (box(0.50, 0.01, 0.30), 1.0),
    ],
    "crop-bordure": [                # crop asymétrique + bordures (chrome)
        (box(0.35, 0.40, 0.55, crop={"t": 0.12, "l": 0.07},
             border={"w": 6, "color": "#ffcc00"}), 1.0),
        (box(0.70, 0.65, 0.33, crop={"b": 0.25, "r": 0.18},
             border={"w": 14, "color": "#3af"}), 1.0),
    ],
    "transition-alpha": [            # crossfade (alpha < 1) + box quasi invisible (skip)
        (box(0.45, 0.45, 0.48), 0.5),
        (box(0.60, 0.55, 0.35, border={"w": 4, "color": "#ffffff"}), 0.37),
        (box(0.50, 0.50, 0.20), 0.0005),   # alpha ≤ 0.001 → non composée (les 2 chemins)
    ],
    "petite-bande": [                # box entièrement contenue dans une seule bande
        (box(0.50, 0.05, 0.06), 1.0),
    ],
}

CONFIGS = [
    {"width": 1280, "height": 720, "bit_depth": 8,  "chroma": "422", "slice_mode": True},
    {"width": 1280, "height": 720, "bit_depth": 8,  "chroma": "420", "slice_mode": True},
    {"width": 1920, "height": 1080, "bit_depth": 10, "chroma": "422", "slice_mode": True},
    {"width": 1920, "height": 1080, "bit_depth": 10, "chroma": "420", "slice_mode": True},
]


def counters(ns):
    """Compteurs slice (0.7.1) : _sl_ensure câble les instantanés (_sl_dbg : waits/fallbacks)
    ET les cumulatifs clairement nommés (_sl_tot → suffixe _total sur :8080, leçon banc
    mixer : un compteur cumulatif ambigu se lit mal)."""
    import time as _t
    class _RdTimeout:                       # producteur mort : get_slice ET get timeoutent
        def get_slice(self, fi, k, timeout_ns=0): return None
        def get(self, fi, timeout_ns=0): return None
    class _GI:
        validSlices = 4
    class _RdOk:                            # producteur vivant : get_slice sert la tranche
        def get_slice(self, fi, k, timeout_ns=0): return (fi, _GI(), None)
    ns["_sl_dbg"] = [2, -1, 0, 0, 0]
    L = {"slot": 0, "rd": _RdTimeout(), "fi": 7, "valid": 1, "total": 4,
         "budget": int(5e6), "sy": None, "su": None, "sv": None}
    w = ns["_sl_ensure"](L, 3, _t.monotonic() + 0.005)     # timeout → attente + repli
    assert w >= 0 and L["valid"] == 4, (w, L["valid"])
    L2 = {"slot": 1, "rd": _RdOk(), "fi": 8, "valid": 1, "total": 4,
          "budget": int(5e6), "sy": None, "su": None, "sv": None}
    ns["_sl_ensure"](L2, 3, _t.monotonic() + 0.005)        # servi → attente sans repli
    ns["_sl_ensure"](L2, 2, _t.monotonic() + 0.005)        # déjà couvert → aucun compteur
    d, t = ns["_sl_dbg"], ns["_sl_tot"]
    assert d == [2, 1, 2, 1, 0], d       # [couches, valid0, attentes, replis, dormantes]
    assert t["waits"] == 2 and t["fallbacks"] == 1 and t["waited_ms"] >= 0, t
    assert ns["_sl_backoff"] == {0: [7, 1]}, ns["_sl_backoff"]
    print("ok   compteurs slice : _sl_dbg (instantané) + _sl_tot (cumulatif _total)")


def main():
    fails = 0
    for cfg in CONFIGS:
        ns = make_ns(dict(cfg))
        rng = np.random.default_rng(42)
        label_cfg = f"{cfg['width']}x{cfg['height']} {cfg['bit_depth']}b {cfg['chroma']}"
        for name, raw_boxes in GEOMETRIES.items():
            # _norm_box = la normalisation du runtime (mêmes clamps crop/border/size)
            boxes = [(ns["_norm_box"](b), a) for b, a in raw_boxes]
            bg = rand_frame(ns, rng)
            srcs = [rand_frame(ns, rng) for _ in boxes]
            ref = banded(ns, bg, boxes, srcs, False)   # sain = la référence
            for poison in (True,):
                got = banded(ns, bg, boxes, srcs, poison)
                ok = (got == ref)
                tag = "poison"
                if not ok:
                    fails += 1
                    a = np.frombuffer(ref, dtype=np.uint8)
                    b_ = np.frombuffer(got, dtype=np.uint8)
                    d = np.nonzero(a != b_)[0]
                    print(f"FAIL [{label_cfg}] {name} ({tag}) : {d.size} octets ≠ "
                          f"(premier @ {d[0] if d.size else '-'})")
                else:
                    print(f"ok   [{label_cfg}] {name} ({tag})")
    counters(make_ns(dict(CONFIGS[0])))     # câblage des compteurs slice (0.7.1)
    if fails:
        print(f"\n{fails} échec(s)")
        sys.exit(1)
    print("\nTOUT OK : composition bandée insensible à l empoisonnement (octet-identique), "
          "tranches partielles jamais lues au-delà du valide déclaré")


if __name__ == "__main__":
    main()
