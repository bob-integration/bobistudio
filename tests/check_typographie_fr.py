#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""French typography of the interface (i18n/fr.json): a NO-BREAK space before « : ; ! ? » and
inside « », never an ordinary one — with an ordinary space the sign can start a line, alone
(« erreurs\n: en cas de doute », seen 2026-11-08). 1,783 values were fixed that day.

Not concerned: code (`…`, <code>…</code>, <pre>…</pre>) and a sign glued to what follows
(`:8080`, `:{port}`, `?x=1`). Usage: check_typographie_fr.py [file] (default i18n/fr.json).
Fix: write U+00A0 (in Python / JSON: \\u00a0) instead of the space.
"""
import json
import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(RACINE, "i18n", "fr.json")
CODE = re.compile(r"(`[^`]*`|<code>.*?</code>|<pre>.*?</pre>)", re.S)
# the same boundary as the fix: a sign followed by a space, the end, a closing sign or a written "\n"
BAD = re.compile(r" [:;!?»](?=$|[\s»\"'’)\].,…<\\])|« ")

values = json.load(open(path, encoding="utf-8"))
bad = []
for k, v in values.items():
    if not isinstance(v, str):
        continue
    text = "".join(CODE.split(v)[0::2])
    m = BAD.search(text)
    if m:
        bad.append((k, text[max(0, m.start() - 30):m.end() + 10]))

print("%d values checked" % len(values))
if bad:
    print("ÉCHEC — ordinary space where a no-break space (U+00A0) is due (%d):" % len(bad))
    for k, ctx in bad[:40]:
        print("  - %s : …%s…" % (k, ctx))
    sys.exit(1)
print("OK — no ordinary space before : ; ! ? » nor after «")
