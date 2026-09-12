# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Réglages globaux + par-nœud (get/set), schéma enrichi (page Réglages générique),
logo entreprise, stats VMID/IP, occupation et purge des journaux."""

import logging
import os
import time

from flask import jsonify, request

from . import bp
from .. import config
from ..auth import require_login, require_perm
from ..database import db_add_alert

# svg volontairement EXCLU : un SVG uploadé est servi tel quel depuis /static et peut
# embarquer du JS (<script>) → XSS stockée. On n'accepte que des formats raster inertes.
log = logging.getLogger(__name__)

_LOGO_EXTS = ("png", "jpg", "jpeg", "webp", "gif")
_UPLOAD_DIR = config.UPLOADS_DIR


@bp.route("/api/settings", methods=["GET"])
@require_login
def api_get_settings():
    # NB : reste @require_login (pas @require_perm("settings.edit")) — des pages non-admin
    # consomment cette API pour des réglages légitimes (formats vidéo dans static/scripts.js
    # et static/io2110.js, branding/thème). La protection NON-NÉGOCIABLE est le filtrage des
    # secrets via st.public() : la clé de signature de session et les tokens ne sortent JAMAIS.
    from .. import settings as st
    return jsonify(st.public())

@bp.route("/api/settings", methods=["POST"])
@require_perm("settings.edit")
def api_set_settings():
    from .. import settings as st
    data = request.json or {}
    n, ignored = st.update_bulk(data)
    # Rotation du journal : APPLIQUÉE À CHAUD. Elle ne l'était qu'au (re)démarrage — donc un
    # opérateur qui voit son journal grossir pouvait poser le bon réglage, lire « enregistré »,
    # et regarder le fichier continuer de grimper sans comprendre.
    if any(k.startswith("log_") for k in data):
        try:
            from .. import logsetup
            logsetup.appliquer_reglages()
        except Exception:
            log.exception("application à chaud des réglages de journal")
    # `ignored` = clés absentes de settings.DEFAULTS. Avant, elles étaient jetées en silence et la
    # route répondait ok : un champ d'UI oublié dans DEFAULTS semblait s'enregistrer sans effet.
    return jsonify({"status": "ok", "updated": n, "ignored": ignored})


@bp.route("/api/settings/schema", methods=["GET"])
@require_login
def api_settings_schema():
    """Schéma enrichi des réglages déclarés par les services (rendu générique de la page Réglages)."""
    from .. import core_plugins
    return jsonify(core_plugins.settings_schema())


@bp.route("/api/settings/node/<int:node_id>", methods=["GET"])
@require_login
def api_get_node_settings(node_id):
    """Overrides d'un nœud + valeur EFFECTIVE résolue (override > global > défaut) par clé connue.
    Portée « global + override par nœud » de la refonte Réglages."""
    from .. import settings as st
    from ..database import db_get_node_settings
    # Expurgé des clés sensibles (même contrat que /api/settings) : ce chemin renvoie les
    # valeurs effectives résolues, secrets inclus si on ne filtre pas.
    overrides = {k: v for k, v in (db_get_node_settings(node_id) or {}).items()
                 if not st._is_secret_key(k)}
    defaults = st.public()
    effective = {k: st.setting_for(k, node_id) for k in defaults}
    return jsonify({"node_id": node_id, "overrides": overrides, "effective": effective})


@bp.route("/api/settings/node/<int:node_id>", methods=["POST"])
@require_perm("settings.edit")
def api_set_node_settings(node_id):
    """Pose/retire des overrides par-nœud. {key: value} pose l'override ; value null OU clé dans
    `clear:[…]` retire l'override (le réglage ré-hérite du global). Seules les clés connues."""
    from .. import settings as st
    from ..database import db_set_node_setting, db_delete_node_setting
    data = request.json or {}
    known = set(st.all().keys())
    clear = set(data.get("clear") or [])
    n = 0
    for k in clear:
        if k in known:
            db_delete_node_setting(node_id, k); n += 1
    for k, v in (data.get("set") or {}).items():
        if k in known:
            db_set_node_setting(node_id, k, v); n += 1
    return jsonify({"status": "ok", "updated": n})

@bp.route("/api/settings/logo", methods=["POST"])
@require_perm("settings.edit")
def api_upload_logo():
    """Upload du logo entreprise → static/uploads/brand-logo.<ext>, persisté dans
    brand_logo_url (avec cache-buster ?v=). Remplace l'ancien logo."""
    import glob
    from .. import settings as st
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify({"error": "fichier 'logo' manquant"}), 400
    ext = os.path.splitext(f.filename)[1].lower().lstrip(".")
    if ext not in _LOGO_EXTS:
        return jsonify({"error": f"format non supporté ({', '.join(_LOGO_EXTS)})"}), 400
    f.seek(0, os.SEEK_END); size = f.tell(); f.seek(0)
    if size > 2 * 1024 * 1024:
        return jsonify({"error": "image trop lourde (max 2 Mo)"}), 400
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(_UPLOAD_DIR, "brand-logo.*")):
        try: os.remove(old)
        except OSError: pass
    f.save(os.path.join(_UPLOAD_DIR, f"brand-logo.{ext}"))
    url = f"/static/uploads/brand-logo.{ext}?v={int(time.time())}"
    st.set("brand_logo_url", url)
    return jsonify({"status": "ok", "url": url})

