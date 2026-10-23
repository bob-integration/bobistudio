#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Mode web : à qui croit-on quand un client annonce son adresse ?

★ CE QUE CETTE GARDE PROTÈGE. Jusqu'au 2026-10-10, `ProxyFix` était monté inconditionnellement :
`remote_addr` venait de `X-Forwarded-For`, un en-tête que le CLIENT choisit, sur des installations
dont aucune n'avait de proxy. Le plafond d'essais de connexion se contournait en changeant
d'en-tête à chaque essai. Mesuré, même attaquant, même adresse TCP, 25 essais :

    sans en-tête                       → 15 bloqués
    X-Forwarded-For différent à chaque → 0 bloqué        ← le mot de passe se devinait sans limite

Ce qui est vérifié ici, base jetable, aucun réseau :
  1. le middleware : en `http` il ne touche à RIEN ; en `proxy` il réécrit adresse, schéma, hôte ;
  2. L'ATTAQUE REJOUÉE de bout en bout : en `http`, l'en-tête tournant ne contourne plus le plafond ;
  3. le filtre des liens publics lit le pair TCP en `http`, même devant un en-tête forgé ;
  4. le mode se valide à la saisie, se lit au démarrage, et l'écart actif/enregistré se voit ;
  5. le secours par variable d'environnement prime, et une valeur invalide retombe sur `http` ;
  6. `public_trust_proxy` a bien disparu — un réglage mort documenté comme agissant est un piège.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ECHECS = []
MDP = "Banc-Mode-Web-2026!"


