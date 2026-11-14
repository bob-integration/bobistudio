#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""A derived build whose `FROM` is missing must say SO — not blame a registry.

★ WHAT HAPPENED (acceptance run, r620-3/r620-1, 2026-08-21). `bobi-compute-gpu:0.10` is a thin
layer `FROM bobi-compute:0.29`. Built on a node that does not have the 0.29 locally, Docker goes
looking for it on Docker Hub and returns « pull access denied, repository does not exist or may
require authorization ». The message names AUTHORIZATION; the cause is AVAILABILITY. The tester
spent his time on registry credentials — the interface had sent him there.

★ WHAT THIS GUARD REALLY EXISTS FOR (found 2026-11-09). The fix written that day was placed in
`_build_image_worker`, which returns to `_build_node_only_worker` for every `node_only` image
BEFORE reaching it. All three images that have a `base_from` (compute-gpu, gui, decklink) are
`node_only`, and no shared image has one — so the pre-flight could not fire on any installation,
ever. The TODO entry was ticked, the code was there, and the defect was intact. Hence check 1: the
pre-flight is verified ON THE PATHS THE DERIVED IMAGES ACTUALLY TAKE, not where it reads well.

What it holds:
1. ★★ every image with a `base_from` is covered on the path that builds it (the three of them are
   `node_only`), and the pre-flight runs BEFORE `build_image`, not after;
2. ★★ the refusal NAMES the base tag and the node, and points at distributing the image;
3. ★★ tri-state: an unreadable inventory or a node without an agent is NOT « base image missing ».
   Confusing the two sends an operator to distribute an image that is already there, and blocks a
   build on a read we do not have — the same misleading message, one layer up;
4. ★★ an abstention does not block: the build runs, and its failure is still explained;
5. ★★ one node missing its base does not stop the other nodes from building;
6. ★★ a registry denial naming one of OUR images is explained; one naming `debian:13` is left
   alone — reinterpreting a real registry failure would be the same mistake in reverse;
7. ★ the three failure surfaces (Images banner, per-node banner, alert) pass through the
   explanation, so the operator never reads the raw wording alone.

Offline: throwaway DB, no node reachable, no Docker, no network.

MUTATIONS (IMG_BASE_MUTATION=<name>) — each one must give rc=1:
    base_toujours_la        `_base_image_state` always answers « present »
    abstention_en_absence   an unreadable inventory becomes « base image missing »
    preflight_apres_build   the pre-flight runs after `build_image` instead of before
    preflight_retire        the per-node pre-flight is removed
    refus_sans_tag          the refusal no longer names the base image
    un_noeud_bloque_tout    the refusal escapes the per-node try → the whole fleet stops
    explication_toujours    a `debian:13` denial is reinterpreted as one of ours
    explication_jamais      the denial is passed through raw
    alerte_brute            the alert keeps the raw Docker wording
    cle_non_publiee         the per-node state keeps only the French sentence
    phrase_dans_lalerte     the alert goes back to a French blob in a parameter
    localite_en_parametre   « locally » and « on node X » share one key
    param_oublie            a `{param}` of the label is no longer provided
    js_sans_cle             the screen reads `msg` and ignores the key

    ./venv/bin/python tests/verif_image_base_absente.py