@bp.route("/api/settings/logo", methods=["DELETE"])
@require_perm("settings.edit")
def api_delete_logo():
    import glob
    from .. import settings as st
    for old in glob.glob(os.path.join(_UPLOAD_DIR, "brand-logo.*")):
        try: os.remove(old)
        except OSError: pass
    st.set("brand_logo_url", "")
    return jsonify({"status": "ok"})

@bp.route("/api/settings/stats", methods=["GET"])
@require_login
def api_settings_stats():
    from ..allocations import vmid_stats, ip_stats
    return jsonify({"vmid": vmid_stats(), "ip": ip_stats()})

@bp.route("/api/settings/logs", methods=["GET"])
@require_perm("settings.edit")
def api_logs_info():
    """Occupation disque du journal (actif + archives + legacy), CONFRONTÉE au plafond que les
    réglages de rotation promettent, et à la place restante sur la partition. Les clés `files` et
    `total_bytes` sont conservées telles quelles (l'onglet Réglages les lit déjà)."""
    from .. import logsetup
    e = logsetup.etat()
    return jsonify({"ok": True, "files": e["fichiers"], "total_bytes": e["total_bytes"],
                    "plafond_bytes": e["plafond_bytes"], "depasse": e["depasse"],
                    "rotation": e["rotation"], "max_mb": e["max_mb"], "backups": e["backups"],
                    "rotate_days": e["rotate_days"],
                    "libre_bytes": e["libre_bytes"], "capacite_bytes": e["capacite_bytes"]})

