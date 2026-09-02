# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime

log = logging.getLogger(__name__)

from flask import (Blueprint, jsonify, request, render_template, send_file,
                   redirect, url_for, abort, Response, stream_with_context, session)
from ..database import (db_get_containers, db_get_container, db_get_alerts, db_add_alert, db_upsert_container,
                      db_delete_container, db_get_projects, db_save_project,
                      db_delete_project, db_import_project, db_get_project,
                      db_save_layout, db_get_layouts,
                      db_delete_layout, db_get_user, db_list_users,
                      db_create_user, db_update_user, db_delete_user,
                      db_count_users, db_get_user_by_id,
                      db_cable_snapshot_save, db_cable_snapshots_list,
                      db_cable_snapshot_get, db_cable_snapshot_delete,
                      db_cable_layout_save, db_cable_layouts_list,
                      db_cable_layout_get, db_cable_layout_delete,
                      db_create_share_link, db_get_share_link,
                      db_list_share_links, db_delete_share_link,
                      db_get_nodes, db_get_node, db_add_node, db_update_node, db_delete_node,
                      db_get_node_by_enroll_token,
                      db_get_node_interfaces, db_upsert_node_interface, db_delete_node_interface)
from ..containers import (detruire_container, redemarrer_container,
                        update_resources)
from ..deploy import deployer_script
from ..vmlocks import verrou_vmid
from .. import hostnames
from ..projects import restaurer_projet, detruire_containers_projet, planifier_restore
# B1a/B1b-2 : résolveurs d'hôte par-nœud (remplacent le réglage global proxmox_host pour les host-ops).
from ..addressing import primary_host as _primary_host, host_for_vmid as _host_for_vmid, \
    node_host as _node_host, \
    controller_ipv4s as _controller_ipv4s, controller_on_subnet as _controller_on_subnet, \
    controller_route_to as _controller_route_to
# Helpers transversaux (utilisés dans tout ce fichier ET par node_network.py/plugin_routes/
# split.py) : extraits dans shared.py, sans dépendance sur `bp` → importables ici sans risque
# de circularité, quel que soit l'endroit du fichier.
from .shared import _load_dc, _mixer_proxy, _ptp_apply_core, _mtl_total_queues, _fetch_host_nics


def _req_node_id():
    """node_id de la requête (query-arg ou body JSON), int ou None."""
    nid = request.args.get("node_id")
    if nid in (None, "") and request.is_json:
        nid = (request.get_json(silent=True) or {}).get("node_id")
    try:
        return int(nid) if nid not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _eff_node_id():
    """node_id effectif d'une op host-prep : celui de la requête, sinon le nœud unique (transition
    1 nœud), sinon None (dev local 0 nœud / multi-nœud sans sélection)."""
    nid = _req_node_id()
    if nid is not None:
        return nid
    from ..database import db_get_nodes
    nodes = db_get_nodes() or []
    return nodes[0]["id"] if len(nodes) == 1 else None


def _req_host():
    """Hôte SSH du nœud de la requête (repli nœud unique). None si indéterminable."""
    return _node_host(_eff_node_id()) or _primary_host()
from ..auth import (require_login, require_perm, has_perm, current_user, login_user,
                  logout_user, verify_password, hash_password,
                  ROLES, ROLE_LABELS, PERMISSIONS,
                  check_vmid_access, scoped_project_ids, require_global_access,
                  vmid_project_ids)

bp = Blueprint("routes", __name__)

# B3-2a : garde-fou « standby = passif ». Un contrôleur en STANDBY sert l'UI en lecture mais ne doit
# JAMAIS muter l'état (anti-split-brain : il n'a pas la main sur les nœuds). On refuse donc tout
# mutateur /api/* (POST/PATCH/PUT/DELETE) sauf la liste blanche du pilotage HA : /api/ha/*
# (réplication + promote à venir) et /api/update/* (sync code entre instances). Le login (/login)
# et /setup ne sont pas sous /api/ → déjà autorisés. Les GET/HEAD (UI lecture) passent toujours.
# Défaut active → aucun effet.
_HA_STANDBY_ALLOW = ("/api/ha/", "/api/update/")


@bp.route("/api/ping", methods=["GET", "HEAD"])
def api_ping():
    """Sonde de connexion du navigateur. VOLONTAIREMENT publique et sans état : elle répond à
    « le serveur est-il joignable ? », pas « qui es-tu ? ». Exiger une session en ferait un test
    de DEUX choses à la fois, et une session expirée se lirait « connexion perdue » — deux pannes
    différentes qui appellent deux gestes différents (recharger vs se reconnecter).

    Ni DB, ni réglages, ni verrou : elle est appelée en boucle quand la liaison est coupée, et
    doit rester le moins cher possible. `no-store` parce qu'une sonde servie par un cache
    répondrait « ça va » depuis un navigateur qui ne parle plus à personne."""
    r = jsonify({"ok": True})
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return r


@bp.before_request
def _ha_readonly_guard():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    path = request.path or ""
    if not path.startswith("/api/"):
        return None
    if any(path.startswith(p) for p in _HA_STANDBY_ALLOW):
        return None
    from .. import ha
    if ha.is_standby():
        return jsonify({"ok": False,
                        "error": "contrôleur en STANDBY (lecture seule)"}), 409
    return None


# Chantier 1 : les utilisateurs `interface=projets` n'ont pas accès aux PAGES techniques
# — liste blanche de préfixes (les API ont leur propre scoping, cf. auth.check_vmid_access).
# Approche liste blanche : toute page technique future est couverte par défaut.
_PROJECT_UI_ALLOW = ("/workspaces", "/workspace/", "/login", "/logout", "/static/",
                     "/api/", "/aide", "/setup", "/watch")

@bp.before_request
def _poser_auteur_ecriture():
    """Auteur des écritures de configuration faites PENDANT cette requête (cf. edit_lock :
    la garde de révision ne déclare un conflit que si la dernière écriture vient d'un autre).
    Lu de la session, sans requête DB — ce hook tourne sur TOUS les appels, y compris les polls
    de 5 secondes."""
    from ..edit_lock import poser_auteur
    poser_auteur(session.get("user_id"))
    return None


@bp.before_request
def _project_interface_guard():
    if request.method not in ("GET", "HEAD"):
        return None
    path = request.path or ""
    if any(path == p.rstrip("/") or path.startswith(p) for p in _PROJECT_UI_ALLOW):
        return None
    u = current_user()
    if u and u.get("interface") == "projets":
        return redirect(url_for("routes.workspaces_page"))
    return None


@bp.app_context_processor
def inject_role():
    """Expose le rôle de contrôle (active|standby) à tous les templates → badge HA global
    dans layout.html. Tolérant (jamais d'exception au rendu)."""
    try:
        from .. import ha
        return {"control_role": ha.role()}
    except Exception:
        return {"control_role": "active"}

def _attach_projects(containers):
    """Enrichit chaque container avec la liste des projets qui le référencent
    (via snapshot) et le projet direct (colonne project_id)."""
    projets = db_get_projects()
    proj_by_id = {p["id"]: p for p in projets}
    by_vmid = {}
    for p in projets:
        for snap in p.get("snapshot") or []:
            by_vmid.setdefault(snap.get("vmid"), []).append(
                {"id": p["id"], "name": p["name"]})
    for c in containers:
        c["projects"] = by_vmid.get(c["vmid"], [])
        pid = c.get("project_id")
        proj = proj_by_id.get(pid) if pid else None
        c["project"] = {"id": proj["id"], "name": proj["name"], "media_path": proj.get("media_path")} if proj else None
    return containers


def _attach_fabric(containers):
    """Enrichit chaque container avec son rôle dans le tissu de composition :
    fabric_role ('shard'|'proxy'|'logical') + fabric_parent (1er multiview parent, pour le repli UI).
    Permet à l'UI de masquer/replier les internes (shards, proxies pyramide) sous leur multiview."""
    try:
        from .. import compositor_fabric as _cf
        lay = _cf.fabric_layout(containers)
        # parent affiché = 1er parent encore présent dans la flotte (sinon None)
        present = {c.get("vmid") for c in containers}
        for c in containers:
            info = lay.get(c.get("vmid")) or {}
            c["fabric_role"] = info.get("role", "logical")
            c["fabric_shards"] = info.get("shards", 0)
            parents = [int(p) for p in (info.get("parents") or []) if str(p).isdigit()]
            c["fabric_parent"] = next((p for p in parents if p in present), None)
    except Exception as _e:
        log.warning("fabric_layout: %s", _e)
        for c in containers:
            c.setdefault("fabric_role", "logical")
            c.setdefault("fabric_shards", 0)
            c.setdefault("fabric_parent", None)
    return containers

# ─── Login / Logout / Setup / Service systemd ────────────────────────────────
# Extraits dans app/routes/auth_pages.py.

# ─── Build de distribution, mise à jour, HA, pairs ───────────────────────────
# Extraits dans app/routes/updates.py, ha.py, peers.py (domaines à faible couplage,
# tranche 2 du découpage — le garde standby et l'injecteur de rôle ci-dessus RESTENT
# ici, ce sont des hooks globaux sur `bp`, pas des routes).

# ─── Pages ────────────────────────────────────────────────

