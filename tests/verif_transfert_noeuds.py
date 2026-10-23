#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Garde : TRANSFERT DE FICHIERS NŒUD → NŒUD (agent ≥ 0.24.0, port 9101).

Une copie de média entre nœuds ne passe plus par l'orchestrateur : le nœud cible télécharge chez le
nœud source, sur un port DÉDIÉ en mTLS, avec une autorisation signée par l'orchestrateur. Ce port
s'ouvre à des NŒUDS (le port de pilotage 9100, lui, reste réservé au contrôleur) : ce qu'il laisse
passer doit être exactement ce qui est autorisé, et rien d'autre.

Le vrai `TransferHandler` de node_agent/agent.py est servi en mTLS sur la boucle locale avec le vrai
contexte (`_make_transfer_ssl_context`), une vraie CA (app/ca.py) et la vraie fabrique
d'autorisations de l'orchestrateur (`node_driver.transfer_grant`). Le téléchargement est le vrai
(`transfer_fetch` → curl). Aucun réseau hors loopback, tout en répertoires jetables.

★ VÉRIFIÉ :
  1. un NŒUD muni d'une autorisation lit le fichier, en entier ou à partir d'un octet (reprise) ;
  2. un CONTENEUR est refusé, même avec une autorisation valide ;
  3. autorisation falsifiée, expirée, signée avec un autre jeton : refusées ;
  4. l'autorisation désigne UN fichier : impossible d'en lire un autre, ni de sortir du volume ;
  5. un fichier qui a changé depuis l'autorisation est refusé (taille) ;
  6. le téléchargement réel arrive entier, à la date d'origine, et REPREND un `.partial` interrompu ;
  7. côté orchestrateur : un nœud n'est « capable » qu'avec un agent ≥ 0.24.0 ET le TLS ;
  8. le CONTRÔLEUR lit aussi sur ce port (export d'un projet avec ses médias) ;
  9. dépôt d'un fichier par le contrôleur (agent ≥ 0.25.0) : entier, daté, jamais par-dessus un
     fichier existant, jamais hors du volume, jeton exigé ;
 10. agent 0.27.0 : plage FERMÉE `bytes=a-b` → 206, Content-Length et Content-Range exacts ;
     `bytes=-n` ; 416 + `bytes */T` hors fichier ; `bytes=T-` toujours 206 vide (reprise curl) ;
     la connexion RESTE OUVERTE entre deux requêtes (même après un refus), et l'inactivité la ferme ;
 11. contrôleur : UN pool par nœud — 20 lectures = 1 connexion ; lecture d'un fichier entier en
     plages exactes ; lecture abandonnée → connexion fermée, jamais rendue avec des octets non lus ;
     lectures simultanées justes, connexions gardées bornées ; pool fermé après inactivité ;
     agent < 0.27.0 : repli d'avant (`bytes=a-` coupé, une connexion par lecture).
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
import time
from urllib.parse import quote

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)
ECHECS = []
JETON = "jeton-du-noeud-source-0123456789"


def verifier(cond, libelle):
    print(("  ok   " if cond else "  ÉCHEC ") + libelle)
    if not cond:
        ECHECS.append(libelle)


def ecrire(d, nom, data):
    p = os.path.join(d, nom)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    return p


