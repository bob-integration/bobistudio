#!/usr/bin/env python3
"""Garde : l'échéance des certificats du plan de contrôle se voit venir, et se repousse.

DÉFAUT D'ORIGINE (relevé le 2026-10-11) : rien ne renouvelait ces certificats et rien ne
prévenait — contrôleur au 2028-10-06, nœuds en novembre 2028. Ce jour-là, plus aucun nœud
pilotable, sans un mot avant.

Demande de l'exploitation : une alerte à J-30, une à J-10, puis EN CONTINU à partir de J-5.

Sections :
  1. la règle des paliers, à nu ;
  2. la séquence d'alertes complète d'un nœud, de J-40 à après l'échéance, horloge simulée ;
  3. le contrôleur : alerte, puis renouvellement (même clé, adresses gardées, écoute rechargée) ;
  4. ★ un nœud dont le certificat a DÉJÀ expiré : le contrôleur ne peut plus lui parler, et le
     renouvellement passe quand même — vrai agent-nœud servi en mTLS sur la boucle locale ;
  5. l'onglet Sécurité, rendu dans les deux langues.
Base, répertoires TLS et agent jetables ; aucun réseau hors loopback.
"""
import datetime
import importlib.util
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

ECHECS = []
MDP = "Mot-de-passe-echeance-2026!"
JETON = "jeton-noeud-echeance-0123456789abcdef"
J = datetime.timedelta(days=1)
H = datetime.timedelta(hours=1)


