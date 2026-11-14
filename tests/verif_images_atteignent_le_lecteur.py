#!/usr/bin/env python3
"""Every key of `_IMAGES` must reach a READER.

A guard on the producer does not prove the reader. `tests/verif_image_decklink_couche_fine.py`
checked that `decklink` was in `_IMG_ORDER` — the producer — and the image was still invisible
everywhere an operator could have acted on it: the Images panel listed four images out of seven,
the node checklist another four, `/readiness` a third four, and the only per-node build button was
hard-wired to `mtl`. Four hand-written lists, none derived from `_IMAGES`, each FILTERING in
silence. The product offered to build an image no screen showed, and its refusal message pointed
at a screen that could not exist (measured 2026-11-13, on a node carrying a DeckLink card).

So this guard looks at the READER side, and it refuses a hand-written list wherever one used to be.
It works on a COPY of the tree, so every check can be mutated and seen to turn red — a check that
has never failed proves nothing (and a mirror made of symlinks would let the mutation write into
the real repository: `shutil.copy2`, never `os.symlink`).
"""
import ast
import io
import json
import os
import re
import shutil
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FICHIERS = ("app/routes/images.py", "app/routes/nodes.py", "app/routes/node_network.py",
            "app/docker_compute.py", "templates/settings.html", "i18n/fr.json", "i18n/en.json")

# The three refusal causes that used to be one message, plus the key KEPT for the rows already
# written: alerts persist their i18n key, so retiring it would render old rows as a raw code.
CLES_REFUS = ("alert.deploy.compute.sans_capacite_decklink",
              "alert.deploy.compute.sans_capacite_gui",
              "alert.deploy.compute.image_non_enregistree",
              "alert.deploy.compute.sans_reseau_conteneurs",
              "alert.deploy.compute.sans_image_ou_reseau")
CLES_UI = ("settings.img.build_layer", "settings.img.build_long_mtl", "settings.img.go_build_tab",
           "settings.img.hint_cluster", "settings.img.hint_node_only")


def _lire(racine, rel):
    return io.open(os.path.join(racine, rel), encoding="utf-8").read()


def _source_affectation(src, nom):
    """Source text of the `nom = ...` top-level assignment (empty string if there is none)."""
    arbre = ast.parse(src)
    for n in arbre.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == nom for t in n.targets):
            return ast.get_source_segment(src, n) or ""
    return ""


def _cles_images(src):
    """Keys of the `_IMAGES` dict literal, read without importing (the copy is mutated)."""
    arbre = ast.parse(src)
    for n in arbre.body:
        if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_IMAGES"
                                              for t in n.targets)
                and isinstance(n.value, ast.Dict)):
            return [k.value for k in n.value.keys if isinstance(k, ast.Constant)]
    return []


def _corps_fonction(src, nom):
    """Source text of a function or method named `nom`, anywhere in the file."""
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == nom:
            return ast.get_source_segment(src, n) or ""
    return ""


def _corps_js(src, signature):
    """Source text of a JS function from its signature up to the matching closing brace."""
    i = src.find(signature)
    if i < 0:
        return ""
    j = src.index("{", i)
    prof, k = 0, j
    while k < len(src):
        if src[k] == "{":
            prof += 1
        elif src[k] == "}":
            prof -= 1
            if prof == 0:
                return src[i:k + 1]
        k += 1
    return ""


# ── THE CHECKS ─────────────────────────────────────────────────────────────────────────────
# Each one returns (ok, message). They read text only, so they run against the mutated copy.

