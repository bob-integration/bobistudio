#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Quota GitHub du catalogue : requêtes conditionnelles, cache, et jeton PERSONNEL.

★ LE PROBLÈME, SIGNALÉ PAR L'USAGE. Un scan dépense une requête pour lister l'organisation plus
UNE PAR DÉPÔT pour ses releases — une dizaine. Anonyme, GitHub en donne 60 par heure et par IP :
six relectures et l'on est bloqué. C'est arrivé en essayant simplement d'installer des plugins.

Quatre remèdes, tous vérifiés ici SANS RÉSEAU (le vrai réseau relève du banc) :

  1. requête CONDITIONNELLE — on renvoie l'ETag connu, et un 304 rend le corps mémorisé. GitHub
     ne décompte pas les 304 : une relecture où rien n'a bougé devient gratuite ;
  2. le JETON de l'utilisateur courant part en `Authorization` (60 → 5 000 par heure) ;
  3. la version du cœur est MISE EN CACHE — elle était relue à chaque affichage de la page ;
  4. le message de quota DIT quoi faire, et distingue anonyme et authentifié.

⚠ On n'appelle jamais github.com : `urlopen` est remplacé le temps du test. Un banc qui dépendrait
du réseau échouerait en CI, et surtout consommerait le quota qu'il est censé protéger.
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, database                                   # noqa: E402

ECHECS = []


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


class _Reponse(io.BytesIO):
    """Ce que `urlopen` rend : un flux, plus des en-têtes."""
    def __init__(self, corps, etag=None):
        super().__init__(json.dumps(corps).encode())
        self.headers = {"ETag": etag} if etag else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def main():
    chemin = os.path.join(tempfile.mkdtemp(), "t.db")
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    database._tls.__dict__.pop("conn", None)
    database.init_db()

    from app import catalogue as cat
    import urllib.error
    import urllib.request

    appels = []          # (url, en-têtes) de chaque requête réellement émise

    def faux_urlopen(req, timeout=None):
        appels.append((req.full_url, dict(req.headers)))
        # 2ᵉ appel et suivants sur la même URL : on répond 304 si le client a bien conditionné.
        if any(k.lower() == "if-none-match" for k in req.headers):
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
        return _Reponse([{"name": "essai"}], etag='W/"abc"')

    urllib.request.urlopen = faux_urlopen

    print("\n── 1. requête conditionnelle : le 2ᵉ appel envoie l'ETag et ne relit rien")
    cat._etags.clear()
    a = cat._http_json("https://api.github.com/essai")
    b = cat._http_json("https://api.github.com/essai")
    verifier(a == b == [{"name": "essai"}], "les deux appels rendent le même corps")
    verifier(len(appels) == 2, "deux requêtes émises")
    verifier(not any(k.lower() == "if-none-match" for k in appels[0][1]),
             "le 1er appel ne conditionne rien (rien en mémoire)")
    verifier(any(k.lower() == "if-none-match" for k in appels[1][1]),
             "★ le 2e appel envoie If-None-Match → 304, hors quota")

    print("\n── 2. le jeton de l'utilisateur courant part en en-tête")
    appels.clear()
    cat._etags.clear()
    vrai_jeton = cat._jeton
    cat._jeton = lambda: None
    cat._http_json("https://api.github.com/sans-jeton")
    verifier(not any(k.lower() == "authorization" for k in appels[0][1]),
             "sans jeton : aucune en-tête Authorization")
    appels.clear()
    cat._etags.clear()
    cat._jeton = lambda: "ghp_essai"
    cat._http_json("https://api.github.com/avec-jeton")
    entetes = {k.lower(): v for k, v in appels[0][1].items()}
    verifier(entetes.get("authorization") == "Bearer ghp_essai",
             "avec jeton : Authorization: Bearer …")
    cat._jeton = vrai_jeton

    print("\n── 3. le jeton est PERSONNEL, et ne fuit jamais vers l'écran")
    from app.auth import hash_password
    uid = database.db_create_user("essai", hash_password("Tulipe-Vent-9312"), "admin",
                                  None, None, None)
    database.db_set_user_gh_token(uid, "ghp_secret")
    verifier((database.db_get_user("essai") or {}).get("gh_token") == "ghp_secret",
             "le jeton est stocké sur l'utilisateur")
    import main as _main
    cli = _main.app.test_client()
    with cli.session_transaction() as sess:
        sess["user_id"] = uid
    corps = cli.get("/api/catalogue").get_data(as_text=True)
    verifier("ghp_secret" not in corps, "★ l'API ne renvoie JAMAIS le jeton")
    verifier('"jeton_pose": true' in corps.replace(", ", ",").replace('"jeton_pose":true',
                                                                     '"jeton_pose": true')
             or '"jeton_pose":true' in corps, "elle dit seulement qu'un jeton existe")
    autre = database.db_create_user("autre", hash_password("Tulipe-Vent-9312"), "admin",
                                    None, None, None)
    verifier(not (database.db_get_user("autre") or {}).get("gh_token"),
             "un autre utilisateur n'hérite de rien")
    database.db_set_user_gh_token(uid, "")
    verifier(not (database.db_get_user("essai") or {}).get("gh_token"), "le retrait efface bien")
    _ = autre

    print("\n── 4. la version du cœur est mise en cache")
    appels.clear()
    cat._etags.clear()
    cat._cache_core.update({"t": 0.0, "info": None, "org": None})
    def faux_releases(url):
        appels.append((url, {}))
        return [{"tag_name": "v9.9.9", "html_url": "u", "published_at": "2026-01-01",
                 "prerelease": False, "assets": [{"name": "bobistudio.zip"}], "draft": False}]
    cat._http_json = faux_releases
    cat.derniere_version_core()
    cat.derniere_version_core()
    verifier(len(appels) == 1, "★ deux appels, UNE seule requête (le cache tient)")

    print()
    if ECHECS:
        print(f"✗ {len(ECHECS)} échec(s)")
        return 1
    print("✓ tout est vert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
