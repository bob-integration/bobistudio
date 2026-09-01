# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import requests
from .numerotation import cle_input, cle_input_v, cle_input_a, numero, indice
import logging
import os
import glob
import base64
import hashlib
import hmac
import threading
import time as _time
from requests.adapters import HTTPAdapter
from .addressing import get_container_ip
from .scripts import generer_script
from . import plugins
from . import ca
from . import config

log = logging.getLogger(__name__)


# ── Auth agent par-conteneur (:8081) — rétro-compatible ───────────────────────
# DEUX sources, dans cet ordre :
#   1. `containers.agent_token` — valeur ALÉATOIRE par conteneur, tirée et persistée par le driver
#      au moment du `docker run` qui l'injecte en MXL_AGENT_TOKEN (docker_compute / docker_driver).
#      C'est la source NORMALE depuis 2026-07.
#   2. REPLI : token DÉRIVÉ (HMAC-SHA256) du secret contrôleur (`flask_secret_key`) et du vmid —
#      le schéma historique. Il n'existe que pour les conteneurs créés AVANT la migration : leur
#      agent ne connaît de toute façon aucun token (l'injection n'était posée nulle part), donc
#      l'en-tête est ignoré. Ce repli est ce qui évite tout redéploiement forcé de la flotte ; la
#      bascule se fait conteneur par conteneur, au fil des redéploiements.
# L'en-tête est envoyé best-effort : un agent SANS MXL_AGENT_TOKEN ignore l'en-tête inconnu ; un
# agent AVEC le token l'exige (hmac.compare_digest).
#
# POURQUOI on ne s'en tient pas au dérivé : `flask_secret_key` signe AUSSI les cookies de session
# Flask. Tant qu'un conteneur en dépend, cette clé n'est PAS rotable — la faire tourner après un
# incident rendrait toute la flotte impilotable. Traçabilité de la bascule :
# `database.db_agent_token_etat()` (et le champ `agent_auth` de chaque conteneur).
_AGENT_TOKEN_HEADER = "X-MXL-Agent-Token"

def _controller_secret():
    try:
        from .database import db_get_setting
        s = db_get_setting("flask_secret_key", None)
        if s:
            return s.encode() if isinstance(s, str) else bytes(s)
    except Exception:
        pass
    return None

def agent_token_derive(vmid):
    """Token HISTORIQUE, dérivé du secret contrôleur + vmid (repli), ou None si pas de secret."""
    secret = _controller_secret()
    if not secret or vmid is None:
        return None
    return hmac.new(secret, ("agent:%s" % vmid).encode(), hashlib.sha256).hexdigest()

def agent_token(vmid):
    """Token d'agent de ce conteneur : le STOCKÉ (aléatoire, injecté au `docker run`) s'il existe,
    sinon le DÉRIVÉ historique. None si ni l'un ni l'autre."""
    if vmid is None:
        return None
    try:
        from .database import db_get_agent_token
        tok = db_get_agent_token(vmid)
        if tok:
            return tok
    except Exception as e:
        log.warning("agent_token %s : lecture du token stocké impossible (%s) — repli dérivé",
                    vmid, e)
    return agent_token_derive(vmid)

def agent_headers(vmid):
    """En-têtes d'auth à joindre à toute requête :8081 pour ce vmid ({} si pas de token)."""
    tok = agent_token(vmid)
    return {_AGENT_TOKEN_HEADER: tok} if tok else {}


# Réglage HORS DEFAULTS (usage exceptionnel), volontairement non listé dans settings._BASE_DEFAULTS :
#   `agent_token_inject` = 0/false/off/no → on N'INJECTE PLUS MXL_AGENT_TOKEN au `docker run`.
# ÉCHAPPATOIRE : activer une authentification jusque-là inopérante peut verrouiller l'orchestrateur
# hors de ses propres conteneurs (un chemin d'appel qui oublierait l'en-tête). Ce réglage ramène au
# comportement historique (agent OUVERT) SANS avoir à recréer quoi que ce soit à la main : le
# poser, puis redéployer le conteneur concerné. Le token stocké est alors oublié
# (`db_clear_agent_token`) pour que l'état de migration ne mente pas.
def injection_token_active():
    """True si les drivers doivent injecter MXL_AGENT_TOKEN au `docker run` (défaut : oui)."""
    try:
        from . import settings as _st
        v = _st.get("agent_token_inject", None)
    except Exception:
        return True
    if v is None:
        return True
    return str(v).strip().lower() not in ("0", "false", "off", "no", "")


def token_a_injecter(vmid):
    """Token à poser en MXL_AGENT_TOKEN dans l'environnement du conteneur, ou None si l'injection
    est désactivée / indisponible. APPELÉ UNIQUEMENT par les drivers au moment de construire la
    spec `docker run` : c'est ce même appel qui TIRE et PERSISTE le token (aléatoire) la première
    fois — un token stocké sans avoir été injecté rendrait le conteneur injoignable."""
    from .database import db_ensure_agent_token, db_clear_agent_token
    if not injection_token_active():
        db_clear_agent_token(vmid)
        return None
    try:
        return db_ensure_agent_token(vmid)
    except Exception as e:
        log.warning("token_a_injecter %s: %s — agent créé SANS token (repli dérivé côté appels)",
                    vmid, e)
        return None


# ── TLS du plan de contrôle vers l'agent conteneur :8081 (chantier feat/mtls) ──
# Quand la CA interne est disponible, les conteneurs sont recréés HTTPS-only (cert conteneur signé
# CA injecté au `docker run`, cf. docker_compute / agent.py). Le contrôleur parle alors à :8081 en
# HTTPS : chaîne VÉRIFIÉE contre la CA, mais check_hostname DÉSACTIVÉ (connexion single-hop vers une
# IP macvlan allouée au déploiement — on évite tout problème de timing d'IP SAN), + cert client du
# contrôleur (mTLS). L'en-tête token X-MXL-Agent-Token reste envoyé EN PLUS (double facteur).
# NB : ne concerne QUE :8081 (l'agent). Les :8080 (métriques) / :8082 (hot-wire) sont servis par le
# script du plugin, restent en HTTP clair SANS AUTHENTIFICATION et ne passent PAS par ce helper —
# c'est aujourd'hui le seul chemin d'écriture non authentifié du produit (:8082 pilote à chaud).
#
# ⚠ CORRIGÉ le 2026-08-31 : ce commentaire affirmait que le moteur 2110 (`controller.py` de
# l'image, --network host) « ne lit pas /etc/bobi-tls → hors périmètre, ses appels restent en
# http ». C'EST FAUX, et ça l'était depuis un moment : `controller.py` a `_agent_tls_context()`,
# et l'orchestrateur lui parle bien en `https://<nœud>:8081`. Le moteur est DANS le périmètre
# mTLS comme les autres. Un commentaire périmé sur la sécurité est pire qu'aucun commentaire :
# celui-ci aurait pu servir à justifier de ne pas protéger un chemin qui l'est déjà.
class _NoHostnameHTTPSAdapter(HTTPAdapter):
    """HTTPAdapter qui monte un SSLContext maison (vérif chaîne CA + cert client, hostname OFF).
    `assert_hostname=False` désactive AUSSI la vérification de nom faite par urllib3 lui-même (elle
    est indépendante de ssl_context.check_hostname) — indispensable pour joindre une IP macvlan sans
    SAN correspondant. La chaîne reste vérifiée contre la CA (CERT_REQUIRED du contexte)."""
    def __init__(self, ssl_context, *a, **kw):
        self._ssl_context = ssl_context
        super().__init__(*a, **kw)

    def init_poolmanager(self, *a, **kw):
        kw["ssl_context"] = self._ssl_context
        kw["assert_hostname"] = False
        return super().init_poolmanager(*a, **kw)

    def proxy_manager_for(self, *a, **kw):
        kw["ssl_context"] = self._ssl_context
        kw["assert_hostname"] = False
        return super().proxy_manager_for(*a, **kw)


_agent_tls_enabled = None      # cache tri-état : None=non résolu, True/False
_agent_session_https = None
_agent_session_lock = threading.Lock()


def agent_tls_on():
    """True si le contrôleur doit parler HTTPS à l'agent :8081 (CA interne disponible → conteneurs
    recréés HTTPS-only). Rétro-compat : False → http (comportement historique)."""
    global _agent_tls_enabled
    if _agent_tls_enabled is None:
        try:
            _agent_tls_enabled = bool(ca.ca_available())
        except Exception:
            _agent_tls_enabled = False
    return _agent_tls_enabled


def agent_scheme():
    return "https" if agent_tls_on() else "http"


def agent_url(ip, path="", port=8081):
    """URL d'un endpoint de l'agent conteneur (:8081), schéma résolu selon la CA."""
    return f"{agent_scheme()}://{ip}:{port}{path}"


def controller_port_base(vmid):
    """Base des ports du contrôleur bobi-mtl d'un conteneur : :base (métriques/rapport get_metrics),
    :base+1 (contrat agent /nmos/subscribe /status /tx /pin), :base+2 (contrôle à chaud). Défaut 8080
    → moteur mono-nœud / compute STRICTEMENT inchangés. Une sonde probe_2110 offsette sa base
    (deploy_config.params.controller_port_base) pour coexister avec le moteur sur le même nœud
    (--network host). Lu par metrics/_notify_agent/repush pour cibler le bon port par conteneur."""
    if vmid is None:
        return 8080
    try:
        import json as _j
        from .database import db_get_container as _gc
        c = _gc(vmid)
        dc = c.get("deploy_config") if c else None
        dc = _j.loads(dc) if isinstance(dc, str) else (dc or {})
        b = int(((dc.get("params") or {}) or {}).get("controller_port_base") or 8080)
        return b if b >= 1024 else 8080
    except Exception:
        return 8080


def agent_port(vmid):
    """Port du contrat agent (:base+1) d'un conteneur — 8081 par défaut, offsetté pour une sonde."""
    return controller_port_base(vmid) + 1


def _build_agent_ssl_context():
    """SSLContext client du contrôleur : vérif chaîne contre la CA, cert client (mTLS), hostname OFF."""
    ctx = ca.controller_client_context()   # cafile=CA + load_cert_chain(controller) [interface figée]
    ctx.check_hostname = False             # single-hop IP macvlan éphémère → pas de vérif de nom
    return ctx


def agent_session():
    """`requests.Session` (ou le module requests en http) à utiliser pour TOUT appel :8081. En HTTPS :
    Session mémoïsée avec l'adapter TLS (CA + cert client, hostname off). Toujours joindre en plus les
    headers d'auth via agent_headers(vmid) et un timeout explicite."""
    if not agent_tls_on():
        return requests   # module : même API .get/.post ; comportement http historique
    global _agent_session_https
    if _agent_session_https is None:
        with _agent_session_lock:
            if _agent_session_https is None:
                s = requests.Session()
                s.mount("https://", _NoHostnameHTTPSAdapter(_build_agent_ssl_context()))
                _agent_session_https = s
    return _agent_session_https


# ── Filet : DÉSACCORD DE SCHÉMA contrôleur (https) ↔ agent conteneur (http) ────
# POURQUOI (panne de prod 2026-07) : l'agent-nœud matérialise les PEM d'un conteneur sous
# /run/bobi-tls/<nom>/ — or /run est un TMPFS. Au reboot du nœud : /run est vidé, Docker relève les
# conteneurs `--restart unless-stopped` AVANT toute reprovision, la source du bind-mount n'existe
# plus donc Docker la RECRÉE VIDE, et l'agent du conteneur (qui lit /etc/bobi-tls au démarrage) sert
# en HTTP CLAIR. Le contrôleur, lui, a tranché HTTPS globalement (agent_tls_on, mémoïsé) → le
# conteneur devient injoignable DÉFINITIVEMENT tout en continuant de tourner, et le seul symptôme
# était un timeout indiscernable d'un conteneur mort.
#
# Ce filet ne fait QUE du diagnostic : quand l'agent ne répond pas en HTTPS, on sonde UNE fois en
# clair. S'il répond, le verdict est CERTAIN (le conteneur est vivant, c'est le schéma qui diverge)
# et on le dit — alerte datée, niveau error. On ne bascule JAMAIS ce conteneur en HTTP : un repli
# silencieux transformerait une panne visible en dégradation de sécurité permanente et muette.
# La RÉPARATION est ailleurs (node_recovery : re-provision des certs au reboot).
_SCHEMA_ALERTE_PERIODE_S = 900          # anti-spam : au plus une alerte / conteneur / 15 min
_schema_alerte_ts = {}                  # vmid (ou ip) → time.monotonic() du dernier signalement
_schema_alerte_lock = threading.Lock()


def _sonde_agent(url, vmid, timeout=2):
    """True si QUELQUE CHOSE répond HTTP à cette URL (n'importe quel code : un 401 prouve qu'un
    agent est bien là). Aucune exception ne sort."""
    try:
        sess = agent_session() if url.startswith("https://") else requests
        sess.get(url, timeout=timeout, headers=agent_headers(vmid))
        return True
    except Exception:
        return False


def diagnostiquer_schema_agent(ip, vmid=None, port=None, alerter=True):
    """Verdict sur l'agent :8081 d'un conteneur — « ok » | « clair » | « injoignable ».

    - « ok »          : il répond dans le schéma attendu (https si CA dispo, sinon http).
    - « clair »       : DÉSACCORD DE SCHÉMA — muet en HTTPS, mais répond en HTTP clair. Certificat
                        conteneur perdu (typiquement : /run vidé par un reboot de nœud). Le conteneur
                        est VIVANT, il faut re-provisionner ses certs et le redémarrer.
    - « injoignable » : muet dans les deux schémas (conteneur réellement mort / IP fausse).

    Ne change JAMAIS le schéma du contrôleur (pas de repli http). `alerter=False` pour un simple
    constat (l'appelant décide du message)."""
    if not ip:
        return "injoignable"
    _port = port or agent_port(vmid)
    if not agent_tls_on():
        # Pas de CA → tout le monde est en http par construction : rien à désaccorder.
        return "ok" if _sonde_agent(f"http://{ip}:{_port}/status", vmid) else "injoignable"
    try:
        agent_session()      # matériel client du contrôleur lisible ? (sinon TOUT sera « muet »)
    except Exception as e:
        # Problème CÔTÉ CONTRÔLEUR (cert/clé illisibles) : ne pas l'imputer au conteneur.
        log.error("mTLS agent : contexte client du contrôleur inconstruisible (%s) — verdict de "
                  "schéma impossible pour %s", e, vmid)
        return "injoignable"
    if _sonde_agent(f"https://{ip}:{_port}/status", vmid):
        return "ok"
    if not _sonde_agent(f"http://{ip}:{_port}/status", vmid):
        return "injoignable"
    if alerter:
        _alerter_schema(ip, vmid, _port)
    return "clair"


def _alerter_schema(ip, vmid, port):
    """Alerte (throttlée) sur un désaccord de schéma constaté. Toujours loggée, même throttlée."""
    log.error("agent conteneur %s (%s:%s) : DÉSACCORD DE SCHÉMA — muet en HTTPS, répond en HTTP "
              "clair (certificat mTLS perdu)", vmid, ip, port)
    cle = vmid if vmid is not None else ip
    now = _time.monotonic()
    with _schema_alerte_lock:
        last = _schema_alerte_ts.get(cle)
        if last is not None and (now - last) < _SCHEMA_ALERTE_PERIODE_S:
            return
        _schema_alerte_ts[cle] = now
    db_add_alert(
        "alert.agent.schema_desaccord",
        "error", vmid=vmid, kind="agent", params={"vmid": vmid, "ip": ip, "port": port})