@bp.route("/api/settings/logs/purge", methods=["POST"])
@require_perm("settings.edit")
def api_logs_purge():
    """Vide les journaux maintenant : supprime les archives rotées, tronque le journal actif (le
    handler est en mode append → réécrit à 0, pas de fichier creux), et supprime le log legacy
    `orchestrateur.log` s'il traîne. Retourne l'espace libéré."""
    import glob, os
    from ..config import LOG_PATH
    freed = 0
    for f in glob.glob(LOG_PATH + ".*"):
        try:
            freed += os.path.getsize(f); os.unlink(f)
        except OSError:
            pass
    try:
        freed += os.path.getsize(LOG_PATH)
        open(LOG_PATH, "w").close()
    except OSError:
        pass
    legacy = os.path.join(os.path.dirname(LOG_PATH), "orchestrateur.log")
    if os.path.isfile(legacy):
        try:
            freed += os.path.getsize(legacy); os.unlink(legacy)
        except OSError:
            pass
    db_add_alert("alert.ui.journaux_vides", "info", kind="disk",
                 params={"mo": freed // (1024*1024)})
    return jsonify({"ok": True, "freed_bytes": freed})


# ─── Sortie PUSH du journal d'alertes (canal webhook) ────────────────────────
# La logique vit désormais dans le service `services/alerting/` (couche de décision unique + N
# canaux) ; ces deux routes restent en place parce que l'onglet Réglages → Alarmes les appelle
# déjà — elles ne font plus que déléguer au canal `webhook`.
@bp.route("/api/settings/alert_webhook/state", methods=["GET"])
@require_perm("settings.edit")
def api_alert_webhook_state():
    """État d'émission du webhook d'alerte : dernier envoi OK, dernier échec + sa cause, compteurs.
    Un webhook cassé DOIT se voir sans fouiller les logs (anti-patron « échec silencieux »)."""
    from services import alerting
    st = alerting.state()
    canal = next((c for c in st.get("channels") or [] if c.get("id") == "webhook"), {})
    return jsonify({"ok": True, **canal, "queued": st.get("queued", 0),
                    "dropped": st.get("dropped", 0), "last_url": canal.get("last_target")})


@bp.route("/api/settings/alert_webhook/test", methods=["POST"])
@require_perm("settings.edit")
def api_alert_webhook_test():
    """Envoi de test SYNCHRONE (bouton des Réglages) : vérifier la configuration sans attendre une
    vraie panne. Utilise l'URL du corps si fournie (permet de tester AVANT d'enregistrer)."""
    from services import alerting
    url = ((request.json or {}).get("url") or "").strip() or None
    ok, detail = alerting.envoyer_test("webhook", **({"url": url} if url else {}))
    return jsonify({"ok": ok, "detail": detail})


def _sait_trancher(type_plugin):
    """Vrai si CE type de plugin expose `slice_mode` dans son `config_schema`.

    Sert à distinguer « étage qui suit le global » (à afficher) de « étage qui ne sait pas
    trancher » (hors sujet). Sans ce filtre on lister­ait des conteneurs pour lesquels la question
    ne se pose pas, ce qui brouillerait la vue autant que l'inverse.

    ⚠ `config_schema` est une LISTE d'entrées `{name, …}`, PAS un dict indexé par nom : un
    `"slice_mode" in schema` renvoie False en silence et l'étage disparaît de la vue — soit
    exactement le défaut qu'on corrige. Vérifié sur multiview/avsync/pyramide."""
    try:
        from .. import plugins as _pl
        cs = (_pl.get(type_plugin) or {}).get("config_schema") or []
        if isinstance(cs, dict):                       # tolère l'autre forme si elle réapparaît
            return "slice_mode" in cs
        return any(isinstance(e, dict) and (e.get("name") or e.get("key")) == "slice_mode"
                   for e in cs)
    except Exception:
        return False


@bp.route("/api/slice/etat", methods=["GET"])
@require_login
def api_slice_etat():
    """État EFFECTIF du mode tranche, étage par étage — pas l'intention, le réel.

    POURQUOI CETTE ROUTE EXISTE. Le réglage `slice_mode_global` affichait « tranche activée »
    alors qu'UN SEUL étage sur quatre l'était réellement, et rien ne permettait de s'en
    apercevoir : le moteur 2110 avait son propre interrupteur (`mtl_engine_env.SLICE_MODE`, hors
    du champ du global — corrigé depuis), et un `slice_mode` explicite persisté sur un conteneur
    prime sur le global sans le dire. Un opérateur lisait donc une intention, jamais l'état.

    C'est le même piège qui a produit plusieurs diagnostics faux le 2026-08-11 : croire la
    configuration au lieu de vérifier ce qui tourne. La règle qu'on en tire vaut au-delà de la
    tranche — cf. la note de mémoire sur l'âge absolu : n'affiche JAMAIS une intention là où
    l'opérateur croira lire un état.

    CE QUE CETTE ROUTE REND, EXACTEMENT : l'état **configuré** — c'est-à-dire ce qu'un
    (re)déploiement appliquerait, global et surcharges combinés. Elle NE lit PAS les conteneurs en
    cours d'exécution : un étage déployé avant un changement de réglage affichera donc la nouvelle
    valeur alors qu'il tourne encore avec l'ancienne. Le mode tranche du moteur, en particulier,
    n'est lu qu'au `docker run` — il exige une RECRÉATION, pas un redéploiement.

    C'est une limite assumée et non un oubli : interroger chaque conteneur coûterait un exec par
    étage à chaque appel. Le jour où l'écart importe, la lecture vive existe déjà côté plugins
    (métrique `slice_mode` sur :8080) et côté moteur (`docker inspect` de l'env).
    """
    from .. import settings as st
    from ..database import get_db
    import json as _json

    out = {"global": bool(st.get("slice_mode_global")), "etages": []}
    try:
        _env = _json.loads(st.get("mtl_engine_env") or "{}")
    except Exception:
        _env = {}
    _forcage = str((_env or {}).get("SLICE_MODE", "")).strip()
    db = get_db()
    for r in db.execute("SELECT vmid, hostname, deploy_config FROM containers "
                        "ORDER BY vmid").fetchall():
        try:
            cfg = _json.loads(r["deploy_config"] or "{}")
        except Exception:
            continue
        typ, par = cfg.get("type"), (cfg.get("params") or {})
        if typ == "2110_io":
            # Le moteur ne porte pas `slice_mode` : son mode vient du global, qu'une clé
            # explicite dans `mtl_engine_env` peut encore surcharger (échappatoire de banc).
            conf = (_forcage in ("1", "true", "yes", "on")) if _forcage else out["global"]
            out["etages"].append({"vmid": r["vmid"], "hostname": r["hostname"], "type": typ,
                                  "configure": conf, "surcharge": bool(_forcage),
                                  "porte_rx_tx": True})
        elif "slice_mode" in par:
            out["etages"].append({"vmid": r["vmid"], "hostname": r["hostname"], "type": typ,
                                  "configure": bool(par.get("slice_mode")),
                                  "surcharge": True, "porte_rx_tx": False})
        elif _sait_trancher(typ):
            # SANS surcharge : l'étage SUIT le global. Il doit apparaître quand même — une vue
            # d'état qui n'affiche que les exceptions laisse croire que le reste n'existe pas,
            # et c'est exactement l'angle mort qu'on corrige ici.
            out["etages"].append({"vmid": r["vmid"], "hostname": r["hostname"], "type": typ,
                                  "configure": out["global"], "surcharge": False,
                                  "porte_rx_tx": False})
    out["incoherent"] = [e["hostname"] for e in out["etages"]
                         if bool(e["configure"]) != out["global"]]
    return jsonify(out)