def verifier(racine):
    res = []

    def dire(cid, ok, msg):
        res.append((cid, bool(ok), msg))

    images = _lire(racine, "app/routes/images.py")
    nodes = _lire(racine, "app/routes/nodes.py")
    netw = _lire(racine, "app/routes/node_network.py")
    compute = _lire(racine, "app/docker_compute.py")
    html = _lire(racine, "templates/settings.html")

    cles = _cles_images(images)
    dire("images_lues", len(cles) >= 5, "`_IMAGES` holds %d image(s): %s" % (len(cles), ", ".join(cles)))

    # 1. COMPLETENESS IS DERIVED, not retyped. The expression must walk `_IMAGES` itself, so an
    #    image added to the table cannot be absent from the order — at worst it lands last.
    src_order = _source_affectation(images, "IMAGE_ORDER")
    dire("ordre_derive", bool(src_order) and "_IMAGES" in src_order,
         "`IMAGE_ORDER` is derived from `_IMAGES`" if src_order else "`IMAGE_ORDER` is missing")

    # 2. No dead name in the display order (a renamed image would silently lose its place).
    src_pref = _source_affectation(images, "_PREFERRED_ORDER")
    noms_pref = re.findall(r'"([a-z0-9_-]+)"', src_pref)
    morts = [w for w in noms_pref if w not in cles]
    dire("ordre_sans_nom_mort", not morts,
         "no dead name in `_PREFERRED_ORDER`" if not morts else "unknown image(s): %s" % morts)

    # 3. `/api/images/status` serves the order, so the browser stops keeping a list of its own.
    st = _corps_fonction(images, "api_images_status")
    dire("statut_sert_lordre", '"_order"' in st and "IMAGE_ORDER" in st,
         "`/api/images/status` serves `_order` from IMAGE_ORDER")

    # 4. The `return` that was missing in the `BaseImageMissing` branch: without it, execution fell
    #    through with `ctx` unbound and a NameError OVERWROTE the one clear message of the function.
    worker = _corps_fonction(images, "_build_node_only_worker")
    m = re.search(r"except BaseImageMissing[^\n]*\n((?:[ \t]+[^\n]*\n|\n)+?)(?=[ \t]*except |\Z)",
                  worker)
    dire("retour_base_absente", bool(m) and re.search(r"^\s*return\s*$", m.group(1), re.M),
         "the `BaseImageMissing` branch returns instead of falling through")

    # 5. `_IMG_ORDER` re-exports, it does not retype. It used to carry the warning « EVERY IMAGE IN
    #    `_IMAGES` MUST APPEAR HERE… and that is silent » — a comment is not a mechanism.
    src_img_order = _source_affectation(nodes, "_IMG_ORDER")
    dire("noeuds_reexportent", src_img_order.strip() == "_IMG_ORDER = IMAGE_ORDER",
         "`nodes._IMG_ORDER` re-exports IMAGE_ORDER (found: %r)" % src_img_order.strip()[:60])

    # 6. Shared against node_only is already declared in `_IMAGES`; retyping it would, when
    #    forgotten, push an image that exists on one node only.
    src_shared = _source_affectation(nodes, "_SHARED_IMAGES")
    dire("partagees_derivees", "node_only" in src_shared and "IMAGE_ORDER" in src_shared,
         "`_SHARED_IMAGES` is derived from the `node_only` flag")

    # 7. `/readiness` — the fourth list. It must walk IMAGE_ORDER and name no image.
    rd = _corps_fonction(netw, "api_node_readiness")
    bloc = rd[rd.find('out["images"]'):] if 'out["images"]' in rd else ""
    nommees = [w for w in cles if '"%s":' % w in bloc]
    dire("readiness_derive",
         "for which in IMAGE_ORDER" in bloc and not nommees,
         "`/readiness` walks IMAGE_ORDER" if not nommees
         else "`/readiness` still names: %s" % nommees)

    # 8. It must also say which images are built ON the node, so the page can offer the build
    #    rather than sending the operator to a tab that does not build them.
    dire("readiness_dit_node_only",
         'out["images_node_only"] = ' in bloc and 'out["image_builds"] = ' in bloc,
         "`/readiness` publishes `images_node_only` and `image_builds`")

    # 9. The node checklist (`_imgMap`) reads what the server judged relevant. The hand-written
    #    version named four images, so `gui` and `decklink` had no checklist line at all — and a
    #    missing line does not read as a bug, it reads as nothing to do.
    imgmap = _corps_js(html, "function _imgMap(")
    nommees = [w for w in cles if "'%s'" % w in imgmap or '"%s"' % w in imgmap]
    dire("imgmap_derive", "Object.keys" in imgmap and not nommees,
         "`_imgMap` derives from the server payload" if not nommees
         else "`_imgMap` still names: %s" % nommees)

    # 10. The Images panel takes its order from the server.
    dire("panneau_prend_lordre", re.search(r"const order\s*=\s*d\._order", html),
         "the Images panel orders from `d._order`")

    # 11. One generic per-node build, parameterised by image. The old one was wired to `mtl`,
    #     which is precisely why gui and decklink had no button.
    bn = _corps_js(html, "async function buildNodeImage(")
    appels = re.findall(r"JSON\.stringify\(\{which[^}]*\}\)", html)
    figes = [a for a in appels if re.search(r"which\s*:\s*['\"]", a)]
    dire("build_generique",
         bool(bn) and "which" in bn.split("{", 1)[0] and bool(appels) and not figes,
         "`buildNodeImage(id, which)` exists and no build call is wired to a fixed image"
         if not figes else "build call(s) wired to a fixed image: %s" % figes)

    # 12. The capability refusal is REACHABLE. `if not (image and network)` fired first, so
    #     `sans_capacite_decklink` could never be rendered when the tag setting was empty: the
    #     precise message existed and nothing could reach it.
    dep = _corps_fonction(compute, "deploy_compute")
    dire("deploy_compute_lue", bool(dep), "`deploy_compute` located in app/docker_compute.py")
    dire("refus_capacite_atteignable",
         "not image and not _refus_capacite" in dep and "if not (image and network)" not in dep,
         "the image refusal steps aside when the capability is the cause")

    # 13. Three causes, three keys, each produced somewhere in the module.
    manquantes = [k for k in CLES_REFUS[:4] if '"%s"' % k not in compute]
    dire("trois_causes", not manquantes,
         "the four precise refusals are produced" if not manquantes else "not produced: %s" % manquantes)

    # 14. Both catalogues, including the obsolete key (alerts persist keys: retiring it would turn
    #     every row written before today into a raw code on screen).
    for lang in ("fr", "en"):
        cat = json.loads(_lire(racine, "i18n/%s.json" % lang))
        absentes = [k for k in CLES_REFUS + CLES_UI if k not in cat]
        dire("i18n_" + lang, not absentes,
             "%s: the %d keys are translated" % (lang, len(CLES_REFUS + CLES_UI))
             if not absentes else "%s: missing %s" % (lang, absentes))
    return res


