#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# Banc du CLIENT IS-07 (`services/nmos/is07_client.py`) — le sens ENTRANT.
#
# LE MONTAGE. On boucle sur NOTRE PROPRE serveur IS-07 : notre émetteur publie, notre client
# s'abonne, et on vérifie qu'un tally posé à l'intérieur ressort chez le client. C'est un vrai
# test — les deux bouts sont dissymétriques (le client masque ses trames, le serveur jamais ; le
# client bat, le serveur compte les battements) — et il ne demande ni réseau ni tiers.
#
# CE QU'IL PROTÈGE, et qui casse en silence :
#   · pas de masquage → le serveur ferme la connexion (RFC 6455 §5.1), sans erreur côté client ;
#   · pas de battement → la session est fermée d'en face au bout de 12 s : le client ne voit
#     aucune erreur, il ne reçoit simplement plus rien ;
#   · `Sec-WebSocket-Accept` non vérifié → n'importe quel serveur répondant « 101 » passe pour un
#     pair, et on lit ses octets comme des trames ;
#   · la déconnexion qui LAISSE le dernier état → un rouge allumé sur un plateau pendant que la
#     liaison est morte, ce qui est le pire des deux ;
#   · une valeur inconnue devinée → un émetteur tiers a SA propre énumération, IS-07 laisse le
#     contenu des enums au constructeur.
#
# ⚠ CE BANC OUVRE `nmos_is07` ET LE SERVEUR WS le temps de la mesure, et referme dans un `finally`.
#
#   $ ./venv/bin/python tools/verif_is07_client.py
import importlib
import os
import socket
import ssl
import sys
import tempfile
import threading
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

echecs, reussites = [], []


def controle(intitule, condition, explication=""):
    (reussites if condition else echecs).append(intitule)
    print("  %-5s %s" % ("OK" if condition else "ÉCHEC", intitule))
    if not condition and explication:
        print("        → %s" % explication)


def jusqua(predicat, delai=6.0, pas=0.05):
    """Attend qu'une condition devienne vraie. Un `sleep` fixe rendrait le banc lent ET fragile."""
    fin = time.monotonic() + delai
    while time.monotonic() < fin:
        if predicat():
            return True
        time.sleep(pas)
    return predicat()


from app.database import db_set_setting, db_get_setting                # noqa: E402


def _leve(f, *types):
    """`f()` lève-t-elle ? (l'un des `types`, ou n'importe quoi si aucun n'est donné)"""
    try:
        f()
    except Exception as e:
        return isinstance(e, types) if types else True
    return False


def _erreur(f):
    try:
        f()
    except Exception as e:
        return e
    return None


def _certificat_bidon():
    """(cert.pem, cle.pem, ca.pem) auto-signés, pour 127.0.0.1. Éphémères."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime
    import ipaddress
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nom = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    maintenant = datetime.datetime.now(datetime.timezone.utc)
    crt = (x509.CertificateBuilder().subject_name(nom).issuer_name(nom)
           .public_key(k.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(maintenant - datetime.timedelta(minutes=5))
           .not_valid_after(maintenant + datetime.timedelta(hours=1))
           .add_extension(x509.SubjectAlternativeName(
               [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
           .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
           .sign(k, hashes.SHA256()))
    d = tempfile.mkdtemp(prefix="is07-tls-")
    pc, pk = os.path.join(d, "cert.pem"), os.path.join(d, "cle.pem")
    open(pc, "wb").write(crt.public_bytes(serialization.Encoding.PEM))
    open(pk, "wb").write(k.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.TraditionalOpenSSL,
                                         serialization.NoEncryption()))
    return pc, pk, pc          # auto-signé : il est sa propre CA


def _serveur_tls(cert, cle):
    """Un serveur TLS minimal qui accepte, lit, et ferme. Renvoie (socket, port)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, cle)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(8)

    def _boucle():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            def _servir(c=c):
                try:
                    with ctx.wrap_socket(c, server_side=True) as t:
                        t.settimeout(2)
                        t.recv(4096)
                        t.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                except Exception:
                    pass
                finally:
                    try:
                        c.close()
                    except Exception:
                        pass
            threading.Thread(target=_servir, daemon=True).start()

    threading.Thread(target=_boucle, daemon=True).start()
    return s, s.getsockname()[1]