_STATIC_UPLOADS = config.UPLOADS_DIR

def _resolve_brand_settings(params):
    """Injecte l'identité client (réglages Personnalisation) dans les params d'un plugin :
    textes (nom système, entreprise, localisation) + LOGO en base64 (autonome, pas de
    dépendance réseau ; redéployer le générateur pour changer le logo)."""
    from . import settings as _st
    params = dict(params)
    params["brand_system_name"] = _st.get("brand_system_name") or ""
    params["brand_org_name"]    = _st.get("brand_org_name") or ""
    params["brand_location"]    = _st.get("brand_location") or ""
    params["brand_logo_b64"]    = ""
    params["brand_logo_ext"]    = ""
    try:
        for fp in glob.glob(os.path.join(_STATIC_UPLOADS, "brand-logo.*")):
            with open(fp, "rb") as f:
                params["brand_logo_b64"] = base64.b64encode(f.read()).decode("ascii")
            params["brand_logo_ext"] = os.path.splitext(fp)[1].lstrip(".")
            break
    except Exception as e:
        log.warning("logo marque illisible: %s", e)
    return params

from .database import (db_update_script, db_add_alert, db_update_source,
                      db_update_deploy_config, db_get_container, db_get_containers)


def _attendre_agent_pret(ip, attempts=20, delay=0.5, vmid=None):
    """Poll GET :8081/status jusqu'à ce que l'agent/contrôleur réponde, avant de POSTer /deploy.
    Évite l'échec « Connection refused » quand le contrôleur (LXC qui démarre, ou conteneur Docker
    --network host qui (re)lance son HTTPServer :8081) n'écoute pas encore. Borné (defaut 10 s) ;
    renvoie True dès qu'il répond, False si toujours injoignable au bout des tentatives (on tente
    quand même le deploy ensuite → comportement inchangé si l'agent ne revient pas)."""
    import time
    _port = agent_port(vmid)   # :8081 par défaut, offsetté pour une sonde (CONTROLLER_PORT_BASE)
    for _ in range(max(1, attempts)):
        try:
            if agent_session().get(agent_url(ip, "/status", port=_port), timeout=2,
                                   headers=agent_headers(vmid)).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(delay)
    return False


def attendre_controleur_pret(ip, vmid=None, timeout_s=150.0):
    """Readiness du contrôleur :8081 d'un conteneur — UNIQUE point d'attente avant tout resync
    (slots TX, abonnements RX NMOS). Poll GET :8081/status avec backoff borné par une ÉCHÉANCE
    (pas par un nombre d'essais : quand le port est fermé, `connection refused` revient
    instantanément et un `for _ in range(30)` avec sleep(1) épuisait son budget en ~30 s — alors
    qu'un moteur 2110 recréé met 30-60 s à servir :8081 ; c'est la course qui rendait le moteur
    muet après redéploiement, cf. NOTE course de resync dans docker_driver.deploy_docker).

    Renvoie True dès qu'il répond 200, False si toujours injoignable à l'échéance. Les en-têtes
    d'auth sont joints (un :8081 sous token répondait 401 → jamais 200 → readiness jamais atteinte)."""
    import time
    t0 = time.monotonic()
    _port = agent_port(vmid)
    delay = 0.5
    while time.monotonic() - t0 < timeout_s:
        try:
            if agent_session().get(agent_url(ip, "/status", port=_port), timeout=3,
                                   headers=agent_headers(vmid)).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)
    return False


def _agent_script_running(ip, vmid):
    """True si l'agent du conteneur répond ET qu'un script est déployé + en marche
    (`{"running": true, "path": "/opt/script/main.py"}`). `path: null` = rootfs éphémère recréé
    (docker restart) : le script a DISPARU du disque — un /start ou un hot-apply est sans objet,
    seul un redéploiement complet répare. Utilisé pour gater les chemins hot-apply."""
    try:
        r = agent_session().get(agent_url(ip, "/status", port=agent_port(vmid)),
                                timeout=3, headers=agent_headers(vmid))
        if r.status_code != 200:
            return False
        st = r.json() or {}
        return bool(st.get("running")) and bool(st.get("path"))
    except Exception:
        return False


_MULTIVIEW_STRUCTURAL = frozenset({
    "out_width", "out_height", "chroma", "bit_depth",
    "shm_video_ring", "shm_out", "fps", "scan", "genlock",
    "orientation",   # change les dims/sens du flux émis (rotation 90°) → recrée le flux, pas de hot-apply
    # Lus à l'IMPORT du script (constantes de module) : un hot-apply ne les relit jamais, donc les
    # changer sans re-rendre le script ne fait RIEN — et `deployer_script` renvoie quand même True.
    # RESTAURÉ le 2026-08-11 : ces clés avaient été perdues (cf. note ci-dessous). Sans elles,
    # activer le mode tranche sur un mur hot-applique en silence : la base annonce `gpu_slice: True`,
    # le conteneur exécute un script qui n'en sait rien, et l'écart n'apparaît nulle part. Vérifié
    # hors ligne sur les six transitions de `gpu_slice` — les six rendaient « hot-apply ».
    "slice_mode", "slice_lines", "gpu_slice", "gpu_batch_bands",
    "force_cpu", "interlace_mode", "vars_check_s",
    # `cadence` manquait — MÊME PIÈGE que `gpu_slice`, trouvé le 2026-08-12. Elle est lue à
    # l'import (`CADENCE = str(CONFIG.get("cadence", "genlock"))`) et commande le mode FLOW,
    # c'est-à-dire le CIBLAGE D'INDEX de tout l'étage. La changer hot-appliquait : la base
    # annonçait `flow`, le conteneur exécutait un script qui n'en savait rien, et rien ne
    # signalait l'écart — `deployer_script` renvoyait True.
    "cadence",
})

def _multiview_only_hot_changed(old_params, new_params):
    """True si aucun param structurel n'a changé → hot-apply possible."""
    for k in _MULTIVIEW_STRUCTURAL:
        if str(old_params.get(k) or "") != str(new_params.get(k) or ""):
            return False
    return True

def _gras(params):
    """Params « GRAS » (destinés au CONTENEUR) : les polices de bibliothèque référencées par
    ces params (clés `lib:<sha16>`) sont EMBARQUÉES en base64 dans `font_library` — même motif
    que le logo de marque : le rootfs du conteneur est éphémère et l'agent :8081/deploy ne
    pousse que du texte, donc la police voyage avec le script / le hot-apply.

    ⚠ SÉPARATION MAIGRE/GRAS : la version grasse (jusqu'à 8 Mo de base64) ne doit JAMAIS être
    persistée (`db_update_deploy_config`) ni comparée (`_multiview_only_hot_changed`) — la
    bibliothèque (disque + table `fonts`) est la source de vérité, les références `lib:` suffisent
    en base. On ne rebind donc jamais `params` avec ce retour : on l'utilise à l'ENVOI seulement.
    No-op quand aucune police de la bibliothèque n'est référencée."""
    try:
        from . import fonts as _fonts
        return _fonts.resolve_params(params)
    except Exception as _e:
        log.warning("injection polices : %s", _e)
        return params


def _multiview_hot_apply(ip, params):
    """Applique style + géométrie + sources à chaud via :8082.
    Retourne True si tout a réussi, False → redéploiement complet requis."""
    import requests as _rq
    T = 2
    try:
        style = {k: params[k] for k in
                 ("show_no_signal", "freeze_detect_s", "show_proxy", "default_template")
                 if k in params}
        if style:
            r = _rq.post(f"http://{ip}:8082/style", json=style, timeout=T)
            if r.status_code != 200:
                return False
        from .monitor import _shm_fmt
        fc = []
        for entry in (params.get("flux_config") or []):
            e = dict(entry)
            path = (e.get("path") or "").replace("/dev/shm/", "")
            if path:
                fmt = _shm_fmt(path)
                if fmt:
                    if not (e.get("in_w") and e.get("in_h")):
                        e["in_w"], e["in_h"] = fmt["w"], fmt["h"]
                    # Format déclaré du producteur (chip « format source ») : toujours rafraîchi.
                    e["in_fps"], e["in_scan"] = fmt.get("fps") or "", fmt.get("scan") or "p"
            fc.append(e)
        # meter_blocks : blocs VU-mètres de MUR (indépendants des fenêtres, cf. render_meters) —
        # même remplacement atomique que flux_config, aucune résolution de dims/proxy nécessaire
        # (pas de vidéo, juste une source audio par bloc).
        r = _rq.post(f"http://{ip}:8082/reconfigure",
                     json={"flux_config": fc,
                           "meter_blocks": params.get("meter_blocks") or [],
                           # Blocs d'historique vidéo/audio (0.37.0) : mêmes conventions que
                           # meter_blocks — remplacement atomique, aucune résolution de dims.
                           "video_history_blocks": params.get("video_history_blocks") or [],
                           "audio_history_blocks": params.get("audio_history_blocks") or [],
                           # Polices de la bibliothèque référencées par la NOUVELLE config
                           # (injectées en base64 par fonts.resolve_params juste avant) : sans
                           # elles, un hot-apply qui introduit une police téléversée la ferait
                           # retomber sur DejaVu jusqu'au prochain redéploiement complet.
                           "font_library": params.get("font_library") or []},
                     timeout=T)
        if r.status_code != 200:
            return False
        ov = params.get("overlays")
        if ov is not None:
            r = _rq.post(f"http://{ip}:8082/overlays", json={"overlays": ov}, timeout=T)
            if r.status_code != 200:
                return False
        return True
    except Exception:
        return False


def _apply_pipeline_bit_depth(params, mode):
    """Résout le mode d'arbitrage de profondeur du pipeline shm en une valeur concrète
    écrite dans `params["bit_depth"]` (= profondeur des frames shared memory, lue par le
    script du plugin). `video.bit_depth` (encode de SORTIE) n'est PAS touché — il reste
    piloté par le format d'encodage du plugin (ex. recorder ProRes 10 bits).

    - "force8"  → 8 bits imposé partout (défaut : perfs/mémoire actuelles, zéro régression).
    - "follow"  → profondeur du format vidéo, clampée {8,10,12} via normalize_bit_depth.
    """
    from .scripts import normalize_bit_depth
    if mode == "follow":
        params["bit_depth"] = normalize_bit_depth(params.get("bit_depth"))
    else:
        params["bit_depth"] = 8


def _proxy_flux_lisible(shm, pyr_node_id, node_id):
    """Le proxy `shm`, produit sur `pyr_node_id`, est-il LISIBLE depuis `node_id` ?

    Même nœud → oui (même /dev/shm). Nœud distinct → seulement si une réplication RDMA de CE flux
    y est RUNNING. On exige `running`, pas `pending` : injecter un chemin dont la réplication n'est
    pas encore établie ferait pointer un consommateur sur un flux absent. Le lien est demandé par
    l'appelant ; à la passe suivante, une fois établi, le proxy est injecté. Auto-amorçage, sans
    jamais désigner un flux mort."""
    try:
        if int(pyr_node_id) == int(node_id):
            return True
    except (TypeError, ValueError):
        return False
    try:
        from .database import db_list_rdma_links
        for _l in db_list_rdma_links():
            if (_l.get("src_flow") == shm and _l.get("status") == "running"
                    and int(_l.get("dst_node_id") or -1) == int(node_id)):
                return True
    except Exception:                                                      # noqa: BLE001
        return False
    return False


