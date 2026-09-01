# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import time
import threading
import logging
from . import settings

from .database import (db_upsert_container, db_update_status, db_delete_container,
                      db_increment_restarts, db_get_container, db_add_alert,
                      db_update_ip)

log = logging.getLogger(__name__)

# Volume média partagé (plugins Player/Recorder/Storage) : un dossier de l'hôte
# bind-monté dans chaque container média → fichiers partagés sans NFS. Valeurs en dur —
# éditer ici si besoin.
MEDIA_HOST_DIR = "/srv/mxl-media"
MEDIA_MOUNT    = "/mnt/media"

def ajouter_alerte(message, niveau="info", vmid=None, node_id=None, kind=None, params=None):
    """Alerte + trace log. `vmid`/`node_id`/`kind` = contexte MACHINE optionnel (cf.
    `database.ALERT_KINDS`) : passer None quand on ne sait pas, jamais une devinette.

    `message` peut être une CLÉ i18n (`alert.<…>`) accompagnée de `params` — forme à privilégier
    pour tout nouveau site d'appel : elle seule permet de rendre l'alerte dans la langue du
    lecteur (cf. `database._alert_cle`). Une phrase française reste acceptée."""
    canonique = db_add_alert(message, niveau, vmid=vmid, node_id=node_id, kind=kind, params=params)
    # Le journal reçoit la phrase, pas la clé ; et rien du tout si l'anti-rebond a tu la ligne
    # (sinon le journal contredirait la base sur ce qui a été retenu).
    if canonique:
        log.info(f"ALERTE [{niveau}] {canonique}")



def _cleanup_fabric_on_destroy(vmid):
    """Nettoie le registre du tissu de composition quand on détruit un conteneur :
      - retire la ligne ASSEMBLEUR (`asm:<vmid>`) si ce vmid était un mur logique shardé ;
      - retire la ligne SHARD si ce vmid était lui-même un shard (ref == vmid) ;
      - TORNE les shards devenus ORPHELINS (aucun mur parent vivant) — un shard PARTAGÉ (dédup)
        qu'un autre mur consomme encore est conservé.
    Auto-suffisant : ne dépend ni de `fabric_auto` ni d'un reconcile (sinon, suppression d'un mur =
    lignes/shards fantômes, vécu). Best-effort, ne lève jamais."""
    import json as _json
    from .database import db_fabric_all, db_fabric_delete
    sv = str(vmid)
    try:
        rows = db_fabric_all()
    except Exception:
        return
    # 1+2. retire la ligne propre à CE conteneur (assembleur du mur, et/ou shard si c'en était un)
    asm_key = "asm:%s" % vmid
    for r in rows:
        if r["signature"] == asm_key:
            db_fabric_delete(asm_key)
        elif r["kind"] in ("shard", "shared") and str(r.get("ref") or "") == sv:
            db_fabric_delete(r["signature"])
    # 3. shards orphelins : aucun parent (mur logique) n'a plus de ligne assembleur vivante
    rows = db_fabric_all()
    live_asm = {str(r["vmid"]) for r in rows if r["kind"] == "assembler" and r.get("vmid") is not None}
    orphan_refs = []
    for r in rows:
        if r["kind"] not in ("shard", "shared"):
            continue
        try:
            parents = _json.loads(r.get("parents") or "[]")
        except Exception:
            parents = []
        if not any(str(p) in live_asm for p in parents):
            db_fabric_delete(r["signature"])       # ligne retirée AVANT le teardown → pas de re-cascade
            ref = r.get("ref")
            if ref and str(ref).isdigit():
                orphan_refs.append(int(ref))
    for ref in orphan_refs:
        try:
            detruire_container(ref)                 # lignes déjà retirées → cleanup récursif = no-op
        except Exception as _e:
            log.warning("teardown shard orphelin %s : %s", ref, _e)


