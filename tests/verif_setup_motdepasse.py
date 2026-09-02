#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""L'écran de PREMIER DÉMARRAGE (`/setup`) et son mot de passe.

★ POURQUOI. Le formulaire n'annonçait AUCUNE condition, et un refus renvoyait un formulaire
VIDE : on retapait identifiant, prénom, nom et courriel pour une faute qui ne portait que sur le
mot de passe, sans jamais apprendre la règle. Le pire est que tout existait déjà — la liste de
règles vivante de la page Compte et son miroir navigateur (`static/js/motdepasse.js`) — mais
aucun des deux n'était branché ici.

Ce que ce fichier vérifie, dans les DEUX langues :
  1. les règles sont ANNONCÉES avant la frappe (une ligne par règle, libellé traduit) ;
  2. le miroir navigateur est chargé et reçoit les seuils SERVIS ;
  3. un refus CONSERVE la saisie, et ne renvoie JAMAIS le mot de passe au navigateur ;
  4. le PROFIL d'exigence choisi sur l'écran fait autorité côté SERVEUR — c'est le point qui
     compte : le sélecteur ne doit pas être un décor, et une valeur inconnue ne doit pas
     désactiver le contrôle.

⚠ PIÈGE DE BANC PAYÉ ICI. `database.get_db()` mémorise la connexion en THREAD-LOCAL à la
première utilisation : réassigner `DB_PATH` après le moindre accès n'isole plus rien, et le test
part écrire dans la base de PRODUCTION. D'où `_base_neuve()`, qui remet aussi `_tls.conn` à zéro.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, database                                   # noqa: E402

ECHECS = []


def _base_neuve():
    """Base jetable ET connexion oubliée (cf. l'avertissement de l'en-tête)."""
    chemin = os.path.join(tempfile.mkdtemp(), "t.db")
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    database._tls.__dict__.pop("conn", None)
    database.init_db()


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def main():
    _base_neuve()
    import main as _main                                           # après le DB_PATH
    from app import settings as st

    for lang in ("fr", "en"):
        print(f"\n── /setup en « {lang} »")
        _base_neuve()
        st.set("ui_lang_default", lang)
        cli = _main.app.test_client()
        page = cli.get("/setup").get_data(as_text=True)

        regles = re.findall(r'<li data-regle="([^"]+)"[^>]*>([^<]*)</li>', page)
        from app.auth import PWD_REGLES
        verifier(len(regles) == len(PWD_REGLES), f"{len(PWD_REGLES)} règles annoncées avant la frappe")
        verifier({r for r, _ in regles} == set(PWD_REGLES), "les règles annoncées sont celles du serveur")
        # Un libellé non traduit sortirait sous forme de clé (« compte.pwd_regle_court »).
        verifier(all("pwd_regle" not in txt for _, txt in regles), "libellés traduits (pas des clés)")
        verifier(all("{n}" not in txt for _, txt in regles), "le seuil est substitué, pas laissé en « {n} »")
        verifier("motdepasse.js" in page and "PWD_EXIGENCES" in page, "miroir navigateur chargé + seuils servis")
        verifier('name="pwd_profil"' in page and "PWD_PROFILS" in page, "sélecteur de profil présent")

        # Refus : la saisie survit, le mot de passe ne revient jamais.
        r = cli.post("/setup", data={"username": "cyril", "password": "abc", "password2": "abc",
                                     "prenom": "Cyril", "nom": "Mazouer", "email": "c@x.fr"})
        h = r.get_data(as_text=True)
        verifier(r.status_code == 200 and 'value="cyril"' in h and 'value="Cyril"' in h
                 and 'value="c@x.fr"' in h, "un refus CONSERVE la saisie")
        verifier(">abc<" not in h and 'value="abc"' not in h, "le mot de passe n'est jamais renvoyé")

    # ── Le profil fait-il autorité côté serveur ? « Cerise-8x » : 9 signes, 3 classes.
    print("\n── le profil choisi sur l'écran fait autorité")
    mdp = "Cerise-8x"
    attendu = {"souple": True, "standard": False, "stricte": False,
               # Valeur inconnue / absente → on retombe sur le réglage actif (standard), on ne
               # désactive PAS le contrôle. C'est le cas qui compte : l'écran est PUBLIC.
               "": False, "profil-bidon": False, "../souple": False, None: False,
               # Normalisation de casse assumée.
               "SOUPLE": True}
    for profil, doit_passer in attendu.items():
        _base_neuve()
        cli = _main.app.test_client()
        data = {"username": "essai", "password": mdp, "password2": mdp}
        if profil is not None:
            data["pwd_profil"] = profil
        r = cli.post("/setup", data=data)
        cree = r.status_code == 302 and database.db_count_users() == 1
        verifier(cree == doit_passer,
                 f"pwd_profil={profil!r:16s} → {'accepté' if cree else 'refusé'}"
                 f" (attendu : {'accepté' if doit_passer else 'refusé'})")
        if cree:
            attendu_profil = "souple" if (profil or "").lower() == "souple" else "standard"
            verifier(st.get("pwd_profil") == attendu_profil,
                     f"    et le profil est PERSISTÉ ({attendu_profil})")

    print()
    if ECHECS:
        print(f"✗ {len(ECHECS)} échec(s)")
        return 1
    print("✓ tout est vert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
