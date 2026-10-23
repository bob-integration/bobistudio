#!/usr/bin/env python3
"""Garde : le certificat de la CA interne se télécharge depuis l'interface — lui, et lui seul.

Motif : en mode web tls, chaque poste d'exploitant doit faire confiance à notre CA, sinon le
navigateur avertit à chaque connexion. Jusqu'au 2026-10-11, le seul moyen était de copier
`tls/ca.crt` depuis le contrôleur. Un bouton le sert désormais (Réglages → Système → Sécurité).

Ce qui compte, dans l'ordre : (1) jamais la clé ; (2) le fichier servi EST la CA, dont l'empreinte
correspond à celle affichée (c'est ce que l'exploitant compare) ; (3) un exploitant SANS droit de
réglage y accède, un anonyme non ; (4) le bouton et ses libellés existent sur la page rendue,
dans les deux langues. Base et répertoire TLS jetables.
"""
import hashlib
import os
import re
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

ECHECS = []
MDP = "Mot-de-passe-telechargement-CA-2026!"


def verifier(cond, libelle):
    print(("  ok   " if cond else "  ÉCHEC ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def main():
    chemin_db = os.path.join(tempfile.mkdtemp(), "t.db")
    from app import config, database
    config.DB_PATH = chemin_db
    database.DB_PATH = chemin_db
    # ⚠ Connexion THREAD-LOCAL : sans cette purge, la garde écrirait dans la base de PRODUCTION.
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    from app import ca
    from app.auth import hash_password
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    config.TLS_DIR = tempfile.mkdtemp(prefix="ctrl-")
    database.db_create_user("admin", hash_password(MDP), "admin")
    database.db_create_user("exploitant", hash_password(MDP), "viewer")
    import main as app_main
    app = app_main.app
    app.config["TESTING"] = True

    def client(qui=None):
        c = app.test_client()
        if qui:
            c.post("/login", data={"username": qui, "password": MDP})
        return c

    print("\n1. Sans autorité")
    r = client("admin").get("/api/security/ca.crt")
    verifier(r.status_code == 404, "pas encore de CA → 404 (et pas un fichier vide)")

    ca.create_ca_material(["127.0.0.1"])
    P = ca.paths()
    attendu = open(P["ca_cert"], "rb").read()

    print("\n2. Ce qui est servi")
    r = client("exploitant").get("/api/security/ca.crt")
    corps = r.get_data()
    verifier(r.status_code == 200, "★ un exploitant SANS droit de réglage le télécharge")
    verifier(corps == attendu, "le fichier servi est exactement tls/ca.crt")
    verifier(b"PRIVATE KEY" not in corps and open(P["ca_key"], "rb").read() not in corps,
             "★ aucune clé privée dans la réponse")
    cert = x509.load_pem_x509_certificate(corps)
    fp = cert.fingerprint(hashes.SHA256()).hex()
    affiche = ca.ca_info()["fingerprint_sha256"].replace(":", "")
    verifier(fp == affiche, "son empreinte est celle qu'affiche l'onglet (ce que l'exploitant compare)")
    verifier(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca,
             "c'est bien une AUTORITÉ (CA:TRUE), pas le certificat du contrôleur")
    verifier("attachment" in r.headers.get("Content-Disposition", "")
             and "bobistudio-ca.crt" in r.headers.get("Content-Disposition", ""),
             "servi en pièce jointe, sous un nom parlant")
    verifier(r.mimetype == "application/x-x509-ca-cert",
             "type que les systèmes proposent d'installer comme autorité")
    verifier(hashlib.sha256(corps).hexdigest() != hashlib.sha256(
        open(P["ctrl_cert"], "rb").read()).hexdigest(), "ce n'est pas le certificat du contrôleur")

    print("\n3. Qui y accède")
    r = client().get("/api/security/ca.crt")
    verifier(r.status_code == 401 and b"BEGIN CERTIFICATE" not in r.get_data(),
             "un anonyme est refusé (401), sans le fichier")

    print("\n4. Le bouton, sur la page rendue")
    for lang in ("fr", "en"):
        database.get_db().execute("update users set lang=? where username='admin'", (lang,))
        database.get_db().commit()
        h = client("admin").get("/settings").get_data(as_text=True)
        # Le catalogue JS est servi en FICHIER (/i18n/<lang>.<version>.js, 2026-10-18) : on lit
        # ce que la page charge réellement, page + catalogue.
        m = re.search(r'<script src="(/i18n/[^"]+)"', h)
        verifier(m is not None, "[%s] la page charge son catalogue JS" % lang)
        if m:
            h += client("admin").get(m.group(1)).get_data(as_text=True)
        verifier('href="/api/security/ca.crt"' in h, "[%s] le lien de téléchargement est là" % lang)
        for cle in ("settings.securite.ca_download", "settings.securite.ca_download_help"):
            verifier('"%s"' % cle in h, "[%s] %s est dans le catalogue JS de la page" % (lang, cle))
        libelle = {"fr": "charger le certificat de l", "en": "Download the authority certificate"}[lang]
        verifier(libelle in h,
                 "[%s] … dans la bonne langue (%r)" % (lang, libelle))

    print()
    if ECHECS:
        print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  - " + e)
        sys.exit(1)
    print("TOUT PASSE")


if __name__ == "__main__":
    main()