def _proxies_for_source(src_name, node_id):
    """Proxies pyramide utilisables pour `src_name` par un consommateur du nœud `node_id` :
    [{path:/dev/shm/<src>__pL, w, h}], ou [] si aucun. Source de vérité des dimensions = la
    pyramide elle-même (derive_wiring.produces) → identique octet-pour-octet à ce qu'elle écrit.

    ⚠ LA PYRAMIDE N'A PAS À ÊTRE SUR LE NŒUD DU CONSOMMATEUR. La règle portait sur « qui a
    PRODUIT » (`_c["node_id"] != node_id → continue`) alors qu'elle doit porter sur « est-ce
    LISIBLE ICI ». La différence est architecturale : réduire À LA SOURCE et ne faire traverser
    que les proxies, c'est ~4,8× moins de données sur les liens RDMA que de répliquer du 1080p
    pour le réduire à l'arrivée (832×482 contre 1920×1080). Constaté le 2026-08-07 : une pyramide
    déployée près des sources (dl360-1) restait ignorée des shards de dell-1, qui continuaient à
    réduire eux-mêmes du 1080p — 12 ms d'`inputs` par trame contre 3,4 pour un nœud qui recopie.

    La réplication du proxy est DEMANDÉE ici (ensure_cable_link, best-effort) mais le proxy n'est
    injecté qu'une fois le lien `running` (cf. _proxy_flux_lisible)."""
    if not src_name or node_id is None:
        return []
    import json as _json
    from . import plugins as _pl
    for _c in db_get_containers():
        try:
            _dc = _json.loads(_c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        if _dc.get("type") != "pyramide":
            continue
        _pp = _dc.get("params") or {}
        try:
            _n = int(_pp.get("n_inputs") or 8)
        except (TypeError, ValueError):
            _n = 8
        if not any(_pp.get(cle_input(_k)) == src_name for _k in range(_n)):
            continue
        _w = _pl.derive_wiring("pyramide", _c.get("hostname") or "", _pp)
        _pnid = _c.get("node_id")
        _distant = str(_pnid) != str(node_id)
        _out = []
        for _pr in _w.get("produces") or []:
            _shm = _pr.get("shm") or ""
            _f = _pr.get("format") or {}
            if not (_shm.startswith(src_name + "__p") and _f.get("width") and _f.get("height")):
                continue
            if _distant:
                # Pyramide d'un AUTRE nœud : demander la réplication (idempotent, dédupliqué)…
                try:
                    from services import rdma as _rdma
                    _rdma.ensure_cable_link(_pnid, _c.get("vmid"), _shm, node_id, kind="video")
                except Exception as _e:                                    # noqa: BLE001
                    log.warning("proxy %s : réplication vers le nœud %s non demandée (%s)",
                                _shm, node_id, _e)
                # … et n'injecter que si elle est EFFECTIVE (sinon on pointerait un flux absent).
                if not _proxy_flux_lisible(_shm, _pnid, node_id):
                    continue
            _out.append({"path": "/dev/shm/" + _shm,
                         "w": int(_f["width"]), "h": int(_f["height"])})
        if _out:
            return _out
    return []


_MAX_CUSTOM_PER_SRC = 6   # cap de tailles sur-mesure produites par source (anti-prolifération)

# ─── Pyramide pilotée par la demande : état partagé (reconcile concurrent → sous _pyr_lock) ───
import time as _time
import threading as _threading
_pyr_lock = _threading.Lock()
_WIRE_GRACE_S = 10.0          # délai avant de décâbler une source qui n'est plus demandée (anti-flap)
_wire_absent_since = {}       # (vmid, slot) → t du 1er cycle « plus demandée »
_wire_cap_last = {}           # node_id → t du dernier avertissement « capacité insuffisante » (throttle 60s)
_LAT_HI_FRAC = 0.9            # latency_ms > 0.9*intervalle → saturation : interdire ajout + délester
_LAT_LO_FRAC = 0.8            # latency_ms < 0.8*intervalle → ré-autoriser les ajouts (hystérésis)
_pyr_capped = {}             # (vmid, src) → bool (état d'hystérésis du cap réactif)
_core_drift_seen = {}        # vmid → (have, want) déjà signalé (throttle alerte dérive cœurs)
_core_resize_done = {}       # vmid → want déjà déclenché en recréation auto (anti-boucle)


def _bucketize_needs(raw, tol=0.02):
    """Regroupe des tailles proches (≤ tol sur W ET H), garde la PLUS GRANDE du bucket (downscale)
    et SOMME les comptes. raw = [(w,h,count)…] → [(w,h,total)…]."""
    clusters = []
    for w, h, c in sorted(raw, key=lambda t: -(t[0] * t[1])):
        for cl in clusters:
            if abs(cl[0] - w) <= tol * cl[0] and abs(cl[1] - h) <= tol * cl[1]:
                cl[0] = max(cl[0], w); cl[1] = max(cl[1], h); cl[2] += c
                break
        else:
            clusters.append([w, h, c])
    return [(cl[0], cl[1], cl[2]) for cl in clusters]


def _extra_norm(d):
    """Forme canonique comparable d'un dict extra_sizes (pour détecter un changement réel)."""
    return {k: sorted([[int(x[0]), int(x[1])] for x in v]) for k, v in (d or {}).items() if v}


def _set_pyramide_input(c, slot, src, fmt):
    """POST :8082/input (hot) pour (dé)câbler une source dans un slot. Pousse le `format` résolu en
    DB (pas de devinette SHM côté pyramide). `src=None` libère le slot. True si appliqué."""
    ip = c.get("ip") or get_container_ip(c.get("vmid"))
    if not ip:
        return False
    body = {"slot": int(slot), "shm": src}
    if src and fmt and fmt.get("w") and fmt.get("h"):
        body["format"] = {"width": int(fmt["w"]), "height": int(fmt["h"]),
                          "chroma": fmt.get("chroma"), "bit_depth": fmt.get("bit_depth")}
    try:
        r = requests.post(f"http://{ip}:8082/input", json=body, timeout=2)
        return r.status_code == 200
    except Exception as _e:
        log.warning("pyramide %s /input slot %s: %s", c.get("vmid"), slot, _e)
        return False


def _pyramide_state(c):
    """`GET :8082/state` d'une pyramide → dict, ou **None si injoignable**.

    Le None est signifiant : TOUT le câblage d'une pyramide (slots d'entrée ET tailles sur-mesure)
    est du RUNTIME, jamais du disque. Conclure « rien n'est câblé » d'une requête ratée ferait
    re-pousser tout le câblage à une pyramide simplement en train de démarrer."""
    ip = c.get("ip") or get_container_ip(c.get("vmid"))
    if not ip:
        return None
    try:
        r = requests.get(f"http://{ip}:8082/state", timeout=2)
        if r.status_code != 200:
            return None
        return r.json() or {}
    except Exception as _e:
        log.debug("pyramide %s /state: %s", c.get("vmid"), _e)
        return None


def _pyramide_runtime_inputs(c, st=None):
    """Slots réellement câblés DANS LE PROCESS de la pyramide (`/state` → `input_{i}`).
    Renvoie {slot: shm}, ou None si la pyramide est injoignable (cf. `_pyramide_state`)."""
    if st is None:
        st = _pyramide_state(c)
    if st is None:
        return None
    out = {}
    # ── FORME RÉELLE DU /state : {"inputs": {"0": "<shm>", "1": null, …}} ────────────────────
    # Dict IMBRIQUÉ, clés = numéro de slot en CHAÎNE, 0-based, valeur = nom de shm ou null.
    # Ce parcours cherchait des clés `input_N` de PREMIER NIVEAU, que la pyramide n'émet pas :
    # il rendait donc TOUJOURS {} → l'appelant concluait « câblage runtime perdu », re-poussait
    # tout le câblage et écrivait une alerte, À CHAQUE TOUR. Mesuré le 2026-08-15 : 879 alertes,
    # dont 637 sur les seules 24 h — pour une pyramide dont le câblage était parfaitement en place.
    # Un lecteur qui ne sait pas lire son témoin ne rapporte pas « je ne sais pas » : il rapporte
    # « tout est cassé », et déclenche une réparation permanente.
    src = st.get("inputs")
    if isinstance(src, dict):
        for k, v in src.items():
            if not v:
                continue                       # slot libre : absent du résultat, comme avant
            k = str(k)
            if k.isdigit():
                out[int(k)] = v.split("/")[-1] if isinstance(v, str) else v
        return out
    # Repli : forme À PLAT `input_N` au premier niveau (jamais observée sur la pyramide, gardée
    # au cas où un autre producteur adopterait ce contrat).
    for k, v in st.items():
        if k.startswith("input_") and v:
            n = k[len("input_"):]
            if n.isdigit():
                out[int(n)] = v
    return out


def _pyr_input_fmt(fmt):
    """Forme CANONIQUE de `input_{i}_fmt` (clés attendues par plugins.derive_wiring : width/height/
    chroma/…) à partir du fmt résolu par monitor._shm_fmt (w/h/chroma/bit_depth/colorimetry). {} si
    dimensions inconnues. Persisté au câblage → derive_wiring peut lister les proxies SUR-MESURE
    (sinon console Pyramide / topologie vides dès que base_octaves=none, les octaves ne masquant
    plus l'absence de format). Cf. format-from-db-not-shm."""
    if not fmt or not fmt.get("w") or not fmt.get("h"):
        return {}
    d = {"width": int(fmt["w"]), "height": int(fmt["h"]), "chroma": fmt.get("chroma") or "422"}
    if fmt.get("bit_depth"):
        d["bit_depth"] = fmt["bit_depth"]
    if fmt.get("colorimetry"):
        d["colorimetry"] = fmt["colorimetry"]
    return d


def _persist_pyramide_params(c, dc, p):
    """Persiste les params de la pyramide (DB) ET met à jour le deploy_config du dict `c` local
    (pour que la suite du reconcile, qui re-parse `c['deploy_config']`, voie le câblage à jour)."""
    import json as _json
    dc["params"] = p
    db_update_deploy_config(c.get("vmid"), "pyramide", p)
    try:
        c["deploy_config"] = _json.dumps(dc)
    except Exception:
        pass


def _reconcile_pyramide_wiring(node_id, conts, wanted_sources):
    """AUTO-CÂBLAGE piloté par la demande : assigne chaque source réclamée (proxy_needs) à un slot de
    pyramide libre du nœud (hot + format DB + persist), et libère les slots dont la source n'est plus
    demandée (après un délai de grâce anti-flap). Mute deploy_config (DB + conts en place)."""
    import json as _json
    from .monitor import _shm_fmt
    pyrs = []
    for c in conts:
        try:
            dc = _json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        if dc.get("type") == "pyramide":
            pyrs.append((c, dc, dc.get("params") or {}))
    if not pyrs:
        return
    def _n(p):
        try: return int(p.get("n_inputs") or 8)
        except (TypeError, ValueError): return 8
    now = _time.time()
    # sources déjà câblées quelque part sur le nœud
    wired = set()
    for (_c, _dc, p) in pyrs:
        for i in range(_n(p)):
            s = p.get(cle_input(i))
            if s:
                wired.add(s)
    # 0. Backfill du format des slots DÉJÀ câblés sans `input_{i}_fmt` persisté (résolu via DB,
    #    idempotent) → derive_wiring peut lister les proxies sur-mesure (console/topologie).
    for (c, dc, p) in pyrs:
        changed = False
        for i in range(_n(p)):
            s = p.get(cle_input(i))
            if s and not p.get(cle_input(i, fmt=True)):
                try:
                    cf = _pyr_input_fmt(_shm_fmt(s))
                except Exception:
                    cf = {}
                if cf:
                    p[cle_input(i, fmt=True)] = cf
                    changed = True
        if changed:
            _persist_pyramide_params(c, dc, p)
    # 0b. RESYNC RUNTIME. Le câblage d'une pyramide est HOT (`mode: hot-wire`) : il vit dans le
    #     process, pas sur disque. Tout redémarrage du script — SIGBUS d'un producteur, restart de
    #     l'agent, reboot du nœud, simple stop/start — le PERD, pendant que la DB continue
    #     d'affirmer que les slots sont câblés. Le reconcile, qui ne lisait que la DB, se croyait
    #     alors idempotent (`changed: []`) et laissait la pyramide à **0 fps DÉFINITIVEMENT** ; les
    #     consommateurs, eux, cessaient en silence de trouver des proxies et repassaient au plein
    #     format. Aucun signal nulle part — l'anti-patron de l'échec silencieux, constaté au banc le
    #     2026-07-29 après un simple stop/start du script. Même remède que pour le moteur 2110 après
    #     recréation (cf. resync_moteur) : c'est le RUNTIME qui fait foi pour savoir ce qui est
    #     appliqué, la DB ne dit que ce qui est VOULU.
    for (c, dc, p) in pyrs:
        live = _pyramide_runtime_inputs(c)
        if live is None:
            continue                      # injoignable : on ne conclut rien (jamais sur du vide)
        manquants = [i for i in range(_n(p))
                     if p.get(cle_input(i)) and live.get(i) != p.get(cle_input(i))]
        if not manquants:
            continue
        remis = 0
        for i in manquants:
            s = p.get(cle_input(i))
            fmt = None
            _f = p.get(cle_input(i, fmt=True)) or {}
            if _f.get("width") and _f.get("height"):
                fmt = {"w": _f["width"], "h": _f["height"],
                       "chroma": _f.get("chroma"), "bit_depth": _f.get("bit_depth")}
            else:
                try:
                    fmt = _shm_fmt(s)
                except Exception:
                    fmt = None
            if _set_pyramide_input(c, i, s, fmt):
                remis += 1
        if remis:
            db_add_alert("alert.resource.pyramide_wiring_perdu",
                         "warning", vmid=c.get("vmid"), node_id=node_id, kind="resource",
                         params={"h": c.get("hostname") or c.get("vmid"),
                                 "remis": remis, "total": len(manquants)})
    # 1. Libérer les slots dont la source n'est plus demandée (grâce anti-flap)
    for (c, dc, p) in pyrs:
        vmid = c.get("vmid")
        for i in range(_n(p)):
            s = p.get(cle_input(i))
            with _pyr_lock:
                if not s or s in wanted_sources:
                    _wire_absent_since.pop((vmid, i), None)
                    grace_ok = False
                else:
                    t0 = _wire_absent_since.setdefault((vmid, i), now)
                    grace_ok = (now - t0) >= _WIRE_GRACE_S
            if s and s not in wanted_sources and grace_ok:
                if _set_pyramide_input(c, i, None, None):
                    p[cle_input(i)] = ""
                    p.pop(cle_input(i, fmt=True), None)
                    _persist_pyramide_params(c, dc, p)
                    wired.discard(s)
                    with _pyr_lock:
                        _wire_absent_since.pop((vmid, i), None)
    # 2. Câbler les sources demandées non encore câblées
    unplaced = []
    for src in sorted(s for s in wanted_sources if s and s not in wired):
        placed = False
        for (c, dc, p) in pyrs:
            slot = next((i for i in range(_n(p)) if not p.get(cle_input(i))), None)
            if slot is None:
                continue
            try:
                fmt = _shm_fmt(src)
            except Exception:
                fmt = None
            if _set_pyramide_input(c, slot, src, fmt):
                p[cle_input(slot)] = src
                p[cle_input(slot, fmt=True)] = _pyr_input_fmt(fmt)
                _persist_pyramide_params(c, dc, p)
                wired.add(src)
                placed = True
                break
        if not placed:
            unplaced.append(src)
    if unplaced:
        with _pyr_lock:
            last = _wire_cap_last.get(node_id, 0)
            do = (now - last) >= 60.0
            if do:
                _wire_cap_last[node_id] = now
        if do:
            _liste = ", ".join(unplaced[:4])
            _reste = len(unplaced) - 4
            if _reste > 0:
                db_add_alert("alert.resource.pyramide_capacite_insuffisante_reste", "warning",
                             node_id=node_id, kind="resource",
                             params={"n": node_id, "nb": len(unplaced), "liste": _liste, "reste": _reste})
            else:
                db_add_alert("alert.resource.pyramide_capacite_insuffisante", "warning",
                             node_id=node_id, kind="resource",
                             params={"n": node_id, "nb": len(unplaced), "liste": _liste})


def _check_pyramide_core_drift(c, params, read_count):
    """Dérive cœurs : compare cœurs alloués vs voulus (charge auto-câblée, pondérée résolution). Le
    cpuset Docker n'étant pas modifiable à chaud : recréation AUTO seulement si AUCUN consommateur
    (tous les proxies orphelins), sinon ALERTE (warning sous-dim / info sur-dim). Best-effort."""
    from . import core_pool, docker_compute as _dc
    vmid = c.get("vmid"); node_id = c.get("node_id")
    if not node_id:
        return
    res = (plugins.get("pyramide") or {}).get("resources") or {}
    try:
        per = float((params or {}).get("cores_per_1080p_input") or res.get("cores_per_1080p_input") or 0)
    except (TypeError, ValueError):
        per = 0.0
    if per <= 0 or not res.get("pin"):
        return
    base = int(res.get("cores") or 1)
    _, load = _dc.pyramide_input_load(params)
    total = core_pool.cores_status(node_id)["total"]
    import math as _math
    want = min(max(base, base + _math.ceil(per * load)), max(base, total))
    have = core_pool.allocated_for(node_id, vmid)
    if have == 0 or have == want:
        return
    # Tous les proxies de cette pyramide orphelins ? (aucun consommateur)
    consumers = 0
    try:
        for prod in plugins.derive_wiring("pyramide", params.get("hostname") or c.get("hostname") or "",
                                          params).get("produces") or []:
            consumers += int(read_count.get(prod.get("shm") or "", 0))
    except Exception:
        consumers = 1   # en cas de doute, ne pas recréer
    if consumers == 0:
        with _pyr_lock:
            already = _core_resize_done.get(vmid) == want
            if not already:
                _core_resize_done[vmid] = want
        if not already:
            db_add_alert("alert.resource.pyramide_redim", "info", vmid=vmid, kind="resource",
                         params={"h": c.get("hostname") or vmid, "have": have, "want": want})
            # Redéploiement complet (deploy_compute fait `docker rm -f` + run → release+realloc du cpuset).
            # En thread pour ne pas bloquer le reconcile. Anti-boucle : _core_resize_done garde le `want`.
            try:
                _t = _threading.Thread(target=deployer_script, args=(vmid, "pyramide", dict(params)),
                                       daemon=True)
                _t.start()
            except Exception as _e:
                log.warning("recréation pyramide %s (resize cœurs): %s", vmid, _e)
        return
    # Consommateurs présents → pas de coupure : alerte (throttlée sur le couple have/want)
    with _pyr_lock:
        changed = _core_drift_seen.get(vmid) != (have, want)
        if changed:
            _core_drift_seen[vmid] = (have, want)
    if changed:
        if have < want:
            db_add_alert("alert.resource.pyramide_sous_dim", "warning", vmid=vmid, kind="resource",
                         params={"h": c.get("hostname") or vmid, "have": have, "want": want})
        else:
            db_add_alert("alert.resource.pyramide_sur_dim", "info", vmid=vmid, kind="resource",
                         params={"h": c.get("hostname") or vmid, "have": have, "want": want,
                                  "diff": have - want})


def _apply_reactive_cap(vmid, slot, src, kept, old_extra):
    """CAP RÉACTIF : si le worker de ce slot frôle son budget de trame (latency_ms vs intervalle dérivé
    du fps live), interdit tout AJOUT (n'autorise que les tailles déjà servies) et déleste la moins
    demandée (1/cycle). Hystérésis 0,8/0,9 anti-flapping. Pas de signal fiable → comportement inchangé.
    Renvoie la liste `kept` éventuellement réduite."""
    try:
        from . import metrics as _m
        live = (_m.pyr_sources_cache.get(vmid) or {}).get(str(slot))
    except Exception:
        live = None
    if not live or live.get("latency_ms") is None or not (live.get("fps") or 0):
        return kept   # conteneur pas up / source figée → ne pas caper
    interval = 1000.0 / float(live["fps"])
    lat = float(live["latency_ms"])
    key = (vmid, src)
    with _pyr_lock:
        capped = _pyr_capped.get(key, False)
        if lat > _LAT_HI_FRAC * interval:
            capped = True
        elif lat < _LAT_LO_FRAC * interval:
            capped = False
        was = _pyr_capped.get(key, False)
        _pyr_capped[key] = capped
    if not capped:
        return kept
    old_set = {(int(x[0]), int(x[1])) for x in (old_extra.get(src) or [])}
    kept = [b for b in kept if (b[0], b[1]) in old_set]   # interdit tout AJOUT sous saturation
    if kept:
        kept = kept[:-1]                                  # déleste la taille la moins demandée
    if not was:
        db_add_alert("alert.resource.pyramide_saturee", "warning", vmid=vmid, kind="resource",
                     params={"vmid": vmid, "src": src, "lat": lat, "interval": interval})
    return kept


def pousser_telemetrie():
    """Pousse la santé du NŒUD et du CONTRÔLEUR vers les murs qui l'AFFICHENT.

    Un conteneur ne voit que son propre cgroup : il ne peut pas mesurer la machine qui le porte.
    L'orchestrateur, lui, échantillonne déjà les deux — il les pousse donc, plutôt que de laisser
    un mur interroger l'API du contrôleur (ce qui demanderait une adresse, un jeton, et créerait
    une dépendance inverse pour un simple affichage).

    N'écrit QU'AUX murs dont un texte contient une de ces variables : sans ce filtre, chaque cycle
    enverrait une requête à tous les conteneurs du parc pour rien. Best-effort et silencieux — un
    mur injoignable a déjà ses propres alertes, ce n'est pas à un indicateur d'en ajouter."""
    import json as _json
    from .database import db_get_node
    try:
        from . import node_health as _nh
        snap = _nh.latest() or {}
    except Exception:                                                      # noqa: BLE001
        return 0
    noeuds = snap.get("nodes") or {}
    ctrl = snap.get("controller") or {}

    def _bloc(sn, nom=None, node_id=None):
        r = (sn or {}).get("resources") or {}
        mt, mu = r.get("mem_total_mb"), r.get("mem_used_mb")
        d = (sn or {}).get("disks") or {}
        out = {"cpu_pct": None if r.get("cpu_pct") is None else round(r["cpu_pct"], 1),
               "ram_used_mb": mu, "ram_total_mb": mt,
               "ram_pct": (round(mu / mt * 100.0, 1) if (mt and mu is not None) else None),
               "disk_pct": (d.get("root") or {}).get("pct"),
               "load1": r.get("load1"), "temp_c": ((sn or {}).get("sensors") or {}).get("cpu_c")}
        if nom:
            out["nom"] = nom
        # RDMA : REMPLISSAGE des liens de réplication. Le trafic est la somme des ports actifs, le
        # dénominateur leur débit NOMINAL cumulé — c'est la question « reste-t-il de la place ? »,
        # celle qu'on se pose avant d'y faire passer un flux de plus. Un nœud sans RDMA ne publie
        # rien plutôt que zéro : « 0 % » laisserait croire à un lien vide alors qu'il n'y en a pas.
        try:
            _rd = (sn or {}).get("rdma") or {}
            _devs = [x for x in (_rd.get("devices") or []) if (x.get("state") or "").upper() == "ACTIVE"]
            if _devs:
                _rx = sum(x.get("rx_gbps") or 0.0 for x in _devs)
                _tx = sum(x.get("tx_gbps") or 0.0 for x in _devs)
                _rate = sum(x.get("rate_gbps") or 0.0 for x in _devs)
                out["rdma_rx_gbps"] = round(_rx, 2)
                out["rdma_tx_gbps"] = round(_tx, 2)
                out["rdma_rate_gbps"] = round(_rate, 1) or None
                out["rdma_pct"] = round((_rx + _tx) / _rate * 100.0, 1) if _rate else None
                out["rdma_liens"] = len(_devs)
        except Exception:                                                  # noqa: BLE001
            pass
        return out

    envoyes = 0
    for c in db_get_containers():
        try:
            dc = _json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:                                                  # noqa: BLE001
            continue
        if dc.get("type") != "multiview" or (c.get("status") or "") != "running":
            continue
        _txt = _json.dumps(dc.get("params") or {}, ensure_ascii=False)
        if not any(("%%%s%%" % v) in _txt for v in
                   ("cpu_noeud", "ram_noeud", "ram_noeud_mo", "disque_noeud", "temp_noeud",
                    "charge_noeud", "nom_noeud", "cpu_orch", "ram_orch", "disque_orch")):
            continue
        ip = c.get("ip") or get_container_ip(c.get("vmid"))
        if not ip:
            continue
        _n = db_get_node(c.get("node_id")) or {}
        # Le nœud de CE mur, le contrôleur, ET tous les nœuds nommés — pour qu'un mur de
        # supervision puisse afficher n'importe lequel via `%cpu_noeud:dl360-1%`. Le parc tient en
        # quelques nœuds : envoyer le tout coûte moins qu'un protocole d'abonnement.
        _tous = {}
        for _nid, _sn in noeuds.items():
            try:
                _nn = (db_get_node(int(_nid)) or {}).get("name")
            except (TypeError, ValueError):
                _nn = None
            if _nn:
                _tous[_nn] = _bloc(_sn, _nn)
        corps = {"noeud": _bloc(noeuds.get(str(c.get("node_id"))), _n.get("name")),
                 "orchestrateur": _bloc(ctrl),
                 "noeuds": _tous}
        try:
            requests.post(f"http://{ip}:8082/telemetry", json=corps, timeout=1.5)
            envoyes += 1
        except Exception:                                                  # noqa: BLE001
            pass
    return envoyes


def reconcile_pyramide_sizes(node_id):
    """Agrège les besoins de tailles (proxy_needs :8080) de TOUS les consommateurs du nœud, par
    (source, taille bucketisée), et met à jour `extra_sizes` des pyramides. SEUIL SOUPLE : sert
    TOUTES les tailles demandées jusqu'au cap (`_MAX_CUSTOM_PER_SRC`/source), priorité au count
    décroissant ; `custom_size_threshold` ne départage le surplus que s'il y a PLUS de tailles
    distinctes que le cap (sinon il n'écarte plus rien — une taille demandée par une seule fenêtre
    serait sinon lue en `gather`). Redéploie une pyramide UNIQUEMENT si son ensemble de tailles
    change (hot-apply, anti deploy-storm)."""
    import json as _json
    try:
        tous = db_get_containers()
        conts = [c for c in tous if c.get("node_id") == node_id]
    except Exception as _e:
        log.warning("reconcile pyramide: lecture containers échouée : %s", _e)
        return {"changed": []}
    # DEMANDE INTER-NŒUD. Une pyramide sert les sources produites SUR SON NŒUD, à qui que ce soit —
    # y compris un consommateur d'un autre nœud. C'est tout l'intérêt de réduire À LA SOURCE :
    # ne faire traverser que des proxies (~4,8× moins de données qu'un 1080p répliqué pour être
    # réduit à l'arrivée). Sans cet élargissement, une pyramide placée près des sources n'apprend
    # jamais ce qu'on lui demande et reste à vide — constaté le 2026-08-07 sur dl360-1.
    # On ne retient d'un consommateur DISTANT que les sources produites ICI : chaque pyramide
    # reste responsable de son propre voisinage.
    _prod_ici = set()
    for _c in conts:
        for _tok in str(_c.get("shm_out") or "").split("·"):
            _tok = _tok.strip()
            if _tok and " " not in _tok and ":" not in _tok:
                _prod_ici.add(_tok)
    _distants = [c for c in tous if c.get("node_id") != node_id]
    # 1. Besoins agrégés par source : src -> [(w,h,count)…]
    agg = {}
    for c in conts + _distants:
        _loin = c.get("node_id") != node_id
        try:
            dc = _json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        if dc.get("type") == "pyramide":
            continue
        ip = c.get("ip") or get_container_ip(c.get("vmid"))
        if not ip:
            continue
        try:
            m = requests.get(f"http://{ip}:8080/", timeout=1.0).json()
        except Exception:
            continue
        needs = m.get("proxy_needs")
        if not isinstance(needs, dict):
            continue
        for src, items in needs.items():
            if _loin and src not in _prod_ici:
                continue      # source d'un autre voisinage : ce n'est pas à cette pyramide-ci
            for it in (items or []):
                try:
                    w = int(it[0]); h = int(it[1]); cnt = int(it[2]) if len(it) > 2 else 1
                except (TypeError, ValueError, IndexError):
                    continue
                if w >= 2 and h >= 2:
                    agg.setdefault(src, []).append((w, h, max(1, cnt)))
    # 1b. AUTO-CÂBLAGE : les sources réclamées (clés de proxy_needs) sont assignées aux slots de
    # pyramide libres du nœud (hot), les inutiles libérées. Mute deploy_config (DB + conts) → la
    # boucle extra_sizes ci-dessous re-parse et voit le câblage à jour.
    wanted_sources = {s for s in agg.keys() if s}
    try:
        _reconcile_pyramide_wiring(node_id, conts, wanted_sources)
    except Exception as _e:
        log.warning("auto-câblage pyramide nœud %s: %s", node_id, _e)
    # read_count : nb de consommateurs par shm (pour la détection « pyramide orpheline » de la dérive cœurs).
    read_count = {}
    try:
        from . import metrics as _m
        for _v, _lst in (_m.proxy_read_cache or {}).items():
            for _sh in (_lst or []):
                read_count[_sh] = read_count.get(_sh, 0) + 1
    except Exception:
        read_count = {}
    # 2. Mise à jour des pyramides du nœud
    changed = []
    for c in conts:
        try:
            dc = _json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        if dc.get("type") != "pyramide":
            continue
        p = dc.get("params") or {}
        try:
            thr = max(1, int(p.get("custom_size_threshold") or 2))
        except (TypeError, ValueError):
            thr = 2
        try:
            n = int(p.get("n_inputs") or 8)
        except (TypeError, ValueError):
            n = 8
        new_extra = {}
        old_extra = p.get("extra_sizes") if isinstance(p.get("extra_sizes"), dict) else {}
        vmid = c.get("vmid")
        for i in range(n):
            src = p.get(cle_input(i))
            if not src:
                continue
            buckets = _bucketize_needs(agg.get(src) or [])
            buckets.sort(key=lambda b: -b[2])   # count décroissant = priorité
            # SEUIL SOUPLE : on sert TOUTES les tailles demandées jusqu'au cap (les tailles
            # meter-réduites demandées par UNE seule fenêtre — non-diviseur-entier de la source —
            # tombaient sinon en `gather`, le cas exact que la pyramide doit éliminer). Le cap borne
            # la prolifération ; `custom_size_threshold` ne départage QUE le surplus quand il y a
            # PLUS de tailles distinctes que le cap (sinon il n'écarte plus rien).
            if len(buckets) > _MAX_CUSTOM_PER_SRC:
                buckets = [b for b in buckets if b[2] >= thr]
            kept = buckets[:_MAX_CUSTOM_PER_SRC]
            kept = _apply_reactive_cap(vmid, i, src, kept, old_extra)   # CAP RÉACTIF (latence)
            if kept:
                new_extra[src] = [[w, h] for (w, h, _cnt) in kept]
        # Dérive cœurs (alerte, ou recréation auto si aucun consommateur) — toujours évaluée.
        try:
            _check_pyramide_core_drift(c, p, read_count)
        except Exception as _e:
            log.warning("dérive cœurs pyramide %s: %s", vmid, _e)
        # Le garde-fou d'idempotence compare le VOULU à la DB — mais la DB ne dit que ce qu'on a
        # demandé, jamais ce que le process produit. Les tailles sur-mesure sont du RUNTIME au même
        # titre que les slots d'entrée : un redémarrage du script les efface, et avec
        # `base_octaves: none` elles sont TOUTE la production → la pyramide ne publie plus RIEN
        # pendant que la DB affirme le contraire. Constaté au banc le 2026-07-29 : 8 tailles en base,
        # `/state.proxies == []`. On ne saute donc le hot-apply que si la DB est à jour ET que le
        # runtime produit effectivement les proxies attendus.
        _st = _pyramide_state(c)
        _manque = []
        if _st is not None:
            _vus = set(_st.get("proxies") or ())
            try:
                _p_att = dict(p); _p_att["extra_sizes"] = new_extra
                _att = {(_pr.get("shm") or "").removeprefix("/dev/shm/")
                        for _pr in (plugins.derive_wiring("pyramide", c.get("hostname") or "",
                                                          _p_att).get("produces") or [])}
                _manque = sorted(x for x in _att if x and x not in _vus)
            except Exception as _e:
                log.debug("pyramide %s attendus: %s", c.get("vmid"), _e)
        if _extra_norm(new_extra) == _extra_norm(old_extra) and not _manque:
            continue
        if _manque:
            _liste = ", ".join(_manque[:3])
            _reste = len(_manque) - 3
            _h = c.get("hostname") or c.get("vmid")
            if _reste > 0:
                db_add_alert("alert.resource.pyramide_proxies_absents_reste", "warning",
                             vmid=c.get("vmid"), node_id=node_id, kind="resource",
                             params={"h": _h, "nb": len(_manque), "liste": _liste, "reste": _reste})
            else:
                db_add_alert("alert.resource.pyramide_proxies_absents", "warning",
                             vmid=c.get("vmid"), node_id=node_id, kind="resource",
                             params={"h": _h, "nb": len(_manque), "liste": _liste})
        ip = c.get("ip") or get_container_ip(c.get("vmid"))
        if not ip:
            continue
        try:
            # HOT-APPLY : POST :8082/extra_sizes → le worker crée/retire les proxies concernés SANS
            # redémarrer (aucune coupure des proxies inchangés). Puis persiste dans deploy_config
            # (sans restart) pour survivre à un futur redéploiement.
            r = requests.post(f"http://{ip}:8082/extra_sizes", json={"sizes": new_extra}, timeout=2)
            if r.status_code == 200:
                p2 = dict(p); p2["extra_sizes"] = new_extra
                db_update_deploy_config(c.get("vmid"), "pyramide", p2)
                changed.append(c.get("vmid"))
                db_add_alert("alert.deploy.pyramide_tailles_maj", "info", vmid=c.get("vmid"), kind="deploy",
                             params={"h": c.get("hostname") or c.get("vmid"),
                                     "n": sum(len(v) for v in new_extra.values())})
            else:
                log.warning("reconcile pyramide %s: /extra_sizes HTTP %s", c.get("vmid"), r.status_code)
        except Exception as _e:
            log.warning("reconcile pyramide hot-apply %s: %s", c.get("vmid"), _e)
    return {"changed": changed}


def _schedule_pyramide_reconcile(vmid, delay=6.0):
    """Planifie un reconcile des tailles pyramide du nœud d'un multiview, APRÈS qu'il ait eu le
    temps de publier ses proxy_needs sur :8080 (un cycle métriques). Idempotent (no-op si rien ne
    change). Lancé en thread pour ne pas bloquer le déploiement."""
    import threading as _th
    def _run():
        try:
            c = db_get_container(vmid)
            if c and c.get("node_id") is not None:
                reconcile_pyramide_sizes(c.get("node_id"))
        except Exception as _e:
            log.warning("reconcile pyramide planifié (%s): %s", vmid, _e)
    t = _th.Timer(delay, _run); t.daemon = True; t.start()


def refresh_consumers_of_source(producer_vmid):
    """Après un (re)déploiement de la SOURCE (son flux MXL est alors RECRÉÉ — même si le format est
    identique : un nouveau flux, les abonnés restant collés à l'ancien lisent du garbage), rafraîchit
    TOUS ses consommateurs sur le nœud :
      - multiview : recalcule in_w/in_h via _shm_fmt + `/reconfigure` à chaud SI les dims changent
        (les multiviews se reconnectent sinon seuls via leur handler SIGBUS) ;
      - 2110_io (sorties TX) : re-pousse les slots TX (`push_tx_slots`) → le contrôleur re-résout le
        format du flux courant et se ré-abonne (équivalent du « délier/relier » manuel ; le contrôleur
        ne gère PAS le SIGBUS-reconnect, d'où le garbage après recréation du flux producteur) ;
      - streamer : re-déploie son script → reconnexion au flux courant.
    NO-OP sans consommateur. Appelé après tout (re)déploiement orchestré de la source."""
    import json as _json
    from .monitor import _shm_fmt
    from . import docker_driver
    prod = db_get_container(producer_vmid)
    if not prod:
        return
    node = prod.get("node_id")
    try:
        pdc = _json.loads(prod.get("deploy_config") or "{}") or {}
    except Exception:
        return
    # shm(s) produits par la source
    prod_shms = set()
    try:
        w = plugins.derive_wiring(pdc.get("type"), prod.get("hostname") or "", pdc.get("params") or {})
        for p in (w.get("produces") or []):
            if p.get("shm"):
                prod_shms.add(p["shm"])
    except Exception:
        pass
    if not prod_shms:
        return
    refreshed = False
    for c in db_get_containers():
        if c.get("node_id") != node or c.get("vmid") == producer_vmid:
            continue
        try:
            cdc = _json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        ctype = cdc.get("type")
        cp = cdc.get("params") or {}
        if ctype == "multiview":
            fc = list(cp.get("flux_config") or [])
            changed = False
            for i, e in enumerate(fc):
                src = (e.get("path") or "").removeprefix("/dev/shm/")
                if src not in prod_shms:
                    continue
                fmt = _shm_fmt(src)
                if not fmt or not fmt.get("w") or not fmt.get("h"):
                    continue
                nw, nh = int(fmt["w"]), int(fmt["h"])
                if int(e.get("in_w") or 0) != nw or int(e.get("in_h") or 0) != nh:
                    e = dict(e); e["in_w"] = nw; e["in_h"] = nh
                    if fmt.get("fps") is not None: e["in_fps"] = fmt.get("fps") or ""
                    if fmt.get("scan"): e["in_scan"] = fmt.get("scan")
                    if fmt.get("colorimetry") is not None: e["in_colorimetry"] = fmt.get("colorimetry") or ""
                    fc[i] = e; changed = True
            if not changed:
                continue
            cp = dict(cp); cp["flux_config"] = fc
            db_update_deploy_config(c.get("vmid"), "multiview", cp)
            ip = c.get("ip") or get_container_ip(c.get("vmid"))
            if ip:
                try:
                    requests.post(f"http://{ip}:8082/reconfigure", json={"flux_config": fc}, timeout=2)
                except Exception as _e:
                    log.warning("refresh consumer multiview %s /reconfigure: %s", c.get("vmid"), _e)
            db_add_alert("alert.deploy.multiview_format_refresh", "info", vmid=c.get("vmid"), kind="deploy",
                         params={"h": c.get("hostname") or c.get("vmid"),
                                 "prod_h": prod.get("hostname") or producer_vmid})
            refreshed = True
        elif ctype == "2110_io":
            # Sortie TX consommant un shm de la source ? (state_field tx{i}_shm / tx_audio{i}_shm /
            # tx_anc{i}_shm). Si oui → re-pousse les slots : le contrôleur re-résout le format du flux
            # courant et se ré-abonne (corrige le « garbage » après recréation du flux producteur).
            consumes = any(v in prod_shms for k, v in cp.items()
                           if isinstance(v, str) and k.startswith("tx") and k.endswith("_shm"))
            if not consumes:
                continue
            try:
                docker_driver.push_tx_slots(c.get("vmid"), cp)
                db_add_alert("alert.deploy.io2110_tx_repousse", "info", vmid=c.get("vmid"), kind="deploy",
                             params={"h": c.get("hostname") or c.get("vmid"),
                                     "prod_h": prod.get("hostname") or producer_vmid})
                refreshed = True
            except Exception as _e:
                log.warning("refresh consumer 2110_io %s push_tx_slots: %s", c.get("vmid"), _e)
        elif ctype == "streamer":
            # Streamer lisant ce flux (shm_name vidéo ou audio_shm) → re-déploiement = reconnexion.
            if cp.get("shm_name") in prod_shms or cp.get("audio_shm") in prod_shms:
                try:
                    deployer_script(c.get("vmid"), "streamer", cp)
                    db_add_alert("alert.deploy.streamer_reconnecte", "info", vmid=c.get("vmid"), kind="deploy",
                                 params={"h": c.get("hostname") or c.get("vmid"),
                                         "prod_h": prod.get("hostname") or producer_vmid})
                    refreshed = True
                except Exception as _e:
                    log.warning("refresh consumer streamer %s redeploy: %s", c.get("vmid"), _e)
    if refreshed and node is not None:
        try:
            reconcile_pyramide_sizes(node)
        except Exception as _e:
            log.warning("refresh consumers: reconcile pyramide nœud %s: %s", node, _e)


def _schedule_consumer_refresh(producer_vmid, delay=4.0):
    """Diffère refresh_consumers_of_source après un (re)déploiement (le temps que le shm de la
    source soit recréé à sa nouvelle taille). En thread, idempotent."""
    import threading as _th
    def _run():
        try:
            refresh_consumers_of_source(producer_vmid)
        except Exception as _e:
            log.warning("refresh consumers planifié (%s): %s", producer_vmid, _e)
    t = _th.Timer(delay, _run); t.daemon = True; t.start()


def version_en_cours(ip, vmid=None, timeout=2.0):
    """Version du plugin RÉELLEMENT en cours d'exécution, lue sur `:8080` — ou None si le
    conteneur ne la publie pas (script antérieur à multiview 0.68.0) ou ne répond pas.

    `deploy_config.plugin_version` n'est qu'une écriture en base : RIEN ne garantissait qu'elle
    décrive le script qui tourne. Un mur a passé une nuit entière estampillé 0.67.0 sans porter
    le correctif de cette version — et la garde de hot-apply, se fiant à l'estampille, le croyait
    à jour et ne le redéployait donc jamais. Il a fallu un `docker exec … grep` pour le voir.
    Ici on demande au conteneur, comme `_asm_en_place` demande au mur son câblage plutôt que de
    consulter sa propre comptabilité."""
    if not ip:
        return None
    try:
        j = requests.get("http://%s:8080/" % ip, timeout=timeout).json()
    except Exception:                                                      # noqa: BLE001
        return None
    v = j.get("plugin_version")
    return str(v) if v else None


def deployer_script(vmid, type_script, params, script_path="/opt/script/main.py", version=None,
                    script_content=None):
    """Point d'entrée déploiement — SÉRIALISÉ PAR VMID (vmlocks) : deux opérations de
    cycle de vie concurrentes sur le même vmid (deploy/destroy/restart) raceraient sinon sur
    l'agent-nœud / l'API Docker. RLock → réentrant dans le même thread (compute → chemin agent,
    ou route qui tient déjà verrou_vmid)."""
    from .vmlocks import verrou_vmid
    from .database import db_set_desired_state
    with verrou_vmid(vmid, op="deploy"):
        db_set_desired_state(vmid, "running")   # un déploiement = intention « ça doit tourner »
        # Un déploiement ne crée PLUS d'emplacement. Un emplacement est une position de
        # production (« MULTIVIEW RÉGIE 1 »), donc une décision humaine ; le dériver
        # automatiquement d'un conteneur confondait le 3e barreau d'identité avec le 2e, et
        # produisait un libellé — le hostname — qui est justement ce qu'un emplacement ne doit
        # pas être. Mesuré le 2026-08-30 : 282 emplacements semés, 8 servis, et 2 des survivants
        # jamais renommés. Ils se créent maintenant depuis Réglages → Ember+ → Emplacements.
        return _deployer_script_locked(vmid, type_script, params, script_path=script_path,
                                       version=version, script_content=script_content)


def _deployer_script_locked(vmid, type_script, params, script_path="/opt/script/main.py",
                            version=None, script_content=None):
    # `h` = hostname : sans lui, le suivi de création par lot ne peut pas rattacher cette alerte
    # à sa ligne (il apparie par `msg_params.h`, cf. `_lastAlertFor` dans static/scripts.js) et
    # l'étape « Configuration » resterait inatteignable. La lecture est déjà faite quelques lignes
    # plus bas ; on l'avance, plutôt que d'ajouter un aller-retour en base.
    _c0 = db_get_container(vmid)
    db_add_alert("alert.deploy.script.en_cours", "info", vmid=vmid, kind="deploy",
                 params={"t": type_script, "vmid": vmid, "h": (_c0 or {}).get("hostname")})

    # Backend Docker : deux chemins. MTL = controller bâti dans l'image, config par NMOS, pas
    # d'agent :8081 ni de rendu script.py → chemin dédié. Compute générique = conteneur agent
    # macvlan : on le garantit up ici puis on POURSUIT sur le chemin agent standard (comme un LXC).
    _cd = _c0
    if _cd:
        from . import docker_compute
        if docker_compute.is_mtl_type(type_script):
            from . import docker_driver
            return docker_driver.deploy_docker(vmid, params)
        if not docker_compute.deploy_compute(vmid, params, deploy_type=type_script):
            return False
        # … pas de return : on tombe dans le rendu + POST :8081/deploy ci-dessous.

    ip = get_container_ip(vmid)
    if not ip:
        db_add_alert("alert.net.ip_introuvable", "error", vmid=vmid, kind="net", params={"vmid": vmid})
        return False

    # Notify NMOS dès le début du deploy pour que le registre reflète rapidement
    # tout changement de type ou de topologie. Le rebuild_model() post-deploy
    # (après db_update_deploy_config) reste le notify authoritative.
    if plugins.is_plugin(type_script):
        try:
            from services import nmos as _nmos
            _nmos.notify_state_change()
        except Exception:
            pass

    # Producteurs nommés par hostname (mixer, correcteur) : on injecte le hostname
    # réel du container dans params pour que scripts.py et la dénormalisation
    # shm_out plus bas tombent sur le même nom que la topologie home (routes.py),
    # qui dérive le shm producteur depuis c.hostname. Sans ça, le câble pointe
    # sur melangeur_pgm mais le script écrit sur mxl_pgm → écran noir.
    if plugins.is_plugin(type_script):
        c = db_get_container(vmid) or {}
        params = dict(params)
        params.setdefault("hostname", c.get("hostname") or f"mxl{vmid}")

    # (Le pool SR-IOV nic_pool et l'allocation VF par-conteneur ont été retirés : le moteur
    # 2110_io tourne sur la PF en AF-XDP, --network host, sans VF.)

    # Hook before_deploy : normalisation déclarée par le plugin (hooks.py).
    # Reçoit params + contexte incluant tous les settings orchestrateur (lecture seule).
    # Un hook qui lève une exception est ignoré ; le deploy continue.
    _hook = plugins.get_hook(type_script, "before_deploy")
    if _hook:
        try:
            from . import settings as _st
            _result = _hook(dict(params), {"vmid": vmid, "type": type_script,
                                           "hostname": params.get("hostname", ""),
                                           "settings": _st.all()})
            if _result is not None:
                params = _result
        except Exception as _e:
            log.warning("hook before_deploy %s: %s", type_script, _e)

    # Plugins qui déclarent `brand` : on injecte l'identité (perso) dans leurs params.
    if plugins.is_plugin(type_script) and plugins.wants_brand(type_script):
        params = _resolve_brand_settings(params)

    # (Bibliothèque de polices : voir `_gras()` — les params PERSISTÉS restent MAIGRES, seuls
    # les params ENVOYÉS au conteneur portent le base64.)

    # Injecte les ring sizes depuis les settings (lus ici pour être dans le CONFIG du script).
    if plugins.is_plugin(type_script):
        from . import settings as _st_ring
        params = dict(params)
        # Ring vidéo : suit le réglage (borné [2:8] par le formulaire, MTL st20 ≤8). Le réglage
        # est l'unique source de vérité ; pas de garde-fou ici (fallback aligné sur le défaut 8).
        params.setdefault("shm_video_ring", int(_st_ring.get("shm_video_ring") or 8))
        params.setdefault("shm_audio_ring", int(_st_ring.get("shm_audio_ring") or 100))
        # Profondeur de bits du pipeline shm : arbitrage orchestrateur. Le script ne connaît
        # pas le mode (force8/follow), il ne reçoit qu'un `bit_depth` ∈ {8,10,12} dans CONFIG.
        _apply_pipeline_bit_depth(params, _st_ring.get("mxl_pipeline_bit_depth") or "force8")

    # Multiview : résoudre in_w/in_h (+ in_fps/in_scan, format déclaré du producteur,
    # pour le chip « format source ») de chaque fenêtre depuis la DB (même logique que
    # _apply_wire). Corrige les valeurs laissées au défaut 640×360 quand le déploiement
    # vient de l'éditeur plutôt que de l'outil câble.
    if type_script == "multiview":
        from .monitor import _shm_fmt
        _self_c = db_get_container(vmid) or {}
        _self_node = _self_c.get("node_id")
        _fc = list(params.get("flux_config") or [])
        _fc_changed = False
        for _i, _entry in enumerate(_fc):
            _p = ((_entry.get("path") or "")).removeprefix("/dev/shm/")
            if not _p:
                continue
            _fmt = _shm_fmt(_p)
            _new = dict(_entry)
            if _fmt:
                _vals = {"in_w": _fmt["w"], "in_h": _fmt["h"],
                         "in_fps": _fmt.get("fps") or "", "in_scan": _fmt.get("scan") or "p",
                         "in_colorimetry": _fmt.get("colorimetry") or ""}
                _new.update(_vals)
            # Pyramide (Phase 2, OPPORTUNISTE) : si une pyramide CO-LOCALISÉE (même nœud =
            # même /dev/shm) descale cette source, injecter ses proxies {path,w,h}. Le script
            # multiview choisit à l'exécution le proxy le mieux dimensionné par tuile (sinon
            # plein). Absent → on retire un éventuel `proxies` périmé (retour au classique).
            _px = _proxies_for_source(_p, _self_node)
            if _px:
                _new["proxies"] = _px
            else:
                _new.pop("proxies", None)
            if _new != _entry:
                _fc[_i] = _new
                _fc_changed = True
        if _fc_changed:
            params = dict(params)
            params["flux_config"] = _fc

    # Multiview : hot-apply si seuls des params style/géo/source ont changé.
    # Compare avec les params actuellement en DB ; si aucun param structurel n'a
    # bougé et que le script répond, on évite le redéploiement (pas de coupure sortie).
    # GARDE « script réellement en marche » : le rootfs des conteneurs est ÉPHÉMÈRE — après un
    # docker restart, /opt/script est vide (agent /status → running:false, path:null). Sans cette
    # garde, la branche MUR SHARDÉ ci-dessous retournait True sans jamais redéployer de script
    # (le tissu ne fait que du hot-apply :8082) → mur définitivement stoppé (vu sur le 163).
    if (type_script == "multiview" and ip and script_content is None
            and _agent_script_running(ip, vmid)):
        import json as _json_mv
        _old_cd = db_get_container(vmid)
        if _old_cd:
            _old_p = (_json_mv.loads(_old_cd.get("deploy_config") or "{}") or {}).get("params") or {}
            # Hot-apply seulement si le script déployé est À LA version du manifeste :
            # un vieux script ignorerait les nouveaux champs (ex. hidden → PiP fantômes).
            _cur_ver = (plugins.get(type_script) or {}).get("version")
            # ÉTAT OBSERVÉ d'abord : ce que le conteneur EXÉCUTE prime sur ce que la base
            # prétend. Un script antérieur à 0.68.0 ne publie pas sa version → None → on
            # retombe sur l'estampille (comportement historique, aucune régression), et le
            # premier redéploiement rend le mur vérifiable pour toujours.
            _ver_vue = version_en_cours(ip, vmid)
            _ver_effective = _ver_vue if _ver_vue is not None else _old_p.get("plugin_version")
            if _ver_vue is not None and _ver_vue != _old_p.get("plugin_version"):
                log.warning("deploy %s: le conteneur EXÉCUTE %s alors que la base annonce %s — "
                            "c'est le conteneur qui fait foi", vmid, _ver_vue,
                            _old_p.get("plugin_version"))
                db_add_alert("alert.deploy.version_desaccord", "warning", vmid=vmid, kind="deploy",
                             params={"vmid": vmid, "ver_vue": _ver_vue,
                                     "ver_base": _old_p.get("plugin_version")})
            if (_ver_effective == _cur_ver
                    and _multiview_only_hot_changed(_old_p, params)):
                from .database import db_fabric_get as _dfg
                if _dfg(f"asm:{vmid}"):
                    # Mur SHARDÉ : le conteneur logique est l'ASSEMBLEUR (copie pure des shards
                    # pré-rendus). Ne JAMAIS lui hot-appliquer les params logiques : le chrome se
                    # dessinerait autour de chaque BLOC de shard, et le flux_config logique
                    # écraserait le tuilage. Canal correct = persister puis RE-PLANIFIER le tissu
                    # (les signatures de cellule incluent le style → shards re-matérialisés,
                    # assembleur reconfiguré). Pousser /style aux shards directement est aussi
                    # proscrit : un shard PARTAGÉ entre deux murs serait corrompu pour l'autre.
                    params = {**params, "plugin_version": _cur_ver}
                    db_update_deploy_config(vmid, type_script, params)
                    _fabric_refresh_wall(vmid)
                    db_add_alert("alert.deploy.multiview_shard_replanifie", "info", vmid=vmid, kind="deploy",
                                 params={"vmid": vmid})
                    return True
                if _multiview_hot_apply(ip, _gras(params)):   # gras à l'ENVOI, maigre en base
                    # Le script n'a PAS changé (hot-apply) → il tourne toujours en _cur_ver.
                    # On reporte plugin_version (l'éditeur ne l'envoie pas) sinon le PROCHAIN
                    # hot-apply échouerait la garde de version et forcerait un redéploiement.
                    params = {**params, "plugin_version": _cur_ver}
                    db_update_deploy_config(vmid, type_script, params)
                    db_add_alert("alert.deploy.multiview_hot_apply", "info", vmid=vmid, kind="deploy",
                                 params={"vmid": vmid})
                    return True

    if script_content is None:
        # Rendu du script = params GRAS (polices en base64) ; `params` reste MAIGRE pour la
        # persistance et les comparaisons (cf. _gras).
        script_content = generer_script(type_script, _gras(params), version)
    if not script_content:
        db_add_alert("alert.deploy.type_inconnu", "error", vmid=vmid, kind="deploy",
                     params={"t": type_script})
        return False

    # Attendre que le contrôleur :8081 écoute (LXC qui démarre / conteneur Docker dont le HTTPServer
    # n'est pas encore up) — évite l'alerte « Connection refused » lors d'une (re)création de container.
    if not _attendre_agent_pret(ip, vmid=vmid):
        log.warning("deploy %s: agent :8081 (%s) ne répond pas après attente — tentative quand même", vmid, ip)
        # Filet mTLS : muet en HTTPS ≠ conteneur mort. Une sonde en clair tranche (et alerte si
        # c'est un désaccord de schéma) — sans quoi on repart sur un POST /deploy qui échouera
        # avec un message de timeout n'expliquant rien.
        diagnostiquer_schema_agent(ip, vmid=vmid)

    try:
        # bobimxl.py POUSSÉ AVEC LE SCRIPT (modèle « script poussé » étendu au binding) : déposé
        # à côté de main.py, il SHADOWE le module baké dans l'image runtime (sys.path[0] =
        # /opt/script) → les évolutions PUR-PYTHON du binding (codec ANC RFC 8331, data flows…)
        # atteignent la flotte au redéploiement, SANS rebuild d'image. La compatibilité avec la
        # libmxl.so de l'image reste requise (le binding ctypes vérifie ses symboles) — validé
        # sur les images actuelles (round-trip ANC dans bobi-compute). Best-effort : un échec
        # n'empêche pas le déploiement (le script retombe sur le bobimxl de l'image).
        try:
            _bmxl_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      "script_templates", "bobimxl.py")
            with open(_bmxl_path, encoding="utf-8") as _bf:
                _bmxl = _bf.read()
            agent_session().post(
                agent_url(ip, "/deploy"),
                json={"path": os.path.join(os.path.dirname(script_path), "bobimxl.py"),
                      "content": _bmxl},
                headers=agent_headers(vmid),
                timeout=10)
        except Exception as _e:
            log.warning("deploy %s: push bobimxl.py raté (%s) — binding de l'image conservé",
                        vmid, _e)
        r = agent_session().post(
            agent_url(ip, "/deploy"),
            json={"path": script_path, "content": script_content},
            headers=agent_headers(vmid),
            timeout=10
        )
        if r.status_code == 200:
            db_update_script(vmid, script_path)

            # source/shm : hook source_shm du plugin, ou fallback wiring déclaratif
            _ss_hook = plugins.get_hook(type_script, "source_shm")
            if _ss_hook:
                try:
                    # Import local : `_st` n'est lié plus haut que si le plugin a un hook
                    # before_deploy — un plugin qui n'a QUE source_shm levait UnboundLocalError.
                    from . import settings as _st
                    _ss = _ss_hook(dict(params), {"vmid": vmid, "type": type_script,
                                                   "hostname": params.get("hostname", ""),
                                                   "settings": {k: _st.get(k) for k in _st.DEFAULTS}})
                    source = _ss.get("source", "—")
                    shm    = _ss.get("shm", "—")
                except Exception as _e:
                    log.warning("hook source_shm %s: %s", type_script, _e)
                    source, shm = "—", "—"
            elif plugins.is_plugin(type_script):
                hn = params.get("hostname", "mxl")
                w = plugins.derive_wiring(type_script, hn, params)
                prod = " · ".join(p.get("shm", "") for p in w["produces"] if p.get("shm"))
                cons = " · ".join(
                    (c.get("shm") or (params.get(c["state_field"]) if c.get("state_field") else "") or "")
                    for c in w["consumes"]).strip(" ·")
                source = (params.get("file") or cons or "—")
                shm    = (prod or cons or "—")
            else:
                source, shm = "—", "—"

            # Versioning : enregistre la version du plugin dans deploy_config (JSON, pas
            # de migration SQL). Permet "version par container déployé" + détection drift.
            if plugins.is_plugin(type_script):
                # Version réellement rendue (la demandée si archivée connue, sinon courante).
                params["plugin_version"] = plugins.resolved_version(type_script, version)

            db_update_source(vmid, source, shm)
            db_update_deploy_config(vmid, type_script, params)
            # Re-notifier NMOS APRÈS l'écriture du deploy_config : rebuild_model
            # détecte les senders/receivers depuis deploy_config.type, donc le
            # notify précoce (haut de fonction) tombe sur l'ancienne config et
            # n'expose pas le flux fraîchement déployé.
            try:
                from services import nmos
                nmos.notify_state_change()
            except Exception:
                pass
            try:
                from services import emberplus
                emberplus.notify_change()
            except Exception:
                pass

            # « Plan 2 » : pousser au CONTENEUR la description de ses propres ressources NMOS, pour
            # qu'il serve son Node API sur son :8081 (cf. services/nmos/conteneur_node.py). Ici et
            # pas plus haut : `document()` lit `deploy_config`, qui vient seulement d'être écrit —
            # pousser avant enverrait la description de la config PRÉCÉDENTE.
            # Best-effort et fermé par défaut : un agent d'ancienne image ne connaît pas /nmos, et
            # un déploiement ne doit jamais échouer pour cette raison.
            try:
                from services.nmos import conteneur_node as _cn
                _cn.pousser(vmid)
            except Exception as _e:
                log.info("plan 2 : push NMOS vers %s ignoré (%s)", vmid, _e)

            # Pyramide : un multiview (re)déployé peut changer ses besoins de tailles → reconcile
            # différé (le temps qu'il publie proxy_needs sur :8080), hot-apply, no-op si inchangé.
            if type_script == "multiview":
                _schedule_pyramide_reconcile(vmid)
                # Tissu : sharder un mur lourd SANS attendre le tick de 30 s (sinon il sature le bus
                # mémoire en monolithe et pénalise les sorties live). No-op si fabric_auto off / mur
                # interne / pas saturé. Sérialisé par le verrou par-nœud (anti-race avec le tick).
                _schedule_fabric_reconcile(vmid)

            # Changement de format ORCHESTRÉ : si cette (re)déploiement modifie le format d'une
            # source, rafraîchir à chaud les multiviews qui la consomment (in_w/in_h + /reconfigure).
            # Différé (shm recréé à sa nouvelle taille) ; no-op si pas de consommateur / dims inchangées.
            _schedule_consumer_refresh(vmid)

            # Relancer le script pour qu'il prenne le nouveau code
            try:
                agent_session().post(agent_url(ip, "/stop"), timeout=5, headers=agent_headers(vmid))
            except Exception:
                pass
            # ★ SAUF SI L'EXPLOITATION L'A DÉSACTIVÉ. Le script est déployé (le code est à jour et
            # prêt), mais il n'est PAS démarré : `containers.script_enabled` porte l'intention,
            # posée par le `NcWorker.enabled` du modèle MS-05-02. Sans ce garde, un contrôleur NMOS
            # tiers verrait son arrêt accepté puis défait au premier redéploiement — le pilotage
            # aurait l'air de marcher et ne marcherait pas.
            from .database import db_script_enabled
            _voulu = db_script_enabled(vmid)
            if not _voulu:
                # Déployé et prêt, volontairement PAS démarré. On le DIT : un script à l'arrêt sans
                # trace se lit comme une panne, et quelqu'un ira le relancer à la main.
                db_add_alert("alert.deploy.script_deploie_non_demarre", "info", vmid=vmid,
                             kind="deploy", params={"t": type_script, "vmid": vmid})
            try:
                r2 = agent_session().post(agent_url(ip, "/start"), timeout=5,
                                          headers=agent_headers(vmid)) if _voulu else None
                if r2 is None:
                    pass
                elif r2.status_code == 200:
                    db_add_alert(
                        "alert.deploy.script_deploie_redemarre",
                        "info", vmid=vmid, kind="deploy", params={"t": type_script, "vmid": vmid})
                else:
                    db_add_alert(
                        "alert.deploy.script_start_code",
                        "warning", vmid=vmid, kind="deploy",
                        params={"t": type_script, "vmid": vmid, "code": r2.status_code})
            except Exception as e:
                db_add_alert(
                    "alert.deploy.script_start_echoue",
                    "warning", vmid=vmid, kind="deploy",
                    params={"t": type_script, "vmid": vmid, "e": str(e)})
            return True
    except Exception as e:
        db_add_alert("alert.deploy.erreur", "error", vmid=vmid, kind="deploy",
                     params={"vmid": vmid, "e": str(e)})
    return False