def detruire_container(vmid, progress=None):
    """Détruit le CT (stop vérifié + DELETE confirmé, 1 retry forcé).
    `progress(msg)` : callback optionnel recevant chaque sous-étape (utilisé
    par le RAZ pour streamer le détail ; None → seulement alertes + log).
    SÉRIALISÉ PAR VMID (vmlocks) : un destroy concurrent d'un deploy/restart
    du même vmid racerait sur l'agent-nœud / l'API Docker."""
    from .vmlocks import verrou_vmid
    with verrou_vmid(vmid, op="destroy"):
        return _detruire_container_locked(vmid, progress=progress)


def _detruire_container_locked(vmid, progress=None):
    _p = progress or (lambda m: None)
    # Libère les cœurs CPU épinglés (pool de pinning Docker), quel que soit le backend — évite les
    # allocations orphelines. Best-effort.
    try:
        from . import core_pool
        core_pool.release_cores(vmid)
    except Exception:
        pass
    # Libère le GPU alloué (sélecteur --gpus) — même esprit anti-orphelin. Best-effort, tous backends.
    try:
        from . import gpu_pool
        gpu_pool.release_gpu(vmid)
    except Exception:
        pass
    # Libère les réservations multicast (ledger atomique, cf. db_reserve_mcast) de tous les flux TX
    # de ce container — sinon ses adresses restent bloquées pour un owner_ref mort à jamais.
    try:
        from .database import db_release_mcast_prefix
        db_release_mcast_prefix(f"tx:{vmid}:")
    except Exception:
        pass
    # Supprime les liens RDMA que ce container ALIMENTAIT (il en était la source) — même famille
    # anti-orphelin que ci-dessus. Un lien réplique le flux d'un producteur PRÉCIS : celui-ci
    # détruit, le lien n'a plus d'objet, et un container recréé plus tard en est un AUTRE (vmid
    # neuf). Le laisser survivre laisserait deux containers de réplication tourner à vide sur deux
    # nœuds, et ferait espérer le retour d'une source qui ne reviendra pas. La suppression est en
    # CASCADE (les consommateurs du flux répliqué sont décâblés) — cf. services/rdma.
    try:
        from services import rdma as _rdma
        _liens = _rdma.liberer_liens_du_producteur(vmid)
        if _liens:
            _p(f"RDMA : {len(_liens)} lien(s) supprimé(s) (ce container en était la source)")
    except Exception as _e:
        log.warning(f"libération des liens RDMA du producteur {vmid} : {_e}")
    # …et les liens qui alimentaient ce container en tant que CONSOMMATEUR. Le teardown existait
    # déjà (`release_cable_link`), mais uniquement sur le chemin DÉCÂBLAGE : détruire un
    # consommateur, ou le redéployer sur un autre nœud, laissait ses liens derrière lui pour
    # toujours. Mesuré le 2026-08-07 : 86 liens pour un besoin réel d'environ 21, dont 56 vers deux
    # nœuds sans le moindre consommateur. On libère donc ici aussi, avant que la ligne DB (et donc
    # le câblage dérivé qui sert à décider) ne disparaisse.
    try:
        from services import rdma as _rdma
        from .routes.cabling import _flow_consumers_on_node as _conso
        from . import plugins as _pl
        import json as _json
        _c = db_get_container(vmid) or {}
        try:
            _dc = _json.loads(_c.get("deploy_config") or "{}") or {}
        except (TypeError, ValueError):
            _dc = {}
        _nid = _c.get("node_id")
        if _dc and _dc.get("type") and _nid is not None and _pl.is_plugin(_dc["type"]):
            _w = _pl.derive_wiring(_dc["type"], _c.get("hostname") or "", _dc.get("params") or {})
            _shms = {x.get("shm") for x in (_w.get("consumes") or []) if x.get("shm")}
            _n = 0
            for _shm in _shms:
                # `- 1` : CE container compte encore parmi les consommateurs à cet instant.
                if _conso(_shm, _nid) - 1 <= 0 and _rdma.release_cable_link(_shm, _nid):
                    _n += 1
            if _n:
                _p(f"RDMA : {_n} lien(s) libéré(s) (ce container en était le dernier consommateur)")
    except Exception as _e:
        log.warning(f"libération des liens RDMA du consommateur {vmid} : {_e}")
    # Destruction déléguée au driver. MTL = docker rm -f + xdp off ;
    # compute = docker rm -f simple (pas de NIC/xdp).
    _cd = db_get_container(vmid)
    if _cd:
        from . import docker_compute
        # Garde-fou anti-orphelin : la classification compute/MTL passe par is_mtl_type, qui lit le
        # REGISTRE plugins (needs_dpdk). Dans un contexte où le registre n'est pas chargé, un
        # conteneur MTL serait misclassé « compute » et son conteneur docker (bobi-mtl-<vmid>) resterait
        # orphelin (vécu : coco corrompu détruit côté DB mais bobi-mtl-2010 toujours up). On retire donc
        # explicitement TOUS les noms possibles sur le nœud avant de router le nettoyage spécifique.
        try:
            from .database import db_get_node
            _node = db_get_node(_cd.get("node_id"))
            if _node and _node.get("host"):
                from .host_ops import ssh_run
                import shlex as _shlex
                for _nm in {f"bobi-cmp-{vmid}", f"bobi-mtl-{vmid}", _cd.get("docker_name") or ""}:
                    if _nm:
                        ssh_run(_node["host"], f"docker rm -f {_shlex.quote(_nm)} >/dev/null 2>&1", timeout=30)
        except Exception as _e:
            log.warning(f"garde-fou anti-orphelin docker {vmid} : {_e}")
        if docker_compute.is_compute_container(_cd):
            _res = docker_compute.destroy_compute(vmid, progress=_p)
        else:
            from . import docker_driver
            _res = docker_driver.destroy_docker(vmid, progress=_p)
        # Nettoyage du tissu de composition : retire les lignes registre de CE conteneur (assembleur /
        # shard) et torne les shards devenus orphelins (plus aucun mur parent). Sinon, supprimer un mur
        # laissait ses shards + sa ligne asm fantômes (le reconcile ne nettoie pas si fabric_auto off).
        try:
            _cleanup_fabric_on_destroy(vmid)
        except Exception as _e:
            log.warning(f"nettoyage tissu après suppression {vmid} : {_e}")
        # Purge des ressources NMOS auto-seedées devenues orphelines (le conteneur n'existe plus →
        # son instance_uuid disparaît). Garde le pool fixe (label_locked)/servi/abonné. Best-effort.
        try:
            from services import nmos as _nmos
            _nmos.purge_orphan_resources(dry_run=False)
            # ⚠ La purge ne suffit PAS : elle nettoie le REGISTRE de ressources auto-seedées, mais
            # ne reconstruit pas le modèle en mémoire. Sans cette notification, les ressources du
            # conteneur détruit (ses ports MXL en particulier) restaient annoncées sur /x-nmos/
            # jusqu'à ce qu'un tout autre événement déclenche un rebuild — et, quand on est
            # enregistré auprès d'un registre, elles y restaient jusqu'à l'expiration du Node
            # entier. Un contrôleur continuait donc de proposer un routage vers un flux disparu.
            # Constaté au banc le 2026-08-31 : aucun chemin de destruction compute n'appelait
            # `notify_state_change` (seuls le déploiement et le chemin MTL le faisaient).
            _nmos.notify_state_change()
        except Exception as _e:
            log.warning(f"purge orphelins NMOS après suppression {vmid} : {_e}")
        return _res
    # Ligne introuvable en base : rien à détruire côté nœud, rien à purger.
    _p(f"container {vmid} introuvable en base")
    return


