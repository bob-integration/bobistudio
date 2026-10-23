#!/usr/bin/env python3
"""Garde : seul un CONTRÔLEUR pilote un agent-nœud (`:9100`), pas un conteneur ni un autre nœud.

DÉFAUT D'ORIGINE (agent ≤ 0.22.0) : l'agent ne vérifiait que la CHAÎNE du certificat client
(`CERT_REQUIRED`). Or la même CA signe contrôleurs, nœuds et conteneurs — et un conteneur exécute du
code de plugin tiers, avec sa clé posée sur place. Ce conteneur passait la poignée de main de tous
les agents-nœuds de la flotte ; seul le jeton applicatif l'arrêtait encore.

Le vrai `Handler` de node_agent/agent.py est servi en mTLS sur un port éphémère, avec le vrai
`_make_server_ssl_context` ; chaque sorte de certificat se présente avec le BON jeton — c'est donc
le rôle, et lui seul, qui décide. Aucun réseau hors loopback, tout en répertoires jetables.
"""
import http.client
import importlib.util
import json
import os
import socket
import ssl
import sys
import tempfile
import threading

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

ECHECS = []
JETON = "jeton-du-noeud-de-test-0123456789"


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


def ecrire(d, nom, data):
    p = os.path.join(d, nom)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    return p