# monitoring_page, aide_page+api_changelog, home, containers_page, traitements_index/legacy,
# multiview_page, labels_page, tsl_sources_redirect, cables_page, streams_page, share links,
# projects_page, backup_page, settings_page — extraits dans app/routes/pages.py.


# Télémétrie/diagnostic infra (fabric overview, panneaux monitoring, pyramide,
# membw, santé nœuds, GPU, statut shm) extraits dans app/routes/monitoring_api.py.




# Registre des plugins (liste/drift, import/export, versions, activation) + shell
# de rubrique + proxy de contrôle générique extraits dans app/routes/plugin_registry.py.
# API Streams (encodeur streamer) extraite dans app/routes/streams_api.py.




# ─── Settings API ─────────────────────────────────────────

# API Réglages (get/set/schema/node overrides/logo/stats/logs) extraite dans
# app/routes/settings_api.py ; /api/sources + /api/home/summary extraits dans
# app/routes/home_dashboard.py.


# ─── Câblage en place depuis la home ───────────────────────────
# Le user clique source puis destination sur la home → POST direct ici.
# Le serveur met à jour le deploy_config du consommateur et redeploie.

# _load_dc extrait dans app/routes/shared.py (helper le plus transversal du fichier).

# Câblage en place depuis la home (wire/unwire/insert_udc), snapshots de câblage
# + vues de disposition (page Câbles) extraits dans app/routes/cabling.py.


# D Phase 2a : routes Proxmox (connexion/options LXC/recréation template 299) RETIRÉES (full-Docker).

# ─── Backup / Restauration de la DB ──────────────────────
# Extrait dans app/routes/backup.py.

# Layouts multiview + Projets (CRUD, export/import, restore streamé) extraits
# dans app/routes/projects_api.py.


@bp.route("/api/containers/<int:vmid>/compose", methods=["POST"])
@require_perm("multiview.edit")
def compose(vmid):
    data = request.json
    def _compose():
        with verrou_vmid(vmid, op="compose"):
            deployer_script(vmid, "multiview", data["params"], "/opt/script/main.py")
    threading.Thread(target=_compose).start()
    return jsonify({"status": "deploiement_en_cours", "vmid": vmid})

@bp.route("/api/containers", methods=["GET"])
@require_login
def liste_containers():
    from ..metrics import gpu_cache as _gpu
    rows = _attach_fabric(_attach_projects(db_get_containers()))
    # Nom du nœud d'exécution, pour l'affichage par tuile (la table ne porte que node_id).
    _noms = {n["id"]: (n.get("name") or f"Nœud {n['id']}") for n in db_get_nodes()}
    for c in rows:
        c["node_name"] = _noms.get(c.get("node_id")) if c.get("node_id") else None
    member_pids = scoped_project_ids()   # None = accès global (pas de filtre)
    if member_pids is not None:
        uid = (current_user() or {}).get("id")
        rows = [c for c in rows if (vmid_project_ids(c["vmid"]) & member_pids)
                or c.get("monitor_user_id") == uid]
    for c in rows:                                  # badge GPU : compositing accéléré cupy (:8080 → cache)
        g = _gpu.get(c.get("vmid"))
        if g and g.get("gpu"):
            c["gpu"] = g
    # CADENCE prête à afficher : {cible, tenue, mesure}. Le verdict « tenue » est rendu par
    # l'orchestrateur et non par le navigateur, pour que le badge et l'alarme de sous-cadence ne
    # puissent pas se contredire (même cible, même seuil de clôture — cf. metrics.cadence_etat).
    from ..metrics import cadence_etat as _cadence
    for c in rows:
        try:
            _dc = json.loads(c["deploy_config"]) if isinstance(c.get("deploy_config"), str) \
                  else (c.get("deploy_config") or {})
        except (ValueError, TypeError):
            _dc = {}
        c["cadence"] = _cadence(c["vmid"], c, _dc)
    return jsonify(rows)

def _raz_infra():
    """L'INFRASTRUCTURE que le RAZ ne balaie pas par défaut : la passerelle WebRTC et LE moteur
    2110_io de chaque nœud.

    Le critère commun n'est pas « c'est important » — tout est important sur un RAZ — mais :
    **l'exploitant ne peut pas le recréer depuis la palette**. La passerelle se déploie depuis
    Réglages → WebRTC ; le moteur porte `auto_provision` et la route de création le REFUSE (400),
    il ne revient que par `docker_driver.ensure_node_engine` (reconfiguration d'un port média, ou
    `backfill_node_engines` 45 s après un redémarrage de l'orchestrateur) — et il revient VIDE,
    aux `deploy_defaults` du plugin : ses `rx_flows`/`tx_flows`/`rx_pins`/`tx_pins`/`tx_slots`
    partent avec la ligne en base. Les emporter dans un balayage par défaut, c'est perdre le
    câblage 2110 de l'installation sur un geste qui ne l'annonçait pas.

    Retourne [(vmid, libellé), …], la passerelle d'abord. La sonde `probe_2110` n'en est PAS :
    elle se crée à la main, donc elle se balaie comme le reste."""
    from .. import settings as st
    from ..i18n import t as _t
    from ..docker_compute import is_mtl_type, _type_of
    from ..docker_driver import _is_probe_type

    infra = []
    gateway_vmid = int(st.get("webrtc_gateway_vmid") or 0)
    if gateway_vmid and db_get_container(gateway_vmid):
        infra.append((gateway_vmid, _t("settings.raz.gateway")))
    for c in db_get_containers():
        t_ = _type_of(c)
        if c["vmid"] != gateway_vmid and is_mtl_type(t_) and not _is_probe_type(t_):
            infra.append((c["vmid"], _t("settings.raz.engine") + " " + (c.get("hostname") or f"#{c['vmid']}")))
    return infra


@bp.route("/api/containers/raz/plan", methods=["GET"])
@require_perm("containers.delete")
def raz_plan():
    """Liste ce que le RAZ supprimera : les containers, et — seulement si on le demande —
    l'infrastructure (cf. `_raz_infra`). Les `steps` décrivent ce que fait RÉELLEMENT
    `detruire_container` : un plan qui annonce autre chose que l'exécution est pire que pas de plan."""
    from ..i18n import t as _t
    del_infra   = request.args.get("del_infra") == "1"
    infra       = _raz_infra()
    infra_vmids = {v for v, _lbl in infra}

    containers = [c for c in db_get_containers() if c["vmid"] not in infra_vmids]
    plan = []
    for c in containers:
        plan.append({
            "vmid":     c["vmid"],
            "hostname": c.get("hostname") or f"#{c['vmid']}",
            "status":   c.get("status") or "unknown",
            "is_infra": False,
            "steps":    [_t("settings.raz.step.docker"),
                         _t("settings.raz.step.liberer"),
                         _t("settings.raz.step.base")],
        })

    if del_infra:
        for vmid, libelle in infra:
            plan.append({
                "vmid":     vmid,
                "hostname": libelle,
                "status":   "—",
                "is_infra": True,
                "steps":    [_t("settings.raz.step.docker"),
                             _t("settings.raz.step.liberer"),
                             _t("settings.raz.step.base")],
            })

    return jsonify({
        "containers": plan,
        "total":      len(plan),
        # Toujours renvoyée (même sans `del_infra`) : la case doit NOMMER ce qu'elle emporte.
        "infra":      [{"vmid": v, "label": lbl} for v, lbl in infra],
    })

# Flag d'interruption du RAZ (op admin globale : un seul RAZ à la fois).
_raz_abort = threading.Event()

@bp.route("/api/containers/raz/abort", methods=["POST"])
@require_perm("containers.delete")
def raz_abort():
    """Demande l'interruption du RAZ en cours (prise en compte entre containers)."""
    _raz_abort.set()
    return jsonify({"ok": True})

