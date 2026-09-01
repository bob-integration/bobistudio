# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import json
import logging
import os
import sys
import secrets
import threading
import time
from datetime import timedelta
from flask import Flask
from app.config import LOG_PATH, CHECK_INTERVAL
from app.database import (init_db, db_get_containers,
                      db_get_setting, db_set_setting,
                      db_update_status, db_add_alert)
from app.containers import redemarrer_container
from app.metrics import rafraichir_metrics
from app.deploy import rewire_on_restart
from app import backup
from app.routes import bp

from app import logsetup
# ⚠ Le `StreamHandler` n'est posé QUE si la sortie est un TERMINAL. Il sert à voir défiler les
# lignes quand on lance `python main.py` à la main ; en service, la destination du fichier est le
# handler ci-dessous, et lui seul. La règle a d'abord été « poser la console sauf si stdout pointe
# déjà sur le fichier de journal » — juste, mais dépendante de la façon dont l'unité systemd est
# écrite : sous `StandardOutput=append:<fichier>` chaque ligne partait DEUX FOIS dans le même
# fichier (mesuré le 2026-08-15 : la moitié d'un journal de 304 Mo était des doublons exacts).
# `isatty()` ne dépend d'aucune configuration extérieure et couvre le même cas plus largement :
# aucune redirection, quelle qu'elle soit, ne peut recréer le doublon.
_handlers = [logsetup.make_handler(LOG_PATH)]   # rotation taille (prioritaire) + temps
try:
    _console = sys.stderr is not None and sys.stderr.isatty()
except (AttributeError, ValueError):
    _console = False
if _console:
    _handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger(__name__)

from jinja2 import ChoiceLoader, FileSystemLoader
app = Flask(__name__,
            template_folder="templates",
            static_folder="static")
app.jinja_loader = ChoiceLoader([
    FileSystemLoader("templates"),
    FileSystemLoader("services"),
])
app.jinja_env.filters['from_json'] = lambda s: json.loads(s) if s else None

def _asset_v(path):
    """Cache-buster pour les assets statiques servis hors blueprint Flask (référencés en dur
    par /static/...). Renvoie le mtime du fichier → l'URL change à chaque déploiement, le
    navigateur recharge sans Ctrl-F5. `path` est relatif au dossier static (ex. 'io2110.js')."""
    try:
        return int(os.path.getmtime(os.path.join("static", path)))
    except OSError:
        return 0

app.jinja_env.globals['asset_v'] = _asset_v

# GABARITS RELUS À CHAUD. Sans ça, Jinja met les gabarits en cache LRU et ne regarde jamais leur
# date : un fichier modifié n'est repris QUE s'il se fait évincer du cache — donc jamais pour
# `layout.html`, utilisé par toutes les pages, et par intermittence pour les autres. Le
# 2026-08-19/20, trois correctifs de la page Câbles ont été testés par l'utilisateur sur un
# gabarit périmé, avec le temps perdu que ça suppose. Le coût est un `stat()` par gabarit et par
# rendu — négligeable ici (les pages sont rendues à la navigation, pas au poll : les rafraîchis-
# sements de 5 s passent par /api/*, qui ne rend aucun gabarit).
# Pour revenir au comportement d'avant : passer à False et redémarrer.
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# i18n : globals Jinja `t()` / `_()` (référencent des CLÉS, cf. app/i18n.py)
from app import i18n as _i18n
app.jinja_env.globals['t'] = _i18n.t
app.jinja_env.globals['_'] = _i18n.t

@app.context_processor
def inject_i18n():
    """Expose la langue courante, la liste des langues et le sous-catalogue JS
    (clés js.*/plugin.*) injecté dans window.I18N par layout.html."""
    lang = _i18n.current_lang()
    return {"lang": lang,
            "languages": _i18n.LANGUAGES,
            "js_catalog": _i18n.js_catalog(lang)}

# Secret key persistée en DB (générée au premier démarrage)
def _ensure_secret_key():
    init_db()
    # Les habilitations sont chargées ICI, et pas dans `__main__` : ce chemin est le seul que
    # traversent AUSSI les processus qui importent l'app sans passer par `main` (WSGI, bancs,
    # outils). Un contrôleur qui servirait des permissions par défaut alors que la base en porte
    # d'autres accorderait des droits que personne n'a donnés.
    from app.auth import recharger_habilitations
    recharger_habilitations()
    # ★ BALAYAGE DES LIGNES DE LIBELLÉ SANS CONFIGURATION. Une source CONFIGURÉE (un libellé
    # écrit, une correspondance TSL ou IS-07) se garde même quand son conteneur a disparu : il
    # reviendra peut-être. Une ligne sans rien dessus n'est que du résidu — sur ce parc,
    # dix-neuf sur soixante-huit — et il n'y a rien à décider. Best-effort : cette fonction ne
    # doit jamais empêcher un démarrage.
    try:
        from app.database import db_purger_libelles_orphelins
        db_purger_libelles_orphelins()
    except Exception as _e:
        logging.getLogger(__name__).warning("libellés : balayage impossible (%s)", _e)
    raw = db_get_setting("flask_secret_key", None)
    if not raw:
        raw = secrets.token_hex(32)
        db_set_setting("flask_secret_key", raw)
    return raw

app.secret_key = _ensure_secret_key()
app.permanent_session_lifetime = timedelta(days=30)
# Cookie sécurisé seulement quand servi via TLS (reverse-proxy)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# SESSION_COOKIE_SECURE : le cookie de session n'est émis qu'en HTTPS. Défaut FALSE : l'accès
# HTTP direct (LAN, install neuve) fonctionne d'emblée — sinon le cookie `Secure` est rejeté par
# le navigateur en HTTP et la connexion boucle EN SILENCE (login OK côté serveur, session perdue).
# À ACTIVER explicitement derrière un reverse-proxy TLS : setting `cookie_secure=True` (DB) ou
# variable d'env BOBI_COOKIE_SECURE=1.
def _cookie_secure_enabled():
    env = os.environ.get("BOBI_COOKIE_SECURE")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "")
    val = db_get_setting("cookie_secure", False)
    return val in (True, 1, "1", "true", "True", "yes", "on")
app.config["SESSION_COOKIE_SECURE"] = _cookie_secure_enabled()
# Le reverse-proxy doit transmettre X-Forwarded-Proto pour que Flask sache que c'est HTTPS
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

