#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Le serveur MCP de lecture (`tools/mcp_bobi.py`) — poignée de main, surface, et la propriété
qui compte : il ne peut RIEN écrire.

Hors ligne : aucun appel réseau, le transport est remplacé par un bouchon. Ce qu'on vérifie
n'est pas « est-ce que ça répond » mais « est-ce que ça répond ce que le protocole attend » —
une implémentation de protocole écrite de mémoire se croit juste jusqu'au premier vrai client."""
import io
import json
import os
import sys

sys.path.insert(0, "/opt/bobistudio")
sys.path.insert(0, "/opt/bobistudio/tools")
os.environ.setdefault("BOBI_TOKEN", "bobi_bouchon")
import mcp_bobi as M                                                      # noqa: E402

tout = True


def cas(nom, attendu, obtenu):
    global tout
    ok = attendu == obtenu
    tout = tout and ok
    print("%s %-50s %s" % ("OK  " if ok else "✕ KO", nom,
                           "" if ok else "attendu=%r obtenu=%r" % (attendu, obtenu)))


class OrchBouchon:
    """Remplace le transport. Retient ce qui a été demandé : on vérifie le CHEMIN et les
    paramètres, pas le contenu — c'est l'orchestrateur qui décide du contenu."""

    def __init__(self):
        self.vus = []

    def get(self, chemin, params=None):
        self.vus.append((chemin, params))
        return {"bouchon": chemin, "params": params}


def rep(methode, params=None, ident=1, orch=None):
    msg = {"jsonrpc": "2.0", "id": ident, "method": methode}
    if params is not None:
        msg["params"] = params
    return M.traite(msg, orch or OrchBouchon())


print("── poignée de main ──")
r = rep("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
cas("initialize : la version demandée est rendue", "2025-06-18", r["result"]["protocolVersion"])
r2 = rep("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
cas("version inconnue → repli déclaré", M.PROTOCOLES[-1], r2["result"]["protocolVersion"])
cas("capacité tools annoncée", True, "tools" in r["result"]["capabilities"])
cas("serverInfo nommé", "bobi", r["result"]["serverInfo"]["name"])
cas("jsonrpc 2.0 dans la réponse", "2.0", r["jsonrpc"])
cas("l'id est renvoyé tel quel", 1, r["id"])

print("── notifications : aucune réponse (sinon le client casse) ──")
cas("notifications/initialized", None,
    M.traite({"jsonrpc": "2.0", "method": "notifications/initialized"}, OrchBouchon()))
cas("notification inconnue", None,
    M.traite({"jsonrpc": "2.0", "method": "notifications/cancelled"}, OrchBouchon()))

print("── catalogue d'outils ──")
outils = rep("tools/list")["result"]["tools"]
cas("tous les outils déclarés sont listés", len(M.OUTILS), len(outils))
cas("chaque outil a un schéma d'entrée", True, all("inputSchema" in o for o in outils))
cas("chaque outil a une description non vide", True,
    all(len(o.get("description") or "") > 30 for o in outils))
cas("schémas fermés (pas de paramètre surprise)", True,
    all(o["inputSchema"].get("additionalProperties") is False for o in outils))

print("── appel d'outil ──")
o = OrchBouchon()
r = M.traite({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
              "params": {"name": "noeuds", "arguments": {}}}, o)
cas("le bon chemin est appelé", [("/api/nodes", None)], o.vus)
cas("le contenu est du texte", "text", r["result"]["content"][0]["type"])
cas("pas marqué en erreur", False, bool(r["result"].get("isError")))

o = OrchBouchon()
M.traite({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
          "params": {"name": "alertes", "arguments": {"vmid": 619, "kind": None}}}, o)
cas("les paramètres passent, les None sont écartés",
    [("/api/alerts", {"vmid": 619})], o.vus)

r = M.traite({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
              "params": {"name": "inexistant", "arguments": {}}}, OrchBouchon())
cas("outil inconnu → résultat en erreur, PAS une erreur de protocole",
    (True, True), (bool(r["result"]["isError"]), "error" not in r))


class OrchCasse:
    def get(self, chemin, params=None):
        raise RuntimeError("orchestrateur injoignable sur http://exemple")


r = M.traite({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
              "params": {"name": "noeuds"}}, OrchCasse())
cas("panne de transport → isError + cause lisible",
    (True, True), (bool(r["result"]["isError"]),
                   "injoignable" in r["result"]["content"][0]["text"]))

print("── méthode non gérée ──")
r = rep("resources/list")
cas("code JSON-RPC « méthode inconnue »", -32601, r["error"]["code"])

print("── LECTURE SEULE, par construction ──")
src = io.open("/opt/bobistudio/tools/mcp_bobi.py", encoding="utf-8").read()
corps = src.split('"""', 2)[2]          # hors docstring de module : le CODE seul
# ZÉRO POST depuis le passage au jeton d'API : il n'y a plus de session à ouvrir, donc plus
# aucune écriture possible, même par accident. C'était 1 (le login) jusqu'au 2026-10-10.
cas("aucun POST dans tout le code", 0, corps.count('method="POST"'))
cas("aucun DELETE / PUT / PATCH", 0,
    sum(corps.count('method="%s"' % v) for v in ("DELETE", "PUT", "PATCH")))
cas("tous les chemins exposés sont sous /api/", True,
    all(o["chemin"].startswith("/api/") for o in M.OUTILS.values()))

# La garde du transport : un chemin hors surface doit être refusé AVANT tout réseau. Sans ça,
# un appelant futur qui calculerait un chemin pourrait atteindre une route d'écriture.
vrai = M.Orchestrateur()
try:
    vrai.get("/api/containers/619/destroy")
    cas("chemin hors surface refusé", "refus", "PASSÉ")
except RuntimeError as e:
    cas("chemin hors surface refusé avant tout réseau", True,
        "hors de la surface" in str(e))

print("── boucle stdio de bout en bout ──")
entree = io.StringIO("\n".join([
    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}}),
    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    "",                                                     # ligne vide : ignorée
    "ceci n'est pas du json",                               # illisible : ignorée, pas fatale
    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
]) + "\n")
sortie = io.StringIO()
M.boucle(entree, sortie)
reponses = [json.loads(l) for l in sortie.getvalue().strip().split("\n")]
cas("3 réponses pour 6 lignes (notif, vide et illisible n'en valent pas)", 3, len(reponses))
cas("les ids suivent l'ordre", [1, 2, 3], [r["id"] for r in reponses])
cas("une ligne illisible ne tue pas la boucle", True, reponses[-1]["result"] == {})