@bp.route("/api/containers/raz", methods=["POST"])
@require_perm("containers.delete")
def raz_executer():
    """Supprime tous les containers (et optionnellement l'infrastructure), en streaming."""
    import queue as _queue
    from .. import settings as st

    data        = request.json or {}
    del_infra   = bool(data.get("del_infra"))
    gateway_vmid = int(st.get("webrtc_gateway_vmid") or 0)
    infra       = _raz_infra()
    infra_vmids = {v for v, _lbl in infra}
    # Containers explicitement conservés (décochés dans le plan).
    exclude_vmids = {int(v) for v in (data.get("exclude_vmids") or [])}

    def _detruire_stream(vmid):
        """Lance detruire_container dans un thread et yield ses sous-étapes en
        temps réel. Renvoie (via la dernière valeur) le résultat (ok|err)."""
        q = _queue.Queue()
        box = {}
        def worker():
            try:
                with verrou_vmid(vmid, op="raz-destroy"):
                    detruire_container(vmid, progress=lambda m: q.put(m))
                box["ok"] = True
            except Exception as e:
                box["err"] = str(e)
            finally:
                q.put(None)
        threading.Thread(target=worker, daemon=True).start()
        while True:
            m = q.get()
            if m is None:
                break
            yield f"      · {m}\n"
        box["_done"] = True
        yield box

    def raz_iter():
        # Nouveau RAZ : on repart d'un flag propre.
        _raz_abort.clear()
        containers = [c for c in db_get_containers()
                      if c["vmid"] not in infra_vmids
                      and c["vmid"] not in exclude_vmids]
        cible_infra = infra if del_infra else []
        total       = len(containers) + len(cible_infra)

        if total == 0:
            yield "ℹ Aucune opération à effectuer.\n"
            yield "✅ RAZ terminée.\n"
            return

        ok_count  = 0
        err_count = 0
        interrompu = False

        for i, c in enumerate(containers, 1):
            if _raz_abort.is_set():
                interrompu = True
                yield f"⛔ Interruption demandée — arrêt du RAZ ({i-1}/{total} traités).\n"
                break
            vmid     = c["vmid"]
            hostname = c.get("hostname") or f"#{vmid}"
            yield f"[{i}/{total}] {hostname} (vmid {vmid})…\n"
            box = None
            for item in _detruire_stream(vmid):
                if isinstance(item, dict):
                    box = item
                else:
                    yield item
            if box and box.get("ok"):
                yield f"  ✓ supprimé\n"
                ok_count += 1
            else:
                yield f"  ✕ échec : {box.get('err') if box else 'inconnu'}\n"
                err_count += 1

        # L'infrastructure passe EN DERNIER : si l'exploitant interrompt, il garde le moteur
        # 2110 et la passerelle plutôt que de perdre d'abord ce qui ne se recrée pas en palette.
        for j, (vmid, libelle) in enumerate(cible_infra, len(containers) + 1):
            if _raz_abort.is_set():
                interrompu = True
                yield f"⛔ Interruption demandée — arrêt du RAZ ({j-1}/{total} traités).\n"
                break
            yield f"[{j}/{total}] {libelle} (vmid {vmid})…\n"
            if vmid == gateway_vmid:
                # La passerelle a son propre teardown : il coupe AUSSI le réglage webrtc_enabled.
                try:
                    from services import webrtc_gateway
                    ok, msg = webrtc_gateway.destroy_gateway()
                    if ok:
                        yield f"  ✓ {msg}\n"
                        ok_count += 1
                    else:
                        yield f"  ✕ échec : {msg}\n"
                        err_count += 1
                except Exception as e:
                    yield f"  ✕ échec suppression passerelle : {e}\n"
                    err_count += 1
                continue
            box = None
            for item in _detruire_stream(vmid):
                if isinstance(item, dict):
                    box = item
                else:
                    yield item
            if box and box.get("ok"):
                yield f"  ✓ supprimé\n"
                ok_count += 1
            else:
                yield f"  ✕ échec : {box.get('err') if box else 'inconnu'}\n"
                err_count += 1

        if interrompu:
            yield f"⛔ RAZ interrompue : {ok_count} supprimé(s), {err_count} échec(s).\n"
        elif err_count == 0:
            yield f"✅ RAZ terminée : {ok_count}/{total} supprimés.\n"
        else:
            yield f"⚠ RAZ terminée avec erreurs : {ok_count} supprimés, {err_count} échecs.\n"

    return Response(stream_with_context(raz_iter()),
                    mimetype="text/plain; charset=utf-8",
                    headers={"X-Accel-Buffering": "no",
                             "Cache-Control": "no-cache"})

@bp.route("/api/containers", methods=["POST"])
@require_perm("containers.create")
def creer():
    data = request.json or {}
    from .. import plugins
    _dtype = (data.get("deploy_type") or "").strip()

    # Full-Docker : un container se crée SUR un nœud. Un `node_id` absent n'est plus « un autre
    # backend » (il n'y en a plus qu'un), c'est une requête incomplète — refusée en fin de fonction.
    # On crée la ligne (vmid synthétique) puis on docker run le contrôleur.
    if data.get("node_id"):
        from .. import docker_driver, docker_compute
        node_id = data.get("node_id")
        # Hostname : validé ICI, à la FRONTIÈRE de saisie, avant tout thread — c'était le seul
        # champ d'identité sans contrôle serveur. La normalisation JS était la seule barrière :
        # un POST direct écrivait « ma caméra n°1 » en base, d'où un nom de flux MXL et un
        # composant de chemin de bind-mount (/var/lib/bobi/state/<hostname>) dégénérés.
        # Un hostname vide reste permis : les chemins auto-provisionnés (moteur 2110, monitoring)
        # posent eux-mêmes un nom construit plus bas.
        if str(data.get("hostname") or "").strip():
            _hn, _err = hostnames.valider(data.get("hostname"))
            if _err:
                return jsonify({"error": _err}), 409 if "déjà utilisé" in _err else 400
            data["hostname"] = _hn
        # PAS de repli silencieux sur 2110_io : un type manquant doit ÉCHOUER, sinon une
        # création « Correcteur de couleur » (type non transmis) devenait un receiver MTL en douce.
        d_type = _dtype
        if not d_type:
            return jsonify({"error": "type de déploiement manquant — sélectionne un type avant de créer."}), 400
        # Un type marqué AUTO-PROVISIONNÉ (manifeste `auto_provision:true`, ex. 2110_io) est géré par le
        # nœud (docker_driver.ensure_node_engine à la config d'un port média 2110) : UN seul moteur par
        # nœud. La création MANUELLE est désactivée (décision 2026-07-09 : éviter doublons/confusion +
        # collision avec la garde « 1 moteur/nœud »). Rejet aligné sur le MÊME flag que le filtre UI
        # (revue m3, générique) — la sonde probe_2110 (sans le flag) reste créable à la main.
        if plugins.is_plugin(d_type) and (plugins.get(d_type) or {}).get("auto_provision"):
            return jsonify({"error": "Le type « %s » est auto-provisionné à la configuration d'un port "
                            "média 2110 (Réglages → Réseau) — création manuelle désactivée." % d_type}), 400
        d_params = data.get("deploy_params") or {}
        if plugins.is_plugin(d_type):
            m = plugins.get(d_type)
            # Tier 1 — refus explicite d'une valeur hors bornes (jamais d'écrêtage muet).
            errs = plugins.validate_config(d_type, d_params)
            if errs:
                return jsonify({"error": "Réglages hors bornes : " + " ".join(errs), "errors": errs}), 400
            d_params = {**plugins.effective_deploy_defaults(d_type), **d_params}
            d_params = plugins.coerce_config(d_type, d_params)

        # MTL = chemin matériel dédié (controller bâti, NMOS). Sinon = compute générique
        # (conteneur agent macvlan) → création puis déploiement par le chemin agent standard.
        if docker_compute.is_mtl_type(d_type):
            # 2110_io = infra LIÉE AU NŒUD, jamais rattachée à un projet (décision
            # 2026-07-05) : un project_id éventuel est ignoré sur ce chemin.
            def _creer_docker():
                vmid = docker_driver.creer_container_docker(
                    int(node_id), hostname=data.get("hostname"), deploy_type=d_type)
                if vmid:
                    docker_driver.deploy_docker(vmid, d_params, type_script=d_type)
        else:
            # Création DANS un projet (chantier 5) : project_id posé AVANT le deploy →
            # bind média du projet au docker run + niveau de tally du projet par défaut.
            _proj_id = data.get("project_id")
            def _creer_docker():
                vmid = docker_compute.creer_container_compute(
                    int(node_id), d_type, hostname=data.get("hostname"))
                if vmid:
                    if _proj_id:
                        from ..database import db_set_project
                        try:
                            db_set_project(vmid, int(_proj_id))
                        except (TypeError, ValueError):
                            pass
                    from ..deploy import deployer_script
                    deployer_script(vmid, d_type, d_params)
        threading.Thread(target=_creer_docker).start()
        return jsonify({"status": "creation_docker_en_cours"})

    return jsonify({"error": "node_id requis — un container se crée sur un nœud Docker "
                             "(Réglages → Déploiement → Nœuds)."}), 400

@bp.route("/api/containers/<int:vmid>", methods=["DELETE"])
@require_perm("containers.delete")
def detruire(vmid):
    # Journal d'exploitation : la ligne de DEMANDE est posée ICI, tant qu'on a encore le contexte
    # de requête (donc l'acteur) ET le hostname — après destruction, plus rien en base ne permet
    # de dire de quoi il s'agissait. Cf. app/audit.py.
    from ..audit import journal as _journal
    _journal("alert.audit.detruire_conteneur",
             cible=(db_get_container(vmid) or {}).get("hostname"), vmid=vmid, kind="deploy")
    def _detruire():
        with verrou_vmid(vmid, op="destroy"):
            detruire_container(vmid)
    threading.Thread(target=_detruire).start()
    return jsonify({"status": "destruction_en_cours", "vmid": vmid})