app.register_blueprint(bp)
# ⚠ SEUL IMPORT DE SERVICE AU NIVEAU MODULE — donc le seul qui puisse empêcher le
# DÉMARRAGE. Tous les autres sont paresseux, dans des fonctions : un service absent y
# casse un chemin, pas le produit.
#
# Il l'était aussi, et c'était une panne sèche : un `git clone` SANS `--recursive` —
# l'oubli le plus courant — donnait `ImportError: cannot import name 'nmos'` avant même
# la première page. Constaté le 2026-09-01 sur le dépôt public fraîchement publié.
#
# Or NMOS est fonctionnellement OPTIONNEL : `nmos_enabled` vaut false sur une installation
# neuve. Empêcher le démarrage pour une fonctionnalité éteinte par défaut est
# disproportionné — et le pire est que le remède (installer le service depuis la page
# Catalogue) exige que le produit TOURNE. Un cercle dont l'utilisateur ne peut pas sortir.
#
# On dégrade donc : le service manque, l'API NMOS n'existe pas, tout le reste marche, et
# l'alerte le dit en toutes lettres plutôt que de laisser deviner.
try:
    from services import nmos
    app.register_blueprint(nmos.bp)
    # IS-04 exige des en-têtes CORS sur toutes les réponses de l'API, y compris les erreurs —
    # donc au niveau de l'application, pas du blueprint (un 404 sur un chemin inconnu lui échappe).
    nmos.installer_cors(app)
except (ImportError, AttributeError) as _e:
    # ⚠ ATTRAPER AUSSI AttributeError, et ce n'est pas du zèle : un `git clone` sans
    # `--recursive` laisse un dossier services/nmos VIDE — pas absent. Python l'accepte
    # alors comme paquet-espace-de-noms, l'import RÉUSSIT, et c'est `nmos.bp` qui casse.
    # Un garde sur le seul ImportError ne se déclenche donc JAMAIS dans le cas qu'il vise.
    # Vérifié en reproduisant le clone non récursif.
    nmos = None
    logging.getLogger(__name__).warning(
        "services/nmos absent ou incomplet (%s) — l'API NMOS ne sera pas servie. Si vous "
        "avez cloné sans --recursive : `git submodule update --init services/nmos`. Sinon, "
        "installez le service depuis Réglages → Catalogue.", _e)
from app.testplan import testplan_bp
app.register_blueprint(testplan_bp)

@app.context_processor
def inject_theme():
    """Thème effectif : préférence UTILISATEUR → défaut du système → 'classic'.

    Même cascade que la langue (`i18n.current_lang`). Le setting global `theme` n'est plus
    le thème de tout le monde : c'est le défaut servi à qui n'a rien choisi (et aux pages
    hors session, login compris)."""
    from app import settings as st
    valid = {t["id"] for t in st.THEMES}
    theme = None
    try:
        from app.auth import current_user
        u = current_user()
        if u and u.get("theme") in valid:
            theme = u["theme"]
    except Exception:
        pass
    if theme is None:
        theme = st.get("theme") or "classic"
    if theme not in valid:
        theme = "classic"
    # `languages` voyage avec le thème : le menu utilisateur porte les deux, et
    # chaque page qui l'affiche en aurait sinon besoin dans son propre contexte.
    from app import i18n as _i18n
    return {"theme": theme,
            "theme_defaut": st.get("theme") or "classic",
            "themes": st.THEMES,
            # Page RECETTE : le layout doit savoir s'il affiche l'entrée de menu.
            # Un seul booléen, pas les réglages entiers — exposer tout le jeu à
            # tous les gabarits invite à s'en servir n'importe où.
            "recette_active": str(st.get("testplan_enabled", "0")).strip().lower()
                              not in ("0", "false", "off", ""),
            "languages": _i18n.LANGUAGES}

@app.context_processor
def inject_brand():
    """Identité client (personnalisation) exposée à tous les templates, EN PLUS de la
    marque produit (« Bobi.Studio ») qui reste affichée partout."""
    from app import settings as st
    return {"brand": {
        "system_name": st.get("brand_system_name") or "",
        "org_name":    st.get("brand_org_name") or "",
        "logo_url":    st.get("brand_logo_url") or "",
        "location":    st.get("brand_location") or "",
    }}

@app.context_processor
def inject_auth():
    """Injecte current_user et helper has_perm() dans tous les templates."""
    from app.auth import current_user, has_perm
    return {"current_user": current_user(), "has_perm": has_perm}

@app.context_processor
def inject_plugins():
    """Expose les sections de nav contribuées par les plugins (rubrique Medias…) et
    les schémas de config déclaratifs (Tier 1, rendus dans la palette)."""
    from app import plugins
    return {"plugin_sections": plugins.sections(),
            "plugin_config_schemas": plugins.config_schemas(),
            "plugin_versions": {t: plugins.versions(t) for t in plugins.REGISTRY},
            "plugin_video_format": {t: m.get("video_format", True)
                                    for t, m in plugins.REGISTRY.items()},
            # Défaut 'docker' — cf. plugins.runtime() : le backend LXC est retiré, un manifeste
            # sans clé `runtime` décrit un type Docker comme tous les autres.
            "plugin_runtimes": {t: (m.get("runtime") or "docker")
                                for t, m in plugins.REGISTRY.items()},
            "plugin_image_kinds": {t: plugins.image_kind(t) for t in plugins.REGISTRY},
            # Profil de ressources DÉCLARÉ (resources.cores/memory/pin/gpu…) : c'est ce que
            # `docker_compute` appliquera au `docker run`. Exposé à la palette de création pour
            # annoncer le coût AVANT de créer. Un type sans bloc `resources` renvoie {} — l'UI doit
            # dire « pool partagé », pas « 0 cœur » (cf. docker_compute.py, repli pool partagé).
            "plugin_resources": {t: (m.get("resources") or {}) for t, m in plugins.REGISTRY.items()},
            # Badge de carte : le libellé passe par le catalogue (clé `type.<id>.badge`, repli sur
            # le manifeste) — sinon la carte d'un conteneur reste étiquetée en français en anglais.
            "plugin_badges": {t: dict(m["badge"],
                                      label=plugins._traduit(f"type.{t}.badge",
                                                             m["badge"].get("label") or t))
                              for t, m in plugins.REGISTRY.items() if m.get("badge")},
            "mxl_plugins_json": plugins.manifest_summary_for_js(),
            "badge_css": plugins.badge_css_vars()}

# Mémoire de l'état précédent par vmid pour ne loguer qu'aux transitions
_prev_pve_status = {}
_fabric_tick = [0]   # throttle de l'auto-trigger du tissu de composition (gated par réglage fabric_auto)

# Monitoring pyramide (P4) : état problème par pyramide → alerte UNIQUEMENT à la transition
# OK→problème (pas de spam ; re-alerte si le problème réapparaît après résolution).
_pyr_alert_state = {}

