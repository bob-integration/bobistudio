# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Watch the CPU guarantees once they are set — point 4 of the plan agreed with Cyril on 2026-11-07.

A guarantee is a decision taken once (calibrate, then reserve). Two things can silently break it
afterwards, and nothing said so:

  1. THE RESERVATION IS NO LONGER APPLIED. The cores are counted to the container in
     `node_core_alloc`, but its real cpuset (measured on the host) is something else — a recreation
     on an older spec, a failed `docker update` — or a SHARED container runs on top of those cores
     (its cpuset was computed before the reservation). `core_pool.vue_coeurs` already computes both
     (`coherent`, `deborde`) for the cores window; no alert ever read them. Any dedicated
     container, whatever its level: a reservation that is not where it says is a fault anywhere.

  2. THE MEASUREMENT NO LONGER COVERS REALITY. An on-air container, calibrated and reserved, now
     consumes more than its calibrated peak (the calibration missed a regime, or the configuration
     grew without changing its signature), or nearly fills its reservation (≥ 90 %). Only on-air
     containers: the others run best effort by decision, and their peaks are nobody's promise.

Edge-triggered and persisted (`EtatEpisodes`): announced once, lifted once (« restored ») — never a
stream. A consumption alarm needs CONSUMPTION_PASSES consecutive passes (one hot sample is not a
regime: a transition, a GC); a placement alarm needs PLACEMENT_PASSES (a recreation or a hot
re-apply is briefly incoherent by nature). A node whose placement cannot be measured is not judged
— no alarm, and no lift either: unknown is not « fine ».

