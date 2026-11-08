#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""`window.copierTexte` really copies — including from inside a modal <dialog>.

★ WHY. Copying is the one action whose failure is invisible: the page says « copied », the
clipboard holds nothing. It happened twice. First over plain HTTP, where
`navigator.clipboard` does not exist at all (insecure context) and the old code called it
directly. Then on 2026-11-08, in the registration window: the shared helper's fallback
appends a throwaway textarea to `document.body`, but inside a MODAL <dialog> everything
outside the dialog is INERT — the textarea cannot be selected, `execCommand` does nothing,
and the helper still reported success.

The fix gave the helper an optional second argument: a field ALREADY IN THE PAGE to use for
the fallback. This guard holds that contract, which nothing else measures — the only other
test that mentions `copierTexte` replaces it with a stub.

Checked here: the secure path uses the clipboard API; without it, a supplied field is used
and its previous value RESTORED; without a field, a temporary textarea is used; and a
failing `execCommand` returns false instead of claiming success.

Non-zero exit = at least one control failed.
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile

RACINE = pathlib.Path(__file__).resolve().parent.parent
echecs, reussites = [], []


def controle(libelle, ok, detail=""):
    (reussites if ok else echecs).append(libelle)
    print(("  OK    " if ok else "  ÉCHEC ") + libelle + (("\n        → " + detail) if not ok and detail else ""))


def _helper():
    """The helper's source, taken from the template that SERVES it (single source).

    Brace counting rather than a regex: the body holds nested functions and a `catch` block,
    so a non-greedy match would stop at the first `};`.
    """
    src = (RACINE / "templates" / "layout.html").read_text(encoding="utf-8")
    i = src.index("window.copierTexte = function(")
    prof, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        if src[k] == "{":
            prof += 1
        elif src[k] == "}":
            prof -= 1
            if prof == 0:
                return src[i:k + 1] + ";"
    raise AssertionError("bloc non terminé")


STUB = """
global.window = global;
var _journal = [];
global.EXEC_OK = true;
var _ta = null;
global.document = {
  createElement: function(){ _ta = { value: '', style: {}, setAttribute: function(){},
      select: function(){ _journal.push('select:textarea'); },
      setSelectionRange: function(){} }; return _ta; },
  body: { appendChild: function(){ _journal.push('append:body'); },
          removeChild: function(){ _journal.push('remove:body'); } },
  execCommand: function(c){ _journal.push('exec:' + c); return global.EXEC_OK; }
};
"""

CHAMP = """
var champ = { value: 'AVANT', focus: function(){}, select: function(){ _journal.push('select:champ'); } };
"""


def _run(corps, secure, clipboard=True, champ=False, exec_ok=True):
    js = [STUB, _helper()]
    if champ:
        js.append(CHAMP)
    js.append("global.EXEC_OK = %s;\n" % ("true" if exec_ok else "false"))
    js.append("global.isSecureContext = %s;\n" % ("true" if secure else "false"))
    # ★ defineProperty, PAS une affectation : depuis Node 21 `navigator` est un global
    # NATIF en lecture seule, et `global.navigator = ...` y est ignoré en silence. Le bouchon
    # ne s'appliquait donc pas, le helper ne voyait pas de presse-papier et retombait sur le
    # textarea — vert sur le Node 20 de la machine de dev, ROUGE en CI publique (08/11).
    js.append("Object.defineProperty(globalThis, 'navigator', "
              "{ value: %s, configurable: true, writable: true });\n" % (
        "{ clipboard: { writeText: function(t){ _journal.push('clipboard:' + t); "
        "return Promise.resolve(); } } }" if clipboard else "{}"))
    js.append(corps)
    d = tempfile.mkdtemp()
    f = os.path.join(d, "c.js")
    open(f, "w", encoding="utf-8").write("".join(js))
    r = subprocess.run(["node", f], capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


APPEL = ("window.copierTexte('TEXTE'%s).then(function(ok){"
         " console.log(JSON.stringify({ok: ok, j: _journal, champ: %s})); });\n")

rc, out, err = _run(APPEL % ("", "null"), secure=True)
controle("★★ contexte sécurisé : la copie passe par navigator.clipboard",
         rc == 0 and '"clipboard:TEXTE"' in out, err or out)

rc, out, err = _run(APPEL % (", champ", "champ.value"), secure=False, clipboard=False, champ=True)
controle("★★★ hors HTTPS avec un champ : c'est CE champ qui est sélectionné, pas un textarea",
         rc == 0 and '"select:champ"' in out and "select:textarea" not in out, err or out)
controle("★★★ la valeur d'origine du champ est RESTAURÉE (sinon on écrase la saisie)",
         rc == 0 and '"champ":"AVANT"' in out, err or out)
controle("★★ et le résultat est vrai quand execCommand réussit",
         rc == 0 and '"ok":true' in out, err or out)

rc, out, err = _run(APPEL % ("", "null"), secure=False, clipboard=False)
controle("★★ hors HTTPS sans champ : repli sur un textarea temporaire, retiré après",
         rc == 0 and '"select:textarea"' in out and '"remove:body"' in out, err or out)

rc, out, err = _run(APPEL % (", champ", "champ.value"), secure=False, clipboard=False,
                    champ=True, exec_ok=False)
controle("★★★ si execCommand échoue, on renvoie FAUX — jamais « copié » à tort",
         rc == 0 and '"ok":false' in out, err or out)
controle("★★ et le champ est quand même restauré après un échec",
         rc == 0 and '"champ":"AVANT"' in out, err or out)

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