def port_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    from app import config, ca
    config.TLS_DIR = tempfile.mkdtemp(prefix="ctrl-")
    ca.create_ca_material(["127.0.0.1"])
    P = ca.paths()

    # L'agent du nœud SOURCE (et, plus bas, il fait aussi office de nœud cible : même code).
    tls = tempfile.mkdtemp(prefix="noeud-tls-")
    crt, key = ca.generate_leaf("noeud-1", ip="127.0.0.1", node_id=1)
    ecrire(tls, "node.crt", crt)
    ecrire(tls, "node.key", key)
    ecrire(tls, "ca.crt", ca.ca_cert_pem())
    media = tempfile.mkdtemp(prefix="volume-")
    cfg = ecrire(tempfile.mkdtemp(), "config.json",
                 json.dumps({"token": JETON, "tls_dir": tls, "media_mount": media, "capabilities": []}))
    os.environ["BOBI_NODE_AGENT_CONFIG"] = cfg
    spec = importlib.util.spec_from_file_location("bobi_node_agent", os.path.join(RACINE, "node_agent", "agent.py"))
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)

    os.makedirs(os.path.join(media, "clips"))
    contenu = os.urandom(3 * 1024 * 1024 + 17)
    ecrire(os.path.join(media, "clips"), "match.mxf", contenu)
    ecrire(os.path.join(media, "clips"), "autre.mxf", b"secret")
    os.utime(os.path.join(media, "clips", "match.mxf"), (1700000000, 1700000000))

    port = port_libre()
    httpd = agent.ThreadingHTTPServer(("127.0.0.1", port), agent.TransferHandler)
    httpd.socket = agent._make_transfer_ssl_context().wrap_socket(httpd.socket, server_side=True)
    httpd.handle_error = lambda *a: None
    ACCEPTS = []                                    # une entrée par connexion TCP acceptée par l'agent
    httpd.verify_request = lambda req, addr: ACCEPTS.append(addr) or True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    from app import node_driver
    source = {"agent_token": JETON, "host": "127.0.0.1"}

    tmp = tempfile.mkdtemp(prefix="clients-")
    noeud2 = ca.generate_leaf("noeud-2", ip="127.0.0.1", node_id=2)
    conteneur = ca.generate_leaf("mxl901", uri="bobi://container/901")

    def lire(cert, grant, rng=None):
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=P["ca_cert"])
        ctx.load_cert_chain(ecrire(tmp, "c.crt", cert[0]), ecrire(tmp, "c.key", cert[1]))
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
        try:
            c.request("GET", "/v1/transfer/read?g=" + quote(grant), headers={"Range": rng} if rng else {})
            r = c.getresponse()
            return r.status, r.read()
        except Exception as e:
            return "exception %s" % type(e).__name__, b""
        finally:
            c.close()

    g = node_driver.transfer_grant(source, "clips/match.mxf", len(contenu))
    print("\nQui lit, quoi")
    st, data = lire(noeud2, g)
    verifier(st == 200 and data == contenu, "un NŒUD autorisé lit le fichier entier")
    st, data = lire(noeud2, g, "bytes=1000-")
    verifier(st == 206 and data == contenu[1000:], "… ou à partir d'un octet (reprise)")
    verifier(lire(conteneur, g)[0] == 403, "★ un CONTENEUR est refusé, même avec une autorisation valide")
    body, sig = g.rsplit(".", 1)
    verifier(lire(noeud2, body + "." + "0" * len(sig))[0] == 403, "autorisation falsifiée → 403")
    verifier(lire(noeud2, node_driver.transfer_grant({"agent_token": "autre"}, "clips/match.mxf", len(contenu)))[0] == 403,
             "autorisation signée avec le jeton d'un AUTRE nœud → 403")
    verifier(lire(noeud2, node_driver.transfer_grant(source, "clips/match.mxf", len(contenu), ttl_s=-5))[0] == 403,
             "autorisation expirée → 403")
    verifier(lire(noeud2, node_driver.transfer_grant(source, "../" + os.path.basename(cfg), None))[0] == 404,
             "★ sortir du volume média (../) → refusé")
    verifier(lire(noeud2, node_driver.transfer_grant(source, "clips/match.mxf", 12))[0] == 409,
             "fichier changé depuis l'autorisation (taille) → 409")
    # Le chemin est DANS l'autorisation : aucun paramètre de requête ne le change.
    st, data = lire(noeud2, g + "&p=clips/autre.mxf")
    verifier(data != b"secret", "★ l'autorisation désigne UN fichier : impossible d'en lire un autre")

    print("\nTéléchargement réel (curl, basse priorité)")
    url = "https://127.0.0.1:%d/v1/transfer/read?g=%s" % (port, quote(g))
    agent.CONFIG["media_mount"] = media
    code, r = agent.transfer_fetch({"id": "t1", "url": url, "dest": "copie/match.mxf", "size": len(contenu),
                                    "mtime": 1700000000, "rate": 50_000_000})
    for _ in range(100):
        time.sleep(0.1)
        code, st = agent.transfer_status("t1")
        if st.get("state") != "running":
            break
    dest = os.path.join(media, "copie", "match.mxf")
    verifier(st.get("state") == "done" and open(dest, "rb").read() == contenu, "le fichier arrive entier")
    verifier(int(os.path.getmtime(dest)) == 1700000000, "à la date d'origine (sinon il passe pour « en cours »)")
    part = os.path.join(media, "copie", "reprise.mxf.partial")
    ecrire(os.path.dirname(part), "reprise.mxf.partial", contenu[:1_000_000])
    agent.transfer_fetch({"id": "t2", "url": url, "dest": "copie/reprise.mxf", "size": len(contenu)})
    for _ in range(100):
        time.sleep(0.1)
        code, st = agent.transfer_status("t2")
        if st.get("state") != "running":
            break
    verifier(st.get("state") == "done" and open(os.path.join(media, "copie", "reprise.mxf"), "rb").read() == contenu,
             "★ un .partial interrompu est REPRIS, et le résultat est entier")
    code, r = agent.transfer_fetch({"id": "t3", "url": url, "dest": "../dehors.mxf"})
    verifier(code == 400, "destination hors du volume refusée")

    print("\nCôté orchestrateur")
    node_driver._tls_on = lambda n: bool(n.get("tls_ready"))
    node_driver.has_agent = lambda n: True
    verifier(node_driver.transfer_capable({"agent_version": "0.24.0", "tls_ready": True}), "agent 0.24.0 + TLS → direct")
    verifier(not node_driver.transfer_capable({"agent_version": "0.23.0", "tls_ready": True}), "agent 0.23.0 → repli")
    verifier(not node_driver.transfer_capable({"agent_version": "0.24.0", "tls_ready": False}), "sans TLS → repli")

    verifier(node_driver.upload_capable({"agent_version": "0.25.0", "agent_url": "http://x"}), "agent 0.25.0 → dépôt direct")
    verifier(not node_driver.upload_capable({"agent_version": "0.24.0", "agent_url": "http://x"}), "agent 0.24.0 → repli")

    print("\nLe contrôleur lit un nœud (export d'un projet avec ses médias)")
    node_driver.TRANSFER_PORT = port
    r = node_driver.transfer_open(source, "clips/match.mxf", len(contenu))
    verifier(r is not None and r.content == contenu, "le certificat du CONTRÔLEUR est accepté sur le port de transfert")
    verifier(node_driver.transfer_open(source, "clips/match.mxf", 12) is None, "taille annoncée fausse → rien n'est lu")

    print("\nAgent 0.27.0 : plages fermées, connexion gardée")

    def conn_neuve(cert=noeud2):
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=P["ca_cert"])
        ctx.load_cert_chain(ecrire(tmp, "k.crt", cert[0]), ecrire(tmp, "k.key", cert[1]))
        return http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)

    def req(c, rng=None, grant=g):
        c.request("GET", "/v1/transfer/read?g=" + quote(grant), headers={"Range": rng} if rng else {})
        r = c.getresponse()
        return r.status, r.getheader("Content-Range"), r.getheader("Content-Length"), r.read()

    T = len(contenu)
    c = conn_neuve()
    n0 = len(ACCEPTS)
    st, cr, cl, data = req(c, "bytes=1000-1999")
    verifier(st == 206 and cr == "bytes 1000-1999/%d" % T and cl == "1000" and data == contenu[1000:2000],
             "★ bytes=1000-1999 → 206, Content-Range et Content-Length EXACTS, octets exacts")
    sock1 = c.sock
    st, cr, cl, data = req(c, "bytes=%d-%d" % (T - 10, T + 500))
    verifier(st == 206 and cr == "bytes %d-%d/%d" % (T - 10, T - 1, T) and data == contenu[-10:],
             "fin au-delà du fichier : bornée à la taille")
    st, cr, cl, data = req(c, "bytes=-100")
    verifier(st == 206 and data == contenu[-100:], "bytes=-100 : les 100 derniers octets")
    st, cr, cl, data = req(c, "bytes=%d-%d" % (T, T + 9))
    verifier(st == 416 and cr == "bytes */%d" % T, "★ début ≥ taille → 416 + Content-Range bytes */T")
    st, cr, cl, data = req(c, "bytes=%d-" % T)
    verifier(st == 206 and data == b"", "rétrocompatible : bytes=T- → 206 vide (reprise curl d'un fichier complet)")
    st, cr, cl, data = req(c, "bytes=0-")
    verifier(st == 200 and data == contenu, "bytes=0- → 200, fichier entier")
    verifier(c.sock is sock1 and len(ACCEPTS) == n0 + 1,
             "★ KEEP-ALIVE : six requêtes (dont un 416) sur UNE seule connexion")
    c.close()
    verifier(0 < (agent.TransferHandler.timeout or 0) <= 300, "délai d'inactivité de l'agent BORNÉ (%s s)"
             % agent.TransferHandler.timeout)
    agent.TransferHandler.timeout = 0.5
    c = conn_neuve()
    req(c, "bytes=0-9")
    time.sleep(1.2)
    try:
        ferme = c.sock.recv(1) == b""
    except Exception:                                                      # noqa: BLE001
        ferme = True
    verifier(ferme, "★ connexion inactive fermée par l'agent (délai d'inactivité)")
    c.close()
    agent.TransferHandler.timeout = 120

    print("\nContrôleur : pool de connexions par nœud")
    from app import media_registry as mr
    neuf = dict(source, agent_version="0.27.0", tls_ready=True, name="noeud-1")
    vieux = dict(source, agent_version="0.26.0", tls_ready=True, name="noeud-1")
    verifier(node_driver.transfer_range_capable(neuf) and not node_driver.transfer_range_capable(vieux),
             "plages exactes : agent ≥ 0.27.0 seulement")
    n0 = len(ACCEPTS)
    import random
    ok = True
    for i in range(20):
        a = random.randrange(0, T - 300000)
        ok &= b"".join(mr.read_node_range(neuf, "clips/match.mxf", T, a, 300000)) == contenu[a:a + 300000]
    verifier(ok and len(ACCEPTS) == n0 + 1, "★ 20 lectures de plage : octets exacts, UNE connexion (%d)" % (len(ACCEPTS) - n0))
    verifier(b"".join(mr.read_node_range(neuf, "clips/match.mxf", T, 0, T)) == contenu and len(ACCEPTS) == n0 + 1,
             "fichier entier (> 1 plage) lu en plages exactes, toujours la même connexion")
    pool = node_driver.transfer_pool(neuf)
    verifier(pool.num_connections == 1 and pool.pool.qsize() == node_driver.TRANSFER_POOL_MAXSIZE,
             "la connexion est RENDUE au pool après chaque lecture complète")
    gen = mr.read_node_range(neuf, "clips/match.mxf", T, 0, T)
    next(gen)
    gen.close()                                     # le navigateur est parti au milieu
    verifier(b"".join(mr.read_node_range(neuf, "clips/match.mxf", T, 5, 40)) == contenu[5:45],
             "lecture ABANDONNÉE au milieu : la lecture suivante reste exacte (connexion fermée par nous ; urllib3 écarte aussi une connexion qui a des octets en attente)")
    res = []

    def lecteur(k):
        a = 1000 * k
        res.append(b"".join(mr.read_node_range(neuf, "clips/match.mxf", T, a, 200000)) == contenu[a:a + 200000])
    th = [threading.Thread(target=lecteur, args=(k,)) for k in range(10)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    garde = sum(1 for _ in range(pool.pool.qsize()) if pool.pool.queue[_] is not None)
    verifier(len(res) == 10 and all(res) and garde <= node_driver.TRANSFER_POOL_MAXSIZE,
             "★ 10 lectures SIMULTANÉES exactes ; connexions gardées bornées (%d ≤ %d)"
             % (garde, node_driver.TRANSFER_POOL_MAXSIZE))
    n0 = len(ACCEPTS)
    ok = all(b"".join(mr.read_node_range(vieux, "clips/match.mxf", T, a, 5000)) == contenu[a:a + 5000]
             for a in (0, 7, 100000, T - 5000))
    verifier(ok and len(ACCEPTS) == n0 + 4, "★ agent 0.26.0 : repli d'avant, exact, une connexion par lecture (%d)"
             % (len(ACCEPTS) - n0))
    verifier(node_driver._tpool_reaper is not None and node_driver._tpool_reaper.is_alive(),
             "un fil de ménage surveille les pools ouverts")
    node_driver.TRANSFER_POOL_IDLE_S = 0.4
    time.sleep(0.6)
    node_driver._tpool_sweep()                      # ce que fait ce fil, sans attendre son réveil
    verifier(not node_driver._tpools, "pool FERMÉ après inactivité (aucune connexion gardée à vie)")
    n0 = len(ACCEPTS)
    verifier(b"".join(mr.read_node_range(neuf, "clips/match.mxf", T, 9, 9)) == contenu[9:18] and len(ACCEPTS) == n0 + 1,
             "… et rouvert à la lecture suivante")
    node_driver.TRANSFER_POOL_IDLE_S = 30.0

    print("\nDépôt d'un fichier par le contrôleur (import d'un projet avec ses médias)")
    # Le port de pilotage, servi en clair ici : seul le jeton est en jeu (le pair TLS est vérifié
    # par ailleurs, cf. verif_agent_* ).
    p2 = port_libre()
    h2 = agent.ThreadingHTTPServer(("127.0.0.1", p2), agent.Handler)
    h2.handle_error = lambda *a: None
    threading.Thread(target=h2.serve_forever, daemon=True).start()
    noeud = {"agent_url": "http://127.0.0.1:%d" % p2, "agent_token": JETON, "agent_version": "0.25.0"}
    depot = os.urandom(2 * 1024 * 1024 + 5)
    import io
    ok, msg = node_driver.upload_file(noeud, "projet/rush/depot.mxf", io.BytesIO(depot), len(depot), mtime=1600000000)
    cible = os.path.join(media, "projet", "rush", "depot.mxf")
    verifier(ok and open(cible, "rb").read() == depot, "fichier déposé entier, dossiers créés")
    verifier(int(os.path.getmtime(cible)) == 1600000000, "à la date d'origine")
    verifier(not os.path.exists(cible + ".partial"), "pas de .partial résiduel")
    ok, msg = node_driver.upload_file(noeud, "projet/rush/depot.mxf", io.BytesIO(b"autre"), 5)
    verifier(not ok and open(cible, "rb").read() == depot, "★ jamais par-dessus un fichier existant")
    ok, msg = node_driver.upload_file(noeud, "../hors-volume.mxf", io.BytesIO(b"x"), 1)
    verifier(not ok and not os.path.exists(os.path.join(os.path.dirname(media), "hors-volume.mxf")),
             "★ destination hors du volume refusée")
    ok, msg = node_driver.upload_file(dict(noeud, agent_token="mauvais"), "projet/x.mxf", io.BytesIO(b"x"), 1)
    verifier(not ok and not os.path.exists(os.path.join(media, "projet", "x.mxf")), "mauvais jeton → refusé")
    h2.shutdown()

    httpd.shutdown()
    print()
    if ECHECS:
        print("ÉCHEC — %d vérification(s) :" % len(ECHECS))
        for e in ECHECS:
            print("  - " + e)
        sys.exit(1)
    print("OK — %s" % "transfert nœud → nœud conforme")


if __name__ == "__main__":
    main()