def rewire_on_restart(vmid):
    """Re-pousse à chaud (POST :8082/input) tous les câbles d'un plugin hot-wire après
    son redémarrage, sans re-déployer le script. Lit deploy_config + résout les state_fields
    des consumes pour reconstituer les wires persistés en DB."""
    import re as _re
    import json as _json
    try:
        c = db_get_container(vmid)
        if not c:
            return
        cfg = _json.loads(c.get("deploy_config") or "{}")
        t = cfg.get("type") or cfg.get("plugin_type") or ""
        params = cfg.get("params") or {}
        if not t or not plugins.is_plugin(t):
            return
        # Moteur MTL (2110_io) : son état vit sur :8081 (slots TX + abonnements RX IS-05), PAS sur
        # le hot-wire :8082 générique. On rejoue le MÊME resync que deploy_docker, sinon un restart
        # du conteneur laisse senders=0 → SDP TX en 404 (sorties NMOS non abonnables) et les RX ne
        # sont rétablis que par un contrôleur NMOS externe. push_tx_slots/repush gèrent le retry
        # le temps que :8081 réponde (conteneur qui vient de démarrer).
        from . import docker_compute
        if docker_compute.is_mtl_type(t):
            # MÊME séquence VÉRIFIÉE que le (re)déploiement (docker_driver.resync_moteur) :
            # readiness bornée → slots TX → abonnements RX → vérification + ALERTE si le moteur
            # reste vide. L'ancien code enchaînait les deux pushes sans readiness commune ni
            # vérification → un moteur relancé par la surveillance revenait muet EN SILENCE.
            from . import docker_driver
            docker_driver.resync_moteur(vmid, params)
            log.info("rewire_on_restart: resync MTL (slots TX + abonnements RX) sur vmid %d", vmid)
            return
        w = plugins.derive_wiring(t, params.get("hostname") or "", params)
        if w.get("mode") != "hot-wire":
            return
        ip = get_container_ip(vmid)
        if not ip:
            return
        rewired = 0
        for spec in (w.get("consumes") or []):
            sf = spec.get("state_field")
            if not sf:
                continue
            essence = spec.get("essence") or "video"
            # state_field peut contenir {i1} (ex. "input_{i1}") — résout sur les slots 0..n, la
            # CLÉ étant 1-based (cf. app/numerotation.py). `i` reste l'indice de slot ; seul le
            # numéro écrit dans la clé est décalé — d'où `numero(i)` et non `i`.
            if "{i1}" in sf:
                i = 0
                while True:
                    key = sf.replace("{i1}", str(numero(i)))
                    shm = params.get(key) or ""
                    if not shm:
                        break
                    slot = spec.get("slot", i)
                    fmt = params.get(key + "_fmt") or params.get("input_format")
                    try:
                        body = {"essence": essence, "shm": shm}
                        if slot is not None:
                            body["idx"] = slot
                        if fmt and essence == "video":
                            body["format"] = fmt
                        requests.post(f"http://{ip}:8082/input", json=body, timeout=2)
                        rewired += 1
                    except Exception:
                        pass
                    i += 1
            else:
                shm = params.get(sf) or ""
                if not shm:
                    continue
                slot = spec.get("slot")
                fmt = params.get(sf + "_fmt") or params.get("input_format")
                try:
                    body = {"essence": essence, "shm": shm}
                    if slot is not None:
                        body["idx"] = slot
                    if fmt and essence == "video":
                        body["format"] = fmt
                    requests.post(f"http://{ip}:8082/input", json=body, timeout=2)
                    rewired += 1
                except Exception:
                    pass
        if rewired:
            log.info("rewire_on_restart: %d wire(s) re-poussé(s) sur vmid %d", rewired, vmid)
    except Exception as e:
        log.warning("rewire_on_restart %d: %s", vmid, e)


