#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""The installer records WHAT IT KNOWS: the installed ref and its commit.

★ WHY. An install from the GitHub archive has no `.git`, and `build_dist` never ran, so
`builder.current_build_info()` fell back to its `dev` marker — no `build_id`, no fingerprint —
even though `v0.9.15` had been asked for explicitly. Two controllers installed that way, with
DIFFERENT code, both reported `dev` and therefore compared as identical in the Fleet page and
in the Push/Pull preview. That is precisely the failure `ensure_build` documents having fixed
for the other case: "a label that does not describe what it designates is no better than no
label". Measured on a virgin install on 2026-11-10.

The installer is the ONLY moment that knowledge exists: `get.sh` picked the ref and asked the
API for its SHA. Afterwards nobody can recover it.

★ WHAT IS GUARDED, and why each point is here rather than a reading of the code:
  1. the identity is written, and carries the ref — a fingerprint alone names nothing to a human;
  2. an EXISTING build_info.json is never overwritten (a `build_dist` archive ships one);
  3. nothing is written when `get.sh` said nothing (local source: the tree has its `.git`);
  4. a missing SHA degrades to the ref alone, with `git_hash` at None and NOT the empty string;
  5. `install_controller` really CALLS it, AFTER `_extract` — a function nobody calls proves
     nothing, and called too early it would be overwritten by the extraction;
  6. `get.sh` exports both variables and keeps its `|| true`;
  7. `ensure_build` does NOT rebuild when the local git identity is unreadable — writing a SHA
     while `_git_hash()` returns None would otherwise rebuild the zip on every call.

Non-zero exit = at least one property lost.
"""
import ast
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile

RACINE = pathlib.Path(__file__).resolve().parent.parent
SRC_INSTALL = RACINE / "install" / "install.py"
SRC_GETSH = RACINE / "get.sh"

echecs = []


def v(cond, quoi):
    print(("  ✓ " if cond else "  ✗ ") + quoi)
    if not cond:
        echecs.append(quoi)
    return bool(cond)


def _charger(source_texte=None):
    """Loads install.py FROM SOURCE, optionally mutated. `install_proxmox` sits next to it."""
    texte = source_texte if source_texte is not None else SRC_INSTALL.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as d:
        cible = pathlib.Path(d) / "install.py"
        cible.write_text(texte, encoding="utf-8")
        # The module refuses to load without its sibling; we link the real one.
        os.symlink(RACINE / "install" / "install_proxmox.py",
                   pathlib.Path(d) / "install_proxmox.py")
        spec = importlib.util.spec_from_file_location("inst_essai", cible)
        m = importlib.util.module_from_spec(spec)
        sys.modules["inst_essai"] = m
        anciens = sys.path[:]
        sys.path.insert(0, d)
        try:
            spec.loader.exec_module(m)
        finally:
            sys.path[:] = anciens
            sys.modules.pop("inst_essai", None)
        return m


def _ecrire(mod, ref, sha, deja=None):
    """Runs write_build_info in a throwaway dir → the file's content, or None."""
    with tempfile.TemporaryDirectory() as d:
        if deja is not None:
            (pathlib.Path(d) / "build_info.json").write_text(json.dumps(deja), encoding="utf-8")
        anciens = {k: os.environ.get(k) for k in ("BOBI_SRC_REF", "BOBI_SRC_SHA")}
        for k, val in (("BOBI_SRC_REF", ref), ("BOBI_SRC_SHA", sha)):
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                mod.write_build_info(d)
        finally:
            for k, val in anciens.items():
                if val is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = val
        p = pathlib.Path(d) / "build_info.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _appels_ordonnes(texte, fonction):
    """Names of the calls made inside `fonction`, in source order."""
    arbre = ast.parse(texte)
    for n in ast.walk(arbre):
        if isinstance(n, ast.FunctionDef) and n.name == fonction:
            noms = []
            for sous in ast.walk(n):
                if isinstance(sous, ast.Call):
                    f = sous.func
                    nom = getattr(f, "id", None) or getattr(f, "attr", None)
                    if nom:
                        noms.append((getattr(sous, "lineno", 0), nom))
            return [nom for _l, nom in sorted(noms)]
    return []


# ─── 1. l'identité est écrite, et elle nomme la ref ──────────────────────────
print("\n  1. ce que l'installeur écrit")
M = _charger()
d1 = _ecrire(M, "v0.9.15", "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678")
v(d1 is not None, "un build_info.json est écrit quand get.sh a parlé")
if d1:
    v("v0.9.15" in (d1.get("label") or ""), "le libellé porte la ref demandée")
    v(d1.get("git_hash") == "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
      "git_hash porte le SHA entier, pas sa forme courte")
    v(bool(d1.get("build_id")), "build_id est posé (il distingue deux artefacts)")
    v(bool(d1.get("built_at")), "built_at est posé")
    v("a1b2c3d4e5f6" in (d1.get("label") or ""), "le libellé porte aussi l'empreinte courte")
    v((d1.get("label") or "").strip() != "dev" and "dev" != (d1.get("label") or "")[:3],
      "le libellé n'est plus « dev »")