def _check_pyramide_alerts():
    """Alerte (transition) sur le gaspillage/sous-couverture des pyramides : proxies orphelins
    (produits, lus par personne) et besoins ≥ seuil non couverts (reconcile à relancer)."""
    try:
        from app.metrics import pyramide_overview
        ov = pyramide_overview()
    except Exception as e:
        log.error(f"check pyramide alerts: {e}")
        return
    for pyr in ov.get("pyramides", []):
        vmid = pyr.get("vmid")
        orphans = int(pyr.get("orphans") or 0)
        unmet_q = [u for u in (pyr.get("unmet") or []) if u.get("would_qualify")]
        problem = bool(orphans) or bool(unmet_q)
        if problem and not _pyr_alert_state.get(vmid):
            parts = []
            if orphans:
                parts.append(f"{orphans} proxy(s) orphelin(s) (produits, lus par personne)")
            if unmet_q:
                parts.append(f"{len(unmet_q)} besoin(s) ≥ seuil non couvert(s) — lancer « Optimiser »")
            db_add_alert(f"Pyramide {pyr.get('hostname') or vmid} : " + " ; ".join(parts), "warning")
        _pyr_alert_state[vmid] = problem

# Métriques en POOL (audit B5) : un agent qui traîne ne bloque plus ni les autres conteneurs
# ni le tick suivant. Garde anti-réentrance par vmid (un rafraîchissement encore en vol n'est
# pas redoublé). NB : les caches métriques peuvent donc avoir ~1 tick de retard pour les
# consommateurs du même tick (_check_pyramide_alerts) — sans conséquence (lecture 5 s plus tard).
from concurrent.futures import ThreadPoolExecutor as _TPE
_metrics_pool = _TPE(max_workers=8, thread_name_prefix="metrics")
_metrics_inflight = set()
_metrics_lock = threading.Lock()

def _soumettre_metrics(vmid):
    with _metrics_lock:
        if vmid in _metrics_inflight:
            return
        _metrics_inflight.add(vmid)
    def _run():
        try:
            rafraichir_metrics(vmid)
        except Exception as e:
            log.error(f"Erreur métriques {vmid}: {e}")
        finally:
            with _metrics_lock:
                _metrics_inflight.discard(vmid)
    _metrics_pool.submit(_run)

# Watchdog de durée de tick (audit B5) : un tick > seuil = quelque chose traîne (agent,
# SSH legacy, DB) → alerte à transition, info au retour à la normale.
_TICK_SLOW_S = 30.0
_tick_slow = [False]

def _watchdog_tick(duree):
    if duree > _TICK_SLOW_S and not _tick_slow[0]:
        log.warning(f"surveillance : tick lent ({duree:.1f} s)")
        db_add_alert(f"Boucle de surveillance lente ({duree:.0f} s) — un nœud/agent qui "
                     "traîne ? La détection de pannes est retardée d'autant.", "warning")
        _tick_slow[0] = True
    elif duree <= _TICK_SLOW_S and _tick_slow[0]:
        db_add_alert("Boucle de surveillance : durée redevenue normale.", "info")
        _tick_slow[0] = False

_PYR_RECONCILE_S = 120.0      # période de la réconciliation pyramide (idempotente)
_pyr_last = [0.0]             # dernier lancement (liste = mutable depuis la closure)
_pyr_encours = threading.Event()

_RDMA_RECONCILE_S = 30.0      # période de la réconciliation RDMA (idempotente)
_rdma_last = [0.0]
_rdma_encours = threading.Event()


def _rdma_reconcile_periodique():
    """Réconcilie les liens RDMA en tâche de fond.

    ★ POURQUOI CE DÉPORT (incident du 2026-08-08). Ce bloc tournait EN LIGNE dans la boucle de
    surveillance de 5 s, sous le commentaire « quasi gratuit si aucun lien actif ». Il ne l'est
    plus dès qu'il y a des liens EN ÉCHEC : chaque démarrage de target attend son `target-info`
    pendant 25 s, et le parc en comptait 32 bloqués. La boucle qui surveille les conteneurs,
    rafraîchit les métriques et déclenche le tissu se retrouvait bloquée derrière — **tick mesuré
    à 63 s** (le pire jamais relevé ; le record précédent était 41 s), orchestrateur à 154 % de CPU
    et interface qui ne répondait plus, jusqu'à l'arrêt manuel du service.

    C'est exactement le raisonnement déjà écrit deux lignes plus haut pour la PYRAMIDE (« elle
    interroge le :8080 de chaque consommateur → JAMAIS dans cette boucle de 5 s ») et appliqué au
    TISSU (dispatché en threads). Le bloc RDMA était le seul à enfreindre la règle. On copie donc
    le motif éprouvé : thread détaché, jamais deux en vol (`_rdma_encours`), période bornée.

    Conséquence assumée : les liens sont réconciliés au plus toutes les `_RDMA_RECONCILE_S`, au
    lieu de « à chaque tick quand la passe précédente a fini ». C'est plus lent SUR LE PAPIER et
    plus rapide EN VRAI — une passe qui bloquait 60 s ne réconciliait rien de plus, elle empêchait
    juste le reste de tourner."""
    import time as _t
    if _rdma_encours.is_set() or (_t.monotonic() - _rdma_last[0]) < _RDMA_RECONCILE_S:
        return
    _rdma_last[0] = _t.monotonic()
    _rdma_encours.set()

    def _run():
        try:
            from services import rdma
            # Marque 'error' + alerte (transition) les liens dont un conteneur target/initiator
            # n'est plus up.
            rdma.verifier_liens()
            # Liens dont une extrémité a été supprimée : ils ne peuvent PAS aboutir et leurs
            # retentatives noient le journal. Purge avant la réconciliation, pour ne pas les
            # recompter comme du travail à faire.
            rdma.purger_liens_orphelins()
            # Pendant du précédent : liens dont le NŒUD existe toujours mais dont plus AUCUN
            # conteneur ne lit le flux. Le teardown événementiel (release_cable_link) ne couvre
            # que le décâblage et la destruction ; un consommateur déplacé ou reconfiguré autrement
            # laissait ses liens à vie. Relevé le 2026-08-07 : 86 liens pour ~21 nécessaires.
            # Passe AVANT reconcilier_cables, qui recrée ce qui manque : les deux prédicats sont
            # le même (le câblage dérivé), donc ils ne peuvent pas se contredire.
            rdma.purger_liens_sans_consommateur()
            # Câble inter-nœud sans lien de réplication : on le provisionne. `_apply_wire` le fait
            # déjà à la pose, mais tous les câbles ne passent pas par lui (composeur multiview,
            # restauration de snapshot, câblages antérieurs à la fonctionnalité) — ceux-là
            # n'obtenaient rien et le flux n'arrivait jamais.
            rdma.reconcilier_cables()
            # Flux LUS mais plus ÉCRITS. Rien à voir avec l'état des liens : un flux MXL survit à
            # la mort de son producteur et continue de servir ses lecteurs (tête collée à la grille
            # TAI, cadence nominale, anneau périmé rejoué). Passe APRÈS la réconciliation, pour ne
            # pas juger un flux que la passe est justement en train de rétablir.
            rdma.verifier_fraicheur_flux()
            rdma.echantillonner_stats_all()
        except Exception as _e:
            log.error(f"Erreur vérification liens RDMA: {_e}")
        finally:
            _rdma_encours.clear()

    threading.Thread(target=_run, daemon=True, name="rdma-reconcile").start()


