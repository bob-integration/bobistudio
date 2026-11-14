#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Settings → Nodes: a local helper called outside the function that defines it (2026-11-13).

`rafraichirNodes` defines `const v = (x) => escapeHtml(x || '')`. On 2026-10-31 the GPU line moved
out into `_hdLigneGpu`, still calling `v(...)`: a ReferenceError for any node with a GPU image,
swallowed by the `catch` of `rafraichirNodes` → « Erreur de chargement des nœuds » and no node
list at all, so a node could not even be deleted. Held here: in templates/settings.html, a
top-level function that calls `v(` must define `v` itself.

    ./venv/bin/python tests/verif_settings_helper_hors_portee.py
"""
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(RACINE, "templates", "settings.html"), encoding="utf-8").read()

ko = 0
debuts = [m.start() for m in re.finditer(r"(?m)^(?:async )?function \w+\(", src)]
for i, d in enumerate(debuts):
    corps = src[d:debuts[i + 1] if i + 1 < len(debuts) else len(src)]
    nom = re.match(r"(?:async )?function (\w+)", corps).group(1)
    if re.search(r"(?<![\w.$])v\(", corps) and not re.search(r"\b(?:const|let|var) v\s*=", corps):
        print("  ✗ %s calls v() without defining it" % nom)
        ko += 1
print("  ✓ %d top-level functions checked" % len(debuts) if not ko else "")
if ko:
    print("✗ %d failure(s)" % ko)
    sys.exit(1)
print("✓ all green")