# Verrou par-nœud des reconciles de tissu : le tick périodique (main.py) ET le hook au (re)deploy
# (_schedule_fabric_reconcile) peuvent viser le même nœud en même temps → sans sérialisation ils
# racent sur l'API (double matérialisation de shards). Non-bloquant : un reconcile en cours suffit.
_fabric_node_locks = {}
_fabric_node_locks_guard = threading.Lock()


def _fabric_lock(node_id):
    with _fabric_node_locks_guard:
        lk = _fabric_node_locks.get(node_id)
        if lk is None:
            lk = threading.Lock()
            _fabric_node_locks[node_id] = lk
        return lk


# ─── ANNULATION D'UNE RÉPLANIFICATION DEVENUE OBSOLÈTE ──────────────────────────────────────
# Composer un layout, c'est déplacer trente choses en deux minutes. Chaque édition lançait une
# réplanification qui allait AU BOUT — planifier, créer un conteneur, attendre qu'il produise,
# basculer, détruire le précédent — pour être remplacée quatre secondes plus tard par la
# suivante. Mesuré le 2026-08-07 : six conteneurs créés en trente secondes, chacun obsolète
# avant même d'avoir servi. Aucun n'était inutile quand il a été décidé ; ils l'étaient devenus
# PENDANT leur fabrication.
# Chaque édition incrémente donc la génération du nœud, et une réplanification renonce dès
# qu'elle se sait dépassée — aux points où renoncer ne casse rien. On ne fait jamais patienter
# l'opérateur : c'est le travail périmé qu'on supprime, pas ses gestes.
_fabric_gen = {}
_fabric_gen_guard = threading.Lock()


