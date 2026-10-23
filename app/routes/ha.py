# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Haute disponibilité (paire de contrôleurs) — rôle, réplication, promote/demote.

Le garde-fou standby (`_ha_readonly_guard`, whitelist /api/ha/ + /api/update/) et
l'injecteur de rôle pour les templates (`inject_role`) restent dans app/routes/
__init__.py : ce sont des hooks globaux attachés directement au blueprint `bp`
(before_request / context_processor), pas des routes — ils doivent être visibles
dès l'exécution du module racine, indépendamment de l'ordre d'import des domaines."""

import logging

from flask import jsonify, request

from . import bp
from ..auth import require_perm, require_login

log = logging.getLogger(__name__)


@bp.route("/api/ha/role", methods=["GET"])
@require_login
def ha_role_get():
    from .. import ha
    return jsonify({"role": ha.role(), "roles": list(ha.ROLES)})

@bp.route("/api/ha/role", methods=["POST"])
@require_perm("settings.edit")
def ha_role_set():
    """Fixe le rôle de CE contrôleur (active|standby). Prend effet au prochain redémarrage du
    service (la bascule à chaud = promote, B3-2c). Whitelisté côté garde-fou standby."""
    from .. import ha
    from ..database import db_set_setting, db_add_alert
    r = str((request.json or {}).get("role") or "").strip().lower()
    if r not in ha.ROLES:
        return jsonify({"ok": False, "error": f"rôle inconnu : {r}"}), 400
    db_set_setting("control_role", r)
    db_add_alert("alert.node.ha_role_change", "warning", kind="node", params={"r": r})
    return jsonify({"ok": True, "role": r})


def _pair_mtls():
    """L'URI de contrôleur du pair, si la requête est arrivée par l'écoute mTLS HA ET que son
    certificat porte le marqueur — posée par `ha_tls.GardeHA`, et par elle seule. None sinon.

    Remplace le secret partagé en en-tête (`X-MXL-Update-Token`), retiré le 2026-10-10 : il
    voyageait en clair à chaque requête et se rejouait."""
    from ..ha_tls import CLE_ENVIRON
    return request.environ.get(CLE_ENVIRON)


_REFUS_CLAIR = ("réplication en clair retirée : elle passe désormais par l'écoute mTLS des "
                "contrôleurs appairés (Réglages → Haute disponibilité → Appairer)")


@bp.route("/api/ha/peer", methods=["GET"])
def ha_peer():
    """Témoin de vie pour le chien de garde de l'AUTRE contrôleur. Volontairement minimal — si
    cette route ne répond pas, c'est que l'orchestrateur est à terre, ce qui est l'information."""
    if not _pair_mtls():
        return jsonify({"ok": False, "error": _REFUS_CLAIR}), 403
    from .. import ha
    return jsonify({"ok": True, "role": ha.role()})


@bp.route("/api/ha/replicate", methods=["POST"])
def ha_replicate():
    """Réception (côté STANDBY) d'un snapshot SQLite poussé par l'actif, en mTLS. Stagé, pas
    appliqué à chaud. Whitelisté par le garde standby (/api/ha/)."""
    if not _pair_mtls():
        return jsonify({"ok": False, "error": _REFUS_CLAIR}), 403
    from .. import ha
    raw = request.get_data(cache=False)
    if not raw:
        return jsonify({"ok": False, "error": "corps vide"}), 400
    try:
        meta = ha.store_replica(raw, source=request.remote_addr or "?")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "bytes": meta.get("bytes"), "at": meta.get("at")})


