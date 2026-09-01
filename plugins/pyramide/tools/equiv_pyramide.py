"""Équivalence bit-exact du câblage mvk dans plugins/pyramide/script.py : chemin numpy (repli)
≡ chemin kernel C fusionné (mvk_place_into), sur les DEUX voies de génération de proxy :
  - whole-frame (_mvk_place_plane + resize_plane)
  - mode tranche (_mvk_band + _emit_band, fast-path strided ET gather, bandes partielles)
Méthode de la validation multiview (meters_equiv.py) : rendre le script du plugin, extraire par
AST les défs pures nécessaires (exec stmt par stmt, garde-fous contre serveurs/threads/Reader/
Writer/Instance/boucles), puis comparer les deux chemins sur une grille de configs (ratios
entiers, non entiers, 8/10 bits, bandes partielles). Exit 0 = tout identique (Δ=0)."""
import ast, os, sys
import numpy as np

sys.path.insert(0, "/opt/bobistudio")
sys.path.insert(0, "/opt/bobistudio/script_templates")   # import bobimxl (gate _MVK du script)
os.environ["BOBI_MVK_LIB"] = "/tmp/claude-0/-opt-bobistudio/61bf488d-b0a5-4fb8-a840-43d7ca28b39e/scratchpad/libbobi_mvk.so"

from app import plugins

NEEDED = {"_chroma_factors", "_make_layout", "_proxy_dims", "resize_plane",
          "_mvk_place_plane", "_emit_band", "_mvk_band"}
BANNED = ("HTTPServer", "serve_forever", ".start()", "Instance(", "Writer(", "Reader(",
          "socket.", "while True", "signal.signal", "threading.Thread")


def load_ns(cfg):
    src = plugins.render_script("pyramide", cfg, "equivtest")
    tree = ast.parse(src)
    ns = {"__name__": "pyr_equiv"}
    for node in tree.body:
        seg = ast.unparse(node)
        if any(b in seg for b in BANNED):
            continue
        try:
            exec(compile(ast.Module([node], []), "<pyr>", "exec"), ns)
        except Exception as e:
            print("skip:", type(node).__name__, "->", e)
        if NEEDED <= set(ns):
            break
    missing = NEEDED - set(ns)
    if missing:
        raise SystemExit("défs manquantes après extraction : %s" % missing)
    if not ns.get("_MVK"):
        raise SystemExit("_MVK est False — lib mvk non chargée, test non probant")
    return ns


def _mk_lyt(ns, w, h, chroma="420", bit_depth=8):
    return ns["_make_layout"](w, h, chroma=chroma, bit_depth=bit_depth)


def _rand_planes(rng, lyt):
    dt = lyt["np_dt"]
    hi = (1 << lyt["bit_depth"])
    y0 = rng.integers(0, hi, size=(lyt["height"], lyt["width"]), dtype=np.int64).astype(dt)
    u0 = rng.integers(0, hi, size=(lyt["uv_h"], lyt["uv_w"]), dtype=np.int64).astype(dt)
    v0 = rng.integers(0, hi, size=(lyt["uv_h"], lyt["uv_w"]), dtype=np.int64).astype(dt)
    return y0, u0, v0