def _fabric_bump(node_id):
    """Nouvelle intention sur ce nœud → tout ce qui est en vol devient périmé."""
    with _fabric_gen_guard:
        g = _fabric_gen.get(node_id, 0) + 1
        _fabric_gen[node_id] = g
        return g


def _fabric_gen_courante(node_id):
    with _fabric_gen_guard:
        return _fabric_gen.get(node_id, 0)


def _fabric_refresh_wall(vmid):
    """Re-planifie le tissu du nœud d'un mur DÉJÀ SHARDÉ après un changement de ses params
    logiques (style/fenêtres poussés à chaud). BYPASSE la gate `fabric_auto` : maintenir un mur
    déjà shardé n'est pas de l'auto-sharding — sans ça, gate off = pushes silencieusement perdus.
    Verrou par-nœud BLOQUANT (contrairement au tick, qui skippe) : notre modif vient d'être
    persistée, le reconcile DOIT repasser derrière un éventuel run en vol pour la voir."""
    _c0 = db_get_container(vmid)
    _nid = (_c0 or {}).get("node_id")
    if _nid is None:
        return
    _gen = _fabric_bump(_nid)     # AVANT le fil : les réplanifications en vol sont déjà périmées

    def _run():
        try:
            with _fabric_lock(_nid):
                # Une édition PLUS RÉCENTE est arrivée pendant qu'on attendait le verrou : elle
                # planifiera à partir d'un état plus juste que le nôtre. On se retire.
                if _fabric_gen_courante(_nid) != _gen:
                    return
                _reconcile_fabric_node_impl(
                    _nid, perime=lambda: _fabric_gen_courante(_nid) != _gen)
        except Exception as _e:
            log.warning("fabric refresh (%s): %s", vmid, _e)
    threading.Thread(target=_run, daemon=True).start()