@bp.route("/api/ha/replicate_lv2", methods=["POST"])
def ha_replicate_lv2():
    """Réception (côté STANDBY) de la bibliothèque de plugins LV2 de l'actif (services/audio/lv2_plugins.mirror), en mTLS.
    Appliquée tout de suite : ce ne sont que des fichiers, que le standby ne distribue pas tant qu'il n'est pas
    promu (la copie vers les nœuds est un mutateur, refusé en standby)."""
    if not _pair_mtls():
        return jsonify({"ok": False, "error": _REFUS_CLAIR}), 403
    try:
        from services.audio import lv2_plugins
    except ImportError:          # audio service absent on this standby: refused, the active one logs the code
        return jsonify({"ok": False, "code": "audio_service_absent", "error": "audio_service_absent"}), 501
    raw = request.get_data(cache=False)
    try:
        names = lv2_plugins.mirror(raw)
    except lv2_plugins.Lv2Error as e:
        return jsonify({"ok": False, "code": e.code, "params": e.params, "error": e.code}), 422
    return jsonify({"ok": True, "bundles": names, "digest": lv2_plugins.digest()})


# ─── Appairage (cf. app/ha_appairage.py) ──────────────────────────────────────

def _etat_appairage():
    from .. import ca, ha_appairage, ha_tls
    e = {"pret": ha_tls.pret(), "port_ha": ha_tls.port_ha(),
         "code": ha_appairage.etat_actif()}
    try:
        with open(ca.paths()["ca_cert"], "rb") as f:
            e["empreinte_ca"] = ha_appairage.empreinte_ca(f.read())
    except Exception:
        e["empreinte_ca"] = None
    return e


@bp.route("/api/ha/pairing", methods=["GET"])
@require_perm("settings.edit")
def ha_pairing_state():
    return jsonify(_etat_appairage())


@bp.route("/api/ha/pairing/start", methods=["POST"])
@require_perm("settings.edit")
def ha_pairing_start():
    """Côté ACTIF : ouvre un appairage et rend le code À AFFICHER. Il n'est stocké qu'en mémoire,
    brûlé au premier succès ou après 5 preuves fausses, et expire en 10 minutes."""
    from flask import current_app
    from .. import ha_appairage, ha_tls
    try:
        code, expire = ha_appairage.demarrer()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    # Le marqueur vient peut-être d'être posé sur notre certificat : l'écoute peut démarrer.
    ha_tls.demarrer(current_app._get_current_object())
    e = _etat_appairage()
    e.update({"ok": True, "code_affiche": code, "expire": expire})
    return jsonify(e)


@bp.route("/api/ha/pairing/cancel", methods=["POST"])
@require_perm("settings.edit")
def ha_pairing_cancel():
    from .. import ha_appairage
    ha_appairage.annuler()
    return jsonify({"ok": True})


@bp.route("/api/ha/pairing/claim", methods=["POST"])
def ha_pairing_claim():
    """Côté ACTIF, PUBLIQUE et en HTTP : le standby n'a pas encore la CA, il ne peut pas parler
    mTLS. Rien d'utilisable n'y circule en clair — un CSR, un aléa, des preuves, un paquet chiffré
    sous une clé dérivée du code (cf. app/ha_appairage.py). La réponse ne dit pas POURQUOI une
    preuve est fausse : elle n'a pas à renseigner qui cherche."""
    from .. import ha_appairage
    try:
        rep = ha_appairage.traiter_demande(request.get_json(silent=True) or {},
                                           request.remote_addr)
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    except ValueError:
        return jsonify({"ok": False, "error": "appairage refusé"}), 403
    except Exception as e:
        return jsonify({"ok": False, "error": "erreur côté actif : %s" % e}), 500
    return jsonify(rep)


@bp.route("/api/ha/pairing/join", methods=["POST"])
@require_perm("settings.edit")
def ha_pairing_join():
    """Côté STANDBY : rejoindre l'actif avec le code affiché là-bas. Remplace l'autorité locale
    (l'ancienne est gardée), désigne l'actif comme pair, et relance l'écoute mTLS."""
    from flask import current_app
    from .. import ha_appairage, ha_tls
    from ..database import db_set_setting
    d = request.json or {}
    try:
        empreinte, url = ha_appairage.rejoindre(d.get("actif"), d.get("code"))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    db_set_setting("ha_standby_url", url)
    ha_tls.redemarrer(current_app._get_current_object())
    # L'écoute HTTPS (mode web tls) servait encore le certificat de l'ANCIENNE autorité : la
    # recharger. `synchroniser_certificat` y ajoute l'IP virtuelle si le réglage local la connaît ;
    # sinon (certificat reçu déjà complet) on recharge quand même.
    from .. import vip, web_mode
    try:
        if not vip.synchroniser_certificat():
            web_mode.recharger_certificat()
    except Exception as e:
        log.error("appairage : écoute HTTPS non rechargée (%s)", e)
    e = _etat_appairage()
    e.update({"ok": True, "empreinte_ca": empreinte, "actif": url})
    return jsonify(e)