# ── MUTATIONS ──────────────────────────────────────────────────────────────────────────────
# (cible, ancien, nouveau, check that must turn RED). A green reference run proves nothing on its
# own: these say what each check would have caught.
MUTATIONS = [
    ("ordre_derive", "app/routes/images.py",
     "IMAGE_ORDER = tuple([w for w in _PREFERRED_ORDER if w in _IMAGES]\n"
     "                    + [w for w in _IMAGES if w not in _PREFERRED_ORDER])",
     'IMAGE_ORDER = ("compute", "media", "webrtc", "mtl")'),
    ("ordre_sans_nom_mort", "app/routes/images.py",
     '_PREFERRED_ORDER = ("compute",', '_PREFERRED_ORDER = ("receiver_2110", "compute",'),
    ("statut_sert_lordre", "app/routes/images.py",
     '"_order": list(IMAGE_ORDER)}', '}'),
    ("retour_base_absente", "app/routes/images.py",
     "        return\n    except Exception as e:",
     "    except Exception as e:"),
    ("noeuds_reexportent", "app/routes/nodes.py", "_IMG_ORDER = IMAGE_ORDER",
     '_IMG_ORDER = ("compute", "media", "webrtc", "mtl")'),
    ("partagees_derivees", "app/routes/nodes.py",
     "_SHARED_IMAGES = tuple(w for w in IMAGE_ORDER if not _IMAGES[w].get(\"node_only\"))",
     '_SHARED_IMAGES = ("compute", "media", "webrtc")'),
    ("readiness_derive", "app/routes/node_network.py",
     "    for which in IMAGE_ORDER:", '    out["images"]["compute"] = _img_ok("compute")\n    for which in ():'),
    ("readiness_dit_node_only", "app/routes/node_network.py",
     '    out["images_node_only"] = []', "    _inutilise = []"),
    ("imgmap_derive", "templates/settings.html",
     "    return Object.keys(im).map(k => ({ k, ok: im[k] }));",
     "    return [{ k: 'compute', ok: im.compute }, { k: 'mtl', ok: im.mtl }];"),
    ("panneau_prend_lordre", "templates/settings.html",
     "const order = d._order || ['compute', 'media', 'webrtc', 'mtl'];",
     "const order = ['compute', 'media', 'webrtc', 'mtl'];"),
    ("build_generique", "templates/settings.html",
     "JSON.stringify({which})", "JSON.stringify({which:'mtl'})"),
    ("refus_capacite_atteignable", "app/docker_compute.py",
     "    if not image and not _refus_capacite:", "    if not (image and network):"),
    ("trois_causes", "app/docker_compute.py",
     '"alert.deploy.compute.image_non_enregistree"', '"alert.deploy.compute.autre_chose"'),
    ("i18n_fr", "i18n/fr.json",
     '  "alert.deploy.compute.sans_image_ou_reseau"', '  "alert.deploy.compute.retire"'),
    ("i18n_en", "i18n/en.json",
     '  "settings.img.hint_node_only"', '  "settings.img.retire"'),
]