def check_whole_frame(ns, label, w, h, level, chroma="420", bit_depth=8):
    lyt = _mk_lyt(ns, w, h, chroma, bit_depth)
    cw, ch = lyt["cw"], lyt["ch"]
    pw, ph = ns["_proxy_dims"](w, h, level, cw, ch)
    uw, uh = pw // cw, ph // ch
    rng = np.random.default_rng(hash((label, w, h, level)) & 0xffffffff)
    y0, u0, v0 = _rand_planes(rng, lyt)

    dt = lyt["np_dt"]
    ref_y = np.zeros((ph, pw), dtype=dt); ref_u = np.zeros((uh, uw), dtype=dt); ref_v = np.zeros((uh, uw), dtype=dt)
    ref_y[...] = ns["resize_plane"](y0, ph, pw)
    ref_u[...] = ns["resize_plane"](u0, uh, uw)
    ref_v[...] = ns["resize_plane"](v0, uh, uw)

    got_y = np.zeros((ph, pw), dtype=dt); got_u = np.zeros((uh, uw), dtype=dt); got_v = np.zeros((uh, uw), dtype=dt)
    ok_y = ns["_mvk_place_plane"](got_y, y0, ph, pw)
    ok_u = ns["_mvk_place_plane"](got_u, u0, uh, uw)
    ok_v = ns["_mvk_place_plane"](got_v, v0, uh, uw)
    if not (ok_y and ok_u and ok_v):
        return None   # mvk non applicable pour cette géométrie -> pas de comparaison possible

    dY = int(np.abs(ref_y.astype(np.int64) - got_y.astype(np.int64)).max()) if ref_y.size else 0
    dU = int(np.abs(ref_u.astype(np.int64) - got_u.astype(np.int64)).max()) if ref_u.size else 0
    dV = int(np.abs(ref_v.astype(np.int64) - got_v.astype(np.int64)).max()) if ref_v.size else 0
    return dY, dU, dV