@bp.route("/api/containers/<int:vmid>/resources", methods=["PATCH"])
@require_perm("containers.deploy")
def patch_resources(vmid):
    """Met à jour cores/memory/pinned_cores du container (stop+reconfigure+start)."""
    data = request.json or {}
    cores  = data.get("cores")
    memory = data.get("memory")
    pinned = data.get("pinned_cores")  # str "4,5" / "" / null
    if cores is not None and (not isinstance(cores, int) or cores < 1 or cores > 256):
        return jsonify({"error": "cores hors plage 1..256"}), 400
    if memory is not None and (not isinstance(memory, int) or memory < 64 or memory > 1024*1024):
        return jsonify({"error": "memory hors plage 64..1048576 MB"}), 400
    # pinned : valider parsing si non vide
    if pinned:
        from ..host_ops import parse_cpuset
        try:
            _ = parse_cpuset(pinned)
        except Exception as e:
            return jsonify({"error": f"pinned_cores invalide: {e}"}), 400
    from ..audit import journal as _journal
    # Les changements sont des paires `clé=valeur` TECHNIQUES (cores=4, memory=8192) : une
    # donnée, donc un paramètre légitime. Le « sans changement » du repli, lui, est du FRANÇAIS —
    # il devient une clé à part entière, jamais une valeur par défaut.
    _chgs = ", ".join(f"{k}={v}" for k, v in (("cores", cores), ("memory", memory),
                                              ("pinned_cores", pinned)) if v is not None)
    _journal("alert.audit.modifier_ressources" if _chgs else "alert.audit.modifier_ressources_neant",
             cible=(db_get_container(vmid) or {}).get("hostname"), vmid=vmid, kind="deploy",
             params=({"chgs": _chgs} if _chgs else None))
    def _resize():
        with verrou_vmid(vmid, op="resize"):
            update_resources(vmid=vmid, cores=cores, memory=memory,
                             pinned_cores=pinned if pinned is not None else None)
    threading.Thread(target=_resize).start()
    return jsonify({"status": "maj_en_cours", "vmid": vmid})

# ptp_history/events/log_sources/logs/status extraits dans app/routes/ptp_routes.py.


@bp.route("/api/mxl/pipeline", methods=["GET"])
@require_global_access
def mxl_pipeline():
    """Métriques live du pipeline MXL : rings, shm, RAM hôte Proxmox, grains, frame_index."""
    import os, json
    from ..database import db_get_containers
    from .. import settings as st
    from ..metrics import frame_index_cache
    PIPELINE_TYPES = {
        "2110_io", "streamer",
        "mixer", "multiview", "color_corrector", "udc", "delay", "avsync", "split",
        "player", "recorder",
    }
    video_ring = int(st.get("shm_video_ring") or 10)

    # ── /dev/shm : index taille des fichiers ─────────────────────
    shm_total_bytes = 0
    shm_size_map = {}   # nom_fichier → octets
    try:
        for entry in os.scandir("/dev/shm"):
            if entry.is_file():
                try:
                    sz = entry.stat().st_size
                    shm_size_map[entry.name] = sz
                    shm_total_bytes += sz
                except OSError:
                    pass
    except OSError:
        pass

    from .. import plugins as _plugins
    from ..scripts import CHROMA_DIV

    cs = db_get_containers()
    containers = []
    for c in cs:
        dc_raw = c.get("deploy_config")
        try:
            dc = json.loads(dc_raw) if isinstance(dc_raw, str) else (dc_raw or {})
        except Exception:
            dc = {}
        t = dc.get("type") or ""   # type dans deploy_config, pas de colonne dédiée en DB
        if t not in PIPELINE_TYPES:
            if c.get("status") != "running":
                continue
        vmid = c["vmid"]
        hostname = c.get("hostname") or f"ct-{vmid}"

        # ── Débit théorique via topology_ports (même formule que home/summary) ──
        bw_bps = 0.0
        try:
            kind = t
            p = dc.get("params") or {}
            hn = p.get("hostname") or hostname
            produces = []
            tp_hook = _plugins.get_hook(kind, "topology_ports") if kind else None
            if tp_hook:
                produces = tp_hook(hn, p, {}).get("produces", [])
            elif _plugins.is_plugin(kind):
                w_info = _plugins.derive_wiring(kind, hn, p)
                for prod in w_info.get("produces", []):
                    if prod.get("shm"):
                        entry = {"kind": prod.get("essence") or "video"}
                        if prod.get("format"):
                            entry["format"] = prod["format"]
                        produces.append(entry)
            for pr in produces:
                fmt = pr.get("format") or {}
                if (pr.get("kind") or "video") == "audio":
                    sr  = int(fmt.get("sample_rate") or 48000)
                    ch  = int(fmt.get("channels") or 8)
                    bd  = int(fmt.get("bit_depth") or 24)
                    bw_bps += sr * ch * (bd / 8.0) * 8.0
                else:
                    fw = int(fmt.get("width") or 0)
                    fh = int(fmt.get("height") or 0)
                    fps = float(fmt.get("fps") or 0)
                    cw, ch2 = CHROMA_DIV.get(str(fmt.get("chroma") or "422"), CHROMA_DIV["422"])
                    if fw and fh and fps:
                        bw_bps += fw * fh * (1.0 + 2.0 / (cw * ch2)) * fps * 8.0
        except Exception:
            pass

        containers.append({
            "vmid":        vmid,
            "hostname":    hostname,
            "type":        t,
            "status":      c.get("status") or "unknown",
            "fps":         c.get("fps"),
            "frame_index": frame_index_cache.get(vmid),
            "bw_bps":      int(bw_bps),
        })

    # ── RAM hôte ─────────────────────────
    # D Phase 2a : full-Docker, plus de RAM hôte via l'API Proxmox. La RAM par-nœud reviendra avec
    # le retargeting hôte-du-nœud (B). Pour l'instant : non renseignée.
    mem_available_mb = None
    mem_total_mb = None

    return jsonify({
        "video_ring":       int(st.get("shm_video_ring") or 10),
        "audio_ring":       int(st.get("shm_audio_ring") or 100),
        "containers":       containers,
        "shm_total_mb":     round(shm_total_bytes / 1024 / 1024, 1),
        "mem_available_mb": mem_available_mb,
        "mem_total_mb":     mem_total_mb,
    })

# ptp_install/ptp_apply extraits dans app/routes/ptp_routes.py.


# ─── MTL : prép host DPDK/E810 ────────────────────────────────────────────
# Extrait dans app/routes/mtl_engine.py (regroupé avec les flux composables RX/TX :
# les deux forment le domaine du moteur 2110_io).

# Monitoring WebRTC par utilisateur + monitor dédié par player extraits dans
# app/routes/monitor_routes.py.


@bp.route("/api/cpu/status", methods=["GET"])
@require_global_access
def cpu_status():
    """Carte des CPUs de l'host avec qui pin quoi + détection de conflits."""
    from .. import settings as st
    from ..host_ops import host_cpu_count, parse_cpuset
    host = _req_host()
    ok, n_cpus, msg = host_cpu_count(host)
    node_id = _eff_node_id()
    # ⚠ FILTRER PAR NŒUD. Sans ça, la carte CPU du nœud sélectionné recevait les épinglages de TOUS
    # les nœuds : conteneurs étrangers listés, et surtout FAUX CONFLITS (deux conteneurs épinglés sur
    # les mêmes NUMÉROS de cœur mais sur des machines différentes étaient signalés en orange alors
    # qu'ils ne se croisent jamais). La partie core_pool ci-dessous était, elle, déjà scopée au nœud.
    containers = [c for c in db_get_containers()
                  if node_id is None or c.get("node_id") == node_id]
    # Collecte pinning par container
    by_container = []
    by_cpu = {i: [] for i in range(n_cpus)}
    conflicts = set()
    seen_vmids = set()
    for c in containers:
        pin = c.get("pinned_cores")
        if not pin:
            continue
        cpus = parse_cpuset(pin)
        seen_vmids.add(c["vmid"])
        by_container.append({
            "vmid": c["vmid"], "hostname": c.get("hostname"), "source": "pin",
            "pinned_cores": pin, "cpu_set": sorted(cpus),
        })
        for cpu in cpus:
            if cpu in by_cpu:
                if by_cpu[cpu]:
                    conflicts.add(cpu)
                by_cpu[cpu].append({"vmid": c["vmid"], "hostname": c.get("hostname")})
    # Réservations `core_pool`/node_core_alloc NON reflétées dans containers.pinned_cores : les
    # lcores DPDK des moteurs 2110 (posés par reserve_exact au déploiement). Sans ça, la carte CPU
    # montrait les moteurs comme « rien pinné » alors qu'ils tiennent leurs cœurs en exclusivité.
    from .. import core_pool
    hostname_by_vmid = {c["vmid"]: c.get("hostname") for c in containers}
    for vmid, cores in core_pool.allocations_by_vmid(_eff_node_id()).items():
        if vmid in seen_vmids or not cores:
            continue
        by_container.append({
            "vmid": vmid, "hostname": hostname_by_vmid.get(vmid), "source": "engine",
            "pinned_cores": core_pool.fmt_cpuset(cores), "cpu_set": sorted(cores),
        })
        for cpu in cores:
            if cpu in by_cpu:
                if by_cpu[cpu]:
                    conflicts.add(cpu)
                by_cpu[cpu].append({"vmid": vmid, "hostname": hostname_by_vmid.get(vmid)})
    return jsonify({
        "ok": ok, "host": host, "n_cpus": n_cpus, "error": None if ok else msg,
        "by_cpu": by_cpu, "by_container": by_container,
        "conflicts": sorted(conflicts),
    })

