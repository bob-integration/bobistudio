#!/usr/bin/env python3
"""Le catalogue JS (`i18n.js_catalog`) est mémoïsé — vérifie qu'il ne SERT JAMAIS une version périmée.

Pourquoi : le context processor l'appelle à chaque rendu de gabarit ; le calcul (10 856 clés,
753 Ko, mesuré le 2026-10-18) rendait les pages lentes sur un orchestrateur chargé. Le risque
d'un cache, c'est l'échec silencieux : une traduction corrigée dans l'éditeur qui ne s'affiche
pas, sans erreur nulle part. Base jetable (DB_PATH posé AVANT tout accès).
"""
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

from app import config, database                  # noqa: E402

_base = os.path.join(tempfile.mkdtemp(), "t.db")
config.DB_PATH = _base
database.DB_PATH = _base
database._tls.__dict__.pop("conn", None)
database.init_db()

from app import i18n                              # noqa: E402

FAILS = []


def check(cond, label):
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        FAILS.append(label)


def main():
    i18n.reload()
    a = i18n.js_catalog("fr")
    check(i18n.js_catalog("fr") is a, "deuxième appel : servi du cache (même objet)")
    ref = {k: i18n.t(k, "fr") for k in i18n._all_keys()
           if any(k.startswith(p) for p in i18n.js_catalog.__defaults__[1])}
    check(a == ref, "contenu identique au calcul d'origine (%d clés)" % len(ref))
    key = next(k for k in a if k.startswith("js."))
    i18n.set_override("fr", key, "VALEUR-SURCHARGEE")
    b = i18n.js_catalog("fr")
    check(b.get(key) == "VALEUR-SURCHARGEE", "surcharge de l'éditeur : visible au rendu suivant")
    check(i18n.js_catalog("en") is not b, "une langue n'est pas servie pour une autre")
    sub = i18n.js_catalog("fr", prefixes=("cables.",))
    check(sub and all(k.startswith("cables.") for k in sub), "préfixes différents : entrée de cache distincte")
    # Fichier /i18n/<lang>.<version>.js : gardé un an par le navigateur, donc la version DOIT
    # changer avec le contenu, sinon une traduction corrigée ne s'afficherait jamais.
    body1, v1 = i18n.js_catalog_script("fr")
    check(b"VALEUR-SURCHARGEE" in body1, "fichier JS : contient la surcharge")
    i18n.set_override("fr", key, None)
    check(i18n.js_catalog("fr").get(key) != "VALEUR-SURCHARGEE", "surcharge retirée : plus servie")
    body2, v2 = i18n.js_catalog_script("fr")
    check(v2 != v1 and b"VALEUR-SURCHARGEE" not in body2, "fichier JS : nouvelle version (%s → %s) dès que le contenu change" % (v1, v2))
    check(i18n.js_catalog_script("fr")[1] == v2, "fichier JS : version stable tant que rien ne change")
    print()
    if FAILS:
        print("✗ %d échec(s)" % len(FAILS))
        return 1
    print("✓ catalogue JS mémoïsé : jamais périmé")
    return 0


if __name__ == "__main__":
    sys.exit(main())
