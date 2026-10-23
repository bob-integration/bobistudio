# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Transport de la réplication HA : une écoute mTLS DÉDIÉE, qui n'accepte QUE des contrôleurs.

AVANT (jusqu'au 2026-10-10) : l'actif poussait la base ENTIÈRE vers `http://<standby>:5000`
toutes les N minutes — `flask_secret_key` (de quoi forger n'importe quelle session), jetons des
agents, mots de passe hachés — en CLAIR, authentifiée par un secret partagé envoyé en en-tête à
chaque requête, donc rejouable : qui l'avait capté pouvait déposer une base empoisonnée dans le
staging du standby, appliquée à la bascule suivante. Et ce secret était aussi celui des mises à
jour entre instances.

MAINTENANT : les deux contrôleurs partagent la CA (cf. app/ha_appairage.py) et se parlent sur
`ha_tls_port` (5444 par défaut) en mTLS. Le secret partagé ne sert plus à la HA.

DEUX VÉRIFICATIONS, et la seconde est celle qui compte :
  1. la chaîne : le pair présente un certificat signé par NOTRE CA (`CERT_REQUIRED`) ;
  2. le RÔLE : ce certificat porte le marqueur de contrôleur (`ca.MARQUEUR_CONTROLEUR`). La même
     CA signe aussi les nœuds et les CONTENEURS — qui exécutent du code de plugin tiers. Sans
     cette seconde vérification, un plugin compromis pourrait pousser une base au standby.

Les deux sens sont vérifiés : l'écoute refuse un client sans marqueur, et le client refuse
d'envoyer la base à un serveur sans marqueur — un nœud qui usurperait l'adresse du standby ne
recevrait rien.

Pas de vérification du NOM d'hôte (`check_hostname = False`), délibérément : l'identité utile est
« un contrôleur de notre CA », et une paire n'en compte que deux. Exiger le nom aurait rendu la
réplication fragile au premier changement d'adressage ou à un nom DNS — sans rien protéger de plus.
"""
import http.client
import logging
import threading
from urllib.parse import urlparse

log = logging.getLogger(__name__)

PORT_DEFAUT = 5444
CHEMINS = ("/api/ha/replicate", "/api/ha/peer", "/api/ha/replicate_lv2")
CLE_ENVIRON = "bobi.ha.pair"     # posée UNIQUEMENT par GardeHA ; un en-tête HTTP ne peut pas
                                 # l'écrire (les en-têtes deviennent HTTP_*, jamais une clé pointée)

_srv = None
_ctx = None             # contexte de l'écoute en service — gardé pour recharger le certificat
_srv_lock = threading.Lock()


def port_ha():
    try:
        from . import settings as st
        p = int(st.get("ha_tls_port") or PORT_DEFAUT)
        return p if 0 < p < 65536 else PORT_DEFAUT
    except Exception:
        return PORT_DEFAUT


def pret():
    """Ce contrôleur peut-il parler HA en mTLS ? CA présente et certificat marqué."""
    from . import ca
    try:
        if not ca.ca_available():
            return False
        with open(ca.paths()["ctrl_cert"], "rb") as f:
            return ca.est_controleur(f.read())
    except Exception:
        return False


def _uri_controleur_pem(pem):
    from . import ca
    for u in ca.uris_san(pem):
        if u.startswith(ca.MARQUEUR_CONTROLEUR):
            return u
    return None


class GardeHA:
    """Middleware de l'écoute mTLS. La chaîne a déjà été vérifiée par TLS (`CERT_REQUIRED`) ;
    on vérifie ici le RÔLE, puis on ne laisse passer que les chemins de réplication."""

    def __init__(self, wsgi_app):
        self._app = wsgi_app

    def _refus(self, start_response, code, msg):
        import json
        corps = json.dumps({"ok": False, "error": msg}).encode()
        start_response(code, [("Content-Type", "application/json"),
                              ("Content-Length", str(len(corps)))])
        return [corps]

    def __call__(self, environ, start_response):
        # On ne fait PAS confiance à une clé qui arriverait déjà posée.
        environ.pop(CLE_ENVIRON, None)
        if (environ.get("PATH_INFO") or "") not in CHEMINS:
            return self._refus(start_response, "404 Not Found", "hors réplication HA")
        pem = environ.get("SSL_CLIENT_CERT")
        if environ.get("SSL_CLIENT_VERIFY") != "SUCCESS" or not pem:
            return self._refus(start_response, "403 Forbidden", "certificat client requis")
        try:
            uri = _uri_controleur_pem(pem)
        except Exception:
            uri = None
        if not uri:
            log.warning("HA : certificat de notre CA SANS marqueur de contrôleur refusé (%s)",
                        environ.get("REMOTE_ADDR"))
            return self._refus(start_response, "403 Forbidden",
                               "certificat valide, mais pas celui d'un contrôleur")
        environ[CLE_ENVIRON] = uri
        return self._app(environ, start_response)


def contexte_serveur():
    import ssl
    from . import ca
    ctx = ca.server_ssl_context(require_client_cert=True)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def demarrer(app, hote="0.0.0.0", port=None):
    """Démarre l'écoute mTLS (idempotent). → le serveur, ou None si ce contrôleur n'est pas prêt
    ou si le port est pris (journalisé : la réplication échouera alors en le disant)."""
    global _srv, _ctx
    with _srv_lock:
        if _srv is not None:
            return _srv
        if not pret():
            log.info("HA : pas d'écoute mTLS — contrôleur non appairé (pas de marqueur)")
            return None
        try:
            from cheroot import wsgi
            from cheroot.ssl.builtin import BuiltinSSLAdapter
            from . import ca
            p = ca.paths()
            port = port or port_ha()
            srv = wsgi.Server((hote, port), GardeHA(app), numthreads=4,
                              request_queue_size=16, server_name="bobistudio-ha")
            ad = BuiltinSSLAdapter(p["ctrl_cert"], p["ctrl_key"])
            ad.context = contexte_serveur()
            srv.ssl_adapter = ad
            srv.prepare()
        except Exception as e:
            log.error("HA : écoute mTLS impossible sur %s:%s (%s)", hote, port, e)
            return None
        threading.Thread(target=srv.serve, daemon=True, name="ha-mtls-%d" % port).start()
        _srv, _ctx = srv, ad.context
        log.info("HA : écoute mTLS active sur le port %d (contrôleurs seulement)", port)
        return srv


def arreter():
    global _srv, _ctx
    with _srv_lock:
        if _srv is not None:
            try:
                _srv.stop()
            finally:
                _srv = None
                _ctx = None


def recharger_certificat():
    """Fait présenter le certificat ACTUEL par l'écoute mTLS, sans la redémarrer (cf.
    web_mode.recharger_certificat). La CA de confiance, elle, n'est PAS rechargée ici : un
    changement d'autorité (appairage) passe par `redemarrer`. → True si rechargée."""
    from . import ca
    with _srv_lock:
        if _srv is None or _ctx is None:
            return False
        p = ca.paths()
        _ctx.load_cert_chain(p["ctrl_cert"], p["ctrl_key"])
    log.info("HA : certificat de l'écoute mTLS rechargé")
    return True


def redemarrer(app):
    """Après un appairage : la CA et le certificat ont changé, l'écoute doit les recharger."""
    arreter()
    return demarrer(app)


def requete(methode, chemin, corps=None, en_tetes=None, timeout=60, hote=None, port=None):
    """Appel mTLS vers l'AUTRE contrôleur. → (statut, corps bytes).

    Le serveur doit présenter un certificat de notre CA ET porter le marqueur — vérifié APRÈS la
    poignée de main et AVANT d'envoyer quoi que ce soit : la base ne part jamais vers un pair qui
    n'a pas prouvé être un contrôleur."""
    from . import ca, ha
    if not pret():
        raise PermissionError("contrôleurs non appairés — Réglages → Haute disponibilité → "
                              "Appairer")
    if hote is None:
        url = ha.peer_url()
        if not url:
            raise ValueError("pas d'adresse de l'autre contrôleur")
        hote = urlparse(url if "://" in url else "http://" + url).hostname
    ctx = ca.controller_client_context()
    ctx.check_hostname = False           # cf. docstring : l'identité est le rôle, pas le nom
    conn = http.client.HTTPSConnection(hote, port or port_ha(), context=ctx, timeout=timeout)
    try:
        conn.connect()
        der = conn.sock.getpeercert(binary_form=True)
        import ssl
        if not der or not _uri_controleur_pem(ssl.DER_cert_to_PEM_cert(der)):
            raise PermissionError("le pair a un certificat de notre CA mais n'est PAS un "
                                  "contrôleur — rien n'est envoyé")
        conn.request(methode, chemin, body=corps, headers=en_tetes or {})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()