print("IS-07 entrant — notre client s'abonne à notre propre émetteur\n")

AVANT = db_get_setting("nmos_is07")
AVANT_WS = db_get_setting("nmos_is07_ws")
client = None
try:
    db_set_setting("nmos_is07", "1")
    db_set_setting("nmos_is07_ws", "1")
    from services import tsl                                          # noqa: E402
    from services.nmos import is07                                    # noqa: E402
    from services.nmos import is07_client                             # noqa: E402
    importlib.reload(is07)

    srcs = is07._sources()
    controle("★ des Sources sont publiables, sinon le banc ne prouve rien", bool(srcs))
    if not srcs:
        raise SystemExit(1)
    shm, idx, niveau, _nom = srcs[0]
    sid = is07._sid(shm, niveau)

    is07.demarrer()
    # `etat_ws()` expose `actif`, pas `running` — vérifié plutôt que supposé : la première
    # version de ce contrôle échouait sur une clé qui n'existe pas, alors que le serveur écoutait.
    controle("★★ le serveur IS-07 écoute", jusqua(lambda: is07.etat_ws().get("actif")),
             "sans lui, tout ce qui suit ne mesure rien. Obtenu %r" % (is07.etat_ws(),))

    recus = []
    verrou = threading.Lock()

    def sur_etat(source_id, valeur):
        with verrou:
            recus.append((source_id, valeur))

    uri = "ws://127.0.0.1:%d/" % is07._port()
    client = is07_client.ClientIS07(uri, [sid], sur_etat, nom="banc")
    client.demarrer()

    controle("★★★ la poignée de main aboutit et l'abonnement est accepté",
             jusqua(lambda: client.connecte),
             "masquage, Sec-WebSocket-Accept, abonnement : c'est toute la RFC 6455 côté client "
             "qui se vérifie ici. Erreur : %r" % client.derniere_erreur)

    # « Each time a client submits its subscriptions list … the server will resend all the
    # current states » : sans ce renvoi, un abonné reste aveugle jusqu'au prochain changement.
    controle("★★★ l'état courant arrive SANS attendre un changement",
             jusqua(lambda: any(s == sid for s, _ in recus)),
             "un abonné qui doit attendre un changement peut rester aveugle indéfiniment — le "
             "tally d'une source qui ne bouge pas ne bouge pas. Reçus : %r" % recus[:3])

    tsl.poser_tally("banc:is07", {})
    with verrou:
        recus.clear()
    tsl.poser_tally("banc:is07", {(idx, niveau): "red"})
    is07._pousser([sid])
    controle("★★★ un tally posé à l'intérieur ressort chez le client",
             jusqua(lambda: any(v == "red" for s, v in recus if s == sid)),
             "c'est la chaîne entière, dans les deux sens : état interne → Source → trame WS → "
             "client. Reçus : %r" % recus[:3])

    with verrou:
        recus.clear()
    tsl.poser_tally("banc:is07", {(idx, niveau): "amber"})
    is07._pousser([sid])
    controle("★★ l'ambre traverse aussi",
             jusqua(lambda: any(v == "amber" for s, v in recus if s == sid)),
             "c'est la valeur qui porte le cumul : la perdre en route ramène au modèle "
             "exclusif. Reçus : %r" % recus[:3])

    # ── Le battement, mesuré côté SERVEUR ────────────────────────────────
    # Il n'y a pas d'autre façon de le prouver : un client qui ne bat pas ne voit rien changer
    # pendant douze secondes, puis cesse de recevoir. On regarde donc la session d'en face.
    sess = list(is07._sessions)
    controle("★★ le serveur voit UNE session, la nôtre", len(sess) == 1,
             "obtenu %d" % len(sess))
    if sess:
        t0 = sess[0].sante
        controle("★★★ le client BAT, et le serveur en tient compte",
                 jusqua(lambda: sess[0].sante > t0, delai=8.0),
                 "sans battement, la session est fermée d'en face après %ds — le client ne voit "
                 "aucune erreur, il ne reçoit simplement plus rien" % is07.SANTE_TIMEOUT_S)

    # ── Perdre la liaison ÉTEINT ce qu'on affirmait ──────────────────────
    with verrou:
        recus.clear()
    client.arreter()
    controle("★★★ à la déconnexion, le client annonce qu'il ne sait plus rien",
             any(s is None and v is None for s, v in recus),
             "garder le dernier état connu laisserait un rouge allumé sur un plateau pendant que "
             "la liaison est morte — c'est le pire des deux. Reçus : %r" % recus[:3])
    client = None

    # ── Ce qu'on refuse de deviner ───────────────────────────────────────
    c2 = is07_client.ClientIS07("ws://127.0.0.1:1/", [], lambda *_: None, nom="muet")
    c2._traiter({"identity": {"source_id": "x"}, "payload": {"value": "PGM"}})
    controle("★★★ une valeur hors de NOTRE énumération n'est pas devinée",
             c2.recus == 1,
             "IS-07 laisse le contenu des enums au constructeur : lire « PGM » comme un rouge "
             "serait inventer une convention que l'émetteur n'a pas déclarée")
    # ── Un serveur qui répond « 101 » n'est pas un pair WebSocket ────────
    # ⚠ CE CONTRÔLE A ÉTÉ AJOUTÉ APRÈS UNE MUTATION MUETTE : en boucle sur notre propre serveur,
    # l'Accept est toujours correct, donc retirer sa vérification ne changeait rien. Il faut un
    # imposteur pour le prouver — sans cette garde, n'importe quel service répondant 101 passe
    # pour un pair et on lit ses octets comme des trames.
    import socket as _s
    srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_faux = srv.getsockname()[1]

    def _imposteur():
        try:
            c, _ = srv.accept()
            c.recv(4096)
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      b"Connection: Upgrade\r\nSec-WebSocket-Accept: pasmoi=\r\n\r\n")
            time.sleep(1.5)
            c.close()
        except Exception:
            pass

    threading.Thread(target=_imposteur, daemon=True).start()
    c3 = is07_client.ClientIS07("ws://127.0.0.1:%d/" % port_faux, [], lambda *_: None,
                                nom="imposteur")
    try:
        c3._session()
        refuse = False
    except IOError as e:
        refuse = "Accept" in str(e)
    except Exception:
        refuse = False
    controle("★★★ un serveur qui répond 101 avec un mauvais Accept est REFUSÉ", refuse,
             "sinon n'importe quel service répondant 101 passe pour un pair WebSocket, et on "
             "interprète ses octets comme des trames — du bruit lu comme du tally")
    try:
        c3._fermer()
        srv.close()
    except Exception:
        pass

    # ── wss:// : chiffré, et VÉRIFIÉ ─────────────────────────────────────
    h, prt, chem, tls = is07_client._url("wss://exemple/tally")
    controle("★★ `wss://` est reconnu, et son port par défaut est 443",
             (h, prt, chem, tls) == ("exemple", 443, "/tally", True),
             "obtenu %r" % ((h, prt, chem, tls),))
    controle("★ `ws://` reste en clair sur 80", is07_client._url("ws://x/")[1:] == (80, "/", False))
    controle("★★ un schéma inconnu est refusé", _leve(lambda: is07_client._url("ftp://x/")))

    # Un vrai serveur TLS auto-signé : c'est le seul moyen de prouver que la vérification MORD.
    # Sans lui on ne testerait que la lecture d'une URL — et un client qui accepte n'importe quel
    # certificat lit l'URL tout aussi bien.
    cert, cle, ca = _certificat_bidon()
    srv_tls, port_tls = _serveur_tls(cert, cle)
    try:
        db_set_setting("is07_tls_ca", "")
        db_set_setting("is07_tls_verifier", "1")
        c4 = is07_client.ClientIS07("wss://127.0.0.1:%d/" % port_tls, [], lambda *_: None,
                                    nom="tls")
        # ⚠ ON EXIGE UNE ERREUR DE CERTIFICAT, pas n'importe quelle erreur. Une première version
        # acceptait toute `OSError` — or le serveur de banc répond 400 et non 101, donc la
        # poignée de main WebSocket échoue MÊME QUAND TLS PASSE. Le contrôle était donc vert avec
        # ou sans vérification : une mutation l'a montré.
        err4 = _erreur(c4._session)
        controle("★★★ un certificat non approuvé est REFUSÉ par défaut",
                 isinstance(err4, ssl.SSLCertVerificationError)
                 or "certificate verify failed" in str(err4).lower(),
                 "accepter le premier certificat venu rendrait le chiffrement décoratif : "
                 "n'importe qui sur le chemin pourrait se faire passer pour le contrôleur. "
                 "Obtenu %r (%s)" % (err4, type(err4).__name__))
        c4._fermer()

        db_set_setting("is07_tls_ca", ca)
        c5 = is07_client.ClientIS07("wss://127.0.0.1:%d/" % port_tls, [], lambda *_: None,
                                    nom="tls-ca")
        # La poignée de main WebSocket échouera (le serveur de banc ne répond pas 101), mais le
        # TLS, lui, doit être PASSÉ : c'est ce qu'on mesure, et l'erreur le dit.
        err = _erreur(c5._session)
        controle("★★★ ...et ACCEPTÉ quand la CA du site est déclarée",
                 "certificate" not in str(err).lower() and "ssl" not in type(err).__name__.lower(),
                 "c'est la bonne réponse à un contrôleur auto-signé : la chaîne reste vérifiée, "
                 "contre VOTRE autorité. Obtenu %r" % err)
        c5._fermer()

        db_set_setting("is07_tls_ca", "/n/existe/pas.pem")
        c6 = is07_client.ClientIS07("wss://127.0.0.1:%d/" % port_tls, [], lambda *_: None, nom="x")
        controle("★★★ une CA illisible ARRÊTE, elle ne retombe pas sur le magasin système",
                 "illisible" in str(_erreur(c6._session)),
                 "l'exploitant CROIT vérifier contre son autorité : le laisser sur une "
                 "vérification qu'il n'a pas choisie serait mentir sur ce qui protège")
        c6._fermer()

        db_set_setting("is07_tls_ca", "")
        db_set_setting("is07_tls_verifier", "0")
        c7 = is07_client.ClientIS07("wss://127.0.0.1:%d/" % port_tls, [], lambda *_: None, nom="y")
        err = _erreur(c7._session)
        controle("★★ l'échappatoire explicite passe outre la vérification",
                 "certificate" not in str(err).lower(),
                 "elle existe pour un parc interne, mais elle doit être DEMANDÉE. Obtenu %r" % err)
        c7._fermer()
    finally:
        srv_tls.shutdown(socket.SHUT_RDWR) if False else None
        try:
            srv_tls.close()
        except Exception:
            pass
        db_set_setting("is07_tls_ca", "")
        db_set_setting("is07_tls_verifier", "1")
finally:
    try:
        if client:
            client.arreter()
    except Exception:
        pass
    try:
        from services.nmos import is07 as _i
        _i.arreter()
    except Exception:
        pass
    try:
        from services import tsl as _t
        _t.poser_tally("banc:is07", {})
        residu = _t.get_tally_state()
    except Exception:
        residu = "?"
    db_set_setting("nmos_is07", "0" if AVANT in (None, "", 0, "0", False) else AVANT)
    db_set_setting("nmos_is07_ws", "0" if AVANT_WS in (None, "", 0, "0", False) else AVANT_WS)
    print("\n  réglages restaurés : nmos_is07=%r nmos_is07_ws=%r · tally résiduel : %r"
          % (db_get_setting("nmos_is07"), db_get_setting("nmos_is07_ws"), residu))

print("\n%d contrôle(s) OK, %d en échec." % (len(reussites), len(echecs)))
sys.exit(1 if echecs else 0)