def update_resources(vmid, cores=None, memory=None, pinned_cores=None):
    """Persiste les ressources (cores/RAM/pinning) en DB. D Phase 2a : full-Docker — plus de
    reconfiguration Proxmox à chaud. Les cores/RAM s'appliquent au prochain (re)déploiement du
    conteneur Docker ; le pinning CPU est géré par core_pool au run."""
    from .database import db_update_resources, db_get_container
    c = db_get_container(vmid)
    if not c:
        ajouter_alerte("alert.deploy.resources_introuvable", "error",
                       params={"vmid": vmid})
        return False
    db_update_resources(vmid, cores=cores, memory=memory, pinned_cores=pinned_cores)
    ajouter_alerte("alert.deploy.resources_maj", "info", params={"vmid": vmid})
    return True


def changer_media_projet(vmid, project_id, media_host_dir):
    """Change le bind mount média d'un container vers un nouveau dossier projet.
    Stop → replace_media_bind → start. DB mise à jour UNIQUEMENT si tout réussit.
    media_host_dir=None → dossier global MEDIA_HOST_DIR (aucun projet)."""
    from .database import db_set_project, db_get_container
    target_dir = media_host_dir or MEDIA_HOST_DIR

    c = db_get_container(vmid)
    label = (c.get("hostname") if c else None) or str(vmid)
    ajouter_alerte("alert.deploy.media_changement_debut", "info", params={"label": label, "vmid": vmid})

    # Re-scoper un conteneur « compute » = rattacher le projet puis RECRÉER le conteneur
    # (deploy_compute relit project_id et bind le nouveau sous-dossier média).
    if c:
        import json as _json
        from .deploy import deployer_script
        # deploy_compute relit project_id en DB → il faut le poser AVANT le redéploiement,
        # mais le REMETTRE à l'ancien si le redéploiement échoue (sinon la DB prétend le
        # nouveau projet alors que le conteneur bind encore l'ancien dossier).
        prev_project_id = c.get("project_id")
        db_set_project(vmid, project_id)
        try:
            _dc = c.get("deploy_config")
            _dc = _json.loads(_dc) if isinstance(_dc, str) else (_dc or {})
        except Exception:
            _dc = {}
        if not _dc.get("type"):
            db_set_project(vmid, prev_project_id)
            ajouter_alerte("alert.deploy.media_type_inconnu", "error", params={"label": label})
            return False
        if not deployer_script(vmid, _dc["type"], _dc.get("params", {})):
            db_set_project(vmid, prev_project_id)
            ajouter_alerte("alert.deploy.media_redeploiement_echoue", "error", params={"label": label})
            return False
        ajouter_alerte("alert.deploy.media_maj", "info", params={"label": label})
        return True

    ajouter_alerte("alert.deploy.media_introuvable", "error", params={"label": label})
    return False