"""
import ast
import importlib.util
import json
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                                        # noqa: E402

_TMP = os.path.join(tempfile.mkdtemp(prefix="verif_img_base_"), "t.db")
config.DB_PATH = _TMP
database.DB_PATH = _TMP
database._tls.__dict__.pop("conn", None)
database.init_db()

MUT = os.environ.get("IMG_BASE_MUTATION", "")
echecs, total = [], [0]


def verifie(nom, cond, detail=""):
    total[0] += 1
    if cond:
        print(f"  \033[32m✓\033[0m {nom}")
    else:
        print(f"  \033[31m✗\033[0m {nom}" + (f"  → {detail}" if detail else ""))
        echecs.append(nom)


# ─── The module under test, loaded FROM SOURCE so the defect can be re-created ───────
# Mutating the source re-creates the bug in the real code. A mutation that only touched a stand-in
# would prove nothing: a stand-in cannot contradict its author.
_SRC_IMG = os.path.join(RACINE, "app", "routes", "images.py")
_SRC_NODES = os.path.join(RACINE, "app", "routes", "nodes.py")

_REFUS_AVANT = """    return ("image de base « %s » ABSENTE %s : ce build en dérive (FROM). Docker irait la "
            "chercher sur Docker Hub et échouerait sur « pull access denied » — un message "
            "d'autorisation pour une cause de disponibilité. Distribuer d'abord « %s » "
            "(Réglages → Images), puis relancer ce build." % (base_tag, ou, base_tag))"""
_REFUS_APRES = """    return ("échec de build%.0s%.0s%.0s" % (base_tag, ou, base_tag))"""

_MUT_IMG = {
    "base_toujours_la": ("    return any((i.get(\"tag\") or \"\") == base_tag for i in imgs), base_tag, where",
                         "    return True, base_tag, where"),
    "abstention_en_absence": ("    imgs, err = _node_image_inventory(node, force=True)\n"
                              "    if err:\n        return None, base_tag, where\n",
                              "    imgs, err = _node_image_inventory(node, force=True)\n"
                              "    if err:\n        return False, base_tag, where\n"),
    "preflight_apres_build": ("            if _bstate is False:\n"
                              "                raise BaseImageMissing(_btag, _bwhere)\n",
                              "            _differe = _bstate is False\n"),
    "preflight_retire": ("            _bstate, _btag, _bwhere = _base_image_state(which, node=node)\n",
                         "            _bstate, _btag, _bwhere = (True, None, \"\")\n"),
    # The whole sentence replaced by a bare « build failed »: the defect this guards
    # against is a message that names neither the image nor the gesture.
    "refus_sans_tag": (_REFUS_AVANT, _REFUS_APRES),
    # The refusal escapes the per-node loop instead of failing one node: the whole fleet stops.
    "un_noeud_bloque_tout": ("        except BaseImageMissing as e:\n            tail, refus = str(e), e\n",
                             "        except BaseImageMissing as e:\n            tail, refus = str(e), e\n"
                             "            raise\n"),
    "explication_toujours": ("    if not refs:\n        return tail\n",
                             "    refs = refs or [\"?\"]\n    if False:\n        return tail\n"),
    "explication_jamais": ("    if not tail or not _REGISTRY_DENIAL_RE.search(tail):\n        return tail",
                           "    if True:\n        return tail"),
    "cle_non_publiee": ('            if refus is not None:\n'
                        '                _node_img_build[key]["msg_key"] = refus.key\n',
                        '            if False:\n'
                        '                _node_img_build[key]["msg_key"] = refus.key\n'),
    "phrase_dans_lalerte": ('                db_add_alert(refus.alert_key, "warning", node_id=nid, kind="prep",\n'
                            '                             params=dict(refus.params, tag=tag))',
                            '                db_add_alert("alert.image.build_noeud_echec", "warning", node_id=nid,\n'
                            '                             kind="prep", params={"tag": tag, "n": nname, "e": _expl})'),
    "localite_en_parametre": ('        self.key = ("settings.img.base_absente_noeud" if node_name\n'
                              '                    else "settings.img.base_absente_locale")',
                              '        self.key = "settings.img.base_absente_noeud"'),
    "param_oublie": ('        if node_name:\n            self.params["n"] = node_name\n',
                     '        if False:\n            self.params["n"] = node_name\n'),
    "alerte_brute": ("params={\"tag\": tag, \"n\": nname, \"e\": _expl[-1200:]}",
                     "params={\"tag\": tag, \"n\": nname, \"e\": str(tail)[-1200:]}"),
}


def charger(src_path, mutations, nom_mod, paquet):
    src = open(src_path, encoding="utf-8").read()
    if MUT in mutations:
        avant, apres = mutations[MUT]
        assert avant in src, f"mutation {MUT} : motif introuvable dans {src_path} (source bougée)"
        src = src.replace(avant, apres, 1)
    spec = importlib.util.spec_from_file_location(nom_mod, src_path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = paquet
    exec(compile(src, src_path, "exec"), mod.__dict__)
    return mod, src


I, SRC_IMG = charger(_SRC_IMG, _MUT_IMG, "app.routes._images_sous_test", "app.routes")
SRC_NODES = open(_SRC_NODES, encoding="utf-8").read()
if MUT == "preflight_retire":
    # The same removal, on the other node path: one guard, both doors.
    SRC_NODES = SRC_NODES.replace(
        "            _bstate, _btag, _bwhere = _base_image_state(which, node=node)\n",
        "            _bstate, _btag, _bwhere = (True, None, \"\")\n", 1)


# ════════════════════════════════════════════════════════════════════════════════════
print("\n1. The pre-flight is on the paths the derived images REALLY take")

derivees = {k: v for k, v in I._IMAGES.items() if v.get("base_from")}
verifie("1a. at least one thin layer exists (otherwise this guard watches nothing)",
        bool(derivees), str(list(I._IMAGES)))
# The 2026-11-09 finding, written as a check: if a thin layer were NOT node_only, the shared path
# would have to be covered too — it is, but this states which path each image needs.
for k, v in derivees.items():
    verifie(f"1b. {k} derives from {v['base_from']} and is node_only={bool(v.get('node_only'))}",
            True)


def _corps(src, nom_fonction, chemin=()):
    """The AST body of a function, found by name (optionally nested inside another)."""
    arbre = ast.parse(src)
    noeuds = [n for n in ast.walk(arbre)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == nom_fonction]
    assert noeuds, f"fonction {nom_fonction} introuvable"
    return noeuds[0]


def _ordre_appels(noeud, *noms):
    """First line number at which each name is called inside this function (None if never)."""
    vus = {n: None for n in noms}
    for n in ast.walk(noeud):
        if isinstance(n, ast.Call):
            f = n.func
            nom = getattr(f, "id", None) or getattr(f, "attr", None)
            if nom in vus and vus[nom] is None:
                vus[nom] = n.lineno
    return vus


for libelle, src, fonction in (("node_only worker", SRC_IMG, "_build_node_only_worker"),
                               ("page Nœuds", SRC_NODES, "api_node_build_image"),
                               ("page Images", SRC_IMG, "_build_image_worker")):
    f = _corps(src, fonction)
    o = _ordre_appels(f, "_base_image_state", "build_image", "_stage_tar")
    verifie(f"1c. {libelle} : the pre-flight is called", o["_base_image_state"] is not None)
    if o["_base_image_state"] is not None and o["build_image"] is not None:
        verifie(f"1d. {libelle} : pre-flight BEFORE build_image",
                o["_base_image_state"] < o["build_image"],
                f"pré-vol l.{o['_base_image_state']} / build l.{o['build_image']}")
    # A pre-flight that only computes a value and never refuses is decoration: the refusal must be
    # conditioned on `is False` — never on a plain truth test, which would swallow the abstention.
    txt = ast.unparse(f)
    verifie(f"1e. {libelle} : refusal on `is False` (abstention not swallowed)",
            "_bstate is False" in txt, txt[:0] or "pas de `_bstate is False`")
    verifie(f"1f. {libelle} : the refusal raises",
            any(isinstance(n, ast.Raise) for n in ast.walk(f)))


# ════════════════════════════════════════════════════════════════════════════════════
print("\n2. The refusal names the image, the node, and the gesture")

msg = I._base_missing_message("bobi-compute:0.29", "r620-1")
verifie("2a. names the base tag", "bobi-compute:0.29" in msg, msg)
verifie("2b. names the node", "r620-1" in msg, msg)
verifie("2c. says « absente », not « autorisation »",
        "ABSENTE" in msg and "pull access denied" in msg, msg)
verifie("2d. points at distributing the image", "istribuer" in msg, msg)
verifie("2e. names the repair place (Réglages → Images)", "Réglages → Images" in msg, msg)


# ════════════════════════════════════════════════════════════════════════════════════
print("\n3. Tri-state: not measured is NOT « missing »")

I._image_tag = lambda which: {"compute": "bobi-compute:0.29",
                              "compute-gpu": "bobi-compute-gpu:0.10"}.get(which, which + ":1")

_INV = {"etat": ([{"tag": "bobi-compute:0.29"}], "")}
I._node_image_inventory = lambda node, force=False: _INV["etat"]

NOEUD = {"id": 1, "name": "r620-1", "host": "198.51.100.11", "agent_url": "http://198.51.100.11:9100"}

_INV["etat"] = ([{"tag": "bobi-compute:0.29"}], "")
verifie("3a. base present → True", I._base_image_state("compute-gpu", node=NOEUD)[0] is True)

_INV["etat"] = ([{"tag": "bobi-compute:0.21"}], "")
e, tag, ou = I._base_image_state("compute-gpu", node=NOEUD)
verifie("3b. base absent (0.21 only) → False", e is False, repr(e))
verifie("3c. the tag returned is the one Docker will resolve", tag == "bobi-compute:0.29", str(tag))
verifie("3d. the node is named", ou == "r620-1", str(ou))

_INV["etat"] = ([], "host_exec: connexion refusée")
e = I._base_image_state("compute-gpu", node=NOEUD)[0]
verifie("3e. ★ unreadable inventory → None, NOT False", e is None, repr(e))

e = I._base_image_state("compute-gpu", node={"id": 2, "name": "legacy-1", "host": "198.51.100.12"})[0]
verifie("3f. ★ node without agent → None (ssh read known unreliable here)", e is None, repr(e))

_INV["etat"] = ([], "")
e, tag, _ = I._base_image_state("compute", node=NOEUD)
verifie("3g. image without base_from → True and no tag", e is True and tag is None, f"{e} {tag}")


# ════════════════════════════════════════════════════════════════════════════════════
print("\n4-5. The worker: refuses the right node, and only that one")

import app.node_driver as ND                                            # noqa: E402

_APPELS = []
_VRAI_BUILD = ND.build_image


def _faux_build(node, tag, ctx, timeout=2400):
    _APPELS.append(str(node.get("name")))
    return 0, "naming to %s done" % tag


def _lancer(inventaires, which="compute-gpu"):
    """Run the per-node worker over two nodes whose inventories we control."""
    del _APPELS[:]
    noeuds = [{"id": 1, "name": "dell-1", "host": "198.51.100.11",
               "agent_url": "http://198.51.100.11:9100"},
              {"id": 2, "name": "dl360-1", "host": "198.51.100.12",
               "agent_url": "http://198.51.100.12:9100"}]
    I._image_target_nodes = lambda w: noeuds
    I._stage_tar = lambda w: b"tar"
    I._node_image_inventory = lambda node, force=False: inventaires[node["name"]]
    I._image_present_node = lambda node, tag, force=False: True
    I.record_node_only_tag = lambda *a, **k: None
    I.db_get_nodes = lambda: []
    ND.build_image = _faux_build
    try:
        I._build_node_only_worker(which)
    except Exception as e:
        # A refusal that escapes the per-node loop stops the whole fleet — recorded, not swallowed.
        _APPELS.append("CRASH:%s" % e)
    finally:
        ND.build_image = _VRAI_BUILD
    return {n["name"]: dict(I._node_img_build.get("%d:%s" % (n["id"], which), {})) for n in noeuds}


PRESENT = ([{"tag": "bobi-compute:0.29"}], "")
ABSENT = ([{"tag": "bobi-compute:0.21"}], "")
MUET = ([], "host_exec: délai dépassé")

r = _lancer({"dell-1": ABSENT, "dl360-1": PRESENT})
verifie("4a. ★ the node without its base is NOT built", "dell-1" not in _APPELS, str(_APPELS))
verifie("5a. ★ the other node IS built (one failure does not stop the fleet)",
        "dl360-1" in _APPELS, str(_APPELS))
verifie("4b. its status is error", r["dell-1"].get("status") == "error", str(r["dell-1"]))
verifie("4c. its message names the base image",
        "bobi-compute:0.29" in r["dell-1"].get("msg", ""), r["dell-1"].get("msg", "")[:160])
verifie("4d. its message names the node", "dell-1" in r["dell-1"].get("msg", ""),
        r["dell-1"].get("msg", "")[:160])
verifie("5b. the built node reports ok", r["dl360-1"].get("status") == "ok", str(r["dl360-1"]))

r = _lancer({"dell-1": MUET, "dl360-1": PRESENT})
verifie("4e. ★ abstention does not block: the build runs anyway",
        "dell-1" in _APPELS, str(_APPELS))

r = _lancer({"dell-1": PRESENT, "dl360-1": PRESENT})
verifie("4f. both built when both have the base", sorted(_APPELS) == ["dell-1", "dl360-1"],
        str(_APPELS))


# ════════════════════════════════════════════════════════════════════════════════════
print("\n6. The registry denial is explained — and only when it is ours")

DENI = ("#8 [internal] load metadata for docker.io/library/bobi-compute:0.29\n"
        "ERROR: failed to solve: bobi-compute:0.29: pull access denied, repository does not "
        "exist or may require authorization")
out = I._explain_registry_denial(DENI)
verifie("6a. our image → explanation appended", len(out) > len(DENI), out[-80:])
verifie("6b. says it is not an authorization problem",
        "PAS UN PROBLÈME D'AUTORISATION" in out, out[-300:])
verifie("6c. names the reference", "bobi-compute:0.29" in out[len(DENI):], out[len(DENI):][:160])
verifie("6d. says there is no credential to check", "identifiant" in out[len(DENI):])
verifie("6e. keeps the original output", out.startswith(DENI))

TIERS = ("ERROR: failed to solve: debian:13: pull access denied, repository does not exist "
         "or may require authorization")
verifie("6f. ★ a third-party image is NOT reinterpreted",
        I._explain_registry_denial(TIERS) == TIERS, I._explain_registry_denial(TIERS)[-120:])

AUTRE = "ERROR: failed to solve: process \"/bin/sh -c pip install cupy\" did not complete"
verifie("6g. an unrelated failure is untouched", I._explain_registry_denial(AUTRE) == AUTRE)
verifie("6h. empty stays empty", I._explain_registry_denial("") == "")


# ════════════════════════════════════════════════════════════════════════════════════
print("\n7. The three failure surfaces pass through the explanation")

for libelle, src, fonction in (("node_only worker", SRC_IMG, "_build_node_only_worker"),
                               ("page Nœuds", SRC_NODES, "api_node_build_image"),
                               ("page Images", SRC_IMG, "_build_image_worker")):
    txt = ast.unparse(_corps(src, fonction))
    verifie(f"7a. {libelle} : the failure message is explained",
            "_explain_registry_denial" in txt)

# The alert and the banner are two surfaces of the SAME failure: the alert must not keep the raw
# wording while the banner is explained — the alert list is where an operator looks days later.
f = ast.unparse(_corps(SRC_IMG, "_build_node_only_worker"))
verifie("7b. ★ the per-node alert carries the explained text too",
        "'e': _expl[-1200:]" in f or '"e": _expl[-1200:]' in f, f[-400:])

# ════════════════════════════════════════════════════════════════════════════════════
print("\n8. The refusal travels as a KEY + values, rendered in the reader's language")

e_noeud = I.BaseImageMissing("bobi-compute:0.29", "dell-1")
e_local = I.BaseImageMissing("bobi-compute:0.29")
verifie("8a. a node refusal and a local one do NOT share a key", e_noeud.key != e_local.key,
        e_noeud.key)
verifie("8b. the banner key and the alert key are distinct", e_noeud.key != e_noeud.alert_key)
verifie("8c. the params carry the base tag", e_noeud.params.get("base") == "bobi-compute:0.29",
        str(e_noeud.params))
verifie("8d. the node name is a VALUE, not baked into a sentence",
        e_noeud.params.get("n") == "dell-1", str(e_noeud.params))
verifie("8e. the local refusal carries no node", "n" not in e_local.params, str(e_local.params))
verifie("8f. str(e) keeps the canonical French fallback (journal, older client)",
        "bobi-compute:0.29" in str(e_noeud) and "ABSENTE" in str(e_noeud), str(e_noeud)[:80])

CAT = {}
for lang in ("fr", "en"):
    with open(os.path.join(RACINE, "i18n", "%s.json" % lang), encoding="utf-8") as f:
        CAT[lang] = json.load(f)

for e, nom in ((e_noeud, "nœud"), (e_local, "local")):
    for champ in ("key", "alert_key"):
        cle = getattr(e, champ)
        for lang in ("fr", "en"):
            verifie(f"8g. {cle} présente en {lang}", cle in CAT[lang], cle)
        # A label that keeps a raw `{base}` on screen is worse than no label at all: every
        # placeholder of the sentence must be among the params actually sent.
        fournis = set(e.params) | {"tag"}        # `tag` is added at the alert call site
        for lang in ("fr", "en"):
            manquants = set(re.findall(r"\{(\w+)\}", CAT[lang].get(cle, ""))) - fournis
            verifie(f"8h. {cle} ({lang}) : tous les {{param}} sont fournis", not manquants,
                    str(manquants))

# The worker and the route must PUBLISH the key, not only the French sentence.
r = _lancer({"dell-1": ABSENT, "dl360-1": PRESENT})
verifie("8i. ★ the per-node state carries msg_key",
        r["dell-1"].get("msg_key") == e_noeud.key, str(r["dell-1"].get("msg_key")))
verifie("8j. ★ it carries the values too",
        (r["dell-1"].get("msg_params") or {}).get("n") == "dell-1",
        str(r["dell-1"].get("msg_params")))
verifie("8k. the French sentence stays as fallback", "bobi-compute:0.29" in r["dell-1"].get("msg", ""))

# The alert must use the dedicated key — not the generic one with a French blob in a parameter.
_lignes = database.db_get_alerts(kind="prep")
_cles = [l.get("msg_key") for l in _lignes]
verifie("8l. ★ the alert uses the dedicated key", e_noeud.alert_key in _cles, str(_cles[:4]))
_la = next((l for l in _lignes if l.get("msg_key") == e_noeud.alert_key), {})
_prm = _la.get("msg_params")
if isinstance(_prm, str):
    _prm = json.loads(_prm or "{}")
verifie("8m. ★ no French sentence smuggled in a parameter",
        not any("ABSENTE" in str(v) or "dérive" in str(v) for v in (_prm or {}).values()),
        str(_prm))

_MUT_TPL = {
    # The server can publish the key perfectly and the screen still show the raw French: the
    # composer must be wired at every display point, which is what this mutation removes.
    "js_sans_cle": ("buildMsg(", "(j||{}).msg || "),
}
TPL = {}
for _c in ("templates/layout.html", "templates/settings.html"):
    _t = open(os.path.join(RACINE, _c), encoding="utf-8").read()
    if MUT in _MUT_TPL:
        _t = _t.replace(*_MUT_TPL[MUT])
    TPL[_c] = _t

for chemin, attendus in (
        ("templates/layout.html", ["window.tp = function(key, params)"]),
        ("templates/settings.html", ["function buildMsg(j, repli)",
                                     "buildMsg(s, '')",            # bandeau page Images
                                     "buildMsg(mb, setT(",         # dernier build échoué
                                     "buildMsg(j, T('settings.js.st_fail')); _imgNodePoll"])):
    txt = TPL[chemin]
    for att in attendus:
        verifie(f"8n. {chemin} : {att[:46]}", att in txt)

verifie("8o. ★ /api/images/status forwards the key",
        '"msg_key": bs.get("msg_key")' in SRC_IMG)
verifie("8p. ★ the Nodes route publishes the key as well",
        '_node_img_build[key]["msg_key"] = refus.key' in SRC_NODES)


# ════════════════════════════════════════════════════════════════════════════════════
print("\n" + "─" * 78)
if echecs:
    print(f"\033[31m✗ {len(echecs)} échec(s) sur {total[0]}\033[0m : " + ", ".join(echecs))
    sys.exit(1)
print(f"\033[32m✓ {total[0]} vérifications\033[0m" + (f"  (mutation {MUT} non détectée !)" if MUT else ""))
sys.exit(0)
