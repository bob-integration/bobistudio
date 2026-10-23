# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit de l'adressage multicast des sorties 2110 — deux anomalies que rien ne signalait.

1. GROUPE PARTAGÉ. L'abonnement IGMP porte sur l'ADRESSE DE GROUPE, pas sur le port : deux flux
   sur la même adresse ne sont pas dissociables côté récepteur. Qui s'abonne à l'un reçoit l'autre,
   et un récepteur qui ne trie pas par port les mélange. Constaté à Horace le 2026-10-08 : l'audio
   de TX6 était émis sur le groupe de la vidéo de TX2, l'audio de TX2 sur celui de la vidéo de TX1.
   Le SNP signalait « Video Out Of Range Packets ». Ces partages dataient de JUILLET, sans une
   alarme en dix semaines.

2. MOTEUR HORS PLAN. Le plan d'adressage déterministe (une adresse par flux, déduite du rang —
   `allocations._plan_offset`) date du 2026-07-27. Il s'applique aux NOUVELLES adresses ; un moteur
   adressé avant n'y passe que si quelqu'un clique « ⊞ Plan multicast ». Le moteur d'Horace, créé
   la veille, a gardé l'ancien adressage sans que rien le dise.

Les deux vérifications lisent la BASE (ce que le moteur reçoit au déploiement), pas le réseau : un
émetteur tiers sur nos adresses reste invisible ici.

