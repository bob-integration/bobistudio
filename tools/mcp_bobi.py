#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Serveur MCP Bobi.Studio — surface de LECTURE de l'orchestrateur pour un agent.

POURQUOI : un agent qui diagnostique le cluster le fait aujourd'hui par `ssh root@` et `curl`
sur les nœuds. C'est un canal LATÉRAL : il ignore les habilitations du produit, n'apparaît dans
aucun journal applicatif, et son autorisation se règle par motifs sur des lignes de commande.
Passer par MCP déplace les trois : l'autorisation devient par OPÉRATION NOMMÉE (règles
`mcp__bobi__*`), l'accès emprunte un COMPTE Bobi.Studio avec ses habilitations, et les actions
retombent dans la piste d'audit du produit.

PÉRIMÈTRE — LECTURE SEULE, par CONSTRUCTION et pas par convention : `_get()` est le seul
transport, il n'émet que des GET, et seuls les chemins de `OUTILS` sont atteignables. Le seul
POST n'existe plus depuis le passage aux jetons. Il n'existe donc aucun chemin de code capable de
déployer, redémarrer ou détruire quoi que ce soit — un outil d'écriture exigerait d'ouvrir ce
transport, ce qui se voit en revue.

STDLIB SEULE : pas de paquet `mcp`. Le venv est partagé avec le produit et une dépendance n'y
entre pas pour de l'outillage. Le protocole stdio est du JSON-RPC 2.0 délimité par des sauts de
ligne — 150 lignes suffisent, et on ne subit pas le versionnement d'un tiers.

AUTHENTIFICATION : un JETON D'API, porté par un compte Bobi.Studio. Le jeton emprunte les
habilitations de son compte (`auth.current_user`), donc pour un accès en lecture seule on lui
donne un compte en lecture seule — le jeton ne porte pas de droits propres. Il se crée et se
révoque depuis l'API (`/api/api-tokens`, permission `settings.edit`) ou `tools/jeton_api.py`.
Conséquence heureuse : ce fichier n'émet PLUS AUCUN POST, le périmètre de lecture n'est plus
seulement une discipline mais une propriété du code.

⚠ Un jeton voyage comme un mot de passe. En mode web `http` (le défaut), le `:5000` est en
CLAIR : à réserver à un réseau maîtrisé. En mode `tls` (Réglages → Sécurité → Accès web), viser
l'écoute HTTPS — `BOBI_URL=https://<contrôleur>:443` — avec `BOBI_CA` pointant le certificat de
la CA interne (`tls/ca.crt` du contrôleur), qu'aucun système ne connaît d'office. Une
REDIRECTION n'est jamais suivie : en tls, le `:5000` renvoie vers HTTPS, et la suivre aurait
d'abord envoyé le jeton en clair, puis l'aurait renvoyé. On s'arrête en nommant la bonne
adresse. (Le mTLS du plan de contrôle `:9100` est un autre canal, par lequel aucun jeton ne
passe.)

CONFIGURATION par l'environnement (jamais dans le dépôt) :
    BOBI_URL    défaut http://127.0.0.1:5000
    BOBI_CA     certificat de la CA interne, si BOBI_URL est en https (mode web tls)
    BOBI_TOKEN  jeton d'API (« bobi_… ») d'un compte dédié, de préférence en lecture seule

ENREGISTREMENT (à faire par un humain — accorder un accès n'est pas à l'agent de le faire) :
    claude mcp add bobi --env BOBI_URL=http://192.0.2.10:5000 \\
      --env BOBI_TOKEN='bobi_…' \\
      -- /opt/bobistudio/venv/bin/python /opt/bobistudio/tools/mcp_bobi.py

GARDE (hors ligne, aucun réseau) :
    ./venv/bin/python tests/verif_mcp_bobi.py
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
PROTOCOLES = ("2025-06-18", "2025-03-26", "2024-11-05")

BASE = (os.environ.get("BOBI_URL") or "http://127.0.0.1:5000").rstrip("/")
JETON = os.environ.get("BOBI_TOKEN") or ""
CA = os.environ.get("BOBI_CA") or ""


