#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Le neutraliseur Jinja de `app/template_check.py` ne doit ni MENTIR ni se TAIRE.

Pourquoi cet outil. Le 2026-08-29, ce contrôle a crié pendant des heures, en `error` — « les
fonctions de cette page ne s'exécutent plus » — sur une page Réglages parfaitement saine. Le
gabarit était juste, le rendu aussi ; c'est le neutraliseur qui remplaçait `{{ … }}` par `"x"`,
guillemets compris, transformant `mxlToast("{{ _('k') }}", 'ok')` en `mxlToast(""x"", 'ok')`.
Une alerte de niveau `error` qui hurle à tort apprend à ignorer la catégorie entière.

Le correctif (substitut IDENTIFIANT et non CHAÎNE) rend le contrôle plus permissif d'un cran :
il fallait donc prouver qu'il n'est pas devenu aveugle. C'est l'objet des cas `ko_*`, dont
`ko_backtick` — le défaut historique qui a motivé la création du module.

    ./venv/bin/python tools/verif_neutraliseur_jinja.py

Code de retour 1 si un cas se comporte autrement qu'annoncé.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import template_check as tc            # noqa: E402

# (nom, source, doit_etre_signale)
CAS = [
    # ── SAINS : tout ceci se rend correctement dans le navigateur ────────────────────────────
    ("ok_toast", """<script>
       function a() { mxlToast("{{ _('settings.public.saved') }}", 'success'); }
     </script>""", False),
    ("ok_interpolation_partielle", """<script>
       const s = "début {{ v }} fin"; const n = {{ compte }}; f({{ arg }});
     </script>""", False),
    ("ok_apres_point", """<script>
       const v = obj.{{ champ }}; const o = { {{ cle }}: 1 };
     </script>""", False),
    ("ok_apostrophe_francaise", """<script>
       // l'onglet n'existe pas, et c'est normal — {{ _('x') }}
       const t = `${T('{{ cle }}')}`;
     </script>""", False),
    # ── CASSÉS : le contrôle doit continuer à les voir ───────────────────────────────────────
    ("ko_backtick_dans_commentaire", """<script>
       function h(n) { return `<div>
         <!-- le champ `nom` vient d'ailleurs -->
         ${n}</div>`; }
     </script>""", True),
    ("ko_parenthese", """<script>
       function b() { console.log("{{ _('k') }}" ; }
     </script>""", True),
    ("ko_accolade", """<script>
       function c() { if (a) { return 1; }
     </script>""", True),
]


def main():
    if not tc._node_dispo():
        print("node introuvable — contrôle sauté (comme le fait template_check lui-même)")
        return 0
    dossier = tempfile.mkdtemp(prefix="verif-neutraliseur-")
    for nom, src, _ in CAS:
        with open(os.path.join(dossier, nom + ".html"), "w", encoding="utf-8") as f:
            f.write(src)
    detail = {os.path.basename(g): m for g, _l, m in tc.verifier(dossier)}
    signales = set(detail)

    ok = True
    for nom, _src, doit in CAS:
        vu = (nom + ".html") in signales
        if vu != doit:
            ok = False
        print("%-6s %-30s attendu=%-7s signalé=%-5s %s"
              % ("OK" if vu == doit else "✕ RATÉ", nom,
                 "défaut" if doit else "sain", vu, detail.get(nom + ".html", "")[:50]))
    for nom, _s, _d in CAS:
        try:
            os.unlink(os.path.join(dossier, nom + ".html"))
        except OSError:
            pass
    try:
        os.rmdir(dossier)
    except OSError:
        pass
    print("\n" + ("✓ le neutraliseur ne ment pas, et n'est pas devenu aveugle" if ok
                  else "✕ RÉGRESSION du neutraliseur Jinja"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