@bp.route("/api/containers/<int:vmid>/restart", methods=["POST"])
@require_perm("containers.deploy")
def restart(vmid):
    # Garde-fou : redémarrer un MOTEUR 2110_io coupe TOUS ses flux (RX, TX + consommateurs aval) →
    # confirm:true requis. Les autres types (singuliers) restent sans confirmation.
    from ..database import db_get_container
    data = request.get_json(silent=True) or {}
    _c = db_get_container(vmid)
    _dc = _load_dc(_c) if _c else None
    if _dc and _dc.get("type") == "2110_io" and not bool(data.get("confirm")):
        return jsonify({"ok": False, "needs_confirm": True,
                        "reason": "Redémarrage du moteur 2110 — coupure brève de TOUS les flux "
                                  "(RX, TX et consommateurs aval)."}), 409
    def _restart():
        with verrou_vmid(vmid, op="restart"):
            redemarrer_container(vmid)
    threading.Thread(target=_restart).start()
    return jsonify({"status": "redemarrage_en_cours", "vmid": vmid})

@bp.route("/api/containers/<int:vmid>/recreate", methods=["POST"])
@require_perm("containers.deploy")
def recreate(vmid):
    """Recrée le conteneur SUR L'IMAGE COURANTE du nœud (après un build), sans perdre sa ligne
    en base : `instance_uuid`, emplacement, `deploy_config` et tokens survivent.

    Pourquoi une route dédiée : `restart` ne suffit pas pour un conteneur compute — il fait un
    `docker start`, qui repart sur l'image d'origine. Adopter une image fraîchement buildée
    demandait jusqu'ici un `docker rm` à la main sur chaque conteneur (fait à Horace le
    2026-08-19 : deux murs, quatre shards, une pyramide).

    ⚠ Coupure de quelques secondes → `confirm:true` obligatoire, comme le redémarrage d'un moteur.
    Un MOTEUR (`2110_io`) n'a pas besoin de cette route : en `--rm`, il adopte la nouvelle image
    au simple `restart`."""
    from ..database import db_get_container
    from .. import docker_compute
    data = request.get_json(silent=True) or {}
    _c = db_get_container(vmid)
    if not _c:
        return jsonify({"ok": False, "error": f"container #{vmid} introuvable"}), 404
    if not docker_compute.is_compute_container(_c):
        return jsonify({"ok": False,
                        "error": "réservé aux conteneurs compute — un moteur adopte sa nouvelle "
                                 "image par /restart (il tourne en --rm)"}), 400
    if not bool(data.get("confirm")):
        return jsonify({"ok": False, "needs_confirm": True,
                        "image_courante": docker_compute.image_courante_compute(vmid),
                        "reason": "Recréation du conteneur — coupure de quelques secondes le "
                                  "temps du retrait et du redéploiement du script."}), 409

    def _recreate():
        with verrou_vmid(vmid, op="recreate"):
            docker_compute.recreer_compute(vmid)
    threading.Thread(target=_recreate, daemon=True).start()
    return jsonify({"ok": True, "status": "recreation_en_cours", "vmid": vmid})


@bp.route("/api/containers/<int:vmid>/project", methods=["PATCH"])
@require_perm("containers.deploy")
def patch_container_project(vmid):
    from ..database import db_get_container, db_get_project
    from ..containers import changer_media_projet
    data = request.json or {}
    project_id = data.get("project_id")  # None ou int
    c = db_get_container(vmid)
    if not c:
        return jsonify({"error": "container introuvable"}), 404
    # Infra liée au nœud / partagée (2110_io, passerelle…) : jamais rattachée à un projet.
    from ..projects import PROJECT_EXCLUDED_TYPES
    _t = (_load_dc(c) or {}).get("type")
    if project_id and _t in PROJECT_EXCLUDED_TYPES:
        return jsonify({"error": f"le type « {_t} » est lié au nœud/infra partagée — "
                                 "non rattachable à un projet"}), 400
    media_host_dir = None
    if project_id:
        proj = db_get_project(int(project_id))
        if not proj:
            return jsonify({"error": "projet introuvable"}), 404
        media_host_dir = proj.get("media_path")
    def _changer_projet():
        with verrou_vmid(vmid, op="project"):
            changer_media_projet(vmid, int(project_id) if project_id else None, media_host_dir)
    threading.Thread(target=_changer_projet).start()
    return jsonify({"status": "en_cours"})

# ─── Sauvegardes de config par container (2110_io & co, décision 2026-07-05) ──
# Le 2110_io étant lié au nœud (exclu des projets), sa config se sauvegarde ICI :
# des snapshots nommés des params (deploy_config), génériques à tous les types.
# Stockage : plugin_store scope "cfgsnap:<vmid>" (voyage déjà nulle part, purgé avec rien —
# volontairement indépendant des projets).

@bp.route("/api/containers/<int:vmid>/config_snapshots", methods=["GET"])
@require_login
def config_snapshots_list(vmid):
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    from ..database import plugin_store_list_scope
    rows = plugin_store_list_scope(f"cfgsnap:{vmid}")
    return jsonify({"snapshots": [{"id": r.get("id"), "name": r.get("name"),
                                   "created_at": r.get("created_at")} for r in rows]})

@bp.route("/api/containers/<int:vmid>/config_snapshots", methods=["POST"])
@require_perm("containers.deploy")
def config_snapshots_save(vmid):
    from ..database import plugin_store_create, db_get_container
    name = ((request.json or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "name requis"}), 400
    c = db_get_container(vmid)
    dc = _load_dc(c) if c else None
    if not dc or not dc.get("type"):
        return jsonify({"error": "container sans script déployé"}), 400
    sid = plugin_store_create(dc["type"], f"cfgsnap:{vmid}", name,
                              {"type": dc["type"], "params": dc.get("params") or {}},
                              unique_name=True)
    if sid is None:
        return jsonify({"error": "ce nom de sauvegarde existe déjà"}), 409
    return jsonify({"status": "ok", "id": sid})

@bp.route("/api/containers/<int:vmid>/config_snapshots/<int:sid>/restore", methods=["POST"])
@require_perm("containers.deploy")
def config_snapshots_restore(vmid, sid):
    """Réapplique un snapshot de config : remplace les params et REDÉPLOIE (pour un
    2110_io en marche, coupure brève de tous les flux — confirm requis)."""
    from ..database import plugin_store_get, db_get_container
    from ..vmlocks import verrou_vmid
    snap = plugin_store_get(sid)
    if not snap or snap.get("scope") != f"cfgsnap:{vmid}":
        return jsonify({"error": "snapshot introuvable"}), 404
    val = snap.get("value") or {}
    t, params = val.get("type"), val.get("params") or {}
    if not t:
        return jsonify({"error": "snapshot invalide"}), 400
    c = db_get_container(vmid)
    if t == "2110_io" and (c or {}).get("status") == "running" \
            and not bool((request.json or {}).get("confirm")):
        return jsonify({"ok": False, "needs_confirm": True,
                        "reason": "Redéploiement du moteur 2110 — coupure brève de TOUS les flux."}), 409
    def _apply():
        from ..deploy import deployer_script
        with verrou_vmid(vmid, op="cfgsnap"):
            deployer_script(vmid, t, params)
    threading.Thread(target=_apply).start()
    return jsonify({"status": "en_cours"})

@bp.route("/api/containers/<int:vmid>/config_snapshots/<int:sid>", methods=["DELETE"])
@require_perm("containers.deploy")
def config_snapshots_delete(vmid, sid):
    from ..database import plugin_store_get, plugin_store_delete
    snap = plugin_store_get(sid)
    if not snap or snap.get("scope") != f"cfgsnap:{vmid}":
        return jsonify({"error": "snapshot introuvable"}), 404
    plugin_store_delete(sid)
    return jsonify({"status": "ok"})

@bp.route("/api/containers/<int:vmid>/config", methods=["GET"])
@require_login
def get_config(vmid):
    from ..database import db_get_container
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    container = db_get_container(vmid)
    if not container:
        return jsonify({"error": "Container introuvable"}), 404
    return jsonify(container)