# ─── 2. on n'écrase jamais ce qui existe ─────────────────────────────────────
print("\n  2. un build_info.json existant est conservé")
avant = {"build_id": "deadbeef", "label": "2026-11-01T10:00:00 · abc123",
         "built_at": "2026-11-01T10:00:00", "git_hash": "abc123"}
d2 = _ecrire(M, "v0.9.15", "ffff", deja=avant)
v(d2 == avant, "l'identité produite par build_dist n'est pas écrasée")

# ─── 3. source locale : on n'écrit rien ──────────────────────────────────────
print("\n  3. sans ref, rien n'est écrit")
v(_ecrire(M, None, None) is None, "pas de BOBI_SRC_REF → aucun fichier (l'arbre a son .git)")
v(_ecrire(M, "", "") is None, "une ref vide vaut une absence de ref")

# ─── 4. dégradation : la ref seule, et git_hash à None ───────────────────────
print("\n  4. empreinte indisponible (quota d'API épuisé, réseau coupé)")
d4 = _ecrire(M, "v0.9.15", None)
v(d4 is not None and "v0.9.15" in (d4.get("label") or ""),
  "la ref seule est écrite — très au-dessus de « dev »")
v(d4 is not None and d4.get("git_hash") is None,
  "git_hash vaut None, JAMAIS la chaîne vide (les lecteurs testent la présence)")

# ─── 5. l'appelant existe, et il appelle au bon moment ───────────────────────
print("\n  5. install_controller appelle, et après _extract")
texte = SRC_INSTALL.read_text(encoding="utf-8")
appels = _appels_ordonnes(texte, "install_controller")
v("write_build_info" in appels, "write_build_info est appelé par install_controller")
if "write_build_info" in appels and "_extract" in appels:
    v(appels.index("_extract") < appels.index("write_build_info"),
      "il est appelé APRÈS _extract (sinon l'extraction l'écraserait)")
else:
    v(False, "il est appelé APRÈS _extract (sinon l'extraction l'écraserait)")

# ─── 6. get.sh transmet, et ne meurt pas sur un quota épuisé ─────────────────
print("\n  6. get.sh transmet ce qu'il sait")
g = SRC_GETSH.read_text(encoding="utf-8")
v('export BOBI_SRC_REF="$REF"' in g, "BOBI_SRC_REF est exporté")
v("BOBI_SRC_SHA" in g and "export BOBI_SRC_SHA" in g, "BOBI_SRC_SHA est exporté")
v("_sha_depot_principal" in g, "le SHA du dépôt principal est demandé")
bloc = g.split("_sha_depot_principal() {", 1)[-1].split("\n}", 1)[0] if "_sha_depot_principal() {" in g else ""
v("|| true" in bloc, "la fonction finit par « || true » (set -e tuerait l'installation sinon)")
i_ref = g.find('export BOBI_SRC_REF')
i_exec = g.find("exec python3 install/install.py")
v(0 < i_ref < i_exec, "les variables sont exportées AVANT le lancement de l'installeur")

# ─── 7. ensure_build ne reconstruit pas sans identité git lisible ────────────
print("\n  7. ensure_build hors dépôt git")
sys.path.insert(0, str(RACINE))
from app import updater as U       # noqa: E402
from app import builder as B       # noqa: E402

def _hash_ok_simule(local, ecrit):
    """Rejoue la décision de ensure_build sans rien construire."""
    _gh, _cbi = B._git_hash, B.current_build_info
    construit = {"n": 0}
    _build = B.build
    B._git_hash = lambda: local
    B.current_build_info = lambda: {"git_hash": ecrit, "build_id": "x", "label": "l"}
    B.build = lambda *a, **k: construit.__setitem__("n", construit["n"] + 1)
    _zip, _mtime = U.ZIP_PATH, U._code_mtime
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            U.ZIP_PATH = f.name
        U._code_mtime = lambda: 0        # le zip est plus récent que le code
        with contextlib.redirect_stdout(io.StringIO()):
            U.ensure_build()
        return construit["n"]
    finally:
        B._git_hash, B.current_build_info, B.build = _gh, _cbi, _build
        try:
            os.unlink(U.ZIP_PATH)
        except OSError:
            pass
        U.ZIP_PATH, U._code_mtime = _zip, _mtime

v(_hash_ok_simule(None, "a1b2c3") == 0,
  "identité locale illisible + SHA écrit → AUCUNE reconstruction")
v(_hash_ok_simule("a1b2c3", "a1b2c3") == 0, "identités égales → aucune reconstruction")
v(_hash_ok_simule("ffffff", "a1b2c3") == 1, "identités différentes → reconstruction (inchangé)")

# ─── 8. les deux langues de l'installeur ─────────────────────────────────────
print("\n  8. messages de l'installeur")
for cle in ("build_kept", "build_written"):
    for lang in ("fr", "en"):
        v(cle in M.STR[lang], "%s présent en %s" % (cle, lang))
v("{l}" in M.STR["fr"]["build_written"] and "{l}" in M.STR["en"]["build_written"],
  "build_written interpole le libellé dans les deux langues")

print()
if echecs:
    print("  ✗ %d propriété(s) perdue(s) :" % len(echecs))
    for e in echecs:
        print("     ·", e)
    sys.exit(1)
print("  ✓ tout est en place")
sys.exit(0)