Tout tourne dans un THREAD, jamais en ligne dans `surveillance()` : la simulation du plan lit la
base flux par flux, et la boucle de surveillance ne doit attendre personne (cf. la réconciliation
RDMA qui l'avait bloquée). Alerte par TRANSITION, comme les autres détecteurs."""
import json
import logging
import threading
import time

log = logging.getLogger(__name__)

SHARED_EVERY_S = 60       # groupes partagés : lecture de base seule, peu coûteuse
PLAN_EVERY_S = 300        # écart au plan : simulation complète, plus chère
DETAIL_MAX = 4            # groupes cités dans le message ; le reste est compté

_last_shared = 0.0
_last_plan = 0.0
_running = threading.Lock()
_shared_alerted = {}      # vmid → frozenset des groupes signalés (re-signale si l'ensemble change)
_plan_alerted = {}        # vmid → nb de flux hors plan signalé


def tx_destinations(params):
    """Toutes les destinations configurées des sorties d'un moteur : une entrée par flux et par
    chemin 2022-7. [{slot, essence, sub, leg, ip, port}]. Un flux sans adresse est ignoré."""
    out = []
    for i, t in enumerate((params or {}).get("tx_slots") or []):
        t = t or {}
        for leg, sfx in ((0, ""), (1, "_leg1")):
            if t.get("multicast_ip" + sfx):
                out.append({"slot": i, "essence": "video", "sub": 0, "leg": leg,
                            "ip": t["multicast_ip" + sfx], "port": t.get("dest_port" + sfx)})
            if t.get("anc_multicast_ip" + sfx):
                out.append({"slot": i, "essence": "anc", "sub": 0, "leg": leg,
                            "ip": t["anc_multicast_ip" + sfx], "port": t.get("anc_dest_port" + sfx)})
            for ai, a in enumerate(t.get("audios") or []):
                a = a or {}
                if a.get("multicast_ip" + sfx):
                    out.append({"slot": i, "essence": "audio", "sub": ai, "leg": leg,
                                "ip": a["multicast_ip" + sfx], "port": a.get("dest_port" + sfx)})
    return out


def flow_label(hostname, d):
    """`2110-io-dl360 TX2 audio1` — numérotation 1-based, comme l'affichent les SDP et l'UI."""
    ess = d["essence"] + (str(d["sub"] + 1) if d["essence"] == "audio" else "")
    leg = " (B)" if d.get("leg") else ""
    return "%s TX%d %s%s" % (hostname, d["slot"] + 1, ess, leg)


def shared_groups(engines):
    """`engines` = {vmid: (hostname, params)}. Retourne {groupe: [(vmid, libellé)…]} pour chaque
    adresse portée par PLUSIEURS flux — entre moteurs aussi : deux moteurs du même plateau sur la
    même adresse se mélangent exactement de la même façon."""
    par_groupe = {}
    for vmid, (hn, params) in engines.items():
        for d in tx_destinations(params):
            par_groupe.setdefault(d["ip"], []).append((vmid, flow_label(hn, d)))
    return {ip: fl for ip, fl in par_groupe.items() if len(fl) > 1}


def _engines():
    """Les moteurs 2110 en base : {vmid: (hostname, params)}."""
    from .database import db_get_containers
    out = {}
    for c in db_get_containers():
        try:
            dc = json.loads(c.get("deploy_config") or "{}") or {}
        except Exception:
            continue
        if dc.get("type") == "2110_io":
            out[c["vmid"]] = (c.get("hostname") or str(c["vmid"]), dc.get("params") or {})
    return out


def check_shared(engines, add_alert):
    """Alerte par transition sur les groupes partagés. `add_alert` = `db_add_alert` (injecté pour
    les tests). Un moteur est alerté sur les groupes qui le CONCERNENT ; l'alerte repart si
    l'ensemble de ses groupes partagés change (un nouveau partage ne doit pas passer inaperçu
    derrière un ancien déjà signalé)."""
    partages = shared_groups(engines)
    for vmid, (hn, _p) in engines.items():
        siens = {ip: fl for ip, fl in partages.items() if any(v == vmid for v, _l in fl)}
        cle = frozenset(siens)
        avant = _shared_alerted.get(vmid, frozenset())
        if cle and cle != avant:
            detail = " ; ".join("%s : %s" % (ip, ", ".join(l for _v, l in fl))
                                for ip, fl in sorted(siens.items())[:DETAIL_MAX])
            if len(siens) > DETAIL_MAX:
                detail += " ; …"
            add_alert("alert.net.mcast_groupe_partage", "warning", vmid=vmid, kind="net",
                      params={"h": hn, "n": len(siens), "detail": detail})
        elif not cle and avant:
            add_alert("alert.net.mcast_groupe_partage_resolu", "info", vmid=vmid, kind="net",
                      params={"h": hn})
        if cle:
            _shared_alerted[vmid] = cle
        else:
            _shared_alerted.pop(vmid, None)
    for vmid in list(_shared_alerted):        # moteur détruit : on oublie son état
        if vmid not in engines:
            _shared_alerted.pop(vmid, None)


def check_plan(engines, add_alert, simulate):
    """Alerte par transition quand un moteur n'est pas aligné sur le plan d'adressage.
    `simulate(vmid)` = `allocations.plan_tx_multicast(vmid, appliquer=False)` (injecté)."""
    for vmid, (hn, _p) in engines.items():
        try:
            diff, err = simulate(vmid)
        except Exception as e:                                             # noqa: BLE001
            log.debug("audit plan multicast %s : %s", vmid, e)
            continue
        if err:
            continue
        n = sum(1 for d in diff or [] if d.get("etat") == "a_changer")
        avant = _plan_alerted.get(vmid, 0)
        if n and n != avant:
            add_alert("alert.net.mcast_hors_plan", "warning", vmid=vmid, kind="net",
                      params={"h": hn, "n": n})
        elif not n and avant:
            add_alert("alert.net.mcast_hors_plan_resolu", "info", vmid=vmid, kind="net",
                      params={"h": hn})
        if n:
            _plan_alerted[vmid] = n
        else:
            _plan_alerted.pop(vmid, None)
    for vmid in list(_plan_alerted):
        if vmid not in engines:
            _plan_alerted.pop(vmid, None)


def maybe_audit():
    """Point d'appel de la surveillance : lance l'audit dans un thread si son échéance est passée.
    Ne bloque jamais l'appelant ; jamais deux audits à la fois."""
    global _last_shared, _last_plan
    now = time.monotonic()
    do_shared = now - _last_shared >= SHARED_EVERY_S
    do_plan = now - _last_plan >= PLAN_EVERY_S
    if not (do_shared or do_plan) or not _running.acquire(blocking=False):
        return
    if do_shared:
        _last_shared = now
    if do_plan:
        _last_plan = now

    def _worker():
        try:
            from .database import db_add_alert
            engines = _engines()
            if do_shared:
                check_shared(engines, db_add_alert)
            if do_plan:
                from .allocations import plan_tx_multicast
                check_plan(engines, db_add_alert, lambda v: plan_tx_multicast(v, appliquer=False))
        except Exception as e:                                             # noqa: BLE001
            log.warning("audit multicast : %s", e)
        finally:
            _running.release()

    threading.Thread(target=_worker, daemon=True, name="mcast-audit").start()