def verifier(cond, libelle):
    print(("  ✓ " if cond else "  ✗ ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def _echo(environ, start_response):
    """Application WSGI témoin : renvoie ce que le middleware lui a transmis."""
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [("%s|%s|%s" % (environ.get("REMOTE_ADDR"), environ.get("wsgi.url_scheme"),
                           environ.get("HTTP_HOST"))).encode()]


def _passe(mw, xff=None, proto=None, host=None):
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "SERVER_NAME": "orch", "SERVER_PORT": "5000",
           "REMOTE_ADDR": "203.0.113.66", "wsgi.url_scheme": "http", "HTTP_HOST": "orch:5000",
           "wsgi.input": None}
    if xff:
        env["HTTP_X_FORWARDED_FOR"] = xff
    if proto:
        env["HTTP_X_FORWARDED_PROTO"] = proto
    if host:
        env["HTTP_X_FORWARDED_HOST"] = host
    return b"".join(mw(env, lambda *a, **k: None)).decode().split("|")


def main():
    chemin = os.path.join(tempfile.mkdtemp(), "t.db")
    from app import config, database
    config.DB_PATH = chemin
    database.DB_PATH = chemin
    # ⚠ Connexion THREAD-LOCAL : sans cette purge, le banc écrirait dans la base de PRODUCTION.
    database._tls.__dict__.pop("conn", None)
    database.init_db()
    os.environ.pop("BOBI_WEB_MODE", None)

    from app import web_mode
    from app.auth import hash_password
    database.db_create_user("cible", hash_password(MDP), "admin")

    # ── 1. Le middleware, à nu ─────────────────────────────────────────────────────────────
    print("\n1. Le middleware")
    mw = web_mode.EnTetesTransmis(_echo)
    web_mode.fixer("http")
    ip, sch, hote = _passe(mw, xff="198.51.100.9", proto="https", host="attaquant.example")
    verifier(ip == "203.0.113.66", "http : l'adresse reste le PAIR TCP malgré X-Forwarded-For (%s)" % ip)
    verifier(sch == "http", "http : le schéma n'est pas réécrit par X-Forwarded-Proto (%s)" % sch)
    verifier(hote == "orch:5000",
             "http : l'hôte n'est PAS imposable par X-Forwarded-Host (%s)" % hote)
    web_mode.fixer("proxy")
    ip, sch, hote = _passe(mw, xff="198.51.100.9", proto="https", host="studio.example")
    verifier(ip == "198.51.100.9", "proxy : l'adresse transmise est crue (%s)" % ip)
    verifier(sch == "https", "proxy : le schéma transmis est cru (%s)" % sch)
    verifier(hote == "studio.example", "proxy : l'hôte transmis est cru (%s)" % hote)

    # ── 2. L'attaque, rejouée de bout en bout ──────────────────────────────────────────────
    print("\n2. L'attaque rejouée : X-Forwarded-For tournant contre le plafond d'essais")
    import main as app_main
    app = app_main.app
    app.config["TESTING"] = True
    from app.routes import auth_pages as ap
    n = ap._LOGIN_MAX_FAILS + 15

    def rafale(tournant):
        ap._login_fails.clear()
        cli = app.test_client()
        codes = []
        for i in range(n):
            h = {"X-Forwarded-For": "198.51.100.%d" % (i + 1)} if tournant else {}
            r = cli.post("/login", data={"username": "cible", "password": "faux-%d" % i},
                         headers=h, environ_overrides={"REMOTE_ADDR": "203.0.113.66"})
            codes.append(r.status_code)
        return codes.count(429)

    web_mode.fixer("http")
    sans, avec = rafale(False), rafale(True)
    verifier(sans > 0, "http, sans en-tête : le plafond bloque (%d/%d)" % (sans, n))
    verifier(avec == sans,
             "http, en-tête TOURNANT : bloqué AUTANT (%d/%d) — c'était 0 avant le correctif"
             % (avec, n))
    # Derrière un proxy, des X-Forwarded-For différents SONT des clients différents : c'est le
    # proxy qui les a écrits. Le comportement attendu n'est donc pas le blocage — on vérifie
    # qu'on croit bien le proxy, pas qu'on bloque.
    web_mode.fixer("proxy")
    verifier(rafale(True) == 0,
             "proxy : chaque adresse transmise compte comme un client distinct (attendu)")
    web_mode.fixer("http")

    # ── 3. Le filtre des liens publics ─────────────────────────────────────────────────────
    print("\n3. Liens publics : le filtre d'adresses")
    from app.routes.pages import _ip_client_pour_filtre
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.66"},
                                  headers={"X-Forwarded-For": "10.0.0.1"}):
        verifier(_ip_client_pour_filtre() == "203.0.113.66",
                 "http : une liste blanche ne se contourne pas par un en-tête forgé")
    # LA CEINTURE. En mode http il n'y a plus de ProxyFix, donc « croire l'en-tête » et « lire le
    # pair TCP » donnent la même adresse — une mutation qui ferait TOUJOURS confiance passerait
    # inaperçue (constaté : M5 survivait). La branche ne compte que si un ProxyFix était remonté
    # AILLEURS, sans passer par le mode. On simule ce remontage et on vérifie qu'elle tient.
    from werkzeug.middleware.proxy_fix import ProxyFix
    vu = {}

    def _capture(environ, start_response):
        vu.update(environ)
        start_response("200 OK", [])
        return [b""]
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "SERVER_NAME": "orch", "SERVER_PORT": "5000",
           "REMOTE_ADDR": "203.0.113.66", "wsgi.url_scheme": "http", "HTTP_HOST": "orch:5000",
           "HTTP_X_FORWARDED_FOR": "10.0.0.1", "wsgi.input": None}
    b"".join(ProxyFix(_capture, x_for=1)(env, lambda *a, **k: None))
    web_mode.fixer("http")
    with app.request_context(vu):
        verifier(_ip_client_pour_filtre() == "203.0.113.66",
                 "http + un ProxyFix remonté hors du mode : la ceinture retombe sur le pair TCP")
    web_mode.fixer("proxy")
    with app.request_context(vu):
        verifier(_ip_client_pour_filtre() == "10.0.0.1",
                 "proxy : l'adresse transmise par le proxy est bien celle filtrée")
    web_mode.fixer("http")

    # ── 4. Saisie, lecture au démarrage, écart actif/enregistré ────────────────────────────
    print("\n4. Le réglage")
    cli = app.test_client()
    cli.post("/login", data={"username": "cible", "password": MDP},
             environ_overrides={"REMOTE_ADDR": "192.0.2.1"})
    r = cli.post("/api/settings", json={"web_mode": "https-svp"})
    verifier(r.status_code == 400, "une valeur inconnue est REFUSÉE à la saisie (%s)" % r.status_code)
    r = cli.post("/api/settings", json={"web_mode": " PROXY "})
    verifier(r.status_code == 200, "une valeur valide est acceptée, normalisée (%s)" % r.status_code)
    from app import settings as st
    verifier(st.get("web_mode") == "proxy", "elle est enregistrée normalisée (%r)" % st.get("web_mode"))
    e = cli.get("/api/web-mode").get_json() or {}
    verifier(e.get("actif") == "http" and e.get("enregistre") == "proxy"
             and e.get("redemarrage_requis") is True,
             "l'écart ACTIF/ENREGISTRÉ se voit : redémarrage requis (%s)" % e)
    verifier(web_mode.appliquer() == "proxy", "au démarrage, le mode enregistré s'applique")
    e = cli.get("/api/web-mode").get_json() or {}
    verifier(e.get("redemarrage_requis") is False, "après application, plus d'écart")
    verifier(web_mode.cookie_secure_impose() is True, "proxy : le cookie de session est `Secure`")
    web_mode.fixer("http")
    verifier(web_mode.cookie_secure_impose() is False,
             "http : PAS `Secure` (sinon le navigateur ne le renverrait jamais)")

    # ── 5. Le secours par l'environnement ──────────────────────────────────────────────────
    print("\n5. Le secours BOBI_WEB_MODE")
    os.environ["BOBI_WEB_MODE"] = "http"
    verifier(web_mode.resoudre()[0] == "http",
             "la variable PRIME sur le réglage enregistré (proxy en base)")
    os.environ["BOBI_WEB_MODE"] = "nimportequoi"
    m, src = web_mode.resoudre()
    verifier(m == "http" and "invalide" in src,
             "une variable invalide retombe sur http ET le dit (%s)" % src)
    os.environ.pop("BOBI_WEB_MODE", None)
    database.db_set_setting("web_mode", "corrompu") if hasattr(database, "db_set_setting") \
        else st.set("web_mode", "corrompu")
    m, src = web_mode.resoudre()
    verifier(m == "http" and "invalide" in src,
             "un réglage corrompu en base retombe sur http ET le dit (%s)" % src)

    # ── 6. L'ancien réglage a disparu ──────────────────────────────────────────────────────
    print("\n6. `public_trust_proxy` est retiré, pas laissé mort")
    verifier("public_trust_proxy" not in st.DEFAULTS, "absent des réglages déclarés")
    r = cli.post("/api/settings", json={"public_trust_proxy": True})
    verifier("public_trust_proxy" in ((r.get_json() or {}).get("ignored") or []),
             "l'écrire est signalé `ignored`, pas faussement enregistré")

    # ── 7. Le mode tls ─────────────────────────────────────────────────────────────────────
    print("\n7. Mode tls : deux écoutes")
    st.set("web_mode", "http")
    st.set("web_tls_port", 443)
    web_mode.fixer("tls")
    ecoute = web_mode.EcouteMachines(_echo)

    def passe_http(chemin, methode="GET", qs="", hote="orch:5000"):
        env = {"REQUEST_METHOD": methode, "PATH_INFO": chemin, "QUERY_STRING": qs,
               "SERVER_NAME": "orch", "SERVER_PORT": "5000", "REMOTE_ADDR": "203.0.113.66",
               "wsgi.url_scheme": "http", "HTTP_HOST": hote, "wsgi.input": None}
        vu = {}

        def sr(statut, entetes, *a):
            vu["statut"] = statut
            vu["entetes"] = dict(entetes)
        corps = b"".join(ecoute(env, sr)).decode()
        return vu["statut"][:3], vu["entetes"], corps

    # ⚠ ÉCRIT EN DUR, et c'est voulu. La première version lisait `web_mode.CHEMINS_MACHINE` :
    # retirer un chemin du module raccourcissait AUSSI l'attente, et la garde restait verte
    # (mutation M2, constatée). Cet inventaire est un fait sur les APPELANTS, relevé le
    # 2026-10-10 — pas une propriété du module qu'on vérifie.
    machines = ["/api/identity", "/api/nodes/enroll", "/iso/abc.iso", "/api/update/ping",
                "/api/ha/pairing/claim"]
    passes = [c for c in machines if passe_http(c)[0] == "200"]
    verifier(passes == machines, "tls : les %d chemins MACHINES passent en HTTP (%s)"
             % (len(machines), set(machines) - set(passes) or "tous"))
    code, ent, _ = passe_http("/login", qs="next=%2Fcables")
    verifier(code == "301" and ent.get("Location") == "https://orch/login?next=%2Fcables",
             "tls : GET /login en HTTP → 301 vers HTTPS, port 443 omis, requête conservée (%s %s)"
             % (code, ent.get("Location")))
    code, _, corps = passe_http("/login", methode="POST")
    verifier(code == "403" and "https_required" in corps,
             "tls : POST /login en HTTP → REFUSÉ, pas redirigé (le corps a déjà fui) (%s)" % code)
    # La réplication HA n'est PLUS servie en HTTP : elle a sa propre écoute mTLS (ha_tls).
    verifier(passe_http("/api/ha/replicate", methode="POST")[0] == "403",
             "tls : la réplication HA n'est plus servie sur le :5000 en clair")
    code, _, _ = passe_http("/api/containers")
    verifier(code == "301", "tls : une route d'API ordinaire n'est pas servie en HTTP (%s)" % code)
    st.set("web_tls_port", 8443)
    code, ent, _ = passe_http("/", hote="192.0.2.10:5000")
    verifier(ent.get("Location") == "https://192.0.2.10:8443/",
             "tls : un port HTTPS non standard est porté dans la redirection (%s)" % ent.get("Location"))
    st.set("web_tls_port", 443)
    web_mode.fixer("http")
    verifier(passe_http("/login")[0] == "200", "http : l'écoute reste TRANSPARENTE (aucune redirection)")

    print("\n   URL remises aux machines")
    web_mode.fixer("tls")
    verifier(web_mode.url_machines("https://192.0.2.10/") == "http://192.0.2.10:5000",
             "tls : l'URL HTTPS de l'administrateur devient l'écoute HTTP machine")
    verifier(web_mode.url_machines(None, "ctrl.example:443") == "http://ctrl.example:5000",
             "tls : sans proposition, on part de l'hôte de la requête")
    verifier(web_mode.url_machines("https://[fd00::1]:443/") == "http://[fd00::1]:5000",
             "tls : une adresse IPv6 reste un littéral d'URL valide")
    from app.routes import enrollment
    with app.test_request_context("/", base_url="https://192.0.2.10/"):
        from flask import request as _rq
        verifier(enrollment._url_machines(_rq.host_url) == "http://192.0.2.10:5000",
                 "tls : l'ENRÔLEMENT remet bien l'URL machine à un nœud neuf")
    web_mode.fixer("http")
    verifier(web_mode.url_machines("http://192.0.2.10:5000/") == "http://192.0.2.10:5000",
             "http : l'URL remise aux machines est inchangée")
    web_mode.fixer("tls")
    verifier(web_mode.cookie_secure_impose() is True, "tls : le cookie de session est `Secure`")

    print("\n   Un VRAI aller-retour HTTPS, sur le certificat du contrôleur")
    import socket
    import ssl
    import urllib.request
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port_libre = s.getsockname()[1]
    s.close()
    st.set("web_tls_port", port_libre)
    srv = web_mode.demarrer_https(app, hote="127.0.0.1")
    verifier(srv is not None and web_mode.actif() == "tls",
             "l'écoute HTTPS démarre (port éphémère %d — le 443 de la machine n'est pas touché)"
             % port_libre)
    if srv is not None:
        from app import ca
        ctx = ssl.create_default_context(cafile=ca.paths()["ca_cert"])
        ctx.check_hostname = False   # le SAN du certificat est l'IP du contrôleur, pas 127.0.0.1
        try:
            with urllib.request.urlopen("https://127.0.0.1:%d/api/update/ping" % port_libre,
                                        context=ctx, timeout=10) as r:
                verifier(r.status == 200,
                         "HTTPS répond, et la chaîne se VÉRIFIE contre la CA du contrôleur")
        except Exception as e:
            verifier(False, "HTTPS répond et se vérifie (%s)" % e)
        vieux = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        vieux.check_hostname = False
        vieux.verify_mode = ssl.CERT_NONE
        try:
            vieux.maximum_version = ssl.TLSVersion.TLSv1_1
            vieux.minimum_version = ssl.TLSVersion.TLSv1
            with socket.create_connection(("127.0.0.1", port_libre), timeout=5) as raw:
                try:
                    with vieux.wrap_socket(raw):
                        refuse = False
                except ssl.SSLError:
                    refuse = True
            verifier(refuse, "un client limité à TLS 1.1 est REFUSÉ (cheroot accepterait tout "
                             "par défaut)")
        except (ValueError, ssl.SSLError) as e:
            print("  · TLS 1.1 non testable ici : l'OpenSSL local ne sait plus le parler (%s)" % e)
        # La poignée de main ci-dessus ne suffit pas : l'OpenSSL de Debian 13 refuse TLS 1.1 DE
        # LUI-MÊME, donc retirer notre minimum ne changeait rien ici (mutation M1, survivante).
        # On vérifie donc le contexte que le serveur porte réellement : c'est lui qui rend la
        # propriété indépendante de la politique de l'OpenSSL de la machine.
        verifier(srv.ssl_adapter.context.minimum_version == ssl.TLSVersion.TLSv1_2,
                 "le contexte MONTÉ impose TLS 1.2 lui-même, sans dépendre de l'OpenSSL système")
        srv.stop()

    print("\n   Repli : jamais d'interface injoignable")
    occupe = socket.socket()
    occupe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    occupe.bind(("127.0.0.1", 0))
    occupe.listen(1)
    st.set("web_tls_port", occupe.getsockname()[1])
    # tls ENREGISTRÉ : sans cette précondition, enregistré == actif (http) et « pas de
    # redémarrage requis » était vrai pour une mauvaise raison (mutation M6, survivante).
    st.set("web_mode", "tls")
    web_mode.fixer("tls")
    srv = web_mode.demarrer_https(app, hote="127.0.0.1")
    occupe.close()
    e = web_mode.etat()
    verifier(srv is None and web_mode.actif() == "http",
             "port HTTPS occupé → l'écoute HTTPS échoue et l'on passe en HTTP COMPLET")
    verifier(passe_http("/login")[0] == "200",
             "… donc le :5000 sert de nouveau /login — sinon redirection vers un HTTPS mort")
    verifier((e.get("repli") or {}).get("cle") == "alert.securite.web_tls_repli_port"
             and e.get("redemarrage_requis") is False,
             "l'état NOMME la cause, et ne prétend pas qu'un redémarrage suffirait")
    with database.get_db() as db:
        lg = [dict(r) for r in db.execute(
            "SELECT * FROM alerts WHERE msg_key LIKE 'alert.securite.web_tls_repli%' "
            "ORDER BY id DESC")]
    verifier(lg and lg[0].get("niveau") == "error" and lg[0].get("kind") == "auth",
             "une alerte d'ERREUR entre dans la piste d'audit (kind auth)")
    os.environ["BOBI_WEB_MODE"] = "tls"
    orig = web_mode.fichiers_tls
    web_mode.fichiers_tls = lambda: ("/nulle/part.crt", "/nulle/part.key")
    try:
        verifier(web_mode.appliquer() == "http"
                 and web_mode.etat()["repli"]["cle"] == "alert.securite.web_tls_repli_certificat",
                 "certificat introuvable au démarrage → repli http, cause nommée")
    finally:
        web_mode.fichiers_tls = orig
        os.environ.pop("BOBI_WEB_MODE", None)
    web_mode.fixer("http")

    print("\n   Le port se valide à la saisie")
    for val, attendu in ((5000, 400), (8000, 400), ("abc", 400), (70000, 400), (8443, 200)):
        r = cli.post("/api/settings", json={"web_tls_port": val})
        verifier(r.status_code == attendu, "web_tls_port=%r → %s (attendu %s)"
                 % (val, r.status_code, attendu))

    print()
    if ECHECS:
        print("ÉCHEC — %d point(s) :" % len(ECHECS))
        for x in ECHECS:
            print("  · " + x)
        return 1
    print("TOUT PASSE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