@bp.route("/api/ha/replicate-now", methods=["POST"])
@require_perm("settings.edit")
def ha_replicate_now():
    """Déclenche un push immédiat (côté actif)."""
    from .. import ha
    ok, msg = ha.push_replica()
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 400)


@bp.route("/api/ha/status", methods=["GET"])
@require_login
def ha_status():
    from .. import ha
    return jsonify(ha.replication_status())


# ─── VIP de management (keepalived) ───────────────────────────────────────────
# Sous /api/ha/ → whitelistées par le garde standby : un contrôleur en veille DOIT pouvoir
# configurer sa propre VIP (sinon la moitié de la paire reste non configurable).

@bp.route("/api/ha/vip", methods=["GET"])
@require_perm("settings.edit")
def ha_vip_get():
    """État + aperçu de la conf qui SERAIT écrite (jamais d'écriture sur un GET)."""
    from .. import vip
    st = vip.status()
    st["preview"] = vip.render_config() if st["address"] else ""
    st["auth_pass"] = vip.config_values()["auth_pass"]   # à recopier sur l'autre contrôleur
    return jsonify(st)


@bp.route("/api/ha/vip", methods=["POST"])
@require_perm("settings.edit")
def ha_vip_set():
    """Enregistre les réglages VIP puis applique (écrit la conf + (re)démarre keepalived).
    `install: true` installe le paquet au passage (action explicite de l'utilisateur)."""
    from .. import vip
    from ..database import db_set_setting
    data = request.json or {}
    for key, cast in (("vip_enabled", bool), ("vip_address", str), ("vip_interface", str),
                      ("vip_vrid", int), ("vip_auth_pass", str),
                      ("vip_priority_active", int), ("vip_priority_standby", int)):
        if key in data:
            try:
                db_set_setting(key, cast(data[key]) if cast is not str else str(data[key]).strip())
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"valeur invalide pour {key}"}), 400
    if "vip_address" in data:
        # Les navigateurs qui visent la VIP comparent son adresse au certificat (mode web tls).
        # Indépendant de keepalived : une VIP peut aussi être posée à la main (cf. HA.md).
        try:
            vip.synchroniser_certificat()
        except Exception as e:
            log.error("VIP : certificat du contrôleur non mis à jour (%s)", e)
    if not data.get("apply", True):
        return jsonify({"ok": True, "msg": "réglages enregistrés (non appliqués)"})
    ok, msg = vip.apply(install_pkg=bool(data.get("install")))
    if ok is None:      # VIP désactivée : enregistrer sans appliquer n'est pas un échec
        return jsonify({"ok": True, "msg": msg})
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 400)


@bp.route("/api/ha/vip/remove", methods=["POST"])
@require_perm("settings.edit")
def ha_vip_remove():
    from .. import vip
    from ..database import db_set_setting
    db_set_setting("vip_enabled", False)
    ok, msg = vip.remove()
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 400)


@bp.route("/api/ha/promote", methods=["POST"])
@require_perm("settings.edit")
def ha_promote():
    """Bascule STANDBY → ACTIF : applique le replica stagé + redémarre. Whitelistée par le garde
    standby (/api/ha/) → doit fonctionner SUR un standby."""
    from .. import ha
    ok, msg = ha.promote()
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 400)


@bp.route("/api/ha/demote", methods=["POST"])
@require_perm("settings.edit")
def ha_demote():
    """Bascule ACTIF → VEILLE : flip du rôle + redémarre (DB intouchée)."""
    from .. import ha
    ok, msg = ha.demote()
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 400)
