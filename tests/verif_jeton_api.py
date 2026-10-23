#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Jetons d'API — accès programmatique à l'orchestrateur.

★ CE QUE CETTE GARDE PROTÈGE. Un jeton ouvre un accès DURABLE à l'API, sans cookie et sans
expiration. Quatre propriétés doivent tenir, et chacune échoue silencieusement si elle casse :

  1. le secret n'est stocké NULLE PART en clair — une base volée ne donne aucun accès ;
  2. le jeton AGIT COMME SON COMPTE, avec l'habilitation de celui-ci, ni plus ni moins ;
  3. la révocation est IMMÉDIATE, et INDÉPENDANTE des sessions navigateur (fermer ses onglets
     ne coupe pas un serveur MCP ; révoquer un jeton ne déconnecte pas l'humain) ;
  4. le jeton n'apparaît dans AUCUNE ligne de journal ni d'alerte (un jeton d'agent a déjà
     fuité par une alerte interpolant un objet brut — cf. la règle sur les alertes).

Hors ligne, base jetable, aucun réseau : on parle à l'application par son client de test.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ECHECS = []
MDP = "Banc-Jeton-2026!"


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def main():
    rep = tempfile.mkdtemp()
    chemin = os.path.join(rep, "t.db")
    from app import config, database
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    # ⚠ La connexion est THREAD-LOCAL et réutilisée : sans cette purge, surcharger DB_PATH
    # n'isole rien et le banc écrirait dans la base de PRODUCTION.
    database._tls.__dict__.pop("conn", None)
    database.init_db()

    from app.auth import hash_password
    database.db_create_user("admine", hash_password(MDP), "admin")
    database.db_create_user("liseuse", hash_password(MDP), "viewer")
    uid_admin = database.db_get_user("admine")["id"]
    uid_vue = database.db_get_user("liseuse")["id"]

    import main as app_main
    app = app_main.app
    app.config["TESTING"] = True

    # ── 1. Le secret ne se trouve nulle part ────────────────────────────────────────────────
    print("\n1. Le secret n'est stocké qu'en empreinte")
    tid, jeton = database.db_api_token_creer(uid_admin, label="banc", created_by=uid_admin)
    verifier(jeton.startswith("bobi_") and len(jeton) > 30,
             "le jeton est rendu en clair, préfixé et long (%d car.)" % len(jeton))
    with database.get_db() as db:
        lignes = [dict(r) for r in db.execute("SELECT * FROM api_tokens")]
    verifier(all(jeton not in (r.get("token_hash") or "") for r in lignes),
             "la colonne token_hash ne contient PAS le clair")
    verifier(lignes[0]["prefix"] == jeton[:8],
             "seul un préfixe de 8 caractères est lisible, pour l'identifier")
    # Le test qui compte : le clair est-il quelque part dans le FICHIER de base ?
    with open(chemin, "rb") as f:
        brut = f.read()
    verifier(jeton.encode() not in brut,
             "le clair est ABSENT du fichier de base (y compris index et WAL en page)")
    verifier(all("token_hash" not in t for t in database.db_api_tokens_liste()),
             "la liste exposée ne porte même pas l'empreinte")

    # ── 2. Résolution ──────────────────────────────────────────────────────────────────────
    print("\n2. Résolution du porteur")
    u = database.db_api_token_resoudre(jeton)
    verifier(u is not None and u["id"] == uid_admin, "un jeton valide rend son porteur")
    verifier(database.db_api_token_resoudre("bobi_inexistant") is None, "jeton inconnu → None")
    verifier(database.db_api_token_resoudre("pas-un-jeton") is None, "jeton sans préfixe → None")
    verifier(database.db_api_token_resoudre("") is None, "jeton vide → None")
    with database.get_db() as db:
        vu = db.execute("SELECT last_used_at FROM api_tokens WHERE id=?", (tid,)).fetchone()[0]
    verifier(bool(vu), "l'usage est horodaté (last_used_at posé)")

    # ── 3. Le jeton agit comme son compte ──────────────────────────────────────────────────
    print("\n3. Les habilitations du COMPTE, ni plus ni moins")
    _, jeton_vue = database.db_api_token_creer(uid_vue, label="lecture")
    cli = app.test_client()
    r = cli.get("/api/home/summary", headers={"Authorization": "Bearer " + jeton})
    verifier(r.status_code == 200, "jeton admin sur une route de lecture → 200 (%s)" % r.status_code)
    # Le jeton hérite du PÉRIMÈTRE autant que des droits. `/api/home/summary` est gardée par
    # `require_global_access` (topologie du cluster, sans équivalent scopé projet) et le rôle
    # `viewer` ne porte AUCUNE permission : un jeton viewer doit s'y faire refuser. C'est la
    # propriété qui rendra la restriction par projet applicable à un accès programmatique.
    r = cli.get("/api/home/summary", headers={"X-API-Token": jeton_vue})
    verifier(r.status_code == 403,
             "jeton viewer sur une route à accès global → 403 (%s)" % r.status_code)
    verifier((r.get_json() or {}).get("reason") == "global_access_required",
             "et le refus NOMME sa raison, pas un 403 muet")
    # … mais il passe là où le simple login suffit : le jeton authentifie bel et bien.
    r = cli.get("/api/users/me/sessions", headers={"X-API-Token": jeton_vue})
    verifier(r.status_code == 200,
             "jeton viewer sur une route require_login → 200 (%s)" % r.status_code)
    r = cli.get("/api/api-tokens", headers={"Authorization": "Bearer " + jeton_vue})
    verifier(r.status_code == 403,
             "jeton viewer sur une route settings.edit → 403, pas 200 (%s)" % r.status_code)
    r = cli.get("/api/api-tokens", headers={"Authorization": "Bearer " + jeton})
    verifier(r.status_code == 200, "jeton admin sur la même route → 200 (%s)" % r.status_code)
    r = cli.get("/api/home/summary")
    verifier(r.status_code == 401, "sans jeton ni cookie → 401 (%s)" % r.status_code)
    r = cli.get("/api/home/summary", headers={"Authorization": "Bearer bobi_faux"})
    verifier(r.status_code == 401, "jeton faux → 401 (%s)" % r.status_code)

    # ── 4. Révocation ──────────────────────────────────────────────────────────────────────
    print("\n4. Révocation immédiate et idempotente")
    _, jeton_jetable = database.db_api_token_creer(uid_admin, label="jetable")
    tid_jetable = database.db_api_tokens_liste()[0]["id"]
    r = cli.get("/api/home/summary", headers={"Authorization": "Bearer " + jeton_jetable})
    verifier(r.status_code == 200, "avant révocation : 200")
    verifier(database.db_api_token_revoquer(tid_jetable) is True, "la révocation s'applique")
    r = cli.get("/api/home/summary", headers={"Authorization": "Bearer " + jeton_jetable})
    verifier(r.status_code == 401, "après révocation : 401 (%s)" % r.status_code)
    verifier(database.db_api_token_revoquer(tid_jetable) is False,
             "re-révoquer rend False (la date du premier coup reste la vérité)")
    avant = [t for t in database.db_api_tokens_liste() if t["id"] == tid_jetable][0]
    verifier(bool(avant["revoked_at"]), "le jeton révoqué reste LISTABLE, daté")

    # ── 5. Jetons et sessions sont indépendants ────────────────────────────────────────────
    print("\n5. Jetons et sessions navigateur : cycles séparés")
    nav = app.test_client()
    r = nav.post("/login", data={"username": "admine", "password": MDP})
    verifier(r.status_code in (200, 302), "l'humain se connecte au navigateur")
    r = nav.delete("/api/users/me/sessions/autres")
    verifier(r.status_code in (200, 204),
             "« fermer mes autres sessions » aboutit (%s)" % r.status_code)
    r = cli.get("/api/home/summary", headers={"Authorization": "Bearer " + jeton})
    verifier(r.status_code == 200,
             "le jeton SURVIT à la fermeture des autres sessions (%s)" % r.status_code)
    r = nav.get("/api/home/summary")
    verifier(r.status_code == 200, "et l'humain reste connecté (%s)" % r.status_code)

    # ── 6. Le cookie primé sur le jeton ────────────────────────────────────────────────────
    print("\n6. Devant un navigateur, le cookie décide")
    # Un outil pourrait poser un en-tête dans une requête de navigateur : l'humain ne doit pas
    # se retrouver à agir sous une autre identité que la sienne.
    r = nav.get("/api/api-tokens", headers={"Authorization": "Bearer " + jeton_vue})
    verifier(r.status_code == 200,
             "session admin + en-tête viewer → les droits de la SESSION (%s)" % r.status_code)

    # ── 7. Aucun secret dans le journal ────────────────────────────────────────────────────
    print("\n7. Le secret n'entre pas dans le journal")
    r = cli.post("/api/api-tokens", json={"user_id": uid_vue, "label": "créé par l'API"},
                 headers={"Authorization": "Bearer " + jeton})
    verifier(r.status_code == 200, "la route de création répond 200 (%s)" % r.status_code)
    neuf = (r.get_json() or {}).get("token") or ""
    verifier(neuf.startswith("bobi_"), "elle rend le clair UNE fois")
    liste = cli.get("/api/api-tokens", headers={"Authorization": "Bearer " + jeton}).get_data(True)
    verifier(neuf not in liste, "la liste ne le rend PLUS jamais")
    with database.get_db() as db:
        alertes = [dict(r2) for r2 in db.execute("SELECT * FROM alerts")]
    tout = json.dumps(alertes, ensure_ascii=False)
    verifier(neuf not in tout and jeton not in tout,
             "aucune alerte ne contient un jeton (%d lignes examinées)" % len(alertes))
    cles = {a.get("msg_key") for a in alertes}
    verifier("alert.securite.api_token_cree" in cles,
             "la création EST journalisée (sans le secret)")
    with open(chemin, "rb") as f:
        verifier(neuf.encode() not in f.read(), "ni le fichier de base")

    # ── 8. Les clés d'alerte existent dans les deux langues ────────────────────────────────
    print("\n8. Les clés émises sont traduites")
    from app import i18n
    for cle in ("alert.securite.api_token_cree", "alert.securite.api_token_revoque"):
        p = {"label": "x", "identifiant": "y", "par": "z"}
        fr, en = i18n.t(cle, "fr", **p), i18n.t(cle, "en", **p)
        verifier(fr != cle and en != cle and fr != en,
                 "%s : rendue dans les deux langues, différemment" % cle.split(".")[-1])

    print()
    if ECHECS:
        print("ÉCHEC — %d point(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  · " + e)
        return 1
    print("TOUT PASSE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