def verifier(cond, libelle):
    print(("  ok   " if cond else "  ÉCHEC ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def port_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    chemin_db = os.path.join(tempfile.mkdtemp(), "t.db")
    from app import config, database
    config.DB_PATH = chemin_db
    database.DB_PATH = chemin_db
    # ⚠ Connexion THREAD-LOCAL : sans cette purge, la garde écrirait dans la base de PRODUCTION.
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    from app import ca, cert_expiry as ce, i18n, node_driver
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    import ipaddress

    config.TLS_DIR = tempfile.mkdtemp(prefix="ctrl-")
    ca.create_ca_material(["127.0.0.1"])
    P = ca.paths()
    ca_c, ca_k = ca._load_ca()
    maintenant = datetime.datetime.now(datetime.timezone.utc)

    def cert_noeud(nid, debut, fin, cle=None):
        """Certificat de nœud aux dates CHOISIES (y compris déjà expiré)."""
        cle = cle or ec.generate_private_key(ec.SECP256R1())
        c = (x509.CertificateBuilder()
             .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "noeud-%d" % nid)]))
             .issuer_name(ca_c.subject).public_key(cle.public_key())
             .serial_number(x509.random_serial_number())
             .not_valid_before(debut).not_valid_after(fin)
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
             # AKI/SKI : exigés par le mode strict d'OpenSSL (`Missing Authority Key Identifier`).
             # Sans eux, la vérification échoue pour une raison qui n'est PAS la date — et la
             # garde éprouverait autre chose que ce qu'elle annonce.
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_k.public_key()),
                            critical=False)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(cle.public_key()),
                            critical=False)
             .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                   ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
             .add_extension(x509.SubjectAlternativeName([
                 x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                 x509.UniformResourceIdentifier("bobi://node/%d" % nid)]), critical=False)
             .sign(ca_k, hashes.SHA256()))
        return c.public_bytes(serialization.Encoding.PEM), cle

    def alertes(prefixe):
        return database.get_db().execute(
            "SELECT msg_key, niveau, node_id, kind FROM alerts WHERE msg_key LIKE ? ORDER BY id",
            (prefixe + "%",)).fetchall()

    # ── 1. La règle ──────────────────────────────────────────────────────────────────────────
    print("\n1. Paliers")
    for jours, attendu in ((40, None), (31, None), (30, 30), (11, 30), (10, 10), (6, 10),
                           (5, 5), (0, 5), (-1, 0), (-400, 0)):
        verifier(ce.palier(jours) == attendu, "J%+d → %r" % (-jours, attendu))
    fin = maintenant + 30 * J
    verifier(ce.jours_restants(fin, maintenant + H) == 29,
             "29 j 23 h restants comptent pour J-29 (on alerte trop tôt plutôt que trop tard)")

    # ── 2. La séquence d'un nœud ─────────────────────────────────────────────────────────────
    print("\n2. Séquence d'alertes d'un nœud (horloge simulée)")
    nid = database.db_add_node("dl360-test", "127.0.0.1")
    fin = maintenant + 60 * J
    pem, _k = cert_noeud(nid, maintenant - J, fin)
    # L'échéance RÉELLE : un certificat tronque les microsecondes. Viser des bornes exactes avec la
    # date non tronquée faisait tomber J-11 sur J-10 (le premier jet de cette garde s'y est pris).
    fin = ca.fin_validite(pem)
    database.db_update_node(nid, tls_ready=1, node_cert=pem.decode())
    t0 = 1_000_000.0

    def passe(delta_avant_fin, horloge):
        return [p for c, p in ce.verifier(maintenant=fin - delta_avant_fin, horloge=horloge)
                if c == "noeud:%d" % nid]

    verifier(passe(40 * J, t0) == [], "J-40 : rien")
    verifier(passe(30 * J - H, t0 + 1) == [30], "J-30 : une alerte")
    verifier(passe(30 * J - 2 * H, t0 + 2) == [], "… pas deux le même jour")
    verifier(passe(11 * J, t0 + 3) == [], "J-11 : rien de plus")
    verifier(passe(10 * J - H, t0 + 4) == [10], "J-10 : une alerte")
    verifier(passe(6 * J, t0 + 5) == [], "J-6 : rien de plus")
    verifier(passe(5 * J - H, t0 + 6) == [5], "J-5 : alerte, et le mode continu commence")
    verifier(passe(5 * J - 2 * H, t0 + 6 + 1800) == [], "… pas avant une heure")
    verifier(passe(5 * J - 3 * H, t0 + 6 + 3601) == [5], "★ … répétée au bout d'une heure")
    verifier(passe(2 * J, t0 + 6 + 7300) == [5], "★ … et encore à J-2")
    verifier(passe(-H, t0 + 6 + 7301) == [0], "★ échéance passée : alerte d'expiration immédiate")
    verifier(passe(-2 * H, t0 + 6 + 7302) == [], "… pas avant une heure")
    verifier(passe(-3 * H, t0 + 6 + 7302 + 3600) == [0], "★ … puis répétée tant que rien n'est fait")
    # Remplacé par un AUTRE certificat, lui-même proche de l'échéance (ré-enrôlement à la hâte,
    # autorité réémise…) : c'est un nouveau certificat, il a droit à SES alertes. Sans remise à
    # zéro par numéro de série, le palier « expiré » de l'ancien les masquait jusqu'à J-5.
    pem_court, _ = cert_noeud(nid, maintenant - J, fin + 20 * J)
    database.db_update_node(nid, tls_ready=1, node_cert=pem_court.decode())
    verifier(passe(-3 * H, t0 + 6 + 7302 + 3601) == [30],
             "★ remplacé par un certificat à J-20 : il reçoit son alerte J-30 tout de suite")
    lignes = alertes("alert.securite.cert_noeud")
    verifier([r["msg_key"] for r in lignes] == ["alert.securite.cert_noeud_expire_bientot"] * 5
             + ["alert.securite.cert_noeud_expire"] * 2
             + ["alert.securite.cert_noeud_expire_bientot"],
             "les clés : 5 « expire bientôt », 2 « expiré », puis celle du certificat remplaçant")
    verifier([r["niveau"] for r in lignes] == ["warning", "warning"] + ["error"] * 5 + ["warning"],
             "niveaux : avertissement à J-30 et J-10, erreur à partir de J-5")
    verifier(all(r["node_id"] == nid and r["kind"] == "node" for r in lignes),
             "rattachées au nœud (node_id, kind=node)")
    # Un certificat RENOUVELÉ est un autre certificat : ses paliers repartent de zéro.
    pem2, _ = cert_noeud(nid, maintenant - J, fin + 400 * J)
    database.db_update_node(nid, tls_ready=1, node_cert=pem2.decode())
    verifier(passe(-4 * H, t0 + 99999) == [], "renouvelé : plus aucune alerte")
    etat = database.db_get_setting(ce.CLE_ETAT)
    verifier(etat["noeud:%d" % nid].get("palier") is None, "… et son état repart de zéro")
    # Des alertes pas de répétition avalée : l'anti-rebond général est porté au-delà d'une heure.
    database.db_set_setting("alerts_antirebond_s", 7200)
    pem3, _ = cert_noeud(nid, maintenant - J, fin)
    database.db_update_node(nid, tls_ready=1, node_cert=pem3.decode())
    n_avant = len(alertes("alert.securite.cert_noeud"))
    passe(3 * J, t0 + 200000)
    passe(3 * J - 2 * H, t0 + 200000 + 3601)
    verifier(len(alertes("alert.securite.cert_noeud")) - n_avant == 2,
             "★ la répétition horaire n'est pas avalée par un anti-rebond réglé à 2 h")
    database.db_set_setting("alerts_antirebond_s", 900)
    for cle in ("alert.securite.cert_noeud_expire_bientot", "alert.securite.cert_noeud_expire",
                "alert.securite.cert_controleur_expire_bientot",
                "alert.securite.cert_controleur_expire", "alert.securite.cert_ca_expire_bientot",
                "alert.securite.cert_ca_expire", "alert.securite.cert_controleur_renouvele",
                "alert.node.mtls_renouvele"):
        for lang in ("fr", "en"):
            txt = i18n.t(cle, lang, n="dl360-1", j=7, date="2028-10-06", par="admin")
            verifier(txt != cle and "{" not in txt, "[%s] %s se rend" % (lang, cle))

    # ── 3. Le contrôleur ─────────────────────────────────────────────────────────────────────
    print("\n3. Le contrôleur : alerte, puis renouvellement")
    database.db_update_node(nid, tls_ready=1, node_cert=pem2.decode())    # nœud hors du jeu
    cle_ctrl = serialization.load_pem_private_key(open(P["ctrl_key"], "rb").read(), None)
    sans_avant = open(P["ctrl_cert"], "rb").read()
    san = x509.load_pem_x509_certificate(sans_avant).extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value
    court = ca._sign(ca._leaf_builder("bobi-controller", cle_ctrl.public_key(), ca_c,
                                      list(san), 8), ca_k)
    with open(P["ctrl_cert"], "wb") as f:
        f.write(court.public_bytes(serialization.Encoding.PEM))
    emis = ce.verifier(horloge=t0 + 500000)
    verifier(("controleur", 10) in emis, "certificat du contrôleur à J-8 → alerte (palier J-10)")
    ligne = alertes("alert.securite.cert_controleur")[-1]
    verifier(ligne["kind"] == "auth" and ligne["node_id"] is None,
             "… dans la piste d'audit (kind=auth), sans nœud")

    from app import web_mode
    port_web = port_libre()
    from app import settings as st
    st.set("web_tls_port", port_web)
    st.set("vip_address", "192.0.2.250/24")
    web_mode.fixer("tls")

    def app_wsgi(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    srv = web_mode.demarrer_https(app_wsgi, hote="127.0.0.1")
    verifier(srv is not None, "(précondition : écoute HTTPS démarrée)")

    def fin_servie():
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=P["ca_cert"])
        ctx.check_hostname = False
        with socket.create_connection(("127.0.0.1", port_web), timeout=5) as s, \
                ctx.wrap_socket(s) as t:
            return x509.load_der_x509_certificate(t.getpeercert(True)).not_valid_after_utc

    try:
        verifier((fin_servie() - maintenant).days <= 8, "(précondition : l'écoute sert le certificat court)")
        fin_neuve = ce.renouveler_controleur()
        neuf = x509.load_pem_x509_certificate(open(P["ctrl_cert"], "rb").read())
        verifier((neuf.not_valid_after_utc - maintenant).days >= 800,
                 "★ renouvelé : durée de vie neuve (~825 j)")
        verifier(neuf.public_key().public_numbers() == cle_ctrl.public_key().public_numbers(),
                 "… même clé")
        verifier(ca.est_controleur(neuf) and "192.0.2.250" in [
            str(v) for v in neuf.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)],
            "… marqueur et IP virtuelle gardés")
        verifier(fin_neuve[:10] == neuf.not_valid_after_utc.date().isoformat(), "… échéance rendue")
        verifier((fin_servie() - maintenant).days >= 800,
                 "★ l'écoute HTTPS sert le certificat renouvelé, sans redémarrage")
    finally:
        srv.stop()
        web_mode._ctx_https = None
    verifier(ce.verifier(horloge=t0 + 600000) == [], "renouvelé : plus d'alerte pour le contrôleur")

    # ── 4. ★ Un nœud au certificat DÉJÀ expiré ───────────────────────────────────────────────
    print("\n4. Renouveler un nœud dont le certificat a DÉJÀ expiré (vrai agent-nœud)")
    tls_noeud = tempfile.mkdtemp(prefix="noeud-tls-")
    nid2 = database.db_add_node("noeud-expire", "127.0.0.1")
    pem_exp, cle_exp = cert_noeud(nid2, maintenant - 900 * J, maintenant - 2 * J)
    with open(os.path.join(tls_noeud, "node.crt"), "wb") as f:
        f.write(pem_exp)
    with open(os.path.join(tls_noeud, "node.key"), "wb") as f:
        f.write(cle_exp.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    with open(os.path.join(tls_noeud, "ca.crt"), "wb") as f:
        f.write(ca.ca_cert_pem())
    cfg = os.path.join(tempfile.mkdtemp(), "config.json")
    json.dump({"token": JETON, "tls_dir": tls_noeud, "capabilities": []}, open(cfg, "w"))
    os.environ["BOBI_NODE_AGENT_CONFIG"] = cfg
    spec = importlib.util.spec_from_file_location("bobi_node_agent_echeance",
                                                  os.path.join(RACINE, "node_agent", "agent.py"))
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    agent._schedule_restart = lambda *a, **k: None   # le vrai ré-exec remplacerait CE processus
    port = port_libre()
    httpd = agent.ThreadingHTTPServer(("127.0.0.1", port), agent.Handler)
    httpd.socket = agent._make_server_ssl_context().wrap_socket(httpd.socket, server_side=True)
    httpd.handle_error = lambda *a: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    database.db_update_node(nid2, agent_url="http://127.0.0.1:%d" % port, agent_token=JETON,
                            tls_ready=1, node_cert=pem_exp.decode())
    try:
        noeud = database.db_get_node(nid2)
        ok, err = node_driver._call(noeud, "GET", "/v1/health", timeout=10)
        verifier(not ok, "(le défaut : le contrôleur refuse un nœud au certificat expiré)")
        # Le DIAGNOSTIC : il disait « signé par une AUTRE autorité » et envoyait ré-enrôler un nœud
        # qu'il suffit de renouveler — le remède opposé, le jour même de l'échéance.
        verifier("EXPIRÉ" in str(err) and "AUTRE" not in str(err) and "Renouveler" in str(err),
                 "★ le message dit « expiré » et renvoie au renouvellement, pas à « une autre autorité »")
        verifier(node_driver.cert_expire(noeud), "le nœud est reconnu comme expiré")
        ok, msg = node_driver.rotate_tls(noeud)
        verifier(ok and msg == "renouvelé", "★ le renouvellement passe quand même (%s)" % msg)
        apres = database.db_get_node(nid2)
        fin2 = ca.fin_validite(apres["node_cert"])
        verifier((fin2 - maintenant).days >= 800, "★ nouvelle échéance en base (~825 j)")
        recu = open(os.path.join(tls_noeud, "node.crt"), "rb").read()
        verifier((ca.fin_validite(recu) - maintenant).days >= 800 and ca.fin_validite(recu) == fin2,
                 "… et ce nouveau certificat est posé sur le nœud")
        verifier(x509.load_pem_x509_certificate(recu).public_key().public_numbers()
                 == cle_exp.public_key().public_numbers(), "… pour la MÊME clé (gardée par l'agent)")
        verifier(alertes("alert.node.mtls_renouvele") and not alertes("alert.node.mtls_migre"),
                 "dit « renouvelé », pas « migré en HTTPS »")
        # La tolérance aux dates ne vaut QUE pour le renouvellement.
        ok, _e = node_driver._call(dict(noeud, node_cert=apres["node_cert"]), "GET", "/v1/health",
                                   timeout=10, bypass_breaker=True)
        verifier(not ok, "un appel ordinaire reste refusé tant que l'agent sert l'ancien certificat")

        # L'inverse : c'est le certificat du CONTRÔLEUR qui a expiré, et le nœud qui le rejette.
        # (La date du certificat du nœud est tolérée ici, pour isoler le refus du côté nœud.)
        sauve = open(P["ctrl_cert"], "rb").read()
        perime = (x509.CertificateBuilder()
                  .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bobi-controller")]))
                  .issuer_name(ca_c.subject).public_key(cle_ctrl.public_key())
                  .serial_number(x509.random_serial_number())
                  .not_valid_before(maintenant - 900 * J).not_valid_after(maintenant - J)
                  .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                  .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                      ca_k.public_key()), critical=False)
                  .add_extension(x509.SubjectKeyIdentifier.from_public_key(cle_ctrl.public_key()),
                                 critical=False)
                  .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                        ExtendedKeyUsageOID.CLIENT_AUTH]),
                                 critical=False)
                  .add_extension(x509.SubjectAlternativeName([
                      x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                      x509.UniformResourceIdentifier(ca.MARQUEUR_CONTROLEUR + "127.0.0.1")]),
                      critical=False)
                  .sign(ca_k, hashes.SHA256()))
        with open(P["ctrl_cert"], "wb") as f:
            f.write(perime.public_bytes(serialization.Encoding.PEM))
        try:
            ok, err = node_driver._call(dict(noeud, node_cert=pem_exp.decode()), "GET",
                                        "/v1/health", timeout=10, bypass_breaker=True,
                                        tolerer_expiration=True)
            verifier(not ok and "CONTRÔLEUR" in str(err) and "AUTRE" not in str(err),
                     "★ contrôleur expiré : le message le dit (%s)" % str(err)[:50])
        finally:
            with open(P["ctrl_cert"], "wb") as f:
                f.write(sauve)
    finally:
        httpd.shutdown()

    # ── 5. L'onglet ──────────────────────────────────────────────────────────────────────────
    print("\n5. Réglages → Système → Sécurité")
    from app.auth import hash_password
    database.db_create_user("admin", hash_password(MDP), "admin")
    import main as app_main
    app = app_main.app
    app.config["TESTING"] = True
    cli = app.test_client()
    cli.post("/login", data={"username": "admin", "password": MDP})
    d = cli.get("/api/security/mtls/status").get_json()
    verifier(d.get("controleur_cert", {}).get("jours", 0) >= 800, "l'état donne l'échéance du contrôleur")
    verifier(d.get("ca_cert", {}).get("jours", 0) >= 3000, "… celle de l'autorité")
    noeuds = {n["id"]: n for n in d.get("nodes", [])}
    verifier((noeuds.get(nid2, {}).get("cert") or {}).get("jours", 0) >= 800,
             "… et celle de chaque nœud")
    verifier(d.get("seuils") == {"paliers": [30, 10], "continu": 5}, "… et la politique d'alerte")
    r = cli.post("/api/security/mtls/controller/renew")
    verifier(r.status_code == 200 and (r.get_json() or {}).get("ok"),
             "la route de renouvellement du contrôleur répond")
    verifier(alertes("alert.securite.cert_controleur_renouvele"),
             "… et le renouvellement est inscrit au journal de sécurité")
    for lang in ("fr", "en"):
        database.get_db().execute("update users set lang=? where username='admin'", (lang,))
        database.get_db().commit()
        h = cli.get("/settings").get_data(as_text=True)
        # Le catalogue JS est servi en FICHIER (/i18n/<lang>.<version>.js, 2026-10-18) : on lit
        # ce que la page charge réellement, page + catalogue.
        m = re.search(r'<script src="(/i18n/[^"]+)"', h)
        verifier(m is not None, "[%s] la page charge son catalogue JS" % lang)
        if m:
            h += cli.get(m.group(1)).get_data(as_text=True)
        for cle in ("settings.securite.renew_ctrl_btn", "settings.securite.renew_btn",
                    "settings.securite.col_cert", "settings.securite.cert_echeance",
                    "settings.securite.cert_politique"):
            verifier('"%s"' % cle in h, "[%s] %s dans le catalogue de la page" % (lang, cle))
        verifier("renewSecuriteNode(" in h and "renewSecuriteCtrl()" in h,
                 "[%s] les deux renouvellements sont câblés" % lang)

    print()
    if ECHECS:
        print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  - " + e)
        sys.exit(1)
    print("TOUT PASSE")


if __name__ == "__main__":
    main()