def _pyramide_reconcile_periodique():
    """Réconcilie les tailles de pyramide de tous les nœuds qui en portent une, en tâche de fond.

    Le travail est déporté dans un thread parce qu'il interroge le `:8080` de chaque consommateur
    (1 s de timeout chacun) : inline, il bloquerait la boucle de surveillance et retarderait la
    détection des conteneurs tombés. `_pyr_encours` garantit qu'il n'y en a jamais deux en vol —
    sur un parc lent, deux passes concurrentes se disputeraient les mêmes hot-applies."""
    import time as _t
    if _pyr_encours.is_set() or (_t.monotonic() - _pyr_last[0]) < _PYR_RECONCILE_S:
        return
    _pyr_last[0] = _t.monotonic()
    _pyr_encours.set()

    def _run():
        try:
            import json as _json
            from app.deploy import reconcile_pyramide_sizes
            from app.database import db_get_containers
            noeuds = sorted({c.get("node_id") for c in db_get_containers()
                             if c.get("node_id") is not None
                             and (_json.loads(c.get("deploy_config") or "{}") or {}).get("type")
                             == "pyramide"})
            for n in noeuds:
                try:
                    _ch = (reconcile_pyramide_sizes(n) or {}).get("changed") or []
                    if _ch:
                        log.info("réconciliation pyramide (périodique) nœud %s : %s", n, _ch)
                except Exception as _e:
                    log.warning("réconciliation pyramide nœud %s : %s", n, _e)
        except Exception as _e:
            log.warning("réconciliation pyramide périodique : %s", _e)
        finally:
            _pyr_encours.clear()

    threading.Thread(target=_run, daemon=True).start()


