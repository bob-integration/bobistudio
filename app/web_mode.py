# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Mode d'exposition du port web de l'orchestrateur (`:5000`).

    http   — HTTP nu. Le défaut, pour un banc ou un réseau fermé.
    tls    — l'orchestrateur chiffre LUI-MÊME (petite installation), cf. plus bas.
    proxy  — derrière un reverse proxy qui termine TLS (grosse installation). Les en-têtes
             `X-Forwarded-*` sont CRUS.

UNE SEULE QUESTION, ET C'EST LA SÉCURITÉ ELLE-MÊME : faut-il croire `X-Forwarded-For` ?

Cet en-tête est envoyé par le CLIENT. Il ne dit la vérité que si un reverse proxy de confiance
l'a réécrit, ET que l'orchestrateur n'est joignable qu'à travers lui. Jusqu'au 2026-10-10,
`ProxyFix` était monté INCONDITIONNELLEMENT : le produit faisait comme si toutes les
installations avaient un proxy, alors qu'aucune n'en avait. Conséquence mesurée sur une base
jetable, même attaquant, même adresse TCP :

    25 essais de mot de passe, sans en-tête                     → 15 bloqués (le plafond tient)
    25 essais, avec un X-Forwarded-For différent à chaque essai → 0 bloqué

Le plafond d'essais de connexion était donc CONTOURNABLE : un mot de passe se devinait sans
limite, et le journal de sécurité enregistrait l'adresse que l'attaquant choisissait. `x_host`
permettait en outre au client d'imposer le nom d'hôte des URL fabriquées par l'orchestrateur
(liens publics, URL d'enrôlement). La page des liens publics avait vu le piège et lisait le
vrai pair TCP ; la connexion ne l'avait pas vu.

Le mode décide donc de monter `ProxyFix` ou non, et rien d'autre ne doit en décider : un second
réglage qui dirait la même chose finirait par dire le contraire.

LE MODE EST LU AU DÉMARRAGE, pas à chaque requête. D'abord parce qu'une lecture de réglage par
requête ferait de la base une pompe à lectures (le tableau de bord interroge toutes les 5 s, par
onglet). Ensuite parce qu'un changement de mode change la nature de ce qu'on écoute : l'appliquer
au vol créerait une fenêtre où la moitié des requêtes suivrait l'ancien. `etat()` rend donc le
mode ACTIF et le mode ENREGISTRÉ, pour que l'interface dise quand ils diffèrent — c'est-à-dire
« redémarrez » — au lieu d'afficher « enregistré » sur un réglage qui ne s'applique pas encore.

MODE `tls` — DEUX ÉCOUTES, et c'est voulu. Passer le `:5000` en HTTPS aurait cassé tout ce qui
l'appelle EN MACHINE sans connaître notre CA : un nœud en cours d'installation (il ne l'a pas
encore reçue), les agents-nœuds déjà déployés (`urlopen` sans contexte — il aurait fallu tous les
redéployer), le firmware iLO qui lit l'ISO, la sonde keepalived, la réplication HA et les mises à
jour entre instances. Donc :

  · HTTPS sur `web_tls_port` (443 par défaut) pour tout ce qui porte une IDENTITÉ : navigateurs,
    connexion, API, jetons, MCP. Certificat : `tls/controller.crt`, signé par la CA du contrôleur.
  · HTTP sur `:5000`, RÉDUIT à la liste blanche `CHEMINS_MACHINE` / `PREFIXES_MACHINE`. Tout le
    reste y est redirigé vers HTTPS (GET) ou refusé (le reste) — `/login` compris : un mot de
    passe ne peut plus passer en clair, même par une ancienne adresse en favori.

Les URL remises aux MACHINES (enrôlement, ISO, pairs) passent par `url_machines()`, qui rend
l'adresse HTTP du `:5000` en mode tls — sinon un administrateur connecté en HTTPS remettrait à un
nœud neuf une URL `https://` qu'il ne saurait pas vérifier.

REPLI : si le mode tls est demandé mais impossible (dépendance absente, certificat illisible,
port occupé), on sert en HTTP COMPLET et on lève une alerte de sécurité. Le choix est délibéré :
rester joignable en clair en le criant vaut mieux qu'un contrôleur de régie injoignable — et une
liste blanche qui redirigerait vers un HTTPS mort serait exactement ça.

SECOURS : la variable d'environnement `BOBI_WEB_MODE` force le mode au démarrage et prime sur la
base. C'est la porte de sortie si un mode mal posé rend l'interface injoignable — on ne doit
jamais pouvoir s'enfermer dehors avec un réglage.
"""
import logging
import os

log = logging.getLogger(__name__)

MODES = ("http", "tls", "proxy")
DEFAUT = "http"
ENV = "BOBI_WEB_MODE"

PORT_HTTP = 5000
PORT_HTTPS_DEFAUT = 443

# Ce que les MACHINES appellent sur le `:5000`, relevé le 2026-10-10 (et la seule chose que
# l'écoute HTTP sert encore en mode tls). Ajouter un appelant machine sans l'inscrire ici le
# ferait rediriger vers HTTPS — il échouerait sur notre CA. La garde en vérifie la liste.
CHEMINS_MACHINE = (
    "/api/identity",      # agents-nœuds déjà déployés (urlopen sans contexte TLS)
    "/api/nodes/enroll",  # nœud en cours d'installation : il n'a pas encore la CA
    "/api/ha/pairing/claim",  # standby en cours d'appairage : même raison (cf. ha_appairage)
)
PREFIXES_MACHINE = (
    "/iso/",              # média virtuel lu par l'iLO — un firmware n'acceptera pas notre CA
    "/api/update/",       # sonde keepalived (/ping) et mises à jour tirées par une autre instance
    # « /api/ha/ » N'Y EST PLUS (2026-10-10) : la réplication et la sonde de pair passent par
    # l'écoute mTLS dédiée (app/ha_tls.py). Seule la demande d'appairage reste ici, ci-dessus.
)

# Mode EFFECTIVEMENT appliqué par ce processus. Posé par `appliquer()` au démarrage ; ne bouge
# plus ensuite (cf. la docstring). Les gardes le posent via `fixer()`.
_actif = DEFAUT
_source = "défaut"
_repli = None          # (cle_i18n, detail) si tls demandé mais impossible ; None sinon


def _enregistre():
    """Le mode tel qu'enregistré en base, ou None s'il est absent ou illisible."""
    try:
        from . import settings as st
        v = (st.get("web_mode") or "").strip().lower()
        return v or None
    except Exception as e:
        log.warning("mode web : réglage illisible (%s)", e)
        return None


def resoudre():
    """→ (mode, source). La variable d'environnement prime sur la base, la base sur le défaut.

    Une valeur INCONNUE retombe sur `http`, et on le DIT au journal plutôt que de deviner :
    c'est le seul repli qui ne puisse pas rendre l'interface injoignable, mais un repli muet
    sur un réglage de sécurité est précisément ce qu'on ne veut pas laisser passer."""
    env = (os.environ.get(ENV) or "").strip().lower()
    if env:
        if env in MODES:
            return env, "variable %s" % ENV
        log.error("mode web : %s=%r inconnu (attendu : %s) — repli sur %s",
                  ENV, env, ", ".join(MODES), DEFAUT)
        return DEFAUT, "repli (variable %s invalide)" % ENV
    v = _enregistre()
    if v is None:
        return DEFAUT, "défaut"
    if v in MODES:
        return v, "réglage"
    log.error("mode web : réglage web_mode=%r inconnu (attendu : %s) — repli sur %s",
              v, ", ".join(MODES), DEFAUT)
    return DEFAUT, "repli (réglage invalide)"


def port_https():
    try:
        from . import settings as st
        p = int(st.get("web_tls_port") or PORT_HTTPS_DEFAUT)
        return p if 0 < p < 65536 else PORT_HTTPS_DEFAUT
    except Exception:
        return PORT_HTTPS_DEFAUT


def fichiers_tls():
    from . import ca
    pth = ca.paths()
    # ⚠ Les clés sont `ctrl_cert` / `ctrl_key`. Le premier jet lisait `controller_crt`, qui
    # n'existe pas : le mode tls aurait TOUJOURS basculé en repli en se disant « certificat
    # illisible » — une panne qui se serait fait passer pour une autre.
    return pth["ctrl_cert"], pth["ctrl_key"]


def contexte_tls(crt=None, key=None):
    """Contexte TLS SERVEUR pour l'écoute HTTPS. TLS 1.2 minimum, imposé ici : `cheroot` accepte
    par défaut n'importe quelle version (`MINIMUM_SUPPORTED`), constaté à l'installation."""
    import ssl
    if crt is None:
        crt, key = fichiers_tls()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(crt, key)
    return ctx


def faisabilite_tls(crt=None, key=None):
    """→ None si le mode tls peut démarrer, sinon (clé d'alerte, détail technique)."""
    import importlib
    try:
        for m in ("cheroot.wsgi", "cheroot.ssl.builtin"):
            importlib.import_module(m)
    except ImportError as e:
        return ("alert.securite.web_tls_repli_dependance", str(e))
    try:
        contexte_tls(crt, key)
    except Exception as e:
        return ("alert.securite.web_tls_repli_certificat", "%s" % e)
    return None


def _alerter_repli():
    try:
        from .database import db_add_alert
        cle, detail = _repli
        # kind « auth » : c'est l'authentification entière qui repasse en clair — mot de passe,
        # cookie, jetons. La ligne doit vivre dans la piste d'audit, avec sa rétention propre.
        db_add_alert(cle, "error", kind="auth",
                     params={"detail": str(detail)[:300], "port": port_https()})
    except Exception as e:
        log.error("mode web : alerte de repli non enregistrée (%s)", e)


def https_echoue(cle, detail):
    """Appelé par main.py si l'écoute HTTPS n'a pas pu démarrer (port occupé…). Bascule en HTTP
    COMPLET : sans cela, la liste blanche du :5000 redirigerait tout le monde vers un HTTPS mort."""
    global _actif, _repli
    _actif, _repli = "http", (cle, detail)
    log.error("mode web : HTTPS impossible (%s) — REPLI EN HTTP COMPLET", detail)
    _alerter_repli()


def appliquer():
    """À appeler UNE FOIS au démarrage. Fige le mode actif de ce processus."""
    global _actif, _source, _repli
    _actif, _source = resoudre()
    _repli = None
    if _actif == "tls":
        pb = faisabilite_tls()
        if pb:
            _actif, _repli = "http", pb
            log.error("mode web : tls demandé mais impossible (%s) — REPLI EN HTTP COMPLET", pb[1])
            _alerter_repli()
    log.info("mode web : %s (%s)%s", _actif, _source,
             " — X-Forwarded-* CRUS" if _actif == "proxy" else
             " — X-Forwarded-* IGNORÉS, adresse cliente = pair TCP")
    return _actif


def fixer(mode):
    """Pose le mode actif sans passer par la base. Réservé aux gardes."""
    global _actif, _source
    if mode not in MODES:
        raise ValueError("mode web inconnu : %r" % mode)
    global _repli
    _actif, _source, _repli = mode, "fixé", None


def actif():
    return _actif


def croire_entetes_transmis():
    """True si `X-Forwarded-*` doit être cru — c'est-à-dire derrière un proxy, et seulement là."""
    return _actif == "proxy"


def cookie_secure_impose():
    """True si le cookie de session DOIT porter `Secure`.

    En tls, la connexion n'est servie QUE sur l'écoute HTTPS : le cookie n'a aucune raison de
    voyager ailleurs. Derrière un proxy, le navigateur parle HTTPS au proxy : un cookie sans `Secure` pourrait
    repartir en clair sur un lien HTTP oublié. En HTTP nu, `Secure` le rendrait inutilisable —
    le navigateur ne le renverrait jamais, et personne ne pourrait se connecter."""
    return _actif in ("tls", "proxy")


def valider(valeur):
    """Pour la route d'enregistrement : rend le mode normalisé, ou lève ValueError."""
    v = (str(valeur or "")).strip().lower()
    if v not in MODES:
        raise ValueError("web_mode doit valoir %s (reçu %r)" % (" ou ".join(MODES), valeur))
    return v


def etat():
    """Ce que l'interface affiche : le mode ACTIF, le mode ENREGISTRÉ, et s'ils diffèrent."""
    enr, source = resoudre()
    return {"actif": _actif, "source_actif": _source,
            "enregistre": enr, "source_enregistre": source,
            "force_par_env": bool((os.environ.get(ENV) or "").strip()),
            # Un repli n'est PAS un « redémarrez » : redémarrer ne changera rien tant que la
            # cause demeure. On le distingue pour que l'interface dise la vraie cause.
            "redemarrage_requis": enr != _actif and _repli is None,
            "repli": ({"cle": _repli[0], "detail": str(_repli[1])} if _repli else None),
            "port_https": port_https(),
            "modes": list(MODES)}


class EnTetesTransmis:
    """Middleware WSGI : `ProxyFix` en mode proxy, rien en mode http.

    Les deux chemins sont construits UNE fois ; le choix se fait par requête sur une variable
    du module, donc sans lecture de base. On garde la même instance `ProxyFix` que l'ancien
    montage inconditionnel (x_for, x_proto, x_host à 1) : en mode proxy, le comportement est
    exactement celui d'avant — seul le mode http change, et c'est lui qui était faux."""

    def __init__(self, wsgi_app):
        from werkzeug.middleware.proxy_fix import ProxyFix
        self._direct = wsgi_app
        self._via_proxy = ProxyFix(wsgi_app, x_for=1, x_proto=1, x_host=1)

    def __call__(self, environ, start_response):
        if _actif == "proxy":
            return self._via_proxy(environ, start_response)
        return self._direct(environ, start_response)


def chemin_machine(chemin):
    return chemin in CHEMINS_MACHINE or chemin.startswith(PREFIXES_MACHINE)


def _hote_sans_port(hote):
    hote = (hote or "").strip()
    if hote.startswith("["):                      # IPv6 littéral : [::1]:5000
        return hote[:hote.index("]") + 1] if "]" in hote else hote
    return hote.rsplit(":", 1)[0] if hote.count(":") == 1 else hote


def url_machines(proposee=None, hote=None):
    """L'URL du contrôleur à remettre à une MACHINE (nœud, iLO, pair).

    En mode tls, c'est l'écoute HTTP du :5000 — même si l'administrateur qui déclenche
    l'enrôlement est connecté en HTTPS : un nœud neuf ne saurait pas vérifier notre CA. Hors tls,
    on rend l'URL proposée telle quelle (comportement inchangé)."""
    if _actif != "tls":
        return (proposee or "").rstrip("/")
    from urllib.parse import urlparse
    nom = urlparse(proposee).hostname if proposee else None
    nom = nom or _hote_sans_port(hote) or "127.0.0.1"
    if ":" in nom and not nom.startswith("["):
        nom = "[%s]" % nom                        # IPv6 nu → littéral d'URL
    return "http://%s:%d" % (nom, PORT_HTTP)


class EcouteMachines:
    """Middleware de l'écoute HTTP (:5000). Hors mode tls : transparent.

    En mode tls : laisse passer la liste blanche machine, redirige les GET/HEAD vers HTTPS, et
    REFUSE le reste. Un POST n'est pas redirigé (307) : le client renverrait son corps — un mot
    de passe, par exemple — qui a DÉJÀ traversé le réseau en clair ; mieux vaut un refus qui dit
    pourquoi qu'une redirection qui rend l'erreur invisible."""

    def __init__(self, wsgi_app):
        self._app = wsgi_app

    def __call__(self, environ, start_response):
        if _actif != "tls":
            return self._app(environ, start_response)
        chemin = environ.get("PATH_INFO") or "/"
        if chemin_machine(chemin):
            return self._app(environ, start_response)
        port = port_https()
        nom = _hote_sans_port(environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "")
        cible = "https://%s%s%s" % (nom, "" if port == 443 else ":%d" % port, chemin)
        qs = environ.get("QUERY_STRING")
        if qs:
            cible += "?" + qs
        if environ.get("REQUEST_METHOD", "GET") in ("GET", "HEAD"):
            start_response("301 Moved Permanently",
                           [("Location", cible), ("Content-Type", "text/plain; charset=utf-8"),
                            ("Cache-Control", "no-store")])
            return [("HTTPS : %s\n" % cible).encode("utf-8")]
        import json
        corps = json.dumps({"error": "https_required", "url": cible}).encode("utf-8")
        start_response("403 Forbidden", [("Content-Type", "application/json"),
                                         ("Content-Length", str(len(corps)))])
        return [corps]


_ctx_https = None       # contexte de l'écoute HTTPS en service — gardé pour recharger le certificat


def recharger_certificat():
    """Fait servir le certificat ACTUEL du contrôleur par l'écoute HTTPS, sans la redémarrer.

    `load_cert_chain` sur le contexte en service : les poignées de main suivantes présentent le
    nouveau certificat, les connexions ouvertes gardent l'ancien. Appelé après toute ré-émission
    (IP virtuelle, appairage). → True si une écoute a été rechargée."""
    if _ctx_https is None:
        return False
    _ctx_https.load_cert_chain(*fichiers_tls())
    log.info("mode web : certificat de l'écoute HTTPS rechargé")
    return True


def demarrer_https(app, hote="0.0.0.0"):
    """Démarre l'écoute HTTPS dans un fil démon. → le serveur, ou None en cas d'échec (le repli
    est alors posé, alerté, et l'écoute HTTP redevient complète)."""
    if _actif != "tls":
        return None
    import threading
    try:
        from cheroot import wsgi
        from cheroot.ssl.builtin import BuiltinSSLAdapter
        crt, key = fichiers_tls()
        port = port_https()
        # 16 fils comme Waitress (l'iLO tient une connexion pendant tout un téléchargement
        # d'ISO) et une file de 128 : celle de cheroot vaut 5 par défaut, contre 1024 chez
        # Waitress — un tableau de bord ouvert dans dix onglets la saturerait.
        srv = wsgi.Server((hote, port), app, numthreads=16, request_queue_size=128,
                          server_name="bobistudio")
        adaptateur = BuiltinSSLAdapter(crt, key)
        adaptateur.context = contexte_tls(crt, key)
        srv.ssl_adapter = adaptateur
        srv.prepare()                             # LIE le port : c'est ici qu'un 443 occupé échoue
    except Exception as e:
        https_echoue("alert.securite.web_tls_repli_port", "%s" % e)
        return None
    global _ctx_https
    _ctx_https = adaptateur.context
    threading.Thread(target=srv.serve, daemon=True, name="https-%d" % port).start()
    log.info("mode web : HTTPS actif sur le port %d ; le :%d ne sert plus que les machines",
             port, PORT_HTTP)
    return srv