def main():
    from app import config, ca
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    import ipaddress

    config.TLS_DIR = tempfile.mkdtemp(prefix="ctrl-")
    ca.create_ca_material(["127.0.0.1"])
    P = ca.paths()

    # ── L'agent-nœud, tel qu'il tourne sur un nœud enrôlé ─────────────────────────────────────
    tls_noeud = tempfile.mkdtemp(prefix="noeud-tls-")
    crt, key = ca.generate_leaf("noeud-1", ip="127.0.0.1", node_id=1)
    ecrire(tls_noeud, "node.crt", crt)
    ecrire(tls_noeud, "node.key", key)
    ecrire(tls_noeud, "ca.crt", ca.ca_cert_pem())
    cfg = ecrire(tempfile.mkdtemp(), "config.json",
                 json.dumps({"token": JETON, "tls_dir": tls_noeud, "capabilities": []}))
    os.environ["BOBI_NODE_AGENT_CONFIG"] = cfg
    spec = importlib.util.spec_from_file_location("bobi_node_agent",
                                                  os.path.join(RACINE, "node_agent", "agent.py"))
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    assert agent.CONFIG.get("token") == JETON, "config de test non chargée"

    port = port_libre()
    httpd = agent.ThreadingHTTPServer(("127.0.0.1", port), agent.Handler)
    httpd.socket = agent._make_server_ssl_context().wrap_socket(httpd.socket, server_side=True)
    httpd.handle_error = lambda *a: None   # les poignées de main nues des préconditions coupent net
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="clients-")

    def appel(crt_pem, key_pem, chemin="/v1/host/clock", methode="GET", jeton=JETON):
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=P["ca_cert"])
        ctx.check_hostname = False
        ctx.load_cert_chain(ecrire(tmp, "c.crt", crt_pem), ecrire(tmp, "c.key", key_pem))
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
        try:
            c.request(methode, chemin, body=b"{}" if methode == "POST" else None,
                      headers={"X-MXL-Node-Token": jeton} if jeton else {})
            r = c.getresponse()
            r.read()
            return r.status
        except Exception as e:
            return "exception %s" % type(e).__name__
        finally:
            c.close()

    def lire(p):
        with open(p, "rb") as f:
            return f.read()

    ctrl = (lire(P["ctrl_cert"]), lire(P["ctrl_key"]))
    conteneur = ca.generate_leaf("mxl901", uri="bobi://container/901")
    autre_noeud = ca.generate_leaf("noeud-2", ip="127.0.0.1", node_id=2)
    # Un conteneur qui se serait fait émettre le CN du contrôleur : l'URI de rôle doit l'emporter.
    deguise = ca.generate_leaf("bobi-controller", uri="bobi://container/902")
    inconnu = ca.generate_leaf("quelquun")
    # Le certificat d'un contrôleur d'AVANT le marqueur (fabriqué comme avant le 2026-10-10).
    cle_ctrl = serialization.load_pem_private_key(ctrl[1], password=None)
    ca_c, ca_k = ca._load_ca()
    ancien = ca._sign(ca._leaf_builder(
        "bobi-controller", cle_ctrl.public_key(), ca_c,
        [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))], 365), ca_k)
    ancien = (ancien.public_bytes(serialization.Encoding.PEM), ctrl[1])

    # ── Préconditions : sans elles, un 403 ne prouverait rien ─────────────────────────────────
    print("\nPréconditions")
    verifier(ca.est_controleur(ctrl[0]), "le contrôleur de test porte le marqueur")
    verifier(not ca.est_controleur(ancien[0]), "l'ancien certificat de contrôleur n'en porte pas")
    for nom, (c, k) in (("conteneur", conteneur), ("autre nœud", autre_noeud)):
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=P["ca_cert"])
        ctx.check_hostname = False
        ctx.load_cert_chain(ecrire(tmp, "p.crt", c), ecrire(tmp, "p.key", k))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s, \
                    ctx.wrap_socket(s) as t:
                t.do_handshake()
                ok = True
        except Exception:
            ok = False
        verifier(ok, "un certificat de %s PASSE la poignée de main (le défaut d'origine : "
                     "la chaîne seule l'accepte)" % nom)

    # ── Le rôle décide ────────────────────────────────────────────────────────────────────────
    print("\nQui pilote le nœud (tous avec le BON jeton)")
    verifier(appel(*ctrl) == 200, "le contrôleur (marqueur) → 200")
    verifier(appel(*ancien) == 200,
             "un contrôleur d'avant le marqueur (CN seul, aucune URI) → 200 : pas de verrouillage")
    verifier(appel(*conteneur) == 403, "★ un CONTENEUR → 403")
    verifier(appel(*autre_noeud) == 403, "★ un autre NŒUD → 403")
    verifier(appel(*deguise) == 403, "un conteneur qui porte le CN du contrôleur → 403 (l'URI gagne)")
    verifier(appel(*inconnu) == 403, "une identité inconnue de notre CA → 403")
    verifier(appel(*conteneur, chemin="/v1/nope", methode="POST") == 403,
             "le POST est gardé aussi (un conteneur → 403 avant tout routage)")
    verifier(appel(*ctrl, chemin="/v1/nope", methode="POST") == 404,
             "… et un contrôleur atteint le routage du POST (404 sur une route inconnue)")
    verifier(appel(*conteneur, chemin="/v1/ping") == 200, "/v1/ping reste ouvert (liveness)")
    verifier(appel(*ctrl, jeton=None) == 401, "le jeton reste exigé du contrôleur (second facteur)")
    verifier(appel(*ctrl, jeton="faux") == 401, "… et un faux jeton est refusé")

    agent.CONFIG["verify_controller_identity"] = False
    verifier(appel(*conteneur) == 200,
             "échappatoire `verify_controller_identity: false` → chaîne seule (historique)")
    agent.CONFIG.pop("verify_controller_identity")
    verifier(appel(*conteneur) == 403, "… et sans elle, le refus revient")

    # ── Le CN du contrôleur ne se DEMANDE pas ─────────────────────────────────────────────────
    print("\nsign_csr : le CN du contrôleur est réservé")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    def csr(cn):
        k = ec.generate_private_key(ec.SECP256R1())
        return x509.CertificateSigningRequestBuilder().subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])).sign(
            k, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)

    for cn in ("bobi-controller", "BOBI-Controller", " bobi-controller "):
        try:
            ca.sign_csr(csr(cn), ip="127.0.0.1", node_id=9)
            refuse = False
        except ValueError:
            refuse = True
        verifier(refuse, "un nœud qui DEMANDE le CN %r est refusé" % cn)
    honnete = ca.sign_csr(csr("dl360-1"), ip="127.0.0.1", node_id=9)
    verifier("dl360-1" in x509.load_pem_x509_certificate(honnete).subject.rfc4514_string(),
             "un CSR de nœud honnête est toujours signé, CN conservé")
    std = ca.signer_controleur(csr("peu-importe"), "127.0.0.2")
    verifier("bobi-controller" in x509.load_pem_x509_certificate(std).subject.rfc4514_string()
             and ca.est_controleur(std),
             "le seul chemin légitime (appairage HA) émet toujours CN + marqueur")

    # ── Le contrôleur pose son marqueur au démarrage, avant tout appel sortant ────────────────
    print("\nmain.py")
    src = open(os.path.join(RACINE, "main.py"), encoding="utf-8").read()
    i_marq = src.find("synchroniser_certificat()")
    i_actif = src.find("if _ha.is_active():")
    i_surv = src.find("threading.Thread(target=surveillance")
    verifier(0 < i_marq < i_actif < i_surv,
             "le marqueur est posé au démarrage, dans les deux rôles, avant la surveillance")

    httpd.shutdown()
    print()
    if ECHECS:
        print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  - " + e)
        sys.exit(1)
    print("OK — seul un contrôleur pilote un agent-nœud")


if __name__ == "__main__":
    main()