def _copie(dest):
    """Real copy of the files under test (copy2, NEVER a symlink: a mirror made of links lets the
    mutation write into the real repository — measured 2026-11-12)."""
    for rel in FICHIERS:
        cible = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(cible), exist_ok=True)
        shutil.copy2(os.path.join(RACINE, rel), cible)
        assert not os.path.islink(cible)


def main():
    echecs = 0
    print("Reference run (no mutation)")
    ref = verifier(RACINE)
    etats = {}
    for cid, ok, msg in ref:
        etats[cid] = ok
        print("  %s %-28s %s" % ("✓" if ok else "✗", cid, msg))
        if not ok:
            echecs += 1
    if echecs:
        print("\n%d check(s) FAILED on the real tree — the mutations are not run." % echecs)
        return 1

    print("\nMutations (each must turn its check red)")
    for cid, rel, vieux, neuf in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="verif-images-lecteur-") as tmp:
            _copie(tmp)
            p = os.path.join(tmp, rel)
            src = io.open(p, encoding="utf-8").read()
            if src.count(vieux) < 1:
                print("  ✗ %-28s anchor not found in %s — the mutation did not apply, so "
                      "it proves nothing" % (cid, rel))
                echecs += 1
                continue
            io.open(p, "w", encoding="utf-8").write(src.replace(vieux, neuf, 1))
            try:
                apres = dict((c, ok) for c, ok, _ in verifier(tmp))
            except SyntaxError as e:
                print("  ✗ %-28s the mutation broke the parse (%s)" % (cid, e))
                echecs += 1
                continue
            if apres.get(cid) is True:
                print("  ✗ %-28s NOT caught — the check is blind to it" % cid)
                echecs += 1
            else:
                print("  ✓ %-28s caught" % cid)
    print("\n%d/%d mutations caught" % (len(MUTATIONS) - echecs, len(MUTATIONS)))
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
