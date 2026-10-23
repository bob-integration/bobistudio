#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Appairage HA et réplication mTLS.

★ CE QUE CETTE GARDE PROTÈGE. Jusqu'au 2026-10-10 :
  · un standby promu ne pouvait piloter AUCUN nœud : la CA ne lui était jamais transmise, et les
    agents-nœuds (CERT_REQUIRED) le refusaient dans les deux sens ;
  · la base entière — `flask_secret_key` compris — partait en HTTP clair, authentifiée par un
    secret rejouable.

Ce qui est vérifié, sans réseau extérieur ni seconde machine (deux « contrôleurs » simulés dans
deux répertoires TLS jetables, base jetable, écoutes sur des ports éphémères de 127.0.0.1) :
  1. le marqueur de contrôleur : posé, ré-émis sur un ancien certificat, INFALSIFIABLE par un CSR ;
  2. le protocole : le code ne circule jamais ; toute altération, tout rejeu, toute erreur échoue ;
  3. le code côté actif : brûlé au premier succès, après 5 preuves fausses, et à l'expiration ;
  4. l'appairage de bout en bout, à travers la vraie route publique ;
  5. ★ LA BASCULE : un agent-nœud enrôlé par l'actif REFUSE le standby avant appairage et
     l'ACCEPTE après — c'est le défaut d'origine ;
  6. l'écoute mTLS : contrôleur accepté ; nœud, CONTENEUR, sans certificat, CA étrangère refusés ;
     et le client n'envoie rien à un pair qui n'est pas un contrôleur ;
  7. plus de réplication en clair, même avec l'ancien secret correct ;
  8. une réplication complète, de l'instantané au staging, en mTLS.
