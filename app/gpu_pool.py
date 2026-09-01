# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Allocateur de GPU NVIDIA par nœud (sélecteur `docker run --gpus`).

Modèle = core_pool : un nœud GPU-capable déclare son nombre de GPU (`nodes.gpu_count`, défaut 1) ;
une table d'allocation par conteneur (`node_gpu_alloc`) attribue un INDEX de GPU à un vmid
(IDEMPOTENT au redéploiement) et renvoie le sélecteur `--gpus "device=<idx>"`. `release_gpu` libère
à la suppression. Contrairement au CPU (partitionné), un GPU se PARTAGE entre conteneurs (time-slicing
NVIDIA) sans danger — le banc Phase 0 a montré contexte ~158 Mio et contention bénigne. On alloue donc
en ROUND-ROBIN par occupation (équilibrage) plutôt qu'en exclusif : plusieurs vmid peuvent viser le
même index, ce qui est voulu (Modèle A : un conteneur GPU par mur, tous sur la même T4).
"""
import logging
from .database import get_db, db_get_node

log = logging.getLogger(__name__)


def _gpu_count(node):
    """Nb de GPU du nœud (colonne gpu_count, défaut 1 si gpu_capable, 0 sinon)."""
    if not node or not node.get("gpu_capable"):
        return 0
    try:
        n = int(node.get("gpu_count") or 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, n)


def allocate_gpu(node_id, vmid):
    """Attribue un INDEX de GPU au vmid sur le nœud (round-robin par occupation). IDEMPOTENT : si le
    vmid a déjà un GPU sur ce nœud → renvoie son sélecteur tel quel. Renvoie le sélecteur
    `docker --gpus` (ex. "device=0"), ou None si le nœud n'est pas GPU-capable."""
    if not node_id:
        return None
    node = db_get_node(node_id)
    ngpu = _gpu_count(node)
    if ngpu <= 0:
        return None
    with get_db() as db:
        row = db.execute("SELECT gpu_index FROM node_gpu_alloc WHERE node_id=? AND vmid=?",
                         (node_id, vmid)).fetchone()
        if row is not None:
            return "device=%d" % int(row["gpu_index"])
        # Round-robin : choisir l'index le MOINS chargé (équilibrage des contextes sur les GPU).
        counts = {i: 0 for i in range(ngpu)}
        for r in db.execute("SELECT gpu_index, COUNT(*) AS c FROM node_gpu_alloc WHERE node_id=? "
                            "GROUP BY gpu_index", (node_id,)).fetchall():
            if 0 <= int(r["gpu_index"]) < ngpu:
                counts[int(r["gpu_index"])] = int(r["c"])
        idx = min(counts, key=lambda i: counts[i])
        db.execute("INSERT OR REPLACE INTO node_gpu_alloc (node_id, gpu_index, vmid) VALUES (?,?,?)",
                   (node_id, idx, vmid))
    return "device=%d" % idx


def release_gpu(vmid):
    """Libère le GPU alloué au vmid (à la suppression du conteneur). Best-effort, tous backends."""
    try:
        with get_db() as db:
            db.execute("DELETE FROM node_gpu_alloc WHERE vmid=?", (vmid,))
    except Exception as e:
        log.warning("release_gpu vmid=%s: %s", vmid, e)


def gpu_par_vmid(node_id):
    """{vmid: index_gpu} des conteneurs qui tiennent un GPU sur ce nœud.

    Sert la carte CPU du Monitoring : un cœur qui fait tourner un conteneur GPU doit se distinguer
    d'un cœur de calcul ordinaire. Sans ça, rien ne montre qu'un mur GPU est épinglé du MAUVAIS côté
    du bus — le socket qui porte la carte graphique est une information de placement, pas un détail
    d'inventaire (cf. core_pool.capacite_par_socket)."""
    if not node_id:
        return {}
    try:
        with get_db() as db:
            return {int(r["vmid"]): int(r["gpu_index"]) for r in db.execute(
                "SELECT vmid, gpu_index FROM node_gpu_alloc WHERE node_id=?", (node_id,))}
    except Exception as e:
        log.debug("gpu_par_vmid(%s): %s", node_id, e)
        return {}


def gpu_status(node_id):
    """{count, used, per_gpu:{idx:nb_contextes}} pour l'UI Nœuds. count=0 si non GPU-capable."""
    node = db_get_node(node_id)
    ngpu = _gpu_count(node)
    per = {i: 0 for i in range(ngpu)}
    used = 0
    if ngpu > 0:
        with get_db() as db:
            for r in db.execute("SELECT gpu_index, COUNT(*) AS c FROM node_gpu_alloc WHERE node_id=? "
                               "GROUP BY gpu_index", (node_id,)).fetchall():
                if 0 <= int(r["gpu_index"]) < ngpu:
                    per[int(r["gpu_index"])] = int(r["c"])
            used = db.execute("SELECT COUNT(*) AS c FROM node_gpu_alloc WHERE node_id=?",
                             (node_id,)).fetchone()["c"]
    return {"count": ngpu, "used": used, "per_gpu": per}