@bp.route("/api/containers/<int:vmid>/deploy", methods=["POST"])
@require_perm("containers.deploy")
def deploy(vmid):
    data = request.json
    type_ = data["type"]
    params = data["params"] or {}
    # ── GARDE ANTI-ÉCRASEMENT (édition à plusieurs) ──────────────────────────────────────────
    # Un éditeur qui poste TOUT l'état d'un conteneur (le composer multiview le fait à chaque
    # geste) écrase ce qu'un autre a fait entre son chargement et son envoi : l'image posée par A
    # disparaissait dès que B bougeait quoi que ce soit. `base_rev` = la révision que l'éditeur
    # avait sous les yeux ; si `config_rev` a bougé depuis, on REFUSE plutôt que d'écraser.
    # Optionnel par construction : un client qui ne l'envoie pas garde le comportement historique
    # (palette, macros, page Câbles, restauration de projet — aucun n'écrase un travail humain en
    # cours d'édition). Cf. `app/edit_lock.py` pour l'autre moitié, consultative.
    if data.get("base_rev") is not None:
        from ..database import db_config_rev_auteur
        _rev, _auteur = db_config_rev_auteur(vmid)
        try:
            _base = int(data.get("base_rev"))
        except (TypeError, ValueError):
            _base = -1
        _moi = (current_user() or {}).get("id")
        # Conflit = la config a bougé ET c'est QUELQU'UN D'AUTRE qui l'a bougée. Nos propres
        # écritures ne nous barrent jamais la route : le déploiement étant asynchrone, l'éditeur
        # ne peut pas connaître la révision qu'il vient lui-même de produire.
        if _base != _rev and _auteur is not None and _auteur != _moi:
            from ..edit_lock import etat as _verrou_etat
            _v = _verrou_etat(vmid)
            return jsonify({"ok": False, "error": "config_perimee", "rev": _rev,
                            "base_rev": _base,
                            "par": ("" if _v.get("libre") else _v.get("user_name") or ""),
                            "reason": "Ce conteneur a été modifié depuis l'ouverture de votre "
                                      "éditeur — votre envoi n'a pas été appliqué."}), 409
    # Garde-fou : RE-déployer un moteur 2110_io DÉJÀ EN MARCHE relance mtl_init → coupure de TOUS les
    # flux. confirm:true requis dans ce cas (le bouton « Redéployer pour réaligner » l'envoie). Le 1er
    # déploiement (conteneur pas encore running) ne coupe rien → pas de confirmation.
    if type_ == "2110_io" and not bool(data.get("confirm")):
        from ..database import db_get_container as _dgc
        _cc = _dgc(vmid)
        if _cc and _cc.get("status") == "running":
            return jsonify({"ok": False, "needs_confirm": True,
                            "reason": "Redéploiement du moteur 2110 — coupure brève de TOUS les flux "
                                      "(RX, TX et consommateurs aval)."}), 409
    # Type plugin déployé depuis la palette : compléter avec les deploy_defaults du
    # manifeste PUIS les params déjà persistés (même type) — une clé absente du POST
    # garde sa valeur courante au lieu de retomber au défaut (la palette n'expose plus
    # tous les champs : scope user → page plugin).
    from .. import plugins
    if plugins.is_plugin(type_):
        m = plugins.get(type_)
        existing = {}
        _dc = _load_dc(db_get_container(vmid)) or {}
        if _dc.get("type") == type_ and isinstance(_dc.get("params"), dict):
            existing = _dc["params"]
        # Tier 1 — les valeurs POSTÉES sont VALIDÉES (refus 400), jamais écrêtées en silence.
        errs = plugins.validate_config(type_, data.get("params") or {})
        if errs:
            return jsonify({"error": "Réglages hors bornes : " + " ".join(errs), "errors": errs}), 400
        params = {**plugins.effective_deploy_defaults(type_), **existing, **params}
        params = plugins.coerce_config(type_, params)   # filet (params PERSISTÉS hérités → alerte)
    # Version optionnelle : rappeler une version archivée précise (palette). None = courante.
    version = (data.get("version") or "").strip() or None
    _path = data.get("path", "/opt/script/main.py")
    # Journal d'exploitation : ligne de DEMANDE, posée avant le dispatch (cf. app/audit.py). Les
    # lignes « déploiement en cours… / déployé et redémarré » émises ensuite par le thread restent
    # sans acteur : elles décrivent le travail de la machine, pas la décision de l'humain.
    from ..audit import journal as _journal
    # Le suffixe de version est un fragment conditionnel : deux clés complètes, pas un suffixe
    # collé — collé, il resterait français au milieu d'une phrase anglaise.
    _journal("alert.audit.deployer_script_version" if version else "alert.audit.deployer_script",
             cible=(db_get_container(vmid) or {}).get("hostname"), vmid=vmid, kind="deploy",
             params=({"t": type_, "version": version} if version else {"t": type_}))
    _auteur = (current_user() or {}).get("id")
    def _deploy():
        # Un thread n'hérite ni de la session ni du thread-local : on repose l'auteur nous-mêmes,
        # sinon l'écriture serait attribuée à « la machine » et la garde de révision laisserait
        # passer l'écrasement suivant.
        from ..edit_lock import poser_auteur
        poser_auteur(_auteur)
        with verrou_vmid(vmid, op="deploy"):
            deployer_script(vmid=vmid, type_script=type_, params=params,
                            script_path=_path, version=version)
    threading.Thread(target=_deploy).start()
    return jsonify({"status": "deploiement_en_cours", "vmid": vmid})

# ─── Verrou d'ÉDITION (consultatif) + révision de configuration ──────────────
# « Qui a la main sur ce conteneur ? » Le verrou n'interdit rien au serveur (les permissions
# restent seules juges) : il évite la collision par la conversation, là où l'utilisateur peut
# encore décider. Le filet dur, lui, est la garde `base_rev` du déploiement ci-dessus.
# Générique par conteneur : le composer multiview est le premier client, tout éditeur de plugin
# qui poste un état complet peut s'en servir.

@bp.route("/api/containers/<int:vmid>/edit-lock", methods=["GET"])
@require_login
def edit_lock_get(vmid):
    from ..edit_lock import etat, BATTEMENT_S
    from ..database import db_config_rev
    err = check_vmid_access(vmid, "viewer")
    if err:
        return err
    u = current_user() or {}
    return jsonify({**etat(vmid, u.get("id")), "rev": db_config_rev(vmid),
                    "battement_s": BATTEMENT_S})

@bp.route("/api/containers/<int:vmid>/edit-lock", methods=["POST"])
@require_perm("containers.deploy")
def edit_lock_post(vmid):
    """Prise ou BATTEMENT de cœur. `force` = reprise de main explicite (l'utilisateur a lu qui
    éditait). 409 + l'état courant quand quelqu'un d'autre a la main."""
    from ..edit_lock import prendre, BATTEMENT_S
    from ..database import db_config_rev
    err = check_vmid_access(vmid, "operator")
    if err:
        return err
    u = current_user() or {}
    body = request.get_json(silent=True) or {}
    # Nom LISIBLE : « Vincent » vaut mieux que « vhamon » dans un bandeau lu en régie.
    _nom = " ".join(x for x in (u.get("prenom"), u.get("nom")) if x).strip() or u.get("username") or ""
    obtenu, st = prendre(vmid, u.get("id"), _nom, force=bool(body.get("force")))
    if body.get("force") and obtenu:
        from ..audit import journal as _journal
        _journal("alert.audit.forcer_edition",
                 cible=(db_get_container(vmid) or {}).get("hostname"), vmid=vmid, kind="deploy")
    return jsonify({**st, "rev": db_config_rev(vmid), "battement_s": BATTEMENT_S}), (200 if obtenu else 409)

@bp.route("/api/containers/<int:vmid>/edit-lock", methods=["DELETE"])
@require_login
def edit_lock_delete(vmid):
    from ..edit_lock import rendre
    u = current_user() or {}
    return jsonify({"rendu": rendre(vmid, u.get("id"))})

# color_corrector est désormais un plugin : son contrôle (/state, /params, /reset,
# /input) passe par le proxy plugin générique /api/containers/<vmid>/plugin/<path>.

# ─── Stockage générique par plugin (plugin_store) ────────────
# Tout plugin persiste des entrées JSON nommées sans toucher au cœur : presets globaux
# (scope=''), mémoires par container (scope=str(vmid))… `unique_name` lu du manifeste `store`.
def _store_opts(type_):
    from .. import plugins
    return (plugins.get(type_) or {}).get("store") or {}

@bp.route("/api/plugins/<type_>/store", methods=["GET"])
@require_login
def plugin_store_get_route(type_):
    from .. import plugins
    from ..database import plugin_store_list
    if not plugins.is_plugin(type_):
        return jsonify({"error": f"type plugin inconnu : {type_}"}), 404
    scope = (request.args.get("scope") or "").strip()
    return jsonify(plugin_store_list(type_, scope))