def _schedule_fabric_reconcile(vmid, delays=(8.0, 20.0)):
    """Planifie un reconcile du tissu sur le nœud d'un multiview APRÈS (re)déploiement, sans
    attendre le tick périodique de 30 s : un mur lourd tourne sinon en MONOLITHE plein écran
    (gros consommateur de bande passante mémoire → pénalise les sorties live) jusqu'au prochain
    tick. Plusieurs tentatives décalées couvrent le temps de boot du conteneur (le monolithe doit
    avoir publié son own_latency sur :8080 pour être jugé saturé). Skippe les nœuds INTERNES du
    tissu (hostnames bobi-fab-*), qui ne doivent jamais re-déclencher un reconcile. Idempotent,
    sérialisé par le verrou par-nœud de reconcile_fabric_node."""
    def _run():
        try:
            c = db_get_container(vmid)
            if not c or c.get("node_id") is None:
                return
            if str(c.get("hostname") or "").startswith("bobi-fab-"):
                return   # nœud interne du tissu (shard/assembleur) → pas de re-déclenchement
            reconcile_fabric_node(c.get("node_id"))
        except Exception as _e:
            log.warning("reconcile fabric planifié (%s): %s", vmid, _e)
    for d in delays:
        t = threading.Timer(d, _run); t.daemon = True; t.start()


# Un mur déployé depuis moins que ça n'est pas jugé par le tissu : sa latence est celle de la
# rafale de re-bake, pas de son régime. Deux minutes couvrent le re-bake d'habillage plein cadre
# et la recomposition des frises, mesurés sur un mur de 4 cellules richement habillé.
_DELAI_JUGEMENT_S = 120.0

# ─── Confirmer avant de sharder, et savoir en sortir ─────────────────────────────────────────
# ★ POURQUOI CES DEUX CONSTANTES EXISTENT (2026-08-08, mur 906 sur dell-1).
#
# Un délai fixe ne suffit pas. `_DELAI_JUGEMENT_S` protège du re-bake, mais il DEVINE une durée :
# le mur 906, plus lourdement habillé que celui sur lequel les 120 s ont été calibrées, était
# encore à 17,7 ms bien après la fenêtre. Le tissu a jugé sur cette valeur transitoire et l'a
# découpé en 2 shards — 9 cœurs et 3 processus au lieu de 3 — alors qu'en régime il tenait le
# budget. On n'allonge pas le délai (on devinerait encore) : on exige que la saturation soit
# CONSTATÉE PLUSIEURS FOIS DE SUITE. À un tick de 30 s, 4 passages = 2 min de saturation réelle.
_CONFIRMATIONS_SATURATION = 4

# Et il faut une porte de SORTIE. Le critère de sortie du sharding était le nombre de tuiles
# câblées, jamais le gain : un mur shardé par accident ne redevenait donc jamais monolithe. On
# estime le coût qu'aurait le monolithe par la SOMME des `own` de ses shards — un MAJORANT (chaque
# shard paie déjà son propre gather et sa propre sortie, que le monolithe ne paierait qu'une fois),
# donc conclure « ça tient » sur cette base est conservateur. La marge crée l'HYSTÉRÉSIS qui évite
# le flap : on shardE au-dessus du budget, on ne restaure qu'en dessous de 80 % de ce budget.
_MARGE_RESTAURATION = 0.80

_satur_streak = {}      # vmid → nb de passages CONSÉCUTIFS au-dessus du budget
_restau_streak = {}     # vmid → nb de passages CONSÉCUTIFS où le monolithe tiendrait


def _restaurables_tissu(node_id, budget_by_vmid, budget_ms):
    """Murs SHARDÉS dont le découpage ne rapporte plus, confirmés sur plusieurs passages.

    Renvoie un set de vmids à rendre au monolithe. Prudent par construction : un shard dont la
    latence est illisible interdit toute conclusion (on ne restaure pas sur une mesure partielle),
    et la décision doit tenir `_CONFIRMATIONS_SATURATION` passages d'affilée."""
    import json as _json
    from . import settings as _st2
    from .database import db_fabric_all as _dfa
    # ── ⛔ DÉSACTIVÉE PAR DÉFAUT — et ce n'est PAS de la prudence de principe ────────────────
    # La sortie économique est juste, mais elle est INEXPLOITABLE tant qu'un défaut amont n'est
    # pas corrigé : **restaurer un monolithe ne lui rend pas son GPU**. Mesuré le 2026-08-08 sur
    # le mur 906 — restauré, il est revenu en `gpu=False / mvk=True` (CPU), à 31,8 ms de `own`
    # pour un budget de 20, donc réellement saturé ; le tissu l'a re-shardé, à raison. On obtenait
    # une OSCILLATION : restauration → deux minutes de mur à 24-28 fps sous les yeux du
    # réalisateur → re-sharding. Pire que le défaut qu'on voulait corriger.
    # Réactiver seulement quand `restore_fn` réacquiert le GPU libéré au passage en assembleur
    # (le mur redevient alors ce qu'il était, pas une version CPU de lui-même).
    if str(_st2.get("fabric_restore_auto") or "off").lower() not in ("on", "1", "true"):
        return set()
    par_mur = {}
    for r in _dfa(node_id):
        if r.get("kind") != "shard":
            continue
        ref = r.get("ref")
        try:
            parents = _json.loads(r.get("parents") or "[]")
        except Exception:
            parents = []
        for pv in parents:
            try:
                par_mur.setdefault(int(pv), []).append(int(ref))
            except (TypeError, ValueError):
                pass
    out = set()
    for vmid, shards in par_mur.items():
        budget = budget_by_vmid.get(vmid, budget_ms)
        total = 0.0
        complet = bool(shards)
        for sv in shards:
            c = db_get_container(sv)
            ip = (c or {}).get("ip") or get_container_ip(sv)
            lat = None
            if ip:
                try:
                    lat = requests.get(f"http://{ip}:8080/", timeout=1.0).json().get("own_latency_ms")
                except Exception:
                    lat = None
            if lat is None:
                complet = False       # mesure partielle → aucune conclusion
                break
            total += float(lat)
        if not complet:
            _restau_streak[vmid] = 0
            continue
        if total <= budget * _MARGE_RESTAURATION:
            _restau_streak[vmid] = _restau_streak.get(vmid, 0) + 1
            if _restau_streak[vmid] >= _CONFIRMATIONS_SATURATION:
                out.add(vmid)
                log.info("tissu : mur %s — le monolithe tiendrait (%.1f ms cumulés pour un budget "
                         "de %.1f, confirmé %d fois) → retour au monolithe",
                         vmid, total, budget, _restau_streak[vmid])
        else:
            _restau_streak[vmid] = 0
    return out


