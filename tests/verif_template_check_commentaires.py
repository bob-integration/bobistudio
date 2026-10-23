#!/usr/bin/env python3
"""Garde : le contrôle de syntaxe JS des gabarits ignore les COMMENTAIRES HTML.

DÉFAUT D'ORIGINE (relevé le 2026-10-20 par l'utilisateur dans les alertes en cours) : à chaque
démarrage, « Interface : JavaScript invalide dans templates/deploy_palette.html (vers la ligne 2)
— SyntaxError: Unexpected identifier 'template' », en `error`. La page était saine : le mot
« <script> » écrit dans un COMMENTAIRE HTML (« un <script> de template ne s'exécute qu'au
clonage ») ouvrait un faux bloc pour l'expression régulière, qui confiait du français à node.

On exécute le VRAI `app.template_check.verifier` sur des gabarits jetables. Il faut node.
"""
import os
import shutil
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
ECHECS = []


def verifier(cond, libelle):
    print(("  ok   " if cond else "  ÉCHEC ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def main():
    from app import template_check as tc
    if not shutil.which("node"):
        print("node introuvable : garde sautée"); return
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "templates"))
    tpl = lambda n, t: open(os.path.join(d, "templates", n), "w", encoding="utf-8").write(t)
    tpl("sain.html", "<!-- Tables HORS du <template> : un <script> de template ne s'exécute qu'au clonage. -->\n"
                     "<script>\nwindow.A = {{ a | tojson }};\n</script>\n")
    tpl("casse.html", "<!-- un <script> en commentaire -->\n<script>\nconst x = ;\n</script>\n")
    tpl("script_avec_commentaire.html", "<script>\nconst s = '<!-- pas un commentaire HTML ici -->';\nconst y = ;\n</script>\n")
    soucis = {n: (l, m) for n, l, m in tc.verifier(os.path.join(d, "templates"))}
    noms = {os.path.basename(k): v for k, v in soucis.items()}

    print("\n1. « <script> » écrit dans un commentaire HTML")
    verifier("sain.html" not in noms, "★ pas de fausse alerte sur une page saine (%s)" % noms.get("sain.html"))

    print("\n2. Le contrôle n'est pas devenu aveugle")
    verifier("casse.html" in noms and noms["casse.html"][0] == 2,
             "★ une vraie erreur derrière un commentaire est vue, à la bonne ligne (%s)" % (noms.get("casse.html"),))
    verifier("script_avec_commentaire.html" in noms,
             "un script qui contient « <!-- » reste un script, jugé comme tel")
    shutil.rmtree(d, ignore_errors=True)

    print()
    if ECHECS:
        print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  - " + e)
        sys.exit(1)
    print("TOUT PASSE")


if __name__ == "__main__":
    main()
