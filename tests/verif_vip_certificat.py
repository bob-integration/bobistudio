#!/usr/bin/env python3
"""Garde : l'IP virtuelle HA est dans le certificat du contrôleur, et les écoutes le servent.

DÉFAUT D'ORIGINE : le certificat du contrôleur ne portait que SA propre IP. Les opérateurs visent
l'IP virtuelle ; en mode web tls, avec notre CA installée sur les postes, le navigateur refusait
donc l'adresse — et pareil sur le standby après bascule, puisque la VIP le suit. Ni les
agents-nœuds ni la réplication HA n'étaient touchés : ils ne vérifient pas d'adresse.

Ce qu'on vérifie, comme le ferait un navigateur : une poignée de main RÉELLE avec
`check_hostname` contre l'IP virtuelle, refusée avant, acceptée après — sans redémarrer l'écoute.
Base jetable, répertoires TLS jetables, écoutes sur des ports éphémères en loopback.
"""
import json
import os
import socket
import ssl
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

ECHECS = []
MDP = "mot-de-passe-de-test-vip-1234"
VIP1, VIP2 = "192.0.2.250", "192.0.2.251"


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
    from app import ca, vip, web_mode, ha_tls, ha_appairage as hp
    from app import settings as st
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    config.TLS_DIR = tempfile.mkdtemp(prefix="ctrl-")
    ca.create_ca_material(["127.0.0.1"])
    P = ca.paths()

    def cert_disque():
        with open(P["ctrl_cert"], "rb") as f:
            return x509.load_pem_x509_certificate(f.read())

    def ips(cert):
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound:
            return []
        return [str(v) for v in san.get_values_for_type(x509.IPAddress)]

    # ── 1. Lecture du réglage ──────────────────────────────────────────────────────────────────
    print("\n1. L'IP virtuelle telle que réglée")
    for brut, attendu in (("192.0.2.250/24", VIP1), (" 192.0.2.250 ", VIP1), ("", None),
                          ("pas-une-ip/24", None)):
        st.set("vip_address", brut)
        verifier(vip.ip_certificat() == attendu, "%r → %r" % (brut, attendu))

    # ── 2. La ré-émission ─────────────────────────────────────────────────────────────────────
    print("\n2. Ré-émission du certificat")
    st.set("vip_address", "")
    avant = cert_disque()
    verifier(vip.synchroniser_certificat() is False,
             "sans IP virtuelle, rien n'est ré-émis (installation neuve déjà marquée)")
    st.set("vip_address", VIP1 + "/24")
    verifier(vip.synchroniser_certificat() is True, "une IP virtuelle réglée → ré-émis")
    apres = cert_disque()
    verifier(VIP1 in ips(apres), "le certificat porte l'IP virtuelle")
    verifier("127.0.0.1" in ips(apres), "… et garde l'IP propre du contrôleur")
    verifier(ca.est_controleur(apres), "… et son marqueur de contrôleur")
    verifier(apres.public_key().public_numbers() == avant.public_key().public_numbers(),
             "… avec la MÊME clé")
    verifier(abs((apres.not_valid_after_utc - avant.not_valid_after_utc).days) <= 1,
             "… et la même échéance (pas de prolongation silencieuse)")
    verifier(os.path.exists(P["ctrl_cert"] + ".precedent"), "l'ancien certificat est gardé")
    verifier(vip.synchroniser_certificat() is False, "idempotent : pas de seconde ré-émission")

    # Un certificat d'avant le marqueur reçoit marqueur ET IP en une seule ré-émission.
    cle = serialization.load_pem_private_key(open(P["ctrl_key"], "rb").read(), password=None)
    ca_c, ca_k = ca._load_ca()
    import ipaddress
    vieux = ca._sign(ca._leaf_builder("bobi-controller", cle.public_key(), ca_c, [
        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))], 300), ca_k)
    with open(P["ctrl_cert"], "wb") as f:
        f.write(vieux.public_bytes(serialization.Encoding.PEM))
    verifier(vip.synchroniser_certificat() is True and ca.est_controleur(cert_disque())
             and VIP1 in ips(cert_disque()),
             "un contrôleur d'avant le marqueur reçoit marqueur ET IP virtuelle d'un coup")

    # ── 3. Les écoutes servent le NOUVEAU certificat, sans redémarrer ─────────────────────────
    print("\n3. Rechargement à chaud des écoutes")

    def app_wsgi(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    def poignee(port, nom_vise, client=False):
        """Comme un navigateur : CA de confiance + vérification de l'ADRESSE visée.
        → (accepté ?, IP du certificat présenté)."""
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=P["ca_cert"])
        if client:
            ctx.load_cert_chain(P["ctrl_cert"], P["ctrl_key"])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s, \
                    ctx.wrap_socket(s, server_hostname=nom_vise) as t:
                der = t.getpeercert(binary_form=True)
                return True, ips(x509.load_der_x509_certificate(der))
        except ssl.SSLCertVerificationError:
            return False, None

    port_web = port_libre()
    st.set("web_tls_port", port_web)
    web_mode.fixer("tls")
    srv_web = web_mode.demarrer_https(app_wsgi, hote="127.0.0.1")
    verifier(srv_web is not None, "(précondition : écoute HTTPS démarrée)")
    port_ha = port_libre()
    srv_ha = ha_tls.demarrer(app_wsgi, hote="127.0.0.1", port=port_ha)
    verifier(srv_ha is not None, "(précondition : écoute mTLS HA démarrée)")
    try:
        verifier(poignee(port_web, VIP1)[0], "HTTPS : un navigateur qui vise la VIP l'accepte")
        verifier(not poignee(port_web, VIP2)[0],
                 "(précondition : une adresse ABSENTE du certificat est bien refusée)")
        st.set("vip_address", VIP2 + "/24")
        verifier(vip.synchroniser_certificat() is True, "la VIP change → ré-émis")
        ok_web, ips_web = poignee(port_web, VIP2)
        verifier(ok_web, "★ HTTPS : la NOUVELLE VIP est acceptée sans redémarrer l'écoute")
        verifier(poignee(port_web, VIP1)[0],
                 "l'ancienne VIP reste dans le certificat (ajout seulement, assumé)")
        ok_ha, ips_ha = poignee(port_ha, VIP2, client=True)
        verifier(ok_ha and VIP2 in (ips_ha or []),
                 "★ HA : l'écoute mTLS présente aussi le nouveau certificat")
    finally:
        srv_web.stop()
        ha_tls.arreter()
        web_mode._ctx_https = None
    verifier(web_mode.recharger_certificat() is False and ha_tls.recharger_certificat() is False,
             "sans écoute en service, le rechargement ne fait rien (et ne lève pas)")

    # ── 4. Le standby reçoit la VIP à l'appairage ─────────────────────────────────────────────
    print("\n4. Le certificat du standby, émis à l'appairage")
    code, _exp = hp.demarrer()
    dem, _cle_std = hp.preparer_demande(code)
    paquet = hp.ouvrir(code, dem["nonce"], hp.traiter_demande(dem, "127.0.0.2"))
    std = x509.load_pem_x509_certificate(paquet["cert"].encode())
    verifier(VIP2 in ips(std) and "127.0.0.2" in ips(std) and ca.est_controleur(std),
             "★ le standby porte l'IP virtuelle (il la portera promu), la sienne, et le marqueur")

    # ── 5. Par la vraie route d'enregistrement de la VIP ──────────────────────────────────────
    print("\n5. Réglages → Haute disponibilité → VIP, par la route")
    from app.auth import hash_password
    database.db_create_user("admin", hash_password(MDP), "admin")
    import main as app_main
    app = app_main.app
    app.config["TESTING"] = True
    cli = app.test_client()
    cli.post("/login", data={"username": "admin", "password": MDP})
    VIP3 = "192.0.2.252"
    r = cli.post("/api/ha/vip", data=json.dumps({"vip_address": VIP3 + "/24", "apply": False}),
                 content_type="application/json")
    verifier(r.status_code == 200, "la route accepte l'enregistrement (sans appliquer keepalived)")
    verifier(VIP3 in ips(cert_disque()),
             "★ enregistrer une VIP l'inscrit dans le certificat, même sans keepalived")

    # ── 6. Le standby qui rejoint l'actif recharge son écoute HTTPS ───────────────────────────
    print("\n6. Rejoindre l'actif (standby) : l'écoute HTTPS sert le certificat REÇU")
    # Défaut voisin, antérieur à la VIP : la route relançait l'écoute HA mais pas l'écoute HTTPS,
    # qui servait encore le certificat de l'ANCIENNE autorité jusqu'au redémarrage suivant.
    # L'échange réseau est remplacé : seul compte ce que la route fait APRÈS l'installation.
    IP_RECUE = "198.51.100.7"
    st.set("vip_address", "")      # → synchroniser ne ré-émet rien : c'est le rechargement direct
    st.set("ha_tls_port", port_libre())
    port_web = port_libre()
    st.set("web_tls_port", port_web)
    web_mode.fixer("tls")             # l'import de main.py a ré-appliqué le mode enregistré (http)
    srv_web = web_mode.demarrer_https(app_wsgi, hote="127.0.0.1")
    if srv_web is None:
        verifier(False, "(précondition : écoute HTTPS redémarrée pour la section 6)")
        return fin()

    def faux_rejoindre(url, code):
        crt, _k = ca.generate_leaf("bobi-controller", ip=IP_RECUE,
                                   uri=ca.MARQUEUR_CONTROLEUR + IP_RECUE)
        cle_pem = open(P["ctrl_key"], "rb").read()
        reemis = ca._sign(ca._leaf_builder(
            "bobi-controller", serialization.load_pem_private_key(cle_pem, None).public_key(),
            ca_c, list(x509.load_pem_x509_certificate(crt).extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value), 300), ca_k)
        with open(P["ctrl_cert"], "wb") as f:
            f.write(reemis.public_bytes(serialization.Encoding.PEM))
        return "empreinte", "http://127.0.0.9:5000"

    vrai = hp.rejoindre
    hp.rejoindre = faux_rejoindre
    try:
        verifier(not poignee(port_web, IP_RECUE)[0],
                 "(précondition : l'écoute HTTPS ne connaît pas encore le certificat reçu)")
        r = cli.post("/api/ha/pairing/join",
                     data=json.dumps({"actif": "http://127.0.0.9:5000", "code": "x"}),
                     content_type="application/json")
        verifier(r.status_code == 200, "la route de jonction répond 200")
        verifier(poignee(port_web, IP_RECUE)[0],
                 "★ après jonction, l'écoute HTTPS présente le certificat reçu, sans redémarrer")
    finally:
        hp.rejoindre = vrai
        srv_web.stop()
        ha_tls.arreter()

    fin()


def fin():
    print()
    if ECHECS:
        print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  - " + e)
        sys.exit(1)
    print("TOUT PASSE")


if __name__ == "__main__":
    main()