class _PasDeRedirection(urllib.request.HTTPRedirectHandler):
    """Une redirection n'est JAMAIS suivie (cf. l'en-tête du fichier) : on la rend en erreur."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError(
            "l'orchestrateur redirige vers %s — il est en mode web tls. Visez cette adresse "
            "(BOBI_URL) avec BOBI_CA = certificat de sa CA interne. Le jeton vient de passer EN "
            "CLAIR une fois : s'il a traversé un réseau non maîtrisé, révoquez-le." % newurl)


def _ouvreur():
    ctx = ssl.create_default_context(cafile=CA) if CA else None
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                       _PasDeRedirection())

# ─── Surface exposée ─────────────────────────────────────────────────────────
# Un outil = un nom, une description, un schéma d'entrée, et UN chemin de l'API de lecture.
# Les descriptions sont ce que l'agent lit pour choisir : elles disent à quoi sert la donnée,
# pas seulement ce qu'elle contient — un libellé « liste les nœuds » ne dit pas quand s'en
# servir. Les paramètres optionnels deviennent une query string.
_SANS_PARAM = {"type": "object", "properties": {}, "additionalProperties": False}

OUTILS = {
    "noeuds": {
        "chemin": "/api/nodes",
        "description": "Les nœuds enrôlés : nom, hôte, état, réseau Docker, montage MXL, "
                       "identité du domaine MXL. À lire d'abord pour savoir sur quoi on "
                       "travaille — les vmid et les hostnames des autres outils s'y rattachent.",
        "schema": _SANS_PARAM,
    },
    "sante_noeuds": {
        "chemin": "/api/nodes/health",
        "description": "Santé échantillonnée des nœuds : CPU, RAM, disque, PTP, GPU, RDMA, "
                       "capteurs. C'est la vue AGRÉGÉE tenue par le contrôleur, donc la même "
                       "que celle des pages — préférer ceci à une lecture de /proc sur le nœud.",
        "schema": _SANS_PARAM,
    },
    "conteneurs": {
        "chemin": "/api/containers",
        "description": "Les conteneurs managés : vmid, hostname, type de plugin, nœud, statut "
                       "Docker, fps. Un statut `script_stopped` signifie conteneur debout mais "
                       "script arrêté — ce n'est pas la même panne qu'un conteneur tombé.",
        "schema": _SANS_PARAM,
    },
    "alertes": {
        "chemin": "/api/alerts",
        "description": "Les alertes récentes, les plus fraîches d'abord. Filtrables par vmid, "
                       "node_id ou kind. Première chose à lire devant un symptôme : la plupart "
                       "des pannes du cluster y ont déjà déposé une ligne, souvent datée.",
        "schema": {
            "type": "object",
            "properties": {
                "vmid": {"type": "integer", "description": "n'garder que ce conteneur"},
                "node_id": {"type": "integer", "description": "n'garder que ce nœud"},
                "kind": {"type": "string", "description": "catégorie (vocabulaire fermé "
                                                          "database.ALERT_KINDS)"},
                "limit": {"type": "integer", "description": "nombre de lignes (défaut 1000)"},
            },
            "additionalProperties": False,
        },
    },
    "topologie": {
        "chemin": "/api/home/summary",
        "description": "Le câblage vivant : qui produit quel flux MXL, qui le consomme, par "
                       "nœud. C'est la source du schéma de la page d'accueil. À lire pour "
                       "comprendre une chaîne avant d'incriminer un maillon.",
        "schema": _SANS_PARAM,
    },
    "reseaux_2110": {
        "chemin": "/api/media_networks",
        "description": "Les réseaux ST 2110 et leur horloge : domaine PTP, grandmaster, verrou, "
                       "offset. Rouge et bleu sont deux legs d'UN réseau, pas deux réseaux.",
        "schema": _SANS_PARAM,
    },
    "ptp": {
        "chemin": "/api/ptp/status",
        "description": "État PTP détaillé par nœud et par domaine : verrou, offset, mean path "
                       "delay, source de l'horloge. Un nœud à moteur 2110 tient son horloge par "
                       "libmtl, pas par ptp4l — les deux ne se lisent pas au même endroit.",
        "schema": _SANS_PARAM,
    },
}


# ─── Transport ───────────────────────────────────────────────────────────────

class Orchestrateur:
    """Accès HTTP en lecture à l'orchestrateur, par jeton d'API.

    Il n'y a PLUS DE SESSION à ouvrir : chaque requête porte son en-tête `Authorization`. Donc
    plus de cookie à garder, plus de reprise sur session expirée, et surtout plus de POST dans
    ce fichier — la lecture seule devient une propriété du code et non une discipline.

    Le jeton reste en mémoire et n'est JAMAIS journalisé, même tronqué : un préfixe suffit à
    rapprocher deux traces, et une trace est faite pour être lue."""

    def get(self, chemin, params=None):
        """GET sur un chemin de `OUTILS`. SEUL transport du fichier.

        Le chemin est confronté à la surface déclarée : même si un appelant futur calculait un
        chemin, il ne pourrait pas atteindre une route d'écriture."""
        permis = {o["chemin"] for o in OUTILS.values()}
        if chemin not in permis:
            raise RuntimeError("chemin %r hors de la surface déclarée" % chemin)
        if not JETON:
            raise RuntimeError(
                "BOBI_TOKEN absent de l'environnement — ce serveur n'invente pas d'accès. "
                "Créez un jeton (Réglages, ou tools/jeton_api.py) et passez-le à "
                "`claude mcp add` ; voir l'en-tête de ce fichier.")
        url = BASE + chemin
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET",
                                     headers={"Authorization": "Bearer " + JETON,
                                              "Accept": "application/json"})
        try:
            with _ouvreur().open(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # On NOMME la cause probable : un 401 sur un jeton, c'est révoqué ou faux ; un
                # 403, c'est le compte qui n'a pas le droit. Les confondre fait chercher au
                # mauvais endroit pendant longtemps.
                quoi = ("jeton refusé (inconnu ou révoqué)" if e.code == 401 else
                        "compte sans le droit requis pour " + chemin)
                raise RuntimeError("accès refusé par l'orchestrateur : %s" % quoi)
            raise RuntimeError("HTTP %s sur %s — %s"
                               % (e.code, chemin, e.read(200).decode("utf-8", "replace")))
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                raise RuntimeError(
                    "certificat de l'orchestrateur non reconnu (%s). En mode web tls, passez "
                    "BOBI_CA = chemin du certificat de sa CA interne (tls/ca.crt)."
                    % (e.reason.verify_message or e.reason))
            raise
        except urllib.error.URLError as e:
            raise RuntimeError("orchestrateur injoignable sur %s — %s" % (BASE, e.reason))


# ─── Protocole MCP (JSON-RPC 2.0 sur stdio, messages délimités par \n) ───────

def _reponse(ident, resultat):
    return {"jsonrpc": "2.0", "id": ident, "result": resultat}


def _erreur(ident, code, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _liste_outils():
    return {"tools": [
        {"name": nom, "description": o["description"], "inputSchema": o["schema"]}
        for nom, o in OUTILS.items()]}


def _appel(orch, params):
    nom = (params or {}).get("name")
    o = OUTILS.get(nom)
    if o is None:
        return {"content": [{"type": "text", "text": "outil inconnu : %r" % nom}],
                "isError": True}
    args = {k: v for k, v in ((params or {}).get("arguments") or {}).items() if v is not None}
    try:
        data = orch.get(o["chemin"], args or None)
    except Exception as e:
        # L'échec revient comme RÉSULTAT en erreur, pas comme erreur JSON-RPC : l'agent doit
        # pouvoir lire la cause et enchaîner, pas voir sa requête rejetée par le protocole.
        return {"content": [{"type": "text", "text": "%s" % e}], "isError": True}
    return {"content": [{"type": "text",
                         "text": json.dumps(data, ensure_ascii=False, indent=1)}]}


def traite(msg, orch):
    """Un message → une réponse, ou None pour une notification (qui n'en attend aucune)."""
    ident = msg.get("id")
    methode = msg.get("method")
    if ident is None:
        return None                              # notifications/initialized, cancelled…
    if methode == "initialize":
        demande = ((msg.get("params") or {}).get("protocolVersion") or "")
        return _reponse(ident, {
            "protocolVersion": demande if demande in PROTOCOLES else PROTOCOLES[-1],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "bobi", "version": VERSION},
            "instructions": "Surface de LECTURE de l'orchestrateur Bobi.Studio. Préférer ces "
                            "outils à un ssh ou un curl vers un nœud : la donnée est celle que "
                            "le contrôleur tient déjà, et l'accès passe par un compte du "
                            "produit. Aucune écriture n'est possible ici.",
        })
    if methode == "tools/list":
        return _reponse(ident, _liste_outils())
    if methode == "tools/call":
        return _reponse(ident, _appel(orch, msg.get("params")))
    if methode == "ping":
        return _reponse(ident, {})
    return _erreur(ident, -32601, "méthode non gérée : %s" % methode)


def boucle(entree=None, sortie=None):
    entree = entree or sys.stdin
    sortie = sortie or sys.stdout
    orch = Orchestrateur()
    for ligne in entree:
        ligne = ligne.strip()
        if not ligne:
            continue
        try:
            msg = json.loads(ligne)
        except ValueError:
            # Pas d'id à qui répondre : on le dit sur stderr (que le client journalise) et on
            # continue. Tuer le serveur sur un message malformé coûterait toute la session.
            print("mcp_bobi: message illisible, ignoré", file=sys.stderr)
            continue
        rep = traite(msg, orch)
        if rep is not None:
            sortie.write(json.dumps(rep, ensure_ascii=False) + "\n")
            sortie.flush()


if __name__ == "__main__":
    boucle()