@bp.route("/api/plugins/<type_>/store", methods=["POST"])
@require_perm("plugins.operate")
def plugin_store_post_route(type_):
    from .. import plugins
    from ..database import plugin_store_create
    if not plugins.is_plugin(type_):
        return jsonify({"error": f"type plugin inconnu : {type_}"}), 404
    data = request.json or {}
    try:
        sid = plugin_store_create(type_, data.get("scope") or "", data.get("name"),
                                  data.get("value"),
                                  unique_name=bool(_store_opts(type_).get("unique_name")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if sid is None:
        return jsonify({"ok": False, "error": "nom déjà utilisé"}), 409
    return jsonify({"ok": True, "id": sid})

@bp.route("/api/plugins/<type_>/store/<int:sid>", methods=["PATCH"])
@require_perm("plugins.operate")
def plugin_store_patch_route(type_, sid):
    from ..database import plugin_store_update
    data = request.json or {}
    return jsonify({"ok": plugin_store_update(sid, name=data.get("name"), value=data.get("value"))})

@bp.route("/api/plugins/<type_>/store/<int:sid>", methods=["DELETE"])
@require_perm("plugins.operate")
def plugin_store_delete_route(type_, sid):
    from ..database import plugin_store_delete
    return jsonify({"ok": plugin_store_delete(sid)})

# _mixer_proxy extrait dans app/routes/shared.py (partagé par plugin_routes/split.py).

# ─── Rappel de presets à chaud (réutilisé par le provider Ember+) ────────────
# Générique pour la *découverte* (control.recall du manifeste : source du store +
# endpoint :8082) ; l'*application* reste bespoke par type (pattern hybride, pas de
# transform déclaratif). POST direct vers :8082 (pas via _mixer_proxy, qui renvoie
# des réponses Flask et exige un contexte d'app — inutilisable depuis un thread).
def recall_presets(vmid, type_):
    """Liste ordonnée des presets rappelables d'un container (selon control.recall).
    Retourne (rc|None, [preset,...]) — chaque preset a au moins une clé 'name'."""
    from .. import plugins
    rc = ((plugins.get(type_) or {}).get("control") or {}).get("recall")
    if not rc:
        return None, []
    src = rc.get("source")
    if src == "plugin_store":
        from ..database import plugin_store_list
        scope = str(vmid) if rc.get("scope") == "container" else ""
        return rc, plugin_store_list(type_, scope)
    if src == "layouts":
        from ..database import db_get_layouts
        return rc, db_get_layouts()
    return rc, []

def recall_preset(vmid, type_, index, duration_ms=0):
    """Applique à chaud le index-ième preset. Retourne (ok: bool, detail: str).
    `duration_ms` : durée de transition quand le type la supporte (split), 0 = cut."""
    from ..addressing import get_container_ip
    import requests as _req
    rc, presets = recall_presets(vmid, type_)
    if not rc:
        return False, f"{type_} sans control.recall"
    if not (0 <= index < len(presets)):
        return False, f"index {index} hors borne (0..{len(presets) - 1})"
    preset = presets[index]
    ip = get_container_ip(vmid)
    if not ip:
        return False, "IP container introuvable"
    endpoint = rc.get("endpoint")

    def _post(path, body):
        try:
            r = _req.post(f"http://{ip}:8082{path}", json=body, timeout=2)
            return r.status_code < 400
        except Exception as e:
            log.warning(f"recall: POST {path} échec : {e}")
            return False

    name = preset.get("name")
    if type_ == "split":
        return _post(endpoint, {"boxes": preset["value"],
                                "duration_ms": max(0, int(duration_ms or 0))}), name
    if type_ == "color_corrector":
        return _post(endpoint, preset["value"]), name
    if type_ == "multiview":
        # layout global : on pousse la géométrie de chaque fenêtre commune à la cible
        # (le style n'est pas appliqué à chaud) puis on persiste en deploy_config.
        flux = (preset.get("config") or {}).get("flux_config") or []
        c = db_get_container(vmid)
        dc = _load_dc(c) or {}
        cur = list((dc.get("params") or {}).get("flux_config") or [])
        n = min(len(flux), len(cur))
        ok_any = False
        for i in range(n):
            body = {k: int(flux[i][k]) for k in ("x", "y", "w", "h") if k in flux[i]}
            if not body:
                continue
            body["idx"] = i
            if _post(endpoint, body):
                ok_any = True
                cur[i] = {**cur[i], **{k: body[k] for k in ("x", "y", "w", "h") if k in body}}
        if len(flux) != len(cur):
            log.info(f"recall multiview #{vmid}: layout {len(flux)} fenêtres vs cible {len(cur)} — {n} appliquées")
        if ok_any:
            try:
                from ..database import db_update_deploy_config
                dc["params"]["flux_config"] = cur
                db_update_deploy_config(vmid, dc["type"], dc["params"])
            except Exception as e:
                log.warning(f"recall multiview #{vmid}: persistance échec : {e}")
        return ok_any, name
    return False, f"type {type_} non géré pour le recall"

# Les routes bespoke /api/containers/<vmid>/mixer/* ont été migrées vers le proxy
# plugin générique /api/containers/<vmid>/plugin/<path> (endpoints déclarés dans
# plugins/mixer/plugin.json:control.endpoints ; /state et /preview.png en lecture
# via control.read_endpoints). Le contrôle live du mixer n'a plus de routes dédiées.

# ─── Split / SuperSource — persistance + mémoires ────────────────────────────
# Routes bespoke extraites dans app/routes/plugin_routes/split.py (premier module d'un
# nouveau paquet — cf. app/routes/plugin_routes/__init__.py pour la convention : un
# module par type de plugin qui a besoin de routes touchant la DB au-delà du proxy générique).

@bp.route("/api/alerts", methods=["GET"])
@require_login
def liste_alertes():
    # Les alertes sont des messages libres non rattachés à un projet : un utilisateur
    # scopé reçoit une liste vide (les alertes par-projet arrivent avec le chantier 2/3).
    if scoped_project_ids() is not None:
        return jsonify([])
    q = (request.args.get("q") or "").strip() or None
    niveau = (request.args.get("niveau") or "").strip() or None
    try:
        limit = max(1, min(int(request.args.get("limit", 1000)), 1000))
    except ValueError:
        limit = 1000
    # Filtres CONTEXTE (colonnes `vmid`/`node_id`/`kind`, cf. database.ALERT_KINDS) : « toutes les
    # alertes de ce conteneur / de ce nœud / de cette nature ». Absents = comportement historique.
    def _iarg(nom):
        v = (request.args.get(nom) or "").strip()
        try:
            return int(v) if v else None
        except ValueError:
            return None
    kind = (request.args.get("kind") or "").strip() or None
    # `user=` : filtre du JOURNAL D'EXPLOITATION — « qu'a fait untel ? ». La valeur spéciale
    # `machine` isole les actions sans acteur (surveillance, réconciliation).
    # Rendu à la LECTURE, dans la langue du lecteur : les lignes portant une clé (`msg_key`)
    # sont re-rendues ici, les autres servies telles quelles (cf. `i18n.rendre_alerte`).
    from ..i18n import rendre_alertes
    return jsonify(rendre_alertes(db_get_alerts(q=q, niveau=niveau, limit=limit,
                                  vmid=_iarg("vmid"), node_id=_iarg("node_id"), kind=kind,
                                  user=(request.args.get("user") or "").strip() or None)))


@bp.route("/api/alerts/episodes", methods=["GET"])
@require_login
def liste_episodes_alertes():
    """Ce que l'ANTI-REBOND a tu (cf. `database._antirebond`) : un épisode par symptôme en cours,
    avec son compte réel et le nombre d'occurrences étouffées depuis la dernière ligne écrite.
    Sans cette vue, l'étouffement serait un échec silencieux de plus.
    `?tous=1` inclut les épisodes déjà clos (encore en base une semaine)."""
    if scoped_project_ids() is not None:
        return jsonify([])
    from ..database import db_alert_episodes
    tous = (request.args.get("tous") or "").strip() in ("1", "true", "oui")
    return jsonify(db_alert_episodes(actifs_seulement=not tous))

# Agrégation NMOS/2110 (SDP parsing, receivers/senders detail, /api/io/mtl,
# abonnement manuel receiver) extraite dans app/routes/nmos_detail.py.



@bp.route("/api/containers/<int:vmid>/control/<action>", methods=["POST"])
@require_perm("plugins.operate")
def container_control_action(vmid, action):
    """Route générique de contrôle à chaud pour les plugins déclarant un hook control_action.
    Délègue la mutation des params + la payload hot-wire au plugin. Persiste + hot-wire + repli redeploy."""
    from .. import plugins as _ctl_pl
    from ..database import db_update_deploy_config
    from ..addressing import get_container_ip
    err = check_vmid_access(vmid, "operator")
    if err:
        return err
    c = db_get_container(vmid)
    if not c:
        return jsonify({"error": f"container #{vmid} introuvable"}), 404
    dc = _load_dc(c)
    if not dc:
        return jsonify({"error": f"#{vmid} sans script déployé"}), 400
    t = dc.get("type"); params = dict(dc.get("params") or {})
    h = _ctl_pl.get_hook(t, "control_action")
    if not h:
        return jsonify({"error": f"action '{action}' non supportée pour le type {t}"}), 404
    body = request.get_json(force=True, silent=True) or {}
    try:
        res = h(action, body, params, {"vmid": vmid, "type": t,
                                       "hostname": c.get("hostname") or ""})
    except Exception as _e:
        return jsonify({"error": str(_e)}), 400
    if res is None:
        return jsonify({"error": f"action '{action}' inconnue"}), 404
    new_params = res["params"]
    db_update_deploy_config(vmid, t, new_params)
    ip = c.get("ip") or get_container_ip(vmid)
    hot_ep = res.get("hot_endpoint"); hot_body = res.get("hot_body")
    hot_ok = False
    if ip and hot_ep and hot_body:
        try:
            import requests as _req
            r = _req.post(f"http://{ip}:8082{hot_ep}", json=hot_body, timeout=2)
            hot_ok = r.status_code == 200
        except Exception:
            pass
    extra = {k: v for k, v in res.items()
             if k not in ("params", "hot_endpoint", "hot_body")}
    if not hot_ok:
        def _redeploy():
            with verrou_vmid(vmid, op="plugin-action-redeploy"):
                deployer_script(vmid, t, new_params)
        threading.Thread(target=_redeploy, daemon=True).start()
        return jsonify({"status": "deploiement_en_cours", "vmid": vmid, **extra})
    return jsonify({"status": "bascule_a_chaud", "vmid": vmid, **extra})

# _fetch_host_nics + _NIC_CAPS_PROBE extraits dans app/routes/shared.py (helper transversal,
# partagé par /api/ethernet/status ici et app/routes/node_network.py).


@bp.route("/api/ethernet/status", methods=["GET"])
@require_global_access
def ethernet_status():
    """Inventaire des NICs visibles sur l'hôte Proxmox : nom, MAC, link state,
    speed, IPv4, compteurs rx/tx_bytes. Le client calcule la bande passante
    en faisant un delta entre deux appels successifs."""
    host = _req_host()
    ok, error, nics = _fetch_host_nics(host)
    if not ok:
        return jsonify({"ok": False, "host": host, "error": error}), 500
    return jsonify({
        "ok": True,
        "host": host,
        "timestamp_ms": int(__import__("time").time() * 1000),
        "interfaces": nics,
    })

@bp.route("/api/alerts/export", methods=["GET"])
@require_global_access
def export_alertes():
    import csv, io
    from flask import Response
    q = (request.args.get("q") or "").strip() or None
    niveau = (request.args.get("niveau") or "").strip() or None
    fmt = (request.args.get("format") or "csv").lower()
    # L'export part dans la langue de celui qui le déclenche — c'est lui qui lira le fichier.
    from ..i18n import rendre_alertes
    rows = rendre_alertes(db_get_alerts(q=q, niveau=niveau, limit=1000))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if fmt == "txt":
        body = "\n".join(
            f"[{r['timestamp']}] {r['niveau']}: {r['message']}" for r in rows
        ) + "\n"
        return Response(body, mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="alerts-{stamp}.txt"'})

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "niveau", "message"])
    for r in rows:
        w.writerow([r["timestamp"], r["niveau"], r["message"]])
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="alerts-{stamp}.csv"'})

@bp.route("/api/containers/<int:vmid>/start_script", methods=["POST"])
@require_perm("containers.deploy")
def start_script(vmid):
    from ..database import db_get_container
    from .. import docker_compute
    _c = db_get_container(vmid)
    # MTL : start = docker run du controller. Compute : a un agent :8081 → start du SCRIPT via
    # le chemin agent standard (comme un LXC), pas du conteneur.
    if _c and not docker_compute.is_compute_container(_c):
        from .. import docker_driver
        ok = docker_driver.start_docker(vmid)
        return jsonify({"ok": bool(ok)}), (200 if ok else 500)
    from ..addressing import get_container_ip
    from .. import deploy
    ip = get_container_ip(vmid)
    if not ip:
        return jsonify({"error": "IP introuvable"}), 404
    try:
        r = deploy.agent_session().post(deploy.agent_url(ip, "/start"), timeout=5,
                                        headers=deploy.agent_headers(vmid))
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@bp.route("/api/containers/<int:vmid>/tally", methods=["GET", "POST"])
@require_perm("multiview.edit")
def tally(vmid):
    from ..addressing import get_container_ip
    import requests as req
    err = check_vmid_access(vmid, "operator")
    if err:
        return err
    ip = get_container_ip(vmid)
    if not ip:
        return jsonify({"error": "IP introuvable"}), 404
    try:
        if request.method == "GET":
            r = req.get(f"http://{ip}:8080/tally", timeout=2)
        else:
            r = req.post(f"http://{ip}:8080/tally", json=request.json, timeout=2)
        return (r.text, r.status_code, {"Content-Type": "application/json"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@bp.route("/api/containers/<int:vmid>/stop_script", methods=["POST"])
@require_perm("containers.deploy")
def stop_script(vmid):
    from ..database import db_get_container
    from .. import docker_compute
    _c = db_get_container(vmid)
    if _c and not docker_compute.is_compute_container(_c):
        from .. import docker_driver
        ok, msg = docker_driver.stop_docker(vmid)
        return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 500)
    from ..addressing import get_container_ip
    from .. import deploy
    ip = get_container_ip(vmid)
    if not ip:
        return jsonify({"error": "IP introuvable"}), 404
    try:
        r = deploy.agent_session().post(deploy.agent_url(ip, "/stop"), timeout=5,
                                        headers=deploy.agent_headers(vmid))
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Nœuds (CRUD, GPU, images, agent) ─────────────────────
# Extrait dans app/routes/nodes.py.

# ─── Enrôlement zéro-touch (jeton, USB, ISO iLO, PXE) ────────────────────────
# Extrait dans app/routes/enrollment.py.

# ─── Réseau par nœud (containers/io2110/interfaces/macvlan/préflight/stockage) ──
# Extrait dans app/routes/node_network.py + mcast_ranges.py + media_networks.py.

# ─── Users, i18n ──────────────────────────────────────────
# Extraits dans app/routes/users.py et app/routes/i18n.py.


# ─── Core plugins (services infrastructure) ──────────────────────────
# Extrait dans app/routes/core_services.py (registre core_plugins ; nommé différemment
# pour ne pas entrer en collision avec app.core_plugins).

from . import core_services  # noqa: F401 — registre de services (core_plugins)
from . import auth_pages     # noqa: F401 — setup/login/logout + widget systemd

# Domaines extraits en modules du paquet (tranche 2 : faible couplage). Chacun importe
# `bp` (et au besoin d'autres noms) depuis ce module — importés en dernier, une fois
# tout ce dont ils dépendent déjà défini ci-dessus.
from . import updates    # noqa: F401 — build de distribution + mise à jour entre instances
from . import ha         # noqa: F401 — haute disponibilité (paire de contrôleurs)
from . import peers      # noqa: F401 — registre de pairs (dépend de updates._my_identity)
from . import backup     # noqa: F401 — sauvegarde / restauration DB
from . import users      # noqa: F401 — comptes utilisateurs
from . import i18n       # noqa: F401 — éditeur de traductions

# Domaine nœuds/cluster (tranche 3, gros morceau). Ordre de dépendance : images (fondation :
# _IMAGES/_image_tag/_repo_root/_img_lock) → nodes (CRUD, GPU, agent — dépend de images) →
# enrollment (dépend de images._provision_shared_images) → node_network (dépend de nodes et
# images) → mcast_ranges / media_networks (indépendants).
from . import images          # noqa: F401
from . import nodes           # noqa: F401
from . import enrollment      # noqa: F401
from . import node_network    # noqa: F401
from . import mcast_ranges    # noqa: F401
from . import media_networks  # noqa: F401

# Moteur ST 2110 (2110_io/MTL) : flux composables + prép host DPDK/E810 (tranche 6).
# DOIT être importé avant nmos_detail (son domaine désormais réel destinataire de
# _mtl_media_port_count/_mtl_active_caps) : nmos_detail fait `from .mtl_engine import …` à son
# niveau module — mtl_engine doit déjà être dans sys.modules à ce moment-là.
from . import mtl_engine  # noqa: F401

# Agrégation NMOS/2110 (tranche 7) : receivers/senders detail + /api/io/mtl. Dépendance à double
# sens avec mtl_engine (cf. docstring de nmos_detail.py) — importé APRÈS mtl_engine.
from . import nmos_detail  # noqa: F401

# Réglages (API get/set/schema/overrides/logo/stats/logs) + home dashboard (tranche 8).
from . import settings_api    # noqa: F401
from . import home_dashboard  # noqa: F401

# Câblage (wire/unwire/insert_udc, snapshots, vues de disposition) — tranche 9.
from . import cabling  # noqa: F401

# Registre des plugins + shell de rubrique + proxy de contrôle générique, API Streams — tranche 10.
from . import plugin_registry  # noqa: F401
from . import catalogue_api   # noqa: F401
from . import streams_api      # noqa: F401

# Monitoring WebRTC par utilisateur + monitor dédié par player — tranche 11.
from . import monitor_routes  # noqa: F401

# Pages (rendu Jinja) + share links (page publique) — tranche 12.
from . import pages  # noqa: F401

# Télémétrie/diagnostic infra (fabric, monitoring panels, pyramide, membw, santé nœuds, GPU, shm) — tranche 13.
from . import monitoring_api  # noqa: F401

# Layouts multiview + Projets (CRUD, export/import, restore streamé) — tranche 14.
from . import projects_api  # noqa: F401
from . import macros_api    # noqa: F401
from . import tally_api     # noqa: F401
from . import labels_api    # noqa: F401

# PTP (historique/événements/logs/status/install/apply) — tranche 15 (dernière).
from . import ptp_routes  # noqa: F401

# Sonde ST 2110 (probe_2110) — analyseur ponctuel piloté par NMOS (Phase A).
from . import probe  # noqa: F401

# Bibliothèque de polices (Réglages → Polices) — cf. app/fonts.py.
from . import fonts_api  # noqa: F401

# Journaux DURABLES de conteneurs (journald lu sur l'hôte du nœud) — cf. app/journal.py.
from . import container_logs  # noqa: F401

# Routes bespoke par type de plugin (persistance DB — cf. app/routes/plugin_routes/__init__.py).
# Importé en dernier : chaque module y importe `bp` depuis ce package (et `_load_dc`/
# `_mixer_proxy` depuis .shared), qui doivent déjà exister au moment de l'import.
from . import plugin_routes  # noqa: F401

# Étalonnage CPU : campagne de mesure + profils garantis (cf. app/etalonnage.py).
from . import etalonnage_api  # noqa: F401

# Emplacements (rôles) : identité fonctionnelle stable adressée par les contrôleurs externes.
from . import roles_api  # noqa: F401

from . import habilitations_api  # noqa: F401

# Inventaire Docker par nœud : confronte ce que voit l'agent à la base (orphelins destructibles).
from . import inventaire  # noqa: F401