print("── mode web tls : notre CA, et jamais de redirection suivie ──")
# Défaut trouvé en documentant (2026-10-11) : `urlopen` sans contexte ne connaît que les CA du
# système → en mode tls, le serveur ne pouvait PAS joindre l'orchestrateur. Et sur le :5000, il
# SUIVAIT la redirection vers HTTPS : le jeton partait en clair, puis repartait.
# Écoutes RÉELLES sur la boucle locale, CA jetable : seul le transport est éprouvé ici.
import http.server
import ssl
import tempfile
import threading
from app import config, ca
config.TLS_DIR = tempfile.mkdtemp(prefix="mcp-ca-")
ca.create_ca_material(["127.0.0.1"])
recus = []


class Repond(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        recus.append((self.server.nom, self.headers.get("Authorization")))
        if self.server.nom == "http":
            self.send_response(301)
            self.send_header("Location", "https://127.0.0.1:%d%s" % (https.server_port, self.path))
            self.end_headers()
            return
        corps = b'{"ok": 1}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)


https = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Repond)
https.nom = "https"
ctx_srv = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx_srv.load_cert_chain(ca.paths()["ctrl_cert"], ca.paths()["ctrl_key"])
https.socket = ctx_srv.wrap_socket(https.socket, server_side=True)
https.handle_error = lambda *a: None          # la poignée de main refusée coupe net, c'est voulu
clair = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Repond)
clair.nom = "http"
for srv in (https, clair):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
chemin = next(iter(M.OUTILS.values()))["chemin"]


def essai(base, ca_crt):
    M.BASE, M.CA = base, ca_crt
    try:
        return M.Orchestrateur().get(chemin), None
    except Exception as e:
        return None, str(e)


r, err = essai("https://127.0.0.1:%d" % https.server_port, ca.paths()["ca_cert"])
cas("★ https + BOBI_CA : l'orchestrateur répond", {"ok": 1}, r)
cas("… le jeton est bien présenté", ("https", "Bearer bobi_bouchon"), recus[-1] if recus else None)
r, err = essai("https://127.0.0.1:%d" % https.server_port, "")
cas("https SANS BOBI_CA : refus qui nomme BOBI_CA", True, r is None and "BOBI_CA" in (err or ""))
del recus[:]
r, err = essai("http://127.0.0.1:%d" % clair.server_port, ca.paths()["ca_cert"])
cas("★ :5000 qui redirige : la redirection n'est PAS suivie", ["http"], [n for n, _ in recus])
cas("… et l'erreur nomme l'adresse https à viser", True,
    r is None and ("https://127.0.0.1:%d" % https.server_port) in (err or ""))
for srv in (https, clair):
    srv.shutdown()

print()
print("TOUT PASSE" if tout else "ÉCHEC")
sys.exit(0 if tout else 1)