def surveillance():
    while True:
        _t0 = time.monotonic()
        # db_get_containers() sous garde : une seule OperationalError (DB busy) hors try
        # tuait le thread pour toujours (plus de surveillance/métriques/backup, sans alerte).
        try:
            containers = db_get_containers()
        except Exception as e:
            log.error(f"Erreur surveillance (lecture containers): {e}")
            containers = []
        # Inventaire docker PAR NŒUD, en parallèle (audit B5) : un appel agent par nœud
        # remplace un `docker inspect` par conteneur — un nœud lent n'aveugle plus la boucle.
        from app import fleet_status
        try:
            _states, _unreach, _sans_agent = fleet_status.poll_nodes()
        except Exception as e:
            log.error(f"Erreur inventaire flotte: {e}")
            _states, _unreach, _sans_agent = {}, set(), set()
        # NŒUD TOMBÉ : `_unreach` était calculé à chaque tour et JETÉ — il ne servait qu'à
        # empêcher les transitions de conteneurs. Rien n'écrivait jamais `nodes.status="down"`
        # (la seule fonction qui le fait, node_driver.refresh, n'a aucun appelant), donc rien
        # ne signalait un nœud mort et `_eligible()` continuait de lui envoyer des déploiements.
        try:
            _noeuds_ko = fleet_status.evaluer_noeuds(_states, _unreach)
        except Exception as e:
            log.error(f"Erreur évaluation des nœuds: {e}")
            _noeuds_ko = {}
        for c in containers:
            vmid = c["vmid"]
            try:
                # Full-Docker : liveness via l'inventaire par-nœud (repli docker inspect pour
                # un nœud sans agent), et PAS de redemarrer_container automatique (--rm → un
                # exited est supprimé ; auto-restart = restart-storm). Alerte à la transition.
                st = fleet_status.status_of(_states, _unreach, _sans_agent, c)
                if st is None:
                    # Nœud injoignable. Ne PAS fabriquer un « stopped » (un timeout d'agent n'en
                    # est pas un) — mais ne pas laisser croire que tout va bien non plus : ce
                    # `continue` sautait aussi `_soumettre_metrics`, donc toute la machinerie
                    # `unreachable` de metrics, et le conteneur restait affiché `running` avec
                    # sa dernière cadence, indéfiniment. On dit ce qu'on sait : rien.
                    fleet_status.marquer_absent(c, _noeuds_ko)
                    continue
                fleet_status.oublier_absent(vmid)
                if st == "fallback":  # nœud legacy sans agent → chemin historique par-vmid
                    from app import docker_driver
                    st = docker_driver.status_docker(vmid)
                db_update_status(vmid, "running" if st == "running" else "stopped")
                prev = _prev_pve_status.get(vmid)
                if st == "running":
                    if prev is not None and prev != "running":
                        threading.Thread(target=rewire_on_restart, args=(vmid,), daemon=True).start()
                    _soumettre_metrics(vmid)
                elif prev == "running":
                    db_add_alert(f"Conteneur Docker {vmid} arrêté ({st}) — "
                                 "relancer depuis l'UI si besoin", "warning")
                _prev_pve_status[vmid] = "running" if st == "running" else "stopped"
            except Exception as e:
                log.error(f"Erreur surveillance {vmid}: {e}")
        # Réconciliation DB↔réalité (audit B2) : disparus (desired running absents de docker ps)
        # + orphelins (bobi-* inconnus de la DB). Nœuds injoignables exclus (pas de faux positifs).
        try:
            fleet_status.reconcile(_states, _unreach)
        except Exception as e:
            log.error(f"Erreur réconciliation flotte: {e}")
        # Alertes pyramide (gaspillage / sous-couverture) — lit les caches fraîchement rafraîchis.
        try:
            _check_pyramide_alerts()
        except Exception as e:
            log.error(f"Erreur alertes pyramide: {e}")
        # Bande passante mémoire par nœud (canary memcpy, throttlé ~60 s) — le compositing est
        # memory-bandwidth bound ; alerte par transition quand le bus RAM sature.
        try:
            from app import membw
            membw.sample_all()
        except Exception as e:
            log.error(f"Erreur mesure bande passante mémoire: {e}")
        # Télémétrie GPU par nœud (NVIDIA : util/VRAM + échange PCIe RAM↔GPU, throttlé ~5 s) —
        # le compositing accéléré est borné par le transfert PCIe ; fusionnée dans node_health.
        try:
            from app import gpu
            gpu.sample_all()
        except Exception as e:
            log.error(f"Erreur télémétrie GPU: {e}")
        # Santé matérielle des nœuds + contrôleur (CPU/RAM/disque/versions, throttlé ~5 s) —
        # remplace la console Proxmox sur les nœuds sans hyperviseur. Fusionne membw + gpu (ci-dessus).
        try:
            from app import node_health
            node_health.sample_all()
        except Exception as e:
            log.error(f"Erreur santé nœuds: {e}")
        # Horloges du cluster : relevé throttlé (~20 s) qui alimente l'historique de la page
        # Réglages → Réseau → Horloges. Sans ce tick, la courbe ne se remplirait que pendant qu'un
        # opérateur regarde la page — donc jamais quand ça compte, et l'écart d'un nœud qui part à
        # la dérive n'aurait aucune trace.
        try:
            from app import clocks
            clocks.etat()
        except Exception as e:
            log.error(f"Erreur horloges: {e}")
        # Réconciliation du pinning MOTEUR : le cpuset réellement posé (docker inspect) fait foi et
        # est réenregistré dans core_pool + retiré du pool de calcul (throttlé 60 s/nœud, lecture
        # seule côté nœud). Sans ça, une dérive née d'un changement de réglage reste invisible.
        try:
            from app import docker_driver as _dd
            from app.database import db_get_nodes as _dgn2
            from app import core_pool as _cp
            for _n in _dgn2():
                # En THREAD : un `docker inspect` sur un nœud qui traîne ne doit pas allonger le tick.
                threading.Thread(target=_dd.reconcile_engine_pinning, args=(_n["id"],),
                                 daemon=True).start()
                # Dérive de DIMENSIONNEMENT du moteur (lcores/quota/format/cap de files) : l'env est
                # figé au `docker run`, un réglage changé ensuite ne s'applique qu'au redéploiement.
                # Lecture seule + throttlé 300 s/nœud ; signale, ne coupe rien.
                threading.Thread(target=_dd.reconcile_engine_sizing, args=(_n["id"],),
                                 daemon=True).start()
                _cp.verifier_capacite(_n["id"])   # sur-souscription du pool → alerte à la transition
        except Exception as e:
            log.error(f"Erreur réconciliation pinning moteur: {e}")
        # CONSTAT du placement RÉEL (cf. app/placement.py) : le pendant de tout ce qui précède.
        # `core_pool` CALCULE un placement, `reconcile_engine_pinning` recale la base sur le cpuset
        # posé — mais rien ne regardait ce que le NŒUD en fait. C'est ainsi qu'un moteur pinné sur
        # 16 cœurs dont 15 ISOLÉS a tourné avec 274 threads sur un seul cœur sans qu'aucune alarme
        # ne s'en émeuve (dl360-1, 2026-08-01). En THREAD (sonde = un exec par nœud) et throttlé à
        # 120 s côté module : lecture seule, ne déplace rien, alerte à la transition.
        try:
            from app import placement as _pl
            threading.Thread(target=_pl.verifier_tous, daemon=True).start()
        except Exception as e:
            log.error(f"Erreur constat de placement CPU: {e}")
        # Coût CPU MESURÉ par type (cf. app/cpu_profiles.py) : accumule ce que l'agent remonte déjà
        # dans `containers.cpu_percent`. Sans cette accumulation, le seul chiffre disponible pour
        # dimensionner reste le `resources.cores` du manifeste, que rien ne confronte à la réalité.
        # Auto-throttlé à 60 s, aucune I/O réseau, aucune alerte : il OBSERVE.
        try:
            from app import cpu_profiles as _cprof
            _cprof.echantillonner()
        except Exception as e:
            log.error(f"Erreur profils CPU par type: {e}")
        # Pression CPU (PSI) par nœud ET par conteneur (throttlé ~10 s) — LE détecteur de famine :
        # un cpu_pct « au vert » (moyenne sur 48 cœurs) noie une famine confinée aux 5 cœurs du pool
        # de calcul. PSI mesure le temps passé BLOQUÉ en attente de CPU → alerte par transition.
        try:
            from app import cpu_pressure
            cpu_pressure.sample_all()
        except Exception as e:
            log.error(f"Erreur pression CPU (PSI): {e}")
        # Épisodes de cadence : purge des conteneurs disparus (auto-throttlée à 5 min côté metrics).
        # Un vmid recyclé qui hériterait d'un épisode « déjà alerté » resterait SILENCIEUX sur sa
        # propre sous-cadence — cf. metrics.purger_episodes_cadence.
        try:
            from app import metrics as _mx
            _mx.purger_episodes_cadence()
            # Slots « signal » déclarés instables : lever le silence de ceux qui se sont stabilisés
            # (un silence qui ne se lève jamais est une alarme perdue, pas une alarme calme).
            _mx._signal_purger_mutes()
        except Exception as e:
            log.error(f"Erreur purge épisodes cadence/signal: {e}")
        # Sonde ST 2110 (probe_2110) : moteur d'événements longue durée — poll :8080 des sondes
        # (+ receivers 2110_io surveillés) → seuils → probe_events + alertes (throttlé ~5 s).
        # No-op tant qu'aucune sonde n'est déployée ni aucun signal marqué surveillé.
        try:
            from app import probe_monitor
            probe_monitor.sample_all()
        except Exception as e:
            log.error(f"Erreur sonde 2110 (monitoring): {e}")
        # Occupation des JOURNAUX (throttlé 5 min) : place disque restante, et surtout écart entre
        # ce qu'occupent les journaux et ce que les réglages de rotation PROMETTENT. Personne ne
        # surveillait ça : un `bobistudio.log` de 291 Mo et 3,7 Go d'archives figées au 11 juillet
        # ont vécu des semaines sans un mot, alors qu'un journal non tourné a déjà tué un nœud.
        try:
            logsetup.verifier()
        except Exception as e:
            log.error(f"Erreur contrôle d'occupation des journaux: {e}")
        # Liens RDMA (service mxl-fabrics) : DÉPORTÉ dans un thread — cf. _rdma_reconcile_periodique.
        try:
            _rdma_reconcile_periodique()
        except Exception as e:
            log.error(f"Erreur vérification liens RDMA: {e}")
        # Télémétrie nœud/contrôleur vers les murs qui l'AFFICHENT (%cpu_noeud%, %cpu_orch%…).
        # Un conteneur ne voit que son cgroup : sans cette poussée, ces variables resteraient à
        # « — ». Filtrée sur les murs concernés, donc gratuite pour un parc qui ne s'en sert pas.
        try:
            from app.deploy import pousser_telemetrie
            pousser_telemetrie()
        except Exception as e:
            log.error(f"Erreur poussée de télémétrie : {e}")
        # Tailles de la pyramide : réconciliation PÉRIODIQUE. Jusqu'ici elle n'était déclenchée que
        # par un ÉVÉNEMENT (déploiement, câblage, route manuelle) — une pyramide dont rien ne bouge
        # restait donc inerte indéfiniment, alors que la demande, elle, change sans événement : un
        # mur redimensionne ses fenêtres à chaud, un consommateur arrive sur un autre nœud, un lien
        # de réplication s'établit enfin. Idempotente (no-op si rien ne change) mais elle interroge
        # le :8080 de chaque consommateur → JAMAIS dans cette boucle de 5 s : un thread détaché,
        # au plus un à la fois, toutes les _PYR_RECONCILE_S.
        try:
            _pyramide_reconcile_periodique()
        except Exception as e:
            log.error(f"Erreur réconciliation périodique pyramide: {e}")
        # Sauvegarde quotidienne de la DB (vérif quasi gratuite ; ne tourne qu'1×/jour).
        try:
            backup.maybe_daily_backup()
        except Exception as e:
            log.error(f"Erreur backup quotidien: {e}")
        # Auto-trigger du tissu de composition (gated par réglage `fabric_auto` ; throttlé ~30 s).
        # reconcile_fabric_node court-circuite immédiatement si le réglage est off → quasi gratuit.
        try:
            _fabric_tick[0] += 1
            if _fabric_tick[0] % 6 == 0:
                from app.deploy import reconcile_fabric_node
                from app.database import db_get_nodes
                for _n in db_get_nodes():
                    threading.Thread(target=reconcile_fabric_node, args=(_n["id"],), daemon=True).start()
        except Exception as e:
            log.error(f"Erreur auto-trigger fabric: {e}")
        _watchdog_tick(time.monotonic() - _t0)
        time.sleep(CHECK_INTERVAL)