def reconcile_fabric_node(node_id):
    """Wrapper orchestrateur de l'auto-trigger du tissu (cf. compositor_fabric.reconcile_fabric).
    GATED par `fabric_auto` (défaut off). VERROU PAR-NŒUD non bloquant : le tick périodique
    (main.py) et le hook au (re)deploy (_schedule_fabric_reconcile) peuvent se chevaucher → un
    reconcile déjà en cours sur le nœud suffit, on skippe (évite la double matérialisation)."""
    from . import settings as _st
    if str(_st.get("fabric_auto") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return {"skipped": "fabric_auto off"}
    _lk = _fabric_lock(node_id)
    if not _lk.acquire(blocking=False):
        return {"skipped": "reconcile fabric déjà en cours sur ce nœud"}
    try:
        return _reconcile_fabric_node_impl(node_id)
    finally:
        _lk.release()


def _reconcile_fabric_node_impl(node_id, perime=None):
    """Corps du reconcile (cf. reconcile_fabric_node). Pour chaque multiview LOGIQUE du nœud qui
    SATURE (own_latency :8080 > budget), matérialise ses shards (conteneurs compute) et reconfigure
    le mur en assembleur (hot) ; restaure le monolithe quand il ne sature plus. Idempotent."""
    import json as _json
    from . import settings as _st, compositor_fabric as _cf, docker_compute as _dc, containers as _ct
    try:
        budget = float(_st.get("fabric_budget_ms") or 20.0)
    except (TypeError, ValueError):
        budget = 20.0
    try:
        max_cells = max(2, int(_st.get("fabric_max_cells") or 4))
    except (TypeError, ValueError):
        max_cells = 4
    # Exclure les NŒUDS INTERNES du tissu (shards/partagés) de l'ensemble à sharder : ce sont des
    # conteneurs de type `multiview`, mais les inclure ferait sharder les shards RÉCURSIVEMENT
    # (asm:<shard> + sous-shards dont le déploiement échoue → registre incohérent, shard reconfiguré
    # en assembleur lisant des fab inexistants → tuiles noires). Seuls les murs LOGIQUES sont candidats.
    from .database import db_fabric_all as _dfa
    _internal = set()
    for _r in _dfa(node_id):
        _ref = _r.get("ref")
        if _r.get("kind") in ("shard", "shared") and _ref and str(_ref).isdigit():
            _internal.add(int(_ref))
    # multiviews du nœud + leur own_latency (lecture :8080)
    mvs, latency, budget_by_vmid = {}, {}, {}
    for c in db_get_containers():
        if c.get("node_id") != node_id:
            continue
        try:
            dc = _json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        if dc.get("type") != "multiview":
            continue
        if c.get("vmid") in _internal:
            continue   # shard/partagé du tissu → jamais re-shardé
        vmid = c.get("vmid"); p = dc.get("params") or {}
        mvs[vmid] = p
        # Intention de cadence PAR multiview (fps_target) → budget de trame propre ; sinon global.
        try:
            _ft = float(p.get("fps_target") or 0)
            if _ft > 0:
                budget_by_vmid[vmid] = 1000.0 / _ft
        except (TypeError, ValueError):
            pass
        # ── ÉTAT TRANSITOIRE : ne pas juger un mur qui vient d'être déployé ─────────────────
        # Un (re)déploiement déclenche une rafale de re-bake — habillage plein cadre, frises,
        # vignettes — qui pousse la latence bien au-delà du budget pendant une à deux minutes,
        # AVANT que le régime établi ne s'installe. Le tissu, lui, réconcilie justement après un
        # déploiement : il mesurait donc la rafale et concluait « ce mur sature ».
        # Vécu le 2026-08-08 : un mur à 11 ms en régime, momentanément à 28 pendant sa rafale,
        # a été découpé en deux shards — fabriqués à partir du script en cours de déploiement,
        # qui se trouvait être cassé. Le tissu a ainsi propagé une panne au lieu de la contenir.
        # On IGNORE donc les murs déployés depuis moins de _DELAI_JUGEMENT_S : latence None =
        # non jugé, ni shardé ni restauré. Le prochain cycle les prendra en régime établi.
        _dep = c.get("deployed_at")
        if _dep:
            try:
                from datetime import datetime as _dt
                _age = (_dt.now() - _dt.fromisoformat(_dep)).total_seconds()
                if 0 <= _age < _DELAI_JUGEMENT_S:
                    latency[vmid] = None
                    _satur_streak[vmid] = 0     # la fenêtre de rafale ne compte pour rien
                    log.info("tissu : mur %s déployé il y a %.0f s — pas jugé (rafale de re-bake)",
                             vmid, _age)
                    continue
            except (TypeError, ValueError):
                pass
        ip = c.get("ip") or get_container_ip(vmid)
        lat = None
        if ip:
            try:
                lat = requests.get(f"http://{ip}:8080/", timeout=1.0).json().get("own_latency_ms")
            except Exception:
                lat = None
        # ── SATURATION CONFIRMÉE, jamais sur un seul relevé ─────────────────────────────────
        # Le délai de rafale devine une durée ; ceci constate un RÉGIME. Un mur au-dessus du
        # budget une seule fois n'est pas saturé — il peut sortir d'un re-bake, d'un pic de
        # frise, ou d'un démarrage plus lent que la fenêtre calibrée. Tant que la saturation
        # n'est pas confirmée, on rend `None` : non jugé, ni shardé ni restauré (même contrat
        # que la fenêtre de rafale). Découper un mur coûte 6 cœurs et 2 conteneurs, ça vaut
        # bien deux minutes de patience.
        _budget_v = budget_by_vmid.get(vmid, budget)
        if lat is None:
            _satur_streak[vmid] = 0
        elif lat > _budget_v:
            _satur_streak[vmid] = _satur_streak.get(vmid, 0) + 1
            if _satur_streak[vmid] < _CONFIRMATIONS_SATURATION:
                log.info("tissu : mur %s à %.1f ms (budget %.1f) — saturation non confirmée "
                         "(%d/%d), pas jugé", vmid, lat, _budget_v,
                         _satur_streak[vmid], _CONFIRMATIONS_SATURATION)
                latency[vmid] = None
                continue
        else:
            _satur_streak[vmid] = 0
        latency[vmid] = lat
    if not mvs:
        return {"nodes": 0}

    def _deploy(name, params, hostname):
        # crée un conteneur compute (multiview shard) + déploie son script ; renvoie str(vmid) = ref.
        vmid = _dc.creer_container_compute(node_id, "multiview", hostname)
        if not vmid:
            return None
        deployer_script(vmid, "multiview", params)
        return str(vmid)

    def _destroy(ref):
        try: _ct.detruire_container(int(ref))
        except Exception as _e: log.warning("fabric teardown %s: %s", ref, _e)

    def _apply_mv(ip, default_template, overlays, flux_config, meter_blocks=None,
                  video_history_blocks=None, audio_history_blocks=None):
        # Applique style (/style) + overlays (/overlays) + sources+meter_blocks (/reconfigure) À
        # CHAUD. default_template : le MODÈLE PAR DÉFAUT du mur suit le RÔLE du conteneur — None
        # pour l'ASSEMBLEUR (copie pure : sans le clear explicite, le DEFAULT_TEMPLATE du mur
        # restait chargé dans le moteur converti à chaud → l'habillage se redessinait autour de
        # chaque BLOC de shard), la valeur du mur au restore (monolithe).
        # meter_blocks : VU-mètres de MUR (0.36.0) — même remplacement atomique que flux_config,
        # aucune résolution de dims (cf. _multiview_hot_apply). Toujours réappliqués (assembleur
        # ET restore) car ils étaient jusqu'ici silencieusement perdus par le canal fabric (le
        # mur logique édité persistait bien meter_blocks en DB — cf. _multiview_hot_apply — mais
        # ce canal-ci, dédié aux transitions shard↔monolithe, ne les propageait jamais au conteneur).
        # Polices : ce canal-ci (transitions shard↔monolithe) reconfigure À CHAUD un conteneur
        # dont le script a pu être rendu AVANT l'ajout de la police → on ré-embarque le base64
        # des polices référencées par les params poussés (sinon un mur shardé retomberait sur
        # DejaVu). Résolution à la volée : rien de gras n'est jamais persisté (cf. _gras).
        _lib = _gras({"default_template": default_template, "overlays": overlays or [],
                      "flux_config": flux_config or [], "meter_blocks": meter_blocks or [],
                      "video_history_blocks": video_history_blocks or [],
                      "audio_history_blocks": audio_history_blocks or []}).get("font_library") or []
        # Statuts VÉRIFIÉS et renvoyés : ce canal sert aussi au REBIND d'un shard, où « le push
        # a-t-il pris ? » décide de garder le conteneur ou de le remplacer. Un 500 avalé y
        # laisserait un shard au contenu périmé, sans rien de visible côté orchestrateur.
        _r1 = requests.post(f"http://{ip}:8082/style",
                            json={"default_template": default_template}, timeout=3)
        _r2 = requests.post(f"http://{ip}:8082/overlays", json={"overlays": overlays or []}, timeout=3)
        _r3 = requests.post(f"http://{ip}:8082/reconfigure",
                            json={"flux_config": flux_config or [], "meter_blocks": meter_blocks or [],
                                  # Frises d'historique de MUR (0.37.0) : même transit que meter_blocks
                                  # (sinon même fuite silencieuse sur un mur shardé).
                                  "video_history_blocks": video_history_blocks or [],
                                  "audio_history_blocks": audio_history_blocks or [],
                                  "font_library": _lib},
                            timeout=3)
        return all(r.status_code == 200 for r in (_r1, _r2, _r3))

    def _reconfigure(vmid, asm):
        ip = get_container_ip(vmid)
        if not ip:
            return
        try:
            _apply_mv(ip, None, asm.get("overlays"), asm.get("flux_config"), asm.get("meter_blocks"),
                      asm.get("video_history_blocks"), asm.get("audio_history_blocks"))
        except Exception as _e:
            log.warning("fabric reconfigure %s: %s", vmid, _e)

    def _pret(refs, deadline_s=40.0):
        """Attend que des shards fraîchement créés PRODUISENT (première trame publiée sur :8080).
        Renvoie l'ensemble des refs prêts. Un shard qui ne répond pas dans le délai n'est PAS
        déclaré prêt : l'appelant diffère alors la bascule du mur concerné et réessaiera à la passe
        suivante — le mur continue d'afficher sa composition actuelle, ce qui est toujours mieux
        qu'une région noire. Le délai couvre docker run + démarrage du script + création du flux."""
        attente = {str(r) for r in refs or ()}
        prets = set()
        t0 = _time.monotonic()
        while attente and (_time.monotonic() - t0) < deadline_s:
            if perime is not None and perime():
                log.info("tissu : attente de production abandonnée — une édition plus récente "
                         "est arrivée (shard(s) %s)", sorted(attente))
                return prets
            for r in sorted(attente):
                try:
                    ip = get_container_ip(int(r))
                except (TypeError, ValueError):
                    attente.discard(r); continue      # ref non numérique : pas d'attente possible
                if not ip:
                    continue
                try:
                    j = requests.get(f"http://{ip}:8080/", timeout=1.5).json()
                except Exception:                                          # noqa: BLE001
                    continue
                # Un agent qui répond ne suffit PAS : le flux MXL peut ne pas encore exister. Le
                # témoin est la CADENCE publiée — le multiview ne sert pas de `frame_index` (il
                # publie `fps`/`frames_per_s`) ; on tolère `frame_index` pour les autres types.
                # ⚠ Ne jamais deviner ce nom : s'être fié au contrat générique de CLAUDE.md sans
                # vérifier ce plugin a figé toutes les bascules du tissu (2026-08-06).
                if (float(j.get("fps") or 0) > 0 or float(j.get("frames_per_s") or 0) > 0
                        or int(j.get("frame_index") or 0) > 0):
                    prets.add(r); attente.discard(r)
            if attente:
                _time.sleep(1.0)
        if attente:
            log.warning("tissu : shard(s) %s toujours sans trame après %.0f s — bascule différée",
                        sorted(attente), deadline_s)
        return prets

    def _rebind(ref, params):
        """Remplace À CHAUD le contenu d'un shard existant : même conteneur, même shm de sortie,
        donc AUCUNE reconfiguration de l'assembleur et aucune seconde de région vide. Renvoie True
        seulement si le shard a réellement pris la nouvelle config — sinon l'appelant repart sur
        créer+détruire, qui repose un script neuf.

        Deux refus délibérés :
          • script pas à la version du manifeste → un vieux script ignorerait les champs récents
            (même raisonnement que la garde de `deployer_script`) ; on préfère le remplacement ;
          • échec d'un des trois POST → on ne garde pas un shard au contenu périmé.
        La config est aussi PERSISTÉE dans `deploy_config` : le rootfs des conteneurs est éphémère,
        un shard muté seulement à chaud rendrait l'ancienne région après un redémarrage."""
        import json as _json_rb
        try:
            svmid = int(ref)
        except (TypeError, ValueError):
            return False
        c = db_get_container(svmid)
        if not c:
            return False
        try:
            _old = (_json_rb.loads(c.get("deploy_config") or "{}") or {}).get("params") or {}
        except Exception:                                                  # noqa: BLE001
            return False
        _ver = (plugins.get("multiview") or {}).get("version")
        ip = c.get("ip") or get_container_ip(svmid)
        if not ip:
            return False
        # Même règle que la garde de hot-apply : ce que le shard EXÉCUTE prime sur l'estampille
        # en base. Muter à chaud un script d'une autre version, c'est le risque de champs
        # récents silencieusement ignorés — le refus fait retomber sur créer+détruire, qui
        # repose un script neuf.
        _vue = version_en_cours(ip, svmid)
        if (_vue if _vue is not None else _old.get("plugin_version")) != _ver:
            return False
        try:
            ok = _apply_mv(ip, params.get("default_template"), params.get("overlays"),
                           params.get("flux_config"))
        except Exception as _e:                                            # noqa: BLE001
            log.warning("fabric rebind %s: %s", ref, _e)
            return False
        if not ok:
            log.warning("fabric rebind %s : le shard n'a pas pris la config — remplacement", ref)
            return False
        db_update_deploy_config(svmid, "multiview", {**params, "plugin_version": _ver})
        return True

    def _etat(vmid):
        """État RAPPORTÉ par le mur (`:8082/state`) — lecture PURE, aucun effet de bord.
        Sert à ne repousser la config d'assembleur qu'en cas de divergence réelle : un GET ne coûte
        aucune recuisson, alors que le re-push périodique qu'il remplace en provoquait une (purge du
        cache de polices + `overlay_dirty` → trame lente → image figée toutes les ~10 min).
        Toute erreur renvoie None = « état inconnu » → l'appelant repousse (jamais l'inverse)."""
        ip = get_container_ip(vmid)
        if not ip:
            return None
        try:
            r = requests.get(f"http://{ip}:8082/state", timeout=2)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def _restore(vmid, logical):
        ip = get_container_ip(vmid)
        if not ip:
            return
        try:   # restaure le modèle par défaut + overlays + sources + meter_blocks d'ORIGINE (monolithe)
            _apply_mv(ip, logical.get("default_template"), logical.get("overlays"),
                      logical.get("flux_config"), logical.get("meter_blocks"),
                      logical.get("video_history_blocks"), logical.get("audio_history_blocks"))
        except Exception as _e:
            log.warning("fabric restore %s: %s", vmid, _e)

    # TISSU EN TRANCHES (docs/chantiers/TISSU_SLICE.md) : setting fabric_slice_mode (défaut OFF, opt-in acté)
    # OU réglage global slice_mode_global (Réglages → Vidéo — l'un OU l'autre actif ⇒ tissu en
    # tranche) → les nœuds du tissu sont matérialisés en cadence "flow" + slice. NB : un nœud DÉJÀ
    # matérialisé garde ses params (dédup par signature) — basculer le setting exige un teardown
    # des nœuds fab (ou l'édition du mur, qui re-planifie) pour prendre effet.
    _slice = any(str(_st.get(k) or "").strip().lower() in ("1", "true", "yes", "on")
                 for k in ("fabric_slice_mode", "slice_mode_global"))
    # Combien de shards ce nœud peut-il réellement ÉPINGLER ? Budget = cœurs LIBRES du pool +
    # ceux que tiennent déjà les shards du tissu (ils seront relâchés si la découpe change) ÷
    # profil du type. Sans ce plafond, le tissu planifiait des conteneurs que le nœud ne pouvait
    # que sur-souscrire — mesuré sur dl360-1 : `0 cœur(s) libre(s)/12, 3 demandé(s) → repli quota`.
    _max_noeuds = None
    try:
        from . import core_pool as _cp, cpu_profiles as _cpr
        _par = int((_cpr._resources("multiview") or {}).get("cores") or 0)
        if _par > 0:
            _alloc = _cp.allocations_by_vmid(node_id)
            _tenus = 0
            for _r in _dfa(node_id):
                if _r.get("kind") in ("shard", "shared") and _r.get("ref"):
                    try:
                        _tenus += len(_alloc.get(int(_r["ref"])) or [])
                    except (TypeError, ValueError):
                        pass
            _libres = int((_cp.cores_status(node_id) or {}).get("free") or 0)
            _max_noeuds = max(1, (_libres + _tenus) // _par)
    except Exception as _e:                                                # noqa: BLE001
        log.warning("tissu : capacité d'épinglage du nœud %s illisible (%s) — pas de plafond",
                    node_id, _e)
    return _cf.reconcile_fabric(node_id, mvs, latency, _deploy, _destroy, _reconfigure, _restore,
                                budget_ms=budget, max_cells_per_shard=max_cells,
                                budget_by_vmid=budget_by_vmid, slice_mode=_slice, etat_fn=_etat,
                                rebind_fn=_rebind, pret_fn=_pret, max_noeuds=_max_noeuds,
                                perime_fn=perime,
                                # Sortie ÉCONOMIQUE du sharding : murs dont les shards cumulés
                                # tiendraient dans le budget d'un monolithe, confirmé plusieurs
                                # passages. Sans ça, shardé un jour = shardé toujours.
                                restaurables=_restaurables_tissu(node_id, budget_by_vmid, budget))