"""
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ECHECS = []
MDP = "Banc-HA-Appairage-2026!"


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def port_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def ecrire(d, nom, data):
    chemin = os.path.join(d, nom)
    with open(chemin, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    return chemin


def main():
    chemin_db = os.path.join(tempfile.mkdtemp(), "t.db")
    from app import config, database
    config.DB_PATH = chemin_db
    database.DB_PATH = chemin_db
    # ⚠ Connexion THREAD-LOCAL : sans cette purge, le banc écrirait dans la base de PRODUCTION.
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    from app import ca, ha, ha_appairage as hp, ha_tls
    from app import settings as st
    from cryptography import x509

    A = tempfile.mkdtemp(prefix="ctrl-actif-")
    B = tempfile.mkdtemp(prefix="ctrl-standby-")
    config.TLS_DIR = B
    ca.create_ca_material(["127.0.0.1"])
    config.TLS_DIR = A
    ca.create_ca_material(["127.0.0.1"])

    # ── 1. Le marqueur ─────────────────────────────────────────────────────────────────────
    print("\n1. Le marqueur de contrôleur")
    with open(ca.paths()["ctrl_cert"], "rb") as f:
        verifier(ca.est_controleur(f.read()), "une installation neuve naît avec le marqueur")
    # Un ANCIEN certificat (sans marqueur) : on le fabrique comme avant le 2026-10-10.
    from cryptography.hazmat.primitives import serialization
    with open(ca.paths()["ctrl_key"], "rb") as f:
        cle_a = serialization.load_pem_private_key(f.read(), password=None)
    ca_c, ca_k = ca._load_ca()
    vieux = ca._sign(ca._leaf_builder("bobi-controller", cle_a.public_key(), ca_c,
                                      [x509.IPAddress(__import__("ipaddress").ip_address(
                                          "127.0.0.1"))], 365), ca_k)
    ecrire(A, "controller.crt", vieux.public_bytes(serialization.Encoding.PEM))
    verifier(not ca.est_controleur(vieux.public_bytes(serialization.Encoding.PEM)),
             "un certificat d'avant le correctif n'a pas le marqueur")
    verifier(ca.assurer_marqueur_controleur() is True, "il est ré-émis avec le marqueur")
    with open(ca.paths()["ctrl_cert"], "rb") as f:
        neuf = x509.load_pem_x509_certificate(f.read())
    verifier(ca.est_controleur(neuf) and neuf.public_key().public_numbers()
             == cle_a.public_key().public_numbers(),
             "… avec la MÊME clé (rien ne change pour les nœuds, qui ne vérifient que la chaîne)")
    verifier(os.path.exists(ca.paths()["ctrl_cert"] + ".avant-marqueur"),
             "l'ancien certificat est gardé")
    verifier(ca.assurer_marqueur_controleur() is False, "idempotent : pas de seconde ré-émission")
    noeud_crt, noeud_key = ca.generate_leaf("noeud-7", ip="127.0.0.1", node_id=7)
    cont_crt, cont_key = ca.generate_leaf("mxl901", uri="bobi://container/901")
    verifier(not ca.est_controleur(noeud_crt), "un certificat de NŒUD n'est pas un contrôleur")
    verifier(not ca.est_controleur(cont_crt), "un certificat de CONTENEUR n'est pas un contrôleur")
    # Le CN ne prouve rien par lui-même : un CERTIFICAT qui le porte n'est pas un contrôleur s'il n'a
    # pas le marqueur. (Le DEMANDER par CSR est en outre refusé depuis — cf. verif_agent_noeud_role.)
    cert_menteur, _ = ca.generate_leaf("bobi-controller", ip="127.0.0.1", node_id=8)
    verifier("bobi-controller" in x509.load_pem_x509_certificate(cert_menteur).subject
             .rfc4514_string() and not ca.est_controleur(cert_menteur),
             "un certificat de nœud qui PORTE le CN « bobi-controller » n'est pas contrôleur")

    # ── 2. Le protocole ────────────────────────────────────────────────────────────────────
    print("\n2. Le protocole, à nu")
    code = hp.nouveau_code()
    dem, cle_std = hp.preparer_demande(code)
    hp.verifier_demande(code, dem)
    rep = hp.sceller(code, dem["nonce"], {"secret": "la CA"})
    verifier(hp.ouvrir(code, dem["nonce"], rep) == {"secret": "la CA"}, "chemin nominal")
    trafic = json.dumps(dem) + json.dumps(rep)
    verifier(hp.normaliser(code) not in hp.normaliser(trafic) and code not in trafic,
             "le CODE n'apparaît dans AUCUN message échangé")
    verifier("PRIVATE KEY" not in json.dumps(dem),
             "la clé du standby ne quitte pas le standby (seul un CSR part)")

    def leve(f):
        try:
            f()
            return False
        except ValueError:
            return True
    verifier(leve(lambda: hp.verifier_demande(hp.nouveau_code(), dem)),
             "une demande faite avec un AUTRE code est refusée")
    altere = dict(rep, paquet=rep["paquet"][:-6] + ("AAAA==" if rep["paquet"][-6:] != "AAAA==" else "BBBB=="))
    verifier(leve(lambda: hp.ouvrir(code, dem["nonce"], altere)),
             "un paquet altéré est refusé (sa preuve ne correspond plus)")
    verifier(leve(lambda: hp.ouvrir(code, dem["nonce"], dict(rep, preuve="0" * 64))),
             "une réponse sans la preuve de l'actif n'est même pas ouverte")
    verifier(leve(lambda: hp.ouvrir(code, "autre-aleas", rep)),
             "un paquet resservi à une autre demande ne s'ouvre pas")
    verifier(hp.normaliser("abcd efgh-2345") == "ABCDEFGH2345",
             "le code se recopie sans souci de casse, d'espaces ni de tirets")

    # ── 3. Le code côté actif ──────────────────────────────────────────────────────────────
    print("\n3. Le code côté actif")
    hp.annuler()
    try:
        hp.traiter_demande(dem, "127.0.0.1")
        verifier(False, "sans appairage ouvert : refus")
    except PermissionError:
        verifier(True, "sans appairage ouvert : refus")
    code, _ = hp.demarrer()
    dem, cle_std = hp.preparer_demande(code)
    rep = hp.traiter_demande(dem, "127.0.0.1")
    verifier(isinstance(rep, dict) and rep.get("paquet"), "une demande valide est servie")
    try:
        hp.traiter_demande(dem, "127.0.0.1")
        verifier(False, "rejouer la MÊME demande : refus (code brûlé au premier succès)")
    except PermissionError:
        verifier(True, "rejouer la MÊME demande : refus (code brûlé au premier succès)")
    code, _ = hp.demarrer()
    for i in range(hp.ECHECS_MAX):
        mauvaise, _ = hp.preparer_demande(hp.nouveau_code())
        try:
            hp.traiter_demande(mauvaise, "198.51.100.9")
        except (ValueError, PermissionError):
            pass
    bonne, _ = hp.preparer_demande(code)
    try:
        hp.traiter_demande(bonne, "127.0.0.1")
        verifier(False, "après %d preuves fausses, même le BON code est refusé" % hp.ECHECS_MAX)
    except PermissionError:
        verifier(True, "après %d preuves fausses, même le BON code est refusé" % hp.ECHECS_MAX)
    with database.get_db() as db:
        brule = db.execute("SELECT count(*) FROM alerts WHERE msg_key="
                           "'alert.securite.ha_appairage_brule'").fetchone()[0]
    verifier(brule == 1, "et ça entre dans la piste d'audit")
    code, _ = hp.demarrer()
    hp._en_cours["expire"] = time.time() - 1
    try:
        hp.traiter_demande(hp.preparer_demande(code)[0], "127.0.0.1")
        verifier(False, "un code expiré est refusé")
    except PermissionError:
        verifier(True, "un code expiré est refusé")

    # ── 4. De bout en bout, par la vraie route ─────────────────────────────────────────────
    print("\n4. Appairage de bout en bout, par la route publique")
    from app.auth import hash_password
    database.db_create_user("admin", hash_password(MDP), "admin")
    import main as app_main
    app = app_main.app
    app.config["TESTING"] = True
    cli = app.test_client()
    cli.post("/login", data={"username": "admin", "password": MDP})
    config.TLS_DIR = A
    # Précondition RÉALISTE : un actif installé avant le 2026-10-10, donc SANS marqueur. Sans
    # cette remise à l'ancien, la section 1 l'aurait déjà posé et on ne vérifierait pas que
    # l'ouverture d'un appairage le pose elle-même.
    import shutil
    shutil.copy(ca.paths()["ctrl_cert"] + ".avant-marqueur", ca.paths()["ctrl_cert"])
    with open(ca.paths()["ctrl_cert"], "rb") as f:
        verifier(not ca.est_controleur(f.read()), "(précondition : actif ancien, sans marqueur)")
    r = cli.post("/api/ha/pairing/start")
    code = (r.get_json() or {}).get("code_affiche")
    verifier(r.status_code == 200 and code, "l'actif ouvre un appairage et affiche un code")
    with open(ca.paths()["ctrl_cert"], "rb") as f:
        verifier(ca.est_controleur(f.read()),
                 "ouvrir l'appairage a posé le marqueur sur l'actif lui-même")
    with open(os.path.join(B, "ca.crt"), "rb") as f:
        ca_b_avant = f.read()
    capte = {}

    def par_la_route(url, demande):
        capte["demande"] = demande
        rr = cli.post("/api/ha/pairing/claim", json=demande,
                      environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        capte["reponse"] = rr.get_json()
        if rr.status_code != 200:
            raise ValueError("refus %s" % rr.status_code)
        return capte["reponse"]
    empreinte, _ = hp.rejoindre("http://127.0.0.1:5000", code, envoyer=par_la_route, tls_dir=B)
    with open(os.path.join(A, "ca.crt"), "rb") as fa, open(os.path.join(B, "ca.crt"), "rb") as fb:
        verifier(fa.read() == fb.read(), "le standby porte désormais la CA de l'actif")
    with open(os.path.join(B, "controller.crt"), "rb") as f:
        verifier(ca.est_controleur(f.read()), "… et un certificat de contrôleur marqué")
    sauvegardes = [d for d in os.listdir(os.path.dirname(B))
                   if d.startswith(os.path.basename(B) + ".avant-appairage-")]
    verifier(len(sauvegardes) == 1, "l'ancienne autorité du standby est GARDÉE, pas écrasée")
    with open(os.path.join(A, "ca.crt"), "rb") as f:
        ca_a = f.read()
    verifier(empreinte == hp.empreinte_ca(ca_a) and empreinte != hp.empreinte_ca(ca_b_avant),
             "l'empreinte affichée est celle de la CA REÇUE, plus celle d'avant (%s)" % empreinte)
    verifier("PRIVATE KEY" not in json.dumps(capte["demande"]),
             "aucune clé privée dans la DEMANDE")
    verifier("PRIVATE KEY" not in json.dumps(capte["reponse"]),
             "aucune clé privée EN CLAIR dans la réponse (la CA voyage chiffrée)")
    r = cli.post("/api/ha/pairing/claim", json=capte["demande"])
    verifier(r.status_code == 403, "rejouer la demande captée contre la route : refus (%s)"
             % r.status_code)

    print("\n   L'installation refuse un paquet qui ne tient pas")
    with open(os.path.join(A, "ca.crt")) as f:
        ca_a_pem = f.read()
    _, cle_x = hp.preparer_demande(hp.nouveau_code())
    vide = tempfile.mkdtemp()

    def installe(paquet, cle):
        try:
            hp.installer(paquet, cle, tls_dir=vide)
            return "installé"
        except ValueError as e:
            return str(e)
    # Un certificat de NŒUD au lieu d'un certificat de contrôleur.
    r_noeud = installe({"ca_cert": ca_a_pem, "ca_key": "x", "cert": noeud_crt.decode()}, noeud_key)
    verifier("marqueur" in r_noeud, "un certificat sans marqueur de contrôleur : refusé")
    # Un certificat de contrôleur… qui ne correspond pas à la clé gardée ici.
    with open(os.path.join(B, "controller.crt")) as f:
        crt_b = f.read()
    r_cle = installe({"ca_cert": ca_a_pem, "ca_key": "x", "cert": crt_b}, cle_x)
    verifier("clé" in r_cle, "un certificat qui ne correspond pas à NOTRE clé : refusé")
    # Un certificat de contrôleur signé par une AUTRE CA que celle reçue.
    with open(os.path.join(sauve := os.path.join(os.path.dirname(B), sauvegardes[0]),
                           "controller.crt")) as f, \
         open(os.path.join(sauve, "controller.key"), "rb") as k:
        r_ca = installe({"ca_cert": ca_a_pem, "ca_key": "x", "cert": f.read()}, k.read())
    verifier("signé" in r_ca or "marqueur" in r_ca,
             "un certificat qui ne chaîne pas à la CA reçue : refusé (%s)" % r_ca[:50])
    verifier(not os.listdir(vide), "… et rien n'a été écrit dans le répertoire TLS")

    # ── 5. LA BASCULE ──────────────────────────────────────────────────────────────────────
    print("\n5. ★ La bascule : un agent-nœud enrôlé par l'actif face au standby")
    config.TLS_DIR = A
    n_crt, n_key = ca.generate_leaf("noeud-enrole", ip="127.0.0.1", node_id=12)
    dn = tempfile.mkdtemp()
    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.load_cert_chain(ecrire(dn, "n.crt", n_crt), ecrire(dn, "n.key", n_key))
    srv_ctx.load_verify_locations(cafile=os.path.join(A, "ca.crt"))
    srv_ctx.verify_mode = ssl.CERT_REQUIRED          # = node_agent/agent.py:217
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port_noeud = sock.getsockname()[1]

    def agent():
        while True:
            c, _ = sock.accept()
            try:
                with srv_ctx.wrap_socket(c, server_side=True) as s:
                    s.recv(256)
                    s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            except Exception:
                pass
    threading.Thread(target=agent, daemon=True).start()

    def piloter(ca_crt, crt, key):
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_crt)
        ctx.check_hostname = False
        ctx.load_cert_chain(crt, key)
        try:
            with socket.create_connection(("127.0.0.1", port_noeud), timeout=5) as raw:
                with ctx.wrap_socket(raw) as s:
                    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                    return b"200" in s.recv(64)
        except Exception:
            return False
    sauve = os.path.join(os.path.dirname(B), sauvegardes[0])
    verifier(not piloter(os.path.join(sauve, "ca.crt"), os.path.join(sauve, "controller.crt"),
                         os.path.join(sauve, "controller.key")),
             "AVANT l'appairage, le standby promu est REFUSÉ par le nœud (le défaut d'origine)")
    verifier(piloter(os.path.join(B, "ca.crt"), os.path.join(B, "controller.crt"),
                     os.path.join(B, "controller.key")),
             "APRÈS l'appairage, le standby promu PILOTE le nœud")

    # ── 6. L'écoute mTLS ───────────────────────────────────────────────────────────────────
    print("\n6. L'écoute mTLS : contrôleurs seulement")
    config.TLS_DIR = A
    ph = port_libre()
    ha_tls.arreter()
    srv = ha_tls.demarrer(app, hote="127.0.0.1", port=ph)
    verifier(srv is not None, "l'écoute démarre sur un contrôleur appairé (port %d)" % ph)

    def appel(crt=None, key=None, ca_crt=None, chemin="/api/ha/peer"):
        import http.client
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH,
                                         cafile=ca_crt or os.path.join(A, "ca.crt"))
        ctx.check_hostname = False
        if crt:
            ctx.load_cert_chain(crt, key)
        try:
            c = http.client.HTTPSConnection("127.0.0.1", ph, context=ctx, timeout=10)
            c.request("GET", chemin)
            r = c.getresponse()
            return r.status
        except Exception:
            return "poignée de main refusée"
    verifier(appel(os.path.join(B, "controller.crt"), os.path.join(B, "controller.key")) == 200,
             "le standby (contrôleur de la même CA) est accepté")
    verifier(appel(ecrire(dn, "nn.crt", noeud_crt), ecrire(dn, "nn.key", noeud_key)) == 403,
             "un NŒUD de la même CA est refusé")
    verifier(appel(ecrire(dn, "cc.crt", cont_crt), ecrire(dn, "cc.key", cont_key)) == 403,
             "un CONTENEUR de la même CA est refusé — un plugin compromis ne pousse pas de base")
    verifier(appel() == "poignée de main refusée", "sans certificat : refusé dès TLS")
    sauve_p = os.path.join(os.path.dirname(B), sauvegardes[0])
    verifier(appel(os.path.join(sauve_p, "controller.crt"), os.path.join(sauve_p, "controller.key"))
             == "poignée de main refusée", "un contrôleur d'une AUTRE CA : refusé dès TLS")
    verifier(appel(os.path.join(B, "controller.crt"), os.path.join(B, "controller.key"),
                   chemin="/api/containers") == 404,
             "même un contrôleur n'atteint QUE la réplication par cette écoute")

    print("\n   … et le client n'envoie rien à qui n'est pas un contrôleur")
    recu = []
    faux_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    faux_ctx.load_cert_chain(os.path.join(dn, "nn.crt"), os.path.join(dn, "nn.key"))
    fsock = socket.socket()
    fsock.bind(("127.0.0.1", 0))
    fsock.listen(2)
    pf = fsock.getsockname()[1]

    def faux_standby():
        c, _ = fsock.accept()
        try:
            with faux_ctx.wrap_socket(c, server_side=True) as s:
                s.settimeout(3)
                try:
                    recu.append(s.recv(4096))
                except Exception:
                    pass
        except Exception:
            pass
    threading.Thread(target=faux_standby, daemon=True).start()
    # Toute AUTRE issue qu'un refus avant envoi est un échec — y compris une connexion qui tombe
    # APRÈS l'envoi. La première version ne rattrapait que PermissionError : quand le client
    # envoyait quand même (mutation M2), le faux standby coupait, l'exception imprévue faisait
    # planter la garde, et l'écoute restée ouverte la figeait. Détecté, mais par un dépassement
    # de délai qui ne disait rien. Désormais, ça se dit.
    try:
        ha_tls.requete("POST", "/api/ha/replicate", corps=b"SECRET-DE-LA-BASE",
                       hote="127.0.0.1", port=pf, timeout=5)
        issue = "envoyé, et le faux standby a répondu"
    except PermissionError:
        issue = "refusé avant envoi"
    except Exception as e:
        issue = "envoyé puis coupé (%s)" % type(e).__name__
    verifier(issue == "refusé avant envoi",
             "un NŒUD qui se fait passer pour le standby : refusé AVANT envoi (%s)" % issue)
    time.sleep(0.5)
    verifier(not any(b"SECRET" in x for x in recu), "… et il n'a RIEN reçu de la base")

    # ── 7. Plus de clair ───────────────────────────────────────────────────────────────────
    print("\n7. La réplication en clair est retirée")
    st.set("update_token", "ancien-secret")
    r = cli.post("/api/ha/replicate", data=b"SQLite format 3\x00...",
                 headers={"X-MXL-Update-Token": "ancien-secret"})
    verifier(r.status_code == 403, "même avec l'ANCIEN secret correct : refus (%s)" % r.status_code)
    verifier(cli.get("/api/ha/peer", headers={"X-MXL-Update-Token": "ancien-secret"})
             .status_code == 403, "la sonde de pair non plus ne passe plus en clair")

    # ── 8. Une réplication complète ────────────────────────────────────────────────────────
    print("\n8. Une réplication complète, en mTLS")
    st.set("ha_standby_url", "http://127.0.0.1:5000")
    st.set("ha_tls_port", ph)
    for f in (ha.STAGING_DB, ha.STAGING_META):
        try:
            os.remove(f)
        except OSError:
            pass
    ok, msg = ha.push_replica()
    verifier(ok is True, "l'instantané part et arrive (%s)" % msg)
    verifier(os.path.exists(ha.STAGING_DB) and ha._is_valid_sqlite(ha.STAGING_DB),
             "… et il est stagé, SQLite cohérent")
    vivant, raison = ha.probe_peer()
    verifier(vivant is True and "mTLS" in raison, "la sonde de pair répond en mTLS (%s)" % raison)
    ha_tls.arreter()

    print()
    if ECHECS:
        print("ÉCHEC — %d point(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  · " + e)
        return 1
    print("TOUT PASSE")
    return 0


if __name__ == "__main__":
    # L'écoute HA tourne sur des fils de cheroot qui retiennent le processus : si la garde
    # plante en route, sans cet arrêt elle se figerait au lieu de rendre son verdict.
    try:
        code = main()
    finally:
        from app import ha_tls
        ha_tls.arreter()
    sys.exit(code)