def _start_emberplus():
    """Démarre Ember+ si activé en settings."""
    try:
        from services import emberplus
        enabled = db_get_setting("emberplus_enabled", False)
        port = int(db_get_setting("emberplus_port", 9000) or 9000)
        if enabled:
            emberplus.start(port)
            log.info(f"Ember+ activé sur le port {port}")
        else:
            log.info("Ember+ désactivé (settings)")
    except Exception as e:
        log.error(f"Erreur démarrage Ember+ : {e}")

def _start_atem():
    """Démarre l'émulateur switcher ATEM si activé en settings."""
    try:
        from services import atem
        enabled = db_get_setting("atem_enabled", False)
        port = int(db_get_setting("atem_port", 9910) or 9910)
        if enabled:
            atem.start(port)
            log.info(f"ATEM émulateur activé sur UDP {port}")
        else:
            log.info("ATEM émulateur désactivé (settings)")
    except Exception as e:
        log.error(f"Erreur démarrage ATEM : {e}")

def _start_skaarhoj():
    """Connecte les panels Skaarhoj activés au boot. Migration depuis l'ancien schéma."""
    try:
        from services import skaarhoj
        from app.database import db_get_setting, db_set_setting
        import uuid as _uuid

        # Migration : ancien schéma scalaire → presets + panels
        old_mapping = db_get_setting("skaarhoj_mapping")
        old_ip      = db_get_setting("skaarhoj_ip", "") or ""
        presets     = db_get_setting("skaarhoj_presets", []) or []
        panels      = db_get_setting("skaarhoj_panels",  []) or []
        if old_mapping and not presets:
            preset_id = str(_uuid.uuid4())
            if isinstance(old_mapping, str):
                import json as _j
                try: old_mapping = _j.loads(old_mapping)
                except Exception: old_mapping = {}
            presets = [{
                "id": preset_id, "name": "Mapping par défaut",
                "mode": old_mapping.get("mode", "mixer"),
                "container_vmid": old_mapping.get("container_vmid"),
                "buttons": old_mapping.get("buttons", [None]*6),
            }]
            db_set_setting("skaarhoj_presets", presets)
            if old_ip and not panels:
                panel_id = str(_uuid.uuid4())
                panels = [{"id": panel_id, "label": "Panel (migré)", "ip": old_ip,
                           "port": int(db_get_setting("skaarhoj_port", 9923) or 9923),
                           "enabled": True, "preset_id": preset_id}]
                db_set_setting("skaarhoj_panels", panels)
            log.info("Skaarhoj: migration ancien schéma → presets/panels effectuée")

        enabled = db_get_setting("skaarhoj_enabled", False)
        if not enabled:
            log.info("Skaarhoj: désactivé (settings)")
            return

        for panel in panels:
            if panel.get("enabled") and panel.get("ip"):
                skaarhoj.connect_panel(panel["id"], panel["ip"],
                                       panel.get("port", 9923), panel.get("preset_id"))
                log.info(f"Skaarhoj: panel {panel.get('label')} → {panel['ip']}:{panel.get('port',9923)}")
    except Exception as e:
        log.error(f"Erreur démarrage Skaarhoj : {e}")

def _start_nmos():
    """Démarre NMOS si activé en settings."""
    try:
        from services import nmos
        enabled = db_get_setting("nmos_enabled", False)
        registry = db_get_setting("nmos_registry_url", "")
        if enabled:
            nmos.start(registry)
            log.info(f"NMOS activé (registry={registry or 'aucun, mode API-only'})")
            # DÉCOUVERTE du registre de l'installation d'accueil (IS-04, DNS-SD). Sans elle, il
            # faut saisir l'URL à la main : un Node qui ne sait pas trouver le registre de la
            # salle où on le branche ne peut pas « apparaître ». Le réglage explicite ci-dessus
            # gagne toujours — la boucle ne cherche que s'il est vide.
            try:
                from services.nmos import decouverte as _dec
                _dec.demarrer()
            except Exception as e:
                log.error(f"NMOS : découverte de registre non démarrée : {e}")
            # Supervision des appareils TIERS : une session IS-12 par pair du registre, dont les
            # changements de statut BCP-008 deviennent des alertes chez nous. Fermé par défaut, et
            # DIFFÉRÉ : les pairs s'enregistrent après notre démarrage, donc ouvrir les sessions
            # tout de suite ne trouverait personne.
            def _tiers():
                import time as _t
                _t.sleep(20)
                try:
                    from services.nmos import supervision_tiers as _sup
                    n = _sup.demarrer()
                    if n:
                        log.info("Supervision BCP-008 des tiers : %d session(s)", n)
                except Exception as _e:
                    log.warning("Supervision BCP-008 des tiers : %s", _e)
            threading.Thread(target=_tiers, daemon=True, name="nmos-tiers-start").start()
            # Transport WebSocket d'IS-07 (tally poussé). Fermé par défaut ; même motif de port
            # dédié qu'IS-12 — Waitress ne sait pas faire l'upgrade WebSocket.
            try:
                from services.nmos import is07 as _i7
                if _i7.demarrer():
                    log.info("IS-07 : transport WebSocket démarré (%s)", _i7.href())
            except Exception as _e:
                log.warning("IS-07 : transport WebSocket non démarré (%s)", _e)
        else:
            log.info("NMOS désactivé (settings)")
    except Exception as e:
        log.error(f"Erreur démarrage NMOS : {e}")