def redemarrer_container(vmid):
    # MTL --rm → re-run depuis le deploy_config. Compute (pas --rm) → docker start (ou
    # re-déploiement si le conteneur a disparu).
    # SÉRIALISÉ PAR VMID (vmlocks) — cf. deployer_script/detruire_container.
    from .vmlocks import verrou_vmid
    with verrou_vmid(vmid, op="restart"):
        # Un restart MANUEL sort le container de la quarantaine crash-loop (audit B3) :
        # l'opérateur reprend la main, l'auto-restart redémarre de zéro (backoff réinitialisé).
        from .metrics import reset_crash_loop
        reset_crash_loop(vmid)
        return _redemarrer_container_locked(vmid)


def _redemarrer_container_locked(vmid):
    _cd = db_get_container(vmid)
    if _cd:
        from . import docker_compute
        if docker_compute.is_compute_container(_cd):
            ajouter_alerte("alert.deploy.redemarrage_compute", "info", params={"vmid": vmid})
            return docker_compute.start_compute(vmid)
        from . import docker_driver
        ajouter_alerte("alert.deploy.redemarrage_docker", "info", params={"vmid": vmid})
        return docker_driver.start_docker(vmid)
    ajouter_alerte("alert.deploy.redemarrage_introuvable", "warning", params={"vmid": vmid})
    return False