def _make_pd(lyt, pw, ph):
    cw, ch = lyt["cw"], lyt["ch"]
    uw, uh = pw // cw, ph // ch
    strided = (lyt["height"] % ph == 0 and lyt["width"] % pw == 0)
    return dict(pw=pw, ph=ph, uw=uw, uh=uh,
                chp=max(1, ph // max(1, uh)), strided=strided,
                cx=None if strided else ((np.arange(pw) * lyt["width"]) // pw).astype(np.int32),
                ccx=None if strided else ((np.arange(uw) * lyt["uv_w"]) // uw).astype(np.int32))


def check_band(ns, label, w, h, level, chroma="420", bit_depth=8, n_bands=5, force_gather=False):
    lyt = _mk_lyt(ns, w, h, chroma, bit_depth)
    cw, ch = lyt["cw"], lyt["ch"]
    pw, ph = ns["_proxy_dims"](w, h, level, cw, ch)
    if force_gather:
        pw -= 1 if pw % 2 == 0 else 0   # casse le ratio entier -> force le chemin gather
        pw = max(cw * 2, pw - (pw % (2 * cw)) + cw)  # garder pair/aligné chroma mais non-diviseur
    pd = _make_pd(lyt, pw, ph)
    uw, uh = pd["uw"], pd["uh"]
    rng = np.random.default_rng(hash((label, w, h, level, force_gather)) & 0xffffffff)
    y0, u0, v0 = _rand_planes(rng, lyt)
    dt = lyt["np_dt"]

    # Découpe le proxy en n_bands bandes de lignes (alignées chp) -> commit progressif, comme
    # le worker (upto croissant).
    step = max(pd["chp"], (ph // n_bands) - ((ph // n_bands) % pd["chp"]) or pd["chp"])
    bounds = list(range(step, ph, step)) + [ph]

    max_d = [0, 0, 0]
    for use_mvk in (False, True):
        py = np.zeros((ph, pw), dtype=dt); pu = np.zeros((uh, uw), dtype=dt); pv = np.zeros((uh, uw), dtype=dt)
        out = [pd, None, py, pu, pv, 0, 0]
        for upto in bounds:
            if use_mvk:
                b = min(pd["ph"], int(upto)); b -= b % pd["chp"]
                a = out[5]
                if b <= a:
                    continue
                qa, qb = a // pd["chp"], b // pd["chp"]
                assert ns["_mvk_band"](out, y0, u0, v0, lyt, a, b, qa, qb), "mvk_band a échoué (géométrie non applicable)"
                out[5] = b
            else:
                # _emit_band tente mvk lui-même si _MVK ; on veut le chemin numpy PUR ici pour
                # calculer la référence -> on ré-implémente son repli directement (identique au
                # code source, cf. plugins/pyramide/script.py::_emit_band, branche numpy).
                b = min(pd["ph"], int(upto)); b -= b % pd["chp"]
                a = out[5]
                if b <= a:
                    continue
                qa, qb = a // pd["chp"], b // pd["chp"]
                src_h = lyt["height"]; s_uvh = lyt["uv_h"]
                if pd["strided"]:
                    sy = src_h // pd["ph"]; sx = lyt["width"] // pd["pw"]
                    py[a:b] = y0[a * sy:b * sy:sy, ::sx]
                    if qb > qa:
                        scy = s_uvh // pd["uh"]; scx = lyt["uv_w"] // pd["uw"]
                        pu[qa:qb] = u0[qa * scy:qb * scy:scy, ::scx]
                        pv[qa:qb] = v0[qa * scy:qb * scy:scy, ::scx]
                else:
                    ry = (np.arange(a, b) * src_h) // pd["ph"]
                    py[a:b] = y0[ry][:, pd["cx"]]
                    if qb > qa:
                        rc = (np.arange(qa, qb) * s_uvh) // pd["uh"]
                        pu[qa:qb] = u0[rc][:, pd["ccx"]]
                        pv[qa:qb] = v0[rc][:, pd["ccx"]]
                out[5] = b
        if use_mvk:
            got_y, got_u, got_v = py, pu, pv
        else:
            ref_y, ref_u, ref_v = py, pu, pv

    dY = int(np.abs(ref_y.astype(np.int64) - got_y.astype(np.int64)).max()) if ref_y.size else 0
    dU = int(np.abs(ref_u.astype(np.int64) - got_u.astype(np.int64)).max()) if ref_u.size else 0
    dV = int(np.abs(ref_v.astype(np.int64) - got_v.astype(np.int64)).max()) if ref_v.size else 0
    return dY, dU, dV, pd["strided"]


def main():
    ns = load_ns({"flux_config": []})
    print("_MVK =", ns["_MVK"])
    fails = 0
    checks = 0

    print("\n== whole-frame (resize_plane vs _mvk_place_plane) ==")
    for label, w, h, chroma, bd in (
        ("8b 420 1920x1080", 1920, 1080, "420", 8),
        ("10b 422 1920x1080", 1920, 1080, "422", 10),
        ("8b 420 1280x720", 1280, 720, "420", 8),
    ):
        for level in (2, 3, 4, 8, 16):
            res = check_whole_frame(ns, label, w, h, level, chroma, bd)
            checks += 1
            if res is None:
                print("SKIP(mvk n/a) %-22s level=%2d" % (label, level))
                continue
            dY, dU, dV = res
            ok = dY == 0 and dU == 0 and dV == 0
            fails += 0 if ok else 1
            print("%s %-22s level=%2d maxΔ(Y,U,V)=%s" % ("OK  " if ok else "FAIL", label, level, res))

    print("\n== mode tranche (numpy repli vs _mvk_band), bandes partielles ==")
    for label, w, h, chroma, bd in (
        ("8b 420 1920x1080", 1920, 1080, "420", 8),
        ("10b 422 1920x1080", 1920, 1080, "422", 10),
    ):
        for level in (2, 4, 8, 16):
            for force_gather in (False, True):
                res = check_band(ns, label, w, h, level, chroma, bd, n_bands=7, force_gather=force_gather)
                checks += 1
                dY, dU, dV, strided = res
                ok = dY == 0 and dU == 0 and dV == 0
                fails += 0 if ok else 1
                print("%s %-22s level=%2d gather_forced=%-5s strided=%-5s maxΔ(Y,U,V)=%s" %
                      ("OK  " if ok else "FAIL", label, level, force_gather, strided, res[:3]))

    print("\nTotal checks=%d fails=%d" % (checks, fails))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