def _start_tsl():
    """Démarre le service TSL 5.0 centralisé (multi-connexions depuis DB)."""
    try:
        from services import tsl
        tsl.start_all()
        log.info("TSL: service démarré (connexions depuis DB)")
    except Exception as e:
        log.error(f"Erreur démarrage TSL : {e}")

def _start_sap():
    """Démarre l'annonce/découverte SAP-SDP (RFC 2974) si activée en réglages.

    Après NMOS : l'annonce diffuse le transportfile IS-05 de nos senders, donc le registre
    doit être bâti. Un démarrage anticipé ne casserait rien (le premier cycle réessaie
    30 s plus tard) mais laisserait une alerte « SDP indisponible » au boot, à chaque boot."""
    try:
        from services import sap
        sap.start_all()
    except Exception as e:
        log.error(f"Erreur démarrage SAP : {e}")

def _start_snmp():
    """Démarre l'agent SNMPv3 si activé en réglages.

    ★ Appelé UNIQUEMENT depuis la branche `ha.is_active()` — comme NMOS et SAP, et pour une
    raison plus dure encore : les deux contrôleurs d'une paire partagent le même engine ID (il
    suit la VIP, il est en base, donc répliqué). Deux agents à l'écoute sur la même adresse se
    présenteraient à l'NMS comme un agent qui redémarre en boucle, invalidant les clés USM
    localisées à chaque bascule. `snmp.start()` re-vérifie `is_active()` de son côté : la garde
    est doublée à dessein, c'est le piège principal du chantier (docs/chantiers/SNMP.md §1.2)."""
    try:
        from services import snmp
        snmp.start()
    except Exception as e:
        log.error(f"Erreur démarrage SNMP : {e}")

def _start_triggers():
    """Démarre le poller des déclencheurs permanents (project_triggers, docs/reference/PROJETS.md §7)."""
    try:
        from app import macros as _macros
        _macros.start_triggers()
        log.info("Déclencheurs : poller démarré (project_triggers)")
    except Exception as e:
        log.error(f"Erreur démarrage déclencheurs : {e}")

def _durcir_waitress_accept():
    """Durcit Waitress contre Errno 24 (EMFILE) / Errno 23 (ENFILE) sur accept() (incident
    2026-07-11) : par défaut, `BaseWSGIServer.handle_accept` logue un traceback complet à
    CHAQUE échec d'accept() SANS AUCUN backoff. Tant que la limite de fds reste franchie, le
    socket d'écoute reste « readable » (select()/poll() retourne immédiatement) → boucle serrée
    qui loggue en tempête (~1,7 Mo/s mesurés, 2,3 Go en 10 min) et rend le service inutilisable —
    c'est la boucle de log qui a tué le service, pas la limite de fds elle-même (déjà relevée à
    65536 par le drop-in systemd LimitNOFILE, mitigation conservée).
    On monkeypatche uniquement `BaseWSGIServer.accept` (n'affecte pas les autres dispatchers
    wasyncore, ex. les channels de connexion) : sur EMFILE/ENFILE on absorbe l'erreur (retour
    None, déjà le contrat de `accept()` pour EWOULDBLOCK/ECONNABORTED/EAGAIN), on dort 200 ms
    pour laisser le temps aux fds de se libérer, et on logue au plus 1 warning / 5 s (pas de
    traceback complet). Les autres OSError (adresse bogus, pair déjà fermé — cf. commentaire
    waitress) gardent le comportement d'origine (log + continue, cas rares/bénins)."""
    import errno as _errno
    from waitress.server import BaseWSGIServer

    _orig_accept = BaseWSGIServer.accept
    _etat = {"dernier_log": 0.0}

    def _accept_avec_backoff(self):
        try:
            return _orig_accept(self)
        except OSError as e:
            if e.errno in (_errno.EMFILE, _errno.ENFILE):
                maintenant = time.monotonic()
                if maintenant - _etat["dernier_log"] > 5.0:
                    log.error(f"accept() saturé (fds épuisés, {e}) — backoff 200 ms appliqué "
                              "(prochain log dans 5 s max, pas de tempête de tracebacks)")
                    _etat["dernier_log"] = maintenant
                time.sleep(0.2)
                return None
            raise

    BaseWSGIServer.accept = _accept_avec_backoff
    log.info("Waitress durci : backoff+throttle de log sur accept() EMFILE/ENFILE (Errno 24/23).")