Read only: it never moves a core. Called from `placement.verifier_tous`, throttled per node.
"""
import logging
import threading
import time

from .database import db_add_alert, db_get_node
from .episodes import EtatEpisodes, _SEP

log = logging.getLogger(__name__)

PERIOD_S = 60.0           # one pass per node per minute (the surveillance tick is 5 s)
CONSUMPTION_PASSES = 3    # ~3 min above the peak before saying so
PLACEMENT_PASSES = 2
SATURATION = 0.9          # consumption / reservation, same threshold as the resources brick

_episodes = EtatEpisodes("guarantee_watch")
_streak = {}              # (node, vmid, code) → consecutive passes seen (memory only)
_last = {}                # node → time of last pass
_lock = threading.Lock()

# code → (level, i18n key)
CODES = {
    "reservation_not_applied": ("error", "alert.resource.guarantee_not_applied"),
    "shared_on_dedicated": ("error", "alert.resource.guarantee_shared_on_dedicated"),
    "reservation_saturated": ("error", "alert.resource.guarantee_saturated"),
    "peak_exceeded": ("warning", "alert.resource.guarantee_peak_exceeded"),
    "io_cores_shared": ("error", "alert.resource.guarantee_io_cores_shared"),
}


_USAGE = ("reservation_saturated", "peak_exceeded")


def _fmt(pct):
    return ("%.1f" % (float(pct) / 100.0)).replace(".", ",") if pct is not None else "—"


def defects(node_id):
    """{(vmid, code): params} seen NOW on this node, or None when the node cannot be judged."""
    from . import core_pool, etalonnage
    from .database import db_get_container
    from .docker_compute import is_mtl_type, _type_of
    view = core_pool.vue_coeurs(node_id)
    if not view.get("releve"):
        return None
    out = {}
    for line in view.get("dedies") or []:
        v = line.get("vmid")
        if is_mtl_type(_type_of(db_get_container(v) or {})):
            continue        # a 2110 engine's cpuset holds its service cores too: not comparable
        if line.get("coherent") is False:
            out[(v, "reservation_not_applied")] = {"h": line.get("hostname") or v,
                                                   "voulu": line.get("cpuset_reserve") or "",
                                                   "applique": line.get("cpuset_applique") or "—"}
    # Node I/O (DeckLink) holds WHOLE physical cores: any other container measured on one of their
    # logical CPUs — sibling included — is the contention that made the Z8 flash black on 30/10.
    core_of = core_pool.core_map_cached(node_id) or {}
    held = core_pool.physical_allocations(node_id) if core_of else {}
    for io_vmid, cores in held.items():
        phys = {core_of[c] for c in cores if c in core_of}
        others = []
        for line in (view.get("dedies") or []) + (view.get("partages") or []):
            if line.get("vmid") == io_vmid:
                continue
            on = sorted(c for c in core_pool.parse_cpuset(line.get("cpuset_applique") or "")
                        if core_of.get(c) in phys)
            if on:
                others.append((line.get("hostname") or line.get("vmid"), on))
        if others:
            c = db_get_container(io_vmid) or {}
            out[(io_vmid, "io_cores_shared")] = {
                "h": c.get("hostname") or io_vmid,
                "voisins": ", ".join("%s (%s)" % (h, core_pool.fmt_cpuset(on)) for h, on in others)}
    for line in view.get("partages") or []:
        if line.get("deborde"):
            out[(line.get("vmid"), "shared_on_dedicated")] = {"h": line.get("hostname") or line.get("vmid"),
                                                              "coeurs": line["deborde"]}
    for r in etalonnage.ressources(node_id=node_id):
        if r.get("level") != "dedicated" or r.get("reserve_pct") is None or r.get("etat") != "etalonne":
            continue
        cons, pic = r.get("consomme_pct"), (r.get("mesure") or {}).get("max")
        if cons is None:
            continue
        params = {"h": r.get("hostname") or r["vmid"], "conso": _fmt(cons), "pic": _fmt(pic),
                  "reserve": _fmt(r["reserve_pct"])}
        if cons >= SATURATION * r["reserve_pct"]:
            out[(r["vmid"], "reservation_saturated")] = params
        elif pic and cons > pic:
            out[(r["vmid"], "peak_exceeded")] = params
    return out


def check_node(node_id, force=False):
    """One pass on one node: count, announce at the transition, lift what disappeared."""
    now = time.time()
    with _lock:
        if not force and now - _last.get(node_id, 0) < PERIOD_S:
            return None
        _last[node_id] = now
    found = defects(node_id)
    if found is None:
        return None
    name = (db_get_node(node_id) or {}).get("name") or node_id
    seen = set()
    for (vmid, code), params in found.items():
        key = (str(node_id), str(vmid), code)
        seen.add(key)
        _streak[key] = _streak.get(key, 0) + 1
        need = CONSUMPTION_PASSES if code in _USAGE else PLACEMENT_PASSES
        if _streak[key] < need or _episodes.get(key):
            continue
        level, msg = CODES[code]
        _episodes.poser(key, level)
        db_add_alert(msg, level, vmid=vmid, node_id=node_id, kind="resource", params=dict(params, n=name))
    for k in [k for k in _streak if k[0] == str(node_id) and k not in seen]:
        _streak.pop(k, None)
    for txt in _episodes.cles():
        parts = txt.split(_SEP)
        if len(parts) != 3 or parts[0] != str(node_id) or tuple(parts) in seen:
            continue
        _episodes.retirer(tuple(parts))
        # Above the peak → nearly full (or back): the container did not return within its guarantee,
        # its consumption alarm changed. Saying « restored » there would announce the opposite.
        if parts[2] in _USAGE and any((parts[0], parts[1], c) in seen for c in _USAGE):
            continue
        vmid = int(parts[1]) if parts[1].isdigit() else None
        from .database import db_get_container
        host = (db_get_container(vmid) or {}).get("hostname") if vmid else None
        msg = ("alert.resource.guarantee_restored_usage" if parts[2] in _USAGE
               else "alert.resource.guarantee_restored_placement")
        db_add_alert(msg, "info", vmid=vmid, node_id=node_id, kind="resource",
                     params={"n": name, "h": host or ("#" + parts[1])})
    return found


def flush():
    _episodes.flush()