if __name__ == "__main__":
    init_db()
    # Fuseau horaire du système AVANT tout le reste : le logging Python rend l'heure locale via
    # time.localtime, donc TZ doit être posé avant la première ligne de journal, sinon les premières
    # entrées du démarrage seraient dans un autre fuseau que les suivantes.
    from app import settings as _settings_tz
    _tz_applied = _settings_tz.apply_timezone()
    if _tz_applied:
        log.info("Fuseau horaire du système : %s (réglage `timezone`).", _tz_applied)
    # B3-2a : rôle de contrôle (paire HA). Seul un contrôleur `active` PILOTE (surveillance des
    # nœuds, services NMOS/Ember+/ATEM/TSL/Skaarhoj, sampler PTP, backup quotidien). Un `standby`
    # boote passif — uniquement l'app Flask (UI lecture seule) — pour éviter le split-brain (deux
    # NMOS qui annoncent les mêmes ressources, deux loops qui redémarrent les conteneurs, etc.).
    from app import ha as _ha
    if _ha.is_active():
        # mTLS : la CA interne est auto-initialisée au 1er boot du contrôleur ACTIF (install neuve).
        # Idempotent (no-op si déjà présente). Le standby ne génère PAS de CA (il reçoit TLS_DIR par
        # réplication out-of-band, cf. app/ca.py) — sinon deux CA divergentes casseraient la confiance.
        # Une fois la CA présente, ca_available()=True active le mTLS : conteneurs recréés en HTTPS,
        # nœuds migrés à chaud via /api/nodes/<id>/tls/rotate.
        try:
            from app import ca as _ca
            if _ca.ensure_ca():
                log.warning("mTLS: CA interne auto-initialisée (%s) — plan de contrôle en HTTPS",
                            _ca.paths()["ca_cert"])
        except Exception as e:
            log.error(f"mTLS: auto-init CA échouée : {e}")
        threading.Thread(target=surveillance, daemon=True).start()
        # Backfill au boot (durcissement A) : provisionne les moteurs 2110_io manquants sur les nœuds
        # qui ont déjà un port média 2110 configuré. Best-effort, différé (laisse le 1er tick de
        # surveillance + les agents-nœuds remonter avant de tenter creer/deploy). Sous is_active() → un
        # standby ne provisionne rien.
        def _boot_backfill_engines():
            time.sleep(45)
            try:
                from app import docker_driver as _dd
                _dd.backfill_node_engines()
            except Exception as e:
                log.error(f"Backfill moteurs 2110_io au boot : {e}")
        threading.Thread(target=_boot_backfill_engines, daemon=True).start()
        from app import monitor as _monitor
        _monitor.start_reaper()
        # Fenêtre de maintenance TX (docs/reference/TX_LAYOUTS.md étage 2) : applique les lots planifiés « à HH:MM ».
        from app import tx_maintenance as _txm
        _txm.start_scheduler()
        # Format des sources câblées aux sorties TX (docs/reference/TX_LAYOUTS.md étage 3) : une source qui bascule
        # en exploitation casse l'invariant « source = format annoncé par le slot » → alerte, puis
        # insertion automatique d'un UDC (swap de source : zéro commit, zéro stop de port).
        from app import tx_format_watch as _txfw
        _txfw.start()
        # Relais du relevé 2110 du moteur vers les scopes qui mesurent une source 2110. Le
        # scope ne peut PAS aller le chercher : macvlan ne joint pas son propre hôte, seul
        # l'orchestrateur voit les deux côtés (cf. app/scope_2110.py).
        from app import scope_2110 as _sc2110
        _sc2110.start()
        # Câbles posés en PRÉ-CÂBLAGE (source pas encore en service : l'écart de format n'était
        # qu'une prédiction sur un format DÉCLARÉ) : tranche sur le format RÉEL dès que le flux
        # apparaît. Sans cette repasse, tolérer le pré-câblage serait un échec silencieux.
        from app import wire_format_watch as _wfw
        _wfw.start()
        _start_emberplus()
        _start_atem()
        _start_skaarhoj()
        _start_nmos()
        _start_tsl()
        _start_sap()
        _start_snmp()
        _start_triggers()
        from app import ptp as _ptp
        _ptp.start_sampler()
        from app import node_health as _node_health
        _node_health.load_persisted()   # recharge les agrégats 24 h (sparklines survivent au restart)
        from app import cpu_pressure as _psi
        _psi.load_persisted()           # idem pour la pression CPU (PSI)
        # Contrôle de syntaxe du JS embarqué dans les gabarits. Un seul bloc <script> invalide met
        # HORS SERVICE toutes les fonctions de sa page côté navigateur — vécu : deux backticks dans
        # un commentaire HTML, à l'intérieur d'un littéral de gabarit, et la page Réglages entière
        # affichait tous ses sous-onglets sans plus aucune navigation. Le serveur, lui, rendait la
        # page sans broncher : seule la console du navigateur le disait. En thread (node --check sur
        # ~28 blocs) pour ne pas retarder le boot d'une vérification de confort.
        from app import template_check as _tc
        threading.Thread(target=_tc.verifier_au_demarrage, daemon=True).start()
        # B3-2b : réplication d'état vers le standby (no-op tant que ha_standby_url vide).
        _ha.start_replication()
    else:
        log.warning("Contrôleur en STANDBY : pilotage désactivé (pas de surveillance, services "
                    "NMOS/Ember+/ATEM/TSL ni sampler PTP) — UI en lecture seule, en attente de bascule.")
        # Chien de garde : le standby ne bascule pas tout seul, mais il ne se tait plus quand
        # l'actif tombe (alarme + bandeau). Seul thread de fond autorisé en veille.
        _ha.start_watchdog()
    # VIP de management (keepalived) : la priorité VRRP est dérivée du rôle, or le rôle peut avoir
    # été changé À LA MAIN puis suivi d'un simple redémarrage (chemin documenté dans HA.md) — sans
    # ce re-rendu au boot, l'adresse resterait sur l'ancien actif. No-op si la VIP est désactivée.
    try:
        from app import vip as _vip, settings as _st
        if _st.get("vip_enabled"):
            threading.Thread(target=_vip.refresh_for_role, daemon=True).start()
    except Exception as e:
        log.error(f"VIP : re-rendu au boot impossible : {e}")
    # Si une mise à jour était en attente de validation, l'app remonte → on la valide.
    try:
        from app import updater as _updater
        _updater.confirm_boot_ok()
    except Exception as e:
        log.error(f"Erreur confirmation update au boot : {e}")
    # Purge des ISO d'enrôlement orphelines (token consommé) — cf. app/node_iso (service iLO).
    try:
        from app import node_iso as _niso
        from app.database import db_get_nodes as _dgn
        _niso.purge_orphans([n.get("enroll_token") for n in _dgn() if n.get("enroll_token")])
    except Exception as e:
        log.error(f"Erreur purge ISO orphelines au boot : {e}")
    log.info("Orchestrateur démarré sur http://x.x.x.x:5000")
    # Serveur PXE dédié (HTTP/1.1 keep-alive) : le firmware UEFI HTTP Boot exige de réutiliser la
    # connexion entre HEAD et GET ; le Werkzeug de l'orchestrateur force « Connection: close » (donc
    # inutile d'y toucher) → on sert le boot réseau sur un port à part. Non bloquant si échec.
    try:
        from app import pxe_server
        pxe_server.start()
    except Exception as e:
        log.error(f"Serveur PXE keep-alive non démarré : {e}")
    # Serveur WSGI de production : Waitress (multi-thread, robuste) au lieu du serveur de dev
    # Werkzeug (app.run). Mono-processus → les threads de fond démarrés ci-dessus (surveillance,
    # samplers, services NMOS/TSL/Ember/ATEM, reaper, réplication HA) restent uniques et intacts.
    # Fallback sur app.run si Waitress absent (env sans dépendance) pour ne pas casser le boot.
    try:
        from waitress import serve
        _durcir_waitress_accept()
        # threads : équivalent du threaded=True (iLO tient la connexion CD virtuel — lecture Range
        # de l'ISO — pendant tout l'install ~10 min ; il faut assez de threads pour ne pas bloquer).
        serve(app, host="0.0.0.0", port=5000, threads=16)
    except ImportError:
        log.warning("Waitress absent — repli sur le serveur de dev Werkzeug (app.run). "
                    "Installer waitress pour la production (pip install waitress).")
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
